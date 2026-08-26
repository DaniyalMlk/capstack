"""The exit: what the equity is worth, who gets it, and where it came from.

Every layer below this one describes the hold. This one answers the question the
deal was done to answer, and it answers it three times over, because a return
that is not decomposed is a number nobody can argue with or learn from.

*What the business sells for.* An EBITDA multiple applied to the final projected
period, less the net debt the schedule leaves behind, less the cost of selling.
The multiple defaults to the entry multiple, because assuming expansion is
assuming the answer: a case that only works at a higher exit multiple is a case
that only works if somebody else pays more than the sponsor did.

*Who receives it.* A sponsor cheque is rarely one instrument. Preferred with a
compounding coupon sitting ahead of common is the ordinary shape, and management
rollover sits in the common beside it. That structure is the point rather than a
detail: the preferred takes its accrued return off the top and the common takes
the leverage on whatever is left, so the two report very different multiples on
the same exit. Reporting one blended figure for the equity hides the only
distinction the structure was built to create.

*Where it came from.* Three sources: the business earned more, the market paid a
different multiple for it, and the debt was repaid out of cash flow. The bridge
has to tie to the actual change in equity value, so there is a fourth line for
what the first three do not explain — entry and exit costs — rather than a
rounding difference left on the page for the reader to notice.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import NamedTuple

from .daycount import DayCount, year_fraction
from .debt import DebtSchedule
from .incentive import IncentiveError, OptionPool, PoolOutcome, Ratchet, settle_pool
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
from .returns import CashFlow, CashFlowStream, IRRError
from .transaction import Transaction

__all__ = [
    "Attribution",
    "Distribution",
    "ExitValuation",
    "Outcome",
    "Security",
    "SecurityKind",
    "SecurityOutcome",
    "default_securities",
]


class SecurityKind(Enum):
    """Where an instrument sits in the equity waterfall.

    ``PREFERRED`` is repaid its capital and its accrued return before anything
    reaches the common. ``COMMON`` takes what is left, which is why it carries
    the leverage in both directions.
    """

    PREFERRED = "preferred"
    COMMON = "common"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Security:
    """One instrument in the equity, and the terms it is held on.

    ``ownership`` is the share of the residual — what is left after every
    preferred claim has been met. A non-participating preferred holds none of
    it; a participating one holds both its claim and a share of the residual,
    which is the term a sponsor asks for and a management team resists.

    ``preferred_rate`` accrues on the capital invested. It compounds by default,
    because a preferred return that does not compound is worth materially less
    over a five-year hold and the documents almost always say it does.
    """

    name: str
    kind: SecurityKind
    invested: Money
    ownership: Money = ZERO
    preferred_rate: Money = ZERO
    compounding: bool = True
    seniority: int = 0

    @classmethod
    def of(
        cls,
        name: str,
        kind: SecurityKind,
        invested: Numeric,
        *,
        ownership: Numeric = 0,
        preferred_rate: Numeric = 0,
        compounding: bool = True,
        seniority: int = 0,
    ) -> Security:
        return cls(
            name=name,
            kind=kind,
            invested=money(invested),
            ownership=money(ownership),
            preferred_rate=money(preferred_rate),
            compounding=compounding,
            seniority=seniority,
        )

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("a security needs a name")
        if self.invested < 0:
            raise ValueError(f"{self.name}: capital invested must not be negative")
        if not (0 <= self.ownership <= 1):
            raise ValueError(f"{self.name}: ownership is a share, so between 0 and 1")
        if self.preferred_rate < 0:
            raise ValueError(f"{self.name}: the preferred return must not be negative")
        if self.preferred_rate and self.kind is not SecurityKind.PREFERRED:
            raise ValueError(
                f"{self.name}: a preferred return accrues on preferred capital, and "
                f"this is described as {self.kind}"
            )
        if self.seniority < 0:
            raise ValueError(f"{self.name}: seniority must not be negative")

    @property
    def participates(self) -> bool:
        """Whether this instrument shares in the residual as well as its claim."""
        return self.ownership > 0

    def accrued_at(self, years: Money) -> Money:
        """The preferred return accumulated over ``years``, excluding capital.

        Compounding uses the exact elapsed year fraction rather than a whole
        number of periods, so a deal exited eleven days late is worth eleven
        days more to the preferred, which is what the document says.
        """
        return self.accrue(
            capital=self.invested, accrued=ZERO, years=years
        )

    def accrue(self, *, capital: Money, accrued: Money, years: Money) -> Money:
        """What accrues over ``years`` on a claim standing at ``capital`` plus ``accrued``.

        Split out from :meth:`accrued_at` because a claim does not always start
        from the original cheque. A preferred paid down part-way through a hold
        accrues afterwards on what is left, and charging its coupon on money it
        no longer holds would overstate its claim at exit and understate
        everything ranking behind it.

        Compounding accrues on the whole standing claim; a non-compounding
        preferred accrues on capital alone, which is what makes the distinction
        worth several points over a five-year hold.
        """
        if self.kind is not SecurityKind.PREFERRED or self.preferred_rate == 0:
            return ZERO
        if years <= 0:
            return ZERO
        if not self.compounding:
            return capital * self.preferred_rate * years
        # Decimal has no fractional power, so the growth factor is computed in
        # floating point and returned to exact arithmetic immediately. The
        # boundary is deliberate and the same one the IRR solver crosses.
        factor = money(repr(float(ONE + self.preferred_rate) ** float(years)))
        return (capital + accrued) * (factor - ONE)

    def claim_at(self, years: Money) -> Money:
        """Capital plus accrued return: what must be paid before the common."""
        if self.kind is not SecurityKind.PREFERRED:
            return ZERO
        return self.invested + self.accrued_at(years)


def default_securities(transaction: Transaction) -> tuple[Security, ...]:
    """The plain structure: sponsor and rollover common, split by ownership.

    What a deal file describes when it says nothing about the equity. Both
    holders share the residual in proportion to what they put in, which is the
    arrangement any other structure is a departure from.
    """
    sponsor = max(transaction.sponsor_equity, ZERO)
    rollover = transaction.rollover_equity
    total = sponsor + rollover
    if total <= 0:
        raise ValueError(
            "this deal has no equity in it, so there is nothing to hold or to exit"
        )
    securities = [
        Security.of(
            "Sponsor equity",
            SecurityKind.COMMON,
            sponsor,
            ownership=safe_div(sponsor, total, default=ZERO),
        )
    ]
    if rollover > 0:
        securities.append(
            Security.of(
                "Rollover equity",
                SecurityKind.COMMON,
                rollover,
                ownership=safe_div(rollover, total, default=ZERO),
            )
        )
    else:
        # One holder takes the whole residual; the division above would round
        # to one anyway, but stating it avoids a share of 0.9999... on a deal
        # where the arithmetic does not terminate.
        securities[0] = Security.of(
            "Sponsor equity", SecurityKind.COMMON, sponsor, ownership=1
        )
    return tuple(securities)


@dataclass(frozen=True, slots=True)
class ExitValuation:
    """What the business is sold for, and what is left after the debt.

    ``fee_rate`` is charged on enterprise value and covers the cost of running
    a sale. It is small and it is not zero, and leaving it out flatters every
    return in the model by roughly the same amount.
    """

    when: date
    ebitda: Money
    multiple: Money
    debt: Money
    cash: Money
    fee_rate: Money = ZERO

    @classmethod
    def of(
        cls,
        when: date,
        ebitda: Numeric,
        multiple: Numeric,
        *,
        debt: Numeric = 0,
        cash: Numeric = 0,
        fee_rate: Numeric = 0,
    ) -> ExitValuation:
        return cls(
            when=when,
            ebitda=money(ebitda),
            multiple=money(multiple),
            debt=money(debt),
            cash=money(cash),
            fee_rate=money(fee_rate),
        )

    def __post_init__(self) -> None:
        if self.multiple <= 0:
            raise ValueError("the exit multiple must be positive")
        if self.debt < 0:
            raise ValueError("debt outstanding at exit must not be negative")
        if self.cash < 0:
            raise ValueError("cash at exit must not be negative")
        if self.fee_rate < 0:
            raise ValueError("the exit fee rate must not be negative")

    @property
    def enterprise_value(self) -> Money:
        """Negative EBITDA gives a negative enterprise value, which is honest.

        A business losing money is not worth a multiple of its losses in any
        real sale, but clamping it to zero would report an exit the case does
        not support. The equity value below is floored instead, at the point
        where the number stops being a valuation and becomes a distribution.
        """
        return self.ebitda * self.multiple

    @property
    def fees(self) -> Money:
        return max(self.enterprise_value, ZERO) * self.fee_rate

    @property
    def net_debt(self) -> Money:
        return self.debt - self.cash

    @property
    def gross_equity_value(self) -> Money:
        """Before flooring: what the arithmetic says, sign and all."""
        return self.enterprise_value - self.net_debt - self.fees

    @property
    def equity_value(self) -> Money:
        """What there is to distribute.

        Floored at zero. Equity in a leveraged company is an option: the holders
        can walk away, and a business worth less than its debt hands the keys to
        the lenders rather than sending its shareholders a bill.
        """
        return max(self.gross_equity_value, ZERO)

    @property
    def is_wiped_out(self) -> bool:
        return self.gross_equity_value <= 0

    @property
    def exit_leverage(self) -> Money:
        return safe_div(self.net_debt, self.ebitda, default=ZERO)


@dataclass(frozen=True, slots=True)
class SecurityOutcome:
    """What one instrument received, and what that was worth to it."""

    security: Security
    accrued: Money
    preferred_paid: Money
    residual_paid: Money
    irr: float | None
    irr_note: str = ""
    interim: Money = ZERO
    standing_capital: Money = ZERO
    unpaid_capital: Money = ZERO

    @property
    def name(self) -> str:
        return self.security.name

    @property
    def invested(self) -> Money:
        return self.security.invested

    @property
    def proceeds(self) -> Money:
        """What arrived at the exit."""
        return self.preferred_paid + self.residual_paid

    @property
    def received(self) -> Money:
        """Everything that came back, whenever it came back."""
        return self.proceeds + self.interim

    @property
    def profit(self) -> Money:
        return self.received - self.invested

    @property
    def moic(self) -> Money | None:
        """Everything received over capital. ``None`` where no capital was put in.

        Counts interim distributions, because a money multiple is a question
        about totals and not about timing. The rate of return beside it is the
        measure that knows when anything happened, and the gap between the two
        is precisely what a recapitalisation creates.
        """
        if self.invested <= 0:
            return None
        return self.received / self.invested

    @property
    def claim(self) -> Money:
        """What was owed at the exit: capital still outstanding plus accrued return.

        For a preferred paid down during the hold this is smaller than the
        original cheque plus a whole hold's coupon, which is the point of
        paying it down.
        """
        if self.security.kind is not SecurityKind.PREFERRED:
            return ZERO
        return self.standing_capital + self.accrued

    @property
    def capital_repaid_early(self) -> Money:
        """Capital met by an interim distribution rather than at the exit."""
        if self.security.kind is not SecurityKind.PREFERRED:
            return ZERO
        return self.invested - self.standing_capital

    @property
    def shortfall(self) -> Money:
        """Preferred claim left unpaid, which is a real entitlement that went unmet."""
        return max(self.claim - self.preferred_paid, ZERO)


@dataclass(frozen=True, slots=True)
class Attribution:
    """Where the change in equity value came from.

    The convention is stated rather than assumed, because the cross term has to
    go somewhere. Earnings growth is valued at the *entry* multiple, and the
    multiple change is applied to *exit* EBITDA, which puts the interaction
    between the two — the extra turns earned on the extra EBITDA — in the
    multiple line. The alternative convention flatters growth in exactly the
    deals where the multiple expanded, which is to say the ones where the
    sponsor least wants to be asked about it.
    """

    ebitda_growth: Money
    multiple_change: Money
    debt_paydown: Money
    costs: Money
    invested: Money
    realised: Money
    floored: Money = ZERO

    @property
    def total(self) -> Money:
        return self.ebitda_growth + self.multiple_change + self.debt_paydown + self.costs

    @property
    def value_created(self) -> Money:
        return self.realised - self.invested

    @property
    def distributed(self) -> Money:
        """What the equity actually receives, after the limited-liability floor.

        Below zero the shareholders hand the keys to the lenders rather than
        writing a cheque, so the bridge and the distribution part company. The
        difference is reported rather than smuggled into the costs line.
        """
        return self.realised + self.floored

    def reconciles(self, tolerance: Numeric = "1E-12") -> bool:
        """Whether the four components explain the change in value exactly."""
        return is_close(self.total, self.value_created, tolerance=tolerance)

    def share(self, component: Money) -> Money:
        """One component as a share of the gross movement.

        Measured against the sum of the absolute components rather than against
        the net, so a bridge with an offsetting multiple contraction still
        reports shares that mean something instead of exceeding one.
        """
        gross = (
            abs(self.ebitda_growth)
            + abs(self.multiple_change)
            + abs(self.debt_paydown)
            + abs(self.costs)
        )
        return safe_div(component, gross, default=ZERO)


@dataclass(frozen=True, slots=True)
class Distribution:
    """One payment to the equity, and how the waterfall split it.

    Interim distributions and the exit are the same object because they are the
    same event: cash arrives, the preferred take their claim, the rest goes to
    the common. What differs is that the exit is the last one and the incentive
    plan settles against it.
    """

    when: date
    years: Money
    amount: Money
    label: str
    preferred_paid: tuple[Money, ...]
    residual_paid: tuple[Money, ...]
    accrued: tuple[Money, ...]
    capital: tuple[Money, ...]
    incentive: PoolOutcome | None = None
    is_exit: bool = False

    @property
    def to_preferred(self) -> Money:
        return sum(self.preferred_paid, ZERO)

    @property
    def to_common(self) -> Money:
        return sum(self.residual_paid, ZERO)

    @property
    def to_incentive(self) -> Money:
        return ZERO if self.incentive is None else self.incentive.paid

    @property
    def paid(self) -> Money:
        """Everything the payment was split into."""
        return self.to_preferred + self.to_common + self.to_incentive

    def reconciles(self, tolerance: Numeric = "1E-12") -> bool:
        """Whether the split accounts for the whole payment."""
        return is_close(self.paid, self.amount, tolerance=tolerance)

    def to(self, index: int) -> Money:
        return self.preferred_paid[index] + self.residual_paid[index]


class _Ledger:
    """The equity's standing claims, rolled forward through a series of payments.

    Held as capital and accrued separately rather than as one claim, because a
    payment to a preferred instrument is applied to the accrued return before it
    touches capital. That ordering is what makes "how much capital is still
    outstanding" a well-defined question after a partial repayment, and the
    answer is what the next period's coupon is charged on.
    """

    def __init__(self, securities: Sequence[Security]) -> None:
        self.securities = tuple(securities)
        self.capital = [
            s.invested if s.kind is SecurityKind.PREFERRED else ZERO
            for s in self.securities
        ]
        self.accrued = [ZERO for _ in self.securities]
        self.received = [ZERO for _ in self.securities]
        self.as_of = ZERO

    def _roll(self, years: Money) -> None:
        """Bring every claim up to ``years``."""
        elapsed = years - self.as_of
        if elapsed <= 0:
            return
        for i, security in enumerate(self.securities):
            self.accrued[i] += security.accrue(
                capital=self.capital[i], accrued=self.accrued[i], years=elapsed
            )
        self.as_of = years

    def _settle_preferred(self, pot: Money) -> tuple[list[Money], Money]:
        """Pay the preferred claims out of ``pot``, most senior class first."""
        paid = [ZERO for _ in self.securities]
        remaining = pot
        ranks = sorted(
            {
                s.seniority
                for s in self.securities
                if s.kind is SecurityKind.PREFERRED
            }
        )
        for rank in ranks:
            if remaining <= 0:
                break
            members = [
                i
                for i, s in enumerate(self.securities)
                if s.kind is SecurityKind.PREFERRED and s.seniority == rank
            ]
            claims = [self.capital[i] + self.accrued[i] for i in members]
            for i, amount in zip(members, allocate_pro_rata(remaining, claims)):
                paid[i] = amount
                remaining -= amount
                # Accrued return first, then capital. The remaining capital is
                # what the coupon runs on from here.
                to_accrued = min(amount, self.accrued[i])
                self.accrued[i] -= to_accrued
                self.capital[i] -= amount - to_accrued
        return paid, max(remaining, ZERO)

    def pay(
        self,
        amount: Money,
        *,
        when: date,
        years: Money,
        label: str,
        pool: OptionPool | None = None,
        is_exit: bool = False,
    ) -> Distribution:
        """Run one payment through the waterfall and record what it did."""
        self._roll(years)
        # Snapshotted before the payment: the claim as it stood when the money
        # arrived is what the payment was measured against.
        standing_accrued = tuple(self.accrued)
        standing_capital = tuple(self.capital)
        preferred_paid, residual = self._settle_preferred(amount)

        settled: PoolOutcome | None = None
        if pool is not None:
            watched = (
                _measured(self.securities, pool.ratchet)
                if pool.ratchet is not None
                else []
            )
            settled = settle_pool(
                pool,
                residual,
                years=years,
                measured_capital=sum(
                    (self.securities[i].invested for i in watched), ZERO
                ),
                # Everything the watched instruments have taken so far, this
                # payment's preferred claim included. A money multiple counts
                # what a holder has received, not only what arrives last.
                measured_prior=sum(
                    (self.received[i] + preferred_paid[i] for i in watched), ZERO
                ),
                measured_ownership=sum(
                    (self.securities[i].ownership for i in watched), ZERO
                ),
            )
            residual = settled.to_common

        shares = [s.ownership for s in self.securities]
        residual_paid = allocate_pro_rata(residual, [residual * s for s in shares])
        for i in range(len(self.securities)):
            self.received[i] += preferred_paid[i] + residual_paid[i]

        return Distribution(
            when=when,
            years=years,
            amount=amount,
            label=label,
            preferred_paid=tuple(preferred_paid),
            residual_paid=tuple(residual_paid),
            accrued=standing_accrued,
            capital=standing_capital,
            incentive=settled,
            is_exit=is_exit,
        )


def _measured(securities: Sequence[Security], ratchet: Ratchet) -> list[int]:
    """The securities a ratchet's hurdles are read against.

    Naming nothing means the equity as a whole, which is the right reading when
    the sponsor is the only institutional holder and the wrong one the moment a
    rollover sits beside it. Naming an instrument that is not in the stack is an
    error rather than an empty selection: a plan measured on nothing would clear
    no hurdle at any price, and would do it silently.
    """
    if not ratchet.measured_on:
        return list(range(len(securities)))
    by_name = {s.name: i for i, s in enumerate(securities)}
    missing = [name for name in ratchet.measured_on if name not in by_name]
    if missing:
        raise IncentiveError(
            f"the ratchet is measured on {', '.join(repr(m) for m in missing)}, "
            f"which {'is' if len(missing) == 1 else 'are'} not in the equity; the "
            f"stack holds {', '.join(repr(s.name) for s in securities)}"
        )
    return [by_name[name] for name in ratchet.measured_on]


@dataclass(frozen=True, slots=True)
class Outcome:
    """The exit, the waterfall through it, and the attribution behind it."""

    valuation: ExitValuation
    entry_date: date
    securities: tuple[SecurityOutcome, ...]
    attribution: Attribution
    convention: DayCount = DayCount.ACT_365F
    incentive: PoolOutcome | None = None
    payments: tuple[Distribution, ...] = ()

    def __len__(self) -> int:
        return len(self.securities)

    def __iter__(self) -> Iterator[SecurityOutcome]:
        return iter(self.securities)

    def security(self, name: str) -> SecurityOutcome:
        for row in self.securities:
            if row.name == name:
                return row
        raise KeyError(f"no security named {name!r}")

    @classmethod
    def realise(
        cls,
        transaction: Transaction,
        model: OperatingModel,
        schedule: DebtSchedule,
        *,
        entry_date: date,
        exit_multiple: Numeric | None = None,
        exit_fee_rate: Numeric = 0,
        securities: Sequence[Security] | None = None,
        incentive: OptionPool | None = None,
        convention: DayCount = DayCount.ACT_365F,
    ) -> Outcome:
        """Value the exit and run the equity through it.

        ``exit_multiple`` defaults to the entry multiple. A flat-multiple case is
        the one a sponsor has to be able to defend, and it is the only one that
        does not borrow from the buyer on the way out.

        ``incentive`` is the management plan. Where there is one, the returns
        reported per security are net of it, which is the only version of them
        worth quoting: a sponsor multiple that ignores the pool is a multiple on
        money somebody else is going to receive.
        """
        if len(schedule) != len(model):
            raise ValueError(
                f"the schedule covers {len(schedule)} periods and the operating "
                f"case {len(model)}"
            )
        holders = tuple(
            securities if securities is not None else default_securities(transaction)
        )
        _validate(holders)

        last = schedule[-1]
        valuation = ExitValuation(
            when=last.period.end,
            ebitda=model.exit_ebitda,
            multiple=(
                money(exit_multiple)
                if exit_multiple is not None
                else transaction.valuation.entry_multiple
            ),
            debt=last.closing_debt,
            cash=last.closing_cash,
            fee_rate=money(exit_fee_rate),
        )
        if valuation.when <= entry_date:
            raise ValueError(
                f"the exit ({valuation.when}) is not after the close ({entry_date})"
            )

        ledger = _Ledger(holders)
        interim: list[Distribution] = []
        for period, amount in schedule.distributions():
            if period.end >= valuation.when:
                # A distribution at the exit date is part of the exit, not a
                # payment before it; folding it in twice would pay it twice.
                continue
            interim.append(
                ledger.pay(
                    amount,
                    when=period.end,
                    years=year_fraction(entry_date, period.end, convention),
                    label=f"Distribution, period {period.index}",
                )
            )

        years = year_fraction(entry_date, valuation.when, convention)
        final = ledger.pay(
            valuation.equity_value,
            when=valuation.when,
            years=years,
            label="Exit proceeds",
            pool=incentive,
            is_exit=True,
        )
        payments = tuple(interim) + (final,)

        rows = []
        for i, holder in enumerate(holders):
            rate = _rate(
                holder.invested,
                [(p.when, p.to(i)) for p in payments],
                entry_date,
                convention,
            )
            rows.append(
                SecurityOutcome(
                    security=holder,
                    accrued=final.accrued[i],
                    preferred_paid=final.preferred_paid[i],
                    residual_paid=final.residual_paid[i],
                    interim=sum((p.to(i) for p in interim), ZERO),
                    standing_capital=final.capital[i],
                    unpaid_capital=ledger.capital[i],
                    irr=rate.value,
                    irr_note=rate.note,
                )
            )

        return cls(
            valuation=valuation,
            entry_date=entry_date,
            securities=tuple(rows),
            attribution=_attribute(transaction, model, schedule, valuation, holders),
            convention=convention,
            incentive=final.incentive,
            payments=payments,
        )

    # -- Deal-level figures ----------------------------------------------

    @property
    def invested(self) -> Money:
        return sum((r.invested for r in self.securities), ZERO)

    @property
    def proceeds(self) -> Money:
        """What the securities took at the exit."""
        return sum((r.proceeds for r in self.securities), ZERO)

    @property
    def interim(self) -> Money:
        """What the securities took before the exit."""
        return sum((r.interim for r in self.securities), ZERO)

    @property
    def received(self) -> Money:
        return self.proceeds + self.interim

    @property
    def distributions(self) -> tuple[Distribution, ...]:
        """The payments before the exit, in the order they were made."""
        return tuple(p for p in self.payments if not p.is_exit)

    @property
    def was_recapitalised(self) -> bool:
        return bool(self.distributions)

    @property
    def profit(self) -> Money:
        return self.received - self.invested

    @property
    def moic(self) -> Money | None:
        if self.invested <= 0:
            return None
        return self.received / self.invested

    @property
    def holding_period_years(self) -> Money:
        return year_fraction(self.entry_date, self.valuation.when, self.convention)

    @property
    def irr(self) -> float | None:
        """The deal-level rate, across all equity taken together."""
        receipts = [
            (p.when, sum(p.preferred_paid, ZERO) + sum(p.residual_paid, ZERO))
            for p in self.payments
        ]
        return _rate(self.invested, receipts, self.entry_date, self.convention).value

    @property
    def incentive_paid(self) -> Money:
        """What the management plan took, net of the cost of exercising."""
        return ZERO if self.incentive is None else self.incentive.paid

    @property
    def distributed(self) -> Money:
        """Everything the equity value was split into, management included.

        The strike does not appear because it cancels: management pay it in and
        it is divided along with everything else, so the pot the holders share
        is larger by exactly what the plan contributed to it.
        """
        return self.proceeds + self.incentive_paid

    @property
    def distributes_everything(self) -> bool:
        """Whether the waterfall paid out exactly what there was to pay out."""
        return is_close(self.distributed, self.valuation.equity_value, tolerance="1E-12")


def _validate(securities: Sequence[Security]) -> None:
    if not securities:
        raise ValueError("the equity needs at least one security in it")
    names = [s.name for s in securities]
    if len(names) != len(set(names)):
        raise ValueError("security names must be distinct")
    ownership = sum((s.ownership for s in securities), ZERO)
    if not is_close(ownership, ONE, tolerance="1E-9"):
        raise ValueError(
            f"the residual must be fully owned: the shares given sum to {ownership}, "
            f"not to 1"
        )


class Rate(NamedTuple):
    """An annualised rate, or the reason there is not one.

    The reason travels with the missing number. A caller looking at ``None``
    should not have to reconstruct from the amounts whether the security was
    wiped out or was never funded in the first place.
    """

    value: float | None
    note: str = ""


def _rate(
    invested: Money,
    receipts: Sequence[tuple[date, Money]],
    entry: date,
    convention: DayCount,
) -> Rate:
    """The annualised rate on capital in and everything that came back.

    A dated stream rather than a pair, because an interim distribution is worth
    what it is worth only by virtue of arriving early. Every receipt after the
    first is positive, so the stream changes sign exactly once and the root is
    unique — but the solver is asked rather than assumed, because a structure
    that ever calls capital mid-hold would break that and should say so instead
    of reporting whichever root it happened to find.
    """
    if invested <= 0:
        return Rate(None, "no capital was invested")
    received = sum((amount for _, amount in receipts), ZERO)
    if received <= 0:
        return Rate(None, "wiped out: there is no rate at which nothing back is a return")
    flows = [CashFlow(when=entry, amount=-invested, label="invested")]
    flows.extend(
        CashFlow(when=when, amount=amount, label="received")
        for when, amount in receipts
        if amount != 0
    )
    try:
        return Rate(CashFlowStream(flows=tuple(flows), convention=convention).xirr())
    except IRRError as exc:  # pragma: no cover - one sign change always solves
        return Rate(None, str(exc))


def _attribute(
    transaction: Transaction,
    model: OperatingModel,
    schedule: DebtSchedule,
    valuation: ExitValuation,
    securities: Sequence[Security],
) -> Attribution:
    """Decompose the change in equity value into its three sources and a residual.

    Entry EBITDA is the LTM figure the deal was priced on rather than the first
    projected period, because that is the number the entry multiple was paid
    against. Entry net debt is what the structure funded less the cash it left
    on the balance sheet, so the paydown line measures the schedule's work and
    not the target's opening position.
    """
    entry_multiple = transaction.valuation.entry_multiple
    entry_ebitda = transaction.valuation.ltm_ebitda
    exit_ebitda = valuation.ebitda

    entry_net_debt = transaction.total_debt - schedule.opening_cash
    invested = sum((s.invested for s in securities), ZERO)

    growth = (exit_ebitda - entry_ebitda) * entry_multiple
    multiple = (valuation.multiple - entry_multiple) * exit_ebitda
    paydown = entry_net_debt - valuation.net_debt

    # Computed from the deal rather than taken as the plug, so that the four
    # components summing to the change in value is a real check on the bridge
    # instead of an identity that cannot fail. In the ordinary case this is
    # exactly the fees and issue discount the equity funded at close, plus the
    # cost of selling: the implied equity value at close is lower than the
    # cheque written for it by precisely the amount of those costs.
    entry_equity_value = entry_multiple * entry_ebitda - entry_net_debt
    costs = (entry_equity_value - invested) - valuation.fees

    return Attribution(
        ebitda_growth=growth,
        multiple_change=multiple,
        debt_paydown=paydown,
        costs=costs,
        invested=invested,
        # The bridge is drawn on what the arithmetic says the equity is worth.
        # Flooring it at zero is a legal fact about limited liability rather
        # than a source of value, so it is reported beside the bridge and not
        # inside it.
        realised=valuation.gross_equity_value,
        floored=valuation.equity_value - valuation.gross_equity_value,
    )
