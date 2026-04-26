"""Quick validation of extract.py parsing against saved reference HTML files."""
from bs4 import BeautifulSoup
import pathlib, sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from extract import parse_rows, get_last_page, ordered_columns

files = [
    "references/50 Metres - men - senior - all.html",
    "references/Long Jump - women - senior - all.html",
]

for fname in files:
    html = pathlib.Path(fname).read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    last  = get_last_page(soup)
    rows  = parse_rows(soup)
    cols  = ordered_columns(rows)
    print()
    print(f"=== {fname}")
    print(f"  last_page : {last}")
    print(f"  rows      : {len(rows)}")
    print(f"  columns   : {cols}")
    if rows:
        r = rows[0]
        for col in cols:
            print(f"  {col:<15}: {r.get(col, '')}")
