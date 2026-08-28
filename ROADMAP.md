# Roadmap

The engine is built from the bottom up: the arithmetic first, then the deal, then
the schedule that makes an LBO an LBO, and finally the reporting that a deal team
would actually read.

## Phase 1 — Numerics core

- [x] Decimal money type with an explicit rounding policy
- [x] Day-count conventions (ACT/365F, ACT/360, 30/360 US, ACT/ACT ISDA)
- [x] Period grids: annual, semi-annual, quarterly, monthly, with end-of-month handling
- [x] Discounting, NPV, and a dated cash-flow stream
- [x] IRR by bracketed root-finding, XIRR for irregular dates, MoIC and CAGR

## Phase 2 — The transaction

- [x] Entry valuation: enterprise value from an EBITDA multiple, the equity bridge
- [x] Sources and uses that balance exactly, with the sponsor equity as the plug
- [x] Transaction fees, financing fees and original issue discount
- [x] Deal files, so a transaction can be described once and reused
- [x] Opening balance sheet after the recapitalisation

## Phase 3 — Operating model

- [x] Driver series: constants, explicit values, and ramps between two points
- [x] Driver-based revenue and margin build
- [x] Depreciation, amortisation and capital expenditure schedules
- [x] Net working capital as a function of revenue, and the cash effect of its change
- [x] Cash taxes with net-operating-loss carryforward, including the usage cap
- [x] Unlevered free cash flow

## Phase 4 — Capital structure and the debt schedule

- [x] Tranche model: revolver, term loans, notes, mezzanine, seller paper
- [x] Mandatory amortisation, PIK accrual and cash interest
- [x] Cash sweep waterfall by seniority, with pro-rata treatment inside a class
- [x] Resolution of the interest/balance circularity by damped iteration

## Phase 5 — Covenants

- [x] Leverage, interest coverage and fixed-charge coverage tests
- [x] Headroom measurement and breach detection with the period identified
- [x] Sweep percentages stepping with a leverage grid

## Phase 6 — Returns

- [x] Exit valuation and the equity bridge at exit
- [x] IRR and MoIC per security, including preferred returns
- [x] Value-creation attribution: EBITDA growth, multiple change, debt paydown

## Phase 7 — Analysis and reporting

- [x] Two-dimensional sensitivity grids over entry and exit assumptions
- [x] Command line over the whole engine
- [x] Investment committee report
- [x] Break-evens: where the case stops clearing its tests
- [x] Worked examples checked into the test suite

## Phase 8 — The rest of the equity, and the things that happen during a hold

The first seven phases assume a deal is done once, held flat and sold once. Most
are not. The next layer is the events that happen in between, and the parts of
the equity that only exist because somebody has to be paid to run the business.

- [x] Management incentive plan: an option pool, its strike, and the dilution it
      lands on the common at exit
- [x] A ratchet, so the pool's share of the residual steps with the sponsor's
      own return rather than sitting flat
- [x] Dividend recapitalisation mid-hold: a distribution funded by new debt, and
      the effect on the return of getting paid early
- [x] Add-on acquisitions: a purchase during the hold, funded from cash or from
      an incremental facility, and its effect on the blended entry multiple
- [x] Refinancing an existing tranche, including the call premium and the
      unamortised financing fees written off
- [x] A stub period at close, so a deal signing in November is not modelled as
      though it closed on the first day of the year
- [x] Contractual amortisation on face drawn after close. Amortisation is a
      fraction of original face, which is right for paper placed at close and
      leaves a delayed-draw facility repaying nothing however much is drawn on
      it later
- [x] An amortisation schedule for capitalised financing fees, so the balance
      written off at a refinancing is derived rather than stated in the file

## Phase 9 — Sub-annual grids, and the facilities a file cannot yet describe

Everything above is exercised on annual grids. The engine builds quarterly and
monthly ones, and two things stop them being trustworthy on a leveraged deal.

- [ ] Leverage and coverage measured on a trailing twelve months rather than on
      the period. On a quarterly grid the ratios divide a whole debt balance by
      one quarter's earnings and read four times too high, which is not a
      presentational problem but a covenant that breaches in every period
- [ ] A delayed-draw or acquisition facility described as a commitment rather
      than as a term loan with nothing drawn. A commitment is only legal on a
      revolver today, so a ticking fee cannot be charged on undrawn capacity and
      a draw cannot be checked against the capacity that exists
- [ ] Continuous compounding of a driver across a sub-annual grid, so a growth
      rate stated annually is not applied four times a year
- [ ] Automated checks on every push. The suite and the type checker are run by
      hand, which is enough for one pair of eyes and not enough for two
