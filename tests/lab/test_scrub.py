"""D34: stderr is scrubbed of paths, ROOT, home, login and chunk stems."""

from __future__ import annotations

from pathlib import Path

from traceback_lab.scrub import BoundedStderr, Scrubber


def test_donor_named_path_is_redacted() -> None:
    scrubber = Scrubber(
        root=Path("/tmp/lab-root"),
        chunk_stems=["DonorName_plasma"],
        home="/Users/someone",
        login="someone",
    )
    line = "[E::hts_open] fail to open /Users/someone/Downloads/DonorName_plasma.bam: truncated"
    out = scrubber.line(line)
    assert "DonorName" not in out
    assert "/Users" not in out
    assert "someone" not in out
    assert out == "[E::hts_open] fail to open <path>: truncated"


def test_bare_chunk_stem_and_login_are_redacted() -> None:
    scrubber = Scrubber(chunk_stems=["DonorName_plasma"], home="/home/someone", login="someone")
    out = scrubber.line("reading DonorName_plasma as someone (someoneelse stays)")
    assert out == "reading <redacted> as <redacted> (someoneelse stays)"


def test_root_becomes_a_token_and_keeps_store_relative_tail(tmp_path: Path) -> None:
    root = tmp_path / "root"
    scrubber = Scrubber(root=root, home=None, login=None)
    out = scrubber.line(f"cannot write {root}/lab/inputs/.staging-ab12/aligned.bam")
    assert out == "cannot write <ROOT>/lab/inputs/.staging-ab12/aligned.bam"


def test_quoted_path_with_spaces_and_tilde() -> None:
    scrubber = Scrubber(home=None, login=None)
    assert scrubber.line("open '/Volumes/My Drive/x.bam' failed") == "open '<path>' failed"
    assert scrubber.line("see ~/Downloads/x.bam") == "see <path>"


def test_relative_tokens_and_ratios_are_untouched() -> None:
    scrubber = Scrubber(home=None, login=None)
    assert scrubber.line("processed 12/40 chunks; a/b ok") == "processed 12/40 chunks; a/b ok"


def test_lines_are_capped() -> None:
    scrubber = Scrubber(home=None, login=None)
    out = scrubber.line("x" * 1000)
    assert len(out) == 300


def test_bounded_stderr_keeps_head_and_tail() -> None:
    buffer = BoundedStderr(head=2, tail=3)
    for number in range(10):
        buffer.add(f"line {number}\n")
    assert buffer.raw_lines() == [
        "line 0",
        "line 1",
        "… 5 lines omitted …",
        "line 7",
        "line 8",
        "line 9",
    ]
    scrubbed = buffer.scrubbed(Scrubber(home=None, login=None), last=2)
    assert scrubbed == ["line 8", "line 9"]


def test_delimited_paths_and_short_stems_are_redacted() -> None:
    scrubber = Scrubber(chunk_stems=["AB"], home=None, login=None)
    assert scrubber.line("open </private/tmp/run.bam> failed") == "open <<path>> failed"
    assert scrubber.line("reading AB.bam") == "reading <redacted>.bam"
    assert scrubber.line("(/opt/x/y)") == "(<path>)"
