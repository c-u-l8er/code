#!/usr/bin/env python3
"""Give every lane its repository's name, and finish the showcase split.

A lane called `abd` is a name only the person who chose it can expand. The
architect reads lane names in four different contexts and has no way to learn
that `abd` is AmpersandBoxDesign, `pulse` is PULSE, or that `webhost` and
WebHost.Systems are the same thing. Naming a lane after its repository removes a
translation step that was never written down anywhere.

Two names cannot follow the rule and say so out loud:

  WRLM   shares repo c-u-l8er/TRVM with TRVM. Two lanes cannot both be `TRVM`,
         and WRLM is a workstream inside that repo (TRVM/wrlm/), not a repo.
  delegatic-engine
         the directory is `delegatic` but the GitHub repo is `delegatic-engine`,
         and there is a separate `delegatic.com` lane. The repo name is the one
         that distinguishes them.

Run with --dry to see the plan. Idempotent: a lane already named correctly is
skipped, not re-renamed.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import amp  # noqa: E402

# workspace -> [(old, new)]. Lanes already named after their repo are absent.
RENAMES = {
    "academy": [
        ("plugins", "ampersand-plugins"),
        ("residency", "the-residency"),
    ],
    "compose": [
        ("agentelic", "agentelic.com"),
        ("agentromatic", "agentromatic.com"),
        ("deliberatic", "deliberatic.com"),
        ("geofleetic", "geofleetic.com"),
        ("specprompt", "specprompt.com"),
        ("ticktickclock", "ticktickclock.com"),
        ("webhost", "WebHost.Systems"),
    ],
    "core": [
        ("abd", "AmpersandBoxDesign"),
        ("delegatic", "delegatic-engine"),
        ("fleetprompt", "fleetprompt.com"),
        ("prism", "PRISM"),
        ("pulse", "PULSE"),
        ("supabase", "ampersand-supabase"),
    ],
    "products": [
        ("bendscript", "bendscript.com"),
        ("runefort", "runefort.com"),
    ],
    "research": [
        ("opensentience", "opensentience.org"),
    ],
    "substrate": [
        ("traaviis", "TRAAVIIS"),
        ("trvm", "TRVM"),
        ("wrl", "WRL"),
        ("wrlm", "WRLM"),
    ],
}

# Lanes correct already, listed so the roster below is a complete account of all
# 29 rather than a list of the ones that happened to change.
KEPT = ["workbench", "graphonomous", "code", "weave",
        "delegatic.com", "docs", "graphonomous.com"]

# The showcase split: these two came into showcase two hours ago and are going
# straight back out. Recorded as a move rather than folded into the creation
# step, because the harness's history should show the decision being changed.
MOVES = [("graphonomous.com", "showcase", "products"),
         ("delegatic.com", "showcase", "compose")]

# Records left behind by the earlier lane moves, which live in `core` and name
# lanes that now live elsewhere. `move_lane` counts these and does not carry
# them; that is a known gap and not this script's to close. But a rename done
# without them turns "in the wrong workspace" into "names nothing that exists",
# and the second is much harder to find later. So the references follow the
# name even though the records have not followed the lane.
ORPHANS = {"core": [("traaviis", "TRAAVIIS"), ("trvm", "TRVM"),
                    ("wrl", "WRL"), ("wrlm", "WRLM")]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    blocked = amp.switch_blocked()
    if blocked:
        print("refusing:", blocked)
        return 1

    was = amp.workspaces()["current"]
    fails = 0
    try:
        for slug, pairs in RENAMES.items():
            amp.use_workspace(slug)
            lanes = amp.config().get("lanes") or {}
            print(f"\n=== {slug}")
            for old, new in pairs:
                if new in lanes and old not in lanes:
                    print(f"  --  {old} -> {new} (already done)")
                    continue
                try:
                    p = amp.rename_lane(old, new, dry_run=args.dry)
                except Exception as e:
                    print(f"  !!  {old} -> {new}: {e}")
                    fails += 1
                    continue
                recs = ", ".join(f"{k} {v}" for k, v in sorted(p["records"].items()))
                print(f"  ok  {old} -> {new}"
                      + (f"  [branch {p['branch']}]" if p["branch"] else "  [no branch]")
                      + ("  [worktree]" if p["worktree"] else "")
                      + (f"\n        records: {recs}" if recs else "\n        records: none"))

        for slug, pairs in ORPHANS.items():
            amp.use_workspace(slug)
            print(f"\n=== stranded records still in {slug}")
            for old, new in pairs:
                if old in (amp.config().get("lanes") or {}):
                    print(f"  !!  {old} is a live lane in {slug}; not an orphan")
                    fails += 1
                    continue
                c = amp.relabel_lane_records(old, new, dry_run=args.dry)
                recs = ", ".join(f"{k} {v}" for k, v in sorted(c.items()))
                print(f"  ok  {old} -> {new}: {recs or 'nothing'}")

        print("\n=== showcase split")
        for lane, src, dst in MOVES:
            amp.use_workspace(src)
            if lane not in (amp.config().get("lanes") or {}):
                print(f"  --  {lane}: not in {src} (already moved)")
                continue
            try:
                p = amp.move_lane(lane, dst, dry_run=args.dry, from_slug=src)
            except Exception as e:
                print(f"  !!  {lane} -> {dst}: {e}")
                fails += 1
                continue
            left = p.get("left_behind") or {}
            print(f"  ok  {lane}: {src} -> {dst}, {p['goals']} goal(s)"
                  + (f" — LEFT BEHIND: {left}" if left else ""))
    finally:
        amp.use_workspace(was)

    print(f"\n{'dry run' if args.dry else 'applied'}; {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
