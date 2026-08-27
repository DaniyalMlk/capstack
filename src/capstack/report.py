"""The memo.

Every other subcommand answers one question. This one assembles the answers into
the document somebody actually takes into a room, in the order the argument is
made rather than the order the code was written: what is being bought and for
what, how it is funded, what the case says, what the schedule does to the debt,
where the covenants sit, what the sponsor makes, and where the value came from.

*The section that is not reformatting.* A memo that restates the base case tells
a reader the one thing they already believe. The last section says where the
case stops working — the exit multiple at which the sponsor gets its money back
and no more, the margin shift at which the tightest covenant trips, the leverage
at which the structure stops clearing its tests. Those are break-evens, solved by
re-running the engine along the dimension until the metric crosses, and they are
the numbers a committee spends its time on.

*Two renderings, one document.* The content is assembled once into sections,
lines and tables, then rendered as aligned text for a terminal or as markdown for
anything that has to be pasted somewhere. Assembling twice is how the two
versions come to disagree about a figure, which is the sort of thing that is
noticed at exactly the wrong moment.

*What is not here.* No recommendation. The engine can say what the case is worth
and where it breaks; whether that is a deal worth doing is a judgement, and a
model that renders one has overstepped.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date

from .covenants import CovenantReport
from .incentive import PoolOutcome
from .money import ONE, ZERO, Money, money, quantize, safe_div
from .outcome import Outcome
from .returns import cagr
from .sensitivity import (
    Breakeven,
    Case,
    Dimension,
    Metric,
    SensitivityError,
    Unit,
    format_value,
    solve,
)
from .spec import Deal

__all__ = ["Line", "Report", "Section", "Table", "prepare"]

#: Where each break-even is looked for. Wide enough to hold the answer for any
#: deal that is recognisably a buyout, and bounded, because a search that
#: wanders far enough finds prices at which the deal is not a transaction.
EXIT_MULTIPLE_BRACKET = (money(1), money(30))
LEVERAGE_BRACKET = (money("0.5"), money(15))
MARGIN_SHIFT_BRACKET = (money("-0.08"), money("0.08"))

_NONE = "n/a"


@dataclass(frozen=True, slots=True)
class Line:
    """One labelled figure, with an optional gloss."""

    label: str
    value: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class Table:
    """A grid of already-rendered strings.

    Formatting happens when the section is assembled rather than when it is
    printed, so the text and the markdown cannot round a figure differently.
    """

    headings: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    align: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        width = len(self.headings)
        for row in self.rows:
            if len(row) != width:
                raise ValueError(
                    f"a row has {len(row)} cells and the table has {width} columns"
                )
        if self.align and len(self.align) != width:
            raise ValueError("the alignment must name every column or none of them")

    @property
    def alignment(self) -> tuple[str, ...]:
        """Left for the first column, right for the rest, unless told otherwise."""
        if self.align:
            return self.align
        return ("l",) + ("r",) * (len(self.headings) - 1)


@dataclass(frozen=True, slots=True)
class Section:
    """One part of the memo."""

    title: str
    summary: str = ""
    lines: tuple[Line, ...] = ()
    table: Table | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Report:
    """The whole memo, assembled and not yet rendered."""

    name: str
    close: date | None
    sections: tuple[Section, ...] = field(default_factory=tuple)

    def __len__(self) -> int:
        return len(self.sections)

    def __iter__(self) -> Iterator[Section]:
        return iter(self.sections)

    def section(self, title: str) -> Section:
        for part in self.sections:
            if part.title == title:
                return part
        raise KeyError(f"no section titled {title!r}")

    @property
    def title(self) -> str:
        if self.close is None:
            return f"{self.name} — investment committee"
        return f"{self.name} — investment committee (close {self.close.isoformat()})"

    def as_text(self) -> str:
        return _render_text(self)

    def as_markdown(self) -> str:
        return _render_markdown(self)


# -- Assembly ------------------------------------------------------------


def prepare(deal: Deal, *, breakevens: bool = True) -> Report:
    """Run every layer once and lay the answers out as a memo.

    ``breakevens`` is separable because it is the expensive part: each crossing
    costs a bounded number of full engine runs, and a caller that only wants
    the base case should not pay for the boundary of it.
    """
    case = Case.run(deal)
    sections = [
        _transaction(deal, case),
        _operating(deal, case),
        _schedule(deal, case),
    ]
    if case.covenants is not None:
        sections.append(_covenants(case.covenants))
    sections.append(_exit(case.outcome))
    if deal.has_acquisitions:
        sections.append(_acquisitions(deal, case))
    if case.outcome.was_recapitalised:
        sections.append(_recapitalisation(deal, case))
    if case.outcome.incentive is not None:
        sections.append(_incentive(case.outcome.incentive))
    sections.append(_bridge(case.outcome))
    if breakevens:
        sections.append(_boundary(deal, case))
    return Report(name=deal.name, close=deal.close_date, sections=tuple(sections))


def _amount(value: Money) -> str:
    return f"{quantize(value, 1):,}"


def _turns(value: Money) -> str:
    return format_value(Unit.TURNS, value)


def _percent(value: Money) -> str:
    return format_value(Unit.RATE, value)


def _rate(value: float | None) -> str:
    return _NONE if value is None else format_value(Unit.RATE, money(repr(value)))


def _multiple(value: Money | None) -> str:
    return _NONE if value is None else format_value(Unit.MULTIPLE, value)


def _transaction(deal: Deal, case: Case) -> Section:
    transaction = deal.transaction
    valuation = transaction.valuation
    opening_cash = case.schedule.opening_cash
    net_debt = transaction.total_debt - opening_cash
    leverage = safe_div(net_debt, valuation.ltm_ebitda, default=ZERO)
    gross = safe_div(transaction.total_debt, valuation.ltm_ebitda, default=ZERO)

    return Section(
        title="The transaction",
        summary=(
            f"{_amount(valuation.ltm_ebitda)} of LTM EBITDA at "
            f"{_turns(valuation.entry_multiple)}, funded at {_turns(gross)} of "
            f"gross debt and {_turns(leverage)} net of the cash left on the "
            f"balance sheet."
        ),
        lines=(
            Line("LTM EBITDA", _amount(valuation.ltm_ebitda)),
            Line("Entry multiple", _turns(valuation.entry_multiple)),
            Line("Enterprise value", _amount(valuation.enterprise_value)),
            Line(
                "Equity purchase price",
                _amount(valuation.equity_purchase_price),
                "after repaying the target's own debt",
            ),
            Line("Debt raised", _amount(transaction.total_debt), "at face"),
            Line("Rollover equity", _amount(transaction.rollover_equity)),
            Line(
                "Sponsor equity",
                _amount(transaction.sponsor_equity),
                "the plug that balances the funding table",
            ),
            Line("Fees and issue discount", _amount(transaction.total_fees + transaction.original_issue_discount)),
            Line("Cash at close", _amount(opening_cash)),
            Line("Net leverage at close", _turns(leverage)),
        ),
        table=Table(
            headings=("Tranche", "Face", "Proceeds", "Turns"),
            rows=tuple(
                (
                    tranche.name,
                    _amount(tranche.face),
                    _amount(tranche.proceeds),
                    _turns(
                        safe_div(tranche.face, valuation.ltm_ebitda, default=ZERO)
                    ),
                )
                for tranche in transaction.debt
            ),
        ),
    )


def _operating(deal: Deal, case: Case) -> Section:
    model = case.model
    first, last = model[0], model[-1]
    years = _years(deal, len(model))
    # Compounded from the position at close rather than from the first
    # projected period. First-to-last across five periods spans four years of
    # growth, and reporting it over five understates the case by about a fifth
    # of itself - the same off-by-one that flatters a deal in a pitch.
    opening = deal.opening_revenue
    growth = (
        _growth(opening, last.revenue, years) if opening is not None else _NONE
    )
    ebitda_growth = _growth(
        deal.transaction.valuation.ltm_ebitda, last.ebitda, years
    )
    margin_move = last.ebitda_margin - first.ebitda_margin

    return Section(
        title="The operating case",
        summary=(
            f"Revenue compounds at {growth} from close and EBITDA at "
            f"{ebitda_growth}, with margin moving {_points(margin_move)} across "
            f"the projected periods to end at {_percent(last.ebitda_margin)}."
        ),
        table=Table(
            headings=(
                "Period",
                "Revenue",
                "EBITDA",
                "Margin",
                "Capex",
                "Cash tax",
                "Unlevered FCF",
            ),
            rows=tuple(
                (
                    period.period.end.isoformat(),
                    _amount(period.revenue),
                    _amount(period.ebitda),
                    _percent(period.ebitda_margin),
                    _amount(period.capital_expenditure),
                    _amount(period.tax.cash_tax),
                    _amount(period.unlevered_free_cash_flow),
                )
                for period in model
            ),
        ),
        lines=(
            Line(
                "Cumulative unlevered free cash flow",
                _amount(model.total_unlevered_free_cash_flow),
            ),
            Line(
                "Cash conversion, final period",
                _percent(last.cash_conversion),
                "unlevered free cash flow over EBITDA",
            ),
        ),
    )


def _schedule(deal: Deal, case: Case) -> Section:
    schedule = case.schedule
    first, last = schedule[0], schedule[-1]
    # Two different figures, and the difference is the point. Repayments are
    # what left the business; the net movement is that less the interest that
    # accrued in kind and grew the balance while nobody paid it.
    repaid = sum(
        (p.mandatory_repayment + p.sweep_repayment for p in schedule), ZERO
    )
    drawn = sum((p.revolver_draw for p in schedule), ZERO)
    net_movement = first.opening_debt - last.closing_debt
    # Leverage at close is quoted on LTM EBITDA, which is what the deal was
    # priced against, and at exit on exit EBITDA. Anything else has the two
    # ends of the same sentence measured against different denominators.
    entry_leverage = safe_div(
        first.opening_debt - schedule.opening_cash,
        deal.transaction.valuation.ltm_ebitda,
        default=ZERO,
    )
    exit_leverage = safe_div(last.net_debt, case.model.exit_ebitda, default=ZERO)

    notes = []
    drawing = [p for p in schedule if p.revolver_draw > 0]
    if drawing:
        last_draw = drawing[-1].index
        notes.append(
            f"The revolver is drawn through period {last_draw}: until then the "
            f"business does not cover its own interest and amortisation out of "
            f"cash flow, and the facility is carrying the difference."
        )
    if any(not p.is_funded for p in schedule):
        first_gap = next(p for p in schedule if not p.is_funded)
        notes.append(
            f"Period {first_gap.index} does not fund itself: the case is short "
            f"{_amount(first_gap.funding_shortfall)} after the revolver."
        )
    elif any(not p.meets_minimum_cash for p in schedule):
        first_low = next(p for p in schedule if not p.meets_minimum_cash)
        notes.append(
            f"Period {first_low.index} closes below the minimum cash balance by "
            f"{_amount(first_low.cash_below_minimum)}, which is a conversation "
            f"rather than an unpaid bill."
        )

    return Section(
        title="The debt schedule",
        summary=(
            f"{_amount(repaid)} repaid across the hold against "
            f"{_amount(schedule.total_pik_interest)} accrued in kind and "
            f"{_amount(drawn)} drawn on the revolver, a net reduction of "
            f"{_amount(net_movement)}. Net leverage goes from "
            f"{_turns(entry_leverage)} on LTM EBITDA to {_turns(exit_leverage)} "
            f"on exit EBITDA."
        ),
        table=Table(
            headings=(
                "Period",
                "Opening",
                "Interest",
                "Mandatory",
                "Sweep",
                "Closing",
                "Net leverage",
            ),
            rows=tuple(
                (
                    period.period.end.isoformat(),
                    _amount(period.opening_debt),
                    _amount(period.cash_interest + period.pik_interest),
                    _amount(period.mandatory_repayment),
                    _amount(period.sweep_repayment),
                    _amount(period.closing_debt),
                    _turns(
                        safe_div(period.net_debt, case.model[i].ebitda, default=ZERO)
                    ),
                )
                for i, period in enumerate(schedule)
            ),
        ),
        lines=(
            Line("Cash interest paid", _amount(schedule.total_cash_interest)),
            Line(
                "Interest accrued in kind",
                _amount(schedule.total_pik_interest),
                "owed at exit rather than paid during the hold",
            ),
            Line("Repayments", _amount(repaid), "mandatory and swept"),
            Line("Drawn on the revolver", _amount(drawn)),
            Line(
                "Net reduction in debt",
                _amount(net_movement),
                "repayments less what accrued in kind and was drawn",
            ),
            Line("Cash at exit", _amount(last.closing_cash)),
        ),
        notes=tuple(notes),
    )


def _covenants(report: CovenantReport) -> Section:
    tightest = report.tightest
    breach = report.first_breach

    if breach is not None:
        summary = (
            f"{breach.covenant} breaches in period {breach.index}, at "
            f"{_turns(breach.actual)} against a threshold of "
            f"{_turns(breach.threshold)}."
            if breach.actual is not None
            else f"{breach.covenant} breaches in period {breach.index}."
        )
    elif tightest is not None and tightest.ebitda_cushion is not None:
        summary = (
            f"Every test is met. The structure is tightest against "
            f"{tightest.covenant} in period {tightest.index}, where EBITDA "
            f"could fall {_percent(tightest.ebitda_cushion)} before it trips."
        )
    else:
        summary = "Every test is met, and none of them is measurable on this case."

    rows = []
    for covenant in report.covenants:
        observations = [o for o in report.for_covenant(covenant.name) if o.tested]
        if not observations:
            rows.append((covenant.name, str(covenant.measure), _NONE, _NONE, "not tested"))
            continue
        worst = min(
            observations,
            key=lambda o: o.ebitda_cushion if o.ebitda_cushion is not None else ZERO,
        )
        rows.append(
            (
                covenant.name,
                str(covenant.measure),
                _turns(worst.threshold),
                _NONE if worst.actual is None else _turns(worst.actual),
                (
                    f"breaches in period {worst.index}"
                    if worst.breached
                    else f"{_percent(worst.ebitda_cushion)} of cushion in period {worst.index}"
                    if worst.ebitda_cushion is not None
                    else f"tightest in period {worst.index}"
                ),
            )
        )

    return Section(
        title="Covenants",
        summary=summary,
        table=Table(
            headings=("Covenant", "Measure", "Threshold", "Actual", "At its tightest"),
            rows=tuple(rows),
            align=("l", "l", "r", "r", "l"),
        ),
    )


def _exit(outcome: Outcome) -> Section:
    valuation = outcome.valuation
    rows = []
    for row in outcome:
        rows.append(
            (
                row.name,
                str(row.security.kind),
                _amount(row.invested),
                _amount(row.proceeds),
                _multiple(row.moic),
                _rate(row.irr) if row.irr is not None else (row.irr_note or _NONE),
            )
        )
    rows.append(
        (
            "Total",
            "",
            _amount(outcome.invested),
            _amount(outcome.proceeds),
            _multiple(outcome.moic),
            _rate(outcome.irr),
        )
    )

    notes = []
    for row in outcome:
        if row.shortfall > 0:
            notes.append(
                f"{row.name} is left {_amount(row.shortfall)} short of its "
                f"preferred claim."
            )
    if valuation.is_wiped_out:
        notes.append(
            "The business is worth less than it owes, so the equity is wiped "
            "out and the lenders take what there is."
        )

    return Section(
        title="The exit",
        summary=(
            f"Sold at {_turns(valuation.multiple)} on "
            f"{_amount(valuation.ebitda)} of EBITDA, leaving "
            f"{_amount(valuation.equity_value)} of equity value after "
            f"{_amount(valuation.net_debt)} of net debt and the cost of sale. "
            f"The equity returns {_multiple(outcome.moic)} and "
            f"{_rate(outcome.irr)} over "
            f"{quantize(outcome.holding_period_years, 2)} years."
        ),
        lines=(
            Line("Exit EBITDA", _amount(valuation.ebitda)),
            Line("Exit multiple", _turns(valuation.multiple)),
            Line("Enterprise value", _amount(valuation.enterprise_value)),
            Line("Net debt", _amount(valuation.net_debt)),
            Line("Cost of sale", _amount(valuation.fees)),
            Line("Equity value", _amount(valuation.equity_value)),
            Line("Net leverage at exit", _turns(valuation.exit_leverage)),
        ),
        table=Table(
            headings=("Security", "Kind", "Invested", "Proceeds", "MoIC", "IRR"),
            rows=tuple(rows),
            align=("l", "l", "r", "r", "r", "r"),
        ),
        notes=tuple(notes),
    )


def _acquisitions(deal: Deal, case: Case) -> Section:
    """What was bought, what it blended the entry down to, and whether it paid.

    The counterfactual is the platform on its own — same structure, same
    operating case, no purchases — and it is the only way to answer the question
    the strategy is judged on. A buy-and-build reported against itself always
    looks good: earnings went up. Reported against the platform it was built on,
    it has to show that the earnings arrived for less than they were worth, that
    the debt raised to buy them was serviced, and that what is left over after
    both is larger than doing nothing.

    Not every roll-up clears that. One that buys three turns below its own
    multiple and spends two of them on fees, interest and integration has
    created a bigger business and a worse return, and this is the section where
    that becomes visible.
    """
    blended = deal.blended_entry
    schedule = case.schedule
    outcome = case.outcome

    platform = Case.run(dataclasses.replace(deal, acquisitions=()))
    moved_moic = _delta_multiple(outcome.moic, platform.outcome.moic)
    moved_irr = _delta_rate(outcome.irr, platform.outcome.irr)

    rows = tuple(
        (
            add_on.label,
            f"P{add_on.period}",
            _amount(add_on.ebitda),
            _multiple(add_on.multiple),
            _amount(add_on.enterprise_value),
            _amount(add_on.face),
            _amount(add_on.from_cash),
        )
        for add_on in deal.acquisitions
    )

    lines = [
        Line("Businesses acquired", str(len(deal.acquisitions))),
        Line("EBITDA acquired", _amount(blended.acquired_ebitda), "run-rate at purchase"),
        Line("Enterprise value paid", _amount(blended.acquired_enterprise_value)),
        Line(
            "Capital deployed",
            _amount(blended.capital_deployed),
            "platform and add-ons, fees included",
        ),
        Line("Funded with new debt", _amount(schedule.total_acquisition_debt)),
        Line("Funded from cash", _amount(schedule.total_acquisition_from_cash)),
        Line("Platform multiple", _multiple(blended.platform_multiple)),
        Line("Blended multiple", _multiple(blended.blended_multiple)),
        Line(
            "After synergies",
            _multiple(blended.synergised_multiple),
            "earnings not yet earned",
        ),
        Line("On capital deployed", _multiple(blended.all_in_multiple)),
        Line("Money multiple, as run", _multiple(outcome.moic)),
        Line("Money multiple, platform alone", _multiple(platform.outcome.moic)),
        Line("Rate of return, as run", _rate(outcome.irr)),
        Line("Rate of return, platform alone", _rate(platform.outcome.irr)),
    ]

    acquired = case.model.exit_acquired_ebitda
    notes = [
        f"{_amount(acquired)} of the {_amount(case.model.exit_ebitda)} of EBITDA "
        f"the exit is priced on was bought rather than built, which is "
        f"{_percent(safe_div(acquired, case.model.exit_ebitda, default=ZERO))} of "
        f"it. An exit multiple argued from the platform's own growth has to "
        f"carry that share too.",
        "Each purchase closes at a period end, so the debt raised for it is "
        "outstanding for a full period before any of the earnings it bought are "
        "recorded. Leverage measured at that boundary is therefore at its worst "
        "reading of the hold, and the covenant tests see it.",
    ]

    return Section(
        title="Bought during the hold",
        summary=(
            f"{len(deal.acquisitions)} acquisitions added "
            f"{_amount(blended.acquired_ebitda)} of run-rate EBITDA at "
            f"{_multiple(blended.blended_multiple)} blended against a platform "
            f"bought at {_multiple(blended.platform_multiple)}, "
            f"{_turns(blended.arbitrage)} of arbitrage. Against the platform run "
            f"on its own the money multiple moves {moved_moic} and the rate of "
            f"return moves {moved_irr}."
        ),
        lines=tuple(lines),
        table=Table(
            headings=(
                "Business",
                "Closes",
                "EBITDA",
                "Multiple",
                "Price",
                "New debt",
                "From cash",
            ),
            rows=rows,
            align=("l", "l", "r", "r", "r", "r", "r"),
        ),
        notes=tuple(notes),
    )


def _recapitalisation(deal: Deal, case: Case) -> Section:
    """What was paid out mid-hold, what it cost, and which measure noticed.

    The counterfactual costs a second run of the whole engine and is worth it.
    A recapitalisation reported on its own says the sponsor received some money
    early; reported against the same deal without it, it says what that was
    worth — and the two measures disagree in the direction that explains why
    anybody does this.
    """
    outcome = case.outcome
    schedule = case.schedule

    flat = Case.run(dataclasses.replace(deal, recapitalisations=()))
    moved_moic = _delta_multiple(outcome.moic, flat.outcome.moic)
    moved_irr = _delta_rate(outcome.irr, flat.outcome.irr)

    rows = tuple(
        (
            d.when.isoformat(),
            d.label,
            _amount(d.amount),
            f"{quantize(d.years, 2)}",
            _amount(d.to_preferred),
            _amount(d.to_common),
        )
        for d in outcome.distributions
    )

    lines = [
        Line("Distributed during the hold", _amount(outcome.interim)),
        Line("Incremental face raised", _amount(schedule.total_recapitalised)),
        Line(
            "Cost of raising it",
            _amount(sum((e.cost_of_raising for e in schedule.recapitalisations), ZERO)),
            "fees and issue discount",
        ),
        Line("Money multiple, as run", _multiple(outcome.moic)),
        Line("Money multiple, held flat", _multiple(flat.outcome.moic)),
        Line("Rate of return, as run", _rate(outcome.irr)),
        Line("Rate of return, held flat", _rate(flat.outcome.irr)),
    ]

    notes = []
    for event in schedule.recapitalisations:
        before, after = event.leverage_before, event.leverage_after
        if event.turns_added is not None and before is not None and after is not None:
            notes.append(
                f"{event.label} put {_turns(event.turns_added)} of leverage back "
                f"on, taking net debt from {_turns(before)} to {_turns(after)} of "
                f"EBITDA."
            )
    notes.append(
        "The plan settles against the exit alone. An interim distribution goes "
        "to the securities rather than being shared with the pool, though what "
        "the holders it watches have already received does count towards a "
        "ratchet hurdle."
    )

    return Section(
        title="Paid during the hold",
        summary=(
            f"{_amount(outcome.interim)} reached the equity before the exit, "
            f"funded by {_amount(schedule.total_recapitalised)} of new debt. The "
            f"money multiple moves {moved_moic} and the rate of return moves "
            f"{moved_irr}: the same money, banked earlier, less what the debt "
            f"cost to carry."
        ),
        lines=tuple(lines),
        table=Table(
            headings=("Date", "Payment", "Amount", "Years", "Preferred", "Common"),
            rows=rows,
            align=("l", "l", "r", "r", "r", "r"),
        ),
        notes=tuple(notes),
    )


def _delta_multiple(actual: Money | None, flat: Money | None) -> str:
    if actual is None or flat is None:
        return _NONE
    change = actual - flat
    return f"{'+' if change >= 0 else ''}{quantize(change, 2)}x"


def _delta_rate(actual: float | None, flat: float | None) -> str:
    if actual is None or flat is None:
        return _NONE
    change = actual - flat
    return f"{'+' if change >= 0 else ''}{change * 100:.1f}pp"


def _incentive(settled: PoolOutcome) -> Section:
    """The management plan: what it is worth, and what it cost the common."""
    pool = settled.pool
    notes: list[str] = []

    if not settled.exercised:
        notes.append(
            "The pool is out of the money at this exit. The options lapse, so "
            "management are paid nothing and the common are not diluted — which "
            "is the point of striking a plan rather than granting shares."
        )
    if settled.forfeited_share > 0:
        notes.append(
            f"{_percent(settled.forfeited_share)} of the pool had not vested by "
            f"the exit and is forfeited rather than accelerated."
        )
    if pool.ratchet is not None:
        watched = (
            ", ".join(pool.ratchet.measured_on)
            if pool.ratchet.measured_on
            else "the equity as a whole"
        )
        notes.append(
            f"The ratchet is measured on {watched}, and pays a marginal share "
            f"above each hurdle rather than a higher share of everything. That "
            f"is what keeps the sponsor's proceeds rising with the sale price at "
            f"every point, including the one where a hurdle is crossed."
        )

    lines = [
        Line("Pool, fully diluted", _percent(pool.share)),
        Line("Vested at exit", _percent(settled.vested)),
        Line("Residual before the plan", _amount(settled.residual)),
        Line("Strike paid in", _amount(settled.strike_paid)),
        Line("Pot divided", _amount(settled.pot)),
        Line("Share of the pot taken", _percent(settled.effective_share)),
        Line("Paid to management", _amount(settled.paid)),
    ]

    if pool.ratchet is not None:
        rows = tuple(
            (
                _turns(band.hurdle) if band.hurdle > 0 else "from the first pound",
                _percent(band.share),
            )
            for band in pool.ratchet
        )
        table: Table | None = Table(
            headings=("Above", "Marginal share"),
            rows=rows,
            align=("l", "r"),
        )
    else:
        table = None

    summary = (
        f"Management take {_amount(settled.paid)} out of "
        f"{_amount(settled.residual)} reaching the common, an effective "
        f"{_percent(settled.effective_share)} of the pot they share in. What "
        f"they are paid is what the common give up, to the penny."
    )
    if not settled.exercised:
        summary = (
            f"The plan pays nothing at this exit: a "
            f"{_percent(pool.share)} pool struck at "
            f"{_amount(pool.strike)} is not worth exercising against "
            f"{_amount(settled.residual)} of residual."
        )

    return Section(
        title=pool.name,
        summary=summary,
        lines=tuple(lines),
        table=table,
        notes=tuple(notes),
    )


def _bridge(outcome: Outcome) -> Section:
    attribution = outcome.attribution
    components = (
        ("EBITDA growth", attribution.ebitda_growth),
        ("Multiple change", attribution.multiple_change),
        ("Debt paydown", attribution.debt_paydown),
        ("Entry and exit costs", attribution.costs),
    )
    largest = max(components, key=lambda pair: abs(pair[1]))

    notes = []
    if not attribution.reconciles():
        notes.append(
            "The components do not sum to the change in equity value, which "
            "means the bridge is not describing this deal."
        )
    if attribution.floored > 0:
        notes.append(
            f"{_amount(attribution.floored)} of the loss falls on the lenders "
            f"rather than the shareholders, who owe nothing beyond their capital."
        )

    return Section(
        title="Where the value came from",
        summary=(
            f"{largest[0]} accounts for {_percent(abs(attribution.share(largest[1])))} "
            f"of the gross movement in equity value."
        ),
        table=Table(
            headings=("Source", "Value", "Share of gross"),
            rows=tuple(
                (label, _amount(amount), _percent(abs(attribution.share(amount))))
                for label, amount in components
            )
            + (
                ("Value created", _amount(attribution.total), ""),
            ),
        ),
        notes=tuple(notes),
    )


def _boundary(deal: Deal, case: Case) -> Section:
    """Where the case stops working, one dimension at a time."""
    questions: list[tuple[str, Dimension, Metric, Money, tuple[Money, Money]]] = [
        (
            "Exit multiple returning capital and no more",
            Dimension.EXIT_MULTIPLE,
            Metric.MOIC,
            ONE,
            EXIT_MULTIPLE_BRACKET,
        ),
        (
            "Exit multiple returning twice capital",
            Dimension.EXIT_MULTIPLE,
            Metric.MOIC,
            money(2),
            EXIT_MULTIPLE_BRACKET,
        ),
    ]
    if case.covenants is not None:
        questions.extend(
            [
                (
                    "Margin shift tripping the first covenant",
                    Dimension.EBITDA_MARGIN,
                    Metric.CUSHION,
                    ZERO,
                    MARGIN_SHIFT_BRACKET,
                ),
                (
                    "Opening leverage tripping the first covenant",
                    Dimension.LEVERAGE,
                    Metric.CUSHION,
                    ZERO,
                    LEVERAGE_BRACKET,
                ),
            ]
        )

    found: list[Breakeven] = []
    rows = []
    # Reasons go underneath rather than into a column. A sentence explaining
    # why a crossing does not exist is longer than every other cell in the
    # table put together, and putting it inline stretches the page to fit it.
    notes = [
        "Each figure is the crossing found by re-running the whole model along "
        "that assumption, holding everything else at the case above."
    ]
    for label, dimension, metric, target, (low, high) in questions:
        try:
            crossing = solve(
                deal, dimension, metric, target=target, low=low, high=high
            )
        except SensitivityError as exc:
            rows.append((label, _NONE))
            notes.append(f"{label}: {exc}.")
            continue
        found.append(crossing)
        rows.append((label, crossing.format()))
        if crossing.note:
            notes.append(f"{label}: {crossing.note}.")

    return Section(
        title="Where the case stops working",
        summary=_boundary_summary(deal, case, found),
        table=Table(
            headings=("Question", "Answer"),
            rows=tuple(rows),
            align=("l", "r"),
        ),
        notes=tuple(notes),
    )


def _years(deal: Deal, periods: int) -> Money:
    """The hold in years, from the grid's own frequency."""
    per_year = deal.grid.frequency.periods_per_year if deal.grid is not None else 1
    return money(periods) / money(per_year)


def _boundary_summary(deal: Deal, case: Case, found: Sequence[Breakeven]) -> str:
    """One sentence on how much room the entry price has, if it has any."""
    for crossing in found:
        if (
            crossing.dimension is Dimension.EXIT_MULTIPLE
            and crossing.metric is Metric.MOIC
            and crossing.target == ONE
            and crossing.value is not None
        ):
            entry = deal.transaction.valuation.entry_multiple
            room = entry - crossing.value
            if room >= 0:
                return (
                    f"The sponsor gets its capital back at "
                    f"{_turns(crossing.value)}, which is {_turns(room)} below "
                    f"the {_turns(entry)} paid going in."
                )
            return (
                f"The sponsor only gets its capital back at "
                f"{_turns(crossing.value)}, which is {_turns(-room)} above the "
                f"{_turns(entry)} paid going in: this case needs a buyer to pay "
                f"more than the sponsor did."
            )
    return "No break-even was found inside the brackets searched."


def _growth(begin: Money, end: Money, years: Money) -> str:
    try:
        return format_value(Unit.RATE, money(repr(cagr(begin, end, years))))
    except (ValueError, ArithmeticError):
        return _NONE


def _points(value: Money) -> str:
    return format_value(Unit.POINTS, value)


# -- Rendering -----------------------------------------------------------


def _widths(table: Table) -> list[int]:
    widths = [len(h) for h in table.headings]
    for row in table.rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    return widths


def _render_text(report: Report) -> str:
    out: list[str] = [report.title, "=" * len(report.title)]
    for section in report:
        out.append("")
        out.append(f"  {section.title}")
        out.append(f"  {'-' * len(section.title)}")
        if section.summary:
            out.append("")
            out.extend(f"  {line}" for line in _wrap(section.summary, 74))
        if section.lines:
            out.append("")
            label_width = max(len(line.label) for line in section.lines)
            value_width = max(len(line.value) for line in section.lines)
            for line in section.lines:
                note = f"   {line.note}" if line.note else ""
                out.append(
                    f"    {line.label:<{label_width}}  "
                    f"{line.value:>{value_width}}{note}".rstrip()
                )
        if section.table is not None and section.table.rows:
            out.append("")
            out.extend(_render_text_table(section.table))
        for note in section.notes:
            out.append("")
            out.extend(f"    {line}" for line in _wrap(note, 72))
    return "\n".join(out) + "\n"


def _render_text_table(table: Table) -> list[str]:
    widths = _widths(table)
    alignment = table.alignment
    out = [
        "    "
        + "  ".join(
            heading.ljust(widths[i]) if alignment[i] == "l" else heading.rjust(widths[i])
            for i, heading in enumerate(table.headings)
        ).rstrip()
    ]
    out.append("    " + "  ".join("-" * w for w in widths))
    for row in table.rows:
        out.append(
            "    "
            + "  ".join(
                cell.ljust(widths[i]) if alignment[i] == "l" else cell.rjust(widths[i])
                for i, cell in enumerate(row)
            ).rstrip()
        )
    return out


def _render_markdown(report: Report) -> str:
    out: list[str] = [f"# {report.title}"]
    for section in report:
        out.append("")
        out.append(f"## {section.title}")
        if section.summary:
            out.append("")
            out.append(section.summary)
        if section.lines:
            out.append("")
            out.append("| | |")
            out.append("|---|---:|")
            for line in section.lines:
                label = f"{line.label} <sub>{line.note}</sub>" if line.note else line.label
                out.append(f"| {label} | {line.value} |")
        if section.table is not None and section.table.rows:
            out.append("")
            out.extend(_render_markdown_table(section.table))
        for note in section.notes:
            out.append("")
            out.append(f"> {note}")
    return "\n".join(out) + "\n"


def _render_markdown_table(table: Table) -> list[str]:
    alignment = table.alignment
    rule = "|" + "|".join(
        "---" if a == "l" else "---:" for a in alignment
    ) + "|"
    out = ["| " + " | ".join(table.headings) + " |", rule]
    for row in table.rows:
        out.append("| " + " | ".join(row) + " |")
    return out


def _wrap(text: str, width: int) -> list[str]:
    """Wrap on spaces, without pulling in a dependency to do it.

    Long words are left long rather than broken. A number split across two
    lines is worse than a line that runs a little over.
    """
    words = text.split()
    if not words:
        return []
    lines = [words[0]]
    for word in words[1:]:
        if len(lines[-1]) + 1 + len(word) <= width:
            lines[-1] += " " + word
        else:
            lines.append(word)
    return lines
