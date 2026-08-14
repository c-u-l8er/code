"""Numbers nobody wrote down.

`worth` and `lane_rungs` are pure functions of state that get read as if they
were facts - `worth` orders every proposal the harness considers, and
`lane_rungs` is described in its own docstring as "the single most useful fact
for judging how much the mission wants a proposal". Neither is stored, so there
is no record to check them against; the only way they can be wrong is quietly.

Both are cheap to pin exactly, which is the argument for doing it: given a fixed
store, assert the number, not a property of the number.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE))


@pytest.fixture()
def amp(tmp_path, monkeypatch):
    monkeypatch.setenv("AMP_HOME", str(tmp_path / "state"))
    for m in ("amp", "store"):
        sys.modules.pop(m, None)
    import amp as amp_mod
    assert amp_mod.STATE_ROOT == tmp_path / "state"
    return amp_mod


# --------------------------------------------------- the state-name declarations


def test_every_bound_state_name_is_declared(amp):
    """`_STATE_LAYOUT` and the annotation block above `_bind_state` must agree.

    Two lists of the same twenty-eight names is a duplication, and it is here on
    purpose: nothing else tells a reader - or a type checker - that these names
    exist, because `_bind_state` writes them straight into `globals()`. The
    hazard the duplication creates is exactly one thing, that somebody adds a
    line to the table and not to the declarations, so that is the thing checked.

    A missing declaration is not a crash. The harness runs perfectly well without
    it; the name simply goes back to being invisible, which is the state this was
    written to leave.
    """
    bound = {"STATE"} | {name for name, _rel in amp._STATE_LAYOUT}
    # Only the names annotated `Path`: the module has other module-level
    # annotations (`_DOC_RLOCKS: dict[...]` and friends) that are ordinary
    # annotated assignments and have nothing to do with workspace state.
    declared = {n for n, t in amp.__annotations__.items()
                if t is Path or t == "Path"}
    assert not (bound - declared), (
        "bound by `_bind_state` and declared nowhere, so ruff will call every use "
        f"an undefined name again: {sorted(bound - declared)}")
    assert not (declared - bound), (
        "declared as workspace state and never bound, so the annotation promises "
        f"a path that will not be there: {sorted(declared - bound)}")


def test_every_state_path_lands_inside_the_state_root(amp):
    """Every name in the table must resolve to a path under the bound root.

    The first draft of this claimed to catch a declaration that had grown an
    `= Path(...)`. It does not, and the reason is worth keeping: `_bind_state`
    runs at import and rebinds every name in the table, so an extra module-level
    assignment to a name that IS in the table is overwritten and harmless. The
    name declared and NOT in the table is the real hazard, and the test above is
    what catches it.

    What this one catches is a table entry whose second column is absolute.
    `base / "/etc/thing"` is `/etc/thing` - pathlib discards the left operand -
    so one leading slash in a column of relative names silently moves a piece of
    workspace state outside the workspace, and every workspace then shares it.
    """
    for name in {"STATE"} | {n for n, _rel in amp._STATE_LAYOUT}:
        got = getattr(amp, name)
        assert isinstance(got, Path), f"{name} is not a path: {got!r}"
        assert got == amp.STATE or amp.STATE in got.parents, (
            f"{name} is {got}, which is not under the bound state root "
            f"{amp.STATE} - it was assigned somewhere other than `_bind_state`")


# ------------------------------------------------------------------- worth


@pytest.mark.parametrize("p, want", [
    # c * need / cost, and the arithmetic stated rather than re-derived.
    ({"confidence": 0.8, "need": 0.5, "cost_usd": 2.0}, 0.2),
    ({"confidence": 1.0, "need": 1.0, "cost_usd": 1.0}, 1.0),
    ({"confidence": 0.0, "need": 1.0, "cost_usd": 1.0}, 0.0),
    # Rounded to three places, so two proposals a ten-thousandth apart sort
    # equal rather than in an order that came out of floating point.
    ({"confidence": 0.333, "need": 0.333, "cost_usd": 1.0}, 0.111),
])
def test_worth_is_movement_per_dollar(amp, p, want):
    assert amp.worth(p) == want


@pytest.mark.parametrize("cost", [None, 0.0, 0.01, 0.25])
def test_a_cheap_or_uncosted_proposal_hits_the_floor(amp, cost):
    """Without the floor, a proposal estimated at nothing would divide by
    nothing and sort above every real proposal for ever - and "nobody costed
    it" would read as "infinitely worth doing"."""
    assert amp.worth({"confidence": 1.0, "need": 1.0, "cost_usd": cost}) == 4.0
    assert amp.COST_FLOOR_USD == 0.25


@pytest.mark.parametrize("p", [
    {"confidence": None, "need": 0.9},
    {"confidence": 0.9, "need": None},
    {"confidence": None, "need": None},
    {},
])
def test_worth_refuses_a_partial_answer(amp, p):
    """None, never a number built from the fields that happen to be present.

    A partial answer here is worse than no answer: the proposal would rank
    either top or bottom depending on which field was missing, and nothing
    about that ordering would be visible as a guess.
    """
    assert amp.worth(p) is None


def test_worth_is_never_a_gate(amp):
    """It ranks. `proposal_hold` is the thing that stops work, and it does not
    consult `worth` - checked by reading, because the claim is an absence."""
    import inspect
    src = inspect.getsource(amp.proposal_hold) + inspect.getsource(
        amp.proposal_policy_hold)
    assert "worth(" not in src


# -------------------------------------------------------------- lane_rungs


def _reviews(amp, *reviews) -> None:
    d = amp.direction_store()
    d["reviews"] = list(reviews)
    amp._save_direction(d)


def _rev(lane, *entries, rid="r1"):
    return {"id": rid, "lane": lane, "at": "2026-01-01T00:00:00+00:00",
            "ladder": [{"claim": c, "from": f, "to": t} for c, f, t in entries]}


def test_no_reviews_is_no_rungs(amp):
    """Empty, not a default rung. A lane nobody has judged has not been judged
    to reach `spec`."""
    assert amp.lane_rungs() == {}


def test_the_highest_rung_wins_not_the_latest(amp):
    """A later review that only moved something to `spec` does not demote a
    lane that has already been judged to run somewhere real."""
    _reviews(amp,
             _rev("code", ("it runs locally", "in_tree", "live_local"), rid="r1"),
             _rev("code", ("the doc exists", None, "spec"), rid="r2"))
    assert amp.lane_rungs() == {"code": "live_local"}


def test_lanes_are_judged_separately(amp):
    _reviews(amp,
             _rev("code", ("a", None, "live_deployed"), rid="r1"),
             _rev("docs", ("b", None, "spec"), rid="r2"))
    assert amp.lane_rungs() == {"code": "live_deployed", "docs": "spec"}


@pytest.mark.parametrize("rung", ["", None, "shipped", "SPEC", "production"])
def test_a_rung_that_is_not_on_the_ladder_is_not_a_rung(amp, rung):
    """`production` is a lane STAGE, not a ladder rung, and the two vocabularies
    are close enough to be typed for each other."""
    _reviews(amp, _rev("code", ("a", None, rung)))
    assert amp.lane_rungs() == {}


def test_a_retracted_entry_stops_counting(amp):
    """Without this the reading is monotonic by construction: the highest rung
    ever claimed could never fall, even after a later review judged that it was
    never earned, and a lane that overstated itself once would go on being
    scored as if it had not."""
    _reviews(amp, _rev("code",
                       ("the doc exists", None, "spec"),
                       ("it is deployed", "live_local", "live_deployed")))
    assert amp.lane_rungs() == {"code": "live_deployed"}
    r = amp.retract_rung("code", "it is deployed", "live_deployed",
                         "no deployment was ever found")
    assert r is not None and r["from"] == "live_local"
    assert amp.lane_rungs() == {"code": "spec"}


def test_the_review_is_not_edited_by_a_retraction(amp):
    """The review is the only account of how the mistake was made. Losing it
    to tidy up the derived number would cost the thing worth keeping."""
    _reviews(amp, _rev("code", ("it is deployed", "live_local", "live_deployed")))
    before = amp.direction_store()["reviews"]
    amp.retract_rung("code", "it is deployed", "live_deployed", "it was not")
    assert amp.direction_store()["reviews"] == before


def test_a_retraction_cannot_invent_the_claim_it_walks_back(amp):
    """The check that keeps a retraction honest: no entry, no retraction, and
    nothing written to the store."""
    _reviews(amp, _rev("code", ("it is deployed", "live_local", "live_deployed")))
    assert amp.retract_rung("code", "something nobody said", "live_deployed",
                            "because") is None
    assert amp.retract_rung("docs", "it is deployed", "live_deployed",
                            "wrong lane") is None
    assert amp.retract_rung("code", "it is deployed", "external",
                            "wrong rung") is None
    assert amp.direction_store().get("retractions") in (None, [])


def test_retracting_twice_records_once(amp):
    _reviews(amp, _rev("code", ("it is deployed", "live_local", "live_deployed")))
    a = amp.retract_rung("code", "it is deployed", "live_deployed", "first")
    b = amp.retract_rung("code", "it is deployed", "live_deployed", "second")
    assert a["id"] == b["id"]
    assert len(amp.direction_store()["retractions"]) == 1


def test_a_second_undisputed_entry_holds_the_rung_up(amp):
    """The case that made a live gate look broken: retracting one entry does
    not lower the lane if another review claimed the same rung and nobody has
    disputed that one. The rung is a fact about the EVIDENCE, not about any
    single sentence."""
    _reviews(amp,
             _rev("code", ("it is deployed", "live_local", "live_deployed"), rid="r1"),
             _rev("code", ("the site answers", "live_local", "live_deployed"), rid="r2"))
    amp.retract_rung("code", "it is deployed", "live_deployed", "no it is not")
    assert amp.lane_rungs() == {"code": "live_deployed"}
