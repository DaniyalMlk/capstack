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

- [x] Leverage and coverage measured on a trailing twelve months rather than on
      the period. On a quarterly grid the ratios divided a whole debt balance by
      one quarter's earnings and read four times too high, which was not a
      presentational problem but a covenant that breached in every period. The
      cash sweep stepped on the same figure and sat at its top rate for a whole
      hold. A date with fewer than twelve months behind it does not certify
- [x] A delayed-draw or acquisition facility described as a commitment rather
      than as a term loan with nothing drawn. A commitment was only legal on a
      revolver, so a ticking fee could not be charged on undrawn capacity and a
      draw could not be checked against the capacity that exists. A term
      commitment states an availability period, ticks on the face not yet taken
      down, and does not come back when it is repaid
- [x] An annual assumption applied to a sub-annual grid. A growth rate stated
      annually was applied once per period rather than compounded across the
      year, a whole period booked a full year of trading rather than its share
      of one, an assumption series was read by column rather than by year, and
      contractual amortisation written per year was charged per period. The four
      together made a quarterly grid a different and much better business than
      the annual grid built from the same file
- [x] A file that means the same thing on any frequency. Maturities, add-on
      closing periods and first test dates were stated as period numbers, so a
      file moved from an annual grid to a quarterly one had to restate every one
      of them or the paper matured a year and a half early. A date can now be
      stated in years and resolved against the grid, and an assumption series is
      expanded over the years of the case rather than over its columns
- [x] Automated checks on every push. The suite and the type checker are run by
      hand, which is enough for one pair of eyes and not enough for two —
      settled in phase 10, where the workflow landed

## Phase 10 — The work runs somewhere other than one machine

- [x] A parametrised sweep that renders `--help` for the root parser and every
      subcommand, since a help string is expanded only when it is displayed
      and nothing had displayed one
- [x] Commands enumerated by walking the parser, recursively, rather than
      listed by hand
- [x] A completeness check that every argument carries help at all — it passes
      today, and exists so that it keeps passing
- [x] Continuous integration: the suite on 3.11, 3.12 and 3.13, which is the
      range the project claims to support
- [x] `mypy --strict` over source and tests
- [x] The wheel built, installed into a clean environment, and driven through
      a whole deal — the waterfall, the covenant tests, the exit and the memo
      — rather than only imported
- [x] The help sweep run against that installed wheel
- [x] Lint: `ruff check` under the configuration the sibling projects use
      reported 69 findings, led by 21 `zip` calls with no explicit `strict=`
      and 12 long lines. Worth its own pass rather than a drive-by, since one
      of them (`RUF032`, a `Decimal` built from a float literal) might have
      been a defect rather than a style point — cleared in phase 11, where it
      turned out to be the one finding worth keeping and is now exempted at
      the line with the reason.

## Phase 11 — The linter runs, and the backlog behind it is gone

- [x] A `[tool.ruff]` section matching the sibling projects, so a habit learnt
      in one of them is the habit this one wants too
- [x] Seventy-three findings cleared, in commits grouped by what they were
      rather than by which file they were in
- [x] `strict=True` or `itertools.pairwise` on all twenty-one zips: `zip` stops
      at the shorter sequence, so a cash flow vector one element longer than
      its time vector produces an NPV silently missing a flow
- [x] Six exceptions renamed to the family's `Error` suffix, after checking
      that the obvious rename for one of them would have made a class its own
      parent
- [x] `match=` patterns in `pytest.raises` declared as regex or escaped as
      text; four were looser than they read, and one of those is a test about
      an error message naming two rates of return
- [x] The `Decimal(float)` finding exempted rather than fixed, with the reason
      at the line: the binary value of 0.1 is what `money()` exists to avoid
      producing, so the test has to construct it to compare against it
- [x] A superseded annualisation deleted from the revenue build-up, and the
      choice it lost to stated as two tests rather than as a comment —
      including one asserting the two candidates differ on this case, since
      otherwise the first would distinguish nothing
- [x] The lint step added to CI only once the tree was green, because a lint
      step on a red tree is a step somebody turns off

## Phase 12 — Installable, and a front page a reader can check

- [x] An SPDX licence expression with `license-files`, so the terms reach the
      artefact and not only the repository — the `license = {text = "MIT"}`
      table it replaced had no equivalent, and the repository had no licence
      file at all, so every wheel this project could have built claimed MIT
      and shipped none of it
- [x] `py.typed` inside the package, since the annotations were there and
      nothing downstream was allowed to read them: in a checkout a type
      checker reads them off the files and everything looks fine, while a
      consumer who installed the package gets none
- [x] `--version`, read off `__version__` rather than restated, so the two
      cannot disagree
- [x] A distribution name that is not the import name, because `capstack` on
      the index is an unrelated library. It publishes as `capstack-lbo` with
      the import name, the binary and every identifier unchanged, and a
      console-script alias so that invoking the distribution name resolves
      rather than failing
- [x] Metadata tests over the things that break an install rather than a
      calculation: the version agreeing between the attribute, the installed
      distribution and `pyproject.toml` in the checkout — the last because an
      editable install's metadata is a snapshot and the first two agree on a
      tree that has already moved on
- [x] The split name asserted in both directions, since renaming the
      distribution back would fail an upload against a project nobody here
      owns, which is a confusing thing to debug from a build log
- [x] Both artefacts built, passed `twine check --strict`, installed into
      clean environments and driven through a whole memo — two code paths, so
      checking one proves nothing about the other
- [x] A tag-driven release holding `id-token: write` and nothing else, split
      from the build so the job with the publishing identity checks out no
      code and runs none of this project's; the tag checked against the
      declared version first, because an index will not let a version be
      re-uploaded once it has been taken
- [x] A front page that opens with what was checked and against what, with
      the two closed-form rows first: the ambiguous-IRR stream is a quadratic
      whose discriminant is a perfect square, so its roots are exactly 20% and
      30% and the solver — which scans for sign changes and knows nothing
      about quadratics — agreeing to 1.5e-13 is a statement about the solver
- [x] The other three rows asserted as equality rather than as a tolerance,
      because a funding table out by a few thousand on a billion has a missing
      line item and a tolerance would hide exactly the error the table exists
      to catch
- [x] The grid's axes built around the file's own values, so the base cell
      exists by construction: a first draft used 9/10/11 against a deal struck
      at 9.75 and found no base cell to compare
- [x] The figures pinned from the document's side as well, so a deleted row
      fails and not only a moved number, with the whole rendered row checked
      rather than the label and the value separately
- [x] The value direction deliberately not scanned, since looking for a number
      in the document cannot tell which number it found and two figures in a
      table of five can be close enough to pass against the wrong one
- [x] `examples` inside the mypy scope, since a script that decides whether a
      release is honest should be held to the standard of the library it
      measures
- [ ] A first release on the index, which waits on the publisher being
      registered there for this project

## Phase 13 — Tax the business as it is actually financed

- [x] A tax layer taking an interest series rather than a capital structure,
      so the computation is a pure sequential pass over periods and can be
      driven by interest written down by hand instead of by a solver
- [x] Taxable income as EBIT less the interest actually deducted, which is the
      line the operating case cannot produce for itself: it is unlevered on
      purpose, and taxing EBIT is the right thing for it to do
- [x] A cap on the deduction, because a leveraged deal is exactly where such a
      cap binds — the threshold measured rather than restated, and read as
      structure: 3.75 turns of leverage at an 8% blended coupon on an EBITDA
      base, 3.00 on an EBIT base, against sponsor structures struck at five
- [x] Both earnings bases, since the same deal passes on one and binds on the
      other and both are live regimes; the EBIT threshold is the EBITDA one
      scaled by EBIT's share of EBITDA, so it is capital intensity rather than
      leverage that moves it
- [x] Disallowed interest carried in a pool and tested again next period
      alongside next period's interest, rather than lost — and the case where
      the structure never generates the headroom to absorb it, which is the
      usual one
- [x] Interest offered to the cap whether paid in cash or accrued, with the
      regime that waits for the cash deferring the accrued part to the pool
      instead of discarding it
- [x] The cap struck against earnings taken before relief, which fixes its
      order against the loss pool: the cap first, the carried-forward loss
      against what the deduction leaves behind. The reversal is a different
      cash tax and a different closing pool in every period where both bite,
      and the case that distinguishes them is in the suite
- [x] The saving carried as a difference against the same period computed with
      no deduction from the same opening pool. Deduction times rate is right
      only where the period is profitable on both measures and unsheltered; it
      is kept alongside as the naive figure so the two can be compared rather
      than argued about
- [x] Both pool movements asserted as equalities rather than tolerances, since
      the arithmetic is exact and a tolerance would hide the error the identity
      exists to catch
- [x] Zero interest checked against the operating model's own tax line, period
      by period, so the new path is cross-checked against the one already in
      the model rather than only against itself
- [x] The circularity closed: the deduction frees cash, the cash repays debt,
      the smaller balance earns less interest, and the smaller interest earns
      a smaller deduction. A fixed point between two whole-hold passes rather
      than inside the schedule's per-period one, because both pools carry
      forward; it takes a callable that runs a schedule for a cash-flow series,
      so recapitalisations, add-ons and refinancings stay where they already
      work and the solve never learns they exist
- [x] Convergence measured rather than promised: every structure from zero to
      eight turns, at coupons from 4% to 20%, settles in at most six passes,
      and the solve reports the residual it stopped on
- [x] The solve checking its own counterfactual before it starts, since the
      saving credited to the cash flow is a difference against the operating
      case's own tax line and a policy at another rate would measure it off the
      wrong base
- [x] What is returned being one pass rather than an average of several:
      re-running each shipped deal's schedule on the cash flows the solve
      returns reproduces its interest exactly, and breaking that deliberately
      moved it by 2.4e-06
- [x] The two results a reader would not guess. A binding cap damps the
      circularity, because it pins the deduction to earnings and leaves the
      loop no gain: a saturated structure settles in two passes on a residual
      of exactly zero. And past saturation a turn of leverage buys no further
      deduction at all — four structures from six turns to eight deduct the
      same amount and pay the same cash tax to the last place, differing only
      in the debt they leave behind
- [x] The corollary stated rather than buried: the saving is the wrong way to
      price the cap, because deferring a deduction leaves debt outstanding to
      earn more interest that is itself deducted, so a capped run can book a
      larger saving and a lower cash tax while leaving the equity behind more
      debt
- [x] The whole thing reachable from the command line and from a deal file,
      with the cap's rate and its earnings base in the operating block
- [x] Two of the front page's rows carrying it, both gated: the settled solve
      across the shipped deals, and the saturation equality with the spread of
      the debt beside it, so the equality is not vacuous

## Phase 14 — The returns reflect the tax the structure pays

- [x] A case runnable on either tax basis, with the settled solve kept on it
      so a metric or a grid can read the tax as well as the schedule
- [x] The basis opt-in rather than the new default, because flipping it would
      move every reported return in the repository including the worked
      example's hand-computed figures, and which convention a model runs under
      is a modelling decision rather than a defect
- [x] The regression guard parametrised over every shipped deal and asserted
      against `deal.schedule` itself rather than against recorded figures, so
      it stays true as the engine changes
- [x] The saving stated in the unit the question is asked in: 90 to 189 basis
      points of return across the shipped deals, measured by the gate
- [x] The finding behind that figure, which arrived as a failing test. It
      asserted the obvious thing — that the equity gains exactly the cash tax
      the structure saved — and the equity gains more, 1.09 to 1.15 times it.
      The saved cash is swept against the debt, and debt repaid early does not
      accrue the interest it would otherwise have accrued, so the equity
      collects the saving and the interest the saving avoided
- [x] The identity that does hold asserted alongside it: the gain is the debt
      the business no longer owes plus the cash it is holding, since the basis
      moves neither the exit multiple nor the EBITDA it is struck on
- [x] Reachable from the command line, saying which basis it ran on rather
      than quietly running one, with the flag's help carrying what it is worth
- [ ] Whether the levered basis should become the default, which moves every
      reported figure in the repository and is a decision rather than a task

## Phase 15 — The cap as a surface

- [x] A grid runnable on either tax basis, passed down to every cell, since a
      grid is what a deal is actually priced off and the basis moves it by more
      than the width of most of the bands such a table is read for
- [x] The cap on an axis, as a level rather than a shift, so a surface can be
      drawn over leverage and the cap together
- [x] Cells able to report the cash tax and the saving, both guarded the way
      the covenant cushion is already guarded: refused up front with a message
      naming what to do, because on the unlevered basis the saving is zero by
      construction and the grid would be a table of identical excuses
- [x] Saturation visible as a shape rather than inferred from two numbers. At
      a cap tight enough to bind everywhere the saving column is identical from
      four turns to eight — to the last digit, on exact arithmetic — and at a
      loose cap it rises throughout. At the regime's own rate the column rises
      and then flattens, which locates the saturation point
- [x] The second exact relation that falls out of it: a saturated deduction is
      the sum of rate times earnings, so the saving is exactly proportional to
      the cap. Doubling the cap doubles the saving with no remainder
- [x] The flatness checked against its own opposite, since a column that does
      not move might be a grid ignoring its row axis rather than a finding
- [x] Reading the cap off a file that caps nothing refused rather than given an
      invented centre, while flexing a deal *onto* a cap is allowed, because
      what a cap would cost a structure underwritten without one is the
      question most worth asking of it
