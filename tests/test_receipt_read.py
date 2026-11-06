"""A receipt that cannot be parsed must be one sentence, not a decoder error.

Running the tool on a zero-byte file printed

    quotamirror: Expecting value: line 1 column 1 (char 0)

with no file name, no byte count, and no distinction between an empty file and a
receipt caught mid-write - the exact state this tool exists to talk about. A
non-receipt JSON object already got a sentence ("does not look like a usage
cache"), so the three unreadable shapes below were the remaining hole, and they
sat in the shared loader behind every receipt command.

Every assertion is on the text a user sees through the real entry point.
"""

import json

import quotamirror


def _path(tmp_path, name="stats-cache.json"):
    return tmp_path / name


def test_empty_receipt_is_one_sentence_with_the_file_and_the_byte_count(tmp_path, capsys):
    path = _path(tmp_path)
    path.write_bytes(b"")

    code = quotamirror.main(["scan", "--path", str(path)])

    err = capsys.readouterr().err
    assert code == 2, err
    assert f"{path} is empty (0 bytes" in err
    assert "Expecting value" not in err
    assert "0 model" not in err


def test_truncated_receipt_says_not_valid_json_and_where_it_stopped(tmp_path, capsys):
    path = _path(tmp_path)
    raw = b'{"version": 5, "modelUsage": {'
    path.write_bytes(raw)

    code = quotamirror.main(["units", "--path", str(path)])

    err = capsys.readouterr().err
    assert code == 2, err
    assert f"{path} is not valid JSON" in err
    assert "line 1" in err
    assert f"{len(raw)} bytes read)" in err
    assert "mid-write" in err


def test_non_utf8_receipt_names_the_byte_instead_of_the_codec(tmp_path, capsys):
    path = _path(tmp_path)
    path.write_bytes(b'\xff\xfe{"version": 5}')

    code = quotamirror.main(["drift", "--path", str(path), "--no-record"])

    err = capsys.readouterr().err
    assert code == 2, err
    assert f"{path} is not UTF-8 text" in err
    assert "0xff" in err or "xff" in err
    assert "codec can't decode" not in err


def test_whitespace_only_receipt_is_empty_not_zero_models(tmp_path, capsys):
    path = _path(tmp_path)
    raw = b"  \n\t\n"
    path.write_bytes(raw)

    code = quotamirror.main(["revise", "--path", str(path), "--no-record"])

    err = capsys.readouterr().err
    assert code == 2, err
    assert f"{path} is empty ({len(raw)} bytes" in err
    assert "0 model" not in err


def test_bare_invocation_on_an_empty_receipt_still_prints_one_sentence(tmp_path, monkeypatch, capsys):
    path = _path(tmp_path)
    path.write_bytes(b"")
    monkeypatch.setattr(quotamirror, "DEFAULT_CACHE", path)

    code = quotamirror.main([])

    out = capsys.readouterr().out
    assert code == 2, out
    assert "found   : nothing - is empty" in out
    assert "Expecting value" not in out


def test_a_valid_receipt_is_still_read(tmp_path, capsys):
    path = _path(tmp_path)
    path.write_text(
        json.dumps({"version": 5, "modelUsage": {"m": {"inputTokens": 7}}}),
        encoding="utf-8",
    )

    code = quotamirror.main(["scan", "--path", str(path)])

    out = capsys.readouterr().out
    assert code == 0, out
    assert "TOKENS" in out
