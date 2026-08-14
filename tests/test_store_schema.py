"""The schema field, made capable of disagreeing.

It was written once with `INSERT OR IGNORE` and read back nowhere; `status()`
reported `SCHEMA`, the running code's own constant. So the one field that looks
like a version check was comparing the program against itself, and could not
fail.

The test that carries this file is `test_a_newer_file_is_refused_before_it_is
_touched`: it is not enough that opening raises. Nothing may have been written
first, because an older reader running its migrations over a newer file is the
loss this refusal exists to stop.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE))


@pytest.fixture()
def st(tmp_path, monkeypatch):
    # `AMP_HOME` as well as `bind`, and both to the same directory: `amp`
    # calls `store.bind(STATE_ROOT)` at import, so a test that binds by hand
    # and then imports `amp` gets silently re-pointed at the REAL `.amp`.
    monkeypatch.setenv("AMP_HOME", str(tmp_path / "state"))
    for m in ("amp", "store"):
        sys.modules.pop(m, None)
    import store as store_mod
    store_mod.bind(tmp_path / "state")
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    # A sweep over an empty directory records nothing and so never opens the
    # database. Open it here, the way the harness's first write would.
    store_mod._connect()
    return store_mod


def _bump(st, to: int) -> None:
    """Say the file was written by a build that knows more than this one."""
    c = sqlite3.connect(str(st.db_path()))
    c.execute("UPDATE meta SET value=? WHERE key='schema'", (str(to),))
    c.commit()
    c.close()


def _reopen(st):
    """Drop the cached connection, as a restart would.

    Closed, not just forgotten: WAL means an open connection can be holding
    committed bytes that are not in the main file yet, and a byte comparison
    of the main file alone would then pass by not looking.
    """
    if st._CONN is not None:
        st._CONN.close()
    st._CONN = None


def _files(st) -> dict[str, bytes]:
    """The database and everything sqlite keeps beside it."""
    p = Path(st.db_path())
    return {s: (p.parent / (p.name + s)).read_bytes()
            for s in ("", "-wal", "-shm") if (p.parent / (p.name + s)).exists()}


def test_the_file_says_a_number_and_status_reports_it(st):
    """Two numbers, from two places."""
    s = st.status()
    assert s["schema_code"] == st.SCHEMA
    assert s["schema_db"] == st.SCHEMA
    assert "schema" not in s, "the single self-agreeing field is still there"


def test_a_newer_file_is_refused(st):
    _bump(st, st.SCHEMA + 1)
    _reopen(st)
    with pytest.raises(st.SchemaTooNew) as e:
        st._connect()
    assert e.value.found == st.SCHEMA + 1
    assert e.value.known == st.SCHEMA
    # Both numbers in the sentence the operator actually reads.
    assert str(st.SCHEMA + 1) in str(e.value)
    assert str(st.SCHEMA) in str(e.value)


def test_a_newer_file_is_refused_before_it_is_touched(st):
    """The point. A refusal after the migrations have run is not a refusal.

    Compared byte for byte: the whole reason to check early is that every line
    after the check writes.
    """
    _bump(st, st.SCHEMA + 1)
    _reopen(st)
    before = _files(st)
    with pytest.raises(st.SchemaTooNew):
        st._connect()
    assert _files(st) == before


def test_the_refusal_reaches_status_and_does_not_raise(st):
    """Settings must still render. A mirror that cannot open is not a crash."""
    _bump(st, st.SCHEMA + 9)
    _reopen(st)
    s = st.status()
    assert s["ok"] is False
    assert s["schema_db"] == st.SCHEMA + 9
    assert s["schema_code"] == st.SCHEMA
    assert str(st.SCHEMA + 9) in s["error"]


def test_record_still_refuses_to_raise_over_it(st):
    """`record` cannot take the harness down. Not even for this."""
    _bump(st, st.SCHEMA + 1)
    _reopen(st)
    p = Path(st.db_path()).parent / "board.json"
    p.write_text("{}\n")
    assert st.record(p, "{}\n") is False


def test_an_older_file_is_migrated_not_refused(st):
    """Older is a migration. Only newer is a refusal.

    And after it opens, the file says the current number - the old
    `INSERT OR IGNORE` left it saying the old one forever, which is a version
    field that goes stale the first time it would have mattered.
    """
    _bump(st, st.SCHEMA - 1)
    _reopen(st)
    st._connect()
    assert st.stored_schema() == st.SCHEMA


def test_a_file_that_never_said_is_not_a_refusal(st):
    """Written before `meta` existed. Older-or-equal by definition."""
    c = sqlite3.connect(str(st.db_path()))
    c.execute("DELETE FROM meta WHERE key='schema'")
    c.commit()
    c.close()
    _reopen(st)
    st._connect()
    assert st.stored_schema() == st.SCHEMA


def test_an_unreadable_number_does_not_take_the_backup_away(st):
    """Refusing on a string nobody can compare would be a guess with a cost."""
    c = sqlite3.connect(str(st.db_path()))
    c.execute("UPDATE meta SET value='banana' WHERE key='schema'")
    c.commit()
    c.close()
    _reopen(st)
    st._connect()  # opens
    assert st.stored_schema() == st.SCHEMA


def test_the_cli_reports_the_refusal_instead_of_raising(st, capsys):
    """`amp db status` reads `s['docs']`, which a refused status has not got.

    This is the half of the change that is not in `store.py` at all: adding a
    failure mode means finding everyone who assumed there wasn't one.
    """
    _bump(st, st.SCHEMA + 1)
    _reopen(st)
    import types
    import amp
    assert amp.store is st, "amp is talking to a different store than this test"
    rc = amp.cmd_db(types.SimpleNamespace(sub="status"))
    out = capsys.readouterr().out
    assert rc == 1, "a script cannot tell the backup is unusable from the exit code"
    assert str(st.SCHEMA + 1) in out


def test_stored_schema_does_not_go_through_connect(st):
    """It has to answer in exactly the case where `_connect` refuses."""
    _bump(st, st.SCHEMA + 1)
    _reopen(st)
    assert st.stored_schema() == st.SCHEMA + 1
    assert st._CONN is None, "reading the number opened the connection it reports on"
