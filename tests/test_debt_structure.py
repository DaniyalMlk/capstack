import pytest

from capstack.daycount import DayCount
from capstack.debt import CapitalStructure, InterestBasis, Tranche, TrancheKind
from capstack.drivers import Driver
from capstack.money import money


def revolver(face: int = 0, commitment: int = 100) -> Tranche:
    return Tranche.of(
        "Revolver",
        TrancheKind.REVOLVER,
        face,
        cash_rate="0.035",
        commitment=commitment,
        undrawn_fee="0.005",
    )


def term_loan(face: int = 500) -> Tranche:
    return Tranche.of(
        "Term Loan B",
        TrancheKind.TERM_LOAN,
        face,
        cash_rate="0.04",
        floor="0.01",
        amortisation=Driver.constant("0.01", 5),
    )


class TestTrancheKind:
    def test_the_waterfall_runs_from_the_revolver_down(self) -> None:
        order = [
            TrancheKind.REVOLVER,
            TrancheKind.TERM_LOAN,
            TrancheKind.NOTES,
            TrancheKind.MEZZANINE,
            TrancheKind.SELLER_NOTE,
        ]
        ranks = [k.default_seniority for k in order]
        assert ranks == sorted(ranks)
        assert len(set(ranks)) == len(ranks)

    def test_only_bank_debt_is_swept_by_default(self) -> None:
        assert TrancheKind.REVOLVER.default_swept
        assert TrancheKind.TERM_LOAN.default_swept
        assert not TrancheKind.NOTES.default_swept
        assert not TrancheKind.MEZZANINE.default_swept
        assert not TrancheKind.SELLER_NOTE.default_swept

    def test_only_bank_debt_floats_by_default(self) -> None:
        assert TrancheKind.TERM_LOAN.default_floating
        assert not TrancheKind.NOTES.default_floating
        assert not TrancheKind.SELLER_NOTE.default_floating

    def test_it_prints_readably(self) -> None:
        assert str(TrancheKind.SELLER_NOTE) == "seller note"


class TestTranche:
    def test_a_fixed_tranche_ignores_the_base_rate(self) -> None:
        notes = Tranche.of("Notes", TrancheKind.NOTES, 400, cash_rate="0.0725")
        assert notes.rate_at(money("0.05")) == money("0.0725")

    def test_a_floating_tranche_prices_over_the_base(self) -> None:
        assert term_loan().rate_at(money("0.045")) == money("0.085")

    def test_the_floor_holds_when_the_base_falls_through_it(self) -> None:
        # Base at 25bp, floor at 100bp: the borrower pays 100 + 400, not 25 + 400.
        assert term_loan().rate_at(money("0.0025")) == money("0.05")

    def test_the_floor_does_not_bind_above_itself(self) -> None:
        assert term_loan().rate_at(money("0.06")) == money("0.10")

    def test_amortisation_is_a_share_of_the_original_face(self) -> None:
        loan = term_loan(500)
        assert loan.scheduled_amortisation(0) == money(5)
        assert loan.scheduled_amortisation(4) == money(5)

    def test_no_schedule_means_a_bullet(self) -> None:
        notes = Tranche.of("Notes", TrancheKind.NOTES, 400, cash_rate="0.07")
        assert notes.scheduled_amortisation(0) == money(0)

    def test_a_stepped_schedule_is_read_period_by_period(self) -> None:
        loan = Tranche.of(
            "TLA",
            TrancheKind.TERM_LOAN,
            1000,
            amortisation=Driver.of(["0.05", "0.10", "0.15"]),
            floating=False,
        )
        assert [loan.scheduled_amortisation(i) for i in range(3)] == [
            money(50),
            money(100),
            money(150),
        ]

    def test_accretion_is_recognised(self) -> None:
        mezz = Tranche.of("Mezzanine", TrancheKind.MEZZANINE, 200, cash_rate="0.05",
                          pik_rate="0.06")
        assert mezz.accretes
        assert not term_loan().accretes

    def test_a_revolver_commitment_defaults_to_what_is_drawn(self) -> None:
        r = Tranche.of("RCF", TrancheKind.REVOLVER, 40)
        assert r.commitment == money(40)
        assert r.undrawn_at(money(40)) == money(0)

    def test_undrawn_capacity(self) -> None:
        assert revolver(face=25, commitment=100).undrawn_at(money(25)) == money(75)

    def test_a_term_loan_with_no_commitment_has_no_undrawn_capacity(self) -> None:
        assert term_loan().undrawn_at(money(500)) == money(0)

    def test_drawing_more_than_the_commitment_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="exceeds the commitment"):
            Tranche.of("RCF", TrancheKind.REVOLVER, 120, commitment=100)

    def test_a_term_commitment_has_to_say_how_long_it_is_available(self) -> None:
        with pytest.raises(ValueError, match="how long it can be drawn"):
            Tranche.of("TLB", TrancheKind.TERM_LOAN, 100, commitment=200)

    def test_a_term_commitment_that_says_so_is_accepted(self) -> None:
        facility = Tranche.of(
            "TLB", TrancheKind.TERM_LOAN, 100, commitment=200, availability=2
        )
        assert facility.has_commitment
        assert facility.undrawn_at(money(100), period=1) == money(100)

    def test_a_nameless_tranche_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="needs a name"):
            Tranche.of("  ", TrancheKind.NOTES, 100)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("face", -1),
            ("cash_rate", -1),
            ("pik_rate", -1),
            ("floor", -1),
            ("undrawn_fee", -1),
        ],
    )
    def test_negative_inputs_are_rejected(self, field: str, value: int) -> None:
        kwargs: dict[str, object] = {"cash_rate": 0}
        kwargs[field] = value
        face = kwargs.pop("face", 100)
        with pytest.raises(ValueError, match="must not be negative"):
            Tranche.of("TLB", TrancheKind.TERM_LOAN, face, **kwargs)  # type: ignore[arg-type]

    def test_maturity_is_a_period_index(self) -> None:
        with pytest.raises(ValueError, match="maturity is a period index"):
            Tranche.of("TLB", TrancheKind.TERM_LOAN, 100, maturity=0)

    def test_negative_amortisation_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="amortisation must not be negative"):
            Tranche.of(
                "TLB", TrancheKind.TERM_LOAN, 100, amortisation=Driver.of(["0.05", "-0.01"])
            )


class TestCapitalStructure:
    def test_face_and_commitment_are_different_totals(self) -> None:
        s = CapitalStructure.of(
            [revolver(face=25, commitment=100), term_loan(500)],
            base_rate=Driver.constant("0.04", 5),
        )
        assert s.total_face == money(525)
        assert s.total_commitment == money(100)

    def test_tranches_are_reachable_by_name(self) -> None:
        s = CapitalStructure.of([term_loan()], base_rate=Driver.constant("0.04", 5))
        assert s.tranche("Term Loan B").face == money(500)
        with pytest.raises(KeyError, match="no tranche named"):
            s.tranche("Second lien")

    def test_the_sweep_order_groups_equal_ranks(self) -> None:
        pari = Tranche.of(
            "Term Loan A", TrancheKind.TERM_LOAN, 300, seniority=1, amortisation=None
        )
        s = CapitalStructure.of(
            [
                revolver(face=0, commitment=100),
                term_loan(500),
                pari,
                Tranche.of("Notes", TrancheKind.NOTES, 400, cash_rate="0.07"),
            ],
            base_rate=Driver.constant("0.04", 5),
        )
        ranks = s.sweep_order
        assert [rank for rank, _ in ranks] == [0, 1]
        assert [t.name for t in ranks[0][1]] == ["Revolver"]
        assert sorted(t.name for t in ranks[1][1]) == ["Term Loan A", "Term Loan B"]
        # Notes are outside the sweep entirely.
        assert all("Notes" not in [t.name for t in group] for _, group in ranks)

    def test_the_blended_coupon_is_weighted_by_face(self) -> None:
        s = CapitalStructure.of(
            [
                Tranche.of("TLB", TrancheKind.TERM_LOAN, 600, cash_rate="0.04"),
                Tranche.of("Notes", TrancheKind.NOTES, 400, cash_rate="0.08"),
            ],
            base_rate=Driver.constant("0.03", 5),
        )
        # 600 at 7% and 400 at 8% blends to 7.4%.
        assert s.blended_cash_rate(0) == money("0.074")

    def test_an_undrawn_structure_has_no_blended_rate_to_report(self) -> None:
        s = CapitalStructure.of([Tranche.of("RCF", TrancheKind.REVOLVER, 0, commitment=100)],
                                base_rate=Driver.constant("0.03", 5))
        assert s.blended_cash_rate(0) == money(0)

    def test_defaults_are_the_market_ones(self) -> None:
        s = CapitalStructure.of([Tranche.of("Notes", TrancheKind.NOTES, 100, cash_rate="0.07")])
        assert s.day_count is DayCount.ACT_360
        assert s.interest_basis is InterestBasis.AVERAGE
        assert s.sweep_rate == money(1)

    def test_a_floating_tranche_without_a_base_rate_is_refused(self) -> None:
        with pytest.raises(ValueError, match="a base rate is required: Term Loan B"):
            CapitalStructure.of([term_loan()])

    def test_an_all_fixed_structure_needs_no_base_rate(self) -> None:
        s = CapitalStructure.of([Tranche.of("Notes", TrancheKind.NOTES, 400, cash_rate="0.07")])
        assert s.base_at(0) == money(0)

    def test_an_empty_structure_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one tranche"):
            CapitalStructure.of([])

    def test_duplicate_names_are_refused(self) -> None:
        with pytest.raises(ValueError, match="names must be distinct"):
            CapitalStructure.of(
                [
                    Tranche.of("TLB", TrancheKind.TERM_LOAN, 100, floating=False),
                    Tranche.of("TLB", TrancheKind.TERM_LOAN, 200, floating=False),
                ]
            )

    @pytest.mark.parametrize("rate", [-1, "1.5"])
    def test_the_sweep_rate_is_a_share(self, rate: object) -> None:
        with pytest.raises(ValueError, match="between 0 and 1"):
            CapitalStructure.of(
                [Tranche.of("Notes", TrancheKind.NOTES, 100, cash_rate="0.07")],
                sweep_rate=rate,  # type: ignore[arg-type]
            )

    @pytest.mark.parametrize("damping", [0, "1.5", -1])
    def test_damping_is_bounded(self, damping: object) -> None:
        with pytest.raises(ValueError, match="damping"):
            CapitalStructure.of(
                [Tranche.of("Notes", TrancheKind.NOTES, 100, cash_rate="0.07")],
                damping=damping,  # type: ignore[arg-type]
            )

    def test_iteration_and_tolerance_are_checked(self) -> None:
        tranche = Tranche.of("Notes", TrancheKind.NOTES, 100, cash_rate="0.07")
        with pytest.raises(ValueError, match="tolerance must be positive"):
            CapitalStructure.of([tranche], tolerance=0)
        with pytest.raises(ValueError, match="at least one iteration"):
            CapitalStructure.of([tranche], max_iterations=0)

    def test_negative_minimum_cash_is_refused(self) -> None:
        with pytest.raises(ValueError, match="minimum cash"):
            CapitalStructure.of(
                [Tranche.of("Notes", TrancheKind.NOTES, 100, cash_rate="0.07")],
                minimum_cash=-1,
            )
