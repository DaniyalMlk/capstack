"""Command line over the engine.

Grows a subcommand per layer as the layers land: ``returns`` over a set of dated
cash flows, and ``deal`` over a deal file. Every layer is reachable from here,
so nothing in the engine is only exercisable from a test.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from decimal import Decimal
from typing import Any, Sequence

from .daycount import DayCount
from .money import quantize
from .returns import AmbiguousIRR, CashFlow, CashFlowStream, IRRError
from .spec import DealSpecError, load_deal
from .transaction import Transaction

__all__ = ["main"]

_CONVENTIONS = {c.value: c for c in DayCount}


def _parse_flow(token: str) -> CashFlow:
    """Parse one ``YYYY-MM-DD:amount`` token, with an optional trailing label."""
    parts = token.split(":")
    if len(parts) < 2:
        raise argparse.ArgumentTypeError(
            f"expected DATE:AMOUNT[:LABEL], got {token!r}"
        )
    when_text, amount_text = parts[0], parts[1]
    label = parts[2] if len(parts) > 2 else ""
    try:
        when = date.fromisoformat(when_text.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a date: {when_text!r}") from exc
    try:
        amount = Decimal(amount_text.strip().replace("_", "").replace(",", ""))
    except Exception as exc:  # noqa: BLE001 - argparse wants one error type
        raise argparse.ArgumentTypeError(f"not an amount: {amount_text!r}") from exc
    return CashFlow(when=when, amount=amount, label=label)


def _format_money(value: Decimal) -> str:
    return f"{value:,.2f}"


def _amount_str(value: Decimal) -> str:
    """Serialise an amount at cent scale.

    ``Decimal`` carries the scale of whatever arithmetic produced it, so a fee
    computed as ``2760.0 * 0.014`` stringifies as ``38.64000``. The value is
    right; the presentation is not, and callers comparing against ``"38.64"``
    would be surprised. Rounding happens here, at the edge, and nowhere earlier.
    """
    return str(quantize(value, 2))


def _returns_report(stream: CashFlowStream) -> dict[str, Any]:
    report: dict[str, Any] = {
        "flows": [
            {
                "date": f.when.isoformat(),
                "amount": _amount_str(f.amount),
                "label": f.label,
            }
            for f in stream
        ],
        "convention": str(stream.convention),
        "holding_period_years": float(stream.holding_period_years()),
        "moic": float(stream.moic()),
        "net": _amount_str(stream.total),
    }
    try:
        report["irr"] = stream.xirr()
    except AmbiguousIRR as exc:
        report["irr"] = None
        report["irr_note"] = str(exc)
        report["irr_candidates"] = list(exc.roots)
    except IRRError as exc:
        report["irr"] = None
        report["irr_note"] = str(exc)
    return report


def _print_returns(report: dict[str, Any]) -> None:
    width = max((len(f["label"]) for f in report["flows"]), default=0)
    print("Cash flows")
    for flow in report["flows"]:
        label = f"  {flow['label']:<{width}}" if width else ""
        print(
            f"  {flow['date']}{label}  "
            f"{_format_money(Decimal(flow['amount'])):>18}"
        )
    print()
    print(f"  {'Day count':<22}{report['convention']}")
    print(f"  {'Holding period':<22}{report['holding_period_years']:.2f} years")
    print(f"  {'Net':<22}{_format_money(Decimal(report['net']))}")
    print(f"  {'MoIC':<22}{report['moic']:.2f}x")
    if report.get("irr") is None:
        print(f"  {'IRR':<22}not reported")
        print(f"  {'':<22}{report['irr_note']}")
    else:
        print(f"  {'IRR':<22}{report['irr']:.2%}")


def _cmd_returns(args: argparse.Namespace) -> int:
    stream = CashFlowStream(
        flows=tuple(args.flow), convention=_CONVENTIONS[args.convention]
    )
    report = _returns_report(stream)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_returns(report)
    return 0


def _deal_report(name: str, close: date | None, deal: Transaction) -> dict[str, Any]:
    table = deal.sources_and_uses()
    return {
        "name": name,
        "close_date": close.isoformat() if close else None,
        "entry": {
            "ltm_ebitda": _amount_str(deal.valuation.ltm_ebitda),
            "multiple": float(deal.valuation.entry_multiple),
            "enterprise_value": _amount_str(deal.valuation.enterprise_value),
            "net_debt": _amount_str(deal.valuation.net_debt),
            "equity_purchase_price": _amount_str(deal.valuation.equity_purchase_price),
        },
        "sources": [
            {"label": i.label, "amount": _amount_str(i.amount), "note": i.note}
            for i in table.sources
        ],
        "uses": [
            {"label": i.label, "amount": _amount_str(i.amount), "note": i.note}
            for i in table.uses
        ],
        "total_sources": _amount_str(table.total_sources),
        "total_uses": _amount_str(table.total_uses),
        "balanced": table.total_sources == table.total_uses,
        "metrics": {
            "entry_leverage": float(deal.entry_leverage),
            "total_capitalisation": _amount_str(deal.total_capitalisation),
            "equity_contribution_rate": float(deal.equity_contribution_rate),
            "sponsor_ownership": float(deal.sponsor_ownership),
            "sponsor_equity": _amount_str(deal.sponsor_equity),
            "overfunded": deal.is_overfunded,
        },
    }


def _print_table(
    title: str,
    rows: list[dict[str, str]],
    total: str,
    label_width: int,
    amount_width: int,
) -> None:
    print(f"  {title}")
    for row in rows:
        amount = _format_money(Decimal(row["amount"]))
        note = f"   {row['note']}" if row["note"] else ""
        print(f"    {row['label']:<{label_width}}  {amount:>{amount_width}}{note}")
    print(f"    {'':<{label_width}}  {'-' * amount_width}")
    print(f"    {'Total':<{label_width}}  {_format_money(Decimal(total)):>{amount_width}}")


def _print_deal(report: dict[str, Any]) -> None:
    header = report["name"]
    if report["close_date"]:
        header += f"   (close {report['close_date']})"
    print(header)
    print("=" * len(header))
    print()

    entry = report["entry"]
    print("  Entry")
    print(f"    {'LTM EBITDA':<26}{_format_money(Decimal(entry['ltm_ebitda'])):>16}")
    print(f"    {'Entry multiple':<26}{entry['multiple']:>15.2f}x")
    print(f"    {'Enterprise value':<26}{_format_money(Decimal(entry['enterprise_value'])):>16}")
    print(f"    {'Net debt':<26}{_format_money(Decimal(entry['net_debt'])):>16}")
    print(
        f"    {'Equity purchase price':<26}"
        f"{_format_money(Decimal(entry['equity_purchase_price'])):>16}"
    )
    print()

    # Both halves of the funding table share column widths, so the two totals
    # sit in the same place on the page and can be read against each other.
    rows = [*report["sources"], *report["uses"]]
    label_width = max(max((len(r["label"]) for r in rows), default=0), len("Total"))
    amount_width = max(
        len(_format_money(Decimal(a)))
        for a in [*(r["amount"] for r in rows), report["total_sources"], report["total_uses"]]
    )

    _print_table(
        "Sources", report["sources"], report["total_sources"], label_width, amount_width
    )
    print()
    _print_table("Uses", report["uses"], report["total_uses"], label_width, amount_width)
    print()

    metrics = report["metrics"]
    print("  Entry metrics")
    print(f"    {'Total leverage':<26}{metrics['entry_leverage']:>15.2f}x")
    print(f"    {'Equity contribution':<26}{metrics['equity_contribution_rate']:>15.1%}")
    print(f"    {'Sponsor ownership':<26}{metrics['sponsor_ownership']:>15.1%}")
    print(
        f"    {'Total capitalisation':<26}"
        f"{_format_money(Decimal(metrics['total_capitalisation'])):>16}"
    )
    if metrics["overfunded"]:
        print()
        print("    Note: the debt raised exceeds what the deal requires, so the")
        print("    sponsor takes a distribution at close rather than writing a cheque.")


def _cmd_deal(args: argparse.Namespace) -> int:
    name, close, deal = load_deal(args.file)
    report = _deal_report(name, close, deal)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_deal(report)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="capstack",
        description="A leveraged buyout engine.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    returns = sub.add_parser(
        "returns",
        help="return measures for a dated cash-flow stream",
        description=(
            "Compute IRR, MoIC and holding period for a set of dated flows. "
            "Signs are from the investor's point of view: negative is money out."
        ),
    )
    returns.add_argument(
        "flow",
        nargs="+",
        type=_parse_flow,
        metavar="DATE:AMOUNT[:LABEL]",
        help="a dated cash flow, e.g. 2026-06-30:-420000000:equity",
    )
    returns.add_argument(
        "--convention",
        choices=sorted(_CONVENTIONS),
        default=DayCount.ACT_365F.value,
        help="day-count convention used to annualise (default: %(default)s)",
    )
    returns.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    returns.set_defaults(handler=_cmd_returns)

    deal = sub.add_parser(
        "deal",
        help="entry valuation and the sources and uses table",
        description=(
            "Read a deal file and print what the business costs, how the purchase "
            "is funded, and the entry metrics that fall out of it."
        ),
    )
    deal.add_argument("file", help="path to a deal file (JSON)")
    deal.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    deal.set_defaults(handler=_cmd_deal)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result: int = args.handler(args)
        return result
    except DealSpecError as exc:
        print(f"capstack: {exc}", file=sys.stderr)
        return 1
    except (ValueError, ZeroDivisionError) as exc:
        print(f"capstack: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
