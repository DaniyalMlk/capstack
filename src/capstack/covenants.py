"""Maintenance covenants, and the headroom left under them.

The debt schedule answers whether a structure *can* be serviced. A credit
agreement asks a narrower and more consequential question: whether the business
is still permitted to carry it. Tripping a maintenance test does not stop the
interest being paid — it hands the lenders a right to accelerate, and that is
what turns a case that merely disappoints into one that is renegotiated.

Three decisions shape this layer.

*A threshold is a series.* Covenants step down: a leverage test set at 7.00x at
close will be 5.50x by the fourth year, tightening as the model promises
deleveraging. A single number cannot express that, and a model that tests every
year against the closing level reports headroom that does not exist in the early
years and a breach that does not exist in the later ones. Tests also start
late — commonly at the end of the fourth full quarter — so a holiday is part of
the description rather than something the reader is expected to remember.

*Headroom is measured in EBITDA, not in turns.* The distance between 5.20x and a
6.00x threshold is 0.80x, which is arithmetic rather than information. What gets
asked in a credit committee is how far EBITDA can fall before the test trips,
and that is a different number for every test on the same page: a leverage test
scales with EBITDA directly while a coverage test does not. Every observation
here carries the EBITDA at which it would breach and the shortfall from the
projection as a percentage, so the tightest test in a structure is visible
rather than inferred.

*An undefined ratio is not a pass.* A period with no EBITDA has no leverage
ratio, and reporting one is worse than reporting none. The distinction that
matters is whether the ratio is undefined because nothing is at risk — no debt
to be levered, no interest to be covered — or because the denominator collapsed,
which is the case a covenant exists to catch. The first passes, the second
breaches, and neither reports a number.

*Every ratio is measured over twelve months.* A covenant compares a stock
against a flow, and a flow only means something over a stated interval. That
interval is a year in every credit agreement, not a reporting period — so on a
quarterly grid the debt is the balance on the test date and the earnings are the
four quarters behind it. Dividing a whole debt balance by one quarter's earnings
reports a structure at 5.2x as 20.8x and breaches every test in the file from the
first certification. On an annual grid the trailing year *is* the period, and
nothing about an existing model changes.

A business that has not yet traded twelve months cannot certify. Nine months of
earnings is not a conservative year, it is a different measure, and a ratio built
on one reads a third too high. Those periods are reported untested with the
reason on the row — which is what already happened to a stub, for the same
reason.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import Enum

from .debt import DebtPeriod, DebtSchedule
from .drivers import Driver
from .money import ZERO, Money, Numeric, money
from .operating import OperatingModel, OperatingPeriod
from .periods import Period, TrailingWindow, trailing_window

__all__ = [
    "Certification",
    "Covenant",
    "CovenantObservation",
    "CovenantReport",
    "Direction",
    "Measure",
]

#: The interval a maintenance covenant is measured over, in months.
TEST_INTERVAL_MONTHS = 12


class Direction(Enum):
    """Which side of the threshold a test has to stay on."""

    MAXIMUM = "maximum"
    MINIMUM = "minimum"

    def __str__(self) -> str:
        return self.value


class Measure(Enum):
    """What a maintenance test measures.

    ``LEVERAGE`` and ``NET_LEVERAGE`` differ only in whether cash on the balance
    sheet is netted against the debt. The distinction is not cosmetic: a
    business holding a year of interest in cash is a different credit from one
    that is not, and which of the two a lender agreed to test on is one of the
    more heavily negotiated lines in a term sheet.

    ``FIXED_CHARGE_COVERAGE`` is the test with the most definitions in
    circulation. The one used here charges the business for the cash it cannot
    avoid spending — capital expenditure and tax — and asks whether what is left
    covers cash interest and contractual amortisation. Agreements differ on
    whether maintenance capex or all capex belongs in the numerator; the
    conservative reading is modelled, and a deal that negotiated the other one
    can express it by describing capex net of the growth component.
    """

    LEVERAGE = "leverage"
    NET_LEVERAGE = "net_leverage"
    INTEREST_COVERAGE = "interest_coverage"
    FIXED_CHARGE_COVERAGE = "fixed_charge_coverage"

    @property
    def direction(self) -> Direction:
        """Leverage is capped; coverage has a floor."""
        if self in (Measure.LEVERAGE, Measure.NET_LEVERAGE):
            return Direction.MAXIMUM
        return Direction.MINIMUM

    @property
    def is_leverage(self) -> bool:
        return self in (Measure.LEVERAGE, Measure.NET_LEVERAGE)

    @property
    def label(self) -> str:
        return self.value.replace("_", " ")

    def __str__(self) -> str:
        return self.value


#: What a ratio is called when there is nothing to measure and nothing at risk.
NOTHING_AT_RISK = "no exposure to measure"

#: What it is called when the denominator collapsed instead.
DENOMINATOR_COLLAPSED = "the business earned nothing to measure against"

#: What a period is called when the business has not traded a year yet.
NO_YEAR_YET = "fewer than twelve months traded; nothing to certify against"


@dataclass(frozen=True, slots=True)
class Certification:
    """Everything one test date is measured on, assembled over twelve months.

    The stocks — debt, cash — are the balances standing on the test date, which
    is what a balance is. The flows are summed over the year behind it, which is
    what a flow needs to be compared against one. Keeping them in a single
    object is what stops the two being mixed up at the point of division, which
    is the whole of the defect this class exists to prevent.
    """

    window: TrailingWindow
    debt: DebtPeriod
    ebitda: Money
    capital_expenditure: Money
    cash_tax: Money
    cash_cost_of_debt: Money
    mandatory_repayment: Money

    @property
    def period(self) -> Period:
        return self.debt.period

    @property
    def certifiable(self) -> bool:
        """Whether a full year stands behind this date."""
        return self.window.complete and not self.period.is_stub

    @property
    def interval(self) -> str:
        """How the measurement would be described on a compliance certificate."""
        if self.window.complete:
            return f"twelve months to {self.window.closes.isoformat()}"
        return f"{self.window.days} days to {self.window.closes.isoformat()}"

    @classmethod
    def assemble(
        cls,
        position: int,
        schedule: DebtSchedule,
        model: OperatingModel,
        *,
        months: int = TEST_INTERVAL_MONTHS,
    ) -> Certification:
        """Gather the twelve months ending with period ``position``."""
        periods = [row.period for row in schedule]
        window = trailing_window(periods, position, months)
        inside = {p.index for p in window}
        debt_rows = [row for row in schedule if row.period.index in inside]
        case_rows = [row for row in model if row.period.index in inside]
        return cls(
            window=window,
            debt=schedule[position],
            ebitda=sum((r.ebitda for r in case_rows), ZERO),
            capital_expenditure=sum((r.capital_expenditure for r in case_rows), ZERO),
            cash_tax=sum((r.tax.cash_tax for r in case_rows), ZERO),
            cash_cost_of_debt=sum((r.cash_cost_of_debt for r in debt_rows), ZERO),
            mandatory_repayment=sum((r.mandatory_repayment for r in debt_rows), ZERO),
        )


@dataclass(frozen=True, slots=True)
class Covenant:
    """One maintenance test, as a credit agreement would describe it.

    ``threshold`` is a series so a test can tighten over the life of the loan.
    ``first_test_period`` is 1-based and matches the period numbering used
    everywhere else, so a covenant first tested at the end of the second year of
    an annual model has a first test period of 2.

    ``tranches`` narrows a leverage test to part of the stack, which is how a
    first-lien net leverage covenant is written: the same business, the same
    EBITDA, a smaller numerator. Naming no tranches measures the whole stack.
    """

    name: str
    measure: Measure
    threshold: Driver
    first_test_period: int = 1
    tranches: tuple[str, ...] = ()

    @classmethod
    def of(
        cls,
        name: str,
        measure: Measure,
        threshold: Driver | Numeric,
        *,
        first_test_period: int = 1,
        tranches: Sequence[str] = (),
    ) -> Covenant:
        """Build a covenant, accepting a bare number as a flat threshold."""
        series = (
            threshold
            if isinstance(threshold, Driver)
            else Driver.of([money(threshold)])
        )
        return cls(
            name=name,
            measure=measure,
            threshold=series,
            first_test_period=first_test_period,
            tranches=tuple(tranches),
        )

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("a covenant needs a name")
        if self.first_test_period < 1:
            raise ValueError(
                f"{self.name}: the first test period is 1-based, so 1 or later"
            )
        if any(v <= 0 for v in self.threshold):
            raise ValueError(f"{self.name}: a threshold must be positive")
        if self.tranches and not self.measure.is_leverage:
            raise ValueError(
                f"{self.name}: naming tranches narrows the debt a leverage test "
                f"measures, and {self.measure.label} does not measure debt"
            )
        if len(set(self.tranches)) != len(self.tranches):
            raise ValueError(f"{self.name}: tranche names must be distinct")

    def threshold_at(self, index: int) -> Money:
        """The level in force for period ``index``, zero-based."""
        return self.threshold.at(index)

    def tests(self, index: int) -> bool:
        """Whether the covenant is live in period ``index``, zero-based."""
        return index + 1 >= self.first_test_period

    def satisfied_by(self, actual: Money, threshold: Money) -> bool:
        if self.measure.direction is Direction.MAXIMUM:
            return actual <= threshold
        return actual >= threshold


@dataclass(frozen=True, slots=True)
class CovenantObservation:
    """One covenant, tested in one period.

    ``actual`` is ``None`` when the ratio is undefined, and ``note`` says which
    of the two undefined cases it was. A reader should never have to work out
    whether a blank cell was a pass or a breach; ``passes`` is always populated.
    """

    covenant: str
    measure: Measure
    period: Period
    tested: bool
    threshold: Money
    actual: Money | None
    passes: bool
    ebitda: Money
    ebitda_at_breach: Money | None
    note: str = ""
    window: TrailingWindow | None = None

    @property
    def index(self) -> int:
        return int(self.period.index)

    @property
    def interval(self) -> str:
        """The interval the flows were measured over, as a certificate says it.

        Worth carrying on the row rather than leaving to the reader: on an
        annual grid it restates the column heading, and on a quarterly one it is
        the difference between a figure a lender would recognise and one four
        times too large.
        """
        if self.window is None:
            return f"period to {self.period.end.isoformat()}"
        if self.window.complete:
            return f"twelve months to {self.window.closes.isoformat()}"
        # Said in days rather than rounded up to months, because the whole
        # reason the row is untested is that the interval is not the one a
        # certificate names, and describing it as a year would bury that.
        return f"{self.window.days} days to {self.window.closes.isoformat()}"

    @property
    def breached(self) -> bool:
        return self.tested and not self.passes

    @property
    def headroom(self) -> Money | None:
        """Distance to the threshold in turns, positive when the test passes.

        Signed so that the reading is the same for both directions: a leverage
        test two turns below its cap and a coverage test two turns above its
        floor both report two turns of headroom.
        """
        if self.actual is None:
            return None
        if self.measure.direction is Direction.MAXIMUM:
            return self.threshold - self.actual
        return self.actual - self.threshold

    @property
    def ebitda_cushion(self) -> Money | None:
        """How far EBITDA could fall before the test trips, as a share of itself.

        Negative when the test is already breached, in which case it reads as
        the increase in EBITDA that would cure it. The debt and the charges are
        held at their projected levels while EBITDA is flexed, which is the
        standard simplification and the reason this is a cushion rather than a
        forecast: a business earning less would in reality also sweep less and
        carry more debt, so the true cushion is a little thinner than the one
        reported here.
        """
        if self.ebitda_at_breach is None or self.ebitda <= 0:
            return None
        return (self.ebitda - self.ebitda_at_breach) / self.ebitda


def _measured_debt(
    covenant: Covenant, row: DebtPeriod
) -> Money:
    """Closing debt in scope for a leverage test."""
    if not covenant.tranches:
        return row.closing_debt
    wanted = set(covenant.tranches)
    return sum((t.closing for t in row.tranches if t.name in wanted), ZERO)


def _observe(
    covenant: Covenant,
    index: int,
    certification: Certification,
) -> CovenantObservation:
    """Test one covenant on one test date, over the year behind it."""
    threshold = covenant.threshold_at(index)
    debt = certification.debt
    ebitda = certification.ebitda

    def untested(note: str) -> CovenantObservation:
        return CovenantObservation(
            covenant=covenant.name,
            measure=covenant.measure,
            period=debt.period,
            tested=False,
            threshold=threshold,
            actual=None,
            passes=True,
            ebitda=ebitda,
            ebitda_at_breach=None,
            note=note,
            window=certification.window,
        )

    # A stub is not a test date. A maintenance covenant is certified on a
    # reporting date against the last twelve months' earnings, and a business
    # six weeks into a hold has no twelve months to certify — measuring debt
    # against a part-period figure would report leverage several turns worse
    # than the deal carries and breach a covenant nobody has breached.
    if debt.period.is_stub:
        return untested("stub period; no twelve months to certify against")

    # The same objection, and it outlasts the stub. On a quarterly grid the
    # first three certification dates have three, six and nine months behind
    # them, and none of those is a year.
    if not certification.certifiable:
        return untested(NO_YEAR_YET)

    if not covenant.tests(index):
        return untested("not yet tested")

    if covenant.measure.is_leverage:
        numerator = _measured_debt(covenant, debt)
        if covenant.measure is Measure.NET_LEVERAGE:
            numerator -= debt.closing_cash
        # Net of a cash balance larger than the debt, the ratio is negative,
        # which is a real and passing state rather than something to clamp.
        if ebitda > 0:
            actual = numerator / ebitda
            at_breach = numerator / threshold if numerator > 0 else ZERO
            return CovenantObservation(
                covenant=covenant.name,
                measure=covenant.measure,
                period=debt.period,
                tested=True,
                threshold=threshold,
                actual=actual,
                passes=covenant.satisfied_by(actual, threshold),
                ebitda=ebitda,
                ebitda_at_breach=at_breach,
                window=certification.window,
            )
        passes = numerator <= 0
        return CovenantObservation(
            covenant=covenant.name,
            measure=covenant.measure,
            period=debt.period,
            tested=True,
            threshold=threshold,
            actual=None,
            passes=passes,
            ebitda=ebitda,
            ebitda_at_breach=None,
            note=NOTHING_AT_RISK if passes else DENOMINATOR_COLLAPSED,
            window=certification.window,
        )

    # Both sides of a coverage test are flows, so both are taken over the same
    # twelve months. Measuring a period of earnings against a period of charges
    # would land near the right answer by accident on a regular grid and nowhere
    # near it on a grid with a stub, or in a year holding a refinancing.
    if covenant.measure is Measure.INTEREST_COVERAGE:
        charges = certification.cash_cost_of_debt
        earnings = ebitda
        floor = ZERO
    else:
        charges = certification.cash_cost_of_debt + certification.mandatory_repayment
        unavoidable = certification.capital_expenditure + certification.cash_tax
        earnings = ebitda - unavoidable
        floor = unavoidable

    if charges <= 0:
        # Nothing to cover. A business with no cash debt service cannot fail a
        # coverage test, and reporting an infinite ratio would be worse than
        # reporting none.
        return CovenantObservation(
            covenant=covenant.name,
            measure=covenant.measure,
            period=debt.period,
            tested=True,
            threshold=threshold,
            actual=None,
            passes=True,
            ebitda=ebitda,
            ebitda_at_breach=None,
            note=NOTHING_AT_RISK,
            window=certification.window,
        )

    actual = earnings / charges
    return CovenantObservation(
        covenant=covenant.name,
        measure=covenant.measure,
        period=debt.period,
        tested=True,
        threshold=threshold,
        actual=actual,
        passes=covenant.satisfied_by(actual, threshold),
        ebitda=ebitda,
        ebitda_at_breach=threshold * charges + floor,
        window=certification.window,
    )


@dataclass(frozen=True, slots=True)
class CovenantReport:
    """Every covenant tested across every period of a schedule."""

    covenants: tuple[Covenant, ...]
    observations: tuple[CovenantObservation, ...]

    def __len__(self) -> int:
        return len(self.observations)

    def __iter__(self) -> Iterator[CovenantObservation]:
        return iter(self.observations)

    @classmethod
    def test(
        cls,
        covenants: Sequence[Covenant],
        schedule: DebtSchedule,
        model: OperatingModel,
    ) -> CovenantReport:
        """Run ``covenants`` against a schedule and the case that produced it.

        Both are required and neither is derivable from the other: the schedule
        holds the debt and what it costs, the operating case holds the EBITDA
        the tests are measured against and the capex and tax a fixed-charge test
        deducts.
        """
        if len(schedule) != len(model):
            raise ValueError(
                f"the schedule covers {len(schedule)} periods and the operating "
                f"case {len(model)}"
            )
        known = {t.name for t in schedule.structure}
        for covenant in covenants:
            unknown = [n for n in covenant.tranches if n not in known]
            if unknown:
                raise ValueError(
                    f"{covenant.name}: no tranche named {unknown[0]!r} in the structure"
                )

        # Ordered by period and then by covenant, because the page is read down
        # a column: every test for one period before the next period's.
        # Assembled once per test date rather than once per covenant: gathering
        # a year of flows is the expensive half, and every covenant certified on
        # the same date is measured on the same year of them.
        certifications = [
            Certification.assemble(i, schedule, model) for i in range(len(schedule))
        ]
        observations = tuple(
            _observe(covenant, schedule[i].period.driver_index, certifications[i])
            for i in range(len(schedule))
            for covenant in covenants
        )
        return cls(covenants=tuple(covenants), observations=observations)

    def for_covenant(self, name: str) -> tuple[CovenantObservation, ...]:
        rows = tuple(o for o in self.observations if o.covenant == name)
        if not rows:
            raise KeyError(f"no covenant named {name!r}")
        return rows

    def at(self, index: int) -> tuple[CovenantObservation, ...]:
        """Every test in period ``index``, zero-based."""
        periods = sorted({o.index for o in self.observations})
        if not periods:
            return ()
        if not -len(periods) <= index < len(periods):
            raise IndexError(f"period {index} is outside the report")
        wanted = periods[index]
        return tuple(o for o in self.observations if o.index == wanted)

    @property
    def breaches(self) -> tuple[CovenantObservation, ...]:
        return tuple(o for o in self.observations if o.breached)

    @property
    def first_breach(self) -> CovenantObservation | None:
        """The earliest failing test, which is the one that ends the hold.

        Everything after a breach is hypothetical — the lenders have a right to
        accelerate from that date — so the first one is the only one that
        strictly matters, and the rest are shown because a reader wants to know
        whether the case recovers.
        """
        return self.breaches[0] if self.breaches else None

    @property
    def passes(self) -> bool:
        return not self.breaches

    @property
    def tightest(self) -> CovenantObservation | None:
        """The tested observation with the least EBITDA cushion.

        The single number that describes how much room a structure has, and the
        one a sponsor is asked for before signing.
        """
        measurable = [
            o for o in self.observations if o.tested and o.ebitda_cushion is not None
        ]
        if not measurable:
            return None
        return min(measurable, key=lambda o: o.ebitda_cushion or ZERO)

    @property
    def minimum_cushion(self) -> Money | None:
        tightest = self.tightest
        return None if tightest is None else tightest.ebitda_cushion
