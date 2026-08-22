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

from .money import Money, money
from .transaction import DebtFunding, EntryValuation, LineItem, Transaction

__all__ = ["DealSpecError", "load_deal", "parse_deal"]


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
    return LineItem(
        label=str(_require(data, "label", where)),
        amount=_amount(_require(data, "amount", where), f"{where}.amount"),
        note=str(data.get("note", "")),
    )


def parse_deal(data: dict[str, Any]) -> tuple[str, date | None, Transaction]:
    """Build a transaction from a parsed deal document.

    Returns the deal's name and close date alongside the transaction, since both
    belong to the deal rather than to the funding table.
    """
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
    return name, close, transaction


def load_deal(path: str | Path) -> tuple[str, date | None, Transaction]:
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
