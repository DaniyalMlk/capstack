"""capstack — a leveraged buyout engine."""

from .balance_sheet import (
    BalanceSheetError,
    OpeningBalanceSheet,
    PurchaseAccounting,
    TargetBookBalanceSheet,
    UnbalancedBalanceSheet,
)
from .daycount import DayCount, year_fraction
from .drivers import Driver
from .money import Money, money, quantize, rate
from .operating import (
    OperatingAssumptions,
    OperatingModel,
    OperatingPeriod,
    TaxResult,
    apply_carryforward,
)
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
from .spec import Deal, DealSpecError, load_deal, parse_deal
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
    "BalanceSheetError",
    "CashFlow",
    "CashFlowStream",
    "DayCount",
    "Deal",
    "DealSpecError",
    "DebtFunding",
    "Driver",
    "EntryValuation",
    "Frequency",
    "IRRError",
    "LineItem",
    "Money",
    "NoSignChange",
    "OpeningBalanceSheet",
    "OperatingAssumptions",
    "OperatingModel",
    "OperatingPeriod",
    "Period",
    "PeriodGrid",
    "PurchaseAccounting",
    "SourcesAndUses",
    "TargetBookBalanceSheet",
    "TaxResult",
    "Transaction",
    "UnbalancedBalanceSheet",
    "UnbalancedTransaction",
    "__version__",
    "apply_carryforward",
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
