#!/usr/bin/env python3
"""Does the published TRVM benchmarks page show the numbers the benchmark produced?

Run from the workspace root. Exits 0 if current, 1 if the page is stale, 2 if the
question cannot be answered - and the difference between 1 and 2 is the point.

This check is cheap and exact because `site/build_benchmarks.py` already records
the sha256 of `bench/results.json` inside the page's embedded payload. So the
question "is the page current?" is decided by comparing two digests, with no
rebuild, no benchmark run, and no write anywhere. A check that had to re-run the
benchmark to find out whether the page matched it would cost more than the fix.

Exit 2 (not 1) when the page or results are missing or unreadable: "the benchmark
page is stale" would be an invented observation, and a check with no verdict must
say so rather than guess a bad one.
"""
import hashlib
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
PAGE = ROOT / "TRVM" / "site" / "benchmarks.html"
RESULTS = ROOT / "TRVM" / "bench" / "results.json"
BLOCK = re.compile(r'<script id="bench-data" type="application/json">(.*?)</script>', re.S)


def main() -> int:
    for p in (PAGE, RESULTS):
        if not p.exists():
            print(f"cannot decide: {p} does not exist", file=sys.stderr)
            return 2

    m = BLOCK.search(PAGE.read_text())
    if not m:
        print("cannot decide: no <script id=\"bench-data\"> block in the page",
              file=sys.stderr)
        return 2
    try:
        published = json.loads(m.group(1)).get("sha256")
    except json.JSONDecodeError as e:
        print(f"cannot decide: the page's embedded payload is not JSON: {e}", file=sys.stderr)
        return 2
    if not published:
        print("cannot decide: the page's payload records no sha256 of its source",
              file=sys.stderr)
        return 2

    current = hashlib.sha256(RESULTS.read_bytes()).hexdigest()
    print(f"published page was built from results.json  {published}")
    print(f"bench/results.json on disk is               {current}")
    if published == current:
        return 0
    print("the benchmark has been re-run since the page was built", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
