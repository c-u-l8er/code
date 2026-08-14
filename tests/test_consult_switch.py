"""The consult is the longest-running thing the console does, and nothing knew.

`advance_consult` reads its own file, sends the whole thread to the architect,
and then reads its own file AGAIN to append the answer. Between those two reads
is a network call that routinely takes minutes. Both reads resolve through
`CONSULT_DIR`, which is a module global that `_bind_state` rebinds whenever the
operator switches workspace - and `switch_blocked`, the guard whose entire job is
to refuse while something is in flight, counted live workers and a busy
orchestrator and stopped there. A consult is neither, so the switch went
straight through.

Two things are checked here, and they are two halves of the same defect:

  - the second read is now guarded, and the guard prints the answer before it
    stops, because the answer has already been paid for and exists nowhere else;
  - the switch is now refused for the duration, so the first half is a backstop
    rather than the plan.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE))


@pytest.fixture()
def amp(tmp_path, monkeypatch):
    """`amp` on a throwaway state directory.

    `AMP_HOME` before the import, always: `amp` binds every state path at import
    time, so a test that binds afterwards is pointed at the real `.amp`.
    """
    monkeypatch.setenv("AMP_HOME", str(tmp_path / "state"))
    for m in ("amp", "store"):
        sys.modules.pop(m, None)
    import amp as amp_mod
    assert amp_mod.STATE_ROOT == tmp_path / "state"
    return amp_mod


def _consult(amp, cid="c0000000001", lane="code") -> dict:
    c = {"id": cid, "lane": lane, "model": "gpt", "opened_at": amp.now(),
         "status": "open", "trigger": "test", "question": "?", "cost_tokens": 0,
         "turns": [{"role": "packet", "at": amp.now(), "text": "the packet"}]}
    amp.save_consult(c)
    return c


def _answer(text="a ruling with nothing outstanding in it") -> dict:
    return {"choices": [{"message": {"content": text}}],
            "model": "gpt", "usage": {"total_tokens": 10}}


# ------------------------------------------------- the answer survives the loss


def test_a_consult_that_vanishes_mid_turn_stops_instead_of_crashing(amp, capsys):
    """`Died`, not `TypeError: 'NoneType' object is not subscriptable`.

    The difference matters because of what is in the process when it happens: an
    architect answer that has been paid for and written down nowhere. A stack
    trace gives the operator nothing to keep.
    """
    _consult(amp)
    reply = "the whole of what the architect said, which is not written down yet"

    def vanish(msgs, model, **kw):
        amp.consult_path("c0000000001").unlink()
        return _answer(reply)

    amp.architect_chat = vanish
    with pytest.raises(amp.Died):
        amp.advance_consult("c0000000001")

    err = capsys.readouterr().err
    assert reply in err, "the answer was lost, which is the only unrecoverable part"
    assert str(amp.CONSULT_DIR) in err, "say where it looked, since that is the bug"


# ------------------------------------------------------- the switch is refused


def test_switch_is_refused_while_the_architect_is_thinking(amp):
    """And it names the lane.

    Checked from INSIDE the architect call rather than by poking the registry,
    because the claim is about when the registration is live: a `try/finally`
    that wraps the wrong statement would still leave the dict correct before and
    after, and this is the only moment at which the answer can be wrong.
    """
    _consult(amp, lane="traaviis")
    seen = {}

    def observe(msgs, model, **kw):
        seen["blocked"] = amp.switch_blocked()
        seen["lanes"] = amp.consults_in_flight()
        return _answer()

    assert amp.switch_blocked() is None, "nothing is running yet"
    amp.architect_chat = observe
    amp.advance_consult("c0000000001")

    assert seen["lanes"] == ["traaviis"]
    assert seen["blocked"] and "traaviis" in seen["blocked"], (
        f"the switch was allowed, or would not say where: {seen['blocked']!r}")
    assert amp.switch_blocked() is None, "the block outlived the call"


def test_a_failed_architect_call_still_releases_the_switch(amp):
    """The `finally` is the point. A consult that raised used to be a workspace
    the operator could never leave, and nothing on screen would say why."""
    _consult(amp)

    def boom(msgs, model, **kw):
        raise RuntimeError("the wire went down")

    amp.architect_chat = boom
    with pytest.raises(RuntimeError):
        amp.advance_consult("c0000000001")
    assert amp.consults_in_flight() == []
    assert amp.switch_blocked() is None


def test_in_flight_is_not_persisted(amp):
    """It is a fact about THIS process. A file saying "in flight" outlives the
    crash that made it false, and then the workspace can never be left at all."""
    _consult(amp)

    def observe(msgs, model, **kw):
        assert not list(amp.STATE.rglob("*inflight*"))
        return _answer()

    amp.architect_chat = observe
    amp.advance_consult("c0000000001")
