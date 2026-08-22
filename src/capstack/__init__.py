"""capstack — a leveraged buyout engine."""

from .daycount import DayCount, year_fraction
from .money import Money, money, quantize, rate
from .periods import Frequency, Period, PeriodGrid
from .returns import (
    AmbiguousIRR,
    CashFlow,
    CashFlowStream,
    IRRError,
    NoSignChange,
    cagr,
    irr_periodic,
    moic,
    npv_periodic,
)

__version__ = "0.1.0"

__all__ = [
    "AmbiguousIRR",
    "CashFlow",
    "CashFlowStream",
    "DayCount",
    "Frequency",
    "IRRError",
    "Money",
    "NoSignChange",
    "Period",
    "PeriodGrid",
    "__version__",
    "cagr",
    "irr_periodic",
    "moic",
    "money",
    "npv_periodic",
    "quantize",
    "rate",
    "year_fraction",
]
