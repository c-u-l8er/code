"""The console's front door.

Downstream of these routes are three `shell=True` call sites, so a request that
arrives is a command that runs, and the only thing between a web page and those
call sites is `Handler._refuse`. That function is checked here the way a lock is
checked: not by reading it, but by asking it for the thing it exists to stop.

The second half is shape. Every route returns a dict the page indexes into by
name, and a key that quietly stops being sent is a blank panel rather than an
error - the browser reads `undefined` and draws nothing. One row per route,
naming the keys the page actually reads, so that removing one is a red test
rather than an empty box somebody notices a week later.
"""

from __future__ import annotations

import json
import sys
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE))

_MODULES = ("amp", "store", "blueprint", "preview", "server")


class Console:
    """A client that says exactly what it means to say.

    `http.client` rather than `urllib` because every check in `_refuse` reads a
    header, and two of them - `Host` and a second request on one kept-alive
    connection - are things a convenience wrapper decides for you.
    """

    def __init__(self, port: int, token: str, mod):
        self.port, self.token, self.amp = port, token, mod.amp

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    def open(self) -> HTTPConnection:
        return HTTPConnection("127.0.0.1", self.port, timeout=30)

    def send(self, conn, method, path, body=None, *, token=..., host=None,
             headers=None):
        h = {"Host": self.host if host is None else host}
        if token is ...:
            token = self.token
        if token:
            h[self.amp.TOKEN_HEADER] = token
        h.update(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        conn.request(method, path, payload, h)
        r = conn.getresponse()
        raw = r.read()
        return r.status, dict(r.getheaders()), raw

    def req(self, method, path, body=None, **kw):
        conn = self.open()
        try:
            return self.send(conn, method, path, body, **kw)
        finally:
            conn.close()

    def get(self, path, **kw):
        return self.req("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.req("POST", path, body if body is not None else {}, **kw)

    def json(self, path, **kw) -> dict:
        code, _, raw = self.get(path, **kw)
        assert code == 200, f"{path} answered {code}: {raw[:300]!r}"
        return json.loads(raw)


@pytest.fixture(scope="module")
def console(tmp_path_factory):
    """A real console on a real socket, on a throwaway state directory.

    A real one because the thing under test is the request: the header checks
    run in `BaseHTTPRequestHandler`, and a test that calls `_refuse` directly
    would be testing a function nothing routes through.
    """
    mp = pytest.MonkeyPatch()
    home = tmp_path_factory.mktemp("http") / "state"
    mp.setenv("AMP_HOME", str(home))
    for m in _MODULES:
        sys.modules.pop(m, None)
    import server as mod
    assert mod.amp.STATE_ROOT == home, "the console is pointed at the real .amp"
    # A lane, because half of what these routes return is per-lane and an empty
    # workspace makes every one of them pass by returning nothing.
    mod.amp.save_json(mod.amp.CONFIG_PATH, {
        "lanes": {"code": {"path": str(CODE), "branch": "main"}},
        "consult_model": mod.amp.DEFAULT_CONSULT,
    })

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    port = httpd.server_address[1]
    mod.set_allowed_hosts("127.0.0.1", port)
    token = mod.mint_console_token()
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield Console(port, token, mod)
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=10)
        mp.undo()
        for m in _MODULES:
            sys.modules.pop(m, None)


# --------------------------------------------------------------------- the gate


def test_the_console_answers_itself(console):
    code, _, raw = console.get("/api/state")
    assert code == 200
    assert json.loads(raw)["lanes"] is not None


def test_no_token_is_no_answer(console):
    code, _, raw = console.get("/api/state", token=None)
    assert code == 401
    # The refusal says where the token is, because the client that hits this is
    # a worker or a script, and "401" alone is not something it can act on.
    assert console.amp.TOKEN_HEADER in json.loads(raw)["error"]


def test_a_wrong_token_is_no_answer(console):
    code, _, _ = console.get("/api/state", token="x" * 43)
    assert code == 401


def test_a_token_that_is_a_prefix_of_the_real_one_is_refused(console):
    """`compare_digest`, not `==`. The interesting failure is not that a short
    token is accepted - it is that comparing them with `==` answers faster the
    sooner they differ, which is a token you can read one byte at a time."""
    code, _, _ = console.get("/api/state", token=console.token[:-1])
    assert code == 401


def test_a_rebound_host_is_refused_before_anything_else(console):
    """The one attack the token cannot stop: under DNS rebinding the page is
    same-origin, so it sends a real `Origin`, a real `Sec-Fetch-Site`, and the
    token it read out of the index.html it was entitled to fetch."""
    code, _, raw = console.get("/api/state", host="evil.example:80")
    assert code == 403
    assert "evil.example" in json.loads(raw)["error"]


def test_the_host_check_also_covers_the_page(console):
    """Serving index.html to a rebound origin is what hands over the token, so
    the check cannot start at `/api/`."""
    code, _, _ = console.get("/", host="evil.example:80")
    assert code == 403


def test_a_missing_host_header_is_refused(console):
    code, _, raw = console.get("/api/state", host="")
    assert code == 403
    assert "(no Host header)" in json.loads(raw)["error"]


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_fetch_metadata_refuses_what_the_token_would_let_through(console, site):
    """`same-site` is refused on purpose: nothing here is served from a sibling
    subdomain, so a `same-site` request is not this console. Sent WITH a valid
    token, because a check that only fires when the token is missing is not a
    second layer."""
    code, _, raw = console.get("/api/state", headers={"Sec-Fetch-Site": site})
    assert code == 403
    assert site in json.loads(raw)["error"]


@pytest.mark.parametrize("site", ["same-origin", "none"])
def test_the_console_and_the_address_bar_are_let_through(console, site):
    assert console.get("/api/state", headers={"Sec-Fetch-Site": site})[0] == 200


def test_a_foreign_origin_is_refused(console):
    code, _, raw = console.get(
        "/api/state", headers={"Origin": "http://evil.example"})
    assert code == 403
    assert "evil.example" in json.loads(raw)["error"]


def test_our_own_origin_is_not(console):
    code, _, _ = console.get(
        "/api/state", headers={"Origin": f"http://{console.host}"})
    assert code == 200


def test_a_static_asset_needs_no_token(console):
    """They are files on disk next to the server. A page that got past the Host
    check to read app.css read a file it could have read from the repository."""
    code, _, raw = console.get("/app.css", token=None)
    assert code == 200 and raw


def test_the_page_is_served_the_token_and_nothing_else_is(console):
    """The token reaches the browser here and only here - no route hands it
    out, and it is never in a file a build step could commit."""
    code, _, raw = console.get("/", token=None)
    assert code == 200
    assert console.token.encode() in raw
    assert b"__AMP_TOKEN__" not in raw


def test_a_refused_post_does_not_run_its_handler(console):
    """The assertion that matters: not the status, the effect. `/api/mission`
    is the one text in the harness no agent may author, and it goes out with
    the doctrine to every planner and worker from the next prompt onward."""
    before = console.amp.mission()
    code, _, _ = console.post("/api/mission", {"text": "smuggled in"},
                              token=None)
    assert code == 401
    assert console.amp.mission() == before

    # And the same request with the token DOES write it - otherwise the check
    # above passes for a route that never worked.
    code, _, raw = console.post("/api/mission", {"text": "written by the operator"})
    assert code == 200 and json.loads(raw)["ok"]
    assert console.amp.mission() != before


def test_a_refused_post_still_drains_its_body(console):
    """A refusal that leaves the body in the socket makes the NEXT request on a
    kept-alive connection start mid-JSON, and that surfaces as an unrelated
    parse error somewhere else entirely."""
    conn = console.open()
    try:
        code, _, _ = console.send(conn, "POST", "/api/mission",
                                  {"text": "x" * 4000}, token=None)
        assert code == 401
        code, _, raw = console.send(conn, "GET", "/api/state")
        assert code == 200, "the second request on this connection started mid-body"
        assert "lanes" in json.loads(raw)
    finally:
        conn.close()


# ------------------------------------------------------------ what is not there


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_an_unknown_api_route_is_a_404_that_says_so(console, method):
    code, _, raw = console.req(method, "/api/nothing-like-this",
                               {} if method == "POST" else None)
    assert code == 404
    assert json.loads(raw)["error"] == "unknown endpoint"


@pytest.mark.parametrize("path", [
    "/../amp.py",              # the directory above
    "/../../../etc/passwd",    # off the disk entirely
    "/nothing-like-this.js",
    "/tests",                  # a directory is not a file
])
def test_static_serving_is_confined_to_this_directory(console, path):
    assert console.get(path)[0] == 404


def test_confinement_is_a_path_test_and_not_a_prefix_test(console, tmp_path,
                                                          monkeypatch):
    """The check was `str(target).startswith(str(HERE))`, which is true of any
    SIBLING whose name merely begins with this directory's - `code-backup` is
    not inside `code`, and a resolved path that leaves the tree by the front
    door passes a prefix test on the way out.

    `HERE` is moved rather than a probe directory created next to the real one,
    because a test that writes into the repository to prove a point is a test
    that leaves the point lying there when it dies.
    """
    import server

    here = tmp_path / "code"
    here.mkdir()
    (here / "app.css").write_text("the real one")
    sib = tmp_path / "code-backup"
    sib.mkdir()
    (sib / "secrets.txt").write_text("not yours")
    monkeypatch.setattr(server, "HERE", here)

    code, _, raw = console.get("/app.css")
    assert (code, raw) == (200, b"the real one"), "HERE did not move"
    assert console.get("/../code-backup/secrets.txt")[0] == 404, \
        "a sibling directory was served out of the tree"


@pytest.mark.parametrize("name", ["../../../etc/passwd", "nothing-like-this.md"])
def test_a_ruling_is_read_by_name_and_only_by_name(console, name):
    """Only the file NAME is taken from the request, so a path with a slash in
    it resolves to a name that is not in the ruling directory."""
    code, _, raw = console.get(f"/api/ruling?name={name}")
    assert code == 404
    assert json.loads(raw)["error"] == "no such ruling"


def test_a_report_is_read_by_name_and_only_by_name(console):
    code, _, _ = console.get("/reports/../../../etc/passwd")
    assert code == 404


def test_a_bad_query_parameter_is_answered_not_swallowed(console):
    """`limit` is passed to `int()` with nothing between. This is a 500 today
    rather than a 400 - what is pinned here is that the console ANSWERS, in
    JSON, and is still up afterwards, because a handler that raised on the way
    out used to be indistinguishable from a hang."""
    code, _, raw = console.get("/api/history?limit=not-a-number")
    assert code in (400, 500)
    assert json.loads(raw)["error"]
    assert console.get("/api/state")[0] == 200


# ---------------------------------------------------------------------- shape
#
# The keys are the ones app.js indexes into. A route that stops sending one is
# a panel that draws nothing rather than an error anybody sees.


@pytest.mark.parametrize("path, keys", [
    ("/api/state", ("lanes", "workspace", "summary", "health", "limits")),
    ("/api/rulings", ("rulings",)),
    ("/api/lanes", ("lanes", "modes", "means", "default")),
    ("/api/findings", ("ok", "findings", "summary", "doctrine", "doctrine_state")),
    ("/api/ideas", ("ok", "ideas")),
    ("/api/obligations", ("ok", "obligations", "summary")),
    ("/api/direction", ("ok", "direction")),
    ("/api/doctrine", ("ok", "text", "core", "mission", "workspace", "stats")),
    ("/api/lane/directions", ("ok", "mission", "fields", "lanes", "after_bar")),
    ("/api/blueprint", ("ok", "rungs", "stacks", "nodes", "edges")),
    ("/api/blueprint/triggers", ("ok", "triggers", "events", "rungs", "stacks")),
    ("/api/blueprint/actions", ("ok", "actions")),
    ("/api/db", ("ok", "path", "exists", "settings", "schema_code", "schema_db")),
])
def test_a_route_sends_the_keys_the_page_reads(console, path, keys):
    got = console.json(path)
    missing = [k for k in keys if k not in got]
    assert not missing, f"{path} no longer sends {missing}"


def test_the_direction_fields_are_the_ones_the_editor_draws(console):
    """The editor builds one input per name in this list. A field added to
    `DIRECTION_FIELDS` and not sent here is a field nobody can fill in."""
    assert tuple(console.json("/api/lane/directions")["fields"]) == \
        console.amp.DIRECTION_FIELDS


def test_a_lane_carries_its_judged_rung_beside_its_written_claim(console):
    """The one failure that screen exists to catch is a direction claiming more
    than the lane's reviews ever did, and that is invisible while the written
    claim and the judged rung live on different pages."""
    lanes = console.json("/api/lane/directions")["lanes"]
    assert lanes, "no lanes at all"
    for lane in lanes:
        assert {"name", "mode", "rung", "direction"} <= set(lane)


def test_the_database_export_is_a_file_and_not_json(console):
    code, headers, raw = console.get("/api/db/export")
    assert code == 200
    assert headers["Content-Type"] == "application/octet-stream"
    assert "amp-backup.db" in headers["Content-Disposition"]
    assert raw[:16] == b"SQLite format 3\x00", "that is not a database"
