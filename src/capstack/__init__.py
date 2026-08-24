"""capstack — a leveraged buyout engine."""

from .balance_sheet import (
    BalanceSheetError,
    OpeningBalanceSheet,
    PurchaseAccounting,
    TargetBookBalanceSheet,
    UnbalancedBalanceSheet,
)
from .covenants import (
    Covenant,
    CovenantObservation,
    CovenantReport,
    Direction,
    Measure,
)
from .daycount import DayCount, year_fraction
from .debt import (
    CapitalStructure,
    CircularityNotResolved,
    DebtPeriod,
    DebtSchedule,
    InterestBasis,
    SweepGrid,
    SweepStep,
    Tranche,
    TranchePeriod,
    TrancheKind,
)
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
    "CapitalStructure",
    "CashFlow",
    "CashFlowStream",
    "CircularityNotResolved",
    "Covenant",
    "CovenantObservation",
    "CovenantReport",
    "DayCount",
    "Deal",
    "DealSpecError",
    "DebtFunding",
    "DebtPeriod",
    "DebtSchedule",
    "Direction",
    "Driver",
    "EntryValuation",
    "Frequency",
    "IRRError",
    "InterestBasis",
    "LineItem",
    "Measure",
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
    "SweepGrid",
    "SweepStep",
    "TargetBookBalanceSheet",
    "TaxResult",
    "Tranche",
    "TrancheKind",
    "TranchePeriod",
    "Transaction",
    "UnbalancedBalanceSheet",
    "UnbalancedTransaction",
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
    "__version__",
]
