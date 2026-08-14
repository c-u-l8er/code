"""One console per state directory (F1).

`main` assumes it is the only console on its `.amp`: it marks every `running`
orchestrator turn failed, and `adopt_orphans` reconciles the shared board
against in-process dicts a second console starts empty. So the second console
does not merely race the first - it writes "the worker did not survive" about a
live worker, and adopts that worker's process group.

`free_port` is what made that silent. It walks to the next free port rather
than failing on the one asked for, so a second console STARTS, cleanly, on a
port nobody expected, and the way an operator finds out is by reading two
boards that disagree.

What is checked here is the shape of the refusal, not just its existence: a
refusal that does not name the live pid and port is a refusal the operator
cannot act on, and the whole reason to fail at startup instead of walking to
the next port is to send them somewhere.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE))


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    """`server` on a throwaway state directory.

    `AMP_HOME` before the import, always: `amp` binds every state path at
    import time, and `server` reads `amp.STATE_ROOT` for the lock.
    """
    monkeypatch.setenv("AMP_HOME", str(tmp_path / "state"))
    for m in ("amp", "store", "blueprint", "preview", "server"):
        sys.modules.pop(m, None)
    import server as mod
    assert mod.amp.STATE_ROOT == tmp_path / "state", \
        "the lock is being taken on the real .amp"
    yield mod
    for m in ("amp", "store", "blueprint", "preview", "server"):
        sys.modules.pop(m, None)


def _lock(srv) -> Path:
    return srv.amp.STATE_ROOT / srv.CONSOLE_LOCK


def _a_live_console() -> subprocess.Popen:
    """A process that is alive AND passes the `/proc/<pid>/cmdline` check.

    A bare `sleep` would not: `_lock_holder` reads the command line and treats
    a pid that is not running `server.py` as a recycled one, which is the
    behaviour that stops a refusal an operator cannot act on. So the argv has
    to carry the name, and this is the honest way to get a pid that does.
    """
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                          "server.py"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    return p


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", ""])
    p.wait()
    return p.pid


# ---------------------------------------------------------------- taking it


def test_the_lock_names_who_holds_it(srv):
    path = srv.take_console_lock(9021, "127.0.0.1")
    assert path == _lock(srv)
    rec = json.loads(path.read_text())
    assert rec["pid"] == os.getpid()
    assert rec["port"] == 9021 and rec["host"] == "127.0.0.1"
    assert rec["at"], "a lock with no start time cannot be reported to anyone"


def test_the_state_root_is_created_if_it_is_not_there(srv):
    """The lock is taken before anything else writes, so on a first run the
    directory does not exist yet."""
    assert not srv.amp.STATE_ROOT.exists()
    srv.take_console_lock(9021, "127.0.0.1")
    assert _lock(srv).is_file()


# --------------------------------------------------------- the second console


def test_a_second_console_on_one_state_root_refuses_and_says_where_to_go(srv):
    live = _a_live_console()
    try:
        _lock(srv).parent.mkdir(parents=True, exist_ok=True)
        _lock(srv).write_text(json.dumps({
            "pid": live.pid, "port": 8765, "host": "127.0.0.1",
            "at": "2026-07-31T00:00:00+00:00"}))
        with pytest.raises(SystemExit) as e:
            srv.take_console_lock(8766, "127.0.0.1")
        msg = str(e.value)
        # The three facts an operator needs to act on it. Without the port
        # there is nowhere to go, and going to the next free port is exactly
        # what made this silent in the first place.
        assert str(live.pid) in msg, "the refusal does not name the live console"
        assert "8765" in msg, "the refusal does not name the port to use"
        assert str(srv.amp.STATE_ROOT) in msg, \
            "the refusal does not name the directory being shared"
        assert "8766" not in msg, \
            "the refusal offers the port THIS console wanted, which is not running"
    finally:
        live.kill()
        live.wait()


def test_the_lock_is_left_alone_when_it_is_refused(srv):
    """A refusal must not clear the lock on the way out - that would let the
    third console in."""
    live = _a_live_console()
    try:
        _lock(srv).parent.mkdir(parents=True, exist_ok=True)
        before = json.dumps({"pid": live.pid, "port": 8765})
        _lock(srv).write_text(before)
        with pytest.raises(SystemExit):
            srv.take_console_lock(8766, "127.0.0.1")
        assert _lock(srv).read_text() == before
    finally:
        live.kill()
        live.wait()


def test_two_consoles_on_different_state_roots_both_run(srv, tmp_path,
                                                        monkeypatch):
    """Two `AMP_HOME`s are two harnesses. This pair is a normal setup here -
    one of them serving a preview - so a lock keyed on the machine, or on the
    port, would refuse the case it is supposed to allow."""
    first = srv.take_console_lock(8765, "127.0.0.1")
    other = tmp_path / "other-state"
    monkeypatch.setattr(srv.amp, "STATE_ROOT", other)
    second = srv.take_console_lock(8766, "127.0.0.1")
    assert first.is_file() and second.is_file()
    assert first != second
    assert json.loads(second.read_text())["port"] == 8766


# ------------------------------------------------------------- stale locks


def test_a_lock_whose_process_is_gone_is_cleared(srv, capsys):
    """A lock file outlives `kill -9`. Honouring it would mean recovering from
    a crash by deleting a dotfile nobody documented."""
    _lock(srv).parent.mkdir(parents=True, exist_ok=True)
    _lock(srv).write_text(json.dumps({"pid": _dead_pid(), "port": 8765}))
    srv.take_console_lock(8766, "127.0.0.1")
    assert json.loads(_lock(srv).read_text())["pid"] == os.getpid()
    assert "stale" in capsys.readouterr().out, \
        "a lock was cleared and nobody was told"


def test_an_unreadable_lock_names_nobody(srv):
    """There is no port to send the operator to and no pid to check, so it
    cannot be honoured."""
    _lock(srv).parent.mkdir(parents=True, exist_ok=True)
    _lock(srv).write_text("{ this is not json")
    srv.take_console_lock(8766, "127.0.0.1")
    assert json.loads(_lock(srv).read_text())["pid"] == os.getpid()


def test_a_recycled_pid_is_not_a_console(srv):
    """A pid is reused. If some unrelated program has this one, refusing to
    start for it is a refusal the operator cannot act on - there is no console
    at that port to go to."""
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _lock(srv).parent.mkdir(parents=True, exist_ok=True)
        _lock(srv).write_text(json.dumps({"pid": other.pid, "port": 8765}))
        srv.take_console_lock(8766, "127.0.0.1")
        assert json.loads(_lock(srv).read_text())["pid"] == os.getpid()
    finally:
        other.kill()
        other.wait()


def test_our_own_pid_is_not_a_second_console(srv):
    """A restart in one process - a test, a reload - is not two consoles."""
    srv.take_console_lock(8765, "127.0.0.1")
    srv.take_console_lock(8766, "127.0.0.1")
    assert json.loads(_lock(srv).read_text())["port"] == 8766


# ---------------------------------------------------------- giving it back


def test_releasing_removes_our_own_lock(srv):
    path = srv.take_console_lock(8765, "127.0.0.1")
    srv.release_console_lock(path)
    assert not path.exists()


def test_releasing_does_not_remove_somebody_elses(srv):
    """This process may hold a lock it took over as stale, which a third
    console has since taken. Deleting that one hands the directory to whoever
    starts next, which is the race this whole thing closes."""
    path = srv.take_console_lock(8765, "127.0.0.1")
    path.write_text(json.dumps({"pid": os.getpid() + 100000, "port": 8767}))
    srv.release_console_lock(path)
    assert path.exists(), "a lock belonging to another console was deleted"
    assert json.loads(path.read_text())["port"] == 8767


def test_releasing_nothing_is_not_an_error(srv):
    srv.release_console_lock(None)


def test_releasing_a_lock_that_is_already_gone_is_not_an_error(srv):
    """The state directory can be wiped underneath a console. Raising on the
    way out would turn a clean shutdown into a traceback."""
    path = srv.take_console_lock(8765, "127.0.0.1")
    path.unlink()
    srv.release_console_lock(path)


# ------------------------------------------------------- and it really binds


def test_two_real_consoles_race_and_only_one_starts(tmp_path):
    """The end-to-end version: two processes calling `take_console_lock` on one
    state root at the same moment. `O_EXCL` is what settles it, and this is the
    only test here that exercises the exclusive create rather than the pid
    check that runs after it.
    """
    src = (f"import sys, os, time\nsys.path.insert(0, {str(CODE)!r})\n"
           "import server\n"
           "try:\n"
           "    server.take_console_lock(int(sys.argv[1]), '127.0.0.1')\n"
           "    print('TOOK')\n"
           "    time.sleep(3)\n"
           "except SystemExit as e:\n"
           "    print('REFUSED')\n")
    home = tmp_path / "shared"
    env = dict(os.environ, AMP_HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
    # The trailing `server.py` is load-bearing and not decoration: the holder
    # check reads `/proc/<pid>/cmdline` and treats a pid that is not running
    # `server.py` as recycled. A child launched with `-c` has no such argv, so
    # without this the second one clears the first one's live lock and the
    # test passes for the wrong reason.
    a = subprocess.Popen([sys.executable, "-c", src, "8765", "server.py"],
                         env=env, stdout=subprocess.PIPE, text=True)
    # Long enough that the first has the lock, short enough that it is still
    # holding it. The race for O_EXCL is covered by the assertion below being
    # about the PAIR, not about which one won.
    time.sleep(1.0)
    b = subprocess.Popen([sys.executable, "-c", src, "8766", "server.py"],
                         env=env, stdout=subprocess.PIPE, text=True)
    out_b = b.communicate(timeout=30)[0]
    out_a = a.communicate(timeout=30)[0]
    assert "TOOK" in out_a and "REFUSED" in out_b, \
        f"both consoles started on one state root\nfirst: {out_a}\nsecond: {out_b}"
