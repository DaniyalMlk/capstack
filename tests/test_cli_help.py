"""Help text is only as good as the last time anybody rendered it.

Two holes this closes, both invisible to the rest of the suite.

The first is that ``--help`` can raise. argparse expands every help string
through ``%``-formatting so that ``%(default)s`` works, which makes a literal
per cent sign a conversion specifier: ``20% in cash`` is read as ``%i``,
handed a dict of parameters, and raises ``TypeError``. Nothing else notices,
because the commands themselves never render their help. Only ``--help`` is
broken, which is the first thing anybody types.

The second is that help text can simply be absent. An option that prints its
flag name and its choices and nothing else tells the reader what values are
accepted and not what any of them mean.

Both sweeps enumerate commands and arguments from the parser rather than from
a list written here. A hand-written list is why this class of bug survives:
the subcommand added next week is not in it.
"""

from __future__ import annotations

import argparse
import io
from collections.abc import Iterator
from contextlib import redirect_stdout

import pytest

from capstack.cli import build_parser, main


def command_paths(
    command: argparse.ArgumentParser, prefix: tuple[str, ...] = ()
) -> Iterator[tuple[str, ...]]:
    """Every command path the parser can reach, the root included.

    Recursive, so a subcommand that grows subcommands of its own is swept
    without anybody remembering to come back here.
    """
    yield prefix
    for action in command._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in sorted(action.choices.items()):
                yield from command_paths(child, (*prefix, name))


def arguments_of(command: argparse.ArgumentParser) -> Iterator[argparse.Action]:
    for action in command._actions:
        if isinstance(action, argparse._SubParsersAction):
            continue
        # --help and --version describe themselves.
        if isinstance(action, argparse._HelpAction | argparse._VersionAction):
            continue
        yield action


PATHS = tuple(command_paths(build_parser()))
IDS = tuple(" ".join(path) or "capstack" for path in PATHS)


def test_the_sweep_found_the_commands() -> None:
    """Guards the guard.

    If introspection stops finding subcommands the sweeps below would pass by
    testing nothing at all, so the count and a known name are asserted here
    rather than left implicit.
    """
    assert len(PATHS) >= 12  # the root plus eleven subcommands
    assert () in PATHS
    assert ("deal",) in PATHS
    assert ("covenants",) in PATHS


@pytest.mark.parametrize("path", PATHS, ids=IDS)
def test_help_renders(path: tuple[str, ...]) -> None:
    printed = io.StringIO()
    with redirect_stdout(printed), pytest.raises(SystemExit) as exited:
        main([*path, "--help"])
    assert exited.value.code == 0
    text = printed.getvalue()
    assert text.startswith("usage:")
    assert " ".join(("capstack", *path)) in " ".join(text.split())


@pytest.mark.parametrize("path", PATHS, ids=IDS)
def test_every_argument_has_help(path: tuple[str, ...]) -> None:
    command = build_parser()
    for name in path:
        for action in command._actions:
            if isinstance(action, argparse._SubParsersAction):
                command = action.choices[name]
                break
    missing = [
        action.dest
        for action in arguments_of(command)
        if not (action.help or "").strip()
    ]
    assert not missing, f"{' '.join(('capstack', *path))}: {missing}"


def test_a_literal_per_cent_sign_would_be_caught() -> None:
    """The sweep above is only a guard if a bad string really fails it.

    Built here rather than left to trust: the same expansion argparse performs
    at display time, on the string that breaks it.
    """
    broken = argparse.ArgumentParser(prog="broken", add_help=True)
    broken.add_argument("--cash", help="20% in cash")
    with pytest.raises(TypeError):
        broken.format_help()

    # And the escaped form renders, with the sign still in it.
    fixed = argparse.ArgumentParser(prog="fixed", add_help=True)
    fixed.add_argument("--cash", help="20%% in cash")
    assert "20% in cash" in " ".join(fixed.format_help().split())
