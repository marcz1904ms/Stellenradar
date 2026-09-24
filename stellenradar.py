"""Stellenradar: sammelt HR-Stellen, bewertet sie und erzeugt eine Übersichtsseite.

Quellen
  1. Jobbörse der Bundesagentur für Arbeit (öffentliche Schnittstelle)
  2. Optional: Personio-Karriereseiten ausgewählter Wunscharbeitgeber

Ausgabe
  docs/index.html   Übersichtsseite (wird über GitHub Pages angezeigt)
  data/stellen.json Gedächtnis, damit neue Stellen erkannt werden
"""

import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

BASIS = Path(__file__).parent
CONFIG = BASIS / "config.yaml"
GEDAECHTNIS = BASIS / "data" / "stellen.json"
VORLAGE = BASIS / "vorlage.html"
AUSGABE = BASIS / "docs" / "index.html"

BA_URLS = [
    "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobs",
    "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs",
]
BA_HEADER = {"X-API-Key": "jobboerse-jobsuche", "User-Agent": "Stellenradar (privat)"}
BA_DETAIL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{}"

HEUTE = datetime.now(timezone.utc).date()


# ----------------------------------------------------------------------------
# Quellen
# ----------------------------------------------------------------------------

def ba_suche(begriff, region, befristung, tage):
    """Eine Suche bei der Arbeitsagentur. befristung: 1 = befristet, 2 = unbefristet."""
    params = {
        "was": begriff,
        "wo": region["ort"],
        "umkreis": region["umkreis"],
        "angebotsart": 1,          # Arbeit (keine Ausbildung, kein Praktikum)
        "befristung": befristung,
        "zeitarbeit": "false",     # Zeitarbeit ausschließen
        "veroeffentlichtseit": tage,
        "size": 100,
        "page": 1,
    }
    for url in BA_URLS:
        try:
            antwort = requests.get(url, headers=BA_HEADER, params=params, timeout=30)
            if antwort.status_code == 200:
                return antwort.json().get("stellenangebote", []) or []
        except (requests.RequestException, ValueError):
            continue
    print(f"  Hinweis: keine Antwort für '{begriff}' in {region['ort']}", file=sys.stderr)
    return []


def ba_stelle_umwandeln(roh, region_name, befristet):
    refnr = roh.get("refnr") or roh.get("referenznummer")
    if not refnr:
        return None
    ort = roh.get("arbeitsort") or {}
    if isinstance(ort, list):
        ort = ort[0] if ort else {}
    return {
        "id": "ba:" + refnr,
        "titel": (roh.get("titel") or roh.get("beruf") or "").strip(),
        "arbeitgeber": (roh.get("arbeitgeber") or "").strip(),
        "ort": " ".join(x for x in [ort.get("plz", ""), ort.get("ort", "")] if x).strip(),
        "region": region_name,
        "veroeffentlicht": (roh.get("aktuelleVeroeffentlichungsdatum")
                            or roh.get("modifikationsTimestamp", "")[:10] or ""),
        "link": roh.get("externeUrl") or BA_DETAIL.format(refnr),
        "befristet": befristet,
        "quelle": "Arbeitsagentur",
    }


def personio_firma(firma):
    url = f"https://{firma['kennung']}.jobs.personio.de/xml"
    try:
        antwort = requests.get(url, timeout=30)
        antwort.raise_for_status()
        wurzel = ET.fromstring(antwort.content)
    except Exception:
        print(f"  Hinweis: Personio-Seite von {firma['name']} nicht erreichbar", file=sys.stderr)
        return []
    stellen = []
    for pos in wurzel.findall("position"):
        pid = (pos.findtext("id") or "").strip()
        if not pid:
            continue
        stellen.append({
            "id": f"personio:{firma['kennung']}:{pid}",
            "titel": (pos.findtext("name") or "").strip(),
            "arbeitgeber": firma["name"],
            "ort": (pos.findtext("office") or "").strip(),
            "region": "Wunscharbeitgeber",
            "veroeffentlicht": (pos.findtext("createdAt") or "")[:10],
            "link": f"https://{firma['kennung']}.jobs.personio.de/job/{pid}",
            "befristet": (pos.findtext("employmentType") or "").strip().lower() == "temporary",
            "quelle": "Personio",
        })
    return stellen


# ----------------------------------------------------------------------------
# Bewertung
# ----------------------------------------------------------------------------

def enthaelt(text, begriffe):
    return [b for b in begriffe if b.lower() in text]


def enthaelt_wort(text, begriffe):
    treffer = []
    for b in begriffe:
        if re.search(r"(?<![a-zäöüß])" + re.escape(b.lower()) + r"(?![a-zäöüß])", text):
            treffer.append(b)
    return treffer


def bewerten(stelle, cfg):
    titel = " " + stelle["titel"].lower() + " "
    firma = " " + stelle["arbeitgeber"].lower() + " "

    punkte, stufe, stufe_punkte = 0, "", 0
    for s in cfg["bewertung"]:
        if enthaelt(titel, s["begriffe"]) and s["punkte"] > stufe_punkte:
            stufe, stufe_punkte = s["stufe"], s["punkte"]
    punkte = stufe_punkte

    ausgeblendet = ""
    ausschluss = enthaelt(titel, cfg["ausschluss_titel"])
    if ausschluss:
        ausgeblendet = "Titel enthält „" + ausschluss[0] + "“"
    elif enthaelt(titel, cfg["ausschluss_wenn_allein"]) and stufe_punkte < 3:
        ausgeblendet = "Schwerpunkt Employer Branding"
    elif punkte == 0:
        ausgeblendet = "Kein HR-Bezug im Titel"

    hinweise = []
    if stelle["befristet"]:
        hinweise.append("befristet")
    if enthaelt(titel, cfg["markierung_seniorität"]):
        hinweise.append("Seniorität prüfen")
    if enthaelt(titel, cfg["markierung_praktikum"]):
        hinweise.append("Praktikum/Trainee")
    if enthaelt(firma, cfg["markierung_personaldienstleister"]):
        hinweise.append("Personaldienstleister?")
    if enthaelt_wort(firma, cfg["markierung_oeffentlich"]):
        hinweise.append("öffentlicher Dienst")

    stelle.update({"punkte": punkte, "stufe": stufe, "hinweise": hinweise,
                   "ausgeblendet": ausgeblendet})
    return stelle


# ----------------------------------------------------------------------------
# Ablauf
# ----------------------------------------------------------------------------

def schluessel(stelle):
    """Erkennt dieselbe Stelle, auch wenn sie über mehrere Suchbegriffe kam."""
    return re.sub(r"\W+", "", (stelle["titel"] + stelle["arbeitgeber"]).lower())


def sammeln(cfg):
    gefunden = {}
    befristungen = [2] + ([1] if cfg.get("befristete_stellen_zeigen", True) else [])
    for region in cfg["regionen"]:
        print(f"Suche in {region['name']} …")
        for begriff in cfg["suchbegriffe"]:
            for b in befristungen:
                for roh in ba_suche(begriff, region, b, cfg.get("tage_zurueck", 30)):
                    stelle = ba_stelle_umwandeln(roh, region["name"], befristet=(b == 1))
                    if stelle and stelle["id"] not in gefunden:
                        gefunden[stelle["id"]] = stelle
                time.sleep(0.4)
    for firma in cfg.get("personio_firmen") or []:
        print(f"Prüfe Personio: {firma['name']} …")
        for stelle in personio_firma(firma):
            gefunden.setdefault(stelle["id"], stelle)

    # Doppelte (gleicher Titel beim gleichen Arbeitgeber) zusammenfassen
    eindeutig = {}
    for stelle in gefunden.values():
        eindeutig.setdefault(schluessel(stelle), stelle)
    return list(eindeutig.values())


def mit_gedaechtnis_abgleichen(stellen, neu_tage):
    alt = {}
    if GEDAECHTNIS.exists():
        alt = json.loads(GEDAECHTNIS.read_text(encoding="utf-8"))
    for s in stellen:
        s["zuerst_gesehen"] = alt.get(s["id"], {}).get("zuerst_gesehen", HEUTE.isoformat())
        s["neu"] = (HEUTE - date.fromisoformat(s["zuerst_gesehen"])).days < neu_tage
    GEDAECHTNIS.parent.mkdir(exist_ok=True)
    GEDAECHTNIS.write_text(
        json.dumps({s["id"]: {"zuerst_gesehen": s["zuerst_gesehen"], "titel": s["titel"]}
                    for s in stellen}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    return stellen


def seite_schreiben(stellen, cfg):
    stellen.sort(key=lambda s: s["veroeffentlicht"], reverse=True)
    stellen.sort(key=lambda s: (-s["punkte"], not s["neu"]))
    daten = {
        "stand": datetime.now(timezone(timedelta(hours=2))).strftime("%d.%m.%Y, %H:%M Uhr"),
        "regionen": [r["name"] for r in cfg["regionen"]]
                    + (["Wunscharbeitgeber"] if cfg.get("personio_firmen") else []),
        "stellen": stellen,
    }
    json_text = json.dumps(daten, ensure_ascii=False).replace("</", "<\\/")
    seite = VORLAGE.read_text(encoding="utf-8").replace("__DATEN__", json_text)
    AUSGABE.parent.mkdir(exist_ok=True)
    AUSGABE.write_text(seite, encoding="utf-8")


def main():
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    stellen = [bewerten(s, cfg) for s in sammeln(cfg)]
    stellen = mit_gedaechtnis_abgleichen(stellen, cfg.get("neu_fuer_tage", 3))
    seite_schreiben(stellen, cfg)
    sichtbar = [s for s in stellen if not s["ausgeblendet"]]
    print(f"Fertig: {len(sichtbar)} passende Stellen, "
          f"davon {sum(s['neu'] for s in sichtbar)} neu, "
          f"{len(stellen) - len(sichtbar)} ausgeblendet.")


if __name__ == "__main__":
    main()
