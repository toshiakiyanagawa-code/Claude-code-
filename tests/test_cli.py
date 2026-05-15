import json
from pathlib import Path

from click.testing import CliRunner

from podedit.cli import _browser_url, cli


def test_browser_url_prefers_codespaces_forwarded_url(monkeypatch) -> None:
    monkeypatch.setenv("CODESPACE_NAME", "sample-space")
    monkeypatch.setenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev")

    assert _browser_url("0.0.0.0", 8765) == "https://sample-space-8765.app.github.dev"


def test_browser_url_rewrites_wildcard_host_to_loopback(monkeypatch) -> None:
    monkeypatch.delenv("CODESPACE_NAME", raising=False)
    monkeypatch.delenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", raising=False)

    assert _browser_url("0.0.0.0", 8765) == "http://127.0.0.1:8765"


def test_browser_url_brackets_ipv6(monkeypatch) -> None:
    monkeypatch.delenv("CODESPACE_NAME", raising=False)
    monkeypatch.delenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", raising=False)

    assert _browser_url("::1", 8765) == "http://[::1]:8765"


# ----- dict-eval CLI -----


def _make_transcript(words: list[tuple[str, float, float]]) -> dict:
    return {
        "schema_version": 1,
        "segments": [
            {
                "id": "s0",
                "start": words[0][1],
                "end": words[-1][2],
                "text": "".join(w[0] for w in words),
                "words": [
                    {"id": f"s0-w{i}", "start": s, "end": e, "text": t}
                    for i, (t, s, e) in enumerate(words)
                ],
            }
        ],
    }


def test_dict_eval_reports_replacements(tmp_path: Path) -> None:
    tx = _make_transcript([("黒", 0.0, 0.3), ("だ", 0.3, 0.6)])
    transcript_path = tmp_path / "ep.transcript.json"
    transcript_path.write_text(json.dumps(tx, ensure_ascii=False), encoding="utf-8")

    dict_path = tmp_path / "dictionary.json"
    dict_path.write_text(
        json.dumps({"version": 1, "entries": [{"from": "黒だ", "to": "クロード"}]}),
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["dict-eval", str(transcript_path), "--dictionary", str(dict_path)])
    assert result.exit_code == 0, result.output
    assert "Replacements    : 1" in result.output
    assert "Words collapsed : 1" in result.output
    assert "クロード" in result.output


def test_dict_eval_no_match_reports_zero(tmp_path: Path) -> None:
    tx = _make_transcript([("こんにちは", 0.0, 0.5)])
    transcript_path = tmp_path / "ep.transcript.json"
    transcript_path.write_text(json.dumps(tx, ensure_ascii=False), encoding="utf-8")

    dict_path = tmp_path / "dictionary.json"
    dict_path.write_text(
        json.dumps({"version": 1, "entries": [{"from": "存在しない", "to": "X"}]}),
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["dict-eval", str(transcript_path), "--dictionary", str(dict_path)])
    assert result.exit_code == 0
    assert "Replacements    : 0" in result.output
    assert "No matches" in result.output


def test_dict_eval_does_not_mutate_transcript(tmp_path: Path) -> None:
    """dict-eval must be read-only: the transcript on disk is byte-identical after run."""
    tx = _make_transcript([("黒", 0.0, 0.3), ("だ", 0.3, 0.6)])
    transcript_path = tmp_path / "ep.transcript.json"
    transcript_path.write_text(json.dumps(tx, ensure_ascii=False), encoding="utf-8")
    before = transcript_path.read_bytes()

    dict_path = tmp_path / "dictionary.json"
    dict_path.write_text(
        json.dumps({"version": 1, "entries": [{"from": "黒だ", "to": "クロード"}]}),
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["dict-eval", str(transcript_path), "--dictionary", str(dict_path)])
    assert result.exit_code == 0
    after = transcript_path.read_bytes()
    assert before == after, "dict-eval must not mutate the transcript file"


def test_dict_eval_show_negative_rejected(tmp_path: Path) -> None:
    """--show -1 would otherwise slice ops[:-1]; click.IntRange should reject it."""
    tx = _make_transcript([("x", 0.0, 0.1)])
    transcript_path = tmp_path / "ep.transcript.json"
    transcript_path.write_text(json.dumps(tx), encoding="utf-8")

    dict_path = tmp_path / "dictionary.json"
    dict_path.write_text(
        json.dumps({"version": 1, "entries": [{"from": "x", "to": "X"}]}),
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["dict-eval", str(transcript_path), "--dictionary", str(dict_path), "--show", "-1"],
    )
    assert result.exit_code != 0


def test_dict_eval_empty_dictionary_errors(tmp_path: Path) -> None:
    tx = _make_transcript([("x", 0.0, 0.1)])
    transcript_path = tmp_path / "ep.transcript.json"
    transcript_path.write_text(json.dumps(tx), encoding="utf-8")

    dict_path = tmp_path / "dictionary.json"
    dict_path.write_text(json.dumps({"version": 1, "entries": []}), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(cli, ["dict-eval", str(transcript_path), "--dictionary", str(dict_path)])
    assert result.exit_code != 0
