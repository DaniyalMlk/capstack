"""Checks on the distribution metadata rather than on the engine.

A model nobody can install is not useful, and the failures that stop an install
are not the kind the rest of this suite catches: a version written in two files
that drift apart, a licence claimed in metadata and absent from the artefact, a
typing marker that exists in the source and never reaches the wheel. Each is
invisible until somebody tries to depend on the package, which is the worst
moment to find out.

This project has one wrinkle the siblings do not. `capstack` on the package
index is an unrelated library, so the distribution name is `capstack-lbo` while
the import name stays `capstack`. That split is exactly the kind of thing that
is right in one file and forgotten in another, so it is asserted in both
directions here: the metadata is looked up under the distribution name, and the
entry points are checked to include the distribution's own.
"""

from __future__ import annotations

import re
import sys
from importlib.metadata import entry_points, metadata, version
from pathlib import Path

import pytest

import capstack

ROOT = Path(__file__).resolve().parent.parent

#: What the index serves it as, which is not what you import.
DISTRIBUTION = "capstack-lbo"


def test_version_is_exported() -> None:
    """The package says what it is without an installed-metadata lookup."""
    assert re.fullmatch(r"\d+\.\d+\.\d+", capstack.__version__)


def test_exported_version_matches_the_installed_distribution() -> None:
    """The attribute and the installed metadata are the same string.

    These live in two different files. Bumping one and forgetting the other is
    the ordinary mistake, and it produces a release whose reported version is a
    lie.
    """
    assert capstack.__version__ == version(DISTRIBUTION)


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib is 3.11+")
def test_exported_version_matches_the_source_of_truth() -> None:
    """The attribute agrees with pyproject.toml in the checkout.

    The check above compares against what is *installed*, which in an editable
    install is a snapshot taken at install time and can be stale — so it would
    pass on a tree whose pyproject.toml has already moved on. Reading the file
    is what makes the check bite without a reinstall.
    """
    import tomllib

    pyproject = ROOT / "pyproject.toml"
    if not pyproject.exists():  # installed without the source tree alongside
        pytest.skip("no checkout to compare against")

    declared = tomllib.loads(pyproject.read_text())["project"]["version"]
    assert capstack.__version__ == declared


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib is 3.11+")
def test_the_distribution_name_is_not_the_import_name() -> None:
    """The split is deliberate, and stated where both halves can be seen.

    If somebody ever renames the distribution back to `capstack` the upload
    fails against a project they do not own, which is a confusing error to
    debug from the build log. Failing here says why.
    """
    import tomllib

    pyproject = ROOT / "pyproject.toml"
    if not pyproject.exists():
        pytest.skip("no checkout to compare against")

    declared = tomllib.loads(pyproject.read_text())["project"]["name"]
    assert declared == DISTRIBUTION
    assert capstack.__name__ == "capstack"


def test_licence_text_is_present() -> None:
    """The licence the metadata claims exists as a file, and says MIT."""
    licence = ROOT / "LICENSE"
    if not licence.exists():
        pytest.skip("no checkout to compare against")
    assert "MIT License" in licence.read_text()


def test_licence_reaches_the_built_distribution() -> None:
    """The installed distribution carries the licence, not just the repository.

    `license-files` in pyproject.toml is what does this, and the legacy
    `license = {text = "MIT"}` table it replaced had no equivalent — so the
    metadata went on claiming MIT while the artefact shipped no terms at all.
    That is the state every wheel this project could have built before now.
    """
    fields = metadata(DISTRIBUTION)
    assert fields["License-Expression"] == "MIT"
    assert "LICENSE" in fields.get_all("License-File", [])


def test_the_typing_marker_ships() -> None:
    """`Typing :: Typed` is only true if py.typed is inside the package.

    The failure is quiet: in a source checkout a type checker reads the
    annotations off the files and everything looks fine, while a consumer who
    installed the package gets none of them.
    """
    assert (Path(capstack.__file__).parent / "py.typed").is_file()
    assert "Typing :: Typed" in metadata(DISTRIBUTION).get_all("Classifier", [])


def test_both_console_scripts_are_installed() -> None:
    """`capstack` and `capstack-lbo` both resolve to the same entry point.

    The second exists because `uvx capstack-lbo` is what somebody who read the
    install line will type, and a distribution whose only script is named
    something else leaves that failing with nothing to go on.
    """
    scripts = {
        point.name: point.value
        for point in entry_points(group="console_scripts")
        if point.value.startswith("capstack.cli")
    }
    assert scripts.get("capstack") == "capstack.cli:main"
    assert scripts.get("capstack-lbo") == "capstack.cli:main"


def test_the_entry_point_reports_the_version() -> None:
    """`capstack --version` prints it, and exits zero doing so.

    argparse's version action raises SystemExit, so the assertion is on the
    exit code as much as on the text.
    """
    from capstack.cli import main

    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
