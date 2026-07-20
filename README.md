# Market Watch Bot

A lightweight cloud-ready stock alert bot for a low-frequency, panic-buy / strength-sell investing style.

It checks your watchlist on a schedule and emails you when a rule is triggered:

- a held stock rises sharply
- VIX reaches extreme fear or extreme greed / complacency
- Crypto Fear & Greed reaches extreme fear or extreme greed
- a held stock trades near its 1-year, 3-year, or 5-year low
- a possible market-confirmed fundamental re-rating appears, defined as a strong positive price move with unusual volume

The report is intentionally alert-only and bilingual Chinese/English. It does not send routine portfolio summaries unless `send_email_when_no_alerts` is set to `true`.

## Email Style

Alert emails are written as a CEO investment brief, not a machine log. Each alert tries to explain:

- the conclusion first
- what happened
- why it matters
- what the signal means in plain language
- what decision may be worth considering as CEO
- what fundamental red flags should be checked before approving action

Technical terms are explained briefly in the email. For example, volume is described as market participation/attention, and a market-confirmed re-rating is explained as price plus volume showing that real money may be repricing the asset.

The tone is deliberately direct: recommendation, rationale, decision condition, and risk guardrail.

## Holdings vs Watchlist

`position > 0` means the instrument is currently held. The bot can treat it as both:

- a possible funding source when it shows sellable strength
- a possible buy/add target when it enters a low-buy opportunity zone

`position: 0` with `allow_as_target_when_not_held: true` means the instrument is on the watchlist only. It can appear as a possible buy target, but it will not be used as a funding source.

For watchlist-only names, ordinary rallies are ignored. The bot focuses on buy-relevant signals such as being near historical lows, sharp selloffs, or market-confirmed fundamental re-ratings.

Private companies such as SpaceX are marked `disabled: true` because there is no reliable public ticker for automatic Yahoo Finance monitoring.

## Default Alert Rules

The default rules in `config/watchlist.yml` are tuned for low-frequency investing:

| Rule | Default |
|---|---:|
| Single-day big rally | `+5%` |
| 5-day strength | `+10%` |
| Historic low window | `1Y`, `3Y`, `5Y` |
| Near-low tolerance | within `2%` of the low |
| Market-confirmed re-rating | `+8%` day or `+15%` over 5 days, plus `2x` 20-day average volume |
| VIX extreme fear | `>= 30` |
| VIX extreme greed / complacency | `<= 12` |
| Crypto Fear & Greed fear watch | `<= 25` |
| Crypto Fear & Greed extreme fear | `<= 15` |
| Crypto Fear & Greed extreme greed | `>= 80` |

The bot deliberately ignores ordinary selloffs and routine daily movements.

## Portfolio Rotation Engine

The config includes a portfolio-wide rotation engine. It is meant to support low-frequency switching from a stronger holding into a distressed higher-upside holding.

It triggers only when both sides are present:

- one current holding shows sellable strength, based on its own `rotation.sell_strength` rules
- another current holding shows a low-buy opportunity, based on its own `rotation.buy_opportunity` rules

This covers routes such as:

- Airbus → Oracle
- Oracle → Airbus
- Airbus → PDD / Meituan / Microsoft
- Airbus / other holdings → Bitcoin, when BTC is near a low or in a sharp selloff
- any other current holding pair that meets the rules

The alert always includes a manual fundamental guardrail for the target stock. Before acting, check that the target stock has not broken its core thesis.

Bitcoin is configured with `position: 0` and `allow_as_target_when_not_held: true`, so it can appear as a potential target for future rotation without being treated as a funding source.

### Deep-Loss Protection

Some high-volatility or deeply underwater holdings are protected from being used as funding sources before meaningful recovery. In `config/watchlist.yml`, this is controlled by:

```yaml
loss_protection:
  avoid_as_funding_source_below_recovery: true
  recovery_price: 160
```

For example, Meituan can still appear as a possible low-buy opportunity, but it will not be suggested as the stock to sell until it recovers toward the configured recovery price. This reflects the preference to wait for a recovery instead of realizing a severe loss, unless the thesis is manually judged broken.

## Cloud Option: GitHub Actions

1. Create a private GitHub repository.
2. Upload this folder.
3. Add these repository secrets under `Settings -> Secrets and variables -> Actions`:

| Secret | Example |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | your email login |
| `SMTP_PASSWORD` | app password |
| `EMAIL_FROM` | sender email |
| `EMAIL_TO` | receiver email |

4. Edit `config/watchlist.yml` with your own tickers, thresholds, and notes.
5. The workflow runs Monday-Friday at 16:45 Paris time by default, after European markets close and before/around the U.S. session.

You can also run it manually from GitHub Actions using `workflow_dispatch`.

## Local Run

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python src/market_watch_bot.py --config config/watchlist.yml
```

For local email testing, export environment variables first:

```bash
export SMTP_HOST=smtp.gmail.com
export SMTP_PORT=587
export SMTP_USER=you@example.com
export SMTP_PASSWORD='your-app-password'
export EMAIL_FROM=you@example.com
export EMAIL_TO=you@example.com
```

## Ticker Notes

The first config uses Yahoo Finance tickers:

- Samsung Electronics: `005930.KS`
- Airbus: `AIR.PA`
- Oracle: `ORCL`
- Microsoft: `MSFT`
- PDD: `PDD`
- Meituan: `3690.HK`
- Tencent: `0700.HK`
- Bitcoin: `BTC-USD`
- BlackRock Bitcoin ETF: `IBIT`
- JD Hong Kong: `9618.HK`
- NIO: `NIO`
- iQIYI: `IQ`

Major index and FX proxies include `^N225`, `^FTSE`, `^GDAXI`, `^FCHI`, `^HSI`, and `EURCNY=X`.

If your broker holds ADRs instead of Hong Kong shares, replace the HK tickers with U.S. ADR tickers where applicable.

## Not Investment Advice

This bot is only an alerting tool. It does not place trades and does not make investment decisions automatically.
