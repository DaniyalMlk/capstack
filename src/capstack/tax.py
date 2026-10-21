"""Tax on a business that is actually financed, rather than on one that is not.

The operating case computes tax on EBIT, which is the right thing for it to do:
it is an unlevered layer, and taxing it as though the structure did not exist
keeps it independent of the structure. But a leveraged company does not pay tax
on EBIT. It deducts its interest and pays tax on what is left, and for a deal
carrying five turns of debt that deduction is the second-largest cash line in
the model after EBITDA itself.

This module is the levered computation. It takes an interest series as its
input rather than a capital structure, which keeps it a pure sequential pass
over periods and makes it testable against interest written down by hand. The
circularity that follows — the deduction frees cash, the cash repays debt, the
smaller balance earns less interest, the smaller interest earns a smaller
deduction — is solved a layer up.

Three things make this domain logic rather than a subtraction.

*The deduction is capped, and a leveraged deal is where the cap binds.* A limit
of thirty per cent of earnings sounds generous until it is set against a
structure deliberately levered to the point where interest is a large fraction
of earnings, which is the entire design of the instrument. The cap is the
difference between a model that says leverage is free after tax and one that
says it stops being free somewhere, and where that somewhere is.

*The earnings the cap is struck against are a convention, not a fact.* The same
deal tested against thirty per cent of EBITDA and thirty per cent of EBIT gives
two different answers, because depreciation sits between them, and in a
capital-intensive business it sits between them heavily. Both bases are live
regimes. A model that hard-codes one of them cannot be asked the question.

*Disallowed interest is deferred, not lost, and it competes.* Interest the cap
refuses joins a pool, and the pool is tested again next period against next
period's cap, alongside next period's interest. So a deal can be inside the cap
on its own current interest and still have a disallowed balance, and a pool
accumulated in the early years can stay stuck for the whole hold. Treating the
disallowed amount as permanently lost overstates tax; treating it as freely
deductible later understates it.

*And the order against the loss pool matters.* The earnings base for the
interest cap is struck before any loss relief, so the cap is applied first, and
the carried-forward loss then shelters what the interest deduction has left
behind. Running the two the other way round gives a different cash tax in every
period where both bite, and the difference does not wash out over the hold
because the pools close at different balances.

Every figure here is an exact ``Decimal``. The pool movements are conservation
identities and are tested as equalities rather than tolerances: a unit of
interest is either deducted or in the disallowed pool, never approximately one
or the other.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from enum import Enum

from .debt import CapitalStructure, DebtSchedule
from .money import ZERO, Money, Numeric, money, safe_div
from .operating import DEFAULT_NOL_USAGE_LIMIT, OperatingAssumptions, OperatingModel

__all__ = [
    "DEFAULT_INTEREST_LIMIT_RATE",
    "DEFAULT_SOLVE_TOLERANCE",
    "EarningsBase",
    "InterestDeduction",
    "InterestLimit",
    "LeveredSolution",
    "LeveredTaxPeriod",
    "LeveredTaxSchedule",
    "ScheduleRunner",
    "TaxCircularityError",
    "TaxPolicy",
    "TaxPolicyError",
    "levered_schedule",
    "levered_tax",
    "levered_tax_for_model",
    "levered_tax_schedule",
    "solve_levered",
]

#: How close two successive saving series must be for the solve to stop. The
#: quantity is money, so the default is a hundredth of a currency unit's
#: hundredth: far below anything a deal file expresses and far above the
#: precision the arithmetic carries.
DEFAULT_SOLVE_TOLERANCE = money("0.0001")

#: The maximum number of schedule runs a solve will make before giving up. The
#: measured worst case across the structures in the suite is well inside this;
#: see :func:`solve_levered`.
MAX_SOLVE_ITERATIONS = 100

#: The share of earnings a net interest deduction is commonly limited to. Thirty
#: per cent is the figure in the regime most leveraged deals are now struck
#: under; set the limit to ``None`` to model a regime with no cap at all.
DEFAULT_INTEREST_LIMIT_RATE = money("0.30")


class TaxPolicyError(ValueError):
    """A tax policy that cannot be applied as written."""


class TaxCircularityError(RuntimeError):
    """The deduction and the debt it is charged on did not settle.

    Distinct from :class:`~capstack.debt.CircularityError`, which is the inner
    fixed point between a period's balance and the interest accrued on it. This
    is the outer one, between the whole hold's tax and the whole hold's debt,
    and a structure can solve the inner one in every period and still fail
    here.
    """


class EarningsBase(Enum):
    """Which earnings figure the interest cap is struck against.

    The two differ by depreciation and amortisation, which is not a detail: in a
    business running D&A at a tenth of revenue on a mid-single-digit margin, the
    EBIT base is a small fraction of the EBITDA one and a structure comfortably
    inside the cap on the first measure is nowhere near it on the second.
    """

    #: Earnings before interest, tax, depreciation and amortisation.
    EBITDA = "ebitda"
    #: Earnings before interest and tax, so after depreciation and amortisation.
    EBIT = "ebit"


@dataclass(frozen=True, slots=True)
class InterestLimit:
    """A cap on the interest deductible in a period.

    ``rate`` is applied to whichever earnings figure ``base`` names, and the
    result is the most interest a period may deduct however much it incurred.
    The earnings figure is taken before any loss relief, which is what fixes the
    ordering against the loss pool.
    """

    rate: Money
    base: EarningsBase = EarningsBase.EBITDA

    @classmethod
    def of(
        cls,
        rate: Numeric = DEFAULT_INTEREST_LIMIT_RATE,
        base: EarningsBase = EarningsBase.EBITDA,
    ) -> InterestLimit:
        return cls(rate=money(rate), base=base)

    def __post_init__(self) -> None:
        if self.rate < 0:
            raise TaxPolicyError("the interest limit rate must not be negative")

    def allowance(self, *, ebitda: Money, ebit: Money) -> Money:
        """The most interest this period may deduct.

        Negative earnings give an allowance of zero rather than a negative one:
        a loss-making period does not owe the revenue authority a deduction, and
        a negative allowance would turn the cap into a charge.
        """
        earnings = ebitda if self.base is EarningsBase.EBITDA else ebit
        return max(ZERO, earnings * self.rate)


@dataclass(frozen=True, slots=True)
class TaxPolicy:
    """The tax regime a case is run under.

    ``interest_limit`` of ``None`` means an uncapped deduction, which is both a
    real historic regime and the useful comparison: running a case twice, once
    capped and once not, prices the cap.
    """

    rate: Money
    nol_usage_limit: Money = DEFAULT_NOL_USAGE_LIMIT
    interest_limit: InterestLimit | None = None
    #: Whether interest that accrues without being paid is deductible as it
    #: accrues. A PIK margin rolled into the balance is an expense the period
    #: incurred, and the common regimes deduct it on that basis; a regime that
    #: waits for the cash sets this to ``False`` and defers it to the pool.
    deduct_accrued_interest: bool = True

    @classmethod
    def from_assumptions(
        cls,
        assumptions: OperatingAssumptions,
        *,
        interest_limit: InterestLimit | None = None,
        deduct_accrued_interest: bool = True,
    ) -> TaxPolicy:
        """Take the rate and the loss limit from the operating case itself.

        The usual way to build a policy, and the safe one. The saving a solve
        credits to the cash flow is measured against the operating case's own
        tax line, so a policy whose rate disagrees with that case measures the
        saving off the wrong base. Reading both off the assumptions makes the
        disagreement impossible rather than merely detectable.
        """
        return cls(
            rate=assumptions.tax_rate,
            nol_usage_limit=assumptions.nol_usage_limit,
            interest_limit=interest_limit,
            deduct_accrued_interest=deduct_accrued_interest,
        )

    @classmethod
    def of(
        cls,
        rate: Numeric,
        *,
        nol_usage_limit: Numeric = DEFAULT_NOL_USAGE_LIMIT,
        interest_limit: InterestLimit | None = None,
        deduct_accrued_interest: bool = True,
    ) -> TaxPolicy:
        return cls(
            rate=money(rate),
            nol_usage_limit=money(nol_usage_limit),
            interest_limit=interest_limit,
            deduct_accrued_interest=deduct_accrued_interest,
        )

    def __post_init__(self) -> None:
        if self.rate < 0 or self.rate > 1:
            raise TaxPolicyError("the tax rate must be between 0 and 1")
        if self.nol_usage_limit < 0 or self.nol_usage_limit > 1:
            raise TaxPolicyError("the loss usage limit must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class InterestDeduction:
    """What the cap did to one period's interest.

    The whole movement is reported rather than only the deductible figure,
    because the pool is the part a reader cannot reconstruct: a period deducting
    its full current interest tells you nothing about whether a balance from
    three years ago is still stuck behind the cap.
    """

    #: Interest the period incurred and offered up for deduction.
    arising: Money
    #: Interest disallowed in earlier periods and tested again here.
    carried_in: Money
    #: The cap, or ``None`` where the regime has no cap.
    allowance: Money | None
    #: What was actually deducted against this period's income.
    deductible: Money
    #: What the cap refused, which joins the pool.
    disallowed: Money
    #: The pool after this period.
    carried_out: Money

    @property
    def offered(self) -> Money:
        """Everything tested against the cap: this period's interest and the pool."""
        return self.arising + self.carried_in

    @property
    def binding(self) -> bool:
        """Whether the cap refused anything that was offered to it."""
        return self.disallowed > ZERO

    @property
    def headroom(self) -> Money:
        """Allowance left unused. Zero where the cap bound, or where there is none."""
        if self.allowance is None:
            return ZERO
        return max(ZERO, self.allowance - self.offered)


@dataclass(frozen=True, slots=True)
class LeveredTaxPeriod:
    """One period of the levered tax computation, every line shown."""

    index: int
    ebitda: Money
    ebit: Money
    deduction: InterestDeduction
    #: EBIT less the interest actually deducted, before any loss relief.
    taxable_income: Money
    loss_relief_used: Money
    taxable_after_relief: Money
    cash_tax: Money
    closing_carryforward: Money
    #: What this period would have paid with no interest deduction at all, from
    #: the same opening loss pool. Carried rather than derived because it is the
    #: only honest way to state the saving; see :attr:`shield`.
    cash_tax_unlevered: Money
    #: The rate the period was taxed at, kept so :attr:`naive_shield` can be
    #: stated without dividing a tax by an income that may be zero.
    rate: Money

    @property
    def interest(self) -> Money:
        """Interest the period incurred, whether or not it was deductible."""
        return self.deduction.arising

    @property
    def shield(self) -> Money:
        """Cash tax the interest deduction actually saved this period.

        Not the deduction multiplied by the rate. That figure is right only
        where the period is profitable before and after the deduction and the
        loss pool is not in play, and it overstates the saving everywhere else:
        a deduction that takes a period from a small profit into a loss saves
        tax on the profit and nothing on the rest of itself, and a deduction
        taken in a period already sheltered by relief saves a fraction of the
        rate. So the saving is the difference against the same period computed
        with no deduction from the same opening pool, which is exact.
        """
        return self.cash_tax_unlevered - self.cash_tax

    @property
    def naive_shield(self) -> Money:
        """Deduction times rate: what a model that stops at the interest line books.

        Kept so the two can be compared rather than argued about. It is an upper
        bound on :attr:`shield`, and the gap between them is the saving such a
        model books and never receives.
        """
        return self.deduction.deductible * self.rate


@dataclass(frozen=True, slots=True)
class LeveredTaxSchedule:
    """The levered tax computation across the hold, with the pools rolled."""

    periods: tuple[LeveredTaxPeriod, ...]
    opening_carryforward: Money
    opening_disallowed_interest: Money

    def __len__(self) -> int:
        return len(self.periods)

    def __iter__(self) -> Iterator[LeveredTaxPeriod]:
        return iter(self.periods)

    def __getitem__(self, index: int) -> LeveredTaxPeriod:
        return self.periods[index]

    @property
    def cash_taxes(self) -> tuple[Money, ...]:
        """The cash tax series, which is what the cash flow needs from here."""
        return tuple(p.cash_tax for p in self.periods)

    @property
    def total_cash_tax(self) -> Money:
        return sum((p.cash_tax for p in self.periods), ZERO)

    @property
    def total_cash_tax_unlevered(self) -> Money:
        """What the hold would have paid with no interest deduction at all."""
        return sum((p.cash_tax_unlevered for p in self.periods), ZERO)

    @property
    def total_shield(self) -> Money:
        """Cash tax the structure saved over the hold.

        The difference of two totals, not the sum of the per-period
        differences. The two are equal in exact arithmetic and are not quite
        equal here: five roundings of a period difference against two roundings
        of a total, in a context carrying 34 digits, leave the two orderings
        disagreeing in the last places — measured at 7e-32 on the reference
        case. Which is nothing, but it is not zero, and a reader reconciling
        this against a cash flow should know which ordering produced it.

        This ordering is the one chosen because it is the one that ties: both
        totals are figures the model reports elsewhere, so the saving can be
        checked against two numbers a reader already has.
        """
        return self.total_cash_tax_unlevered - self.total_cash_tax

    @property
    def total_naive_shield(self) -> Money:
        """The saving a model that stops at the interest line would book."""
        return sum((p.naive_shield for p in self.periods), ZERO)

    @property
    def total_interest_arising(self) -> Money:
        return sum((p.deduction.arising for p in self.periods), ZERO)

    @property
    def total_interest_deducted(self) -> Money:
        return sum((p.deduction.deductible for p in self.periods), ZERO)

    @property
    def closing_disallowed_interest(self) -> Money:
        """Interest still stuck behind the cap at the end of the hold."""
        if not self.periods:
            return self.opening_disallowed_interest
        return self.periods[-1].deduction.carried_out

    @property
    def closing_carryforward(self) -> Money:
        if not self.periods:
            return self.opening_carryforward
        return self.periods[-1].closing_carryforward

    @property
    def deducted_share(self) -> Money:
        """Share of the interest arising over the hold that was ever deducted.

        The honest headline for a capped structure. A deal deducting four fifths
        of its interest is paying tax on the other fifth of it, and that fifth
        does not appear anywhere in a model that stops at the cash interest line.
        """
        return safe_div(self.total_interest_deducted, self.total_interest_arising, default=ZERO)

    @property
    def binding_periods(self) -> tuple[int, ...]:
        """The indices of the periods where the cap refused something."""
        return tuple(p.index for p in self.periods if p.deduction.binding)


def _limit_interest(
    *,
    arising: Money,
    carried_in: Money,
    ebitda: Money,
    ebit: Money,
    limit: InterestLimit | None,
) -> InterestDeduction:
    """Test a period's interest, and the pool behind it, against the cap.

    Current interest and the pool are tested together rather than in order,
    because the pool carries forward indefinitely and is not tracked by vintage:
    with no expiry to race, which of the two is deemed deducted first changes no
    cash tax in any period and no closing balance.
    """
    offered = arising + carried_in
    if limit is None:
        return InterestDeduction(
            arising=arising,
            carried_in=carried_in,
            allowance=None,
            deductible=offered,
            disallowed=ZERO,
            carried_out=ZERO,
        )

    allowance = limit.allowance(ebitda=ebitda, ebit=ebit)
    deductible = min(offered, allowance)
    return InterestDeduction(
        arising=arising,
        carried_in=carried_in,
        allowance=allowance,
        deductible=deductible,
        disallowed=offered - deductible,
        carried_out=offered - deductible,
    )


@dataclass(frozen=True, slots=True)
class _Relief:
    """The loss pool's effect on one income figure."""

    used: Money
    after_relief: Money
    cash_tax: Money
    closing_carryforward: Money


def _apply_relief(taxable_income: Money, pool: Money, policy: TaxPolicy) -> _Relief:
    """Shelter an income figure with the loss pool, subject to the usage limit.

    Shared by the real computation and the no-deduction counterfactual beside
    it, so that the saving the deduction produces is a difference between two
    runs of the same arithmetic rather than between two implementations of it.
    """
    if taxable_income <= 0:
        return _Relief(
            used=ZERO,
            after_relief=ZERO,
            cash_tax=ZERO,
            closing_carryforward=pool - taxable_income,
        )
    used = min(pool, taxable_income * policy.nol_usage_limit)
    after_relief = taxable_income - used
    return _Relief(
        used=used,
        after_relief=after_relief,
        cash_tax=after_relief * policy.rate,
        closing_carryforward=pool - used,
    )


def levered_tax(
    *,
    index: int,
    ebitda: Money,
    ebit: Money,
    cash_interest: Money,
    accrued_interest: Money = ZERO,
    opening_carryforward: Money = ZERO,
    opening_disallowed_interest: Money = ZERO,
    policy: TaxPolicy,
) -> LeveredTaxPeriod:
    """Compute one period's cash tax on a levered basis.

    The order is fixed and is the point of the function: the cap is struck
    against earnings taken before relief, the interest deduction is taken
    against EBIT, and the loss pool then shelters what remains. Each step reads
    the output of the one before it, and swapping any two changes the answer.
    """
    if cash_interest < 0 or accrued_interest < 0:
        raise TaxPolicyError("interest must not be negative")
    if opening_carryforward < 0:
        raise TaxPolicyError("the carryforward pool must not be negative")
    if opening_disallowed_interest < 0:
        raise TaxPolicyError("the disallowed interest pool must not be negative")

    # Interest that accrues without being paid is an expense the period incurred.
    # Where the regime declines to recognise it until it is paid, it is not
    # offered to the cap at all and waits in the pool instead, which is why it
    # is added to the carried balance rather than dropped.
    if policy.deduct_accrued_interest:
        arising = cash_interest + accrued_interest
        deferred = ZERO
    else:
        arising = cash_interest
        deferred = accrued_interest

    deduction = _limit_interest(
        arising=arising,
        carried_in=opening_disallowed_interest,
        ebitda=ebitda,
        ebit=ebit,
        limit=policy.interest_limit,
    )
    if deferred:
        deduction = InterestDeduction(
            arising=deduction.arising,
            carried_in=deduction.carried_in,
            allowance=deduction.allowance,
            deductible=deduction.deductible,
            disallowed=deduction.disallowed,
            carried_out=deduction.carried_out + deferred,
        )

    # EBIT less the interest the cap allowed. A loss here joins the pool as the
    # levered loss: interest the cap allowed is what made it a loss, so it is
    # what the pool inherits.
    taxable_income = ebit - deduction.deductible
    relief = _apply_relief(taxable_income, opening_carryforward, policy)

    # The same period with nothing deducted, from the same opening pool. This is
    # the counterfactual the saving is measured against, and it is the figure
    # the operating layer already computes for itself.
    unlevered = _apply_relief(ebit, opening_carryforward, policy)

    return LeveredTaxPeriod(
        index=index,
        ebitda=ebitda,
        ebit=ebit,
        deduction=deduction,
        taxable_income=taxable_income,
        loss_relief_used=relief.used,
        taxable_after_relief=relief.after_relief,
        cash_tax=relief.cash_tax,
        closing_carryforward=relief.closing_carryforward,
        cash_tax_unlevered=unlevered.cash_tax,
        rate=policy.rate,
    )


def levered_tax_schedule(
    *,
    ebitda: Sequence[Money],
    ebit: Sequence[Money],
    cash_interest: Sequence[Money],
    accrued_interest: Sequence[Money] | None = None,
    indices: Sequence[int] | None = None,
    policy: TaxPolicy,
    opening_carryforward: Numeric = 0,
    opening_disallowed_interest: Numeric = 0,
) -> LeveredTaxSchedule:
    """Run the levered computation across periods, rolling both pools.

    Takes bare series rather than an operating model so that the pass can be
    driven by interest written down by hand, which is how the cap and the pool
    are tested without a solver in the way. See
    :func:`levered_tax_for_model` for the usual entry point.
    """
    accrued = list(accrued_interest) if accrued_interest is not None else [ZERO] * len(ebit)
    if not (len(ebitda) == len(ebit) == len(cash_interest) == len(accrued)):
        raise TaxPolicyError("the earnings and interest series must be the same length")
    keys = list(indices) if indices is not None else list(range(1, len(ebit) + 1))
    if len(keys) != len(ebit):
        raise TaxPolicyError("one index per period is required")

    pool = money(opening_carryforward)
    disallowed = money(opening_disallowed_interest)
    rows: list[LeveredTaxPeriod] = []
    for i, key in enumerate(keys):
        row = levered_tax(
            index=key,
            ebitda=ebitda[i],
            ebit=ebit[i],
            cash_interest=cash_interest[i],
            accrued_interest=accrued[i],
            opening_carryforward=pool,
            opening_disallowed_interest=disallowed,
            policy=policy,
        )
        pool = row.closing_carryforward
        disallowed = row.deduction.carried_out
        rows.append(row)

    return LeveredTaxSchedule(
        periods=tuple(rows),
        opening_carryforward=money(opening_carryforward),
        opening_disallowed_interest=money(opening_disallowed_interest),
    )


def levered_tax_for_model(
    model: OperatingModel,
    *,
    cash_interest: Sequence[Money],
    accrued_interest: Sequence[Money] | None = None,
    policy: TaxPolicy,
    opening_carryforward: Numeric = 0,
    opening_disallowed_interest: Numeric = 0,
) -> LeveredTaxSchedule:
    """Run the levered computation against an operating case's earnings.

    The earnings come from the model and the interest from the caller, which is
    the shape the circularity above this layer needs: the solver holds the
    operating case still and varies the interest.
    """
    return levered_tax_schedule(
        ebitda=[p.ebitda for p in model],
        ebit=[p.ebit for p in model],
        cash_interest=cash_interest,
        accrued_interest=accrued_interest,
        indices=[p.index for p in model],
        policy=policy,
        opening_carryforward=opening_carryforward,
        opening_disallowed_interest=opening_disallowed_interest,
    )


# -- The circularity -----------------------------------------------------

#: A function that runs the capital structure against a cash-flow series.
#:
#: The solve varies the cash flow and reads back the interest, and has no other
#: business with the schedule. Taking a runner rather than a structure is what
#: keeps recapitalisations, add-ons, refinancings and fee write-offs out of this
#: layer: the caller configures them once, closes over them, and the fixed point
#: never learns they exist.
ScheduleRunner = Callable[[Sequence[Money]], DebtSchedule]


@dataclass(frozen=True, slots=True)
class LeveredSolution:
    """A capital structure and a tax computation that agree with each other."""

    schedule: DebtSchedule
    tax: LeveredTaxSchedule
    #: The cash flow the schedule was finally run against: the operating case's
    #: unlevered free cash flow plus the saving the deduction produced.
    cash_flows: tuple[Money, ...]
    #: How many times the schedule was run, the first pass included.
    iterations: int
    #: The largest period-on-period movement in the saving when the solve
    #: stopped. Reported rather than discarded, because a caller comparing two
    #: structures to a hundredth of a turn should be able to see it.
    residual: Money

    @property
    def total_shield(self) -> Money:
        """Cash tax the structure saved, net of the debt the saving repaid."""
        return self.tax.total_shield

    @property
    def shields(self) -> tuple[Money, ...]:
        return tuple(p.shield for p in self.tax)


def _shield_series(model: OperatingModel, tax: LeveredTaxSchedule) -> list[Money]:
    """The saving per period, measured against the operating case's own tax line.

    Not against the tax layer's internal counterfactual. The two agree — that
    is checked before the solve starts — but the unlevered free cash flow the
    saving is added to was struck net of the *model's* figure, so the model's
    figure is the one the adjustment has to be measured from or the cash flow
    double-counts the difference between them.
    """
    return [period.tax.cash_tax - row.cash_tax for period, row in zip(model, tax, strict=True)]


def solve_levered(
    *,
    model: OperatingModel,
    run: ScheduleRunner,
    policy: TaxPolicy,
    opening_carryforward: Numeric = 0,
    opening_disallowed_interest: Numeric = 0,
    tolerance: Numeric = DEFAULT_SOLVE_TOLERANCE,
    max_iterations: int = MAX_SOLVE_ITERATIONS,
) -> LeveredSolution:
    """Settle the interest deduction against the debt it is charged on.

    Two sequential passes, alternated. The tax pass has to see the whole hold at
    once because both pools carry forward, which is why this cannot be folded
    into the schedule's own per-period fixed point: it sits outside it, and a
    structure can solve the inner one in every period and still fail here.

    The map contracts hard. One turn of the loop moves the saving by the tax
    rate times the interest on the cash the last turn freed, so the residual
    falls by roughly ``rate x coupon x year_fraction`` each time — a few tenths
    of a per cent at any realistic combination — and the feedback is negative:
    a larger deduction frees cash, the cash repays debt, and the smaller balance
    earns the smaller interest that shrinks the next deduction. Across the
    structures in the suite no solve has taken more than four passes.

    Before iterating, the solve checks its own counterfactual: run with no
    interest at all, the tax pass must reproduce the operating case's tax line
    period for period. A policy whose rate or loss limit disagrees with the case
    it is handed fails that check and is refused, because the saving credited to
    the cash flow is a difference against that line and measuring it off a
    different base would quietly misstate every period.
    """
    if max_iterations < 1:
        raise TaxPolicyError("a solve needs at least one iteration")
    limit = money(tolerance)
    if limit < 0:
        raise TaxPolicyError("the tolerance must not be negative")

    unlevered = levered_tax_for_model(
        model,
        cash_interest=[ZERO] * len(model),
        policy=policy,
        opening_carryforward=opening_carryforward,
        opening_disallowed_interest=opening_disallowed_interest,
    )
    for period, row in zip(model, unlevered, strict=True):
        if row.cash_tax != period.tax.cash_tax:
            raise TaxPolicyError(
                "this tax policy disagrees with the operating case it is being run "
                f"against: with no interest at all, period {period.index} pays "
                f"{row.cash_tax} here and {period.tax.cash_tax} in the projection. "
                "Build the policy with TaxPolicy.from_assumptions so the rate and "
                "the loss limit come from the case itself."
            )

    base = [p.unlevered_free_cash_flow for p in model]
    shields = [ZERO] * len(model)
    # The series the schedule in hand was actually run against, so that what is
    # returned is self-consistent: the schedule, the tax read off it, and the
    # cash flow that produced it all describe the same pass.
    applied = list(base)
    schedule = run(applied)
    tax = unlevered
    residual = ZERO
    iterations = 1

    while True:
        tax = levered_tax_for_model(
            model,
            cash_interest=[p.cash_interest for p in schedule],
            accrued_interest=[p.pik_interest for p in schedule],
            policy=policy,
            opening_carryforward=opening_carryforward,
            opening_disallowed_interest=opening_disallowed_interest,
        )
        fresh = _shield_series(model, tax)
        residual = max((abs(a - b) for a, b in zip(fresh, shields, strict=True)), default=ZERO)
        shields = fresh
        if residual <= limit:
            # The schedule in hand was run against the previous saving series,
            # which this one is within tolerance of, so it is the answer. The
            # flows returned are the ones that produced it, not the ones just
            # computed, which no schedule has been run against.
            return LeveredSolution(
                schedule=schedule,
                tax=tax,
                cash_flows=tuple(applied),
                iterations=iterations,
                residual=residual,
            )
        flows = [cf + s for cf, s in zip(base, shields, strict=True)]
        if iterations >= max_iterations:
            raise TaxCircularityError(
                f"the interest deduction and the debt it is charged on did not "
                f"settle in {max_iterations} passes; the saving was still moving "
                f"by {residual} when the solve stopped"
            )
        applied = flows
        schedule = run(applied)
        iterations += 1


def levered_schedule(
    structure: CapitalStructure,
    model: OperatingModel,
    *,
    policy: TaxPolicy,
    opening_cash: Numeric = 0,
    opening_ebitda: Numeric | None = None,
    opening_carryforward: Numeric = 0,
    opening_disallowed_interest: Numeric = 0,
    tolerance: Numeric = DEFAULT_SOLVE_TOLERANCE,
    max_iterations: int = MAX_SOLVE_ITERATIONS,
) -> LeveredSolution:
    """Run a structure against a case, with the deduction settled against it.

    The plain entry point, for a structure with no events attached to it. A deal
    with recapitalisations, add-ons or refinancings builds its own runner and
    calls :func:`solve_levered` directly, which is what ``Deal.levered`` does.
    """
    ebitda = [model.trailing_ebitda(i) for i in range(len(model))]
    anchor = opening_ebitda if opening_ebitda is not None else model.entry_ebitda

    def run(cash_flows: Sequence[Money]) -> DebtSchedule:
        return DebtSchedule.run(
            structure,
            [p.period for p in model],
            cash_flows,
            opening_cash=opening_cash,
            ebitda=ebitda,
            opening_ebitda=anchor,
        )

    return solve_levered(
        model=model,
        run=run,
        policy=policy,
        opening_carryforward=opening_carryforward,
        opening_disallowed_interest=opening_disallowed_interest,
        tolerance=tolerance,
        max_iterations=max_iterations,
    )
