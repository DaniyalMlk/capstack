"""Reading a deal from a file.

A deal is described as JSON rather than as command-line flags. There are already
more inputs than a flag list can carry legibly, and the operating case and debt
schedule will add more, so the file is the interface that scales.

Numbers are parsed straight to ``Decimal``. Going through ``float`` first would
reintroduce exactly the binary drift the money layer exists to avoid: a
``0.995`` issue price read as a float and multiplied by a face amount of four
hundred million lands a few hundredths away from where it should.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from collections.abc import Iterator, Sequence
from typing import Any

from dataclasses import dataclass

from .balance_sheet import OpeningBalanceSheet, PurchaseAccounting, TargetBookBalanceSheet
from .covenants import Covenant, CovenantReport, Measure
from .daycount import DayCount
from .debt import (
    CapitalStructure,
    DebtSchedule,
    InterestBasis,
    SweepGrid,
    Tranche,
    TrancheKind,
)
from .drivers import Driver
from .events import (
    AddOn,
    AddOnError,
    BlendedEntry,
    Draw,
    Recapitalisation,
    RecapitalisationError,
)
from .incentive import IncentiveError, OptionPool, Ratchet, Vesting
from .money import ONE, ZERO, Money, money, safe_div
from .operating import (
    DEFAULT_NOL_USAGE_LIMIT,
    AcquiredStream,
    OperatingAssumptions,
    OperatingModel,
)
from .outcome import Outcome, Security, SecurityKind, default_securities
from .periods import Frequency, PeriodGrid
from .transaction import DebtFunding, EntryValuation, LineItem, Transaction

__all__ = [
    "Deal",
    "DealSpecError",
    "EquityPlan",
    "Funding",
    "IncentivePlan",
    "SecurityPlan",
    "load_deal",
    "parse_deal",
]

#: The default exit fee rate, named so the dataclass field below can carry it.
ZERO_RATE = money(0)

_FREQUENCIES = {
    "annual": Frequency.ANNUAL,
    "semi-annual": Frequency.SEMI_ANNUAL,
    "quarterly": Frequency.QUARTERLY,
    "monthly": Frequency.MONTHLY,
}


class Funding(Enum):
    """Where a security's capital comes from.

    ``SPONSOR`` and ``ROLLOVER`` point at cheques the funding table derives.
    ``STATED`` is an amount written directly into the file, which is the right
    answer for a co-investor whose commitment is fixed and the wrong one for
    anything that moves when the price does.
    """

    SPONSOR = "sponsor"
    ROLLOVER = "rollover"
    STATED = "stated"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class SecurityPlan:
    """One instrument, described the way the file describes it.

    This exists because a ``Security`` holds an amount, and an amount is the
    wrong thing to remember about a sponsor cheque. The sponsor's contribution
    is the plug that balances the funding table: move the entry multiple by a
    quarter of a turn and the cheque moves with it. A plan resolved once at
    parse time and then carried around as a number would report every multiple
    in a sensitivity column against the base case's denominator, which is a
    quiet, plausible and completely wrong answer.

    So the *description* survives — 85% of whatever the sponsor writes — and the
    amount is derived against whichever transaction is being valued.
    """

    name: str
    kind: SecurityKind
    funding: Funding
    share: Money = ONE
    amount: Money = ZERO
    ownership: Money = ZERO
    preferred_rate: Money = ZERO
    compounding: bool = True
    seniority: int = 0

    def __post_init__(self) -> None:
        # Everything checkable without a transaction is checked here, so a
        # malformed file still fails when it is read rather than at the moment
        # somebody asks for a number.
        if not self.name.strip():
            raise ValueError("a security needs a name")
        if not (0 <= self.share <= 1):
            raise ValueError(f"{self.name}: a share, so between 0 and 1")
        if self.amount < 0:
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

    def capital(self, transaction: Transaction) -> Money:
        """What this instrument puts in, given a transaction.

        The sponsor cheque is floored at zero. A deal funded entirely by debt
        and rollover has a negative plug — the structure raises more than the
        purchase needs — and that is cash coming off the table rather than
        capital contributed at a negative multiple.
        """
        if self.funding is Funding.STATED:
            return self.amount
        cheque = (
            max(transaction.sponsor_equity, ZERO)
            if self.funding is Funding.SPONSOR
            else transaction.rollover_equity
        )
        return cheque * self.share

    def resolve(self, transaction: Transaction) -> Security:
        """This plan as a security, priced against ``transaction``."""
        return Security(
            name=self.name,
            kind=self.kind,
            invested=self.capital(transaction),
            ownership=self.ownership,
            preferred_rate=self.preferred_rate,
            compounding=self.compounding,
            seniority=self.seniority,
        )


@dataclass(frozen=True, slots=True)
class EquityPlan:
    """The equity as the file describes it, before any transaction is applied."""

    plans: tuple[SecurityPlan, ...] = ()

    def __post_init__(self) -> None:
        names = [p.name for p in self.plans]
        if len(names) != len(set(names)):
            raise ValueError("security names must be distinct")
        # Every cheque referenced has to be fully allocated. Capital left over
        # is capital that shares in nothing, which is never what was meant and
        # would quietly overstate every multiple in the report.
        claimed: dict[Funding, Money] = {}
        for plan in self.plans:
            if plan.funding is not Funding.STATED:
                claimed[plan.funding] = claimed.get(plan.funding, ZERO) + plan.share
        for source, total in claimed.items():
            if total != ONE:
                raise ValueError(
                    f"the {source} cheque is {total} allocated; the shares of a "
                    f"cheque must come to exactly 1"
                )

    def __bool__(self) -> bool:
        return bool(self.plans)

    def __len__(self) -> int:
        return len(self.plans)

    def __iter__(self) -> Iterator[SecurityPlan]:
        return iter(self.plans)

    def resolve(self, transaction: Transaction) -> tuple[Security, ...]:
        return tuple(plan.resolve(transaction) for plan in self.plans)


@dataclass(frozen=True, slots=True)
class IncentivePlan:
    """The management plan as the file describes it, before a price is applied.

    A plan exists for the same reason ``SecurityPlan`` does: the useful way to
    describe a strike is not as an amount. Options granted at close are normally
    struck at what the equity was worth that morning, so that management earn on
    value they create and not on value they were handed. That figure moves with
    the entry multiple, and a strike frozen at parse time would price every
    column of a sensitivity grid against the base case's entry valuation.

    ``strike_at_entry`` is therefore a multiple of the fully diluted equity value
    at close rather than an amount. The derivation matters and is easy to get
    wrong: the existing holders' cheque buys them the share of the company the
    pool does *not* hold, so the fully diluted value at close is that cheque
    grossed up by one less the pool's share, and the strike is the pool's share
    of it. Struck at exactly that, a plan is precisely at the money on a deal
    that creates no value — which is what "struck at cost" is supposed to mean,
    and what the naive derivation of share times cheque quietly fails to deliver.
    """

    name: str
    share: Money
    strike: Money = ZERO
    strike_at_entry: Money | None = None
    vesting: Vesting | None = None
    ratchet: Ratchet | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise IncentiveError("an incentive plan needs a name")
        if not (0 <= self.share < 1):
            raise IncentiveError(
                f"{self.name}: the pool holds a share of the equity, so at least 0 "
                f"and below 1; got {self.share}"
            )
        if self.strike < 0:
            raise IncentiveError(f"{self.name}: the strike must not be negative")
        if self.strike_at_entry is not None and self.strike_at_entry < 0:
            raise IncentiveError(
                f"{self.name}: a strike at a negative multiple of the entry value "
                f"is not a strike"
            )

    def strike_on(self, invested: Money) -> Money:
        """The aggregate cost of exercising the whole pool.

        ``invested`` is the equity capital the pool dilutes: every cheque in the
        stack, preferred included, since the pool's share is of the company and
        not of one instrument in it.
        """
        if self.strike_at_entry is None:
            return self.strike
        entry_equity_value = safe_div(invested, ONE - self.share, default=ZERO)
        return self.share * entry_equity_value * self.strike_at_entry

    def resolve(self, securities: Sequence[Security]) -> OptionPool:
        """This plan as a pool, priced against the equity that funded the deal."""
        invested = sum((s.invested for s in securities), ZERO)
        return OptionPool(
            name=self.name,
            share=self.share,
            strike=self.strike_on(invested),
            vesting=self.vesting,
            ratchet=self.ratchet,
        )


@dataclass(frozen=True, slots=True)
class Deal:
    """Everything a deal file describes.

    The transaction is always present; the projection is optional, because a
    funding table is a useful thing to look at on its own before an operating
    case exists.
    """

    name: str
    transaction: Transaction
    close_date: date | None = None
    grid: PeriodGrid | None = None
    operating: OperatingAssumptions | None = None
    opening_revenue: Money | None = None
    opening_net_working_capital: Money | None = None
    book: TargetBookBalanceSheet | None = None
    accounting: PurchaseAccounting | None = None
    structure: CapitalStructure | None = None
    opening_cash: Money | None = None
    covenants: tuple[Covenant, ...] = ()
    exit_multiple: Money | None = None
    exit_fee_rate: Money = ZERO_RATE
    equity: EquityPlan = EquityPlan()
    incentive: IncentivePlan | None = None
    recapitalisations: tuple[Recapitalisation, ...] = ()
    acquisitions: tuple[AddOn, ...] = ()

    @property
    def has_acquisitions(self) -> bool:
        return bool(self.acquisitions)

    @property
    def blended_entry(self) -> BlendedEntry:
        """The platform and its add-ons priced as a single entry.

        Available whether or not the file describes any acquisitions: a deal
        with none blends to its own multiple, which keeps every caller from
        having to ask first.
        """
        valuation = self.transaction.valuation
        return BlendedEntry(
            platform_enterprise_value=valuation.enterprise_value,
            platform_ebitda=valuation.ltm_ebitda,
            add_ons=self.acquisitions,
        )

    def acquired_streams(self) -> tuple[AcquiredStream, ...]:
        """The acquired earnings, each carried at a margin this deal implies.

        An acquisition that did not state its revenue is carried at the
        platform's margin in the period it closes — not at the margin in the
        first projected period, and not at an average. A business bought in year
        three is bought out of the market as it stands in year three, and by
        then a case with a margin ramp in it is somewhere quite different from
        where it started.
        """
        if not self.acquisitions or self.operating is None:
            return ()
        margins = self.operating.ebitda_margin
        try:
            return tuple(
                a.stream(margins.at(a.period - 1)) for a in self.acquisitions
            )
        except AddOnError as exc:
            raise DealSpecError(f"acquisitions: {exc}") from exc

    @property
    def pool(self) -> OptionPool | None:
        """The management plan, struck against this deal's equity.

        Derived for the same reason the securities are: a deal rebuilt at a
        different entry multiple raises a different sponsor cheque, and a plan
        struck at the equity value of that cheque has to move with it.
        """
        if self.incentive is None:
            return None
        return self.incentive.resolve(self.securities or default_securities(self.transaction))

    @property
    def securities(self) -> tuple[Security, ...]:
        """The equity, priced against this deal's transaction.

        Derived rather than stored, so a deal rebuilt on a different price
        carries an equity stack that agrees with the funding table that price
        produced.
        """
        return self.equity.resolve(self.transaction)

    @property
    def has_projection(self) -> bool:
        return self.grid is not None and self.operating is not None

    @property
    def has_balance_sheet(self) -> bool:
        return self.book is not None

    def recapitalise(self) -> OpeningBalanceSheet:
        """Build the balance sheet the target carries out of close.

        Requires the target's own book position, which the funding table does
        not need and so is not required to describe a deal.
        """
        if self.book is None:
            raise DealSpecError(
                'this deal has no opening balance sheet; add a "target" block '
                "describing the book position before close"
            )
        return OpeningBalanceSheet.recapitalise(self.transaction, self.book, self.accounting)

    @property
    def has_structure(self) -> bool:
        return self.structure is not None

    @property
    def cash_at_close(self) -> Money:
        """Cash the business holds the morning after the deal.

        What the target held, less what the deal took out of it, plus what the
        structure funded back in. Taking this from the transaction rather than
        asking for it again keeps the schedule from opening on a cash balance
        the funding table never produced.
        """
        valuation = self.transaction.valuation
        return (
            valuation.existing_cash
            - self.transaction.cash_from_balance_sheet
            + self.transaction.cash_to_balance_sheet
        )

    def schedule(self, model: OperatingModel | None = None) -> DebtSchedule:
        """Run the capital structure against the operating case.

        Needs both: a structure with nothing to service is not a schedule, and
        an operating case with no structure is the projection that already
        exists one layer down.

        ``model`` is taken rather than run again when the caller already has it.
        A schedule solves a fixed point and a caller running the same deal many
        times over — a sensitivity grid, say — should pay for that once per
        case rather than once per question asked of it.
        """
        if self.structure is None:
            raise DealSpecError(
                'this deal has no capital structure; add a "structure" block and '
                "price the tranches under \"debt\""
            )
        try:
            return DebtSchedule.from_operating_model(
                self.structure,
                self.project() if model is None else model,
                opening_cash=(
                    self.opening_cash
                    if self.opening_cash is not None
                    else self.cash_at_close
                ),
                # The certificate that sets the first period's sweep step was
                # signed on the LTM figure the deal was priced on, not on a
                # projection.
                opening_ebitda=self.transaction.valuation.ltm_ebitda,
                recapitalisations=self.recapitalisations,
                acquisitions=self.acquisitions,
            )
        except RecapitalisationError as exc:
            raise DealSpecError(f"recapitalisations: {exc}") from exc
        except AddOnError as exc:
            raise DealSpecError(f"acquisitions: {exc}") from exc

    @property
    def has_recapitalisations(self) -> bool:
        return bool(self.recapitalisations)

    @property
    def has_covenants(self) -> bool:
        return bool(self.covenants)

    def test_covenants(
        self,
        model: OperatingModel | None = None,
        schedule: DebtSchedule | None = None,
    ) -> CovenantReport:
        """Run the described covenants against the schedule and the case.

        Raises if the file described none rather than reporting a structure with
        no tests as a structure that passes all of them.
        """
        if not self.covenants:
            raise DealSpecError(
                'this deal has no covenants; add a "covenants" block describing '
                "the maintenance tests"
            )
        case = self.project() if model is None else model
        run = self.schedule(case) if schedule is None else schedule
        return CovenantReport.test(self.covenants, run, case)

    def realise(
        self,
        model: OperatingModel | None = None,
        schedule: DebtSchedule | None = None,
    ) -> Outcome:
        """Value the exit and run the equity through it.

        Needs a close date as well as a schedule, because a rate of return is
        measured over elapsed time and the model has to know when the clock
        started.
        """
        if self.close_date is None:
            raise DealSpecError(
                "an exit is measured from close, so a close date is required"
            )
        case = self.project() if model is None else model
        run = self.schedule(case) if schedule is None else schedule
        try:
            return Outcome.realise(
                self.transaction,
                case,
                run,
                entry_date=self.close_date,
                exit_multiple=self.exit_multiple,
                exit_fee_rate=self.exit_fee_rate,
                securities=self.securities or None,
                incentive=self.pool,
            )
        except IncentiveError as exc:
            raise DealSpecError(f"exit.incentive: {exc}") from exc
        except ValueError as exc:
            raise DealSpecError(f"exit: {exc}") from exc

    def project(self) -> OperatingModel:
        """Run the operating case.

        Raises if the file did not describe one, rather than inventing
        assumptions on the caller's behalf.
        """
        if self.grid is None or self.operating is None or self.opening_revenue is None:
            raise DealSpecError(
                "this deal has no operating case; add \"projection\" and "
                "\"operating\" blocks to the deal file"
            )
        try:
            return OperatingModel.project(
                self.grid,
                self.operating,
                opening_revenue=self.opening_revenue,
                opening_net_working_capital=self.opening_net_working_capital,
                acquisitions=self.acquired_streams(),
            )
        except ValueError as exc:
            if isinstance(exc, DealSpecError):
                raise
            raise DealSpecError(f"acquisitions: {exc}") from exc


class DealSpecError(ValueError):
    """The deal file is missing something, or holds something it should not."""


def _require(data: dict[str, Any], key: str, where: str) -> Any:
    if key not in data:
        raise DealSpecError(f"{where}: missing required field {key!r}")
    return data[key]


def _amount(value: Any, where: str) -> Money:
    try:
        return money(value if isinstance(value, (Decimal, int, str)) else str(value))
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise DealSpecError(f"{where}: not a number: {value!r}") from exc


def _optional_amount(data: dict[str, Any], key: str, where: str, default: str = "0") -> Money:
    if key not in data or data[key] is None:
        return money(default)
    return _amount(data[key], f"{where}.{key}")


def _flag(data: dict[str, Any], key: str, where: str) -> bool | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise DealSpecError(f"{where}.{key}: expected true or false, got {value!r}")
    return value


def _whole(data: dict[str, Any], key: str, where: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise DealSpecError(f"{where}.{key}: not a whole number: {value!r}") from exc


def _tranche(data: Any, index: int) -> DebtFunding:
    """The funding view of a tranche: what it raises and what it costs to raise."""
    where = f"debt[{index}]"
    if not isinstance(data, dict):
        raise DealSpecError(f"{where}: expected an object")
    return DebtFunding(
        name=str(_require(data, "name", where)),
        face=_amount(_require(data, "face", where), f"{where}.face"),
        issue_price=_optional_amount(data, "issue_price", where, default="1"),
        financing_fee_rate=_optional_amount(data, "financing_fee_rate", where),
    )


def _schedule_tranche(data: Any, index: int, periods: int) -> Tranche:
    """The schedule view of the same tranche: what it costs to carry.

    Deliberately the same object in the file. A structure described twice is a
    structure that will eventually disagree with itself — the funding table
    showing one face and the schedule accruing on another.
    """
    where = f"debt[{index}]"
    if not isinstance(data, dict):
        raise DealSpecError(f"{where}: expected an object")

    kind_name = str(data.get("kind", TrancheKind.TERM_LOAN.value)).lower()
    try:
        kind = TrancheKind(kind_name)
    except ValueError as exc:
        raise DealSpecError(
            f"{where}.kind: unknown kind {kind_name!r}; expected one of "
            f"{', '.join(k.value for k in TrancheKind)}"
        ) from exc

    amortisation = (
        _driver(data["amortisation"], periods, f"{where}.amortisation")
        if data.get("amortisation") is not None
        else None
    )
    commitment = (
        _amount(data["commitment"], f"{where}.commitment")
        if data.get("commitment") is not None
        else None
    )

    try:
        return Tranche.of(
            str(_require(data, "name", where)),
            kind,
            _amount(_require(data, "face", where), f"{where}.face"),
            cash_rate=_optional_amount(data, "cash_rate", where),
            pik_rate=_optional_amount(data, "pik_rate", where),
            floating=_flag(data, "floating", where),
            floor=_optional_amount(data, "floor", where),
            amortisation=amortisation,
            seniority=_whole(data, "seniority", where),
            swept=_flag(data, "swept", where),
            commitment=commitment,
            undrawn_fee=_optional_amount(data, "undrawn_fee", where),
            maturity=_whole(data, "maturity", where),
        )
    except ValueError as exc:
        raise DealSpecError(f"{where}: {exc}") from exc


def _other_use(data: Any, index: int) -> LineItem:
    where = f"other_uses[{index}]"
    if not isinstance(data, dict):
        raise DealSpecError(f"{where}: expected an object")
    capitalised = data.get("capitalised", False)
    if not isinstance(capitalised, bool):
        raise DealSpecError(
            f"{where}.capitalised: expected true or false, got {capitalised!r}"
        )
    return LineItem(
        label=str(_require(data, "label", where)),
        amount=_amount(_require(data, "amount", where), f"{where}.amount"),
        note=str(data.get("note", "")),
        capitalised=capitalised,
    )


def _driver(value: Any, periods: int, where: str) -> Driver:
    """Read one assumption series.

    Four shapes are accepted, and they exist because operating cases are
    written four ways:

    * a bare number — flat across the hold;
    * a list — a value per period, spelled out;
    * ``{"constant": x}`` — the same, said explicitly;
    * ``{"ramp": [start, end]}`` — a straight line between two values.

    A series shorter than the projection holds its final value, so supplying
    three years of assumptions against a five-year hold is a decision rather
    than a crash.
    """
    if isinstance(value, dict):
        if "ramp" in value:
            ends = value["ramp"]
            if not isinstance(ends, list) or len(ends) != 2:
                raise DealSpecError(f"{where}.ramp: expected exactly two values, a start and an end")
            return Driver.ramp(_amount(ends[0], where), _amount(ends[1], where), periods)
        if "constant" in value:
            return Driver.constant(_amount(value["constant"], where), periods)
        if "values" in value:
            return _driver(value["values"], periods, where)
        raise DealSpecError(f"{where}: expected one of 'constant', 'ramp' or 'values'")
    if isinstance(value, list):
        if not value:
            raise DealSpecError(f"{where}: an empty series says nothing")
        return Driver.of([_amount(v, where) for v in value]).extended_to(periods)
    return Driver.constant(_amount(value, where), periods)


def _parse_target(data: Any) -> tuple[TargetBookBalanceSheet, PurchaseAccounting]:
    """Read the target's book position and how the price is allocated over it."""
    where = "target"
    if not isinstance(data, dict):
        raise DealSpecError(f"{where}: expected an object")
    book = TargetBookBalanceSheet(
        total_assets=_amount(
            _require(data, "total_assets", where), f"{where}.total_assets"
        ),
        total_liabilities=_amount(
            _require(data, "total_liabilities", where), f"{where}.total_liabilities"
        ),
        goodwill=_optional_amount(data, "goodwill", where),
    )
    accounting = PurchaseAccounting(
        step_up=_optional_amount(data, "step_up", where),
        step_up_tax_rate=_optional_amount(data, "step_up_tax_rate", where),
    )
    return book, accounting


_DAY_COUNTS = {c.value.lower(): c for c in DayCount}
_INTEREST_BASES = {b.value: b for b in InterestBasis}
_MEASURES = {m.value: m for m in Measure}
_SECURITY_KINDS = {k.value: k for k in SecurityKind}
_FUNDING_SOURCES = {
    Funding.SPONSOR.value: Funding.SPONSOR,
    Funding.ROLLOVER.value: Funding.ROLLOVER,
}


def _parse_security(data: Any, index: int) -> SecurityPlan:
    """Read one equity instrument, and what funds it.

    Capital can be stated outright or, more usefully, as a share of a cheque the
    funding table derives. The sponsor's contribution is a plug — it is whatever
    balances the deal — so a file that restates it as a number will disagree
    with the funding table the first time any other assumption moves.
    """
    where = f"exit.equity[{index}]"
    if not isinstance(data, dict):
        raise DealSpecError(f"{where}: expected an object")

    kind_name = str(data.get("kind", SecurityKind.COMMON.value)).lower()
    if kind_name not in _SECURITY_KINDS:
        raise DealSpecError(
            f"{where}.kind: unknown kind {data.get('kind')!r}; expected one of "
            f"{', '.join(sorted(_SECURITY_KINDS))}"
        )

    source = data.get("of")
    if source is None and data.get("invested") is None:
        raise DealSpecError(
            f"{where}: say what funds this security — either 'invested' as an "
            f"amount, or 'of' naming a cheque from the funding table"
        )
    if source is not None and data.get("invested") is not None:
        raise DealSpecError(
            f"{where}: 'invested' and 'of' are two answers to the same question"
        )

    if source is None:
        funding = Funding.STATED
        amount = _amount(data["invested"], f"{where}.invested")
        share = money(1)
    else:
        name = str(source).lower()
        if name not in _FUNDING_SOURCES:
            raise DealSpecError(
                f"{where}.of: unknown source {source!r}; expected sponsor or rollover"
            )
        funding = _FUNDING_SOURCES[name]
        amount = money(0)
        share = _optional_amount(data, "share", where, default="1")
        if not (0 <= share <= 1):
            raise DealSpecError(f"{where}.share: a share, so between 0 and 1")

    compounding = _flag(data, "compounding", where)
    try:
        return SecurityPlan(
            name=str(_require(data, "name", where)),
            kind=_SECURITY_KINDS[kind_name],
            funding=funding,
            share=share,
            amount=amount,
            ownership=_optional_amount(data, "ownership", where),
            preferred_rate=_optional_amount(data, "preferred_rate", where),
            compounding=True if compounding is None else compounding,
            seniority=_whole(data, "seniority", where) or 0,
        )
    except ValueError as exc:
        raise DealSpecError(f"{where}: {exc}") from exc


def _parse_draw(data: Any, index: int, where: str) -> Draw:
    """Read one incremental take-down, priced on its own terms."""
    at = f"{where}.draws[{index}]"
    if not isinstance(data, dict):
        raise DealSpecError(f"{at}: expected an object")
    try:
        return Draw(
            tranche=str(_require(data, "tranche", at)),
            amount=_amount(_require(data, "amount", at), f"{at}.amount"),
            issue_price=_optional_amount(data, "issue_price", at, default="1"),
            financing_fee_rate=_optional_amount(data, "financing_fee_rate", at),
        )
    except RecapitalisationError as exc:
        raise DealSpecError(f"{at}: {exc}") from exc


def _parse_recapitalisation(data: Any, index: int) -> Recapitalisation:
    """Read one mid-hold raise, and what it pays out."""
    where = f"recapitalisations[{index}]"
    if not isinstance(data, dict):
        raise DealSpecError(f"{where}: expected an object")

    draws_raw = data.get("draws", [])
    if not isinstance(draws_raw, list):
        raise DealSpecError(f"{where}.draws: expected a list of take-downs")

    period_raw = _whole(data, "period", where)
    if period_raw is None:
        raise DealSpecError(
            f"{where}: say which period this lands at the end of, numbered from one"
        )

    try:
        return Recapitalisation(
            period=period_raw,
            draws=tuple(
                _parse_draw(item, i, where) for i, item in enumerate(draws_raw)
            ),
            from_cash=_optional_amount(data, "from_cash", where),
            label=str(data.get("label", "Dividend recapitalisation")),
        )
    except RecapitalisationError as exc:
        raise DealSpecError(f"{where}: {exc}") from exc


def _parse_acquisition(data: Any, index: int, periods: int) -> AddOn:
    """Read one business bought during the hold.

    ``revenue`` is optional and its absence is meaningful rather than lazy: a
    file that states only the earnings and the multiple is saying the business
    is carried at the platform's own margin, which is the assumption that
    changes the model least. Stating revenue replaces that assumption with the
    margin the business actually earns.
    """
    where = f"acquisitions[{index}]"
    if not isinstance(data, dict):
        raise DealSpecError(f"{where}: expected an object")

    period = _whole(data, "period", where)
    if period is None:
        raise DealSpecError(
            f"{where}: say which period this closes at the end of, numbered from one"
        )

    draws_raw = data.get("draws", [])
    if not isinstance(draws_raw, list):
        raise DealSpecError(f"{where}.draws: expected a list of take-downs")

    growth_raw = data.get("growth")
    growth = (
        None if growth_raw is None else _driver(growth_raw, periods, f"{where}.growth")
    )
    phase_in = _whole(data, "synergy_phase_in", where)

    try:
        return AddOn(
            period=period,
            ebitda=_amount(_require(data, "ebitda", where), f"{where}.ebitda"),
            multiple=_amount(_require(data, "multiple", where), f"{where}.multiple"),
            revenue=(
                None
                if data.get("revenue") is None
                else _amount(data["revenue"], f"{where}.revenue")
            ),
            synergies=_optional_amount(data, "synergies", where),
            synergy_phase_in=1 if phase_in is None else phase_in,
            growth=growth,
            fee_rate=_optional_amount(data, "fee_rate", where),
            integration_cost=_optional_amount(data, "integration_cost", where),
            draws=tuple(_parse_draw(item, i, where) for i, item in enumerate(draws_raw)),
            label=str(data.get("label", f"Add-on {index + 1}")),
        )
    except (AddOnError, RecapitalisationError) as exc:
        raise DealSpecError(f"{where}: {exc}") from exc


def _parse_vesting(data: Any, where: str) -> Vesting:
    """Read a vesting schedule.

    A bare number is the common case — "four-year straight-line" — and needs no
    object around it to say so.
    """
    if not isinstance(data, dict):
        return _vesting(_amount(data, where), ZERO, False, where)
    accelerates = _flag(data, "accelerates", where)
    return _vesting(
        _amount(_require(data, "years", where), f"{where}.years"),
        _optional_amount(data, "cliff_years", where),
        False if accelerates is None else accelerates,
        where,
    )


def _vesting(years: Money, cliff: Money, accelerates: bool, where: str) -> Vesting:
    try:
        return Vesting(years=years, cliff_years=cliff, accelerates=accelerates)
    except IncentiveError as exc:
        raise DealSpecError(f"{where}: {exc}") from exc


def _parse_ratchet(data: Any, where: str) -> Ratchet:
    """Read a ratchet: the bands, and the paper the hurdles are read against.

    Bands are pairs for the same reason sweep-grid steps are — "5% to a 2.0x,
    10% above it" is two numbers and a level, and naming each half of the pair
    makes the schedule harder to read rather than easier.
    """
    if not isinstance(data, dict):
        raise DealSpecError(f"{where}: expected an object")
    bands_raw = _require(data, "bands", where)
    if not isinstance(bands_raw, list) or not bands_raw:
        raise DealSpecError(f"{where}.bands: expected a list of [hurdle, share] pairs")

    bands: list[tuple[Money, Money]] = []
    for i, item in enumerate(bands_raw):
        if not isinstance(item, list) or len(item) != 2:
            raise DealSpecError(f"{where}.bands[{i}]: expected a pair of [hurdle, share]")
        bands.append(
            (
                _amount(item[0], f"{where}.bands[{i}][0]"),
                _amount(item[1], f"{where}.bands[{i}][1]"),
            )
        )

    measured_raw = data.get("measured_on", [])
    if not isinstance(measured_raw, list):
        raise DealSpecError(f"{where}.measured_on: expected a list of security names")

    try:
        return Ratchet.of(bands, measured_on=[str(name) for name in measured_raw])
    except IncentiveError as exc:
        raise DealSpecError(f"{where}: {exc}") from exc


def _parse_incentive(data: Any) -> IncentivePlan:
    """Read the management plan.

    The strike is described one of two ways and not both. An amount is right for
    a plan whose price was agreed in cash. A multiple of the entry equity value
    is right for everything else, and it is the only form that survives being
    asked what the deal looks like at a different entry multiple.
    """
    where = "exit.incentive"
    if not isinstance(data, dict):
        raise DealSpecError(f"{where}: expected an object")

    if data.get("strike") is not None and data.get("strike_at_entry") is not None:
        raise DealSpecError(
            f"{where}: 'strike' and 'strike_at_entry' are two answers to the same "
            f"question; give one"
        )
    strike_at_entry = (
        _amount(data["strike_at_entry"], f"{where}.strike_at_entry")
        if data.get("strike_at_entry") is not None
        else None
    )

    vesting = (
        _parse_vesting(data["vesting"], f"{where}.vesting")
        if data.get("vesting") is not None
        else None
    )
    ratchet = (
        _parse_ratchet(data["ratchet"], f"{where}.ratchet")
        if data.get("ratchet") is not None
        else None
    )

    try:
        return IncentivePlan(
            name=str(data.get("name", "Management incentive plan")),
            share=_amount(_require(data, "share", where), f"{where}.share"),
            strike=_optional_amount(data, "strike", where),
            strike_at_entry=strike_at_entry,
            vesting=vesting,
            ratchet=ratchet,
        )
    except IncentiveError as exc:
        raise DealSpecError(f"{where}: {exc}") from exc


def _parse_exit(data: Any) -> tuple[Money | None, Money, EquityPlan, IncentivePlan | None]:
    """Read the exit assumptions and the equity that shares in them."""
    where = "exit"
    if not isinstance(data, dict):
        raise DealSpecError(f"{where}: expected an object")

    multiple = (
        _amount(data["multiple"], f"{where}.multiple")
        if data.get("multiple") is not None
        else None
    )
    fee_rate = _optional_amount(data, "fee_rate", where)
    incentive = (
        _parse_incentive(data["incentive"]) if data.get("incentive") is not None else None
    )

    equity_raw = data.get("equity")
    if equity_raw is None:
        return multiple, fee_rate, EquityPlan(), incentive
    if not isinstance(equity_raw, list) or not equity_raw:
        raise DealSpecError(f"{where}.equity: expected a list of securities")

    plans = tuple(_parse_security(item, i) for i, item in enumerate(equity_raw))
    try:
        return multiple, fee_rate, EquityPlan(plans=plans), incentive
    except ValueError as exc:
        raise DealSpecError(f"{where}.equity: {exc}") from exc


def _parse_sweep_grid(data: Any, where: str) -> SweepGrid:
    """Read a sweep grid: the rungs, the rate below them, and what is measured.

    Written as pairs rather than as objects because that is how a term sheet
    puts it — "50% stepping to 25% at 4.50x" is two numbers and a level, and
    surrounding each pair with field names makes the grid harder to read, not
    easier.
    """
    if not isinstance(data, dict):
        raise DealSpecError(f"{where}: expected an object")
    steps_raw = _require(data, "steps", where)
    if not isinstance(steps_raw, list) or not steps_raw:
        raise DealSpecError(f"{where}.steps: expected a list of [leverage, rate] pairs")

    steps: list[tuple[Money, Money]] = []
    for i, item in enumerate(steps_raw):
        if not isinstance(item, list) or len(item) != 2:
            raise DealSpecError(
                f"{where}.steps[{i}]: expected a pair of [leverage, rate]"
            )
        steps.append(
            (
                _amount(item[0], f"{where}.steps[{i}][0]"),
                _amount(item[1], f"{where}.steps[{i}][1]"),
            )
        )

    net = _flag(data, "net", where)
    try:
        return SweepGrid.of(
            steps,
            floor=_optional_amount(data, "floor", where),
            net=True if net is None else net,
        )
    except ValueError as exc:
        raise DealSpecError(f"{where}: {exc}") from exc


def _parse_covenant(data: Any, index: int, periods: int) -> Covenant:
    """Read one maintenance test."""
    where = f"covenants[{index}]"
    if not isinstance(data, dict):
        raise DealSpecError(f"{where}: expected an object")

    measure_name = str(_require(data, "measure", where)).lower()
    if measure_name not in _MEASURES:
        raise DealSpecError(
            f"{where}.measure: unknown measure {data.get('measure')!r}; expected "
            f"one of {', '.join(sorted(_MEASURES))}"
        )

    tranches_raw = data.get("tranches", [])
    if not isinstance(tranches_raw, list):
        raise DealSpecError(f"{where}.tranches: expected a list of tranche names")

    try:
        return Covenant.of(
            str(_require(data, "name", where)),
            _MEASURES[measure_name],
            _driver(_require(data, "threshold", where), periods, f"{where}.threshold"),
            first_test_period=_whole(data, "first_test_period", where) or 1,
            tranches=[str(t) for t in tranches_raw],
        )
    except ValueError as exc:
        raise DealSpecError(f"{where}: {exc}") from exc


def _parse_structure(
    data: Any, tranches: Sequence[Tranche], periods: int
) -> tuple[CapitalStructure, Money | None]:
    """Read the rules that govern how cash moves through the stack."""
    where = "structure"
    if not isinstance(data, dict):
        raise DealSpecError(f"{where}: expected an object")

    day_count_name = str(data.get("day_count", DayCount.ACT_360.value)).lower()
    if day_count_name not in _DAY_COUNTS:
        raise DealSpecError(
            f"{where}.day_count: unknown convention {data.get('day_count')!r}; expected one "
            f"of {', '.join(c.value for c in DayCount)}"
        )

    basis_name = str(data.get("interest_basis", InterestBasis.AVERAGE.value)).lower()
    if basis_name not in _INTEREST_BASES:
        raise DealSpecError(
            f"{where}.interest_basis: expected one of "
            f"{', '.join(sorted(_INTEREST_BASES))}, got {data.get('interest_basis')!r}"
        )

    base_rate = (
        _driver(data["base_rate"], periods, f"{where}.base_rate")
        if data.get("base_rate") is not None
        else None
    )
    opening_cash = (
        _amount(data["opening_cash"], f"{where}.opening_cash")
        if data.get("opening_cash") is not None
        else None
    )
    sweep_grid = (
        _parse_sweep_grid(data["sweep_grid"], f"{where}.sweep_grid")
        if data.get("sweep_grid") is not None
        else None
    )

    try:
        structure = CapitalStructure.of(
            tranches,
            minimum_cash=_optional_amount(data, "minimum_cash", where),
            sweep_rate=_optional_amount(data, "sweep_rate", where, default="1"),
            sweep_grid=sweep_grid,
            base_rate=base_rate,
            day_count=_DAY_COUNTS[day_count_name],
            interest_basis=_INTEREST_BASES[basis_name],
            damping=_optional_amount(data, "damping", where, default="1"),
            tolerance=_optional_amount(data, "tolerance", where, default="0.000000001"),
            max_iterations=_whole(data, "max_iterations", where) or 100,
        )
    except ValueError as exc:
        raise DealSpecError(f"{where}: {exc}") from exc
    return structure, opening_cash


def _parse_projection(data: dict[str, Any], close: date | None) -> PeriodGrid:
    where = "projection"
    if close is None:
        raise DealSpecError(
            "projection: a close date is required, because the grid starts at close"
        )
    years_raw = _require(data, "years", where)
    try:
        years = int(years_raw)
    except (TypeError, ValueError) as exc:
        raise DealSpecError(f"{where}.years: not a whole number: {years_raw!r}") from exc

    frequency_name = str(data.get("frequency", "annual")).lower()
    if frequency_name not in _FREQUENCIES:
        raise DealSpecError(
            f"{where}.frequency: unknown frequency {frequency_name!r}; "
            f"expected one of {', '.join(sorted(_FREQUENCIES))}"
        )
    try:
        return PeriodGrid.build(close, years=years, frequency=_FREQUENCIES[frequency_name])
    except ValueError as exc:
        raise DealSpecError(f"{where}: {exc}") from exc


def _parse_operating(
    data: dict[str, Any], periods: int
) -> tuple[OperatingAssumptions, Money, Money | None]:
    where = "operating"
    opening_revenue = _amount(
        _require(data, "opening_revenue", where), f"{where}.opening_revenue"
    )
    opening_nwc = (
        _amount(data["opening_net_working_capital"], f"{where}.opening_net_working_capital")
        if data.get("opening_net_working_capital") is not None
        else None
    )

    assumptions = OperatingAssumptions(
        revenue_growth=_driver(
            _require(data, "revenue_growth", where), periods, f"{where}.revenue_growth"
        ),
        ebitda_margin=_driver(
            _require(data, "ebitda_margin", where), periods, f"{where}.ebitda_margin"
        ),
        da_rate=_driver(data.get("da_rate", 0), periods, f"{where}.da_rate"),
        capex_rate=_driver(data.get("capex_rate", 0), periods, f"{where}.capex_rate"),
        nwc_rate=_driver(data.get("nwc_rate", 0), periods, f"{where}.nwc_rate"),
        tax_rate=_optional_amount(data, "tax_rate", where),
        opening_carryforward=_optional_amount(data, "opening_carryforward", where),
        nol_usage_limit=(
            _amount(data["nol_usage_limit"], f"{where}.nol_usage_limit")
            if data.get("nol_usage_limit") is not None
            else DEFAULT_NOL_USAGE_LIMIT
        ),
    )
    return assumptions, opening_revenue, opening_nwc


def parse_deal(data: dict[str, Any]) -> Deal:
    """Build a deal from a parsed document."""
    if not isinstance(data, dict):
        raise DealSpecError("the deal file must contain an object at the top level")

    name = str(data.get("name", "Untitled"))

    close: date | None = None
    if data.get("close_date"):
        try:
            close = date.fromisoformat(str(data["close_date"]))
        except ValueError as exc:
            raise DealSpecError(f"close_date: not a date: {data['close_date']!r}") from exc

    entry = _require(data, "entry", "deal")
    if not isinstance(entry, dict):
        raise DealSpecError("entry: expected an object")

    valuation = EntryValuation(
        ltm_ebitda=_amount(_require(entry, "ltm_ebitda", "entry"), "entry.ltm_ebitda"),
        entry_multiple=_amount(_require(entry, "multiple", "entry"), "entry.multiple"),
        existing_debt=_optional_amount(entry, "existing_debt", "entry"),
        existing_cash=_optional_amount(entry, "existing_cash", "entry"),
    )

    debt_raw = data.get("debt", [])
    if not isinstance(debt_raw, list):
        raise DealSpecError("debt: expected a list of tranches")
    debt = tuple(_tranche(item, i) for i, item in enumerate(debt_raw))

    other_raw = data.get("other_uses", [])
    if not isinstance(other_raw, list):
        raise DealSpecError("other_uses: expected a list")
    other_uses = tuple(_other_use(item, i) for i, item in enumerate(other_raw))

    transaction = Transaction(
        valuation=valuation,
        debt=debt,
        rollover_equity=_optional_amount(data, "rollover_equity", "deal"),
        cash_from_balance_sheet=_optional_amount(data, "cash_from_balance_sheet", "deal"),
        cash_to_balance_sheet=_optional_amount(data, "cash_to_balance_sheet", "deal"),
        transaction_fee_rate=_optional_amount(data, "transaction_fee_rate", "deal"),
        other_uses=other_uses,
    )

    grid: PeriodGrid | None = None
    assumptions: OperatingAssumptions | None = None
    opening_revenue: Money | None = None
    opening_nwc: Money | None = None

    projection_raw = data.get("projection")
    operating_raw = data.get("operating")
    if (projection_raw is None) != (operating_raw is None):
        missing = "operating" if projection_raw is not None else "projection"
        raise DealSpecError(
            f"a projection needs both a 'projection' and an 'operating' block; "
            f"{missing!r} is missing"
        )
    if projection_raw is not None and operating_raw is not None:
        if not isinstance(projection_raw, dict):
            raise DealSpecError("projection: expected an object")
        if not isinstance(operating_raw, dict):
            raise DealSpecError("operating: expected an object")
        grid = _parse_projection(projection_raw, close)
        assumptions, opening_revenue, opening_nwc = _parse_operating(
            operating_raw, len(grid)
        )

    book: TargetBookBalanceSheet | None = None
    accounting: PurchaseAccounting | None = None
    if data.get("target") is not None:
        book, accounting = _parse_target(data["target"])

    structure: CapitalStructure | None = None
    opening_cash: Money | None = None
    if data.get("structure") is not None:
        if not debt_raw:
            raise DealSpecError(
                "structure: there are no tranches to schedule; describe them under 'debt'"
            )
        # Amortisation and base-rate series are read against the projection, so
        # the grid has to be known first. Without one they collapse to a single
        # period, which is enough to validate the structure but not to run it.
        span = len(grid) if grid is not None else 1
        tranches = tuple(_schedule_tranche(item, i, span) for i, item in enumerate(debt_raw))
        structure, opening_cash = _parse_structure(data["structure"], tranches, span)

    covenants: tuple[Covenant, ...] = ()
    covenants_raw = data.get("covenants")
    if covenants_raw is not None:
        if not isinstance(covenants_raw, list):
            raise DealSpecError("covenants: expected a list of tests")
        if structure is None:
            raise DealSpecError(
                "covenants: there is nothing to test; describe the tranches under "
                "'debt' and the rules under 'structure'"
            )
        if grid is None:
            raise DealSpecError(
                "covenants: a maintenance test is measured against an operating "
                "case, so a projection is required"
            )
        covenants = tuple(
            _parse_covenant(item, i, len(grid)) for i, item in enumerate(covenants_raw)
        )

    recapitalisations: tuple[Recapitalisation, ...] = ()
    recaps_raw = data.get("recapitalisations")
    if recaps_raw is not None:
        if not isinstance(recaps_raw, list):
            raise DealSpecError("recapitalisations: expected a list of events")
        if structure is None:
            raise DealSpecError(
                "recapitalisations: there is nothing to draw on; describe the "
                "tranches under 'debt' and the rules under 'structure'"
            )
        if grid is None:
            raise DealSpecError(
                "recapitalisations: an event lands in a period, so a projection "
                "is required"
            )
        recapitalisations = tuple(
            _parse_recapitalisation(item, i) for i, item in enumerate(recaps_raw)
        )

    acquisitions: tuple[AddOn, ...] = ()
    acquisitions_raw = data.get("acquisitions")
    if acquisitions_raw is not None:
        if not isinstance(acquisitions_raw, list):
            raise DealSpecError("acquisitions: expected a list of purchases")
        if grid is None or assumptions is None:
            raise DealSpecError(
                "acquisitions: a purchase adds earnings to the case, so a "
                "projection and an operating block are required"
            )
        acquisitions = tuple(
            _parse_acquisition(item, i, len(grid))
            for i, item in enumerate(acquisitions_raw)
        )

    exit_multiple: Money | None = None
    exit_fee_rate: Money = ZERO_RATE
    equity = EquityPlan()
    incentive: IncentivePlan | None = None
    if data.get("exit") is not None:
        if structure is None or grid is None:
            raise DealSpecError(
                "exit: there is nothing to exit from; describe the capital "
                "structure and the projection first"
            )
        exit_multiple, exit_fee_rate, equity, incentive = _parse_exit(data["exit"])

    return Deal(
        name=name,
        close_date=close,
        transaction=transaction,
        grid=grid,
        operating=assumptions,
        opening_revenue=opening_revenue,
        opening_net_working_capital=opening_nwc,
        book=book,
        accounting=accounting,
        structure=structure,
        opening_cash=opening_cash,
        covenants=covenants,
        exit_multiple=exit_multiple,
        exit_fee_rate=exit_fee_rate,
        equity=equity,
        incentive=incentive,
        recapitalisations=recapitalisations,
        acquisitions=acquisitions,
    )


def load_deal(path: str | Path) -> Deal:
    """Read and parse a deal file."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise DealSpecError(f"cannot read {p}: {exc}") from exc
    try:
        # parse_float keeps decimal literals exact; parse_int leaves ints alone
        # because Decimal(int) is already exact.
        data = json.loads(text, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise DealSpecError(f"{p}: invalid JSON at line {exc.lineno}: {exc.msg}") from exc
    return parse_deal(data)
