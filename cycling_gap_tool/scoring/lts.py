"""
lts.py
Level of Traffic Stress (LTS) scoring for road segments and gap corridors.

Framework: Mekuria, Furth & Nixon (2012)
"Low-Stress Bicycling and Network Connectivity"
Mineta Transportation Institute Report 11-19
https://transweb.sjsu.edu/research/low-stress-bicycling-and-network-connectivity

LTS Levels:
  LTS 1 — Suitable for children and most risk-averse adults
           Physically separated from traffic, or very low speed/volume
           Target: the connected cycling network should achieve LTS 1-2

  LTS 2 — Suitable for most adults ("interested but concerned" cyclists)
           Protected lanes or low-speed roads with acceptable volume
           This is the MTO / OTM Book 18 target for inclusive cycling networks

  LTS 3 — Tolerable for confident/experienced adult cyclists
           Painted lanes on moderate-speed/volume roads

  LTS 4 — Only comfortable for strong/fearless cyclists
           High-speed or high-volume roads with no meaningful separation

Inputs per road segment:
  - highway: OSM classification (used as AADT proxy)
  - maxspeed: posted speed in km/h
  - lanes: total number of through lanes
  - cycleway: existing cycling facility tag
  - parking: presence of on-street parking (increases stress)

Note on AADT: Where actual AADT is unavailable, highway classification is used
as a proxy per _aadt_proxy() in graph_builder.py. Output flags this with
aadt_is_proxy=True. Future version: accept AADT shapefile/CSV input from
municipal traffic models to override proxy values.

Canadian context note:
OTM Book 18 facility selection thresholds align closely with LTS 2 as the
target comfort level for the majority of users. LTS scoring is therefore
directly used in the OTM Book 18 recommendation module (otm18.py).
"""

import logging

logger = logging.getLogger(__name__)

# ─── LTS lookup tables ────────────────────────────────────────────────────────
# Based on Furth et al. Table 3 and Table 4, adapted for Canadian road context
# Primary variables: speed (km/h), lanes, cycling facility type

# Speed thresholds in km/h (converted from mph in original paper)
# Original: 25mph=40km/h, 35mph=55km/h, 45mph=70km/h

LTS_NO_FACILITY = {
    # (max_speed_kmh, max_lanes): lts_level
    # Road with NO cycling facility — stress from traffic exposure
    (30, 1):  1,
    (30, 2):  2,
    (30, 99): 3,
    (40, 1):  2,
    (40, 2):  3,
    (40, 99): 4,
    (50, 1):  3,
    (50, 2):  3,   # 50 km/h, <=2 lanes, no facility -> LTS 3 (confident cyclists).
                   # Per Mekuria/Furth, LTS 4 is reserved for higher speed or
                   # 4+ lanes; a standard 50 km/h two-lane urban arterial is
                   # LTS 3. (Was 4 -- that mislabelled most untagged secondary
                   # roads as maximally hostile, since OSM rarely tags `lanes`.)
    (50, 99): 4,   # 50 km/h with many (4+) lanes remains LTS 4
    (60, 99): 4,
    (80, 99): 4,
    (99, 99): 4,
}

LTS_PAINTED_LANE = {
    # Road with painted cycle lane (no physical separation)
    (30, 1):  1,
    (30, 2):  1,
    (30, 99): 2,
    (40, 1):  1,
    (40, 2):  2,
    (40, 99): 3,
    (50, 1):  2,
    (50, 2):  2,
    (50, 99): 3,
    (60, 1):  3,
    (60, 99): 4,
    (80, 99): 4,
    (99, 99): 4,
}

LTS_PROTECTED = {
    # Protected track or multi-use path — inherently low stress
    # Stress can be elevated by conflict points (driveways, intersections)
    # Simplified: assume LTS 1 for dedicated, LTS 1-2 for shared path
    "protected_track": 1,
    "shared_path":     1,
    "cycle_lane":      None,  # use painted lane table
    "shared_roadway":  None,  # use no-facility table
    "signed_route":    None,  # use no-facility table
    "unknown":         None,
}

# OSM highway class → default speed (km/h) if maxspeed tag absent
HIGHWAY_DEFAULT_SPEED = {
    "motorway":     100,
    "trunk":         80,
    "primary":       60,
    "secondary":     50,
    "tertiary":      50,
    "unclassified":  50,
    "residential":   40,
    "living_street": 20,
    "service":       20,
    "cycleway":      20,
    "path":          20,
    "footway":       10,
}


def score_segment_lts(
    highway: str,
    maxspeed: int,
    lanes: int,
    cycleway: str,
    facility_type: str = None,
    parking: bool = False,
) -> dict:
    """
    Score a road segment's LTS level.

    Returns dict:
      lts: int (1-4)
      lts_label: str description
      speed_used: int (actual or default)
      speed_is_default: bool
      facility_considered: str
      notes: list of str
    """
    notes = []

    # Resolve speed
    speed = maxspeed if maxspeed and maxspeed > 0 else HIGHWAY_DEFAULT_SPEED.get(highway, 50)
    speed_is_default = (maxspeed == 0 or maxspeed is None)
    if speed_is_default:
        notes.append(f"Speed defaulted to {speed} km/h based on highway class '{highway}'")

    # Resolve facility
    facility = facility_type or _facility_from_cycleway(cycleway)

    # Protected infrastructure → LTS from facility table
    if facility in ("protected_track", "shared_path"):
        lts = LTS_PROTECTED[facility]
        return {
            "lts": lts,
            "lts_label": _lts_label(lts),
            "speed_used": speed,
            "speed_is_default": speed_is_default,
            "facility_considered": facility,
            "notes": notes,
        }

    # Motorway / trunk — always LTS 4 regardless of lanes/speed
    if highway in ("motorway", "trunk", "motorway_link", "trunk_link"):
        return {
            "lts": 4,
            "lts_label": _lts_label(4),
            "speed_used": speed,
            "speed_is_default": speed_is_default,
            "facility_considered": facility,
            "notes": notes + ["Motorway/trunk — LTS 4 regardless of facility"],
        }

    # Parking adjustment: on-street parking raises stress by 1 level on painted lanes
    if parking and facility == "cycle_lane":
        notes.append("On-street parking adjacent to cycle lane — stress elevated")
        use_table = LTS_NO_FACILITY  # treat as unprotected due to door zone
    elif facility == "cycle_lane":
        use_table = LTS_PAINTED_LANE
    else:
        use_table = LTS_NO_FACILITY

    lts = _lookup_lts(use_table, speed, lanes)

    return {
        "lts": lts,
        "lts_label": _lts_label(lts),
        "speed_used": speed,
        "speed_is_default": speed_is_default,
        "facility_considered": facility,
        "notes": notes,
    }


def score_gap_lts_context(start_facility: str, end_facility: str,
                          candidate_roads: list) -> dict:
    """
    Score the LTS context of a gap based on:
      - Facility types at each endpoint
      - Stress level of candidate road(s) that would fill the gap

    Returns:
      endpoint_lts_start: int
      endpoint_lts_end: int
      corridor_lts: int (stress of the road the gap would be filled on)
      gap_lts_context: str ('completes_low_stress' | 'reduces_stress' | 'high_stress_barrier')
      notes: list
    """
    start_lts = _facility_to_lts(start_facility)
    end_lts = _facility_to_lts(end_facility)
    notes = []

    # Score the best (lowest stress) candidate road
    corridor_lts = 4  # assume worst until proven otherwise
    if candidate_roads:
        best = candidate_roads[0]  # already sorted by proximity
        result = score_segment_lts(
            highway=best.get("highway", ""),
            maxspeed=best.get("maxspeed", 0),
            lanes=best.get("lanes", 1),
            cycleway=best.get("cycleway", "none"),
        )
        corridor_lts = result["lts"]
        notes.extend(result["notes"])
        if best.get("aadt_is_proxy"):
            notes.append(
                f"AADT proxy used for {best.get('name', 'unnamed road')} "
                f"({best.get('highway', '')}). "
                "Verify with municipal traffic model for final recommendation."
            )

    # Classify gap context
    both_endpoints_low = start_lts <= 2 and end_lts <= 2
    corridor_manageable = corridor_lts <= 3

    if both_endpoints_low and corridor_lts <= 2:
        context = "completes_low_stress"
        notes.append("Gap connects two LTS 1-2 endpoints via a low-stress corridor — high value connection")
    elif both_endpoints_low and corridor_lts == 3:
        context = "reduces_stress"
        notes.append("Gap connects LTS 1-2 endpoints but corridor is LTS 3 — upgraded facility needed")
    elif corridor_lts == 4:
        context = "high_stress_barrier"
        notes.append("Corridor is LTS 4 — significant infrastructure investment required")
    else:
        context = "reduces_stress"

    return {
        "endpoint_lts_start": start_lts,
        "endpoint_lts_end": end_lts,
        "corridor_lts": corridor_lts,
        "gap_lts_context": context,
        "notes": notes,
    }


def lts_priority_contribution(lts_context: dict) -> float:
    """
    Convert LTS context to a 0-1 contribution to the priority score.
    Accounts for 15% of the composite score (applied in priority.py).

    Scoring rationale:
      completes_low_stress: highest value — closes a gap in an otherwise usable network
      reduces_stress: high value — addresses a stress barrier
      high_stress_barrier: lower value for gap score (needs major infra) but flagged
    """
    mapping = {
        "completes_low_stress": 1.0,
        "reduces_stress":       0.65,
        "high_stress_barrier":  0.30,
    }
    context = lts_context.get("gap_lts_context", "reduces_stress")
    return mapping.get(context, 0.5)


# ─── Internal helpers ─────────────────────────────────────────────────────────

def _lookup_lts(table: dict, speed: int, lanes: int) -> int:
    """Look up LTS from speed/lanes table. Returns highest matching entry."""
    best_lts = 4
    for (max_speed, max_lanes), lts in table.items():
        if speed <= max_speed and lanes <= max_lanes:
            best_lts = min(best_lts, lts)
    return best_lts


def _facility_from_cycleway(cycleway: str) -> str:
    mapping = {
        "track":         "protected_track",
        "lane":          "cycle_lane",
        "opposite_lane": "cycle_lane",
        "shared_lane":   "shared_roadway",
        "share_busway":  "shared_roadway",
        "none":          "none",
        "no":            "none",
    }
    return mapping.get(cycleway, "none")


def _facility_to_lts(facility: str) -> int:
    """Convert a facility type string to its typical LTS level."""
    mapping = {
        "protected_track": 1,
        "shared_path":     1,
        "cycle_lane":      2,
        "shared_roadway":  3,
        "signed_route":    3,
        "unknown":         3,
    }
    return mapping.get(facility, 3)


def _lts_label(lts: int) -> str:
    labels = {
        1: "LTS 1 — Suitable for all ages and abilities",
        2: "LTS 2 — Suitable for most adults",
        3: "LTS 3 — Suitable for confident cyclists only",
        4: "LTS 4 — High stress, experienced cyclists only",
    }
    return labels.get(lts, "Unknown")
