#!/usr/bin/env python3
"""amp - lead-manager harness for the [&] stack.

One chat window (Claude) dispatches coding work to Codex Cloud workers across
many repos, tracks them on a board, and escalates to GPT-5.6 when a lane stalls.

Layers:
  lanes    a named (repo, worktree, codex env) triple - one per sub-repo
  workers  `codex cloud exec` tasks, billed to the ChatGPT subscription
  consult  GPT-5.6 via OpenRouter, reached with a packet built from a lane

Stdlib only. No secrets are stored in tracked files.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent                   # the program (this repo)
ROOT = Path(os.environ.get("AMP_ROOT") or HERE.parent)   # workspace holding the lanes

# State lives OUTSIDE this repo so the program stays shareable and no key,
# board, packet, or ruling is ever committed. Override with AMP_HOME.
STATE = Path(os.environ.get("AMP_HOME") or (ROOT / ".amp"))
CONFIG_PATH = STATE / "config.json"
BOARD_PATH = STATE / ".board.json"
SECRETS_PATH = STATE / ".secrets.json"
PACKET_DIR = STATE / "packets"
RULING_DIR = STATE / "rulings"

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# GPT-5.6 family on OpenRouter, cheapest first. There is no gpt-5.6-codex.
CONSULT_MODELS = {
    "luna": "openai/gpt-5.6-luna-pro",    # $1 / $6  per M
    "terra": "openai/gpt-5.6-terra-pro",  # $2.5 / $15 per M
    "sol": "openai/gpt-5.6-sol-pro",      # $5 / $30 per M
}
DEFAULT_CONSULT = "terra"

# codex cloud list --limit is capped at 20 by the CLI.
LIST_LIMIT = 20

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


def run(cmd: list[str], cwd: Path | None = None, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, check=check
    )


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


def lane_or_die(cfg: dict, name: str) -> dict:
    lane = cfg["lanes"].get(name)
    if not lane:
        known = ", ".join(sorted(cfg["lanes"])) or "(none configured)"
        die(f"unknown lane {name!r}. Known lanes: {known}")
    return lane


# ---------------------------------------------------------------- codex bridge


def codex_available() -> bool:
    return shutil.which("codex") is not None


def codex_logged_in() -> bool:
    if not codex_available():
        return False
    p = run(["codex", "login", "status"])
    return "not logged in" not in (p.stdout + p.stderr).lower()


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


def cmd_doctor(args):
    cfg = config()
    print("amp doctor")
    print("=" * 60)

    ok = True

    codex_ok = codex_available()
    print(f"  codex CLI          {'OK' if codex_ok else 'MISSING (npm i -g @openai/codex)'}")
    if codex_ok:
        v = run(["codex", "--version"]).stdout.strip()
        print(f"    version          {v}")
    ok &= codex_ok

    logged = codex_logged_in()
    print(f"  codex auth         {'OK' if logged else 'NOT LOGGED IN -> run: codex login'}")
    ok &= logged

    gh_ok = shutil.which("gh") is not None
    print(f"  gh CLI             {'OK' if gh_ok else 'missing (optional)'}")

    key = find_openrouter_key()
    if key:
        print(f"  openrouter key     OK ({key[:12]}...)")
    else:
        print("  openrouter key     MISSING -> escalation disabled")
        ok = False

    lanes = cfg["lanes"]
    print(f"  lanes configured   {len(lanes)}")
    unbound = [n for n, l in lanes.items() if not l.get("env_id")]
    for n in sorted(lanes):
        lane = lanes[n]
        mark = "OK " if lane.get("env_id") else "NO ENV"
        print(f"    {mark:7} {n:<22} {lane.get('repo','?')}")
    if unbound:
        print()
        print("  Lanes without a codex env id cannot dispatch. Get ids with:")
        print("    codex cloud            # interactive TUI, copy the env id per repo")
        print("    ./amp lane env <name> <ENV_ID>")
        ok = False

    print("=" * 60)
    print("READY" if ok else "NOT READY - resolve the items above")
    return 0 if ok else 1


def cmd_lanes(args):
    cfg = config()
    lanes = cfg["lanes"]
    if not lanes:
        print("no lanes. Add one:\n  ./amp lane add wrl --repo c-u-l8er/WRL --path WRL")
        return 0
    print(f"{'LANE':<22} {'REPO':<32} {'BRANCH':<14} ENV")
    for name in sorted(lanes):
        l = lanes[name]
        env = l.get("env_id") or "-- unbound --"
        print(f"{name:<22} {l.get('repo',''):<32} {l.get('branch','main'):<14} {env}")
    return 0


def cmd_lane_add(args):
    cfg = config()
    path = args.path or args.name
    abs_path = (ROOT / path).resolve()
    if not abs_path.exists():
        die(f"path does not exist: {abs_path}")
    repo = args.repo
    if not repo:
        p = run(["git", "remote", "get-url", "origin"], cwd=abs_path)
        url = p.stdout.strip()
        if url.startswith("git@github.com:"):
            repo = url.split(":", 1)[1].removesuffix(".git")
        elif "github.com/" in url:
            repo = url.split("github.com/", 1)[1].removesuffix(".git")
        else:
            die(f"could not infer repo from origin {url!r}; pass --repo owner/name")
    cfg["lanes"][args.name] = {
        "repo": repo,
        "path": str(abs_path.relative_to(ROOT)),
        "branch": args.branch,
        "env_id": args.env,
    }
    save_json(CONFIG_PATH, cfg)
    print(f"lane {args.name!r} -> {repo} ({path}) branch={args.branch}")
    if not args.env:
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


def cmd_dispatch(args):
    cfg = config()
    lane = lane_or_die(cfg, args.lane)
    env_id = lane.get("env_id")
    if not env_id:
        die(
            f"lane {args.lane!r} has no codex env id.\n"
            f"  Run `codex cloud` to browse environments, then:\n"
            f"    ./amp lane env {args.lane} <ENV_ID>"
        )
    if not codex_logged_in():
        die("codex is not logged in. Run: codex login")

    prompt = args.prompt
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text()

    branch = args.branch or lane.get("branch") or "main"
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


def cmd_poll(args):
    if not codex_logged_in():
        die("codex is not logged in. Run: codex login")
    cfg = config()
    b = board()

    lanes = [args.lane] if args.lane else sorted(cfg["lanes"])
    seen = 0
    for name in lanes:
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
    for name in sorted(cfg["lanes"]):
        lane = cfg["lanes"][name]
        tasks = remote.get(name, [])
        env = lane.get("env_id")
        head = f"{name}  [{lane.get('repo','?')}]"
        if not env:
            print(f"  {head}\n      unbound - no codex env id")
            continue
        if not tasks:
            print(f"  {head}\n      idle")
            continue
        print(f"  {head}")
        for t in tasks[:5]:
            title = (t.get("title") or "").replace("\n", " ")[:46]
            print(f"      {t.get('status','?'):<12} {str(t.get('task_id'))[:20]:<22} {title}")
    print("=" * 78)
    stalled = [n for n, ts in remote.items() if any(_is_stalled(t) for t in ts)]
    if stalled:
        print(f"  needs attention: {', '.join(stalled)}   (./amp ask <lane> -q '...')")
    return 0


def _is_stalled(t: dict) -> bool:
    s = (t.get("status") or "").lower()
    return s in {"failed", "error", "cancelled", "canceled"}


def cmd_diff(args):
    cfg = config()
    lane_or_die(cfg, args.lane) if args.lane in cfg["lanes"] else None
    task_id = args.task_id or _latest_task_id(args.lane)
    cmd = ["codex", "cloud", "diff", task_id]
    if args.attempt:
        cmd += ["--attempt", str(args.attempt)]
    p = run(cmd)
    print(p.stdout or p.stderr)
    return p.returncode


def cmd_apply(args):
    task_id = args.task_id or _latest_task_id(args.lane)
    cfg = config()
    lane = lane_or_die(cfg, args.lane)
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
            brief.write(f"- {h['dispatched_at']} ({h.get('status')}): {h['prompt'][:200]}\n")
        brief.write("\n")
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
        for rel in extra_files:
            src = Path(rel)
            if not src.is_absolute():
                src = repo_path / rel
            if src.exists() and src.is_file():
                z.write(src, f"files/{src.name}")
            else:
                z.writestr(f"files/MISSING-{Path(rel).name}.txt", f"not found: {rel}")

    return zpath, brief_text


def consult_gpt(brief: str, model_key: str, max_tokens: int = 8000) -> dict:
    model = CONSULT_MODELS.get(model_key, model_key)
    key = openrouter_key()
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are the consulting architect for the [&] protocol stack. "
                    "You are given a packet describing a stalled or ambiguous engineering "
                    "situation. Rule decisively. State: (1) the ruling, (2) the reasoning, "
                    "(3) the exact next build order as numbered steps, (4) anything you are "
                    "refusing to rule on and why. Be concrete and terse. Do not hedge."
                ),
            },
            {"role": "user", "content": brief},
        ],
        "max_tokens": max_tokens,
    }
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


def cmd_packet(args):
    zpath, brief = build_packet(args.lane, args.question or "(no question stated)", args.file or [])
    print(f"packet: {zpath}  ({zpath.stat().st_size} bytes)")
    print("forward this to GPT-5.6 manually, or run ./amp ask to send it automatically")
    return 0


def cmd_ask(args):
    cfg = config()
    model_key = args.model or cfg.get("consult_model", DEFAULT_CONSULT)
    zpath, brief = build_packet(args.lane, args.question, args.file or [])
    print(f"packet: {zpath}")
    print(f"consulting {CONSULT_MODELS.get(model_key, model_key)} ...")

    t0 = time.time()
    resp = consult_gpt(brief, model_key)
    dt = time.time() - t0

    choice = (resp.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or "(empty response)"
    usage = resp.get("usage") or {}

    RULING_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    rpath = RULING_DIR / f"{args.lane}-{stamp}.md"
    rpath.write_text(
        f"# GPT-5.6 ruling: {args.lane}\n\n"
        f"- model: {resp.get('model')}\n- packet: {zpath.name}\n- asked: {now()}\n"
        f"- tokens: {usage.get('prompt_tokens')} in / {usage.get('completion_tokens')} out\n\n"
        f"## Question\n\n{args.question}\n\n## Ruling\n\n{text}\n"
    )

    print("=" * 78)
    print(text)
    print("=" * 78)
    print(f"saved: {rpath}   ({dt:.0f}s, {usage.get('total_tokens','?')} tokens)")
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

    sub.add_parser("doctor", help="preflight: codex auth, keys, lane bindings").set_defaults(
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
    a.add_argument("--env", help="codex cloud environment id")
    a.set_defaults(func=cmd_lane_add)

    e = lane.add_parser("env", help="bind a codex env id to a lane")
    e.add_argument("name")
    e.add_argument("env_id")
    e.set_defaults(func=cmd_lane_env)

    d = sub.add_parser("dispatch", help="send a task to a lane's Codex Cloud worker")
    d.add_argument("lane")
    d.add_argument("prompt", nargs="?", default="")
    d.add_argument("--prompt-file")
    d.add_argument("--attempts", type=int, default=1, help="best-of-N")
    d.add_argument("--branch")
    d.set_defaults(func=cmd_dispatch)

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

    args = p.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
