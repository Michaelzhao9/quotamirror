"""Every command must be able to explain itself.

`compare --help` and `nested --help` crashed with
`ValueError: unsupported format character ')' (0x29) at index 102` because the
tolerance default was written as `2%` inside an argparse help string, and
argparse formats help with `%` substitution. The command a user runs *first*
was the one that raised. These tests drive help through the real entry point,
so the check is the CLI's own, not a copy of its parser.
"""

import re

import pytest

import quotamirror


def _subcommands(capsys):
    """The registry the CLI itself prints, so a new command is covered for free."""
    with pytest.raises(SystemExit):
        quotamirror.main(["--help"])
    out = capsys.readouterr().out
    match = re.search(r"\{([a-z][a-z,]*,?)\}", out)
    assert match, f"top-level help did not list the commands:\n{out}"
    return match.group(1).split(",")


def test_every_command_can_format_its_own_help(capsys):
    names = _subcommands(capsys)
    assert len(names) >= 7, names
    broken = []
    for name in names:
        try:
            with pytest.raises(SystemExit) as exc:
                quotamirror.main([name, "--help"])
        except Exception as err:  # noqa: BLE001 - the point is to name the failure
            broken.append(f"{name}: {type(err).__name__}: {err}")
            continue
        if exc.value.code != 0:
            broken.append(f"{name}: help exited {exc.value.code}")
    assert broken == [], "these commands cannot explain themselves:\n" + "\n".join(broken)


def test_help_shows_the_tolerance_default_as_a_percent(capsys):
    with pytest.raises(SystemExit):
        quotamirror.main(["compare", "--help"])
    out = capsys.readouterr().out
    # The value must survive as one percent sign: not doubled by the escape,
    # and not lost to it. Asserted over the whole help text because argparse
    # wraps this help onto the lines after the flag.
    assert "--tolerance" in out, out
    assert "(default 0.02 = 2%)" in out, out
    assert "%%" not in out, out
