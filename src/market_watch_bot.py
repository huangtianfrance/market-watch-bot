import argparse
import os
import smtplib
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from typing import Any, Dict, List, Optional, Tuple

import yaml
import yfinance as yf


@dataclass
class Quote:
    ticker: str
    last: float
    previous_close: float
    daily_pct: float
    five_day_pct: Optional[float]
    volume: Optional[float]
    avg_volume_20d: Optional[float]


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def fetch_quote(ticker: str) -> Quote:
    data = yf.download(ticker, period="2mo", interval="1d", progress=False, auto_adjust=False)
    if data.empty or len(data) < 2:
        raise ValueError(f"No price data returned for {ticker}")

    if isinstance(data.columns, tuple):
        data.columns = [col[0] for col in data.columns]
    elif hasattr(data.columns, "nlevels") and data.columns.nlevels > 1:
        data.columns = data.columns.get_level_values(0)

    close = data["Close"].dropna()
    volume = data["Volume"].dropna() if "Volume" in data.columns else None
    last = float(close.iloc[-1])
    previous_close = float(close.iloc[-2])
    daily_pct = (last / previous_close - 1) * 100

    five_day_pct = None
    if len(close) >= 6:
        five_day_pct = (last / float(close.iloc[-6]) - 1) * 100

    current_volume = None
    avg_volume_20d = None
    if volume is not None and not volume.empty:
        current_volume = float(volume.iloc[-1])
        if len(volume) >= 21:
            avg_volume_20d = float(volume.iloc[-21:-1].mean())

    return Quote(
        ticker=ticker,
        last=last,
        previous_close=previous_close,
        daily_pct=daily_pct,
        five_day_pct=five_day_pct,
        volume=current_volume,
        avg_volume_20d=avg_volume_20d,
    )


def pct_line(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.2f}%"


def volume_ratio(quote: Quote) -> Optional[float]:
    if not quote.volume or not quote.avg_volume_20d:
        return None
    if quote.avg_volume_20d == 0:
        return None
    return quote.volume / quote.avg_volume_20d


def check_stock_rules(stock: Dict[str, Any], quote: Quote, global_rules: Dict[str, Any]) -> List[str]:
    alerts: List[str] = []
    name = stock["name"]

    if quote.daily_pct >= global_rules["daily_up_pct"]:
        alerts.append(f"{name} single-day jump: {quote.daily_pct:+.2f}%")
    if quote.daily_pct <= global_rules["daily_down_pct"]:
        alerts.append(f"{name} single-day selloff: {quote.daily_pct:+.2f}%")

    if quote.five_day_pct is not None and quote.five_day_pct >= global_rules["five_day_up_pct"]:
        alerts.append(f"{name} 5-day strength: {quote.five_day_pct:+.2f}%")
    if quote.five_day_pct is not None and quote.five_day_pct <= global_rules["five_day_down_pct"]:
        alerts.append(f"{name} 5-day weakness: {quote.five_day_pct:+.2f}%")

    ratio = volume_ratio(quote)
    if ratio is not None and ratio >= global_rules["volume_ratio_min"]:
        alerts.append(f"{name} unusual volume: {ratio:.1f}x 20-day average")

    levels = stock.get("levels", {})
    for label, level in levels.items():
        if not isinstance(level, (int, float)):
            continue
        if any(word in label for word in ["trim", "resistance", "confirmation"]) and quote.last >= level:
            alerts.append(f"{name} reached {label}: last {quote.last:.2f} >= {level:.2f}")
        if any(word in label for word in ["support", "risk", "panic"]) and quote.last <= level:
            alerts.append(f"{name} reached {label}: last {quote.last:.2f} <= {level:.2f}")

    return alerts


def check_indicator_rules(indicator: Dict[str, Any], quote: Quote) -> List[str]:
    alerts: List[str] = []
    rules = indicator.get("rules", {})
    name = indicator["name"]

    for level in rules.get("above", []):
        if quote.last >= level:
            alerts.append(f"{name} above {level}: current {quote.last:.2f}")
    for level in rules.get("below", []):
        if quote.last <= level:
            alerts.append(f"{name} below {level}: current {quote.last:.2f}")

    return alerts


def build_report(config: Dict[str, Any]) -> Tuple[str, bool]:
    lines: List[str] = []
    triggered = False
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    lines.append(f"Market Watch Report - {now}")
    lines.append("")

    global_rules = config.get("global_rules", {})

    lines.append("Portfolio")
    lines.append("---------")
    for stock in config.get("stocks", []):
        try:
            quote = fetch_quote(stock["ticker"])
            alerts = check_stock_rules(stock, quote, global_rules)
            triggered = triggered or bool(alerts)
            ratio = volume_ratio(quote)
            ratio_text = "n/a" if ratio is None else f"{ratio:.1f}x"

            lines.append(
                f"{stock['name']} ({stock['ticker']}): "
                f"{quote.last:.2f}, day {pct_line(quote.daily_pct)}, "
                f"5d {pct_line(quote.five_day_pct)}, volume {ratio_text}"
            )
            if stock.get("notes"):
                lines.append(f"  Note: {stock['notes']}")
            for alert in alerts:
                lines.append(f"  ALERT: {alert}")
        except Exception as exc:
            triggered = True
            lines.append(f"{stock.get('name', stock.get('ticker'))}: ERROR {exc}")

    lines.append("")
    lines.append("Market Indicators")
    lines.append("-----------------")
    for indicator in config.get("market_indicators", []):
        try:
            quote = fetch_quote(indicator["ticker"])
            alerts = check_indicator_rules(indicator, quote)
            triggered = triggered or bool(alerts)
            lines.append(
                f"{indicator['name']} ({indicator['ticker']}): "
                f"{quote.last:.2f}, day {pct_line(quote.daily_pct)}, 5d {pct_line(quote.five_day_pct)}"
            )
            if indicator.get("notes"):
                lines.append(f"  Note: {indicator['notes']}")
            for alert in alerts:
                lines.append(f"  ALERT: {alert}")
        except Exception as exc:
            triggered = True
            lines.append(f"{indicator.get('name', indicator.get('ticker'))}: ERROR {exc}")

    return "\n".join(lines), triggered


def send_email(subject: str, body: str) -> None:
    required = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_FROM", "EMAIL_TO"]
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise RuntimeError(f"Missing email environment variables: {', '.join(missing)}")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = os.environ["EMAIL_FROM"]
    message["To"] = os.environ["EMAIL_TO"]
    message.set_content(body)

    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"])) as smtp:
        smtp.starttls()
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(message)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/watchlist.yml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)
    report, triggered = build_report(config)
    print(report)

    send_when_no_alerts = config.get("portfolio", {}).get("send_email_when_no_alerts", False)
    if args.dry_run:
        return
    if triggered or send_when_no_alerts:
        subject = "Market Watch Alert" if triggered else "Market Watch Daily Report"
        send_email(subject, report)


if __name__ == "__main__":
    main()
