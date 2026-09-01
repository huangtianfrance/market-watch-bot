import argparse
import json
import os
import smtplib
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any, Dict, List, Optional, Set, Tuple

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
    quality: "DataQuality"


@dataclass
class SentimentIndex:
    name: str
    value: int
    classification: str
    timestamp: str
    quality: "DataQuality"


@dataclass
class DataQuality:
    source: str
    symbol: str
    status: str
    rows: int = 0
    latest_date: str = "n/a"
    freshness_days: Optional[int] = None
    attempts: int = 1
    warnings: Optional[List[str]] = None
    error: Optional[str] = None


DATA_QUALITY_LOG: List[DataQuality] = []
ROTATION_TARGET_TICKERS: Set[str] = set()
NEWS_CHECK_CACHE: Dict[str, str] = {}
INFLUENCER_CHECK_CACHE: Dict[str, str] = {}
ROTATION_PAIR_CONTEXT: List[Tuple[str, str, str, str]] = []
FETCH_SETTINGS: Dict[str, Any] = {
    "request_pause_seconds": 0.7,
    "max_retries": 3,
    "retry_backoff_seconds": 2.0,
    "stale_after_calendar_days": 7,
    "min_history_rows": 60,
    "include_quality_report": True,
    "include_successful_fetches": True,
}
NEWS_SEARCH_SETTINGS: Dict[str, Any] = {
    "enabled": False,
    "provider": "GDELT DOC API",
    "lookback": "14d",
    "max_red_flags_per_stock": 3,
    "max_articles_per_stock": 5,
    "request_pause_seconds": 3.0,
}
INFLUENCER_WATCH_SETTINGS: Dict[str, Any] = {
    "enabled": False,
    "lookback": "7d",
    "max_articles_per_influencer": 2,
    "max_rotation_pairs_to_check": 3,
    "request_pause_seconds": 3.0,
    "people": [],
}


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as file:
        config = yaml.safe_load(file)
    FETCH_SETTINGS.update(config.get("data_fetch", {}))
    NEWS_SEARCH_SETTINGS.update(config.get("news_search", {}))
    INFLUENCER_WATCH_SETTINGS.update(config.get("influencer_watch", {}))
    return config


def log_quality(quality: DataQuality) -> None:
    DATA_QUALITY_LOG.append(quality)


def quality_line(quality: DataQuality) -> str:
    warnings = quality.warnings or []
    warning_text = "无 / none" if not warnings else "; ".join(warnings)
    freshness = "n/a" if quality.freshness_days is None else f"{quality.freshness_days}天"
    base = (
        f"{quality.symbol}: {quality.status}; 来源/source={quality.source}; "
        f"尝试/attempts={quality.attempts}; 数据行/rows={quality.rows}; "
        f"最新日期/latest={quality.latest_date}; 新鲜度/freshness={freshness}; "
        f"提示/warnings={warning_text}"
    )
    if quality.error:
        base += f"; 错误/error={quality.error}"
    return base


def fetch_quality_report() -> str:
    if not FETCH_SETTINGS.get("include_quality_report", True):
        return ""

    if not DATA_QUALITY_LOG:
        return ""

    ok_count = sum(1 for item in DATA_QUALITY_LOG if item.status == "ok")
    def is_fallback_only(item: DataQuality) -> bool:
        warnings = item.warnings or []
        return bool(warnings) and all("备用源" in warning or "fallback used" in warning for warning in warnings)

    fallback_items = [item for item in DATA_QUALITY_LOG if is_fallback_only(item)]
    warning_items = [item for item in DATA_QUALITY_LOG if item.warnings and not is_fallback_only(item)]
    failed_items = [item for item in DATA_QUALITY_LOG if item.status != "ok"]
    warn_count = len(warning_items) + len(fallback_items)
    fail_count = sum(1 for item in DATA_QUALITY_LOG if item.status != "ok")
    total_count = len(DATA_QUALITY_LOG)
    qualities_to_show = failed_items + [item for item in warning_items if item.status == "ok"]
    if FETCH_SETTINGS.get("include_successful_fetches", False):
        qualities_to_show = DATA_QUALITY_LOG

    quality_status = "良好 / good"
    if fail_count:
        quality_status = "有失败，需要复核 / failures, review needed"
    elif warn_count:
        quality_status = "有轻微警告 / minor warnings"

    lines = [
        "数据获取质量 / Data Quality",
        "----------------------------",
        markdown_table(
            ["结论", "请求汇总", "交易前提醒"],
            [
                [
                    quality_status,
                    f"{total_count} total / {ok_count} ok / {warn_count} warnings / {fail_count} failed",
                    "公开数据可能延迟；关键价格和新闻仍需人工复核。",
                ]
            ],
        ),
    ]
    if fallback_items:
        lines.append("")
        lines.append(
            markdown_table(
                ["特殊情况", "影响"],
                [[f"新闻搜索 {len(fallback_items)} 次使用备用源", "降级成功，不影响行情数据。"]],
            )
        )
    if qualities_to_show:
        lines.append("")
        lines.append("异常项 / Exceptions:")
        lines.append(
            markdown_table(
                ["标的", "状态", "来源", "提示"],
                [
                    [
                        item.symbol,
                        item.status,
                        trim_text(item.source, 38),
                        trim_text("; ".join(item.warnings or []) or item.error or "n/a", 90),
                    ]
                    for item in qualities_to_show[:10]
                ],
            )
        )
    return "\n".join(lines)


def data_latest_date_and_freshness(close: Any) -> Tuple[str, Optional[int]]:
    try:
        latest = close.index[-1]
        if hasattr(latest, "to_pydatetime"):
            latest_dt = latest.to_pydatetime()
        else:
            latest_dt = latest
        if latest_dt.tzinfo is None:
            latest_dt = latest_dt.replace(tzinfo=timezone.utc)
        freshness_days = (datetime.now(timezone.utc).date() - latest_dt.date()).days
        return latest_dt.date().isoformat(), freshness_days
    except Exception:
        return "n/a", None


def fetch_quote(ticker: str) -> Quote:
    attempts = int(FETCH_SETTINGS.get("max_retries", 3))
    backoff = float(FETCH_SETTINGS.get("retry_backoff_seconds", 2.0))
    pause = float(FETCH_SETTINGS.get("request_pause_seconds", 0.7))
    last_error: Optional[Exception] = None
    data = None

    for attempt in range(1, attempts + 1):
        if pause > 0:
            time.sleep(pause)
        try:
            data = yf.download(
                ticker,
                period="5y",
                interval="1d",
                progress=False,
                auto_adjust=False,
                threads=False,
                timeout=20,
            )
            if data is not None and not data.empty and len(data) >= 2:
                break
            last_error = ValueError(f"No usable price data returned for {ticker}")
        except Exception as exc:
            last_error = exc
        if attempt < attempts:
            time.sleep(backoff * attempt)

    if data is None or data.empty or len(data) < 2:
        quality = DataQuality(
            source="Yahoo Finance/yfinance",
            symbol=ticker,
            status="failed",
            attempts=attempts,
            warnings=["数据为空或不足 / empty or insufficient data"],
            error=str(last_error) if last_error else "unknown error",
        )
        log_quality(quality)
        raise ValueError(f"No price data returned for {ticker}: {quality.error}")

    if data.empty or len(data) < 2:
        raise ValueError(f"No price data returned for {ticker}")

    if isinstance(data.columns, tuple):
        data.columns = [col[0] for col in data.columns]
    elif hasattr(data.columns, "nlevels") and data.columns.nlevels > 1:
        data.columns = data.columns.get_level_values(0)

    close = data["Close"].dropna()
    volume = data["Volume"].dropna() if "Volume" in data.columns else None
    latest_date, freshness_days = data_latest_date_and_freshness(close)
    warnings: List[str] = []
    if len(close) < int(FETCH_SETTINGS.get("min_history_rows", 60)):
        warnings.append("历史数据偏少 / limited history")
    stale_after = int(FETCH_SETTINGS.get("stale_after_calendar_days", 7))
    if freshness_days is not None and freshness_days > stale_after:
        warnings.append(f"最新行情可能偏旧，超过{stale_after}天 / stale data")
    if volume is None or volume.empty:
        warnings.append("缺少成交量数据 / missing volume")

    quality = DataQuality(
        source="Yahoo Finance/yfinance",
        symbol=ticker,
        status="ok",
        rows=len(close),
        latest_date=latest_date,
        freshness_days=freshness_days,
        attempts=attempt,
        warnings=warnings,
    )
    log_quality(quality)

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
        quality=quality,
    )


def fetch_crypto_fear_greed() -> SentimentIndex:
    url = "https://api.alternative.me/fng/?limit=1"
    attempts = int(FETCH_SETTINGS.get("max_retries", 3))
    backoff = float(FETCH_SETTINGS.get("retry_backoff_seconds", 2.0))
    pause = float(FETCH_SETTINGS.get("request_pause_seconds", 0.7))
    last_error: Optional[Exception] = None
    payload = None
    for attempt in range(1, attempts + 1):
        if pause > 0:
            time.sleep(pause)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "market-watch-bot/1.0"})
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(backoff * attempt)

    if payload is None:
        quality = DataQuality(
            source="Alternative.me Crypto Fear & Greed API",
            symbol="CRYPTO_FNG",
            status="failed",
            attempts=attempts,
            warnings=["情绪指数获取失败 / sentiment fetch failed"],
            error=str(last_error) if last_error else "unknown error",
        )
        log_quality(quality)
        raise ValueError(f"Crypto Fear & Greed fetch failed: {quality.error}")

    item = payload["data"][0]
    warnings: List[str] = []
    timestamp = item.get("timestamp", "")
    freshness_days = None
    if timestamp:
        try:
            ts_dt = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
            freshness_days = (datetime.now(timezone.utc).date() - ts_dt.date()).days
            if freshness_days > int(FETCH_SETTINGS.get("stale_after_calendar_days", 7)):
                warnings.append("情绪指数可能偏旧 / stale sentiment data")
        except Exception:
            warnings.append("无法解析情绪指数时间戳 / timestamp parse failed")
    quality = DataQuality(
        source="Alternative.me Crypto Fear & Greed API",
        symbol="CRYPTO_FNG",
        status="ok",
        rows=1,
        latest_date=timestamp or "n/a",
        freshness_days=freshness_days,
        attempts=attempt,
        warnings=warnings,
    )
    log_quality(quality)
    return SentimentIndex(
        name="Crypto Fear & Greed",
        value=int(item["value"]),
        classification=item["value_classification"],
        timestamp=item.get("timestamp", ""),
        quality=quality,
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


def quote_metrics_inline(quote: Quote) -> str:
    ratio = volume_ratio(quote)
    ratio_text = "n/a" if ratio is None else f"{ratio:.1f}x"
    return f"{quote.last:.2f}; 今日 {pct_line(quote.daily_pct)}; 5日 {pct_line(quote.five_day_pct)}; 量 {ratio_text}"


def daily_volume_watch_readout(stock: Dict[str, Any], quote: Quote) -> Tuple[str, str]:
    rules = stock.get("daily_volume_watch", {})
    ratio = volume_ratio(quote)
    ratio_text = "n/a" if ratio is None else f"{ratio:.1f}x"
    support = rules.get("support_zone")
    reclaim = rules.get("reclaim_zone")
    panic_ratio = rules.get("panic_volume_ratio", 2.0)
    accumulation_ratio = rules.get("accumulation_volume_ratio", 1.5)
    quiet_ratio = rules.get("quiet_volume_ratio", 0.8)

    if ratio is None:
        read = "成交量数据不足，今天只能看价格，不能判断资金是否真正进出。"
        decision = "不做动作；等下一次有可靠成交量数据。"
    elif quote.daily_pct < -3 and ratio >= panic_ratio:
        read = f"放量大跌：成交量约 {ratio_text}，价格下跌 {quote.daily_pct:+.2f}%。这通常说明机构或短线资金还在主动卖。"
        decision = "不要接第一刀；等后续缩量止跌或放量反弹确认。"
    elif quote.daily_pct < 0 and ratio <= quiet_ratio:
        read = f"缩量下跌：成交量约 {ratio_text}，卖压比前期弱。简单说，跌还在跌，但卖的人可能少了。"
        decision = "进入观察，不急买；如果后续不再创新低，才考虑小仓研究。"
    elif quote.daily_pct > 2 and ratio >= accumulation_ratio:
        read = f"放量反弹：成交量约 {ratio_text}，价格上涨 {quote.daily_pct:+.2f}%。这比普通反弹更重要，可能有资金回补。"
        decision = "可准备小仓试探，但仍要确认不是一天反抽；优先看能否站稳关键价位。"
    elif quote.daily_pct > 0:
        read = f"温和反弹：成交量约 {ratio_text}。价格有修复，但资金确认力度还不够强。"
        decision = "继续观察；不追高，等放量或站稳关键区间。"
    else:
        read = f"震荡/弱势整理：成交量约 {ratio_text}，今天没有明确资金重新进场信号。"
        decision = "保持雷达状态；等待卖压衰竭或资金确认。"

    if support is not None and quote.last <= support:
        decision += f"\n关键提醒：价格已到/跌破观察支撑 {support}，需要确认是否能收回，否则技术面继续受伤。"
    elif reclaim is not None and quote.last >= reclaim:
        decision += f"\n关键提醒：价格已到/收复观察区 {reclaim}，如果配合成交量放大，说明财报后杀估值可能开始修复。"

    focus = rules.get("focus")
    if focus:
        read += f"\n观察重点：{focus}"
    note = rules.get("decision_note")
    if note:
        decision += f"\nCEO口径：{note}"

    decision += "\n" + framework_readout(stock, quote, ["daily volume watch"])
    return read, decision


def overhang_watch_readout(stock: Dict[str, Any], quote: Quote) -> Tuple[str, str]:
    rules = stock.get("overhang_watch", {})
    price = quote.last
    auto_levels = auto_overhang_levels(quote) or {}
    battle_low = rules.get("current_battle_zone_low", auto_levels.get("current_battle_zone_low"))
    battle_high = rules.get("current_battle_zone_high", auto_levels.get("current_battle_zone_high"))
    near_low = rules.get("near_resistance_low", auto_levels.get("near_resistance_low"))
    near_high = rules.get("near_resistance_high", auto_levels.get("near_resistance_high"))
    heavy_low = rules.get("heavy_overhang_low", auto_levels.get("heavy_overhang_low"))
    heavy_high = rules.get("heavy_overhang_high", auto_levels.get("heavy_overhang_high"))
    support_low = rules.get("support_low", auto_levels.get("support_low"))
    support_high = rules.get("support_high", auto_levels.get("support_high"))
    deep_low = rules.get("deep_value_low", auto_levels.get("deep_value_low"))
    deep_high = rules.get("deep_value_high", auto_levels.get("deep_value_high"))
    auto_note = ""
    if auto_levels and not rules:
        auto_note = " 这些区间基于过去1年价格结构自动估算，用来帮助你识别低位承接和上方套牢盘压力。"

    if battle_low is not None and battle_high is not None and battle_low <= price < battle_high:
        read = (
            f"股价位于修复拉锯区 {battle_low:.0f}-{battle_high:.0f}。"
            " 这通常说明低位反弹已经发生，但最近一批被套/想回本离场的筹码还没有完全消化。"
        )
        decision = rules.get(
            "current_battle_note",
            "Repair zone only. Respect the rebound, but do not chase while the nearest supply wall is still overhead.",
        )
    elif near_low is not None and near_high is not None and near_low <= price < near_high:
        read = (
            f"股价正在接近最近套牢盘压力区 {near_low:.0f}-{near_high:.0f}。"
            " 如果没有明显放量和基本面催化，最容易出现回本卖压。"
        )
        decision = rules.get(
            "near_resistance_note",
            "Nearest trapped-holder wall. Watch for profit-taking or break-even selling rather than fresh chasing.",
        )
    elif heavy_low is not None and heavy_high is not None and heavy_low <= price <= heavy_high:
        read = (
            f"股价进入更重的中期套牢盘区 {heavy_low:.0f}-{heavy_high:.0f}。"
            " 这类区域通常不是轻松突破的地方，除非订单、指引或信用担忧出现明显改善。"
        )
        decision = rules.get(
            "heavy_overhang_note",
            "Heavier trapped supply overhead. Treat this zone as a likely trim/review area unless the thesis improves materially.",
        )
    elif support_low is not None and support_high is not None and support_low <= price < support_high:
        read = (
            f"股价位于最近低位换手支撑区 {support_low:.0f}-{support_high:.0f}。"
            " 简单说，这里更像低位承接区，而不是明显的套牢墙。"
        )
        decision = rules.get(
            "support_note",
            "Support band. If this area holds, low-zone buyers are still defending and the repair structure stays alive.",
        )
    elif deep_low is not None and deep_high is not None and deep_low <= price < deep_high:
        read = (
            f"股价回到深度价值区 {deep_low:.0f}-{deep_high:.0f}。"
            " 这更符合你在恐慌里布局的风格，但必须先确认基本面没有继续恶化。"
        )
        decision = rules.get(
            "deep_value_note",
            "Best value area for your style if the thesis remains intact and there is no new balance-sheet or cash-flow damage.",
        )
    else:
        read = "当前价格没有落在预设的主要套牢盘或价值区间，先结合量价和基本面一起看。"
        decision = "把套牢盘结构当作辅助地图，不单独作为买卖理由。"

    if auto_note:
        read += auto_note
    return read, decision


def entry_alert_row(stock: Dict[str, Any], quote: Quote) -> Optional[List[str]]:
    rules = stock.get("entry_alert", {})
    if not rules.get("enabled", False):
        return None

    ratio = volume_ratio(quote)
    ratio_min = rules.get("volume_ratio_min")
    volume_ok = ratio_min is None or (ratio is not None and ratio >= ratio_min)
    daily_drop = rules.get("daily_drop_pct")
    five_day_drop = rules.get("five_day_drop_pct")
    daily_ok = daily_drop is not None and quote.daily_pct <= daily_drop
    five_day_ok = five_day_drop is not None and quote.five_day_pct is not None and quote.five_day_pct <= five_day_drop

    if not ((daily_ok or five_day_ok) and volume_ok):
        return None

    ratio_text = "n/a" if ratio is None else f"{ratio:.1f}x"
    trigger_bits = []
    if daily_ok:
        trigger_bits.append(f"单日 {quote.daily_pct:+.2f}% <= {daily_drop:+.2f}%")
    if five_day_ok:
        trigger_bits.append(f"5日 {quote.five_day_pct:+.2f}% <= {five_day_drop:+.2f}%")
    trigger_text = "; ".join(trigger_bits)
    guardrails = guardrail_text(stock)

    read = (
        f"触发深回撤建仓雷达：{trigger_text}，成交量约 {ratio_text}。\n"
        "这不是自动买入，而是说明市场正在用真实成交量重新定价这只高质量/高弹性标的。\n"
        "前提条件：基本面没有坏。必须先排除下面红旗：\n"
        f"{guardrails}"
    )
    decision = (
        f"{rules.get('message', 'Prepare staged-entry research if the thesis remains intact.')}\n"
        "建议动作：先做基本面红旗核查；如果红旗没有出现，等待止跌或放量反弹，再考虑小仓分批。"
    )
    return [
        f"{stock['name']} ({stock['ticker']})",
        "深回撤建仓雷达 / deep pullback entry watch",
        quote_metrics_inline(quote),
        read,
        decision,
    ]


def plain_text(text: str) -> str:
    return str(text).replace("<br>", "\n").replace("|", "/")


def indent_text(text: str, spaces: int = 6) -> str:
    prefix = " " * spaces
    return "\n".join(prefix + line if line else line for line in plain_text(text).splitlines())


def table_cell(text: str) -> str:
    return text.replace("\n", "<br>").replace("|", "/")


def markdown_table(headers: List[str], rows: List[List[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(table_cell(item) for item in row) + " |")
    return "\n".join(lines)


def short_reasons(reasons: List[str]) -> str:
    return "\n".join(f"- {reason}" for reason in reasons)


def first_guardrails(stock: Dict[str, Any], limit: int = 3) -> str:
    red_flags = stock.get("rotation", {}).get("fundamental_guardrail", {}).get("red_flags", [])
    if not red_flags:
        return "暂无特别红旗 / no specific red flags configured"
    shown = red_flags[:limit]
    suffix = "" if len(red_flags) <= limit else f"<br>另有 {len(red_flags) - limit} 项，交易前再展开复核"
    return "<br>".join(f"- {flag}" for flag in shown) + suffix


def trim_text(text: str, max_len: int = 120) -> str:
    cleaned = " ".join(str(text).split())
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 3] + "..."


def gdelt_articles_for_query(query: str, max_records: int, lookback: str) -> List[Dict[str, Any]]:
    params = urllib.parse.urlencode(
        {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "maxrecords": max_records,
            "sort": "hybrid",
            "timespan": lookback,
        }
    )
    url = f"https://api.gdeltproject.org/api/v2/doc/doc?{params}"
    request = urllib.request.Request(url, headers={"User-Agent": "market-watch-bot/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload.get("articles", []) or []


def google_news_rss_articles_for_query(query: str, max_records: int) -> List[Dict[str, Any]]:
    params = urllib.parse.urlencode(
        {
            "q": query,
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        }
    )
    url = f"https://news.google.com/rss/search?{params}"
    request = urllib.request.Request(url, headers={"User-Agent": "market-watch-bot/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        root = ET.fromstring(response.read())
    articles: List[Dict[str, Any]] = []
    for item in root.findall("./channel/item")[:max_records]:
        link = item.findtext("link", default="")
        title = item.findtext("title", default="Untitled")
        source_node = item.find("source")
        domain = source_node.text if source_node is not None and source_node.text else "Google News"
        articles.append({"title": title, "url": link, "domain": domain})
    return articles


def news_articles_for_query(query: str, max_records: int, lookback: str) -> Tuple[List[Dict[str, Any]], str, List[str]]:
    try:
        return gdelt_articles_for_query(query, max_records, lookback), "GDELT DOC API news search", []
    except Exception as gdelt_exc:
        articles = google_news_rss_articles_for_query(query, max_records)
        warning = "主新闻源限流或失败，已使用 Google News RSS 备用源 / primary news source failed, fallback used"
        return articles, "Google News RSS fallback", [warning]


def red_flag_keywords(red_flags: List[str]) -> List[str]:
    stopwords = {
        "materially",
        "worsens",
        "worsen",
        "deteriorates",
        "deteriorate",
        "current",
        "without",
        "support",
        "business",
        "growth",
        "risk",
        "risks",
    }
    keywords: List[str] = []
    for flag in red_flags:
        for raw_word in flag.replace("/", " ").replace("-", " ").split():
            word = "".join(char for char in raw_word.lower() if char.isalnum())
            if len(word) < 5 or word in stopwords:
                continue
            if word not in keywords:
                keywords.append(word)
    return keywords[:10]


def article_evidence_text(article: Dict[str, Any]) -> str:
    title = trim_text(article.get("title", "Untitled"), 80)
    domain = article.get("domain") or article.get("sourceCountry") or "source"
    return f"{title} ({domain})".strip()


def article_matches_flag(article: Dict[str, Any], flag: str) -> bool:
    text = f"{article.get('title', '')} {article.get('seendate', '')} {article.get('domain', '')}".lower()
    flag_words = red_flag_keywords([flag])
    if not flag_words:
        return False
    matches = sum(1 for word in flag_words if word in text)
    needed = 1 if len(flag_words) == 1 else 2
    return matches >= needed


def stock_news_query(stock: Dict[str, Any], red_flags: List[str]) -> str:
    keywords = red_flag_keywords(red_flags)
    query_terms = " OR ".join(keywords[:8]) if keywords else "risk OR guidance OR downgrade"
    ticker_base = str(stock["ticker"]).split(".")[0].replace("-", " ")
    return f'"{stock["name"]}" "{ticker_base}" ({query_terms})'


def red_flag_news_check(stock: Dict[str, Any]) -> str:
    if not NEWS_SEARCH_SETTINGS.get("enabled", False):
        return first_guardrails(stock)

    cache_key = stock["ticker"]
    if cache_key in NEWS_CHECK_CACHE:
        return NEWS_CHECK_CACHE[cache_key]

    red_flags = stock.get("rotation", {}).get("fundamental_guardrail", {}).get("red_flags", [])
    if not red_flags:
        result = "暂无特别红旗 / no specific red flags configured"
        NEWS_CHECK_CACHE[cache_key] = result
        return result

    max_flags = int(NEWS_SEARCH_SETTINGS.get("max_red_flags_per_stock", 3))
    max_articles = int(NEWS_SEARCH_SETTINGS.get("max_articles_per_stock", 5))
    lookback = str(NEWS_SEARCH_SETTINGS.get("lookback", "14d"))
    pause = float(NEWS_SEARCH_SETTINGS.get("request_pause_seconds", 1.0))
    shown_flags = red_flags[:max_flags]
    query = stock_news_query(stock, shown_flags)

    if pause > 0:
        time.sleep(pause)
    try:
        articles, source, warnings = news_articles_for_query(query, max_articles, lookback)
        log_quality(
            DataQuality(
                source=source,
                symbol=f"NEWS:{stock['ticker']}",
                status="ok",
                rows=len(articles),
                latest_date=lookback,
                warnings=warnings,
            )
        )
    except Exception as exc:
        log_quality(
            DataQuality(
                source="GDELT DOC API news search",
                symbol=f"NEWS:{stock['ticker']}",
                status="failed",
                warnings=["红旗新闻核查失败 / red-flag news check failed"],
                error=str(exc),
            )
        )
        result = "新闻核查失败，需要人工搜索 / news check failed, manual review needed"
        NEWS_CHECK_CACHE[cache_key] = result
        return result

    evidence_rows: List[str] = []
    missed_flags: List[str] = []
    for index, flag in enumerate(shown_flags, start=1):
        matched_articles = [article for article in articles if article_matches_flag(article, flag)]
        evidence_articles = matched_articles[:2]
        if not articles:
            missed_flags.append(trim_text(flag, 42))
            continue
        if not evidence_articles:
            missed_flags.append(trim_text(flag, 42))
            continue

        evidence = []
        for article in evidence_articles:
            evidence.append(article_evidence_text(article))
        evidence_rows.append(
            f"- 疑似红旗：{trim_text(flag, 48)}\n"
            f"  证据：{'; '.join(evidence)}"
        )

    extra_count = len(red_flags) - max_flags
    rows: List[str] = []
    if evidence_rows:
        rows.append("发现疑似线索，需人工复核：")
        rows.extend(evidence_rows)
    else:
        rows.append(f"未发现直接匹配红旗新闻（已查前 {len(shown_flags)} 项）。")
    if missed_flags:
        rows.append("仍需人工确认：" + "；".join(missed_flags[:3]))
    if extra_count > 0:
        rows.append(f"另有 {extra_count} 条红旗未自动搜索，交易前再展开。")

    result = "\n".join(rows)
    NEWS_CHECK_CACHE[cache_key] = result
    return result


def query_from_aliases(aliases: List[str], extra_terms: str = "") -> str:
    quoted_aliases = [f'"{alias}"' for alias in aliases if alias]
    alias_query = " OR ".join(quoted_aliases)
    if extra_terms:
        return f"({alias_query}) {extra_terms}"
    return f"({alias_query})"


def article_titles_summary(articles: List[Dict[str, Any]], max_items: int) -> str:
    if not articles:
        return "未发现最新公开动态 / no recent public item found"
    return "\n".join(f"- {article_evidence_text(article)}" for article in articles[:max_items])


def stance_from_articles(articles: List[Dict[str, Any]]) -> str:
    if not articles:
        return "未发现 / not found"
    text = " ".join(article.get("title", "") for article in articles).lower()
    bullish_words = ["buy", "bull", "bullish", "breakout", "long", "accumulate", "leader", "strength"]
    bearish_words = ["sell", "bear", "bearish", "short", "avoid", "crash", "breakdown", "risk", "loss"]
    bullish = sum(1 for word in bullish_words if word in text)
    bearish = sum(1 for word in bearish_words if word in text)
    if bullish > bearish:
        return "偏正面 / leaning positive"
    if bearish > bullish:
        return "偏谨慎 / leaning cautious"
    return "有提及但态度不明确 / mentioned, unclear stance"


def article_mentions_stock(article: Dict[str, Any], stock: Dict[str, Any]) -> bool:
    text = f"{article.get('title', '')} {article.get('domain', '')}".lower()
    ticker_base = str(stock.get("ticker", "")).split(".")[0].lower()
    name_words = [word.lower() for word in str(stock.get("name", "")).replace("/", " ").split() if len(word) >= 4]
    if ticker_base and ticker_base in text:
        return True
    return any(word in text for word in name_words)


def fetch_public_commentary(query: str, symbol: str, max_records: int, lookback: str) -> List[Dict[str, Any]]:
    try:
        articles, source, warnings = news_articles_for_query(query, max_records, lookback)
        log_quality(
            DataQuality(
                source=source,
                symbol=symbol,
                status="ok",
                rows=len(articles),
                latest_date=lookback,
                warnings=warnings,
            )
        )
        return articles
    except Exception as exc:
        log_quality(
            DataQuality(
                source="Public commentary news/RSS search",
                symbol=symbol,
                status="failed",
                warnings=["公开观点搜索失败 / public commentary search failed"],
                error=str(exc),
            )
        )
        return []


def influencer_latest_rows(config: Dict[str, Any]) -> List[List[str]]:
    settings = config.get("influencer_watch", {})
    if not settings.get("enabled", False):
        return []

    people = settings.get("people", [])
    max_articles = int(settings.get("max_articles_per_influencer", 2))
    lookback = str(settings.get("lookback", "7d"))
    pause = float(settings.get("request_pause_seconds", 3.0))
    rows: List[List[str]] = []

    for person in people:
        if pause > 0:
            time.sleep(pause)
        aliases = person.get("aliases", [person.get("name", "")])
        query = query_from_aliases(aliases, person.get("topic_terms", "stock trading OR investing"))
        cache_key = f"latest:{person.get('name')}:{query}"
        if cache_key in INFLUENCER_CHECK_CACHE:
            latest = INFLUENCER_CHECK_CACHE[cache_key]
        else:
            articles = fetch_public_commentary(query, f"INFL:{person.get('name', 'unknown')}", max_articles, lookback)
            latest = article_titles_summary(articles, max_articles)
            INFLUENCER_CHECK_CACHE[cache_key] = latest
        rows.append(
            [
                person.get("name", ""),
                person.get("background", ""),
                person.get("core_view", ""),
                latest,
                "只作风格参考，不作为买卖指令 / style reference only",
            ]
        )
    return rows


def influencer_rotation_check(from_stock: Dict[str, Any], to_stock: Dict[str, Any], pair_index: int) -> str:
    if not INFLUENCER_WATCH_SETTINGS.get("enabled", False):
        return "未启用 / disabled"

    max_pairs = int(INFLUENCER_WATCH_SETTINGS.get("max_rotation_pairs_to_check", 3))
    if pair_index > max_pairs:
        return "未自动搜索；超过本次组合核查上限 / not checked, pair limit reached"

    people = INFLUENCER_WATCH_SETTINGS.get("people", [])
    aliases: List[str] = []
    for person in people:
        aliases.extend(person.get("aliases", [person.get("name", "")]))
    influencers_query = " OR ".join(f'"{alias}"' for alias in aliases[:16] if alias)
    pair_terms = (
        f'"{from_stock["name"]}" OR "{from_stock["ticker"]}" '
        f'"{to_stock["name"]}" OR "{to_stock["ticker"]}"'
    )
    query = f"({influencers_query}) ({pair_terms})"
    cache_key = f"rotation:{from_stock['ticker']}:{to_stock['ticker']}"
    if cache_key in INFLUENCER_CHECK_CACHE:
        return INFLUENCER_CHECK_CACHE[cache_key]

    pause = float(INFLUENCER_WATCH_SETTINGS.get("request_pause_seconds", 3.0))
    if pause > 0:
        time.sleep(pause)
    max_articles = int(INFLUENCER_WATCH_SETTINGS.get("max_articles_per_influencer", 2))
    lookback = str(INFLUENCER_WATCH_SETTINGS.get("lookback", "7d"))
    articles = fetch_public_commentary(query, f"INFL_ROT:{from_stock['ticker']}->{to_stock['ticker']}", max_articles, lookback)
    articles = [article for article in articles if article_mentions_stock(article, to_stock)]
    if not articles:
        result = "未发现这些大V公开讨论该组合 / no tracked influencer comment found"
    else:
        result = f"{stance_from_articles(articles)}\n{article_titles_summary(articles, max_articles)}"
    INFLUENCER_CHECK_CACHE[cache_key] = result
    return result


def stock_signal_blocks(rows: List[List[str]]) -> str:
    grouped: Dict[str, List[List[str]]] = {}
    for row in rows:
        grouped.setdefault(row[0], []).append(row)

    blocks = []
    for index, (name, stock_rows) in enumerate(grouped.items(), start=1):
        data = stock_rows[0][2]
        analyses = [f"- {plain_text(row[3])}" for row in stock_rows]
        decisions = [f"- {plain_text(row[4])}" for row in stock_rows]
        blocks.append(
            "\n".join(
                [
                    f"{index}. {name}",
                    f"   数据：{data}",
                    "   分析：",
                    indent_text("\n".join(analyses), 3),
                    "   结论：",
                    indent_text("\n".join(decisions), 3),
                ]
            )
        )
    return "\n\n".join(blocks)


def influencer_blocks(rows: List[List[str]]) -> str:
    table_rows = []
    for name, background, core_view, latest, usage in rows:
        table_rows.append(
            [
                name,
                trim_text(background, 54),
                trim_text(core_view, 54),
                trim_text(plain_text(latest), 90),
                trim_text(usage, 42),
            ]
        )
    return markdown_table(["人物", "背景", "方法", "最新信号", "用法"], table_rows)


def unique_lines(items: List[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for item in items:
        cleaned = item.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


def grouped_stock_sections(rows: List[List[str]]) -> List[Tuple[str, str, List[List[str]]]]:
    grouped: Dict[str, List[List[str]]] = {}
    for row in rows:
        grouped.setdefault(row[0], []).append(row)

    sections: List[Tuple[str, str, List[List[str]]]] = []
    for name, stock_rows in grouped.items():
        metrics = stock_rows[0][2]
        sections.append((name, metrics, stock_rows))
    return sections


def executive_summary(rotation_alerts: List[str], stock_rows: List[List[str]], indicator_alerts: List[str]) -> str:
    rows: List[List[str]] = []
    if rotation_alerts:
        rows.append(["调仓", "有轮动候选触发", "只进入人工复核，不直接下单。"])
    if any("held strength" in row[1] or "持仓大涨" in row[1] for row in stock_rows):
        rows.append(["持仓", "部分持仓进入强势检查区", "优先考虑分批止盈，而不是追高。"])
    if any("deep pullback" in row[1] or "接近" in row[1] or "低位" in row[1] for row in stock_rows):
        rows.append(["机会", "出现低位/深回撤信号", "先排除基本面破坏，再考虑分批。"])
    if any("extreme-fear" in text.lower() or "极度恐慌" in text for text in indicator_alerts):
        rows.append(["情绪", "市场进入极度恐慌观察区", "提高低位研究优先级。"])

    if not rows:
        rows = [["结论", "今天没有新的高优先级动作", "继续观察，不做情绪化交易。"]]
    return markdown_table(["类别", "核心判断", "动作"], rows)


def row_priority_score(row: List[str]) -> int:
    priority_keys = [
        "deep pullback",
        "低位",
        "重估",
        "re-rating",
        "held strength",
        "持仓大涨",
        "5d strength",
        "daily price-volume watch",
        "每日量价观察",
        "套牢盘压力",
    ]
    text = f"{row[1]} {row[3]} {row[4]}".lower()
    for index, key in enumerate(priority_keys):
        if key in text:
            return index
    return len(priority_keys)


def top_signal_rows(rows: List[List[str]], max_rows: int = 6) -> List[List[str]]:
    ranked = sorted(rows, key=row_priority_score)
    top_rows = ranked[:max_rows]
    return top_rows


def decision_signal_table(rows: List[List[str]], max_rows: int = 6) -> str:
    ranked = top_signal_rows(rows, max_rows=max_rows)
    table_rows: List[List[str]] = []
    for name, signal_type, metrics, analysis, decision in ranked:
        table_rows.append(
            [
                name,
                trim_text(signal_type, 28),
                trim_text(metrics, 58),
                trim_text(plain_text(analysis), 82),
                trim_text(plain_text(decision), 82),
            ]
        )
    return markdown_table(
        ["标的 / Asset", "信号 / Signal", "数据 / Snapshot", "判断 / Read", "动作 / Action"],
        table_rows,
    )


def concise_signal_briefs(rows: List[List[str]], max_rows: int = 4) -> str:
    return decision_signal_table(rows, max_rows=max_rows)


def portfolio_diagnosis(rows: List[List[str]]) -> str:
    sections = grouped_stock_sections(rows)
    blocks: List[str] = []
    for name, metrics, stock_rows in sections:
        role = "观察标的 / watchlist"
        actions: List[str] = []
        risks: List[str] = []
        states: List[str] = []
        for row in stock_rows:
            signal_type = row[1].lower()
            analysis = plain_text(row[3])
            decision = plain_text(row[4])
            if "held" in signal_type or "持仓" in signal_type:
                role = "持仓 / held"
            if "deep pullback" in signal_type or "低位" in signal_type:
                states.append("低位研究区 / low-zone research")
            elif "re-rating" in signal_type or "重估" in signal_type:
                states.append("重估观察 / re-rating watch")
            elif "strength" in signal_type or "强势" in signal_type:
                states.append("强势检查区 / strength review")
            else:
                states.append(trim_text(row[1], 28))
            actions.append(decision)
            risks.append(analysis)
        blocks.append(
            "\n".join(
                [
                    f"**{name}**",
                    f"- 角色 / Role: {role}",
                    f"- 当前阶段 / Current phase: {'; '.join(unique_lines(states[:2]))}",
                    f"- 关键信息 / Key read: {trim_text(' | '.join(unique_lines(risks[:2])), 180)}",
                    f"- 建议动作 / Suggested action: {trim_text(' | '.join(unique_lines(actions[:2])), 180)}",
                ]
            )
        )
    return "\n\n".join(blocks)


def tight_portfolio_diagnosis(rows: List[List[str]], max_names: int = 6) -> str:
    sections = grouped_stock_sections(rows)[:max_names]
    table_rows: List[List[str]] = []
    for name, metrics, stock_rows in sections:
        signal = trim_text(stock_rows[0][1], 26)
        read = trim_text(plain_text(stock_rows[0][3]), 96)
        action = trim_text(plain_text(stock_rows[0][4]), 96)
        table_rows.append([name, signal, trim_text(metrics, 52), read, action])
    return markdown_table(["标的", "阶段", "数据", "判断", "动作"], table_rows)


def market_temperature(indicator_alerts: List[str]) -> str:
    if not indicator_alerts:
        return "今天没有新的宏观/情绪硬信号。/ No new macro or sentiment hard trigger today."
    return "\n\n".join(indicator_alerts[:4])


def compact_market_temperature(indicator_alerts: List[str], max_items: int = 3) -> str:
    if not indicator_alerts:
        return ""
    return "\n".join(f"- {trim_text(plain_text(text), 150)}" for text in indicator_alerts[:max_items])


def quote_by_indicator_name(config: Dict[str, Any], quote_cache: Dict[str, Quote], name: str) -> Optional[Quote]:
    for indicator in config.get("market_indicators", []):
        if indicator.get("name") == name and indicator.get("type") != "crypto_fear_greed":
            try:
                return get_quote(indicator["ticker"], quote_cache)
            except Exception:
                return None
    return None


def geo_stock_lines(scenario: Dict[str, Any], tickers: List[str]) -> List[str]:
    stock_map = scenario.get("stock_impact", {})
    lines: List[str] = []
    for ticker in tickers:
        if ticker in stock_map:
            lines.append(f"- {ticker}: {stock_map[ticker]}")
    return lines


def geo_theme_block(
    scenario: Dict[str, Any],
    score: int,
    state: str,
    market_trade: str,
    read: str,
    impact: str,
    proxies: str,
    stock_lines: List[str],
) -> str:
    notes = scenario.get("notes", {})
    duration = notes.get("base_case_duration", "unknown")
    watch_for_change = notes.get("watch_for_change", "")
    lines = [
        f"主线 / Theme: {scenario.get('label', 'Geopolitics')}",
        f"评分 / Score: {score}/5",
        f"状态 / State: {state}",
        f"市场在交易什么 / What the market is trading: {market_trade}",
        f"市场读法 / Market read: {read}",
        f"对组合的含义 / Portfolio impact: {impact}",
        f"预计持续 / Expected duration: {duration}",
        f"变化观察点 / Watch for change: {watch_for_change}",
        f"关键市场代理 / Key market proxies: {proxies}",
    ]
    if stock_lines:
        lines.extend(["影响地图 / Impact map:"] + stock_lines)
    return "\n".join(lines)


def evaluate_middle_east_theme(config: Dict[str, Any], quote_cache: Dict[str, Quote], scenario: Dict[str, Any]) -> Optional[Tuple[int, str]]:
    brent = quote_by_indicator_name(config, quote_cache, "Brent Crude")
    vix = quote_by_indicator_name(config, quote_cache, "VIX")
    tnx = quote_by_indicator_name(config, quote_cache, "US 10Y Yield")
    qqq = quote_by_indicator_name(config, quote_cache, "Nasdaq 100 ETF")
    if not brent or not vix or not tnx or not qqq:
        return None

    oil_up = brent.five_day_pct is not None and brent.five_day_pct >= float(scenario.get("oil_spike_5d_pct", 6.0))
    oil_down = brent.five_day_pct is not None and brent.five_day_pct <= float(scenario.get("oil_drop_5d_pct", -4.0))
    vix_high = vix.last >= float(scenario.get("vix_fear_level", 25.0))
    tnx_high = tnx.last >= float(scenario.get("tnx_risk_level", 47.0))
    qqq_risk_on = qqq.five_day_pct is not None and qqq.five_day_pct >= float(scenario.get("risk_on_qqq_5d_pct", 2.5))

    score = 1
    state = "中性 / mixed"
    market_trade = "市场没有把中东单独当成主导变量，更多还是把它当成油价、通胀预期和风险偏好的次级扰动。"
    read = "地缘主线暂时没有压倒其他宏观变量，市场仍在多因素混合定价。"
    impact = "对你的组合来说，更适合继续看个股自己的位置、量价和基本面，而不是按宏观新闻直接追单。"
    if oil_up and (vix_high or tnx_high):
        score = 5
        state = "升级压力 / escalation pressure"
        market_trade = "市场在交易油价上行、通胀再抬头、长端利率更难下去，以及高估值科技股估值被再压一次。"
        read = scenario.get("notes", {}).get("escalation", read)
        impact = (
            "传导链：油价上 -> 通胀担忧上 -> 长端利率偏高 -> 科技估值承压；"
            "同时风险偏好下降，更不利于 Oracle、Apple、PDD 这类需要更低贴现率或更强风险偏好的标的。"
        )
    elif oil_down and qqq_risk_on and not vix_high:
        score = 4
        state = "降温修复 / de-escalation repair"
        market_trade = "市场在交易地缘降温后的风险修复，重点不是油，而是利率压力缓和后科技和成长股的反弹空间。"
        read = scenario.get("notes", {}).get("deescalation", read)
        impact = (
            "传导链：油价下 -> 通胀担忧缓和 -> 利率压力减轻 -> 科技与成长修复。"
            "这通常利好 Oracle / Microsoft / Apple 的反弹，也利于 Airbus 维持风险偏好，但更像修复而不是无脑追高环境。"
        )

    stock_lines = geo_stock_lines(scenario, ["AIR.PA", "ORCL", "MSFT", "AAPL", "PDD", "BTC-USD"])
    return score, geo_theme_block(
        scenario,
        score,
        state,
        market_trade,
        read,
        impact,
        f"Brent {pct_line(brent.five_day_pct)} over 5d, VIX {vix.last:.2f}, US10Y {tnx.last/10:.2f}%, QQQ {pct_line(qqq.five_day_pct)} over 5d.",
        stock_lines,
    )


def evaluate_china_theme(config: Dict[str, Any], quote_cache: Dict[str, Quote], scenario: Dict[str, Any]) -> Optional[Tuple[int, str]]:
    hsi = quote_by_indicator_name(config, quote_cache, "Hang Seng Index")
    eurcny = quote_by_indicator_name(config, quote_cache, "EUR/CNY")
    qqq = quote_by_indicator_name(config, quote_cache, "Nasdaq 100 ETF")
    if not hsi or not eurcny or not qqq:
        return None

    hsi_weak = hsi.five_day_pct is not None and hsi.five_day_pct <= float(scenario.get("hsi_five_day_risk_off", -3.0))
    eurcny_stress = eurcny.five_day_pct is not None and eurcny.five_day_pct >= float(scenario.get("eurcny_five_day_stress", 1.0))
    qqq_soft = qqq.five_day_pct is not None and qqq.five_day_pct <= float(scenario.get("qqq_five_day_semis_stress", -2.5))

    score = 1
    state = "中性 / mixed"
    market_trade = "市场把这条线当成估值折价因子，而不是每天都立刻触发的大波动来源。"
    read = "中美/中国ADR/半导体限制主线暂时没有压倒其他变量。"
    impact = "如果没有新的限制升级，China ADR 和 AI 硬件链更受自身财报、估值和风险偏好驱动。"
    if hsi_weak and (eurcny_stress or qqq_soft):
        score = 5
        state = "限制压力 / restriction pressure"
        market_trade = "市场在交易政策与资本通道风险，先砍 China ADR 的估值，再压半导体和苹果这类有中国敞口的链条情绪。"
        read = scenario.get("notes", {}).get("escalation", read)
        impact = (
            "传导链：HK/China 风险偏好下滑 -> China ADR 折价扩大 -> 半导体/AI 链条情绪转弱。"
            "这通常压制 PDD / JD / Tencent 的修复，也会拖累 Apple 与 AI 硬件情绪。"
        )
    elif not hsi_weak and qqq.five_day_pct is not None and qqq.five_day_pct > 0:
        score = 3
        state = "可控 / manageable"
        market_trade = "市场在交易‘政策风险还在，但短期并未打断修复’，所以资金会继续在中概修复和AI链里做选择性回流。"
        read = scenario.get("notes", {}).get("deescalation", read)
        impact = (
            "市场暂时把最新限制消息视为可管理，而不是立即打断中国ADR或AI链主线。"
            "这更利于 PDD / Tencent 这类修复票维持反弹，但不等于政策风险消失。"
        )

    stock_lines = geo_stock_lines(scenario, ["PDD", "JD", "0700.HK", "AAPL", "ORCL", "NVDA"])
    return score, geo_theme_block(
        scenario,
        score,
        state,
        market_trade,
        read,
        impact,
        f"HSI {pct_line(hsi.five_day_pct)} over 5d, EUR/CNY {pct_line(eurcny.five_day_pct)} over 5d, QQQ {pct_line(qqq.five_day_pct)} over 5d.",
        stock_lines,
    )


def evaluate_europe_defense_theme(config: Dict[str, Any], quote_cache: Dict[str, Quote], scenario: Dict[str, Any]) -> Optional[Tuple[int, str]]:
    dax = quote_by_indicator_name(config, quote_cache, "DAX")
    cac = quote_by_indicator_name(config, quote_cache, "CAC 40")
    brent = quote_by_indicator_name(config, quote_cache, "Brent Crude")
    vix = quote_by_indicator_name(config, quote_cache, "VIX")
    if not dax or not cac or not brent or not vix:
        return None

    dax_bid = dax.five_day_pct is not None and dax.five_day_pct >= float(scenario.get("dax_five_day_defense_bid", 2.0))
    cac_bid = cac.five_day_pct is not None and cac.five_day_pct >= float(scenario.get("cac_five_day_defense_bid", 1.5))
    brent_stress = brent.five_day_pct is not None and brent.five_day_pct >= float(scenario.get("brent_five_day_stress", 4.0))
    vix_risk = vix.last >= float(scenario.get("vix_risk_level", 22.0))

    score = 1
    state = "中性 / mixed"
    market_trade = "市场还没有把欧洲防务单独抬成最强风格，更多是把它当成工业、财政和安全预算的中期配置方向。"
    read = "欧洲防务/俄乌安全周期主线暂时没有明显加强。"
    impact = "如果安全溢价没有继续上升，欧洲防务相关票更偏向看各自估值和订单兑现。"
    if (dax_bid or cac_bid) and (brent_stress or vix_risk):
        score = 4
        state = "安全溢价 / security bid"
        market_trade = "市场在交易欧洲再武装、国防预算上修和防务订单可见度，而不是单纯避险。"
        read = scenario.get("notes", {}).get("escalation", read)
        impact = (
            "市场在把部分资金转向安全与防务支出受益方向，而不是全面恐慌。"
            "这通常相对利好 Airbus、Leonardo、BAE、Rheinmetall 等欧洲防务链。"
        )
    elif not brent_stress and not vix_risk:
        score = 2
        state = "降温 / cooling"
        market_trade = "市场仍承认防务长期逻辑，但短期没有继续追价，更多回到订单兑现和估值消化。"
        read = scenario.get("notes", {}).get("deescalation", read)
        impact = (
            "如果安全溢价降温，欧洲防务股更容易从主题驱动转回估值/业绩驱动；"
            "Airbus 这类更混合的工业龙头则更看整体风险偏好。"
        )

    stock_lines = geo_stock_lines(scenario, ["AIR.PA", "LDO.MI", "SAF.PA", "BA.L", "RHM.DE"])
    return score, geo_theme_block(
        scenario,
        score,
        state,
        market_trade,
        read,
        impact,
        f"DAX {pct_line(dax.five_day_pct)} over 5d, CAC {pct_line(cac.five_day_pct)} over 5d, Brent {pct_line(brent.five_day_pct)} over 5d, VIX {vix.last:.2f}.",
        stock_lines,
    )


def geopolitical_main_theme(config: Dict[str, Any], quote_cache: Dict[str, Quote]) -> str:
    framework = config.get("geopolitical_framework", {})
    if not framework.get("enabled", False):
        return ""
    max_active = int(framework.get("max_active_themes", 3))
    scenarios = framework.get("scenarios", {})
    evaluated: List[Tuple[int, int, str]] = []

    evaluators = {
        "middle_east": evaluate_middle_east_theme,
        "china_adr_semis": evaluate_china_theme,
        "europe_defense_ukraine": evaluate_europe_defense_theme,
    }

    for key, scenario in scenarios.items():
        if not scenario.get("enabled", False):
            continue
        evaluator = evaluators.get(key)
        if not evaluator:
            continue
        result = evaluator(config, quote_cache, scenario)
        if not result:
            continue
        score, text = result
        priority = int(scenario.get("priority", 99))
        evaluated.append((score, -priority, text))

    if not evaluated:
        return ""

    evaluated.sort(reverse=True)
    sections = [item[2] for item in evaluated[:max_active]]
    header = [
        f"{framework.get('title', 'Geopolitical Main Themes')}",
        f"口径 / Framework: {framework.get('focus', '')}",
    ]
    return "\n\n".join(["\n".join(header)] + sections)


def compact_geopolitical_themes(config: Dict[str, Any], quote_cache: Dict[str, Quote]) -> str:
    framework = config.get("geopolitical_framework", {})
    if not framework.get("enabled", False):
        return ""

    max_active = int(framework.get("max_active_themes", 3))
    scenarios = framework.get("scenarios", {})
    evaluators = {
        "middle_east": evaluate_middle_east_theme,
        "china_adr_semis": evaluate_china_theme,
        "europe_defense_ukraine": evaluate_europe_defense_theme,
    }
    evaluated: List[Tuple[int, int, str, str]] = []

    for key, scenario in scenarios.items():
        if not scenario.get("enabled", False):
            continue
        evaluator = evaluators.get(key)
        if not evaluator:
            continue
        result = evaluator(config, quote_cache, scenario)
        if not result:
            continue
        score, text = result
        priority = int(scenario.get("priority", 99))
        evaluated.append((score, -priority, scenario.get("label", key), text))

    if not evaluated:
        return ""

    evaluated.sort(reverse=True)
    table_rows: List[List[str]] = []
    for score, _, label, text in evaluated[:max_active]:
        picked: Dict[str, str] = {}
        for raw in text.splitlines():
            if ": " not in raw:
                continue
            key, value = raw.split(": ", 1)
            picked[key] = value
        table_rows.append(
            [
                label,
                f"{score}/5",
                trim_text(picked.get("状态 / State", ""), 32),
                trim_text(picked.get("市场在交易什么 / What the market is trading", ""), 90),
                trim_text(picked.get("预计持续 / Expected duration", ""), 32),
                trim_text(picked.get("变化观察点 / Watch for change", ""), 90),
            ]
        )
    return markdown_table(["主线", "评分", "状态", "市场在交易", "持续窗口", "失效观察"], table_rows)


def not_expanded_today(config: Dict[str, Any], quote_cache: Dict[str, Quote], expanded_rows: List[List[str]]) -> str:
    expanded_names = {row[0].split(" (")[0] for row in expanded_rows}
    buckets: Dict[str, List[str]] = {}
    for stock in [item for item in config.get("stocks", []) if not item.get("disabled")]:
        name = stock.get("name", "")
        if name in expanded_names or stock.get("position", 0) > 0:
            continue
        try:
            quote = get_quote(stock["ticker"], quote_cache)
        except Exception:
            continue
        position_label = one_year_position_label(quote)
        ratio = one_year_position_ratio(quote)
        if ratio is None:
            continue
        if ratio >= 0.65:
            buckets.setdefault("偏高 / high-zone", []).append(f"{name} ({position_label})")
        elif ratio >= 0.35:
            buckets.setdefault("中位 / mid-zone", []).append(f"{name} ({position_label})")
    if not buckets:
        return ""
    table_rows = []
    for bucket, names in buckets.items():
        table_rows.append(
            [
                bucket,
                str(len(names)),
                trim_text(", ".join(names[:8]), 140),
                "没有新的亮点或警示；不占用正文篇幅。",
            ]
        )
    return markdown_table(["位置", "数量", "代表标的", "处理"], table_rows)


def experience_reminders() -> str:
    return markdown_table(
        ["经验来源", "提醒", "动作含义"],
        [
            ["Tencent", "好公司也要买在好价格。", "等待低位或恐慌，不在情绪高点追。"],
            ["Oracle", "浮盈不兑现会回吐。", "强反弹后分批止盈，不把盈利交还市场。"],
            ["OVH", "持有时间不会自动纠错。", "基本面坏了就降级，不靠等待修复错误。"],
        ],
    )


def should_include_influencer_section(rotation_alerts: List[str], stock_rows: List[List[str]]) -> bool:
    if rotation_alerts:
        return True
    important_keywords = ["deep pullback", "低位", "re-rating", "重估", "held strength", "持仓大涨"]
    for row in stock_rows:
        text = f"{row[1]} {row[3]} {row[4]}".lower()
        if any(keyword in text for keyword in important_keywords):
            return True
    return False


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


def framework_summary(config: Dict[str, Any]) -> str:
    framework = config.get("trading_framework", {})
    if not framework.get("enabled", False):
        return ""
    return "\n".join(
        [
            "本次使用的交易框架 / Decision Framework",
            "- 主线强势：用 Minervini / David Ryan 的方法看行业主线、相对强度、成交量确认和龙头地位。",
            "- 逆向下注：用 Keith Gill 的精神做深度研究，只在基本面没死、市场可能重新定价时允许集中，但不能让单一故事失控。",
            "- 风险纪律：用 Oliver Kell 的纪律先定义失效条件和退出规则，再谈盈利目标。",
            f"- 核心原则：{framework.get('core_principle', '')}",
        ]
    )


def theme_label(stock: Dict[str, Any]) -> str:
    ticker = stock.get("ticker", "")
    name = stock.get("name", "")
    text = f"{name} {ticker}".lower()
    if any(term in text for term in ["oracle", "microsoft", "nvidia", "broadcom", "asml", "tsmc", "vertiv", "semiconductor", "ai"]):
        return "AI / cloud infrastructure"
    if any(term in text for term in ["pdd", "meituan", "tencent", "jd", "alibaba", "china", "hong kong"]):
        return "China internet / consumption recovery"
    if any(term in text for term in ["airbus", "leonardo", "thales", "bae", "lockheed", "rheinmetall", "safran"]):
        return "Aerospace / defense / industrial"
    if any(term in text for term in ["bitcoin", "btc", "ibit", "crypto"]):
        return "Crypto liquidity cycle"
    if any(term in text for term in ["totalenergies", "exxon", "siemens", "schneider", "eaton", "ge"]):
        return "Energy / electrification"
    return "General watchlist"


def framework_readout(stock: Dict[str, Any], quote: Quote, reasons: Optional[List[str]] = None) -> str:
    reasons = reasons or []
    ratio = volume_ratio(quote)
    has_volume_confirmation = ratio is not None and ratio >= 1.5 and quote.daily_pct > 0
    has_price_strength = quote.daily_pct > 0 and quote.five_day_pct is not None and quote.five_day_pct > 0
    has_contrarian_setup = any(
        key in reason
        for reason in reasons
        for key in ["低位", "low", "大跌", "selloff", "低吸", "buy zone"]
    )
    role = stock.get("rotation", {}).get("role", "watchlist")
    held = stock.get("position", 0) > 0

    if has_volume_confirmation or has_price_strength:
        trend_line = "主线/强势股镜头：有价格确认；如果它也是当前市场主线里的龙头，可提高优先级。"
    else:
        trend_line = "主线/强势股镜头：暂时缺少价格确认；按 Minervini / Ryan 逻辑，不因为便宜就自动买。"

    if has_contrarian_setup:
        contrarian_line = "逆向重估镜头：价格进入被嫌弃区域；只有红旗核查干净、基本面没死，才符合 Keith Gill 式研究下注。"
    else:
        contrarian_line = "逆向重估镜头：不是明显低位错杀信号；更像观察主线强弱，而不是左侧抄底。"

    if held and role in {"aggressive", "speculative"}:
        sizing_line = "仓位纪律：这是高波动持仓，新增只能小比例分批，不能让单一故事决定整个账户。"
    elif held:
        sizing_line = "仓位纪律：已有持仓，优先考虑轮动后的组合比例，而不是单票情绪。"
    else:
        sizing_line = "仓位纪律：未持有标的只作为候选池；首次试仓应小，不用一次买满。"

    stop_line = "退出纪律：先写清楚失效条件；如果财报、订单、现金流、监管或信用逻辑被证伪，按 Oliver Kell 纪律退出，不用希望扛单。"
    return "\n".join(
        [
            f"主题归类：{theme_label(stock)}",
            f"- {trend_line}",
            f"- {contrarian_line}",
            f"- {sizing_line}",
            f"- {stop_line}",
        ]
    )


def flow_readout(quote: Quote) -> str:
    """Classify observable price-volume behavior without pretending it proves fundamentals."""
    ratio = volume_ratio(quote)
    if ratio is None:
        return "量价证据不足：只知道价格变化，无法判断资金是否确认。"
    if quote.daily_pct <= -3 and ratio >= 1.8:
        return f"卖压仍强：下跌 {quote.daily_pct:+.1f}%，量 {ratio:.1f}x；先等卖盘衰竭。"
    if quote.daily_pct < 0 and ratio <= 0.8:
        return f"卖压可能减弱：下跌但量仅 {ratio:.1f}x；需等不创新低确认。"
    if quote.daily_pct > 1 and ratio >= 1.5:
        return f"买方开始确认：上涨且量 {ratio:.1f}x；仍需后续站稳验证。"
    return f"资金流未确认：今日 {quote.daily_pct:+.1f}%，量 {ratio:.1f}x；不单凭此下单。"


def default_capital_profile(stock: Dict[str, Any]) -> str:
    theme = theme_label(stock)
    if theme == "AI / cloud infrastructure":
        return "picks_and_shovels"
    if theme == "China internet / consumption recovery":
        return "cash_compounder"
    if "bank" in str(stock.get("notes", "")).lower() or stock.get("ticker") in {"BNP.PA", "GLE.PA", "UBS", "BAC"}:
        return "bank_credit_cycle"
    return "cyclical_repair"


def research_profile_for_stock(stock: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """Return the business-model-specific research lens for a stock, not a generic sector label."""
    research = config.get("advisor_research", {})
    for profile_name, profile in research.get("profiles", {}).items():
        if stock.get("ticker") in profile.get("tickers", []):
            result = dict(profile)
            result["name"] = profile_name
            result.update(stock.get("advisor_profile", {}))
            return result

    framework = config.get("trading_framework", {})
    profile_name = stock.get("advisor_profile", {}).get("capital_profile", default_capital_profile(stock))
    result = {
        "name": profile_name,
        "mispricing": "未配置特定错误定价假设；先明确市场可能错在哪里，再考虑交易。",
        "fundamentals": "核对前瞻收入、EPS、FCF、利润率、订单和市场份额。",
        "capital": framework.get("default_capital_profiles", {}).get(profile_name, "人工核对CapEx、债务、利息和稀释。"),
        "credit": "核对利率、再融资、评级、债券利差及现金流韧性。",
        "valuation": "一年区间位置不是内在价值；用前瞻盈利和FCF验证。",
        "catalysts": framework.get("default_catalysts", []),
    }
    result.update(stock.get("advisor_profile", {}))
    return result


def joined_profile_items(value: Any, limit: int = 3) -> str:
    if isinstance(value, list):
        return "；".join(str(item) for item in value[:limit])
    return str(value or "未配置；没有可验证催化剂时，不因低位而买入。")


def investment_memo_table(stock: Dict[str, Any], quote: Quote, config: Dict[str, Any]) -> str:
    """Produce the fixed eight-gate review only for a real decision-level signal."""
    profile = research_profile_for_stock(stock, config)
    position_label = "已有持仓" if stock.get("position", 0) > 0 else "观察名单，首次仓位应小"
    role = stock.get("rotation", {}).get("role", "watchlist")
    if role in {"aggressive", "speculative"}:
        position_label += "；高波动/凸性仓，不能让单一逻辑主导账户。"
    valuation_read = profile.get("valuation_question", profile.get("valuation"))
    catalyst_read = joined_profile_items(profile.get("catalysts"))
    red_flag_read = red_flag_news_check(stock)
    invalidation = first_guardrails(stock, limit=3)
    rows = [
        ["基本面 / Fundamentals", trim_text(profile.get("mispricing", "") + " " + profile.get("fundamentals", ""), 175), "自动红旗筛查：" + trim_text(plain_text(red_flag_read), 90)],
        ["资本结构 / Capital", trim_text(profile.get("capital", ""), 175), "增长必须覆盖新增CapEx、融资成本和潜在稀释；看增量ROIC，不只看收入。"],
        ["资金流 / Flow", flow_readout(quote), "寻找坏消息不再创新低、二次回踩缩量，或上涨日放量。"],
        ["信用 / Credit", trim_text(profile.get("credit", ""), 175), "信用市场通常比股价更早暴露风险；本 bot 不把未发现新闻误判为信用安全。"],
        ["估值 / Valuation", f"{one_year_position_label(quote)}。{valuation_read}", "技术低位不等于内在价值低；避免只看过去PE。"],
        ["催化剂 / Catalyst", trim_text(catalyst_read, 145), "至少有一个可观察的验证节点，才从研究名单升级为交易候选。"],
        ["失效条件 / Invalidation", trim_text(plain_text(invalidation), 145), "任何一项被事实证伪，停止摊低成本，重新评估或退出。"],
        ["仓位 / Position", position_label, "按预期收益 x 概率相对于最大下跌决定仓位；核心仓、凸性仓和现金分开。"],
    ]
    return markdown_table(["八栏 / Gate", "当前判断 / Read", "CEO复核 / Required decision"], rows)


def decision_memos_for_rows(
    rows: List[List[str]], config: Dict[str, Any], quote_cache: Dict[str, Quote], max_names: int = 2
) -> str:
    decision_terms = ("deep pullback", "接近", "低位", "re-rating", "重估", "held strength", "持仓大涨", "5d strength", "连续走强")
    selected: List[Dict[str, Any]] = []
    for row in top_signal_rows(rows, max_rows=12):
        row_text = f"{row[1]} {row[3]}".lower()
        if not any(term in row_text for term in decision_terms):
            continue
        matched = next((stock for stock in config.get("stocks", []) if stock.get("ticker") in row[0]), None)
        if matched and matched not in selected:
            selected.append(matched)
        if len(selected) >= max_names:
            break

    # A rotation target can be intentionally omitted from routine stock rows.
    # It still deserves the same pre-trade review when the engine proposes funding it.
    if len(selected) < max_names:
        for stock in config.get("stocks", []):
            if stock.get("ticker") in ROTATION_TARGET_TICKERS and stock not in selected:
                selected.append(stock)
            if len(selected) >= max_names:
                break

    if not selected:
        return ""
    sections = []
    for stock in selected:
        quote = get_quote(stock["ticker"], quote_cache)
        sections.append(f"**{stock['name']} ({stock['ticker']})**\n{investment_memo_table(stock, quote, config)}")
    return "\n\n".join(sections)


def indicator_quote(config: Dict[str, Any], quote_cache: Dict[str, Quote], name: str) -> Optional[Quote]:
    indicator = next((item for item in config.get("market_indicators", []) if item.get("name") == name), None)
    if not indicator:
        return None
    try:
        return get_quote(indicator["ticker"], quote_cache)
    except Exception:
        return None


def portfolio_regime_table(config: Dict[str, Any], quote_cache: Dict[str, Quote]) -> str:
    """Summarize cross-asset regime signals without claiming that market proxies are causation."""
    tnx = indicator_quote(config, quote_cache, "US 10Y Yield")
    vix = indicator_quote(config, quote_cache, "VIX")
    hyg = indicator_quote(config, quote_cache, "High Yield Credit ETF")
    lqd = indicator_quote(config, quote_cache, "Investment Grade Credit ETF")
    qqq = indicator_quote(config, quote_cache, "Nasdaq 100 ETF")
    smh = indicator_quote(config, quote_cache, "Semiconductor ETF")
    hsi = indicator_quote(config, quote_cache, "Hang Seng Index")

    rows: List[List[str]] = []
    if tnx and vix:
        rate_state = "估值压力偏高" if tnx.last >= 45 or vix.last >= 25 else "未见极端压力"
        rows.append(["利率/风险偏好", rate_state, f"10Y {tnx.last / 10:.2f}%；VIX {vix.last:.1f}", "高利率首先压制长久期、高CapEx和高估值资产；不是对单股的买卖指令。"])
    if hyg and lqd and hyg.five_day_pct is not None and lqd.five_day_pct is not None:
        spread = hyg.five_day_pct - lqd.five_day_pct
        credit_state = "信用风险偏紧" if hyg.five_day_pct <= -2.0 and spread <= -1.0 else "信用代理未见明显恶化"
        rows.append(["信用条件", credit_state, f"HYG 5日 {hyg.five_day_pct:+.1f}%；相对LQD {spread:+.1f}pct", "HYG弱于LQD说明高收益信用承压；它是市场代理，不替代公司评级或债券利差。"])
    if qqq and smh and qqq.five_day_pct is not None and smh.five_day_pct is not None:
        ai_state = "AI硬件风险偏好走弱" if smh.five_day_pct <= -5.0 else "AI风险偏好未见急剧撤退"
        rows.append(["AI资本周期", ai_state, f"QQQ 5日 {qqq.five_day_pct:+.1f}%；SMH {smh.five_day_pct:+.1f}", "价格代理只反映资金偏好；真实资本周期仍需财报中的客户CapEx、订单和利用率验证。"])
    if hsi and hsi.five_day_pct is not None:
        china_state = "中概风险偏好偏弱" if hsi.five_day_pct <= -3.0 else "中概风险偏好稳定/改善"
        rows.append(["中国风险溢价", china_state, f"恒指 5日 {hsi.five_day_pct:+.1f}%", "中国平台重估还取决于消费、竞争、政策和跨境风险，不只看指数。"])
    if not rows:
        return ""
    return markdown_table(["主线", "当前状态", "市场代理", "投资含义"], rows)


def trailing_return(quote: Quote, trading_days: int) -> Optional[float]:
    try:
        close = quote.close_history.dropna()
        if len(close) <= trading_days:
            return None
        return (float(close.iloc[-1]) / float(close.iloc[-trading_days - 1]) - 1) * 100
    except Exception:
        return None


def market_rotation_scan(config: Dict[str, Any], quote_cache: Dict[str, Quote]) -> Tuple[str, bool]:
    """Find early research candidates where sector flow and stock price location have not fully detached."""
    settings = config.get("market_scan", {})
    if not settings.get("enabled", False):
        return "", False

    try:
        benchmark = get_quote(settings.get("benchmark_ticker", "SPY"), quote_cache)
    except Exception as exc:
        return f"赛道扫描不可用 / Market scan unavailable: {exc}", False

    benchmark_20d = trailing_return(benchmark, 20)
    if benchmark_20d is None:
        return "赛道扫描数据不足 / Market scan lacks benchmark history.", False

    confirmed_threshold = float(settings.get("min_relative_strength_20d_pct", 1.5))
    early_threshold = float(settings.get("early_relative_strength_20d_pct", 0.5))
    max_position = float(settings.get("candidate_max_1y_position", 0.65))
    min_5d = float(settings.get("candidate_min_5d_pct", -2.0))
    theme_rows: List[Tuple[float, List[str]]] = []
    candidate_rows: List[List[str]] = []

    for track in settings.get("tracks", []):
        try:
            proxy = get_quote(track["proxy_ticker"], quote_cache)
            confirmer = get_quote(track["confirmer_ticker"], quote_cache)
        except Exception:
            continue
        proxy_20d = trailing_return(proxy, 20)
        confirmer_20d = trailing_return(confirmer, 20)
        if proxy_20d is None or confirmer_20d is None:
            continue
        relative = proxy_20d - benchmark_20d
        confirmer_relative = confirmer_20d - benchmark_20d
        confirmed = relative >= confirmed_threshold and confirmer_relative >= -1.0
        emerging = relative >= early_threshold and confirmer_relative >= -2.0
        if confirmed:
            stage = "资金确认 / confirmed flow"
        elif emerging:
            stage = "早期轮动 / early rotation"
        else:
            stage = "未确认 / not confirmed"

        eligible: List[Tuple[str, Quote, float]] = []
        for ticker in track.get("candidates", []):
            try:
                candidate = get_quote(ticker, quote_cache)
            except Exception:
                continue
            price_position = one_year_position_ratio(candidate)
            if price_position is None or candidate.five_day_pct is None:
                continue
            # A leader can be strong while a candidate is still in a normal valuation/range zone.
            if emerging and price_position <= max_position and candidate.five_day_pct >= min_5d:
                eligible.append((ticker, candidate, price_position))

        opportunity = "无合格低位候选 / no early candidate"
        if eligible:
            opportunity = f"{len(eligible)} 个未过热候选 / {len(eligible)} non-extended candidate(s)"
        theme_rows.append(
            (
                relative,
                [
                    track["name"],
                    stage,
                    f"20日相对SPY {relative:+.1f}pct；确认器 {confirmer_relative:+.1f}pct",
                    opportunity,
                ],
            )
        )
        for ticker, candidate, price_position in eligible:
            readiness = "可研究，不追买" if confirmed else "提前研究，等待资金确认"
            candidate_rows.append(
                [
                    track["name"],
                    ticker,
                    f"{candidate.last:.2f}；5日 {candidate.five_day_pct:+.1f}%；一年位置 {price_position:.0%}",
                    readiness,
                    trim_text(track.get("required_checks", "财报、估值和红旗核查"), 105),
                ]
            )

    if not theme_rows:
        return "赛道扫描暂无足够数据 / Market scan has insufficient data.", False

    max_themes = int(settings.get("max_themes_in_email", 3))
    theme_rows.sort(key=lambda item: item[0], reverse=True)
    lines = [
        "原则：赛道强势不等于立即买入。只有资金开始流入、候选未明显过热、且后续基本面核查通过，才进入研究优先级。",
        markdown_table(
            ["赛道 / Theme", "阶段 / Stage", "资金证据 / Flow", "低位候选 / Early candidates"],
            [row for _, row in theme_rows[:max_themes]],
        ),
    ]
    if candidate_rows:
        lines.extend(
            [
                "",
                markdown_table(
                    ["赛道", "候选", "价格位置", "结论", "下单前核对"],
                    candidate_rows[:6],
                ),
            ]
        )
    else:
        lines.append("结论：市场可能正在交易部分新赛道，但没有同时满足‘相对走强 + 未过热’的候选；不为了轮动而轮动。")
    return "\n".join(lines), bool(candidate_rows)


def one_year_drawdown_pct(quote: Quote) -> Optional[float]:
    high = history_window_max(quote, 1)
    if high is None or high <= 0:
        return None
    return (quote.last / high - 1) * 100


def elite_franchise_reset_watch(config: Dict[str, Any], quote_cache: Dict[str, Quote]) -> Tuple[str, bool]:
    """Apply a stricter, staged process to deep resets in exceptional large-cap franchises."""
    settings = config.get("elite_franchise_reset_watch", {})
    if not settings.get("enabled", False):
        return "", False

    stock_map = {stock.get("ticker"): stock for stock in config.get("stocks", [])}
    ticker_groups: Dict[str, Tuple[str, str]] = {}
    for group in settings.get("groups", []):
        for ticker in group.get("tickers", []):
            if ticker not in ticker_groups:
                ticker_groups[ticker] = (group.get("name", "旗舰资产"), group.get("required_checks", "财报与基本面核查"))

    min_drawdown = float(settings.get("min_drawdown_from_1y_high_pct", -20.0))
    panic_daily = float(settings.get("panic_daily_drop_pct", -4.0))
    panic_volume = float(settings.get("panic_volume_ratio", 1.8))
    quiet_volume = float(settings.get("seller_exhaustion_volume_ratio", 0.8))
    buyer_daily = float(settings.get("buyer_confirmation_daily_pct", 1.0))
    buyer_volume = float(settings.get("buyer_confirmation_volume_ratio", 1.3))
    rows: List[List[str]] = []

    for ticker, (group_name, required_checks) in ticker_groups.items():
        stock = stock_map.get(ticker)
        if not stock or stock.get("disabled"):
            continue
        try:
            quote = get_quote(ticker, quote_cache)
        except Exception:
            continue
        drawdown = one_year_drawdown_pct(quote)
        if drawdown is None or drawdown > min_drawdown:
            continue
        ratio = volume_ratio(quote)
        ratio_text = "n/a" if ratio is None else f"{ratio:.1f}x"
        if quote.daily_pct <= panic_daily and ratio is not None and ratio >= panic_volume:
            phase = "第一轮主动抛售 / active liquidation"
            action = "不加仓；这是风险释放，不是确认底部。等待卖压衰竭。"
        elif quote.daily_pct >= buyer_daily and ratio is not None and ratio >= buyer_volume:
            phase = "买方开始确认 / buyer confirmation"
            action = "红旗核查通过后，可考虑第一笔很小的分批仓位；不要一次买满。"
        elif quote.daily_pct <= 0 and ratio is not None and ratio <= quiet_volume:
            phase = "卖压衰竭观察 / seller exhaustion watch"
            action = "进入重点研究；等不创新低或放量收复关键价位，再决定是否分批。"
        else:
            phase = "深回撤，尚未确认 / deep reset, unconfirmed"
            action = "只做基本面与信用核查；等待量价给出下一步证据。"
        red_flag = trim_text(plain_text(red_flag_news_check(stock)), 95)
        rows.append(
            [
                group_name,
                f"{stock['name']} ({ticker})",
                f"距一年高点 {drawdown:+.1f}%；今日 {quote.daily_pct:+.1f}%；量 {ratio_text}",
                phase,
                trim_text(required_checks, 115) + "<br>新闻筛查：" + red_flag,
                action,
            ]
        )

    if not rows:
        return "", False
    rows = rows[: int(settings.get("max_candidates_per_email", 4))]
    private_note = settings.get("private_company_manual_watch", {}).get("note", "")
    lines = [
        settings.get("principle", "旗舰资产大跌是研究机会，不是自动加仓理由。"),
        markdown_table(["类别", "标的", "回撤/量价", "阶段", "必须核对", "建议"], rows),
    ]
    if private_note:
        lines.extend(["", f"SpaceX人工观察：{private_note}"])
    return "\n".join(lines), True


def market_mover_watch(config: Dict[str, Any], quote_cache: Dict[str, Quote]) -> str:
    """Rank a curated, liquid global leadership universe instead of noisy all-market penny-stock movers."""
    settings = config.get("market_mover_watch", {})
    if not settings.get("enabled", False):
        return ""

    configured_stocks = {stock.get("ticker"): stock for stock in config.get("stocks", [])}
    movers: List[Tuple[Dict[str, Any], Quote]] = []
    for asset in settings.get("universe", []):
        if settings.get("external_only", False) and asset.get("ticker") in configured_stocks:
            continue
        try:
            movers.append((asset, get_quote(asset["ticker"], quote_cache)))
        except Exception:
            continue
    if not movers:
        return "全球旗舰资产榜数据不足 / Global mover board has insufficient data."

    def table_rows(items: List[Tuple[Dict[str, Any], Quote]]) -> List[List[str]]:
        rows: List[List[str]] = []
        for asset, quote in items:
            existing = configured_stocks.get(asset["ticker"])
            if settings.get("external_only", False):
                coverage = "外部候选 / external"
            elif existing and existing.get("position", 0) > 0:
                coverage = "持仓 / held"
            elif existing:
                coverage = "已有观察 / already tracked"
            else:
                coverage = "外部候选 / external"
            rows.append(
                [
                    f"{asset['name']} ({asset['ticker']})",
                    f"{asset.get('region', '')} · {asset.get('style', '')}",
                    f"今日 {quote.daily_pct:+.1f}%；5日 {pct_line(quote.five_day_pct)}",
                    f"量 {volume_ratio(quote):.1f}x" if volume_ratio(quote) is not None else "量 n/a",
                    one_year_position_label(quote),
                    coverage,
                ]
            )
        return rows

    count = int(settings.get("max_rows", 10))
    gainers = sorted(movers, key=lambda item: item[1].daily_pct, reverse=True)[:count]
    losers = sorted(movers, key=lambda item: item[1].daily_pct)[:count]
    external_movers = [item for item in movers if item[0]["ticker"] not in configured_stocks]
    external_count = int(settings.get("external_max_rows", 5))
    external_spotlight = sorted(external_movers, key=lambda item: abs(item[1].daily_pct), reverse=True)[:external_count]
    lines = [
        settings.get("note", "精选资产排行榜，不是全交易所原始涨跌榜。"),
        "",
        "综合涨幅前十 / Overall Top 10 Gainers",
        markdown_table(["资产", "地区/类型", "涨跌", "成交量", "一年位置", "覆盖"], table_rows(gainers)),
        "",
        "综合跌幅前十 / Overall Top 10 Losers",
        markdown_table(["资产", "地区/类型", "涨跌", "成交量", "一年位置", "覆盖"], table_rows(losers)),
    ]
    if external_spotlight:
        lines.extend(
            [
                "",
                "名单外机会雷达 / External Opportunity Radar",
                markdown_table(
                    ["资产", "地区/类型", "涨跌", "成交量", "一年位置", "结论"],
                    [
                        row[:-1]
                        + [
                            "外部候选：若大跌，进入基本面/信用/量价复核；若大涨，先判断是否已过热。"
                        ]
                        for row in table_rows(external_spotlight)
                    ],
                ),
            ]
        )
    lines.extend(
        [
            "",
            "用法：榜单是发现线索，不是交易指令。跌幅榜只进入基本面、信用和量价复核；涨幅榜用于识别赛道领导力与可能的止盈/不追高区。",
        ]
    )
    return "\n".join(lines)


def history_window_min(quote: Quote, years: int) -> Optional[float]:
    trading_days = 252 * years
    close = quote.close_history.dropna()
    if len(close) < 30:
        return None
    window = close.iloc[-trading_days - 1 : -1] if len(close) > trading_days else close.iloc[:-1]
    if window.empty:
        return None
    return float(window.min())


def history_window_max(quote: Quote, years: int) -> Optional[float]:
    trading_days = 252 * years
    close = quote.close_history.dropna()
    if len(close) < 30:
        return None
    window = close.iloc[-trading_days - 1 : -1] if len(close) > trading_days else close.iloc[:-1]
    if window.empty:
        return None
    return float(window.max())


def one_year_position_ratio(quote: Quote) -> Optional[float]:
    low_1y = history_window_min(quote, 1)
    high_1y = history_window_max(quote, 1)
    if low_1y is None or high_1y is None or high_1y <= low_1y:
        return None
    return (quote.last - low_1y) / (high_1y - low_1y)


def one_year_position_label(quote: Quote) -> str:
    ratio = one_year_position_ratio(quote)
    if ratio is None:
        return "一年区间位置不明 / 1Y position unavailable"
    if ratio >= 0.85:
        return "接近一年高位 / near 1Y high"
    if ratio >= 0.65:
        return "一年区间中上部 / upper half of 1Y range"
    if ratio >= 0.35:
        return "一年区间中部 / middle of 1Y range"
    if ratio >= 0.15:
        return "接近一年低位上方 / above the 1Y low zone"
    return "接近一年低位 / near 1Y low"


def should_expand_overhang_watch(stock: Dict[str, Any], quote: Quote) -> bool:
    if stock.get("position", 0) > 0:
        return True
    ratio = one_year_position_ratio(quote)
    if ratio is None:
        return True
    return ratio <= 0.55


def auto_overhang_levels(quote: Quote) -> Optional[Dict[str, float]]:
    low_1y = history_window_min(quote, 1)
    high_1y = history_window_max(quote, 1)
    if low_1y is None or high_1y is None or high_1y <= low_1y:
        return None

    span = high_1y - low_1y
    return {
        "deep_value_low": low_1y,
        "deep_value_high": low_1y + span * 0.16,
        "support_low": low_1y + span * 0.16,
        "support_high": low_1y + span * 0.32,
        "current_battle_zone_low": low_1y + span * 0.32,
        "current_battle_zone_high": low_1y + span * 0.52,
        "near_resistance_low": low_1y + span * 0.52,
        "near_resistance_high": low_1y + span * 0.68,
        "heavy_overhang_low": low_1y + span * 0.68,
        "heavy_overhang_high": high_1y,
    }


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


def stock_signal_rows(stock: Dict[str, Any], quote: Quote, global_rules: Dict[str, Any]) -> List[List[str]]:
    rows: List[List[str]] = []
    name = stock["name"]
    is_held = stock.get("position", 0) > 0

    if stock.get("daily_volume_watch", {}).get("enabled", False):
        read, decision = daily_volume_watch_readout(stock, quote)
        rows.append(
            [
                f"{name} ({stock['ticker']})",
                "每日量价观察 / daily price-volume watch",
                quote_metrics_inline(quote),
                read,
                decision,
            ]
        )

    if stock.get("overhang_watch", {}).get("enabled", True) and should_expand_overhang_watch(stock, quote):
        read, decision = overhang_watch_readout(stock, quote)
        rows.append(
            [
                f"{name} ({stock['ticker']})",
                "套牢盘压力 / holder overhang",
                quote_metrics_inline(quote),
                read,
                decision,
            ]
        )

    entry_row = entry_alert_row(stock, quote)
    if entry_row:
        rows.append(entry_row)

    if is_held and quote.daily_pct >= global_rules["stock_big_up_daily_pct"]:
        rows.append(
            [
                f"{name} ({stock['ticker']})",
                "持仓大涨 / held strength",
                quote_metrics_inline(quote),
                f"单日上涨 {quote.daily_pct:+.2f}%，符合你的强势日检查纪律",
                "不追高；检查是否减一点、锁利润，或换入更低位标的",
            ]
        )

    if is_held and quote.five_day_pct is not None and quote.five_day_pct >= global_rules["stock_big_up_5d_pct"]:
        rows.append(
            [
                f"{name} ({stock['ticker']})",
                "持仓连续走强 / 5d strength",
                quote_metrics_inline(quote),
                f"5个交易日上涨 {quote.five_day_pct:+.2f}%，不是单日随机波动",
                "评估是否把一小部分利润轮动出去",
            ]
        )

    ratio = volume_ratio(quote)
    rerating_volume = ratio is not None and ratio >= global_rules["confirmed_rerating_volume_ratio_min"]
    rerating_daily = quote.daily_pct >= global_rules["confirmed_rerating_daily_pct"]
    rerating_5d = quote.five_day_pct is not None and quote.five_day_pct >= global_rules["confirmed_rerating_5d_pct"]
    if rerating_volume and (rerating_daily or rerating_5d):
        rows.append(
            [
                f"{name} ({stock['ticker']})",
                "市场确认重估 / re-rating",
                quote_metrics_inline(quote),
                f"价格强势且成交量约 {ratio:.1f}x，说明有真实资金参与",
                "提高研究优先级；确认财报、订单、指引或监管变化是否支撑",
            ]
        )

    tolerance = 1 + global_rules["historic_low_tolerance_pct"] / 100
    for years in global_rules.get("historic_low_lookback_years", []):
        low = history_window_min(quote, int(years))
        if low is not None and quote.last <= low * tolerance:
            rows.append(
                [
                    f"{name} ({stock['ticker']})",
                    f"接近{years}年低位 / near {years}Y low",
                    quote_metrics_inline(quote),
                    f"当前 {quote.last:.2f}，{years}年低点约 {low:.2f}",
                    "进入投研优先区；先判断是错杀还是价值陷阱，再考虑分批",
                ]
            )

    return rows


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
    extreme_fear_triggered = False

    extreme_fear = rules.get("extreme_fear_below")
    if extreme_fear is not None and sentiment.value <= extreme_fear:
        extreme_fear_triggered = True
        alerts.append(
            bilingual(
                (
                    f"{name} 进入极度恐慌区：当前 {sentiment.value} ({sentiment.classification})，触发线 <= {extreme_fear}。\n"
                    f"这是你的硬触发通知：市场情绪已经进入可以认真研究低位买入的区域。\n"
                    f"简单说：加密市场情绪很差，很多人在逃离风险。\n"
                    f"CEO 决策点：如果 BTC ETF 没有持续流出、监管和网络安全没有新雷，可以开始认真研究小仓分批。"
                ),
                (
                    f"{name} entered extreme-fear territory: current {sentiment.value} ({sentiment.classification}), threshold <= {extreme_fear}.\n"
                    f"This is your hard notification trigger: sentiment is weak enough to seriously review staged low-entry opportunities.\n"
                    f"In plain English, crypto sentiment is very weak. If ETF flows, regulation, and network security remain acceptable, this can be a staged-entry research signal."
                ),
            )
        )

    fear_watch = rules.get("fear_watch_below")
    if fear_watch is not None and sentiment.value <= fear_watch and not extreme_fear_triggered:
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
    ROTATION_TARGET_TICKERS.clear()
    ROTATION_PAIR_CONTEXT.clear()
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

    pair_rows: List[List[str]] = []
    max_pairs = int(engine.get("max_pairs_per_email", 5))
    for from_stock, from_quote, from_reasons in sell_candidates:
        for to_stock, to_quote, to_reasons in buy_candidates:
            if from_stock["ticker"] == to_stock["ticker"]:
                continue
            if len(pair_rows) >= max_pairs:
                break
            ROTATION_TARGET_TICKERS.add(to_stock["ticker"])
            pair_index = len(pair_rows) + 1
            ROTATION_PAIR_CONTEXT.append(
                (from_stock["name"], from_stock["ticker"], to_stock["name"], to_stock["ticker"])
            )
            target_is_held = "持仓加仓 / held add" if to_stock.get("position", 0) > 0 else "观察买入 / watchlist buy"
            pair_rows.append(
                [
                    f"{from_stock['name']} -> {to_stock['name']}",
                    target_is_held,
                    trim_text(quote_metrics_inline(to_quote), 58),
                    trim_text(plain_text("; ".join(to_reasons)), 90),
                    trim_text(plain_text(red_flag_news_check(to_stock)), 90),
                    trim_text(plain_text(influencer_rotation_check(from_stock, to_stock, pair_index)), 70),
                    "人工复核；基本面没破才小比例/分批。",
                ]
            )
        if len(pair_rows) >= max_pairs:
            break

    if not pair_rows:
        return []

    funding_rows = [
        [
            f"{stock['name']} ({stock['ticker']})",
            trim_text(quote_metrics_inline(quote), 58),
            trim_text(plain_text("; ".join(reasons)), 90),
        ]
        for stock, quote, reasons in sell_candidates
    ]
    lines = [
        f"结论：出现 {len(pair_rows)} 个轮动候选；这是研究清单，不是买入指令。",
        "",
        "资金来源 / Funding source",
        markdown_table(["标的", "数据", "卖出理由"], funding_rows),
        "",
        "候选去向 / Rotation candidates",
        markdown_table(["组合", "类型", "目标数据", "机会", "红旗", "大V", "动作"], pair_rows),
        "",
        "口径：红旗和大V只做证据提示；未发现不等于风险不存在。",
    ]
    return ["\n".join(lines)]


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
    alerts.append(
        markdown_table(
            ["组合", "资金来源", "目标", "红旗", "动作"],
            [
                [
                    f"{signal['from_name']} -> {signal['to_name']}",
                    trim_text(quote_snapshot(from_quote), 70),
                    trim_text(quote_snapshot(to_quote), 70),
                    trim_text("; ".join(red_flags) or "无特别红旗", 90),
                    trim_text(signal.get("action", "Review this rotation manually."), 90),
                ]
            ],
        )
    )
    return alerts


def build_report(config: Dict[str, Any]) -> Tuple[str, bool]:
    DATA_QUALITY_LOG.clear()
    ROTATION_TARGET_TICKERS.clear()
    NEWS_CHECK_CACHE.clear()
    INFLUENCER_CHECK_CACHE.clear()
    ROTATION_PAIR_CONTEXT.clear()
    lines: List[str] = []
    triggered = False
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    lines.append(f"CEO 投资简报 / CEO Investment Brief - {now}")
    lines.append("")
    lines.append("汇报口径：只汇报可决策信号；同类信息合并，避免重复。")
    lines.append("Standard: decision-level signals only; similar information is grouped to avoid repetition.")
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

    market_scan_section = ""
    try:
        market_scan_section, market_scan_triggered = market_rotation_scan(config, quote_cache)
        triggered = triggered or market_scan_triggered
    except Exception as exc:
        market_scan_section = bilingual(
            f"赛道轮动扫描失败：{exc}",
            f"Market rotation scan failed: {exc}",
        )

    elite_reset_section = ""
    try:
        elite_reset_section, elite_reset_triggered = elite_franchise_reset_watch(config, quote_cache)
        triggered = triggered or elite_reset_triggered
    except Exception as exc:
        elite_reset_section = bilingual(
            f"旗舰资产深回撤扫描失败：{exc}",
            f"Elite franchise reset watch failed: {exc}",
        )

    market_mover_section = market_mover_watch(config, quote_cache)
    if config.get("market_mover_watch", {}).get("send_daily_email", False):
        triggered = True

    stock_rows: List[List[str]] = []
    stock_errors: List[str] = []
    for stock in [item for item in config.get("stocks", []) if not item.get("disabled")]:
        try:
            quote = get_quote(stock["ticker"], quote_cache)
            if stock["ticker"] in ROTATION_TARGET_TICKERS and not stock.get("daily_volume_watch", {}).get("enabled", False):
                continue
            rows = stock_signal_rows(stock, quote, global_rules)
            triggered = triggered or bool(rows)
            stock_rows.extend(rows)
        except Exception as exc:
            triggered = True
            stock_errors.append(
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

    influencer_rows: List[List[str]] = []
    if config.get("influencer_watch", {}).get("enabled", False) and should_include_influencer_section(rotation_alerts, stock_rows):
        try:
            influencer_rows = influencer_latest_rows(config)
        except Exception as exc:
            influencer_rows = [
                [
                    "大V雷达 / Influencer radar",
                    "数据获取失败 / fetch failed",
                    "",
                    str(exc),
                    "本次忽略，不影响行情信号 / ignored this run",
                ]
            ]

    has_any_signal = bool(rotation_alerts or stock_rows or stock_errors or indicator_alerts or market_scan_section or elite_reset_section or market_mover_section)

    lines.append("一、今日结论 / Today's Conclusion")
    lines.append("--------------------------------")
    lines.append(executive_summary(rotation_alerts, stock_rows, indicator_alerts))
    lines.append("")

    if market_mover_section:
        lines.append("二、全球旗舰资产异动榜 / Global Leadership Movers")
        lines.append("-----------------------------------------------------")
        lines.append(market_mover_section)
        lines.append("")

    if rotation_alerts or stock_rows:
        lines.append("三、可执行信号 / Actionable Signals")
        lines.append("----------------------------------")
        if stock_rows:
            lines.append(concise_signal_briefs(stock_rows, max_rows=4))
            lines.append("")
        if rotation_alerts:
            lines.append("重点调仓候选 / Rotation Candidates")
            lines.append("\n\n".join(rotation_alerts[:2]))
        lines.append("")

    if elite_reset_section:
        lines.append("四、旗舰资产深回撤 / Elite Franchise Deep-Reset Watch")
        lines.append("---------------------------------------------------------")
        lines.append(elite_reset_section)
        lines.append("")

    if market_scan_section:
        lines.append("五、赛道轮动扫描 / Market Rotation Scan")
        lines.append("------------------------------------------")
        lines.append(market_scan_section)
        lines.append("")

    decision_memos = decision_memos_for_rows(stock_rows, config, quote_cache)
    if decision_memos:
        lines.append("六、交易前八栏复核 / Pre-Trade Eight-Gate Review")
        lines.append("---------------------------------------------------")
        lines.append("只对真正触发的机会/强势信号展开。自动数据用于筛查，不替代财报、估值与信用人工核验。")
        lines.append("Expanded only for decision-level signals. Automated data screens for risk; it does not replace earnings, valuation, or credit verification.")
        lines.append("")
        lines.append(decision_memos)
        lines.append("")

    supplemental_stock_rows = top_signal_rows(stock_rows, max_rows=10)[4:] if stock_rows else []
    if supplemental_stock_rows or stock_errors:
        lines.append("七、补充观察 / Additional Watch")
        lines.append("-------------------------------")
        if supplemental_stock_rows:
            lines.append(tight_portfolio_diagnosis(supplemental_stock_rows, max_names=6))
        if stock_errors:
            lines.append("")
            lines.append("数据异常 / Data Exceptions")
            lines.append(
                markdown_table(
                    ["事项", "说明"],
                    [["数据异常", trim_text(plain_text(error), 120)] for error in stock_errors[:6]],
                )
            )
        lines.append("")

    geo_section = compact_geopolitical_themes(config, quote_cache)
    regime_section = portfolio_regime_table(config, quote_cache) if (rotation_alerts or stock_rows) else ""

    if indicator_alerts or geo_section or regime_section:
        lines.append("八、市场温度 / Market Temperature")
        lines.append("--------------------------------")
        if regime_section:
            lines.append(regime_section)
            lines.append("")
        if geo_section:
            lines.append(geo_section)
            lines.append("")
        if indicator_alerts:
            indicator_rows = [
                [trim_text(plain_text(alert).split()[0], 36), trim_text(plain_text(alert), 150)]
                for alert in indicator_alerts[:3]
            ]
            lines.append(markdown_table(["指标", "核心信号"], indicator_rows))
        lines.append("")

    omitted_section = not_expanded_today(config, quote_cache, stock_rows)
    if omitted_section:
        lines.append("九、今天不单独展开 / Not Expanded Today")
        lines.append("-------------------------------------")
        lines.append(omitted_section)
        lines.append("")

    if influencer_rows:
        lines.append("十、高手雷达 / Influencer Radar")
        lines.append("--------------------------------")
        lines.append(influencer_blocks(influencer_rows[:3]))
        lines.append("")

    lines.append("十一、经验提醒 / Experience Reminders")
    lines.append("-----------------------------------")
    lines.append(experience_reminders())
    lines.append("")

    if not has_any_signal:
        lines.append("结论：今天没有达到决策级别的新信号，继续观察，不做动作。")
        lines.append("Conclusion: no decision-level signal today; keep watching and do not force a trade.")

    quality_report = fetch_quality_report()
    if quality_report:
        lines.append("")
        lines.append(quality_report)

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
        subject = "CEO投资简报 / CEO Investment Brief" if triggered else "CEO投资简报：无动作 / CEO Brief: No Action"
        send_email(subject, report)


if __name__ == "__main__":
    main()
