"""Three document stores, one defect, and what a workspace switch does to them.

`load_consult`, `load_goal` and `load_specrun` are each typed `-> dict | None`,
and each is read at the start of a long operation, then read AGAIN at the end to
write the answer down. Between the two reads is an architect call, a shell check
or a whole worker - minutes, not milliseconds. All three resolve through a module
global (`CONSULT_DIR`, `GOAL_DIR`, `SPECRUN_DIR`) that `_bind_state` rebinds when
the operator switches workspace, so the second read can miss a file the first
read found.

Unguarded, that cost `TypeError: 'NoneType' object is not subscriptable`, thrown
from whichever line happened to index the result - a line with nothing to do with
the cause. The `require_*` family is the guard, and this is what it is for.

The switch is checked by DOING it, through `use_workspace`, rather than by
rebinding the globals by hand: the claim is about what an operator can cause from
the console, and a test that pokes `globals()` would still pass if the switch
stopped calling `_bind_state` at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE))


@pytest.fixture()
def amp(tmp_path, monkeypatch):
    """`amp` on a throwaway state directory.

    `AMP_HOME` before the import, always: `amp` binds every state path at import
    time, so a test that binds afterwards is pointed at the real `.amp`.
    """
    monkeypatch.setenv("AMP_HOME", str(tmp_path / "state"))
    for m in ("amp", "store"):
        sys.modules.pop(m, None)
    import amp as amp_mod
    assert amp_mod.STATE_ROOT == tmp_path / "state"
    return amp_mod


# The three stores, described the same way so the test below can be one test.
# `saver` writes a minimal document; `dirname` is the global the switch moves.
STORES = [
    ("consult", "require_consult", "load_consult", "CONSULT_DIR",
     lambda amp, i: amp.save_consult(
         {"id": i, "lane": "code", "model": "gpt", "opened_at": amp.now(),
          "status": "open", "trigger": "test", "question": "?", "turns": []})),
    ("goal", "require_goal", "load_goal", "GOAL_DIR",
     lambda amp, i: amp.save_goal(
         {"id": i, "lane": "code", "objective": "o", "status": "open", "tasks": []})),
    ("spec run", "require_specrun", "load_specrun", "SPECRUN_DIR",
     lambda amp, i: amp.save_specrun(
         {"id": i, "lane": "code", "status": "open", "rounds": []})),
]
IDS = [s[0] for s in STORES]


@pytest.mark.parametrize("what,require,load,dirname,save", STORES, ids=IDS)
def test_a_present_document_comes_back_unchanged(amp, what, require, load, dirname, save):
    """The guard is a guard and nothing else - it must not filter or reshape."""
    save(amp, "x0000000001")
    assert getattr(amp, require)("x0000000001") == getattr(amp, load)("x0000000001")


@pytest.mark.parametrize("what,require,load,dirname,save", STORES, ids=IDS)
def test_the_document_going_missing_names_the_id_and_the_directory(
        amp, capsys, what, require, load, dirname, save):
    """`Died` with both facts in it, because neither is guessable afterwards.

    The id says which document, and the directory says where it looked - and the
    directory is the whole bug, since it is the thing that moved. A message with
    only the id sends the operator to look in a folder that is no longer the one
    the process was reading.
    """
    with pytest.raises(amp.Died):
        getattr(amp, require)("x0000000001")
    said = capsys.readouterr().err
    assert "x0000000001" in said
    assert str(getattr(amp, dirname)) in said


@pytest.mark.parametrize("what,require,load,dirname,save", STORES, ids=IDS)
def test_a_workspace_switch_between_the_two_reads_is_refused_not_crashed(
        amp, what, require, load, dirname, save):
    """The defect itself: found, switch, gone.

    This is the backstop rather than the plan - `switch_blocked` is what should
    stop the switch happening at all while something is in flight - but the
    backstop is the half that stays true no matter what else gets added to the
    console later.
    """
    save(amp, "x0000000001")
    assert getattr(amp, require)("x0000000001")["id"] == "x0000000001"
    before = getattr(amp, dirname)

    amp.add_workspace("Other", slug="other")
    amp.use_workspace("other")
    assert getattr(amp, dirname) != before, "the switch did not move the directory"

    with pytest.raises(amp.Died):
        getattr(amp, require)("x0000000001")


@pytest.mark.parametrize("what,require,load,dirname,save", STORES, ids=IDS)
def test_load_still_answers_none_for_the_callers_that_ask_it_a_question(
        amp, what, require, load, dirname, save):
    """`require_*` is an addition, not a replacement.

    Plenty of callers say `load_goal(gid) or {}` and mean it: a missing document
    is an ordinary answer there, usually "this goal has been deleted, stop". If
    the conversion had gone through `load_*` itself, every one of those would
    have started raising instead.
    """
    assert getattr(amp, load)("x0000000001") is None
