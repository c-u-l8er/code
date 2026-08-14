#!/usr/bin/env python3
"""store - the SQLite mirror of everything this harness keeps on disk.

The console has never stored anything in the browser: every board, chat,
finding, goal and ruling is a JSON file under `.amp/`. That is durable but it
is not backed up, it has no history, and it is a directory rather than a thing
you can hand to a sync service. This file adds the second copy.

Three rules decide the whole design:

  The JSON files stay authoritative. Nothing here is ever read on the hot
  path, and `record` cannot raise - a mirror that can take the harness down
  with it is worse than no mirror. Failures are counted and shown in Settings.

  The mirror is additive. A file deleted from disk is still held here and is
  reported as held, not dropped. "Do not lose the data" is the entire point,
  so the one operation this file does not have is a delete.

  Secrets never enter it. `.secrets.json` holds an OpenRouter key and a Claude
  worker token, and this database is explicitly meant to leave the machine one
  day. Excluding it here, at the only door, is cheaper than remembering to
  exclude it at every future upload.

The revisions table doubles as the change feed a cloud sync would read: its
`seq` is monotonic, so "everything since seq N" is one indexed query, and
pruning old history cannot renumber what a remote has already seen.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 1
DB_NAME = "amp.db"

# What a sweep will not copy, and why each one is here rather than a general
# ignore list someone can widen without noticing what they let through.
SKIP_NAMES = {
    ".secrets.json",   # credentials - see the module docstring
    DB_NAME, DB_NAME + "-wal", DB_NAME + "-shm",
}
SKIP_DIRS = {
    "worktrees",       # git checkouts: large, and reproducible from the repos
    "__pycache__",
}
# `.lock` keeps the console lock out. `.token` keeps the console's inbound
# credential out: a secret with a full revision history is still a secret with
# a full revision history, and this mirror never forgets anything on purpose.
SKIP_SUFFIX = (".tmp", ".lock", ".token")

# A board is already 1.3 MB, so the cap is not tight; it exists so that one
# stray core dump or video in the state directory cannot balloon the file the
# operator is being told to treat as their backup.
MAX_BYTES = 16 * 1024 * 1024

class SchemaTooNew(RuntimeError):
    """The file on disk was written by a build that knows more than this one.

    Refusing is the point. An older reader that opens a newer file does not
    fail - it runs its own migrations against it, drops a column it does not
    recognise, and writes a worse version of the file back. That is the shape
    of loss this whole module exists to prevent, and the only moment it can be
    stopped is before the first write.

    An OLDER file is not this. Older is a migration, and migrations are what
    `_connect` already does.
    """

    def __init__(self, found: int, known: int):
        self.found, self.known = found, known
        super().__init__(
            f"{DB_NAME} says its schema is {found}; this build knows {known}. "
            "Refusing to open it - a newer file was written by a program this "
            "one would migrate backwards. Update amp, or move the file aside.")


_LOCK = threading.RLock()
_CONN: sqlite3.Connection | None = None
_ROOT: Path | None = None
_SETTINGS: dict | None = None

# Never raising means the failure has to surface somewhere else. Here.
_FAILS = {"n": 0, "last": None, "at": None}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def bind(root: Path) -> None:
    """Point the mirror at a state directory. Called once, by amp."""
    global _ROOT, _CONN, _SETTINGS
    with _LOCK:
        if _ROOT is not None and Path(root) == _ROOT:
            return
        _ROOT = Path(root)
        if _CONN is not None:
            _CONN.close()
        _CONN = None
        _SETTINGS = None


def db_path() -> Path | None:
    return None if _ROOT is None else _ROOT / DB_NAME


def _stored_schema(c: sqlite3.Connection) -> int | None:
    """The schema number the FILE claims, or None if it has not said.

    None is two situations kept together on purpose: a database this build is
    about to create, and one written before `meta` existed. Both are
    older-or-equal by definition, and only a number GREATER than ours is a
    refusal - so nothing is lost by not telling them apart.

    A value that is present but not a number is also None. It cannot be
    compared, and refusing to open the mirror over an unparseable string would
    take the harness's backup away on the strength of a guess.
    """
    try:
        row = c.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
    except sqlite3.OperationalError:
        return None  # no `meta` table: a file this build is about to create
    try:
        return int(row[0]) if row else None
    except (TypeError, ValueError):
        return None


def _connect() -> sqlite3.Connection:
    """Open (creating if needed) the one connection this process uses.

    WAL, because the console is a threading server and a sweep must not block
    a board write. `check_same_thread=False` with every call under `_LOCK`:
    one lock is easier to reason about than a connection per thread, and this
    is not a hot path.
    """
    global _CONN
    if _CONN is not None:
        return _CONN
    if _ROOT is None:
        raise RuntimeError("store.bind() has not been called")
    _ROOT.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(_ROOT / DB_NAME), check_same_thread=False, timeout=15)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    # Before anything is created, altered or written. Every line below this
    # point changes the file, and the whole value of the check is that it
    # happens while the file is still exactly as the newer build left it.
    found = _stored_schema(c)
    if found is not None and found > SCHEMA:
        c.close()
        raise SchemaTooNew(found, SCHEMA)
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta(
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        -- `docs` is an index, not a second copy: the current body IS the
        -- newest revision, and naming it here rather than storing it again
        -- halves the file and removes the chance of the two disagreeing.
        CREATE TABLE IF NOT EXISTS docs(
            path       TEXT PRIMARY KEY,   -- relative to the state root
            workspace  TEXT NOT NULL,
            sha        TEXT NOT NULL,
            bytes      INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            seq        INTEGER NOT NULL    -- the revision holding the body
        );
        CREATE TABLE IF NOT EXISTS revisions(
            seq        INTEGER PRIMARY KEY AUTOINCREMENT,
            path       TEXT NOT NULL,
            workspace  TEXT NOT NULL,
            body       BLOB NOT NULL,
            sha        TEXT NOT NULL,
            bytes      INTEGER NOT NULL,
            written_at TEXT NOT NULL,
            device     TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS revisions_doc ON revisions(path, seq DESC);
        """
    )
    # An earlier build kept the body in `docs` as well. Dropping the column is
    # safe in exactly one direction: every one of those bodies is also the
    # revision `docs.seq` names, so nothing is being thrown away that is not
    # still held next to it.
    if "body" in {r[1] for r in c.execute("PRAGMA table_info(docs)")}:
        c.execute("ALTER TABLE docs DROP COLUMN body")
    # Bodies are compressed, because the board is 1.3 MB of JSON rewritten on
    # every worker heartbeat and twenty versions of it uncompressed is 27 MB
    # for one file. The flag is per row rather than global so a body that
    # compresses to something larger (a zip, a png) is stored as it is, and so
    # a database written before this existed still reads.
    if "zip" not in {r[1] for r in c.execute("PRAGMA table_info(revisions)")}:
        c.execute("ALTER TABLE revisions ADD COLUMN zip INTEGER NOT NULL DEFAULT 0")
    # Written AFTER the migrations above, and updated rather than ignored. The
    # old `INSERT OR IGNORE` meant the number never moved once the file
    # existed, so a file migrated by a newer build still claimed the old
    # schema - and a field that cannot change is a field that cannot disagree,
    # which is the same as not having it.
    if found is None or found < SCHEMA:
        c.execute("INSERT INTO meta(key, value) VALUES('schema', ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(SCHEMA),))
    # A device id, so a future sync can tell this machine's writes from another
    # machine's without asking anyone to name their laptop.
    c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('device', ?)", (uuid.uuid4().hex[:12],))
    c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('created_at', ?)", (_now(),))
    c.commit()
    _CONN = c
    return c


# ------------------------------------------------------------------ settings
#
# The switches live in the database rather than in config.json because
# config.json is per-workspace and this file is not: one mirror covers every
# workspace on the machine, so "is the mirror on" cannot have four answers.

DEFAULTS = {
    "mirror": "1",        # copy every save_json write as it happens
    "history_keep": "20",  # revisions kept per document by a prune
    "sweep_min": "10",     # minutes between background sweeps, 0 = off
}


def settings() -> dict:
    global _SETTINGS
    with _LOCK:
        if _SETTINGS is None:
            try:
                rows = _connect().execute("SELECT key, value FROM meta").fetchall()
                _SETTINGS = {**DEFAULTS, **{k: v for k, v in rows}}
            except Exception as e:  # a broken mirror must not stop the harness
                _note_fail(e)
                return dict(DEFAULTS)
        return dict(_SETTINGS)


def set_setting(key: str, value) -> dict:
    if key not in DEFAULTS:
        raise ValueError(f"no such database setting: {key}")
    if key == "mirror":
        v = "1" if value else "0"
    else:
        n = int(value)
        if key == "history_keep" and not 1 <= n <= 10_000:
            raise ValueError("history_keep must be between 1 and 10000")
        if key == "sweep_min" and not 0 <= n <= 1440:
            raise ValueError("sweep_min must be between 0 and 1440 minutes")
        v = str(n)
    global _SETTINGS
    with _LOCK:
        c = _connect()
        c.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, v))
        c.commit()
        _SETTINGS = None
    return settings()


def enabled() -> bool:
    return settings().get("mirror") == "1"


def _pack(body: bytes) -> tuple[bytes, int]:
    z = zlib.compress(body, 6)
    return (z, 1) if len(z) < len(body) else (body, 0)


def _unpack(blob: bytes, zip_: int) -> bytes:
    return zlib.decompress(blob) if zip_ else bytes(blob)


def _note_fail(e: Exception):
    _FAILS["n"] += 1
    _FAILS["last"] = f"{type(e).__name__}: {e}"
    _FAILS["at"] = _now()


# ------------------------------------------------------------------ writing


def _rel(path: Path) -> str | None:
    """Where a file sits relative to the state root, or None if it is outside."""
    if _ROOT is None:
        return None
    try:
        return Path(path).resolve().relative_to(_ROOT.resolve()).as_posix()
    except ValueError:
        return None


def _workspace_of(rel: str) -> str:
    """Which workspace a relative path belongs to.

    Derived from the path rather than read from the registry, so the mirror
    keeps working during a workspace switch - the one moment when the registry
    and the files disagree. The default workspace IS the state root, which is
    why anything not under `ws/` is `core`.
    """
    parts = rel.split("/")
    return parts[1] if len(parts) > 2 and parts[0] == "ws" else "core"


def skipped(rel: str) -> str | None:
    """Why this path is not mirrored, or None if it is."""
    parts = rel.split("/")
    if parts[-1] in SKIP_NAMES:
        return "excluded by name"
    if any(p in SKIP_DIRS for p in parts[:-1]):
        return "excluded directory"
    if rel.endswith(SKIP_SUFFIX):
        return "temporary file"
    return None


def record(path, body: bytes | str | None = None) -> bool:
    """Mirror one file. Never raises; returns whether a new revision landed.

    `body` is the bytes just written, when the caller already has them - it
    saves a read and, more importantly, mirrors exactly what was written even
    if something rewrites the file a millisecond later.
    """
    try:
        if not enabled():
            return False
        return _record(Path(path), body)
    except Exception as e:
        _note_fail(e)
        return False


def _record(path: Path, body=None, *, force: bool = False) -> bool:
    rel = _rel(path)
    if rel is None or skipped(rel):
        return False
    if body is None:
        if not path.is_file():
            return False
        if path.stat().st_size > MAX_BYTES:
            return False
        body = path.read_bytes()
    elif isinstance(body, str):
        body = body.encode()
    if len(body) > MAX_BYTES:
        return False
    sha = hashlib.sha256(body).hexdigest()
    with _LOCK:
        c = _connect()
        row = c.execute("SELECT sha FROM docs WHERE path=?", (rel,)).fetchone()
        # An identical write is not a revision. The board is rewritten on every
        # worker heartbeat, and a history of a thousand identical boards would
        # bury the one change worth finding.
        if row and row[0] == sha and not force:
            return False
        ws = _workspace_of(rel)
        at = _now()
        dev = settings().get("device", "?")
        blob, z = _pack(body)
        cur = c.execute(
            "INSERT INTO revisions(path, workspace, body, sha, bytes, written_at, device, zip) "
            "VALUES(?,?,?,?,?,?,?,?)", (rel, ws, blob, sha, len(body), at, dev, z))
        c.execute(
            "INSERT INTO docs(path, workspace, sha, bytes, updated_at, seq) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
            "workspace=excluded.workspace, sha=excluded.sha, bytes=excluded.bytes, "
            "updated_at=excluded.updated_at, seq=excluded.seq",
            (rel, ws, sha, len(body), at, cur.lastrowid))
        c.commit()
    return True


def _walk() -> list[Path]:
    out = []
    if _ROOT is None or not _ROOT.is_dir():
        return out
    for dirpath, dirnames, names in os.walk(_ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for n in names:
            p = Path(dirpath) / n
            rel = _rel(p)
            if rel and not skipped(rel):
                out.append(p)
    return out


def backup() -> dict:
    """Sweep the whole state directory into the mirror.

    This is what catches everything `save_json` does not: packets, rulings,
    published reports, consult transcripts, goal files, and anything a future
    part of the harness writes without being taught about this file.
    """
    written = failed = 0
    started = _now()
    files = _walk()
    for p in files:
        try:
            if _record(p):
                written += 1
        except Exception as e:
            _note_fail(e)
            failed += 1
    return {"ok": True, "written": written, "failed": failed,
            "scanned": len(files), "started": started, "finished": _now()}


# ------------------------------------------------------------------ reading


def stored_schema() -> int | None:
    """The schema the FILE claims, read without going through `_connect`.

    `_connect` is the thing that refuses a newer file, so asking it would
    produce the number only in the cases where nobody needs it. Read-only, so
    a report cannot be the thing that touches a file this build has just
    decided it must not touch.
    """
    p = db_path()
    if not (p and p.exists()):
        return None
    try:
        c = sqlite3.connect(f"{p.as_uri()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        return _stored_schema(c)
    finally:
        c.close()


def status() -> dict:
    p = db_path()
    out = {
        "ok": True,
        "path": str(p) if p else None,
        "exists": bool(p and p.exists()),
        "root": str(_ROOT) if _ROOT else None,
        "settings": settings(),
        "failures": dict(_FAILS),
        # Two numbers, because one number cannot disagree with itself. The old
        # `"schema": SCHEMA` reported the running code to a reader who was
        # being invited to check the file.
        "schema_code": SCHEMA,
        "schema_db": stored_schema(),
        "max_mb": MAX_BYTES // (1024 * 1024),
        "excluded": sorted(SKIP_NAMES | SKIP_DIRS),
    }
    if not out["exists"]:
        return out
    try:
        with _LOCK:
            c = _connect()
            out["bytes"] = sum(
                (p.parent / (p.name + s)).stat().st_size
                for s in ("", "-wal", "-shm") if (p.parent / (p.name + s)).exists())
            out["docs"], out["doc_bytes"] = c.execute(
                "SELECT COUNT(*), COALESCE(SUM(bytes),0) FROM docs").fetchone()
            out["revisions"], out["stored_bytes"] = c.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(body)),0) FROM revisions").fetchone()
            out["cursor"] = c.execute(
                "SELECT COALESCE(MAX(seq),0) FROM revisions").fetchone()[0]
            out["last_write"] = c.execute(
                "SELECT MAX(updated_at) FROM docs").fetchone()[0]
            out["device"] = settings().get("device")
            out["workspaces"] = [
                {"workspace": w, "docs": n, "bytes": b} for w, n, b in c.execute(
                    "SELECT workspace, COUNT(*), SUM(bytes) FROM docs "
                    "GROUP BY workspace ORDER BY workspace")]
    except Exception as e:
        _note_fail(e)
        out["ok"] = False
        out["error"] = str(e)
    return out


def verify() -> dict:
    """Compare the mirror against the files, and say exactly how they differ.

    Five verdicts, kept apart because they mean five different things:
      current    the mirror has this file as it is on disk
      stale      the file changed since it was mirrored (a sweep fixes it)
      missing    on disk, never mirrored (a sweep fixes it)
      too_large  over the cap, so no sweep will ever mirror it
      held       in the mirror, no longer on disk - the point of the exercise

    `too_large` exists because without it those files were filed under
    `missing`, next to a verdict whose docstring says a sweep fixes it. The
    operator sweeps, the report does not change, and there is nothing in the
    output to tell them there is no sweep that would. Nothing is broken there;
    the report was unfalsifiable, which is worse.
    """
    on_disk = {}
    too_large = []
    for p in _walk():
        rel = _rel(p)
        try:
            size = p.stat().st_size
            # Asked of the file, not of the bytes: `_record` refuses by size
            # without reading, and a verify that reads a 4 GB file into memory
            # to decide it is too large to store has already lost.
            if size > MAX_BYTES:
                too_large.append({"path": rel, "bytes": size, "cap": MAX_BYTES})
                continue
            on_disk[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            continue
    stale, missing, held = [], [], []
    n_current = 0
    with _LOCK:
        rows = dict(_connect().execute("SELECT path, sha FROM docs").fetchall())
    for rel, sha in sorted(on_disk.items()):
        if rel not in rows:
            missing.append(rel)
        elif rows[rel] != sha:
            stale.append(rel)
        else:
            n_current += 1
    over = {t["path"] for t in too_large}
    for rel in sorted(rows):
        # An oversized file is not `held`. It is on disk; it is the mirror that
        # cannot hold it, and a path in both lists reads as two different
        # problems when it is one.
        if rel not in on_disk and rel not in over:
            held.append(rel)
    return {"ok": True, "current": n_current, "stale": stale,
            "missing": missing, "too_large": sorted(too_large, key=lambda t: t["path"]),
            "held": held,
            # `too_large` is deliberately NOT dirty. Clean means "a sweep has
            # nothing left to do", and a sweep has nothing it can do about
            # these - reporting the mirror as permanently dirty over a file it
            # was designed to refuse teaches the operator to ignore the flag.
            "clean": not stale and not missing}


def docs(workspace: str | None = None) -> list[dict]:
    with _LOCK:
        q = ("SELECT path, workspace, sha, bytes, updated_at, seq FROM docs "
             + ("WHERE workspace=? " if workspace else "") + "ORDER BY path")
        rows = _connect().execute(q, (workspace,) if workspace else ()).fetchall()
    return [{"path": p, "workspace": w, "sha": s, "bytes": b, "updated_at": u, "seq": q_}
            for p, w, s, b, u, q_ in rows]


def history(path: str, limit: int = 50) -> list[dict]:
    with _LOCK:
        rows = _connect().execute(
            "SELECT seq, sha, bytes, written_at, device FROM revisions "
            "WHERE path=? ORDER BY seq DESC LIMIT ?", (path, limit)).fetchall()
    return [{"seq": s, "sha": h, "bytes": b, "written_at": w, "device": d}
            for s, h, b, w, d in rows]


def body(path: str, seq: int | None = None) -> bytes | None:
    """One document, current or at a revision. Read-only: nothing writes disk."""
    with _LOCK:
        c = _connect()
        if seq is None:
            # The current body is not stored again: `docs.seq` names the
            # revision holding it, so the current version is read the same
            # way as any older one.
            row = c.execute(
                "SELECT r.body, r.zip FROM docs d JOIN revisions r ON r.seq = d.seq "
                "WHERE d.path=?", (path,)).fetchone()
        else:
            row = c.execute("SELECT body, zip FROM revisions WHERE path=? AND seq=?",
                            (path, seq)).fetchone()
    return _unpack(row[0], row[1]) if row else None


def restorable(path: str, seq: int | None = None) -> dict:
    """Which held revision a restore would write back, and why that one.

    Read-only. It chooses and explains; it does not write. The write belongs
    to `amp`, which owns `save_text` and therefore owns the one durable way
    anything in this harness reaches disk - a restore that wrote its own bytes
    would be the one write in the program not subject to D1.

    With no `seq`, the default is the newest revision whose body DIFFERS from
    what is on disk now. Not simply the newest: the newest is usually the
    corruption, because the mirror faithfully copied it. "Give me back the
    last version that was not this" is the request an operator actually has,
    and making them work out the seq for it at the exact moment their console
    will not start is the gap this closes.

    Failures are named rather than raised - every one of them is something the
    operator has to be told, and `die("unknown")` is not that.
    """
    out = {"ok": False, "path": path, "seq": None, "body": None,
           "on_disk": None, "error": None}
    try:
        rows = history(path, limit=10_000)
    except Exception as e:
        _note_fail(e)
        out["error"] = f"cannot read the mirror: {e}"
        return out
    if not rows:
        out["error"] = f"nothing is held at {path}"
        return out
    out["held"] = len(rows)

    p = (_ROOT / path) if _ROOT else None
    live = None
    if p and p.exists():
        try:
            live = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError as e:
            out["error"] = f"cannot read {path} on disk: {e}"
            return out
    out["on_disk"] = live

    if seq is not None:
        pick = next((r for r in rows if r["seq"] == seq), None)
        if pick is None:
            out["error"] = (f"{path} has no revision {seq} - "
                            f"held: {', '.join(str(r['seq']) for r in rows[:20])}")
            return out
        out["reason"] = "asked for"
    else:
        # `rows` is newest-first, so the first differing one IS the newest.
        pick = next((r for r in rows if r["sha"] != live), None)
        if pick is None:
            out["error"] = (f"every held revision of {path} is byte-identical to "
                            "what is on disk - there is nothing to go back to")
            return out
        out["reason"] = ("newest revision that differs from disk"
                         if live else "newest revision (nothing on disk)")

    b = body(path, pick["seq"])
    if b is None:
        # `history` named it and `body` cannot produce it: the two tables
        # disagree, which is worth saying in those words rather than as a
        # missing file.
        out["error"] = f"revision {pick['seq']} of {path} is indexed but has no body"
        return out
    out.update({"ok": True, "seq": pick["seq"], "body": b, "sha": pick["sha"],
                "bytes": pick["bytes"], "written_at": pick["written_at"],
                "unchanged": pick["sha"] == live})
    return out


def changes(since: int = 0, limit: int = 500) -> dict:
    """The feed a cloud sync would pull: every revision after `since`.

    Bodies are left out deliberately - this answers "what moved", and the
    mover is fetched by path and seq. A feed that carries bodies cannot be
    asked cheaply and often, which is the only way it is useful.
    """
    with _LOCK:
        c = _connect()
        rows = c.execute(
            "SELECT seq, path, workspace, sha, bytes, written_at, device FROM revisions "
            "WHERE seq > ? ORDER BY seq LIMIT ?", (since, limit)).fetchall()
        head = c.execute("SELECT COALESCE(MAX(seq),0) FROM revisions").fetchone()[0]
    return {
        "ok": True, "since": since, "cursor": head,
        "more": bool(rows) and rows[-1][0] < head,
        "changes": [{"seq": s, "path": p, "workspace": w, "sha": h,
                     "bytes": b, "written_at": t, "device": d}
                    for s, p, w, h, b, t, d in rows],
    }


# ------------------------------------------------------------------ upkeep


def prune(keep: int | None = None) -> dict:
    """Drop old revisions, keeping the newest `keep` of each document.

    The current body is a revision like any other now, so this says outright
    that a `seq` named by `docs` is never deleted. The window below would
    spare it anyway for any sane `keep`, but "the newest survives because it
    sorts first" is a coincidence, and the current version of a file is the
    one thing this must not be able to drop.
    """
    keep = int(keep if keep is not None else settings()["history_keep"])
    with _LOCK:
        c = _connect()
        before = c.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
        c.execute(
            "DELETE FROM revisions WHERE seq NOT IN (SELECT seq FROM docs) "
            "AND seq NOT IN ("
            "  SELECT seq FROM (SELECT seq, ROW_NUMBER() OVER "
            "    (PARTITION BY path ORDER BY seq DESC) AS n FROM revisions)"
            "  WHERE n <= ?)", (keep,))
        c.commit()
        after = c.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
    return {"ok": True, "removed": before - after, "kept": after, "keep": keep}


def compact() -> dict:
    with _LOCK:
        c = _connect()
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.execute("VACUUM")
        c.commit()
        # The vacuum itself writes every rebuilt page through the WAL, so a
        # checkpoint only before it leaves the freed space sitting in a
        # multi-megabyte -wal file. Compacting has to end by emptying it.
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return {"ok": True}


def snapshot(dest: Path) -> Path:
    """A consistent copy of the database, safe to take while it is in use.

    A plain file copy of a WAL database can miss committed pages, which is
    precisely the failure a backup must not have.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        src = _connect()
        out = sqlite3.connect(str(dest))
        try:
            src.backup(out)
        finally:
            out.close()
    return dest
