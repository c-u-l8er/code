"""The quarter the scorer never sees.

C2 put the calibration table into the prompt that produces the scores. From that
moment every calibration number is measured on data the scorer was shown, and a
scorer that has learned to restate the table it was handed measures as perfectly
calibrated. The shown half cannot tell that apart from getting better.

So one proposal in four is kept out of the block, and the Δ between the halves is
the only number that is not self-reported. These tests pin the three things that
would make that Δ a lie:

- **Membership must not move.** A proposal held out today and shown last week was
  never held out, and a Δ computed across it measures nothing. The split is a
  hash of the proposal's own id for this reason, and the test that matters is
  that an INSERTION - which happens constantly - moves nobody.
- **The halves must partition.** A proposal counted in both sides, or in neither,
  is a proposal the Δ is quietly wrong about.
- **The block must refuse the wrong table.** A held-out proposal that reaches a
  prompt has stopped being held out for good, and no later run can undo it. That
  gate is inside `calibration_block` rather than trusted to its callers.
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
    amp_mod.save_json(amp_mod.CONFIG_PATH, {
        "lanes": {"code": {}, "docs": {}},
        "autonomy": {"adopt_confidence": 0.6, "adopt_need": 0.5},
    })
    return amp_mod


def _settled(amp, pid: str, *, lane: str = "code", confidence: float = 0.8,
             done: bool = True) -> None:
    """One adopted proposal and the finished goal it opened.

    The pair is the unit `calibration` counts: a score that was stated, and an
    outcome that has since happened. A goal still open is counted nowhere, so
    every case here is closed one way or the other.
    """
    gid = f"g-{pid}"
    amp.save_goal({"id": gid, "lane": lane, "objective": "o",
                   "state": "done" if done else "stopped", "tasks": []})
    d = amp.direction_store()
    d.setdefault("proposals", []).append(
        {"id": pid, "lane": lane, "state": "adopted", "goal_id": gid,
         "confidence": confidence, "title": "t"})
    amp._save_direction(d)


def _fill(amp, n: int, *, lane: str = "code", **kw) -> list[str]:
    ids = [f"p{i:03d}" for i in range(n)]
    for pid in ids:
        _settled(amp, pid, lane=lane, **kw)
    return ids


# ------------------------------------------------- who is held out, and forever


def test_membership_is_decided_by_the_id_and_nothing_else(amp):
    """Two calls, same answer - and the answer survives a reload of the module.

    A random draw would pass the first assertion and fail the second, and a
    split that is redrawn per process is not a split: it reports a Δ between two
    samples that were both, at different times, shown.
    """
    ids = [f"p{i:03d}" for i in range(40)]
    first = {pid: amp._held_out(pid) for pid in ids}
    assert {pid: amp._held_out(pid) for pid in ids} == first

    for m in ("amp", "store"):
        sys.modules.pop(m, None)
    import amp as reloaded
    assert {pid: reloaded._held_out(pid) for pid in ids} == first


def test_the_split_is_not_degenerate(amp):
    """A quarter, near enough. All-shown and all-held both pass every other test
    here while making the Δ unmeasurable, so the proportion is asserted."""
    held = sum(amp._held_out(f"p{i:03d}") for i in range(400))
    assert 60 < held < 140, f"{held}/400 held out is not one in {amp.CALIBRATION_HOLDOUT}"


def test_inserting_a_proposal_moves_nobody(amp):
    """The property that rules out "every fourth by position".

    Positional membership is stable right up until a proposal is added, which is
    the one thing this record does all day. An insertion at the front would move
    every case after it across the line, retroactively rewriting which numbers
    were measured on data the scorer had seen.
    """
    ids = _fill(amp, 12)
    before = {pid: amp._held_out(pid) for pid in ids}

    d = amp.direction_store()
    d["proposals"].insert(0, {"id": "brand-new", "lane": "code",
                              "state": "proposed", "title": "t"})
    amp._save_direction(d)

    assert {pid: amp._held_out(pid) for pid in ids} == before


# ------------------------------------------------------- the halves partition


def test_the_two_halves_are_the_whole_and_do_not_overlap(amp):
    """Everything judged is on exactly one side.

    A case in both halves is counted twice by a Δ that assumes it is counted
    once; a case in neither is a case the split silently drops. Both look like a
    working split from the outside.
    """
    _fill(amp, 24)
    whole = {p["id"] for p, _g in amp._calibration_cases()}
    shown = {p["id"] for p, _g in amp._calibration_cases("shown")}
    held = {p["id"] for p, _g in amp._calibration_cases("held")}
    assert whole
    assert shown | held == whole
    assert not shown & held


def test_an_unsettled_goal_is_on_neither_side(amp):
    """A running goal is not evidence yet, in either half.

    `calibration` has always refused to count open goals - counting them as
    failures is how a table talks itself into saying the estimates are worse
    than anyone has shown. The split has to inherit that, or the held-out half
    is measured by a different rule than the shown one.
    """
    _settled(amp, "closed")
    amp.save_goal({"id": "g-open", "lane": "code", "objective": "o",
                   "state": "running", "tasks": []})
    d = amp.direction_store()
    d["proposals"].append({"id": "open", "lane": "code", "state": "adopted",
                           "goal_id": "g-open", "confidence": 0.9, "title": "t"})
    amp._save_direction(d)

    for half in (None, "shown", "held"):
        assert "open" not in {p["id"] for p, _g in amp._calibration_cases(half)}


def test_each_table_says_which_half_it_is(amp):
    """The one way this split can silently lie is a caller holding a table and
    not knowing where it came from."""
    assert amp.calibration()["half"] is None
    assert amp.calibration("shown")["half"] == "shown"
    assert amp.calibration("held")["half"] == "held"


# --------------------------------------------------------------- the Δ itself


def test_too_few_reports_that_instead_of_a_rate(amp):
    """Four judged cases cannot support a percentage - one of them is 25 points.

    This is the same discipline `calibration` applies by counting open goals
    nowhere: the refusal IS the finding. A Δ computed from two such numbers is
    noise wearing the clothes of a result, and this screen is where the
    confidence it invites would be spent.
    """
    _fill(amp, 4)
    s = amp.calibration_split()
    odds = next(m for m in s["measures"] if m["name"] == "odds")
    assert odds["delta"] is None
    assert odds["short"], "a half below the minimum has to say so"
    assert s["min_n"] == amp.CALIBRATION_MIN_N


def test_the_delta_is_the_difference_of_the_two_gaps(amp):
    """|held gap| − |shown gap|, computed from the two tables it reports.

    Asserted against the halves it publishes rather than against a number
    written here: the claim is that the Δ and the two tables are the same
    arithmetic, which is exactly what would stop being true if one of them were
    later measured over a different set of cases.
    """
    _fill(amp, 60)
    s = amp.calibration_split()
    odds = next(m for m in s["measures"] if m["name"] == "odds")
    assert odds["shown_n"] >= s["min_n"] and odds["held_n"] >= s["min_n"]
    assert odds["delta"] == round(abs(odds["held_gap"]) - abs(odds["shown_gap"]), 3)
    assert odds["shown_gap"] == round(s["shown"]["stated"] - s["shown"]["actual"], 3)
    assert odds["held_gap"] == round(s["held"]["stated"] - s["held"]["actual"], 3)


def test_refine_is_not_given_a_delta(amp):
    """It is measured from the sharpen log, not from adopted proposals.

    There is no proposal id to hash, so there is nothing to hold out, and a Δ
    for it would be the same number reported twice as though it were two.
    """
    _fill(amp, 60)
    assert "refine" not in {m["name"] for m in amp.calibration_split()["measures"]}


def test_a_lane_living_entirely_on_one_side_is_named(amp):
    """A Δ can be a statement about which lanes settled, not about the scoring.

    That is invisible in the totals - the headline reads as a finding about
    calibration when it is a finding about lane mix - so the lanes that are on
    one side only are reported by name.
    """
    _fill(amp, 40, lane="code")
    _settled(amp, "lonely", lane="docs")
    s = amp.calibration_split()
    assert "docs" in s["lopsided"]
    assert "code" not in s["lopsided"]
    assert {r["lane"] for r in s["lanes"]} == {"code", "docs"}


# ------------------------------------------------- the prompt boundary refuses


def test_a_prompt_is_refused_the_whole_table(amp):
    """Not served quietly from the wrong half.

    A split that fails open is worse than no split: it goes on reporting a Δ
    that is measured on nothing, and nobody reading the number can tell. The
    check lives here, at the one boundary between the record and a prompt,
    because a held-out proposal that gets into a prompt through any route has
    stopped being held out permanently.
    """
    _fill(amp, 8)
    for bad in (amp.calibration(), amp.calibration("held")):
        with pytest.raises(SystemExit):
            amp.calibration_block(bad)
    assert amp.calibration_block(amp.calibration("shown")) is not None


def test_the_refusal_says_which_table_it_got(amp):
    """The sentence is the whole user interface of this refusal, and "the wrong
    table" would send whoever reads it looking in the wrong place."""
    _fill(amp, 8)
    with pytest.raises(SystemExit) as e:
        amp.calibration_block(amp.calibration("held"))
    assert "held" in str(e.value)
    with pytest.raises(SystemExit) as e:
        amp.calibration_block(amp.calibration())
    assert "whole" in str(e.value)
