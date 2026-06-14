"""
analyze_dismissals.py
=====================
Step 4 — turn manual review into threshold calibration.

The interactive review map (output/review_map.py) records, for every dismissed
gap, a structured reason and an optional note, and exports them to a
`dismissed.json` sidecar. This script reads that sidecar together with the
run's `*_gaps.csv` and answers the question a maintainer actually has:

    "Which detection thresholds are generating the false positives my
     reviewers keep dismissing, and what should I change them to?"

It does three things:

  1. Counts dismissals by reason, separating false-positive categories (which
     implicate a tunable threshold) from decision categories (already planned /
     not feasible / out of scope — valid gaps, not detection errors).

  2. For each false-positive category, lists the thresholds it implicates
     (from DISMISS_REASON_THRESHOLDS in review_map.py) and prints the
     distribution of the relevant metric across the dismissed gaps, so the new
     value is data-driven rather than guessed. For example, if every
     "Already connected" dismissal had a separation_ratio between 1.2 and 1.35,
     raising ALREADY_CONNECTED_RATIO toward 1.4 would have caught them all.

  3. Prints concrete, copy-pasteable suggestions.

Usage:
  python analyze_dismissals.py \\
    --dismissed ./output/kitchener_dismissed.json \\
    --gaps ./output/kitchener_gaps.csv

This is read-only: it never edits thresholds, it just tells you which to change
and to what. Re-run main.py with the new values (--edge-snap-m, etc.) to apply.
"""

import argparse
import csv
import json
import logging
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from output.review_map import DISMISS_REASON_THRESHOLDS  # noqa: E402

logger = logging.getLogger(__name__)

FALSE_POSITIVE_REASONS = set(DISMISS_REASON_THRESHOLDS.keys())

# Which CSV metric is most diagnostic for each false-positive reason.
REASON_METRIC = {
    "Same corridor (fragmentation)": "straight_line_m",
    "Parallel facility": "separation_ratio",
    "Already connected": "separation_ratio",
    "Data error": None,
}


def _load_dismissed(path: str) -> dict:
    with open(path) as f:
        data = json.load(f)
    # dismissed.json is { gap_id: {reason, note}, ... }
    if not isinstance(data, dict):
        raise ValueError("dismissed.json is not a {gap_id: {...}} object")
    return data


def _load_gaps_csv(path: str) -> dict:
    """Return {gap_id: row_dict}."""
    rows = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows[row.get("gap_id")] = row
    return rows


def _to_float(v):
    if v is None or v == "" or v == "inf":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _distribution(values):
    """Return (n, min, median, max) for a list of floats, ignoring None."""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return (0, None, None, None)
    n = len(xs)
    med = xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2
    return (n, xs[0], med, xs[-1])


def analyze(dismissed: dict, gaps: dict) -> None:
    by_reason = defaultdict(list)        # reason -> [gap_id, ...]
    for gid, info in dismissed.items():
        reason = (info or {}).get("reason", "") or "Unspecified"
        by_reason[reason].append(gid)

    total = len(dismissed)
    fp = sum(len(v) for r, v in by_reason.items() if r in FALSE_POSITIVE_REASONS)
    decision = total - fp

    print("=" * 68)
    print("  DISMISSAL FEEDBACK ANALYSIS")
    print("=" * 68)
    print(f"  Total dismissed gaps:        {total}")
    print(f"  False-positive dismissals:   {fp}  (implicate detection thresholds)")
    print(f"  Decision dismissals:         {decision}  (valid gaps set aside)")
    print()
    print("  Dismissals by reason:")
    for reason in sorted(by_reason, key=lambda r: -len(by_reason[r])):
        tag = "FP" if reason in FALSE_POSITIVE_REASONS else "  "
        print(f"    [{tag}] {reason:34s} {len(by_reason[reason]):>4d}")
    print()

    if fp == 0:
        print("  No false-positive dismissals — detection thresholds look well "
              "calibrated for this run. Nothing to tune.")
        print("=" * 68)
        return

    print("-" * 68)
    print("  THRESHOLD CALIBRATION SUGGESTIONS")
    print("-" * 68)
    for reason in sorted(FALSE_POSITIVE_REASONS):
        gids = by_reason.get(reason, [])
        if not gids:
            continue
        print(f"\n  ▸ {reason}  ({len(gids)} dismissed)")
        for t in DISMISS_REASON_THRESHOLDS[reason]:
            print(f"      • {t}")

        metric = REASON_METRIC.get(reason)
        if metric:
            vals = []
            for gid in gids:
                row = gaps.get(gid)
                if not row:
                    continue
                vals.append(_to_float(row.get(metric)))
            n, lo, med, hi = _distribution(vals)
            if n:
                print(f"      {metric} across these dismissals: "
                      f"n={n}, min={lo:.2f}, median={med:.2f}, max={hi:.2f}")
                if reason == "Already connected" and hi is not None:
                    print(f"        → raising ALREADY_CONNECTED_RATIO to ~{hi + 0.05:.2f} "
                          f"would have auto-suppressed all {n}.")
                if reason == "Same corridor (fragmentation)" and hi is not None:
                    print(f"        → these gaps span up to {hi:.0f} m; if they are "
                          f"genuine fragmentation, raising --edge-snap-m toward "
                          f"{min(hi, 30):.0f} m may close them at the source.")
            else:
                print(f"      (no {metric} values found in gaps.csv for these IDs — "
                      f"is this the matching run's CSV?)")

    print()
    print("  Re-run with adjusted values, e.g.:")
    print("    python main.py --region \"<region>\" --edge-snap-m 22")
    print("  or edit the constants in core/gap_finder.py "
          "(ALREADY_CONNECTED_RATIO, MIN_ISLAND_EXTENT_M) and core/network_clean.py.")
    print("=" * 68)


def main():
    parser = argparse.ArgumentParser(
        description="Analyze review-map dismissals to calibrate detection thresholds."
    )
    parser.add_argument("--dismissed", required=True,
                        help="Path to dismissed.json exported from the review map")
    parser.add_argument("--gaps", required=True,
                        help="Path to the matching *_gaps.csv from the same run")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    if not os.path.exists(args.dismissed):
        parser.error(f"dismissed.json not found: {args.dismissed}")
    if not os.path.exists(args.gaps):
        parser.error(f"gaps.csv not found: {args.gaps}")

    dismissed = _load_dismissed(args.dismissed)
    gaps = _load_gaps_csv(args.gaps)

    if not dismissed:
        print("No dismissals recorded yet — review some gaps in the review map "
              "and export dismissed.json first.")
        return

    analyze(dismissed, gaps)


if __name__ == "__main__":
    main()
