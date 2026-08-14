"""The complete history, made reachable.

`store` has kept every prior version since it was written. There was no way to
put one back: the operator could print the good version and had to copy it out
of a terminal by hand, under the one condition the feature is for, which is a
state file corrupt enough that the console will not start.

The test that carries this file is `test_a_truncated_board_is_recoverable`. It
does the whole thing: write a board, mirror it, destroy it, confirm the load
that reads it fails, restore, confirm the load succeeds and the contents are
the ones that were there. Every other test here is a way that could go wrong.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE))


@pytest.fixture()
def amp(tmp_path, monkeypatch):
    """`amp` and `store` on the same throwaway state directory.

    Both, to the same path: `amp` calls `store.bind(STATE_ROOT)` at import, so
    binding by hand and importing afterwards silently re-points at the real
    `.amp/`.
    """
    monkeypatch.setenv("AMP_HOME", str(tmp_path / "state"))
    for m in ("amp", "store"):
        sys.modules.pop(m, None)
    import amp as amp_mod
    assert amp_mod.STATE_ROOT == tmp_path / "state"
    return amp_mod


def _restore(amp, path, seq=None):
    return amp.cmd_db(types.SimpleNamespace(sub="restore", path=path, seq=seq))


# ------------------------------------------------------------------ the point


def test_a_truncated_board_is_recoverable(amp, capsys):
    """The whole feature, end to end, on the file it exists for."""
    good = {"tasks": {"code": [{"task_id": "t1"}]}}
    amp.save_json(amp.BOARD_PATH, good)
    amp.store.backup()

    amp.BOARD_PATH.write_text("")           # what a bad write leaves behind
    with pytest.raises(SystemExit):
        amp.load_json(amp.BOARD_PATH, None)

    assert _restore(amp, ".board.json") == 0
    assert amp.load_json(amp.BOARD_PATH, None) == good


def test_the_restore_is_recorded(amp):
    """A state file that changed under the board with no trace is the thing
    this subsystem exists to prevent. A restore is exactly that unless it says
    so."""
    amp.save_json(amp.BOARD_PATH, {"a": 1})
    amp.store.backup()
    amp.save_json(amp.BOARD_PATH, {"a": 2})
    amp.store.backup()
    _restore(amp, ".board.json")
    ns = [n for n in amp.notes() if n["kind"] == "restore"]
    assert len(ns) == 1
    assert ".board.json" in ns[0]["text"]


def test_it_writes_through_save_text(amp, monkeypatch):
    """A recovery less durable than an ordinary write can leave you worse off
    than the corruption did."""
    amp.save_json(amp.BOARD_PATH, {"a": 1})
    amp.store.backup()
    amp.BOARD_PATH.write_text("")
    seen = []
    real = amp.save_text
    monkeypatch.setattr(amp, "save_text", lambda p, t: (seen.append(p), real(p, t))[1])
    _restore(amp, ".board.json")
    assert amp.BOARD_PATH in seen, "the restore wrote its own bytes"


# --------------------------------------------------------- choosing a revision


def test_the_default_is_the_newest_revision_that_differs(amp, capsys):
    """Not simply the newest.

    The newest is usually the corruption - the mirror copied it faithfully.
    "The last version that was not this" is the request an operator has.
    """
    for n in (1, 2, 3):
        amp.save_json(amp.BOARD_PATH, {"a": n})
        amp.store.backup()
    _restore(amp, ".board.json")
    assert amp.load_json(amp.BOARD_PATH, None) == {"a": 2}


def test_an_explicit_revision_is_obeyed(amp):
    seqs = []
    for n in (1, 2, 3):
        amp.save_json(amp.BOARD_PATH, {"a": n})
        amp.store.backup()
        seqs.append(amp.store.history(".board.json")[0]["seq"])
    _restore(amp, ".board.json", seq=seqs[0])
    assert amp.load_json(amp.BOARD_PATH, None) == {"a": 1}


def test_an_unknown_revision_names_the_ones_there_are(amp):
    amp.save_json(amp.BOARD_PATH, {"a": 1})
    amp.store.backup()
    seq = amp.store.history(".board.json")[0]["seq"]
    with pytest.raises(SystemExit):
        _restore(amp, ".board.json", seq=999999)
    r = amp.store.restorable(".board.json", 999999)
    assert str(seq) in r["error"], "the refusal does not say what IS held"


def test_a_file_with_no_history_is_named_not_guessed(amp):
    with pytest.raises(SystemExit):
        _restore(amp, "never-existed.json")


def test_identical_history_is_not_a_restore(amp, capsys):
    """Every held revision equals disk. There is nothing to go back to, and
    saying "restored" would be a lie about a file nobody changed."""
    amp.save_json(amp.BOARD_PATH, {"a": 1})
    amp.store.backup()
    with pytest.raises(SystemExit):
        _restore(amp, ".board.json")


# ------------------------------------------------------------- what it refuses


def test_unswept_bytes_are_kept_before_they_are_overwritten(amp):
    """The rule is "do not lose what is on disk", and the honest way to obey it
    is to keep it - not to refuse and make the operator argue.

    The first version of this refused instead, and needed a `--force` in
    exactly the case the command exists for.
    """
    amp.save_json(amp.BOARD_PATH, {"a": 1})
    amp.store.backup()
    amp.BOARD_PATH.write_text('{"a": 99}\n')   # never swept
    assert _restore(amp, ".board.json") == 0
    assert amp.load_json(amp.BOARD_PATH, None) == {"a": 1}
    # And the 99 is not gone: it is the newest thing that differs, so the
    # restore is its own undo.
    assert _restore(amp, ".board.json") == 0
    assert amp.load_json(amp.BOARD_PATH, None) == {"a": 99}


def test_a_truncated_file_needs_no_extra_argument(amp):
    """Zero bytes, never swept. The corruption this feature is FOR."""
    amp.save_json(amp.BOARD_PATH, {"a": 1})
    amp.store.backup()
    amp.BOARD_PATH.write_text("")
    assert _restore(amp, ".board.json") == 0
    assert amp.load_json(amp.BOARD_PATH, None) == {"a": 1}


def test_a_file_the_mirror_refuses_is_not_destroyed(amp):
    """The one case that still refuses: bytes that cannot be kept.

    `.secrets.json` is excluded from the mirror by name, so there is no copy
    to fall back to and the restore must not proceed.
    """
    p = amp.STATE_ROOT / ".secrets.json"
    amp.save_text(p, '{"k": 1}\n')
    # Give it a history by another route, so the refusal is about the CURRENT
    # bytes and not about there being nothing held.
    amp.store._record(p, '{"k": 0}\n', force=True)
    amp.save_text(p, '{"k": 2}\n')
    with pytest.raises(SystemExit):
        _restore(amp, ".secrets.json")
    assert p.read_text() == '{"k": 2}\n'


def test_restorable_writes_nothing(amp):
    """It chooses and explains. The write belongs to the one durable path."""
    amp.save_json(amp.BOARD_PATH, {"a": 1})
    amp.store.backup()
    amp.BOARD_PATH.write_text("")
    before = amp.BOARD_PATH.read_bytes()
    r = amp.store.restorable(".board.json")
    assert r["ok"] and r["body"]
    assert amp.BOARD_PATH.read_bytes() == before
