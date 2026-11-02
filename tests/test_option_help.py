"""Every option must say what it reads or writes, and nine flags must say what belongs together.

`compare --help` printed nine options in one flat heap: two input directories, two
thresholds, two output switches, and one `--path` with **no description at all** -
and that silence was identical on all seven commands. `--path` is the only way to
point the tool at a different receipt, and its meaning differs per command (the
stats cache for five of them, the projects directory for `loop` and `nested`), so
a reader could not know which file it meant without reading the source.

That is the help-text form of the defect the receipt code was just fixed for: a
field printed without saying what it measures. These tests drive help through the
real entry point and read the text a user sees, so a new command or a new flag is
covered without editing the tests.
"""

import re

import pytest

import quotamirror

# The subcommand names the CLI itself prints, so the registry cannot drift.
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
    """Split an `options:`-style section into {flag: description}.

    An entry starts at a two-space-indented flag. Any following line that is
    indented further and does not start a new flag belongs to that flag's
    description - that is how argparse wraps a long help string. A flag line
    with only its metavar (`--path PATH`) yields an empty description, which is
    exactly the silence these tests exist to catch.
    """
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


def test_every_option_carries_a_description(capsys):
    silent = []
    for name in _subcommands(capsys):
        options = _options(_help(capsys, name))
        assert "--help" in options, f"{name}: no options section found"
        for flag, description in options.items():
            if flag == "-h":
                continue
            if not description:
                silent.append(f"{name} {flag}")
    assert silent == [], "options with no description:\n" + "\n".join(silent)


@pytest.mark.parametrize(
    "command, expected",
    [
        ("scan", "stats-cache.json"),
        ("units", "stats-cache.json"),
        ("drift", "stats-cache.json"),
        ("revise", "stats-cache.json"),
        ("compare", "stats-cache.json"),
        ("loop", "projects"),
        ("nested", "projects"),
    ],
)
def test_path_help_names_the_default_it_actually_uses(capsys, command, expected):
    # A generic sentence would satisfy the test above while still leaving the
    # reader to guess which file `--path` opens; this asserts the help names the
    # default for *this* command, and the defaults genuinely differ.
    description = _options(_help(capsys, command))["--path"]
    assert expected in description, f"{command} --path does not name {expected}: {description!r}"


def test_compare_groups_its_options_by_what_they_do(capsys):
    text = _help(capsys, "compare")
    headings = [m.group(1) for m in map(HEADING.match, text.splitlines()) if m]
    assert len(headings) >= 3, f"compare --help is not grouped: {headings}"
    for title in ("input", "day cross-check", "output"):
        assert title in headings, f"missing group {title!r} in {headings}"


def test_grouping_does_not_drop_an_option(capsys):
    # The groups are a presentation change; the flags themselves must survive.
    text = _help(capsys, "compare")
    options = _options(text)
    for flag in (
        "--codex",
        "--path",
        "--json",
        "--max-sessions",
        "--claude-transcripts",
        "--min-tokens",
        "--tolerance",
        "--poll-share",
    ):
        assert flag in options, f"{flag} disappeared from compare --help\n{text}"
    assert len([flag for flag in options if flag not in ("-h", "--help")]) == 8, sorted(options)


def test_no_group_heading_stands_over_nothing(capsys):
    # A title with no entries under it is the same lie as an empty total.
    lines = _help(capsys, "compare").splitlines()
    empty = []
    for index, line in enumerate(lines):
        if not HEADING.match(line):
            continue
        later = [entry for entry in lines[index + 1:] if entry.strip()]
        if not later or HEADING.match(later[0]) or not later[0].startswith("  "):
            empty.append(line.strip())
    assert empty == [], f"group headings with no entries: {empty}"
