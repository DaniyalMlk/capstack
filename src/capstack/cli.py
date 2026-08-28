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

from .covenants import CovenantObservation
from .daycount import DayCount
from .money import quantize
from .outcome import Outcome
from .report import Report, prepare
from .returns import AmbiguousIRR, CashFlow, CashFlowStream, IRRError
from .sensitivity import Axis, Grid, Metric, SensitivityError, format_value
from .fees import FeeMethod
from .spec import Deal, DealSpecError, load_deal
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


def _column(index: int) -> str:
    """What heads a column: ``Stub`` for period zero, ``P3`` otherwise."""
    return "Stub" if index == 0 else f"P{index}"


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


_PROJECTION_ROWS: tuple[tuple[str, str], ...] = (
    ("Revenue", "revenue"),
    ("EBITDA", "ebitda"),
    ("Depreciation & amortisation", "depreciation_and_amortisation"),
    ("EBIT", "ebit"),
    ("Cash tax", "cash_tax"),
    ("NOPAT", "nopat"),
    ("Add back D&A", "depreciation_and_amortisation"),
    ("Capital expenditure", "capital_expenditure"),
    ("Change in working capital", "change_in_net_working_capital"),
    ("Unlevered free cash flow", "unlevered_free_cash_flow"),
)

#: Rows shown as an outflow, so each column reads the way the arithmetic runs
#: down the page rather than requiring the reader to remember the signs.
_NEGATED_ROWS = frozenset({"Capital expenditure", "Change in working capital", "Cash tax"})

#: Rows after which a blank line separates one subtotal block from the next.
_RULED_ROWS = frozenset({"EBIT", "NOPAT"})


def _projection_report(deal: Deal) -> dict[str, Any]:
    model = deal.project()
    return {
        "name": deal.name,
        "opening_revenue": _amount_str(model.opening_revenue),
        "opening_net_working_capital": _amount_str(model.opening_net_working_capital),
        "periods": [
            {
                "index": p.index,
                "ending": p.period.end.isoformat(),
                "revenue": _amount_str(p.revenue),
                "ebitda": _amount_str(p.ebitda),
                "ebitda_margin": float(p.ebitda_margin),
                "depreciation_and_amortisation": _amount_str(p.depreciation_and_amortisation),
                "ebit": _amount_str(p.ebit),
                "taxable_income": _amount_str(p.tax.taxable_income),
                "loss_relief_used": _amount_str(p.tax.loss_relief_used),
                "cash_tax": _amount_str(p.tax.cash_tax),
                "closing_carryforward": _amount_str(p.tax.closing_carryforward),
                "nopat": _amount_str(p.nopat),
                "capital_expenditure": _amount_str(p.capital_expenditure),
                "net_working_capital": _amount_str(p.net_working_capital),
                "change_in_net_working_capital": _amount_str(p.change_in_net_working_capital),
                "unlevered_free_cash_flow": _amount_str(p.unlevered_free_cash_flow),
                "cash_conversion": float(p.cash_conversion),
            }
            for p in model
        ],
        "totals": {
            "entry_ebitda": _amount_str(model.entry_ebitda),
            "exit_ebitda": _amount_str(model.exit_ebitda),
            "unlevered_free_cash_flow": _amount_str(model.total_unlevered_free_cash_flow),
            "cash_tax": _amount_str(model.total_cash_tax),
            "capital_expenditure": _amount_str(model.total_capital_expenditure),
            "working_capital_absorbed": _amount_str(model.working_capital_absorbed),
            "closing_carryforward": _amount_str(model.closing_carryforward),
        },
    }


def _print_projection(report: dict[str, Any]) -> None:
    periods = report["periods"]
    header = f"{report['name']} - operating case"
    print(header)
    print("=" * len(header))
    print()

    label_width = max(len(label) for label, _ in _PROJECTION_ROWS) + 2
    widest = max(
        len(_format_money(Decimal(p[key]))) for p in periods for _, key in _PROJECTION_ROWS
    )
    column = max(widest, 7) + 2

    def row(label: str, cells: list[str]) -> str:
        return "  " + label.ljust(label_width) + "".join(c.rjust(column) for c in cells)

    print(row("", [_column(p["index"]) for p in periods]))
    print(row("", [p["ending"][:7] for p in periods]))
    print("  " + "-" * (label_width + column * len(periods)))

    for label, key in _PROJECTION_ROWS:
        cells = []
        for p in periods:
            value = Decimal(p[key])
            if label in _NEGATED_ROWS:
                value = -value
            cells.append(_format_money(value))
        print(row(label, cells))
        if label in _RULED_ROWS:
            print()

    print()
    print(row("EBITDA margin", [f"{p['ebitda_margin']:.1%}" for p in periods]))
    print(row("Cash conversion", [f"{p['cash_conversion']:.1%}" for p in periods]))

    totals = report["totals"]
    print()
    print("  Across the hold")
    for label, key in (
        ("Entry EBITDA", "entry_ebitda"),
        ("Exit EBITDA", "exit_ebitda"),
        ("Cumulative unlevered FCF", "unlevered_free_cash_flow"),
        ("Cash tax paid", "cash_tax"),
        ("Capital expenditure", "capital_expenditure"),
        ("Working capital absorbed", "working_capital_absorbed"),
    ):
        print(f"    {label:<28}{_format_money(Decimal(totals[key])):>14}")
    if Decimal(totals["closing_carryforward"]) > 0:
        print(
            f"    {'Losses carried forward':<28}"
            f"{_format_money(Decimal(totals['closing_carryforward'])):>14}"
        )


#: The opening balance sheet, as it would be laid out on a page. Each row is a
#: label, the attribute behind it, and whether it belongs on the asset side.
_BALANCE_SHEET_ROWS: tuple[tuple[str, str, str], ...] = (
    ("assets", "Cash", "cash"),
    ("assets", "Identifiable assets", "identifiable_assets"),
    ("assets", "Goodwill", "goodwill"),
    ("assets", "Deferred financing costs", "deferred_financing_costs"),
    ("assets", "Unamortised issue discount", "unamortised_issue_discount"),
    ("liabilities", "Debt at face", "debt_at_face"),
    ("liabilities", "Operating liabilities", "operating_liabilities"),
    ("liabilities", "Deferred tax liability", "deferred_tax_liability"),
    ("equity", "Sponsor equity", "sponsor_equity"),
    ("equity", "Rollover equity", "rollover_equity"),
    ("equity", "Expensed at close", "expensed_at_close"),
)

#: Shown as a deduction, because it is one.
_BALANCE_SHEET_NEGATED = frozenset({"Expensed at close"})


def _balance_report(deal: Deal) -> dict[str, Any]:
    sheet = deal.recapitalise()
    return {
        "name": deal.name,
        "close_date": deal.close_date.isoformat() if deal.close_date else None,
        "assets": {
            key: _amount_str(getattr(sheet, key))
            for side, _, key in _BALANCE_SHEET_ROWS
            if side == "assets"
        },
        "liabilities": {
            key: _amount_str(getattr(sheet, key))
            for side, _, key in _BALANCE_SHEET_ROWS
            if side == "liabilities"
        },
        "equity": {
            key: _amount_str(getattr(sheet, key))
            for side, _, key in _BALANCE_SHEET_ROWS
            if side == "equity"
        },
        "total_assets": _amount_str(sheet.total_assets),
        "total_liabilities": _amount_str(sheet.total_liabilities),
        "total_equity": _amount_str(sheet.total_equity),
        "total_liabilities_and_equity": _amount_str(sheet.total_liabilities_and_equity),
        "balanced": sheet.total_assets == sheet.total_liabilities_and_equity,
        "net_debt": _amount_str(sheet.net_debt),
        "goodwill_share_of_assets": float(sheet.goodwill_share_of_assets),
    }


def _print_balance(report: dict[str, Any]) -> None:
    header = f"{report['name']} - opening balance sheet"
    if report["close_date"]:
        header += f"   (close {report['close_date']})"
    print(header)
    print("=" * len(header))
    print()

    label_width = max(len(label) for _, label, _ in _BALANCE_SHEET_ROWS)
    amount_width = max(
        len(_format_money(Decimal(report[side][key])))
        for side, _, key in _BALANCE_SHEET_ROWS
    )
    amount_width = max(amount_width, len(_format_money(Decimal(report["total_assets"]))))

    def block(title: str, side: str, total_label: str, total_key: str) -> None:
        print(f"  {title}")
        for row_side, label, key in _BALANCE_SHEET_ROWS:
            if row_side != side:
                continue
            value = Decimal(report[side][key])
            if label in _BALANCE_SHEET_NEGATED:
                value = -value
            elif value == 0:
                continue
            print(f"    {label:<{label_width}}  {_format_money(value):>{amount_width}}")
        print(f"    {'':<{label_width}}  {'-' * amount_width}")
        print(
            f"    {total_label:<{label_width}}  "
            f"{_format_money(Decimal(report[total_key])):>{amount_width}}"
        )
        print()

    block("Assets", "assets", "Total assets", "total_assets")
    block("Liabilities", "liabilities", "Total liabilities", "total_liabilities")
    block("Equity", "equity", "Total equity", "total_equity")

    print(
        f"    {'Liabilities and equity':<{label_width}}  "
        f"{_format_money(Decimal(report['total_liabilities_and_equity'])):>{amount_width}}"
    )
    print()
    print(f"    {'Net debt':<{label_width}}  {_format_money(Decimal(report['net_debt'])):>{amount_width}}")
    print(
        f"    {'Goodwill share of assets':<{label_width}}  "
        f"{report['goodwill_share_of_assets']:>{amount_width}.1%}"
    )


def _cmd_balance(args: argparse.Namespace) -> int:
    deal = load_deal(args.file)
    report = _balance_report(deal)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_balance(report)
    return 0


#: Summary rows above the per-tranche detail: label, attribute, and whether the
#: figure is money leaving the business.
_SCHEDULE_ROWS: tuple[tuple[str, str, bool], ...] = (
    ("Unlevered free cash flow", "unlevered_free_cash_flow", False),
    ("Cash interest", "cash_interest", True),
    ("Commitment fees", "undrawn_fees", True),
    ("Levered free cash flow", "levered_free_cash_flow", False),
    ("Mandatory repayment", "mandatory_repayment", True),
    ("Cash sweep", "sweep_repayment", True),
    ("Revolver draw", "revolver_draw", False),
    ("Recapitalisation", "recapitalisation", False),
    ("Distribution paid", "distribution", True),
    ("Closing cash", "closing_cash", False),
    ("Accrued to balances", "pik_interest", False),
    ("Closing debt", "closing_debt", False),
)


#: Rows that only appear on a schedule where something actually happened.
_EVENT_ROWS = frozenset({"recapitalisation", "distribution"})


def _schedule_report(deal: Deal) -> dict[str, Any]:
    schedule = deal.schedule()
    model = deal.project()
    names = [t.name for t in schedule.structure]
    return {
        "name": deal.name,
        "tranches": names,
        "periods": [
            {
                "index": period.index,
                "ending": period.period.end.isoformat(),
                "base_rate": float(period.base_rate),
                **{key: _amount_str(getattr(period, key)) for _, key, _ in _SCHEDULE_ROWS},
                "funding_shortfall": _amount_str(period.funding_shortfall),
                "iterations": period.iterations,
                "residual": _amount_str(period.residual),
                # A stub has no leverage reading. The debt is a whole balance
                # and the earnings are a fraction of a year's, so the ratio is
                # not several turns worse than the deal carries — it is not a
                # ratio at all, for the same reason a covenant does not test
                # there.
                "leverage": (
                    None
                    if period.period.is_stub
                    else float(schedule.leverage_at(i, model[i].ebitda))
                ),
                "balances": {
                    row.name: _amount_str(row.closing) for row in period.tranches
                },
                "interest": {
                    row.name: _amount_str(row.cash_interest) for row in period.tranches
                },
                "amortisation_basis": {
                    row.name: _amount_str(row.amortisation_basis)
                    for row in period.tranches
                },
            }
            for i, period in enumerate(schedule)
        ],
        "totals": {
            "cash_interest": _amount_str(schedule.total_cash_interest),
            "pik_interest": _amount_str(schedule.total_pik_interest),
            "undrawn_fees": _amount_str(schedule.total_undrawn_fees),
            "repaid": _amount_str(schedule.total_repaid),
            "drawn": _amount_str(schedule.total_drawn),
            "recapitalised": _amount_str(schedule.total_recapitalised),
            "distributed": _amount_str(schedule.total_distributed),
            "opening_debt": _amount_str(schedule.opening_debt),
            "closing_debt": _amount_str(schedule.closing_debt),
            "closing_net_debt": _amount_str(schedule.closing_net_debt),
            "debt_repaid": _amount_str(schedule.debt_repaid),
            "peak_revolver_drawn": _amount_str(schedule.peak_revolver_drawn),
        },
        "entry_leverage": float(deal.transaction.entry_leverage),
        "exit_leverage": float(schedule.leverage_at(len(schedule) - 1, model.exit_ebitda)),
        "funded": schedule.is_funded,
        "max_iterations": schedule.max_iterations_used,
        "max_residual": _amount_str(schedule.max_residual),
    }


def _print_schedule(report: dict[str, Any]) -> None:
    periods = report["periods"]
    header = f"{report['name']} - debt schedule"
    print(header)
    print("=" * len(header))
    print()

    labels = [label for label, _, _ in _SCHEDULE_ROWS] + report["tranches"]
    label_width = max(len(label) for label in labels) + 2
    widest = max(
        len(_format_money(Decimal(p[key])))
        for p in periods
        for _, key, _ in _SCHEDULE_ROWS
    )
    column = max(widest, 9) + 2

    def row(label: str, cells: list[str]) -> str:
        return "  " + label.ljust(label_width) + "".join(c.rjust(column) for c in cells)

    print(row("", [_column(p["index"]) for p in periods]))
    print(row("", [p["ending"][:7] for p in periods]))
    print("  " + "-" * (label_width + column * len(periods)))

    for label, key, outflow in _SCHEDULE_ROWS:
        values = [Decimal(p[key]) for p in periods]
        # The two event rows are silent on a deal that had no events. A row of
        # zeroes in every column tells a reader nothing and costs them a line
        # of attention on every schedule they ever read.
        if key in _EVENT_ROWS and not any(values):
            continue
        cells = [_format_money(-v if outflow else v) for v in values]
        print(row(label, cells))
        if label in ("Levered free cash flow", "Closing cash"):
            print()

    print()
    print("  Closing balances")
    for name in report["tranches"]:
        print(row(name, [_format_money(Decimal(p["balances"][name])) for p in periods]))

    # The face the instalments were struck against, shown only where it moved.
    # On a deal with no draws after close it is the funding table repeated in a
    # second place, which is a line of attention spent to learn nothing.
    if any(
        len({p["amortisation_basis"][name] for p in periods}) > 1
        for name in report["tranches"]
    ):
        print()
        print("  Amortising face")
        for name in report["tranches"]:
            print(
                row(
                    name,
                    [
                        _format_money(Decimal(p["amortisation_basis"][name]))
                        for p in periods
                    ],
                )
            )

    print()
    print(
        row(
            "Leverage",
            [
                "-" if p["leverage"] is None else f"{p['leverage']:.2f}x"
                for p in periods
            ],
        )
    )
    print(row("Base rate", [f"{p['base_rate']:.2%}" for p in periods]))

    totals = report["totals"]
    print()
    print("  Across the hold")
    for label, key in (
        ("Cash interest paid", "cash_interest"),
        ("Interest accrued to balances", "pik_interest"),
        ("Commitment fees", "undrawn_fees"),
        ("Repayments made", "repaid"),
        ("Revolver drawn", "drawn"),
        ("Net reduction in debt", "debt_repaid"),
        ("Peak revolver balance", "peak_revolver_drawn"),
        ("Closing net debt", "closing_net_debt"),
    ):
        print(f"    {label:<30}{_format_money(Decimal(totals[key])):>14}")
    print(f"    {'Entry leverage':<30}{report['entry_leverage']:>13.2f}x")
    print(f"    {'Exit leverage':<30}{report['exit_leverage']:>13.2f}x")

    if not report["funded"]:
        short = next(p for p in periods if Decimal(p["funding_shortfall"]) > 0)
        print()
        print(
            f"    Period {short['index']} is short by "
            f"{_format_money(Decimal(short['funding_shortfall']))} after the revolver is"
        )
        print("    fully drawn. The structure does not fund itself as described.")


def _cmd_schedule(args: argparse.Namespace) -> int:
    deal = load_deal(args.file)
    report = _schedule_report(deal)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_schedule(report)
    return 0


def _covenant_report(deal: Deal) -> dict[str, Any]:
    report = deal.test_covenants()
    return {
        "name": deal.name,
        "covenants": [
            {"name": c.name, "measure": str(c.measure), "first_test_period": c.first_test_period}
            for c in report.covenants
        ],
        "observations": [_observation(o) for o in report],
        "passes": report.passes,
        "breaches": [_observation(o) for o in report.breaches],
        "first_breach": (
            _observation(report.first_breach) if report.first_breach is not None else None
        ),
        "tightest": (
            _observation(report.tightest) if report.tightest is not None else None
        ),
    }


def _observation(o: CovenantObservation) -> dict[str, Any]:
    """One tested covenant, flattened.

    Ratios are emitted as floats and amounts as strings, which is the split used
    everywhere else in this layer: a multiple is a measurement and a balance is
    a quantity of money that has to survive a round trip.
    """
    return {
        "covenant": o.covenant,
        "measure": str(o.measure),
        "period": o.index,
        "ending": o.period.end.isoformat(),
        "tested": o.tested,
        "threshold": float(o.threshold),
        "actual": None if o.actual is None else float(o.actual),
        "passes": o.passes,
        "breached": o.breached,
        "headroom": None if o.headroom is None else float(o.headroom),
        "ebitda": _amount_str(o.ebitda),
        "ebitda_at_breach": (
            None if o.ebitda_at_breach is None else _amount_str(o.ebitda_at_breach)
        ),
        "ebitda_cushion": (
            None if o.ebitda_cushion is None else float(o.ebitda_cushion)
        ),
        "note": o.note,
    }


#: What a cell shows when the ratio has no value. Distinct from a blank, which
#: would read as a number the report forgot to print.
_NO_RATIO = "n/a"


def _ratio(value: float | None, suffix: str = "x") -> str:
    return _NO_RATIO if value is None else f"{value:.2f}{suffix}"


def _print_covenants(report: dict[str, Any]) -> None:
    header = f"{report['name']} - covenants"
    print(header)
    print("=" * len(header))
    print()

    observations = report["observations"]
    periods = sorted({o["period"] for o in observations})
    endings = {o["period"]: o["ending"][:7] for o in observations}

    label_width = max(
        max((len(c["name"]) for c in report["covenants"]), default=0), len("Cushion")
    ) + 4
    column = 11

    def row(label: str, cells: list[str]) -> str:
        return "  " + label.ljust(label_width) + "".join(c.rjust(column) for c in cells)

    print(row("", [_column(i) for i in periods]))
    print(row("", [endings[i] for i in periods]))
    print("  " + "-" * (label_width + column * len(periods)))

    for covenant in report["covenants"]:
        rows = {o["period"]: o for o in observations if o["covenant"] == covenant["name"]}
        print(row(covenant["name"], [_ratio(rows[i]["actual"]) for i in periods]))
        print(
            row(
                "  covenant",
                [
                    _NO_RATIO if not rows[i]["tested"] else _ratio(rows[i]["threshold"])
                    for i in periods
                ],
            )
        )
        print(
            row(
                "  cushion",
                [
                    _NO_RATIO
                    if rows[i]["ebitda_cushion"] is None or not rows[i]["tested"]
                    else f"{rows[i]['ebitda_cushion']:.1%}"
                    for i in periods
                ],
            )
        )
        print(
            row(
                "  status",
                [
                    "-" if not rows[i]["tested"] else ("ok" if rows[i]["passes"] else "BREACH")
                    for i in periods
                ],
            )
        )
        print()

    tightest = report["tightest"]
    if tightest is not None:
        print("  Tightest test")
        print(f"    {tightest['covenant']} in period {tightest['period']}")
        print(
            f"    {'EBITDA projected':<26}"
            f"{_format_money(Decimal(tightest['ebitda'])):>14}"
        )
        if tightest["ebitda_at_breach"] is not None:
            print(
                f"    {'Breaches below':<26}"
                f"{_format_money(Decimal(tightest['ebitda_at_breach'])):>14}"
            )
        if tightest["ebitda_cushion"] is not None:
            print(f"    {'Cushion':<26}{tightest['ebitda_cushion']:>13.1%}")
        print()

    first = report["first_breach"]
    if first is None:
        print("  No maintenance test is breached across the hold.")
    else:
        print(
            f"    {first['covenant']} is breached in period {first['period']} "
            f"({first['ending']})."
        )
        if first["actual"] is not None:
            print(
                f"    The test reads {first['actual']:.2f}x against a "
                f"{first['threshold']:.2f}x covenant."
            )
        else:
            print(f"    {first['note'].capitalize()}.")
        print(f"    {len(report['breaches'])} of {len(observations)} tests fail.")


def _cmd_covenants(args: argparse.Namespace) -> int:
    deal = load_deal(args.file)
    report = _covenant_report(deal)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_covenants(report)
    # A breached case is a finding, not a failure of the tool, so the exit code
    # is only non-zero where a caller would want a script to stop.
    return 0


def _exit_report(deal: Deal) -> dict[str, Any]:
    outcome = deal.realise()
    v = outcome.valuation
    a = outcome.attribution
    return {
        "name": deal.name,
        "close_date": deal.close_date.isoformat() if deal.close_date else None,
        "exit": {
            "date": v.when.isoformat(),
            "ebitda": _amount_str(v.ebitda),
            "multiple": float(v.multiple),
            "enterprise_value": _amount_str(v.enterprise_value),
            "debt": _amount_str(v.debt),
            "cash": _amount_str(v.cash),
            "net_debt": _amount_str(v.net_debt),
            "fees": _amount_str(v.fees),
            "equity_value": _amount_str(v.equity_value),
            "exit_leverage": float(v.exit_leverage),
            "wiped_out": v.is_wiped_out,
        },
        "securities": [
            {
                "name": row.name,
                "kind": str(row.security.kind),
                "invested": _amount_str(row.invested),
                "accrued": _amount_str(row.accrued),
                "preferred_paid": _amount_str(row.preferred_paid),
                "residual_paid": _amount_str(row.residual_paid),
                "proceeds": _amount_str(row.proceeds),
                "shortfall": _amount_str(row.shortfall),
                "ownership": float(row.security.ownership),
                "interim": _amount_str(row.interim),
                "received": _amount_str(row.received),
                "moic": None if row.moic is None else float(row.moic),
                "irr": row.irr,
                "irr_note": row.irr_note,
            }
            for row in outcome
        ],
        "incentive": _incentive_block(outcome),
        "distributions": [
            {
                "date": d.when.isoformat(),
                "label": d.label,
                "amount": _amount_str(d.amount),
                "to_preferred": _amount_str(d.to_preferred),
                "to_common": _amount_str(d.to_common),
                "years": float(d.years),
            }
            for d in outcome.distributions
        ],
        "totals": {
            "invested": _amount_str(outcome.invested),
            "proceeds": _amount_str(outcome.proceeds),
            "profit": _amount_str(outcome.profit),
            "interim": _amount_str(outcome.interim),
            "received": _amount_str(outcome.received),
            "moic": None if outcome.moic is None else float(outcome.moic),
            "irr": outcome.irr,
            "holding_period_years": float(outcome.holding_period_years),
            "distributed": _amount_str(outcome.distributed),
        },
        "attribution": {
            "ebitda_growth": _amount_str(a.ebitda_growth),
            "multiple_change": _amount_str(a.multiple_change),
            "debt_paydown": _amount_str(a.debt_paydown),
            "costs": _amount_str(a.costs),
            "total": _amount_str(a.total),
            "value_created": _amount_str(a.value_created),
            "floored": _amount_str(a.floored),
            "reconciles": a.reconciles(),
        },
    }


def _incentive_block(outcome: Outcome) -> dict[str, Any] | None:
    """The management plan, or ``None`` where the deal describes none.

    Absent rather than zeroed, because a deal with no plan and a deal whose
    plan expired worthless are different facts and a reader consuming this as
    JSON should not have to infer which one they are looking at.
    """
    settled = outcome.incentive
    if settled is None:
        return None
    return {
        "name": settled.name,
        "vested": float(settled.vested),
        "residual_before": _amount_str(settled.residual),
        "pot": _amount_str(settled.pot),
        "entitlement": _amount_str(settled.entitlement),
        "strike_paid": _amount_str(settled.strike_paid),
        "paid": _amount_str(settled.paid),
        "dilution": _amount_str(settled.dilution),
        "effective_share": float(settled.effective_share),
        "exercised": settled.exercised,
    }


#: The bridge, in the order it is read: what the business earned, what the
#: market paid for it, what the lenders were given back, what it cost.
_ATTRIBUTION_ROWS: tuple[tuple[str, str], ...] = (
    ("EBITDA growth", "ebitda_growth"),
    ("Multiple change", "multiple_change"),
    ("Debt paydown", "debt_paydown"),
    ("Entry and exit costs", "costs"),
)


def _print_distributions(blocks: list[dict[str, Any]]) -> None:
    """Print what was paid out during the hold, and when.

    The elapsed years are the column that matters. An interim distribution is
    worth what it is worth because of when it arrived, and a table that showed
    only the amount would be a table about the wrong thing.
    """
    if not blocks:
        return
    print("  Paid during the hold")
    width = max(len(b["label"]) for b in blocks) + 2
    for block in blocks:
        print(
            "    "
            + block["date"]
            + "  "
            + block["label"].ljust(width)
            + _format_money(Decimal(block["amount"])).rjust(13)
            + f"{block['years']:>9.2f}y"
        )
    total = sum((Decimal(b["amount"]) for b in blocks), Decimal(0))
    print("    " + "-" * (width + 34))
    print("    " + "Total distributed".ljust(width + 12) + _format_money(total).rjust(13))
    print()


def _print_incentive(block: dict[str, Any] | None) -> None:
    """Print the management plan, and say plainly when it was worth nothing."""
    if block is None:
        return
    print(f"  {block['name']}")
    if not block["exercised"]:
        print(
            "    Out of the money at this exit: the options lapse, management are"
        )
        print("    paid nothing, and the common are not diluted.")
        print(f"    {'Vested':<26}{block['vested']:>13.1%}")
        print()
        return
    print(f"    {'Vested':<26}{block['vested']:>13.1%}")
    print(f"    {'Share of the pot':<26}{block['effective_share']:>13.1%}")
    for label, key in (
        ("Residual before the plan", "residual_before"),
        ("Strike paid in", "strike_paid"),
        ("Pot divided", "pot"),
        ("Entitlement", "entitlement"),
        ("Paid to management", "paid"),
    ):
        print(f"    {label:<26}{_format_money(Decimal(block[key])):>14}")
    print("    What management are paid is what the common give up, to the penny.")
    print()


def _print_exit(report: dict[str, Any]) -> None:
    header = f"{report['name']} - exit"
    print(header)
    print("=" * len(header))
    print()

    exit_ = report["exit"]
    print(f"  Exit at {exit_['date']}")
    for label, key in (
        ("Exit EBITDA", "ebitda"),
        ("Enterprise value", "enterprise_value"),
        ("Debt outstanding", "debt"),
        ("Cash", "cash"),
        ("Cost of sale", "fees"),
        ("Equity value", "equity_value"),
    ):
        print(f"    {label:<26}{_format_money(Decimal(exit_[key])):>14}")
    print(f"    {'Exit multiple':<26}{exit_['multiple']:>13.2f}x")
    print(f"    {'Exit leverage':<26}{exit_['exit_leverage']:>13.2f}x")
    if exit_["wiped_out"]:
        print()
        print("    The business is worth less than it owes, so the equity is wiped")
        print("    out and the lenders take what there is.")
    print()

    rows = report["securities"]
    totals = report["totals"]
    # The interim column only exists where something was paid before the exit.
    # Without it the multiple would be quoted against a proceeds figure that
    # does not produce it, which is the sort of table a reader stops trusting.
    paid_early = any(Decimal(r["interim"]) != 0 for r in rows)
    columns: tuple[tuple[str, str], ...] = (
        (("invested", "invested"), ("during hold", "interim"), ("at exit", "proceeds"),
         ("received", "received"))
        if paid_early
        else (("invested", "invested"), ("proceeds", "proceeds"))
    )

    label_width = max(max(len(r["name"]) for r in rows), len("Total")) + 2
    ruler = label_width + 13 * (len(columns) + 2)
    print("  Equity")
    print(
        "    "
        + "".ljust(label_width)
        + "".join(h.rjust(13) for h, _ in columns)
        + "MoIC".rjust(13)
        + "IRR".rjust(13)
    )
    for row in rows:
        print(
            "    "
            + row["name"].ljust(label_width)
            + "".join(_format_money(Decimal(row[key])).rjust(13) for _, key in columns)
            + (_NO_RATIO if row["moic"] is None else f"{row['moic']:.2f}x").rjust(13)
            + (_NO_RATIO if row["irr"] is None else f"{row['irr']:.1%}").rjust(13)
        )
        if Decimal(row["shortfall"]) > 0:
            print(
                f"      unpaid preferred claim of "
                f"{_format_money(Decimal(row['shortfall']))}"
            )
    print("    " + "-" * ruler)
    print(
        "    "
        + "Total".ljust(label_width)
        + "".join(_format_money(Decimal(totals[key])).rjust(13) for _, key in columns)
        + (_NO_RATIO if totals["moic"] is None else f"{totals['moic']:.2f}x").rjust(13)
        + (_NO_RATIO if totals["irr"] is None else f"{totals['irr']:.1%}").rjust(13)
    )
    print(f"    over {totals['holding_period_years']:.2f} years")
    print()

    _print_distributions(report["distributions"])
    _print_incentive(report["incentive"])

    attribution = report["attribution"]
    print("  Where the value came from")
    for label, key in _ATTRIBUTION_ROWS:
        print(f"    {label:<26}{_format_money(Decimal(attribution[key])):>14}")
    print(f"    {'':<26}{'-' * 14}")
    print(f"    {'Value created':<26}{_format_money(Decimal(attribution['total'])):>14}")
    if Decimal(attribution["floored"]) > 0:
        print()
        print(
            f"    Of that loss, "
            f"{_format_money(Decimal(attribution['floored']))} falls on the lenders "
            f"rather than"
        )
        print("    the shareholders, who owe nothing beyond their capital.")


# -- Sensitivity ---------------------------------------------------------

#: Marks in the table, kept to one column each so the grid stays aligned.
_BREACH_MARK = "!"
_BASE_MARK = "*"
_NO_CELL = "-"


def _sensitivity_report(
    deal: Deal, rows: Axis, columns: Axis, metric: Metric
) -> dict[str, Any]:
    grid = Grid.run(deal, rows, columns, metric)
    return {
        "name": deal.name,
        "metric": {
            "name": str(metric),
            "label": metric.label,
            "unit": str(metric.unit),
        },
        "rows": {
            "dimension": str(rows.dimension),
            "label": rows.dimension.label,
            "unit": str(rows.dimension.unit),
            "values": [_amount_str(v) for v in rows],
            "labels": [rows.format(v) for v in rows],
            "base": [rows.is_base(v, deal) for v in rows],
        },
        "columns": {
            "dimension": str(columns.dimension),
            "label": columns.dimension.label,
            "unit": str(columns.dimension.unit),
            "values": [_amount_str(v) for v in columns],
            "labels": [columns.format(v) for v in columns],
            "base": [columns.is_base(v, deal) for v in columns],
        },
        "base": {
            "value": None if grid.base is None else _amount_str(grid.base),
            "label": (
                None if grid.base is None else format_value(metric.unit, grid.base)
            ),
            "note": grid.base_note,
        },
        "cells": [
            [
                {
                    "value": None if cell.value is None else _amount_str(cell.value),
                    "label": (
                        None
                        if cell.value is None
                        else format_value(metric.unit, cell.value)
                    ),
                    "note": cell.note,
                    "breached": cell.breached,
                    "breach": cell.breach_note,
                }
                for cell in line
            ]
            for line in grid
        ],
    }


def _print_sensitivity(report: dict[str, Any]) -> None:
    header = f"{report['name']} - sensitivity"
    print(header)
    print("=" * len(header))
    print()

    metric = report["metric"]
    rows, columns = report["rows"], report["columns"]
    print(f"  {metric['label']}")
    print(f"    {columns['label'].lower()} across, {rows['label'].lower()} down")
    base = report["base"]
    if base["label"] is not None:
        print(f"    base case {base['label']}, marked {_BASE_MARK}")
    elif base["note"]:
        print(f"    base case: {base['note']}")
    print()

    cells = report["cells"]
    # Every cell carries a mark or a space in the same position, so the columns
    # line up whether or not anything in them is flagged.
    rendered = [
        [
            (_NO_CELL if c["label"] is None else c["label"])
            + (_BREACH_MARK if c["breached"] else " ")
            for c in line
        ]
        for line in cells
    ]
    # The base mark sits on the axis label rather than in the cell, so a
    # reader finds the file's own case by following one row and one column in
    # rather than hunting for a decorated cell in the middle of the table.
    headings = [
        label + (_BASE_MARK if flag else "")
        for label, flag in zip(columns["labels"], columns["base"])
    ]
    stub = max((len(label) for label in rows["labels"]), default=0) + 2
    width = max(
        max((len(text) for line in rendered for text in line), default=0),
        max((len(h) for h in headings), default=0),
    ) + 3

    print(("    " + "".ljust(stub) + "".join(h.rjust(width) for h in headings)).rstrip())
    for i, line in enumerate(rendered):
        label = rows["labels"][i] + (_BASE_MARK if rows["base"][i] else "")
        print(
            ("    " + label.ljust(stub) + "".join(t.rjust(width) for t in line)).rstrip()
        )

    legend = []
    if any(c["breached"] for line in cells for c in line):
        legend.append(f"{_BREACH_MARK} a covenant breaches on this case")
    if any(c["label"] is None for line in cells for c in line):
        legend.append(f"{_NO_CELL} no answer; see below")
    if any(rows["base"]) or any(columns["base"]):
        legend.append(f"{_BASE_MARK} the assumption the file describes")
    if legend:
        print()
        for entry in legend:
            print(f"    {entry}")

    # Distinct reasons only. A row of cells that all failed for the same reason
    # is one fact about the deal, not eight.
    reasons: list[str] = []
    for line in cells:
        for cell in line:
            if cell["label"] is None and cell["note"] not in reasons:
                reasons.append(cell["note"])
    if reasons:
        print()
        for reason in reasons:
            print(f"    {_NO_CELL} {reason}")


def _cmd_sensitivity(args: argparse.Namespace) -> int:
    deal = load_deal(args.file)
    report = _sensitivity_report(
        deal, Axis.parse(args.rows), Axis.parse(args.columns), Metric(args.metric)
    )
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_sensitivity(report)
    return 0


# -- The memo ------------------------------------------------------------


def _report_document(report: Report) -> dict[str, Any]:
    """The memo as data, for anything that would rather lay it out itself."""
    return {
        "name": report.name,
        "close_date": report.close.isoformat() if report.close else None,
        "title": report.title,
        "sections": [
            {
                "title": section.title,
                "summary": section.summary,
                "lines": [
                    {"label": line.label, "value": line.value, "note": line.note}
                    for line in section.lines
                ],
                "table": (
                    None
                    if section.table is None
                    else {
                        "headings": list(section.table.headings),
                        "rows": [list(row) for row in section.table.rows],
                        "align": list(section.table.alignment),
                    }
                ),
                "notes": list(section.notes),
            }
            for section in report
        ],
    }


def _cmd_report(args: argparse.Namespace) -> int:
    report = prepare(load_deal(args.file), breakevens=not args.no_breakevens)
    if args.json:
        print(json.dumps(_report_document(report), indent=2))
    elif args.markdown:
        print(report.as_markdown(), end="")
    else:
        print(report.as_text(), end="")
    return 0


def _cmd_exit(args: argparse.Namespace) -> int:
    deal = load_deal(args.file)
    report = _exit_report(deal)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_exit(report)
    return 0


def _cmd_project(args: argparse.Namespace) -> int:
    deal = load_deal(args.file)
    report = _projection_report(deal)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_projection(report)
    return 0


def _acquisitions_report(deal: Deal) -> dict[str, Any]:
    """The acquisition programme: what each purchase cost and what it blended to.

    The funding table is built from the events rather than from the schedule, so
    a price can be read out of a file with no operating case behind it. Where
    there is one, the schedule is run as well — it is the only thing that knows
    whether the business could actually pay its share of the price.
    """
    if not deal.acquisitions:
        raise DealSpecError(
            'this deal buys nothing during the hold; add an "acquisitions" block '
            "describing the purchases"
        )
    blended = deal.blended_entry
    landed: dict[int, Any] = {}
    if deal.has_projection and deal.has_structure:
        landed = {o.event.period: o for o in deal.schedule().acquisitions}

    purchases = []
    for add_on in deal.acquisitions:
        outcome = landed.get(add_on.period)
        purchases.append(
            {
                "label": add_on.label,
                "period": add_on.period,
                "ebitda": str(add_on.ebitda),
                "multiple": str(add_on.multiple),
                "enterprise_value": str(add_on.enterprise_value),
                "fees": str(add_on.fees),
                "integration_cost": str(add_on.integration_cost),
                "uses": str(add_on.uses),
                "face_drawn": str(add_on.face),
                "debt_proceeds": str(add_on.debt_proceeds),
                "from_cash": str(add_on.from_cash),
                "total_cost": str(add_on.total_cost),
                "synergies": str(add_on.synergies),
                "synergy_phase_in": add_on.synergy_phase_in,
                "synergised_multiple": str(add_on.synergised_multiple),
                "cash_after": None if outcome is None else str(outcome.cash_after),
                "leverage_after": (
                    None
                    if outcome is None or outcome.leverage_after is None
                    else str(outcome.leverage_after)
                ),
                "turns_added": (
                    None
                    if outcome is None or outcome.turns_added is None
                    else str(outcome.turns_added)
                ),
            }
        )

    return {
        "name": deal.name,
        "purchases": purchases,
        "blended": {
            "platform_enterprise_value": str(blended.platform_enterprise_value),
            "platform_ebitda": str(blended.platform_ebitda),
            "platform_multiple": str(blended.platform_multiple),
            "acquired_enterprise_value": str(blended.acquired_enterprise_value),
            "acquired_ebitda": str(blended.acquired_ebitda),
            "synergies": str(blended.synergies),
            "enterprise_value": str(blended.enterprise_value),
            "ebitda": str(blended.ebitda),
            "capital_deployed": str(blended.capital_deployed),
            "blended_multiple": str(blended.blended_multiple),
            "synergised_multiple": str(blended.synergised_multiple),
            "all_in_multiple": str(blended.all_in_multiple),
            "arbitrage": str(blended.arbitrage),
            "acquired_share": str(blended.acquired_share),
        },
    }


def _print_acquisitions(report: dict[str, Any]) -> None:
    header = f"{report['name']} - acquisitions"
    print(header)
    print("=" * len(header))
    print()

    for p in report["purchases"]:
        title = f"{p['label']}  (end of period {p['period']})"
        print(f"  {title}")
        print("  " + "-" * len(title))
        rows: list[tuple[str, str]] = [
            ("EBITDA acquired", _format_money(Decimal(p["ebitda"]))),
            ("Multiple paid", f"{Decimal(p['multiple']):.2f}x"),
            ("Enterprise value", _format_money(Decimal(p["enterprise_value"]))),
        ]
        if Decimal(p["synergies"]) > 0:
            phase = p["synergy_phase_in"]
            over = "immediate" if phase == 1 else f"over {phase} periods"
            rows.append((f"Synergies, {over}", _format_money(Decimal(p["synergies"]))))
            rows.append(
                ("Multiple after synergies", f"{Decimal(p['synergised_multiple']):.2f}x")
            )
        rows.extend(
            [
                ("Transaction fees", _format_money(Decimal(p["fees"]))),
                ("Integration cost", _format_money(Decimal(p["integration_cost"]))),
                ("Total uses", _format_money(Decimal(p["uses"]))),
                ("Face drawn", _format_money(Decimal(p["face_drawn"]))),
                ("Debt proceeds", _format_money(Decimal(p["debt_proceeds"]))),
                ("Funded from cash", _format_money(Decimal(p["from_cash"]))),
                ("Capital deployed", _format_money(Decimal(p["total_cost"]))),
            ]
        )
        if p["cash_after"] is not None:
            rows.append(("Cash after", _format_money(Decimal(p["cash_after"]))))
        if p["leverage_after"] is not None:
            rows.append(("Leverage after", f"{Decimal(p['leverage_after']):.2f}x"))
        if p["turns_added"] is not None:
            rows.append(("Turns added", f"{Decimal(p['turns_added']):+.2f}x"))
        for label, value in rows:
            print(f"    {label:<28}{value:>14}")
        print()

    b = report["blended"]
    print("  Blended entry")
    for label, value in (
        (
            "Platform enterprise value",
            _format_money(Decimal(b["platform_enterprise_value"])),
        ),
        ("Platform EBITDA", _format_money(Decimal(b["platform_ebitda"]))),
        ("Platform multiple", f"{Decimal(b['platform_multiple']):.2f}x"),
        (
            "Acquired enterprise value",
            _format_money(Decimal(b["acquired_enterprise_value"])),
        ),
        ("EBITDA acquired", _format_money(Decimal(b["acquired_ebitda"]))),
        ("Combined EBITDA", _format_money(Decimal(b["ebitda"]))),
        ("Capital deployed", _format_money(Decimal(b["capital_deployed"]))),
    ):
        print(f"    {label:<28}{value:>14}")
    print()
    for label, value in (
        ("Blended multiple", f"{Decimal(b['blended_multiple']):.2f}x"),
        ("After synergies", f"{Decimal(b['synergised_multiple']):.2f}x"),
        ("On capital deployed", f"{Decimal(b['all_in_multiple']):.2f}x"),
        ("Multiple arbitrage", f"{Decimal(b['arbitrage']):+.2f}x"),
        ("Bought, not built", f"{Decimal(b['acquired_share']):.1%}"),
    ):
        print(f"    {label:<28}{value:>14}")


def _fees_report(deal: Deal, method: FeeMethod) -> dict[str, Any]:
    schedule = deal.fee_schedule(method)
    grid = deal.grid
    assert grid is not None  # fee_schedule refuses a deal without one
    return {
        "name": deal.name,
        "method": str(method),
        "periods": [p.end.isoformat() for p in grid],
        "period_indices": [p.index for p in grid],
        "tranches": [
            {
                "name": t.name,
                "capitalised": _amount_str(t.capitalised),
                "method": str(t.method),
                "coupon": float(t.coupon),
                "effective_rate": float(t.effective_rate),
                "rate_uplift": float(t.rate_uplift),
                "charges": [_amount_str(p.charge) for p in t],
                "balances": [_amount_str(p.closing) for p in t],
            }
            for t in schedule
        ],
        "total_capitalised": _amount_str(schedule.total_capitalised),
        "total_charged": _amount_str(schedule.total_charged),
        "unreleased": _amount_str(schedule.unreleased),
        "write_offs": [
            {
                "label": event.label,
                "period": event.period,
                "tranche": event.tranche,
                "amount": _amount_str(amount),
            }
            for event in deal.refinancings
            for amount in [deal.write_offs().get(event.period)]
            if amount is not None
        ],
    }


def _print_fees(report: dict[str, Any]) -> None:
    header = f"{report['name']} - capitalised financing costs"
    print(header)
    print("=" * len(header))
    print()

    rows = [t for t in report["tranches"] if Decimal(t["capitalised"]) > 0]
    if not rows:
        print("  Nothing was capitalised: every tranche was placed at par with no fee.")
        return

    label_width = max(len(t["name"]) for t in rows) + 2
    column = 11
    periods = report["periods"]

    def line(label: str, cells: list[str]) -> str:
        return "  " + label.ljust(label_width) + "".join(c.rjust(column) for c in cells)

    print(line("", [_column(i) for i in report["period_indices"]]))
    print(line("", [p[:7] for p in periods]))
    print("  " + "-" * (label_width + column * len(periods)))

    print("  Charge for the period")
    for t in rows:
        print(line(t["name"], [_format_money(Decimal(c)) for c in t["charges"]]))
    print()
    print("  Balance remaining")
    for t in rows:
        print(line(t["name"], [_format_money(Decimal(b)) for b in t["balances"]]))

    print()
    print("  Cost of the money")
    width = max(len(t["name"]) for t in rows) + 2
    print(f"    {'':<{width}}{'coupon':>10}{'effective':>12}{'uplift':>10}  method")
    for t in rows:
        print(
            f"    {t['name']:<{width}}{t['coupon']:>10.2%}{t['effective_rate']:>12.2%}"
            f"{t['rate_uplift'] * 10000:>9.0f}bp  {t['method']}"
        )

    print()
    print(f"    {'Capitalised at close':<34}{_format_money(Decimal(report['total_capitalised'])):>12}")
    print(f"    {'Released if the paper runs to term':<34}{_format_money(Decimal(report['total_charged'])):>12}")
    print(f"    {'Still capitalised at the end':<34}{_format_money(Decimal(report['unreleased'])):>12}")

    if report["write_offs"]:
        print()
        print("  Charged off early at a takeout")
        for w in report["write_offs"]:
            print(
                f"    P{w['period']} {w['label']} ({w['tranche']}): "
                f"{_format_money(Decimal(w['amount']))}"
            )
        print()
        print("    The takeout ends the release. What is charged off above is")
        print("    incurred at the takeout instead of over the periods after it,")
        print("    which is why this schedule runs past the event rather than")
        print("    stopping at it.")


def _cmd_fees(args: argparse.Namespace) -> int:
    deal = load_deal(args.file)
    method = (
        FeeMethod.STRAIGHT_LINE
        if args.method == "straight-line"
        else FeeMethod.EFFECTIVE_INTEREST
    )
    report = _fees_report(deal, method)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_fees(report)
    return 0


def _cmd_acquisitions(args: argparse.Namespace) -> int:
    deal = load_deal(args.file)
    report = _acquisitions_report(deal)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_acquisitions(report)
    return 0


def _cmd_deal(args: argparse.Namespace) -> int:
    deal = load_deal(args.file)
    report = _deal_report(deal.name, deal.close_date, deal.transaction)
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

    balance = sub.add_parser(
        "balance",
        help="the opening balance sheet after the recapitalisation",
        description=(
            "Apply the transaction to the target's book position and print the "
            "balance sheet the business carries out of close."
        ),
    )
    balance.add_argument("file", help="path to a deal file (JSON)")
    balance.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    balance.set_defaults(handler=_cmd_balance)

    project = sub.add_parser(
        "project",
        help="the operating case, from revenue to unlevered free cash flow",
        description=(
            "Run the operating case in a deal file and print the projection, "
            "before any of it is claimed by lenders."
        ),
    )
    project.add_argument("file", help="path to a deal file (JSON)")
    project.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    project.set_defaults(handler=_cmd_project)

    schedule = sub.add_parser(
        "schedule",
        help="the debt schedule: interest, amortisation, the sweep and the revolver",
        description=(
            "Run the capital structure against the operating case and print what "
            "the debt costs, what gets repaid, and where the leverage ends up."
        ),
    )
    schedule.add_argument("file", help="path to a deal file (JSON)")
    schedule.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    schedule.set_defaults(handler=_cmd_schedule)

    covenants = sub.add_parser(
        "covenants",
        help="maintenance tests, headroom, and the first breach",
        description=(
            "Test the maintenance covenants in a deal file against the schedule "
            "and the operating case, and report how far EBITDA can fall before "
            "each one trips."
        ),
    )
    covenants.add_argument("file", help="path to a deal file (JSON)")
    covenants.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    covenants.set_defaults(handler=_cmd_covenants)

    exit_ = sub.add_parser(
        "exit",
        help="the exit: equity value, returns by security, and the value bridge",
        description=(
            "Value the exit, run the equity waterfall through it, and decompose "
            "the return into earnings growth, multiple change and debt paydown."
        ),
    )
    exit_.add_argument("file", help="path to a deal file (JSON)")
    exit_.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    exit_.set_defaults(handler=_cmd_exit)

    acquisitions = sub.add_parser(
        "acquisitions",
        help="businesses bought during the hold, and the entry multiple they blend to",
        description=(
            "Show the funding table for each acquisition and the multiple the "
            "platform and its add-ons together were bought at, before and after "
            "the synergies underwritten for them."
        ),
    )
    acquisitions.add_argument("file", help="path to a deal file (JSON)")
    acquisitions.add_argument(
        "--json", action="store_true", help="emit JSON instead of a table"
    )
    acquisitions.set_defaults(handler=_cmd_acquisitions)

    fees = sub.add_parser(
        "fees",
        help="capitalised financing costs, and the balance a takeout writes off",
        description=(
            "Release the arrangement fees and the original issue discount each "
            "tranche was placed with across the life of the paper, and report "
            "what a refinancing would charge off. The effective method solves "
            "the rate the contractual flows discount back to net proceeds; the "
            "straight line spreads the balance evenly."
        ),
    )
    fees.add_argument("file", help="path to a deal file (JSON)")
    fees.add_argument(
        "--method",
        choices=("effective-interest", "straight-line"),
        default="effective-interest",
        help="how the balance is released (default: %(default)s)",
    )
    fees.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    fees.set_defaults(handler=_cmd_fees)

    sensitivity = sub.add_parser(
        "sensitivity",
        help="a metric across two assumptions, with the deal rebuilt per cell",
        description=(
            "Run the whole engine at every intersection of two assumptions and "
            "report one figure per cell. An axis is written as "
            "DIMENSION:v1,v2,v3 - levels as they are said (entry-multiple:11,"
            "11.5,12), shifts in percentage points off the file's own case "
            "(ebitda-margin:-1.5,0,1.5)."
        ),
    )
    sensitivity.add_argument("file", help="path to a deal file (JSON)")
    sensitivity.add_argument(
        "--rows",
        required=True,
        metavar="DIMENSION:VALUES",
        help="the axis down the side, as exit-multiple:9,10,11",
    )
    sensitivity.add_argument(
        "--columns",
        required=True,
        metavar="DIMENSION:VALUES",
        help="the axis across the top, as entry-multiple:11,11.5,12",
    )
    sensitivity.add_argument(
        "--metric",
        default=Metric.IRR.value,
        choices=[m.value for m in Metric],
        help="what each cell reports (default: %(default)s)",
    )
    sensitivity.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    sensitivity.set_defaults(handler=_cmd_sensitivity)

    report = sub.add_parser(
        "report",
        help="the whole deal as one memo, including where the case stops working",
        description=(
            "Assemble every layer into one document: the transaction, the "
            "operating case, the debt schedule, the covenants, the exit and "
            "the value bridge, followed by the break-evens - the exit multiple "
            "that returns capital and no more, and the assumptions at which "
            "the first covenant trips."
        ),
    )
    report.add_argument("file", help="path to a deal file (JSON)")
    output = report.add_mutually_exclusive_group()
    output.add_argument(
        "--markdown", action="store_true", help="emit markdown instead of aligned text"
    )
    output.add_argument(
        "--json", action="store_true", help="emit the memo as structured data"
    )
    report.add_argument(
        "--no-breakevens",
        action="store_true",
        help="skip the break-evens, which are the expensive part",
    )
    report.set_defaults(handler=_cmd_report)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result: int = args.handler(args)
        return result
    except (DealSpecError, SensitivityError) as exc:
        print(f"capstack: {exc}", file=sys.stderr)
        return 1
    except (ValueError, ZeroDivisionError) as exc:
        print(f"capstack: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
