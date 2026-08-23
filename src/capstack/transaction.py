"""The transaction: what is bought, and what pays for it.

A sources and uses table is the first page of every deal model. Uses are
everything cash goes towards at close; sources are everything cash comes from.
The two sides are equal by construction, and the sponsor's equity cheque is
whatever number makes them equal.

The equality is exact here, not approximate. A table that balances to within a
few thousand on a billion-dollar deal has not nearly balanced — it has a missing
line item, and making that impossible to overlook is most of what the table is
for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .money import ONE, ZERO, Money, Numeric, money, safe_div

__all__ = [
    "DebtFunding",
    "EntryValuation",
    "LineItem",
    "SourcesAndUses",
    "Transaction",
    "UnbalancedTransaction",
]


class UnbalancedTransaction(ValueError):
    """Sources and uses do not agree."""


class LineKind(Enum):
    SOURCE = "source"
    USE = "use"


@dataclass(frozen=True, slots=True)
class LineItem:
    """One row of the funding table.

    ``capitalised`` says what a payment leaves behind. Most closing payments buy
    nothing — a change-of-control payment to management settles a contract and
    is gone — and those are charged against equity on day one. A few buy
    something the balance sheet keeps, such as a licence acquired alongside the
    business. The funding table does not care either way, but the opening
    balance sheet cannot be built without knowing.
    """

    label: str
    amount: Money
    note: str = ""
    capitalised: bool = False

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise ValueError("a line item needs a label")


@dataclass(frozen=True, slots=True)
class EntryValuation:
    """What the business is worth going in, and what the equity in it costs.

    The multiple is applied to last-twelve-months EBITDA to give enterprise
    value. Getting from there to the cheque written to selling shareholders
    means undoing the target's own capital structure: repay what it owes, keep
    what it holds.
    """

    ltm_ebitda: Money
    entry_multiple: Money
    existing_debt: Money = ZERO
    existing_cash: Money = ZERO

    @classmethod
    def of(
        cls,
        ltm_ebitda: Numeric,
        entry_multiple: Numeric,
        *,
        existing_debt: Numeric = 0,
        existing_cash: Numeric = 0,
    ) -> EntryValuation:
        return cls(
            ltm_ebitda=money(ltm_ebitda),
            entry_multiple=money(entry_multiple),
            existing_debt=money(existing_debt),
            existing_cash=money(existing_cash),
        )

    def __post_init__(self) -> None:
        if self.ltm_ebitda <= 0:
            raise ValueError("a buyout needs positive EBITDA to price off")
        if self.entry_multiple <= 0:
            raise ValueError("the entry multiple must be positive")
        if self.existing_debt < 0:
            raise ValueError("existing debt must not be negative")
        if self.existing_cash < 0:
            raise ValueError("existing cash must not be negative")

    @property
    def enterprise_value(self) -> Money:
        return self.ltm_ebitda * self.entry_multiple

    @property
    def net_debt(self) -> Money:
        return self.existing_debt - self.existing_cash

    @property
    def equity_purchase_price(self) -> Money:
        """What selling shareholders receive.

        Enterprise value less what the business owes, plus what it holds. This
        can be negative for a business bought at a low multiple with more debt
        than the enterprise is worth — a distressed situation, and one the model
        should be able to express rather than reject.
        """
        return self.enterprise_value - self.net_debt


@dataclass(frozen=True, slots=True)
class DebtFunding:
    """A tranche as it appears in the funding table.

    ``issue_price`` is expressed per unit of face: 1 is par, 0.99 is ninety-nine
    cents on the dollar. A tranche placed below par raises less than it carries,
    and the shortfall — original issue discount — is a use of funds. Modelling
    the tranche at proceeds instead of face is the common mistake: it
    understates leverage and understates every interest payment that follows,
    because interest accrues on what is owed rather than on what was received.
    """

    name: str
    face: Money
    issue_price: Money = ONE
    financing_fee_rate: Money = ZERO

    @classmethod
    def of(
        cls,
        name: str,
        face: Numeric,
        *,
        issue_price: Numeric = 1,
        financing_fee_rate: Numeric = 0,
    ) -> DebtFunding:
        return cls(
            name=name,
            face=money(face),
            issue_price=money(issue_price),
            financing_fee_rate=money(financing_fee_rate),
        )

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("a tranche needs a name")
        if self.face < 0:
            raise ValueError(f"{self.name}: face amount must not be negative")
        if not (0 < self.issue_price <= 2):
            raise ValueError(
                f"{self.name}: issue price is per unit of face, so 0.99 rather than 99"
            )
        if self.financing_fee_rate < 0:
            raise ValueError(f"{self.name}: financing fee rate must not be negative")

    @property
    def proceeds(self) -> Money:
        """Cash actually raised."""
        return self.face * self.issue_price

    @property
    def original_issue_discount(self) -> Money:
        """Face less proceeds. Negative for paper placed above par."""
        return self.face - self.proceeds

    @property
    def financing_fee(self) -> Money:
        """Arrangement and underwriting fees, charged on face."""
        return self.face * self.financing_fee_rate


@dataclass(frozen=True, slots=True)
class SourcesAndUses:
    """A funding table that is equal on both sides by construction."""

    sources: tuple[LineItem, ...]
    uses: tuple[LineItem, ...]

    def __post_init__(self) -> None:
        if self.total_sources != self.total_uses:
            raise UnbalancedTransaction(
                f"sources ({self.total_sources}) do not equal uses ({self.total_uses}); "
                f"the difference is {self.total_sources - self.total_uses}"
            )

    @property
    def total_sources(self) -> Money:
        return sum((item.amount for item in self.sources), ZERO)

    @property
    def total_uses(self) -> Money:
        return sum((item.amount for item in self.uses), ZERO)

    def source(self, label: str) -> Money:
        """Amount on the sources side under ``label``, zero if absent."""
        return sum((i.amount for i in self.sources if i.label == label), ZERO)

    def use(self, label: str) -> Money:
        """Amount on the uses side under ``label``, zero if absent."""
        return sum((i.amount for i in self.uses if i.label == label), ZERO)


#: Labels used on the funding table, named once so lookups cannot drift.
SPONSOR_EQUITY = "Sponsor equity"
ROLLOVER_EQUITY = "Rollover equity"
CASH_FROM_BALANCE_SHEET = "Cash from balance sheet"
EQUITY_PURCHASE_PRICE = "Purchase of equity"
REFINANCE_EXISTING_DEBT = "Repay existing debt"
TRANSACTION_FEES = "Transaction fees"
FINANCING_FEES = "Financing fees"
ORIGINAL_ISSUE_DISCOUNT = "Original issue discount"
CASH_TO_BALANCE_SHEET = "Cash to balance sheet"


@dataclass(frozen=True, slots=True)
class Transaction:
    """A complete purchase: what is bought, how it is funded, what it costs.

    The sponsor's cheque is not an input. It is derived as the residual that
    makes the table balance, which is how it works on a real deal: leverage is
    negotiated with lenders, the price is negotiated with the seller, and equity
    fills whatever gap is left.
    """

    valuation: EntryValuation
    debt: tuple[DebtFunding, ...] = ()
    rollover_equity: Money = ZERO
    cash_from_balance_sheet: Money = ZERO
    cash_to_balance_sheet: Money = ZERO
    transaction_fee_rate: Money = ZERO
    other_uses: tuple[LineItem, ...] = field(default_factory=tuple)

    @classmethod
    def of(
        cls,
        valuation: EntryValuation,
        *,
        debt: tuple[DebtFunding, ...] = (),
        rollover_equity: Numeric = 0,
        cash_from_balance_sheet: Numeric = 0,
        cash_to_balance_sheet: Numeric = 0,
        transaction_fee_rate: Numeric = 0,
        other_uses: tuple[LineItem, ...] = (),
    ) -> Transaction:
        return cls(
            valuation=valuation,
            debt=debt,
            rollover_equity=money(rollover_equity),
            cash_from_balance_sheet=money(cash_from_balance_sheet),
            cash_to_balance_sheet=money(cash_to_balance_sheet),
            transaction_fee_rate=money(transaction_fee_rate),
            other_uses=other_uses,
        )

    def __post_init__(self) -> None:
        if self.rollover_equity < 0:
            raise ValueError("rollover equity must not be negative")
        if self.cash_from_balance_sheet < 0:
            raise ValueError("cash taken from the balance sheet must not be negative")
        if self.cash_to_balance_sheet < 0:
            raise ValueError("cash funded to the balance sheet must not be negative")
        if self.transaction_fee_rate < 0:
            raise ValueError("the transaction fee rate must not be negative")
        if self.cash_from_balance_sheet > self.valuation.existing_cash:
            raise ValueError(
                f"cannot take {self.cash_from_balance_sheet} from the balance sheet; "
                f"the target holds {self.valuation.existing_cash}"
            )
        names = [t.name for t in self.debt]
        if len(names) != len(set(names)):
            raise ValueError("tranche names must be distinct")

    # -- Derived amounts -------------------------------------------------

    @property
    def total_debt(self) -> Money:
        """Face value of debt raised. This is what accrues interest."""
        return sum((t.face for t in self.debt), ZERO)

    @property
    def debt_proceeds(self) -> Money:
        """Cash raised from the debt, after issue discount."""
        return sum((t.proceeds for t in self.debt), ZERO)

    @property
    def original_issue_discount(self) -> Money:
        return sum((t.original_issue_discount for t in self.debt), ZERO)

    @property
    def financing_fees(self) -> Money:
        """Capitalised at close and amortised over the life of the paper."""
        return sum((t.financing_fee for t in self.debt), ZERO)

    @property
    def transaction_fees(self) -> Money:
        """Advisory, legal and diligence costs, charged on enterprise value."""
        return self.valuation.enterprise_value * self.transaction_fee_rate

    @property
    def total_fees(self) -> Money:
        return self.transaction_fees + self.financing_fees

    @property
    def sponsor_equity(self) -> Money:
        """The plug.

        Everything the deal needs, less everything other than the sponsor that
        pays for it. A negative result means the debt raised more than the deal
        required and the excess goes out to the sponsor at close.
        """
        return self._gross_uses - self._non_sponsor_sources

    @property
    def is_overfunded(self) -> bool:
        """Whether the structure returns cash to the sponsor at close.

        Legal and not unheard of, but it means the deal is being financed
        entirely by its own borrowing capacity, so it is worth naming.
        """
        return self.sponsor_equity < 0

    @property
    def _gross_uses(self) -> Money:
        return (
            self.valuation.equity_purchase_price
            + self.valuation.existing_debt
            + self.transaction_fees
            + self.financing_fees
            + self.original_issue_discount
            + self.cash_to_balance_sheet
            + sum((item.amount for item in self.other_uses), ZERO)
        )

    @property
    def _non_sponsor_sources(self) -> Money:
        return self.total_debt + self.rollover_equity + self.cash_from_balance_sheet

    # -- The table -------------------------------------------------------

    def sources_and_uses(self) -> SourcesAndUses:
        """Build the funding table.

        Debt appears on the sources side at face rather than at proceeds, with
        the discount carried across to uses. Both presentations balance, but
        this one keeps face value visible, and face value is what leverage and
        interest are computed on.
        """
        sources: list[LineItem] = [
            LineItem(t.name, t.face, note=_issue_note(t)) for t in self.debt if t.face
        ]
        if self.rollover_equity:
            sources.append(
                LineItem(ROLLOVER_EQUITY, self.rollover_equity, note="no cash moves")
            )
        if self.cash_from_balance_sheet:
            sources.append(LineItem(CASH_FROM_BALANCE_SHEET, self.cash_from_balance_sheet))
        sources.append(
            LineItem(
                SPONSOR_EQUITY,
                self.sponsor_equity,
                note="distribution to the sponsor at close" if self.is_overfunded else "the plug",
            )
        )

        uses: list[LineItem] = [
            LineItem(EQUITY_PURCHASE_PRICE, self.valuation.equity_purchase_price)
        ]
        if self.valuation.existing_debt:
            uses.append(LineItem(REFINANCE_EXISTING_DEBT, self.valuation.existing_debt))
        if self.transaction_fees:
            uses.append(LineItem(TRANSACTION_FEES, self.transaction_fees, note="expensed"))
        if self.financing_fees:
            uses.append(LineItem(FINANCING_FEES, self.financing_fees, note="capitalised"))
        if self.original_issue_discount:
            uses.append(LineItem(ORIGINAL_ISSUE_DISCOUNT, self.original_issue_discount))
        if self.cash_to_balance_sheet:
            uses.append(LineItem(CASH_TO_BALANCE_SHEET, self.cash_to_balance_sheet))
        uses.extend(self.other_uses)

        return SourcesAndUses(sources=tuple(sources), uses=tuple(uses))

    # -- Entry metrics ---------------------------------------------------

    @property
    def entry_leverage(self) -> Money:
        """Total debt as a multiple of LTM EBITDA."""
        return self.total_debt / self.valuation.ltm_ebitda

    @property
    def total_capitalisation(self) -> Money:
        """Debt plus every form of equity that funds the deal."""
        return self.total_debt + self.sponsor_equity + self.rollover_equity

    @property
    def equity_contribution_rate(self) -> Money:
        """Equity as a share of total capitalisation.

        The number a lender looks at first. Sponsors are expected to have real
        money at risk, and a thin contribution is what a credit committee
        objects to before it objects to anything else.
        """
        return safe_div(
            self.sponsor_equity + self.rollover_equity,
            self.total_capitalisation,
            default=ZERO,
        )

    @property
    def sponsor_ownership(self) -> Money:
        """The sponsor's share of the equity, with rollover holders taking the rest."""
        total_equity = self.sponsor_equity + self.rollover_equity
        return safe_div(self.sponsor_equity, total_equity, default=ZERO)


def _issue_note(tranche: DebtFunding) -> str:
    if tranche.issue_price == ONE:
        return ""
    return f"issued at {tranche.issue_price * 100:.2f}"
