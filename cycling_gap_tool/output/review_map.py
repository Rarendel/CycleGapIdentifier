"""
review_map.py
Phase 2: Interactive gap review tool.

Generates a standalone HTML file that lets engineers review, dismiss,
and tag gaps before finalising the analysis for route design.

Features:
  - All gap data embedded in HTML — no server required
  - Click any gap to open a review panel
  - Dismiss gaps with a reason (data error / already planned / not feasible / out of scope)
  - Restore dismissed gaps at any time
  - Live rank recalculation after each dismiss/restore
  - Filter view by gap type, priority band, or status
  - Export finalised gaps as GeoJSON and CSV
  - Session state persisted in browser localStorage
  - Dismissed gaps saved to dismissed.json sidecar for re-run persistence

Dismiss reasons (aligned with common engineering review decisions):
  1. Data error          — OSM tagging artefact, false positive
  2. Already planned     — in municipal capital program or ATMP
  3. Not feasible        — ROW constraint, barrier, cost prohibitive
  4. Out of scope        — rural, outside study area, pedestrian only
  5. Custom note         — free text for anything else
"""

import json
import os
import csv
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

DISMISS_REASONS = [
    # False-positive categories — these map to tunable detection thresholds and
    # are aggregated by analyze_dismissals.py to guide calibration.
    "Same corridor (fragmentation)",
    "Parallel facility",
    "Already connected",
    "Data error",
    # Decision categories — valid gaps the reviewer is setting aside.
    "Already planned",
    "Not feasible",
    "Out of scope",
    "Other (see note)",
]

# Maps each false-positive reason to the threshold(s) a reviewer's choice
# implicates, so analyze_dismissals.py can suggest what to tune. Decision
# categories (already planned / not feasible / out of scope) are intentionally
# absent — they are not detection errors.
DISMISS_REASON_THRESHOLDS = {
    "Same corridor (fragmentation)": [
        "edge_snap_m (raise: dead-ends not snapping to the through-way)",
        "consolidate_degree2 (corridor still fragmented post-clean)",
    ],
    "Parallel facility": [
        "edge_snap_m (lower: separate facilities being bridged)",
        "ALREADY_CONNECTED_RATIO (raise: parallel sides scored as a gap)",
    ],
    "Already connected": [
        "ALREADY_CONNECTED_RATIO / ALREADY_CONNECTED_ABS_M (raise to suppress)",
    ],
    "Data error": [
        "upstream OSM tagging — not a tool threshold (review source data)",
    ],
}


def generate_review_map(
    scored_gaps: list,
    cycling_ways: list,
    region_name: str,
    output_path: str,
    dismissed_path: str = None,
    destinations: list = None,
    spines: list = None,
) -> str:
    """
    Generate the interactive review HTML map.

    Args:
      scored_gaps:    Output of score_all_gaps()
      cycling_ways:   Raw OSM cycling way dicts for network display
      region_name:    Display name for the region
      output_path:    Path to write the HTML file
      dismissed_path: Optional path to a dismissed.json sidecar file
                      from a previous session — pre-loads dismissed state
      destinations:   Optional destination points for display
      spines:         Optional spine markers for display
    """
    from output.report import (
        _gaps_to_geojson, _cycling_ways_to_geojson,
        _destinations_to_geojson, _spines_to_geojson,
        _priority_band, FACILITY_COLOURS, PRIORITY_COLOURS, DESTINATION_COLOURS
    )

    # Load any previously dismissed gaps
    previously_dismissed = {}
    if dismissed_path and os.path.exists(dismissed_path):
        try:
            with open(dismissed_path) as f:
                previously_dismissed = json.load(f)
            logger.info(f"Loaded {len(previously_dismissed)} previously dismissed gaps")
        except Exception as e:
            logger.warning(f"Could not load dismissed.json: {e}")

    # Ensure all required review fields are present on every gap
    # (score_gap output doesn't include these — add defaults here)
    for gap in scored_gaps:
        gid = gap.get("gap_id", "")
        if gid in previously_dismissed:
            gap["dismissed"] = True
            gap["dismiss_reason"] = previously_dismissed[gid].get("reason", "")
            gap["dismiss_note"] = previously_dismissed[gid].get("note", "")
        else:
            gap.setdefault("dismissed", False)
            gap.setdefault("dismiss_reason", "")
            gap.setdefault("dismiss_note", "")
        # Ensure scores dict always present (guard against missing field)
        gap.setdefault("scores", {
            "connectivity": 0, "buildability": 0,
            "destination": 0, "lts": 0, "equity": 0
        })
        # Ensure rank is always present
        gap.setdefault("rank", 0)

    # Serialise all data.
    # CRITICAL: when JSON is embedded inside an HTML <script> block, any literal
    # "</script>" in the data (e.g. an OSM name containing it) terminates the
    # script tag and breaks the page, and "<!--" / "<script" can enable
    # injection. Escaping "<", ">", and "&" to their \uXXXX forms keeps the JSON
    # semantically identical while making it impossible to break out of the
    # script context. This is the standard safe-embedding technique.
    def _safe_json(obj):
        return (json.dumps(obj)
                .replace("<", "\\u003c")
                .replace(">", "\\u003e")
                .replace("&", "\\u0026")
                .replace("\u2028", "\\u2028")   # JS line separator — illegal raw
                .replace("\u2029", "\\u2029"))  # JS paragraph separator

    gaps_json       = _safe_json(_gaps_to_geojson(scored_gaps))
    cycling_json    = _safe_json(_cycling_ways_to_geojson(cycling_ways))
    dest_json       = _safe_json(_destinations_to_geojson(destinations or []))
    spine_json      = _safe_json(_spines_to_geojson(spines or []))
    fac_colours     = _safe_json(FACILITY_COLOURS)
    pri_colours     = _safe_json(PRIORITY_COLOURS)
    dst_colours     = _safe_json(DESTINATION_COLOURS)
    dismiss_reasons = _safe_json(DISMISS_REASONS)

    # Centre map
    active = [g for g in scored_gaps if g.get("start_lat")]
    centre_lat = active[0]["start_lat"] if active else 43.48
    centre_lon = active[0]["start_lon"] if active else -80.52

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    total = len(scored_gaps)

    # region_name comes from user input / config and lands in three contexts:
    # HTML text (title, heading), and a JS string literal (STORAGE_KEY). Escape
    # for each so an apostrophe or angle bracket can't break the page.
    import html as _html_mod
    region_html = _html_mod.escape(str(region_name))
    region_js = (json.dumps(str(region_name))  # yields a quoted, JS-safe literal
                 .replace("<", "\\u003c").replace(">", "\\u003e"))
    # STORAGE_KEY needs a bare (unquoted) safe token; strip to alnum/_-.
    import re as _re_mod
    region_key = _re_mod.sub(r"[^A-Za-z0-9_-]", "_", str(region_name))
    # Filename slug for export downloads — lowercase, only safe chars.
    region_slug = _re_mod.sub(r"[^a-z0-9_-]", "_", str(region_name).lower()).strip("_") or "region"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Gap Review — {region_html}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  :root {{
    --bg:       #0d1117;
    --surface:  #161b22;
    --border:   #21262d;
    --text:     #c9d1d9;
    --muted:    #8b949e;
    --accent:   #58a6ff;
    --danger:   #e74c3c;
    --warning:  #f39c12;
    --success:  #2ecc71;
    --dismiss:  #6e7681;
    --magenta:  #e91e8c;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'SF Mono', 'Fira Code', monospace; background: var(--bg);
          color: var(--text); height: 100vh; overflow: hidden; }}

  /* Layout */
  #app {{ display: grid; grid-template-columns: 320px 1fr 380px; height: 100vh; }}
  #sidebar-left  {{ background: var(--surface); border-right: 1px solid var(--border);
                    display: flex; flex-direction: column; overflow: hidden; }}
  #map-container {{ position: relative; height: 100%; overflow: hidden; }}
  #map {{ width: 100%; height: 100%; min-height: 0; }}
  #panel-right {{ background: var(--surface); border-left: 1px solid var(--border);
                  display: flex; flex-direction: column; overflow: hidden; }}

  /* Header */
  .pane-header {{ padding: 14px 16px; border-bottom: 1px solid var(--border);
                  background: var(--bg); flex-shrink: 0; }}
  .pane-title {{ font-size: 0.7rem; font-weight: 700; letter-spacing: 0.08em;
                 text-transform: uppercase; color: var(--accent); }}
  .pane-sub {{ font-size: 0.68rem; color: var(--muted); margin-top: 2px; }}

  /* Stats bar */
  #stats-bar {{ display: grid; grid-template-columns: 1fr 1fr 1fr 1fr;
                padding: 10px 16px; gap: 8px; border-bottom: 1px solid var(--border);
                flex-shrink: 0; }}
  .stat {{ text-align: center; }}
  .stat-num {{ font-size: 1.2rem; font-weight: 700; }}
  .stat-num.active {{ color: var(--accent); }}
  .stat-num.dismissed {{ color: var(--dismiss); }}
  .stat-num.high {{ color: var(--danger); }}
  .stat-num.medium {{ color: var(--warning); }}
  .stat-label {{ font-size: 0.58rem; color: var(--muted); text-transform: uppercase;
                 letter-spacing: 0.06em; margin-top: 1px; }}

  /* Filter bar */
  #filter-bar {{ padding: 8px 12px; border-bottom: 1px solid var(--border);
                 display: flex; gap: 6px; flex-wrap: wrap; flex-shrink: 0; }}
  .filter-btn {{ font-size: 0.62rem; padding: 3px 8px; border-radius: 3px;
                 border: 1px solid var(--border); background: transparent;
                 color: var(--muted); cursor: pointer; font-family: inherit;
                 text-transform: uppercase; letter-spacing: 0.04em; transition: all 0.15s; }}
  .filter-btn:hover {{ border-color: var(--accent); color: var(--accent); }}
  .filter-btn.active {{ background: var(--accent); color: var(--bg);
                        border-color: var(--accent); }}

  /* Gap list */
  #gap-list {{ overflow-y: auto; flex: 1; }}
  .gap-item {{ padding: 10px 16px; border-bottom: 1px solid var(--border);
               cursor: pointer; transition: background 0.1s; position: relative; }}
  .gap-item:hover {{ background: rgba(88,166,255,0.05); }}
  .gap-item.selected {{ background: rgba(88,166,255,0.1);
                        border-left: 2px solid var(--accent); }}
  .gap-item.dismissed-item {{ opacity: 0.4; }}
  .gap-item.dismissed-item:hover {{ opacity: 0.7; }}
  .gap-rank {{ font-size: 0.6rem; color: var(--muted); text-transform: uppercase;
               letter-spacing: 0.06em; }}
  .gap-score-row {{ display: flex; justify-content: space-between; align-items: center;
                    margin-top: 2px; }}
  .gap-score-val {{ font-size: 1.1rem; font-weight: 700; }}
  .gap-score-val.high {{ color: var(--danger); }}
  .gap-score-val.medium {{ color: var(--warning); }}
  .gap-score-val.low {{ color: #f1c40f; }}
  .gap-score-val.dismissed-score {{ color: var(--dismiss); }}
  .gap-type-badge {{ font-size: 0.55rem; padding: 2px 5px; border-radius: 2px;
                     text-transform: uppercase; letter-spacing: 0.05em; font-weight: 700; }}
  .badge-island   {{ background: rgba(88,166,255,0.15); color: var(--accent); }}
  .badge-detour   {{ background: rgba(243,156,18,0.15); color: var(--warning); }}
  .badge-dangling {{ background: rgba(46,204,113,0.15); color: var(--success); }}
  .badge-corridor {{ background: rgba(233,30,140,0.15); color: var(--magenta); }}
  .badge-connector {{ background: rgba(155,89,182,0.18); color: #b388e0; }}
  .badge-dismissed {{ background: rgba(110,118,129,0.2); color: var(--dismiss); }}
  .gap-streets {{ font-size: 0.65rem; color: var(--muted); margin-top: 3px;
                  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .gap-length {{ font-size: 0.62rem; color: var(--muted); }}
  .dismiss-tag {{ font-size: 0.6rem; color: var(--dismiss); margin-top: 2px; }}

  /* Right panel — review */
  #review-empty {{ flex: 1; display: flex; align-items: center; justify-content: center;
                   flex-direction: column; gap: 8px; color: var(--muted); }}
  #review-empty .icon {{ font-size: 2rem; opacity: 0.3; }}
  #review-content {{ flex: 1; overflow-y: auto; padding: 16px; display: none; }}

  .review-gap-id {{ font-size: 0.7rem; color: var(--accent); font-weight: 700;
                    text-transform: uppercase; letter-spacing: 0.08em; }}
  .review-street {{ font-size: 1rem; font-weight: 600; margin-top: 4px; color: var(--text); }}
  .review-length {{ font-size: 0.72rem; color: var(--muted); margin-top: 2px; }}

  .score-display {{ margin-top: 16px; padding: 12px; background: var(--bg);
                    border-radius: 6px; border: 1px solid var(--border); }}
  .score-big {{ font-size: 2.4rem; font-weight: 700; }}
  .score-big.high {{ color: var(--danger); }}
  .score-big.medium {{ color: var(--warning); }}
  .score-big.low {{ color: #f1c40f; }}
  .score-rank {{ font-size: 0.68rem; color: var(--muted); margin-top: 2px; }}

  .score-bars {{ margin-top: 12px; display: flex; flex-direction: column; gap: 6px; }}
  .score-bar-row {{ display: flex; align-items: center; gap: 8px; }}
  .score-bar-label {{ font-size: 0.62rem; color: var(--muted); width: 90px;
                      text-transform: uppercase; letter-spacing: 0.04em; flex-shrink: 0; }}
  .score-bar-track {{ flex: 1; height: 4px; background: var(--border); border-radius: 2px; }}
  .score-bar-fill {{ height: 100%; border-radius: 2px; background: var(--accent);
                     transition: width 0.4s ease; }}
  .score-bar-val {{ font-size: 0.62rem; color: var(--muted); width: 28px; text-align: right;
                    flex-shrink: 0; }}

  .review-section {{ margin-top: 14px; }}
  .review-section-title {{ font-size: 0.62rem; color: var(--muted); text-transform: uppercase;
                            letter-spacing: 0.08em; border-bottom: 1px solid var(--border);
                            padding-bottom: 4px; margin-bottom: 8px; }}
  .review-row {{ display: flex; justify-content: space-between; font-size: 0.72rem;
                 padding: 3px 0; }}
  .review-row-label {{ color: var(--muted); }}
  .review-row-val {{ color: var(--text); font-weight: 500; text-align: right;
                     max-width: 60%; }}

  .notes-list {{ font-size: 0.68rem; color: var(--muted); margin-top: 4px; }}
  .notes-list li {{ padding: 2px 0; list-style: none; padding-left: 12px;
                    position: relative; }}
  .notes-list li::before {{ content: '⚠'; position: absolute; left: 0; font-size: 0.6rem; }}

  /* Dismiss panel */
  #dismiss-panel {{ margin-top: 16px; padding: 12px; background: var(--bg);
                    border-radius: 6px; border: 1px solid var(--border); }}
  .dismiss-title {{ font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.08em;
                    color: var(--muted); margin-bottom: 8px; }}
  .reason-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-bottom: 8px; }}
  .reason-btn {{ font-size: 0.62rem; padding: 6px 8px; border-radius: 4px;
                 border: 1px solid var(--border); background: transparent;
                 color: var(--text); cursor: pointer; font-family: inherit;
                 text-align: left; transition: all 0.15s; line-height: 1.3; }}
  .reason-btn:hover {{ border-color: var(--danger); color: var(--danger); }}
  .reason-btn.selected {{ background: rgba(231,76,60,0.15); border-color: var(--danger);
                          color: var(--danger); }}
  #dismiss-note {{ width: 100%; background: var(--border); border: 1px solid var(--border);
                   border-radius: 4px; padding: 6px 8px; color: var(--text); font-size: 0.7rem;
                   font-family: inherit; resize: vertical; min-height: 50px;
                   margin-bottom: 8px; }}
  #dismiss-note:focus {{ outline: none; border-color: var(--accent); }}
  #dismiss-note::placeholder {{ color: var(--muted); }}
  .action-row {{ display: flex; gap: 8px; }}
  .btn {{ font-size: 0.68rem; padding: 7px 14px; border-radius: 4px; cursor: pointer;
          font-family: inherit; font-weight: 600; text-transform: uppercase;
          letter-spacing: 0.05em; border: none; transition: all 0.15s; }}
  .btn-dismiss {{ background: var(--danger); color: white; flex: 1; }}
  .btn-dismiss:hover {{ background: #c0392b; }}
  .btn-dismiss:disabled {{ opacity: 0.4; cursor: not-allowed; }}
  .btn-restore {{ background: rgba(46,204,113,0.15); color: var(--success);
                  border: 1px solid var(--success); flex: 1; }}
  .btn-restore:hover {{ background: rgba(46,204,113,0.3); }}
  .dismissed-badge {{ background: rgba(110,118,129,0.2); color: var(--dismiss);
                      padding: 6px 12px; border-radius: 4px; font-size: 0.68rem;
                      text-align: center; margin-bottom: 8px; }}

  /* Map overlay */
  #map-header {{ position: absolute; top: 12px; left: 50%; transform: translateX(-50%);
                 z-index: 1000; background: rgba(13,17,23,0.9); backdrop-filter: blur(8px);
                 padding: 6px 14px; border-radius: 20px; border: 1px solid var(--border);
                 font-size: 0.68rem; color: var(--muted); white-space: nowrap; }}
  #map-header span {{ color: var(--accent); font-weight: 700; }}

  /* Export bar */
  #export-bar {{ padding: 10px 16px; border-top: 1px solid var(--border);
                 display: flex; gap: 8px; flex-shrink: 0; }}
  .btn-export {{ background: rgba(88,166,255,0.1); color: var(--accent);
                 border: 1px solid rgba(88,166,255,0.3); flex: 1; }}
  .btn-export:hover {{ background: rgba(88,166,255,0.2); }}

  /* Legend */
  #map-legend {{ position: absolute; bottom: 24px; left: 12px; z-index: 1000;
                 background: rgba(13,17,23,0.92); backdrop-filter: blur(6px);
                 padding: 10px 12px; border-radius: 6px; border: 1px solid var(--border);
                 font-size: 0.62rem; color: var(--muted); min-width: 140px; }}
  .leg-title {{ color: var(--muted); text-transform: uppercase; letter-spacing: 0.07em;
                font-size: 0.58rem; margin-bottom: 6px; }}
  .leg-row {{ display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }}
  .leg-dot {{ width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }}
  .leg-line {{ width: 16px; height: 3px; border-radius: 1px; flex-shrink: 0; }}

  /* Scrollbar */
  ::-webkit-scrollbar {{ width: 4px; }}
  ::-webkit-scrollbar-track {{ background: transparent; }}
  ::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 2px; }}

  .leaflet-control-layers {{
    background: rgba(13,17,23,0.92) !important;
    color: var(--text) !important;
    border: 1px solid var(--border) !important;
    font-size: 0.68rem !important;
    font-family: inherit !important;
  }}
</style>
</head>
<body>
<div id="app">

  <!-- LEFT SIDEBAR: gap list -->
  <div id="sidebar-left">
    <div class="pane-header">
      <div class="pane-title">Gap Review — {region_html}</div>
      <div class="pane-sub">{total} gaps identified · {timestamp}</div>
    </div>

    <div id="stats-bar">
      <div class="stat">
        <div class="stat-num active" id="stat-active">—</div>
        <div class="stat-label">Active</div>
      </div>
      <div class="stat">
        <div class="stat-num dismissed" id="stat-dismissed">—</div>
        <div class="stat-label">Dismissed</div>
      </div>
      <div class="stat">
        <div class="stat-num high" id="stat-high">—</div>
        <div class="stat-label">High Pri</div>
      </div>
      <div class="stat">
        <div class="stat-num medium" id="stat-medium">—</div>
        <div class="stat-label">Med Pri</div>
      </div>
    </div>

    <div id="filter-bar">
      <button class="filter-btn active" data-filter="all">All</button>
      <button class="filter-btn" data-filter="active">Active</button>
      <button class="filter-btn" data-filter="dismissed">Dismissed</button>
      <button class="filter-btn" data-filter="high">High</button>
      <button class="filter-btn" data-filter="corridor">Corridor</button>
      <button class="filter-btn" data-filter="island">Island</button>
      <button class="filter-btn" data-filter="dangling">Dangling</button>
      <button class="filter-btn" data-filter="connector">Connector</button>
    </div>

    <div id="gap-list"></div>

    <div id="export-bar">
      <button class="btn btn-export" onclick="exportGeoJSON()">⬇ GeoJSON</button>
      <button class="btn btn-export" onclick="exportCSV()">⬇ CSV</button>
      <button class="btn btn-export" onclick="exportDismissed()">⬇ Dismissed</button>
    </div>
  </div>

  <!-- MAP -->
  <div id="map-container">
    <div id="map-header">
      🚲 <span id="header-active">—</span> active gaps ·
      <span id="header-dismissed">—</span> dismissed
    </div>
    <div id="map"></div>
    <div id="map-legend">
      <div class="leg-title">Network</div>
      <div class="leg-row"><div class="leg-line" style="background:#2ecc71"></div>Protected track</div>
      <div class="leg-row"><div class="leg-line" style="background:#3498db"></div>Cycle lane</div>
      <div class="leg-title" style="margin-top:8px">Gaps</div>
      <div class="leg-row"><div class="leg-dot" style="background:#e74c3c"></div>High priority</div>
      <div class="leg-row"><div class="leg-dot" style="background:#f39c12"></div>Medium</div>
      <div class="leg-row"><div class="leg-dot" style="background:#f1c40f"></div>Low</div>
      <div class="leg-row"><div class="leg-dot" style="background:#444"></div>Dismissed</div>
    </div>
  </div>

  <!-- RIGHT PANEL: review -->
  <div id="panel-right">
    <div class="pane-header">
      <div class="pane-title">Gap Review</div>
      <div class="pane-sub">Select a gap to review</div>
    </div>

    <div id="review-empty">
      <div class="icon">📍</div>
      <div style="font-size:0.72rem">Click a gap on the map<br>or in the list to review</div>
    </div>

    <div id="review-content"></div>
  </div>
</div>

<script>
// ── Data ─────────────────────────────────────────────────────────────────────
const GAPS_GEOJSON    = {gaps_json};
const CYCLING_GEOJSON = {cycling_json};
const DEST_GEOJSON    = {dest_json};
const SPINE_GEOJSON   = {spine_json};
const FAC_COLOURS     = {fac_colours};
const PRI_COLOURS     = {pri_colours};
const DST_COLOURS     = {dst_colours};
const DISMISS_REASONS = {dismiss_reasons};

// ── State ─────────────────────────────────────────────────────────────────────
const STORAGE_KEY = 'cycling_gaps_review_{region_key}';
let state = {{
  dismissed: {{}},   // gap_id -> {{reason, note, timestamp}}
  selectedId: null,
  filter: 'all',
  selectedReason: null,
}};

// Load persisted state from localStorage
(function() {{
  try {{
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved) {{
      const parsed = JSON.parse(saved);
      state.dismissed = parsed.dismissed || {{}};
    }}
  }} catch(e) {{}}

  // Also load any server-side pre-dismissed gaps (from dismissed.json)
  GAPS_GEOJSON.features.forEach(function(f) {{
    if (f.geometry.type !== 'Point') return;
    const p = f.properties;
    if (p.dismissed && !state.dismissed[p.gap_id]) {{
      state.dismissed[p.gap_id] = {{
        reason: p.dismiss_reason || 'Previously dismissed',
        note: p.dismiss_note || '',
        timestamp: 'imported'
      }};
    }}
  }});
}})();

function saveState() {{
  try {{
    localStorage.setItem(STORAGE_KEY, JSON.stringify({{dismissed: state.dismissed}}));
  }} catch(e) {{}}
}}

// ── Gap data helpers ──────────────────────────────────────────────────────────
function getGaps() {{
  return GAPS_GEOJSON.features.filter(f => f.geometry.type === 'Point');
}}

function isDismissed(gapId) {{
  return !!state.dismissed[gapId];
}}

function getActiveGaps() {{
  return getGaps().filter(f => !isDismissed(f.properties.gap_id));
}}

function priorityBand(score) {{
  if (score >= 66) return 'high';
  if (score >= 33) return 'medium';
  return 'low';
}}

// Recompute ranks for active gaps only
function recomputeRanks() {{
  const active = getGaps()
    .filter(f => !isDismissed(f.properties.gap_id))
    .sort((a, b) => b.properties.composite_score - a.properties.composite_score);
  active.forEach((f, i) => {{ f.properties._active_rank = i + 1; }});
  const dismissed = getGaps().filter(f => isDismissed(f.properties.gap_id));
  dismissed.forEach(f => {{ f.properties._active_rank = null; }});
}}

// ── Map setup ─────────────────────────────────────────────────────────────────
const map = L.map('map').setView([{centre_lat}, {centre_lon}], 13);
L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
  attribution: '© OpenStreetMap © CARTO', maxZoom: 19
}}).addTo(map);

// Cycling network layer
L.geoJSON(CYCLING_GEOJSON, {{
  style: function(f) {{
    return {{color: FAC_COLOURS[f.properties.facility_type] || '#95a5a6',
             weight: 3, opacity: 0.7}};
  }}
}}).addTo(map);

// Destination layer (toggleable)
const destLayer = L.layerGroup();
DEST_GEOJSON.features.forEach(function(f) {{
  const p = f.properties;
  const col = DST_COLOURS[p.dest_type] || '#bdc3c7';
  L.circleMarker([f.geometry.coordinates[1], f.geometry.coordinates[0]], {{
    radius: 4, fillColor: col, color: '#fff', weight: 1, opacity: 0.8, fillOpacity: 0.8
  }}).bindTooltip('<b>' + (p.name || p.dest_type) + '</b>', {{direction:'top'}})
  .addTo(destLayer);
}});

// Spine layer (toggleable)
const spineLayer = L.layerGroup();
SPINE_GEOJSON.features.forEach(function(f) {{
  const p = f.properties;
  const icon = L.divIcon({{
    html: '<div style="width:14px;height:14px;background:#e91e8c;border:2px solid #fff;'
        + 'transform:rotate(45deg);border-radius:2px;box-shadow:0 0 5px rgba(233,30,140,0.7)"></div>',
    iconSize: [14,14], iconAnchor: [7,7], className: ''
  }});
  L.marker([f.geometry.coordinates[1], f.geometry.coordinates[0]], {{icon: icon}})
  .bindTooltip('<b>🔗 ' + p.name + '</b>', {{direction:'top'}})
  .addTo(spineLayer);
}});

L.control.layers(null, {{"🎯 Destinations": destLayer, "🔗 Cycling Spines": spineLayer}},
  {{collapsed: false, position: 'topright'}}).addTo(map);

// Gap markers layer — rebuilt on dismiss/restore
const gapMarkersLayer = L.layerGroup().addTo(map);
const gapLineLayer = L.layerGroup().addTo(map);
const markerRefs = {{}};  // gap_id -> leaflet marker

function buildGapMarkers() {{
  gapMarkersLayer.clearLayers();
  gapLineLayer.clearLayers();
  Object.keys(markerRefs).forEach(k => delete markerRefs[k]);

  recomputeRanks();

  GAPS_GEOJSON.features.forEach(function(f) {{
    const p = f.properties;
    if (f.geometry.type === 'LineString') {{
      const dismissed = isDismissed(p.gap_id);
      L.polyline(f.geometry.coordinates.map(c => [c[1], c[0]]), {{
        color: dismissed ? '#333' : (PRI_COLOURS[priorityBand(p.composite_score)] || '#f1c40f'),
        weight: 1.5, opacity: dismissed ? 0.2 : 0.5, dashArray: '5,4'
      }}).addTo(gapLineLayer);
      return;
    }}
    if (f.geometry.type !== 'Point') return;

    const dismissed = isDismissed(p.gap_id);
    const band = priorityBand(p.composite_score);
    const colour = dismissed ? '#444' : (PRI_COLOURS[band] || '#f1c40f');
    const selected = state.selectedId === p.gap_id;

    const marker = L.circleMarker(
      [f.geometry.coordinates[1], f.geometry.coordinates[0]],
      {{
        radius: selected ? 10 : 7,
        fillColor: colour,
        color: selected ? '#fff' : (dismissed ? '#555' : '#fff'),
        weight: selected ? 2.5 : 1.5,
        opacity: 1,
        fillOpacity: dismissed ? 0.3 : 0.9
      }}
    );

    marker.on('click', function() {{ selectGap(p.gap_id, true); }});
    marker.addTo(gapMarkersLayer);
    markerRefs[p.gap_id] = marker;
  }});
}}

// ── Gap list rendering ─────────────────────────────────────────────────────────
function renderGapList() {{
  recomputeRanks();
  const list = document.getElementById('gap-list');
  const gaps = getGaps();

  let filtered = gaps;
  if (state.filter === 'active')    filtered = gaps.filter(f => !isDismissed(f.properties.gap_id));
  if (state.filter === 'dismissed') filtered = gaps.filter(f => isDismissed(f.properties.gap_id));
  if (state.filter === 'high')      filtered = gaps.filter(f => !isDismissed(f.properties.gap_id) && f.properties.composite_score >= 66);
  if (['corridor','island','dangling','detour','connector'].includes(state.filter))
    filtered = gaps.filter(f => f.properties.gap_type === state.filter);

  // Sort: active by rank, dismissed at bottom
  filtered.sort((a, b) => {{
    const da = isDismissed(a.properties.gap_id);
    const db = isDismissed(b.properties.gap_id);
    if (da && !db) return 1;
    if (!da && db) return -1;
    return b.properties.composite_score - a.properties.composite_score;
  }});

  list.innerHTML = filtered.map(function(f) {{
    const p = f.properties;
    const dismissed = isDismissed(p.gap_id);
    const band = priorityBand(p.composite_score);
    const rank = p._active_rank ? '#' + p._active_rank : '—';
    const dismissInfo = dismissed ? state.dismissed[p.gap_id] : null;

    return '<div class="gap-item' + (dismissed ? ' dismissed-item' : '') + (state.selectedId === p.gap_id ? ' selected' : '') +
           '" data-id="' + p.gap_id + '" data-action="select">' + '<div class="gap-rank">' + rank + ' · ' + escapeHtml(p.gap_id) + '</div>' +
      '<div class="gap-score-row">' + '<span class="gap-score-val ' + (dismissed ? 'dismissed-score' : band) + '">' +
          p.composite_score + '<span style="font-size:0.6rem;color:var(--muted)">/100</span></span>' + '<span class="gap-type-badge ' + (dismissed ? 'badge-dismissed' : 'badge-' + p.gap_type) + '">' +
          (dismissed ? 'dismissed' : p.gap_type) + '</span>' + '</div>' +
      '<div class="gap-streets">📍 ' + escapeHtml(p.from_street || '?') + ' → ' + escapeHtml(p.to_street || '?') + '</div>' + '<div class="gap-length">' + (p.straight_line_m || 0).toFixed(0) + 'm</div>' +
      (dismissInfo ? '<div class="dismiss-tag">↩ ' + escapeHtml(dismissInfo.reason) + '</div>' : '') + '</div>';
  }}).join('');

  updateStats();
}}

function updateStats() {{
  const gaps = getGaps();
  const active = gaps.filter(f => !isDismissed(f.properties.gap_id));
  const dismissed = gaps.filter(f => isDismissed(f.properties.gap_id));
  const high = active.filter(f => f.properties.composite_score >= 66);
  const med  = active.filter(f => f.properties.composite_score >= 33 && f.properties.composite_score < 66);

  document.getElementById('stat-active').textContent = active.length;
  document.getElementById('stat-dismissed').textContent = dismissed.length;
  document.getElementById('stat-high').textContent = high.length;
  document.getElementById('stat-medium').textContent = med.length;
  document.getElementById('header-active').textContent = active.length;
  document.getElementById('header-dismissed').textContent = dismissed.length;
}}

// ── Gap selection ─────────────────────────────────────────────────────────────
function selectGap(gapId, fromMap) {{
  state.selectedId = gapId;
  state.selectedReason = null;

  const gap = getGaps().find(f => f.properties.gap_id === gapId);
  if (!gap) return;

  // Pan map to gap
  if (!fromMap) {{
    map.setView([gap.geometry.coordinates[1], gap.geometry.coordinates[0]], 15);
  }}

  // Rebuild markers to show selection highlight
  buildGapMarkers();
  renderGapList();
  renderReviewPanel(gap);

  // Scroll list item into view
  setTimeout(function() {{
    const el = document.querySelector('[data-id="' + gapId + '"]');
    if (el) el.scrollIntoView({{block:'nearest', behavior:'smooth'}});
  }}, 50);
}}

// ── Review panel ─────────────────────────────────────────────────────────────
function renderReviewPanel(f) {{
  const p = f.properties;
  const dismissed = isDismissed(p.gap_id);
  const dismissInfo = dismissed ? state.dismissed[p.gap_id] : null;
  const band = priorityBand(p.composite_score);
  const scores = p.scores || {{}};

  document.getElementById('review-empty').style.display = 'none';
  const content = document.getElementById('review-content');
  content.style.display = 'block';

  const scoreBars = [
    ['Connectivity', scores.connectivity || 0],
    ['Buildability', scores.buildability || 0],
    ['Destination',  scores.destination  || 0],
    ['LTS',          scores.lts          || 0],
    ['Equity',       scores.equity       || 0],
  ].map(function(row) {{
    return '<div class="score-bar-row">' + '<div class="score-bar-label">' + row[0] + '</div>' +
      '<div class="score-bar-track"><div class="score-bar-fill" style="width:' + row[1] + '%"></div></div>' + '<div class="score-bar-val">' + row[1] + '</div></div>';
  }}).join('');

  const reasonBtns = DISMISS_REASONS.map(function(r, idx) {{
    const sel = state.selectedReason === r;
    return '<button class="reason-btn' + (sel ? ' selected' : '') +
           '" data-reason-idx="' + idx + '">' + escapeHtml(r) + '</button>';
  }}).join('');

  const activeRank = p._active_rank ? 'Rank #' + p._active_rank + ' of active gaps' : 'Dismissed';

  content.innerHTML =
    '<div class="review-gap-id">' + escapeHtml(p.gap_id) + ' · ' + escapeHtml(p.gap_type.toUpperCase()) + '</div>' + '<div class="review-street">📍 ' + escapeHtml(p.from_street || '?') + ' → ' + escapeHtml(p.to_street || '?') + '</div>' +
    '<div class="review-length">' + (p.straight_line_m || 0).toFixed(0) + 'm straight-line distance</div>' +

    '<div class="score-display">' + '<div class="score-big ' + (dismissed ? '' : band) + '" style="' + (dismissed ? 'color:var(--dismiss)' : '') + '">' +
        p.composite_score + '<span style="font-size:1rem;color:var(--muted)">/100</span></div>' + '<div class="score-rank">' + activeRank + '</div>' +
      '<div class="score-bars" style="margin-top:10px">' + scoreBars + '</div>' + '</div>' +

    '<div class="review-section">' + '<div class="review-section-title">Facility Recommendation</div>' +
      '<div class="review-row"><span class="review-row-label">Type</span>' + '<span class="review-row-val">' + (p.recommended_facility || '—') + '</span></div>' +
      '<div class="review-row"><span class="review-row-label">LTS Achievable</span>' + '<span class="review-row-val">LTS ' + (p.lts_achievable || '—') + '</span></div>' +
      '<div class="review-row"><span class="review-row-label">Cost Tier</span>' + '<span class="review-row-val">' + (p.cost_tier || '—') + '</span></div>' +
      '<div class="review-row" style="align-items:flex-start"><span class="review-row-label">OTM 18 Basis</span>' + '<span class="review-row-val" style="font-size:0.65rem">' + (p.otm18_basis || '—') + '</span></div>' +
    '</div>' +

    (p.notes && p.notes.length ? '<div class="review-section"><div class="review-section-title">Notes</div>' + '<ul class="notes-list">' + p.notes.map(n => '<li>' + n + '</li>').join('') + '</ul></div>' : '') +

    '<div id="dismiss-panel">' + (dismissed ?
        '<div class="dismissed-badge">✗ Dismissed: ' + escapeHtml(dismissInfo ? dismissInfo.reason : '') + '</div>' + (dismissInfo && dismissInfo.note ? '<div style="font-size:0.65rem;color:var(--muted);margin-bottom:8px">' + escapeHtml(dismissInfo.note) + '</div>' : '') +
        '<div class="action-row"><button class="btn btn-restore" data-action="restore" data-id="' + p.gap_id + '">↩ Restore Gap</button></div>'
        :
        '<div class="dismiss-title">Dismiss this gap</div>' + '<div class="reason-grid">' + reasonBtns + '</div>' +
        '<textarea id="dismiss-note" placeholder="Optional note (e.g. project reference, constraint details)..."></textarea>' + '<div class="action-row">' +
          '<button class="btn btn-dismiss" id="dismiss-btn" data-action="dismiss" data-id="' + p.gap_id + '" disabled>✗ Dismiss</button>' + '</div>'
      ) + '</div>';
}}

function selectReason(reason) {{
  state.selectedReason = reason;
  // Re-render reason buttons — match by the reason's index in DISMISS_REASONS
  // rather than by textContent, so escaped characters can't break the compare.
  const selIdx = DISMISS_REASONS.indexOf(reason);
  document.querySelectorAll('.reason-btn').forEach(function(btn) {{
    const idx = parseInt(btn.getAttribute('data-reason-idx'), 10);
    btn.classList.toggle('selected', idx === selIdx);
  }});
  const dismissBtn = document.getElementById('dismiss-btn');
  if (dismissBtn) dismissBtn.disabled = false;
}}

function dismissGap(gapId) {{
  if (!state.selectedReason) return;
  const note = (document.getElementById('dismiss-note') || {{}}).value || '';
  state.dismissed[gapId] = {{
    reason: state.selectedReason,
    note: note,
    timestamp: new Date().toISOString()
  }};
  state.selectedReason = null;
  saveState();
  buildGapMarkers();
  renderGapList();

  // Re-render review panel for same gap (now shows dismissed state)
  const gap = getGaps().find(f => f.properties.gap_id === gapId);
  if (gap) renderReviewPanel(gap);
}}

function restoreGap(gapId) {{
  delete state.dismissed[gapId];
  saveState();
  buildGapMarkers();
  renderGapList();
  const gap = getGaps().find(f => f.properties.gap_id === gapId);
  if (gap) renderReviewPanel(gap);
}}

// Escape text destined for innerHTML so street names / notes containing
// <, >, &, or quotes cannot break the markup or inject anything.
function escapeHtml(s) {{
  if (s === null || s === undefined) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}}

// Delegated handler for the review panel. Buttons carry data-* attributes
// instead of inline onclick handlers — this avoids the quote-escaping
// fragility that previously produced invalid JS when a value (street name,
// dismiss reason) contained an apostrophe or quote.
document.addEventListener('click', function(ev) {{
  const btn = ev.target.closest('[data-action], [data-reason-idx]');
  if (!btn) return;

  if (btn.hasAttribute('data-reason-idx')) {{
    const idx = parseInt(btn.getAttribute('data-reason-idx'), 10);
    selectReason(DISMISS_REASONS[idx]);
    return;
  }}
  const action = btn.getAttribute('data-action');
  const id = btn.getAttribute('data-id');
  if (action === 'restore') restoreGap(id);
  else if (action === 'dismiss') dismissGap(id);
  else if (action === 'select') selectGap(id, false);
}});

// ── Filter buttons ────────────────────────────────────────────────────────────
document.querySelectorAll('.filter-btn').forEach(function(btn) {{
  btn.addEventListener('click', function() {{
    state.filter = this.dataset.filter;
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    this.classList.add('active');
    renderGapList();
  }});
}});

// ── Export functions ──────────────────────────────────────────────────────────
function exportGeoJSON() {{
  const active = getGaps().filter(f => !isDismissed(f.properties.gap_id));
  const lines = GAPS_GEOJSON.features.filter(
    f => f.geometry.type === 'LineString' &&
         active.some(a => a.properties.gap_id === f.properties.gap_id)
  );
  const fc = {{type:'FeatureCollection', features: active.concat(lines)}};
  downloadFile(
    JSON.stringify(fc, null, 2),
    'gaps_finalised_{region_slug}.geojson',
    'application/json'
  );
}}

function exportCSV() {{
  const active = getGaps().filter(f => !isDismissed(f.properties.gap_id));
  const headers = ['rank','gap_id','gap_type','from_street','to_street',
                   'composite_score','straight_line_m','recommended_facility',
                   'lts_achievable','cost_tier','crosses_barrier'];
  const rows = active.map(function(f, i) {{
    const p = f.properties;
    return [p._active_rank || i+1, p.gap_id, p.gap_type,
            '"' + (p.from_street||'') + '"', '"' + (p.to_street||'') + '"',
            p.composite_score, p.straight_line_m, '"' + (p.recommended_facility||'') + '"',
            p.lts_achievable, p.cost_tier, p.crosses_barrier].join(',');
  }});
  downloadFile(
    [headers.join(',')].concat(rows).join('\\n'),
    'gaps_finalised_{region_slug}.csv',
    'text/csv'
  );
}}

function exportDismissed() {{
  const dismissed = {{}};
  Object.keys(state.dismissed).forEach(function(id) {{
    dismissed[id] = state.dismissed[id];
  }});
  downloadFile(
    JSON.stringify(dismissed, null, 2),
    'dismissed_{region_slug}.json',
    'application/json'
  );
}}

function downloadFile(content, filename, type) {{
  const blob = new Blob([content], {{type: type}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}}

// ── Init ──────────────────────────────────────────────────────────────────────
// Wait for DOM + Leaflet to be fully ready before rendering gap list and markers
// This prevents the blank-list issue when map tile loading blocks JS execution
document.addEventListener('DOMContentLoaded', function() {{
  recomputeRanks();
  buildGapMarkers();
  renderGapList();
  setTimeout(function() {{ map.invalidateSize(); }}, 100);
}});

// Fallback: also run immediately in case DOMContentLoaded already fired
// (happens reliably for inline scripts at end-of-body). Without
// invalidateSize() here the map's tile pane measures 0×0 inside the CSS grid
// column and never loads tiles, producing the blank-map symptom.
if (document.readyState === 'complete' || document.readyState === 'interactive') {{
  recomputeRanks();
  buildGapMarkers();
  renderGapList();
  setTimeout(function() {{ map.invalidateSize(); }}, 100);
}}

// Belt-and-braces: re-invalidate on window load and on resize — covers the case
// where fonts or web-fonts shift the grid columns after the initial layout.
window.addEventListener('load', function() {{
  setTimeout(function() {{ map.invalidateSize(); }}, 50);
}});
window.addEventListener('resize', function() {{ map.invalidateSize(); }});
</script>
</body>
</html>"""

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    logger.info(f"Review map written to {output_path}")
    return output_path


def save_dismissed_sidecar(dismissed_state: dict, output_path: str) -> str:
    """
    Save dismissed gap state to a JSON sidecar file.
    This is called by the export button in the review map, and can also
    be loaded on the next run to preserve review decisions across re-analysis.

    Format:
      {
        "GAP-001": {"reason": "Data error", "note": "OSM tagging artefact", "timestamp": "..."},
        "GAP-007": {"reason": "Already planned", "note": "King St EA 2024", "timestamp": "..."},
      }
    """
    with open(output_path, "w") as f:
        json.dump(dismissed_state, f, indent=2)
    logger.info(f"Dismissed state saved to {output_path}")
    return output_path
