#!/usr/bin/env python3
"""The figures on the front page, computed rather than remembered.

An LBO model is easy to describe and hard to believe. Every one of them claims
to price a deal, schedule its debt, test its covenants and report a return; the
question a reader has is what any of those numbers were checked against. Prose
cannot answer that, so this script answers it with figures and the README
quotes them.

Two of the checks here are against closed forms that share no derivation with
the engine, which is worth more than a table of self-consistency:

* The ambiguous-IRR stream is a *quadratic*. A flow of -1,000 / +2,500 / -1,560
  gives ``1000x^2 - 2500x + 1560 = 0`` in ``x = 1 + r``, whose discriminant is
  10,000 — a perfect square — so the roots are exactly 1.2 and 1.3 and the
  rates are exactly 20% and 30%. The solver knows nothing about quadratics; it
  scans for sign changes and refines brackets. Agreeing with the algebra is a
  statement about the solver.
* A two-flow investment's rate of return is its money multiple over the hold,
  ``m ** (1 / years) - 1``, by definition and not by implementation. Anything
  the root finder does differently from that on two flows is wrong.

The rest are the places where a *tolerance* would hide the error the check
exists to catch. A funding table that is out by a few thousand on a billion has
not nearly balanced, it has a missing line; a bridge that nearly ties to the
change in equity value has an unexplained component; a debt schedule that
nearly reaches zero has an amortisation profile that does not amortise. All
three are asserted as equality on exact ``Decimal`` arithmetic, which is only
possible because money here is never a float.

Run it directly. It exits non-zero if any figure moves, so it is a gate and not
a printout:

    python examples/evidence.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from capstack import (
    AmbiguousIRRError,
    Axis,
    CashFlowStream,
    Dimension,
    Grid,
    Metric,
    irr_periodic,
    load_deal,
    money,
)
from capstack.sensitivity import Case

EXAMPLES = Path(__file__).resolve().parent

#: The stream whose rate of return is genuinely two numbers.
AMBIGUOUS = (money("-1000"), money("2500"), money("-1560"))

#: Exact roots of 1000x^2 - 2500x + 1560 = 0, in x = 1 + r.
AMBIGUOUS_EXACT = (Decimal("0.20"), Decimal("0.30"))


@dataclass(frozen=True)
class Figures:
    """Everything the front page quotes, measured."""

    # -- against closed forms --------------------------------------------
    ambiguous_roots: tuple[float, ...]
    ambiguous_worst_error: float
    two_flow_irr: float
    two_flow_closed_form: float
    two_flow_error: float

    # -- where a tolerance would hide the error --------------------------
    deals: int
    funding_worst_gap: Decimal
    bridge_worst_gap: Decimal
    grid_cells: int
    grid_base_gap: Decimal


def _ambiguous() -> tuple[tuple[float, ...], float]:
    """Every rate the solver finds, against the quadratic's exact roots."""
    try:
        irr_periodic(list(AMBIGUOUS))
    except AmbiguousIRRError as exc:
        roots = tuple(sorted(exc.roots))
    else:  # pragma: no cover - the stream is ambiguous by construction
        raise SystemExit("the ambiguous stream returned a single rate")

    if len(roots) != len(AMBIGUOUS_EXACT):
        raise SystemExit(f"expected {len(AMBIGUOUS_EXACT)} roots, got {roots}")
    worst = max(
        abs(found - float(exact))
        for found, exact in zip(roots, AMBIGUOUS_EXACT, strict=True)
    )
    return roots, worst


def _two_flow() -> tuple[float, float, float]:
    """A two-flow deal's rate of return against the multiple over the hold.

    Dated four years apart on the same day of the year, so that under ACT/365F
    the hold is a clean number of years and the closed form has no rounding of
    its own to explain away.
    """
    stream = CashFlowStream.of(
        [(date(2026, 1, 1), money("-400000000")), (date(2030, 1, 1), money("1000000000"))]
    )
    found = stream.xirr()
    multiple = float(stream.moic())
    years = float(stream.holding_period_years())
    closed = multiple ** (1.0 / years) - 1.0
    return found, closed, abs(found - closed)


def _deal_identities() -> tuple[int, Decimal, Decimal, int, Decimal]:
    """The identities every shipped deal has to satisfy, as equalities."""
    files = sorted(EXAMPLES.glob("*.json"))
    if not files:  # pragma: no cover - the examples are committed
        raise SystemExit("no example deals to check")

    funding_worst = Decimal(0)
    bridge_worst = Decimal(0)

    for path in files:
        deal = load_deal(path)
        case = Case.run(deal)

        # A sources and uses table balances by equality or the object does not
        # exist. Recomputing the two sides here rather than trusting the
        # constructor is what makes this evidence instead of a restatement.
        uses = deal.transaction.sources_and_uses()
        funding_worst = max(funding_worst, abs(Decimal(uses.total_sources - uses.total_uses)))

        # The bridge decomposes the change in equity value. Its components have
        # to come to that change and not to something near it: a residue is a
        # component nobody has named.
        bridge = case.outcome.attribution
        residue = Decimal(bridge.total - bridge.value_created)
        bridge_worst = max(bridge_worst, abs(residue))

    # One grid, with its base case where the base case is. Each cell is a full
    # rebuild of the engine at a moved assumption, so the cell that moves
    # nothing has to come back to the number the deal returns on its own.
    deal = load_deal(EXAMPLES / "kestrel.json")
    base = Case.run(deal)
    # The axes are built around the file's *own* values rather than from
    # hand-typed ones, so the base cell exists by construction and stays
    # correct if the example deal is ever re-struck. A grid whose axes happen
    # to miss the base case has no cell to compare, and an axis step is not
    # the check here.
    own_exit = Dimension.EXIT_MULTIPLE.read(deal)
    own_leverage = Dimension.LEVERAGE.read(deal)
    rows_axis = Axis.of(
        Dimension.EXIT_MULTIPLE,
        [own_exit - money("1"), own_exit, own_exit + money("1")],
    )
    columns_axis = Axis.of(Dimension.LEVERAGE, [own_leverage, own_leverage + money("0.5")])
    grid = Grid.run(deal, rows=rows_axis, columns=columns_axis, metric=Metric.EQUITY_VALUE)
    cells = tuple(cell for line in grid.cells for cell in line)

    # The base cell is the one whose coordinates are the file's own assumptions
    # on both axes. Found by asking each axis rather than by index, since an
    # axis that does not contain its base case has no such cell.
    base_cells = [
        cell
        for cell in cells
        if rows_axis.is_base(cell.row, deal) and columns_axis.is_base(cell.column, deal)
    ]
    if len(base_cells) != 1:
        raise SystemExit(f"expected one base cell, found {len(base_cells)}")
    found = base_cells[0].value
    if found is None:
        raise SystemExit("the base cell did not evaluate")
    grid_gap = abs(Decimal(found) - Decimal(base.outcome.valuation.equity_value))

    return len(files), funding_worst, bridge_worst, len(cells), grid_gap


def measure() -> Figures:
    roots, ambiguous_worst = _ambiguous()
    found, closed, two_flow_error = _two_flow()
    deals, funding_worst, bridge_worst, cells, grid_gap = _deal_identities()
    return Figures(
        ambiguous_roots=roots,
        ambiguous_worst_error=ambiguous_worst,
        two_flow_irr=found,
        two_flow_closed_form=closed,
        two_flow_error=two_flow_error,
        deals=deals,
        funding_worst_gap=funding_worst,
        bridge_worst_gap=bridge_worst,
        grid_cells=cells,
        grid_base_gap=grid_gap,
    )


#: The bands the README promises. Bounds rather than digits, because where two
#: implementations of a rate stop agreeing is a fact about the floating-point
#: unit underneath, while an exact-arithmetic identity is zero or it is wrong.
BOUNDS: tuple[tuple[str, str], ...] = (
    ("the solver against the quadratic's exact roots", "1e-09"),
    ("a two-flow return against the multiple over the hold", "1e-12"),
)


def check(figures: Figures) -> None:
    """Fail on anything the README would then be wrong about."""
    if figures.ambiguous_worst_error > 1e-9:
        raise SystemExit(
            f"the solver's roots {figures.ambiguous_roots} are "
            f"{figures.ambiguous_worst_error:.3e} from the quadratic's"
        )
    if figures.two_flow_error > 1e-12:
        raise SystemExit(
            f"a two-flow return came back {figures.two_flow_error:.3e} from the closed form"
        )
    # Equalities, on exact arithmetic. Not "small": zero.
    for what, gap in (
        ("a funding table", figures.funding_worst_gap),
        ("a value-creation bridge", figures.bridge_worst_gap),
        ("a grid's base cell against the base case", figures.grid_base_gap),
    ):
        if gap != 0:
            raise SystemExit(f"{what} is out by {gap}")
    if figures.deals < 4:
        raise SystemExit(f"only {figures.deals} deals checked; the repository ships more")
    if figures.grid_cells != 6:
        raise SystemExit(f"the grid has {figures.grid_cells} cells, not 6")


def _exact(amount: Decimal) -> str:
    """A gap, rendered.

    An exact-arithmetic zero carries whatever exponent the subtraction left it
    with, so the identities above print as `0E-31`. That reads like a very
    small number rather than like nothing, which is the opposite of the point.
    """
    return "0" if amount == 0 else str(amount)


def rows(figures: Figures) -> tuple[tuple[str, str], ...]:
    """The README's table, as it is rendered there."""
    low, high = figures.ambiguous_roots
    return (
        (
            "The ambiguous-IRR stream against the quadratic's exact roots",
            f"{low:.1%} and {high:.1%}, within "
            f"{figures.ambiguous_worst_error:.1e} of 20% and 30%",
        ),
        (
            "A two-flow return against the money multiple over the hold",
            f"{figures.two_flow_irr:.6%} a year, agreeing with "
            f"m ** (1 / years) - 1 to {figures.two_flow_error:.1e}",
        ),
        (
            f"Sources and uses on all {figures.deals} shipped deals",
            f"balanced by equality, worst gap {_exact(figures.funding_worst_gap)}, "
            f"not within a tolerance",
        ),
        (
            "The value-creation bridge against the change in equity value",
            f"worst unexplained residue {_exact(figures.bridge_worst_gap)} across "
            f"{figures.deals} deals",
        ),
        (
            "A sensitivity grid's base cell against the deal run on its own",
            f"{figures.grid_cells} cells each a full rebuild, base cell out by "
            f"{_exact(figures.grid_base_gap)}",
        ),
    )


def report(figures: Figures | None = None) -> str:
    """The table, plus the figures spelled out underneath it."""
    measured = measure() if figures is None else figures
    check(measured)

    lines = ["", "1. What is checked, and against what", "", "| Check | Result |", "| --- | --- |"]
    for label, result in rows(measured):
        lines.append(f"| {label} | {result} |")

    lines.extend(["", "2. The same figures, spelled out", ""])
    lines.append(f"   ambiguous roots: {measured.ambiguous_roots}")
    lines.append(f"   worst distance from the quadratic: {measured.ambiguous_worst_error:.3e}")
    lines.append(
        f"   two flows: solver {measured.two_flow_irr!r}, "
        f"closed form {measured.two_flow_closed_form!r}"
    )
    lines.append(f"   deals checked: {measured.deals}")
    lines.append(f"   funding worst gap: {_exact(measured.funding_worst_gap)}")
    lines.append(f"   bridge worst residue: {_exact(measured.bridge_worst_gap)}")
    lines.append(
        f"   grid: {measured.grid_cells} cells, base cell gap "
        f"{_exact(measured.grid_base_gap)}"
    )
    for what, bound in BOUNDS:
        lines.append(f"   bound: {what} to {bound}")
    lines.extend(["", "every figure above is inside the band the README states"])
    return "\n".join(lines)


def main() -> int:
    print(report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
