#!/usr/bin/env python3
"""Give every lane a thesis and a statement of what it does not know.

`DIRECTION_FIELDS` grew two entries. This fills them in for the lanes that
already exist, and it does so by MERGING rather than by writing a direction:
`set_lane_direction` replaces the whole object, so a script that sent only the
two new keys would silently delete `for`, `claim`, `bar` and `not_for` from
every lane it touched - and it would look like it had worked.

The two fields, and the line between them:

  thesis  - the one bet the lane makes, in a form that could turn out to be
            FALSE. No evidence in it and no rung in it; those are `claim`.
  unknown - what nobody has established that would change the answer. This is
            UPSTREAM of `bar`: a bar is a test somebody has already worked out
            how to run, so writing one means having stopped being uncertain
            about the shape of the answer. The uncertainty before that had
            nowhere to live and so lived nowhere.

Every sentence below is written from what is already recorded about the lane -
its spec, its evidence rung, its findings. Nothing here is a new claim, and
where the honest answer is that the lane's own premise is untested, that is
what it says.

Run with --dry to see the transcript without writing anything.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import amp  # noqa: E402

DRY = "--dry" in sys.argv


def say(ok: bool, msg: str) -> None:
    print(("  ok   " if ok else "  MISS ") + msg)


def ws_exists(slug: str) -> bool:
    return (amp.workspace_dir(slug) / "config.json").exists()


def lanes_of(slug: str) -> set[str]:
    """Which lanes a workspace holds, WITHOUT switching into it.

    `config()` reads a module global that only points at the current workspace,
    so asking it about another one silently answers about this one - which here
    would report every lane as missing and make a dry run look like a completed
    one.
    """
    import json
    p = amp.workspace_dir(slug) / "config.json"
    if not p.exists():
        return set()
    return set((json.loads(p.read_text()).get("lanes") or {}).keys())


# slug -> lane -> {thesis, unknown}
T: dict[str, dict[str, dict[str, str]]] = {

    # ------------------------------------------------------------- academy
    "academy": {
        "ampersand-plugins": {
            "thesis": "An agent given these skills works this stack measurably "
                      "better than the same agent, on the same models, given none.",
            "unknown": "Nobody has run the comparison. It is not known whether the "
                       "skills help, do nothing, or crowd out context that would "
                       "have served the task better - and four plugins are "
                       "distributed daily on the assumption of the first.",
        },
        "the-residency": {
            "thesis": "A board of several residents deliberating reaches findings "
                      "that a single agent working alone does not.",
            "unknown": "What the losing arm looks like. No single-agent control has "
                       "been defined, so a board that appeared to win today would be "
                       "winning against nothing, and Gate 0 would still be open.",
        },
        "workbench": {
            "thesis": "A skill taught once in a browser can be sealed into a bundle "
                      "that replays somewhere else and scores the same against the "
                      "six proof gates.",
            "unknown": "Whether the gates measure the teaching or measure the "
                       "recorder. No bundle has been taught in one browser and "
                       "replayed in another, so portability is assumed rather than "
                       "shown, and the 23 open svelte-check diagnostics mean the "
                       "tree it would be sealed from is not yet clean.",
        },
    },

    # ------------------------------------------------------------- compose
    "compose": {
        "WebHost.Systems": {
            "thesis": "Hosting is a capability this portfolio should own rather "
                      "than rent.",
            "unknown": "Whether it belongs here at all. The lane is frozen pending a "
                       "workspace move and nothing has been decided about which "
                       "workspace, or about whether it is a product or a dependency.",
        },
        "agentelic.com": {
            "thesis": "A spec can be turned into a tested, packaged agent by "
                      "machine, by somebody who is not us.",
            "unknown": "Whether the boundary with KILN is a real seam or an "
                       "accident. Both lanes turn a description into a shippable "
                       "artifact, and neither spec states where one stops.",
        },
        "agentromatic.com": {
            "thesis": "Allocating a task among competing agents under quorum "
                      "produces a better allocation than assigning it.",
            "unknown": "Everything downstream of the spec. 1118 lines have met no "
                       "executing system, so it is not established that the quorum "
                       "rule terminates, let alone that it allocates well.",
        },
        "delegatic.com": {
            "thesis": "The engine's authorization evidence can be made legible to "
                      "somebody who did not build it.",
            "unknown": "Who the reader is. No non-author has read this surface, so "
                       "which parts are illegible is unmeasured and every "
                       "improvement to it is a guess.",
        },
        "deliberatic.com": {
            "thesis": "Conflicting arguments can be reduced to a checkable verdict "
                      "by weighted bipolar argumentation.",
            "unknown": "Whether Dung-style semantics survive contact with the "
                       "arguments this portfolio actually produces. Every example in "
                       "the spec was written to fit the semantics.",
        },
        "fleetprompt.com": {
            "thesis": "A trace produced on one machine can be turned into a manifest "
                      "that a different machine installs and runs.",
            "unknown": "What happens when the two machines disagree about their "
                       "environment. Installation has only ever been exercised where "
                       "both ends were ours and both were configured by us.",
        },
        "geofleetic.com": {
            "thesis": "Spatial reasoning is a service the stack should offer once, "
                      "rather than each product implementing its own.",
            "unknown": "Whether any product wants it. 987 lines of spec exist and no "
                       "consumer has asked for &space, so the demand is assumed.",
        },
        "graphonomous.com": {
            "thesis": "The engine's memory claims can be made convincing to a reader "
                      "who will not run the benchmark themselves.",
            "unknown": "Which numbers a reader needs. The page shows the numbers we "
                       "have, and it has never been established that those are the "
                       "ones that persuade.",
        },
        "specprompt.com": {
            "thesis": "A spec can be made checkable, so that a spec and its "
                      "implementation are able to disagree out loud.",
            "unknown": "Why nothing runs it. The linter works and has no consumer, "
                       "and it has never been established whether that is a "
                       "distribution problem or a sign that the check is not worth "
                       "the noise it makes.",
        },
        "ticktickclock.com": {
            "thesis": "Temporal reasoning is a service the stack should offer once, "
                      "rather than each product implementing its own.",
            "unknown": "Whether &time and &space are two services or one. The "
                       "delta-CRDT layer is specified independently in both this "
                       "lane and GeoFleetic, and nobody has decided which owns it.",
        },
    },

    # ---------------------------------------------------------------- core
    "core": {
        "AmpersandBoxDesign": {
            "thesis": "A governance decision can be computed and shipped with a "
                      "certificate a third party re-checks, instead of asserted.",
            "unknown": "Whether the eight rungs are the right eight. The ladder has "
                       "never been exercised against a policy written outside this "
                       "stack, so its completeness is untested rather than confirmed.",
        },
        "KILN": {
            "thesis": "A working trace can be crystallized into a shippable artifact "
                      "by machine, with no human packaging step in the middle.",
            "unknown": "Which traces are crystallizable. Every trace it has consumed "
                       "was produced by us, for it, so the failure modes of a trace "
                       "it did not expect are entirely unmapped.",
        },
        "PRISM": {
            "thesis": "Memory systems can be compared over time on one leaderboard, "
                      "and the comparison is diagnostic rather than a ranking.",
            "unknown": "Whether the measurement transfers. Nothing outside this "
                       "portfolio has ever been on the leaderboard, so it is not "
                       "known whether PRISM measures memory or measures Graphonomous.",
        },
        "PULSE": {
            "thesis": "Every loop in the portfolio can declare its phases, cadence "
                      "and cross-loop signals in one manifest, and that declaration "
                      "is enough for two loops to interoperate.",
            "unknown": "Whether the six tokens are sufficient or merely ours. No "
                       "manifest authored outside this repo exists, so the vocabulary "
                       "has never met a loop it was not designed around.",
        },
        "ampersand-supabase": {
            "thesis": "A shared Postgres with per-product schemas is the right data "
                      "layer for this portfolio. Recorded because it was the bet, "
                      "and it was set aside by decision rather than disproved.",
            "unknown": "What happens to the data still in it. `fleet.*` serves KILN "
                       "and FleetPrompt today and `kag.*` has been orphaned since the "
                       "BendScript pivot. Archiving the lane archived neither.",
        },
        "delegatic-engine": {
            "thesis": "Authorization can be a signed, checkable block rather than a "
                      "policy engine's opinion.",
            "unknown": "What it refuses. The gate has only ever permitted, so the "
                       "refusal path is untested and it is not established that a "
                       "denial is even reachable in the deployed configuration.",
        },
        "graphonomous": {
            "thesis": "A continual-learning memory graph beats retrieval over "
                      "documents on the questions that need accumulated context.",
            "unknown": "Whether the score survives leaving the process. Every "
                       "measurement is local, and it is not known which part of the "
                       "result is the engine and which is the local process holding "
                       "it up.",
        },
        "studbook": {
            "thesis": "A content-addressed record store built on the substrate the "
                      "stack already owns can carry KILN's manifest and audit "
                      "lineage and refuse a tampered parent by name, without Postgres.",
            "unknown": "The record identity, the index design and the fate of the "
                       "still-serving `fleet.*` data are all unruled, so the premise "
                       "that substrate supports the access patterns this needs is "
                       "untested rather than merely unbuilt.",
        },
    },

    # ------------------------------------------------------------ products
    "products": {
        "bendscript.com": {
            "thesis": "Documents are better modelled as a graph with typed inline "
                      "link facets than as text with markup around it.",
            "unknown": "Whether anything wants a `bend:` URI. Five vocabularies are "
                       "reserved and none is used, so the format has never been "
                       "constrained by a real consumer.",
        },
        "code": {
            "thesis": "A harness can run the fleet unattended without ever deciding "
                      "what to build.",
            "unknown": "Whether unattended is reachable at all, or whether every "
                       "gate it enforces is one an operator would rather have been "
                       "asked about. Nothing measures the gates it got wrong.",
        },
        "runefort.com": {
            "thesis": "Tiled file-backed UIs share a layout problem worth solving "
                      "once, as a protocol.",
            "unknown": "Whether it is a protocol or a library. Nothing has "
                       "implemented the layout independently, so the interoperable "
                       "part has never been separated from this implementation.",
        },
    },

    # ------------------------------------------------------------ research
    "research": {
        "opensentience.org": {
            "thesis": "Research claims can be published at the tier the evidence "
                      "actually reaches, badge and all, rather than at the tier that "
                      "reads best.",
            "unknown": "Whether a tier badge changes how a claim is read. No outside "
                       "reader has been observed using one.",
        },
        "weave": {
            "thesis": "The cost of a program can be certified statically, so the "
                      "resource rung is decided before running rather than measured "
                      "afterwards.",
            "unknown": "Whether the certificate binds anything. The rung and the "
                       "certificate have never met, so it is not established that "
                       "the numbers weave produces are the numbers the rung wants.",
        },
    },

    # ------------------------------------------------------------ showcase
    "showcase": {
        "docs": {
            "thesis": "A documentation site can derive every fact from the "
                      "repository it describes, so that documentation cannot "
                      "silently go stale.",
            "unknown": "What a derived fact costs when its source moves. No build "
                       "has yet failed on a mismatch, so the derivation has never "
                       "been shown to catch one.",
        },
    },

    # ----------------------------------------------------------- substrate
    "substrate": {
        "TRAAVIIS": {
            "thesis": "An episode can be re-verified by pure replay - no agent, no "
                      "network - from the bundle alone.",
            "unknown": "Whether a bundle survives the machine that made it. Every "
                       "replay so far has run on the machine that produced it.",
        },
        "TRVM": {
            "thesis": "Interaction combinators give a real parallel speedup on work "
                      "this stack actually runs.",
            "unknown": "Whether a speedup shown would be the runtime's or the "
                       "measurement's. Cell-major ordering inflated w=16 to 223.5x "
                       "against a true 136x, which proves the harness can select its "
                       "own answer, and no speedup has yet been measured with that "
                       "ruled out.",
        },
        "WRL": {
            "thesis": "Two independent implementations can be made to agree on "
                      "identity, and to disagree out loud, from the written spine "
                      "alone.",
            "unknown": "Whether the agreement survives an implementation nobody here "
                       "wrote. Both verifiers were built from the same reading, by "
                       "the same two parties, in the same order.",
        },
        "WRLM": {
            "thesis": "A proposer can generate goal and task corpora whose coverage "
                      "is measured rather than asserted.",
            "unknown": "Whether the corpus teaches anything. No consumer outside "
                       "WRLM has read it, so it is not established that coverage "
                       "measured is coverage that matters.",
        },
    },
}


def main() -> int:
    started = amp.current_workspace()
    why = amp.switch_blocked()
    if why and not DRY:
        print(f"refusing: {why}")
        return 1
    if DRY:
        print("DRY RUN - nothing is written\n")

    total = changed = 0
    for slug, lanes in T.items():
        print(f"\n{slug}")
        if not ws_exists(slug):
            say(False, f"no workspace {slug!r} - {len(lanes)} lane(s) skipped")
            continue
        have = lanes_of(slug)
        was = amp.current_workspace()
        try:
            if not DRY:
                amp.use_workspace(slug)
            for lane, add in lanes.items():
                total += 1
                if lane not in have:
                    say(False, f"{lane}: no such lane here")
                    continue
                # Merge. The existing four fields are the lane's recorded
                # direction and this script has no opinion about them; sending
                # only the two new keys would delete them.
                cur = amp.lane_direction(lane) if not DRY else {}
                merged = {**cur, **add}
                moved = sorted(k for k in add if cur.get(k) != add[k]) if not DRY \
                    else sorted(add)
                if not moved:
                    say(True, f"{lane}: already current")
                    continue
                if not DRY:
                    amp.set_lane_direction(lane, merged)
                changed += 1
                say(True, f"{lane}: +{', '.join(moved)}"
                          + (f" (keeping {', '.join(sorted(cur))})" if cur else ""))
        finally:
            if not DRY:
                amp.use_workspace(was)

    if not DRY and amp.current_workspace() != started:
        amp.use_workspace(started)
    print(f"\n{changed} of {total} lane(s) written · workspace {amp.current_workspace()}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
