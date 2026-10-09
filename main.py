#!/usr/bin/env python3
"""Read-only Binance futures price alerts. Python 3.12+, standard library only.

Default baseline = previous completed Binance UTC daily candle, NOT an
underlying stock exchange's official previous close. No trading API is used.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import contextvars
import copy
import datetime as dt
import functools
import decimal
import gzip
import html
import http.client
import inspect
import json
import hmac
import logging
import math
import os
import re
import secrets
import signal
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

D = decimal.Decimal
UTC = dt.timezone.utc
BEIJING = dt.timezone(dt.timedelta(hours=8))
DAY_MS = 86_400_000
VERSION = "1.35.4"
LOG = logging.getLogger("close-alert")
NAMES = {"UNITREEUSDT": "宇树 UNITREE", "HK0625USDT": "SHEIN 希音",
         "CXMTUSDT": "长鑫 CXMT", "SKHYNIXUSDT": "SK 海力士"}
ALIASES = {"UNITREE": "UNITREEUSDT", "宇树": "UNITREEUSDT",
           "SHEIN": "HK0625USDT", "HK0625": "HK0625USDT", "希音": "HK0625USDT",
           "CXMT": "CXMTUSDT", "长鑫": "CXMTUSDT",
           "SKHYNIX": "SKHYNIXUSDT", "海力士": "SKHYNIXUSDT"}


def number(value: Any, label: str = "数值", *, zero_ok: bool = False) -> D:
    try:
        result = D(str(value))
    except (decimal.InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{label}必须是数字") from None
    if not result.is_finite() or result < 0 or (result == 0 and not zero_ok):
        raise ValueError(f"{label}必须是{'非负' if zero_ok else '正'}的有限数字")
    return result


def fmt(value: Any) -> str:
    value = D(str(value))
    return format(value, ",.8f").rstrip("0").rstrip(".")


def fmt_price(value: Any) -> str:
    """Contract prices trimmed for display: 4 decimals at or above 1, 6 below (feeds send 8+)."""
    value = D(str(value))
    return fmt(value.quantize(D("0.0001") if abs(value) >= 1 else D("0.000001")))


def brief_error(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def percent(price: D, baseline: D) -> D:
    return (number(price) - number(baseline)) / baseline * D(100)


def beijing_day(now: float | None = None) -> str:
    return dt.datetime.fromtimestamp(time.time() if now is None else now, BEIJING).date().isoformat()


def stamp(ms: int | float, seconds: bool = True) -> str:
    """Beijing-time stamp such as 09-17 16:00:00, or 09-17 16:00 without seconds."""
    return dt.datetime.fromtimestamp(float(ms) / 1000, BEIJING).strftime("%m-%d %H:%M:%S" if seconds else "%m-%d %H:%M")


def close_label(close_ms: int) -> str:
    return f"｜收盘 {stamp(close_ms, seconds=False)}（北京时间）" if close_ms else ""


# --- presentation helpers -----------------------------------------------------------------------
# Messages are composed as plain text; bold spans are marked with sentinels and turned into HTML
# tags only after escaping, so symbols, error strings and prices can never break the markup.
B0, B1 = "\x01", "\x02"


def bold(text: Any) -> str:
    return f"{B0}{text}{B1}"


def to_html(text: str) -> str:
    return html.escape(text, quote=False).replace(B0, "<b>").replace(B1, "</b>")


def trend_mark(change: D, style: str) -> str:
    """Colour dot for a percentage move. cn = 红涨绿跌 (A-share convention), us = 绿涨红跌."""
    if abs(change) < D("0.0005"):
        return "⚪"
    up = change > 0
    return ("🔴" if up else "🟢") if style == "cn" else ("🟢" if up else "🔴")


def pct_text(change: D, style: str, strong: bool = False, digits: int = 2) -> str:
    body = f"{change:+.{digits}f}%"
    return f"{trend_mark(change, style)} {bold(body) if strong else body}"


def tree(rows: list[str]) -> list[str]:
    """Prefix rows with ├ / └ so a block reads as one unit."""
    rows = [r for r in rows if r]
    return [("└ " if i == len(rows) - 1 else "├ ") + r for i, r in enumerate(rows)]


def hhmm(ms: int | float) -> str:
    return stamp(ms, seconds=False)[6:]


def short_source(source: str) -> str:
    """'上交所688836·腾讯' -> '腾讯'; '韩交所000660·Naver 日K·含 NXT' -> 'Naver 日K·含 NXT' (only the venue and code go)."""
    return source.split("·", 1)[-1] if source else ""


def baseline_brief(base: "Baseline") -> str:
    """Short origin of a baseline for the 基准 row: '15:00 交易所收盘时刻·成交价', '08:00 UTC日K', '手动·适用 09-23'."""
    kind = base.key.split(":", 1)[0]
    if kind == "manual":
        return f"手动·适用 {base.key.split(':')[1][5:]}" + ("·" + hhmm(base.close_ms) + " 收" if base.close_ms else "")
    when = stamp(base.close_ms, seconds=False) if base.close_ms else ""
    if kind == "daily":
        return f"{when} UTC日K换日".strip()
    if kind == "exchange_time":
        price_kind = "标记价" if "标记价" in base.label else "成交价"
        pending = re.search(r"⏳ (\S+) 收盘待确认", base.label)
        return f"{when} 交易所收盘时刻·{price_kind}".strip() + (f"·⏳ {pending.group(1)} 收盘待确认" if pending else "")
    return when or kind


def legend(style: str) -> str:
    return "🔴 涨 🟢 跌 ⚪ 平" if style == "cn" else "🟢 涨 🔴 跌 ⚪ 平"


def clean_error(error: BaseException | str) -> str:
    # Telegram tokens must never appear in logs, chat messages or diagnostics.
    text = str(error)
    text = re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{15,}\b", "[TOKEN REDACTED]", text)
    text = re.sub(r"https?://\S+", "[URL]", text)
    return text[:280]


def parse_bounded(env: dict[str, str], key: str, default: str, low: float, high: float) -> float:
    try:
        value = float(env.get(key, default))
    except ValueError:
        raise ValueError(f"{key} 必须是数字") from None
    if not (low <= value <= high and math.isfinite(value)):
        raise ValueError(f"{key} 必须在 {low:g}～{high:g} 之间")
    return value


def bounded_int(env: dict[str, str], key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(env.get(key, str(default)))
    except ValueError:
        raise ValueError(f"{key} 必须是整数") from None
    if not low <= value <= high:
        raise ValueError(f"{key} 必须在 {low}～{high} 之间")
    return value


@dataclass(frozen=True)
class StockTicker:
    """Where to fetch a contract's underlying stock close: market prefix + exchange code."""
    market: str          # sh / sz (Eastmoney A-share), hk (Eastmoney HKEX), kr (Naver KRX)
    code: str
    same_unit: bool = False  # True when the contract is quoted in the stock's own currency.


@dataclass(frozen=True)
class StockMarketInfo:
    name: str
    currency: str
    utc_offset: int
    close_time: dt.time  # When the official closing price is fixed, local time; the bar is final ~15 min later.
    tz_name: str = "北京时间"

    def close_label(self, close_ms: int) -> str:
        """'｜收盘 09-18 15:00（北京时间）', plus the venue's local time when it differs."""
        if not close_ms:
            return ""
        if self.utc_offset == 8:
            return close_label(close_ms)
        local = dt.datetime.fromtimestamp(close_ms / 1000, dt.timezone(dt.timedelta(hours=self.utc_offset)))
        return f"｜收盘 {stamp(close_ms, seconds=False)}（北京时间，{self.tz_name[:-2]} {local.strftime('%H:%M')}）"


# Official closing-price times, verified 2026-09:
#   SSE/SZSE: continuous trading ends 15:00 (STAR after-hours fixed-price trading 15:05-15:30 uses that close).
#   HKEX: closing auction 16:00-16:10, the closing price is fixed at 16:08-16:10.
#   KRX: regular session 09:00-15:30 KST fixes the official close; the after-hours sessions added on
#        2026-09-14 (15:40-16:00 close-price trading, 16:00-20:00 continuous) do not change it.
STOCK_MARKETS = {
    "sh": StockMarketInfo("上交所", "CNY", 8, dt.time(15, 0)),
    "sz": StockMarketInfo("深交所", "CNY", 8, dt.time(15, 0)),
    "hk": StockMarketInfo("港交所", "HKD", 8, dt.time(16, 10)),
    "kr": StockMarketInfo("韩交所", "KRW", 9, dt.time(15, 30), "韩国时间"),
}
# Underlying stocks of the default contracts (all listed as of 2026-09): Unitree 688836.SS,
# SHEIN 0625.HK, CXMT 688825.SS, SK hynix 000660.KS.
# HK0625USDT is a quanto contract: its price is the HKD stock price itself, so it compares directly (:same).
# The others are USD-denominated, so their exchange closes are converted with the FX rate first.
DEFAULT_TICKERS = "UNITREEUSDT=sh:688836,HK0625USDT=hk:00625:same,CXMTUSDT=sh:688825,SKHYNIXUSDT=kr:000660"
NOTICE_GRACE_SECONDS = 30  # a data fault must last this long before subscribers hear of it (one failed request is not an outage)
WATCHDOG_SECONDS = 600     # the sampling loop not starting a cycle for this long means it is stuck: the process exits, Railway restarts it
SHUTDOWN_GRACE_SECONDS = 8  # at shutdown, how long messages already on their way may take to finish
REFERENCE_TICK = 1        # seconds between checks in each reference task (each feed has its own cadence; stock quotes run at 1 s)
REFERENCE_TIMEOUT = 300   # one reference refresh may take this long before it is abandoned
EXCHANGE_BASE_HOLD_DAYS = 30  # safety cap for a held exchange-close baseline; National Day / Chuseok fit easily


# Exchange holidays on weekdays (official 2026 notices where known); override with HOLIDAYS_CN/HK/KR/SG.
# Only dates that are certain are listed: a holiday missing here costs a "close pending" evening, a wrong one would
# skip a real session. 2027 Lunar New Year closures are added once the exchanges publish them.
DEFAULT_HOLIDAYS = {
    "CN": "2026-09-25,2026-10-01..2026-10-07,2027-01-01",   # SSE notice: Mid-Autumn 9/25, National Day 10/1-10/7; New Year
    "KR": "2026-09-24,2026-09-25,2026-10-05,2026-10-09,2026-12-25,2026-12-31,2027-01-01",  # Chuseok, Foundation Day (substitute), Hangul Day, Christmas, year-end closure, New Year
    "HK": "2026-10-01,2026-10-19,2026-12-25,2027-01-01",  # National Day, the day after Chung Yeung (10-18 is a Sunday), Christmas (Boxing Day falls on a Saturday: no weekday off), New Year
    # SGX (the A50 futures): Singapore's gazetted holidays; a weekday one shuts both the day and the night session
    "SG": "2026-01-01,2026-02-17,2026-02-18,2026-04-03,2026-05-01,2026-05-27,2026-06-01,2026-08-10,2026-11-09,2026-12-25,2027-01-01",
}
# Days whose session differs from the usual one (verified notices; override with HK_HALF_DAYS / KR_LATE_DAYS):
#   HKEX half days (the eves of Christmas, New Year and Lunar New Year): morning session only, closing auction
#     12:00–12:10, HSI futures day session ends 12:30 and there is no after-hours session that evening.
#   KRX CSAT day (the college entrance exam, third Thursday of November): everything one hour later, regular
#     session 10:00–16:30 KST, close fixed at 16:30, Nextrade after-hours from 16:40.
#   SGX half days (the eves of Lunar New Year, Christmas and New Year): the A50 day session ends at noon
#     (A50_HALF_DAY_END) and there is no T+1 session that evening.
DEFAULT_SPECIAL_DAYS = {"HK_HALF": "2026-12-24,2026-12-31,2027-02-05", "KR_LATE": "2026-11-19",
                        "SG_HALF": "2026-02-16,2026-12-24,2026-12-31"}
HOLIDAY_WARN_DAYS = 30  # warn this many days before the configured calendar runs out


def parse_dates(spec: str, label: str) -> frozenset:
    days: set[dt.date] = set()
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        start, _, end = item.partition("..")
        try:
            first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end or start)
        except ValueError:
            raise ValueError(f"{label} 日期格式应为 YYYY-MM-DD 或 YYYY-MM-DD..YYYY-MM-DD：{item}") from None
        while first <= last:
            days.add(first)
            first += dt.timedelta(days=1)
    return frozenset(days)


def parse_holidays(env: dict[str, str]) -> dict[str, frozenset]:
    cn = parse_dates(env.get("HOLIDAYS_CN", DEFAULT_HOLIDAYS["CN"]), "HOLIDAYS_CN")
    return {"sh": cn, "sz": cn, "hk": parse_dates(env.get("HOLIDAYS_HK", DEFAULT_HOLIDAYS["HK"]), "HOLIDAYS_HK"),
            "kr": parse_dates(env.get("HOLIDAYS_KR", DEFAULT_HOLIDAYS["KR"]), "HOLIDAYS_KR"),
            "sg": parse_dates(env.get("HOLIDAYS_SG", DEFAULT_HOLIDAYS["SG"]), "HOLIDAYS_SG")}


def calendar_until(holidays: dict[str, frozenset]) -> dt.date | None:
    """The last date the holiday table knows about (per market the earliest of those), None when it is empty."""
    ends = [max(days) for days in holidays.values() if days]
    return min(ends) if ends else None


def calendar_warning(holidays: dict[str, frozenset], today: dt.date) -> str:
    """A reminder to extend HOLIDAYS_CN/HK/KR/SG before the table runs out ("" while it reaches far enough)."""
    until = calendar_until(holidays)
    if until is None:
        return "⚠️ 未配置任何交易所假期（HOLIDAYS_CN/HK/KR/SG），假期会被当成交易日"
    if (until - today).days < HOLIDAY_WARN_DAYS:
        return (f"⚠️ 假期表只配置到 {until.isoformat()}" + ("（已过期）" if until < today else "") +
                "，请在 Railway 变量 HOLIDAYS_CN/HK/KR/SG 补充之后的休市日，否则假期会被当成交易日")
    return ""


def parse_beta(value: str, name: str = "A50_BETA") -> float:
    try:
        beta = float(value)
    except ValueError:
        raise ValueError(f"{name} 必须是数字，如 0.8") from None
    if not 0 < beta <= 3:
        raise ValueError(f"{name} 应在 0～3 之间")
    return beta


def parse_prob_vol(spec: str) -> dict[str, float]:
    """PROB_VOL="UNITREEUSDT=3.5,HSI=1.2,KOSPI=2" — daily volatility in percent, overriding estimates."""
    result: dict[str, float] = {}
    for item in spec.split(","):
        if not item.strip():
            continue
        key, _, value = item.partition("=")
        try:
            sigma = float(value) / 100
        except ValueError:
            raise ValueError(f"PROB_VOL 数值无效：{item.strip()}") from None
        if not 0 < sigma < 1:
            raise ValueError(f"PROB_VOL 应为 0～100 之间的日波动率百分比：{item.strip()}")
        result[key.strip().upper()] = sigma
    return result


def parse_fx(spec: str) -> dict[str, D]:
    """FX_RATES="CNY=7.12,HKD=7.79": units of each currency per 1 USD; overrides the fetched rates."""
    rates: dict[str, D] = {}
    for item in spec.split(","):
        if not item.strip():
            continue
        currency, _, value = item.partition("=")
        currency = currency.strip().upper()
        if currency not in CURRENCIES:
            raise ValueError(f"FX_RATES 不支持的货币：{currency or item.strip()}")
        rates[currency] = number(value.strip(), f"FX_RATES {currency}")
    return rates
TICKER_RE = re.compile(r"(sh|sz|hk|kr):([0-9A-Za-z]{1,12})(:same)?", re.IGNORECASE)


# Hyperliquid HIP-3 markets for the same underlyings (trade.xyz dex). Unknown names simply show "未找到".
DEFAULT_HL_TICKERS = "UNITREEUSDT=xyz:UNITREE,HK0625USDT=xyz:SHEIN,CXMTUSDT=xyz:CXMT,SKHYNIXUSDT=xyz:SKHX"
# Index perps on Hyperliquid compared with the cash index (KR200 = KOSPI 200); "off" disables.
DEFAULT_HL_INDEX = "KR200=xyz:KR200"
HL_TICKER_RE = re.compile(r"(?:([A-Za-z0-9_]{1,16}):)?([A-Za-z0-9_.-]{1,24})")


DEFAULT_PREDICT_SLUGS = ("HSI=hang-seng-index,KOSPI=kospi-composite-index,SSE=sse-composite-index,"
                         "UNITREEUSDT=unitree,HK0625USDT=shein,CXMTUSDT=cxmt,SKHYNIXUSDT=sk-hynix-inc")
PREDICT_SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def parse_predict_slugs(spec: str) -> dict[str, str]:
    """"HSI=hang-seng-index,CXMTUSDT=cxmt" -> {key: slug stem}; the date part is added per target day."""
    out: dict[str, str] = {}
    for part in spec.split(","):
        if not part.strip():
            continue
        key, sep, stem = part.partition("=")
        key, stem = key.strip().upper(), stem.strip().lower()
        if not sep or not re.fullmatch(r"[A-Z0-9_]{2,40}", key) or not PREDICT_SLUG_RE.fullmatch(stem):
            raise ValueError(f"PREDICT_SLUGS 格式错误：{part.strip()}（应为 HSI=hang-seng-index 这样的 键=slug前缀）")
        out[key] = stem
    return out


def parse_ref_code(value: str) -> str:
    code = value.strip()
    if code and not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", code):
        raise ValueError("PREDICT_REF_CODE 应为 1～32 位字母、数字、- 或 _（留空表示不加邀请码）")
    return code


def parse_deadlines(spec: str) -> dict[str, int]:
    """LADDER_DEADLINES="STRC-100=2026-12-31;OTHER=2027-01-15": a STOCK_HIT_MARKETS key -> 23:59 ET of that date, in ms."""
    out: dict[str, int] = {}
    for part in re.split(r"[;,\s]+", spec.strip()):
        if not part:
            continue
        key, sep, day = part.partition("=")
        try:
            date = dt.date.fromisoformat(day.strip())
        except ValueError:
            raise ValueError(f"LADDER_DEADLINES 里 {part!r} 不是 键=YYYY-MM-DD") from None
        if not sep or not key.strip():
            raise ValueError(f"LADDER_DEADLINES 里 {part!r} 缺少键")
        out[key.strip().upper()] = et_wall_ms(date, 23, 59)
    return out


def parse_hl_tickers(spec: str, symbols: tuple[str, ...]) -> dict[str, tuple[str, str]]:
    """HL_TICKERS="SYMBOL=dex:COIN,..." (dex omitted = the main Hyperliquid perp dex); "off" disables."""
    tickers: dict[str, tuple[str, str]] = {}
    if spec.strip().lower() in {"", "off", "none"}:
        return tickers
    for item in spec.split(","):
        if not item.strip():
            continue
        symbol, _, ticker = item.partition("=")
        symbol, match = symbol.strip().upper(), HL_TICKER_RE.fullmatch(ticker.strip())
        if not symbol or not match:
            raise ValueError("HL_TICKERS 格式：合约=dex:币种，如 SKHYNIXUSDT=xyz:SKHX（主 dex 可省略 dex:）")
        if symbol in symbols:
            tickers[symbol] = ((match.group(1) or "").lower(), match.group(2).upper())
    return tickers


def parse_tickers(spec: str, symbols: tuple[str, ...]) -> dict[str, StockTicker]:
    """EXCHANGE_TICKERS="SYMBOL=market:code[:same],..."; "off" disables automatic exchange closes."""
    tickers: dict[str, StockTicker] = {}
    if spec.strip().lower() in {"", "off", "none"}:
        return tickers
    for item in spec.split(","):
        if not item.strip():
            continue
        symbol, _, ticker = item.partition("=")
        symbol = symbol.strip().upper()
        match = TICKER_RE.fullmatch(ticker.strip())
        if not symbol or not match:
            raise ValueError("EXCHANGE_TICKERS 格式：合约=市场:代码，如 SKHYNIXUSDT=kr:000660；市场取 sh/sz/hk/kr")
        if symbol in symbols:  # Entries for unmonitored symbols are harmless and ignored.
            tickers[symbol] = StockTicker(match.group(1).lower(), match.group(2), bool(match.group(3)))
    return tickers


BASELINE_SHORT = {"binance_daily": "UTC 日K（北京 08:00）", "exchange_close": "交易所收盘时刻", "manual": "手动参考价"}
BASELINE_MODES = {
    "binance_daily": "币安上一 UTC 日日 K 收盘（非股票正式昨收）",
    "exchange_close": "币安合约在证券交易所收盘时刻的价格（与股票收盘同一时点）",
    "manual": "手动同口径参考价（每日核对）",
}


@dataclass(frozen=True)
class Config:
    token: str
    admin_id: int
    symbols: tuple[str, ...]
    db_path: str
    threshold: D
    cooldown: int
    poll: int
    step: D
    min_gap: int
    max_age: int
    baseline_mode: str
    base_url: str
    tickers: dict[str, "StockTicker"]
    color_style: str
    fx_manual: dict[str, D]
    hsi_futures: bool = True  # Show the Hang Seng Index futures quote (night session after HK close).
    probability: bool = True  # Show model probabilities that the next close ends above/below the reference.
    prob_vol: dict[str, float] = field(default_factory=dict)  # Daily σ overrides (fraction), keyed by symbol/HSI/KOSPI.
    sse_index: bool = True  # Shanghai Composite with the FTSE China A50 futures as after-hours proxy.
    a50_beta: float = 0.8   # Composite move per unit of A50 move when mapping the proxy.
    kospi_beta: float = 1.0  # KOSPI move per unit of HL KR200 (KOSPI 200 perp) move.
    holidays: dict[str, frozenset] = field(default_factory=dict)  # market -> non-trading weekdays
    hk_half_days: frozenset = frozenset()  # HKEX half days (close 12:10, futures 12:30, no night session)
    kr_late_days: frozenset = frozenset()  # KRX days that run one hour late (CSAT day: 10:00–16:30)
    quote_refresh: int = 1   # seconds between realtime stock quotes while a stock's session runs (QUOTE_REFRESH_SECONDS)
    a50_beta_dynamic: bool = True  # fit the A50 → Composite coefficient from the bot's own after-hours record (A50_BETA is the prior)
    web_port: int = 0        # Read-only probability web page; 0 = disabled. Railway injects PORT.
    web_token: str = ""      # Secret path segment; generated and persisted when empty.
    web_base: str = ""       # Public base URL, e.g. https://xxx.up.railway.app
    hl_tickers: dict[str, tuple[str, str]] = field(default_factory=dict)  # symbol -> (dex, coin) on Hyperliquid
    ladder_deadlines: dict[str, int] = field(default_factory=dict)  # STOCK_HIT_MARKETS key -> deadline (23:59 ET, ms) pinned by hand
    kospi_index: bool = True  # Show the KOSPI composite index for Korea-listed underlyings.
    hl_index: dict[str, tuple[str, str]] = field(default_factory=dict)  # index name -> (dex, coin), e.g. KR200
    predict: bool = True     # Fetch the matching Predict.fun up/down orderbooks and compare them with the model.
    predict_slugs: dict[str, str] = field(default_factory=dict)  # HSI/KOSPI/SSE/symbol -> Predict slug stem
    predict_api_key: str = ""  # Optional x-api-key for api.predict.fun (REST orderbook).
    predict_poll: int = 15   # seconds between orderbook refreshes
    predict_ref: str = "B00EA"  # referral code appended to Predict market links (?ref=); empty = none
    predict_fee_bps: int = 200   # taker fee rate when a market states none (Predict: 2% × min(p, 1 − p); makers 0)
    predict_trade_usd: float = 100.0  # trade size a taker edge is priced for (walks the book's depth)
    predict_min_edge: float = 0.02    # net edge (per $1 share) a suggestion needs at least, on top of the model error
    sim: bool = True         # paper trading: a simulated buy whenever a suggestion's net edge reaches sim_edge
    sim_edge: float = 0.10   # net edge (per $1 share) that triggers a simulated buy
    sim_shares: float = 100.0  # shares per simulated buy
    sim_ways: str = "taker"  # which suggestions it takes: taker (吃单), maker (挂单) or both
    sim_markets: frozenset = frozenset({"close"})  # the market kinds it trades (SIM_KINDS keys); SIM_MARKETS=all for every kind
    sim_group_usd: float = 300.0  # the most one driver's positions may lose on a single move (paper $); 0 = no limit
    touch: bool = True       # BNB $700 / $900 first-touch market card (Binance spot + Predict book)
    auction_alert: bool = True  # Telegram reminder when a market's closing auction starts
    preopen_alert: bool = True  # Telegram reminder before a market's pre-open auction (its first direction signal)
    preopen_lead: int = 5       # minutes before the pre-open auction starts (0 = as it starts)
    edge_alert: bool = True  # Telegram: a suggestion reaching edge_alert_edge; later, that suggestion going away or turning
    edge_alert_edge: float = 0.10  # net edge (per $1 share) a suggestion needs before it is announced
    edge_alert_confirm: int = 60   # seconds a change has to hold before it is announced (a one-refresh blip is not)
    edge_alert_cooldown: int = 900  # seconds before the same market and side is announced as new again
    edge_alert_digest: int = 0  # minutes: 新机会 gathered into one message per period, grouped by driver; 0 = one by one

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        e = dict(os.environ if env is None else env)
        symbols = tuple(dict.fromkeys(s.strip().upper() for s in
                        e.get("SYMBOLS", ",".join(NAMES)).split(",") if s.strip()))
        if not symbols or len(symbols) > 30 or any(not re.fullmatch(r"[A-Z0-9_]{3,40}", s) for s in symbols):
            raise ValueError("SYMBOLS 应为 1～30 个逗号分隔的币安合约代码")
        tickers = parse_tickers(e.get("EXCHANGE_TICKERS", DEFAULT_TICKERS), symbols)
        # With exchange tickers configured the baseline defaults to the exchange-close instant, so the
        # contract's deviation and the stock's close are measured from the same moment.
        mode = e.get("BASELINE_MODE", "exchange_close" if tickers else "binance_daily").strip()
        token = e.get("WEB_TOKEN", "").strip()
        if token and not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", token):
            raise ValueError("WEB_TOKEN 应为 16～64 位字母、数字、- 或 _")
        if mode not in BASELINE_MODES:
            raise ValueError("BASELINE_MODE 只能是 binance_daily、manual 或 exchange_close")
        sim_ways = (e.get("SIM_WAYS") or "taker").strip().lower()
        if sim_ways not in {"taker", "maker", "both"}:
            raise ValueError("SIM_WAYS 只能是 taker（只吃单）、maker（只挂单）或 both（都做）")
        wanted = {k.strip().lower() for k in (e.get("SIM_MARKETS") or "close").split(",") if k.strip()}
        sim_markets = frozenset(SIM_KINDS) if "all" in wanted else frozenset(wanted)
        if not sim_markets or not sim_markets <= set(SIM_KINDS):
            raise ValueError(f"SIM_MARKETS 只能是 all，或 {'、'.join(SIM_KINDS)} 的组合（逗号分隔）")
        url = e.get("BINANCE_BASE_URL", "https://fapi.binance.com").rstrip("/")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
            raise ValueError("BINANCE_BASE_URL 必须是无账号、查询参数的 HTTPS 根地址")
        threshold = number(e.get("ALERT_THRESHOLD_PCT", "1"), "ALERT_THRESHOLD_PCT")
        if not D("0.01") <= threshold <= D(100):
            raise ValueError("ALERT_THRESHOLD_PCT 必须在 0.01～100 之间，1 表示 1%")
        style = e.get("COLOR_STYLE", "cn").strip().lower()
        if style not in {"cn", "us"}:
            raise ValueError("COLOR_STYLE 只能是 cn（红涨绿跌）或 us（绿涨红跌）")
        # The session helpers consult one process-wide calendar (a half day is one fact, not a dozen call sites).
        holidays = parse_holidays(e)
        CALENDAR.configure(parse_dates(e.get("HK_HALF_DAYS", DEFAULT_SPECIAL_DAYS["HK_HALF"]), "HK_HALF_DAYS"),
                           parse_dates(e.get("KR_LATE_DAYS", DEFAULT_SPECIAL_DAYS["KR_LATE"]), "KR_LATE_DAYS"),
                           holidays["sg"], parse_dates(e.get("SG_HALF_DAYS", DEFAULT_SPECIAL_DAYS["SG_HALF"]), "SG_HALF_DAYS"))
        return cls(
            token=e.get("TELEGRAM_BOT_TOKEN", "").strip(),
            admin_id=bounded_int(e, "ADMIN_USER_ID", 0, 0, 10**15),
            symbols=symbols, db_path=e.get("STATE_DB", "./data/bot.sqlite3"),
            threshold=threshold,
            cooldown=bounded_int(e, "ALERT_COOLDOWN_SECONDS", 300, 0, 86400),
            poll=bounded_int(e, "POLL_SECONDS", 5, 3, 3600),
            quote_refresh=bounded_int(e, "QUOTE_REFRESH_SECONDS", 1, 1, 300),
            step=number(e.get("ALERT_STEP_PCT", "1"), "ALERT_STEP_PCT", zero_ok=True),
            min_gap=bounded_int(e, "MIN_ALERT_GAP_SECONDS", 30, 0, 3600),
            max_age=bounded_int(e, "MAX_PRICE_AGE_SECONDS", 120, 5, 3600),
            baseline_mode=mode, base_url=url,
            tickers=tickers,
            color_style=style, fx_manual=parse_fx(e.get("FX_RATES", "")),
            hsi_futures=e.get("HSI_FUTURES", "on").strip().lower() not in {"off", "0", "false", "no"},
            hl_tickers=parse_hl_tickers(e.get("HL_TICKERS", DEFAULT_HL_TICKERS), symbols),
            kospi_index=e.get("KOSPI_INDEX", "on").strip().lower() not in {"off", "0", "false", "no"},
            hl_index=parse_hl_tickers(e.get("HL_INDEX", DEFAULT_HL_INDEX), ("KR200",)),
            predict=e.get("PREDICT", "on").strip().lower() not in {"off", "0", "false", "no"},
            predict_slugs=parse_predict_slugs(e.get("PREDICT_SLUGS", DEFAULT_PREDICT_SLUGS)),
            predict_api_key=e.get("PREDICT_API_KEY", "").strip(),
            predict_poll=bounded_int(e, "PREDICT_POLL_SECONDS", 15, 5, 3600),
            predict_ref=parse_ref_code(e.get("PREDICT_REF_CODE", "B00EA")),
            predict_fee_bps=bounded_int(e, "PREDICT_FEE_BPS", 200, 0, 1000),
            predict_trade_usd=parse_bounded(e, "PREDICT_TRADE_USD", "100", 1, 1_000_000),
            predict_min_edge=parse_bounded(e, "PREDICT_MIN_EDGE_CENTS", "2", 0, 50) / 100,
            sim=e.get("SIM", "on").strip().lower() not in {"off", "0", "false", "no"},
            sim_edge=parse_bounded(e, "SIM_EDGE_CENTS", "10", 0.5, 50) / 100,
            sim_shares=parse_bounded(e, "SIM_SHARES", "100", 1, 1_000_000),
            sim_ways=sim_ways, sim_markets=sim_markets,
            sim_group_usd=parse_bounded(e, "SIM_GROUP_USD", "300", 0, 10_000_000),
            touch=e.get("BNB_TOUCH", "on").strip().lower() not in {"off", "0", "false", "no"},
            ladder_deadlines=parse_deadlines(e.get("LADDER_DEADLINES", "")),
            auction_alert=e.get("AUCTION_ALERT", "on").strip().lower() not in {"off", "0", "false", "no"},
            preopen_alert=e.get("PREOPEN_ALERT", "on").strip().lower() not in {"off", "0", "false", "no"},
            preopen_lead=bounded_int(e, "PREOPEN_ALERT_LEAD_MINUTES", 5, 0, 60),
            edge_alert=e.get("EDGE_ALERT", "on").strip().lower() not in {"off", "0", "false", "no"},
            edge_alert_edge=parse_bounded(e, "EDGE_ALERT_CENTS", "10", 1, 50) / 100,
            edge_alert_confirm=bounded_int(e, "EDGE_ALERT_CONFIRM_SECONDS", 60, 0, 3600),
            edge_alert_cooldown=bounded_int(e, "EDGE_ALERT_COOLDOWN_SECONDS", 900, 0, 86400),
            edge_alert_digest=bounded_int(e, "EDGE_ALERT_DIGEST_MINUTES", 0, 0, 1440),
            probability=e.get("PROBABILITY", "on").strip().lower() not in {"off", "0", "false", "no"},
            prob_vol=parse_prob_vol(e.get("PROB_VOL", "")),
            sse_index=e.get("SSE_INDEX", "on").strip().lower() not in {"off", "0", "false", "no"},
            a50_beta=parse_beta(e.get("A50_BETA", "0.8")),
            a50_beta_dynamic=e.get("A50_BETA_DYNAMIC", "on").strip().lower() not in {"off", "0", "false", "no"},
            kospi_beta=parse_beta(e.get("KOSPI_BETA", "1"), "KOSPI_BETA"),
            holidays=holidays,
            hk_half_days=parse_dates(e.get("HK_HALF_DAYS", DEFAULT_SPECIAL_DAYS["HK_HALF"]), "HK_HALF_DAYS"),
            kr_late_days=parse_dates(e.get("KR_LATE_DAYS", DEFAULT_SPECIAL_DAYS["KR_LATE"]), "KR_LATE_DAYS"),
            web_port=0 if e.get("WEB", "on").strip().lower() in {"off", "0", "false", "no"}
            else bounded_int(e, "WEB_PORT", int(e.get("PORT") or 0), 0, 65535),
            web_token=e.get("WEB_TOKEN", "").strip(),
            web_base=(e.get("WEB_BASE_URL", "").strip().rstrip("/")
                      or (f"https://{e['RAILWAY_PUBLIC_DOMAIN'].strip()}" if e.get("RAILWAY_PUBLIC_DOMAIN", "").strip() else "")),
        )


class Store:
    """Small persistent JSON records in SQLite. All calls use the event-loop thread."""
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("CREATE TABLE IF NOT EXISTS records (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        self.conn.commit()
        self.touched: dict[str, int] = {}  # key family ("sim", "notice"...) -> how many writes it has seen: in-memory views check this

    def _bump(self, key: str) -> None:
        family = key.partition(":")[0]
        self.touched[family] = self.touched.get(family, 0) + 1

    def get(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT v FROM records WHERE k=?", (key,)).fetchone()
        return json.loads(row[0]) if row else copy.deepcopy(default)

    UPSERT = "INSERT INTO records(k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v"

    def put(self, key: str, value: Any) -> None:
        with self.conn:
            self.conn.execute(self.UPSERT, (key, json.dumps(value, ensure_ascii=False)))
        self._bump(key)

    def put_many(self, pairs: Any) -> None:
        """Several records in one transaction (one fsync instead of one per record)."""
        rows = [(key, json.dumps(value, ensure_ascii=False)) for key, value in pairs]
        if rows:
            with self.conn:
                self.conn.executemany(self.UPSERT, rows)
            for key, _ in rows:
                self._bump(key)

    def keys(self, prefix: str) -> list[str]:
        return [k for (k,) in self.conn.execute("SELECT k FROM records WHERE k >= ? AND k < ? ORDER BY k", self._bounds(prefix))]

    @staticmethod
    def _bounds(prefix: str) -> tuple[str, str]:
        """Key range [low, high) covering every key that starts with ``prefix`` (uses the primary-key index)."""
        return prefix, (prefix[:-1] + chr(ord(prefix[-1]) + 1)) if prefix else "\U0010ffff"

    def items(self, prefix: str) -> list[tuple[str, Any]]:
        rows = self.conn.execute("SELECT k, v FROM records WHERE k >= ? AND k < ? ORDER BY k", self._bounds(prefix))
        return [(k, json.loads(v)) for k, v in rows]

    def count(self, prefix: str) -> int:
        return int(self.conn.execute("SELECT count(*) FROM records WHERE k >= ? AND k < ?", self._bounds(prefix)).fetchone()[0])

    def delete_prefix(self, prefix: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM records WHERE k >= ? AND k < ?", self._bounds(prefix))
        self._bump(prefix)

    def delete_keys(self, keys: list[str]) -> None:
        if keys:
            with self.conn:
                self.conn.executemany("DELETE FROM records WHERE k=?", [(k,) for k in keys])
            for key in keys:
                self._bump(key)

    def close(self) -> None:
        self.conn.close()


class PendingData(ValueError):
    """Data that simply has not arrived yet (first fetch still running): shown in /status, no error notice."""


class RemoteError(Exception):
    def __init__(self, message: str, retry_after: int = 0):
        super().__init__(message)
        self.retry_after = max(0, retry_after)


BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
MAX_BODY = 8_000_000  # bytes per response, before and after decompression
# One TLS context for the process: urlopen without one builds a fresh context, CA bundle parsed and all, for every
# connection, which at a few requests a second is most of the CPU the HTTP layer burns.
SSL_CONTEXT = ssl.create_default_context()
# What an HTTP status means for the operator. Binance's geo / ban codes and their long cool-downs mean nothing on
# Telegram, whose answers carry their own description (and retry_after); a 409 there is the two-instances conflict.
HTTP_HINTS = {451: "部署所在地或接口访问受限；请核对官方地区规则",
              403: "访问被拒绝；请核对权限与服务地区",
              418: "接口暂时封禁；停止高频请求并等待解除",
              429: "接口限流，等待后重试"}
TELEGRAM_HINTS = {409: "Telegram 轮询冲突；同一个 Bot Token 只能运行一个实例"}


def _inflate(raw: bytes, encoding: str) -> bytes:
    """Decode a gzip / deflate body (only when the server says it is one), capped like a plain body."""
    encoding = (encoding or "").strip().lower()
    if encoding not in {"gzip", "x-gzip", "deflate"}:
        return raw
    try:
        out = zlib.decompressobj(zlib.MAX_WBITS | (16 if encoding != "deflate" else 0)).decompress(raw, MAX_BODY + 1)
    except zlib.error:
        return raw  # not actually compressed: use the bytes as they came
    if len(out) > MAX_BODY:
        raise RemoteError("接口返回的数据过大")
    return out


def http_error_text(body: Any) -> str:
    """The reason an API puts in an error body: Telegram's description, Binance's msg, a NestJS-style message (a string
    or a list of them) or detail, else its error field (a string or {message}); "" when nothing readable is there."""
    if not isinstance(body, dict):
        return ""
    for key in ("description", "msg", "message", "detail", "error"):
        value = body.get(key)
        if isinstance(value, dict):
            value = value.get("message") or value.get("msg") or value.get("detail") or ""
        if isinstance(value, list):
            value = "；".join(str(v) for v in value if v)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return ""


def _http_get(url: str, payload: dict | None = None, timeout: int = 15,
              headers: dict[str, str] | None = None) -> bytes:
    """GET (or POST ``payload`` as JSON) and return the body; HTTP/network failures become RemoteError."""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": f"CloseAlert/{VERSION}", "Accept": "application/json", "Accept-Encoding": "gzip",
        **({"Content-Type": "application/json"} if data is not None else {}), **(headers or {}),
    })
    host = urllib.parse.urlsplit(url).hostname or ""
    telegram = host.endswith("telegram.org")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as response:
            raw = response.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise RemoteError("接口返回的数据过大")
            return _inflate(raw, response.headers.get("Content-Encoding", ""))
    except urllib.error.HTTPError as error:
        retry = 0
        try:
            retry = max(0, int(error.headers.get("Retry-After", "0")))
        except (ValueError, TypeError):
            pass
        description = ""
        try:
            body = json.loads(_inflate(error.read(4096), error.headers.get("Content-Encoding", "")))
            description = http_error_text(body)
            retry = max(retry, int(body.get("parameters", {}).get("retry_after", 0)))
        except (ValueError, TypeError, AttributeError, RemoteError):
            pass
        if telegram:  # Telegram says exactly what is wrong (blocked, kicked, retry after N) and how long to wait
            text = TELEGRAM_HINTS.get(error.code) or description or "接口请求失败"
        else:
            if "binance" in host and error.code in {418, 429}:
                retry = max(retry, 120 if error.code == 418 else 30)
            text = HTTP_HINTS.get(error.code) or description or "接口请求失败"
        raise RemoteError(clean_error(f"HTTP {error.code}: {text}"), retry) from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        # Keep the underlying reason (DNS failure, refused, proxy 403, certificate...): /diag relies on it.
        reason = getattr(error, "reason", None) if isinstance(error, urllib.error.URLError) else None
        detail = f": {clean_error(str(reason))[:80]}" if reason else ""
        raise RemoteError(f"网络错误 ({type(error).__name__}{detail})") from None
    except http.client.HTTPException as error:  # IncompleteRead, BadStatusLine, LineTooLong: a broken answer
        detail = clean_error(str(error))[:80]
        raise RemoteError(f"网络错误 ({type(error).__name__}{': ' + detail if detail else ''})") from None


def _http_json(url: str, payload: dict | None = None, timeout: int = 15) -> Any:
    try:
        return json.loads(_http_get(url, payload, timeout))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise RemoteError("接口未返回有效 JSON") from None


# Reference-data tasks run their blocking requests on their own thread pool, so a hung quote feed
# can never occupy the default pool that Binance and Telegram requests use.
HTTP_POOL: contextvars.ContextVar[ThreadPoolExecutor | None] = contextvars.ContextVar("http_pool", default=None)


async def _blocking(fn: Any, *args: Any) -> Any:
    pool = HTTP_POOL.get()
    if pool is None:
        return await asyncio.to_thread(fn, *args)
    return await asyncio.get_running_loop().run_in_executor(pool, fn, *args)


async def http_json(url: str, payload: dict | None = None, timeout: int = 15) -> Any:
    return await _blocking(_http_json, url, payload, timeout)


async def http_get(url: str, timeout: int = 15, headers: dict[str, str] | None = None) -> bytes:
    return await _blocking(_http_get, url, None, timeout, headers)


SOURCE_TIMEOUT = 8  # seconds per quote-feed request: working feeds answer in < 1 s, a dead one must not cost 15


class SourceHealth:
    """Per-host cooldown for quote feeds. A host that keeps failing (e.g. Eastmoney dropping or 502-ing
    requests from an overseas server) is moved behind the other sources of the same data for a while,
    instead of costing a timeout at the front of every refresh. It is still tried when nothing else
    answers, and it returns to its normal place once the cooldown ends or any request to it succeeds."""
    FAILS = 2           # consecutive failures before a host is moved to the back
    BASE = 60           # first cooldown (seconds), doubled per further failure
    MAX = 600           # short enough that a recovered preferred feed (A50 futures vs the CFD) is back soon

    def __init__(self) -> None:
        self.hosts: dict[str, list] = {}  # host -> [consecutive failures, cool until (monotonic), last error]

    @staticmethod
    def host(url: str) -> str:
        return urllib.parse.urlsplit(url).hostname or url

    def cooling(self, url: str) -> float:
        state = self.hosts.get(self.host(url))
        return max(0.0, state[1] - time.monotonic()) if state else 0.0

    def record(self, url: str, error: str = "") -> None:
        host = self.host(url)
        if not error:
            self.hosts.pop(host, None)
            return
        state = self.hosts.setdefault(host, [0, 0.0, ""])
        state[0] += 1
        state[2] = error
        if state[0] >= self.FAILS:
            state[1] = time.monotonic() + min(self.MAX, self.BASE * 2 ** (state[0] - self.FAILS))

    def order(self, sources: Any) -> list:
        """(name, url, ...) tuples with cooling hosts moved to the back (the most-failed last), otherwise
        in preference order."""
        def rank(source: Any) -> int:
            state = self.hosts.get(self.host(source[1]))
            return state[0] if state and self.cooling(source[1]) > 0 else 0
        return sorted(sources, key=rank)

    def lines(self) -> list[str]:
        now = time.monotonic()
        return [f"  ⏸️ {host}：连续失败 {n} 次，排到最后还剩 {int(until - now)} 秒｜{brief_error(error, 60)}"
                for host, (n, until, error) in sorted(self.hosts.items()) if until > now]


SOURCE_HEALTH = SourceHealth()


async def fetch_source(url: str, extra: dict[str, str] | None = None, record: bool = True) -> bytes:
    """GET one quote-feed URL with a browser UA and the short feed timeout, recording the host's health
    (``record=False`` for a diagnostic probe, which must not reorder the sources the live refresh uses)."""
    try:
        raw = await http_get(url, timeout=SOURCE_TIMEOUT, headers={"User-Agent": BROWSER_UA, "Accept": "*/*", **(extra or {})})
    except (RemoteError, TimeoutError, OSError) as error:
        if record:
            SOURCE_HEALTH.record(url, clean_error(error) or type(error).__name__)
        raise
    if record:
        SOURCE_HEALTH.record(url)
    return raw


async def pick_quote(sources: Any, parse: Any, problem: Any) -> tuple[Any, list[str], str]:
    """Try ``sources`` (name, url, headers) in preference order (hosts in cooldown last) until one answers with a
    quote that stands for its market now (``problem(quote) == ""``). An answer that parses but is stale or undated
    does not end the search. Returns (the current quote, else the parsed one with the newest market time, else
    None; why each source before it was passed over; why nothing current was found, "" when something was)."""
    notes, best = [], None
    for name, url, extra in SOURCE_HEALTH.order(sources):
        try:
            quote = parse(name, await fetch_source(url, extra))
        except Exception as error:
            notes.append(f"{name}: {clean_error(error) or type(error).__name__}")
            continue
        why = problem(quote)
        if not why:
            return quote, notes, ""
        notes.append(f"{name}: {why}")
        if best is None or quote.quoted_ms > best.quoted_ms:
            best = quote
    return best, notes, "；".join(notes)


def newer(quote: Any, previous: Any) -> Any:
    """The quote with the later market time (the new one on a tie); either may be None."""
    if quote is None or (previous is not None and previous.quoted_ms > quote.quoted_ms):
        return previous
    return quote


@dataclass(frozen=True)
class Refreshed:
    """What one background refresh achieved. ok: valid new data for every part; partial: for some parts; failed:
    for none (every part kept its last good data). A refresh that was not due returns False instead."""
    status: str
    error: str = ""


def refreshed(errors: list[str], got: int) -> Refreshed:
    """Outcome of a refresh with ``got`` parts that brought valid new data and the given errors."""
    errors = [error for error in errors if error]
    if not errors:
        return Refreshed("ok")
    return Refreshed("partial" if got else "failed", "；".join(dict.fromkeys(errors)))


@dataclass(frozen=True)
class Quote:
    """The price the bot judges by: the last trade when fresh, else Binance's mark price.

    Thin contracts (e.g. HK0625USDT after the HK session) can go minutes without a trade while
    the mark price keeps updating every second, so a quiet book no longer looks like a dead feed.
    """
    price: D
    timestamp_ms: int
    source: str = "last"        # "last" = 最新成交价, "mark" = 标记价
    last_price: D | None = None  # The stale last trade, kept for display when source == "mark".
    last_ms: int = 0
    index_price: D | None = None  # Binance's index (its view of the underlying), for cross-checking.

    @property
    def kind(self) -> str:
        return "标记价" if self.source == "mark" else "最新成交"

    @staticmethod
    def _timestamp(row: dict, key: str, now_ms: int) -> int:
        try:
            timestamp = int(row[key])
        except (KeyError, TypeError, ValueError):
            raise ValueError("行情缺少有效时间戳，停止涨跌提醒") from None
        if timestamp <= 0 or timestamp > now_ms + 30_000:
            raise ValueError("行情时间戳异常，停止涨跌提醒")
        return timestamp

    @classmethod
    def parse(cls, row: dict, symbol: str, now_ms: int, max_age: int) -> "Quote":
        if row.get("symbol") != symbol:
            raise ValueError(f"行情代码不匹配：{symbol}")
        last = number(row.get("price"), "最新成交价")
        last_ms = cls._timestamp(row, "time", now_ms)
        last_age = (now_ms - last_ms) / 1000
        index = None
        with contextlib.suppress(ValueError):  # Optional diagnostic; never blocks a quote.
            index = number(row.get("indexPrice"), "指数价") if row.get("indexPrice") is not None else None
        if last_age <= max_age:
            return cls(last, last_ms, index_price=index)
        if row.get("markPrice") is None:
            raise ValueError(f"最新成交价已过期（{int(last_age)} 秒未更新），暂不发涨跌提醒")
        mark = number(row.get("markPrice"), "标记价")
        mark_ms = cls._timestamp(row, "markTime", now_ms)
        mark_age = (now_ms - mark_ms) / 1000
        if mark_age > max_age:
            raise ValueError(f"最新成交价 {int(last_age)} 秒、标记价 {int(mark_age)} 秒未更新，暂不发涨跌提醒")
        return cls(mark, mark_ms, "mark", last, last_ms, index)

    def price_row(self, now_ms: int) -> str:
        """'币安 73.20｜指数 73.205｜15:53:05（54 秒前）', noting when the mark price stands in."""
        note = ""
        if self.source == "mark":
            idle = max(0, int((now_ms - self.last_ms) / 1000))
            note = f"（标记价·{idle} 秒无成交）"
        index = f"｜指数 {fmt_price(self.index_price)}" if self.index_price is not None else ""
        age = max(0, int((now_ms - self.timestamp_ms) / 1000))
        return f"币安 {bold(fmt_price(self.price))}{note}{index}｜{stamp(self.timestamp_ms)}（{age} 秒前）"


@dataclass(frozen=True)
class Baseline:
    value: D
    key: str
    label: str
    valid_until_ms: int
    close_ms: int = 0  # When the close behind this baseline happened; 0 = unknown.
    currency: str = ""  # Price unit when it differs from the contract's quote unit (reference prices only).
    source: str = ""    # Where an automatically fetched reference price came from, e.g. "上交所688836·腾讯".
    close_note: str = ""  # Preformatted close-time text (venue local + Beijing); empty = derive from close_ms.
    prev_value: D | None = None  # The session before this close (exchange references), for the stock's own day move.

    @property
    def close_text(self) -> str:
        return self.close_note or close_label(self.close_ms)


def daily_baseline(rows: Any, now_ms: int) -> Baseline:
    """Require precisely the immediately previous UTC day; never use an older bar."""
    boundary = now_ms // DAY_MS * DAY_MS
    previous_start = boundary - DAY_MS
    if not isinstance(rows, list):
        raise ValueError("日 K 数据格式异常")
    for row in rows:
        if not isinstance(row, list) or len(row) < 7:
            continue
        try:
            opening, closing = int(row[0]), int(row[6])
        except (ValueError, TypeError):
            continue
        if opening == previous_start and closing == boundary - 1 and closing < now_ms:
            value = number(row[4], "上一日收盘价")
            day = dt.datetime.fromtimestamp(opening / 1000, UTC).date().isoformat()
            return Baseline(value, f"daily:{day}:{value}",
                            f"币安日 K 昨收｜{day}（UTC 日）｜收盘 {stamp(boundary, seconds=False)}（北京时间，即 UTC 00:00）",
                            boundary + DAY_MS, boundary)
    raise ValueError("没有完整的上一 UTC 日日 K（可能新上市或接口数据未就绪）；不使用旧基准")


# Two kinds of user-entered daily prices share one record format and one command grammar:
#   manual   -> the alert baseline in manual mode (same quote unit as the Binance contract)
#   exchange -> the securities exchange's close in its own currency (HKD, KRW, ...), for reference
PRICE_KINDS = {"manual": ("手动参考价", "/setclose", ""),
               "exchange": ("证券交易所收盘价", "/setexchange", "相对交易所")}
REFERENCE_KINDS = ("exchange",)  # Shown next to the baseline, never used for triggering.


def manual_baseline(record: dict | None, now_ms: int, kind: str = "manual") -> Baseline:
    label, command, _ = PRICE_KINDS[kind]
    today = beijing_day(now_ms / 1000)
    if not record or record.get("valid_date") != today:
        raise ValueError(f"缺少 {today} 的{label}；请用 {command} 设置，不能沿用过期价格")
    value = number(record.get("value"), label)
    until = dt.datetime.combine(dt.date.fromisoformat(today) + dt.timedelta(days=1),
                                dt.time(), BEIJING)
    close_ms = 0
    with contextlib.suppress(ValueError, TypeError):  # Older records have no close time.
        close_ms = int(dt.datetime.fromisoformat(str(record.get("close_at"))).replace(tzinfo=BEIJING).timestamp() * 1000)
    currency = str(record.get("currency") or "").upper()
    return Baseline(value, f"{kind}:{today}:{value}",
                    f"{label}｜适用日 {today}（北京时间）" + close_label(close_ms),
                    int(until.timestamp() * 1000), close_ms, currency)


def close_when(ref: "Baseline") -> str:
    """Beijing close time such as '09-23 14:30'."""
    return stamp(ref.close_ms, seconds=False) if ref.close_ms else "上一交易日"


def reference_row(kind: str, price: D, ref: Baseline | None, fx: "FxRates | None", style: str,
                  unit_note: str = "") -> str:
    """'交易所 490.97 CNY ≈ 73.278（09-23 15:00 收·腾讯）→ 🟢 -0.11%｜当日 🔴 +0.36%'."""
    label, command, _ = PRICE_KINDS[kind]
    label = "交易所" if kind == "exchange" else label
    if ref is None:
        return f"{label} 未设置（{command}）"
    when = f"{stamp(ref.close_ms, seconds=False)} 收" if ref.close_ms else "上一交易日"
    meta = "·".join(x for x in (when, short_source(ref.source)) if x)
    day = f"｜当日 {pct_text(percent(ref.value, ref.prev_value), style)}" if ref.prev_value else ""
    if ref.currency in SAME_UNIT:
        unit = f" {ref.currency}" if ref.currency else ""
        return f"{label} {bold(fmt(ref.value) + unit)}（{meta}）→ {pct_text(percent(price, ref.value), style)}{unit_note}{day}"
    rate = fx.rate(ref.currency) if fx else None
    shown = bold(f"{fmt(ref.value)} {ref.currency}")
    if rate is None:
        return f"{label} {shown}（{meta}）→ ⚪ 无 {ref.currency} 汇率{day}"
    usd = (ref.value / rate).quantize(D("0.001"))
    return f"{label} {shown} ≈ {fmt(usd)}（{meta}）→ {pct_text(percent(price, usd), style)}{day}"


class Binance:
    def __init__(self, config: Config):
        self.config = config
        self.cached: dict[str, Baseline] = {}
        self.at_cache: dict[tuple[str, int], tuple[D, str]] = {}
        self.offset_ms = 0
        self.clock_checked = 0.0
        self.blocked_until = 0.0

    def now_ms(self) -> int:
        return int(time.time() * 1000) + self.offset_ms

    async def get(self, path: str, **params: Any) -> Any:
        if time.monotonic() < self.blocked_until:
            raise RemoteError("币安接口冷却中，暂停请求", int(self.blocked_until - time.monotonic()) + 1)
        query = "?" + urllib.parse.urlencode(params) if params else ""
        try:
            data = await http_json(self.config.base_url + path + query)
        except RemoteError as error:
            if error.retry_after:
                self.blocked_until = max(self.blocked_until, time.monotonic() + error.retry_after)
            raise
        if isinstance(data, dict) and isinstance(data.get("code"), int) and data["code"] < 0:
            raise RemoteError(f"币安错误 {data['code']}: {clean_error(data.get('msg', '请求失败'))}")
        return data

    async def sync_clock(self) -> None:
        if time.monotonic() - self.clock_checked < 600:
            return
        start = int(time.time() * 1000)
        data = await self.get("/fapi/v1/time")
        end = int(time.time() * 1000)
        try:
            server = int(data["serverTime"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("币安服务器时间格式异常") from None
        self.offset_ms = server - ((start + end) // 2)
        self.clock_checked = time.monotonic()

    async def prices(self) -> dict[str, dict]:
        """Last trade per symbol, plus the mark price (markPrice/markTime) when the index feed answers."""
        data, marks = await asyncio.gather(self.get("/fapi/v2/ticker/price"),
                                           self.get("/fapi/v1/premiumIndex"), return_exceptions=True)
        if isinstance(data, BaseException):
            raise data
        if not isinstance(data, list):
            raise ValueError("币安最新价接口没有返回合约列表")
        rows = {r["symbol"]: dict(r) for r in data if isinstance(r, dict) and r.get("symbol") in self.config.symbols}
        if isinstance(marks, list):  # Optional: a mark-price outage must not stop last-trade alerts.
            for mark in marks:
                if isinstance(mark, dict) and mark.get("symbol") in rows and mark.get("markPrice") is not None:
                    rows[mark["symbol"]].update(markPrice=mark["markPrice"], markTime=mark.get("time"),
                                                indexPrice=mark.get("indexPrice"))
        elif isinstance(marks, BaseException):
            LOG.debug("premiumIndex unavailable: %s", clean_error(marks))
        return rows

    async def price_at(self, symbol: str, at_ms: int) -> tuple[D, str]:
        """The contract price at the instant ``at_ms``: close of the 1-minute candle ending then.

        Thin contracts may have no trade in that minute, so the mark-price candle is the fallback.
        Returns (price, "成交价" | "标记价"). Cached per symbol and instant.
        """
        key = (symbol, at_ms)
        if key in self.at_cache:
            return self.at_cache[key]
        start = at_ms - 60_000
        for path, kind in (("/fapi/v1/klines", "成交价"), ("/fapi/v1/markPriceKlines", "标记价")):
            rows = await self.get(path, symbol=symbol, interval="1m", startTime=start, endTime=at_ms - 1, limit=1)
            if not isinstance(rows, list) or not rows or not isinstance(rows[0], list) or len(rows[0]) < 6:
                continue
            row = rows[0]
            try:
                if int(row[0]) != start or (kind == "成交价" and D(str(row[5])) <= 0):
                    continue  # Wrong candle, or no trade in that minute.
                result = number(row[4], "币安收盘时刻价格"), kind
            except (ValueError, TypeError, decimal.InvalidOperation):
                continue
            self.at_cache = {k: v for k, v in self.at_cache.items() if k[0] != symbol}  # one instant per symbol
            self.at_cache[key] = result
            return result
        raise ValueError(f"币安没有 {stamp(at_ms, seconds=False)}（北京时间）那一分钟的 K 线")

    async def baseline(self, symbol: str, now_ms: int) -> Baseline:
        old = self.cached.get(symbol)
        if old and old.valid_until_ms - DAY_MS <= now_ms < old.valid_until_ms:
            return old
        boundary = now_ms // DAY_MS * DAY_MS
        rows = await self.get("/fapi/v1/klines", symbol=symbol, interval="1d", endTime=boundary - 1, limit=3)
        baseline = daily_baseline(rows, now_ms)
        self.cached[symbol] = baseline
        return baseline


def parse_daily_bars(market: str, raw: bytes) -> list[tuple[dt.date, D]]:
    """(date, close) per daily bar, oldest first, from the market's data source."""
    return [(day, close) for day, _, close in parse_daily_ohlc(market, raw)]


def parse_daily_ohlc(market: str, raw: bytes) -> list[tuple[dt.date, D | None, D]]:
    """(date, open or None, close) per daily bar, oldest first, from the market's data source."""
    bars: list[tuple[dt.date, D | None, D]] = []
    if market == "kr":
        # Naver: <item data="20260917|open|high|low|close|volume" /> (EUC-KR page, digits are ASCII)
        for date, opening, close in re.findall(rb'data="(\d{8})\|([^|"]*)\|[^|"]*\|[^|"]*\|([0-9.]+)\|', raw):
            bars.append((dt.datetime.strptime(date.decode(), "%Y%m%d").date(), _open_price(opening.decode()),
                         number(close.decode(), "收盘价")))
    else:
        # Eastmoney: {"data": {"klines": ["2026-09-17,open,close,high,low,...", ...]}}
        try:
            klines = json.loads(raw)["data"]["klines"]
        except (ValueError, KeyError, TypeError):
            raise ValueError("日 K 接口返回格式异常（代码可能不存在）") from None
        for line in klines:
            parts = str(line).split(",")
            if len(parts) >= 3:
                bars.append((dt.date.fromisoformat(parts[0]), _open_price(parts[1]), number(parts[2], "收盘价")))
    if not bars:
        raise ValueError("日 K 接口没有返回任何交易日")
    return bars


def yahoo_url(symbol: str, span: str = "15d") -> str:
    return f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range={span}&interval=1d"


def parse_yahoo_daily(raw: bytes) -> list[tuple[dt.date, D | None, D]]:
    """Yahoo v8 chart (interval=1d) -> (exchange-local date, open, close), oldest first. Yahoo's .KS / ^KS11 bars are
    the KRX regular session only (no Nextrade hours); ^KS11 is also what Predict's KOSPI markets settle on."""
    try:
        result = json.loads(raw)["chart"]["result"][0]
        offset = int(result["meta"].get("gmtoffset") or 0)
        quote = result["indicators"]["quote"][0]
        stamps, closes, opens = result["timestamp"], quote["close"], quote.get("open") or []
    except (ValueError, KeyError, IndexError, TypeError):
        raise ValueError("Yahoo 日 K 返回格式异常") from None
    bars = []
    for i, ts in enumerate(stamps):
        close = closes[i] if i < len(closes) else None
        if close is None:
            continue
        day = dt.datetime.fromtimestamp(int(ts) + offset, dt.timezone.utc).date()
        bars.append((day, _open_price(str(opens[i])) if i < len(opens) and opens[i] is not None else None,
                     D(str(close)).quantize(D("0.01"))))
    if not bars:
        raise ValueError("Yahoo 日 K 没有返回任何交易日")
    return sorted(bars)


def _open_price(text: str) -> D | None:
    """A daily bar's open, or None when missing / not a positive number (it only feeds volatility)."""
    try:
        value = D(str(text).strip())
    except decimal.InvalidOperation:
        return None
    return value if value.is_finite() and value > 0 else None


def finished_bars(bars: list[tuple], market: str, now_ms: int) -> list[tuple]:
    """Daily bars (date first) whose session has ended: today's bar counts only 15 minutes after the close."""
    info = STOCK_MARKETS[market]
    tz = dt.timezone(dt.timedelta(hours=info.utc_offset))
    local = dt.datetime.fromtimestamp(now_ms / 1000, tz)
    done = local >= dt.datetime.combine(local.date(), CALENDAR.close_time(market, local.date()), tz) + dt.timedelta(minutes=15)
    return [bar for bar in bars if bar[0] < local.date() or (bar[0] == local.date() and done)]


def last_completed_bar(bars: list[tuple[dt.date, D]], info: StockMarketInfo,
                       now_ms: int) -> tuple[dt.date, D, D | None]:
    """Newest bar whose session has ended (today's counts only 15 minutes after the close),
    plus the close of the bar before it when known."""
    tz = dt.timezone(dt.timedelta(hours=info.utc_offset))
    local = dt.datetime.fromtimestamp(now_ms / 1000, tz)
    final_from = dt.datetime.combine(local.date(), CALENDAR.close_time_for(info, local.date()), tz) + dt.timedelta(minutes=15)
    ordered = sorted(bars, reverse=True)
    for index, (day, close) in enumerate(ordered):
        if day < local.date() or (day == local.date() and local >= final_from):
            return day, close, ordered[index + 1][1] if index + 1 < len(ordered) else None
    raise ValueError("还没有已完结的交易日")


def parse_quote_close(source: str, market: str, raw: bytes, info: StockMarketInfo,
                      now_ms: int) -> tuple[dt.date | None, D]:
    """Tencent/Sina realtime quotes give the last price, previous close and quote time.

    After the session is final the last price is that day's close; while a session runs the
    previous close is the latest completed one (its exact date is unknown, hence None).
    """
    text = raw.decode("gbk", errors="ignore")
    match = re.search(r'="([^"]*)"', text)
    if not match or not match.group(1).strip():
        raise ValueError(f"{source}行情为空（代码可能不存在）")
    fields = match.group(1).split("~" if source == "腾讯" else ",")
    try:
        if source == "腾讯":
            current, previous, when = fields[3], fields[4], fields[30]
            digits = re.sub(r"\D", "", when)[:12]  # 20260918150003 or 2026/09/18 16:08:11
            quoted = dt.datetime.strptime(digits[:12], "%Y%m%d%H%M")
        elif market == "hk":  # Sina rt_hk: ..., prev[3], ..., current[6], ..., date[17], time[18]
            current, previous = fields[6], fields[3]
            quoted = dt.datetime.strptime(f"{fields[17]} {fields[18]}", "%Y/%m/%d %H:%M:%S")
        else:  # Sina A-share: name, open, prev[2], current[3], ..., date[30], time[31]
            current, previous = fields[3], fields[2]
            quoted = dt.datetime.strptime(f"{fields[30]} {fields[31]}", "%Y-%m-%d %H:%M:%S")
    except (IndexError, ValueError):
        raise ValueError(f"{source}行情格式异常") from None
    tz = dt.timezone(dt.timedelta(hours=info.utc_offset))
    local = dt.datetime.fromtimestamp(now_ms / 1000, tz)
    final_from = dt.datetime.combine(local.date(), CALENDAR.close_time_for(info, local.date()), tz) + dt.timedelta(minutes=15)
    if quoted.date() < local.date() or (quoted.date() == local.date() and local >= final_from):
        return quoted.date(), number(current, "收盘价"), number(previous, "昨收价")
    return None, number(previous, "昨收价"), None


def stock_live_window(market: str, now_ms: int, holidays: frozenset = frozenset()) -> tuple[int, int] | None:
    """(open, final) epoch ms of today's session while the stock trades, else None.

    Runs from the first continuous-trading minute until the close is final (close time + 15 minutes):
    the pre-open auction's indicative prices are not trades (stock_quote_window covers them, labelled).
    """
    info = STOCK_MARKETS[market]
    tz = dt.timezone(dt.timedelta(hours=info.utc_offset))
    local = dt.datetime.fromtimestamp(now_ms / 1000, tz)
    today = local.date()
    if today.weekday() >= 5 or today in holidays:
        return None
    start = dt.datetime.combine(today, CALENDAR.sessions(market, today)[0][0], tz)
    final = dt.datetime.combine(today, CALENDAR.close_time(market, today), tz) + dt.timedelta(minutes=15)
    if not start <= local < final:
        return None
    return int(start.timestamp() * 1000), int(final.timestamp() * 1000)


def stock_quote_window(market: str, now_ms: int, holidays: frozenset = frozenset()) -> tuple[int, int] | None:
    """(start, final) epoch ms of the stretch in which the stock's own quote is read: from the pre-open auction's
    start (its indicative price, HKEX's 参考平衡价, is the day's first direction signal; the matched print follows)
    until the close is final. A market without a pre-open auction in PRE_AUCTIONS starts at its open."""
    info = STOCK_MARKETS[market]
    tz = dt.timezone(dt.timedelta(hours=info.utc_offset))
    local = dt.datetime.fromtimestamp(now_ms / 1000, tz)
    today = local.date()
    if today.weekday() >= 5 or today in holidays:
        return None
    start = dt.datetime.combine(today, CALENDAR.sessions(market, today)[0][0], tz)
    window = preopen_window(market, now_ms)
    if window:  # the table keeps Beijing time; the venue's own day is the same day
        start = min(start, dt.datetime.combine(dt.datetime.fromtimestamp(now_ms / 1000, BEIJING).date(), window[0], BEIJING).astimezone(tz))
    final = dt.datetime.combine(today, CALENDAR.close_time(market, today), tz) + dt.timedelta(minutes=15)
    if not start <= local < final:
        return None
    return int(start.timestamp() * 1000), int(final.timestamp() * 1000)


def split_quote_records(source: str, raw: bytes) -> dict[str, str]:
    """A Tencent / Sina answer for several codes -> {code: record text}: 'v_sh688836="..."; v_r_hk00625="..."' and
    'var hq_str_sh688836="..."; var hq_str_rt_hk00625="..."'. A code the feed does not know comes back empty."""
    prefix = "v_" if source == "腾讯" else "hq_str_"
    text = raw.decode("gbk", errors="ignore")
    return {name.removeprefix(prefix): body for name, body in re.findall(r'(\w+)="([^"]*)"', text)}


def parse_stock_live(source: str, market: str, raw: bytes, now_ms: int) -> IndexQuote:
    """Realtime stock quote -> IndexQuote(last, previous close, quote time). Naver for KRX, Tencent/Sina otherwise."""
    if source == "Naver":
        q = parse_naver_index(raw, now_ms)
        return dataclasses.replace(q, source="Naver")
    text = raw.decode("gbk", errors="ignore")
    match = re.search(r'="([^"]*)"', text)
    if not match or not match.group(1).strip():
        raise ValueError(f"{source}行情为空（代码可能不存在）")
    return parse_stock_record(source, market, match.group(1), now_ms)


def parse_stock_record(source: str, market: str, record: str, now_ms: int) -> IndexQuote:
    """One Tencent ('~'-separated) or Sina (','-separated) quote record -> IndexQuote."""
    if not record.strip():
        raise ValueError(f"{source}行情为空（代码可能不存在）")
    fields = record.split("~" if source == "腾讯" else ",")
    try:
        if source == "腾讯":
            name, current, previous, opened = fields[1], fields[3], fields[4], fields[5]
            digits = re.sub(r"\D", "", fields[30])[:14]
            quoted = dt.datetime.strptime(digits[:12], "%Y%m%d%H%M")
            if len(digits) == 14:
                quoted = quoted.replace(second=int(digits[12:]))
        elif market == "hk":
            name, current, previous, opened = fields[1], fields[6], fields[3], fields[2]
            quoted = dt.datetime.strptime(f"{fields[17]} {fields[18]}", "%Y/%m/%d %H:%M:%S")
        else:
            name, current, previous, opened = fields[0], fields[3], fields[2], fields[1]
            quoted = dt.datetime.strptime(f"{fields[30]} {fields[31]}", "%Y-%m-%d %H:%M:%S")
    except (IndexError, ValueError):
        raise ValueError(f"{source}行情格式异常") from None
    last = number(current, "现价")
    if last <= 0:
        raise ValueError(f"{source}现价为 0（尚未成交或停牌）")
    tz = dt.timezone(dt.timedelta(hours=STOCK_MARKETS[market].utc_offset))
    return IndexQuote(name, last, _opt(previous), _opt(opened), None, None,
                      int(quoted.replace(tzinfo=tz).timestamp() * 1000), source)


class StockMarket:
    """Fetches each contract's underlying stock close from public quote feeds.

    Sources are tried in order with a retry each; the last good close is persisted so a
    restart or a flaky feed does not blank the reference line. Read-only and best effort.
    """
    REFRESH_SECONDS = 600
    ATTEMPTS = 2

    def __init__(self, config: Config, store: "Store | None" = None):
        self.config = config
        self.store = store
        self.closes: dict[str, Baseline] = {}
        self.errors: dict[str, str] = {}
        self.notes: dict[str, str] = {}  # ticker code -> a caveat about the close that is not a failure (KRX correction)
        self.refreshed = -1e9
        self.live: dict[str, IndexQuote] = {}   # realtime stock quote while its session runs
        self.live_errors: dict[str, str] = {}
        self.live_refreshed = -1e9
        for symbol in config.tickers:
            saved = store.get(f"stock_close:{symbol}") if store else None
            if saved:
                with contextlib.suppress(Exception):
                    self.closes[symbol] = Baseline(number(saved["value"], "收盘价"), saved["key"], saved["label"],
                                                   int(saved["valid_until_ms"]), int(saved["close_ms"]),
                                                   saved.get("currency", ""), saved.get("source", ""),
                                                   # Re-derive: older versions stored venue-local text.
                                                   STOCK_MARKETS[config.tickers[symbol].market].close_label(int(saved["close_ms"])),
                                                   D(saved["prev_value"]) if saved.get("prev_value") else None)

    @staticmethod
    def sources(ticker: StockTicker) -> list[tuple[str, str, dict[str, str]]]:
        """(name, url, extra headers) in preference order."""
        code = urllib.parse.quote(ticker.code)
        if ticker.market == "kr":
            return [("Yahoo", yahoo_url(f"{ticker.code}.KS"), {}),
                    ("Naver", f"https://fchart.stock.naver.com/sise.nhn?requestType=0&timeframe=day&count=10&symbol={code}",
                     {"Referer": "https://finance.naver.com/"})]
        secid = {"sh": "1", "sz": "0", "hk": "116"}[ticker.market] + "." + ticker.code
        sina = ("rt_hk" if ticker.market == "hk" else ticker.market) + ticker.code
        tencent = ("r_hk" if ticker.market == "hk" else ticker.market) + code  # plain hkXXXXX is 15 minutes delayed
        return [
            ("东方财富", "https://push2his.eastmoney.com/api/qt/stock/kline/get?klt=101&fqt=0&end=20500101&lmt=10"
                         "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56&secid=" + urllib.parse.quote(secid),
             {"Referer": "https://quote.eastmoney.com/"}),
            ("腾讯", f"https://qt.gtimg.cn/q={tencent}", {"Referer": "https://gu.qq.com/"}),
            ("新浪", f"https://hq.sinajs.cn/list={sina}", {"Referer": "https://finance.sina.com.cn/"}),
        ]

    @staticmethod
    def live_code(source: str, ticker: StockTicker) -> str:
        """The code a realtime feed knows the stock by: Tencent r_hk00625 / sh688836 (plain hkXXXXX is 15 minutes
        delayed), Sina rt_hk00625 / sh688836."""
        if source == "腾讯":
            return ("r_hk" if ticker.market == "hk" else ticker.market) + ticker.code
        return ("rt_hk" if ticker.market == "hk" else ticker.market) + ticker.code

    @classmethod
    def live_batch_sources(cls, tickers: list[StockTicker]) -> list[tuple[str, str, dict[str, str]]]:
        """(name, url, headers) asking Tencent, then Sina, for every A-share / HK ticker in one request."""
        codes = lambda source: ",".join(dict.fromkeys(urllib.parse.quote(cls.live_code(source, t)) for t in tickers))
        return [("腾讯", f"https://qt.gtimg.cn/q={codes('腾讯')}", {"Referer": "https://gu.qq.com/"}),
                ("新浪", f"https://hq.sinajs.cn/list={codes('新浪')}", {"Referer": "https://finance.sina.com.cn/"})]

    @classmethod
    def live_sources(cls, ticker: StockTicker) -> list[tuple[str, str, dict[str, str]]]:
        if ticker.market == "kr":
            return [("Naver", f"https://polling.finance.naver.com/api/realtime/domestic/stock/{urllib.parse.quote(ticker.code)}",
                     {"Referer": "https://finance.naver.com/"})]
        return cls.live_batch_sources([ticker])

    @staticmethod
    def vol_sources(ticker: StockTicker) -> list[tuple[str, str, dict[str, str], str]]:
        """(name, url, headers, format) of ~40 daily bars with opens: the stock's own exchange sessions, for its σ."""
        code = urllib.parse.quote(ticker.code)
        if ticker.market == "kr":  # Yahoo's .KS bars are the KRX session only; Naver's chart blends in NXT after hours
            return [("Yahoo", yahoo_url(f"{ticker.code}.KS", "3mo"), {}, "yahoo"),
                    ("Naver", f"https://fchart.stock.naver.com/sise.nhn?requestType=0&timeframe=day&count=40&symbol={code}",
                     {"Referer": "https://finance.naver.com/"}, "kr")]
        secid = {"sh": "1", "sz": "0", "hk": "116"}[ticker.market] + "." + ticker.code
        return [("腾讯", f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={ticker.market}{code},day,,,40,qfq",
                 {"Referer": "https://gu.qq.com/"}, "tencent"),
                ("东方财富", "https://push2his.eastmoney.com/api/qt/stock/kline/get?klt=101&fqt=1&end=20500101&lmt=40"
                            "&fields1=f1,f2,f3&fields2=f51,f52,f53&secid=" + urllib.parse.quote(secid),
                 {"Referer": "https://quote.eastmoney.com/"}, ticker.market)]

    async def daily_ohlc(self, ticker: StockTicker, now_ms: int) -> tuple[list[tuple[dt.date, D | None, D]], str]:
        """The stock's finished daily bars (date, open, close), oldest first, and their source; raises when none answer."""
        failures = []
        for name, url, extra, kind in SOURCE_HEALTH.order(self.vol_sources(ticker)):
            try:
                raw = await fetch_source(url, extra)
                bars = (parse_yahoo_daily(raw) if kind == "yahoo" else DailyCloses.parse(kind, ticker.market, raw)
                        if kind == "tencent" else parse_daily_ohlc(kind, raw))
                bars = finished_bars(sorted(bars, key=lambda bar: bar[0]), ticker.market, now_ms)
                if len(bars) < 3:
                    raise ValueError(f"只有 {len(bars)} 根已完结日 K")
                return bars, name
            except Exception as error:
                failures.append(f"{name}: {clean_error(error) or type(error).__name__}")
        raise ValueError("；".join(failures))

    LIVE_STALE_MS = 10 * 60_000

    async def refresh_live(self, now_ms: int, force: bool = False) -> Refreshed | bool:
        """Realtime quotes, only for the stocks whose session is running now, every QUOTE_REFRESH_SECONDS (1 s by
        default). The A-share / HK stocks share one request per source (Tencent, then Sina for the ones it left
        stale or unknown); each Korean stock is one Naver request. A lagging or undated feed (delayed quotes) does
        not end the search, and the round keeps the newest print it saw."""
        due = {symbol: ticker for symbol, ticker in self.config.tickers.items()
               if stock_quote_window(ticker.market, now_ms, self.config.holidays.get(ticker.market, frozenset()))}
        if not due or (not force and time.monotonic() - self.live_refreshed < self.config.quote_refresh):
            return False  # nothing trading / not due: nothing fetched
        self.live_refreshed = time.monotonic()
        failures: dict[str, list[str]] = {symbol: [] for symbol in due}
        fresh: set[str] = set()
        newest: dict[str, IndexQuote] = {}

        def consider(symbol: str, name: str, q: IndexQuote) -> None:
            if symbol not in newest or q.quoted_ms > newest[symbol].quoted_ms:
                newest[symbol] = q
            self.live[symbol] = newest[symbol]
            if self.live_quote(symbol, now_ms)[1]:
                failures[symbol].append(f"{name}: " + (f"报价停在 {stamp(q.quoted_ms, seconds=False)}" if q.quoted_ms else "缺少报价时间"))
            else:
                fresh.add(symbol)

        batch = {symbol: ticker for symbol, ticker in due.items() if ticker.market != "kr"}
        for name, _, extra in SOURCE_HEALTH.order(self.live_batch_sources(list(batch.values()))) if batch else []:
            wanted = {symbol: ticker for symbol, ticker in batch.items() if symbol not in fresh}
            if not wanted:
                break
            url = next(u for n, u, _ in self.live_batch_sources(list(wanted.values())) if n == name)
            try:
                records = split_quote_records(name, await fetch_source(url, extra))
            except Exception as error:
                for symbol in wanted:
                    failures[symbol].append(f"{name}: {clean_error(error)}")
                continue
            for symbol, ticker in wanted.items():
                try:
                    q = parse_stock_record(name, ticker.market, records.get(self.live_code(name, ticker), ""), now_ms)
                except Exception as error:
                    failures[symbol].append(f"{name}: {clean_error(error)}")
                    continue
                consider(symbol, name, q)
        for index, (symbol, ticker) in enumerate((s, t) for s, t in due.items() if t.market == "kr"):
            if index:
                await asyncio.sleep(0.3)  # spread requests out; feeds drop bursts from one IP
            for name, url, extra in SOURCE_HEALTH.order(self.live_sources(ticker)):
                try:
                    q = parse_stock_live(name, ticker.market, await fetch_source(url, extra), now_ms)
                except Exception as error:
                    failures[symbol].append(f"{name}: {clean_error(error)}")
                    continue
                consider(symbol, name, q)
                if symbol in fresh:
                    break
        for symbol in due:
            if symbol in fresh:
                self.live_errors.pop(symbol, None)
            else:
                self.live_errors[symbol] = "；".join(failures[symbol])
        return refreshed([f"{symbol}：{self.live_errors[symbol]}" for symbol in due if symbol in self.live_errors], len(fresh))

    def live_quote(self, symbol: str, now_ms: int) -> tuple[IndexQuote | None, str]:
        """(today's fresh realtime quote, why not) while the stock trades or its pre-open auction runs; (None, "")
        outside. In the auction the quote is the indicative price: it must be stamped today in the auction and have
        moved off the previous close (a feed that still shows yesterday's close at 0% has no indicative price yet),
        and it may sit still for minutes without going stale."""
        ticker = self.config.tickers.get(symbol)
        holidays = self.config.holidays.get(ticker.market, frozenset()) if ticker else frozenset()
        window = ticker and stock_quote_window(ticker.market, now_ms, holidays)
        if not window:
            return None, ""
        q = self.live.get(symbol)
        error = self.live_errors.get(symbol, "")
        if q is None:
            return None, f"现货行情未取得（{brief_error(error, 60)}）" if error else "等待现货行情"
        if q.quoted_ms <= 0:
            return None, "现货行情缺少报价时间"
        preopen = preopen_running(ticker.market, now_ms, holidays)
        if q.quoted_ms < window[0]:  # still yesterday's print (the window opens with the pre-open auction)
            return None, "开市前竞价进行中，行情源还没给出今日参考价" if preopen else "现货今日尚未开盘成交"
        if preopen:
            if q.prev_close and q.last == q.prev_close:
                return None, "开市前竞价进行中，现价仍等于昨收（尚无参考平衡价）"
            return q, ""
        info = STOCK_MARKETS[ticker.market]
        tz = dt.timezone(dt.timedelta(hours=info.utc_offset))
        day = dt.datetime.fromtimestamp(now_ms / 1000, tz).date()
        sessions = CALENDAR.sessions(ticker.market, day)
        lunch = (sessions[0][1], sessions[1][0]) if len(sessions) > 1 else None  # HK/A-share lunch, Beijing time
        end = max(sessions[-1][1], CALENDAR.close_time(ticker.market, day))  # no new prints after the close; don't call its last one stale
        clock = min(now_ms, int(dt.datetime.combine(day, end, tz).timestamp() * 1000) + 60_000)
        if quote_stale(q, clock, self.LIVE_STALE_MS, lunch):
            return None, f"现货行情已超 10 分钟未更新（最后 {stamp(q.quoted_ms, seconds=False)}）"
        return q, ""

    async def fetch(self, symbol: str, ticker: StockTicker, now_ms: int) -> Baseline:
        """The latest close from the first source that has it. A dated answer older than the calendar expects (a
        lagging feed) does not end the search: the next sources are asked, and the newest answer wins."""
        info = STOCK_MARKETS[ticker.market]
        expected = expected_close_date(ticker.market, now_ms, self.config.holidays.get(ticker.market, frozenset()))
        failures, lagging = [], None
        # One pass over every source (hosts in cooldown last); only when all of them failed, one more pass.
        for attempt in range(self.ATTEMPTS):
            if attempt:
                if lagging:
                    break  # an answer came, just an old one: a second pass would not change it
                await asyncio.sleep(1.5)
            for name, url, extra in SOURCE_HEALTH.order(self.sources(ticker)):
                try:
                    raw = await fetch_source(url, extra)
                    if name == "Yahoo":
                        day, close, prev = last_completed_bar([(d, c) for d, _, c in parse_yahoo_daily(raw)], info, now_ms)
                        if ticker.market == "kr":
                            close = close.quantize(D(1))  # whole won
                            prev = prev.quantize(D(1)) if prev is not None else None
                            # today's bar only counts from 15:45: until then a KRX close captured from 15:33 is newer
                            kday, kclose, kprev, kname = await self.krx_official(ticker, day, close, prev, now_ms, name)
                            if kday > day:
                                day, close, prev, name = kday, kclose, kprev, kname
                            elif self.store and (saved := self.store.get(f"krx_close:{ticker.code}:{day.isoformat()}")):
                                # KRX now also trades after hours (to 20:00): the regular-session close captured
                                # 15:33–15:40 is known-good, so it wins should Yahoo's bar carry an after-hours print
                                # (the label moves to Naver only when the figure really changes)
                                with contextlib.suppress(decimal.InvalidOperation, TypeError, IndexError):
                                    if D(saved[0]) != close:
                                        close, prev, name = D(saved[0]), D(saved[1]) if saved[1] else prev, "Naver KRX"
                    elif name in {"东方财富", "Naver"}:
                        day, close, prev = last_completed_bar(parse_daily_bars(ticker.market, raw), info, now_ms)
                        if ticker.market == "kr":
                            chart = "Naver 日K·含 NXT" if name == "Naver" else name  # the chart blends in after-hours trades
                            day, close, prev, name = await self.krx_official(ticker, day, close, prev, now_ms, chart)
                    else:
                        day, close, prev = parse_quote_close(name, ticker.market, raw, info, now_ms)
                    base = self.baseline(ticker, info, name, day, close, prev)
                except Exception as error:
                    failures.append(f"{name}: {clean_error(error)}")
                    continue
                if day is None or day >= expected:
                    return base
                failures.append(f"{name}: 收盘停在 {day:%m-%d}，应有 {expected:%m-%d}")
                if lagging is None or day > lagging[0]:
                    lagging = (day, base)
        if lagging:
            return lagging[1]
        raise ValueError("；".join(dict.fromkeys(failures)))

    async def krx_official(self, ticker: StockTicker, day: dt.date, close: D, prev: D | None,
                           now_ms: int, source: str) -> tuple[dt.date, D, D | None, str]:
        """Naver's daily chart now blends in Nextrade (NXT) after-hours trades up to 20:00, so its "close" drifts
        away from the KRX closing auction. Naver's realtime quote is the KRX regular session: its price is the
        official close once the session is over, and price − change is the official previous close (기준가).
        Best effort: on any failure the incoming values stand. ``source`` names where the incoming close came from:
        another source's close keeps its own name unless the KRX figure really replaces it (a different value or day);
        Naver's own chart figure, once the KRX quote confirms it, is the KRX close."""
        replaced = lambda d, c: source if (d, c) == (day, close) and not source.startswith("Naver") else "Naver KRX"
        try:
            url = self.live_sources(ticker)[0][1]
            q = parse_naver_index(await fetch_source(url, {"Referer": "https://finance.naver.com/"}), now_ms)
        except Exception as error:  # the chart's close stands; /diag shows that the KRX correction was not available
            self.notes[ticker.code] = f"Naver 实时价不可用，收盘未经 KRX 正规时段校正：{clean_error(error) or type(error).__name__}"
            return day, close, prev, source
        self.notes.pop(ticker.code, None)
        if q.quoted_ms <= 0:
            return day, close, prev, source  # an undated quote cannot say which session it belongs to
        kst = dt.timezone(dt.timedelta(hours=9))
        quoted = dt.datetime.fromtimestamp(q.quoted_ms / 1000, kst)
        qday = quoted.date()  # may be newer than the chart's last final bar (today's counts only from 15:45)
        key = f"krx_close:{ticker.code}:{qday.isoformat()}"
        close_time = CALENDAR.close_time("kr", qday)  # 16:30 on the CSAT day, when a 15:33 print is still intraday
        close_ms = int(dt.datetime.combine(qday, close_time, kst).timestamp() * 1000)
        settled = now_ms >= close_ms + CLOSE_SETTLE_MS["kr"]  # the auction's random end and feed lag are past
        after_close = qday >= day and quoted.time() >= close_time
        if after_close and settled and quoted.time() < CALENDAR.kr_time(KRX_NXT_AFTER, qday):
            # between the KRX close and Nextrade's after-hours session the quote is the KRX close: keep it
            if self.store:
                self.store.put(key, [str(q.last), str(q.prev_close or "")])
            return qday, q.last, q.prev_close or prev, replaced(qday, q.last)
        saved = self.store.get(key) if self.store else None
        if after_close and saved:
            # from 15:40 the quote follows NXT after-hours trades: use the KRX close captured before that
            with contextlib.suppress(decimal.InvalidOperation, TypeError, IndexError):
                return qday, D(saved[0]), D(saved[1]) if saved[1] else prev, replaced(qday, D(saved[0]))
        if qday > day and q.prev_close and not after_close:
            # today's 기준가 = the close of the chart's last session
            return day, q.prev_close, prev, replaced(day, q.prev_close)
        return day, close, prev, source

    @staticmethod
    def baseline(ticker: StockTicker, info: StockMarketInfo, source: str, day: dt.date | None, close: D,
                 prev: D | None = None) -> Baseline:
        tz = dt.timezone(dt.timedelta(hours=info.utc_offset))
        close_ms = int(dt.datetime.combine(day, CALENDAR.close_time(ticker.market, day), tz).timestamp() * 1000) if day else 0
        when = str(day) if day else "上一交易日"
        return Baseline(close, f"exchange:{when}:{close}", f"证券交易所收盘价｜{when} {info.name}",
                        (close_ms or int(time.time() * 1000)) + DAY_MS, close_ms,
                        "" if ticker.same_unit else info.currency, f"{info.name}{ticker.code}·{source}",
                        info.close_label(close_ms), prev)

    def remember(self, symbol: str, close: Baseline) -> None:
        if self.store:
            self.store.put(f"stock_close:{symbol}", {
                "value": str(close.value), "key": close.key, "label": close.label, "valid_until_ms": close.valid_until_ms,
                "close_ms": close.close_ms, "currency": close.currency, "source": close.source,
                "close_note": close.close_note, "prev_value": str(close.prev_value) if close.prev_value else ""})

    PENDING_SECONDS = 60      # refresh cadence while a session's close is due but not yet confirmed
    PENDING_WINDOW_MS = 2 * 3600_000  # ...for this long after it was due (then back to the normal cadence)

    def close_pending(self, now_ms: int) -> bool:
        """A close the calendar says should be final by now is not confirmed yet (and is less than two hours overdue):
        the baseline switch must not wait for the 10-minute cadence."""
        for symbol, ticker in self.config.tickers.items():
            holidays = self.config.holidays.get(ticker.market, frozenset())
            expected = expected_close_date(ticker.market, now_ms, holidays)
            info = STOCK_MARKETS[ticker.market]
            tz = dt.timezone(dt.timedelta(hours=info.utc_offset))
            final_ms = int((dt.datetime.combine(expected, CALENDAR.close_time(ticker.market, expected), tz)
                            + dt.timedelta(minutes=15)).timestamp() * 1000)
            known = self.closes.get(symbol)
            known_day = dt.datetime.fromtimestamp(known.close_ms / 1000, tz).date() if known and known.close_ms else None
            if (known_day is None or known_day < expected) and final_ms <= now_ms < final_ms + self.PENDING_WINDOW_MS:
                return True
        return False

    async def refresh(self, now_ms: int, force: bool = False) -> Refreshed | bool:
        if not self.config.tickers:
            return False
        cadence = self.PENDING_SECONDS if self.close_pending(now_ms) else self.REFRESH_SECONDS
        if not force and time.monotonic() - self.refreshed < cadence:
            return False  # not due yet: nothing fetched
        self.refreshed = time.monotonic()
        started, got = time.monotonic(), 0
        for index, (symbol, ticker) in enumerate(self.config.tickers.items()):
            if index:
                await asyncio.sleep(0.5)  # Spread requests out; feeds drop bursts from one IP.
            # each stock is judged at its own moment: a round that straddles the "bar is final" minute must not
            # reject a close for the later stocks with the earlier clock
            now_ms = now_ms + int((time.monotonic() - started) * 1000) if index else now_ms
            try:
                close = await self.fetch(symbol, ticker, now_ms)
            except Exception as error:  # Keep the last good close; report the failure alongside it.
                self.errors[symbol] = clean_error(error)
                continue
            info = STOCK_MARKETS[ticker.market]
            day = dt.datetime.fromtimestamp(close.close_ms / 1000, dt.timezone(dt.timedelta(hours=info.utc_offset))).date()
            expected = expected_close_date(ticker.market, now_ms, self.config.holidays.get(ticker.market, frozenset()))
            if close.close_ms and day < expected:
                self.errors[symbol] = f"收盘只取到 {day:%m-%d}，应有 {expected:%m-%d}（各源未更新或失败）"  # nothing new this round
            else:
                self.errors.pop(symbol, None)
                got += 1
            old = self.closes.get(symbol)
            if not close.close_ms and old and old.close_ms and old.value == close.value:
                close = old  # an undated quote repeating the dated close must not erase its date
            elif close.close_ms and old is not None and old.close_ms > close.close_ms:
                close = old  # a lagging answer never steps the close back to an older session
            self.closes[symbol] = close
            self.remember(symbol, close)
        return refreshed([f"{symbol}：{error}" for symbol, error in self.errors.items() if symbol in self.config.tickers], got)


class FxRates:
    """USD reference rates (units of currency per 1 USD) for converting exchange closes to USD.

    Manual FX_RATES entries always win; fetched rates come from keyless public sources.
    """
    REFRESH_SECONDS = 6 * 3600
    RETRY_SECONDS = 300  # after a failed round (both sources), not six hours later
    SOURCES = (("Frankfurter（欧洲央行参考汇率）",
                "https://api.frankfurter.app/latest?from=USD&to=CNY,HKD,KRW,JPY,EUR,GBP,SGD,INR"),
               ("open.er-api.com", "https://open.er-api.com/v6/latest/USD"))

    def __init__(self, manual: dict[str, D] | None = None):
        self.manual = dict(manual or {})
        self.rates: dict[str, D] = {}
        self.source = ""
        self.updated = ""
        self.error = ""
        self.refreshed = -1e9

    def rate(self, currency: str) -> D | None:
        currency = currency.upper()
        if currency in SAME_UNIT:
            return D(1)
        found = self.manual.get(currency) or self.rates.get(currency)
        if found is None and currency == "CNH":  # Offshore yuan tracks onshore closely enough for display.
            found = self.manual.get("CNY") or self.rates.get("CNY")
        return found

    def summary(self, currencies: tuple[str, ...] = ("CNY", "HKD", "KRW")) -> str:
        """'1 USD = 6.7001 CNY · 7.79 HKD · 1,382.55 KRW（Frankfurter 09-22）'."""
        shown = []
        for currency in currencies:
            rate = self.rate(currency)
            if rate is not None and currency not in SAME_UNIT:
                tag = "手动" if currency in self.manual else ""
                shown.append(f"{fmt(rate.quantize(D('0.0001')))} {currency}{('（' + tag + '）') if tag else ''}")
        parts = []
        if shown:
            parts.append("1 USD = " + " · ".join(shown))
        if self.rates and any(c not in self.manual for c in currencies):
            parts.append(f"{self.source.split('（')[0]} {self.updated[5:10] if len(self.updated) >= 10 else self.updated}".strip())
        if self.error:
            parts.append(f"⚠️ 汇率获取失败：{self.error}")
        return "｜".join(parts) if parts else "汇率尚未获取"

    async def refresh(self, force: bool = False) -> Refreshed | bool:
        if not force and time.monotonic() - self.refreshed < self.REFRESH_SECONDS:
            return False  # not due yet: nothing fetched
        self.refreshed = time.monotonic()
        failures = []
        for name, url in self.SOURCES:
            try:
                data = await http_json(url)
                rates = data.get("rates") if isinstance(data, dict) else None
                if not isinstance(rates, dict):
                    raise ValueError("汇率接口未返回 rates")
                parsed = {str(k).upper(): number(v, f"{k} 汇率") for k, v in rates.items()
                          if str(k).upper() in CURRENCIES and isinstance(v, (int, float, str))}
                if not parsed:
                    raise ValueError("汇率接口没有所需货币")
                self.rates, self.source, self.error = parsed, name, ""
                self.updated = str(data.get("date") or data.get("time_last_update_utc") or "")[:32]
                return Refreshed("ok")
            except Exception as error:
                failures.append(f"{name}: {clean_error(error)}")
        self.error = "；".join(failures)
        self.refreshed = time.monotonic() - self.REFRESH_SECONDS + self.RETRY_SECONDS  # try again soon
        return Refreshed("failed", self.error)


@dataclass(frozen=True)
class FuturesQuote:
    """One index-futures quote (the front/main contract) plus the cash index for the basis.

    quoted_ms is the market's own time for the price (0 = unknown); fetched_ms is when the bot read it. A source
    that prints no time (etnet's live block) gets the time its values were first seen unchanged, so a frozen
    page ages instead of passing for a live one."""
    name: str
    last: D
    prev_settle: D | None
    open: D | None
    high: D | None
    low: D | None
    quoted_ms: int
    source: str
    spot: D | None = None
    spot_source: str = ""
    spot_prev: D | None = None  # Cash index previous close, for the index's own day move.
    water: D | None = None      # Premium/discount as published by the source (etnet), signed.
    exchange_contract: bool = True  # False for CFD fallbacks that are not the HKEX contract.
    session: str = ""           # "日市" / "夜市" when the source says which block this is; else derived from the time
    fetched_ms: int = 0         # when the bot read the futures quote
    spot_ms: int = 0            # market time of the cash index (0 = unknown: the futures page's own time stands in)

    def session_name(self, holidays: frozenset = frozenset()) -> str:
        return self.session or hk_futures_session(self.quoted_ms, holidays)

    @property
    def contract(self) -> str:
        """'10/2026' from etnet's 恒指期货(10/2026)日市; "" when the source does not name the month."""
        found = re.search(r"\((\d{2}/\d{4})\)", self.name)
        return found.group(1) if found else ""

    @property
    def spot_time(self) -> int:
        return self.spot_ms or (self.quoted_ms if self.spot_source in ("", self.source) else 0)

    @property
    def change(self) -> D | None:
        return self.last - self.prev_settle if self.prev_settle else None

    @property
    def basis(self) -> D | None:
        """Futures minus cash index: positive = 高水 (premium), negative = 低水 (discount)."""
        if self.water is not None:
            return self.water
        return self.last - self.spot if self.spot is not None else None


def page_text(raw: bytes) -> str:
    """Visible text of an HTML page: scripts/styles dropped, tags to spaces, entities decoded."""
    for encoding in ("utf-8", "big5hkscs", "big5"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="ignore")
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text)


ETNET_NUM = r"([\d,]+(?:\.\d+)?)"


def parse_etnet_futures(raw: bytes, now_ms: int, holidays: frozenset = frozenset()) -> "FuturesQuote":
    """etnet 指數期貨 page: HKEX HSI futures (日市/夜市 blocks) plus 恒生指數現貨.

    Parsed from visible text so markup changes do not matter. On the live page the block times
    are drawn inside the chart images, not the text, so the newer block is found from the prices:
    a night session that followed the day block opens from the day block's last price (its 前收市),
    while last night's block shares the day block's 前收市. Its published 高水/低水 is kept as the basis.
    """
    text = page_text(raw)
    spot = spot_prev = None
    spot_block = re.search(r"恒生指數現貨(.{0,400})", text)
    if spot_block:
        m = re.search(r"[▲▼]?\s*([\d,]+\.\d+)\s*[+-]?[\d,.]+\s*\(", spot_block.group(1))
        spot = number(m.group(1).replace(",", ""), "恒生指數") if m else None
        m = re.search(r"前收市\s*[:：]\s*" + ETNET_NUM, spot_block.group(1))
        spot_prev = number(m.group(1).replace(",", ""), "恒指前收") if m else None
    candidates = []
    pattern = (r"恒生指數期貨\((\d{2}/\d{4})\)\s*(日市|夜市)(.*?)"
               r"(?=恒生指數期貨\(\d{2}/\d{4}\)\s*(?:日市|夜市)|未平倉|恒生指數現貨|$)")
    for month, session, body in re.findall(pattern, text, flags=re.S):
        m = re.search(r"[▲▼]?\s*([\d,]{4,})\s*([+-]?[\d,]+)\s*\(\s*([+-]?[\d.]+)%\s*\)", body)
        if not m:
            continue
        def field(label: str) -> D | None:
            f = re.search(label + r"\s*[:：]\s*" + ETNET_NUM, body)
            return number(f.group(1).replace(",", ""), label, zero_ok=True) if f else None
        water = None
        w = re.search(r"(高水|低水|平水)\s*(\d+)?", body)
        if w:
            water = D(w.group(2) or 0) * (-1 if w.group(1) == "低水" else 1)
        stamp_match = re.search(r"(\d{4}/\d{2}/\d{2} \d{2}:\d{2})", body)
        quoted_ms = 0
        if stamp_match:
            quoted = dt.datetime.strptime(stamp_match.group(1), "%Y/%m/%d %H:%M").replace(tzinfo=BEIJING)
            quoted_ms = int(quoted.timestamp() * 1000)
        candidates.append(FuturesQuote(
            f"恒指期货({month}){session}", number(m.group(1).replace(",", ""), "恒指期货"), field("前收市"),
            field("開市"), field("最高"), field("最低"), quoted_ms, "etnet", spot, "etnet", spot_prev, water,
            session=session, fetched_ms=now_ms))
    if not candidates:
        raise ValueError("etnet 页面没有找到恒指期货报价")
    if all(q.quoted_ms for q in candidates):
        return max(candidates, key=lambda q: q.quoted_ms)
    day = next((q for q in candidates if q.session == "日市"), None)
    night = next((q for q in candidates if q.session == "夜市"), None)
    if day and night:
        month = lambda q: tuple(reversed([int(x) for x in re.findall(r"\((\d{2})/(\d{4})\)", q.name)[0]]))
        if month(day) != month(night):
            # expiry day: the day block is the expiring contract, the night block already the next month
            chosen = night if month(night) > month(day) else day
        elif night.prev_settle == day.last and night.prev_settle != day.prev_settle:
            chosen = night   # tonight's (or last night's, after 03:00) session followed this day block
        elif night.prev_settle == day.prev_settle and night.prev_settle != day.last:
            chosen = day     # the night block is the one before this day session
        else:
            # can't tell from the prices (e.g. a contract roll): the session running now, else the one that ended last
            session = hk_futures_session(now_ms, holidays)
            if session not in ("日市", "夜市"):
                session = "夜市" if hk_session_end("夜市", now_ms, holidays) > hk_session_end("日市", now_ms, holidays) else "日市"
            chosen = night if session == "夜市" else day
    else:
        chosen = day or night
    if not chosen.quoted_ms and hk_futures_session(now_ms, holidays) != chosen.session:
        # a block whose session is over printed its last price by that session's end
        chosen = dataclasses.replace(chosen, quoted_ms=hk_session_end(chosen.session, now_ms, holidays))
    return chosen  # the running session's block keeps quoted_ms 0: unknown, never "now" (IndexFutures dates it)


def stale_note(quoted_ms: int, now_ms: int, tz: dt.tzinfo) -> str:
    """'｜⚠️ 非今日数据' when the quote's local calendar day is earlier than today's."""
    quoted = dt.datetime.fromtimestamp(quoted_ms / 1000, tz).date()
    today = dt.datetime.fromtimestamp(now_ms / 1000, tz).date()
    return "｜⚠️ 非今日数据" if quoted < today else ""


def hk_trading_day(day: dt.date, holidays: frozenset = frozenset()) -> bool:
    return day.weekday() < 5 and day not in holidays


def hk_previous_day(day: dt.date, holidays: frozenset = frozenset()) -> dt.date:
    """The HK trading day before ``day``."""
    day -= dt.timedelta(days=1)
    while not hk_trading_day(day, holidays):
        day -= dt.timedelta(days=1)
    return day


def hk_cash_close_date(now_ms: int, holidays: frozenset = frozenset()) -> dt.date:
    """The latest HK trading day whose cash close (16:10; 12:10 on a half day) has passed."""
    local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
    day = local.date() if local.time() >= CALENDAR.close_time("hk", local.date()) else local.date() - dt.timedelta(days=1)
    while not hk_trading_day(day, holidays):
        day -= dt.timedelta(days=1)
    return day


def local_ms(text: str, tz: dt.tzinfo = BEIJING) -> int:
    """'2026/09/28 16:08:59' or '20260928160859' (wall time in ``tz``) -> epoch ms; 0 when it is not a time."""
    digits = re.sub(r"\D", "", str(text or ""))
    if len(digits) < 12:
        return 0
    try:
        moment = dt.datetime.strptime(digits[:14] if len(digits) >= 14 else digits[:12],
                                      "%Y%m%d%H%M%S" if len(digits) >= 14 else "%Y%m%d%H%M")
    except ValueError:
        return 0
    return int(moment.replace(tzinfo=tz).timestamp() * 1000)


def hk_futures_session(now_ms: int, holidays: frozenset = frozenset()) -> str:
    """HKEX HSI futures: day session 09:15-16:30, after-hours (夜市) 17:15-03:00 next day, HK time.
    Sessions only start on trading days, so Friday's night ends Saturday 03:00 and weekends are shut.
    A half day ends the day session at 12:30 and has no night session."""
    moment = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
    local, today = moment.time(), moment.date()
    if local >= dt.time(17, 15) and hk_trading_day(today, holidays) and not CALENDAR.half_day("hk", today):
        return "夜市"
    yesterday = today - dt.timedelta(days=1)
    if local < dt.time(3, 0) and hk_trading_day(yesterday, holidays) and not CALENDAR.half_day("hk", yesterday):
        return "夜市"
    if dt.time(9, 15) <= local <= CALENDAR.hk_futures_day_end(today) and hk_trading_day(today, holidays):
        return "日市"
    return "休市"


def hk_session_end(session: str, now_ms: int, holidays: frozenset = frozenset()) -> int:
    """When the latest finished ``session`` ("日市"/"夜市") ended, at or before ``now_ms``."""
    local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
    day = local.date()
    for _ in range(30):
        if hk_trading_day(day, holidays) and (session == "日市" or not CALENDAR.half_day("hk", day)):
            end = (dt.datetime.combine(day, CALENDAR.hk_futures_day_end(day), BEIJING) if session == "日市"
                   else dt.datetime.combine(day + dt.timedelta(days=1), dt.time(3, 0), BEIJING))
            if end <= local:
                return int(end.timestamp() * 1000)
        day -= dt.timedelta(days=1)
    return now_ms


def sina_hf_time(fields: list[str], label: str) -> tuple[int, str]:
    """(quote time ms, name) from a Sina hf_ futures record.

    Layout seen live (2026-09): last, ?, bid, ask, high, low, time [6], prev settle, open, volume, ?, ?,
    date [12], name [13], ?. Older records put the date at [14]; the date is found by its format so a shifted
    layout still parses, and a record without one is rejected rather than stamped with "now".
    """
    for index, value in enumerate(fields):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
            try:
                quoted = dt.datetime.strptime(f"{value.strip()} {fields[6].strip()}", "%Y-%m-%d %H:%M:%S")
            except (ValueError, IndexError):
                break
            name = next((f for f in fields[index + 1:index + 2] + fields[index - 1:index]
                         if f and not re.fullmatch(r"[\d.:-]+", f)), "")
            return int(quoted.replace(tzinfo=BEIJING).timestamp() * 1000), name
    raise ValueError(f"新浪{label}报价时间格式异常")


def parse_eastmoney_quote(raw: bytes) -> dict[str, Any]:
    """push2 stock/get with fltt=2: {"data": {"f43": last, "f44": high, "f45": low, "f46": open, "f60": prev, "f86": ts}}"""
    try:
        data = json.loads(raw)["data"]
    except (ValueError, KeyError, TypeError):
        raise ValueError("东方财富报价格式异常") from None
    if not isinstance(data, dict) or data.get("f43") in (None, "-"):
        raise ValueError("东方财富没有该合约的报价")
    return data


def _opt(value: Any) -> D | None:
    try:
        return number(value, "行情", zero_ok=True) if value not in (None, "", "-") else None
    except ValueError:
        return None


def hsi_anchor_key(q: "FuturesQuote") -> str:
    """HKEX-contract prints and the Sina CFD fallback keep separate close anchors (their levels differ)."""
    return "HSI" if q.exchange_contract else "HSI:cfd"


def hsi_family(source: str) -> str:
    """The anchor family a futures source's quotes belong to: etnet and Eastmoney print the HKEX contract, Sina a CFD."""
    return "HSI:cfd" if "CFD" in source else "HSI"


HSI_FAMILY_NAMES = {"HSI": "港交所合约", "HSI:cfd": "新浪CFD"}


class IndexFutures:
    """Hang Seng Index futures (main contract, incl. the 17:15-03:00 after-hours session) with the
    cash index for the 高水/低水 basis. Eastmoney first, Sina as fallback; read-only, best effort."""
    REFRESH_SECONDS = 60   # between sessions
    SESSION_SECONDS = 20   # while the cash index or a futures session trades (etnet is a scraped page, not a feed)
    EM = "https://push2.eastmoney.com/api/qt/stock/get?fltt=2&invt=2&fields=f43,f44,f45,f46,f57,f58,f60,f86&secid="
    # etnet is the HKEX-designated free real-time site the user checks against; Sina hf_HSI is a CFD,
    # not the HKEX contract (it prints decimals), so it is only a last-resort, clearly labelled fallback.
    FUTURES_SOURCES = (("etnet", "https://www.etnet.com.hk/www/tc/futures/index.php", {"Referer": "https://www.etnet.com.hk/"}),
                       ("东方财富", EM + "134.HSI_M", {"Referer": "https://quote.eastmoney.com/"}),
                       ("新浪CFD", "https://hq.sinajs.cn/list=hf_HSI", {"Referer": "https://finance.sina.com.cn/"}))
    SPOT_SOURCES = (("东方财富", EM + "100.HSI", {"Referer": "https://quote.eastmoney.com/"}),
                    ("腾讯", "https://qt.gtimg.cn/q=hkHSI", {"Referer": "https://gu.qq.com/"}),
                    ("新浪", "https://hq.sinajs.cn/list=rt_hkHSI", {"Referer": "https://finance.sina.com.cn/"}))
    # Sina's 5-minute bars of the same hf_HSI record the CFD quote comes from: its 16:10 price when no print was recorded
    CFD_FIVE_MINUTES = "https://gu.sina.cn/ft/api/jsonp.php/var%20_HSI_5=/GlobalService.getMink?symbol=HSI&type=5"
    SPOT_SECONDS = 3         # the cash index alone, between the futures page's refreshes, while the cash session runs
    SPOT_RETRY_SECONDS = 27  # ... but 30 seconds after a round in which no timed feed answered with a current figure

    def __init__(self, enabled: bool = True, holidays: frozenset = frozenset()):
        self.enabled = enabled
        self.holidays = holidays
        self.quote: FuturesQuote | None = None
        self.families: dict[str, FuturesQuote] = {}  # anchor family -> the latest quote read from it (hsi_anchor_key)
        self.family_errors: dict[str, str] = {}      # anchor family -> why its last read gave nothing current ("" when fine)
        self.skipped = ""                              # why the preferred sources were passed over when a later one answered
        self.spot_refreshed = -1e9
        self.error = ""
        self.refreshed = -1e9
        self.refreshed_ms = 0    # market clock of the last refresh (the 16:10 cash close is caught at once)
        self.spot_at = -1e9      # monotonic time of the last good cash-index read
        self.spot_error = ""
        self.seen: dict[tuple, tuple[tuple, int]] = {}  # untimed source -> (values, when first seen unchanged)
        self.dated_close: Any = lambda day: None         # dated daily closes, plugged in by the Bot

    SPOT_KEEP_SECONDS = 5 * 60
    LUNCH = (dt.time(12, 0), dt.time(13, 0))  # cash and futures day sessions both pause 12:00–13:00

    @staticmethod
    def parse_futures(source: str, raw: bytes, now_ms: int, holidays: frozenset = frozenset()) -> FuturesQuote:
        if source == "etnet":
            return parse_etnet_futures(raw, now_ms, holidays)
        if source == "东方财富":
            d = parse_eastmoney_quote(raw)
            return FuturesQuote(str(d.get("f58") or "恒指期货主力"), number(d["f43"], "恒指期货"), _opt(d.get("f60")),
                                _opt(d.get("f46")), _opt(d.get("f44")), _opt(d.get("f45")), eastmoney_ms(d), source,
                                fetched_ms=now_ms)
        # Sina hf_HSI: last, ?, bid, ask, high, low, time [6], prev settle, open, ..., date, name (see sina_hf_time)
        match = re.search(r'="([^"]*)"', raw.decode("gbk", errors="ignore"))
        fields = match.group(1).split(",") if match else []
        if len(fields) < 13 or not fields[0]:
            raise ValueError("新浪恒指期货报价为空")
        quoted_ms, name = sina_hf_time(fields, "恒指期货")
        return FuturesQuote(name or "恒指期货", number(fields[0], "恒指期货"), _opt(fields[7]), _opt(fields[8]),
                            _opt(fields[4]), _opt(fields[5]), quoted_ms, source, exchange_contract=False, fetched_ms=now_ms)

    @staticmethod
    def parse_spot_quote(source: str, raw: bytes, now_ms: int = 0) -> IndexQuote:
        """Cash index: last, previous close and the feed's own time (0 when it prints none)."""
        if source == "东方财富":
            d = parse_eastmoney_quote(raw)
            return IndexQuote("恒生指数", number(d["f43"], "恒生指数"), _opt(d.get("f60")), None, None, None,
                              eastmoney_ms(d), source, fetched_ms=now_ms)
        text = raw.decode("gbk", errors="ignore")
        match = re.search(r'="([^"]*)"', text)
        if not match or not match.group(1).strip():
            raise ValueError(f"{source}恒生指数报价为空")
        fields = match.group(1).split("~" if source == "腾讯" else ",")
        try:  # Tencent: current [3], prev close [4], time [30]; Sina rt_hk: prev close [3], current [6], date [17], time [18]
            last, prev = (fields[3], fields[4]) if source == "腾讯" else (fields[6], fields[3])
            value = number(last, "恒生指数")
        except IndexError:
            raise ValueError(f"{source}恒生指数格式异常") from None
        when = fields[30] if source == "腾讯" and len(fields) > 30 else " ".join(fields[17:19]) if len(fields) > 18 else ""
        return IndexQuote("恒生指数", value, _opt(prev), None, None, None, local_ms(when), source, fetched_ms=now_ms)

    @classmethod
    def parse_spot(cls, source: str, raw: bytes) -> tuple[D, D | None]:
        """Cash index (last, previous close)."""
        q = cls.parse_spot_quote(source, raw)
        return q.last, q.prev_close

    def cash_open(self, now_ms: int) -> bool:
        """The HSI cash session (09:30–16:10, closing auction included; to 12:10 on a half day) is running."""
        local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
        return hk_trading_day(local.date(), self.holidays) and dt.time(9, 30) <= local.time() < CALENDAR.close_time("hk", local.date())

    def futures_problem(self, q: FuturesQuote, now_ms: int) -> str:
        """Why q cannot stand for the futures now ("" when it can): while a session trades it must have moved within
        10 minutes (the lunch break aside); while the market is shut it must come from the session that ended last."""
        live = hk_futures_session(now_ms, self.holidays) in ("日市", "夜市")
        last_end = max(hk_session_end("日市", now_ms, self.holidays), hk_session_end("夜市", now_ms, self.holidays))
        return quote_problem(q.quoted_ms, now_ms, live, last_end, self.LUNCH)

    def spot_problem(self, q: FuturesQuote, now_ms: int) -> str:
        """Why q's cash index cannot be used now ("" when it can). During the cash session it must be today's and
        have moved within 10 minutes (lunch aside). Its previous close must be the dated close of the session
        before (when that is known): otherwise the figure belongs to another day, e.g. yesterday's page."""
        if q.spot is None:
            return "缺少恒指现货"
        if self.cash_open(now_ms):
            day, when = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING).date(), q.spot_time
            opened = int(dt.datetime.combine(day, dt.time(9, 30), BEIJING).timestamp() * 1000)
            if when <= 0:
                return "恒指现货报价时间未知"
            if when < opened:
                return f"恒指现货停在 {stamp(when, seconds=False)}，早于今日开盘"
            if quiet_ms(when, now_ms, self.LUNCH) > LIVE_STALE_MS:
                return f"恒指现货停在 {stamp(when, seconds=False)}，已超 10 分钟未更新"
        else:
            day = hk_cash_close_date(now_ms, self.holidays)
        before = hk_previous_day(day, self.holidays)
        expected = self.dated_close(before)
        if expected is not None and q.spot_prev is not None and abs(percent(q.spot_prev, expected)) > D("0.005"):
            return (f"恒指现货的前收 {fmt(q.spot_prev)} 不是 {before:%m-%d} 收盘 {fmt(expected)}，"
                    f"不是 {day:%m-%d} 的行情")
        return ""

    def observed(self, key: tuple, values: tuple, fetched_ms: int) -> int:
        """For a source that prints no time: when these exact values were first seen in a row. A trading market keeps
        moving, so a page whose values stop changing ages (and goes stale) instead of passing for a live one."""
        old = self.seen.get(key)
        if old is not None and old[0] == values:
            return old[1]
        self.seen[key] = (values, fetched_ms)
        return fetched_ms

    def dated(self, q: FuturesQuote, now_ms: int) -> FuturesQuote:
        """An untimed live block (etnet) and its cash index get the time their values were first seen unchanged."""
        changes: dict[str, int] = {}
        if not q.quoted_ms:
            changes["quoted_ms"] = self.observed(("futures", q.source), (q.name, q.last, q.high, q.low, q.open,
                                                                        q.prev_settle, q.water), now_ms)
        if q.spot is not None and q.spot_source == q.source and not q.spot_ms:
            changes["spot_ms"] = self.observed(("spot", q.source), (q.spot, q.spot_prev), now_ms)
        return dataclasses.replace(q, **changes) if changes else q

    def cash_close_due(self, now_ms: int) -> bool:
        """The first refresh after the 16:10 cash close is not left to the 60-second cadence: the futures price at
        that moment is the anchor that maps the after-hours move onto the close."""
        local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
        close = int(dt.datetime.combine(local.date(), CALENDAR.close_time("hk", local.date()), BEIJING).timestamp() * 1000)
        return hk_trading_day(local.date(), self.holidays) and self.refreshed_ms < close <= now_ms

    def cadence(self, now_ms: int) -> int:
        """Seconds between refreshes: SESSION_SECONDS while the cash index or the futures trade, else REFRESH_SECONDS."""
        live = self.cash_open(now_ms) or hk_futures_session(now_ms, self.holidays) != "休市"
        return self.SESSION_SECONDS if live else self.REFRESH_SECONDS

    def with_spot(self, quote: FuturesQuote, s: IndexQuote) -> FuturesQuote:
        """``quote`` carrying another feed's cash index: etnet's published premium (水位) belonged to its own spot."""
        return dataclasses.replace(quote, spot=s.last, spot_prev=s.prev_close, spot_source=s.source, spot_ms=s.quoted_ms,
                                   water=None)

    async def refresh_spot(self, now_ms: int) -> Refreshed:
        """The cash index alone (Tencent / Sina, each with its own quote time) every SPOT_SECONDS during the cash session,
        so the in-session odds follow the index within seconds; the futures page itself is read every SESSION_SECONDS."""
        self.spot_refreshed = time.monotonic()
        base = self.quote
        spot, _, error = await pick_quote(self.SPOT_SOURCES, lambda n, r: self.parse_spot_quote(n, r, now_ms),
                                          lambda s: self.spot_problem(self.with_spot(base, s), now_ms))
        if spot is None or error:
            self.spot_refreshed += self.SPOT_RETRY_SECONDS  # the timed feeds are failing: look again in half a minute
            if self.quote is not None and not self.spot_problem(self.quote, now_ms):
                return Refreshed("ok")  # the futures page's own cash index still stands
            return Refreshed("partial", f"恒指现货：{error or '无报价'}")
        if self.quote is not None and spot.quoted_ms >= self.quote.spot_time:
            self.quote = self.with_spot(self.quote, spot)
            self.spot_at, self.spot_error = time.monotonic(), ""
        return Refreshed("ok")

    def remember(self, q: FuturesQuote) -> None:
        """Keep the newest quote of each anchor family, current or not (the odds pick a family that has its 16:10 price)."""
        family = hsi_anchor_key(q)
        old = self.families.get(family)
        if old is None or q.quoted_ms >= old.quoted_ms:
            self.families[family] = q

    async def read_families(self, now_ms: int, attempted: set[str], parse: Any) -> None:
        """After the cash close, also read each source whose family the preferred one did not cover (hosts in cooldown
        aside): every family then has its own current quote, and its own print at 16:10 to anchor it."""
        current = {family for family, q in self.families.items() if q.fetched_ms == now_ms and not self.futures_problem(q, now_ms)}
        for name, url, extra in self.FUTURES_SOURCES:
            family = hsi_family(name)
            if family in current or name in attempted:
                continue
            if SOURCE_HEALTH.cooling(url):
                if not self.family_errors.get(family):  # say why this family has no current quote
                    failures, _, last = SOURCE_HEALTH.hosts.get(SOURCE_HEALTH.host(url)) or [0, 0.0, ""]
                    self.family_errors[family] = (f"{name}: 连续失败 {failures} 次，{int(SOURCE_HEALTH.cooling(url))} 秒后再试"
                                                  + (f"（{last}）" if last else ""))
                continue
            try:
                q = parse(name, await fetch_source(url, extra))
            except Exception as error:
                self.family_errors[family] = f"{name}: {clean_error(error) or type(error).__name__}"
                continue
            why = self.futures_problem(q, now_ms)
            self.family_errors[family] = f"{name}: {why}" if why else ""
            if not why:
                current.add(family)

    async def cfd_bar_at(self, close_ms: int) -> D:
        """The Sina CFD's 5-minute bar stamped at ``close_ms`` (the 16:10 cash close), from the same hf_HSI record."""
        raw = await fetch_source(self.CFD_FIVE_MINUTES, {"Referer": "https://finance.sina.com.cn/"})
        bars = parse_sina_bars(raw, "恒指")
        wanted = dt.datetime.fromtimestamp(close_ms / 1000, BEIJING).strftime("%Y-%m-%d %H:%M")
        for when, close in bars:
            if when == wanted:
                return close
        span = f"{bars[0][0][5:]}～{bars[-1][0][5:]}" if bars else "无数据"
        raise ValueError(f"新浪恒指 5分钟K里没有 {wanted}（返回 {len(bars)} 根：{span}）")

    async def refresh(self, now_ms: int, force: bool = False) -> Refreshed | bool:
        if not self.enabled:
            return False
        if not (force or time.monotonic() - self.refreshed >= self.cadence(now_ms) or self.cash_close_due(now_ms)):
            if (self.cash_open(now_ms) and self.quote is not None
                    and time.monotonic() - self.spot_refreshed >= self.SPOT_SECONDS):
                return await self.refresh_spot(now_ms)
            return False  # not due yet: nothing fetched
        self.refreshed, self.refreshed_ms = time.monotonic(), now_ms
        parsed: list[FuturesQuote] = []

        def parse(name: str, raw: bytes) -> FuturesQuote:
            q = self.dated(self.parse_futures(name, raw, now_ms, self.holidays), now_ms)
            parsed.append(q)
            self.remember(q)
            return q
        # a source whose answer parses but is stale (or undated) does not end the search
        quote, skipped, error = await pick_quote(self.FUTURES_SOURCES, parse, lambda q: self.futures_problem(q, now_ms))
        for family in {hsi_family(name) for name, _, _ in self.FUTURES_SOURCES}:
            notes = [note for note in skipped if hsi_family(note.split(":", 1)[0]) == family]
            got = any(hsi_anchor_key(q) == family and not self.futures_problem(q, now_ms) for q in parsed)
            self.family_errors[family] = "" if got else "；".join(notes)
        self.skipped = "" if error else "；".join(skipped)
        if not self.cash_open(now_ms):
            attempted = {note.split(":", 1)[0] for note in skipped} | {q.source for q in parsed}
            await self.read_families(now_ms, attempted, parse)
        if error:  # nothing current: keep the newest quote known, with the reason shown
            self.quote, self.error = newer(quote, self.quote), error
            return Refreshed("failed", error)
        prev = self.quote
        if (self.cash_open(now_ms) and prev is not None and prev.spot is not None and prev.spot_source != quote.source
                and prev.spot_time > (quote.spot_time if quote.spot is not None else 0)):
            # the 3-second cash index is newer than the one on the futures page: keep it
            quote = dataclasses.replace(quote, spot=prev.spot, spot_prev=prev.spot_prev, spot_source=prev.spot_source,
                                        spot_ms=prev.spot_time, water=None)
        why = self.spot_problem(quote, now_ms)
        self.spot_error = ""
        if why:  # no cash index with the futures (Eastmoney, CFD), or etnet's cannot be trusted now: ask the timed feeds
            base = quote
            spot, _, spot_error = await pick_quote(self.SPOT_SOURCES, lambda n, r: self.parse_spot_quote(n, r, now_ms),
                                                   lambda s: self.spot_problem(self.with_spot(base, s), now_ms))
            if spot is not None and (not spot_error or quote.spot is None):
                quote = self.with_spot(quote, spot)
            elif (quote.spot is None and prev is not None and prev.spot is not None
                  and time.monotonic() - self.spot_at < self.SPOT_KEEP_SECONDS):
                # one failed round must not blank the HSI card: keep the last cash index (and its time) for a few minutes
                quote = dataclasses.replace(quote, spot=prev.spot, spot_prev=prev.spot_prev, spot_source=prev.spot_source,
                                            spot_ms=prev.spot_time)
            if spot_error:
                self.spot_error = (f"{quote.source}: {why}；" if quote.spot_source == quote.source else "") + spot_error
                LOG.debug("HSI spot unavailable: %s", self.spot_error)
        if not self.spot_error:
            self.spot_at = time.monotonic()
        self.quote, self.error = quote, ""
        return refreshed([f"恒指现货：{self.spot_error}" if self.spot_error else ""], 1)

    def line(self, now_ms: int, style: str) -> str:
        if not self.enabled:
            return ""
        q = self.quote
        if q is None:
            return f"📈 恒指期货 ⚠️ 获取失败（{self.error}）" if self.error else "📈 恒指期货 ⏳ 等待首次获取"
        session = q.session_name(self.holidays)
        if session in ("日市", "夜市") and hk_futures_session(now_ms, self.holidays) != session:
            session += "（已收市）"
        parts = [f"📈 {bold('恒指期货 ' + session)} {bold(fmt(q.last))}"]
        if q.spot is not None:
            local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
            cash_open = hk_trading_day(local.date(), self.holidays) and dt.time(9, 30) <= local.time() <= dt.time(16, 10)
            water = "高水" if q.basis > 0 else "低水" if q.basis < 0 else "平水"
            parts[0] += (f" → 恒指{'' if cash_open else '收盘'} {bold(fmt(q.spot))} {pct_text(percent(q.last, q.spot), style)}"
                         f"（{water} {abs(q.basis):,.0f}）")
            if q.spot_prev:
                parts.append(f"恒指当日 {pct_text(percent(q.spot, q.spot_prev), style)}")
        if q.change is not None and q.prev_settle:
            parts.append(f"期货前收 {bold(fmt(q.prev_settle))} {pct_text(q.change / q.prev_settle * 100, style)}（{q.change:+,.0f}）")
        source = q.source if q.exchange_contract else f"{q.source}·非港交所合约，仅参考"
        # "非今日数据" only when the quote really is behind (not on a weekend, when the last session's print is the right one)
        parts.append(f"{quote_time(q.quoted_ms)} {source}"
                     + (stale_note(q.quoted_ms, now_ms, BEIJING) if q.quoted_ms and self.futures_problem(q, now_ms) else ""))
        line = "｜".join(parts)
        missed = self.skipped or (self.family_errors.get("HSI", "") if not q.exchange_contract else "")
        if missed and not self.error:
            line += f"｜⚠️ 前序源未取到：{brief_error(missed, 90)}"
        if self.spot_error:
            line += f"｜⚠️ 恒指现货刷新失败：{brief_error(self.spot_error)}"
        return line + f"｜⚠️ 刷新失败：{brief_error(self.error)}" if self.error else line


@dataclass(frozen=True)
class HlQuote:
    """One Hyperliquid perp's context (USD-quoted)."""
    coin: str          # Full market name, e.g. "xyz:SKHX"
    mark: D
    oracle: D | None
    mid: D | None
    prev_day: D | None
    funding: D | None  # Hourly funding rate as a fraction (0.0000125 = 0.00125 %/h)
    fetched_ms: int

    @property
    def day_change(self) -> D | None:
        return percent(self.mark, self.prev_day) if self.prev_day else None


def kr200_price(hl: HlQuote) -> tuple[D, str]:
    """The KR200 price compared with the anchor. The anchor is a traded candle close, so the book mid
    (where trades happen) is used rather than the mark, which leans on the oracle while Korea is shut."""
    return (hl.mid, "中间价") if hl.mid else (hl.mark, "标记价")


class Hyperliquid:
    """Reference quotes from Hyperliquid's public info endpoint (no key), one request per perp dex.

    HIP-3 builder dexes (e.g. trade.xyz for equities) are queried with the "dex" parameter and
    name markets "dex:COIN". Read-only, best effort; a missing market is a note, not an error.
    """
    URL = "https://api.hyperliquid.xyz/info"
    REFRESH_SECONDS = 30

    def __init__(self, tickers: dict[str, tuple[str, str]]):
        self.tickers = dict(tickers)
        self.quotes: dict[str, HlQuote] = {}
        self.notes: dict[str, str] = {}   # symbol -> why there is no quote (not listed / fetch failed)
        self.at_cache: dict[tuple[str, int, str], D] = {}
        self.refreshed = -1e9

    async def price_at(self, coin: str, at_ms: int, interval: str = "1m") -> D:
        """Close of the candle that ends exactly at ``at_ms`` (it opens one interval earlier): the market's price at
        that instant. A neighbouring candle is never taken instead: with the 14:29 minute missing, 14:26's close is not
        the price at 14:30, so the caller falls back to the 5-minute candle ending at the same instant.

        Hyperliquid only serves the latest 5,000 candles per interval (about 3.5 days of 1-minute bars),
        so after a long holiday the 5-minute candle ending at the same instant is the fallback.
        """
        key = (coin, at_ms, interval)
        if key in self.at_cache:
            return self.at_cache[key]
        span = {"1m": 1, "5m": 5, "15m": 15}[interval] * 60_000
        rows = await http_json(self.URL, {"type": "candleSnapshot", "req": {
            "coin": coin, "interval": interval, "startTime": at_ms - 5 * span, "endTime": at_ms}})
        wanted = at_ms - span

        def exact(row: Any) -> bool:
            with contextlib.suppress(TypeError, ValueError):
                # "t" opens the candle, "T" (when sent) closes it: t + interval − 1 ms
                return int(row["t"]) == wanted and ("T" not in row or int(row["T"]) in (at_ms - 1, at_ms))
            return False
        candle = next((r for r in rows if isinstance(r, dict) and exact(r)), None) if isinstance(rows, list) else None
        if candle is None:
            got = sorted(stamp(int(r["t"]), seconds=False)[6:] for r in rows if isinstance(r, dict) and str(r.get("t", "")).isdigit()) \
                if isinstance(rows, list) else []
            raise ValueError(f"Hyperliquid 没有 {coin} 在 {stamp(wanted, seconds=False)}–{hhmm(at_ms)} 的 {interval} K 线"
                             + (f"（只有 {'、'.join(got[-3:])} 开始的）" if got else ""))
        price = number(candle["c"], "HL 收盘时刻价格")
        self.at_cache = {k: v for k, v in self.at_cache.items() if k[0] != coin}
        self.at_cache[key] = price
        return price

    @staticmethod
    def parse_dex(data: Any) -> dict[str, HlQuote]:
        """metaAndAssetCtxs -> {COIN (upper, without dex prefix): HlQuote}."""
        try:
            universe, contexts = data[0]["universe"], data[1]
        except (KeyError, IndexError, TypeError):
            raise ValueError("Hyperliquid 返回格式异常") from None
        quotes: dict[str, HlQuote] = {}
        now_ms = int(time.time() * 1000)
        for asset, ctx in zip(universe, contexts):
            if not isinstance(asset, dict) or not isinstance(ctx, dict) or asset.get("isDelisted"):
                continue
            name = str(asset.get("name", ""))
            try:
                mark = number(ctx.get("markPx"), "标记价")
            except ValueError:
                continue
            quotes[name.split(":")[-1].upper()] = HlQuote(
                name, mark, _opt(ctx.get("oraclePx")), _opt(ctx.get("midPx")), _opt(ctx.get("prevDayPx")),
                _opt(ctx.get("funding")), now_ms)
        return quotes

    async def refresh(self, force: bool = False) -> Refreshed | bool:
        if not self.tickers or (not force and time.monotonic() - self.refreshed < self.REFRESH_SECONDS):
            return False  # not due yet: nothing fetched
        self.refreshed = time.monotonic()
        by_dex: dict[str, dict[str, HlQuote] | str] = {}
        for dex in {dex for dex, _ in self.tickers.values()}:
            payload: dict[str, Any] = {"type": "metaAndAssetCtxs"}
            if dex:
                payload["dex"] = dex
            try:
                by_dex[dex] = self.parse_dex(await http_json(self.URL, payload))
            except Exception as error:
                by_dex[dex] = clean_error(error)
        for symbol, (dex, coin) in self.tickers.items():
            result = by_dex.get(dex)
            label = f"{dex}:{coin}" if dex else coin
            if isinstance(result, str):  # Fetch failed: keep the last quote, remember the failure.
                self.notes[symbol] = f"获取失败（{result}）"
            elif coin in result:
                self.quotes[symbol] = result[coin]
                self.notes.pop(symbol, None)
            else:
                similar = [q.coin for k, q in result.items() if coin[:3] in k][:4]
                hint = f"，相近：{'、'.join(similar)}" if similar else ""
                self.notes[symbol] = f"未找到市场 {label}（该 dex 共 {len(result)} 个市场{hint}），可用 HL_TICKERS 指定"
                self.quotes.pop(symbol, None)
        failed = [f"{dex or '主市场'}：{result}" for dex, result in by_dex.items() if isinstance(result, str)]
        return refreshed(failed, len(by_dex) - len(failed))

    def line(self, symbol: str, price_usd: D | None, style: str, unit_note: str = "") -> str:
        """'HL 73.279（24h 🔴 +0.03%·费率 0.0006%/h）→ 🟢 -0.11%' as a block row."""
        if symbol not in self.tickers:
            return ""
        quote, note = self.quotes.get(symbol), self.notes.get(symbol)
        if quote is None:
            return f"HL {note or '⏳ 等待首次获取'}"
        meta = []
        if quote.day_change is not None:
            meta.append(f"24h {pct_text(quote.day_change, style)}")
        if quote.funding is not None:
            meta.append(f"费率 {fmt((quote.funding * 100).quantize(D('0.00001')))}%/h")
        deviation = ("⚪ 无汇率" if price_usd is None
                     else pct_text(percent(price_usd, quote.mark), style) + unit_note)
        line = f"HL {bold(fmt(quote.mark))}" + (f"（{'·'.join(meta)}）" if meta else "") + f" → {deviation}"
        return line + f"｜⚠️ {brief_error(note)}" if note else line


@dataclass(frozen=True)
class IndexQuote:
    """A cash index level with its previous close and session status. quoted_ms is the market's own time for the
    price (0 = the feed gave none); fetched_ms is when the bot read it."""
    name: str
    last: D
    prev_close: D | None
    open: D | None
    high: D | None
    low: D | None
    quoted_ms: int
    source: str
    status: str = ""  # e.g. "交易中" / "已收盘"
    fetched_ms: int = 0

    @property
    def change(self) -> D | None:
        return self.last - self.prev_close if self.prev_close else None


def naver_number(value: Any) -> D | None:
    """Naver prints numbers with thousands separators ("3,371.89"); changes may be signed."""
    if value in (None, ""):
        return None
    try:
        result = D(str(value).replace(",", "").strip())
    except decimal.InvalidOperation:
        return None
    return result if result.is_finite() else None


def parse_naver_index(raw: bytes, now_ms: int) -> IndexQuote:
    """polling.finance.naver.com/api/realtime/domestic/index/KOSPI -> {"datas": [{...}]}"""
    try:
        item = json.loads(raw)["datas"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        raise ValueError("Naver 指数返回格式异常") from None
    last = number(str(item.get("closePrice", "")).replace(",", ""), "KOSPI")
    change = naver_number(item.get("compareToPreviousClosePrice"))
    direction = str((item.get("compareToPreviousPrice") or {}).get("code", ""))
    ratio = str(item.get("fluctuationsRatio", ""))
    if change is not None and (direction == "5" or ratio.startswith("-")):  # 5 = 하락 (falling)
        change = -abs(change)
    prev = last - change if change is not None else None
    quoted_ms = 0  # unknown unless the feed says: a frozen answer must never pass for a live one
    with contextlib.suppress(ValueError, TypeError):
        traded = dt.datetime.fromisoformat(str(item.get("localTradedAt")))
        quoted_ms = int(traded.replace(tzinfo=traded.tzinfo or dt.timezone(dt.timedelta(hours=9))).timestamp() * 1000)
    status = {"OPEN": "交易中", "CLOSE": "已收盘", "PREOPEN": "盘前"}.get(str(item.get("marketStatus", "")).upper(), "")
    return IndexQuote(str(item.get("stockName") or "KOSPI"), last, prev, naver_number(item.get("openPrice")),
                      naver_number(item.get("highPrice")), naver_number(item.get("lowPrice")), quoted_ms, "Naver", status,
                      now_ms)


def eastmoney_ms(d: dict) -> int:
    """push2 f86 (epoch seconds) -> ms; 0 when absent (unknown, not "now")."""
    return int(d["f86"]) * 1000 if str(d.get("f86", "")).isdigit() and int(d["f86"]) > 0 else 0


def parse_eastmoney_index(raw: bytes, now_ms: int, name: str) -> IndexQuote:
    d = parse_eastmoney_quote(raw)
    return IndexQuote(str(d.get("f58") or name), number(d["f43"], name), _opt(d.get("f60")), _opt(d.get("f46")),
                      _opt(d.get("f44")), _opt(d.get("f45")), eastmoney_ms(d), "东方财富", fetched_ms=now_ms)


def krx_session(now_ms: int, holidays: frozenset = frozenset()) -> str:
    local = dt.datetime.fromtimestamp(now_ms / 1000, dt.timezone(dt.timedelta(hours=9)))
    if local.weekday() >= 5 or local.date() in holidays:
        return "休市"
    start, end = CALENDAR.sessions("kr", local.date())[0]
    return "交易中" if start <= local.time() <= end else "已收盘"


# SGX FTSE China A50 futures, Beijing time (= Singapore time): T session 09:00–16:30, T+1 session 16:45–05:15.
A50_DAY_START, A50_DAY_END = dt.time(9, 0), dt.time(16, 30)
A50_NIGHT_START, A50_NIGHT_END = dt.time(16, 45), dt.time(5, 15)
A50_HALF_DAY_END = dt.time(12, 0)  # SGX half days (SG_HALF_DAYS): the morning only, and no T+1 session that evening


def a50_trading_day(day: dt.date) -> bool:
    """SGX trades the A50 on weekdays that are not Singapore exchange holidays (HOLIDAYS_SG)."""
    return day.weekday() < 5 and day not in CALENDAR.sg_holidays


def a50_day_end(day: dt.date) -> dt.time:
    return A50_HALF_DAY_END if CALENDAR.half_day("sg", day) else A50_DAY_END


def a50_night(day: dt.date) -> bool:
    """A T+1 (night) session starts on ``day`` evening: every full trading day, never a half day or a holiday."""
    return a50_trading_day(day) and not CALENDAR.half_day("sg", day)


def a50_session(now_ms: int) -> str:
    """FTSE China A50 futures (SGX): day 09:00-16:30, night 16:45-05:15 Beijing time.

    Sessions only start on SGX trading days: Friday's night session ends Saturday 05:15, Sunday night has none,
    a Singapore holiday (HOLIDAYS_SG) has neither session, and a half day (SG_HALF_DAYS) trades the morning only.
    """
    moment = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
    local, today = moment.time(), moment.date()
    if local < A50_NIGHT_END and a50_night(today - dt.timedelta(days=1)):
        return "夜盘"
    if not a50_trading_day(today):
        return "休市"
    if A50_DAY_START <= local <= a50_day_end(today):
        return "日盘"
    if local >= A50_NIGHT_START and a50_night(today):
        return "夜盘"
    return "休市"


def a50_closed_note(now_ms: int) -> str:
    """'休市', or why A50 is shut when the reason is Singapore's calendar rather than the clock: the status line
    and the odds say '新加坡交易所假期休市' / '新加坡半日市休市（当晚无夜盘）' instead of a stale-quote warning."""
    local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
    day = local.date()
    if day.weekday() < 5 and day in CALENDAR.sg_holidays:
        return "新加坡交易所假期休市"
    if CALENDAR.half_day("sg", day) and local.time() > A50_HALF_DAY_END:
        return "新加坡半日市休市（当晚无夜盘）"
    return "休市"


def a50_expiry(day: dt.date) -> bool:
    """SGX FTSE China A50 futures expire on the second-last business day of the month (weekdays; SGX holidays
    aside): that evening the continuous series (CN00Y, Sina's CFD) moves to the next month's contract."""
    last = dt.date(day.year + day.month // 12, day.month % 12 + 1, 1) - dt.timedelta(days=1)
    while last.weekday() >= 5:
        last -= dt.timedelta(days=1)
    second = last - dt.timedelta(days=1)
    while second.weekday() >= 5:
        second -= dt.timedelta(days=1)
    return day == second


def a50_next_open(now_ms: int) -> int | None:
    """When A50 trading resumes after a closure that outlasts the 16:30-16:45 break; None while it trades
    or during that break (the day session's last print still stands for those 15 minutes)."""
    if a50_session(now_ms) != "休市":
        return None
    local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
    day, clock = local.date(), local.time()
    if a50_night(day) and A50_DAY_END < clock < A50_NIGHT_START:
        return None
    if not (a50_trading_day(day) and clock < A50_DAY_START):
        day += dt.timedelta(days=1)  # the next SGX trading day's day session (weekends and HOLIDAYS_SG skipped)
    for _ in range(30):
        if a50_trading_day(day):
            break
        day += dt.timedelta(days=1)
    return int(dt.datetime.combine(day, A50_DAY_START, BEIJING).timestamp() * 1000)


def a50_last_session_end(now_ms: int) -> int:
    """Most recent SGX A50 session end at or before now_ms, including Friday night's Saturday morning close
    (a holiday or a half day has no night session, so the end before it is the one that counts)."""
    local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
    day = local.date()
    for _ in range(30):
        if a50_trading_day(day):
            ends = [dt.datetime.combine(day, a50_day_end(day), BEIJING)]
            if a50_night(day):
                ends.append(dt.datetime.combine(day + dt.timedelta(days=1), A50_NIGHT_END, BEIJING))
            past = [end for end in ends if end <= local]
            if past:
                return int(max(past).timestamp() * 1000)
        day -= dt.timedelta(days=1)
    return now_ms


def a50_code_ok(code: Any) -> bool:
    """Eastmoney labels SGX A50 contracts CN00Y (continuous), CN2610, ...; an empty code is tolerated,
    anything else (another contract) is not."""
    code = str(code or "").strip().upper()
    return not code or code.startswith("CN")


def parse_sina_bars(raw: bytes, label: str = "A50") -> list[tuple[str, D]]:
    """Sina GlobalService.getMink JSONP: [{"d": "2026-09-24 15:00:00", "o": .., "h": .., "l": .., "c": .., "v": ..}, ...]
    -> [("2026-09-24 15:00", close)], oldest first. Tolerates either quoted or bare keys and numbers."""
    text = raw.decode("utf-8", errors="replace")
    bars = []
    for obj in re.findall(r"\{[^{}]*\}", text):
        when = re.search(r'"?d(?:ay|ate)?"?\s*:\s*"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})', obj)
        close = re.search(r'"?c(?:lose)?"?\s*:\s*"?([\d.]+)', obj)
        if when and close:
            bars.append((when.group(1), number(close.group(1), label)))
    if not bars:
        raise ValueError(f"新浪 {label} 5分钟K 格式异常或为空")
    return sorted(bars)


def a50_print_note(quoted_ms: int, close_ms: int) -> str:
    """Label of an A50 anchor taken from the first live print after the 15:00 close: within a minute it is the price
    at the close for all practical purposes ('15:00 后 12 秒首笔'), later it is an approximation and says so."""
    late = max(0, quoted_ms - close_ms) // 1000
    return f"15:00 后 {late} 秒首笔" if late < 60 else f"15:00 后 {late // 60} 分钟首笔近似"


def a50_anchor_exact(note: str) -> bool:
    """The anchor is the A50 price at the 15:00 close itself (the 15:00 one-minute bar or a print within a minute)."""
    return note == "15:00" or note.endswith("秒首笔")


def a50_family(source: str) -> str:
    """Quotes from the same contract share a family: Eastmoney's quote and its K lines are both the SGX
    futures; Sina's CFD is a different instrument and never mixes with them."""
    return "东方财富" if source.startswith("东方财富") else source


def parse_cn_index(source: str, raw: bytes, now_ms: int, name: str = "上证指数") -> IndexQuote:
    """Tencent v_sh000001 / Sina hq_str_sh000001 / Eastmoney push2 -> IndexQuote (last, prev close, time).

    The answer must name the Composite itself (code 000001 on the Shanghai board); anything else is
    rejected rather than shown as the index.
    """
    if source == "东方财富":
        data = parse_eastmoney_quote(raw)
        if str(data.get("f57", "")) != "000001":
            raise ValueError(f"东方财富返回的代码不是上证指数 000001（{clean_error(str(data.get('f57', '空')))}）")
        return parse_eastmoney_index(raw, now_ms, name)
    text = raw.decode("gbk", errors="ignore")
    match = re.search(r'(\w+)="([^"]*)"', text)
    if not match or not match.group(2).strip():
        raise ValueError(f"{source}{name}报价为空")
    if not match.group(1).endswith("sh000001"):
        raise ValueError(f"{source}返回的代码不是上证指数 sh000001（{match.group(1)}）")
    fields = match.group(2).split("~" if source == "腾讯" else ",")
    if source == "腾讯" and (len(fields) < 3 or fields[2] != "000001"):
        raise ValueError(f"{source}返回的代码不是上证指数 000001")
    try:
        if source == "腾讯":  # [2] code, [3] current, [4] prev close, [5] open, [30] yyyymmddHHMMSS
            last, prev, opening = fields[3], fields[4], fields[5]
            quoted = dt.datetime.strptime(re.sub(r"\D", "", fields[30])[:12], "%Y%m%d%H%M")
        else:  # Sina: name, open, prev, current, high, low, ..., date [30], time [31]
            last, prev, opening = fields[3], fields[2], fields[1]
            quoted = dt.datetime.strptime(f"{fields[30]} {fields[31]}", "%Y-%m-%d %H:%M:%S")
    except (IndexError, ValueError):
        raise ValueError(f"{source}{name}格式异常") from None
    return IndexQuote(name, number(last, name), _opt(prev), _opt(opening), None, None,
                      int(quoted.replace(tzinfo=BEIJING).timestamp() * 1000), source)


def parse_cn_daily(source: str, raw: bytes) -> list[tuple[dt.date, D]]:
    """Dated Shanghai Composite daily bars (date, close), oldest first; the code is checked."""
    return [(day, close) for day, _, close in parse_cn_daily_ohlc(source, raw)]


def sina_daily_rows(raw: bytes) -> list[tuple[str, str, str]]:
    """Sina CN_MarketData.getKLineData (scale=240): [{"day":"2026-09-24","open":"3871.000",...,"close":"3888.370",...}, ...]
    -> [(day, open, close)]. Keys may come quoted or bare; the record names no code (the request does)."""
    rows = []
    for obj in re.findall(r"\{[^{}]*\}", raw.decode("utf-8", errors="replace")):
        day = re.search(r'"?day"?\s*:\s*"(\d{4}-\d{2}-\d{2})', obj)
        opening = re.search(r'"?open"?\s*:\s*"?([\d.]+)', obj)
        close = re.search(r'"?close"?\s*:\s*"?([\d.]+)', obj)
        if day and close:
            rows.append((day.group(1), opening.group(1) if opening else "", close.group(1)))
    return rows


def parse_cn_daily_ohlc(source: str, raw: bytes) -> list[tuple[dt.date, D | None, D]]:
    """As parse_cn_daily, with each bar's open (None when missing). Tencent and Eastmoney name the code and it is
    checked, Yahoo's meta names the symbol; Sina's daily bars carry no code (the request names it)."""
    try:
        if source == "Yahoo日K":  # v8 chart of 000001.SS: exchange-local dates, closes to 0.01
            meta = json.loads(raw)["chart"]["result"][0]["meta"]
            if str(meta.get("symbol", "")).upper() != "000001.SS":
                raise ValueError(f"Yahoo 日 K 代码不是上证指数 000001.SS（{clean_error(str(meta.get('symbol') or '空'))}）")
            return parse_yahoo_daily(raw)
        if source == "新浪日K":
            rows = sina_daily_rows(raw)
        else:
            data = json.loads(raw)["data"]
            if source == "腾讯日K":  # {"data": {"sh000001": {"day": [["2026-09-24", open, close, high, low, vol], ...]}}}
                if "sh000001" not in data:
                    raise ValueError("腾讯日 K 没有返回 sh000001")
                rows = [(r[0], r[1], r[2]) for r in data["sh000001"].get("day") or [] if len(r) > 2]
            else:  # Eastmoney: {"data": {"code": "000001", "market": 1, "klines": ["2026-09-24,open,close", ...]}}
                if str(data.get("code")) != "000001" or int(data.get("market", -1)) != 1:
                    raise ValueError(f"东方财富日 K 代码不是上证指数（{data.get('market')}.{data.get('code')}）")
                rows = [tuple(str(line).split(",")[:3]) for line in data.get("klines") or []]
        bars = sorted((dt.date.fromisoformat(day), _open_price(opening), number(close, "上证收盘")) for day, opening, close in rows)
    except (ValueError, KeyError, TypeError, AttributeError, IndexError) as error:
        raise ValueError(f"{source}格式异常：{clean_error(error)}") from None
    if not bars:
        raise ValueError(f"{source}没有返回任何交易日")
    return bars


@dataclass(frozen=True)
class DailyClose:
    """A close confirmed by a dated exchange daily bar."""
    day: dt.date
    value: D
    prev: D | None
    source: str
    checked_ms: int  # when the bar was read

    @property
    def close_ms(self) -> int:
        return int(dt.datetime.combine(self.day, dt.time(15, 0), BEIJING).timestamp() * 1000)


LIVE_STALE_MS = 10 * 60_000  # a live feed silent this long while its market trades is stale


def quiet_ms(quoted_ms: int, now_ms: int, lunch: tuple[dt.time, dt.time] | None = None) -> int:
    """Time since ``quoted_ms``; a lunch break (Beijing time) does not count."""
    age = now_ms - quoted_ms
    if lunch:
        day = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING).date()
        start, end = (int(dt.datetime.combine(day, t, BEIJING).timestamp() * 1000) for t in lunch)
        age -= max(0, min(end, now_ms) - max(start, quoted_ms))
    return age


def quote_stale(q: IndexQuote, now_ms: int, limit_ms: int, lunch: tuple[dt.time, dt.time] | None = None) -> bool:
    """True when a live quote has not updated within ``limit_ms`` (a lunch break pauses the clock)."""
    return quiet_ms(q.quoted_ms, now_ms, lunch) > limit_ms


def quote_problem(quoted_ms: int, now_ms: int, live: bool, last_end_ms: int,
                  lunch: tuple[dt.time, dt.time] | None = None) -> str:
    """Why a quote cannot stand for its market now ("" when it can). Its market time must be known; while the
    market trades it must have updated within 10 minutes (a lunch break does not count); while the market is shut
    it must come from the session that ended last (whose final hour may be thin)."""
    if quoted_ms <= 0:
        return "报价时间未知"
    if live and quiet_ms(quoted_ms, now_ms, lunch) > LIVE_STALE_MS:
        return f"报价停在 {stamp(quoted_ms, seconds=False)}，已超 10 分钟未更新"
    if not live and quoted_ms < last_end_ms - 60 * 60_000:
        return f"报价停在 {stamp(quoted_ms, seconds=False)}，早于最近一个交易时段"
    return ""


def quote_time(ms: int) -> str:
    """'09-28 10:05', or 时间未知 for a quote without a market time."""
    return stamp(ms, seconds=False) if ms > 0 else "时间未知"


class CnIndex:
    """Shanghai Composite (000001) plus FTSE China A50 futures (SGX) as its after-hours proxy.

    Composite: Tencent → Sina → Eastmoney. A50: Eastmoney 104.CN00Y (month-continuous contract)
    then Sina's CFD as a labelled last resort. Refreshed every 10 s while either trades, else every 60 s.
    The close used as the after-hours reference comes only from a dated daily bar (Tencent, then Eastmoney,
    Sina, Yahoo 000001.SS): a realtime "last price" says nothing reliable about which session it closed.
    """
    REFRESH_SECONDS = 60      # between sessions
    SESSION_SECONDS = 10      # while Shanghai trades (its own index) or an A50 session runs (the after-hours proxy)
    DAILY_SECONDS = 600       # re-read the daily bars this often once the expected close is confirmed
    PENDING_SECONDS = 60      # ...and this often while the close the calendar expects is not confirmed yet
    STALE_MS = 10 * 60_000    # a live quote older than this is not used for new probabilities
    DAILY_SOURCES = (("腾讯日K", "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,,,40,",
                      {"Referer": "https://gu.qq.com/"}),
                     ("东方财富日K", "https://push2his.eastmoney.com/api/qt/stock/kline/get?klt=101&fqt=0&end=20500101"
                                    "&lmt=40&fields1=f1,f2,f3&fields2=f51,f52,f53&secid=1.000001",
                      {"Referer": "https://quote.eastmoney.com/"}),
                     # the same dated bars from two more hosts, for the evenings the first two are unreachable
                     ("新浪日K", "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
                                "?symbol=sh000001&scale=240&ma=no&datalen=40", {"Referer": "https://finance.sina.com.cn/"}),
                     ("Yahoo日K", yahoo_url("000001.SS", "3mo"), {}))
    SSE_SOURCES = (("腾讯", "https://qt.gtimg.cn/q=sh000001", {"Referer": "https://gu.qq.com/"}),
                   ("新浪", "https://hq.sinajs.cn/list=sh000001", {"Referer": "https://finance.sina.com.cn/"}),
                   ("东方财富", IndexFutures.EM + "1.000001", {"Referer": "https://quote.eastmoney.com/"}))
    # The live A50 price: Eastmoney's quote, else the latest bar of the same contract's 1-minute K line
    # (another Eastmoney host, same futures, so it can still be mapped against the Eastmoney anchor),
    # and Sina's CFD only as a labelled last resort that is never mixed with a futures anchor.
    A50_SOURCES = (("东方财富", IndexFutures.EM + "104.CN00Y", {"Referer": "https://quote.eastmoney.com/"}),
                   ("东方财富K线", "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=104.CN00Y&klt=1&fqt=0"
                                  "&lmt=30&end=20500101&fields1=f1&fields2=f51,f52,f53", {"Referer": "https://quote.eastmoney.com/"}),
                   ("新浪CFD", "https://hq.sinajs.cn/list=hf_CHA50CFD", {"Referer": "https://finance.sina.com.cn/"}))
    A50_MINUTES = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=104.CN00Y&klt=1&fqt=0"
                   "&fields1=f1&fields2=f51,f52,f53&beg={beg}&end={end}")
    A50_FIVE_MINUTES = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=104.CN00Y&klt=5&fqt=0"
                        "&fields1=f1&fields2=f51,f52,f53&beg={beg}&end={end}")
    # Sina's 5-minute bars of the same hf_CHA50CFD record the live Sina quote comes from (its own family).
    A50_SINA_FIVE_MINUTES = ("https://gu.sina.cn/ft/api/jsonp.php/var%20_CHA50CFD_5=/GlobalService.getMink"
                             "?symbol=CHA50CFD&type=5")

    def __init__(self, enabled: bool = True, holidays: frozenset = frozenset()):
        self.enabled = enabled
        self.holidays = holidays
        self.quote: IndexQuote | None = None
        self.a50: IndexQuote | None = None
        self.close: DailyClose | None = None
        self.bars: list[tuple[dt.date, D]] = []
        self.opens: dict[dt.date, D] = {}  # daily-bar opens (volatility split only)
        self.error = ""
        self.a50_error = ""
        self.a50_skipped = ""  # why earlier A50 sources failed when a later one answered
        self.daily_error = ""
        self.refreshed = -1e9
        self.refreshed_ms = 0  # market clock of the last refresh (the 15:00 close is caught at once)
        self.daily_refreshed = -1e9

    @staticmethod
    def parse_a50(source: str, raw: bytes, now_ms: int) -> IndexQuote:
        if source == "东方财富":
            data = parse_eastmoney_quote(raw)
            if not a50_code_ok(data.get("f57")):
                raise ValueError(f"东方财富返回的代码不是 A50 合约（{clean_error(str(data.get('f57')))}）")
            if not str(data.get("f86", "")).isdigit():
                raise ValueError("东方财富 A50 报价缺少行情时间")
            q = parse_eastmoney_index(raw, now_ms, "A50期货")
            return IndexQuote("A50期货", q.last, q.prev_close, q.open, q.high, q.low, q.quoted_ms, source)
        if source == "东方财富K线":  # latest finished 1-minute bar: "2026-09-26 05:15,open,close"
            try:
                data = json.loads(raw)["data"]
                if not a50_code_ok(data.get("code")):
                    raise ValueError(f"代码不是 A50 合约（{data.get('code')}）")
                when, _, close = str(data["klines"][-1]).split(",")[:3]
                quoted_ms = int(dt.datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=BEIJING).timestamp() * 1000)
            except (ValueError, KeyError, IndexError, TypeError, AttributeError) as error:
                raise ValueError(f"东方财富 A50 K 线格式异常：{clean_error(error)}") from None
            return IndexQuote("A50期货", number(close, "A50"), None, None, None, None, quoted_ms, source)
        # Sina hf_: last, ?, bid, ask, high, low, time [6], prev settle, open, ..., date, name (see sina_hf_time)
        match = re.search(r'(\w+)="([^"]*)"', raw.decode("gbk", errors="ignore"))
        if match and not match.group(1).endswith("CHA50CFD"):
            raise ValueError(f"新浪返回的代码不是 A50（{match.group(1)}）")
        fields = match.group(2).split(",") if match else []
        if len(fields) < 13 or not fields[0]:
            raise ValueError("新浪 A50 报价为空")
        quoted_ms, _ = sina_hf_time(fields, " A50 ")
        return IndexQuote("A50期货", number(fields[0], "A50"), _opt(fields[7]), _opt(fields[8]), _opt(fields[4]),
                          _opt(fields[5]), quoted_ms, source)

    LUNCH = (dt.time(11, 30), dt.time(13, 0))

    def sse_session(self, now_ms: int) -> tuple[bool, int]:
        """(the Shanghai continuous session runs now, when the latest finished session closed)."""
        local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
        live = (local.weekday() < 5 and local.date() not in self.holidays
                and dt.time(9, 30) <= local.time() < dt.time(15, 0))
        last = expected_close_date("sh", now_ms, self.holidays)
        return live, int(dt.datetime.combine(last, dt.time(15, 0), BEIJING).timestamp() * 1000)

    def sse_problem(self, q: IndexQuote, now_ms: int) -> str:
        """Why q cannot stand for the Composite now ("" when it can): see quote_problem."""
        live, last_end = self.sse_session(now_ms)
        return quote_problem(q.quoted_ms, now_ms, live, last_end, self.LUNCH)

    def a50_problem(self, q: IndexQuote, now_ms: int) -> str:
        """Why q cannot stand for A50 now ("" when it can): a print from the session running, or from the last one."""
        return quote_problem(q.quoted_ms, now_ms, a50_session(now_ms) != "休市", a50_last_session_end(now_ms))

    def cadence(self, now_ms: int) -> int:
        """Seconds between refreshes: SESSION_SECONDS while the Composite or the A50 trades, else REFRESH_SECONDS."""
        live = stock_live_window("sh", now_ms, self.holidays) or a50_session(now_ms) != "休市"
        return self.SESSION_SECONDS if live else self.REFRESH_SECONDS

    def close_due(self, now_ms: int) -> bool:
        """The first refresh after the 15:00 close is not left to the cadence: the Composite's closing print stands
        in for the daily bar, and the first A50 print after the close is the anchor when no dated bar can be had."""
        local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
        if local.weekday() >= 5 or local.date() in self.holidays:
            return False
        close = int(dt.datetime.combine(local.date(), CALENDAR.close_time("sh", local.date()), BEIJING).timestamp() * 1000)
        return self.refreshed_ms < close <= now_ms

    async def refresh(self, now_ms: int, force: bool = False) -> Refreshed | bool:
        if not self.enabled or not (force or time.monotonic() - self.refreshed >= self.cadence(now_ms) or self.close_due(now_ms)):
            return False  # not due yet: nothing fetched
        self.refreshed, self.refreshed_ms = time.monotonic(), now_ms
        # a source whose answer parses but is stale or undated does not end the search; nothing current keeps the newest
        quote, _, self.error = await pick_quote(self.SSE_SOURCES, lambda n, r: parse_cn_index(n, r, now_ms),
                                                lambda q: self.sse_problem(q, now_ms))
        self.quote = newer(quote, self.quote) if self.error else quote
        a50, skipped, self.a50_error = await pick_quote(self.A50_SOURCES, lambda n, r: self.parse_a50(n, r, now_ms),
                                                        lambda q: self.a50_problem(q, now_ms))
        self.a50 = newer(a50, self.a50) if self.a50_error else a50
        self.a50_skipped = "" if self.a50_error else "；".join(skipped)  # why earlier sources were passed over
        if self.a50_skipped:
            LOG.info("A50 fell back to %s: %s", self.a50.source if self.a50 else "-", self.a50_skipped)
        daily = await self.refresh_daily(now_ms, force)
        return refreshed([f"上证：{self.error}" if self.error else "", f"A50：{self.a50_error}" if self.a50_error else "",
                          f"上证日K：{daily}" if daily else ""],
                         (not self.error) + (not self.a50_error) + (daily == ""))

    def expected_close(self, now_ms: int) -> dt.date:
        return expected_close_date("sh", now_ms, self.holidays)

    def confirmed(self, now_ms: int) -> bool:
        """The latest close the calendar expects is backed by a dated daily bar."""
        return self.close is not None and self.close.day >= self.expected_close(now_ms)

    def close_pending(self, now_ms: int) -> bool:
        """The cash session ended, but its dated daily bar has not confirmed the close yet."""
        local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
        return (local.weekday() < 5 and local.date() not in self.holidays and local.time() >= dt.time(15, 0)
                and (self.close is None or self.close.day < local.date()))

    async def refresh_daily(self, now_ms: int, force: bool = False) -> str | None:
        """Read the dated daily bars: every minute while the expected close is unconfirmed, else every 10 min.
        An answer that lags (its newest finished bar is older than the calendar expects) does not stop the search;
        answers are merged and the close never steps back. None = not due; "" = read; else what failed."""
        every = self.DAILY_SECONDS if self.confirmed(now_ms) else self.PENDING_SECONDS
        if not force and time.monotonic() - self.daily_refreshed < every:
            return None
        self.daily_refreshed = time.monotonic()
        expected, today = self.expected_close(now_ms), dt.datetime.fromtimestamp(now_ms / 1000, BEIJING).date()
        failures, got = [], False
        for name, url, extra in SOURCE_HEALTH.order(self.DAILY_SOURCES):
            try:
                raw = await fetch_source(url, extra)
                ohlc = [bar for bar in parse_cn_daily_ohlc(name, raw) if bar[0] <= today]
                bars = [(day, close) for day, _, close in ohlc]
                day, close, prev = last_completed_bar(bars, STOCK_MARKETS["sh"], now_ms)
            except Exception as error:
                failures.append(f"{name}: {clean_error(error)}")
                continue
            got = True
            if self.close is None or day >= self.close.day:  # never step back to an older session
                self.close = DailyClose(day, close, prev, name, now_ms)
            self.bars = sorted({**dict(self.bars), **dict(bars)}.items())[-60:]
            self.opens = dict(sorted({**self.opens, **{d: o for d, o, _ in ohlc if o}}.items())[-120:])
            if day >= expected:
                break  # this answer has the expected close: the next source is not needed
        # a source that answered but lags is not a failure (the bar may simply not be out yet)
        self.daily_error = "" if got else "；".join(failures)
        return self.daily_error

    def live_stale(self, now_ms: int) -> bool:
        return self.quote is not None and quote_stale(self.quote, now_ms, self.STALE_MS, self.LUNCH)

    def a50_stale(self, now_ms: int) -> bool:
        """Reject an old live quote, including one that predates the last completed A50 session. A thin final
        stretch (the live Sina feed's last Friday-night print was 04:56 for a 05:15 close) is fine."""
        return self.a50 is not None and bool(self.a50_problem(self.a50, now_ms))

    async def _a50_bar_at(self, day: dt.date, template: str, label: str) -> D:
        url = template.format(beg=(day - dt.timedelta(days=1)).strftime("%Y%m%d"),
                              end=(day + dt.timedelta(days=1)).strftime("%Y%m%d"))
        raw = await fetch_source(url, {"Referer": "https://quote.eastmoney.com/"})
        try:
            data = json.loads(raw)["data"]
            if not a50_code_ok(data.get("code")):
                raise ValueError("返回代码不是 A50 合约")
            klines = data["klines"]
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ValueError(f"A50 {label}返回格式或代码异常") from None
        wanted = f"{day.isoformat()} 15:00"
        for line in klines:
            parts = str(line).split(",")
            if parts[0] == wanted and len(parts) >= 3:
                return number(parts[2], f"A50 {label} 15:00")
        raise ValueError(f"A50 {label}里没有 {wanted}")

    async def a50_at(self, day: dt.date) -> D:
        """Prefer the 15:00 one-minute bar as the same-time A50 anchor."""
        return await self._a50_bar_at(day, self.A50_MINUTES, "1分钟K线")

    async def a50_sina_five_minute_at(self, day: dt.date) -> D:
        """Sina hf_CHA50CFD 5-minute bar stamped 15:00 on ``day`` (same record as the Sina live quote)."""
        raw = await fetch_source(self.A50_SINA_FIVE_MINUTES, {"Referer": "https://finance.sina.com.cn/"})
        bars = parse_sina_bars(raw)
        wanted = f"{day.isoformat()} 15:00"
        for when, close in bars:
            if when == wanted:
                return close
        span = f"{bars[0][0][5:]}～{bars[-1][0][5:]}" if bars else "无数据"
        raise ValueError(f"新浪 A50 5分钟K里没有 {wanted}（返回 {len(bars)} 根：{span}）")

    async def a50_five_minute_at(self, day: dt.date) -> D:
        """Use a dated 15:00 five-minute bar only as an explicitly approximate anchor."""
        return await self._a50_bar_at(day, self.A50_FIVE_MINUTES, "5分钟K线")

    def status(self, now_ms: int, holidays: frozenset) -> str:
        q = self.quote
        local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
        quoted = dt.datetime.fromtimestamp(q.quoted_ms / 1000, BEIJING) if q else local
        open_now = (local.weekday() < 5 and local.date() not in holidays and quoted.date() == local.date()
                    and dt.time(9, 30) <= local.time() < dt.time(15, 0))
        if local.date() in holidays or local.weekday() >= 5:
            return "休市"
        return "交易中" if open_now else "已收盘"

    def line(self, now_ms: int, style: str, holidays: frozenset) -> str:
        if not self.enabled:
            return ""
        q = self.quote
        if q is None:
            return f"🇨🇳 上证 ⚠️ 获取失败（{brief_error(self.error)}）" if self.error else "🇨🇳 上证 ⏳ 等待首次获取"
        line = f"🇨🇳 {bold('上证 ' + self.status(now_ms, holidays))} {bold(fmt(q.last))}"
        if q.prev_close:
            line += f" → 昨收 {bold(fmt(q.prev_close))} {pct_text(percent(q.last, q.prev_close), style)}（{q.last - q.prev_close:+,.2f}）"
        line += f"｜{quote_time(q.quoted_ms)} {q.source}" + (stale_note(q.quoted_ms, now_ms, BEIJING) if q.quoted_ms else "")
        if self.status(now_ms, holidays) != "交易中":
            if self.close_pending(now_ms):
                day = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING).date()
                why = f"；日 K 获取失败：{brief_error(self.daily_error)}" if self.daily_error else ""
                line += f"｜{bold('收盘价待确认')}（等待 {day.strftime('%m-%d')} 日 K{why}）"
            elif self.confirmed(now_ms):
                line += f"｜收盘 {self.close.day.strftime('%m-%d')} {bold(fmt(self.close.value))}（{self.close.source}确认）"
            else:
                why = f"；日 K 获取失败：{brief_error(self.daily_error)}" if self.daily_error else ""
                line += f"｜{bold('收盘价待确认')}（等待 {self.expected_close(now_ms).strftime('%m-%d')} 日 K{why}）"
        return line + f"｜⚠️ 刷新失败：{brief_error(self.error)}" if self.error else line

    def a50_line(self, now_ms: int, style: str, anchor: D | None, anchor_note: str = "15:00") -> str:
        if not self.enabled:
            return ""
        a = self.a50
        if a is None:
            return f"📈 A50期货 ⚠️ 获取失败（{brief_error(self.a50_error)}）" if self.a50_error else "📈 A50期货 ⏳ 等待首次获取"
        line = f"📈 {bold('A50期货 ' + a50_session(a.quoted_ms))} {bold(fmt(a.last))}"
        if anchor:
            label = "上证收盘时" if a50_anchor_exact(anchor_note) else "上证收盘附近"
            line += f" → {label} {bold(fmt(anchor))} {pct_text(percent(a.last, anchor), style)}"
            if anchor_note != "15:00":
                line += f"（{anchor_note}）"
        if a.prev_close:
            line += f"｜昨结 {fmt(a.prev_close)} {pct_text(percent(a.last, a.prev_close), style)}"
        source = a.source if "CFD" not in a.source else f"{a.source}·非交易所合约，仅参考"
        line += f"｜{stamp(a.quoted_ms, seconds=False)} {source}" + stale_note(a.quoted_ms, now_ms, BEIJING)
        if self.a50_stale(now_ms):
            line += "｜⚠️ 报价已超 10 分钟未更新"
        if a50_closed_note(now_ms) != "休市":
            line += f"｜{a50_closed_note(now_ms)}"
        if self.a50_skipped and not self.a50_error:
            line += f"｜⚠️ 前序源未取到：{brief_error(self.a50_skipped, 90)}"
        return line + f"｜⚠️ 刷新失败：{brief_error(self.a50_error)}" if self.a50_error else line


def merge_closes(daily: dict[dt.date, D], ranks: dict[dt.date, int], bars: list[tuple], rank: int,
                 keep: int = 30) -> tuple[dict[dt.date, D], dict[dt.date, int]]:
    """Merge one source's finished daily bars (date, open, close) into the known closes. A lagging answer never
    drops a day already known, and a day taken from a better-ranked source (lower rank) is not overwritten by a
    worse one; the same source may correct its own figure. The newest ``keep`` days are kept."""
    daily, ranks = dict(daily), dict(ranks)
    for day, _, close in bars:
        if ranks.get(day, rank) >= rank:
            daily[day], ranks[day] = close, rank
    kept = sorted(daily)[-keep:]
    return {day: daily[day] for day in kept}, {day: ranks[day] for day in kept}


class DailyCloses:
    """Dated official closes of an index from daily bars (a session's bar counts once it is final), refreshed every
    10 minutes and every minute from 15 minutes after the close until the day's bar is in. Realtime index feeds can
    be read before the closing auction's final value is published; a dated bar is the settled close."""

    def __init__(self, market: str, sources: tuple[tuple[str, str], ...], holidays: frozenset = frozenset()):
        self.market, self.sources, self.holidays = market, sources, holidays
        self.daily: dict[dt.date, D] = {}
        self.ranks: dict[dt.date, int] = {}  # which source each close came from (earlier in ``sources`` wins)
        self.refreshed = -1e9
        self.error = ""

    REFERERS = {"tencent": {"Referer": "https://gu.qq.com/"}, "eastmoney": {"Referer": "https://quote.eastmoney.com/"}}

    @staticmethod
    def parse(kind: str, market: str, raw: bytes) -> list[tuple[dt.date, D | None, D]]:
        if kind == "tencent":  # {"data": {"hkHSI": {"day": [["2026-09-28", open, close, high, low, volume], ...]}}}
            node = next(iter(json.loads(raw)["data"].values()))
            return [(dt.date.fromisoformat(r[0]), _open_price(r[1]), number(r[2], "收盘")) for r in (node.get("day") or node.get("qfqday") or [])
                    if len(r) > 2]
        if kind == "yahoo":
            return parse_yahoo_daily(raw)
        return parse_daily_ohlc(market, raw)

    async def refresh(self, now_ms: int) -> str | None:
        """None = not due; "" = closes read; else what failed."""
        info = STOCK_MARKETS[self.market]
        tz = dt.timezone(dt.timedelta(hours=info.utc_offset))
        local = dt.datetime.fromtimestamp(now_ms / 1000, tz)
        final = dt.datetime.combine(local.date(), CALENDAR.close_time(self.market, local.date()), tz) + dt.timedelta(minutes=15)
        waiting = (local.weekday() < 5 and local.date() not in self.holidays and local >= final
                   and local.date() not in self.daily)  # a holiday evening has no bar to wait for
        if time.monotonic() - self.refreshed < (60 if waiting else 600):
            return None
        self.refreshed, failures, got = time.monotonic(), [], False
        for rank, (kind, url) in enumerate(self.sources):
            try:
                raw = await fetch_source(url, self.REFERERS.get(kind))
                bars = finished_bars(sorted(self.parse(kind, self.market, raw)), self.market, now_ms)
                if not bars:
                    raise ValueError("日 K 为空")
            except Exception as error:
                failures.append(f"{kind}: {clean_error(error)}")
                continue
            # a failed or lagging answer keeps the closes already known; an earlier source wins a shared day
            self.daily, self.ranks = merge_closes(self.daily, self.ranks, bars, rank)
            got = True
            if not waiting or local.date() in self.daily:
                break  # the day's bar is in (or not due yet): no need to ask the fallbacks
        self.error = "" if got else "；".join(failures)
        return self.error


def dated_ref(daily: dict[dt.date, D], day: dt.date, live: D | None) -> tuple[D | None, str]:
    """(reference close, label): the dated daily close of ``day`` when known, else the realtime figure; when the
    two differ the label shows both."""
    official, label = daily.get(day), f"{day.strftime('%m-%d')} 收盘"
    if official is None:
        return live, label
    if live is not None and abs(percent(official, live)) >= D("0.005"):
        return official, f"{label}（日K；实时行情为 {fmt(live)}）"
    return official, label


class KospiIndex:
    """KOSPI composite index: Naver's realtime index feed first, Eastmoney (100.KS11) as fallback."""
    REFRESH_SECONDS = 60   # between sessions
    SESSION_SECONDS = 10   # while the KRX regular session runs (to 15 minutes after the close)
    SOURCES = (("Naver", "https://polling.finance.naver.com/api/realtime/domestic/index/KOSPI",
                {"Referer": "https://finance.naver.com/"}),
               ("东方财富", IndexFutures.EM + "100.KS11", {"Referer": "https://quote.eastmoney.com/"}))
    SOURCES_200 = (("Naver", "https://polling.finance.naver.com/api/realtime/domestic/index/KPI200",
                    {"Referer": "https://finance.naver.com/"}),)

    def __init__(self, enabled: bool = True, holidays: frozenset = frozenset()):
        self.enabled = enabled
        self.holidays = holidays
        self.quote: IndexQuote | None = None
        self.quote200: IndexQuote | None = None  # KOSPI 200, the index behind Hyperliquid's KR200 perp
        self.error = ""
        self.error200 = ""
        self.refreshed = -1e9
        self.daily: dict[dt.date, D] = {}   # dated official closes (Yahoo ^KS11, else Naver's daily chart)
        self.daily_rank: dict[dt.date, int] = {}  # which source each close came from (0 = Yahoo, the settlement one)
        self.daily_refreshed = -1e9
        self.daily_error = ""

    DAILY_URL = "https://fchart.stock.naver.com/sise.nhn?requestType=0&timeframe=day&count=10&symbol=KOSPI"
    YAHOO_URL = yahoo_url("^KS11")  # Predict's KOSPI markets resolve on Yahoo ^KS11

    @staticmethod
    def parse(source: str, raw: bytes, now_ms: int) -> IndexQuote:
        if source == "Naver":
            return parse_naver_index(raw, now_ms)
        return parse_eastmoney_index(raw, now_ms, "韩国KOSPI")

    def official_close(self, day: dt.date) -> D | None:
        """The dated close of ``day`` from the daily chart (only once that session is final)."""
        return self.daily.get(day)

    def session(self, now_ms: int) -> tuple[bool, int]:
        """(the KRX regular session runs now, when the latest finished session closed)."""
        kst = dt.timezone(dt.timedelta(hours=9))
        local = dt.datetime.fromtimestamp(now_ms / 1000, kst)
        live = (local.weekday() < 5 and local.date() not in self.holidays
                and CALENDAR.sessions("kr", local.date())[0][0] <= local.time() < CALENDAR.kr_time(KRX_SETTLED, local.date()))
        last = expected_close_date("kr", now_ms, self.holidays)
        return live, int(dt.datetime.combine(last, CALENDAR.close_time("kr", last), kst).timestamp() * 1000)

    def problem(self, q: IndexQuote, now_ms: int) -> str:
        """Why q cannot stand for the index now ("" when it can): see quote_problem."""
        live, last_end = self.session(now_ms)
        return quote_problem(q.quoted_ms, now_ms, live, last_end)

    async def refresh_daily(self, now_ms: int) -> str | None:
        """Every 10 minutes, and every minute from the close until the day's bar is in. Answers are merged by date:
        a lagging one never drops a close already known, and a day taken from Yahoo (what Predict settles on) is
        not overwritten by Naver. None = not due; "" = the latest session's close is known; else what went wrong."""
        kst = dt.timezone(dt.timedelta(hours=9))
        local = dt.datetime.fromtimestamp(now_ms / 1000, kst)
        waiting = (local.weekday() < 5 and local.date() not in self.holidays
                   and local.time() >= CALENDAR.kr_time(dt.time(15, 45), local.date()) and local.date() not in self.daily)
        if time.monotonic() - self.daily_refreshed < (60 if waiting else 600):
            return None
        self.daily_refreshed = time.monotonic()
        expected = expected_close_date("kr", now_ms, self.holidays)
        failures, got = [], False
        for rank, (name, url, parse) in enumerate((("Yahoo", self.YAHOO_URL, parse_yahoo_daily),
                                                   ("Naver", self.DAILY_URL, lambda raw: parse_daily_ohlc("kr", raw)))):
            try:
                extra = {"Referer": "https://finance.naver.com/"} if name == "Naver" else {}
                bars = finished_bars(parse(await fetch_source(url, extra)), "kr", now_ms)
                if not bars:
                    raise ValueError("no daily bars")
            except Exception as error:
                failures.append(f"{name}: {clean_error(error) or type(error).__name__}")
                continue
            got = True
            self.daily, self.daily_rank = merge_closes(self.daily, self.daily_rank, bars, rank)
            if bars[-1][0] >= expected:
                break  # this answer has the latest session: the next source is not needed
            failures.append(f"{name}: 日 K 最新为 {bars[-1][0]:%m-%d}，应有 {expected:%m-%d}")
        self.daily_error = "" if got else "；".join(failures)
        return "" if expected in self.daily else "；".join(failures)

    def cadence(self, now_ms: int) -> int:
        """Seconds between refreshes: SESSION_SECONDS while the KRX session runs, else REFRESH_SECONDS."""
        return self.SESSION_SECONDS if stock_live_window("kr", now_ms, self.holidays) else self.REFRESH_SECONDS

    async def refresh(self, now_ms: int, force: bool = False) -> Refreshed | bool:
        if not self.enabled or (not force and time.monotonic() - self.refreshed < self.cadence(now_ms)):
            return False  # not due yet: nothing fetched
        self.refreshed = time.monotonic()
        self.quote, self.error = await self._fetch(self.SOURCES, now_ms, self.quote)
        self.quote200, self.error200 = await self._fetch(self.SOURCES_200, now_ms, self.quote200)
        daily = await self.refresh_daily(now_ms)
        return refreshed([f"KOSPI：{self.error}" if self.error else "", f"KOSPI200：{self.error200}" if self.error200 else "",
                          f"日K：{daily}" if daily else ""],
                         (not self.error) + (not self.error200) + (daily == ""))

    async def _fetch(self, sources: tuple, now_ms: int, previous: IndexQuote | None) -> tuple[IndexQuote | None, str]:
        """The first source with a current quote; a stale or undated answer does not stop the search. Without a
        current one the newest quote known is kept, and the error says why."""
        quote, _, error = await pick_quote(sources, lambda name, raw: self.parse(name, raw, now_ms),
                                           lambda q: self.problem(q, now_ms))
        return (newer(quote, previous) if error else quote), error

    def line(self, now_ms: int, style: str) -> str:
        if not self.enabled:
            return ""
        q = self.quote
        if q is None:
            return f"🇰🇷 KOSPI ⚠️ 获取失败（{self.error}）" if self.error else "🇰🇷 KOSPI ⏳ 等待首次获取"
        status = q.status or krx_session(now_ms, self.holidays)
        line = f"🇰🇷 {bold('KOSPI ' + status)} {bold(fmt(q.last))}"
        if q.change is not None and q.prev_close:
            line += f" → 昨收 {bold(fmt(q.prev_close))} {pct_text(q.change / q.prev_close * 100, style)}（{q.change:+,.2f}）"
        kst = dt.timezone(dt.timedelta(hours=9))
        line += f"｜{quote_time(q.quoted_ms)} {q.source}" + (stale_note(q.quoted_ms, now_ms, kst) if q.quoted_ms and self.problem(q, now_ms) else "")
        return line + f"｜⚠️ 刷新失败：{brief_error(self.error)}" if self.error else line

    def line200(self, now_ms: int, style: str, hl: "HlQuote | None", hl_note: str = "") -> str:
        """KOSPI 200 versus Hyperliquid's KR200 perp, the 24/7 price for the same index."""
        if not self.enabled:
            return ""
        q = self.quote200
        if q is None:
            return f"🇰🇷 KOSPI200 ⚠️ 获取失败（{brief_error(self.error200)}）" if self.error200 else "🇰🇷 KOSPI200 ⏳ 等待首次获取"
        status = q.status or krx_session(now_ms, self.holidays)
        line = f"🇰🇷 {bold('KOSPI200 ' + status)} {bold(fmt(q.last))}"
        if q.change is not None and q.prev_close:
            line += f" → 昨收 {bold(fmt(q.prev_close))} {pct_text(q.change / q.prev_close * 100, style)}"
        if hl is not None:
            meta = f"（24h {pct_text(hl.day_change, style)}）" if hl.day_change is not None else ""
            line += f"｜🌊 HL {hl.coin.split(':')[-1]} {bold(fmt(hl.mark))} → 相对 KOSPI200 {pct_text(percent(hl.mark, q.last), style)}{meta}"
        elif hl_note:
            line += f"｜🌊 HL {brief_error(hl_note, 40)}"
        kst = dt.timezone(dt.timedelta(hours=9))
        line += f"｜{quote_time(q.quoted_ms)} {q.source}" + (stale_note(q.quoted_ms, now_ms, kst) if q.quoted_ms and self.problem(q, now_ms) else "")
        return line + f"｜⚠️ 刷新失败：{brief_error(self.error200)}" if self.error200 else line


# --- close-direction probability model ----------------------------------------------------------
# effective = reference close × proxy_now / proxy_at_reference_close   (proxy: Binance / HL / futures)
# P(strict up) = 1 − Φ(ln((ref + tick/2) / effective) / σ_remaining),  P(strict down) = Φ(ln((ref − tick/2) / effective) / σ)
# σ_remaining = σ_daily × √(remaining session minutes / session minutes); a flat close pays each side half.
SESSIONS = {  # continuous-trading intervals, local time
    "sh": ((dt.time(9, 30), dt.time(11, 30)), (dt.time(13, 0), dt.time(15, 0))),
    "sz": ((dt.time(9, 30), dt.time(11, 30)), (dt.time(13, 0), dt.time(15, 0))),
    "hk": ((dt.time(9, 30), dt.time(12, 0)), (dt.time(13, 0), dt.time(16, 0))),
    "kr": ((dt.time(9, 0), dt.time(15, 30)),),
}
HK_HALF_DAY_CLOSE = dt.time(12, 10)      # closing auction 12:00–12:10 on a half day
HK_HALF_DAY_FUTURES_END = dt.time(12, 30)  # HSI futures day session on a half day
KR_LATE_SHIFT = dt.timedelta(hours=1)   # the CSAT day runs one hour late


class SessionCalendar:
    """Session exceptions by market day: HKEX half days and KRX days that run late. One process-wide instance
    (CALENDAR), configured from the environment by Config.from_env and consulted by every session-time helper, so
    "12-24 is a half day" is one fact rather than a dozen call sites remembering it."""

    def __init__(self) -> None:
        self.hk_half: frozenset = frozenset()
        self.kr_late: frozenset = frozenset()
        self.sg_holidays: frozenset = frozenset()  # SGX closures (the A50 futures trade neither session)
        self.sg_half: frozenset = frozenset()      # SGX half days (A50 morning only, no T+1 session)

    def configure(self, hk_half: frozenset, kr_late: frozenset, sg_holidays: frozenset = frozenset(),
                  sg_half: frozenset = frozenset()) -> None:
        self.hk_half, self.kr_late = frozenset(hk_half), frozenset(kr_late)
        self.sg_holidays, self.sg_half = frozenset(sg_holidays), frozenset(sg_half)

    def half_day(self, market: str, day: dt.date) -> bool:
        return (market == "hk" and day in self.hk_half) or (market == "sg" and day in self.sg_half)

    def late_day(self, market: str, day: dt.date) -> bool:
        return market == "kr" and day in self.kr_late

    def kr_time(self, base: dt.time, day: dt.date) -> dt.time:
        """A KRX clock time on ``day`` (KRX_SETTLED, KRX_NXT_AFTER... are one hour later on a late day)."""
        if not self.late_day("kr", day):
            return base
        return (dt.datetime.combine(day, base) + KR_LATE_SHIFT).time()

    def close_time(self, market: str, day: dt.date) -> dt.time:
        """When the official close is fixed on ``day`` (local time)."""
        if self.half_day(market, day):
            return HK_HALF_DAY_CLOSE
        if self.late_day(market, day):
            return self.kr_time(STOCK_MARKETS["kr"].close_time, day)
        return STOCK_MARKETS[market].close_time

    def close_time_for(self, info: StockMarketInfo, day: dt.date) -> dt.time:
        market = next((key for key, value in STOCK_MARKETS.items() if value is info), "")
        return self.close_time(market, day) if market else info.close_time

    def sessions(self, market: str, day: dt.date) -> tuple:
        """The continuous-trading intervals of ``day`` (local time)."""
        if self.half_day(market, day):
            return (SESSIONS["hk"][0],)
        if self.late_day(market, day):
            return tuple((self.kr_time(a, day), self.kr_time(b, day)) for a, b in SESSIONS["kr"])
        return SESSIONS[market]

    def hk_futures_day_end(self, day: dt.date) -> dt.time:
        return HK_HALF_DAY_FUTURES_END if self.half_day("hk", day) else dt.time(16, 30)

    def note(self, market: str, day: dt.date) -> str:
        """'半日市' / '高考日延后 1 小时' for labels, "" on an ordinary day."""
        if self.half_day(market, day):
            return "半日市"
        if self.late_day(market, day):
            return "高考日延后 1 小时"
        return ""


CALENDAR = SessionCalendar()
CALENDAR.configure(parse_dates(DEFAULT_SPECIAL_DAYS["HK_HALF"], "HK_HALF_DAYS"), parse_dates(DEFAULT_SPECIAL_DAYS["KR_LATE"], "KR_LATE_DAYS"),
                   parse_dates(DEFAULT_HOLIDAYS["SG"], "HOLIDAYS_SG"), parse_dates(DEFAULT_SPECIAL_DAYS["SG_HALF"], "SG_HALF_DAYS"))
PRIOR_VOL = {"sh": 0.035, "sz": 0.035, "hk": 0.03, "kr": 0.03, "HSI": 0.013, "KOSPI": 0.02, "SSE": 0.011}
PRIOR_WEIGHT = 10  # pseudo-observations given to the prior when blending with estimated volatility


def norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def price_tick(market: str, price: D, index: bool = False) -> D:
    """Minimum price step, so an exactly-flat close gets its own (small) probability."""
    if index or market in {"sh", "sz"}:
        return D("0.01")
    table = {"hk": ((D("0.25"), "0.001"), (D("0.5"), "0.005"), (D(10), "0.01"), (D(20), "0.02"), (D(100), "0.05"),
                    (D(200), "0.1"), (D(500), "0.2"), (D(1000), "0.5"), (D(2000), "1"), (D(5000), "2")),
             "kr": ((D(2000), "1"), (D(5000), "5"), (D(20000), "10"), (D(50000), "50"), (D(200000), "100"),
                    (D(500000), "500"))}
    last = {"hk": "5", "kr": "1000"}
    for bound, step in table.get(market, ()):
        if price < bound:
            return D(step)
    return D(last.get(market, "0.01"))


def session_remaining(market: str, now_ms: int, close_date: dt.date | None = None,
                      holidays: frozenset = frozenset()) -> tuple[float, dt.date]:
    """(share of a full session's variance still ahead, target close date) for the next close.

    Weekends and the configured exchange holidays are skipped; each skipped weekday holiday adds
    half a session of variance (news keeps arriving while the market is shut).
    """
    info = STOCK_MARKETS[market]
    tz = dt.timezone(dt.timedelta(hours=info.utc_offset))
    local = dt.datetime.fromtimestamp(now_ms / 1000, tz)
    total = sum((dt.datetime.combine(local.date(), b) - dt.datetime.combine(local.date(), a)).seconds
                for a, b in SESSIONS[market]) / 60  # a full ordinary session is the unit of variance
    today = local.date()
    trading_today = today.weekday() < 5 and today not in holidays
    finalised = local >= dt.datetime.combine(today, CALENDAR.close_time(market, today), tz) + dt.timedelta(minutes=15)
    if trading_today and not finalised and (close_date is None or close_date < today):
        remaining = sum(max(0.0, (dt.datetime.combine(today, b, tz) - max(dt.datetime.combine(today, a, tz), local)).total_seconds())
                        for a, b in CALENDAR.sessions(market, today)) / 60  # a half day has less of it ahead
        if auction_running(market, now_ms, holidays):
            # the closing auction is still finding the close: HK's runs after continuous trading, so the minutes left
            # would be none and the close look all but decided from a 0.1% lead; it is worth a fixed share instead
            remaining = max(remaining, AUCTION_VARIANCE_MINUTES.get(market, 0.0))
        return max(remaining, 1.0) / total, today
    target, skipped = today + dt.timedelta(days=1), 0
    while target.weekday() >= 5 or target in holidays:
        skipped += target.weekday() < 5
        target += dt.timedelta(days=1)
    if not trading_today and today.weekday() < 5:
        skipped += 1  # today itself is a holiday
    return 1.0 + 0.5 * skipped, target


def expected_close_date(market: str, now_ms: int, holidays: frozenset = frozenset()) -> dt.date:
    """The latest session whose close should already be final, by the weekday + holiday calendar.

    Only used to tell whether a confirmed close is overdue; a baseline is never created from it.
    """
    info = STOCK_MARKETS[market]
    tz = dt.timezone(dt.timedelta(hours=info.utc_offset))
    local = dt.datetime.fromtimestamp(now_ms / 1000, tz)
    day = local.date()
    if local < dt.datetime.combine(day, CALENDAR.close_time(market, day), tz) + dt.timedelta(minutes=15):
        day -= dt.timedelta(days=1)
    for _ in range(60):
        if day.weekday() < 5 and day not in holidays:
            return day
        day -= dt.timedelta(days=1)
    return day


@dataclass(frozen=True)
class CloseOdds:
    """Model probability that the next close ends above / at / below the reference close."""
    name: str
    target: dt.date
    ref: D
    ref_note: str
    proxy_note: str        # e.g. "币安 73.45 / 收盘时刻 72.81 → +0.879%"
    effective: D
    sigma_daily: float
    sigma_note: str
    remaining: float       # share of a session's variance left
    up: float
    flat: float
    down: float
    unit: str = ""
    beta: float = 1.0      # proxy coefficient used for the effective price
    mode: str = "盘中"     # "盘中" = live index vs previous close; "盘后" = mapped from an after-hours proxy
    warn: str = ""         # the inputs are aging: shown, but no trade suggestion is made from these odds
    tick: D = D("0.01")    # the price step a flat close is measured with

    @property
    def direct(self) -> bool:
        """The effective price is the underlying's own print (live, the pre-open auction's indicative price once orders
        cannot be withdrawn, or the matched opening price), not a proxy-mapped estimate."""
        return "直接用现货" in self.proxy_note or self.preopen or self.matched

    @property
    def preopen(self) -> bool:
        """The effective price is the pre-open auction's indicative price (参考平衡价): the open is not matched yet."""
        return "开市前竞价参考价" in self.proxy_note

    @property
    def matched(self) -> bool:
        """The effective price is the opening price the auction matched, before continuous trading."""
        return "开盘价已撮合" in self.proxy_note

    @property
    def move(self) -> float:
        """The proxy's own log move since the anchor (before β)."""
        return math.log(float(self.effective / self.ref)) / self.beta

    @property
    def sigma(self) -> float:
        return self.sigma_daily * math.sqrt(self.remaining)

    @property
    def z(self) -> float:
        return math.log(float(self.effective / self.ref)) / self.sigma

    @property
    def fair_up(self) -> float:
        return self.up + self.flat / 2

    @property
    def fair_down(self) -> float:
        return self.down + self.flat / 2

    def row(self) -> str:
        """Compact status row: '🎲 09-28收 涨 41.0¢｜跌 59.0¢（有效 6,958.67·σ 3.02%）'."""
        return (f"🎲 {self.target.strftime('%m-%d')}收 涨 {bold(f'{self.fair_up * 100:.1f}¢')}｜跌 {bold(f'{self.fair_down * 100:.1f}¢')}"
                f"（{'开盘价' if self.matched else '竞价参考' if self.preopen else '有效'} {fmt(self.effective.quantize(D('0.01')))}·σ {self.sigma * 100:.2f}%）")

    def detail(self) -> list[str]:
        unit = f" {self.unit}" if self.unit else ""
        return [
            f"参考收盘 {bold(fmt(self.ref) + unit)}（{self.ref_note}）→ 目标 {self.target.strftime('%m-%d')} 收盘",
            f"代理 {self.proxy_note}",
            f"有效价 {bold(fmt(self.effective.quantize(D('0.0001'))) + unit)}（{percent(self.effective, self.ref):+.3f}%）",
            f"σ 日 {self.sigma_daily * 100:.2f}%（{self.sigma_note}）× √{self.remaining:.3f} = {self.sigma * 100:.2f}%｜z {self.z:+.3f}",
            f"涨 {self.up * 100:.2f}%·平 {self.flat * 100:.2f}%·跌 {self.down * 100:.2f}% → 公平价 涨 {bold(f'{self.fair_up * 100:.1f}¢')} / 跌 {bold(f'{self.fair_down * 100:.1f}¢')}",
        ] + ([f"⚠️ {self.warn}"] if self.warn else [])


def close_odds(name: str, ref: D, effective: D, sigma_daily: float, remaining: float, target: dt.date, tick: D,
               ref_note: str, proxy_note: str, sigma_note: str, unit: str = "", beta: float = 1.0,
               mode: str = "盘中") -> CloseOdds:
    sigma = max(sigma_daily * math.sqrt(max(remaining, 1e-6)), 1e-9)
    half = tick / 2
    hi = math.log(float((ref + half) / effective)) / sigma
    lo = math.log(float((ref - half) / effective)) / sigma if ref > half else -math.inf
    up, down = 1 - norm_cdf(hi), norm_cdf(lo)
    return CloseOdds(name, target, ref, ref_note, proxy_note, effective, sigma_daily, sigma_note, remaining,
                     up, max(0.0, 1 - up - down), down, unit, beta, mode, tick=tick)


MODEL_SIGMA_ERROR = 1.25  # σ is an estimate: a suggested edge must survive σ × or ÷ this
MODEL_BETA_ERROR = 0.25   # a proxy's coefficient (β) is uncertain by about this much
LOG_DRIFT_VARIANT = 0.0   # the other drift convention a touch model's error allows for: a driftless log price (the
#                           default, drift −½σ², is a driftless price); the point estimate keeps the default


def model_swing(odds: CloseOdds) -> float:
    """How far the fair 涨 price moves under plausible model error: σ ×/÷ 1.25 and, for an estimate mapped from a proxy,
    β ± 0.25 (each varied alone). An edge smaller than this lies within the model's own error."""
    def fair(effective: D, sigma: float) -> float:
        return close_odds(odds.name, odds.ref, effective, sigma, odds.remaining, odds.target, odds.tick, "", "", "").fair_up
    variants = [fair(odds.effective, odds.sigma_daily * MODEL_SIGMA_ERROR), fair(odds.effective, odds.sigma_daily / MODEL_SIGMA_ERROR)]
    if not odds.direct:
        for beta in (odds.beta + MODEL_BETA_ERROR, max(0.05, odds.beta - MODEL_BETA_ERROR)):
            variants.append(fair(odds.ref * D(str(math.exp(beta * odds.move))), odds.sigma_daily))
    return max(abs(v - odds.fair_up) for v in variants)


PRED_EVERY_MS = 30 * 60_000   # one saved prediction snapshot per index per 30 minutes
BETA_WINDOW_DAYS = 60         # the after-hours β regression uses snapshots of the last this many days
BETA_PRIOR_DAYS = 10          # weight (in days) of the configured β when it is blended with the fitted slope
BETA_MIN_DAYS = 5             # fewer closed target days than this: the configured β stands
BETA_BOUNDS = (0.3, 1.2)      # the blended β stays in this range (a proxy never moves the index several times over)
BETA_REFRESH_SECONDS = 3600   # the fit is redone this often (new outcomes arrive once a day)
PRED_KEEP_DAYS = 400          # snapshots older than this are pruned (a year of /calib history, not an ever-growing table)
MARK_EVERY_MS = 2 * 3_600_000  # one saved snapshot of every priced Predict market (ladder levels included) per 2 hours
MARK_KEEP_DAYS = 120           # ...kept this long: scored by /calib against the market's own middle once it has a result
CALIB_MIN_DAYS = 10           # walk-forward: target days used only for training before the first test day


def wilson(hits: float, n: float, z: float = 1.96) -> tuple[float, float]:
    """The 95% Wilson score interval of a rate seen ``hits`` times in ``n`` tries: 60 of 100 is 50–69%, not "60% give
    or take a point". Counted in days here (snapshots of one day share its outcome)."""
    if n <= 0:
        return 0.0, 1.0
    p = hits / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def brier_scores(ps: list[float], hits: list[float]) -> tuple[float, float]:
    """(Brier, log loss) of probabilities against outcomes (1, 0 or ½)."""
    brier = sum((p - h) ** 2 for p, h in zip(ps, hits)) / len(ps)
    loss = -sum(h * math.log(min(max(p, 1e-6), 1 - 1e-6)) + (1 - h) * math.log(min(max(1 - p, 1e-6), 1 - 1e-6))
                for p, h in zip(ps, hits)) / len(ps)
    return brier, loss


def market_baseline_line(rows: list[dict], unit: str = "日") -> str:
    """The model against the Predict middle it was trading against, on the scored rows that saved one: Brier / log loss
    of each, and how often the model was the closer of the two. "" without any. A model that does not beat the market
    here has no edge to sell, whatever its own calibration says."""
    both = [r for r in rows if r.get("mkt") is not None]
    if not both:
        return ""
    model, market = brier_scores([r["up"] for r in both], [r["hit"] for r in both]), brier_scores([r["mkt"] for r in both], [r["hit"] for r in both])
    closer = sum(abs(r["up"] - r["hit"]) < abs(r["mkt"] - r["hit"]) - 1e-12 for r in both) / len(both)
    units = len({r.get("target") or r.get("market") for r in both})
    return (f"  对比 Predict 盘口中间价（{len(both)} 条/{units} {unit}）：模型 Brier {model[0]:.3f} / 对数损失 {model[1]:.3f}｜"
            f"市场 {market[0]:.3f} / {market[1]:.3f}｜模型更接近结果的占 {closer * 100:.0f}%"
            + ("（模型没有赢过市场：优势只是模型声称的）" if model[0] >= market[0] else ""))


def calibration_report(preds: list[dict], outcomes: dict[str, float]) -> list[str]:
    """Score saved predictions against the official closes, per index and mode.

    Brier / log loss of the live model versus a coin flip, a reliability table, and a fit of
    ln(close/ref) = a + b·(proxy log move) + e with e ~ N(0, k²·R). The fit is scored walk-forward by
    target day (each day is predicted only from days already closed) and is never applied automatically.
    Snapshots of the same target day are strongly correlated: the real sample size is the day count.
    """
    lines: list[str] = []
    groups: dict[tuple[str, str], list[dict]] = {}
    for p in preds:
        groups.setdefault((p["key"], p.get("mode", "")), []).append(p)
    for (key, mode), rows in sorted(groups.items()):
        done = [dict(r, close=outcomes[f"{key}:{r['target']}"]) for r in rows if f"{key}:{r['target']}" in outcomes]
        days = sorted({r["target"] for r in done})
        lines.append(f"📐 {key}·{mode}：快照 {len(rows)} 条（{len({r['target'] for r in rows})} 个目标日），"
                     f"已有结果 {len(done)} 条 / {len(days)} 日")
        if not done:
            continue
        for r in done:
            # scored as displayed: "up" against the reference the card showed (the Predict target price when used);
            # the proxy fit below uses the model's own reference
            r["hit"] = 1.0 if r["close"] > r["ref"] else 0.5 if r["close"] == r["ref"] else 0.0
            r["y"] = math.log(r["close"] / r.get("ref_raw", r["ref"]))
        struck = sum(1 for r in done if r.get("strike"))
        if struck:
            lines.append(f"  其中 {struck} 条按 Predict 目标价评估（与网页显示一致）")

        scores = brier_scores
        brier, loss = scores([r["up"] for r in done], [r["hit"] for r in done])
        lines.append(f"  现行模型：Brier {brier:.3f}（抛硬币 0.250）｜对数损失 {loss:.3f}（0.693）")
        baseline = market_baseline_line(done)
        if baseline:
            lines.append(baseline)
        bins = []
        for lo in (0.0, 0.2, 0.4, 0.6, 0.8):
            sel = [r for r in done if lo <= r["up"] < lo + 0.2 or (lo == 0.8 and r["up"] == 1.0)]
            if sel:
                # the interval is counted in days: a bin's snapshots of one day share one outcome, so 40 snapshots of
                # 4 days say no more than 4 tries do
                hit, bin_days = sum(r["hit"] for r in sel) / len(sel), len({r["target"] for r in sel})
                low, high = wilson(hit * bin_days, bin_days)
                bins.append(f"{lo * 100:.0f}–{lo * 100 + 20:.0f}%：预测 {sum(r['up'] for r in sel) / len(sel) * 100:.0f}% "
                            f"实际 {hit * 100:.0f}%（95% 区间 {low * 100:.0f}–{high * 100:.0f}%，{len(sel)} 条/{bin_days} 日）")
        lines.append("  校准：" + "；".join(bins) + "；区间按日数算，窄到能分辨几个点的差别之前，别把小数点后的优势当真")
        lines.extend(residual_lines(done))
        if mode != "盘后" or len(days) < 3:
            continue
        fit = fit_proxy(done)
        if fit:
            a, b, k = fit
            beta_now = sum(r["beta"] for r in done) / len(done)
            sigma_now = sum(r["sigma"] for r in done) / len(done)
            lines.append(f"  全样本拟合：系数 b {b:.2f}（现用 β {beta_now:g}）｜截距 {a * 100:+.3f}%｜"
                         f"残差 σ {k * 100:.2f}%/日（现用 σ 均值 {sigma_now * 100:.2f}%）")
        if len(days) < CALIB_MIN_DAYS + 5:
            lines.append(f"  逐日向前检验：目标日 {len(days)} 个，至少要 {CALIB_MIN_DAYS + 5} 个才下结论")
            continue
        tested, fitted = [], []
        for day in days[CALIB_MIN_DAYS:]:
            fit = fit_proxy([r for r in done if r["target"] < day])
            if not fit:
                continue
            a, b, k = fit
            for r in (r for r in done if r["target"] == day):
                tested.append(r)
                fitted.append(norm_cdf((a + b * r["move"]) / max(k * math.sqrt(max(r["R"], 1e-6)), 1e-9)))
        if tested:
            old = scores([r["up"] for r in tested], [r["hit"] for r in tested])
            new = scores(fitted, [r["hit"] for r in tested])
            lines.append(f"  逐日向前检验（{len({r['target'] for r in tested})} 日 {len(tested)} 条）：拟合模型 Brier {new[0]:.3f} / "
                         f"对数损失 {new[1]:.3f}，现行 {old[0]:.3f} / {old[1]:.3f}")
    return lines or ["📐 还没有保存的预测快照（每个指数每 30 分钟存一条，需要概率功能开启）"]


CALIB_BUCKETS = ((0.0, 0.25, "R<0.25"), (0.25, 0.5, "0.25–0.5"), (0.5, 1.0, "0.5–1"), (1.0, math.inf, "R≥1"))


def residual_lines(done: list[dict]) -> list[str]:
    """The model's own scale against the outcomes: the standardised residual z = (ln(close/ref) − β·move) / (σ·√R) of each
    scored snapshot, which is N(0, 1) when σ, β and the remaining share R are right. Its root mean square k says how far
    σ is off (k > 1: too small), its mean whether the proxy mapping leans one way; the same by bucket of R (a session
    nearly over, mid-session, after hours, a holiday ahead), since each of those rests on a different constant. Each
    target day carries one unit of weight (its snapshots share one outcome)."""
    rows = [r for r in done if r.get("sigma") and r.get("R") and r["sigma"] > 0 and r["R"] > 0]
    if not rows:
        return []
    per_day: dict[str, int] = {}
    for r in rows:
        per_day[r["target"]] = per_day.get(r["target"], 0) + 1

    def summary(sel: list[dict]) -> tuple[float, float, int]:
        ws = [1 / per_day[r["target"]] for r in sel]
        zs = [(r["y"] - r["beta"] * r["move"]) / (r["sigma"] * math.sqrt(r["R"])) for r in sel]
        total = sum(ws)
        return (sum(w * z for w, z in zip(ws, zs)) / total, math.sqrt(sum(w * z * z for w, z in zip(ws, zs)) / total),
                len({r["target"] for r in sel}))
    mean, rms, days = summary(rows)
    lines = [f"  标准化残差 z=(实际−预测)/σ剩余：均值 {mean:+.2f}，均方根 {rms:.2f}（校准为 1；>1 表示 σ 偏小）｜{len(rows)} 条/{days} 日"]
    parts = []
    for low, high, label in CALIB_BUCKETS:
        sel = [r for r in rows if low <= r["R"] < high]
        if sel:
            mean, rms, days = summary(sel)
            parts.append(f"{label}：{len(sel)} 条/{days} 日，预测 {sum(r['up'] for r in sel) / len(sel) * 100:.0f}% "
                         f"实际 {sum(r['hit'] for r in sel) / len(sel) * 100:.0f}%，残差均方根 {rms:.2f}")
    if len(parts) > 1:
        lines.append("  按剩余方差份额 R 分桶：" + "；".join(parts))
    return lines


def fit_proxy(rows: list[dict]) -> tuple[float, float, float] | None:
    """Weighted OLS of y = a + b·move, and k with e ~ N(0, k²·R): (a, b, k); None when underdetermined.

    Each target day carries the same total weight (its snapshots share one outcome), and the degrees
    of freedom are counted in days, so many snapshots of few days do not look like a large sample.
    """
    per_day: dict[str, int] = {}
    for r in rows:
        per_day[r["target"]] = per_day.get(r["target"], 0) + 1
    days = len(per_day)
    if days < 3:
        return None
    ws = [1 / per_day[r["target"]] for r in rows]
    xs, ys = [r["move"] for r in rows], [r["y"] for r in rows]
    total = sum(ws)
    mx, my = sum(w * x for w, x in zip(ws, xs)) / total, sum(w * y for w, y in zip(ws, ys)) / total
    sxx = sum(w * (x - mx) ** 2 for w, x in zip(ws, xs))
    if sxx <= 0:
        return None
    b = sum(w * (x - mx) * (y - my) for w, x, y in zip(ws, xs, ys)) / sxx
    a = my - b * mx
    k2 = sum(w * (y - a - b * x) ** 2 / max(r["R"], 1e-6) for w, x, y, r in zip(ws, xs, ys, rows)) / (days - 2)
    return a, b, math.sqrt(k2) if k2 > 0 else 1e-9


def realised_vol(closes: list[D]) -> tuple[float, int]:
    """Sample standard deviation of daily log returns, and the number of returns used."""
    values = [float(c) for c in closes if c and c > 0]
    returns = [math.log(b / a) for a, b in zip(values, values[1:])]
    if len(returns) < 2:
        return 0.0, len(returns)
    mean = sum(returns) / len(returns)
    return math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)), len(returns)


INTRADAY_SHARE_BOUNDS = (0.3, 1.0)  # in-session share of the daily variance is clamped to this range
INTRADAY_SHARE_MIN_DAYS = 10


def intraday_share(opens: list[D | None], closes: list[D]) -> tuple[float, int] | None:
    """Share of close-to-close variance that happens open→close: Σ ln(C/O)² / (Σ ln(O/C₋₁)² + Σ ln(C/O)²).

    During a session the opening gap has already happened, so only this share of the daily variance is
    still ahead (scaled by the session time left). None when too few days carry an open.
    """
    gap = body = 0.0
    n = 0
    for prev, opening, close in zip(closes, opens[1:], closes[1:]):
        if opening and prev and close and opening > 0 and prev > 0 and close > 0:
            gap += math.log(float(opening / prev)) ** 2
            body += math.log(float(close / opening)) ** 2
            n += 1
    if n < INTRADAY_SHARE_MIN_DAYS or gap + body <= 0:
        return None
    low, high = INTRADAY_SHARE_BOUNDS
    return min(high, max(low, body / (gap + body))), n


class VolBook:
    """Daily volatility per asset: manual override, else recent realised volatility blended with a prior."""
    REFRESH_SECONDS = 6 * 3600

    def __init__(self, overrides: dict[str, float]):
        self.overrides = dict(overrides)
        self.estimates: dict[str, tuple[float, int, str]] = {}   # key -> (sigma, n, source)
        self.shares: dict[str, tuple[float, int]] = {}           # key -> (in-session share of daily variance, n)
        self.refreshed: dict[str, float] = {}

    def due(self, key: str) -> bool:
        return key not in self.overrides and time.monotonic() - self.refreshed.get(key, -1e9) >= self.REFRESH_SECONDS

    def record(self, key: str, closes: list[D], source: str, opens: list[D | None] | None = None) -> None:
        """closes oldest first, finished sessions only; opens (same days) split the variance into gap and session."""
        sigma, n = realised_vol(closes)
        self.estimates[key] = (sigma, n, source)
        self.refreshed[key] = time.monotonic()
        share = intraday_share(opens, closes) if opens else None
        if share is None:
            self.shares.pop(key, None)
        else:
            self.shares[key] = share

    def get(self, key: str, prior_key: str, intraday: bool = False) -> tuple[float, str]:
        """Daily close-to-close σ. intraday=True: only the in-session part (today's opening gap has happened)."""
        if key in self.overrides:
            sigma, note = self.overrides[key], "PROB_VOL 手动设定"
        else:
            prior = PRIOR_VOL[prior_key]
            est, n, source = self.estimates.get(key, (0.0, 0, ""))
            if n < 2:
                sigma, note = prior, f"先验 {prior * 100:.1f}%，暂无历史"
            else:
                sigma = math.sqrt((n * est ** 2 + PRIOR_WEIGHT * prior ** 2) / (n + PRIOR_WEIGHT))
                note = f"{source} {n} 日 {est * 100:.2f}% 与先验 {prior * 100:.1f}% 加权"
        share = self.shares.get(key)
        if intraday and share is not None:
            return sigma * math.sqrt(share[0]), f"{note}；盘中取 {share[0] * 100:.0f}% 方差（{share[1]} 日开盘跳空已扣除）"
        return sigma, note


# --- Predict.fun orderbook vs model ---------------------------------------------------------------
# Each daily "X up or down on <date>" market has one orderbook, quoted for 涨 (Up) on a 0–1 scale.
# Buying 跌 at p is the same as selling 涨 at 1 − p, so for 跌 the prices are 1 − 卖1 (maker) / 1 − 买1 (taker).
PREDICT_GRAPHQL = "https://graphql.predict.fun/graphql"
PREDICT_REST = "https://api.predict.fun/v1"
PREDICT_SITE = "https://predict.fun/zh-cn/market/"
PREDICT_ITEMS = (("HSI", "恒生指数", "hk"), ("KOSPI", "KOSPI", "kr"), ("SSE", "上证指数", "sh"))
PREDICT_KEYS = {title: key for key, title, _ in PREDICT_ITEMS}
PREDICT_STALE_MS = 90_000       # a book older than this is shown as stale and never recommended
PREDICT_PARALLEL = 6            # Predict requests in flight at once (a hundred ladder books share the feeds' thread pool)
PREDICT_BACKOFF_SECONDS = 20    # after a 429 without retry_after: how long every Predict request waits
PREDICT_META_SECONDS = 600     # outcome names / status of a ladder market are re-read this often
PREDICT_REWARD_SECONDS = 60    # price-ladder reward metadata is refreshed every minute
PREDICT_REWARD_STALE_SECONDS = 120  # allows the normal refresh to finish across a 60-second alert confirmation
PREDICT_POINTS_STALE_SECONDS = 3 * PREDICT_META_SECONDS  # other markets' points (read with the fee) are shown this long
PREDICT_STRIKE_SECONDS = 300   # a known target price is re-read this often (the site may correct it)
PREDICT_MISS_SECONDS = 60      # an unknown slug is looked up again after this long (new markets show up within a minute)
PREDICT_DEPTH = 20            # levels kept per side: a fill for the trade size is judged on these (a book "too thin" at
#                               5 levels was often only cut off there), the page and the evidence show the top of them
PREDICT_SHOW_DEPTH = 10       # levels sent to the page per side (it recomputes the edges for the viewer's trade size)
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december")


def predict_slug(stem: str, day: dt.date) -> str:
    """hang-seng-index + 2026-09-28 -> hang-seng-index-up-or-down-on-september-28-2026 (the site's URL slug)."""
    return f"{stem}-up-or-down-on-{MONTHS[day.month - 1]}-{day.day}-{day.year}"


def predict_url(slug: str, ref: str = "") -> str:
    """Market page link, with the referral code when one is configured."""
    return PREDICT_SITE + slug + (f"?ref={urllib.parse.quote(ref)}" if ref else "")


def predict_levels(rows: Any, bids: bool) -> tuple[tuple[D, D], ...]:
    """[[price, size], ...] or [{price, size}, ...] -> sorted (price, size) with empty levels dropped."""
    out = []
    for row in rows if isinstance(rows, list) else []:
        try:
            price, size = (row[0], row[1]) if isinstance(row, (list, tuple)) else (row.get("price"), row.get("size", row.get("quantity")))
            price, size = D(str(price)), D(str(size))
        except (IndexError, TypeError, AttributeError, decimal.InvalidOperation):
            continue
        if price.is_finite() and size.is_finite() and 0 < price < 1 and size > 0:
            out.append((price, size))
    out.sort(key=lambda level: level[0], reverse=bids)
    return tuple(out[:PREDICT_DEPTH])


def predict_markets(data: Any) -> list[dict]:
    """Market records ({id, conditionId, title}) anywhere in a GraphQL / REST answer."""
    found: list[dict] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(node, dict):
            if node.get("id") is not None and ("conditionId" in node or "question" in node) and "title" in node:
                found.append({"id": str(node["id"]), "conditionId": str(node.get("conditionId") or ""),
                              "title": str(node.get("title") or node.get("question") or ""),
                              "question": str(node.get("question") or "")})
                return
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, depth + 1)
    walk(data)
    return list({m["id"]: m for m in found}.values())


@dataclass(frozen=True)
class PredictBook:
    key: str
    slug: str
    market_id: str
    title: str
    bids: tuple[tuple[D, D], ...]
    asks: tuple[tuple[D, D], ...]
    fetched_ms: int
    fee_bps: int | None = None  # the market's own taker fee rate (feeRateBps); None = not stated

    @property
    def bid(self) -> tuple[D, D] | None:
        return self.bids[0] if self.bids else None

    @property
    def ask(self) -> tuple[D, D] | None:
        return self.asks[0] if self.asks else None

    def stale(self, now_ms: int) -> bool:
        return now_ms - self.fetched_ms > PREDICT_STALE_MS


def flip_book(book: "PredictBook") -> "PredictBook":
    """The complementary outcome's book: its bids are 1 − the asks, its asks 1 − the bids."""
    flip = lambda rows: tuple((D(1) - p, q) for p, q in rows)
    return dataclasses.replace(book, bids=flip(book.asks), asks=flip(book.bids))


@dataclass(frozen=True)
class LadderRow:
    """One market of a threshold ladder (e.g. "$200M" of "what market cap will X hit")."""
    target: D
    market_id: str
    title: str
    book: PredictBook | None
    error: str = ""
    question: str = ""  # the market's question (a price ladder's direction may be spelt there)


def cap_target(title: str) -> D | None:
    """'$200M' / '↑ 1B' / 'Will X hit $1.5B?' -> 200000000 / 1000000000 / 1500000000; None when absent."""
    match = re.search(r"(\d+(?:\.\d+)?)\s*([KMB])\b", str(title).replace(",", ""), re.I)
    if not match:
        return None
    return D(match.group(1)) * {"K": D(10) ** 3, "M": D(10) ** 6, "B": D(10) ** 9}[match.group(2).upper()]


@dataclass(frozen=True)
class EdgeCosts:
    """What a Predict trade costs beyond its price. A taker pays fee_bps × min(p, 1 − p) per share (the market's own
    feeRateBps when it states one) and walks the book for ``notional`` USD; a maker pays no fee (its fill is not assured).
    The defaults (no fee, top of book) give the plain price gap."""
    fee_bps: int = 0
    notional: float = 0.0


@dataclass(frozen=True)
class BookEdge:
    """One way to trade the book: side 涨/跌, maker (挂) or taker (吃), the 涨/跌-denominated price and the net edge."""
    side: str
    maker: bool
    price: float       # the best level's price (涨/跌-denominated)
    edge: float        # net, per share (1.0 = $1): model fair price − what a share really costs (fill price + fee)
    size: float        # maker: shares queued at that level; taker: shares the trade size buys within the visible book
    gross: float = 0.0  # fair price − best price, before fee and depth
    fee: float = 0.0    # taker fee per share
    slip: float = 0.0   # taker: average fill price − best price for the trade size
    short: bool = False  # taker: the visible book cannot fill the whole trade size

    @property
    def label(self) -> str:
        return f"{'挂' if self.maker else '吃'}{self.side}"


def taker_fill(levels: tuple[tuple[float, float], ...], notional: float) -> tuple[float, float, bool]:
    """(average price, shares, short?) buying ``notional`` USD across ``levels`` [(price, shares)], best first;
    notional 0 = the best level as it stands."""
    if not levels:
        return 0.0, 0.0, True
    if notional <= 0:
        return levels[0][0], levels[0][1], False
    spent = shares = 0.0
    for price, size in levels:
        take = min(size, (notional - spent) / price)
        spent, shares = spent + take * price, shares + take
        if spent >= notional - 1e-9:
            return spent / shares, shares, False
    return spent / shares, shares, True


def taker_fee(price: float, bps: int) -> float:
    """Predict's taker fee per share: rate × min(p, 1 − p) (largest at 50¢, nothing at the extremes)."""
    return bps / 10_000 * min(price, 1 - price)


def book_edges(fair_up: float, book: PredictBook, costs: EdgeCosts | None = None) -> list[BookEdge]:
    """挂涨 = rest a bid at 买1, 挂跌 = rest a 跌 bid at 1 − 卖1, 吃涨 = buy at 卖1, 吃跌 = buy 跌 at 1 − 买1. Edges are
    net: a taker pays the fee and walks the book for the trade size; a maker pays no fee."""
    costs = costs or EdgeCosts()
    bps = book.fee_bps if book.fee_bps is not None else costs.fee_bps
    fair_down = 1 - fair_up
    edges = []
    if book.bid:
        bid, size = float(book.bid[0]), float(book.bid[1])
        edges.append(BookEdge("涨", True, bid, fair_up - bid, size, fair_up - bid))
        avg, shares, short = taker_fill(tuple((1 - float(p), float(q)) for p, q in book.bids), costs.notional)
        fee = taker_fee(avg, bps)
        edges.append(BookEdge("跌", False, 1 - bid, fair_down - avg - fee, shares, fair_down - (1 - bid), fee, avg - (1 - bid), short))
    if book.ask:
        ask, size = float(book.ask[0]), float(book.ask[1])
        edges.append(BookEdge("跌", True, 1 - ask, fair_down - (1 - ask), size, fair_down - (1 - ask)))
        avg, shares, short = taker_fill(tuple((float(p), float(q)) for p, q in book.asks), costs.notional)
        fee = taker_fee(avg, bps)
        edges.append(BookEdge("涨", False, ask, fair_up - avg - fee, shares, fair_up - ask, fee, avg - ask, short))
    return sorted(edges, key=lambda e: (not e.maker, e.side != "涨"))


def edge_level_norm(text: str) -> str:
    """'$300M' / '300m' / '↑ $120k' / '1B' -> '300m' / '120k' / '1b': how a ladder level is matched in /edge."""
    return re.sub(r"[\s$＄↑↓,，]", "", str(text or "")).lower()


def best_edge(edges: list[BookEdge], need: float = 0.0005) -> BookEdge | None:
    """The direction with the largest net edge above ``need`` (the minimum that covers the model's own error); a maker
    wins ties (it also collects the spread). None when no direction clears it."""
    good = [e for e in edges if e.edge > need]
    return max(good, key=lambda e: (round(e.edge, 4), e.maker)) if good else None


def edge_json(edge: BookEdge, best: BookEdge | None, label: str | None = None) -> dict:
    """One edge chip for the web page: net edge plus its parts, so the tooltip can show where the costs went."""
    return {"label": label or edge.label, "up": edge.side == "涨", "maker": edge.maker, "price": edge.price, "edge": edge.edge,
            "size": edge.size, "gross": edge.gross, "fee": edge.fee, "slip": edge.slip, "short": edge.short, "best": edge is best}


WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def day_fields(target: dt.date, now_ms: int) -> dict:
    """Web card date badge: '09-29 周二' plus 今天 / 明天 / 后天 / 下周一 relative to Beijing today."""
    today = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING).date()
    ahead = (target - today).days
    week = WEEKDAYS[target.weekday()]
    tag = {0: "今天", 1: "明天", 2: "后天"}.get(ahead) or (
        ("下" if target.isocalendar()[1] != today.isocalendar()[1] else "本") + week if 0 < ahead < 14 else "")
    return {"day": target.isoformat(), "day_label": f"{target:%m-%d} {week}", "day_tag": tag, "day_ahead": ahead}


# After the close, how long before the cards move on to the next session. KRX's closing auction ends at a
# random moment up to 30 s after 15:30 KST (랜덤엔드), and feeds such as Naver publish the fixed price a minute
# or so later, so Korea rolls over at 15:33 KST (14:33 Beijing).
CLOSE_SETTLE_MS = {"kr": 3 * 60_000}
KRX_SETTLED = dt.time(15, 30 + CLOSE_SETTLE_MS["kr"] // 60_000)  # KST: the session's figures are final from here
KRX_NXT_AFTER = dt.time(15, 40)  # Nextrade's after-hours session: from here Naver's realtime quote is no longer KRX


# Closing auctions, Beijing time: (start, end, description). The indicative price in the last minutes is
# nearly the close, so up/down is mostly decided once the auction runs.
AUCTIONS = {
    "kr": (dt.time(14, 20), dt.time(14, 30), "韩交所收盘集合竞价（首尔 15:20–15:30，随机结束至 15:30:30）"),
    "hk": (dt.time(16, 0), dt.time(16, 10), "港交所收市竞价（16:00–16:10，16:08 后随机收市）"),
    "sh": (dt.time(14, 57), dt.time(15, 0), "沪深收盘集合竞价（14:57–15:00）"),
}
AUCTIONS["sz"] = AUCTIONS["sh"]
# Pre-open auctions, Beijing time like AUCTIONS: the indicative price (HKEX's 参考平衡价 IEP, the A-share 虚拟撮合价, KRX's
# 예상체결가) forms from the start and is the day's first direction signal; HK matches at a random moment in 09:20–09:22,
# A-shares at 09:25, KRX at 09:00 Seoul. An opening gap is not the day's close.
PRE_AUCTIONS = {
    "hk": (dt.time(9, 0), dt.time(9, 30), "港交所开市前竞价（09:00–09:30，09:20–09:22 随机撮合，09:30 连续交易）"),
    "sh": (dt.time(9, 15), dt.time(9, 30), "沪深开盘集合竞价（09:15–09:25，09:25 撮合开盘价，09:30 连续交易）"),
    "kr": (dt.time(7, 30), dt.time(8, 0), "韩交所开盘同时呼价（首尔 08:30–09:00，09:00 撮合开盘价）"),
}
PRE_AUCTIONS["sz"] = PRE_AUCTIONS["sh"]
# The indicative price keeps moving until the match: worth about this much continuous trading on top of the whole
# session still ahead (a 0.5–1% swing of a stock's indicative price before the match is usual).
PREOPEN_VARIANCE_MINUTES = {"hk": 30.0, "sh": 30.0, "sz": 30.0, "kr": 30.0}


# The auction's phases, Beijing time. While orders can still be withdrawn the indicative price is often a probe: shown,
# not priced on. Once they cannot (HK 09:15, A-shares 09:20) it is priced on, with the match still to move it (new orders
# may be added). After the match (HK 09:22 at the latest, A-shares 09:25) the quote is the opening price itself, firm
# until continuous trading at 09:30. KRX allows withdrawal until its 09:00 match, which opens continuous trading.
PRE_AUCTION_PHASES = {
    "hk": ((dt.time(9, 0), "可撤单"), (dt.time(9, 15), "不可撤单"), (dt.time(9, 20), "随机撮合"), (dt.time(9, 22), "已撮合")),
    "sh": ((dt.time(9, 15), "可撤单"), (dt.time(9, 20), "不可撤单"), (dt.time(9, 25), "已撮合")),
    "kr": ((dt.time(7, 30), "可撤单"),),  # withdrawable to the 09:00 Seoul match, which opens continuous trading; the 08:30–08:40
}                                       # off-hours trades at the previous close show as "no indicative price yet" (price = close)
PRE_AUCTION_PHASES["sz"] = PRE_AUCTION_PHASES["sh"]
PREOPEN_PRICED = {"不可撤单", "随机撮合", "已撮合"}  # the phases whose indicative / matched price the odds rest on


def preopen_phase(market: str, now_ms: int, holidays: frozenset = frozenset()) -> str:
    """Which phase of ``market``'s pre-open auction is on now ("" when none is): the last one begun, Beijing time, an
    hour later on the KRX late day."""
    if not preopen_running(market, now_ms, holidays):
        return ""
    local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
    shift = KR_LATE_SHIFT if CALENDAR.late_day(market, local.date()) else dt.timedelta()
    phase = ""
    for start, name in PRE_AUCTION_PHASES.get(market, ()):
        if (dt.datetime.combine(local.date(), start, BEIJING) + shift).time() <= local.time():
            phase = name
    return phase


def preopen_window(market: str, now_ms: int) -> tuple[dt.time, dt.time, str] | None:
    """(start, end, description) of ``market``'s pre-open auction on the day of ``now_ms``, Beijing time: the usual
    window (a HK half day keeps it), or the KRX late day's (an hour later)."""
    base = PRE_AUCTIONS.get(market)
    info = STOCK_MARKETS.get(market)
    if not base or not info:
        return None
    day = dt.datetime.fromtimestamp(now_ms / 1000, dt.timezone(dt.timedelta(hours=info.utc_offset))).date()
    if CALENDAR.late_day(market, day):
        start, end = (dt.datetime.combine(day, t, BEIJING) + KR_LATE_SHIFT for t in base[:2])
        return start.time(), end.time(), "韩交所开盘同时呼价（高考日延后：首尔 09:30–10:00，10:00 撮合开盘价）"
    return base


def preopen_running(market: str, now_ms: int, holidays: frozenset = frozenset()) -> bool:
    """Whether ``market``'s pre-open auction is under way now (a weekday that is not a configured holiday)."""
    window = preopen_window(market, now_ms)
    if not window:
        return False
    local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
    day = local.date()
    if STOCK_MARKETS[market].utc_offset != 8:
        day = dt.datetime.fromtimestamp(now_ms / 1000, dt.timezone(dt.timedelta(hours=STOCK_MARKETS[market].utc_offset))).date()
    return day.weekday() < 5 and day not in holidays and window[0] <= local.time() < window[1]


def session_minutes(market: str) -> float:
    """Minutes of continuous trading in an ordinary session (the unit of a day's variance)."""
    return sum((dt.datetime.combine(dt.date(2000, 1, 1), b) - dt.datetime.combine(dt.date(2000, 1, 1), a)).seconds
               for a, b in SESSIONS[market]) / 60


def quote_pages(ticker: "StockTicker") -> list[dict]:
    """Where a person can watch the stock's own quote (the pre-open auction's indicative price and its update time
    among them): the venue-side pages that show it, first the one that shows the most."""
    code = ticker.code
    if ticker.market == "hk":
        return [{"name": "富途", "url": f"https://www.futunn.com/stock/{code}-HK"},
                {"name": "AAStocks", "url": f"https://www.aastocks.com/tc/stocks/quote/detail-quote.aspx?symbol={code}"},
                {"name": "腾讯", "url": f"https://gu.qq.com/hk{code}"},
                {"name": "etnet", "url": f"https://www.etnet.com.hk/www/tc/stocks/realtime/quote.php?code={int(code)}"},
                {"name": "港交所", "url": f"https://www.hkex.com.hk/Market-Data/Securities-Prices/Equities/Equities-Quote?sym={int(code)}&sc_lang=zh-HK"}]
    if ticker.market == "kr":
        return [{"name": "Naver", "url": f"https://finance.naver.com/item/main.naver?code={code}"}]
    # the A-share auction's matched price, matched volume and unmatched volume: the free quote pages show them
    return [{"name": "东方财富", "url": f"https://quote.eastmoney.com/{ticker.market}{code}.html"},
            {"name": "同花顺", "url": f"https://stockpage.10jqka.com.cn/{code}/"},
            {"name": "腾讯", "url": f"https://gu.qq.com/{ticker.market}{code}"},
            {"name": "富途", "url": f"https://www.futunn.com/stock/{code}-{ticker.market.upper()}"}]


# How much of a session's variance a running closing auction is still worth, in minutes of continuous trading: the
# indicative price keeps moving until the match (HSI: a 0.1–0.2% move against the 16:00 level is usual, about the
# variance of 5 continuous minutes at σ 1.3% a day), so the odds must not treat the 16:00 print as the close.
AUCTION_VARIANCE_MINUTES = {"hk": 5.0, "kr": 5.0, "sh": 3.0, "sz": 3.0}


def auction_window(market: str, now_ms: int) -> tuple[dt.time, dt.time, str] | None:
    """(start, end, description) of ``market``'s closing auction on the day of ``now_ms``, Beijing time: the usual
    window, or the half day's / late day's one."""
    base = AUCTIONS.get(market)
    info = STOCK_MARKETS.get(market)
    if not base or not info:
        return None
    day = dt.datetime.fromtimestamp(now_ms / 1000, dt.timezone(dt.timedelta(hours=info.utc_offset))).date()
    if CALENDAR.half_day(market, day):
        return dt.time(12, 0), HK_HALF_DAY_CLOSE, "港交所收市竞价（半日市 12:00–12:10，12:08 后随机收市）"
    if CALENDAR.late_day(market, day):
        start, end = (dt.datetime.combine(day, t, BEIJING) + KR_LATE_SHIFT for t in base[:2])
        return start.time(), end.time(), "韩交所收盘集合竞价（高考日延后：首尔 16:20–16:30，随机结束至 16:30:30）"
    return base


def auction_running(market: str, now_ms: int, holidays: frozenset = frozenset()) -> bool:
    """Whether ``market``'s closing auction is under way now (a weekday that is not a configured holiday)."""
    window = auction_window(market, now_ms)
    if not window:
        return False
    local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
    day = local.date()
    if STOCK_MARKETS[market].utc_offset != 8:  # holidays are listed in the venue's own dates
        day = dt.datetime.fromtimestamp(now_ms / 1000, dt.timezone(dt.timedelta(hours=STOCK_MARKETS[market].utc_offset))).date()
    return day.weekday() < 5 and day not in holidays and window[0] <= local.time() < window[1]


def session_state(market: str, now_ms: int, holidays: frozenset = frozenset()) -> str:
    """Where ``market`` stands now (its local time): 未开盘 / 开盘中 / 午休 / 已收盘, or 休市 on a weekend or holiday."""
    info = STOCK_MARKETS.get(market)
    if market not in SESSIONS or not info:
        return ""
    local = dt.datetime.fromtimestamp(now_ms / 1000, dt.timezone(dt.timedelta(hours=info.utc_offset)))
    if local.weekday() >= 5 or local.date() in holidays:
        return "休市"
    sessions = CALENDAR.sessions(market, local.date())
    t = local.time()
    if t < sessions[0][0]:
        return "未开盘"
    if any(start <= t < end for start, end in sessions):
        return "开盘中"
    if len(sessions) > 1 and sessions[0][1] <= t < sessions[1][0]:
        return "午休"
    return "已收盘"


def ref_relative(ref_day: str, target: dt.date, now_ms: int) -> str:
    """'09-28' -> 昨收 (a close before today, Beijing) / 今收 (today's own close, after the session) / 参考."""
    if not ref_day:
        return "参考"
    today = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING).date()
    with contextlib.suppress(ValueError):
        month, day = map(int, ref_day.split("-"))
        year = target.year if (month, day) <= (target.month, target.day) else target.year - 1
        return "今收" if dt.date(year, month, day) == today else "昨收"
    return "参考"


def cents(value: float, sign: bool = False) -> str:
    return f"{value * 100:+.1f}¢" if sign else f"{value * 100:.1f}¢"


def book_lines(book: PredictBook | None, error: str, odds: "CloseOdds | str | None", now_ms: int,
               url: str = "", costs: EdgeCosts | None = None, min_edge: float = 0.0005) -> list[str]:
    """Lines for Telegram (bold sentinels, send as HTML): quote, the four net edges, the best maker and the best taker
    trade (each judged on its own costs) when they clear the threshold, the market link."""
    if book is None:
        return [f"📕 Predict 盘口暂缺：{error or '等待首次获取'}"]
    bid = f"{cents(float(book.bid[0]))}×{fmt(book.bid[1])}" if book.bid else "无"
    ask = f"{cents(float(book.ask[0]))}×{fmt(book.ask[1])}" if book.ask else "无"
    age = (now_ms - book.fetched_ms) // 1000
    head = f"📕 Predict 涨 买1 {bid}｜卖1 {ask}"
    if book.bid and book.ask:
        head += f"｜中间 {cents(float(book.bid[0] + book.ask[0]) / 2)}"
    lines = [head + (f"｜⚠️ {age} 秒前" if book.stale(now_ms) else "")]
    if error:
        lines.append(f"⚠️ 最近刷新失败：{brief_error(error, 60)}（显示上次盘口）")
    if not isinstance(odds, CloseOdds):
        lines.append("模型概率暂缺，无法比较优势")
        return lines
    edges = book_edges(odds.fair_up, book, costs)
    if not edges:
        lines.append("盘口为空，无法比较")
        return lines
    swing = model_swing(odds)
    need = max(min_edge, swing)
    lines.append(f"模型 涨 {cents(odds.fair_up)}｜跌 {cents(odds.fair_down)}｜净优势门槛 {cents(need)}（模型误差 {cents(swing)}）")
    notional = costs.notional if costs else 0.0
    for maker in (True, False):
        row = [f"{e.label} {cents(e.price)} 净 {bold(cents(e.edge, True)) if e.edge > need else cents(e.edge, True)}"
               for e in edges if e.maker == maker]
        if row:
            note = "（免手续费）" if maker else (f"（按 ${notional:g} 吃单：含深度与手续费）" if notional else "（含手续费）")
            lines.append("｜".join(row) + note)
    if book.stale(now_ms):
        lines.append("盘口过期，不给建议")
    elif odds.warn:
        lines.append(f"⚠️ {odds.warn}，暂不给建议")
    else:
        maker = best_edge([e for e in edges if e.maker], need)
        taker = best_edge([e for e in edges if not e.maker], need)
        if maker:
            lines.append(f"👉 挂单 {bold(maker.label)} @ {cents(maker.price)} 净优势 {cents(maker.edge, True)}（排队，成交不保证）")
        if taker:
            fill = f"均价 {cents(taker.price + taker.slip)}·手续费 {cents(taker.fee)}"
            size = (f"盘口只够 {taker.size:,.0f} 份" if taker.short else f"${notional:g} 约 {taker.size:,.0f} 份") if notional \
                else f"卖1/买1 只有 {taker.size:g} 份"
            lines.append(f"👉 吃单 {bold(taker.label)} @ {cents(taker.price)} 净优势 {cents(taker.edge, True)}（立即成交，{fill}，{size}）")
        if not maker and not taker:
            lines.append(f"👉 扣除手续费、深度和模型误差后，没有方向超过门槛 {cents(need)}，暂不挂")
    if url:
        lines.append(url)
    return lines


class PredictFeed:
    """Resolves each day's Predict.fun slug to its market and polls the orderbook (GraphQL + REST, read-only)."""

    def __init__(self, config: Config):
        self.config = config
        self.markets: dict[str, tuple[dict | None, float]] = {}  # slug -> (market or None = not listed, when)
        self.book_keys: dict[str, str] = {}                      # market id -> id/conditionId that answered
        self.books: dict[str, PredictBook] = {}                  # item key -> latest book
        self.errors: dict[str, str] = {}
        self.slugs: dict[str, str] = {}                          # item key -> slug currently followed
        self.types: dict[str, str] = {}                          # slug -> GraphQL category type
        self.strikes: dict[str, tuple[D | None, float]] = {}     # slug -> (target price shown on the site, when)
        self.no_strike: set[str] = set()                         # category types without marketData.startPrice
        self.info: dict[str, dict] = {}                          # slug -> {"outcomes": [...], "created_ms": int}
        self.want_info: set[str] = set()                         # slugs whose outcome names / creation time matter
        self.market_lists: dict[str, tuple[list[dict] | None, float]] = {}  # slug -> every market of the category
        self.market_meta: dict[str, tuple[dict, float]] = {}     # market id -> ({"outcomes": [...], "status": str}, when)
        self.meta_errors: dict[str, str] = {}                  # latest detail refresh failure (old rewards are not trusted)
        self.reward_keys: set[str] = set()                      # price ladders: refresh reward metadata more often
        self.ladder_keys: set[str] = set()                       # item keys whose category holds several Yes/No markets
        self.ladder_parse: dict[str, Any] = {}                   # item key -> its markets' level parser (default cap_target)
        self.ladder_pick: dict[str, Any] = {}                    # item key -> which of the category's markets its ladder keeps
        self.ladders: dict[str, list[LadderRow]] = {}            # item key -> one row per market, by threshold
        self.fees: dict[str, tuple[int | None, float]] = {}      # market id -> (feeRateBps or None, when read)
        self.refreshed = -1e9
        self.gate = asyncio.Semaphore(PREDICT_PARALLEL)          # requests in flight: the other feeds share the thread pool
        self.blocked_until = 0.0                                 # monotonic: after a 429, no request before this

    def headers(self) -> dict[str, str]:
        return {"x-api-key": self.config.predict_api_key} if self.config.predict_api_key else {}

    async def fetch(self, url: str, payload: dict | None = None) -> Any:
        """One Predict request, at most PREDICT_PARALLEL at a time; a 429 (or any retry_after) pauses them all."""
        left = self.blocked_until - time.monotonic()
        if left > 0:
            raise RemoteError(f"Predict 接口限流冷却中（{int(left) + 1} 秒后重试）", int(left) + 1)
        async with self.gate:
            try:
                raw = await _blocking(_http_get, url, payload, SOURCE_TIMEOUT, self.headers() if url.startswith(PREDICT_REST) else {})
            except RemoteError as error:
                if error.retry_after or "429" in str(error):
                    self.blocked_until = max(self.blocked_until, time.monotonic() + max(error.retry_after, PREDICT_BACKOFF_SECONDS))
                raise
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise RemoteError("Predict 接口未返回有效 JSON") from None

    async def graphql(self, query: str, variables: dict) -> Any:
        data = await self.fetch(PREDICT_GRAPHQL, {"query": query, "variables": variables})
        if isinstance(data, dict) and data.get("errors"):
            raise RemoteError("Predict GraphQL 错误：" + clean_error(json.dumps(data["errors"], ensure_ascii=False))[:120])
        return data.get("data") if isinstance(data, dict) else None

    async def resolve(self, slug: str) -> dict | None:
        """slug -> its (first) market; see find()."""
        cached = self.markets.get(slug)
        if cached and (cached[0] is not None or time.monotonic() - cached[1] < PREDICT_MISS_SECONDS):
            return cached[0]
        found = await self.find(slug)
        market = found[0] if found else None
        self.markets[slug] = (market, time.monotonic())
        return market

    async def resolve_all(self, slug: str) -> list[dict] | None:
        """slug -> every market of the category (a ladder such as $200M / $300M / ...); None = not listed."""
        cached = self.market_lists.get(slug)
        if cached and (cached[0] is not None or time.monotonic() - cached[1] < PREDICT_MISS_SECONDS):
            return cached[0]
        found = await self.find(slug) or None
        self.market_lists[slug] = (found, time.monotonic())
        return found

    async def find(self, slug: str) -> list[dict]:
        """slug -> markets via GraphQL category(id: slug) → markets(categoryId); REST /categories/<slug> as fallback.
        [] = not listed; network trouble raises (and is not cached as "not listed")."""
        found: list[dict] = []
        errors = []  # only failures that leave the answer unknown; "no such category" is an answer
        try:
            data = await self.graphql("query($id: ID!) { category(id: $id) { id __typename } }", {"id": slug})
            category = (data or {}).get("category") or {}
            if re.fullmatch(r"[A-Za-z_]\w*", str(category.get("__typename") or "")):
                self.types[slug] = category["__typename"]
            if category.get("id") is not None:
                data = await self.graphql(
                    "query($f: MarketFilterInput!) { markets(filter: $f, pagination: { first: 20 }) "
                    "{ edges { node { id conditionId title question } } } }", {"f": {"categoryId": str(category["id"])}})
                found = predict_markets(data)
        except (RemoteError, TimeoutError, OSError) as error:
            errors.append(clean_error(error))
        if not found:
            try:
                found = predict_markets(await self.fetch(f"{PREDICT_REST}/categories/{urllib.parse.quote(slug)}"))
            except (RemoteError, TimeoutError, OSError) as error:
                if "HTTP 404" not in str(error) and errors:
                    errors.append(clean_error(error))
                else:
                    errors.clear()  # GraphQL answered "no such category", or REST did (404)
        if not found and errors:
            raise RemoteError("；".join(errors))  # network trouble: not cached as "not listed"
        return found

    async def orderbook(self, market: dict) -> tuple[tuple, tuple, str]:
        keys = [k for k in dict.fromkeys([self.book_keys.get(market["id"]), market["id"], market.get("conditionId")]) if k]
        last = ""
        for key in keys:
            try:
                data = await self.fetch(f"{PREDICT_REST}/markets/{urllib.parse.quote(key)}/orderbook")
            except RemoteError as error:
                last = clean_error(error)
                if "429" in last or "401" in last or "403" in last:
                    break  # same answer for every key; do not burn more requests
                continue
            book = data.get("data", data) if isinstance(data, dict) else {}
            if not isinstance(book, dict) or ("bids" not in book and "asks" not in book):
                last = "订单簿格式异常"
                continue
            self.book_keys[market["id"]] = key
            return predict_levels(book.get("bids"), True), predict_levels(book.get("asks"), False), key
        raise RemoteError(last or "订单簿获取失败")

    async def strike(self, slug: str, market_id: str) -> D | None:
        """The market's target price (目标价 / price to beat: the close it settles against), from GraphQL
        category.marketData.startPrice. Best effort: None when the category type has no such field."""
        cached = self.strikes.get(slug)
        if cached and time.monotonic() - cached[1] < (PREDICT_STRIKE_SECONDS if cached[0] is not None else PREDICT_MISS_SECONDS):
            return cached[0]
        kind = self.types.get(slug, "")
        value = cached[0] if cached else None
        if kind and kind not in self.no_strike:
            try:
                data = await self.graphql(f"query($id: ID!) {{ category(id: $id) {{ ... on {kind} "
                                          "{ marketData { marketId startPrice } } } }", {"id": slug})
                rows = ((data or {}).get("category") or {}).get("marketData") or []
                rows = [r for r in rows if isinstance(r, dict) and r.get("startPrice") not in (None, "", 0)]
                row = next((r for r in rows if str(r.get("marketId")) == market_id), rows[0] if len(rows) == 1 else None)
                if row:
                    value = D(str(row["startPrice"]))
                    value = value if value > 0 else None
            except RemoteError as error:
                if "Cannot query field" in str(error) or "Unknown type" in str(error):
                    self.no_strike.add(kind)  # schema answer: never ask this type again
            except (TimeoutError, OSError, ValueError, TypeError, decimal.InvalidOperation):
                pass
        self.strikes[slug] = (value, time.monotonic())
        return value

    async def market_details(self, market_id: str) -> dict:
        """Outcome names in index order (the orderbook prices the first one), status and creation time, via REST."""
        data = await self.fetch(f"{PREDICT_REST}/markets/{urllib.parse.quote(market_id)}")
        market = data.get("data", data) if isinstance(data, dict) else {}
        outcomes = market.get("outcomes") if isinstance(market, dict) else None
        if not isinstance(outcomes, list) or not outcomes:
            raise RemoteError("Predict 市场详情缺少 outcomes")
        rows = sorted((o for o in outcomes if isinstance(o, dict)), key=lambda o: int(o.get("indexSet") or 0))
        created = 0
        with contextlib.suppress(ValueError, TypeError):
            created = int(dt.datetime.fromisoformat(str(market.get("createdAt")).replace("Z", "+00:00")).timestamp() * 1000)
        fee = None
        with contextlib.suppress(ValueError, TypeError):  # absent (None) or not a number: not stated
            stated = int(market.get("feeRateBps"))
            fee = stated if 0 <= stated <= 10_000 else None
        return {"outcomes": [str(o.get("name") or "") for o in rows], "created_ms": created,
                "status": str(market.get("status") or ""), "fee_bps": fee, "resolved": predict_resolution(market),
                "rules": str(market.get("description") or market.get("rules") or "")[:3000],
                "question": str(market.get("question") or ""),
                "trading_status": str(market.get("tradingStatus") or ""), "rewards": market.get("rewards"),
                "spread_threshold": market.get("spreadThreshold"), "share_threshold": market.get("shareThreshold")}

    async def market_fee(self, market_id: str) -> int | None:
        """The market's own taker fee rate (feeRateBps), re-read every 10 minutes; None when it states none (or the
        details cannot be read: the configured default applies)."""
        cached = self.fees.get(market_id)
        if cached and time.monotonic() - cached[1] < PREDICT_META_SECONDS:
            return cached[0]
        fee = cached[0] if cached else None
        with contextlib.suppress(Exception):  # best effort: never blocks the orderbook
            details = await self.market_details(market_id)
            self.market_meta[market_id] = (details, time.monotonic())  # its points (rewards) are read from here too
            fee = details["fee_bps"]
        self.fees[market_id] = (fee, time.monotonic())
        return fee

    async def market_info(self, slug: str, market_id: str) -> None:
        details = await self.market_details(market_id)
        self.info[slug] = {"outcomes": details["outcomes"], "created_ms": details["created_ms"]}

    CLOSED_WORDS = re.compile(r"\b(RESOLV\w*|SETTL\w*|FINAL\w*|CLOSED|ENDED|EXPIR\w*|CANCEL\w*)\b")

    @classmethod
    def market_closed(cls, meta: dict | None) -> bool:
        """The market no longer trades, by its details: resolved, settled, closed, ended, expired or cancelled."""
        meta = meta or {}
        return bool(cls.CLOSED_WORDS.search(f"{meta.get('status', '')} {meta.get('trading_status', '')}".upper()))

    @staticmethod
    def market_settled(meta: dict | None) -> bool:
        """Predict has settled the market (what the cards call 已结算)."""
        return bool(re.search(r"RESOLV|SETTL|FINAL", str((meta or {}).get("status", "")).upper()))

    def book_absent(self, row: "LadderRow") -> bool:
        """The level's error is an answer rather than a failure: the market has no book (HTTP 404), or it no longer
        trades by its details and the API refuses its book (HTTP 400 / 410)."""
        if "HTTP 404" in row.error:
            return True
        meta = (self.market_meta.get(row.market_id) or ({}, 0))[0]
        return self.market_closed(meta) and bool(re.search(r"HTTP 4(00|10)\b", row.error))

    async def ladder_row(self, key: str, slug: str, market: dict) -> "LadderRow | None":
        parse = self.ladder_parse.get(key, cap_target)
        target = parse(market.get("title", "")) or parse(market.get("question", ""))
        if target is None:
            return None
        meta = self.market_meta.get(market["id"])
        refresh_seconds = PREDICT_REWARD_SECONDS if key in self.reward_keys else PREDICT_META_SECONDS
        if meta is None or time.monotonic() - meta[1] >= refresh_seconds:
            try:
                self.market_meta[market["id"]] = (await self.market_details(market["id"]), time.monotonic())
                self.meta_errors.pop(market["id"], None)
            except (RemoteError, TimeoutError, OSError) as error:
                self.meta_errors[market["id"]] = clean_error(error) or type(error).__name__
        if self.market_settled((self.market_meta.get(market["id"]) or ({}, 0))[0]):
            # Predict has settled this level: it has no live book any more (asking gets HTTP 400) and the card needs none
            return LadderRow(target, market["id"], market["title"], None, "", market.get("question", ""))
        try:
            bids, asks, _ = await self.orderbook(market)
        except (RemoteError, TimeoutError, OSError) as error:
            # one timed-out request must not blank the level (its edge, its alerts, its paper trades) for a whole round:
            # the book it had last time stays, aging as it does (stale after PREDICT_STALE_MS), with the failure noted
            kept = next((row.book for row in self.ladders.get(key) or () if row.market_id == market["id"] and row.book), None)
            return LadderRow(target, market["id"], market["title"], kept, clean_error(error) or type(error).__name__,
                             market.get("question", ""))
        fee = (self.market_meta.get(market["id"]) or ({}, 0))[0].get("fee_bps")
        book = PredictBook(key, slug, market["id"], market["title"], bids, asks, int(time.time() * 1000), fee)
        return LadderRow(target, market["id"], market["title"], book, "", market.get("question", ""))

    async def refresh_ladder(self, key: str, slug: str) -> None:
        """A category of Yes/No markets, one per threshold: every market's book, sorted by threshold. A level whose book
        could not be read keeps its previous one; the failures are recorded for the card and /status."""
        markets = await self.resolve_all(slug)
        if not markets:
            self.ladders.pop(key, None)
            self.errors[key] = f"Predict 上还没有这个市场（{slug}）"
            return
        rows = [row for row in await asyncio.gather(*(self.ladder_row(key, slug, m) for m in markets)) if row]
        if not rows:
            raise RemoteError("Predict 市场标题里没有可识别的档位")
        if key in self.ladder_pick:
            rows = self.ladder_pick[key](rows, self.market_meta)
        self.ladders[key] = sorted(rows, key=lambda row: row.target)
        failed = [row for row in rows if row.error and not self.book_absent(row)]  # no book at all is an answer, not a failure
        if failed:
            which = "、".join(brief_error(row.title or str(row.target), 14) for row in failed[:3]) + ("…" if len(failed) > 3 else "")
            self.errors[key] = f"{len(failed)}/{len(rows)} 档盘口刷新失败（{which}：{brief_error(failed[0].error, 60)}）；显示上次盘口"
        else:
            self.errors.pop(key, None)

    def yes_book(self, row: "LadderRow") -> tuple["PredictBook | None", str]:
        """The row's book priced as "Yes", whatever the market's outcome order."""
        if row.book is None:
            return None, row.error
        names = ((self.market_meta.get(row.market_id) or ({}, 0))[0]).get("outcomes") or []
        first = names[0].strip().lower() if names else ""
        if first == "yes":
            return row.book, ""
        if first == "no":
            return flip_book(row.book), ""
        return None, f"盘口方向未确认（结果名称：{'、'.join(names) or '未取得'}）"

    async def refresh_one(self, key: str, slug: str) -> None:
        if key in self.ladder_keys:
            try:
                await self.refresh_ladder(key, slug)
            except (RemoteError, TimeoutError, OSError) as error:
                self.errors[key] = clean_error(error) or type(error).__name__
            return
        try:
            market = await self.resolve(slug)
            if market is None:
                self.books.pop(key, None)
                self.errors[key] = f"Predict 上还没有这个市场（{slug}）"
                return
            await self.strike(slug, market["id"])
            if slug in self.want_info and slug not in self.info:
                await self.market_info(slug, market["id"])
            fee = await self.market_fee(market["id"])
            bids, asks, _ = await self.orderbook(market)
            self.books[key] = PredictBook(key, slug, market["id"], market["title"], bids, asks, int(time.time() * 1000), fee)
            self.errors.pop(key, None)
        except (RemoteError, TimeoutError, OSError) as error:
            self.errors[key] = clean_error(error) or type(error).__name__

    async def refresh(self, targets: dict[str, str], force: bool = False) -> Refreshed | bool:
        """targets: item key -> slug. False = not due yet."""
        if not force and time.monotonic() - self.refreshed < self.config.predict_poll:
            return False
        if (left := self.blocked_until - time.monotonic()) > 0:
            return Refreshed("failed", f"Predict 接口限流，冷却 {int(left) + 1} 秒后再试（盘口暂按上次结果显示）")
        self.refreshed = time.monotonic()
        for key in list(self.books):
            if targets.get(key) != self.books[key].slug:
                self.books.pop(key)  # the target day moved on: never show yesterday's book for today's market
        for key in list(self.errors):
            if key not in targets:
                self.errors.pop(key)
        for key in list(self.ladders):
            if key not in targets:
                self.ladders.pop(key)
        self.slugs = dict(targets)
        keep = set(targets.values())  # yesterday's daily slugs are not kept around for ever
        for table in (self.markets, self.market_lists, self.strikes, self.types, self.info):
            for slug in [s for s in table if s not in keep]:
                table.pop(slug, None)
        await asyncio.gather(*(self.refresh_one(key, slug) for key, slug in targets.items()))
        # a market not listed yet is an answer, not a failure
        failed = [f"{key}：{error}" for key, error in self.errors.items() if key in targets and "还没有这个市场" not in error]
        return refreshed(failed, len(targets) - len(failed))


# --- first-touch market (which barrier does BNB hit first?) ------------------------------------------
def et_ms(year: int, month: int, day: int, hour: int, minute: int, utc_offset: int) -> int:
    """US Eastern wall time -> epoch ms; utc_offset is -4 (EDT, Mar-Nov) or -5 (EST)."""
    return int(dt.datetime(year, month, day, hour, minute, tzinfo=dt.timezone(dt.timedelta(hours=utc_offset))).timestamp() * 1000)


TOUCH_DEADLINE_MS = et_ms(2026, 12, 31, 23, 59, -5)


@dataclass(frozen=True)
class TouchSpec:
    """One "which price is hit first" market: Binance spot pair, the two barriers, and its Predict slug."""
    key: str          # item / book key, e.g. BNB
    slug: str
    symbol: str       # Binance spot pair (the resolution source)
    low: D
    high: D
    created_ms: int = 0  # window start from the rules (the creation time unless the rules name a date)
    deadline_ms: int = TOUCH_DEADLINE_MS
    et_offset: int = -5  # US Eastern offset at the deadline, for its label
    fixed_start: bool = False  # the rules name the window start: it wins over Predict's creation time

    def label(self, value: D) -> str:
        """$70,000 -> 70k, $3,000 -> 3k, $900 -> 900: short enough for the edge chips."""
        return f"{int(value) // 1000}k" if value >= 1000 and value % 1000 == 0 else fmt(value)

    def close_label(self) -> str:
        et = dt.datetime.fromtimestamp(self.deadline_ms / 1000, dt.timezone(dt.timedelta(hours=self.et_offset)))
        bj = dt.datetime.fromtimestamp(self.deadline_ms / 1000, BEIJING)
        return f"{et:%m-%d %H:%M} ET（北京 {bj:%m-%d %H:%M}）截止；都没碰到按 50/50 结算"


TOUCH_MARKETS = (
    TouchSpec("BNB", "will-bnb-hit-700-or-900", "BNBUSDT", D("700"), D("900")),
    TouchSpec("SOL", "will-solana-hit-60-or-140-first", "SOLUSDT", D("60"), D("140"),
              int(dt.datetime(2026, 3, 12, 13, 27, 8, 415000, tzinfo=dt.timezone.utc).timestamp() * 1000)),
    # "between August 25th, 2026 at 10:00 AM ET and October 25th, 2026 at 11:59 PM ET" (both EDT)
    TouchSpec("BTC", "will-btc-hit-70000-or-90000-first", "BTCUSDT", D("70000"), D("90000"),
              et_ms(2026, 8, 25, 10, 0, -4), et_ms(2026, 10, 25, 23, 59, -4), -4, fixed_start=True),
    # window opens at the market's creation (from Predict; the rules give no date)
    TouchSpec("ETH", "will-ethereum-hit-1k-or-3k-first", "ETHUSDT", D("1000"), D("3000")),
)
TOUCH_SPOT = ("https://data-api.binance.vision", "https://api.binance.com", "https://api1.binance.com")
YEAR_MS = 365 * 24 * 3600 * 1000


SPOT_BACKOFF = {"until": 0.0}  # monotonic: after a 418 / 429 from Binance spot no spot request goes out before this


async def binance_spot(path: str, **params: Any) -> Any:
    """GET /api/v3/<path> from Binance spot (the crypto markets' resolution source): the public hosts in turn, those in
    cooldown last. A rate-limit answer (retry_after) pauses every spot request for that long instead of trying the next
    host at once: the api*.binance.com hosts share one IP-based limit, and a 429 ignored escalates to a 418 ban."""
    left = SPOT_BACKOFF["until"] - time.monotonic()
    if left > 0:
        raise RemoteError(f"币安现货接口限流冷却中（{int(left) + 1} 秒后重试）", int(left) + 1)
    query = urllib.parse.urlencode(params)
    failures, retry = [], 0
    for _, base in SOURCE_HEALTH.order([(host, host) for host in TOUCH_SPOT]):
        try:
            return json.loads(await fetch_source(f"{base}/api/v3/{path}?{query}"))
        except RemoteError as error:
            failures.append(f"{urllib.parse.urlsplit(base).hostname}: {clean_error(error)}")
            if error.retry_after:
                retry = error.retry_after
                SPOT_BACKOFF["until"] = max(SPOT_BACKOFF["until"], time.monotonic() + retry)
                break
        except Exception as error:
            failures.append(f"{urllib.parse.urlsplit(base).hostname}: {clean_error(error)}")
    raise RemoteError("；".join(failures), retry)


def _log_erfc_pos(z: float) -> float:
    """log(erfc(z)) for z ≥ 0 without underflow (Numerical Recipes' erfcc, |rel err| < 1.2e-7)."""
    if z == 0:
        return 0.0
    t = 1 / (1 + 0.5 * z)
    return (math.log(t) - z * z - 1.26551223 + t * (1.00002368 + t * (0.37409196 + t * (0.09678418 + t * (
        -0.18628806 + t * (0.27886807 + t * (-1.13520398 + t * (1.48851587 + t * (-0.82215223 + t * 0.17087277)))))))))


def _log_phi(z: float) -> float:
    if z == 0:
        return -math.log(2)
    q = -math.log(2) + _log_erfc_pos(abs(z) / math.sqrt(2))
    return q if z < 0 else math.log1p(-math.exp(q))


def _log_add(a: float, b: float) -> float:
    m = max(a, b)
    return m if m == -math.inf else m + math.log(math.exp(a - m) + math.exp(b - m))


def _infinite_upper(x: float, a: float, sigma: float, mu: float) -> float:
    """P(hit the upper barrier first) with no deadline; x = ln(S/L), a = ln(U/L)."""
    r = 2 * (mu - 0.5 * sigma * sigma) / (sigma * sigma)
    if abs(r * a) < 1e-7:
        return x / a + r * x * (a - x) / (2 * a)
    if r > 0:
        return math.expm1(-r * x) / math.expm1(-r * a)
    return math.exp(r * (a - x)) * (-math.expm1(r * x)) / (-math.expm1(r * a))


@dataclass(frozen=True)
class TouchOdds:
    lower: float   # P(the low barrier is hit first, before the deadline)
    upper: float
    none: float    # neither by the deadline (settles 50/50)

    @property
    def fair_lower(self) -> float:
        return self.lower + self.none / 2

    @property
    def fair_upper(self) -> float:
        return self.upper + self.none / 2


def first_touch(spot: float, low: float, high: float, sigma: float, mu: float, years: float) -> TouchOdds:
    """Double-barrier first passage of a geometric Brownian motion (constant σ, drift μ) before a deadline.

    Short horizons use the method of images; long ones the eigenfunction series. Neither side hit by
    the deadline settles 50/50, which is what fair_lower / fair_upper price in."""
    if spot <= low:
        return TouchOdds(1.0, 0.0, 0.0)
    if spot >= high:
        return TouchOdds(0.0, 1.0, 0.0)
    if years <= 0:
        return TouchOdds(0.0, 0.0, 1.0)
    if not sigma > 0:
        raise ValueError("波动率无效")
    v = sigma * sigma
    b = mu - v / 2
    k = b / v
    a = math.log(high / low)
    x = math.log(spot / low)
    tau = v * years / (a * a)
    if tau < 0.08 or abs(k * a) > 12:
        st, ab = sigma * math.sqrt(years), abs(b)

        def image(d: float, pref: float) -> float:
            z = abs(d)
            if z == 0:
                return 0.0
            log_j = _log_add(-ab * z / v + _log_phi((ab * years - z) / st), ab * z / v + _log_phi((-ab * years - z) / st))
            return math.copysign(math.exp(pref + log_j), d)

        lower, upper = image(x, -k * x), image(a - x, k * (a - x))
        for n in range(1, 10001):
            terms = (image(x + 2 * n * a, -k * x), image(x - 2 * n * a, -k * x),
                     image(a - x + 2 * n * a, k * (a - x)), image(a - x - 2 * n * a, k * (a - x)))
            lower += terms[0] + terms[1]
            upper += terms[2] + terms[3]
            if max(map(abs, terms)) < 1e-15:
                break
    else:
        p_inf = _infinite_upper(x, a, sigma, mu)
        coeff = v * math.pi / (a * a)
        count = max(5, math.ceil(math.sqrt(2 * (45 + abs(k * a)) / (math.pi * math.pi * tau))) + 3)
        tail_l = tail_u = 0.0
        for n in range(1, count + 1):
            lam = (v * (n * math.pi / a) ** 2 + b * b / v) / 2
            term = n * math.sin(n * math.pi * x / a) * math.exp(-lam * years) / lam
            tail_l += term
            tail_u += term if n % 2 else -term
        lower = 1 - p_inf - coeff * math.exp(-k * x) * tail_l
        upper = p_inf - coeff * math.exp(k * (a - x)) * tail_u
    lower, upper = min(1.0, max(0.0, lower)), min(1.0, max(0.0, upper))
    if lower + upper > 1:
        total = lower + upper
        lower, upper = lower / total, upper / total
    return TouchOdds(lower, upper, max(0.0, 1 - lower - upper))


def realized_vol(rows: list, now_ms: int) -> float:
    """Annualised σ from the last 720 finished hourly closes (30 days)."""
    done = [r for r in rows if isinstance(r, list) and len(r) > 6 and int(r[6]) < now_ms][-721:]
    if len(done) < 721:
        raise ValueError("30 日小时 K 线不足")
    rets = [math.log(float(done[i][4]) / float(done[i - 1][4])) for i in range(1, len(done))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var * 365 * 24)


def touch_outcome(name: str, spec: TouchSpec) -> str:
    """'$700' / 'Yes, $700' / '900 first' -> 'low' | 'high' | '' (whole numbers only: 60 is not in 160)."""
    name = name.replace(",", "")
    has = lambda value: any(re.search(rf"(?<![\d.]){n}(?![\d])", name, re.I) for n in
                            (str(int(value)), *([f"{int(value) // 1000}k"] if value % 1000 == 0 else [])))
    has_low, has_high = has(spec.low), has(spec.high)
    return "low" if has_low and not has_high else "high" if has_high and not has_low else ""


class TouchMarket:
    """A Binance spot pair vs its two barriers: live price, 30-day σ, and the path since the market opened.

    The path check reads hourly bars and drills into 1-minute bars only for an hour that reached a barrier,
    so a hit is dated to the minute; a minute that spans both barriers, or the market's opening minute,
    cannot be ordered from OHLC and is reported for a manual check."""
    PRICE_SECONDS = 30
    VOL_SECONDS = 3600
    SCAN_SECONDS = 300
    PRICE_STALE_MS = 5 * 60_000     # a spot price older than this prices nothing
    SIGMA_STALE_MS = 24 * 3600_000  # σ not re-measured for a day: shown, not advised on
    SCAN_STALE_MS = 3 * 3600_000    # the path check must reach this close to now before advice is given

    def __init__(self, store: "Store", spec: TouchSpec):
        self.store, self.spec = store, spec
        self.price: D | None = None
        self.priced_ms = 0
        self.sigma: float | None = None
        self.sigma_ms = 0
        self.error = ""
        self.times = {"price": -1e9, "vol": -1e9, "scan": -1e9}
        self.start_ms = spec.created_ms  # market creation (Predict, else the rules), 0 = unknown

    async def get(self, path: str, **params: Any) -> Any:
        return await binance_spot(path, **params)

    @property
    def history(self) -> dict:
        """{'kind': 'clear'|'low'|'high'|'ambiguous', 'through'/'time': ms, 'start': ms} or {}."""
        saved = self.store.get(f"touch:{self.spec.slug}", {})
        return saved if isinstance(saved, dict) and saved.get("start") == self.start_ms else {}

    async def refresh(self, now_ms: int) -> None:
        mono = time.monotonic()
        try:
            if mono - self.times["price"] >= self.PRICE_SECONDS:
                self.times["price"] = mono
                data = await self.get("ticker/price", symbol=self.spec.symbol)
                self.price, self.priced_ms = number(data["price"], "BNB"), now_ms
                if (self.price <= self.spec.low or self.price >= self.spec.high) and self.history.get("kind") in {None, "clear"}:
                    self.times["scan"] = -1e9  # the price is at a line right now: check the path at once, not in 5 minutes
            if mono - self.times["vol"] >= self.VOL_SECONDS or self.sigma is None:
                self.times["vol"] = mono
                # 722: the newest bar is the hour still running, which realized_vol drops; 721 finished bars remain
                self.sigma = realized_vol(await self.get("klines", symbol=self.spec.symbol, interval="1h", limit=722), now_ms)
                self.sigma_ms = now_ms
            if self.start_ms and mono - self.times["scan"] >= self.SCAN_SECONDS:
                self.times["scan"] = mono
                await self.scan(now_ms)
            self.error = ""
        except Exception as error:
            self.error = clean_error(error) or type(error).__name__

    @property
    def window_end(self) -> int:
        """The end of the deadline's own minute: the window includes that minute."""
        return self.spec.deadline_ms + 60_000

    async def scan(self, now_ms: int) -> None:
        """Extend the barrier check from where it stopped (persisted) up to now. Whole hours are read as hourly bars;
        once the deadline has passed, the hour it falls in is read minute by minute through the deadline's own minute,
        so a "clear" record reaching window_end covers the whole window (never the last hours left unread)."""
        hist = self.history
        if hist.get("kind") in {"low", "high", "ambiguous"}:
            return
        end = min(now_ms, self.spec.deadline_ms)
        if end <= self.start_ms:
            return  # the window has not opened yet: nothing to check
        cursor = int(hist.get("through") or self.start_ms)
        low, high = float(self.spec.low), float(self.spec.high)
        for _ in range(50):  # 50 000 hours: far more than the market can span
            rows = await self.get("klines", symbol=self.spec.symbol, interval="1h", startTime=cursor - cursor % 3_600_000,
                                  endTime=end, limit=1000)
            done = [r for r in rows if isinstance(r, list) and len(r) > 6 and int(r[6]) < end]
            for row in done:
                if int(row[6]) < cursor or not (float(row[3]) <= low or float(row[2]) >= high):
                    continue
                result = await self.scan_hour(int(row[0]), cursor, end)
                if result:
                    self.store.put(f"touch:{self.spec.slug}", {**result, "start": self.start_ms})
                    return
            if not done:
                break
            cursor = int(done[-1][6]) + 1
            if len(rows) < 1000:
                break
        if end == now_ms and cursor <= now_ms:
            # The hour still running: its closed minutes are checked as well, so a spike is seen within SCAN_SECONDS of
            # its minute rather than up to an hour later when the bar closes (Predict settles at once). The "through"
            # mark stays at the hour's start: the hourly pass covers the hour once it has closed.
            result = await self.peek(cursor, now_ms)
            if result:
                self.store.put(f"touch:{self.spec.slug}", {**result, "start": self.start_ms})
                return
        if now_ms >= self.window_end and cursor < self.window_end:
            # past the deadline: the rest of the window, minute by minute, through the deadline's minute
            rows = await self.get("klines", symbol=self.spec.symbol, interval="1m", startTime=cursor,
                                  endTime=self.window_end - 1, limit=1000)
            minutes = [r for r in rows if isinstance(r, list) and len(r) > 6 and int(r[0]) >= cursor - 59_999
                       and int(r[0]) <= self.spec.deadline_ms]
            for row in minutes:
                opened, closed, hi, lo = int(row[0]), int(row[6]), float(row[2]), float(row[3])
                if closed < cursor:
                    continue
                hit_low, hit_high = lo <= low, hi >= high
                if hit_low or hit_high:
                    kind = "ambiguous" if (hit_low and hit_high) or opened < self.start_ms else "low" if hit_low else "high"
                    self.store.put(f"touch:{self.spec.slug}", {"kind": kind, "time": opened, "hi": hi, "lo": lo,
                                                               "start": self.start_ms})
                    return
            if minutes and max(int(r[0]) for r in minutes) >= self.spec.deadline_ms:
                cursor = self.window_end  # every minute up to the deadline's own one has been read
            elif minutes:
                cursor = max(cursor, max(int(r[6]) for r in minutes) + 1)  # read this far; the rest next time
        self.store.put(f"touch:{self.spec.slug}", {"kind": "clear", "through": cursor, "start": self.start_ms})

    async def peek(self, start: int, now_ms: int) -> dict | None:
        """The closed minutes from ``start`` (inside the hour still running) up to now: a line reached in one of them
        is a hit like any other; the minute still forming decides nothing."""
        rows = await self.get("klines", symbol=self.spec.symbol, interval="1m", startTime=start, endTime=now_ms, limit=60)
        low, high = float(self.spec.low), float(self.spec.high)
        for row in rows:
            if not (isinstance(row, list) and len(row) > 6):
                continue
            opened, closed, hi, lo = int(row[0]), int(row[6]), float(row[2]), float(row[3])
            if closed > now_ms or closed < start or opened > self.spec.deadline_ms:
                continue
            hit_low, hit_high = lo <= low, hi >= high
            if hit_low or hit_high:
                kind = "ambiguous" if (hit_low and hit_high) or opened < self.start_ms else "low" if hit_low else "high"
                return {"kind": kind, "time": opened, "hi": hi, "lo": lo}
        return None

    def verified_clear(self) -> bool:
        """The whole window, through the deadline's minute, has been read and neither line was reached."""
        hist = self.history
        return hist.get("kind") == "clear" and int(hist.get("through") or 0) >= self.window_end

    async def scan_hour(self, hour_ms: int, start: int, end: int) -> dict | None:
        rows = await self.get("klines", symbol=self.spec.symbol, interval="1m", startTime=hour_ms, endTime=hour_ms + 3_599_999, limit=60)
        for row in rows:
            opened, closed, hi, lo = int(row[0]), int(row[6]), float(row[2]), float(row[3])
            if closed < start:
                continue
            hit_low, hit_high = lo <= float(self.spec.low), hi >= float(self.spec.high)
            if not (hit_low or hit_high):
                continue
            if opened < self.start_ms or closed > end or (hit_low and hit_high):
                return {"kind": "ambiguous", "time": opened, "hi": hi, "lo": lo}
            return {"kind": "low" if hit_low else "high", "time": opened, "hi": hi, "lo": lo}
        return None

    def odds(self, now_ms: int) -> TouchOdds | str:
        hist = self.history
        if hist.get("kind") == "low":
            return TouchOdds(1.0, 0.0, 0.0)
        if hist.get("kind") == "high":
            return TouchOdds(0.0, 1.0, 0.0)
        if self.price is None or self.sigma is None:
            return f"等待币安行情（{brief_error(self.error, 60)}）" if self.error else "等待币安行情"
        if now_ms - self.priced_ms > self.PRICE_STALE_MS:
            why = f"：{brief_error(self.error, 60)}" if self.error else ""
            return f"币安价格停在 {stamp(self.priced_ms, seconds=False)}（{(now_ms - self.priced_ms) // 60_000} 分钟未更新{why}），暂停概率"
        return first_touch(float(self.price), float(self.spec.low), float(self.spec.high), self.sigma, 0.0,
                           max(0.0, (self.spec.deadline_ms - max(now_ms, self.start_ms)) / YEAR_MS))

    def model_swing(self, now_ms: int) -> float:
        """How far the fair "high first" price moves with σ ×/÷ 1.25 (σ is a 30-day estimate), or with a driftless log
        price (μ = σ²/2) instead of a driftless price (each varied alone): over months the drift convention alone moves
        the barrier odds by cents, as it does on the BTC up/down card."""
        odds = self.odds(now_ms)
        if not isinstance(odds, TouchOdds) or self.history.get("kind") in {"low", "high"}:
            return 0.0
        years = max(0.0, (self.spec.deadline_ms - max(now_ms, self.start_ms)) / YEAR_MS)
        spot, low, high = float(self.price), float(self.spec.low), float(self.spec.high)
        variants = [(self.sigma * MODEL_SIGMA_ERROR, 0.0), (self.sigma / MODEL_SIGMA_ERROR, 0.0),
                    (self.sigma, (LOG_DRIFT_VARIANT + 0.5) * self.sigma * self.sigma)]
        return max(abs(first_touch(spot, low, high, sigma, mu, years).fair_upper - odds.fair_upper) for sigma, mu in variants)

    def advice_problem(self, now_ms: int) -> str:
        """Why the model's edge must not be recommended now ("" when it may): the path since the window opened has
        to be verified (one minute spanning both barriers cannot be ordered), and the inputs must be current."""
        hist, kind = self.history, self.history.get("kind")
        if kind == "ambiguous":
            return "开盘以来有一分钟同时碰到两条线，先后不明，需人工核对；暂不给建议"
        if kind in {"low", "high"}:
            return ""
        if not self.start_ms:
            return "开盘时间未知，无法核验此前是否已触线；暂不给建议"
        if kind != "clear":
            return "开盘以来是否触线尚未核验；暂不给建议"
        if now_ms - int(hist.get("through") or 0) > self.SCAN_STALE_MS:
            return f"触线核验停在 {stamp(int(hist['through']), seconds=False)}；暂不给建议"
        if now_ms - self.sigma_ms > self.SIGMA_STALE_MS:
            return "波动率超过一天未更新；暂不给建议"
        return ""

    def status(self) -> str:
        hist = self.history
        kind = hist.get("kind")
        if kind in {"low", "high"}:
            return f"已于 {stamp(hist['time'], seconds=False)} 先触及 ${int(self.spec.low if kind == 'low' else self.spec.high)}"
        if kind == "ambiguous":
            return f"需人工核对：{stamp(hist['time'], seconds=False)} 这一分钟无法判断先后（高 {hist['hi']:g}·低 {hist['lo']:g}）"
        if kind == "clear" and int(hist["through"]) >= self.window_end:
            return "整个窗口都已核验：两条线都没碰到"
        if kind == "clear":
            return f"开盘以来未触线（核至 {stamp(hist['through'], seconds=False)}）"
        return "开盘以来是否触线：待核验" if self.start_ms else "开盘时间未知：按此前未触线计算"


# --- period up/down market (does the pair end the month above the close of its first minute?) -------------------
def us_eastern_offset(ms: int) -> int:
    """US Eastern UTC offset at an instant: −4 (EDT) from the second Sunday of March 02:00 EST to the first Sunday of
    November 02:00 EDT, else −5 (EST). No tz database needed (slim images often lack one)."""
    utc = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)
    sunday = lambda day: day + dt.timedelta(days=(6 - day.weekday()) % 7)  # the first Sunday on or after day
    starts = dt.datetime.combine(sunday(dt.date(utc.year, 3, 8)), dt.time(7), dt.timezone.utc)    # 02:00 EST
    ends = dt.datetime.combine(sunday(dt.date(utc.year, 11, 1)), dt.time(6), dt.timezone.utc)     # 02:00 EDT
    return -4 if starts <= utc < ends else -5


@dataclass(frozen=True)
class UpDownSpec:
    """A "<coin> Up/Down <month>" market (BTC, ETH): Up when the Binance 1-minute candle named for the period's end
    closes above the one named for its start, Down when below, 50-50 when equal. Candles are named by their open time
    in US Eastern."""
    key: str        # item / book key, also the card's favourite key (not the bare pair: a 先触 card has that one)
    slug: str
    symbol: str     # Binance spot pair (the resolution source)
    name: str       # card title
    start_ms: int   # open time of the starting 1-minute candle: its close is the line to beat
    end_ms: int     # open time of the final 1-minute candle: its close settles the market

    def label(self, open_ms: int, plain: bool = False) -> str:
        """'09-30 23:59 ET（北京 10-01 11:59）' for the candle opening at open_ms; plain: '09-30 23:59 ET，北京 10-01 11:59'."""
        et = dt.datetime.fromtimestamp(open_ms / 1000, dt.timezone(dt.timedelta(hours=us_eastern_offset(open_ms))))
        bj = stamp(open_ms, seconds=False)
        return f"{et:%m-%d %H:%M} ET，北京 {bj}" if plain else f"{et:%m-%d %H:%M} ET（北京 {bj}）"


UPDOWN_MARKETS = (
    # "the Binance 1 minute candle for BTC/USDT Sep 30 '26 11:59 PM in the ET timezone" against the one for
    # "Oct 31 '26 11:59 PM ET" (both EDT: daylight time ends on Nov 1)
    UpDownSpec("BTC-2026-10", "btc-up-down-october-2026", "BTCUSDT", "BTC 10月涨跌",
               et_ms(2026, 9, 30, 23, 59, -4), et_ms(2026, 10, 31, 23, 59, -4)),
    # the same two minutes on ETH/USDT ("ETH closed September at $2,696.07 on Binance": the starting candle's close)
    UpDownSpec("ETH-2026-10", "eth-up-down-october-2026", "ETHUSDT", "ETH 10月涨跌",
               et_ms(2026, 9, 30, 23, 59, -4), et_ms(2026, 10, 31, 23, 59, -4)),
)


def updown_odds(price: float, line: float, sigma: float, years: float, drift: float = -0.5,
                tick: float = 0.01) -> tuple[float, float, float, float]:
    """(up, flat, down, z) for a close `years` from now against `line`: log-normal with annual σ, its log mean moved by
    drift·σ²τ (−½: no drift in the price itself, so the median sits σ²τ/2 below today's price; 0: median = today's
    price). Prices are whole ticks; a close equal to the line is `flat` and settles 50-50."""
    s = sigma * math.sqrt(max(years, 0.0))
    if not s > 0:
        return (1.0, 0.0, 0.0, math.inf) if price > line else (0.0, 0.0, 1.0, -math.inf) if price < line else (0.0, 1.0, 0.0, 0.0)
    mean = math.log(price) + drift * s * s
    up = 1 - norm_cdf((math.log(line + tick / 2) - mean) / s)
    down = norm_cdf((math.log(line - tick / 2) - mean) / s) if line > tick / 2 else 0.0
    return up, max(0.0, 1 - up - down), down, (mean - math.log(line)) / s


@dataclass(frozen=True)
class UpDownOdds:
    up: float
    flat: float      # the final close equals the line (settles 50-50)
    down: float
    z: float
    line: D          # the starting candle's close
    price: D         # what it is measured from: the live price, or the final close once settled
    years: float     # time left until the final candle closes (0 once settled)
    settled: bool = False

    @property
    def fair_up(self) -> float:
        return self.up + self.flat / 2

    @property
    def fair_down(self) -> float:
        return self.down + self.flat / 2


class UpDownMarket:
    """A Binance spot pair against the close of its period's first 1-minute candle: live price, 30-day σ, and the two
    candle closes that set and settle the market, each read once its minute is over (strictly that minute) and kept."""
    PRICE_SECONDS = 30
    VOL_SECONDS = 3600
    CANDLE_SECONDS = 30            # a candle still missing is asked for again this often
    CANDLE_GRACE_MS = 2_000        # and only this long after its minute ended
    PRICE_STALE_MS = TouchMarket.PRICE_STALE_MS
    SIGMA_STALE_MS = TouchMarket.SIGMA_STALE_MS

    def __init__(self, store: "Store", spec: UpDownSpec):
        self.store, self.spec = store, spec
        self.price: D | None = None
        self.priced_ms = 0
        self.sigma: float | None = None
        self.sigma_ms = 0
        self.error = ""
        self.times = {"price": -1e9, "vol": -1e9, "candle": -1e9}

    async def get(self, path: str, **params: Any) -> Any:
        return await binance_spot(path, **params)

    def close_of(self, which: str) -> D | None:
        """The kept close of the "start" or "end" candle, None until it has been read."""
        open_ms = self.spec.start_ms if which == "start" else self.spec.end_ms
        saved = self.store.get(f"updown:{self.spec.slug}:{which}")
        with contextlib.suppress(TypeError, KeyError, ValueError, decimal.InvalidOperation):
            if int(saved["open"]) == open_ms:
                value = D(str(saved["close"]))
                return value if value > 0 else None
        return None

    async def read_candle(self, open_ms: int) -> D:
        """The close of exactly the 1-minute candle that opens at open_ms (Binance answers with the next minute when
        that one is missing: a neighbouring minute is never taken for it)."""
        rows = await self.get("klines", symbol=self.spec.symbol, interval="1m", startTime=open_ms,
                              endTime=open_ms + 59_999, limit=1)
        row = next((r for r in rows or [] if isinstance(r, list) and len(r) > 6 and int(r[0]) == open_ms), None)
        if row is None or int(row[6]) != open_ms + 59_999:
            raise ValueError(f"币安没有返回 {self.spec.label(open_ms)}这根 1 分钟 K 线")
        return number(row[4], f"{self.spec.symbol} 1 分钟 K 收盘价")

    async def refresh(self, now_ms: int) -> None:
        mono = time.monotonic()
        errors = []
        if mono - self.times["candle"] >= self.CANDLE_SECONDS:
            self.times["candle"] = mono
            for which, open_ms in (("start", self.spec.start_ms), ("end", self.spec.end_ms)):
                if self.close_of(which) is None and now_ms >= open_ms + 60_000 + self.CANDLE_GRACE_MS:
                    try:
                        close = await self.read_candle(open_ms)
                        self.store.put(f"updown:{self.spec.slug}:{which}", {"open": open_ms, "close": str(close), "read_ms": now_ms})
                    except Exception as error:
                        errors.append(clean_error(error) or type(error).__name__)
        if self.close_of("end") is None:  # settled: nothing is left to price
            try:
                if mono - self.times["price"] >= self.PRICE_SECONDS:
                    self.times["price"] = mono
                    data = await self.get("ticker/price", symbol=self.spec.symbol)
                    self.price, self.priced_ms = number(data["price"], self.spec.symbol), now_ms
                if mono - self.times["vol"] >= self.VOL_SECONDS or self.sigma is None:
                    self.times["vol"] = mono
                    self.sigma = realized_vol(await self.get("klines", symbol=self.spec.symbol, interval="1h", limit=722), now_ms)
                    self.sigma_ms = now_ms
            except Exception as error:
                errors.append(clean_error(error) or type(error).__name__)
        self.error = "；".join(errors)

    def odds(self, now_ms: int) -> UpDownOdds | str:
        spec = self.spec
        line, final = self.close_of("start"), self.close_of("end")
        if line is not None and final is not None:
            up, down = float(final > line), float(final < line)
            return UpDownOdds(up, 1.0 - up - down, down, 0.0, line, final, 0.0, True)
        why = f"（{brief_error(self.error, 60)}）" if self.error else ""
        if line is None:
            if now_ms < spec.start_ms + 60_000:
                return f"起点价要等 {spec.label(spec.start_ms)}这根 1 分钟 K 收盘后确定"
            return f"等待币安 {spec.label(spec.start_ms)}这根 1 分钟 K 的收盘价{why}"
        if now_ms >= spec.end_ms + 60_000:
            return f"已到结算时刻，等待币安 {spec.label(spec.end_ms)}这根 1 分钟 K 的收盘价{why}"
        if self.price is None or self.sigma is None:
            return f"等待币安行情{why}"
        if now_ms - self.priced_ms > self.PRICE_STALE_MS:
            return (f"币安价格停在 {stamp(self.priced_ms, seconds=False)}（{(now_ms - self.priced_ms) // 60_000} 分钟未更新"
                    f"{'：' + brief_error(self.error, 60) if self.error else ''}），暂停概率")
        if not self.sigma > 0:
            return "波动率无效，暂停概率"
        years = (spec.end_ms + 60_000 - now_ms) / YEAR_MS
        up, flat, down, z = updown_odds(float(self.price), float(line), self.sigma, years)
        return UpDownOdds(up, flat, down, z, line, self.price, years)

    def model_swing(self, odds: UpDownOdds) -> float:
        """How far the fair Up price moves with σ ×/÷ 1.25, or with the median at today's price instead of σ²τ/2 below
        it (each varied alone): over a month that alone is a few cents at the line."""
        if odds.settled or not self.sigma:
            return 0.0
        def fair(sigma: float, drift: float) -> float:
            up, flat, _, _ = updown_odds(float(odds.price), float(odds.line), sigma, odds.years, drift)
            return up + flat / 2
        return max(abs(fair(sigma, drift) - odds.fair_up) for sigma, drift in
                   ((self.sigma * MODEL_SIGMA_ERROR, -0.5), (self.sigma / MODEL_SIGMA_ERROR, -0.5), (self.sigma, 0.0)))

    def advice_problem(self, odds: UpDownOdds, now_ms: int) -> str:
        """Why no trade is suggested from these odds ("" when one may be)."""
        if odds.settled:
            return "已出结果，以 Predict 结算为准；不再给建议"
        if now_ms - self.sigma_ms > self.SIGMA_STALE_MS:
            return "波动率超过一天未更新；暂不给建议"
        return ""


# --- flip market (does one coin trade above another on Hyperliquid within the window?) -------------------------------
HOUR_MS = 3_600_000


@dataclass(frozen=True)
class FlipSpec:
    """A "will A flip B" market: Yes once any 1-minute candle in the window closes with A above B at the same timestamp.
    Hyperliquid's USDC perps; the rules leave spot markets out."""
    key: str         # item / book key, also the card's favourite key
    slug: str
    name: str        # card title
    coin: str        # the one that has to climb (HYPE)
    other: str       # the one it has to pass (SOL)
    start_ms: int    # open time of the first 1-minute candle that counts
    end_ms: int      # open time of the last one

    def window(self) -> str:
        et = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.timezone(dt.timedelta(hours=us_eastern_offset(ms)))).strftime("%m-%d %H:%M")
        return (f"{et(self.start_ms)} – {et(self.end_ms)} ET（北京 {stamp(self.start_ms, seconds=False)} – "
                f"{stamp(self.end_ms, seconds=False)}）")


FLIP_MARKETS = (
    # "at any point between October 2, 2026, 04:00 AM ET and October 31, 2026, 11:59 PM ET" (both EDT): the HYPE/USDC
    # and SOL/USDC 1-minute closes with the same timestamp on Hyperliquid
    FlipSpec("HYPE-SOL", "will-hype-flip-sol-by-nov-26", "HYPE 反超 SOL", "HYPE", "SOL",
             et_ms(2026, 10, 2, 4, 0, -4), et_ms(2026, 10, 31, 23, 59, -4)),
)


def short_price(value: D | None) -> str:
    """90.2515 -> '90.25', 0.0123456 -> '0.01235': short enough for a card's one-line summary."""
    if value is None:
        return "—"
    return f"{value:,.2f}" if value >= 10 else f"{value:.4g}"


def ratio_vol(a_rows: list, b_rows: list, now_ms: int) -> float:
    """Annualised σ of ln(A/B) from the last 721 finished hours both coins have (Hyperliquid 1h candles)."""
    closes = lambda rows: {int(r["t"]): float(r["c"]) for r in rows if int(r["t"]) + HOUR_MS <= now_ms and float(r["c"]) > 0}
    a, b = closes(a_rows), closes(b_rows)
    hours = sorted(set(a) & set(b))[-721:]
    if len(hours) < 721:
        raise ValueError("30 日小时 K 线不足")
    logs = [math.log(a[t] / b[t]) for t in hours]
    rets = [y - x for x, y in zip(logs, logs[1:])]
    mean = sum(rets) / len(rets)
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1) * 365 * 24)


class FlipMarket:
    """Two Hyperliquid perps against each other: live mids, the 30-day σ of their price ratio, and the path since the
    window opened. The path check reads hourly bars and opens an hour's 1-minute candles only when a flip was possible
    in it (A's high reached B's low), so it costs next to nothing while the two are far apart."""
    PRICE_SECONDS = 30
    VOL_SECONDS = 3600
    SCAN_SECONDS = 300
    PRICE_STALE_MS = 5 * 60_000     # mids older than this price nothing
    SIGMA_STALE_MS = 24 * 3600_000  # σ not re-measured for a day: shown, not advised on
    SCAN_STALE_MS = 3 * 3600_000    # the path check must reach this close to now before advice is given

    def __init__(self, store: "Store", spec: FlipSpec):
        self.store, self.spec = store, spec
        self.prices: dict[str, D] = {}
        self.priced_ms = 0
        self.sigma: float | None = None
        self.sigma_ms = 0
        self.error = ""
        self.times = {"price": -1e9, "vol": -1e9, "scan": -1e9}

    async def info(self, payload: dict) -> Any:
        return await http_json(Hyperliquid.URL, payload)

    async def candles(self, coin: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
        rows = await self.info({"type": "candleSnapshot", "req": {"coin": coin, "interval": interval,
                                                                  "startTime": start_ms, "endTime": end_ms}})
        if not isinstance(rows, list):
            raise ValueError("Hyperliquid K 线格式异常")
        return [r for r in rows if isinstance(r, dict) and str(r.get("t", "")).isdigit()]

    @property
    def ratio(self) -> float | None:
        a, b = self.prices.get(self.spec.coin), self.prices.get(self.spec.other)
        return float(a / b) if a and b else None

    @property
    def history(self) -> dict:
        """{'kind': 'clear'|'flip', 'through': ms (next hour to check), 'top'/'top_at': highest hourly-close ratio,
        'time'/'a'/'b': the flip minute and both closes, 'start': the window} or {}."""
        saved = self.store.get(f"flip:{self.spec.slug}", {})
        return saved if isinstance(saved, dict) and saved.get("start") == self.spec.start_ms else {}

    async def refresh(self, now_ms: int) -> None:
        mono, errors = time.monotonic(), []
        if mono - self.times["price"] >= self.PRICE_SECONDS:
            self.times["price"] = mono
            try:
                mids = await self.info({"type": "allMids"})
                prices = {}
                for coin in (self.spec.coin, self.spec.other):
                    if not isinstance(mids, dict) or coin not in mids:
                        raise ValueError(f"Hyperliquid 没有 {coin} 的报价")
                    prices[coin] = number(mids[coin], f"{coin} 价格")
                self.prices, self.priced_ms = prices, now_ms
            except Exception as error:
                errors.append(clean_error(error) or type(error).__name__)
        if mono - self.times["vol"] >= self.VOL_SECONDS or self.sigma is None:
            self.times["vol"] = mono
            try:
                first = now_ms - 32 * 24 * HOUR_MS
                a = await self.candles(self.spec.coin, "1h", first, now_ms)
                b = await self.candles(self.spec.other, "1h", first, now_ms)
                self.sigma, self.sigma_ms = ratio_vol(a, b, now_ms), now_ms
            except Exception as error:
                errors.append(f"波动率：{clean_error(error) or type(error).__name__}")
        if mono - self.times["scan"] >= self.SCAN_SECONDS:
            self.times["scan"] = mono
            try:
                await self.scan(now_ms)
            except Exception as error:
                errors.append(f"反超核验：{clean_error(error) or type(error).__name__}")
        self.error = "；".join(errors)

    async def scan(self, now_ms: int) -> None:
        """Extend the flip check from where it stopped (kept) up to the last closed minute of the window."""
        spec, hist = self.spec, self.history
        if hist.get("kind") == "flip":
            return
        end = min(now_ms - now_ms % 60_000, spec.end_ms + 60_000)  # minutes opening before this have closed
        if end <= spec.start_ms:
            return  # the window has not opened yet: nothing to check
        cursor = int(hist.get("through") or spec.start_ms)
        if cursor >= end:
            return  # checked to the end of the window (or to this very minute)
        top, top_at = float(hist.get("top") or 0), int(hist.get("top_at") or 0)
        first = cursor - cursor % HOUR_MS
        a_rows = await self.candles(spec.coin, "1h", first, end)
        b_by_hour = {int(r["t"]): r for r in await self.candles(spec.other, "1h", first, end)}
        for row in sorted(a_rows, key=lambda r: int(r["t"])):
            t, other = int(row["t"]), b_by_hour.get(int(row["t"]))
            if t + HOUR_MS <= cursor or t >= end:
                continue
            if other is None:
                break  # an hour one coin lacks cannot be vouched for: try again next time
            if float(row["h"]) >= float(other["l"]):  # only then can a minute close with A above B
                hit = await self.scan_minutes(max(t, cursor, spec.start_ms), min(t + HOUR_MS, end))
                if hit:
                    self.store.put(f"flip:{spec.slug}", {**hit, "start": spec.start_ms, "top": max(top, hit["a"] / hit["b"]), "top_at": hit["time"]})
                    return
            ratio = float(row["c"]) / float(other["c"])
            if ratio > top:
                top, top_at = ratio, t
            if t + HOUR_MS > end:
                break  # the running hour: its closed minutes were checked; the hour itself is not done yet
            cursor = t + HOUR_MS
        self.store.put(f"flip:{spec.slug}", {"kind": "clear", "through": cursor, "start": spec.start_ms, "top": top, "top_at": top_at})

    async def scan_minutes(self, start_ms: int, end_ms: int) -> dict | None:
        """The first closed minute in [start_ms, end_ms) whose A close is above B's (same timestamp), or None."""
        if end_ms <= start_ms:
            return None
        a = {int(r["t"]): float(r["c"]) for r in await self.candles(self.spec.coin, "1m", start_ms, end_ms - 1)}
        b = {int(r["t"]): float(r["c"]) for r in await self.candles(self.spec.other, "1m", start_ms, end_ms - 1)}
        if not a or not b:
            raise ValueError(f"Hyperliquid 没有 {stamp(start_ms, seconds=False)} 起的 1 分钟 K 线，无法核验")  # kept ~3.5 days
        for t in sorted(set(a) & set(b)):
            if start_ms <= t < end_ms and t <= self.spec.end_ms and a[t] > b[t]:
                return {"kind": "flip", "time": t, "a": a[t], "b": b[t]}
        return None

    def odds(self, now_ms: int) -> float | str:
        """P(Yes), or why there is none: 1 once a minute closed with A above B; otherwise the chance the ratio's running
        maximum reaches 1 before the window ends (zero drift, fixed σ, the window's remaining time)."""
        spec, hist = self.spec, self.history
        if hist.get("kind") == "flip":
            return 1.0
        why = f"（{brief_error(self.error, 60)}）" if self.error else ""
        if now_ms >= spec.end_ms + 60_000:
            if hist.get("kind") == "clear" and int(hist.get("through") or 0) >= spec.end_ms + 60_000:
                return 0.0
            return f"窗口已结束，等待核验最后几小时{why}"
        if self.ratio is None or self.sigma is None:
            return f"等待 Hyperliquid 行情{why}"
        if now_ms - self.priced_ms > self.PRICE_STALE_MS:
            return (f"Hyperliquid 价格停在 {stamp(self.priced_ms, seconds=False)}（{(now_ms - self.priced_ms) // 60_000} 分钟未更新"
                    f"{'：' + brief_error(self.error, 60) if self.error else ''}），暂停概率")
        years = (spec.end_ms + 60_000 - max(now_ms, spec.start_ms)) / YEAR_MS
        return hit_probability(self.ratio, 1.0, self.sigma, years)

    def model_swing(self, now_ms: int) -> float:
        """How far P(Yes) moves with σ ×/÷ 1.25 (σ is a 30-day estimate), or with a driftless log ratio instead of a
        driftless ratio (each varied alone)."""
        odds = self.odds(now_ms)
        if not isinstance(odds, float) or odds >= 1.0 or not self.sigma or self.ratio is None:
            return 0.0
        years = max(0.0, (self.spec.end_ms + 60_000 - max(now_ms, self.spec.start_ms)) / YEAR_MS)
        variants = [(self.sigma * MODEL_SIGMA_ERROR, -0.5), (self.sigma / MODEL_SIGMA_ERROR, -0.5), (self.sigma, LOG_DRIFT_VARIANT)]
        return max(abs(hit_probability(self.ratio, 1.0, sigma, years, drift) - odds) for sigma, drift in variants)

    def advice_problem(self, now_ms: int) -> str:
        """Why the model's edge must not be suggested now ("" when it may): the window's path must be checked up to
        nearly now, and σ must be current."""
        hist, kind = self.history, self.history.get("kind")
        if kind == "flip":
            return ""
        if now_ms > self.spec.start_ms + self.SCAN_STALE_MS:
            if kind != "clear":
                return "窗口开始以来是否反超尚未核验；暂不给建议"
            through = int(hist.get("through") or 0)
            if through < self.spec.end_ms + 60_000 and now_ms - through > self.SCAN_STALE_MS:
                return f"反超核验停在 {stamp(through, seconds=False)}；暂不给建议"
        if now_ms - self.sigma_ms > self.SIGMA_STALE_MS:
            return "波动率超过一天未更新；暂不给建议"
        return ""

    def status(self, now_ms: int) -> str:
        spec, hist = self.spec, self.history
        if hist.get("kind") == "flip":
            return (f"已于 {stamp(hist['time'], seconds=False)} 这一分钟反超：{spec.coin} {hist['a']:g} > {spec.other} {hist['b']:g}")
        if now_ms < spec.start_ms:
            return f"窗口北京 {stamp(spec.start_ms, seconds=False)} 开始"
        if hist.get("kind") == "clear":
            top = f"；窗口内最高比值 {hist['top']:.4f}（{stamp(hist['top_at'], seconds=False)} 那一小时收盘）" if hist.get("top") else ""
            return f"窗口开始以来未反超（核至 {stamp(hist['through'], seconds=False)}）{top}"
        return "窗口开始以来是否反超：待核验"


# --- market-cap ladder (will a token's market cap reach each threshold?) ----------------------------
@dataclass(frozen=True)
class CapSpec:
    """A "what market cap / FDV will X hit" category: Yes/No per threshold, touched on any 1-minute candle."""
    key: str
    slug: str
    name: str             # card title
    token: str            # contract / mint, as the chain spells it
    start_ms: int         # resolution window, from the rules
    end_ms: int
    targets: tuple[D, ...]  # shown until Predict's own market titles are read
    trade_end: str = ""   # when Predict stops trading, if earlier than the window
    chain: str = "bsc"    # DexScreener chain id
    pair: str = ""        # a named DexScreener pair (the rules' source); else the token's most liquid pair
    supply: str = "rpc"   # "rpc": BSC totalSupply − dead balances; "fdv": DexScreener's FDV ÷ price
    gecko: str = "bsc"    # GeckoTerminal network for hourly bars; "" = none (σ prior, live-observed high only)
    metric: str = "市值"
    settle: str = "Flap.sh"
    prior_sigma: float = 3.0  # annualised σ assumed when there are no bars to measure it
    gecko_pool: str = ""  # the GeckoTerminal pool for the bars; "" = the token's most liquid pool, looked up by its address


CAP_MARKETS = (
    # "from 11:30 PM ET on August 16, 2026 to 11:59 PM ET on October 31, 2026" (both EDT); settles on Flap.sh
    CapSpec("NIULAI", "what-marketcap-will-niu-lai-hit-before-nov-2026", "$牛来 市值", "0xbeea1d618e533a387d941f58a7d4c9b7bd377777",
            et_ms(2026, 8, 16, 23, 30, -4), et_ms(2026, 10, 31, 23, 59, -4),
            (D("2e8"), D("3e8"), D("5e8"), D("1e9")), "Predict 交易至北京 11-01 07:59"),
    # "between market creation on August 17, 13:00 PM ET to 11:59 PM ET on October 31, 2026"; FDV = price × total supply
    CapSpec("ANSEM", "what-fdv-will-ansem-hit-before-nov-2026", "$ANSEM FDV", "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump",
            et_ms(2026, 8, 17, 13, 0, -4), et_ms(2026, 10, 31, 23, 59, -4),
            tuple(D(f"{n}e8") for n in (5, 6, 7, 8, 9)) + (D("1e9"),),
            chain="solana", supply="fdv", gecko="solana", metric="FDV", settle="pump.fun"),
    # "between market creation on August 31, 2026 at 06:00 AM ET to October 31, 2026 at 11:59 PM ET"; the rules' own
    # source is this DexScreener pair; FDV = (total supply − burned) × price
    CapSpec("PONS", "what-fdv-will-pons-hit-before-nov-2026", "$PONS FDV", "0x39dBED3a2bd333467115dE45665cC57F813C4571",
            et_ms(2026, 8, 31, 6, 0, -4), et_ms(2026, 10, 31, 23, 59, -4),
            (D("7e8"), D("8e8"), D("9e8"), D("1e9")),
            chain="robinhood", pair="0x10cc6bd38112cac182db90b6a71d8bb5939526ba", supply="fdv", gecko="",
            metric="FDV", settle="DexScreener"),
    # three more Robinhood-chain FDV ladders settled on the rules' own DexScreener pairs (FDV = (total − burned) × price);
    # their levels are not listed here: the card shows the ones Predict's market titles carry
    # "between market creation on September 4 at 6:00 AM ET, 2026 to October 31, 2026 at 11:59 PM ET" (MEME/USDG)
    CapSpec("MEME", "what-fdv-will-meme-hit-before-november-2026", "$MEME FDV", "0x385F4f8ae47651ce5F58F5265395a669f8281e18",
            et_ms(2026, 9, 4, 6, 0, -4), et_ms(2026, 10, 31, 23, 59, -4), (),
            chain="robinhood", pair="0xc6e298e137f2905398db87e6eae49ede64d231fee37330fa433fec917f4618b6", supply="fdv", gecko="",
            metric="FDV", settle="DexScreener"),
    # "between market creation on September 2, 2026 at 01:00 AM ET to October 31, 2026 at 11:59 PM ET" (CASHCAT/WETH)
    CapSpec("CASHCAT", "what-fdv-will-cashcat-hit-before-november-2026", "$CASHCAT FDV", "0x020bfC650A365f8BB26819deAAbF3E21291018b4",
            et_ms(2026, 9, 2, 1, 0, -4), et_ms(2026, 10, 31, 23, 59, -4), (),
            chain="robinhood", pair="0xa70fc67c9f69da90b63a0e4c05d229954574e313", supply="fdv", gecko="", metric="FDV", settle="DexScreener"),
    # "between market creation on September 1, 2026 at 5:00 AM ET to October 31, 2026 at 11:59 PM ET" (the AI pair)
    CapSpec("AI", "what-fdv-will-ai-hit-before-nov-2026", "$AI FDV", "0x2E8c31162b855A2ffa90F6F8634643Ad6F111e18",
            et_ms(2026, 9, 1, 5, 0, -4), et_ms(2026, 10, 31, 23, 59, -4), (),
            chain="robinhood", pair="0xcbdfea90430a30ee4469c9902e120a77e7c7e4711d5643671c1d1957f2f1ce27", supply="fdv", gecko="",
            metric="FDV", settle="DexScreener"),
    # "between market creation on September 6 at 04:00 AM ET, 2026 to October 31, 2026 at 11:59 PM ET" (STONK/SOL on Solana);
    # the rules' own DexScreener pair settles it, FDV = (total − burned) × price; the pair is spelt as the rules' link spells it
    # (DexScreener reads it either way), which GeckoTerminal would not accept as a pool path; the hourly bars (σ, the window's
    # high since 09-06) come from the mint's pool whose address matches it without regard to case, else its most liquid pool
    CapSpec("STONK", "what-fdv-will-stonk-hit-before-november-2026", "$STONK FDV", "6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rUJHacpMpUNgx",
            et_ms(2026, 9, 6, 4, 0, -4), et_ms(2026, 10, 31, 23, 59, -4), (),
            chain="solana", pair="afrddtgywcveqb1gxcahr8i48o6qtxyqksdvkeludehg", supply="fdv", gecko="solana", metric="FDV", settle="DexScreener"),
    # "between market creation on September 1, 2026 at 3:45 AM ET to October 31, 2026 at 11:59 PM ET" (STONKBROKER/WETH)
    CapSpec("STONKBROKER", "what-fdv-will-stonkbroker-hit-by-november-2026", "$STONKBROKER FDV", "0xe934e36A439C94017B64a3FecE66AF12099aBF50",
            et_ms(2026, 9, 1, 3, 45, -4), et_ms(2026, 10, 31, 23, 59, -4), (),
            chain="robinhood", pair="0xd33c8fd38b06e989cdbd4dffdefab71c4bdd415b24964c8d69e38ff35b068f92", supply="fdv", gecko="",
            metric="FDV", settle="DexScreener"),
)
BSC_RPC = ("https://bsc-dataseed.bnbchain.org", "https://bsc-dataseed.binance.org", "https://bsc-rpc.publicnode.com")
BURN_ADDRESSES = ("0x000000000000000000000000000000000000dead", "0x0000000000000000000000000000000000000000")
GECKO = "https://api.geckoterminal.com/api/v2/networks"


def hit_probability(spot: float, level: float, sigma: float, years: float, drift: float = -0.5) -> float:
    """P(the running maximum reaches ``level`` before ``years``) for a GBM whose log drift is drift·σ² (−½: a driftless
    price, the median σ²T/2 below today's; 0: a driftless log price): Φ((−h + bT)/s) + e^{2bh/σ²}·Φ((−h − bT)/s),
    h = ln(K/S), b = drift·σ², s = σ√T. With drift −½ this is Φ((−h − s²/2)/s) + (S/K)·Φ((−h + s²/2)/s)."""
    if spot >= level:
        return 1.0
    if years <= 0 or sigma <= 0:
        return 0.0
    h, s = math.log(level / spot), sigma * math.sqrt(years)
    b = drift * sigma * sigma
    return min(1.0, norm_cdf((-h + b * years) / s) + math.exp(2 * b * h / (sigma * sigma)) * norm_cdf((-h - b * years) / s))


def sampled_coverage(samples: list) -> tuple[float, float]:
    """(Σ squared log returns, seconds covered) from [[epoch s, price], ...]; gaps over an hour are skipped."""
    pts = sorted((int(t), float(p)) for t, p in samples if float(p) > 0)
    squares = seconds = 0.0
    for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
        if 0 < t1 - t0 <= 3600:
            squares += math.log(p1 / p0) ** 2
            seconds += t1 - t0
    return squares, seconds


def bars_sigma(bars: list) -> tuple[float, float, int]:
    """(annualised σ, hours covered, bars missing) from hourly bars [(open s, o, h, l, close)] oldest first, weighting
    each close-to-close return by the time it spans: GeckoTerminal serves no bar for an hour without a trade, so two
    neighbouring bars may be 2–6 hours apart, and taking every return as one hour's would overstate σ by up to 80%.
    σ² = Σ (r − μ·Δt)² / Δt ÷ (n − 1), μ = Σr / ΣΔt (the hourly-sample formula when no bar is missing)."""
    pts = [(int(b[0]), float(b[4])) for b in bars if float(b[4]) > 0]
    steps = [(t1 - t0, math.log(p1 / p0)) for (t0, p0), (t1, p1) in zip(pts, pts[1:]) if t1 > t0]
    if len(steps) < 2:
        raise ValueError("小时 K 线不足 2 根")
    span = sum(s for s, _ in steps)
    mu = sum(r for _, r in steps) / span
    var = sum((r - mu * s) ** 2 / s for s, r in steps) / (len(steps) - 1) * 365 * 86400
    return math.sqrt(var), span / 3600, round(span / 3600) - len(steps)


def sampled_sigma(samples: list) -> tuple[float, float] | None:
    """(annualised σ, hours covered) from [[epoch s, price], ...]; gaps over an hour are skipped; None under 12 h."""
    squares, seconds = sampled_coverage(samples)
    if seconds < 12 * 3600:
        return None
    return math.sqrt(squares / (seconds / (365 * 86400))), seconds / 3600


def usd_short(value: D | float | None) -> str:
    """83_200_000 -> $83.2M; 1_000_000_000 -> $1B."""
    if value is None:
        return "—"
    v = float(value)
    for size, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= size:
            text = f"{v / size:.3g}" if v / size < 100 else f"{v / size:.0f}"
            return f"${text}{unit}"
    return f"${v:,.2f}"


def dex_price(data: Any, token: str) -> tuple[D, D | None, D | None, str]:
    """DexScreener answer ([pairs], {"pairs": [...]} or {"pair": {...}}) -> (USD price, market cap, FDV, pair label)
    from the most liquid pair that has the token as its base."""
    if isinstance(data, list):
        pairs = data
    elif isinstance(data, dict):
        pairs = data.get("pairs") or ([data["pair"]] if isinstance(data.get("pair"), dict) else [])
    else:
        pairs = []
    best = None
    number_or_none = lambda value: D(str(value)) if value not in (None, "", 0) else None
    for pair in pairs or []:
        if not isinstance(pair, dict) or str((pair.get("baseToken") or {}).get("address", "")).lower() != token.lower():
            continue
        try:
            price = D(str(pair.get("priceUsd")))
            liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
        except (decimal.InvalidOperation, TypeError, ValueError):
            continue
        if price > 0 and (best is None or liquidity > best[0]):
            cap = fdv = None
            with contextlib.suppress(decimal.InvalidOperation, TypeError, ValueError):
                cap, fdv = number_or_none(pair.get("marketCap")), number_or_none(pair.get("fdv"))
            best = (liquidity, price, cap or fdv, fdv or cap, f"{pair.get('dexId', '')}")
    if best is None:
        raise ValueError("DexScreener 没有这个代币的交易对")
    return best[1], best[2], best[3], best[4]


def gecko_pool(data: Any, prefer: str = "") -> str:
    """GeckoTerminal token-pools answer -> the address of the pool ``prefer`` names (matched without regard to case: a
    rules page may spell a Solana address in lower case), else of the pool with the most liquidity."""
    rows = (data or {}).get("data") if isinstance(data, dict) else None
    best = None
    for row in rows or []:
        attrs = (row or {}).get("attributes") or {}
        if prefer and str(attrs.get("address") or "").lower() == prefer.lower():
            return str(attrs["address"])
        try:
            reserve = float(attrs.get("reserve_in_usd") or 0)
        except (TypeError, ValueError):
            reserve = 0.0
        if attrs.get("address") and (best is None or reserve > best[0]):
            best = (reserve, str(attrs["address"]))  # Solana addresses are case-sensitive
    if best is None:
        raise ValueError("GeckoTerminal 没有这个代币的池子")
    return best[1]


def top_high(hist: dict) -> None:
    """A cap record's "high"/"at": the higher of the bars' high and the bot's own samples' high. A record from an older
    build (one "high" for both) keeps that high as the bars' until the pool changes."""
    if "bar_high" not in hist:
        hist["bar_high"], hist["bar_at"] = float(hist.get("high") or 0), int(hist.get("at") or 0)
    hist["high"], hist["at"] = max((float(hist.get("bar_high") or 0), int(hist.get("bar_at") or 0)),
                                   (float(hist.get("seen_high") or 0), int(hist.get("seen_at") or 0)))


def gecko_bars(data: Any) -> list[tuple[int, float, float, float, float]]:
    """GeckoTerminal OHLCV answer -> [(open time s, open, high, low, close)] oldest first."""
    rows = (((data or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") if isinstance(data, dict) else None
    out = []
    for row in rows or []:
        with contextlib.suppress(TypeError, ValueError, IndexError):
            out.append((int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4])))
    return sorted(set(out))


def rpc_uint(data: Any) -> int:
    result = (data or {}).get("result") if isinstance(data, dict) else None
    if not isinstance(result, str) or not result.startswith("0x"):
        raise ValueError(f"BSC 节点返回异常：{clean_error(json.dumps((data or {}).get('error', data), ensure_ascii=False))[:80]}")
    return int(result, 16) if len(result) > 2 else 0


class CapMarket:
    """A token's market cap / FDV = price × supply (BSC: total − burned via RPC; else DexScreener's FDV ÷ price),
    its 30-day σ, and its highest point since the resolution window opened (GeckoTerminal hourly bars, the
    window's first partial hour from 1-minute bars, plus every price the bot itself sees).

    Settlement reads the rules' own chart (Flap.sh, pump.fun, DexScreener); these feeds are a close stand-in."""
    PRICE_SECONDS = 30
    FAST_PRICE_SECONDS = 10  # without bars the bot's own samples are the only record of the window: sample three times as often
    GAP_S = 120           # without bars, an unobserved stretch of the window longer than this is recorded (a touch inside it is unknown)
    GAPS_KEEP = 40        # the longest such stretches kept per window
    SAMPLE_GAP_S = 900    # rebuilding the coverage from the 5-minute samples: a break longer than this between them is a gap
    SEEN_WRITE_S = 60     # the "last sampled" mark is persisted this often (a restart then sees at most a minute too much gap)
    SUPPLY_SECONDS = 600
    VOL_SECONDS = 3600
    SCAN_SECONDS = 300
    RETRY_SECONDS = 300   # after a failed σ request (GeckoTerminal allows ~30 calls a minute): wait, never hammer
    SIGMA_KEEP_MS = 24 * 3600_000  # a saved σ stands in for this long while fresh bars cannot be fetched
    SAMPLE_MS = 300_000   # own price samples, for σ where no bars are served (PONS) or while they fail
    GECKO_GAP = 2.5       # seconds between GeckoTerminal requests across all ladders (it allows ~30 a minute)
    _gecko_lock: asyncio.Lock | None = None
    _gecko_loop: Any = None
    _gecko_last = 0.0

    def __init__(self, store: "Store", spec: CapSpec):
        self.store, self.spec = store, spec
        self.price: D | None = None
        self.dex_cap: D | None = None
        self.dex_fdv: D | None = None
        self.source = ""
        self.supply: D | None = None      # total − burned, in whole tokens
        self.sigma: float | None = None
        self.sigma_note = ""
        self.pool = ""
        self.hour_high: float = 0.0       # the running hour's high (not yet in the persisted scan)
        self.sigma_kind = ""              # "bars" | "saved" | "samples" | "prior"
        self.vol_error = ""               # why hourly bars could not be read (shown with a prior σ)
        self.sampled_ms = 0
        self.priced_ms = 0                # when the price was last read successfully
        self.seen_written_s = 0           # when the "last sampled" mark was last persisted
        self.error = ""
        self.times = {"price": -1e9, "supply": -1e9, "vol": -1e9, "scan": -1e9}

    @property
    def price_seconds(self) -> int:
        """How often the price is read: every 10 s where the bot's own samples are the window's only record."""
        return self.PRICE_SECONDS if self.spec.gecko else self.FAST_PRICE_SECONDS

    PRICE_STALE_MS = 5 * 60_000  # a price older than this prices nothing (only a level already reached stays settled)

    def input_problem(self, now_ms: int) -> str:
        """Why the live inputs cannot price new probabilities now ("" when they can)."""
        if self.price is not None and now_ms - self.priced_ms > self.PRICE_STALE_MS:
            why = f"：{brief_error(self.error, 60)}" if self.error else ""
            return (f"价格停在 {quote_time(self.priced_ms)}（{(now_ms - self.priced_ms) // 60_000} 分钟未更新{why}），"
                    "暂停概率与建议")
        return ""

    async def gecko_turn(self) -> None:
        """Space GeckoTerminal requests out (shared by every ladder) so a burst never earns a 429."""
        cls, loop = CapMarket, asyncio.get_running_loop()
        if cls._gecko_lock is None or cls._gecko_loop is not loop:
            cls._gecko_lock, cls._gecko_loop = asyncio.Lock(), loop
        async with cls._gecko_lock:
            wait = cls._gecko_last + self.GECKO_GAP - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            cls._gecko_last = time.monotonic()

    async def get(self, url: str, payload: dict | None = None) -> Any:
        if "geckoterminal" in url:
            await self.gecko_turn()
        if payload is None:
            raw = await fetch_source(url, {"Accept": "application/json"})
        else:
            raw = await _blocking(_http_get, url, payload, SOURCE_TIMEOUT, {"User-Agent": BROWSER_UA})
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise RemoteError("接口未返回有效 JSON") from None

    async def rpc(self, data: str) -> int:
        failures = []
        for url in BSC_RPC:
            try:
                return rpc_uint(await self.get(url, {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                                     "params": [{"to": self.spec.token, "data": data}, "latest"]}))
            except Exception as error:
                failures.append(f"{urllib.parse.urlsplit(url).hostname}: {clean_error(error)}")
        raise RemoteError("；".join(failures))

    async def resolve_pool(self) -> str:
        """The GeckoTerminal pool the bars come from: the one the spec names; else the rules' own pair when GeckoTerminal
        lists it (so the bars come from the pool the market settles on); else the token's most liquid pool."""
        if not self.pool:
            base = f"{GECKO}/{self.spec.gecko}"
            self.pool = self.spec.gecko_pool or gecko_pool(await self.get(f"{base}/tokens/{self.spec.token}/pools?page=1"), self.spec.pair)
        return self.pool

    async def ohlcv(self, frame: str, before_s: int, limit: int) -> list[tuple[int, float, float, float, float]]:
        base = f"{GECKO}/{self.spec.gecko}"
        await self.resolve_pool()
        return gecko_bars(await self.get(f"{base}/pools/{self.pool}/ohlcv/{frame}?aggregate=1&limit={limit}"
                                         f"&before_timestamp={before_s}&currency=usd&token={self.spec.token}"))

    def fallback_sigma(self, now_ms: int) -> None:
        """Without fresh hourly bars: the σ saved from the last good fetch (a day at most), else σ measured
        from the bot's own 5-minute price samples (12 hours at least), else the spec's prior."""
        saved = self.store.get(f"capsigma:{self.spec.slug}")
        with contextlib.suppress(TypeError, ValueError, IndexError):
            if now_ms - int(saved[2]) < self.SIGMA_KEEP_MS:
                self.sigma, self.sigma_kind = float(saved[0]), "saved"
                self.sigma_note = f"{saved[1]}，{stamp(int(saved[2]), seconds=False)} 保存"
                return
        samples = self.store.get(f"capsamples:{self.spec.slug}", [])
        measured = sampled_sigma(samples)
        if measured:
            self.sigma, self.sigma_kind = measured[0], "samples"
            self.sigma_note = f"机器人自采 5 分钟价，{measured[1]:.0f} 小时"
            return
        self.sigma, self.sigma_kind = self.spec.prior_sigma, "prior"
        hours = sampled_coverage(samples)[1] / 3600
        self.sigma_note = f"先验；自采价格 {hours:.1f} / 12 小时"

    def sample(self, now_ms: int) -> None:
        """Every 5 minutes, keep the live price (30 days) so σ can be measured where no bars are served."""
        if self.price is None or now_ms - self.sampled_ms < self.SAMPLE_MS:
            return
        self.sampled_ms = now_ms
        rows = [r for r in self.store.get(f"capsamples:{self.spec.slug}", []) if isinstance(r, list) and len(r) == 2
                and now_ms // 1000 - int(r[0]) < 30 * 86400]
        rows.append([now_ms // 1000, float(self.price)])
        self.store.put(f"capsamples:{self.spec.slug}", rows)

    def observe(self, now_ms: int) -> None:
        """Keep the highest price the bot itself has seen inside the window (persisted with the scan). Without bars, also
        keep where the window went unobserved: from its opening to the first sample, and every later stretch longer than
        GAP_S between samples (the bot down, the feed failing). A touch inside those cannot be known."""
        if self.price is None or not self.spec.start_ms <= now_ms <= self.spec.end_ms:
            return
        hist = dict(self.history) or {"start": self.spec.start_ms, "high": 0.0, "at": 0}
        now_s, changed = now_ms // 1000, False
        if float(self.price) > float(hist.get("seen_high") or 0):
            hist["seen_high"], hist["seen_at"], changed = float(self.price), now_s, True
            top_high(hist)
        if not self.spec.gecko:
            if hist.get("coverage_v") != 2:
                self.rebuild_coverage(hist, now_s)  # once: a record older than the coverage marks (or wrongly given the whole window as a gap)
                changed = True
            since = int(hist.get("seen") or 0) or int(hist.get("monitored_from") or now_s)
            if now_s - since > self.GAP_S:
                gaps = [g for g in hist.get("gaps") or [] if isinstance(g, list) and len(g) == 2] + [[since, now_s]]
                hist["gaps"] = sorted(sorted(gaps, key=lambda g: g[1] - g[0])[-self.GAPS_KEEP:])  # the longest kept, in time order
                hist["gap_s"] = int(hist.get("gap_s") or 0) + (now_s - since)
                changed = True
            if changed or now_s - self.seen_written_s >= self.SEEN_WRITE_S:
                hist["seen"], changed, self.seen_written_s = now_s, True, now_s
        if changed:
            self.store.put(f"cap:{self.spec.slug}", hist)

    def rebuild_coverage(self, hist: dict, now_s: int) -> None:
        """What the bot has actually watched of this window, from the 5-minute price samples it keeps for σ (and the
        time of its highest sample): the first sample is when it began monitoring the ladder (monitored_from; the window
        before that was never watched, which is not the same as a sampling gap), and every break longer than
        SAMPLE_GAP_S between samples is a gap. Replaces a record from before the coverage marks existed, which the first
        build of those marks had wrongly given the whole window (opening → its first run) as one gap."""
        start_s = self.spec.start_ms // 1000
        stamps = sorted(int(r[0]) for r in self.store.get(f"capsamples:{self.spec.slug}", [])
                        if isinstance(r, list) and len(r) == 2 and start_s <= int(r[0]) <= now_s)
        first = min([s for s in (stamps[:1] + [int(hist.get("seen_at") or 0), int(hist.get("at") or 0)]) if s >= start_s] or [now_s])
        gaps = [[a, b] for a, b in zip(stamps, stamps[1:]) if b - a > self.SAMPLE_GAP_S]
        if stamps and now_s - stamps[-1] > self.SAMPLE_GAP_S:
            gaps.append([stamps[-1], now_s])
        hist["monitored_from"] = min(first, now_s)
        hist["gaps"] = sorted(sorted(gaps, key=lambda g: g[1] - g[0])[-self.GAPS_KEEP:])
        hist["gap_s"] = sum(b - a for a, b in gaps)
        hist["coverage_v"] = 2

    def spike_note(self) -> str:
        """The window's high came from a wick: an hourly bar whose high is more than double its open and close ("" when
        not). A wick on the bars' pool may be a mispriced fill the rules' own chart never printed: worth checking there."""
        spike = self.history.get("spike")
        if not (isinstance(spike, list) and len(spike) == 3) or float(self.history.get("bar_high") or 0) != float(spike[1]):
            return ""
        return (f"窗口最高来自 {stamp(int(spike[0]) * 1000, seconds=False)} 那一小时的插针（最高 {spike[1]:.6g}，开收盘最高 {spike[2]:.6g} USD）："
                "K 线池子里的一笔异常成交也会留下这样的影子，结算图上是否真有这根请核实")

    def gaps_note(self) -> str:
        """Without bars: what the bot has not watched of the window, in words — the stretch before it began monitoring
        the ladder, and the breaks in its sampling since ("" when it has watched the whole window; "" with bars)."""
        hist = self.history
        if self.spec.gecko or not hist:
            return ""
        start_s = self.spec.start_ms // 1000
        from_s = int(hist.get("monitored_from") or 0)
        gaps = sorted(g for g in hist.get("gaps") or [] if isinstance(g, list) and len(g) == 2)
        span = lambda seconds: f"{seconds / 3600:.1f} 小时" if seconds >= 3600 else f"{max(1, seconds // 60)} 分钟"
        parts = []
        if from_s and from_s - start_s > self.GAP_S:
            parts.append(f"机器人从 {stamp(from_s * 1000, seconds=False)} 起才监控这张卡，开窗后的前 {span(from_s - start_s)}没有任何记录")
        if gaps:
            longest = max(gaps, key=lambda g: g[1] - g[0])
            total = span(int(hist.get("gap_s") or sum(b - a for a, b in gaps)))
            if longest[1] - longest[0] < 600:  # restarts and failed reads: a few minutes each, not worth a timetable
                parts.append(f"监控以来有 {len(gaps)} 次短暂中断（重启或行情接口失败），共 {total}，最长 {span(longest[1] - longest[0])}")
            else:
                parts.append(f"监控以来有 {len(gaps)} 段没有采样，共 {total}（最长 {span(longest[1] - longest[0])}："
                             f"{stamp(longest[0] * 1000, seconds=False)} → {stamp(longest[1] * 1000, seconds=False)}）")
        if not parts:
            return ""
        return "；".join(parts) + "；这些时段碰没碰到档位无法判断（Predict 已结算的档位除外）"

    @property
    def cap(self) -> D | None:
        if self.price is None:
            return None
        return self.price * self.supply if self.supply else self.dex_cap

    @property
    def history(self) -> dict:
        """{'high': USD price, 'at': s, 'through': s (next hour to read), 'first': 'done'|'skipped'} for this window."""
        saved = self.store.get(f"cap:{self.spec.slug}", {})
        return saved if isinstance(saved, dict) and saved.get("start") == self.spec.start_ms else {}

    async def refresh(self, now_ms: int) -> None:
        mono, failures = time.monotonic(), []
        if mono - self.times["price"] >= self.price_seconds:
            self.times["price"] = mono
            try:
                url = (f"https://api.dexscreener.com/latest/dex/pairs/{self.spec.chain}/{self.spec.pair}" if self.spec.pair
                       else f"https://api.dexscreener.com/tokens/v1/{self.spec.chain}/{self.spec.token}")
                self.price, self.dex_cap, self.dex_fdv, self.source = dex_price(await self.get(url), self.spec.token)
                self.source = f"DexScreener {self.source}".strip()
                self.priced_ms = now_ms
                if self.spec.supply == "fdv" and self.dex_fdv:
                    self.supply = self.dex_fdv / self.price
                self.observe(now_ms)
            except Exception as error:
                failures.append(f"价格：{clean_error(error)}")
        if self.spec.supply == "rpc" and (mono - self.times["supply"] >= self.SUPPLY_SECONDS or self.supply is None):
            self.times["supply"] = mono
            try:
                decimals = await self.rpc("0x313ce567")
                total = await self.rpc("0x18160ddd")
                burned = 0
                for address in BURN_ADDRESSES:
                    burned += await self.rpc("0x70a08231" + address[2:].rjust(64, "0"))
                self.supply = D(total - burned) / (D(10) ** decimals)
            except Exception as error:
                failures.append(f"供应量：{clean_error(error)}")
        self.sample(now_ms)
        if self.spec.gecko and mono - self.times["vol"] >= self.VOL_SECONDS:
            self.times["vol"] = mono
            self.pool = ""  # look the most liquid pool up again (a token can move pools)
            try:
                now_s = now_ms // 1000
                # the last 30 days of finished hours (hours without a trade have no bar: a bar count is not a time span)
                bars = [b for b in await self.ohlcv("hour", now_s, 1000) if b[0] + 3600 <= now_s and b[0] >= now_s - 721 * 3600]
                if len(bars) < 49:
                    raise ValueError(f"小时 K 线只有 {len(bars)} 根，不足 2 天")
                self.sigma, hours, missing = bars_sigma(bars)
                self.sigma_note = f"{hours / 24:.0f} 日小时收盘" + (f"（缺 {missing} 根，按实际间隔计）" if missing > 0 else "")
                self.sigma_kind, self.vol_error = "bars", ""
                self.store.put(f"capsigma:{self.spec.slug}", [self.sigma, self.sigma_note, now_ms])
            except Exception as error:
                self.times["vol"] = mono - self.VOL_SECONDS + self.RETRY_SECONDS  # try again in 5 minutes
                self.vol_error = clean_error(error) or type(error).__name__
        if self.sigma_kind != "bars":
            self.fallback_sigma(now_ms)
        if self.spec.gecko and mono - self.times["scan"] >= self.SCAN_SECONDS:
            self.times["scan"] = mono
            try:
                await self.scan(now_ms)
            except Exception as error:
                failures.append(f"窗口最高：{clean_error(error)}")
        self.error = "；".join(failures)

    @property
    def window_end_s(self) -> int:
        """The end of the window's last minute (seconds): the window includes that minute."""
        return (self.spec.end_ms + 60_000) // 1000

    async def scan(self, now_ms: int) -> None:
        """Extend the window's highest price (persisted) with the hours finished since the last scan. An hour counts
        once it is over; only hours that start inside the window count (the last one ends with the window's own last
        minute). Once the window is over and read to its end, "through" reaches window_end_s: the record is complete.
        "bars_from" is the earliest bar the feed has ever served. A scan that meets older bars than that (a short page,
        or a pool with little history, had been served before; a record from an older build has no mark at all) reads the
        whole window again instead of only the hours after "through", so an early high is never left behind."""
        now_s = now_ms // 1000
        start_s, end_s = self.spec.start_ms // 1000, min(now_s, self.window_end_s)
        if end_s <= start_s:
            return
        hist = dict(self.history) or {"start": self.spec.start_ms, "high": 0.0, "at": 0}
        start_hour = start_s - start_s % 3600 + (3600 if start_s % 3600 else 0)
        top_high(hist)  # an older build's record gets its bar_high here
        pool = await self.resolve_pool()
        if hist.get("pool") and hist["pool"] != pool:
            # another pool's bars are not this pool's record: its high and progress go, the window is read again from the opening
            for key in ("through", "first", "bars_from", "spike"):
                hist.pop(key, None)
            hist["bar_high"], hist["bar_at"] = 0.0, 0
        hist["pool"] = pool
        hist.setdefault("through", start_hour)
        if start_s % 3600 and "first" not in hist:
            # the window opens mid-hour: that hour counts only from the opening minute
            try:
                bars = [b for b in await self.ohlcv("minute", start_s - start_s % 3600 + 3600, 60) if b[0] >= start_s]
                hist["first"] = "done" if bars else "skipped"
                for bar in bars:
                    if bar[2] > hist["bar_high"]:
                        hist["bar_high"], hist["bar_at"] = bar[2], bar[0]
            except Exception:
                hist["first"] = "skipped"
        rows: list[tuple[int, float, float, float, float]] = []
        before, floor, known = end_s, int(hist["through"]), int(hist.get("bars_from") or 1 << 62)
        for _ in range(10):  # 1000 hours per page, newest first
            page = await self.ohlcv("hour", before, 1000)
            rows += page
            if page and page[0][0] < known:
                floor = start_hour  # older bars than ever seen: read the window from its opening again
            if not page or page[0][0] <= floor or len(page) < 1000:
                break
            before = page[0][0]
        if rows and min(b[0] for b in rows) < known:
            hist["bars_from"] = min(b[0] for b in rows)
        inside = [b for b in rows if b[0] >= floor and b[0] < self.window_end_s]
        finished = [b for b in inside if b[0] + 3600 <= now_s]
        running = [b for b in inside if b[0] + 3600 > now_s]
        for bar in finished:
            if bar[2] > hist["bar_high"]:
                hist["bar_high"], hist["bar_at"] = bar[2], bar[0]
                body = max(bar[1], bar[4])
                if body > 0 and bar[2] > 2 * body:  # a wick more than double the bar's open and close: remembered, shown
                    hist["spike"] = [bar[0], bar[2], body]
        if finished:
            hist["through"] = max(b[0] for b in finished) + 3600
        if now_s >= self.window_end_s and not running:
            hist["through"] = max(hist["through"], self.window_end_s)  # hours without a bar had no trades
        top_high(hist)
        self.hour_high = max((b[2] for b in running), default=0.0)
        self.store.put(f"cap:{self.spec.slug}", hist)

    def pool_note(self) -> str:
        """Where the bars come from: the rules' own pair, or the most liquid pool of the token ("" without bars)."""
        if not self.spec.gecko or not self.pool:
            return ""
        short = f"{self.pool[:6]}…{self.pool[-4:]}"
        if self.spec.gecko_pool and self.pool == self.spec.gecko_pool or self.spec.pair and self.pool.lower() == self.spec.pair.lower():
            return f"规则交易对的池子 {short}"
        return f"最活跃的池子 {short}" + ("（GeckoTerminal 没列出规则交易对）" if self.spec.pair else "")

    def backfill_note(self, now_ms: int) -> str:
        """A bars card: what the window's high may still be missing ("" once every finished hour since the opening is
        in, when the card may call it 窗口最高 rather than 已观测最高). Without bars the sampling gaps say it instead."""
        if not self.spec.gecko:
            return ""
        hist, now_s, start_s = self.history, now_ms // 1000, self.spec.start_ms // 1000
        if now_s <= start_s:
            return ""
        through = int(hist.get("through") or 0)
        if not through:
            return "历史 K 线尚未回填（启动后约 5 分钟内读取）"
        parts = []
        bars_from = int(hist.get("bars_from") or 0)
        if bars_from and bars_from >= start_s + 3600:
            parts.append(f"K 线最早到 {stamp(bars_from * 1000, seconds=False)}，开窗到那时的 {(bars_from - start_s) / 3600:.0f} 小时没有记录")
        last_done = min(now_s - now_s % 3600, self.window_end_s)  # the latest hour that has finished
        if through < last_done and now_s - last_done > 2 * self.SCAN_SECONDS:
            parts.append(f"已核验到 {stamp(through * 1000, seconds=False)}，之后 {(last_done - through) / 3600:.0f} 小时尚未读取")
        return "；".join(parts)

    def coverage(self) -> str:
        """Why the window's history is not complete yet ("" once every hour of it has been read)."""
        hist = self.history
        if not self.spec.gecko:
            return "没有 K 线来源：窗口最高只含机器人看到的价格"
        if self.spec.start_ms // 1000 % 3600 and hist.get("first") == "skipped":
            return "开窗那一小时的分钟 K 未取得"  # permanent: waiting does not fill it
        if int(hist.get("through") or 0) < self.window_end_s or (self.spec.start_ms // 1000 % 3600 and hist.get("first") != "done"):
            return "窗口尚未核验到截止"
        return ""

    def window_high(self) -> tuple[D | None, int]:
        """Highest market cap in the window so far (persisted hours, the running hour, the live price). Only what falls
        inside the window counts: a price read after its last minute never does."""
        hist = self.history
        prices = [(float(hist.get("high") or 0), int(hist.get("at") or 0)), (self.hour_high, 0)]
        if self.price is not None and self.spec.start_ms <= self.priced_ms < self.spec.end_ms + 60_000:
            prices.append((float(self.price), 0))
        top, at = max(prices)
        supply = self.supply or (self.dex_cap / self.price if self.dex_cap and self.price else None)
        return (D(str(top)) * supply if top and supply else None), at

    def model_swing(self, target: D, now_ms: int, fair: float | None) -> float:
        """How far P(hit ``target``) moves with σ ×/÷ 1.25, or with a driftless log price instead of a driftless price
        (each varied alone): an edge inside that is the model's own error. At σ of 300% a year the drift convention
        alone is worth several times the σ variation."""
        if fair is None or fair >= 1.0 or self.cap is None or self.sigma is None:
            return 0.0
        years = max(0.0, (self.spec.end_ms - max(now_ms, self.spec.start_ms)) / YEAR_MS)
        variants = [(self.sigma * MODEL_SIGMA_ERROR, -0.5), (self.sigma / MODEL_SIGMA_ERROR, -0.5), (self.sigma, LOG_DRIFT_VARIANT)]
        return max(abs(hit_probability(float(self.cap), float(target), sigma, years, drift) - fair) for sigma, drift in variants)

    def probability(self, target: D, now_ms: int) -> float | None:
        cap, (high, _) = self.cap, self.window_high()
        if high is not None and high >= target:
            return 1.0
        if now_ms >= self.spec.end_ms + 60_000:
            return 0.0  # the window is over: only what happened inside it counts, never today's market cap
        if cap is None or self.sigma is None or self.input_problem(now_ms):
            return None
        years = max(0.0, (self.spec.end_ms - max(now_ms, self.spec.start_ms)) / YEAR_MS)
        return hit_probability(float(cap), float(target), self.sigma, years)


# --- price ladders ("what price will Bitcoin hit in October?": one Yes/No market per level) ---------------------------
@dataclass(frozen=True)
class RangeSpec:
    """A "what price will X hit in <month>" category: one Yes/No market per price level, Yes as soon as any Binance
    1-minute candle of the window has a High at or above an upward level (a Low at or below a downward one). Each market's
    own rules (or its title) say which; the rules for this category's levels name the default."""
    key: str          # item / book key and the card's favourite key
    slug: str
    symbol: str       # the Binance pair the rules name
    venue: str        # "spot" (BTC/USDT, ETH/USDT, SOL/USDT) or "futures" (HYPEUSDT, a USDⓈ-M perpetual)
    name: str         # card title
    start_ms: int     # open of the first candle: 00:00 ET on the 1st
    end_ms: int       # open of the last candle: 23:59 ET on the last day
    default_dir: str = "up"  # the direction of the rules the category was added with ("up": High ≥, "down": Low ≤)

    def label(self, ms: int) -> str:
        et = dt.datetime.fromtimestamp(ms / 1000, dt.timezone(dt.timedelta(hours=us_eastern_offset(ms))))
        return f"{et:%m-%d %H:%M} ET（北京 {stamp(ms, seconds=False)}）"


RANGE_MARKETS = (
    # "from 00:00 AM ET on the first day to 11:59 PM ET on the last" (October: EDT throughout); Binance 1m candles
    RangeSpec("BTC-HIT-10", "what-price-will-bitcoin-hit-in-october-2026", "BTCUSDT", "spot", "BTC 10月价格",
              et_ms(2026, 10, 1, 0, 0, -4), et_ms(2026, 10, 31, 23, 59, -4), "down"),
    RangeSpec("ETH-HIT-10", "what-price-will-ethereum-hit-in-october-2026", "ETHUSDT", "spot", "ETH 10月价格",
              et_ms(2026, 10, 1, 0, 0, -4), et_ms(2026, 10, 31, 23, 59, -4)),
    RangeSpec("SOL-HIT-10", "what-price-will-solana-hit-in-october-2026", "SOLUSDT", "spot", "SOL 10月价格",
              et_ms(2026, 10, 1, 0, 0, -4), et_ms(2026, 10, 31, 23, 59, -4)),
    # HYPEUSDT on Binance futures (the rules link binance.com/en/futures/HYPEUSDT)
    RangeSpec("HYPE-HIT-10", "what-price-will-hyperliquid-hit-in-october-2026", "HYPEUSDT", "futures", "HYPE 10月价格",
              et_ms(2026, 10, 1, 0, 0, -4), et_ms(2026, 10, 31, 23, 59, -4)),
)


# --- a US stock's "hits $X by <date>" market: TradingView 1-minute candles of the regular session, read from Yahoo -----------
STOCK_HIT_MARKETS = (
    # "any TradingView 1 minute candle for STRC between market creation and the listed date, 11:59 PM ET, has a final
    # High of at least $100" (NASDAQ:STRC). The window is read from Predict: the market's createdAt and the date its title
    # names (start_ms / end_ms = 0 here); LADDER_DEADLINES=STRC-100=2026-12-31 pins the deadline by hand.
    RangeSpec("STRC-100", "strc-hits-100-by-20260618001620693", "STRC", "nasdaq", "STRC 触及 $100", 0, 0),
)
YAHOO_CHART = ("https://query1.finance.yahoo.com/v8/finance/chart/", "https://query2.finance.yahoo.com/v8/finance/chart/")
# NYSE / Nasdaq closures and 13:00 ET early closes, 2026–2027 (weekends are never sessions)
US_MARKET_HOLIDAYS = frozenset({
    dt.date(2026, 1, 1), dt.date(2026, 1, 19), dt.date(2026, 2, 16), dt.date(2026, 4, 3), dt.date(2026, 5, 25), dt.date(2026, 6, 19),
    dt.date(2026, 7, 3), dt.date(2026, 9, 7), dt.date(2026, 11, 26), dt.date(2026, 12, 25),
    dt.date(2027, 1, 1), dt.date(2027, 1, 18), dt.date(2027, 2, 15), dt.date(2027, 3, 26), dt.date(2027, 5, 31), dt.date(2027, 6, 18),
    dt.date(2027, 7, 5), dt.date(2027, 9, 6), dt.date(2027, 11, 25), dt.date(2027, 12, 24)})
US_HALF_DAYS = frozenset({dt.date(2026, 11, 27), dt.date(2026, 12, 24), dt.date(2027, 11, 26)})
US_SESSION_MS = 390 * 60_000  # 09:30–16:00 ET
US_TRADING_DAYS = 252
MONTH_NAMES = {name: i + 1 for i, name in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}


def et_date(ms: int) -> dt.date:
    """The US Eastern calendar date of an instant."""
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone(dt.timedelta(hours=us_eastern_offset(ms)))).date()


def et_wall_ms(day: dt.date, hour: int, minute: int) -> int:
    """US Eastern wall time on a date -> epoch ms, with that date's own offset (DST switches at 02:00; 17:00 UTC is the
    same civil day either side of the switch)."""
    probe = int(dt.datetime(day.year, day.month, day.day, 17, tzinfo=dt.timezone.utc).timestamp() * 1000)
    return et_ms(day.year, day.month, day.day, hour, minute, us_eastern_offset(probe))


def us_session(day: dt.date) -> tuple[int, int] | None:
    """(open, close) in ms of the regular session on a date: 09:30–16:00 ET, 13:00 on an early close; None when closed."""
    if day.weekday() >= 5 or day in US_MARKET_HOLIDAYS:
        return None
    return et_wall_ms(day, 9, 30), et_wall_ms(day, 13 if day in US_HALF_DAYS else 16, 0)


def us_session_state(now_ms: int) -> tuple[str, int]:
    """("交易中" | "已收盘", when the next session opens; 0 while one is running)."""
    day = et_date(now_ms)
    for i in range(14):
        session = us_session(day + dt.timedelta(days=i))
        if session and session[0] <= now_ms < session[1]:
            return "交易中", 0
        if session and session[0] > now_ms:
            return "已收盘", session[0]
    return "已收盘", 0


def us_trading_years(now_ms: int, end_ms: int, intraday: float = 1.0) -> float:
    """Trading time left before end_ms, in years of US_TRADING_DAYS full sessions: the rest of the running session and every
    later one through the deadline's date (an early close is its share of a full day). Nothing can be hit between sessions.
    ``intraday``: the share of a day's close-to-close variance that happens inside the session (intraday_share). The
    running session's remainder is worth only that share: its opening gap has already happened, while every later
    session still has its own gap ahead and counts whole."""
    if end_ms <= now_ms:
        return 0.0
    total, day, last = 0.0, et_date(now_ms), et_date(end_ms)
    while day <= last:
        session = us_session(day)
        if session:
            left = max(0, min(session[1], end_ms) - max(session[0], now_ms)) / US_SESSION_MS
            total += left * (intraday if session[0] <= now_ms < session[1] else 1.0)
        day += dt.timedelta(days=1)
    return total / US_TRADING_DAYS


def deadline_from_text(texts: list[str], after_ms: int) -> int:
    """The deadline a market's title names, as 23:59 ET of that date: "by December 31", "by Dec. 31, 2026", "by (the end
    of) December", "截止于 12 月 31 日", "2026-12-31". A date without a year is the first such date on or after after_ms (the
    market's creation). The first text that names one wins; 0 when none does."""
    since = et_date(after_ms) if after_ms else dt.date.today()
    # whole month words only: "before MARket close" and "by DECision of the committee" name no month
    month = (r"(january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|august|aug|september|sept|sep|"
             r"october|oct|november|nov|december|dec)\b\.?")
    patterns = [
        (r"(20\d\d)-(\d\d)-(\d\d)", lambda g: (int(g[0]), int(g[1]), int(g[2]))),
        (r"(20\d\d)\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", lambda g: (int(g[0]), int(g[1]), int(g[2]))),
        (r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", lambda g: (0, int(g[0]), int(g[1]))),
        (r"(?i)\b(?:by|before|until|through)\s+(?:the\s+)?(?:end\s+of\s+)?" + month + r"\s*(\d{1,2})?(?:st|nd|rd|th)?,?\s*(20\d\d)?",
         lambda g: (int(g[2]) if g[2] else 0, MONTH_NAMES[g[0].lower()[:3]], int(g[1]) if g[1] else 0)),
        (r"(?i)\b" + month + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(20\d\d)?",
         lambda g: (int(g[2]) if g[2] else 0, MONTH_NAMES[g[0].lower()[:3]], int(g[1]))),
    ]
    for text in texts:
        for pattern, pick in patterns:
            found = re.search(pattern, str(text or ""))
            if not found:
                continue
            try:
                year, mon, day = pick(found.groups())
                if not day:  # "by December": the month's last day
                    nxt = dt.date(year or since.year, mon, 1) + dt.timedelta(days=32)
                    day = (nxt.replace(day=1) - dt.timedelta(days=1)).day
                date = dt.date(year or since.year, mon, day)
                if not year and date < since:
                    date = dt.date(since.year + 1, mon, day)
            except ValueError:
                continue
            return et_wall_ms(date, 23, 59)
    return 0


def parse_yahoo_chart(raw: bytes) -> tuple[dict, list[tuple[int, float | None, float | None, float | None, float | None]]]:
    """Yahoo v8 chart -> ({"price": regularMarketPrice, "time_ms": its trade time, "offset": gmtoffset},
    bars (open ms, open, high, low, close) oldest first, those with no close dropped). A chart error is a ValueError."""
    try:
        chart = json.loads(raw)["chart"]
        if chart.get("error"):
            raise ValueError(str((chart["error"] or {}).get("description") or chart["error"])[:80])
        result = chart["result"][0]
        meta = result["meta"]
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
        stamps = result.get("timestamp") or []
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise ValueError(f"Yahoo 行情返回格式异常：{clean_error(error)[:60]}" if str(error) else "Yahoo 行情返回格式异常") from None
    price, when = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    out = {"price": float(price) if isinstance(price, (int, float)) else None,
           "time_ms": int(when) * 1000 if isinstance(when, (int, float)) else 0, "offset": int(meta.get("gmtoffset") or 0)}
    col = lambda key: quote.get(key) or []
    bars = []
    for i, ts in enumerate(stamps):
        values = [c[i] if i < len(c) and isinstance(c[i], (int, float)) else None for c in (col("open"), col("high"), col("low"), col("close"))]
        if values[3] is None:
            continue
        bars.append((int(ts) * 1000, *values))
    return out, sorted(bars)


def price_level(title: str) -> D | None:
    """The price a ladder market's title names: '↑ 130,000' / '$4.5K' / '↓ $100k' / 'Will Solana reach $250?' -> the
    number; a year next to a month name is not a price. None when there is none."""
    text = re.sub(r"(?i)\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?:\d{1,2},?\s+)?20\d\d\b", " ", str(title))
    text = re.sub(r"\b20\d\d\b(?![,.]?\d)", " ", text) if re.search(r"[$↑↓▲▼]|\d\s*[kK]\b", text) else text
    found = [(m.group(1), m.group(2), m.start()) for m in re.finditer(r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*([kKmM]?)(?![\w.])", text)]
    if not found:
        return None
    marked = [f for f in found if f[1] or re.search(r"[$↑↓▲▼]\s*$", text[:f[2]])]
    number, unit, _ = (marked or found)[0]
    value = D(number.replace(",", ""))
    return value * {"": D(1), "k": D(1000), "m": D(10) ** 6}[unit.lower()] if value > 0 else None


def hit_level_parser(slug: str) -> Any:
    """The level parser of a "<stock> hits $X by <date>" category. Predict lists the question once per deadline, and such
    a market's title may name the date alone ("December 31"): a bare day number is not a price. A "$"-marked price in the
    title (or "$100" in the question) is the level; else the one the category's slug names (strc-hits-100-by-...)."""
    named = re.search(r"-hits?-(\d+(?:\.\d+)?)-by-", slug)
    base = D(named.group(1)) if named else None

    def parse(title: str) -> D | None:
        found = re.search(r"\$\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*([kK]?)(?![\w.])", str(title or ""))
        if found:
            value = D(found.group(1).replace(",", "")) * (1000 if found.group(2) else 1)
            return value if value > 0 else base
        return base
    return parse


def level_direction(rules: str, *titles: str) -> tuple[str, str]:
    """('up' | 'down' | '', where it was read): a market's own rules (High ≥ / Low ≤; rules naming both say nothing about
    this one market), else an arrow in its title or question, else a word there (reach / dip); '' when nothing says. An
    arrow against the rules (the rules text may be the whole category's) is returned as the arrow says, marked "冲突"."""
    text = re.sub(r"\s+", " ", re.sub(r"[\"'“”‘’*]", "", str(rules))).lower()
    down = re.search(r"\blow(?: price)? (?:is )?(?:equal to or (?:lower|less|below)|at or below|<=|≤)|\bfinal low\b", text)
    up = re.search(r"\bhigh(?: price)? (?:is )?(?:equal to or (?:greater|higher|above)|at or above|>=|≥)|\bfinal high\b", text)
    by_rules = ("down" if down else "up") if bool(down) != bool(up) else ""
    arrow = next((d for t in titles for d, mark in (("down", r"[↓▼]"), ("up", r"[↑▲]")) if re.search(mark, str(t))), "")
    if by_rules and arrow and arrow != by_rules:
        return arrow, "冲突"
    if by_rules:
        return by_rules, "规则"
    if arrow:
        return arrow, "标题"
    for title in titles:
        t = str(title).lower()
        if re.search(r"\bdips?\b|\bdrops?\b|\bfalls?\b|\bbelow\b|跌", t):
            return "down", "标题"
        if re.search(r"\breach(?:es)?\b|\babove\b|\brises?\b|\bhigh(?:er)?\b|涨", t):
            return "up", "标题"
    return "", ""


# a level whose market does not say ↑ or ↓ (or says both ways): the card's note, and the short reason the paper trader keeps
RANGE_GUESS = {"推断": "这个市场的规则和标题都没写明上破还是下破，按档位在月初价格之上（↑）还是之下（↓）推断；只作参考",
               "默认": "这个市场的规则和标题都没写明上破还是下破，方向按本类规则默认；只作参考",
               "冲突": "标题的箭头和规则写的方向相反，按标题显示；请到 Predict 核实后再看"}
RANGE_GUESS_SHORT = {"推断": "方向是推断的", "默认": "方向按本类规则默认", "冲突": "标题与规则的方向相反"}
# Price-ladder maker suggestions and alerts require a currently active reward period and a usable two-sided book.
# Paper trading remains taker-only: a profitable resting quote is not evidence of an actual fill.
RANGE_SIM_MAKERS = False


def predict_reward_status(meta: dict, now_ms: int) -> dict:
    """Current LP points from Predict's documented rewards.current, never from boosts or a future schedule.
    Malformed / missing metadata is unknown, rather than assumed active; period bounds are timezone-aware and
    start-inclusive / end-exclusive. A positive rate alone is insufficient to establish activation."""
    out = {"points_active": None, "points_note": "积分状态暂缺", "points_rate": None, "points_end_ms": None}
    rewards = meta.get("rewards")
    if not isinstance(rewards, dict) or "current" not in rewards:
        return out
    current = rewards["current"]
    if current is None:
        return {**out, "points_active": False, "points_note": "积分未激活"}
    if not isinstance(current, dict):
        return out
    try:
        rate = D(str(current.get("hourlyRate")))
        start = dt.datetime.fromisoformat(str(current.get("startsAt")).replace("Z", "+00:00"))
        end = dt.datetime.fromisoformat(str(current.get("endsAt")).replace("Z", "+00:00"))
        if not rate.is_finite() or rate < 0 or start.utcoffset() is None or end.utcoffset() is None or end <= start:
            return out
        start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        rate_float = float(rate)
        if not math.isfinite(rate_float):
            return out
    except (ValueError, TypeError, OverflowError, decimal.InvalidOperation):
        return out
    active = rate > 0 and start_ms <= now_ms < end_ms
    note = "积分已激活" if active else "积分未开始" if now_ms < start_ms else "积分已结束" if now_ms >= end_ms else "积分未激活"
    return {"points_active": active, "points_note": note, "points_rate": rate_float, "points_end_ms": end_ms}


def low_probability(spot: float, level: float, sigma: float, years: float, drift: float = -0.5) -> float:
    """P(the running minimum reaches ``level`` (below ``spot``) before ``years``) for a GBM whose log drift is drift·σ²
    (the mirror of hit_probability: the log price's drift changes sign): Φ((−a − bT)/s) + e^{−2ba/σ²}·Φ((−a + bT)/s),
    a = ln(S/L), b = drift·σ², s = σ√T. With drift −½ this is Φ((−a + s²/2)/s) + (S/L)·Φ((−a − s²/2)/s)."""
    if spot <= level:
        return 1.0
    if years <= 0 or sigma <= 0:
        return 0.0
    a, s = math.log(spot / level), sigma * math.sqrt(years)
    b = drift * sigma * sigma
    return min(1.0, norm_cdf((-a - b * years) / s) + math.exp(-2 * b * a / (sigma * sigma)) * norm_cdf((-a + b * years) / s))


def level_label(value: D | float) -> str:
    """130000 -> '$130k', 4500 -> '$4.5k', 250 -> '$250', 62.5 -> '$62.5'."""
    v = float(value)
    return f"${v / 1000:,.4g}k" if v >= 1000 else f"${v:,.6g}"


def book_disputes(book: "PredictBook | None") -> bool:
    """Our data says a level was reached, yet its Yes book still trades under 90¢: the market does not agree. A book
    with no quotes at all (often a market already settled) disputes nothing."""
    if book is None or not (book.bids or book.asks):
        return False
    return max((float(p) for p, _ in (*book.bids[:1], *book.asks[:1])), default=0.0) < 0.9


def book_decided(book: "PredictBook | None") -> str:
    """The market is trading as if its question were already answered: "up" when the Yes side is bid at 90¢ or more,
    "down" when it is offered at 10¢ or less, else "". Against a model that still sees an open question, that is the
    market knowing something (a spike the path check has not reached yet), not a 49¢ edge."""
    if book is None:
        return ""
    if book.bid and float(book.bid[0]) >= 0.9:
        return "up"
    if book.ask and float(book.ask[0]) <= 0.1:
        return "down"
    return ""


async def binance_futures(path: str, **params: Any) -> Any:
    """GET /fapi/v1/<path> from Binance USDⓈ-M futures (HYPEUSDT's resolution source)."""
    query = urllib.parse.urlencode(params)
    data = await http_json(f"https://fapi.binance.com/fapi/v1/{path}" + (f"?{query}" if query else ""))
    if isinstance(data, dict) and isinstance(data.get("code"), int) and data["code"] < 0:
        raise RemoteError(f"币安合约错误 {data['code']}: {clean_error(data.get('msg', '请求失败'))}")
    return data


class RangeMarket:
    """A Binance pair over one month: live price, 30-day σ, and the window's highest High and lowest Low so far. Hourly
    candles carry exactly the extremes of their minutes, and the window runs from one hour boundary to another, so they
    decide every level; the running hour's own candle counts at once (a level reached resolves the market at once)."""
    PRICE_SECONDS = 30
    VOL_SECONDS = 3600
    SCAN_SECONDS = 60
    PRICE_STALE_MS = 5 * 60_000     # a price older than this prices nothing
    SIGMA_STALE_MS = 24 * 3600_000  # σ not re-measured for a day: shown, not advised on
    SCAN_STALE_MS = 15 * 60_000     # the window's extremes must be read this close to now before advice is given

    def __init__(self, store: "Store", spec: RangeSpec, futures: Any = None):
        self.store, self.spec = store, spec
        self.futures = futures or binance_futures  # the bot passes its own futures feed (base URL, rate-limit cooldown)
        self.price: D | None = None
        self.priced_ms = 0
        self.sigma: float | None = None
        self.sigma_ms = 0
        self.running: dict = {}           # the running hour's candle (in the window): {"open", "high", "low"}
        self.scanned_ms = 0               # when the extremes were last read up to now
        self.error = ""
        self.failures = {"price": "", "vol": "", "scan": ""}  # each part's last failure, kept until that part succeeds
        self.times = {"price": -1e9, "vol": -1e9, "scan": -1e9}

    async def get(self, path: str, **params: Any) -> Any:
        return await (self.futures if self.spec.venue == "futures" else binance_spot)(path, **params)

    @property
    def window_end(self) -> int:
        """The end of the window's last minute."""
        return self.end_ms + 60_000

    @property
    def history(self) -> dict:
        """{'start': ms, 'through': ms (the next hour to read), 'open': the window's first price, 'high'/'low': prices,
        'high_at'/'low_at': hour opens}."""
        saved = self.store.get(f"range:{self.spec.slug}", {})
        return saved if isinstance(saved, dict) and saved.get("start") == self.start_ms else {}

    # the window and the card's wording: a stock market (StockRangeMarket) answers these differently
    range_word = "本月"

    @property
    def start_ms(self) -> int:
        return self.spec.start_ms

    @property
    def end_ms(self) -> int:
        return self.spec.end_ms

    def price_stale(self, now_ms: int) -> bool:
        return now_ms - self.priced_ms > self.PRICE_STALE_MS

    def remaining_years(self, now_ms: int) -> float:
        return max(0.0, (self.window_end - max(now_ms, self.start_ms)) / YEAR_MS)

    def source_name(self) -> str:
        return "币安合约" if self.spec.venue == "futures" else "币安现货"

    def venue_name(self) -> str:
        return "币安 USDⓈ-M 合约" if self.spec.venue == "futures" else "币安现货"

    def extremes_source(self) -> str:
        return f"{self.source_name()} {self.spec.symbol} 小时 K（与 1 分钟 K 的最高/最低一致）"

    def extremes_name(self) -> str:
        """The candles the window's extremes are read from, short (the evidence's source column)."""
        return "币安小时 K"

    def sigma_note(self) -> str:
        return "30 日小时收盘"

    def close_label(self) -> str:
        return f"{self.spec.label(self.end_ms)}这根 1 分钟 K 为止"

    def window_label(self) -> str:
        return f"{self.spec.label(self.start_ms)}起"

    def card_extras(self, now_ms: int) -> dict:
        """Extra ladder fields for the card (none for a Binance pair)."""
        return {}

    def missing_note(self, now_ms: int) -> str:
        """Why the card prices nothing now ("" when it does)."""
        if self.price is None or self.sigma is None:
            return f"等待币安行情（{brief_error(self.error, 80)}）" if self.error else "等待币安行情"
        if self.price_stale(now_ms) and now_ms < self.window_end:
            return f"币安价格停在 {stamp(self.priced_ms, seconds=False)}，暂停概率（已触及的档位仍算已触及）"
        return ""

    def learn(self, rows: list, meta: dict) -> None:
        """What Predict's own listing says about the window (nothing for a Binance pair: the rules fix it)."""

    async def refresh(self, now_ms: int) -> None:
        mono, why = time.monotonic(), lambda error: clean_error(error) or type(error).__name__
        if mono - self.times["price"] >= self.PRICE_SECONDS:
            self.times["price"] = mono
            try:
                data = await self.get("ticker/price", symbol=self.spec.symbol)
                self.price, self.priced_ms = number(data["price"], self.spec.symbol), now_ms
                self.failures["price"] = ""
            except Exception as error:
                self.failures["price"] = f"价格：{why(error)}"
        if mono - self.times["vol"] >= self.VOL_SECONDS:
            self.times["vol"] = mono
            try:  # 722: the newest bar is the running hour, which realized_vol drops
                self.sigma = realized_vol(await self.get("klines", symbol=self.spec.symbol, interval="1h", limit=722), now_ms)
                self.sigma_ms = now_ms
                self.failures["vol"] = ""
            except Exception as error:
                self.times["vol"] = mono - self.VOL_SECONDS + (60 if self.sigma is None else 300)  # again in 1 / 5 minutes
                self.failures["vol"] = f"波动率：{why(error)}"
        if mono - self.times["scan"] >= self.SCAN_SECONDS:
            self.times["scan"] = mono
            try:
                await self.scan(now_ms)
                self.failures["scan"] = ""
            except Exception as error:
                self.failures["scan"] = f"区间核验：{why(error)}"
        self.error = "；".join(text for text in self.failures.values() if text)

    async def scan(self, now_ms: int) -> None:
        """Extend the window's extremes (persisted) with the hours finished since the last scan; keep the running hour's
        candle apart. Once the window is over and read to its end, "through" reaches window_end: the record is complete."""
        start, end = self.start_ms, self.window_end
        if now_ms <= start:
            return
        hist = dict(self.history) or {"start": start, "through": start, "high": None, "high_at": 0, "low": None, "low_at": 0}
        running: dict = {}
        for _ in range(50):
            cursor = int(hist["through"])
            if cursor >= end:
                break
            rows = await self.get("klines", symbol=self.spec.symbol, interval="1h", startTime=cursor,
                                  endTime=min(now_ms, end) - 1, limit=1000)
            if not isinstance(rows, list):  # an error object is not "no candles" (that would close the window unread)
                raise RemoteError(f"币安小时 K 格式异常：{str(rows)[:80]}")
            rows = [r for r in rows if isinstance(r, list) and len(r) > 6 and cursor <= int(r[0]) < end]
            for row in rows:
                opened, closed, high, low = int(row[0]), int(row[6]), float(row[2]), float(row[3])
                if opened == start and hist.get("open") is None:
                    hist["open"] = float(row[1])  # the window's first price: which side of it a level sits on
                if closed >= now_ms:  # still running: counts now, persisted once it is over
                    running = {"open": opened, "high": high, "low": low}
                    continue
                if hist["high"] is None or high > hist["high"]:
                    hist["high"], hist["high_at"] = high, opened
                if hist["low"] is None or low < hist["low"]:
                    hist["low"], hist["low_at"] = low, opened
                hist["through"] = closed + 1
            if len(rows) < 1000 or running:
                break
        if now_ms >= end and not running:
            hist["through"] = max(int(hist["through"]), end)  # hours without a candle had no trades
        self.running, self.scanned_ms = running, now_ms
        self.store.put(f"range:{self.spec.slug}", hist)

    def marks(self) -> dict:
        """{'high', 'high_at', 'low', 'low_at'}: the highest High and lowest Low inside the window so far, and the hour
        each was seen in: finished hours, the running hour, the live price (only while it falls inside the window). The
        earliest hour wins a tie."""
        hist, run = self.history, self.running
        highs = [(hist.get("high"), int(hist.get("high_at") or 0)), (run.get("high"), int(run.get("open") or 0))]
        lows = [(hist.get("low"), int(hist.get("low_at") or 0)), (run.get("low"), int(run.get("open") or 0))]
        if self.price is not None and self.start_ms <= self.priced_ms < self.window_end:
            hour = self.priced_ms - self.priced_ms % 3_600_000
            highs.append((float(self.price), hour))
            lows.append((float(self.price), hour))
        high = max((x for x in highs if x[0] is not None), key=lambda x: x[0], default=(None, 0))
        low = min((x for x in lows if x[0] is not None), key=lambda x: x[0], default=(None, 0))
        return {"high": high[0], "high_at": high[1], "low": low[0], "low_at": low[1]}

    def extremes(self) -> tuple[float | None, float | None]:
        """(highest High, lowest Low) inside the window so far (see marks)."""
        marks = self.marks()
        return marks["high"], marks["low"]

    def reached(self, level: D, direction: str) -> bool:
        high, low = self.extremes()
        if direction == "up":
            return high is not None and high >= float(level)
        return low is not None and low <= float(level)

    def probability(self, level: D, direction: str, now_ms: int) -> float | None:
        """P(Yes) for one level: 1 once reached; 0 once the window is over and read to its end without it; else the touch
        model from the live price (zero drift, the 30-day σ). None while the price or σ is missing or stale, or while the
        window's last hours are not read yet."""
        if self.reached(level, direction):
            return 1.0
        if now_ms >= self.window_end:
            return 0.0 if self.complete() else None
        if self.price is None or self.sigma is None or self.price_stale(now_ms):
            return None
        years = self.remaining_years(now_ms)
        touch = hit_probability if direction == "up" else low_probability
        return touch(float(self.price), float(level), self.sigma, years)

    def model_swing(self, level: D, direction: str, now_ms: int, fair: float | None) -> float:
        """How far P(Yes) moves with σ ×/÷ 1.25, or with a driftless log price instead of a driftless price (each varied
        alone): an edge inside that is the model's own error."""
        if fair is None or fair >= 1.0 or self.price is None or self.sigma is None:
            return 0.0
        years = self.remaining_years(now_ms)
        touch = hit_probability if direction == "up" else low_probability
        variants = [(self.sigma * MODEL_SIGMA_ERROR, -0.5), (self.sigma / MODEL_SIGMA_ERROR, -0.5), (self.sigma, LOG_DRIFT_VARIANT)]
        return max(abs(touch(float(self.price), float(level), sigma, years, drift) - fair) for sigma, drift in variants)

    def advice_problem(self, now_ms: int) -> str:
        """Why no level may be suggested now ("" when they may): the window's extremes must be read up to now (a level
        already reached would otherwise look open), σ current, the window still running."""
        if now_ms >= self.window_end:
            return "窗口已结束，等待结算"
        if now_ms > self.start_ms and now_ms - self.scanned_ms > self.SCAN_STALE_MS:
            through = int(self.history.get("through") or 0)
            return f"{self.range_word}最高/最低核验停在 {stamp(through, seconds=False) if through else '开始前'}；暂不给建议"
        if now_ms - self.sigma_ms > self.SIGMA_STALE_MS:
            return "波动率超过一天未更新；暂不给建议"
        return ""

    def complete(self) -> bool:
        """Every hour of the window has been read (so "not reached" is settled)."""
        return int(self.history.get("through") or 0) >= self.window_end

    def reference(self) -> float | None:
        """The price a level is above (↑) or below (↓) when nothing else says which: the window's first price, before
        the window opens the live one."""
        opening = self.history.get("open")
        return float(opening) if opening else float(self.price) if self.price is not None else None


class StockRangeMarket(RangeMarket):
    """A US stock's "hits $X by <date>" market (STOCK_HIT_MARKETS). TradingView's 1-minute High of the regular session is the
    resolution source; Yahoo's chart feed stands in for it (the same session, consolidated prints: a touch by a few cents is
    worth checking on Predict). The window runs from the market's creation (Predict's createdAt) to 23:59 ET of the date
    its title names. Time passes in sessions only: σ is from daily closes, annualised over 252 sessions, and the time left
    is the trading time left; the last trade is the live price and stays valid while the market is closed."""
    PRICE_SECONDS = 30
    CLOSED_PRICE_SECONDS = 120  # between sessions the last trade does not change: a quarter of the requests (still well inside PRICE_STALE_MS)
    CLOSE_PRINT_MS = 15 * 60_000  # the closing print may still be corrected this long after the bell: keep the session cadence
    SCAN_SECONDS = 300          # daily bars: σ and the finished days' extremes from one request
    SIGMA_STALE_MS = 3 * 24 * 3600_000  # a long weekend changes nothing: the daily bars are re-read every 5 minutes anyway
    VOL_DAYS = 60
    range_word = "窗口内"

    def __init__(self, store: "Store", spec: RangeSpec, deadline_override: int = 0):
        super().__init__(store, spec)
        self.deadline_override = deadline_override
        self.level_names: list[str] = []  # "$100": the levels Predict lists, for the question line (set with each payload)
        self.fetched_ms = 0  # when the live price was last read (the trade itself may be hours old while closed)
        self.share: tuple[float, int] | None = None  # (in-session share of the daily variance, days), from the daily bars
        saved = store.get(f"range:{spec.slug}:window", {})
        saved = saved if isinstance(saved, dict) else {}
        self._start, self._end, self.title = int(saved.get("start") or 0), int(saved.get("end") or 0), str(saved.get("title") or "")
        self.failures = {"price": "", "scan": ""}
        self.times = {"price": -1e9, "scan": -1e9}

    @property
    def start_ms(self) -> int:
        return self._start

    @property
    def end_ms(self) -> int:
        return self.deadline_override or self._end

    def known(self) -> bool:
        return bool(self.start_ms and self.end_ms)

    def choose(self, rows: list, meta: dict) -> list:
        """Predict lists this question once per deadline ("June 30", "September 30", "December 31": a market each). The card
        prices one of them: the pinned deadline's (LADDER_DEADLINES), else the latest deadline still ahead, else the latest."""
        if len(rows) <= 1:
            return list(rows)
        now = int(time.time() * 1000)
        dated = []
        for row in rows:
            details = (meta.get(row.market_id) or ({}, 0))[0] if row.market_id else {}
            texts = [row.title, row.question, details.get("question", ""), details.get("rules", "")]
            dated.append((deadline_from_text(texts, int(details.get("created_ms") or 0) or now), row))
        if self.deadline_override:
            pinned = [row for deadline, row in dated if deadline == self.deadline_override]
            if pinned:
                return pinned[:1]
        ahead = [pair for pair in dated if pair[0] >= now]
        return [max(ahead or dated, key=lambda pair: pair[0])[1]]

    def learn(self, rows: list, meta: dict) -> None:
        """The window from Predict's listing: createdAt (the start) and the date the title / question names (the end),
        kept in SQLite so a restart prices at once."""
        for row in rows:
            details = (meta.get(row.market_id) or ({}, 0))[0] if row.market_id else {}
            created = int(details.get("created_ms") or 0)
            deadline = deadline_from_text([row.title, row.question, details.get("question", ""), details.get("rules", "")],
                                          created or int(time.time() * 1000))
            title = row.title or row.question or ""
            start, end, title = created or self._start, deadline or self._end, title or self.title
            if (start, end, title) != (self._start, self._end, self.title):
                self._start, self._end, self.title = start, end, title
                self.store.put(f"range:{self.spec.slug}:window", {"start": start, "end": end, "title": title})
            return

    async def chart(self, **params: Any) -> tuple[dict, list]:
        errors = []
        for host in YAHOO_CHART:
            url = host + urllib.parse.quote(self.spec.symbol) + "?" + urllib.parse.urlencode(params)
            try:
                return parse_yahoo_chart(await fetch_source(url))
            except (RemoteError, TimeoutError, OSError, ValueError) as error:
                errors.append(clean_error(error) or type(error).__name__)
        raise RemoteError("；".join(dict.fromkeys(errors)))

    def price_seconds(self, now_ms: int) -> int:
        """How often the live price is read: every 30 s while the regular session runs (and shortly after its close, while
        the closing print may still be corrected), every 2 minutes while the market is closed and nothing can change."""
        if us_session_state(now_ms)[0] == "交易中":
            return self.PRICE_SECONDS
        session = us_session(et_date(now_ms))
        if session and session[1] <= now_ms < session[1] + self.CLOSE_PRINT_MS:
            return self.PRICE_SECONDS
        return self.CLOSED_PRICE_SECONDS

    async def refresh(self, now_ms: int) -> None:
        mono, why = time.monotonic(), lambda error: clean_error(error) or type(error).__name__
        if mono - self.times["price"] >= self.price_seconds(now_ms):
            self.times["price"] = mono
            try:
                meta, bars = await self.chart(interval="1m", range="1d", includePrePost="false")
                if meta["price"] is None:
                    raise RemoteError("Yahoo 没有返回 regularMarketPrice")
                self.price, self.priced_ms, self.fetched_ms = D(f"{meta['price']:.4f}"), meta["time_ms"] or now_ms, now_ms
                self.running = self.day_candle(bars)
                self.failures["price"] = ""
            except Exception as error:
                self.failures["price"] = f"价格：{why(error)}"
        if mono - self.times["scan"] >= self.SCAN_SECONDS:
            self.times["scan"] = mono
            try:
                _, bars = await self.chart(interval="1d", range="1y")
                self.sigma, self.sigma_ms = self.daily_sigma(bars, now_ms), now_ms
                self.share = self.session_share(bars, now_ms)
                self.scan_days(bars, now_ms)
                self.failures["scan"] = ""
            except Exception as error:
                self.times["scan"] = mono - self.SCAN_SECONDS + 60  # again in a minute
                self.failures["scan"] = f"日 K：{why(error)}"
        self.error = "；".join(text for text in self.failures.values() if text)

    def day_candle(self, bars: list) -> dict:
        """The latest session's extremes from its 1-minute bars, as far as they fall inside the window (the creation minute
        counts); {} when none do. Finished sessions are persisted by scan_days; this one counts at once."""
        if not self.known() or not bars:
            return {}
        day = et_date(bars[-1][0])
        inside = [b for b in bars if et_date(b[0]) == day and b[0] + 60_000 > self.start_ms and b[0] < self.window_end
                  and b[2] is not None and b[3] is not None]
        if not inside:
            return {}
        return {"open": inside[0][0], "high": max(b[2] for b in inside), "low": min(b[3] for b in inside)}

    def daily_sigma(self, bars: list, now_ms: int) -> float:
        """Annualised σ from the last VOL_DAYS close-to-close log returns of finished sessions."""
        today = et_date(now_ms)
        closes = [D(str(b[4])) for b in bars if et_date(b[0]) < today][-(self.VOL_DAYS + 1):]
        sigma, count = realised_vol(closes)
        if count < 10:
            raise RemoteError(f"日 K 只有 {count} 个收益，不够估 σ")
        return sigma * math.sqrt(US_TRADING_DAYS)

    def session_share(self, bars: list, now_ms: int) -> tuple[float, int] | None:
        """The share of the daily variance that happens inside the session, from the same finished days as σ: the close
        model's intraday_share. None when too few days carry an open (the running session then counts whole)."""
        today = et_date(now_ms)
        done = [b for b in bars if et_date(b[0]) < today and b[4] is not None][-(self.VOL_DAYS + 1):]
        opens = [D(str(b[1])) if b[1] is not None and b[1] > 0 else None for b in done]
        return intraday_share(opens, [D(str(b[4])) for b in done])

    def scan_days(self, bars: list, now_ms: int) -> None:
        """The window's extremes from the daily bars of its finished sessions (the creation day counted whole), persisted;
        "through" reaches the start of today once every session before today is read, the window's end once its last one is."""
        if not self.known():
            return
        today, first, last = et_date(now_ms), et_date(self.start_ms), et_date(self.end_ms)
        hist = dict(self.history) or {"start": self.start_ms, "through": self.start_ms, "high": None, "high_at": 0, "low": None, "low_at": 0}
        done = [b for b in bars if first <= et_date(b[0]) <= last and et_date(b[0]) < today and b[2] is not None and b[3] is not None]
        for opened, open_, high, low, _ in done:
            if hist.get("open") is None and et_date(opened) == first and open_ is not None:
                hist["open"] = open_
            if hist["high"] is None or high > hist["high"]:
                hist["high"], hist["high_at"] = high, opened
            if hist["low"] is None or low < hist["low"]:
                hist["low"], hist["low_at"] = low, opened
        need = max((d for d in (first + dt.timedelta(days=i) for i in range((min(last, today - dt.timedelta(days=1)) - first).days + 1))
                    if us_session(d)), default=None)  # the last session before today the window needs read
        if need is None or (done and et_date(done[-1][0]) >= need):
            hist["through"] = max(int(hist["through"]), min(et_wall_ms(today, 0, 0), self.window_end))
        self.scanned_ms = now_ms
        self.store.put(f"range:{self.spec.slug}", hist)

    def price_stale(self, now_ms: int) -> bool:
        return now_ms - self.fetched_ms > self.PRICE_STALE_MS

    def remaining_years(self, now_ms: int) -> float:
        """Trading time left, the running session's remainder scaled by the in-session share of the daily variance (its
        opening gap is behind it; σ, from close-to-close returns, holds a gap for every day). On the deadline day that
        is the whole difference between a level a few cents away looking reachable and not."""
        if not self.known():
            return 0.0
        return us_trading_years(max(now_ms, self.start_ms), self.window_end, self.share[0] if self.share else 1.0)

    def probability(self, level: D, direction: str, now_ms: int) -> float | None:
        return super().probability(level, direction, now_ms) if self.known() else None

    def advice_problem(self, now_ms: int) -> str:
        if not self.known():
            return "等待 Predict 的创建时间和截止日期；暂不给建议"
        return super().advice_problem(now_ms)

    def source_name(self) -> str:
        return f"Yahoo 行情（NASDAQ:{self.spec.symbol}）"

    def venue_name(self) -> str:
        return "Yahoo 行情，NASDAQ:"

    def extremes_source(self) -> str:
        return f"Yahoo 日 K + 1 分钟 K（NASDAQ:{self.spec.symbol} 常规时段，与 TradingView 的 1 分钟 K 同口径）"

    def sigma_note(self) -> str:
        return f"{self.VOL_DAYS} 个交易日收盘，按 {US_TRADING_DAYS} 个交易日年化"

    def extremes_name(self) -> str:
        return "Yahoo 日 K（常规时段）"

    def et_label(self, ms: int) -> str:
        return self.spec.label(ms)

    def close_label(self) -> str:
        if not self.end_ms:
            return "截止日期待 Predict 确认（标题里的日期，当天 23:59 ET）"
        return f"{self.et_label(self.end_ms)}前触及即 Yes，到期未触及为 No（TradingView 1 分钟 K 的最高价 ≥ 档位算触及）"

    def question(self, levels: list[str]) -> str:
        """The market's question in one line: 'STRC 在 12-31 23:59 ET 之前是否触及 $100'."""
        price = "、".join(levels) if levels else "标题里的价格"
        when = f"在 {self.et_label(self.end_ms)}之前" if self.end_ms else "在截止日（待 Predict 确认）之前"
        return f"{self.spec.symbol} {when}是否触及 {price}：窗口内任一 1 分钟 K 的最高价达到即 Yes，到期没碰到为 No"

    def window_label(self) -> str:
        return f"市场创建 {self.et_label(self.start_ms)}起" if self.start_ms else "市场创建起（时间待 Predict 确认）"

    def card_extras(self, now_ms: int) -> dict:
        state, opens = us_session_state(now_ms)
        share = f"；今日剩余时段按 {self.share[0] * 100:.0f}% 方差计（{self.share[1]} 日开盘跳空已扣除）" if self.share else ""
        session = f"美股交易中（常规时段 09:30–16:00 ET）{share}" if state == "交易中" else \
            f"美股已收盘，{self.et_label(opens)}开盘；收盘期间现价为最后成交价，概率按剩余交易时段计算" if opens else "美股已收盘"
        return {"range_word": self.range_word, "session": session, "question": self.question(self.level_names),
                "extremes_note": "Yahoo 日 K 的最高/最低（常规交易时段，与 TradingView 的 1 分钟 K 同口径；创建当日按整日计）",
                "rule_note": f"↑ 档：创建后任一 TradingView 1 分钟 K（NASDAQ:{self.spec.symbol}，常规时段）的最高价 ≥ 档位即 Yes；"
                             "数据源不同，差几分钱的触及请到 Predict 核实"}

    def missing_note(self, now_ms: int) -> str:
        if not self.known():
            return "等待 Predict 的创建时间和截止日期" + (f"（标题：{brief_error(self.title, 60)}）" if self.title else "")
        if self.price is None or self.sigma is None:
            return f"等待 Yahoo 行情（{brief_error(self.error, 80)}）" if self.error else "等待 Yahoo 行情"
        if self.price_stale(now_ms) and now_ms < self.window_end:
            return f"Yahoo 行情停在 {stamp(self.fetched_ms, seconds=False)}，暂停概率（已触及的档位仍算已触及）"
        return ""


def range_market(store: "Store", spec: RangeSpec, futures: Any, config: "Config") -> RangeMarket:
    """The market object for a price-ladder spec: a Binance pair, or a US stock read from Yahoo."""
    if spec.venue == "nasdaq":
        return StockRangeMarket(store, spec, config.ladder_deadlines.get(spec.key, 0))
    return RangeMarket(store, spec, futures)


# --- paper trading (does buying every 10¢ edge make money in the long run?) ------------------------------------------
@dataclass(frozen=True)
class SimMarket:
    """One Predict market as the paper trader sees it, exactly as its card does: the model's fair price for the 涨 / Yes
    side, the book oriented to that side, the bar a suggestion must clear, and what deciding the result needs."""
    market: str              # unique: the slug, plus "#<market id>" for one level of a ladder
    item: str                # the card's name for it (恒生指数 / BTC 10月涨跌 / $牛来 市值 $200M)
    kind: str                # close | touch | updown | flip | range | ladder
    key: str                 # the card's book key (HSI / UNITREEUSDT / BNB / BTC-2026-10 / NIULAI)
    fair_up: float
    book: PredictBook        # bids / asks price the 涨 / Yes side
    need: float
    hold: str                # why the card suggests nothing now ("" = it may)
    sides: tuple[str, str]   # the card's names for the two sides: (涨, 跌), (Yes, No), ($3k, $1k)
    settle: dict             # what deciding the result needs
    evidence: Mapping = field(default_factory=dict)  # what the odds rest on (model inputs, price sources, proxy / anchor); a
    # LazyEvidence builds it on first access, i.e. when a trade is opened or filled, not on every look at every market
    makers: bool = True      # paper-trading maker orders (price ladders remain taker-only)
    maker_alerts: bool = False  # price ladders: separately gated maker opportunities, independent of taker alerts
    maker_note: str = ""     # why a price-ladder maker suggestion is unavailable


class LazyEvidence(Mapping):
    """A read-only mapping whose contents are built by ``build`` on first access. A failure never stops a trade: it is
    kept as the record ({"error": ...})."""

    def __init__(self, build: Any):
        self._build, self._data = build, None

    def _load(self) -> dict:
        if self._data is None:
            try:
                built = self._build()
                self._data = built if isinstance(built, dict) else {}
            except Exception as error:
                self._data = {"error": clean_error(error) or type(error).__name__}
        return self._data

    def __getitem__(self, key: str) -> Any:
        return self._load()[key]

    def __iter__(self) -> Any:
        return iter(self._load())

    def __len__(self) -> int:
        return len(self._load())

    def __repr__(self) -> str:
        return repr(self._load())


SIM_KINDS = {"close": "指数/个股日涨跌", "touch": "先触价", "updown": "月度涨跌", "flip": "反超", "range": "价格阶梯",
             "ladder": "市值阶梯"}
# A resting paper order is placed only on a book it could fill in: both sides quoted, no further apart than this. A lone
# 0.2¢ bid under an 80¢ ask is not a quote anyone sells into: an order joining it would rest until the result and tell
# nothing (10-03: 54 such price-ladder orders sat as 挂单中 0/100 for weeks).
SIM_MAKER_SPREAD = 0.10
SIM_MIN_FILL = 0.5  # a taker buys only when the book fills at least this share of SIM_SHARES (dust is not a trade)


def book_crossed(book: PredictBook) -> bool:
    """Bid at or above ask: a snapshot caught mid-update (or a broken feed), not a book anyone could trade."""
    return bool(book.bid and book.ask and float(book.bid[0]) >= float(book.ask[0]) - 1e-9)


def sim_maker_block(book: PredictBook, spread: float = SIM_MAKER_SPREAD) -> str:
    """Why no resting paper order is placed on this book (one-sided, crossed, or a spread wider than ``spread``); ""
    when it may be."""
    if not book.bid or not book.ask:
        return "盘口只有一边"
    if book_crossed(book):
        return "盘口交叉（买价不低于卖价），快照不可信"
    gap = float(book.ask[0]) - float(book.bid[0])
    if gap > spread + 1e-9:
        return f"买卖价差 {cents(gap)} 超过 {cents(spread)}"
    return ""


def sim_scope(config: Config) -> tuple[str, str]:
    """What the paper trader trades, in words: (which suggestions: 只吃单 / 只挂单 / 挂单和吃单, which markets)."""
    ways = {"taker": "只吃单", "maker": "只挂单", "both": "挂单和吃单"}[config.sim_ways]
    kinds = "全部市场" if config.sim_markets >= set(SIM_KINDS) else "、".join(n for k, n in SIM_KINDS.items() if k in config.sim_markets)
    return ways, kinds


# --- common risk: positions that one event settles together ----------------------------------------------------------
def market_driver(kind: str, key: str, settle: dict | None = None, item: str = "") -> tuple[str, str]:
    """(key, shown name) of the one event a market settles on, so positions that lose together are counted together:
    ten markets can be one risk. A daily card: its index or stock on its target day (恒生指数 10-07 is one event, 10-08
    another). A crypto card: the Binance pair it is read from (BTC 先触, BTC 10月涨跌 and the BTC price ladder all follow
    BTCUSDT). A flip: its pair. A market-cap ladder: its token (every level of $PONS is one pump away). Derived from
    the kind and key alone, so records saved before the driver was kept group the same way."""
    settle = settle or {}
    if kind == "close":
        target = str(settle.get("target") or "")
        name = item.partition("｜")[0] or key
        return f"{key}@{target}", name + (f"（{target[5:]}）" if target else "")
    if kind == "flip":
        spec = next((s for s in FLIP_MARKETS if s.key == key), None)
        pair = f"{spec.coin}/{spec.other}" if spec else key
        return pair, pair
    if kind == "ladder":
        spec = next((s for s in CAP_MARKETS if s.key == key), None)
        return key, spec.name if spec else (item.rsplit(" ", 1)[0] if item else key)
    specs = {"touch": TOUCH_MARKETS, "updown": UPDOWN_MARKETS, "range": (*RANGE_MARKETS, *STOCK_HIT_MARKETS)}.get(kind, ())
    spec = next((s for s in specs if s.key == key), None)
    symbol = spec.symbol if spec else key
    return symbol, symbol.removesuffix("USDT")


def trade_driver(trade: dict) -> tuple[str, str]:
    """A paper trade's driver: as saved at the order, else derived (records from before it was kept)."""
    if trade.get("driver"):
        return str(trade["driver"]), str(trade.get("driver_name") or trade["driver"])
    return market_driver(str(trade.get("kind") or ""), str(trade.get("key") or ""), trade.get("settle"), str(trade.get("item") or ""))


def scenario_result(kind: str, settle: dict, direction: str, level: float | None) -> float:
    """What the 涨 / Yes side pays (1, 0 or ½) when the driver makes one move: "up" to ``level`` (every upward level at
    or under it is touched; None = past them all), "down" likewise, or "flat" (nothing touched, a tie where one is
    possible). A price ladder's downward levels are touched by the down move only."""
    if kind in {"range", "ladder"}:
        target = float(settle.get("target") or 0)
        want = settle.get("dir", "up") if kind == "range" else "up"
        if direction == "flat" or direction != want:
            return 0.0
        if level is None:
            return 1.0
        return 1.0 if (target <= level if direction == "up" else target >= level) else 0.0
    if direction == "flat":
        return 0.0 if kind == "flip" else 0.5
    return 1.0 if direction == "up" else 0.0


def scenario_text(name: str, kinds: set[str], direction: str, level: float | None) -> str:
    """The move in words: '$PONS FDV 涨到 $1B', 'BTC 跌到 $60k', '恒生指数（10-07）收跌', 'BTC 先触高线', 'HYPE/SOL 反超'."""
    label = usd_short if "ladder" in kinds else level_label
    name = name + (" " if name and name[-1].isascii() else "")
    if kinds <= {"close", "updown"}:
        return name + {"up": "收涨", "down": "收跌"}.get(direction, "收平")
    if kinds == {"touch"}:
        return name + {"up": "先触高线", "down": "先触低线"}.get(direction, "都没触及")
    if kinds == {"flip"}:
        return name + ("反超" if direction == "up" else "没反超")
    if direction == "flat":
        return name + "都没触及"
    word = "涨" if direction == "up" else "跌"
    return f"{name}{word}到 {label(level)}" if level is not None else f"{name}{word}过所有档位"


def group_worst_case(positions: list[dict], name: str = "") -> tuple[float, str]:
    """(P&L, the move) of one driver's positions under the single move that hurts them most. Candidates: the driver
    moves up to each upward level (or past all), down to each downward level (or past all), or does neither. Three
    No positions at $300M / $500M / $1B lose together on the move past $1B: that is the number to limit, not the
    count of markets. ``positions``: dicts with kind, side, settle, price and shares (the filled ones)."""
    kinds = {str(p.get("kind") or "") for p in positions}
    up_levels = sorted({float(p["settle"].get("target") or 0) for p in positions
                        if p.get("kind") == "ladder" or (p.get("kind") == "range" and (p.get("settle") or {}).get("dir", "up") == "up")})
    down_levels = sorted({float(p["settle"].get("target") or 0) for p in positions
                          if p.get("kind") == "range" and (p.get("settle") or {}).get("dir", "up") == "down"}, reverse=True)
    scenarios = [("flat", None), *(("up", t) for t in up_levels), ("up", None), *(("down", t) for t in down_levels), ("down", None)]

    def pnl(direction: str, level: float | None) -> float:
        total = 0.0
        for p in positions:
            up = scenario_result(str(p.get("kind") or ""), p.get("settle") or {}, direction, level)
            payout = up if p.get("side") == "up" else 1 - up
            total += (payout - float(p["price"])) * float(p["shares"])
        return total
    worst = min(scenarios, key=lambda s: (round(pnl(*s), 6), s[1] is None))  # past every level only when that is worse
    return pnl(*worst), scenario_text(name, kinds, *worst)


def sim_positions(trades: list[dict]) -> list[dict]:
    """The open paper positions (filled shares of a filled or resting trade), as group_worst_case counts them."""
    return [t for t in trades if t.get("status") in {"filled", "resting"} and float(t.get("shares") or 0) > 0]


def sim_groups(trades: list[dict]) -> list[dict]:
    """Open positions by driver, the most dangerous first: how many, what they cost, the worst single move and what it
    would lose, the share of all open money that one event holds, and the resting orders waiting to join."""
    held = sim_positions(trades)
    resting = [t for t in trades if t.get("status") == "resting"]
    total = sum(float(t["price"]) * float(t["shares"]) for t in held)
    out = []
    for driver in dict.fromkeys(trade_driver(t)[0] for t in [*held, *resting]):
        mine = [t for t in held if trade_driver(t)[0] == driver]
        waiting = [t for t in resting if trade_driver(t)[0] == driver]
        name = trade_driver((mine or waiting)[0])[1]
        cost = sum(float(t["price"]) * float(t["shares"]) for t in mine)
        worst, event = group_worst_case(mine, name) if mine else (0.0, "")
        out.append({"driver": driver, "name": name, "kinds": sorted({str(t.get("kind") or "") for t in [*mine, *waiting]}),
                    "positions": len(mine), "markets": len({t["market"] for t in mine}), "cost": cost,
                    "share": cost / total if total else 0.0, "worst": worst, "event": event,
                    "resting": len(waiting), "resting_usd": sum((float(t.get("order") or 0) - float(t.get("shares") or 0)) * float(t["price"])
                                                            for t in waiting),
                    "items": list(dict.fromkeys(str(t.get("item") or "") for t in [*mine, *waiting]))[:6]})
    return sorted(out, key=lambda g: (g["worst"], -g["cost"]))


def sim_sources(trades: list[dict]) -> list[dict]:
    """What the open positions' odds rest on: each price source named in an open trade's entry record (a trade with two
    sources counts for both), with the trades and money that go wrong together when that source does."""
    out: dict[str, dict] = {}
    for t in trades:
        if t.get("status") not in {"filled", "resting"}:
            continue
        money = float(t["price"]) * max(float(t.get("shares") or 0), float(t.get("order") or 0) if t.get("status") == "resting" else 0.0)
        names = {str(s.get("source") or "") for s in ((t.get("entry") or {}).get("sources") or []) if isinstance(s, dict)}
        for name in names - {""}:
            row = out.setdefault(name, {"source": name, "trades": 0, "cost": 0.0, "items": []})
            row["trades"] += 1
            row["cost"] += money
            if str(t.get("item") or "") not in row["items"] and len(row["items"]) < 6:
                row["items"].append(str(t.get("item") or ""))
    return sorted(out.values(), key=lambda r: (-r["cost"], -r["trades"], r["source"]))


# --- markouts: where the market goes after a fill (a fill that is followed by a fall was someone else's exit) ---------
MARKOUT_HORIZONS = ((60, "1m"), (300, "5m"), (1800, "30m"))


def side_mid(book: PredictBook, side: str) -> float | None:
    """The book's middle for buying one side (涨 / Yes: the Yes bid and ask; 跌 / No: their complement), the one quote
    when only one side is there, None on an empty book."""
    quotes = [float(book.bid[0]) if book.bid else None, float(book.ask[0]) if book.ask else None]
    have = [q for q in quotes if q is not None]
    if not have:
        return None
    mid = sum(have) / len(have)
    return mid if side == "up" else 1 - mid


def markout_stats(trades: list[dict]) -> dict[str, dict]:
    """Per horizon, over the trades that have the mark: how many, the mean move of the market's middle against the fill
    price (per share), and the mean move of the model's own fair price. A fill the market then moves away from was
    filled by someone who knew more; the points earned meanwhile have to cover that."""
    out = {}
    for _, label in MARKOUT_HORIZONS:
        marks = [t["markout"][label] for t in trades if isinstance(t.get("markout"), dict) and isinstance(t["markout"].get(label), dict)
                 and t["markout"][label].get("mid") is not None]
        if marks:
            out[label] = {"n": len(marks), "avg": sum(float(x["move"]) for x in marks) / len(marks),
                          "fair": sum(float(x.get("fair_move") or 0) for x in marks) / len(marks)}
    return out


def alert_kind(text: str) -> str:
    """appear / gone / flip from an announcement's first line (records saved before the kind was kept)."""
    head = text.split("\n", 1)[0]
    return "appear" if "新机会" in head else "flip" if "方向反转" in head else "gone" if "建议失效" in head else "other"


def edge_watch(st: dict, sides: dict | None, best: "BookEdge | None", now_ms: int, bar: float, confirm_ms: int,
               cooldown_ms: int) -> str:
    """One look at one market for the edge alerts; returns the change to announce ("appear:up", "gone:down", ...) or "".

    appear: nothing is announced and the card's suggestion reaches ``bar``; gone: the announced side is no longer
    suggested at all (a dip under ``bar`` is not enough: no flapping around the line); flip: the other side is suggested
    instead. A change is announced once it has held ``confirm_ms``; a side announced as new is not announced as new again
    for ``cooldown_ms``. ``sides`` = {"up" / "down": that side's best edge clearing the card's bar, or None}; ``sides``
    None = the card suggests nothing right now (a stale book, the model holding back): nothing moves. ``st`` is the
    market's saved state, updated in place."""
    if sides is None:
        st["pending"] = None
        return ""
    told, last = st.get("told"), st.setdefault("last", {})
    want = ""
    if told is None:
        side = best and ("up" if best.side == "涨" else "down")
        if best is not None and best.edge >= bar - 1e-9 and now_ms - last.get(side, -10**15) >= cooldown_ms:
            want = "appear:" + side
    elif sides.get(told) is None:
        other = "down" if told == "up" else "up"
        want = ("flip:" + other) if sides.get(other) is not None else ("gone:" + told)
    pending = st.get("pending")
    if not want:
        st["pending"] = None
        return ""
    if not pending or pending.get("want") != want:
        st["pending"] = pending = {"want": want, "since": now_ms}
    if now_ms - pending["since"] < confirm_ms:
        return ""
    st["pending"] = None
    kind, side = want.split(":")
    # after a flip the new side counts as announced only when it reaches the bar itself
    st["told"] = None if kind == "gone" or (kind == "flip" and sides[side].edge < bar - 1e-9) else side
    if st["told"]:
        last[side] = now_ms
    st["seq"] = int(st.get("seq", 0)) + 1
    return want


def side_levels(book: PredictBook, side: str) -> list[tuple[float, float]]:
    """What buying one side costs, best first: 涨 / Yes = the asks; 跌 / No = 1 − the bids."""
    if side == "up":
        return [(float(p), float(q)) for p, q in book.asks]
    return [(1 - float(p), float(q)) for p, q in book.bids]


def fill_shares(levels: list[tuple[float, float]], shares: float) -> tuple[float, float]:
    """(average price, shares bought) taking up to ``shares`` from ``levels`` [(price, size)], best first."""
    got = cost = 0.0
    for price, size in levels:
        take = min(size, shares - got)
        if take <= 0:
            break
        got, cost = got + take, cost + take * price
    return (cost / got if got else 0.0), got


def own_levels(book: PredictBook, side: str) -> list[tuple[float, float]]:
    """Where a resting buy of one side queues, best first: 涨 / Yes = the bids; 跌 / No = 1 − the asks."""
    if side == "up":
        return [(float(p), float(q)) for p, q in book.bids]
    return [(1 - float(p), float(q)) for p, q in book.asks]


def book_snapshot(book: PredictBook, depth: int = 5) -> dict:
    """The top of a book as evidence, priced as the 涨 / Yes side (as on the card), with when it was read."""
    return {"bids": [[float(p), float(q)] for p, q in book.bids[:depth]],
            "asks": [[float(p), float(q)] for p, q in book.asks[:depth]],
            "fetched_ms": book.fetched_ms, "market_id": book.market_id, "fee_bps": book.fee_bps}


def price_evidence(what: str, source: str, symbol: str, kind: str, price: Any, quoted_ms: int = 0,
                   fetched_ms: int = 0, **extra: Any) -> dict:
    """One price behind a card: what it stands for, the source it really came from, the code asked for, the kind of
    price (last trade, mark, mid, daily close ...), and when it was quoted (market time) and read (by the bot)."""
    with contextlib.suppress(TypeError, ValueError, decimal.InvalidOperation):
        price = float(price) if price is not None else None
    return {"what": what, "source": source or "", "symbol": symbol, "type": kind, "price": price,
            "quoted_ms": int(quoted_ms or 0), "fetched_ms": int(fetched_ms or 0), **extra}


def taker_quote(book: PredictBook, side: str, shares: float, fee_bps: int) -> dict | None:
    """Buying ``shares`` of one side now, across the book: average price, shares got (fewer when the book is thin),
    fee per share, cost per share (average + fee), the best price and the levels walked. None for an empty side."""
    levels = side_levels(book, side)
    if not levels:
        return None
    avg, got = fill_shares(levels, shares)
    if got <= 0:
        return None
    walked, left = [], shares
    for price, size in levels:
        if left <= 1e-12:
            break
        take = min(size, left)
        walked.append([price, take])
        left -= take
    fee = taker_fee(avg, fee_bps)
    return {"avg": avg, "got": got, "fee": fee, "cost": avg + fee, "best": levels[0][0], "short": got < shares - 1e-9,
            "levels": walked}


def maker_fill(trade: dict, book: PredictBook) -> tuple[float, dict]:
    """Shares of a resting paper buy that the book now shows as filled, and the evidence. Sellers at or through its
    price would have traded with it; while they show, every real bid at that price is gone, so the queue that was ahead
    of it is too. Only what is visible counts, and the most ever seen, never a sum of looks (one resting seller is seen
    again on every look): a conservative, presumed fill (推定成交)."""
    price = trade["price"]
    through = [(p, q) for p, q in side_levels(book, trade["side"]) if p <= price + 1e-9]
    visible = sum(q for _, q in through)
    credited = min(float(trade["order"]), max(float(trade["shares"]), visible))
    queue = next((q for p, q in own_levels(book, trade["side"]) if abs(p - price) < 1e-9), 0.0)
    return credited, {"through": [[round(p, 6), q] for p, q in through[:5]], "visible": visible, "queue_now": queue}


def sim_upgrade(trade: dict) -> dict:
    """A record kept before evidence was saved, in today's shape: the order size, its fills and its local settlement,
    nothing invented (no snapshots: the review page says so). Old maker fills followed the old rule (touched = the whole
    order filled); an order still resting had filled nothing."""
    if trade.get("v") == 2:
        return trade
    t = dict(trade, v=2, legacy=True, revisions=list(trade.get("revisions") or []))
    t.setdefault("order", t.get("shares", 0.0))
    if t["status"] == "resting":
        t["shares"] = 0.0
    fills = []
    if t.get("filled"):
        fills.append({"at": t["filled"], "shares": t["shares"], "fair": t.get("fill_fair", t["fair"]),
                      "how": "旧规则：触价即算全部成交" if t.get("maker") else "吃单立即成交"})
    t.setdefault("fills", fills)
    if t["status"] == "settled" and "local" not in t:
        up = t["payout"] if t["side"] == "up" else 1 - t["payout"]
        t["local"] = {"up": up, "note": t.get("note", ""), "at": t.get("settled", 0), "evidence": {}}
        t.setdefault("confirm", "local")
    return t


def predict_resolution(market: dict) -> dict | None:
    """What a Predict market object says it settled on, if it says anything readable: {"index": the winning outcome in
    indexSet order, "name", "split": a 50/50, "how": the field that said so}. None while open or unreadable."""
    rows = sorted((o for o in market.get("outcomes") or [] if isinstance(o, dict)), key=lambda o: int(o.get("indexSet") or 0))
    names = [str(o.get("name") or "") for o in rows]
    won = [i for i, o in enumerate(rows) if str(o.get("status") or "").strip().upper() in {"WON", "WIN", "WINNER"}]
    if len(won) == 1:
        return {"index": won[0], "name": names[won[0]], "split": False, "how": "outcomes.status"}
    if len(won) == len(rows) == 2:
        return {"index": None, "name": "50/50", "split": True, "how": "outcomes.status"}
    pays = []
    for o in rows:
        value = next((o[k] for k in ("payout", "payoutNumerator", "payoutPerShare") if o.get(k) not in (None, "")), None)
        with contextlib.suppress(TypeError, ValueError):
            pays.append(float(value))
    if rows and len(pays) == len(rows) and sum(pays) > 0:
        top = max(pays)
        if all(abs(p - top) < 1e-9 for p in pays):
            return {"index": None, "name": "50/50", "split": True, "how": "outcomes.payout"}
        if sum(1 for p in pays if p > 0) == 1:
            i = pays.index(top)
            return {"index": i, "name": names[i], "split": False, "how": "outcomes.payout"}
    status = str(market.get("status") or "").upper()
    if not any(word in status for word in ("RESOLV", "SETTL", "FINAL")):
        return None
    res = next((market.get(k) for k in ("resolution", "resolvedOutcome", "winningOutcome", "winner") if market.get(k)), None)
    if isinstance(res, dict):
        name = str(res.get("name") or res.get("outcome") or "")
    else:
        name = res if isinstance(res, str) else ""
    index = None
    if isinstance(res, dict) and str(res.get("indexSet") or "").isdigit():
        index = next((i for i, o in enumerate(rows) if str(o.get("indexSet")) == str(res["indexSet"])), None)
    if index is None and name:
        index = next((i for i, n in enumerate(names) if n.strip().lower() == str(name).strip().lower()), None)
    if index is None:
        return None
    return {"index": index, "name": names[index], "split": False, "how": "resolution"}


def sim_payout(side: str, up_result: float) -> float:
    """What one share of ``side`` pays when the 涨 / Yes side settles at up_result (1, 0, or ½ for a tie)."""
    return up_result if side == "up" else 1 - up_result


def sim_fill_expectation(trade: dict) -> float:
    """The model's expectation for the shares bought, valued at the fair price when each of them was filled (a maker
    order filled as the market turned against it shows up here, not in the expectation at the order)."""
    fills = trade.get("fills") or []
    if not fills:
        return trade["edge"] * trade["shares"]
    return sum((float(f.get("fair", trade["fair"])) - trade["price"]) * float(f["shares"]) for f in fills)


def sim_stats(trades: list[dict]) -> dict:
    """Totals for a set of paper trades: settled count and result split, money in and out, the model's own expectation
    for those positions (at the decision, and at the fills), how they were settled, and what is still open."""
    done = [t for t in trades if t["status"] == "settled"]
    held = [t for t in trades if t["status"] in {"filled", "resting"} and float(t.get("shares") or 0) > 0]
    cost = sum(t["price"] * t["shares"] for t in done)
    back = sum(t["payout"] * t["shares"] for t in done)
    return {"trades": len(trades), "settled": len(done), "wins": sum(t["payout"] == 1 for t in done),
            "losses": sum(t["payout"] == 0 for t in done), "ties": sum(0 < t["payout"] < 1 for t in done),
            "cost": cost, "payout": back, "pnl": back - cost, "roi": (back - cost) / cost if cost else 0.0,
            "expected": sum(t["edge"] * t["shares"] for t in done),
            "expected_fill": sum(sim_fill_expectation(t) for t in done),
            "confirmed": sum(t.get("confirm") == "confirmed" for t in done),
            "mismatch": sum(t.get("confirm") == "mismatch" for t in done),
            "local": sum(t.get("confirm") not in {"confirmed", "mismatch"} for t in done),
            "open": sum(t["status"] == "filled" for t in trades), "open_cost": sum(t["price"] * t["shares"] for t in held),
            "resting": sum(t["status"] == "resting" for t in trades),
            "partial": sum(bool(t.get("maker")) and 0 < float(t.get("shares") or 0) < float(t.get("order") or 0) - 1e-9
                           for t in trades if t["status"] not in {"expired", "cancelled"}),
            "expired": sum(t["status"] == "expired" for t in trades),
            "cancelled": sum(t["status"] == "cancelled" for t in trades),
            "markout": markout_stats(trades)}


def sim_status(trade: dict) -> str:
    """'持仓' / '挂单中' / '挂单中（推定成交 30/100 份）' / '未成交' / '已撤单' / '赢 +$38.00' / '输 −$62.00' / '平局 −$12.00'."""
    status = trade["status"]
    if status != "settled":
        if status == "resting" and float(trade.get("shares") or 0) > 0:
            return f"挂单中（推定成交 {float(trade['shares']):g}/{float(trade['order']):g} 份）"
        return {"resting": "挂单中", "filled": "持仓", "expired": "未成交", "cancelled": "已撤单"}.get(status, status)
    pnl = (trade["payout"] - trade["price"]) * trade["shares"]
    word = "赢" if trade["payout"] == 1 else "输" if trade["payout"] == 0 else "平局"
    return f"{word} {'+' if pnl >= 0 else '−'}${abs(pnl):,.2f}"


def sim_state(trade: dict) -> str:
    """How far a settled trade's result is confirmed: 预结算 (the bot's own data) / 已确认 (Predict agrees, or settled
    it) / 结果不一致 (Predict differs: re-settled on its result); "" while open."""
    if trade["status"] != "settled":
        return ""
    return {"confirmed": "已确认", "mismatch": "结果不一致"}.get(trade.get("confirm", ""), "预结算")


# --- read-only probability web page -------------------------------------------------------------
WEB_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<meta name="theme-color" content="#f2f4f8" data-c="#f2f4f8" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0d1014" data-c="#0d1014" media="(prefers-color-scheme: dark)">
<title>收盘涨跌概率</title>
<script>try{var t=JSON.parse(localStorage.getItem("theme"));if(t==="light"||t==="dark")document.documentElement.dataset.theme=t}catch(e){}</script>
<style>
:root{color-scheme:light;--bg:#f2f4f8;--card:#fff;--text:#161a20;--muted:#636b77;--faint:#98a0ab;--line:#e2e6ec;--line2:#edf0f4;--chip:#eef1f5;--chip2:#e2e6ed;--best:#2a66e0;--on-accent:#fff;--best-bg:#e8f0fe;--best-soft:#cfdefb;--warn:#b86e00;--warn-bg:#fff4df;--up:#dd3a40;--down:#17a05b;--up-bg:#fdeaea;--down-bg:#e3f6ec;--flat:#c6ccd4;--hot:#e4262d;--hot-bg:#fdeaea;--hot-soft:#f6c4c6;--star:#f3b304;--shadow:0 1px 2px rgba(18,26,40,.05),0 2px 8px rgba(18,26,40,.05);--shadow2:0 8px 24px rgba(18,26,40,.12);--r:14px}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){color-scheme:dark;--bg:#0d1014;--card:#171b21;--text:#e8ebef;--muted:#9aa3ae;--faint:#6b747f;--line:#262c34;--line2:#20262d;--chip:#20252c;--chip2:#2b313a;--best:#79a7f7;--on-accent:#0d1014;--best-bg:#19284a;--best-soft:#2a4172;--warn:#e6a93f;--warn-bg:#33270f;--up:#ff6166;--down:#3ccc7f;--up-bg:#3a1b1f;--down-bg:#11301f;--flat:#3a424c;--hot:#ff5a60;--hot-bg:#3a1b1f;--hot-soft:#5c2a2f;--star:#f5c518;--shadow:0 1px 2px rgba(0,0,0,.35),0 2px 8px rgba(0,0,0,.25);--shadow2:0 8px 24px rgba(0,0,0,.45)}}
:root[data-theme=dark]{color-scheme:dark;--bg:#0d1014;--card:#171b21;--text:#e8ebef;--muted:#9aa3ae;--faint:#6b747f;--line:#262c34;--line2:#20262d;--chip:#20252c;--chip2:#2b313a;--best:#79a7f7;--on-accent:#0d1014;--best-bg:#19284a;--best-soft:#2a4172;--warn:#e6a93f;--warn-bg:#33270f;--up:#ff6166;--down:#3ccc7f;--up-bg:#3a1b1f;--down-bg:#11301f;--flat:#3a424c;--hot:#ff5a60;--hot-bg:#3a1b1f;--hot-soft:#5c2a2f;--star:#f5c518;--shadow:0 1px 2px rgba(0,0,0,.35),0 2px 8px rgba(0,0,0,.25);--shadow2:0 8px 24px rgba(0,0,0,.45)}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;-webkit-font-smoothing:antialiased}
button:focus-visible,a:focus-visible,summary:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--best);outline-offset:2px}
@keyframes pulse{0%,100%{box-shadow:0 0 0 3px var(--down-bg)}50%{box-shadow:0 0 0 6px transparent}}
@keyframes shimmer{from{background-position:100% 0}to{background-position:0 0}}
@keyframes chgr{from{background:var(--up-bg)}to{background:transparent}}@keyframes chgg{from{background:var(--down-bg)}to{background:transparent}}
@media (prefers-reduced-motion:reduce){.card.flash{animation:none;outline:3px solid var(--best);outline-offset:3px}h1:before,.skel,.odds.chg-r,.odds.chg-g{animation:none!important}.bar i,#totop{transition:none!important}}
.wrap{max-width:1560px;margin:0 auto;padding:12px 16px 28px}
header{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:6px 16px;padding:4px 0 8px}
.ttl{display:flex;flex-direction:column;gap:2px;min-width:0}
h1{font-size:20px;font-weight:750;letter-spacing:-.01em;margin:0;display:flex;align-items:center;gap:9px;line-height:1.3}
h1:before{content:"";flex:none;width:9px;height:9px;border-radius:50%;background:var(--down);animation:pulse 2.2s ease-out infinite}
body.olddata h1:before{background:var(--warn);animation:none;box-shadow:0 0 0 3px var(--warn-bg)}
.hr{display:flex;flex-wrap:wrap;align-items:center;gap:6px 8px}
.tog{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;line-height:1.4;color:var(--muted);background:var(--card);border:1px solid var(--line);border-radius:999px;padding:4px 11px 4px 9px;cursor:pointer;user-select:none;white-space:nowrap;transition:border-color .15s,color .15s,background .15s}
.tog:hover{border-color:var(--best-soft);color:var(--text)}.tog input{margin:0;accent-color:var(--best)}
body.nobook .pb{display:none}
.meta{color:var(--muted);font-size:12px;display:flex;flex-wrap:wrap;gap:2px 10px;font-variant-numeric:tabular-nums}
.legend{color:var(--faint);font-size:11.5px;margin:0 0 6px;display:flex;flex-wrap:wrap;align-items:center;gap:2px 12px}.legend .sw{white-space:nowrap}.legend i{display:inline-block;width:9px;height:9px;border-radius:3px;margin:0 3px 0 6px;vertical-align:-1px}.legend .sw i:first-child{margin-left:0}.legend .hot i{background:var(--hot)}
h2{font-size:13px;font-weight:700;color:var(--text);letter-spacing:.02em;margin:18px 2px 8px;display:flex;align-items:center;gap:6px;line-height:1.4}
.grid{display:grid;gap:12px;align-items:stretch;grid-template-columns:repeat(auto-fill,minmax(280px,1fr))}
.skel{height:168px;border-radius:var(--r);border:1px solid var(--line);background:linear-gradient(100deg,var(--card) 30%,var(--chip) 50%,var(--card) 70%);background-size:300% 100%;animation:shimmer 1.6s linear infinite}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:12px 14px 10px;display:flex;flex-direction:column;gap:7px;min-width:0;box-shadow:var(--shadow);transition:box-shadow .2s,border-color .2s}
@media (hover:hover) and (pointer:fine){.card:hover{box-shadow:var(--shadow2);border-color:var(--chip2)}}
.card.hot{border:2px solid var(--hot);box-shadow:0 0 0 4px var(--hot-bg),var(--shadow);padding:11px 13px 9px}
.card.rolled{border-color:var(--best);box-shadow:0 0 0 1px var(--best),var(--shadow)}
.head{display:flex;align-items:center;flex-wrap:wrap;gap:4px 6px}
.tags{display:flex;align-items:center;gap:5px;margin-left:auto;flex:none}
.star{flex:none;border:0;background:none;padding:4px;margin:-4px -3px -4px -5px;font-size:15px;line-height:1;cursor:pointer;color:var(--faint);transition:color .15s,transform .15s}
@media (pointer:coarse){.star{padding:8px;margin:-8px -6px -8px -9px}.grip{padding:8px 8px;margin:-8px 0 -8px -10px}}.star.on{color:var(--star)}.star:hover{color:var(--star);transform:scale(1.15)}
.name{font-weight:700;font-size:15px;min-width:0;max-width:calc(100% - 20px);flex:1 0 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;letter-spacing:-.005em}
.tag{border-radius:999px;padding:2px 8px;font-size:11.5px;font-weight:600;line-height:1.35;background:var(--chip);color:var(--muted);white-space:nowrap;font-variant-numeric:tabular-nums}
.tag.next{background:var(--best-bg);color:var(--best)}.tag.auc{background:var(--warn);color:var(--on-accent)}.tag.open{background:var(--down-bg);color:var(--down)}.tag.new{background:var(--best);color:var(--on-accent)}.tag.hotk{background:var(--hot);color:var(--on-accent)}
.cd{font-size:11.5px;font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap;background:var(--chip);border-radius:999px;padding:2px 8px;line-height:1.35}
.cd.done{color:var(--muted);font-weight:500}.cd.soon{color:var(--warn);background:var(--warn-bg)}
.odds{display:flex;align-items:center;gap:10px;font-variant-numeric:tabular-nums;margin:2px -4px 0;padding:2px 4px;border-radius:8px}
.odds.chg-r{animation:chgr 1.6s ease-out}.odds.chg-g{animation:chgg 1.6s ease-out}
.odds b{font-size:24px;font-weight:750;letter-spacing:-.02em;white-space:nowrap;line-height:1.1}.odds .lbl{color:var(--muted);font-size:12px;font-weight:500;letter-spacing:0;margin:0 4px}
.u{color:var(--up)}.d{color:var(--down)}
.bar{flex:1;display:flex;height:8px;border-radius:999px;overflow:hidden;background:var(--line)}.bar i{display:block;height:100%;transition:width .6s}
details{font-size:12.5px}summary{cursor:pointer;list-style:none;display:flex;flex-wrap:wrap;align-items:baseline;gap:1px 5px;color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap;min-width:0;padding:2px 0;transition:color .15s}
summary:hover{color:var(--text)}summary>*{flex:none}summary .sep{margin-left:4px}summary .v{color:var(--text);font-weight:600}summary .un{color:var(--faint);font-size:11px}summary .rd{color:var(--faint);font-size:12px}summary .chip{margin-left:auto}
summary::-webkit-details-marker{display:none}summary:before{content:"▸";color:var(--faint);font-size:11px;width:10px}details[open] summary:before{content:"▾"}
.chip{font-size:11.5px;border-radius:999px;padding:1px 8px;background:var(--chip);font-weight:650;line-height:1.5}
dl{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;margin:7px 0 3px;font-size:12px;padding:8px 11px;background:var(--chip);border-radius:10px}dt{color:var(--muted)}dd{margin:0;word-break:break-word;font-variant-numeric:tabular-nums}
.card.missing p{margin:0;color:var(--muted);font-size:12.5px}
.pb{margin-top:auto;border-top:1px solid var(--line2);padding-top:8px;font-size:12.5px;font-variant-numeric:tabular-nums}
.quote a.open{font-weight:650;color:var(--best);text-decoration:none;white-space:nowrap;border:1px solid var(--best-soft);border-radius:999px;padding:1px 10px;background:var(--best-bg);transition:background .15s,color .15s}
.quote a.open:hover{background:var(--best);color:#fff;border-color:var(--best)}.quote a.pg{font-size:11.5px;color:var(--muted);text-decoration:none;border:1px solid var(--line);border-radius:999px;padding:1px 8px;white-space:nowrap}.quote a.pg:hover{color:var(--best);border-color:var(--best-soft)}@media (pointer:coarse){.quote a.open{padding:4px 11px}}
.quote{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 10px;color:var(--muted)}.quote span{white-space:nowrap}.quote b{color:var(--text);font-weight:600}.quote .pt{font-weight:600;color:var(--best)}
.edges{display:grid;grid-template-columns:repeat(4,1fr);gap:5px;margin-top:6px}
.edge{display:flex;flex-direction:column;align-items:center;justify-content:center;background:var(--chip);border:1.5px solid transparent;border-radius:10px;padding:5px 3px;min-width:0;line-height:1.25;overflow:hidden;font:inherit;color:inherit;cursor:pointer;min-height:44px;transition:border-color .15s,background .15s}
@media (hover:hover) and (pointer:fine){.edge:hover{border-color:var(--chip2)}}
.edge.sel{outline:2px solid var(--muted);outline-offset:1px}.edge .short{font-size:10px;color:var(--warn);line-height:1.1}
.edet{margin-top:6px;padding:8px 10px;border-radius:10px;background:var(--chip);border-left:3px solid var(--best);font-size:12.5px;line-height:1.55;display:flex;flex-direction:column;gap:1px}
.edet b.ok{color:var(--best)}.edet b.no{color:var(--warn)}
.ages{display:flex;flex-wrap:wrap;gap:0 10px;font-size:11px;color:var(--faint);margin-top:auto}.ages .old{color:var(--warn)}.ages .src{color:var(--muted)}
.ages+.pb{margin-top:0}
.fbar{position:sticky;top:0;z-index:5;background:var(--bg);display:flex;flex-wrap:wrap;align-items:center;gap:6px 12px;padding:7px 0;margin-bottom:4px;font-size:12.5px;border-bottom:1px solid var(--line);transition:box-shadow .2s}
.fbar.stuck{box-shadow:0 10px 18px -14px rgba(0,0,0,.35)}
.fchips{display:flex;flex-wrap:wrap;gap:5px}.fchips .tog.on,.famt .tog.on{border-color:var(--best);color:var(--on-accent);background:var(--best)}
button.tog{font-family:inherit}.fsort select{font:inherit;font-size:12.5px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--text);padding:3px 6px}
.famt{display:inline-flex;align-items:center;gap:4px;color:var(--muted)}.famt input{width:64px;font:inherit;font-size:12.5px;padding:3px 6px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--text)}
/* a phone: the bar is one rail that scrolls sideways (chips, sort, size), so it does not stack three rows on top of every screen */
@media(max-width:560px){.fbar{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;overscroll-behavior-x:contain;-webkit-overflow-scrolling:touch;margin:0 -16px 4px;padding:6px 16px;mask-image:linear-gradient(90deg,#000 calc(100% - 28px),transparent);-webkit-mask-image:linear-gradient(90deg,#000 calc(100% - 28px),transparent)}
.fbar::-webkit-scrollbar{display:none}.fbar>*{flex:none}.fchips{flex-wrap:nowrap}.fbar .famt:after{content:"";display:block;width:12px;flex:none}}
#stale{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;background:var(--warn-bg);border:1px solid var(--warn);color:var(--text);border-radius:12px;padding:8px 12px;margin:6px 0;font-size:13px}
body.olddata .card.hot{border:1px solid var(--line);box-shadow:var(--shadow);padding:12px 14px 10px}
body.olddata .edge.best,body.olddata .edge.hot{border-color:transparent;background:var(--chip)}
body.olddata .edge b,body.olddata .edge .el{color:var(--faint)}body.olddata .odds{opacity:.55}
body.flatview .wrap>h2:not(#h-flat),body.flatview .wrap>.grid:not(#g-flat){display:none!important}
#h-flat{display:flex;align-items:center;gap:8px}
.edge .el{color:var(--faint);font-size:11px;white-space:nowrap;max-width:100%;overflow:hidden;text-overflow:ellipsis}.edge .el i{font-style:normal;font-variant-numeric:tabular-nums}
.edge b{font-weight:700;font-size:14.5px;color:var(--faint);white-space:nowrap;font-variant-numeric:tabular-nums}.edge.pos b{color:var(--text)}.edge.pos .el{color:var(--muted)}
.edge.best{border-color:var(--best);background:var(--best-bg)}.edge.best .el{color:var(--text)}.edge.best b{color:var(--best)}
.edge.hot{border-color:var(--hot);background:var(--hot-bg)}.edge.hot b{color:var(--hot)}
.quote .qe{white-space:normal;word-break:break-all}
.grid.wide{grid-template-columns:repeat(auto-fill,minmax(min(100%,500px),500px));align-items:start}
.card.lad .pb{margin-top:0}.grid:not(.wide)>.card.lad{grid-column:1/-1}
/* 市值阶梯 two or more to a row: every card the same height (the tallest, up to a cap); a longer table scrolls inside the card under a sticky header */
@media(min-width:1042px){#g-ladder.wide{grid-auto-rows:1fr;align-items:stretch}#g-ladder.wide>.card.lad{max-height:460px}
#g-ladder.wide>.card.lad .pb{display:flex;flex-direction:column;flex:0 1 auto;min-height:0}#g-ladder.wide>.card.lad .pg{overflow-y:auto;min-height:0;overscroll-behavior:contain}
#g-ladder.wide>.card.lad .pg .lh{position:sticky;top:0;background:var(--card);z-index:1}#g-ladder.wide>.card.lad .ages{margin-top:0}}
.lstat{display:flex;flex-direction:column;gap:4px}.lstat:empty{display:none}.qline{font-size:12.5px;line-height:1.5;padding:7px 10px;border-radius:10px;background:var(--chip);border-left:3px solid var(--best)}
.card.lad .name{flex:0 1 auto}.card.lad .cd{margin-left:6px}
.touched{display:flex;flex-wrap:wrap;align-items:center;gap:4px 6px;font-size:12px}.touched .k{color:var(--down);font-weight:600}
.tchip{border-radius:999px;padding:1px 8px;background:var(--down-bg);color:var(--down);font-weight:600;font-variant-numeric:tabular-nums}
.simtop{display:flex;align-items:baseline;gap:8px}.simpnl{font-size:28px;font-weight:800;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.simln{display:flex;flex-wrap:wrap;gap:2px 12px;font-size:13px;color:var(--muted)}
.simg{display:grid;grid-template-columns:auto auto auto 1fr;gap:3px 12px;font-size:12.5px;font-variant-numeric:tabular-nums;margin-top:4px;padding:8px 10px;border-radius:10px;background:var(--chip)}.simg .lh{color:var(--faint);font-size:11px}.simg .ln{text-align:right}
.simrows{display:flex;flex-direction:column;gap:2px;margin-top:4px}
a.simrow{display:grid;grid-template-columns:auto 1fr auto auto;gap:2px 8px;font-size:12.5px;color:inherit;text-decoration:none;font-variant-numeric:tabular-nums;padding:4px 6px;margin:0 -6px;border-radius:8px;border-top:1px dashed var(--line)}
a.simrow>*{min-width:0}a.simrow>:nth-child(2){overflow:hidden;text-overflow:ellipsis;white-space:nowrap}a.simrow:hover{background:var(--chip)}.simj{font-size:12.5px;color:var(--best);text-decoration:none;align-self:flex-start}.simj:hover{text-decoration:underline}.panel a.cb{text-decoration:none;color:var(--best)}
.small{font-size:12px;margin-top:3px}.mut{color:var(--faint)}.warn{color:var(--warn)}footer{color:var(--faint);font-size:11.5px;margin-top:22px;padding-top:12px;border-top:1px solid var(--line);line-height:1.6;max-width:760px}
#opps{display:flex;flex-direction:column;align-items:stretch;gap:5px;margin:8px 0 4px;padding:8px 11px;font-size:12px;border-radius:12px;border:1px solid var(--hot-soft);background:linear-gradient(90deg,var(--hot-bg),var(--card) 85%)}
#opps.pin{position:sticky;top:var(--top-h,46px);z-index:4;max-height:36vh;overflow-y:auto;overscroll-behavior:contain;box-shadow:var(--shadow)}
#opps .ofold{margin-left:auto;border:0;background:none;color:var(--muted);font:inherit;font-size:11.5px;cursor:pointer;padding:2px 4px;white-space:nowrap;border-radius:6px}#opps .ofold:hover{color:var(--text);background:var(--chip)}
#opps .osum{display:flex;flex-wrap:wrap;align-items:center;gap:5px 10px}
#opps .orow{display:flex;flex-wrap:wrap;align-items:center;gap:5px 6px}#opps .ok{color:var(--hot);font-weight:700;white-space:nowrap}#opps .og{color:var(--muted);font-weight:600;white-space:nowrap;margin-left:2px}
.opp{display:inline-flex;align-items:baseline;gap:4px;max-width:100%;border:1px solid var(--hot-soft);background:var(--card);color:var(--text);border-radius:999px;padding:3px 10px;font:inherit;font-size:12px;line-height:1.4;cursor:pointer;font-variant-numeric:tabular-nums;box-shadow:var(--shadow);transition:border-color .15s,box-shadow .15s}
.opp b{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:46vw}.opp span{color:var(--muted);white-space:nowrap}.opp i{font-style:normal;color:var(--hot);font-weight:700}
.opp:hover{border-color:var(--hot);box-shadow:0 0 0 3px var(--hot-bg)}body.olddata #opps{display:none}
a.opp{text-decoration:none}a.opp.link:after{content:"↗";color:var(--best);font-size:11px;align-self:center}
h2 .secsort{margin-left:auto;font-weight:400;font-size:12px;color:var(--muted);letter-spacing:0}h2 .secsort select{font-size:12px;padding:2px 5px}
.hotset{display:inline-flex;align-items:center;gap:3px;font-size:12px;font-weight:400;color:var(--muted);letter-spacing:0;white-space:nowrap}
.hotset input{width:52px;font:inherit;font-size:12px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--text);padding:2px 5px}
.hotset input:not(:placeholder-shown){border-color:var(--hot);color:var(--hot);font-weight:600}h2 .hotset{margin-left:6px}.hotrow{margin-top:4px;display:flex;align-items:center;gap:6px}
.card.flash{animation:flash 1.4s ease-out}@keyframes flash{from{outline:3px solid var(--best);outline-offset:3px}to{outline:3px solid transparent;outline-offset:3px}}
#g-levels{grid-template-columns:repeat(auto-fill,minmax(min(100%,440px),1fr))}
.card.price-lad{gap:8px}.price-top{display:flex;flex-wrap:wrap;align-items:baseline;gap:5px 9px;font-variant-numeric:tabular-nums}.price-top b{font-size:26px;font-weight:750;letter-spacing:-.02em}.price-top .plabel{font-size:12px;color:var(--muted)}
.price-range{display:flex;flex-wrap:wrap;gap:4px 14px;color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}.price-range b{font-weight:600;color:var(--text)}
.card.price-lad .lstat{min-height:0;gap:4px}.price-model summary{font-size:12px}.price-model dl{line-height:1.5}.price-model{padding-bottom:2px}
.price-lad .touched{gap:4px}.price-lad .touched summary{font-size:12px;color:var(--down)}.price-lad .touched .tchips{display:flex;flex-wrap:wrap;gap:4px;margin-top:5px}
.price-tools{display:flex;flex-wrap:wrap;align-items:center;gap:5px 8px;margin-top:8px;font-size:11px;color:var(--muted)}.price-tools .cb{font-size:11px;margin-left:auto}.price-tools .hint{flex:1;min-width:0}
.pg{display:grid;grid-template-columns:fit-content(190px) minmax(40px,auto) minmax(0,1fr) minmax(0,1fr);column-gap:10px;row-gap:0;align-items:stretch;margin-top:7px;font-size:12px;font-variant-numeric:tabular-nums}
.pg .lh{padding:0 0 6px;font-size:11px;font-weight:600;letter-spacing:.03em;color:var(--faint);white-space:nowrap}.pg .lr{text-align:right}.pg .pcell{border-top:1px solid var(--line2);padding:7px 0;min-width:0;align-content:center}.pg .pmodel{text-align:right;color:var(--muted)}
.ptarget{font:inherit;color:var(--text);border:0;background:none;text-align:left;cursor:pointer;min-width:0;padding-right:0}.ptarget .ltl{display:flex;flex-wrap:wrap;align-items:baseline;gap:1px 6px}.ptarget .lt{font-weight:700;white-space:nowrap}.ptarget .pdist{font-size:10.5px;color:var(--faint)}
.ppoints{display:inline-block;font-size:10.5px;line-height:1.7;border-radius:999px;padding:0 7px;background:var(--chip);color:var(--muted);white-space:nowrap;font-weight:600;vertical-align:1px}
.ppoints.active{background:var(--best-bg);color:var(--best)}.ppoints.off{padding:0 5px;color:var(--faint);font-weight:400}.quote .ppoints{align-self:center}
/* a wide card: 目标 is three aligned columns (level, distance, points), so the pills line up down the table; a phone puts the pill under the level */
@supports(grid-template-columns:subgrid){@media(min-width:561px){.pg{grid-template-columns:max-content max-content max-content minmax(40px,auto) minmax(0,1fr) minmax(0,1fr)}
.pg>.lh:first-child,.pg .ptarget{grid-column:span 3}.pg .ptarget{display:grid;grid-template-columns:subgrid;column-gap:8px;align-items:baseline}
.ptarget .ltl{display:contents}.ptarget .lt{grid-column:1}.ptarget .pdist{grid-column:2;justify-self:start}.ptarget .ppoints{grid-column:3;justify-self:start}}}
@supports not (grid-template-columns:subgrid){@media(min-width:561px){.ptarget .lt{min-width:4em}.ptarget .pdist{min-width:3.4em}}}
@media(max-width:560px){.ptarget .ltl{display:grid;grid-template-columns:auto minmax(0,1fr);column-gap:5px;row-gap:1px;align-items:baseline}.ptarget .pdist{grid-column:2;justify-self:start}.ptarget .ppoints{grid-column:1/-1;justify-self:start}}
.paction{font:inherit;border:0;background:none;color:var(--faint);text-align:left;cursor:pointer;line-height:1.35;min-width:0;border-radius:6px}.paction .pa{display:flex;flex-wrap:wrap;gap:1px 5px;align-items:baseline}.paction .pa b{font-weight:550}.paction .pv{font-weight:700}.paction.pos .pa{color:var(--text)}.paction.pos .pv{color:var(--best)}.paction.hot .pv{color:var(--hot)}.paction .pwhy{display:block;font-size:10px;line-height:1.35;color:var(--faint);margin-top:1px}.paction.disabled{cursor:pointer}
@media (hover:hover) and (pointer:fine){.paction:hover,.ptarget:hover{background:var(--chip)}}
body.olddata .paction .pa,body.olddata .paction .pv{color:var(--faint)}body.olddata .price-top{opacity:.55}
.pg .lrow{grid-column:1/-1;margin:0 0 7px;padding:8px 10px;border-radius:10px;background:var(--chip);border-left:3px solid var(--best)}.pg .lnote{font-size:12px;line-height:1.5;color:var(--muted)}.pg .lnote .warn{display:block}.pg .lnote b{color:var(--text)}.pg .lspot{grid-column:1/-1;color:var(--best);font-weight:600;font-size:11px;display:flex;align-items:center;gap:8px;padding:5px 0}.pg .lspot:before,.pg .lspot:after{content:"";flex:1;border-top:1px dashed var(--best-soft)}
.price-lad .pb{padding-top:8px}.price-lad .quote{gap:5px 8px}.price-empty{font-size:12px;color:var(--muted);padding-top:8px}
@media(max-width:560px){.pg{grid-template-columns:fit-content(150px) minmax(36px,auto) minmax(0,1fr) minmax(0,1fr);column-gap:6px;font-size:11.5px}.pg .pcell{padding:6px 0}.ptarget .pdist,.ppoints,.paction .pwhy{font-size:10px}.price-top b{font-size:24px}.price-range{gap:3px 10px}.price-lad .edges,.pg .edges{grid-template-columns:repeat(2,1fr)}}
@media(max-width:360px){.pg{grid-template-columns:fit-content(120px) minmax(30px,auto) minmax(0,1fr) minmax(0,1fr);column-gap:4px;font-size:11px}.pg .lh{font-size:10px}}
[hidden]{display:none!important}
.grip{flex:none;border:0;background:none;padding:3px 5px;margin:-3px 0 -3px -6px;font-size:16px;line-height:1;color:var(--muted);cursor:grab;touch-action:none;user-select:none;-webkit-user-select:none;-webkit-touch-callout:none}.grip:hover{color:var(--text)}
body.sorting,body.sorting *{cursor:grabbing!important;user-select:none!important}
.card.dragging,h2.dragging{opacity:.55;outline:2px dashed var(--best);outline-offset:2px}h2 .grip{font-size:14px;margin:-4px 2px -4px 0;padding:2px 5px}
.ctl{display:flex;align-items:center;gap:6px;font-size:12px;padding-bottom:6px;border-bottom:1px dashed var(--line)}.ctl .sp,.panel .sp{flex:1}
.ctl .grip{font-size:16px;margin:0;padding:1px 6px;border:1px solid var(--line);border-radius:6px;background:var(--chip)}
.cb{border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:8px;padding:2px 10px;font:inherit;font-size:12px;line-height:1.6;cursor:pointer;box-shadow:var(--shadow);transition:border-color .15s,color .15s,background .15s}.cb:hover{border-color:var(--best);color:var(--best)}
.cb.pri{background:var(--best);border-color:var(--best);color:var(--on-accent)}.cb.pri:hover{color:var(--on-accent);filter:brightness(1.08)}.cb.arm{border-color:var(--warn);color:var(--warn)}.cb:disabled{opacity:.4;cursor:default}
h2 .cb{margin-left:6px;padding:0 8px;letter-spacing:0;font-weight:400}
h2 .fold{border:0;background:none;font:inherit;color:inherit;letter-spacing:inherit;padding:4px 8px 4px 0;margin:-4px 0;cursor:pointer;display:inline-flex;align-items:center;gap:5px;border-radius:6px}
h2 .fold:before{content:"▾";color:var(--faint);font-size:11px;width:10px}h2 .fold[aria-expanded=false]:before{content:"▸"}h2 .fold:hover .hn{color:var(--best)}
h2 .fs{font-weight:400;color:var(--faint)}h2 .fs b{color:var(--hot);font-weight:600}
.card.off,.grid.off .card{opacity:.45}
.panel{background:var(--card);border:1px solid var(--best-soft);border-radius:var(--r);padding:12px 14px;margin:6px 0 10px;display:flex;flex-direction:column;gap:8px;font-size:13px;box-shadow:var(--shadow2)}
.panel .pr{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px}.panel .cgrp{display:flex;flex-wrap:wrap;align-items:center;gap:4px 6px;width:100%}
.panel input[type=number]{width:58px;font:inherit;padding:2px 6px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text)}
button.tog{font-family:inherit;padding:4px 11px}.tog.on{border-color:var(--best);color:var(--best);background:var(--best-bg)}
#totop{position:fixed;right:16px;bottom:calc(16px + env(safe-area-inset-bottom,0px));z-index:6;width:40px;height:40px;border-radius:50%;border:1px solid var(--line);background:var(--card);box-shadow:var(--shadow2);color:var(--text);font-size:16px;line-height:1;cursor:pointer;opacity:0;pointer-events:none;transform:translateY(8px);transition:opacity .2s,transform .2s}
#totop.show{opacity:1;pointer-events:auto;transform:none}#totop:hover{color:var(--best);border-color:var(--best)}
/* a phone: the same look, tighter (a screen holds the cards it held before: the bar, a section title and three cards) */
@media(max-width:560px){.wrap{padding:10px 16px 24px}header{padding:2px 0 4px}h1{font-size:19px}.grid{gap:10px}.card{padding:10px 12px 8px;gap:6px}.card.hot{padding:9px 11px 7px}body.olddata .card.hot{padding:10px 12px 8px}
h2{margin:12px 2px 6px}.legend{margin-bottom:4px}.edge{min-height:40px}.pb{padding-top:6px}.odds{margin-top:0}.tag,.cd{padding:1px 7px}.quote a.open{padding:3px 10px}.skel{height:150px}}
#fsent{height:1px;margin-top:-1px}
</style></head><body><div class="wrap">
<header><div class="ttl"><h1>收盘涨跌概率</h1><div class="meta" id="meta">加载中…</div></div><div class="hr"><label class="tog"><input type="checkbox" id="showbook" checked>显示 Predict 盘口</label><button type="button" class="tog" id="theme" title="切换主题">◐ 自动</button><button type="button" class="tog" id="edit" title="调整卡片和栏目顺序、隐藏卡片或栏目、改高亮门槛">✎ 自定义</button></div></header>
<div class="legend" id="legend"></div>
<div id="fsent" aria-hidden="true"></div>
<div class="fbar" id="fbar"><div class="fchips" id="fchips"></div>
<label class="fsort">排序 <select id="sortsel"><option value="">默认</option><option value="edge">按净优势</option><option value="time">按剩余时间</option></select></label>
<span class="famt" title="吃单按这个金额计算成交均价、可买份数、手续费和滑点">试算 <span id="amts"></span><input type="number" id="amtin" min="1" step="1" placeholder="自定" aria-label="自定金额"> U</span></div>
<div id="stale" hidden><span id="stalemsg"></span><button type="button" class="cb" id="retry">立即重试</button></div>
<div id="opps" hidden></div>
<div class="panel" id="custom" hidden>
<div class="pr"><b>自定义布局</b><span class="mut">拖动 ⠿ 或点 ◀ ▶ 调整卡片顺序（收藏栏平时也能拖）；拖动栏目标题前的 ⠿ 或点旁边的 ↑ ↓ 调整栏目顺序；“隐藏”收起不看的卡片。只保存在这个浏览器。</span></div>
<div class="pr" id="secs"></div>
<div class="pr" id="cards"></div>
<div class="pr" id="onerow"></div>
<div class="pr" id="oppsrc"></div>
<div class="pr" id="oppway"></div>
<div class="pr"><span class="mut">“价格阶梯”和“模拟交易”栏默认不显示，勾上才出现；每笔交易的证据和导出在</span><a class="cb" id="journal" href="#">模拟交易复盘 ↗</a></div>
<div class="pr"><label>高亮门槛 <input type="number" id="hotin" min="1" max="50" step="1"> ¢</label><span class="mut">净优势达到这个值的卡片标红框、进机会条（和模型的“建议门槛”不是一回事：没超过建议门槛的方向不会被建议）。栏目标题和每张卡的工具条（自定义时）、每一档的说明（点开档位）旁各有一个“红框 ¢”框：填了按它，空着跟随上一级（档位 → 卡片 → 栏目 → 这里）</span></div>
<div class="pr"><span class="mut" id="hidn"></span><span class="sp"></span><button type="button" class="cb" id="showall">全部显示</button><button type="button" class="cb" id="reset" title="还原卡片和栏目顺序、隐藏与高亮门槛（收藏保留）">恢复默认布局</button><button type="button" class="cb pri" id="done">完成</button></div>
</div>
<h2 id="h-flat" hidden></h2><div class="grid" id="g-flat" hidden></div>
<h2 id="h-fav" hidden>⭐ 收藏</h2><div class="grid" id="g-fav"></div>
<h2 id="h-index">指数</h2><div class="grid" id="g-index"><div class="skel"></div><div class="skel"></div><div class="skel"></div></div>
<h2 id="h-contract">合约标的</h2><div class="grid" id="g-contract"></div>
<h2 id="h-crypto">加密</h2><div class="grid" id="g-crypto"></div>
<h2 id="h-levels">价格阶梯</h2><div class="grid wide" id="g-levels"></div>
<h2 id="h-ladder">市值阶梯</h2><div class="grid wide" id="g-ladder"></div>
<h2 id="h-sim">模拟交易</h2><div class="grid wide" id="g-sim"></div>
<footer id="foot">模型参考，非投资建议。</footer>
<button type="button" id="totop" title="回到顶部" aria-label="回到顶部">↑</button>
</div>
<script>
const $=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e};
const open=new Set(),seen={},rolled={};let skew=0,fetchedAt=0,style="cn",last=null;
// the viewer's theme: the system's unless a light or dark one is picked (kept in this browser; <head> applies it before the first paint)
const THEMES={auto:["◐","自动"],light:["☀","浅色"],dark:["☾","深色"]};let theme=stored("theme","auto",v=>typeof v==="string"&&Object.prototype.hasOwnProperty.call(THEMES,v));
function applyTheme(){const r=document.documentElement;if(theme==="auto")delete r.dataset.theme;else r.dataset.theme=theme;
  const b=document.getElementById("theme");b.textContent=THEMES[theme][0]+" "+THEMES[theme][1];b.title="主题："+THEMES[theme][1]+"（点击切换：自动 → 浅色 → 深色）";
  document.querySelectorAll("meta[name=theme-color]").forEach(m=>m.content=theme==="dark"?"#0d1014":theme==="light"?"#f2f4f8":m.dataset.c)}
const changedAt={};  // card key -> when its fair price last moved between two refreshes (the number flashes once)
// saved in this browser only (localStorage), keyed by the ticker (or the index name) so a renamed card keeps its star
const favKey=it=>it.symbol||it.name;let favs=[];try{favs=JSON.parse(localStorage.getItem("favs")||"[]")}catch(e){}
if(!Array.isArray(favs))favs=[];
favs=[...new Set(favs.filter(k=>typeof k==="string").map(k=>k.includes("|")?(k.split("|")[1]||k.split("|")[0]):k))];  // old "name|symbol" keys
function toggleFav(k){favs=favs.includes(k)?favs.filter(x=>x!==k):[...favs,k];keep("favs",favs);if(last)render(last)}
// the viewer's layout, also in this browser only: card order per section, hidden cards and sections, the red-frame bar
const SECTIONS=["index","contract","crypto","levels","ladder","sim"],SEC_NAMES={fav:"⭐ 收藏",index:"指数",contract:"合约标的",crypto:"加密",levels:"价格阶梯",ladder:"市值阶梯",sim:"模拟交易"};
function keep(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}
function stored(k,d,ok){try{const v=JSON.parse(localStorage.getItem(k));return ok(v)?v:d}catch(e){return d}}
const strs=v=>Array.isArray(v)&&v.every(x=>typeof x==="string");
let order=stored("order",{},v=>!!v&&typeof v==="object"&&!Array.isArray(v)&&Object.values(v).every(strs));
let hidden=stored("hidden",[],strs),hideSec=stored("hideSec",["sim","levels"],strs),oneRow=stored("oneRow",[],strs);  // oneRow: ladder sections shown one card per row
try{if(!localStorage.getItem("simDefault")){if(!hideSec.includes("sim"))hideSec=[...hideSec,"sim"];keep("hideSec",hideSec);localStorage.setItem("simDefault","1")}}catch(e){}  // 模拟交易 starts hidden (show it in 自定义)
try{if(!localStorage.getItem("levelsDefault")){if(!hideSec.includes("levels"))hideSec=[...hideSec,"levels"];  // 价格阶梯 starts hidden too
  if(localStorage.getItem("ladderDefault")){hideSec=hideSec.filter(x=>x!=="ladder");localStorage.removeItem("ladderDefault")}  // 1.21.5 hid 市值阶梯 instead: undo that once
  keep("hideSec",hideSec);localStorage.setItem("levelsDefault","1")}}catch(e){}
let hotCents=stored("hot",10,v=>typeof v==="number"&&v>=1&&v<=50),HOT=hotCents/100;  // an edge this large gets the red frame
// the red-frame bar can also be set per section ("sec:<group>"), per card ("card:<key>") or per ladder level ("row:<key>#<label>"),
// in cents; the most specific one set wins, the global one stands in otherwise
let hotMap=stored("hotmap",{},v=>!!v&&typeof v==="object"&&!Array.isArray(v)&&Object.values(v).every(x=>typeof x==="number"&&x>=1&&x<=50));
const hotByCard={};  // card key -> the bar it was last drawn with (for the chips in its book block)
function hotCentsFor(it,label){const k=favKey(it),g=it.group||"contract",row=label!=null?hotMap["row:"+k+"#"+label]:undefined;
  return row??hotMap["card:"+k]??hotMap["sec:"+g]??hotCents}
function hotFor(it,label){return hotCentsFor(it,label)/100}
function setHot(key,v){if(v==null)delete hotMap[key];else hotMap[key]=v;keep("hotmap",hotMap);drawLegend();if(last)render(last)}
const hotTyping=()=>!!document.activeElement&&document.activeElement.matches(".hotset input");  // a ¢ box has the focus: render() waits (as under a drag)
function hotInput(key,inherit,tip){  // a small ¢ box: empty = inherit (the inherited bar shows as the placeholder)
  const l=$("label","hotset"),i=$("input");i.type="number";i.min="1";i.max="50";i.step="1";i.placeholder=String(inherit);if(hotMap[key]!=null)i.value=hotMap[key];
  i.title=tip;i.setAttribute("aria-label",tip);l.title=tip;
  for(const ev of["click","pointerdown","mousedown","touchstart"])l.addEventListener(ev,e=>e.stopPropagation());  // not a fold, drag or row toggle
  const commit=()=>{const t=i.value.trim(),n=Math.round(Number(t)),v=t!==""&&Number.isFinite(n)?Math.min(50,Math.max(1,n)):null;
    if(v!==(hotMap[key]??null))setHot(key,v)};  // Chrome repeats change when a box is taken out of the page: the same value changes nothing
  i.addEventListener("change",()=>{commit();i.blur()});  // the rebuild the new bar needs runs once the box has let go of the focus (below)
  i.addEventListener("blur",()=>{commit();if(pending&&!drag){const d=pending;pending=null;render(d)}});
  l.append("红框 ",i," ¢");return l}
const isBool=v=>typeof v==="boolean";
let oppOff=stored("oppOff",[],strs),oppMakers=stored("oppMakers",true,isBool),oppTakers=stored("oppTakers",true,isBool),oppPoints=stored("oppPoints",true,isBool);  // the strip on top: sections left out, which sides, makers only where points are earned
try{if(localStorage.getItem("oppTaker")==="true"){oppMakers=false;keep("oppMakers",false)}localStorage.removeItem("oppTaker")}catch(e){}  // the old "只列吃单" switch carries over, once
let folded=stored("folded",[],strs);  // sections folded away: the title stays, with how many cards it holds and how many are red-framed
let oppPin=stored("oppPin",true,isBool),oppFold=stored("oppFold",false,isBool);  // the strip stays under the filter bar while scrolling; folded to one line
function toggleFold(g){folded=folded.includes(g)?folded.filter(x=>x!==g):[...folded,g];keep("folded",folded);if(last)render(last)}
let secOrder=stored("secs",SECTIONS,strs);secOrder=[...new Set(secOrder.filter(g=>SECTIONS.includes(g)))];
SECTIONS.forEach((g,i)=>{if(!secOrder.includes(g)){const prev=SECTIONS.slice(0,i).reverse().find(x=>secOrder.includes(x));  // a new section joins after its default neighbour
  secOrder.splice(prev?secOrder.indexOf(prev)+1:0,0,g)}});
let editing=false,drag=null,pending=null;
function ctlBtn(t,tip,fn,dis){const b=$("button","cb",t);b.type="button";b.title=tip;b.disabled=!!dis;b.addEventListener("click",e=>{e.preventDefault();fn()});return b}
function arrange(items,g){  // the viewer's order first; cards it has not placed yet follow in the page's own order
  const pos=new Map((order[g]||[]).map((k,i)=>[k,i])),at=(it,i)=>pos.has(favKey(it))?pos.get(favKey(it)):1e6+i;
  return items.map((it,i)=>[at(it,i),it]).sort((a,b)=>a[0]-b[0]).map(x=>x[1])}
function saveOrder(g,keys){  // keys = the section's cards as they now stand; cards not on screen (hidden) keep their place after them
  if(g==="fav"){favs=[...keys.filter(k=>favs.includes(k)),...favs.filter(k=>!keys.includes(k))];keep("favs",favs)}
  else{order[g]=[...keys,...(order[g]||[]).filter(k=>!keys.includes(k))];keep("order",order)}}
function keysOf(g){return[...document.getElementById("g-"+g).children].map(x=>x.dataset.key)}
function nudge(g,key,step){const keys=keysOf(g),i=keys.indexOf(key),j=i+step;
  if(i<0||j<0||j>=keys.length)return;[keys[i],keys[j]]=[keys[j],keys[i]];saveOrder(g,keys);if(last)render(last)}
function moveSec(g,step){  // past the next section that is on the page (sections without cards are skipped)
  const vis=secOrder.filter(s=>(plan[s]||[]).length),i=vis.indexOf(g),j=i+step;if(i<0||j<0||j>=vis.length)return;
  const rest=secOrder.filter(s=>s!==g);rest.splice(rest.indexOf(vis[j])+(step>0?1:0),0,g);secOrder=rest;keep("secs",secOrder);drawPanel();if(last)render(last)}
function toggleHidden(k){hidden=hidden.includes(k)?hidden.filter(x=>x!==k):[...hidden,k];keep("hidden",hidden);drawPanel();if(last)render(last)}
function startSecDrag(e,g){  // 自定义: a section title (its ⠿) dragged past another section moves the whole section, as a card does
  if(e.button>0||drag)return;e.preventDefault();
  const id=e.pointerId,h=document.getElementById("h-"+g),foot=document.getElementById("foot");let at=null;drag={sec:g};h.classList.add("dragging");document.body.classList.add("sorting");
  const place=()=>{
    const t=(document.elementFromPoint(at.x,at.y)||document.body).closest(".wrap>h2,.wrap>.grid"),tg=t&&t.id.slice(2);
    if(!tg||tg===g||!SECTIONS.includes(tg))return;  // the starred section stays on top
    const vis=secOrder.filter(x=>(plan[x]||[]).length),i=vis.indexOf(g),j=vis.indexOf(tg);if(i<0||j<0)return;
    const mid=(document.getElementById("h-"+tg).getBoundingClientRect().top+document.getElementById("g-"+tg).getBoundingClientRect().bottom)/2;
    if(!(i<j&&at.y>mid)&&!(i>j&&at.y<mid))return;  // past the other section's middle only, so sections do not flip back and forth
    const rest=secOrder.filter(x=>x!==g);rest.splice(rest.indexOf(tg)+(i<j?1:0),0,g);secOrder=rest;keep("secs",secOrder);
    for(const x of secOrder)foot.before(document.getElementById("h-"+x),document.getElementById("g-"+x))};
  const scroll=setInterval(()=>{if(!at)return;const dy=at.y<56?-14:at.y>innerHeight-56?14:0;if(dy){window.scrollBy(0,dy);place()}},40);
  const move=ev=>{if(ev.pointerId!==id)return;if(ev.pointerType==="mouse"&&!ev.buttons)return end(ev);at={x:ev.clientX,y:ev.clientY};place()};
  const end=ev=>{if(ev.type!=="blur"&&ev.pointerId!==id)return;clearInterval(scroll);
    document.removeEventListener("pointermove",move);document.removeEventListener("pointerup",end);document.removeEventListener("pointercancel",end);removeEventListener("blur",end);
    h.classList.remove("dragging");document.body.classList.remove("sorting");drag=null;const d=pending||last;pending=null;if(d){drawPanel();render(d)}};
  document.addEventListener("pointermove",move);document.addEventListener("pointerup",end);document.addEventListener("pointercancel",end);addEventListener("blur",end)}
function startDrag(e,c,g){
  // pointer events (mouse, pen and touch alike); the card moves in front of or behind the card under the pointer once
  // the pointer is past that card's middle, so cards of different heights do not flip back and forth
  if(e.button>0||drag)return;e.preventDefault();
  const grid=c.parentElement,id=e.pointerId;let at=null;drag={c,g};c.classList.add("dragging");document.body.classList.add("sorting");
  const place=()=>{
    const t=(document.elementFromPoint(at.x,at.y)||document.body).closest(".card");
    if(!t||t===c||t.parentElement!==grid)return;
    const kids=[...grid.children],from=kids.indexOf(c),to=kids.indexOf(t),r=t.getBoundingClientRect();
    const row=Math.abs(r.top-c.getBoundingClientRect().top)<2,mid=row?at.x-(r.left+r.width/2):at.y-(r.top+r.height/2);
    if(from<to&&mid>0)grid.insertBefore(c,t.nextSibling);else if(from>to&&mid<0)grid.insertBefore(c,t)};
  const scroll=setInterval(()=>{if(!at)return;  // held near the top or bottom edge: the page scrolls on by itself
    const dy=at.y<56?-14:at.y>innerHeight-56?14:0;if(dy){window.scrollBy(0,dy);place()}},40);
  const move=ev=>{if(ev.pointerId!==id)return;if(ev.pointerType==="mouse"&&!ev.buttons)return end(ev);  // released out of sight
    at={x:ev.clientX,y:ev.clientY};place()};
  const end=ev=>{if(ev.type!=="blur"&&ev.pointerId!==id)return;clearInterval(scroll);  // a lost window ends it too: never stuck
    document.removeEventListener("pointermove",move);document.removeEventListener("pointerup",end);
    document.removeEventListener("pointercancel",end);removeEventListener("blur",end);
    c.classList.remove("dragging");document.body.classList.remove("sorting");saveOrder(g,keysOf(g));
    drag=null;const d=pending||last;pending=null;if(d)render(d)};
  document.addEventListener("pointermove",move);document.addEventListener("pointerup",end);document.addEventListener("pointercancel",end);
  addEventListener("blur",end)}
const two=n=>String(n).padStart(2,"0"),pct=x=>(x*100).toFixed(1),cent=x=>(x*100).toFixed(1)+"¢";
const like=(v,ref)=>{const d=(String(ref).split(".")[1]||"").length,n=Number(String(v).replace(/,/g,""));
  if(!isFinite(n))return v;const k=d>=2?d:(Math.abs(n)>=1000?0:2);return n.toLocaleString("en-US",{minimumFractionDigits:k,maximumFractionDigits:k})};
const qk=n=>n>=1000?(n/1000).toFixed(n>=9950?0:1).replace(/\\.0$/,"")+"k":qty(n);  // 1,609 -> 1.6k: quote lines stay on one line
const qty=n=>Number(n).toLocaleString("en-US",{maximumFractionDigits:n>=100?0:n>=10?1:2});
const sg=x=>(x>=0?"+":"")+cent(x);
// --- the trade size taker edges are priced for (0 = the server's own), the filter / sort bar, data freshness ----------
let amount=stored("amount",0,v=>typeof v==="number"&&v>=0&&v<=1e6);
let filt=stored("filt",[],strs),sortBy=stored("sort","",v=>["","edge","time"].includes(v));
// the ladder sections' own card order (per browser): by market cap, LP points, best edge, closeness, σ or time left
const SEC_SORTS={ladder:[["","默认顺序"],["cap","市值 高→低"],["capasc","市值 低→高"],["pp","PP/h 高→低"],["edge","最大净优势"],["near","离下一档 近→远"],["sigma","σ 低→高"],["time","剩余时间 短→长"]],
  levels:[["","默认顺序"],["pp","PP/h 高→低"],["edge","最大净优势"],["near","离下一档 近→远"],["sigma","σ 低→高"],["time","剩余时间 短→长"]]};
let secSort=stored("secsort",{},v=>!!v&&typeof v==="object"&&!Array.isArray(v)&&Object.values(v).every(x=>typeof x==="string"));
function secMetric(it,k){  // the number a section sort key reads off a card: its ladder block and its open levels
  const L=it.ladder||{},rows=(L.rows||[]).filter(r=>!r.touched);
  if(k==="cap"||k==="capasc")return L.cap_usd??null;
  if(k==="pp"){const v=rows.filter(r=>r.points_active===true&&r.points_rate!=null).map(r=>r.points_rate);return v.length?Math.max(...v):null}
  if(k==="edge"){const b=view(it).best;return b?b.edge:null}
  if(k==="near"){const v=rows.filter(r=>r.dist!=null&&r.dist>0).map(r=>r.dist);return v.length?Math.min(...v):null}
  if(k==="sigma")return L.sigma??null;
  if(k==="time")return it.close_ms||null;
  return null}
function sortSection(items,g){  // cards without the number go last, ties keep the viewer's own order
  const k=secSort[g]||"";if(!k||!SEC_SORTS[g])return items;
  const desc=["cap","pp","edge"].includes(k);
  return items.map((it,i)=>({it,i,v:secMetric(it,k)})).sort((a,b)=>{if(a.v==null&&b.v==null)return a.i-b.i;if(a.v==null)return 1;if(b.v==null)return -1;return(desc?b.v-a.v:a.v-b.v)||a.i-b.i}).map(x=>x.it)}
const FILTERS=[["sug","有建议","只看现在有建议的卡片"],["no","仅 No/跌","只看建议买 No（或 跌、后一个结果）的"],["maker","仅挂单","只看建议挂单的"],
  ["taker","仅吃单","只看建议吃单的"],["soon","3 小时内收盘","只看 3 小时内收盘或截止的"]];
const SOON_MS=3*3600e3,STALE_MS=60e3;  // no successful refresh for a minute: every highlight comes off
let openChip={},okAt=0,failMsg="",hots=[];  // hots: this render's red-framed suggestions, for the strip on top
function takerFill(levels,notional){  // [average price, shares, short?] buying `notional` USD across [[price, size]], best first
  if(!levels.length)return[0,0,true];let spent=0,shares=0;
  for(const[p,q]of levels){const take=Math.min(q,(notional-spent)/p);spent+=take*p;shares+=take;if(spent>=notional-1e-9)return[spent/shares,shares,false]}
  return[spent/shares,shares,true]}
function edgesFor(p,fair){  // the server's four edges, for the viewer's trade size: 挂涨@买1, 挂跌@1−卖1, 吃涨@卖1, 吃跌@1−买1
  const n=amount||p.notional||100,bps=p.fee_bps||0,fd=1-fair,fee=x=>bps/1e4*Math.min(x,1-x),[up,dn]=p.sides||["涨","跌"],out=[];
  if(p.bids&&p.bids.length){const[bid,size]=p.bids[0];out.push({label:"挂"+up,up:true,maker:true,price:bid,edge:fair-bid,size,gross:fair-bid,fee:0,slip:0,short:false});
    const[avg,sh,short]=takerFill(p.bids.map(([x,q])=>[1-x,q]),n),f=fee(avg);
    out.push({label:"吃"+dn,up:false,maker:false,price:1-bid,edge:fd-avg-f,size:sh,gross:fd-(1-bid),fee:f,slip:avg-(1-bid),short})}
  if(p.asks&&p.asks.length){const[ask,size]=p.asks[0];out.push({label:"挂"+dn,up:false,maker:true,price:1-ask,edge:fd-(1-ask),size,gross:fd-(1-ask),fee:0,slip:0,short:false});
    const[avg,sh,short]=takerFill(p.asks,n),f=fee(avg);out.push({label:"吃"+up,up:true,maker:false,price:ask,edge:fair-avg-f,size:sh,gross:fair-ask,fee:f,slip:avg-ask,short})}
  return out.sort((a,b)=>(a.maker===b.maker?0:a.maker?-1:1)||(a.up===b.up?0:a.up?-1:1))}
const rank=e=>Math.round(e.edge*1e4);
function bestOf(edges,need){  // the largest net edge above the bar; a maker wins a tie (as on the server)
  return edges.filter(e=>e.edge>need).reduce((a,b)=>!a||rank(b)>rank(a)||(rank(b)===rank(a)&&b.maker&&!a.maker)?b:a,null)}
function bookView(p){  // a book block's edges for the chosen size, its suggestion, and every direction that may be suggested
  if(!p||p.fair==null||p.need==null)return{edges:[],pool:[],best:null,ok:[]};  // priced blocks carry need; the chips are computed here from the depth (data.json carries no edges)
  const edges=edgesFor(p,p.fair),pool=p.makers===false?edges.filter(e=>!e.maker):edges,blocked=!!(p.stale||p.hold),need=p.need||0;
  return{edges,pool,best:blocked?null:bestOf(pool,need),ok:blocked?[]:pool.filter(e=>e.edge>need)}}  // pool: the directions that may be suggested
function view(it){  // a card's suggestions (a ladder: every level's) and its best one, for highlights, filters and sorting
  if(it.kind==="ladder"){const ok=[];let best=null;
    (it.ladder.rows||[]).filter(r=>!r.touched).forEach(r=>{const v=bookView(r);ok.push(...v.ok);if(v.best&&(!best||v.best.edge>best.edge))best=v.best});
    return{ok,best}}
  return bookView(it.predict)}
function chipDetail(e,ctx){  // everything behind one edge chip, readable on a phone (no hover needed)
  const d=$("div","edet"),fair=e.up?ctx.fair:1-ctx.fair,line=(...xs)=>{const p=$("div");p.append(...xs);d.append(p)};
  line($("b","",e.label+" @ "+cent(e.price)),e.maker?"：挂单排队，成交不保证，免手续费":"：立即成交");
  line("模型公平价 "+cent(fair)+" − "+cent(e.price)+" = 毛优势 "+sg(e.gross));
  if(e.maker)line("这个价位已有 "+qty(e.size)+" 份在排队");
  else line("按 $"+(amount||ctx.notional)+" 吃单：均价 "+cent(e.price+e.slip)+"（深度滑点 "+sg(e.slip)+"）· 手续费 "+cent(e.fee)+"/份 · "+
    (e.short?"盘口只够买 "+qty(e.size)+" 份，金额超出已读取的深度":"约 "+qty(e.size)+" 份"));
  const counted=!(e.maker&&ctx.makers===false),ok=counted&&e.edge>ctx.need;
  if(!counted)line($("span","warn",ctx.maker_note||"这类档位的挂单不算建议：挂的价位往往只是对面一张远离行情的挂单，基本不会成交"));
  line("净优势 "+sg(e.edge)+" · 建议门槛 "+cent(ctx.need)+(ctx.swing&&ctx.swing>=ctx.need-1e-9?"（模型误差）":"（最低净优势）")+" → ",$("b",ok?"ok":"no",ok?"满足":counted?"不满足":"不计入"));
  const hb=ctx.hot??HOT;if(ok)line("高亮门槛 "+Math.round(hb*100)+"¢ → "+(e.edge>=hb?"标红框":"不标红框"));
  if(ctx.stale)line($("span","warn","盘口过期：不给建议"));else if(ctx.hold)line($("span","warn","暂不建议："+ctx.hold));
  return d}
function chips(key,v,ctx){  // the four edge chips; tap one for its details (kept open across the 10-second refresh)
  const box=$("div","edgebox"),g=$("div","edges"),sel=openChip[key];
  v.edges.forEach(e=>{const x=$("button","edge"+(e===v.best?" best":"")+(e===v.best&&e.edge>=(ctx.hot??HOT)?" hot":"")+(v.pool.includes(e)&&e.edge>ctx.need?" pos":"")+(sel===e.label?" sel":""));
    x.type="button";x.title="点开看明细";x.setAttribute("aria-expanded",sel===e.label?"true":"false");
    const el=$("span","el",e.label+" ");el.append($("i","",(e.price*100).toFixed(1)));x.append(el,$("b","",sg(e.edge)));
    if(e.short)x.append($("span","short","深度不足"));
    x.addEventListener("click",ev=>{ev.preventDefault();ev.stopPropagation();openChip[key]=sel===e.label?null:e.label;if(last)render(last)});g.append(x)});
  box.append(g);const e=v.edges.find(x=>x.label===sel);if(e)box.append(chipDetail(e,ctx));return box}
const ctxOf=p=>({fair:p.fair,need:p.need||0,swing:p.swing||0,hold:p.hold||"",stale:p.stale,notional:p.notional||100,makers:p.makers!==false,maker_note:p.maker_note||""});
function ageSpan(ms,old){const s=$("span","age");s.dataset.ms=ms;s.dataset.old=old;return s}
function ages(it){  // when this card's price and book were last read, and what the price is (spot, a proxy's estimate, ...)
  const row=$("div","ages"),book=it.kind==="ladder"?Math.min(...(it.ladder.rows||[]).map(r=>r.fetched_ms||Infinity)):it.predict&&it.predict.fetched_ms;
  if(it.quote_ms){const s=$("span");s.append("行情 ",ageSpan(it.quote_ms,300e3));row.append(s)}
  if(book&&isFinite(book)){const s=$("span");s.append("盘口 ",ageSpan(book,90e3));row.append(s)}  // a ladder: its oldest level
  if(it.source)row.append($("span","src",it.source));
  if(it.feed){const s=$("span","src",it.feed+(it.quote_ms?" "+hms(it.quote_ms):""));s.title="这个价格来自哪个行情源，以及该源给出的报价时间（北京时间）；竞价期间这就是参考平衡价的更新时间";row.append(s)}
  return row.childNodes.length?row:null}
function hms(ms){try{return new Date(ms).toLocaleTimeString("zh-CN",{timeZone:"Asia/Shanghai",hour12:false})}catch(e){const d=new Date(ms);return two(d.getHours())+":"+two(d.getMinutes())+":"+two(d.getSeconds())}}
function matches(it){  // the filter bar: every chip that is on must hold for one and the same suggestion
  const v=view(it);let pool=v.ok;
  if(filt.includes("no"))pool=pool.filter(e=>!e.up);
  if(filt.includes("maker"))pool=pool.filter(e=>e.maker);
  if(filt.includes("taker"))pool=pool.filter(e=>!e.maker);
  if(filt.some(f=>f!=="soon")&&!pool.length)return null;
  const now=Date.now()+skew;
  if(filt.includes("soon")&&!(it.close_ms&&it.close_ms>now&&it.close_ms-now<=SOON_MS))return null;
  return pool.length?Math.max(...pool.map(e=>e.edge)):-1}  // the sort key: its largest matching net edge
function openLink(url){  // the market opens from this one button (in a new tab); the chips open their details instead
  const a=$("a","pt open","Predict ↗");a.href=url;a.target="_blank";a.rel="noopener noreferrer";a.title="在新标签页打开 Predict 市场";return a}
function pointsPill(p){  // LP points: blue ● with the hourly rate when a quote placed now would earn (Predict's own requirements met);
  // a faint ○ when not, keeping the rate when the market pays at all (the tooltip says why); nothing while unknown
  if(p.points_active==null)return null;  // unknown (nothing read yet, stale, or a refresh failure): the gate fails closed, the pill stays off
  const rate=p.points_active===true&&p.points_rate!=null?qty(p.points_rate):"",unit=x=>{if(rate)x.append(" ",$("span","pu","PP/h"));return x};
  if(p.points_ok===true){const x=unit($("span","ppoints active","● "+(rate||"积分")));
    x.title="积分可得：这个市场的挂单每小时发 "+(rate||"?")+" PP"+(p.points_spread!=null?"（价差不超过 "+cent(p.points_spread)+(p.points_min_shares?"、至少 "+p.points_min_shares+" 份":"")+"）":"");return x}
  const x=unit($("span","ppoints off","○"+(rate?" "+rate:"")));
  x.title=(rate?"有积分（每小时 "+rate+" PP），但现在拿不到：":"")+(p.points_why||p.points_note||"积分未激活");x.setAttribute("aria-label",x.title);return x}
function book(p,key,pages){
  const w=$("div","pb"),q=$("div","quote");q.append(openLink(p.url));w.append(q);
  (pages||[]).slice(0,3).forEach(g=>{const a=$("a","pt pg",g.name+" ↗");a.href=g.url;a.target="_blank";a.rel="noopener noreferrer";a.title="在新标签页打开 "+g.name+" 的行情页：竞价时看当前价（参考平衡价）和更新时间，别只看逐笔成交";q.append(a)});
  const has=p.bids||p.asks;
  if(has&&!(p.bids||[]).length&&!(p.asks||[]).length){q.append($("span","mut","暂无挂单"));if(p.stale)q.append($("span","warn",p.age+" 秒前"))}
  else if(has){const b=p.bids[0],k=p.asks[0],lv=(t,l)=>{const x=$("span","",t+" ");x.append($("b","",l?cent(l[0]):"无"));if(l)x.append("×"+qk(l[1]));if(l)x.title=qty(l[1])+" 份";return x};
    q.append(lv("买1",b),lv("卖1",k));if(b&&k)q.title="价差 "+cent(k[0]-b[0]);
    if(p.stale)q.append($("span","warn",p.age+" 秒前"))}
  else if(!p.error)q.append($("span","","等待获取"));
  const pp=pointsPill(p);if(pp)q.append(pp);  // the market's points, after the quotes (a narrow card wraps it, not the quotes)
  if(p.error)w.append($("div","warn small",(has?"刷新失败，显示上次盘口：":"")+p.error));
  const v=bookView(p);
  if(v.edges.length){w.append(chips(key,v,{...ctxOf(p),hot:hotByCard[key]??HOT}));if(p.stale)w.append($("div","warn small","盘口过期，不给建议"))}  // framed = suggested, grey = not big enough
  return w}
function upColor(){return style==="us"?"var(--down)":"var(--up)"}function downColor(){return style==="us"?"var(--up)":"var(--down)"}
function simCard(c,it){
  // paper trading: would buying every suggestion of 10¢ or more have made money? Results only, nothing is ever ordered
  const s=it.sim,t=s.total,cents=(s.edge*100).toFixed(0),money=x=>(x>=0?"+$":"−$")+Math.abs(x).toFixed(2),usd=x=>"$"+x.toFixed(2);
  const col=x=>x>0?upColor():x<0?downColor():"var(--muted)";
  const how=[];if(s.ways!=="只挂单")how.push("吃单按这么多份吃到的均价和手续费成交");if(s.ways!=="只吃单")how.push("挂单只挂在价差不超过 10¢ 的双边盘口，只按盘口出现的卖单数量推定成交，出结果时没成交的部分作废，不再做的挂单撤掉");
  c.append($("div","small mut","净优势 ≥"+cents+"¢ 时按卡片建议买 "+s.shares+" 份，只记账不下单。"+(s.scope?"范围："+s.scope+"；"+s.ways+"。":"")+how.join("；")+"。先预结算，再以 Predict 结果确认。"));
  const jl=$("a","simj","完整复盘（每笔证据、导出）↗");jl.href=location.pathname.replace(/\\/$/,"")+"/journal";c.append(jl);
  if(!t.trades){c.append($("p","","还没有触发过：等有卡片的净优势达到 "+cents+"¢ 就开始记录。"));return c}
  const top=$("div","simtop"),big=$("b","simpnl",money(t.pnl));big.style.color=col(t.pnl);
  top.append($("span","mut","已结算盈亏"),big);if(t.cost)top.append($("span","mut",(t.roi>=0?"+":"")+(t.roi*100).toFixed(1)+"%"));c.append(top);
  const line=(...xs)=>{const d=$("div","simln");xs.forEach(x=>d.append(typeof x==="string"?$("span","",x):x));c.append(d)};
  line("已结算 "+t.settled+" 笔：赢 "+t.wins+" · 输 "+t.losses+(t.ties?" · 平 "+t.ties:""),"成本 "+usd(t.cost)+" → 回款 "+usd(t.payout));
  line("已确认 "+t.confirmed+" 笔","预结算 "+t.local+" 笔",...(t.mismatch?[$("b","warn","结果不一致 "+t.mismatch+" 笔")]:[]));
  const exp=$("span","","模型预期 "+money(t.expected));exp.title="同一批已结算的交易，按下单时的净优势 × 份数加总：实际盈亏长期应接近它，差得多说明模型有偏差";
  const expf=$("span","","成交时 "+money(t.expected_fill));expf.title="按每份成交那一刻的公平价算：比下单时低很多，说明挂单常在行情转向时被成交";line(exp,expf);
  line("持仓 "+t.open+" 笔（"+usd(t.open_cost)+"）","挂单中 "+t.resting+" 笔",...(t.partial?["部分成交 "+t.partial+" 笔"]:[]),"未成交作废 "+t.expired+" 笔",...(t.cancelled?["撤单 "+t.cancelled+" 笔"]:[]));
  const groups=[...s.kinds,...(s.modes.length>1?s.modes:[])];
  if(groups.length>1){const g=$("div","simg");g.append(...["类别","结算","盈亏","预期"].map(x=>$("span","lh",x)));
    groups.forEach(r=>{const v=$("span","ln",money(r.pnl));v.style.color=col(r.pnl);g.append($("span","",r.name),$("span","ln",r.settled+" 笔"),v,$("span","ln mut",money(r.expected)))});c.append(g)}
  // common risk: the open positions by the event that settles them, the worst single move first (ten markets can be one risk)
  const held=(s.groups||[]).filter(r=>r.positions);
  if(held.length){const g=$("div","simg risk");g.title="持仓按结算事件分组：同一标的的几档、同一指数同一天的几笔，一次行情一起亏；最坏单一事件 = 对这组最不利的那一个走势下的合计盈亏";
    g.append(...["共同风险","持仓","最坏单一事件",""].map(x=>$("span","lh",x)));
    held.slice(0,5).forEach(r=>{const n=$("span","",r.name);n.title=r.items.join("、");const v=$("span","ln",money(r.worst));v.style.color=col(r.worst);
      g.append(n,$("span","ln",r.positions+" 笔 "+usd(r.cost)+(r.share?"（"+(r.share*100).toFixed(0)+"%）":"")),v,$("span","mut",r.event))});
    c.append(g);if(held.length>5)c.append($("div","small mut","还有 "+(held.length-5)+" 组，见复盘页"))}
  if((s.sources||[]).length){const d=$("div","simln mut");d.title="持仓的概率依赖哪些行情源：一个源停更或出错，这些仓位的判断一起失效";
    d.append($("span","","持仓依赖的数据源："+s.sources.slice(0,4).map(x=>x.source+" "+x.trades+" 笔 "+usd(x.cost)).join(" · ")+(s.sources.length>4?" …":"")));c.append(d)}
  if((s.blocks||[]).length){const b=s.blocks[0],d=$("div","simln warn");d.title=b.why;
    d.append($("span","","⛔ 组上限 $"+s.group_cap+" 一周内拦下 "+s.blocks.length+" 笔；最近 "+b.item+" "+b.label+" @ "+cent(b.price)));c.append(d)}
  const mo=t.markout||{},mks=["1m","5m","30m"].filter(k=>mo[k]);
  if(mks.length){const d=$("div","simln");d.title="成交后 1 / 5 / 30 分钟，盘口中间价减成交价（每份）的平均：持续为负说明挂单常被更快的人吃掉旧报价，赚到的积分要先补这个";
    d.append($("span","mut","成交后市场走向："));mks.forEach(k=>{const x=$("span","",k.replace("m"," 分钟")+" "+sg(mo[k].avg)+"（"+mo[k].n+" 笔）");x.style.color=col(mo[k].avg);d.append(x)});c.append(d)}
  const det=$("details");det.open=open.has(it.name);det.addEventListener("toggle",()=>{det.open?open.add(it.name):open.delete(it.name)});
  det.append($("summary","","最近 "+s.rows.length+" 笔"));const list=$("div","simrows");
  s.rows.forEach(r=>{const a=$("a","simrow");a.href=jl.href+"#"+encodeURIComponent(r.id);a.title=(r.note?r.note+"；":"")+"点开看这笔的完整复盘";
    const st=$("b","",r.text+(r.state?" · "+r.state:""));if(r.pnl!=null)st.style.color=col(r.pnl);if(r.state==="结果不一致")st.style.color="var(--hot)";
    a.append($("span","mut",r.opened),$("span","",r.item+" "+r.label+" "+(r.price*100).toFixed(1)+"¢×"+r.shares+(r.maker&&r.shares<r.order?"/"+r.order:"")),$("span","mut",(r.edge>=0?"+":"")+(r.edge*100).toFixed(1)+"¢"),st);list.append(a)});
  det.append(list);c.append(det);return c}
function ladder(c,it){
  c.classList.add("lad");
  // a market-cap ladder: one Yes/No market per threshold; reached ones fold into one line, open ones get a row each
  const L=it.ladder,det=$("details");det.open=open.has(it.name);det.addEventListener("toggle",()=>{det.open?open.add(it.name):open.delete(it.name)});
  const sm=$("summary");sm.title="点开看计算明细";
  if(L.kind==="price")return priceLadder(c,it,L,det,sm);
  const highWord=(L.coverage||!L.bars)?"已观测最高":"窗口最高";  // "窗口最高" only once every finished hour since the opening has been read; without bars it is only ever what the bot saw
  sm.append($("span","rd",L.metric),$("span","v",L.cap),$("span","rd",highWord),$("span","v",L.high));
  if(L.sigma)sm.append($("span","rd","σ"),$("span","v",(L.sigma*100).toFixed(0)+"%"));
  det.append(sm);const dl=$("dl");const row=(k,v)=>dl.append($("dt","",k),$("dd","",v));
  row("窗口",L.window+" → "+it.close_label);row("价格",L.price+" USD（"+L.source+"）");row("供应量",L.supply+"（"+L.supply_note+"）");
  row(highWord,L.high+(L.high_at?"（"+L.high_at+"）":"")+"："+(L.bars?"GeckoTerminal 小时 K 近似（"+(L.pool_note||"最活跃的池子")+"）":"只含机器人运行以来每 10 秒看到的价格")+(L.coverage?"；"+L.coverage:"")+(L.spike?"；"+L.spike:"")+(L.gaps?"；"+L.gaps:"")+"，结算以 "+L.settle+" 1 分钟 K 为准；Predict 已结算的档位算已触及"+(L.first_skipped?"；开窗首个半小时的分钟 K 未取得，未计入":""));
  if(L.sigma)row("σ",(L.sigma*100).toFixed(0)+"%（"+L.sigma_note+"）｜剩 "+(L.years*365).toFixed(1)+" 天");
  row("模型",(L.sigma_kind==="prior"?"σ 为先验值，仅供参考。":"")+"碰到即 Yes：零漂移、固定波动率的单边触及概率 Φ((−h−s²/2)/s) + (M/K)·Φ((−h+s²/2)/s)，h = ln(K/M)，s = σ√T");
  det.append(dl);c.append(det);
  const st=$("div","lstat");  // status lines (missing / errors / prior σ / reached): a fixed band so tables line up
  if(it.missing)st.append($("p","","概率暂缺："+it.missing));else if(L.error)st.append($("div","warn small","⚠️ "+L.error));
  if(L.waiting)st.append($("div","mut small",L.waiting));  // a spec without levels of its own, before Predict lists them
  if(L.gaps&&!it.missing){const g=$("div","warn small","⚠️ "+L.gaps);g.title="这张卡没有 K 线来源，窗口最高只含机器人自己采到的价格；没采到的时段里碰到档位不会被发现，以 Predict 为准";st.append(g)}
  if(L.coverage&&!it.missing){const g=$("div","warn small","⚠️ 历史未补齐："+L.coverage);g.title="已观测最高只含已读到的小时 K 和机器人自己看到的价格；没读到的时段里碰到档位不会被发现，以 Predict 为准";st.append(g)}
  if(L.spike&&!it.missing){const g=$("div","warn small","⚠️ "+L.spike);g.title="按这个高点算已触及的档位，盘口不认同时会标“请核实”；以结算图为准";st.append(g)}
  const prior=L.sigma_kind==="prior";
  if(prior&&!it.missing){const prog=(L.sigma_note||"").match(/自采价格 ([0-9.]+)/);
    const w=$("div","warn small","⚠️ σ 暂用先验 "+(L.sigma*100).toFixed(0)+"%"+(prog?"（自采 "+prog[1]+"/12 小时）":"")+"，优势仅供参考");
    w.title=(L.bars?"K 线暂不可用"+(L.vol_error?"（"+L.vol_error+"）":""):"这条链没有 K 线来源")+"；机器人自采价格满 12 小时后自动改用实测 σ";st.append(w)}
  const done=L.rows.filter(r=>r.touched),live=L.rows.filter(r=>!r.touched);
  if(done.length){const t=$("div","touched");t.append($("span","k","✓ 已触及"));
    const tip=done.map(r=>r.label+(r.bid!=null||r.ask!=null?"（盘口 "+(r.bid==null?"无":(r.bid*100).toFixed(1))+" / "+(r.ask==null?"无":(r.ask*100).toFixed(1))+"）":"")).join("、");
    const shown=done.length>3?[{label:"≤ "+done[done.length-1].label+" · "+done.length+" 档"}]:done;  // many levels: one chip
    shown.forEach(r=>{const x=$("span","tchip",r.label+(r.settled?" · 已结算":""));x.title=(r.settled?"Predict 已把这一档结算为已触及（机器人自己的记录不一定看到）；":"")+"窗口内"+L.metric+"已达到："+tip;t.append(x)});
    st.append(t)}
  c.append(st);return priceLadderBook(c,it,L,live)}  // the same table as the price ladders
function priceLadder(c,it,L,det,sm){
  // a price ladder: one Yes/No market per level, ↑ reached by a 1-minute High, ↓ by a 1-minute Low, inside the month
  c.classList.add("price-lad");det.classList.add("price-model");
  const top=$("div","price-top");top.append($("span","plabel","现价"),$("b","",L.price));c.append(top);
  const rw=L.range_word||"本月",range=$("div","price-range"),high=$("span","",rw+"最高 "),low=$("span","","最低 ");high.append($("b","",L.high));low.append($("b","",L.low));range.append(high,low);c.append(range);
  sm.textContent="规则与计算明细";
  det.append(sm);const dl=$("dl"),row=(k,v)=>dl.append($("dt","",k),$("dd","",v));
  row("窗口",L.window+" → "+it.close_label);row("价格",L.price+"（"+L.venue+" "+L.symbol+"）");
  row(rw+"最高 / 最低",L.high+(L.high_at?"（"+L.high_at+(L.range_word?"）":" 那一小时）"):"")+" / "+L.low+(L.low_at?"（"+L.low_at+(L.range_word?"）":" 那一小时）"):"")+
    "："+(L.extremes_note||"币安小时 K 的最高/最低（与 1 分钟 K 一致）")+(L.through?"，已核至 "+L.through:"")+(L.range_word?"，加上今天的 1 分钟 K 和现价":"，加上正在走的这一小时和现价"));
  if(L.sigma)row("σ",(L.sigma*100).toFixed(0)+"%（"+L.sigma_note+"）｜剩 "+(L.years*365).toFixed(1)+" 天");
  row("规则",L.rule_note||"↑ 档：本月任一 1 分钟 K 的最高价 ≥ 档位即 Yes；↓ 档：最低价 ≤ 档位即 Yes。方向按每个市场自己的规则或标题判断");
  row("模型","零漂移、固定波动率的单边触及概率：↑ Φ((−h−s²/2)/s) + (S/K)·Φ((−h+s²/2)/s)，h = ln(K/S)；↓ Φ((−a+s²/2)/s) + (S/K)·Φ((−a−s²/2)/s)，a = ln(S/K)；s = σ√T");
  det.append(dl);c.append(det);
  const st=$("div","lstat");
  if(it.missing)st.append($("p","","概率暂缺："+it.missing));else if(L.error)st.append($("div","warn small","⚠️ "+L.error));
  if(L.hold&&!it.missing)st.append($("div","warn small","⚠️ "+L.hold));
  if(L.question)st.append($("div","qline",L.question));  // a stock: the market's question in one line
  if(L.session)st.append($("div","mut small",L.session));  // a stock: trading now, or closed until the next open
  if(L.waiting)st.append($("div","mut small",L.waiting));
  const done=L.rows.filter(r=>r.touched),live=L.rows.filter(r=>!r.touched);
  if(done.length){const t=$("details","touched"),dk=it.name+"#touched";t.open=open.has(dk);t.addEventListener("toggle",()=>{t.open?open.add(dk):open.delete(dk)});
    t.append($("summary","","✓ 已触及 "+done.length+" 档"));const ts=$("div","tchips");
    done.forEach(r=>{const x=$("span","tchip",r.label+(r.settled?" · 已结算":""));x.title=(r.settled?"Predict 已把这一档结算为已触及；":"")+(L.range_word||"本月")+(r.dir==="up"?"最高价":"最低价")+"已到 "+r.label.slice(2);ts.append(x)});t.append(ts);st.append(t)}
  c.append(st);return priceLadderBook(c,it,L,live)}
function priceLadderBook(c,it,L,live){
  const w=$("div","pb"),h=$("div","quote"),key=favKey(it);h.append(it.predict?openLink(it.predict.url):$("span","pt","Predict"));
  if(it.predict&&it.predict.error)h.append($("span","warn qe",it.predict.error));
  w.append(h);const views=new Map(live.map(r=>[r,bookView(r)]));let hot=null,hotTk=null,hotMk=null;
  // Compute opportunities from every level, including those folded out of the compact table: the best maker and the best taker.
  for(const r of live){const v=views.get(r),bar=hotFor(it,r.label);for(const e of v.ok){if(e.edge>=bar){if(!hot||e.edge>hot.edge)hot={...e,row:r.label};
    if(e.maker){if(!hotMk||e.edge>hotMk.edge)hotMk={...e,row:r.label,points:r.points_ok===true}}else if(!hotTk||e.edge>hotTk.edge)hotTk={...e,row:r.label}}}}
  if(live.length){
    // the compact view: every level whose quote earns points now or that has a suggestion, the two around the price, then the nearest up to five
    const allKey=it.name+"#all-levels",all=open.has(allKey),focus=new Set(live.filter(r=>r.points_ok===true||views.get(r).ok.length));
    const dists=live.filter(r=>r.dist!=null),above=dists.filter(r=>r.dist>0).sort((a,b)=>a.dist-b.dist)[0],below=dists.filter(r=>r.dist<=0).sort((a,b)=>b.dist-a.dist)[0];
    for(const r of [above,below])if(r)focus.add(r);  // the price keeps its neighbours however many levels earn points
    [...live].sort((a,b)=>Math.abs(a.dist??Infinity)-Math.abs(b.dist??Infinity)).forEach(r=>{if(focus.size<5)focus.add(r)});
    const shown=all||live.length<=6?live:live.filter(r=>focus.has(r));
    const tools=$("div","price-tools");tools.append($("span","hint",(L.kind==="price"?"挂单只在积分可得的档位 · ":"")+"吃单按 "+(amount||live[0].notional||100)+" U"));
    if(shown.length<live.length||all){const bt=$("button","cb",all?"收起完整档位":"全部 "+live.length+" 档（+"+(live.length-shown.length)+"）");bt.type="button";bt.setAttribute("aria-expanded",all?"true":"false");bt.title="默认显示积分可得、有建议和离现价最近的档位；展开可查看全部档位";bt.addEventListener("click",()=>{all?open.delete(allKey):open.add(allKey);if(last)render(last)});tools.append(bt)}
    w.append(tools);const g=$("div","pg");g.append($("span","lh","目标"),$("span","lh lr","模型"),$("span","lh","挂单"),$("span","lh","吃单"));
    let marked=false;const spot=() => $("div","lspot","现价 "+L.price);
    shown.forEach(r=>{const v=views.get(r),rk=key+"#"+r.label,toggle=ev=>{ev.preventDefault();openChip[rk]=openChip[rk]?null:"row";if(last)render(last)};
      if(!marked&&L.spot!=null&&r.level<=L.spot){g.append(spot());marked=true}
      const target=$("button","pcell ptarget"),dist=r.dist==null?"":(r.dist>=0?"+":"−")+(Math.abs(r.dist)*100).toFixed(Math.abs(r.dist)<.1?1:0)+"%";
      target.type="button";target.setAttribute("aria-expanded",openChip[rk]?"true":"false");target.title=r.dir_note||"查看盘口、四个方向和计算明细";
      const ltl=$("span","ltl");ltl.append($("span","lt",r.label));if(dist)ltl.append($("span","pdist",dist));const point=pointsPill(r);if(point)ltl.append(point);
      target.append(ltl);target.addEventListener("click",toggle);
      const model=$("span","pcell pmodel",r.fair==null?"—":cent(r.fair));
      const action=maker=>{const a=$("button","pcell paction"+(maker&&r.makers===false?" disabled":""));a.type="button";a.addEventListener("click",toggle);a.setAttribute("aria-expanded",openChip[rk]?"true":"false");
        const candidates=v.edges.filter(e=>e.maker===maker),e=candidates.sort((a,b)=>b.edge-a.edge)[0],eligible=!maker||r.makers===true,ok=e&&eligible&&v.ok.includes(e);
        if(maker&&!eligible){a.append($("span","pa","—"));a.title=r.bids==null&&r.asks==null?r.error||"Predict 暂无盘口":r.maker_note||r.points_note||"积分状态确认后才提示挂单优势";return a}
        if(!e){const nobook=r.bids==null&&r.asks==null;a.append($("span","pa",r.error?"⚠ 请核实":r.stale?"盘口过期":nobook?"—":r.fair==null?"模型暂缺":"暂无报价"));
          a.title=r.error||r.hold||(r.stale?"盘口过期，等待新盘口":nobook?"Predict 暂无盘口":r.fair==null?"模型价暂缺，等待行情或 σ":"等待有效盘口");return a}
        if(ok){a.classList.add("pos");if(e.edge>=HOT)a.classList.add("hot")}
        const line=$("span","pa");line.append($("b","",e.up?"Yes":"No"),$("span","",cent(e.price)),$("span","pv",sg(e.edge)));a.append(line);
        if(r.error||r.hold||r.stale)a.append($("span","pwhy",r.stale?"盘口过期":"暂不建议"));else if(ok&&!maker&&e.short)a.append($("span","pwhy","深度不足"));
        else if(!ok&&e.edge>0)a.append($("span","pwhy","低于门槛"));  // a positive edge the model's own error swallows: say so without a hover
        const bar=r.need!=null?"未过建议门槛 "+cent(r.need)+"（"+(r.swing!=null&&r.swing>=r.need-1e-9&&r.swing>0?"σ ×/÷1.25 的模型误差":"最低净优势")+"）":"未过建议门槛";
        a.title=e.label+" @ "+cent(e.price)+"；净优势 "+sg(e.edge)+(maker?"，挂单排队，成交不保证":"，约 "+qty(e.size)+" 份；已扣手续费与滑点")+(r.error?"；"+r.error:r.hold?"；"+r.hold:r.stale?"；盘口过期":!ok?"；"+bar:"");return a};
      g.append(target,model,action(true),action(false));
      if(openChip[rk]){const d=$("div","lrow");d.append(rowNote(r,L,it));if(v.edges.length)d.append(chips(rk+"/",v,{...ctxOf(r),hot:hotFor(it,r.label)}));g.append(d)}});
    if(!marked&&L.spot!=null)g.append(spot());w.append(g)
  }else if(!L.waiting)w.append($("div","price-empty","暂无待触及的档位"));
  c.append(w);const ag=ages(it);if(ag)c.append(ag);
  if(hot){c.classList.add("hot");c.title="净优势 ≥"+hotCentsFor(it,hot.row)+"¢："+hot.row+" "+hot.label+" @ "+cent(hot.price)+" "+sg(hot.edge);
    const text=e=>e.row+" "+e.label+" "+(e.price*100).toFixed(1);
    hots.push({key,name:it.name,group:it.group||"levels",url:it.predict&&it.predict.url||"",maker:hotMk?{text:text(hotMk),edge:hotMk.edge,points:hotMk.points}:null,taker:hotTk?{text:text(hotTk),edge:hotTk.edge}:null})}
  return c}
function drawOpps(){  // every red-framed suggestion on the page in one strip on top: makers (挂单) on one row, takers (吃单) on the next, largest first; tap one to jump to its card.
  // 自定义 leaves sections out, drops either side, or lists makers whatever their points; by default a maker is listed only where a
  // quote placed now earns points (a maker's edge is only real once it fills: the points are what makes the wait pay)
  const el=document.getElementById("opps"),seen=new Set(),byEdge=(a,b)=>b.pick.edge-a.pick.edge;
  const pool=hots.filter(h=>!oppOff.includes(h.group)&&!seen.has(h.key)&&seen.add(h.key));
  const makers=oppMakers?pool.filter(h=>h.maker&&(!oppPoints||h.maker.points)).map(h=>({...h,pick:h.maker})).sort(byEdge):[];
  const takers=oppTakers?pool.filter(h=>h.taker).map(h=>({...h,pick:h.taker})).sort(byEdge):[];
  const n=makers.length+takers.length;el.hidden=editing||!n;el.classList.toggle("pin",oppPin);if(el.hidden){el.replaceChildren();return}
  const foldBtn=()=>{const b=$("button","ofold",oppFold?"展开 ▾":"收起 ▴");b.type="button";b.title=oppFold?"展开机会条":"把机会条折成一行（只剩数量）";
    b.addEventListener("click",()=>{oppFold=!oppFold;keep("oppFold",oppFold);drawOpps()});return b};
  if(oppFold){const sum=$("div","osum");sum.append($("span","ok","🔥 机会 "+n),$("span","og","挂单 "+makers.length),$("span","og","吃单 "+takers.length),foldBtn());el.replaceChildren(sum);return}
  el.replaceChildren();let head=$("span","ok","🔥 机会 "+n);
  const jump=h=>{const find=()=>[...document.querySelectorAll(".card")].find(x=>x.dataset.key===h.key);let c=find();if(!c)return;
    const g=c.parentElement.id.slice(2);if(folded.includes(g)){folded=folded.filter(x=>x!==g);keep("folded",folded);if(last)render(last);c=find()}
    c.style.scrollMarginTop=(document.getElementById("fbar").offsetHeight+(oppPin&&!el.hidden?el.offsetHeight:0)+8)+"px";c.scrollIntoView({behavior:matchMedia("(prefers-reduced-motion: reduce)").matches?"auto":"smooth",block:"start"});
    c.classList.remove("flash");void c.offsetWidth;c.classList.add("flash")};
  const group=(label,list,title)=>{if(!list.length)return;const row=$("div","orow");if(head){row.append(head);head=null}
    const t=$("span","og",label+" "+list.length);t.title=title;row.append(t);
    list.forEach(h=>{  // every one, wrapping onto more lines; a tap opens the market on Predict (a new tab) and brings its card into view here
      const b=h.url?$("a","opp link"):$("button","opp");if(h.url){b.href=h.url;b.target="_blank";b.rel="noopener noreferrer"}else b.type="button";
      b.title=h.url?"在新标签页打开这个 Predict 市场，并定位到它的卡片":"跳到这张卡";b.append($("b","",h.name),$("span","",h.pick.text),$("i","",sg(h.pick.edge)));
      b.addEventListener("click",()=>jump(h));row.append(b)});
    el.append(row)};
  group("挂单",makers,"挂单机会：排队等成交，不保证成交"+(oppPoints?"；只列现在挂单能拿积分的市场":""));
  group("吃单",takers,"吃单机会：立即成交，已扣手续费与滑点");
  if(el.firstChild)el.firstChild.append(foldBtn())}
function rowNote(r,L,it){  // one line about a ladder level itself, above its four directions: on a phone nothing hovers
  const d=$("div","lnote"),parts=[];
  if(r.dist!=null)parts.push((L.kind==="price"?"现价还要"+(r.dist>=0?"涨 ":"跌 "):L.metric+"还要涨 ")+(Math.abs(r.dist)*100).toFixed(1)+"% 才碰到");
  if(L.kind==="price"&&r.dist!=null&&Math.abs(r.dist)<0.02&&L.range_word)parts.push("离档位很近，差几分钱的触及以 Predict 为准");
  if(r.fair!=null)parts.push("模型 Yes "+cent(r.fair));
  if(r.bid!=null||r.ask!=null)parts.push("Yes 盘口 "+(r.bid==null?"无":(r.bid*100).toFixed(1))+" / "+(r.ask==null?"无":(r.ask*100).toFixed(1)));
  if(r.need!=null&&r.fair!=null&&!r.touched)parts.push("建议门槛 "+cent(r.need)+(r.swing!=null&&r.swing>=r.need-1e-9&&r.swing>0?"（σ ×/÷1.25 的模型误差）":"（最低净优势）"));
  d.append($("b","",r.label),$("span","",parts.length?"："+parts.join(" · "):""));
  if(r.points_active!==undefined){const point=r.points_note||"积分状态暂缺",why=r.maker_note||r.points_why;d.append($("div","",point+(r.points_active===true&&r.points_rate!=null?" · "+qty(r.points_rate)+" PP/小时":"")));
    if(r.points_ok===false&&why&&why!==point)d.append($("div","warn",why))}
  if(r.error)d.append($("span","warn","⚠️ "+r.error));
  if(r.dir_note)d.append($("span","warn",r.dir_note));
  if(r.hold&&!r.error&&r.hold!==r.dir_note)d.append($("span","warn","暂不建议："+r.hold));
  else if(r.stale&&!r.error)d.append($("span","warn","盘口过期：不给建议"));
  if(it&&!r.touched){const hs=$("div","hotrow");hs.append(hotInput("row:"+favKey(it)+"#"+r.label,hotCentsFor(it),"这一档的红框门槛（空 = 跟随这张卡 / 栏目 / 全局）"),$("span","mut","这一档"));d.append(hs)}
  return d}
function card(it,g){
  const c=$("div","card"+(it.missing?" missing":"")),head=$("div","head"),nm=$("div","name",it.name);
  nm.title=it.symbol||it.name;const fk=favKey(it),on=favs.includes(fk),st=$("button","star"+(on?" on":""),on?"★":"☆");
  c.dataset.key=fk;hotByCard[fk]=hotFor(it);
  st.type="button";st.title=on?"取消收藏":"收藏（排到最前）";st.setAttribute("aria-label",st.title);st.addEventListener("click",e=>{e.preventDefault();toggleFav(fk)});
  const grip=$("button","grip","⠿");grip.type="button";grip.title="按住拖动，调整顺序";grip.setAttribute("aria-label","拖动排序");
  grip.addEventListener("pointerdown",e=>startDrag(e,c,g));
  if(editing){  // 自定义: every card can move (drag, ◀ ▶) and be hidden or shown again
    const bar=$("div","ctl"),off=hidden.includes(fk),keys=keysOrder(g),i=keys.indexOf(fk);
    bar.append(grip,ctlBtn("◀","前移",()=>nudge(g,fk,-1),i<=0),ctlBtn("▶","后移",()=>nudge(g,fk,1),i<0||i>=keys.length-1),
      hotInput("card:"+fk,hotMap["sec:"+(it.group||"contract")]??hotCents,"这张卡的红框门槛（空 = 跟随栏目 / 全局）"),$("span","sp"),
      ctlBtn(off?"显示":"隐藏",off?"恢复显示这张卡":"隐藏这张卡（在自定义里可恢复）",()=>toggleHidden(fk)));
    c.append(bar);if(off)c.classList.add("off")}
  const tg=$("div","tags");head.append(...(g==="fav"&&!editing?[grip]:[]),st,nm,tg);  // tags wrap under the full name when the card is narrow
  if(it.day){const t=$("span","tag"+(it.day_ahead>0?" next":""),(!it.day_tag?it.day_label:it.day_tag==="今天"&&(it.trading||it.auction)?it.day_label.split(" ")[0]:it.day_label.split(" ")[0]+" "+it.day_tag));t.title="交易日 "+it.day_label;tg.append(t);
    const k=it.name+"|"+(it.symbol||"");if(seen[k]&&seen[k]<it.day)rolled[k]=Date.now();seen[k]=it.day;
    if(rolled[k]&&Date.now()-rolled[k]<600000){c.classList.add("rolled");t.className="tag new";t.textContent+=" 新"}}
  if(it.auction){const a=$("span","tag auc","集合竞价");a.title=it.auction+"：此时价格基本就是收盘价";tg.append(a)}
  else if(it.preopen){const ph=it.preopen_phase||"",a=$("span","tag auc","竞价·"+(ph||"进行中"));
    a.title=it.preopen+"："+({"可撤单":"还能撤单，参考价常是试探：只展示，概率仍按币安代理算"+(it.preopen.startsWith("韩交所")?"；韩股盘前显示的价格也可能是 Nextrade 盘前成交，以 09:00（首尔）撮合的开盘价为准":""),"不可撤单":"不能撤单了，参考价比之前可信，加单仍会改变它，概率按它算","随机撮合":"随机撮合中，概率按参考价算","已撮合":"开盘价已定，概率按它算，连续交易 09:30 开始"}[ph]||"")+"；竞价高开或低开不等于当天收涨或收跌";tg.append(a)}
  else if(it.trading){const a=$("span","tag "+(it.trading==="开盘中"?"open":"lunch"),it.trading);
    a.title={"开盘中":"交易所连续交易中：直接用现货相对昨收","午休":"午间休市","未开盘":"今日尚未开盘：按代理估算",
      "已收盘":"今日已收盘","休市":"今天不是交易日"}[it.trading]||"";tg.append(a)}
  if(it.close_ms){const cd=$("span","cd");cd.dataset.close=it.close_ms;cd.title="目标 "+it.close_label;tg.append(cd)}
  c.append(head);
  if(it.kind==="sim")return simCard(c,it);
  if(it.kind==="ladder")return ladder(c,it);
  const v=view(it),best=v.best,hb=hotFor(it);  // for the trade size picked in the bar; hb = the red-frame bar of this card
  if(best&&best.edge>=hb){c.classList.add("hot");c.title="净优势 ≥"+hotCentsFor(it)+"¢："+best.label+" @ "+cent(best.price)+" +"+cent(best.edge);
    const by=(a,b)=>b.edge-a.edge,mk=v.ok.filter(e=>e.maker).sort(by)[0],tk=v.ok.filter(e=>!e.maker).sort(by)[0],text=e=>e.label+" "+(e.price*100).toFixed(1);
    hots.push({key:fk,name:it.name,group:it.group||"contract",url:it.predict&&it.predict.url||"",maker:mk&&mk.edge>=hb?{text:text(mk),edge:mk.edge,points:!!(it.predict&&it.predict.points_ok===true)}:null,
               taker:tk&&tk.edge>=hb?{text:text(tk),edge:tk.edge}:null})}
  if(it.missing){c.append($("p","","概率暂缺："+it.missing));tail(c,it);return c}
  const o=$("div","odds"),a=$("b",style==="us"?"d":"u"),b=$("b",style==="us"?"u":"d");
  const lb=it.labels||["涨","跌"];a.append($("span","lbl",lb[0]),pct(it.fair_up)+"¢");b.append(pct(it.fair_down)+"¢",$("span","lbl",lb[1]));
  const bar=$("div","bar");[[it.up,upColor()],[it.flat,"var(--flat)"],[it.down,downColor()]].forEach(([w,col])=>{const i=$("i");i.style.width=(w*100)+"%";i.style.background=col;bar.append(i)});
  const sw=it.predict&&it.predict.swing;if(sw>0)o.title="模型误差约 ±"+cent(sw)+"（σ ×/÷1.25、代理系数、漂移口径各变一次取最大）：优势不超过它的方向不算建议；/calib 看模型是否真的赢过市场";
  o.append(a,bar,b);c.append(o);const ch=changedAt[fk];if(ch&&Date.now()-ch.at<2500)o.classList.add(ch.up===(style!=="us")?"chg-r":"chg-g");  // a moved fair price flashes once, in the scheme's colour
  const unit=it.unit?" "+it.unit:"";
  if(it.touch){const t=it.touch,det=$("details");det.open=open.has(it.name);det.addEventListener("toggle",()=>{det.open?open.add(it.name):open.delete(it.name)});
    const sm=$("summary"),sg=x=>(x>=0?"+":"")+x.toFixed(1)+"%";sm.title="点开看计算明细";
    sm.append($("span","rd",t.coin),$("span","v",t.price),$("span","rd","距"+lb[1]),$("span","v",sg(t.to_low)),$("span","rd","距"+lb[0]),$("span","v",sg(t.to_high)));det.append(sm);
    const dl=$("dl");const row=(k,v)=>dl.append($("dt","",k),$("dd","",v));
    row("截止",it.close_label);row("先 "+lb[0],(t.p_high*100).toFixed(2)+"%");row("先 "+lb[1],(t.p_low*100).toFixed(2)+"%");row("都没碰到",(t.p_none*100).toFixed(2)+"%（各算一半）");
    row("σ","30 日小时收盘年化 "+(t.sigma*100).toFixed(1)+"%｜剩 "+(t.years*365).toFixed(1)+" 天");row("开盘以来",t.status);
    row("模型","双边界首达，连续路径、固定波动率、零漂移；公平价 = P(先触该边) + ½·P(都没碰到)");
    det.append(dl);c.append(det);if(t.error)c.append($("div","warn small","⚠️ 币安刷新失败："+t.error));
    if(t.status.startsWith("需人工核对"))c.append($("div","warn small","⚠️ "+t.status));
    else if(t.hold)c.append($("div","warn small","⚠️ "+t.hold));
    tail(c,it);return c}
  if(it.flip){const f=it.flip,det=$("details");det.open=open.has(it.name);det.addEventListener("toggle",()=>{det.open?open.add(it.name):open.delete(it.name)});
    // a flip market: A has to close a minute above B; the ratio and how far it still has to climb
    const sm=$("summary");sm.title="点开看计算明细";
    sm.append($("span","rd",f.coin),$("span","v",f.a),$("span","rd",f.other),$("span","v",f.b),$("span","rd","比"),$("span","v",f.ratio==null?"—":f.ratio.toFixed(4)));
    if(f.gap!=null&&f.gap>0){const ch=$("span","chip","差 +"+(f.gap*100).toFixed(1)+"%");ch.title=f.coin+" 相对 "+f.other+" 还要涨这么多才反超";sm.append(ch)}
    det.append(sm);const dl=$("dl");const row=(k,v)=>dl.append($("dt","",k),$("dd","",v));
    row("窗口",f.window);row("规则","Hyperliquid 永续，同一分钟的 1 分钟 K 收盘 "+f.coin+" > "+f.other+" 即 Yes；只看每个币的单价");
    row("σ","30 日小时收盘的 ln("+f.coin+"/"+f.other+") 年化 "+(f.sigma*100).toFixed(1)+"%｜剩 "+(f.years*365).toFixed(1)+" 天");
    row("窗口以来",f.status);row("模型","比值单边触及 1：零漂移、固定波动率，Φ((−h−s²/2)/s) + R·Φ((−h+s²/2)/s)，h = ln(1/R)，s = σ√T");
    det.append(dl);c.append(det);if(f.error)c.append($("div","warn small","⚠️ Hyperliquid 刷新失败："+f.error));
    if(f.hold)c.append($("div","warn small","⚠️ "+f.hold));
    tail(c,it);return c}
  const det=$("details");det.open=open.has(it.name);det.addEventListener("toggle",()=>{det.open?open.add(it.name):open.delete(it.name)});
  const sm=$("summary");sm.title=(it.ref_day?it.ref_day+" 收盘 → 有效价":"参考 → 有效价")+"；点开看计算明细";
  // 昨收 1,768,000 · 今日 1,769,000 (while trading) / 今收 … · 估算 … (after the close: the proxy's view of the next close)
  sm.append($("span","rd",it.ref_rel||it.ref_day||"参考"),$("span","v",it.ref),$("span","rd sep",it.eff_label||"→"),$("span","v",like(it.effective,it.ref)));
  if(it.unit)sm.append($("span","un",it.unit));
  if(it.preopen_price&&it.eff_label!=="竞价"&&it.eff_label!=="开盘价"){const pv=$("span","v",it.preopen_price);pv.title="开市前竞价的参考价（"+(it.preopen_phase||"")+"阶段）：只展示，概率按币安代理算";sm.append($("span","rd sep","竞价"),pv,$("span","un",it.preopen_phase||""))}
  sm.title=(it.ref_day?it.ref_day+" 收盘 ":"参考 ")+it.ref+unit+"；"+(it.eff_label==="今日"?"今日现价":it.eff_label==="竞价"?"开市前竞价参考价（撮合前会变）":it.eff_label==="开盘价"?"已撮合的开盘价（连续交易前）":"按代理估算的下一收盘")+" "+it.effective+unit+"；点开看计算明细";
  const chip=$("span","chip",(it.move>=0?"+":"")+it.move.toFixed(2)+"%");chip.style.color=it.move>0?upColor():it.move<0?downColor():"var(--muted)";sm.append(chip);det.append(sm);
  const dl=$("dl");const row=(k,v)=>dl.append($("dt","",k),$("dd","",v));
  row("目标",it.close_label);row("参考",it.ref+unit+"（"+it.ref_note+"）");row("有效",it.effective+unit);row("代理",it.proxy_note);
  row("σ","日 "+(it.sigma_daily*100).toFixed(2)+"% × √"+it.remaining.toFixed(3)+" = "+(it.sigma*100).toFixed(2)+"%");
  row("σ 来源",it.sigma_note);row("涨/平/跌",(it.up*100).toFixed(2)+"% / "+(it.flat*100).toFixed(2)+"% / "+(it.down*100).toFixed(2)+"%");row("z",it.z.toFixed(3));
  det.append(dl);c.append(det);if(it.warn){const w=$("div","warn small","⚠️ "+it.warn);c.append(w)}
  tail(c,it);return c}
function tail(c,it){const ag=ages(it);if(ag)c.append(ag);if(it.predict)c.append(book(it.predict,favKey(it),it.pages))}
function tick(){
  const now=Date.now()+skew;
  document.querySelectorAll(".cd").forEach(el=>{const left=Math.floor((Number(el.dataset.close)-now)/1000);
    if(left<=0){el.className="cd done";el.textContent="已到收盘";return}
    const d=Math.floor(left/86400),h=Math.floor(left%86400/3600),m=Math.floor(left%3600/60),s=left%60;
    el.className="cd"+(left<1800?" soon":"");el.textContent="⏳ "+(d?d+"天 ":"")+two(h)+":"+two(m)+":"+two(s)});
  const ago=document.getElementById("ago");if(ago&&fetchedAt)ago.textContent=Math.max(0,Math.round((Date.now()-fetchedAt)/1000))+" 秒前刷新";
  document.querySelectorAll(".age").forEach(el=>{const a=Math.max(0,now-Number(el.dataset.ms));
    el.textContent=a<60e3?Math.round(a/1000)+" 秒前":a<3600e3?Math.floor(a/60e3)+" 分钟前":Math.floor(a/3600e3)+" 小时前";el.classList.toggle("old",a>Number(el.dataset.old))});
  drawStale()}
function drawStale(){  // a failed refresh says the cards are old; past STALE_MS their suggestions stop being highlighted
  const el=document.getElementById("stale"),gone=okAt?Date.now()-okAt:Infinity,old=gone>STALE_MS,bad=!!failMsg||old&&!!okAt;
  document.body.classList.toggle("olddata",old&&(!!okAt||!!failMsg));el.hidden=!bad;if(!bad)return;
  const at=okAt?new Date(okAt):null,hms=at?two(at.getHours())+":"+two(at.getMinutes())+":"+two(at.getSeconds()):"";
  document.getElementById("stalemsg").textContent="⚠️ "+(failMsg?"刷新失败（"+failMsg+"），":"")+(okAt?"当前为旧数据：最后成功 "+hms+"（"+Math.round(gone/1000)+" 秒前）":"还没取到数据")+
    (old?"；建议高亮已撤掉":"，"+Math.max(0,Math.ceil((STALE_MS-gone)/1000))+" 秒后撤掉建议高亮")}
let plan={};  // section -> the card keys it shows, for the ◀ ▶ buttons while a render is being built
function keysOrder(g){return plan[g]||[]}
function scrollMarks(){  // where each card's table (.pg, scrolling inside a wide 市值阶梯 card) stands, by card key
  return new Map([...document.querySelectorAll(".card .pg")].filter(e=>e.scrollTop>0).map(e=>[e.closest(".card").dataset.key,e.scrollTop]))}
function restoreScroll(marks){if(marks.size)document.querySelectorAll(".card .pg").forEach(e=>{const t=marks.get(e.closest(".card").dataset.key);if(t)e.scrollTop=t})}
function render(d){
  if(drag||hotTyping()){pending=d;return}  // never rebuild the cards under a drag or while a ¢ box is being typed in; the latest data is drawn when that ends
  const marks=scrollMarks();drawBar();hots=[];
  const flat=!editing&&(filt.length>0||sortBy!=="");document.body.classList.toggle("flatview",flat);
  const fh=document.getElementById("h-flat"),fg=document.getElementById("g-flat");fh.hidden=fg.hidden=!flat;
  if(flat){  // every visible card that passes the bar, in one list (cards and sections hidden in 自定义 stay hidden)
    const got=new Set(),pool=[...favs.map(k=>d.items.find(i=>favKey(i)===k)).filter(Boolean),
      ...d.items.filter(i=>!favs.includes(favKey(i))&&!hideSec.includes(i.group||"contract"))].filter(i=>!hidden.includes(favKey(i))&&!got.has(favKey(i))&&got.add(favKey(i)));
    const hits=pool.map((it,i)=>({it,i,score:matches(it)})).filter(x=>x.score!==null&&(filt.length||x.it.kind!=="sim"));
    if(sortBy==="edge")hits.sort((a,b)=>b.score-a.score||a.i-b.i);
    else if(sortBy==="time")hits.sort((a,b)=>(a.it.close_ms||Infinity)-(b.it.close_ms||Infinity)||a.i-b.i);
    const clear=ctlBtn("清除筛选","回到按栏目分组的页面",()=>{filt=[];sortBy="";keep("filt",filt);keep("sort",sortBy);if(last)render(last)});
    fh.replaceChildren($("span","hn",(filt.length?"筛选结果":"全部卡片")+" "+hits.length+" 张"+(sortBy==="edge"?" · 按净优势":sortBy==="time"?" · 按剩余时间":"")),clear);
    fg.replaceChildren(...(hits.length?hits.map(x=>card(x.it,"flat")):[$("p","mut","没有符合条件的卡片")]));
    restoreScroll(marks);drawOpps();tick();return}
  // starred cards leave their own section for the one on top, in the order the viewer keeps them (drag ⠿ to change);
  // hidden cards and sections are left out, except in 自定义 where they show faded so they can be brought back
  const shown=i=>editing||!hidden.includes(favKey(i));
  const lists={fav:favs.map(k=>d.items.find(i=>favKey(i)===k)).filter(i=>i&&shown(i))};
  for(const g of SECTIONS){const own=arrange(d.items.filter(i=>(i.group||"contract")===g&&!favs.includes(favKey(i))&&shown(i)),g);lists[g]=editing?own:sortSection(own,g)}
  plan=Object.fromEntries(Object.entries(lists).map(([g,l])=>[g,l.map(favKey)]));
  const vis=secOrder.filter(s=>lists[s].length),foot=document.getElementById("foot");
  for(const[g,items]of Object.entries(lists)){
    const grid=document.getElementById("g-"+g),h=document.getElementById("h-"+g),off=g!=="fav"&&hideSec.includes(g),i=vis.indexOf(g);
    const fold=!editing&&folded.includes(g);  // 自定义 shows every section open, so cards can be arranged
    grid.replaceChildren(...items.map(it=>card(it,g)));
    h.hidden=!items.length||(off&&!editing);grid.hidden=h.hidden||fold;grid.classList.toggle("off",off);
    grid.classList.toggle("wide",["levels","ladder","sim"].includes(g)&&!oneRow.includes(g));  // 自定义 "整行显示": one wide card per row, as in the starred section
    const name=$("span","hn",SEC_NAMES[g]+(off?"（已隐藏）":""));let head=name,sum=null;
    if(!editing){head=$("button","fold");head.type="button";head.setAttribute("aria-expanded",fold?"false":"true");
      head.title=fold?"展开这一栏":"折叠这一栏（标题留着，写明张数和红框机会数）";head.append(name);head.addEventListener("click",()=>toggleFold(g))}
    if(fold){const hot=grid.querySelectorAll(".card.hot").length;sum=$("span","fs",items.length+" 张");if(hot)sum.append(" · ",$("b","","🔥 "+hot))}
    const sg=editing&&g!=="fav"?$("button","grip","⠿"):null;  // 自定义: drag the title to move the whole section (↑ ↓ do the same)
    if(sg){sg.type="button";sg.title="按住拖动，调整栏目顺序";sg.setAttribute("aria-label","拖动栏目");sg.addEventListener("pointerdown",e=>startSecDrag(e,g))}
    h.replaceChildren(...(sg?[sg]:[]),head,...(sum?[sum]:[]),
      ...(editing&&g!=="fav"?[ctlBtn("↑","栏目上移",()=>moveSec(g,-1),i<=0),ctlBtn("↓","栏目下移",()=>moveSec(g,1),i<0||i>=vis.length-1)]:[]));
    if(!editing&&SEC_SORTS[g]&&items.length>1){  // the ladder sections: a sort of their own (the filter bar's sort flattens the page instead)
      const l=$("label","fsort secsort"),s=$("select");l.append("排序 ",s);SEC_SORTS[g].forEach(([v,t])=>{const o=$("option","",t);o.value=v;s.append(o)});s.value=secSort[g]||"";
      s.title="这一栏卡片的排列顺序（只影响本浏览器；默认顺序 = 自定义里拖出来的顺序）";l.addEventListener("click",e=>e.stopPropagation());
      s.addEventListener("change",()=>{secSort={...secSort,[g]:s.value};keep("secsort",secSort);if(last)render(last)});h.append(l)}
    if(editing&&g!=="fav"&&g!=="sim")h.append(hotInput("sec:"+g,hotCents,"这一栏的红框门槛（空 = 全局）"))}  // 自定义: the section's own bar
  const now=[...document.querySelectorAll(".wrap>.grid")].map(e=>e.id.slice(2)).filter(g=>g!=="fav");
  if(now.join()!==secOrder.join())for(const g of secOrder)foot.before(document.getElementById("h-"+g),document.getElementById("g-"+g));
  restoreScroll(marks);drawOpps();tick()}
function drawLegend(){
  const lg=document.getElementById("legend");const sw=$("span","sw");[["涨",upColor()],["平","var(--flat)"],["跌",downColor()]].forEach(([t,col])=>{const i=$("i");i.style.background=col;sw.append(i,t)});
  const own=Object.keys(hotMap).length,hot=$("span","sw hot");hot.append($("i"),"红框 = 净优势 ≥"+hotCents+"¢（高亮门槛"+(own?"，另有 "+own+" 处单独设置":"")+"）");
  hot.title="可在 ✎ 自定义 里修改，栏目、卡片、档位还能各设各的；和建议门槛不是一回事：没超过建议门槛的方向不会被建议";
  const rule=$("span","","¢ 公平价 · 净优势 = 公平价 − 成交价 − 费用");rule.title="挂涨@买1 · 挂跌@1−卖1 · 吃涨@卖1 · 吃跌@1−买1；吃单另扣手续费和按单笔金额吃到的深度；加框的是建议方向，灰色的优势不够大（要超过最低净优势和模型误差中较大的那个），不建议；平盘两边各半";
  lg.replaceChildren(sw,rule,hot)}
function drawPanel(){
  const secs=document.getElementById("secs");secs.replaceChildren($("span","mut","显示的栏目："));
  secOrder.forEach(g=>{const l=$("label","tog"),i=$("input");i.type="checkbox";i.checked=!hideSec.includes(g);i.dataset.sec=g;
    i.addEventListener("change",()=>{hideSec=i.checked?hideSec.filter(x=>x!==g):[...hideSec,g];keep("hideSec",hideSec);if(last)render(last)});
    l.append(i,SEC_NAMES[g]);secs.append(l)});
  // every card, by section: untick to hide it (the same as the card's own 隐藏 button), tick to bring it back
  const cl=document.getElementById("cards");cl.replaceChildren($("span","mut","显示的卡片："));
  if(last)for(const g of secOrder){const items=last.items.filter(it=>(it.group||"contract")===g);if(!items.length)continue;
    const grp=$("span","cgrp");grp.append($("span","mut",SEC_NAMES[g]+"："));
    items.forEach(it=>{const k=favKey(it),l=$("label","tog"),i=$("input");i.type="checkbox";i.checked=!hidden.includes(k);i.dataset.card=k;
      i.addEventListener("change",()=>{hidden=i.checked?hidden.filter(x=>x!==k):[...hidden,k];keep("hidden",hidden);drawPanel();if(last)render(last)});
      l.append(i,it.name);grp.append(l)});cl.append(grp)}
  const orw=document.getElementById("onerow");orw.replaceChildren($("span","mut","整行显示（每行一张卡）："));
  ["levels","ladder"].forEach(g=>{const l=$("label","tog"),i=$("input");i.type="checkbox";i.checked=oneRow.includes(g);i.dataset.row=g;
    i.addEventListener("change",()=>{oneRow=i.checked?[...oneRow,g]:oneRow.filter(x=>x!==g);keep("oneRow",oneRow);if(last)render(last)});
    l.append(i,SEC_NAMES[g]);l.title="这一栏每行只放一张卡，表格和收藏栏里一样宽";orw.append(l)});
  const os=document.getElementById("oppsrc");os.replaceChildren($("span","mut","🔥 机会条列出："));
  SECTIONS.filter(g=>g!=="sim").forEach(g=>{const l=$("label","tog"),i=$("input");i.type="checkbox";i.checked=!oppOff.includes(g);i.dataset.opp=g;
    i.addEventListener("change",()=>{oppOff=i.checked?oppOff.filter(x=>x!==g):[...oppOff,g];keep("oppOff",oppOff);if(last)render(last)});l.append(i,SEC_NAMES[g]);os.append(l)});
  const ow=document.getElementById("oppway");ow.replaceChildren($("span","mut","机会条的方向："));
  [["opp-maker","挂单",oppMakers,v=>{oppMakers=v;keep("oppMakers",v)},"列出挂单机会（排队等成交，不保证成交）"],
   ["opp-taker","吃单",oppTakers,v=>{oppTakers=v;keep("oppTakers",v)},"列出吃单机会（立即成交，已扣手续费与滑点）"],
   ["opp-points","挂单只列积分可得的",oppPoints,v=>{oppPoints=v;keep("oppPoints",v)},"挂单机会只列现在挂单能拿积分的市场（蓝色 ● 的那些）；勾掉则所有标红框的挂单都列"],
   ["opp-pin","固定在顶部",oppPin,v=>{oppPin=v;keep("oppPin",v)},"滚动时机会条一直贴在筛选栏下面（右侧“收起”可折成一行）；勾掉则随页面滚走"]
  ].forEach(([id,label,on,set,title])=>{const l=$("label","tog"),i=$("input");i.type="checkbox";i.id=id;i.checked=on;l.title=title;
    i.addEventListener("change",()=>{set(i.checked);if(last)render(last)});l.append(i,label);ow.append(l)});
  document.getElementById("hotin").value=hotCents;
  document.getElementById("hidn").textContent=hidden.length?"已隐藏 "+hidden.length+" 张卡片（变淡显示，点“显示”恢复）":"没有隐藏的卡片";
  document.getElementById("showall").disabled=!hidden.length}
function setEditing(on){
  editing=on;document.getElementById("custom").hidden=!on;
  const b=document.getElementById("edit");b.classList.toggle("on",on);b.textContent=on?"✓ 完成":"✎ 自定义";
  if(on)drawPanel();if(last)render(last)}
document.getElementById("edit").addEventListener("click",()=>setEditing(!editing));
document.getElementById("journal").href=location.pathname.replace(/\\/$/,"")+"/journal";
document.getElementById("done").addEventListener("click",()=>setEditing(false));
document.getElementById("hotin").addEventListener("change",e=>{const v=Math.round(Number(e.target.value));
  if(e.target.value.trim()!==""&&Number.isFinite(v)){hotCents=Math.min(50,Math.max(1,v));HOT=hotCents/100;keep("hot",hotCents)}
  e.target.value=hotCents;drawLegend();if(last)render(last)});
document.getElementById("showall").addEventListener("click",()=>{hidden=[];keep("hidden",hidden);drawPanel();if(last)render(last)});
let armed=0;  // 恢复默认布局 takes a second click within 4 s: no dialog, which some in-app browsers never show
document.getElementById("reset").addEventListener("click",e=>{const b=e.currentTarget,idle=()=>{b.textContent="恢复默认布局";b.classList.remove("arm")};
  if(Date.now()-armed>4000){armed=Date.now();b.textContent="再点一次确认";b.classList.add("arm");setTimeout(()=>{if(Date.now()-armed>=4000)idle()},4100);return}
  armed=0;idle();  // the layout only: stars stay, their order too
  order={};hidden=[];hideSec=["sim","levels"];secOrder=[...SECTIONS];hotCents=10;HOT=.1;oppOff=[];oppMakers=oppTakers=oppPoints=true;oppPin=true;oppFold=false;folded=[];oneRow=[];secSort={};hotMap={};
  ["order","hidden","secs","hot","oppOff","oppTaker","oppMakers","oppTakers","oppPoints","oppPin","oppFold","folded","oneRow","secsort","hotmap"].forEach(k=>{try{localStorage.removeItem(k)}catch(e){}});keep("hideSec",hideSec);
  drawPanel();drawLegend();if(last)render(last)});
let loading=null,lastSig="",dead=false;  // the fetch in flight: a slow answer never piles up behind the next tick, and a hung one is cut off
const LOAD_TIMEOUT_MS=8000;
function load(){
  if(loading||dead)return loading;
  const ctl=new AbortController(),timer=setTimeout(()=>ctl.abort(),LOAD_TIMEOUT_MS);
  loading=(async()=>{try{
    const r=await fetch(location.pathname.replace(/\\/$/,"")+"/data.json",{cache:"no-store",signal:ctl.signal});
    if(!r.ok)throw new Error("HTTP "+r.status);
    const d=await r.json();if(d.server_ms)skew=d.server_ms-Date.now();fetchedAt=okAt=Date.now();failMsg="";style=d.color_style||"cn";
    if(last)for(const it of d.items){const k=favKey(it),o=last.items.find(x=>favKey(x)===k);  // a fair price that moved since the last refresh
      if(o&&o.fair_up!=null&&it.fair_up!=null&&Math.abs(o.fair_up-it.fair_up)>=5e-4)changedAt[k]={at:Date.now(),up:it.fair_up>o.fair_up}}
    const sig=JSON.stringify(d.items),same=!!last&&sig===lastSig;lastSig=sig;  // books move every 15 s, crypto quotes every 30 s: a repeat
    last=d;if(!same||drag)render(d);                                            // rebuilds nothing (a drag still notes it); the stream patches the daily cards between answers
    drawMeta(d);
    if(!same)drawLegend();
    document.getElementById("foot").textContent=d.note;tick();
  }catch(e){failMsg=e.name==="AbortError"?"超过 "+LOAD_TIMEOUT_MS/1000+" 秒没有响应":(e.message||"网络错误");
    if(e.message==="HTTP 404"){dead=true;failMsg="这个链接已失效（令牌已更换或网页已关闭）：请在 Telegram 重新发送 /web 取得新链接；本页不再自动刷新"}
    if(!okAt){document.getElementById("meta").replaceChildren($("span","warn","刷新失败："+failMsg+(dead?"":"，稍后自动重试")));document.querySelectorAll(".skel").forEach(e=>e.remove())}drawStale()}
  finally{clearTimeout(timer);loading=null}})();
  return loading}
function drawMeta(d){const ago=$("span","","");ago.id="ago";
  document.getElementById("meta").replaceChildren(...(d.today?[$("span","","今天 "+d.today)]:[]),$("span","","数据 "+d.generated_at),ago,
    ...(live?[$("span","","实时推送")]:[]),$("span","","基准 "+d.mode),$("span","","v"+d.version))}
// The event stream (/events): the index and contract cards' numbers arrive within a second of a change and are patched into the
// last full answer; data.json still brings the books, the crypto cards and the texts every 10 seconds, and is the fallback.
let es=null,live=false;
const LIVE_KEYS=["quote_ms","effective","move","ref","up","flat","down","z","fair_up","fair_down","sigma","remaining","warn","proxy_note","source","eff_label","missing"];
function patchLive(d){
  if(!last||!Array.isArray(d.items))return;if(d.server_ms)skew=d.server_ms-Date.now();
  let changed=false;
  for(const li of d.items){const it=last.items.find(x=>x.name===li.name&&(x.symbol||"")===(li.symbol||""));if(!it)continue;
    if(!!li.missing!==!!it.missing)continue;  // a card that just appeared or paused: the next full answer draws it whole
    for(const k of LIVE_KEYS){if(!(k in li)||JSON.stringify(li[k])===JSON.stringify(it[k]))continue;
      if(k==="fair_up"&&it.fair_up!=null&&li.fair_up!=null&&Math.abs(li.fair_up-it.fair_up)>=5e-4)changedAt[favKey(it)]={at:Date.now(),up:li.fair_up>it.fair_up};
      it[k]=li[k];changed=true}}
  if(!changed)return;if(d.generated_at)last.generated_at=d.generated_at;fetchedAt=okAt=Date.now();failMsg="";
  if(document.visibilityState!=="hidden")render(last);drawMeta(last);tick()}
function connectLive(){
  if(!("EventSource" in window)||dead||es)return;
  try{es=new EventSource(location.pathname.replace(/\\/$/,"")+"/events")}catch(e){es=null;return}
  es.addEventListener("live",e=>{live=true;try{patchLive(JSON.parse(e.data))}catch(err){}});
  es.onerror=()=>{live=false;if(last)drawMeta(last);if(dead&&es){es.close();es=null}}}  // EventSource reconnects on its own; the polling carries on meanwhile
function drawBar(){  // the filter chips, the sort and the trade size, as this browser keeps them
  const fc=document.getElementById("fchips");fc.replaceChildren(...FILTERS.map(([k,t,tip])=>{const b=$("button","tog"+(filt.includes(k)?" on":""),t);b.type="button";b.title=tip;
    b.setAttribute("aria-pressed",filt.includes(k)?"true":"false");
    b.addEventListener("click",()=>{filt=filt.includes(k)?filt.filter(x=>x!==k):[...filt,k];if(k==="maker"&&filt.includes("maker"))filt=filt.filter(x=>x!=="taker");
      if(k==="taker"&&filt.includes("taker"))filt=filt.filter(x=>x!=="maker");keep("filt",filt);if(last)render(last)});return b}));
  document.getElementById("sortsel").value=sortBy;
  const base=(last&&last.notional)||100,cur=amount||base;
  document.getElementById("amts").replaceChildren(...[50,100,500].map(n=>{const b=$("button","tog"+(cur===n?" on":""),String(n));b.type="button";
    b.addEventListener("click",()=>{amount=n===base?0:n;keep("amount",amount);document.getElementById("amtin").value="";if(last)render(last)});return b}));
  const ai=document.getElementById("amtin");if(document.activeElement!==ai)ai.value=[50,100,500].includes(cur)?"":cur}
const sb=document.getElementById("showbook");try{sb.checked=localStorage.getItem("showbook")!=="0"}catch(e){}
const applyBook=()=>document.body.classList.toggle("nobook",!sb.checked);applyBook();
sb.addEventListener("change",()=>{applyBook();try{localStorage.setItem("showbook",sb.checked?"1":"0")}catch(e){}});
document.getElementById("sortsel").addEventListener("change",e=>{sortBy=e.target.value;keep("sort",sortBy);if(last)render(last)});
document.getElementById("amtin").addEventListener("change",e=>{const v=Math.round(Number(e.target.value));
  if(e.target.value.trim()!==""&&Number.isFinite(v)&&v>=1){const base=(last&&last.notional)||100;amount=v===base?0:Math.min(v,1e6);keep("amount",amount)}if(last)render(last)});
document.getElementById("retry").addEventListener("click",()=>load());
document.getElementById("theme").addEventListener("click",()=>{const ks=Object.keys(THEMES);theme=ks[(ks.indexOf(theme)+1)%ks.length];keep("theme",theme);applyTheme()});applyTheme();
const fbarEl=document.getElementById("fbar");  // a shadow under the bar once it sticks to the top
if("IntersectionObserver" in window)new IntersectionObserver(([e])=>fbarEl.classList.toggle("stuck",!e.isIntersecting),{threshold:0}).observe(document.getElementById("fsent"));
function setTopH(){const st=document.getElementById("stale");document.documentElement.style.setProperty("--top-h",(fbarEl.offsetHeight+(st.hidden?0:st.offsetHeight))+"px")}  // the pinned strip sits right under the bar
setTopH();addEventListener("resize",setTopH);if("ResizeObserver" in window)new ResizeObserver(setTopH).observe(fbarEl);
const totop=document.getElementById("totop");addEventListener("scroll",()=>totop.classList.toggle("show",scrollY>600),{passive:true});
totop.addEventListener("click",()=>scrollTo({top:0,behavior:matchMedia("(prefers-reduced-motion: reduce)").matches?"auto":"smooth"}));
document.addEventListener("visibilitychange",()=>{if(document.visibilityState==="visible")load()});  // back from another tab: fetch at once
load();connectLive();setInterval(()=>{if(document.visibilityState!=="hidden")load()},10000);setInterval(tick,1000);  // a hidden tab waits: visibilitychange fetches at once on return
</script></body></html>"""


# --- the paper trades' review page (/p/<token>/journal): every trade with its evidence, exports ---------------------
JOURNAL_PAGE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>模拟交易复盘</title>
<script>try{var t=JSON.parse(localStorage.getItem("theme"));if(t==="light"||t==="dark")document.documentElement.dataset.theme=t}catch(e){}</script>
<style>
:root{color-scheme:light;--bg:#f2f4f8;--card:#fff;--text:#161a20;--muted:#636b77;--faint:#98a0ab;--line:#e2e6ec;--line2:#edf0f4;--chip:#eef1f5;--chip2:#e2e6ed;--best:#2a66e0;--on-accent:#fff;--best-bg:#e8f0fe;--best-soft:#cfdefb;--warn:#b86e00;--warn-bg:#fff4df;--up:#dd3a40;--down:#17a05b;--hot:#e4262d;--hot-bg:#fdeaea;--shadow:0 1px 2px rgba(18,26,40,.05),0 2px 8px rgba(18,26,40,.05);--shadow2:0 8px 24px rgba(18,26,40,.12);--r:14px}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){color-scheme:dark;--bg:#0d1014;--card:#171b21;--text:#e8ebef;--muted:#9aa3ae;--faint:#6b747f;--line:#262c34;--line2:#20262d;--chip:#20252c;--chip2:#2b313a;--best:#79a7f7;--on-accent:#0d1014;--best-bg:#19284a;--best-soft:#2a4172;--warn:#e6a93f;--warn-bg:#33270f;--up:#ff6166;--down:#3ccc7f;--hot:#ff5a60;--hot-bg:#3a1b1f;--shadow:0 1px 2px rgba(0,0,0,.35),0 2px 8px rgba(0,0,0,.25);--shadow2:0 8px 24px rgba(0,0,0,.45)}}
:root[data-theme=dark]{color-scheme:dark;--bg:#0d1014;--card:#171b21;--text:#e8ebef;--muted:#9aa3ae;--faint:#6b747f;--line:#262c34;--line2:#20262d;--chip:#20252c;--chip2:#2b313a;--best:#79a7f7;--on-accent:#0d1014;--best-bg:#19284a;--best-soft:#2a4172;--warn:#e6a93f;--warn-bg:#33270f;--up:#ff6166;--down:#3ccc7f;--hot:#ff5a60;--hot-bg:#3a1b1f;--shadow:0 1px 2px rgba(0,0,0,.35),0 2px 8px rgba(0,0,0,.25);--shadow2:0 8px 24px rgba(0,0,0,.45)}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;-webkit-font-smoothing:antialiased}
button:focus-visible,a:focus-visible{outline:2px solid var(--best);outline-offset:2px}
.wrap{max-width:1100px;margin:0 auto;padding:12px 16px 40px}
header{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:6px 12px;padding:4px 0 6px}
.ttl{display:flex;flex-direction:column;gap:2px;min-width:0}
h1{font-size:20px;font-weight:750;letter-spacing:-.01em;margin:0;line-height:1.3}h2{font-size:13px;font-weight:700;color:var(--text);letter-spacing:.02em;margin:18px 2px 8px}
.meta{color:var(--muted);font-size:12px;display:flex;flex-wrap:wrap;gap:2px 10px;font-variant-numeric:tabular-nums}
.tog{display:inline-flex;align-items:center;gap:6px;font:inherit;font-size:12.5px;line-height:1.4;color:var(--muted);background:var(--card);border:1px solid var(--line);border-radius:999px;padding:4px 11px 4px 9px;cursor:pointer;user-select:none;white-space:nowrap;transition:border-color .15s,color .15s}.tog:hover{border-color:var(--best-soft);color:var(--text)}
a{color:var(--best)}.mut{color:var(--muted)}.small{font-size:12.5px}.warn{color:var(--warn)}.bad{color:var(--hot)}.ok{color:var(--best)}
.btns{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}
.btn{display:inline-flex;align-items:center;border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:999px;padding:5px 13px;font:inherit;font-size:13px;text-decoration:none;cursor:pointer;box-shadow:var(--shadow);transition:border-color .15s,color .15s}.btn:hover{border-color:var(--best);color:var(--best)}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(165px,1fr));gap:10px;margin:8px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:10px 13px;min-width:0;box-shadow:var(--shadow)}
.tile .k{font-size:12px;color:var(--muted)}.tile .v{font-size:20px;font-weight:750;letter-spacing:-.01em;font-variant-numeric:tabular-nums;white-space:nowrap;line-height:1.3}
.tile .s{font-size:12px;color:var(--muted);margin-top:2px}
.tbl{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow)}
table{border-collapse:collapse;font-size:12.5px;width:100%;font-variant-numeric:tabular-nums}
td,th{border-bottom:1px solid var(--line2);padding:6px 10px;text-align:left;vertical-align:top;white-space:nowrap}
th{color:var(--faint);font-weight:600;font-size:11.5px;letter-spacing:.03em;background:var(--chip)}tr:last-child td{border-bottom:0}tr:hover td{background:var(--chip)}td.wrap{white-space:normal;min-width:140px}
.filters{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:8px 0;font-size:12.5px;color:var(--muted)}
.chip{border:1px solid var(--line);background:var(--card);border-radius:999px;padding:4px 11px;font:inherit;font-size:12.5px;color:var(--muted);cursor:pointer;transition:border-color .15s,color .15s,background .15s}
.chip:hover{border-color:var(--best-soft);color:var(--text)}.chip.on{border-color:var(--best);color:var(--on-accent);background:var(--best)}
.list{display:flex;flex-direction:column;gap:8px}
.tr{background:var(--card);border:1px solid var(--line);border-radius:var(--r);min-width:0;box-shadow:var(--shadow);transition:box-shadow .2s,border-color .2s}
.tr:hover{box-shadow:var(--shadow2)}.tr.open{border-color:var(--best);box-shadow:0 0 0 1px var(--best),var(--shadow)}
.row{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:2px 12px;padding:10px 14px;cursor:pointer;width:100%;border-radius:var(--r);
  background:none;border:0;font:inherit;color:inherit;text-align:left}
.row .l{min-width:0;flex:1 1 220px}.row .t1{font-weight:650}.row .t2{font-size:12.5px;color:var(--muted);font-variant-numeric:tabular-nums}
.row .t3{font-size:12px;color:var(--warn);margin-top:3px;line-height:1.4}
.row .r{text-align:right;font-variant-numeric:tabular-nums;flex:0 0 auto}.row .r b{display:block;font-size:16px;font-weight:750}
.badge{font-size:11.5px;border-radius:999px;padding:1px 8px;background:var(--chip);color:var(--muted);white-space:nowrap;font-weight:600}
.badge.ok{color:var(--best);background:var(--best-bg)}.badge.bad{color:var(--on-accent);background:var(--hot)}.badge.pre{color:var(--warn);background:var(--warn-bg)}
.det{border-top:1px solid var(--line2);padding:10px 14px 14px;display:flex;flex-direction:column;gap:14px;min-width:0}
.sec h3{font-size:13px;margin:0 0 6px;display:flex;align-items:center;gap:8px}.sec h3:before{content:"";width:3px;height:14px;border-radius:2px;background:var(--best)}
.sec .sub{font-size:12px;color:var(--muted);margin:8px 0 4px}
dl{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;margin:0;font-size:12.5px;padding:8px 11px;background:var(--chip);border-radius:10px}dt{color:var(--muted);white-space:nowrap}
dd{margin:0;word-break:break-word;font-variant-numeric:tabular-nums}
.note{font-size:12.5px;background:var(--chip);border-radius:10px;padding:8px 10px;border-left:3px solid var(--warn)}.note.bad{border-left-color:var(--hot)}
footer{margin-top:20px;padding-top:12px;border-top:1px solid var(--line);color:var(--faint);font-size:12px}
</style></head><body><div class="wrap">
<header><div class="ttl"><h1>模拟交易复盘</h1><div class="meta" id="meta">加载中…</div></div><button type="button" class="tog" id="theme" title="切换主题">◐ 自动</button></header>
<div class="btns"><a class="btn" id="back" href="#">← 概率页</a><a class="btn" id="csv" href="journal.csv" download="模拟交易日记.csv">导出 CSV</a><a class="btn" id="json" href="journal.json" download="模拟交易日记.json">导出 JSON</a></div>
<p class="small mut" id="intro"></p>
<div class="tiles" id="tiles"></div>
<h2 id="h-groups" hidden>分组</h2><div class="tbl" id="groups" hidden></div>
<h2 id="h-risk" hidden>共同风险</h2><div id="risk" hidden></div>
<h2>交易</h2><div class="filters" id="filters"></div>
<div class="list" id="list"></div>
<footer>只记账，不会向 Predict 下单。挂单的成交是按盘口快照推定的（推定成交），不代表真实成交。</footer>
</div>
<script>
const $=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e};
const cent=x=>x==null||!isFinite(x)?"—":(x*100).toFixed(1)+"¢",sg=x=>x==null||!isFinite(x)?"—":(x>=0?"+":"")+cent(x);
const money=x=>(x>=0?"+$":"−$")+Math.abs(x).toFixed(2),usd=x=>"$"+Number(x).toFixed(2);
const num=x=>x==null||x===""?"—":typeof x==="number"?x.toLocaleString("en-US",{maximumFractionDigits:6}):String(x);
const when=ms=>ms?new Date(ms).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",hour12:false,month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit"}):"—";
const span=ms=>{if(ms==null||!isFinite(ms))return"—";const a=Math.abs(ms),s=Math.round(a/1000);
  const t=s<90?s+" 秒":s<5400?Math.round(s/60)+" 分钟":s<172800?(s/3600).toFixed(1)+" 小时":(s/86400).toFixed(1)+" 天";return ms<0?t+"后":t};
const KINDS={close:"指数/个股日涨跌",touch:"先触价",updown:"月度涨跌",flip:"反超",range:"价格阶梯",ladder:"市值阶梯"};
const LABELS={fair_up:"模型 涨/Yes 公平价",ref:"参考线",ref_note:"参考说明",effective:"有效价",sigma_daily:"σ（日）",sigma_note:"σ 来源",
  remaining:"剩余方差占比",sigma:"σ（剩余）",z:"z",beta:"β",mode:"口径",direct:"直接用现货",proxy_note:"代理换算",target:"目标日",warn:"提示",
  close_ms:"收盘/截止",price:"现价",low:"低线",high:"高线",sigma_ms:"σ 计算时间",deadline_ms:"截止",years:"剩余年数",path:"路径核验",start_ms:"窗口开始",
  line:"起点",settled:"已结算",start:"起点 K 线",end:"终点 K 线",a:"A 价格",b:"B 价格",ratio:"A/B",window:"窗口",cap:"市值",
  supply:"供应量",sigma_kind:"σ 类型",window_high:"窗口最高",high_at:"最高时间",coverage:"历史覆盖",
  proxy:"代理",family:"合约来源",contract:"合约",quoted_ms:"报价时间",fetched_ms:"抓取时间",anchor:"锚点价格",anchor_ms:"锚点时间",
  anchor_note:"锚点说明",anchor_family:"锚点合约来源",approx:"锚点是近似值",expiry_day:"A50 到期换月日",exchange_contract:"交易所合约",
  session:"时段",maps:"映射",code:"代码版本",sim_edge:"买入门槛",sim_shares:"每笔份数",sim_ways:"方式",sim_markets:"范围",sim_group_usd:"组上限（$，最坏单一事件）",min_edge:"最低净优势",fee_bps:"默认费率（基点）",
  trade_usd:"卡片吃单金额",a50_beta:"A50 β",kospi_beta:"KOSPI β",sigma_error:"σ 误差系数",beta_error:"β 误差",rule:"规则",close:"收盘",
  source:"来源",day:"日期",history:"核验记录"};
const MS_KEYS=new Set(["close_ms","sigma_ms","deadline_ms","start_ms","quoted_ms","fetched_ms","anchor_ms","at"]);
const CENT_KEYS=new Set(["fair_up"]);
let data=null,stateF="all",wayF="all",kindF="all",openId=decodeURIComponent(location.hash.slice(1)||"");
const base=location.pathname.replace(/\/journal\/?$/,"");
document.getElementById("back").href=base;
document.getElementById("csv").href=base+"/journal.csv";document.getElementById("json").href=base+"/journal.json";  // also right under /journal/
function val(k,v){if(v==null||v==="")return"—";if(typeof v==="boolean")return v?"是":"否";
  if(MS_KEYS.has(k))return v?when(v):"—";if(k==="high_at")return v?when(v*1000):"—";if(CENT_KEYS.has(k))return cent(v);
  if(k==="sigma_daily"||k==="sigma")return (v*100).toFixed(2)+"%";if(k==="history")return JSON.stringify(v);
  if(typeof v==="object")return JSON.stringify(v);return num(v)}
function dl(obj,skip){const d=$("dl");Object.entries(obj||{}).forEach(([k,v])=>{if(skip&&skip.includes(k))return;d.append($("dt","",LABELS[k]||k),$("dd","",val(k,v)))});return d}
function table(head,rows){const w=$("div","tbl"),t=$("table"),h=$("tr");head.forEach(x=>h.append($("th","",x)));t.append(h);
  rows.forEach(r=>{const tr=$("tr");r.forEach((x,i)=>{const td=$("td",i===r.length-1&&head[i]==="说明"?"wrap":"");td.append(x instanceof Node?x:String(x??"—"));tr.append(td)});t.append(tr)});w.append(t);return w}
function sec(title,...kids){const s=$("div","sec"),h=$("h3","",title);s.append(h,...kids.filter(Boolean));return s}
function cat(t){if(t.status==="resting")return"rest";if(t.status==="filled")return"open";if(t.status==="expired"||t.status==="cancelled")return"exp";
  return t.confirm==="confirmed"?"ok":t.confirm==="mismatch"?"bad":"pre"}
function badge(t){const c=cat(t),b=$("span","badge"+(c==="ok"?" ok":c==="bad"?" bad":c==="pre"?" pre":""),t.state||t.text);return b}
function perShare(t){return t.shares>0?t.expected_fill/t.shares:null}
function sources(list,at){if(!list||!list.length)return $("p","small mut","没有记录行情来源");
  return table(["项目","来源","代码","类型","价格","报价时间","报价距下单","抓取时间"],list.map(x=>[x.what||"",x.source||"",x.symbol||"",x.type||"",
    x.price==null?"—":num(x.price),x.quoted_ms?when(x.quoted_ms):"—",x.quoted_ms?span(at-x.quoted_ms):"—",x.fetched_ms?when(x.fetched_ms):"—"]))}
function bookTable(b){if(!b)return null;const n=Math.max((b.bids||[]).length,(b.asks||[]).length),rows=[];
  for(let i=0;i<n;i++){const x=(b.bids||[])[i],y=(b.asks||[])[i];rows.push([x?cent(x[0]):"",x?num(x[1]):"",y?cent(y[0]):"",y?num(y[1]):""])}
  const w=$("div");w.append($("div","sub","盘口快照（涨/Yes 一侧，"+when(b.fetched_ms)+" 读取"+(b.fee_bps!=null?"，费率 "+b.fee_bps+" 基点":"")+"）"),
    table(["买价","买量","卖价","卖量"],rows));return w}
function detail(t){
  const d=$("div","det"),e=t.entry||{};
  if(t.legacy)d.append($("div","note warn","这是旧版本记下的交易：当时没有保存判断依据、行情来源和盘口快照，只有价格、成交和结果。"));
  // 1. why the model priced it so
  const basis=$("div");if(e.basis){basis.append(dl(e.basis));if(e.basis.close_ms)basis.append($("div","sub","下单时距收盘/截止 "+span(e.basis.close_ms-t.opened)))}
  else if(e.error)basis.append($("p","small warn","证据记录失败："+e.error));
  const side=$("dl");[["这一边的公平价",cent(t.fair)],["建议门槛",cent(e.need)],["成本价",cent(t.price)],["下单时净优势",sg(t.edge)+" /份"]].forEach(([k,v])=>side.append($("dt","",k),$("dd","",v)));
  d.append(sec("判断依据（下单时）",side,basis.childNodes.length?basis:null));
  const fills=(t.fills||[]).filter(f=>f.basis);
  if(t.maker&&fills.length){const f=fills[0],b=$("div");b.append($("div","sub","第一笔推定成交时（"+when(f.at)+"）的模型输入"),dl(f.basis));d.append(sec("判断依据（成交时）",b))}
  // 2. prices behind it
  if(!t.legacy){const s=$("div");s.append(sources(e.sources,t.opened));if(fills.length){s.append($("div","sub","成交时（"+when(fills[0].at)+"）"),sources(fills[0].sources,fills[0].at))}d.append(sec("行情来源",s))}
  // 3. proxy and anchor
  if(!t.legacy)d.append(sec("代理与锚点",e.proxy?dl(e.proxy):$("p","small mut",e.basis&&e.basis.direct===false?"没有记录代理":"直接用标的本身的价格，没有代理")));
  // 4. how it filled
  const fx=$("div"),fl=$("dl"),add=(k,v)=>fl.append($("dt","",k),$("dd","",v));
  add("下单份数",num(t.order));add("成交份数",num(t.shares)+(t.unfilled?"（未成交 "+num(t.unfilled)+" 份已作废）":""));
  if(t.withdrawn)add("撤单",when(t.withdrawn.at)+"："+(t.withdrawn.why||"")+"，没成交的 "+num(t.withdrawn.unfilled)+" 份作废");
  if(t.maker){add("成交方式","推定成交：盘口出现卖到挂价或更低的卖单才算，按看到的数量、取最多看到的一次");
    if(t.queue_ahead!=null)add("排队假设","排在挂单时该价位已有的 "+num(t.queue_ahead)+" 份之后；之后最少剩 "+num(t.queue_min)+" 份")}
  else{add("成交方式","吃单立即成交");add("成交均价",cent(t.avg));add("最优价",cent(t.best));add("深度滑点",sg(t.slip));add("手续费",cent(t.fee)+" /份")}
  fx.append(fl);
  if((t.fills||[]).length)fx.append($("div","sub","成交记录"),table(["时间","份数","当时公平价","依据"],t.fills.map(f=>[when(f.at),num(f.shares),cent(f.fair),
    f.levels?"吃过："+f.levels.map(l=>cent(l[0])+"×"+num(l[1])).join("、")+(f.short?"（盘口不够）":""):
    f.seen?"看到卖单："+(f.seen.through||[]).map(l=>cent(l[0])+"×"+num(l[1])).join("、")+"，共 "+num(f.seen.visible)+" 份；挂价排队剩 "+num(f.seen.queue_now):f.how||""])));
  const mo=t.markout||{},mks=["1m","5m","30m"].filter(k=>mo[k]);
  if(mks.length)fx.append($("div","sub","成交后市场走向（盘口中间价 − 成交价；模型公平价 − 成交时公平价）"),
    table(["之后","实际间隔","中间价","市价变动","模型变动"],mks.map(k=>[k.replace("m"," 分钟"),span(mo[k].after_s*1000),cent(mo[k].mid),sg(mo[k].move),sg(mo[k].fair_move)])));
  const bt=bookTable(e.book);if(bt)fx.append(bt);
  if(e.card&&e.card.length)fx.append($("div","sub","下单时卡片上的四个方向（按 $"+((t.version||{}).trade_usd||"")+" 计）"),
    table(["方向","价格","净优势","建议"],e.card.map(c=>[c.label,cent(c.price),sg(c.edge),c.best?"✓ 加框":""])));
  d.append(sec("成交证据",fx));
  // 5. how it was settled
  const st=$("div"),sl=$("dl"),put=(k,v)=>sl.append($("dt","",k),$("dd","",v));
  const res=r=>r==null?"—":r.up===1?"涨 / Yes":r.up===0?"跌 / No":"50/50";
  if(t.local){put("本地预结算",res(t.local)+" · "+(t.local.note||""));put("预结算时间",when(t.local.at))}
  if(t.final){put("Predict 结果",res(t.final)+" · "+(t.final.name||"")+"（"+(t.final.how||"")+"）");put("确认时间",when(t.final.at))}
  else if(t.status==="settled"||t.local)put("Predict 结果","待确认"+(t.final_error?"（读取失败："+t.final_error+"）":t.final_check?"（市场状态："+(t.final_check.status||"未知")+"）":""));
  put("状态",t.text+(t.state?" · "+t.state:""));
  if(t.wait)put("现在等什么",t.wait);
  st.append(sl);
  if(t.local&&t.local.evidence&&Object.keys(t.local.evidence).length)st.append($("div","sub","本地结算依据"),dl(t.local.evidence));
  if((t.revisions||[]).length)st.append($("div","sub","修订记录"),table(["时间","依据","回款/份","说明"],t.revisions.map(r=>[when(r.at),r.by,num(r.from)+" → "+num(r.to),r.note||""])));
  if(t.confirm==="mismatch")st.prepend($("div","note bad","本地结果与 Predict 不一致，已按 Predict 的结果重新结算"));
  d.append(sec("结算证据",st));
  // 6. version
  if(t.version)d.append(sec("版本与设置",dl(t.version)));
  const a=$("a","btn","打开 Predict 市场 ↗");a.href=t.url;a.target="_blank";a.rel="noopener noreferrer";d.append(a);
  return d}
function row(t){
  const w=$("div","tr"+(openId===t.id?" open":"")),b=$("button","row");b.type="button";b.setAttribute("aria-expanded",openId===t.id?"true":"false");
  const l=$("div","l"),r=$("div","r");
  l.append($("div","t1",t.item+" "+t.label+" @ "+cent(t.price)+" × "+num(t.shares)+(t.maker&&t.shares<t.order?"/"+num(t.order):"")));
  l.append($("div","t2",when(t.opened)+" · "+(KINDS[t.kind]||t.kind)+(t.driver_name?" · "+t.driver_name:"")+" · 下单时 "+sg(t.edge)+" · 成交时 "+sg(perShare(t))+" /份"));
  if(t.wait)l.append($("div","t3","⏳ "+t.wait));
  const pn=$("b","",t.pnl==null?"":money(t.pnl));if(t.pnl!=null)pn.className=t.pnl>=0?"ok":"bad";r.append(pn,badge(t));
  b.append(l,r);b.addEventListener("click",()=>{openId=openId===t.id?"":t.id;history.replaceState(null,"",openId?"#"+encodeURIComponent(openId):location.pathname);render()});
  w.append(b);if(openId===t.id)w.append(detail(t));return w}
function tiles(){
  const s=data.total,el=document.getElementById("tiles"),tile=(k,v,sub,cls)=>{const x=$("div","tile");x.append($("div","k",k));const vv=$("div","v",v);if(cls)vv.className+=" "+cls;x.append(vv);if(sub)x.append(typeof sub==="string"?$("div","s",sub):sub);return x};
  const conf=$("div","s");conf.append("预结算 "+s.local+" · ",$("span",s.mismatch?"bad":"","结果不一致 "+s.mismatch));
  el.replaceChildren(tile("已结算",s.settled+" 笔","赢 "+s.wins+" · 输 "+s.losses+" · 平 "+s.ties),
    tile("盈亏",money(s.pnl),"成本 "+usd(s.cost)+(s.cost?" · "+(s.roi>=0?"+":"")+(s.roi*100).toFixed(1)+"%":""),s.pnl>=0?"ok":"bad"),
    tile("模型预期（下单时）",money(s.expected),"按下单时的净优势 × 份数"),
    tile("模型预期（成交时）",money(s.expected_fill),"按成交那一刻的公平价；比下单时低很多 = 挂单常在行情转向时被成交"),
    tile("结算确认","已确认 "+s.confirmed,conf),
    tile("进行中","持仓 "+s.open,"挂单中 "+s.resting+" · 部分成交 "+s.partial+" · 作废 "+s.expired+(s.cancelled?" · 撤单 "+s.cancelled:"")))}
function groups(){
  const rows=[...data.kinds,...(data.modes.length>1?data.modes:[])],g=document.getElementById("groups"),h=document.getElementById("h-groups");
  g.hidden=h.hidden=rows.length<2;if(rows.length<2)return;
  g.replaceChildren(table(["分组","已结算","盈亏","下单时预期","成交时预期","不一致"],rows.map(r=>[r.name,r.settled+" 笔",money(r.pnl),money(r.expected),money(r.expected_fill),r.mismatch])).firstChild)}
function risk(){  // the open positions by the event that settles them (ten markets can be one risk), the sources they rest on, the buys the cap refused
  const el=document.getElementById("risk"),h=document.getElementById("h-risk"),gs=(data.groups||[]),held=gs.filter(g=>g.positions),src=data.sources||[],bl=data.blocks||[];
  el.hidden=h.hidden=!gs.length&&!bl.length;if(el.hidden)return;
  const parts=[];
  if(gs.length){parts.push($("p","small mut","持仓按结算事件分组：同一标的的几档、同一指数同一天的几笔，一次行情一起亏。最坏单一事件 = 对这组最不利的那一个走势（涨到某档、跌到某档、收涨/收跌、都没触及）下的合计盈亏；挂单按未成交份数另计。"+(data.group_cap?"模拟盘每组最坏单一事件不超过 $"+data.group_cap+"（SIM_GROUP_USD），超过的建议不买、记在下面。":"")));
    parts.push(table(["事件 / 标的","持仓","成本","占持仓","最坏单一事件","合计盈亏","挂单中","市场"],gs.map(g=>{const v=$("b","",g.positions?money(g.worst):"—");v.className=g.worst<0?"bad":"ok";
      const n=$("span","",g.name);n.title=(g.items||[]).join("、");return[n,g.positions+" 笔",usd(g.cost),g.positions?(g.share*100).toFixed(0)+"%":"—",g.event||"—",v,g.resting?g.resting+" 笔（"+usd(g.resting_usd)+"）":"—",(g.items||[]).join("、")]})))}
  if(src.length){parts.push($("div","sub","持仓依赖的数据源（一个源停更或出错，这些仓位的判断一起失效）"));
    parts.push(table(["数据源","持仓/挂单","金额","市场"],src.map(x=>[x.source,x.trades+" 笔",usd(x.cost),(x.items||[]).join("、")])))}
  if(bl.length){parts.push($("div","sub","组上限拦下的买入（最近一周）"));
    parts.push(table(["时间","市场","方向","价格","原因"],bl.map(b=>[when(b.at),b.item,b.label,cent(b.price),b.why])))}
  el.replaceChildren(...parts)}
function filters(){
  const f=document.getElementById("filters"),chip=(t,on,fn)=>{const b=$("button","chip"+(on?" on":""),t);b.type="button";b.addEventListener("click",fn);return b};
  const sts=[["all","全部"],["open","持仓"],["rest","挂单中"],["pre","预结算"],["ok","已确认"],["bad","结果不一致"],["exp","未成交/撤单"]];
  const ways=[["all","全部"],["maker","挂单"],["taker","吃单"]];
  const kinds=[["all","全部"],...Object.entries(KINDS).filter(([k])=>data.trades.some(t=>t.kind===k))];
  f.replaceChildren($("span","","状态"),...sts.map(([k,t])=>chip(t,stateF===k,()=>{stateF=k;render()})),$("span","","　方式"),
    ...ways.map(([k,t])=>chip(t,wayF===k,()=>{wayF=k;render()})),...(kinds.length>2?[$("span","","　市场"),...kinds.map(([k,t])=>chip(t,kindF===k,()=>{kindF=k;render()}))]:[]))}
function render(){
  if(!data)return;
  const how=[];if(data.ways!=="只挂单")how.push("吃单按这么多份吃到的均价和手续费判断并成交");if(data.ways!=="只吃单")how.push("挂单只挂在双边都有报价、价差不超过 10¢ 的盘口，排在已有挂单之后，只有盘口出现卖到挂价或更低的卖单才按看到的数量推定成交；不再做的挂单撤掉");
  document.getElementById("intro").textContent="净优势 ≥"+(data.edge*100).toFixed(0)+"¢ 时按卡片建议买 "+data.shares+" 份。"+(data.scope?"范围："+data.scope+"；"+data.ways+"。":"")+how.join("；")+"。先用机器人数据预结算，再以 Predict 的结果确认，不一致时按 Predict 重新结算并留下修订记录。";
  tiles();groups();risk();filters();
  const list=document.getElementById("list"),shown=data.trades.filter(t=>(stateF==="all"||cat(t)===stateF)&&(wayF==="all"||(wayF==="maker")===t.maker)&&(kindF==="all"||t.kind===kindF));
  list.replaceChildren(...(shown.length?shown.map(row):[$("p","mut",data.trades.length?"没有符合条件的交易":"还没有模拟交易")]))}
let okAt=0,dead=false;
function two(n){return String(n).padStart(2,"0")}
async function load(){
  if(dead||load.busy)return;load.busy=true;
  try{const r=await fetch(base+"/journal.json",{cache:"no-store"});if(!r.ok)throw new Error("HTTP "+r.status);data=await r.json();okAt=Date.now();
    document.getElementById("meta").replaceChildren($("span","","数据 "+data.generated_at),$("span","","v"+data.version),$("span","",data.trades.length+" 笔"));
    render();const o=openId&&document.querySelector(".tr.open");if(o&&!load.done)o.scrollIntoView({block:"start"});load.done=true}
  catch(e){const at=okAt?new Date(okAt):null,hms=at?two(at.getHours())+":"+two(at.getMinutes())+":"+two(at.getSeconds()):"";
    if(e.message==="HTTP 404")dead=true;
    document.getElementById("meta").replaceChildren($("span","bad","⚠️ 读取失败："+(dead?"链接已失效，请在 Telegram 重新发送 /web；本页不再自动刷新":e.message)+
      (okAt?"；下面显示的是 "+hms+" 的旧数据":"")))}
  finally{load.busy=false}}
document.addEventListener("visibilitychange",()=>{if(document.visibilityState==="visible")load()});  // back from another tab: fetch at once
const THEMES={auto:["◐","自动"],light:["☀","浅色"],dark:["☾","深色"]};let theme="auto";try{const t=JSON.parse(localStorage.getItem("theme"));if(typeof t==="string"&&Object.prototype.hasOwnProperty.call(THEMES,t))theme=t}catch(e){}
function applyTheme(){const r=document.documentElement;if(theme==="auto")delete r.dataset.theme;else r.dataset.theme=theme;const b=document.getElementById("theme");b.textContent=THEMES[theme][0]+" "+THEMES[theme][1];b.title="主题："+THEMES[theme][1]+"（点击切换）"}
document.getElementById("theme").addEventListener("click",()=>{const ks=Object.keys(THEMES);theme=ks[(ks.indexOf(theme)+1)%ks.length];try{localStorage.setItem("theme",JSON.stringify(theme))}catch(e){}applyTheme()});applyTheme();
load();setInterval(load,60000);
</script></body></html>"""


def accepts_gzip(header_block: str) -> bool:
    """Whether the request's Accept-Encoding lists gzip (any positive q), from the raw header lines after the request line."""
    for line in header_block.split("\r\n"):
        name, sep, value = line.partition(":")
        if sep and name.strip().lower() == "accept-encoding":
            for token in value.split(","):
                coding, _, params = token.strip().partition(";")
                if coding.strip().lower() in {"gzip", "x-gzip", "*"}:
                    q = params.strip().lower().removeprefix("q=").strip() if params.strip().lower().startswith("q=") else "1"
                    try:
                        return float(q) > 0
                    except ValueError:
                        return False
    return False


def page_payload(payload: dict) -> dict:
    """odds_payload() as data.json carries it: the server's edge chips (four per book, eleven fields each) are left out of
    every book block. The page computes its own from the depth for the trade size the viewer picks and never reads them;
    /book, the alerts, the paper trader and the tests use the server's. Fetched every 10 seconds by every open tab, the
    file is about 40% smaller without them (a quarter smaller after gzip). The payload given is left as it is."""
    without = lambda block: {k: v for k, v in block.items() if k != "edges"}
    items = []
    for item in payload.get("items") or []:
        item = dict(item)
        if isinstance(item.get("predict"), dict):
            item["predict"] = without(item["predict"])
        ladder = item.get("ladder")
        if isinstance(ladder, dict) and isinstance(ladder.get("rows"), list):
            item["ladder"] = {**ladder, "rows": [without(row) if isinstance(row, dict) else row for row in ladder["rows"]]}
        items.append(item)
    return {**payload, "items": items}


class WebServer:
    """Tiny read-only HTTP server (stdlib asyncio) for the probability page.

    Routes: /health, /p/<token> (HTML), /p/<token>/data.json (JSON), /p/<token>/events (server-sent events: the daily
    cards' live numbers whenever they change, checked every second), /p/<token>/journal (the paper trades' review
    page) with journal.json / journal.csv (exports). Everything else is 404, the token is compared in constant time,
    and responses are no-store with a restrictive CSP. Text bodies are gzip-compressed for a client that accepts it
    (the page is ~80 KB, data.json is fetched every 10 seconds by every open tab).
    """
    MAX_HEADER_BYTES = 8192
    GZIP_MIN_BYTES = 512  # below this a gzip header costs about as much as it saves
    CACHE_SECONDS = {"data.json": 1.0, "journal.json": 5.0, "live.json": 1.0}  # several tabs (or a scanner) share one build per interval
    STREAM_SECONDS = 1.0        # an event stream looks for a change this often
    STREAM_PING_SECONDS = 20    # a comment keeps a quiet stream (and any proxy in front of it) open
    STREAM_LIFE_SECONDS = 3600  # then the stream ends; the browser's EventSource reconnects by itself
    STREAM_MAX = 32             # concurrent streams; beyond it a request is told to retry later

    def __init__(self, bot: "Bot", port: int, token: str):
        self.bot, self.port, self.token = bot, port, token
        self.streams = 0  # event streams open now
        self.server: asyncio.base_events.Server | None = None
        self.pages = {"page": WEB_PAGE.encode("utf-8"), "journal": JOURNAL_PAGE.encode("utf-8")}
        self.cache: dict[str, tuple[float, bytes]] = {}  # name -> (expires, body): the JSON is built once per interval
        self.gzipped: dict[bytes, bytes] = {body: gzip.compress(body, compresslevel=9) for body in self.pages.values()}

    def cached(self, name: str, build: Any) -> bytes:
        entry = self.cache.get(name)
        if entry is None or time.monotonic() >= entry[0]:
            entry = self.cache[name] = (time.monotonic() + self.CACHE_SECONDS.get(name, 0.0), build())
        return entry[1]

    def compressed(self, body: bytes) -> bytes:
        """gzip of ``body``, computed once per distinct body (the pages forever, a JSON answer for its cache interval)."""
        found = self.gzipped.get(body)
        if found is None:
            if len(self.gzipped) > 8:
                self.gzipped = {page: self.gzipped[page] for page in self.pages.values()}
            found = self.gzipped[body] = gzip.compress(body, compresslevel=6)
        return found

    async def start(self) -> int:
        self.server = await asyncio.start_server(self.handle, "0.0.0.0", self.port)
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            with contextlib.suppress(Exception):
                await self.server.wait_closed()

    def route(self, method: str, path: str) -> tuple[int, str, bytes]:
        if method not in {"GET", "HEAD"}:
            return 405, "text/plain; charset=utf-8", b"method not allowed"
        if path in {"/", "/health"}:
            return 200, "text/plain; charset=utf-8", b"ok"
        parts = path.strip("/").split("/")
        # (compare_digest needs ASCII on both sides: a scanner's odd bytes are simply not the token)
        if len(parts) in {2, 3} and parts[0] == "p" and parts[1].isascii() and hmac.compare_digest(parts[1], self.token):
            if len(parts) == 2:
                return 200, "text/html; charset=utf-8", self.pages["page"]
            if parts[2] == "data.json":
                return 200, "application/json; charset=utf-8", self.cached(
                    "data.json", lambda: json.dumps(page_payload(self.bot.odds_payload()), ensure_ascii=False, default=str).encode("utf-8"))
            if parts[2] == "journal":
                return 200, "text/html; charset=utf-8", self.pages["journal"]
            if parts[2] == "journal.json":
                return 200, "application/json; charset=utf-8", self.cached(
                    "journal.json", lambda: json.dumps(self.bot.journal_payload(), ensure_ascii=False, default=str).encode("utf-8"))
            if parts[2] == "journal.csv":
                return 200, "text/csv; charset=utf-8", self.bot.journal_csv().encode("utf-8")
        return 404, "text/plain; charset=utf-8", b"not found"

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
            if len(head) > self.MAX_HEADER_BYTES:
                raise ValueError("header too large")
            request_line, _, rest = head.decode("latin-1").partition("\r\n")
            method, target, _ = request_line.split(" ", 2)
            gzip_ok = accepts_gzip(rest)
            path = urllib.parse.urlsplit(target).path
            if method.upper() == "GET" and self.is_stream(path):
                await self.stream(writer)
                return
            status, ctype, body = self.route(method.upper(), path)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ValueError):
            status, ctype, body, method, gzip_ok = 400, "text/plain; charset=utf-8", b"bad request", "GET", False
        except Exception as error:  # Never let a page request touch the bot's loops.
            self.bot.log_limited("web", f"web request failed: {clean_error(error) or type(error).__name__}")
            status, ctype, body, method, gzip_ok = 500, "text/plain; charset=utf-8", b"error", "GET", False
        encoding = ""
        if gzip_ok and status == 200 and len(body) >= self.GZIP_MIN_BYTES:
            body, encoding = self.compressed(body), "Content-Encoding: gzip\r\n"
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed", 500: "Internal Server Error"}[status]
        headers = (f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n{encoding}"
                   "Vary: Accept-Encoding\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\n"
                   "X-Robots-Tag: noindex\r\nContent-Security-Policy: default-src 'self'; style-src 'unsafe-inline'; "
                   "script-src 'unsafe-inline'; img-src 'none'; frame-ancestors 'none'\r\nConnection: close\r\n\r\n")
        with contextlib.suppress(Exception):
            writer.write(headers.encode("latin-1") + (b"" if method.upper() == "HEAD" else body))
            await writer.drain()
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()

    def is_stream(self, path: str) -> bool:
        parts = path.strip("/").split("/")
        return (len(parts) == 3 and parts[0] == "p" and parts[2] == "events" and parts[1].isascii()
                and hmac.compare_digest(parts[1], self.token))

    async def stream(self, writer: asyncio.StreamWriter) -> None:
        """Server-sent events: Bot.live_payload as an ``event: live`` whenever it changes, checked every second (one build
        per second shared by every open stream), a comment every STREAM_PING_SECONDS otherwise. Ends when the viewer
        leaves, the bot stops, or after STREAM_LIFE_SECONDS (the browser reconnects)."""
        if self.streams >= self.STREAM_MAX:
            with contextlib.suppress(Exception):
                writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: 4\r\n"
                             b"Retry-After: 5\r\nCache-Control: no-store\r\nConnection: close\r\n\r\nbusy")
                await writer.drain()
                writer.close()
            return
        self.streams += 1
        try:
            writer.write(("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream; charset=utf-8\r\nCache-Control: no-store\r\n"
                          "X-Accel-Buffering: no\r\nX-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\n"
                          "X-Robots-Tag: noindex\r\nConnection: close\r\n\r\nretry: 2000\n\n").encode("latin-1"))
            await writer.drain()
            sent, wrote, started = b"", time.monotonic(), time.monotonic()
            while not writer.is_closing() and not self.bot.stopping.is_set() and time.monotonic() - started < self.STREAM_LIFE_SECONDS:
                try:
                    body = self.cached("live.json", lambda: json.dumps(self.bot.live_payload(), ensure_ascii=False, default=str).encode("utf-8"))
                except Exception as error:  # a broken card must not end every stream: try again next second
                    self.bot.log_limited("web", f"event stream build failed: {clean_error(error) or type(error).__name__}")
                    body = sent
                if body != sent:
                    writer.write(b"event: live\ndata: " + body + b"\n\n")
                    sent, wrote = body, time.monotonic()
                elif time.monotonic() - wrote >= self.STREAM_PING_SECONDS:
                    writer.write(b": ping\n\n")
                    wrote = time.monotonic()
                await writer.drain()
                await asyncio.sleep(self.STREAM_SECONDS)
        except Exception:  # the viewer left (reset / broken pipe): nothing to report
            pass
        finally:
            self.streams -= 1
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()


# --- diagnostics (/diag, --diag) --------------------------------------------------------------------

DIAG_TIMEOUT = 12      # seconds per probe
DIAG_PARALLEL = 4      # probes in flight at once (gentle on free feeds that drop bursts)


@dataclass(frozen=True)
class ProbeResult:
    group: str
    name: str
    ok: bool
    ms: int
    detail: str


def raw_snippet(raw: Any, limit: int = 140) -> str:
    """The start of a response, whitespace collapsed, for 'what did the feed actually send'."""
    if isinstance(raw, (bytes, bytearray)):
        text = raw.decode("utf-8", errors="replace")
        if "\ufffd" in text:  # not UTF-8: Sina/Tencent answer in GBK
            text = raw.decode("gbk", errors="replace")
    else:
        text = json.dumps(raw, ensure_ascii=False, default=str) if not isinstance(raw, str) else raw
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", text, flags=re.S | re.I)
    if text.lstrip().startswith("<"):  # an HTML error/block page: keep the words, drop the markup
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text).strip()
    return clean_error(text[:limit] + ("…" if len(text) > limit else "")) or "（空响应）"


async def run_probe(group: str, name: str, fetch: Any, check: Any) -> ProbeResult:
    """Fetch once and check the answer; a failure says whether the request or the content was wrong."""
    started = time.monotonic()
    try:
        raw = await asyncio.wait_for(fetch(), DIAG_TIMEOUT)
    except asyncio.TimeoutError:
        return ProbeResult(group, name, False, int((time.monotonic() - started) * 1000), f"请求超时（>{DIAG_TIMEOUT} 秒）")
    except Exception as error:
        return ProbeResult(group, name, False, int((time.monotonic() - started) * 1000),
                           f"请求失败：{clean_error(error) or type(error).__name__}")
    ms = int((time.monotonic() - started) * 1000)
    try:
        return ProbeResult(group, name, True, ms, check(raw))
    except Exception as error:
        return ProbeResult(group, name, False, ms,
                           f"内容异常：{clean_error(error) or type(error).__name__}｜返回：{raw_snippet(raw)}")


def eastmoney_fields(raw: bytes, keys: tuple[str, ...]) -> str:
    """'f57=CN00Y f86=09-26 05:14' from a push2 answer, so a rejected code or missing time is visible."""
    try:
        data = json.loads(raw).get("data") or {}
    except (ValueError, AttributeError):
        return ""
    parts = []
    for key in keys:
        value = data.get(key)
        if key == "f86" and str(value).isdigit():
            value = stamp(int(value) * 1000, seconds=False)
        parts.append(f"{key}={value if value not in (None, '') else '空'}")
    return " ".join(parts)


@dataclass(frozen=True)
class Plan:
    reason: str
    next_state: dict


def alert_plan(state: dict | None, baseline_key: str, change: D, now: float,
               threshold: D, cooldown: int, step: D, min_gap: int) -> tuple[dict, Plan | None]:
    """Returns passive state + proposed post-delivery state. Commit Plan only on send success."""
    s = copy.deepcopy(state or {})
    if s.get("baseline_key") != baseline_key:
        # A changed reference opens a fresh alert episode; retain the short spam guard.
        s = {"baseline_key": baseline_key, "side": 0, "highest_tier": 0,
             "last_sent": s.get("last_sent", 0)}
    magnitude = abs(change)
    if magnitude <= threshold * D("0.8"):
        s["side"], s["highest_tier"] = 0, 0
        return s, None
    # Strictly greater than 1%; equality must not trigger.
    if magnitude <= threshold:
        return s, None
    side = 1 if change > 0 else -1
    tier = int((magnitude - threshold) // step) + 1 if step > 0 else 1
    elapsed = now - float(s.get("last_sent", 0))
    reason = ""
    if s.get("side", 0) != side:
        reason = ("方向反转并超过阈值" if s.get("side")
                  else "重新超过阈值（曾回到阈值 80% 以内）" if s.get("last_sent") else "首次超过阈值")
    elif step > 0 and tier > int(s.get("highest_tier", 0)):
        reason = "偏离继续扩大"
    elif cooldown > 0 and elapsed >= cooldown:
        reason = "持续超标提醒"
    if not reason or (s.get("last_sent", 0) and elapsed < min_gap):
        return s, None
    next_state = copy.deepcopy(s)
    next_state.update(side=side, highest_tier=max(tier, int(s.get("highest_tier", 0)))
                      if s.get("side") == side else tier, last_sent=now)
    return s, Plan(reason, next_state)


def alert_text(symbol: str, quote: Quote, base: Baseline, change: D, threshold: D, reason: str,
               references: dict[str, Baseline] | None = None, fx: "FxRates | None" = None,
               style: str = "cn", context: list[str] | None = None, now_ms: int | None = None) -> str:
    """Alert body with bold sentinels; send it with html=True. ``context`` = extra rows (HL, indices); ``now_ms`` is
    when the text is rendered, so the quote's age ("N 秒前") is real rather than always 0."""
    side = "上涨" if change > 0 else "下跌"
    rows = [f"基准 {fmt(base.value)}（{baseline_brief(base)}）→ {pct_text(change, style, strong=True, digits=3)}"]
    rows += [reference_row(kind, quote.price, ref, fx, style) for kind, ref in (references or {}).items()]
    rows += [line for line in (context or []) if line]
    rows.append(f"📝 {reason}")
    return "\n".join([f"{trend_mark(change, style)} {bold(f'{side}超过 {fmt(threshold)}%｜{NAMES.get(symbol, symbol)}')}（{symbol}）",
                      quote.price_row(quote.timestamp_ms if now_ms is None else max(now_ms, quote.timestamp_ms)), *tree(rows),
                      "⚠️ 合约行情提示，不代表股票官方收盘结算结果。"])


# A message's freshness check (set by Bot.tell): run again after the send queue's wait, right before the message goes
# out, so a Telegram cool-down (429 retry_after can be minutes) never delivers a price that was current back then.
SEND_CHECK: contextvars.ContextVar[Any] = contextvars.ContextVar("send_check", default=None)
# A message's renderer (set by Bot.tell): rebuilds the text right before it goes out, so what it says about "now"
# (a quote's age) is true at the moment of sending, not at the moment it was queued.
SEND_RENDER: contextvars.ContextVar[Any] = contextvars.ContextVar("send_render", default=None)
# Telegram says this when a chat can no longer be delivered to; retrying every cycle would only clog the queue.
CHAT_GONE = ("bot was blocked by the user", "user is deactivated", "bot was kicked", "chat not found",
             "bot is not a member", "message thread not found", "topic_closed", "chat_write_forbidden",
             "have no rights to send", "not enough rights to send")


def chat_gone(error: BaseException) -> str:
    """Why Telegram will keep refusing this chat ("" when the failure may be temporary)."""
    text = str(error).lower()
    return next((reason for reason in CHAT_GONE if reason in text), "")


class StaleMessage(Exception):
    """A queued message whose data went stale while it waited to be sent: dropped, never delivered late."""


class Telegram:
    CHAT_GAP = 1.1    # seconds between two messages to the same chat (Telegram allows about one per second there)
    GLOBAL_GAP = 0.1  # seconds between any two messages (Telegram's overall limit is about 30 per second)
    PARALLEL = 4      # requests in flight at once: one chat's slow or hanging sendMessage never holds the others' back

    def __init__(self, token: str):
        self.root = f"https://api.telegram.org/bot{token}/"
        self.lock = asyncio.Lock()                     # hands out the global send slots (held for a moment, never across a request)
        self.sends = asyncio.Semaphore(self.PARALLEL)  # requests in flight
        self.next_send = 0.0                           # no message to anyone before this (monotonic)
        self.chat_next: dict[Any, float] = {}          # chat -> no message to it before this (its 429 cool-down)
        self.chat_locks: dict[Any, asyncio.Lock] = {}  # chat -> its messages keep their order

    async def call(self, method: str, payload: dict | None = None, timeout: int = 15) -> Any:
        data = await http_json(self.root + method, payload or {}, timeout)
        if not isinstance(data, dict) or not data.get("ok"):
            msg = data.get("description", "未知错误") if isinstance(data, dict) else "响应格式异常"
            retry = data.get("parameters", {}).get("retry_after", 0) if isinstance(data, dict) else 0
            raise RemoteError(f"Telegram: {clean_error(msg)}", int(retry))
        return data.get("result")

    async def paced(self, method: str, payload: dict) -> Any:
        """Serialize outgoing messages per chat and respect Telegram's send rates: about one message per second to a
        chat (a 429 cool-down holds only that chat), a small gap between any two. The global lock only hands out the
        next send slot; the request itself runs outside it, up to PARALLEL at a time, so one chat's send that hangs
        for its 15-second timeout does not stall every other chat's alerts, command replies and card edits behind it.
        A message with a freshness check is re-validated after the wait, right before it is sent (StaleMessage when its
        data no longer holds); one with a renderer is rebuilt then."""
        chat = payload.get("chat_id")
        if len(self.chat_locks) > 2000:
            self.chat_locks = {c: lock for c, lock in self.chat_locks.items() if lock.locked()}
            self.chat_next = {c: t for c, t in self.chat_next.items() if t > time.monotonic()}
        async with self.chat_locks.setdefault(chat, asyncio.Lock()):
            await asyncio.sleep(max(0, self.chat_next.get(chat, 0) - time.monotonic()))
            async with self.lock:  # reserve the slot; the wait for it happens with the lock released
                slot = max(self.next_send, time.monotonic())
                self.next_send = slot + self.GLOBAL_GAP
            await asyncio.sleep(max(0, slot - time.monotonic()))
            async with self.sends:
                check = SEND_CHECK.get()  # right before the request goes out, after every wait
                if check is not None and not check():
                    raise StaleMessage("排队等待发送期间数据已失效，未发送")
                render = SEND_RENDER.get()
                if render is not None and "text" in payload:
                    payload = {**payload, "text": split_text(render(), html_mode=payload.get("parse_mode") == "HTML")[0]}
                    SEND_RENDER.set(None)
                try:
                    return await self.call(method, payload)
                except RemoteError as error:
                    self.chat_next[chat] = time.monotonic() + max(self.CHAT_GAP, error.retry_after)
                    raise
                finally:
                    self.chat_next[chat] = max(self.chat_next.get(chat, 0), time.monotonic() + self.CHAT_GAP)

    async def send(self, chat: int, thread: int, text: str, reply_markup: dict | None = None,
                   parse_mode: str | None = None) -> None:
        # Plain text by default; HTML only for messages that were escaped with to_html().
        chunks = split_text(text, html_mode=parse_mode == "HTML")
        if len(chunks) > 1:
            SEND_RENDER.set(None)  # a long message is sent as queued: its parts must come from one rendering
            if parse_mode == "HTML":
                chunks = balance_bold(chunks)
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {"chat_id": chat, "text": chunk,
                                      "link_preview_options": {"is_disabled": True}}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            if thread:
                payload["message_thread_id"] = thread
            if reply_markup and index == len(chunks) - 1:
                payload["reply_markup"] = reply_markup  # Buttons belong under the final chunk.
            await self.paced("sendMessage", payload)
            SEND_CHECK.set(None)  # once the first part is out, the rest of the message follows it

    async def edit(self, chat: int, message_id: int, text: str, reply_markup: dict | None = None) -> None:
        """Rewrite a card in place after a button press so it reflects the new selection."""
        payload: dict[str, Any] = {"chat_id": chat, "message_id": message_id, "text": split_text(text)[0],
                                   "link_preview_options": {"is_disabled": True}}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        await self.paced("editMessageText", payload)


HTML_TAG = re.compile(r"<[^<>]*>")


def rendered_len(text: str, html_mode: bool = False) -> int:
    """How long Telegram counts a message: with HTML parsing the tags are gone and entities are single characters, so
    a card full of <b> spans is a good deal shorter than its markup (counting the markup split /prob in two every time)."""
    return len(html.unescape(HTML_TAG.sub("", text))) if html_mode else len(text)


def split_text(text: str, limit: int = 3400, html_mode: bool = False) -> list[str]:
    """Parts of at most ``limit`` characters as Telegram counts them (rendered_len). A part ends at a blank line (the gap
    between two cards' blocks) when one falls in its last 40%, else at the last line end, else at the limit, never
    through an HTML entity (&amp; → &am + p;) or a tag."""
    chunks: list[str] = []
    remaining = text
    while remaining:
        if rendered_len(remaining, html_mode) <= limit:
            chunks.append(remaining)
            break
        cut = limit
        if html_mode:  # the longest prefix that renders within the limit
            low, high = 0, len(remaining)
            while low < high:
                mid = (low + high + 1) // 2
                if rendered_len(remaining[:mid], True) <= limit:
                    low = mid
                else:
                    high = mid - 1
            cut = low
        block = remaining.rfind("\n\n", 0, cut + 1)
        line = remaining.rfind("\n", 0, cut + 1)
        cut = block if block >= cut * 0.6 else line if line > 0 else cut
        amp = remaining.rfind("&", max(0, cut - 8), cut)
        if amp >= 0 and remaining.find(";", amp, cut) < 0:
            cut = amp
        tag = remaining.rfind("<", max(0, cut - 8), cut)
        if html_mode and tag >= 0 and remaining.find(">", tag, cut) < 0:
            cut = tag
        cut = max(cut, 1)  # always progress
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return chunks or [""]


def balance_bold(chunks: list[str]) -> list[str]:
    """Close a <b> left open at the end of a chunk and reopen it in the next: Telegram rejects a part with an unmatched tag."""
    out, open_tag = [], False
    for chunk in chunks:
        if open_tag:
            chunk = "<b>" + chunk
        open_tag = chunk.count("<b>") > chunk.count("</b>")
        out.append(chunk + "</b>" if open_tag else chunk)
    return out


def target_from_message(message: dict) -> tuple[int, int]:
    return int(message["chat"]["id"]), int(message.get("message_thread_id") or 0)


def subscription_key(chat: int, thread: int) -> str:
    return f"{chat}:{thread}"


def is_admin(message: dict, config: Config) -> bool:
    sender = message.get("from") or {}
    return bool(config.admin_id and not sender.get("is_bot") and sender.get("id") == config.admin_id)


@dataclass(frozen=True)
class Command:
    """One bot command: drives /help text, the Telegram "菜单" button and dispatch."""
    name: str
    description: str
    example: str = ""  # Arguments shown in /help and the menu hint, e.g. "1" for /threshold 1.
    note: str = ""     # Extra /help line under the command.

    @property
    def usage(self) -> str:
        return f"/{self.name} {self.example}".rstrip()

    def help_line(self) -> str:
        line = f"{self.usage}  {self.description}"
        return f"{line}\n  {self.note}" if self.note else line

    def menu_entry(self) -> dict[str, str]:
        # Telegram shows only name + description in the menu; append a usage hint when arguments exist.
        text = f"{self.description}｜用法 {self.usage}" if self.example else self.description
        return {"command": self.name, "description": text[:256]}


COMMANDS: tuple[Command, ...] = (
    Command("subscribe", "在当前私聊/群组话题订阅"),
    Command("unsubscribe", "取消当前订阅"),
    Command("status", "查看合约、基准与数据状态"),
    Command("threshold", "改为严格超过 ±1% 提醒", "1", "不带数字则弹出档位按钮卡片，点选即可"),
    Command("cooldown", "持续超标每 300 秒提醒（0=关闭周期提醒）", "300", "不带数字则弹出档位按钮卡片，点选即可"),
    Command("mode", "daily=币安上一 UTC 日日 K 收盘；exchange=币安合约在证券交易所收盘时刻的价格；manual=手动参考价",
            "daily|exchange|manual", "不带参数则弹出模式按钮卡片，点选即可"),
    Command("setclose", "设置手动参考价，可一次发多条", "UNITREE 75 09-17 16:00",
            "示例：75 是基准，09-17 16:00 是它的收盘时间（北京，可省略）\n"
            "  末尾再写 YYYY-MM-DD 可指定适用日（默认今天）；批量：每行一组，首行可写统一适用日"),
    Command("setexchange", "记录证券交易所收盘价，可带货币（对照显示）", "SKHYNIX 258000 KRW 09-17 14:30",
            "带 HKD/CNY/KRW 等非美元货币时只展示不算涨跌；不带货币按同口径算相对涨跌"),
    Command("pause", "暂停当前订阅"),
    Command("resume", "恢复当前订阅"),
    Command("prob", "查看各标的下个收盘涨跌概率及计算过程"),
    Command("book", "对比 Predict 订单簿和模型公平价，看挂涨还是挂跌优势大"),
    Command("edge", "优势提醒门槛：按栏目 / 市场 / 档位单独设置几¢才提醒", "牛来 4",
            "不带参数查看；/edge 6 改默认；/edge 市值阶梯 6、/edge 牛来 4、/edge 牛来 300M 3；末尾 off 取消；/edge 清空"),
    Command("web", "获取概率网页链接（自动刷新）"),
    Command("calib", "用已保存的预测快照和实际收盘给概率模型打分（Brier/校准/逐日向前拟合）"),
    Command("sim", "模拟交易：净优势 ≥10¢ 时买 100 份，看长期是赚还是亏（只记账，不下单）"),
    Command("diag", "逐个检测数据源（币安/交易所/上证/A50/恒指/KOSPI/HL/汇率），找出哪里出问题"),
    Command("test", "发送测试消息，不代表行情正常"),
    Command("id", "查看你的用户 ID、聊天 ID、话题 ID"),
    Command("help", "显示说明"),
)
# Accepted spellings that are not listed in the menu.
COMMAND_ALIASES = {"/start": "/help", "/price": "/status"}
# Threshold choices offered as buttons on the /threshold card (percent deviation from the baseline).
THRESHOLD_PRESETS = ("0.3", "0.5", "1", "1.5", "2", "3", "5", "10")


def threshold_card(current: D) -> tuple[str, dict]:
    """Card text + inline keyboard; the active preset is ticked."""
    buttons = [{"text": ("✅ " if D(p) == current else "") + f"±{p}%", "callback_data": f"threshold:{p}"}
               for p in THRESHOLD_PRESETS]
    text = (f"📏 提醒阈值：跟上一日收盘价（基准）偏离多少才提醒\n当前：严格超过 ±{fmt(current)}%\n\n"
            "点选下方档位即时生效；其他数值请发送 /threshold 0.8。")
    return text, {"inline_keyboard": [buttons[i:i + 4] for i in range(0, len(buttons), 4)]}


COOLDOWN_PRESETS = (0, 60, 300, 600, 1800, 3600)  # seconds; 0 = no repeat while the deviation persists


def cooldown_card(current: int) -> tuple[str, dict]:
    """Card text + inline keyboard for the repeat interval; the active preset is ticked."""
    label = lambda s: "关闭" if s == 0 else f"{s // 60} 分钟" if s % 60 == 0 and s >= 60 else f"{s} 秒"
    buttons = [{"text": ("✅ " if s == current else "") + label(s), "callback_data": f"cooldown:{s}"} for s in COOLDOWN_PRESETS]
    text = (f"🔁 周期重复提醒：偏离持续超过阈值时，每隔多久再提醒一次\n当前：{label(current)}"
            f"{'' if current else '（只在首次超过、档位扩大、方向反转时提醒）'}\n\n点选下方档位即时生效；其他秒数请发送 /cooldown 900。")
    return text, {"inline_keyboard": [buttons[i:i + 3] for i in range(0, len(buttons), 3)]}


def mode_card(current: str) -> tuple[str, dict]:
    """Card text + inline keyboard for the baseline mode; the active one is ticked."""
    names = (("binance_daily", "daily 日K"), ("exchange_close", "exchange 交易所收盘"), ("manual", "manual 手动"))
    buttons = [{"text": ("✅ " if mode == current else "") + name, "callback_data": f"mode:{mode}"} for mode, name in names]
    text = ("🧭 基准模式：涨跌幅相对哪个价格计算\n当前：" + BASELINE_MODES.get(current, current) + "\n\n"
            + "\n".join(f"· {name}：{BASELINE_MODES[mode]}" for mode, name in names)
            + "\n\n点选即时生效（切换后重新判断当前偏离，30 秒最短间隔仍然有效）。")
    return text, {"inline_keyboard": [buttons]}

HELP = "📡 合约昨收偏离提醒\n\n" + "\n".join(c.help_line() for c in COMMANDS) + """

⚠️ 默认并非股票交易所正式昨收，也不是滚动 24h 涨跌幅。
手动价必须与币安合约显示数值采用同一计价口径；不自动换汇。
只有管理员能订阅或修改设置；不会自动下单、撤单。"""


def id_text(user_id: int, chat: int, thread: int) -> str:
    return f"你的用户 ID：{user_id}\n聊天 ID：{chat}\n话题 ID：{thread}"


@dataclass(frozen=True)
class Request:
    """A parsed admin command with the chat it came from."""
    command: str
    args: list[str]
    chat: int
    thread: int
    user_id: int
    raw: str = ""  # Argument text with line breaks kept, for multi-line commands such as /setclose.

    @property
    def sub_id(self) -> str:
        return subscription_key(self.chat, self.thread)


@dataclass(frozen=True)
class Reply:
    """A command reply that needs more than plain text (inline keyboard and/or HTML formatting)."""
    text: str
    markup: dict | None = None
    html: bool = False


DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_day(value: str) -> str:
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError(f"日期格式应为 YYYY-MM-DD：{value}") from None


def short_name(symbol: str) -> str:
    """Shortest ASCII alias for a symbol, used in examples (UNITREEUSDT -> UNITREE)."""
    aliases = [a for a, s in ALIASES.items() if s == symbol and a.isascii()]
    return min(aliases, key=len) if aliases else symbol


SHORT_DATE_RE = re.compile(r"\d{1,2}-\d{1,2}")
TIME_RE = re.compile(r"\d{1,2}:\d{2}")


@dataclass(frozen=True)
class CloseEntry:
    alias: str
    value: D
    day: str        # Applicable Beijing calendar day (YYYY-MM-DD).
    close_at: str   # Beijing close datetime "YYYY-MM-DDTHH:MM", or "" when not given.
    currency: str = ""  # ISO-style code such as HKD/KRW when the price is not in the contract's quote unit.


CURRENCIES = {"USD", "USDT", "USDC", "HKD", "CNY", "CNH", "KRW", "JPY", "TWD", "EUR", "GBP", "SGD", "INR"}
SAME_UNIT = {"", "USD", "USDT", "USDC"}  # Currencies comparable with the USDT-quoted contract price.


def parse_close_time(date_token: str, time_token: str, now_ms: int) -> str:
    """'2026-09-17 16:00' or '09-17 16:00' (Beijing) -> 'YYYY-MM-DDTHH:MM'; must not be in the future."""
    now = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
    try:
        if DATE_RE.fullmatch(date_token):
            day = dt.date.fromisoformat(date_token)
        else:
            month, dom = (int(p) for p in date_token.split("-"))
            day = dt.date(now.year, month, dom)
            if day > now.date():  # e.g. "12-31" typed in early January
                day = dt.date(now.year - 1, month, dom)
        hour, minute = (int(p) for p in time_token.split(":"))
        moment = dt.datetime.combine(day, dt.time(hour, minute), BEIJING)
    except ValueError:
        raise ValueError(f"收盘时间格式应为 MM-DD HH:MM 或 YYYY-MM-DD HH:MM：{date_token} {time_token}") from None
    if moment > now + dt.timedelta(minutes=5):
        raise ValueError(f"收盘时间不能晚于当前时间：{date_token} {time_token}")
    return moment.strftime("%Y-%m-%dT%H:%M")


@dataclass(frozen=True)
class Tail:
    """Optional qualifiers after a price (or before the first symbol, as batch-wide defaults)."""
    day: str
    close_at: str = ""
    currency: str = ""


def parse_close_tail(tokens: list[str], default: Tail, now_ms: int) -> tuple[Tail, list[str]]:
    """Consume leading qualifiers: a date followed by HH:MM is the close time, a lone date the
    applicable day, a currency code (HKD, KRW, ...) the price unit. Returns (tail, remaining tokens)."""
    day, close_at, currency = default.day, default.close_at, default.currency
    i = 0
    while i < len(tokens):
        token, following = tokens[i], tokens[i + 1] if i + 1 < len(tokens) else ""
        if (DATE_RE.fullmatch(token) or SHORT_DATE_RE.fullmatch(token)) and TIME_RE.fullmatch(following):
            close_at = parse_close_time(token, following, now_ms)
            i += 2
        elif DATE_RE.fullmatch(token):
            day = parse_day(token)
            i += 1
        elif token.upper() in CURRENCIES:
            currency = token.upper()
            i += 1
        else:
            break
    return Tail(day, close_at, currency), tokens[i:]


def parse_close_entries(raw: str, now_ms: int, command: str = "/setclose") -> list[CloseEntry]:
    """Parse "SYMBOL PRICE [货币] [收盘日期 HH:MM] [适用日]" entries separated by newlines/commas.

    Qualifiers before the first symbol apply to every entry that has none of its own.
    Nothing is stored here, so a bad line rejects the whole batch.
    """
    raw = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", raw)  # 258,000 is one price, not two entries
    today = beijing_day(now_ms / 1000)
    usage = (f"用法：{command} UNITREE 75 [货币 如 HKD] [收盘时间 MM-DD HH:MM] [适用日期 YYYY-MM-DD]\n"
             "批量：每行（或用逗号分隔）一组「合约 价格 [货币] [收盘时间]」，最前面可写统一适用日/收盘时间/货币，例如\n"
             f"{command} {today}\nUNITREE 75 09-17 16:00\nSHEIN 40 09-17 16:00\n"
             "不带货币时价格须与币安显示值同口径；不自动换汇")
    entries = [e.split() for e in re.split(r"[\n\r,，;；]+", raw) if e.strip()]
    default = Tail(today)
    if entries:
        default, entries[0] = parse_close_tail(entries[0], default, now_ms)
        if not entries[0]:
            entries.pop(0)
    if not entries:
        raise ValueError(usage)
    result = []
    for tokens in entries:
        if len(tokens) < 2:
            raise ValueError(usage)
        alias, value = tokens[0], number(tokens[1], f"{tokens[0]} 价格")
        tail, rest = parse_close_tail(tokens[2:], default, now_ms)
        if rest:
            raise ValueError(f"无法识别「{' '.join(rest)}」\n{usage}")
        result.append(CloseEntry(alias, value, tail.day, tail.close_at, tail.currency))
    return result


class Bot:
    def __init__(self, config: Config, store: Store, market: Binance, telegram: Telegram):
        self.config, self.store, self.market, self.telegram = config, store, market, telegram
        self.snapshots: dict[str, dict] = {}
        self.stocks = StockMarket(config, store)
        self.fx = FxRates(config.fx_manual)
        self.hsi = IndexFutures(config.hsi_futures, config.holidays.get("hk", frozenset()))
        self.hl = Hyperliquid({**config.hl_tickers, **config.hl_index})
        self.kospi = KospiIndex(config.kospi_index, config.holidays.get("kr", frozenset()))
        self.cn = CnIndex(config.sse_index, config.holidays.get("sh", frozenset()))
        self.web_token = config.web_token or self.store.get("web_token") or ""
        if config.web_port and not self.web_token:
            self.web_token = secrets.token_urlsafe(18)
            self.store.put("web_token", self.web_token)
        self.web: WebServer | None = None
        self.vols = VolBook(config.prob_vol)
        self.predict = PredictFeed(config)
        self.hsi_daily = DailyCloses("hk", (
            ("tencent", "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=hkHSI,day,,,10,"),
            ("eastmoney", "https://push2his.eastmoney.com/api/qt/stock/kline/get?klt=101&fqt=0&end=20500101&lmt=10"
                          "&fields1=f1&fields2=f51,f52,f53&secid=100.HSI"),
            ("yahoo", yahoo_url("^HSI"))), config.holidays.get("hk", frozenset()))
        self.hsi.dated_close = lambda day: self.hsi_daily.daily.get(day)  # checks the cash index belongs to the right day
        self.touches = {spec.key: TouchMarket(store, spec) for spec in TOUCH_MARKETS}
        self.updowns = {spec.key: UpDownMarket(store, spec) for spec in UPDOWN_MARKETS}
        self.flips = {spec.key: FlipMarket(store, spec) for spec in FLIP_MARKETS}
        self.caps = {spec.key: CapMarket(store, spec) for spec in CAP_MARKETS}
        self.ranges = {spec.key: range_market(store, spec, self.binance_futures, config) for spec in (*RANGE_MARKETS, *STOCK_HIT_MARKETS)}
        self.predict.ladder_parse.update({key: price_level for key in self.ranges})
        for key, rm in self.ranges.items():
            if isinstance(rm, StockRangeMarket):  # "hits $100 by <date>": the date-titled markets, one per deadline
                self.predict.ladder_parse[key], self.predict.ladder_pick[key] = hit_level_parser(rm.spec.slug), rm.choose
        self.predict.reward_keys.update(self.ranges)
        if config.touch:
            self.predict.want_info.update(spec.slug for spec in (*TOUCH_MARKETS, *UPDOWN_MARKETS, *FLIP_MARKETS))
            self.predict.ladder_keys.update(self.caps)
            self.predict.ladder_keys.update(self.ranges)
        self.exchange_bases: dict[str, Baseline] = {}  # exchange_close mode: held until a newer close is confirmed
        self.reference_tasks: list[asyncio.Task] = []
        self.sim_ran = -1e9  # monotonic time of the paper trader's last look
        self.sim_checked: dict[str, float] = {}  # market id -> monotonic time Predict was last asked for its result
        self.edge_ran = -1e9  # monotonic time of the edge alerts' last look
        self.reference_pool: ThreadPoolExecutor | None = None
        self.reference_state: dict[str, dict] = {}  # per background feed: last success, last error, duration
        self.anchors: dict[str, tuple[int, D]] = {}  # key -> (reference close ms, proxy price then)
        self.kospi_anchor_note, self.kospi_anchor_error = "", ""
        self.pred_last: dict[str, int] = {}  # index -> ms of the last saved prediction snapshot
        self.a50_anchor_note = "15:00"
        self.a50_anchor_error = ""  # why the last anchor lookup failed (shown in /diag and the odds row)
        self.hsi_cfd_error = ""     # why the Sina CFD's 16:10 bar could not be read (when it was needed)
        self.hsi_used: FuturesQuote | None = None  # the futures quote the HSI after-hours odds were last mapped from
        self.a50_anchor_source = "东方财富"
        self.anchor_tries: dict[str, float] = {}
        self.deliveries: dict[str, asyncio.Task] = {}  # Telegram sends in flight, by alert / notice key
        self.faults: dict[tuple[str, str], float] = {}  # (subscription, data item) -> when its current fault began
        self.reply_seq = 0  # command replies get their own delivery keys
        self.last_send_error = ""  # the latest Telegram send failure, for /status
        self.sim_cache: dict[str, dict] | None = None  # in-memory view of the sim: records (sim_trades)
        self.sim_gen = -1
        self.notice_cache: dict[str, dict] | None = None  # in-memory view of the notice: records
        self.notice_gen = -1
        self.pred_pruned = 0.0  # when old prediction snapshots were last pruned
        self.mark_last = -10**15  # ms of the last all-market snapshot (record_marks)
        self.mark_pruned = 0.0
        self.beta_cache: dict[str, tuple[float, float, str]] = {}  # index -> (fitted at, β, how it was obtained)
        self.vol_errors: dict[str, str] = {}  # symbol -> why its exchange daily bars could not be read (σ is the prior)
        self.stopping = asyncio.Event()
        self.started = time.time()
        self.last_cycle = 0.0
        self.last_log: dict[str, float] = {}
        self.command_notice: dict[int, float] = {}
        self.username = ""
        self.handlers = {
            "/help": self.cmd_help, "/id": self.cmd_id, "/status": self.cmd_status, "/test": self.cmd_test,
            "/subscribe": self.cmd_subscribe, "/resume": self.cmd_resume, "/pause": self.cmd_pause,
            "/unsubscribe": self.cmd_unsubscribe, "/threshold": self.cmd_threshold,
            "/cooldown": self.cmd_cooldown, "/mode": self.cmd_mode, "/setclose": self.cmd_setclose,
            "/setexchange": self.cmd_setexchange, "/prob": self.cmd_prob, "/web": self.cmd_web, "/diag": self.cmd_diag, "/calib": self.cmd_calib,
            "/book": self.cmd_book, "/sim": self.cmd_sim, "/edge": self.cmd_edge,
        }

    def settings(self) -> dict:
        saved = self.store.get("settings", {})
        return {"threshold": str(self.config.threshold), "cooldown": self.config.cooldown,
                "mode": self.config.baseline_mode, **saved}

    def update_settings(self, **changes: Any) -> dict:
        settings = {**self.settings(), **changes}
        self.store.put("settings", settings)
        return settings

    def subscriptions(self) -> dict:
        return self.store.get("subscriptions", {})

    def set_subscription(self, req: Request, active: bool) -> None:
        subs = self.subscriptions()
        subs[req.sub_id] = {"chat": req.chat, "thread": req.thread, "active": active}
        self.store.put("subscriptions", subs)

    def exchange_close_ms(self, symbol: str, now_ms: int) -> tuple[int, StockMarketInfo, str]:
        """The instant of the underlying's latest close confirmed by a dated exchange bar.

        There is deliberately no calendar fallback: a weekday guess can land on an unconfigured
        holiday. Without a confirmed close the caller keeps its previous baseline instead.
        """
        ticker = self.config.tickers.get(symbol)
        if not ticker:
            raise ValueError("该合约未配置证券交易所代码（EXCHANGE_TICKERS），无法按交易所收盘时刻取基准")
        info = STOCK_MARKETS[ticker.market]
        ref = self.stocks.closes.get(symbol)
        if ref and ref.close_ms and ref.close_ms + 15 * 60_000 <= now_ms:
            return ref.close_ms, info, f"{info.name}{ticker.code}"
        return 0, info, f"{info.name}{ticker.code}"

    def held_exchange_base(self, symbol: str, ticker: StockTicker) -> Baseline | None:
        """The last exchange-close baseline, from memory or the store (survives restarts)."""
        if symbol in self.exchange_bases:
            return self.exchange_bases[symbol]
        saved = self.store.get(f"exchange_base:{symbol}")
        if not saved or saved.get("ticker") != f"{ticker.market}:{ticker.code}":
            return None
        with contextlib.suppress(Exception):
            info, close_ms = STOCK_MARKETS[ticker.market], int(saved["close_ms"])
            base = self.exchange_base(D(saved["value"]), saved["kind"], info, close_ms, saved["source"])
            self.exchange_bases[symbol] = base
            return base
        return None

    @staticmethod
    def exchange_base(price: D, kind: str, info: StockMarketInfo, close_ms: int, source: str) -> Baseline:
        # Held until the next confirmed close replaces it (a long holiday must not expire it); the
        # cap only stops a feed that has been dead for weeks from anchoring alerts forever.
        return Baseline(price, f"exchange_time:{close_ms}:{price}",
                        f"币安合约{kind}@{info.name}收盘时刻" + info.close_label(close_ms) + f"｜{source}",
                        close_ms + EXCHANGE_BASE_HOLD_DAYS * DAY_MS, close_ms)

    async def exchange_time_baseline(self, symbol: str, now_ms: int) -> Baseline:
        """Binance contract price at the underlying exchange's latest confirmed close: same instant as
        the exchange close, so 相对基准 and 相对交易所 are directly comparable.

        The baseline only moves when a newer close is confirmed by a dated exchange bar. Until then
        (holidays, weekends, a feed outage) the previous one stays in force.
        """
        close_ms, info, source = self.exchange_close_ms(symbol, now_ms)
        ticker = self.config.tickers[symbol]
        held = self.held_exchange_base(symbol, ticker)
        if close_ms and (held is None or close_ms > held.close_ms):
            price, kind = await self.market.price_at(symbol, close_ms)
            base = self.exchange_base(price, kind, info, close_ms, source)
            self.exchange_bases[symbol] = base
            self.store.put(f"exchange_base:{symbol}", {"ticker": f"{ticker.market}:{ticker.code}", "value": str(price),
                                                       "kind": kind, "close_ms": close_ms, "source": source})
            return base
        if held is None and symbol not in self.stocks.closes and symbol not in self.stocks.errors:
            raise PendingData(f"等待首次获取{info.name}{ticker.code}收盘价（后台刷新中）")
        if held is None:
            raise ValueError(f"{info.name}{ticker.code} 的收盘价尚未由带日期的日 K 确认，暂停该合约提醒"
                             "（不按日历推算，以免把假期当成交易日）")
        tz = dt.timezone(dt.timedelta(hours=info.utc_offset))
        held_day = dt.datetime.fromtimestamp(held.close_ms / 1000, tz).date()
        expected = expected_close_date(ticker.market, now_ms, self.config.holidays.get(ticker.market, frozenset()))
        if expected > held_day:
            return dataclasses.replace(held, label=held.label + f"｜⏳ {expected.strftime('%m-%d')} 收盘待确认，沿用此基准")
        return held

    def manual_baseline_for(self, symbol: str, now_ms: int) -> Baseline:
        day = beijing_day(now_ms / 1000)
        return manual_baseline(self.store.get(f"manual:{symbol}:{day}"), now_ms)

    async def wait(self, seconds: float) -> None:
        """Sleep, but wake immediately when a stop is requested."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.stopping.wait(), timeout=seconds)

    def resolve_symbol(self, value: str) -> str:
        symbol = ALIASES.get(value.upper(), value.upper())
        if symbol not in self.config.symbols:
            raise ValueError("未知合约。支持：" + ", ".join(self.config.symbols))
        return symbol

    def log_limited(self, key: str, text: str, every: int = 60) -> None:
        now = time.monotonic()
        if now - self.last_log.get(key, -1e9) >= every:
            LOG.warning("%s", clean_error(text))
            self.last_log[key] = now

    async def tell(self, chat: int, thread: int, text: str, reply_markup: dict | None = None,
                   html_mode: bool = False, fresh: Any = None, render: Any = None) -> bool:
        """Send one message; True once Telegram accepted it. ``fresh`` (optional) is re-checked right before sending,
        also after any wait in the send queue: when it says the data no longer holds, nothing is sent (False).
        ``render`` (optional, returns the text) is called right before sending too, so the text speaks of that moment."""
        token = SEND_CHECK.set(fresh)
        rtoken = SEND_RENDER.set(None if render is None else (lambda: to_html(render())) if html_mode else render)
        try:
            if fresh is not None and not fresh():
                raise StaleMessage("数据已失效，未发送")
            if render is not None:
                text = render()
            if html_mode:
                await self.telegram.send(chat, thread, to_html(text), reply_markup, "HTML")
            else:
                await self.telegram.send(chat, thread, text, reply_markup)
            return True
        except StaleMessage as error:
            LOG.info("Telegram message dropped: %s", error)
            return False
        except Exception as error:
            gone = chat_gone(error)
            if gone:
                self.suspend_target(chat, thread, gone)
            self.last_send_error = f"{stamp(time.time() * 1000, seconds=False)} chat {chat}：{brief_error(clean_error(error), 80)}"
            self.log_limited(f"telegram_send:{chat}", f"Telegram 发送失败（chat {chat}）：{clean_error(error)}")
            return False
        finally:
            SEND_CHECK.reset(token)
            SEND_RENDER.reset(rtoken)

    def suspend_target(self, chat: int, thread: int, why: str) -> None:
        """Telegram refuses this chat for good (bot blocked or kicked, topic deleted): pause its subscription instead
        of retrying every cycle, and tell the administrator. /resume (or /subscribe) in that chat turns it back on."""
        subs = self.subscriptions()
        sub_id = subscription_key(chat, thread)
        sub = subs.get(sub_id)
        if not sub or not sub.get("active"):
            return
        subs[sub_id] = {**sub, "active": False, "suspended": why, "suspended_at": time.time()}
        self.store.put("subscriptions", subs)
        LOG.warning("订阅 %s 已自动暂停：Telegram 拒绝投递（%s）", sub_id, why)
        if self.config.admin_id and (chat, thread) != (self.config.admin_id, 0):
            text = (f"⏸ 订阅 {sub_id} 已自动暂停\nTelegram 拒绝向该聊天投递：{why}\n"
                    "机器人可能已被移出群组、被拉黑，或话题已删除/关闭。处理后在该聊天发送 /resume 恢复。")
            self.deliver(f"notice:suspended:{sub_id}", functools.partial(self.tell, self.config.admin_id, 0, text))

    # --- delivery: sampling never waits on Telegram -------------------------------------------------------------------

    def delivering(self, key: str) -> bool:
        task = self.deliveries.get(key)
        return task is not None and not task.done()

    def deliver(self, key: str, job: Any) -> bool:
        """Run one Telegram delivery in the background, so the 5-second sampling never waits on Telegram (a 429 cool-
        down can last minutes). One delivery per key at a time: while it is in flight the same message is not planned
        again; it re-checks its data right before sending and records itself only once Telegram accepted it."""
        if self.delivering(key) or self.stopping.is_set():
            return False

        async def guarded() -> None:
            try:
                await job()
            except Exception as error:  # a delivery must never take the bot down
                self.log_limited("deliver", f"消息投递异常：{clean_error(error)}")
        self.deliveries = {k: t for k, t in self.deliveries.items() if not t.done()}
        self.deliveries[key] = asyncio.create_task(guarded(), name=f"deliver:{key}")
        return True

    async def drain_deliveries(self) -> None:
        """Wait for every delivery in flight (one-off runs and tests have no background to leave them to)."""
        while pending := [t for t in self.deliveries.values() if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)
        self.deliveries.clear()

    def notice(self, sub_id: str, sub: dict, key: str, error: str | None) -> None:
        """A fault / recovery notice for one subscription, sent in the background and recorded once delivered.
        A fault is announced only once it has lasted NOTICE_GRACE_SECONDS (one timed-out request is not an outage),
        then at most every 30 minutes; the recovery note follows only a fault that was announced."""
        record_key = f"notice:{sub_id}:{key}"
        if self.delivering(record_key):
            return
        now = time.time()
        if error:
            since = self.faults.setdefault((sub_id, key), now)
            if now - since < NOTICE_GRACE_SECONDS:
                return
            old = self.notice_records().get(record_key, {})
            if old.get("active") and now - old.get("sent", 0) < 1800:
                return
            lasting = f"已持续 {int((now - since) // 60)} 分钟｜" if now - since >= 60 else ""
            text = f"⚠️ 行情监控异常｜{key}\n{lasting}{error}\n该项暂停涨跌提醒；恢复后继续。\n这不代表价格没有变化。"
            state = {"active": True, "sent": now}
        else:
            self.faults.pop((sub_id, key), None)
            old = self.notice_records().get(record_key, {})
            if not old.get("active"):
                return
            text, state = f"✅ 数据恢复｜{key}\n后续按当前基准继续监控。", {"active": False, "sent": now}

        async def send() -> None:
            if await self.tell(sub["chat"], sub["thread"], text):
                self.store.put(record_key, state)
        self.deliver(record_key, send)

    def notice_records(self) -> dict[str, dict]:
        """notice:* records as an in-memory view (read every cycle for every subscription and contract), refreshed
        whenever one is written or deleted."""
        generation = self.store.touched.get("notice", 0)
        if self.notice_cache is None or self.notice_gen != generation:
            self.notice_cache = {k: v for k, v in self.store.items("notice:") if isinstance(v, dict)}
            self.notice_gen = generation
        return self.notice_cache

    def parse_request(self, message: dict) -> Request | None:
        """Return the command in ``message`` when it is addressed to this bot, else None."""
        text = message.get("text", "").strip()
        if not text.startswith("/"):
            return None
        parts = text.split()
        raw_command, _, mention = parts[0].lower().partition("@")
        if mention and self.username and mention != self.username.lower():
            return None
        chat, thread = target_from_message(message)
        user_id = int((message.get("from") or {}).get("id") or 0)
        return Request(COMMAND_ALIASES.get(raw_command, raw_command), parts[1:], chat, thread, user_id,
                       raw=text[len(parts[0]):].strip())

    async def process_message(self, message: dict) -> None:
        req = self.parse_request(message)
        if req is None:
            return
        if not is_admin(message, self.config):
            # Only /id and public help work before administrator configuration. Everything else
            # is ignored so settings are never exposed to, or altered by, arbitrary group members.
            if req.command in {"/id", "/help"}:
                await self.public_id_notice(req)
            return
        markup, html_mode = None, False
        try:
            handler = self.handlers.get(req.command)
            if handler is None:
                if req.chat < 0 and "@" not in message.get("text", "").split()[0]:
                    return  # a group's command for some other bot: not ours to answer
                reply: Any = "未知命令。发送 /help 查看用法。"
            else:
                reply = handler(req)
            if inspect.isawaitable(reply):
                reply = await reply
            if isinstance(reply, tuple):  # (text, inline keyboard) card
                reply, markup = reply
            elif isinstance(reply, Reply):
                reply, markup, html_mode = reply.text, reply.markup, reply.html
        except (ValueError, decimal.InvalidOperation) as error:
            reply = "❌ " + clean_error(error)
        except Exception as error:  # a bug in one handler must not leave the administrator without any answer
            LOG.exception("命令 %s 处理失败", req.command)
            reply = (f"❌ 命令执行失败：{clean_error(error) or type(error).__name__}\n"
                     "已写入日志；可发 /diag 检查数据源，或稍后重试。")
        await self.reply(req, reply, markup, html_mode)

    async def reply(self, req: Request, text: str, markup: dict | None = None, html_mode: bool = False) -> None:
        """Answer a command. While the background loops run the answer is queued like any other message, so a
        Telegram cool-down never stalls the command poll (the next command is read at once); one-off runs and tests
        send inline."""
        await self.background("reply", functools.partial(self.tell, req.chat, req.thread, text, markup, html_mode))

    async def background(self, kind: str, job: Any) -> None:
        """Run one Telegram request from the command poll: queued (a delivery of its own) while the background loops run,
        so a chat's cool-down or a slow send never holds getUpdates; inline in one-off runs and tests."""
        if not self.reference_tasks:
            await job()
            return
        self.reply_seq += 1
        self.deliver(f"{kind}:{self.reply_seq}", job)

    async def process_callback(self, query: dict) -> None:
        """Handle a tap on a card button (callback_query)."""
        query_id = str(query.get("id", ""))
        message = query.get("message") or {}

        async def answer(text: str, alert: bool = False) -> None:
            with contextlib.suppress(Exception):  # A missed toast must not abort the update.
                await self.telegram.call("answerCallbackQuery",
                                         {"callback_query_id": query_id, "text": text[:200], "show_alert": alert})

        if not is_admin(query, self.config):
            await answer("仅管理员可以修改设置")
            return
        kind, _, value = str(query.get("data", "")).partition(":")
        try:
            if kind == "threshold":
                toast = self.apply_threshold(value)
                text, markup = threshold_card(D(self.settings()["threshold"]))
            elif kind == "cooldown":
                toast = self.apply_cooldown(value)
                text, markup = cooldown_card(int(self.settings()["cooldown"]))
            elif kind == "mode":
                full = self.apply_mode(value)
                toast = full.splitlines()[0]
                text, markup = mode_card(self.settings()["mode"])
                if self.settings()["mode"] == "manual" and message.get("chat"):  # the template to fill in is worth a message of its own
                    await self.reply(Request("/mode", [], int(message["chat"]["id"]), int(message.get("message_thread_id") or 0),
                                             int((query.get("from") or {}).get("id") or 0)), full)
            else:
                raise ValueError("未知操作，请重新发送命令")
        except (ValueError, decimal.InvalidOperation) as error:
            await answer("❌ " + clean_error(error), alert=True)
            return
        await answer(toast)
        if message.get("message_id") and message.get("chat"):
            async def edit(chat: int = int(message["chat"]["id"]), message_id: int = int(message["message_id"])) -> None:
                try:
                    await self.telegram.edit(chat, message_id, text, markup)
                except RemoteError as error:
                    # "message is not modified" when re-selecting the current preset is harmless.
                    if "not modified" not in str(error):
                        self.log_limited("telegram_edit", f"卡片更新失败：{clean_error(error)}")
            await self.background("edit", edit)

    async def public_id_notice(self, req: Request) -> None:
        now = time.monotonic()
        if now - self.command_notice.get(req.user_id, -1e9) < 10:
            return
        if len(self.command_notice) > 2000:
            self.command_notice.clear()
        self.command_notice[req.user_id] = now
        intro = ("ℹ️ 这是一个合约涨跌提醒机器人：订阅、查看状态和修改设置只有管理员能做。\n" if req.command == "/help" else "")
        await self.background("reply", functools.partial(
            self.tell, req.chat, req.thread, intro + id_text(req.user_id, req.chat, req.thread) +
            "\n如果你是部署者：把你的用户 ID 填入 Railway 的 ADMIN_USER_ID 后重新部署，再发 /subscribe。"))

    # --- command handlers: each returns the reply text or raises ValueError with the usage hint ---

    def cmd_help(self, req: Request) -> str:
        return HELP

    def cmd_id(self, req: Request) -> str:
        return id_text(req.user_id, req.chat, req.thread)

    def cmd_status(self, req: Request) -> "Reply":
        return Reply(self.status(req.sub_id), html=True)

    def cmd_test(self, req: Request) -> str:
        return "✅ TG 测试消息发送成功。\n此测试仅验证推送，行情是否正常请看 /status。"

    def cmd_subscribe(self, req: Request) -> "Reply":
        self.set_subscription(req, active=True)
        return Reply("✅ 当前私聊/话题已订阅。首次观察就超过阈值，也会提醒。\n下面是当前状态（随时可发 /status 再看）：\n\n"
                     + self.status(req.sub_id), html=True)

    def cmd_resume(self, req: Request) -> str:
        sub = self.subscriptions().get(req.sub_id)
        if sub is None:
            raise ValueError("当前私聊/话题还没有订阅；请先发送 /subscribe")
        if sub.get("active"):
            return "ℹ️ 当前订阅本来就在运行中，无需恢复。\n" + self.config_summary()
        self.set_subscription(req, active=True)
        why = f"\n（此前因「{sub['suspended']}」被自动暂停）" if sub.get("suspended") else ""
        return "✅ 当前订阅已恢复。" + why + "\n" + self.config_summary()

    def cmd_pause(self, req: Request) -> str:
        sub = self.subscriptions().get(req.sub_id)
        if sub is None:
            raise ValueError("当前私聊/话题没有订阅，无需暂停；/subscribe 可以订阅")
        if not sub.get("active"):
            return "ℹ️ 当前订阅已经是暂停状态；/resume 可以恢复。"
        self.set_subscription(req, active=False)
        return "⏸ 当前订阅已暂停；/resume 可以恢复。"

    def cmd_unsubscribe(self, req: Request) -> str:
        subs = self.subscriptions()
        if req.sub_id not in subs:
            return "ℹ️ 当前私聊/话题没有订阅，无需取消。"
        subs.pop(req.sub_id, None)
        self.store.put("subscriptions", subs)
        self.store.delete_prefix(f"alert:{req.sub_id}:")
        self.store.delete_prefix(f"notice:{req.sub_id}:")
        return "✅ 已取消当前私聊/话题的订阅。"

    def cmd_threshold(self, req: Request) -> str | tuple[str, dict]:
        if not req.args:
            return threshold_card(D(self.settings()["threshold"]))
        if len(req.args) != 1:
            raise ValueError("用法：/threshold 1（1 表示 1%），或直接发 /threshold 选择档位")
        return self.apply_threshold(req.args[0])

    def apply_threshold(self, raw: str) -> str:
        """Validate and persist a new global threshold; shared by the command and the card buttons."""
        value = number(raw.rstrip("%"), "阈值")
        if not D("0.01") <= value <= D(100):
            raise ValueError("阈值必须在 0.01～100 之间")
        self.update_settings(threshold=str(value))
        self.reset_alerts()
        return f"✅ 全局阈值已改为严格超过 ±{fmt(value)}%。下一轮按新阈值判断。"

    def reset_alerts(self) -> None:
        """Open a fresh alert episode for every subscription and contract (a changed threshold / mode re-judges the
        current deviation), keeping only each one's last send time so the MIN_ALERT_GAP spam guard still holds."""
        self.store.put_many((key, {"last_sent": value.get("last_sent", 0)})
                            for key, value in self.store.items("alert:") if isinstance(value, dict))

    def cmd_cooldown(self, req: Request) -> str | tuple[str, dict]:
        if not req.args:
            return cooldown_card(int(self.settings()["cooldown"]))
        if len(req.args) != 1:
            raise ValueError("用法：/cooldown 300，范围 0～86400 秒；0 关闭周期重复提醒；不带数字弹出档位卡片")
        return self.apply_cooldown(req.args[0])

    def apply_cooldown(self, raw: str) -> str:
        if not raw.isdigit() or not 0 <= int(raw) <= 86400:
            raise ValueError("用法：/cooldown 300，范围 0～86400 秒；0 关闭周期重复提醒")
        seconds = int(raw)
        self.update_settings(cooldown=seconds)
        return f"✅ 周期重复提醒间隔：{seconds} 秒（0 表示关闭）。"

    def cmd_mode(self, req: Request) -> str | tuple[str, dict]:
        if not req.args:
            return mode_card(self.settings()["mode"])
        if len(req.args) != 1:
            raise ValueError("用法：/mode daily、/mode exchange 或 /mode manual；不带参数弹出模式卡片")
        return self.apply_mode(req.args[0])

    def apply_mode(self, choice: str) -> str:
        """Validate and persist the baseline mode; shared by the command and the card buttons."""
        choice = choice.lower()
        aliases = {"daily": "binance_daily", "binance_daily": "binance_daily", "manual": "manual",
                   "exchange": "exchange_close", "exchange_close": "exchange_close", "stock": "exchange_close"}
        if choice not in aliases:
            raise ValueError("用法：/mode daily、/mode exchange 或 /mode manual")
        settings = self.update_settings(mode=aliases[choice])
        self.snapshots.clear()
        self.reset_alerts()
        reply = "✅ " + self.config_summary()
        if settings["mode"] == "exchange_close":
            missing = [s for s in self.config.symbols if s not in self.config.tickers]
            reply += "\n基准取币安合约在标的交易所最近一次收盘时刻的价格，与“证券交易所收盘价”同一时点，两者可直接对比。"
            if missing:
                reply += "\n⚠️ 未配置交易所代码、无法取基准的合约：" + "、".join(missing) + "（见 EXCHANGE_TICKERS）"
        if settings["mode"] == "manual":
            reply += ("\n请设置参考价：复制下面模板，把“价格”换成数值，“MM-DD HH:MM”换成该价格的收盘时间"
                      "（北京时间，可删掉不填）。缺失/过期的合约不发涨跌提醒。\n"
                      + self.setclose_template(beijing_day(self.market.now_ms() / 1000)))
        return reply

    def cmd_setclose(self, req: Request) -> str:
        lines = self.store_prices(req, "manual")
        lines.append("这是你输入的参考价，未独立核验，不自动换汇。")
        if self.settings()["mode"] != "manual":
            lines.append(f"当前基准为「{BASELINE_MODES.get(self.settings()['mode'], self.settings()['mode'])}」，不是手动模式；发 /mode manual 后才会使用这些价格。")
        return "\n".join(lines)

    def cmd_setexchange(self, req: Request) -> str:
        lines = self.store_prices(req, "exchange")
        lines.append("证券交易所收盘价仅作对照显示，不参与触发判断。带非美元货币时只展示、不计算涨跌；不自动换汇。")
        return "\n".join(lines)

    def store_prices(self, req: Request, kind: str) -> list[str]:
        """Parse a (batch) price message and persist it under ``kind``; returns one reply line per record."""
        label, command, _ = PRICE_KINDS[kind]
        now_ms = self.market.now_ms()
        today = beijing_day(now_ms / 1000)
        # Resolve and validate every line first so a typo in one line does not half-apply the batch.
        seen: dict[tuple[str, str], CloseEntry] = {}
        for entry in parse_close_entries(req.raw, now_ms, command):
            symbol = self.resolve_symbol(entry.alias)
            if kind == "manual" and entry.currency not in SAME_UNIT:
                raise ValueError(f"{symbol}：手动参考价必须与币安合约同口径，不能带 {entry.currency}；"
                                 "交易所本币价格请用 /setexchange 记录")
            previous = seen.get((symbol, entry.day))
            if previous and previous != entry:
                raise ValueError(f"{symbol} 在 {entry.day} 出现了两条不同的记录，请只保留一条")
            seen[(symbol, entry.day)] = entry
        lines = []
        for (symbol, day), entry in seen.items():
            record = {"value": str(entry.value), "valid_date": day}
            if entry.close_at:
                record["close_at"] = entry.close_at
            if entry.currency:
                record["currency"] = entry.currency
            self.store.put(f"{kind}:{symbol}:{day}", record)
            if kind == "manual":
                self.snapshots.pop(symbol, None)
            unit = f" {entry.currency}" if entry.currency else ""
            close = f"｜收盘 {entry.close_at[5:].replace('T', ' ')}（北京时间）" if entry.close_at else "｜未填收盘时间"
            lines.append(f"✅ {symbol} {label}：{fmt(entry.value)}{unit}｜适用日 {day}（北京时间）{close}")
        missing = [s for s in self.config.symbols if not self.store.get(f"{kind}:{s}:{today}")]
        if missing and (kind in REFERENCE_KINDS or self.settings()["mode"] == "manual"):
            lines.append(f"今日（{today}）尚未设置{label}：" + "、".join(missing))
        return lines

    def reference_for(self, kind: str, symbol: str, now_ms: int) -> Baseline | None:
        """Today's reference close of ``kind`` for ``symbol``: a manual record wins over the fetched one."""
        try:
            return manual_baseline(self.store.get(f"{kind}:{symbol}:{beijing_day(now_ms / 1000)}"), now_ms, kind)
        except ValueError:
            return self.stocks.closes.get(symbol) if kind == "exchange" else None

    def reference_status(self, kind: str, symbol: str, price: D, now_ms: int) -> list[str]:
        ref = self.reference_for(kind, symbol, now_ms)
        error = self.stocks.errors.get(symbol) if kind == "exchange" else None
        if ref is None and error:
            return [f"交易所 ⚠️ 获取失败（{error}）；可用 /setexchange 手动记录"]
        rows = [reference_row(kind, price, ref, self.fx, self.config.color_style)]
        if error and ref and ref.source:
            rows.append(f"⚠️ 交易所沿用上次数据，刷新失败：{brief_error(error)}")
        return rows

    def usd_price(self, symbol: str, price: D) -> tuple[D | None, str]:
        """The contract price in USD: quanto contracts quoted in the stock's currency are converted."""
        ticker = self.config.tickers.get(symbol)
        if ticker and ticker.same_unit:
            currency = STOCK_MARKETS[ticker.market].currency
            rate = self.fx.rate(currency)
            return (price / rate if rate else None), f"（{currency} 折美元）"
        return price, ""

    def hl_line(self, symbol: str, price: D) -> str:
        usd, note = self.usd_price(symbol, price)
        return self.hl.line(symbol, usd, self.config.color_style, note)

    # --- probability inputs and odds -----------------------------------------------------------------

    async def refresh_odds_inputs(self, now_ms: int) -> Refreshed:
        """Proxy prices at each reference close and volatility estimates (cached; best effort)."""
        for symbol, ticker in self.config.tickers.items():
            self.note_live_close(symbol, ticker, now_ms)
            close_ms = self.odds_base(symbol, now_ms)[0]
            if close_ms and self.anchors.get(symbol, (0,))[0] != close_ms and self.retry_ok(symbol):
                with contextlib.suppress(Exception):
                    price, _ = await self.market.price_at(symbol, close_ms)
                    self.anchors[symbol] = (close_ms, price)
            if self.vols.due(symbol):
                # The stock's own exchange sessions (close to close, opens kept so the opening gap and the in-session
                # part split as for the indices) – not Binance's 24-hour candles, which mix in nights and weekends.
                self.vols.refreshed[symbol] = time.monotonic()
                try:
                    bars, source = await self.stocks.daily_ohlc(ticker, now_ms)
                    bars = bars[-31:]
                    self.vols.record(symbol, [close for _, _, close in bars], f"{source}日K", [o for _, o, _ in bars])
                    self.vol_errors.pop(symbol, None)
                except Exception as error:
                    self.vol_errors[symbol] = clean_error(error) or type(error).__name__
                    self.vols.refreshed[symbol] = time.monotonic() - VolBook.REFRESH_SECONDS + 600  # again in 10 minutes
        kospi, hl = self.kospi.quote, self.hl.quotes.get("KR200")
        if kospi and hl:
            await self.kospi_anchor(kospi, hl)
        for q in {id(q): q for q in (self.hsi.quote, *self.hsi.families.values()) if q is not None}.values():
            self.note_hsi_close_print(q)  # each family keeps its own print at the 16:10 close
        await self.recover_hsi_cfd_anchor(now_ms)

        a50 = self.cn.a50
        if a50 is not None:
            # Remember the first A50 print after each 15:00 close as it arrives: the close itself is only
            # confirmed by the daily bar ~15 minutes later, too late to catch it then.
            quoted = dt.datetime.fromtimestamp(a50.quoted_ms / 1000, BEIJING)
            at_close = int(dt.datetime.combine(quoted.date(), dt.time(15, 0), BEIJING).timestamp() * 1000)
            key = f"a50_print:{quoted.date().isoformat()}:{a50_family(a50.source)}"
            if (0 <= a50.quoted_ms - at_close <= 5 * 60_000 and quoted.weekday() < 5
                    and quoted.date() not in self.config.holidays.get("sh", frozenset()) and not self.store.get(key)):
                self.store.put(key, [a50.quoted_ms, str(a50.last)])
        close_ms = self.sse_close_ms(now_ms)
        if close_ms:
            saved = self.store.get("anchor:A50", ())
            if self.anchors.get("A50", (0,))[0] != close_ms and isinstance(saved, (list, tuple)) and len(saved) >= 2:
                with contextlib.suppress(ValueError, TypeError, decimal.InvalidOperation):
                    if int(saved[0]) == close_ms and D(str(saved[1])) > 0:
                        self.anchors["A50"] = (close_ms, D(str(saved[1])))
                        if len(saved) >= 4 and str(saved[2]).startswith("15:00") and saved[3] in {"东方财富", "新浪CFD"}:
                            self.a50_anchor_note, self.a50_anchor_source = saved[2], saved[3]
                        else:
                            # Old records could be either the futures feed or a CFD: recheck before using.
                            self.a50_anchor_note, self.a50_anchor_source = "旧版锚点来源未记录", "未知"
            anchor = self.anchors.get("A50")
            day = dt.datetime.fromtimestamp(close_ms / 1000, BEIJING).date()
            live = a50_family(a50.source) if a50 else "东方财富"
            # The anchor must come from the same record as the live quote, or the two cannot be compared.
            chain = ([(self.cn.a50_sina_five_minute_at, "15:00 五分钟K近似")] if live == "新浪CFD" else
                     [(self.cn.a50_at, "15:00"), (self.cn.a50_five_minute_at, "15:00 五分钟K近似")])
            wrong = (not anchor or anchor[0] != close_ms or self.a50_anchor_source == "未知"
                     or a50_family(self.a50_anchor_source) != live)
            if wrong and self.retry_ok("A50"):
                price = None
                failures = []
                for fetch, note in chain:
                    try:
                        price = await fetch(day)
                        break
                    except Exception as error:
                        failures.append(clean_error(error))
                printed = self.store.get(f"a50_print:{day.isoformat()}:{live}")
                if price is None and printed:
                    with contextlib.suppress(ValueError, TypeError, IndexError, decimal.InvalidOperation):
                        price, note = D(str(printed[1])), a50_print_note(int(printed[0]), close_ms)  # recorded as it happened
                if price is None and a50 and 0 <= a50.quoted_ms - close_ms <= 5 * 60_000:
                    price, note = a50.last, a50_print_note(a50.quoted_ms, close_ms)
                self.a50_anchor_error = "；".join(failures) if price is None else ""
                if price is not None:
                    self.anchors["A50"] = (close_ms, price)
                    self.a50_anchor_note, self.a50_anchor_source = note, live
                    self.store.put("anchor:A50", [close_ms, str(price), note, live])
            elif (not wrong and live == "东方财富" and self.a50_anchor_note != "15:00"
                  and self.retry_ok("A50-exact", every=600)):
                # Upgrade an approximate Eastmoney anchor once the one-minute history is back.
                with contextlib.suppress(Exception):
                    price = await self.cn.a50_at(day)
                    self.anchors["A50"] = (close_ms, price)
                    self.a50_anchor_note, self.a50_anchor_source = "15:00", "东方财富"
                    self.store.put("anchor:A50", [close_ms, str(price), "15:00", "东方财富"])
        if self.vols.due("SSE") and len(self.cn.bars) > 2:  # the dated bars CnIndex already read
            self.vols.refreshed["SSE"] = time.monotonic()
            done = finished_bars(self.cn.bars, "sh", now_ms)
            self.vols.record("SSE", [close for _, close in done], "上证日K", [self.cn.opens.get(day) for day, _ in done])
        # Index volatility: KOSPI from Naver; HSI from Tencent, else Eastmoney. One attempt per window.
        sources = {"KOSPI": (("https://fchart.stock.naver.com/sise.nhn?requestType=0&timeframe=day&count=40&symbol=KOSPI", "naver"),),
                   "HSI": (("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=hkHSI,day,,,40,", "tencent"),
                           ("https://push2his.eastmoney.com/api/qt/stock/kline/get?klt=101&fqt=0&end=20500101&lmt=40"
                            "&fields1=f1&fields2=f51,f52,f53&secid=100.HSI", "eastmoney"))}
        for key, urls in sources.items():
            if not self.vols.due(key):
                continue
            self.vols.refreshed[key] = time.monotonic()
            for url, kind in urls:
                try:
                    raw = await fetch_source(url)
                    market = "kr" if key == "KOSPI" else "hk"
                    if kind == "tencent":
                        days = json.loads(raw)["data"]["hkHSI"]
                        bars = [(dt.date.fromisoformat(r[0]), _open_price(r[1]), number(r[2], "收盘"))
                                for r in (days.get("day") or days.get("qfqday") or []) if len(r) > 2]
                    else:
                        bars = parse_daily_ohlc(market, raw)
                    bars = finished_bars(sorted(bars, key=lambda bar: bar[0]), market, now_ms)  # today's bar is still moving
                    if len(bars) > 2:
                        self.vols.record(key, [c for _, _, c in bars], "指数日K", [o for _, o, _ in bars])
                        break
                except Exception:
                    continue
        self.record_predictions(now_ms)
        return refreshed([f"A50 锚点：{self.a50_anchor_error}" if self.config.sse_index and self.a50_anchor_error else "",
                          f"KR200 锚点：{self.kospi_anchor_error}" if self.config.kospi_index and self.kospi_anchor_error else ""], 1)

    def predict_mid(self, key: str, slug: str, now_ms: int) -> float | None:
        """The 涨 / Yes middle of the Predict book a card is compared with right now (None without a fresh book for
        that very market)."""
        book = self.predict.books.get(key)
        if book is None or (slug and book.slug != slug) or book.stale(now_ms):
            return None
        return side_mid(book, "up")

    async def record_marks(self, now_ms: int) -> "Refreshed | bool":
        """Every two hours, what every priced Predict market showed (the ladders' levels included): the model's fair
        price for 涨 / Yes, the book's middle, the bar and whether the card held back. /calib scores them by kind once
        the markets have results: the model against the market it was trading against, which the index snapshots
        (pred:, every 30 minutes, with the proxy fit's inputs) cannot say for the crypto and ladder cards."""
        if now_ms - self.mark_last < MARK_EVERY_MS:
            return False
        self.mark_last = now_ms
        rows = []
        for mk in self.sim_markets(now_ms):
            rows.append((f"mark:{mk.market}:{now_ms}", {
                "market": mk.market, "item": mk.item, "kind": mk.kind, "key": mk.key, "t": now_ms, "up": mk.fair_up,
                "mkt": None if mk.book.stale(now_ms) else side_mid(mk.book, "up"), "need": mk.need, "hold": mk.hold,
                "settle": mk.settle, "driver": market_driver(mk.kind, mk.key, mk.settle, mk.item)[0]}))
        if rows:
            self.store.put_many(rows)
        if time.time() - self.mark_pruned > 86400:
            self.mark_pruned = time.time()
            cutoff = now_ms - MARK_KEEP_DAYS * DAY_MS
            old = [k for k in self.store.keys("mark:") if k.rsplit(":", 1)[-1].isdigit() and int(k.rsplit(":", 1)[-1]) < cutoff]
            if old:
                self.store.delete_keys(old)
        return Refreshed("ok")

    def mark_report(self) -> list[str]:
        """The all-market snapshots scored by kind, each market weighted once (its snapshots share one result): the
        model's Brier, and against the Predict middle where one was saved. A market's result comes from the bot's own
        settlement logic (sim_result), so a ladder level counts once its window is read to the end or it was touched."""
        rows = [v for _, v in self.store.items("mark:") if isinstance(v, dict) and v.get("up") is not None]
        if not rows:
            return []
        now_ms = self.market.now_ms()
        results: dict[str, float | None] = {}
        for r in rows:
            market = r["market"]
            if market in results:
                continue
            pseudo = {"kind": r.get("kind"), "key": r.get("key"), "market": market, "settle": r.get("settle") or {}, "side": "up"}
            try:
                res = self.sim_result(pseudo, now_ms)
            except Exception:  # a record whose market the bot no longer prices: not scored
                res = None
            results[market] = res[0] if res else None
        lines = [f"📊 全市场快照（每 {MARK_EVERY_MS // 3_600_000} 小时一条，含阶梯各档，保留 {MARK_KEEP_DAYS} 天；各市场计一个权重）："]
        for kind, name in SIM_KINDS.items():
            mine = [r for r in rows if r.get("kind") == kind]
            if not mine:
                continue
            scored = [dict(r, hit=results[r["market"]]) for r in mine if results.get(r["market"]) is not None]
            markets = len({r["market"] for r in mine})
            if not scored:
                lines.append(f"  {name}：快照 {len(mine)} 条 / {markets} 个市场，还没有已出结果的市场")
                continue
            per_market: dict[str, int] = {}
            for r in scored:
                per_market[r["market"]] = per_market.get(r["market"], 0) + 1
            weight = lambda r: 1 / per_market[r["market"]]
            total = sum(weight(r) for r in scored)
            brier = sum(weight(r) * (r["up"] - r["hit"]) ** 2 for r in scored) / total
            lines.append(f"  {name}：{len(per_market)} 个市场已出结果（{len(scored)} 条，共 {markets} 个市场）：模型 Brier {brier:.3f}（抛硬币 0.250）")
            baseline = market_baseline_line(scored, "个市场")
            if baseline:
                lines.append("  " + baseline.strip())
        return lines

    def record_predictions(self, now_ms: int) -> None:
        """Save what each index card showed (every 30 min) and each official close as it becomes known, so the model
        can later be scored against real outcomes (/calib). The score uses the odds as displayed – against the
        Predict market's target price when the page used it – with the model's own figures, the target price and the
        market's link kept beside them."""
        for key, title, odds_of in (("SSE", "上证指数", self.sse_odds), ("KOSPI", "KOSPI", self.kospi_odds),
                                    ("HSI", "恒生指数", self.hsi_odds)):
            raw = odds_of(now_ms)
            if not isinstance(raw, CloseOdds) or now_ms - self.pred_last.get(key, -PRED_EVERY_MS) < PRED_EVERY_MS:
                continue
            self.pred_last[key] = now_ms
            shown = self.settle_on_strike(title, raw)  # exactly what the web page and /prob display
            stem = self.config.predict_slugs.get(key) if self.config.predict else None
            slug = predict_slug(stem, raw.target) if stem else ""
            self.store.put(f"pred:{key}:{now_ms}", {
                "key": key, "t": now_ms, "target": raw.target.isoformat(), "mode": raw.mode,
                "ref": float(shown.ref), "up": shown.fair_up, "eff": float(shown.effective), "warn": shown.warn,
                "mkt": self.predict_mid(key, slug, now_ms),  # the market's own 涨 price then: /calib scores both
                "ref_raw": float(raw.ref), "up_raw": raw.fair_up, "strike": float(shown.ref) if shown.ref != raw.ref else None,
                "move": raw.move, "beta": raw.beta, "sigma": raw.sigma_daily, "R": raw.remaining, "proxy": raw.proxy_note,
                "slug": slug, "url": predict_url(slug, self.config.predict_ref) if slug else ""})
        if time.time() - self.pred_pruned > 86400:  # the snapshots are kept PRED_KEEP_DAYS, not forever
            self.pred_pruned = time.time()
            cutoff = now_ms - PRED_KEEP_DAYS * DAY_MS
            old = [k for k in self.store.keys("pred:") if k.rsplit(":", 1)[-1].isdigit() and int(k.rsplit(":", 1)[-1]) < cutoff]
            if old:
                self.store.delete_keys(old)
                LOG.info("pruned %s prediction snapshots older than %s days", len(old), PRED_KEEP_DAYS)
        if self.cn.close:
            self.note_outcome("SSE", self.cn.close.day.isoformat(), self.cn.close.value, self.cn.close.source)
        k = self.kospi.quote
        kst = dt.timezone(dt.timedelta(hours=9))
        quoted_kst = dt.datetime.fromtimestamp(k.quoted_ms / 1000, kst) if k else None
        if quoted_kst and quoted_kst.time() >= CALENDAR.close_time("kr", quoted_kst.date()):
            day = quoted_kst.date()
            official = self.kospi.official_close(day)
            rank = self.kospi.daily_rank.get(day)
            self.note_outcome("KOSPI", day.isoformat(), official or k.last,
                              ("Yahoo ^KS11 日K", "Naver 日K")[rank] if official and rank in (0, 1) else f"{k.source} 实时（日K未出）")
        q, local = self.hsi.quote, dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
        hk_final = dt.datetime.combine(local.date(), CALENDAR.close_time("hk", local.date()), BEIJING) + dt.timedelta(minutes=5)
        if local >= hk_final and hk_cash_close_date(now_ms, self.hsi.holidays) == local.date():
            # the dated daily close once it is out; before that the cash quote, if it is that day's
            close, source = self.hsi_daily.daily.get(local.date()), ""
            if close is not None:
                rank = self.hsi_daily.ranks.get(local.date())
                source = f"{self.hsi_daily.sources[rank][0]} 日K" if rank is not None else "日K"
            elif q is not None and q.spot is not None and not self.hsi.spot_problem(q, now_ms):
                close, source = q.spot, f"{q.spot_source or q.source} 实时（日K未出）"
            if close is not None:
                self.note_outcome("HSI", local.date().isoformat(), close, source)

    PRED_FIELDS = frozenset({"key", "t", "target", "mode", "ref", "up", "move", "R", "beta", "sigma"})

    def calibration_text(self) -> str:
        saved = [v for _, v in self.store.items("pred:") if isinstance(v, dict)]
        preds = [v for v in saved if self.PRED_FIELDS <= v.keys()]  # snapshots from before 1.14 lack the fit's inputs
        outcomes = {k.removeprefix("outcome:"): float(v) for k, v in self.store.items("outcome:")}
        skipped = f"（忽略 {len(saved) - len(preds)} 条旧格式快照）" if len(saved) > len(preds) else ""
        return "\n".join([f"📐 概率模型回测（只评估，不会自动改参数）{skipped}"] + calibration_report(preds, outcomes) + self.mark_report())

    async def cmd_calib(self, req: Request) -> str:
        return self.calibration_text()

    async def kospi_anchor(self, kospi: IndexQuote, hl: HlQuote) -> None:
        """HL KR200 at the KOSPI 15:30 close, from the same perp as the live proxy, persisted across restarts.

        Order: saved record → 1-minute candle → 5-minute candle (same instant, survives longer on HL)
        → the live mark recorded within two minutes after the close.
        """
        close_ms = self.kospi_close_ms(kospi)
        if 0 <= hl.fetched_ms - close_ms <= 2 * 60_000 and not self.store.get(f"kr200_print:{close_ms}"):
            self.store.put(f"kr200_print:{close_ms}", [hl.fetched_ms, str(kr200_price(hl)[0])])  # too late to catch afterwards
        if self.anchors.get("KOSPI", (0,))[0] == close_ms:
            return
        if hl.fetched_ms < close_ms:
            return  # today's session is still running: its 15:30 has not happened yet, nothing to look up
        saved = self.store.get("anchor:KOSPI", ())
        with contextlib.suppress(ValueError, TypeError, IndexError, decimal.InvalidOperation):
            if int(saved[0]) == close_ms and D(str(saved[1])) > 0:
                self.anchors["KOSPI"] = (close_ms, D(str(saved[1])))
                self.kospi_anchor_note = str(saved[2]) if len(saved) > 2 else "15:30"
                return
        if not self.retry_ok("KOSPI"):
            return
        price, note, failures = None, "", []
        for interval, label in (("1m", "15:30 一分钟K"), ("5m", "15:30 五分钟K")):
            try:
                price, note = await self.hl.price_at(hl.coin, close_ms, interval), label
                break
            except Exception as error:
                failures.append(clean_error(error))
        printed = self.store.get(f"kr200_print:{close_ms}")
        if price is None and printed:
            with contextlib.suppress(ValueError, TypeError, IndexError, decimal.InvalidOperation):
                price, note = D(str(printed[1])), "15:30 后两分钟内实时价近似"
        self.kospi_anchor_error = "；".join(failures) if price is None else ""
        if price is not None:
            self.anchors["KOSPI"] = (close_ms, price)
            self.kospi_anchor_note = note
            self.store.put("anchor:KOSPI", [close_ms, str(price), note])

    def retry_ok(self, key: str, every: float = 60) -> bool:
        """Throttle failing anchor lookups to one attempt per minute per key."""
        now = time.monotonic()
        if now - self.anchor_tries.get(key, -1e9) < every:
            return False
        self.anchor_tries[key] = now
        return True

    def sse_close(self, now_ms: int) -> DailyClose | None:
        """The Composite's latest close: the dated daily bar, or, while today's bar is still pending
        after 15:00, the realtime quote printed at/after the close (replaced once the bar arrives)."""
        q = self.cn.quote
        if q is None or not self.cn.close_pending(now_ms):
            return self.cn.close
        today = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING).date()
        at_close = int(dt.datetime.combine(today, dt.time(15, 0), BEIJING).timestamp() * 1000)
        if q.quoted_ms < at_close or dt.datetime.fromtimestamp(q.quoted_ms / 1000, BEIJING).date() != today:
            return self.cn.close  # no print from the close yet
        return DailyClose(today, q.last, q.prev_close, f"{q.source}实时收盘（日K待确认）", q.quoted_ms)

    def sse_close_ms(self, now_ms: int | None = None) -> int:
        """15:00 on the Composite's latest close (see sse_close); 0 = none yet."""
        close = self.sse_close(self.market.now_ms() if now_ms is None else now_ms)
        return close.close_ms if close else 0

    def proxy_beta(self, key: str, prior: float, now_ms: int) -> tuple[float, str]:
        """The after-hours proxy coefficient for ``key`` ("SSE": A50 → Composite) and how it was obtained.

        The slope b of ln(close / reference close) on the proxy's log move since that close, fitted (fit_proxy: every
        target day weighted once) on the bot's own after-hours snapshots of the last BETA_WINDOW_DAYS days whose close
        is known, blended with the configured prior as BETA_PRIOR_DAYS pseudo-days, β = (n·b + N0·prior) / (n + N0),
        and kept within BETA_BOUNDS. Below BETA_MIN_DAYS closed target days the prior stands. Redone hourly."""
        cached = self.beta_cache.get(key)
        if cached and time.monotonic() - cached[0] < BETA_REFRESH_SECONDS:
            return cached[1], cached[2]
        beta, note, days = prior, "", 0
        try:
            outcomes = {k.rsplit(":", 1)[-1]: float(v) for k, v in self.store.items(f"outcome:{key}:") if isinstance(v, (int, float))}
            since = now_ms - (BETA_WINDOW_DAYS + 7) * DAY_MS
            rows = []
            for k, v in self.store.items(f"pred:{key}:"):
                saved = k.rsplit(":", 1)[-1]
                if (not saved.isdigit() or int(saved) < since or not isinstance(v, dict) or v.get("mode") != "盘后"
                        or v.get("warn")):
                    continue
                close, ref, move, remaining = outcomes.get(str(v.get("target"))), v.get("ref_raw", v.get("ref")), v.get("move"), v.get("R")
                if close is None or not ref or close <= 0 or move is None or not remaining or remaining <= 0:
                    continue
                rows.append({"target": str(v["target"]), "move": float(move), "y": math.log(close / float(ref)), "R": float(remaining)})
            days = len({r["target"] for r in rows})
            fit = fit_proxy(rows) if days >= BETA_MIN_DAYS else None
            if fit:
                slope = fit[1]
                blended = (days * slope + BETA_PRIOR_DAYS * prior) / (days + BETA_PRIOR_DAYS)
                beta = min(BETA_BOUNDS[1], max(BETA_BOUNDS[0], blended))
                note = (f"β {beta:.2f}（近 {days} 日盘后回归 {slope:.2f}，与先验 {prior:g} 按 {BETA_PRIOR_DAYS} 日权重合成"
                        + ("，已限幅" if beta != blended else "") + "）")
        except Exception as error:  # a damaged snapshot must not take the odds down: the prior stands
            self.log_limited("beta", f"{key} β 回归失败：{clean_error(error) or type(error).__name__}")
        if not note:
            note = f"β {prior:g}（暂定，未校准）·盘后样本 {days} 日，满 {BETA_MIN_DAYS} 日起自动回归"
        self.beta_cache[key] = (time.monotonic(), beta, note)
        return beta, note

    def sse_odds(self, now_ms: int) -> CloseOdds | str | None:
        q = self.cn.quote
        if not self.config.probability or not self.config.sse_index:
            return None
        holidays = self.config.holidays.get("sh", frozenset())
        sigma, sigma_note = self.vols.get("SSE", "SSE")
        close = self.cn.close
        if self.cn.sse_session(now_ms)[0]:
            # the session runs: only today's own, fresh print prices it (never a frozen or yesterday's quote, and
            # never a silent switch to the A50 proxy)
            if q is None:
                return "缺少上证实时报价，暂不输出概率"
            if self.cn.live_stale(now_ms):
                return f"上证实时报价已超 10 分钟未更新（最后 {quote_time(q.quoted_ms)}），暂不输出新概率"
            if close and close.day == self.cn.expected_close(now_ms):
                ref, ref_note = close.value, f"{close.day.strftime('%m-%d')} 收盘"
            elif q.prev_close:
                ref, ref_note = q.prev_close, "昨收（实时行情，日 K 待确认）"
            else:
                return "缺少上证昨收"
            today = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING).date()
            remaining, target = session_remaining("sh", now_ms, today - dt.timedelta(days=1), holidays)
            sigma, sigma_note = self.vols.get("SSE", "SSE", intraday=True)
            return close_odds("上证指数", ref, q.last, sigma, remaining, target, D("0.01"), ref_note,
                              f"上证现货 {fmt(q.last)}（盘中直接用现货）", sigma_note)
        # After hours the reference is the close of a dated daily bar; only today's own closing print
        # stands in for it while that bar is pending, never an older realtime "last price".
        close = self.sse_close(now_ms)
        if close is self.cn.close and (self.cn.close_pending(now_ms) or not self.cn.confirmed(now_ms)):
            detail = f"，日 K 最新为 {close.day.strftime('%m-%d')}" if close else ""
            expected = (dt.datetime.fromtimestamp(now_ms / 1000, BEIJING).date() if self.cn.close_pending(now_ms)
                        else self.cn.expected_close(now_ms))
            return f"收盘价待确认（等待 {expected.strftime('%m-%d')} 上证日 K{detail}）"
        close_ms, close_date = close.close_ms, close.day
        remaining, target = session_remaining("sh", now_ms, close_date, holidays)
        a50, anchor = self.cn.a50, self.anchors.get("A50")
        ref_note = f"{close_date.strftime('%m-%d')} 收盘"
        reopen = a50_next_open(now_ms)
        if reopen:
            # No A50 trading, so nothing new to map: pause rather than keep showing the last session's odds.
            return f"A50 {a50_closed_note(now_ms)}，{stamp(reopen, seconds=False)} 开盘后恢复概率"
        if a50 is None:
            return "暂无 A50 报价，暂不输出概率"
        warn = ""
        if self.cn.a50_stale(now_ms):
            # A short gap (a feed outage, the 16:30-16:45 break running long) prices from the last print, flagged;
            # after an hour without a print the odds would be guesswork: pause them.
            if a50_session(now_ms) == "休市" or now_ms - a50.quoted_ms > self.A50_SOFT_STALE_MS:
                return f"A50 报价已超 10 分钟未更新（最后 {stamp(a50.quoted_ms, seconds=False)}），暂不输出新概率"
            warn = (f"A50 {(now_ms - a50.quoted_ms) // 60_000} 分钟未更新，按 "
                    f"{dt.datetime.fromtimestamp(a50.quoted_ms / 1000, BEIJING):%H:%M} 报价估算")
        if a50.quoted_ms < close_ms:
            return f"A50 报价早于 {close_date.strftime('%m-%d')} 15:00 收盘，等待新报价"
        rolled = int(dt.datetime.combine(close_date, dt.time(16, 35), BEIJING).timestamp() * 1000)
        if a50_expiry(close_date) and a50.quoted_ms >= rolled:
            # the 15:00 anchor is the expiring contract, the night quote already the next month: their spread
            # would be read as a move. No same-contract anchor exists, so no estimate until the cash market reopens.
            return (f"A50 {close_date.month} 月合约 {close_date.strftime('%m-%d')} 到期换月：15:00 锚点是旧合约、"
                    "夜盘报价已是新合约，价差会被当成涨跌；暂不输出概率，上证开盘后恢复")
        if anchor and anchor[0] == close_ms:
            if a50_family(self.a50_anchor_source) != a50_family(a50.source):
                if self.a50_anchor_source == "未知":
                    return "旧版 A50 锚点未记录合约来源，且历史 K 线暂不可核实；暂停概率以免混算"
                why = f"（东方财富未取到：{brief_error(self.cn.a50_skipped, 60)}）" if self.cn.a50_skipped else ""
                return (f"A50 锚点来自{a50_family(self.a50_anchor_source)}期货，当前只有{a50.source}报价{why}；"
                        "不同合约不能混算，暂不输出概率")
            base, base_note = anchor[1], self.a50_anchor_note
        else:
            which = "新浪 A50 5 分钟 K" if a50_family(a50.source) == "新浪CFD" else "东方财富 1 分钟及 5 分钟 K"
            why = f"：{brief_error(self.a50_anchor_error, 90)}" if self.a50_anchor_error else ""
            return (f"缺少 {close_date.strftime('%m-%d')} 15:00 的 A50 锚点（{which}均未取得{why}）；"
                    "下一个上证收盘 15:00 后会自动记录，暂不输出概率")
        if self.config.a50_beta_dynamic:
            beta, beta_note = self.proxy_beta("SSE", self.config.a50_beta, now_ms)
        else:
            beta, beta_note = self.config.a50_beta, f"β {self.config.a50_beta:g}（A50_BETA 固定）"
        move = math.log(float(a50.last / base))
        effective = close.value * D(str(math.exp(beta * move)))
        odds = close_odds("上证指数", close.value, effective, sigma, remaining, target, D("0.01"),
                          f"{ref_note}·{close.source}",
                          f"A50 {fmt(a50.last)} / {base_note} {fmt(base)} → {percent(a50.last, base):+.3f}% × {beta_note}",
                          sigma_note, beta=beta, mode="盘后")
        return dataclasses.replace(odds, warn=warn) if warn else odds

    @staticmethod
    def kospi_close_ms(q: IndexQuote) -> int:
        kst = dt.timezone(dt.timedelta(hours=9))
        day = dt.datetime.fromtimestamp(q.quoted_ms / 1000, kst).date()
        return int(dt.datetime.combine(day, CALENDAR.close_time("kr", day), kst).timestamp() * 1000)

    @staticmethod
    def hk_cash_close_date(now_ms: int, holidays: frozenset = frozenset()) -> dt.date:
        return hk_cash_close_date(now_ms, holidays)

    def note_live_close(self, symbol: str, ticker: StockTicker, now_ms: int) -> None:
        """Remember the stock's own print once today's close has passed, before the daily bar confirms it,
        so the probability can move on to the next close right away (persisted across restarts)."""
        info = STOCK_MARKETS[ticker.market]
        tz = dt.timezone(dt.timedelta(hours=info.utc_offset))
        today = dt.datetime.fromtimestamp(now_ms / 1000, tz).date()
        close_ms = int(dt.datetime.combine(today, CALENDAR.close_time(ticker.market, today), tz).timestamp() * 1000)
        if now_ms < close_ms + CLOSE_SETTLE_MS.get(ticker.market, 60_000):
            return
        live, _ = self.stocks.live_quote(symbol, now_ms)  # None once the close is final (+15 min): keep the last one
        if live is None or live.quoted_ms < close_ms - 5 * 60_000:
            return  # no print from the closing auction yet
        if ticker.market == "kr" and live.quoted_ms >= close_ms + 10 * 60_000:
            return  # 15:40 KST on: Nextrade after-hours prints, not the KRX close
        if ticker.market == "kr" and live.quoted_ms >= close_ms and not self.store.get(f"krx_close:{ticker.code}:{today.isoformat()}"):
            # the KRX close, seen before the after-hours session: the daily close refresh (every 10 min) may miss this window
            self.store.put(f"krx_close:{ticker.code}:{today.isoformat()}", [str(live.last), str(live.prev_close or "")])
        record = [close_ms, str(live.last), live.source]
        if self.store.get(f"live_close:{symbol}") != record:
            self.store.put(f"live_close:{symbol}", record)

    def odds_base(self, symbol: str, now_ms: int) -> tuple[int, D | None, str]:
        """(close ms, close value, note) the next-close probability is measured from: the confirmed
        exchange close, or today's live closing print while the daily bar has not confirmed it yet."""
        ref = self.reference_for("exchange", symbol, now_ms)
        best = (ref.close_ms, ref.value, f"{close_when(ref)}·{short_source(ref.source)}") if ref and ref.close_ms else (0, None, "")
        saved = self.store.get(f"live_close:{symbol}")
        with contextlib.suppress(ValueError, TypeError, IndexError, decimal.InvalidOperation):
            close_ms, value = int(saved[0]), D(str(saved[1]))
            # A manual close in another unit (≥20% apart) cannot be compared with the stock's own print.
            if close_ms > best[0] and value > 0 and close_ms <= now_ms and (best[1] is None or abs(percent(best[1], value)) < 20):
                best = (close_ms, value, f"{stamp(close_ms, seconds=False)}·{saved[2]}现货收盘（日K待确认）")
        return best

    def stock_sigma(self, symbol: str, market: str, intraday: bool = False) -> tuple[float, str]:
        """The stock's daily σ (its exchange sessions), saying why when the daily bars could not be read."""
        sigma, note = self.vols.get(symbol, market, intraday)
        if symbol not in self.vols.estimates and self.vol_errors.get(symbol):
            note += f"（交易所日K未取得：{brief_error(self.vol_errors[symbol], 50)}）"
        return sigma, note

    def contract_odds(self, symbol: str, price: D, now_ms: int, quote_ms: int = 0) -> CloseOdds | str | None:
        """quote_ms: when the Binance price was quoted (0 = just now); an old one cannot map the move."""
        ticker = self.config.tickers.get(symbol)
        if not self.config.probability or not ticker:
            return None
        ref = self.reference_for("exchange", symbol, now_ms)
        base_ms, base_value, base_label = self.odds_base(symbol, now_ms)
        if not base_ms:
            return "缺少带收盘时刻的交易所收盘价"
        anchor = self.anchors.get(symbol)
        info = STOCK_MARKETS[ticker.market]
        holidays = self.config.holidays.get(ticker.market, frozenset())
        tz = dt.timezone(dt.timedelta(hours=info.utc_offset))
        close_date = dt.datetime.fromtimestamp(base_ms / 1000, tz).date()
        sigma, sigma_note = self.stock_sigma(symbol, ticker.market)
        unit = "" if ticker.same_unit else ((ref.currency if ref else "") or info.currency)
        live, live_why = self.stocks.live_quote(symbol, now_ms)
        closed_today = close_date == dt.datetime.fromtimestamp(now_ms / 1000, tz).date() and base_ms <= now_ms
        phase = preopen_phase(ticker.market, now_ms, holidays)
        if live is not None and phase == "可撤单":
            # orders can still be withdrawn: the indicative price is often a probe. Shown on the card, not priced on.
            live, live_why = None, f"开市前竞价可撤单阶段，参考价 {fmt_price(live.last)} 常是试探、仅供参考"
        if live is not None and not closed_today:
            # The stock itself is trading: use its own price, not the Binance proxy.
            same_unit = not live.prev_close or abs(percent(base_value, live.prev_close)) < 20  # a manual close in another unit
            if close_date == expected_close_date(ticker.market, now_ms, holidays) and same_unit:
                base, base_note = base_value, base_label
            elif live.prev_close:
                base, base_note = live.prev_close, "昨收（实时行情）"
            else:
                return "缺少昨收"
            remaining, target = session_remaining(ticker.market, now_ms, None, holidays)
            sigma, sigma_note = self.stock_sigma(symbol, ticker.market, intraday=True)  # today's opening gap has happened
            if phase == "已撮合":
                # the matched opening price: firm until continuous trading, the whole session ahead of it
                first = CALENDAR.sessions(ticker.market, dt.datetime.fromtimestamp(now_ms / 1000, tz).date())[0][0]
                note = (f"{info.name}开盘价已撮合 {fmt_price(live.last)}（{live.source}·{hhmm(live.quoted_ms)} 更新；"
                        f"{first:%H:%M} 起连续交易）")
            elif phase:
                # orders cannot be withdrawn any more, but may still be added until the match: the gap the indicative
                # price shows can move, worth some minutes of trading on top of the whole session ahead
                remaining += PREOPEN_VARIANCE_MINUTES.get(ticker.market, 0.0) / session_minutes(ticker.market)
                note = (f"{info.name}开市前竞价参考价 {fmt_price(live.last)}（{live.source}·{hhmm(live.quoted_ms)} 更新；"
                        f"{phase}阶段，撮合前仍会变）")
            else:
                note = f"{info.name}现货 {fmt_price(live.last)}（{live.source}·盘中直接用现货）"
            return close_odds(NAMES.get(symbol, symbol), base, live.last, sigma, remaining, target,
                              price_tick(ticker.market, base), base_note, note, sigma_note, unit)
        if anchor is None or anchor[0] != base_ms:
            return "等待币安在收盘时刻的价格"
        if quote_ms and now_ms - quote_ms > self.config.max_age * 1000:
            return f"币安行情已 {(now_ms - quote_ms) // 1000} 秒未更新，暂不输出新概率"
        remaining, target = session_remaining(ticker.market, now_ms, close_date, holidays)
        move = percent(price, anchor[1])
        return close_odds(NAMES.get(symbol, symbol), base_value, base_value * price / anchor[1], sigma, remaining, target,
                          price_tick(ticker.market, base_value), base_label,
                          f"币安 {fmt_price(price)} / 收盘时刻 {fmt_price(anchor[1])} → {move:+.3f}%"
                          + (f"｜{live_why}，暂用币安" if live_why else ""), sigma_note, unit)

    HSI_PRINT_MS = 5 * 60_000  # the first futures print this soon after the 16:10 cash close is its anchor

    def note_hsi_close_print(self, q: FuturesQuote) -> None:
        """The futures price at the 16:10 cash close: the first quote read at or after it (within five minutes, the day
        session's, current when read), per contract family and month, persisted. It maps every later futures move
        (16:10–16:30, the night session, the next morning) onto the close; the feeds keep no history to recover it."""
        holidays = self.hsi.holidays
        read_ms = q.fetched_ms or q.quoted_ms
        if not read_ms or not q.quoted_ms:
            return
        day = dt.datetime.fromtimestamp(read_ms / 1000, BEIJING).date()
        close_ms = int(dt.datetime.combine(day, CALENDAR.close_time("hk", day), BEIJING).timestamp() * 1000)
        if (not hk_trading_day(day, holidays) or not close_ms <= read_ms <= close_ms + self.HSI_PRINT_MS
                or q.session_name(holidays) != "日市" or self.hsi.futures_problem(q, read_ms)):
            return
        key = f"anchor:{hsi_anchor_key(q)}"
        saved = self.store.get(key)
        if isinstance(saved, list) and saved and saved[0] == close_ms:
            return  # only the first print counts
        late = (read_ms - close_ms) // 60_000
        note = "16:10 现货收市时" if late < 1 else f"16:10 后 {late} 分钟首笔近似"
        self.store.put(key, [close_ms, str(q.last), note, q.contract, read_ms])

    async def recover_hsi_cfd_anchor(self, now_ms: int) -> None:
        """When the CFD has no anchor for the latest cash close (the bot was not running at 16:10) and it is needed (the
        quote in use is the CFD, or the HKEX contract has no exact print either), take the Sina CFD's price then from its
        5-minute bars: an approximate anchor for the CFD family, so the after-hours odds need not pause. Retried every
        5 minutes while it fails."""
        if not (self.config.hsi_futures and self.config.probability) or self.hsi.cash_open(now_ms):
            return
        day = hk_cash_close_date(now_ms, self.hsi.holidays)
        close_ms = int(dt.datetime.combine(day, CALENDAR.close_time("hk", day), BEIJING).timestamp() * 1000)
        if now_ms < close_ms + self.HSI_PRINT_MS:
            return  # the live prints come first
        def has(family: str, exact: bool) -> bool:
            saved = self.store.get(f"anchor:{family}")
            return (isinstance(saved, list) and len(saved) >= 3 and saved[0] == close_ms
                    and (not exact or "近似" not in str(saved[2])))
        cfd_in_use = self.hsi.quote is not None and not self.hsi.quote.exchange_contract
        if has("HSI:cfd", False) or (has("HSI", True) and not cfd_in_use):
            return  # the CFD has its anchor, or the HKEX contract in use has its own exact price at the close
        if not self.retry_ok("HSI-cfd", every=300):
            return
        try:
            price = await self.hsi.cfd_bar_at(close_ms)
        except Exception as error:
            self.hsi_cfd_error = clean_error(error) or type(error).__name__
            return
        self.hsi_cfd_error = ""
        label = dt.datetime.fromtimestamp(close_ms / 1000, BEIJING).strftime("%H:%M")
        self.store.put("anchor:HSI:cfd", [close_ms, str(price), f"{label} 五分钟K近似", "", now_ms])

    @staticmethod
    def anchor_quality(note: str, approximate: bool) -> int:
        """How close an anchor is to the price at the close: 0 itself, 1 within minutes (a late first print, a 5-minute
        bar), 2 the 16:30 day close (twenty minutes of futures moves missing)."""
        return 0 if not approximate else 2 if "16:30" in note else 1

    def hsi_proxy(self, now_ms: int, close_date: dt.date,
                  choices: list[FuturesQuote]) -> tuple[FuturesQuote | None, D | None, str, bool]:
        """(the quote, its anchor, the anchor's label, approximate?) the after-hours odds map from: among the current quotes
        of each family (the one in use first), the one whose own 16:10 price is closest to the close itself. The families
        never mix: a CFD print is only ever compared with a CFD anchor. Without any, the reason, naming what is missing."""
        best = None
        for rank, c in enumerate(choices):
            anchor, note, approximate = self.hsi_anchor(c, close_date, now_ms)
            if anchor is not None and (best is None or (self.anchor_quality(note, approximate), rank) < best[0]):
                best = ((self.anchor_quality(note, approximate), rank), c, anchor, note, approximate)
        if best is not None:
            return best[1], best[2], best[3], best[4]
        q = choices[0]
        why = self.hsi_anchor(q, close_date, now_ms)[1]
        if not q.exchange_contract:
            hkex = self.hsi.family_errors.get("HSI") or self.hsi.skipped
            why = (f"当前只有新浪CFD报价（港交所合约：{brief_error(hkex, 60) if hkex else '暂无当前报价'}）：{why}"
                   + (f"；新浪5分钟K也未取到（{brief_error(self.hsi_cfd_error, 60)}）" if self.hsi_cfd_error else ""))
        return None, None, why, False

    def hsi_choices(self, now_ms: int) -> list[FuturesQuote]:
        """The futures quotes the after-hours odds may use: the one in use, then each other family's latest quote that
        still stands for the market now."""
        q = self.hsi.quote
        out = [q] if q is not None else []
        out += [f for f in self.hsi.families.values()
                if (q is None or hsi_anchor_key(f) != hsi_anchor_key(q)) and not self.hsi.futures_problem(f, now_ms)]
        return out

    def hsi_anchor(self, q: FuturesQuote, close_date: dt.date, now_ms: int) -> tuple[D | None, str, bool]:
        """(the futures price at close_date's 16:10 cash close, its label, approximate?) for mapping q onto that close.
        Prefers the recorded print of the same family and contract month; otherwise the same contract's 16:30 day
        close, clearly approximate (the 16:10–16:30 futures move is missing); else none, with the reason."""
        close_ms = int(dt.datetime.combine(close_date, CALENDAR.close_time("hk", close_date), BEIJING).timestamp() * 1000)
        saved = self.store.get(f"anchor:{hsi_anchor_key(q)}")
        with contextlib.suppress(ValueError, TypeError, IndexError, decimal.InvalidOperation):
            if int(saved[0]) == close_ms and (not saved[3] or not q.contract or saved[3] == q.contract) and D(str(saved[1])) > 0:
                return D(str(saved[1])), str(saved[2]), "近似" in str(saved[2])
        holidays = self.hsi.holidays
        session, quoted = q.session_name(holidays), dt.datetime.fromtimestamp(q.quoted_ms / 1000, BEIJING)
        approx = "日市 16:30 收市（近似：缺 16:10 现货收市时的期货价，未含 16:10–16:30 期货变动）"
        if q.source == "etnet" and q.prev_settle and (session == "夜市" or quoted.date() > close_date):
            return q.prev_settle, approx, True  # etnet's 前收市 there is that contract's 16:30 day close
        if session == "日市" and quoted.date() == close_date and hk_futures_session(now_ms, holidays) != "日市":
            return q.last, approx, True  # the day session is over: its last price is the 16:30 close
        why = f"缺少恒指期货在 {close_date:%m-%d} 16:10 现货收市时的价格（只能在收市时记录）"
        return None, why + ("；16:30 日市收市后按收市价近似" if session == "日市" and quoted.date() == close_date else ""), False

    def hsi_odds(self, now_ms: int) -> CloseOdds | str | None:
        q = self.hsi.quote
        if not self.config.probability or not self.config.hsi_futures:
            return None
        if q is None:
            return "缺少恒指期货"
        holidays = self.hsi.holidays
        local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
        sigma, sigma_note = self.vols.get("HSI", "HSI")
        if self.hsi.cash_open(now_ms):
            # the cash session runs: only today's own, fresh index prices it (never yesterday's page or a frozen feed)
            why = self.hsi.spot_problem(q, now_ms)
            if why:
                return f"{why}，暂不输出新概率"
            if not q.spot_prev:
                return "缺少恒指昨收"
            remaining, target = session_remaining("hk", now_ms, local.date() - dt.timedelta(days=1), holidays)
            sigma, sigma_note = self.vols.get("HSI", "HSI", intraday=True)
            prev_day = expected_close_date("hk", now_ms, holidays)
            ref, ref_note = dated_ref(self.hsi_daily.daily, prev_day, q.spot_prev)
            return close_odds("恒生指数", ref, q.spot, sigma, remaining, target, D("0.01"), ref_note,
                              f"恒指现货 {fmt(q.spot)}（盘中直接用现货）", sigma_note)
        close_date = hk_cash_close_date(now_ms, holidays)
        self.hsi_used = None
        choices = [c for c in self.hsi_choices(now_ms) if not self.hsi.futures_problem(c, now_ms)]
        if not choices:
            return f"恒指期货{self.hsi.futures_problem(q, now_ms)}，暂不输出新概率"
        if close_date not in self.hsi_daily.daily:  # the close comes from the cash quote itself: it must be that day's
            why = self.hsi.spot_problem(q, now_ms)
            if why:
                return f"{why}，暂不输出概率"
        used, anchor, anchor_note, approximate = self.hsi_proxy(now_ms, close_date, choices)
        if used is None or anchor is None:
            return anchor_note
        self.hsi_used = used
        remaining, target = session_remaining("hk", now_ms, close_date, holidays)
        ref, ref_note = dated_ref(self.hsi_daily.daily, close_date, q.spot)
        label = "恒指期货" if used.exchange_contract else "恒指期货·新浪CFD"
        odds = close_odds("恒生指数", ref, ref * used.last / anchor, sigma, remaining, target, D("0.01"), ref_note,
                          f"{label} {fmt(used.last)} / {anchor_note} {fmt(anchor)} → {percent(used.last, anchor):+.3f}%",
                          sigma_note, mode="盘后")
        return dataclasses.replace(odds, warn="期货锚点是近似值，暂不给建议") if approximate else odds

    A50_SOFT_STALE_MS = 60 * 60_000  # A50 silent longer than this: no odds at all (shorter: odds with a warning)
    HL_STALE_MS = 10 * 60_000  # an HL mark older than this (refresh failing) is not used for new probabilities

    def kospi_ref(self, day: dt.date, live: D | None) -> tuple[D, str]:
        """The close a KOSPI up/down market compares with: the dated daily-chart close of ``day``, else the
        realtime feed's figure (Naver's index feed has been seen frozen at 15:15, before the closing auction)."""
        return dated_ref(self.kospi.daily, day, live)

    def kospi_odds(self, now_ms: int) -> CloseOdds | str | None:
        k = self.kospi.quote
        if not self.config.probability or not self.config.kospi_index:
            return None
        if k is None:
            return "缺少 KOSPI"
        sigma, sigma_note = self.vols.get("KOSPI", "KOSPI")
        kst = dt.timezone(dt.timedelta(hours=9))
        local = dt.datetime.fromtimestamp(now_ms / 1000, kst)
        holidays = self.config.holidays.get("kr", frozenset())
        if self.kospi.session(now_ms)[0]:
            # the session runs: only today's own, fresh print prices it (a feed frozen at 09:30 must not drive the
            # odds towards 0 or 100 as the remaining time shrinks)
            why = self.kospi.problem(k, now_ms)
            if why:
                return f"KOSPI 实时{why}，暂不输出新概率"
            if not k.prev_close:
                return "缺少 KOSPI 昨收"
            remaining, target = session_remaining("kr", now_ms, local.date() - dt.timedelta(days=1), holidays)
            sigma, sigma_note = self.vols.get("KOSPI", "KOSPI", intraday=True)
            prev_day = expected_close_date("kr", now_ms, holidays)
            ref, ref_note = self.kospi_ref(prev_day, k.prev_close)
            return close_odds("KOSPI", ref, k.last, sigma, remaining, target, D("0.01"), ref_note,
                              f"KOSPI 现货 {fmt(k.last)}（盘中直接用现货）", sigma_note)
        if not k.quoted_ms:
            return "KOSPI 报价时间未知，无法确认是哪一天的收盘；暂不输出概率"
        quoted_day = dt.datetime.fromtimestamp(k.quoted_ms / 1000, kst).date()
        hl, anchor = self.hl.quotes.get("KR200"), self.anchors.get("KOSPI")
        remaining, target = session_remaining("kr", now_ms, quoted_day, holidays)
        expected = expected_close_date("kr", now_ms, holidays)
        if quoted_day < expected or (quoted_day == local.date() and local.time() < CALENDAR.kr_time(KRX_SETTLED, local.date())):
            return f"KOSPI 基准停在 {stamp(k.quoted_ms, seconds=False)}，应为 {expected.strftime('%m-%d')} 收盘；暂不输出概率"
        if hl is None:
            return "缺少 HL KR200 代理"
        if now_ms - hl.fetched_ms > self.HL_STALE_MS:
            return f"HL KR200 报价已超 10 分钟未更新（最后 {stamp(hl.fetched_ms, seconds=False)}），暂不输出新概率"
        if not (anchor and anchor[0] == self.kospi_close_ms(k)):
            # Never divide the perp by the KOSPI200 cash level: their basis would be read as a move.
            why = f"：{brief_error(self.kospi_anchor_error, 90)}" if self.kospi_anchor_error else ""
            return f"缺少 HL KR200 在 {quoted_day.strftime('%m-%d')} 15:30 的同源锚点{why}；暂不输出概率"
        base, base_note = anchor[1], "收盘时刻" if self.kospi_anchor_note == "15:30 一分钟K" else self.kospi_anchor_note
        price, kind = kr200_price(hl)
        beta = self.config.kospi_beta
        ref, ref_note = self.kospi_ref(quoted_day, k.last)
        effective = ref * D(str(math.exp(beta * math.log(float(price / base)))))
        return close_odds("KOSPI", ref, effective, sigma, remaining, target, D("0.01"), ref_note,
                          f"HL KR200 {kind} {fmt(price)} / {base_note} {fmt(base)} → {percent(price, base):+.3f}%"
                          + (f" × β {beta:g}" if beta != 1 else "") + "（KOSPI200 代理）", sigma_note, beta=beta, mode="盘后")

    @staticmethod
    def odds_row(odds: CloseOdds | str | None, label: str = "") -> str:
        if odds is None:
            return ""
        prefix = f"🎲 {label} " if label else "🎲 "
        if isinstance(odds, str):
            return f"{prefix}概率暂缺：{odds}"
        return odds.row().replace("🎲 ", prefix, 1)

    def odds_items(self, now_ms: int) -> list[tuple[str, CloseOdds | str]]:
        items = [("恒生指数", self.hsi_odds(now_ms)), ("KOSPI", self.kospi_odds(now_ms)), ("上证指数", self.sse_odds(now_ms))]
        for symbol in self.config.symbols:
            snapshot = self.snapshots.get(symbol) or {}
            quote = snapshot.get("quote")
            items.append((f"{NAMES.get(symbol, symbol)}｜{symbol}",
                          self.contract_odds(symbol, quote.price, now_ms, quote.timestamp_ms) if quote else "等待行情"))
        return [(title, self.settle_on_strike(title, odds)) for title, odds in items if odds is not None]

    def settle_on_strike(self, title: str, odds: CloseOdds | str) -> CloseOdds | str:
        """Measure the odds against the Predict market's own target price (the close it settles against,
        e.g. Yahoo ^KS11 / KRX official) when it is known and our reference close differs from it."""
        if not isinstance(odds, CloseOdds) or not self.config.predict:
            return odds
        key = self.predict_key(title)
        stem = self.config.predict_slugs.get(key)
        slug = predict_slug(stem, odds.target) if stem else ""
        strike = (self.predict.strikes.get(slug) or (None,))[0] if slug else None
        if strike is None or strike == odds.ref:
            return odds
        if abs(percent(strike, odds.ref)) >= 5:  # another unit or another underlying: do not mix them
            return dataclasses.replace(odds, ref_note=odds.ref_note + f"｜Predict 目标价 {fmt(strike)} 与参考相差过大，未采用")
        ticker = self.config.tickers.get(title.split("｜")[-1])
        tick = price_tick(ticker.market, strike) if ticker and "｜" in title else D("0.01")
        # A proxy-mapped estimate is "our close × proxy move": move it onto the settlement close too.
        effective = odds.effective if odds.direct else odds.effective * strike / odds.ref
        return dataclasses.replace(
            close_odds(odds.name, strike, effective, odds.sigma_daily, odds.remaining, odds.target, tick,
                       f"Predict 目标价（本地参考 {fmt(odds.ref)}·{odds.ref_note}）", odds.proxy_note, odds.sigma_note,
                       odds.unit, odds.beta, odds.mode), warn=odds.warn)

    def odds_payload(self) -> dict:
        """JSON for the web page: one entry per item, numbers raw, text already plain."""
        now_ms = self.market.now_ms()
        items = []
        for title, odds in (self.odds_items(now_ms) if self.config.probability else []):
            name, _, symbol = title.partition("｜")
            base = {"name": name, "symbol": symbol, "group": "contract" if symbol else "index"}
            book = self.predict_payload(title, odds, now_ms)
            if book:
                base["predict"] = book
            market = self.item_market(title)
            if market:
                holidays = self.config.holidays.get(market, frozenset())
                if auction_running(market, now_ms, holidays):
                    base["auction"] = (auction_window(market, now_ms) or AUCTIONS[market])[2]
                elif symbol and (phase := preopen_phase(market, now_ms, holidays)):
                    base["preopen"], base["preopen_phase"] = (preopen_window(market, now_ms) or PRE_AUCTIONS[market])[2], phase
                    q, window = self.stocks.live.get(symbol), stock_quote_window(market, now_ms, holidays)
                    if q is not None and window and q.quoted_ms >= window[0] and not (q.prev_close and q.last == q.prev_close):
                        base["preopen_price"], base["preopen_at"] = fmt_price(q.last), q.quoted_ms  # the indicative price, whatever the odds rest on
                elif state := session_state(market, now_ms, holidays):
                    base["trading"] = state
            if symbol and symbol in self.config.tickers:
                base["pages"] = quote_pages(self.config.tickers[symbol])  # where to watch the stock's own quote
            if isinstance(odds, str):
                target = self.predict_day(title, now_ms)
                items.append({**base, "missing": odds, **(day_fields(target, now_ms) if target else {})})
                continue
            close_ms, close_label = self.target_close(title, odds.target)
            ref_day = re.search(r"\b\d\d-\d\d\b", odds.ref_note)
            items.append({
                **base, **day_fields(odds.target, now_ms), "ref_day": ref_day.group(0) if ref_day else "",
                "ref_rel": ref_relative(ref_day.group(0) if ref_day else "", odds.target, now_ms),
                "target": odds.target.strftime("%m-%d"), "unit": odds.unit or self.card_currency(symbol),
                "close_ms": close_ms, "close_label": close_label, "ref_note": odds.ref_note,
                "sigma_daily": odds.sigma_daily, "sigma_note": odds.sigma_note,
                **self.odds_numbers(title, name, odds),
            })
        if self.config.touch:  # one broken card must not take the whole page down with it
            items.extend(self.safe_card(f"{t.spec.symbol.removesuffix('USDT')} 先触", t.spec.key, "crypto", self.touch_payload, t, now_ms)
                         for t in self.touches.values())
            items.extend(self.safe_card(u.spec.name, u.spec.key, "crypto", self.updown_payload, u, now_ms) for u in self.updowns.values())
            items.extend(self.safe_card(f.spec.name, f.spec.key, "crypto", self.flip_payload, f, now_ms) for f in self.flips.values())
            items.extend(self.safe_card(r.spec.name, r.spec.key, "crypto", self.range_payload, r, now_ms) for r in self.ranges.values())
            items.extend(self.safe_card(c.spec.name, c.spec.key, "crypto", self.cap_payload, c, now_ms) for c in self.caps.values())
        if self.config.sim and self.config.predict:
            items.append(self.safe_card("模拟交易", "SIM", "sim", lambda: {"name": "模拟交易", "symbol": "SIM", "group": "sim",
                                                                          "kind": "sim", "sim": self.sim_report()}))
        today = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING).date()
        return {"generated_at": stamp(now_ms) + "（北京时间）", "version": VERSION, "server_ms": now_ms,
                "today": f"{today:%m-%d} {WEEKDAYS[today.weekday()]}",
                "mode": BASELINE_SHORT.get(self.settings()["mode"], self.settings()["mode"]),
                "color_style": self.config.color_style, "items": items, "notional": self.config.predict_trade_usd,
                "note": ("模型参考，非投资建议。有效价 = 参考收盘 × 代理现价 ÷ 代理在参考收盘时刻的价格；"
                         "P(涨) = 1 − Φ(ln((参考+半跳)/有效)/σ剩余)，平盘两边各计一半。目标日跳过周末和已配置的交易所假期。"
                         if self.config.probability else "概率功能已关闭（PROBABILITY=off）。")}

    def odds_numbers(self, title: str, name: str, odds: CloseOdds) -> dict:
        """A daily card's figures that move with every quote: shared by data.json and the event stream (live.json), so
        the page can patch a card's headline in place between full answers."""
        return {
            "eff_label": "开盘价" if odds.matched else "竞价" if odds.preopen else "今日" if odds.direct else "隐含" if name == "上证指数" else "估算",  # A50-implied, not an SSE print
            "quote_ms": self.odds_quote_ms(title, odds), "feed": self.quote_feed(title, odds),
            "source": ("已撮合开盘价" if odds.matched else "竞价参考价" if odds.preopen else "现货" if odds.direct
                       else "代理估算·近似锚点" if "近似" in odds.warn else "代理估算"),
            "ref": fmt(odds.ref), "effective": fmt(odds.effective.quantize(D("0.0001"))),
            "move": float(percent(odds.effective, odds.ref)), "proxy_note": odds.proxy_note, "warn": odds.warn,
            "sigma": odds.sigma, "remaining": odds.remaining, "z": odds.z, "up": odds.up, "flat": odds.flat, "down": odds.down,
            "fair_up": odds.fair_up, "fair_down": odds.fair_down,
        }

    def quote_feed(self, title: str, odds: CloseOdds) -> str:
        """The feed a contract card's own price came from (腾讯 / 新浪 / Naver) while it prices the stock itself, "" when
        the price is a proxy's estimate: the card says which source and when it quoted, so the figure can be checked
        against that source's page."""
        if not odds.direct or "｜" not in title:
            return ""
        q = self.stocks.live.get(title.split("｜")[-1])
        return q.source if q is not None else ""

    def live_payload(self) -> dict:
        """The index and contract cards' live numbers for the page's event stream (/events): every item data.json has
        for them, keyed the same way (name + symbol), with only the figures that change between quotes. A card whose
        odds are paused carries the reason instead. The crypto cards and the books keep to data.json."""
        now_ms = self.market.now_ms()
        items = []
        for title, odds in (self.odds_items(now_ms) if self.config.probability else []):
            name, _, symbol = title.partition("｜")
            if isinstance(odds, str):
                items.append({"name": name, "symbol": symbol, "missing": odds})
            else:
                items.append({"name": name, "symbol": symbol, **self.odds_numbers(title, name, odds)})
        return {"server_ms": now_ms, "generated_at": stamp(now_ms) + "（北京时间）", "items": items}

    def safe_card(self, name: str, key: str, group: str, build: Any, *args: Any) -> dict:
        """One card's payload, or a placeholder saying it could not be built (logged, rate-limited): a feed changing
        shape under one card must not turn the whole page into an HTTP 500."""
        try:
            return build(*args)
        except Exception as error:
            self.log_limited(f"card:{key}", f"网页卡片 {name} 构建失败：{clean_error(error) or type(error).__name__}")
            return {"name": name, "symbol": key, "group": group, "missing": f"卡片构建失败：{brief_error(clean_error(error) or type(error).__name__, 80)}"}

    def touch_book(self, spec: TouchSpec) -> tuple[PredictBook | None, str]:
        """The book priced as "high barrier first" (the card's 涨 side), whatever the market's outcome order."""
        book = self.predict.books.get(spec.key)
        if book is None:
            return None, ""
        names = (self.predict.info.get(spec.slug) or {}).get("outcomes") or []
        first = touch_outcome(names[0], spec) if names else ""
        if first == "high":
            return book, ""
        if first == "low":  # the book prices "low first": its complement is "high first"
            return flip_book(book), ""
        return None, f"盘口方向未确认（结果名称：{'、'.join(names) or '未取得'}），暂不比较"

    def touch_payload(self, touch: "TouchMarket", now_ms: int) -> dict:
        """Web card for a first-touch market: 涨 = the high barrier first, 跌 = the low one first."""
        spec = touch.spec
        low, high = spec.label(spec.low), spec.label(spec.high)
        base = {"name": f"{spec.symbol.removesuffix('USDT')} 先触 {low}/{high}", "symbol": spec.symbol, "group": "crypto",
                "labels": [f"${high}", f"${low}"], "close_ms": spec.deadline_ms, "close_label": spec.close_label()}
        out: dict[str, Any] = {"url": predict_url(spec.slug, self.config.predict_ref), "error": self.predict.errors.get(spec.key, "")}
        book, why = self.touch_book(spec)
        odds = touch.odds(now_ms)
        hold = touch.advice_problem(now_ms) if isinstance(odds, TouchOdds) else ""
        if isinstance(odds, TouchOdds) and not hold and book is not None and not book.stale(now_ms):
            decided = book_decided(book)
            if (decided == "up" and odds.fair_upper < 0.9) or (decided == "down" and odds.fair_upper > 0.1):
                hold = (f"盘口已把 {high if decided == 'up' else low} 先触当作定局（{'买价 ≥90¢' if decided == 'up' else '卖价 ≤10¢'}），"
                        "模型尚未核验到这次触线；待核验，暂不给建议")
        if book is not None:
            ok = isinstance(odds, TouchOdds)
            swing = touch.model_swing(now_ms) if ok else 0.0
            self.book_block(out, book, odds.fair_upper if ok else None, self.edge_need(swing), swing, hold, (high, low), now_ms)
        elif why and spec.key in self.predict.books:
            out["error"] = why
        if self.config.predict:
            base["predict"] = out
        if isinstance(odds, str):
            return {**base, "missing": odds}
        price = touch.price
        years = max(0.0, (spec.deadline_ms - now_ms) / YEAR_MS)
        return {**base, "quote_ms": touch.priced_ms, "source": "币安现货", "fair_up": odds.fair_upper, "fair_down": odds.fair_lower, "up": odds.upper, "flat": odds.none,
                "down": odds.lower, "touch": {
                    "coin": spec.symbol.removesuffix("USDT"),
                    "price": f"{price:,.2f}" if price is not None else "—",
                    "to_low": float(percent(spec.low, price)) if price else 0.0,
                    "to_high": float(percent(spec.high, price)) if price else 0.0,
                    "sigma": touch.sigma or 0.0, "years": years, "status": touch.status(),
                    "p_low": odds.lower, "p_high": odds.upper, "p_none": odds.none,
                    "error": touch.error, "hold": hold}}

    def updown_book(self, spec: UpDownSpec) -> tuple[PredictBook | None, str]:
        """The book priced as "Up" (the card's 涨 side), whatever the market's outcome order."""
        book = self.predict.books.get(spec.key)
        if book is None:
            return None, ""
        names = (self.predict.info.get(spec.slug) or {}).get("outcomes") or []
        first = names[0].strip().lower() if names else ""
        if first in {"up", "涨", "上涨"}:
            return book, ""
        if first in {"down", "跌", "下跌"}:  # the book prices "Down": its complement is "Up"
            return flip_book(book), ""
        return None, f"盘口方向未确认（结果名称：{'、'.join(names) or '未取得'}），暂不比较"

    def flip_yes_book(self, spec: FlipSpec) -> tuple[PredictBook | None, str]:
        """The book priced as "Yes" (the card's left side), whatever the market's outcome order."""
        book = self.predict.books.get(spec.key)
        if book is None:
            return None, ""
        names = (self.predict.info.get(spec.slug) or {}).get("outcomes") or []
        first = names[0].strip().lower() if names else ""
        if first == "yes":
            return book, ""
        if first == "no":
            return flip_book(book), ""
        return None, f"盘口方向未确认（结果名称：{'、'.join(names) or '未取得'}），暂不比较"

    def flip_hold(self, fm: "FlipMarket", odds: float, book: PredictBook | None, now_ms: int) -> str:
        """Why no trade is suggested: the market's own reasons, or our data says it flipped while the book disagrees."""
        hold = fm.advice_problem(now_ms)
        if odds >= 1.0 and fm.history.get("kind") == "flip" and book_disputes(book):
            hold = hold or "数据显示已反超，但盘口仍低于 90¢；以 Hyperliquid 1 分钟 K 为准，请核实"
        return hold

    def flip_payload(self, fm: "FlipMarket", now_ms: int) -> dict:
        """Web card for a flip market: Yes = A closes a minute above B inside the window."""
        spec = fm.spec
        base = {"name": spec.name, "symbol": spec.key, "group": "crypto", "labels": ["Yes", "No"], "close_ms": spec.end_ms + 60_000,
                "close_label": f"{spec.window()}，任一 1 分钟 K 收盘 {spec.coin} > {spec.other} 即 Yes"}
        odds = fm.odds(now_ms)
        book, why = self.flip_yes_book(spec)
        hold = self.flip_hold(fm, odds, book, now_ms) if isinstance(odds, float) else ""
        out: dict[str, Any] = {"url": predict_url(spec.slug, self.config.predict_ref), "error": self.predict.errors.get(spec.key, "")}
        if book is not None:
            ok = isinstance(odds, float)
            swing = fm.model_swing(now_ms) if ok else 0.0
            self.book_block(out, book, odds if ok else None, self.edge_need(swing), swing, hold, ("Yes", "No"), now_ms)
        elif why and spec.key in self.predict.books:
            out["error"] = why
        if self.config.predict:
            base["predict"] = out
        if isinstance(odds, str):
            return {**base, "missing": odds}
        a, b, ratio = fm.prices.get(spec.coin), fm.prices.get(spec.other), fm.ratio
        return {**base, "quote_ms": fm.priced_ms, "source": "Hyperliquid 中间价", "fair_up": odds, "fair_down": 1 - odds, "up": odds, "flat": 0.0, "down": 1 - odds, "flip": {
            "coin": spec.coin, "other": spec.other, "a": short_price(a), "b": short_price(b), "ratio": ratio,
            "gap": (1 / ratio - 1) if ratio else None, "sigma": fm.sigma or 0.0, "window": spec.window(),
            "years": max(0.0, (spec.end_ms + 60_000 - max(now_ms, spec.start_ms)) / YEAR_MS),
            "status": fm.status(now_ms), "error": fm.error, "hold": hold}}

    def updown_hold(self, mkt: "UpDownMarket", odds: UpDownOdds, now_ms: int) -> tuple[str, str]:
        """(why no trade is suggested, a note on the line): the market's own reasons, and Predict's target price (when its
        category shows one) must be the same opening close we read from Binance."""
        hold = mkt.advice_problem(odds, now_ms)
        strike = (self.predict.strikes.get(mkt.spec.slug) or (None,))[0]
        if strike is None:
            return hold, ""
        if abs(percent(strike, odds.line)) > D("0.005"):
            return hold or f"Predict 目标价 {strike:,.2f} 与币安起点 {odds.line:,.2f} 不一致，请核实；暂不给建议", ""
        return hold, "；与 Predict 目标价一致"

    def updown_payload(self, mkt: "UpDownMarket", now_ms: int) -> dict:
        """Web card for a period up/down market: the usual 涨/跌 card, measured from the starting candle's close."""
        spec = mkt.spec
        base = {"name": spec.name, "symbol": spec.key, "group": "crypto", "close_ms": spec.end_ms + 60_000,
                "close_label": f"{spec.label(spec.end_ms)}这根 1 分钟 K 的收盘价；高于起点为涨、低于为跌、相同按 50/50 结算"}
        odds = mkt.odds(now_ms)
        hold, agree = self.updown_hold(mkt, odds, now_ms) if isinstance(odds, UpDownOdds) else ("", "")
        out: dict[str, Any] = {"url": predict_url(spec.slug, self.config.predict_ref), "error": self.predict.errors.get(spec.key, "")}
        book, why = self.updown_book(spec)
        if book is not None:
            ok = isinstance(odds, UpDownOdds)
            swing = mkt.model_swing(odds) if ok else 0.0
            self.book_block(out, book, odds.fair_up if ok else None, self.edge_need(swing), swing, hold, ("涨", "跌"), now_ms)
        elif why and spec.key in self.predict.books:
            out["error"] = why
        if self.config.predict:
            base["predict"] = out
        if isinstance(odds, str):
            return {**base, "missing": odds}
        sigma = mkt.sigma or 0.0
        verdict = "涨" if odds.up else "跌" if odds.down else "持平，按 50/50"
        return {**base, "quote_ms": 0 if odds.settled else mkt.priced_ms, "source": "已结算" if odds.settled else "币安现货",
                "ref": f"{odds.line:,.2f}", "ref_rel": "起点", "unit": "USDT",
                "ref_note": f"币安 {spec.symbol} 1 分钟 K 收盘：{spec.label(spec.start_ms, plain=True)}{agree}",
                "eff_label": "终点" if odds.settled else "现价", "effective": f"{odds.price:,.2f}",
                "move": float(percent(odds.price, odds.line)),
                "proxy_note": (f"已结算：终点 {odds.price:,.2f}（{spec.label(spec.end_ms, plain=True)}）对起点 {odds.line:,.2f} → {verdict}"
                               if odds.settled else f"不用代理：币安 {spec.symbol} 现价就是结算源"),
                "warn": hold, "sigma_daily": sigma / math.sqrt(365), "remaining": odds.years * 365,
                "sigma": sigma * math.sqrt(odds.years),
                "sigma_note": (f"币安 {spec.symbol} 30 日小时收盘年化 {sigma * 100:.1f}%；价格零漂移，中位数比现价低 σ²τ/2"
                               if not odds.settled else "已结算"),
                "z": odds.z, "up": odds.up, "flat": odds.flat, "down": odds.down,
                "fair_up": odds.fair_up, "fair_down": odds.fair_down}

    def ladder_fair(self, cap: "CapMarket", row: "LadderRow", now_ms: int) -> float | None:
        """A ladder level's Yes fair price as its card shows it (a market Predict settled before the window closed counts
        as reached: only a touch settles one early)."""
        fair = cap.probability(row.target, now_ms)
        meta = (self.predict.market_meta.get(row.market_id) or ({}, 0))[0] if row.market_id else {}
        if "RESOLVED" in str(meta.get("status", "")).upper() and now_ms < cap.spec.end_ms:
            fair = 1.0
        return fair

    def cap_settled(self, row: "LadderRow") -> bool:
        """Predict has settled this level's market."""
        meta = (self.predict.market_meta.get(row.market_id) or ({}, 0))[0] if row.market_id else {}
        return "RESOLVED" in str(meta.get("status", "")).upper()

    SIM_SECONDS = 10                  # the paper trader looks at the books this often (they refresh every 15 s)
    SIM_SETTLE_MS = 60 * 60_000       # a daily market is settled this long after its close: the official close is in

    def sim_trades(self) -> dict[str, dict]:
        """Every paper trade, from an in-memory view of the store (the journal page and the sim step would otherwise parse
        every record's JSON on every look); any write to a sim: record, by whoever, refreshes the view."""
        generation = self.store.touched.get("sim", 0)
        if self.sim_cache is None or self.sim_gen != generation:
            self.sim_cache = {k.removeprefix("sim:"): sim_upgrade(v) for k, v in self.store.items("sim:")
                              if isinstance(v, dict) and "status" in v}
            self.sim_gen = generation
        return dict(self.sim_cache)

    def sim_save(self, pairs: Any) -> None:
        """Persist (trade id, trade) pairs in one transaction and keep the in-memory view current."""
        pairs = list(pairs)
        self.store.put_many((f"sim:{tid}", trade) for tid, trade in pairs)
        if self.sim_cache is not None:
            self.sim_cache.update(pairs)
        self.sim_gen = self.store.touched.get("sim", 0)

    def note_outcome(self, key: str, day: str, value: Any, source: str) -> None:
        """An official close as it becomes known (scored by /calib, settles the paper trades), with where it came from."""
        value = float(value)
        if self.store.get(f"outcome:{key}:{day}") != value:
            self.store.put(f"outcome:{key}:{day}", value)
        record = {"value": value, "source": source}
        if {k: v for k, v in (self.store.get(f"outsrc:{key}:{day}") or {}).items() if k != "at"} != record:
            self.store.put(f"outsrc:{key}:{day}", {**record, "at": self.market.now_ms()})

    def close_evidence(self, title: str, odds: CloseOdds, now_ms: int) -> dict:
        """What a daily card's odds rest on, for the trade's record: the model's inputs, every price behind them (its real
        source, code, quote and read times, kind of price) and, for an estimate, the proxy and its anchor."""
        basis = {"fair_up": odds.fair_up, "ref": float(odds.ref), "ref_note": odds.ref_note, "effective": float(odds.effective),
                 "sigma_daily": odds.sigma_daily, "sigma_note": odds.sigma_note, "remaining": odds.remaining,
                 "sigma": odds.sigma, "z": odds.z, "beta": odds.beta, "mode": odds.mode, "direct": odds.direct,
                 "proxy_note": odds.proxy_note, "target": odds.target.isoformat(), "warn": odds.warn,
                 "close_ms": self.target_close(title, odds.target)[0]}
        sources: list[dict] = []
        proxy: dict | None = None
        if odds.ref_note.startswith("Predict 目标价"):
            sources.append(price_evidence("参考线", "Predict 目标价（startPrice）", self.predict_key(title), "结算基准", odds.ref))
        if title == "上证指数":
            q, close = self.cn.quote, self.cn.close
            if odds.direct and q is not None:
                sources.append(price_evidence("上证现货", q.source, "sh000001", "最新价", q.last, q.quoted_ms, q.fetched_ms))
            if close is not None:
                sources.append(price_evidence("参考收盘", close.source, "sh000001", "日K收盘", close.value, close.close_ms,
                                              close.checked_ms, day=close.day.isoformat()))
            a50, anchor = self.cn.a50, self.anchors.get("A50")
            if not odds.direct and a50 is not None:
                sources.append(price_evidence("A50 期货", a50.source, a50.name, "最新价", a50.last, a50.quoted_ms, a50.fetched_ms))
                proxy = {"proxy": "富时中国 A50 期货", "family": a50_family(a50.source), "contract": a50.name,
                         "price": float(a50.last), "quoted_ms": a50.quoted_ms,
                         "anchor": float(anchor[1]) if anchor else None, "anchor_ms": anchor[0] if anchor else 0,
                         "anchor_note": self.a50_anchor_note, "anchor_family": a50_family(self.a50_anchor_source),
                         "beta": odds.beta, "approx": "近似" in self.a50_anchor_note,
                         "expiry_day": bool(close and a50_expiry(close.day))}
        elif title == "恒生指数":
            q, holidays = self.hsi.quote, self.hsi.holidays
            if q is not None:
                day = expected_close_date("hk", now_ms, holidays) if odds.direct else hk_cash_close_date(now_ms, holidays)
                rank = self.hsi_daily.ranks.get(day)
                ref_source = self.hsi_daily.sources[rank][0] + " 日K" if rank is not None else (q.spot_source or q.source) + " 实时"
                sources.append(price_evidence("参考收盘", ref_source, "HSI", "收盘", odds.ref, day=day.isoformat()))
                if odds.direct:
                    sources.append(price_evidence("恒指现货", q.spot_source or q.source, "HSI", "现货指数", q.spot,
                                                  q.spot_ms or q.quoted_ms, q.fetched_ms))
                else:
                    fq = self.hsi_used or q  # the family the odds were mapped from
                    sources.append(price_evidence("恒指期货", fq.source, fq.name, "期货最新价", fq.last, fq.quoted_ms, fq.fetched_ms))
                    anchor, note, approx = self.hsi_anchor(fq, day, now_ms)
                    proxy = {"proxy": "恒指期货", "contract": fq.contract or fq.name, "exchange_contract": fq.exchange_contract,
                             "session": fq.session_name(holidays), "price": float(fq.last), "quoted_ms": fq.quoted_ms,
                             "anchor": float(anchor) if anchor is not None else None, "anchor_note": note, "approx": approx}
        elif title == "KOSPI":
            k, holidays = self.kospi.quote, self.config.holidays.get("kr", frozenset())
            if k is not None:
                kst = dt.timezone(dt.timedelta(hours=9))
                day = (expected_close_date("kr", now_ms, holidays) if odds.direct
                       else dt.datetime.fromtimestamp(k.quoted_ms / 1000, kst).date())
                rank = self.kospi.daily_rank.get(day) if day in self.kospi.daily else None
                ref_source = ("Yahoo ^KS11 日K", "Naver 日K")[rank] if rank in (0, 1) else f"{k.source} 实时"
                sources.append(price_evidence("参考收盘", ref_source, "KOSPI", "收盘", odds.ref, day=day.isoformat()))
                if odds.direct:
                    sources.append(price_evidence("KOSPI 现货", k.source, "KOSPI", "最新价", k.last, k.quoted_ms, k.fetched_ms))
            hl, anchor = self.hl.quotes.get("KR200"), self.anchors.get("KOSPI")
            if not odds.direct and hl is not None:
                price, kind = kr200_price(hl)
                sources.append(price_evidence("HL KR200 永续", "Hyperliquid", hl.coin, kind, price, 0, hl.fetched_ms))
                proxy = {"proxy": "Hyperliquid KR200 永续（跟踪 KOSPI200）", "maps": "KOSPI200 → KOSPI 综合", "price": float(price),
                         "fetched_ms": hl.fetched_ms, "anchor": float(anchor[1]) if anchor else None,
                         "anchor_ms": anchor[0] if anchor else 0, "anchor_note": self.kospi_anchor_note, "beta": odds.beta,
                         "approx": "近似" in self.kospi_anchor_note}
        else:
            symbol = title.split("｜")[-1]
            ticker = self.config.tickers.get(symbol)
            info = STOCK_MARKETS[ticker.market] if ticker else None
            code = f"{ticker.market}:{ticker.code}" if ticker else symbol
            base = self.stocks.closes.get(symbol)
            if base is not None:
                sources.append(price_evidence("参考收盘", base.source, code, "交易所收盘", base.value, base.close_ms))
            live_close = self.store.get(f"live_close:{symbol}")
            if isinstance(live_close, list) and len(live_close) == 3 and "现货收盘" in odds.ref_note:
                sources.append(price_evidence("参考收盘（日K待确认）", f"{live_close[2]} 现货收盘价", code, "收盘价", live_close[1],
                                              int(live_close[0])))
            if odds.direct:
                live = self.stocks.live.get(symbol)
                if live is not None:
                    sources.append(price_evidence(f"{info.name if info else ''}现货", live.source, code, "最新价", live.last,
                                                  live.quoted_ms, live.fetched_ms))
            else:
                quote, anchor = (self.snapshots.get(symbol) or {}).get("quote"), self.anchors.get(symbol)
                if quote is not None:
                    kind = "最新成交价" if quote.source == "last" else "标记价"
                    sources.append(price_evidence("币安合约", "币安 U 本位合约", symbol, kind, quote.price, quote.timestamp_ms))
                    proxy = {"proxy": f"币安 {symbol} 合约", "price": float(quote.price), "quoted_ms": quote.timestamp_ms,
                             "anchor": float(anchor[1]) if anchor else None, "anchor_ms": anchor[0] if anchor else 0,
                             "anchor_note": "收盘时刻的币安价格", "approx": False}
        return {"basis": basis, "sources": sources, "proxy": proxy}

    def market_evidence(self, kind: str, now_ms: int, **parts: Any) -> dict:
        """The same record for the crypto cards and the ladders (their prices are the resolution sources' own feeds)."""
        if kind == "touch":
            touch: TouchMarket = parts["touch"]
            spec = touch.spec
            return {"basis": {"price": float(touch.price or 0), "low": float(spec.low), "high": float(spec.high),
                              "sigma": touch.sigma, "sigma_ms": touch.sigma_ms, "deadline_ms": spec.deadline_ms,
                              "years": max(0.0, (spec.deadline_ms - max(now_ms, touch.start_ms)) / YEAR_MS),
                              "path": touch.status(), "start_ms": touch.start_ms},
                    "sources": [price_evidence("现货", "币安现货", spec.symbol, "最新价", touch.price, touch.priced_ms,
                                               touch.priced_ms)], "proxy": None}
        if kind == "updown":
            mkt: UpDownMarket = parts["mkt"]
            odds: UpDownOdds = parts["odds"]
            spec = mkt.spec
            return {"basis": {"line": float(odds.line), "price": float(odds.price), "sigma": mkt.sigma, "years": odds.years,
                              "settled": odds.settled, "start": spec.label(spec.start_ms, plain=True),
                              "end": spec.label(spec.end_ms, plain=True)},
                    "sources": [price_evidence("现货", "币安现货", spec.symbol, "最新价", mkt.price, mkt.priced_ms, mkt.priced_ms),
                                price_evidence("起点", "币安 1 分钟 K", spec.symbol, "收盘", odds.line, spec.start_ms)],
                    "proxy": None}
        if kind == "flip":
            fm: FlipMarket = parts["fm"]
            spec = fm.spec
            return {"basis": {"a": float(fm.prices.get(spec.coin) or 0), "b": float(fm.prices.get(spec.other) or 0),
                              "ratio": fm.ratio, "sigma": fm.sigma, "window": spec.window(), "path": fm.status(now_ms)},
                    "sources": [price_evidence(coin, "Hyperliquid", coin, "中间价", fm.prices.get(coin), fm.priced_ms, fm.priced_ms)
                                for coin in (spec.coin, spec.other)], "proxy": None}
        if kind == "range":
            rm: RangeMarket = parts["rm"]
            row, direction = parts["row"], parts["direction"]
            high, low = rm.extremes()
            hist = rm.history
            return {"basis": {"price": float(rm.price or 0), "level": float(row.target), "dir": direction,
                              "dir_source": parts.get("source", ""), "sigma": rm.sigma, "sigma_ms": rm.sigma_ms,
                              "years": rm.remaining_years(now_ms),
                              "window_high": high, "window_low": low, "through": int(hist.get("through") or 0),
                              "close_ms": rm.window_end},
                    "sources": [price_evidence("现价", rm.source_name(), rm.spec.symbol, "最新价", rm.price, rm.priced_ms, rm.priced_ms),
                                price_evidence(f"{rm.range_word}最高", rm.extremes_name(), rm.spec.symbol, "最高价", hist.get("high"),
                                               int(hist.get("high_at") or 0)),
                                price_evidence(f"{rm.range_word}最低", rm.extremes_name(), rm.spec.symbol, "最低价", hist.get("low"),
                                               int(hist.get("low_at") or 0))], "proxy": None}
        cap: CapMarket = parts["cap"]
        row = parts["row"]
        high, high_at = cap.window_high()
        supply = "BSC 节点：总量 − 销毁" if cap.spec.supply == "rpc" else "DexScreener FDV ÷ 价格"
        return {"basis": {"cap": float(cap.cap) if cap.cap else None, "target": float(row.target), "price": float(cap.price or 0),
                          "supply": float(cap.supply) if cap.supply else None, "sigma": cap.sigma, "sigma_kind": cap.sigma_kind,
                          "sigma_note": cap.sigma_note, "window_high": float(high) if high else None, "high_at": high_at,
                          "coverage": cap.coverage() or "完整"},
                "sources": [price_evidence("价格", cap.source, cap.spec.pair or cap.spec.token, "最新价", cap.price, cap.priced_ms,
                                           cap.priced_ms),
                            price_evidence("供应量", supply, cap.spec.token, "供应量", cap.supply)], "proxy": None}

    def sim_version(self) -> dict:
        """The code and the settings a trade was made under, so a change in results can be traced to a change here."""
        c = self.config
        ways, kinds = sim_scope(c)
        return {"code": VERSION, "sim_edge": c.sim_edge, "sim_shares": c.sim_shares, "sim_ways": ways, "sim_markets": kinds,
                "sim_group_usd": c.sim_group_usd, "min_edge": c.predict_min_edge,
                "fee_bps": c.predict_fee_bps, "trade_usd": c.predict_trade_usd, "a50_beta": c.a50_beta,
                "kospi_beta": c.kospi_beta, "sigma_error": MODEL_SIGMA_ERROR, "beta_error": MODEL_BETA_ERROR}

    def evidence(self, build: Any) -> "LazyEvidence":
        """A SimMarket's evidence, built on first access: when a trade is opened or filled, not for every market on
        every look of the paper trader, the alerts and the journal page."""
        return LazyEvidence(build)

    @staticmethod
    def evidence_of(mk: SimMarket) -> dict:
        return dict(mk.evidence)

    def sim_markets(self, now_ms: int) -> list[SimMarket]:
        """Every Predict market a card prices right now, with the bar its suggestion must clear and why it holds back."""
        out: list[SimMarket] = []
        if self.config.probability:
            for title, odds in self.odds_items(now_ms):
                key = self.predict_key(title)
                slug, book = self.predict.slugs.get(key), self.predict.books.get(key)
                if not isinstance(odds, CloseOdds) or not slug or book is None or book.slug != slug:
                    continue
                name, _, symbol = title.partition("｜")
                close_ms, _ = self.target_close(title, odds.target)
                out.append(SimMarket(slug, name, "close", key, odds.fair_up, book, self.edge_need(model_swing(odds)), odds.warn,
                                     ("涨", "跌"), {"key": symbol or key, "target": odds.target.isoformat(),
                                                   "line": float(odds.ref), "close_ms": close_ms},
                                     self.evidence(lambda title=title, odds=odds: self.close_evidence(title, odds, now_ms))))
        if not self.config.touch:
            return out
        for touch in self.touches.values():
            spec, odds = touch.spec, touch.odds(now_ms)
            book, _ = self.touch_book(spec)
            if isinstance(odds, TouchOdds) and book is not None:
                low, high = spec.label(spec.low), spec.label(spec.high)
                out.append(SimMarket(spec.slug, f"{spec.symbol.removesuffix('USDT')} 先触 {low}/{high}", "touch", spec.key,
                                     odds.fair_upper, book, self.edge_need(touch.model_swing(now_ms)),
                                     touch.advice_problem(now_ms), (high, low), {"deadline": spec.deadline_ms},
                                     self.evidence(lambda touch=touch: self.market_evidence("touch", now_ms, touch=touch))))
        for mkt in self.updowns.values():
            odds = mkt.odds(now_ms)
            book, _ = self.updown_book(mkt.spec)
            if isinstance(odds, UpDownOdds) and book is not None:
                out.append(SimMarket(mkt.spec.slug, mkt.spec.name, "updown", mkt.spec.key, odds.fair_up, book,
                                     self.edge_need(mkt.model_swing(odds)), self.updown_hold(mkt, odds, now_ms)[0], ("涨", "跌"),
                                     {"end": mkt.spec.end_ms},
                                     self.evidence(lambda mkt=mkt, odds=odds: self.market_evidence("updown", now_ms, mkt=mkt, odds=odds))))
        for fm in self.flips.values():
            odds = fm.odds(now_ms)
            book, _ = self.flip_yes_book(fm.spec)
            if isinstance(odds, float) and book is not None:
                out.append(SimMarket(fm.spec.slug, fm.spec.name, "flip", fm.spec.key, odds, book, self.edge_need(fm.model_swing(now_ms)),
                                     self.flip_hold(fm, odds, book, now_ms), ("Yes", "No"), {"end": fm.spec.end_ms},
                                     self.evidence(lambda fm=fm: self.market_evidence("flip", now_ms, fm=fm))))
        for rm in self.ranges.values():
            problem = rm.advice_problem(now_ms)
            for row in self.predict.ladders.get(rm.spec.key) or []:
                direction, source = self.range_level(rm, row)
                fair = self.range_fair(rm, row, direction, now_ms)
                book, _ = self.predict.yes_book(row) if row.market_id else (None, "")
                if fair is None or book is None:
                    continue
                hold = ("数据显示已触及，但盘口仍低于 90¢" if fair == 1.0 and book_disputes(book) else problem
                        or ("Predict 已结算" if self.range_settled(row) else "") or RANGE_GUESS_SHORT.get(source, ""))
                maker_status = self.range_maker_status(row, book, now_ms)
                out.append(SimMarket(f"{rm.spec.slug}#{row.market_id}", f"{rm.spec.name} {('↑ ' if direction == 'up' else '↓ ')}"
                                     f"{level_label(row.target)}", "range", rm.spec.key, fair, book,
                                     self.edge_need(rm.model_swing(row.target, direction, now_ms, fair)), hold, ("Yes", "No"),
                                     {"target": str(row.target), "dir": direction, "end": rm.end_ms},
                                     self.evidence(lambda rm=rm, row=row, direction=direction, source=source:
                                                   self.market_evidence("range", now_ms, rm=rm, row=row, direction=direction,
                                                                        source=source)), makers=RANGE_SIM_MAKERS,
                                     maker_alerts=maker_status["makers"], maker_note=maker_status["maker_note"]))
        for cap in self.caps.values():
            for row in self.predict.ladders.get(cap.spec.key) or []:
                fair = self.ladder_fair(cap, row, now_ms)
                book, _ = self.predict.yes_book(row) if row.market_id else (None, "")
                if fair is None or book is None:
                    continue
                hold = ("数据显示已触及，但盘口仍低于 90¢" if fair == 1.0 and book_disputes(book)
                        else "窗口已结束" if now_ms >= cap.spec.end_ms + 60_000
                        else "σ 是先验值" if cap.sigma_kind == "prior" else "")
                out.append(SimMarket(f"{cap.spec.slug}#{row.market_id}", f"{cap.spec.name} {usd_short(row.target)}", "ladder",
                                     cap.spec.key, fair, book, self.edge_need(cap.model_swing(row.target, now_ms, fair)), hold,
                                     ("Yes", "No"), {"target": str(row.target), "end": cap.spec.end_ms},
                                     self.evidence(lambda cap=cap, row=row: self.market_evidence("ladder", now_ms, cap=cap, row=row))))
        return out

    def sim_note_closes(self) -> None:
        """Each contract's exchange close, kept per day (its feed holds only the latest), to settle the daily market."""
        for symbol, base in self.stocks.closes.items():
            ticker = self.config.tickers.get(symbol)
            if ticker is None or not base.close_ms:
                continue
            offset = dt.timezone(dt.timedelta(hours=STOCK_MARKETS[ticker.market].utc_offset))
            day = dt.datetime.fromtimestamp(base.close_ms / 1000, offset).date().isoformat()
            self.note_outcome(symbol, day, base.value, base.source)

    def sim_open(self, mk: SimMarket, side: str, now_ms: int, maker: BookEdge | None = None,
                 taker: dict | None = None) -> dict:
        """A paper buy of SIM_SHARES of one side, with everything it was decided on (entry snapshot: the model's inputs,
        the price sources and proxy, the book, the code and settings). A taker fills now across the book's depth (the
        quote it was judged on); a maker rests at its price behind the queue already there and fills only as sellers
        show up at or through it (see maker_fill)."""
        fair = mk.fair_up if side == "up" else 1 - mk.fair_up
        edges = book_edges(mk.fair_up, mk.book, self.edge_costs())
        shown = None if mk.hold or mk.book.stale(now_ms) else best_edge(edges, mk.need)  # the card's framed direction
        driver, driver_name = market_driver(mk.kind, mk.key, mk.settle, mk.item)
        trade = {"v": 2, "market": mk.market, "slug": mk.book.slug, "market_id": mk.book.market_id, "item": mk.item,
                 "kind": mk.kind, "key": mk.key, "side": side, "label": ("挂" if maker else "吃") + mk.sides[0 if side == "up" else 1],
                 "maker": maker is not None, "fair": fair, "opened": now_ms, "settle": mk.settle,
                 "driver": driver, "driver_name": driver_name,  # the event it settles on: positions sharing it lose together
                 "order": self.config.sim_shares, "fills": [], "revisions": [],
                 "entry": {"at": now_ms, "fair": fair, "fair_up": mk.fair_up, "need": mk.need, "book": book_snapshot(mk.book),
                           "card": [edge_json(e, shown, e.label.replace("涨", mk.sides[0]).replace("跌", mk.sides[1]))
                                    for e in edges], **self.evidence_of(mk)},
                 "version": self.sim_version()}
        if maker is not None:
            queue = next((q for p, q in own_levels(mk.book, side) if abs(p - maker.price) < 1e-9), 0.0)
            trade.update(price=maker.price, signal=maker.edge, shares=0.0, status="resting", filled=None,
                         queue_ahead=queue, queue_min=queue)
        else:
            trade.update(price=taker["cost"], avg=taker["avg"], fee=taker["fee"], best=taker["best"],
                         slip=taker["avg"] - taker["best"], signal=fair - taker["cost"], shares=taker["got"], status="filled",
                         filled=now_ms, fill_fair=fair,
                         fills=[{"at": now_ms, "shares": taker["got"], "fair": fair, "how": "吃单立即成交",
                                 "levels": taker["levels"], "short": taker["short"]}])
        trade["edge"] = fair - trade["price"]  # the expectation per share when the decision was made
        return trade

    def sim_fill(self, trade: dict, mk: SimMarket, now_ms: int) -> None:
        """A resting order against the book as it stands now: more shares presumed filled when sellers show at or
        through its price (with a snapshot of what was seen, the model's view and its sources at that moment)."""
        credited, seen = maker_fill(trade, mk.book)
        trade["queue_min"] = min(float(trade.get("queue_min", trade.get("queue_ahead", 0.0))), seen["queue_now"])
        if credited <= float(trade["shares"]) + 1e-9:
            return
        fair = mk.fair_up if trade["side"] == "up" else 1 - mk.fair_up
        trade["fills"].append({"at": now_ms, "shares": credited - float(trade["shares"]), "fair": fair, "fair_up": mk.fair_up,
                               "how": "推定成交", "seen": seen, "book": book_snapshot(mk.book), **self.evidence_of(mk)})
        trade["shares"] = credited
        if trade.get("filled") is None:
            trade["filled"], trade["fill_fair"] = now_ms, fair
        if credited >= float(trade["order"]) - 1e-9:
            trade["status"] = "filled"

    def sim_withdraw_reason(self, trade: dict, mk: SimMarket | None) -> str:
        """Why a resting paper order is withdrawn now rather than left to the result: the trader no longer places such
        orders (SIM_WAYS, SIM_MARKETS, a kind that is taker-only such as the price ladders). "" while it stands."""
        kind = str(trade.get("kind") or "")
        if self.config.sim_ways == "taker":
            return "模拟交易已改为只吃单"
        if kind not in self.config.sim_markets:
            return f"模拟交易范围已不含{SIM_KINDS.get(kind, kind)}"
        if (kind == "range" and not RANGE_SIM_MAKERS) or (mk is not None and not mk.makers):
            return f"{SIM_KINDS.get(kind, kind)}只做吃单"
        return ""

    def sim_withdraw(self, trade: dict, why: str, now_ms: int) -> None:
        """Withdraw a resting order: the shares it had filled stay a position, the rest lapses now, not at the result."""
        shares, order = float(trade["shares"]), float(trade["order"])
        trade["withdrawn"] = {"at": now_ms, "why": why, "unfilled": order - shares}
        if shares > 1e-9:
            trade.update(status="filled", unfilled=order - shares,
                         note=f"撤单：{why}；已推定成交的 {shares:g} 份继续持有，其余 {order - shares:g} 份作废")
        else:
            trade.update(status="cancelled", unfilled=order, note=f"撤单：{why}，一份都没成交")

    def sim_wait(self, trade: dict, mk: SimMarket | None, now_ms: int) -> str:
        """What an open trade is waiting for, in words: a resting order, where the sellers stand against its price; a
        position, the result it settles on and when the bot can decide it. "" once settled, expired or withdrawn."""
        status, s = trade["status"], trade.get("settle") or {}
        at = lambda ms: stamp(ms, seconds=False)
        if status == "resting":
            if mk is None:
                return "这个市场现在没有报价（卡片未定价或盘口没读到），挂单原地等着"
            if mk.book.stale(now_ms):
                return "盘口已过期，等新盘口"
            price, fair = float(trade["price"]), (mk.fair_up if trade["side"] == "up" else 1 - mk.fair_up)
            levels = side_levels(mk.book, trade["side"])
            if not levels:
                return f"挂 {cents(price)}，盘口这一边没有卖单：要有人卖到挂价或更低才算成交；现在公平价 {cents(fair)}"
            best = levels[0][0]
            if best > price + 1e-9:
                return (f"挂 {cents(price)}，最低卖价 {cents(best)}（高出 {cents(best - price)}）：要有人卖到挂价或更低才算成交；"
                        f"现在公平价 {cents(fair)}")
            return f"盘口有卖到挂价的卖单，已按看到的数量推定成交；现在公平价 {cents(fair)}"
        if status != "filled":
            return ""
        kind = str(trade.get("kind") or "")
        end = int(s.get("close_ms") or s.get("deadline") or s.get("end") or 0)
        if not end:
            return "等市场出结果"
        settle_at = end + self.SIM_SETTLE_MS
        if now_ms > settle_at:
            check = trade.get("final_check") or {}
            tail = (f"（读取失败：{trade['final_error']}）" if trade.get("final_error")
                    else f"（Predict 市场状态：{check.get('status') or '未知'}）" if check else "")
            if kind == "close":
                return f"收盘（{at(end)}）已过 1 小时，官方收盘还没读到，等 Predict 结算{tail}"
            return f"窗口已结束（{at(end)}），本地数据还定不了结果，等 Predict 结算{tail}"
        if kind == "close":
            return f"等 {str(s.get('target') or '')[5:]} 收盘（{at(end)}）后 1 小时（{at(settle_at)}），按官方收盘预结算，再等 Predict 确认"
        if kind == "touch":
            return f"先碰到哪条线就按那条线结算；都没碰到的话等截止 {at(end)} 后按 50/50（{at(settle_at)} 起核验）"
        if kind == "updown":
            return f"等窗口结束 {at(end)}，按终点 1 分钟 K 结算（{at(settle_at)} 起）"
        if kind == "flip":
            return f"窗口内任一分钟反超即 Yes；否则等窗口结束 {at(end)} 后按 No（{at(settle_at)} 起）"
        if kind in {"range", "ladder"}:
            return f"碰到档位即 Yes；否则等窗口结束 {at(end)} 后按 No（{at(settle_at)} 起）"
        return f"等市场出结果（{at(end)}）"

    def sim_result(self, trade: dict, now_ms: int) -> tuple[float, str, dict] | None:
        """The market's result from the bot's own data, once it is decided: (the 涨 / Yes side's result: 1, 0 or ½ for a
        tie, how it was decided, the evidence). None while undecided or while the data cannot decide it (a window not yet
        read to its end): Predict's own result then settles it."""
        s, kind = trade.get("settle") or {}, trade.get("kind")
        if kind == "close":
            close = self.store.get(f"outcome:{s.get('key')}:{s.get('target')}")
            if close is None or now_ms < int(s.get("close_ms") or 0) + self.SIM_SETTLE_MS:
                return None
            line = float(s["line"])
            up = 1.0 if close > line else 0.0 if close < line else 0.5
            source = (self.store.get(f"outsrc:{s.get('key')}:{s.get('target')}") or {}).get("source", "")
            return up, f"{s['target'][5:]} 收盘 {fmt(close)}，对 {fmt(line)}", {
                "rule": "收盘高于参考线为涨，低于为跌，相同各半", "close": close, "line": line, "source": source,
                "day": s.get("target")}
        if kind == "touch":
            touch = self.touches.get(trade["key"])
            hist = touch.history if touch else {}
            proof = {"rule": "截止前先碰到哪条线；都没碰到 50/50", "source": "币安现货 1 小时 K，命中的那一小时逐分钟",
                     "history": hist}
            if hist.get("kind") in {"low", "high"}:
                return (1.0 if hist["kind"] == "high" else 0.0), touch.status(), proof
            deadline = int(s.get("deadline") or 0)
            if touch and touch.verified_clear() and now_ms > deadline + self.SIM_SETTLE_MS:
                return 0.5, "整个窗口都没碰到两条线，按 50/50", proof  # read through the deadline's own minute
            return None
        if kind == "updown":
            mkt = self.updowns.get(trade["key"])
            odds = mkt.odds(now_ms) if mkt else None
            if isinstance(odds, UpDownOdds) and odds.settled:
                return odds.fair_up, f"终点 {odds.price:,.2f}，起点 {odds.line:,.2f}", {
                    "rule": "终点那根 1 分钟 K 收盘高于起点为涨，低于为跌，相同 50/50", "source": "币安 1 分钟 K",
                    "start": float(odds.line), "end": float(odds.price)}
            return None
        if kind == "flip":
            fm = self.flips.get(trade["key"])
            hist = fm.history if fm else {}
            proof = {"rule": "窗口内任一分钟收盘 A > B 即 Yes", "source": "Hyperliquid 1 分钟 K", "history": hist}
            if hist.get("kind") == "flip":
                return 1.0, fm.status(now_ms), proof
            end = int(s.get("end") or 0)
            if hist.get("kind") == "clear" and int(hist.get("through") or 0) >= end + 60_000 and now_ms > end + self.SIM_SETTLE_MS:
                return 0.0, "窗口内没有反超", proof
            return None
        if kind == "range":
            rm = self.ranges.get(trade["key"])
            if rm is None:
                return None
            level, direction = D(str(s["target"])), s.get("dir", "up")
            marks = rm.marks()
            high, low = marks["high"], marks["low"]
            proof = {"rule": f"窗口内任一 1 分钟 K 的{'最高价 ≥' if direction == 'up' else '最低价 ≤'} {level_label(level)} 即 Yes",
                     "source": rm.extremes_source(),
                     **marks, "through": int(rm.history.get("through") or 0)}
            if rm.reached(level, direction):
                mid = trade["market"].partition("#")[2]
                row = next((r for r in self.predict.ladders.get(rm.spec.key) or [] if r.market_id == mid), None)
                if book_disputes(self.predict.yes_book(row)[0] if row else None):
                    return None  # the book still trades it as open: Predict's own result settles it
                seen = high if direction == "up" else low
                return 1.0, (f"{'↑' if direction == 'up' else '↓'} {level_label(level)} 已触及（{rm.spec.symbol} {rm.range_word}"
                             f"{'最高' if direction == 'up' else '最低'} {seen:,.6g}）"), proof
            if rm.complete() and now_ms > rm.window_end + self.SIM_SETTLE_MS:
                return 0.0, f"整个窗口都没到 {level_label(level)}", proof
            return None
        if kind == "ladder":
            cap = self.caps.get(trade["key"])
            if cap is None:
                return None
            target, mid = D(str(s["target"])), trade["market"].partition("#")[2]
            row = next((r for r in self.predict.ladders.get(cap.spec.key) or [] if r.market_id == mid), None)
            high, high_at = cap.window_high()
            meta = (self.predict.market_meta.get(mid) or ({}, 0))[0]
            proof = {"rule": f"窗口内任一分钟市值 ≥ {usd_short(target)} 即 Yes（以 {cap.spec.settle} 为准）",
                     "source": "GeckoTerminal 小时 K + 机器人看到的价格", "window_high": float(high) if high else None,
                     "high_at": high_at, "coverage": cap.coverage() or "完整"}
            reached = (high is not None and high >= target) or (
                "RESOLVED" in str(meta.get("status", "")).upper() and now_ms < int(s["end"]))
            if reached:
                book = self.predict.yes_book(row)[0] if row else None
                top = max((float(p) for p, _ in (*book.bids[:1], *book.asks[:1])), default=1.0) if book else 1.0
                if top >= 0.9:  # the book agrees, or is gone: reached (an open disagreement waits)
                    return 1.0, f"{usd_short(target)} 已触及（按机器人数据，以 {cap.spec.settle} 为准）", proof
                return None
            if now_ms > int(s["end"]) + self.SIM_SETTLE_MS and not cap.coverage():
                return 0.0, f"窗口结束前没到 {usd_short(target)}（按机器人数据，以 {cap.spec.settle} 为准）", proof
        return None

    def sim_settle(self, trade: dict, up: float, note: str, now_ms: int, by: str) -> None:
        """Settle (or re-settle) a trade on a result: the filled shares are paid, an unfilled rest of a maker order lapses.
        A changed result is kept as a revision (when, by whom, from what to what)."""
        if float(trade["shares"]) <= 1e-9:
            if trade["status"] != "expired":
                trade.update(status="expired", settled=now_ms, note="市场已出结果，挂单一直没成交：" + note)
            return
        payout = sim_payout(trade["side"], up)
        if trade["status"] == "settled" and abs(float(trade.get("payout", payout)) - payout) > 1e-9:
            trade["revisions"].append({"at": now_ms, "by": by, "from": trade["payout"], "to": payout, "note": note})
        if trade.get("maker") and float(trade["shares"]) < float(trade["order"]) - 1e-9:
            trade["unfilled"] = float(trade["order"]) - float(trade["shares"])
        trade.update(status="settled", payout=payout, note=note, settled=trade.get("settled") or now_ms)

    SIM_CONFIRM_SECONDS = 600  # each market's final result is asked of Predict at most this often
    SIM_GIVE_UP_MS = 7 * DAY_MS  # ...and no longer than this after the local settlement

    def sim_due(self, trade: dict, now_ms: int) -> bool:
        """Should Predict have a final result for this trade's market by now (or has the local data decided it)?"""
        if trade.get("final") or trade["status"] in {"expired", "cancelled"} and not float(trade.get("shares") or 0):
            return False
        if (trade.get("final_check") or {}).get("gave_up"):
            return False  # Predict never gave a readable result for this market: the local settlement stands
        if trade.get("local"):
            return True
        s = trade.get("settle") or {}
        end = int(s.get("close_ms") or s.get("deadline") or s.get("end") or 0)
        return bool(end) and now_ms > end + self.SIM_SETTLE_MS

    def resolution_up(self, trade: dict, resolved: dict) -> float | None:
        """Predict's result as the 涨 / Yes side's payout (1, 0, ½), read by outcome name for this kind of market."""
        if resolved.get("split"):
            return 0.5
        name = str(resolved.get("name") or "").strip()
        if trade["kind"] == "touch":
            touch = self.touches.get(trade["key"])
            side = touch_outcome(name, touch.spec) if touch else ""
            return 1.0 if side == "high" else 0.0 if side == "low" else None
        word = name.lower()
        if word in {"yes", "up", "涨", "higher", "above"}:
            return 1.0
        if word in {"no", "down", "跌", "lower", "below"}:
            return 0.0
        if trade["kind"] == "close" and resolved.get("index") in (0, 1):
            return 1.0 if resolved["index"] == 0 else 0.0  # a daily book prices its first outcome as 涨
        return None

    async def sim_confirm(self, trades: dict[str, dict], now_ms: int) -> None:
        """Predict's final word on each decided market: it confirms the local result (已确认), or differs (结果不一致: the
        trade is re-settled on Predict's result, the change kept as a revision), or settles what the local data could
        not. Asked at most every SIM_CONFIRM_SECONDS per market; a failed or unreadable answer leaves it pending."""
        wanted: dict[str, list[str]] = {}
        for tid, trade in trades.items():
            if self.sim_due(trade, now_ms):
                mid = trade.get("market_id") or trade["market"].partition("#")[2]
                if not mid:  # an old record without the market's id: look it up by its slug
                    with contextlib.suppress(Exception):
                        market = await self.predict.resolve(trade.get("slug") or trade["market"])
                        mid = str(market["id"]) if market else ""
                if mid:
                    wanted.setdefault(mid, []).append(tid)
        for mid, tids in wanted.items():
            if time.monotonic() - self.sim_checked.get(mid, -1e9) < self.SIM_CONFIRM_SECONDS:
                continue
            self.sim_checked[mid] = time.monotonic()
            try:
                details = await self.predict.market_details(mid)
            except Exception as error:
                for tid in tids:
                    trades[tid]["final_error"] = clean_error(error) or type(error).__name__
                self.sim_save((tid, trades[tid]) for tid in tids)
                continue
            resolved = details.get("resolved")
            for tid in tids:
                trade = trades[tid]
                trade.pop("final_error", None)
                up = self.resolution_up(trade, resolved) if resolved else None
                if up is None:
                    trade["final_check"] = {"at": now_ms, "status": details.get("status", ""), "resolved": resolved}
                    since = int((trade.get("local") or {}).get("at") or now_ms)
                    if now_ms - since > self.SIM_GIVE_UP_MS:  # a result name the bot cannot read, or a market taken down
                        trade["final_check"]["gave_up"] = True
                        trade["note"] = (trade.get("note") or "") + "；Predict 结果 7 天内无法识别或未公布，已停止核对，以本地预结算为准"
                    self.sim_save([(tid, trade)])
                    continue
                trade["final"] = {"up": up, "name": resolved.get("name", ""), "how": resolved.get("how", ""), "at": now_ms,
                                  "status": details.get("status", ""), "outcomes": details.get("outcomes", [])}
                local = trade.get("local")
                mismatch = bool(local) and abs(float(local["up"]) - up) > 1e-9
                self.sim_settle(trade, up, f"Predict 结算：{resolved.get('name', '')}"
                                + (f"（本地预结算为 {local['note']}）" if mismatch else ""), now_ms, "Predict 最终结果")
                trade["confirm"] = "mismatch" if mismatch else "confirmed"
                self.sim_save([(tid, trade)])

    async def sim_step(self, now_ms: int) -> "Refreshed | bool":
        """Paper trading: whenever a card suggests a trade whose net edge reaches SIM_EDGE_CENTS, buy SIM_SHARES of it.
        The best maker (挂) and the best taker (吃) are judged apart, as the suggestions are, each once per market and
        side. A taker is judged on the very fill it would get for SIM_SHARES (not on the card's PREDICT_TRADE_USD view):
        signal, cost and net edge are one figure. A resting order is placed only on a two-sided book no wider than
        SIM_MAKER_SPREAD, fills only as far as sellers show at or through its price, and is withdrawn once the trader no
        longer places such orders. Every position is pre-settled on the bot's own data, then confirmed (or corrected) by
        Predict's result. Nothing is ever sent to Predict."""
        if time.monotonic() - self.sim_ran < self.SIM_SECONDS:
            return False
        self.sim_ran = time.monotonic()
        self.sim_note_closes()
        markets = {mk.market: mk for mk in self.sim_markets(now_ms)}
        trades = self.sim_trades()
        costs, bar = self.edge_costs(), self.config.sim_edge
        ways, kinds = self.config.sim_ways, self.config.sim_markets
        changed: list[tuple[str, dict]] = []  # written once, in one transaction
        for mk in markets.values():
            if mk.hold or mk.book.stale(now_ms) or mk.kind not in kinds or book_crossed(mk.book):
                continue  # positions filled in other kinds (SIM_MARKETS narrowed) still settle below; resting ones are withdrawn
            maker = (best_edge([e for e in book_edges(mk.fair_up, mk.book, costs) if e.maker], mk.need)
                     if mk.makers and ways != "taker" else None)
            if maker is not None and maker.edge >= bar - 1e-9 and not sim_maker_block(mk.book):
                side = "up" if maker.side == "涨" else "down"
                tid = f"{mk.market}|{side}|挂"
                if tid not in trades:  # one position per market, side and way of trading, however long the edge lasts
                    why = self.sim_group_room(trades, mk, side, maker.price, self.config.sim_shares)
                    if why:
                        self.sim_note_block(tid, mk, "挂" + mk.sides[0 if side == "up" else 1], maker.price, why, now_ms)
                    else:
                        trades[tid] = self.sim_open(mk, side, now_ms, maker=maker)
                        changed.append((tid, trades[tid]))
            bps = mk.book.fee_bps if mk.book.fee_bps is not None else self.config.predict_fee_bps
            quotes = []
            for side in ("up", "down") if ways != "maker" else ():
                q = taker_quote(mk.book, side, self.config.sim_shares, bps)
                fair = mk.fair_up if side == "up" else 1 - mk.fair_up
                # checked on the fill itself; a few dust shares at a stray price are not the trade the edge is about,
                # and would hold the market's one position slot against the real opportunity
                if (q and q["got"] >= self.config.sim_shares * SIM_MIN_FILL - 1e-9
                        and fair - q["cost"] > mk.need and fair - q["cost"] >= bar - 1e-9):
                    quotes.append((round(fair - q["cost"], 4), side, q))
            if quotes:
                _, side, q = max(quotes, key=lambda x: x[0])
                tid = f"{mk.market}|{side}|吃"
                if tid not in trades:
                    why = self.sim_group_room(trades, mk, side, q["cost"], q["got"])
                    if why:
                        self.sim_note_block(tid, mk, "吃" + mk.sides[0 if side == "up" else 1], q["cost"], why, now_ms)
                    else:
                        trades[tid] = self.sim_open(mk, side, now_ms, taker=q)
                        changed.append((tid, trades[tid]))
        opened = {tid for tid, _ in changed}
        for tid, trade in trades.items():
            if trade.get("final") or tid in opened:
                continue  # settled and confirmed: nothing below applies (and no JSON round trip for it every 10 seconds)
            before = json.dumps(trade, sort_keys=True, default=str)
            mk = markets.get(trade["market"])
            if trade["status"] == "resting":
                why = self.sim_withdraw_reason(trade, mk)
                if why:
                    self.sim_withdraw(trade, why, now_ms)
                elif mk is not None and not mk.book.stale(now_ms) and not book_crossed(mk.book):
                    # a crossed snapshot opens nothing (above) and fills nothing either: its "sellers through the price"
                    # are a feed caught mid-update, and a presumed fill is never taken back
                    self.sim_fill(trade, mk, now_ms)
            if mk is not None and not mk.book.stale(now_ms) and not book_crossed(mk.book):
                self.sim_markout(trade, mk, now_ms)
            if not trade.get("final") and (trade["status"] in {"resting", "filled"} or trade.get("confirm") == "local"):
                result = self.sim_result(trade, now_ms)
                local = trade.get("local")
                if result is not None and (local is None or abs(float(local["up"]) - result[0]) > 1e-9):
                    # a first local result, or the local data corrected (e.g. the official close replacing a quote)
                    up, note, proof = result
                    trade["local"] = {"up": up, "note": note, "at": now_ms, "evidence": proof}
                    self.sim_settle(trade, up, note, now_ms, "本地数据更正" if local else "本地预结算")
                    trade["confirm"] = "local"
            if json.dumps(trade, sort_keys=True, default=str) != before:
                changed.append((tid, trade))
        if changed:
            self.sim_save(changed)
        await self.sim_confirm(trades, now_ms)
        return Refreshed("ok")

    def sim_url(self, trade: dict) -> str:
        return predict_url(trade.get("slug") or trade["market"].partition("#")[0], self.config.predict_ref)

    SIM_BLOCK_KEEP_MS = 7 * DAY_MS  # a refused paper buy is remembered this long
    SIM_BLOCK_NOTE_MS = 10 * 60_000  # ...and the same refusal re-recorded (its time) at most this often

    def sim_group_room(self, trades: dict[str, dict], mk: SimMarket, side: str, price: float, shares: float) -> str:
        """"" when a paper position of ``shares`` at ``price`` may be added under SIM_GROUP_USD, else why not: with it,
        the driver's positions (filled shares, plus resting orders as if filled) would lose more than the cap on their
        worst single move. A position on the other side of the same event adds nothing to that; a fourth No on the
        same ladder adds all of itself. The cap is on the event, not on the count of markets."""
        cap = self.config.sim_group_usd
        if not cap:
            return ""
        driver, name = market_driver(mk.kind, mk.key, mk.settle, mk.item)
        mine = []
        for t in trades.values():
            if t.get("status") not in {"filled", "resting"} or trade_driver(t)[0] != driver:
                continue
            held = max(float(t.get("shares") or 0), float(t.get("order") or 0) if t.get("status") == "resting" else 0.0)
            if held > 0:
                mine.append({**t, "shares": held})
        candidate = {"kind": mk.kind, "side": side, "settle": mk.settle, "price": price, "shares": shares}
        worst, event = group_worst_case([*mine, candidate], name)
        if -worst > cap + 1e-9:
            return (f"{event}时这组仓位合计将亏 ${-worst:,.2f}，超过组上限 ${cap:g}"
                    f"（{name} 已有 {len(mine)} 笔，SIM_GROUP_USD 调整）")
        return ""

    def sim_note_block(self, tid: str, mk: SimMarket, label: str, price: float, why: str, now_ms: int) -> None:
        """Remember a paper buy the group cap refused (per driver, the latest per market and side, a week), so the
        journal can say what the rule kept out and what it would have cost."""
        driver, name = market_driver(mk.kind, mk.key, mk.settle, mk.item)
        record = self.store.get(f"simblock:{driver}") or {}
        blocked = {k: v for k, v in (record.get("blocked") or {}).items()
                   if isinstance(v, dict) and now_ms - int(v.get("at") or 0) <= self.SIM_BLOCK_KEEP_MS}
        old = blocked.get(tid)
        if old and now_ms - int(old.get("at") or 0) < self.SIM_BLOCK_NOTE_MS and old.get("why") == why:
            return
        blocked[tid] = {"at": now_ms, "market": mk.market, "item": mk.item, "label": label, "price": price, "why": why,
                        "first": int((old or {}).get("first") or now_ms)}
        if len(blocked) > 20:
            for key in sorted(blocked, key=lambda k: int(blocked[k].get("at") or 0))[:len(blocked) - 20]:
                del blocked[key]
        self.store.put(f"simblock:{driver}", {"name": name, "blocked": blocked})

    def sim_blocks(self) -> list[dict]:
        """Every paper buy the group cap refused in the last week, newest first."""
        now_ms = self.market.now_ms()
        out = []
        for key, record in self.store.items("simblock:"):
            if not isinstance(record, dict):
                continue
            for tid, b in (record.get("blocked") or {}).items():
                if isinstance(b, dict) and now_ms - int(b.get("at") or 0) <= self.SIM_BLOCK_KEEP_MS:
                    out.append({**b, "id": tid, "driver": key.removeprefix("simblock:"), "name": record.get("name", "")})
        return sorted(out, key=lambda b: -int(b.get("at") or 0))

    def sim_markout(self, trade: dict, mk: SimMarket, now_ms: int) -> None:
        """At 1, 5 and 30 minutes after a trade's first fill (the first look at or after each), where the market's
        middle for its side stands against the fill price (the taker's average before fee), and where the model's own
        fair price stands against the one at the fill. A fill the market then moves away from was someone's informed
        exit: markout_stats adds them up."""
        filled = trade.get("filled")
        if not filled or float(trade.get("shares") or 0) <= 0:
            return
        marks = trade.get("markout") if isinstance(trade.get("markout"), dict) else None
        due = [(s, label) for s, label in MARKOUT_HORIZONS if (not marks or label not in marks) and now_ms - int(filled) >= s * 1000]
        if not due:
            return
        mid = side_mid(mk.book, trade["side"])
        if mid is None:
            return
        fair = mk.fair_up if trade["side"] == "up" else 1 - mk.fair_up
        paid = float(trade.get("avg", trade["price"]))
        if marks is None:
            marks = trade["markout"] = {}
        for _, label in due:
            marks[label] = {"at": now_ms, "after_s": (now_ms - int(filled)) // 1000, "mid": mid, "move": mid - paid,
                            "fair": fair, "fair_move": fair - float(trade.get("fill_fair", trade["fair"]))}

    def sim_report(self, recent: int = 30) -> dict:
        """The paper trader's record for the page: totals, by market kind and by maker / taker, the latest trades."""
        trades = sorted(self.sim_trades().items(), key=lambda kv: kv[1].get("opened", 0))
        rows = [{"id": tid, "opened": stamp(t["opened"], seconds=False), "item": t["item"], "label": t["label"],
                 "maker": t["maker"], "price": t["price"], "shares": t["shares"], "order": t.get("order", t["shares"]),
                 "edge": t["edge"], "status": t["status"], "text": sim_status(t), "state": sim_state(t),
                 "pnl": (t["payout"] - t["price"]) * t["shares"] if t["status"] == "settled" else None,
                 "note": t.get("note", ""), "url": self.sim_url(t)}
                for tid, t in reversed(trades[-recent:])]
        values = [t for _, t in trades]
        ways, kinds = sim_scope(self.config)
        return {"edge": self.config.sim_edge, "shares": self.config.sim_shares, "ways": ways, "scope": kinds, "total": sim_stats(values),
                "kinds": [{"name": name, **sim_stats([t for t in values if t["kind"] == kind])}
                          for kind, name in SIM_KINDS.items() if any(t["kind"] == kind for t in values)],
                "modes": [{"name": name, **sim_stats([t for t in values if t["maker"] == maker])}
                          for name, maker in (("挂单", True), ("吃单", False)) if any(t["maker"] == maker for t in values)],
                "groups": sim_groups(values), "sources": sim_sources(values), "blocks": self.sim_blocks(),
                "group_cap": self.config.sim_group_usd, "rows": rows}

    def sim_text(self) -> str:
        r = self.sim_report(recent=10)
        t, edge, shares = r["total"], r["edge"] * 100, f"{r['shares']:g}"
        how = [f"吃单按 {shares} 份吃到的均价和手续费判断并成交"] if r["ways"] != "只挂单" else []
        if r["ways"] != "只吃单":
            how.append("挂单只挂在双边都有报价、价差不超过 10¢ 的盘口，排在已有挂单之后，只有盘口出现卖到挂价或更低的卖单才按看到的数量推定成交，"
                       "出结果时没成交的部分作废；不再做的挂单撤掉")
        lines = [f"🧪 {bold('模拟交易')}（净优势 ≥{edge:g}¢ 时按卡片建议买 {shares} 份，只记账不下单）",
                 f"范围：{r['scope']}；{r['ways']}。" + "；".join(how) + "。先按机器人数据预结算，再以 Predict 的结果确认。"]
        if not t["trades"]:
            lines.append(f"\n还没有触发过：等有卡片的净优势达到 {edge:g}¢ 就开始记录。")
            return "\n".join(lines)
        money = lambda x: f"{'+' if x >= 0 else '−'}${abs(x):,.2f}"
        lines += ["", f"已结算 {t['settled']} 笔：赢 {t['wins']}｜输 {t['losses']}｜平 {t['ties']}"
                      f"（已确认 {t['confirmed']}｜预结算 {t['local']}｜结果不一致 {t['mismatch']}）",
                  f"成本 ${t['cost']:,.2f} → 回款 ${t['payout']:,.2f}｜盈亏 {bold(money(t['pnl']))}"
                  + (f"（{t['roi'] * 100:+.1f}%）" if t["cost"] else ""),
                  f"模型预期：下单时 {money(t['expected'])}｜成交时 {money(t['expected_fill'])}",
                  f"持仓 {t['open']} 笔（成本 ${t['open_cost']:,.2f}）｜挂单中 {t['resting']} 笔｜部分成交 {t['partial']} 笔"
                  f"｜未成交作废 {t['expired']} 笔" + (f"｜撤单 {t['cancelled']} 笔" if t["cancelled"] else "")]
        for group in (r["kinds"], r["modes"]):
            if len(group) > 1:
                lines.append("｜".join(f"{g['name']} {g['settled']} 笔 {money(g['pnl'])}" for g in group))
        lines.append("｜".join(f"{label} {s['settled']} 笔 {money(s['pnl'])}（预期 {money(s['expected'])}）"
                               for label, s in self.sim_recent_stats().items()))  # is the model holding up lately?
        lines.extend(self.sim_risk_lines(r))
        lines.append("\n最近：")
        lines += [f"{x['opened']} {x['item']} {x['label']} {x['price'] * 100:.1f}¢×{x['shares']:g} → {x['text']}"
                  + (f"·{x['state']}" if x["state"] else "") for x in r["rows"]]
        if self.config.web_port and self.web_token:
            lines.append(f"\n完整复盘（每笔的判断依据、来源、成交与结算证据，可导出）：{self.web_url()}/journal")
        return "\n".join(lines)

    def sim_risk_lines(self, r: dict) -> list[str]:
        """The common-risk view of the open positions and the markouts of the fills, for /sim: one event can hold many
        markets' money; a fill the market then runs away from was someone's informed exit."""
        money = lambda x: f"{'+' if x >= 0 else '−'}${abs(x):,.2f}"
        lines = []
        held = [g for g in r["groups"] if g["positions"]]
        if held:
            top = held[0]
            lines.append(f"🧩 最坏单一事件：{top['event']} → {money(top['worst'])}（{top['name']} {top['positions']} 笔，"
                         f"占持仓成本 {top['share'] * 100:.0f}%）")
            rest = [f"{g['name']} {g['positions']} 笔 ${g['cost']:,.0f} → {g['event']} {money(g['worst'])}" for g in held[1:4]]
            if rest:
                lines.append("　其他组：" + "｜".join(rest) + ("…" if len(held) > 4 else ""))
        if r["sources"]:
            lines.append("📡 持仓依赖的数据源：" + "｜".join(f"{x['source']} {x['trades']} 笔 ${x['cost']:,.0f}" for x in r["sources"][:5])
                         + ("…" if len(r["sources"]) > 5 else ""))
        if r["blocks"]:
            b = r["blocks"][0]
            lines.append(f"⛔ 组上限 ${r['group_cap']:g} 一周内拦下 {len(r['blocks'])} 笔；最近 {stamp(b['at'], seconds=False)} "
                         f"{b['item']} {b['label']} @ {cents(b['price'])}：{b['why']}")
        marks = r["total"].get("markout") or {}
        if marks:
            lines.append("📈 成交后市场走向（盘口中间价 − 成交价，每份）：" + "｜".join(
                f"{label.replace('m', ' 分钟')} {cents(marks[label]['avg'], True)}（{marks[label]['n']} 笔）"
                for _, label in MARKOUT_HORIZONS if label in marks) + "；持续为负 = 挂单常被更快的人吃掉旧报价")
        return lines

    def sim_recent_stats(self) -> dict[str, dict]:
        """{'今天': stats, '近 7 天': stats} over the trades settled in those windows (Beijing days): the lifetime total
        cannot say whether the model has stopped working lately."""
        now_ms = self.market.now_ms()
        today = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
        windows = {"今天": int(today.timestamp() * 1000), "近 7 天": int((today - dt.timedelta(days=6)).timestamp() * 1000)}
        trades = [t for t in self.sim_trades().values() if t["status"] == "settled" and t.get("settled")]
        return {label: sim_stats([t for t in trades if int(t["settled"]) >= since]) for label, since in windows.items()}

    def journal_payload(self) -> dict:
        """Every paper trade with its whole record (entry and fill snapshots, fills, local and final settlement,
        revisions, version), newest first: the review page and the JSON export."""
        now_ms = self.market.now_ms()
        trades = sorted(self.sim_trades().items(), key=lambda kv: kv[1].get("opened", 0), reverse=True)
        values = [t for _, t in trades]
        markets: dict[str, SimMarket] = {}
        if any(t["status"] in {"resting", "filled"} for t in values):
            try:
                markets = {mk.market: mk for mk in self.sim_markets(now_ms)}
            except Exception as error:  # the journal is still served; open trades just cannot say where the book stands
                LOG.warning("journal: markets unavailable: %s", clean_error(error) or type(error).__name__)
        rows = [{**t, "id": tid, "driver_name": trade_driver(t)[1], "text": sim_status(t), "state": sim_state(t),
                 "pnl": (t["payout"] - t["price"]) * t["shares"] if t["status"] == "settled" else None,
                 "expected_fill": sim_fill_expectation(t) if float(t.get("shares") or 0) else 0.0, "url": self.sim_url(t),
                 "wait": self.sim_wait(t, markets.get(t["market"]), now_ms)}
                for tid, t in trades]
        return {"version": VERSION, "server_ms": now_ms, "generated_at": stamp(now_ms) + "（北京时间）",
                "enabled": self.config.sim and self.config.predict, "edge": self.config.sim_edge,
                "shares": self.config.sim_shares, "ways": sim_scope(self.config)[0], "scope": sim_scope(self.config)[1],
                "total": sim_stats(values),
                "kinds": [{"key": kind, "name": name, **sim_stats([t for t in values if t["kind"] == kind])}
                          for kind, name in SIM_KINDS.items() if any(t["kind"] == kind for t in values)],
                "modes": [{"name": name, **sim_stats([t for t in values if t["maker"] == maker])}
                          for name, maker in (("挂单", True), ("吃单", False)) if any(t["maker"] == maker for t in values)],
                "groups": sim_groups(values), "sources": sim_sources(values), "blocks": self.sim_blocks(),
                "group_cap": self.config.sim_group_usd, "trades": rows}

    JOURNAL_COLUMNS = ("编号", "下单时间", "市场", "类型", "方向", "挂/吃", "下单份数", "成交份数", "成本价", "成交均价", "手续费",
                       "最优价", "滑点", "下单时公平价", "下单时净优势", "下单时预期", "成交时预期", "状态", "确认", "本地结果",
                       "Predict 结果", "结果不一致", "回款/份", "盈亏", "结算时间", "说明", "修订", "参考线", "有效价", "σ（日）",
                       "剩余方差占比", "β", "口径", "下单距收盘（小时）", "最旧报价年龄（秒）", "行情来源", "代理与锚点", "版本", "市场链接",
                       "共同风险组", "成交后1分钟市价变动", "成交后5分钟市价变动", "成交后30分钟市价变动")

    def journal_csv(self) -> str:
        """The journal as one row per trade (UTF-8 with BOM, so spreadsheet apps read the Chinese), for analysis."""
        import csv
        import io
        when = lambda ms: dt.datetime.fromtimestamp(ms / 1000, BEIJING).strftime("%Y-%m-%d %H:%M:%S") if ms else ""
        result = lambda r: "" if not r else {1.0: "涨/Yes", 0.0: "跌/No", 0.5: "50/50"}.get(float(r["up"]), str(r["up"]))
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(self.JOURNAL_COLUMNS)
        for t in self.journal_payload()["trades"]:
            entry = t.get("entry") or {}
            sources = "；".join(f"{x.get('what', '')}={x.get('source', '')}"
                               + (f" {x['symbol']}" if x.get("symbol") else "")
                               + (f" @{when(x['quoted_ms'])}" if x.get("quoted_ms") else "")
                               for x in entry.get("sources") or [])
            proxy = entry.get("proxy") or {}
            proxy_text = (f"{proxy.get('proxy', '')}；锚点 {proxy.get('anchor')}（{proxy.get('anchor_note', '')}）"
                          + ("；近似" if proxy.get("approx") else "")) if proxy else ""
            final = t.get("final")
            basis = entry.get("basis") or {}
            ages = [(entry.get("at", t["opened"]) - x["quoted_ms"]) / 1000 for x in entry.get("sources") or [] if x.get("quoted_ms")]
            left = (basis["close_ms"] - t["opened"]) / 3_600_000 if basis.get("close_ms") else ""
            writer.writerow([
                t["id"], when(t["opened"]), t["item"], SIM_KINDS.get(t["kind"], t["kind"]), t["label"],
                "挂单" if t["maker"] else "吃单", t.get("order", t["shares"]), t["shares"], round(t["price"], 6),
                round(t.get("avg", t["price"]), 6), round(t.get("fee", 0.0), 6), round(t.get("best", t["price"]), 6),
                round(t.get("slip", 0.0), 6), round(t["fair"], 6), round(t["edge"], 6), round(t["edge"] * t["shares"], 4),
                round(t["expected_fill"], 4), t["text"], t["state"], result(t.get("local")),
                (result(final) + f"（{final.get('name', '')}）") if final else "", "是" if t.get("confirm") == "mismatch" else "",
                t.get("payout", ""), round(t["pnl"], 4) if t["pnl"] is not None else "", when(t.get("settled") or 0),
                t.get("note", ""), "；".join(f"{when(r['at'])} {r['by']}：{r['from']}→{r['to']}" for r in t.get("revisions") or []),
                basis.get("ref", ""), basis.get("effective", ""), basis.get("sigma_daily", ""), basis.get("remaining", ""),
                basis.get("beta", ""), basis.get("mode", ""), round(left, 2) if left != "" else "", round(max(ages), 1) if ages else "",
                sources or ("旧记录：无快照" if t.get("legacy") else ""), proxy_text,
                (t.get("version") or {}).get("code", "") or ("旧版" if t.get("legacy") else ""), t["url"],
                trade_driver(t)[1], *(round(float(marks[label]["move"]), 6) if isinstance(marks.get(label), dict) else ""
                                     for marks in [t.get("markout") if isinstance(t.get("markout"), dict) else {}]
                                     for _, label in MARKOUT_HORIZONS)])
        return "\ufeff" + out.getvalue()

    def cmd_sim(self, req: Request) -> "Reply":
        if not self.config.sim or not self.config.predict:
            return Reply("模拟交易已关闭（SIM=off 或 PREDICT=off）。")
        return Reply(self.sim_text(), html=True)

    def cap_payload(self, cap: "CapMarket", now_ms: int) -> dict:
        """Web card for a market-cap ladder: per threshold the model's P(Yes), the Yes book and its best edge."""
        spec = cap.spec
        start = dt.datetime.fromtimestamp(spec.start_ms / 1000, dt.timezone(dt.timedelta(hours=us_eastern_offset(spec.start_ms))))
        end = dt.datetime.fromtimestamp(spec.end_ms / 1000, dt.timezone(dt.timedelta(hours=us_eastern_offset(spec.end_ms))))
        bj = lambda ms: dt.datetime.fromtimestamp(ms / 1000, BEIJING).strftime("%m-%d %H:%M")
        rows_in = self.predict.ladders.get(spec.key) or [LadderRow(t, "", "", None, "") for t in spec.targets]
        rows = []
        for row in rows_in:
            fair = self.ladder_fair(cap, row, now_ms)
            book, why = self.predict.yes_book(row) if row.market_id else (None, "")
            out: dict[str, Any] = {"label": usd_short(row.target), "fair": fair, "error": why,
                                   "dist": float(row.target / cap.cap - 1) if cap.cap else None, "settled": self.cap_settled(row)}
            if book is not None:
                out.update(bid=float(book.bid[0]) if book.bid else None, ask=float(book.ask[0]) if book.ask else None)
                if fair == 1.0 and book_disputes(book):
                    # our history says touched, the market does not: sources disagree, so no "sure thing" edge
                    out["error"] = "数据显示已触及，但盘口仍低于 90¢；以 Flap.sh 为准，请核实"
                priced = fair is not None and not out["error"]
                swing = cap.model_swing(row.target, now_ms, fair) if priced else 0.0
                # a prior σ or a finished window: the edges are shown, never suggested (the page greys the closest one)
                hold = ("窗口已结束，等待结算" if now_ms >= spec.end_ms + 60_000
                        else "σ 是先验值，只作参考" if cap.sigma_kind == "prior" else "")
                self.book_block(out, book, fair if priced else None, self.edge_need(swing), swing, hold, ("Yes", "No"), now_ms)
            # reached, and the book agrees (settled, gone, or ≥ 90¢): folded into one "已触及" line on the card
            out["touched"] = fair == 1.0 and "请核实" not in out["error"]
            rows.append(out)
        high, high_at = cap.window_high()
        item: dict[str, Any] = {
            "name": spec.name, "symbol": spec.key, "group": "ladder", "kind": "ladder", "close_ms": spec.end_ms,
            "quote_ms": cap.priced_ms if cap.price is not None else 0, "source": cap.source or "",
            "close_label": f"{end:%m-%d %H:%M} ET（北京 {bj(spec.end_ms)}）截止" + (f"；{spec.trade_end}" if spec.trade_end else ""),
            "ladder": {"cap": usd_short(cap.cap), "high": usd_short(high), "high_at": stamp(high_at * 1000, seconds=False) if high_at else "",
                       "cap_usd": float(cap.cap) if cap.cap is not None else None, "high_usd": float(high) if high is not None else None,
                       "monitored_from": (stamp(int(cap.history.get("monitored_from") or 0) * 1000, seconds=False)
                                          if cap.history.get("monitored_from") else ""),
                       "sigma": cap.sigma, "sigma_note": cap.sigma_note, "rows": rows, "error": cap.error,
                       "price": f"{cap.price:.10g}" if cap.price is not None else "—", "source": cap.source or "—",
                       "supply": fmt(cap.supply.quantize(D(1))) if cap.supply else "—",
                       "window": f"{start:%m-%d %H:%M} ET（北京 {bj(spec.start_ms)}）起",
                       "years": max(0.0, (spec.end_ms - max(now_ms, spec.start_ms)) / YEAR_MS),
                       "first_skipped": cap.history.get("first") == "skipped", "gaps": cap.gaps_note(),
                       "coverage": cap.backfill_note(now_ms), "pool": cap.pool if spec.gecko else "", "spike": cap.spike_note(),
                       "pool_note": cap.pool_note(),
                       "metric": spec.metric, "settle": spec.settle, "bars": bool(spec.gecko),
                       "sigma_kind": cap.sigma_kind, "vol_error": cap.vol_error,
                       "supply_note": "总量 − 销毁" if spec.supply == "rpc" else f"DexScreener {spec.metric} ÷ 价格"},
        }
        if self.config.predict:
            item["predict"] = {"url": predict_url(spec.slug, self.config.predict_ref), "error": self.predict.errors.get(spec.key, "")}
        if not rows and self.config.predict:
            item["ladder"]["waiting"] = "等待 Predict 档位"  # a spec without levels of its own: they come from Predict's market titles
        if cap.cap is None or cap.sigma is None:
            item["missing"] = f"等待市值数据（{brief_error(cap.error, 80)}）" if cap.error else "等待市值数据"
        elif cap.input_problem(now_ms):
            item["missing"] = cap.input_problem(now_ms)  # levels already reached stay settled; the rest wait
        return item

    async def binance_futures(self, path: str, **params: Any) -> Any:
        """GET /fapi/v1/<path> through the bot's own Binance futures feed (BINANCE_BASE_URL, shared rate-limit cooldown)."""
        return await self.market.get(f"/fapi/v1/{path}", **params)

    def range_level(self, rm: RangeMarket, row: LadderRow) -> tuple[str, str]:
        """(direction, where it was read) for one level: the market's own rules, else its title or question; failing
        both it is guessed ("推断": shown, never suggested) from the side of the window's first price the level sits on
        (a ↓ level above it would have been reached at once), or, before any price, the category's default ("默认")."""
        meta = (self.predict.market_meta.get(row.market_id) or ({}, 0))[0] if row.market_id else {}
        direction, source = level_direction(meta.get("rules", ""), row.title, row.question or meta.get("question", ""))
        if direction:
            return direction, source
        ref = rm.reference()
        return ("up" if float(row.target) > ref else "down", "推断") if ref else (rm.spec.default_dir, "默认")

    @staticmethod
    def range_guess(source: str) -> str:
        """Why a level's direction is only a guess, or disputed ("" when its market says it)."""
        return RANGE_GUESS.get(source, "")

    def range_settled(self, row: LadderRow) -> bool:
        """Predict has settled this level's market (nothing is suggested on it any more)."""
        meta = (self.predict.market_meta.get(row.market_id) or ({}, 0))[0] if row.market_id else {}
        return "RESOLVED" in str(meta.get("status", "")).upper()

    def points_status(self, market_id: str, book: PredictBook | None, now_ms: int, stale_s: float) -> dict:
        """A market's LP points as its card shows them: the programme (points_active: paying now, points_rate per hour)
        and whether a quote placed now would earn (points_ok), else why not (points_why: Predict's own requirements —
        trading open, a two-sided book, the spread at or under the market's cap). Unknown or stale rewards fail closed.
        A one-sided book must not turn an isolated 99.9¢ ask into a supposedly profitable 0.1¢ maker quote."""
        cached = self.predict.market_meta.get(market_id) if market_id else None
        meta = cached[0] if cached else {}
        status = predict_reward_status(meta, now_ms)
        if cached is None or time.monotonic() - cached[1] >= stale_s:
            status.update(points_active=None, points_note="积分状态已过期" if cached else "积分状态暂缺")
        elif market_id in self.predict.meta_errors:
            status.update(points_active=None, points_note="积分状态刷新失败")
        reason = status["points_note"] if status["points_active"] is not True else ""
        if not reason and str(meta.get("trading_status", "")).upper() != "OPEN":
            reason = "市场暂停交易" if meta.get("trading_status") else "市场交易状态暂缺"
        if not reason and (book is None or not book.bid or not book.ask):
            reason = "缺少双边盘口，挂单暂不建议"
        if not reason and book.bid[0] >= book.ask[0]:
            reason = "盘口交叉，挂单暂不建议"
        threshold = None
        with contextlib.suppress(decimal.InvalidOperation, ValueError, TypeError):
            value = D(str(meta.get("spread_threshold")))
            if value.is_finite() and 1 <= value <= 100:
                value = value / 100  # stated in cents (a cap of "1" can only mean 1¢: a 100% spread caps nothing)
            if value.is_finite() and 0 < value <= 1:
                threshold = value
        if not reason and threshold is None:
            reason = "积分价差要求暂缺，挂单暂不建议"
        if not reason and book.ask[0] - book.bid[0] > threshold:  # Predict's "最大价差": a spread right at the cap still earns
            reason = f"价差 {cents(float(book.ask[0] - book.bid[0]))} 超过积分上限 {cents(float(threshold))}"
        if not reason and book.stale(now_ms):
            reason = "盘口已过期，挂单暂不建议"
        return {**status, "points_ok": not bool(reason), "points_why": reason,
                "points_spread": float(threshold) if threshold is not None else None,
                "points_min_shares": meta.get("share_threshold")}

    def range_maker_status(self, row: LadderRow, book: PredictBook | None, now_ms: int) -> dict:
        """The UI and alerts use the same price-ladder maker gate: a maker is suggested only where its quote would earn
        points now; the level's reward metadata is read every minute and trusted for PREDICT_REWARD_STALE_SECONDS."""
        status = self.points_status(row.market_id, book, now_ms, PREDICT_REWARD_STALE_SECONDS)
        return {**status, "makers": status["points_ok"], "maker_note": status["points_why"]}

    def range_fair(self, rm: RangeMarket, row: LadderRow, direction: str, now_ms: int) -> float | None:
        """P(Yes) for one level as its card shows it. A market Predict settled before the window closed: its own result
        when readable, else reached (only a touch settles one early)."""
        if self.range_settled(row) and now_ms < rm.window_end:
            resolved = (self.predict.market_meta.get(row.market_id) or ({}, 0))[0].get("resolved")
            up = self.resolution_up({"kind": "range", "key": rm.spec.key}, resolved) if resolved else None
            return 1.0 if up is None else up
        return rm.probability(row.target, direction, now_ms)

    def range_payload(self, rm: RangeMarket, now_ms: int) -> dict:
        """Web card for a price ladder: per level its direction, the model's P(Yes), the Yes book and its best edge."""
        spec, hist = rm.spec, rm.history
        problem = rm.advice_problem(now_ms)
        rows = []
        for row in self.predict.ladders.get(spec.key) or []:
            direction, source = self.range_level(rm, row)
            fair = self.range_fair(rm, row, direction, now_ms)
            book, why = self.predict.yes_book(row) if row.market_id else (None, "")
            out: dict[str, Any] = {"label": ("↑ " if direction == "up" else "↓ ") + level_label(row.target), "dir": direction,
                                   "dir_source": source, "dir_note": self.range_guess(source), "level": float(row.target),
                                   "fair": fair, "error": why, "settled": self.range_settled(row),
                                   "dist": float(row.target / rm.price - 1) if rm.price else None}
            out.update(self.range_maker_status(row, book, now_ms))
            if book is not None:
                out.update(bid=float(book.bid[0]) if book.bid else None, ask=float(book.ask[0]) if book.ask else None)
                if fair == 1.0 and book_disputes(book):
                    # our candles say reached, the market does not: no "sure thing" edge until someone looks
                    out["error"] = "数据显示已触及，但盘口仍低于 90¢；以币安 1 分钟 K 为准，请核实"
                priced = fair is not None and not out["error"]
                swing = rm.model_swing(row.target, direction, now_ms, fair) if priced else 0.0
                hold = problem or ("Predict 已结算" if self.range_settled(row) else "") or self.range_guess(source)
                self.book_block(out, book, fair if priced else None, self.edge_need(swing), swing, hold, ("Yes", "No"), now_ms,
                                makers=out["makers"])
            out["touched"] = fair == 1.0 and "请核实" not in out["error"]
            rows.append(out)
        rows.sort(key=lambda r: -r["level"])  # high to low: the price sits between the ↑ and the ↓ levels
        if isinstance(rm, StockRangeMarket):
            rm.level_names = [r["label"].split(" ", 1)[-1] for r in rows]  # "$100", for the card's question line
        marks = rm.marks()
        high, low = marks["high"], marks["low"]
        price_text = lambda v: f"${v:,.2f}" if v is not None and v < 1000 else f"${v:,.0f}" if v is not None else "—"
        item: dict[str, Any] = {
            "name": spec.name, "symbol": spec.key, "group": "levels", "kind": "ladder", "close_ms": rm.end_ms,
            "quote_ms": rm.priced_ms if rm.price is not None else 0,
            "source": rm.source_name(),
            "close_label": rm.close_label(),
            "ladder": {"kind": "price", "metric": "价格", "price": price_text(float(rm.price) if rm.price is not None else None),
                       "spot": float(rm.price) if rm.price is not None else None,  # the page draws its line among the levels
                       "high": price_text(high), "low": price_text(low),
                       "high_at": stamp(marks["high_at"], seconds=False) if marks["high_at"] else "",
                       "low_at": stamp(marks["low_at"], seconds=False) if marks["low_at"] else "",
                       "through": stamp(int(hist["through"]), seconds=False) if hist.get("through") else "",
                       "sigma": rm.sigma, "sigma_note": rm.sigma_note(), "rows": rows, "error": rm.error, "hold": problem,
                       "symbol": spec.symbol, "venue": rm.venue_name(),
                       "window": rm.window_label(),
                       "years": rm.remaining_years(now_ms), **rm.card_extras(now_ms)},
        }
        if self.config.predict:
            item["predict"] = {"url": predict_url(spec.slug, self.config.predict_ref), "error": self.predict.errors.get(spec.key, "")}
        if missing := rm.missing_note(now_ms):
            item["missing"] = missing
        if not rows and self.config.predict:
            item["ladder"]["waiting"] = "等待 Predict 档位"  # the Predict line of the card says why (not listed yet, an error)
        return item

    def item_market(self, title: str) -> str | None:
        """The exchange an odds item follows: hk / kr / sh for the indices, the ticker's market for contracts."""
        market = {"恒生指数": "hk", "KOSPI": "kr", "上证指数": "sh"}.get(title)
        ticker = self.config.tickers.get(title.split("｜")[-1])
        return market or (ticker.market if ticker else None)

    AUCTION_JOIN_MS = 2 * 60_000   # a reminder is (re)tried only this soon after the auction opens
    AUCTION_FRESH_MS = 60_000      # a reminder older than this when its turn to send comes is rebuilt, not sent

    EDGE_ALERT_SECONDS = 10              # the edge alerts look at the cards this often (the books refresh every 15 s)
    EDGE_ALERT_FRESH_MS = 10 * 60_000    # an announcement is sent (a failed send retried) this long, unless a newer one
    #                                      for its market replaces it

    async def edge_alerts(self, now_ms: int) -> "Refreshed | bool":
        """Telegram, for every Predict market a card prices: its suggestion reaching EDGE_ALERT_CENTS (新机会); later the
        announced side no longer suggested at all (建议失效) or the other side suggested instead (方向反转), both with a
        reminder to check any order placed on it. Each change has to hold EDGE_ALERT_CONFIRM_SECONDS; a side announced
        as new is not announced as new again for EDGE_ALERT_COOLDOWN_SECONDS. Existing streams freeze on a stale book
        or a card that holds back; price-ladder maker streams separately withdraw when points / quote eligibility is
        lost. A subscription is recorded once Telegram accepted its copy; a failed send is retried while the
        announcement is young and still the market's latest."""
        if time.monotonic() - self.edge_ran < self.EDGE_ALERT_SECONDS:
            return False
        self.edge_ran = time.monotonic()
        state = self.store.get("edgealerts", {})
        state = state if isinstance(state, dict) else {}
        before = json.dumps(state, sort_keys=True)
        costs = self.edge_costs()
        bars, _ = self.edge_bars()  # read once a round, not once a market
        observed_makers = set()
        for mk in self.sim_markets(now_ms):
            bar = self.edge_bar(mk, bars)  # the level's, market's or section's own bar (/edge), else EDGE_ALERT_CENTS
            self.edge_observe(state, mk.market, mk, costs, bar, now_ms)
            if mk.kind == "range":
                # Its own state keeps a same-side taker alert from hiding a new maker opportunity (and vice versa).
                maker_key = mk.market + "|maker"
                observed_makers.add(maker_key)
                self.edge_observe(state, maker_key, mk, costs, bar, now_ms, maker_only=True)
        for key, st in state.items():
            if key.endswith("|maker") and key not in observed_makers:
                st["pending"] = None  # missing model / book breaks the continuous confirmation period
                if (st.get("alert") or {}).get("kind") in {"appear", "flip"}:
                    st.pop("alert", None)
        for market in [k for k, st in state.items() if now_ms - int(st.get("seen", 0)) > DAY_MS]:
            del state[market]  # no card has priced it for a day (it ended): forgotten
            self.store.delete_prefix(f"edgesent:{market}:")
        if json.dumps(state, sort_keys=True) != before:
            self.store.put("edgealerts", state)
        self.edge_deliver(state, now_ms)
        return Refreshed("ok")

    def edge_observe(self, state: dict, market: str, mk: SimMarket, costs: EdgeCosts, bar: float, now_ms: int,
                     maker_only: bool = False) -> None:
        """Observe one alert stream. Existing streams keep their original gate; range makers have separate history."""
        st = state.get(market, {})
        edges, sides, best = [], None, None
        available = not mk.hold and not mk.book.stale(now_ms)
        if maker_only and (not mk.maker_alerts or not available):
            # Stop retrying a queued new opportunity immediately. A previously sent one gets the confirmed
            # withdrawal reminder, including when points turn off but the same-side taker remains profitable.
            if (st.get("alert") or {}).get("kind") in {"appear", "flip"}:
                st.pop("alert", None)
            sides = {"up": None, "down": None}
        elif available:
            all_edges = book_edges(mk.fair_up, mk.book, costs)
            edges = ([e for e in all_edges if e.maker] if maker_only
                     else [e for e in all_edges if mk.makers or not e.maker])
            best = best_edge(edges, mk.need)
            sides = {key: best_edge([e for e in edges if e.side == side], mk.need)
                     for key, side in (("up", "涨"), ("down", "跌"))}
            old_alert = st.get("alert") or {}
            if maker_only and old_alert.get("kind") == "gone" and sides.get(old_alert.get("side")) is not None:
                st.pop("alert", None)  # restored eligibility cancels a queued withdrawal, even during cooldown
        note = st.get("note")
        change = edge_watch(st, sides, best, now_ms, bar, self.config.edge_alert_confirm * 1000,
                            self.config.edge_alert_cooldown * 1000)
        if change:
            kind, side = change.split(":")
            st["note"] = self.edge_note(mk, sides[st["told"]], now_ms) if st["told"] else None
            st["alert"] = {"seq": st["seq"], "at": now_ms, "kind": kind, "side": side, "market": mk.market, "item": mk.item,
                           "driver": market_driver(mk.kind, mk.key, mk.settle, mk.item)[1],
                           "url": predict_url(mk.book.slug or mk.market.partition("#")[0], self.config.predict_ref),
                           "text": self.edge_alert_text(kind, side, mk, edges, sides, best, note, maker_only, bar)}
            if kind == "appear" and best is not None:
                st["alert"]["offer"] = {**self.edge_note(mk, best, now_ms), "fair": mk.fair_up if best.side == "涨" else 1 - mk.fair_up}
            if maker_only:
                st["alert"].update(maker_only=True, note=note)
        if st.get("told") or st.get("pending") or st.get("alert") or any(now_ms - t < DAY_MS for t in st.get("last", {}).values()):
            st["seen"] = now_ms - now_ms % 3_600_000
            state[market] = st
        else:
            state.pop(market, None)

    def edge_maker_fresh(self, alert: dict, now_ms: int) -> bool:
        """Recheck a queued price-ladder maker recommendation at send time, including points and book freshness."""
        if not alert.get("maker_only"):
            return True
        mk = next((mk for mk in self.sim_markets(now_ms) if mk.market == alert.get("market")), None)
        edges = (book_edges(mk.fair_up, mk.book, self.edge_costs())
                 if mk is not None and mk.maker_alerts and not mk.hold and not mk.book.stale(now_ms) else [])
        current = next((e for e in edges if e.maker and (e.side == "涨") == (alert.get("side") == "up")
                        and e.edge > mk.need), None)
        if alert.get("kind") == "gone":
            return current is None
        return current is not None and (alert.get("kind") != "appear" or current.edge >= self.edge_bar(mk) - 1e-9)

    def edge_maker_text(self, alert: dict, now_ms: int) -> str:
        """Rebuild a confirmed maker announcement from today's quote, retaining its event and previous note."""
        mk = next((mk for mk in self.sim_markets(now_ms) if mk.market == alert.get("market")), None)
        if mk is None:
            lines = alert["text"].splitlines()
            return "\n".join([lines[0], "档位数据暂缺，挂单建议暂停，请检查是否撤单或改价", lines[-1]])
        edges = ([e for e in book_edges(mk.fair_up, mk.book, self.edge_costs()) if e.maker]
                 if mk.maker_alerts and not mk.hold and not mk.book.stale(now_ms) else [])
        sides = {key: best_edge([e for e in edges if e.side == side], mk.need)
                 for key, side in (("up", "涨"), ("down", "跌"))}
        # An appearance was confirmed on this side: a wider spread must not switch its direction at send time.
        best = sides[alert["side"]]
        return self.edge_alert_text(alert["kind"], alert["side"], mk, edges, sides, best, alert.get("note"), True)

    @staticmethod
    def edge_note(mk: SimMarket, edge: BookEdge, now_ms: int) -> dict:
        """What an announcement told, for the follow-up that may come later."""
        return {"label": ("挂" if edge.maker else "吃") + mk.sides[0 if edge.side == "涨" else 1], "price": edge.price,
                "edge": edge.edge, "at": now_ms}

    def edge_alert_text(self, kind: str, side: str, mk: SimMarket, edges: list[BookEdge], sides: dict, best: BookEdge | None,
                        note: dict | None, maker_only: bool = False, bar: float | None = None) -> str:
        """新机会 (side: the suggested one), 方向反转 (side: the one suggested now) or 建议失效 (side: the one announced)."""
        default = self.edge_default()
        bar = default if bar is None else bar
        name = mk.item + (f"（{mk.settle['target'][5:]}）" if mk.kind == "close" and mk.settle.get("target") else "")
        label = lambda e: ("挂" if e.maker else "吃") + mk.sides[0 if e.side == "涨" else 1]
        fair = lambda e: mk.fair_up if e.side == "涨" else 1 - mk.fair_up
        offer = lambda e: f"{bold(label(e))} @ {cents(e.price)}｜净优势 {bold(cents(e.edge, True))}（模型 {cents(fair(e))}）"
        url = predict_url(mk.book.slug or mk.market.partition("#")[0], self.config.predict_ref)
        told = (f"之前提醒：{note['label']} @ {cents(note['price'])} {cents(note['edge'], True)}（{hhmm(note['at'])}）"
                if note else "")
        check = "⚠️ 如果按之前的提醒挂了单，请检查是否撤单或改价"
        if kind == "appear":
            more = [e for e in edges if e is not best and e.edge > mk.need and e.edge >= bar - 1e-9]
            lines = [f"🟢 {bold('新机会')}｜{name}", offer(best),
                     "挂单：排队等成交，不保证成交" if best.maker
                     else f"吃单：按 ${self.config.predict_trade_usd:g} 计，已扣手续费和盘口深度",
                     "积分已激活；优势仅按模型与挂单价计算，不含积分收益" if maker_only else "",
                     ("也可以：" + "；".join(f"{label(e)} @ {cents(e.price)} {cents(e.edge, True)}" for e in more)) if more else "",
                     f"（这个市场的提醒门槛 {cents(bar)}，/edge 设置）" if abs(bar - default) > 1e-9 else ""]
        elif kind == "flip":
            lines = [f"🔄 {bold('方向反转')}｜{name}", told, "现在建议另一边：" + offer(sides[side]), check]
        else:
            near = max((e for e in edges if (e.side == "涨") == (side == "up")), key=lambda e: e.edge, default=None)
            lines = [f"⚪ {bold('建议失效')}｜{name}", told,
                     ("挂单建议暂停：" + (mk.maker_note or mk.hold or "盘口已过期")) if maker_only and not edges else
                     (f"现在这一边最好是 {label(near)} {cents(near.edge, True)}，" if near else "现在这一边没有挂单可比，")
                     + f"不到建议门槛 {cents(mk.need)}，不再建议", check]
        return "\n".join(x for x in [*lines, url] if x)

    def edge_deliver(self, state: dict, now_ms: int) -> None:
        """Each market's latest announcement to every active subscription that has not got it yet."""
        subs = [(sub_id, sub) for sub_id, sub in self.subscriptions().items() if sub.get("active")]
        digest = self.config.edge_alert_digest
        for market, st in state.items():
            alert = st.get("alert")
            if not alert or now_ms - int(alert["at"]) > self.EDGE_ALERT_FRESH_MS:
                continue
            if digest and alert.get("kind") == "appear":
                continue  # gathered into the digest below: 建议失效 / 方向反转 still go one by one, they protect an order
            for sub_id, sub in subs:
                key = f"edgesent:{market}:{sub_id}"
                if int(self.store.get(key) or 0) >= alert["seq"] or self.delivering(key):
                    continue

                prepared: dict = {}

                def fresh(market: str = market, seq: int = alert["seq"], at: int = alert["at"], prepared: dict = prepared) -> bool:
                    latest = ((self.store.get("edgealerts", {}) or {}).get(market) or {}).get("alert") or {}
                    current = self.market.now_ms()
                    return (latest.get("seq") == seq and current - at <= self.EDGE_ALERT_FRESH_MS
                            and self.edge_maker_fresh(latest, current)
                            and (not latest.get("maker_only") or prepared.get("text") == self.edge_maker_text(latest, current)))

                async def send(sub: dict = sub, key: str = key, text: str = alert["text"], seq: int = alert["seq"],
                               fresh: Any = fresh, market: str = market, prepared: dict = prepared) -> None:
                    latest = ((self.store.get("edgealerts", {}) or {}).get(market) or {}).get("alert") or {}
                    if latest.get("maker_only"):
                        if latest.get("seq") != seq or not self.edge_maker_fresh(latest, self.market.now_ms()):
                            return
                        text = prepared["text"] = self.edge_maker_text(latest, self.market.now_ms())
                        if latest.get("kind") != "gone":
                            mk = next((mk for mk in self.sim_markets(self.market.now_ms())
                                       if mk.market == latest.get("market")), None)
                            edge = next((e for e in book_edges(mk.fair_up, mk.book, self.edge_costs()) if e.maker
                                         and (e.side == "涨") == (latest.get("side") == "up")), None) if mk else None
                            prepared["note"] = self.edge_note(mk, edge, self.market.now_ms()) if edge else None
                    if await self.tell(sub["chat"], sub["thread"], text, html_mode=True, fresh=fresh):
                        self.store.put(key, seq)
                        self.count_alert(str(latest.get("kind") or alert_kind(text)))
                        if latest.get("maker_only"):
                            current_state = self.store.get("edgealerts", {}) or {}
                            record = current_state.get(market) or {}
                            if (record.get("alert") or {}).get("seq") == seq and record.get("told") and prepared.get("note"):
                                record["note"] = {**prepared["note"], "at": self.market.now_ms()}
                                self.store.put("edgealerts", current_state)
                self.deliver(key, send)
        if digest:
            self.edge_digest(subs, now_ms)

    def edge_digest(self, subs: list[tuple[str, dict]], now_ms: int) -> None:
        """Digest mode (EDGE_ALERT_DIGEST_MINUTES): the 新机会 announcements are not sent one by one but gathered, once
        per period and subscription, into one message grouped by driver (five levels of one ladder are one entry with
        five lines, not five interruptions). A line is included only while its market's latest announcement is that
        one and the card still suggests that side right now (its current price and edge are what the line shows),
        re-checked right before sending; nothing to say sends nothing and leaves the period open."""
        period = self.config.edge_alert_digest * 60_000
        for sub_id, sub in subs:
            key = f"edgedigest:{sub_id}"
            if now_ms - int(self.store.get(key) or 0) < period or self.delivering(key):
                continue
            included: dict[str, int] = {}

            def build(sub_id: str = sub_id, included: dict = included) -> str:
                now = self.market.now_ms()
                latest = self.store.get("edgealerts", {}) or {}
                live = {mk.market: mk for mk in self.sim_markets(now)}
                included.clear()
                rows = []
                for market, st in latest.items():
                    alert = (st.get("alert") or {}) if isinstance(st, dict) else {}
                    if alert.get("kind") != "appear" or st.get("told") != alert.get("side"):
                        continue
                    if int(self.store.get(f"edgesent:{market}:{sub_id}") or 0) >= int(alert.get("seq") or 0):
                        continue
                    mk = live.get(market.removesuffix("|maker"))
                    offer = self.edge_current(mk, str(alert["side"]), bool(alert.get("maker_only"))) if mk is not None else None
                    if offer is None:
                        continue  # the card is gone, holds back, or no longer suggests that side: not a chance to list
                    included[market] = int(alert["seq"])
                    rows.append({**alert, "offer": {**self.edge_note(mk, offer, now), "fair": mk.fair_up if offer.side == "涨" else 1 - mk.fair_up}})
                return self.edge_digest_text(rows) if rows else ""
            if not build():
                continue

            async def send(sub: dict = sub, key: str = key, sub_id: str = sub_id, build: Any = build, included: dict = included) -> None:
                if await self.tell(sub["chat"], sub["thread"], build(), html_mode=True, fresh=lambda: bool(build()), render=build):
                    for market, seq in included.items():
                        self.store.put(f"edgesent:{market}:{sub_id}", seq)
                    self.store.put(key, self.market.now_ms())
                    self.count_alert("digest", len(included))
            self.deliver(key, send)

    def edge_current(self, mk: SimMarket, side: str, maker_only: bool = False) -> "BookEdge | None":
        """The best way to trade ``side`` of a market as its card stands now, if it clears the card's bar (what the
        alert stream calls "suggested"); None when the card holds back, its book is stale, or nothing clears it."""
        now_ms = self.market.now_ms()
        if mk.hold or mk.book.stale(now_ms) or (maker_only and not mk.maker_alerts):
            return None
        all_edges = book_edges(mk.fair_up, mk.book, self.edge_costs())
        edges = [e for e in all_edges if e.maker] if maker_only else [e for e in all_edges if mk.makers or not e.maker]
        return best_edge([e for e in edges if (e.side == "涨") == (side == "up")], mk.need)

    def edge_digest_text(self, alerts: list[dict]) -> str:
        """One message for several 新机会: by driver, the largest edge first, each market one line; the Predict link once
        per driver when its markets share one (a ladder's levels), else per line."""
        groups: dict[str, list[dict]] = {}
        for a in alerts:
            groups.setdefault(str(a.get("driver") or a.get("item") or ""), []).append(a)
        order = sorted(groups.items(), key=lambda kv: -max(float(x["offer"].get("edge") or 0) for x in kv[1]))
        minutes = self.config.edge_alert_digest
        lines = [f"📬 {bold('机会摘要')}｜最近 {minutes} 分钟出现、现在仍成立的新机会 {len(alerts)} 个（{len(groups)} 组）"]
        for driver, rows in order:
            rows.sort(key=lambda x: -float(x["offer"].get("edge") or 0))
            urls = {str(x.get("url") or "") for x in rows}
            lines.append(f"▪ {bold(driver)}（{len(rows)} 个）" + ("：同一标的，一次行情一起变" if len(rows) > 1 else ""))
            for x in rows:
                o = x["offer"]
                item = str(x.get("item") or "")
                short = item[len(driver):].strip() if driver and item.startswith(driver) and len(item) > len(driver) else item
                lines.append(f"· {short}：{o['label']} @ {cents(float(o['price']))} {cents(float(o['edge']), True)}（模型 {cents(float(o.get('fair') or 0))}）"
                             + (f" {x['url']}" if len(urls) > 1 and x.get("url") else ""))
            if len(urls) == 1 and rows[0].get("url"):
                lines.append(str(rows[0]["url"]))
        lines.append("挂单不保证成交，吃单已扣手续费与深度；建议失效 / 方向反转仍会即时提醒。")
        return "\n".join(lines)

    ALERT_COUNT_KEEP_DAYS = 31

    def count_alert(self, kind: str, items: int = 1) -> None:
        """One more edge announcement delivered today (Beijing): /status shows how often the bot interrupted."""
        day = dt.datetime.fromtimestamp(self.market.now_ms() / 1000, BEIJING).date()
        key = f"alerts:{day.isoformat()}"
        record = self.store.get(key) or {}
        record = record if isinstance(record, dict) else {}
        record[kind] = int(record.get(kind) or 0) + 1
        if kind == "digest":
            record["digest_items"] = int(record.get("digest_items") or 0) + items
        self.store.put(key, record)
        cutoff = (day - dt.timedelta(days=self.ALERT_COUNT_KEEP_DAYS)).isoformat()
        old = [k for k in self.store.keys("alerts:") if k.removeprefix("alerts:") < cutoff]
        if old:
            self.store.delete_keys(old)

    def alert_count_line(self, now_ms: int) -> str:
        """📣 today's and yesterday's interruptions by kind: the number to watch when a new feature is meant to need
        less of you, not to let you watch more."""
        day = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING).date()
        today = self.store.get(f"alerts:{day.isoformat()}") or {}
        yesterday = self.store.get(f"alerts:{(day - dt.timedelta(days=1)).isoformat()}") or {}
        count = lambda r: sum(int(v) for k, v in r.items() if k != "digest_items") if isinstance(r, dict) else 0
        parts = [f"{label} {today[k]}" for k, label in (("appear", "新机会"), ("gone", "失效"), ("flip", "反转")) if today.get(k)]
        if today.get("digest"):
            parts.append(f"摘要 {today['digest']} 条含 {today.get('digest_items', 0)} 个")
        return (f"📣 今日优势提醒 {count(today)} 条" + (f"（{'｜'.join(parts)}）" if parts else "") + f"｜昨日 {count(yesterday)} 条"
                + ("｜摘要模式：新机会每 " + f"{self.config.edge_alert_digest} 分钟合并一条" if self.config.edge_alert_digest else ""))

    async def auction_reminders(self, now_ms: int) -> None:
        """Once per market, day and subscription, as its closing auction starts: where each card stands and the best
        trade. Each subscription is recorded only after Telegram accepted its copy, so a failed send is retried on the
        next cycle while the auction is young; a copy whose data aged in the send queue is dropped, then rebuilt."""
        if not self.config.auction_alert or not self.config.probability:
            return
        for market in AUCTIONS:
            holidays = self.config.holidays.get(market, frozenset())
            if market == "sz" or not auction_running(market, now_ms, holidays):
                continue
            start, end, label = auction_window(market, now_ms) or AUCTIONS[market]
            local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
            opened = dt.datetime.combine(local.date(), start, BEIJING)
            day_key = f"auction:{market}:{local.date().isoformat()}"
            if local - opened > dt.timedelta(milliseconds=self.AUCTION_JOIN_MS) or self.store.get(day_key):
                continue  # joined too late for a reminder to help (or an older version already sent it to everyone)
            due = [(sub_id, sub) for sub_id, sub in self.subscriptions().items() if sub.get("active")
                   and not self.store.get(f"{day_key}:{sub_id}") and not self.delivering(f"{day_key}:{sub_id}")]
            if not due:
                continue
            items = [(title, odds) for title, odds in self.odds_items(now_ms)
                     if self.item_market(title) in ({market, "sz"} if market == "sh" else {market})]
            if not items:
                continue
            text = self.auction_text(label, items, now_ms)

            def fresh(market: str = market, built: int = now_ms, holidays: frozenset = holidays) -> bool:
                current = self.market.now_ms()
                return auction_running(market, current, holidays) and current - built <= self.AUCTION_FRESH_MS
            for sub_id, sub in due:
                key = f"{day_key}:{sub_id}"

                async def send(sub: dict = sub, key: str = key, text: str = text, fresh: Any = fresh) -> None:
                    if await self.tell(sub["chat"], sub["thread"], text, html_mode=True, fresh=fresh):
                        self.store.put(key, self.market.now_ms())
                self.deliver(key, send)

    def auction_text(self, label: str, items: list[tuple[str, CloseOdds | str]], now_ms: int) -> str:
        lines = [f"🔔 {bold(label + ' 开始')}", "竞价最后几分钟的价格基本就是收盘价，涨跌大体已定："]
        for title, odds in items:
            lines.append("\n" + bold(f"📍 {title}"))
            lines.extend(tree(self.card_rows(title, odds, now_ms, "现")))
        return "\n".join(lines)

    def card_rows(self, title: str, odds: CloseOdds | str, now_ms: int, word: str) -> list[str]:
        """A card's state for a reminder: its reference close against the price the odds rest on (``word``: 现 /
        估算), the model's fair prices, and the Predict book with its best way when one clears the bar."""
        if not isinstance(odds, CloseOdds):
            return [f"概率暂缺：{odds}"]
        unit = f" {odds.unit}" if odds.unit else ""
        rows = [f"昨收 {fmt(odds.ref)}{unit} → {word} {fmt(odds.effective)}{unit}（{percent(odds.effective, odds.ref):+.2f}%）",
                f"模型 涨 {bold(cents(odds.fair_up))}｜跌 {bold(cents(odds.fair_down))}"
                + ("（按币安代理估算，竞价参考价出来后会更新）" if not odds.direct else "")]
        book = self.predict.books.get(self.predict_key(title))
        if book is not None and not book.stale(now_ms):
            need = self.edge_need(model_swing(odds))
            best = None if odds.warn else best_edge(book_edges(odds.fair_up, book, self.edge_costs()), need)
            quote = f"Predict 买1 {cents(float(book.bid[0])) if book.bid else '无'}｜卖1 {cents(float(book.ask[0])) if book.ask else '无'}"
            rows.append(quote + (f"｜👉 {bold(best.label)} @ {cents(best.price)} 净优势 {cents(best.edge, True)}" if best
                                 else f"｜{odds.warn}，暂不给建议" if odds.warn
                                 else f"｜扣除费用和模型误差后没有方向超过门槛 {cents(need)}"))
        return rows

    PREOPEN_JOIN_MS = 2 * 60_000  # the pre-open reminder is (re)tried only this soon after its moment

    async def preopen_reminders(self, now_ms: int) -> None:
        """Once per market, day and subscription, PREOPEN_ALERT_LEAD_MINUTES before the market's pre-open auction: the
        auction is the day's first direction signal and HK matches at 09:20–09:22, before the 09:30 open, so an order
        resting in a prediction market on yesterday's view wants checking before 09:00. Each of the venue's contract
        cards: its close, the Binance-mapped estimate and the model, the Predict book's best way, and where to watch
        the indicative price. Delivered like the closing-auction reminder (recorded per subscription once Telegram
        accepted it; a copy that aged in the queue is dropped and rebuilt)."""
        if not self.config.preopen_alert:
            return
        for market in PRE_AUCTIONS:
            if market == "sz":
                continue
            window = preopen_window(market, now_ms)
            info = STOCK_MARKETS[market]
            venue_day = dt.datetime.fromtimestamp(now_ms / 1000, dt.timezone(dt.timedelta(hours=info.utc_offset))).date()
            if not window or venue_day.weekday() >= 5 or venue_day in self.config.holidays.get(market, frozenset()):
                continue
            local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
            start = dt.datetime.combine(local.date(), window[0], BEIJING)
            due_at = start - dt.timedelta(minutes=self.config.preopen_lead)
            if not due_at <= local <= due_at + dt.timedelta(milliseconds=self.PREOPEN_JOIN_MS):
                continue
            day_key = f"preopen:{market}:{local.date().isoformat()}"
            if self.store.get(day_key):
                continue
            venues = {market, "sz"} if market == "sh" else {market}
            symbols = [symbol for symbol in self.config.symbols
                       if symbol in self.config.tickers and self.config.tickers[symbol].market in venues]
            if not symbols:
                continue
            due = [(sub_id, sub) for sub_id, sub in self.subscriptions().items() if sub.get("active")
                   and not self.store.get(f"{day_key}:{sub_id}") and not self.delivering(f"{day_key}:{sub_id}")]
            if not due:
                continue
            odds = dict(self.odds_items(now_ms)) if self.config.probability else {}
            text = self.preopen_text(window, start, symbols, odds, now_ms)

            def fresh(built: int = now_ms, end: dt.datetime = dt.datetime.combine(local.date(), window[1], BEIJING)) -> bool:
                current = self.market.now_ms()
                return current - built <= self.AUCTION_FRESH_MS and dt.datetime.fromtimestamp(current / 1000, BEIJING) < end
            for sub_id, sub in due:
                key = f"{day_key}:{sub_id}"

                async def send(sub: dict = sub, key: str = key, text: str = text, fresh: Any = fresh) -> None:
                    if await self.tell(sub["chat"], sub["thread"], text, html_mode=True, fresh=fresh):
                        self.store.put(key, self.market.now_ms())
                self.deliver(key, send)

    async def preopen_price_alerts(self, now_ms: int) -> None:
        """Once per venue, day and subscription, as soon as a contract card prices its stock off the pre-open auction's
        indicative price (the first direction signal of the day, read while the auction runs): each of the venue's
        cards with its reference price against that indicative price, the model, the Predict book's best way, and
        where to watch it. Cards whose feed shows no indicative price yet say so."""
        if not self.config.preopen_alert or not self.config.probability:
            return
        for market in PRE_AUCTIONS:
            phase = preopen_phase(market, now_ms, self.config.holidays.get(market, frozenset()))
            if market == "sz" or phase not in PREOPEN_PRICED:
                continue  # while orders can still be withdrawn the indicative price is a probe: nothing to announce
            local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
            day_key = f"preopenprice:{market}:{local.date().isoformat()}"
            if self.store.get(day_key):
                continue
            venues = {market, "sz"} if market == "sh" else {market}
            items = [(title, odds) for title, odds in self.odds_items(now_ms)
                     if "｜" in title and self.item_market(title) in venues]
            if not any(isinstance(odds, CloseOdds) and (odds.preopen or odds.matched) for _, odds in items):
                continue  # no indicative price read yet
            due = [(sub_id, sub) for sub_id, sub in self.subscriptions().items() if sub.get("active")
                   and not self.store.get(f"{day_key}:{sub_id}") and not self.delivering(f"{day_key}:{sub_id}")]
            if not due:
                continue
            window = preopen_window(market, now_ms) or PRE_AUCTIONS[market]
            text = self.preopen_price_text(window, phase, items, now_ms)

            def fresh(built: int = now_ms, market: str = market) -> bool:
                current = self.market.now_ms()
                return current - built <= self.AUCTION_FRESH_MS and preopen_running(market, current, self.config.holidays.get(market, frozenset()))
            for sub_id, sub in due:
                key = f"{day_key}:{sub_id}"

                async def send(sub: dict = sub, key: str = key, text: str = text, fresh: Any = fresh) -> None:
                    if await self.tell(sub["chat"], sub["thread"], text, html_mode=True, fresh=fresh):
                        self.store.put(key, self.market.now_ms())
                self.deliver(key, send)

    def preopen_price_text(self, window: tuple, phase: str, items: list[tuple[str, CloseOdds | str]], now_ms: int) -> str:
        label = window[2].partition("（")[0]
        if phase == "已撮合":
            lines = [f"📊 {bold(label + '：开盘价已撮合')}（{hhmm(now_ms)}）",
                     "开盘价定了，连续交易从 09:30 开始；竞价高开或低开不等于当天收涨或收跌。"]
        else:
            lines = [f"📊 {bold(label + '：' + phase + '阶段，参考平衡价')}（{hhmm(now_ms)}）",
                     "撤不了单了，参考价比可撤单时可信，但加单仍会改变它，撮合后才定；竞价高开或低开不等于当天收涨或收跌。"]
        for title, odds in items:
            lines.append("\n" + bold(f"📍 {title}"))
            if isinstance(odds, CloseOdds) and (odds.preopen or odds.matched):
                rows = self.card_rows(title, odds, now_ms, "开盘价" if odds.matched else "竞价参考")
                rows[0] += f"｜{odds.proxy_note.partition('（')[2].rstrip('）')}"
            else:
                rows = [f"尚无参考平衡价：{odds.proxy_note.rpartition('｜')[2] if isinstance(odds, CloseOdds) else odds}"]
            symbol = title.split("｜")[-1]
            if symbol in self.config.tickers:
                rows.append("看竞价行情：" + "｜".join(f"{g['name']} {g['url']}" for g in quote_pages(self.config.tickers[symbol])))
            lines.extend(tree(rows))
        return "\n".join(lines)

    def preopen_text(self, window: tuple, start: dt.datetime, symbols: list[str], odds: dict, now_ms: int) -> str:
        lead = self.config.preopen_lead
        label = window[2]
        head, detail = (label.partition("（") + ("",))[:2], label.partition("（")[2]
        when = f"{lead} 分钟后开始" if lead else "开始"
        lines = [f"🔔 {bold(head[0] + ' ' + when)}（{detail}" if detail else f"🔔 {bold(label + ' ' + when)}",
                 "竞价一开始就有方向信息，撮合可能早于连续交易：按昨日判断挂在预测市场的单，请在竞价前核对或撤掉。"
                 "竞价参考价会随即更新到网页和 /status；竞价高开或低开不等于当天收涨或收跌。"]
        for symbol in symbols:
            title = f"{NAMES.get(symbol, symbol)}｜{symbol}"
            lines.append("\n" + bold(f"📍 {title}"))
            rows = self.card_rows(title, odds.get(title, "概率功能未开启"), now_ms, "估算")
            pages = quote_pages(self.config.tickers[symbol])
            rows.append("看竞价行情：" + "｜".join(f"{g['name']} {g['url']}" for g in pages))
            lines.extend(tree(rows))
        return "\n".join(lines)

    def card_currency(self, symbol: str) -> str:
        """Every contract card names its price currency, also when the contract is quoted in it (HKD)."""
        ticker = self.config.tickers.get(symbol) if symbol else None
        return STOCK_MARKETS[ticker.market].currency if ticker else ""

    def predict_day(self, title: str, now_ms: int) -> dt.date | None:
        """The session a card without odds is waiting for (its next close by the calendar)."""
        market = {"恒生指数": "hk", "KOSPI": "kr", "上证指数": "sh"}.get(title)
        ticker = self.config.tickers.get(title.split("｜")[-1])
        market = market or (ticker.market if ticker else None)
        if market is None:
            return None
        return session_remaining(market, now_ms, None, self.config.holidays.get(market, frozenset()))[1]

    def predict_key(self, title: str) -> str:
        return PREDICT_KEYS.get(title) or title.split("｜")[-1]

    def predict_targets(self, now_ms: int) -> dict[str, str]:
        """Item key -> Predict slug for the close the model is pricing (the next close when the model has none)."""
        if not self.config.predict:
            return {}
        odds = dict(self.odds_items(now_ms)) if self.config.probability else {}
        entries = [*PREDICT_ITEMS, *((symbol, f"{NAMES.get(symbol, symbol)}｜{symbol}", self.config.tickers[symbol].market)
                                     for symbol in self.config.symbols if symbol in self.config.tickers)]
        targets = {}
        for key, title, market in entries:
            stem = self.config.predict_slugs.get(key)
            if not stem:
                continue
            item = odds.get(title)
            day = (item.target if isinstance(item, CloseOdds)
                   else session_remaining(market, now_ms, None, self.config.holidays.get(market, frozenset()))[1])
            targets[key] = predict_slug(stem, day)
        if self.config.touch:
            targets.update({spec.key: spec.slug for spec in (*TOUCH_MARKETS, *UPDOWN_MARKETS, *FLIP_MARKETS, *CAP_MARKETS,
                                                              *RANGE_MARKETS, *STOCK_HIT_MARKETS)})
        return targets

    def edge_costs(self) -> EdgeCosts:
        return EdgeCosts(self.config.predict_fee_bps, self.config.predict_trade_usd)

    # --- /edge: the announcement bar per section, market or level -------------------------------------------------------

    EDGE_GROUPS = {"index": "指数", "contract": "合约标的", "crypto": "加密", "levels": "价格阶梯", "ladder": "市值阶梯"}
    EDGE_GROUP_WORDS = {"指数": "index", "index": "index", "合约": "contract", "合约标的": "contract", "个股": "contract",
                        "contract": "contract", "加密": "crypto", "crypto": "crypto", "价格阶梯": "levels", "levels": "levels",
                        "range": "levels", "市值阶梯": "ladder", "ladder": "ladder", "cap": "ladder"}
    EDGE_ALIASES = {"恒指": "HSI", "恒生": "HSI", "恒生指数": "HSI", "上证": "SSE", "沪指": "SSE", "上证指数": "SSE", "韩国": "KOSPI"}

    def edge_group(self, mk: SimMarket) -> str:
        """The web page's section (the big heading) a market's card sits in."""
        if mk.kind == "close":
            return "index" if mk.key in PREDICT_KEYS.values() else "contract"
        return {"range": "levels", "ladder": "ladder"}.get(mk.kind, "crypto")

    @staticmethod
    def edge_level(mk: SimMarket) -> str:
        """A ladder level as the user would type it ("300m", "120k"), "" for a market without levels."""
        return edge_level_norm(mk.item.rsplit(" ", 1)[-1]) if mk.kind in {"range", "ladder"} else ""

    def edge_bars(self) -> tuple[dict[str, float], dict[str, str]]:
        """({bar key: cents}, {bar key: shown name}) as set with /edge (persisted in the settings)."""
        settings = self.settings()
        bars, names = settings.get("edge_bars") or {}, settings.get("edge_names") or {}
        bars = {k: float(v) for k, v in bars.items() if isinstance(v, (int, float))} if isinstance(bars, dict) else {}
        return bars, (names if isinstance(names, dict) else {})

    def edge_default(self, bars: dict[str, float] | None = None) -> float:
        """The default announcement bar (per $1 share): /edge's, else EDGE_ALERT_CENTS."""
        bars = self.edge_bars()[0] if bars is None else bars
        return bars["default"] / 100 if "default" in bars else self.config.edge_alert_edge

    def edge_bar(self, mk: SimMarket, bars: dict[str, float] | None = None) -> float:
        """The net edge (per $1 share) a suggestion on this market must reach to be announced: the level's own bar, else
        the market's, else its section's, else the default (/edge; EDGE_ALERT_CENTS at first)."""
        bars = self.edge_bars()[0] if bars is None else bars
        level = self.edge_level(mk)
        for key in (f"level:{mk.key}|{level}" if level else "", f"key:{mk.key}", f"group:{self.edge_group(mk)}", "default"):
            if key and key in bars:
                return bars[key] / 100
        return self.config.edge_alert_edge

    def edge_markets(self) -> list[tuple[str, str]]:
        """(key, card name) of every market the bars can be set for."""
        out = [(key, title) for key, title, _ in PREDICT_ITEMS]
        out += [(symbol, NAMES.get(symbol, symbol)) for symbol in self.config.symbols]
        for table in (self.touches, self.updowns, self.flips, self.caps, self.ranges):
            out += [(key, getattr(mkt.spec, "name", key)) for key, mkt in table.items()]
        return out

    def edge_target(self, words: list[str]) -> tuple[str, str]:
        """The bar an /edge command addresses, from its words: nothing = the default; a section; a market (its key, an
        alias, or part of its card's name), optionally followed by one of its levels. Returns (bar key, shown name)."""
        if not words:
            return "default", "默认"
        text = " ".join(words)
        if text.lower() in {"默认", "default", "全局"}:
            return "default", "默认"
        group = self.EDGE_GROUP_WORDS.get(text.lower())
        if group:
            return f"group:{group}", self.EDGE_GROUPS[group]
        plain = lambda s: s.replace("$", "").replace("＄", "").lower()
        markets = self.edge_markets()
        for n in range(len(words), 0, -1):
            head, rest = " ".join(words[:n]), words[n:]
            want = plain(head)
            alias = ALIASES.get(head.upper()) or self.EDGE_ALIASES.get(head)
            exact = [(k, name) for k, name in markets if k.lower() == want or (alias and k == alias) or plain(name) == want]
            found = exact or [(k, name) for k, name in markets if want and want in plain(name)]
            if len({k for k, _ in found}) > 1:
                raise ValueError(f"「{head}」对应不止一个市场：" + "、".join(dict(found).values()) + "；请写全名或代码")
            if found:
                key, name = found[0]
                if rest:
                    level = edge_level_norm(" ".join(rest))
                    if not re.fullmatch(r"\d+(\.\d+)?[kmb]?", level):
                        raise ValueError(f"档位「{' '.join(rest)}」看不懂；写成 300M、1B、120k 这样")
                    return f"level:{key}|{level}", f"{name} {' '.join(rest)}"
                return f"key:{key}", name
        raise ValueError(f"找不到市场或栏目「{text}」。栏目：{'、'.join(self.EDGE_GROUPS.values())}；市场：" +
                         "、".join(f"{name}（{k}）" for k, name in markets[:12]) + ("…" if len(markets) > 12 else ""))

    def edge_text(self) -> str:
        bars, names = self.edge_bars()
        default = bars.get("default", self.config.edge_alert_edge * 100)
        lines = [f"🔔 优势提醒门槛：默认 ≥{default:g}¢（持续 {self.config.edge_alert_confirm} 秒才提醒）"]
        order = {"group": 0, "key": 1, "level": 2}
        extra = sorted((k for k in bars if k != "default"), key=lambda k: (order.get(k.partition(':')[0], 9), k))
        if extra:
            lines.append("单独设置（档位 > 市场 > 栏目 > 默认）：")
            lines += [f"· {names.get(k, k)}：≥{bars[k]:g}¢" for k in extra]
        else:
            lines.append("没有单独设置的栏目、市场或档位。")
        lines.append("用法：/edge 6 改默认；/edge 市值阶梯 6（栏目：指数 / 合约标的 / 加密 / 价格阶梯 / 市值阶梯）；"
                     "/edge 牛来 4（市场：名字、别名或代码）；/edge 牛来 300M 3（档位）；末尾写 off 取消；/edge 清空 全部取消。"
                     "网页红框的门槛另在网页 ✎ 自定义里设置。")
        return "\n".join(lines)

    def edge_status_line(self) -> str:
        bars, names = self.edge_bars()
        line = (f"🔔 优势提醒：Predict 建议净优势 ≥{bars.get('default', self.config.edge_alert_edge * 100):g}¢ 持续 "
                f"{self.config.edge_alert_confirm} 秒提醒；提醒过的建议失效或反转也会提醒")
        extra = [k for k in bars if k != "default"]
        if extra:
            line += "｜单独门槛：" + "、".join(f"{names.get(k, k)} {bars[k]:g}¢" for k in extra[:6]) + ("…" if len(extra) > 6 else "")
        return line + "｜/edge 调整"

    def cmd_edge(self, req: Request) -> str:
        """/edge: show or set the announcement bar per section, market or level."""
        args = list(req.args)
        if not args:
            return self.edge_text()
        bars, names = self.edge_bars()
        if len(args) == 1 and args[0].lower() in {"清空", "clear", "全部取消"}:
            self.update_settings(edge_bars={}, edge_names={})
            return "✅ 已取消全部单独门槛，都按默认。\n" + self.edge_text()
        value = args[-1].rstrip("¢c￠")
        off = value.lower() in {"off", "关", "关闭", "取消", "删除", "reset"}
        if not off:
            try:
                cents = float(value)
            except ValueError:
                raise ValueError("用法：/edge 6（默认）、/edge 市值阶梯 6、/edge 牛来 4、/edge 牛来 300M 3、/edge 牛来 off；"
                                 "单位 ¢，0.5～50") from None
            if not 0.5 <= cents <= 50:
                raise ValueError("门槛范围 0.5～50¢")
        key, name = self.edge_target(args[:-1])
        if off:
            if key not in bars:
                return f"ℹ️ {name} 没有单独的门槛，无需取消。\n" + self.edge_text()
            bars.pop(key, None); names.pop(key, None)
            self.update_settings(edge_bars=bars, edge_names=names)
            return f"✅ 已取消 {name} 的单独门槛。\n" + self.edge_text()
        bars[key] = round(cents, 2); names[key] = name
        self.update_settings(edge_bars=bars, edge_names=names)
        what = "默认门槛" if key == "default" else f"{name} 的提醒门槛"
        return f"✅ {what}改为 ≥{cents:g}¢（下一轮生效；已提醒过的建议不重发）。\n" + self.edge_text()

    def edge_need(self, swing: float = 0.0) -> float:
        """The net edge a suggestion must clear: the configured minimum, or the model's own error when that is larger."""
        return max(self.config.predict_min_edge, swing)

    def book_block(self, out: dict, book: PredictBook, fair: float | None, need: float, swing: float, hold: str,
                   sides: tuple[str, str], now_ms: int, makers: bool = True) -> None:
        """Fill a card's book block: both sides' depth (priced as the 涨 / Yes side), the model's fair price for that side,
        the bar a suggestion must clear and why none is made, and the four edges for PREDICT_TRADE_USD. The page
        recomputes the edges from the same depth for the trade size the viewer picks. ``makers`` False: only the taker
        edges may be suggested (the maker ones are shown, never best)."""
        out.update(bids=[[float(p), float(q)] for p, q in book.bids[:PREDICT_SHOW_DEPTH]],
                   asks=[[float(p), float(q)] for p, q in book.asks[:PREDICT_SHOW_DEPTH]],
                   age=max(0, (now_ms - book.fetched_ms) // 1000), stale=book.stale(now_ms), fetched_ms=book.fetched_ms,
                   fee_bps=book.fee_bps if book.fee_bps is not None else self.config.predict_fee_bps, sides=list(sides),
                   notional=self.config.predict_trade_usd, makers=makers)
        if "points_active" not in out:  # a price ladder sets its levels' points itself (read every minute, with the maker gate)
            out.update(self.points_status(book.market_id, book, now_ms, PREDICT_POINTS_STALE_SECONDS))
        if fair is None:
            return
        edges = book_edges(fair, book, self.edge_costs())
        best = None if book.stale(now_ms) or hold else best_edge([e for e in edges if makers or not e.maker], need)
        label = lambda e: e.label.replace("涨", sides[0]).replace("跌", sides[1])
        out.update(fair=fair, need=need, swing=swing, hold=hold, edges=[edge_json(e, best, label(e)) for e in edges])

    def odds_quote_ms(self, title: str, odds: CloseOdds) -> int:
        """When the price behind a card's odds was quoted: the index or stock itself while it trades, else its proxy."""
        quote: Any
        if title == "恒生指数":
            q = self.hsi.quote
            if odds.direct:
                return q.spot_time if q is not None else 0  # the cash index prices it in session
            quote = self.hsi_used or q
        elif title == "KOSPI":
            quote = self.kospi.quote if odds.direct else self.hl.quotes.get("KR200")
        elif title == "上证指数":
            quote = self.cn.quote if odds.direct else self.cn.a50
        elif odds.direct:
            quote = self.stocks.live.get(title.split("｜")[-1])
        else:
            quote = (self.snapshots.get(title.split("｜")[-1]) or {}).get("quote")
        if quote is None:
            return 0
        return int(getattr(quote, "quoted_ms", 0) or getattr(quote, "timestamp_ms", 0) or getattr(quote, "fetched_ms", 0) or 0)

    def predict_payload(self, title: str, odds: CloseOdds | str | None, now_ms: int) -> dict | None:
        """The web page's orderbook block for one item, or None when that item has no Predict market configured."""
        key = self.predict_key(title)
        slug = self.predict.slugs.get(key)
        if not self.config.predict or not slug:
            return None
        book, error = self.predict.books.get(key), self.predict.errors.get(key, "")
        out: dict[str, Any] = {"url": predict_url(slug, self.config.predict_ref), "error": error}
        if book is None:
            return out
        ok = isinstance(odds, CloseOdds)
        swing = model_swing(odds) if ok else 0.0
        self.book_block(out, book, odds.fair_up if ok else None, self.edge_need(swing), swing, odds.warn if ok else "",
                        ("涨", "跌"), now_ms)
        return out

    def cmd_book(self, req: Request) -> "Reply":
        if not self.config.predict:
            return Reply("Predict 盘口功能已关闭（PREDICT=off）。")
        now_ms = self.market.now_ms()
        odds = dict(self.odds_items(now_ms)) if self.config.probability else {}
        lines = [f"📕 {bold('Predict 盘口 vs 模型公平价')}",
                 "挂涨=在买1排队买涨；挂跌=在 1−卖1 排队买跌；吃=立即成交。净优势=模型公平价−成交价，吃单再扣手续费"
                 f"（{self.config.predict_fee_bps / 100:g}%×min(p,1−p)）和按 ${self.config.predict_trade_usd:g} 吃到的深度（每份，¢）；"
                 f"门槛=max({cents(self.config.predict_min_edge)}, 模型误差：σ×/÷{MODEL_SIGMA_ERROR:g}、代理系数±{MODEL_BETA_ERROR:g})"]
        titles = [title for _, title, _ in PREDICT_ITEMS] + [f"{NAMES.get(s, s)}｜{s}" for s in self.config.symbols]
        shown = 0
        for title in titles:
            key = self.predict_key(title)
            slug = self.predict.slugs.get(key)
            if not slug:
                continue
            shown += 1
            item = odds.get(title)
            book, error = self.predict.books.get(key), self.predict.errors.get(key, "")
            lines.append("\n" + bold(f"📍 {title}"))
            url = predict_url(slug, self.config.predict_ref)
            rows = book_lines(book, error, item, now_ms, url, self.edge_costs(), self.config.predict_min_edge)
            if book is None:
                rows.append(url)
            if isinstance(item, str):
                rows.insert(0, f"概率暂缺：{item}")
            lines.extend(tree(rows))
        if not shown:
            lines.append("\n⏳ 还没有盘口数据，启动后约 15 秒首次获取；请稍后再试。")
        lines.append("\n⚠️ 模型只是参考；未计积分/LP 奖励与挂单返佣，挂单不保证成交。")
        return Reply("\n".join(lines), html=True)

    def target_close(self, title: str, target: dt.date) -> tuple[int, str]:
        """Epoch ms and label of the target session's official close for an odds item."""
        market = {"恒生指数": "hk", "KOSPI": "kr", "上证指数": "sh"}.get(title)
        if market is None:
            ticker = self.config.tickers.get(title.split("｜")[-1])
            market = ticker.market if ticker else "sh"
        info = STOCK_MARKETS[market]
        close = dt.datetime.combine(target, CALENDAR.close_time(market, target), dt.timezone(dt.timedelta(hours=info.utc_offset)))
        local = f"，{info.tz_name[:-2]} {close.strftime('%H:%M')}" if info.utc_offset != 8 else ""
        special = f"，{CALENDAR.note(market, target)}" if CALENDAR.note(market, target) else ""
        return (int(close.timestamp() * 1000),
                f"{close.astimezone(BEIJING).strftime('%m-%d %H:%M')} {info.name}收盘（北京时间{local}{special}）")

    def web_url(self) -> str:
        path = f"/p/{self.web_token}"
        return f"{self.config.web_base}{path}" if self.config.web_base else path

    def cmd_web(self, req: Request) -> str:
        if not self.config.web_port or not self.web_token:
            return ("网页未开启：需要一个监听端口。Railway 会自动提供 PORT；其他环境请设置 WEB_PORT。"
                    "设置 WEB=off 可显式关闭。")
        if not self.config.web_base:
            return (f"网页已在端口 {self.config.web_port} 运行，但还没有公网域名。\n"
                    "Railway：服务 Settings → Networking → Generate Domain，重新部署后再发 /web；"
                    f"或设置 WEB_BASE_URL。\n路径：{self.web_url()}")
        return f"📊 概率网页（每 10 秒自动刷新，链接含私密令牌，请勿转发）：\n{self.web_url()}"

    def cmd_prob(self, req: Request) -> "Reply":
        if not self.config.probability:
            return Reply("概率功能已关闭（PROBABILITY=off）。")
        now_ms = self.market.now_ms()
        lines = [f"🎲 {bold('收盘涨跌概率（模型参考，非投资建议）')}",
                 "有效价 = 参考收盘 × 代理现价 / 代理在参考收盘时刻的价格",
                 "P(涨) = 1 − Φ(ln((参考+半跳)/有效) / σ剩余)，平盘两边各计一半"]
        for title, odds in self.odds_items(now_ms):
            if odds is None:
                continue
            lines.append("\n" + bold(f"📍 {title}"))
            rows = odds.detail() if isinstance(odds, CloseOdds) else [f"概率暂缺：{odds}"]
            key = self.predict_key(title)
            if key in self.predict.slugs:
                rows += book_lines(self.predict.books.get(key), self.predict.errors.get(key, ""), odds, now_ms, "",
                                   self.edge_costs(), self.config.predict_min_edge)
            lines.extend(tree(rows))
        lines.append("\n⚠️ 目标日跳过周末和已配置的交易所假期（HOLIDAYS_*），每个假日按半天方差计入；σ 为历史估计；代理与结算标的之间有基差。")
        return Reply("\n".join(lines), html=True)

    def kospi_line200(self, now_ms: int) -> str:
        return self.kospi.line200(now_ms, self.config.color_style, self.hl.quotes.get("KR200"), self.hl.notes.get("KR200", ""))

    def context_lines(self, symbol: str, now_ms: int, price: D | None = None) -> list[str]:
        """Market-context lines for an alert: HSI futures for Hong Kong-listed underlyings, Hyperliquid quote."""
        lines = []
        ticker = self.config.tickers.get(symbol)
        if self.config.hsi_futures and ticker and ticker.market == "hk" and self.hsi.quote:
            lines.append(self.hsi.line(now_ms, self.config.color_style))
        if self.config.kospi_index and ticker and ticker.market == "kr" and self.kospi.quote:
            lines.append(self.kospi.line(now_ms, self.config.color_style))
        if self.config.sse_index and ticker and ticker.market in {"sh", "sz"} and self.cn.quote:
            lines.append(self.cn.line(now_ms, self.config.color_style, self.config.holidays.get("sh", frozenset())))
        if self.config.kospi_index and ticker and ticker.market == "kr" and self.kospi.quote200:
            lines.append(self.kospi_line200(now_ms))
        if price is not None and symbol in self.hl.quotes:
            lines.append(self.hl_line(symbol, price))
        if price is not None:  # Model odds for the next close, so each alert carries its own read.
            lines.append(self.odds_row(self.contract_odds(symbol, price, now_ms)))
        return [line for line in lines if line]

    def references_for(self, symbol: str, now_ms: int) -> dict[str, Baseline]:
        found = {kind: self.reference_for(kind, symbol, now_ms) for kind in REFERENCE_KINDS}
        return {kind: ref for kind, ref in found.items() if ref is not None}

    def setclose_template(self, day: str) -> str:
        """A ready-to-edit batch /setclose covering every monitored symbol."""
        return f"/setclose {day}\n" + "\n".join(f"{short_name(s)} 价格 MM-DD HH:MM" for s in self.config.symbols)

    def health_line(self) -> str:
        """How the bot itself is doing: when it last sampled, what is still on its way to Telegram, the last send failure."""
        sampled = f"{int(time.time() - self.last_cycle)} 秒前" if self.last_cycle else "尚未开始"
        in_flight = sum(not task.done() for task in self.deliveries.values())
        line = f"⏱ 最近采样 {sampled}｜运行 {int((time.time() - self.started) // 60)} 分钟｜Telegram 投递中 {in_flight} 条"
        return line + (f"｜最近发送失败：{self.last_send_error}" if self.last_send_error else "")

    def config_summary(self) -> str:
        settings = self.settings()
        mode = BASELINE_SHORT.get(settings["mode"], settings["mode"])
        return (f"⚙️ 基准 {mode}｜阈值 ±{fmt(settings['threshold'])}%｜每 {self.config.poll} 秒"
                f"｜周期 {settings['cooldown']} 秒")

    def status(self, sub_id: str) -> str:
        """Status card with bold sentinels; send it with html_mode=True."""
        now_ms = self.market.now_ms()
        sub = self.subscriptions().get(sub_id)
        active = ("🟢 已订阅" if sub and sub.get("active") else f"⏸ 已自动暂停（{sub['suspended']}）→ /resume 恢复"
                  if sub and sub.get("suspended") else "⏸ 未订阅/已暂停")
        style = self.config.color_style
        lines = [f"📡 {bold(f'监控状态 v{VERSION}')}｜{active}", self.config_summary(), self.health_line(),
                 f"📊 {legend(style)}｜→ 后为币安现价相对该行价格",
                 calendar_warning(self.config.holidays, dt.datetime.fromtimestamp(now_ms / 1000, BEIJING).date())]
        if self.settings()["mode"] == "binance_daily" and self.config.tickers:
            lines.append("💡 /mode exchange 可把基准对齐到交易所收盘时刻")
        if self.config.edge_alert and self.config.predict:
            lines.append(self.edge_status_line())
            lines.append(self.alert_count_line(now_ms))
        if self.config.hsi_futures:
            lines.append(self.hsi.line(now_ms, style))
            lines.append(self.odds_row(self.hsi_odds(now_ms), "恒指"))
        if self.config.kospi_index:
            lines.append(self.kospi.line(now_ms, style))
            lines.append(self.kospi_line200(now_ms))
            lines.append(self.odds_row(self.kospi_odds(now_ms), "KOSPI"))
        if self.config.sse_index:
            holidays = self.config.holidays.get("sh", frozenset())
            lines.append(self.cn.line(now_ms, style, holidays))
            anchor = self.anchors.get("A50")
            lines.append(self.cn.a50_line(now_ms, style, anchor[1] if anchor and anchor[0] == self.sse_close_ms(now_ms)
                                          and self.cn.a50 and a50_family(self.a50_anchor_source) == a50_family(self.cn.a50.source)
                                          else None,
                                          self.a50_anchor_note))
            lines.append(self.odds_row(self.sse_odds(now_ms), "上证"))
        lines = [line for line in lines if line]
        for symbol in self.config.symbols:
            snapshot = self.snapshots.get(symbol)
            lines.append("\n" + bold(f"📍 {NAMES.get(symbol, symbol)}｜{symbol}"))
            if not snapshot:
                lines.append("└ ⏳ 等待首次采样或基准切换后的刷新")
                continue
            if "error" in snapshot:
                lines.append("└ ⚠️ " + snapshot["error"])
                continue
            quote, base = snapshot["quote"], snapshot["baseline"]
            age = (now_ms - quote.timestamp_ms) / 1000
            if age > self.config.max_age or now_ms >= base.valid_until_ms:
                lines.append("└ ⚠️ 缓存已过期，等待有效的新行情/基准；不应据此判断当前涨跌")
                continue
            change = percent(quote.price, base.value)
            rows = [quote.price_row(now_ms),
                    f"基准 {fmt(base.value)}（{baseline_brief(base)}）→ {pct_text(change, style, strong=True, digits=3)}"]
            for kind in REFERENCE_KINDS:
                rows.extend(self.reference_status(kind, symbol, quote.price, now_ms))
            if symbol in self.config.hl_tickers:
                rows.append(self.hl_line(symbol, quote.price))
            rows.append(self.odds_row(self.contract_odds(symbol, quote.price, now_ms)))
            lines.extend(tree(rows))
        used = tuple(dict.fromkeys(STOCK_MARKETS[t.market].currency for t in self.config.tickers.values()))
        lines.append("\n💱 " + self.fx.summary(used or ("CNY", "HKD", "KRW")))
        lines.append("仅价格提醒；不会自动撤单/交易。")
        return "\n".join(lines)

    async def one_cycle(self) -> None:
        settings = self.settings()
        threshold = D(settings["threshold"])
        self.last_cycle = time.time()
        try:
            await self.market.sync_clock()
            rows = await self.market.prices()
            now_ms = self.market.now_ms()
        except Exception as error:
            text = clean_error(error)
            for symbol in self.config.symbols:
                self.snapshots[symbol] = {"error": text}
            self.log_limited("prices", text)
            for sub_id, sub in self.subscriptions().items():
                if sub.get("active"):
                    self.notice(sub_id, sub, "币安行情接口", text)
            if not self.reference_tasks:
                await self.drain_deliveries()
            return

        # Reference data normally refreshes in its own background tasks (run()); this loop only reads
        # the latest good results. Without those tasks (one-off runs, tests) refresh inline instead.
        inline = not self.reference_tasks
        if inline:
            await asyncio.gather(*(job() for name, job in self.reference_jobs() if name != "概率输入"))

        async def collect(symbol: str) -> tuple[str, dict]:
            try:
                if symbol not in rows:
                    raise ValueError("接口没有此合约；核对 SYMBOLS、上市状态和接口可用性。不会替换为其他合约")
                quote = Quote.parse(rows[symbol], symbol, now_ms, self.config.max_age)
                if settings["mode"] == "binance_daily":
                    base = await self.market.baseline(symbol, now_ms)
                elif settings["mode"] == "exchange_close":
                    base = await self.exchange_time_baseline(symbol, now_ms)
                else:
                    base = self.manual_baseline_for(symbol, now_ms)
                return symbol, {"quote": quote, "baseline": base}
            except PendingData as error:
                return symbol, {"error": clean_error(error), "pending": True}
            except Exception as error:
                return symbol, {"error": clean_error(error)}

        collected = dict(await asyncio.gather(*(collect(s) for s in self.config.symbols)))
        # A Telegram command may change global settings while a request is in flight.
        # Discard the old batch rather than sending alerts using a now-obsolete mode.
        if self.settings() != settings:
            return
        self.snapshots = collected
        if inline and self.config.probability:
            with contextlib.suppress(Exception):  # Probabilities are informational; never block alerts.
                await self.refresh_odds_inputs(now_ms)
        try:
            await self.auction_reminders(now_ms)
            await self.preopen_reminders(now_ms)
            await self.preopen_price_alerts(now_ms)
        except Exception as error:  # a reminder must never block price alerts
            self.log_limited("auction", f"集合竞价提醒失败：{clean_error(error)}")
        try:
            await self.plan_alerts(collected, settings, threshold)
        finally:
            if inline:
                await self.drain_deliveries()  # no background to leave them to

    async def plan_alerts(self, collected: dict, settings: dict, threshold: D) -> None:
        """Decide each subscription's notices and price alerts from this sample; deliveries go out in the background."""
        for sub_id, sub in self.subscriptions().items():
            if not sub.get("active"):
                continue
            self.notice(sub_id, sub, "币安行情接口", None)
            for symbol in self.config.symbols:
                snapshot = collected[symbol]
                error = snapshot.get("error")
                if error and snapshot.get("pending"):
                    continue  # still loading, not a fault: no notice
                if error:
                    self.log_limited(symbol, f"{symbol}: {error}")
                    self.notice(sub_id, sub, symbol, error)
                    continue
                self.notice(sub_id, sub, symbol, None)
                # (nothing here awaits: the settings and subscriptions read at the start still hold; the delivery re-checks
                # both right before it sends)
                quote: Quote = snapshot["quote"]
                base: Baseline = snapshot["baseline"]
                current_ms = self.market.now_ms()
                if current_ms >= base.valid_until_ms or current_ms - quote.timestamp_ms > self.config.max_age * 1000:
                    continue  # Slow Telegram/API calls must not produce stale price alerts.
                if settings["mode"] == "manual" and self.manual_baseline_for(symbol, current_ms).key != base.key:
                    continue
                state_key = f"alert:{sub_id}:{symbol}"
                if self.delivering(state_key):
                    continue  # the last alert is still on its way (it re-checks its data before it goes out)
                change = percent(quote.price, base.value)
                old = self.store.get(state_key, {})
                passive, plan = alert_plan(old, base.key, change, time.time(), threshold,
                                            int(settings["cooldown"]), self.config.step, self.config.min_gap)
                if passive != old:
                    self.store.put(state_key, passive)
                if not plan:
                    continue
                render = functools.partial(self.render_alert, symbol, quote, base, change, threshold, plan.reason,
                                           self.references_for(symbol, current_ms),
                                           self.context_lines(symbol, current_ms, quote.price))
                self.deliver(state_key, functools.partial(self.deliver_alert, sub_id, sub, symbol, render, plan, passive,
                                                          quote, base, settings))

    def render_alert(self, symbol: str, quote: Quote, base: Baseline, change: D, threshold: D, reason: str,
                     references: dict[str, Baseline], context: list[str]) -> str:
        """The alert text as of now (called when the message is planned and again right before it is sent)."""
        return alert_text(symbol, quote, base, change, threshold, reason, references, self.fx, self.config.color_style,
                          context, self.market.now_ms())

    def alert_still_true(self, sub_id: str, symbol: str, plan: Plan, state: dict, quote: Quote, base: Baseline,
                         settings: dict) -> bool:
        """Right before a price alert goes out (also after a wait in Telegram's queue): the subscription and settings are
        unchanged, the quote is still within MAX_PRICE_AGE, and the latest sample still triggers it."""
        now = self.market.now_ms()
        if self.settings() != settings or not self.subscriptions().get(sub_id, {}).get("active"):
            return False
        if now >= base.valid_until_ms or now - quote.timestamp_ms > self.config.max_age * 1000:
            return False
        latest = self.snapshots.get(symbol) or {}
        newest, newest_base = latest.get("quote"), latest.get("baseline")
        if newest is None or newest_base is None or newest_base.key != base.key:
            return False  # the feed broke or the baseline moved on since the alert was planned
        if newest.timestamp_ms > quote.timestamp_ms:
            _, again = alert_plan(state, base.key, percent(newest.price, base.value), time.time(), D(settings["threshold"]),
                                  int(settings["cooldown"]), self.config.step, self.config.min_gap)
            if again is None:
                return False  # the move that triggered it is gone
        return True

    async def deliver_alert(self, sub_id: str, sub: dict, symbol: str, render: Any, plan: Plan, state: dict, quote: Quote,
                            base: Baseline, settings: dict) -> None:
        fresh = functools.partial(self.alert_still_true, sub_id, symbol, plan, state, quote, base, settings)
        if await self.tell(sub["chat"], sub["thread"], "", html_mode=True, fresh=fresh, render=render):
            # Only mark a price alert as delivered AFTER Telegram accepts it.
            # Avoid resurrecting state deleted by a command during delivery.
            if self.settings() == settings and self.subscriptions().get(sub_id, {}).get("active"):
                self.store.put(f"alert:{sub_id}:{symbol}", plan.next_state)

    # --- diagnostics ---------------------------------------------------------------------------------

    def diag_probes(self, now_ms: int) -> list[tuple[str, str, Any, Any]]:
        """(group, source, fetch, check) for every feed the bot uses, each source separately."""
        probes: list[tuple[str, str, Any, Any]] = []

        def get(url: str, extra: dict[str, str]) -> Any:
            return lambda: fetch_source(url, extra, record=False)  # a probe must not reorder the live refresh's sources

        def when(ms: int) -> str:
            return quote_time(ms) + (stale_note(ms, now_ms, BEIJING) if ms else "")

        # Binance: clock and the monitored contracts
        def check_time(data: Any) -> str:
            offset = int(data["serverTime"]) - int(time.time() * 1000)
            return f"服务器时间正常，本机偏差 {offset / 1000:+.1f} 秒"

        def check_prices(rows: Any) -> str:
            missing = [sym for sym in self.config.symbols if sym not in rows]
            if missing:
                raise ValueError("接口没有这些合约：" + "、".join(missing))
            return "、".join(f"{short_name(sym)} {fmt_price(number(rows[sym]['price'], sym))}" for sym in self.config.symbols)

        probes += [("币安", "服务器时间", lambda: self.market.get("/fapi/v1/time"), check_time),
                   ("币安", "合约最新价", self.market.prices, check_prices)]

        # Underlying stock closes: every source for every ticker
        for symbol, ticker in self.config.tickers.items():
            info = STOCK_MARKETS[ticker.market]
            for name, url, extra in StockMarket.sources(ticker):
                def check_stock(raw: bytes, name=name, ticker=ticker, info=info) -> str:
                    if name == "Yahoo":
                        day, close, _ = last_completed_bar([(d, c) for d, _, c in parse_yahoo_daily(raw)], info, now_ms)
                    elif name in {"东方财富", "Naver"}:
                        day, close, _ = last_completed_bar(parse_daily_bars(ticker.market, raw), info, now_ms)
                    else:
                        day, close, _ = parse_quote_close(name, ticker.market, raw, info, now_ms)
                    note = ("（日 K 含 NXT 盘后；实际使用 KRX 实时价校正后的收盘）" if name == "Naver"
                            else "（日 K 仅 KRX 正规时段）" if name == "Yahoo" else "")
                    return f"{day.strftime('%m-%d') if day else '上一交易日（无日期）'} 收盘 {fmt(close)} {info.currency}{note}"
                probes.append((f"交易所收盘·{short_name(symbol)}", f"{name} {ticker.market}:{ticker.code}", get(url, extra), check_stock))

        if self.config.touch:
            for rm in self.ranges.values():
                if not isinstance(rm, StockRangeMarket):
                    continue
                def check_chart(raw: bytes, rm=rm) -> str:
                    meta, bars = parse_yahoo_chart(raw)
                    if meta["price"] is None:
                        raise ValueError("没有 regularMarketPrice")
                    state, opens = us_session_state(now_ms)
                    return (f"{rm.spec.symbol} {meta['price']:,.2f}（成交 {when(meta['time_ms'])}；今日 {len(bars)} 根 1 分钟 K；{state}"
                            + (f"，{rm.et_label(opens)}开盘" if opens else "") + "）")
                url = YAHOO_CHART[0] + urllib.parse.quote(rm.spec.symbol) + "?" + urllib.parse.urlencode(
                    {"interval": "1m", "range": "1d", "includePrePost": "false"})
                probes.append((f"美股触及·{rm.spec.symbol}", "Yahoo 1 分钟 K", get(url, {}), check_chart))

        if self.config.sse_index:
            for name, url, extra in CnIndex.SSE_SOURCES:
                def check_sse(raw: bytes, name=name) -> str:
                    q = parse_cn_index(name, raw, now_ms)
                    return f"{fmt(q.last)}（昨收 {fmt(q.prev_close) if q.prev_close else '—'}）｜{when(q.quoted_ms)}"
                probes.append(("上证实时", name, get(url, extra), check_sse))
            for name, url, extra in CnIndex.DAILY_SOURCES:
                def check_daily(raw: bytes, name=name) -> str:
                    bars = parse_cn_daily(name, raw)
                    day, close, _ = last_completed_bar(bars, STOCK_MARKETS["sh"], now_ms)
                    expected = self.cn.expected_close(now_ms)
                    flag = "" if day >= expected else f"｜⚠️ 应有 {expected.strftime('%m-%d')}，尚未出现"
                    return f"最新完结 {day.strftime('%m-%d')} 收盘 {fmt(close)}（共 {len(bars)} 根）{flag}"
                probes.append(("上证日K", name, get(url, extra), check_daily))
            for name, url, extra in CnIndex.A50_SOURCES:
                def check_a50(raw: bytes, name=name) -> str:
                    fields = eastmoney_fields(raw, ("f57", "f58", "f86")) if name == "东方财富" else ""
                    try:
                        q = CnIndex.parse_a50(name, raw, now_ms)
                    except Exception as error:
                        raise ValueError(f"{clean_error(error)}{'｜' + fields if fields else ''}") from None
                    stale = "｜⚠️ 超 10 分钟未更新" if a50_session(now_ms) != "休市" and now_ms - q.quoted_ms > CnIndex.STALE_MS else ""
                    return (f"{fmt(q.last)}｜{when(q.quoted_ms)}（{a50_session(q.quoted_ms)}）{stale}"
                            + (f"｜{fields}" if fields else "") + ("｜非交易所合约" if "CFD" in name else ""))
                probes.append(("A50实时", name, get(url, extra), check_a50))
            close_day = self.cn.close.day if self.cn.close else self.cn.expected_close(now_ms)
            for label, template in (("1分钟K", CnIndex.A50_MINUTES), ("5分钟K", CnIndex.A50_FIVE_MINUTES)):
                url = template.format(beg=(close_day - dt.timedelta(days=1)).strftime("%Y%m%d"),
                                      end=(close_day + dt.timedelta(days=1)).strftime("%Y%m%d"))
                def check_hist(raw: bytes, label=label) -> str:
                    data = json.loads(raw).get("data") or {}
                    rows = [str(r).split(",") for r in data.get("klines") or []]
                    wanted = f"{close_day.isoformat()} 15:00"
                    hit = next((r for r in rows if r[0] == wanted and len(r) >= 3), None)
                    span = f"{rows[0][0][5:]}～{rows[-1][0][5:]}" if rows else "无数据"
                    if not a50_code_ok(data.get("code")):
                        raise ValueError(f"代码 {data.get('code')} 不是 A50")
                    if hit is None:
                        raise ValueError(f"没有 {wanted} 这一根（返回 {len(rows)} 根：{span}）")
                    return f"{wanted[5:]} 收 {fmt(number(hit[2], 'A50'))}（返回 {len(rows)} 根：{span}）"
                probes.append(("A50锚点", f"东方财富{label} {close_day.strftime('%m-%d')} 15:00",
                               get(url, {"Referer": "https://quote.eastmoney.com/"}), check_hist))

            def check_sina_hist(raw: bytes) -> str:
                bars = parse_sina_bars(raw)
                wanted = f"{close_day.isoformat()} 15:00"
                hit = next((c for w, c in bars if w == wanted), None)
                span = f"{bars[0][0][5:]}～{bars[-1][0][5:]}"
                if hit is None:
                    raise ValueError(f"没有 {wanted} 这一根（返回 {len(bars)} 根：{span}）")
                return f"{wanted[5:]} 收 {fmt(hit)}（返回 {len(bars)} 根：{span}）"
            probes.append(("A50锚点", f"新浪5分钟K {close_day.strftime('%m-%d')} 15:00",
                           get(CnIndex.A50_SINA_FIVE_MINUTES, {"Referer": "https://finance.sina.com.cn/"}), check_sina_hist))

        if self.config.hsi_futures:
            holidays = self.hsi.holidays
            for name, url, extra in IndexFutures.FUTURES_SOURCES:
                def check_fut(raw: bytes, name=name) -> str:
                    q = IndexFutures.parse_futures(name, raw, now_ms, holidays)
                    basis = f"｜{'高' if q.basis > 0 else '低' if q.basis < 0 else '平'}水 {abs(q.basis):,.0f}" if q.spot is not None else ""
                    return f"{q.session_name(holidays)} {fmt(q.last)}（前收 {fmt(q.prev_settle) if q.prev_settle else '—'}）{basis}｜{when(q.quoted_ms)}"
                probes.append(("恒指期货", name, get(url, extra), check_fut))
            hk_day = hk_cash_close_date(now_ms, holidays)
            hk_close = f"{hk_day.isoformat()} {CALENDAR.close_time('hk', hk_day):%H:%M}"

            def check_cfd_hist(raw: bytes) -> str:
                bars = parse_sina_bars(raw, "恒指")
                hit = next((c for w, c in bars if w == hk_close), None)
                span = f"{bars[0][0][5:]}～{bars[-1][0][5:]}"
                if hit is None:
                    raise ValueError(f"没有 {hk_close} 这一根（返回 {len(bars)} 根：{span}）")
                return f"{hk_close[5:]} 收 {fmt(hit)}（返回 {len(bars)} 根：{span}）"
            probes.append(("恒指锚点", f"新浪CFD 5分钟K {hk_close[5:]}", get(IndexFutures.CFD_FIVE_MINUTES,
                                                                            {"Referer": "https://finance.sina.com.cn/"}), check_cfd_hist))
            for name, url, extra in IndexFutures.SPOT_SOURCES:
                def check_spot(raw: bytes, name=name) -> str:
                    last, prev = IndexFutures.parse_spot(name, raw)
                    return f"{fmt(last)}（昨收 {fmt(prev) if prev else '—'}）"
                probes.append(("恒指现货", name, get(url, extra), check_spot))
            for kind, url in self.hsi_daily.sources:
                def check_hsi_daily(raw: bytes, kind=kind) -> str:
                    bars = finished_bars(sorted(DailyCloses.parse(kind, "hk", raw)), "hk", now_ms)
                    if not bars:
                        raise ValueError("日 K 为空")
                    return f"最新完结 {bars[-1][0]:%m-%d} 收盘 {fmt(bars[-1][2])}（共 {len(bars)} 根）"
                probes.append(("恒指日K", {"tencent": "腾讯日K", "eastmoney": "东方财富日K", "yahoo": "Yahoo ^HSI"}.get(kind, kind),
                               get(url, DailyCloses.REFERERS.get(kind)), check_hsi_daily))

        if self.config.kospi_index:
            for group, sources in (("KOSPI", KospiIndex.SOURCES), ("KOSPI200", KospiIndex.SOURCES_200)):
                for name, url, extra in sources:
                    def check_kospi(raw: bytes, name=name) -> str:
                        q = KospiIndex.parse(name, raw, now_ms)
                        return f"{fmt(q.last)}（昨收 {fmt(q.prev_close) if q.prev_close else '—'}）｜{quote_time(q.quoted_ms)}"
                    probes.append((group, name, get(url, extra), check_kospi))

        for dex in sorted({dex for dex, _ in self.hl.tickers.values()}):
            coins = sorted(coin for d, coin in self.hl.tickers.values() if d == dex)
            payload: dict[str, Any] = {"type": "metaAndAssetCtxs", **({"dex": dex} if dex else {})}
            def check_hl(data: Any, coins=coins) -> str:
                found = Hyperliquid.parse_dex(data)
                missing = [c for c in coins if c not in found]
                if missing:
                    raise ValueError(f"找不到 {'、'.join(missing)}（该 dex 共 {len(found)} 个市场）")
                return "、".join(f"{c} {fmt(found[c].mark)}" for c in coins)
            probes.append(("Hyperliquid", f"dex {dex or '主市场'}", lambda payload=payload: http_json(Hyperliquid.URL, payload), check_hl))

        for name, url in FxRates.SOURCES:
            def check_fx(data: Any) -> str:
                rates = data.get("rates") if isinstance(data, dict) else None
                if not isinstance(rates, dict):
                    raise ValueError("没有 rates 字段")
                return "、".join(f"{c} {fmt(number(rates[c], c))}" for c in ("CNY", "HKD", "KRW") if c in rates) or "没有所需货币"
            probes.append(("汇率", name.split("（")[0], lambda url=url: http_json(url), check_fx))
        return probes

    async def diagnose(self) -> list[ProbeResult]:
        now_ms = self.market.now_ms()
        gate = asyncio.Semaphore(DIAG_PARALLEL)

        async def one(group: str, name: str, fetch: Any, check: Any) -> ProbeResult:
            async with gate:
                return await run_probe(group, name, fetch, check)

        token = HTTP_POOL.set(self.reference_pool)  # keep probe requests off the Binance/Telegram pool
        try:
            return list(await asyncio.gather(*(one(*p) for p in self.diag_probes(now_ms))))
        finally:
            HTTP_POOL.reset(token)

    def diag_state(self, now_ms: int) -> list[str]:
        """What the running bot currently holds: background refresh health and derived values."""
        lines = []
        if self.reference_state:
            lines.append("🔄 后台刷新")
            for name, _ in self.reference_jobs():
                state = self.reference_state.get(name)
                if not state:
                    lines.append(f"  ⏳ {name}：尚未运行")
                    continue
                running = time.time() - state["running_since"] if state.get("running_since") else 0.0
                if not state["runs"]:
                    lines.append(f"  ⏳ {name}：首轮进行中（已 {running:.0f}s）" if running else f"  ⏳ {name}：尚未完成首轮")
                    continue
                ago = f"{int(time.time() - state['ok_at'])} 秒前" if state["ok_at"] else ""
                status = state.get("status") or ("failed" if state["error"] and state["error_at"] >= state["ok_at"] else "ok")
                if status == "failed":
                    mark, what = "❌", f"上一轮失败，最近成功 {ago}" if ago else "从未成功"
                elif status == "partial":
                    mark, what = "⚠️", f"部分成功（{ago}）"
                else:
                    mark, what = "✅", f"{ago}成功"
                err = f"｜{'未取得' if status == 'partial' else '最近错误'}：{brief_error(state['error'], 80)}" if state["error"] else ""
                busy = f"｜本轮进行中 {running:.0f}s" if running >= 1 else ""
                lines.append(f"  {mark} {name}：{what}｜上次用时 {state['ms'] / 1000:.1f}s｜共 {state['runs']} 轮{busy}{err}")
        else:
            lines.append("🔄 后台刷新：未启动（命令行诊断或测试环境）")
        cooling = SOURCE_HEALTH.lines()
        if cooling:
            lines.append("⏸️ 暂时排到最后的源（连续失败；其余源优先，全部失败时仍会尝试）")
            lines.extend(cooling)
        lines.append("📌 当前使用中的数据")
        if self.config.sse_index:
            close = self.cn.close
            lines.append(f"  上证收盘：{f'{close.day:%m-%d} {fmt(close.value)}（{close.source}）' if close else '未确认'}"
                         f"{'' if self.cn.confirmed(now_ms) else f'｜⚠️ 应确认到 {self.cn.expected_close(now_ms):%m-%d}'}"
                         + (f"｜日K错误：{brief_error(self.cn.daily_error, 80)}" if self.cn.daily_error else ""))
            a50 = self.cn.a50
            lines.append(f"  A50 报价：{f'{fmt(a50.last)}｜{stamp(a50.quoted_ms, seconds=False)}｜{a50.source}' if a50 else '无'}"
                         + (f"｜前序源失败：{brief_error(self.cn.a50_skipped, 100)}" if self.cn.a50_skipped else "")
                         + (f"｜全部失败：{brief_error(self.cn.a50_error, 100)}" if self.cn.a50_error else ""))
            anchor = self.anchors.get("A50")
            lines.append(f"  A50 锚点：{f'{fmt(anchor[1])} @ {stamp(anchor[0], seconds=False)}（{self.a50_anchor_note}·{self.a50_anchor_source}）' if anchor else '无'}"
                         + (f"｜最近查找失败：{brief_error(self.a50_anchor_error, 120)}" if self.a50_anchor_error else ""))
            odds = self.sse_odds(now_ms)
            if isinstance(odds, CloseOdds):
                lines.append(f"  上证概率：涨 {odds.fair_up * 100:.1f}¢（有效 {fmt(odds.effective.quantize(D('0.01')))}·σ {odds.sigma * 100:.2f}%·{odds.sigma_note}）")
            elif odds is not None:
                lines.append(f"  上证概率：暂缺——{odds}")
        if self.config.hsi_futures:
            daily = max(self.hsi_daily.daily.items()) if self.hsi_daily.daily else None
            lines.append(f"  恒指日K收盘：{f'{daily[0]:%m-%d} {fmt(daily[1])}' if daily else '未取得'}"
                         + (f"｜错误：{brief_error(self.hsi_daily.error, 60)}" if self.hsi_daily.error else ""))
        if self.config.kospi_index:
            k, hl, anchor = self.kospi.quote, self.hl.quotes.get("KR200"), self.anchors.get("KOSPI")
            daily = max(self.kospi.daily.items()) if self.kospi.daily else None
            lines.append(f"  KOSPI 日K收盘：{f'{daily[0]:%m-%d} {fmt(daily[1])}' if daily else '未取得'}"
                         + (f"｜错误：{brief_error(self.kospi.daily_error, 60)}" if self.kospi.daily_error else ""))
            lines.append(f"  KOSPI 基准：{f'{fmt(k.last)}｜{quote_time(k.quoted_ms)}｜{k.source}' if k else '无'}"
                         + (f"｜错误：{brief_error(self.kospi.error, 80)}" if self.kospi.error else ""))
            lines.append(f"  KR200 代理：{f'{fmt(kr200_price(hl)[0])}（HL {hl.coin} {kr200_price(hl)[1]}；标记价 {fmt(hl.mark)}）｜{int((now_ms - hl.fetched_ms) / 1000)} 秒前' if hl else '无'}")
            lines.append(f"  KR200 锚点：{f'{fmt(anchor[1])} @ {stamp(anchor[0], seconds=False)}（{self.kospi_anchor_note}·HL）' if anchor else '无'}"
                         + (f"｜最近查找失败：{brief_error(self.kospi_anchor_error, 120)}" if self.kospi_anchor_error else ""))
            sigma, note = self.vols.get("KOSPI", "KOSPI")
            share = self.vols.shares.get("KOSPI")
            lines.append(f"  KOSPI 波动率：{sigma * 100:.2f}%（{note}）"
                         + (f"｜盘中 {sigma * math.sqrt(share[0]) * 100:.2f}%（开盘→收盘占 {share[0] * 100:.0f}%）" if share else "｜暂无开盘价，盘中不扣跳空"))
            odds = self.kospi_odds(now_ms)
            if isinstance(odds, CloseOdds):
                lines.append(f"  KOSPI 概率：涨 {odds.fair_up * 100:.1f}¢（有效 {fmt(odds.effective.quantize(D('0.01')))}）")
            elif odds is not None:
                lines.append(f"  KOSPI 概率：暂缺——{odds}")
        if self.config.hsi_futures and (self.hsi.quote or self.hsi.error):
            q = self.hsi.quote
            lines.append(f"  恒指期货：{f'{q.session_name(self.hsi.holidays)} {fmt(q.last)}｜{quote_time(q.quoted_ms)}｜{q.source}' if q else '无'}"
                         + (f"｜错误：{brief_error(self.hsi.error, 80)}" if self.hsi.error else ""))
            day = hk_cash_close_date(now_ms, self.hsi.holidays)
            close_ms = int(dt.datetime.combine(day, CALENDAR.close_time("hk", day), BEIJING).timestamp() * 1000)
            anchors = []
            for family, name in HSI_FAMILY_NAMES.items():
                saved = self.store.get(f"anchor:{family}")
                fresh = self.hsi.families.get(family)
                now_text = f"现价 {fmt(fresh.last)}（{fresh.source}·{quote_time(fresh.quoted_ms)}）" if fresh else "无报价"
                if isinstance(saved, list) and len(saved) >= 3 and saved[0] == close_ms:
                    anchors.append(f"{name} {fmt(D(str(saved[1])))}（{saved[2]}）·{now_text}")
                else:
                    anchors.append(f"{name} 无 {day:%m-%d} 锚点·{now_text}")
            lines.append(f"  恒指锚点：{'；'.join(anchors)}"
                         + (f"｜新浪5分钟K：{brief_error(self.hsi_cfd_error, 60)}" if self.hsi_cfd_error else ""))
            odds = self.hsi_odds(now_ms) if self.config.probability else None
            if isinstance(odds, CloseOdds):
                lines.append(f"  恒指概率：涨 {odds.fair_up * 100:.1f}¢（有效 {fmt(odds.effective.quantize(D('0.01')))}·{odds.proxy_note}）")
            elif odds is not None:
                lines.append(f"  恒指概率：暂缺——{odds}")
        for symbol in self.config.symbols:
            snap = self.snapshots.get(symbol) or {}
            err = self.stocks.errors.get(symbol)
            live, why = self.stocks.live_quote(symbol, now_ms)
            live_text = (f"股票实时：{fmt_price(live.last)}｜{stamp(live.quoted_ms, seconds=False)}｜{live.source}" if live
                         else f"股票实时：{why}" if why else "")
            ticker = self.config.tickers.get(symbol)
            vol = ""
            if ticker and self.config.probability:
                sigma, note = self.stock_sigma(symbol, ticker.market)
                share = self.vols.shares.get(symbol)
                vol = f"σ {sigma * 100:.2f}%（{note}）" + (f"，盘中占 {share[0] * 100:.0f}%" if share else "")
            note = self.stocks.notes.get(ticker.code, "") if ticker else ""
            if "error" in snap or err or live_text or vol or note:
                lines.append(f"  {short_name(symbol)}：" + "｜".join(x for x in (snap.get("error"), f"交易所收盘：{err}" if err else "",
                                                                                note, live_text, vol) if x))
        return lines

    @staticmethod
    def diag_text(results: list[ProbeResult], state: list[str], title: str) -> str:
        failed = [r for r in results if not r.ok]
        groups: dict[str, list[ProbeResult]] = {}
        for r in results:
            groups.setdefault(r.group, []).append(r)
        broken = [g for g, rs in groups.items() if not any(r.ok for r in rs)]
        lines = [title, f"共 {len(results)} 项｜✅ {len(results) - len(failed)}｜❌ {len(failed)}"]
        if results and len(failed) == len(results):
            reasons = sorted({r.detail for r in failed}, key=lambda d: -sum(r.detail == d for r in failed))
            lines.append("🚨 所有数据源都失败：多半是部署环境本身连不上外网（DNS/出网/代理），不是某个接口的问题。"
                         f"最常见的错误：{brief_error(reasons[0], 100)}")
        elif broken:
            lines.append("🚨 整组全部失败（该数据当前拿不到）：" + "、".join(broken))
        partial = [r for r in failed if r.group not in broken]
        if partial and len(failed) < len(results):
            lines.append("⚠️ 其余失败的源（同组有别的源顶上）：" + "、".join(f"{r.group}·{r.name}" for r in partial))
        # what the bot runs on comes first (that is what a phone screen shows); a group with nothing wrong is one line
        lines += ["", *state, ""]
        for group, rs in groups.items():
            lines.append(f"\n【{group}】")
            if all(r.ok for r in rs) and len(rs) > 1:
                lines.append(f"✅ 全部正常（{len(rs)} 项）：" + "；".join(f"{r.name} {r.ms} ms" for r in rs))
            else:
                lines.extend(f"{'✅' if r.ok else '❌'} {r.name}（{r.ms} ms）：{r.detail}" for r in rs)
        return "\n".join(lines)

    async def cmd_diag(self, req: Request) -> str:
        """Probe every source. With the background loops running the probes run beside the command poll (they can
        take up to two minutes) and the report follows as its own message; one-off runs and tests do it inline."""
        key = f"diag:{req.sub_id}"
        if self.delivering(key):
            return "🩺 上一轮检测仍在进行，结果稍后发到这里。"
        note = "🩺 正在逐个检测数据源，通常 10–30 秒，最长约 2 分钟；结果稍后发到这里…"

        async def report() -> str:
            results = await self.diagnose()
            return self.diag_text(results, self.diag_state(self.market.now_ms()),
                                  f"🩺 数据源检测 v{VERSION}｜{stamp(self.market.now_ms())}（北京时间）")

        if self.reference_tasks:
            async def deliver_report() -> None:
                await self.tell(req.chat, req.thread, await report())
            self.deliver(key, deliver_report)
            return note
        await self.tell(req.chat, req.thread, note)
        return await report()

    def reference_jobs(self) -> list[tuple[str, Any]]:
        """(name, coroutine factory) per reference feed; each has its own refresh cadence inside."""
        now = self.market.now_ms
        jobs = [("交易所收盘", lambda: self.stocks.refresh(now())), ("股票实时", lambda: self.stocks.refresh_live(now())),
                ("汇率", self.fx.refresh),
                ("恒指期货", lambda: self.refresh_hsi(now())), ("Hyperliquid", self.hl.refresh),
                ("KOSPI", lambda: self.kospi.refresh(now())), ("上证/A50", lambda: self.cn.refresh(now()))]
        if self.config.probability:
            jobs.append(("概率输入", lambda: self.refresh_odds_inputs(now())))
        if self.config.predict:
            jobs.append(("Predict 盘口", lambda: self.predict.refresh(self.predict_targets(now()))))
        if self.config.touch:
            jobs.append(("先触市场", lambda: self.refresh_touch(now())))
            jobs.append(("涨跌市场", lambda: self.refresh_updown(now())))
            jobs.append(("反超市场", lambda: self.refresh_flip(now())))
            jobs.append(("市值阶梯", lambda: self.refresh_caps(now())))
            jobs.append(("价格阶梯", lambda: self.refresh_ranges(now())))
        if self.config.sim and self.config.predict:
            jobs.append(("模拟交易", lambda: self.sim_step(now())))
        if self.config.predict:
            jobs.append(("市场快照", lambda: self.record_marks(now())))
        if self.config.edge_alert and self.config.predict:
            jobs.append(("优势提醒", lambda: self.edge_alerts(now())))
        return jobs

    async def refresh_hsi(self, now_ms: int) -> Refreshed | bool:
        result = await self.hsi.refresh(now_ms)
        # the dated closes also tell whether etnet's cash index is today's (spot_problem), so they are not tied to PROBABILITY
        daily = await self.hsi_daily.refresh(now_ms) if self.config.hsi_futures else None
        if result is False and daily is None:
            return False  # neither part was due
        errors = [result.error if isinstance(result, Refreshed) else "", f"恒指日K：{daily}" if daily else ""]
        return refreshed(errors, (isinstance(result, Refreshed) and result.status != "failed") + (daily == ""))

    async def refresh_ranges(self, now_ms: int) -> Refreshed:
        for rm in self.ranges.values():
            rm.learn(self.predict.ladders.get(rm.spec.key) or [], self.predict.market_meta)  # a stock's window comes from Predict
            await rm.refresh(now_ms)
        failed = [f"{key}：{rm.error}" for key, rm in self.ranges.items() if rm.error]
        return refreshed(failed, len(self.ranges) - len(failed))

    async def refresh_caps(self, now_ms: int) -> Refreshed:
        for cap in self.caps.values():
            await cap.refresh(now_ms)
        failed = [f"{key}：{cap.error}" for key, cap in self.caps.items() if cap.error]
        return refreshed(failed, len(self.caps) - len(failed))

    async def refresh_flip(self, now_ms: int) -> Refreshed:
        for fm in self.flips.values():
            await fm.refresh(now_ms)
        failed = [f"{key}：{fm.error}" for key, fm in self.flips.items() if fm.error]
        return refreshed(failed, len(self.flips) - len(failed))

    async def refresh_updown(self, now_ms: int) -> Refreshed:
        for mkt in self.updowns.values():
            await mkt.refresh(now_ms)
        failed = [f"{key}：{mkt.error}" for key, mkt in self.updowns.items() if mkt.error]
        return refreshed(failed, len(self.updowns) - len(failed))

    async def refresh_touch(self, now_ms: int) -> Refreshed:
        for touch in self.touches.values():
            created = touch.spec.created_ms if touch.spec.fixed_start else (
                (self.predict.info.get(touch.spec.slug) or {}).get("created_ms") or touch.spec.created_ms)
            if created and created != touch.start_ms:
                touch.start_ms = created
                touch.times["scan"] = -1e9  # check the path from the (new) opening time at once
            await touch.refresh(now_ms)
        failed = [f"{key}：{touch.error}" for key, touch in self.touches.items() if touch.error]
        return refreshed(failed, len(self.touches) - len(failed))

    async def reference_loop(self, name: str, job: Any) -> None:
        """Refresh one reference feed forever, isolated from the price-alert loop and from the other feeds."""
        token = HTTP_POOL.set(self.reference_pool)  # this task's blocking requests stay off the default pool
        try:
            while not self.stopping.is_set():
                started = time.monotonic()
                state = self.reference_state.setdefault(name, {"ok_at": 0.0, "error": "", "error_at": 0.0, "ms": 0,
                                                               "runs": 0, "running_since": 0.0, "status": ""})
                state["running_since"] = time.time()
                ran = True
                try:
                    # False = not due yet: a skipped tick is not a run and must not overwrite the last timing
                    result = await asyncio.wait_for(job(), timeout=REFERENCE_TIMEOUT)
                    ran = result is not False
                    if ran:
                        self.note_refresh(state, result if isinstance(result, Refreshed) else Refreshed("ok"))
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # failures are shown in /status by each feed; keep the last good data
                    text = clean_error(error) or type(error).__name__
                    self.note_refresh(state, Refreshed("failed", text))
                    self.log_limited(f"reference:{name}", f"参考数据 {name} 刷新异常：{text}")
                finally:
                    state["running_since"] = 0.0
                if ran:
                    state.update(ms=int((time.monotonic() - started) * 1000), runs=state["runs"] + 1)
                await self.wait(REFERENCE_TICK)
        finally:
            HTTP_POOL.reset(token)

    @staticmethod
    def note_refresh(state: dict, outcome: Refreshed) -> None:
        """Record one run: only a run that brought valid new data (ok, or partial) moves the success time."""
        now = time.time()
        state["status"] = outcome.status
        if outcome.status != "failed":
            state["ok_at"] = now
        state["error"] = outcome.error
        if outcome.error:
            state["error_at"] = now

    def start_reference_tasks(self) -> None:
        self.reference_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="reference")
        self.reference_tasks = [asyncio.create_task(self.reference_loop(name, job), name=f"reference:{name}")
                                for name, job in self.reference_jobs()]

    async def monitor_loop(self) -> None:
        while not self.stopping.is_set():
            started = time.monotonic()
            try:  # nothing in here may end the loop: a price-alert loop that died quietly would be the worst fault
                await self.one_cycle()
                self.heartbeat()
            except Exception as error:
                self.log_limited("monitor", "监控轮次异常：" + clean_error(error))
            await self.wait(max(0.1, self.config.poll - (time.monotonic() - started)))

    WATCHDOG_TICK = 30  # seconds between the watchdog's looks

    async def watchdog(self) -> None:
        """Ends, and with it run() (exit 1, so Railway restarts the process), when the sampling loop has not begun a
        cycle for WATCHDOG_SECONDS: a request that never returns or a deadlock would otherwise leave a bot that still
        answers commands but no longer watches prices, which no "loop ended" check can see."""
        while not self.stopping.is_set():
            await self.wait(self.WATCHDOG_TICK)
            idle = time.time() - max(self.last_cycle, self.started)
            if not self.stopping.is_set() and idle > WATCHDOG_SECONDS:
                LOG.error("监控循环已 %s 秒没有开始新一轮采样：退出，等待 Railway 重启", int(idle))
                return

    def heartbeat(self) -> None:
        if time.monotonic() - self.last_log.get("heartbeat", -1e9) >= 60:
            ok = sum("quote" in value for value in self.snapshots.values())
            LOG.info("heartbeat: valid_quotes=%s/%s active_subscriptions=%s mode=%s deliveries=%s", ok,
                     len(self.config.symbols), sum(bool(s.get("active")) for s in self.subscriptions().values()),
                     self.settings()["mode"], sum(not t.done() for t in self.deliveries.values()))
            self.last_log["heartbeat"] = time.monotonic()

    async def process_update(self, update: dict) -> None:
        message = update.get("message")
        if isinstance(message, dict):
            # Do not execute old configuration commands left over from long downtime.
            age = time.time() - float(message.get("date", time.time()))
            if -60 <= age <= 900:
                await self.process_message(message)
            elif age > 900 and is_admin(message, self.config) and (req := self.parse_request(message)) is not None:
                await self.reply(req, f"⌛ 这条命令发送于 {int(age // 60)} 分钟前（机器人当时未运行），已过期未执行；需要的话请重发。")
            return
        query = update.get("callback_query")
        if isinstance(query, dict):  # A button tap is a live intent, so it is not age-filtered.
            await self.process_callback(query)

    async def commands_loop(self) -> None:
        offset = int(self.store.get("telegram_offset", 0))
        failures = 0
        while not self.stopping.is_set():
            try:
                # A 20-second long poll: at shutdown the blocked request has to finish before the process can exit,
                # and Railway gives 45 seconds in all.
                updates = await self.telegram.call("getUpdates", {"offset": offset, "timeout": 20,
                                                   "allowed_updates": ["message", "callback_query"]}, timeout=30)
                if not isinstance(updates, list):
                    raise ValueError("Telegram 更新格式异常")
                failures = 0
                for update in updates:
                    if self.stopping.is_set():
                        break
                    try:
                        await self.process_update(update)
                    except Exception as error:
                        self.log_limited("command", "命令处理异常：" + clean_error(error))
                    offset = int(update["update_id"]) + 1
                    self.store.put("telegram_offset", offset)
            except Exception as error:
                failures += 1
                self.log_limited("poll", "TG 轮询异常：" + clean_error(error))
                retry_after = getattr(error, "retry_after", 0)
                await self.wait(max(retry_after, min(60, 2 ** min(failures, 6))))

    async def register_menu(self) -> None:
        """Publish the command list so Telegram shows the "菜单" button next to the input box.

        Failure here is not fatal: commands still work when typed by hand.
        """
        try:
            await self.telegram.call("setMyCommands", {"commands": [c.menu_entry() for c in COMMANDS]})
            await self.telegram.call("setChatMenuButton", {"menu_button": {"type": "commands"}})
            LOG.info("Registered %s menu commands", len(COMMANDS))
        except Exception as error:
            LOG.warning("注册命令菜单失败（不影响手动输入命令）：%s", clean_error(error))

    async def run(self) -> int:
        """The bot's life: 0 when it stopped on request (signal), 1 when a core loop ended on its own (the process
        exits so Railway restarts it; a bot that keeps answering commands but no longer samples prices must not live)."""
        # Refuse to silently remove a webhook that may belong to another service.
        webhook = await self.telegram.call("getWebhookInfo")
        if webhook.get("url"):
            raise ValueError("该 Bot Token 已绑定 Webhook。请为本项目使用独立机器人，或在原服务移除 Webhook 后重试")
        me = await self.telegram.call("getMe")
        self.username = me.get("username", "")
        LOG.info("Starting @%s; symbols=%s; administrator_configured=%s", self.username,
                 ",".join(self.config.symbols), bool(self.config.admin_id))
        await self.register_menu()
        if not self.config.admin_id:
            LOG.warning("ADMIN_USER_ID 尚未配置：只能使用 /id；没有任何自动订阅")
        if warning := calendar_warning(self.config.holidays, dt.datetime.now(BEIJING).date()):
            LOG.warning("%s", warning)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.stopping.set)
        # Binance, Telegram and the long poll share the default pool; on a one-core box Python gives it 5 threads, and at
        # a day change several contracts' candle requests would then queue the Telegram sends behind them.
        loop.set_default_executor(ThreadPoolExecutor(max_workers=16, thread_name_prefix="io"))
        self.start_reference_tasks()
        core = [asyncio.create_task(self.monitor_loop(), name="monitor"),
                asyncio.create_task(self.commands_loop(), name="commands"),
                asyncio.create_task(self.watchdog(), name="watchdog")]
        tasks = [*core, *self.reference_tasks]
        if self.config.web_port:
            try:
                self.web = WebServer(self, self.config.web_port, self.web_token)
                port = await self.web.start()
                LOG.info("Probability page listening on port %s (%s)", port,
                         "public URL via /web" if self.config.web_base else "no public domain yet")
            except OSError as error:
                LOG.warning("概率网页启动失败（不影响提醒）：%s", clean_error(error))
                self.web = None
        exit_code = 0
        stopper = asyncio.create_task(self.stopping.wait(), name="stopping")
        try:
            await asyncio.wait([stopper, *core], return_when=asyncio.FIRST_COMPLETED)
            for task in core:
                if task.done() and not task.cancelled():  # a core loop never ends on its own: let Railway restart us
                    error = task.exception()
                    if error is None and self.stopping.is_set():
                        continue  # the stop request ended this loop a moment before run() woke up: the ordinary exit, not a fault
                    LOG.error("核心循环 %s 意外结束：%s；进程退出等待重启", task.get_name(),
                              clean_error(error) if error else "监控循环卡住" if task.get_name() == "watchdog" else "无异常")
                    exit_code = 1
        finally:
            self.stopping.set()
            stopper.cancel()
            # Railway drains for 45 seconds: give the messages already on their way a moment to finish (Telegram may
            # have taken them, and only then is their state recorded), then stop everything else.
            pending = [task for task in self.deliveries.values() if not task.done()]
            if pending:
                await asyncio.wait(pending, timeout=SHUTDOWN_GRACE_SECONDS)
            for task in [*tasks, *pending]:
                task.cancel()
            await asyncio.gather(*tasks, *pending, stopper, return_exceptions=True)
            if self.reference_pool:
                self.reference_pool.shutdown(wait=False, cancel_futures=True)
            if self.web:
                await self.web.stop()
            LOG.info("Stopped safely (exit %s)", exit_code)
        return exit_code


async def run_diagnostics(config: Config) -> int:
    """`python main.py --diag`: probe every source once, refresh the reference data the way the bot does,
    and print the same report as /diag. No Telegram token is needed; nothing is sent or stored."""
    store = Store(":memory:")
    bot = Bot(config, store, Binance(config), None)  # type: ignore[arg-type]
    try:
        with contextlib.suppress(Exception):
            await bot.market.sync_clock()
        now_ms = bot.market.now_ms()
        await asyncio.gather(*(job() for name, job in bot.reference_jobs() if name != "概率输入"), return_exceptions=True)
        if config.probability:
            with contextlib.suppress(Exception):
                await bot.refresh_odds_inputs(now_ms)
        results = await bot.diagnose()
        print(bot.diag_text(results, bot.diag_state(bot.market.now_ms()),
                            f"🩺 数据源检测 v{VERSION}｜{stamp(bot.market.now_ms())}（北京时间）"))
        return 0 if all(r.ok for r in results) else 1
    finally:
        store.close()


async def check_market(config: Config) -> int:
    """Live read-only diagnostics; no Telegram token or administrator is necessary."""
    market = Binance(config)
    try:
        await market.sync_clock()
        rows = await market.prices()
    except Exception as error:
        print("FAIL: " + clean_error(error))
        return 1
    failed = False
    for symbol in config.symbols:
        try:
            if symbol not in rows:
                raise ValueError("未发现合约；未替换代码")
            quote = Quote.parse(rows[symbol], symbol, market.now_ms(), config.max_age)
            base = await market.baseline(symbol, market.now_ms())
            print(f"OK {symbol}: {quote.kind}={quote.price}, daily_close={base.value}, "
                  f"change={percent(quote.price, base.value):+.4f}%, quote_time={stamp(quote.timestamp_ms)}, {base.label}")
        except Exception as error:
            failed = True
            print(f"FAIL {symbol}: {clean_error(error)}")
    print("口径：币安上一 UTC 日日 K，不是股票交易所正式昨收。")
    stocks, fx = StockMarket(config), FxRates(config.fx_manual)
    await asyncio.gather(stocks.refresh(market.now_ms(), force=True), fx.refresh(force=True))
    print(fx.summary())
    hsi = IndexFutures(config.hsi_futures)
    await hsi.refresh(market.now_ms(), force=True)
    print(hsi.line(market.now_ms(), config.color_style).replace(B0, "").replace(B1, ""))
    kospi = KospiIndex(config.kospi_index)
    await kospi.refresh(market.now_ms(), force=True)
    print(kospi.line(market.now_ms(), config.color_style).replace(B0, "").replace(B1, ""))
    hl = Hyperliquid({**config.hl_tickers, **config.hl_index})
    await hl.refresh(force=True)
    for symbol in config.hl_tickers:
        print(hl.line(symbol, None, config.color_style).replace(B0, "").replace(B1, ""))
    print(kospi.line200(market.now_ms(), config.color_style, hl.quotes.get("KR200"), hl.notes.get("KR200", "")).replace(B0, "").replace(B1, ""))
    for symbol, ticker in config.tickers.items():
        close = stocks.closes.get(symbol)
        if close:
            print(f"OK {symbol} 证券交易所收盘价: {fmt(close.value)} {close.currency} "
                  f"({close.label}{close.close_text}, 来源 {close.source})")
        else:
            failed = True
            print(f"FAIL {symbol} 证券交易所收盘价: {stocks.errors.get(symbol, '未知错误')}")
    return int(failed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只诊断币安最新价和昨日日 K；不发送 TG")
    parser.add_argument("--diag", action="store_true", help="逐个检测全部数据源并打印报告（同 /diag）；不发送 TG")
    parser.add_argument("--calib", action="store_true", help="读取数据库里的预测快照与实际收盘，打印回测报告（同 /calib）")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = None
    try:
        config = Config.from_env()
        if args.check:
            return asyncio.run(check_market(config))
        if args.diag:
            return asyncio.run(run_diagnostics(config))
        if args.calib:
            store = Store(config.db_path)
            print(Bot(config, store, Binance(config), None).calibration_text())  # type: ignore[arg-type]
            return 0
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", config.token):
            raise ValueError("请在 Railway Variables 配置 TELEGRAM_BOT_TOKEN，不要写进代码或提交到 GitHub")
        store = Store(config.db_path)
        return asyncio.run(Bot(config, store, Binance(config), Telegram(config.token)).run())
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        LOG.error("启动失败：%s", clean_error(error))
        return 1
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
