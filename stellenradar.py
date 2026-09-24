"""Stellenradar: sammelt HR-Stellen, bewertet sie und erzeugt eine Übersichtsseite.

Quellen
  1. Jobbörse der Bundesagentur für Arbeit (öffentliche Schnittstelle)
  2. Karriereseiten der Arbeitgeber aus arbeitgeber.csv (siehe karriereseiten.py)

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

import karriereseiten

BASIS = Path(__file__).parent
CONFIG = BASIS / "config.yaml"
GEDAECHTNIS = BASIS / "data" / "stellen.json"
VORLAGE = BASIS / "vorlage.html"
AUSGABE = BASIS / "docs" / "index.html"
ARBEITGEBER = BASIS / "arbeitgeber.csv"

BA_URLS = [
    "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs",
    "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/app/jobs",
    "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobs",
]
BA_HEADER = {
    "X-API-Key": "jobboerse-jobsuche",
    "User-Agent": "Jobsuche/2.9.2 (de.arbeitsagentur.jobboerse; build:1077; iOS 15.1.0) Alamofire/5.4.4",
    "Accept": "application/json",
}
BA_DETAIL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{}"

HEUTE = datetime.now(timezone.utc).date()


# ----------------------------------------------------------------------------
# Quellen
# ----------------------------------------------------------------------------

PROTOKOLL = {"anfragen": 0, "erfolgreich": 0, "fehler": {}}
_funktionierende_url = None


def ba_suche(begriff, region, befristung, tage):
    """Eine Suche bei der Arbeitsagentur. befristung: 1 = befristet, 2 = unbefristet."""
    global _funktionierende_url
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
    PROTOKOLL["anfragen"] += 1
    urls = [_funktionierende_url] if _funktionierende_url else BA_URLS
    for url in urls:
        try:
            antwort = requests.get(url, headers=BA_HEADER, params=params, timeout=30)
        except requests.RequestException as e:
            PROTOKOLL["fehler"][f"{'/'.join(url.split('/')[-3:])} Verbindung: {type(e).__name__}"] = 1
            continue
        if antwort.status_code != 200:
            schluessel = f"{'/'.join(url.split('/')[-3:])} Status {antwort.status_code}"
            PROTOKOLL["fehler"][schluessel] = PROTOKOLL["fehler"].get(schluessel, 0) + 1
            continue
        try:
            daten = antwort.json()
        except ValueError:
            PROTOKOLL["fehler"]["Antwort ist kein JSON"] = 1
            continue
        liste = daten.get("stellenangebote") or daten.get("ergebnisliste") or []
        if liste and not PROTOKOLL.get("beispiel"):
            PROTOKOLL["beispiel"] = liste[0]
            print(f"  Felder einer Stelle: {list(liste[0])}")
        _funktionierende_url = url
        PROTOKOLL["erfolgreich"] += 1
        return liste
    return []


def _text(wert):
    """Holt einen Namen aus Text oder aus einem verschachtelten Feld."""
    if isinstance(wert, dict):
        for k in ("name", "titel", "bezeichnung", "text"):
            if isinstance(wert.get(k), str):
                return wert[k]
        return ""
    return wert if isinstance(wert, str) else ""


def _erstes(roh, *felder):
    for f in felder:
        if roh.get(f):
            return roh[f]
    return None


def ba_stelle_umwandeln(roh, region_name, befristet):
    # Manche Versionen verpacken die Stelle noch einmal
    if len(roh) == 1 and isinstance(next(iter(roh.values())), dict):
        roh = next(iter(roh.values()))
    refnr = _erstes(roh, "refnr", "referenznummer")
    kennung = refnr or _erstes(roh, "hashId", "id")
    if not kennung:
        return None
    ort = _erstes(roh, "arbeitsort", "arbeitsorte") or {}
    if isinstance(ort, list):
        ort = ort[0] if ort else {}
    if not isinstance(ort, dict):
        ort = {"ort": str(ort)}
    datum = str(_erstes(roh, "aktuelleVeroeffentlichungsdatum", "veroeffentlichungsdatum",
                        "ersteVeroeffentlichungsdatum", "veroeffentlichtAm", "datumVeroeffentlichung") or "")[:10]
    if not re.match(r"\d{4}-\d{2}-\d{2}", datum):
        # Neuere Versionen benennen das Feld anders: jedes Datumsfeld mit "veroeffentlich" nehmen
        datum = next((str(v)[:10] for k, v in roh.items()
                      if "veroeffentlich" in k.lower() and re.match(r"\d{4}-\d{2}-\d{2}", str(v))), "")
    if not datum:
        datum = str(roh.get("modifikationsTimestamp") or "")[:10]
    link = _erstes(roh, "externeUrl", "externeURL")
    if not link:
        link = BA_DETAIL.format(refnr) if refnr else (
            "https://www.arbeitsagentur.de/jobsuche/suche?was=" + requests.utils.quote(_text(_erstes(roh, "titel", "stellenangebotsTitel", "beruf"))))
    return {
        "id": "ba:" + str(kennung),
        "titel": _text(_erstes(roh, "titel", "stellenangebotsTitel", "stellentitel", "beruf") or "").strip(),
        "arbeitgeber": _text(_erstes(roh, "arbeitgeber", "arbeitgeberName", "firma") or "").strip(),
        "ort": " ".join(str(x) for x in [ort.get("plz", ""), ort.get("ort", "")] if x).strip(),
        "region": region_name,
        "veroeffentlicht": datum,
        "link": link,
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


def am_wortanfang(text, begriffe):
    return [b for b in begriffe
            if re.search(r"(?<![a-zäöüß])" + re.escape(b.lower()), text)]


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
        if am_wortanfang(titel, s["begriffe"]) and s["punkte"] > stufe_punkte:
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

    hinweise = list(stelle.get("zusatz_hinweise", []))
    if stelle["befristet"]:
        hinweise.append("befristet")
    if enthaelt(titel, cfg.get("markierung_seniorität") or []):
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
    titel = re.sub(r"\(.*?\)|\W+", "", stelle["titel"].lower())
    return titel + re.sub(r"\W+", "", stelle["arbeitgeber"].lower())[:5]


def ist_hr_titel_fuer(cfg):
    return lambda titel: bewerten({"titel": titel, "arbeitgeber": "", "befristet": False}, cfg)["punkte"] >= 1


def sammeln(cfg, cache):
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

    ka_stellen, bericht = karriereseiten.ueberwachen(cfg, ARBEITGEBER, cache, ist_hr_titel_fuer(cfg))
    PROTOKOLL["karriereseiten"] = bericht
    # Karriereseiten zuerst, damit bei Doppelten der direkte Link gewinnt
    alle = ka_stellen + list(gefunden.values())

    # Doppelte (gleicher Titel beim gleichen Arbeitgeber) zusammenfassen
    eindeutig = {}
    for stelle in alle:
        k = schluessel(stelle)
        if k not in eindeutig:
            eindeutig[k] = stelle
            continue
        erste = eindeutig[k]
        # Stelle steht auf der Karriereseite und bei der Arbeitsagentur:
        # Datum der Arbeitsagentur übernehmen
        if not erste.get("veroeffentlicht") and stelle.get("veroeffentlicht"):
            erste["veroeffentlicht"] = stelle["veroeffentlicht"]
        if stelle["quelle"] not in erste.get("quellen", [erste["quelle"]]):
            erste["quellen"] = erste.get("quellen", [erste["quelle"]]) + [stelle["quelle"]]
    for stelle in eindeutig.values():
        stelle.setdefault("quellen", [stelle["quelle"]])
    return list(eindeutig.values())


def gedaechtnis_laden():
    if not GEDAECHTNIS.exists():
        return {}, {}
    daten = json.loads(GEDAECHTNIS.read_text(encoding="utf-8"))
    if "version" in daten:
        return daten.get("stellen", {}), daten.get("karriereseiten", {})
    return daten, {}


def mit_gedaechtnis_abgleichen(stellen, neu_tage, alt, cache):
    for s in stellen:
        s["zuerst_gesehen"] = alt.get(s["id"], {}).get("zuerst_gesehen", HEUTE.isoformat())
        s["neu"] = (HEUTE - date.fromisoformat(s["zuerst_gesehen"])).days < neu_tage
    GEDAECHTNIS.parent.mkdir(exist_ok=True)
    GEDAECHTNIS.write_text(json.dumps({
        "version": 2,
        "stellen": {s["id"]: {"zuerst_gesehen": s["zuerst_gesehen"], "titel": s["titel"]} for s in stellen},
        "karriereseiten": cache,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return stellen


def seite_schreiben(stellen, cfg):
    for s in stellen:
        if re.match(r"\d{4}-\d{2}-\d{2}", s.get("veroeffentlicht") or ""):
            s["datum"], s["datum_art"] = s["veroeffentlicht"][:10], "online"
        else:
            s["datum"], s["datum_art"] = s["zuerst_gesehen"], "entdeckt"
    stellen.sort(key=lambda s: s["veroeffentlicht"], reverse=True)
    stellen.sort(key=lambda s: (-s["punkte"], not s["neu"]))
    daten = {
        "stand": datetime.now(timezone(timedelta(hours=2))).strftime("%d.%m.%Y, %H:%M Uhr"),
        "regionen": [r["name"] for r in cfg["regionen"]]
                    + (["Wunscharbeitgeber"] if any(s["region"] == "Wunscharbeitgeber" for s in stellen) else []),
        "stellen": stellen,
        "karriereseiten": PROTOKOLL.get("karriereseiten", []),
    }
    json_text = json.dumps(daten, ensure_ascii=False).replace("</", "<\\/")
    seite = VORLAGE.read_text(encoding="utf-8").replace("__DATEN__", json_text)
    AUSGABE.parent.mkdir(exist_ok=True)
    AUSGABE.write_text(seite, encoding="utf-8")


def main():
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    alt, cache = gedaechtnis_laden()
    roh = sammeln(cfg, cache)

    print(f"\nDiagnose: {PROTOKOLL['erfolgreich']} von {PROTOKOLL['anfragen']} Anfragen erfolgreich, "
          f"Schnittstelle: {_funktionierende_url or 'keine'}")
    for text, anzahl in PROTOKOLL["fehler"].items():
        print(f"  Fehler: {text} ({anzahl}x)")
    if PROTOKOLL["erfolgreich"] == 0 or not roh:
        print("\nABBRUCH: Keine Stellen empfangen. Die Übersichtsseite bleibt unverändert.")
        sys.exit(1)

    stellen = [bewerten(s, cfg) for s in roh]
    stellen = mit_gedaechtnis_abgleichen(stellen, cfg.get("neu_fuer_tage", 3), alt, cache)
    seite_schreiben(stellen, cfg)
    sichtbar = [s for s in stellen if not s["ausgeblendet"]]
    print(f"Fertig: {len(stellen)} Stellen empfangen, {len(sichtbar)} passend, "
          f"davon {sum(s['neu'] for s in sichtbar)} neu, "
          f"{len(stellen) - len(sichtbar)} ausgeblendet.")


if __name__ == "__main__":
    main()
