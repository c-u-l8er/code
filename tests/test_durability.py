"""How this harness writes files, asserted rather than described.

Two tests, and the second one is the point.

The first pins the order inside `save_text`: bytes flushed and fsynced, THEN
renamed, then the directory fsynced. That is the discipline `save_json` already
had.

The second is a grep. It fails when a NEW raw writer appears anywhere in the
tree. The defect this file was written for was not that four call sites were
wrong - it was that nothing noticed three of them for the length of the
project, and a rule that lives only in a comment is a rule the next writer does
not read.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE))


@pytest.fixture()
def amp(tmp_path, monkeypatch):
    """`amp` bound to a throwaway state directory.

    `AMP_HOME` is read at import, so it has to be set before the import - which
    is why this is a fixture and not a module-level import.
    """
    monkeypatch.setenv("AMP_HOME", str(tmp_path / "state"))
    for m in ("amp", "store"):
        sys.modules.pop(m, None)
    import amp as amp_mod
    return amp_mod


# ------------------------------------------------------------ the write order


def test_save_text_fsyncs_before_it_renames(amp, tmp_path, monkeypatch):
    """fsync(file) -> replace -> fsync(dir), in that order and all three.

    A flush after the rename is the failure this guards: the directory entry
    reaches the disk pointing at bytes that have not, and what a reader finds
    is a zero-length file where a whole one used to be.
    """
    seen: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def spy_fsync(fd):
        # Which fsync this is, asked of the descriptor rather than assumed from
        # the order - otherwise this test would pass on any two fsyncs at all.
        try:
            kind = "dir" if os.fstat(fd).st_mode & 0o040000 else "file"
        except OSError:
            kind = "file"
        seen.append(f"fsync-{kind}")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(os, "replace", lambda a, b: (seen.append("replace"), real_replace(a, b))[1])
    # Path.replace does not go through os.replace, so it gets its own spy.
    real_path_replace = Path.replace
    monkeypatch.setattr(
        Path, "replace",
        lambda self, t: (seen.append("replace"), real_path_replace(self, t))[1])

    p = tmp_path / "d" / "thing.md"
    amp.save_text(p, "hello\n")

    assert p.read_text() == "hello\n"
    assert seen[:1] == ["fsync-file"], f"renamed before fsyncing the bytes: {seen}"
    assert "replace" in seen
    assert seen.index("fsync-file") < seen.index("replace"), seen
    assert seen[-1] == "fsync-dir", f"directory never fsynced: {seen}"


def test_save_text_temp_file_is_a_sibling(amp, tmp_path, monkeypatch):
    """A temp on another filesystem turns `replace` into copy-and-delete.

    That silently removes the atomicity the whole function exists for, so the
    sibling is a requirement and not a convention.
    """
    seen: list[tuple[Path, Path]] = []
    real = Path.replace
    monkeypatch.setattr(Path, "replace",
                        lambda self, t: (seen.append((self, Path(t))), real(self, t))[1])
    p = tmp_path / "d" / "thing.md"
    amp.save_text(p, "x\n")
    src, dst = seen[0]
    assert src.parent == dst.parent, f"temp is not a sibling: {src} -> {dst}"


def test_save_json_is_save_text(amp, tmp_path, monkeypatch):
    """One write path, not two that have to be kept in step."""
    called = []
    monkeypatch.setattr(amp, "save_text", lambda p, t: called.append((p, t)))
    amp.save_json(tmp_path / "a.json", {"b": 1})
    assert called, "save_json no longer goes through save_text"


# ------------------------------------------------------- no new raw writers

# Every way to put bytes in a file that does not go through `save_text`.
RAW_WRITE = re.compile(r"""\.write_text\(|\.write_bytes\(|\bopen\([^)]*["']w[b]?["']""")

# The writes that are deliberately raw, each with the reason it cannot be
# `save_text`. A line is exempt by its CONTENT, not by its number: line numbers
# move, and an exemption that drifts onto a different line is an exemption
# granted to a write nobody approved.
ALLOWED = {
    # save_text itself.
    'with open(tmp, "w", encoding="utf-8") as f:',
    # The console lock and the console token. Both are O_EXCL / 0600 creates
    # through `os.open`, which is the whole point of them: `save_text` would
    # clobber the lock it is meant to contend for, and would give the token a
    # world-readable window. Both already flush, fsync and fsync the directory.
    'with os.fdopen(fd, "w") as f:',
}


def _sources() -> list[Path]:
    return sorted(p for p in CODE.glob("*.py"))


def test_no_new_raw_writers():
    offenders = []
    for path in _sources():
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if RAW_WRITE.search(line) and line.strip() not in ALLOWED:
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert not offenders, (
        "raw file writes outside save_text:\n  " + "\n  ".join(offenders)
        + "\n\nUse amp.save_text(path, text). If this write genuinely cannot "
          "(an O_EXCL create, a binary stream), add the line to ALLOWED in this "
          "file with the reason.")


def test_the_grep_can_fail(tmp_path):
    """The guard above, proven to be a guard.

    A test that only ever passes is indistinguishable from a test that cannot
    fail, and this one's whole job is to fail one day.
    """
    assert RAW_WRITE.search('    p.write_text("x")')
    assert RAW_WRITE.search('    with open(p, "w") as f:')
    assert RAW_WRITE.search("    q.write_bytes(b'x')")
    assert not RAW_WRITE.search("    amp.save_text(p, text)")
    assert not RAW_WRITE.search('    with open(p) as f:')
