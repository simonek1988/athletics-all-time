"""
add_age.py
----------
Adds an 'age' column (float years at time of performance) to every
results.csv file in event_data/*/men/, women/, men-u20/, women-u20/,
men-u18/, and women-u18/.

DOB formats handled:
  DD MMM YYYY  → exact date
  MMM YYYY     → assume day 15 of that month
  YYYY         → assume 1 Jul of that year (mid-year)
  <empty>      → age left blank

Ages below MIN_AGE are treated as data errors and left blank.

Runs idempotently: if 'age' already exists it is recomputed.
"""

import csv
import io
import re
from datetime import date
from pathlib import Path

MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4,  "MAY": 5,  "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

MIN_AGE = 13  # ages below this are treated as data errors

_FULL_RE  = re.compile(r"^(\d{2}) ([A-Z]{3}) (\d{4})$")
_MON_RE   = re.compile(r"^([A-Z]{3}) (\d{4})$")
_YEAR_RE  = re.compile(r"^(\d{4})$")


def parse_date(s: str) -> date | None:
    """Parse a date string to a datetime.date, or None if unparseable."""
    s = s.strip()
    m = _FULL_RE.match(s)
    if m:
        return date(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)))
    m = _MON_RE.match(s)
    if m:
        return date(int(m.group(2)), MONTHS[m.group(1)], 15)
    m = _YEAR_RE.match(s)
    if m:
        return date(int(m.group(1)), 7, 1)
    return None


def age_years(dob: date, perf: date) -> float:
    return (perf - dob).days / 365.25


def process_file(path: Path) -> tuple[int, int]:
    """
    Read results.csv, add/recompute 'age' column, write back in-place.
    Returns (total_rows, rows_with_age).
    """
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        original_fields = reader.fieldnames or []
        rows = list(reader)

    if not rows:
        return 0, 0

    # Build fieldnames with 'age' appended (or already present)
    if "age" in original_fields:
        out_fields = original_fields
    else:
        out_fields = original_fields + ["age"]

    total = 0
    with_age = 0
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=out_fields, lineterminator="\n",
                            extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        total += 1
        dob_str  = row.get("dob",  "").strip()
        date_str = row.get("date", "").strip()
        dob  = parse_date(dob_str)
        perf = parse_date(date_str)
        if dob and perf and perf >= dob:
            age = round(age_years(dob, perf), 2)
            if age >= MIN_AGE:
                row["age"] = age
                with_age += 1
            else:
                row["age"] = ""
        else:
            row["age"] = ""
        writer.writerow(row)

    path.write_text(buf.getvalue(), encoding="utf-8")
    return total, with_age


def main():
    root = Path("event_data")
    valid_folders = {"men", "women", "men-u20", "women-u20", "men-u18", "women-u18"}
    csvs = sorted(
        p for p in root.rglob("results.csv")
        if p.parent.name in valid_folders
    )

    if not csvs:
        print("No results.csv files found under event_data/")
        return

    total_files = total_rows = total_aged = 0
    for p in csvs:
        rows, aged = process_file(p)
        total_files += 1
        total_rows  += rows
        total_aged  += aged
        print(f"  {p.relative_to(root)}  {aged}/{rows} rows have age")

    print(f"\nDone. {total_files} files, {total_aged}/{total_rows} rows with age computed.")


if __name__ == "__main__":
    main()
