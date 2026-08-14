#!/usr/bin/env python3
"""blueprint - the whole stack drawn once, and what moves between its layers.

Three questions this answers that no other screen in the console does:

  HOW IT STACKS.  Every pane elsewhere shows one lane. This shows all of them at
  once, on two axes that mean different things. The rows are the evidence ladder
  and are DERIVED - a lane sits at the rung its reviews actually awarded it, and
  nothing here can move it, because a diagram that could promote a lane would be
  a second, editable copy of the one number the whole harness scores against.
  The columns are a stack the operator authors and names, because how these
  lanes layer is a design decision and there is nowhere else on disk it is
  written down.

  WHAT FIRES BETWEEN THEM.  A trigger is a condition on one lane and an
  objective for another. It never starts anything: when it fires it writes an
  ordinary proposal into the ordinary queue, unscored, where the bars, the
  lane's mode and the stage gate all still apply. That is the whole design.
  Everything else in this harness that starts work does so through
  `proposal_hold`, and a trigger that reached around it would be a second door
  into the fleet with none of the locks on it.

  WHAT EACH ACTION IS ACTUALLY SENT.  For every model call this harness makes,
  build its real context right now and show every block in it: what produced the
  block, what that block read to say what it says, how big it is, and the exact
  bytes. Nothing here is a hand-drawn map of the code - the blocks record
  themselves as they are built (`amp.trace_blocks`), so a block added tomorrow
  appears here tomorrow without anyone updating a diagram.

No model is called anywhere in this file. Building a context is reading; sending
it is not, and this screen only ever reads.
"""

from __future__ import annotations

import re
import threading
import uuid

import amp

_LOCK = threading.Lock()

_EMPTY = {"stacks": [], "placement": {}, "triggers": [], "fired": []}


def store() -> dict:
    d = amp.load_json(amp.BLUEPRINT_PATH, dict(_EMPTY))
    for k, v in _EMPTY.items():
        d.setdefault(k, type(v)())
    return d


def _save(d: dict):
    # The fired log is the only unbounded thing here and it is evidence, not
    # state: it is how you tell a trigger that has never fired from one that
    # fires every tick. Trimmed rather than dropped.
    d["fired"] = (d.get("fired") or [])[-400:]
    amp.save_json(amp.BLUEPRINT_PATH, d)


# ------------------------------------------------------------------ the layers
#
# Two axes, and they are not the same kind of thing.

# DERIVED. The rung a lane's evidence has been judged to reach, newest reviews
# first. `amp.LADDER_RUNGS` is the ladder itself; a lane no review has ever
# moved has no rung at all, which is a fact and not a zero.
RUNGS = amp.LADDER_RUNGS

# AUTHORED. The fallback names, used only when the architect is off. Four nouns
# chosen before anyone had read this portfolio: a starting point, not a taxonomy
# the harness believes in, and it says so when it offers them.
SUGGESTED_STACKS = (
    ("substrate", "What everything else is built on. Storage, identity, the runtime."),
    ("engine", "The thing that does the work. Where the real code lives."),
    ("protocol", "The specs and schemas the engines have to agree on."),
    ("surface", "What a person or another program actually touches."),
)


def _stacks(d: dict) -> list[dict]:
    return [s for s in (d.get("stacks") or []) if s.get("id")]


def add_stack(name: str, note: str = "") -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("a layer needs a name")
    with _LOCK:
        d = store()
        if any(s.get("name", "").lower() == name.lower() for s in _stacks(d)):
            raise ValueError(f"there is already a layer called {name!r}")
        s = {"id": "s" + uuid.uuid4().hex[:8], "name": name[:60],
             "note": (note or "").strip()[:400]}
        d.setdefault("stacks", []).append(s)
        _save(d)
    return s


def rename_stack(sid: str, name: str, note: str | None = None) -> dict:
    with _LOCK:
        d = store()
        s = next((x for x in _stacks(d) if x["id"] == sid), None)
        if not s:
            raise ValueError(f"no layer {sid!r}")
        if (name or "").strip():
            s["name"] = name.strip()[:60]
        if note is not None:
            s["note"] = note.strip()[:400]
        _save(d)
    return s


def remove_stack(sid: str) -> dict:
    """Drop a layer. Its lanes become unplaced; nothing about them changes."""
    with _LOCK:
        d = store()
        d["stacks"] = [x for x in _stacks(d) if x["id"] != sid]
        d["placement"] = {k: v for k, v in (d.get("placement") or {}).items() if v != sid}
        # A trigger scoped to a layer that no longer exists would match nothing
        # and say nothing about why, which is worse than being switched off. It
        # is switched off and left in place, named, for the operator to repoint.
        for t in d.get("triggers") or []:
            for side in ("from", "to"):
                ref = t.get(side) or {}
                if ref.get("kind") == "stack" and ref.get("id") == sid:
                    t["on"] = False
                    t["broken"] = f"the layer this trigger's `{side}` pointed at was removed"
        _save(d)
    return {"ok": True, "id": sid}


def place(lane: str, sid: str | None) -> dict:
    """Put a lane in a layer, or take it out of all of them."""
    with _LOCK:
        d = store()
        if lane not in (amp.config().get("lanes") or {}):
            raise ValueError(f"unknown lane {lane!r}")
        if sid and not any(x["id"] == sid for x in _stacks(d)):
            raise ValueError(f"no layer {sid!r}")
        p = d.setdefault("placement", {})
        if sid:
            p[lane] = sid
        else:
            p.pop(lane, None)
        _save(d)
    return {"ok": True, "lane": lane, "stack": sid}


# ------------------------------------------------------------ drafting layers
#
# `SUGGESTED_STACKS` is four nouns chosen before anyone had read this portfolio.
# This asks the architect to name the layers off what the lanes themselves say
# they are for - and then refuses to believe it about anything checkable. A lane
# it names has to BE a lane, and a lane it forgets is reported as forgotten
# rather than quietly dropped, because a layering that silently omits half the
# workspace looks exactly like a layering that covers it.
#
# It drafts. It never writes. Layers feed triggers and triggers write proposals,
# so a model that got to install its own taxonomy here would be shaping what
# work gets proposed, one step removed and unattributed.


STACK_DRAFT_SYSTEM = """\
You are naming the LAYERS of a software portfolio.

A layer is a horizontal band. Two lanes belong in the same layer when they play \
the same ROLE with respect to the others - not when they use the same language, \
and not when they happen to be worked on together.

You are given what the workspace is for, every lane in its own words (what it is \
for, the bet it makes, what it does NOT know, and the evidence rung its claims \
have actually been judged to reach), the relationships between lanes that are \
already facts on disk, and any layers the operator has already drawn.

Return ONE JSON object and nothing else:

{
  "layers": [
    {
      "name":  "one or two words. the ROLE, not the technology.",
      "note":  "one sentence: what belongs in this layer, and what does not.",
      "lanes": [
        {"lane": "<the lane name, exactly as given>",
         "why":  "one clause. why THIS lane sits at THIS layer."}
      ]
    }
  ],
  "unplaced": [
    {"lane": "<lane name>",
     "why":  "what the evidence does not say, and what you would need to know."}
  ]
}

Rules:

- Use lane names EXACTLY as they are given to you. Do not invent one, do not \
abbreviate, do not fix the capitalisation, do not pluralise.
- Every lane must appear exactly ONCE, counting `layers` and `unplaced` together.
- Order the layers bottom up: whatever the rest is built on comes first.
- Three to six layers is usually right. A layer holding one lane is fine when \
that lane really is alone at its level. A layer holding almost everything is not \
a layer, it is a shrug.
- If two lanes describe the same job, do not paper over it. Put them in the same \
layer and say so plainly in the `why`.
- Prefer `unplaced` to a confident guess. A named gap is worth more here than a \
placement the evidence does not support, and the operator can settle it in one \
click. Say what you would need to know.
- Do not rank the lanes by how finished they are. The rung is already a row on \
this screen; a layer is about role, and a lane with no rung at all still has one.
"""


@amp.traced_block("config.json (every lane's `direction`, `mode` and `stage`)",
                  ".direction.json (reviews, so the judged rung)")
def _lanes_block(nodes: list[dict]) -> str:
    """Every lane in its own words, plus the rung its evidence was judged to reach."""
    if not nodes:
        return ""
    rows = []
    for n in nodes:
        # The rung is stated as absent rather than omitted. "No review has ever
        # moved a claim here" is a fact about the lane, and a drafter that saw
        # nothing would be free to assume the bottom of the ladder instead.
        rung = n.get("rung") or "no review has ever moved a claim in this lane"
        bits = [f"## {n['lane']}",
                f"- path on disk: `{n.get('path') or '.'}`",
                f"- mode: {n.get('mode')} \u00b7 stage: {n.get('stage')} \u00b7 judged rung: {rung}"]
        for key, label in (("for", "For"), ("thesis", "The bet"),
                           ("unknown", "What it does not know")):
            v = (n.get(key) or "").strip()
            if v:
                bits.append(f"- **{label}:** {v}")
        if not any((n.get(k) or "").strip() for k in ("for", "thesis", "unknown")):
            bits.append("- _no direction has been written for this lane_")
        rows.append("\n".join(bits))
    return ("# Every lane, in its own words\n\n"
            "These are the only lane names that exist. Use them exactly.\n\n"
            + "\n\n".join(rows))


@amp.traced_block("config.json (each lane's `path`, resolved on disk)",
                  "config.json (each lane's `direction.represents`)")
def _edges_block(edges: list[dict]) -> str:
    """The relationships that are already facts, which a layering has to survive."""
    if not edges:
        return ""
    rows = [f"- `{e['from']}` \u2192 `{e['to']}` \u2014 {e.get('why') or e.get('kind')}"
            for e in edges]
    return ("# What is already true between them\n\n"
            "Read off disk and off the directions, not authored on this screen. A "
            "layering that puts a container above the thing it contains is "
            "probably wrong, and should at least say why.\n\n" + "\n".join(rows))


@amp.traced_block(".blueprint.json (layers the operator has already drawn)")
def _current_block(stacks: list[dict]) -> str:
    """Layers that already exist, so a redraft argues with them instead of ignoring them."""
    if not stacks:
        return ""
    rows = [f"- **{s.get('name')}** \u2014 {s.get('note') or '_no note_'}" for s in stacks]
    return ("# Layers the operator has already drawn\n\n"
            "You are redrafting. Keep a name that is working, and when you change "
            "one, the `note` should make it obvious what moved.\n\n" + "\n".join(rows))


def _stack_draft_context() -> str:
    """The real message the layer drafter is sent."""
    m = map_view()
    parts = [amp.mission_block(),
             _lanes_block(m.get("nodes") or []),
             _edges_block(m.get("edges") or []),
             _current_block(m.get("stacks") or [])]
    return "\n\n".join(p for p in parts if p)


def _check_draft(out: dict) -> dict:
    """Hold the answer against the lanes that actually exist.

    Three separate things can be wrong and they are reported separately, because
    they mean different things: a lane that does not exist is the model making
    something up, a lane named twice is it contradicting itself, and a lane never
    mentioned is a hole it did not notice. Only the last one is recoverable by
    itself, and it is recovered by SHOWING the hole - the lane is added to
    `unplaced` saying nobody placed it, which is the truth.
    """
    real = set(amp.config().get("lanes") or {})
    layers, invented, duplicated, seen = [], [], [], set()

    def take(name: str) -> str | None:
        name = str(name or "").strip()
        if name not in real:
            invented.append(name)
            return None
        if name in seen:
            duplicated.append(name)
            return None
        seen.add(name)
        return name

    for lyr in (out.get("layers") or [])[:12]:
        if not isinstance(lyr, dict):
            continue
        name = str(lyr.get("name") or "").strip()
        if not name:
            continue
        lanes = []
        for row in (lyr.get("lanes") or [])[:60]:
            row = row if isinstance(row, dict) else {"lane": row}
            got = take(row.get("lane"))
            if got:
                lanes.append({"lane": got, "why": str(row.get("why") or "").strip()[:300]})
        layers.append({"name": name[:60], "note": str(lyr.get("note") or "").strip()[:400],
                       "lanes": lanes})

    unplaced = []
    for row in (out.get("unplaced") or [])[:60]:
        row = row if isinstance(row, dict) else {"lane": row}
        got = take(row.get("lane"))
        if got:
            unplaced.append({"lane": got, "why": str(row.get("why") or "").strip()[:300],
                             "missed": False})
    for name in sorted(real - seen):
        unplaced.append({"lane": name, "missed": True,
                         "why": "the drafter did not mention this lane at all, so nobody "
                                "has said where it belongs"})

    return {"layers": layers, "unplaced": unplaced,
            "invented": sorted(set(invented)), "duplicated": sorted(set(duplicated))}


def _preset_draft(why: str) -> dict:
    """The four generic names, offered as a draft so the review step still applies.

    Every lane comes back unplaced, which is correct: nothing read them. The
    reason is rewritten because "the drafter did not mention this lane" would be
    blaming a drafter that never ran.
    """
    d = _check_draft({"layers": [{"name": n, "note": w, "lanes": []}
                                 for n, w in SUGGESTED_STACKS]})
    for row in d["unplaced"]:
        row["missed"] = False
        row["why"] = "nothing read this lane - these are generic names, so put it where you think it goes"
    return {"ok": True, "source": "preset", "note": why, **d}


def draft_stacks() -> dict:
    """Propose the layers off what the lanes say they are for. Writes nothing.

    Returns the draft for the operator to edit and accept. Accepting is a
    separate, explicit act through `apply_stacks`.
    """
    if not (amp.config().get("lanes") or {}):
        # Caught here rather than after the call. A model asked to layer nothing
        # correctly answers with nothing, and "the architect named no usable
        # layer" would blame it for being right.
        return {"ok": False, "error": "this workspace has no lanes yet, and a layer "
                                      "is a claim about how lanes sit relative to "
                                      "each other"}
    if not amp.architect_available():
        return _preset_draft(
            amp.architect_off_reason()
            + ", so these are the generic starting names rather than anything "
              "read off your lanes. They are a placeholder, not a reading.")

    model = amp.config().get("consult_model", amp.DEFAULT_CONSULT)
    resp = amp.architect_chat(
        [{"role": "system", "content": STACK_DRAFT_SYSTEM},
         {"role": "user", "content": _stack_draft_context()}],
        model, web=False)
    text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    out = amp._json_reply(text)
    if not out:
        return {"ok": False, "error": "the architect's answer could not be read as JSON",
                "raw": text[:2000]}
    checked = _check_draft(out)
    if not checked["layers"]:
        return {"ok": False, "error": "the architect named no usable layer",
                "raw": text[:2000], **checked}
    return {"ok": True, "source": "architect", "model": model, **checked}


def apply_stacks(layers: list[dict]) -> dict:
    """Write an accepted draft: the layers, and the lanes placed in them.

    Replaces the layers wholesale, so this is the accept button and not a merge.
    A lane the draft does not mention keeps whatever placement it already had
    only if that layer survived; otherwise it comes out, the same as if the
    layer had been removed by hand.
    """
    if not isinstance(layers, list) or not layers:
        raise ValueError("there is nothing here to accept")
    real = set(amp.config().get("lanes") or {})
    built, placement, seen_names, seen_lanes = [], {}, set(), set()
    for lyr in layers:
        if not isinstance(lyr, dict):
            continue
        name = str(lyr.get("name") or "").strip()[:60]
        if not name:
            raise ValueError("a layer needs a name")
        if name.lower() in seen_names:
            raise ValueError(f"there is more than one layer called {name!r}")
        seen_names.add(name.lower())
        sid = "s" + uuid.uuid4().hex[:8]
        built.append({"id": sid, "name": name,
                      "note": str(lyr.get("note") or "").strip()[:400]})
        for row in (lyr.get("lanes") or []):
            lane = str((row if isinstance(row, dict) else {"lane": row}).get("lane")
                       or "").strip()
            if lane not in real:
                raise ValueError(f"unknown lane {lane!r}")
            if lane in seen_lanes:
                raise ValueError(f"{lane!r} is in more than one layer")
            seen_lanes.add(lane)
            placement[lane] = sid
    if not built:
        raise ValueError("there is nothing here to accept")

    with _LOCK:
        d = store()
        # Triggers are scoped to layer IDs and every ID here is new, so any
        # trigger pointing at an old layer is switched off and named, exactly
        # as `remove_stack` does. Silently rebinding by name would be a guess
        # about which of two layers the operator meant.
        gone = {s["id"] for s in _stacks(d)}
        for t in d.get("triggers") or []:
            for side in ("from", "to"):
                ref = t.get(side) or {}
                if ref.get("kind") == "stack" and ref.get("id") in gone:
                    t["on"] = False
                    t["broken"] = (f"the layers were redrawn, so the layer this "
                                   f"trigger's `{side}` pointed at is gone")
        d["stacks"] = built
        d["placement"] = placement
        _save(d)
    return {"ok": True, "stacks": built, "placed": len(placement)}


# -------------------------------------------------------------------- the map


def _lane_dirs() -> dict[str, str]:
    """Every lane's resolved directory, for working out what contains what."""
    out = {}
    for name, ln in (amp.config().get("lanes") or {}).items():
        try:
            out[name] = str((amp.ROOT / (ln.get("path") or ".")).resolve())
        except OSError:
            continue
    return out


def map_view() -> dict:
    """Every lane, where it sits, and every edge between them that is real."""
    d = store()
    cfg = amp.config()
    lanes = cfg.get("lanes") or {}
    rungs = amp.lane_rungs()
    placement = d.get("placement") or {}
    dirs = _lane_dirs()
    by_dir = {p: n for n, p in dirs.items()}

    nodes = []
    for name in sorted(lanes):
        ln = lanes[name] or {}
        dr = amp.lane_direction(name)
        gs = amp.goals(name)
        unread = [f for f in amp.findings(lane=name, unread_only=True)]
        nodes.append({
            "lane": name,
            "path": ln.get("path") or ".",
            "repo": ln.get("repo") or "",
            "branch": ln.get("branch") or "main",
            "mode": amp.lane_mode(name),
            "stage": amp.lane_stage(name),
            # Derived, never authored. None means no review has ever moved a
            # claim in this lane, which is different from "it is at the bottom".
            "rung": rungs.get(name),
            "stack": placement.get(name),
            "for": dr.get("for", ""),
            "thesis": dr.get("thesis", ""),
            "claim": dr.get("claim", ""),
            "bar": dr.get("bar", ""),
            "unknown": dr.get("unknown", ""),
            "represents": dr.get("represents", ""),
            "goals": {
                "open": sum(1 for g in gs if g.get("state") in ("planning", "running")),
                "blocked": sum(1 for g in gs if g.get("state") == "blocked"),
                "done": sum(1 for g in gs if g.get("state") == "done"),
            },
            "findings": len(unread),
            "contradictions": sum(1 for f in unread if f.get("bearing") == "contradicted"),
            "specs": len(amp.lane_spec_files(name)),
        })

    edges = []
    for n in nodes:
        # A presentation surface naming the lane whose evidence it shows. The
        # written form may carry a path, so it is matched on the last segment -
        # the same reading `set_lane_direction` refuses a self-reference on.
        rep = (n["represents"] or "").split("/")[-1]
        if rep and rep in lanes:
            edges.append({"from": n["lane"], "to": rep, "kind": "represents",
                          "why": "presents that lane's evidence; a commit here moves no rung on it"})
        # Containment, read off the paths rather than declared. `lane_owning`
        # answers with the DEEPEST enclosing lane, so a lane three deep is drawn
        # against its actual parent and not against the workspace root.
        try:
            owner = amp.lane_owning(amp.Path(dirs[n["lane"]]), by_dir)
        except (KeyError, OSError):
            owner = None
        if owner and owner != n["lane"]:
            edges.append({"from": owner, "to": n["lane"], "kind": "contains",
                          "why": "its directory holds this one on disk"})

    return {
        "rungs": list(RUNGS),
        "stacks": _stacks(d),
        "nodes": nodes,
        "edges": edges,
        "suggest": not _stacks(d),
    }


# ----------------------------------------------------------------- the triggers
#
# What a trigger may notice. Every one of these is answerable from state already
# on this disk - no network, no model, no worker. That is the whole selection
# rule: a condition the harness cannot evaluate for itself would be a switch
# that silently never fires, which is worse than not offering it.
#
# Each returns a list of (key, note). The KEY is what makes two firings the same
# firing - a goal's id, a finding's id, the rung reached - so a trigger proposes
# once per occurrence and not once per minute.

def _ev_rung_reaches(lane: str, arg: str) -> list[tuple[str, str]]:
    got = amp.lane_rungs().get(lane)
    if not got or arg not in RUNGS:
        return []
    if RUNGS.index(got) < RUNGS.index(arg):
        return []
    return [(f"rung:{got}", f"its evidence has been judged to reach `{got}`")]


def _ev_goal_done(lane: str, arg: str) -> list[tuple[str, str]]:
    return [(f"goal:{g['id']}", f"the goal \u201c{(g.get('objective') or '')[:120]}\u201d finished")
            for g in amp.goals(lane) if g.get("state") == "done"][:6]


def _ev_contradiction(lane: str, arg: str) -> list[tuple[str, str]]:
    return [(f"finding:{f['id']}", f"a contradiction is open on it: {f.get('text', '')[:160]}")
            for f in amp.findings(lane=lane, unread_only=True)
            if f.get("bearing") == "contradicted"][:6]


def _ev_no_spec(lane: str, arg: str) -> list[tuple[str, str]]:
    if amp.lane_spec_files(lane):
        return []
    return [("nospec", "it has no design document under `docs/spec/` at all")]


def _ev_obligation_failing(lane: str, arg: str) -> list[tuple[str, str]]:
    return [(f"obligation:{o['id']}:{o.get('state')}",
             f"the standing obligation \u201c{o.get('name')}\u201d is {o.get('state')}")
            for o in amp.obligations()
            if o.get("lane") == lane and o.get("enabled")
            and o.get("state") in ("drifted", "broken")][:6]


def _ev_idle(lane: str, arg: str) -> list[tuple[str, str]]:
    gs = amp.goals(lane)
    if any(g.get("state") in ("planning", "running", "blocked") for g in gs):
        return []
    # Keyed on the last goal that settled, so this fires once when the lane goes
    # quiet rather than every tick for as long as it stays quiet.
    last = next((g["id"] for g in gs), "never")
    return [(f"idle:{last}", "nothing is open in it")]


EVENTS = {
    "rung_reaches": {
        "label": "reaches a rung",
        "what": "its evidence has been judged to reach the rung you pick, or past it",
        "arg": "rung",
        "fn": _ev_rung_reaches,
    },
    "goal_done": {
        "label": "finishes a goal",
        "what": "a goal in it closed as done",
        "arg": None,
        "fn": _ev_goal_done,
    },
    "contradiction": {
        "label": "reports a contradiction",
        "what": "the work filed a finding that something we believed and acted on is false",
        "arg": None,
        "fn": _ev_contradiction,
    },
    "no_spec": {
        "label": "has no spec",
        "what": "there is no markdown under its `docs/spec/`",
        "arg": None,
        "fn": _ev_no_spec,
    },
    "obligation_failing": {
        "label": "breaks a standing obligation",
        "what": "one of its obligations last checked as drifted or broken",
        "arg": None,
        "fn": _ev_obligation_failing,
    },
    "idle": {
        "label": "goes idle",
        "what": "nothing is open in it - no goal planning, running or blocked",
        "arg": None,
        "fn": _ev_idle,
    },
}


# -------------------------------------------------------- the workspace ports
#
# A trigger may point at a lane in ANOTHER workspace. It still does not write
# there. This harness binds one workspace's state at a time - `amp._bind_state`
# rewrites the module globals - so "put a proposal in workspace B" would mean
# either loading B underneath whatever is running in A, or hand-editing B's
# files from outside B's own locks. Both are exactly the second door this
# screen exists not to build.
#
# So firing writes a REQUEST into the sending workspace's own outbox, and
# stops. When B is next loaded it reads its siblings' outboxes and admits what
# is addressed to it as an ordinary unscored proposal, against its OWN lanes,
# its own mode, its own stage and its own bars. The gate stays with the thing
# being gated.
#
# Writing is only ever done by the workspace that owns the file: the sender
# keeps `out`, the receiver keeps `in`, and neither ever edits the other's
# ledger. Reading across workspaces is how both ends see the same queue.
#
# Neither list is trimmed. `in` is the record that stops a request being
# admitted twice, so a row dropped out of it is a duplicate proposal later;
# and both are bounded by real occurrences, which are rare.

def _empty_outbox() -> dict:
    # Built fresh every call, not a shared constant. A shallow copy of one would
    # hand the SAME lists to every workspace whose file does not exist yet, so
    # the first request appended by anybody would appear to be sitting in every
    # other empty outbox in the process - a request delivered twice, from a
    # workspace that never sent it.
    return {"out": [], "in": []}


def _outbox_path(slug: str | None = None):
    if slug is None or slug == amp.current_workspace():
        return amp.OUTBOX_PATH
    return amp.workspace_dir(slug) / ".outbox.json"


def outbox(slug: str | None = None) -> dict:
    d = amp.load_json(_outbox_path(slug), None)
    if not isinstance(d, dict):
        return _empty_outbox()
    for k, v in _empty_outbox().items():
        if not isinstance(d.get(k), list):
            d[k] = v
    return d


def _save_outbox(d: dict):
    # amp.OUTBOX_PATH and not a slug, deliberately: the only outbox this
    # process may write is the one belonging to the workspace it has loaded.
    amp.save_json(amp.OUTBOX_PATH, d)


def _ws_lanes(slug: str) -> dict:
    """The lanes another workspace has, read straight off its own config."""
    if slug == amp.current_workspace():
        return amp.config().get("lanes") or {}
    cfg = amp.load_json(amp.workspace_dir(slug) / "config.json", {}) or {}
    return cfg.get("lanes") or {}


def _port(t: dict) -> str | None:
    """The workspace a trigger fires into, if it is not this one."""
    ws = ((t.get("to") or {}).get("ws") or "").strip()
    return ws if ws and ws != amp.current_workspace() else None


def _requests_to(me: str, reg: dict) -> list[dict]:
    """Every request any other workspace has addressed to `me`."""
    out = []
    for slug in reg["workspaces"]:
        if slug == me:
            continue
        try:
            box = outbox(slug)
        except amp.WorkspaceError:
            continue
        for r in box.get("out") or []:
            if r.get("to_ws") == me:
                out.append(dict(r, from_ws=slug))
    return out


def admit_requests() -> list[dict]:
    """Take in what other workspaces have addressed to this one.

    Each becomes an ordinary UNSCORED proposal - the same thing a trigger
    inside this workspace writes, and refused by `proposal_hold` for the same
    plain reason until somebody scores it. A request naming a lane this
    workspace does not have is recorded as refused rather than dropped: the
    sending workspace can then see that its port pointed at nothing.
    """
    me = amp.current_workspace()
    reg = amp.workspaces()
    box = outbox()
    known = {r.get("request") for r in box.get("in") or []}
    waiting = [r for r in _requests_to(me, reg) if r.get("id") not in known]
    if not waiting:
        return []

    lanes = amp.config().get("lanes") or {}
    took = []
    with amp._DIRECTION_LOCK:
        ds = amp.direction_store()
        seen = {amp._norm_prompt(p.get("text", "")) for p in ds.get("proposals", [])}
        changed = False
        for r in waiting:
            row = {"request": r.get("id"), "at": amp.now(), "from_ws": r.get("from_ws"),
                   "lane": r.get("to_lane"), "objective": r.get("objective")}
            if r.get("to_lane") not in lanes:
                row.update(state="refused", why=f"this workspace has no lane "
                                                f"{r.get('to_lane')!r}")
            elif amp._norm_prompt(r.get("objective") or "") in seen:
                row.update(state="skipped", why="that objective is already in this "
                                                "workspace's queue")
            else:
                seen.add(amp._norm_prompt(r.get("objective") or ""))
                p = {"id": "p" + uuid.uuid4().hex[:9], "at": amp.now(),
                     "kind": "goal", "lane": r["to_lane"], "text": r["objective"],
                     "why": (f"Admitted from the workspace \u201c{r.get('from_ws')}\u201d: "
                             + (r.get("why") or "a blueprint port fired there."))[:1000],
                     "state": "open", "source": "blueprint",
                     "from_workspace": r.get("from_ws"), "from_request": r.get("id"),
                     # Unscored, like everything else this screen writes.
                     "confidence": None, "need": None, "cost_usd": None}
                ds.setdefault("proposals", []).append(p)
                changed = True
                row.update(state="admitted", proposal=p["id"])
            box.setdefault("in", []).append(row)
            took.append(row)
        if changed:
            amp._save_direction(ds)
    _save_outbox(box)
    return took


def ports_view() -> dict:
    """Every workspace, and what is waiting on the wire between them.

    Read-only and cross-workspace: a port is only meaningful as a pair, so the
    count of what is still waiting has to be computed from both ends at once.
    """
    reg = amp.workspaces()
    me = reg["current"]
    admitted = {}
    for slug in reg["workspaces"]:
        try:
            admitted[slug] = {r.get("request") for r in (outbox(slug).get("in") or [])}
        except amp.WorkspaceError:
            admitted[slug] = set()

    edges: dict[tuple[str, str], dict] = {}
    for slug in reg["workspaces"]:
        try:
            box = outbox(slug)
        except amp.WorkspaceError:
            continue
        for r in box.get("out") or []:
            to = r.get("to_ws")
            if to not in reg["workspaces"]:
                continue
            e = edges.setdefault((slug, to), {"from": slug, "to": to,
                                              "waiting": 0, "landed": 0})
            if r.get("id") in admitted.get(to, ()):
                e["landed"] += 1
            else:
                e["waiting"] += 1
    return {"current": me,
            "workspaces": [{"slug": s, "name": w.get("name") or s, "current": s == me}
                           for s, w in sorted(reg["workspaces"].items())],
            "edges": sorted(edges.values(), key=lambda e: (e["from"], e["to"]))}


def _ref_lanes(ref: dict, d: dict) -> list[str]:
    """Which lanes a `from`/`to` reference actually names, right now."""
    ref = ref or {}
    kind, rid = ref.get("kind"), ref.get("id")
    lanes = amp.config().get("lanes") or {}
    if kind == "lane":
        return [rid] if rid in lanes else []
    if kind == "stack":
        return sorted(n for n, s in (d.get("placement") or {}).items()
                      if s == rid and n in lanes)
    if kind == "rung":
        got = amp.lane_rungs()
        return sorted(n for n in lanes if got.get(n) == rid)
    return []


def add_trigger(body: dict) -> dict:
    src, dst = body.get("from") or {}, body.get("to") or {}
    when = str(body.get("when") or "")
    obj = str(body.get("objective") or "").strip()
    if when not in EVENTS:
        raise ValueError(f"{when!r} is not something the harness can notice")
    if not obj:
        raise ValueError("a trigger has to say what it would propose")
    if dst.get("kind") != "lane":
        # Deliberately narrower than the `from` side. A proposal belongs to one
        # lane - that is what its mode, its stage and its bars are read from -
        # so a trigger that fired into "a layer" would be a proposal with no
        # gate to answer to.
        raise ValueError("a trigger has to name one lane to propose into")
    ws = (dst.get("ws") or "").strip()
    if ws and ws not in amp.workspaces()["workspaces"]:
        raise ValueError(f"there is no workspace called {ws!r}")
    # Checked against the TARGET workspace's own config, read off its disk. A
    # port pointing at a lane that does not exist over there would be a request
    # that can never be admitted, and nothing on this side would ever say so.
    if dst.get("id") not in _ws_lanes(ws or amp.current_workspace()):
        raise ValueError(f"{dst.get('id')!r} is not a lane in "
                         + (f"the workspace {ws!r}" if ws else "this workspace"))
    arg = str(body.get("arg") or "").strip()
    if EVENTS[when]["arg"] == "rung" and arg not in RUNGS:
        raise ValueError(f"pick a rung: {', '.join(RUNGS)}")
    with _LOCK:
        d = store()
        t = {"id": "t" + uuid.uuid4().hex[:8], "at": amp.now(),
             "from": {"kind": src.get("kind") or "lane", "id": src.get("id")},
             "when": when, "arg": arg or None,
             "to": {"kind": "lane", "id": dst["id"], "ws": ws or None},
             "objective": obj[:1000],
             "why": str(body.get("why") or "").strip()[:600],
             # Off. A trigger written is a trigger being thought about; arming
             # it is a separate press, and one that has to be made after seeing
             # what it would have proposed.
             "on": False, "fires": 0, "last": None}
        if not _ref_lanes(t["from"], d):
            raise ValueError("that `from` names no lane in this workspace")
        d.setdefault("triggers", []).append(t)
        _save(d)
    return t


def set_trigger(tid: str, patch: dict) -> dict:
    with _LOCK:
        d = store()
        t = next((x for x in (d.get("triggers") or []) if x["id"] == tid), None)
        if not t:
            raise ValueError(f"no trigger {tid!r}")
        if "on" in patch:
            t["on"] = bool(patch["on"])
            if t["on"]:
                t.pop("broken", None)
        for k in ("objective", "why"):
            if k in patch:
                t[k] = str(patch[k] or "").strip()[:1000]
        _save(d)
    return t


def remove_trigger(tid: str) -> dict:
    with _LOCK:
        d = store()
        d["triggers"] = [x for x in (d.get("triggers") or []) if x["id"] != tid]
        _save(d)
    return {"ok": True, "id": tid}


def _pending(t: dict, d: dict) -> list[dict]:
    """Every occurrence this trigger currently matches that it has not proposed."""
    ev = EVENTS.get(t.get("when"))
    if not ev:
        return []
    done = {(f.get("trigger"), f.get("key")) for f in (d.get("fired") or [])}
    out = []
    for lane in _ref_lanes(t.get("from") or {}, d):
        for key, note in ev["fn"](lane, t.get("arg") or ""):
            k = f"{lane}\u0000{key}"
            if (t["id"], k) in done:
                continue
            out.append({"lane": lane, "key": k, "note": note})
    return out


def triggers_view() -> dict:
    """Every trigger, and - the point of the screen - what it would do now."""
    d = store()
    box = outbox()
    reg = amp.workspaces()
    rows = []
    for t in d.get("triggers") or []:
        pend = _pending(t, d)
        port = _port(t)
        sent = [r for r in (box.get("out") or []) if r.get("trigger") == t["id"]]
        row = {**t,
               "from_lanes": _ref_lanes(t.get("from") or {}, d),
               "event": EVENTS.get(t.get("when"), {}).get("label", t.get("when")),
               "would": pend,
               "history": [f for f in (d.get("fired") or [])
                           if f.get("trigger") == t["id"]][-8:]}
        if port:
            # Whether they LANDED is the receiving workspace's record, not ours,
            # so it is read from over there rather than assumed from here.
            try:
                seen = {r.get("request") for r in (outbox(port).get("in") or [])}
            except amp.WorkspaceError:
                seen = set()
            row["port"] = {"ws": port,
                           "name": (reg["workspaces"].get(port) or {}).get("name") or port,
                           "sent": len(sent),
                           "waiting": sum(1 for r in sent if r.get("id") not in seen)}
        rows.append(row)
    return {"triggers": rows, "events": {k: {"label": v["label"], "what": v["what"],
                                             "arg": v["arg"]}
                                         for k, v in EVENTS.items()},
            "rungs": list(RUNGS), "stacks": _stacks(d),
            "current_ws": reg["current"],
            "workspaces": [{"slug": s, "name": w.get("name") or s,
                            "lanes": sorted(_ws_lanes(s))}
                           for s, w in sorted(reg["workspaces"].items())],
            "inbox": list(reversed((box.get("in") or [])[-12:]))}


# ---------------------------------------------------------- drafting triggers
#
# The same two steps as the layers, and for a stronger reason. A trigger is a
# standing instruction to write work into a queue, so a drafted one that could
# arm itself would be a model deciding what this fleet does next, on a timer,
# with nobody having read it. Every drafted trigger arrives OFF, exactly like a
# hand-written one, and arming it is a press made after seeing what it would
# have proposed - which the Triggers pane shows before it has fired once.
#
# There is no preset fallback here, unlike the layers. Four generic layer names
# are a starting point somebody can argue with; four generic triggers would be
# guesses about a portfolio nothing had read, aimed at real lanes.

TRIGGER_DRAFT_SYSTEM = """\
You are wiring one software portfolio to itself.

A TRIGGER is a standing rule: "when <this lane> <does this>, propose <that work> \
in <that lane>". It never starts anything. When it fires it writes an unscored \
proposal into the ordinary queue, where every existing bar still applies, and a \
human still has to score and adopt it.

You are given what the workspace is for, every lane in its own words, the \
layers the operator has drawn, the relationships that are already facts on \
disk, the exact catalogue of conditions this harness can actually notice, the \
triggers that already exist, and the other workspaces this one can address.

Return ONE JSON object and nothing else:

{
  "triggers": [
    {
      "when":      "<an event id from the catalogue, exactly>",
      "arg":       "<only if that event needs one; otherwise omit>",
      "from":      {"kind": "lane|stack|rung", "id": "<name or layer name or rung>"},
      "to":        {"id": "<the lane to propose into>", "ws": "<workspace slug, or omit for this one>"},
      "objective": "the proposal text. imperative, specific, one job.",
      "why":       "one sentence: why this consequence follows from that condition."
    }
  ],
  "note": "one sentence on what you wired and what you deliberately did not."
}

Rules:

- Use event ids, lane names, layer names and workspace slugs EXACTLY as given.
- `to.id` must be a lane. If `to.ws` is given, the lane must be one of THAT \
workspace's lanes, from the list you were given.
- The `objective` is work somebody will do. "Investigate the thing" is not an \
objective; name what would be true when it is done.
- Propose FEW. Three to six good rules beat twenty. Every trigger you write is \
something that will fire again and again without anyone asking it to.
- Do not restate a trigger that already exists. If one is nearly right, say so \
in `note` rather than writing a second copy of it.
- Do not wire a lane to itself unless the two ends are genuinely different \
lanes at different layers.
- A condition you cannot see in the catalogue does not exist. Do not invent one, \
and do not describe one in prose hoping it will be understood.
"""


@amp.traced_block("blueprint.EVENTS (what the harness can actually notice)")
def _events_block() -> str:
    """The exact catalogue of conditions that can be wired, so none is invented."""
    rows = []
    for k, v in EVENTS.items():
        arg = f" \u2014 needs an `arg`: one of {', '.join(RUNGS)}" if v["arg"] else ""
        rows.append(f"- `{k}` \u2014 **{v['label']}**: {v['what']}{arg}")
    return ("# The only conditions this harness can notice\n\n"
            "Each is answerable from state already on this disk. There is no other "
            "list; a condition that is not here cannot be wired.\n\n" + "\n".join(rows))


@amp.traced_block(".blueprint.json (triggers already written)")
def _triggers_block(rows: list[dict]) -> str:
    """The wiring that already exists, so a draft adds to it instead of repeating it."""
    if not rows:
        return ""
    out = []
    for t in rows:
        to = t["to"]["id"] + (f" in workspace `{t['to']['ws']}`" if (t.get("to") or {}).get("ws") else "")
        out.append(f"- when `{(t.get('from') or {}).get('id')}` "
                   f"{EVENTS.get(t.get('when'), {}).get('label', t.get('when'))} "
                   f"\u2192 propose in `{to}`: {t.get('objective')} "
                   f"({'armed' if t.get('on') else 'off'}, fired {t.get('fires') or 0}\u00d7)")
    return ("# Triggers that already exist\n\n"
            "Do not write a second copy of one of these.\n\n" + "\n".join(out))


@amp.traced_block(".workspaces.json (the other workspaces)",
                  "each other workspace's config.json (its lanes)")
def _ports_block(me: str, reg: dict) -> str:
    """The other workspaces and their real lanes, so a port cannot be aimed at nothing."""
    rows = []
    for slug, w in sorted(reg["workspaces"].items()):
        if slug == me:
            continue
        lanes = sorted(_ws_lanes(slug))
        rows.append(f"- `{slug}` ({w.get('name') or slug}) \u2014 lanes: "
                    + (", ".join(f"`{n}`" for n in lanes) if lanes else "_none_"))
    if not rows:
        return ""
    return ("# Other workspaces this one can address\n\n"
            "A trigger with a `to.ws` writes a REQUEST rather than a proposal. The "
            "target workspace admits it against its own bars the next time it is "
            "opened. Only wire one when the consequence genuinely belongs over "
            "there.\n\n" + "\n".join(rows))


def _trigger_draft_context() -> str:
    """The real message the trigger drafter is sent."""
    m = map_view()
    d = store()
    reg = amp.workspaces()
    parts = [amp.mission_block(),
             _lanes_block(m.get("nodes") or []),
             _current_block(m.get("stacks") or []),
             _edges_block(m.get("edges") or []),
             _events_block(),
             _triggers_block(d.get("triggers") or []),
             _ports_block(reg["current"], reg)]
    return "\n\n".join(p for p in parts if p)


def _side(v) -> dict:
    """One end of a drafted trigger, whether it came back as an object or a name."""
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip():
        return {"id": v.strip()}
    return {}


def _check_triggers(out: dict) -> dict:
    """Hold each drafted trigger against what can actually be wired.

    A row that cannot be wired is REJECTED WITH ITS REASON rather than dropped.
    Silently returning four of six would look exactly like a drafter that only
    found four, and the two that were wrong are the interesting ones.
    """
    d = store()
    reg = amp.workspaces()
    me = reg["current"]
    ok, bad = [], []
    for row in (out.get("triggers") or [])[:20]:
        if not isinstance(row, dict):
            continue
        why_bad = None
        when = str(row.get("when") or "").strip()
        # A bare string where the schema asks for an object is read as a lane
        # name, because that is the only thing it could be: `kind` already
        # defaults to "lane" and `ws` already defaults to this workspace. It is
        # the shorthand, not a guess at what was meant - and rejecting it would
        # report the reason as an empty id, which names nothing.
        src = _side(row.get("from"))
        dst = _side(row.get("to"))
        ws = str(dst.get("ws") or "").strip()
        lane = str(dst.get("id") or "").strip()
        obj = str(row.get("objective") or "").strip()
        arg = str(row.get("arg") or "").strip()
        src = {"kind": str(src.get("kind") or "lane").strip(),
               "id": str(src.get("id") or "").strip()}
        if src["kind"] == "stack":
            # Layers are named in the prompt and identified by id on disk, so the
            # drafter answers with a name and this is where it becomes an id.
            hit = next((s for s in _stacks(d)
                        if (s.get("name") or "").lower() == src["id"].lower()), None)
            if hit:
                src["id"] = hit["id"]
                src["name"] = hit["name"]

        if when not in EVENTS:
            why_bad = f"{when!r} is not a condition this harness can notice"
        elif EVENTS[when]["arg"] == "rung" and arg not in RUNGS:
            why_bad = f"`{when}` needs a rung, and {arg!r} is not one"
        elif not obj:
            why_bad = "it did not say what to propose"
        elif ws and ws not in reg["workspaces"]:
            why_bad = f"there is no workspace called {ws!r}"
        elif lane not in _ws_lanes(ws or me):
            why_bad = (f"{lane!r} is not a lane in "
                       + (f"the workspace {ws!r}" if ws else "this workspace"))
        elif not _ref_lanes(src, d):
            why_bad = f"the `from` side, {src['id']!r}, names no lane right now"

        built = {"when": when, "arg": arg or None, "from": src,
                 "to": {"kind": "lane", "id": lane, "ws": ws or None},
                 "objective": obj[:1000], "why": str(row.get("why") or "").strip()[:600],
                 "event": EVENTS.get(when, {}).get("label", when)}
        if why_bad:
            bad.append({**built, "rejected": why_bad})
        else:
            ok.append({**built, "from_lanes": _ref_lanes(src, d)})
    return {"triggers": ok, "rejected": bad, "note": str(out.get("note") or "").strip()[:600]}


def draft_triggers() -> dict:
    """Propose the wiring off what the lanes and layers say. Writes nothing.

    Accepting is a separate call (`apply_triggers`), and even accepting only
    writes triggers that are OFF.
    """
    if not (amp.config().get("lanes") or {}):
        return {"ok": False, "error": "this workspace has no lanes yet, and a trigger "
                                      "is a rule about what one lane does to another"}
    if not amp.architect_available():
        # No preset. See the section note: a generic trigger is a guess aimed at
        # a real queue, which is not a starting point, it is a mess to undo.
        return {"ok": False,
                "error": amp.architect_off_reason()
                         + ", and there is no generic wiring worth offering - a "
                           "trigger is about these lanes specifically"}

    model = amp.config().get("consult_model", amp.DEFAULT_CONSULT)
    resp = amp.architect_chat(
        [{"role": "system", "content": TRIGGER_DRAFT_SYSTEM},
         {"role": "user", "content": _trigger_draft_context()}],
        model, web=False)
    text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    out = amp._json_reply(text)
    if not out:
        return {"ok": False, "error": "the architect's answer could not be read as JSON",
                "raw": text[:2000]}
    checked = _check_triggers(out)
    if not checked["triggers"] and not checked["rejected"]:
        return {"ok": False, "error": "the architect proposed no wiring at all",
                "raw": text[:2000], **checked}
    return {"ok": True, "source": "architect", "model": model, **checked}


def apply_triggers(rows: list[dict]) -> dict:
    """Write accepted drafted triggers. Every one of them arrives OFF.

    Goes through `add_trigger` rather than around it, so a drafted trigger is
    checked by exactly the code that checks a hand-written one. If that ever
    stops being true, the drafter is the way in.
    """
    if not isinstance(rows, list) or not rows:
        raise ValueError("there is nothing here to accept")
    made, failed = [], []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            made.append(add_trigger(row))
        except (ValueError, LookupError) as e:                 # noqa: PERF203
            failed.append({"objective": (row.get("objective") or "")[:120],
                           "error": str(e)})
    if not made and failed:
        raise ValueError("; ".join(f["error"] for f in failed[:3]))
    return {"ok": True, "added": len(made), "triggers": made, "failed": failed,
            "note": "every one of them is off. Arm them one at a time, after "
                    "reading what each would propose right now."}


def step() -> list[dict]:
    """Fire what is armed and matching. Writes proposals. Starts nothing.

    Called from the heartbeat. Every proposal it writes goes into the ordinary
    queue UNSCORED, which means `proposal_hold` refuses to adopt it for the
    plainest possible reason - nobody has scored it yet - and the sharpener
    picks it up on a later tick like anything else. There is no path from here
    to a running worker that does not go through the same bars as every other
    proposal in the console.

    A trigger aimed at another workspace writes a request into this workspace's
    outbox instead, and that is all it does here. See the ports section.
    """
    # First, because it is the other half of the same wire and the heartbeat is
    # the only thing that runs on a tick. Taken before `_LOCK`, on its own, so
    # the one lock order in this file is never inverted.
    admitted = admit_requests()

    out = [{"admitted": r} for r in admitted]
    with _LOCK:
        d = store()
        armed = [t for t in (d.get("triggers") or []) if t.get("on")]
        if not armed:
            return out
        box = outbox()
        posted = False
        with amp._DIRECTION_LOCK:
            ds = amp.direction_store()
            seen = {amp._norm_prompt(p.get("text", "")) for p in ds.get("proposals", [])}
            changed = False
            for t in armed:
                port = _port(t)
                for occ in _pending(t, d):
                    obj = t["objective"]
                    if port:
                        r = {"id": "r" + uuid.uuid4().hex[:9], "at": amp.now(),
                             "to_ws": port, "to_lane": t["to"]["id"], "objective": obj,
                             "why": (f"A blueprint trigger fired in "
                                     f"\u201c{amp.current_workspace()}\u201d: "
                                     f"{occ['lane']} {EVENTS[t['when']]['label']} "
                                     f"\u2014 {occ['note']}."
                                     + (f" {t['why']}" if t.get("why") else ""))[:1000],
                             "trigger": t["id"], "from_lane": occ["lane"]}
                        box.setdefault("out", []).append(r)
                        posted = True
                        t["fires"] = int(t.get("fires") or 0) + 1
                        t["last"] = {"at": r["at"], "lane": occ["lane"],
                                     "request": r["id"], "ws": port}
                        d.setdefault("fired", []).append(
                            {"trigger": t["id"], "key": occ["key"], "at": r["at"],
                             "lane": occ["lane"], "request": r["id"], "ws": port,
                             "note": occ["note"]})
                        out.append({"trigger": t["id"], "lane": occ["lane"],
                                    "into": t["to"]["id"], "ws": port,
                                    "request": r["id"], "note": occ["note"]})
                        continue
                    # The same de-duplication every other proposer answers to.
                    # A trigger that restated an objective already in the queue
                    # would be a machine arguing with the queue it writes into.
                    if amp._norm_prompt(obj) in seen:
                        d.setdefault("fired", []).append(
                            {"trigger": t["id"], "key": occ["key"], "at": amp.now(),
                             "lane": occ["lane"], "proposal": None,
                             "note": "already in the queue, so nothing was added"})
                        continue
                    seen.add(amp._norm_prompt(obj))
                    p = {"id": "p" + uuid.uuid4().hex[:9], "at": amp.now(),
                         "kind": "goal", "lane": t["to"]["id"], "text": obj,
                         "why": (f"A blueprint trigger fired: {occ['lane']} "
                                 f"{EVENTS[t['when']]['label']} \u2014 {occ['note']}."
                                 + (f" {t['why']}" if t.get("why") else ""))[:1000],
                         "state": "open", "source": "blueprint",
                         "from_trigger": t["id"], "from_lane": occ["lane"],
                         # Left unscored on purpose. See the docstring.
                         "confidence": None, "need": None, "cost_usd": None}
                    ds.setdefault("proposals", []).append(p)
                    changed = True
                    t["fires"] = int(t.get("fires") or 0) + 1
                    t["last"] = {"at": p["at"], "lane": occ["lane"], "proposal": p["id"]}
                    d.setdefault("fired", []).append(
                        {"trigger": t["id"], "key": occ["key"], "at": p["at"],
                         "lane": occ["lane"], "proposal": p["id"], "note": occ["note"]})
                    out.append({"trigger": t["id"], "lane": occ["lane"],
                                "into": t["to"]["id"], "proposal": p["id"],
                                "note": occ["note"]})
            if changed:
                amp._save_direction(ds)
        if posted:
            _save_outbox(box)
        _save(d)
    return out


# ------------------------------------------------------- what an action is sent
#
# One entry per model call this harness makes. `build` returns the real user
# message, assembled by the same function the real call uses - never a copy.
# `subject` finds the thing the call is about (a goal, a proposal, a document)
# and answers with WHICH one it picked, because a context built against an
# unnamed example is a context you cannot check.


def _latest_goal(lane: str, states: tuple[str, ...]) -> dict | None:
    for g in amp.goals(lane):
        if g.get("state") in states:
            return amp.load_goal(g["id"])
    return None


def _c_worker(lane: str) -> tuple[str, str]:
    g = _latest_goal(lane, ("running", "blocked", "done"))
    if not g:
        raise LookupError("no goal has ever been opened in this lane, so there is "
                          "nothing a worker would be sent about")
    task = next((t for t in (g.get("tasks") or []) if t.get("state") != "dropped"), None)
    if not task:
        raise LookupError(f"goal {g['id']} has no tasks, so no worker prompt exists for it")
    return amp.goal_worker_prompt(g, task), f"goal {g['id']} \u00b7 task \u201c{task['text'][:80]}\u201d"


def _c_plan(lane: str) -> tuple[str, str]:
    g = _latest_goal(lane, ("planning", "running", "blocked", "done"))
    if not g:
        raise LookupError("no goal has ever been opened in this lane")
    return amp.goal_plan_prompt(g), f"goal {g['id']}"


def _c_review(lane: str) -> tuple[str, str]:
    g = _latest_goal(lane, ("done",))
    if not g:
        raise LookupError("no goal in this lane has ever finished, and a review is "
                          "what happens when one does")
    return amp._direction_context(g, []), f"goal {g['id']}"


def _c_explore(lane: str) -> tuple[str, str]:
    return amp._explore_context(lane, web=False), f"lane {lane}, without the web"


def _c_sharpen(lane: str) -> tuple[str, str]:
    p = next((x for x in amp.proposals() if x.get("lane") == lane), None)
    if not p:
        raise LookupError("there is no open proposal in this lane to sharpen")
    return amp._sharpen_context(p), f"proposal {p['id']}"


def _c_direction_draft(lane: str) -> tuple[str, str]:
    return amp._direction_draft_context(lane), f"lane {lane}"


def _c_spec_rate(lane: str) -> tuple[str, str]:
    docs = amp.lane_spec_files(lane)
    if not docs:
        raise LookupError("this lane has no document under `docs/spec/` to rate")
    doc = docs[0]
    body = amp.Path(doc["abs"]).read_text() if doc.get("abs") else ""
    return amp._spec_rate_context(lane, doc["rel"], body, False), f"{lane}/{doc['rel']}"


def _c_case(lane: str) -> tuple[str, str]:
    return amp._case_context(lane), f"lane {lane}"


def _c_supervise(_lane: str) -> tuple[str, str]:
    return amp._supervisor_context(), "the whole workspace"


def _c_doctrine(_lane: str) -> tuple[str, str]:
    return amp._doctrine_context(), "the whole workspace"


def _c_settle(_lane: str) -> tuple[str, str]:
    rows = [f for f in amp.findings(unread_only=True) if f.get("bearing") == "contradicted"]
    if not rows:
        raise LookupError("there is no open contradiction, and this call only runs on one")
    return amp._settle_context(rows[:24]), f"{len(rows[:24])} open contradiction(s)"


def _c_stack_draft(_lane: str) -> tuple[str, str]:
    return _stack_draft_context(), "the whole workspace"


def _c_trigger_draft(_lane: str) -> tuple[str, str]:
    return _trigger_draft_context(), "the whole workspace"


def _c_solve(_lane: str) -> tuple[str, str]:
    r = amp.last_report()
    if not r:
        raise LookupError("no report has been taken in this workspace yet")
    return amp._solver_context(r), f"report {r.get('id')}"


ACTIONS = (
    {"id": "worker", "title": "Worker, sent at one task",
     "what": "The prompt a Claude or Codex worker is actually given. The one that writes code.",
     "scope": "lane", "build": _c_worker},
    {"id": "plan", "title": "Architect, planning a goal",
     "what": "Turns one objective into a definition of done and a task list.",
     "scope": "lane", "build": _c_plan},
    {"id": "review", "title": "Architect, reviewing a finished goal",
     "what": "Answers 'and then what' down one lane, and awards the rungs.",
     "scope": "lane", "build": _c_review},
    {"id": "explore", "title": "Architect, exploring across lanes",
     "what": "Reads every lane at once and looks for what a single-lane review cannot see.",
     "scope": "lane", "build": _c_explore},
    {"id": "sharpen", "title": "Architect, sharpening a proposal",
     "what": "Re-scores a held proposal and tries to write a better version of it.",
     "scope": "lane", "build": _c_sharpen},
    {"id": "direction_draft", "title": "Architect, drafting what a lane is for",
     "what": "Proposes the lane's direction fields. It drafts; it never saves.",
     "scope": "lane", "build": _c_direction_draft},
    {"id": "spec_rate", "title": "Reviewer, rating a design document",
     "what": "Judges how solid one spec is and names what is missing from it.",
     "scope": "lane", "build": _c_spec_rate},
    {"id": "case", "title": "Architect, making the case for a lane",
     "what": "Argues what this lane is worth against the rest of the stack.",
     "scope": "lane", "build": _c_case},
    {"id": "supervise", "title": "Supervisor, holding the workspace to its mission",
     "what": "Reads everything and says whether this is still the mission. Recommends only.",
     "scope": "workspace", "build": _c_supervise},
    {"id": "doctrine", "title": "Architect, reviewing the doctrine",
     "what": "Reads the rules every agent here is held to.",
     "scope": "workspace", "build": _c_doctrine},
    {"id": "settle", "title": "Architect, settling contradictions",
     "what": "Decides what to do about claims the work has found false.",
     "scope": "workspace", "build": _c_settle},
    {"id": "solve", "title": "Architect, solving a report",
     "what": "Reads a report's own numbers back and proposes against them.",
     "scope": "workspace", "build": _c_solve},
    {"id": "stack_draft", "title": "Architect, drafting the layers",
     "what": "Names the layers off what the lanes say they are for, on this screen. "
             "It drafts; it never saves.",
     "scope": "workspace", "build": _c_stack_draft},
    {"id": "trigger_draft", "title": "Architect, drafting the wiring",
     "what": "Proposes triggers between the lanes, on this screen. It drafts; it "
             "never saves, and what it drafts arrives disarmed.",
     "scope": "workspace", "build": _c_trigger_draft},
)

_ACTION = {a["id"]: a for a in ACTIONS}

_HEAD = re.compile(r"(?m)^#\s+(.+)$")


def _sections(text: str) -> list[tuple[int, int, str]]:
    """The prompt cut at its top-level headings. Everything before the first is
    its own section, because a preamble is content too and the failure worth
    catching is a block that is entirely preamble."""
    marks = [(m.start(), m.group(1).strip()) for m in _HEAD.finditer(text)]
    if not marks:
        return [(0, len(text), "(no headings)")] if text.strip() else []
    out = []
    if text[:marks[0][0]].strip():
        out.append((0, marks[0][0], "(before the first heading)"))
    for i, (pos, title) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        out.append((pos, end, title))
    return out


def context_view(action: str, lane: str | None) -> dict:
    """Build one action's real context and take it apart.

    The blocks are not matched by name. They are recorded as they are built and
    then LOCATED in the finished prompt, so a section that no block claims is
    reported as assembled inline rather than attributed to whichever block
    looked closest. Getting that wrong is the only way this screen could lie.
    """
    a = _ACTION.get(action)
    if not a:
        return {"ok": False, "error": f"no action {action!r}"}
    lane = lane or ""
    if a["scope"] == "lane" and lane not in (amp.config().get("lanes") or {}):
        return {"ok": False, "error": "pick a lane - this call is made about one"}
    try:
        with amp.trace_blocks() as rec:
            text, subject = a["build"](lane)
    except LookupError as e:
        return {"ok": False, "error": str(e), "action": action}
    except Exception as e:                                        # noqa: BLE001
        return {"ok": False, "error": f"building it raised {type(e).__name__}: {e}",
                "action": action}

    # Where each recorded block ended up in the finished prompt. A block whose
    # text is not in there was built and then dropped by the caller - which is a
    # real thing that happens (a block is built, comes back empty, and the
    # caller appends nothing) and is worth showing as exactly that.
    spans = []
    for r in rec:
        body = r["text"].strip()
        i = text.find(body) if body else -1
        spans.append({"fn": r["fn"], "why": r["why"], "reads": r["reads"],
                      "chars": len(body), "start": i,
                      "end": (i + len(body)) if i >= 0 else -1})

    # Headings are how the prompt is cut, but they are not what the prompt is
    # MADE of: a block that quotes design documents carries their headings along
    # with it, and splitting there would report one block as six. So sections are
    # attributed first and then contiguous sections from the same source are put
    # back together. The heading count is kept, because "this one block is six
    # documents deep" is the thing an operator is scrolling to find out.
    runs: list[dict] = []
    for start, end, title in _sections(text):
        best, overlap = None, 0
        for s in spans:
            if s["start"] < 0:
                continue
            ov = min(end, s["end"]) - max(start, s["start"])
            if ov > overlap:
                best, overlap = s, ov
        src = best["fn"] if best else None
        if runs and runs[-1]["src"] == src and src is not None:
            runs[-1]["end"] = end
            runs[-1]["heads"] += 1
        else:
            runs.append({"src": src, "best": best, "start": start, "end": end,
                         "heading": title, "heads": 1})

    blocks = []
    for r in runs:
        body = text[r["start"]:r["end"]]
        best = r["best"]
        blocks.append({
            "heading": r["heading"],
            "headings": r["heads"],
            "chars": len(body),
            # Said as an estimate because it is one. A real count needs the
            # tokenizer of whichever model this call goes to, and there are four.
            "tokens_est": round(len(body) / 4),
            "from": best["fn"] if best else f"assembled inline by {a['build'].__name__}",
            "shared": bool(best),
            "why": best["why"] if best else "written into this call and nowhere else",
            "reads": best["reads"] if best else [],
            "text": body,
        })

    dropped = [{"fn": s["fn"], "why": s["why"], "reads": s["reads"]}
               for s in spans if s["start"] < 0]
    return {
        "ok": True, "action": action, "lane": lane, "subject": subject,
        "title": a["title"], "what": a["what"],
        "chars": len(text), "tokens_est": round(len(text) / 4),
        "blocks": blocks,
        # Every block that was built and did not make it in. Almost always this
        # is a block that had nothing to say, and that is the sentence worth
        # reading: "the mission is blank" beats a prompt that quietly has no
        # mission in it.
        "empty": dropped,
    }


def actions_view() -> dict:
    return {"actions": [{k: v for k, v in a.items() if k != "build"} for a in ACTIONS]}


# ------------------------------------------------------------------- the flow
#
# One canvas, three lenses. The lenses are the three panes that already exist,
# which is the point: this draws the SAME things the lists draw, from the same
# functions, so a graph and a list can never disagree about what is wired.
# Nothing here reads state that the panes do not - it reshapes it.
#
# The elements come out already in cytoscape's shape. The alternative was a
# neutral shape plus a translator in the browser, which is one more place for
# the picture to drift from the data.


def _flow_map(_arg: dict) -> dict:
    m = map_view()
    els, seen_stack = [], set()
    for s in m["stacks"]:
        els.append({"data": {"id": "L:" + s["id"], "label": s["name"],
                             "note": s.get("note") or ""}, "classes": "layer"})
        seen_stack.add(s["id"])
    loose = [n for n in m["nodes"] if n.get("stack") not in seen_stack]
    if loose:
        els.append({"data": {"id": "L:none", "label": "not in a layer",
                             "note": "no layer has been drawn for these yet"},
                    "classes": "layer loose"})
    for n in m["nodes"]:
        sid = n.get("stack") if n.get("stack") in seen_stack else ("none" if loose else None)
        els.append({"data": {
            "id": "N:" + n["lane"], "label": n["lane"],
            "parent": ("L:" + sid) if sid else None,
            "rung": n.get("rung") or "",
            "mode": n.get("mode"), "stage": n.get("stage"),
            "open": n["goals"]["open"], "blocked": n["goals"]["blocked"],
            "findings": n.get("findings") or 0,
            "tip": (n.get("for") or "")[:240],
        }, "classes": "lane" + (" unjudged" if not n.get("rung") else "")})
    for e in m["edges"]:
        els.append({"data": {"id": f"E:{e['kind']}:{e['from']}:{e['to']}",
                             "source": "N:" + e["from"], "target": "N:" + e["to"],
                             "label": e["kind"], "tip": e.get("why") or ""},
                    "classes": e["kind"]})
    return {"elements": els,
            "legend": [{"k": "layer", "t": "a layer the operator drew"},
                       {"k": "contains", "t": "its directory holds the other on disk"},
                       {"k": "represents", "t": "presents that lane's evidence"},
                       {"k": "unjudged", "t": "no review has ever moved a claim here"}],
            "empty": "no lanes in this workspace yet" if not m["nodes"] else ""}


def _flow_triggers(_arg: dict) -> dict:
    v = triggers_view()
    m = map_view()
    els, have = [], set()

    def lane_node(name: str):
        if name in have:
            return
        have.add(name)
        n = next((x for x in m["nodes"] if x["lane"] == name), None)
        els.append({"data": {"id": "N:" + name, "label": name,
                             "rung": (n or {}).get("rung") or "",
                             "tip": ((n or {}).get("for") or "")[:240]},
                    "classes": "lane"})

    for n in m["nodes"]:
        lane_node(n["lane"])

    ports = set()
    for t in v["triggers"]:
        cls = "trig " + ("armed" if t.get("on") else "off")
        if t.get("broken"):
            cls += " broken"
        port = (t.get("port") or {})
        if port:
            pid = "W:" + port["ws"]
            if port["ws"] not in ports:
                ports.add(port["ws"])
                els.append({"data": {"id": pid, "label": port["name"],
                                     "tip": "another workspace. A trigger into it "
                                            "writes a request; that workspace admits "
                                            "it against its own bars."},
                            "classes": "port"})
            tgt, extra = pid, f" \u2192 {t['to']['id']}"
        else:
            lane_node(t["to"]["id"])
            tgt, extra = "N:" + t["to"]["id"], ""
        wait = port.get("waiting") or 0
        label = t.get("event") or t.get("when")
        if wait:
            label += f" \u00b7 {wait} waiting"
        for src in (t.get("from_lanes") or []):
            lane_node(src)
            els.append({"data": {
                "id": f"T:{t['id']}:{src}", "source": "N:" + src, "target": tgt,
                "label": label + extra,
                "tip": t.get("objective") or "",
                "would": len(t.get("would") or []),
            }, "classes": cls + (" waiting" if wait else "")})
    return {"elements": els,
            "legend": [{"k": "armed", "t": "armed - it will fire on the next tick"},
                       {"k": "off", "t": "written but not armed. It does nothing."},
                       {"k": "port", "t": "another workspace. Requests wait there "
                                          "until it is opened."}],
            "empty": "no triggers written yet" if not v["triggers"] else ""}


def _flow_context(arg: dict) -> dict:
    action = (arg.get("action") or "").strip()
    if not action:
        return {"elements": [], "legend": [], "empty": "pick an action"}
    c = context_view(action, (arg.get("lane") or "").strip() or None)
    if not c.get("ok"):
        return {"elements": [], "legend": [], "empty": c.get("error") or "it refused"}
    els = [{"data": {"id": "A:call", "label": c["title"],
                     "tip": f"{c['chars']:,} chars \u00b7 ~{c['tokens_est']:,} tokens "
                            f"\u00b7 about {c.get('subject') or 'nothing in particular'}"},
            "classes": "call"}]
    for i, b in enumerate(c["blocks"]):
        bid = f"B:{i}"
        els.append({"data": {"id": bid, "label": b["heading"], "chars": b["chars"],
                             "tip": b["why"], "reads": ", ".join(b["reads"])},
                    "classes": "block" + ("" if b["shared"] else " inline")})
        els.append({"data": {"id": f"BE:{i}", "source": bid, "target": "A:call",
                             "label": f"{b['chars']:,}", "tip": b["why"]},
                    "classes": "feeds"})
    for i, e in enumerate(c.get("empty") or []):
        els.append({"data": {"id": f"X:{i}", "label": e["fn"], "chars": 0,
                             "tip": e["why"], "reads": ", ".join(e["reads"])},
                    "classes": "block empty"})
        els.append({"data": {"id": f"XE:{i}", "source": f"X:{i}", "target": "A:call",
                             "label": "nothing", "tip": "built, and produced nothing"},
                    "classes": "feeds empty"})
    return {"elements": els,
            "legend": [{"k": "block", "t": "a shared builder - other calls get it too"},
                       {"k": "inline", "t": "written into this call and nowhere else"},
                       {"k": "empty", "t": "built and produced nothing, so it is "
                                           "invisible in the prompt itself"}],
            "empty": ""}


# --------------------------------------------------------------- the agents
#
# Who does what, drawn as the pipeline they do it in, with the switches that
# govern each rung sitting on the rung itself.
#
# Every control on this lens posts to an endpoint that already existed - the
# role switches from Settings, the mode and the ceiling from Lanes, the spec
# loop from Specs, the handoff from Pull requests. Nothing here is a new way to
# change the harness; it is the existing ways, gathered where the flow is. That
# is deliberate: a second writer for a setting is a second place for it to be
# validated, and the two copies disagree the first time either one changes.
#
# What is NOT editable is the order of the rungs, and the honest thing is to say
# so on the screen rather than let an arrow be dragged that changes nothing.
# direction -> spec -> goals -> code is not a preference, it is what the code
# does; the arrows an operator can actually author are the triggers, on the
# lens next door.

# `operator` is not in `amp.ROLES` and should not be: it has no model, cannot be
# switched off, and nothing asks it for a completion. But it performs three of
# the seven rungs, and a picture of the agents that leaves out whoever does the
# last third of the work is a picture of an automated pipeline that does not
# exist.
_ACTORS = ("architect", "worker", "supervisor", "operator")

_ACTOR_WHAT = {
    "architect": "Proposes what is worth building, plans it, reviews it when it "
                 "lands, and awards the rungs.",
    "worker": "Writes the code and the documents. One task at a time, in an "
              "isolated worktree.",
    "supervisor": "Watches what the architect decided and what the worker did, "
                  "reads both against the mission, and advises. It recommends; it "
                  "never starts anything.",
    "operator": "You. Every rung nothing automates falls to you, and so does "
                "every blocker marked `operator`.",
}

# The same picture at four scales. A level is named rather than reached by
# zooming because every node on this screen offers a jump to the exact prompt it
# builds, and a jump needs an address: `level` plus a node id is one, a scroll
# position is not.
_LEVELS = (
    {"id": "lane", "label": "this lane",
     "what": "The seven rungs one lane's work passes through, and who holds each."},
    {"id": "workspace", "label": "this workspace",
     "what": "The loops that run over every lane at once - the report, the "
             "supervisor, the contradictions, the doctrine."},
    {"id": "workspaces", "label": "all workspaces",
     "what": "What is on the wire between workspaces, and which end has admitted it."},
    {"id": "harness", "label": "the harness itself",
     "what": "The calls that improve the machinery rather than the work. Every one "
             "of them writes something the next call is judged against."},
)

# Which registered context builds each part of the pipeline actually makes.
#
# Written out rather than derived from `lane_flow`'s `actions`, because those are
# buttons - they post to the board and open tabs - and this is a different
# question: which MODEL CALL does this rung make, so the prompt can be read. The
# two lists overlap and are not the same, and folding them together would make
# `open Publish` look like something with a prompt behind it.
#
# A rung with no calls is a real answer and is said out loud: staging and
# production are the operator's, and nothing asks a model anything there.
_STAGE_CALLS = {
    "direction": ("explore", "case", "sharpen"),
    "spec": ("spec_rate",),
    "goals": ("plan",),
    "code": ("worker",),
    "review": ("review",),
    "staging": (),
    "production": (),
}

_ACTOR_CALLS = {
    "architect": ("explore", "plan", "review", "sharpen", "case", "direction_draft"),
    "worker": ("worker",),
    "supervisor": ("supervise",),
    "operator": (),
}


def _fact(label: str, value: str, why: str = "") -> dict:
    return {"kind": "fact", "label": label, "value": value, "why": why}


def _goto(action: str, why: str, lane: str = "") -> dict:
    """A jump to the Context lens, at the exact prompt this node builds.

    This is the whole reason the lenses share one canvas. A rung that says
    `the architect reviews it` and a screen elsewhere that shows what the
    architect is sent are the same fact twice, and the second one is unreachable
    from the first unless something carries the address across.
    """
    a = _ACTION[action]
    return {"kind": "goto", "label": a["title"], "lens": "context",
            "action": action, "lane": lane, "why": why or a["what"]}


def _goto_level(level: str, node: str, label: str, why: str) -> dict:
    """A jump to another level of this same lens, landing on one node.

    The levels are the reason this exists at all: the report is not a rung and
    must not be drawn as one, but the rung it proposes into is a lane rung, and
    a picture that shows only one end of that is the picture that made the
    report look missing.
    """
    return {"kind": "goto", "label": label, "lens": "agents",
            "level": level, "node": node, "why": why}


def _calls(ids, lane: str = "", none: str = "") -> list[dict]:
    if not ids:
        return [_fact("model calls", "none", none)] if none else []
    return [_goto(i, "", lane) for i in ids]


def _check_calls():
    """Every call this lens names has to be one the Context lens can build.

    Checked at import rather than when a panel is opened: a jump to an action
    that is not registered would be a dead button that only the operator who
    pressed it ever finds out about.
    """
    for where, ids in (*(("rung " + k, v) for k, v in _STAGE_CALLS.items()),
                       *(("actor " + k, v) for k, v in _ACTOR_CALLS.items())):
        for i in ids:
            if i not in _ACTION:
                raise RuntimeError(f"the agents lens sends {where} at {i!r}, "
                                   f"which is not a registered context build")


_check_calls()


def _agent_role_panel(role: str, roles: dict, lane: str = "") -> dict:
    if role == "operator":
        return {"title": "The operator", "what": _ACTOR_WHAT["operator"],
                "rows": [_fact("switch", "none",
                               "the operator is not a model. There is nothing here "
                               "to turn off, and nothing that answers instead."),
                         *_calls((), none="nothing asks you for a completion. The "
                                          "rungs that are yours have no prompt.")]}
    r = roles.get(role) or {}
    return {
        "title": f"The {role}", "what": _ACTOR_WHAT[role],
        "rows": [
            {"kind": "toggle", "label": "on", "value": bool(r.get("on")),
             "post": "/api/role/set", "body": {"role": role}, "field": "on",
             "why": "off means nothing here runs as this role at all"},
            {"kind": "select", "label": "model", "value": r.get("choice"),
             "options": [{"value": o["value"], "note": o["note"],
                          "cannot": o.get("cannot") or "",
                          "why_not": o.get("why_not") or ""}
                         for o in (r.get("choices") or [])],
             "post": "/api/role/set", "body": {"role": role}, "field": "model",
             "why": "which model answers when this role is asked"},
            _fact("ready", "yes" if r.get("ready") else "no", r.get("why") or ""),
            *_calls(_ACTOR_CALLS[role], lane),
        ],
    }


def _agent_stage_panel(lane: str, s: dict, f: dict) -> dict:
    stage = s["stage"]
    rows: list[dict] = [
        _fact("runs", s["runs"]),
        _fact("who", s["who"]),
        _fact("state", f"{s['state']} — {s['means']}", s["at"]),
    ]
    if not s["automated"]:
        rows.append(_fact("automated", "no",
                          "nothing in the harness drives this rung. It is here "
                          "because the work still has to pass through it."))
    # The per-rung loops, on the rung they drive. Both are per lane and both
    # already have a switch elsewhere in the console; this is the same switch.
    if stage == "spec":
        rows.append({
            "kind": "toggle", "label": "the spec loop",
            "value": bool(amp.spec_auto(lane).get("on")),
            "post": "/api/spec/auto", "body": {"lane": lane}, "field": "on",
            "why": "drafts what is missing, rates what exists, sharpens what is "
                   "thin. It sends workers into worktrees, so it is per lane."})
        # The switch and what the switch gets are two different facts, and the
        # toggle shows the switch: one that read back off after being turned on,
        # because the mode outranks it, would look broken rather than outranked.
        if not amp.spec_auto_on(lane) and amp.spec_auto(lane).get("on"):
            rows.append(_fact("but it will not run", "outranked",
                              amp.lane_admits(lane, "development")
                              or amp.stage_admits(lane, "spec") or ""))
    if stage == "review":
        rows.append({
            "kind": "toggle", "label": "the handoff loop",
            "value": bool(amp.handoff_auto(lane).get("on")),
            "post": "/api/pr/auto", "body": {"lane": lane}, "field": "on",
            "why": "writes the missing checks, opens the pull request, and merges "
                   "it once GitHub's own rollup passes."})
        if not amp.handoff_auto_on(lane) and amp.handoff_auto(lane).get("on"):
            rows.append(_fact("but it will not run", "outranked",
                              amp.lane_admits(lane, "development")
                              or amp.stage_admits(lane, "review") or ""))
    if not s["is_ceiling"]:
        rows.append({
            "kind": "button", "label": "stop the lane here",
            "post": "/api/lane/stage", "body": {"lane": lane}, "field": "stage",
            "value": stage,
            "why": (f"nothing past {stage} would start unattended"
                    if s["in_reach"] else
                    f"the lane stops at {f['stage']} today, so this rung is out of "
                    f"reach until the ceiling is raised to it")})
    for b in s["blockers"]:
        rows.append(_fact(f"blocked ({b['whose']})", b["what"], b.get("why") or ""))
    rows += _calls(_STAGE_CALLS[stage], lane,
                   none=f"{stage} is the operator's, and nothing here asks a model "
                        f"anything. There is no prompt to read.")
    if stage == "direction":
        # Not every proposal on this rung was written by this lane's own loop.
        # The report reads the whole workspace's numbers back and proposes into
        # whichever lane the answer lands in, so a picture of `direction` that
        # shows only the lane's own arrows is missing an entire source of the
        # work that arrives here.
        rows.append(_goto_level(
            "workspace", "K:report", "\u2191 the report proposes here too",
            "reports are taken over the whole workspace, not one lane, so the "
            "loop is drawn a level up"))
    return {"title": stage, "what": amp.STAGE_MEANS[stage], "rows": rows}


def _agents_lane(arg: dict) -> dict:
    lanes = sorted(amp.config().get("lanes") or {})
    if not lanes:
        return {"elements": [], "legend": [], "panels": {}, "lanes": [],
                "empty": "no lanes in this workspace yet"}
    lane = (arg.get("lane") or "").strip() or lanes[0]
    if lane not in lanes:
        return {"elements": [], "legend": [], "panels": {}, "lanes": lanes,
                "empty": f"no lane {lane!r} in this workspace"}

    f = amp.lane_flow(lane)
    roles = {r["role"]: r for r in f["actors"]}
    els: list[dict] = []
    panels: dict[str, dict] = {}

    for role in _ACTORS:
        r = roles.get(role) or {}
        rid = "R:" + role
        off = role != "operator" and not r.get("on")
        # `ready` is not the same question as `on`, and the difference is the one
        # worth drawing: a role switched on whose model will not answer is the
        # failure that looks like nothing happening.
        broke = role not in ("operator",) and r.get("on") and not r.get("ready")
        els.append({"data": {
            "id": rid, "label": role,
            # The RESOLVED model, not the choice. A supervisor set to follow the
            # architect would otherwise be drawn as running a model called
            # "architect", and the one thing a node here has to say is who is
            # actually going to answer. The choice is in the panel, where it can
            # say what following means.
            "sub": ("you" if role == "operator" else (r.get("model") or "")),
            "tip": _ACTOR_WHAT[role],
        }, "classes": "actor" + (" off" if off else "") + (" broken" if broke else "")})
        panels[rid] = _agent_role_panel(role, roles, lane)

    # The lane itself, at the head of the chain: the mode is not a property of
    # any one rung, it is what the whole lane is allowed to start.
    lid = "L:" + lane
    mode = f["mode"]
    els.append({"data": {"id": lid, "label": lane, "sub": mode,
                         "tip": amp.MODE_MEANS[mode]}, "classes": "subject"})
    panels[lid] = {
        "title": lane, "what": "The lane every rung below is about.",
        "rows": [
            {"kind": "select", "label": "mode", "value": mode,
             "options": [{"value": m, "note": amp.MODE_MEANS[m]}
                         for m in amp.LANE_MODES],
             "post": "/api/lane/mode", "body": {"lane": lane}, "field": "mode",
             "why": "what may START here. Reading, reviewing and reporting are "
                    "never gated by it."},
            {"kind": "select", "label": "runs up to", "value": f["stage"],
             "options": [{"value": s,
                          "note": amp.STAGE_MEANS[s] + ("" if amp.STAGE_AUTOMATED[s]
                                                        else " (nothing automates it)")}
                         for s in amp.LANE_STAGES],
             "post": "/api/lane/stage", "body": {"lane": lane}, "field": "stage",
             "why": "the ceiling. Nothing past it starts unattended."},
            _fact("rung", f.get("rung") or "nothing judged past spec",
                  "how far a review has judged this lane's evidence"),
            _goto("direction_draft", "", lane),
            _goto("case", "", lane),
        ],
    }
    els.append({"data": {"id": f"SE:{lane}", "source": lid,
                         "target": "S:" + amp.LANE_STAGES[0],
                         "label": mode, "tip": amp.MODE_MEANS[mode]},
                "classes": "subject-edge"})
    # The supervisor holds no rung, so what it is attached to has to be drawn
    # from what it actually does, and those are two different verbs. It WATCHES
    # the other two - `_supervisor_context` is built out of the goals the
    # architect set and the work the worker filed - and it ADVISES the lane,
    # which is a weaker thing than watching it: nothing it says starts anything,
    # and the lane changes only if the operator or a later review acts on it.
    #
    # Drawing one `watches` arrow at the lane collapsed both verbs into the
    # wrong one. It read as supervision of the work, which is the claim the
    # harness is most at risk of overstating about itself.
    for other in ("architect", "worker"):
        els.append({"data": {"id": f"W:sup-{other}", "source": "R:supervisor",
                             "target": "R:" + other, "label": "watches",
                             "tip": f"what the {other} did is what the supervisor "
                                    f"reads. It is not asked; it is read after."},
                    "classes": "watches"})
    els.append({"data": {"id": "W:sup-lane", "source": "R:supervisor",
                         "target": lid, "label": "advises",
                         "tip": "a recommendation about this lane, against the "
                                "mission. It starts nothing and changes nothing "
                                "on its own."},
                "classes": "advises"})

    for s in f["flow"]:
        sid = "S:" + s["stage"]
        cls = ["stage", s["state"]]
        if not s["in_reach"]:
            cls.append("out")
        if s["is_ceiling"]:
            cls.append("ceiling")
        if not s["automated"]:
            cls.append("manual")
        els.append({"data": {
            "id": sid, "label": s["stage"], "sub": s["who"],
            "state": s["state"], "blockers": len(s["blockers"]),
            "tip": s["at"],
        }, "classes": " ".join(cls)})
        panels[sid] = _agent_stage_panel(lane, s, f)
        # Who performs it. Dashed, and drawn from the actor rather than folded
        # into the node label, because the question this lens exists to answer
        # is which of them is holding which rung.
        who = "R:" + s["who"]
        els.append({"data": {"id": f"P:{s['stage']}", "source": who, "target": sid,
                             "label": "", "tip": f"the {s['who']} performs {s['stage']}"},
                    "classes": "performs"})

    for i, e in enumerate(f["edges"]):
        kind = e.get("kind") or "forward"
        els.append({"data": {
            "id": f"F:{i}", "source": "S:" + e["from"], "target": "S:" + e["to"],
            "label": e["who"], "tip": e["what"],
        }, "classes": "hand " + kind})

    return {
        "elements": els, "panels": panels, "lanes": lanes, "lane": lane,
        "legend": [
            {"k": "actor", "t": "an agent. Click it to switch it or change its model."},
            {"k": "off", "t": "switched off, or a model that will not answer"},
            {"k": "ceiling", "t": "as far as this lane runs unattended"},
            {"k": "out", "t": "above the ceiling - nothing here starts"},
            {"k": "feedback", "t": "what makes it a loop rather than a conveyor"},
        ],
        "note": "The order of the rungs is what the code does, not a preference, "
                "so these arrows are not draggable. The arrows you can author are "
                "on the Triggers lens.",
        "empty": "",
    }


# ------------------------------------------------------- the whole workspace
#
# What the lane level structurally cannot show. Every loop here runs over EVERY
# lane at once, so drawing any of them inside one lane's pipeline would be a
# claim the code does not make - there is no per-lane report, no per-lane
# supervisor pass, and no per-lane doctrine.
#
# The report is the one that was noticed missing, and it is missing from the
# lane level correctly: `report_now()` takes the whole workspace's numbers, and
# `solve_report()` reads them back and proposes into whichever lane the answer
# lands in. Only the last arrow of that loop touches a lane, which is exactly
# why it looked absent from a picture of one.

def _ws_node(nid: str, label: str, sub: str, tip: str, cls: str,
             els: list, panels: dict, panel: dict):
    els.append({"data": {"id": nid, "label": label, "sub": sub, "tip": tip},
                "classes": cls})
    panels[nid] = panel


def _agents_workspace(_arg: dict) -> dict:
    slug = amp.current_workspace()
    reg = amp.workspaces()
    ws = reg["workspaces"].get(slug) or {}
    lanes = sorted(amp.config().get("lanes") or {})
    props = amp.open_proposals()
    from_report = [p for p in props if p.get("source") == "solve"]
    rep = amp.last_report()
    contra = [f for f in amp.findings(unread_only=True)
              if f.get("bearing") == "contradicted"]
    mission = amp.mission(slug)

    els: list[dict] = []
    panels: dict[str, dict] = {}

    wid = "WS:" + slug
    _ws_node(wid, ws.get("name") or slug, f"{len(lanes)} lane(s)",
             mission[:160] or "no mission written for this workspace",
             "subject", els, panels, {
                 "title": ws.get("name") or slug,
                 "what": "The workspace every loop on this level runs over.",
                 "rows": [
                     _fact("mission", "written" if mission else "none",
                           mission[:400] or "every call that quotes the mission "
                                            "quotes an empty string until one is written"),
                     _fact("lanes", str(len(lanes)), ", ".join(lanes[:12])),
                     _goto_level("workspaces", "P:" + slug, "\u2192 what is on the wire",
                                 "this workspace's ports to the others"),
                 ]})

    # --- the report loop -----------------------------------------------------
    _ws_node("K:report", "report", (rep or {}).get("at", "")[:10] or "never taken",
             "what was true across every lane at the moment it was taken",
             "loop measure", els, panels, {
                 "title": "The report",
                 "what": "A snapshot of every lane at once - what is running, what is "
                         "blocked, what it cost, what moved since the last one.",
                 "rows": [
                     _fact("last taken", (rep or {}).get("at") or "never",
                           "a report is a measurement, so an old one is not wrong, "
                           "it is about a moment further back"),
                     _fact("this is not a lane rung", "correct",
                           "there is no per-lane report. It is taken over the "
                           "workspace, which is why it does not appear on the "
                           "pipeline of any one lane."),
                 ]})
    _ws_node("K:solve", "solve", "architect",
             "reads the report's own numbers back and proposes against them",
             "loop call", els, panels, {
                 "title": "Solving the report",
                 "what": "The architect is handed the report and asked what to do "
                         "about what it says. It proposes; the ordinary gate still "
                         "refuses anything nobody has scored.",
                 "rows": [
                     _fact("open proposals from here", str(len(from_report)),
                           "counted by `source: solve` on the proposal itself, not "
                           "guessed from timing"),
                     *([_fact("but it cannot run", "no report",
                              "this call is built out of a report, so a workspace "
                              "that has never taken one has nothing to solve")]
                       if not rep else []),
                     _goto("solve", ""),
                 ]})
    _ws_node("K:lanes", "every lane's direction", f"{len(props)} open proposal(s)",
             "where a proposal actually lands", "loop lanes", els, panels, {
                 "title": "Every lane's direction rung",
                 "what": "Where a proposal from any of these loops arrives, and where "
                         "it meets the same bar as one the lane proposed for itself.",
                 "rows": [
                     _fact("open proposals", str(len(props))),
                     _fact("of those, from the report", str(len(from_report))),
                     _goto_level("lane", "S:direction", "\u2193 open one lane's pipeline",
                                 "the rung these arrive on, drawn a level down"),
                 ]})
    _ws_node("K:work", "what shipped", "the lanes, running",
             "the work itself - this is the lane level, seen from above",
             "loop work", els, panels, {
                 "title": "The work",
                 "what": "Every lane's pipeline, which is what the next report will "
                         "be a measurement of.",
                 "rows": [
                     _goto_level("lane", "", "\u2193 open the pipeline",
                                 "one lane's seven rungs"),
                 ]})

    for a, b, lbl, kind, tip in (
            ("K:report", "K:solve", "the numbers", "forward",
             "the report is the input to the call, not a summary of it"),
            ("K:solve", "K:lanes", "proposals", "forward",
             "unscored, into the ordinary queue, held until something scores them"),
            ("K:lanes", "K:work", "what starts", "forward",
             "a proposal that clears its bar becomes a goal, and a goal becomes tasks"),
            ("K:work", "K:report", "what shipped", "feedback",
             "and the next report measures it. This arrow is the loop; without it "
             "the report is a dashboard rather than part of the machine")):
        els.append({"data": {"id": f"E:{a}-{b}", "source": a, "target": b,
                             "label": lbl, "tip": tip}, "classes": "hand " + kind})

    # --- the three that advise rather than propose ---------------------------
    for nid, label, action, sub, tip, panel_rows in (
            ("K:supervise", "supervise", "supervise", "supervisor",
             "reads everything against the mission and recommends",
             [_fact("starts nothing", "by design",
                    "the supervisor's verdict is a recommendation. Nothing in the "
                    "harness acts on it without you.")]),
            ("K:settle", "settle", "settle", f"{len(contra)} open",
             "decides what to do about claims the work has found false",
             [_fact("open contradictions", str(len(contra)),
                    "a claim a lane recorded that later work contradicted. Until one "
                    "is settled it keeps being shown to every proposal scored in "
                    "every lane."),
              *([_fact("but it cannot run", "nothing open",
                       "this call only runs on an open contradiction")]
                if not contra else [])]),
            ("K:doctrine", "doctrine", "doctrine", "the rules",
             "reviews the rules every agent here is held to",
             [_fact("what it changes", "the rules, not the work",
                    "the doctrine is quoted into other prompts, so a change here "
                    "reaches every call rather than one")])):
        _ws_node(nid, label, sub, tip, "loop call advisory", els, panels, {
            "title": _ACTION[action]["title"], "what": _ACTION[action]["what"],
            "rows": [*panel_rows, _goto(action, "")]})
        els.append({"data": {"id": f"A:{nid}", "source": nid,
                             "target": wid if nid != "K:settle" else "K:lanes",
                             "label": "advises", "tip": tip}, "classes": "advises"})

    return {
        "elements": els, "panels": panels, "lanes": lanes,
        "legend": [
            {"k": "measure", "t": "a measurement of the whole workspace"},
            {"k": "call", "t": "a model call. Click it to read the prompt it builds."},
            {"k": "feedback", "t": "what makes it a loop rather than a dashboard"},
            {"k": "advises", "t": "recommends only - nothing here starts anything"},
        ],
        "note": "Nothing on this level is a lane rung, and that is why none of it "
                "appears on the lane pipeline. Only the last arrow of the report "
                "loop touches a lane, and it arrives at `direction` like any other "
                "proposal - held until something scores it.",
        "empty": "",
    }


# ------------------------------------------------------- between workspaces
#
# Drawn entirely from `ports_view()`, which already computes this from BOTH ends
# - whether a request landed is the receiving workspace's record, and reading it
# from the sender would only ever report what the sender hoped.

def _agents_workspaces(_arg: dict) -> dict:
    v = ports_view()
    els: list[dict] = []
    panels: dict[str, dict] = {}

    for w in v["workspaces"]:
        nid = "P:" + w["slug"]
        lanes = sorted(_ws_lanes(w["slug"]))
        out = sum(e["waiting"] + e["landed"] for e in v["edges"] if e["from"] == w["slug"])
        inb = sum(e["waiting"] + e["landed"] for e in v["edges"] if e["to"] == w["slug"])
        els.append({"data": {
            "id": nid, "label": w["name"],
            "sub": f"{len(lanes)} lane(s)",
            "tip": ("the workspace you are in" if w["current"] else
                    "another workspace. Its lanes, goals and worktrees are its own."),
        }, "classes": "ws" + (" current" if w["current"] else "")})
        panels[nid] = {
            "title": w["name"],
            "what": ("The workspace this console is currently in."
                     if w["current"] else
                     "Another workspace. A trigger here can point a port at one of "
                     "its lanes; nothing else reaches across."),
            "rows": [
                _fact("slug", w["slug"]),
                _fact("lanes", str(len(lanes)), ", ".join(lanes[:12]) or "none"),
                _fact("sent from here", str(out)),
                _fact("addressed to here", str(inb)),
                *([] if w["current"] else [{
                    "kind": "button", "label": "work in this workspace",
                    "post": "/api/workspace/use", "body": {}, "field": "slug",
                    "value": w["slug"],
                    "why": "the same switch as the picker in the header. It changes "
                           "every tab, not just this one."}]),
            ],
        }

    for e in v["edges"]:
        eid = f"PE:{e['from']}-{e['to']}"
        n = e["waiting"] + e["landed"]
        els.append({"data": {
            "id": eid, "source": "P:" + e["from"], "target": "P:" + e["to"],
            "label": (f"{e['waiting']} waiting" if e["waiting"]
                      else f"{e['landed']} landed"),
            "tip": f"{n} request(s) on this wire",
        }, "classes": "port" + (" waiting" if e["waiting"] else "")})
        panels[eid] = {
            "title": f"{e['from']} \u2192 {e['to']}",
            "what": "Proposals a trigger addressed at a lane in another workspace.",
            "rows": [
                _fact("waiting", str(e["waiting"]),
                      "sent, and the far end has not admitted them. A port does not "
                      "write into another workspace - it asks, and the other end "
                      "decides."),
                _fact("landed", str(e["landed"]),
                      "read from the RECEIVING workspace's own record, because the "
                      "sender only ever knows what it sent"),
                _goto_level("workspace", "", "\u2193 the loops inside a workspace",
                            "one workspace, from the inside"),
            ],
        }

    return {
        "elements": els, "panels": panels,
        "legend": [
            {"k": "current", "t": "the workspace you are in"},
            {"k": "waiting", "t": "sent, and the far end has not admitted it yet"},
        ],
        "note": ("Nothing crosses a workspace boundary except a request on a port, "
                 "and a request is admitted by the receiving end or it is not "
                 "admitted at all."
                 + ("" if v["edges"] else
                    " No trigger in any of these workspaces has a port aimed at "
                    "another one yet, so there is nothing on the wire.")),
        "empty": ("this registry has one workspace, so there is nothing between them"
                  if len(v["workspaces"]) < 2 and not v["edges"] else ""),
    }


# ------------------------------------------------- the harness on the harness
#
# The calls that improve the machinery rather than the work. Each one writes
# something that a LATER call is judged against, which is the only reason they
# belong on one picture: sharpening re-scores against a bar, a direction is what
# the next proposer is shown, the layers are what the triggers are drawn on, and
# the doctrine is quoted into all of it.
#
# Four of the six write NOTHING until an operator accepts a draft. That is drawn,
# because a loop that closes by itself and a loop that closes through a person
# are different machines, and it is the difference that decides how much of this
# can run unattended.
_HARNESS = (
    {"id": "sharpen", "into": "H:queue", "writes": "a better version of a proposal",
     "auto": True,
     "why": "re-scores a held proposal against the bar it missed and tries to write "
            "one that clears it. It writes the new version straight into the queue."},
    {"id": "direction_draft", "into": "H:direction", "writes": "nothing until accepted",
     "auto": False,
     "why": "drafts what a lane is FOR. The direction is read by every proposal, "
            "every review and every sharpen pass in that lane, which is exactly why "
            "a model is not allowed to install its own."},
    {"id": "stack_draft", "into": "H:layers", "writes": "nothing until accepted",
     "auto": False,
     "why": "names the layers on the Blueprint. Layers are what triggers are drawn "
            "between, so a taxonomy installed here would shape what gets proposed, "
            "one step removed and unattributed."},
    {"id": "trigger_draft", "into": "H:triggers", "writes": "nothing until accepted",
     "auto": False,
     "why": "proposes the wiring between the layers. What it drafts arrives "
            "disarmed even after it is accepted."},
    {"id": "settle", "into": "H:ladder", "writes": "retractions and proposals",
     "auto": False,
     "why": "decides what to do about claims the work found false. A verdict whose "
            "consequence the harness cannot perform is downgraded rather than "
            "recorded as done."},
    {"id": "doctrine", "into": "H:rules", "writes": "nothing until accepted",
     "auto": False,
     "why": "reviews the rules every agent here is held to. They are quoted into "
            "other prompts, so a change reaches every call rather than one."},
)

_HARNESS_WRITTEN = (
    ("H:queue", "the proposal queue", "what is waiting to be scored"),
    ("H:direction", "what each lane is for", "read by every call about that lane"),
    ("H:layers", "the layers", "what triggers are drawn between"),
    ("H:triggers", "the triggers", "propose, never start"),
    ("H:ladder", "the rungs", "what a lane is judged to have earned"),
    ("H:rules", "the doctrine", "quoted into every prompt"),
)


def _agents_harness(_arg: dict) -> dict:
    els: list[dict] = []
    panels: dict[str, dict] = {}

    for nid, label, sub in _HARNESS_WRITTEN:
        els.append({"data": {"id": nid, "label": label, "sub": sub,
                             "tip": "what the call to its left writes"},
                    "classes": "written"})
        panels[nid] = {"title": label, "what": sub, "rows": [
            _fact("who reads it", "the next call", sub),
            _goto_level("lane", "", "\u2193 where it is read",
                        "the pipeline these all end up governing"),
        ]}

    for h in _HARNESS:
        a = _ACTION[h["id"]]
        nid = "H:" + h["id"]
        els.append({"data": {"id": nid, "label": h["id"].replace("_", " "),
                             "sub": "writes" if h["auto"] else "drafts",
                             "tip": h["why"]},
                    "classes": "loop call" + ("" if h["auto"] else " draft")})
        panels[nid] = {"title": a["title"], "what": h["why"], "rows": [
            _fact("writes", h["writes"],
                  "straight through" if h["auto"] else
                  "it drafts, and an operator accepts. Until then nothing changed."),
            _fact("scope", a["scope"]),
            _goto(h["id"], ""),
        ]}
        els.append({"data": {"id": f"HE:{h['id']}", "source": nid, "target": h["into"],
                             "label": "writes" if h["auto"] else "drafts",
                             "tip": h["writes"]},
                    "classes": "hand " + ("forward" if h["auto"] else "advises")})
        # And back: everything written here is read by the calls that do the
        # ordinary work, and what those calls produce is what the next pass of
        # this same loop is drafted against. That arrow is why this is a level
        # and not a settings page.
        els.append({"data": {"id": f"HB:{h['id']}", "source": h["into"], "target": nid,
                             "label": "", "kind": "feedback",
                             "tip": "and what the work does next is what the next "
                                    "pass of this call is drafted against"},
                    "classes": "hand feedback"})

    return {
        "elements": els, "panels": panels,
        "legend": [
            {"k": "call", "t": "a model call. Click it to read the prompt it builds."},
            {"k": "draft", "t": "writes nothing until you accept it"},
            {"k": "written", "t": "what it changes, and who reads it after"},
            {"k": "feedback", "t": "what the work does next is what it is drafted against"},
        ],
        "note": "Five of these six draft rather than write. The one that writes "
                "straight through is sharpen, and what it writes is a proposal - "
                "which still has to clear the same bar as any other.",
        "empty": "",
    }


def _panel_calls(panel: dict | None) -> list[dict]:
    """The Context jumps a node's own panel already offers.

    Read back off the finished panel rather than from `_STAGE_CALLS` and friends,
    so this holds no second copy of which node makes which call. A node that
    stops offering a prompt stops offering it on its arrows in the same moment.
    """
    return [r for r in (panel or {}).get("rows") or []
            if r.get("kind") == "goto" and r.get("lens") == "context"]


def _edge_panels(els: list[dict], panels: dict) -> None:
    """Give every arrow a panel too, carrying the calls that are behind it.

    An arrow was the one thing on this canvas a click fell through on: nodes had
    panels, edges got the plain tooltip, so the supervisor's own arrows - the
    ones the whole three-edge fix is about - offered no way to read the prompt
    that produces them. The relationship IS the interesting object there.

    An arrow makes no call of its own, so it can only carry the calls at its
    ends, and WHICH end is stated rather than quietly merged:

    - both ends: an actor's arrow into the rung it holds. `architect performs
      review` narrows six calls to the one the arrow is actually about.
    - the tail only: `supervisor watches architect`. `supervise` is built out of
      exactly what that arrow points at, and the architect's own six calls are
      not what this arrow is.
    - the head only: `the report -> solve`. The report is a measurement and asks
      nothing; the call is what receives it.
    - neither: a hand-off between rungs. Said out loud, because an arrow with no
      prompt behind it is a real answer and the honest one for most of them.
    """
    lab = {e["data"]["id"]: (e["data"].get("label") or e["data"]["id"])
           for e in els if "source" not in e["data"]}
    for e in els:
        d = e["data"]
        # Edges only, and never over a panel the level wrote for itself - the
        # port edges already say something this cannot know.
        if "source" not in d or d["id"] in panels:
            continue
        a, b = _panel_calls(panels.get(d["source"])), _panel_calls(panels.get(d["target"]))
        both = [r for r in a if any(r["action"] == q["action"] for q in b)]
        if both:
            calls = [{**r, "why": "both ends of this arrow make this call, which "
                                  "is what the arrow is"} for r in both]
        elif a:
            calls = [{**r, "why": f"made by {lab.get(d['source'], '')}, at the tail "
                                  f"of this arrow"} for r in a]
        elif b:
            calls = [{**r, "why": f"made by {lab.get(d['target'], '')}, at the head "
                                  f"of this arrow"} for r in b]
        else:
            calls = [_fact("model calls", "none",
                           "neither end of this arrow asks a model anything. It is "
                           "the order the code runs in, not a prompt.")]
        panels[d["id"]] = {
            "title": f"{lab.get(d['source'], d['source'])} \u2192 "
                     f"{lab.get(d['target'], d['target'])}",
            "what": d.get("tip") or "How these two are connected.",
            "rows": ([_fact("on the arrow", d["label"])] if d.get("label") else []) + calls,
        }


_AGENT_LEVELS = {"lane": _agents_lane, "workspace": _agents_workspace,
                 "workspaces": _agents_workspaces, "harness": _agents_harness}


def _flow_agents(arg: dict) -> dict:
    level = (arg.get("level") or "").strip() or "lane"
    fn = _AGENT_LEVELS.get(level)
    if not fn:
        return {"elements": [], "panels": {}, "levels": list(_LEVELS), "level": "lane",
                "empty": f"no level {level!r} on this lens"}
    out = fn(arg)
    # Applied here rather than in the four builders, because it is one rule and
    # four copies of it would be four chances for a level to quietly not have it.
    _edge_panels(out.get("elements") or [], out.setdefault("panels", {}))
    # The lane picker is offered only where a lane is what the picture is about.
    # Shown and ignored, it reads as a control that stopped working.
    return {**out, "levels": list(_LEVELS), "level": level,
            "level_what": next(l["what"] for l in _LEVELS if l["id"] == level),
            "needs_lane": level == "lane"}


_LENSES = {"agents": _flow_agents, "map": _flow_map,
           "triggers": _flow_triggers, "context": _flow_context}


def flow_view(lens: str, arg: dict | None = None) -> dict:
    fn = _LENSES.get(lens)
    if not fn:
        return {"ok": False, "error": f"no lens {lens!r}"}
    return {"ok": True, "lens": lens, **fn(arg or {})}
