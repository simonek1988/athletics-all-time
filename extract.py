#!/usr/bin/env python3
"""
World Athletics All-Time Toplists Extractor
============================================
Personal/research use only.
Data © World Athletics (worldathletics.org). Not for redistribution.

Fetches the all-time best performance lists (one result per athlete) for every
event × gender combination and saves:

    event_data/{event-slug}/{gender}/results.csv
    event_data/{event-slug}/{gender}/metadata.json

Quick start:
    pip install requests beautifulsoup4 lxml
    python extract.py                            # all events, both genders
    python extract.py --events "100 Metres" "Long Jump"
    python extract.py --gender men
    python extract.py --max-pages 2              # limit pages (for testing)
    python extract.py --resume                   # skip already-extracted events

To inspect a result file nicely:
    python -c "import pandas as pd; print(pd.read_csv('event_data/100-metres/men/results.csv').to_string())"
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import time
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Configuration ──────────────────────────────────────────────────────────────

BASE_URL = "https://worldathletics.org/records/all-time-toplists"
TODAY = date.today().isoformat()
SAVE_ROOT = Path("event_data")
REQUEST_DELAY = 2.0      # polite pause between HTTP requests (seconds)
MAX_RETRIES = 3

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ── Cell label mapping ─────────────────────────────────────────────────────────
# Maps the data-th attribute value found in <td> cells to clean field names.

CELL_LABEL_MAP: dict[str, str] = {
    "Rank":         "rank",
    "Mark":         "mark",
    "WIND":         "wind",
    "Competitor":   "athlete",
    "DOB":          "dob",
    "Nat":          "country",
    "Pos":          "pos",
    "Venue":        "venue",
    "Date":         "date",
    "ResultScore":  "result_score",
    "Results Score":"result_score",
}

# Preferred column order in the output CSV.
# athlete_url is automatically inserted after athlete.
# Only columns that appear in the actual data are written.
COLUMN_ORDER = [
    "rank", "mark", "wind",
    "athlete", "athlete_url",
    "dob", "country", "pos",
    "venue", "date", "result_score",
]

# ── Event Catalogue ────────────────────────────────────────────────────────────
# discipline_slug  →  list of event names belonging to that discipline.
# The discipline slug is the path segment used in the World Athletics URL.

EVENT_DISCIPLINE: dict[str, list[str]] = {
    "sprints": [
        "50 Metres", "55 Metres", "60 Metres", "100 Metres",
        "200 Metres", "200 Metres Short Track",
        "300 Metres", "300 Metres Short Track",
        "400 Metres", "400 Metres Short Track",
        "500 Metres Short Track", "600 Metres", "600 Metres Short Track",
    ],
    "middle-long": [
        "800 Metres", "800 Metres Short Track",
        "1000 Metres", "1000 Metres Short Track",
        "1500 Metres", "1500 Metres Short Track",
        "Mile", "Mile Short Track",
        "2000 Metres", "2000 Metres Short Track",
        "3000 Metres", "3000 Metres Short Track",
        "2 Miles", "2 Miles Short Track",
        "5000 Metres", "5000 Metres Short Track",
        "10,000 Metres",
    ],
    "hurdles": [
        "50 Metres Hurdles", "55 Metres Hurdles", "60 Metres Hurdles",
        "100 Metres Hurdles", "110 Metres Hurdles", "400 Metres Hurdles",
    ],
    "steeplechase": [
        "2000 Metres Steeplechase", "3000 Metres Steeplechase",
    ],
    "jumps": [
        "High Jump", "Pole Vault", "Long Jump", "Triple Jump",
    ],
    "throws": [
        "Shot Put", "Discus Throw", "Hammer Throw", "Javelin Throw",
    ],
    "combined-events": [
        "Decathlon", "Heptathlon",
        "Heptathlon Short Track", "Pentathlon Short Track",
    ],
    "road-running": [
        "Mile Road", "5 Kilometres Road", "10 Kilometres Road",
        "15 Kilometres Road", "10 Miles Road", "20 Kilometres Road",
        "Half Marathon", "Marathon",
    ],
    "race-walk": [
        "3000 Metres Race Walk", "3000 Metres Race Walk Short Track",
        "5000 Metres Race Walk", "5 Kilometres Race Walk",
        "5000 Metres Race Walk Short Track",
        "10,000 Metres Race Walk", "10,000 Metres Race Walk Short Track",
        "10 Kilometres Race Walk",
        "20,000 Metres Race Walk", "20 Kilometres Race Walk",
        "Half Marathon Race Walk",
        "30 Kilometres Race Walk", "35 Kilometres Race Walk",
        "Marathon Race Walk", "50 Kilometres Race Walk",
    ],
    "relays": [
        "4x100 Metres Relay", "4x200 Metres Relay",
        "4x200 Metres Relay Short Track",
        "4x400 Metres Relay", "4x400 Metres Relay Short Track",
        "4x800 Metres Relay", "4x800 Metres Relay Short Track",
        "4x1500 Metres Relay",
    ],
}

# World Athletics event IDs, harvested from the <select id="eventId"> dropdowns
# in the saved reference HTML pages (one for men, one for women).
EVENT_IDS: dict[str, dict[str, int]] = {
    "men": {
        "50 Metres": 10230285,          "55 Metres": 10230287,
        "60 Metres": 10229683,          "100 Metres": 10229630,
        "200 Metres": 10229605,         "200 Metres Short Track": 10229552,
        "300 Metres": 10229500,         "300 Metres Short Track": 10229553,
        "400 Metres": 10229631,         "400 Metres Short Track": 10229554,
        "500 Metres Short Track": 10229555,
        "600 Metres": 10229604,         "600 Metres Short Track": 10229600,
        "800 Metres": 10229501,         "800 Metres Short Track": 10229556,
        "1000 Metres": 10229606,        "1000 Metres Short Track": 10229557,
        "1500 Metres": 10229502,        "1500 Metres Short Track": 10229558,
        "Mile": 10229503,               "Mile Short Track": 10229559,
        "2000 Metres": 10229632,        "2000 Metres Short Track": 10229561,
        "3000 Metres": 10229607,        "3000 Metres Short Track": 10229560,
        "2 Miles": 10229608,            "2 Miles Short Track": 10229599,
        "5000 Metres": 10229609,        "5000 Metres Short Track": 10229562,
        "10,000 Metres": 10229610,
        "50 Metres Hurdles": 10230274,  "55 Metres Hurdles": 10230289,
        "60 Metres Hurdles": 10230176,  "110 Metres Hurdles": 10229611,
        "400 Metres Hurdles": 10229612,
        "2000 Metres Steeplechase": 10229613,
        "3000 Metres Steeplechase": 10229614,
        "High Jump": 10229615,          "Pole Vault": 10229616,
        "Long Jump": 10229617,          "Triple Jump": 10229618,
        "Shot Put": 10229619,           "Discus Throw": 10229620,
        "Hammer Throw": 10229621,       "Javelin Throw": 10229636,
        "Mile Road": 10229752,          "5 Kilometres Road": 204597,
        "10 Kilometres Road": 10229507, "15 Kilometres Road": 10229504,
        "10 Miles Road": 10229505,      "20 Kilometres Road": 10229506,
        "Half Marathon": 10229633,      "Marathon": 10229634,
        "3000 Metres Race Walk": 10229776,
        "3000 Metres Race Walk Short Track": 10229786,
        "5000 Metres Race Walk": 10229644,
        "5 Kilometres Race Walk": 10229624,
        "5000 Metres Race Walk Short Track": 10229669,
        "10,000 Metres Race Walk": 10229637,
        "10,000 Metres Race Walk Short Track": 10229788,
        "10 Kilometres Race Walk": 10229625,
        "20,000 Metres Race Walk": 10229638,
        "20 Kilometres Race Walk": 10229508,
        "Half Marathon Race Walk": 10230472,
        "30 Kilometres Race Walk": 10229626,
        "35 Kilometres Race Walk": 10229627,
        "Marathon Race Walk": 10230474,
        "50 Kilometres Race Walk": 10229628,
        "Decathlon": 10229629,
        "Heptathlon Short Track": 10229571,
        "4x100 Metres Relay": 204593,   "4x200 Metres Relay": 204601,
        "4x200 Metres Relay Short Track": 204602,
        "4x400 Metres Relay": 204595,   "4x400 Metres Relay Short Track": 204609,
        "4x800 Metres Relay": 204605,   "4x800 Metres Relay Short Track": 10229643,
        "4x1500 Metres Relay": 204606,
    },
    "women": {
        "50 Metres": 10230286,          "55 Metres": 10230288,
        "60 Metres": 10229684,          "100 Metres": 10229509,
        "200 Metres": 10229510,         "200 Metres Short Track": 10229575,
        "300 Metres": 10229515,         "300 Metres Short Track": 10229576,
        "400 Metres": 10229511,         "400 Metres Short Track": 10229577,
        "500 Metres Short Track": 10229578,
        "600 Metres": 10229602,         "600 Metres Short Track": 10229601,
        "800 Metres": 10229512,         "800 Metres Short Track": 10229579,
        "1000 Metres": 10229516,        "1000 Metres Short Track": 10229580,
        "1500 Metres": 10229513,        "1500 Metres Short Track": 10229581,
        "Mile": 10229517,               "Mile Short Track": 10229582,
        "2000 Metres": 10229518,        "2000 Metres Short Track": 10229583,
        "3000 Metres": 10229519,        "3000 Metres Short Track": 10229584,
        "2 Miles": 10229520,            "2 Miles Short Track": 10229585,
        "5000 Metres": 10229514,        "5000 Metres Short Track": 10229586,
        "10,000 Metres": 10229521,
        "50 Metres Hurdles": 10230275,  "55 Metres Hurdles": 10230290,
        "60 Metres Hurdles": 10230177,  "100 Metres Hurdles": 10229522,
        "400 Metres Hurdles": 10229523,
        "2000 Metres Steeplechase": 10229525,
        "3000 Metres Steeplechase": 10229524,
        "High Jump": 10229526,          "Pole Vault": 10229527,
        "Long Jump": 10229528,          "Triple Jump": 10229529,
        "Shot Put": 10229530,           "Discus Throw": 10229531,
        "Hammer Throw": 10229532,       "Javelin Throw": 10229533,
        "Mile Road": 10229753,          "5 Kilometres Road": 204598,
        "10 Kilometres Road": 10229537, "15 Kilometres Road": 10229538,
        "10 Miles Road": 10229539,      "20 Kilometres Road": 10229540,
        "Half Marathon": 10229541,      "Marathon": 10229534,
        "3000 Metres Race Walk": 10229659,
        "3000 Metres Race Walk Short Track": 10229682,
        "5000 Metres Race Walk": 10229641,
        "5 Kilometres Race Walk": 10229546,
        "5000 Metres Race Walk Short Track": 10229787,
        "10,000 Metres Race Walk": 10229639,
        "10 Kilometres Race Walk": 10229547,
        "20,000 Metres Race Walk": 10229640,
        "20 Kilometres Race Walk": 10229535,
        "Half Marathon Race Walk": 10230473,
        "35 Kilometres Race Walk": 10229989,
        "Marathon Race Walk": 10230475,
        "50 Kilometres Race Walk": 10229603,
        "Heptathlon": 10229536,
        "Pentathlon Short Track": 10229595,
        "4x100 Metres Relay": 204594,   "4x200 Metres Relay": 204603,
        "4x200 Metres Relay Short Track": 204604,
        "4x400 Metres Relay": 204596,   "4x400 Metres Relay Short Track": 204610,
        "4x800 Metres Relay": 204607,   "4x800 Metres Relay Short Track": 10229642,
        "4x1500 Metres Relay": 204608,
    },
}

# Reverse lookup: event name → discipline slug
_EVENT_TO_DISCIPLINE: dict[str, str] = {
    evt: disc
    for disc, evts in EVENT_DISCIPLINE.items()
    for evt in evts
}

# ── Helper utilities ───────────────────────────────────────────────────────────

def to_slug(name: str) -> str:
    """Convert an event name to a lowercase, hyphen-separated folder/URL slug."""
    s = name.lower().replace(",", "")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def get_discipline(event_name: str) -> str:
    disc = _EVENT_TO_DISCIPLINE.get(event_name)
    if disc is None:
        raise ValueError(f"No discipline mapping for event: {event_name!r}")
    return disc


def build_url(discipline: str, event_slug: str, gender: str, page: int,
              event_id: int, age_category: str = "senior") -> str:
    path = f"{BASE_URL}/{discipline}/{event_slug}/all/{gender}/{age_category}"
    qs = (
        f"regionType=world"
        f"&timing=electronic"
        f"&windReading=regular"
        f"&page={page}"
        f"&bestResultsOnly=true"
        f"&firstDay=1900-01-01"
        f"&lastDay={TODAY}"
        f"&maxResultsByCountry=all"
        f"&eventId={event_id}"
        f"&ageCategory={age_category}"
    )
    return f"{path}?{qs}"


def age_dir(gender: str, age_category: str) -> str:
    """Return the subfolder name for a gender + age-category combination."""
    if age_category == "senior":
        return gender
    return f"{gender}-{age_category}"

# ── HTTP fetching ──────────────────────────────────────────────────────────────

def fetch_page(session: requests.Session, url: str) -> BeautifulSoup | None:
    """Fetch a URL and return a parsed BeautifulSoup, with retry on failure."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "lxml")
        except requests.RequestException as exc:
            wait = 2 ** attempt
            logging.warning(
                "Attempt %d/%d failed (%s). Waiting %ds…", attempt, MAX_RETRIES, exc, wait
            )
            if attempt < MAX_RETRIES:
                time.sleep(wait)
    logging.error("All retries exhausted for: %s", url)
    return None


def get_last_page(soup: BeautifulSoup) -> int:
    """
    Determine total page count from the pagination block.
    Uses the highest data-page value found in any pagination link.
    Returns 1 when there is no pagination (single-page result).
    """
    page_nums = [
        int(a["data-page"])
        for a in soup.select("a[data-page]")
        if a.get("data-page", "").isdigit()
    ]
    return max(page_nums) if page_nums else 1

# ── Row parsing ────────────────────────────────────────────────────────────────

def parse_rows(soup: BeautifulSoup) -> list[dict[str, str]]:
    """
    Extract all result rows from the records table.
    Uses data-th cell attributes for field identification, so column presence
    and order are determined by what the page actually provides.
    """
    rows: list[dict[str, str]] = []
    for tr in soup.select("table.records-table tbody tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        record: dict[str, str] = {}
        for cell in cells:
            label = cell.get("data-th", "").strip()
            if not label:           # skip the blank spacer column
                continue
            field = CELL_LABEL_MAP.get(label, label.lower())
            if field == "athlete":
                a_tag = cell.find("a")
                if a_tag:
                    record["athlete"] = a_tag.get_text(strip=True)
                    record["athlete_url"] = a_tag.get("href", "").strip()
                else:
                    record["athlete"] = cell.get_text(strip=True)
                    record["athlete_url"] = ""
            elif field == "country":
                img = cell.find("img")
                record["country"] = img["alt"].strip() if img else cell.get_text(strip=True)
            else:
                record[field] = cell.get_text(strip=True)
        if record:
            rows.append(record)
    return rows


def ordered_columns(rows: list[dict[str, str]]) -> list[str]:
    """
    Return the columns that appear in the data, sorted by COLUMN_ORDER.
    Any unknown columns are appended at the end.
    """
    found: set[str] = set()
    for row in rows:
        found.update(row.keys())
    ordered = [c for c in COLUMN_ORDER if c in found]
    extras = sorted(found - set(ordered))
    return ordered + extras

# ── Event extraction ───────────────────────────────────────────────────────────

def extract_event(
    session: requests.Session,
    event_name: str,
    gender: str,
    event_id: int,
    discipline: str,
    max_pages: int | None,
    age_category: str = "senior",
) -> tuple[list[dict[str, str]], dict]:
    """
    Fetch all pages for one event/gender/age-category and return (rows, metadata).
    """
    event_slug = to_slug(event_name)
    url_p1 = build_url(discipline, event_slug, gender, 1, event_id, age_category)

    logging.info("    page 1 → %s", url_p1)
    soup = fetch_page(session, url_p1)
    if soup is None:
        return [], {}

    last_page = get_last_page(soup)
    if max_pages:
        last_page = min(last_page, max_pages)

    all_rows = parse_rows(soup)
    time.sleep(REQUEST_DELAY)

    for page in range(2, last_page + 1):
        url = build_url(discipline, event_slug, gender, page, event_id, age_category)
        logging.info("    page %d/%d", page, last_page)
        page_soup = fetch_page(session, url)
        if page_soup is None:
            logging.warning("    aborting pagination after page %d failure", page)
            break
        all_rows.extend(parse_rows(page_soup))
        time.sleep(REQUEST_DELAY)

    meta = {
        "event":           event_name,
        "gender":          gender,
        "age_category":    age_category,
        "event_id":        event_id,
        "discipline":      discipline,
        "pages_fetched":   last_page,
        "rows_extracted":  len(all_rows),
        "source_url":      build_url(discipline, event_slug, gender, 1, event_id, age_category),
        "extracted_on":    TODAY,
    }
    return all_rows, meta

# ── File output ────────────────────────────────────────────────────────────────

def save_event(
    event_name: str,
    gender: str,
    rows: list[dict[str, str]],
    meta: dict,
    age_category: str = "senior",
) -> Path:
    out_dir = SAVE_ROOT / to_slug(event_name) / age_dir(gender, age_category)
    out_dir.mkdir(parents=True, exist_ok=True)

    columns = ordered_columns(rows)

    csv_path = out_dir / "results.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    meta_path = out_dir / "metadata.json"
    with meta_path.open("w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, ensure_ascii=False)

    return csv_path

# ── Work list ──────────────────────────────────────────────────────────────────

def build_work_list(
    selected_events: list[str] | None,
    selected_gender: str | None,
    age_categories: list[str],
) -> list[tuple[str, str, int, str, str]]:
    """Return list of (event_name, gender, event_id, discipline, age_category) to process."""
    genders = [selected_gender] if selected_gender else ["men", "women"]
    work: list[tuple[str, str, int, str, str]] = []
    for age_category in age_categories:
        for gender in genders:
            for event_name, event_id in EVENT_IDS[gender].items():
                if selected_events and event_name not in selected_events:
                    continue
                try:
                    discipline = get_discipline(event_name)
                except ValueError as exc:
                    logging.warning("Skipping — %s", exc)
                    continue
                work.append((event_name, gender, event_id, discipline, age_category))
    return work

# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Extract World Athletics all-time toplists to CSV files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--events", nargs="+", metavar="EVENT",
        help='Restrict to specific events, e.g. --events "100 Metres" "Long Jump"',
    )
    parser.add_argument(
        "--gender", choices=["men", "women"],
        help="Restrict to one gender (default: both)",
    )
    parser.add_argument(
        "--max-pages", type=int, metavar="N",
        help="Cap pages per event — handy for a quick test run",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Skip any event/gender that already has a results.csv",
    )
    parser.add_argument(
        "--age-category", nargs="+", metavar="CAT",
        choices=["senior", "u20", "u18"],
        default=["senior"],
        help="Age categories to extract (default: senior). E.g. --age-category u20 u18",
    )
    args = parser.parse_args()

    work = build_work_list(args.events, args.gender, args.age_category)
    logging.info("Planned: %d event/gender combinations", len(work))

    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)

    done = skipped = failed = 0
    for event_name, gender, event_id, discipline, age_category in work:
        label = f"{event_name} / {gender} / {age_category}"
        out_csv = SAVE_ROOT / to_slug(event_name) / age_dir(gender, age_category) / "results.csv"

        if args.resume and out_csv.exists():
            logging.info("SKIP   %s", label)
            skipped += 1
            continue

        logging.info("START  %s", label)
        rows, meta = extract_event(session, event_name, gender, event_id, discipline, args.max_pages, age_category)

        if not rows:
            logging.warning("FAIL   %s — no rows returned", label)
            failed += 1
            continue

        csv_path = save_event(event_name, gender, rows, meta, age_category)
        logging.info("DONE   %s → %s  (%d rows)", label, csv_path, len(rows))
        done += 1

    logging.info("─" * 60)
    logging.info("Finished.  Done: %d  Skipped: %d  Failed: %d", done, skipped, failed)


if __name__ == "__main__":
    main()
