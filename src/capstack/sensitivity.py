"""The same deal, run again under a different assumption.

Every layer below this one reports one case. A deal team does not argue about
one case. It argues about the boundary between the cases that work and the cases
that do not, and that boundary is a surface rather than a point: the return
holds at eleven times and a five-year hold, and does not hold at eleven times
and four years, and the interesting question is where in between it stops.

*Why the whole engine runs per cell.* The cheap way to build a grid is to
differentiate the base case once and step along the gradient. It is also wrong,
and wrong asymmetrically. Raising the entry multiple raises the purchase price,
which the funding table absorbs into the sponsor cheque, which changes the
capital every multiple in the column is measured against — and, because the debt
did not move, changes opening leverage and therefore the sweep step and
therefore the debt outstanding at exit. Lowering the *exit* multiple does none of
that: it touches the last period and nothing upstream. A linearisation reports
those two as mirror images. They are not, and the deals that get done live in
exactly that asymmetry.

So a cell is a deal rebuilt from its assumptions and run end to end. It costs
more and it is the only way the table means what it says.

*Why a cell can fail without the table failing.* Flex far enough and the engine
runs out of deal: the equity is wiped out, the fixed point will not converge, the
structure raises more than the purchase needs. Those cells are worth seeing —
they are the edge of the surface, which is the thing being looked for — so a cell
holds either a number or the reason there is not one, and the rest of the grid
prints either way.

*Why every cell carries a covenant flag.* A cell that clears the return
threshold and trips a maintenance test in year three is not a cell that works.
The lenders can accelerate from that date and everything after it is
hypothetical. The metric answers the question that was asked; the flag says
whether the answer was reachable.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum

from .covenants import CovenantReport
from .debt import CircularityNotResolved, DebtSchedule
from .drivers import Driver
from .money import ONE, ZERO, Money, money, quantize, rescale, safe_div
from .operating import OperatingAssumptions, OperatingModel
from .outcome import Outcome
from .periods import PeriodGrid
from .spec import Deal

__all__ = [
    "Axis",
    "Cell",
    "Dimension",
    "Grid",
    "Metric",
    "SensitivityError",
    "Unit",
    "format_value",
]


class SensitivityError(ValueError):
    """The grid cannot be built as described."""


#: Failures that mean *this cell has no answer* rather than *the request was
#: malformed*. ``ArithmeticError`` covers both a division by zero and a decimal
#: operation that overflows, which a far enough flex will eventually produce.
CELL_FAILURES = (ValueError, ArithmeticError, CircularityNotResolved)


class Unit(Enum):
    """How a number on an axis or in a cell should be read."""

    RATE = "rate"
    MULTIPLE = "multiple"
    TURNS = "turns"
    AMOUNT = "amount"
    SHARE = "share"
    YEARS = "years"
    POINTS = "points"

    def __str__(self) -> str:
        return self.value


def format_value(unit: Unit, value: Money) -> str:
    """Render ``value`` the way its unit is spoken.

    Shared between the grid and whatever prints it, because an axis header and
    the cells beneath it disagreeing about how many decimal places a turn of
    leverage has is the kind of thing that gets noticed in a committee.
    """
    if unit is Unit.RATE or unit is Unit.SHARE:
        return f"{quantize(value * money(100), 1)}%"
    if unit is Unit.MULTIPLE or unit is Unit.TURNS:
        return f"{quantize(value, 2)}x"
    if unit is Unit.YEARS:
        return f"{quantize(value, 0)}y"
    if unit is Unit.POINTS:
        points = quantize(value * money(100), 2)
        # Trailing zeros are stripped by hand rather than by ``normalize``,
        # which turns fifty into 5E+1 and makes a committee pack look broken.
        digits = f"{points:f}".rstrip("0").rstrip(".") or "0"
        sign = "+" if points > 0 else ""
        return f"{sign}{digits}pp"
    return f"{quantize(value, 1):,}"


# -- Dimensions ----------------------------------------------------------


def _shift(driver: Driver, by: Money) -> Driver:
    """Move every period of an assumption by the same amount.

    A parallel shift rather than a scaling. "Margin 150bp lower throughout" is
    the sentence an investment committee says; "margin 8% lower than plan"
    means something different in year one than in year five and is almost never
    what was meant.
    """
    return Driver(values=tuple(v + by for v in driver.values))


def _with_entry_multiple(deal: Deal, value: Money) -> Deal:
    valuation = dataclasses.replace(deal.transaction.valuation, entry_multiple=value)
    return dataclasses.replace(
        deal, transaction=dataclasses.replace(deal.transaction, valuation=valuation)
    )


def _with_exit_multiple(deal: Deal, value: Money) -> Deal:
    return dataclasses.replace(deal, exit_multiple=value)


def _with_leverage(deal: Deal, turns: Money) -> Deal:
    """Resize the funded debt to ``turns`` of LTM EBITDA.

    Every tranche is scaled by the same factor, which holds the shape of the
    structure — the split between term loan, notes and mezzanine — while moving
    its size. Revolver commitments are left alone: a commitment is negotiated
    against the working-capital swing of the business, not against how much term
    debt sits above it, and scaling it would quietly change how much liquidity
    the case assumes at the same time as the leverage.

    The sponsor cheque absorbs the difference, because it is the plug.
    """
    if turns < 0:
        raise SensitivityError("leverage is measured in turns of EBITDA, so not below zero")
    if deal.transaction.total_debt <= 0:
        raise SensitivityError(
            "this deal funds no debt, so there is no leverage to flex; the "
            "structure would have to be described before it can be resized"
        )
    target = deal.transaction.valuation.ltm_ebitda * turns
    faces = rescale(target, [t.face for t in deal.transaction.debt])
    sized = dict(zip((t.name for t in deal.transaction.debt), faces))
    transaction = dataclasses.replace(
        deal.transaction,
        debt=tuple(
            dataclasses.replace(t, face=sized[t.name]) for t in deal.transaction.debt
        ),
    )
    structure = deal.structure
    if structure is not None:
        # The funding table and the schedule hold the same tranche twice, in two
        # shapes. Resizing one and not the other would produce a deal that funds
        # one amount of debt and services another, which balances nowhere.
        structure = dataclasses.replace(
            structure,
            tranches=tuple(
                dataclasses.replace(t, face=sized.get(t.name, t.face))
                for t in structure.tranches
            ),
        )
    return dataclasses.replace(deal, transaction=transaction, structure=structure)


def _with_revenue_growth(deal: Deal, shift: Money) -> Deal:
    operating = _operating(deal)
    return dataclasses.replace(
        deal,
        operating=dataclasses.replace(
            operating, revenue_growth=_shift(operating.revenue_growth, shift)
        ),
    )


def _with_ebitda_margin(deal: Deal, shift: Money) -> Deal:
    operating = _operating(deal)
    return dataclasses.replace(
        deal,
        operating=dataclasses.replace(
            operating, ebitda_margin=_shift(operating.ebitda_margin, shift)
        ),
    )


def _with_exit_year(deal: Deal, years: Money) -> Deal:
    """Hold the case and move the exit.

    The grid is rebuilt from close rather than sliced, so this works in both
    directions: an assumption series shorter than the new grid holds its final
    value, which is what extending a case means, and one longer than it is
    simply not reached.
    """
    if deal.grid is None:
        raise SensitivityError(
            "this deal has no projection, so there is no exit year to move"
        )
    if years != years.to_integral_value():
        raise SensitivityError(
            f"the exit year is a whole number of years, so {years} is not one"
        )
    count = int(years)
    if count <= 0:
        raise SensitivityError("a hold has to be at least one year long")
    return dataclasses.replace(
        deal, grid=PeriodGrid.build(deal.grid.start, count, deal.grid.frequency)
    )


def _with_base_rate(deal: Deal, shift: Money) -> Deal:
    structure = deal.structure
    if structure is None or structure.base_rate is None:
        raise SensitivityError(
            "this structure prices off no floating base rate, so there is "
            "nothing to shift; the tranches are fixed-rate"
        )
    return dataclasses.replace(
        deal,
        structure=dataclasses.replace(
            structure, base_rate=_shift(structure.base_rate, shift)
        ),
    )


def _operating(deal: Deal) -> OperatingAssumptions:
    if deal.operating is None:
        raise SensitivityError(
            "this deal has no operating case, so there are no drivers to flex"
        )
    return deal.operating


class Dimension(Enum):
    """An assumption the grid can move, and what moving it means.

    Two kinds live here and the distinction matters when reading an axis. A
    *level* dimension is set to the value given — an entry multiple of 11.5 is
    11.5. A *shift* dimension is added to whatever the file says, across every
    period, so its base case sits at zero and its axis reads as a departure.
    """

    ENTRY_MULTIPLE = "entry-multiple"
    EXIT_MULTIPLE = "exit-multiple"
    LEVERAGE = "leverage"
    REVENUE_GROWTH = "revenue-growth"
    EBITDA_MARGIN = "ebitda-margin"
    EXIT_YEAR = "exit-year"
    BASE_RATE = "base-rate"

    def __str__(self) -> str:
        return self.value

    @property
    def label(self) -> str:
        return {
            Dimension.ENTRY_MULTIPLE: "Entry multiple",
            Dimension.EXIT_MULTIPLE: "Exit multiple",
            Dimension.LEVERAGE: "Opening leverage",
            Dimension.REVENUE_GROWTH: "Revenue growth",
            Dimension.EBITDA_MARGIN: "EBITDA margin",
            Dimension.EXIT_YEAR: "Exit year",
            Dimension.BASE_RATE: "Base rate",
        }[self]

    @property
    def unit(self) -> Unit:
        return {
            Dimension.ENTRY_MULTIPLE: Unit.MULTIPLE,
            Dimension.EXIT_MULTIPLE: Unit.MULTIPLE,
            Dimension.LEVERAGE: Unit.TURNS,
            Dimension.REVENUE_GROWTH: Unit.POINTS,
            Dimension.EBITDA_MARGIN: Unit.POINTS,
            Dimension.EXIT_YEAR: Unit.YEARS,
            Dimension.BASE_RATE: Unit.POINTS,
        }[self]

    @property
    def is_shift(self) -> bool:
        """Whether the value is added to the file's assumption or replaces it."""
        return self.unit is Unit.POINTS

    def apply(self, deal: Deal, value: Money) -> Deal:
        """``deal`` as it would be with this dimension set to ``value``."""
        return _APPLY[self](deal, value)

    def read(self, deal: Deal) -> Money:
        """Where ``deal`` already sits on this dimension.

        A shift dimension always reads zero: the file is its own base case, and
        the axis measures distance from it.
        """
        if self.is_shift:
            return ZERO
        if self is Dimension.ENTRY_MULTIPLE:
            return deal.transaction.valuation.entry_multiple
        if self is Dimension.EXIT_MULTIPLE:
            return (
                deal.exit_multiple
                if deal.exit_multiple is not None
                else deal.transaction.valuation.entry_multiple
            )
        if self is Dimension.LEVERAGE:
            return safe_div(
                deal.transaction.total_debt,
                deal.transaction.valuation.ltm_ebitda,
                default=ZERO,
            )
        if deal.grid is None:
            raise SensitivityError(
                "this deal has no projection, so there is no exit year to read"
            )
        return money(len(deal.grid)) / money(deal.grid.frequency.periods_per_year)


_APPLY = {
    Dimension.ENTRY_MULTIPLE: _with_entry_multiple,
    Dimension.EXIT_MULTIPLE: _with_exit_multiple,
    Dimension.LEVERAGE: _with_leverage,
    Dimension.REVENUE_GROWTH: _with_revenue_growth,
    Dimension.EBITDA_MARGIN: _with_ebitda_margin,
    Dimension.EXIT_YEAR: _with_exit_year,
    Dimension.BASE_RATE: _with_base_rate,
}


# -- Metrics -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Case:
    """One deal, run end to end, with everything a metric might ask of it."""

    deal: Deal
    model: OperatingModel
    schedule: DebtSchedule
    outcome: Outcome
    covenants: CovenantReport | None

    @classmethod
    def run(cls, deal: Deal) -> Case:
        """Run every layer once.

        The case and the schedule are computed here and handed down rather than
        recomputed by each caller. The schedule solves a fixed point, and a grid
        that solved it twice per cell would take twice as long to say the same
        thing.
        """
        model = deal.project()
        schedule = deal.schedule(model)
        outcome = deal.realise(model, schedule)
        covenants = (
            deal.test_covenants(model, schedule) if deal.has_covenants else None
        )
        return cls(
            deal=deal,
            model=model,
            schedule=schedule,
            outcome=outcome,
            covenants=covenants,
        )

    @property
    def breached(self) -> bool:
        return self.covenants is not None and not self.covenants.passes


class Metric(Enum):
    """What a cell reports."""

    IRR = "irr"
    MOIC = "moic"
    EQUITY_VALUE = "equity-value"
    SPONSOR_EQUITY = "sponsor-equity"
    ENTRY_LEVERAGE = "entry-leverage"
    EXIT_LEVERAGE = "exit-leverage"
    CUSHION = "cushion"

    def __str__(self) -> str:
        return self.value

    @property
    def label(self) -> str:
        return {
            Metric.IRR: "IRR",
            Metric.MOIC: "MoIC",
            Metric.EQUITY_VALUE: "Equity value at exit",
            Metric.SPONSOR_EQUITY: "Sponsor equity at close",
            Metric.ENTRY_LEVERAGE: "Net leverage at close",
            Metric.EXIT_LEVERAGE: "Net leverage at exit",
            Metric.CUSHION: "Tightest covenant cushion",
        }[self]

    @property
    def unit(self) -> Unit:
        return {
            Metric.IRR: Unit.RATE,
            Metric.MOIC: Unit.MULTIPLE,
            Metric.EQUITY_VALUE: Unit.AMOUNT,
            Metric.SPONSOR_EQUITY: Unit.AMOUNT,
            Metric.ENTRY_LEVERAGE: Unit.TURNS,
            Metric.EXIT_LEVERAGE: Unit.TURNS,
            Metric.CUSHION: Unit.SHARE,
        }[self]

    @property
    def needs_covenants(self) -> bool:
        return self is Metric.CUSHION

    def read(self, case: Case) -> tuple[Money | None, str]:
        """The figure, or the reason there is not one.

        The reason travels with the missing number. A blank cell in a committee
        pack gets read as a zero unless something says otherwise.
        """
        outcome = case.outcome
        if self is Metric.IRR:
            if outcome.irr is None:
                return None, _no_rate(case)
            return money(repr(outcome.irr)), ""
        if self is Metric.MOIC:
            if outcome.moic is None:
                return None, "no capital was invested"
            return outcome.moic, ""
        if self is Metric.EQUITY_VALUE:
            return outcome.valuation.equity_value, ""
        if self is Metric.SPONSOR_EQUITY:
            return case.deal.transaction.sponsor_equity, ""
        if self is Metric.ENTRY_LEVERAGE:
            net_debt = case.deal.transaction.total_debt - case.schedule.opening_cash
            return (
                safe_div(
                    net_debt, case.deal.transaction.valuation.ltm_ebitda, default=ZERO
                ),
                "",
            )
        if self is Metric.EXIT_LEVERAGE:
            return outcome.valuation.exit_leverage, ""
        if case.covenants is None:
            return None, "this deal describes no covenants"
        cushion = case.covenants.minimum_cushion
        if cushion is None:
            return None, "no covenant is testable on this case"
        return cushion, ""


def _no_rate(case: Case) -> str:
    if case.outcome.valuation.is_wiped_out:
        return "the equity is wiped out"
    return "there is no rate on these flows"


# -- The grid ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Axis:
    """One side of the table: a dimension and the values stepped along it."""

    dimension: Dimension
    values: tuple[Money, ...]

    def __post_init__(self) -> None:
        if not self.values:
            raise SensitivityError(
                f"{self.dimension}: an axis needs at least one value on it"
            )
        if len(set(self.values)) != len(self.values):
            raise SensitivityError(f"{self.dimension}: an axis repeats a value")

    def __len__(self) -> int:
        return len(self.values)

    def __iter__(self) -> Iterator[Money]:
        return iter(self.values)

    @classmethod
    def of(cls, dimension: Dimension, values: Sequence[Money | str | int]) -> Axis:
        return cls(dimension=dimension, values=tuple(money(v) for v in values))

    @classmethod
    def parse(cls, text: str) -> Axis:
        """Read ``dimension:v1,v2,v3``.

        Percentage-point axes are written the way they are said — ``-1.5`` for a
        hundred and fifty basis points off — and divided by a hundred here. An
        axis of raw decimals for a shift would be ``-0.015``, which is easy to
        mistype by a factor of ten and hard to spot once it is in the table.
        """
        name, _, values_text = text.partition(":")
        name = name.strip().lower()
        try:
            dimension = Dimension(name)
        except ValueError:
            known = ", ".join(d.value for d in Dimension)
            raise SensitivityError(
                f"unknown dimension {name!r}; expected one of {known}"
            ) from None
        if not values_text.strip():
            raise SensitivityError(
                f"{name}: say which values to step along, as {name}:1,2,3"
            )
        values = []
        for token in values_text.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                value = Decimal(token)
            except InvalidOperation:
                raise SensitivityError(f"{name}: not a number: {token!r}") from None
            values.append(value / money(100) if dimension.is_shift else value)
        return cls(dimension=dimension, values=tuple(values))

    def format(self, value: Money) -> str:
        return format_value(self.dimension.unit, value)

    def is_base(self, value: Money, deal: Deal) -> bool:
        """Whether ``value`` is where the file already sits on this dimension."""
        try:
            return self.dimension.read(deal) == value
        except SensitivityError:
            return False


@dataclass(frozen=True, slots=True)
class Cell:
    """One cell: the coordinates, the figure, and whether it was reachable."""

    row: Money
    column: Money
    value: Money | None
    note: str = ""
    breached: bool = False
    breach_note: str = ""

    @property
    def ok(self) -> bool:
        return self.value is not None


@dataclass(frozen=True, slots=True)
class Grid:
    """A metric read across two axes, plus the base case it moves around."""

    rows: Axis
    columns: Axis
    metric: Metric
    cells: tuple[tuple[Cell, ...], ...]
    base: Money | None
    base_note: str = ""

    def __len__(self) -> int:
        return len(self.cells)

    def __iter__(self) -> Iterator[tuple[Cell, ...]]:
        return iter(self.cells)

    def at(self, row: int, column: int) -> Cell:
        return self.cells[row][column]

    @property
    def populated(self) -> tuple[Cell, ...]:
        return tuple(c for line in self.cells for c in line if c.ok)

    @property
    def failures(self) -> tuple[Cell, ...]:
        return tuple(c for line in self.cells for c in line if not c.ok)

    @property
    def breaches(self) -> tuple[Cell, ...]:
        return tuple(c for line in self.cells for c in line if c.breached)

    @classmethod
    def run(cls, deal: Deal, rows: Axis, columns: Axis, metric: Metric) -> Grid:
        """Rebuild and run ``deal`` at every intersection of the two axes."""
        if rows.dimension is columns.dimension:
            raise SensitivityError(
                f"both axes move the {rows.dimension.label.lower()}; a grid needs "
                f"two different assumptions to be a grid"
            )
        if metric.needs_covenants and not deal.has_covenants:
            raise SensitivityError(
                f"{metric.label.lower()} is measured against maintenance tests, "
                f'and this deal describes none; add a "covenants" block'
            )
        # The base case is run before anything is flexed, so a grid built on a
        # deal that does not run at all fails outright instead of printing a
        # table of identical excuses.
        base_case = Case.run(deal)
        base, base_note = metric.read(base_case)

        lines = []
        for row in rows:
            line = []
            for column in columns:
                line.append(_cell(deal, rows, row, columns, column, metric))
            lines.append(tuple(line))
        return cls(
            rows=rows,
            columns=columns,
            metric=metric,
            cells=tuple(lines),
            base=base,
            base_note=base_note,
        )


def _cell(
    deal: Deal,
    rows: Axis,
    row: Money,
    columns: Axis,
    column: Money,
    metric: Metric,
) -> Cell:
    """Rebuild the deal at one intersection and read the metric off it."""
    try:
        flexed = columns.dimension.apply(rows.dimension.apply(deal, row), column)
        case = Case.run(flexed)
    except CELL_FAILURES as exc:
        return Cell(row=row, column=column, value=None, note=str(exc))
    value, note = metric.read(case)
    breach = case.covenants.first_breach if case.covenants is not None else None
    return Cell(
        row=row,
        column=column,
        value=value,
        note=note,
        breached=breach is not None,
        breach_note=(
            "" if breach is None else f"{breach.covenant} in period {breach.index}"
        ),
    )


#: A neutral shift, named because ``money(0)`` at a call site reads as an amount.
NO_SHIFT: Money = ZERO

#: A multiple of one, for the same reason.
UNCHANGED: Money = ONE
