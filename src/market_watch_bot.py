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
        f"结论：本次数据质量 {quality_status}。",
        f"Summary: data quality this run was {quality_status}.",
        f"请求汇总：共 {total_count} 个数据点，成功 {ok_count}，警告 {warn_count}，失败 {fail_count}。",
        f"Fetch summary: {total_count} data points, {ok_count} ok, {warn_count} warnings, {fail_count} failures.",
        "说明：这里只展开异常项。免费/公开接口可能有延迟、节假日缺口或个别 ticker 抓取失败；交易前仍应人工复核关键价格和新闻。",
        "Note: only exceptions are listed. Public/free data can be delayed or missing around holidays; verify key prices and news manually before trading.",
    ]
    if fallback_items:
        lines.append(f"新闻搜索提示：{len(fallback_items)} 次使用备用新闻源，属于降级成功，不影响行情数据。")
        lines.append(f"News-search note: fallback source used {len(fallback_items)} times; this is degraded-but-successful, not a price-data failure.")
    if qualities_to_show:
        lines.append("")
        lines.append("异常项 / Exceptions:")
        lines.extend(quality_line(item) for item in qualities_to_show)
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
    blocks = []
    for row in rows:
        name, background, core_view, latest, usage = row
        blocks.append(
            "\n".join(
                [
                    f"- {name}",
                    f"  背景：{background}",
                    f"  方法：{core_view}",
                    f"  最新：\n{indent_text(latest, 4)}",
                    f"  用法：{usage}",
                ]
            )
        )
    return "\n\n".join(blocks)


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
    actions: List[str] = []
    if rotation_alerts:
        actions.append("有轮动候选需要人工复核；优先看强转弱/高切低机会。")
        actions.append("Rotation candidates triggered; review strong-to-cheap switches first.")
    if any("held strength" in row[1] or "持仓大涨" in row[1] for row in stock_rows):
        actions.append("部分持仓进入强势检查区，更接近分批止盈，而不是追高。")
        actions.append("Some held names moved into trim-review territory rather than chase territory.")
    if any("deep pullback" in row[1] or "接近" in row[1] or "低位" in row[1] for row in stock_rows):
        actions.append("有低位/深回撤信号，但仍需先确认基本面没有被破坏。")
        actions.append("There are low-zone / deep-pullback signals, but the thesis still needs a clean fundamental check.")
    if any("extreme-fear" in text.lower() or "极度恐慌" in text for text in indicator_alerts):
        actions.append("市场情绪进入高价值观察区，可以提高低位研究优先级。")
        actions.append("Sentiment moved into a high-value watch zone; raise priority on panic-entry research.")

    if not actions:
        actions = [
            "今天没有新的高优先级动作，继续观察，不做情绪化交易。",
            "No new high-priority action today; keep watching and avoid emotional trading.",
        ]
    return "\n".join(f"- {line}" for line in unique_lines(actions))


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
                trim_text(signal_type, 30),
                trim_text(metrics, 48),
                trim_text(analysis, 68),
                trim_text(decision, 68),
            ]
        )
    return markdown_table(
        ["标的 / Asset", "信号 / Signal", "数据 / Snapshot", "判断 / Read", "动作 / Action"],
        table_rows,
    )


def concise_signal_briefs(rows: List[List[str]], max_rows: int = 4) -> str:
    briefs: List[str] = []
    for name, signal_type, metrics, analysis, decision in top_signal_rows(rows, max_rows=max_rows):
        briefs.append(
            "\n".join(
                [
                    f"**{name}**",
                    f"- 信号 / Signal: {trim_text(signal_type, 36)}",
                    f"- 判断 / Read: {trim_text(plain_text(analysis), 120)}",
                    f"- 动作 / Action: {trim_text(plain_text(decision), 120)}",
                ]
            )
        )
    return "\n\n".join(briefs)


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


def market_temperature(indicator_alerts: List[str]) -> str:
    if not indicator_alerts:
        return "今天没有新的宏观/情绪硬信号。/ No new macro or sentiment hard trigger today."
    return "\n\n".join(indicator_alerts[:4])


def not_expanded_today(config: Dict[str, Any], quote_cache: Dict[str, Quote], expanded_rows: List[List[str]]) -> str:
    expanded_names = {row[0].split(" (")[0] for row in expanded_rows}
    omitted: List[str] = []
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
            omitted.append(f"- {name}: {position_label}，离一年低位已经不近，今天没有新催化，不单独展开。")
        elif ratio >= 0.35:
            omitted.append(f"- {name}: {position_label}，还在中间区间，没有新的亮点或警示点。")
    if not omitted:
        return ""
    return "\n".join(omitted[:12])


def experience_reminders() -> str:
    reminders = [
        "好股票也要买在好价格；腾讯的历史交易已经验证，时机和价格同样重要。",
        "Even a good company needs the right entry price; your Tencent history already proved timing matters.",
        "浮盈不兑现会回吐；Oracle 的经历说明分批止盈比死拿更适合你。",
        "Unrealized gains can evaporate; your Oracle experience supports staged profit-taking over passive hope.",
        "持有时间不会自动纠错；OVH 说明错误标的或错误时点不能只靠等待解决。",
        "Time does not fix every mistake; OVH showed that wrong picks or wrong timing cannot always be repaired by waiting.",
    ]
    return "\n".join(f"- {line}" for line in reminders)


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

    pair_blocks: List[str] = []
    max_pairs = int(engine.get("max_pairs_per_email", 5))
    for from_stock, from_quote, from_reasons in sell_candidates:
        for to_stock, to_quote, to_reasons in buy_candidates:
            if from_stock["ticker"] == to_stock["ticker"]:
                continue
            if len(pair_blocks) >= max_pairs:
                break
            ROTATION_TARGET_TICKERS.add(to_stock["ticker"])
            pair_index = len(pair_blocks) + 1
            ROTATION_PAIR_CONTEXT.append(
                (from_stock["name"], from_stock["ticker"], to_stock["name"], to_stock["ticker"])
            )
            target_is_held = "持仓加仓 / held add" if to_stock.get("position", 0) > 0 else "观察买入 / watchlist buy"
            pair_blocks.append(
                "\n".join(
                    [
                        f"{pair_index}. {from_stock['name']} -> {to_stock['name']} ({target_is_held})",
                        f"   目标数据：{quote_metrics_inline(to_quote)}",
                        "   机会理由：",
                        indent_text(short_reasons(to_reasons)),
                        "   红旗核查：",
                        indent_text(red_flag_news_check(to_stock)),
                        "   大V观点：",
                        indent_text(influencer_rotation_check(from_stock, to_stock, pair_index)),
                        "   建议动作：先人工复核；若基本面没破，只考虑小比例/分批。",
                    ]
                )
            )
        if len(pair_blocks) >= max_pairs:
            break

    if not pair_blocks:
        return []

    funding_blocks = [
        f"- {stock['name']} ({stock['ticker']}): {quote_metrics_inline(quote)}\n"
        f"  理由：{plain_text('; '.join(reasons))}"
        for stock, quote, reasons in sell_candidates
    ]
    lines = [
        f"结论：出现 {len(pair_blocks)} 个轮动候选。它们是研究清单，不是同时买入指令。",
        f"Conclusion: {len(pair_blocks)} rotation candidates were triggered. This is a research list, not a trade order.",
        "",
        "资金来源候选 / Possible funding source:",
        "\n".join(funding_blocks),
        "",
        "候选轮动 / Rotation candidates:",
        "\n\n".join(pair_blocks),
        "",
        "红旗核查口径：只对触发轮动的目标标的搜索；查询使用公司名 + ticker + 红旗关键词。结果是证据提示，不是自动定罪；未发现直接匹配不等于风险不存在。",
        "大V核查口径：只搜索配置名单里的公开信息；未发现不等于他们没有观点，可能只是公开搜索源未覆盖。",
        "",
        "口径解释：成交量是市场参与热度；下跌放量说明资金正在重新定价，要先确认是恐慌错杀还是基本面坏了。",
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

    has_any_signal = bool(rotation_alerts or stock_rows or stock_errors or indicator_alerts)

    lines.append("一、今日结论 / Today's Conclusion")
    lines.append("--------------------------------")
    lines.append(executive_summary(rotation_alerts, stock_rows, indicator_alerts))
    lines.append("")

    if rotation_alerts or stock_rows:
        lines.append("二、可执行信号 / Actionable Signals")
        lines.append("----------------------------------")
        if stock_rows:
            lines.append(concise_signal_briefs(stock_rows, max_rows=4))
            lines.append("")
        if rotation_alerts:
            lines.append("重点调仓候选 / Rotation Candidates")
            lines.append("\n\n".join(rotation_alerts[:2]))
        lines.append("")

    if stock_rows or stock_errors:
        lines.append("三、持仓与观察诊断 / Portfolio & Watchlist Diagnosis")
        lines.append("--------------------------------------------------")
        if stock_rows:
            lines.append(portfolio_diagnosis(top_signal_rows(stock_rows, max_rows=8)))
        if stock_errors:
            lines.append("")
            lines.append("数据异常 / Data Exceptions")
            lines.append("\n\n".join(stock_errors))
        lines.append("")

    if indicator_alerts:
        lines.append("四、市场温度 / Market Temperature")
        lines.append("--------------------------------")
        lines.append(market_temperature(indicator_alerts))
        lines.append("")

    omitted_section = not_expanded_today(config, quote_cache, stock_rows)
    if omitted_section:
        lines.append("五、今天不单独展开 / Not Expanded Today")
        lines.append("-------------------------------------")
        lines.append(omitted_section)
        lines.append("")

    if influencer_rows:
        lines.append("六、高手雷达 / Influencer Radar")
        lines.append("--------------------------------")
        lines.append(influencer_blocks(influencer_rows[:3]))
        lines.append("")

    lines.append("七、经验提醒 / Experience Reminders")
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
