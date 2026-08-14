"""The second copy.

`store` is the only thing in this harness whose entire job is not losing data,
and it is the one module that may never raise: `record` is called from inside
every durable write, so a mirror that throws takes the harness with it. That
guarantee makes it hard to notice when it stops working - a mirror that has
quietly recorded nothing since Tuesday looks exactly like one with nothing to
record.

So the round trip is asserted on the BYTES, and the failure paths are asserted
on the counters, because a swallowed exception that increments nothing is a
swallowed exception nobody will ever see.
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
    """`store` bound to a throwaway state root, through `amp` as in real life.

    Imported via `amp` rather than bound by hand: `amp` is what calls
    `store.bind`, and a test that binds it itself is testing a wiring nothing
    uses.
    """
    monkeypatch.setenv("AMP_HOME", str(tmp_path / "state"))
    for m in ("amp", "store"):
        sys.modules.pop(m, None)
    import amp  # noqa: F401  binds the store
    import store as store_mod
    assert store_mod.db_path() == tmp_path / "state" / store_mod.DB_NAME
    # `amp` creates the state root lazily, on the first durable write. Every
    # test here starts by putting a file in it, so make it now.
    store_mod._ROOT.mkdir(parents=True, exist_ok=True)
    return store_mod


def _put(st, rel: str, text: str) -> Path:
    """Write a file the way the harness does, and mirror exactly those bytes."""
    p = st._ROOT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    st.record(p, text)
    return p


# ------------------------------------------------------------- the round trip


def test_what_went_in_comes_back_out(st):
    _put(st, "board.json", '{"tasks": {}}')
    assert st.body("board.json") == b'{"tasks": {}}'


@pytest.mark.parametrize("text", [
    "x" * 40_000,          # compresses to almost nothing
    "\x00\x01\x02" * 3,    # too short to be worth compressing
    "",                    # nothing at all is still a revision
    "héllo — ünicode",
])
def test_the_body_survives_compression(st, text):
    """`_pack` stores zlib or raw depending on which is smaller, so both
    branches are live in normal use and only one of them is ever exercised by
    any single document."""
    _put(st, "doc.json", text)
    assert st.body("doc.json") == text.encode()


def test_an_identical_write_is_not_a_revision(st):
    """The board is rewritten on every worker heartbeat. A history of a
    thousand identical boards buries the one change worth finding."""
    p = _put(st, "board.json", "same")
    assert st.record(p, "same") is False
    assert len(st.history("board.json")) == 1


def test_a_different_write_is(st):
    _put(st, "board.json", "one")
    _put(st, "board.json", "two")
    h = st.history("board.json")
    assert len(h) == 2
    assert h[0]["seq"] > h[1]["seq"], "history is not newest-first"
    assert st.body("board.json") == b"two"
    assert st.body("board.json", h[1]["seq"]) == b"one", (
        "the older revision is indexed but does not hold its bytes")


def test_the_current_body_is_a_revision_like_any_other(st):
    """`docs.seq` NAMES the revision holding the current body rather than
    storing it a second time, which is the arrangement that makes it
    impossible for the two to disagree."""
    _put(st, "board.json", "one")
    _put(st, "board.json", "two")
    newest = st.history("board.json")[0]["seq"]
    assert st.body("board.json") == st.body("board.json", newest)


def test_a_file_outside_the_state_root_is_not_mirrored(st, tmp_path):
    stray = tmp_path / "elsewhere.json"
    stray.write_text("not ours")
    assert st.record(stray, "not ours") is False
    assert st.body("elsewhere.json") is None


# ---------------------------------------------------------------- the exclusions


@pytest.mark.parametrize("rel, why", [
    (".secrets.json", "excluded by name"),
    ("amp.db", "excluded by name"),
    ("amp.db-wal", "excluded by name"),
    ("worktrees/code/amp.py", "excluded directory"),
    ("x/__pycache__/amp.pyc", "excluded directory"),
    ("board.json.tmp", "temporary file"),
    (".console.lock", "temporary file"),
    (".console.token", "temporary file"),
    # The per-writer temp name from the locking work still ends `.tmp`, so it
    # is still excluded. `skipped` asks `endswith`, not `suffix ==`, and the
    # two changes were made months and files apart.
    ("board.json.31337.7f2a.tmp", "temporary file"),
])
def test_what_never_enters_the_mirror(st, rel, why):
    assert st.skipped(rel) == why


@pytest.mark.parametrize("rel", ["board.json", "goals/g1.json",
                                 "ws/other/board.json", "notes.md"])
def test_what_does(st, rel):
    assert st.skipped(rel) is None


def test_the_secrets_file_is_refused_at_the_door(st):
    """Not by the sweep and not by the caller - here, at the one place every
    write passes through. This database is meant to leave the machine."""
    p = _put(st, ".secrets.json", '{"openrouter_key": "sk-live-do-not-mirror"}')
    assert p.is_file(), "the test did not write the file it is checking"
    assert st.body(".secrets.json") is None
    assert st.verify()["missing"] == [], "a skipped file is not a missing one"


@pytest.mark.parametrize("rel", ["ws/other/board.json", "board.json",
                                 "goals/g1.json", "ws/only-two-parts"])
def test_a_document_knows_which_workspace_it_belongs_to(st, rel):
    """Derived from the path, not read from the registry, so the mirror keeps
    working during a workspace switch - the one moment the registry and the
    files disagree."""
    _put(st, rel, "x")
    want = "other" if rel.startswith("ws/other/") else "core"
    assert [d for d in st.docs() if d["path"] == rel][0]["workspace"] == want


# ------------------------------------------------------------------- the verdicts


def test_a_mirrored_file_is_current(st):
    _put(st, "board.json", "one")
    v = st.verify()
    assert v["clean"] and v["current"] >= 1
    assert v["stale"] == [] and v["missing"] == [] and v["held"] == []


def test_a_file_changed_behind_the_mirror_is_stale(st):
    p = _put(st, "board.json", "one")
    p.write_text("changed without telling anyone")
    v = st.verify()
    assert "board.json" in v["stale"] and not v["clean"]


def test_a_file_never_mirrored_is_missing(st):
    (st._ROOT / "unswept.json").write_text("nobody recorded this")
    v = st.verify()
    assert "unswept.json" in v["missing"] and not v["clean"]
    assert st.backup()["written"] >= 1
    assert st.verify()["clean"], "a sweep did not fix what it is documented to fix"


def test_a_file_deleted_from_disk_is_held(st):
    """The point of the exercise. The mirror is additive: nothing here has a
    delete, so a file removed on disk is reported as held, not dropped."""
    p = _put(st, "gone.json", "the only copy")
    p.unlink()
    v = st.verify()
    assert v["held"] == ["gone.json"]
    assert v["clean"], "a deleted file is not something a sweep can fix"
    assert st.body("gone.json") == b"the only copy"


def test_a_file_over_the_cap_is_named_as_such_and_not_as_missing(st):
    """Without its own verdict these sat under `missing`, whose docstring says
    a sweep fixes it. The operator sweeps, the report does not change, and
    nothing in the output says there is no sweep that would. Nothing was
    broken; the report was unfalsifiable, which is worse.
    """
    p = st._ROOT / "huge.bin"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\0" * (st.MAX_BYTES + 1))
    assert st.record(p) is False
    v = st.verify()
    assert [t["path"] for t in v["too_large"]] == ["huge.bin"]
    assert "huge.bin" not in v["missing"] and "huge.bin" not in v["held"]
    assert v["clean"], "a permanent refusal must not read as a dirty mirror"


def test_an_oversized_body_is_refused_without_touching_the_file(st):
    p = st._ROOT / "big.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("small on disk")
    assert st.record(p, "x" * (st.MAX_BYTES + 1)) is False
    assert st.body("big.json") is None


# --------------------------------------------------------------- the change feed


def test_the_feed_is_monotonic_and_exhaustible(st):
    """`seq` is what a cloud sync would hold as its cursor, so "everything
    since N" has to stay true across pruning and across restarts."""
    for i in range(3):
        _put(st, "board.json", f"v{i}")
    first = st.changes(0)
    assert [c["path"] for c in first["changes"]] == ["board.json"] * 3
    assert first["cursor"] == first["changes"][-1]["seq"]
    assert first["more"] is False
    assert st.changes(first["cursor"])["changes"] == []


def test_the_feed_says_when_there_is_more(st):
    for i in range(5):
        _put(st, "board.json", f"v{i}")
    page = st.changes(0, limit=2)
    assert len(page["changes"]) == 2 and page["more"] is True


# ------------------------------------------------------------------- upkeep


def test_pruning_cannot_drop_the_current_version(st):
    """The window would spare it anyway for any sane `keep`, but "the newest
    survives because it sorts first" is a coincidence, and this is the one
    thing a prune must not be able to drop."""
    for i in range(6):
        _put(st, "board.json", f"v{i}")
    r = st.prune(keep=1)
    assert r["removed"] == 5
    assert st.body("board.json") == b"v5"
    assert len(st.history("board.json")) == 1


def test_pruning_does_not_renumber_what_a_remote_has_seen(st):
    for i in range(4):
        _put(st, "board.json", f"v{i}")
    head = st.changes(0)["cursor"]
    st.prune(keep=1)
    assert st.changes(0)["cursor"] == head


def test_a_restore_offers_the_last_version_that_was_not_this_one(st):
    """Not simply the newest: the newest is usually the corruption, because
    the mirror faithfully copied it."""
    _put(st, "board.json", "the good one")
    good = st.history("board.json")[0]["seq"]
    _put(st, "board.json", "")          # truncated, and mirrored as such
    r = st.restorable("board.json")
    assert r["ok"] and r["seq"] == good
    assert r["body"] == b"the good one"
    assert r["reason"] == "newest revision that differs from disk"


def test_a_restore_names_what_it_cannot_do(st):
    """Every failure here is something the operator has to be told, and
    `die("unknown")` is not that."""
    assert "nothing is held" in st.restorable("never-seen.json")["error"]
    _put(st, "board.json", "one")
    same = st.restorable("board.json")
    assert not same["ok"] and "byte-identical" in same["error"]
    assert "no revision 99999" in st.restorable("board.json", 99999)["error"]


def test_a_restore_writes_nothing(st):
    """It chooses and explains. The write belongs to `amp`, which owns the one
    durable path to disk - a restore with its own writer would be the single
    write in the program not covered by the fsync discipline."""
    p = _put(st, "board.json", "the good one")
    _put(st, "board.json", "truncated")
    assert st.restorable("board.json")["ok"]
    assert p.read_text() == "truncated", "restorable() wrote to disk"


# ------------------------------------------------------------ the failure paths


def test_the_mirror_can_never_take_the_harness_down(st, monkeypatch):
    """`record` is called from inside every durable write. The guarantee it
    makes is not "it works" - it is "it never raises"."""
    def broken():
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(st, "_connect", broken)
    before = st._FAILS["n"]
    assert st.record(st._ROOT / "board.json", "x") is False
    # Counted, not equal to `before + 1`: one `record` asks the database twice
    # on a cold cache - once through `enabled()` for the settings, once to
    # write - and each refusal is its own note. The guarantee is that a failure
    # is never silent, not that it is counted exactly once.
    assert st._FAILS["n"] > before, (
        "the exception was swallowed and counted nowhere - nobody will see it")
    assert st._FAILS["last"].startswith("OperationalError")
    reported = st.status()["failures"]
    assert reported["n"] > 0 and reported["last"].startswith("OperationalError"), (
        "status() does not carry the failure forward to the operator")


def test_a_newer_file_is_refused_rather_than_migrated_backwards(st):
    """An older reader that opens a newer file does not fail - it runs its own
    migrations against it, drops a column it does not recognise, and writes a
    worse version back. That is the exact shape of loss this module exists to
    prevent, and the only moment to stop it is before the first write."""
    _put(st, "board.json", "one")
    c = st._connect()
    c.execute("UPDATE meta SET value='999' WHERE key='schema'")
    c.commit()
    c.close()
    st._CONN = None
    with pytest.raises(st.SchemaTooNew) as e:
        st._connect()
    assert e.value.found == 999 and e.value.known == st.SCHEMA
    # And it is still refused when asked through the front door, without the
    # report itself being the thing that touches the file.
    assert st.stored_schema() == 999


def test_the_status_reports_two_schema_numbers(st):
    """One number cannot disagree with itself. `"schema": SCHEMA` reported the
    running code to a reader who was being invited to check the file."""
    _put(st, "board.json", "one")
    s = st.status()
    assert s["schema_code"] == st.SCHEMA
    assert s["schema_db"] == st.SCHEMA
    assert s["exists"] is True


def test_the_mirror_can_be_switched_off_and_nothing_is_recorded(st):
    st.set_setting("mirror", False)
    assert st.enabled() is False
    p = st._ROOT / "board.json"
    p.write_text("while off")
    assert st.record(p, "while off") is False
    assert st.body("board.json") is None
    # A sweep is explicit, so it is not what `mirror` switches off.
    assert st.backup()["written"] >= 1
    assert st.body("board.json") == b"while off"


@pytest.mark.parametrize("key, value", [
    ("history_keep", 0), ("history_keep", 10_001),
    ("sweep_min", -1), ("sweep_min", 1441),
    ("nonesuch", 1),
])
def test_a_setting_out_of_range_is_refused_not_clamped(st, key, value):
    """Written and clamped at read time is where the operator never learns the
    number is not where they set it."""
    with pytest.raises(ValueError):
        st.set_setting(key, value)
