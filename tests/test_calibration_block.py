"""The measurement of the scores has to reach the prompt that makes them.

It reached the console, the report and the generated advice, and not one
prompt. These tests are what stops it falling back out: the last one asserts
the block's bytes are in the REAL assembled scoring context, not that the
function exists.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE))

# `half: "shown"` on both: a prompt only ever sees the shown half, and
# `calibration_block` refuses a table that does not say it is that half. A fixture
# without it would be testing a call the real code refuses to make.
FULL = {
    "half": "shown",
    "n": 7, "bar": 0.6, "need_bar": 0.5, "stated": 0.72, "actual": 0.43,
    "bands": [{"from": 0.0, "to": 0.5, "n": 0, "finished": 0, "rate": None},
              {"from": 0.85, "to": 1.0, "n": 4, "finished": 2, "rate": 0.5}],
    "need": {"n": 5, "stated": 0.66, "moved": 0.4},
    "cost": {"n": 3, "stated": 4.0, "actual": 9.5},
    "refine": {"n": 12, "stated": 0.31, "actual": 0.25, "gain": 0.011, "floor": 0.15},
}
EMPTY = {"half": "shown", "n": 0, "need": {"n": 0}, "cost": {"n": 0}, "refine": {"n": 0}}


@pytest.fixture()
def amp(tmp_path, monkeypatch):
    monkeypatch.setenv("AMP_HOME", str(tmp_path / "state"))
    for m in ("amp", "store"):
        sys.modules.pop(m, None)
    import amp as amp_mod
    return amp_mod


def test_nothing_measured_renders_nothing(amp):
    """Silence, not a table of `None`s.

    An empty block shows up in the Blueprint inspector's `empty[]` list, which
    is how "the table was empty" is told apart from "the block was unwired".
    """
    assert amp.calibration_block(EMPTY) == ""


def test_every_line_carries_its_sample_size(amp):
    """"3 of 4" and "300 of 400" are not the same evidence."""
    out = amp.calibration_block(FULL)
    assert "over 7 judged" in out
    assert "over 5 judged" in out
    assert "over 3 finished objective(s)" in out
    assert "over 12 attempts" in out
    assert "2 of 4 finished" in out


def test_a_band_with_no_cases_is_not_printed(amp):
    """A band nobody scored into says nothing about anything."""
    assert "0.00-0.50" not in amp.calibration_block(FULL)


def test_the_configured_bar_is_stated_beside_the_measurement(amp):
    """The defect was the prompt stating the bar and withholding the reliability
    of the number compared against it. Both, or neither."""
    out = amp.calibration_block(FULL)
    assert "adopt bar is 0.6" in out
    assert "need bar is 0.5" in out


def test_it_reaches_the_scoring_prompt(amp, monkeypatch):
    """The one that matters: real bytes in the real assembled context."""
    # Takes the half, because the real caller asks for one. A stub with the
    # narrower signature would pass here and fail against the code as written.
    monkeypatch.setattr(amp, "calibration", lambda half=None: FULL)
    with amp.trace_blocks() as rec:
        ctx = amp._explore_context("demo", web=False)
    blocks = [r for r in rec if r["fn"] == "calibration_block"]
    assert blocks, "calibration_block is not built while the scoring context is"
    assert blocks[0]["text"], "it was built and produced nothing"
    assert blocks[0]["text"] in ctx, "it was built and did not reach the prompt"
