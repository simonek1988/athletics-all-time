#!/usr/bin/env python3
"""
Athletics All-Time Performances — Visualisation Web App
========================================================
Run locally:
    pip install flask
    python app.py
Then open http://127.0.0.1:5001 in your browser.

Reads the CSVs produced by extract.py from the event_data/ directory.
No external data dependencies at runtime — everything is served from disk.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)
EVENT_DATA = Path("event_data")


# ── Mark parsing ───────────────────────────────────────────────────────────────

def parse_mark(raw: str) -> float | None:
    """
    Convert a raw mark string to a numeric float.

    Handled formats
    ---------------
    9.58          → 9.58      (sprint, plain seconds)
    1:45.01       → 105.01    (middle distance, M:SS.cc → seconds)
    2:01:39       → 7299.0    (marathon, H:MM:SS → seconds)
    7:55.0h       → 475.0     (hand-timed 'h' suffix stripped)
    7.52          → 7.52      (long jump, metres)
    1108          → 1108.0    (decathlon score, plain integer)
    """
    if not raw:
        return None
    s = raw.strip()
    # Strip known suffix flags: h/H = hand-timed, A/a = altitude-assisted
    s = s.rstrip("hHaA").strip()
    if not s:
        return None
    parts = s.split(":")
    try:
        if len(parts) == 3:
            # H:MM:SS[.cc]
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            # M:SS[.cc]
            return int(parts[0]) * 60 + float(parts[1])
        else:
            return float(s)
    except (ValueError, IndexError):
        return None


def _percentile(sorted_data: list, p: float) -> float:
    """Linear-interpolation percentile on pre-sorted data. p in [0, 100]."""
    n = len(sorted_data)
    if n == 0:
        return float("nan")
    if n == 1:
        return sorted_data[0]
    idx = (p / 100.0) * (n - 1)
    lo  = int(idx)
    hi  = min(lo + 1, n - 1)
    return sorted_data[lo] + (idx - lo) * (sorted_data[hi] - sorted_data[lo])


def _parse_combo(combo: str) -> tuple[str, bool]:
    """Return (base_folder, is_short_track) for a combo key like 'men-standard'."""
    if combo.endswith("-short"):    return combo[:-6], True
    if combo.endswith("-standard"): return combo[:-9], False
    return combo, False


def event_unit(event_name: str) -> str:
    """Return the measurement unit for an event based on its name."""
    low = event_name.lower()
    if any(x in low for x in ("decathlon", "heptathlon", "pentathlon")):
        return "points"
    if any(x in low for x in ("jump", "vault", "throw", "put")):
        return "metres"
    return "seconds"


# ── API ────────────────────────────────────────────────────────────────────────

def _distance_metres(name: str) -> "float | None":
    """Extract the primary distance in metres from an event name (for sorting)."""
    low = name.lower()
    m = re.search(r"(\d+)\s*[x\u00d7]\s*(\d[\d,]*(?:\.\d+)?)\s*(metres|kilometres|miles)", low)
    if m:
        count = int(m.group(1))
        dist  = float(m.group(2).replace(",", ""))
        unit  = m.group(3)
        if unit.startswith("k"):  dist *= 1000
        elif unit.startswith("mi"): dist *= 1609.34
        return count * dist
    if "half marathon" in low or "half-marathon" in low:
        return 21097.5
    if "marathon" in low:
        return 42195.0
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*kilometres", low)
    if m:
        return float(m.group(1).replace(",", "")) * 1000
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*miles", low)
    if m:
        return float(m.group(1).replace(",", "")) * 1609.34
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*metres", low)
    if m:
        return float(m.group(1).replace(",", ""))
    return None


def _event_type(name: str) -> int:
    """Return a sort bucket: 0=running, 1=relay, 2=race walk, 3=jumping, 4=throwing, 5=other."""
    low = name.lower()
    if "relay" in low:                                              return 1
    if "race walk" in low:                                         return 2
    if any(x in low for x in ("jump", "vault")):                   return 3
    if any(x in low for x in ("throw", "put")):                    return 4
    if any(x in low for x in ("decathlon", "heptathlon", "pentathlon")): return 5
    return 0  # running (track, road, hurdles, steeplechase, short track, …)


def _event_sort_key(ev: dict):
    """Sort by type bucket, then distance in metres, then alphabetically."""
    low  = ev["name"].lower()
    typ  = _event_type(ev["name"])
    dist = _distance_metres(ev["name"]) or 0.0
    return (typ, dist, low)


@app.route("/api/events")
def api_events():
    """Return sorted list of all events available on disk."""
    if not EVENT_DATA.exists():
        return jsonify([])
    events = []
    for event_dir in sorted(EVENT_DATA.iterdir()):
        if not event_dir.is_dir():
            continue
        genders = sorted(
            g.name for g in event_dir.iterdir()
            if g.is_dir() and (g / "results.csv").exists()
        )
        if not genders:
            continue
        name = event_dir.name          # fallback: slug
        for g in genders:
            meta_path = event_dir / g / "metadata.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                name = meta.get("event", name)
                break
        events.append({"slug": event_dir.name, "name": name, "genders": genders})
    events.sort(key=_event_sort_key)
    return jsonify(events)


@app.route("/api/data")
def api_data():
    """
    Return parsed mark values for a given event and combination(s).

    Query params:
        event – the event slug (e.g. "100-metres")
        combo – one or more folder names: men, women, men-u20, women-u18, …
    """
    slug   = request.args.get("event", "").strip()
    combos = request.args.getlist("combo")
    if not slug or not combos:
        return jsonify({"error": "missing params"}), 400

    result: dict = {"marks": {}, "unit": "seconds", "event": slug}
    event_name = slug

    for combo in combos:
        csv_path = EVENT_DATA / slug / combo / "results.csv"
        if not csv_path.exists():
            continue
        meta_path = EVENT_DATA / slug / combo / "metadata.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            event_name = meta.get("event", event_name)
        marks: list[float] = []
        with csv_path.open(encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                v = parse_mark(row.get("mark", ""))
                if v is not None:
                    marks.append(v)
        result["marks"][combo] = marks

    result["unit"]  = event_unit(event_name)
    result["event"] = event_name
    return jsonify(result)


@app.route("/api/pace")
def api_pace():
    """
    Return average pace (s/km) of the top-N athletes for each flat running event.

    Query params:
        combo       – one or more combo folder names (men, women-u20, …)
        top_n       – number of top athletes to average (default 10)
        short_track – "true" to include Short Track events (default false)
    """
    combos      = request.args.getlist("combo")
    top_n       = max(1, int(request.args.get("top_n", "10") or "10"))
    parsed_combos = {c: _parse_combo(c) for c in combos}

    if not combos:
        return jsonify({"error": "missing params"}), 400

    series: dict[str, list] = {c: [] for c in combos}

    if not EVENT_DATA.exists():
        return jsonify({"series": series})

    for event_dir in sorted(EVENT_DATA.iterdir()):
        if not event_dir.is_dir():
            continue

        # Resolve event name from any available metadata
        event_name = event_dir.name
        for sub in event_dir.iterdir():
            if sub.is_dir() and (sub / "metadata.json").exists():
                meta = json.loads((sub / "metadata.json").read_text(encoding="utf-8"))
                event_name = meta.get("event", event_name)
                break

        low = event_name.lower()

        # Only flat running (type 0), no relays, race walk
        if _event_type(event_name) != 0:
            continue
        if "relay" in low:
            continue
        if "hurdle" in low or "steeplechase" in low:
            continue
        is_shorttrack = "short track" in low

        dist_m = _distance_metres(event_name)
        if not dist_m:
            continue

        for combo in combos:
            base_folder, wants_short = parsed_combos[combo]
            if is_shorttrack != wants_short:
                continue
            csv_path = EVENT_DATA / event_dir.name / base_folder / "results.csv"
            if not csv_path.exists():
                continue
            marks: list[float] = []
            with csv_path.open(encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    v = parse_mark(row.get("mark", ""))
                    if v is not None:
                        marks.append(v)
                    if len(marks) >= top_n:
                        break
            if not marks:
                continue
            avg_time = sum(marks) / len(marks)
            pace_s_per_km = avg_time / (dist_m / 1000.0)
            series[combo].append({
                "event":    event_name,
                "distance": dist_m,
                "pace":     round(pace_s_per_km, 4),
                "n":        len(marks),
            })

    # Sort each series by distance ascending
    for pts in series.values():
        pts.sort(key=lambda p: p["distance"])

    return jsonify({"series": series})


@app.route("/api/age")
def api_age():
    """
    Return age-at-performance values from the 'age' column (added by add_age.py).

    Query params:
        event – the event slug
        combo – one or more combo folder names (only senior men/women have age data)
    """
    slug   = request.args.get("event", "").strip()
    combos = request.args.getlist("combo")
    if not slug or not combos:
        return jsonify({"error": "missing params"}), 400

    result: dict = {"ages": {}, "event": slug}
    event_name = slug

    for combo in combos:
        csv_path = EVENT_DATA / slug / combo / "results.csv"
        if not csv_path.exists():
            continue
        meta_path = EVENT_DATA / slug / combo / "metadata.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            event_name = meta.get("event", event_name)
        ages: list[float] = []
        with csv_path.open(encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                raw = row.get("age", "").strip()
                if raw:
                    try:
                        ages.append(float(raw))
                    except ValueError:
                        pass
        result["ages"][combo] = ages

    result["event"] = event_name
    return jsonify(result)


@app.route("/api/age_stats")
def api_age_stats():
    """
    Return per-event age distribution statistics across all running events.

    Query params:
        combo       – one or more combo keys (men, women, men-u20, …)
        top_n       – slice to top-N best performers per event (default: all)
        hurdles     – "true" to include hurdles / steeplechase (default: false)
        short_track – "true" to include Short Track events (default: false)
    """
    combos      = request.args.getlist("combo")
    top_n_s     = request.args.get("top_n", "").strip()
    top_n       = max(1, int(top_n_s)) if top_n_s else None
    parsed_combos = {c: _parse_combo(c) for c in combos}

    if not combos:
        return jsonify({"error": "missing params"}), 400

    results: list[dict] = []

    for event_dir in sorted(EVENT_DATA.iterdir()):
        if not event_dir.is_dir():
            continue

        event_name = event_dir.name
        for sub in event_dir.iterdir():
            if sub.is_dir() and (sub / "metadata.json").exists():
                meta = json.loads((sub / "metadata.json").read_text(encoding="utf-8"))
                event_name = meta.get("event", event_name)
                break

        low = event_name.lower()

        if _event_type(event_name) != 0:
            continue
        if "relay" in low:
            continue
        if "hurdle" in low or "steeplechase" in low:
            continue
        is_shorttrack = "short track" in low

        dist_m = _distance_metres(event_name)
        if not dist_m:
            continue

        combo_stats: dict = {}
        for combo in combos:
            base_folder, wants_short = parsed_combos[combo]
            if is_shorttrack != wants_short:
                continue
            csv_path = EVENT_DATA / event_dir.name / base_folder / "results.csv"
            if not csv_path.exists():
                continue
            ages: list[float] = []
            with csv_path.open(encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    raw = row.get("age", "").strip()
                    if raw:
                        try:
                            ages.append(float(raw))
                        except ValueError:
                            pass
                    if top_n and len(ages) >= top_n:
                        break
            if not ages:
                continue
            ages_s = sorted(ages)
            n    = len(ages_s)
            mean = sum(ages_s) / n
            std  = (sum((x - mean) ** 2 for x in ages_s) / n) ** 0.5
            combo_stats[combo] = {
                "n":    n,
                "mean": round(mean, 3),
                "std":  round(std,  3),
                "p10":  round(_percentile(ages_s, 10), 3),
                "p25":  round(_percentile(ages_s, 25), 3),
                "p50":  round(_percentile(ages_s, 50), 3),
                "p75":  round(_percentile(ages_s, 75), 3),
                "p90":  round(_percentile(ages_s, 90), 3),
            }

        if combo_stats:
            results.append({
                "slug":    event_dir.name,
                "name":    event_name,
                "dist_m":  dist_m,
                "combos":  combo_stats,
            })

    results.sort(key=lambda e: e["dist_m"])
    return jsonify({"events": results})


# ── Frontend ───────────────────────────────────────────────────────────────────

HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Athletics All-Time Performances</title>
  <style>
    :root {
      --bg:      #05142c;
      --panel:   #040f22;
      --text:    #ffffff;
      --muted:   rgba(255,255,255,.75);
      --border:  rgba(255,255,255,.65);
      --border2: rgba(255,255,255,.25);
      --inputbg: #000000;
      --accent:  rgba(255,255,255,.9);
    }

    html { height: 100%; scroll-snap-type: y mandatory; overflow-y: scroll; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "Courier New", Courier, ui-monospace, SFMono-Regular,
                   Menlo, Consolas, monospace;
      font-size: 15px;
      line-height: 1.35;
    }

    .wrap {
      max-width: 1600px;
      margin: 0 auto;
      padding: 24px 18px 40px 18px;
    }

    h1 {
      margin: 0 0 10px 0;
      font-size: 22px;
      font-weight: 700;
      letter-spacing: .3px;
    }

    .subtitle {
      margin: 0 0 18px 0;
      color: var(--muted);
    }

    .panel {
      border: 1px solid var(--border2);
      background: var(--panel);
      padding: 16px;
    }

    .grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px 18px;
      align-items: start;
    }

    .full { grid-column: 1 / -1; }

    .row3 {
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap: 14px 18px;
      align-items: start;
    }

    label {
      display: block;
      font-weight: 700;
      margin-bottom: 6px;
    }

    select,
    input[type="number"] {
      width: 100%;
      box-sizing: border-box;
      padding: 10px;
      border: 1px solid var(--border);
      border-radius: 0;
      background: var(--inputbg);
      color: var(--text);
      font-family: inherit;
      font-size: 14px;
      outline: none;
    }
    select:focus,
    input[type="number"]:focus { border-color: var(--accent); }

    .hint {
      margin-top: 6px;
      font-size: 12px;
      color: var(--muted);
    }

    .checks {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px 14px;
      margin-top: 6px;
    }

    .checks-inline {
      grid-template-columns: repeat(4, 1fr);
    }

    .check { display: flex; align-items: center; gap: 10px; }

    .ascii-check { cursor: pointer; user-select: none; }
    .ascii-check input[type="checkbox"] {
      position: absolute;
      opacity: 0;
      width: 1px;
      height: 1px;
      pointer-events: none;
    }
    .ascii-check .box {
      display: inline-block;
      min-width: 3ch;
    }
    .ascii-check .box::before                             { content: "[ ]"; }
    .ascii-check input[type="checkbox"]:checked + .box::before { content: "[x]"; }
    .ascii-check input[type="checkbox"]:focus  + .box::before {
      outline: 1px solid var(--border);
      outline-offset: 2px;
    }
    .ascii-check .label { color: var(--text); }

    #plot-wrap {
      margin-top: 18px;
      border: 1px solid var(--border2);
      background: var(--panel);
      flex: 1;
      min-height: 0;
    }

    #plot { height: 100%; }

    .status {
      margin-top: 8px;
      color: var(--muted);
      font-size: 12px;
      min-height: 1.4em;
    }

    .site-footer {
      margin-top: 18px;
      color: var(--muted);
      font-size: 12px;
      display: flex;
      justify-content: space-between;
    }

    .page-section {
      height: 100vh;
      width: 100%;
      overflow: hidden;
      scroll-snap-align: start;
      display: flex;
      flex-direction: column;
    }
    .page-section > .wrap {
      flex: 1;
      min-height: 0;
      width: 100%;
      max-width: 1200px;
      margin: 0 auto;
      box-sizing: border-box;
      display: flex;
      flex-direction: column;
      padding-bottom: 10px;
    }
    /* Desktop: section-chart grows to fill remaining space */
    .section-chart {
      flex: 1;
      min-height: 0;
      margin-top: 18px;
      border: 1px solid var(--border2);
      background: var(--panel);
    }
    .section-chart > div {
      height: 100%;
    }

    @media (max-width: 700px) {
      .grid          { grid-template-columns: 1fr; }
      .checks        { grid-template-columns: 1fr; }
      .checks-inline { grid-template-columns: 1fr 1fr; }

      /* On mobile: free scrolling, no fixed 100vh sections */
      html { scroll-snap-type: none; }
      .page-section {
        height: auto;
        overflow: visible;
      }
      .page-section > .wrap {
        flex: none;
      }

      /* Controls block: at least full screen height so it feels like one page */
      .section-controls {
        min-height: 100dvh;
        display: flex;
        flex-direction: column;
        justify-content: flex-start;
        padding-bottom: 8px;
      }

      /* Chart block: fixed height, visually its own screen */
      .section-chart {
        height: 75vw;
        min-height: 260px;
        max-height: 92vw;
        flex: none;
        margin-top: 0;
        margin-bottom: 24px;
      }
      .section-chart > div {
        height: 100%;
      }
    }
  </style>
</head>
<body>
<section class="page-section">
<div class="wrap">
  <div class="section-controls">
  <h1>Athletics All-Time Performances</h1>
  <p class="subtitle">Distribution of best-ever athletes across athletics events.</p>

  <div class="panel">
    <div class="grid">

      <div class="full">
        <label for="event-select">Event</label>
        <select id="event-select"><option value="">Loading…</option></select>
      </div>

      <div>
        <label>Gender</label>
        <div class="checks">
          <label class="check ascii-check">
            <input type="checkbox" id="chk-men" checked />
            <span class="box" aria-hidden="true"></span>
            <span class="label">men</span>
          </label>
          <label class="check ascii-check">
            <input type="checkbox" id="chk-women" checked />
            <span class="box" aria-hidden="true"></span>
            <span class="label">women</span>
          </label>
        </div>
      </div>

      <div>
        <label>Age category</label>
        <div class="checks" style="grid-template-columns:1fr 1fr 1fr">
          <label class="check ascii-check">
            <input type="checkbox" id="chk-senior" checked />
            <span class="box" aria-hidden="true"></span>
            <span class="label">senior</span>
          </label>
          <label class="check ascii-check">
            <input type="checkbox" id="chk-u20" />
            <span class="box" aria-hidden="true"></span>
            <span class="label">U20</span>
          </label>
          <label class="check ascii-check">
            <input type="checkbox" id="chk-u18" />
            <span class="box" aria-hidden="true"></span>
            <span class="label">U18</span>
          </label>
        </div>
      </div>

      <div>
        <label>Axes</label>
        <div class="checks">
          <label class="check ascii-check">
            <input type="checkbox" id="chk-logy" />
            <span class="box" aria-hidden="true"></span>
            <span class="label">logarithmic Y</span>
          </label>
          <label class="check ascii-check">
            <input type="checkbox" id="chk-normalize" />
            <span class="box" aria-hidden="true"></span>
            <span class="label">normalize</span>
          </label>
        </div>
      </div>

      <div class="full row3">
        <div>
          <label for="input-bins">Number of bins</label>
          <input type="number" id="input-bins" value="100" min="2" max="5000" />
        </div>
        <div>
          <label for="input-binsize">
            Bin size
            <span id="unit-label" style="color:var(--muted);font-weight:400">(s)</span>
          </label>
          <input type="number" id="input-binsize" step="any" min="0" />
        </div>
        <div>
          <label for="input-max-athletes">Top N athletes</label>
          <input type="number" id="input-max-athletes" placeholder="all" min="1" />
        </div>
      </div>

    </div>
  </div>

  </div><!-- /.section-controls -->

  <div class="section-chart">
  <div id="plot-wrap"><div id="plot" style="height:100%"></div></div>
  <div id="status" class="status"></div>
  </div><!-- /.section-chart -->

  <div class="site-footer">
    <span>© Simon Ek. Data © World Athletics.</span>
  </div>
</div>
</section>

<section class="page-section" id="s2">
<div class="wrap">
  <div class="section-controls">
  <h1>Pace vs. Distance</h1>
  <p class="subtitle">Average pace of the top-N athletes across flat running events.</p>

  <div class="panel">
    <div class="grid">

      <div>
        <label>Gender</label>
        <div class="checks">
          <label class="check ascii-check">
            <input type="checkbox" id="s2-chk-men" checked />
            <span class="box" aria-hidden="true"></span>
            <span class="label">men</span>
          </label>
          <label class="check ascii-check">
            <input type="checkbox" id="s2-chk-women" checked />
            <span class="box" aria-hidden="true"></span>
            <span class="label">women</span>
          </label>
        </div>
      </div>

      <div>
        <label>Age category</label>
        <div class="checks" style="grid-template-columns:1fr 1fr 1fr">
          <label class="check ascii-check">
            <input type="checkbox" id="s2-chk-senior" checked />
            <span class="box" aria-hidden="true"></span>
            <span class="label">senior</span>
          </label>
          <label class="check ascii-check">
            <input type="checkbox" id="s2-chk-u20" />
            <span class="box" aria-hidden="true"></span>
            <span class="label">U20</span>
          </label>
          <label class="check ascii-check">
            <input type="checkbox" id="s2-chk-u18" />
            <span class="box" aria-hidden="true"></span>
            <span class="label">U18</span>
          </label>
        </div>
      </div>

      <div>
        <label>Track type</label>
        <div class="checks">
          <label class="check ascii-check">
            <input type="checkbox" id="s2-chk-standard" checked />
            <span class="box" aria-hidden="true"></span>
            <span class="label">standard</span>
          </label>
          <label class="check ascii-check">
            <input type="checkbox" id="s2-chk-short" />
            <span class="box" aria-hidden="true"></span>
            <span class="label">short</span>
          </label>
        </div>
      </div>

      <div>
        <label>Axes / display</label>
        <div class="checks">
          <label class="check ascii-check">
            <input type="checkbox" id="s2-chk-logx" checked />
            <span class="box" aria-hidden="true"></span>
            <span class="label">logarithmic X</span>
          </label>
          <label class="check ascii-check">
            <input type="checkbox" id="s2-chk-trendline" />
            <span class="box" aria-hidden="true"></span>
            <span class="label">trendline</span>
          </label>
        </div>
      </div>

      <div>
        <label for="s2-input-topn">Top N athletes</label>
        <input type="number" id="s2-input-topn" value="10" min="1" max="10000" />
        <div class="hint">Pace = average of top N</div>
      </div>

    </div>
  </div>

  </div><!-- /.section-controls -->

  <div class="section-chart">
  <div id="pace-plot-wrap">
    <div id="pace-plot" style="height:100%"></div>
  </div>
  <div id="s2-status" class="status"></div>
  </div><!-- /.section-chart -->

  <div class="site-footer">
    <span>© Simon Ek. Data © World Athletics.</span>
  </div>
</div>
</section>

<section class="page-section" id="s3">
<div class="wrap">
  <div class="section-controls">
  <h1>Peak Age Distribution</h1>
  <p class="subtitle">Age of athletes at the time of their best performance.</p>

  <div class="panel">
    <div class="grid">

      <div class="full">
        <label for="s3-event-select">Event</label>
        <select id="s3-event-select"><option value="">Loading…</option></select>
      </div>

      <div>
        <label>Gender</label>
        <div class="checks">
          <label class="check ascii-check">
            <input type="checkbox" id="s3-chk-men" checked />
            <span class="box" aria-hidden="true"></span>
            <span class="label">men</span>
          </label>
          <label class="check ascii-check">
            <input type="checkbox" id="s3-chk-women" checked />
            <span class="box" aria-hidden="true"></span>
            <span class="label">women</span>
          </label>
        </div>
      </div>

      <div class="full">
        <label>Display / fit</label>
        <div class="checks checks-inline">
          <label class="check ascii-check">
            <input type="checkbox" id="s3-chk-logy" />
            <span class="box" aria-hidden="true"></span>
            <span class="label">logarithmic Y</span>
          </label>
          <label class="check ascii-check">
            <input type="checkbox" id="s3-chk-normalize" checked />
            <span class="box" aria-hidden="true"></span>
            <span class="label">normalize</span>
          </label>
          <label class="check ascii-check">
            <input type="checkbox" id="s3-chk-cumulative" />
            <span class="box" aria-hidden="true"></span>
            <span class="label">cumulative</span>
          </label>
          <label class="check ascii-check">
            <input type="checkbox" id="s3-chk-gaussian" />
            <span class="box" aria-hidden="true"></span>
            <span class="label">Gaussian fit</span>
          </label>
        </div>
      </div>

      <div class="full row3">
        <div>
          <label for="s3-input-bins">Number of bins</label>
          <input type="number" id="s3-input-bins" value="50" min="2" max="5000" />
        </div>
        <div>
          <label for="s3-input-binsize">Bin size (years)</label>
          <input type="number" id="s3-input-binsize" step="any" min="0" value="0.5" />
        </div>
        <div>
          <label for="s3-input-max-athletes">Top N athletes</label>
          <input type="number" id="s3-input-max-athletes" placeholder="all" min="1" />
        </div>
      </div>

    </div>
  </div>

  </div><!-- /.section-controls -->

  <div class="section-chart">
  <div id="age-plot-wrap">
    <div id="age-plot" style="height:100%"></div>
  </div>
  <div id="s3-status" class="status"></div>
  </div><!-- /.section-chart -->

  <div class="site-footer">
    <span>© Simon Ek. Data © World Athletics.</span>
  </div>
</div>
</section>

<section class="page-section" id="s4">
<div class="wrap">
  <div class="section-controls">
  <h1>Age Trends by Event</h1>
  <p class="subtitle">How athlete peak age varies across running distances.</p>

  <div class="panel">
    <div class="grid">

      <div>
        <label>Gender</label>
        <div class="checks">
          <label class="check ascii-check">
            <input type="checkbox" id="s4-chk-men" checked />
            <span class="box" aria-hidden="true"></span>
            <span class="label">men</span>
          </label>
          <label class="check ascii-check">
            <input type="checkbox" id="s4-chk-women" checked />
            <span class="box" aria-hidden="true"></span>
            <span class="label">women</span>
          </label>
        </div>
      </div>

      <div>
        <label>Track type</label>
        <div class="checks">
          <label class="check ascii-check">
            <input type="checkbox" id="s4-chk-standard" checked />
            <span class="box" aria-hidden="true"></span>
            <span class="label">standard</span>
          </label>
          <label class="check ascii-check">
            <input type="checkbox" id="s4-chk-short" />
            <span class="box" aria-hidden="true"></span>
            <span class="label">short</span>
          </label>
        </div>
      </div>

      <div class="full">
        <label>Axes &amp; Metrics</label>
        <div class="checks checks-inline">
          <label class="check ascii-check">
            <input type="checkbox" id="s4-chk-logx" checked />
            <span class="box" aria-hidden="true"></span>
            <span class="label">logarithmic X</span>
          </label>
          <label class="check ascii-check">
            <input type="checkbox" id="s4-chk-p50" checked />
            <span class="box" aria-hidden="true"></span>
            <span class="label">median (P50)</span>
          </label>
          <label class="check ascii-check">
            <input type="checkbox" id="s4-chk-iqr" checked />
            <span class="box" aria-hidden="true"></span>
            <span class="label">Inner percentile band (P25&ndash;P75)</span>
          </label>
          <label class="check ascii-check">
            <input type="checkbox" id="s4-chk-outer" />
            <span class="box" aria-hidden="true"></span>
            <span class="label">Outer percentile band (P10&ndash;P90)</span>
          </label>
        </div>
      </div>

      <div>
        <label for="s4-input-topn">Top N athletes</label>
        <input type="number" id="s4-input-topn" placeholder="all" min="1" />
      </div>

    </div>
  </div>

  </div><!-- /.section-controls -->

  <div class="section-chart">
  <div id="age-trend-wrap">
    <div id="age-trend-plot" style="height:100%"></div>
  </div>
  <div id="s4-status" class="status"></div>
  </div><!-- /.section-chart -->

  <div class="site-footer">
    <span>© Simon Ek. Data © World Athletics.</span>
  </div>
</div>
</section>

<section class="page-section" id="s5">
<div class="wrap" style="justify-content:center">
  <h1>Data &amp; Attribution</h1>
  <p class="subtitle">About the data used in this tool.</p>

  <div class="panel" style="max-width:660px">
    <p style="color:var(--text);line-height:1.75;font-size:15px">
      All athletics performance data was sourced from the
      <a href="https://worldathletics.org/records/all-time-toplists"
         target="_blank" rel="noopener noreferrer"
         style="color:#5aadff;text-decoration:underline">World Athletics all-time top lists</a>,
      collected in April 2026.
    </p>
    <p style="color:var(--muted);line-height:1.75;font-size:13px;margin-top:14px">
      Personal and research use only. Data &copy; World Athletics. © Simon Ek
    </p>
  </div>
</div>
</section>

<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
<script>
"use strict";

// ── DOM refs ──────────────────────────────────────────────────────────────────
const eventSelect      = document.getElementById("event-select");
const chkMen           = document.getElementById("chk-men");
const chkWomen         = document.getElementById("chk-women");
const chkSenior        = document.getElementById("chk-senior");
const chkU20           = document.getElementById("chk-u20");
const chkU18           = document.getElementById("chk-u18");
const chkLogY          = document.getElementById("chk-logy");
const chkNormalize     = document.getElementById("chk-normalize");
const inputBins        = document.getElementById("input-bins");
const inputBinSize     = document.getElementById("input-binsize");
const inputMaxAthletes = document.getElementById("input-max-athletes");
const unitLabel        = document.getElementById("unit-label");
const statusEl         = document.getElementById("status");

// ── State ─────────────────────────────────────────────────────────────────────
let currentRange  = null;   // [min, max] across all loaded marks
let currentUnit   = "seconds";
let cachedData    = null;   // last fetched API response
let debounceTimer = null;

const COLOURS = {
  "men":        "rgba(70,  130, 220, 0.82)",
  "men-u20":    "rgba(80,  200, 240, 0.78)",
  "men-u18":    "rgba(120, 230, 200, 0.74)",
  "women":      "rgba(220,  55,  55, 0.82)",
  "women-u20":  "rgba(235, 110, 180, 0.78)",
  "women-u18":  "rgba(240, 170,  80, 0.74)",
};

const LABELS = {
  "men":        "men",
  "men-u20":    "men U20",
  "men-u18":    "men U18",
  "women":      "women",
  "women-u20":  "women U20",
  "women-u18":  "women U18",
};

function comboBase(key)   { return key.replace(/-(?:standard|short)$/, ""); }
function comboIsShort(key){ return key.endsWith("-short"); }
function comboColour(key) { return COLOURS[comboBase(key)] || "rgba(255,255,255,.6)"; }
function comboLabel(key)  {
  const base = comboBase(key);
  return (LABELS[base] || base) + (comboIsShort(key) ? " (short)" : "");
}

const DARK = {
  paper_bgcolor: "#040f22",
  plot_bgcolor:  "#040f22",
};

const AXIS_BASE = {
  color:          "rgba(255,255,255,.80)",
  gridcolor:      "rgba(255,255,255,.12)",
  zerolinecolor:  "rgba(255,255,255,.20)",
  tickfont:       { family: '"Courier New", monospace', size: 11 },
  title:          { font: { family: '"Courier New", monospace', size: 13 } },
};

function buildCombos() {
  const genders = [];
  if (chkMen.checked)   genders.push("men");
  if (chkWomen.checked) genders.push("women");
  const ages = [];
  if (chkSenior.checked) ages.push("senior");
  if (chkU20.checked)    ages.push("u20");
  if (chkU18.checked)    ages.push("u18");
  const combos = [];
  for (const g of genders)
    for (const a of ages)
      combos.push(a === "senior" ? g : `${g}-${a}`);
  return combos;
}

// ── Event list ─────────────────────────────────────────────────────────────────
async function loadEvents() {
  try {
    const resp   = await fetch("/api/events");
    const events = await resp.json();
    for (const sel of [eventSelect, document.getElementById("s3-event-select")]) {
      if (!sel) continue;
      sel.innerHTML = "";
      const skipRelays = (sel !== eventSelect);
      for (const ev of events) {
        if (skipRelays && /relay/i.test(ev.name)) continue;
        const opt       = document.createElement("option");
        opt.value       = ev.slug;
        opt.textContent = ev.name;
        sel.appendChild(opt);
      }
      // Default to 100 m
      const m100 = events.find(e => /^100\s*metres?$/i.test(e.name));
      if (m100) sel.value = m100.slug;
    }
    if (events.length) { updatePlot(); updateAgePlot(); }
  } catch (e) {
    statusEl.textContent = "Could not load event list.";
  }
}

// ── Linked bins ↔ bin size ────────────────────────────────────────────────────
function onBinsChanged() {
  if (currentRange !== null) {
    const n    = Math.max(2, parseInt(inputBins.value) || 100);
    const size = (currentRange[1] - currentRange[0]) / n;
    inputBinSize.value = +size.toPrecision(4);
  }
  scheduleRedraw();
}

function onBinSizeChanged() {
  if (currentRange !== null) {
    const size = parseFloat(inputBinSize.value);
    if (size > 0) {
      const n = Math.round((currentRange[1] - currentRange[0]) / size);
      inputBins.value = Math.max(2, n);
    }
  }
  scheduleRedraw();
}

// ── Debounce ──────────────────────────────────────────────────────────────────
function scheduleRedraw() {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(redraw, 250);
}

// ── Histogram helper ─────────────────────────────────────────────────────────
function computeHistogram(values, binStart, binSize, numBins) {
  // Determine rounding precision from binSize (e.g. 0.05 → 4 dp) to eliminate
  // floating point noise in edge labels (9.549999… → 9.55).
  const prec   = Math.max(0, Math.ceil(-Math.log10(binSize))) + 2;
  const snapFn = x => parseFloat(x.toFixed(prec));

  const counts = new Array(numBins).fill(0);
  // Add a small epsilon before floor() to fix IEEE 754 artefacts: e.g.
  // (9.59 - 9.58) / 0.01 = 0.9999999… → floor gives 0 (wrong).
  // Epsilon of 1e-9 is far smaller than any real measurement difference.
  const EPS = 1e-9;
  for (const v of values) {
    const idx = Math.min(numBins - 1, Math.floor((v - binStart) / binSize + EPS));
    if (idx >= 0) counts[idx]++;
  }
  const centers = [], edges0 = [], edges1 = [];
  for (let i = 0; i < numBins; i++) {
    const e0 = snapFn(binStart + i * binSize);
    edges0.push(e0);
    edges1.push(snapFn(e0 + binSize));
    centers.push(snapFn(e0 + binSize / 2));
  }
  return { centers, counts, edges0, edges1 };
}

// ── Time axis helpers ─────────────────────────────────────────────────────────
function formatTime(sec) {
  const h   = Math.floor(sec / 3600);
  const rem = sec - h * 3600;
  const m   = Math.floor(rem / 60);
  const s   = rem - m * 60;
  if (h > 0) {
    return `${h}:${String(m).padStart(2,'0')}:${String(Math.round(s)).padStart(2,'0')}`;
  }
  if (m > 0) {
    const si  = Math.floor(s);
    const dec = (s - si).toFixed(1).slice(1);
    return `${m}:${String(si).padStart(2,'0')}${dec}`;
  }
  return s.toFixed(2);
}

function makeTimeTicks(minV, maxV) {
  const steps = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 20, 30,
                 60, 90, 120, 300, 600, 900, 1800, 3600, 7200];
  const rawStep = (maxV - minV) / 8;
  const step    = steps.find(s => s >= rawStep) || steps[steps.length - 1];
  const start   = Math.ceil(minV / step) * step;
  const vals    = [];
  for (let v = start; v <= maxV + step * 0.5; v += step) {
    vals.push(Math.round(v * 10000) / 10000);
    if (vals.length > 20) break;
  }
  return { tickvals: vals, ticktext: vals.map(formatTime) };
}

// ── Fetch and render ──────────────────────────────────────────────────────────
async function updatePlot() {
  const slug    = eventSelect.value;
  if (!slug) return;

  const combos = buildCombos();
  if (!combos.length) {
    redraw();
    return;
  }

  statusEl.textContent = "Loading…";
  const params = new URLSearchParams({ event: slug });
  combos.forEach(c => params.append("combo", c));

  try {
    const resp = await fetch("/api/data?" + params);
    cachedData  = await resp.json();
  } catch (e) {
    statusEl.textContent = "Error fetching data.";
    return;
  }

  const allMarks = Object.values(cachedData.marks || {}).flat();
  if (!allMarks.length) {
    statusEl.textContent = "No data for this selection.";
    return;
  }
  currentUnit = cachedData.unit || "seconds";
  redraw();
}

// ── Pure redraw from cached data (used for control changes) ──────────────────
function redraw() {
  if (!cachedData) return;

  const marksData = cachedData.marks || {};

  // Apply max-athletes limit (data is ranked best-first)
  const maxN = parseInt(inputMaxAthletes.value) || Infinity;
  const combosWanted = buildCombos();

  const displayData = {};
  for (const c of combosWanted) {
    if (marksData[c] && marksData[c].length) {
      displayData[c] = isFinite(maxN) ? marksData[c].slice(0, maxN) : marksData[c];
    }
  }

  const allMarks = Object.values(displayData).flat();
  const hasData  = allMarks.length > 0;
  if (hasData) {
    currentRange = [Math.min(...allMarks), Math.max(...allMarks)];
  } else if (!currentRange) {
    return;
  }

  // Bin size — prefer the explicit bin size field when the user has set it,
  // otherwise derive from number-of-bins. This prevents precision loss from
  // the round-trip through integer numBins.
  let binSz, numBins;
  const explicitSize = parseFloat(inputBinSize.value);
  if (explicitSize > 0 && document.activeElement !== inputBins) {
    binSz   = explicitSize;
    numBins = Math.max(2, Math.ceil((currentRange[1] - currentRange[0]) / binSz));
  } else {
    numBins = Math.max(2, parseInt(inputBins.value) || 100);
    binSz   = (currentRange[1] - currentRange[0]) / numBins;
  }

  // Sync binsize field (unless user is actively editing it)
  if (document.activeElement !== inputBinSize) {
    inputBinSize.value = +binSz.toPrecision(4);
  }

  // Unit label
  const unitStr = currentUnit === "seconds" ? "s"
                : currentUnit === "metres"  ? "m"
                : "pts";
  unitLabel.textContent = `(${unitStr})`;

  // Build traces
  const normalize = chkNormalize.checked;
  // Snap bin start to the nearest multiple of binSz below the minimum,
  // so all bin edges are integer multiples of the bin size.
  const binStart  = Math.floor(currentRange[0] / binSz) * binSz;
  const traces = combosWanted
    .filter(c => displayData[c] && displayData[c].length)
    .map(c => {
      const hist   = computeHistogram(displayData[c], binStart, binSz, numBins);
      const N      = displayData[c].length;
      const yVals  = normalize ? hist.counts.map(k => k / N) : hist.counts;
      const label  = LABELS[c] || c;
      const hoverRanges = hist.edges0.map((e0, i) => {
        const e1 = hist.edges1[i];
        return currentUnit === "seconds"
          ? `${formatTime(e0)} \u2013 ${formatTime(e1)}`
          : `${e0.toFixed(2)} \u2013 ${e1.toFixed(2)}`;
      });
      return {
        x:             hist.centers,
        y:             yVals,
        type:          "bar",
        name:          `${label}, N\u202f=\u202f${N.toLocaleString()}`,
        width:         binSz,
        customdata:    hoverRanges,
        hovertemplate: normalize
          ? "%{customdata}<br>fraction: %{y:.4f}<extra>%{fullData.name}</extra>"
          : "%{customdata}<br>athletes: %{y:d}<extra>%{fullData.name}</extra>",
        marker:        { color: COLOURS[c] || "rgba(255,255,255,.6)" },
        opacity:       0.78,
      };
    });

  const xLabel = currentUnit === "seconds" ? "Performance (seconds)"
               : currentUnit === "metres"  ? "Performance (metres)"
               : "Score (points)";

  // Time tick formatting for running events
  let xAxisExtra = {};
  if (currentUnit === "seconds" && currentRange[1] > currentRange[0]) {
    const ticks = makeTimeTicks(currentRange[0], currentRange[1]);
    xAxisExtra = { tickvals: ticks.tickvals, ticktext: ticks.ticktext, tickmode: "array" };
  }

  // Log Y tick formatting (mirrors section 3 behaviour)
  const s1AllY = traces.flatMap(t => t.y || []).filter(v => v > 0);
  const s1YMax = s1AllY.length ? Math.max(...s1AllY) : 1;
  const s1YMin = s1AllY.length ? Math.min(...s1AllY) : s1YMax * 1e-4;
  let s1yaxisExtra = {};
  if (chkLogY.checked && s1YMin > 0) {
    const tk = makeCountLogTicks(s1YMin, s1YMax);
    s1yaxisExtra = {
      tickmode: "array", tickvals: tk.tickvals, ticktext: tk.ticktext,
      range: [Math.log10(s1YMin * 0.5), Math.log10(s1YMax) + 0.1],
    };
  }

  const layout = {
    ...DARK,
    font:   { family: '"Courier New", monospace', color: "#fff", size: 13 },
    title:  { text: cachedData.event || "", font: { size: 15 }, x: 0.04 },
    margin: { t: 44, b: 56, l: 62, r: 18 },
    barmode: "overlay",
    bargap:  0,
    xaxis: {
      ...AXIS_BASE,
      ...xAxisExtra,
      title: { ...AXIS_BASE.title, text: xLabel },
      type:  "linear",
    },
    yaxis: {
      ...AXIS_BASE,
      ...s1yaxisExtra,
      title: { ...AXIS_BASE.title, text: chkNormalize.checked ? "Fraction of athletes" : "Number of athletes" },
      type:  chkLogY.checked ? "log" : "linear",
    },
    legend: {
      font:        { family: '"Courier New", monospace', size: 13 },
      bgcolor:     "rgba(0,0,0,.45)",
      bordercolor: "rgba(255,255,255,.25)",
      borderwidth: 1,
    },
  };

  const config = {
    displayModeBar:          true,
    modeBarButtonsToRemove:  ["select2d", "lasso2d"],
    displaylogo:             false,
    responsive:              true,
  };

  Plotly.react("plot", traces, layout, config);

  if (hasData) {
    const total = Object.values(displayData).reduce((s, a) => s + a.length, 0);
    statusEl.textContent =
      `${total.toLocaleString()} athletes · bin size ${+binSz.toPrecision(4)} ${unitStr}`;
  } else {
    statusEl.textContent = "";
  }
}

// ── Listeners ─────────────────────────────────────────────────────────────────
eventSelect.addEventListener("change",       updatePlot);
chkMen.addEventListener("change",            updatePlot);
chkWomen.addEventListener("change",          updatePlot);chkSenior.addEventListener("change",         updatePlot);
chkU20.addEventListener("change",            updatePlot);
chkU18.addEventListener("change",            updatePlot);chkLogY.addEventListener("change",           redraw);
chkNormalize.addEventListener("change",      redraw);
inputBins.addEventListener("input",          onBinsChanged);
inputBinSize.addEventListener("input",       onBinSizeChanged);
inputMaxAthletes.addEventListener("input",   scheduleRedraw);

// ── Startup ───────────────────────────────────────────────────────────────────
loadEvents();

// ── Section 2: Pace vs Distance ───────────────────────────────────────────────
const s2ChkMen        = document.getElementById("s2-chk-men");
const s2ChkWomen      = document.getElementById("s2-chk-women");
const s2ChkSenior     = document.getElementById("s2-chk-senior");
const s2ChkU20        = document.getElementById("s2-chk-u20");
const s2ChkU18        = document.getElementById("s2-chk-u18");
const s2ChkLogX       = document.getElementById("s2-chk-logx");
const s2ChkTrendline  = document.getElementById("s2-chk-trendline");
const s2ChkStandard   = document.getElementById("s2-chk-standard");
const s2ChkShort      = document.getElementById("s2-chk-short");
const s2InputTopN     = document.getElementById("s2-input-topn");
const s2StatusEl      = document.getElementById("s2-status");
let   s2LastLayout    = null;

function buildS2Combos() {
  const genders = [];
  if (s2ChkMen.checked)   genders.push("men");
  if (s2ChkWomen.checked) genders.push("women");
  const ages = [];
  if (s2ChkSenior.checked) ages.push("senior");
  if (s2ChkU20.checked)    ages.push("u20");
  if (s2ChkU18.checked)    ages.push("u18");
  const tracks = [];
  if (s2ChkStandard.checked) tracks.push("standard");
  if (s2ChkShort.checked)    tracks.push("short");
  const combos = [];
  for (const g of genders)
    for (const a of ages)
      for (const t of tracks) {
        const base = a === "senior" ? g : `${g}-${a}`;
        combos.push(`${base}-${t}`);
      }
  return combos;
}

function formatPace(secPerKm) {
  const m = Math.floor(secPerKm / 60);
  const s = secPerKm - m * 60;
  const si  = Math.floor(s);
  const dec = (s - si).toFixed(1).slice(1);
  return `${m}:${String(si).padStart(2, "0")}${dec}/km`;
}

function formatPaceTick(secPerKm) {
  // Like formatPace but rounds to whole seconds (no decimal) for axis ticks
  const total = Math.round(secPerKm);
  const m = Math.floor(total / 60);
  const s = total - m * 60;
  return `${m}:${String(s).padStart(2, "0")}/km`;
}

function makePaceTicks(minV, maxV) {
  const steps = [5, 10, 15, 20, 30, 60, 120, 300];
  const rawStep = (maxV - minV) / 8;
  const step = steps.find(s => s >= rawStep) || steps[steps.length - 1];
  const start = Math.ceil(minV / step) * step;
  const vals = [];
  for (let v = start; v <= maxV + step * 0.5; v += step) {
    vals.push(Math.round(v * 100) / 100);
    if (vals.length > 20) break;
  }
  return { tickvals: vals, ticktext: vals.map(formatPaceTick) };
}

function gaussianDipFit(xs, ys) {
  // Fits: f(x) = a - b·exp(-((ln(x/c))²) / d²)   (inverted log-space Gaussian)
  // a ≈ 190  — asymptotic pace (s/km) for very long distances
  // b ≈ 90   — depth of the dip  (fastest pace = a-b, occurring near x=c)
  // c ≈ 150  — centre distance (m) where the dip minimum sits
  // d ≈ 1.5  — half-width of the dip in natural-log space
  // Solved by Levenberg-Marquardt nonlinear least squares.
  if (xs.length < 4) return null;
  let params = [190, 90, 150, 1.5];
  let lambda = 0.1;

  function evalModel(a, b, c, d, x) {
    const u = Math.log(x / c);
    return a - b * Math.exp(-(u * u) / (d * d));
  }
  function residuals([a, b, c, d]) {
    return xs.map((x, i) => ys[i] - evalModel(a, b, c, d, x));
  }
  function jacobian([a, b, c, d]) {
    return xs.map(x => {
      const u = Math.log(x / c);
      const e = Math.exp(-(u * u) / (d * d));
      return [
        1,                                  // ∂f/∂a
        -e,                                 // ∂f/∂b
        -b * e * 2 * u / (c * d * d),       // ∂f/∂c
        -b * e * 2 * u * u / (d * d * d),   // ∂f/∂d
      ];
    });
  }
  function sse(r) { return r.reduce((s, v) => s + v * v, 0); }

  for (let iter = 0; iter < 400; iter++) {
    const r = residuals(params);
    const J = jacobian(params);
    const nd = 4;
    const JtJ = Array.from({ length: nd }, () => new Array(nd).fill(0));
    const Jtr = new Array(nd).fill(0);
    for (let i = 0; i < xs.length; i++) {
      for (let a = 0; a < nd; a++) {
        Jtr[a] += J[i][a] * r[i];
        for (let b = 0; b < nd; b++) JtJ[a][b] += J[i][a] * J[i][b];
      }
    }
    // Augmented system: (J^T J + λ·diag(J^T J)) dp = J^T r
    const aug = JtJ.map((row, i) => {
      const nr = [...row];
      nr[i] += lambda * (JtJ[i][i] || 1);
      return [...nr, Jtr[i]];
    });
    // Gaussian elimination with partial pivoting
    for (let col = 0; col < nd; col++) {
      let mx = col;
      for (let row = col + 1; row < nd; row++)
        if (Math.abs(aug[row][col]) > Math.abs(aug[mx][col])) mx = row;
      [aug[col], aug[mx]] = [aug[mx], aug[col]];
      if (Math.abs(aug[col][col]) < 1e-14) { lambda *= 10; continue; }
      for (let row = col + 1; row < nd; row++) {
        const f = aug[row][col] / aug[col][col];
        for (let j = col; j <= nd; j++) aug[row][j] -= f * aug[col][j];
      }
    }
    const dp = new Array(nd).fill(0);
    for (let i = nd - 1; i >= 0; i--) {
      dp[i] = aug[i][nd];
      for (let j = i + 1; j < nd; j++) dp[i] -= aug[i][j] * dp[j];
      dp[i] /= aug[i][i] || 1;
    }
    const np = [
      Math.max(50,  params[0] + dp[0]),  // a ≥ 50
      Math.max(0,   params[1] + dp[1]),  // b ≥ 0
      Math.max(30,  params[2] + dp[2]),  // c ≥ 30 m
      Math.max(0.1, params[3] + dp[3]),  // d ≥ 0.1
    ];
    if (sse(residuals(np)) < sse(r)) {
      params = np;
      lambda = Math.max(1e-7, lambda / 5);
    } else {
      lambda = Math.min(1e7, lambda * 5);
    }
  }
  return params; // [a, b, c, d]
}

async function updatePacePlot() {
  const combos = buildS2Combos();
  if (!combos.length) {
    if (s2LastLayout) Plotly.react("pace-plot", [], s2LastLayout,
      { displayModeBar: true, modeBarButtonsToRemove: ["select2d","lasso2d"], displaylogo: false, responsive: true });
    s2StatusEl.textContent = "";
    return;
  }
  const topN    = Math.max(1, parseInt(s2InputTopN.value) || 10);

  s2StatusEl.textContent = "Loading\u2026";
  const params = new URLSearchParams({ top_n: topN });
  combos.forEach(c => params.append("combo", c));

  let data;
  try {
    const resp = await fetch("/api/pace?" + params);
    data = await resp.json();
  } catch (e) {
    s2StatusEl.textContent = "Error fetching data.";
    return;
  }

  const series  = data.series || {};
  const logX    = s2ChkLogX.checked;
  const showTrend = s2ChkTrendline.checked;
  const traces  = [];
  const allPaces = [];

  for (const combo of combos) {
    const pts = series[combo];
    if (!pts || !pts.length) continue;

    allPaces.push(...pts.map(p => p.pace));
    const colour = comboColour(combo);
    const label  = comboLabel(combo);
    const isShort = comboIsShort(combo);

    traces.push({
      x:    pts.map(p => p.distance),
      y:    pts.map(p => p.pace),
      mode: "markers",
      type: "scatter",
      name: label,
      customdata: pts.map(p =>
        `${p.event}<br>dist: ${p.distance >= 1000
          ? (p.distance / 1000).toFixed(p.distance % 1000 === 0 ? 0 : 1) + " km"
          : p.distance + " m"
        }<br>N = ${p.n}<br>pace: ${formatPace(p.pace)}`),
      hovertemplate: "%{customdata}<extra>%{fullData.name}</extra>",
      marker: {
        color:  colour,
        size:   isShort ? 9 : 10,
        symbol: isShort ? "circle-open" : "circle",
        line:   { color: "rgba(255,255,255,.35)", width: 1 },
      },
    });

    const xs = pts.map(p => p.distance);
    const ys = pts.map(p => p.pace);
    if (showTrend && xs.length >= 4) {
      const fit = gaussianDipFit(xs, ys);
      if (fit) {
        const [fa, fb, fc, fd] = fit;
        const steps  = 300;
        const minD   = Math.min(...xs) * 0.7;
        const maxD   = Math.max(...xs) * 1.3;
        const trendX = Array.from({ length: steps + 1 }, (_, i) =>
          Math.exp(Math.log(minD) + i * (Math.log(maxD) - Math.log(minD)) / steps));
        const trendY = trendX.map(x => {
          const u = Math.log(x / fc);
          return fa - fb * Math.exp(-(u * u) / (fd * fd));
        });
        traces.push({
          x: trendX, y: trendY,
          mode: "lines", type: "scatter",
          name: `${label} trend`,
          showlegend: false,
          hoverinfo: "skip",
          line: { color: colour, width: 1.5, dash: isShort ? "dashdot" : "dot" },
        });
      }
    }
  }

  if (!traces.length) {
    if (s2LastLayout) Plotly.react("pace-plot", [], s2LastLayout,
      { displayModeBar: true, modeBarButtonsToRemove: ["select2d","lasso2d"], displaylogo: false, responsive: true });
    s2StatusEl.textContent = "";
    return;
  }

  const paceMin = Math.min(...allPaces);
  const paceMax = Math.max(...allPaces);
  const ticks   = makePaceTicks(paceMin * 0.97, paceMax * 1.03);

  const layout = {
    ...DARK,
    font:   { family: '"Courier New", monospace', color: "#fff", size: 13 },
    title:  { text: "Pace vs. Distance", font: { size: 15 }, x: 0.04 },
    margin: { t: 44, b: 56, l: 88, r: 18 },
    xaxis: (() => {
      const allDists = combos.flatMap(c => (series[c] || []).map(p => p.distance));
      const minD = Math.min(...allDists);
      const maxD = Math.max(...allDists);
      const DIST_TICKS = [50,100,200,500,1000,2000,5000,10000,20000,50000,100000];
      const distTickVals = DIST_TICKS.filter(d => d >= minD * 0.5 && d <= maxD * 2);
      const distTickText = distTickVals.map(d =>
        d >= 10000 ? (d/1000) + "k" : d >= 1000 ? (d/1000) + "k" : String(d));
      return {
        ...AXIS_BASE,
        title:    { ...AXIS_BASE.title, text: "Distance (m)" },
        type:     logX ? "log" : "linear",
        ...(logX && distTickVals.length ? {
          tickmode: "array",
          tickvals: distTickVals,
          ticktext: distTickText,
        } : {}),
      };
    })(),
    yaxis: {
      ...AXIS_BASE,
      title:    { ...AXIS_BASE.title, text: "Pace (min/km)" },
      tickvals: ticks.tickvals,
      ticktext: ticks.ticktext,
      tickmode: "array",
      range:    [paceMax * 1.03, paceMin * 0.97],
    },
    legend: {
      font:        { family: '"Courier New", monospace', size: 13 },
      bgcolor:     "rgba(0,0,0,.45)",
      bordercolor: "rgba(255,255,255,.25)",
      borderwidth: 1,
    },
  };

  const config = {
    displayModeBar:         true,
    modeBarButtonsToRemove: ["select2d", "lasso2d"],
    displaylogo:            false,
    responsive:             true,
  };

  s2LastLayout = layout;
  Plotly.react("pace-plot", traces, layout, config);
  s2StatusEl.textContent =
    `${combos.filter(c => series[c] && series[c].length).length} series \u00b7`
    + ` top ${topN} athletes per event`;
}

[s2ChkMen, s2ChkWomen, s2ChkSenior, s2ChkU20, s2ChkU18,
 s2ChkLogX, s2ChkTrendline, s2ChkStandard, s2ChkShort].forEach(el =>
  el.addEventListener("change", updatePacePlot));
s2InputTopN.addEventListener("input", () => {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(updatePacePlot, 400);
});

updatePacePlot();

// ── Section 3: Peak Age Distribution ─────────────────────────────────────────
const s3EventSelect      = document.getElementById("s3-event-select");
const s3ChkMen           = document.getElementById("s3-chk-men");
const s3ChkWomen         = document.getElementById("s3-chk-women");
const s3ChkLogY          = document.getElementById("s3-chk-logy");
const s3ChkNormalize     = document.getElementById("s3-chk-normalize");
const s3ChkCumulative    = document.getElementById("s3-chk-cumulative");
const s3InputBins        = document.getElementById("s3-input-bins");
const s3InputBinSize     = document.getElementById("s3-input-binsize");
const s3InputMaxAthletes = document.getElementById("s3-input-max-athletes");
const s3StatusEl         = document.getElementById("s3-status");
const s3ChkGaussian      = document.getElementById("s3-chk-gaussian");

let s3CurrentRange  = null;
let s3CachedData    = null;
let s3DebounceTimer = null;

function buildS3Combos() {
  const combos = [];
  if (s3ChkMen.checked)   combos.push("men");
  if (s3ChkWomen.checked) combos.push("women");
  return combos;
}

async function updateAgePlot() {
  const slug = s3EventSelect.value;
  if (!slug) return;

  const combos = buildS3Combos();
  if (!combos.length) {
    redrawAge();
    return;
  }

  s3StatusEl.textContent = "Loading\u2026";
  const params = new URLSearchParams({ event: slug });
  combos.forEach(c => params.append("combo", c));

  try {
    const resp  = await fetch("/api/age?" + params);
    s3CachedData = await resp.json();
  } catch (e) {
    s3StatusEl.textContent = "Error fetching data.";
    return;
  }

  redrawAge();
}

// ── Normal CDF (needed for cumulative Gaussian) ─────────────────────────────
function erf(x) {
  // Abramowitz & Stegun 7.1.26, max error < 1.5e-7
  const t = 1 / (1 + 0.3275911 * Math.abs(x));
  const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741)
            * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-x * x);
  return x >= 0 ? y : -y;
}
function normalCDF(x, mu, sigma) {
  return 0.5 * (1 + erf((x - mu) / (sigma * Math.SQRT2)));
}

// Returns {tickvals, ticktext} for a log y-axis using a clean 1-2-5 sequence.
function makeCountLogTicks(minY, maxY) {
  if (minY <= 0) minY = maxY * 1e-4 || 1e-9;
  const lo = Math.floor(Math.log10(minY) - 0.01);
  const hi = Math.ceil(Math.log10(maxY)  + 0.01);
  const vals = [], txts = [];
  for (let e = lo; e <= hi; e++) {
    for (const m of [1, 2, 5]) {
      const v = m * 10 ** e;
      if (v >= minY * 0.49 && v <= maxY * 2.1) {
        vals.push(v);
        txts.push(String(+v.toPrecision(2)));
      }
    }
  }
  return { tickvals: vals, ticktext: txts };
}

// Fits f(x) = A·exp(-½·((x−μ)/σ)²) by Levenberg-Marquardt.
// Returns {A, mu, sigma, fwhm, r2} or null.
function fitGaussian1D(xs, ys) {
  const n = xs.length;
  if (n < 4) return null;
  const sumY = ys.reduce((a, b) => a + b, 0);
  if (sumY <= 0) return null;
  const mu0  = xs.reduce((s, x, i) => s + x * ys[i], 0) / sumY;
  const var0 = xs.reduce((s, x, i) => s + (x - mu0) ** 2 * ys[i], 0) / sumY;
  let params = [Math.max(...ys), mu0, Math.sqrt(Math.max(var0, 0.05))];
  let lambda = 1e-3;
  const evalF = ([A, mu, sg], x) => { const z = (x - mu) / sg; return A * Math.exp(-0.5 * z * z); };
  const residuals = p => xs.map((x, i) => ys[i] - evalF(p, x));
  const jacobian  = ([A, mu, sg]) => xs.map(x => {
    const z = (x - mu) / sg, e = Math.exp(-0.5 * z * z);
    return [e, A * e * z / sg, A * e * z * z / sg];
  });
  const sse = r => r.reduce((s, v) => s + v * v, 0);
  for (let iter = 0; iter < 300; iter++) {
    const r = residuals(params), Jrows = jacobian(params), nd = 3;
    const JtJ = Array.from({length: nd}, () => new Array(nd).fill(0));
    const Jtr = new Array(nd).fill(0);
    for (let i = 0; i < n; i++) {
      for (let a = 0; a < nd; a++) {
        Jtr[a] += Jrows[i][a] * r[i];
        for (let b = 0; b < nd; b++) JtJ[a][b] += Jrows[i][a] * Jrows[i][b];
      }
    }
    const aug = JtJ.map((row, i) => { const nr = [...row, Jtr[i]]; nr[i] += lambda * (JtJ[i][i] || 1); return nr; });
    for (let col = 0; col < nd; col++) {
      let mx = col;
      for (let row = col + 1; row < nd; row++)
        if (Math.abs(aug[row][col]) > Math.abs(aug[mx][col])) mx = row;
      [aug[col], aug[mx]] = [aug[mx], aug[col]];
      if (Math.abs(aug[col][col]) < 1e-15) { lambda *= 10; continue; }
      for (let row = col + 1; row < nd; row++) {
        const f = aug[row][col] / aug[col][col];
        for (let j = col; j <= nd; j++) aug[row][j] -= f * aug[col][j];
      }
    }
    const dp = new Array(nd).fill(0);
    for (let i = nd - 1; i >= 0; i--) {
      dp[i] = aug[i][nd];
      for (let j = i + 1; j < nd; j++) dp[i] -= aug[i][j] * dp[j];
      dp[i] /= aug[i][i] || 1;
    }
    const np = [
      Math.max(1e-12, params[0] + dp[0]),
      params[1] + dp[1],
      Math.max(0.05, Math.abs(params[2] + dp[2])),
    ];
    if (sse(residuals(np)) < sse(r)) { params = np; lambda = Math.max(1e-9, lambda / 5); }
    else lambda = Math.min(1e8, lambda * 5);
  }
  const [A, mu, sigma] = params;
  const fwhm   = 2 * Math.sqrt(2 * Math.log(2)) * sigma;
  const yMean  = ys.reduce((s, v) => s + v, 0) / n;
  const ssTot  = ys.reduce((s, v) => s + (v - yMean) ** 2, 0);
  const ssRes  = ys.reduce((s, v, i) => s + (v - evalF(params, xs[i])) ** 2, 0);
  const r2     = ssTot > 1e-20 ? 1 - ssRes / ssTot : 0;
  return { A, mu, sigma, fwhm, r2 };
}

function redrawAge() {
  if (!s3CachedData) return;

  const agesData     = s3CachedData.ages || {};
  const maxN         = parseInt(s3InputMaxAthletes.value) || Infinity;
  const combosWanted = buildS3Combos();

  const displayData = {};
  for (const c of combosWanted) {
    if (agesData[c] && agesData[c].length)
      displayData[c] = isFinite(maxN) ? agesData[c].slice(0, maxN) : agesData[c];
  }

  const allAges = Object.values(displayData).flat();
  const hasData  = allAges.length > 0;
  if (hasData) {
    s3CurrentRange = [Math.min(...allAges), Math.max(...allAges)];
  } else if (!s3CurrentRange) {
    return;
  }

  // Prefer the binSize field when it has a valid value (e.g. the 0.5 default);
  // fall back to numBins field only when binSize is empty/zero.
  let binSz, numBins;
  const bsv = parseFloat(s3InputBinSize.value);
  if (bsv > 0 && document.activeElement !== s3InputBins) {
    binSz   = bsv;
    numBins = Math.max(2, Math.round((s3CurrentRange[1] - s3CurrentRange[0]) / binSz));
    s3InputBins.value = numBins;
  } else {
    numBins = Math.max(2, parseInt(s3InputBins.value) || 50);
    binSz   = (s3CurrentRange[1] - s3CurrentRange[0]) / numBins;
    if (document.activeElement !== s3InputBinSize)
      s3InputBinSize.value = +binSz.toPrecision(4);
  }

  const normalize   = s3ChkNormalize.checked;
  const cumulative  = s3ChkCumulative.checked;
  const gaussianOn  = s3ChkGaussian.checked;
  const binStart    = Math.floor(s3CurrentRange[0]);  // first bin edge on a whole year
  const shapes      = [];
  const annots      = [];
  const fitResults  = {};

  // X-axis right cutoff: furthest bin where any combo first reaches 0.999 cumulative fraction
  let cutoffX = binStart + numBins * binSz;  // default: full range
  {
    let maxCutoff = binStart;
    for (const c of combosWanted.filter(c => displayData[c] && displayData[c].length)) {
      const h = computeHistogram(displayData[c], binStart, binSz, numBins);
      const N = displayData[c].length;
      let acc = 0;
      for (let i = 0; i < h.counts.length; i++) {
        acc += h.counts[i];
        if (acc / N >= 0.999) {
          maxCutoff = Math.max(maxCutoff, binStart + (i + 1) * binSz);
          break;
        }
      }
    }
    if (maxCutoff > binStart) cutoffX = maxCutoff;
  }

  const barTraces = combosWanted
    .filter(c => displayData[c] && displayData[c].length)
    .map(c => {
      const hist  = computeHistogram(displayData[c], binStart, binSz, numBins);
      const N     = displayData[c].length;
      // rawYVals used for Gaussian fitting (always non-cumulative)
      const rawYVals = normalize ? hist.counts.map(k => k / N) : hist.counts;

      if (gaussianOn && !c.includes("u20") && !c.includes("u18")) {
        const fit = fitGaussian1D(hist.centers, rawYVals);
        const xEnd = binStart + numBins * binSz;
        if (fit && fit.r2 > 0 && fit.mu > binStart - binSz && fit.mu < xEnd + binSz)
          fitResults[c] = { ...fit, N, yTotal: rawYVals.reduce((a, b) => a + b, 0) };
      }

      // Apply cumulative after fit
      let yVals = rawYVals;
      if (cumulative) {
        let acc = 0;
        yVals = rawYVals.map(v => (acc += v, acc));
      }

      const label       = LABELS[c] || c;
      const hoverRanges = hist.edges0.map((e0, i) =>
        `${e0.toFixed(2)} \u2013 ${hist.edges1[i].toFixed(2)} yrs`);
      const hoverFmt    = cumulative
        ? (normalize ? "%{customdata}<br>cum. fraction: %{y:.4f}<extra>%{fullData.name}</extra>"
                     : "%{customdata}<br>cum. athletes: %{y:d}<extra>%{fullData.name}</extra>")
        : (normalize ? "%{customdata}<br>fraction: %{y:.4f}<extra>%{fullData.name}</extra>"
                     : "%{customdata}<br>athletes: %{y:d}<extra>%{fullData.name}</extra>");
      return {
        x:             hist.centers,
        y:             yVals,
        type:          "bar",
        name:          `${label}, N\u202f=\u202f${N.toLocaleString()}`,
        width:         binSz,
        customdata:    hoverRanges,
        hovertemplate: hoverFmt,
        marker:  { color: COLOURS[c] || "rgba(255,255,255,.6)" },
        opacity: 0.78,
      };
    });

  // Gaussian overlay
  const gaussTraces = [];
  const xMin = binStart;
  const xDense = Array.from({length: 300}, (_, i) => xMin + (cutoffX - xMin) * i / 299);
  let annIdx = 0;
  for (const [c, fit] of Object.entries(fitResults)) {
    const col   = COLOURS[c] || "rgba(255,255,255,0.8)";
    // Gaussian curve: PDF or CDF scaled to match histogram y-units
    // In cumulative mode scale by yTotal (= Σ rawYVals) so the CDF reaches
    // the same maximum as the cumulative bars regardless of normalisation.
    const evalG = cumulative
      ? x => fit.yTotal * normalCDF(x, fit.mu, fit.sigma)
      : x => { const z = (x - fit.mu) / fit.sigma; return fit.A * Math.exp(-0.5 * z * z); };

    // Gaussian curve
    gaussTraces.push({
      x: xDense, y: xDense.map(evalG),
      type: "scatter", mode: "lines",
      name: `${LABELS[c] || c} fit`, showlegend: false,
      line: { color: col, dash: "dash", width: 2 },
      hoverinfo: "skip",
    });

    // Stats annotation
    annots.push({
      xref: "paper", yref: "paper",
      x: 0.02, y: 0.98 - annIdx * 0.26,
      xanchor: "left", yanchor: "top",
      align: "left",
      text: `<b>${LABELS[c] || c}</b>` +
            `<br>\u03bc\u202f=\u202f${fit.mu.toFixed(2)}\u202fyr` +
            `<br>\u03c3\u202f=\u202f${fit.sigma.toFixed(2)}\u202fyr` +
            `<br>R\u00b2\u202f=\u202f${fit.r2.toFixed(3)}`,
      showarrow: false,
      font:       { family: '"Courier New", monospace', size: 12, color: "#fff" },
      bgcolor:    "rgba(0,0,0,0.55)",
      borderpad:  5,
      bordercolor: col,
      borderwidth: 1,
    });
    annIdx++;
  }

  const traces = [...barTraces, ...gaussTraces];

  // Y-axis range driven by bar data only — Gaussian tails must not affect scaling.
  const barYpos = barTraces.flatMap(t => (t.y || []).filter(v => v > 0));
  const yMax    = barYpos.length ? Math.max(...barYpos) : 1;
  const yMin    = barYpos.length ? Math.min(...barYpos) : 0;

  let yaxisExtra = {};
  if (s3ChkLogY.checked) {
    const tk = makeCountLogTicks(yMin, yMax);
    const logMax = Math.log10(yMax) + 0.1;
    const logMin = Math.log10(yMin * 0.5);
    yaxisExtra = {
      tickmode: "array", tickvals: tk.tickvals, ticktext: tk.ticktext,
      range: [logMin, logMax],
    };
  } else {
    yaxisExtra = { range: [0, yMax * 1.08] };
  }

  const layout = {
    ...DARK,
    font:    { family: '"Courier New", monospace', color: "#fff", size: 13 },
    title:   { text: s3CachedData.event || "", font: { size: 15 }, x: 0.04 },
    margin:  { t: 44, b: 56, l: 62, r: 18 },
    barmode: "overlay",
    bargap:  0,
    xaxis: {
      ...AXIS_BASE,
      title: { ...AXIS_BASE.title, text: "Age (years)" },
      type:  "linear",
      range: [xMin - binSz * 0.5, cutoffX + binSz * 0.5],
    },
    yaxis: {
      ...AXIS_BASE,
      ...yaxisExtra,
      title: { ...AXIS_BASE.title, text:
        cumulative
          ? (normalize ? "Cumulative fraction" : "Cumulative count")
          : (normalize ? "Fraction of athletes" : "Number of athletes") },
      type:  s3ChkLogY.checked ? "log" : "linear",
    },
    shapes,
    annotations: annots,
    legend: {
      font:        { family: '"Courier New", monospace', size: 13 },
      bgcolor:     "rgba(0,0,0,.45)",
      bordercolor: "rgba(255,255,255,.25)",
      borderwidth: 1,
    },
  };

  const config = {
    displayModeBar:         true,
    modeBarButtonsToRemove: ["select2d", "lasso2d"],
    displaylogo:            false,
    responsive:             true,
  };

  Plotly.react("age-plot", traces, layout, config);

  if (hasData) {
    const total = Object.values(displayData).reduce((s, a) => s + a.length, 0);
    s3StatusEl.textContent =
      `${total.toLocaleString()} athletes \u00b7 bin size ${+binSz.toPrecision(4)} yrs`;
  } else {
    s3StatusEl.textContent = "";
  }
}

function onS3BinsChanged() {
  if (s3CurrentRange !== null) {
    const n    = Math.max(2, parseInt(s3InputBins.value) || 50);
    const size = (s3CurrentRange[1] - s3CurrentRange[0]) / n;
    s3InputBinSize.value = +size.toPrecision(4);
  }
  clearTimeout(s3DebounceTimer);
  s3DebounceTimer = setTimeout(redrawAge, 250);
}

function onS3BinSizeChanged() {
  if (s3CurrentRange !== null) {
    const size = parseFloat(s3InputBinSize.value);
    if (size > 0) {
      const n = Math.round((s3CurrentRange[1] - s3CurrentRange[0]) / size);
      s3InputBins.value = Math.max(2, n);
    }
  }
  clearTimeout(s3DebounceTimer);
  s3DebounceTimer = setTimeout(redrawAge, 250);
}

s3EventSelect.addEventListener("change",      updateAgePlot);
s3ChkMen.addEventListener("change",           updateAgePlot);
s3ChkWomen.addEventListener("change",         updateAgePlot);
s3ChkLogY.addEventListener("change",          redrawAge);
s3ChkNormalize.addEventListener("change",     redrawAge);
s3ChkGaussian.addEventListener("change",      redrawAge);
s3ChkCumulative.addEventListener("change",    redrawAge);
s3InputBins.addEventListener("input",         onS3BinsChanged);
s3InputBinSize.addEventListener("input",      onS3BinSizeChanged);
s3InputMaxAthletes.addEventListener("input",  () => {
  clearTimeout(s3DebounceTimer);
  s3DebounceTimer = setTimeout(redrawAge, 250);
});

// ── Section 4: Age Trends by Event ───────────────────────────────────────────
const s4ChkMen      = document.getElementById("s4-chk-men");
const s4ChkWomen    = document.getElementById("s4-chk-women");
const s4ChkP50      = document.getElementById("s4-chk-p50");
const s4ChkIQR      = document.getElementById("s4-chk-iqr");
const s4ChkOuter    = document.getElementById("s4-chk-outer");
const s4ChkLogX     = document.getElementById("s4-chk-logx");
const s4ChkStandard = document.getElementById("s4-chk-standard");
const s4ChkShort    = document.getElementById("s4-chk-short");
const s4InputTopN   = document.getElementById("s4-input-topn");
const s4StatusEl    = document.getElementById("s4-status");
let   s4CachedData  = null;
let   s4LastLayout  = null;
let   s4DebounceTimer = null;

// Semi-transparent fill colours for bands (IQR and outer)
const FILL_COLOURS = {
  "men":       "rgba(70,  130, 220, 0.28)",
  "men-u20":   "rgba(80,  200, 240, 0.22)",
  "men-u18":   "rgba(120, 230, 200, 0.22)",
  "women":     "rgba(220,  55,  55, 0.30)",
  "women-u20": "rgba(235, 110, 180, 0.24)",
  "women-u18": "rgba(240, 170,  80, 0.24)",
};
const FILL_COLOURS_OUTER = {
  "men":       "rgba(70,  130, 220, 0.14)",
  "men-u20":   "rgba(80,  200, 240, 0.11)",
  "men-u18":   "rgba(120, 230, 200, 0.11)",
  "women":     "rgba(220,  55,  55, 0.18)",
  "women-u20": "rgba(235, 110, 180, 0.13)",
  "women-u18": "rgba(240, 170,  80, 0.13)",
};

function buildS4Combos() {
  const genders = [];
  if (s4ChkMen.checked)   genders.push("men");
  if (s4ChkWomen.checked) genders.push("women");
  const tracks = [];
  if (s4ChkStandard.checked) tracks.push("standard");
  if (s4ChkShort.checked)    tracks.push("short");
  const combos = [];
  for (const g of genders)
    for (const t of tracks)
      combos.push(`${g}-${t}`);
  return combos;
}

async function loadAgeStats() {
  const combos = buildS4Combos();
  const s4Config = {
    displayModeBar: true,
    modeBarButtonsToRemove: ["select2d", "lasso2d"],
    displaylogo: false,
    responsive: true,
  };
  if (!combos.length) {
    if (s4LastLayout) Plotly.react("age-trend-plot", [], s4LastLayout, s4Config);
    s4StatusEl.textContent = "";
    return;
  }

  s4StatusEl.textContent = "Loading\u2026";
  const topNVal = s4InputTopN.value.trim();
  const params  = new URLSearchParams();
  combos.forEach(c => params.append("combo", c));
  if (topNVal) params.set("top_n", topNVal);

  let data;
  try {
    const resp = await fetch("/api/age_stats?" + params);
    data = await resp.json();
  } catch (e) {
    s4StatusEl.textContent = "Error fetching data.";
    return;
  }

  s4CachedData = data;
  redrawAgeTrend();
}

function redrawAgeTrend() {
  if (!s4CachedData) return;

  const events   = s4CachedData.events || [];
  const combos   = buildS4Combos();
  const showP50  = s4ChkP50.checked;
  const showIQR   = s4ChkIQR.checked;
  const showOuter = s4ChkOuter.checked;
  const logX     = s4ChkLogX.checked;

  const s4Config = {
    displayModeBar: true,
    modeBarButtonsToRemove: ["select2d", "lasso2d"],
    displaylogo: false,
    responsive: true,
  };

  if (!combos.length || !events.length || (!showP50 && !showIQR && !showOuter)) {
    if (s4LastLayout) Plotly.react("age-trend-plot", [], s4LastLayout, s4Config);
    s4StatusEl.textContent = "";
    return;
  }

  const traces = [];

  for (const combo of combos) {
    const base     = comboBase(combo);
    const isShort  = comboIsShort(combo);
    const col      = COLOURS[base]            || "rgba(255,255,255,.6)";
    const fillCol  = FILL_COLOURS[base]       || "rgba(255,255,255,0.20)";
    const fillColO = FILL_COLOURS_OUTER[base] || "rgba(255,255,255,0.09)";
    const label    = comboLabel(combo);

    const evts = events.filter(e => e.combos[combo]);

    if (!evts.length) continue;

    const xs  = evts.map(e => e.dist_m);
    const get = field => evts.map(e => e.combos[combo][field]);

    const mkHover = arr => arr.map(e => {
      const s = e.combos[combo];
      return `${e.name}` +
             `<br>P50: ${s.p50.toFixed(1)}\u202fyr` +
             `<br>P25-P75: ${s.p25.toFixed(1)}\u2013${s.p75.toFixed(1)}\u202fyr` +
             `<br>P10-P90: ${s.p10.toFixed(1)}\u2013${s.p90.toFixed(1)}\u202fyr` +
             `<br>N\u202f=\u202f${s.n}`;
    });

    // Outer band P10–P90 as closed polygon ("toself")
    if (showOuter && evts.length >= 2) {
      const p10 = get("p10"), p90 = get("p90");
      traces.push({
        x: [...xs, ...xs.slice().reverse()],
        y: [...p10, ...p90.slice().reverse()],
        mode: "lines", type: "scatter",
        fill: "toself", fillcolor: fillColO,
        line: { color: "rgba(0,0,0,0)", width: 0 },
        name: `${label} P10\u2013P90`,
        showlegend: true, hoverinfo: "skip",
        legendgroup: `${combo}-outer`,
      });
    }

    // IQR band P25–P75 as closed polygon ("toself")
    if (showIQR && evts.length >= 2) {
      const p25 = get("p25"), p75 = get("p75");
      traces.push({
        x: [...xs, ...xs.slice().reverse()],
        y: [...p25, ...p75.slice().reverse()],
        mode: "lines", type: "scatter",
        fill: "toself", fillcolor: fillCol,
        line: { color: "rgba(0,0,0,0)", width: 0 },
        name: `${label} P25–P75`,
        showlegend: true, hoverinfo: "skip",
        legendgroup: `${combo}-iqr`,
      });
    }

    // Median (P50) line
    if (showP50 && evts.length) {
      traces.push({
        x: xs, y: get("p50"),
        mode: "lines+markers", type: "scatter",
        name: `${label} median`,
        line:   { color: col, width: 2, ...(isShort ? { dash: "dash" } : {}) },
        marker: { color: col, size: 6, symbol: isShort ? "circle-open" : "circle" },
        customdata: mkHover(evts),
        hovertemplate: "%{customdata}<extra>%{fullData.name}</extra>",
        legendgroup: `${combo}-p50`,
      });
    }
  }

  if (!traces.length) {
    if (s4LastLayout) Plotly.react("age-trend-plot", [], s4LastLayout, s4Config);
    s4StatusEl.textContent = "";
    return;
  }

  // X-axis identical to pace section
  const allDists = events
    .filter(e => combos.some(c => e.combos[c]))
    .map(e => e.dist_m);
  const minD = Math.min(...allDists);
  const maxD = Math.max(...allDists);
  const DIST_TICKS = [50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000];
  const distTickVals = DIST_TICKS.filter(d => d >= minD * 0.5 && d <= maxD * 2);
  const distTickText = distTickVals.map(d =>
    d >= 10000 ? (d / 1000) + "k" : d >= 1000 ? (d / 1000) + "k" : String(d));

  const nEvents = events.filter(e => combos.some(c => e.combos[c])).length;

  const layout = {
    ...DARK,
    font:    { family: '"Courier New", monospace', color: "#fff", size: 13 },
    title:   { text: "Age Trends by Event", font: { size: 15 }, x: 0.04 },
    margin:  { t: 44, b: 56, l: 62, r: 18 },
    xaxis: {
      ...AXIS_BASE,
      title: { ...AXIS_BASE.title, text: "Distance (m)" },
      type:  logX ? "log" : "linear",
      ...(logX && distTickVals.length ? {
        tickmode: "array",
        tickvals: distTickVals,
        ticktext: distTickText,
      } : {}),
    },
    yaxis: {
      ...AXIS_BASE,
      title: { ...AXIS_BASE.title, text: "Age (years)" },
    },
    legend: {
      font:        { family: '"Courier New", monospace', size: 13 },
      bgcolor:     "rgba(0,0,0,.45)",
      bordercolor: "rgba(255,255,255,.25)",
      borderwidth: 1,
    },
  };

  s4LastLayout = layout;
  Plotly.react("age-trend-plot", traces, layout, s4Config);
  s4StatusEl.textContent = `${nEvents} events`;
}

// Combo/metric toggles: redraw from cache (no re-fetch needed)
[s4ChkP50, s4ChkIQR, s4ChkOuter, s4ChkLogX].forEach(el =>
  el.addEventListener("change", redrawAgeTrend));

// Track type / combo changes: must re-fetch
[s4ChkMen, s4ChkWomen, s4ChkStandard, s4ChkShort].forEach(el =>
  el.addEventListener("change", loadAgeStats));

s4InputTopN.addEventListener("input", () => {
  clearTimeout(s4DebounceTimer);
  s4DebounceTimer = setTimeout(loadAgeStats, 400);
});

loadAgeStats();
</script>

</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML)


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5001)
