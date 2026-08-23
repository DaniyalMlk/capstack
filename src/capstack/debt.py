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
from dataclasses import dataclass, field
from enum import Enum

from .daycount import DayCount
from .drivers import Driver
from .money import ONE, ZERO, Money, Numeric, money, safe_div

__all__ = [
    "CapitalStructure",
    "InterestBasis",
    "Tranche",
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

    def undrawn_at(self, drawn: Money) -> Money:
        """Commitment not currently drawn. Zero for anything but a revolver."""
        if not self.is_revolving:
            return ZERO
        return self.commitment - drawn


@dataclass(frozen=True, slots=True)
class CapitalStructure:
    """The whole stack, plus the rules that govern how cash moves through it.

    ``sweep_rate`` is the share of surplus cash applied to debt rather than left
    on the balance sheet. ``minimum_cash`` is the balance the business is left
    with before any sweep, which is an operating requirement rather than a
    financing one: a business cannot run its payroll out of a revolver it has to
    ask permission to draw.
    """

    tranches: tuple[Tranche, ...]
    minimum_cash: Money = ZERO
    sweep_rate: Money = ONE
    base_rate: Driver | None = None
    day_count: DayCount = DayCount.ACT_360
    interest_basis: InterestBasis = InterestBasis.AVERAGE
    damping: Money = field(default_factory=lambda: money("0.5"))
    tolerance: Money = field(default_factory=lambda: money("0.000001"))
    max_iterations: int = 100

    @classmethod
    def of(
        cls,
        tranches: Sequence[Tranche],
        *,
        minimum_cash: Numeric = 0,
        sweep_rate: Numeric = 1,
        base_rate: Driver | None = None,
        day_count: DayCount = DayCount.ACT_360,
        interest_basis: InterestBasis = InterestBasis.AVERAGE,
        damping: Numeric = "0.5",
        tolerance: Numeric = "0.000001",
        max_iterations: int = 100,
    ) -> CapitalStructure:
        return cls(
            tranches=tuple(tranches),
            minimum_cash=money(minimum_cash),
            sweep_rate=money(sweep_rate),
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
