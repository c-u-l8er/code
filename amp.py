#!/usr/bin/env python3
"""amp - lead-manager harness for the [&] stack.

One chat window (Claude) dispatches coding work to workers across many repos,
tracks them on a board, and escalates to GPT-5.6 when a lane stalls.

Layers:
  lanes    a named (repo, path, backend) triple - one per sub-repo
  workers  `claude -p` runs in an isolated worktree (default), or
           `codex cloud exec` tasks billed to the ChatGPT subscription
  consult  GPT-5.6 via OpenRouter, reached with a packet built from a lane

Stdlib only. No secrets are stored in tracked files.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent                   # the program (this repo)
ROOT = Path(os.environ.get("AMP_ROOT") or HERE.parent)   # workspace holding the lanes

# State lives OUTSIDE this repo so the program stays shareable and no key,
# board, packet, or ruling is ever committed. Override with AMP_HOME.
STATE_ROOT = Path(os.environ.get("AMP_HOME") or (ROOT / ".amp"))

# Two things live at the root and are deliberately NOT per-workspace:
#
#   the secrets, because an OpenRouter key and a Claude worker token belong to
#   the machine and the person, not to whichever set of lanes is in front of
#   you. Copying them per workspace would mean signing in again per workspace
#   and having several copies of one credential to leak.
#
#   the workspace registry itself, for the obvious reason.
SECRETS_PATH = STATE_ROOT / ".secrets.json"
WORKSPACES_PATH = STATE_ROOT / ".workspaces.json"

# Everything else is workspace state, and this table is the only place that
# says so. `_bind_state` builds every one of these names from a base directory,
# so adding a file to the harness means adding a line here rather than
# remembering to teach the workspace switch about it later. The names are
# module globals because that is what four thousand lines of this file already
# read; what changed is that they are now bound rather than computed once.
_STATE_LAYOUT = (
    ("CONFIG_PATH", "config.json"),
    ("BOARD_PATH", ".board.json"),
    ("CHAT_PATH", ".chat.json"),
    ("ORCH_PATH", ".orchestrator.json"),
    ("QUEUE_PATH", ".queue.json"),
    ("DOCTRINE_PIN_PATH", ".doctrine.json"),
    ("FINDINGS_PATH", ".findings.json"),
    ("OBLIGATIONS_PATH", ".obligations.json"),
    ("DIRECTION_PATH", ".direction.json"),
    ("SUPERVISOR_PATH", ".supervisor.json"),
    ("PACKET_DIR", "packets"),
    ("RULING_DIR", "rulings"),
    ("WORKTREE_DIR", "worktrees"),
    ("CONSULT_DIR", "consults"),
    ("GOAL_DIR", "goals"),
)


def _bind_state(base: Path) -> None:
    """Point every workspace-scoped path at `base`."""
    g = globals()
    g["STATE"] = base
    for name, rel in _STATE_LAYOUT:
        g[name] = base / rel


_bind_state(STATE_ROOT)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# GPT-5.6 family on OpenRouter, cheapest first. There is no gpt-5.6-codex.
CONSULT_MODELS = {
    "luna": "openai/gpt-5.6-luna-pro",    # $1 / $6  per M
    "terra": "openai/gpt-5.6-terra-pro",  # $2.5 / $15 per M
    "sol": "openai/gpt-5.6-sol-pro",      # $5 / $30 per M
}
DEFAULT_CONSULT = "terra"

# Who answers as the architect. `codex` runs `codex exec` against your ChatGPT
# subscription and costs no metered money; `openrouter` is the metered API path.
ARCHITECT_BACKENDS = ("codex", "openrouter", "claude")
DEFAULT_ARCHITECT = "codex"

# codex cloud list --limit is capped at 20 by the CLI.
LIST_LIMIT = 20

# Worker backends. claude runs locally and can be resumed for follow-up turns;
# codex runs in the cloud, one-shot, with no reply channel.
BACKENDS = ("claude", "codex")
DEFAULT_BACKEND = "claude"
DEFAULT_CLAUDE_MODEL = "opus"
DEFAULT_BUDGET_USD = 1.00

# ---------------------------------------------------------------------- roles
#
# Three models do three different jobs here. A WORKER writes the code. An
# ARCHITECT answers when a worker escalates. A SUPERVISOR holds the whole
# workspace against its mission. Until now only the architect was nameable in
# the console, so the other two were lit by proxy or not lit at all.
#
# They are three settings and not one because they are three decisions. The
# worker runs constantly and is the expensive one; the architect runs on
# escalation; the supervisor runs once, when asked. Wiring them together means a
# change made for one reason silently moves the other two.
#
# Each can also be switched off, and OFF MEANS REFUSED, not hidden. A dark light
# because you turned the role off and a dark light because the CLI is signed out
# are different facts, and the header has to say which one it is looking at -
# the whole point of a light is that you can tell what it means without asking.

ROLES = ("worker", "architect", "supervisor")

# Every model this harness can reach, and which family it comes from. The family
# is what decides how it is called, so it is recorded once here rather than
# re-derived by each caller from the spelling of a name.
MODELS = {
    "opus":   {"kind": "claude", "note": "Claude, on your Claude subscription"},
    "sonnet": {"kind": "claude", "note": "Claude, cheaper and faster"},
    "haiku":  {"kind": "claude", "note": "Claude, cheapest and fastest"},
    "codex":  {"kind": "codex",  "note": "GPT, on your ChatGPT subscription - no metered spend"},
    "luna":   {"kind": "openrouter", "note": "GPT-5.6 luna - $1 / $6 per M"},
    "terra":  {"kind": "openrouter", "note": "GPT-5.6 terra - $2.5 / $15 per M"},
    "sol":    {"kind": "openrouter", "note": "GPT-5.6 sol - $5 / $30 per M"},
}

# The supervisor may also be set to this, meaning: whatever the architect is.
FOLLOW_ARCHITECT = "architect"

# Which families can hold down which job, and why not when they cannot.
#
# Every role's dropdown lists EVERY model, always. Offering each role a
# different short list was the wrong call: a list that silently differs per role
# reads as a bug, and it leaves the console nowhere to say why the option you
# wanted is not there. So the ones that cannot do the job are shown, disabled,
# each carrying its reason.
ROLE_CANNOT = {
    "worker": {
        "openrouter": "no worker runs on the OpenRouter API - a worker edits "
                      "files and runs commands, and that path is chat only",
        "codex": "a codex worker is a property of the lane, not a global "
                 "setting - set the backend on the lane itself",
    },
    "architect": {},
    "supervisor": {},
}


def model_kind(name: str) -> str:
    """Which family a model belongs to, and so how it gets called."""
    return (MODELS.get(name) or {}).get("kind") or "openrouter"

# A worker can spawn a build, a benchmark, or a loop that never ends. These are
# the ceilings it runs under; override any of them in config.json.
DEFAULT_WORKER_TIMEOUT_S = 30 * 60
DEFAULT_WORKER_MEM_GB = 8.0
DEFAULT_WORKER_NICE = 10

# A wall clock cannot tell a worker that is hung from one that is halfway
# through a twenty-minute build - both just sit there. Two readings together
# can: the transcript it writes as it goes has stopped growing, AND its process
# group is burning no CPU. A build is quiet but hot; a wedged worker is quiet
# and cold. Only the second gets stopped.
DEFAULT_WORKER_STALL_S = 15 * 60
IDLE_CPU_PCT = 2.0

# One worker per lane is already enforced, but nine lanes is still nine Opus
# sessions at once - enough to burn a subscription's window in minutes and to
# put nine builds on one laptop. This is the ceiling across the whole board.
DEFAULT_MAX_WORKERS = 3

# A headless worker has nobody to ask. "acceptEdits" waves through file writes but
# still stops on every bash command, so a worker that needs to run a build spends
# its whole budget being asked a question no one can answer. What actually keeps
# this safe is the worktree: a worker only ever sees a clean, throwaway checkout
# on its own branch, and the timeout, memory ceiling and nice level still bind.
DEFAULT_PERMISSION_MODE = "bypassPermissions"

# The orchestrator is not a lane. It runs in the shared checkout, not a
# worktree, because everything it gets asked is about the shared checkout: what
# is running, what is uncommitted, what should go out next. A worktree-isolated
# session is structurally unable to answer any of that. It is one long session,
# resumed every turn, so the window remembers what you already told it.
DEFAULT_ORCH_MODEL = "opus"
DEFAULT_ORCH_BUDGET_USD = 3.00

TERMINAL_STATES = {"completed", "succeeded", "failed", "cancelled", "canceled", "error"}


# ---------------------------------------------------------------- infrastructure


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def die(msg: str, code: int = 1):
    print(f"amp: {msg}", file=sys.stderr)
    raise SystemExit(code)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        die(f"{path.name} is not valid JSON: {e}")


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def config() -> dict:
    return load_json(CONFIG_PATH, {"lanes": {}, "consult_model": DEFAULT_CONSULT})


def board() -> dict:
    return load_json(BOARD_PATH, {"tasks": {}, "polled_at": None})


# ---------------------------------------------------------------- workspaces
#
# A workspace is a whole separate run of this harness against the same disk: its
# own lanes, board, goals, consults, findings, obligations, direction and
# worktrees. Building the core stack and building demos on top of the core stack
# are two different jobs with two different sets of open work, and mixing them
# on one board means the cap, the poll and the direction review are all reasoning
# about a pile that has no single answer to "what is this for".
#
# What is NOT per-workspace: the credentials (one machine, one person, one key)
# and DOCTRINE.md (how we work does not change because the subject did). What IS
# per-workspace is the MISSION - what this particular workspace is for - and it
# is the thing the supervisor holds everything else against.
#
# The default workspace is the state directory itself, not a subdirectory of it.
# That is the one piece of asymmetry here and it is deliberate: everything that
# already exists on this disk keeps working, unmoved, with no migration step
# that could lose a board mid-flight. Later workspaces live under `ws/<slug>`.

DEFAULT_WORKSPACE = "core"
_WORKSPACE_LOCK = threading.Lock()
_SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")

# Anything that has to be dropped when the ground moves under it. The server
# registers its queue here; nothing in this file needs one yet.
WORKSPACE_HOOKS: list = []


class WorkspaceError(ValueError):
    """A workspace cannot be created, removed or switched to right now."""


def _default_registry() -> dict:
    return {
        "current": DEFAULT_WORKSPACE,
        "workspaces": {
            DEFAULT_WORKSPACE: {
                "name": "core development",
                "dir": "",
                "mission": "",
                "created": now(),
            }
        },
    }


def workspaces() -> dict:
    """The registry, repaired rather than trusted.

    A registry naming a current workspace that is not in it would leave every
    path bound to nothing, so the read is where that is caught.
    """
    reg = load_json(WORKSPACES_PATH, None)
    if not isinstance(reg, dict) or not isinstance(reg.get("workspaces"), dict) \
            or not reg["workspaces"]:
        return _default_registry()
    if reg.get("current") not in reg["workspaces"]:
        reg["current"] = DEFAULT_WORKSPACE if DEFAULT_WORKSPACE in reg["workspaces"] \
            else sorted(reg["workspaces"])[0]
    return reg


def _save_workspaces(reg: dict):
    save_json(WORKSPACES_PATH, reg)


def workspace_dir(slug: str, reg: dict | None = None) -> Path:
    reg = reg or workspaces()
    ws = reg["workspaces"].get(slug)
    if ws is None:
        raise WorkspaceError(f"there is no workspace called {slug!r}")
    rel = (ws.get("dir") or "").strip("/")
    return STATE_ROOT / rel if rel else STATE_ROOT


def current_workspace() -> str:
    return workspaces()["current"]


def workspace(slug: str | None = None) -> dict:
    reg = workspaces()
    slug = slug or reg["current"]
    ws = dict(reg["workspaces"].get(slug) or {})
    ws["slug"] = slug
    return ws


def _slug_for(name: str, taken) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:24] or "ws"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def add_workspace(name: str, *, mission: str = "", slug: str | None = None) -> dict:
    """Open a new, empty workspace. It starts with no lanes and no board."""
    name = (name or "").strip()
    if not name:
        raise WorkspaceError("a workspace needs a name")
    with _WORKSPACE_LOCK:
        reg = workspaces()
        slug = slug or _slug_for(name, reg["workspaces"])
        if not _SLUG_OK.match(slug):
            raise WorkspaceError(f"{slug!r} is not a usable workspace id")
        if slug in reg["workspaces"]:
            raise WorkspaceError(f"there is already a workspace called {slug!r}")
        ws = {"name": name, "dir": f"ws/{slug}", "mission": (mission or "").strip(),
              "created": now()}
        reg["workspaces"][slug] = ws
        _save_workspaces(reg)
    (STATE_ROOT / "ws" / slug).mkdir(parents=True, exist_ok=True)
    return dict(ws, slug=slug)


def rename_workspace(slug: str, name: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise WorkspaceError("a workspace needs a name")
    with _WORKSPACE_LOCK:
        reg = workspaces()
        if slug not in reg["workspaces"]:
            raise WorkspaceError(f"there is no workspace called {slug!r}")
        reg["workspaces"][slug]["name"] = name
        _save_workspaces(reg)
        return dict(reg["workspaces"][slug], slug=slug)


def remove_workspace(slug: str) -> dict:
    """Forget a workspace. Its directory is left on disk, untouched.

    Deleting the state would delete goals, rulings and transcripts that are the
    only record of what was decided and why. Dropping it from the registry makes
    it stop appearing; recovering it is a matter of putting the entry back.
    """
    with _WORKSPACE_LOCK:
        reg = workspaces()
        if slug not in reg["workspaces"]:
            raise WorkspaceError(f"there is no workspace called {slug!r}")
        if slug == reg["current"]:
            raise WorkspaceError("switch to another workspace before removing this one")
        if len(reg["workspaces"]) == 1:
            raise WorkspaceError("this is the only workspace")
        gone = reg["workspaces"].pop(slug)
        _save_workspaces(reg)
        return dict(gone, slug=slug, dropped=True)


def switch_blocked() -> str | None:
    """Why the ground must not move right now, or None.

    Every path in this file is a module global, so a switch rebinds them under
    whatever is already running. A worker that started against one board and
    settles against another writes its result into the wrong workspace, and the
    lane it was holding never gets released. There is no clever fix for that
    which is worth having - the honest answer is to refuse while anything is in
    flight and say what is in flight.
    """
    n = live_workers()
    if n:
        # Name them. "2 workers still running" is a fact the operator cannot act
        # on; "traaviis, trvm" tells them which two lanes to stop or wait for,
        # which is the difference between a refusal and a dead end.
        where = live_worker_lanes()
        return (f"{n} worker{'s' if n != 1 else ''} still running"
                + (f" in {', '.join(where)}" if where else ""))
    if orch_busy():
        return "the orchestrator is mid-turn"
    return None


def use_workspace(slug: str) -> dict:
    """Point the whole harness at another workspace."""
    with _WORKSPACE_LOCK:
        reg = workspaces()
        if slug not in reg["workspaces"]:
            raise WorkspaceError(f"there is no workspace called {slug!r}")
        if slug == reg["current"]:
            return workspace(slug)
        why = switch_blocked()
        if why:
            raise WorkspaceError(f"cannot switch workspace: {why}")
        base = workspace_dir(slug, reg)
        base.mkdir(parents=True, exist_ok=True)
        _bind_state(base)
        reg["current"] = slug
        _save_workspaces(reg)
    # Outside the lock: a hook that wanted to read state would deadlock on it,
    # and by here the rebinding is already done and durable.
    for hook in list(WORKSPACE_HOOKS):
        try:
            hook(slug)
        except Exception:                                     # noqa: BLE001
            pass
    return workspace(slug)


# ---------------------------------------------------------------- the mission
#
# The doctrine says how work is done anywhere in this ecosystem. The mission
# says what THIS workspace is for, in the operator's own words, and it is the
# only text in the harness that is neither derived nor generated - nothing
# proposes it, nothing reviews it, nothing may edit it but Travis. Rule 6 in one
# field.
#
# It goes out with the doctrine to every planner, worker and review, because a
# plan that is excellent and off-mission is the expensive failure this is here
# to catch.


def mission(slug: str | None = None) -> str:
    return (workspace(slug).get("mission") or "").strip()


def set_mission(text: str, slug: str | None = None) -> dict:
    with _WORKSPACE_LOCK:
        reg = workspaces()
        slug = slug or reg["current"]
        if slug not in reg["workspaces"]:
            raise WorkspaceError(f"there is no workspace called {slug!r}")
        reg["workspaces"][slug]["mission"] = (text or "").strip()
        reg["workspaces"][slug]["mission_at"] = now()
        _save_workspaces(reg)
        return dict(reg["workspaces"][slug], slug=slug)


def mission_block(intro: str = "What this workspace is for") -> str:
    """The mission under a heading, or nothing. Never an invented one."""
    m = mission()
    return f"\n\n# {intro}\n\n{m}\n" if m else ""


def workspace_view() -> dict:
    reg = workspaces()
    cur = reg["current"]
    return {
        "current": cur,
        "mission": mission(cur),
        "blocked": switch_blocked(),
        "list": [
            {"slug": s, "name": w.get("name") or s,
             "mission": (w.get("mission") or "").strip(),
             "created": w.get("created"), "current": s == cur}
            for s, w in sorted(reg["workspaces"].items(),
                               key=lambda kv: kv[1].get("created") or "")
        ],
    }


# Import binds to the root; this moves it to whichever workspace was in use when
# the console last stopped. Wrapped because a harness that cannot start at all
# because of a damaged registry is worse than one that starts on the default.
try:
    _bind_state(workspace_dir(current_workspace()))
except Exception as _e:                                       # noqa: BLE001
    print(f"amp: staying on the default workspace ({_e})", file=sys.stderr)


CHROME_NAMES = ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser")


def browser_cmd() -> str | None:
    """Chrome by preference. python's webbrowser picks firefox on this box."""
    for name in CHROME_NAMES:
        path = shutil.which(name)
        if path:
            return path
    return shutil.which("xdg-open")


def open_url(url: str) -> bool:
    cmd = browser_cmd()
    if not cmd:
        return False
    subprocess.Popen([cmd, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return True


def run(cmd: list[str], cwd: Path | None = None, check: bool = False,
        env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, check=check,
        env=env,
    )


# The console runs claude workers on background threads, so board writes are
# serialized: two workers finishing at once must not lose each other's record.
_BOARD_LOCK = threading.Lock()


def record_task(lane_name: str, rec: dict):
    with _BOARD_LOCK:
        b = board()
        b.setdefault("tasks", {}).setdefault(lane_name, []).insert(0, rec)
        save_json(BOARD_PATH, b)


def update_task(lane_name: str, task_id: str, patch: dict):
    with _BOARD_LOCK:
        b = board()
        for rec in b.get("tasks", {}).get(lane_name, []):
            if rec.get("task_id") == task_id:
                rec.update(patch)
                break
        save_json(BOARD_PATH, b)


_CHAT_LOCK = threading.Lock()


def add_note(text: str, lane: str | None = None) -> dict:
    """A line the operator typed into the orchestrator thread.

    Only notes are stored. Everything else in the thread is derived from the
    board, so the feed can never drift from what the workers actually did.
    """
    note = {"id": uuid.uuid4().hex[:12], "at": now(), "text": text, "lane": lane}
    with _CHAT_LOCK:
        chat = load_json(CHAT_PATH, {"notes": []})
        chat.setdefault("notes", []).append(note)
        save_json(CHAT_PATH, chat)
    return note


def notes() -> list[dict]:
    return load_json(CHAT_PATH, {"notes": []}).get("notes", [])


# ---------------------------------------------------------------- orchestrator

_ORCH_LOCK = threading.Lock()


def orchestrator() -> dict:
    return load_json(ORCH_PATH, {"session_id": None, "turns": []})


def orchestrator_turns() -> list[dict]:
    return orchestrator().get("turns", [])


def orch_append(turn: dict) -> dict:
    with _ORCH_LOCK:
        o = orchestrator()
        o.setdefault("turns", []).append(turn)
        save_json(ORCH_PATH, o)
    return turn


def orch_update(turn_id: str, patch: dict):
    with _ORCH_LOCK:
        o = orchestrator()
        for t in o.get("turns", []):
            if t.get("id") == turn_id:
                t.update(patch)
                break
        if "session_id" in patch and patch["session_id"]:
            o["session_id"] = patch["session_id"]
        save_json(ORCH_PATH, o)


def orch_busy() -> bool:
    return any(t.get("status") == "running" for t in orchestrator_turns())


# ---------------------------------------------------------------- doctrine
#
# The one piece of context that is not about a lane. Everything else this
# harness assembles - the repository state, the transcript, the board - answers
# "what is here"; this answers "what are we holding ourselves to, and what are
# we trying to find out". It is written once, at {root}/DOCTRINE.md, and injected
# verbatim into every architect plan, every architect review, every goal
# worker's prompt and the orchestrator's own brief, so that no surface can drift
# from it independently.
#
# Only the block between the markers travels. The commentary below it in that
# file is where each rule came from and what it cost to learn - worth reading,
# too long to carry into every prompt, and the discipline of keeping the core
# short is what keeps it from being skimmed.

DOCTRINE_PATH = Path(os.environ.get("AMP_DOCTRINE") or (ROOT / "DOCTRINE.md"))
DOCTRINE_BEGIN = "<!-- BEGIN CORE -->"
DOCTRINE_END = "<!-- END CORE -->"
_DOCTRINE_CACHE: dict = {"mtime": None, "text": ""}


def doctrine() -> str:
    """The injectable core of DOCTRINE.md, or "" if there isn't one.

    Cached on mtime rather than read once at import, so editing the file takes
    effect on the next prompt instead of the next restart - the point of writing
    the values down is that they can be corrected, and a correction nobody is
    running under is not a correction.

    Missing file, missing markers and empty core all return "", and every caller
    treats that as "say nothing" rather than substituting a default. A doctrine
    this harness made up for itself would be exactly the thing rule 6 forbids.
    """
    try:
        st = DOCTRINE_PATH.stat()
    except OSError:
        _DOCTRINE_CACHE.update(mtime=None, text="")
        return ""
    key = (st.st_mtime_ns, st.st_size)
    if _DOCTRINE_CACHE["mtime"] == key:
        return _DOCTRINE_CACHE["text"]
    try:
        raw = DOCTRINE_PATH.read_text()
    except (OSError, UnicodeDecodeError):
        raw = ""
    a, b = raw.find(DOCTRINE_BEGIN), raw.find(DOCTRINE_END)
    core = raw[a + len(DOCTRINE_BEGIN):b].strip() if 0 <= a < b else ""
    _DOCTRINE_CACHE.update(mtime=key, text=core)
    return core


def doctrine_block(intro: str) -> str:
    """The core under a heading, or nothing at all. Never a partial quotation."""
    core = doctrine()
    return f"\n\n# {intro}\n\n{core}\n" if core else ""


# Ratification, and why a digest rather than a permission.
#
# Nothing here can stop an agent editing DOCTRINE.md - the orchestrator runs in
# the shared checkout with a shell, and a rule that a determined process can
# ignore is not an enforcement mechanism. What can be made true is that such an
# edit cannot happen *quietly*: the core is pinned by digest, and any change to
# it stands on the board, named, until the operator says it was theirs. A
# doctrine a system can amend without anyone noticing is one it holds nobody to.
#
# The harness never pins on its own. An unratified core is itself the
# observation - that is the whole point of rule 6.

# DOCTRINE_PIN_PATH is bound by _bind_state; see _STATE_LAYOUT. The doctrine
# itself is ecosystem-wide, but ratifying it is an act by an operator looking at
# one board, so the pin travels with the workspace.


def doctrine_digest() -> str:
    core = doctrine()
    return hashlib.sha256(core.encode()).hexdigest()[:16] if core else ""


def doctrine_pin() -> dict:
    return load_json(DOCTRINE_PIN_PATH, {"digest": None, "at": None})


def ratify_doctrine() -> dict:
    """The operator saying this version of the core is the one that stands."""
    pin = {"digest": doctrine_digest(), "at": now()}
    save_json(DOCTRINE_PIN_PATH, pin)
    return pin


def doctrine_state() -> dict:
    """Whether what is being injected is what the operator last ratified."""
    cur = doctrine_digest()
    pin = doctrine_pin()
    return {"present": bool(cur), "digest": cur, "ratified": pin.get("digest"),
            "ratified_at": pin.get("at"),
            "drifted": bool(cur and pin.get("digest") and pin["digest"] != cur),
            "unratified": bool(cur and not pin.get("digest"))}


# ---------------------------------------------------------------- findings
#
# The return path. Doctrine going out to every worker is only half a loop: if
# nothing comes back, the values can only be obeyed, never corrected, and the
# work that would have corrected them is spent and forgotten inside a transcript
# nobody re-reads. So every worker report and every architect review ends with a
# DOCTRINE: line, and the ones that say something land here, unread, until the
# operator has actually been told.
#
# `none` is deliberately not stored. It is the ordinary answer and storing it
# would bury the four findings a week under two hundred non-findings - the same
# reason the doctrine tells workers not to manufacture one.

# FINDINGS_PATH is bound by _bind_state; see _STATE_LAYOUT.
_FINDINGS_LOCK = threading.Lock()

# Ordered by how much they should interrupt the operator. A contradiction means
# something we believed and acted on is false, so it outranks a success.
BEARINGS = ("contradicted", "advanced", "proposed", "none")

# The heading, however it gets dressed up: `DOCTRINE:`, `## DOCTRINE:`,
# `**DOCTRINE:**`. The bearing is the first word after it, and everything to the
# end of the text is the finding.
_DOCTRINE_HEAD = re.compile(r"^[\s>#*_-]*DOCTRINE[\s*_]*:[\s*_]*", re.I | re.M)


def parse_doctrine(text: str) -> dict | None:
    """The DOCTRINE: finding at the end of a report, or None.

    The *last* heading, not the first: a worker that quotes its own instructions
    back before answering them would otherwise be read as reporting the example.
    An unrecognised first word is kept as `proposed` rather than dropped - a
    finding that does not fit the four words is still a finding, and silently
    discarding it is the failure this channel exists to prevent.
    """
    if not text:
        return None
    heads = list(_DOCTRINE_HEAD.finditer(text))
    if not heads:
        return None
    body = text[heads[-1].end():].strip().lstrip("*_ \t")
    if not body:
        return None
    first = re.match(r"[a-zA-Z]+", body)
    word = first.group(0).lower() if first else ""
    bearing = word if word in BEARINGS else "proposed"
    if bearing == word:
        body = body[first.end():].lstrip(" *_-:.\u2014\t\n")
    if bearing == "none":
        return {"bearing": "none", "text": body[:600]}
    return {"bearing": bearing, "text": body[:4000]} if body else None


def record_finding(bearing: str, text: str, *, lane: str | None = None,
                   source: str = "worker", **where) -> dict | None:
    """File a finding. Returns None for `none`, which is not worth keeping."""
    bearing = (bearing or "").strip().lower()
    text = (text or "").strip()
    if bearing not in BEARINGS or bearing == "none" or not text:
        return None
    f = {"id": uuid.uuid4().hex[:12], "at": now(), "bearing": bearing,
         "text": text[:4000], "lane": lane, "source": source,
         "read_at": None, **{k: v for k, v in where.items() if v}}
    with _FINDINGS_LOCK:
        store = load_json(FINDINGS_PATH, {"findings": []})
        store.setdefault("findings", []).append(f)
        store["findings"] = store["findings"][-500:]
        save_json(FINDINGS_PATH, store)
    return f


def record_worker_finding(lane_name: str, rec: dict) -> dict | None:
    """File whatever a finished worker said under DOCTRINE:, if anything.

    Read from `result` - the worker's own last message - and not from the report
    the architect gets, which carries a diff that may well contain the word.
    """
    f = parse_doctrine(rec.get("result") or "")
    if not f:
        return None
    return record_finding(f["bearing"], f["text"], lane=lane_name, source="worker",
                          task_id=rec.get("task_id"), goal_id=rec.get("goal_id"))


def findings(*, lane: str | None = None, unread_only: bool = False) -> list[dict]:
    """Newest first, and a contradiction ahead of anything else the same day."""
    out = load_json(FINDINGS_PATH, {"findings": []}).get("findings", [])
    if lane:
        out = [f for f in out if f.get("lane") == lane]
    if unread_only:
        out = [f for f in out if not f.get("read_at")]
    return sorted(out, key=lambda f: (f.get("bearing") == "contradicted",
                                      f.get("at") or ""), reverse=True)


def ack_findings(ids: list[str]) -> int:
    """Mark findings as told to the operator. Only they can make this true."""
    want = set(ids or ())
    n = 0
    with _FINDINGS_LOCK:
        store = load_json(FINDINGS_PATH, {"findings": []})
        for f in store.get("findings", []):
            if f.get("id") in want and not f.get("read_at"):
                f["read_at"] = now()
                n += 1
        save_json(FINDINGS_PATH, store)
    return n


def findings_summary() -> dict:
    """What the header needs: how many are waiting, and whether any is bad news."""
    unread = findings(unread_only=True)
    return {"unread": len(unread),
            "contradicted": sum(1 for f in unread if f["bearing"] == "contradicted"),
            "top": unread[0] if unread else None}


# ------------------------------------------------------------------ obligations
#
# A goal is a thing that becomes true once and is then finished. An obligation is
# a thing that has to KEEP being true - the published docs still describe the code,
# the benchmark page still shows the numbers the benchmark actually produced.
#
# The difference that matters is not the schedule. It is that an obligation owns a
# CHECK which decides whether it is currently true. A timer that re-runs a job
# spends a worker whether or not anything moved; a check spends one only when
# something did, and it answers "is this current?" with an observation instead of
# an assumption. That is rule 1 applied to maintenance.
#
# Two deliberate limits, both learned rather than chosen:
#
#   - Checks run at ROOT, in the shared checkout. They have to: the docs atlas
#     build scans every repo in the stack and cannot see a sibling from inside a
#     lane worktree. A check is read-only, so this is safe.
#
#   - A FIX is a worker that writes, and `claude_worktree` already documents why a
#     background writer in the shared checkout is dangerous - interactive sessions
#     run against it constantly. So auto_fix defaults to False: drift is reported,
#     and dispatching something to repair it stays the operator's call until they
#     say otherwise per obligation. An obligation that repaired the tree out from
#     under a live session would be worse than a stale docs site.

# OBLIGATIONS_PATH is bound by _bind_state; see _STATE_LAYOUT.
_OBLIGATION_LOCK = threading.Lock()
OBLIGATION_STATES = ("unchecked", "ok", "drifted", "broken")


def obligations() -> list[dict]:
    return load_json(OBLIGATIONS_PATH, [])


def save_obligations(obs: list[dict]):
    save_json(OBLIGATIONS_PATH, obs)


def add_obligation(name: str, check: str, *, why: str = "", fix: str = "",
                   every_hours: float = 24.0, lane: str | None = None,
                   auto_fix: bool = False) -> dict:
    """Register something that has to keep being true, and the command that decides."""
    name = (name or "").strip()
    check = (check or "").strip()
    if not name:
        die("an obligation needs a name")
    if not check:
        die("an obligation needs a check - without one it is a reminder, not an obligation")
    with _OBLIGATION_LOCK:
        obs = obligations()
        if any(o["name"] == name for o in obs):
            die(f"there is already an obligation named {name!r}")
        ob = {"id": "o" + uuid.uuid4().hex[:8], "name": name, "why": why.strip(),
              "check": check, "fix": fix.strip(), "every_hours": float(every_hours),
              "lane": lane, "auto_fix": bool(auto_fix), "enabled": True,
              "state": "unchecked", "added_at": now(),
              "last_checked": None, "last_ok_at": None, "last_drift_at": None,
              "last_rc": None, "last_output": "", "last_task_id": None, "checks": 0}
        obs.append(ob)
        save_obligations(obs)
    return ob


def update_obligation(oid: str, patch: dict) -> dict | None:
    with _OBLIGATION_LOCK:
        obs = obligations()
        for o in obs:
            if o["id"] == oid:
                o.update(patch)
                save_obligations(obs)
                return o
    return None


def remove_obligation(oid: str) -> bool:
    with _OBLIGATION_LOCK:
        obs = obligations()
        keep = [o for o in obs if o["id"] != oid]
        if len(keep) == len(obs):
            return False
        save_obligations(keep)
    return True


def obligation_due(ob: dict, *, at: float | None = None) -> bool:
    """Enabled, and not checked within its own interval."""
    if not ob.get("enabled"):
        return False
    last = ob.get("last_checked")
    if not last:
        return True
    try:
        prev = datetime.fromisoformat(last).timestamp()
    except (TypeError, ValueError):
        return True
    return (at or time.time()) - prev >= float(ob.get("every_hours") or 24.0) * 3600.0


def run_obligation_check(ob: dict, *, timeout: int = 900) -> dict:
    """Run the check in the shared checkout and record what it said.

    Three outcomes, kept apart on purpose. rc 0 is `ok`. A non-zero rc is
    `drifted` - the thing the check describes is no longer true. A check that
    could not run at all (timeout, missing interpreter) is `broken`, NOT drifted:
    reporting "the docs are stale" because the checker crashed would be inventing
    an observation, and a broken check must be fixed before its verdict means
    anything.
    """
    started = now()
    try:
        p = subprocess.run(ob["check"], shell=True, cwd=str(ROOT), timeout=timeout,
                           capture_output=True, text=True)
        rc, out = p.returncode, ((p.stdout or "") + (p.stderr or ""))
    except subprocess.TimeoutExpired:
        rc, out = None, f"check exceeded {timeout}s and was killed"
    except OSError as e:
        rc, out = None, f"check could not run: {e}"
    state = "broken" if rc is None else ("ok" if rc == 0 else "drifted")
    patch = {"state": state, "last_checked": started, "last_rc": rc,
             "last_output": (out or "").strip()[-4000:],
             "checks": int(ob.get("checks") or 0) + 1}
    if state == "ok":
        patch["last_ok_at"] = started
    elif state == "drifted":
        patch["last_drift_at"] = started
    return update_obligation(ob["id"], patch) or {**ob, **patch}


def obligations_summary() -> dict:
    obs = obligations()
    return {"total": len(obs),
            "drifted": sum(1 for o in obs if o.get("state") == "drifted"),
            "broken": sum(1 for o in obs if o.get("state") == "broken"),
            "unchecked": sum(1 for o in obs if o.get("state") == "unchecked")}


ORCH_BRIEF = """You are [&] orchestrator: the operator's single window onto this workspace.

You run in the shared checkout at {root} - not a worktree - so you can see
uncommitted work, run git, and answer questions about what is actually here.
{doctrine}
That is what this whole workspace is for, and it is what the operator wants to
talk about. Dispatching and status are the mechanics; the conversation worth
having is which claims moved up the ladder, what got contradicted, and what is
worth building next because of it. When you report on work, report it in those
terms - "this took X from in_tree to live_local, here is the evidence" - not as
a list of tasks that finished.

Findings come back to you through the harness, not by you going to look:
  curl -s '{base}/api/findings'
      what workers and the architect have reported under DOCTRINE: - what
      advanced, what was contradicted, what is being proposed. `unread` is what
      the operator has not been told yet. A contradiction outranks everything
      else on the board; surface it first and by name.
  curl -s -X POST {base}/api/findings/ack \\
       -H 'content-type: application/json' -d '{{"ids":["ID","ID"]}}'
      mark findings as told to the operator, once you have actually told them

You may propose an amendment to the doctrine and you must not make one. If work
here shows a rule is wrong, or there is a value worth adding, say so to the
operator as a proposal with what would test it. Editing {root}/DOCTRINE.md is
theirs alone - a system that can rewrite what it is held to is not held to
anything.

Some things are not goals but obligations: they have to keep being true, and each
owns a check that decides whether it currently is. A goal finishes; an obligation
comes back. The harness runs the checks on a clock and you can read the verdicts:
  curl -s '{base}/api/obligations'
      each one's name, its check, when it last ran, and what it said. `drifted`
      means the check ran and the thing is no longer true. `broken` means the
      check could not run - that is NOT drift, and reporting it as though the
      artifact were stale would be inventing an observation.
  curl -s -X POST {base}/api/obligation/check \\
       -H 'content-type: application/json' -d '{{"id":"ID"}}'
      run one now instead of waiting for the clock

Repairing drift is a dispatch, and most obligations are about artifacts built
from the whole workspace rather than one repo - so their fix has to run in the
shared checkout, where the operator's own sessions are working. That is why
auto-fix is off by default. Tell the operator what has drifted and what the
recorded fix is; do not run it in the shared tree on your own initiative.

The console you are part of is serving at {base}. Drive the board through it
rather than reimplementing it: every guard (one worker per lane, the global
worker cap, budgets, timeouts, the memory ceiling) lives behind these
endpoints, and none of them apply to anything you run yourself.

  curl -s {base}/api/state
      lanes with their paths and backends, recent tasks per lane, how many
      workers are running and the cap
  curl -s '{base}/api/log?lane=NAME&task_id=ID'
      a worker's transcript, turn by turn
  curl -s '{base}/api/consults?lane=NAME'
      escalation threads with GPT-5.6 and their rulings
  curl -s -X POST {base}/api/dispatch \\
       -H 'content-type: application/json' \\
       -d '{{"lane":"NAME","prompt":"...","budget":1,"model":"opus"}}'
      start a worker. Returns immediately - it runs in the background. Every
      prompt you write should end with the landing rule below, because a worker
      that does not commit loses its work to the clock, and one that commits
      without building leaves a broken commit that looks finished.
  curl -s -X POST {base}/api/ask \\
       -H 'content-type: application/json' \\
       -d '{{"lane":"NAME","question":"..."}}'
      escalate to GPT-5.6, the architect, about a lane
  curl -s -X POST {base}/api/cancel \\
       -H 'content-type: application/json' -d '{{"task_id":"ID"}}'
      stop a running worker and everything it spawned
  curl -s -X POST {base}/api/restart \\
       -H 'content-type: application/json' -d '{{"lane":"NAME"}}'
      stop a worker and run its own prompt again. Spends a second budget, so
      only when the operator asked for it
  curl -s -X POST {base}/api/lane/add \\
       -H 'content-type: application/json' \\
       -d '{{"lane":"NAME","path":"DIR"}}'
      register a repo under {root} as a new lane, so you can dispatch to it.
      `path` defaults to the lane name; `repo` is read off the git origin;
      `branch` defaults to main. Answers {{"ok":false,"error":"..."}} with the
      reason if the directory is not a repo root, the branch does not exist, or
      the name is already taken

Work that has no lane is not work you cannot do - it is a lane you have not
made yet. If the operator points at a repo under {root} with nothing covering
it, add the lane and dispatch, in the same turn. Do not offer to do the work
yourself in your own turn instead: that spends your budget on something a
worker is for, and leaves nothing on the board to watch. Only ask first if the
right repo or branch is genuinely ambiguous.

When the operator states an objective rather than a task - "finish X", "get Y
working", anything with an end state rather than an action - open a **goal**,
do not dispatch a worker at it. A goal is the whole cycle: the architect writes
a definition of done and an ordered task list against the real repository, a
worker takes one task at a time, every result is judged against that definition,
any done-item carrying a `check` is settled by running the command rather than
by anyone's opinion, and the next task goes out on its own. It ends when the
definition of done is met, or it stops and says which bound it hit.

  curl -s -X POST {base}/api/goal/open \\
       -H 'content-type: application/json' \\
       -d '{{"lane":"NAME","objective":"finish the X implementation"}}'
      plan and start one. This calls the architect, so it takes ~20s
  curl -s '{base}/api/goal?id=GID'
      the whole goal: done-conditions with what met them, tasks and their
      states, the log of every round
  curl -s -X POST {base}/api/goal/answer \\
       -H 'content-type: application/json' -d '{{"goal_id":"GID","text":"..."}}'
      answer what it asked and let it carry on
  curl -s -X POST {base}/api/goal/push \\
       -H 'content-type: application/json' -d '{{"goal_id":"GID"}}'
      give a goal that ran out of rounds its budget back
  curl -s -X POST {base}/api/goal/close \\
       -H 'content-type: application/json' -d '{{"goal_id":"GID"}}'
      abandon it

`observations` in /api/state is the harness saying what is obviously wrong right
now, computed from the board and the worktrees - not guessed. Read it before
answering "how are things going", and quote it rather than re-deriving it:
- `repeat-kill` - the same instruction has been killed by a limit more than
  once. Sending it again unchanged buys another thirty minutes of nothing
- `work-at-risk` - uncommitted changes sitting in a lane worktree with no
  worker in it. Real work that no branch will ever be merged from
- `thread-mute` - a thread waiting on a worker that already finished
- `goal-stopped` / `thread-stopped` - waiting on the operator
- `shared-repo` - two lanes over one directory, which is how two workers each
  "finish" the same thing on separate branches

A consult carries itself: a ruling that ends in NEED: lines has its file needs
answered off disk and a worker sent for anything observable, and the report goes
back to the architect automatically, round after round. So `blocked_on` on a
thread is the thing to read, not the presence of needs:
- `gathering` - a worker is out fetching it. Nothing to do, and nothing to
  report as stalled
- `operator` - it asked for a decision only the operator can make. This one is
  worth surfacing to them, with the actual question
- `stalled`, `rounds`, `tokens` - it stopped itself. Say so plainly; do not
  restart it by asking the same thing again without adding something new

  curl -s -X POST {base}/api/consult/continue \\
       -H 'content-type: application/json' -d '{{"consult_id":"ID"}}'
      send a halted thread after what it is missing again - files off disk, a
      worker for anything observable. This is also how a thread from before any
      of that existed gets asked for the first time

Never answer "is it stuck?" from elapsed time or from a silent log - a worker
halfway through a twenty-minute build looks exactly like a wedged one. The
`workers` map in /api/state has already made that call, per task id:
- `phase` and `doing` - what it is doing right now, read from its transcript
  ("running gcc -O2 ...", "editing src/foo.c", "thinking")
- `quiet_s` - how long since it last wrote anything
- `cpu_pct` - how hard its process group is working. Quiet AND cold is hung;
  quiet AND hot is a build, and is fine
- `stalled` - the harness's own verdict. Trust this one over your own reading
- `adopted` - it outlived a console restart and was picked back up
A stalled worker is killed by the harness on its own, with the reason written
to the board. You do not need to watch for that, and cannot.

Lanes are separate git repos under {root}. A dispatched worker gets a clean
worktree on branch amp/NAME cut from committed HEAD, so it cannot see
uncommitted work in the shared checkout. If that matters for a task, say so
instead of dispatching blind.

End every worker prompt you write with this, verbatim. A goal's workers get it
already; one you dispatch yourself does not, and both failure modes it names
have actually happened here:

{landing}

Ask every worker you dispatch to end its report with a `DOCTRINE:` section
saying which of `advanced` / `contradicted` / `proposed` / `none` its work bears
on, and why. A goal's workers are already asked this. `none` is the ordinary
answer and is fine; an invented finding is worse than no finding.

What you cannot do, and must not promise:
- **You stop existing the moment you reply.** There is no background you. Never
  say you will watch for something, check back, follow up, or do anything
  "when X finishes" - none of that can happen. If it needs doing later, either
  do it now or tell the operator it is theirs to trigger.
- A dispatch with no slot free is not refused, it is **queued**, and the worker
  whose finishing frees the slot starts it. /api/dispatch answers
  `{{"queued": true, "position": N}}` when that happens. Report that as queued
  by the harness - not as something you will come back and do.

How to behave:
- You are a chat first. If the question is "is anything running", answer it.
  Do not dispatch anything unless the operator asked for work to be done.
- Anything long belongs in a lane worker, not in your own turn. Dispatch it,
  then say what you dispatched and where to watch it.
- Read /api/state before naming a lane. Never dispatch to one that is not
  there - create it with /api/lane/add first, then dispatch to it.
- Be brief. The reply lands in a small chat dock, not a terminal.

How this harness authenticates, because it is not how you would guess:
- Workers do NOT use ~/.claude/.credentials.json. That file is very likely
  stale and it does not matter. Reading it, or running `claude auth status`,
  tells you nothing about whether this harness can dispatch.
- The harness mints its own long-lived worker token via the console's
  "Connect Claude" button and stores it at {root}/.amp/.secrets.json under
  `claude_oauth_token`. It is injected into every worker subprocess - including
  the one running you right now.
- So: if you are answering at all, worker auth is working. To check properly,
  read `health.claude_auth` from `curl -s {base}/api/state` - null means fine,
  a string is the actual problem. It is the same reading the header dot shows.
- A `failed` task on a lane can be hours old. Check its timestamp before
  calling anything broken, and never tell the operator to run `claude login`
  or `claude setup-token` in a terminal - the console signs in by itself.

git, specifically:
- Commit when asked. Stage named paths, not `.` or `-A`, so nothing stray or
  secret rides along.
- {root}/.amp is harness state - board, keys, packets, rulings. Never commit it.
- Do not push, force-push, reset --hard, or delete branches unless the
  operator's message asks for it. Their message is the authorisation.
- Most subdirectories here are their own git repos. Commit inside the repo that
  owns the change; do not bump a gitlink in the parent."""


def orchestrator_brief(base_url: str) -> str:
    return ORCH_BRIEF.format(
        root=ROOT, base=base_url, landing=landing_rule(),
        doctrine=mission_block() + doctrine_block("What we are held to"))


def orchestrator_ask(text: str, *, base_url: str, model: str | None = None,
                     budget: float | None = None) -> dict:
    """One orchestrator turn: record it, run it, record the reply.

    Blocking. The console runs it on a thread so the feed can show it working.
    """
    cfg = config()
    model = model or cfg.get("orchestrator_model", DEFAULT_ORCH_MODEL)
    budget = float(budget or cfg.get("orchestrator_budget_usd", DEFAULT_ORCH_BUDGET_USD))

    o = orchestrator()
    prior = o.get("session_id")
    turn_id = uuid.uuid4().hex[:12]
    orch_append({"id": f"u{turn_id}", "at": now(), "role": "you", "text": text})
    orch_append({"id": turn_id, "at": now(), "role": "amp", "status": "running",
                 "text": "", "model": model})

    problem = claude_auth_problem()
    if problem:
        orch_update(turn_id, {"status": "failed", "text": problem, "finished_at": now()})
        return {"ok": False, "error": problem}

    out = claude_turn(
        text, ROOT,
        model=model, budget=budget,
        session_id=prior or str(uuid.uuid4()),
        task_id=f"orch:{turn_id}",
        resume=bool(prior),
        system=orchestrator_brief(base_url),
    )
    orch_update(turn_id, {
        "status": out.get("status"),
        "text": out.get("result") or out.get("error") or "",
        "cost_usd": out.get("cost_usd"),
        "num_turns": out.get("num_turns"),
        "session_id": out.get("session_id"),
        "finished_at": now(),
    })
    return {"ok": out.get("status") == "completed", "turn_id": turn_id,
            "error": out.get("error")}


def latest_claude_task(lane_name: str) -> dict | None:
    for rec in board().get("tasks", {}).get(lane_name, []):
        if rec.get("backend") == "claude":
            return rec
    return None


def resumable_session(lane_name: str, ident: str) -> str | None:
    """The session behind a named task, if it can still be resumed.

    Resuming only ever reached the newest task on a lane, which is exactly the
    wrong one: a worker killed by a limit is usually followed by others, so the
    session actually worth continuing is buried. This resolves any task id or
    session id on the lane - a prefix will do - and confirms the transcript is
    still on disk, because a session with no transcript cannot be resumed and
    saying so now is better than a worker failing to start.
    """
    ident = (ident or "").strip()
    if not ident:
        return None
    for rec in board().get("tasks", {}).get(lane_name, []):
        if rec.get("backend") != "claude":
            continue
        sid = rec.get("session_id") or rec.get("resume_of") or rec.get("task_id")
        if not sid:
            continue
        if ident in (rec.get("task_id"), sid) or sid.startswith(ident) \
                or (rec.get("task_id") or "").startswith(ident):
            return sid if transcript_path(sid) else None
    return None


def find_openrouter_key() -> str | None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key.strip()
    key = load_json(SECRETS_PATH, {}).get("openrouter_api_key")
    return key.strip() if key else None


def openrouter_key() -> str:
    key = find_openrouter_key()
    if not key:
        die(
            "no OpenRouter key. Set OPENROUTER_API_KEY, or write "
            f"{SECRETS_PATH.name} as {{\"openrouter_api_key\": \"sk-or-...\"}} (untracked)."
        )
    return key


def openrouter_enabled() -> bool:
    """The switch in Settings. On unless it has been turned off."""
    return bool(config().get("openrouter_enabled", True))


def openrouter_available() -> bool:
    """Whether anything may spend on OpenRouter right now.

    Callers should ask this rather than `find_openrouter_key`, so that switching
    it off is the same kind of thing as never having had a key: the paths that
    already knew how to do without an architect keep working.
    """
    return openrouter_enabled() and bool(find_openrouter_key())


def architect_backend() -> str:
    """Who answers as the architect: your ChatGPT subscription, or the API.

    An unrecognised value falls back to the default rather than failing, because
    a typo in config.json should not take the architect away entirely.
    """
    b = config().get("architect_backend") or DEFAULT_ARCHITECT
    return b if b in ARCHITECT_BACKENDS else DEFAULT_ARCHITECT


def architect_available() -> bool:
    """Whether an architect round can run at all right now.

    Ask this rather than `openrouter_available`: which backend is wired up is
    settings, and the paths that skip a round when there is no architect should
    not have to know or care which one is missing.
    """
    # The role switch is checked first and for the same reason the light exists:
    # off means refused. If it only dimmed a dot while every escalation still
    # went out, the switch would be decoration.
    if not role_on("architect"):
        return False
    # Asked of the model, not of the backend name, so there is one answer to
    # "is Claude signed in" however many roles are pointed at it.
    ok, _ = model_ready(role_model("architect"))
    return ok


def architect_off_reason() -> str:
    """Why the architect is unavailable, in the words that say what to do.

    "no OpenRouter key configured" is wrong and unhelpful when the truth is that
    you turned it off on purpose two minutes ago, or that the architect is not
    on OpenRouter at all any more.
    """
    if not role_on("architect"):
        return "the architect is switched off in Settings"
    m = role_model("architect")
    _, why = model_ready(m)
    # The fallback is reachable only in a race - available when checked, asked
    # why a moment later. Say that. "the architect is unavailable" would be a
    # tautology with nothing to act on, which is the thing this function exists
    # to avoid.
    return why or f"the architect ({m}) was available a moment ago - try again"


# ------------------------------------------------------- the three role lights
#
# Note what is NOT here: a second copy of `claude_model`, `architect_backend` or
# `consult_model`. A role panel that kept its own idea of the worker model would
# drift from the one the dispatcher actually reads, and the header would then be
# lit for a model nobody is running. These read and write through to the setting
# that already owned each one.

def role_on(role: str) -> bool:
    """Whether this role may run at all. A role nobody has touched is on."""
    return bool((config().get("roles") or {}).get(role, {}).get("on", True))


def role_allows(role: str, model: str) -> str:
    """Why this role cannot be that model, or empty if it can."""
    if model == FOLLOW_ARCHITECT:
        return "" if role == "supervisor" else "only the supervisor can follow the architect"
    if model not in MODELS:
        return f"there is no model called {model!r}"
    return ROLE_CANNOT.get(role, {}).get(model_kind(model), "")


def role_choice(role: str) -> str:
    """What was chosen for this role, which is not always what it resolves to."""
    cfg = config()
    if role == "worker":
        m = cfg.get("claude_model") or DEFAULT_CLAUDE_MODEL
        return m if not role_allows("worker", m) else DEFAULT_CLAUDE_MODEL
    if role == "architect":
        b = architect_backend()
        if b == "codex":
            return "codex"
        if b == "claude":
            m = cfg.get("claude_architect_model") or DEFAULT_CLAUDE_MODEL
            return m if model_kind(m) == "claude" else DEFAULT_CLAUDE_MODEL
        m = cfg.get("consult_model")
        return m if m in CONSULT_MODELS else DEFAULT_CONSULT
    if role == "supervisor":
        m = (cfg.get("roles") or {}).get("supervisor", {}).get("model") or FOLLOW_ARCHITECT
        return m if not role_allows("supervisor", m) else FOLLOW_ARCHITECT
    raise ValueError(f"no such role: {role!r}")


def role_model(role: str) -> str:
    """Which model actually answers as this role.

    The supervisor's default is `architect`, meaning follow whatever the
    architect is. It resolves HERE rather than at the call site, so the header,
    the settings sheet and the code that makes the call cannot disagree about
    who is answering.
    """
    m = role_choice(role)
    return role_model("architect") if m == FOLLOW_ARCHITECT else m


def role_ready(role: str) -> tuple[bool, str]:
    """Can this role answer right now, and if not, what to do about it.

    The reason matters as much as the boolean: "switched off in Settings" and
    "ChatGPT is not connected" both produce a dark light and want completely
    different things from you.
    """
    if not role_on(role):
        return False, "switched off in Settings"
    return model_ready(role_model(role))


def model_ready(model: str) -> tuple[bool, str]:
    """Can this model answer at all right now, and if not, what to do about it.

    Keyed on the family and not on the role, because "is Claude signed in" has
    one answer however many jobs are pointed at it.
    """
    kind = model_kind(model)
    if kind == "claude":
        if not claude_available():
            return False, "Claude Code is not installed"
        problem = claude_auth_problem()
        return (False, problem) if problem else (True, "")
    if kind == "codex":
        if not codex_available():
            return False, "the codex CLI is not installed"
        if not codex_logged_in():
            return False, "ChatGPT is not connected"
        return True, ""
    if not openrouter_enabled():
        return False, "OpenRouter is switched off in Settings"
    if not find_openrouter_key():
        return False, "no OpenRouter key configured"
    return True, ""


def role_choices(role: str) -> list[dict]:
    """Every model, for every role, with the ones that cannot do the job marked.

    `cannot` and `why_not` are two different failures and are kept apart. A model
    that CANNOT hold the role is a fact about the job and never changes; one that
    could but is not signed in is a fact about today and is fixable. Collapsing
    them into one grey entry would tell you to go and fix something that no
    amount of signing in will fix.
    """
    names = ([FOLLOW_ARCHITECT] if role == "supervisor" else []) + list(MODELS)
    out = []
    for m in names:
        cannot = role_allows(role, m)
        resolved = role_model("architect") if m == FOLLOW_ARCHITECT else m
        ok, why = (False, "") if cannot else model_ready(resolved)
        out.append({
            "value": m,
            "note": (f"follows the architect, currently {resolved}"
                     if m == FOLLOW_ARCHITECT else MODELS[m]["note"]),
            "cannot": cannot,
            "why_not": "" if cannot or ok else why,
        })
    return out


def role_view() -> list[dict]:
    """The three lights, in the order they are read: who does the work, who is
    asked when it gets stuck, who checks it is the right work."""
    out = []
    for r in ROLES:
        ok, why = role_ready(r)
        out.append({
            "role": r,
            "on": role_on(r),
            "model": role_model(r),
            "choice": role_choice(r),
            "choices": role_choices(r),
            "ready": ok,
            "why": why,
        })
    return out


def set_role(role: str, on=None, model=None) -> list[dict]:
    """Change a role, writing through to whichever setting already owned it."""
    if role not in ROLES:
        raise ValueError(f"no such role: {role!r}")
    cfg = config()
    slot = cfg.setdefault("roles", {}).setdefault(role, {})
    if on is not None:
        slot["on"] = bool(on)
    if model is not None:
        # One gate, the same one the dropdown was built from. Re-listing the
        # allowed models here is how a menu and a validator drift apart until
        # the console offers you something the server then refuses.
        cannot = role_allows(role, model)
        if cannot:
            raise ValueError(f"the {role} cannot be {model}: {cannot}")
        if role == "worker":
            cfg["claude_model"] = model
        elif role == "architect":
            kind = model_kind(model)
            cfg["architect_backend"] = kind
            # Written to the setting that family already owned, so the value
            # survives switching away and back. Pinning the architect to codex
            # for an afternoon should not forget which OpenRouter tier you
            # were on.
            if kind == "openrouter":
                cfg["consult_model"] = model
            elif kind == "claude":
                cfg["claude_architect_model"] = model
        else:
            slot["model"] = model
    save_json(CONFIG_PATH, cfg)
    return role_view()


def role_chat(role: str, messages: list[dict], max_tokens: int = 8000) -> dict:
    """One round as a named role, through whichever model that role is set to.

    `architect_chat` dispatches on the one global backend, which is right for
    the architect and wrong for anything else: a supervisor pinned to codex
    while the architect sits on OpenRouter would silently go to OpenRouter and
    spend money the operator had moved away from.
    """
    m = role_model(role)
    kind = model_kind(m)
    if kind == "codex":
        return codex_chat(messages, DEFAULT_CONSULT, max_tokens)
    if kind == "claude":
        return claude_chat(messages, m, max_tokens)
    return openrouter_chat(messages, m, max_tokens)


def lane_backend(lane: dict) -> str:
    return lane.get("backend") or DEFAULT_BACKEND


def lane_or_die(cfg: dict, name: str) -> dict:
    lane = cfg["lanes"].get(name)
    if not lane:
        known = ", ".join(sorted(cfg["lanes"])) or "(none configured)"
        die(f"unknown lane {name!r}. Known lanes: {known}")
    return lane


class LaneError(ValueError):
    """A lane could not be created. The message is meant for a person to read."""


# A lane name becomes a git branch (`amp/<name>`) and a directory under
# .amp/worktrees/, so it has to survive being both. Rejecting a bad one here is
# the difference between "that name has a slash in it" and a git error at
# dispatch time, half an hour later, that reads like a harness bug.
LANE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")


def add_lane(name: str, *, repo: str | None = None, path: str | None = None,
             branch: str = "main", backend: str = DEFAULT_BACKEND,
             env_id: str | None = None, replace: bool = False) -> dict:
    """Register a directory as a lane, checking everything dispatch will need.

    Shared by `./amp lane add` and the console, so a lane made from the chat is
    the same object as one made from the terminal. Every check here is one that
    would otherwise fail later, inside a worker, where the operator cannot see
    it. Raises LaneError, which both callers render their own way.
    """
    name = (name or "").strip()
    if not LANE_NAME_RE.match(name):
        raise LaneError(
            f"{name!r} is not a usable lane name. It becomes a git branch and a "
            "directory, so: lowercase letters, digits, dot, dash or underscore, "
            "starting with a letter or digit, up to 32 characters."
        )
    if backend not in BACKENDS:
        raise LaneError(f"backend must be one of {', '.join(BACKENDS)}")

    cfg = config()
    if name in cfg["lanes"] and not replace:
        old = cfg["lanes"][name]
        raise LaneError(
            f"lane {name!r} already exists ({old.get('repo')} at {old.get('path')}). "
            "Pass replace to repoint it - and note its existing worktree at "
            f"{WORKTREE_DIR / name} is reused as-is, so delete that first if the "
            "repo is changing."
        )

    rel = (path or name).strip().strip("/")
    abs_path = (ROOT / rel).resolve()
    if not abs_path.is_dir():
        raise LaneError(f"no such directory: {abs_path}")
    try:
        rel = str(abs_path.relative_to(ROOT))
    except ValueError:
        raise LaneError(f"{abs_path} is outside the workspace {ROOT}") from None

    # Worktrees are cut from this directory, so it has to be a repository root.
    # A subdirectory would silently cut them from the *parent* repo instead, and
    # the worker would look right while editing the wrong tree.
    p = run(["git", "rev-parse", "--show-toplevel"], cwd=abs_path)
    if p.returncode != 0:
        raise LaneError(f"{abs_path} is not a git repository - a lane needs one "
                        "to cut a worker's worktree from")
    top = Path(p.stdout.strip()).resolve()
    if top != abs_path:
        raise LaneError(f"{abs_path} sits inside the repo at {top}. Point the lane "
                        "at the repository root, or workers would edit that one.")

    if not repo:
        p = run(["git", "remote", "get-url", "origin"], cwd=abs_path)
        url = p.stdout.strip()
        if p.returncode != 0 or not url:
            raise LaneError(f"{abs_path} has no `origin` remote, so the repo name "
                            "cannot be inferred - pass repo as owner/name")
        if url.startswith("git@github.com:"):
            repo = url.split(":", 1)[1].removesuffix(".git")
        elif "github.com/" in url:
            repo = url.split("github.com/", 1)[1].removesuffix(".git")
        else:
            raise LaneError(f"could not read a repo name out of origin {url!r} - "
                            "pass repo as owner/name")

    # The base a worker branches from. Checking it now turns a dispatch-time
    # worktree failure into a sentence about a typo.
    branch = (branch or "main").strip() or "main"
    if run(["git", "rev-parse", "--verify", "--quiet", branch], cwd=abs_path).returncode != 0:
        raise LaneError(f"{repo} has no branch {branch!r} to branch workers off")

    cfg["lanes"][name] = {"repo": repo, "path": rel, "branch": branch,
                          "backend": backend, "env_id": env_id}
    save_json(CONFIG_PATH, cfg)
    lane = dict(cfg["lanes"][name], name=name)
    # A codex lane without an env id is configured but cannot dispatch, and the
    # id only exists in an interactive TUI - so say so rather than let the first
    # dispatch discover it.
    lane["needs_env"] = backend == "codex" and not env_id
    return lane


# ---------------------------------------------------------------- claude bridge


def claude_available() -> bool:
    return shutil.which("claude") is not None


CLAUDE_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"


def claude_token() -> str | None:
    """The harness's own long-lived worker token, if one has been connected."""
    tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if tok:
        return tok.strip()
    tok = load_json(SECRETS_PATH, {}).get("claude_oauth_token")
    return tok.strip() if tok else None


def claude_env() -> dict:
    """Environment for a worker subprocess.

    Workers must not depend on whatever credentials the launching shell happens
    to carry - the console has none. Connecting Claude once stores a long-lived
    token here and every worker gets it explicitly.
    """
    env = dict(os.environ)
    tok = claude_token()
    if tok:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    return env


def claude_auth_problem() -> str | None:
    """Why a background worker would fail to authenticate, or None if it can.

    `claude auth status` is useless here: it answers loggedIn:true with a stored
    OAuth token the API rejects with a 401, and the rejection takes ~3 minutes of
    retries to surface. The stored expiry is the honest signal, and it is free.
    """
    if claude_token() or os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        oauth = json.loads(CLAUDE_CREDENTIALS.read_text()).get("claudeAiOauth") or {}
    except (OSError, json.JSONDecodeError):
        return "Claude is not connected - click Connect Claude"
    exp = oauth.get("expiresAt")
    if not isinstance(exp, (int, float)):
        return "Claude credentials carry no expiry - click Connect Claude"
    stale = time.time() - exp / 1000
    if stale > 0:
        return (f"Claude sign-in expired {stale / 86400:.0f} days ago "
                f"- click Connect Claude")
    return None


# ------------------------------------------------------- connecting the worker

TOKEN_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")
# OpenAI keys and any JWT, for the codex pty. The device flow is not supposed to
# print either - the CLI writes its own credential file - but "not supposed to"
# is the assumption that leaked a token here once already.
OAI_SECRET_RE = re.compile(
    r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}"
    r"|\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"
)
AUTH_URL_RE = re.compile(r"https://\S*oauth/authorize\S*")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[>=\]][0-9;]*[a-zA-Z]?|\r")
# The TUI writes runs of spaces as cursor-forward moves. Deleting those glues
# words together ("makesure the fullcde"), so replace them with real spaces.
CUF_RE = re.compile(r"\x1b\[([0-9]*)C")


def strip_ansi(s: str) -> str:
    s = CUF_RE.sub(lambda m: " " * max(1, int(m.group(1) or 1)), s)
    return ANSI_RE.sub("", s)


def redact(s: str) -> str:
    """Never surface the pty buffer raw - the token is printed in it.

    Diagnostics get shown in the console, written to logs and pasted into GPT-5.6
    packets, so a token that reaches an error string reaches all three. Both
    vendors' shapes are covered because both sign-ins now run through here, and
    a redactor that only knows one vendor is a redactor that fails silently the
    first time the other one prints something.
    """
    s = TOKEN_RE.sub("sk-ant-<redacted>", s)
    return OAI_SECRET_RE.sub("<redacted>", s)


def last_message(buf: str, n: int = 2) -> str:
    """The last few meaningful lines of TUI output, for an error message."""
    lines = [ln.strip() for ln in redact(buf).splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def pty_read(fd: int, buf: str, deadline: float, done, idle: float = 0.5) -> str:
    """Read a TUI's pty into `buf` until `done(buf)` or the deadline.

    Neither sign-in closes its stdout while it is waiting, so there is no EOF to
    read to: the caller says what it is waiting for and how long it will wait.
    """
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], idle)
        if not r:
            continue
        try:
            chunk = os.read(fd, 65536)
        except OSError:      # the child exited and the pty went away
            break
        if not chunk:
            break
        buf += strip_ansi(chunk.decode("utf8", "replace"))
        if done(buf):
            break
    return buf


def pty_close(pid: int | None, fd: int | None):
    """Stop a sign-in child and drop its pty, tolerating either already gone."""
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, os.WNOHANG)
        except OSError:
            pass
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


class ClaudeLogin:
    """Drives `claude setup-token` so sign-in can happen in the console.

    `setup-token` is a TUI: it paints an authorize URL, waits for the user to
    paste the code back, then prints a long-lived token. Two details make it
    scriptable - it needs a pty (it will not talk to a pipe), and the pty must
    be very wide or it hard-wraps the URL into unusable fragments.
    """

    WIDTH = 400
    IDLE = 0.5

    def __init__(self):
        self.pid: int | None = None
        self.fd: int | None = None
        self.buf = ""
        self.url: str | None = None
        self.token: str | None = None

    def _drain(self, deadline: float, done) -> str:
        self.buf = pty_read(self.fd, self.buf, deadline, done, self.IDLE)
        return self.buf

    def start(self, timeout: float = 45.0) -> str:
        """Launch the flow and return the URL the user must open."""
        if not claude_available():
            raise RuntimeError("claude CLI not found (npm i -g @anthropic-ai/claude-code)")
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            env = dict(os.environ)
            # A stale ambient credential would make setup-token skip the flow.
            for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                      "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
                env.pop(k, None)
            # Point the CLI's own auto-open at Chrome; without this it follows
            # BROWSER/xdg and lands in firefox on this box.
            chrome = browser_cmd()
            if chrome:
                env["BROWSER"] = chrome
            os.execvpe("claude", ["claude", "setup-token"], env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, self.WIDTH, 0, 0))
        self._drain(time.time() + timeout, AUTH_URL_RE.search)
        m = AUTH_URL_RE.search(self.buf)
        if not m:
            self.close()
            raise RuntimeError("claude setup-token did not offer a sign-in URL:\n"
                               + last_message(self.buf, 4))
        self.url = m.group(0)
        return self.url

    # A rejected code makes the TUI re-draw its prompt instead of exiting, so
    # waiting for the token alone would burn the whole timeout on a typo.
    REPROMPT = re.compile(r"Paste code here|Invalid|invalid|failed|error", re.I)

    def submit(self, code: str, timeout: float = 60.0) -> str:
        """Hand the pasted code back and return the long-lived token."""
        if self.fd is None:
            raise RuntimeError("sign-in was not started")
        self.buf = ""
        os.write(self.fd, code.strip().encode() + b"\r")
        self._drain(time.time() + timeout,
                    lambda b: TOKEN_RE.search(b) or self.REPROMPT.search(b))
        m = TOKEN_RE.search(self.buf)
        if not m:
            self.close()
            raise RuntimeError(self._reason() or
                               "that code was not accepted - try Connect Claude again")
        self.token = m.group(0)
        self.close()
        return self.token

    def _reason(self) -> str | None:
        """The CLI's own complaint, preferred over the trailing 'Press Enter' noise."""
        for ln in redact(self.buf).splitlines():
            s = " ".join(ln.split())
            if s.lower().startswith("oauth error") or "invalid code" in s.lower():
                return s
        return None

    def close(self):
        pty_close(self.pid, self.fd)
        self.pid, self.fd = None, None


def store_claude_token(token: str):
    """Untracked, same file as the OpenRouter key."""
    s = load_json(SECRETS_PATH, {})
    s["claude_oauth_token"] = token.strip()
    save_json(SECRETS_PATH, s)
    SECRETS_PATH.chmod(0o600)


class CodexLogin:
    """Drives `codex login --device-auth` so ChatGPT sign-in can happen here.

    Deliberately not the same shape as ClaudeLogin, because the CLI is not: this
    flow prints a URL *and* a one-time code, the operator types the code into the
    page, and the CLI polls OpenAI itself and exits once it is approved. So the
    second call is a poll, not a submission - there is nothing to paste back, and
    an endpoint that asked for a code would be asking for the wrong thing.

    Whether it worked is decided by `codex login status`, not by the text in the
    buffer. The buffer says what the CLI claims; the status says what is on disk.
    """

    WIDTH = 200
    IDLE = 0.5
    TTL = 15 * 60          # the CLI states the code expires in 15 minutes

    URL_RE = re.compile(r"https://\S*openai\.com/\S*device\S*")
    # The pairing code, e.g. ABCD-EFGHI. Shown to the operator on purpose: it is
    # useless to anyone who does not already hold their browser session, and it
    # is never stored anywhere.
    CODE_RE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,8}\b")

    def __init__(self):
        self.pid: int | None = None
        self.fd: int | None = None
        self.buf = ""
        self.url: str | None = None
        self.code: str | None = None
        self.started = 0.0

    def start(self, timeout: float = 45.0) -> dict:
        """Launch the flow and return the URL and code the operator needs."""
        if not codex_available():
            raise RuntimeError("codex CLI not found (npm i -g @openai/codex)")
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            env = dict(os.environ)
            # An ambient key would let the CLI answer from the environment
            # instead of running the flow that was actually asked for.
            for k in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
                env.pop(k, None)
            # Chrome, for the same reason as the Claude flow: BROWSER/xdg lands
            # in firefox on this box.
            chrome = browser_cmd()
            if chrome:
                env["BROWSER"] = chrome
            os.execvpe("codex", ["codex", "login", "--device-auth"], env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, self.WIDTH, 0, 0))
        self.buf = pty_read(
            self.fd, self.buf, time.time() + timeout,
            lambda b: self.URL_RE.search(b) and self.CODE_RE.search(b), self.IDLE,
        )
        url, code = self.URL_RE.search(self.buf), self.CODE_RE.search(self.buf)
        if not (url and code):
            self.close()
            raise RuntimeError("codex login did not offer a device code:\n"
                               + last_message(self.buf, 4))
        self.url, self.code = url.group(0), code.group(0)
        self.started = time.time()
        return {"url": self.url, "code": self.code}

    def poll(self) -> dict:
        """Where the flow has got to: pending, connected, or failed and why."""
        if self.fd is None:
            return {"state": "failed", "error": "sign-in is not running"}
        # Keep reading, so a complaint printed while we were not looking is in
        # the buffer by the time it is needed for the error message.
        self.buf = pty_read(self.fd, self.buf, time.time() + 0.2, lambda b: False, 0.1)
        try:
            done, _ = os.waitpid(self.pid, os.WNOHANG)
        except (OSError, TypeError):
            done = -1
        if done == 0:
            if time.time() - self.started > self.TTL:
                self.close()
                return {"state": "failed", "error": "that code expired - start again"}
            return {"state": "pending", "url": self.url, "code": self.code}
        self.pid = None          # reaped already; close() must not wait on it
        self.close()
        # `fresh`, because the sign-in we are reporting on finished a moment ago
        # and the cached answer is from before it. Telling someone who has just
        # signed in that they are not signed in is the whole failure mode a
        # cache like that introduces.
        if codex_logged_in(fresh=True):
            return {"state": "connected"}
        return {"state": "failed",
                "error": self._reason() or "sign-in did not complete - try again"}

    def _reason(self) -> str | None:
        """The CLI's own complaint, preferred over an exit code nobody can read."""
        for ln in reversed(redact(self.buf).splitlines()):
            s = " ".join(ln.split())
            if re.match(r"(error|failed)\b", s, re.I) or \
               re.search(r"\b(expired|denied|timed out|rejected)\b", s, re.I):
                return s
        return None

    def close(self):
        pty_close(self.pid, self.fd)
        self.pid, self.fd = None, None


# ---------------------------------------------------------------- supervision


class Killed(Exception):
    """A worker was stopped: by you, by the clock, or by the memory watchdog."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


_PAGE = os.sysconf("SC_PAGE_SIZE")
_CLK_TCK = os.sysconf("SC_CLK_TCK")

_PROCS: dict[str, subprocess.Popen] = {}
_STARTED: dict[str, float] = {}
_CANCELLED: set[str] = set()
# What each watchdog last saw. The watchdogs already tick every two seconds, so
# they do the sampling and the console reads their answer - two samplers taking
# CPU deltas off the same processes would each see a fraction of the truth.
_ACTIVITY: dict[str, dict] = {}
_PROCS_LOCK = threading.Lock()


def limits() -> dict:
    """How much of this machine a single worker is allowed to take."""
    cfg = config()
    return {
        "timeout_s": int(cfg.get("worker_timeout_s", DEFAULT_WORKER_TIMEOUT_S)),
        "mem_gb": float(cfg.get("worker_mem_gb", DEFAULT_WORKER_MEM_GB)),
        "nice": int(cfg.get("worker_nice", DEFAULT_WORKER_NICE)),
        "stall_s": int(cfg.get("worker_stall_s", DEFAULT_WORKER_STALL_S)),
        "max_workers": int(cfg.get("max_workers", DEFAULT_MAX_WORKERS)),
        "permission_mode": str(cfg.get("permission_mode", DEFAULT_PERMISSION_MODE)),
    }


def live_workers() -> int:
    """Workers this harness has running right now, across every lane.

    The orchestrator is not one of them. It is the control surface, so counting
    it means the board can only ever fill `cap - 1` slots, and it takes the last
    one itself at the exact moment it is trying to hand out work.

    Adopted workers ARE counted. They survived a console restart, they are still
    burning a budget and still on the machine, and a cap that cannot see them is
    not a cap - it would wave through a full board on top of them.
    """
    with _PROCS_LOCK:
        tids = set(_PROCS) | set(_ADOPTED)
        return sum(1 for tid in tids if not tid.startswith("orch:"))


def live_worker_lanes() -> list[str]:
    """Which lanes are holding a live worker right now.

    `live_workers` answers how many, which is what a cap needs. A refusal needs
    to say which, because a count is not something anyone can act on.
    """
    with _PROCS_LOCK:
        tids = {t for t in set(_PROCS) | set(_ADOPTED) if not t.startswith("orch:")}
    if not tids:
        return []
    return sorted(
        lane for lane, recs in (board().get("tasks") or {}).items()
        if any(r.get("task_id") in tids for r in recs)
    )


def _proc_table() -> dict[int, dict]:
    """One pass over /proc: ppid, pgrp, resident bytes and CPU ticks per pid."""
    table = {}
    try:
        entries = list(os.scandir("/proc"))
    except OSError:
        return table
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat = Path(entry.path, "stat").read_text()
            # comm is parenthesised and may contain spaces, so fields are read
            # from after the last ')': [0]=state, [1]=ppid, [2]=pgrp,
            # [11]=utime, [12]=stime, [21]=rss.
            f = stat[stat.rindex(")") + 2:].split()
            table[int(entry.name)] = {
                "ppid": int(f[1]), "pgrp": int(f[2]),
                "rss": int(f[21]) * _PAGE,
                "ticks": int(f[11]) + int(f[12]),
            }
        except (OSError, ValueError, IndexError):
            continue
    return table


def worker_procs(root: int, table: dict[int, dict] | None = None) -> set[int]:
    """Every pid belonging to a worker: its descendants, and their groups.

    Not just its process group. `supervise` starts each worker with
    `start_new_session=True` and the comment used to say that made a kill reach
    the whole tree - but Claude Code's Bash tool detaches every shell it runs
    into a session of its own, so a worker's compiler, test run and benchmark
    all sit in process groups this one knows nothing about. Weighing only the
    group meant weighing `claude` and its MCP servers: a worker burning two
    whole cores on `ic32 --test` reported 1% CPU, which is how the stall
    watchdog came to kill a busy worker and write "no output and no CPU" on the
    board about a machine that was pinned.

    So the tree is walked by parentage, and then widened to whole process groups
    so that a child which has already outlived its shell still counts. What this
    still cannot see is a group that left the tree entirely before the sample -
    every member reparented to init with no survivor to name it. Killing by
    group rather than by pid is what keeps that from happening in the first
    place.
    """
    table = _proc_table() if table is None else table
    kids: dict[int, list[int]] = {}
    for pid, p in table.items():
        kids.setdefault(p["ppid"], []).append(pid)

    seen, stack = set(), [root]
    while stack:
        pid = stack.pop()
        if pid in seen or pid not in table:
            continue
        seen.add(pid)
        stack.extend(kids.get(pid, ()))

    groups = {table[pid]["pgrp"] for pid in seen} | {root}
    return seen | {pid for pid, p in table.items() if p["pgrp"] in groups}


def group_stat(root: int) -> dict:
    """Resident bytes, CPU seconds and process count across a worker's tree.

    None of it is ever about `claude` itself - the memory and the CPU are in the
    compiler, the benchmark, or the MCP servers it spawned, so everything it
    started is what gets weighed. CPU is here because it is the only cheap
    reading that distinguishes a worker doing slow work from a worker doing none.
    """
    table = _proc_table()
    pids = worker_procs(root, table)
    rss = sum(table[p]["rss"] for p in pids if p in table)
    ticks = sum(table[p]["ticks"] for p in pids if p in table)
    return {"rss": rss, "cpu_s": ticks / _CLK_TCK, "procs": len(pids)}


def group_rss(root: int) -> int:
    """Resident bytes across everything the worker is running."""
    return group_stat(root)["rss"]


def _alive(pgid: int) -> bool:
    return Path(f"/proc/{pgid}").exists()


def worker_pgids(root: int) -> list[int]:
    """Every process group the worker has anything running in, its own last.

    One `killpg` on the worker's own group is not enough and never was: the
    shells Claude Code runs sit in sessions of their own, so a benchmark or a
    compile survives the kill, keeps a core, and is still there long after the
    board says the task is over.

    Its own group sorts last so a sweep cannot kill the worker first and orphan
    the rest of the tree it was about to walk.
    """
    table = _proc_table()
    groups = {table[p]["pgrp"] for p in worker_procs(root, table) if p in table}
    groups.add(root)
    return sorted(groups, key=lambda g: (g == root, g))


def _signal_groups(groups: list[int], sig: int) -> int:
    sent = 0
    for pgid in groups:
        try:
            os.killpg(pgid, sig)
            sent += 1
        except (ProcessLookupError, PermissionError):
            continue
    return sent


def _kill_pgid(root: int, wait):
    """Take down a worker and everything it spawned, politely then not.

    The groups are read once, before anything is signalled - both because
    signalling as you walk tears down the parentage the walk is reading, and
    because by the time the worker has exited there is nothing left to walk
    from, and its detached shells would go unfound at exactly the moment they
    became orphans.
    """
    groups = worker_pgids(root)
    if not _signal_groups(groups, signal.SIGTERM):
        return
    wait()
    _signal_groups(groups, signal.SIGKILL)


def _kill_group(proc: subprocess.Popen):
    def wait():
        try:
            proc.wait(timeout=10)
            return True
        except subprocess.TimeoutExpired:
            return False
    _kill_pgid(proc.pid, wait)


def _kill_adopted(pgid: int):
    """An adopted worker is not ours to wait() on, so poll /proc instead."""
    def wait():
        for _ in range(20):
            if not _alive(pgid):
                return True
            time.sleep(0.5)
        return False
    _kill_pgid(pgid, wait)


def cancel_worker(task_id: str) -> bool:
    """Stop a running worker on demand. False if it is not running here."""
    with _PROCS_LOCK:
        proc = _PROCS.get(task_id)
        pgid = _ADOPTED.get(task_id)
        if proc or pgid:
            _CANCELLED.add(task_id)
    if proc:
        _kill_group(proc)
        return True
    if pgid:
        _kill_adopted(pgid)
        return True
    return False


# A worker's transcript is the only place it says what it is doing, and it
# writes it as it goes - so a live session can simply be read. These map the
# tool it last reached for onto a word for the kind of work that is.
_PHASES = {
    "Bash": "running", "Edit": "editing", "Write": "writing", "NotebookEdit": "editing",
    "Read": "reading", "Grep": "searching", "Glob": "searching",
    "WebFetch": "fetching", "WebSearch": "searching", "Task": "delegating",
    "TodoWrite": "planning", "AskUserQuestion": "waiting on an answer",
}
# Commands that mean the worker is doing the slow thing on purpose. A worker
# inside one of these is expected to go quiet for a long time.
_LONG_RUNNING = ("bench", "test", "gcc", "cc ", "clang", "make", "cargo", "mix ",
                 "npm ", "pytest", "python", "cmake", "go build", "zig ")


def _tail_records(path: Path, nbytes: int = 64_000) -> list[dict]:
    """Parsed records from the end of a transcript, without reading the file.

    These grow into the hundreds of megabytes, and the console asks every few
    seconds. Only the tail can answer "what is it doing right now" anyway.
    """
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > nbytes:
            fh.seek(size - nbytes)
            buf = fh.read().split(b"\n", 1)[-1]   # the seek lands mid-line
        else:
            buf = fh.read()
    out = []
    for line in buf.decode("utf-8", "replace").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def worker_activity(session_id: str) -> dict:
    """What a running worker is doing, read from the transcript it is writing."""
    path = transcript_path(session_id)
    if not path:
        # Claude Code writes the file on its first turn, so this is startup -
        # or, much later, a worker that never got far enough to say anything.
        return {"phase": "starting", "doing": "", "quiet_s": 0, "turns": 0}
    quiet_s = int(max(0.0, time.time() - path.stat().st_mtime))
    events = [e for rec in _tail_records(path) for e in record_events(rec)]
    turns = sum(1 for e in events if e["kind"] == "tool")
    # Only what the worker itself said. The user side of a transcript carries
    # tool notifications and system reminders, which read as the worker
    # narrating machinery it never chose to run.
    last = next((e for e in reversed(events)
                 if e["kind"] == "tool"
                 or (e["kind"] == "text" and e.get("role") == "assistant")), None)
    if not last:
        return {"phase": "thinking", "doing": "", "quiet_s": quiet_s, "turns": turns}
    if last["kind"] == "text":
        return {"phase": "thinking", "doing": " ".join(last["text"].split())[:120],
                "quiet_s": quiet_s, "turns": turns}
    text = last.get("text") or ""
    phase = _PHASES.get(last.get("tool"), last.get("tool") or "working")
    long_running = (last.get("tool") == "Bash"
                    and any(k in text.lower() for k in _LONG_RUNNING))
    return {"phase": phase, "doing": text[:160], "tool": last.get("tool"),
            "quiet_s": quiet_s, "turns": turns, "long_running": long_running}


def worker_stats() -> dict[str, dict]:
    """What each live worker is costing the machine, and what it is doing."""
    with _PROCS_LOCK:
        tids = list(_PROCS) + [t for t in _ADOPTED if t not in _PROCS]
        seen = {t: dict(_ACTIVITY.get(t) or {}) for t in tids}
    lim = limits()
    out = {}
    for tid in tids:
        s = seen.get(tid) or {}
        sid = s.get("session_id") or tid
        act = worker_activity(sid)
        # The watchdog's own reading of quiet time is what it kills on, but it
        # only samples every two seconds; the transcript mtime is exact.
        out[tid] = {
            "rss_gb": round(s.get("rss", 0) / 1e9, 2),
            "cpu_pct": round(s.get("cpu_pct", 0.0), 1),
            "procs": s.get("procs", 0),
            "elapsed_s": int(time.time() - _STARTED.get(tid, time.time())),
            "mem_gb": lim["mem_gb"],
            "timeout_s": lim["timeout_s"],
            "stall_s": lim["stall_s"],
            "adopted": tid in _ADOPTED,
            "stalled": bool(s.get("cold_s", 0) >= lim["stall_s"]),
            **act,
        }
    return out


def _sample(task_id: str, pgid: int, session_id: str, prev: dict, dt: float) -> dict:
    """One watchdog tick: how hot the group is, and how long it has been mute.

    `cold_s` is the reading that matters - time during which the worker wrote
    nothing AND burned no CPU. It resets the moment either one moves, so a long
    compile (silent, hot) and a long think (brief, then loud) both keep it at
    zero. Only a worker that is doing nothing at all lets it climb.
    """
    g = group_stat(pgid)
    cpu_pct = max(0.0, (g["cpu_s"] - prev.get("cpu_s", g["cpu_s"])) / dt * 100.0) if dt else 0.0
    path = transcript_path(session_id)
    mtime = path.stat().st_mtime if path and path.exists() else 0.0
    wrote = mtime > prev.get("mtime", 0.0)
    cold = 0.0 if (wrote or cpu_pct >= IDLE_CPU_PCT) else prev.get("cold_s", 0.0) + dt
    return {"rss": g["rss"], "cpu_s": g["cpu_s"], "cpu_pct": cpu_pct,
            "procs": g["procs"], "mtime": max(mtime, prev.get("mtime", 0.0)),
            "cold_s": cold, "session_id": session_id}


def supervise(argv: list[str], cwd: Path, env: dict, *, task_id: str,
              timeout_s: int, mem_gb: float, nice_by: int,
              session_id: str = "", stall_s: int = 0) -> subprocess.CompletedProcess:
    """Run a worker so that it cannot take the machine down with it.

    Its own session, lowered priority (so a runaway build never starves the
    desktop), a wall clock, an RSS watchdog, and a stall watchdog. Raises Killed
    if any of those stopped it.

    The session is not what makes a kill reach the whole tree - it used to say
    so here and it was wrong. Claude Code detaches every shell it runs into a
    session of its own, so both the weighing and the killing walk the process
    tree instead; see `worker_procs`.
    """
    # `nice` rather than a preexec_fn: this runs inside a threaded server, where
    # forking with a Python callback can deadlock.
    nicer = shutil.which("nice")
    cmd = [nicer, "-n", str(nice_by), *argv] if nicer and nice_by else list(argv)

    proc = subprocess.Popen(
        cmd, cwd=str(cwd), env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    with _PROCS_LOCK:
        _PROCS[task_id] = proc
        _STARTED[task_id] = time.time()
        _CANCELLED.discard(task_id)

    mem_bytes = int(mem_gb * 1e9)
    stop, breach = threading.Event(), []

    def watch():
        deadline = time.monotonic() + timeout_s
        sid = session_id or task_id
        seen, last = {}, time.monotonic()
        while not stop.wait(2.0):
            tick = time.monotonic()
            seen = _sample(task_id, proc.pid, sid, seen, tick - last)
            last = tick
            with _PROCS_LOCK:
                _ACTIVITY[task_id] = seen
            if tick > deadline:
                span = f"{timeout_s // 60} min" if timeout_s >= 60 else f"{timeout_s}s"
                breach.append(f"stopped after {span} - over its time limit")
            elif mem_bytes and seen["rss"] > mem_bytes:
                breach.append(f"stopped - it and its children held more than {mem_gb:g} GB")
            elif stall_s and seen["cold_s"] >= stall_s:
                # Not a timeout: it had time left and was using none of it.
                breach.append(f"stopped - hung for {int(seen['cold_s']) // 60} min "
                              "with no output and no CPU")
            else:
                continue
            _kill_group(proc)
            return

    threading.Thread(target=watch, daemon=True).start()
    try:
        out, err = proc.communicate()
    finally:
        stop.set()
        with _PROCS_LOCK:
            _PROCS.pop(task_id, None)
            _STARTED.pop(task_id, None)
            _ACTIVITY.pop(task_id, None)
            cancelled = task_id in _CANCELLED
            _CANCELLED.discard(task_id)

    if cancelled:
        raise Killed("cancelled")
    if breach:
        raise Killed(breach[0])
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


# ---------------------------------------------------------------- adoption
#
# Workers run in their own session so that killing one reaches its whole tree.
# The cost of that is they also survive the console: restart it and every live
# worker reparents to init, keeping its budget and losing its watchdog, while
# its board record says `running` until someone notices. That is not a rare
# case - the console gets restarted for every change to this file. So on boot
# the board is reconciled against /proc: what is still alive gets picked back
# up, and what is not gets settled.

_ADOPTED: dict[str, int] = {}

# An adopted worker finishing frees a slot exactly like any other, and the queue
# that wants that slot lives in the console. amp.py cannot import server.py, so
# the console hands its drain in here. Without it, a queue could sit full behind
# workers that were picked up rather than started.
ON_SETTLE = None   # set by the console to its queue drain

# An adopted worker is one this process did not start, so nothing here ran the
# after-the-worker step: reporting back to the architect that sent it, or
# escalating if it came back blocked. Without this hook, a thread that sent a
# worker for evidence waits for a report that already happened and never moves
# again. The console sets it to the same settle path a worker it started uses.
ON_WORKER_DONE = None   # set by the console; called (lane_name, record)


def adopted_task_ids() -> set[str]:
    with _PROCS_LOCK:
        return set(_ADOPTED)


def _settled(lane_name: str | None = None, rec: dict | None = None):
    if ON_WORKER_DONE and lane_name and rec:
        try:
            ON_WORKER_DONE(lane_name, rec)
        except Exception as e:
            print(f"amp: worker-done hook failed: {e}", file=sys.stderr)
    if ON_SETTLE:
        try:
            ON_SETTLE()
        except Exception as e:
            print(f"amp: settle hook failed: {e}", file=sys.stderr)


def _pgid_for_session(session_id: str) -> int | None:
    """The live worker running this session, by the id in its own argv.

    Matching on argv rather than a recorded pid because a pid recorded before a
    restart may since have been reused by something else entirely.

    Both spellings, because a worker carries its session id under `--session-id`
    when it is new and under `--resume` when it is continued, and looking only
    for the first one made every resumed worker invisible here. What that looked
    like on the board was not an error: the record was settled "the worker did
    not survive it", the lane's slot was freed, and the queue dispatched a fresh
    worker onto the same worktree - while the one that had supposedly not
    survived was still running in it. Two workers in one working tree is the one
    thing the lane lock exists to prevent, and this was the way around it.
    """
    needles = [f"{flag}\0{session_id}".encode()
               for flag in ("--session-id", "--resume")]
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            argv = Path(entry.path, "cmdline").read_bytes()
        except OSError:
            continue
        if any(n in argv for n in needles) and b"claude" in argv.split(b"\0", 1)[0]:
            return int(entry.name)
    return None


def _settle_adopted(lane_name: str, rec: dict, reason: str | None):
    """Close out a worker whose exit we watched but whose result we never got.

    An adopted worker's stdout went to a pipe that died with the old console,
    so the JSON summary is gone for good. What it said last is still in the
    transcript, and that is reported as what it is - no cost, no turn count,
    marked `adopted` so nothing downstream reads it as a clean result.
    """
    sid = rec.get("resume_of") or rec["task_id"]
    events = transcript(sid)
    last = next((e["text"] for e in reversed(events)
                 if e["kind"] == "text" and e.get("role") == "assistant"), "")
    patch = {"finished_at": now(), "adopted": True, "session_id": sid}
    if reason:
        patch.update(status="failed", error=reason, killed=reason)
    elif last:
        patch.update(status="completed", result=last,
                     note="picked up after a console restart - cost unknown")
    else:
        patch.update(status="failed",
                     error="the console restarted while this ran, and it left nothing behind")
    update_task(lane_name, rec["task_id"], patch)
    _settled(lane_name, rec)


def _watch_adopted(lane_name: str, rec: dict, pgid: int):
    """The watchdog an adopted worker lost, reattached."""
    task_id, sid = rec["task_id"], rec.get("resume_of") or rec["task_id"]
    lim = limits()
    mem_bytes = int(lim["mem_gb"] * 1e9)
    started = _STARTED.get(task_id, time.time())
    # How long it has been mute is a recorded fact - the transcript's mtime -
    # so an adopted worker does not get a fresh fifteen minutes of silence just
    # because the console was restarted. How busy it has been is not recorded
    # anywhere, so the first tick is a warm-up: it establishes a CPU baseline,
    # and a worker that turns out to be mid-build clears the seeded time on it.
    path = transcript_path(sid)
    quiet = time.time() - path.stat().st_mtime if path and path.exists() else 0.0
    seen = {"mtime": path.stat().st_mtime if path and path.exists() else 0.0,
            "cold_s": max(0.0, min(quiet, time.time() - started))}
    last, reason, warm = time.monotonic(), None, 0
    while True:
        time.sleep(2.0)
        if not _alive(pgid):
            break
        tick = time.monotonic()
        seen = _sample(task_id, pgid, sid, seen, tick - last)
        last = tick
        warm += 1
        with _PROCS_LOCK:
            _ACTIVITY[task_id] = seen
            if task_id in _CANCELLED:
                reason = "cancelled"
                break
        if warm < 2:
            continue
        if time.time() - started > lim["timeout_s"]:
            reason = f"stopped after {lim['timeout_s'] // 60} min - over its time limit"
        elif mem_bytes and seen["rss"] > mem_bytes:
            reason = f"stopped - it and its children held more than {lim['mem_gb']:g} GB"
        elif lim["stall_s"] and seen["cold_s"] >= lim["stall_s"]:
            reason = (f"stopped - hung for {int(seen['cold_s']) // 60} min "
                      "with no output and no CPU")
        if reason:
            _kill_adopted(pgid)
            break
    with _PROCS_LOCK:
        _ADOPTED.pop(task_id, None)
        _STARTED.pop(task_id, None)
        _ACTIVITY.pop(task_id, None)
        cancelled = task_id in _CANCELLED
        _CANCELLED.discard(task_id)
    _settle_adopted(lane_name, rec, "cancelled" if cancelled else reason)


def adopt_orphans() -> list[dict]:
    """Reconcile every `running` board record against what is actually alive."""
    out = []
    for lane_name, tasks in (board().get("tasks") or {}).items():
        for rec in tasks:
            if rec.get("status") != "running" or rec.get("backend") != "claude":
                continue
            task_id = rec["task_id"]
            with _PROCS_LOCK:
                if task_id in _PROCS or task_id in _ADOPTED:
                    continue
            pgid = _pgid_for_session(rec.get("resume_of") or task_id)
            if not pgid:
                _settle_adopted(lane_name, rec,
                                "the console restarted while this was running "
                                "and the worker did not survive it")
                out.append({"lane": lane_name, "task_id": task_id, "adopted": False})
                continue
            with _PROCS_LOCK:
                _ADOPTED[task_id] = pgid
                # Its real start is on the board; the wall clock has been
                # running the whole time the console was not watching.
                _STARTED[task_id] = _epoch(rec.get("dispatched_at")) or time.time()
            threading.Thread(target=_watch_adopted, args=(lane_name, rec, pgid),
                             daemon=True).start()
            out.append({"lane": lane_name, "task_id": task_id, "adopted": True, "pid": pgid})
    return out


def _epoch(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0


def claude_worktree(lane_name: str, lane: dict, branch: str) -> Path:
    """The isolated tree a lane's claude worker edits.

    Workers never touch the shared checkout: interactive sessions run against it
    constantly, and a background writer there would corrupt live work.
    """
    wt = WORKTREE_DIR / lane_name
    if wt.exists():
        return wt
    repo = (ROOT / lane["path"]).resolve()
    wt.parent.mkdir(parents=True, exist_ok=True)
    p = run(["git", "worktree", "add", "-B", f"amp/{lane_name}", str(wt), branch], cwd=repo)
    if p.returncode != 0:
        die(f"could not create a worktree for {lane_name}:\n{(p.stderr or p.stdout).strip()}")
    return wt


def claude_turn(prompt: str, cwd: Path, *, model: str, budget: float, session_id: str,
                task_id: str, resume: bool, system: str | None = None) -> dict:
    """One headless claude turn, supervised.

    Resuming is what Codex Cloud cannot do: it answers a worker that stopped to
    ask, in the session it stopped in.
    """
    lim = limits()
    argv = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--model", model,
        "--max-budget-usd", f"{budget:g}",
        "--permission-mode", lim["permission_mode"],
        "--resume" if resume else "--session-id", session_id,
    ]
    # Appended rather than sent as a first message: it has to hold on every
    # resumed turn too, and a resumed turn sends only what you just typed.
    if system:
        argv += ["--append-system-prompt", system]
    try:
        p = supervise(argv, cwd, claude_env(), task_id=task_id,
                      timeout_s=lim["timeout_s"], mem_gb=lim["mem_gb"], nice_by=lim["nice"],
                      session_id=session_id, stall_s=lim["stall_s"])
    except Killed as k:
        return {"status": "cancelled" if k.reason == "cancelled" else "failed",
                "session_id": session_id, "error": k.reason, "killed": k.reason}
    try:
        d = json.loads(p.stdout)
    except json.JSONDecodeError:
        err = (p.stderr or p.stdout).strip()[:4000]
        return {"status": "failed", "error": err or f"claude exited {p.returncode}"}
    failed = bool(d.get("is_error")) or p.returncode != 0
    status = "failed" if failed else "completed"
    error = d.get("result") if failed else None
    killed = None
    budget_hit = False
    # A worker that ran out of money did not finish, whatever the exit code says.
    # The CLI reports a budget stop as a clean exit, so calling it "completed" is
    # how work gets quietly dropped: the record then looks exactly like a worker
    # that finished and reported, which is the one thing it is not.
    #
    # The tell is an EMPTY RESULT. A worker that genuinely finishes says what it
    # did; one that is cut off says nothing, because saying it was going to be
    # the next thing it did. This deliberately does not test `stop_reason`: the
    # first version of this check only caught `tool_use`, and on the real board
    # that missed a worker which burned $1.04 of a $1.00 cap over 33 turns and
    # reported nothing under `end_turn`. Eleven of thirteen real tasks stopped on
    # their cap and all of them were filed as successes.
    spent, cap = float(d.get("total_cost_usd") or 0), float(budget or 0)
    if not failed and not (d.get("result") or "").strip():
        on_cap = bool(cap) and spent >= cap * 0.9
        killed = (f"stopped after {d.get('num_turns') or '?'} turns with nothing "
                  "reported" + (f" - it spent ${spent:.2f} of its ${cap:.2f} cap"
                                if on_cap else ""))
        status, error = "failed", killed
    elif not failed and cap and spent >= cap * 0.9:
        # It did report, but it was on the cap when it did, so what it says may
        # be a summary of an unfinished job rather than a finished one. Not a
        # failure - it spoke - but the board should not present it as a clean
        # finish either, and `budget_hit` is what the console reads to say so.
        budget_hit = True
    return {
        "status": status,
        "session_id": d.get("session_id") or session_id,
        "result": d.get("result"),
        "cost_usd": d.get("total_cost_usd"),
        "num_turns": d.get("num_turns"),
        "duration_ms": d.get("duration_ms"),
        "stop_reason": d.get("stop_reason"),
        "error": error,
        "killed": killed,
        "budget_hit": budget_hit,
    }


def start_claude_task(
    lane_name: str,
    lane: dict,
    prompt: str,
    *,
    branch: str,
    model: str,
    budget: float,
    isolate: bool = True,
    resume_of: str | None = None,
) -> dict:
    """Put a `running` record on the board and return it. Does not execute."""
    if not claude_available():
        die("claude CLI not found (npm i -g @anthropic-ai/claude-code)")
    cwd = claude_worktree(lane_name, lane, branch) if isolate else (ROOT / lane["path"])
    rec = {
        "task_id": str(uuid.uuid4()),
        "backend": "claude",
        "status": "running",
        "dispatched_at": now(),
        "prompt": prompt,
        "branch": branch,
        "model": model,
        "budget_usd": budget,
        "cwd": str(cwd),
        "isolated": isolate,
        "resume_of": resume_of,
    }
    record_task(lane_name, rec)
    return rec


def run_claude_task(lane_name: str, rec: dict) -> dict:
    """Execute a started record and settle it on the board."""
    problem = claude_auth_problem()
    if problem:
        # Without this the worker spends ~3 minutes retrying into a 401.
        patch = {"status": "failed", "error": problem, "finished_at": now()}
        update_task(lane_name, rec["task_id"], patch)
        return {**rec, **patch}
    out = claude_turn(
        rec["prompt"],
        Path(rec["cwd"]),
        model=rec["model"],
        budget=rec["budget_usd"],
        session_id=rec.get("resume_of") or rec["task_id"],
        task_id=rec["task_id"],
        resume=bool(rec.get("resume_of")),
    )
    patch = {k: out.get(k) for k in
             ("status", "session_id", "result", "cost_usd", "num_turns",
              "stop_reason", "error", "killed", "budget_hit")}
    patch["finished_at"] = now()
    update_task(lane_name, rec["task_id"], patch)
    return {**rec, **patch}


TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"

# A tool result can be a whole file. The console wants the shape of the turn,
# not the payload, so results are clipped here rather than shipped and hidden.
RESULT_CLIP = 1600


def transcript_path(session_id: str) -> Path | None:
    """Claude Code writes one JSONL per session, under a mangled-cwd directory.

    Globbing rather than re-deriving the mangling: the session id is a uuid, so
    it is unique across every project directory, and the mangling is theirs to
    change.
    """
    if not session_id:
        return None
    hits = sorted(TRANSCRIPT_ROOT.glob(f"*/{session_id}.jsonl"))
    return hits[0] if hits else None


def _tool_headline(name: str, inp: dict) -> str:
    """One line naming what a tool call actually touched."""
    if not isinstance(inp, dict):
        return name
    for key in ("file_path", "path", "pattern", "command", "url", "prompt"):
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            return " ".join(val.split())[:200]
    return name


def _clip(text: str, limit: int) -> tuple[str, bool]:
    return (text[:limit], True) if len(text) > limit else (text, False)


def record_events(rec: dict) -> list[dict]:
    """The renderable turns inside one transcript record.

    Bookkeeping records (queue-operation, last-prompt, summaries) carry no
    conversation and yield nothing.
    """
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return []
    role, at = msg.get("role"), rec.get("timestamp")
    content = msg.get("content")
    if isinstance(content, str):
        return [{"role": role, "kind": "text", "text": content, "at": at}]
    if not isinstance(content, list):
        return []
    events = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in ("text", "thinking"):
            body = block.get("text") or block.get("thinking") or ""
            if body.strip():
                events.append({"role": role, "kind": kind, "text": body, "at": at})
        elif kind == "tool_use":
            name = block.get("name") or "tool"
            events.append({"role": role, "kind": "tool", "tool": name, "at": at,
                           "text": _tool_headline(name, block.get("input") or {})})
        elif kind == "tool_result":
            body = block.get("content")
            if isinstance(body, list):
                body = "\n".join(b.get("text", "") for b in body if isinstance(b, dict))
            body, clipped = _clip(str(body or ""), RESULT_CLIP)
            events.append({"role": role, "kind": "result", "text": body, "at": at,
                           "clipped": clipped, "error": bool(block.get("is_error"))})
    return events


def transcript(session_id: str) -> list[dict]:
    """A worker session as ordered, renderable turns."""
    path = transcript_path(session_id)
    if not path:
        return []
    events = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.extend(record_events(rec))
    return events


def transcript_digest(session_id: str, turns: int = 40, clip: int = 300) -> str:
    """The tail of a worker session, one line per turn."""
    events = transcript(session_id)
    if not events:
        return ""
    lines = []
    for e in events[-turns:]:
        who = e.get("tool") if e["kind"] == "tool" else e["kind"]
        lines.append(f"[{who}] " + " ".join((e.get("text") or "").split())[:clip])
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- prior sessions
#
# Every chat you have ever had with Claude Code is already on this disk, and most
# of the work this harness is meant to supervise happened in them. There are
# thousands of files and hundreds of megabytes of them, so nothing here reads a
# whole session: each one is judged from its head and its tail.

HEAD_BYTES = 96_000
TAIL_BYTES = 256_000


def _head_tail(path: Path) -> tuple[list[dict], list[dict]]:
    """Parsed records from the start and the end of a transcript."""
    size = path.stat().st_size
    with path.open("rb") as fh:
        head = fh.read(min(size, HEAD_BYTES))
        if size <= HEAD_BYTES:
            tail = b""                        # the head is the whole file
        else:
            fh.seek(max(HEAD_BYTES, size - TAIL_BYTES))
            # The seek lands mid-line; that first fragment is not JSON.
            tail = fh.read().split(b"\n", 1)[-1]

    def parse(buf: bytes) -> list[dict]:
        out = []
        for line in buf.decode("utf-8", "replace").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    recs = parse(head)
    return recs, (parse(tail) or recs)


# What a session says about itself when it is in trouble.
TROUBLE = {
    "usage limit": "hit a Claude usage limit",
    "rate limit": "was rate limited",
    "context low": "ran low on context",
    "prompt is too long": "overflowed its context window",
    "BLOCKED:": "reported itself blocked",
    "ESCALATE:": "asked to escalate",
    "MemoryError": "ran out of memory",
    "No space left": "filled the disk",
}


def scan_session(path: Path) -> dict:
    """One prior chat, summarised from its two ends."""
    head, tail = _head_tail(path)
    # One enormous opening record can fill the whole head, leaving no turn that
    # carries a cwd. The tail always has one, and a session with no directory is
    # dropped as "not this workspace" - so the fallback decides whether a chat is
    # visible at all, not just how it is labelled.
    cwd = (next((r["cwd"] for r in head if r.get("cwd")), "")
           or next((r["cwd"] for r in reversed(tail) if r.get("cwd")), ""))
    first = next((e["text"] for r in head for e in record_events(r)
                  if e["kind"] == "text" and e["role"] == "user"), "")
    tail_events = [e for r in tail for e in record_events(r)]
    last = tail_events[-1] if tail_events else {}
    return {
        "id": path.stem,
        "cwd": cwd,
        "project": Path(cwd).name if cwd else path.parent.name,
        "branch": next((r["gitBranch"] for r in reversed(tail) if r.get("gitBranch")), ""),
        "started_at": next((r["timestamp"] for r in head if r.get("timestamp")), ""),
        "last_at": next((r["timestamp"] for r in reversed(tail) if r.get("timestamp")), ""),
        "size": path.stat().st_size,
        "first_prompt": " ".join(first.split())[:400],
        "last_kind": last.get("kind", ""),
        "last_text": " ".join((last.get("text") or "").split())[:400],
        "tail_events": tail_events,
    }


# Tools that change nothing by themselves: doing one of these over and over is
# the shape of a worker that is stuck.
PROBE_TOOLS = {"Bash", "Read", "Grep", "Glob", "WebFetch"}


def diagnose_session(s: dict) -> list[str]:
    """What looks wrong with a prior chat, from its tail alone."""
    events = s.get("tail_events") or []
    found = []
    # A chat that is still going has nothing to diagnose: its tail is still being
    # written, so every signal here is about a turn that has not finished yet.
    if _active_recently(s.get("last_at")):
        return found
    # Trouble surfaces either in what the assistant says or in a tool call that
    # failed. A *successful* read is excluded on purpose: a session that merely
    # read a file naming these markers is not a session in trouble, and reading
    # this very file is enough to trip all eight of them at once.
    blob = "\n".join(e.get("text") or "" for e in events[-80:]
                     if e.get("role") == "assistant" or e.get("error"))
    for marker, said in TROUBLE.items():
        if marker in blob:
            found.append(said)

    errors = sum(1 for e in events[-60:] if e.get("error"))
    if errors >= 5:
        found.append(f"{errors} failing tool calls in its last turns")

    # The same command over and over is a worker going in circles. Editing one
    # file repeatedly is not that - it is how editing a file works - so only the
    # tools that make no change on their own are counted.
    calls = [e["text"] for e in events[-40:]
             if e["kind"] == "tool" and e.get("tool") in PROBE_TOOLS]
    repeat = max((calls.count(c) for c in set(calls)), default=0)
    if repeat >= 5:
        found.append(f"ran the same command {repeat} times - it was looping")

    # A session whose last word is the assistant reaching for a tool never got an
    # answer back: it was interrupted, not finished.
    if s.get("last_kind") in ("tool", "thinking"):
        found.append("stopped mid-turn - it was interrupted rather than finished")
    return found


def _active_recently(iso: str | None, minutes: int = 15) -> bool:
    try:
        last = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - last).total_seconds() < minutes * 60


def prior_sessions(limit: int = 40, under: Path | None = ROOT,
                   min_size: int = 4000) -> list[dict]:
    """Recent Claude Code chats, newest first, with what looks wrong in each.

    Scoped to this workspace by default: chats about someone else's directory are
    not what this harness is here to supervise.
    """
    paths = sorted(TRANSCRIPT_ROOT.glob("*/*.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    live = set(_PROCS)
    out = []
    for p in paths:
        if len(out) >= limit:
            break
        try:
            if p.stat().st_size < min_size:
                continue                      # a chat with nothing in it
            s = scan_session(p)
        except OSError:
            continue
        if under and not str(Path(s["cwd"] or "/")).startswith(str(under)):
            continue
        s["symptoms"] = diagnose_session(s)
        s["mine"] = s["id"] in live           # a worker this harness is running
        s.pop("tail_events", None)
        out.append(s)
    return out


def session_events(session_id: str, limit: int = 400) -> list[dict]:
    """The readable tail of any session, this harness's or not.

    A transcript can be a hundred megabytes; only its end is ever read, because
    only its end says where the work got to.
    """
    path = transcript_path(session_id)
    if not path:
        return []
    head, tail = _head_tail(path)
    events = [e for r in (tail or head) for e in record_events(r)]
    return events[-limit:]


def session_brief(session_id: str) -> str:
    """A prior chat written up for an architect: what it was for, and where it got to."""
    path = transcript_path(session_id)
    if not path:
        return ""
    s = scan_session(path)
    events = s.pop("tail_events", [])
    symptoms = diagnose_session({**s, "tail_events": events})
    lines = [f"# Prior Claude session {s['id'][:8]}\n",
             f"- directory: {s['cwd'] or 'unknown'}\n",
             f"- branch: {s['branch'] or 'unknown'}\n",
             f"- ran: {s['started_at']} → {s['last_at']}\n",
             f"- transcript size: {s['size'] // 1000} KB\n"]
    if symptoms:
        lines.append("\n## What looks wrong\n\n" +
                     "".join(f"- {x}\n" for x in symptoms))
    lines.append(f"\n## What it was asked to do\n\n{s['first_prompt']}\n")
    tail = "\n".join(
        f"[{e.get('tool') if e['kind'] == 'tool' else e['kind']}] "
        + " ".join((e.get("text") or "").split())[:300]
        for e in events[-40:])
    lines.append("\n## Where it got to (last turns)\n\n```\n" + tail + "\n```\n")
    return "".join(lines)


# A worker can write a large generated file. Its content still belongs in the
# diff - a name alone tells you nothing about whether the work is right.
UNTRACKED_CLIP = 20000


def claude_diff(lane_name: str, lane: dict) -> str:
    """What the worker changed in its worktree, vs the lane's base branch."""
    wt = WORKTREE_DIR / lane_name
    if not wt.exists():
        return ""
    base = lane.get("branch") or "main"
    if run(["git", "rev-parse", "--verify", "--quiet", base], cwd=wt).returncode != 0:
        # Otherwise `git diff base...HEAD` fails and the caller reads the empty
        # string as "the worker changed nothing".
        return (f"# cannot diff: this lane's base branch {base!r} does not exist in the repo.\n"
                f"# Set the right one on the lane (master? develop?) and dispatch again.")

    parts = []
    committed = run(["git", "diff", f"{base}...HEAD"], cwd=wt).stdout
    if committed:
        stat = run(["git", "diff", "--stat", f"{base}...HEAD"], cwd=wt).stdout
        parts.append(f"# committed on amp/{lane_name} (vs {base})\n{stat}\n{committed}")
    working = run(["git", "diff", "HEAD"], cwd=wt).stdout
    if working:
        parts.append(f"# uncommitted in the worktree\n{working}")

    untracked = run(["git", "ls-files", "--others", "--exclude-standard"], cwd=wt).stdout.split()
    for rel in untracked:
        parts.append(f"# new file: {rel}\n" + _new_file_diff(wt / rel))
    return "\n\n".join(parts)


def _new_file_diff(path: Path) -> str:
    """An untracked file rendered as an all-additions patch."""
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        size = path.stat().st_size if path.exists() else 0
        return f"# (binary or unreadable, {size} bytes)"
    body, clipped = _clip(text, UNTRACKED_CLIP)
    out = "\n".join("+" + line for line in body.splitlines())
    return out + ("\n+ … clipped" if clipped else "")


# ---------------------------------------------------------------- codex bridge


def codex_available() -> bool:
    return shutil.which("codex") is not None


# Asking the CLI whether it is signed in costs a process spawn, and the console
# now asks it several times a second: every role's dropdown lists every model,
# and the poll that draws the header rebuilds all three. Measured at 5 spawns
# and 0.49s per poll before this cache. Sign-in state changes about once a
# month, so a few seconds of staleness buys back all of it - and `fresh=True`
# is there for the one caller that has just changed it and must not be told
# what was true a moment ago.
_CODEX_LOGIN_CACHE = {"at": -1e9, "ok": False}
_CODEX_LOGIN_TTL_S = 15.0


def codex_logged_in(fresh: bool = False) -> bool:
    if not codex_available():
        return False
    now = time.monotonic()
    if not fresh and now - _CODEX_LOGIN_CACHE["at"] < _CODEX_LOGIN_TTL_S:
        return _CODEX_LOGIN_CACHE["ok"]
    p = run(["codex", "login", "status"])
    ok = "not logged in" not in (p.stdout + p.stderr).lower()
    _CODEX_LOGIN_CACHE.update(at=now, ok=ok)
    return ok


# An architect round is one model call, but it is a long one - a full packet,
# a repo's worth of context, and a build order written out as numbered steps.
ARCHITECT_TIMEOUT_S = 15 * 60


def _codex_prompt(messages: list[dict]) -> str:
    """Flatten a chat thread into the single prompt `codex exec` takes.

    Every round re-sends the whole conversation, which is exactly what the
    OpenRouter path did, so nothing changes about what the architect sees.
    `codex exec resume` could send only the new turn instead, and that is the
    obvious next improvement, but it makes the thread a thing the CLI owns and
    we do not - worth doing deliberately, not as a side effect of this.
    """
    parts = []
    for m in messages:
        text = (m.get("content") or "").strip()
        if not text:
            continue
        role = m.get("role")
        if role == "system":
            parts.append(text)
        elif role == "assistant":
            parts.append("--- your earlier ruling ---\n" + text)
        else:
            parts.append("--- ---\n" + text)
    return "\n\n".join(parts)


# The token counts codex adds up, and only those. `cached_input_tokens` and
# `cache_write_input_tokens` are parts of `input_tokens`, and
# `reasoning_output_tokens` is part of `output_tokens`, so summing every integer
# in the record would double count a cached round by roughly twice over.
CODEX_TOKEN_FIELDS = ("input_tokens", "output_tokens")


def _usage_records(obj, out: list):
    """Every usage record in an event, wherever the schema happens to put it.

    Stops descending once it has one, so a record nested inside a record is
    counted once rather than twice.
    """
    if isinstance(obj, dict):
        if "total_tokens" in obj or "input_tokens" in obj:
            out.append(obj)
            return
        for v in obj.values():
            _usage_records(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _usage_records(v, out)


def _record_tokens(rec: dict) -> int:
    """One usage record's total, however that record spells it.

    codex reports `turn.completed` with input and output counts and no total;
    OpenAI-shaped payloads report `total_tokens`. Take whichever is there rather
    than assuming a schema neither of them promised.
    """
    if isinstance(rec.get("total_tokens"), int):
        return rec["total_tokens"]
    return sum(v for k, v in rec.items()
               if k in CODEX_TOKEN_FIELDS and isinstance(v, int))


def _codex_usage(stdout: str, prompt: str, reply: str) -> dict:
    """Token usage from the event stream, or a measured estimate saying so.

    This matters more than it looks. `cost_tokens` is what AUTO_TOKEN_CEILING
    stops a runaway thread with, so if the events ever stop carrying usage and
    this quietly returned zero, the ceiling would stop being a ceiling and
    nothing anywhere would say so. A rough estimate that is labelled as one
    keeps the brake connected - and the first version of this fell back to that
    estimate against a real run, reporting 14 tokens where the CLI had just said
    15,427, which is exactly how a ceiling stops being one without complaint.
    """
    total = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        recs: list = []
        _usage_records(ev, recs)
        # Summed, not maxed: one exec can complete several turns, and each
        # reports only its own.
        total += sum(_record_tokens(r) for r in recs)
    if total:
        return {"total_tokens": total}
    return {"total_tokens": (len(prompt) + len(reply)) // 4, "estimated": True}


def codex_chat(messages: list[dict], model_key: str, max_tokens: int = 8000) -> dict:
    """The architect, answered by `codex exec` against your ChatGPT subscription.

    Returns the shape `openrouter_chat` returns, because the two call sites read
    `choices[0].message.content` and `usage.total_tokens` and have no business
    knowing which backend answered.

    `model_key` is the luna/terra/sol price tier, which means nothing here - a
    subscription has no per-token price to choose between - so it is ignored
    rather than mistranslated into a model name that may not exist. Set
    `codex_architect_model` in config.json to pin one. `max_tokens` likewise has
    no equivalent: the CLI does not take a reply ceiling.

    It runs `-s read-only` from an empty directory, which means it cannot write
    anything, anywhere. It does NOT mean it cannot look: read-only permits reads
    across the filesystem, and a test round asked for the first line of a file
    outside the temp directory and gave it correctly. The empty cwd only means
    it starts with nothing to hand.

    So this is a real difference from the OpenRouter path, which was blind and
    could only answer from the packet. This architect may sometimes go and read
    a file instead of saying NEED[file], which is usually faster and occasionally
    wrong - it reads the live tree, not the lane's isolated worktree, so what it
    sees is not what the worker will be working on. Worth watching; the fix, if
    it turns out to matter, is to point it at the worktree deliberately rather
    than to pretend it cannot see.
    """
    if not codex_available():
        die("codex CLI not found. Install it (npm i -g @openai/codex), or point "
            "the architect back at OpenRouter in Settings.")
    if not codex_logged_in():
        die("ChatGPT is not connected. Click Connect ChatGPT in the console, or "
            "point the architect back at OpenRouter in Settings.")

    prompt = _codex_prompt(messages)
    model = config().get("codex_architect_model")
    with tempfile.TemporaryDirectory(prefix="amp-architect-") as td:
        out = Path(td) / "reply.md"
        cmd = ["codex", "exec", "--json", "--skip-git-repo-check",
               "-s", "read-only", "-C", td, "-o", str(out)]
        if model:
            cmd += ["-m", model]
        cmd.append("-")          # the packet is tens of KB: stdin, never argv
        try:
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                               timeout=ARCHITECT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            die(f"the codex architect timed out after {ARCHITECT_TIMEOUT_S}s")
        reply = out.read_text() if out.is_file() else ""

    if not reply.strip():
        # An empty answer alongside a clean exit code is worse than an error,
        # because it would be appended to the thread as though the architect had
        # deliberately said nothing. Treat it as the failure it is, in the CLI's
        # own words - redacted, because they are its words and not ours.
        detail = last_message(redact(p.stderr or p.stdout), 4).strip()
        die(f"the codex architect returned nothing (exit {p.returncode})"
            + (f":\n{detail}" if detail else ""))

    return {
        "choices": [{"message": {"role": "assistant", "content": reply}}],
        "model": f"codex/{model or 'default'}",
        "usage": _codex_usage(p.stdout, prompt, reply),
    }


def claude_chat(messages: list[dict], model: str, max_tokens: int = 8000) -> dict:
    """One round answered by Claude, as an architect or supervisor - not a worker.

    Returns the shape `openrouter_chat` and `codex_chat` return, for the same
    reason they agree with each other: the call sites read
    `choices[0].message.content` and `usage.total_tokens` and have no business
    knowing who answered.

    `--tools ""` is what makes this a CHAT rather than a coding agent. Without
    it, `claude -p` is the same thing the workers are - it would go and edit
    files while answering a question about them, from whatever directory the
    console happens to be sitting in. With it, this round can read nothing,
    write nothing and run nothing; it answers from the packet, exactly as the
    OpenRouter path does. The empty temp cwd is belt-and-braces on top.

    `max_tokens` has no equivalent on this CLI, same as codex. The spend guard
    that does exist is `--max-budget-usd`, so that is what is passed.
    """
    if not claude_available():
        die("Claude Code is not installed, so it cannot answer as an architect "
            "or supervisor. Pick a different model in Settings.")
    problem = claude_auth_problem()
    if problem:
        die(problem)

    prompt = _codex_prompt(messages)
    with tempfile.TemporaryDirectory(prefix="amp-think-") as td:
        cmd = ["claude", "-p", "--output-format", "json",
               "--model", model, "--tools", "",
               "--max-budget-usd", f"{architect_budget_usd():.2f}"]
        try:
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                               timeout=ARCHITECT_TIMEOUT_S, cwd=td,
                               env=claude_env())
        except subprocess.TimeoutExpired:
            die(f"the Claude architect timed out after {ARCHITECT_TIMEOUT_S}s")

    try:
        env = json.loads(p.stdout or "{}")
    except json.JSONDecodeError:
        env = {}
    reply = (env.get("result") or "") if not env.get("is_error") else ""

    if not reply.strip():
        # Same rule as codex: an empty answer with a clean exit is worse than an
        # error, because it gets appended to the thread as though Claude had
        # deliberately said nothing. Its own words, redacted, because they are
        # its words and not ours.
        detail = last_message(redact(p.stderr or p.stdout), 4).strip()
        die(f"the Claude architect returned nothing (exit {p.returncode})"
            + (f":\n{detail}" if detail else ""))

    usage = env.get("usage") or {}
    total = sum(v for k, v in usage.items()
                if k in ("input_tokens", "output_tokens") and isinstance(v, int))
    return {
        "choices": [{"message": {"role": "assistant", "content": reply}}],
        "model": f"claude/{model}",
        # Measured-and-labelled rather than zero, for the reason spelled out in
        # `_codex_usage`: AUTO_TOKEN_CEILING is a brake, and a brake that reads
        # zero when the numbers go missing has quietly stopped being one.
        "usage": ({"total_tokens": total} if total
                  else {"total_tokens": (len(prompt) + len(reply)) // 4,
                        "estimated": True}),
    }


def architect_budget_usd() -> float:
    """What one thinking round may spend on the Claude path.

    Its own setting, not the worker's `budget_usd`: a worker's budget covers a
    whole build, and handing that same ceiling to a single question would make
    an expensive accident cheap to trigger.
    """
    try:
        return max(0.01, float(config().get("architect_budget_usd") or 0.50))
    except (TypeError, ValueError):
        return 0.50


def _normalize_task(raw: dict) -> dict:
    """codex cloud list --json shape is not contractually stable (EXPERIMENTAL).

    Pull the fields we need by trying known aliases, and keep the raw record so
    nothing is silently lost if the schema moves.
    """

    def pick(*names):
        for n in names:
            if isinstance(raw, dict) and raw.get(n) not in (None, ""):
                return raw[n]
        return None

    return {
        "task_id": pick("id", "task_id", "taskId"),
        "status": (pick("status", "state", "phase") or "unknown"),
        "title": pick("title", "name", "prompt", "query"),
        "env_id": pick("environment_id", "env_id", "environmentId", "env"),
        "url": pick("url", "web_url", "link"),
        "updated_at": pick("updated_at", "updatedAt", "created_at", "createdAt"),
        "raw": raw,
    }


def codex_list(env_id: str | None = None, limit: int = LIST_LIMIT) -> list[dict]:
    cmd = ["codex", "cloud", "list", "--json", "--limit", str(min(limit, LIST_LIMIT))]
    if env_id:
        cmd += ["--env", env_id]
    p = run(cmd)
    if p.returncode != 0:
        die(f"codex cloud list failed:\n{(p.stderr or p.stdout).strip()}")
    text = p.stdout.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # tolerate JSON-lines
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        data = rows
    if isinstance(data, dict):
        data = data.get("tasks") or data.get("data") or data.get("items") or [data]
    return [_normalize_task(r) for r in data if isinstance(r, dict)]


# ---------------------------------------------------------------- commands


def cmd_login(args):
    """Connect a worker token from the terminal. The console does the same thing."""
    lg = ClaudeLogin()
    try:
        url = lg.start()
    except RuntimeError as e:
        die(str(e))
    print("Opening Chrome to sign in. If it did not open, use this URL:\n")
    print(f"  {url}\n")
    open_url(url)
    try:
        code = input("Paste the code here > ").strip()
    except (EOFError, KeyboardInterrupt):
        lg.close()
        die("cancelled")
    try:
        token = lg.submit(code)
    except RuntimeError as e:
        die(str(e))
    store_claude_token(token)
    print(f"\nconnected. token stored in {SECRETS_PATH} (untracked, chmod 600)")
    return 0


def cmd_doctor(args):
    cfg = config()
    print("amp doctor")
    print("=" * 60)

    ok = True
    lanes = cfg["lanes"]
    uses_codex = any(lane_backend(l) == "codex" for l in lanes.values())

    claude_ok = claude_available()
    print(f"  claude CLI         {'OK' if claude_ok else 'MISSING (npm i -g @anthropic-ai/claude-code)'}")
    if claude_ok:
        print(f"    version          {run(['claude', '--version']).stdout.strip()}")
        problem = claude_auth_problem()
        print(f"  claude auth        {'OK' if not problem else 'STALE'}")
        if problem:
            print(f"    {problem}")
            claude_ok = False
    ok &= claude_ok

    codex_ok = codex_available()
    print(f"  codex CLI          {'OK' if codex_ok else 'missing' + (' (npm i -g @openai/codex)' if uses_codex else ' (optional)')}")
    if codex_ok:
        print(f"    version          {run(['codex', '--version']).stdout.strip()}")
        logged = codex_logged_in()
        print(f"  codex auth         {'OK' if logged else 'NOT LOGGED IN -> run: codex login'}")
        if uses_codex:
            ok &= logged
    elif uses_codex:
        ok = False

    gh_ok = shutil.which("gh") is not None
    print(f"  gh CLI             {'OK' if gh_ok else 'missing (optional)'}")

    key = find_openrouter_key()
    if key:
        print(f"  openrouter key     OK ({key[:12]}...)")
    else:
        print("  openrouter key     MISSING -> escalation disabled")
        ok = False

    print(f"  lanes configured   {len(lanes)}")
    unbound = [n for n, l in lanes.items() if lane_backend(l) == "codex" and not l.get("env_id")]
    for n in sorted(lanes):
        lane = lanes[n]
        be = lane_backend(lane)
        mark = "NO ENV" if n in unbound else "OK "
        print(f"    {mark:7} {n:<22} {be:<7} {lane.get('repo','?')}")
    if unbound:
        print()
        print("  Codex lanes without an env id cannot dispatch. Get ids with:")
        print("    codex cloud            # interactive TUI, copy the env id per repo")
        print("    ./amp lane env <name> <ENV_ID>")
        ok = False

    budget = cfg.get("claude_budget_usd", DEFAULT_BUDGET_USD)
    print(f"  claude budget cap  ${budget:.2f} per dispatch  (model {cfg.get('claude_model', DEFAULT_CLAUDE_MODEL)})")
    print(f"  worker worktrees   {WORKTREE_DIR}")

    print("=" * 60)
    print("READY" if ok else "NOT READY - resolve the items above")
    return 0 if ok else 1


def cmd_lanes(args):
    cfg = config()
    lanes = cfg["lanes"]
    if not lanes:
        print("no lanes. Add one:\n  ./amp lane add wrl --repo c-u-l8er/WRL --path WRL")
        return 0
    print(f"{'LANE':<22} {'REPO':<32} {'BRANCH':<14} {'BACKEND':<8} ENV")
    for name in sorted(lanes):
        l = lanes[name]
        be = lane_backend(l)
        env = "" if be == "claude" else (l.get("env_id") or "-- unbound --")
        print(f"{name:<22} {l.get('repo',''):<32} {l.get('branch','main'):<14} {be:<8} {env}")
    return 0


def cmd_lane_add(args):
    try:
        lane = add_lane(args.name, repo=args.repo, path=args.path, branch=args.branch,
                        backend=args.backend, env_id=args.env, replace=args.replace)
    except LaneError as e:
        die(str(e))
    print(f"lane {args.name!r} -> {lane['repo']} ({lane['path']}) "
          f"branch={lane['branch']} backend={lane['backend']}")
    if lane["needs_env"]:
        print("  no env id yet. Run `codex cloud` to find it, then:")
        print(f"    ./amp lane env {args.name} <ENV_ID>")
    return 0


def cmd_lane_env(args):
    cfg = config()
    lane_or_die(cfg, args.name)
    cfg["lanes"][args.name]["env_id"] = args.env_id
    save_json(CONFIG_PATH, cfg)
    print(f"lane {args.name!r} bound to env {args.env_id}")
    return 0


def cmd_lane_backend(args):
    cfg = config()
    lane_or_die(cfg, args.name)
    cfg["lanes"][args.name]["backend"] = args.backend
    save_json(CONFIG_PATH, cfg)
    print(f"lane {args.name!r} now dispatches to {args.backend}")
    if args.backend == "codex" and not cfg["lanes"][args.name].get("env_id"):
        print(f"  bind an env id first: ./amp lane env {args.name} <ENV_ID>")
    return 0


def _print_claude_result(rec: dict):
    print(rec.get("result") or rec.get("error") or "(no output)")
    print()
    cost = rec.get("cost_usd") or 0.0
    print(f"{rec['status']}  ${cost:.4f}  {rec.get('num_turns') or '?'} turns  session {rec.get('session_id')}")
    print(f"worktree: {rec['cwd']}")
    if rec["status"] == "completed":
        print(f"review it with: ./amp diff {rec.get('lane','<lane>')}")


def cmd_dispatch(args):
    cfg = config()
    lane = lane_or_die(cfg, args.lane)
    backend = args.backend or lane_backend(lane)

    prompt = args.prompt
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text()
    if not (prompt or "").strip():
        die("prompt is empty")
    branch = args.branch or lane.get("branch") or "main"

    if backend == "claude":
        budget = args.budget if args.budget is not None else cfg.get(
            "claude_budget_usd", DEFAULT_BUDGET_USD
        )
        model = args.model or cfg.get("claude_model", DEFAULT_CLAUDE_MODEL)
        rec = start_claude_task(
            args.lane, lane, prompt,
            branch=branch, model=model, budget=budget, isolate=not args.shared_tree,
        )
        where = rec["cwd"] if rec["isolated"] else f"{rec['cwd']}  (SHARED TREE)"
        print(f"dispatching -> {args.lane} [claude {model}]  cap ${budget:.2f}")
        print(f"  {where}\n")
        rec = run_claude_task(args.lane, rec)
        rec["lane"] = args.lane
        _print_claude_result(rec)
        return 0 if rec["status"] == "completed" else 1

    env_id = lane.get("env_id")
    if not env_id:
        die(
            f"lane {args.lane!r} has no codex env id.\n"
            f"  Run `codex cloud` to browse environments, then:\n"
            f"    ./amp lane env {args.lane} <ENV_ID>"
        )
    if not codex_logged_in():
        die("codex is not logged in. Run: codex login")

    cmd = ["codex", "cloud", "exec", "--env", env_id, "--branch", branch]
    if args.attempts and args.attempts > 1:
        cmd += ["--attempts", str(args.attempts)]
    cmd += [prompt]

    print(f"dispatching -> {args.lane} ({lane['repo']} @ {branch}, best-of-{args.attempts})")
    p = run(cmd)
    out = (p.stdout + p.stderr).strip()
    print(out or "(no output)")
    if p.returncode != 0:
        die(f"dispatch failed (exit {p.returncode})")

    b = board()
    b["tasks"].setdefault(args.lane, [])
    b["tasks"][args.lane].insert(
        0,
        {
            "backend": "codex",
            "dispatched_at": now(),
            "prompt": prompt,
            "branch": branch,
            "attempts": args.attempts,
            "env_id": env_id,
            "submit_output": out,
            "status": "submitted",
        },
    )
    save_json(BOARD_PATH, b)
    print("\nrecorded on board. Poll with: ./amp poll")
    return 0


def cmd_reply(args):
    """Answer a claude worker mid-task. Codex Cloud has no equivalent."""
    cfg = config()
    lane = lane_or_die(cfg, args.lane)
    prior = latest_claude_task(args.lane)
    if not prior:
        die(f"no claude task on the board for {args.lane!r}")
    session = prior.get("session_id") or prior.get("task_id")
    if getattr(args, "session", None):
        session = resumable_session(args.lane, args.session)
        if not session:
            die(f"no resumable claude session matching {args.session!r} in {args.lane!r}")
        prior = next((r for r in board().get("tasks", {}).get(args.lane, [])
                      if (r.get("session_id") or r.get("task_id")) == session), prior)
    budget = args.budget if args.budget is not None else cfg.get(
        "claude_budget_usd", DEFAULT_BUDGET_USD
    )
    rec = start_claude_task(
        args.lane, lane, args.prompt,
        branch=prior.get("branch") or lane.get("branch") or "main",
        model=prior.get("model") or cfg.get("claude_model", DEFAULT_CLAUDE_MODEL),
        budget=budget,
        isolate=prior.get("isolated", True),
        resume_of=session,
    )
    print(f"resuming {args.lane} session {session}  cap ${budget:.2f}\n")
    rec = run_claude_task(args.lane, rec)
    rec["lane"] = args.lane
    _print_claude_result(rec)
    return 0 if rec["status"] == "completed" else 1


def cmd_poll(args):
    """Refresh codex lanes. Claude lanes settle themselves - nothing to poll."""
    cfg = config()
    b = board()

    lanes = [args.lane] if args.lane else sorted(cfg["lanes"])
    codex_lanes = [n for n in lanes if lane_backend(cfg["lanes"].get(n, {})) == "codex"]
    if codex_lanes and not codex_logged_in():
        die("codex is not logged in. Run: codex login")

    seen = 0
    for name in codex_lanes:
        lane = cfg["lanes"].get(name)
        if not lane or not lane.get("env_id"):
            continue
        tasks = codex_list(env_id=lane["env_id"])
        b.setdefault("remote", {})[name] = tasks
        seen += len(tasks)
    b["polled_at"] = now()
    save_json(BOARD_PATH, b)
    print(f"polled {len(lanes)} lane(s), {seen} remote task(s) at {b['polled_at']}")
    return cmd_board(args)


def cmd_board(args):
    cfg = config()
    b = board()
    remote = b.get("remote", {})
    print()
    print(f"BOARD  (polled {b.get('polled_at') or 'never'})")
    print("=" * 78)
    if not cfg["lanes"]:
        print("  no lanes configured")
        return 0
    local = b.get("tasks", {})
    stalled = []
    for name in sorted(cfg["lanes"]):
        lane = cfg["lanes"][name]
        be = lane_backend(lane)
        head = f"{name}  [{lane.get('repo','?')}]  {be}"

        if be == "claude":
            tasks = [t for t in local.get(name, []) if t.get("backend") == "claude"][:5]
            if not tasks:
                print(f"  {head}\n      idle")
                continue
            print(f"  {head}")
            for t in tasks:
                cost = t.get("cost_usd")
                money = f"${cost:.4f}" if cost else "-"
                line = (t.get("result") or t.get("error") or t.get("prompt") or "")
                print(f"      {t.get('status','?'):<12} {money:<10} {line.replace(chr(10), ' ')[:44]}")
            if any(_is_stalled(t) for t in tasks):
                stalled.append(name)
            continue

        tasks = remote.get(name, [])
        if not lane.get("env_id"):
            print(f"  {head}\n      unbound - no codex env id")
            continue
        if not tasks:
            print(f"  {head}\n      idle")
            continue
        print(f"  {head}")
        for t in tasks[:5]:
            title = (t.get("title") or "").replace("\n", " ")[:46]
            print(f"      {t.get('status','?'):<12} {str(t.get('task_id'))[:20]:<22} {title}")
        if any(_is_stalled(t) for t in tasks):
            stalled.append(name)
    print("=" * 78)
    if stalled:
        print(f"  needs attention: {', '.join(stalled)}   (./amp ask <lane> -q '...')")
    return 0


def _is_stalled(t: dict) -> bool:
    s = (t.get("status") or "").lower()
    return s in {"failed", "error", "cancelled", "canceled"}


def cmd_diff(args):
    cfg = config()
    lane = lane_or_die(cfg, args.lane)
    if lane_backend(lane) == "claude":
        d = claude_diff(args.lane, lane)
        print(d or "(worker made no changes)")
        return 0
    task_id = args.task_id or _latest_task_id(args.lane)
    cmd = ["codex", "cloud", "diff", task_id]
    if args.attempt:
        cmd += ["--attempt", str(args.attempt)]
    p = run(cmd)
    print(p.stdout or p.stderr)
    return p.returncode


def cmd_apply(args):
    cfg = config()
    lane = lane_or_die(cfg, args.lane)
    if lane_backend(lane) == "claude":
        # A claude worker's output already lives on a real branch. Merging it into
        # the shared checkout is yours to do: other sessions are editing that tree.
        print(f"claude lane {args.lane!r} - nothing to apply, the work is on a branch.\n")
        print(f"  worktree  {WORKTREE_DIR / args.lane}")
        print(f"  branch    amp/{args.lane}")
        print(f"\nreview:  ./amp diff {args.lane}")
        print(f"merge:   git -C {ROOT / lane['path']} merge amp/{args.lane}")
        return 0
    task_id = args.task_id or _latest_task_id(args.lane)
    cwd = ROOT / lane["path"]
    cmd = ["codex", "cloud", "apply", task_id]
    if args.attempt:
        cmd += ["--attempt", str(args.attempt)]
    print(f"applying {task_id} into {cwd}")
    p = run(cmd, cwd=cwd)
    print(p.stdout or p.stderr)
    return p.returncode


def _latest_task_id(lane_name: str) -> str:
    b = board()
    tasks = b.get("remote", {}).get(lane_name, [])
    if not tasks:
        die(f"no polled tasks for lane {lane_name!r}. Run ./amp poll (or pass a task id).")
    tid = tasks[0].get("task_id")
    if not tid:
        die(f"latest task for {lane_name!r} has no id in the codex payload")
    return tid


# ---------------------------------------------------------------- packet + consult


def build_packet(lane_name: str, question: str, extra_files: list[str]) -> tuple[Path, str]:
    """Zip a lane's state for GPT-5.6. Returns (zip_path, markdown_brief).

    `zip` is not installed on this box; use python zipfile.
    """
    cfg = config()
    lane = lane_or_die(cfg, lane_name)
    repo_path = ROOT / lane["path"]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    PACKET_DIR.mkdir(parents=True, exist_ok=True)
    zpath = PACKET_DIR / f"{lane_name}-{stamp}.zip"

    status = run(["git", "status", "--porcelain"], cwd=repo_path).stdout
    log = run(["git", "log", "--oneline", "-15"], cwd=repo_path).stdout
    diff = run(["git", "diff", "HEAD"], cwd=repo_path).stdout
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path).stdout.strip()

    b = board()
    lane_tasks = b.get("remote", {}).get(lane_name, [])
    local_hist = b.get("tasks", {}).get(lane_name, [])[:3]

    brief = io.StringIO()
    brief.write(f"# Packet: {lane_name}\n\n")
    brief.write(f"- repo: `{lane['repo']}`  branch: `{branch}`\n")
    brief.write(f"- generated: {now()}\n\n")
    brief.write("## Question for GPT-5.6\n\n")
    brief.write(question.strip() + "\n\n")
    brief.write("## Recent commits\n\n```\n" + (log or "(none)") + "```\n\n")
    brief.write("## Working tree status\n\n```\n" + (status or "(clean)") + "```\n\n")
    if local_hist:
        brief.write("## Recent dispatches\n\n")
        for h in local_hist:
            be = h.get("backend", "codex")
            brief.write(f"- {h['dispatched_at']} [{be}] ({h.get('status')}): {h['prompt'][:200]}\n")
        brief.write("\n")

    last_claude = next((h for h in local_hist if h.get("backend") == "claude"), None)
    if last_claude and (last_claude.get("result") or last_claude.get("error")):
        brief.write("## What the claude worker reported\n\n")
        brief.write((last_claude.get("result") or last_claude.get("error"))[:8000] + "\n\n")
    if last_claude:
        # A stalled worker's own trail is the most useful thing to hand an
        # architect, but a raw transcript is mostly tool payload, so it is one
        # line per turn.
        tail = transcript_digest(last_claude.get("session_id") or last_claude.get("task_id"))
        if tail:
            brief.write("## What the worker actually did (last turns)\n\n```\n" + tail + "```\n\n")

    worker_diff = claude_diff(lane_name, lane) if lane_backend(lane) == "claude" else ""
    if worker_diff:
        brief.write(f"## Worker branch amp/{lane_name} vs {branch}\n\n")
        brief.write("```diff\n" + worker_diff[:20000] + "\n```\n\n")
    if lane_tasks:
        brief.write("## Codex cloud tasks\n\n")
        for t in lane_tasks[:5]:
            brief.write(f"- `{t.get('status')}` {t.get('task_id')} {t.get('title') or ''}\n")
        brief.write("\n")
    if diff:
        brief.write(f"## Uncommitted diff ({len(diff)} bytes, full copy in zip)\n\n")
        brief.write("```diff\n" + diff[:20000] + "\n```\n\n")
    brief_text = brief.getvalue()

    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("PACKET_README.md", brief_text)
        z.writestr("git/status.txt", status)
        z.writestr("git/log.txt", log)
        z.writestr("git/diff.patch", diff)
        if worker_diff:
            z.writestr("git/worker.patch", worker_diff)
        for rel in extra_files:
            src = Path(rel)
            if not src.is_absolute():
                src = repo_path / rel
            if src.exists() and src.is_file():
                z.write(src, f"files/{src.name}")
            else:
                z.writestr(f"files/MISSING-{Path(rel).name}.txt", f"not found: {rel}")

    return zpath, brief_text


CONSULT_SYSTEM = (
    "You are the consulting architect for the [&] protocol stack. You are given a packet "
    "describing a stalled or ambiguous engineering situation, and you are in a working "
    "conversation about it: the operator can answer you, and a Claude worker with shell "
    "and editor access in an isolated git worktree will carry out what you rule and "
    "report back to you.\n\n"
    "Rule decisively. State: (1) the ruling, (2) the reasoning, (3) the exact next build "
    "order as numbered steps, (4) anything you are refusing to rule on and why.\n\n"
    "Write the build order as instructions to that worker, not as advice to a person - "
    "the steps are handed to it verbatim.\n\n"
    "If something is missing and you would otherwise be guessing, do not guess. Put each "
    "missing thing on its own line, and tag it with who can answer it:\n"
    "  NEED[file]: an exact path in the repo. It is read and handed back immediately.\n"
    "  NEED[evidence]: command output, a test result, a source excerpt - anything a "
    "worker with a shell in the worktree can go and observe. It is sent to fetch it and "
    "reports back to you, automatically.\n"
    "  NEED[decision]: a judgement only the operator can make - what to build, what "
    "counts as done, what to spend. This one stops the thread until they answer, so "
    "spend them carefully: ask for evidence wherever evidence would do.\n\n"
    "Be concrete and terse. Do not hedge."
)


def openrouter_chat(messages: list[dict], model_key: str, max_tokens: int = 8000) -> dict:
    # Enforced here rather than only at the callers, because this is the one
    # place the money is actually spent. A guard at each entry point is a guard
    # that is missing from whichever entry point gets added next.
    if not openrouter_enabled():
        die("OpenRouter is switched off in Settings. Turn it back on to use the "
            "architect, or answer this thread yourself.")
    model = CONSULT_MODELS.get(model_key, model_key)
    key = openrouter_key()
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens}
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-Title": "ProjectAmp2 amp harness",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        die(f"OpenRouter HTTP {e.code}: {e.read().decode()[:600]}")
    except urllib.error.URLError as e:
        die(f"OpenRouter unreachable: {e.reason}")


def architect_chat(messages: list[dict], model_key: str, max_tokens: int = 8000) -> dict:
    """One architect round, wherever the architect currently lives.

    Every path that consults the architect goes through here, so switching
    backend is a settings change and not a code change, and so there is exactly
    one place to look when asking what a round costs and who it asks.
    """
    backend = architect_backend()
    if backend == "codex":
        return codex_chat(messages, model_key, max_tokens)
    if backend == "claude":
        return claude_chat(messages, role_model("architect"), max_tokens)
    return openrouter_chat(messages, model_key, max_tokens)


# ---------------------------------------------------------------- consult threads
#
# One consult is one working conversation about one lane. It holds the packet,
# every ruling, every answer you gave, and every report the worker brought back,
# so the architect keeps its own context across as many rounds as it takes.

# CONSULT_DIR is bound by _bind_state; see _STATE_LAYOUT.

_CONSULT_LOCK = threading.Lock()

# How a turn is introduced to the architect. The packet is already a document.
TURN_PREFIX = {
    "packet": "",
    "you": "The operator answers:\n\n",
    "supplied": "You asked for these. Here they are:\n\n",
    "worker": "The Claude worker carried out your build order. It reported:\n\n",
}


def consult_path(cid: str) -> Path:
    return CONSULT_DIR / f"{Path(cid).name}.json"


def load_consult(cid: str) -> dict | None:
    p = consult_path(cid)
    return json.loads(p.read_text()) if p.is_file() else None


def save_consult(c: dict):
    CONSULT_DIR.mkdir(parents=True, exist_ok=True)
    save_json(consult_path(c["id"]), c)


def consults(lane: str | None = None) -> list[dict]:
    """Summaries, newest first."""
    if not CONSULT_DIR.exists():
        return []
    out = []
    for p in CONSULT_DIR.glob("*.json"):
        try:
            c = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if lane and c.get("lane") != lane:
            continue
        out.append({
            "id": c["id"], "lane": c.get("lane"), "opened_at": c.get("opened_at"),
            "status": c.get("status"), "trigger": c.get("trigger"),
            "question": c.get("question"), "model": c.get("model"),
            "rounds": sum(1 for t in c.get("turns", []) if t["role"] == "gpt"),
            "needs": c.get("needs") or [],
            # The same needs, carrying the architect's own `[decision]` tag.
            # `needs` has had the tag stripped off it, and guessing the kind
            # back from the wording gets it wrong: "Select next target: ..." is
            # a decision by any reading and matches none of the markers. Read
            # the tag rather than re-inferring it.
            "needs_typed": parse_typed_needs(last_ruling(c)),
            "blocked_on": c.get("blocked_on"),
            "blocked_why": BLOCK_REASONS.get(c.get("blocked_on") or ""),
            "cost_tokens": c.get("cost_tokens", 0),
            "last_at": (c.get("turns") or [{}])[-1].get("at"),
        })
    out.sort(key=lambda c: c.get("opened_at") or "", reverse=True)
    return out


# A need line, with or without the tag. Untagged lines come from older threads
# and from rulings that ignored the format, and they still have to work.
_NEED_RE = re.compile(
    r"^(?:[-*]\s*|\d+[.)]\s*)?NEED\s*(?:\[\s*(file|evidence|decision)\s*\])?\s*:\s*(.+)$",
    re.IGNORECASE,
)

# Phrases that mean the missing thing is a judgement rather than a fact. Only
# untagged needs are read this way - the architect is asked to tag its own, and
# the tag wins - so this list exists for older threads and for rulings that
# ignore the format.
#
# It leans towards `decision`, deliberately. Calling a decision "evidence" sends
# a worker to fetch something that does not exist, and a worker that cannot find
# a thing is under real pressure to produce a plausible one; the architect then
# rules on an invention as though it had been observed. Calling evidence a
# "decision" only stops the thread and asks you, which is what happened before
# any of this existed. The cheap mistake is the one to make.
_DECISION_MARKERS = (
    "decision", "decide", "your call", "confirm", "choose", "which of",
    "go/no-go", "go / no-go", "approve", "sign-off", "sign off", "priorit",
    "objective", "acceptance criteri", "success criteri", "definition of done",
    "scope", "budget", "preference", "do you want", "should we", "intent",
    "trade-off", "tradeoff", "policy you", "rule from you", "direction",
    "the operator", "operator-side", "you would like", "you prefer",
)


def need_kind(need: str) -> str:
    """Who can answer this: `decision` means only the operator, else `evidence`.

    Nothing is classified `file` here. Whether a need is a file is not a property
    of how it is worded - it is whether the path is on disk, which resolve_needs
    settles by going and looking. A need that names a file we do not have is
    evidence, because a worker can still go and find out.
    """
    low = need.lower()
    return "decision" if any(m in low for m in _DECISION_MARKERS) else "evidence"


def parse_typed_needs(text: str) -> list[dict]:
    """The architect's own list of what it is missing, each with a kind."""
    out = []
    for line in text.splitlines():
        m = _NEED_RE.match(line.strip())
        if not m:
            continue
        tag, body = m.group(1), m.group(2).strip()
        out.append({"kind": (tag or "").lower() or need_kind(body), "text": body})
    return out


def parse_needs(text: str) -> list[str]:
    """The architect's own list of what it is missing."""
    return [n["text"] for n in parse_typed_needs(text)]


def resolve_needs(lane_name: str, needs: list[str]) -> tuple[str, list[str]]:
    """Answer every NEED: that names a file we can just go and read.

    An architect that has to wait a round for a path it already named is an
    architect spending your money on nothing.

    Returns what to send back, and which needs it covers - the rest have to be
    got some other way, and the caller has to know which those are.
    """
    cfg = config()
    lane = cfg["lanes"].get(lane_name) or {}
    roots = [ROOT / lane.get("path", ""), ROOT, WORKTREE_DIR / lane_name]
    chunks, answered = [], []
    for need in needs:
        # A path is the first token that looks like one; the rest is prose.
        cand = next((w.strip("`'\",") for w in need.split()
                     if "/" in w or w.strip("`'\",").endswith((".md", ".py", ".json", ".ex"))), None)
        if not cand:
            continue
        hit = next((r / cand for r in roots if (r / cand).is_file()), None)
        if not hit:
            continue
        body, clipped = _clip(hit.read_text(errors="replace"), 30000)
        chunks.append(f"### {cand}\n\n```\n{body}\n```" + ("\n\n(clipped)" if clipped else ""))
        answered.append(need)
    return "\n\n".join(chunks), answered


def open_consult(lane_name: str, question: str, *, model_key: str,
                 trigger: str = "manual", extra_files: list[str] | None = None) -> dict:
    """Build the packet, start the thread, and get the first ruling."""
    zpath, brief = build_packet(lane_name, question, extra_files or [])
    c = {
        "id": "c" + uuid.uuid4().hex[:10],
        "lane": lane_name,
        "model": model_key,
        "opened_at": now(),
        "status": "open",
        "trigger": trigger,
        "question": question,
        "cost_tokens": 0,
        "turns": [{"role": "packet", "at": now(), "text": brief, "packet": zpath.name}],
    }
    save_consult(c)
    return advance_consult(c["id"])


def advance_consult(cid: str) -> dict:
    """Send the whole thread to the architect and append what it says back."""
    with _CONSULT_LOCK:
        c = load_consult(cid)
        if not c:
            die(f"no consult {cid!r}")
        msgs = [{"role": "system", "content": CONSULT_SYSTEM}]
        for t in c["turns"]:
            if t["role"] == "gpt":
                msgs.append({"role": "assistant", "content": t["text"]})
            else:
                msgs.append({"role": "user", "content": TURN_PREFIX.get(t["role"], "") + t["text"]})

    resp = architect_chat(msgs, c["model"])
    text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or "(empty)"
    usage = resp.get("usage") or {}

    with _CONSULT_LOCK:
        c = load_consult(cid)
        c["turns"].append({"role": "gpt", "at": now(), "text": text,
                           "model": resp.get("model"), "usage": usage})
        c["needs"] = parse_needs(text)
        c["cost_tokens"] = c.get("cost_tokens", 0) + (usage.get("total_tokens") or 0)
        save_consult(c)
    write_ruling(c)

    return auto_continue(cid)


# How far a thread is allowed to carry itself without you. Resolving its own
# NEED: lines is the whole point of the loop; resolving them forever is a bill.
# Both bounds are per thread, and both are small enough that hitting one is
# something you read about rather than something you pay for.
AUTO_MAX_ROUNDS = 6
AUTO_TOKEN_CEILING = 500_000

# Everything that reaches the architect, and therefore spends. Listed here beside
# the loop it describes rather than written into the settings page, because a
# list of what something costs is worth nothing the moment it drifts from the
# code. `unattended` is the column that matters: those fire with no click.
ARCHITECT_ACTIONS = [
    {"action": "Ask GPT-5.6",
     "fires": "you click Ask GPT-5.6, or send a dock message to an open thread",
     "unattended": False},
    {"action": "Continue a thread",
     "fires": "you click Continue on a thread that stopped",
     "unattended": False,
     "note": f"then carries itself up to {AUTO_MAX_ROUNDS} more rounds"},
    {"action": "Ask about history",
     "fires": "you ask a question from the History panel",
     "unattended": False},
    {"action": "Plan a goal",
     "fires": "you open a goal - the architect writes the plan and the next task",
     "unattended": False},
    {"action": "Review a goal's work",
     "fires": "a goal's worker finishes; the architect judges it against the "
              "definition of done and decides what goes next",
     "unattended": True},
    {"action": "Worker reports back",
     "fires": "a worker you relayed a ruling to finishes; its report goes to the "
              "architect that sent it and the thread advances a round",
     "unattended": True},
    {"action": "Gathered evidence returns",
     "fires": "the architect said NEED[evidence], a worker fetched it, and the "
              "thread advances on the answer",
     "unattended": True},
    {"action": "Auto-escalation",
     "fires": "a worker stops blocked and no thread owns it, so one is opened",
     "unattended": True,
     "note": "already has its own switch: auto_escalate"},
]

# Reading the balance is a plain GET against OpenRouter and costs nothing, so it
# keeps working with the switch off - that is how you check what you saved.

# Set by the console to (cid, needs) -> bool. Gathering evidence means starting a
# worker, and only the console knows how to do that - amp.py cannot import it.
# Without the hook the loop still answers file needs; it just cannot fetch.
ON_CONSULT_NEEDS = None

# Why a thread stopped continuing itself. `operator` is the only one that is not
# a limit being hit: it means the architect asked for something that does not
# exist anywhere on this machine, and waiting is correct.
BLOCK_REASONS = {
    "operator": "waiting on a decision only you can make",
    "stalled": "the architect asked for the same things again, so the thread is "
               "going round rather than forward",
    "rounds": "this thread has already continued itself as far as it is allowed to",
    "tokens": "this thread has spent as much of the OpenRouter balance as it is allowed to",
    "no-gather": "nobody is wired up to send a worker for evidence",
    "gather-failed": "a worker could not be sent to gather what it asked for",
}


def _need_key(needs: list[str]) -> list[str]:
    """This round's request, in a form two rounds can be compared by."""
    return sorted(" ".join(n.lower().split())[:120] for n in needs)


def _block(cid: str, why: str | None) -> dict:
    with _CONSULT_LOCK:
        c = load_consult(cid)
        c["blocked_on"] = why
        save_consult(c)
    return c


def auto_continue(cid: str) -> dict:
    """Keep a consult moving on its own until only you can move it.

    A ruling that ended in NEED: lines used to be where a thread stopped. The
    file-shaped needs were answered off disk and everything else sat there until
    you happened to look - which is most of them, because most of what an
    architect asks for is evidence: command output, a test result, what the
    source actually says. A worker in the lane can go and get all of that, and
    the report comes back through the same relay a build order's report comes
    back through, for as many rounds as it takes.

    What it will not do is guess on your behalf. A need that is a decision - what
    to build, what counts as done, what to spend - halts the thread and says so,
    because that answer is not on this machine to be fetched.
    """
    c = load_consult(cid)
    needs = parse_typed_needs(last_ruling(c))
    if not needs:
        return _block(cid, None)

    rounds = c.get("auto_rounds", 0)
    trail = c.get("need_trail") or []
    key = _need_key([n["text"] for n in needs])
    if key in trail:
        return _halted(cid, "stalled", needs)
    if rounds >= AUTO_MAX_ROUNDS:
        return _halted(cid, "rounds", needs)
    if c.get("cost_tokens", 0) >= AUTO_TOKEN_CEILING:
        return _halted(cid, "tokens", needs)

    with _CONSULT_LOCK:
        c = load_consult(cid)
        c["auto_rounds"] = rounds + 1
        c["need_trail"] = (trail + [key])[-8:]
        save_consult(c)

    # Files first: anything already on disk is instant and free.
    supplied, answered = resolve_needs(c["lane"], [n["text"] for n in needs])
    if supplied:
        add_turn(cid, "supplied", supplied)
        return advance_consult(cid)

    # A tagged `file` we could not find is not a decision - a worker can still go
    # and look for it - so it is fetched like any other evidence.
    outstanding = [n for n in needs if n["text"] not in answered]
    evidence = [n["text"] for n in outstanding if n["kind"] != "decision"]
    if not evidence:
        return _halted(cid, "operator", outstanding)
    if not ON_CONSULT_NEEDS:
        return _halted(cid, "no-gather", outstanding)
    try:
        sent = ON_CONSULT_NEEDS(cid, evidence)
    except Exception as e:
        print(f"amp: gather hook failed: {e}", file=sys.stderr)
        sent = False
    if not sent:
        return _halted(cid, "gather-failed", outstanding)
    # A round can want both kinds at once. The worker goes for the evidence
    # straight away, and you are told about the decisions now rather than a round
    # later, so the two can be answered in parallel instead of in series.
    decisions = [n["text"] for n in outstanding if n["kind"] == "decision"]
    if decisions:
        add_note(f"consult {cid} ({c['lane']}) sent a worker for evidence, but also "
                 f"wants a decision only you can make: {'; '.join(decisions)[:400]}",
                 lane=c.get("lane"))
    return _block(cid, "gathering")


def _halted(cid: str, why: str, needs: list[dict]) -> dict:
    """Stop, and put the reason where the operator already looks.

    A thread that goes quiet is the bug being fixed here. Whatever stops it says
    so in the orchestrator feed, including - especially - when what stopped it is
    that it needs an answer from you.
    """
    c = _block(cid, why)
    wanted = "; ".join(n["text"] for n in needs)[:400]
    add_note(f"consult {cid} ({c['lane']}) stopped: {BLOCK_REASONS.get(why, why)}. "
             f"It is still missing: {wanted}", lane=c.get("lane"))
    return c


def _elsewhere(lane_name: str | None) -> str:
    """The other repositories in this workspace, named, with their paths.

    Without this a worker asked about a path in a different repository looks,
    finds nothing - correctly, it is not in its worktree - and reports NOT
    FOUND. The architect cannot tell that apart from the thing being absent, so
    it rules that the evidence does not exist and closes on it. That happened:
    six artifacts were declared unreproducible while all six sat on disk one
    directory over. Naming the other lanes turns "I cannot see it" into a
    different sentence from "it is not there".
    """
    cfg = config()
    others = [(n, ROOT / (l.get("path") or n))
              for n, l in (cfg.get("lanes") or {}).items() if n != lane_name]
    if not others:
        return ""
    rows = "\n".join(f"  {n:<14} {p}" for n, p in sorted(others))
    return (
        "\n\nThis workspace holds several separate git repositories, one per lane. "
        f"You are in the {lane_name!r} lane's worktree and can see only that "
        "repository. The others are real and on disk, at:\n\n" + rows + "\n\n"
        "If an item asks about a path in one of those, do not report it as missing - "
        "you are simply not looking at it. Write `OUT OF THIS LANE:` under that item, "
        "say which lane owns it, and say that a worker in that lane could answer it. "
        "`NOT FOUND` and `OUT OF THIS LANE` mean opposite things to the architect, "
        "and it will close a line of work on the first one."
    )


def gather_prompt(needs: list[str], lane_name: str | None = None) -> str:
    """A read-only errand: get exactly these things, and say so if they are not there."""
    items = "\n".join(f"{i}. {n}" for i, n in enumerate(needs, 1))
    return (
        "A consulting architect (GPT-5.6) is ruling on this lane and has stopped, because "
        "it is missing the things below and will not guess at them. Go and get them.\n\n"
        "This turn is for gathering only. Do not edit files, do not implement anything, "
        "do not fix what you find - report it.\n\n"
        "--- needed ---\n\n" + items + "\n\n--- end needed ---\n\n"
        "Answer each item by its number. For a command, give the exact command and its "
        "real output. For a file, give the exact path and the part that matters. If "
        "something does not exist or cannot be determined from this worktree, write "
        "`NOT FOUND:` under that item and say what you looked at.\n\n"
        "A plain `NOT FOUND` is worth far more here than a plausible answer: the "
        "architect will rule on whatever you report, so anything you infer rather than "
        "observe becomes a decision made on evidence that does not exist. Do not "
        "summarise away detail it asked for by name.\n\n"
        "One thing it may not know about you: you are in an isolated worktree cut from "
        "committed HEAD, so you cannot see uncommitted work in the operator's checkout. "
        "If an item is really about that - what is uncommitted, what is stashed, what is "
        "untracked over there - say so under the item instead of reporting your own "
        "worktree's answer as though it were theirs."
        + _elsewhere(lane_name)
    )


def add_turn(cid: str, role: str, text: str, **extra) -> dict:
    with _CONSULT_LOCK:
        c = load_consult(cid)
        if not c:
            die(f"no consult {cid!r}")
        c["turns"].append({"role": role, "at": now(), "text": text, **extra})
        # You answering is the thread moving for a real reason, so it gets its
        # automatic rounds back - the budget is on the loop running unattended,
        # not on the conversation being long.
        if role == "you":
            c["auto_rounds"] = 0
            c["need_trail"] = []
        save_consult(c)
    return c


def last_ruling(c: dict) -> str:
    for t in reversed(c.get("turns", [])):
        if t["role"] == "gpt":
            return t["text"]
    return ""


def write_ruling(c: dict):
    """A consult also lands as a readable file, one per thread, rewritten each round."""
    RULING_DIR.mkdir(parents=True, exist_ok=True)
    body = [f"# GPT-5.6 consult: {c['lane']}\n",
            f"- id: {c['id']}\n- model: {c['model']}\n- opened: {c['opened_at']}\n"
            f"- trigger: {c.get('trigger')}\n- tokens: {c.get('cost_tokens')}\n"]
    for t in c["turns"]:
        head = {"packet": "Packet", "gpt": "Ruling", "you": "You",
                "supplied": "Supplied", "worker": "Worker"}.get(t["role"], t["role"])
        body.append(f"\n## {head} — {t['at']}\n\n{t['text']}\n")
    (RULING_DIR / f"{c['lane']}-{c['id']}.md").write_text("".join(body))


def worker_report(lane_name: str, rec: dict) -> str:
    """What the worker did, in the form the architect needs to judge it."""
    cfg = config()
    lane = cfg["lanes"].get(lane_name) or {}
    out = [f"status: {rec.get('status')}   turns: {rec.get('num_turns')}   "
           f"cost: ${rec.get('cost_usd') or 0:.4f}",
           "", (rec.get("result") or rec.get("error") or "(no output)")[:8000]]
    diff = claude_diff(lane_name, lane)
    if diff:
        out += ["", "## What it changed", "", "```diff", diff[:30000], "```"]
    else:
        out += ["", "It changed nothing in the worktree."]
    return "\n".join(out)


def ruling_prompt(c: dict) -> str:
    """The ruling, addressed to the worker that has to carry it out."""
    return (
        "A consulting architect (GPT-5.6) reviewed this lane and issued the ruling below. "
        "Carry out its build order in this worktree.\n\n"
        "If a step is impossible, or the ruling is wrong about this codebase, stop and say "
        "so on a line beginning with `BLOCKED:` and explain what it got wrong. Do not "
        "improvise around it - the architect will be shown what you say and will rule again."
        "\n\n--- ruling ---\n\n" + last_ruling(c) + "\n\n--- end ruling ---"
    )


# What makes a finished worker worth escalating. A worker that never got a turn
# in failed on this machine, not on the engineering, so it is not a question for
# an architect.
ESCALATE_MARKERS = ("BLOCKED:", "ESCALATE:")


def needs_escalation(rec: dict) -> str | None:
    """Why this finished task should go to GPT-5.6, or None."""
    if rec.get("backend") != "claude" or rec.get("killed"):
        return None                       # you stopped it, or a limit did
    body = rec.get("result") or ""
    for m in ESCALATE_MARKERS:
        if m in body:
            return body[body.index(m):].splitlines()[0][:300]
    if rec.get("status") == "failed" and rec.get("num_turns"):
        return f"the worker failed after {rec['num_turns']} turns: {(rec.get('error') or '')[:200]}"
    return None


# ---------------------------------------------------------------- goals
#
# A consult is a conversation about a lane. A goal is an objective for one, and
# the difference is that a goal knows when it is finished. It carries three
# things a prompt does not: a definition of done written before any work starts,
# an ordered task list, and a record of which done-items are actually met and by
# what evidence. The architect plans it and judges it, workers carry it out, and
# the loop between them runs without you until it finishes or hits a bound.
#
# The one rule that keeps this honest: a done-item may carry a `check`, a shell
# command run in the lane's worktree. Where there is a check, "met" means the
# command exited zero, and nobody's opinion overrides that. Where there is not,
# "met" is the architect's judgement of a worker's report, and it is recorded as
# such rather than dressed up as a test.

# GOAL_DIR is bound by _bind_state; see _STATE_LAYOUT.
_GOAL_LOCK = threading.Lock()

# The same shape of bound as a consult, for the same reason: this spends real
# money while you are not looking. A goal that hits one stops and says so.
GOAL_MAX_ROUNDS = 14
GOAL_TOKEN_CEILING = 900_000
GOAL_CHECK_TIMEOUT = 180

# Set by the console to (goal, task) -> dispatch result. Starting a worker is
# the console's job; amp.py cannot import it.
ON_GOAL_DISPATCH = None

GOAL_STOPPED = {
    "operator": "it needs a decision only you can make",
    "rounds": "it has run as many rounds as it is allowed to without you",
    "tokens": "it has spent as much of the OpenRouter balance as it is allowed to",
    "no-dispatch": "nobody is wired up to start workers for it",
    "dispatch-failed": "a worker could not be started for its next task",
    "no-plan": "the architect did not return a plan that could be read",
}

GOAL_PLAN_SYSTEM = (
    "You are the consulting architect for the [&] protocol stack. The operator has given "
    "you one objective for one codebase. You are not doing the work: Claude workers with "
    "shell and editor access in an isolated git worktree will, one task at a time, and you "
    "will judge each result against the definition of done you write now.\n\n"
    "Reply with one JSON object and nothing else:\n"
    "{\n"
    '  "done": [{"text": "a single checkable condition", "check": "shell command or null"}],\n'
    '  "tasks": [{"text": "one instruction to one worker, in order"}],\n'
    '  "questions": ["a decision only the operator can make"]\n'
    "}\n\n"
    "Rules:\n"
    "- `done` is the definition of done. Each item is one condition that is either true or "
    "false about the repository, not an activity. Prefer conditions a command can settle, "
    "and put that command in `check` - it is run in the worktree and its exit code is what "
    "counts. Use null where no command can honestly settle it.\n"
    "- `tasks` are sized for one worker session of under twenty minutes each. Order them so "
    "each one is possible when it is reached. Six or fewer to start; you can add more after "
    "you see what comes back.\n"
    "- `questions` is for what you genuinely cannot decide: what the operator wants built, "
    "what counts as good enough, what to spend. Asking one stops everything until they "
    "answer, so ask only for what a worker could not go and find out.\n"
    "- Ground every item in the repository state you were given. Do not invent files, tests, "
    "or commands that are not evidenced there.\n"
    "- The operator's doctrine is included below the objective. Plan under it: where a "
    "done-item is a claim about status, write it in the vocabulary of the evidence ladder "
    "(`spec`, `in_tree`, `live_local`, `live_deployed`, `external`) and make the check the "
    "thing that settles which rung it is on. Never write a done-item that says \"done\"."
)

GOAL_REVIEW_SYSTEM = (
    "You are the consulting architect judging progress against a definition of done you "
    "wrote. You are given the objective, the definition of done with the result of any "
    "checks that were run, the task list, and what the last worker reported.\n\n"
    "Reply with one JSON object and nothing else:\n"
    "{\n"
    '  "done": [{"text": "exactly the item text you were given", "met": true,\n'
    '            "evidence": "what makes it true - a check result, a diff, a file"}],\n'
    '  "tasks": [{"text": "...", "state": "todo|done|dropped"}],\n'
    '  "verdict": "continue|done|blocked",\n'
    '  "why": "one or two sentences",\n'
    '  "questions": ["a decision only the operator can make"],\n'
    '  "doctrine": {"bearing": "advanced|contradicted|proposed|none",\n'
    '               "text": "what it is, in one or two sentences, or \\"\\" for none"}\n'
    "}\n\n"
    "Rules:\n"
    "- Return every done-item, in the order given, with its text unchanged. A check that "
    "was run outranks your reading of any report: if it failed, the item is not met, "
    "whatever the worker said about it.\n"
    "- `met: true` on an item with no check means you are asserting it from evidence in the "
    "report. Say what that evidence is. If the report does not contain it, the item is not "
    "met and the next task is to produce it.\n"
    "- Return the task list with states updated, and append new tasks if what came back "
    "showed work that is genuinely needed. Do not pad it: a goal ends.\n"
    "- `verdict` is `done` only when every done-item is met. Use `blocked` when no worker "
    "can make progress without an operator decision, and put that in `questions`.\n"
    "- `doctrine` is what this round bears on the operator's doctrine, included below the "
    "objective. `advanced` names a claim and the rungs it moved between; `contradicted` "
    "names what is now known to be false or weaker than stated, and outranks everything "
    "else in this reply; `proposed` states a new or amended value as a claim that could be "
    "wrong, and says what would test it. `none` is the ordinary answer and is expected most "
    "rounds. Do not manufacture a finding to have one - that is the fabrication the doctrine "
    "forbids. You propose amendments; only the operator adopts them."
)


def goal_path(gid: str) -> Path:
    return GOAL_DIR / f"{Path(gid).name}.json"


def load_goal(gid: str) -> dict | None:
    p = goal_path(gid)
    return json.loads(p.read_text()) if p.is_file() else None


def save_goal(g: dict):
    GOAL_DIR.mkdir(parents=True, exist_ok=True)
    g["updated_at"] = now()
    save_json(goal_path(g["id"]), g)


def goals(lane: str | None = None) -> list[dict]:
    """Summaries, newest first."""
    if not GOAL_DIR.exists():
        return []
    out = []
    for p in GOAL_DIR.glob("*.json"):
        try:
            g = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if lane and g.get("lane") != lane:
            continue
        dod = g.get("done") or []
        tasks = g.get("tasks") or []
        out.append({
            "id": g["id"], "lane": g.get("lane"), "objective": g.get("objective"),
            "state": g.get("state"), "opened_at": g.get("opened_at"),
            "updated_at": g.get("updated_at"), "rounds": g.get("rounds", 0),
            "cost_tokens": g.get("cost_tokens", 0),
            "stopped_on": g.get("stopped_on"),
            "stopped_why": GOAL_STOPPED.get(g.get("stopped_on") or ""),
            "questions": g.get("questions") or [],
            "met": sum(1 for d in dod if d.get("met")), "dod": len(dod),
            "tasks_done": sum(1 for t in tasks if t.get("state") == "done"),
            "tasks_total": sum(1 for t in tasks if t.get("state") != "dropped"),
            "now": next((t["text"] for t in tasks if t.get("state") == "running"), None),
        })
    out.sort(key=lambda g: g.get("opened_at") or "", reverse=True)
    return out


def goal_brief(lane_name: str) -> str:
    """The repository as it actually is, for planning against.

    Deliberately small and factual. A plan written against an invented file is
    worse than no plan, because the first worker spends a session discovering it.
    """
    cfg = config()
    lane = cfg["lanes"].get(lane_name) or {}
    repo = (ROOT / lane.get("path", ".")).resolve()
    wt = WORKTREE_DIR / lane_name
    where = wt if wt.exists() else repo
    parts = [f"# Lane `{lane_name}`",
             f"- repo: {lane.get('repo') or lane.get('path')}",
             f"- path: {repo}",
             f"- base branch: {lane.get('branch') or 'main'}",
             f"- worker worktree: {where}"]

    def cap(cmd, title, limit=3000):
        p = run(cmd, cwd=where)
        body = (p.stdout or p.stderr or "").strip()
        if body:
            parts.append(f"\n## {title}\n```\n{body[:limit]}\n```")

    cap(["git", "log", "--oneline", "-12"], "Recent commits")
    cap(["git", "status", "--short"], "Working tree")
    cap(["ls", "-1"], "Top level")
    for rel in ("README.md", "docs/spec/README.md", "AGENTS.md", "CLAUDE.md"):
        f = where / rel
        if f.is_file():
            try:
                head = "\n".join(f.read_text().splitlines()[:60])
            except (OSError, UnicodeDecodeError):
                continue
            parts.append(f"\n## {rel} (first 60 lines)\n```\n{head[:4000]}\n```")
    return "\n".join(parts)


def _json_reply(text: str) -> dict | None:
    """The one JSON object in a model's reply, fences and preamble and all."""
    body = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", body, re.S)
    if fence:
        body = fence.group(1).strip()
    start = body.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(body[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    out = json.loads(body[start:i + 1])
                except json.JSONDecodeError:
                    return None
                return out if isinstance(out, dict) else None
    return None


def _goal_chat(g: dict, system: str, user: str) -> tuple[dict | None, str]:
    resp = architect_chat([{"role": "system", "content": system},
                            {"role": "user", "content": user}], g["model"])
    text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    usage = resp.get("usage") or {}
    with _GOAL_LOCK:
        cur = load_goal(g["id"]) or g
        cur["cost_tokens"] = cur.get("cost_tokens", 0) + (usage.get("total_tokens") or 0)
        cur["rounds"] = cur.get("rounds", 0) + 1
        save_goal(cur)
    return _json_reply(text), text


def open_goal(lane_name: str, objective: str, *, model_key: str | None = None) -> dict:
    """State an objective for a lane. The architect turns it into a plan."""
    cfg = config()
    if lane_name not in cfg["lanes"]:
        die(f"unknown lane {lane_name!r}")
    g = {
        "id": "g" + uuid.uuid4().hex[:10],
        "lane": lane_name,
        "objective": objective.strip(),
        "model": model_key or cfg.get("consult_model", DEFAULT_CONSULT),
        "opened_at": now(),
        "state": "planning",
        "done": [], "tasks": [], "questions": [],
        "rounds": 0, "cost_tokens": 0,
        "log": [], "stopped_on": None,
    }
    save_goal(g)
    return plan_goal(g["id"])


def goal_log(gid: str, text: str, **extra) -> dict:
    with _GOAL_LOCK:
        g = load_goal(gid)
        if not g:
            die(f"no goal {gid!r}")
        g.setdefault("log", []).append({"at": now(), "text": text, **extra})
        g["log"] = g["log"][-60:]
        save_goal(g)
    return g


def _goal_stop(gid: str, why: str | None, note: str = "") -> dict:
    with _GOAL_LOCK:
        g = load_goal(gid)
        g["stopped_on"] = why
        if why:
            g["state"] = "blocked"
        save_goal(g)
    if why:
        goal_log(gid, f"stopped: {GOAL_STOPPED.get(why, why)}" + (f" {note}" if note else ""))
        add_note(f"goal {gid} ({g['lane']}) stopped: {GOAL_STOPPED.get(why, why)}."
                 + (f" {note}" if note else ""), lane=g.get("lane"))
    return g


def plan_goal(gid: str) -> dict:
    g = load_goal(gid)
    user = (f"# Objective\n\n{g['objective']}"
            + mission_block()
            + doctrine_block("What this work is held to")
            + f"\n\n# The repository right now\n\n{goal_brief(g['lane'])}")
    plan, raw = _goal_chat(g, GOAL_PLAN_SYSTEM, user)
    if not plan:
        goal_log(gid, "the architect's plan could not be read as JSON", raw=raw[:2000])
        return _goal_stop(gid, "no-plan")
    with _GOAL_LOCK:
        g = load_goal(gid)
        g["done"] = [{"text": str(d.get("text") or "").strip(),
                      "check": (d.get("check") or None), "met": False, "evidence": ""}
                     for d in (plan.get("done") or []) if (d.get("text") or "").strip()]
        g["tasks"] = [{"id": "t" + uuid.uuid4().hex[:6],
                       "text": str(t.get("text") or "").strip(), "state": "todo"}
                      for t in (plan.get("tasks") or []) if (t.get("text") or "").strip()]
        g["questions"] = [q for q in (plan.get("questions") or []) if str(q).strip()]
        g["state"] = "running"
        save_goal(g)
    goal_log(gid, f"planned: {len(g['done'])} done-conditions, {len(g['tasks'])} tasks")
    if g["questions"]:
        return _goal_stop(gid, "operator", "It asks: " + "; ".join(g["questions"])[:400])
    if not g["tasks"]:
        return _goal_stop(gid, "no-plan", "the plan had no tasks in it")
    return goal_dispatch(gid)


# What to tell a worker about the clock, learned the hard way.
#
# A worker stopped by a limit keeps everything it wrote to disk and loses
# everything it was about to say - so an uncommitted worktree is the one state
# where a kill is expensive. Telling a worker to commit early fixes that, and
# creates a second problem: it will commit work it has not built. That is not
# hypothetical. An ic32.c optimisation was cut off mid-rename, sat unverified
# through two more kills, was committed on an early-commit instruction, and only
# a run that survived long enough to call the compiler found that it declared
# one name and used another. So the instruction has to carry both halves: commit
# so the work survives, then verify, then fix or amend - and never end holding
# a commit nobody built.
LANDING_RULE = (
    "You have about {mins} minutes of wall clock before you are stopped. Being "
    "stopped keeps everything you have written to disk and loses everything you "
    "were about to say, so work in small steps and land them as you go:\n\n"
    "1. Commit as soon as you have something coherent, before you verify it. A "
    "commit is how work survives you, not a claim that it works.\n"
    "2. Then build it and run its tests. A commit you have not built is the one "
    "thing worse than uncommitted work, because it looks finished.\n"
    "3. If it fails, fix it and amend, or revert that part and say so. Do not "
    "leave the branch holding a commit nobody has run.\n\n"
    "If you are running long, stop early and report where you got to - a partial "
    "result that is committed and reported is worth more than a complete one "
    "that is cut off."
)


def landing_rule() -> str:
    return LANDING_RULE.format(mins=limits()["timeout_s"] // 60)


def goal_worker_prompt(g: dict, task: dict) -> str:
    dod = "\n".join(f"- {d['text']}" + (f"   [checked by: `{d['check']}`]" if d.get("check") else "")
                    for d in g["done"]) or "- (none stated)"
    done_before = [t["text"] for t in g["tasks"] if t.get("state") == "done"]
    prior = ("\n\nAlready done in this goal by earlier workers:\n"
             + "\n".join(f"- {t}" for t in done_before)) if done_before else ""
    return (
        f"You are one worker on a shared goal in this repository.\n\n"
        f"# The goal\n\n{g['objective']}"
        + mission_block()
        + doctrine_block("What your work is held to")
        + f"\n\n# It is finished when all of these are true\n\n{dod}\n\n"
        f"# Your task, and only this one\n\n{task['text']}{prior}\n\n"
        f"Do this task. Do not do the other tasks - other workers have them, and two "
        f"agents editing the same files is how this goes wrong.\n\n"
        f"{landing_rule()}\n\n"
        f"When you finish, report: what you changed, what you ran and what it printed, and "
        f"which of the done-conditions above you believe your work makes true, with the "
        f"evidence for each. If you could not do the task, say so on a line beginning "
        f"`BLOCKED:` and say exactly what stopped you. Do not improvise a different task.\n\n"
        f"End your report with a section headed `DOCTRINE:` on its own line, whose first "
        f"word is one of `advanced`, `contradicted`, `proposed` or `none`, followed by a "
        f"sentence or two. `none` means this was ordinary work, and is the right answer most "
        f"of the time - do not invent a finding to fill the section."
    )


def goal_dispatch(gid: str) -> dict:
    """Send the next task out. One worker per goal at a time, by construction."""
    g = load_goal(gid)
    if g.get("state") not in ("running",):
        return g
    if any(t.get("state") == "running" for t in g["tasks"]):
        return g                                   # one is already out
    nxt = next((t for t in g["tasks"] if t.get("state") == "todo"), None)
    if not nxt:
        return goal_review(gid, "every task in the list is finished")
    if g.get("rounds", 0) >= GOAL_MAX_ROUNDS:
        return _goal_stop(gid, "rounds")
    if g.get("cost_tokens", 0) >= GOAL_TOKEN_CEILING:
        return _goal_stop(gid, "tokens")
    if not ON_GOAL_DISPATCH:
        return _goal_stop(gid, "no-dispatch")
    with _GOAL_LOCK:
        g = load_goal(gid)
        for t in g["tasks"]:
            if t["id"] == nxt["id"]:
                t["state"] = "running"
        save_goal(g)
    try:
        out = ON_GOAL_DISPATCH(g, nxt)
    except Exception as e:
        print(f"amp: goal dispatch hook failed: {e}", file=sys.stderr)
        out = {"ok": False, "error": str(e)}
    if not out.get("ok"):
        with _GOAL_LOCK:
            g = load_goal(gid)
            for t in g["tasks"]:
                if t["id"] == nxt["id"]:
                    t["state"] = "todo"
            save_goal(g)
        return _goal_stop(gid, "dispatch-failed", str(out.get("error") or "")[:200])
    return goal_log(gid, ("queued" if out.get("queued") else "dispatched")
                    + f" a worker for: {nxt['text'][:200]}",
                    task_id=out.get("task_id"), goal_task=nxt["id"])


def run_goal_checks(gid: str) -> list[dict]:
    """Run every done-item's check in the lane worktree. Exit code is the answer."""
    g = load_goal(gid)
    wt = WORKTREE_DIR / g["lane"]
    if not wt.exists():
        return []
    out = []
    for d in g["done"]:
        cmd = d.get("check")
        if not cmd:
            continue
        try:
            p = subprocess.run(cmd, shell=True, cwd=wt, capture_output=True, text=True,
                               timeout=GOAL_CHECK_TIMEOUT, env={**os.environ, **claude_env()})
            body = ((p.stdout or "") + (p.stderr or "")).strip()
            out.append({"text": d["text"], "check": cmd, "exit": p.returncode,
                        "output": body[-1500:]})
        except subprocess.TimeoutExpired:
            out.append({"text": d["text"], "check": cmd, "exit": None,
                        "output": f"(no result after {GOAL_CHECK_TIMEOUT}s - timed out)"})
        except OSError as e:
            out.append({"text": d["text"], "check": cmd, "exit": None,
                        "output": f"(could not run: {e})"})
    with _GOAL_LOCK:
        g = load_goal(gid)
        by_text = {c["text"]: c for c in out}
        for d in g["done"]:
            c = by_text.get(d["text"])
            if not c:
                continue
            # A check is the only thing here that outranks a judgement, in both
            # directions: passing is not evidence of intent, but failing is proof
            # the item is not met, whatever anyone reported.
            d["check_exit"] = c["exit"]
            d["check_at"] = now()
            if c["exit"] != 0:
                d["met"] = False
        save_goal(g)
    return out


def goal_state_doc(g: dict, checks: list[dict], report: str) -> str:
    dod = []
    by_text = {c["text"]: c for c in checks}
    for d in g["done"]:
        line = f"- [{'x' if d.get('met') else ' '}] {d['text']}"
        if d.get("check"):
            c = by_text.get(d["text"])
            if c:
                line += (f"\n      check `{c['check']}` -> exit {c['exit']}\n"
                         f"      output: {c['output'][:800] or '(nothing)'}")
            else:
                line += f"\n      check `{d['check']}` (not run this round)"
        if d.get("evidence"):
            line += f"\n      previously: {d['evidence'][:300]}"
        dod.append(line)
    tasks = "\n".join(f"- [{t.get('state')}] {t['text']}" for t in g["tasks"])
    return (f"# Objective\n\n{g['objective']}"
            + mission_block()
            + doctrine_block("What this work is held to")
            + f"\n\n# Definition of done\n\n" + "\n".join(dod) + "\n\n"
            f"# Tasks\n\n{tasks}\n\n"
            f"# What just came back\n\n{report[:20000]}")


def mark_reviewing(gid: str, on: bool):
    """Stamp who is judging this goal right now, so an idle one can be told apart.

    Between "the worker finished" and "reviewed:" a goal has no running task and
    unfinished tasks - which is indistinguishable, from the outside, from a goal
    that was dropped mid-review by a console restart. The distinguishing fact is
    whether the process that took the review is still this one. Elapsed time
    cannot answer it: a review runs every done-item's check, and eight checks at
    the 180s ceiling is longer than any timeout worth waiting.
    """
    with _GOAL_LOCK:
        g = load_goal(gid)
        if not g:
            return
        g["reviewing_pid"] = os.getpid() if on else None
        g["reviewing_since"] = now() if on else None
        save_goal(g)


def goal_review(gid: str, report: str) -> dict:
    """Judge what came back against the definition of done, then keep going."""
    mark_reviewing(gid, True)
    try:
        return _goal_review(gid, report)
    finally:
        mark_reviewing(gid, False)


def _goal_review(gid: str, report: str) -> dict:
    g = load_goal(gid)
    if g.get("cost_tokens", 0) >= GOAL_TOKEN_CEILING:
        return _goal_stop(gid, "tokens")
    if g.get("rounds", 0) >= GOAL_MAX_ROUNDS:
        return _goal_stop(gid, "rounds")
    checks = run_goal_checks(gid)
    g = load_goal(gid)
    verdict, raw = _goal_chat(g, GOAL_REVIEW_SYSTEM, goal_state_doc(g, checks, report))
    if not verdict:
        goal_log(gid, "the architect's review could not be read as JSON", raw=raw[:2000])
        return _goal_stop(gid, "no-plan", "its review was not readable")

    with _GOAL_LOCK:
        g = load_goal(gid)
        stated = {str(d.get("text") or "").strip(): d for d in (verdict.get("done") or [])}
        for d in g["done"]:
            said = stated.get(d["text"])
            if not said:
                continue
            met = bool(said.get("met"))
            if d.get("check") and d.get("check_exit") not in (0, None):
                met = False           # the command already answered this
            d["met"] = met
            if said.get("evidence"):
                d["evidence"] = str(said["evidence"])[:600]
        # Task list: keep ids for the ones we already know, append the rest.
        known = {t["text"]: t for t in g["tasks"]}
        fresh = []
        for t in (verdict.get("tasks") or []):
            text = str(t.get("text") or "").strip()
            if not text:
                continue
            state = t.get("state") if t.get("state") in ("todo", "done", "dropped") else "todo"
            if text in known:
                old = known.pop(text)
                # A worker is out on this one; a review does not get to retire it.
                if old.get("state") != "running":
                    old["state"] = state
                fresh.append(old)
            else:
                fresh.append({"id": "t" + uuid.uuid4().hex[:6], "text": text, "state": state})
        for leftover in known.values():
            if leftover.get("state") == "running":
                fresh.append(leftover)
        g["tasks"] = fresh
        g["questions"] = [q for q in (verdict.get("questions") or []) if str(q).strip()]
        g["last_verdict"] = {"verdict": verdict.get("verdict"), "why": verdict.get("why"),
                             "at": now()}
        save_goal(g)

    why = str(verdict.get("why") or "")[:400]
    goal_log(gid, f"reviewed: {verdict.get('verdict')} - {why}")

    d = verdict.get("doctrine")
    if isinstance(d, dict):
        f = record_finding(str(d.get("bearing") or ""), str(d.get("text") or ""),
                           lane=g.get("lane"), source="architect", goal_id=gid)
        if f:
            goal_log(gid, f"doctrine: {f['bearing']} - {f['text'][:300]}")

    met = all(d.get("met") for d in g["done"]) and bool(g["done"])
    if verdict.get("verdict") == "done" and met:
        with _GOAL_LOCK:
            g = load_goal(gid)
            g["state"], g["stopped_on"] = "done", None
            save_goal(g)
        add_note(f"goal {gid} ({g['lane']}) is finished: {g['objective'][:200]}",
                 lane=g.get("lane"))
        # And immediately ask what it was for. This is the only moment the whole
        # goal is in front of the architect with its evidence attached, which is
        # what makes "is there anywhere left to go here" answerable rather than a
        # guess. It must not be able to unfinish the goal: a review that throws
        # loses a proposal, not a completion.
        try:
            direction_review(gid, auto=True)
        except Exception as e:                                    # noqa: BLE001
            goal_log(gid, f"the direction review failed ({e}); the goal is still finished")
        return load_goal(gid) or g
    if verdict.get("verdict") == "done" and not met:
        # Said done, is not done. The definition of done is the authority here.
        unmet = [d["text"] for d in g["done"] if not d.get("met")]
        goal_log(gid, "the architect called it done while these are still unmet: "
                      + "; ".join(unmet)[:400])
    if verdict.get("verdict") == "blocked" or g["questions"]:
        return _goal_stop(gid, "operator",
                          "It asks: " + "; ".join(g["questions"])[:400] if g["questions"] else why)
    return goal_dispatch(gid)


def idle_goals() -> list[dict]:
    """Goals that are running, have work left, and have nobody doing any of it.

    This is the shape every silent halt has taken. A goal stops moving because
    the step that was going to move it - a review, a dispatch - was interrupted,
    and nothing afterwards asks whether anyone is still on it. The goal keeps
    saying `running`, the board shows no worker, and the whole thing reads as
    healthy right up until you count the workers and find none.

    Deliberately NOT time-based. A goal is idle when no task is out and no live
    process is judging it, both of which are facts rather than estimates.
    """
    out = []
    for row in goals():
        if row.get("state") != "running":
            continue
        g = load_goal(row["id"])
        if not g:
            continue
        if any(t.get("state") == "running" for t in g.get("tasks") or []):
            continue
        if not any(t.get("state") == "todo" for t in g.get("tasks") or []):
            continue                      # nothing to send; a review will close it
        pid = g.get("reviewing_pid")
        if pid and pid == os.getpid():
            continue                      # this process is judging it right now
        if pid and pid_alive(pid):
            continue                      # some other live console has it
        if g.get("hold_until") and g["hold_until"] > now():
            continue                      # waiting out a capacity limit
        out.append(g)
    return out


# ------------------------------------------------------- waiting out a limit
#
# There are two reasons a worker can fail, and treating them the same is what
# takes the whole fleet down at once. A worker that failed AT THE WORK left
# something behind to judge, and the architect spends a round judging it. A
# worker that never got to start - the subscription window is spent, the API is
# overloaded - left nothing, and there is nothing to judge.
#
# The second kind arrives in every lane within seconds of the first, because
# they all draw on one account. Reviewing those failures costs a round per goal
# per attempt, and rounds are capped, so a limit lasting an hour ends with every
# goal stopped on `too many rounds` and no worker left running. Then the limit
# lifts and nothing restarts, because nothing is left to restart. That is the
# day-scale halt: the fleet does not crash, it spends itself out against a wall.
#
# So a capacity failure costs no round and no judgement. The task goes back to
# `todo`, the goal is held for a while, and the heartbeat picks it up when the
# hold expires - which is what a person would have done.

CAPACITY_MARKERS = (
    "usage limit", "rate limit", "rate_limit", "429", "529",
    "overloaded", "quota", "try again later", "temporarily unavailable",
)
CAPACITY_HOLD_S = 600


def capacity_problem(rec: dict) -> str | None:
    """Whether a worker failed for want of capacity rather than at the work."""
    if rec.get("status") not in ("failed", "cancelled"):
        return None
    # `error` and `killed` only, never the worker's own prose. A worker that
    # merely WROTE about rate limits has not hit one, and the same mistake -
    # scanning a report for markers the report is allowed to discuss - already
    # cost us once in `diagnose_session`.
    blob = " ".join(str(rec.get(k) or "") for k in ("error", "killed")).lower()
    return next((m for m in CAPACITY_MARKERS if m in blob), None)


def goal_hold(gid: str, seconds: int, why: str):
    """Put a goal down for a while without spending anything on it."""
    with _GOAL_LOCK:
        g = load_goal(gid)
        if not g:
            return
        for t in g["tasks"]:
            if t.get("state") == "running":
                t["state"] = "todo"       # it never ran; it is still owed
        g["hold_until"] = ts_in(seconds)
        save_goal(g)
    goal_log(gid, f"held {seconds // 60}m: {why}")


def ts_in(seconds: int) -> str:
    """A `now()`-shaped stamp `seconds` from now, so the two compare as strings."""
    return (datetime.now(timezone.utc)
            + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def goal_worker_done(gid: str, lane_name: str, rec: dict) -> dict:
    """A goal's worker stopped. Mark its task, then judge what it produced."""
    g = load_goal(gid)
    if not g:
        return {}
    task_id = rec.get("goal_task")
    limit = capacity_problem(rec)
    if limit:
        goal_hold(gid, CAPACITY_HOLD_S,
                  f"{limit} - the worker never started, so there is nothing to judge")
        return load_goal(gid) or {}
    killed = rec.get("killed") or (rec.get("status") == "failed")
    with _GOAL_LOCK:
        g = load_goal(gid)
        for t in g["tasks"]:
            if t["id"] == task_id or (not task_id and t.get("state") == "running"):
                t["state"] = "blocked" if killed else "done"
                t["task_id"] = rec.get("task_id")
                t["note"] = (rec.get("error") or "")[:300] if killed else ""
        save_goal(g)
    report = worker_report(lane_name, rec)
    if killed:
        report = (f"This worker did not finish: {rec.get('error') or 'it was stopped'}.\n"
                  f"Whatever is in the worktree below is as far as it got. Judge that, and "
                  f"if the task needs finishing, say so as a task - it can be resumed.\n\n"
                  + report)
    goal_log(gid, f"worker {str(rec.get('task_id'))[:8]} "
                  + ("was stopped" if killed else "finished") + f" ({rec.get('status')})",
             task_id=rec.get("task_id"))
    if not architect_available():
        # The task state above is already saved; only the architect's judgement
        # of it is skipped. Said in the goal's own log, because that is where
        # someone wondering why nothing happened next will be looking.
        goal_log(gid, f"not reviewed: {architect_off_reason().lower()}",
                 task_id=rec.get("task_id"))
        return load_goal(gid) or {}
    return goal_review(gid, report)


def answer_goal(gid: str, text: str) -> dict:
    """You answering is the thread moving for a real reason: it gets its rounds back."""
    with _GOAL_LOCK:
        g = load_goal(gid)
        if not g:
            die(f"no goal {gid!r}")
        g["questions"] = []
        g["stopped_on"] = None
        g["state"] = "running"
        g["rounds"] = 0
        save_goal(g)
    goal_log(gid, f"you answered: {text[:300]}")
    return goal_review(gid, f"The operator answers your questions:\n\n{text}")


def close_goal(gid: str, state: str = "abandoned") -> dict:
    with _GOAL_LOCK:
        g = load_goal(gid)
        if not g:
            die(f"no goal {gid!r}")
        g["state"] = state
        g["stopped_on"] = None
        save_goal(g)
    return g


# ------------------------------------------------------------------ publishing
#
# The end of the pipeline. Work finishes on `amp/<lane>` inside a worktree and
# until now stopped there - nothing in this harness has ever run `git push`.
#
# What this does NOT do is merge. A pull request is a handoff, not a decision.
# The ladder's top rungs are somebody's judgement about evidence, and a program
# that merged its own work would be awarding itself the rung it is supposed to
# be making a case for. So: push the branch, open the PR, write down what the
# evidence actually was, and stop.
#
# The gate is the goal's own definition of done, read the way the doctrine says
# to read a claim. A condition whose check PASSED is evidence. A condition with
# no check at all, or one whose check has never been run, is a claim at rung
# `spec` wearing the costume of `in_tree`, and it does not open a PR.
#
# The checks are re-run here rather than trusting the exit codes already stored
# on the goal. Those say the check passed once, against a tree that has since
# had more commits written into it - which is not the same as passing on what is
# about to be pushed, and the difference is the entire reason to have a gate.

PUBLISH_REMOTE = "origin"


def _git(wt: Path, *args: str) -> subprocess.CompletedProcess:
    return run(["git", "-C", str(wt), *args])


def publish_report(gid: str, *, rerun: bool = True) -> dict:
    """Decide whether a goal's work may be published, and say why not if not.

    Pure inspection plus the checks themselves - it pushes nothing and opens
    nothing. `publish_goal` calls this first and refuses on any blocker, so the
    dry run an operator reads and the gate the code enforces are the same code
    rather than two descriptions of one intention that drift apart.
    """
    g = load_goal(gid)
    if not g:
        return {"ok": False, "blocked": [f"no goal {gid!r}"]}
    lane = g["lane"]
    wt = WORKTREE_DIR / lane
    cfg = config()["lanes"].get(lane) or {}
    base = cfg.get("branch") or "main"
    branch = f"amp/{lane}"
    rep: dict = {
        "ok": False, "goal_id": gid, "lane": lane, "branch": branch, "base": base,
        "objective": (g.get("objective") or "").split("\n\n")[0],
        "worktree": str(wt), "blocked": [], "conditions": [], "commits": [],
        "repo": None, "ahead": 0, "dirty": 0, "pr_url": g.get("pr_url"),
    }
    if not wt.exists():
        rep["blocked"].append(f"no worktree at {wt} - nothing was ever built here")
        return rep

    r = _git(wt, "remote", "get-url", PUBLISH_REMOTE)
    if r.returncode != 0:
        rep["blocked"].append(f"lane {lane} has no {PUBLISH_REMOTE} remote")
    else:
        rep["repo"] = r.stdout.strip()

    # Uncommitted work is work nobody has reviewed and the PR would not contain.
    # Publishing around it would produce a pull request that is quietly not the
    # thing that was tested.
    dirty = [l for l in _git(wt, "status", "--porcelain").stdout.splitlines() if l.strip()]
    rep["dirty"] = len(dirty)
    if dirty:
        rep["blocked"].append(
            f"{len(dirty)} uncommitted file(s) in the worktree - the PR would not "
            f"contain them, so what gets reviewed would not be what was checked")

    ahead = _git(wt, "rev-list", "--count", f"{base}..{branch}")
    rep["ahead"] = int(ahead.stdout.strip() or 0) if ahead.returncode == 0 else 0
    if not rep["ahead"]:
        rep["blocked"].append(f"{branch} is not ahead of {base} - there is nothing to publish")
    else:
        rep["commits"] = _git(
            wt, "log", "--no-merges", "--format=%h %s", f"{base}..{branch}"
        ).stdout.strip().splitlines()

    if g.get("state") != "done":
        rep["blocked"].append(
            f"goal is {g.get('state')!r}, not done - publish it when it has finished "
            f"making its case, or close it and publish the lane by hand")

    # Only once nothing else has already ruled it out. Re-running the checks is
    # the expensive half of this function and it runs commands - `npm test` and
    # the like - inside the lane worktree. If the goal is still running there is
    # a worker writing to that tree right now, and racing a build against it
    # would be both a wrong answer and a way to break somebody else's work.
    if rerun and not rep["blocked"]:
        run_goal_checks(gid)
        g = load_goal(gid) or g
    elif rerun:
        rep["stale_checks"] = True
    dod = g.get("done") or []
    if not dod:
        rep["blocked"].append("this goal has no definition of done, so there is nothing to gate on")
    for d in dod:
        cond = {"text": d.get("text"), "check": d.get("check"),
                "exit": d.get("check_exit"), "met": bool(d.get("met"))}
        if not d.get("check"):
            cond["verdict"] = "no check - judgement only, rung `spec`"
        elif d.get("check_exit") is None:
            cond["verdict"] = "check has never run"
        elif d.get("check_exit") == 0:
            cond["verdict"] = "passed"
        else:
            cond["verdict"] = f"failed, exit {d.get('check_exit')}"
        rep["conditions"].append(cond)
    unproven = [c for c in rep["conditions"] if c.get("verdict") != "passed"]
    if unproven:
        rep["blocked"].append(
            f"{len(unproven)} of {len(rep['conditions'])} done-conditions are not "
            f"backed by a check that passed just now")

    rep["ok"] = not rep["blocked"]
    return rep


def publish_body(rep: dict, g: dict) -> str:
    """The pull request description: the case, and the evidence for it.

    Written from the goal rather than from the diff on purpose. A reviewer can
    already read the diff; what they cannot recover from it is which conditions
    this work was supposed to satisfy and which command was run to decide that
    it had.
    """
    lines = [(g.get("objective") or "").strip(), "",
             "## What had to be true", ""]
    for c in rep["conditions"]:
        lines.append(f"- [{'x' if c['met'] else ' '}] {c['text']}")
        if c.get("check"):
            lines.append(f"  - `{c['check']}` &rarr; **{c['verdict']}**")
        else:
            lines.append(f"  - {c['verdict']}")
    tasks = [t for t in (g.get("tasks") or []) if t.get("state") == "done"]
    if tasks:
        lines += ["", "## What was done", ""] + [f"- {t['text']}" for t in tasks]
    lines += [
        "", "## How this was produced", "",
        f"An `amp` worker on branch `{rep['branch']}`, {rep['ahead']} commit(s) "
        f"ahead of `{rep['base']}`. The conditions above were re-checked "
        f"immediately before this branch was pushed, in the worktree the work "
        f"was done in.",
        "",
        "Nothing merged this. The harness opens the pull request and stops there "
        "deliberately: whether this is good enough to land is a judgement, and "
        "the harness is not the one making it.",
    ]
    return "\n".join(lines)


def publish_goal(gid: str, *, dry_run: bool = True) -> dict:
    """Push the lane branch and open a pull request. Never merges."""
    rep = publish_report(gid)
    rep["dry_run"] = dry_run
    rep["pushed"] = False
    if not rep["ok"] or dry_run:
        return rep

    g = load_goal(gid)
    wt = Path(rep["worktree"])
    p = _git(wt, "push", "-u", PUBLISH_REMOTE, rep["branch"])
    if p.returncode != 0:
        rep["ok"] = False
        rep["blocked"].append(f"push failed: {(p.stderr or p.stdout).strip()[-600:]}")
        return rep
    rep["pushed"] = True

    title = f"{rep['objective'][:70]}" + ("…" if len(rep["objective"]) > 70 else "")
    pr = run(["gh", "pr", "create", "--base", rep["base"], "--head", rep["branch"],
              "--title", title, "--body", publish_body(rep, g)], cwd=wt)
    out = (pr.stdout + pr.stderr).strip()
    if pr.returncode != 0:
        # The branch is up either way, which is worth saying plainly rather than
        # reporting a clean failure - the push is not rolled back.
        rep["ok"] = False
        rep["blocked"].append(f"branch pushed, but opening the PR failed: {out[-600:]}")
        return rep
    url = next((w for w in out.split() if w.startswith("https://")), out)
    rep["pr_url"] = url
    with _GOAL_LOCK:
        g = load_goal(gid)
        g["pr_url"] = url
        g["published_at"] = now()
        g.setdefault("log", []).append({"at": now(), "text": f"opened a pull request: {url}"})
        save_goal(g)
    return rep


# ------------------------------------------------------------------ direction
#
# A goal answers "what has to be true next". Nothing so far answers "and then
# what" - so the pipeline runs out of direction silently, by having no goals
# left, which looks exactly like being finished.
#
# Direction is the layer above goals. It is four things kept apart on purpose:
#
#   - the THESIS: the bet the whole stack is making, one paragraph
#   - the VALUES: what work is held to on the way there
#   - the FINDINGS: what the work has actually said back about both
#   - the OPEN QUESTIONS: what we believe but have not settled
#
# The first two are DOCTRINE.md and are read from it rather than restated here,
# because a second copy of the values is a second thing to keep true. The third
# already exists. The fourth is half in DOCTRINE.md under "Open theses" and half
# proposed by the architect as work reveals it.
#
# The loop this closes: when a goal finishes, look back at all four and ask
# whether this lane has anywhere left to travel. The answer is a PROPOSAL, not a
# goal. Rule 6 says what to build is Travis's decision, and a system that turned
# its own review into its own next objective would be deciding what to build by
# having decided what to review. `auto_adopt` exists for when he says otherwise,
# and defaults off.

# DIRECTION_PATH is bound by _bind_state; see _STATE_LAYOUT.
_DIRECTION_LOCK = threading.Lock()
_SECTION_HEAD = re.compile(r"^##\s+(.+?)\s*$", re.M)


def doctrine_sections() -> list[dict]:
    """DOCTRINE.md split at its `##` headings.

    The whole file, not just the injectable core: the commentary under each rule
    is where the rule came from and what it cost, and that is the half worth
    reading when deciding where to go next.
    """
    try:
        raw = DOCTRINE_PATH.read_text()
    except OSError:
        return []
    raw = raw.replace(DOCTRINE_BEGIN, "").replace(DOCTRINE_END, "")
    heads = list(_SECTION_HEAD.finditer(raw))
    out = []
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(raw)
        body = re.sub(r"\n-{3,}\s*$", "", raw[h.end():end].strip()).strip()
        if body:
            out.append({"title": h.group(1).strip(), "body": body})
    return out


def _section(sections: list[dict], *words: str) -> dict | None:
    for s in sections:
        low = s["title"].lower()
        if any(w in low for w in words):
            return s
    return None


def direction_store() -> dict:
    return load_json(DIRECTION_PATH, {"proposals": [], "reviews": [], "auto_adopt": False})


def _save_direction(store: dict):
    store["proposals"] = store.get("proposals", [])[-200:]
    store["reviews"] = store.get("reviews", [])[-40:]
    save_json(DIRECTION_PATH, store)


def proposals(*, kind: str | None = None, state: str = "open") -> list[dict]:
    out = direction_store().get("proposals", [])
    if kind:
        out = [p for p in out if p.get("kind") == kind]
    if state:
        out = [p for p in out if p.get("state") == state]
    return sorted(out, key=lambda p: p.get("at") or "", reverse=True)


def set_proposal(pid: str, state: str, **extra) -> dict | None:
    with _DIRECTION_LOCK:
        store = direction_store()
        for p in store.get("proposals", []):
            if p.get("id") == pid:
                p["state"] = state
                p["decided_at"] = now()
                p.update(extra)
                _save_direction(store)
                return p
    return None


def set_auto_adopt(on: bool) -> bool:
    with _DIRECTION_LOCK:
        store = direction_store()
        store["auto_adopt"] = bool(on)
        _save_direction(store)
    return bool(on)


# ------------------------------------------------------- the escalation floor
#
# Auto-adopt on its own is a machine that decides what to build next and then
# builds it, forever, with nobody reading what it found out. The operator asked
# for the middle setting: run everything unattended UNTIL something crosses a
# threshold, and then stop and ask. This is that threshold.
#
# What it is NOT is a spend limit in dollars. Workers run on the Claude
# subscription and the architect on codex, so `cost_usd` on a task record is
# what the API would have charged, not what was charged. Treating it as money
# would be a fabricated number, and the doctrine's rule 2 covers that. It is
# kept as a BURN proxy - the real ceiling is the rolling subscription window -
# and named `notional` everywhere so nobody reads it as a bill.
#
# The four gates, and why each one is worth stopping for:
#
#   contradictions - something we believed and acted on is false. The doctrine
#     says this outranks everything else in a report. Adopting more work on top
#     of an unread contradiction builds on the thing that was just disproved.
#   off_mission    - the supervisor's whole job is to hold the workspace against
#     the mission. If it says we have left it, more autonomy is more distance.
#   lane_failures  - a lane whose goals keep stopping is not a lane that needs
#     another goal. It is a lane with something wrong in it.
#   notional_day   - a crude burn ceiling, so a loop that has started going in
#     circles cannot spend the subscription window doing it all night.
#
# Every gate is a REASON TO ASK, never a reason to kill: nothing already running
# is stopped. Crossing one only means the pipeline stops feeding ITSELF new
# objectives, and the proposals stay open with your name on them.

ESCALATE_DEFAULTS = {
    "contradictions": 3,
    "lane_failures": 2,
    "notional_day_usd": 60.0,
    "off_mission": True,
}


def escalate_policy() -> dict:
    cfg = (config().get("autonomy") or {}).get("escalate") or {}
    return {**ESCALATE_DEFAULTS, **{k: v for k, v in cfg.items() if v is not None}}


def notional_spend_today() -> float:
    """What today's workers would have cost at API prices. A burn proxy."""
    today = now()[:10]
    return round(sum(float(r.get("cost_usd") or 0)
                     for recs in (board().get("tasks") or {}).values() for r in recs
                     if str(r.get("dispatched_at") or "")[:10] == today), 2)


def lane_failure_streak(lane_name: str) -> int:
    """How many of this lane's most recent goals stopped without finishing."""
    n = 0
    for row in goals(lane_name):
        if row.get("state") == "running":
            continue
        if row.get("state") == "done":
            break
        n += 1
    return n


def escalations(lane_name: str | None = None) -> list[dict]:
    """Every threshold currently crossed. Empty means run unattended."""
    pol, out = escalate_policy(), []
    found = findings_summary()
    if pol.get("contradictions") and found.get("contradicted", 0) >= pol["contradictions"]:
        out.append({"gate": "contradictions", "at": found["contradicted"],
                    "limit": pol["contradictions"],
                    "why": f"{found['contradicted']} finding(s) say something we believed is "
                           f"false, and none has been read. Building on top of that is "
                           f"building on the thing that was just disproved."})
    if pol.get("off_mission"):
        last = (supervisor_view() or {}).get("last") or {}
        if last.get("verdict") == "off_mission":
            out.append({"gate": "off_mission", "at": "off_mission", "limit": "aligned",
                        "why": "the supervisor's last reading was that this workspace has "
                               "left its mission: " + str(last.get("assessment") or "")[:300]})
    spend = notional_spend_today()
    if pol.get("notional_day_usd") and spend >= float(pol["notional_day_usd"]):
        out.append({"gate": "notional_day", "at": spend, "limit": pol["notional_day_usd"],
                    "why": f"today's workers would have cost ${spend:.2f} at API prices. That "
                           f"is not a bill - they ran on the subscription - but it is how much "
                           f"of the window has gone, and it is past the mark you set."})
    if lane_name and pol.get("lane_failures"):
        streak = lane_failure_streak(lane_name)
        if streak >= pol["lane_failures"]:
            out.append({"gate": "lane_failures", "at": streak, "limit": pol["lane_failures"],
                        "why": f"{lane_name}'s last {streak} goals stopped without finishing. "
                               f"Another goal is not what that lane needs."})
    return out


DIRECTION_SYSTEM = (
    "You are the consulting architect deciding where a codebase should go next. A goal "
    "has just finished in one lane of the operator's stack. You are given the operator's "
    "doctrine - the thesis the whole stack is betting on, the evidence ladder every claim "
    "is stated in, and the theses that are believed but not settled - then the goal that "
    "just closed with the evidence for each of its conditions, what the work reported back "
    "about the doctrine, what else is already open in this lane, and the repository as it "
    "now is.\n\n"
    "Reply with one JSON object and nothing else:\n"
    "{\n"
    '  "assessment": "where this lane now stands against the thesis, 2-4 sentences",\n'
    '  "ladder": [{"claim": "...", "from": "spec|in_tree|live_local|live_deployed|external",\n'
    '              "to": "...", "evidence": "what settles it"}],\n'
    '  "next": [{"objective": "one objective, what has to be true when it is finished",\n'
    '            "why": "what it buys, in terms of the thesis or an open question"}],\n'
    '  "research": [{"question": "what we do not know", "why": "why it matters",\n'
    '                "settled_by": "the observation or experiment that would answer it"}],\n'
    '  "exhausted": false,\n'
    '  "why_exhausted": "if there is genuinely nowhere worth going in this lane, say why"\n'
    "}\n\n"
    "Rules:\n"
    "- `ladder` is only for claims this finished goal actually moved, with the evidence "
    "that moved them. An empty list is a normal answer. Never move a claim to a rung the "
    "evidence you were given does not reach - when in doubt, downgrade.\n"
    "- `next` is what is worth doing next in THIS lane, grounded in the repository state "
    "you were given and in what is still unsettled. Zero, one, or two - not a backlog. Do "
    "not restate work that is already open, and do not propose work whose only merit is "
    "that it is more work. If the honest answer is that this lane is finished for now, "
    "return an empty list and set `exhausted` to true with a reason.\n"
    "- `research` is the highest-value thing you can return: something we believe and have "
    "not tested, or that this goal has just made answerable. It must be stated so that it "
    "could come out either way, and `settled_by` must be something someone could actually "
    "do. If nothing new opened up, return an empty list.\n"
    "- You are proposing. The operator decides what gets built and what gets adopted into "
    "the doctrine. Do not write as if the decision is made."
)


def _direction_context(g: dict, sections: list[dict]) -> str:
    """Everything the architect needs to answer 'and then what' for one lane."""
    lane_name = g.get("lane") or ""
    open_theses = _section(sections, "open theses", "open questions")
    parts = [mission_block().strip(),
             doctrine_block("The doctrine this stack is held to").strip()]
    if open_theses:
        parts.append(f"# {open_theses['title']}\n\n{open_theses['body']}")

    q = [p["text"] for p in proposals(kind="question")][:10]
    if q:
        parts.append("# Questions already open, do not restate them\n\n"
                     + "\n".join(f"- {t}" for t in q))

    parts.append(f"# The goal that just finished\n\n{g.get('objective', '')}")
    dod = "\n".join(
        f"- [{'x' if d.get('met') else ' '}] {d.get('text', '')}"
        + (f"\n      check: `{d['check']}` exit {d.get('check_exit')}" if d.get("check") else "")
        + (f"\n      evidence: {str(d.get('evidence'))[:400]}" if d.get("evidence") else "")
        for d in (g.get("done") or []))
    if dod:
        parts.append(f"## Its definition of done, as judged\n\n{dod}")
    tasks = "\n".join(f"- ({t.get('state')}) {t.get('text', '')}" for t in (g.get("tasks") or []))
    if tasks:
        parts.append(f"## What was actually done\n\n{tasks}")

    fs = [f for f in findings(lane=lane_name)][:8]
    if fs:
        parts.append("# What the work reported back about the doctrine\n\n"
                     + "\n".join(f"- **{f['bearing']}** ({f.get('source')}): {f['text'][:600]}"
                                 for f in fs))

    others = [x for x in goals(lane_name)
              if x["id"] != g["id"] and x["state"] in ("planning", "running", "blocked")]
    if others:
        parts.append("# Already open in this lane, do not propose these again\n\n"
                     + "\n".join(f"- {o['objective'][:200]}" for o in others))

    parts.append(f"# The repository right now\n\n{goal_brief(lane_name)}")
    return "\n\n".join(parts)


def direction_review(gid: str, *, auto: bool = False) -> dict:
    """Look back at a finished goal and ask whether there is anywhere left to go.

    Returns the review record. Costs one architect call, which is why it fires on
    a goal closing rather than on a timer: the moment there is something new to
    judge is the moment the judgement is worth paying for.
    """
    g = load_goal(gid)
    if not g:
        return {"ok": False, "error": f"no goal {gid!r}"}
    if not architect_available():
        return {"ok": False, "error": architect_off_reason()}

    sections = doctrine_sections()
    model = g.get("model") or config().get("consult_model", DEFAULT_CONSULT)
    resp = architect_chat([{"role": "system", "content": DIRECTION_SYSTEM},
                           {"role": "user", "content": _direction_context(g, sections)}], model)
    text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    out = _json_reply(text)
    if not out:
        return {"ok": False, "error": "the architect's answer could not be read as JSON",
                "raw": text[:2000]}

    rev = {"id": "d" + uuid.uuid4().hex[:9], "at": now(), "lane": g.get("lane"),
           "goal_id": gid, "goal": (g.get("objective") or "")[:300],
           "assessment": str(out.get("assessment") or "")[:2000],
           "ladder": [x for x in (out.get("ladder") or []) if isinstance(x, dict)][:6],
           "exhausted": bool(out.get("exhausted")),
           "why_exhausted": str(out.get("why_exhausted") or "")[:600],
           "tokens": (resp.get("usage") or {}).get("total_tokens") or 0,
           "auto": auto}

    # Nothing already open, and nothing already turned down. A review that
    # re-proposed what was dismissed last week would make dismissing it useless.
    seen = {_norm_prompt(p.get("text", "")) for p in direction_store().get("proposals", [])}
    fresh = []
    for n in (out.get("next") or []):
        obj = str((n or {}).get("objective") or "").strip()
        if not obj or _norm_prompt(obj) in seen:
            continue
        seen.add(_norm_prompt(obj))
        fresh.append({"id": "p" + uuid.uuid4().hex[:9], "at": now(), "kind": "goal",
                      "lane": g.get("lane"), "text": obj[:1000],
                      "why": str(n.get("why") or "")[:1000], "state": "open",
                      "from_goal": gid, "review_id": rev["id"]})
    for r in (out.get("research") or []):
        qn = str((r or {}).get("question") or "").strip()
        if not qn or _norm_prompt(qn) in seen:
            continue
        seen.add(_norm_prompt(qn))
        fresh.append({"id": "p" + uuid.uuid4().hex[:9], "at": now(), "kind": "question",
                      "lane": g.get("lane"), "text": qn[:1000],
                      "why": str(r.get("why") or "")[:1000],
                      "settled_by": str(r.get("settled_by") or "")[:1000],
                      "state": "open", "from_goal": gid, "review_id": rev["id"]})

    with _DIRECTION_LOCK:
        store = direction_store()
        store.setdefault("proposals", []).extend(fresh)
        store.setdefault("reviews", []).append(rev)
        _save_direction(store)

    ngoal = sum(1 for p in fresh if p["kind"] == "goal")
    nq = sum(1 for p in fresh if p["kind"] == "question")
    # Straight into the orchestrator thread, because this is the one thing in the
    # harness that is about where the whole stack is going rather than what one
    # worker did, and it should not have to be gone looking for.
    add_note(f"direction · {g.get('lane')}: {rev['assessment'][:400]}"
             + (f" — {ngoal} goal(s) proposed" if ngoal else "")
             + (f", {nq} open question(s)" if nq else "")
             + (" — nothing further worth doing here: " + rev["why_exhausted"][:200]
                if rev["exhausted"] else ""),
             lane=g.get("lane"))
    goal_log(gid, f"direction reviewed: {ngoal} proposed, {nq} question(s)")

    rev["proposals"] = fresh
    rev["ok"] = True
    if direction_store().get("auto_adopt"):
        blocked = escalations(g.get("lane"))
        rev["escalations"] = blocked
        if blocked:
            # Deliberately leaves the proposals `open`. The work is not thrown
            # away and nothing running is stopped - the pipeline just stops
            # feeding itself, which is the whole difference between autonomy
            # and a machine nobody can get in front of.
            names = ", ".join(b["gate"] for b in blocked)
            goal_log(gid, f"not auto-adopted: {names}")
            add_note(f"auto-adopt held in {g.get('lane')} — {names}. "
                     + " ".join(b["why"] for b in blocked)[:600]
                     + f" {ngoal} proposed objective(s) are waiting for you in Direction.",
                     lane=g.get("lane"))
        else:
            rev["adopted"] = [adopt_proposal(p["id"]) for p in fresh if p["kind"] == "goal"]
    return rev


def adopt_proposal(pid: str) -> dict:
    """Turn a proposed objective into a real goal. Only this makes it work.

    A question is never adopted this way. It is not an objective, it is something
    we do not know, and the answer to it is Travis writing it into the doctrine's
    open theses - which no agent may do.
    """
    p = next((x for x in direction_store().get("proposals", []) if x.get("id") == pid), None)
    if not p:
        return {"ok": False, "error": f"no proposal {pid!r}"}
    if p.get("state") != "open":
        return {"ok": False, "error": f"already {p.get('state')}"}
    if p.get("kind") != "goal":
        return {"ok": False, "error": "an open question is not an objective"}
    objective = p["text"] + (f"\n\nWhy: {p['why']}" if p.get("why") else "")
    g = open_goal(p["lane"], objective)
    set_proposal(pid, "adopted", goal_id=g.get("id"))
    add_note(f"adopted a proposed goal in {p['lane']}: {p['text'][:200]}", lane=p.get("lane"))
    return {"ok": True, "goal": g, "proposal_id": pid}


def direction_view(lane: str | None = None) -> dict:
    """Everything the Direction tab shows, assembled in one place."""
    sections = doctrine_sections()
    store = direction_store()
    props = [p for p in store.get("proposals", []) if p.get("state") == "open"]
    if lane:
        props = [p for p in props if p.get("lane") == lane]
    revs = sorted(store.get("reviews", []), key=lambda r: r.get("at") or "", reverse=True)
    if lane:
        revs = [r for r in revs if r.get("lane") == lane]
    thesis = _section(sections, "thesis")
    return {
        "thesis": thesis,
        "ladder": _section(sections, "ladder"),
        "values": _section(sections, "rules"),
        "owed": _section(sections, "owe"),
        "open_theses": _section(sections, "open theses", "open questions"),
        "sections": sections,
        "findings": findings(lane=lane)[:60],
        "findings_summary": findings_summary(),
        "proposals": sorted([p for p in props if p.get("kind") == "goal"],
                            key=lambda p: p.get("at") or "", reverse=True),
        "questions": sorted([p for p in props if p.get("kind") == "question"],
                            key=lambda p: p.get("at") or "", reverse=True),
        "reviews": revs[:8],
        "auto_adopt": bool(store.get("auto_adopt")),
        "doctrine_state": doctrine_state(),
        "doctrine_path": str(DOCTRINE_PATH),
        # A lane whose goal is finished and never reviewed is exactly where the
        # pipeline goes quiet, so the tab can offer to review it by name.
        "reviewable": [{"id": g["id"], "lane": g["lane"], "objective": g["objective"][:200]}
                       for g in goals(lane)
                       if g["state"] == "done"
                       and not any(r.get("goal_id") == g["id"] for r in store.get("reviews", []))],
    }


# ---------------------------------------------------------------- the supervisor
#
# Everything below the supervisor is local. A worker sees one task. A goal sees
# one objective. A direction review sees one lane, at the moment one goal in it
# finished. Each of those can be entirely correct and the workspace as a whole
# can still be quietly going somewhere nobody chose - the classic failure is not
# a bad step, it is a hundred good steps in a direction that stopped being the
# point three weeks ago.
#
# The supervisor is the only pass that looks at all of it at once and holds it
# against the mission. It is given the mission, the doctrine, every goal in
# every lane with its state, the open proposals and unsettled questions, what
# the work has reported back, which obligations are drifting, and what is on the
# board right now.
#
# Three hard limits on it, in descending order of how easy they would be to lose:
#
#   It never acts. Not a dispatch, not a close, not an adoption, not a pause. It
#   returns a reading and a recommendation and stops. A supervisor that could
#   cancel a goal for being off-mission would be deciding what gets built, which
#   is rule 6, and it would be doing it from a summary rather than from the work.
#
#   It never edits the mission. If the mission is silent on something real it
#   says so under `mission_gap` and that is a question for Travis, not a patch.
#
#   It refuses to run without a mission at all, rather than inventing one to
#   measure against. "Aligned with nothing" is not a finding, it is a fabrication,
#   and rule 2 covers it.

SUPERVISOR_SYSTEM = (
    "You are the supervisor of one workspace in an operator's stack. You are not doing "
    "the work and you are not planning it. Your single job is to say whether what is "
    "actually happening still serves the mission you are given, and to name precisely "
    "where it does not.\n\n"
    "You are given: the mission, in the operator's own words; the doctrine every agent "
    "here is held to; every goal in the workspace with its lane and state; the objectives "
    "proposed but not adopted, and the questions still open; what the work has reported "
    "back about the doctrine; which standing obligations are currently failing; and what "
    "is running right now.\n\n"
    "Reply with one JSON object and nothing else:\n"
    "{\n"
    '  "verdict": "aligned|drifting|off_mission",\n'
    '  "summary": "what this workspace is actually doing right now, and whether that is '
    'the mission, 2-4 sentences",\n'
    '  "aligned": [{"what": "the work", "why": "the part of the mission it serves"}],\n'
    '  "drift": [{"what": "the work", "where": "lane, or goal id, or obligation name",\n'
    '             "why": "what about it does not serve the mission",\n'
    '             "recommend": "what you would do about it"}],\n'
    '  "missing": [{"what": "something the mission calls for that nothing here is doing",\n'
    '               "why": "why it matters"}],\n'
    '  "mission_gap": "if the mission is silent or ambiguous about something real that is '
    'happening here, say what. Otherwise the empty string."\n'
    "}\n\n"
    "Rules:\n"
    "- Judge against the MISSION, not against whether the work looks good. Excellent work "
    "on the wrong thing is the exact failure you exist to catch, and it will look like "
    "progress in everything you are shown.\n"
    "- `drift` must point at something specific by lane, goal id or name. A general worry "
    "with nothing attached to it is not usable and does not belong in the list.\n"
    "- `missing` is for what the mission asks for and nothing here is carrying. An empty "
    "list is a normal answer.\n"
    "- You recommend. You do not decide, and nothing you return is carried out "
    "automatically. Do not write as if work will be stopped because you said so.\n"
    "- Do not invent counts, states or outcomes. If you were not shown it, you do not "
    "know it, and saying so is a better answer than an estimate.\n"
    "- If the workspace genuinely is on mission, say `aligned` and keep it short. Do not "
    "manufacture drift to have something to report."
)

SUPERVISOR_VERDICTS = ("aligned", "drifting", "off_mission")


def supervisor_store() -> dict:
    return load_json(SUPERVISOR_PATH, {"reports": []})


def _save_supervisor(store: dict):
    store["reports"] = store.get("reports", [])[-40:]
    save_json(SUPERVISOR_PATH, store)


def _supervisor_context() -> str:
    """The whole workspace, flattened, in the order it should be read."""
    ws = workspace()
    parts = [f"# The mission of this workspace ({ws.get('name')})\n\n{mission()}",
             doctrine_block("The doctrine every agent here is held to").strip()]

    gs = goals()
    if gs:
        by_lane: dict[str, list[dict]] = {}
        for g in gs:
            by_lane.setdefault(g.get("lane") or "?", []).append(g)
        lines = []
        for lane_name in sorted(by_lane):
            lines.append(f"## {lane_name}")
            for g in sorted(by_lane[lane_name], key=lambda x: x.get("opened_at") or ""):
                lines.append(
                    f"- [{g['state']}] `{g['id']}` {(g.get('objective') or '')[:220]}"
                    + (f"  ({g['met']}/{g['dod']} done-conditions met)" if g.get("dod") else "")
                    + (f"  now: {g['now'][:120]}" if g.get("now") else ""))
        parts.append("# Every goal in this workspace\n\n" + "\n".join(lines))
    else:
        parts.append("# Every goal in this workspace\n\nThere are none.")

    props = proposals(kind="goal")[:12]
    if props:
        parts.append("# Proposed but not adopted\n\n"
                     + "\n".join(f"- ({p.get('lane')}) {p['text'][:220]}" for p in props))
    qs = proposals(kind="question")[:12]
    if qs:
        parts.append("# Questions open and unsettled\n\n"
                     + "\n".join(f"- ({p.get('lane')}) {p['text'][:220]}" for p in qs))

    fs = findings()[:12]
    if fs:
        parts.append("# What the work has reported back about the doctrine\n\n"
                     + "\n".join(f"- **{f['bearing']}** ({f.get('lane') or '-'}): {f['text'][:400]}"
                                 for f in fs))

    bad = [o for o in obligations() if o.get("state") in ("drifted", "broken")]
    if bad:
        parts.append("# Standing obligations that are currently failing\n\n"
                     + "\n".join(f"- [{o['state']}] {o.get('name')}: `{o.get('check')}`"
                                 + (f" — {o.get('why')}" if o.get("why") else "")
                                 for o in bad))

    b = board()
    live = [t for t in b.get("tasks", {}).values() if t.get("state") not in TERMINAL_STATES]
    if live:
        parts.append("# Running right now\n\n"
                     + "\n".join(f"- ({t.get('lane')}) {str(t.get('prompt', ''))[:180]}"
                                 for t in live[:20]))

    lanes = sorted((config().get("lanes") or {}).keys())
    parts.append("# Lanes registered in this workspace\n\n"
                 + (", ".join(lanes) if lanes else "none"))
    return "\n\n".join(p for p in parts if p)


def supervise_workspace() -> dict:
    """Hold the whole workspace against its mission. Reports; changes nothing."""
    if not mission():
        return {"ok": False, "error": "this workspace has no mission yet, and there is "
                                      "nothing to hold the work against until it does",
                "needs_mission": True}
    # Its own switch and its own model, not the architect's. The two roles are
    # asked different questions and an operator who turns off escalation to stop
    # spending has not thereby said the workspace should stop being checked.
    ready, why = role_ready("supervisor")
    if not ready:
        return {"ok": False, "error": f"the supervisor cannot run: {why}"}

    resp = role_chat("supervisor",
                     [{"role": "system", "content": SUPERVISOR_SYSTEM},
                      {"role": "user", "content": _supervisor_context()}])
    text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    out = _json_reply(text)
    if not out:
        return {"ok": False, "error": "the supervisor's answer could not be read as JSON",
                "raw": text[:2000]}

    verdict = str(out.get("verdict") or "").strip().lower()
    rep = {
        "id": "s" + uuid.uuid4().hex[:9], "at": now(),
        "workspace": current_workspace(),
        "mission": mission(),
        # An unrecognised verdict is not quietly promoted to "aligned". The
        # reading is unknown, and unknown is a legitimate thing to display.
        "verdict": verdict if verdict in SUPERVISOR_VERDICTS else "unknown",
        "summary": str(out.get("summary") or "")[:2000],
        "aligned": [x for x in (out.get("aligned") or []) if isinstance(x, dict)][:12],
        "drift": [x for x in (out.get("drift") or []) if isinstance(x, dict)][:12],
        "missing": [x for x in (out.get("missing") or []) if isinstance(x, dict)][:12],
        "mission_gap": str(out.get("mission_gap") or "")[:1000],
        # Who read it. A reading is only as good as what took it, and the model
        # can be changed between readings.
        "model": role_model("supervisor"),
        "tokens": (resp.get("usage") or {}).get("total_tokens") or 0,
    }
    store = supervisor_store()
    store.setdefault("reports", []).append(rep)
    _save_supervisor(store)

    nd, nm = len(rep["drift"]), len(rep["missing"])
    add_note(f"supervisor · {rep['verdict']}: {rep['summary'][:400]}"
             + (f" — {nd} drifting" if nd else "")
             + (f", {nm} unattended" if nm else "")
             + (f" — the mission does not say: {rep['mission_gap'][:200]}"
                if rep["mission_gap"] else ""))
    rep["ok"] = True
    return rep


def supervisor_view() -> dict:
    """What the mission strip shows: the last reading, and whether it is stale.

    Stale means the workspace has moved since the reading was taken. It is a
    count of what has closed or been proposed since, not a clock, because a
    reading of a workspace nobody touched is not out of date just because it is
    from yesterday.
    """
    store = supervisor_store()
    reps = [r for r in store.get("reports", []) if r.get("workspace") == current_workspace()]
    last = reps[-1] if reps else None
    since = 0
    if last:
        at = last.get("at") or ""
        since = (sum(1 for g in goals() if (g.get("updated_at") or "") > at)
                 + sum(1 for p in proposals(state="") if (p.get("at") or "") > at))
    return {
        "mission": mission(),
        "last": last,
        "stale": bool(last and since),
        "since": since,
        "history": [{"id": r["id"], "at": r["at"], "verdict": r.get("verdict"),
                     "drift": len(r.get("drift") or [])} for r in reps[-12:]][::-1],
        "available": architect_available(),
        "why_unavailable": None if architect_available() else architect_off_reason(),
    }


# ---------------------------------------------------------------- observations
#
# What is obviously wrong, right now, computed from what is already on disk. No
# model is asked, because these are facts and a model would only be able to
# guess at them - and because the point of this is to be true at a glance, in a
# window the operator is already looking at.

_KILL_PHRASES = ("over its time limit", "hung for", "held more than",
                 "stopped mid-task")


def _norm_prompt(p: str) -> str:
    return " ".join((p or "").lower().split())[:200]


def _resume_hint(lane_name: str, rec: dict | None) -> str | None:
    """The session id an observation is telling you to continue, or nothing.

    Nothing is the honest answer when the transcript is gone: a suggestion that
    cannot be acted on is worse than no suggestion.
    """
    if not rec:
        return None
    sid = rec.get("session_id") or rec.get("resume_of") or rec.get("task_id")
    return sid if sid and transcript_path(sid) else None


def observations() -> list[dict]:
    """Problems worth naming, newest evidence first. Facts only."""
    out = []
    cfg = config()
    b = board()
    tasks = b.get("tasks") or {}

    # The doctrine every worker is running under is not what you last ratified.
    # High, and first: this is the one file where a change nobody authorised
    # changes what everything else is held to.
    d = doctrine_state()
    if d["drifted"]:
        out.append({
            "kind": "doctrine-changed", "lane": None, "severity": "high",
            "at": now(), "ratify": True,
            "text": f"the doctrine core at {DOCTRINE_PATH} has changed since you "
                    f"ratified it ({d['ratified']} -> {d['digest']}). Every worker and "
                    f"every architect turn is already running under the new text.",
            "fix": "read the diff, then confirm it was you",
        })
    elif d["unratified"]:
        out.append({
            "kind": "doctrine-unratified", "lane": None, "severity": "medium",
            "at": now(), "ratify": True,
            "text": f"the doctrine at {DOCTRINE_PATH} is being injected into every "
                    f"worker and architect prompt but you have never ratified it.",
            "fix": "read it, then confirm it is yours",
        })

    # A standing obligation whose check says it is no longer true. This is the
    # only place in this list where the evidence is something the harness went
    # and RAN, rather than something it noticed lying around, so it carries the
    # command and the rung the check reached.
    for ob in obligations():
        if ob.get("state") == "drifted":
            out.append({
                "kind": "obligation-drifted", "lane": ob.get("lane"), "severity": "medium",
                "at": ob.get("last_drift_at") or ob.get("last_checked") or now(),
                "text": f"{ob['name']} is no longer current: `{ob['check']}` exited "
                        f"{ob.get('last_rc')} at {ob.get('last_checked')}.",
                "fix": (ob.get("fix") or "no fix is recorded for this obligation")
                       + ("" if ob.get("auto_fix") else
                          " (auto-fix is off, so nothing has been dispatched)"),
            })
        elif ob.get("state") == "broken":
            # Not drift. A checker that cannot run has no verdict to report, and
            # saying "stale" on its behalf would be inventing an observation.
            out.append({
                "kind": "obligation-broken", "lane": ob.get("lane"), "severity": "medium",
                "at": ob.get("last_checked") or now(),
                "text": f"the check for {ob['name']} could not run, so nothing is known "
                        f"about whether it is current: {(ob.get('last_output') or '')[:200]}",
                "fix": "fix the check before trusting its verdict",
            })

    # A worker killed by a limit, twice, on the same instruction. Sending it a
    # third time is not persistence, it is the same thirty minutes again.
    for lane_name, recs in tasks.items():
        seen: dict[str, list[dict]] = {}
        for r in recs:
            if any(p in (r.get("error") or "") for p in _KILL_PHRASES):
                seen.setdefault(_norm_prompt(r.get("prompt")), []).append(r)
        for _, group in seen.items():
            if len(group) < 2:
                continue
            # The one to continue is the attempt that got furthest, which is the
            # most recent - not whichever the board happens to list first.
            latest = max(group, key=lambda r: r.get("finished_at") or "")
            # Telling someone to resume a session they have already resumed is
            # noise, and worse than noise when the resume was killed too: the
            # advice reads as untried when it has been tried and did not hold.
            # So say what became of it. A continuation is a later record that
            # names this session as the one it resumed.
            sid = latest.get("session_id") or latest.get("task_id")
            after = [r for r in recs
                     if r.get("resume_of") == sid
                     and r.get("task_id") != latest.get("task_id")
                     and (r.get("finished_at") or "") > (latest.get("finished_at") or "")]
            went = max(after, key=lambda r: r.get("finished_at") or "", default=None)
            done = bool(went) and went.get("status") == "completed"
            out.append({
                "kind": "repeat-kill", "lane": lane_name,
                "severity": "medium" if done else "high",
                "at": group[0].get("finished_at"),
                "text": f"the same instruction has been killed by a limit {len(group)} times "
                        f"in {lane_name}: \"{(group[0].get('prompt') or '')[:120]}…\". "
                        + ("Dispatching it again unchanged will end the same way."
                           if not went else
                           f"It was resumed {len(after)} time(s) since, and the last of "
                           f"those {'finished' if done else 'was cut off too'}."),
                "fix": ("check what it landed before sending anything else" if done else
                        "resume its session instead of restarting it, or cut the task down"),
                "task_id": group[0].get("task_id"),
                # Naming the session is what makes the advice followable: without
                # it, "resume its session" means finding a uuid by hand.
                "resume": _resume_hint(lane_name, latest),
            })

    # Work a killed worker left behind. The worktree still has it; nothing is
    # coming back for it on its own.
    for lane_name, lane in cfg["lanes"].items():
        wt = WORKTREE_DIR / lane_name
        if not wt.exists():
            continue
        dirty = run(["git", "status", "--porcelain"], cwd=wt).stdout.strip()
        if not dirty:
            continue
        last = next((r for r in tasks.get(lane_name, []) if r.get("backend") == "claude"), None)
        if not last or last.get("status") == "running":
            continue                                # someone is still working in there
        # Who left it is not the point and often cannot be known - a later
        # worker runs in the same tree. The fact is that there are changes in a
        # worktree no branch will ever be merged from unless someone applies it.
        # The session worth resuming is the last one that was cut off, which is
        # often not the last one that ran - a short worker can finish after it.
        # Ordered by when they stopped, because the board is in the order things
        # were started and a long worker finishes after a later short one.
        cut_off = [r for r in tasks.get(lane_name, [])
                   if r.get("backend") == "claude" and r.get("killed")]
        killed_last = max(cut_off, key=lambda r: r.get("finished_at") or "", default=None)
        # A worker the operator stopped on purpose is not an alarm: the stopping
        # was a decision, and whatever it left is that decision's consequence.
        # Only a worker a limit cut off is something nobody chose.
        killed = bool(killed_last) and any(
            p in (killed_last.get("error") or "") for p in _KILL_PHRASES)
        n = len(dirty.splitlines())
        out.append({
            "kind": "work-at-risk", "lane": lane_name,
            "severity": "high" if killed else "medium",
            "at": last.get("finished_at"),
            "text": f"{n} uncommitted file(s) sitting in the {lane_name} worktree"
                    + (f", and a worker there {killed_last.get('error') or 'did not finish'}"
                       if killed else ", and no worker is running")
                    + ". Nothing will pick this up by itself.",
            "fix": "resume that session to finish and commit it, or review the diff and apply it",
            "task_id": last.get("task_id"),
            "resume": _resume_hint(lane_name, killed_last or last),
        })

    # Threads and goals that are stopped and waiting on a person.
    for c in consults():
        if c.get("blocked_on") == "gathering":
            # Waiting on a worker is fine. Waiting on a worker that already
            # stopped is the failure that looks exactly like working.
            sent = next((r for lane_recs in tasks.values() for r in lane_recs
                         if r.get("consult_id") == c["id"] and r.get("report_back")), None)
            if sent and sent.get("status") != "running":
                got = load_consult(c["id"]) or {}
                if not any(t.get("task_id") == sent.get("task_id") and t.get("role") == "worker"
                           for t in got.get("turns") or []):
                    out.append({
                        "kind": "thread-mute", "lane": c.get("lane"), "severity": "high",
                        "at": sent.get("finished_at"),
                        "text": f"consult {c['id']} ({c.get('lane')}) is waiting on a worker "
                                "that already finished. Its report never reached the thread, "
                                "so it will wait forever.",
                        "fix": "continue it - that relays the report",
                        "consult_id": c["id"],
                    })
            continue
        if c.get("blocked_on") is None:
            continue
        # Why it stopped and whether it needs a person are two different
        # questions, and only the second one is urgent. A thread that hits the
        # token ceiling holding an unanswered decision was being reported as a
        # budget footnote - medium, no question shown - when what was actually
        # true is that the architect had asked something only the operator can
        # answer and then lost the ability to ask again. The reason it stopped
        # was masking the reason it needs you.
        asks = [n["text"] for n in c.get("needs_typed") or [] if n["kind"] == "decision"]
        why = c.get("blocked_why") or c["blocked_on"]
        limit = c["blocked_on"] in ("rounds", "tokens", "stalled")
        out.append({
            "kind": "thread-stopped", "lane": c.get("lane"),
            "severity": "high" if (asks or c["blocked_on"] == "operator") else "medium",
            "at": c.get("last_at"),
            # The question, not a note that there is one. Quoting it is the
            # difference between the operator answering now and going to find
            # out what was asked first - and the orchestrator brief already
            # promises this is surfaced "with the actual question".
            "text": f"consult {c['id']} ({c.get('lane')}) is stopped: {why}."
                    + (" It asks: " + "; ".join(asks)[:300] if asks else ""),
            "fix": ("answer it - it cannot continue itself any further"
                    if limit and asks else
                    "continue it, or answer it" if limit else "answer it, or continue it"),
            "consult_id": c["id"],
        })
    for g in goals():
        if g.get("state") == "blocked":
            out.append({
                "kind": "goal-stopped", "lane": g.get("lane"), "severity": "high",
                "at": g.get("updated_at"),
                "text": f"goal {g['id']} ({g.get('lane')}) is stopped: "
                        f"{g.get('stopped_why') or g.get('stopped_on')}"
                        + (" It asks: " + "; ".join(g["questions"])[:300] if g["questions"] else ""),
                "fix": "answer it",
                "goal_id": g["id"],
            })

    # Two lanes over one directory. Their workers get separate worktrees, so
    # this is not corruption - it is two plans for one codebase, which is the
    # coordination problem that shows up as work being undone.
    by_path: dict[str, list[str]] = {}
    for lane_name, lane in cfg["lanes"].items():
        by_path.setdefault(str((ROOT / lane.get("path", ".")).resolve()), []).append(lane_name)
    for path, names in by_path.items():
        if len(names) > 1:
            out.append({
                "kind": "shared-repo", "lane": names[0], "severity": "medium", "at": None,
                "text": f"lanes {', '.join(sorted(names))} all point at {path}. Each gets its "
                        "own worktree, so they will not see each other's work, and two of them "
                        "finishing means two branches over the same files.",
                "fix": "give them one goal between them, or merge the lanes",
            })

    order = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda o: (order.get(o.get("severity"), 3), o.get("at") or ""), reverse=False)
    return out


def cmd_packet(args):
    zpath, brief = build_packet(args.lane, args.question or "(no question stated)", args.file or [])
    print(f"packet: {zpath}  ({zpath.stat().st_size} bytes)")
    print("forward this to GPT-5.6 manually, or run ./amp ask to send it automatically")
    return 0


def cmd_ask(args):
    cfg = config()
    model_key = args.model or cfg.get("consult_model", DEFAULT_CONSULT)
    print(f"consulting {CONSULT_MODELS.get(model_key, model_key)} ...")
    t0 = time.time()
    c = open_consult(args.lane, args.question, model_key=model_key, extra_files=args.file or [])
    print("=" * 78)
    print(last_ruling(c))
    print("=" * 78)
    print(f"consult {c['id']}   ({time.time() - t0:.0f}s, {c['cost_tokens']} tokens)")
    print("saved: {}".format(RULING_DIR / "{}-{}.md".format(c["lane"], c["id"])))
    if c.get("needs"):
        print("\nit still needs:\n  " + "\n  ".join(c["needs"]))
    return 0


def cmd_serve(args):
    """Launch the browser console in code/."""
    server = HERE / "server.py"
    if not server.exists():
        die(f"console not found at {server}")
    argv = [sys.executable, str(server), "--port", str(args.port)]
    if args.open:
        argv.append("--open")
    os.execv(sys.executable, argv)


def cmd_credits(args):
    key = openrouter_key()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {key}"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode())["data"]
    left = d["total_credits"] - d["total_usage"]
    print(f"OpenRouter credit remaining: ${left:.2f}")
    for k, m in CONSULT_MODELS.items():
        print(f"  {k:<6} {m}")
    return 0


# ---------------------------------------------------------------- cli


def main(argv=None):
    p = argparse.ArgumentParser(prog="amp", description="lead-manager harness for the [&] stack")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="preflight: worker CLIs, keys, lane bindings").set_defaults(
        func=cmd_doctor
    )
    sub.add_parser("lanes", help="list lanes").set_defaults(func=cmd_lanes)
    sub.add_parser("board", help="show the board").set_defaults(func=cmd_board)
    sub.add_parser("credits", help="OpenRouter credit remaining").set_defaults(func=cmd_credits)

    sv = sub.add_parser("serve", help="launch the browser console (code/)")
    sv.add_argument("--port", type=int, default=8787)
    sv.add_argument("--open", action="store_true")
    sv.set_defaults(func=cmd_serve)

    lane = sub.add_parser("lane", help="manage lanes").add_subparsers(dest="sub", required=True)
    a = lane.add_parser("add", help="add a lane")
    a.add_argument("name")
    a.add_argument("--repo", help="owner/name (inferred from git origin if omitted)")
    a.add_argument("--path", help="path under ProjectAmp2 (defaults to name)")
    a.add_argument("--branch", default="main")
    a.add_argument("--backend", choices=BACKENDS, default=DEFAULT_BACKEND)
    a.add_argument("--env", help="codex cloud environment id")
    a.add_argument("--replace", action="store_true", help="repoint a lane that already exists")
    a.set_defaults(func=cmd_lane_add)

    e = lane.add_parser("env", help="bind a codex env id to a lane")
    e.add_argument("name")
    e.add_argument("env_id")
    e.set_defaults(func=cmd_lane_env)

    lb = lane.add_parser("backend", help="choose a lane's worker backend")
    lb.add_argument("name")
    lb.add_argument("backend", choices=BACKENDS)
    lb.set_defaults(func=cmd_lane_backend)

    d = sub.add_parser("dispatch", help="send a task to a lane's worker")
    d.add_argument("lane")
    d.add_argument("prompt", nargs="?", default="")
    d.add_argument("--prompt-file")
    d.add_argument("--backend", choices=BACKENDS, help="override the lane's backend")
    d.add_argument("--attempts", type=int, default=1, help="codex only: best-of-N")
    d.add_argument("--branch")
    d.add_argument("--model", help=f"claude only, default {DEFAULT_CLAUDE_MODEL}")
    d.add_argument("--budget", type=float, help=f"claude only, USD cap (default {DEFAULT_BUDGET_USD:.2f})")
    d.add_argument(
        "--shared-tree",
        action="store_true",
        help="claude only: edit the real checkout instead of an isolated worktree",
    )
    d.set_defaults(func=cmd_dispatch)

    rp = sub.add_parser("reply", help="answer a claude worker mid-task (resumes its session)")
    rp.add_argument("lane")
    rp.add_argument("prompt")
    rp.add_argument("--budget", type=float)
    rp.add_argument("--session", help="a task or session id (a prefix is enough) - "
                                      "defaults to the newest, which is rarely the "
                                      "one that was cut off")
    rp.set_defaults(func=cmd_reply)

    po = sub.add_parser("poll", help="refresh the board from Codex Cloud")
    po.add_argument("--lane")
    po.set_defaults(func=cmd_poll)

    df = sub.add_parser("diff", help="show a task's diff")
    df.add_argument("lane")
    df.add_argument("--task-id")
    df.add_argument("--attempt", type=int)
    df.set_defaults(func=cmd_diff)

    ap = sub.add_parser("apply", help="apply a task's diff into the local worktree")
    ap.add_argument("lane")
    ap.add_argument("--task-id")
    ap.add_argument("--attempt", type=int)
    ap.set_defaults(func=cmd_apply)

    pk = sub.add_parser("packet", help="build a GPT-5.6 packet zip (manual forward)")
    pk.add_argument("lane")
    pk.add_argument("-q", "--question")
    pk.add_argument("-f", "--file", action="append")
    pk.set_defaults(func=cmd_packet)

    ak = sub.add_parser("ask", help="build a packet AND send it to GPT-5.6")
    ak.add_argument("lane")
    ak.add_argument("-q", "--question", required=True)
    ak.add_argument("-f", "--file", action="append")
    ak.add_argument("--model", choices=list(CONSULT_MODELS), help="default: terra")
    ak.set_defaults(func=cmd_ask)

    lg = sub.add_parser("login", help="connect a long-lived Claude worker token")
    lg.set_defaults(func=cmd_login)

    args = p.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
