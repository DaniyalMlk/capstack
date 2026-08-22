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
from .spec import DealSpecError, load_deal, parse_deal
from .transaction import (
    DebtFunding,
    EntryValuation,
    LineItem,
    SourcesAndUses,
    Transaction,
    UnbalancedTransaction,
)

__version__ = "0.1.0"

__all__ = [
    "AmbiguousIRR",
    "CashFlow",
    "CashFlowStream",
    "DayCount",
    "DealSpecError",
    "DebtFunding",
    "EntryValuation",
    "Frequency",
    "IRRError",
    "LineItem",
    "Money",
    "NoSignChange",
    "Period",
    "PeriodGrid",
    "SourcesAndUses",
    "Transaction",
    "UnbalancedTransaction",
    "__version__",
    "cagr",
    "irr_periodic",
    "load_deal",
    "moic",
    "money",
    "npv_periodic",
    "parse_deal",
    "quantize",
    "rate",
    "year_fraction",
]
