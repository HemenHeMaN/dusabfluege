#!/usr/bin/env python3
"""
DUS-Abflüge Scraper (dus.com Flug-API)
Läuft zwischen 03:00 und 22:00 (Europe/Berlin), Takt über Cron/Scheduler.
Aufruf mit --force ignoriert das Zeitfenster (zum Testen).
Aufruf mit --debug gibt die rohen API-Felder der ersten Abflüge aus
(praktisch, um die Feldnamen für Gate und Check-in zu prüfen).

Pro Abflug werden Gate und Check-in-Schalter direkt von dus.com übernommen.

Ausgabe: departure.json (lokal) und derselbe Inhalt in Firebase (Projekt dus-airport).

Terminal-Zuordnung (Feld "type", Reihenfolge):
  1. Gate    : Gate mit A = "Terminal A", Gate mit B oder C = "Terminal B/C"
  2. Flugtyp : dus.com-Flugtyp 21 (Pauschalreise-Charter) -> "Terminal B/C"
  3. Airline : Airline-Liste -> "Terminal B/C", sonst "Terminal A"
Das Gate vergibt der Flughafen erst später, davor greifen die Fallbacks.
type_source zeigt, woher die Zuordnung stammt ("Gate" = verlässlich).
"""
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import subprocess
import shutil
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- Konfiguration
API_URL = "https://www.dus.com/api/sitecore/flightapi/SearchFlightsWithOutParams"
REFERER = "https://www.dus.com/de-de/fliegen/abflug"
CACHE_FILE = "departure.json"

# Realtime Database im Firebase-Projekt "dus-airport".
# Die genaue URL steht in der Firebase-Konsole oben auf der Seite "Realtime Database".
# Alternativ per Umgebungsvariable FIREBASE_DATABASE_URL setzen (ohne Pfad am Ende).
FIREBASE_DATABASE_URL = os.environ.get(
    "FIREBASE_DATABASE_URL",
    "https://dus-airport-default-rtdb.europe-west1.firebasedatabase.app",
).rstrip("/")
FIREBASE_URL = f"{FIREBASE_DATABASE_URL}/{CACHE_FILE}"

# Daten-Repo (GitHub-Reserve) nur alle X Minuten aktualisieren
DATA_REPO_EVERY_MIN = 10
DATA_REPO_USER = "HemenHeMaN"
DATA_REPO_NAME = "dusflugdaten"

TZ = ZoneInfo("Europe/Berlin")

# Abruf-Zeitfenster (lokale Zeit), am selben Tag.
WINDOW_START = (3, 0)     # frühester Start 03:00
WINDOW_END = (22, 0)      # spätester Start 22:00

# Anzeige-Fenster (nach tatsächlicher/erwarteter Abflugzeit)
MINUTES_PAST = 60
HOURS_FUTURE = 12              # normale Flüge: bis zu 12 Std. im Voraus
HOURS_FUTURE_CANCELLED = 20   # annullierte Flüge: bis zu 20 Std. im Voraus

# Abfrage-Fenster (nach Plan-Zeit), größer als das Anzeige-Fenster,
# damit stark verspätete oder zu früh gestartete Flüge nicht fehlen
FETCH_BACK_HOURS = 4
FETCH_AHEAD_HOURS = HOURS_FUTURE_CANCELLED + 1

PAGE_SIZE = 100
MAX_PAGES = 10

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "de-DE,de;q=0.9",
    "Referer": REFERER,
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"),
}

# Mögliche Feldnamen für Check-in-Schalter in der API (erster Treffer gewinnt)
CHECKIN_KEYS = ("checkIn", "checkin", "checkInCounter", "checkInCounters",
                "checkInDesk", "checkInDesks", "counter", "counters")

# Flughafennamen kommen direkt von dus.com (Name + Land auf Deutsch).
AIRPORT_NAME_OVERRIDES = {
    "HER": "Heraklion (Kreta)", "CHQ": "Chania (Kreta)",
    "SUF": "Lamezia Terme (Kalabrien)", "GWT": "Sylt (Westerland)",
    "BIA": "Bastia (Korsika)", "CTA": "Catania (Sizilien)",
    "OLB": "Olbia (Sardinien)", "CAG": "Cagliari (Sardinien)",
    "TFS": "Teneriffa Süd", "TFN": "Teneriffa Nord", "LPA": "Gran Canaria",
    "XRY": "Jerez de la Frontera", "PVK": "Preveza-Aktion",
    "COV": "Mersin-Çukurova", "ESB": "Ankara-Esenboğa",
    "SAW": "Istanbul-Sabiha Gökçen", "GZP": "Gazipaşa-Alanya",
    "HEL": "Helsinki",
}
COUNTRY_RENAMES = {}

CHARTER_AIRLINES = [
    "condor", "tuifly", "corendon", "freebird", "smartlynx",
    "eurowings discover", "discover airlines", "sunexpress", "sun express",
    "enter air", "tailwind", "sundair", "marabu", "mavi gök", "mavi gok",
]
CHARTER_CODES = {"DE", "X3", "XC", "XR", "FHY", "6Y", "4Y", "XQ", "ENT", "E4",
                 "TWI", "SRD", "MBU"}

SKIP_AIRLINE_KEYWORDS = ("flugschule", "training", "flight school")

# Statustexte (Kleinbuchstaben, Teilstring-Suche)
STATUS_CANCELLED = ("gestrichen", "annull", "cancel")
STATUS_DIVERTED = ("umgeleitet", "divert")
STATUS_DEPARTED = ("abgeflogen", "gestartet", "departed", "took off")
STATUS_BOARDING = ("boarding", "einsteigen", "letzter aufruf", "final call",
                   "gate offen", "gate open")
STATUS_CHECKIN = ("check-in", "checkin", "check in", "abfertigung")
STATUS_KNOWN_SCHEDULED = ("verspätet", "delayed", "planmäßig", "on time", "geplant")


# ---------------------------------------------------------------- Hilfsfunktionen
def in_run_window(now_local):
    start = now_local.replace(hour=WINDOW_START[0], minute=WINDOW_START[1], second=0, microsecond=0)
    end = now_local.replace(hour=WINDOW_END[0], minute=WINDOW_END[1], second=0, microsecond=0)

    # Wenn das End-Fenster vor dem Start liegt, geht es über Mitternacht hinweg
    if end < start:
        if now_local >= start:
            end = end + timedelta(days=1)
        else:
            start = start - timedelta(days=1)

    return start <= now_local <= end


def write_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def parse_dt(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def flat_values(v):
    """Macht aus String, Liste oder Objekt eine flache Liste getrimmter Texte."""
    if v is None:
        return []
    if isinstance(v, str):
        v = v.strip()
        return [v] if v else []
    if isinstance(v, dict):
        for k in ("name", "code", "number", "value", "id"):
            if v.get(k) not in (None, ""):
                return flat_values(v[k])
        return []
    if isinstance(v, (list, tuple)):
        out = []
        for item in v:
            out.extend(flat_values(item))
        return out
    return [str(v)]


def checkin_range(items):
    """Check-in immer als Bereich mit Bindestrich: ['234','239'] -> '234-239',
    ['171','172'] -> '171-172', ['205'] -> '205'."""
    if not items:
        return ""
    if all(i.isdigit() for i in items):
        nums = sorted({int(i) for i in items})
        items = [str(nums[0]), str(nums[-1])] if len(nums) > 1 else [str(nums[0])]
    elif len(items) > 1:
        items = [items[0], items[-1]]
    return "-".join(items)


def gate_text(fl):
    return "/".join(flat_values(fl.get("gate")))


def checkin_text(fl):
    for key in CHECKIN_KEYS:
        items = flat_values(fl.get(key))
        if items:
            return checkin_range(items)
    return ""


def terminal_letter(fl, airline):
    """Terminal-Buchstabe (A, B oder C) aus dem Gate, z. B. 'B24', 'C 03', 'Gate A12'."""
    for g in flat_values(fl.get("gate")):
        m = re.search(r"(?<![A-Za-z])([ABC])\s*-?\s*\d", g, re.IGNORECASE)
        if m:
            return m.group(1).upper()
    fallback = str(airline.get("terminalGateDeparture") or "").strip().upper()
    return fallback if fallback in ("A", "B", "C") else ""


def classify(terminal, fl, airline_name, airline_iata):
    if terminal == "A":
        return "Terminal A", "Gate"
    if terminal in ("B", "C"):
        return "Terminal B/C", "Gate"

    # Gate noch nicht vergeben: Fallbacks
    if (fl.get("flightType") or {}).get("code") == 21:
        return "Terminal B/C", "Flugtyp"
    if str(airline_iata or "").upper() in CHARTER_CODES:
        return "Terminal B/C", "Airline"
    name = str(airline_name or "").lower()
    if any(k in name for k in CHARTER_AIRLINES):
        return "Terminal B/C", "Airline"
    return "Terminal A", "Airline"


def map_status(fl):
    st = fl.get("status") or {}
    text = str((st.get("publicStatus") or {}).get("name") or st.get("description") or "").strip()
    low = text.lower()
    if any(k in low for k in STATUS_CANCELLED):
        return "cancelled", text
    if any(k in low for k in STATUS_DIVERTED):
        return "diverted", text
    if any(k in low for k in STATUS_DEPARTED):
        return "departed", text
    if any(k in low for k in STATUS_BOARDING):
        return "boarding", text
    if any(k in low for k in STATUS_CHECKIN):
        return "checkin", text
    if not text and fl.get("actualTime"):
        return "departed", text
    return "scheduled", text


def airport_info(fl):
    dest = fl.get("destination") or {}
    iata = str(dest.get("iataCode") or "").upper()
    city_obj = dest.get("city") or {}
    name = (AIRPORT_NAME_OVERRIDES.get(iata)
            or str(dest.get("name") or "").strip()
            or str(city_obj.get("name") or "").strip()
            or iata or "Unbekannt")
    country = str((city_obj.get("country") or {}).get("name") or "").strip()
    return name, COUNTRY_RENAMES.get(country, country)


# ---------------------------------------------------------------- Datenabruf
def get_json(url, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
                raise RuntimeError(f"Unerwartete Antwort: {str(data)[:200]}")
            return data["data"]
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                raise
            last = e
        except Exception as e:
            last = e
        if attempt < retries - 1:
            time.sleep(5 * (attempt + 1))
    raise last


def fetch_flights(now):
    start = (now - timedelta(hours=FETCH_BACK_HOURS)).replace(microsecond=0)
    end = (now + timedelta(hours=FETCH_AHEAD_HOURS)).replace(microsecond=0)
    flights, offset = [], 0
    for page in range(MAX_PAGES):
        params = {
            "lang": "de",
            "arrival": "false",
            "offset": offset,
            "codeshare": "true",
            "count": PAGE_SIZE,
            "flightStartTime": start.isoformat(),
            "flightEndTime": end.isoformat(),
            "showDetails": "false",
        }
        try:
            data = get_json(API_URL + "?" + urllib.parse.urlencode(params))
        except Exception as e:
            if page == 0:
                raise
            print(f"Seite {page + 1} fehlgeschlagen ({e}), verwende bisherige Daten.")
            break
        batch = data.get("flights") or []
        flights.extend(batch)
        if not data.get("more") or not batch:
            break
        offset += len(batch)
        time.sleep(random.uniform(1.0, 2.5))
    return flights


# ---------------------------------------------------------------- Hauptlogik
def build_flights(raw, now):
    time_min = now - timedelta(minutes=MINUTES_PAST)
    time_max = now + timedelta(hours=HOURS_FUTURE)
    time_max_cancelled = now + timedelta(hours=HOURS_FUTURE_CANCELLED)

    flights, unknown_status = [], set()
    for fl in raw:
        flight_no = re.sub(r"\s+", "", str(fl.get("flightNumber") or ""))
        if not flight_no:
            continue

        if fl.get("codeshare"):
            continue

        airline = fl.get("airline") or {}
        airline_name = airline.get("name") or airline.get("iataCode") or "Unbekannt"
        airline_iata = airline.get("iataCode") or ""
        if any(k in airline_name.lower() for k in SKIP_AIRLINE_KEYWORDS):
            continue

        sched = parse_dt(fl.get("scheduledTime"))
        if not sched:
            continue
        real = parse_dt(fl.get("actualTime")) or parse_dt(fl.get("estimatedTime")) or sched

        status, status_text = map_status(fl)
        limit = time_max_cancelled if status == "cancelled" else time_max
        if not (time_min <= real <= limit):
            continue

        delay = max(0, int(round((real - sched).total_seconds() / 60)))
        if status == "scheduled" and status_text and \
                not any(k in status_text.lower() for k in STATUS_KNOWN_SCHEDULED):
            unknown_status.add(status_text)

        city, country = airport_info(fl)
        terminal = terminal_letter(fl, airline)
        ftype, type_source = classify(terminal, fl, airline_name, airline_iata)
        flights.append({
            "dt": sched.timestamp(),
            "time_scheduled": sched.strftime("%H:%M"),
            "time_estimated": real.strftime("%H:%M"),
            "flight_no": flight_no,
            "airline": airline_name,
            "city": city,
            "country": country,
            "type": ftype,
            "type_source": type_source,
            "terminal": terminal,
            "gate": gate_text(fl),
            "checkin": checkin_text(fl),
            "status": status,
            "status_text": status_text,
            "delay": delay,
        })

    flights.sort(key=lambda x: x["dt"])

    unique, seen = [], set()
    for f in flights:
        key = (f["flight_no"], f["time_scheduled"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(f)
    return unique, unknown_status


def run_git(data_dir, *args, check=True):
    return subprocess.run(["git", "-C", data_dir, *args],
                          check=check, capture_output=True, text=True)


def push_to_data_repo():
    """Legt CACHE_FILE als Reserve im Daten-Repo ab.
    Holt vorher den aktuellen Stand und pusht ohne Force, damit sich
    arrival.json und departure.json im selben Repo nicht gegenseitig löschen."""
    token = os.environ.get("DUSFLUGDATENKEY")
    if not token:
        print("DUSFLUGDATENKEY fehlt, Daten-Repo wird übersprungen.")
        return

    repo_url = f"https://{token}@github.com/{DATA_REPO_USER}/{DATA_REPO_NAME}.git"
    data_dir = ".git_dus_data"

    try:
        os.makedirs(data_dir, exist_ok=True)
        if not os.path.exists(os.path.join(data_dir, ".git")):
            run_git(data_dir, "init")
            run_git(data_dir, "remote", "add", "origin", repo_url)
        else:
            run_git(data_dir, "remote", "set-url", "origin", repo_url)
        run_git(data_dir, "config", "user.name", "DUS Scraper Bot")
        run_git(data_dir, "config", "user.email", "bot@dus.local")

        for attempt in range(3):
            fetch = run_git(data_dir, "fetch", "--depth", "1", "origin", "main", check=False)
            if fetch.returncode == 0:
                run_git(data_dir, "checkout", "-B", "main", "FETCH_HEAD")
            else:
                run_git(data_dir, "checkout", "-B", "main")

            shutil.copy(CACHE_FILE, os.path.join(data_dir, CACHE_FILE))
            run_git(data_dir, "add", CACHE_FILE)

            if run_git(data_dir, "diff", "--cached", "--quiet", check=False).returncode == 0:
                print("Keine neuen Flugdaten-Änderungen, kein Push nötig.")
                return

            run_git(data_dir, "commit", "-m", f"Auto-update {CACHE_FILE}")
            push = run_git(data_dir, "push", "origin", "main", check=False)
            if push.returncode == 0:
                print(f"{CACHE_FILE} erfolgreich ins Daten-Repo gepusht!")
                return
            # Meist Kollision mit dem Ankunfts-Scraper: kurz warten, neu holen, nochmal
            time.sleep(2 + attempt * 2)
        print("Push ins Daten-Repo nach 3 Versuchen fehlgeschlagen.")
    except Exception as e:
        print(f"Fehler beim Pushen ins Daten-Repo: {str(e).replace(token, '***')}")


def push_to_firebase():
    sa_text = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not sa_text:
        print("FIREBASE_SERVICE_ACCOUNT fehlt, Firebase wird übersprungen.")
        return
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request

        creds = service_account.Credentials.from_service_account_info(
            json.loads(sa_text),
            scopes=[
                "https://www.googleapis.com/auth/firebase.database",
                "https://www.googleapis.com/auth/userinfo.email",
            ],
        )
        creds.refresh(Request())

        with open(CACHE_FILE, "rb") as f:
            body = f.read()
        req = urllib.request.Request(
            FIREBASE_URL + "?access_token=" + creds.token,
            data=body,
            method="PUT",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            print(f"Firebase aktualisiert (HTTP {resp.status}).")
    except Exception as e:
        print(f"Fehler beim Schreiben in Firebase: {e}")


def main():
    now = datetime.now(TZ)

    if "--force" not in sys.argv and not in_run_window(now):
        print(f"{now:%H:%M} liegt außerhalb des erlaubten Zeitfensters "
              f"({WINDOW_START[0]:02d}:{WINDOW_START[1]:02d}–{WINDOW_END[0]:02d}:{WINDOW_END[1]:02d}), kein Abruf.")
        return

    try:
        raw = fetch_flights(now)
    except Exception as e:
        print(f"Fehler beim Abruf: {e}")
        print(f"{CACHE_FILE} bleibt unverändert.")
        sys.exit(1)
    if not raw:
        print(f"API lieferte keine Flüge, {CACHE_FILE} bleibt unverändert.")
        sys.exit(1)

    if "--debug" in sys.argv:
        for fl in raw[:2]:
            print(json.dumps(fl, ensure_ascii=False, indent=2))

    unique, unknown_status = build_flights(raw, now)

    with_gate = sum(1 for f in unique if f["gate"])
    with_checkin = sum(1 for f in unique if f["checkin"])
    print(f"Rohdaten: {len(raw)} Einträge, im Fenster: {len(unique)} "
          f"(mit Gate: {with_gate}, mit Check-in: {with_checkin})")
    if unknown_status:
        print("Unbekannte Statustexte (als 'scheduled' behandelt):", sorted(unknown_status))

    write_json_atomic(CACHE_FILE, {
        "updated_at": now.strftime("%d.%m.%Y %H:%M"),
        "source": "Dus.com",
        "flights": unique,
    })
    print(f"{CACHE_FILE} aktualisiert: {len(unique)} Flüge.")

    # Zuerst Firebase (schnell, ohne Cache), dann das Daten-Repo als Reserve
    push_to_firebase()

    # Reserve im Daten-Repo nur alle 10 Minuten (erste 3 Minuten jedes 10er-Blocks)
    if "--force" in sys.argv or now.minute % DATA_REPO_EVERY_MIN < 3:
        push_to_data_repo()
    else:
        print("Daten-Repo: diesmal übersprungen (nur alle 10 Minuten).")


if __name__ == "__main__":
    main()
