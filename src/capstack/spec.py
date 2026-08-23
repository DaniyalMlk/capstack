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
from pathlib import Path
from collections.abc import Sequence
from typing import Any

from dataclasses import dataclass

from .balance_sheet import OpeningBalanceSheet, PurchaseAccounting, TargetBookBalanceSheet
from .daycount import DayCount
from .debt import (
    CapitalStructure,
    DebtSchedule,
    InterestBasis,
    Tranche,
    TrancheKind,
)
from .drivers import Driver
from .money import Money, money
from .operating import DEFAULT_NOL_USAGE_LIMIT, OperatingAssumptions, OperatingModel
from .periods import Frequency, PeriodGrid
from .transaction import DebtFunding, EntryValuation, LineItem, Transaction

__all__ = ["Deal", "DealSpecError", "load_deal", "parse_deal"]

_FREQUENCIES = {
    "annual": Frequency.ANNUAL,
    "semi-annual": Frequency.SEMI_ANNUAL,
    "quarterly": Frequency.QUARTERLY,
    "monthly": Frequency.MONTHLY,
}


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

    def schedule(self) -> DebtSchedule:
        """Run the capital structure against the operating case.

        Needs both: a structure with nothing to service is not a schedule, and
        an operating case with no structure is the projection that already
        exists one layer down.
        """
        if self.structure is None:
            raise DealSpecError(
                'this deal has no capital structure; add a "structure" block and '
                "price the tranches under \"debt\""
            )
        return DebtSchedule.from_operating_model(
            self.structure,
            self.project(),
            opening_cash=self.opening_cash if self.opening_cash is not None else self.cash_at_close,
        )

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
        return OperatingModel.project(
            self.grid,
            self.operating,
            opening_revenue=self.opening_revenue,
            opening_net_working_capital=self.opening_net_working_capital,
        )


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

    try:
        structure = CapitalStructure.of(
            tranches,
            minimum_cash=_optional_amount(data, "minimum_cash", where),
            sweep_rate=_optional_amount(data, "sweep_rate", where, default="1"),
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
