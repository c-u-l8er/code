#!/usr/bin/env python3
"""Look at what a lane actually built.

A preview is a CHILD of this console, not a page inside it. It binds its own
port on 127.0.0.1 and the browser frames that origin directly, because:

  - a page's own absolute paths (/assets/app.js, /favicon.ico) only resolve if
    it owns the root of an origin, so serving it under /preview/<lane>/ would
    quietly break every site that uses them, and
  - a dev server's live-reload socket connects back to the origin it was served
    from, which a path-prefix proxy is not.

Two shapes, and the difference is who serves:

  static   this file serves the directory - no node, no install, no build
  command  the project serves itself (`npm run dev`) and we only watch it

A command's port is DISCOVERED, not assigned: dev servers disagree about
whether they honour $PORT (vite does not), but they all print the URL they
ended up on. So we read it back out of their output rather than telling them
where to listen and hoping.

Nothing here starts on its own. A preview is started by a click, and dies with
Stop or with the console.
"""

from __future__ import annotations

import fcntl
import functools
import json
import os
import pty
import re
import signal
import socket
import struct
import subprocess
import termios
import threading
import time
from collections import deque
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT_BASE = 8800

# Directories that are never the site and are expensive to walk.
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".amp", "_build",
    "deps", ".next", ".svelte-kit", ".turbo", "target", ".cache", ".pytest_cache",
}

# Where a static site's front door tends to be, in the order a person would look.
INDEX_DIRS = ("", "public", "site", "dist", "build", "www", "docs")

# The first localhost URL a dev server prints is the one it is serving on.
_URL_RE = re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?::(\d+))?(?:/\S*)?")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# How long a command gets to say where it is listening before we stop waiting
# for it. It keeps running; we just stop claiming it is about to be ready.
START_TIMEOUT = 60.0

_PREVIEWS: dict[str, "Preview"] = {}
_LOCK = threading.RLock()


def free_port(start: int = PORT_BASE) -> int:
    for p in range(start, start + 200):
        with socket.socket() as s:
            if s.connect_ex((HOST, p)) != 0:
                return p
    raise RuntimeError("no free port for a preview")


# ---------------------------------------------------------------- detection


def detect(root: Path) -> dict:
    """What this tree looks like it wants to be served as.

    A suggestion, not a decision - it prefills the controls and the operator
    can overrule it. Serving itself beats being served: if a project ships a
    dev server, that is the thing whose output is worth looking at.
    """
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            scripts = json.loads(pkg.read_text()).get("scripts") or {}
        except (json.JSONDecodeError, OSError, AttributeError):
            scripts = {}
        for name in ("dev", "start", "serve", "preview"):
            if name in scripts:
                return {
                    "mode": "command",
                    "cmd": f"npm run {name}",
                    "dir": "",
                    "why": f"package.json has a {name!r} script",
                    "warn": (None if (root / "node_modules").is_dir()
                             else "node_modules is missing - run npm install first"),
                }
    for d in INDEX_DIRS:
        if (root / d / "index.html").is_file():
            return {"mode": "static", "cmd": "", "dir": d,
                    "why": (d + "/" if d else "") + "index.html", "warn": None}
    return {"mode": "static", "cmd": "", "dir": "",
            "why": "no index.html found - you get the file listing", "warn": None}


def stamp(root: Path, limit: int = 4000) -> str:
    """A cheap fingerprint of the tree, for noticing that a worker changed it.

    Newest mtime alone misses a deletion, and a file count alone misses an
    edit, so it is both. Bounded, because this runs on a poll.
    """
    newest = 0.0
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for f in filenames:
            try:
                m = os.stat(os.path.join(dirpath, f)).st_mtime
            except OSError:
                continue
            newest = max(newest, m)
            n += 1
            if n >= limit:
                return f"{n}:{newest:.0f}"
    return f"{n}:{newest:.0f}"


# ---------------------------------------------------------------- static


class _StaticHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler, minus the caching, plus a log we can read."""

    def __init__(self, *a, preview: "Preview" = None, **kw):
        self.preview = preview       # before super(): it serves the request
        super().__init__(*a, **kw)

    def send_head(self):
        # The same Host allow-list the console runs, for the same reason and by
        # the same argument: under DNS rebinding a page served from the
        # attacker's name is same-origin with this port, and what it reads is
        # whatever a lane just built - a worktree of the operator's source. It
        # runs in `send_head` because that is the one place GET and HEAD both
        # pass through. Returning None is how this handler declines: the caller
        # writes no body.
        #
        # Only the `static` mode is ours to guard. A `command` preview is a
        # foreign dev server on its own socket; this process does not see its
        # requests and cannot speak for it.
        allowed = getattr(self.preview, "hosts", None)
        host = (self.headers.get("Host") or "").strip().lower()
        if allowed and host not in allowed:
            self.send_error(403, "wrong Host")
            return None
        # A 304 is the correct answer and the wrong behaviour here: you clicked
        # reload to see the edit a worker just made, not to be told nothing has
        # changed since you first opened it.
        if "If-Modified-Since" in self.headers:
            del self.headers["If-Modified-Since"]
        if "If-None-Match" in self.headers:
            del self.headers["If-None-Match"]
        return super().send_head()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        if self.preview is not None:
            self.preview.say(f"{self.address_string()} {fmt % args}")

    def log_error(self, fmt, *args):
        self.log_message(fmt, *args)


# ---------------------------------------------------------------- the preview


class Preview:
    def __init__(self, lane: str, root: Path, mode: str, cmd: str = ""):
        self.lane = lane
        self.root = root
        self.mode = mode
        self.cmd = cmd
        self.log: deque[str] = deque(maxlen=500)
        self.state = "starting"      # starting | running | failed | stopped
        self.url: str | None = None
        # Empty until a static preview binds a port; a command preview never
        # fills it, and an empty set is the handler's "not mine to judge".
        self.hosts: set[str] = set()
        self.error: str | None = None
        self.started_at = time.time()
        self.proc: subprocess.Popen | None = None
        self.httpd: ThreadingHTTPServer | None = None
        self.fd: int | None = None   # the pty a command's output is read from

    def say(self, line: str):
        self.log.append(line)

    # -- start

    def start(self):
        if self.mode == "static":
            self._start_static()
        else:
            self._start_command()

    def _start_static(self):
        port = free_port()
        # Set before the socket opens, so there is no window in which the
        # handler has no allow-list to consult.
        self.hosts = {f"{h}:{port}" for h in ("127.0.0.1", "localhost", "[::1]")}
        handler = functools.partial(_StaticHandler, directory=str(self.root), preview=self)
        self.httpd = ThreadingHTTPServer((HOST, port), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://{HOST}:{port}/"
        self.state = "running"
        self.say(f"serving {self.root} at {self.url}")

    def _start_command(self):
        # Its own session, so stopping it can kill the GROUP. A dev server is
        # exactly the thing that spawns children (esbuild, a watcher, a worker
        # pool) which outlive the shell that started them - the same leak
        # run_check was rewritten for.
        env = {**os.environ,
               "PORT": str(free_port()),   # a hint; some honour it, vite does not
               "BROWSER": "none",          # do not open a window on the operator
               "NO_COLOR": "1", "FORCE_COLOR": "0"}
        # A pty, not a pipe. Behind a pipe a runtime switches stdout to block
        # buffering, and a dev server that has not flushed the line naming its
        # own URL is a preview that never arrives. Measured: `python3 -m
        # http.server 0` sat in `starting` indefinitely on a pipe, having
        # printed its port to a buffer nobody would see until it exited.
        self.fd, slave = pty.openpty()
        # Wide, so a startup banner cannot wrap the URL in half and hide it.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
        try:
            self.proc = subprocess.Popen(
                self.cmd, shell=True, cwd=str(self.root), env=env,
                start_new_session=True, stdin=subprocess.DEVNULL,
                stdout=slave, stderr=slave, close_fds=True)
        except OSError as e:
            os.close(self.fd)
            self.fd = None
            self.state, self.error = "failed", str(e)
            return
        finally:
            os.close(slave)   # ours is the read end; the child holds the other
        self.say(f"$ {self.cmd}")
        threading.Thread(target=self._read_output, daemon=True).start()

    def _line(self, raw: str):
        line = _ANSI_RE.sub("", raw).replace("\r", "").rstrip()
        self.say(line)
        if self.url is not None:
            return
        m = _URL_RE.search(line)
        if m:
            self.url = f"http://{HOST}:{m.group(1) or '80'}/"
            self.state = "running"
            self.say(f"-> framing {self.url}")

    def _read_output(self):
        buf = ""
        while True:
            try:
                chunk = os.read(self.fd, 4096)
            except OSError:          # the child exited and the pty went away
                break
            if not chunk:
                break
            buf += chunk.decode("utf-8", "replace")
            *lines, buf = buf.split("\n")
            for line in lines:
                self._line(line)
        if buf.strip():
            self._line(buf)
        try:
            os.close(self.fd)
        except OSError:
            pass
        self.fd = None
        rc = self.proc.wait()
        if self.state != "stopped":
            self.state = "failed" if rc else "stopped"
            self.error = f"exited {rc}" if rc else None
            self.say(f"(exited {rc})")

    # -- stop

    def stop(self):
        self.state = "stopped"
        if self.httpd is not None:
            # shutdown() blocks until serve_forever returns, and serve_forever
            # is what we would be calling it from if a request were in flight.
            threading.Thread(target=self._close_httpd, daemon=True).start()
        if self.proc is not None and self.proc.poll() is None:
            for sig, grace in ((signal.SIGTERM, 5), (signal.SIGKILL, 3)):
                try:
                    os.killpg(self.proc.pid, sig)
                except OSError:
                    break
                try:
                    self.proc.wait(timeout=grace)
                    break
                except subprocess.TimeoutExpired:
                    continue

    def _close_httpd(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass

    # -- report

    def view(self) -> dict:
        waited = time.time() - self.started_at
        if self.state == "starting" and waited > START_TIMEOUT:
            self.state = "failed"
            self.error = (f"{self.cmd!r} has not printed a localhost URL in "
                          f"{START_TIMEOUT:.0f}s - it is still running, but "
                          f"there is nothing to frame yet")
        return {
            "lane": self.lane, "root": str(self.root), "mode": self.mode,
            "cmd": self.cmd, "state": self.state, "url": self.url,
            "error": self.error, "uptime_s": round(waited, 1),
            "log": list(self.log)[-200:],
        }


# ---------------------------------------------------------------- api


def get(lane: str) -> Preview | None:
    with _LOCK:
        return _PREVIEWS.get(lane)


def start(lane: str, root: Path, mode: str, cmd: str = "") -> dict:
    if mode not in ("static", "command"):
        return {"ok": False, "error": f"unknown preview mode {mode!r}"}
    if not root.is_dir():
        return {"ok": False, "error": f"{root} is not a directory"}
    if mode == "command" and not cmd.strip():
        return {"ok": False, "error": "no command to run"}
    with _LOCK:
        old = _PREVIEWS.pop(lane, None)
    if old is not None:
        old.stop()            # one per lane: a second would take a second port
    p = Preview(lane, root, mode, cmd.strip())
    p.start()
    with _LOCK:
        _PREVIEWS[lane] = p
    return {"ok": True, "preview": p.view()}


def stop(lane: str) -> dict:
    with _LOCK:
        p = _PREVIEWS.pop(lane, None)
    if p is None:
        return {"ok": True, "stopped": False}
    p.stop()
    return {"ok": True, "stopped": True}


def stop_all():
    with _LOCK:
        previews = list(_PREVIEWS.values())
        _PREVIEWS.clear()
    for p in previews:
        p.stop()
