"""What `verify()` promises the operator, asserted.

The defect was not a wrong number. Every count was right. It was that a file
over the size cap was filed under `missing`, whose whole meaning is "sweep
again and this goes away" - so the operator sweeps, the report does not move,
and nothing in the output says there is no sweep that would.

The test that carries this file is the last one: it sweeps, and asserts the
report is unchanged. That is the loop the old report could not falsify.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE))


@pytest.fixture()
def st(tmp_path):
    """`store` bound to a throwaway state directory.

    Bound rather than imported-with-AMP_HOME: `store.bind()` is the only way
    the real harness points it anywhere, so using it is also a check that it
    still works that way.
    """
    for m in ("amp", "store"):
        sys.modules.pop(m, None)
    import store as store_mod
    store_mod.bind(tmp_path / "state")
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    return store_mod


def _write(st, name: str, size: int) -> Path:
    """A file of exactly `size` bytes, without spending `size` bytes.

    `truncate` leaves a sparse file: `st_size` is the full length and almost
    no blocks are allocated. That is not a shortcut around the test - the code
    under test asks `stat` for the size and never reads the body, which is
    itself the property being relied on, so a sparse file exercises the real
    path. A test that writes 16 MB of `a` to prove a size check only proves
    the tmpdir had 16 MB.
    """
    p = Path(st.db_path()).parent / name
    with open(p, "wb") as f:
        f.truncate(size)
    return p


def test_a_file_over_the_cap_is_named_with_its_size(st):
    """Not just listed - measured, beside the cap it is over.

    "too large" without a number is a verdict the reader has to go and check.
    """
    _write(st, "huge.json", st.MAX_BYTES + 1)
    st.backup()
    v = st.verify()
    assert [t["path"] for t in v["too_large"]] == ["huge.json"]
    assert v["too_large"][0]["bytes"] == st.MAX_BYTES + 1
    assert v["too_large"][0]["cap"] == st.MAX_BYTES


def test_it_is_not_reported_as_missing(st):
    """The defect itself. `missing` means a sweep fixes it; this is not that."""
    _write(st, "huge.json", st.MAX_BYTES + 1)
    _write(st, "small.json", 10)
    st.backup()
    v = st.verify()
    assert v["missing"] == [], v["missing"]
    assert v["current"] == 1


def test_it_is_not_reported_as_held(st):
    """`held` means "gone from disk, kept here". This file is on disk.

    A path in both lists reads as two different problems when it is one.
    """
    _write(st, "huge.json", st.MAX_BYTES + 1)
    st.backup()
    v = st.verify()
    assert v["held"] == []


def test_a_file_under_the_cap_is_untouched_by_any_of_this(st):
    _write(st, "small.json", 10)
    st.backup()
    v = st.verify()
    assert v["too_large"] == []
    assert v["current"] == 1
    assert v["clean"] is True


def test_sweeping_does_not_change_the_report(st):
    """The falsification the old report could not offer.

    `clean` stays true on purpose: clean means "a sweep has nothing left to
    do", and a sweep has nothing it CAN do here. Reporting the mirror as
    permanently dirty over a file it was designed to refuse teaches the
    operator to ignore the flag. The oversized file is said out loud in its
    own verdict instead.
    """
    _write(st, "huge.json", st.MAX_BYTES + 1)
    st.backup()
    before = st.verify()
    st.backup()
    st.backup()
    after = st.verify()
    assert after == before
    assert after["clean"] is True
    assert after["too_large"], "the one thing that must still be said, was not"
