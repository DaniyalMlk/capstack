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
