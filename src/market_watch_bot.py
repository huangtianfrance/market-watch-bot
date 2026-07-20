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
    close_history: Any


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def fetch_quote(ticker: str) -> Quote:
    data = yf.download(ticker, period="5y", interval="1d", progress=False, auto_adjust=False)
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
        close_history=close,
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


def bilingual(zh: str, en: str) -> str:
    return f"{zh}\n{en}"


def quote_snapshot(quote: Quote) -> str:
    ratio = volume_ratio(quote)
    ratio_text = "n/a" if ratio is None else f"{ratio:.1f}x"
    return (
        f"价格/Price: {quote.last:.2f}; "
        f"日涨跌/Day: {pct_line(quote.daily_pct)}; "
        f"5日/5D: {pct_line(quote.five_day_pct)}; "
        f"成交量/Volume: {ratio_text} 20日均量/20D avg"
    )


def history_window_min(quote: Quote, years: int) -> Optional[float]:
    trading_days = 252 * years
    close = quote.close_history.dropna()
    if len(close) < 30:
        return None
    window = close.iloc[-trading_days - 1 : -1] if len(close) > trading_days else close.iloc[:-1]
    if window.empty:
        return None
    return float(window.min())


def check_stock_rules(stock: Dict[str, Any], quote: Quote, global_rules: Dict[str, Any]) -> List[str]:
    alerts: List[str] = []
    name = stock["name"]

    if quote.daily_pct >= global_rules["stock_big_up_daily_pct"]:
        alerts.append(
            bilingual(
                f"{name} 大涨：单日上涨 {quote.daily_pct:+.2f}%。",
                f"{name} big rally: single-day gain {quote.daily_pct:+.2f}%.",
            )
        )

    if quote.five_day_pct is not None and quote.five_day_pct >= global_rules["stock_big_up_5d_pct"]:
        alerts.append(
            bilingual(
                f"{name} 连续走强：5日上涨 {quote.five_day_pct:+.2f}%。",
                f"{name} sustained strength: 5-day gain {quote.five_day_pct:+.2f}%.",
            )
        )

    ratio = volume_ratio(quote)
    rerating_volume = ratio is not None and ratio >= global_rules["confirmed_rerating_volume_ratio_min"]
    rerating_daily = quote.daily_pct >= global_rules["confirmed_rerating_daily_pct"]
    rerating_5d = quote.five_day_pct is not None and quote.five_day_pct >= global_rules["confirmed_rerating_5d_pct"]
    if rerating_volume and (rerating_daily or rerating_5d):
        alerts.append(
            bilingual(
                f"{name} 可能出现基本面重估并被市场确认：价格大涨且成交量达到 {ratio:.1f} 倍20日均量。请检查财报、指引、订单、监管或管理层消息。",
                f"{name} may be undergoing a market-confirmed fundamental re-rating: strong price move with volume at {ratio:.1f}x the 20-day average. Check earnings, guidance, orders, regulation, or management news.",
            )
        )

    tolerance = 1 + global_rules["historic_low_tolerance_pct"] / 100
    for years in global_rules.get("historic_low_lookback_years", []):
        low = history_window_min(quote, int(years))
        if low is not None and quote.last <= low * tolerance:
            alerts.append(
                bilingual(
                    f"{name} 接近{years}年历史低位：当前 {quote.last:.2f}，{years}年低点约 {low:.2f}。",
                    f"{name} is near a {years}-year low: current {quote.last:.2f}, {years}-year low about {low:.2f}.",
                )
            )

    return alerts


def check_indicator_rules(indicator: Dict[str, Any], quote: Quote) -> List[str]:
    alerts: List[str] = []
    rules = indicator.get("rules", {})
    name = indicator["name"]

    extreme_fear = rules.get("extreme_fear_above")
    if extreme_fear is not None and quote.last >= extreme_fear:
        alerts.append(
            bilingual(
                f"{name} 进入极度恐慌区：当前 {quote.last:.2f}，阈值 {extreme_fear}。",
                f"{name} is in extreme-fear territory: current {quote.last:.2f}, threshold {extreme_fear}.",
            )
        )

    extreme_greed = rules.get("extreme_greed_below")
    if extreme_greed is not None and quote.last <= extreme_greed:
        alerts.append(
            bilingual(
                f"{name} 进入极度贪婪/极度自满区：当前 {quote.last:.2f}，阈值 {extreme_greed}。",
                f"{name} is in extreme-greed / extreme-complacency territory: current {quote.last:.2f}, threshold {extreme_greed}.",
            )
        )

    return alerts


def build_report(config: Dict[str, Any]) -> Tuple[str, bool]:
    lines: List[str] = []
    triggered = False
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    lines.append(f"市场观察提醒 / Market Watch Alerts - {now}")
    lines.append("")

    global_rules = config.get("global_rules", {})

    stock_alerts: List[str] = []
    for stock in config.get("stocks", []):
        try:
            quote = fetch_quote(stock["ticker"])
            alerts = check_stock_rules(stock, quote, global_rules)
            triggered = triggered or bool(alerts)
            for alert in alerts:
                stock_alerts.append(f"{stock['name']} ({stock['ticker']})\n{alert}\n{quote_snapshot(quote)}")
        except Exception as exc:
            triggered = True
            stock_alerts.append(
                bilingual(
                    f"{stock.get('name', stock.get('ticker'))} 数据获取失败：{exc}",
                    f"{stock.get('name', stock.get('ticker'))} data fetch failed: {exc}",
                )
            )

    indicator_alerts: List[str] = []
    for indicator in config.get("market_indicators", []):
        try:
            quote = fetch_quote(indicator["ticker"])
            alerts = check_indicator_rules(indicator, quote)
            triggered = triggered or bool(alerts)
            for alert in alerts:
                indicator_alerts.append(f"{indicator['name']} ({indicator['ticker']})\n{alert}\n{quote_snapshot(quote)}")
        except Exception as exc:
            triggered = True
            indicator_alerts.append(
                bilingual(
                    f"{indicator.get('name', indicator.get('ticker'))} 数据获取失败：{exc}",
                    f"{indicator.get('name', indicator.get('ticker'))} data fetch failed: {exc}",
                )
            )

    if stock_alerts:
        lines.append("持仓股票 / Portfolio Stocks")
        lines.append("--------------------------------")
        lines.append("\n\n".join(stock_alerts))
        lines.append("")

    if indicator_alerts:
        lines.append("市场情绪 / Market Sentiment")
        lines.append("----------------------------")
        lines.append("\n\n".join(indicator_alerts))
        lines.append("")

    if not stock_alerts and not indicator_alerts:
        lines.append("没有触发提醒。")
        lines.append("No alerts were triggered.")

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
        subject = "市场观察提醒 / Market Watch Alert" if triggered else "Market Watch Daily Report"
        send_email(subject, report)


if __name__ == "__main__":
    main()
