import json
from datetime import date
from typing import Any
from decimal import Decimal
from pathlib import Path

import pytest

from capstack.money import money
from capstack.spec import DealSpecError, load_deal, parse_deal

MINIMAL = {"entry": {"ltm_ebitda": 100, "multiple": 10}}


def write(tmp_path: Path, payload: object, name: str = "deal.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestParsing:
    def test_minimal_deal(self) -> None:
        d = parse_deal(dict(MINIMAL))
        assert d.name == "Untitled"
        assert d.close_date is None
        assert d.transaction.valuation.enterprise_value == money(1000)

    def test_name_and_close_date(self) -> None:
        d = parse_deal(
            {**MINIMAL, "name": "Project Meridian", "close_date": "2026-06-30"}
        )
        assert d.name == "Project Meridian"
        assert d.close_date == date(2026, 6, 30)

    def test_tranches_are_read_in_order(self) -> None:
        d = parse_deal(
            {
                **MINIMAL,
                "debt": [
                    {"name": "TLB", "face": 400, "issue_price": "0.995"},
                    {"name": "Notes", "face": 200},
                ],
            }
        )
        assert [t.name for t in d.transaction.debt] == ["TLB", "Notes"]
        assert d.transaction.debt[0].proceeds == money(398)

    def test_tranche_defaults_to_par_with_no_fee(self) -> None:
        d = parse_deal({**MINIMAL, "debt": [{"name": "TLB", "face": 400}]})
        assert d.transaction.debt[0].issue_price == money(1)
        assert d.transaction.debt[0].financing_fee == money(0)

    def test_other_uses_are_read(self) -> None:
        d = parse_deal(
            {**MINIMAL, "other_uses": [{"label": "Break fee", "amount": 12, "note": "payable"}]}
        )
        assert d.transaction.other_uses[0].label == "Break fee"
        assert d.transaction.other_uses[0].amount == money(12)
        assert d.transaction.other_uses[0].note == "payable"

    def test_a_closing_payment_is_expensed_unless_it_says_otherwise(self) -> None:
        d = parse_deal({**MINIMAL, "other_uses": [{"label": "Break fee", "amount": 12}]})
        assert d.transaction.other_uses[0].capitalised is False

    def test_a_closing_payment_can_be_marked_capitalised(self) -> None:
        d = parse_deal(
            {
                **MINIMAL,
                "other_uses": [
                    {"label": "Licence acquired", "amount": 30, "capitalised": True}
                ],
            }
        )
        assert d.transaction.other_uses[0].capitalised is True

    def test_capitalised_must_be_a_boolean(self) -> None:
        with pytest.raises(DealSpecError, match=r"other_uses\[0\]\.capitalised"):
            parse_deal(
                {**MINIMAL, "other_uses": [{"label": "x", "amount": 1, "capitalised": "yes"}]}
            )

    def test_optional_amounts_default_to_zero(self) -> None:
        d = parse_deal(dict(MINIMAL))
        assert d.transaction.rollover_equity == money(0)
        assert d.transaction.transaction_fee_rate == money(0)

    def test_explicit_null_is_treated_as_absent(self) -> None:
        d = parse_deal({**MINIMAL, "rollover_equity": None})
        assert d.transaction.rollover_equity == money(0)


class TestExactness:
    def test_decimal_literals_survive_the_round_trip(self, tmp_path: Path) -> None:
        # The reason for parse_float=Decimal. Read as a float, 0.995 times a
        # face of 400,000,000 misses by a fraction of a cent; read as a decimal
        # it is exactly 398,000,000.
        path = write(
            tmp_path,
            {
                "entry": {"ltm_ebitda": 100, "multiple": 10},
                "debt": [{"name": "TLB", "face": 400000000, "issue_price": 0.995}],
            },
        )
        d = load_deal(path)
        assert d.transaction.debt[0].issue_price == Decimal("0.995")
        assert d.transaction.debt[0].proceeds == Decimal("398000000.000")

    def test_a_loaded_deal_still_balances_exactly(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            {
                "entry": {
                    "ltm_ebitda": 87.3,
                    "multiple": 11.75,
                    "existing_debt": 37.77,
                    "existing_cash": 4.13,
                },
                "debt": [
                    {"name": "TLB", "face": 512.37, "issue_price": 0.9925, "financing_fee_rate": 0.0175}
                ],
                "rollover_equity": 19.19,
                "cash_from_balance_sheet": 4.13,
                "transaction_fee_rate": 0.0137,
            },
        )
        d = load_deal(path)
        table = d.transaction.sources_and_uses()
        assert table.total_sources - table.total_uses == money(0)


class TestLoadErrors:
    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(DealSpecError, match="cannot read"):
            load_deal(tmp_path / "absent.json")

    def test_invalid_json_names_the_line(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text('{\n  "entry": {,\n}', encoding="utf-8")
        with pytest.raises(DealSpecError, match="invalid JSON at line 2"):
            load_deal(path)

    def test_top_level_must_be_an_object(self, tmp_path: Path) -> None:
        with pytest.raises(DealSpecError, match="object at the top level"):
            load_deal(write(tmp_path, [1, 2, 3]))

    def test_missing_entry_block(self) -> None:
        with pytest.raises(DealSpecError, match="missing required field 'entry'"):
            parse_deal({"name": "x"})

    def test_missing_ebitda(self) -> None:
        with pytest.raises(DealSpecError, match="missing required field 'ltm_ebitda'"):
            parse_deal({"entry": {"multiple": 10}})

    def test_missing_multiple(self) -> None:
        with pytest.raises(DealSpecError, match="missing required field 'multiple'"):
            parse_deal({"entry": {"ltm_ebitda": 100}})

    def test_entry_must_be_an_object(self) -> None:
        with pytest.raises(DealSpecError, match="entry: expected an object"):
            parse_deal({"entry": 100})

    def test_debt_must_be_a_list(self) -> None:
        with pytest.raises(DealSpecError, match="expected a list of tranches"):
            parse_deal({**MINIMAL, "debt": {"name": "TLB"}})

    def test_a_tranche_must_be_an_object(self) -> None:
        with pytest.raises(DealSpecError, match=r"debt\[0\]: expected an object"):
            parse_deal({**MINIMAL, "debt": ["TLB"]})

    def test_a_tranche_needs_a_face_amount(self) -> None:
        with pytest.raises(DealSpecError, match=r"debt\[1\]: missing required field 'face'"):
            parse_deal({**MINIMAL, "debt": [{"name": "A", "face": 1}, {"name": "B"}]})

    def test_a_bad_number_names_its_field(self) -> None:
        with pytest.raises(DealSpecError, match="entry.existing_debt: not a number"):
            parse_deal({"entry": {"ltm_ebitda": 100, "multiple": 10, "existing_debt": "lots"}})

    def test_a_bad_close_date(self) -> None:
        with pytest.raises(DealSpecError, match="close_date: not a date"):
            parse_deal({**MINIMAL, "close_date": "30 June 2026"})

    def test_other_uses_must_be_a_list(self) -> None:
        with pytest.raises(DealSpecError, match="other_uses: expected a list"):
            parse_deal({**MINIMAL, "other_uses": {"label": "x", "amount": 1}})

    def test_an_other_use_needs_an_amount(self) -> None:
        with pytest.raises(DealSpecError, match=r"other_uses\[0\]: missing required field 'amount'"):
            parse_deal({**MINIMAL, "other_uses": [{"label": "Break fee"}]})

    def test_domain_validation_still_applies_after_parsing(self) -> None:
        # The spec layer parses; the transaction layer is what rejects nonsense.
        with pytest.raises(ValueError, match="positive EBITDA"):
            parse_deal({"entry": {"ltm_ebitda": 0, "multiple": 10}})


class TestShippedExample:
    def test_the_worked_example_loads_and_balances(self) -> None:
        path = Path(__file__).resolve().parents[1] / "examples" / "meridian.json"
        d = load_deal(path)
        assert d.name == "Project Meridian"
        assert d.close_date == date(2026, 6, 30)

        # Checked by hand against the file:
        #   EV            240 x 11.5              = 2,760.00
        #   Net debt      410 - 65                =   345.00
        #   Equity price  2,760 - 345             = 2,415.00
        #   Fin. fees     25.875 + 7.875 + 7.5    =    41.25
        #   OID           5.75 + 5.00             =    10.75
        #   Txn fees      2,760 x 1.4%            =    38.64
        #   Uses          2,415 + 410 + 38.64 + 41.25 + 10.75 + 40 + 18.5 = 2,974.14
        #   Sources       1,850 + 85 + 45 + sponsor           -> sponsor =   994.14
        assert d.transaction.valuation.enterprise_value == money("2760.0")
        assert d.transaction.valuation.equity_purchase_price == money("2415.0")
        assert d.transaction.financing_fees == money("41.25")
        assert d.transaction.original_issue_discount == money("10.75")
        assert d.transaction.transaction_fees == money("38.64")
        assert d.transaction.sponsor_equity == money("994.14")

        table = d.transaction.sources_and_uses()
        assert table.total_uses == money("2974.14")
        assert table.total_sources == money("2974.14")

    def test_the_worked_example_entry_metrics(self) -> None:
        path = Path(__file__).resolve().parents[1] / "examples" / "meridian.json"
        d = load_deal(path)
        assert d.transaction.total_debt == money("1850.0")
        assert d.transaction.entry_leverage == money("1850.0") / money("240.0")
        assert round(float(d.transaction.entry_leverage), 2) == 7.71
        assert d.transaction.total_capitalisation == money("2929.14")
        assert round(float(d.transaction.equity_contribution_rate), 4) == 0.3684
        assert round(float(d.transaction.sponsor_ownership), 4) == 0.9212

    def test_the_undrawn_revolver_is_left_off_the_sources_side(self) -> None:
        path = Path(__file__).resolve().parents[1] / "examples" / "meridian.json"
        d = load_deal(path)
        labels = [i.label for i in d.transaction.sources_and_uses().sources]
        assert "Revolving credit facility" not in labels
        # ...but it is still part of the structure.
        assert any(t.name == "Revolving credit facility" for t in d.transaction.debt)


PROJECTED: dict[str, Any] = {
    "close_date": "2026-06-30",
    "entry": {"ltm_ebitda": 100, "multiple": 10},
    "projection": {"years": 5},
    "operating": {
        "opening_revenue": 1000,
        "revenue_growth": 0.08,
        "ebitda_margin": 0.20,
        "da_rate": 0.04,
        "capex_rate": 0.05,
        "nwc_rate": 0.15,
        "tax_rate": 0.25,
    },
}


class TestProjectionParsing:
    def test_a_deal_without_a_projection_says_so(self) -> None:
        d = parse_deal(dict(MINIMAL))
        assert not d.has_projection
        with pytest.raises(DealSpecError, match="no operating case"):
            d.project()

    def test_a_projected_deal_runs(self) -> None:
        d = parse_deal(dict(PROJECTED))
        assert d.has_projection
        model = d.project()
        assert len(model) == 5
        assert model[0].revenue == money(1080)
        assert model[0].unlevered_free_cash_flow == money("106.80")

    def test_frequency_defaults_to_annual(self) -> None:
        assert len(parse_deal(dict(PROJECTED)).project()) == 5

    def test_quarterly_projection(self) -> None:
        payload = {**PROJECTED, "projection": {"years": 3, "frequency": "quarterly"}}
        assert len(parse_deal(payload).project()) == 12

    def test_projection_needs_a_close_date(self) -> None:
        payload = {k: v for k, v in PROJECTED.items() if k != "close_date"}
        with pytest.raises(DealSpecError, match="close date is required"):
            parse_deal(payload)

    def test_an_unknown_frequency_lists_the_valid_ones(self) -> None:
        payload = {**PROJECTED, "projection": {"years": 5, "frequency": "fortnightly"}}
        with pytest.raises(DealSpecError, match="unknown frequency"):
            parse_deal(payload)

    def test_projection_without_operating_is_rejected(self) -> None:
        payload = {k: v for k, v in PROJECTED.items() if k != "operating"}
        with pytest.raises(DealSpecError, match="'operating' is missing"):
            parse_deal(payload)

    def test_operating_without_projection_is_rejected(self) -> None:
        payload = {k: v for k, v in PROJECTED.items() if k != "projection"}
        with pytest.raises(DealSpecError, match="'projection' is missing"):
            parse_deal(payload)

    def test_missing_opening_revenue(self) -> None:
        operating = {
            k: v for k, v in PROJECTED["operating"].items() if k != "opening_revenue"
        }
        payload = {**PROJECTED, "operating": operating}
        with pytest.raises(DealSpecError, match="missing required field 'opening_revenue'"):
            parse_deal(payload)

    def test_years_must_be_a_whole_number(self) -> None:
        payload = {**PROJECTED, "projection": {"years": "five"}}
        with pytest.raises(DealSpecError, match="not a whole number"):
            parse_deal(payload)

    def test_zero_years_is_rejected(self) -> None:
        payload = {**PROJECTED, "projection": {"years": 0}}
        with pytest.raises(DealSpecError, match="at least one year"):
            parse_deal(payload)


class TestDriverParsing:
    def _run(self, growth: object, years: int = 5) -> list[Decimal]:
        payload = {
            **PROJECTED,
            "projection": {"years": years},
            "operating": {**PROJECTED["operating"], "revenue_growth": growth},
        }
        return list(parse_deal(payload).operating.revenue_growth)  # type: ignore[union-attr]

    def test_a_bare_number_is_constant(self) -> None:
        assert self._run(0.08) == [money("0.08")] * 5

    def test_an_explicit_list(self) -> None:
        assert self._run([0.10, 0.09, 0.08, 0.07, 0.06])[0] == money("0.10")

    def test_a_short_list_holds_its_final_value(self) -> None:
        assert self._run([0.10, 0.05]) == [money("0.10")] + [money("0.05")] * 4

    def test_constant_object(self) -> None:
        assert self._run({"constant": 0.07}) == [money("0.07")] * 5

    def test_ramp_object(self) -> None:
        assert self._run({"ramp": [0.09, 0.03]}) == [
            money("0.09"),
            money("0.075"),
            money("0.06"),
            money("0.045"),
            money("0.03"),
        ]

    def test_values_object(self) -> None:
        assert self._run({"values": [0.10, 0.05]})[0] == money("0.10")

    def test_a_ramp_needs_exactly_two_ends(self) -> None:
        with pytest.raises(DealSpecError, match="exactly two values"):
            self._run({"ramp": [0.09, 0.06, 0.03]})

    def test_an_empty_series_is_rejected(self) -> None:
        with pytest.raises(DealSpecError, match="says nothing"):
            self._run([])

    def test_an_unknown_driver_shape_lists_the_valid_ones(self) -> None:
        with pytest.raises(DealSpecError, match="'constant', 'ramp' or 'values'"):
            self._run({"trend": 0.05})

    def test_a_bad_number_inside_a_driver_names_the_field(self) -> None:
        with pytest.raises(DealSpecError, match="operating.revenue_growth: not a number"):
            self._run("brisk")

    def test_omitted_rates_default_to_zero(self) -> None:
        payload = {
            **PROJECTED,
            "operating": {
                "opening_revenue": 1000,
                "revenue_growth": 0,
                "ebitda_margin": 0.2,
            },
        }
        model = parse_deal(payload).project()
        assert model[0].capital_expenditure == money(0)
        assert model[0].depreciation_and_amortisation == money(0)
        assert model[0].tax.cash_tax == money(0)


class TestShippedExampleProjection:
    def test_the_example_projects(self) -> None:
        path = Path(__file__).resolve().parents[1] / "examples" / "meridian.json"
        d = load_deal(path)
        assert d.has_projection
        model = d.project()

        # Period one, computed by hand from the file:
        #   revenue  1,480.00 x 1.085          = 1,605.80
        #   EBITDA   1,605.80 x 0.163          =   261.7454
        #   D&A      1,605.80 x 0.036          =    57.8088
        #   EBIT                               =   203.9366
        #   tax      203.9366 x 0.25           =    50.98415
        #   capex    1,605.80 x 0.048          =    77.0784
        #   NWC      1,605.80 x 0.112 = 179.8496, opening 165.76, so +14.0896
        #   UFCF     152.95245 + 57.8088 - 77.0784 - 14.0896 = 119.59325
        p = model[0]
        assert p.revenue == money("1605.80")
        assert p.ebitda == money("261.7454")
        assert p.depreciation_and_amortisation == money("57.8088")
        assert p.ebit == money("203.9366")
        assert p.tax.cash_tax == money("50.98415")
        assert p.capital_expenditure == money("77.0784")
        assert model.opening_net_working_capital == money("165.76")
        assert p.change_in_net_working_capital == money("14.0896")
        assert p.unlevered_free_cash_flow == money("119.59325")

    def test_the_operating_case_is_coherent_with_the_entry_multiple(self) -> None:
        # The margin the deal is priced on and the margin the case opens at
        # should be within a whisker of each other, or the two halves of the
        # file are describing different businesses.
        path = Path(__file__).resolve().parents[1] / "examples" / "meridian.json"
        d = load_deal(path)
        model = d.project()
        implied_ltm_margin = d.transaction.valuation.ltm_ebitda / model.opening_revenue
        assert abs(implied_ltm_margin - model[0].ebitda_margin) < money("0.005")
