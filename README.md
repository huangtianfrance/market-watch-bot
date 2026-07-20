# Market Watch Bot

A lightweight cloud-ready stock alert bot for a low-frequency, panic-buy / strength-sell investing style.

It checks your watchlist on a schedule and emails you when a rule is triggered:

- large daily gain or loss
- 5-day move
- unusual volume versus 20-day average
- price breaks above or below custom levels
- portfolio-specific notes, such as "consider trimming" or "watch for panic-buy setup"

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
