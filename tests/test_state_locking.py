"""One writer at a time, across processes.

Every state document here is written WHOLE, from a dict read a moment earlier.
The locks guarding those read-modify-writes were `threading.Lock`, which holds
against the console's own worker threads and against nothing else - so `amp
<cmd>` on a terminal, writing the same file while the console runs, silently
dropped whichever write finished first.

The test that carries this file is `test_three_processes_all_land`. It is not
about exceptions: it counts. Three processes append forty records each and the
file has to hold a hundred and twenty. A lost update raises nothing, produces
no bad bytes and leaves no trace, which is exactly why the assertion has to be
on the count.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE))


@pytest.fixture()
def amp(tmp_path, monkeypatch):
    """`amp` on a throwaway state directory.

    `AMP_HOME` before the import, always: `amp` binds every state path at
    import time, so a test that binds afterwards is pointed at the real `.amp`.
    """
    monkeypatch.setenv("AMP_HOME", str(tmp_path / "state"))
    for m in ("amp", "store"):
        sys.modules.pop(m, None)
    import amp as amp_mod
    assert amp_mod.STATE_ROOT == tmp_path / "state"
    return amp_mod


def _child(home: Path, body: str, *args: str) -> subprocess.Popen:
    src = f"import sys, time\nsys.path.insert(0, {str(CODE)!r})\nimport amp\n" + body
    env = dict(os.environ, AMP_HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
    return subprocess.Popen([sys.executable, "-c", src, *args], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


# ------------------------------------------------------------------ the point


# The window is opened on purpose. `record_task` reads and writes in a few
# microseconds, so three of them collide sometimes; a test that fails
# sometimes is a test nobody believes. The sleep is INSIDE the critical
# section, so a lock that works makes it invisible and a lock that does not
# makes it certain.
_APPEND = """
lane, n = sys.argv[1], int(sys.argv[2])
for i in range(n):
    with amp._BOARD_LOCK:
        b = amp.board()
        b.setdefault("tasks", {}).setdefault(lane, []).insert(0, {"task_id": f"{lane}-{i}"})
        time.sleep(0.004)
        amp.save_json(amp.BOARD_PATH, b)
"""


def test_three_processes_all_land(amp, tmp_path):
    """A hundred and twenty appends from three processes, and a hundred and
    twenty records."""
    home = tmp_path / "state"
    home.mkdir(parents=True, exist_ok=True)
    kids = [_child(home, _APPEND, f"l{i}", "40") for i in range(3)]
    for k in kids:
        out, err = k.communicate(timeout=180)
        assert k.returncode == 0, err

    b = json.loads(amp.BOARD_PATH.read_text())
    got = {lane: len(recs) for lane, recs in b["tasks"].items()}
    assert got == {"l0": 40, "l1": 40, "l2": 40}, (
        "an append is missing, and nothing raised to say so")


def test_record_task_from_two_processes_all_land(amp, tmp_path):
    """The same claim about the real function, with no sleep in it.

    Weaker - it may pass on a broken lock - and here because the test above
    proves the lock and this one proves the caller uses it.
    """
    home = tmp_path / "state"
    home.mkdir(parents=True, exist_ok=True)
    body = """
lane, n = sys.argv[1], int(sys.argv[2])
for i in range(n):
    amp.record_task(lane, {"task_id": f"{lane}-{i}"})
"""
    kids = [_child(home, body, f"r{i}", "60") for i in range(2)]
    for k in kids:
        out, err = k.communicate(timeout=180)
        assert k.returncode == 0, err
    b = json.loads(amp.BOARD_PATH.read_text())
    assert {k: len(v) for k, v in b["tasks"].items()} == {"r0": 60, "r1": 60}


# --------------------------------------------------------------- the mechanism


def test_a_second_process_is_actually_excluded(amp, tmp_path):
    """The lock is a fact about the filesystem, not about this interpreter."""
    home = tmp_path / "state"
    home.mkdir(parents=True, exist_ok=True)
    body = """
import fcntl, os
lp = amp.BOARD_PATH.with_suffix(amp.BOARD_PATH.suffix + ".lock")
fd = os.open(lp, os.O_CREAT | os.O_RDWR, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print("took")
except OSError:
    print("blocked")
"""
    with amp._file_lock(amp.BOARD_PATH):
        k = _child(home, body)
        out, err = k.communicate(timeout=60)
        assert out.strip() == "blocked", err
    # And released on the way out, or the lock is a one-shot.
    k = _child(home, body)
    out, err = k.communicate(timeout=60)
    assert out.strip() == "took", err


def test_it_is_reentrant_in_one_thread(amp):
    """Nesting must not deadlock on itself.

    `flock` is per open file description, so a second descriptor blocks even
    inside one process. Run on a thread with a deadline, because the failure
    this guards against is a hang, and a hanging test tells you nothing.
    """
    done = threading.Event()

    def go():
        with amp._file_lock(amp.BOARD_PATH):
            with amp._file_lock(amp.BOARD_PATH):
                pass
        done.set()

    t = threading.Thread(target=go, daemon=True)
    t.start()
    assert done.wait(10), "a nested lock on one document deadlocked"


def test_the_inner_frame_does_not_release_the_outer_one(amp, tmp_path):
    """Depth counting, checked from outside rather than by reading it."""
    home = tmp_path / "state"
    home.mkdir(parents=True, exist_ok=True)
    body = """
import fcntl, os
lp = amp.BOARD_PATH.with_suffix(amp.BOARD_PATH.suffix + ".lock")
fd = os.open(lp, os.O_CREAT | os.O_RDWR, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print("took")
except OSError:
    print("blocked")
"""
    with amp._file_lock(amp.BOARD_PATH):
        with amp._file_lock(amp.BOARD_PATH):
            pass
        k = _child(home, body)
        out, _ = k.communicate(timeout=60)
        assert out.strip() == "blocked", "the inner exit dropped the outer lock"


def test_waiting_forever_is_not_an_option(amp, tmp_path):
    """A held lock ends in a named refusal, not a hung harness."""
    home = tmp_path / "state"
    home.mkdir(parents=True, exist_ok=True)
    holder = _child(home, """
import time
with amp._file_lock(amp.BOARD_PATH):
    print("held", flush=True)
    time.sleep(30)
""")
    assert holder.stdout.readline().strip() == "held"
    try:
        t0 = time.monotonic()
        with pytest.raises(amp.Busy) as e:
            with amp._file_lock(amp.BOARD_PATH, timeout=1.0):
                pass
        assert 0.9 <= time.monotonic() - t0 < 10
        assert ".board.json" in str(e.value), "the refusal does not name the file"
    finally:
        holder.kill()
        holder.wait()


def test_the_lock_follows_a_workspace_switch(amp, tmp_path):
    """The locks hold a NAME, not a Path.

    `_bind_state` re-points every state path when the workspace changes. A lock
    that captured the Path at import would go on excluding writers to the
    workspace you left, which looks like it is working.
    """
    before = amp._BOARD_LOCK.path
    amp._bind_state(tmp_path / "other")
    try:
        assert amp._BOARD_LOCK.path == tmp_path / "other" / ".board.json"
        assert amp._BOARD_LOCK.path != before
    finally:
        amp._bind_state(amp.STATE_ROOT)
    assert amp._BOARD_LOCK.path == before


def test_lock_files_are_not_mirrored(amp):
    """A `.lock` in the mirror would be a revision history of nothing, and
    `store.verify` would report the state directory permanently dirty."""
    with amp._file_lock(amp.BOARD_PATH):
        pass
    lp = amp.BOARD_PATH.with_suffix(amp.BOARD_PATH.suffix + ".lock")
    assert lp.exists(), "the lock file is not where the skip rule expects it"
    rel = str(lp.relative_to(amp.STATE_ROOT).as_posix())
    assert amp.store.skipped(rel) == "temporary file"


# ------------------------------------------------------------- the temp file
#
# Found by reverting the lock rather than by reading the code. With the lock
# gone the count assertion never got a chance to run: the third process died on
# `FileNotFoundError` renaming a temp file a sibling had already renamed. One
# temp name per document is safe for exactly as long as there is one writer,
# and the crash is the LUCKY ordering - the quiet one is two writers
# interleaving into that file and renaming a mixture into place.


_WHOLE = """
tag, n = sys.argv[1], int(sys.argv[2])
p = amp.STATE_ROOT / "unlocked.txt"
for i in range(n):
    amp.save_text(p, tag * 4000)
"""


def test_a_document_is_never_a_mixture_of_two_writers(amp, tmp_path):
    """No lock at all, and the file still holds one whole write.

    This is the property `save_text` is supposed to have on its own - `replace`
    is atomic against a reader - and a shared temp path took it away without
    touching the rename. Deliberately NOT using the board lock: the point is
    that the write path is safe for the documents that have no lock, and
    `config.json` is twelve writers with no lock.
    """
    home = tmp_path / "state"
    home.mkdir(parents=True, exist_ok=True)
    kids = [_child(home, _WHOLE, c, "60") for c in "abc"]
    for k in kids:
        out, err = k.communicate(timeout=180)
        assert k.returncode == 0, err

    got = (home / "unlocked.txt").read_text()
    assert got in {c * 4000 for c in "abc"}, (
        "the document is a mixture of two writers, and it parsed")


def test_two_writers_do_not_share_a_temp_name(amp, monkeypatch):
    """The mechanism, named where a future edit will trip over it.

    Read off the descriptor that gets fsynced rather than by re-deriving the
    name, because re-deriving it here would just be this test agreeing with a
    copy of the line it is checking.
    """
    seen = []
    real = os.fsync

    def spy(fd):
        seen.append(os.readlink(f"/proc/self/fd/{fd}"))
        return real(fd)

    monkeypatch.setattr(os, "fsync", spy)
    amp.save_text(amp.STATE_ROOT / "t.txt", "x")
    assert seen and seen[0].endswith(".tmp")
    assert str(os.getpid()) in seen[0], "the temp name is not per-writer"


def test_a_failed_write_leaves_no_litter(amp, monkeypatch):
    """A unique name is litter where a shared one overwrote itself."""
    p = amp.STATE_ROOT / "boom.txt"
    p.parent.mkdir(parents=True, exist_ok=True)

    def boom(fd):
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        amp.save_text(p, "x")
    assert not list(p.parent.glob("boom.txt*.tmp")), "a dead write left a temp file"
    assert not p.exists(), "a dead write published a partial document"


# ------------------------------------------------ reading after the slow part


def test_a_poll_does_not_write_back_the_board_it_started_with(amp):
    """`record_remote` re-reads.

    A poll reads the board, waits on the network, and writes the whole board
    back. Everything recorded while it waited is missing from that copy. This
    is that sequence with the network replaced by the thing that happened
    during it.
    """
    amp.board()                                   # the stale read a poll used to do
    amp.record_task("lane-a", {"task_id": "finished-while-polling"})
    amp.record_remote({"lane-a": [{"id": "remote-1"}]})

    b = json.loads(amp.BOARD_PATH.read_text())
    assert [r["task_id"] for r in b["tasks"]["lane-a"]] == ["finished-while-polling"], (
        "the poll wrote back a board from before the worker finished")
    assert b["remote"]["lane-a"] == [{"id": "remote-1"}]
    assert b["polled_at"]


def test_record_remote_touches_only_the_lanes_polled(amp):
    amp.record_remote({"a": [1]})
    amp.record_remote({"b": [2]})
    b = json.loads(amp.BOARD_PATH.read_text())
    assert b["remote"] == {"a": [1], "b": [2]}


def _source(fn) -> str:
    import inspect
    return inspect.getsource(fn)


def test_no_poll_reads_the_board_before_the_network(amp):
    """The half of this that is not reachable by calling anything.

    Both pollers used to hold `b = board()` across the codex calls. The fix is
    the absence of that read, and the only way to check an absence is to look.
    """
    sys.modules.pop("server", None)
    import server
    for fn in (amp.cmd_poll, server.do_poll):
        src = _source(fn)
        assert "board()" not in src, (
            f"{fn.__name__} reads the board before the network again")
        assert "record_remote" in src


def test_that_check_can_fail(amp):
    """A grep that cannot fail is not a check."""
    def bad_poll():
        b = amp.board()
        return b

    assert "board()" in _source(bad_poll)
