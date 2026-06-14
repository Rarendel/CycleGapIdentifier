"""
report.py — Output generation for the cycling gap analysis tool.

Produces:
  1. Interactive HTML map (Leaflet.js, standalone, no server required)
     - Existing cycling network coloured by facility type
     - Gap markers coloured by priority score with from→to street labels
     - Toggleable destinations layer (colour-coded by category)
     - Toggleable cycling spines layer (auto-detected backbone corridors)
     - Clickable popups with full scoring breakdown and OTM Book 18 recommendation

  2. CSV report with from/to street labels for use in Excel or GIS

  3. GeoJSON export for QGIS / ArcGIS
"""

import json
import csv
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Cycling facility colours
FACILITY_COLOURS = {
    "protected_track": "#2ecc71",
    "shared_path":     "#27ae60",
    "cycle_lane":      "#3498db",
    "shared_roadway":  "#95a5a6",
    "signed_route":    "#bdc3c7",
    "unknown":         "#ecf0f1",
}

# Gap priority band colours
PRIORITY_COLOURS = {
    "high":   "#e74c3c",
    "medium": "#f39c12",
    "low":    "#f1c40f",
}

# Destination category colours
DESTINATION_COLOURS = {
    "school":           "#e74c3c",
    "university":       "#e74c3c",
    "college":          "#e74c3c",
    "station":          "#9b59b6",
    "stop_position":    "#9b59b6",
    "tram_stop":        "#9b59b6",
    "library":          "#3498db",
    "community_centre": "#3498db",
    "hospital":         "#e67e22",
    "clinic":           "#e67e22",
    "supermarket":      "#1abc9c",
    "park":             "#27ae60",
    "commercial":       "#f39c12",
    "industrial":       "#95a5a6",
}


def _priority_band(score: float) -> str:
    if score >= 66:
        return "high"
    elif score >= 33:
        return "medium"
    return "low"


# ─── Main output functions ─────────────────────────────────────────────────────

def generate_html_map(
    scored_gaps: list,
    cycling_ways: list,
    region_name: str,
    output_path: str,
    destinations: list = None,
    spines: list = None,
) -> str:
    """Generate a standalone interactive HTML map."""

    cycling_geojson  = _cycling_ways_to_geojson(cycling_ways)
    gaps_geojson     = _gaps_to_geojson(scored_gaps)
    dest_geojson     = _destinations_to_geojson(destinations or [])
    spine_geojson    = _spines_to_geojson(spines or [])

    if scored_gaps and scored_gaps[0].get("start_lat"):
        centre_lat = scored_gaps[0]["start_lat"]
        centre_lon = scored_gaps[0]["start_lon"]
    else:
        centre_lat, centre_lon = 43.48, -80.52  # Waterloo Region

    timestamp   = datetime.now().strftime("%Y-%m-%d %H:%M")
    sidebar_html = "".join(_gap_card_html(g) for g in scored_gaps)

    # Serialise all GeoJSON up front so we can inject into the f-string cleanly
    cycling_json = json.dumps(cycling_geojson)
    gaps_json    = json.dumps(gaps_geojson)
    dest_json    = json.dumps(dest_geojson)
    spine_json   = json.dumps(spine_geojson)
    fac_colours  = json.dumps(FACILITY_COLOURS)
    pri_colours  = json.dumps(PRIORITY_COLOURS)
    dst_colours  = json.dumps(DESTINATION_COLOURS)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cycling Gap Analysis — {region_name}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#1a1a2e;color:#eee}}
  #header{{padding:12px 20px;background:#16213e;border-bottom:2px solid #0f3460;
           display:flex;align-items:center;justify-content:space-between}}
  #header h1{{font-size:1.1rem;color:#e94560;font-weight:600}}
  #header span{{font-size:0.75rem;color:#aaa}}
  #container{{display:flex;height:calc(100vh - 48px)}}
  #map{{flex:1}}
  #sidebar{{width:340px;background:#16213e;overflow-y:auto;border-left:1px solid #0f3460}}
  #sidebar-header{{padding:12px 16px;background:#0f3460;font-size:0.85rem;font-weight:600;color:#e94560}}
  .gap-card{{padding:12px 16px;border-bottom:1px solid #0f3460;cursor:pointer;transition:background 0.2s}}
  .gap-card:hover{{background:#0f3460}}
  .gap-card.active{{background:#1a1a5e;border-left:3px solid #e94560}}
  .gap-rank{{font-size:0.7rem;color:#e94560;font-weight:700;text-transform:uppercase}}
  .gap-score{{font-size:1.4rem;font-weight:700}}
  .gap-score.high{{color:#e74c3c}}
  .gap-score.medium{{color:#f39c12}}
  .gap-score.low{{color:#f1c40f}}
  .gap-facility{{font-size:0.75rem;color:#aaa;margin-top:2px}}
  .gap-streets{{font-size:0.72rem;color:#7fb3d3;margin-top:3px}}
  .gap-meta{{font-size:0.72rem;color:#888;margin-top:4px}}
  .score-bar-container{{margin-top:6px}}
  .score-bar-label{{font-size:0.65rem;color:#aaa;display:flex;justify-content:space-between;margin-bottom:1px}}
  .score-bar{{height:4px;background:#333;border-radius:2px;overflow:hidden}}
  .score-bar-fill{{height:100%;border-radius:2px}}
  .legend{{padding:12px 16px;border-bottom:1px solid #0f3460}}
  .legend h3{{font-size:0.75rem;color:#aaa;margin-bottom:8px;text-transform:uppercase}}
  .legend-item{{display:flex;align-items:center;gap:8px;font-size:0.72rem;color:#ccc;margin-bottom:4px}}
  .legend-dot{{width:10px;height:10px;border-radius:50%;flex-shrink:0}}
  .legend-line{{width:20px;height:3px;flex-shrink:0;border-radius:2px}}
  .badge{{display:inline-block;padding:2px 6px;border-radius:3px;font-size:0.68rem;font-weight:600;color:white}}
  .badge-plan{{background:#8e44ad}}
  .badge-barrier{{background:#c0392b}}
  .leaflet-control-layers{{background:#16213e!important;color:#eee!important;border:1px solid #0f3460!important}}
  .leaflet-control-layers-selector{{accent-color:#e94560}}
</style>
</head>
<body>
<div id="header">
  <h1>🚲 Cycling Network Gap Analysis — {region_name}</h1>
  <span>Generated {timestamp} | {len(scored_gaps)} gaps identified</span>
</div>
<div id="container">
  <div id="map"></div>
  <div id="sidebar">
    <div class="legend">
      <h3>Existing Network</h3>
      <div class="legend-item"><div class="legend-line" style="background:#2ecc71"></div>Protected track / MUP</div>
      <div class="legend-item"><div class="legend-line" style="background:#3498db"></div>Painted cycle lane</div>
      <div class="legend-item"><div class="legend-line" style="background:#95a5a6"></div>Shared roadway</div>
    </div>
    <div class="legend">
      <h3>Gap Priority</h3>
      <div class="legend-item"><div class="legend-dot" style="background:#e74c3c"></div>High (≥ 66)</div>
      <div class="legend-item"><div class="legend-dot" style="background:#f39c12"></div>Medium (33–65)</div>
      <div class="legend-item"><div class="legend-dot" style="background:#f1c40f"></div>Low (< 33)</div>
    </div>
    <div class="legend">
      <h3>Destinations (toggle on map ↗)</h3>
      <div class="legend-item"><div class="legend-dot" style="background:#e74c3c"></div>Education</div>
      <div class="legend-item"><div class="legend-dot" style="background:#9b59b6"></div>Transit</div>
      <div class="legend-item"><div class="legend-dot" style="background:#e67e22"></div>Healthcare</div>
      <div class="legend-item"><div class="legend-dot" style="background:#27ae60"></div>Parks</div>
      <div class="legend-item"><div style="width:10px;height:10px;background:#e91e8c;transform:rotate(45deg);flex-shrink:0;border-radius:1px"></div>🔗 Cycling spine</div>
    </div>
    <div id="sidebar-header">Gaps — sorted by priority score</div>
    {sidebar_html}
  </div>
</div>
<script>
const map = L.map('map').setView([{centre_lat}, {centre_lon}], 13);
L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png',{{
  attribution:'© OpenStreetMap contributors © CARTO',maxZoom:19
}}).addTo(map);

// ── Existing cycling network ──────────────────────────────────────────────────
const cyclingNetwork = {cycling_json};
L.geoJSON(cyclingNetwork,{{
  style:function(f){{
    const c={fac_colours};
    return{{color:c[f.properties.facility_type]||'#95a5a6',weight:3,opacity:0.8}};
  }},
  onEachFeature:function(f,l){{
    l.bindPopup('<b>'+(f.properties.name||'Unnamed')+'</b><br>'+f.properties.facility_type);
  }}
}}).addTo(map);

// ── Destination layer (toggleable) ───────────────────────────────────────────
const destData = {dest_json};
const DEST_C = {dst_colours};
const destLayer = L.layerGroup();
destData.features.forEach(function(f){{
  const p=f.properties;
  const col=DEST_C[p.dest_type]||'#bdc3c7';
  L.circleMarker([f.geometry.coordinates[1],f.geometry.coordinates[0]],{{
    radius:5,fillColor:col,color:'#fff',weight:1,opacity:0.9,fillOpacity:0.85
  }}).bindTooltip('<b>'+(p.name||p.dest_type)+'</b><br><i>'+p.dest_type+'</i>',{{direction:'top'}})
  .addTo(destLayer);
}});

// ── Cycling spines layer (toggleable) ────────────────────────────────────────
const spineData = {spine_json};
const spineLayer = L.layerGroup();
spineData.features.forEach(function(f){{
  const p=f.properties;
  // Magenta rotating diamond — matches sidebar legend
  const spineIcon = L.divIcon({{
    html: '<div style="width:14px;height:14px;background:#e91e8c;border:2px solid #fff;'
        + 'transform:rotate(45deg);border-radius:2px;'
        + 'box-shadow:0 0 5px rgba(233,30,140,0.7)"></div>',
    iconSize: [14,14],
    iconAnchor: [7,7],
    className: ''
  }});
  L.marker([f.geometry.coordinates[1],f.geometry.coordinates[0]],{{icon:spineIcon}})
  .bindTooltip('<b>🔗 Cycling Spine: '+p.name+'</b><br>'
    +Math.round(p.total_length_m)+'m · '+p.source+' detection',{{direction:'top'}})
  .addTo(spineLayer);
}});

// Layer controls
L.control.layers(null,{{"🎯 Destinations":destLayer,"🔗 Cycling Spines":spineLayer}},
  {{collapsed:false,position:'topright'}}).addTo(map);

// ── Gap markers ───────────────────────────────────────────────────────────────
const gapsData = {gaps_json};
const PRI_C = {pri_colours};
const gapLayers = {{}};

L.geoJSON(gapsData,{{
  pointToLayer:function(f,ll){{
    const b=f.properties.priority_band;
    return L.circleMarker(ll,{{
      radius:8,fillColor:PRI_C[b]||'#f1c40f',
      color:'#fff',weight:2,opacity:1,fillOpacity:0.9
    }});
  }},
  style:function(f){{
    if(f.geometry.type==='LineString'){{
      const b=f.properties.priority_band;
      return{{color:PRI_C[b]||'#f1c40f',weight:2,opacity:0.6,dashArray:'6,4'}};
    }}
  }},
  onEachFeature:function(f,l){{
    const p=f.properties;
    if(f.geometry.type!=='Point') return;
    const notes=p.notes&&p.notes.length?'<div style="font-size:0.72rem;color:#888;margin-top:6px;border-top:1px solid #eee;padding-top:4px">⚠ '+p.notes.slice(0,2).join('<br>⚠ ')+'</div>':'';
    const planBadge=p.master_plan_match?'<span style="background:#8e44ad;color:#fff;font-size:0.68rem;padding:2px 6px;border-radius:3px;font-weight:600">📋 TMP Match</span> ':'';
    const barrierBadge=p.crosses_barrier?'<span style="background:#c0392b;color:#fff;font-size:0.68rem;padding:2px 6px;border-radius:3px;font-weight:600">⛔ Barrier</span> ':'';
    l.bindPopup(`
      <div style="min-width:260px;font-family:sans-serif">
        <div style="font-weight:700;font-size:0.95rem;margin-bottom:4px">${{p.gap_id}} — Rank #${{p.rank}}</div>
        <div style="font-size:0.78rem;color:#666;margin-bottom:6px">📍 ${{p.from_street||'?'}} → ${{p.to_street||'?'}}</div>
        ${{planBadge}}${{barrierBadge}}
        <div style="display:flex;justify-content:space-between;font-size:0.8rem;margin-top:6px;margin-bottom:3px">
          <span style="color:#666">Priority Score</span><b>${{p.composite_score}}/100</b>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:0.8rem;margin-bottom:3px">
          <span style="color:#666">Gap Length</span><b>${{p.straight_line_m}}m</b>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:0.8rem;margin-bottom:3px">
          <span style="color:#666">Recommended Facility</span><b style="max-width:160px;text-align:right">${{p.recommended_facility}}</b>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:0.8rem;margin-bottom:3px">
          <span style="color:#666">LTS Achievable</span><b>LTS ${{p.lts_achievable}}</b>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:0.8rem;margin-bottom:6px">
          <span style="color:#666">Cost Tier</span><b>${{p.cost_tier}}</b>
        </div>
        <div style="font-size:0.7rem;color:#888;border-top:1px solid #eee;padding-top:4px">${{p.otm18_basis}}</div>
        ${{notes}}
      </div>`);
    gapLayers[p.gap_id]=l;
  }}
}}).addTo(map);

// Sidebar click → zoom to gap
document.querySelectorAll('.gap-card').forEach(function(card){{
  card.addEventListener('click',function(){{
    const l=gapLayers[this.dataset.gapId];
    if(l){{map.setView(l.getLatLng(),16);l.openPopup();}}
    document.querySelectorAll('.gap-card').forEach(function(c){{c.classList.remove('active')}});
    this.classList.add('active');
  }});
}});
</script>
</body>
</html>"""

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info(f"HTML map written to {output_path}")
    return output_path


def generate_csv_report(scored_gaps: list, output_path: str) -> str:
    """Write a CSV report including from/to street labels."""
    if not scored_gaps:
        return output_path

    fieldnames = [
        "rank", "gap_id", "gap_type", "connector_class",
        "from_street", "to_street",
        "composite_score",
        "score_connectivity", "score_buildability", "score_destination",
        "score_lts", "score_equity",
        "start_lat", "start_lon", "end_lat", "end_lon",
        "straight_line_m", "detour_factor",
        "separation_ratio", "current_network_m",
        "recommended_facility", "lts_achievable", "cost_tier",
        "otm18_basis", "start_facility", "end_facility",
        "lts_context", "corridor_lts",
        "crosses_barrier", "master_plan_match", "plan_bonus_applied",
        "aadt_note",
    ]

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for g in scored_gaps:
            row = {
                "rank":                 g.get("rank"),
                "gap_id":               g.get("gap_id"),
                "gap_type":             g.get("gap_type"),
                "connector_class":      g.get("connector_class", ""),
                "from_street":          g.get("from_street", ""),
                "to_street":            g.get("to_street", ""),
                "composite_score":      g.get("composite_score"),
                "score_connectivity":   g.get("scores", {}).get("connectivity"),
                "score_buildability":   g.get("scores", {}).get("buildability"),
                "score_destination":    g.get("scores", {}).get("destination"),
                "score_lts":            g.get("scores", {}).get("lts"),
                "score_equity":         g.get("scores", {}).get("equity"),
                "start_lat":            g.get("start_lat"),
                "start_lon":            g.get("start_lon"),
                "end_lat":              g.get("end_lat"),
                "end_lon":              g.get("end_lon"),
                "straight_line_m":      g.get("straight_line_m"),
                "detour_factor":        g.get("detour_factor"),
                "separation_ratio":     g.get("separation_ratio"),
                "current_network_m":    g.get("current_network_m"),
                "recommended_facility": g.get("recommended_facility"),
                "lts_achievable":       g.get("lts_achievable"),
                "cost_tier":            g.get("cost_tier"),
                "otm18_basis":          g.get("otm18_basis"),
                "start_facility":       g.get("start_facility"),
                "end_facility":         g.get("end_facility"),
                "lts_context":          g.get("lts_context"),
                "corridor_lts":         g.get("corridor_lts"),
                "crosses_barrier":      g.get("crosses_barrier"),
                "master_plan_match":    g.get("master_plan_match"),
                "plan_bonus_applied":   g.get("plan_bonus_applied"),
                "aadt_note": "AADT proxy used — verify with municipal traffic model",
            }
            writer.writerow(row)

    logger.info(f"CSV report written to {output_path}")
    return output_path


def generate_geojson(scored_gaps: list, output_path: str) -> str:
    """Export gaps as GeoJSON for loading into QGIS / ArcGIS."""
    geojson = _gaps_to_geojson(scored_gaps)
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(geojson, f, indent=2)
    logger.info(f"GeoJSON written to {output_path}")
    return output_path


# ─── GeoJSON helpers ──────────────────────────────────────────────────────────

def _gaps_to_geojson(scored_gaps: list) -> dict:
    features = []
    for g in scored_gaps:
        if g.get("start_lat") is None:
            continue
        mid_lat = (g["start_lat"] + g["end_lat"]) / 2
        mid_lon = (g["start_lon"] + g["end_lon"]) / 2
        band = _priority_band(g["composite_score"])
        props = {**g, "priority_band": band}
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [mid_lon, mid_lat]},
            "properties": props,
        })
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [
                    [g["start_lon"], g["start_lat"]],
                    [g["end_lon"], g["end_lat"]],
                ],
            },
            "properties": {
                "gap_id": g["gap_id"],
                "priority_band": band,
                "composite_score": g["composite_score"],
                "recommended_facility": g.get("recommended_facility"),
            },
        })
    return {"type": "FeatureCollection", "features": features}


def _cycling_ways_to_geojson(cycling_ways: list) -> dict:
    from core.graph_builder import _classify_cycling_facility
    features = []
    for way in cycling_ways:
        if not way.get("coords") or len(way["coords"]) < 2:
            continue
        facility = _classify_cycling_facility(way.get("tags", {}))
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": way["coords"]},
            "properties": {
                "osm_id": way.get("id"),
                "facility_type": facility,
                "name": way.get("tags", {}).get("name", ""),
            },
        })
    return {"type": "FeatureCollection", "features": features}


def _destinations_to_geojson(destinations: list) -> dict:
    features = []
    for dest in destinations:
        if dest.get("lat") is None:
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [dest["lon"], dest["lat"]]},
            "properties": {
                "dest_type": dest.get("type", "unknown"),
                "name": dest.get("name", ""),
            },
        })
    return {"type": "FeatureCollection", "features": features}


def _spines_to_geojson(spines: list) -> dict:
    features = []
    for spine in spines:
        lat, lon = spine.get("centroid", (0, 0))
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "name": spine.get("name", ""),
                "source": spine.get("source", "auto"),
                "total_length_m": spine.get("total_length_m", 0),
                "facility_type": spine.get("facility_type", ""),
            },
        })
    return {"type": "FeatureCollection", "features": features}


def _gap_card_html(g: dict) -> str:
    band = _priority_band(g["composite_score"])
    scores = g.get("scores", {})
    plan_badge = '<span class="badge badge-plan">📋 TMP</span> ' if g.get("master_plan_match") else ""
    barrier_badge = '<span class="badge badge-barrier">⛔</span> ' if g.get("crosses_barrier") else ""
    from_st = g.get("from_street", "?")
    to_st = g.get("to_street", "?")

    bars = ""
    for key, label in [
        ("connectivity", "Connectivity"),
        ("buildability", "Buildability"),
        ("destination",  "Destination"),
        ("lts",          "LTS"),
        ("equity",       "Equity"),
    ]:
        val = scores.get(key, 0)
        bars += f"""
        <div class="score-bar-container">
          <div class="score-bar-label"><span>{label}</span><span>{val}</span></div>
          <div class="score-bar">
            <div class="score-bar-fill" style="width:{val}%;background:#3498db"></div>
          </div>
        </div>"""

    return f"""
    <div class="gap-card" data-gap-id="{g['gap_id']}">
      <div class="gap-rank">#{g.get('rank','?')} · {g['gap_id']} · {g['gap_type'].upper()}</div>
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div class="gap-score {band}">{g['composite_score']}<span style="font-size:0.7rem;color:#aaa">/100</span></div>
        <div>{plan_badge}{barrier_badge}</div>
      </div>
      <div class="gap-facility">{g.get('recommended_facility','Unknown facility')}</div>
      <div class="gap-streets">📍 {from_st} → {to_st}</div>
      <div class="gap-meta">
        {g.get('straight_line_m',0):.0f}m · LTS {g.get('lts_achievable','?')} achievable · {g.get('cost_tier','?')} cost
      </div>
      {bars}
    </div>"""
