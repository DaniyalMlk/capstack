import json
from datetime import date
from pathlib import Path

import pytest

from capstack.cli import _parse_flow, build_parser, main


class TestFlowParsing:
    def test_date_and_amount(self) -> None:
        flow = _parse_flow("2026-06-30:-420000000")
        assert flow.when == date(2026, 6, 30)
        assert flow.amount == -420_000_000
        assert flow.label == ""

    def test_optional_label(self) -> None:
        assert _parse_flow("2026-06-30:-100:sponsor equity").label == "sponsor equity"

    def test_thousands_separators_are_tolerated(self) -> None:
        assert _parse_flow("2026-06-30:-1,250,000").amount == -1_250_000
        assert _parse_flow("2026-06-30:-1_250_000").amount == -1_250_000

    def test_decimal_amount_stays_exact(self) -> None:
        assert str(_parse_flow("2026-06-30:1234.56").amount) == "1234.56"

    def test_missing_amount_is_rejected(self) -> None:
        with pytest.raises(Exception, match="DATE:AMOUNT"):
            _parse_flow("2026-06-30")

    def test_bad_date_is_rejected(self) -> None:
        with pytest.raises(Exception, match="not a date"):
            _parse_flow("30-06-2026:100")

    def test_bad_amount_is_rejected(self) -> None:
        with pytest.raises(Exception, match="not an amount"):
            _parse_flow("2026-06-30:lots")


class TestReturnsCommand:
    def test_reports_irr_and_moic(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(["returns", "2026-06-30:-420000000", "2031-06-30:1134000000"])
        out = capsys.readouterr().out
        assert code == 0
        assert "2.70x" in out
        assert "21.9" in out

    def test_json_output_is_machine_readable(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(["returns", "2026-01-01:-1000", "2031-01-01:2500", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["moic"] == pytest.approx(2.5)
        assert payload["irr"] == pytest.approx(0.2007, abs=1e-3)
        assert len(payload["flows"]) == 2

    def test_an_ambiguous_stream_reports_the_candidates_rather_than_a_number(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(
            ["returns", "2026-01-01:-1000", "2027-01-01:2500", "2028-01-01:-1560", "--json"]
        )
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["irr"] is None
        assert payload["irr_candidates"] == pytest.approx([0.20, 0.30], abs=1e-6)

    def test_a_stream_with_no_sign_change_says_so(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["returns", "2026-01-01:-1000", "2027-01-01:-500", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["irr"] is None
        assert "does not change sign" in payload["irr_note"]

    def test_convention_is_selectable(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(
            ["returns", "2026-01-01:-1000", "2031-01-01:2000", "--convention", "30/360 US", "--json"]
        )
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["convention"] == "30/360 US"
        assert payload["holding_period_years"] == pytest.approx(5.0)

    def test_labels_are_shown(self, capsys: pytest.CaptureFixture[str]) -> None:
        main(["returns", "2026-01-01:-1000:equity", "2031-01-01:2500:exit"])
        out = capsys.readouterr().out
        assert "equity" in out and "exit" in out

    def test_dates_are_not_duplicated_when_there_is_no_label(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["returns", "2026-01-01:-1000", "2031-01-01:2500"])
        first_line = capsys.readouterr().out.splitlines()[1]
        assert first_line.count("2026-01-01") == 1

    def test_a_single_flow_is_reported_without_a_rate(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["returns", "2026-01-01:-1000", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["irr"] is None

    def test_no_capital_invested_exits_with_an_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["returns", "2026-01-01:1000", "2031-01-01:2500"])
        assert code == 2
        assert "no capital" in capsys.readouterr().err


class TestParser:
    def test_a_command_is_required(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_unknown_command_is_rejected(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["nonsense"])


class TestDealCommand:
    @pytest.fixture()
    def example(self) -> str:
        return str(Path(__file__).resolve().parents[1] / "examples" / "meridian.json")

    def test_prints_a_balanced_funding_table(
        self, example: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["deal", example])
        out = capsys.readouterr().out
        assert code == 0
        assert "Project Meridian" in out
        assert out.count("2,974.14") == 2  # the two totals agree
        assert "7.71x" in out

    def test_json_output(self, example: str, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(["deal", example, "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["balanced"] is True
        assert payload["total_sources"] == payload["total_uses"]
        assert payload["metrics"]["sponsor_equity"] == "994.14"
        assert payload["metrics"]["overfunded"] is False

    def test_sources_and_uses_columns_line_up(
        self, example: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["deal", example])
        out = capsys.readouterr().out
        funding = out.split("Entry metrics")[0]
        totals = [ln for ln in funding.splitlines() if ln.strip().startswith("Total ")]
        assert len(totals) == 2
        # Identical strings means identical column positions.
        assert totals[0] == totals[1]

    def test_a_missing_file_exits_with_one(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["deal", "/nonexistent/deal.json"])
        assert code == 1
        assert "cannot read" in capsys.readouterr().err

    def test_a_malformed_file_exits_with_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{not json}", encoding="utf-8")
        code = main(["deal", str(path)])
        assert code == 1
        assert "invalid JSON" in capsys.readouterr().err

    def test_an_overfunded_deal_is_called_out(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "over.json"
        path.write_text(
            json.dumps(
                {
                    "name": "Overreach",
                    "entry": {"ltm_ebitda": 100, "multiple": 5},
                    "debt": [{"name": "TLB", "face": 600}],
                }
            ),
            encoding="utf-8",
        )
        code = main(["deal", str(path)])
        out = capsys.readouterr().out
        assert code == 0
        assert "distribution at close" in out


class TestProjectCommand:
    @pytest.fixture()
    def example(self) -> str:
        return str(Path(__file__).resolve().parents[1] / "examples" / "meridian.json")

    def test_prints_the_operating_case(
        self, example: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["project", example])
        out = capsys.readouterr().out
        assert code == 0
        assert "Unlevered free cash flow" in out
        assert "1,605.80" in out  # period one revenue
        assert "119.59" in out  # period one unlevered FCF

    def test_outflows_are_shown_negative(
        self, example: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["project", example])
        out = capsys.readouterr().out
        capex = next(ln for ln in out.splitlines() if "Capital expenditure" in ln)
        assert "-77.08" in capex

    def test_every_period_gets_a_column(
        self, example: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["project", example])
        out = capsys.readouterr().out
        heading = next(ln for ln in out.splitlines() if "P1" in ln)
        assert heading.split() == ["P1", "P2", "P3", "P4", "P5"]

    def test_json_output(self, example: str, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(["project", example, "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert len(payload["periods"]) == 5
        assert payload["periods"][0]["revenue"] == "1605.80"
        assert payload["periods"][0]["unlevered_free_cash_flow"] == "119.59"
        assert payload["totals"]["exit_ebitda"] == "372.09"

    def test_a_deal_without_an_operating_case_explains_itself(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "funding-only.json"
        path.write_text(
            json.dumps({"name": "Bare", "entry": {"ltm_ebitda": 100, "multiple": 10}}),
            encoding="utf-8",
        )
        code = main(["project", str(path)])
        assert code == 1
        assert "no operating case" in capsys.readouterr().err

    def test_a_loss_making_case_reports_carried_forward_losses(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "loss.json"
        path.write_text(
            json.dumps(
                {
                    "name": "Underwater",
                    "close_date": "2026-06-30",
                    "entry": {"ltm_ebitda": 100, "multiple": 10},
                    "projection": {"years": 3},
                    "operating": {
                        "opening_revenue": 1000,
                        "revenue_growth": 0.02,
                        "ebitda_margin": 0.01,
                        "da_rate": 0.05,
                        "tax_rate": 0.25,
                    },
                }
            ),
            encoding="utf-8",
        )
        code = main(["project", str(path)])
        out = capsys.readouterr().out
        assert code == 0
        assert "Losses carried forward" in out


class TestBalanceCommand:
    @pytest.fixture()
    def example(self) -> str:
        return str(Path(__file__).resolve().parents[1] / "examples" / "meridian.json")

    def test_prints_a_sheet_that_balances(
        self, example: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["balance", example])
        out = capsys.readouterr().out
        assert code == 0
        assert "opening balance sheet" in out
        # Total assets and liabilities-and-equity are the same figure, printed twice.
        assert out.count("3,397.00") == 2
        assert "Goodwill" in out

    def test_expensed_costs_print_as_a_deduction(
        self, example: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["balance", example])
        out = capsys.readouterr().out
        assert code == 0
        assert "-57.14" in out

    def test_json_output(self, example: str, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(["balance", example, "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["balanced"] is True
        assert payload["total_assets"] == payload["total_liabilities_and_equity"]
        assert payload["assets"]["goodwill"] == "1610.00"
        assert payload["liabilities"]["deferred_tax_liability"] == "45.00"

    def test_a_deal_without_a_target_block_says_so(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "deal.json"
        path.write_text(json.dumps({"entry": {"ltm_ebitda": 100, "multiple": 10}}), "utf-8")
        code = main(["balance", str(path)])
        assert code == 1
        assert 'add a "target" block' in capsys.readouterr().err

    def test_a_contradictory_book_position_is_reported(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "deal.json"
        path.write_text(
            json.dumps(
                {
                    "entry": {"ltm_ebitda": 100, "multiple": 10, "existing_debt": 150},
                    "target": {"total_assets": 400, "total_liabilities": 100},
                }
            ),
            "utf-8",
        )
        code = main(["balance", str(path)])
        assert code == 2
        assert "of debt, which is more" in capsys.readouterr().err


class TestScheduleCommand:
    @pytest.fixture()
    def example(self) -> str:
        return str(Path(__file__).resolve().parents[1] / "examples" / "meridian.json")

    def test_prints_the_schedule(
        self, example: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["schedule", example])
        out = capsys.readouterr().out
        assert code == 0
        assert "debt schedule" in out
        assert "Cash sweep" in out
        assert "Closing balances" in out
        # Interest and repayments print as outflows.
        assert "-141.32" in out

    def test_leverage_falls_across_the_hold(
        self, example: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["schedule", example])
        out = capsys.readouterr().out
        assert "7.71x" in out  # entry
        assert "4.80x" in out  # exit, under a sweep that steps down as leverage falls

    def test_json_output(self, example: str, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(["schedule", example, "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["funded"] is True
        assert len(payload["periods"]) == 5
        assert payload["exit_leverage"] < payload["entry_leverage"]
        assert set(payload["periods"][0]["balances"]) == set(payload["tranches"])
        assert payload["max_iterations"] >= 1

    def test_a_deal_without_a_structure_says_so(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "deal.json"
        path.write_text(json.dumps({"entry": {"ltm_ebitda": 100, "multiple": 10}}), "utf-8")
        code = main(["schedule", str(path)])
        assert code == 1
        assert 'add a "structure" block' in capsys.readouterr().err

    def test_a_structure_that_does_not_fund_itself_is_called_out(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "deal.json"
        path.write_text(
            json.dumps(
                {
                    "name": "Overlevered",
                    "close_date": "2026-06-30",
                    "entry": {"ltm_ebitda": 100, "multiple": 10},
                    "debt": [
                        {"name": "Notes", "kind": "notes", "face": 900, "cash_rate": 0.12}
                    ],
                    "structure": {"minimum_cash": 0},
                    "projection": {"years": 2},
                    "operating": {
                        "opening_revenue": 500,
                        "revenue_growth": 0.02,
                        "ebitda_margin": 0.15,
                        "da_rate": 0.03,
                        "capex_rate": 0.04,
                        "nwc_rate": 0.10,
                        "tax_rate": 0.25,
                    },
                }
            ),
            "utf-8",
        )
        code = main(["schedule", str(path)])
        out = capsys.readouterr().out
        assert code == 0
        assert "does not fund itself" in out
