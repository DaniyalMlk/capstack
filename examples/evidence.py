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
from capstack.daycount import DayCount
from capstack.debt import CapitalStructure, DebtSchedule, Tranche, TrancheKind
from capstack.drivers import Driver
from capstack.operating import OperatingAssumptions, OperatingModel
from capstack.periods import Frequency, PeriodGrid
from capstack.sensitivity import Case
from capstack.tax import InterestLimit, TaxPolicy, levered_schedule

EXAMPLES = Path(__file__).resolve().parent

#: The stream whose rate of return is genuinely two numbers.
AMBIGUOUS = (money("-1000"), money("2500"), money("-1560"))

#: Exact roots of 1000x^2 - 2500x + 1560 = 0, in x = 1 + r.
AMBIGUOUS_EXACT = (Decimal("0.20"), Decimal("0.30"))

#: The leverage multiples the saturation check runs at, all of them past the
#: point where the cap binds in every period of the reference case.
SATURATED_TURNS = ("6.0", "6.5", "7.0", "8.0")

#: The blended coupon those multiples are priced at.
SATURATION_COUPON = "0.08"

#: The cap the saturation check is run under.
SATURATION_RATE = "0.30"


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

    # -- the deduction against the debt it is charged on -----------------
    solve_deals: int
    solve_worst_passes: int
    solve_worst_residual: Decimal
    solve_interest_gap: Decimal
    saturated_cases: int
    saturated_deduction: Decimal
    saturated_cash_tax: Decimal
    saturated_deduction_spread: Decimal
    saturated_debt_spread: Decimal
    return_deals: int
    return_gain_low: float
    return_gain_high: float
    return_uplift_low: Decimal
    return_uplift_high: Decimal


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


def _reference_case() -> tuple[OperatingAssumptions, OperatingModel]:
    """The case the saturation check is run against.

    Built here rather than read from a deal file because the claim is about a
    range of leverage multiples against one business, and no shipped deal is
    struck at four different capital structures.
    """
    years = 5
    assumptions = OperatingAssumptions.of(
        revenue_growth=Driver.constant("0.08", years),
        ebitda_margin=Driver.constant("0.20", years),
        da_rate=Driver.constant("0.04", years),
        capex_rate=Driver.constant("0.05", years),
        nwc_rate=Driver.constant("0.15", years),
        tax_rate="0.25",
    )
    grid = PeriodGrid.build(date(2026, 6, 30), years=years, frequency=Frequency.ANNUAL)
    return assumptions, OperatingModel.project(grid, assumptions, opening_revenue=1000)


def _saturation() -> tuple[int, Decimal, Decimal, Decimal, Decimal]:
    """Past the point the cap binds throughout, leverage buys no deduction.

    Once the cap bites in every period and the pool is never absorbed, the
    deduction *is* the allowance, and the allowance is a function of earnings
    alone. So it stops depending on the debt: four structures a full two turns
    apart deduct the same amount and pay the same cash tax, to the last place.

    The spread across the four is returned as well as the common value, because
    "they agree" is only worth reading if the thing they agree on would
    otherwise have differed — and the interest and the closing debt, which do
    differ, are what shows the leverage was really there.
    """
    assumptions, model = _reference_case()
    limit = InterestLimit.of(SATURATION_RATE)
    policy = TaxPolicy.from_assumptions(assumptions, interest_limit=limit)

    deductions: list[Decimal] = []
    taxes: list[Decimal] = []
    debts: list[Decimal] = []
    for turns in SATURATED_TURNS:
        structure = CapitalStructure.of(
            [
                Tranche.of(
                    "Term Loan B",
                    TrancheKind.TERM_LOAN,
                    model.entry_ebitda * money(turns),
                    cash_rate=SATURATION_COUPON,
                    swept=True,
                    floating=False,
                )
            ],
            day_count=DayCount.ACT_365F,
        )
        solution = levered_schedule(structure, model, policy=policy)
        if len(solution.tax.binding_periods) != len(model):
            raise SystemExit(
                f"{turns}x does not saturate the cap: it bound in "
                f"{len(solution.tax.binding_periods)} of {len(model)} periods"
            )
        deductions.append(Decimal(solution.tax.total_interest_deducted))
        taxes.append(Decimal(solution.tax.total_cash_tax))
        debts.append(Decimal(solution.schedule[-1].closing_debt))

    # The allowance, computed from the earnings rather than read back off the
    # answer, so the common value is checked against its own definition.
    allowances = sum((p.ebitda * money(SATURATION_RATE) for p in model), money(0))
    if deductions[0] != Decimal(allowances):
        raise SystemExit(
            f"a saturated structure deducted {deductions[0]}, not the summed "
            f"allowance {Decimal(allowances)}"
        )
    if max(taxes) != min(taxes):
        raise SystemExit(f"saturated structures paid different tax: {taxes}")

    return (
        len(SATURATED_TURNS),
        deductions[0],
        taxes[0],
        max(deductions) - min(deductions),
        max(debts) - min(debts),
    )


def _settled() -> tuple[int, int, Decimal, Decimal]:
    """The deduction settled against the debt, on every shipped deal.

    Two claims. The solve converges, and the pass count and the residual it
    stopped on are reported rather than promised. And what it returns is a
    *pass* rather than an average of passes: re-running the schedule on the
    cash flows it returns reproduces the interest it reported, exactly, which
    is the strongest statement available about a fixed point.
    """
    files = sorted(EXAMPLES.glob("*.json"))
    worst_passes = 0
    worst_residual = Decimal(0)
    worst_interest_gap = Decimal(0)

    for path in files:
        deal = load_deal(path)
        model = deal.project()
        solution = deal.levered(model)
        worst_passes = max(worst_passes, solution.iterations)
        worst_residual = max(worst_residual, Decimal(solution.residual))

        assert deal.structure is not None
        again = DebtSchedule.run(
            deal.structure,
            [p.period for p in model],
            solution.cash_flows,
            opening_cash=(
                deal.opening_cash if deal.opening_cash is not None else deal.cash_at_close
            ),
            ebitda=[model.trailing_ebitda(i) for i in range(len(model))],
            opening_ebitda=deal.transaction.valuation.ltm_ebitda,
            recapitalisations=deal.recapitalisations,
            acquisitions=deal.acquisitions,
            refinancings=deal.refinancings,
            write_offs=deal.write_offs(),
        )
        for left, right in zip(solution.schedule, again, strict=True):
            gap = abs(Decimal(left.cash_interest - right.cash_interest))
            worst_interest_gap = max(worst_interest_gap, gap)

    return len(files), worst_passes, worst_residual, worst_interest_gap


def _return_impact() -> tuple[int, float, float, Decimal, Decimal]:
    """What the deduction is worth, in the unit the question is asked in.

    Two figures per deal. The gain in basis points of return is the headline.
    The uplift is the finding behind it: the equity gains *more* than the cash
    tax the structure saved, because the saved cash is swept against the debt
    and debt repaid early does not accrue the interest it would have accrued.
    So the equity collects the saving and the interest the saving avoided.
    """
    files = sorted(EXAMPLES.glob("*.json"))
    gains: list[float] = []
    uplifts: list[Decimal] = []

    for path in files:
        deal = load_deal(path)
        plain = Case.run(deal)
        settled = Case.run(load_deal(path), levered=True)
        before, after = plain.outcome.irr, settled.outcome.irr
        if before is None or after is None:  # pragma: no cover - every deal exits
            raise SystemExit(f"{path.name} reports no rate of return")
        gains.append((after - before) * 10_000)

        assert settled.levered is not None
        saved = settled.levered.total_shield
        gained = Decimal(
            settled.outcome.valuation.equity_value - plain.outcome.valuation.equity_value
        )
        if saved <= 0:  # pragma: no cover - every shipped deal is levered
            raise SystemExit(f"{path.name} saved no tax, so there is nothing to measure")
        uplifts.append(gained / Decimal(saved))

    return len(files), min(gains), max(gains), min(uplifts), max(uplifts)


def measure() -> Figures:
    roots, ambiguous_worst = _ambiguous()
    found, closed, two_flow_error = _two_flow()
    deals, funding_worst, bridge_worst, cells, grid_gap = _deal_identities()
    solve_deals, passes, residual, interest_gap = _settled()
    cases, deduction, cash_tax, deduction_spread, debt_spread = _saturation()
    return_deals, gain_low, gain_high, uplift_low, uplift_high = _return_impact()
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
        solve_deals=solve_deals,
        solve_worst_passes=passes,
        solve_worst_residual=residual,
        solve_interest_gap=interest_gap,
        saturated_cases=cases,
        saturated_deduction=deduction,
        saturated_cash_tax=cash_tax,
        saturated_deduction_spread=deduction_spread,
        saturated_debt_spread=debt_spread,
        return_deals=return_deals,
        return_gain_low=gain_low,
        return_gain_high=gain_high,
        return_uplift_low=uplift_low,
        return_uplift_high=uplift_high,
    )


#: The bands the README promises. Bounds rather than digits, because where two
#: implementations of a rate stop agreeing is a fact about the floating-point
#: unit underneath, while an exact-arithmetic identity is zero or it is wrong.
BOUNDS: tuple[tuple[str, str], ...] = (
    ("the solver against the quadratic's exact roots", "1e-09"),
    ("a two-flow return against the multiple over the hold", "1e-12"),
    ("the deduction settled against the debt it is charged on", "0.0001"),
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

    # The solve's own stopping criterion, restated as a gate. A residual above
    # the tolerance the solver returned on means it did not converge.
    if figures.solve_worst_residual > Decimal("0.0001"):
        raise SystemExit(
            f"a solve stopped with the saving still moving by "
            f"{figures.solve_worst_residual}"
        )
    if figures.solve_worst_passes > 6:
        raise SystemExit(
            f"a solve took {figures.solve_worst_passes} passes; the README says at most 6"
        )
    # Equalities again, on exact arithmetic.
    for what, gap in (
        ("re-running a settled schedule changed its interest", figures.solve_interest_gap),
        ("saturated structures deducted different amounts", figures.saturated_deduction_spread),
    ):
        if gap != 0:
            raise SystemExit(f"{what}, by {gap}")
    # And the leverage really did differ, or the equality above is vacuous.
    if figures.saturated_debt_spread <= 0:
        raise SystemExit(
            "the saturated structures left the same debt, so they were not "
            "actually different structures"
        )
    if figures.saturated_cases != len(SATURATED_TURNS):
        raise SystemExit(f"only {figures.saturated_cases} saturated structures checked")
    # The deduction is worth a material amount of return or the convention is
    # not worth offering. A band, because the figure is a property of each
    # file's leverage and coupon rather than of the engine.
    if not 50.0 <= figures.return_gain_low <= figures.return_gain_high <= 300.0:
        raise SystemExit(
            f"the deduction moved returns by {figures.return_gain_low:.1f} to "
            f"{figures.return_gain_high:.1f}bp, outside the band the README states"
        )
    # And the equity gains more than the saving, which is the finding. An
    # uplift at or below one would mean the swept cash earned nothing.
    if figures.return_uplift_low <= 1:
        raise SystemExit(
            f"the equity gained only {figures.return_uplift_low} times the saving, "
            "so the interest the repayment avoided has gone missing"
        )


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
        (
            "The interest deduction settled against the debt it is charged on, "
            f"{figures.solve_deals} shipped deals",
            f"at most {figures.solve_worst_passes} passes, worst residual "
            f"{figures.solve_worst_residual:.1e}, and re-running each schedule on "
            f"the settled cash flows moves its interest by "
            f"{_exact(figures.solve_interest_gap)}",
        ),
        (
            f"The deduction past saturation, {figures.saturated_cases} structures from "
            f"{SATURATED_TURNS[0]}x to {SATURATED_TURNS[-1]}x",
            f"all deduct {figures.saturated_deduction:.2f} and pay "
            f"{figures.saturated_cash_tax:.2f} - spread "
            f"{_exact(figures.saturated_deduction_spread)}, while the debt they leave "
            f"spans {figures.saturated_debt_spread:.2f}",
        ),
        (
            f"What the deduction is worth, {figures.return_deals} shipped deals",
            f"{figures.return_gain_low:.0f} to {figures.return_gain_high:.0f} basis "
            f"points of return, and the equity gains "
            f"{figures.return_uplift_low:.2f} to {figures.return_uplift_high:.2f} times "
            f"the cash tax saved rather than once",
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
        f"   settled on {measured.solve_deals} deals: at most "
        f"{measured.solve_worst_passes} passes, worst residual "
        f"{measured.solve_worst_residual}, interest gap on re-run "
        f"{_exact(measured.solve_interest_gap)}"
    )
    lines.append(
        f"   saturated: {measured.saturated_cases} structures deduct "
        f"{measured.saturated_deduction}, pay {measured.saturated_cash_tax}, "
        f"spread {_exact(measured.saturated_deduction_spread)}, debt spread "
        f"{measured.saturated_debt_spread}"
    )
    lines.append(
        f"   return impact on {measured.return_deals} deals: "
        f"{measured.return_gain_low:.2f} to {measured.return_gain_high:.2f}bp, "
        f"equity uplift {measured.return_uplift_low} to {measured.return_uplift_high}"
    )
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
