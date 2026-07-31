#!/usr/bin/env python3
"""Apply WORKSPACE_DIRECTION.md rev 2 to the harness state.

Reads as a transcript: every action prints what it did, and every action is
idempotent, so a partial run can be finished by running it again. Nothing here
creates a repository, and nothing here merges, publishes or dispatches.

The four things it changes, in the only order they can happen in:

  1. workspaces      - `substrate` created, `demo-development` replaced by
                       `showcase`. Missions written for all seven.
  2. lane moves      - eight, each of which carries the lane's goals with it.
  3. new lanes       - the two presentation repos nobody owned.
  4. per-lane        - direction and mode, written inside each workspace,
                       because a lane record lives in its workspace's config.

Run with --dry to see the transcript without writing anything.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import amp  # noqa: E402

DRY = "--dry" in sys.argv


def say(ok, msg):
    print(("  ok  " if ok else "  --  ") + msg)


# Workspaces this run intends to create. A dry run has to reason about a world
# it is not building, so `showcase` and `substrate` are "present" for the
# purposes of the transcript while remaining absent on disk. Without this the
# dry run stops at the first thing that depends on them and reports nothing
# about the 29 directions, which is the part most worth reading before writing.
PLANNED: set[str] = set()


def ws_exists(slug: str) -> bool:
    return slug in amp.workspaces()["workspaces"] or (DRY and slug in PLANNED)


def lanes_of(slug: str) -> dict:
    """Which lanes a workspace holds, WITHOUT switching into it.

    `config()` reads a module global that only points at the current workspace,
    so asking it about another one silently answers about this one - which is
    the worst possible failure here, because it would report every lane as
    missing from its source and present in its target, and a dry run would look
    like a completed one.
    """
    import json
    if slug not in amp.workspaces()["workspaces"]:
        return {}
    p = amp.workspace_dir(slug) / "config.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text()).get("lanes") or {}
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------- the missions

MISSIONS = {
    "core": """Take the [&] stack from live_local to live_deployed: make the dark factory close as ONE pass across the real deployed machines, with a certificate at every step.

The loop is seven steps and each has an owner: perceive/act and install/replay are body-browser and body-os; record and consolidate are Graphonomous; crystallize and ship are FleetPrompt, gated by Delegatic; measure is PRISM. A lane earns its place here by moving one of those steps up the evidence ladder against a REAL deployed substrate - not a stub, not an injected transport, not a single-process round trip. State which claim, which rung it was on, which rung it is on now, and what settles it.

All seven steps have been live_local since 2026-04-24. Not one has run across the deployed machines. That single pass is the whole job.

What this workspace is NOT for: new features, new identity rungs, new protocols. The bet is already stated three ways. This is the pass that shows it runs. Verifiable-execution research belongs in substrate; portfolio surface belongs in compose; explaining any of it belongs in showcase.""",

    "substrate": """Make a divergence refusable instead of silent. The bet: an execution can carry its own identity - the same bytes, sealed once, recomputed by an independent implementation in another language, and refused BY NAME when the two disagree.

A lane earns its place by turning something plausible-and-wrong into something loudly refused. State which artifact, which two implementations, and which refusal code. Cross-implementation agreement about bytes only one side generated is not agreement; the claim worth having is about the same bytes.

The identity spine is FROZEN. Work that would move a sealed sem-, bcnt- or bundle- id is a ruling to be asked for, never a task to be done.

What this workspace is NOT for: shipping a product, deploying a service, or widening the surface for its own sake. A cost cliff, a dead cell, or an unbuildable term is a finding to be named, not a gap to be quietly filled.""",

    "products": """Take a thing from working-here to runnable-by-someone-who-is-not-us.

A lane earns its place by removing one named reason a stranger cannot run it: an undocumented step, a dependency on this machine, an unversioned artifact, a missing install path. Name the reason, remove it, show the run that proves it is gone.

external is the rung this workspace exists to reach and the stack has never reached it anywhere. One stranger running one thing successfully is worth more here than any amount of internal polish.

What this workspace is NOT for: new capability. If the answer to "why can't they run it" is "the feature isn't built", that is a compose or core question.""",

    "compose": """Seven lanes were given complete specifications and, in three cases, no implementation at all. This workspace's job is not to build them. It is to decide, per lane, whether they should be built - and to keep the specs honest either way.

A lane earns its place by settling one of three questions with evidence: is this spec still true; is there a real consumer; is the cost of building justified by what it would prove. A recommendation to ARCHIVE is a full and valuable answer here, and is the expected answer for more than one lane.

AgenTroMatic, GeoFleetic and TickTickClock together hold roughly 3,100 lines of specification and zero lines of implementation, none touched since April. Deciding about them IS the work.

What this workspace is NOT for: building all of it because it was specified. A spec is not a commitment. Rule 6 says what to build is Travis's decision; this workspace exists to put that decision in front of him with evidence attached.""",

    "academy": """Prove that what this stack can do survives being handed to someone else. Three handoffs: to an agent (plugins), to a board of agents (residency), and to a replay (workbench).

A lane earns its place by making one handoff verifiable: the receiver did the thing, and we can show it did the thing rather than something adjacent.

The open thesis this workspace owns is Gate 0 of the-residency - that a multi-resident board beats a single agent - recorded as the untested core premise, deliberately gating everything downstream, still untested at n=1.

What this workspace is NOT for: rebuilding what the receiver could delegate. The residency's own scope note is the rule for all three lanes - cover the layer nothing else covers, delegate the rest.""",

    "research": """The doctrine lists four claims currently believed and not settled. This workspace produces evidence for or against them, and destroying one is worth as much as confirming it.

A lane earns its place by moving one named open thesis: the governed-agent bet, whether the dark factory closes, whether verifiable != executable generalises, or Gate 0.

Retraction is the primary product. The most valuable records in this repository are the retractions - the 448 cells with no preimage, the unsound stop rule, the w=64 cost cliff that had been inferred from a fit while the run it described was killed at five minutes. A finding that survives contact is the exception, not the target.

What this workspace is NOT for: new protocols. OS-012 is deferred and stays deferred.""",

    "showcase": """Turn evidence the stack has already earned into something a person can look at, without changing the underlying claim.

A lane earns its place by making a true thing legible. It NEVER earns its place by making a thing sound better than its rung. Every lane here names the system lane it represents, and a commit here moves no rung on that lane - that separation exists so deployment and messaging changes cannot contaminate an evidence-oriented history.

The failure this workspace is built to prevent is a page that says "shipped" about something at in_tree. That is rule 2 breaking in public, where it is most expensive.

What this workspace is NOT for: making the claim. If a page needs a number the system lane has not earned, the answer is that the number does not exist yet.""",
}

# ------------------------------------------------------------- the lane moves

MOVES = [
    ("trvm", "core", "substrate"),
    ("wrl", "core", "substrate"),
    ("wrlm", "core", "substrate"),
    ("traaviis", "core", "substrate"),
    ("docs", "core", "showcase"),
    ("graphonomous", "products", "core"),
    ("fleetprompt", "compose", "core"),
    ("delegatic", "compose", "core"),
]

# Paths are relative to the repo root, which is what `add_lane` joins against.
# An absolute path here does not fail loudly - it is appended to the root and
# produces a doubled path that reads like a filesystem problem.
NEW_LANES = [
    ("graphonomous.com", "graphonomous.com", "showcase"),
    ("delegatic.com", "delegatic.com", "showcase"),
]

# ------------------------------------------------- the directions, by workspace

D = {}

D["core"] = {
    "abd": {
        "for": "The [&] Protocol itself - the composition algebra every other lane declares "
               "itself in - and the box-and-box governance kernel that turns \"may this "
               "proceed, and is it best\" into arithmetic with a certificate.",
        "claim": "The governed-agent bet. in_tree - 129 property-tested laws at 2000 trials "
                 "each; cross-process kernel admission live_local (Node decides, BEAM acts "
                 "only on permission, certificate retained to disk, 13/13 assertions on "
                 "permit and refuse). Toward external: no party outside us has tried to "
                 "break the kernel.",
        "bar": "Someone who is not us runs `govern` against their own policy and either gets "
               "a certificate they can check or breaks a law. 129 universal properties are "
               "not an exhaustive proof and the laws page says so.",
        "not_for": "New primitives. Six roots and 19 subtypes are the algebra. The &body.* "
                   "runtime gap belongs to body-browser and body-os, not here.",
        "after_bar": "new_ruling"},
    "prism": {
        "for": "Measuring loops over time - the diagnostic third of the three-protocol "
               "stack, and step 7 of the dark factory.",
        "claim": "PRISM measures a real system's memory loop. live_deployed - prism-eval "
                 "running in iad; 3 live OS-011 tests drive compose/register/interact/persist "
                 "against a running Graphonomous. Toward external: no competitor system has "
                 "ever been measured.",
        "bar": "One non-[&] memory system (Mem0, Zep or Letta) adapted as a PRISM system and "
               "scored on the same leaderboard as graphonomous. Until then the leaderboard "
               "compares us to us.",
        "not_for": "More scenarios for systems we wrote. Coverage of our own surface is not "
                   "the bottleneck; a second vendor is.",
    },
    "pulse": {
        "for": "The manifest that lets every loop declare its phases, cadence, nesting and "
               "cross-loop signals in one file, so PRISM can read a system's loop instead of "
               "being told about it.",
        "claim": "A PULSE manifest is executable, not descriptive. live_deployed - os-pulse-mcp "
                 "running, 12 conformance tests, and the 2026-07-28 runtime-driver executes "
                 "real loop phases with behavioral probes. Toward: a manifest authored by "
                 "someone who did not write PULSE.",
        "bar": "A loop manifest authored outside this repo passes conformance and is read by "
               "PRISM without special-casing.",
        "not_for": "New tokens. Six canonical cross-loop tokens exist; a seventh needs a "
                   "protocol that emits it, not a slot that might be used.",
    },
    "supabase": {
        "for": "One Postgres instance with a schema per product, so one `supabase start` runs "
               "the whole ecosystem's data layer.",
        "claim": "unrecorded. 35 migrations across 11 schemas apply cleanly locally; nothing "
                 "records whether they apply against a deployed instance.",
        "bar": "`supabase db reset` against a deployed project, then one product's live suite "
               "passing against it. Separately: kag.* (010-019) has been orphaned since the "
               "BendScript pivot on 2026-04-27 and needs reclaiming or dropping BY NAME.",
        "not_for": "New schemas for products with no implementation. geo.*, temporal.* and "
                   "orchestrate.* are reserved for lanes that have never written a line of code.",
    },
    "graphonomous": {
        "for": "The continual-learning memory engine - dark-factory steps 2 and 3, where an "
               "interaction becomes a trace and a trace becomes consolidated memory.",
        "claim": "&memory.episodic.store/replay works against a real consumer. live_local - "
                 "573 tests, 92.6% QA on LongMemEval 500Q, and the deployed graphonomous-mcp "
                 "has been driven live by FleetPrompt, PRISM and body-browser. Toward "
                 "live_deployed as part of ONE pass, not three separate smoke tests.",
        "bar": "A trace written by a deployed body-* machine, consolidated by the deployed "
               "graphonomous-mcp, replayed by a different deployed machine - one pass, one "
               "certificate chain, no local process anywhere in it.",
        "not_for": "New graph algorithms, new MCP surface, new machine phases. The engine is "
                   "the most finished thing in the stack; what is unproven is the wire "
                   "between it and everything else.",
    },
    "fleetprompt": {
        "for": "Crystallizing an interaction trace into a shippable skill manifest, and "
               "shipping it - dark-factory steps 4 and 5.",
        "claim": "A trace recorded on one machine becomes an installed skill on another. "
                 "live_local - 157 tests, InstallEngine 7/7, live crystallization verified "
                 "against a running local Graphonomous. Toward live_deployed: the "
                 "cross-machine install has never crossed a boundary that is also a machine "
                 "boundary.",
        "bar": "Machine A's trace becomes a manifest in fleet.manifests on a deployed "
               "instance and machine B installs from it. STACK_COMPLETION section 4 step 5 "
               "says this is \"not blocked by code\".",
        "not_for": "Marketplace surface, trust scoring, publisher UX. The loop needs the "
                   "install to happen, not to be browsable.",
    },
    "delegatic": {
        "for": "The OS-006 authorization kernel - the gate that decides whether an agent may "
               "do the thing, with an HMAC-signed AuthorizationBlock as the receipt.",
        "claim": "An authorization decision travels and is verifiable where it lands. "
                 "live_deployed - delegatic-mcp is up and Workbench's gate.authority confirms "
                 "put_policy/authorize/verify live against it. Toward: a deny that actually "
                 "stops a deployed action mid-loop.",
        "bar": "One dark-factory pass where a deliberately unauthorized step is refused by "
               "the deployed delegatic-mcp and the loop halts with the refusal on record. A "
               "gate that has only ever permitted is not a gate that has been tested.",
        "not_for": "Org trees, memberships, effective-policy. The 630-line spec has them; "
                   "v0.1 is the authorization kernel with an in-memory ETS store. Postgres "
                   "is a decision, not a task.",
        "after_bar": "new_ruling",
    },
}

D["substrate"] = {
    "wrl": {
        "for": "The WallRiderLang identity spine and its relation IR - what decides what a "
               "world's identity IS, and refuses by name when two implementations disagree "
               "about it.",
        "claim": "Cross-implementation identity agreement. live_local - 890 checks green, "
                 "register 128 rows, model debt 0; both verifiers refuse all 15 negative "
                 "vectors AND agree on the NAME of every refusal across two languages and "
                 "two repos. Toward external: an implementation neither of us wrote.",
        "bar": "A third party reads test/projection-vectors.json and "
               "test/projection-negative-vectors.json, reproduces every sem- and bcnt- id, "
               "and refuses all 15 tampers with matching codes. Until then both "
               "implementations are ours.",
        "not_for": "Moving a sealed id. The Path C ruled order is exhausted and no next step "
                   "is ruled. Grants, dynamic topology and D9 are out of scope by ruling. A "
                   "new identity rule requires a register row; model debt stays 0.",
    },
    "trvm": {
        "for": "The interaction-calculus runtime under WRL - the reducer, the Forge engine, "
               "and the cross-runtime evidence that independent implementations agree.",
        "claim": "Coordination-free distributed reduction. live_local - 5 IC32-model runtimes "
                 "give byte-identical normal forms AND identical interaction counts; ic32 "
                 "--test 13/13; ic32.wasm matches the reference bit-for-bit. Toward: a "
                 "demonstrated parallel SPEEDUP, which has never been shown. Correctness only.",
        "bar": "A measured speedup with the method stated: baseline, run count, exclusions, "
               "and whether the ordering could have selected the result. Rule 4 was bought in "
               "this lane and binds hardest here.",
        "not_for": "Closing section 6.4 (snapshot) and 6.5 (REF) quietly. They are named GAPs "
                   "and stay named. ic_ref failing SILENTLY on tetration is a finding, not a "
                   "bug to paper over.",
    },
    "wrlm": {
        "for": "The proposer layer above WRL - goal specifications, coverage, and the corpus "
               "that says whether a repertoire can express what it claims to.",
        "claim": "Coverage is derived, not declared. in_tree - 527 accepted records, 298 of "
                 "320 inhabited cells, quota mass 0.823; the domain is COMPUTED by calling "
                 "the derivation functions rather than published as a Cartesian product. "
                 "Toward live_local: nothing outside its own generator has consumed the corpus.",
        "bar": "A consumer outside WRLM reads the corpus and finds something the generator "
               "did not already know.",
        "not_for": "Build-order steps 3-10. Two are shipped and closed; the rest are paper. "
                   "The global marginal-coverage stop rule is RETRACTED as unsound and must "
                   "not return as a convenience.",
        "after_bar": "new_ruling",
    },
    "traaviis": {
        "for": "trvs, the verifiable world terminal over the Forge engine - the CLI that "
               "takes a world, runs it, and hands back an episode bundle a stranger can "
               "replay WITHOUT the agent.",
        "claim": "An episode carries its own proof. live_local - 532 tests, `trvs "
                 "verify-episode` is a pure replay with no agent in it, and `trvs serve --ors` "
                 "gave the Episode Kernel its first transport. Toward external: nobody "
                 "outside has replayed a bundle.",
        "bar": "A bundle produced here, replayed on someone else's machine, reaching the same "
               "verdict - with the agent absent from the replay.",
        "not_for": "The traaviis.com marketing site, which shares this repository but not "
                   "this claim. A commit that only changes index.html moves no rung and must "
                   "not be reported as if it did. The site cannot be split into a showcase "
                   "lane: a lane is a repo root and these share one. `trvs serve --mcp` is "
                   "ruled but unbuilt and is the only remaining ruled step.",
    },
}

D["products"] = {
    "code": {
        "for": "The amp orchestration harness and console - what dispatches every worker, "
               "holds every gate, and is the only place the operator sees what the stack "
               "believes.",
        "claim": "The harness can run the fleet unattended without deciding what to build. "
                 "unrecorded as a rung; the evidence is that no gate here clears on the "
                 "harness agreeing with itself - the merge gate reads GitHub's own "
                 "statusCheckRollup, which the harness structurally cannot manufacture.",
        "bar": "Every gate reads evidence the harness cannot produce. Named exceptions, each "
               "with the reason it is still self-reported.",
        "not_for": "Deciding what to build. Rule 6 is the rule this lane is most able to "
                   "break and least able to notice breaking. auto_adopt defaults off and "
                   "stays off by default.",
    },
    "bendscript": {
        "for": "The BendScript Protocol - a graph-first document format with typed inline "
               "link facets and span-addressable bend: URIs.",
        "claim": "The format round-trips through an LLM without losing structure. in_tree - "
                 "96 tests (85 round-trip + 11 harness) against @bendscript/core. Toward "
                 "live_local: v0.1 final is gated on section 8 round-trip evidence and on "
                 "section 14 needing one portfolio adopter committed to bend: URIs.",
        "bar": "One portfolio product actually emitting or consuming bend: URIs. "
               "Graphonomous via bendscript.memory.v1 is the named candidate. Five "
               "vocabularies are reserved; none is used.",
        "not_for": "Reviving the v1 canvas/KAG SaaS. It is archived under old_scrap/v1/ and "
                   "the kag.* schemas are orphaned. That pivot is settled.",
    },
    "runefort": {
        "for": "The RuneFort layout protocol - tiled, file-backed UI layouts from four "
               "primitives, compiling to CSS grid.",
        "claim": "unrecorded. @runefort/core 0.1.0-alpha.1 ships with NO test script at all - "
                 "honestly untested, which is not the same as zero tests.",
        "bar": "A test script that runs, plus one consumer outside this repo laying out a "
               "real UI from a fort file. The [&] supervisor floor is the named first "
               "customer and has not adopted it.",
        "not_for": "The prior spatial-cognition product, removed in the 2026-04-26 pivot. "
                   "rune.* now serves only the authoring playground.",
        "after_bar": "new_ruling",
    },
}

D["compose"] = {
    "agentelic": {
        "for": "The premium agent builder - a spec-driven pipeline turning a description into "
               "a running agent.",
        "claim": "unrecorded as a rung. live_deployed as a SERVICE (agentelic in iad, 66 "
                 "tests) but no claim about it has ever been placed on the ladder. "
                 "Deployment is not adoption.",
        "bar": "One agent built through the pipeline by someone who is not us, running.",
        "not_for": "More MCP tools. Ten exist, and the drift that broke the suite was a tool "
                   "COUNT - what happens when surface grows faster than use.",
        "after_bar": "new_ruling",
    },
    "specprompt": {
        "for": "The spec-driven development standard - parser, validator, linter - what makes "
               "docs/spec/README.md checkable rather than aspirational.",
        "claim": "unrecorded. 71 tests, deployed in ord, and the entire portfolio is built "
                 "spec-first without a single lane running the linter in CI.",
        "bar": "One portfolio lane's spec gated by SpecPrompt in CI, failing a build when "
               "spec and code disagree. That is the cheapest real consumer available and it "
               "is inside the house.",
        "not_for": "Registry mode and the spec.* schemas until filesystem mode has one gated "
                   "consumer.",
    },
    "webhost": {
        "for": "The WebHost.Systems dashboard - React/Vite + Supabase auth, the only "
               "conventional web app in the portfolio.",
        "claim": "unrecorded. 143 tests; [&] integration specs written, runtime provider "
                 "pending.",
        "bar": "None. This lane is frozen and the direction does not reopen it.",
        "not_for": "Everything, currently.",
        "after_bar": "move_workspace",
    },
    "agentromatic": {
        "for": "Deciding whether the AgenTroMatic deliberation engine should be built at all.",
        "claim": "unrecorded. 1118-line spec (Elixir/OTP + Phoenix + Raft consensus), 50 "
                 "sections, zero implementation, never ran a goal.",
        "bar": "A written recommendation with evidence: who the consumer is, what it proves "
               "that Graphonomous's route.deliberate and the-residency's board do not already "
               "prove, and what building it would cost. ARCHIVE is an acceptable and expected "
               "answer.",
        "not_for": "Implementing it. No Elixir is written here until the recommendation is "
                   "ruled on.",
        "after_bar": "new_ruling",
    },
    "deliberatic": {
        "for": "Deciding whether the Deliberatic argumentation protocol should be built at all.",
        "claim": "unrecorded. 418-line spec (Dung 1995, Raft/PBFT, Merkle evidence), zero "
                 "implementation.",
        "bar": "The same recommendation, measured against box-and-box's deontic rung and the "
               "residency board.",
        "not_for": "Implementing it.",
        "after_bar": "new_ruling",
    },
    "geofleetic": {
        "for": "Deciding whether GeoFleetic should be built at all.",
        "claim": "unrecorded. 987-line spec (delta-CRDTs, federated learning, GNN routing), "
                 "63 sections, zero implementation. STACK_COMPLETION section 7 calls "
                 "multi-agent spatial coordination \"genuine research territory; no protocol "
                 "solves it anywhere\" - which cuts both ways.",
        "bar": "A recommendation that separates the research claim from the product claim. "
               "They may have different answers.",
        "not_for": "Implementing it. geo.* schemas stay reserved and unused.",
        "after_bar": "new_ruling",
    },
    "ticktickclock": {
        "for": "Deciding whether TickTickClock should be built at all.",
        "claim": "unrecorded. 1013-line spec (Mamba anomaly, multi-timescale consolidation), "
                 "51 sections, zero implementation.",
        "bar": "A recommendation that says specifically what this does that PULSE's temporal "
               "algebra and Graphonomous's consolidator do not already do. The overlap is the "
               "question.",
        "not_for": "Implementing it. temporal.* stays reserved and unused.",
        "after_bar": "new_ruling",
    },
}

D["academy"] = {
    "residency": {
        "for": "The-residency board - the only layer nothing else in the portfolio covers: "
               "multiple residents deliberating, findings as first-class objects carrying "
               "provenance, feeding a living paper.",
        "claim": "Gate 0 - that a multi-resident board beats a single agent. UNTESTED. One "
                 "overnight run produced human-verified findings that became real "
                 "TRVM/spec/paper.md edits, at n=1. The doctrine lists this as an open thesis "
                 "and it gates everything downstream.",
        "bar": "Gate 0 answered either way: a board arm and a single-agent arm on the same "
               "task, scored, with the losing arm NAMED. The 2026-06-30 interface gate ran 4 "
               "arms on board-as-INTERFACE; Gate 0 itself is still open.",
        "not_for": "Wiring in memory, benchmarks, loops, governance or execution. The scope "
                   "note is explicit - delegate all of it. Nothing downstream of Gate 0 gets "
                   "built until Gate 0 answers.",
        "after_bar": "new_ruling",
    },
    "workbench": {
        "for": "Teach an agent once, seal the trace into a replayable SkillBundle, score it "
               "against six named proof gates - the handoff to a replay.",
        "claim": "A bundle's verdict is computed, not asserted. live_local - 112 vitest + 15 "
                 "IA laws at 1000 trials each; every gate's verdict comes from an "
                 "InvariantArithmetic.consume call carrying law + invariant_family; "
                 "gate.authority verified live against delegatic-mcp. Toward external.",
        "bar": "A bundle taught in one browser and replayed in another person's browser, "
               "scoring the same. Separately: 23 pre-existing svelte-check diagnostics - this "
               "app is NOT typecheck-clean and must not be called so until they are gone.",
        "not_for": "Rebuilding the leaderboard, the memory or the body. It proxies PRISM, "
                   "delegatic-mcp and body-browser-mcp same-origin and should keep proxying "
                   "rather than reimplementing.",
    },
    "plugins": {
        "for": "The Claude Code skills that teach an agent how to work this stack - the "
               "handoff to an agent that has never seen it.",
        "claim": "unrecorded. Four plugins are distributed and consumed by live sessions "
                 "daily, and nothing has ever measured whether a session that loads them "
                 "works better than one that does not.",
        "bar": "Skills current with the machines they describe - last updated 2026-04-09 "
               "against Graphonomous v0.4 while the engine is at v0.4.3 and PULSE went from 5 "
               "tokens to 6 - plus one measured comparison of a task run with and without them.",
        "not_for": "Adding skills for machines that do not exist. A skill documenting an "
                   "aspirational surface teaches the agent something false.",
    },
}

D["research"] = {
    "opensentience": {
        "for": "The OS-001..011 protocol series and the proof pages that state, with a tier "
               "badge, exactly how each claim was established.",
        "claim": "Topology is the warrant for routing. kappa is MACHINE-CHECKED - exhaustive "
                 "over n=2-5 digraphs and FDS n=2-7, 1,926,351 objects, 0 counterexamples. "
                 "The end-to-end topology/routing/deliberation path is in_tree.",
        "bar": "OS-007 (Adversarial Robustness) is the one still-draft protocol and needs a "
               "real threat demo, not a spec. Separately: every proof page's tier badge must "
               "keep matching its method - \"Proved\" overstating the method is the failure "
               "the tiering was built to stop.",
        "not_for": "OS-012 and beyond. SCOPE is deferred, the wired-arrows gate is unmet, and "
                   "inventing protocols before the existing ones are implemented is named as "
                   "what NOT to prioritize.",
        "after_bar": "new_ruling",
    },
    "weave": {
        "for": "The static cost certificate feeding the kernel's resource rung - what a "
               "computation CAN spend, terminating, with an uncertifiable computation "
               "annihilating to zero.",
        "claim": "EAL cost-certificate inference is sound on the tested corpus. live_local - "
                 "reducer sound on a 12-term battery and ~3000 random STLC terms; 2010 terms "
                 "validated with 0 violations; HVM4 backend diff 6/6.",
        "bar": "The certificate consumed by box-and-box's resource rung in a real verdict, so "
               "a refusal can cite a cost it could not afford. The rung and the certificate "
               "have never met.",
        "not_for": "Becoming a second runtime. TRVM is the runtime; this is the static "
                   "analysis that hands it a bound.",
    },
}

D["showcase"] = {
    "docs": {
        "for": "The stackdocs aggregator - one site that DERIVES what the stack is from the "
               "real repositories rather than restating it.",
        "claim": "The site's facts are derived, not written. in_tree. Derivation is the whole "
                 "point: a hand-written fact here is a second copy of something that will "
                 "drift.",
        "bar": "A build that FAILS when a derived fact stops matching its source, rather than "
               "rendering a stale one.",
        "not_for": "Relocating per-project specs. Specs stay colocated with their code; this "
                   "aggregates, it does not own.",
    },
    "graphonomous.com": {
        "for": "The public explanation of Graphonomous.",
        "represents": "core/graphonomous",
        "claim": "none. A presentation surface makes no claim about the engine and must never "
                 "be read as moving one.",
        "bar": "Every claim on the page matches core/graphonomous's recorded rung. A page "
               "saying \"shipped\" about something at in_tree is rule 2 breaking in public.",
        "not_for": "Benchmarks, live demos, or any number the engine lane has not earned.",
        "after_bar": "maintain",
    },
    "delegatic.com": {
        "for": "The public explanation of Delegatic and OS-006.",
        "represents": "core/delegatic",
        "claim": "none.",
        "bar": "Every claim matches core/delegatic's recorded rung - in particular that v0.1 "
               "is the authorization kernel with an in-memory store, NOT the "
               "org-tree/effective-policy system the 630-line spec describes. That gap is the "
               "one this page is most likely to paper over.",
        "not_for": "Describing the spec as if it were the implementation.",
        "after_bar": "maintain",
    },
}

# The ten demotions. Every one is a decision (rule 6) and every one is listed in
# section 5 of WORKSPACE_DIRECTION.md with its grounds. `webhost` is here at
# `frozen` so the run states it deliberately rather than leaving it to be read
# as an omission.
MODES = {
    "core": {"supabase": "maintain"},
    "products": {"runefort": "maintain"},
    "compose": {"agentelic": "maintain", "specprompt": "maintain",
                "agentromatic": "maintain", "deliberatic": "maintain",
                "geofleetic": "maintain", "ticktickclock": "maintain",
                "webhost": "frozen"},
    "academy": {"plugins": "maintain"},
}


def main():
    started = amp.current_workspace()
    print(f"\n[&] applying WORKSPACE_DIRECTION.md rev 2"
          f"{' (DRY RUN, nothing written)' if DRY else ''}\n")

    # This run switches workspace repeatedly, and a switch rebinds every path in
    # the harness under whatever is already running. Refusing up front is the
    # same refusal `use_workspace` makes, made before anything has been written
    # rather than a third of the way through.
    why = amp.switch_blocked()
    if why and not DRY:
        print(f"  REFUSED: {why}. Nothing was written.\n")
        return 1

    # 1. workspaces -----------------------------------------------------------
    print("workspaces")
    for slug in ("substrate", "showcase"):
        if slug in amp.workspaces()["workspaces"]:
            say(False, f"`{slug}` already exists")
            continue
        if not DRY:
            amp.add_workspace(slug, slug=slug)
        PLANNED.add(slug)
        say(True, f"created `{slug}`")

    # `demo-development` is dropped rather than renamed because a slug is a
    # directory name and renaming one would move state. It has no config, no
    # lanes and no goals - only a chat file - so there is nothing to move, and
    # `remove_workspace` leaves the directory on disk untouched either way.
    if "demo-development" in amp.workspaces()["workspaces"]:
        if not DRY:
            amp.remove_workspace("demo-development")
        say(True, "dropped `demo-development` (empty; its directory is left on disk)")

    for slug, text in MISSIONS.items():
        if not ws_exists(slug):
            say(False, f"no workspace {slug!r} - mission not written")
            continue
        if not DRY:
            amp.set_mission(text, slug)
        say(True, f"mission written for `{slug}` ({len(text)} chars)")

    # 2. lane moves -----------------------------------------------------------
    print("\nlane moves")
    for lane, src, dst in MOVES:
        if lane not in lanes_of(src):
            in_dst = lane in lanes_of(dst)
            say(False, f"{lane}: not in {src}"
                       + (f" - already in {dst}" if in_dst else " - and not in the target either"))
            continue
        if DRY:
            say(True, f"{lane}: {src} -> {dst}")
            continue
        # `move_lane` moves a lane OUT OF THE CURRENT WORKSPACE and checks
        # `from_slug` against the registry's `current`, not against the lane. So
        # the harness has to be standing in the source before each move, and the
        # three inbound moves would otherwise be refused - correctly, and for a
        # reason that reads like a bug in the mover rather than in the caller.
        was = amp.current_workspace()
        try:
            amp.use_workspace(src)
            r = amp.move_lane(lane, dst, from_slug=src)
            say(True, f"{lane}: {src} -> {dst}"
                      + (f", {r.get('goals', 0)} goal(s) carried" if r.get("goals") else "")
                      + (f", worktree moved" if r.get("worktree") else "")
                      + (f" — LEFT BEHIND: {r['left_behind']}" if r.get("left_behind") else ""))
        except Exception as e:  # noqa: BLE001 - a refused move is reportable, not fatal
            say(False, f"{lane}: REFUSED - {e}")
        finally:
            amp.use_workspace(was)

    # 3. the two unowned presentation repos -----------------------------------
    print("\nnew lanes")
    for name, path, slug in NEW_LANES:
        if name in lanes_of(slug):
            say(False, f"{name}: already a lane in {slug}")
            continue
        if DRY:
            say(True, f"{name}: would register {path} in {slug}")
            continue
        was = amp.current_workspace()
        try:
            amp.use_workspace(slug)
            amp.add_lane(name, path=path)
            say(True, f"{name}: registered {path} in {slug}")
        except Exception as e:  # noqa: BLE001 - report, do not swallow
            say(False, f"{name}: REFUSED - {e}")
        finally:
            amp.use_workspace(was)

    # 4. directions and modes, inside each workspace --------------------------
    print("\ndirections")
    for slug, lanes in D.items():
        if not ws_exists(slug):
            say(False, f"no workspace {slug!r} - {len(lanes)} direction(s) not written")
            continue
        was = amp.current_workspace()
        try:
            if not DRY:
                amp.use_workspace(slug)
            have = lanes_of(slug)
            for lane, d in lanes.items():
                if lane not in have:
                    say(False, f"{slug}/{lane}: no such lane here - direction not written")
                    continue
                if not DRY:
                    amp.set_lane_direction(lane, d)
                say(True, f"{slug}/{lane}: " + ", ".join(sorted(d)))
        finally:
            if not DRY:
                amp.use_workspace(was)

    print("\nmodes")
    for slug, lanes in MODES.items():
        if not ws_exists(slug):
            say(False, f"no workspace {slug!r} - {len(lanes)} mode(s) not set")
            continue
        was = amp.current_workspace()
        try:
            if not DRY:
                amp.use_workspace(slug)
            have = lanes_of(slug)
            for lane, mode in lanes.items():
                if lane not in have:
                    say(False, f"{slug}/{lane}: no such lane here - mode not set")
                    continue
                if DRY:
                    say(True, f"{slug}/{lane}: -> {mode}")
                    continue
                r = amp.set_lane_mode(lane, mode)
                say(True, f"{slug}/{lane}: {r['was']} -> {r['mode']}"
                          + (f" ({len(r['in_flight'])} still running)" if r["in_flight"] else ""))
        finally:
            if not DRY:
                amp.use_workspace(was)

    if not DRY and amp.current_workspace() != started:
        amp.use_workspace(started)
    print(f"\ncurrent workspace: {amp.current_workspace()}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
