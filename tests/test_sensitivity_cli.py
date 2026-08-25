"""The sensitivity table as it reaches a reader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from capstack.cli import main
from capstack.money import money
from capstack.spec import load_deal

EXAMPLE = str(Path(__file__).resolve().parents[1] / "examples" / "meridian.json")


def run(*args: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


class TestTheTable:
    def test_it_prints_a_grid_of_the_right_shape(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, _ = run(
            "sensitivity",
            EXAMPLE,
            "--rows",
            "exit-multiple:9,10,11,12",
            "--columns",
            "entry-multiple:10.5,11.5,12.5",
            "--metric",
            "moic",
            capsys=capsys,
        )
        assert code == 0
        assert "Project Meridian - sensitivity" in out
        assert "MoIC" in out
        assert "entry multiple across, exit multiple down" in out
        body = [line for line in out.splitlines() if line.strip().startswith(("9.00x", "10.00x", "11.00x", "12.00x"))]
        assert len(body) == 4
        for line in body:
            assert line.count("x") >= 4  # the stub plus three cells

    def test_the_base_case_is_marked_on_both_axes(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, out, _ = run(
            "sensitivity",
            EXAMPLE,
            "--rows",
            "exit-multiple:10,11,12",
            "--columns",
            "entry-multiple:11,11.5,12",
            "--metric",
            "moic",
            capsys=capsys,
        )
        assert "11.50x*" in out  # the entry multiple the file describes
        assert "11.00x*" in out  # the exit multiple it describes
        assert "the assumption the file describes" in out

    def test_the_headline_figure_appears_as_the_base(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        outcome = load_deal(EXAMPLE).realise()
        assert outcome.moic is not None
        _, out, _ = run(
            "sensitivity",
            EXAMPLE,
            "--rows",
            "exit-multiple:11",
            "--columns",
            "entry-multiple:11.5",
            "--metric",
            "moic",
            capsys=capsys,
        )
        assert f"base case {outcome.moic:.2f}x" in out

    def test_a_breach_is_marked_and_explained(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, out, _ = run(
            "sensitivity",
            EXAMPLE,
            "--rows",
            "ebitda-margin:-6,0",
            "--columns",
            "exit-year:4,5",
            capsys=capsys,
        )
        assert "!" in out
        assert "a covenant breaches on this case" in out

    def test_a_cell_with_no_answer_says_why_once(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, out, _ = run(
            "sensitivity",
            EXAMPLE,
            "--rows",
            "exit-multiple:2,3,11",
            "--columns",
            "entry-multiple:11.5",
            capsys=capsys,
        )
        # Two cells fail for the same reason, and the reason is stated once.
        assert out.count("the equity is wiped out") == 1
        assert "no answer" in out

    def test_no_line_carries_trailing_whitespace(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, out, _ = run(
            "sensitivity",
            EXAMPLE,
            "--rows",
            "ebitda-margin:-6,0,3",
            "--columns",
            "exit-year:3,5",
            capsys=capsys,
        )
        assert all(line == line.rstrip() for line in out.splitlines())


class TestTheJsonForm:
    def test_it_carries_the_axes_the_cells_and_the_base(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, _ = run(
            "sensitivity",
            EXAMPLE,
            "--rows",
            "exit-multiple:10,11",
            "--columns",
            "entry-multiple:11,11.5,12",
            "--metric",
            "irr",
            "--json",
            capsys=capsys,
        )
        assert code == 0
        report = json.loads(out)
        assert report["metric"]["name"] == "irr"
        assert report["rows"]["dimension"] == "exit-multiple"
        assert report["columns"]["labels"] == ["11.00x", "11.50x", "12.00x"]
        assert report["columns"]["base"] == [False, True, False]
        assert len(report["cells"]) == 2
        assert len(report["cells"][0]) == 3
        assert report["base"]["value"] is not None

    def test_the_base_cell_matches_the_headline_in_json(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        deal = load_deal(EXAMPLE)
        _, out, _ = run(
            "sensitivity",
            EXAMPLE,
            "--rows",
            "exit-multiple:11",
            "--columns",
            "entry-multiple:11.5",
            "--metric",
            "moic",
            "--json",
            capsys=capsys,
        )
        report = json.loads(out)
        headline = deal.realise().moic
        assert headline is not None
        assert money(report["cells"][0][0]["value"]) == money(f"{headline:.2f}")
        assert money(report["base"]["value"]) == money(f"{headline:.2f}")

    def test_a_failed_cell_is_null_with_its_reason(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, out, _ = run(
            "sensitivity",
            EXAMPLE,
            "--rows",
            "exit-multiple:2",
            "--columns",
            "entry-multiple:11.5",
            "--json",
            capsys=capsys,
        )
        cell = json.loads(out)["cells"][0][0]
        assert cell["value"] is None
        assert "wiped out" in cell["note"]

    def test_a_shift_axis_reports_the_shift_not_the_level(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, out, _ = run(
            "sensitivity",
            EXAMPLE,
            "--rows",
            "ebitda-margin:-1.5,0",
            "--columns",
            "exit-multiple:11",
            "--json",
            capsys=capsys,
        )
        report = json.loads(out)
        assert report["rows"]["labels"] == ["-1.5pp", "0pp"]
        assert report["rows"]["base"] == [False, True]


class TestArgumentErrors:
    def test_an_unknown_dimension_is_reported_and_exits_one(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, _, err = run(
            "sensitivity",
            EXAMPLE,
            "--rows",
            "wacc:8,9",
            "--columns",
            "exit-multiple:11",
            capsys=capsys,
        )
        assert code == 1
        assert "unknown dimension 'wacc'" in err
        assert "entry-multiple" in err

    def test_one_dimension_on_both_axes_is_reported(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, _, err = run(
            "sensitivity",
            EXAMPLE,
            "--rows",
            "exit-multiple:10",
            "--columns",
            "exit-multiple:11",
            capsys=capsys,
        )
        assert code == 1
        assert "two different assumptions" in err

    def test_an_unknown_metric_is_rejected_by_the_parser(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            main(
                [
                    "sensitivity",
                    EXAMPLE,
                    "--rows",
                    "exit-multiple:10",
                    "--columns",
                    "entry-multiple:11",
                    "--metric",
                    "alpha",
                ]
            )

    def test_both_axes_are_required(self) -> None:
        with pytest.raises(SystemExit):
            main(["sensitivity", EXAMPLE, "--rows", "exit-multiple:10"])
