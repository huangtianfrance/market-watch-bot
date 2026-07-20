# Market Watch Bot

A lightweight cloud-ready stock alert bot for a low-frequency, panic-buy / strength-sell investing style.

It checks your watchlist on a schedule and emails you when a rule is triggered:

- a held stock rises sharply
- VIX reaches extreme fear or extreme greed / complacency
- a held stock trades near its 1-year, 3-year, or 5-year low
- a possible market-confirmed fundamental re-rating appears, defined as a strong positive price move with unusual volume

The report is intentionally alert-only and bilingual Chinese/English. It does not send routine portfolio summaries unless `send_email_when_no_alerts` is set to `true`.

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

The bot deliberately ignores ordinary selloffs and routine daily movements.

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

- Airbus: `AIR.PA`
- Oracle: `ORCL`
- Microsoft: `MSFT`
- PDD: `PDD`
- Meituan: `3690.HK`
- Tencent: `0700.HK`
- JD Hong Kong: `9618.HK`
- NIO: `NIO`
- iQIYI: `IQ`

If your broker holds ADRs instead of Hong Kong shares, replace the HK tickers with U.S. ADR tickers where applicable.

## Not Investment Advice

This bot is only an alerting tool. It does not place trades and does not make investment decisions automatically.
