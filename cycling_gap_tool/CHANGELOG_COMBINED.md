# Unified Route Builder (v5.5 → v5.6)

`build_routes_shortest.py` was a 777-line near-duplicate of
`routing/route_builder.py` (≈95% shared code, differing only in the path weight
and already drifting — different snap distances, divergent route data shapes).
The two are now one implementation.

**Single source of truth.** `routing/route_builder.build_routes(...)` takes a
`strategy` argument:
- `lts` (default) — minimise LTS-weighted cost (the comfortable route)
- `shortest` — minimise physical distance, LTS still scored per segment
- `compare` — build both and annotate the LTS route with how it relates to the
  shortest path (length ratio, stress drop, and a plain-language verdict:
  `direct_is_comfortable` / `comfort_for_free` / `detour_buys_comfort` /
  `no_comfortable_route`).

`build_routes_shortest.py` shrank from 777 → 121 lines and is now a thin wrapper
(`= --strategy shortest`, or `--compare`). `build_routes.py` gains `--strategy`.
Output filenames are suffixed per strategy so they no longer collide.

**Bugs fixed in the merge:**
- *Start-endpoint snapping*: the LTS variant snapped the route start to the gap
  *midpoint* (the Point geometry) instead of the actual start endpoint. It now
  uses `start_lat`/`start_lon` from properties, matching the shortest variant.
- *Dead detour guard*: `MAX_ROUTE_DETOUR` previously only appended a note; an
  excessive detour now caps route quality at `poor` and flags a corridor study.
- *Missing analysis on the LTS route*: `lts_problem_pct` and
  `upgrade_complexity` (LTS 3/4 share and how hard the route is to make
  comfortable) are now computed for both strategies, not just shortest.

The CSV writer now derives fieldnames from the union of row keys, so the
`compare_*` columns appear only in compare mode. Gap detection is untouched.

Deferred (noted, not done this round): cost model still bills every segment
including existing infrastructure; LTS scorer still ignores AADT.

---

# Review-Map JS Fix + Junction-Connector Detector (v5.4 → v5.5)

## 1. Review map: invalid JavaScript (blank page, "unexpected token: string literal")
Two layers of fragile string-building were generating invalid JS:
- **Inline onclick handlers** built dismiss-reason / restore / dismiss buttons by
  concatenating apostrophe-escaped strings *inside a Python f-string*. The
  `\'` escaping collapsed, producing `selectReason('' + ... + '')`.
- **Raw JSON in `<script>`**: data was embedded via `json.dumps`, so any OSM
  name containing `</script>` (or an apostrophe in the region name used for
  export filenames) broke the page.

**Fixes (output/review_map.py):**
- All inline `onclick` handlers replaced with `data-*` attributes + a single
  delegated `click` listener — no escaping, structurally safe.
- New `escapeHtml()` applied to every data value rendered to `innerHTML`
  (street names, gap ids, reasons, notes).
- Embedded JSON now escapes `<`, `>`, `&`, U+2028/2029 to `\uXXXX`, so
  `</script>` can never break out of the script context.
- Region name escaped per-context: HTML-escaped for title/heading, JS-safe for
  STORAGE_KEY, slugified for export filenames.
Verified: generated JS parses clean under Node even when every field contains
`</script><script>alert(1)</script>`, apostrophes, and quotes.

## 2. New opt-in detector: junction connectors (Method 6)
Captures connector gaps a planner would draw by eye that the other detectors
structurally cannot see — a facility *terminus* near a *different* facility on a
*different* street (e.g. the south end of the Franklin St cycle lane and the
east-west lane just south of it). The dangling finder needs both ends to be
degree-1 dead-ends; the corridor finder needs the same street name; neither
fires here.

**Design (core/gap_finder.py `find_junction_connector_gaps`):**
- **Anchors** are facility termini, broader than dangling endpoints: degree-1
  dead-ends, nodes where the facility *type* changes, or nodes where a single
  qualifying cycling edge ends among otherwise non-qualifying edges.
- Each anchor pairs with the nearest point of a *different* cycling component
  within `--connector-max-m` (default 500).
- **Two classes** by distance: `quick_win` (≤ `--connector-quick-win-m`,
  default 150) and `network_link` (longer), so the prioritised list separates
  small wins from substantial projects — as planners maintain them.
- **Buildability gate**: the connector line must stay near a road for ≥50% of
  its length (`--connector-buildability-m`, default 25) and must not cross a
  barrier (motorway/trunk/rail/water). This is the "is there somewhere to build
  it" constraint that keeps recall high without flooding the list.
- Emits into the shared gap list, so it inherits cross-method dedup, the
  roundabout and wrong-endpoint suppressors, and scoring. It cannot reintroduce
  previously-fixed false positives — those filters run downstream.

**Off by default**, behind `--junction-connectors`, so it can be A/B'd against
the current baseline. New `connector_class` column in the CSV; new Connector
filter + badge in the review map.

**CLI:** `--junction-connectors`, `--connector-max-m`, `--connector-quick-win-m`,
`--connector-buildability-m`.

Regression-tested: topology dissolve, corridor legit/disconnected-skip,
roundabout suppression, wrong-endpoint suppression, the Franklin degree-2
terminus case, barrier rejection, and the buildability gate all pass; existing
detectors unchanged when the flag is off.

---

# False-Positive Fixes from Live Kitchener Run (v5.2 → v5.3)

The v5.2 run on real Kitchener data (159 gaps) surfaced four concrete problems.
Each is now fixed and regression-tested.

## 1. Blank review map on load
**Cause:** Leaflet's `map.invalidateSize()` ran only in the `DOMContentLoaded`
handler. For an inline script at end-of-body, `DOMContentLoaded` has usually
already fired, so the *fallback* branch ran instead — and it never called
`invalidateSize()`. The map pane measured 0×0 inside its CSS-grid column and
never requested tiles.
**Fix (output/review_map.py):** call `invalidateSize()` in the fallback branch
too, plus `window.load` and `window.resize` listeners as belt-and-braces.
Confirmed in generated HTML (5 occurrences).

## 2. Corridor false positives (GAP-048, split corridors)
**Cause:** `find_corridor_gaps` sorted same-named road edges by **latitude
only**, which is near-random for east–west streets, then walked that bogus order
as if it were the corridor. It also had no topological-adjacency check, so two
same-named fragments split by a freeway/river formed a "gap" between them
(e.g. Fountain St → Fountain St, 204 m, across open ground).
**Fix (core/gap_finder.py):** rewrote the detector to build a per-name
*subgraph* and walk each genuinely connected sub-corridor by graph adjacency
(degree-2 chains between junctions/endpoints). Same-named-but-disconnected
fragments can no longer be bridged.

## 3. Gaps ending where there is no cycling (GAP-036 "wrong end")
**Cause:** corridor and other detectors could place an endpoint on a road-graph
coordinate with no nearby cycling (across the Grand River, onto a bare road),
yielding a misleading `separation_ratio = inf`.
**Fix:** two layers. (a) The corridor walker now requires qualifying cycling
within `CORRIDOR_NEAR_CYCLING_M` (35 m) of **both** endpoints. (b)
`annotate_separation` gained a wrong-endpoint suppressor: any gap whose endpoint
is farther than `ENDPOINT_TO_CYCLING_MAX_M` (50 m) from the nearest cycling node
is dropped. The nearest-node lookup was also moved to a 50 m spatial grid, so it
is O(1) per gap instead of O(|V|).

## 4. Gaps across roundabouts (GAP-148)
**Cause:** OSM tags the cycling continuation around a roundabout as part of the
carriageway (or omits it), so two legs show as separated dead-ends even though a
rider can ride around the loop. These appear as short dangling gaps with
`separation_ratio = inf`.
**Fix (core/gap_finder.py `filter_roundabout_crossings`):** each
`junction=roundabout`/`circular` cluster is reduced to a centroid + effective
radius; a gap is suppressed when its straight-line **segment** passes within
(radius + 25 m) of a centroid. Robust to where the gap's endpoints/midpoint
fall. Only gaps ≤300 m are candidates, so long legitimate corridors near a
roundabout are kept. Requires the new `junction` tag now preserved on road
edges (core/graph_builder.py). Wired into main.py after cross-method dedup.

## Pipeline order (main.py)
detect → cross-method dedup → **roundabout filter** → **separation + wrong-endpoint
suppression** → master-plan match → score.

## Regression tests (all passing)
chain dissolve · T-junction snap · legit in-corridor gap · disconnected
same-name skip · wrong-endpoint suppression · roundabout straddle suppression ·
long-gap-near-roundabout kept · review-map blank-fix present in output.

---

# Network Continuity Improvements (v5.1 → v5.2)

This round targets the OSM false-positive problem directly: raw OSM fragments a
single physical corridor into many ways and degree-2 vertices, so the detectors
were flagging gaps within one corridor or between near-coincident nodes. On the
real Kitchener run this showed as **194 gaps, 187 of them (96%) "dangling", 75
of those ≤60m** — overwhelmingly fragmentation artifacts, not network gaps.

Four changes, applied in pipeline order:

## A. Topology cleaning before gap-finding — `core/network_clean.py` (NEW)
A pure-networkx cleaning pass (no GIS dependencies) inserted after merge/snap
and before detection, exposed as `clean_topology()`:
- **`snap_endpoints_to_edges()`** — projects degree-1 dead-ends onto nearby edge
  *interiors* and splits/connects when within tolerance (default 18 m). Catches
  the T-junction fragmentation that node-to-node snapping structurally cannot
  (a way that ends near the *middle* of another way). Conservative: only
  degree-1 endpoints, interior projections only, tolerance-bounded so genuine
  parallel facilities are not bridged.
- **`consolidate_degree2()`** — dissolves degree-2 pass-through nodes into edge
  geometry, keeping only intersections and true endpoints. Full polyline and
  summed length are retained on the merged edge. Facility-type transitions are
  preserved as nodes so corridor analysis still sees them. Triangles and
  parallel-edge cases are guarded against silent edge loss.

Mirrors the intent of OSMnx `simplify_graph` (Boeing 2025, *Transactions in
GIS* 29(3) e70037) without the GeoPandas/Shapely stack. Wired into both
`main.py` and `diagnose.py` (the latter reports components before→after).

## B. Holistic separation analysis — `core/gap_finder.py`
- **`annotate_separation()`** computes, per gap, a `separation_ratio` = current
  best on-network cycling path ÷ straight-line distance. Different components
  ⇒ ratio = ∞ (the strongest "these groups don't connect" signal). This is the
  formal version of "visually obvious gap".
- **Already-connected suppression**: a gap whose endpoints are joined by a short
  (≤60 m straight line) low-multiple (ratio ≤1.4) cycling path is dropped as a
  fragmentation artifact.
- **Stricter island extent** (`MIN_ISLAND_EXTENT_M` = 250 m): post-consolidation,
  small isolated fragments are stubs, not islands worth a connection.
- **Cross-method dedup** (`deduplicate_all_gaps()`): collapses the same physical
  break reported by two methods (e.g. island + dangling at the same endpoints),
  keeping the most informative gap via a gap-type priority sort.

## C. Separation feeds ranking — `scoring/priority.py`
Connectivity score is now `0.55·gap-type base + 0.20·spine proximity +
0.25·separation`. `separation_ratio`, `current_network_m`, and
`separation_score` are surfaced in the scored dict and the CSV (additive
columns — existing consumers unaffected).

## D. Dismissal feedback loop — `analyze_dismissals.py` (NEW)
The review map's dismiss reasons were expanded into false-positive categories
(Same corridor / Parallel facility / Already connected / Data error) vs decision
categories, each mapped to the threshold it implicates
(`DISMISS_REASON_THRESHOLDS`). The new CLI reads `dismissed.json` + `*_gaps.csv`
and reports dismissals by reason, the metric distribution across the dismissed
gaps, and concrete suggested threshold values (e.g. "raising
ALREADY_CONNECTED_RATIO to ~1.38 would have suppressed all of these"). Read-only.

## New CLI flags (`main.py`)
- `--no-clean-topology` — disable the cleaning pass (on by default).
- `--edge-snap-m FLOAT` — endpoint-to-edge snap tolerance (default 18).

## Verification
Topology unit tests (chain dissolve, facility-boundary preservation, triangle
safety, T-junction connect, parallel non-bridging, idempotency), full
pipeline-order integration test with CSV write, cross-dedup test, and the
feedback utility on synthetic dismissals — all passing. A live OSM run was not
possible in the build environment (Overpass unreachable); run it on the real
Kitchener bbox and compare the dangling share against the 96% baseline above.

---

# Combined Codebase — Assembly Notes (v5 → v5.1)

This is the consolidated, runnable version of the Cycling Infrastructure Gap
Analysis Tool, assembled from the loose v5 files and the handoff document. The
goal of the pass was: make the project actually import and run as the handoff
describes, remove dead/duplicate files, and fix the defects that were
materially affecting output — without changing the tool's behaviour where it
was already correct.

Nothing below changes the public CLI (`main.py` arguments are unchanged) or the
output file formats (CSV columns, GeoJSON schema, HTML maps are identical).

---

## 1. Restored the package layout the code already assumed

Every module imported from `core.*`, `scoring.*`, `output.*`, and `routing.*`,
but the files were delivered flat in one directory, so nothing imported. The
files were moved into the structure the handoff and the imports describe:

```
cycling_gap_tool/
├── main.py                  entry point
├── diagnose.py              diagnostic tracer
├── build_routes.py          Phase 3 — LTS-weighted routing
├── build_routes_shortest.py Phase 3 — shortest-path routing
├── core/
│   ├── osm_fetcher.py
│   ├── infra_loader.py
│   ├── graph_builder.py
│   ├── gap_finder.py
│   └── street_labels.py
├── scoring/
│   ├── priority.py
│   ├── lts.py
│   ├── otm18.py
│   └── spines.py
├── output/
│   ├── report.py
│   └── review_map.py
├── routing/
│   └── route_builder.py
└── data/field_maps/         template.yaml, winnipeg.yaml, kitchener.yaml
```

Verified: all modules and all four entry-point scripts byte-compile and import
cleanly with the project root on `sys.path` (which is exactly what `main.py`,
`diagnose.py`, and the route builders set up themselves).

## 2. Removed redundant files

- **`review_map_fixed.py`** — was byte-identical to `review_map.py` except for a
  trailing newline. Kept one canonical copy at `output/review_map.py` and
  repointed `main.py`'s import (it had been importing the `_fixed` variant).
- **`route_builder.py` at top level** — was a duplicate location; the real
  import target is `routing.route_builder` (used by `build_routes.py`). Now
  lives only in `routing/`.
- **`kitchener_*.html / .csv / .geojson`** — generated outputs, not source.
  Excluded from the codebase package; regenerate them with `main.py`.

## 3. Bug fixes in `scoring/priority.py`

- **Connectivity scoring now covers all five gap types.**
  `_score_connectivity()` only had branches for `island` and `detour` gaps;
  `dangling`, `corridor`, and `near-miss` (which together are the majority of
  detected gaps) fell through to a flat `0.5`. That meant the most heavily
  weighted criterion (connectivity, 40%) contributed an identical constant for
  most gaps — effectively disabling it for them. These gap types are now scored
  by an inverse-length proximity model (a 40 m break between two real facilities
  scores far higher than a 550 m break), with a small premium for in-corridor
  gaps. A new `max_short_gap_m` value in `compute_network_baseline()` normalizes
  it from the run's own data.
- **Removed a duplicate `import score_spine_proximity`.**
- **Removed duplicate dict keys** (`from_street`, `to_street`, `gap_label`,
  spine fields) in the `score_gap()` return value — they were defined twice, so
  the second silently shadowed the first.

## 4. Performance fix in `core/gap_finder.py`

`find_dangling_gaps()` selected dangling nodes with a list comprehension that,
per node, (a) re-derived the node's component with `[c for c in components if n
in c][0]` and (b) called `_component_total_length()`, a full edge scan. That is
`O(n_nodes × n_components × n_edges)` and reintroduced the O(n²)-class behaviour
the grid index was added to remove. Component lengths are now accumulated in a
single edge pass and looked up in O(1) via the existing `node_to_comp` index.
Output is unchanged (verified on a synthetic graph: same dangling set, same
component-length floor honoured).

## 5. Left intentionally unchanged

- **Gap detection thresholds** in `gap_finder.py` — these are documented as
  deliberately tunable and the diagnostic tool reports which threshold each
  decision hits. No values were touched.
- **Scoring weights** in `priority.py` `WEIGHTS` — design-agreed, untouched.
- **`street_labels.label_gaps()`** — `gap_finder` uses its own inline
  `_nearest_street_labels`, so `label_gaps()` is not on the live path, but it is
  referenced in the handoff and is a clean reusable helper, so it was kept
  rather than deleted.
- **Equity scoring** — still the documented neutral-0.5 placeholder pending
  StatsCan DA income data (Phase 2 item).
- **Phase 4 (ROW analysis)** — not started; still planned per the handoff.

## Verification performed

- `python -m compileall` on the whole package: clean.
- Import of all 12 modules + 4 entry scripts: clean.
- Connectivity fix: asserted dangling/corridor scores now vary with gap length
  and that a 40 m gap outscores a 550 m gap.
- `score_gap()` end-to-end on synthetic gaps of all five types: builds a
  well-formed result dict with no collapsed duplicate keys.
- Dangling perf refactor: asserted identical detection behaviour and that the
  component-length noise floor is still enforced.
