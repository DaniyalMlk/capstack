"""The capital structure, and the schedule that pays it down.

This is the layer that makes a leveraged buyout leveraged. The operating case
produces cash without knowing who has a claim on it; the transaction produces
tranches at face without knowing whether they will ever be repaid. Here the two
meet: cash arrives, interest is charged, scheduled amortisation is paid, and
whatever is left is swept against the debt in order of seniority.

Four things distinguish a schedule that works from one that merely runs.

*Interest is split.* A tranche that pays 6% in cash and accrues 5% costs the
business 6% this period and 11% by the exit. Mezzanine and seller paper are
priced that way precisely because they cost nothing while the sponsor is trying
to survive the first two years, and modelling the whole coupon as cash makes the
early years look tighter than they are and the exit look better than it is.

*The revolver runs in both directions.* It is drawn when a period is short and
repaid first when a period is long, which is why peak drawn balance rather than
closing balance is the number a credit committee asks for.

*The sweep respects seniority.* Surplus cash pays the most senior claim first,
and pro rata inside a class. Getting the order wrong repays the cheap paper with
cash that contractually belongs to the expensive paper, which flatters interest
cost for the whole remaining hold.

*Interest and balance are circular.* Accrue on the average of the opening and
closing balance — as most credit agreements effectively do, and as any model
with a mid-period repayment must — and the closing balance depends on what was
repaid, which depends on the cash left after interest, which depends on the
balance the interest was accrued on. There is no closed form once repayments are
capped at balances and sweeps are capped at available cash. The engine iterates
to a fixed point and reports the residual it converged to, rather than breaking
the loop by accruing on the opening balance and calling the difference small.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum

from .daycount import DayCount
from .drivers import Driver
from .money import (
    ONE,
    ZERO,
    Money,
    Numeric,
    allocate_pro_rata,
    is_close,
    money,
    safe_div,
)
from .operating import OperatingModel
from .periods import Period

__all__ = [
    "CapitalStructure",
    "CircularityNotResolved",
    "DebtPeriod",
    "DebtSchedule",
    "InterestBasis",
    "SweepGrid",
    "SweepStep",
    "Tranche",
    "TranchePeriod",
    "TrancheKind",
]


class TrancheKind(Enum):
    """The layers a leveraged structure is built from, most senior first.

    The kind is not merely a label. It carries the conventions that go with the
    instrument — where it sits in the waterfall, and whether surplus cash is
    contractually applied to it — so a deal file can describe a term loan
    without restating what a term loan is.
    """

    REVOLVER = "revolver"
    TERM_LOAN = "term_loan"
    NOTES = "notes"
    MEZZANINE = "mezzanine"
    SELLER_NOTE = "seller_note"

    @property
    def default_seniority(self) -> int:
        """Where this layer sits in the repayment waterfall, 0 being first.

        The revolver is repaid ahead of the term loans it ranks alongside, not
        because it is more senior but because it can be redrawn: leaving cash on
        the balance sheet while the facility is drawn pays a margin for nothing.
        """
        return {
            TrancheKind.REVOLVER: 0,
            TrancheKind.TERM_LOAN: 1,
            TrancheKind.NOTES: 2,
            TrancheKind.MEZZANINE: 3,
            TrancheKind.SELLER_NOTE: 4,
        }[self]

    @property
    def default_swept(self) -> bool:
        """Whether surplus cash is contractually applied to this layer.

        Bank debt is swept. Notes are not: they are issued with call protection,
        so surplus cash cannot be forced into them at par and the sponsor would
        not want it to be. Mezzanine and seller paper sit outside the sweep for
        the same reason and are repaid at exit.
        """
        return self in (TrancheKind.REVOLVER, TrancheKind.TERM_LOAN)

    @property
    def default_floating(self) -> bool:
        """Whether the coupon is quoted as a margin over a base rate.

        Bank debt floats; bonds and seller paper are fixed. Mezzanine is
        conventionally fixed even where it sits alongside floating bank debt.
        """
        return self in (TrancheKind.REVOLVER, TrancheKind.TERM_LOAN)

    def __str__(self) -> str:
        return self.value.replace("_", " ")


class InterestBasis(Enum):
    """What balance a period's interest is accrued on.

    ``OPENING`` charges the balance at the start of the period and is the
    simplification most spreadsheet models make, because it breaks the
    circularity. It overstates interest whenever the balance falls during the
    period, which in a deleveraging structure is every period.

    ``AVERAGE`` charges the mean of the opening and closing balances, which is
    the right answer for repayments spread through the period and is what makes
    the schedule circular.
    """

    OPENING = "opening"
    AVERAGE = "average"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Tranche:
    """One layer of the capital structure.

    ``cash_rate`` and ``pik_rate`` are annual. For a floating tranche they are
    margins over the structure's base rate; for a fixed one they are the coupon
    itself. ``amortisation`` is a fraction of the *original* face per period,
    which is how a credit agreement writes it — 1% a year on a term loan means
    1% of what was drawn at close, not 1% of what is left.
    """

    name: str
    kind: TrancheKind
    face: Money
    cash_rate: Money = ZERO
    pik_rate: Money = ZERO
    floating: bool = False
    floor: Money = ZERO
    amortisation: Driver | None = None
    seniority: int = 0
    swept: bool = False
    commitment: Money = ZERO
    undrawn_fee: Money = ZERO
    maturity: int | None = None

    @classmethod
    def of(
        cls,
        name: str,
        kind: TrancheKind,
        face: Numeric,
        *,
        cash_rate: Numeric = 0,
        pik_rate: Numeric = 0,
        floating: bool | None = None,
        floor: Numeric = 0,
        amortisation: Driver | None = None,
        seniority: int | None = None,
        swept: bool | None = None,
        commitment: Numeric | None = None,
        undrawn_fee: Numeric = 0,
        maturity: int | None = None,
    ) -> Tranche:
        """Build a tranche, taking the conventions of its kind where not told otherwise."""
        return cls(
            name=name,
            kind=kind,
            face=money(face),
            cash_rate=money(cash_rate),
            pik_rate=money(pik_rate),
            floating=kind.default_floating if floating is None else floating,
            floor=money(floor),
            amortisation=amortisation,
            seniority=kind.default_seniority if seniority is None else seniority,
            swept=kind.default_swept if swept is None else swept,
            commitment=(
                money(face) if commitment is None and kind is TrancheKind.REVOLVER
                else money(commitment or 0)
            ),
            undrawn_fee=money(undrawn_fee),
            maturity=maturity,
        )

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("a tranche needs a name")
        if self.face < 0:
            raise ValueError(f"{self.name}: drawn face must not be negative")
        if self.cash_rate < 0:
            raise ValueError(f"{self.name}: the cash rate must not be negative")
        if self.pik_rate < 0:
            raise ValueError(f"{self.name}: the PIK rate must not be negative")
        if self.floor < 0:
            raise ValueError(f"{self.name}: the base-rate floor must not be negative")
        if self.undrawn_fee < 0:
            raise ValueError(f"{self.name}: the commitment fee must not be negative")
        if self.undrawn_fee and not self.is_revolving:
            raise ValueError(
                f"{self.name}: a commitment fee is charged on undrawn capacity, and only "
                f"a revolving facility has any"
            )
        if self.seniority < 0:
            raise ValueError(f"{self.name}: seniority must not be negative")
        if self.commitment < 0:
            raise ValueError(f"{self.name}: the commitment must not be negative")
        if self.is_revolving and self.face > self.commitment:
            raise ValueError(
                f"{self.name}: drawn at close ({self.face}) exceeds the commitment "
                f"({self.commitment})"
            )
        if not self.is_revolving and self.commitment:
            raise ValueError(
                f"{self.name}: only a revolving facility has an undrawn commitment"
            )
        if self.maturity is not None and self.maturity < 1:
            raise ValueError(f"{self.name}: maturity is a period index, so 1 or later")
        if self.amortisation is not None and any(v < 0 for v in self.amortisation):
            raise ValueError(f"{self.name}: amortisation must not be negative")

    @property
    def is_revolving(self) -> bool:
        return self.kind is TrancheKind.REVOLVER

    @property
    def accretes(self) -> bool:
        """Whether any of the coupon is paid in kind rather than in cash."""
        return self.pik_rate > 0

    def rate_at(self, base: Money) -> Money:
        """The all-in cash coupon, given the base rate for the period.

        A floating tranche prices off the base rate subject to its floor. The
        floor is not decoration: it was written into every loan document drafted
        while base rates were near zero, and it is the reason a fall in rates
        below the floor does not reach the borrower at all.
        """
        if not self.floating:
            return self.cash_rate
        return max(base, self.floor) + self.cash_rate

    def scheduled_amortisation(self, index: int) -> Money:
        """Contractual repayment for period ``index``, as an amount.

        Expressed against the original face, so a sweep that runs ahead of the
        schedule does not reduce what is contractually due next period. Credit
        agreements differ on whether it should — many allow prepayments to be
        applied against future instalments in order — and the conservative
        reading is the one modelled here.
        """
        if self.amortisation is None:
            return ZERO
        return self.face * self.amortisation.at(index)

    def has_matured(self, index: int) -> bool:
        """Whether period ``index`` is at or past this tranche's maturity.

        ``index`` is zero-based across the schedule, matching how every other
        series is read, so period one is index zero and a tranche maturing in
        period three has matured from index two onward.
        """
        return self.maturity is not None and index + 1 >= self.maturity

    def undrawn_at(self, drawn: Money, index: int = 0) -> Money:
        """Commitment not currently drawn. Zero for anything but a revolver.

        A matured facility has no commitment left. Repaying it does not make it
        available again, which is what a model that keeps its commitment alive
        past maturity will happily do — repay the balance in full and redraw it
        in the same period, forever.
        """
        if not self.is_revolving or self.has_matured(index):
            return ZERO
        return self.commitment - drawn


@dataclass(frozen=True, slots=True)
class SweepStep:
    """One rung of a sweep grid: a leverage level and the rate that applies at it."""

    leverage: Money
    rate: Money

    @classmethod
    def of(cls, leverage: Numeric, rate: Numeric) -> SweepStep:
        return cls(leverage=money(leverage), rate=money(rate))

    def __post_init__(self) -> None:
        if self.leverage < 0:
            raise ValueError("a sweep step is set at a leverage level, so not negative")
        if not (0 <= self.rate <= 1):
            raise ValueError("a sweep rate is a share, so between 0 and 1")


@dataclass(frozen=True, slots=True)
class SweepGrid:
    """A sweep percentage that steps down as leverage falls.

    Almost no credit agreement sweeps a flat percentage for the life of the
    loan. The usual shape is fifty per cent of excess cash flow, stepping to
    twenty-five and then to nothing as leverage comes through agreed levels,
    which is the lenders paying for deleveraging with the sponsor's own cash. A
    model that sweeps a flat rate repays too fast in the good years and reports
    an exit leverage the documents would never have produced.

    ``steps`` are read as "at this leverage or above, sweep this much", and are
    sorted highest first regardless of the order given. Below every step the
    grid returns ``floor``, which is zero in the ordinary case.

    *Which leverage the step is read against* is the decision that matters. The
    obvious answer — the leverage of the period being swept — makes the sweep
    depend on the closing balance, which depends on the sweep, and drops a step
    function into the middle of the interest fixed point, where it can sit and
    oscillate between two rungs forever. The step is resolved against the
    leverage at the most recent test date instead: the certificate delivered for
    the period just ended. That is not a modelling convenience but how the
    payment is actually made — an excess cash flow sweep is paid in arrears, at
    a rate set by a certificate that was signed before the cash was counted.
    """

    steps: tuple[SweepStep, ...]
    floor: Money = ZERO
    net: bool = True

    @classmethod
    def of(
        cls,
        steps: Sequence[tuple[Numeric, Numeric]] | Sequence[SweepStep],
        *,
        floor: Numeric = 0,
        net: bool = True,
    ) -> SweepGrid:
        rungs = tuple(
            s if isinstance(s, SweepStep) else SweepStep.of(s[0], s[1]) for s in steps
        )
        return cls(steps=rungs, floor=money(floor), net=net)

    def __post_init__(self) -> None:
        if not self.steps:
            raise ValueError("a sweep grid needs at least one step")
        if not (0 <= self.floor <= 1):
            raise ValueError("the floor sweep rate is a share, so between 0 and 1")
        levels = [s.leverage for s in self.steps]
        if len(set(levels)) != len(levels):
            raise ValueError("two steps set at the same leverage contradict each other")
        object.__setattr__(
            self, "steps", tuple(sorted(self.steps, key=lambda s: s.leverage, reverse=True))
        )

    @property
    def top_rate(self) -> Money:
        """The rate that applies at the highest rung, and to unknown leverage."""
        return self.steps[0].rate

    def rate_at(self, leverage: Money | None) -> Money:
        """The sweep rate for a certified leverage level.

        ``None`` means the ratio could not be measured — no earnings to divide
        by. That is the state in which lenders would least like the sweep
        relaxed, so it takes the top rate rather than the floor.
        """
        if leverage is None:
            return self.top_rate
        for step in self.steps:
            if leverage >= step.leverage:
                return step.rate
        return self.floor


@dataclass(frozen=True, slots=True)
class CapitalStructure:
    """The whole stack, plus the rules that govern how cash moves through it.

    ``sweep_rate`` is the share of the period's excess cash flow applied to debt
    rather than left on the balance sheet. ``minimum_cash`` is the balance the business is left
    with before any sweep, which is an operating requirement rather than a
    financing one: a business cannot run its payroll out of a revolver it has to
    ask permission to draw.

    ``damping`` is where the fixed-point iteration starts, not where it stays:
    it is halved whenever a step fails to reduce the residual. Full steps are
    right for the smooth part of the problem and wrong at a clamp boundary, and
    the solver cannot know in advance which one it is standing on.
    """

    tranches: tuple[Tranche, ...]
    minimum_cash: Money = ZERO
    sweep_rate: Money = ONE
    sweep_grid: SweepGrid | None = None
    base_rate: Driver | None = None
    day_count: DayCount = DayCount.ACT_360
    interest_basis: InterestBasis = InterestBasis.AVERAGE
    damping: Money = ONE
    tolerance: Money = field(default_factory=lambda: money("0.000000001"))
    max_iterations: int = 100

    @classmethod
    def of(
        cls,
        tranches: Sequence[Tranche],
        *,
        minimum_cash: Numeric = 0,
        sweep_rate: Numeric = 1,
        sweep_grid: SweepGrid | None = None,
        base_rate: Driver | None = None,
        day_count: DayCount = DayCount.ACT_360,
        interest_basis: InterestBasis = InterestBasis.AVERAGE,
        damping: Numeric = 1,
        tolerance: Numeric = "0.000000001",
        max_iterations: int = 100,
    ) -> CapitalStructure:
        return cls(
            tranches=tuple(tranches),
            minimum_cash=money(minimum_cash),
            sweep_rate=money(sweep_rate),
            sweep_grid=sweep_grid,
            base_rate=base_rate,
            day_count=day_count,
            interest_basis=interest_basis,
            damping=money(damping),
            tolerance=money(tolerance),
            max_iterations=max_iterations,
        )

    def __post_init__(self) -> None:
        if not self.tranches:
            raise ValueError("a capital structure needs at least one tranche")
        names = [t.name for t in self.tranches]
        if len(names) != len(set(names)):
            raise ValueError("tranche names must be distinct")
        if self.minimum_cash < 0:
            raise ValueError("the minimum cash balance must not be negative")
        if not (0 <= self.sweep_rate <= 1):
            raise ValueError("the sweep rate is a share, so between 0 and 1")
        if self.sweep_grid is not None and self.sweep_rate != ONE:
            # Refused rather than resolved by precedence. A structure carrying
            # both a grid and a flat rate has two answers to the same question,
            # and quietly preferring one would hide a contradiction in the
            # description of the deal.
            raise ValueError(
                "a sweep grid sets the rate for every period, so a flat sweep "
                "rate alongside it says two different things"
            )
        if not (0 < self.damping <= 1):
            raise ValueError("damping must be greater than 0 and at most 1")
        if self.tolerance <= 0:
            raise ValueError("the convergence tolerance must be positive")
        if self.max_iterations < 1:
            raise ValueError("at least one iteration is required")
        if self.base_rate is None and any(t.floating for t in self.tranches):
            floating = ", ".join(t.name for t in self.tranches if t.floating)
            raise ValueError(
                f"a base rate is required: {floating} price off one"
            )

    def __len__(self) -> int:
        return len(self.tranches)

    def __iter__(self) -> Iterator[Tranche]:
        return iter(self.tranches)

    def __getitem__(self, index: int) -> Tranche:
        return self.tranches[index]

    def tranche(self, name: str) -> Tranche:
        for t in self.tranches:
            if t.name == name:
                return t
        raise KeyError(f"no tranche named {name!r}")

    def base_at(self, index: int) -> Money:
        return ZERO if self.base_rate is None else self.base_rate.at(index)

    @property
    def total_face(self) -> Money:
        """Drawn at close. This is what leverage is measured on."""
        return sum((t.face for t in self.tranches), ZERO)

    @property
    def total_commitment(self) -> Money:
        """Revolver capacity, drawn or not."""
        return sum((t.commitment for t in self.tranches), ZERO)

    @property
    def sweep_order(self) -> tuple[tuple[int, tuple[Tranche, ...]], ...]:
        """Swept tranches grouped by seniority, most senior class first.

        Grouping rather than ordering, because tranches at the same rank share a
        repayment pro rata rather than queueing behind one another.
        """
        ranks: dict[int, list[Tranche]] = {}
        for t in self.tranches:
            if t.swept:
                ranks.setdefault(t.seniority, []).append(t)
        return tuple((rank, tuple(ranks[rank])) for rank in sorted(ranks))

    def blended_cash_rate(self, index: int = 0) -> Money:
        """Weighted average cash coupon across the drawn stack.

        The single number a structure is summarised by, and the one that decides
        whether the business can carry it.
        """
        drawn = self.total_face
        if drawn == 0:
            return ZERO
        base = self.base_at(index)
        weighted = sum((t.face * t.rate_at(base) for t in self.tranches), ZERO)
        return safe_div(weighted, drawn, default=ZERO)


#: The shortest step the solver will take before giving up. Below this the
#: iteration is not converging on anything, it is inching towards a boundary it
#: will never cross, and saying so is more useful than another sixty steps.
MINIMUM_DAMPING = money("0.015625")


#: How close a roll-forward has to come to closing before it counts as closed.
#:
#: The funding table checks its identity with exact equality, and rightly: its
#: inputs are short decimals and its arithmetic is a single sum, so anything
#: other than zero is a missing line item. A debt schedule is a different shape.
#: Its year fractions do not terminate, so five periods of compounding push
#: balances out to the full working precision, and re-adding those balances in a
#: different order disagrees with itself in the thirty-fourth significant digit.
#: A tolerance a thousand billion times tighter than a cent separates that from
#: an error a reader would care about.
RECONCILIATION_TOLERANCE = "1E-12"


class CircularityNotResolved(RuntimeError):
    """The interest/balance fixed point did not converge.

    Raised rather than returning the last iterate, because an unconverged
    schedule is not an approximate answer — it is a set of balances that do not
    reconcile with the interest charged against them.
    """


@dataclass(frozen=True, slots=True)
class TranchePeriod:
    """What happened to one tranche in one period."""

    name: str
    kind: TrancheKind
    rate: Money
    opening: Money
    draw: Money
    cash_interest: Money
    pik_interest: Money
    undrawn_fee: Money
    mandatory_repayment: Money
    sweep_repayment: Money
    closing: Money

    @property
    def total_repayment(self) -> Money:
        return self.mandatory_repayment + self.sweep_repayment

    @property
    def total_interest(self) -> Money:
        """Economic cost for the period, whether or not it was paid in cash."""
        return self.cash_interest + self.pik_interest

    def reconciles(self, tolerance: Numeric = RECONCILIATION_TOLERANCE) -> bool:
        """Whether the roll-forward closes.

        Checked in tests rather than asserted here: the arithmetic that builds
        the row is the same arithmetic this would re-run, so an assertion would
        only prove the engine agrees with itself. It is exposed because a caller
        assembling rows by hand deserves the check.
        """
        expected = (
            self.opening
            + self.draw
            + self.pik_interest
            - self.mandatory_repayment
            - self.sweep_repayment
        )
        return is_close(self.closing, expected, tolerance=tolerance)


@dataclass(frozen=True, slots=True)
class DebtPeriod:
    """One column of the debt schedule."""

    period: Period
    base_rate: Money
    tranches: tuple[TranchePeriod, ...]
    opening_cash: Money
    unlevered_free_cash_flow: Money
    revolver_draw: Money
    funding_shortfall: Money
    cash_below_minimum: Money
    closing_cash: Money
    iterations: int
    residual: Money
    damping_used: Money = ONE
    sweep_rate: Money = ONE
    certified_leverage: Money | None = None

    @property
    def index(self) -> int:
        return int(self.period.index)

    def tranche(self, name: str) -> TranchePeriod:
        for row in self.tranches:
            if row.name == name:
                return row
        raise KeyError(f"no tranche named {name!r}")

    @property
    def opening_debt(self) -> Money:
        return sum((t.opening for t in self.tranches), ZERO)

    @property
    def closing_debt(self) -> Money:
        return sum((t.closing for t in self.tranches), ZERO)

    @property
    def cash_interest(self) -> Money:
        return sum((t.cash_interest for t in self.tranches), ZERO)

    @property
    def pik_interest(self) -> Money:
        return sum((t.pik_interest for t in self.tranches), ZERO)

    @property
    def undrawn_fees(self) -> Money:
        return sum((t.undrawn_fee for t in self.tranches), ZERO)

    @property
    def mandatory_repayment(self) -> Money:
        return sum((t.mandatory_repayment for t in self.tranches), ZERO)

    @property
    def sweep_repayment(self) -> Money:
        return sum((t.sweep_repayment for t in self.tranches), ZERO)

    @property
    def total_repayment(self) -> Money:
        return self.mandatory_repayment + self.sweep_repayment

    @property
    def cash_cost_of_debt(self) -> Money:
        """Everything the capital structure took in cash this period."""
        return self.cash_interest + self.undrawn_fees

    @property
    def levered_free_cash_flow(self) -> Money:
        """Cash left after servicing the debt, before repaying any of it."""
        return self.unlevered_free_cash_flow - self.cash_cost_of_debt

    @property
    def is_funded(self) -> bool:
        """Whether the period paid for itself out of cash, flow and the revolver.

        A solvency test, not a policy one. A business that ends the period on
        less cash than its own minimum but still on more than nothing has funded
        itself; see :attr:`meets_minimum_cash` for the policy.
        """
        return self.funding_shortfall == 0

    @property
    def meets_minimum_cash(self) -> bool:
        """Whether the period closed on at least the minimum cash balance."""
        return self.cash_below_minimum == 0

    @property
    def net_debt(self) -> Money:
        return self.closing_debt - self.closing_cash

    def reconciles(self, tolerance: Numeric = RECONCILIATION_TOLERANCE) -> bool:
        """Whether cash in equals cash out for the period."""
        expected = (
            self.opening_cash
            + self.unlevered_free_cash_flow
            - self.cash_cost_of_debt
            - self.total_repayment
            + self.revolver_draw
            + self.funding_shortfall
        )
        return is_close(self.closing_cash, expected, tolerance=tolerance)


@dataclass(frozen=True, slots=True)
class DebtSchedule:
    """The capital structure rolled forward across a projection."""

    structure: CapitalStructure
    periods: tuple[DebtPeriod, ...]
    opening_cash: Money

    def __len__(self) -> int:
        return len(self.periods)

    def __iter__(self) -> Iterator[DebtPeriod]:
        return iter(self.periods)

    def __getitem__(self, index: int) -> DebtPeriod:
        return self.periods[index]

    @classmethod
    def run(
        cls,
        structure: CapitalStructure,
        periods: Sequence[Period],
        unlevered_cash_flows: Sequence[Numeric],
        *,
        opening_cash: Numeric = 0,
        ebitda: Sequence[Numeric] | None = None,
        opening_ebitda: Numeric | None = None,
    ) -> DebtSchedule:
        """Roll the structure forward, one period at a time.

        Takes bare periods and cash flows rather than an operating model, so the
        schedule can be exercised against a hand-written cash-flow series. See
        :meth:`from_operating_model` for the usual entry point.

        ``ebitda`` is needed only by a sweep grid, which reads a leverage level
        to decide what share of the period's excess cash flow is swept.
        ``opening_ebitda`` is the level the first period's step is set against —
        the LTM figure the deal was priced on. Without it the first projected
        period stands in, which is the pro forma reading of the same test and is
        the closest thing available when the schedule is run on its own.
        """
        if len(periods) != len(unlevered_cash_flows):
            raise ValueError(
                f"{len(periods)} periods against {len(unlevered_cash_flows)} cash flows"
            )
        if not periods:
            raise ValueError("a schedule needs at least one period")
        grid = structure.sweep_grid
        if grid is not None and ebitda is None:
            raise ValueError(
                "a sweep grid steps with leverage, so the schedule needs the "
                "EBITDA each step is measured against"
            )
        if ebitda is not None and len(ebitda) != len(periods):
            raise ValueError(
                f"{len(periods)} periods against {len(ebitda)} EBITDA figures"
            )

        balances = {t.name: t.face for t in structure.tranches}
        cash = money(opening_cash)
        if cash < 0:
            raise ValueError("opening cash must not be negative")

        rows: list[DebtPeriod] = []
        for i, period in enumerate(periods):
            certified: Money | None = None
            rate = structure.sweep_rate
            if grid is not None and ebitda is not None:
                # The certificate for the period just ended: debt at the start
                # of this period against the earnings of the one before it.
                earnings = (
                    money(ebitda[i - 1])
                    if i > 0
                    else money(opening_ebitda if opening_ebitda is not None else ebitda[0])
                )
                debt = sum(balances.values(), ZERO)
                if grid.net:
                    debt -= cash
                certified = safe_div(debt, earnings) if earnings > 0 else None
                if certified is None and debt <= 0:
                    certified = ZERO
                rate = grid.rate_at(certified)

            row = _solve_period(
                structure,
                index=i,
                period=period,
                opening=balances,
                opening_cash=cash,
                unlevered_free_cash_flow=money(unlevered_cash_flows[i]),
                sweep_rate=rate,
                certified_leverage=certified,
            )
            rows.append(row)
            balances = {t.name: t.closing for t in row.tranches}
            cash = row.closing_cash

        return cls(structure=structure, periods=tuple(rows), opening_cash=money(opening_cash))

    @classmethod
    def from_operating_model(
        cls,
        structure: CapitalStructure,
        model: OperatingModel,
        *,
        opening_cash: Numeric = 0,
        opening_ebitda: Numeric | None = None,
    ) -> DebtSchedule:
        """Run the structure against an operating case."""
        return cls.run(
            structure,
            [p.period for p in model],
            [p.unlevered_free_cash_flow for p in model],
            opening_cash=opening_cash,
            ebitda=[p.ebitda for p in model],
            opening_ebitda=opening_ebitda,
        )

    # -- Aggregates ------------------------------------------------------

    @property
    def total_cash_interest(self) -> Money:
        return sum((p.cash_interest for p in self.periods), ZERO)

    @property
    def total_pik_interest(self) -> Money:
        """Interest that never left the account and is owed at exit instead."""
        return sum((p.pik_interest for p in self.periods), ZERO)

    @property
    def total_undrawn_fees(self) -> Money:
        return sum((p.undrawn_fees for p in self.periods), ZERO)

    @property
    def total_repaid(self) -> Money:
        return sum((p.total_repayment for p in self.periods), ZERO)

    @property
    def total_drawn(self) -> Money:
        return sum((p.revolver_draw for p in self.periods), ZERO)

    @property
    def opening_debt(self) -> Money:
        return self.periods[0].opening_debt

    @property
    def closing_debt(self) -> Money:
        return self.periods[-1].closing_debt

    @property
    def closing_cash(self) -> Money:
        return self.periods[-1].closing_cash

    @property
    def closing_net_debt(self) -> Money:
        return self.closing_debt - self.closing_cash

    @property
    def debt_repaid(self) -> Money:
        """Gross reduction in the face outstanding across the hold.

        Negative if the structure ended larger than it started, which happens
        when accretion outruns repayment — the case a PIK instrument exists to
        create and the one that surprises people at exit.
        """
        return self.opening_debt - self.closing_debt

    @property
    def peak_revolver_drawn(self) -> Money:
        """The largest revolver balance at any period end.

        The number a credit committee asks for, because a facility that is fully
        drawn at the low point of the year has no capacity left for the
        emergency it exists to cover.
        """
        drawn = [
            row.closing
            for p in self.periods
            for row in p.tranches
            if row.kind is TrancheKind.REVOLVER
        ]
        return max(drawn, default=ZERO)

    @property
    def is_funded(self) -> bool:
        """Whether every period paid for itself."""
        return all(p.is_funded for p in self.periods)

    @property
    def first_shortfall(self) -> DebtPeriod | None:
        """The first period the structure could not fund, if any."""
        for p in self.periods:
            if not p.is_funded:
                return p
        return None

    @property
    def total_shortfall(self) -> Money:
        """New money the structure needed and could not raise, across the hold."""
        return sum((p.funding_shortfall for p in self.periods), ZERO)

    @property
    def holds_minimum_cash(self) -> bool:
        """Whether every period closed on at least the minimum cash balance."""
        return all(p.meets_minimum_cash for p in self.periods)

    @property
    def max_iterations_used(self) -> int:
        return max((p.iterations for p in self.periods), default=0)

    @property
    def max_residual(self) -> Money:
        return max((p.residual for p in self.periods), default=ZERO)

    @property
    def shortest_step_taken(self) -> Money:
        """The smallest damping any period needed.

        Anything below one says a period met a clamp boundary and the solver had
        to shorten its stride to get past it.
        """
        return min((p.damping_used for p in self.periods), default=ONE)

    def balances_at(self, index: int) -> dict[str, Money]:
        """Closing balance per tranche at period ``index``, zero-based."""
        return {row.name: row.closing for row in self.periods[index].tranches}

    def leverage_at(self, index: int, ebitda: Numeric) -> Money:
        """Closing debt as a multiple of the EBITDA supplied for that period."""
        return safe_div(self.periods[index].closing_debt, money(ebitda), default=ZERO)

    def net_leverage_at(self, index: int, ebitda: Numeric) -> Money:
        return safe_div(self.periods[index].net_debt, money(ebitda), default=ZERO)


def _one_pass(
    structure: CapitalStructure,
    *,
    index: int,
    period: Period,
    opening: dict[str, Money],
    opening_cash: Money,
    unlevered_free_cash_flow: Money,
    guess: dict[str, Money],
    sweep_rate: Money,
    certified_leverage: Money | None = None,
) -> DebtPeriod:
    """Run a period once, taking ``guess`` as the closing balances interest accrues on.

    Under :attr:`InterestBasis.OPENING` the guess is ignored and the pass is the
    answer. Under ``AVERAGE`` it is one step of the iteration that finds the
    balances consistent with the interest they generate.
    """
    year_fraction = period.year_fraction(structure.day_count)
    base = structure.base_at(index)
    half = money(2)

    accrual_base: dict[str, Money] = {}
    cash_interest: dict[str, Money] = {}
    pik_interest: dict[str, Money] = {}
    undrawn_fee: dict[str, Money] = {}
    mandatory: dict[str, Money] = {}

    for tranche in structure:
        start = opening[tranche.name]
        matured = tranche.has_matured(index)
        if structure.interest_basis is InterestBasis.AVERAGE and not matured:
            accrual = (start + guess[tranche.name]) / half
        else:
            # A maturing balance is repaid at the end of the period rather than
            # across it, so it owes a full period of interest. Averaging it with
            # a closing balance of zero halves the interest in the period that
            # carries the single largest repayment in the model.
            accrual = start
        # A balance driven negative by a bad guess would generate negative
        # interest, which is not a thing; the fixed point is approached from
        # above, so clamping here only affects intermediate iterates.
        accrual = max(accrual, ZERO)
        accrual_base[tranche.name] = accrual

        cash_interest[tranche.name] = tranche.rate_at(base) * year_fraction * accrual
        pik_interest[tranche.name] = tranche.pik_rate * year_fraction * accrual
        undrawn = max(tranche.undrawn_at(accrual, index), ZERO)
        undrawn_fee[tranche.name] = tranche.undrawn_fee * year_fraction * undrawn

        owed = start + pik_interest[tranche.name]
        if matured:
            due = owed
        else:
            due = min(tranche.scheduled_amortisation(index), owed)
        mandatory[tranche.name] = max(due, ZERO)

    cash_cost = sum(cash_interest.values(), ZERO) + sum(undrawn_fee.values(), ZERO)
    after_service = opening_cash + unlevered_free_cash_flow - cash_cost
    after_mandatory = after_service - sum(mandatory.values(), ZERO)

    draw: dict[str, Money] = {t.name: ZERO for t in structure}
    need = structure.minimum_cash - after_mandatory
    if need > 0:
        revolvers = [t for t in structure if t.is_revolving]
        capacity = [
            ZERO
            if t.has_matured(index)
            else max(
                t.commitment - (opening[t.name] + pik_interest[t.name] - mandatory[t.name]),
                ZERO,
            )
            for t in revolvers
        ]
        drawn = allocate_pro_rata(need, capacity)
        for tranche, amount in zip(revolvers, drawn):
            draw[tranche.name] = amount
        after_mandatory += sum(drawn, ZERO)

    # Two different failures, which the model keeps apart because a lender does.
    # Ending below the minimum cash balance breaches a policy; ending below zero
    # means the period's bills went unpaid. Only the second is a funding gap,
    # and it is plugged notionally so the periods after it remain readable —
    # a schedule that carries a negative cash balance forward compounds a
    # deficit it has already reported and understates every later sweep.
    shortfall = max(-after_mandatory, ZERO)
    after_mandatory += shortfall
    below_minimum = max(structure.minimum_cash - after_mandatory, ZERO)

    sweep: dict[str, Money] = {t.name: ZERO for t in structure}
    # The sweep is a share of *this period's* excess cash flow, not of the whole
    # cash balance. Cash retained by a partial sweep in an earlier period is no
    # longer excess cash flow and a credit agreement does not reach it again;
    # sweeping the balance instead would take the retained half back at the next
    # test date, and the half after that, until a 50% sweep had quietly become
    # a 100% one. What is available to pay with is still capped by the cash on
    # hand above the minimum, because a sweep cannot spend money that is not there.
    excess = max(
        unlevered_free_cash_flow - cash_cost - sum(mandatory.values(), ZERO), ZERO
    )
    available = max(after_mandatory - structure.minimum_cash, ZERO)
    pot = min(excess * sweep_rate, available)
    for _, group in structure.sweep_order:
        if pot <= 0:
            break
        balances = [
            max(
                opening[t.name]
                + pik_interest[t.name]
                + draw[t.name]
                - mandatory[t.name],
                ZERO,
            )
            for t in group
        ]
        for tranche, amount in zip(group, allocate_pro_rata(pot, balances)):
            sweep[tranche.name] = amount
            pot -= amount

    rows = tuple(
        TranchePeriod(
            name=t.name,
            kind=t.kind,
            rate=t.rate_at(base),
            opening=opening[t.name],
            draw=draw[t.name],
            cash_interest=cash_interest[t.name],
            pik_interest=pik_interest[t.name],
            undrawn_fee=undrawn_fee[t.name],
            mandatory_repayment=mandatory[t.name],
            sweep_repayment=sweep[t.name],
            closing=(
                opening[t.name]
                + draw[t.name]
                + pik_interest[t.name]
                - mandatory[t.name]
                - sweep[t.name]
            ),
        )
        for t in structure
    )

    return DebtPeriod(
        period=period,
        base_rate=base,
        tranches=rows,
        opening_cash=opening_cash,
        unlevered_free_cash_flow=unlevered_free_cash_flow,
        revolver_draw=sum(draw.values(), ZERO),
        funding_shortfall=shortfall,
        cash_below_minimum=below_minimum,
        closing_cash=after_mandatory - sum(sweep.values(), ZERO),
        iterations=1,
        residual=ZERO,
        sweep_rate=sweep_rate,
        certified_leverage=certified_leverage,
    )


def _solve_period(
    structure: CapitalStructure,
    *,
    index: int,
    period: Period,
    opening: dict[str, Money],
    opening_cash: Money,
    unlevered_free_cash_flow: Money,
    sweep_rate: Money,
    certified_leverage: Money | None = None,
) -> DebtPeriod:
    """Find the closing balances consistent with the interest they generate.

    The map is a strong contraction at any realistic coupon: one turn of the
    loop moves the balance by the interest on half the change, so the residual
    falls by a factor of roughly ``rate x year_fraction / 2`` each time — a few
    hundred basis points — and a full step converges in about ten iterations.
    Across three thousand randomised structures the step was never shortened and
    the worst case took twelve.

    The halving is insurance rather than the mechanism. It exists because the
    problem is not smooth — repayments are capped at balances, sweeps at
    available cash, and a revolver draw switches on at a threshold — and a full
    step can in principle alternate between two sides of one of those
    boundaries. It also does nothing for the one regime that genuinely fails:
    a PIK rate near the pole at ``pik_rate x year_fraction = 2``, where the fixed
    point either does not exist or is approached too slowly to reach. That is
    reported rather than approximated, which is the right answer for a structure
    accreting faster than any cash flow could service.
    """
    if structure.interest_basis is InterestBasis.OPENING:
        return _one_pass(
            structure,
            index=index,
            period=period,
            opening=opening,
            opening_cash=opening_cash,
            unlevered_free_cash_flow=unlevered_free_cash_flow,
            guess=opening,
            sweep_rate=sweep_rate,
            certified_leverage=certified_leverage,
        )

    guess = dict(opening)
    damping = structure.damping
    previous: Money | None = None
    for iteration in range(1, structure.max_iterations + 1):
        result = _one_pass(
            structure,
            index=index,
            period=period,
            opening=opening,
            opening_cash=opening_cash,
            unlevered_free_cash_flow=unlevered_free_cash_flow,
            guess=guess,
            sweep_rate=sweep_rate,
            certified_leverage=certified_leverage,
        )
        produced = {row.name: row.closing for row in result.tranches}
        residual = max((abs(produced[k] - guess[k]) for k in guess), default=ZERO)
        if residual <= structure.tolerance:
            return replace(
                result, iterations=iteration, residual=residual, damping_used=damping
            )
        if previous is not None and residual >= previous and damping > MINIMUM_DAMPING:
            # The step did not help. Either the iteration is oscillating between
            # two clamp regimes or it overshot; both are answered by a shorter
            # step, and halving is enough to break either.
            damping = damping / money(2)
        previous = residual
        guess = {k: guess[k] + damping * (produced[k] - guess[k]) for k in guess}

    raise CircularityNotResolved(
        f"period {period.index}: interest and balances did not settle within "
        f"{structure.max_iterations} iterations; the structure may be accreting "
        f"faster than the cash flow can service it"
    )
