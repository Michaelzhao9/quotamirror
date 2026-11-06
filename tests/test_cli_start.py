"""The command a new user types first must answer, not scold.

`quotamirror` with no arguments exited 2 with

    usage: quotamirror [-h] {scan,units,loop,drift,revise,nested,compare} ...
    quotamirror: error: the following arguments are required: command

which names seven commands and says nothing about the receipt this machine
actually has. The tool already knows the default path it would open for five of
those commands, so a bare invocation can say what it found there and which
command answers which question.

Every case below is checked against the printed text a user sees, through the
real entry point - not against an internal helper's return value.
"""

import json

import quotamirror


def _receipt(tmp_path, name="stats-cache.json"):
    return tmp_path / name


def _write(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_bare_invocation_reports_the_receipt_and_the_first_command(tmp_path, monkeypatch, capsys):
    path = _write(
        _receipt(tmp_path),
        {
            "version": 5,
            "lastComputedDate": "2026-10-25",
            "dailyActivity": [{"date": "2026-10-24"}, {"date": "2026-10-25"}, {"date": "2026-10-23"}],
            "modelUsage": {
                "claude-opus-5-5": {"inputTokens": 10, "costUSD": 0.0},
                "claude-haiku-5-5": {"inputTokens": 5, "costUSD": 0.0},
            },
        },
    )
    monkeypatch.setattr(quotamirror, "DEFAULT_CACHE", path)

    code = quotamirror.main([])

    out = capsys.readouterr().out
    assert code == 0, out
    assert str(path) in out
    assert "2 model(s)" in out
    assert "3 day(s)" in out
    assert "2026-10-25" in out
    # the point of the screen: a first command to run, not seven bare names
    assert "scan" in out and "units" in out
    assert "usage: quotamirror" not in out


def test_bare_invocation_says_nothing_was_found_instead_of_printing_zeros(tmp_path, monkeypatch, capsys):
    missing = _receipt(tmp_path, "does-not-exist.json")
    monkeypatch.setattr(quotamirror, "DEFAULT_CACHE", missing)

    code = quotamirror.main([])

    out = capsys.readouterr().out
    assert code == 2, out
    assert str(missing) in out
    assert "nothing" in out
    assert "0 model" not in out and "0 day" not in out


def test_bare_invocation_names_why_a_file_is_not_a_receipt(tmp_path, monkeypatch, capsys):
    path = _write(_receipt(tmp_path), {"hello": "world"})
    monkeypatch.setattr(quotamirror, "DEFAULT_CACHE", path)

    code = quotamirror.main([])

    out = capsys.readouterr().out
    assert code == 2, out
    assert "does not look like a usage cache" in out
    assert "0 model" not in out


def test_readable_receipt_with_no_models_states_the_absence(tmp_path, monkeypatch, capsys):
    path = _write(_receipt(tmp_path), {"version": 5, "dailyActivity": [{"date": "2026-10-25"}]})
    monkeypatch.setattr(quotamirror, "DEFAULT_CACHE", path)

    code = quotamirror.main([])

    out = capsys.readouterr().out
    assert code == 0, out
    assert "no modelUsage entries" in out
    assert "0 model(s)" not in out


def test_a_command_still_runs_when_one_is_given(tmp_path, capsys):
    path = _write(
        _receipt(tmp_path),
        {"version": 5, "modelUsage": {"m": {"inputTokens": 7}}},
    )

    code = quotamirror.main(["scan", "--path", str(path)])

    out = capsys.readouterr().out
    assert code == 0, out
    assert "TOKENS" in out
