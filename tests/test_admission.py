"""The gate that decides whether work starts.

`proposal_hold` is the whole of the enforcement for goals: every path that
adopts a proposal with nobody watching comes through it. It has ten ways to say
no, and the thing worth pinning is not that it says no - it is the SENTENCE,
because the sentence is the entire user interface of a refusal. An operator
looking at a quiet lane gets one line to explain it, and a wrong line sends them
to fix something that was never the reason.

So the refusals are a table, one row per sentence, asserted whole and literal.
Written out rather than re-derived from `SHARPEN_FLOOR` and friends on purpose:
a test that rebuilds the string from the same constants the code uses agrees
with itself no matter what either of them says.

The other half of this file is ORDER. Ten refusals means a proposal can be held
for several reasons at once, and which one it reports is a decision the code
makes deliberately - policy before bars, bars before sharpening - documented in
`held_for_sharpening` and untested until now.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE))


@pytest.fixture()
def amp(tmp_path, monkeypatch):
    """`amp` on a throwaway state directory, with two lanes and stated bars.

    The bars are written down rather than left to default, because half the
    sentences below quote them. A test whose expected string depends on a
    default is a test that changes meaning when somebody tunes the default.
    """
    monkeypatch.setenv("AMP_HOME", str(tmp_path / "state"))
    for m in ("amp", "store"):
        sys.modules.pop(m, None)
    import amp as amp_mod
    assert amp_mod.STATE_ROOT == tmp_path / "state"
    amp_mod.save_json(amp_mod.CONFIG_PATH, {
        "lanes": {"code": {}, "docs": {}},
        "autonomy": {"adopt_confidence": 0.6, "adopt_need": 0.5},
    })
    assert (amp_mod.adopt_bar(), amp_mod.need_bar()) == (0.6, 0.5)
    return amp_mod


def _mode(amp, lane: str, mode: str) -> None:
    cfg = amp.config()
    cfg["lanes"][lane]["mode"] = mode
    amp.save_json(amp.CONFIG_PATH, cfg)


def _stage(amp, lane: str, stage: str) -> None:
    cfg = amp.config()
    cfg["lanes"][lane]["stage"] = stage
    amp.save_json(amp.CONFIG_PATH, cfg)


def _p(**kw) -> dict:
    """A proposal that would be adopted, so every row below differs in ONE way.

    A fixture that is already held for some unrelated reason turns every row
    into a test of that reason instead.
    """
    return {"id": "p1", "kind": "goal", "state": "open", "lane": "code",
            "source": "explore", "text": "do the thing", "why": "because",
            "confidence": 0.9, "need": 0.9, "cost_usd": 3.0, **kw}


# --------------------------------------------------------------- it can pass


def test_a_scored_proposal_over_both_bars_is_not_held(amp):
    """The row that makes the other twelve mean something.

    Without it, a `proposal_hold` that returned a string unconditionally would
    pass every refusal test in this file.
    """
    assert amp.proposal_hold(_p()) is None
    assert amp.proposal_policy_hold(_p()) is None


# -------------------------------------------------- standing operator policy


@pytest.mark.parametrize("mode, source, want", [
    ("maintain", "explore",
     "code is set to maintain, so it takes fixes but not new development"
     " - this is development"),
    ("archived", "explore", "code is archived"),
    ("frozen", "explore", "code is frozen, so nothing starts in it unattended"),
    ("archived", "solve", "code is archived"),
    ("frozen", "solve", "code is frozen, so nothing starts in it unattended"),
])
def test_the_mode_refuses_in_its_own_words(amp, mode, source, want):
    _mode(amp, "code", mode)
    assert amp.proposal_policy_hold(_p(source=source)) == want


def test_maintain_takes_a_repair(amp):
    """The mode is not a stop button, and `maintain` is the row that proves it."""
    _mode(amp, "code", "maintain")
    assert amp.proposal_policy_hold(_p(source="solve")) is None
    assert amp.proposal_hold(_p(source="solve")) is None


@pytest.mark.parametrize("source", ["explore", "spec", "review"])
def test_development_sources_are_development(amp, source):
    _mode(amp, "code", "maintain")
    assert amp.work_kind(source) == "development"
    assert amp.proposal_policy_hold(_p(source=source)) is not None


@pytest.mark.parametrize("source", ["settle", "settle-residue", "solve"])
def test_corrective_sources_are_corrective(amp, source):
    _mode(amp, "code", "maintain")
    assert amp.work_kind(source) == "corrective"
    assert amp.proposal_policy_hold(_p(source=source)) is None


@pytest.mark.parametrize("source", ["", None, "  ", "whatever-comes-next"])
def test_an_unclassified_source_is_held_by_maintain(amp, source):
    """The strict reading, and the one that is easy to get backwards.

    An origin nobody has classified is not "harmless until proven otherwise".
    If the default were `corrective`, adding any new source of work anywhere in
    the program would silently punch a hole through every lane an operator had
    set to maintain - and the hole would open by omission, in a file nobody
    editing the new source would think to look at.
    """
    _mode(amp, "code", "maintain")
    assert amp.work_kind(source) == "development"
    assert amp.proposal_policy_hold(_p(source=source)) is not None


@pytest.mark.parametrize("stage", ["direction", "spec"])
def test_a_stage_below_goals_refuses_in_its_own_words(amp, stage):
    _stage(amp, "code", stage)
    assert amp.proposal_policy_hold(_p()) == (
        f"code is set to stop at {stage}, and this is goals — "
        f"nothing past {stage} starts here unattended")


@pytest.mark.parametrize("stage", ["goals", "code", "review", "staging", "production"])
def test_a_stage_at_or_above_goals_does_not(amp, stage):
    """`goals` itself passes. A ceiling is a ceiling, not a floor."""
    _stage(amp, "code", stage)
    assert amp.proposal_policy_hold(_p()) is None


# --------------------------------------------------------- the bars, and why


@pytest.mark.parametrize("p, want", [
    ({"confidence": None, "need": None}, "nobody has scored it yet"),
    ({"confidence": None}, "nobody has judged whether it would finish"),
    ({"need": None}, "nobody has judged how much the mission wants it"),
    ({"confidence": 0.45}, "45% odds of finishing, under the 60% bar you set"),
    ({"need": 0.30}, "the mission wants it 30%, under the 50% bar you set"),
    ({"headroom": 0.40},
     "40% room left to improve it, over the 15% worth another look"
     " — sharpening it first"),
])
def test_each_bar_refuses_in_its_own_words(amp, p, want):
    assert amp.proposal_hold(_p(**p)) == want


def test_unscored_is_three_different_facts_and_says_which(amp):
    """Two missing numbers, three sentences, on purpose.

    A proposal scored under the old single-number scheme has one of these and
    not the other, so collapsing them to "unscored" would tell an operator to
    re-run a call that has already been paid for.
    """
    said = {amp.proposal_hold(_p(confidence=None, need=None)),
            amp.proposal_hold(_p(confidence=None)),
            amp.proposal_hold(_p(need=None))}
    assert len(said) == 3


def test_zero_is_a_score_and_none_is_not(amp):
    """`0.0` and `None` mean opposite things and must not share a sentence."""
    assert amp.proposal_hold(_p(confidence=0.0)) == (
        "0% odds of finishing, under the 60% bar you set")
    assert amp.proposal_hold(_p(confidence=None)) == (
        "nobody has judged whether it would finish")


def test_a_bar_is_a_floor_not_a_fence(amp):
    """Exactly at the bar passes. The refusal is `<`, and off-by-one here is
    the difference between a bar of 60% and a bar of 61%."""
    assert amp.proposal_hold(_p(confidence=0.6, need=0.5)) is None


def test_cost_is_never_a_bar(amp):
    """Stated in `proposal_hold`'s docstring, and worth a row: an expensive
    proposal we are confident about is a decision to spend, which is the
    operator's, not a refusal the harness makes for them."""
    assert amp.proposal_hold(_p(cost_usd=9_000.0)) is None


# ------------------------------------------------------------ how it exits


def test_sharpening_lets_go_when_the_gain_dies(amp):
    """The trap this closes: an architect that keeps claiming headroom while
    the rounds stop moving the odds would hold an objective for ever."""
    p = _p(headroom=0.40, sharpen_log=[{"before": 0.90, "after": 0.905}])
    assert amp.sharpen_converged(p)
    assert amp.proposal_hold(p) is None


def test_sharpening_lets_go_at_the_spend_cap(amp):
    p = _p(headroom=0.40, sharpen_rounds=amp.SHARPEN_HARD_CAP)
    assert "spend cap" in (amp.sharpen_converged(p) or "")
    assert amp.proposal_hold(p) is None


def test_a_round_that_is_still_working_does_not_let_go(amp):
    """The other half. A stopping rule that always stops is not a rule."""
    p = _p(headroom=0.40, sharpen_log=[{"before": 0.50, "after": 0.90}])
    assert amp.sharpen_converged(p) is None
    assert amp.proposal_hold(p) is not None


def test_headroom_nobody_judged_is_not_a_claim_of_none(amp):
    """Absent is not zero. Holding work on a field never written is the
    failure `_num` returns None to avoid."""
    assert amp.proposal_hold(_p(headroom=None)) is None
    assert amp.proposal_hold(_p(headroom=0.05)) is None


# ------------------------------------------------------------------- order


def test_policy_is_reported_before_a_missing_score(amp):
    """A frozen lane says frozen, not "nobody has scored it".

    The order is the point: scoring an unscored proposal is work an operator
    might go and do, and it would not unfreeze the lane.
    """
    _mode(amp, "code", "frozen")
    assert amp.proposal_hold(_p(confidence=None, need=None)) == (
        "code is frozen, so nothing starts in it unattended")


def test_a_bar_is_reported_before_sharpening(amp):
    """Held by a bar AND carrying headroom reports the bar - the reason a
    person can act on."""
    p = _p(confidence=0.45, headroom=0.40)
    assert amp.proposal_hold(p) == "45% odds of finishing, under the 60% bar you set"
    assert amp.held_for_sharpening(p) is False


def test_held_for_sharpening_is_true_only_when_that_is_the_reason(amp):
    """The panel asks this to decide whether the operator is being told to do
    something. Every other hold waits on a person; this one waits on a call the
    harness makes itself, and rendering them alike sends the operator to look
    at a queue that is already draining."""
    assert amp.held_for_sharpening(_p(headroom=0.40)) is True
    assert amp.held_for_sharpening(_p()) is False
    _mode(amp, "code", "frozen")
    assert amp.held_for_sharpening(_p(headroom=0.40)) is False


def test_the_policy_half_is_a_subset_of_the_whole(amp):
    """Whenever policy holds, `proposal_hold` reports exactly that sentence.

    `adopt_proposal` asks the policy half again for itself. If the two could
    disagree, the panel would show one reason and the Adopt button refuse for
    another.
    """
    for setup in (("mode", "frozen"), ("mode", "archived"),
                  ("mode", "maintain"), ("stage", "spec")):
        (_mode if setup[0] == "mode" else _stage)(amp, "code", setup[1])
        for p in (_p(), _p(confidence=None, need=None), _p(headroom=0.9)):
            policy = amp.proposal_policy_hold(p)
            if policy is not None:
                assert amp.proposal_hold(p) == policy
        _mode(amp, "code", "build")
        _stage(amp, "code", "code")


# ------------------------------- the gate is in the function, not the caller


def _propose(amp, **kw) -> str:
    d = amp.direction_store()
    d.setdefault("proposals", []).insert(0, _p(**kw))
    amp._save_direction(d)
    return "p1"


def test_adopt_refuses_a_frozen_lane_and_says_a_click_could_undo_it(amp):
    """The F3 defect: three call sites filtered on `proposal_hold` correctly
    and the fourth - the handler behind the Adopt button - did not check at
    all, so a frozen lane was one click from starting work."""
    _propose(amp)
    _mode(amp, "code", "frozen")
    r = amp.adopt_proposal("p1")
    assert r["ok"] is False
    assert r["hold"] == "code is frozen, so nothing starts in it unattended"
    # Refused, not filtered. The caller asked for this proposal by id, so a
    # silent no-op would leave a button that appears to do nothing.
    assert r["needs_override"] is True
    assert r["proposal_id"] == "p1"


def test_an_override_is_deliberate_and_is_written_down(amp):
    """Overridable rather than absolute - the operator set the switch and may
    unset it. What it may not be is silent.

    Without the note the only trace would be a goal running in a lane whose
    settings say nothing runs, which is how the operator would find out.
    """
    _propose(amp)
    _mode(amp, "code", "frozen")
    r = amp.adopt_proposal("p1", override=True)
    assert r["ok"] is True
    assert r["overrode"] == "code is frozen, so nothing starts in it unattended"
    assert any("OVERRODE: code is frozen" in (n.get("text") or "")
               for n in amp.notes())


def test_an_ordinary_adoption_does_not_claim_an_override(amp):
    """The row that makes the one above mean something: `OVERRODE` has to be
    absent when nothing was overridden, or it is decoration."""
    _propose(amp)
    r = amp.adopt_proposal("p1")
    assert r["ok"] is True and r["overrode"] is None
    assert not any("OVERRODE" in (n.get("text") or "") for n in amp.notes())


def test_a_bar_is_not_re_asked_at_adopt_because_the_click_is_the_attendance(amp):
    """The distinction the whole split exists for. A bar refuses to start work
    UNATTENDED; an operator clicking Adopt is the attendance it was waiting
    for. A mode means the same thing whether or not somebody is looking."""
    _propose(amp, confidence=0.10, need=0.10)
    assert amp.proposal_hold(_p(confidence=0.10, need=0.10)) is not None
    assert amp.adopt_proposal("p1")["ok"] is True


def test_a_question_is_never_adopted(amp):
    _propose(amp, kind="question")
    r = amp.adopt_proposal("p1")
    assert r["ok"] is False and "question" in r["error"]
