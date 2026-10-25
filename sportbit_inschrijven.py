#!/usr/bin/env python3
"""
Sportbit: schrijf je automatisch in voor vaste wekelijkse lessen.

Leest lessen.json, kijkt X dagen vooruit, en schrijft je in voor elke les
die past (dag + tijd + naam). Als de les vol is, zet Sportbit je normaal
gesproken op de wachtlijst. Staat de inschrijving nog niet open, dan wordt
dat gelogd en probeert de volgende run het opnieuw.

Instellingen via omgevingsvariabelen (in GitHub: repository secrets):
  SPORTBIT_URL       bv. https://jouwsportschool.sportbitapp.nl
  SPORTBIT_USER      je e-mailadres / gebruikersnaam
  SPORTBIT_PASS      je wachtwoord
Optioneel:
  DRY_RUN=1          alleen tonen wat er zou gebeuren, niets inschrijven
"""

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

DAGEN = {
    "maandag": 0, "dinsdag": 1, "woensdag": 2, "donderdag": 3,
    "vrijdag": 4, "zaterdag": 5, "zondag": 6,
}

BASE_URL = os.environ.get("SPORTBIT_URL", "").rstrip("/")
USER = os.environ.get("SPORTBIT_USER", "")
PASS = os.environ.get("SPORTBIT_PASS", "")
DRY_RUN = os.environ.get("DRY_RUN", "") not in ("", "0", "false")

LOGIN_PAGE = f"{BASE_URL}/web/nl/login"
HEARTBEAT_URL = f"{BASE_URL}/cbm/api/data/heartbeat/"
LOGIN_URL = f"{BASE_URL}/cbm/api/data/inloggen/"
EVENTS_URL = f"{BASE_URL}/cbm/api/data/events/"


def log(msg):
    print(msg, flush=True)


def nieuwe_sessie():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
    })
    return s


def xsrf(s):
    return s.cookies.get("XSRF-TOKEN", "")


def inloggen(s):
    s.get(LOGIN_PAGE, timeout=15)
    s.get(HEARTBEAT_URL, params={"taalIso": "nl"},
          headers={"Origin": BASE_URL, "Referer": LOGIN_PAGE}, timeout=15)
    r = s.post(
        LOGIN_URL,
        json={"username": USER, "password": PASS, "remember": False},
        headers={"X-XSRF-TOKEN": xsrf(s), "Origin": BASE_URL, "Referer": LOGIN_PAGE},
        timeout=15,
    )
    if r.status_code not in (200, 204):
        raise SystemExit(f"Inloggen mislukt (HTTP {r.status_code}): {r.text[:300]}")
    # Na inloggen de sessie verversen zodat het XSRF-token klopt
    s.get(HEARTBEAT_URL, params={"taalIso": "nl"},
          headers={"Origin": BASE_URL, "Referer": f"{BASE_URL}/web/nl/"}, timeout=15)
    log("Ingelogd.")


def zoek_events(data):
    """Het antwoord van Sportbit kan een lijst zijn of genest; zoek de events."""
    if isinstance(data, list):
        if data and all(isinstance(x, dict) for x in data):
            if any("titel" in x or "start" in x for x in data):
                return data
            gevonden = []
            for x in data:
                gevonden += zoek_events(x)
            return gevonden
        return []
    if isinstance(data, dict):
        gevonden = []
        for v in data.values():
            if isinstance(v, (list, dict)):
                gevonden += zoek_events(v)
        return gevonden
    return []


def events_op(s, dag):
    r = s.get(EVENTS_URL, params={"datum": dag.isoformat()},
              headers={"Origin": BASE_URL, "Referer": f"{BASE_URL}/web/nl/"}, timeout=15)
    r.raise_for_status()
    return zoek_events(r.json())


def starttijd(event):
    try:
        st = datetime.fromisoformat(str(event.get("start")).replace("Z", "+00:00"))
    except ValueError:
        return None
    if st.tzinfo:  # altijd vergelijken in Nederlandse tijd
        st = st.astimezone(ZoneInfo("Europe/Amsterdam"))
    return st


def past(event, les, dag):
    st = starttijd(event)
    if not st or st.date() != dag:
        return False
    if st.strftime("%H:%M") != les["tijd"]:
        return False
    return les["les"].lower() in str(event.get("titel", "")).lower()


def inschrijven(s, event_id):
    r = s.post(f"{EVENTS_URL}{event_id}/deelname/", json={},
               headers={"X-XSRF-TOKEN": xsrf(s), "Origin": BASE_URL,
                        "Referer": f"{BASE_URL}/web/nl/"}, timeout=15)
    return r


def main():
    if not (BASE_URL and USER and PASS):
        raise SystemExit("Zet SPORTBIT_URL, SPORTBIT_USER en SPORTBIT_PASS.")

    config = json.loads((Path(__file__).parent / "lessen.json").read_text(encoding="utf-8"))
    vooruit = int(config.get("dagen_vooruit", 8))
    lessen = config["lessen"]

    s = nieuwe_sessie()
    inloggen(s)

    vandaag = datetime.now(ZoneInfo("Europe/Amsterdam")).date()
    fouten = 0
    for i in range(vooruit + 1):
        dag = vandaag + timedelta(days=i)
        vandaag_lessen = [l for l in lessen if DAGEN[l["dag"].lower()] == dag.weekday()]
        if not vandaag_lessen:
            continue
        try:
            events = events_op(s, dag)
        except Exception as e:
            log(f"{dag}: rooster ophalen mislukt: {e}")
            fouten += 1
            continue

        for les in vandaag_lessen:
            label = f"{dag} {les['tijd']} {les['les']}"
            match = [e for e in events if past(e, les, dag)]
            if not match:
                log(f"{label}: niet gevonden in rooster (nog niet gepubliceerd?)")
                continue
            ev = match[0]
            if ev.get("aangemeld"):
                log(f"{label}: al ingeschreven ✔")
                continue
            if ev.get("opWachtlijst"):
                log(f"{label}: staat al op wachtlijst")
                continue
            if DRY_RUN:
                log(f"{label}: zou nu inschrijven (DRY_RUN)")
                continue
            r = inschrijven(s, ev["id"])
            if r.status_code in (200, 204):
                # Controleer de nieuwe status
                nieuw = [e for e in events_op(s, dag) if e.get("id") == ev["id"]]
                status = "?"
                if nieuw:
                    status = ("ingeschreven ✔" if nieuw[0].get("aangemeld")
                              else "op wachtlijst" if nieuw[0].get("opWachtlijst")
                              else "onbekend")
                log(f"{label}: inschrijving verstuurd → {status}")
            else:
                log(f"{label}: nog niet gelukt (HTTP {r.status_code}): {r.text[:200]}")

    sys.exit(1 if fouten else 0)


if __name__ == "__main__":
    main()
