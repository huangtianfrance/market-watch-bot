# Investment Operating System

## Mandate

The objective is not to predict every daily move or to maximize trade count. The objective is to find mispricings created by the gap between business reality, capital structure, credit conditions, market flow, and prevailing narrative.

The preferred entry is not simply a falling stock. It is:

`fundamentals intact + market overreaction + seller exhaustion or buyer confirmation + identifiable catalyst + defined invalidation`

The system is deliberately conservative about what automation can know. Price, volume, range position, public headlines, and market proxies are screening evidence. They do not prove forward earnings, free cash flow, credit safety, or intrinsic value.

## Portfolio Architecture

| Bucket | Purpose | Typical names | Rule |
|---|---|---|---|
| Core | Compound capital at a lower trading frequency | Tencent, Microsoft, selected AI picks-and-shovels | Sell only when valuation, thesis, or portfolio weight requires it |
| Convexity | Capture a possible 50-100% re-rating with capped portfolio damage | Oracle, Bitcoin, strict-reset AI leaders | 10-15% of portfolio in aggregate; staged entries only |
| Swing | Monetize sentiment and valuation swings in durable cyclicals | Airbus, Groupe ADP | Buy deep reset, trim strength; do not force monthly trades |
| Reserve | Preserve optionality for genuine panic | Cash | Cash is not a failed investment; it is the right to act when others cannot |

The portfolio is not built around the most exciting story. Position size is a function of expected return, probability, downside, correlation, and financing sensitivity.

## Capital-Cycle Lens

AI is not one trade. It has distinct economics:

| Type | Examples | What creates value | What can break it |
|---|---|---|---|
| Cash compounder | MSFT, META, GOOGL, Tencent | AI improves an existing cash engine | CapEx rises without monetization or margin return |
| Picks-and-shovels | AVGO, NVDA, ASML, TSMC, Eaton, Vertiv | Customer AI spending becomes their orders and margins | Customer CapEx slows, inventory builds, supply catches up, valuation crowding unwinds |
| Capital-intensive AI | Oracle | AI revenue converts into FCF faster than CapEx, interest, and dilution rise | Financing, debt, FCF, contract credibility, or incremental ROIC deteriorates |

For all AI assets, revenue growth alone is insufficient. The important question is whether incremental capital earns an attractive return. A company that grows revenue by 50% while CapEx, debt, and dilution grow faster may be destroying per-share value.

## Research Profiles

The watchlist is organized by economic mechanism, rather than by ticker alone.

| Profile | Watchlist coverage | Primary question |
|---|---|---|
| AI cash compounder | MSFT, META, GOOGL, AAPL, AMZN, SAP, Tencent | Does AI improve FCF and ROIC of the existing platform? |
| AI picks-and-shovels | AVGO, NVDA, ASML, TSMC, Samsung, VRT, Eaton, Legrand, Schneider | Are orders, customer CapEx, margins, and utilization still supporting the cycle? |
| AI capital cycle | Oracle | Does OCI/RPO growth outrun CapEx, funding cost, and dilution? |
| Healthcare repair | Novo Nordisk, UnitedHealth, Thermo Fisher, Medtronic | Is the discount an overreaction to policy/product worries, or is the earnings engine genuinely impaired? |
| China platform re-rating | Tencent, Alibaba, PDD, JD, Meituan, Xiaomi | Is the discount policy/flow-driven, or are earnings and competition deteriorating? |
| Defense and aerospace | Airbus, Safran, Leonardo, BAE, Rheinmetall, Thales, Rolls-Royce, Lockheed, GE | Can backlog become delivery and FCF before the security premium becomes overcrowded? |
| Cyclical repair | LVMH, ADP, Inditex, Veolia, Michelin, Siemens, Caterpillar | Has the second derivative turned: from worsening to stabilizing or improving? |
| Financial credit cycle | BNP, Societe Generale, ING, UBS, BAC | Is low valuation a risk premium or a peak-earnings warning? |
| Energy/inflation | TotalEnergies, Exxon | Is FCF resilient at a mid-cycle oil price and is capital discipline intact? |
| Crypto liquidity | Bitcoin | Are liquidity, ETF flows, miner stress, and leverage creating a favorable asymmetry? |
| Speculative optionality | Tesla, Pony AI, NIO, iQIYI | Is there a real commercialization path and sufficient funding runway? |

## Eight-Gate Decision Protocol

Every material purchase, add, trim, or rotation must answer the following.

1. Fundamentals: What must remain true about revenue, EPS, FCF, margins, orders, and share?
2. Capital structure: Is growth creating per-share value after CapEx, financing, and dilution?
3. Flow: Is the market still liquidating, or is supply drying up and demand returning?
4. Credit: What do rates, refinancing, spreads, ratings, and funding conditions imply?
5. Valuation: Is the price wrong, or are future earnings lower than the trailing data suggests?
6. Catalyst: What measurable event can make the market revise its view in 6-18 months?
7. Invalidation: What fact would show the original thesis was wrong?
8. Position: What is the maximum acceptable loss and the appropriate bucket size?

If these cannot be stated clearly, the default decision is no trade.

## Price-Volume Protocol

The bot treats price-volume as evidence of flow, not as a substitute for business analysis.

| Pattern | Interpretation | Default action |
|---|---|---|
| First sharp decline with heavy volume | Active sellers still control the tape | Do not catch the first leg down |
| Second retest on lower volume without a lower low | Marginal sellers may be exhausted | Start research; no automatic full entry |
| Bad news without a new low | The market may have already discounted the news | Look for a catalyst and buyer confirmation |
| Strong up day with 1.5x+ normal volume | Buyers may be validating a re-rating | Verify the fundamental trigger; do not blindly chase |
| Strong stock near overhead supply | Break-even selling can cap the move | Consider trim/review instead of adding |

## Macro and Credit Filter

Macro is an environment filter, not a forecasting game.

- Persistent high rates favor cash flow, low leverage, high ROIC, and suppliers to AI capex. They raise the required return for long-duration and financing-dependent stories.
- HYG relative to LQD is used as an early market proxy for credit stress. It is not a company credit rating.
- QQQ and SMH are risk-appetite proxies for AI. They cannot replace the actual checks: hyperscaler CapEx, orders, utilization, GPU rental economics, and financing terms.
- Brent, VIX, the U.S. 10Y, Hang Seng, and EUR/CNY are used to rank active geopolitical and China-risk themes. At most three themes should be active at one time.

## Market Rotation Scanner

The scanner is designed for early research, not for chasing a sector that has already moved.

1. It measures the 20-trading-day return of a theme proxy against SPY.
2. It checks a second proxy so that a single ETF move does not define a new regime.
3. It searches the track's configured candidates for names below the upper part of their one-year range and without a short-term price breakdown.
4. It labels the result as confirmed flow, early rotation, or not confirmed.
5. It requires the track-specific fundamental checklist before an entry is considered.

The scanner can surface an early research candidate before a headline becomes consensus. It cannot identify an intrinsic bargain from price data alone. In particular, a candidate below its one-year high may be correctly discounted because earnings, regulation, competition, or financing have deteriorated.

## Automation Boundaries

The bot may issue a research or review signal. It never issues an automatic order.

Before acting on any signal, verify the most recent earnings release, guidance, official company filings, material financing/rating changes, and the specific invalidation checklist. A lack of negative headlines means only that the automated public search did not find a direct match.

## Discipline

Do not average down because a position is painful. Average only when the eight gates improve or remain intact and the market is offering a better risk/reward entry.

Do not sell a deeply underwater holding merely to relieve emotion. Sell it when the thesis is invalidated, funding risk makes recovery unlikely, or a clearly superior use of capital is supported by the same decision protocol.

Do not chase a leader because it is strong. Buy strength only when the business evidence, valuation, and position size still support it.
