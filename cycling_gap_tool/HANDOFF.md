# Cycling Infrastructure Gap Analysis Tool — Handoff Document

**Version:** v5 (with diagnostic tool)
**Status:** Active development — Phase 3 (route building) complete, Phase 4 (ROW analysis) pending
**Last updated:** May 2026

---

## Overview

A Python-based tool that identifies, scores, and prioritises gaps in cycling
infrastructure networks using OpenStreetMap data. It outputs interactive HTML
maps, GeoJSON, and CSV files for use in engineering review and route design.

The tool is designed for use by transportation engineers and planners at the
municipal and regional level. It does not replace engineering judgment — it
surfaces candidates for review that would otherwise require manual network
analysis across thousands of road segments.

---

## Architecture Overview

```
OSM Overpass API
      ↓
core/osm_fetcher.py       ← fetch cycling + road network + destinations
      ↓
core/graph_builder.py     ← build NetworkX graphs (cycling + road)
      ↓
core/gap_finder.py        ← identify gaps (5 methods)
      ↓
scoring/priority.py       ← composite score (0-100) per gap
      ↓
output/report.py          ← static HTML analysis map + CSV + GeoJSON
output/review_map.py      ← interactive review map (dismiss/tag/export)
      ↓
build_routes.py           ← LTS-weighted route alignment (Phase 3)
build_routes_shortest.py  ← shortest-path route alignment (Phase 3)
```

---

## File Structure

```
cycling_gap_tool/
│
├── main.py                       Entry point. Orchestrates all steps.
│                                 Args: --region, --output, --master-plan,
│                                       --dismissed, --spines, --verbose
│
├── diagnose.py                   Diagnostic tool for debugging false positives
│                                 and missing gaps in a specific bounding box.
│                                 Args: --bbox "min_lat,max_lat,min_lon,max_lon"
│                                       --region, --out
│
├── build_routes.py               Phase 3: LTS-weighted route builder.
│                                 Takes finalised gaps GeoJSON (from review map
│                                 export), finds lowest-stress path between
│                                 gap endpoints on road network.
│                                 Args: --gaps, --region, --output
│
├── build_routes_shortest.py      Phase 3 (variant): Shortest-path route builder.
│                                 Ignores LTS when routing; scores LTS after.
│                                 Identifies upgrade complexity on direct alignment.
│                                 Outputs: lts_problem_pct, upgrade_complexity
│                                 Args: --gaps, --region, --output
│
├── core/
│   ├── osm_fetcher.py            Overpass API queries. Fetches:
│   │                             - Cycling network (Tier 1+2 only — see below)
│   │                             - Road network (with cycleway attributes)
│   │                             - Destinations (schools, transit, parks, etc.)
│   │                             Known regions hardcoded with bboxes.
│   │                             Falls back to Nominatim for unknown regions.
│   │
│   ├── graph_builder.py          Builds two NetworkX graphs:
│   │                             - cycling_graph: nodes at way endpoints/intersections,
│   │                               edges = cycling way segments with facility_type
│   │                             - road_graph: full road network with LTS-relevant
│   │                               attributes (maxspeed, lanes, cycleway, aadt_proxy)
│   │                             Key functions:
│   │                               build_cycling_graph()
│   │                               build_cycling_graph_from_road_attributes()
│   │                               merge_cycling_graphs()  ← with node snapping
│   │                               _snap_nearby_nodes()    ← 15m snap threshold
│   │                               _classify_cycling_facility()
│   │
│   ├── gap_finder.py             Five gap detection methods + deduplication.
│   │                             Key constants (all tunable):
│   │                               MAX_GAP_STRAIGHT_LINE_M = 600
│   │                               MIN_COMPONENT_LENGTH_M  = 150
│   │                               DANGLING_SEARCH_RADIUS_M = 400
│   │                               MAX_CORRIDOR_GAP_M = 800
│   │                               NON_QUALIFYING_FACILITIES = {shared_roadway,
│   │                                                            signed_route, unknown}
│   │                             Key functions:
│   │                               find_island_gaps()     ← disconnected components
│   │                               find_detour_gaps()     ← high network/straight ratio
│   │                               find_dangling_gaps()   ← dead-end corridors
│   │                               find_corridor_gaps()   ← attribute transitions
│   │                               find_near_miss_gaps()  ← close cross-component pairs
│   │
│   └── street_labels.py          Nearest road name lookup helpers.
│
├── scoring/
│   ├── priority.py               Composite scoring (0-100) per gap.
│   │                             Weights:
│   │                               Connectivity  40%
│   │                               Buildability  20%
│   │                               Destination   20%
│   │                               LTS context   15%
│   │                               Equity         5%
│   │                             Bonuses:
│   │                               +12% corridor continuity bonus
│   │                               +5%  master plan match
│   │
│   ├── lts.py                    Level of Traffic Stress scoring.
│   │                             Framework: Mekuria, Furth & Nixon (2012).
│   │                             LTS 1 = all ages and abilities
│   │                             LTS 2 = most adults comfortable
│   │                             LTS 3 = confident cyclists only
│   │                             LTS 4 = experienced cyclists only
│   │                             Inputs: highway class, maxspeed, lanes, cycleway tag
│   │
│   ├── otm18.py                  OTM Book 18 facility recommendations.
│   │                             Recommends facility type per gap based on:
│   │                               - Road speed and AADT
│   │                               - Endpoint facility context
│   │                               - Barrier crossings
│   │                             Outputs: facility_type, cost_tier, otm18_basis
│   │
│   └── spines.py                 Cycling spine detection.
│                                 Auto-detects major cycling corridors via
│                                 betweenness centrality. Used to boost score
│                                 for gaps that connect to spine corridors.
│
├── output/
│   ├── report.py                 Static HTML analysis map.
│   │                             - Leaflet map with gap markers + network display
│   │                             - Sidebar with scored gap list
│   │                             - Popup per gap with full detail
│   │                             - Destinations and spines as toggleable overlays
│   │                             Also generates: CSV report, GeoJSON export
│   │
│   └── review_map.py             Interactive review map (Phase 2).
│                                 - Three-panel layout: list | map | review panel
│                                 - Dismiss gaps with structured reasons
│                                 - Restore dismissed gaps
│                                 - Live rank recalculation
│                                 - Filter by type, priority, status
│                                 - Search by street name or gap ID
│                                 - Export: GeoJSON (active only), CSV, dismissed.json
│                                 - LocalStorage persistence across sessions
│                                 - Accepts --dismissed sidecar for cross-run persistence
│
└── README.md                     Usage, workflow, deployment instructions.
```

---

## OSM Data Tiers

The tool deliberately restricts which OSM data enters the cycling graph.
This is the primary noise control mechanism.

| Tier | OSM Tags | In Graph | Notes |
|------|----------|----------|-------|
| 1 | `highway=cycleway` | Yes | Standalone cycling ways |
| 1 | `highway=path/footway` + `bicycle=designated` | Yes | Shared use paths |
| 2 | `cycleway=track/lane` on primary/secondary/tertiary | Yes | Road-attributed cycle tracks and lanes |
| 2 | `cycleway:left/right/both=track/lane` on primary/secondary/tertiary | Yes | Directional lane tags |
| 3 | `bicycle=yes` on residential/service | **No** | Too noisy — generates false gaps |
| 3 | `cycleway=shared_lane` (sharrows) | **No** | Not meaningful infrastructure |
| 3 | `route=bicycle` relations | **No** | Signed routes only |

The graph builder also has a facility-level gate: `EXCLUDED_FACILITY_TYPES =
{shared_roadway, signed_route, unknown}`. Even if a Tier 3 way slips through
the fetch query, it is excluded at graph build time.

---

## Gap Detection Methods

### Method 1 — Island Gaps
Disconnected cycling components not connected to the main network.
Threshold scales with component size: small fragments use 900m, medium
clusters 1500m, large clusters 3000m. Both endpoints must have known
facility types.

### Method 2 — Detour Gaps
Pairs of main-network nodes that are geographically close but require
a large network detour (default: ≥ 2.0× detour factor). Uses a spatial
grid index to avoid O(n²) distance checks. Samples up to 80 nodes
with geographic spread weighting.

### Method 3 — Dangling Gaps
Degree-1 nodes (dead-end corridors) with no onward connection in the
cycling graph. Searches up to 400m for the nearest cross-component node.
Uses spatial grid index — capped at 500 dangling nodes with a priority
ordering by component size. Maximum 1 gap per dangling endpoint.

### Method 4 — Corridor Gaps
Scans named road corridors for cycling attribute transitions:
`cycleway=lane → none → lane` within the same street name.
Only activates on corridors where cycling infra exists on ≥ 30% of length.
Flags gaps between 20m and 800m. Gets +12% continuity bonus in scoring.

### Method 5 — Near-miss Gaps
Short-range scan (20–300m) for cross-component node pairs that are
close but not connected. Catches cases missed by the other methods.
Deduplicates against all previously found gaps.

---

## Scoring Weights

| Criterion | Weight | What it measures |
|-----------|--------|-----------------|
| Connectivity | 40% | Component size, detour factor, spine proximity |
| Buildability | 20% | OTM 18 facility type, cost tier, barrier crossings |
| Destination proximity | 20% | Distance to schools, transit, healthcare, parks |
| LTS context | 15% | Stress level of connecting corridor |
| Equity | 5% | Proximity to low-income DAs (placeholder in v1) |

Bonuses applied after weighting:
- **+12%** for corridor gaps (same-street continuity)
- **+5%** for master plan / ATMP matches

---

## Workflow

```
Step 1 — Analysis
  python main.py --region "Waterloo Region" --output ./output
  → Generates: gap_analysis.html, review.html, gaps.csv, gaps.geojson

Step 2 — Review
  Open review.html in browser
  → Dismiss false positives with structured reasons
  → Export: gaps_finalised.geojson + dismissed.json

Step 3 — Route Design
  python build_routes.py --gaps ./output/gaps_finalised.geojson
  → LTS-weighted route alignments + cost estimates
  python build_routes_shortest.py --gaps ./output/gaps_finalised.geojson
  → Direct-path alignments with LTS upgrade complexity rating

Step 4 — Re-run with preserved decisions
  python main.py --region "Waterloo Region" --dismissed ./output/dismissed.json
  → Previous dismiss decisions pre-loaded in new review session
```

---

## Webapp (Option A — Static Hosted)

```
cycling_gap_webapp/
  index.html        Landing page with GeoJSON file upload
  review.html       Full interactive review map (no server needed)
  README.md         Deployment instructions
  sample_gaps.geojson  Test data
```

Deploy to GitHub Pages, Netlify, or any static host. Analysis runs locally
in Python; GeoJSON is uploaded to the hosted review page via FileReader API.
No data leaves the user's machine.

---

## Known Issues (as of v5)

### False Positives
1. **Same-corridor duplicate gaps** — fan patterns where one dangling node
   generates multiple gaps toward nearby nodes on a parallel facility.
   Partially addressed by 150m endpoint dedup radius and max-1-gap-per-node.
   Still occurs near complex intersections and trail junctions.

2. **Gaps between areas with no visible infrastructure** — nodes that exist
   in the cycling graph due to road-attribute extraction (Tier 2) but don't
   represent built infrastructure on the ground. The `NON_QUALIFYING_FACILITIES`
   filter catches most of these but OSM tagging inconsistencies cause leakage.

### Missing Gaps
1. **University Ave / road-attribute corridor gaps** — cycle tracks tagged on
   road ways are extracted but the graph may not connect them to standalone
   cycling ways if endpoint coordinates don't snap within 15m.

2. **Cross-river / cross-barrier gaps** — gaps that require structure crossings
   are flagged with `crosses_barrier=True` but the tool does not distinguish
   between a gap requiring a bridge versus a gap fillable with paint markings.

### Performance
- Dangling gap detection was O(n²) before the grid index fix. With the cap
  at 500 nodes and grid index it completes in <30s on Waterloo Region.
- A high dangling node count (>500 before capping) indicates Tier 3 ways
  are still entering the graph — run `diagnose.py` to identify the source.

---

## Diagnostic Tool

`diagnose.py` traces every gap decision for a specific bounding box.
Use it to investigate a specific false positive or missing gap.

```bash
# Find the bbox from the review map popup (hover over gap dot for lat/lon)
# Add ~0.01 degrees padding in each direction

python diagnose.py \
  --bbox "49.845,49.875,-97.175,-97.135" \
  --region "Winnipeg" \
  --out diag_winnipeg_area1.txt
```

The output covers 9 sections:
1. Raw cycling ways in bbox
2. Road ways with cycleway tags in bbox
3. Graph build (primary + road-attr + merged)
4. Cycling graph nodes in bbox with facility and component
5. Components in bbox with qualifying status
6. What gap would/wouldn't be created per component
7. Dangling nodes with candidate evaluation trace
8. Near-miss scan results
9. Summary

---

## Known Regions (hardcoded bboxes)

| Region | Bbox |
|--------|------|
| Waterloo Region | 43.37,43.60,-80.55,-80.20 |
| City of Kitchener | 43.39,43.50,-80.53,-80.38 |
| City of Waterloo | 43.44,43.55,-80.56,-80.46 |
| City of Cambridge | 43.34,43.43,-80.40,-80.25 |
| City of Vancouver | 49.195,49.320,-123.225,-123.020 |
| City of Victoria | 48.400,48.490,-123.440,-123.310 |
| City of San Jose | 37.250,37.470,-122.020,-121.830 |
| City of Toronto | 43.580,43.860,-79.640,-79.120 |
| City of Ottawa | 45.250,45.530,-76.000,-75.490 |
| City of Calgary | 50.845,51.210,-114.270,-113.860 |
| City of Edmonton | 53.390,53.700,-113.720,-113.270 |
| Winnipeg | 49.795,49.975,-97.325,-96.985 |

Unknown regions fall back to Nominatim geocoding for bbox lookup.

---

## Phase 4 — Planned (ROW Analysis)

The next development phase will add property limit and ROW analysis:

1. **Parcel data ingestion** — load municipal open data parcel boundaries
   (available as open data for most Ontario and BC municipalities)
2. **ROW width calculation** — compare road centreline to parcel boundaries
   to derive approximate ROW width per segment
3. **OTM 18 minimum width check** — cross-reference ROW width against
   minimum facility widths from OTM Book 18 Table 4-1
4. **Property acquisition flag** — segments where the recommended facility
   does not fit within existing ROW flagged for property acquisition review
5. **Integration with route GeoJSON** — per-segment ROW flag added to
   the Phase 3 route output as `requires_property_acquisition: bool`

---

## Dependencies

```
networkx    graph analysis
requests    Overpass API fetch
```

All outputs are standalone HTML (Leaflet CDN) — no frontend build step.

---

## Development Notes

- All gap detection constants are at the top of `gap_finder.py` and are
  intentionally tunable. The diagnostic tool shows which threshold each
  decision is hitting.
- The scoring weights in `priority.py` are in the `WEIGHTS` dict and can
  be adjusted per-project without touching logic.
- The OTM Book 18 lookup tables in `otm18.py` are the primary source of
  facility recommendations — update these if MTO releases a new edition.
- The equity scoring is a placeholder (returns 0.5 neutral for all gaps).
  Real implementation requires StatsCan Dissemination Area income data.
