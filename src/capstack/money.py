"""Exact currency arithmetic.

A debt schedule is a chain of dependent subtractions running many periods deep.
Each period's closing balance is the next period's opening balance, so an error
introduced in period one is still present in period twenty. Binary floating
point introduces such an error on the very first conversion: 0.1 is not
representable, and neither is any of the tenths and hundredths that make up an
interest rate or an amortisation percentage.

The visible symptom is a tranche that is supposed to amortise to exactly zero
and instead lands on a residue of a few billionths, which then draws a revolver
in the final period to repay it. So every currency amount here is a ``Decimal``,
carried at high precision and rounded only when it is presented.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, setcontext
from collections.abc import Sequence
from typing import Union

__all__ = [
    "Money",
    "Numeric",
    "ZERO",
    "ONE",
    "money",
    "rate",
    "to_float",
    "quantize",
    "safe_div",
    "is_close",
    "allocate_pro_rata",
]

Money = Decimal
Numeric = Union[int, str, float, Decimal]

#: Working precision. Deal models run to the billions with cent-level detail,
#: which is about 12 significant digits; 34 leaves ample room for intermediate
#: products such as a balance multiplied by a rate multiplied by a year fraction.
PRECISION = 34

setcontext(Context(prec=PRECISION, rounding=ROUND_HALF_EVEN))

ZERO: Money = Decimal(0)
ONE: Money = Decimal(1)

#: Rounding used when an amount is presented rather than carried. Half-even
#: rather than half-up: a schedule rounds thousands of numbers and half-up would
#: bias every one of them upward.
DISPLAY_ROUNDING = ROUND_HALF_EVEN


def money(value: Numeric) -> Money:
    """Coerce ``value`` to an exact decimal amount.

    Floats are routed through ``repr`` rather than ``Decimal(float)``. The
    direct construction is faithful to the binary value, which is the problem:
    ``Decimal(0.1)`` is ``0.1000000000000000055511151231257827021181583404541015625``
    and carrying that through a model is exactly the drift this module exists to
    avoid. ``repr`` gives the shortest string that round-trips, so a float that
    was written as ``0.1`` becomes the decimal ``0.1``.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool is an int subclass; almost never intended
        raise TypeError("refusing to treat a bool as an amount")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"cannot represent {value!r} as an amount")
        return Decimal(repr(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"not a number: {value!r}") from exc
    raise TypeError(f"cannot convert {type(value).__name__} to an amount")


def rate(value: Numeric) -> Money:
    """Coerce ``value`` to an exact decimal rate.

    Identical mechanics to :func:`money`; the separate name records intent at
    the call site, because a rate and an amount are not interchangeable even
    though they share a representation.
    """
    return money(value)


def to_float(value: Money) -> float:
    """Drop to floating point at a deliberate boundary.

    Root finding wants floats. Calling this is a statement that the value is
    about to leave exact arithmetic on purpose.
    """
    return float(value)


def quantize(value: Money, places: int = 2) -> Money:
    """Round to ``places`` decimal places for presentation."""
    if places < 0:
        raise ValueError("places must not be negative")
    exponent = Decimal(1).scaleb(-places)
    return value.quantize(exponent, rounding=DISPLAY_ROUNDING)


def safe_div(numerator: Money, denominator: Money, *, default: Money | None = None) -> Money:
    """Divide, substituting ``default`` when the denominator is zero.

    Financial ratios divide by quantities that are legitimately zero: a company
    with no debt has no leverage ratio, and one with no interest expense has no
    coverage ratio. Those are not errors, but they are also not numbers, so the
    caller has to say what it wants in that case.
    """
    if denominator == 0:
        if default is None:
            raise ZeroDivisionError("division by zero with no default supplied")
        return default
    return numerator / denominator


def is_close(a: Money, b: Money, *, tolerance: Numeric = "0.01") -> bool:
    """Whether two amounts agree to within ``tolerance``.

    Used for balance checks, where the question is whether two independently
    computed totals agree to the cent, not whether they are bit-identical.
    """
    return abs(a - b) <= money(tolerance)


def allocate_pro_rata(pot: Money, claims: Sequence[Money]) -> list[Money]:
    """Split ``pot`` across ``claims`` pro rata, exactly and without residue.

    Every share but one is computed pro rata and the remaining claim takes what
    is left over. Pro-rating all of them independently would leave a residue of
    a few billionths whenever the ratio does not terminate, and a residue is
    precisely what stops a balance from reaching zero — a tranche that will not
    amortise away, or a security that is repaid in full and still shows a
    fraction of a penny outstanding.

    The remainder goes to the last claim with something outstanding, never
    simply to the last in the list. A claim already at zero is entitled to
    nothing, and handing it the rounding dust gives it a balance of a few
    billionths — of either sign — that nothing afterwards can clear, because
    both the payment and the balance are floored at zero.

    Nobody is paid more than they are owed: the total distributed is the lesser
    of ``pot`` and the claims against it.
    """
    total = sum(claims, ZERO)
    if total <= 0 or pot <= 0:
        return [ZERO for _ in claims]
    payable = min(pot, total)
    plug = max(i for i, claim in enumerate(claims) if claim > 0)

    shares = [ZERO for _ in claims]
    remaining = payable
    for i, claim in enumerate(claims):
        if i == plug or claim <= 0:
            continue
        share = payable * claim / total
        shares[i] = share
        remaining -= share
    shares[plug] = min(max(remaining, ZERO), claims[plug])
    return shares
