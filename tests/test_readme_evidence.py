"""The figures on the front page are the figures the code produces.

A table of measured results is worth exactly as much as the guarantee that it
still matches. A number copied into a document once stops being true quietly:
the engine changes, the suite stays green because nothing in it reads the
document, and the README goes on claiming a precision the solver no longer has.

So the claim is pinned here, in both directions. Every figure the README quotes
has to appear in the output of ``examples/evidence.py``, which is itself a gate
— it exits non-zero if a figure moves outside the band the README states. And
every row label has to still be in the document, which is the other way a
claim disappears: somebody deletes the row.

This test is also what makes "runs in CI" true. CI runs ``pytest``, so the gate
runs here rather than in a workflow step of its own, and a figure that moves
fails the suite on the commit that moved it.

One direction is deliberately *not* attempted. Scanning the README for a number
and checking it against the computed value cannot tell which number it found:
two figures in a table of five can be close enough that a check comparing "some
number near here" to the measurement passes against the wrong one. So the
forward direction is pinned instead — every quoted figure appears in the gate's
output — plus the row labels, which together catch a figure that moved and a
row that vanished.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
GATE = ROOT / "examples" / "evidence.py"


def _load_gate() -> object:
    """Import `examples/evidence.py`, which is a script and not a package."""
    spec = importlib.util.spec_from_file_location("capstack_evidence", GATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


#: Figures the README quotes, exactly as the gate prints them.
#:
#: Written out rather than parsed out of the README on purpose: deleting a row
#: from the table should not silently delete the check that defends it.
QUOTED = (
    "20.0% and 30.0%, within 1.5e-13 of 20% and 30%",
    "25.723629% a year, agreeing with m ** (1 / years) - 1 to 1.7e-16",
    "balanced by equality, worst gap 0, not within a tolerance",
    "worst unexplained residue 0 across 4 deals",
    "6 cells each a full rebuild, base cell out by 0",
    "at most 5 passes, worst residual 5.9e-5, and re-running each schedule on the settled "
    "cash flows moves its interest by 0",
    "all deduct 380.16 and pay 158.40 - spread 0, while the debt they leave spans 615.22",
    "90 to 189 basis points of return, and the equity gains 1.09 to 1.15 times the cash "
    "tax saved rather than once",
)

#: Row labels from the evidence table. Presence is what is checked, not the
#: value beside it — see the module docstring for why that direction is the
#: wrong one to scan for.
ROWS = (
    "The ambiguous-IRR stream against the quadratic's exact roots",
    "A two-flow return against the money multiple over the hold",
    "Sources and uses on all 4 shipped deals",
    "The value-creation bridge against the change in equity value",
    "A sensitivity grid's base cell against the deal run on its own",
    "The interest deduction settled against the debt it is charged on, 4 shipped deals",
    "The deduction past saturation, 4 structures from 6.0x to 8.0x",
    "What the deduction is worth, 4 shipped deals",
)


@pytest.fixture(scope="module")
def gate_output() -> str:
    """Run the gate once and share its output. It rebuilds four deals twice."""
    module = _load_gate()
    return str(module.report())  # type: ignore[attr-defined]


@pytest.mark.parametrize("figure", QUOTED)
def test_the_readme_quotes_a_figure_the_gate_prints(figure: str, gate_output: str) -> None:
    assert figure in gate_output, "the gate no longer produces this figure"
    assert figure in README, "the README no longer quotes this figure"


@pytest.mark.parametrize("row", ROWS)
def test_the_evidence_table_still_has_the_row(row: str) -> None:
    assert row in README


def test_the_gate_renders_the_table_the_readme_carries(gate_output: str) -> None:
    """Every row the gate renders is in the README, verbatim and whole.

    This is what catches a row added to the gate and never put on the front
    page, and a row whose label and value drifted apart — the two halves are
    checked together rather than separately, so a figure moved into the wrong
    row fails.
    """
    rendered = [
        line
        for line in gate_output.splitlines()
        if line.startswith("| ") and not line.startswith("| ---") and line != "| Check | Result |"
    ]
    assert len(rendered) == len(QUOTED)
    for line in rendered:
        assert line in README


def test_the_bands_are_the_ones_the_prose_claims(gate_output: str) -> None:
    """The prose says the rate rows are bounds, so the bounds are stated.

    A reader is told the figures are quoted as bounds rather than digits
    because where two implementations stop agreeing is a fact about the
    hardware. That is only honest if a bound exists, so the gate prints them
    and this reads them back.
    """
    assert "bound: the solver against the quadratic's exact roots to 1e-09" in gate_output
    assert "bound: a two-flow return against the multiple over the hold to 1e-12" in gate_output


def test_the_identities_are_zero_rather_than_small(gate_output: str) -> None:
    """The three exact-arithmetic rows are zero, not nearly zero.

    The README makes a point of this: a funding table out by a few thousand on
    a billion has a missing line item, so the check is equality. Reading the
    spelled-out figures back is what stops the prose from outliving it.
    """
    for line in (
        "funding worst gap: 0",
        "bridge worst residue: 0",
        "base cell gap 0",
        # The settled schedule reproduces its own interest, and the saturated
        # structures deduct the same amount. Both exact, both zero.
        "interest gap on re-run 0",
        "spread 0,",
    ):
        assert line in gate_output


def test_the_suite_size_the_readme_claims_is_about_right() -> None:
    """A loose bound, on purpose.

    An exact count fails on every commit that adds a test, which trains people
    to update the number without reading it. A quarter either way catches a
    figure that has gone properly stale and tolerates ordinary growth.

    Counted by asking pytest rather than by grepping for ``def test_``: most of
    this suite lives in classes and a good deal of it is parametrised, so a
    function count is off by a factor that moves on its own.
    """
    import re
    import subprocess

    match = re.search(r"Behind them: ([\d,]+) tests", README)
    assert match is not None, "the README no longer says how large the suite is"
    claimed = int(match.group(1).replace(",", ""))

    finished = subprocess.run(
        [sys.executable, "-m", "pytest", "-o", "addopts=", "-q", "--collect-only", "tests"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    collected = re.search(r"(\d+) tests? collected", finished.stdout)
    if collected is None:  # pragma: no cover - no checkout to collect from
        pytest.skip(f"could not collect: {finished.stdout[-400:]}")

    actual = int(collected.group(1))
    assert claimed >= actual * 0.75, f"the README claims {claimed}, the suite is {actual}"
    assert claimed <= actual * 1.25, f"the README claims {claimed}, the suite is {actual}"
