# HANDOVER — Athletics All-Time Performances project
*Written for the next Copilot session so it can continue without re-reading the whole conversation history.*

---

## 1. Workspace

```
c:\Users\simon\Documents\python\athletics_all_time\
```

---

## 2. What this project does

Extracts all-time best-performance toplists from worldathletics.org (personal/research use only)
for every athletics event × gender, saves one CSV per combination, and visualises those
distributions as interactive histograms on a local web app.

---

## 3. Files in the workspace

| File | Purpose |
|---|---|
| `extract.py` | Data extraction script — fetches from worldathletics.org, saves CSVs |
| `app.py` | Flask visualisation web app — serves histograms from the saved CSVs |
| `requirements.txt` | `requests`, `beautifulsoup4`, `lxml`, `flask` |
| `test_parse.py` | Quick sanity check that runs the HTML parser against the two reference pages |
| `references/50 Metres - men - senior - all.html` | Saved reference page (track event, no wind) — used to design the parser |
| `references/Long Jump - women - senior - all.html` | Saved reference page (field event, with wind) |
| `mathworksheets/` | Reference project — the new web app must match its look & feel exactly |
| `event_data/` | Output folder — one sub-directory per event, then `men/` and `women/` |
| `event_data/{slug}/{gender}/results.csv` | Extracted performances |
| `event_data/{slug}/{gender}/metadata.json` | Event name, event_id, pages fetched, date |

---

## 4. CSV schema

```
rank, mark, wind, athlete, athlete_url, dob, country, pos, venue, date, result_score
```

- `mark` is the raw string **as published on the site**, e.g. `7:55.0h`, `9.58`, `7.52`, `2:01:39`.
- **`h` suffix** = hand-timed (pre-electronic, common pre-1970s). Valid result, strip the `h` to
  get the numeric value. `parse_mark()` in `app.py` already handles this.
- Other suffixes that can appear: `A` / `a` (altitude-assisted). Same treatment — strip, use value.
- `wind` is blank for non-field events and indoor events; populated (e.g. `+1.4`) for outdoor
  field events.
- `athlete_url` is the full WA athlete profile URL (or relative path for newer-style URLs).

`parse_mark()` in `app.py` converts marks to floats:

| Format | Example | Result |
|---|---|---|
| Plain decimal | `9.58` | `9.58` |
| `M:SS.cc` | `1:45.01` | `105.01` (seconds) |
| `H:MM:SS` | `2:01:39` | `7299.0` (seconds) |
| Hand-timed | `7:55.0h` | `475.0` (seconds) |
| Altitude-assisted | `21.43A` | `21.43` |

For jump/throw events the mark is already in metres (plain float).
For combined events (decathlon, heptathlon) the mark is a points score (plain integer).
`event_unit()` in `app.py` returns `"seconds"`, `"metres"`, or `"points"` based on event name.

---

## 5. Extraction — current state

**The extraction script (`extract.py`) is running right now in a background terminal.**
It was at approximately **200 Metres / women, page 70 of 107** when this handover was written
(around 21:04 local time on 22 April 2026).

All the men's events and most of the sprint/field women's events are already saved.
The currently completed `event_data/` folders (seen on disk at time of writing):

```
10-kilometres-race-walk, 10-miles-road, 100-metres, 1000-metres,
1000-metres-short-track, 10000-metres, 10000-metres-race-walk,
10000-metres-race-walk-short-track, 110-metres-hurdles, 1500-metres,
1500-metres-short-track, 20-kilometres-race-walk, 200-metres (in progress),
200-metres-short-track, 2000-metres, 2000-metres-short-track, 2000-metres-steeplechase,
20000-metres-race-walk, 30-kilometres-race-walk, 300-metres, 300-metres-short-track,
3000-metres, 3000-metres-race-walk, 3000-metres-race-walk-short-track,
3000-metres-short-track, 3000-metres-steeplechase, 35-kilometres-race-walk, 400-metres,
400-metres-hurdles, 400-metres-short-track, 4x100-metres-relay, 4x1500-metres-relay,
4x200-metres-relay, 4x200-metres-relay-short-track, 4x400-metres-relay,
4x400-metres-relay-short-track, 4x800-metres-relay, 4x800-metres-relay-short-track,
5-kilometres-race-walk, 50-kilometres-race-walk, 50-metres, 50-metres-hurdles,
500-metres-short-track, 5000-metres, 5000-metres-race-walk,
5000-metres-race-walk-short-track, 5000-metres-short-track, 55-metres,
55-metres-hurdles, 60-metres, 60-metres-hurdles, 600-metres, 600-metres-short-track,
800-metres, 800-metres-short-track, decathlon, discus-throw, half-marathon,
half-marathon-race-walk, hammer-throw, heptathlon-short-track, high-jump,
javelin-throw, long-jump, marathon, marathon-race-walk, pole-vault, shot-put,
triple-jump
```

**Missing events** (not yet in the EVENT_IDS catalogue — may need to be added after checking the
live site):
- `5 Kilometres Road` (men/women)
- `15 Kilometres Road` (men/women)  
- `Mile Road` (men/women)
- `100 Metres Hurdles` (women — note: separate from 110mH men)
- `Heptathlon` (women — distinct from Heptathlon Short Track)
- `Pentathlon Short Track` (women)

To restart extraction after it finishes (or if it was interrupted):
```
python extract.py --resume
```
`--resume` skips any event/gender that already has a `results.csv`.

To re-fetch specific events:
```
python extract.py --events "100 Metres" "Long Jump" --gender women
```

---

## 6. Visualisation web app (`app.py`) — current state

**Written and complete.** Has not been run yet (Flask installation step was not confirmed).

To install dependencies and start:
```
pip install flask
python app.py
```
Then open http://127.0.0.1:5001

### What it does
- `/api/events`  — returns JSON list of all events that have a `results.csv` on disk
- `/api/data?event=<slug>&gender=men&gender=women` — returns parsed float arrays for histogram
- `/` — serves the single-page app

### Controls in the UI
- **Event** — dropdown populated from `event_data/` at startup
- **Gender** — `[x] men` / `[x] women` checkboxes (ASCII style, matching mathworksheets)
- **Axes** — `[ ] logarithmic Y` / `[ ] logarithmic X` checkboxes
- **Number of bins** — numeric input, default 100
- **Bin size** — numeric input, linked to number of bins (edit one → the other updates)
- Status line shows athlete count and current bin size

### Style
Matches `mathworksheets/` **exactly**:
- Dark monospace theme (`#05142c` background, Courier New font)
- ASCII `[ ]` / `[x]` checkboxes
- `--border`, `--panel`, `--muted` CSS variables
- Minimal footer "Personal / research use only. Data © World Athletics."
- Plotly 2.35.2 loaded from CDN

### Plotting
- Uses `Plotly.react()` with `barmode: "overlay"` for men/women comparison
- Men = `rgba(100, 160, 255, 0.80)` (blue), Women = `rgba(255, 110, 110, 0.80)` (red)
- Dark Plotly theme matching site background

---

## 7. Immediate next tasks

1. **Wait for / confirm extraction completes** — check the background terminal with
   `get_terminal_output` on terminal ID `80313ae2-6adc-4fb8-b0ab-608fca73c512`.
   If it was interrupted, run `python extract.py --resume`.

2. **Test the web app** — run `python app.py` and open http://127.0.0.1:5001.
   Verify: event list populates, histograms render for a few events, bin/binsize linking works.

3. **Possible improvements to discuss with user:**
   - Add a "5 km Road / Mile Road" etc. if they appear important — check live eventId dropdowns
   - Option to show KDE curve overlay instead of / in addition to bars
   - Tooltip on hover showing athlete name + mark (requires scatter trace, not just histogram)
   - For combined events (decathlon/heptathlon), X axis is already "points" — confirm user happy
   - Consider deployment (fly.io like mathworksheets, or GitHub Pages with pre-built JSON — but
     note data is from worldathletics.org so public deployment needs care re: ToS)

4. **Delete `mathworksheets/` from this workspace** once the user confirms the style match is good
   (user said they'd remove it once we'd understood the style — we have).

5. **Clean up `test_parse.py`** — useful during development, can be removed or kept.

---

## 8. Key design decisions already made

- **No Selenium / headless browser** — site is server-rendered HTML, plain `requests` + BS4 works.
- **`--resume` by default when re-running** to avoid duplicate fetching.
- **2-second delay** between requests (polite crawling).
- **`data-th` cell attributes** used for column identification — robust to column reordering.
- **Event IDs hardcoded** from the `<select id="eventId">` dropdowns in the two reference HTML files.
- **`utf-8-sig` CSV encoding** so Excel opens files without mojibake on accented names.
- **`h` / `A` suffixes stripped** in `parse_mark()` — value used as-is (still valid performance).
- **Bin count and bin size are linked** — editing one recalculates the other from the data range.

---

## 9. Reference material

- `mathworksheets/app.py` — the reference project. Read it to understand the exact CSS variables,
  ASCII checkbox pattern, grid layout, and overall style to replicate.
- `references/50 Metres - men - senior - all.html` — confirmed table structure, pagination, and
  eventId dropdown (for men). Contains full men's event list with IDs.
- `references/Long Jump - women - senior - all.html` — same for women; confirms wind column,
  10-page pagination, and women's event IDs.
