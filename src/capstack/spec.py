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
from typing import Any

from dataclasses import dataclass

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

    @property
    def has_projection(self) -> bool:
        return self.grid is not None and self.operating is not None

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


def _tranche(data: Any, index: int) -> DebtFunding:
    where = f"debt[{index}]"
    if not isinstance(data, dict):
        raise DealSpecError(f"{where}: expected an object")
    return DebtFunding(
        name=str(_require(data, "name", where)),
        face=_amount(_require(data, "face", where), f"{where}.face"),
        issue_price=_optional_amount(data, "issue_price", where, default="1"),
        financing_fee_rate=_optional_amount(data, "financing_fee_rate", where),
    )


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

    return Deal(
        name=name,
        close_date=close,
        transaction=transaction,
        grid=grid,
        operating=assumptions,
        opening_revenue=opening_revenue,
        opening_net_working_capital=opening_nwc,
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
