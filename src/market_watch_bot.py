import argparse
import json
import os
import smtplib
import urllib.request
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


@dataclass
class SentimentIndex:
    name: str
    value: int
    classification: str
    timestamp: str


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


def fetch_crypto_fear_greed() -> SentimentIndex:
    url = "https://api.alternative.me/fng/?limit=1"
    with urllib.request.urlopen(url, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    item = payload["data"][0]
    return SentimentIndex(
        name="Crypto Fear & Greed",
        value=int(item["value"]),
        classification=item["value_classification"],
        timestamp=item.get("timestamp", ""),
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
    ratio_text = "n/a" if ratio is None else f"{ratio:.1f}倍"
    return (
        f"当前价格/Price: {quote.last:.2f}\n"
        f"今天涨跌/Today: {pct_line(quote.daily_pct)}\n"
        f"最近5个交易日/Last 5 trading days: {pct_line(quote.five_day_pct)}\n"
        f"成交量/Volume: 约为20日平均成交量的 {ratio_text}\n"
        f"术语解释/Plain English: 成交量可以理解为市场参与热度。价格变化配合放量，通常比单纯涨跌更值得重视。"
    )


def explain_low_signal() -> str:
    return (
        "这不是直接买入指令，而是进入投研优先区：价格已经接近过去一段时间市场给过的低估/恐慌区。\n"
        "CEO 决策点：如果基本面没有破坏，可以进入分批建仓评估；如果基本面已经坏了，则视为价值陷阱。"
    )


def explain_rerating_signal() -> str:
    return (
        "“市场确认的重估”意思是：不只是新闻好听，而是价格明显上涨、成交量也明显放大，说明有真实资金在重新定价。\n"
        "CEO 决策点：这是提高研究优先级的信号，不是追高指令；需要确认基本面变化是否真实可持续。"
    )


def explain_rotation_signal() -> str:
    return (
        "这是一个调仓候选，不是自动交易指令。\n"
        "投资逻辑：把一只已经走强、适合释放部分资金的持仓，和另一只进入低位机会的标的配对，评估是否能提高未来3-12个月的收益/风险比。"
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
    is_held = stock.get("position", 0) > 0

    if is_held and quote.daily_pct >= global_rules["stock_big_up_daily_pct"]:
        alerts.append(
            bilingual(
                (
                    f"{name} 今天涨得比较猛，单日上涨 {quote.daily_pct:+.2f}%。\n"
                    f"投资含义：这更接近你的“上涨时考虑卖一点”纪律，而不是追高信号。\n"
                    f"建议动作：检查是否进入减仓、锁定利润或换入低位标的的窗口。"
                ),
                (
                    f"{name} had a strong rally today, up {quote.daily_pct:+.2f}%.\n"
                    f"This fits your strength-selling discipline. Consider whether this is a trim/rotation zone rather than a chase signal."
                ),
            )
        )

    if is_held and quote.five_day_pct is not None and quote.five_day_pct >= global_rules["stock_big_up_5d_pct"]:
        alerts.append(
            bilingual(
                (
                    f"{name} 最近几天连续走强，5个交易日涨了 {quote.five_day_pct:+.2f}%。\n"
                    f"投资含义：这不是一天的随机波动，可能已有资金连续推升。\n"
                    f"建议动作：评估是否把部分利润轮动到更低位、更有弹性的机会里。"
                ),
                (
                    f"{name} has shown sustained strength, up {quote.five_day_pct:+.2f}% over 5 trading days.\n"
                    f"This is less likely to be a one-day blip. Consider whether part of the gain should be rotated into a cheaper opportunity."
                ),
            )
        )

    ratio = volume_ratio(quote)
    rerating_volume = ratio is not None and ratio >= global_rules["confirmed_rerating_volume_ratio_min"]
    rerating_daily = quote.daily_pct >= global_rules["confirmed_rerating_daily_pct"]
    rerating_5d = quote.five_day_pct is not None and quote.five_day_pct >= global_rules["confirmed_rerating_5d_pct"]
    if rerating_volume and (rerating_daily or rerating_5d):
        alerts.append(
            bilingual(
                (
                    f"{name} 可能出现“市场确认的重估”。简单说：价格涨得明显，成交量也放大到20日均量的 {ratio:.1f} 倍。\n"
                    f"{explain_rerating_signal()}\n"
                    f"需确认事项：是否有财报、业绩指引、订单、监管变化或管理层表态支撑。"
                ),
                (
                    f"{name} may be seeing a market-confirmed re-rating: price moved strongly and volume reached {ratio:.1f}x the 20-day average.\n"
                    f"In plain English, this means real money may be repricing the stock, not just reacting to a headline.\n"
                    f"Next step: check earnings, guidance, orders, regulation, or management commentary."
                ),
            )
        )

    tolerance = 1 + global_rules["historic_low_tolerance_pct"] / 100
    for years in global_rules.get("historic_low_lookback_years", []):
        low = history_window_min(quote, int(years))
        if low is not None and quote.last <= low * tolerance:
            alerts.append(
                bilingual(
                    (
                        f"{name} 已经接近 {years} 年低位。当前价格 {quote.last:.2f}，{years} 年低点大约 {low:.2f}。\n"
                        f"{explain_low_signal()}\n"
                        f"建议动作：加入重点研究清单；如果基本面没破，优先考虑小仓分批，而不是一次性重仓。"
                    ),
                    (
                        f"{name} is near a {years}-year low. Current price is {quote.last:.2f}; the {years}-year low is about {low:.2f}.\n"
                        f"This is a research signal, not an automatic buy. If the thesis is intact, consider staged entry rather than one large trade."
                    ),
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
                (
                    f"{name} 进入极度恐慌区，当前 {quote.last:.2f}，触发线是 {extreme_fear}。\n"
                    f"简单说：市场开始愿意花更多钱买保护，说明大家明显害怕下跌。\n"
                    f"CEO 决策点：这更接近你的恐慌买入环境，但仍要优先挑基本面没坏、只是被一起杀下来的标的。"
                ),
                (
                    f"{name} entered extreme-fear territory: current {quote.last:.2f}, threshold {extreme_fear}.\n"
                    f"In plain English, investors are paying more for downside protection. This can fit your panic-buy setup, but only for stocks whose fundamentals remain intact."
                ),
            )
        )

    extreme_greed = rules.get("extreme_greed_below")
    if extreme_greed is not None and quote.last <= extreme_greed:
        alerts.append(
            bilingual(
                (
                    f"{name} 进入极度贪婪/自满区，当前 {quote.last:.2f}，触发线是 {extreme_greed}。\n"
                    f"简单说：市场太放松，大家不怎么害怕风险。\n"
                    f"CEO 决策点：这通常不是追高的好环境，更适合检查哪些持仓涨多了、是否要卖一点。"
                ),
                (
                    f"{name} entered extreme-greed / complacency territory: current {quote.last:.2f}, threshold {extreme_greed}.\n"
                    f"In plain English, the market is very relaxed about risk. This is usually a better time to review trims than to chase."
                ),
            )
        )

    return alerts


def check_sentiment_index_rules(indicator: Dict[str, Any], sentiment: SentimentIndex) -> List[str]:
    alerts: List[str] = []
    rules = indicator.get("rules", {})
    name = indicator["name"]

    extreme_fear = rules.get("extreme_fear_below")
    if extreme_fear is not None and sentiment.value <= extreme_fear:
        alerts.append(
            bilingual(
                (
                    f"{name} 进入极度恐慌区：当前 {sentiment.value} ({sentiment.classification})，触发线 <= {extreme_fear}。\n"
                    f"简单说：加密市场情绪很差，很多人在逃离风险。\n"
                    f"CEO 决策点：如果 BTC ETF 没有持续流出、监管和网络安全没有新雷，可以开始认真研究小仓分批。"
                ),
                (
                    f"{name} entered extreme-fear territory: current {sentiment.value} ({sentiment.classification}), threshold <= {extreme_fear}.\n"
                    f"In plain English, crypto sentiment is very weak. If ETF flows, regulation, and network security remain acceptable, this can be a staged-entry research signal."
                ),
            )
        )

    fear_watch = rules.get("fear_watch_below")
    if fear_watch is not None and sentiment.value <= fear_watch:
        alerts.append(
            bilingual(
                (
                    f"{name} 进入恐慌观察区：当前 {sentiment.value} ({sentiment.classification})，触发线 <= {fear_watch}。\n"
                    f"这还不一定是极端底部，但已经值得把 BTC 放到重点观察列表。\n"
                    f"建议动作：先不急买，等价格也接近低位，或出现恐慌后不再创新低。"
                ),
                (
                    f"{name} entered fear-watch territory: current {sentiment.value} ({sentiment.classification}), threshold <= {fear_watch}.\n"
                    f"This is not necessarily a bottom, but BTC deserves closer attention. Wait for price confirmation or stabilization."
                ),
            )
        )

    extreme_greed = rules.get("extreme_greed_above")
    if extreme_greed is not None and sentiment.value >= extreme_greed:
        alerts.append(
            bilingual(
                (
                    f"{name} 进入极度贪婪区：当前 {sentiment.value} ({sentiment.classification})，触发线 >= {extreme_greed}。\n"
                    f"简单说：加密市场情绪太热，追涨风险变高。\n"
                    f"CEO 决策点：如果还没买 BTC，通常更适合等待；如果已经持有，才考虑是否卖一点。"
                ),
                (
                    f"{name} entered extreme-greed territory: current {sentiment.value} ({sentiment.classification}), threshold >= {extreme_greed}.\n"
                    f"In plain English, crypto sentiment is hot. If you do not own BTC yet, patience may be better than chasing."
                ),
            )
        )

    return alerts


def sentiment_snapshot(sentiment: SentimentIndex) -> str:
    return f"数值/Value: {sentiment.value}; 状态/Classification: {sentiment.classification}"


def stock_by_name(config: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    for stock in config.get("stocks", []):
        if stock.get("name") == name:
            return stock
    return None


def get_quote(ticker: str, cache: Dict[str, Quote]) -> Quote:
    if ticker not in cache:
        cache[ticker] = fetch_quote(ticker)
    return cache[ticker]


def sell_strength_reasons(stock: Dict[str, Any], quote: Quote, global_rules: Dict[str, Any]) -> List[str]:
    rotation = stock.get("rotation", {})
    rules = rotation.get("sell_strength", {})
    reasons: List[str] = []
    loss_protection = rotation.get("loss_protection", {})

    if loss_protection.get("avoid_as_funding_source_below_recovery"):
        recovery_price = loss_protection.get("recovery_price")
        if recovery_price is not None and quote.last < recovery_price:
            return []

    min_price = rules.get("min_price")
    if min_price is not None and quote.last >= min_price:
        reasons.append(f"价格达到可卖区 {quote.last:.2f} >= {min_price:.2f} / price reached sellable zone")

    min_daily = rules.get("min_daily_pct", global_rules.get("rotation_sell_daily_pct"))
    if min_daily is not None and quote.daily_pct >= min_daily:
        reasons.append(f"单日强势 {quote.daily_pct:+.2f}% >= {min_daily:+.2f}% / strong daily move")

    min_5d = rules.get("min_5d_pct", global_rules.get("rotation_sell_5d_pct"))
    if min_5d is not None and quote.five_day_pct is not None and quote.five_day_pct >= min_5d:
        reasons.append(f"5日强势 {quote.five_day_pct:+.2f}% >= {min_5d:+.2f}% / strong 5-day move")

    return reasons


def buy_opportunity_reasons(stock: Dict[str, Any], quote: Quote, global_rules: Dict[str, Any]) -> List[str]:
    rotation = stock.get("rotation", {})
    rules = rotation.get("buy_opportunity", {})
    reasons: List[str] = []

    max_price = rules.get("max_price")
    if max_price is not None and quote.last <= max_price:
        reasons.append(f"价格进入低吸区 {quote.last:.2f} <= {max_price:.2f} / price entered buy zone")

    max_5d = rules.get("max_5d_pct", global_rules.get("rotation_buy_5d_drop_pct"))
    if max_5d is not None and quote.five_day_pct is not None and quote.five_day_pct <= max_5d:
        reasons.append(f"5日大跌 {quote.five_day_pct:+.2f}% <= {max_5d:+.2f}% / sharp 5-day selloff")

    near_low_years = rules.get("near_low_years", global_rules.get("rotation_buy_near_low_years"))
    if near_low_years:
        low = history_window_min(quote, int(near_low_years))
        tolerance_pct = rules.get("near_low_tolerance_pct", global_rules.get("rotation_buy_near_low_tolerance_pct", 0))
        tolerance = 1 + tolerance_pct / 100
        if low is not None and quote.last <= low * tolerance:
            reasons.append(
                f"接近{near_low_years}年低位：当前 {quote.last:.2f}，低点约 {low:.2f} / near {near_low_years}Y low"
            )

    return reasons


def guardrail_text(stock: Dict[str, Any]) -> str:
    red_flags = stock.get("rotation", {}).get("fundamental_guardrail", {}).get("red_flags", [])
    if not red_flags:
        return "- No stock-specific red flags configured."
    return "\n".join(f"- {flag}" for flag in red_flags)


def plain_reason_list(reasons: List[str]) -> str:
    return "\n".join(f"- {reason}" for reason in reasons)


def check_rotation_engine(config: Dict[str, Any], quote_cache: Dict[str, Quote]) -> List[str]:
    engine = config.get("rotation_engine", {})
    if not engine.get("enabled", False):
        return []

    global_rules = config.get("global_rules", {})
    all_stocks = [stock for stock in config.get("stocks", []) if not stock.get("disabled")]
    sell_stocks = [stock for stock in all_stocks if stock.get("position", 0) > 0]
    buy_stocks = [
        stock
        for stock in all_stocks
        if stock.get("position", 0) > 0 or stock.get("rotation", {}).get("allow_as_target_when_not_held")
    ]
    sell_candidates: List[Tuple[Dict[str, Any], Quote, List[str]]] = []
    buy_candidates: List[Tuple[Dict[str, Any], Quote, List[str]]] = []

    for stock in sell_stocks:
        quote = get_quote(stock["ticker"], quote_cache)
        sell_reasons = sell_strength_reasons(stock, quote, global_rules)
        if sell_reasons:
            sell_candidates.append((stock, quote, sell_reasons))

    for stock in buy_stocks:
        quote = get_quote(stock["ticker"], quote_cache)
        buy_reasons = buy_opportunity_reasons(stock, quote, global_rules)
        if buy_reasons:
            buy_candidates.append((stock, quote, buy_reasons))

    pairs: List[str] = []
    max_pairs = int(engine.get("max_pairs_per_email", 5))
    for from_stock, from_quote, from_reasons in sell_candidates:
        for to_stock, to_quote, to_reasons in buy_candidates:
            if from_stock["ticker"] == to_stock["ticker"]:
                continue
            if len(pairs) >= max_pairs:
                break
            action = engine.get(
                "action_template",
                "Review trimming a small tranche from the strength candidate and moving it into the opportunity candidate.",
            )
            pairs.append(
                bilingual(
                    (
                        f"结论：出现一个需要 CEO 关注的组合轮动候选：{from_stock['name']} → {to_stock['name']}。\n\n"
                        f"{explain_rotation_signal()}\n\n"
                        f"为什么可能卖一点 {from_stock['name']}：\n{plain_reason_list(from_reasons)}\n\n"
                        f"为什么可能研究 {to_stock['name']}：\n{plain_reason_list(to_reasons)}\n\n"
                        f"建议动作：先进入人工复核，不建议直接全仓切换。如果确认逻辑成立，优先考虑小比例试探或分批轮动。\n\n"
                        f"投前条件：确认 {to_stock['name']} 不是基本面坏了。请先排除这些红旗：\n{guardrail_text(to_stock)}"
                    ),
                    (
                        f"Portfolio rotation watch: {from_stock['name']} → {to_stock['name']}.\n\n"
                        f"This is not an automatic trade. It means one holding looks strong enough to consider trimming, while another name looks cheap or washed out enough to research.\n\n"
                        f"Why {from_stock['name']} may be a trim candidate:\n{plain_reason_list(from_reasons)}\n\n"
                        f"Why {to_stock['name']} may be a buy candidate:\n{plain_reason_list(to_reasons)}\n\n"
                        f"Possible decision: research first, then consider a small staged rotation only if the thesis is intact.\n\n"
                        f"Before acting, check these red flags for {to_stock['name']}:\n{guardrail_text(to_stock)}"
                    ),
                )
                + f"\n\n{from_stock['name']} ({from_stock['ticker']})\n{quote_snapshot(from_quote)}"
                + f"\n\n{to_stock['name']} ({to_stock['ticker']})\n{quote_snapshot(to_quote)}"
            )
        if len(pairs) >= max_pairs:
            break

    return pairs


def check_rotation_signal(signal: Dict[str, Any]) -> List[str]:
    alerts: List[str] = []
    from_quote = fetch_quote(signal["from_ticker"])
    to_quote = fetch_quote(signal["to_ticker"])

    from_rules = signal.get("from_strength", {})
    to_rules = signal.get("to_opportunity", {})
    guardrail = signal.get("fundamental_guardrail", {})

    from_price_ok = from_quote.last >= from_rules.get("min_price", float("inf"))
    from_day_ok = from_quote.daily_pct >= from_rules.get("min_daily_pct", float("inf"))
    from_5d_ok = from_quote.five_day_pct is not None and from_quote.five_day_pct >= from_rules.get("min_5d_pct", float("inf"))
    from_strength_ok = from_price_ok or from_day_ok or from_5d_ok

    to_price_ok = to_quote.last <= to_rules.get("max_price", 0)
    to_5d_ok = to_quote.five_day_pct is not None and to_quote.five_day_pct <= to_rules.get("max_5d_pct", -float("inf"))
    to_low_ok = False
    low_years = to_rules.get("near_low_years")
    if low_years:
        low = history_window_min(to_quote, int(low_years))
        tolerance = 1 + to_rules.get("near_low_tolerance_pct", 0) / 100
        to_low_ok = low is not None and to_quote.last <= low * tolerance

    to_opportunity_ok = to_price_ok or to_5d_ok or to_low_ok

    if not (from_strength_ok and to_opportunity_ok):
        return alerts

    red_flags = guardrail.get("red_flags", [])
    red_flag_text = "\n".join(f"- {flag}" for flag in red_flags)
    alerts.append(
        bilingual(
            (
                f"触发调仓观察：{signal['from_name']} → {signal['to_name']}。\n"
                f"建议动作：{signal.get('action', 'Review this rotation manually.')}\n"
                f"{signal['from_name']} 出现可卖强势；{signal['to_name']} 接近低位/大跌机会。\n"
                f"执行前必须人工确认 {signal['to_name']} 基本面支撑仍在，尤其排除以下红旗：\n{red_flag_text}"
            ),
            (
                f"Rotation watch triggered: {signal['from_name']} → {signal['to_name']}.\n"
                f"Suggested action: {signal.get('action', 'Review this rotation manually.')}\n"
                f"{signal['from_name']} shows sellable strength; {signal['to_name']} is near a low / selloff opportunity.\n"
                f"Before acting, manually confirm {signal['to_name']}'s fundamental support is still intact, especially excluding these red flags:\n{red_flag_text}"
            ),
        )
    )
    alerts.append(f"{signal['from_name']} ({signal['from_ticker']})\n{quote_snapshot(from_quote)}")
    alerts.append(f"{signal['to_name']} ({signal['to_ticker']})\n{quote_snapshot(to_quote)}")
    return alerts


def build_report(config: Dict[str, Any]) -> Tuple[str, bool]:
    lines: List[str] = []
    triggered = False
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    lines.append(f"CEO 投资简报 / CEO Investment Brief - {now}")
    lines.append("")
    lines.append(
        "汇报口径：本邮件只在出现值得 CEO 关注的投资信号时发送。"
        "监控维度包括价格位置、近期涨跌、成交量、历史低位、VIX/加密恐惧贪婪指数，以及组合内可轮动配对。"
    )
    lines.append(
        "Briefing standard: this email is sent only when a CEO-level investment signal appears. "
        "The system checks price level, recent moves, volume, historical lows, VIX/Crypto Fear & Greed, and possible portfolio rotation pairs."
    )
    lines.append("")

    global_rules = config.get("global_rules", {})
    quote_cache: Dict[str, Quote] = {}

    rotation_alerts: List[str] = []
    try:
        engine_alerts = check_rotation_engine(config, quote_cache)
        triggered = triggered or bool(engine_alerts)
        rotation_alerts.extend(engine_alerts)
    except Exception as exc:
        triggered = True
        rotation_alerts.append(
            bilingual(
                f"组合轮动引擎数据获取失败：{exc}",
                f"Portfolio rotation engine data fetch failed: {exc}",
            )
        )

    for signal in config.get("rotation_signals", []):
        try:
            alerts = check_rotation_signal(signal)
            triggered = triggered or bool(alerts)
            if alerts:
                rotation_alerts.append("\n\n".join(alerts))
        except Exception as exc:
            triggered = True
            rotation_alerts.append(
                bilingual(
                    f"{signal.get('name', 'Rotation signal')} 数据获取失败：{exc}",
                    f"{signal.get('name', 'Rotation signal')} data fetch failed: {exc}",
                )
            )

    stock_alerts: List[str] = []
    for stock in [item for item in config.get("stocks", []) if not item.get("disabled")]:
        try:
            quote = get_quote(stock["ticker"], quote_cache)
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
            if indicator.get("type") == "crypto_fear_greed":
                sentiment = fetch_crypto_fear_greed()
                alerts = check_sentiment_index_rules(indicator, sentiment)
                triggered = triggered or bool(alerts)
                for alert in alerts:
                    indicator_alerts.append(f"{indicator['name']} ({indicator['ticker']})\n{alert}\n{sentiment_snapshot(sentiment)}")
            else:
                quote = get_quote(indicator["ticker"], quote_cache)
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

    if rotation_alerts:
        lines.append("一、可能的调仓决策 / Potential Rotation Decisions")
        lines.append("----------------------------")
        lines.append("\n\n".join(rotation_alerts))
        lines.append("")

    if stock_alerts:
        lines.append("二、个股机会或风险提示 / Stock-Level Signals")
        lines.append("--------------------------------")
        lines.append("\n\n".join(stock_alerts))
        lines.append("")

    if indicator_alerts:
        lines.append("三、市场情绪背景 / Market Sentiment Context")
        lines.append("----------------------------")
        lines.append("\n\n".join(indicator_alerts))
        lines.append("")

    if not rotation_alerts and not stock_alerts and not indicator_alerts:
        lines.append("结论：今天没有达到 CEO 决策级别的信号。建议继续观察，不做动作。")
        lines.append("Conclusion: no CEO-level decision signal was triggered today. Recommendation: stay patient and take no action.")

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
        subject = "CEO投资简报 / CEO Investment Brief" if triggered else "Market Watch Daily Report"
        send_email(subject, report)


if __name__ == "__main__":
    main()
