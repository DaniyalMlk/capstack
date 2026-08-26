"""The management incentive plan: what the people running the business are paid.

Every layer below this one describes a deal in which management is paid nothing
for five years of work, which is not a structure anybody signs. The plan sits
between the preferred claims and the residual split, and it takes its share out
of the common — the sponsor's own return included.

Three things have to be right for the number to mean anything.

*It is an option, not a share.* A pool struck at the equity value at close earns
on value created after close and nothing else. The arithmetic is the treasury
method: the exercise proceeds join the pot before the pot is divided, so a pool
holding a tenth of the fully diluted equity is not holding a tenth of the
residual — it is holding a tenth of the residual plus its own strike, less the
strike. Below the point where those two are equal the pool is out of the money,
takes nothing, and dilutes nobody. That is a real discontinuity in the payoff and
it is the reason a plan can look expensive in the base case and cost nothing in
the downside.

*It vests.* An exit three years into a five-year vest leaves two years unvested.
The unvested part is forfeited here rather than accelerated, because acceleration
on a change of control is a term that gets negotiated rather than a fact about
how options work. Where it has been negotiated, it is a flag.

*It ratchets.* A flat share is the simple case and the less common one. Most
plans step the pool up as the sponsor clears hurdles, and writing that down
naively produces a circle: the pool's share depends on the sponsor's return,
which depends on the pool's share. Models that implement the circle either
iterate to whatever they land on, or test the hurdle on a pre-dilution figure and
leave the reader to discover that 2.0x meant 2.1x before management were paid.

Neither is necessary. A ratchet written the way a well-drafted one reads — a
*marginal* share of the proceeds in each band above a hurdle, rather than a
retroactive share of everything — makes the sponsor's post-ratchet proceeds a
continuous, strictly increasing function of the pot. Each band's upper boundary
can then be solved in closed form from the boundary below it, in order, from the
bottom up. No iteration, no ambiguity, and the hurdles mean what they say: at the
pot where the second band opens, the sponsor has received exactly 2.0x on what it
actually takes home.

The retroactive alternative — the pool's higher share applying to the whole pot
once a hurdle is cleared — is a genuine structure and it is deliberately not
modelled, because it is discontinuous. A penny more of enterprise value can leave
the sponsor with less money than it had a penny earlier, and any solver asked
where the hurdle binds has to pick between two answers on either side of a cliff.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from .money import ONE, ZERO, Money, Numeric, is_close, money, safe_div

__all__ = [
    "IncentiveError",
    "OptionPool",
    "PoolOutcome",
    "Ratchet",
    "RatchetBand",
    "Vesting",
    "settle_pool",
]


class IncentiveError(ValueError):
    """The plan as described cannot be settled."""


# --------------------------------------------------------------------------
# Vesting
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Vesting:
    """How much of the pool has been earned by the time the business is sold.

    Straight-line from close over ``years``, with nothing earned before the
    cliff and everything earned once the schedule has run. The cliff does not
    slow the accrual — it withholds it: a one-year cliff on a four-year vest
    pays a quarter of the pool on the day it lapses, not a quarter of a year's
    worth.
    """

    years: Money
    cliff_years: Money = ZERO
    accelerates: bool = False

    @classmethod
    def of(
        cls,
        years: Numeric,
        *,
        cliff_years: Numeric = 0,
        accelerates: bool = False,
    ) -> Vesting:
        return cls(
            years=money(years),
            cliff_years=money(cliff_years),
            accelerates=accelerates,
        )

    def __post_init__(self) -> None:
        if self.years <= 0:
            raise IncentiveError("the vesting period must be positive")
        if self.cliff_years < 0:
            raise IncentiveError("the cliff must not be negative")
        if self.cliff_years > self.years:
            raise IncentiveError(
                f"a cliff at {self.cliff_years} years on a {self.years}-year vest "
                f"never lapses before the schedule finishes"
            )

    def vested_at(self, years: Money) -> Money:
        """The share of the pool earned after ``years`` from close.

        Acceleration is checked first and answers the question outright: a plan
        that accelerates on a change of control vests in full at exit whatever
        the schedule says, including before the cliff.
        """
        if self.accelerates:
            return ONE
        if years <= 0 or years < self.cliff_years:
            return ZERO
        if years >= self.years:
            return ONE
        return years / self.years


#: What a plan with no vesting schedule is worth at exit: all of it.
FULLY_VESTED: Money = ONE


# --------------------------------------------------------------------------
# The ratchet
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class RatchetBand:
    """A marginal share of the pot, earned above a hurdle.

    ``hurdle`` is a money multiple on the capital the ratchet is measured
    against, and ``share`` is the fraction of *incremental* proceeds the pool
    takes once that multiple has been reached. The first band's hurdle is zero:
    it is the share the pool holds before any hurdle binds, which is commonly
    but not always nothing.
    """

    hurdle: Money
    share: Money

    @classmethod
    def of(cls, hurdle: Numeric, share: Numeric) -> RatchetBand:
        return cls(hurdle=money(hurdle), share=money(share))

    def __post_init__(self) -> None:
        if self.hurdle < 0:
            raise IncentiveError("a hurdle must not be negative")
        if not (0 <= self.share < 1):
            raise IncentiveError(
                f"a marginal share of {self.share} leaves the holders it dilutes "
                f"nothing at the margin; it must be at least 0 and below 1"
            )


@dataclass(frozen=True, slots=True)
class Ratchet:
    """Bands of marginal entitlement, opening as the measured capital clears hurdles.

    ``measured_on`` names the instruments whose combined proceeds and capital
    define the multiple the hurdles are read against. Left empty it means the
    equity as a whole, which is the reading to use when the sponsor is the only
    institutional holder. Naming a subset is how a plan that watches the
    sponsor's own paper — and not the rollover sitting beside it — gets written
    down.
    """

    bands: tuple[RatchetBand, ...]
    measured_on: tuple[str, ...] = ()

    @classmethod
    def of(
        cls,
        bands: Sequence[tuple[Numeric, Numeric]] | Sequence[RatchetBand],
        *,
        measured_on: Sequence[str] = (),
    ) -> Ratchet:
        parsed: list[RatchetBand] = []
        for band in bands:
            if isinstance(band, RatchetBand):
                parsed.append(band)
            else:
                hurdle, share = band
                parsed.append(RatchetBand.of(hurdle, share))
        return cls(bands=tuple(parsed), measured_on=tuple(measured_on))

    def __post_init__(self) -> None:
        if not self.bands:
            raise IncentiveError("a ratchet needs at least one band")
        if self.bands[0].hurdle != 0:
            raise IncentiveError(
                f"the first band opens at a multiple of {self.bands[0].hurdle}, so "
                f"the pool's entitlement below that is undescribed; start the "
                f"bands at 0"
            )
        for lower, upper in zip(self.bands, self.bands[1:]):
            if upper.hurdle <= lower.hurdle:
                raise IncentiveError(
                    f"hurdles must step up: {upper.hurdle} does not come after "
                    f"{lower.hurdle}"
                )
            if upper.share < lower.share:
                raise IncentiveError(
                    f"the band above {upper.hurdle}x pays {upper.share} where the "
                    f"one below pays {lower.share}; a plan that pays less for a "
                    f"better outcome is not a ratchet"
                )
        if len(set(name.strip() for name in self.measured_on)) != len(self.measured_on):
            raise IncentiveError("the instruments a ratchet is measured on must be distinct")

    def __len__(self) -> int:
        return len(self.bands)

    def __iter__(self) -> Iterator[RatchetBand]:
        return iter(self.bands)

    @property
    def top_share(self) -> Money:
        return self.bands[-1].share

    def boundaries(
        self,
        *,
        measured_capital: Money,
        measured_prior: Money,
        measured_ownership: Money,
        vested: Money = FULLY_VESTED,
    ) -> tuple[Money, ...]:
        """The pot at which each band opens, from the bottom up.

        ``measured_prior`` is what the measured instruments have already
        received before the residual is touched — their preferred claims. It
        counts towards the hurdle, because a money multiple is measured on
        everything a holder receives and not only on the part that arrives last.

        ``measured_ownership`` is their combined share of what is left after the
        pool has taken its cut, which is the slope of their proceeds against the
        pot inside a band.

        The returned tuple has one entry per band. The first is always zero. A
        band whose hurdle was already cleared by the bands below it opens at the
        same pot as its predecessor and is therefore empty, which is the honest
        answer rather than an error: a plan can be written with a hurdle that
        cannot bite.
        """
        if measured_capital <= 0:
            raise IncentiveError(
                "the instruments a ratchet is measured on put in no capital, so "
                "there is no multiple to test the hurdles against"
            )

        opens: list[Money] = [ZERO]
        pot = ZERO
        proceeds = measured_prior
        for lower, upper in zip(self.bands, self.bands[1:]):
            target = upper.hurdle * measured_capital
            slope = measured_ownership * (ONE - vested * lower.share)
            if slope <= 0:
                # The measured holders take nothing at the margin here, so no
                # amount of further proceeds reaches the next hurdle. Every
                # remaining band opens beyond any reachable pot.
                opens.extend(_unreachable(len(self.bands) - len(opens)))
                return tuple(opens)
            width = safe_div(target - proceeds, slope, default=ZERO)
            if width <= 0:
                # Already cleared. The band opens where the one below it did,
                # leaving it empty rather than pretending it starts earlier.
                opens.append(pot)
                continue
            pot += width
            proceeds = target
            opens.append(pot)
        return tuple(opens)

    def entitlement(
        self,
        pot: Money,
        *,
        measured_capital: Money,
        measured_prior: Money,
        measured_ownership: Money,
        vested: Money = FULLY_VESTED,
    ) -> Money:
        """What the pool is entitled to out of ``pot``, band by band."""
        if pot <= 0:
            return ZERO
        opens = self.boundaries(
            measured_capital=measured_capital,
            measured_prior=measured_prior,
            measured_ownership=measured_ownership,
            vested=vested,
        )
        total = ZERO
        for i, band in enumerate(self.bands):
            start = opens[i]
            if start >= pot:
                break
            end = opens[i + 1] if i + 1 < len(opens) else pot
            width = min(end, pot) - start
            if width > 0:
                total += vested * band.share * width
        return total


#: A pot no deal reaches, used to mark bands that can never open because the
#: holders the ratchet watches take nothing at the margin below them.
_UNREACHABLE: Money = money("1E30")


def _unreachable(count: int) -> list[Money]:
    return [_UNREACHABLE for _ in range(max(count, 0))]


# --------------------------------------------------------------------------
# The pool
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class OptionPool:
    """Options over a share of the fully diluted common, held by management.

    ``share`` is that fraction, and it is what applies when there is no ratchet.
    With a ratchet the bands describe the entitlement instead and ``share``
    becomes the figure the plan is quoted at rather than the one it settles on;
    it is still validated, because a file that describes both should not have
    them disagree about what is possible.

    ``strike`` is the aggregate cost of exercising the whole pool. Zero makes
    the plan a straight sweet-equity stake in the residual, which is a real
    structure and the one to model when management have bought their shares
    rather than been granted options over them.
    """

    name: str
    share: Money
    strike: Money = ZERO
    vesting: Vesting | None = None
    ratchet: Ratchet | None = None

    @classmethod
    def of(
        cls,
        name: str,
        share: Numeric,
        *,
        strike: Numeric = 0,
        vesting: Vesting | None = None,
        ratchet: Ratchet | None = None,
    ) -> OptionPool:
        return cls(
            name=name,
            share=money(share),
            strike=money(strike),
            vesting=vesting,
            ratchet=ratchet,
        )

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise IncentiveError("an incentive plan needs a name")
        if not (0 <= self.share < 1):
            raise IncentiveError(
                f"{self.name}: the pool holds a share of the equity, so at least 0 "
                f"and below 1; got {self.share}"
            )
        if self.strike < 0:
            raise IncentiveError(f"{self.name}: the strike must not be negative")

    def vested_at(self, years: Money) -> Money:
        return FULLY_VESTED if self.vesting is None else self.vesting.vested_at(years)


@dataclass(frozen=True, slots=True)
class PoolOutcome:
    """What the plan was worth, and what it cost the holders it diluted.

    ``residual`` is what reached the common before the pool touched it.
    ``pot`` is that plus the exercise proceeds where the options were exercised,
    which is the amount the entitlement is measured against. ``paid`` is what
    management take away after paying the strike, and ``dilution`` is the same
    number seen from the other side: exactly what the common gave up.
    """

    pool: OptionPool
    residual: Money
    vested: Money
    entitlement: Money
    strike_paid: Money
    exercised: bool

    @property
    def name(self) -> str:
        return self.pool.name

    @property
    def pot(self) -> Money:
        """The amount divided: the residual, enlarged by any strike paid in."""
        return self.residual + self.strike_paid

    @property
    def paid(self) -> Money:
        """What management receive, net of exercising."""
        return self.entitlement - self.strike_paid

    @property
    def to_common(self) -> Money:
        """What is left for the common holders after the plan."""
        return self.pot - self.entitlement

    @property
    def dilution(self) -> Money:
        """What the common gave up: the residual they would have had, less what they have."""
        return self.residual - self.to_common

    @property
    def effective_share(self) -> Money:
        """The pool's take as a share of the pot it was measured against.

        With a flat plan this is the vested share. With a ratchet it is the
        blended figure the bands produced, which is the number worth reporting
        precisely because it is not one of the inputs.
        """
        return safe_div(self.entitlement, self.pot, default=ZERO)

    @property
    def is_in_the_money(self) -> bool:
        return self.exercised

    def reconciles(self, tolerance: Numeric = "1E-12") -> bool:
        """Whether the two sides of the plan agree.

        What management take and what the common give up are the same quantity
        reached from opposite ends — one is the entitlement net of the strike,
        the other is the residual net of what the common were left. They are
        algebraically identical, which is why this is worth checking: the two
        expressions associate their arithmetic differently, so the check is a
        real one against the working precision rather than a tautology.
        """
        return is_close(self.paid, self.dilution, tolerance=tolerance)

    @property
    def forfeited_share(self) -> Money:
        """The part of the pool that had not vested by the exit."""
        return ONE - self.vested


def settle_pool(
    pool: OptionPool,
    residual: Money,
    *,
    years: Money,
    measured_capital: Money = ZERO,
    measured_prior: Money = ZERO,
    measured_ownership: Money = ONE,
) -> PoolOutcome:
    """Settle the plan against the residual reaching the common.

    The exercise decision is management's and it is taken on the arithmetic:
    exercise where the entitlement exceeds the cost of exercising, and let the
    options lapse where it does not. Both branches are computed rather than
    assumed, and they agree at the boundary — an entitlement exactly equal to
    the strike is worth nothing either way — so the payoff is continuous in the
    residual even though the decision is not.

    Vesting scales the pool and its strike together. Management pay for the
    options they exercise and not for the ones they forfeited, so a half-vested
    pool exercises half the strike.
    """
    if residual < 0:
        raise IncentiveError("the residual reaching the common must not be negative")

    vested = pool.vested_at(years)
    strike = pool.strike * vested
    pot = residual + strike

    if pool.ratchet is not None:
        entitlement = pool.ratchet.entitlement(
            pot,
            measured_capital=measured_capital,
            measured_prior=measured_prior,
            measured_ownership=measured_ownership,
            vested=vested,
        )
    else:
        entitlement = pot * pool.share * vested

    if entitlement > strike:
        return PoolOutcome(
            pool=pool,
            residual=residual,
            vested=vested,
            entitlement=entitlement,
            strike_paid=strike,
            exercised=True,
        )

    # Out of the money. The options lapse, nothing is paid in, nothing is taken
    # out, and the common keep the residual whole.
    return PoolOutcome(
        pool=pool,
        residual=residual,
        vested=vested,
        entitlement=ZERO,
        strike_paid=ZERO,
        exercised=False,
    )
