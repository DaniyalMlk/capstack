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
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import Enum

from .debt import DebtPeriod, DebtSchedule
from .drivers import Driver
from .money import ZERO, Money, Numeric, money
from .operating import OperatingModel, OperatingPeriod
from .periods import Period

__all__ = [
    "Covenant",
    "CovenantObservation",
    "CovenantReport",
    "Direction",
    "Measure",
]


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

    @property
    def index(self) -> int:
        return int(self.period.index)

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
    debt: DebtPeriod,
    operating: OperatingPeriod,
) -> CovenantObservation:
    """Test one covenant in one period."""
    threshold = covenant.threshold_at(index)
    ebitda = operating.ebitda

    # A stub is not a test date. A maintenance covenant is certified on a
    # reporting date against the last twelve months' earnings, and a business
    # six weeks into a hold has no twelve months to certify — measuring debt
    # against a part-period figure would report leverage several turns worse
    # than the deal carries and breach a covenant nobody has breached.
    if debt.period.is_stub:
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
            note="stub period; no twelve months to certify against",
        )

    if not covenant.tests(index):
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
            note="not yet tested",
        )

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
        )

    if covenant.measure is Measure.INTEREST_COVERAGE:
        charges = debt.cash_cost_of_debt
        earnings = ebitda
        floor = ZERO
    else:
        charges = debt.cash_cost_of_debt + debt.mandatory_repayment
        earnings = ebitda - operating.capital_expenditure - operating.tax.cash_tax
        floor = operating.capital_expenditure + operating.tax.cash_tax

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
        observations = tuple(
            _observe(covenant, schedule[i].period.driver_index, schedule[i], model[i])
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
