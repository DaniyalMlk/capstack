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

import dataclasses
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum

from .daycount import DayCount
from .drivers import Driver
from .events import (
    AddOn,
    AddOnError,
    AddOnOutcome,
    Recapitalisation,
    RecapitalisationError,
    RecapitalisationOutcome,
    Refinancing,
    RefinancingError,
    RefinancingOutcome,
)
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
from .periods import Period, trailing_window

__all__ = [
    "AmortisationBasis",
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
    itself. ``amortisation`` is a fraction of face per period, which is how a
    credit agreement writes it — 1% a year on a term loan means 1% of what was
    borrowed, not 1% of what is left. The face it is struck against is the face
    drawn at close plus anything drawn on the name since; the schedule carries
    that forward in an :class:`AmortisationBasis`.
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
    availability: int | None = None
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
        availability: int | None = None,
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
            availability=availability,
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
        if self.undrawn_fee and not self.commitment:
            raise ValueError(
                f"{self.name}: a fee on undrawn capacity needs a commitment to be "
                f"undrawn against"
            )
        if self.seniority < 0:
            raise ValueError(f"{self.name}: seniority must not be negative")
        if self.commitment < 0:
            raise ValueError(f"{self.name}: the commitment must not be negative")
        if self.commitment and self.face > self.commitment:
            raise ValueError(
                f"{self.name}: drawn at close ({self.face}) exceeds the commitment "
                f"({self.commitment})"
            )
        if self.commitment and not self.is_revolving and self.availability is None:
            raise ValueError(
                f"{self.name}: a term commitment states how long it can be drawn. "
                f"A revolver is available until it matures, but a delayed-draw or "
                f"acquisition facility has an availability period, and without one "
                f"there is nothing to stop the ticking fee running to maturity"
            )
        if self.availability is not None and self.availability < 1:
            raise ValueError(
                f"{self.name}: availability is a period index, so 1 or later"
            )
        if (
            self.availability is not None
            and self.maturity is not None
            and self.availability > self.maturity
        ):
            raise ValueError(
                f"{self.name}: available to draw in period {self.availability}, which "
                f"is after it matures in period {self.maturity}"
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

    def scheduled_amortisation(self, index: int, basis: Money | None = None) -> Money:
        """Contractual repayment for period ``index``, as an amount.

        Struck against face rather than against the balance outstanding, so a
        sweep that runs ahead of the schedule does not reduce what is
        contractually due next period. Credit agreements differ on whether it
        should — many allow prepayments to be applied against future instalments
        in order — and the conservative reading is the one modelled here.

        ``basis`` is the face the instalment is measured on. It defaults to the
        face drawn at close, which is the whole story for paper placed at close
        and only part of it for anything drawn later; see
        :class:`AmortisationBasis` for how the schedule carries it forward.
        """
        if self.amortisation is None:
            return ZERO
        face = self.face if basis is None else basis
        return face * self.amortisation.at(index)

    def has_matured(self, index: int) -> bool:
        """Whether period ``index`` is at or past this tranche's maturity.

        ``index`` is zero-based across the schedule, matching how every other
        series is read, so period one is index zero and a tranche maturing in
        period three has matured from index two onward.
        """
        return self.matured_at(index + 1)

    def matured_at(self, period: int) -> bool:
        """Whether the period *numbered* ``period`` is at or past maturity.

        Stated in period numbers rather than in positions because that is how a
        credit agreement states it, and because a grid opening on a stub has a
        period zero — paper maturing in period two matures at the end of the
        second whole period whether or not six weeks of trading sit in front of
        the first.
        """
        return self.maturity is not None and period >= self.maturity

    @property
    def has_commitment(self) -> bool:
        """Whether anything can be drawn on this name after close."""
        return self.commitment > 0

    def available_at(self, period: int) -> bool:
        """Whether the facility can still be drawn in the period numbered ``period``.

        A revolver is available until it matures. A term commitment — a
        delayed-draw facility, a committed acquisition line — is available for a
        stated period and then lapses, drawn or not, which is the difference
        between the two instruments and the reason the availability period is
        required on one and optional on the other.
        """
        if self.matured_at(period):
            return False
        return self.availability is None or period <= self.availability

    def undrawn_at(self, drawn: Money, index: int = 0, *, period: int | None = None) -> Money:
        """Commitment not yet available to the borrower, given what is ``drawn``.

        What ``drawn`` means differs by instrument, and the difference is the
        whole distinction between the two. On a revolver it is the balance
        outstanding: repay it and the capacity comes back, which is what
        revolving means. On a term commitment it is the cumulative face taken
        down since close, because repaying a delayed-draw term loan does not
        entitle anyone to draw it again. The caller passes the right one; a
        model that passed the balance for both would let a borrower draw an
        acquisition facility, repay it out of cash flow, and draw it again.

        A matured facility has no commitment left, and neither has one whose
        availability period has run — which is what stops a ticking fee being
        charged for the rest of a hold on capacity nobody can take down.
        """
        at = index + 1 if period is None else period
        if not self.has_commitment or not self.available_at(at):
            return ZERO
        return self.commitment - drawn


class AmortisationBasis:
    """The face each tranche's instalment is struck against, carried forward.

    A credit agreement writes amortisation as a fraction of face — 1% a year on
    a term loan means 1% of what was borrowed, not 1% of what is left. Reading
    "what was borrowed" as the face drawn at close is right for paper placed at
    close and wrong for everything else, in both directions.

    It understates. A delayed-draw facility described with nothing drawn at
    close has a basis of zero, so it repays nothing however much is taken down
    on it later; an incremental facility drawn on an existing name grows the
    balance without growing the instalment, so a term loan that doubles in size
    still repays 1% of the original.

    And it overstates. A facility retired at a refinancing keeps a basis it no
    longer has any face behind, so the schedule carries a mandatory repayment on
    paper that has been taken out.

    So the basis is a ledger rather than a property of the tranche. It opens at
    the face drawn at close, rises by incremental face taken down at a period
    boundary, and goes to zero when the facility is retired. Draws are applied
    *after* the period they land in has been solved, which is what makes face
    drawn at the end of period two first amortise in period three — the
    instalment for a period is struck on the basis that period opened on.
    """

    __slots__ = ("_basis", "_retired")

    def __init__(self, structure: CapitalStructure) -> None:
        self._basis: dict[str, Money] = {t.name: t.face for t in structure}
        self._retired: set[str] = set()

    def at(self, name: str) -> Money:
        """The face period instalments are currently measured against."""
        return self._basis[name]

    def snapshot(self) -> dict[str, Money]:
        return dict(self._basis)

    def draw(self, name: str, amount: Money) -> None:
        """Add face taken down on an existing name.

        Incremental face amortises on the same terms as the paper it joins,
        which is what an incremental facility documented under an existing
        credit agreement actually does — it is fungible with the original by
        construction, so it cannot repay on a different schedule.
        """
        if amount <= 0:
            return
        self._basis[name] += amount

    def retire(self, name: str) -> None:
        """Drop the basis to zero because the facility has been taken out.

        Zero rather than reduced by the balance repaid: a refinancing retires
        the facility, not a slice of it, and what remains of the original face
        is a claim the replacement paper now carries on its own terms.

        The name is remembered as well as zeroed. A basis of zero is otherwise
        indistinguishable from a committed facility nobody has drawn yet, and
        the two want opposite treatment: the undrawn one still ticks, and the
        retired one has no commitment left to tick on.
        """
        self._basis[name] = ZERO
        self._retired.add(name)

    @property
    def retired(self) -> frozenset[str]:
        """The facilities taken out, which can no longer be drawn."""
        return frozenset(self._retired)

    def apply(self, row: DebtPeriod) -> None:
        """Carry the basis across a solved period's events, in the order they ran."""
        for tranche in row.tranches:
            if tranche.refinancing_repayment > 0:
                self.retire(tranche.name)
            self.draw(tranche.name, tranche.incremental_draw)


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
    recapitalisation: Money = ZERO
    acquisition: Money = ZERO
    refinancing_draw: Money = ZERO
    refinancing_repayment: Money = ZERO
    amortisation_basis: Money = ZERO

    @property
    def incremental_draw(self) -> Money:
        """Face taken down at the period boundary, whatever it funded.

        Split by purpose rather than carried as one number, because a reader of
        the schedule wants to know whether a tranche grew to pay the
        shareholders, to buy something, or to replace paper priced in a worse
        market."""
        return self.recapitalisation + self.acquisition + self.refinancing_draw

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
            + self.incremental_draw
            - self.refinancing_repayment
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
    distribution: Money = ZERO
    distribution_from_cash: Money = ZERO
    acquisition_spend: Money = ZERO
    acquisition_from_cash: Money = ZERO
    call_premium: Money = ZERO
    fees_written_off: Money = ZERO
    refinancing_from_cash: Money = ZERO

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
    def recapitalisation(self) -> Money:
        """Incremental face taken down at the end of this period."""
        return sum((t.recapitalisation for t in self.tranches), ZERO)

    @property
    def acquisition_debt(self) -> Money:
        """Incremental face taken down to fund an acquisition."""
        return sum((t.acquisition for t in self.tranches), ZERO)

    @property
    def refinanced(self) -> Money:
        """Face retired early at the period boundary."""
        return sum((t.refinancing_repayment for t in self.tranches), ZERO)

    @property
    def refinancing_draw(self) -> Money:
        """Face of the paper that replaced it."""
        return sum((t.refinancing_draw for t in self.tranches), ZERO)

    @property
    def incremental_draw(self) -> Money:
        return self.recapitalisation + self.acquisition_debt + self.refinancing_draw

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
            - self.distribution_from_cash
            - self.acquisition_from_cash
            - self.refinancing_from_cash
        )
        return is_close(self.closing_cash, expected, tolerance=tolerance)


def _by_period(
    events: Sequence[Recapitalisation], structure: CapitalStructure, span: int
) -> dict[int, Recapitalisation]:
    """Index the events by the period they land in, checking each one is possible.

    Everything checkable without running the schedule is checked here, so a
    structure that cannot support the event fails before twenty periods of
    arithmetic rather than in the middle of them.
    """
    known = {t.name for t in structure.tranches}
    scheduled: dict[int, Recapitalisation] = {}
    for event in events:
        if event.period > span:
            raise RecapitalisationError(
                f"{event.label}: period {event.period} is beyond the {span} "
                f"periods the schedule covers"
            )
        if event.period in scheduled:
            raise RecapitalisationError(
                f"two recapitalisations land at the end of period {event.period}; "
                f"combine them into one"
            )
        for draw in event.draws:
            if draw.tranche not in known:
                raise RecapitalisationError(
                    f"{event.label}: there is no tranche named {draw.tranche!r} to "
                    f"draw on; the structure holds "
                    f"{', '.join(repr(n) for n in sorted(known))}"
                )
            tranche = structure.tranche(draw.tranche)
            if tranche.matured_at(event.period):
                raise RecapitalisationError(
                    f"{event.label}: {draw.tranche} has matured by period "
                    f"{event.period}, so there is nothing left to draw on it"
                )
        scheduled[event.period] = event
    return scheduled


def _refinancings_by_period(
    events: Sequence[Refinancing],
    structure: CapitalStructure,
    span: int,
    taken: frozenset[int],
) -> dict[int, Refinancing]:
    """Index the takeouts by period, checking what can be checked in advance.

    Whether there is anything left outstanding to retire cannot be checked here
    — it depends on how fast the sweep has run — so that one waits for the
    schedule. Everything else fails before twenty periods of arithmetic rather
    than in the middle of them.
    """
    known = {t.name for t in structure.tranches}
    scheduled: dict[int, Refinancing] = {}
    for event in events:
        if event.period > span:
            raise RefinancingError(
                f"{event.label}: period {event.period} is beyond the {span} periods "
                f"the schedule covers"
            )
        if event.period in scheduled:
            raise RefinancingError(
                f"two refinancings land at the end of period {event.period}; "
                f"combine them into one"
            )
        if event.period in taken:
            raise RefinancingError(
                f"{event.label}: another event already lands at the end of period "
                f"{event.period}; two things happening at the same instant cannot "
                f"say which came first, and the balance they each act on differs "
                f"by which"
            )
        for name in (event.tranche, *(d.tranche for d in event.into)):
            if name not in known:
                raise RefinancingError(
                    f"{event.label}: there is no tranche named {name!r}; the "
                    f"structure holds {', '.join(repr(n) for n in sorted(known))}"
                )
        if structure.tranche(event.tranche).is_revolving:
            raise RefinancingError(
                f"{event.label}: {event.tranche} is a revolver, which the schedule "
                f"already repays and redraws every period; refinancing it says "
                f"nothing the sweep has not said"
            )
        scheduled[event.period] = event
    return scheduled


def _refinance(
    structure: CapitalStructure,
    row: DebtPeriod,
    event: Refinancing,
    *,
    index: int,
    span: int,
    unamortised_fees: Money = ZERO,
) -> tuple[DebtPeriod, RefinancingOutcome]:
    """Retire a facility at the end of a solved period and draw its replacement.

    The order is repay then draw, which matters when the new paper is the same
    facility at a new price: a repricing nets to the difference rather than
    carrying both balances for an instant, and a same-tranche takeout that drew
    first would report a balance the credit agreement never showed.

    The balance retired is the one left after the period's own amortisation and
    sweep have run, because that is the balance the notice would be served on.
    """
    balance = row.tranche(event.tranche).closing
    if balance <= 0:
        raise RefinancingError(
            f"{event.label}: {event.tranche} has nothing outstanding at the end of "
            f"period {event.period}, so there is nothing to refinance"
        )

    from_cash = event.from_cash(balance)
    available = row.closing_cash - structure.minimum_cash
    if from_cash > available:
        raise RefinancingError(
            f"{event.label}: retiring {balance} at a premium needs {from_cash} off a "
            f"balance sheet holding {row.closing_cash} against a minimum of "
            f"{structure.minimum_cash}, which leaves only {available}; raise more on "
            f"the new paper or move the takeout later"
        )

    base = structure.base_at(index)
    old = structure.tranche(event.tranche)
    old_rate = old.rate_at(base) + old.pik_rate
    new_rate = ZERO
    if event.face > 0:
        weighted = sum(
            (
                d.amount
                * (
                    structure.tranche(d.tranche).rate_at(base)
                    + structure.tranche(d.tranche).pik_rate
                )
                for d in event.into
            ),
            ZERO,
        )
        new_rate = safe_div(weighted, event.face, default=ZERO)

    tranches = tuple(
        replace(
            t,
            refinancing_repayment=balance if t.name == event.tranche else ZERO,
            refinancing_draw=event.draw_on(t.name),
            closing=(
                t.closing
                - (balance if t.name == event.tranche else ZERO)
                + event.draw_on(t.name)
            ),
        )
        for t in row.tranches
    )
    cash_after = row.closing_cash - from_cash
    written_off = event.write_off(unamortised_fees)
    updated = replace(
        row,
        tranches=tranches,
        closing_cash=cash_after,
        call_premium=event.premium_on(balance),
        fees_written_off=written_off,
        refinancing_from_cash=from_cash,
    )
    return updated, RefinancingOutcome(
        event=event,
        index=index,
        repaid=balance,
        cash_before=row.closing_cash,
        cash_after=cash_after,
        old_rate=old_rate,
        new_rate=new_rate,
        periods_remaining=span - event.period,
        written_off=written_off,
    )


def _acquisitions_by_period(
    events: Sequence[AddOn],
    structure: CapitalStructure,
    span: int,
    taken: dict[int, Recapitalisation],
) -> dict[int, AddOn]:
    """Index the acquisitions by period, checking each one before anything runs.

    An acquisition in the final period is refused rather than modelled. It would
    pay cash for earnings no period ever records, and the exit — priced on the
    final period's EBITDA — would then value nothing at all: a purchase price
    out, no earnings in, and a bridge that blames the shortfall on the operating
    case. Bringing the acquisition forward or extending the projection is what
    the file actually means.
    """
    known = {t.name for t in structure.tranches}
    scheduled: dict[int, AddOn] = {}
    for event in events:
        if event.period >= span:
            raise AddOnError(
                f"{event.label}: closes at the end of period {event.period} of "
                f"{span}, so nothing it buys is ever earned; move it earlier or "
                f"lengthen the projection"
            )
        if event.period in scheduled:
            raise AddOnError(
                f"two acquisitions close at the end of period {event.period}; "
                f"combine them into one"
            )
        if event.period in taken:
            raise AddOnError(
                f"{event.label}: a recapitalisation already lands at the end of "
                f"period {event.period}; a model that pays the shareholders and "
                f"buys a business at the same instant cannot say which happened "
                f"first, and the leverage differs by which"
            )
        for draw in event.draws:
            if draw.tranche not in known:
                raise AddOnError(
                    f"{event.label}: there is no tranche named {draw.tranche!r} to "
                    f"draw on; the structure holds "
                    f"{', '.join(repr(n) for n in sorted(known))}"
                )
            if structure.tranche(draw.tranche).matured_at(event.period):
                raise AddOnError(
                    f"{event.label}: {draw.tranche} has matured by period "
                    f"{event.period}, so there is nothing left to draw on it"
                )
        scheduled[event.period] = event
    return scheduled


def _acquire(
    structure: CapitalStructure,
    row: DebtPeriod,
    event: AddOn,
    *,
    index: int,
    earnings: Money | None,
) -> tuple[DebtPeriod, AddOnOutcome]:
    """Apply an acquisition at the end of a solved period.

    The same placement as a recapitalisation, and for the same reason: the new
    balance is what the following period opens on, so interest, the sweep and
    the covenant tests all pick the acquisition up without knowing it happened.

    What differs is the direction of the cash and what it buys. A
    recapitalisation takes cash out of the business and leaves the earnings
    alone. An acquisition takes cash out and puts earnings in — but the earnings
    arrive through the operating case, one layer up, which is why nothing here
    touches EBITDA. The leverage recorded on either side of the event is
    therefore measured against the *same* earnings, and reads worse than the
    deal is: the debt lands a period before the profit it bought.
    """
    from_cash = event.from_cash
    available = row.closing_cash - structure.minimum_cash
    if from_cash > available:
        raise AddOnError(
            f"{event.label}: needs {from_cash} off a balance sheet holding "
            f"{row.closing_cash} against a minimum of {structure.minimum_cash}, "
            f"which leaves only {available}; fund more of it with debt or move it "
            f"to a period the business can pay for it in"
        )

    before = _leverage(row.closing_debt, row.closing_cash, earnings)
    tranches = tuple(
        replace(
            t,
            acquisition=event.draw_on(t.name),
            closing=t.closing + event.draw_on(t.name),
        )
        for t in row.tranches
    )
    cash_after = row.closing_cash - from_cash
    updated = replace(
        row,
        tranches=tranches,
        closing_cash=cash_after,
        acquisition_spend=event.uses,
        acquisition_from_cash=from_cash,
    )
    return updated, AddOnOutcome(
        event=event,
        index=index,
        cash_before=row.closing_cash,
        cash_after=cash_after,
        leverage_before=before,
        leverage_after=_leverage(updated.closing_debt, cash_after, earnings),
    )


def _recapitalise(
    structure: CapitalStructure,
    row: DebtPeriod,
    event: Recapitalisation,
    *,
    index: int,
    earnings: Money | None,
) -> tuple[DebtPeriod, RecapitalisationOutcome]:
    """Apply a recapitalisation at the end of a solved period.

    Placed after the period rather than inside it, which is what makes the event
    cost a period's less interest than a mid-period raise would. The new balance
    is what the following period opens on, so everything downstream — interest,
    the sweep, the covenant tests — picks it up without being told about the
    event at all.

    Cash taken off the balance sheet is refused rather than clamped when it would
    leave the structure below the minimum it is required to hold. A model that
    quietly paid a smaller dividend than the file described would answer a
    question nobody asked.
    """
    available = row.closing_cash - structure.minimum_cash
    if event.from_cash > available:
        raise RecapitalisationError(
            f"{event.label}: takes {event.from_cash} off a balance sheet holding "
            f"{row.closing_cash} against a minimum of {structure.minimum_cash}, "
            f"which leaves only {available} to pay out"
        )

    before = _leverage(row.closing_debt, row.closing_cash, earnings)
    tranches = tuple(
        dataclasses.replace(
            t,
            recapitalisation=event.draw_on(t.name),
            closing=t.closing + event.draw_on(t.name),
        )
        for t in row.tranches
    )
    cash_after = row.closing_cash - event.from_cash
    updated = dataclasses.replace(
        row,
        tranches=tranches,
        closing_cash=cash_after,
        distribution=event.distribution,
        distribution_from_cash=event.from_cash,
    )
    return updated, RecapitalisationOutcome(
        event=event,
        index=index,
        cash_before=row.closing_cash,
        cash_after=cash_after,
        leverage_before=before,
        leverage_after=_leverage(updated.closing_debt, cash_after, earnings),
    )


def _leverage(debt: Money, cash: Money, earnings: Money | None) -> Money | None:
    if earnings is None or earnings <= 0:
        return None
    return (debt - cash) / earnings


@dataclass(frozen=True, slots=True)
class DebtSchedule:
    """The capital structure rolled forward across a projection."""

    structure: CapitalStructure
    periods: tuple[DebtPeriod, ...]
    opening_cash: Money
    recapitalisations: tuple[RecapitalisationOutcome, ...] = ()
    acquisitions: tuple[AddOnOutcome, ...] = ()
    refinancings: tuple[RefinancingOutcome, ...] = ()

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
        recapitalisations: Sequence[Recapitalisation] = (),
        acquisitions: Sequence[AddOn] = (),
        refinancings: Sequence[Refinancing] = (),
        write_offs: dict[int, Money] | None = None,
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

        ``write_offs`` supplies the unamortised capitalised cost on the paper a
        refinancing takes out, keyed by the period the takeout lands in, for
        events that did not state one. The schedule does not derive it — the
        balance belongs to the funding table, one layer up, and a schedule that
        reached for it would have to know how the paper was placed as well as
        what it costs to carry. See :mod:`capstack.fees`.
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

        # Events are described in period numbers, and a grid opening on a stub
        # has one fewer whole period than it has columns. Validating against the
        # column count would accept an event numbered past the last period there
        # is, and it would then simply never fire.
        span = max(p.index for p in periods)
        scheduled = _by_period(recapitalisations or (), structure, span)
        purchases = _acquisitions_by_period(
            acquisitions or (), structure, span, scheduled
        )
        write_offs = dict(write_offs or {})
        takeouts = _refinancings_by_period(
            refinancings or (),
            structure,
            span,
            frozenset(scheduled) | frozenset(purchases),
        )

        balances = {t.name: t.face for t in structure.tranches}
        basis = AmortisationBasis(structure)
        cash = money(opening_cash)
        if cash < 0:
            raise ValueError("opening cash must not be negative")

        rows: list[DebtPeriod] = []
        applied_events: list[RecapitalisationOutcome] = []
        applied_purchases: list[AddOnOutcome] = []
        applied_takeouts: list[RefinancingOutcome] = []
        for i, period in enumerate(periods):
            certified: Money | None = None
            rate = structure.sweep_rate
            if grid is not None and ebitda is not None:
                # The certificate for the period just ended: debt at the start
                # of this period against the earnings of the one before it. A
                # stub does not certify — six weeks of earnings is not the
                # twelve months a compliance certificate is struck on — so the
                # period after a stub falls back to the level the deal was
                # priced at, exactly as the first period does.
                # A date certifies only when a full year stands behind it, so
                # the first three quarters of a quarterly grid fall back to the
                # level the deal was priced at exactly as a stub does. Stepping
                # a sweep on a part-year would pin the structure at its top rate
                # for the whole of the first year on a leverage level that never
                # existed.
                previous = periods[i - 1] if i > 0 else None
                certifies = (
                    previous is not None
                    and not previous.is_stub
                    and trailing_window(periods, i - 1).complete
                )
                earnings = (
                    money(ebitda[i - 1])
                    if certifies
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
                basis=basis.snapshot(),
                unavailable=basis.retired,
                amortisation_scale=_amortisation_scale(periods, i, structure.day_count),
                certified_leverage=certified,
            )
            event = scheduled.get(period.index)
            if event is not None:
                row, applied = _recapitalise(
                    structure,
                    row,
                    event,
                    index=i,
                    earnings=money(ebitda[i]) if ebitda is not None else None,
                )
                applied_events.append(applied)

            purchase = purchases.get(period.index)
            if purchase is not None:
                row, bought = _acquire(
                    structure,
                    row,
                    purchase,
                    index=i,
                    earnings=money(ebitda[i]) if ebitda is not None else None,
                )
                applied_purchases.append(bought)

            takeout = takeouts.get(period.index)
            if takeout is not None:
                row, retired = _refinance(
                    structure,
                    row,
                    takeout,
                    index=i,
                    span=span,
                    unamortised_fees=money(write_offs.get(takeout.period, ZERO)),
                )
                applied_takeouts.append(retired)

            rows.append(row)
            basis.apply(row)
            balances = {t.name: t.closing for t in row.tranches}
            cash = row.closing_cash

        return cls(
            structure=structure,
            periods=tuple(rows),
            opening_cash=money(opening_cash),
            recapitalisations=tuple(applied_events),
            acquisitions=tuple(applied_purchases),
            refinancings=tuple(applied_takeouts),
        )

    @classmethod
    def from_operating_model(
        cls,
        structure: CapitalStructure,
        model: OperatingModel,
        *,
        opening_cash: Numeric = 0,
        opening_ebitda: Numeric | None = None,
        recapitalisations: Sequence[Recapitalisation] = (),
        acquisitions: Sequence[AddOn] = (),
        refinancings: Sequence[Refinancing] = (),
        write_offs: dict[int, Money] | None = None,
    ) -> DebtSchedule:
        """Run the structure against an operating case."""
        return cls.run(
            structure,
            [p.period for p in model],
            [p.unlevered_free_cash_flow for p in model],
            opening_cash=opening_cash,
            # The trailing year at each date rather than the period, because a
            # sweep grid steps on leverage and leverage is a year of earnings.
            ebitda=[model.trailing_ebitda(i) for i in range(len(model))],
            # The level the deal was priced at, which is what the dates with no
            # year behind them fall back to. It has to be a year as well: the
            # first entry of the series is a trailing figure over a window that
            # is not yet complete, and on a quarterly grid falling back to it
            # would certify the opening structure at four times its leverage.
            opening_ebitda=(
                opening_ebitda if opening_ebitda is not None else model.entry_ebitda
            ),
            recapitalisations=recapitalisations,
            acquisitions=acquisitions,
            refinancings=refinancings,
            write_offs=write_offs,
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
    def total_recapitalised(self) -> Money:
        """Incremental face raised across the hold, outside the revolver."""
        return sum((p.recapitalisation for p in self.periods), ZERO)

    @property
    def total_acquisition_debt(self) -> Money:
        """Incremental face raised to buy things, outside the revolver."""
        return sum((p.acquisition_debt for p in self.periods), ZERO)

    @property
    def total_acquisition_spend(self) -> Money:
        """Everything the acquisitions cost, however it was funded."""
        return sum((p.acquisition_spend for p in self.periods), ZERO)

    @property
    def total_acquisition_from_cash(self) -> Money:
        """The share of that spend the business paid for out of its own cash."""
        return sum((p.acquisition_from_cash for p in self.periods), ZERO)

    @property
    def total_refinanced(self) -> Money:
        """Face retired early across the hold."""
        return sum((p.refinanced for p in self.periods), ZERO)

    @property
    def total_call_premiums(self) -> Money:
        return sum((p.call_premium for p in self.periods), ZERO)

    @property
    def total_fees_written_off(self) -> Money:
        """Non-cash, and reported apart from everything that is not."""
        return sum((p.fees_written_off for p in self.periods), ZERO)

    @property
    def total_distributed(self) -> Money:
        """Cash paid out to the equity during the hold."""
        return sum((p.distribution for p in self.periods), ZERO)

    def distributions(self) -> tuple[tuple[Period, Money], ...]:
        """Every interim distribution, with the date it was paid.

        The dates are what make this useful rather than the amounts: an interim
        distribution is worth what it is worth because of when it arrives, and a
        return measured without the date is a return that cannot see the point
        of the exercise.
        """
        return tuple(
            (p.period, p.distribution) for p in self.periods if p.distribution > 0
        )

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


def _amortisation_scale(
    periods: Sequence[Period], index: int, day_count: DayCount
) -> Money:
    """How much of a year's instalment a period of this length owes.

    A credit agreement writes amortisation per year — 1% a year on a term loan —
    so a quarter owes a quarter of it. Charging the whole annual instalment in
    every period would repay 1% four times a year on a quarterly grid, and the
    schedule would retire the paper four times as fast as it is written to
    retire, on the side of the model where interest is already accrued on days.

    A stub owes the fraction of a whole period it covers, and through it the
    same share of the year. Measuring the first part against the first whole
    period on the grid rather than against a nominal year is what lets a stub
    sit in front of a quarterly grid without a second special case.

    An annual grid is unchanged: one whole period a year owes the whole of it.
    """
    period = periods[index]
    share = ONE / Money(period.periods_per_year)
    if not period.is_stub:
        return share
    whole = next((p for p in periods if not p.is_stub), None)
    if whole is None:
        return share
    return share * safe_div(
        period.year_fraction(day_count), whole.year_fraction(day_count), default=ONE
    )


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
    basis: dict[str, Money] | None = None,
    unavailable: frozenset[str] = frozenset(),
    amortisation_scale: Money = ONE,
    certified_leverage: Money | None = None,
) -> DebtPeriod:
    """Run a period once, taking ``guess`` as the closing balances interest accrues on.

    Under :attr:`InterestBasis.OPENING` the guess is ignored and the pass is the
    answer. Under ``AVERAGE`` it is one step of the iteration that finds the
    balances consistent with the interest they generate.

    ``amortisation_scale`` shortens the contractual instalment for a period
    shorter than a whole one. Interest needs no such help — it accrues on a day
    count and prorates itself — but an instalment written as 1% a year is not 1%
    in a forty-six-day stub, and nothing in the amortisation driver knows how
    long the period it is being read for actually is.
    """
    year_fraction = period.year_fraction(structure.day_count)
    driver_index = period.driver_index
    base = structure.base_at(driver_index)
    half = money(2)

    accrual_base: dict[str, Money] = {}
    cash_interest: dict[str, Money] = {}
    pik_interest: dict[str, Money] = {}
    undrawn_fee: dict[str, Money] = {}
    mandatory: dict[str, Money] = {}

    for tranche in structure:
        start = opening[tranche.name]
        matured = tranche.matured_at(period.index)
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
        # A revolver's spare capacity is measured against the balance
        # outstanding, because repaying it restores the capacity. A term
        # commitment is measured against the face taken down since close, which
        # is what the amortisation basis has been carrying all along — repaying
        # a delayed-draw loan does not entitle anyone to draw it again, and
        # charging its ticking fee on a falling balance would bill the borrower
        # more for the facility the more of it they repaid.
        if tranche.is_revolving:
            taken = accrual
        elif basis is not None:
            taken = basis[tranche.name]
        else:
            taken = tranche.face
        if tranche.name in unavailable:
            undrawn = ZERO
        else:
            undrawn = max(tranche.undrawn_at(taken, period=period.index), ZERO)
        undrawn_fee[tranche.name] = tranche.undrawn_fee * year_fraction * undrawn

        owed = start + pik_interest[tranche.name]
        if matured:
            due = owed
        else:
            face = tranche.face if basis is None else basis[tranche.name]
            instalment = tranche.scheduled_amortisation(driver_index, face)
            due = min(instalment * amortisation_scale, owed)
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
            if t.matured_at(period.index)
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
            amortisation_basis=t.face if basis is None else basis[t.name],
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
    basis: dict[str, Money] | None = None,
    unavailable: frozenset[str] = frozenset(),
    amortisation_scale: Money = ONE,
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
            basis=basis,
            unavailable=unavailable,
            amortisation_scale=amortisation_scale,
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
            basis=basis,
            unavailable=unavailable,
            amortisation_scale=amortisation_scale,
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
