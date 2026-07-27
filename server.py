#!/usr/bin/env python3
"""code - browser console for the amp orchestration harness.

Runs LOCALLY. It shells out to the `codex` CLI (which needs your ~/.codex auth),
reads your git worktrees, and holds your OpenRouter key, so it cannot be served
as a static site from code.traaviis.com. Run it here, open localhost.

    ./amp serve            # or: python3 code/server.py --port 8787

Follows the spinner_bench.py convention: stdlib ThreadingHTTPServer, sibling
index.html / app.js / app.css, no build step.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import socket
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import amp  # noqa: E402  the harness itself

ROOT = amp.ROOT  # workspace holding the lane worktrees

HOST = "127.0.0.1"
PORT = 8787

# codex cloud exec can block for a while; serialize dispatches so two clicks
# do not race the same lane.
_DISPATCH_LOCK = threading.Lock()


# ---------------------------------------------------------------- payloads


def state_payload() -> dict:
    cfg = amp.config()
    b = amp.board()
    logged_in = amp.codex_logged_in()
    key = amp.find_openrouter_key()

    lanes = []
    for name in sorted(cfg["lanes"]):
        lane = cfg["lanes"][name]
        tasks = b.get("remote", {}).get(name, [])
        history = b.get("tasks", {}).get(name, [])
        lanes.append(
            {
                "name": name,
                "repo": lane.get("repo"),
                "path": lane.get("path"),
                "branch": lane.get("branch", "main"),
                "env_id": lane.get("env_id"),
                "bound": bool(lane.get("env_id")),
                "tasks": tasks[:5],
                "dispatch_count": len(history),
                "last_dispatch": history[0]["dispatched_at"] if history else None,
            }
        )

    return {
        "lanes": lanes,
        "polled_at": b.get("polled_at"),
        "health": {
            "codex_installed": amp.codex_available(),
            "codex_logged_in": logged_in,
            "openrouter_key": bool(key),
            "unbound_lanes": [l["name"] for l in lanes if not l["bound"]],
        },
        "consult_models": amp.CONSULT_MODELS,
        "default_model": cfg.get("consult_model", amp.DEFAULT_CONSULT),
    }


def rulings_payload() -> list[dict]:
    if not amp.RULING_DIR.exists():
        return []
    out = []
    for p in sorted(amp.RULING_DIR.glob("*.md"), reverse=True)[:40]:
        out.append(
            {
                "name": p.name,
                "lane": p.name.split("-")[0],
                "size": p.stat().st_size,
                "mtime": p.stat().st_mtime,
            }
        )
    return out


def do_poll(lane_filter: str | None) -> dict:
    if not amp.codex_logged_in():
        return {"ok": False, "error": "codex is not logged in. Run: codex login"}
    cfg = amp.config()
    b = amp.board()
    names = [lane_filter] if lane_filter else sorted(cfg["lanes"])
    seen, errors = 0, {}
    for name in names:
        lane = cfg["lanes"].get(name)
        if not lane or not lane.get("env_id"):
            continue
        try:
            tasks = amp.codex_list(env_id=lane["env_id"])
            b.setdefault("remote", {})[name] = tasks
            seen += len(tasks)
        except SystemExit as e:
            errors[name] = str(e) or "codex cloud list failed"
    b["polled_at"] = amp.now()
    amp.save_json(amp.BOARD_PATH, b)
    return {"ok": True, "tasks_seen": seen, "errors": errors, "polled_at": b["polled_at"]}


def do_dispatch(body: dict) -> dict:
    name = body.get("lane")
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return {"ok": False, "error": "prompt is empty"}
    cfg = amp.config()
    lane = cfg["lanes"].get(name)
    if not lane:
        return {"ok": False, "error": f"unknown lane {name!r}"}
    env_id = lane.get("env_id")
    if not env_id:
        return {"ok": False, "error": f"lane {name!r} has no codex env id (bind it first)"}
    if not amp.codex_logged_in():
        return {"ok": False, "error": "codex is not logged in. Run: codex login"}

    branch = body.get("branch") or lane.get("branch") or "main"
    attempts = int(body.get("attempts") or 1)

    cmd = ["codex", "cloud", "exec", "--env", env_id, "--branch", branch]
    if attempts > 1:
        cmd += ["--attempts", str(attempts)]
    cmd += [prompt]

    with _DISPATCH_LOCK:
        p = amp.run(cmd)
        out = (p.stdout + p.stderr).strip()
        if p.returncode != 0:
            return {"ok": False, "error": out or f"codex exited {p.returncode}"}
        b = amp.board()
        b["tasks"].setdefault(name, [])
        b["tasks"][name].insert(
            0,
            {
                "dispatched_at": amp.now(),
                "prompt": prompt,
                "branch": branch,
                "attempts": attempts,
                "env_id": env_id,
                "submit_output": out,
                "status": "submitted",
            },
        )
        amp.save_json(amp.BOARD_PATH, b)
    return {"ok": True, "output": out}


def do_ask(body: dict) -> dict:
    name = body.get("lane")
    question = (body.get("question") or "").strip()
    if not question:
        return {"ok": False, "error": "question is empty"}
    cfg = amp.config()
    if name not in cfg["lanes"]:
        return {"ok": False, "error": f"unknown lane {name!r}"}
    if not amp.find_openrouter_key():
        return {"ok": False, "error": "no OpenRouter key configured"}

    model_key = body.get("model") or cfg.get("consult_model", amp.DEFAULT_CONSULT)
    try:
        zpath, brief = amp.build_packet(name, question, body.get("files") or [])
        resp = amp.consult_gpt(brief, model_key)
    except SystemExit as e:
        return {"ok": False, "error": str(e) or "packet/consult failed"}

    choice = (resp.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or "(empty response)"
    usage = resp.get("usage") or {}

    amp.RULING_DIR.mkdir(parents=True, exist_ok=True)
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    rpath = amp.RULING_DIR / f"{name}-{stamp}.md"
    rpath.write_text(
        f"# GPT-5.6 ruling: {name}\n\n"
        f"- model: {resp.get('model')}\n- packet: {zpath.name}\n- asked: {amp.now()}\n"
        f"- tokens: {usage.get('prompt_tokens')} in / {usage.get('completion_tokens')} out\n\n"
        f"## Question\n\n{question}\n\n## Ruling\n\n{text}\n"
    )
    return {
        "ok": True,
        "ruling": text,
        "model": resp.get("model"),
        "usage": usage,
        "packet": zpath.name,
        "saved": rpath.name,
    }


def do_diff(lane: str, attempt: int | None, task_id: str | None) -> dict:
    b = amp.board()
    if not task_id:
        tasks = b.get("remote", {}).get(lane, [])
        if not tasks:
            return {"ok": False, "error": f"no polled tasks for {lane!r} - poll first"}
        task_id = tasks[0].get("task_id")
    if not task_id:
        return {"ok": False, "error": "task has no id in the codex payload"}
    cmd = ["codex", "cloud", "diff", str(task_id)]
    if attempt:
        cmd += ["--attempt", str(attempt)]
    p = amp.run(cmd)
    return {
        "ok": p.returncode == 0,
        "task_id": task_id,
        "diff": p.stdout,
        "error": p.stderr.strip() if p.returncode != 0 else None,
    }


def do_apply(body: dict) -> dict:
    lane = body.get("lane")
    cfg = amp.config()
    l = cfg["lanes"].get(lane)
    if not l:
        return {"ok": False, "error": f"unknown lane {lane!r}"}
    task_id = body.get("task_id")
    if not task_id:
        tasks = amp.board().get("remote", {}).get(lane, [])
        if not tasks:
            return {"ok": False, "error": "no polled tasks - poll first"}
        task_id = tasks[0].get("task_id")
    cmd = ["codex", "cloud", "apply", str(task_id)]
    if body.get("attempt"):
        cmd += ["--attempt", str(body["attempt"])]
    p = amp.run(cmd, cwd=ROOT / l["path"])
    return {
        "ok": p.returncode == 0,
        "output": (p.stdout + p.stderr).strip(),
        "task_id": task_id,
        "path": l["path"],
    }


def do_credits() -> dict:
    import urllib.request

    key = amp.find_openrouter_key()
    if not key:
        return {"ok": False, "error": "no OpenRouter key"}
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {key}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode())["data"]
        return {"ok": True, "remaining": round(d["total_credits"] - d["total_usage"], 2)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_bind_env(body: dict) -> dict:
    cfg = amp.config()
    name, env_id = body.get("lane"), (body.get("env_id") or "").strip()
    if name not in cfg["lanes"]:
        return {"ok": False, "error": f"unknown lane {name!r}"}
    if not env_id:
        return {"ok": False, "error": "env_id is empty"}
    cfg["lanes"][name]["env_id"] = env_id
    amp.save_json(amp.CONFIG_PATH, cfg)
    return {"ok": True, "lane": name, "env_id": env_id}


# ---------------------------------------------------------------- http


class Handler(BaseHTTPRequestHandler):
    server_version = "ampcode/0.1"

    def log_message(self, fmt, *args):  # quieter console
        pass

    def _send(self, code: int, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _static(self, rel: str):
        # confine to this directory
        target = (HERE / rel).resolve()
        if not str(target).startswith(str(HERE)) or not target.is_file():
            return self._send(404, {"error": "not found"})
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self._static("index.html")
            if u.path == "/api/state":
                return self._send(200, state_payload())
            if u.path == "/api/rulings":
                return self._send(200, {"rulings": rulings_payload()})
            if u.path == "/api/ruling":
                name = (q.get("name") or [""])[0]
                p = amp.RULING_DIR / Path(name).name
                if not p.is_file():
                    return self._send(404, {"error": "no such ruling"})
                return self._send(200, {"ok": True, "text": p.read_text()})
            if u.path == "/api/credits":
                return self._send(200, do_credits())
            if u.path == "/api/diff":
                lane = (q.get("lane") or [""])[0]
                attempt = q.get("attempt")
                return self._send(
                    200, do_diff(lane, int(attempt[0]) if attempt else None, (q.get("task_id") or [None])[0])
                )
            if u.path.startswith("/api/"):
                return self._send(404, {"error": "unknown endpoint"})
            return self._static(u.path.lstrip("/"))
        except Exception:
            traceback.print_exc()
            return self._send(500, {"error": traceback.format_exc(limit=3)})

    def do_POST(self):
        u = urlparse(self.path)
        try:
            body = self._body()
            if u.path == "/api/poll":
                return self._send(200, do_poll(body.get("lane")))
            if u.path == "/api/dispatch":
                return self._send(200, do_dispatch(body))
            if u.path == "/api/ask":
                return self._send(200, do_ask(body))
            if u.path == "/api/apply":
                return self._send(200, do_apply(body))
            if u.path == "/api/lane/env":
                return self._send(200, do_bind_env(body))
            return self._send(404, {"error": "unknown endpoint"})
        except Exception:
            traceback.print_exc()
            return self._send(500, {"error": traceback.format_exc(limit=3)})


def free_port(start: int) -> int:
    for p in range(start, start + 40):
        with socket.socket() as s:
            if s.connect_ex((HOST, p)) != 0:
                return p
    return start


def main(argv=None):
    ap = argparse.ArgumentParser(description="amp browser console")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--open", action="store_true", help="open a browser")
    a = ap.parse_args(argv)

    port = free_port(a.port)
    srv = ThreadingHTTPServer((a.host, port), Handler)
    url = f"http://{a.host}:{port}"
    print(f"amp console -> {url}")
    if not amp.codex_logged_in():
        print("  ! codex is not logged in - run `codex login` to enable dispatch")
    if not amp.find_openrouter_key():
        print("  ! no OpenRouter key - escalation disabled")
    if a.open:
        import webbrowser

        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
