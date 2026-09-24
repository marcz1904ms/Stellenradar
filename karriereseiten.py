"""Überwacht die Karriereseiten der Arbeitgeber aus arbeitgeber.csv.

Ablauf pro Arbeitgeber
  1. Karriereseite aufrufen und erkennen, welches Bewerbermanagementsystem
     dahinter steckt (Personio, Workday, Greenhouse usw.). Das Ergebnis wird
     eine Woche lang gemerkt, damit die täglichen Läufe schnell bleiben.
  2. Bei bekanntem System: Stellen über dessen öffentliche Stellenliste holen.
  3. Sonst: Links auf der Karriereseite nach HR-Stellentiteln durchsuchen.
     Das klappt nur bei Seiten, die ihre Stellen nicht erst per JavaScript laden.
"""

import csv
import re
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

KOPF = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}
ZEITLIMIT = 20
ERKENNUNG_GUELTIG_TAGE = 7

SYSTEME = [
    ("personio", re.compile(r"([a-z0-9-]+)\.jobs\.personio\.(?:de|com)", re.I)),
    ("recruitee", re.compile(r"([a-z0-9-]+)\.recruitee\.com", re.I)),
    ("greenhouse", re.compile(r"(eu\.)?greenhouse\.io/(?:v1/boards/|embed/job_board(?:/js)?\?for=)?([A-Za-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.(eu\.)?lever\.co/([A-Za-z0-9_.-]+)", re.I)),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", re.I)),
    ("workday", re.compile(r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)")),
    ("join", re.compile(r"join\.com/companies/([A-Za-z0-9_-]+)", re.I)),
    ("softgarden", re.compile(r"([a-z0-9-]+)\.softgarden\.io", re.I)),
]
UNGUELTIGE_KENNUNG = {"www", "api", "static", "cdn", "assets", "app", "embed", "js", "boards",
                      "careers", "jobs", "widget", "de", "en", "static-careers",
                      "tracking", "files", "media", "career", "cdn-cgi"}
KARRIERE_LINK = re.compile(r"karriere|career|jobs?\b|stellen|vacanc|join-us|arbeiten-bei|jobboerse", re.I)
# Merkmale eines echten Stellentitels
GENDER = re.compile(r"[(\[]?\s*(?:[mwfdx]\s*[/|,]\s*){2}[mwfdx]\s*[)\]]?|all genders|alle geschlechter|\(gn\)|\(d\)|\*in\b|:in\b|/-?in\b", re.I)
ROLLE = re.compile(r"\b(referent\w*|manager\w*|specialist|spezialist\w*|partner\w*|berater\w*|koordinator\w*|coordinator|"
                   r"generalist|consultant|sachbearbeiter\w*|mitarbeiter\w*|associate|advisor|expert\w*|officer|"
                   r"entwickler\w*|developer|recruiter|trainer\w*|coach|analyst|administrator\w*|assistent\w*|"
                   r"werkstudent\w*|praktikant\w*|trainee|lead|leiter\w*|business partner)\b", re.I)
STELLEN_URL = re.compile(r"(job|stelle|vacanc|position|offer|posting|karriere/.+/.+|\d{4,})", re.I)


def sieht_aus_wie_stelle(titel, url=""):
    """Unterscheidet echte Ausschreibungen von Menüpunkten wie „Fort- und Weiterbildung“."""
    if GENDER.search(titel):
        return True
    return bool(ROLLE.search(titel) and STELLEN_URL.search(url or ""))


NAVIGATION = re.compile(r"^(alle|all|mehr|more|zur|zu den|jetzt|hier|karriere|careers?|jobs?|stellen\w*|"
                        r"bewerb\w*|apply|login|anmelden|initiativ\w*|job alert|jobs? finden)\b", re.I)


# ----------------------------------------------------------------------------
# Hilfsfunktionen
# ----------------------------------------------------------------------------

class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self._href, self._text = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            text = re.sub(r"\s+", " ", " ".join(self._text)).strip()
            self.links.append((self._href, text))
            self._href = None


def links_von(html_text, basis):
    p = _Links()
    try:
        p.feed(html_text)
    except Exception:
        pass
    return [(urljoin(basis, h), t) for h, t in p.links if h and not h.startswith(("mailto:", "tel:", "#"))]


def holen(url):
    try:
        r = requests.get(url, headers=KOPF, timeout=ZEITLIMIT, allow_redirects=True)
        if r.status_code == 200:
            return r.text, r.url
    except requests.RequestException:
        pass
    return None, url


def system_erkennen(text):
    for name, muster in SYSTEME:
        for treffer in muster.finditer(text):
            if name == "workday":
                return {"system": name, "kennung": treffer.group(1), "wd": treffer.group(2),
                        "seite": treffer.group(3)}
            if name == "greenhouse":
                if treffer.group(2).lower() in UNGUELTIGE_KENNUNG:
                    continue
                return {"system": name, "kennung": treffer.group(2), "eu": bool(treffer.group(1))}
            if name == "lever":
                return {"system": name, "kennung": treffer.group(2), "eu": bool(treffer.group(1))}
            kennung = treffer.group(1)
            if kennung.lower() not in UNGUELTIGE_KENNUNG:
                return {"system": name, "kennung": kennung}
    return None


def erkennen(url):
    """Findet das System hinter einer Karriereseite. Folgt bis zu drei Karriere-Links."""
    text, endurl = holen(url)
    if text is None:
        return {"system": "nicht erreichbar", "startseite": url}
    treffer = system_erkennen(endurl + " " + text)
    if treffer:
        return {**treffer, "startseite": endurl}

    domain = urlparse(endurl).netloc.split(".")[-2:]
    kandidaten = []
    for href, linktext in links_von(text, endurl):
        ziel = urlparse(href).netloc.split(".")[-2:]
        if (KARRIERE_LINK.search(href) or KARRIERE_LINK.search(linktext)) and href not in kandidaten:
            if ziel == domain or system_erkennen(href):
                kandidaten.append(href)
    karriereseite = endurl if KARRIERE_LINK.search(endurl) else (kandidaten[0] if kandidaten else endurl)

    for href in kandidaten[:3]:
        treffer = system_erkennen(href)
        if treffer:
            return {**treffer, "startseite": href}
        unter, unterurl = holen(href)
        if unter:
            treffer = system_erkennen(unterurl + " " + unter)
            if treffer:
                return {**treffer, "startseite": unterurl}
    return {"system": "eigene Seite", "startseite": karriereseite}


# ----------------------------------------------------------------------------
# Stellen je System
# ----------------------------------------------------------------------------

def _json(url, **kw):
    r = requests.get(url, headers=KOPF, timeout=ZEITLIMIT, **kw)
    r.raise_for_status()
    return r.json()


def stellen_personio(e):
    for tld in ("de", "com"):
        try:
            r = requests.get(f"https://{e['kennung']}.jobs.personio.{tld}/xml", headers=KOPF, timeout=ZEITLIMIT)
            if r.status_code != 200:
                continue
            wurzel = ET.fromstring(r.content)
        except Exception:
            continue
        return [{"titel": p.findtext("name") or "", "ort": p.findtext("office") or "",
                 "link": f"https://{e['kennung']}.jobs.personio.{tld}/job/{p.findtext('id')}",
                 "datum": (p.findtext("createdAt") or "")[:10],
                 "befristet": (p.findtext("employmentType") or "").lower() == "temporary"}
                for p in wurzel.findall("position")]
    raise RuntimeError("Personio-Liste nicht abrufbar")


def stellen_recruitee(e):
    d = _json(f"https://{e['kennung']}.recruitee.com/api/offers/")
    return [{"titel": o.get("title", ""), "ort": o.get("location") or o.get("city") or "",
             "link": o.get("careers_url") or "", "datum": (o.get("published_at") or o.get("created_at") or "")[:10]}
            for o in d.get("offers", [])]


def stellen_greenhouse(e):
    host = "boards-api.eu.greenhouse.io" if e.get("eu") else "boards-api.greenhouse.io"
    d = _json(f"https://{host}/v1/boards/{e['kennung']}/jobs")
    return [{"titel": j.get("title", ""), "ort": (j.get("location") or {}).get("name", ""),
             "link": j.get("absolute_url", ""), "datum": (j.get("updated_at") or "")[:10]}
            for j in d.get("jobs", [])]


def stellen_lever(e):
    host = "api.eu.lever.co" if e.get("eu") else "api.lever.co"
    d = _json(f"https://{host}/v0/postings/{e['kennung']}?mode=json")
    aus = []
    for j in d if isinstance(d, list) else []:
        ms = j.get("createdAt")
        datum = datetime.fromtimestamp(ms / 1000, timezone.utc).date().isoformat() if ms else ""
        aus.append({"titel": j.get("text", ""), "ort": (j.get("categories") or {}).get("location", ""),
                    "link": j.get("hostedUrl", ""), "datum": datum})
    return aus


def stellen_smartrecruiters(e):
    d = _json(f"https://api.smartrecruiters.com/v1/companies/{e['kennung']}/postings?limit=100")
    return [{"titel": j.get("name", ""),
             "ort": ", ".join(x for x in [(j.get("location") or {}).get("city", ""),
                                          (j.get("location") or {}).get("country", "")] if x),
             "link": f"https://jobs.smartrecruiters.com/{e['kennung']}/{j.get('id')}",
             "datum": (j.get("releasedDate") or "")[:10]}
            for j in d.get("content", [])]


def stellen_ashby(e):
    d = _json(f"https://api.ashbyhq.com/posting-api/job-board/{e['kennung']}")
    return [{"titel": j.get("title", ""), "ort": j.get("location", ""),
             "link": j.get("jobUrl", ""), "datum": (j.get("publishedAt") or "")[:10]}
            for j in d.get("jobs", [])]


def stellen_workday(e):
    basis = f"https://{e['kennung']}.{e['wd']}.myworkdayjobs.com"
    api = f"{basis}/wday/cxs/{e['kennung']}/{e['seite']}/jobs"
    gesehen, aus = set(), []
    for suchwort in ("HR", "Personal", "People", "Learning", "Talent", "Human Resources"):
        r = requests.post(api, json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": suchwort},
                          headers={**KOPF, "Content-Type": "application/json", "Accept": "application/json"},
                          timeout=ZEITLIMIT)
        r.raise_for_status()
        for j in r.json().get("jobPostings", []):
            pfad = j.get("externalPath", "")
            if pfad in gesehen:
                continue
            gesehen.add(pfad)
            aus.append({"titel": j.get("title", ""), "ort": j.get("locationsText", ""),
                        "link": f"{basis}/{e['seite']}{pfad}", "datum": ""})
    return aus


def stellen_linksuche(url, ist_hr_titel):
    text, endurl = holen(url)
    if text is None:
        raise RuntimeError("Seite nicht erreichbar")
    aus, gesehen = [], set()
    for href, linktext in links_von(text, endurl):
        if not (8 <= len(linktext) <= 140) or NAVIGATION.match(linktext):
            continue
        if ist_hr_titel(linktext) and sieht_aus_wie_stelle(linktext, href) and href not in gesehen:
            gesehen.add(href)
            aus.append({"titel": linktext, "ort": "", "link": href, "datum": ""})
    return aus


ABRUF = {
    "personio": stellen_personio, "recruitee": stellen_recruitee, "greenhouse": stellen_greenhouse,
    "lever": stellen_lever, "smartrecruiters": stellen_smartrecruiters, "ashby": stellen_ashby,
    "workday": stellen_workday,
}
LINKSUCHE_URL = {
    "join": lambda e: f"https://join.com/companies/{e['kennung']}",
    "softgarden": lambda e: f"https://{e['kennung']}.softgarden.io/de/vacancies",
}


# ----------------------------------------------------------------------------
# Gesamtablauf
# ----------------------------------------------------------------------------

def arbeitgeber_lesen(pfad):
    if not pfad.exists():
        return []
    with open(pfad, encoding="utf-8-sig", newline="") as f:
        probe = f.read(2048)
        f.seek(0)
        trenner = ";" if probe.count(";") > probe.count(",") else ","
        return [z for z in csv.DictReader(f, delimiter=trenner) if (z.get("karriere_url") or "").strip()]


def region_zuordnen(ort_text, zeile, cfg):
    """Gibt (Region, passt) zurück. passt=None heißt: Ort unklar."""
    text = (ort_text or "").lower()
    if text:
        for r in cfg["regionen"]:
            for o in [r["ort"]] + r.get("orte", []):
                if o.lower() in text:
                    return r["name"], True
    csv_region = (zeile.get("region") or "").lower()
    standard = next((r["name"] for r in cfg["regionen"] if r["ort"].lower()[:4] in csv_region), "Wunscharbeitgeber")
    unklar = (not text) or re.search(r"deutschland|germany|remote|homeoffice|home office|mobil|hybrid|locations|standorte|bundesweit|multiple", text)
    return standard, (None if unklar else False)


def ueberwachen(cfg, pfad_csv, cache, ist_hr_titel):
    zeilen = arbeitgeber_lesen(pfad_csv)
    if not zeilen:
        return [], []
    print(f"Prüfe {len(zeilen)} Karriereseiten …")
    heute = date.today()

    def bearbeiten(zeile):
        name, url = zeile["unternehmen"].strip(), zeile["karriere_url"].strip()
        info = cache.get(name)
        alt = info and info.get("url") == url and (heute - date.fromisoformat(info["geprueft"])).days < ERKENNUNG_GUELTIG_TAGE \
            and info.get("system") != "nicht erreichbar"
        if not alt:
            info = {**erkennen(url), "url": url, "geprueft": heute.isoformat()}
        try:
            if info["system"] in ABRUF:
                roh = ABRUF[info["system"]](info)
            elif info["system"] in LINKSUCHE_URL:
                roh = stellen_linksuche(LINKSUCHE_URL[info["system"]](info), ist_hr_titel)
            elif info["system"] == "eigene Seite":
                roh = stellen_linksuche(info["startseite"], ist_hr_titel)
            else:
                roh = []
            fehler = ""
        except Exception as ex:
            roh, fehler = [], type(ex).__name__
        return zeile, info, roh, fehler

    with ThreadPoolExecutor(max_workers=8) as pool:
        ergebnisse = list(pool.map(bearbeiten, zeilen))

    stellen, bericht = [], []
    for zeile, info, roh, fehler in ergebnisse:
        name = zeile["unternehmen"].strip()
        cache[name] = {k: v for k, v in info.items()}
        status = (zeile.get("status") or "").strip()
        treffer = 0
        for j in roh:
            titel = re.sub(r"\s+", " ", j.get("titel") or "").strip()
            if not titel or not ist_hr_titel(titel):
                continue
            region, passt = region_zuordnen(j.get("ort"), zeile, cfg)
            if passt is False:
                continue
            hinweise = []
            if passt is None:
                hinweise.append("Ort prüfen")
            if status:
                hinweise.append("Arbeitgeber: " + status)
            treffer += 1
            stellen.append({
                "id": f"ka:{name}:{j.get('link') or titel}",
                "titel": titel, "arbeitgeber": name, "ort": j.get("ort") or "",
                "region": region, "veroeffentlicht": j.get("datum") or "",
                "link": j.get("link") or info.get("startseite") or zeile["karriere_url"],
                "befristet": bool(j.get("befristet")), "quelle": "Karriereseite",
                "zusatz_hinweise": hinweise,
            })
        if info["system"] == "nicht erreichbar":
            abdeckung = "Seite nicht erreichbar"
        elif fehler:
            abdeckung = f"Fehler beim Abruf ({fehler})"
        elif info["system"] in ABRUF:
            abdeckung = "vollständig"
        else:
            abdeckung = "Linksuche (Seiten mit JavaScript evtl. unvollständig)"
        bericht.append({"unternehmen": name, "region": zeile.get("region", ""), "typ": zeile.get("typ", ""),
                        "system": info["system"], "abdeckung": abdeckung, "treffer": treffer,
                        "seite": info.get("startseite") or zeile["karriere_url"]})
    erkannt = sum(1 for b in bericht if b["abdeckung"] == "vollständig")
    print(f"  {erkannt} von {len(bericht)} Arbeitgebern mit erkanntem System, "
          f"{len(stellen)} HR-Stellen auf Karriereseiten gefunden")
    return stellen, bericht
