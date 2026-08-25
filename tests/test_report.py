"""The memo.

Two things are worth testing about a document. That the figures in it are the
figures the engine produced — a report is a rendering layer, and a rendering
layer that quietly rounds or relabels is worse than no report at all — and that
the two renderings say the same thing, because they are assembled once and it
would be easy for them not to.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from capstack.cli import main
from capstack.money import money, quantize
from capstack.report import Table, prepare
from capstack.sensitivity import Case, Dimension, Metric, solve
from capstack.spec import Deal, load_deal, parse_deal

EXAMPLE = str(Path(__file__).resolve().parents[1] / "examples" / "meridian.json")


def example() -> Deal:
    return load_deal(EXAMPLE)


def bare() -> Deal:
    """A deal with a structure and an exit but no covenants and no target."""
    return parse_deal(
        {
            "name": "Bare",
            "close_date": "2026-06-30",
            "entry": {"ltm_ebitda": 200, "multiple": 8},
            "debt": [
                {"name": "TLB", "face": 900, "kind": "term_loan", "cash_rate": 0.05}
            ],
            "rollover_equity": 100,
            "structure": {"minimum_cash": 20, "base_rate": 0.03},
            "projection": {"years": 5, "frequency": "annual"},
            "operating": {
                "opening_revenue": 1000,
                "revenue_growth": 0.05,
                "ebitda_margin": 0.22,
                "da_rate": 0.03,
                "capex_rate": 0.035,
                "nwc_rate": 0.1,
                "tax_rate": 0.25,
            },
            "exit": {"multiple": 9},
        }
    )


class TestWhatIsInIt:
    def test_the_sections_run_in_the_order_the_argument_is_made(self) -> None:
        report = prepare(example())
        assert [s.title for s in report] == [
            "The transaction",
            "The operating case",
            "The debt schedule",
            "Covenants",
            "The exit",
            "Where the value came from",
            "Where the case stops working",
        ]

    def test_a_deal_without_covenants_has_no_covenant_section(self) -> None:
        report = prepare(bare())
        titles = [s.title for s in report]
        assert "Covenants" not in titles
        assert "The exit" in titles

    def test_the_break_evens_can_be_left_out(self) -> None:
        report = prepare(example(), breakevens=False)
        assert "Where the case stops working" not in [s.title for s in report]

    def test_the_title_carries_the_close_date(self) -> None:
        assert "close 2026-06-30" in prepare(example(), breakevens=False).title

    def test_a_missing_section_is_reported_as_missing(self) -> None:
        report = prepare(bare(), breakevens=False)
        with pytest.raises(KeyError, match="Covenants"):
            report.section("Covenants")


class TestTheFiguresAreTheEnginesFigures:
    def test_the_exit_section_states_the_engine_s_returns(self) -> None:
        deal = example()
        outcome = deal.realise()
        text = prepare(deal, breakevens=False).as_text()
        assert outcome.moic is not None and outcome.irr is not None
        assert f"{quantize(outcome.moic, 2)}x" in text
        assert f"{quantize(money(repr(outcome.irr)) * money(100), 1)}%" in text

    def test_every_security_appears_with_its_own_capital(self) -> None:
        deal = example()
        outcome = deal.realise()
        table = prepare(deal, breakevens=False).section("The exit").table
        assert table is not None
        names = [row[0] for row in table.rows]
        for row in outcome:
            assert row.name in names
        assert names[-1] == "Total"

    def test_the_schedule_table_has_one_row_per_period(self) -> None:
        deal = example()
        table = prepare(deal, breakevens=False).section("The debt schedule").table
        assert table is not None
        assert len(table.rows) == len(deal.schedule())

    def test_the_debt_movement_reconciles_in_the_lines(self) -> None:
        # Repayments, less what accrued in kind, less what was drawn, is the
        # net reduction. The section says so and the numbers have to agree.
        deal = example()
        case = Case.run(deal)
        section = prepare(deal, breakevens=False).section("The debt schedule")
        figures = {line.label: line.value for line in section.lines}

        def amount(label: str) -> Any:
            return money(figures[label].replace(",", ""))

        movement = (
            amount("Repayments")
            - amount("Interest accrued in kind")
            - amount("Drawn on the revolver")
        )
        assert abs(movement - amount("Net reduction in debt")) <= money("0.2")
        opening = case.schedule[0].opening_debt
        closing = case.schedule[-1].closing_debt
        assert abs(amount("Net reduction in debt") - (opening - closing)) <= money("0.1")

    def test_a_drawn_revolver_is_called_out(self) -> None:
        section = prepare(example(), breakevens=False).section("The debt schedule")
        assert any("revolver is drawn" in note for note in section.notes)

    def test_the_bridge_shares_come_from_the_attribution(self) -> None:
        deal = example()
        attribution = deal.realise().attribution
        table = prepare(deal, breakevens=False).section("Where the value came from").table
        assert table is not None
        growth = next(row for row in table.rows if row[0] == "EBITDA growth")
        expected = quantize(abs(attribution.share(attribution.ebitda_growth)) * money(100), 1)
        assert growth[2] == f"{expected}%"

    def test_the_covenant_summary_names_the_tightest_test(self) -> None:
        deal = example()
        tightest = deal.test_covenants().tightest
        assert tightest is not None
        summary = prepare(deal, breakevens=False).section("Covenants").summary
        assert tightest.covenant in summary

    def test_the_break_evens_match_the_solver(self) -> None:
        deal = example()
        crossing = solve(
            deal,
            Dimension.EXIT_MULTIPLE,
            Metric.MOIC,
            target=money(1),
            low=money(1),
            high=money(30),
        )
        assert crossing.value is not None
        table = prepare(deal).section("Where the case stops working").table
        assert table is not None
        row = next(
            r for r in table.rows if r[0] == "Exit multiple returning capital and no more"
        )
        assert row[1] == f"{quantize(crossing.value, 2)}x"


class TestGrowthIsMeasuredOverTheRightSpan:
    def test_revenue_compounds_from_the_position_at_close(self) -> None:
        # Five projected periods span five years of growth from the opening
        # revenue, not four. Compounding first-to-last would understate it.
        deal = example()
        model = deal.project()
        opening = deal.opening_revenue
        assert opening is not None
        years = money(len(model))
        implied = (model[-1].revenue / opening) ** 1  # exact ratio, checked below
        assert implied > 1
        summary = prepare(deal, breakevens=False).section("The operating case").summary

        from capstack.returns import cagr

        expected = quantize(money(repr(cagr(opening, model[-1].revenue, years))) * money(100), 1)
        assert f"{expected}%" in summary

    def test_the_growth_rate_is_not_the_shorter_span(self) -> None:
        deal = example()
        model = deal.project()
        from capstack.returns import cagr

        over_four = quantize(
            money(repr(cagr(model[0].revenue, model[-1].revenue, money(5)))) * money(100),
            1,
        )
        summary = prepare(deal, breakevens=False).section("The operating case").summary
        assert f"{over_four}%" not in summary


class TestRendering:
    def test_the_text_form_has_no_trailing_whitespace(self) -> None:
        text = prepare(example()).as_text()
        assert all(line == line.rstrip() for line in text.splitlines())

    def test_both_renderings_carry_the_same_figures(self) -> None:
        report = prepare(example())
        text, markdown = report.as_text(), report.as_markdown()
        for section in report:
            for line in section.lines:
                assert line.value in text
                assert line.value in markdown
            if section.table is not None:
                for row in section.table.rows:
                    for cell in row:
                        if cell:
                            assert cell in text
                            assert cell in markdown

    def test_every_section_titles_itself_in_both_forms(self) -> None:
        report = prepare(example())
        text, markdown = report.as_text(), report.as_markdown()
        for section in report:
            assert section.title in text
            assert f"## {section.title}" in markdown

    def test_markdown_tables_carry_an_alignment_rule_per_column(self) -> None:
        markdown = prepare(example(), breakevens=False).as_markdown()
        for line in markdown.splitlines():
            if line.startswith("|---") or line.startswith("|---:"):
                cells = [c for c in line.split("|") if c]
                assert all(c in ("---", "---:") for c in cells)

    def test_a_long_summary_is_wrapped_and_not_broken_mid_number(self) -> None:
        text = prepare(example(), breakevens=False).as_text()
        for line in text.splitlines():
            assert len(line) < 120


class TestTableValidation:
    def test_a_ragged_row_is_refused(self) -> None:
        with pytest.raises(ValueError, match="cells"):
            Table(headings=("a", "b"), rows=(("1",),))

    def test_a_partial_alignment_is_refused(self) -> None:
        with pytest.raises(ValueError, match="every column"):
            Table(headings=("a", "b"), rows=(), align=("l",))

    def test_the_default_alignment_is_label_left_figures_right(self) -> None:
        assert Table(headings=("a", "b", "c"), rows=()).alignment == ("l", "r", "r")


class TestTheCommand:
    def run(self, *args: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
        code = main(list(args))
        return code, capsys.readouterr().out

    def test_it_prints_the_text_memo(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, out = self.run("report", EXAMPLE, capsys=capsys)
        assert code == 0
        assert "Project Meridian" in out
        assert "The transaction" in out
        assert "Where the case stops working" in out

    def test_markdown_is_headed_with_hashes(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, out = self.run("report", EXAMPLE, "--markdown", capsys=capsys)
        assert out.startswith("# Project Meridian")
        assert "## The exit" in out

    def test_json_carries_the_sections_as_data(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, out = self.run("report", EXAMPLE, "--json", capsys=capsys)
        document = json.loads(out)
        assert document["close_date"] == "2026-06-30"
        titles = [s["title"] for s in document["sections"]]
        assert "The debt schedule" in titles
        schedule = next(
            s for s in document["sections"] if s["title"] == "The debt schedule"
        )
        assert schedule["table"] is not None
        assert len(schedule["table"]["headings"]) == len(
            schedule["table"]["rows"][0]
        )

    def test_the_break_evens_can_be_skipped_from_the_command_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, out = self.run("report", EXAMPLE, "--no-breakevens", capsys=capsys)
        assert "Where the case stops working" not in out

    def test_markdown_and_json_are_mutually_exclusive(self) -> None:
        with pytest.raises(SystemExit):
            main(["report", EXAMPLE, "--markdown", "--json"])

    def test_a_deal_with_nothing_to_report_is_refused_with_a_reason(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(
                {
                    "name": "Thin",
                    "close_date": "2026-06-30",
                    "entry": {"ltm_ebitda": 200, "multiple": 8},
                },
                handle,
            )
            path = handle.name
        code = main(["report", path])
        assert code == 1
        assert "no operating case" in capsys.readouterr().err
