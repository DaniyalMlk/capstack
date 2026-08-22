"""Command line over the engine.

Grows a subcommand per layer as the layers land. Today it exposes the return
measures, which is enough to point it at a real set of dated flows and get the
numbers a deal team would quote.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from decimal import Decimal
from typing import Any, Sequence

from .daycount import DayCount
from .returns import AmbiguousIRR, CashFlow, CashFlowStream, IRRError

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


def _returns_report(stream: CashFlowStream) -> dict[str, Any]:
    report: dict[str, Any] = {
        "flows": [
            {
                "date": f.when.isoformat(),
                "amount": str(f.amount),
                "label": f.label,
            }
            for f in stream
        ],
        "convention": str(stream.convention),
        "holding_period_years": float(stream.holding_period_years()),
        "moic": float(stream.moic()),
        "net": str(stream.total),
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

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result: int = args.handler(args)
        return result
    except (ValueError, ZeroDivisionError) as exc:
        print(f"capstack: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
