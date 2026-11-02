"""The store flags must say where they write, and `nested` must group its options.

`--path` now names the file it reads. Its twin was left silent: `--store` is the
*only* way to find the file `drift` and `revise` write, and on this machine the
store usually does not exist yet - so a reader who loses the path has no way back
to it except the source. A write location that is not printed is the same defect
as a total with no labels.

`nested` had six flags in one heap: one directory, one optional cache, two
thresholds, two output switches. It borrows `compare`'s grouping so the two
commands a reader runs on transcripts read the same way.

These tests drive help through the real entry point, so a new command is covered
for free.
"""

import re

import pytest

import quotamirror

ENTRY = re.compile(r"^  (-{1,2}[A-Za-z][\w-]*)")
HEADING = re.compile(r"^([a-z][a-z -]*):\s*$")


def _help(capsys, name):
    with pytest.raises(SystemExit) as exc:
        quotamirror.main([name, "--help"])
    assert exc.value.code == 0
    return capsys.readouterr().out


def _subcommands(capsys):
    with pytest.raises(SystemExit):
        quotamirror.main(["--help"])
    out = capsys.readouterr().out
    match = re.search(r"\{([a-z][a-z,]*,?)\}", out)
    assert match, out
    return match.group(1).split(",")


def _options(text):
    entries = {}
    current = None
    for line in text.splitlines():
        if not line.strip():
            continue
        match = ENTRY.match(line)
        if match and not line.startswith("   "):
            flags, rest = [], line.strip()
            while True:
                head = re.match(r"(-{1,2}[A-Za-z][\w-]*)", rest)
                if not head:
                    break
                flags.append(head.group(1))
                rest = rest[head.end():].lstrip()
                if rest.startswith(","):
                    rest = rest[1:].lstrip()
                    continue
                metavar = re.match(r"[A-Z][A-Z_-]*(?=\s|$)", rest)
                if metavar:
                    rest = rest[metavar.end():].lstrip()
                break
            for flag in flags:
                entries[flag] = rest
            current = flags
            continue
        if current and line.startswith("    "):
            for flag in current:
                entries[flag] = (entries[flag] + " " + line.strip()).strip()
    return entries


@pytest.mark.parametrize(
    "command, expected",
    [
        ("drift", "shapes.json"),
        ("revise", "readings.json"),
    ],
)
def test_store_help_names_the_file_it_writes(capsys, command, expected):
    # The two stores are different files with different contents; a generic
    # "where snapshots are kept" cannot tell a user which one to look in.
    description = _options(_help(capsys, command))["--store"]
    assert expected in description, f"{command} --store does not name {expected}: {description!r}"


def test_store_help_hides_the_home_directory(capsys):
    # Help text gets pasted into issues and screenshots.
    for command in ("drift", "revise"):
        description = _options(_help(capsys, command))["--store"]
        assert str(quotamirror.Path.home()) not in description, description


def test_nested_groups_its_options_by_what_they_do(capsys):
    text = _help(capsys, "nested")
    headings = [m.group(1) for m in map(HEADING.match, text.splitlines()) if m]
    assert len(headings) >= 3, f"nested --help is not grouped: {headings}"
    for title in ("input", "day cross-check", "output"):
        assert title in headings, f"missing group {title!r} in {headings}"


def test_nested_grouping_does_not_drop_an_option(capsys):
    text = _help(capsys, "nested")
    options = _options(text)
    for flag in ("--path", "--json", "--max-files", "--cache", "--min-tokens", "--tolerance"):
        assert flag in options, f"{flag} disappeared from nested --help\n{text}"
    assert len([flag for flag in options if flag not in ("-h", "--help")]) == 6, sorted(options)


def test_no_group_heading_stands_over_nothing(capsys):
    # A title with no entries under it is the same lie as an empty total.
    empty = []
    for name in _subcommands(capsys):
        lines = _help(capsys, name).splitlines()
        for index, line in enumerate(lines):
            if not HEADING.match(line):
                continue
            later = [entry for entry in lines[index + 1:] if entry.strip()]
            if not later or HEADING.match(later[0]) or not later[0].startswith("  "):
                empty.append(f"{name}: {line.strip()}")
    assert empty == [], f"group headings with no entries: {empty}"
