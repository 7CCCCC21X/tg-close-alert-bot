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
import decimal
import html
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
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

D = decimal.Decimal
UTC = dt.timezone.utc
BEIJING = dt.timezone(dt.timedelta(hours=8))
DAY_MS = 86_400_000
VERSION = "1.13.5"
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
    """'上交所688836·腾讯' -> '腾讯'"""
    return source.split("·")[-1] if source else ""


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
REFERENCE_TICK = 5        # seconds between checks in each reference task (each feed has its own cadence)
REFERENCE_TIMEOUT = 300   # one reference refresh may take this long before it is abandoned
EXCHANGE_BASE_HOLD_DAYS = 30  # safety cap for a held exchange-close baseline; National Day / Chuseok fit easily


# Exchange holidays on weekdays (official 2026 notices where known); override with HOLIDAYS_CN/HK/KR.
DEFAULT_HOLIDAYS = {
    "CN": "2026-09-25,2026-10-01..2026-10-07",   # SSE notice: Mid-Autumn 9/25, National Day 10/1-10/7
    "KR": "2026-09-24,2026-09-25,2026-10-05,2026-10-09",  # Chuseok, National Foundation Day (substitute), Hangul Day
    "HK": "2026-10-01",
}


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
            "kr": parse_dates(env.get("HOLIDAYS_KR", DEFAULT_HOLIDAYS["KR"]), "HOLIDAYS_KR")}


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
    web_port: int = 0        # Read-only probability web page; 0 = disabled. Railway injects PORT.
    web_token: str = ""      # Secret path segment; generated and persisted when empty.
    web_base: str = ""       # Public base URL, e.g. https://xxx.up.railway.app
    hl_tickers: dict[str, tuple[str, str]] = field(default_factory=dict)  # symbol -> (dex, coin) on Hyperliquid
    kospi_index: bool = True  # Show the KOSPI composite index for Korea-listed underlyings.
    hl_index: dict[str, tuple[str, str]] = field(default_factory=dict)  # index name -> (dex, coin), e.g. KR200
    predict: bool = True     # Fetch the matching Predict.fun up/down orderbooks and compare them with the model.
    predict_slugs: dict[str, str] = field(default_factory=dict)  # HSI/KOSPI/SSE/symbol -> Predict slug stem
    predict_api_key: str = ""  # Optional x-api-key for api.predict.fun (REST orderbook).
    predict_poll: int = 15   # seconds between orderbook refreshes
    predict_ref: str = "B00EA"  # referral code appended to Predict market links (?ref=); empty = none
    touch: bool = True       # BNB $700 / $900 first-touch market card (Binance spot + Predict book)

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
        return cls(
            token=e.get("TELEGRAM_BOT_TOKEN", "").strip(),
            admin_id=bounded_int(e, "ADMIN_USER_ID", 0, 0, 10**15),
            symbols=symbols, db_path=e.get("STATE_DB", "./data/bot.sqlite3"),
            threshold=threshold,
            cooldown=bounded_int(e, "ALERT_COOLDOWN_SECONDS", 300, 0, 86400),
            poll=bounded_int(e, "POLL_SECONDS", 5, 3, 3600),
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
            touch=e.get("BNB_TOUCH", "on").strip().lower() not in {"off", "0", "false", "no"},
            probability=e.get("PROBABILITY", "on").strip().lower() not in {"off", "0", "false", "no"},
            prob_vol=parse_prob_vol(e.get("PROB_VOL", "")),
            sse_index=e.get("SSE_INDEX", "on").strip().lower() not in {"off", "0", "false", "no"},
            a50_beta=parse_beta(e.get("A50_BETA", "0.8")),
            kospi_beta=parse_beta(e.get("KOSPI_BETA", "1"), "KOSPI_BETA"),
            holidays=parse_holidays(e),
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

    def get(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT v FROM records WHERE k=?", (key,)).fetchone()
        return json.loads(row[0]) if row else copy.deepcopy(default)

    def put(self, key: str, value: Any) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO records(k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                              (key, json.dumps(value, ensure_ascii=False)))

    def items(self, prefix: str) -> list[tuple[str, Any]]:
        rows = self.conn.execute("SELECT k, v FROM records WHERE substr(k,1,?)=? ORDER BY k", (len(prefix), prefix))
        return [(k, json.loads(v)) for k, v in rows]

    def delete_prefix(self, prefix: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM records WHERE substr(k,1,?)=?", (len(prefix), prefix))

    def close(self) -> None:
        self.conn.close()


class PendingData(ValueError):
    """Data that simply has not arrived yet (first fetch still running): shown in /status, no error notice."""


class RemoteError(Exception):
    def __init__(self, message: str, retry_after: int = 0):
        super().__init__(message)
        self.retry_after = max(0, retry_after)


BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"


def _http_get(url: str, payload: dict | None = None, timeout: int = 15,
              headers: dict[str, str] | None = None) -> bytes:
    """GET (or POST ``payload`` as JSON) and return the body; HTTP/network failures become RemoteError."""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": f"CloseAlert/{VERSION}", "Accept": "application/json",
        **({"Content-Type": "application/json"} if data is not None else {}), **(headers or {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise RemoteError("接口返回的数据过大")
            return raw
    except urllib.error.HTTPError as error:
        retry = 0
        try:
            retry = max(0, int(error.headers.get("Retry-After", "0")))
        except (ValueError, TypeError):
            pass
        description = ""
        try:
            body = json.loads(error.read(4096))
            description = str(body.get("description") or body.get("msg") or "")
            retry = max(retry, int(body.get("parameters", {}).get("retry_after", 0)))
        except (ValueError, TypeError, AttributeError):
            pass
        hints = {451: "部署所在地或接口访问受限；请核对官方地区规则",
                 403: "访问被拒绝；请核对权限与服务地区",
                 418: "接口暂时封禁；停止高频请求并等待解除",
                 429: "接口限流，等待后重试",
                 409: "Telegram 轮询冲突；同一个 Bot Token 只能运行一个实例"}
        if error.code in {418, 429}:
            retry = max(retry, 120 if error.code == 418 else 30)
        text = f"HTTP {error.code}: {hints.get(error.code, description or '接口请求失败')}"
        raise RemoteError(clean_error(text), retry) from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        # Keep the underlying reason (DNS failure, refused, proxy 403, certificate...): /diag relies on it.
        reason = getattr(error, "reason", None) if isinstance(error, urllib.error.URLError) else None
        detail = f": {clean_error(str(reason))[:80]}" if reason else ""
        raise RemoteError(f"网络错误 ({type(error).__name__}{detail})") from None


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


async def fetch_source(url: str, extra: dict[str, str] | None = None) -> bytes:
    """GET one quote-feed URL with a browser UA and the short feed timeout, recording the host's health."""
    try:
        raw = await http_get(url, timeout=SOURCE_TIMEOUT, headers={"User-Agent": BROWSER_UA, "Accept": "*/*", **(extra or {})})
    except (RemoteError, TimeoutError, OSError) as error:
        SOURCE_HEALTH.record(url, clean_error(error) or type(error).__name__)
        raise
    SOURCE_HEALTH.record(url)
    return raw


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
    done = local >= dt.datetime.combine(local.date(), info.close_time, tz) + dt.timedelta(minutes=15)
    return [bar for bar in bars if bar[0] < local.date() or (bar[0] == local.date() and done)]


def last_completed_bar(bars: list[tuple[dt.date, D]], info: StockMarketInfo,
                       now_ms: int) -> tuple[dt.date, D, D | None]:
    """Newest bar whose session has ended (today's counts only 15 minutes after the close),
    plus the close of the bar before it when known."""
    tz = dt.timezone(dt.timedelta(hours=info.utc_offset))
    local = dt.datetime.fromtimestamp(now_ms / 1000, tz)
    final_from = dt.datetime.combine(local.date(), info.close_time, tz) + dt.timedelta(minutes=15)
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
    final_from = dt.datetime.combine(local.date(), info.close_time, tz) + dt.timedelta(minutes=15)
    if quoted.date() < local.date() or (quoted.date() == local.date() and local >= final_from):
        return quoted.date(), number(current, "收盘价"), number(previous, "昨收价")
    return None, number(previous, "昨收价"), None


def stock_live_window(market: str, now_ms: int, holidays: frozenset = frozenset()) -> tuple[int, int] | None:
    """(open, final) epoch ms of today's session while the stock trades, else None.

    Runs from the first continuous-trading minute until the close is final (close time + 15 minutes),
    so the pre-open auction's indicative prices are never read as trades.
    """
    info = STOCK_MARKETS[market]
    tz = dt.timezone(dt.timedelta(hours=info.utc_offset))
    local = dt.datetime.fromtimestamp(now_ms / 1000, tz)
    today = local.date()
    if today.weekday() >= 5 or today in holidays:
        return None
    start = dt.datetime.combine(today, SESSIONS[market][0][0], tz)
    final = dt.datetime.combine(today, info.close_time, tz) + dt.timedelta(minutes=15)
    if not start <= local < final:
        return None
    return int(start.timestamp() * 1000), int(final.timestamp() * 1000)


def parse_stock_live(source: str, market: str, raw: bytes, now_ms: int) -> IndexQuote:
    """Realtime stock quote -> IndexQuote(last, previous close, quote time). Naver for KRX, Tencent/Sina otherwise."""
    if source == "Naver":
        q = parse_naver_index(raw, now_ms)
        return dataclasses.replace(q, source="Naver")
    text = raw.decode("gbk", errors="ignore")
    match = re.search(r'="([^"]*)"', text)
    if not match or not match.group(1).strip():
        raise ValueError(f"{source}行情为空（代码可能不存在）")
    fields = match.group(1).split("~" if source == "腾讯" else ",")
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
            return [("Naver", f"https://fchart.stock.naver.com/sise.nhn?requestType=0&timeframe=day&count=10&symbol={code}",
                     {"Referer": "https://finance.naver.com/"})]
        secid = {"sh": "1", "sz": "0", "hk": "116"}[ticker.market] + "." + ticker.code
        sina = ("rt_hk" if ticker.market == "hk" else ticker.market) + ticker.code
        return [
            ("东方财富", "https://push2his.eastmoney.com/api/qt/stock/kline/get?klt=101&fqt=0&end=20500101&lmt=10"
                         "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56&secid=" + urllib.parse.quote(secid),
             {"Referer": "https://quote.eastmoney.com/"}),
            ("腾讯", f"https://qt.gtimg.cn/q={ticker.market}{code}", {"Referer": "https://gu.qq.com/"}),
            ("新浪", f"https://hq.sinajs.cn/list={sina}", {"Referer": "https://finance.sina.com.cn/"}),
        ]

    @staticmethod
    def live_sources(ticker: StockTicker) -> list[tuple[str, str, dict[str, str]]]:
        code = urllib.parse.quote(ticker.code)
        if ticker.market == "kr":
            return [("Naver", f"https://polling.finance.naver.com/api/realtime/domestic/stock/{code}",
                     {"Referer": "https://finance.naver.com/"})]
        sina = ("rt_hk" if ticker.market == "hk" else ticker.market) + ticker.code
        tencent = ("r_hk" if ticker.market == "hk" else ticker.market) + code  # plain hkXXXXX is 15 minutes delayed
        return [("腾讯", f"https://qt.gtimg.cn/q={tencent}", {"Referer": "https://gu.qq.com/"}),
                ("新浪", f"https://hq.sinajs.cn/list={sina}", {"Referer": "https://finance.sina.com.cn/"})]

    LIVE_SECONDS = 20
    LIVE_STALE_MS = 10 * 60_000

    async def refresh_live(self, now_ms: int, force: bool = False) -> bool | None:
        """Realtime quotes, only for the stocks whose session is running now."""
        due = {symbol: ticker for symbol, ticker in self.config.tickers.items()
               if stock_live_window(ticker.market, now_ms, self.config.holidays.get(ticker.market, frozenset()))}
        if not due or (not force and time.monotonic() - self.live_refreshed < self.LIVE_SECONDS):
            return False  # nothing trading / not due: nothing fetched
        self.live_refreshed = time.monotonic()
        for index, (symbol, ticker) in enumerate(due.items()):
            if index:
                await asyncio.sleep(0.3)
            failures, best = [], None
            for name, url, extra in SOURCE_HEALTH.order(self.live_sources(ticker)):
                try:
                    q = parse_stock_live(name, ticker.market, await fetch_source(url, extra), now_ms)
                except Exception as error:
                    failures.append(f"{name}: {clean_error(error)}")
                    continue
                if best is None or q.quoted_ms > best.quoted_ms:
                    best = q
                self.live[symbol] = best
                if not self.live_quote(symbol, now_ms)[1]:
                    break  # fresh: done
                # A lagging feed (delayed quotes): try the next source, keep the newest print
                failures.append(f"{name}: 报价停在 {stamp(q.quoted_ms, seconds=False)}")
            if best is not None and not self.live_quote(symbol, now_ms)[1]:
                self.live_errors.pop(symbol, None)
            else:
                self.live_errors[symbol] = "；".join(failures)

    def live_quote(self, symbol: str, now_ms: int) -> tuple[IndexQuote | None, str]:
        """(today's fresh realtime quote, why not) while the stock trades; (None, "") outside its session."""
        ticker = self.config.tickers.get(symbol)
        window = ticker and stock_live_window(ticker.market, now_ms, self.config.holidays.get(ticker.market, frozenset()))
        if not window:
            return None, ""
        q = self.live.get(symbol)
        error = self.live_errors.get(symbol, "")
        if q is None:
            return None, f"现货行情未取得（{brief_error(error, 60)}）" if error else "等待现货行情"
        if q.quoted_ms < window[0] - 30 * 60_000:  # still yesterday's print (pre-open auction may stamp minutes early)
            return None, "现货今日尚未开盘成交"
        sessions = SESSIONS[ticker.market]
        lunch = (sessions[0][1], sessions[1][0]) if len(sessions) > 1 else None  # HK/A-share lunch, Beijing time
        info = STOCK_MARKETS[ticker.market]
        tz = dt.timezone(dt.timedelta(hours=info.utc_offset))
        day = dt.datetime.fromtimestamp(now_ms / 1000, tz).date()
        end = max(sessions[-1][1], info.close_time)  # no new prints after the close; don't call its last one stale
        clock = min(now_ms, int(dt.datetime.combine(day, end, tz).timestamp() * 1000) + 60_000)
        if quote_stale(q, clock, self.LIVE_STALE_MS, lunch):
            return None, f"现货行情已超 10 分钟未更新（最后 {stamp(q.quoted_ms, seconds=False)}）"
        return q, ""

    async def fetch(self, symbol: str, ticker: StockTicker, now_ms: int) -> Baseline:
        info = STOCK_MARKETS[ticker.market]
        failures = []
        # One pass over every source (hosts in cooldown last); only when all of them failed, one more pass.
        for attempt in range(self.ATTEMPTS):
            if attempt:
                await asyncio.sleep(1.5)
            for name, url, extra in SOURCE_HEALTH.order(self.sources(ticker)):
                try:
                    raw = await fetch_source(url, extra)
                    if name in {"东方财富", "Naver"}:
                        day, close, prev = last_completed_bar(parse_daily_bars(ticker.market, raw), info, now_ms)
                    else:
                        day, close, prev = parse_quote_close(name, ticker.market, raw, info, now_ms)
                    return self.baseline(ticker, info, name, day, close, prev)
                except Exception as error:
                    failures.append(f"{name}: {clean_error(error)}")
        raise ValueError("；".join(dict.fromkeys(failures)))

    @staticmethod
    def baseline(ticker: StockTicker, info: StockMarketInfo, source: str, day: dt.date | None, close: D,
                 prev: D | None = None) -> Baseline:
        tz = dt.timezone(dt.timedelta(hours=info.utc_offset))
        close_ms = int(dt.datetime.combine(day, info.close_time, tz).timestamp() * 1000) if day else 0
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

    async def refresh(self, now_ms: int, force: bool = False) -> bool | None:
        if not self.config.tickers or (not force and time.monotonic() - self.refreshed < self.REFRESH_SECONDS):
            return False  # not due yet: nothing fetched
        self.refreshed = time.monotonic()
        for index, (symbol, ticker) in enumerate(self.config.tickers.items()):
            if index:
                await asyncio.sleep(0.5)  # Spread requests out; feeds drop bursts from one IP.
            try:
                close = await self.fetch(symbol, ticker, now_ms)
                old = self.closes.get(symbol)
                if not close.close_ms and old and old.close_ms and old.value == close.value:
                    close = old  # an undated quote repeating the dated close must not erase its date
                self.closes[symbol] = close
                self.errors.pop(symbol, None)
                self.remember(symbol, close)
            except Exception as error:  # Keep the last good close; report the failure alongside it.
                self.errors[symbol] = clean_error(error)


class FxRates:
    """USD reference rates (units of currency per 1 USD) for converting exchange closes to USD.

    Manual FX_RATES entries always win; fetched rates come from keyless public sources.
    """
    REFRESH_SECONDS = 6 * 3600
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

    async def refresh(self, force: bool = False) -> bool | None:
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
                return
            except Exception as error:
                failures.append(f"{name}: {clean_error(error)}")
        self.error = "；".join(failures)


@dataclass(frozen=True)
class FuturesQuote:
    """One index-futures quote (the front/main contract) plus the cash index for the basis."""
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

    def session_name(self, holidays: frozenset = frozenset()) -> str:
        return self.session or hk_futures_session(self.quoted_ms, holidays)

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
            session=session))
    if not candidates:
        raise ValueError("etnet 页面没有找到恒指期货报价")
    if all(q.quoted_ms for q in candidates):
        return max(candidates, key=lambda q: q.quoted_ms)
    day = next((q for q in candidates if q.session == "日市"), None)
    night = next((q for q in candidates if q.session == "夜市"), None)
    if day and night:
        if night.prev_settle == day.last and night.prev_settle != day.prev_settle:
            chosen = night   # tonight's (or last night's, after 03:00) session followed this day block
        elif night.prev_settle == day.prev_settle and night.prev_settle != day.last:
            chosen = day     # the night block is the one before this day session
        else:
            chosen = night if hk_futures_session(now_ms, holidays) == "夜市" else day
    else:
        chosen = day or night
    if not chosen.quoted_ms:  # time unknown: now while its session runs, else when that session last ended
        live = hk_futures_session(now_ms, holidays) == chosen.session
        chosen = dataclasses.replace(chosen, quoted_ms=now_ms if live else hk_session_end(chosen.session, now_ms, holidays))
    return chosen


def stale_note(quoted_ms: int, now_ms: int, tz: dt.tzinfo) -> str:
    """'｜⚠️ 非今日数据' when the quote's local calendar day is earlier than today's."""
    quoted = dt.datetime.fromtimestamp(quoted_ms / 1000, tz).date()
    today = dt.datetime.fromtimestamp(now_ms / 1000, tz).date()
    return "｜⚠️ 非今日数据" if quoted < today else ""


def hk_trading_day(day: dt.date, holidays: frozenset = frozenset()) -> bool:
    return day.weekday() < 5 and day not in holidays


def hk_futures_session(now_ms: int, holidays: frozenset = frozenset()) -> str:
    """HKEX HSI futures: day session 09:15-16:30, after-hours (夜市) 17:15-03:00 next day, HK time.
    Sessions only start on trading days, so Friday's night ends Saturday 03:00 and weekends are shut."""
    moment = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
    local, today = moment.time(), moment.date()
    if local >= dt.time(17, 15) and hk_trading_day(today, holidays):
        return "夜市"
    if local < dt.time(3, 0) and hk_trading_day(today - dt.timedelta(days=1), holidays):
        return "夜市"
    if dt.time(9, 15) <= local <= dt.time(16, 30) and hk_trading_day(today, holidays):
        return "日市"
    return "休市"


def hk_session_end(session: str, now_ms: int, holidays: frozenset = frozenset()) -> int:
    """When the latest finished ``session`` ("日市"/"夜市") ended, at or before ``now_ms``."""
    local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
    day = local.date()
    for _ in range(30):
        if hk_trading_day(day, holidays):
            end = (dt.datetime.combine(day, dt.time(16, 30), BEIJING) if session == "日市"
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


class IndexFutures:
    """Hang Seng Index futures (main contract, incl. the 17:15-03:00 after-hours session) with the
    cash index for the 高水/低水 basis. Eastmoney first, Sina as fallback; read-only, best effort."""
    REFRESH_SECONDS = 60
    EM = "https://push2.eastmoney.com/api/qt/stock/get?fltt=2&invt=2&fields=f43,f44,f45,f46,f57,f58,f60,f86&secid="
    # etnet is the HKEX-designated free real-time site the user checks against; Sina hf_HSI is a CFD,
    # not the HKEX contract (it prints decimals), so it is only a last-resort, clearly labelled fallback.
    FUTURES_SOURCES = (("etnet", "https://www.etnet.com.hk/www/tc/futures/index.php", {"Referer": "https://www.etnet.com.hk/"}),
                       ("东方财富", EM + "134.HSI_M", {"Referer": "https://quote.eastmoney.com/"}),
                       ("新浪CFD", "https://hq.sinajs.cn/list=hf_HSI", {"Referer": "https://finance.sina.com.cn/"}))
    SPOT_SOURCES = (("东方财富", EM + "100.HSI", {"Referer": "https://quote.eastmoney.com/"}),
                    ("腾讯", "https://qt.gtimg.cn/q=hkHSI", {"Referer": "https://gu.qq.com/"}),
                    ("新浪", "https://hq.sinajs.cn/list=rt_hkHSI", {"Referer": "https://finance.sina.com.cn/"}))

    def __init__(self, enabled: bool = True, holidays: frozenset = frozenset()):
        self.enabled = enabled
        self.holidays = holidays
        self.quote: FuturesQuote | None = None
        self.error = ""
        self.refreshed = -1e9

    @staticmethod
    def parse_futures(source: str, raw: bytes, now_ms: int, holidays: frozenset = frozenset()) -> FuturesQuote:
        if source == "etnet":
            return parse_etnet_futures(raw, now_ms, holidays)
        if source == "东方财富":
            d = parse_eastmoney_quote(raw)
            quoted_ms = int(d["f86"]) * 1000 if str(d.get("f86", "")).isdigit() else now_ms
            return FuturesQuote(str(d.get("f58") or "恒指期货主力"), number(d["f43"], "恒指期货"), _opt(d.get("f60")),
                                _opt(d.get("f46")), _opt(d.get("f44")), _opt(d.get("f45")), quoted_ms, source)
        # Sina hf_HSI: last, ?, bid, ask, high, low, time [6], prev settle, open, ..., date, name (see sina_hf_time)
        match = re.search(r'="([^"]*)"', raw.decode("gbk", errors="ignore"))
        fields = match.group(1).split(",") if match else []
        if len(fields) < 13 or not fields[0]:
            raise ValueError("新浪恒指期货报价为空")
        quoted_ms, name = sina_hf_time(fields, "恒指期货")
        return FuturesQuote(name or "恒指期货", number(fields[0], "恒指期货"), _opt(fields[7]), _opt(fields[8]),
                            _opt(fields[4]), _opt(fields[5]), quoted_ms, source, exchange_contract=False)

    @staticmethod
    def parse_spot(source: str, raw: bytes) -> tuple[D, D | None]:
        """Cash index (last, previous close)."""
        if source == "东方财富":
            d = parse_eastmoney_quote(raw)
            return number(d["f43"], "恒生指数"), _opt(d.get("f60"))
        text = raw.decode("gbk", errors="ignore")
        match = re.search(r'="([^"]*)"', text)
        if not match or not match.group(1).strip():
            raise ValueError(f"{source}恒生指数报价为空")
        fields = match.group(1).split("~" if source == "腾讯" else ",")
        try:  # Tencent: current [3], prev close [4]; Sina rt_hk: current [6], prev close [3]
            last, prev = (fields[3], fields[4]) if source == "腾讯" else (fields[6], fields[3])
            return number(last, "恒生指数"), _opt(prev)
        except IndexError:
            raise ValueError(f"{source}恒生指数格式异常") from None

    async def _first(self, sources: tuple, parse) -> Any:
        failures = []
        for name, url, extra in SOURCE_HEALTH.order(sources):
            try:
                raw = await fetch_source(url, extra)
                return parse(name, raw)
            except Exception as error:
                failures.append(f"{name}: {clean_error(error)}")
        raise ValueError("；".join(failures))

    async def refresh(self, now_ms: int, force: bool = False) -> bool | None:
        if not self.enabled or (not force and time.monotonic() - self.refreshed < self.REFRESH_SECONDS):
            return False  # not due yet: nothing fetched
        self.refreshed = time.monotonic()
        try:
            quote: FuturesQuote = await self._first(self.FUTURES_SOURCES,
                                                    lambda n, r: self.parse_futures(n, r, now_ms, self.holidays))
        except Exception as error:
            self.error = clean_error(error)
            return
        try:
            if quote.spot is None:  # etnet already carries the cash index
                spot, spot_prev = await self._first(self.SPOT_SOURCES, self.parse_spot)
                quote = FuturesQuote(**{**quote.__dict__, "spot": spot, "spot_prev": spot_prev})
        except Exception as error:  # Basis is a nice-to-have; the futures quote alone is still shown.
            LOG.debug("HSI spot unavailable: %s", clean_error(error))
        self.quote, self.error = quote, ""

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
        parts.append(f"{stamp(q.quoted_ms, seconds=False)} {source}" + stale_note(q.quoted_ms, now_ms, BEIJING))
        line = "｜".join(parts)
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
        """Close of the candle ending at ``at_ms`` (the market's price at that instant).

        Hyperliquid only serves the latest 5,000 candles per interval (about 3.5 days of 1-minute bars),
        so after a long holiday the 5-minute candle ending at the same instant is the fallback.
        """
        key = (coin, at_ms, interval)
        if key in self.at_cache:
            return self.at_cache[key]
        minutes = {"1m": 1, "5m": 5, "15m": 15}[interval]
        rows = await http_json(self.URL, {"type": "candleSnapshot", "req": {
            "coin": coin, "interval": interval, "startTime": at_ms - 5 * minutes * 60_000, "endTime": at_ms}})
        candles = [r for r in rows if isinstance(r, dict) and int(r.get("t", 0)) < at_ms] if isinstance(rows, list) else []
        if not candles:
            raise ValueError(f"Hyperliquid 没有 {coin} 在 {stamp(at_ms, seconds=False)} 的 K 线")
        price = number(max(candles, key=lambda r: int(r["t"]))["c"], "HL 收盘时刻价格")
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

    async def refresh(self, force: bool = False) -> bool | None:
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
    """A cash index level with its previous close and session status."""
    name: str
    last: D
    prev_close: D | None
    open: D | None
    high: D | None
    low: D | None
    quoted_ms: int
    source: str
    status: str = ""  # e.g. "交易中" / "已收盘"

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
    quoted_ms = now_ms
    with contextlib.suppress(ValueError, TypeError):
        quoted_ms = int(dt.datetime.fromisoformat(str(item.get("localTradedAt"))).timestamp() * 1000)
    status = {"OPEN": "交易中", "CLOSE": "已收盘", "PREOPEN": "盘前"}.get(str(item.get("marketStatus", "")).upper(), "")
    return IndexQuote(str(item.get("stockName") or "KOSPI"), last, prev, naver_number(item.get("openPrice")),
                      naver_number(item.get("highPrice")), naver_number(item.get("lowPrice")), quoted_ms, "Naver", status)


def parse_eastmoney_index(raw: bytes, now_ms: int, name: str) -> IndexQuote:
    d = parse_eastmoney_quote(raw)
    quoted_ms = int(d["f86"]) * 1000 if str(d.get("f86", "")).isdigit() else now_ms
    return IndexQuote(str(d.get("f58") or name), number(d["f43"], name), _opt(d.get("f60")), _opt(d.get("f46")),
                      _opt(d.get("f44")), _opt(d.get("f45")), quoted_ms, "东方财富")


def krx_session(now_ms: int) -> str:
    local = dt.datetime.fromtimestamp(now_ms / 1000, dt.timezone(dt.timedelta(hours=9))).time()
    return "交易中" if dt.time(9, 0) <= local <= dt.time(15, 30) else "已收盘"


def a50_session(now_ms: int) -> str:
    """FTSE China A50 futures (SGX): day 09:00-16:30, night 16:45-05:15 Beijing time.

    Friday's night session ends Saturday 05:15; Sunday night has no session.
    """
    moment = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
    local, weekday = moment.time(), moment.weekday()
    if weekday == 6 or (weekday == 5 and local >= dt.time(5, 15)) or (weekday == 0 and local < dt.time(9, 0)):
        return "休市"
    if local >= dt.time(16, 45) or local < dt.time(5, 15):
        return "夜盘"
    if dt.time(9, 0) <= local <= dt.time(16, 30):
        return "日盘"
    return "休市"


def a50_next_open(now_ms: int) -> int | None:
    """When A50 trading resumes after a closure that outlasts the 16:30-16:45 break; None while it trades
    or during that break (the day session's last print still stands for those 15 minutes)."""
    if a50_session(now_ms) != "休市":
        return None
    local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
    day, clock, weekday = local.date(), local.time(), local.weekday()
    if weekday < 5 and dt.time(16, 30) < clock < dt.time(16, 45):
        return None
    if weekday >= 5:  # Saturday after 05:15 or Sunday: Monday's day session
        day += dt.timedelta(days=7 - weekday)
    return int(dt.datetime.combine(day, dt.time(9, 0), BEIJING).timestamp() * 1000)


def a50_last_session_end(now_ms: int) -> int:
    """Most recent SGX A50 session end, including Friday night's Saturday morning close."""
    local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
    day, clock, weekday = local.date(), local.time(), local.weekday()
    if weekday < 5 and dt.time(16, 30) <= clock < dt.time(16, 45):
        end_day, end_time = day, dt.time(16, 30)
    elif weekday == 6 or (weekday == 0 and clock < dt.time(9, 0)):
        end_day, end_time = day - dt.timedelta(days=1 if weekday == 6 else 2), dt.time(5, 15)
    elif weekday == 5:
        end_day, end_time = day, dt.time(5, 15)
    else:  # Weekday morning gap after the night session.
        end_day, end_time = day, dt.time(5, 15)
    return int(dt.datetime.combine(end_day, end_time, BEIJING).timestamp() * 1000)


def a50_code_ok(code: Any) -> bool:
    """Eastmoney labels SGX A50 contracts CN00Y (continuous), CN2610, ...; an empty code is tolerated,
    anything else (another contract) is not."""
    code = str(code or "").strip().upper()
    return not code or code.startswith("CN")


def parse_sina_bars(raw: bytes) -> list[tuple[str, D]]:
    """Sina GlobalService.getMink JSONP: [{"d": "2026-09-24 15:00:00", "o": .., "h": .., "l": .., "c": .., "v": ..}, ...]
    -> [("2026-09-24 15:00", close)], oldest first. Tolerates either quoted or bare keys and numbers."""
    text = raw.decode("utf-8", errors="replace")
    bars = []
    for obj in re.findall(r"\{[^{}]*\}", text):
        when = re.search(r'"?d(?:ay|ate)?"?\s*:\s*"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})', obj)
        close = re.search(r'"?c(?:lose)?"?\s*:\s*"?([\d.]+)', obj)
        if when and close:
            bars.append((when.group(1), number(close.group(1), "A50")))
    if not bars:
        raise ValueError("新浪 A50 5分钟K 格式异常或为空")
    return sorted(bars)


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


def parse_cn_daily_ohlc(source: str, raw: bytes) -> list[tuple[dt.date, D | None, D]]:
    """As parse_cn_daily, with each bar's open (None when missing)."""
    try:
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
    except (ValueError, KeyError, TypeError, AttributeError) as error:
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


def quote_stale(q: IndexQuote, now_ms: int, limit_ms: int, lunch: tuple[dt.time, dt.time] | None = None) -> bool:
    """True when a live quote has not updated within ``limit_ms`` (a lunch break pauses the clock)."""
    age = now_ms - q.quoted_ms
    if lunch:
        day = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING).date()
        start, end = (int(dt.datetime.combine(day, t, BEIJING).timestamp() * 1000) for t in lunch)
        age -= max(0, min(end, now_ms) - max(start, q.quoted_ms))
    return age > limit_ms


class CnIndex:
    """Shanghai Composite (000001) plus FTSE China A50 futures (SGX) as its after-hours proxy.

    Composite: Tencent → Sina → Eastmoney. A50: Eastmoney 104.CN00Y (month-continuous contract)
    then Sina's CFD as a labelled last resort. Refreshed every 60 s, best effort.
    The close used as the after-hours reference comes only from a dated daily bar (Tencent,
    then Eastmoney): a realtime "last price" says nothing reliable about which session it closed.
    """
    REFRESH_SECONDS = 60
    DAILY_SECONDS = 600       # re-read the daily bars this often once the expected close is confirmed
    STALE_MS = 10 * 60_000    # a live quote older than this is not used for new probabilities
    DAILY_SOURCES = (("腾讯日K", "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,,,40,",
                      {"Referer": "https://gu.qq.com/"}),
                     ("东方财富日K", "https://push2his.eastmoney.com/api/qt/stock/kline/get?klt=101&fqt=0&end=20500101"
                                    "&lmt=40&fields1=f1,f2,f3&fields2=f51,f52,f53&secid=1.000001",
                      {"Referer": "https://quote.eastmoney.com/"}))
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

    async def _first(self, sources: tuple, parse, now_ms: int, previous) -> tuple[Any, str, str]:
        """(result, error when every source failed, failures of the sources tried before the one that answered)."""
        failures = []
        for name, url, extra in SOURCE_HEALTH.order(sources):
            try:
                raw = await fetch_source(url, extra)
                return parse(name, raw, now_ms), "", "；".join(failures)
            except Exception as error:
                failures.append(f"{name}: {clean_error(error)}")
        return previous, "；".join(failures), ""

    async def refresh(self, now_ms: int, force: bool = False) -> bool | None:
        if not self.enabled or (not force and time.monotonic() - self.refreshed < self.REFRESH_SECONDS):
            return False  # not due yet: nothing fetched
        self.refreshed = time.monotonic()
        self.quote, self.error, _ = await self._first(self.SSE_SOURCES, parse_cn_index, now_ms, self.quote)
        self.a50, self.a50_error, self.a50_skipped = await self._first(self.A50_SOURCES, self.parse_a50, now_ms, self.a50)
        if self.a50_skipped:
            LOG.info("A50 fell back to %s: %s", self.a50.source if self.a50 else "-", self.a50_skipped)
        await self.refresh_daily(now_ms, force)

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

    async def refresh_daily(self, now_ms: int, force: bool = False) -> None:
        """Read the dated daily bars: every refresh while the expected close is unconfirmed, else every 10 min."""
        if not force and self.confirmed(now_ms) and time.monotonic() - self.daily_refreshed < self.DAILY_SECONDS:
            return
        self.daily_refreshed = time.monotonic()
        failures = []
        for name, url, extra in SOURCE_HEALTH.order(self.DAILY_SOURCES):
            try:
                raw = await fetch_source(url, extra)
                ohlc = [bar for bar in parse_cn_daily_ohlc(name, raw) if bar[0] <= dt.datetime.fromtimestamp(now_ms / 1000, BEIJING).date()]
                bars = [(day, close) for day, _, close in ohlc]
                day, close, prev = last_completed_bar(bars, STOCK_MARKETS["sh"], now_ms)
            except Exception as error:
                failures.append(f"{name}: {clean_error(error)}")
                continue
            if self.close is None or day >= self.close.day:  # never step back to an older session
                self.close = DailyClose(day, close, prev, name, now_ms)
            self.bars, self.opens, self.daily_error = bars, {day: o for day, o, _ in ohlc if o}, ""
            return
        self.daily_error = "；".join(failures)

    def live_stale(self, now_ms: int) -> bool:
        return self.quote is not None and quote_stale(self.quote, now_ms, self.STALE_MS, (dt.time(11, 30), dt.time(13, 0)))

    def a50_stale(self, now_ms: int) -> bool:
        """Reject an old live quote, including one that predates the last completed A50 session."""
        if self.a50 is None:
            return False
        if a50_session(now_ms) != "休市":
            return quote_stale(self.a50, now_ms, self.STALE_MS)
        # A thin final stretch (the live Sina feed's last Friday-night print was 04:56 for a 05:15 close) is fine;
        # a print from before the last session's final hour belongs to an older session.
        return self.a50.quoted_ms < a50_last_session_end(now_ms) - 60 * 60_000

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
        line += f"｜{stamp(q.quoted_ms, seconds=False)} {q.source}" + stale_note(q.quoted_ms, now_ms, BEIJING)
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
            label = "上证收盘时" if anchor_note == "15:00" else "上证收盘附近"
            line += f" → {label} {bold(fmt(anchor))} {pct_text(percent(a.last, anchor), style)}"
            if anchor_note != "15:00":
                line += f"（{anchor_note}）"
        if a.prev_close:
            line += f"｜昨结 {fmt(a.prev_close)} {pct_text(percent(a.last, a.prev_close), style)}"
        source = a.source if "CFD" not in a.source else f"{a.source}·非交易所合约，仅参考"
        line += f"｜{stamp(a.quoted_ms, seconds=False)} {source}" + stale_note(a.quoted_ms, now_ms, BEIJING)
        if self.a50_stale(now_ms):
            line += "｜⚠️ 报价已超 10 分钟未更新"
        if self.a50_skipped and not self.a50_error:
            line += f"｜⚠️ 前序源未取到：{brief_error(self.a50_skipped, 90)}"
        return line + f"｜⚠️ 刷新失败：{brief_error(self.a50_error)}" if self.a50_error else line


class KospiIndex:
    """KOSPI composite index: Naver's realtime index feed first, Eastmoney (100.KS11) as fallback."""
    REFRESH_SECONDS = 60
    SOURCES = (("Naver", "https://polling.finance.naver.com/api/realtime/domestic/index/KOSPI",
                {"Referer": "https://finance.naver.com/"}),
               ("东方财富", IndexFutures.EM + "100.KS11", {"Referer": "https://quote.eastmoney.com/"}))
    SOURCES_200 = (("Naver", "https://polling.finance.naver.com/api/realtime/domestic/index/KPI200",
                    {"Referer": "https://finance.naver.com/"}),)

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.quote: IndexQuote | None = None
        self.quote200: IndexQuote | None = None  # KOSPI 200, the index behind Hyperliquid's KR200 perp
        self.error = ""
        self.error200 = ""
        self.refreshed = -1e9

    @staticmethod
    def parse(source: str, raw: bytes, now_ms: int) -> IndexQuote:
        if source == "Naver":
            return parse_naver_index(raw, now_ms)
        return parse_eastmoney_index(raw, now_ms, "韩国KOSPI")

    async def refresh(self, now_ms: int, force: bool = False) -> bool | None:
        if not self.enabled or (not force and time.monotonic() - self.refreshed < self.REFRESH_SECONDS):
            return False  # not due yet: nothing fetched
        self.refreshed = time.monotonic()
        self.quote, self.error = await self._fetch(self.SOURCES, now_ms, self.quote)
        self.quote200, self.error200 = await self._fetch(self.SOURCES_200, now_ms, self.quote200)

    async def _fetch(self, sources: tuple, now_ms: int, previous: IndexQuote | None) -> tuple[IndexQuote | None, str]:
        failures = []
        for name, url, extra in SOURCE_HEALTH.order(sources):
            try:
                raw = await fetch_source(url, extra)
                return self.parse(name, raw, now_ms), ""
            except Exception as error:
                failures.append(f"{name}: {clean_error(error)}")
        return previous, "；".join(failures)

    def line(self, now_ms: int, style: str) -> str:
        if not self.enabled:
            return ""
        q = self.quote
        if q is None:
            return f"🇰🇷 KOSPI ⚠️ 获取失败（{self.error}）" if self.error else "🇰🇷 KOSPI ⏳ 等待首次获取"
        status = q.status or krx_session(now_ms)
        line = f"🇰🇷 {bold('KOSPI ' + status)} {bold(fmt(q.last))}"
        if q.change is not None and q.prev_close:
            line += f" → 昨收 {bold(fmt(q.prev_close))} {pct_text(q.change / q.prev_close * 100, style)}（{q.change:+,.2f}）"
        kst = dt.timezone(dt.timedelta(hours=9))
        line += f"｜{stamp(q.quoted_ms, seconds=False)} {q.source}" + stale_note(q.quoted_ms, now_ms, kst)
        return line + f"｜⚠️ 刷新失败：{brief_error(self.error)}" if self.error else line

    def line200(self, now_ms: int, style: str, hl: "HlQuote | None", hl_note: str = "") -> str:
        """KOSPI 200 versus Hyperliquid's KR200 perp, the 24/7 price for the same index."""
        if not self.enabled:
            return ""
        q = self.quote200
        if q is None:
            return f"🇰🇷 KOSPI200 ⚠️ 获取失败（{brief_error(self.error200)}）" if self.error200 else "🇰🇷 KOSPI200 ⏳ 等待首次获取"
        status = q.status or krx_session(now_ms)
        line = f"🇰🇷 {bold('KOSPI200 ' + status)} {bold(fmt(q.last))}"
        if q.change is not None and q.prev_close:
            line += f" → 昨收 {bold(fmt(q.prev_close))} {pct_text(q.change / q.prev_close * 100, style)}"
        if hl is not None:
            meta = f"（24h {pct_text(hl.day_change, style)}）" if hl.day_change is not None else ""
            line += f"｜🌊 HL {hl.coin.split(':')[-1]} {bold(fmt(hl.mark))} → 相对 KOSPI200 {pct_text(percent(hl.mark, q.last), style)}{meta}"
        elif hl_note:
            line += f"｜🌊 HL {brief_error(hl_note, 40)}"
        kst = dt.timezone(dt.timedelta(hours=9))
        line += f"｜{stamp(q.quoted_ms, seconds=False)} {q.source}" + stale_note(q.quoted_ms, now_ms, kst)
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
                for a, b in SESSIONS[market]) / 60
    today = local.date()
    trading_today = today.weekday() < 5 and today not in holidays
    finalised = local >= dt.datetime.combine(today, info.close_time, tz) + dt.timedelta(minutes=15)
    if trading_today and not finalised and (close_date is None or close_date < today):
        remaining = sum(max(0.0, (dt.datetime.combine(today, b, tz) - max(dt.datetime.combine(today, a, tz), local)).total_seconds())
                        for a, b in SESSIONS[market]) / 60
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
    if local < dt.datetime.combine(day, info.close_time, tz) + dt.timedelta(minutes=15):
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

    @property
    def direct(self) -> bool:
        """The effective price is the underlying's own live print, not a proxy-mapped estimate."""
        return "直接用现货" in self.proxy_note

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
                f"（有效 {fmt(self.effective.quantize(D('0.01')))}·σ {self.sigma * 100:.2f}%）")

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
                     up, max(0.0, 1 - up - down), down, unit, beta, mode)


PRED_EVERY_MS = 30 * 60_000   # one saved prediction snapshot per index per 30 minutes
CALIB_MIN_DAYS = 10           # walk-forward: target days used only for training before the first test day


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
            r["hit"] = 1.0 if r["close"] > r["ref"] else 0.5 if r["close"] == r["ref"] else 0.0
            r["y"] = math.log(r["close"] / r["ref"])

        def scores(ps: list[float], hits: list[float]) -> tuple[float, float]:
            brier = sum((p - h) ** 2 for p, h in zip(ps, hits)) / len(ps)
            loss = -sum(h * math.log(min(max(p, 1e-6), 1 - 1e-6)) + (1 - h) * math.log(min(max(1 - p, 1e-6), 1 - 1e-6))
                        for p, h in zip(ps, hits)) / len(ps)
            return brier, loss
        brier, loss = scores([r["up"] for r in done], [r["hit"] for r in done])
        lines.append(f"  现行模型：Brier {brier:.3f}（抛硬币 0.250）｜对数损失 {loss:.3f}（0.693）")
        bins = []
        for lo in (0.0, 0.2, 0.4, 0.6, 0.8):
            sel = [r for r in done if lo <= r["up"] < lo + 0.2 or (lo == 0.8 and r["up"] == 1.0)]
            if sel:
                bins.append(f"{lo * 100:.0f}–{lo * 100 + 20:.0f}%：预测 {sum(r['up'] for r in sel) / len(sel) * 100:.0f}% "
                            f"实际 {sum(r['hit'] for r in sel) / len(sel) * 100:.0f}%（{len(sel)} 条/{len({r['target'] for r in sel})} 日）")
        lines.append("  校准：" + "；".join(bins))
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
PREDICT_META_SECONDS = 600     # outcome names / status of a ladder market are re-read this often
PREDICT_STRIKE_SECONDS = 300   # a known target price is re-read this often (the site may correct it)
PREDICT_MISS_SECONDS = 60      # an unknown slug is looked up again after this long (new markets show up within a minute)
PREDICT_DEPTH = 5
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
                              "title": str(node.get("title") or node.get("question") or "")})
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


def cap_target(title: str) -> D | None:
    """'$200M' / '↑ 1B' / 'Will X hit $1.5B?' -> 200000000 / 1000000000 / 1500000000; None when absent."""
    match = re.search(r"(\d+(?:\.\d+)?)\s*([KMB])\b", str(title).replace(",", ""), re.I)
    if not match:
        return None
    return D(match.group(1)) * {"K": D(10) ** 3, "M": D(10) ** 6, "B": D(10) ** 9}[match.group(2).upper()]


@dataclass(frozen=True)
class BookEdge:
    """One way to trade the book: side 涨/跌, maker (挂) or taker (吃), the 涨/跌-denominated price and model edge."""
    side: str
    maker: bool
    price: float
    edge: float        # model fair price − price paid, per share (1.0 = $1)
    size: float        # shares resting at the level this refers to

    @property
    def label(self) -> str:
        return f"{'挂' if self.maker else '吃'}{self.side}"


def book_edges(fair_up: float, book: PredictBook) -> list[BookEdge]:
    """挂涨 = rest a bid at 买1, 挂跌 = rest a 跌 bid at 1 − 卖1, 吃涨 = buy at 卖1, 吃跌 = buy 跌 at 1 − 买1."""
    fair_down = 1 - fair_up
    edges = []
    if book.bid:
        bid, size = float(book.bid[0]), float(book.bid[1])
        edges += [BookEdge("涨", True, bid, fair_up - bid, size), BookEdge("跌", False, 1 - bid, fair_down - (1 - bid), size)]
    if book.ask:
        ask, size = float(book.ask[0]), float(book.ask[1])
        edges += [BookEdge("跌", True, 1 - ask, fair_down - (1 - ask), size), BookEdge("涨", False, ask, fair_up - ask, size)]
    return sorted(edges, key=lambda e: (not e.maker, e.side != "涨"))


def best_edge(edges: list[BookEdge]) -> BookEdge | None:
    """The side with the largest positive model edge (maker wins ties: it also collects the spread)."""
    good = [e for e in edges if e.edge > 0.0005]
    return max(good, key=lambda e: (round(e.edge, 4), e.maker)) if good else None


WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def day_fields(target: dt.date, now_ms: int) -> dict:
    """Web card date badge: '09-29 周二' plus 今天 / 明天 / 后天 / 下周一 relative to Beijing today."""
    today = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING).date()
    ahead = (target - today).days
    week = WEEKDAYS[target.weekday()]
    tag = {0: "今天", 1: "明天", 2: "后天"}.get(ahead) or (
        ("下" if target.isocalendar()[1] != today.isocalendar()[1] else "本") + week if 0 < ahead < 14 else "")
    return {"day": target.isoformat(), "day_label": f"{target:%m-%d} {week}", "day_tag": tag, "day_ahead": ahead}


def cents(value: float, sign: bool = False) -> str:
    return f"{value * 100:+.1f}¢" if sign else f"{value * 100:.1f}¢"


def book_lines(book: PredictBook | None, error: str, odds: "CloseOdds | str | None", now_ms: int,
               url: str = "") -> list[str]:
    """Lines for Telegram (bold sentinels, send as HTML): quote, four edges, the best one, the market link."""
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
    edges = book_edges(odds.fair_up, book)
    if not edges:
        lines.append("盘口为空，无法比较")
        return lines
    lines.append(f"模型 涨 {cents(odds.fair_up)}｜跌 {cents(odds.fair_down)}")
    for maker in (True, False):
        row = [f"{e.label} {cents(e.price)} 优势 {bold(cents(e.edge, True)) if e.edge > 0 else cents(e.edge, True)}"
               for e in edges if e.maker == maker]
        if row:
            lines.append("｜".join(row))
    best = best_edge(edges)
    if book.stale(now_ms):
        lines.append("盘口过期，不给建议")
    elif odds.warn:
        lines.append(f"⚠️ {odds.warn}，暂不给建议")
    elif best:
        how = "挂单排队，成交不保证" if best.maker else f"立即成交，卖1/买1 只有 {best.size:g} 份"
        lines.append(f"👉 {bold(best.label)} @ {cents(best.price)} 优势最大 {cents(best.edge, True)}（{how}）")
    else:
        lines.append("👉 四个方向对模型都没有正优势，暂不挂")
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
        self.ladder_keys: set[str] = set()                       # item keys whose category holds several Yes/No markets
        self.ladders: dict[str, list[LadderRow]] = {}            # item key -> one row per market, by threshold
        self.refreshed = -1e9

    def headers(self) -> dict[str, str]:
        return {"x-api-key": self.config.predict_api_key} if self.config.predict_api_key else {}

    async def fetch(self, url: str, payload: dict | None = None) -> Any:
        raw = await _blocking(_http_get, url, payload, SOURCE_TIMEOUT, self.headers() if url.startswith(PREDICT_REST) else {})
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
        return {"outcomes": [str(o.get("name") or "") for o in rows], "created_ms": created, "status": str(market.get("status") or "")}

    async def market_info(self, slug: str, market_id: str) -> None:
        details = await self.market_details(market_id)
        self.info[slug] = {"outcomes": details["outcomes"], "created_ms": details["created_ms"]}

    async def ladder_row(self, key: str, slug: str, market: dict) -> "LadderRow | None":
        target = cap_target(market.get("title", ""))
        if target is None:
            return None
        meta = self.market_meta.get(market["id"])
        if meta is None or time.monotonic() - meta[1] > PREDICT_META_SECONDS:
            with contextlib.suppress(RemoteError, TimeoutError, OSError):
                self.market_meta[market["id"]] = (await self.market_details(market["id"]), time.monotonic())
        try:
            bids, asks, _ = await self.orderbook(market)
        except (RemoteError, TimeoutError, OSError) as error:
            return LadderRow(target, market["id"], market["title"], None, clean_error(error) or type(error).__name__)
        book = PredictBook(key, slug, market["id"], market["title"], bids, asks, int(time.time() * 1000))
        return LadderRow(target, market["id"], market["title"], book, "")

    async def refresh_ladder(self, key: str, slug: str) -> None:
        """A category of Yes/No markets, one per threshold: every market's book, sorted by threshold."""
        markets = await self.resolve_all(slug)
        if not markets:
            self.ladders.pop(key, None)
            self.errors[key] = f"Predict 上还没有这个市场（{slug}）"
            return
        rows = [row for row in await asyncio.gather(*(self.ladder_row(key, slug, m) for m in markets)) if row]
        if not rows:
            raise RemoteError("Predict 市场标题里没有可识别的市值档位")
        self.ladders[key] = sorted(rows, key=lambda row: row.target)
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
            bids, asks, _ = await self.orderbook(market)
            self.books[key] = PredictBook(key, slug, market["id"], market["title"], bids, asks, int(time.time() * 1000))
            self.errors.pop(key, None)
        except (RemoteError, TimeoutError, OSError) as error:
            self.errors[key] = clean_error(error) or type(error).__name__

    async def refresh(self, targets: dict[str, str], force: bool = False) -> bool:
        """targets: item key -> slug. False = not due yet."""
        if not force and time.monotonic() - self.refreshed < self.config.predict_poll:
            return False
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
        await asyncio.gather(*(self.refresh_one(key, slug) for key, slug in targets.items()))
        return True


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

    def __init__(self, store: "Store", spec: TouchSpec):
        self.store, self.spec = store, spec
        self.price: D | None = None
        self.priced_ms = 0
        self.sigma: float | None = None
        self.error = ""
        self.times = {"price": -1e9, "vol": -1e9, "scan": -1e9}
        self.start_ms = spec.created_ms  # market creation (Predict, else the rules), 0 = unknown

    async def get(self, path: str, **params: Any) -> Any:
        query = urllib.parse.urlencode(params)
        failures = []
        for base in TOUCH_SPOT:
            try:
                return json.loads(await fetch_source(f"{base}/api/v3/{path}?{query}"))
            except Exception as error:
                failures.append(f"{urllib.parse.urlsplit(base).hostname}: {clean_error(error)}")
        raise RemoteError("；".join(failures))

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
            if mono - self.times["vol"] >= self.VOL_SECONDS or self.sigma is None:
                self.times["vol"] = mono
                # 722: the newest bar is the hour still running, which realized_vol drops; 721 finished bars remain
                self.sigma = realized_vol(await self.get("klines", symbol=self.spec.symbol, interval="1h", limit=722), now_ms)
            if self.start_ms and mono - self.times["scan"] >= self.SCAN_SECONDS:
                self.times["scan"] = mono
                await self.scan(now_ms)
            self.error = ""
        except Exception as error:
            self.error = clean_error(error) or type(error).__name__

    async def scan(self, now_ms: int) -> None:
        """Extend the barrier check from where it stopped (persisted) up to now."""
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
        self.store.put(f"touch:{self.spec.slug}", {"kind": "clear", "through": cursor, "start": self.start_ms})

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
        return first_touch(float(self.price), float(self.spec.low), float(self.spec.high), self.sigma, 0.0,
                           max(0.0, (self.spec.deadline_ms - max(now_ms, self.start_ms)) / YEAR_MS))

    def status(self) -> str:
        hist = self.history
        kind = hist.get("kind")
        if kind in {"low", "high"}:
            return f"已于 {stamp(hist['time'], seconds=False)} 先触及 ${int(self.spec.low if kind == 'low' else self.spec.high)}"
        if kind == "ambiguous":
            return f"需人工核对：{stamp(hist['time'], seconds=False)} 这一分钟无法判断先后（高 {hist['hi']:g}·低 {hist['lo']:g}）"
        if kind == "clear":
            return f"开盘以来未触线（核至 {stamp(hist['through'], seconds=False)}）"
        return "开盘以来是否触线：待核验" if self.start_ms else "开盘时间未知：按此前未触线计算"


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
)
BSC_RPC = ("https://bsc-dataseed.bnbchain.org", "https://bsc-dataseed.binance.org", "https://bsc-rpc.publicnode.com")
BURN_ADDRESSES = ("0x000000000000000000000000000000000000dead", "0x0000000000000000000000000000000000000000")
GECKO = "https://api.geckoterminal.com/api/v2/networks"


def hit_probability(spot: float, level: float, sigma: float, years: float) -> float:
    """P(the running maximum reaches ``level`` before ``years``) for a zero-drift GBM (log drift −σ²/2):
    Φ((−h − s²/2)/s) + (S/K)·Φ((−h + s²/2)/s), h = ln(K/S), s = σ√T."""
    if spot >= level:
        return 1.0
    if years <= 0 or sigma <= 0:
        return 0.0
    h, s = math.log(level / spot), sigma * math.sqrt(years)
    return min(1.0, norm_cdf((-h - s * s / 2) / s) + spot / level * norm_cdf((-h + s * s / 2) / s))


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


def gecko_pool(data: Any) -> str:
    """GeckoTerminal token-pools answer -> the address of the pool with the most liquidity."""
    rows = (data or {}).get("data") if isinstance(data, dict) else None
    best = None
    for row in rows or []:
        attrs = (row or {}).get("attributes") or {}
        try:
            reserve = float(attrs.get("reserve_in_usd") or 0)
        except (TypeError, ValueError):
            reserve = 0.0
        if attrs.get("address") and (best is None or reserve > best[0]):
            best = (reserve, str(attrs["address"]))  # Solana addresses are case-sensitive
    if best is None:
        raise ValueError("GeckoTerminal 没有这个代币的池子")
    return best[1]


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
    SUPPLY_SECONDS = 600
    VOL_SECONDS = 3600
    SCAN_SECONDS = 300
    RETRY_SECONDS = 300   # after a failed σ request (GeckoTerminal allows ~30 calls a minute): wait, never hammer
    SIGMA_KEEP_MS = 24 * 3600_000  # a saved σ stands in for this long while fresh bars cannot be fetched

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
        self.error = ""
        self.times = {"price": -1e9, "supply": -1e9, "vol": -1e9, "scan": -1e9}

    async def get(self, url: str, payload: dict | None = None) -> Any:
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

    async def ohlcv(self, frame: str, before_s: int, limit: int) -> list[tuple[int, float, float, float, float]]:
        base = f"{GECKO}/{self.spec.gecko}"
        if not self.pool:
            self.pool = self.spec.pair or gecko_pool(await self.get(f"{base}/tokens/{self.spec.token}/pools?page=1"))
        return gecko_bars(await self.get(f"{base}/pools/{self.pool}/ohlcv/{frame}?aggregate=1&limit={limit}"
                                         f"&before_timestamp={before_s}&currency=usd&token={self.spec.token}"))

    def observe(self, now_ms: int) -> None:
        """Keep the highest price the bot itself has seen inside the window (persisted with the scan)."""
        if self.price is None or not self.spec.start_ms <= now_ms <= self.spec.end_ms:
            return
        hist = dict(self.history) or {"start": self.spec.start_ms, "high": 0.0, "at": 0}
        if float(self.price) > float(hist.get("high") or 0):
            hist["high"], hist["at"] = float(self.price), now_ms // 1000
            self.store.put(f"cap:{self.spec.slug}", hist)

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
        if mono - self.times["price"] >= self.PRICE_SECONDS:
            self.times["price"] = mono
            try:
                url = (f"https://api.dexscreener.com/latest/dex/pairs/{self.spec.chain}/{self.spec.pair}" if self.spec.pair
                       else f"https://api.dexscreener.com/tokens/v1/{self.spec.chain}/{self.spec.token}")
                self.price, self.dex_cap, self.dex_fdv, self.source = dex_price(await self.get(url), self.spec.token)
                self.source = f"DexScreener {self.source}".strip()
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
        if not self.spec.gecko:
            self.sigma, self.sigma_note = self.spec.prior_sigma, "先验：没有 K 线来源"
        elif mono - self.times["vol"] >= self.VOL_SECONDS:
            self.times["vol"] = mono
            self.pool = ""  # look the most liquid pool up again (a token can move pools)
            try:
                bars = [b for b in await self.ohlcv("hour", now_ms // 1000, 1000) if b[0] + 3600 <= now_ms // 1000][-721:]
                if len(bars) < 49:
                    raise ValueError(f"小时 K 线只有 {len(bars)} 根，不足 2 天")
                rets = [math.log(bars[i][4] / bars[i - 1][4]) for i in range(1, len(bars)) if bars[i][4] > 0 and bars[i - 1][4] > 0]
                mean = sum(rets) / len(rets)
                self.sigma = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1) * 24 * 365)
                self.sigma_note = f"{len(rets) / 24:.0f} 日小时收盘"
                self.store.put(f"capsigma:{self.spec.slug}", [self.sigma, self.sigma_note, now_ms])
            except Exception as error:
                self.times["vol"] = mono - self.VOL_SECONDS + self.RETRY_SECONDS  # try again in 5 minutes
                saved = self.store.get(f"capsigma:{self.spec.slug}")
                with contextlib.suppress(TypeError, ValueError, IndexError):
                    if self.sigma is None and now_ms - int(saved[2]) < self.SIGMA_KEEP_MS:
                        self.sigma, self.sigma_note = float(saved[0]), f"{saved[1]}，{stamp(int(saved[2]), seconds=False)} 保存"
                if self.sigma is None:
                    failures.append(f"波动率：{clean_error(error)}")
        if self.spec.gecko and mono - self.times["scan"] >= self.SCAN_SECONDS:
            self.times["scan"] = mono
            try:
                await self.scan(now_ms)
            except Exception as error:
                failures.append(f"窗口最高：{clean_error(error)}")
        self.error = "；".join(failures)

    async def scan(self, now_ms: int) -> None:
        """Extend the window's highest price (persisted) with the hours finished since the last scan."""
        start_s, end_s = self.spec.start_ms // 1000, min(now_ms, self.spec.end_ms) // 1000
        if end_s <= start_s:
            return
        hist = dict(self.history) or {"start": self.spec.start_ms, "high": 0.0, "at": 0}
        hist.setdefault("through", start_s - start_s % 3600 + (3600 if start_s % 3600 else 0))
        if start_s % 3600 and "first" not in hist:
            # the window opens mid-hour: that hour counts only from the opening minute
            try:
                bars = [b for b in await self.ohlcv("minute", start_s - start_s % 3600 + 3600, 60) if b[0] >= start_s]
                hist["first"] = "done" if bars else "skipped"
                for bar in bars:
                    if bar[2] > hist["high"]:
                        hist["high"], hist["at"] = bar[2], bar[0]
            except Exception:
                hist["first"] = "skipped"
        rows: list[tuple[int, float, float, float, float]] = []
        before = end_s
        for _ in range(10):  # 1000 hours per page, newest first
            page = await self.ohlcv("hour", before, 1000)
            rows += page
            if not page or page[0][0] <= hist["through"] or len(page) < 1000:
                break
            before = page[0][0]
        finished = [b for b in rows if b[0] >= hist["through"] and b[0] + 3600 <= end_s]
        running = [b for b in rows if b[0] + 3600 > end_s and b[0] >= hist["through"]]
        for bar in finished:
            if bar[2] > hist["high"]:
                hist["high"], hist["at"] = bar[2], bar[0]
        if finished:
            hist["through"] = max(b[0] for b in finished) + 3600
        self.hour_high = max((b[2] for b in running), default=0.0)
        self.store.put(f"cap:{self.spec.slug}", hist)

    def window_high(self) -> tuple[D | None, int]:
        """Highest market cap in the window so far (persisted hours, the running hour, the live price)."""
        hist = self.history
        prices = [(float(hist.get("high") or 0), int(hist.get("at") or 0)), (self.hour_high, 0)]
        if self.price is not None:
            prices.append((float(self.price), 0))
        top, at = max(prices)
        supply = self.supply or (self.dex_cap / self.price if self.dex_cap and self.price else None)
        return (D(str(top)) * supply if top and supply else None), at

    def probability(self, target: D, now_ms: int) -> float | None:
        cap, (high, _) = self.cap, self.window_high()
        if high is not None and high >= target:
            return 1.0
        if cap is None or self.sigma is None:
            return None
        years = max(0.0, (self.spec.end_ms - max(now_ms, self.spec.start_ms)) / YEAR_MS)
        return hit_probability(float(cap), float(target), self.sigma, years)


# --- read-only probability web page -------------------------------------------------------------
WEB_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>收盘涨跌概率</title>
<style>
:root{--best:#2d6cdf;--best-bg:#eef3fd;--warn:#c77c00;--bg:#f4f5f7;--card:#fff;--text:#1b1f23;--muted:#6b737c;--faint:#9aa3ad;--line:#e5e8ec;--up:#d63b3b;--down:#1e9a54;--flat:#b8c0c8;--chip:#f0f2f5;--hot:#e02424;--hot-bg:#fdecec}
@media (prefers-color-scheme:dark){:root{--bg:#101215;--card:#1a1d21;--text:#e8eaed;--muted:#9aa3ad;--faint:#6b737c;--line:#2a2f35;--chip:#23272c;--best:#6f9ef0;--best-bg:#1c2a42;--warn:#e0a040;--hot:#ff5a5a;--hot-bg:#3a1c1e}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1560px;margin:0 auto;padding:12px 16px}
header{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:4px 16px;margin-bottom:4px}
h1{font-size:18px;margin:0}.hr{display:flex;flex-wrap:wrap;align-items:center;gap:6px 14px}.tog{display:inline-flex;align-items:center;gap:5px;font-size:12px;color:var(--muted);background:var(--card);border:1px solid var(--line);border-radius:999px;padding:2px 9px 2px 6px;cursor:pointer;user-select:none}.tog input{margin:0;accent-color:var(--best)}body.nobook .pb{display:none}.meta{color:var(--muted);font-size:12px;display:flex;flex-wrap:wrap;gap:2px 10px}
.legend{color:var(--muted);font-size:12px;margin:0 0 6px;display:flex;flex-wrap:wrap;align-items:center;gap:2px 12px}.legend .sw{white-space:nowrap}.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin:0 3px 0 6px;vertical-align:-1px}.legend .sw i:first-child{margin-left:0}.legend .hot i{background:var(--hot)}
h2{font-size:12px;font-weight:600;color:var(--muted);letter-spacing:.04em;margin:10px 2px 6px}
.grid{display:grid;gap:10px;align-items:stretch;grid-template-columns:repeat(auto-fill,minmax(280px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 12px 8px;display:flex;flex-direction:column;gap:6px;min-width:0}
.card.hot{border:2px solid var(--hot);box-shadow:0 0 0 3px var(--hot-bg);padding:9px 11px 7px}
.card.rolled{border-color:var(--best);box-shadow:0 0 0 1px var(--best)}
.head{display:flex;align-items:center;gap:5px}
.star{flex:none;border:0;background:none;padding:0;margin:0 -2px 0 -1px;font-size:14px;line-height:1;cursor:pointer;color:var(--faint)}.star.on{color:#f5b301}.star:hover{color:#f5b301}
.name{font-weight:650;font-size:14.5px;min-width:0;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tag{border-radius:6px;padding:1px 5px;font-size:12px;font-weight:600;background:var(--chip);color:var(--muted);white-space:nowrap;font-variant-numeric:tabular-nums}
.tag.next{background:var(--best-bg);color:var(--best)}.tag.new{background:var(--best);color:#fff}.tag.hotk{background:var(--hot);color:#fff}
.cd{font-size:12px;font-variant-numeric:tabular-nums;white-space:nowrap;background:var(--chip);border-radius:999px;padding:1px 7px}
.cd.done{color:var(--muted)}.cd.soon{color:var(--warn);font-weight:600}
.odds{display:flex;align-items:center;gap:8px;font-variant-numeric:tabular-nums}
.odds b{font-size:22px;font-weight:700;letter-spacing:-.01em;white-space:nowrap}.odds .lbl{color:var(--muted);font-size:12px;font-weight:400;margin:0 3px}
.u{color:var(--up)}.d{color:var(--down)}
.bar{flex:1;display:flex;height:6px;border-radius:3px;overflow:hidden;background:var(--line)}.bar i{display:block;height:100%}
details{font-size:12.5px}summary{cursor:pointer;list-style:none;display:flex;align-items:baseline;gap:4px;color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap;min-width:0}
summary>*{flex:none}summary .v{color:var(--text)}summary .un{color:var(--faint);font-size:11px}summary .rd{color:var(--faint);font-size:12px}summary .chip{margin-left:auto}
summary::-webkit-details-marker{display:none}summary:before{content:"▸";color:var(--faint)}details[open] summary:before{content:"▾"}
summary .v{color:var(--text)}.chip{font-size:12px;border-radius:6px;padding:0 5px;background:var(--chip);font-weight:600}
dl{display:grid;grid-template-columns:auto 1fr;gap:2px 10px;margin:6px 0 2px;font-size:12px}dt{color:var(--muted)}dd{margin:0;word-break:break-word;font-variant-numeric:tabular-nums}
.card.missing p{margin:0;color:var(--muted);font-size:12.5px}
.pb{margin-top:auto;border-top:1px solid var(--line);padding-top:6px;font-size:12.5px;font-variant-numeric:tabular-nums}
a.pb{display:block;color:inherit;text-decoration:none;border-radius:8px;margin:auto -6px 0;padding:6px 6px 4px;cursor:pointer}
a.pb:hover,a.pb:active{background:var(--chip)}a.pb:hover .edge,a.pb:active .edge{background:var(--card)}
.quote{display:flex;flex-wrap:wrap;align-items:baseline;gap:0 10px;color:var(--muted)}.quote span{white-space:nowrap}.quote b{color:var(--text);font-weight:600}.quote .pt{font-weight:600;color:var(--best)}
.edges{display:grid;grid-template-columns:repeat(4,1fr);gap:4px;margin-top:5px}
.edge{display:flex;flex-direction:column;align-items:center;background:var(--chip);border:1px solid transparent;border-radius:7px;padding:3px 2px;min-width:0;line-height:1.25;overflow:hidden}
.edge .el{color:var(--faint);font-size:11px;white-space:nowrap;max-width:100%;overflow:hidden;text-overflow:ellipsis}.edge .el i{font-style:normal;font-variant-numeric:tabular-nums}
.edge b{font-weight:650;font-size:14px;color:var(--faint);white-space:nowrap;font-variant-numeric:tabular-nums}.edge.pos b{color:var(--text)}.edge.pos .el{color:var(--muted)}
.edge.best{border-color:var(--best);background:var(--best-bg)}.edge.best .el{color:var(--text)}.edge.best b{color:var(--best)}
.edge.hot{border-color:var(--hot);background:var(--hot-bg)}.edge.hot b{color:var(--hot)}
.quote .qe{white-space:normal;word-break:break-all}
.grid.wide{grid-template-columns:repeat(auto-fill,minmax(min(100%,520px),520px));align-items:start}
.card.lad .name{flex:0 1 auto}.card.lad .cd{margin-left:6px}
.lg{display:grid;grid-template-columns:auto auto auto auto auto 1fr;gap:3px 12px;margin-top:5px;font-size:12.5px;font-variant-numeric:tabular-nums;align-items:baseline}
.lg .lh{color:var(--faint);font-size:11px;white-space:nowrap}.lg .lr{text-align:right}.lg .ln{text-align:right;white-space:nowrap}.lg .ld{color:var(--muted)}
.lg .lt{font-weight:650;white-space:nowrap}.lg .lq{color:var(--muted);white-space:nowrap}
.touched{display:flex;flex-wrap:wrap;align-items:center;gap:4px 6px;font-size:12px}.touched .k{color:var(--down);font-weight:600}
.tchip{border-radius:6px;padding:0 6px;background:var(--chip);color:var(--muted);font-variant-numeric:tabular-nums}
.lg .lb{color:var(--faint);white-space:nowrap}.lg .lb.pos{color:var(--text)}.lg .lb.pos b{color:var(--best)}.lg .lb.hot,.lg .lb.hot b{color:var(--hot)}.lg .lk b{color:var(--faint)}.lg .lk.pos b{color:var(--best)}.lg .lz{color:var(--faint);font-size:11px;margin-left:3px}
@media (max-width:560px){.lg{gap:3px 7px;font-size:12px}.lg .ld,.lg .lz,.lg .lp{display:none}.lg{grid-template-columns:auto auto auto auto 1fr}}
.small{font-size:12px;margin-top:3px}.warn{color:var(--warn)}footer{color:var(--faint);font-size:11.5px;margin-top:14px;line-height:1.6;max-width:760px}
</style></head><body><div class="wrap">
<header><h1>收盘涨跌概率</h1><div class="hr"><div class="meta" id="meta">加载中…</div><label class="tog"><input type="checkbox" id="showbook" checked>显示 Predict 盘口</label></div></header>
<div class="legend" id="legend"></div>
<h2 id="h-fav" hidden>⭐ 收藏</h2><div class="grid" id="g-fav"></div>
<h2 id="h-index">指数</h2><div class="grid" id="g-index"></div>
<h2 id="h-contract">合约标的</h2><div class="grid" id="g-contract"></div>
<h2 id="h-crypto">加密</h2><div class="grid" id="g-crypto"></div>
<h2 id="h-ladder">市值阶梯</h2><div class="grid wide" id="g-ladder"></div>
<footer id="foot">模型参考，非投资建议。</footer>
</div>
<script>
const $=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e};
const open=new Set(),seen={},rolled={};let skew=0,fetchedAt=0,style="cn",last=null;
// saved in this browser only (localStorage), keyed by the ticker (or the index name) so a renamed card keeps its star
const favKey=it=>it.symbol||it.name;let favs=[];try{favs=JSON.parse(localStorage.getItem("favs")||"[]")}catch(e){}
if(!Array.isArray(favs))favs=[];
favs=[...new Set(favs.filter(k=>typeof k==="string").map(k=>k.includes("|")?(k.split("|")[1]||k.split("|")[0]):k))];  // old "name|symbol" keys
function toggleFav(k){favs=favs.includes(k)?favs.filter(x=>x!==k):[...favs,k];try{localStorage.setItem("favs",JSON.stringify(favs))}catch(e){}if(last)render(last)}
const two=n=>String(n).padStart(2,"0"),pct=x=>(x*100).toFixed(1),cent=x=>(x*100).toFixed(1)+"¢";
const like=(v,ref)=>{const d=(String(ref).split(".")[1]||"").length,n=Number(String(v).replace(/,/g,""));
  if(!isFinite(n))return v;const k=d>=2?d:(Math.abs(n)>=1000?0:2);return n.toLocaleString("en-US",{minimumFractionDigits:k,maximumFractionDigits:k})};
const qty=n=>Number(n).toLocaleString("en-US",{maximumFractionDigits:n>=100?0:n>=10?1:2});
const HOT=0.1;  // an edge this large (10¢) gets the red frame
function book(p){
  const w=$("a","pb");w.href=p.url;w.target="_blank";w.rel="noopener noreferrer";w.title="打开 Predict 市场";
  const q=$("div","quote");q.append($("span","pt","Predict ↗"));w.append(q);
  const has=p.bids||p.asks;
  if(has){const b=p.bids[0],k=p.asks[0],lv=(t,l)=>{const x=$("span","",t+" ");x.append($("b","",l?cent(l[0]):"无"));if(l)x.append("×"+qty(l[1]));return x};
    q.append(lv("买1",b),lv("卖1",k));if(b&&k)q.title="价差 "+cent(k[0]-b[0]);
    if(p.stale)q.append($("span","warn",p.age+" 秒前"))}
  else if(!p.error)q.append($("span","","等待获取"));
  if(p.error)w.append($("div","warn small",(has?"刷新失败，显示上次盘口：":"")+p.error));
  if(p.edges&&p.edges.length){const g=$("div","edges");
    p.edges.forEach(e=>{const x=$("div","edge"+(e.best?" best":"")+(e.best&&e.edge>=HOT?" hot":"")+(e.edge>0?" pos":""));
      x.title=e.maker?"挂单排队，成交不保证":"立即成交，量 "+qty(e.size);
      const el=$("span","el",e.label+" ");el.append($("i","",(e.price*100).toFixed(1)));x.title=e.label+" @ "+cent(e.price)+"："+x.title;
      x.append(el,$("b","",(e.edge>=0?"+":"")+cent(e.edge)));g.append(x)});
    w.append(g);if(p.stale)w.append($("div","warn small","盘口过期，不给建议"))}
  return w}
function upColor(){return style==="us"?"var(--down)":"var(--up)"}function downColor(){return style==="us"?"var(--up)":"var(--down)"}
function ladder(c,it){
  c.classList.add("lad");
  // a market-cap ladder: one Yes/No market per threshold; reached ones fold into one line, open ones get a row each
  const L=it.ladder,det=$("details");det.open=open.has(it.name);det.addEventListener("toggle",()=>{det.open?open.add(it.name):open.delete(it.name)});
  const sm=$("summary");sm.title="点开看计算明细";
  sm.append($("span","rd",L.metric),$("span","v",L.cap),$("span","rd","窗口最高"),$("span","v",L.high));
  if(L.sigma)sm.append($("span","rd","σ"),$("span","v",(L.sigma*100).toFixed(0)+"%"));
  det.append(sm);const dl=$("dl");const row=(k,v)=>dl.append($("dt","",k),$("dd","",v));
  row("窗口",L.window+" → "+it.close_label);row("价格",L.price+" USD（"+L.source+"）");row("供应量",L.supply+"（"+L.supply_note+"）");
  row("窗口最高",L.high+(L.high_at?"（"+L.high_at+"）":"")+"："+(L.bars?"GeckoTerminal 小时 K 近似":"只含机器人运行以来看到的价格")+"，结算以 "+L.settle+" 1 分钟 K 为准；Predict 已结算的档位算已触及"+(L.first_skipped?"；开窗首个半小时的分钟 K 未取得，未计入":""));
  if(L.sigma)row("σ",(L.sigma*100).toFixed(0)+"%（"+L.sigma_note+"）｜剩 "+(L.years*365).toFixed(1)+" 天");
  row("模型",(L.bars?"":"σ 为先验值（这条链没有 K 线来源），仅供参考。")+"碰到即 Yes：零漂移、固定波动率的单边触及概率 Φ((−h−s²/2)/s) + (M/K)·Φ((−h+s²/2)/s)，h = ln(K/M)，s = σ√T");
  det.append(dl);c.append(det);
  if(it.missing)c.append($("p","","概率暂缺："+it.missing));else if(L.error)c.append($("div","warn small","⚠️ "+L.error));
  if(!L.bars&&!it.missing)c.append($("div","warn small","⚠️ 无 K 线：σ 为先验 "+(L.sigma*100).toFixed(0)+"%，优势仅供参考"));
  const done=L.rows.filter(r=>r.touched),live=L.rows.filter(r=>!r.touched);
  if(done.length){const t=$("div","touched");t.append($("span","k","✓ 已触及"));
    const tip=done.map(r=>r.label+(r.bid!=null||r.ask!=null?"（盘口 "+(r.bid==null?"无":(r.bid*100).toFixed(1))+" / "+(r.ask==null?"无":(r.ask*100).toFixed(1))+"）":"")).join("、");
    const shown=done.length>3?[{label:"≤ "+done[done.length-1].label+" · "+done.length+" 档"}]:done;  // many levels: one chip
    shown.forEach(r=>{const x=$("span","tchip",r.label);x.title="窗口内"+L.metric+"已达到："+tip;t.append(x)});
    c.append(t)}
  const w=it.predict?$("a","pb"):$("div","pb");
  if(it.predict){w.href=it.predict.url;w.target="_blank";w.rel="noopener noreferrer";w.title="打开 Predict 市场"}
  const h=$("div","quote");h.append($("span","pt","Predict ↗"));if(it.predict&&it.predict.error)h.append($("span","warn qe",it.predict.error));
  w.append(h);let hot=null;
  if(live.length){const g=$("div","lg");
    const hd=(t,cl,tip)=>{const x=$("span","lh"+(cl?" "+cl:""),t);if(tip)x.title=tip;return x};
    g.append(hd("目标"),hd("距离","ln ld","还要涨多少才碰到"),hd("模型","ln","模型给 Yes 的公平价"),hd("买1 / 卖1","","Yes 的盘口"),hd("最优"),hd("吃单","","立即成交的较优一边：吃Yes@卖1 或 吃No@1−买1，× 为卖1/买1 的量"));
    live.forEach(r=>{const best=r.edges&&r.edges.find(e=>e.best),n=x=>x==null?"无":(x*100).toFixed(1);
      const q=r.bid==null&&r.ask==null?(r.error?"—":"…"):n(r.bid)+" / "+n(r.ask);
      const b=$("span","lb"+(best&&L.bars?(best.edge>=HOT?" hot":" pos"):""));
      if(best){b.append(best.label+" "+(best.price*100).toFixed(1)+" ",$("b","","+"+cent(best.edge)));b.title=best.maker?"挂单排队，成交不保证":"立即成交，量 "+qty(best.size);
        if(!L.bars)b.title="σ 是先验值，这个优势只作参考、不提醒";
        else if(best.edge>=HOT&&(!hot||best.edge>hot.edge))hot={...best,row:r.label}}
      else b.textContent=r.error?"⚠️":r.stale?"过期":"—";
      if(r.error)b.title=r.error;
      const tk=r.edges&&r.edges.filter(e=>!e.maker).sort((x,y)=>y.edge-x.edge)[0],t=$("span","lb lk"+(tk&&tk.edge>0&&L.bars&&!r.stale?" pos":""));
      if(tk){t.append(tk.label+" ",$("span","lp",(tk.price*100).toFixed(1)+" "),$("b","",(tk.edge>=0?"+":"")+cent(tk.edge)),$("span","lz","×"+qty(tk.size)));t.title=tk.label+" @ "+cent(tk.price)+"，立即成交，最多 "+qty(tk.size)+" 份"+(r.stale?"（盘口过期）":"")}
      else t.textContent="—";
      g.append($("span","lt",r.label),$("span","ln ld",r.dist==null?"—":"+"+(r.dist*100).toFixed(0)+"%"),$("span","ln",r.fair==null?"—":cent(r.fair)),$("span","lq",q),b,t)});
    w.append(g)}
  c.append(w);
  if(hot){c.classList.add("hot");c.title="优势 ≥10¢："+hot.row+" "+hot.label+" @ "+cent(hot.price)+" +"+cent(hot.edge)}
  return c}
function card(it){
  const c=$("div","card"+(it.missing?" missing":"")),head=$("div","head"),nm=$("div","name",it.name);
  nm.title=it.symbol||it.name;const fk=favKey(it),on=favs.includes(fk),st=$("button","star"+(on?" on":""),on?"★":"☆");
  st.type="button";st.title=on?"取消收藏":"收藏（排到最前）";st.setAttribute("aria-label",st.title);st.addEventListener("click",e=>{e.preventDefault();toggleFav(fk)});
  head.append(st,nm);
  if(it.day){const t=$("span","tag"+(it.day_ahead>0?" next":""),(it.day_tag?it.day_label.split(" ")[0]+" "+it.day_tag:it.day_label));t.title="交易日 "+it.day_label;head.append(t);
    const k=it.name+"|"+(it.symbol||"");if(seen[k]&&seen[k]<it.day)rolled[k]=Date.now();seen[k]=it.day;
    if(rolled[k]&&Date.now()-rolled[k]<600000){c.classList.add("rolled");t.className="tag new";t.textContent+=" 新"}}
  if(it.close_ms){const cd=$("span","cd");cd.dataset.close=it.close_ms;cd.title="目标 "+it.close_label;head.append(cd)}
  c.append(head);
  if(it.kind==="ladder")return ladder(c,it);
  const best=it.predict&&it.predict.edges&&it.predict.edges.find(e=>e.best);
  if(best&&best.edge>=HOT){c.classList.add("hot");c.title="优势 ≥10¢："+best.label+" @ "+cent(best.price)+" +"+cent(best.edge)}
  if(it.missing){c.append($("p","","概率暂缺："+it.missing));if(it.predict)c.append(book(it.predict));return c}
  const o=$("div","odds"),a=$("b",style==="us"?"d":"u"),b=$("b",style==="us"?"u":"d");
  const lb=it.labels||["涨","跌"];a.append($("span","lbl",lb[0]),pct(it.fair_up)+"¢");b.append(pct(it.fair_down)+"¢",$("span","lbl",lb[1]));
  const bar=$("div","bar");[[it.up,upColor()],[it.flat,"var(--flat)"],[it.down,downColor()]].forEach(([w,col])=>{const i=$("i");i.style.width=(w*100)+"%";i.style.background=col;bar.append(i)});
  o.append(a,bar,b);c.append(o);
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
    if(it.predict)c.append(book(it.predict));return c}
  const det=$("details");det.open=open.has(it.name);det.addEventListener("toggle",()=>{det.open?open.add(it.name):open.delete(it.name)});
  const sm=$("summary");sm.title=(it.ref_day?it.ref_day+" 收盘 → 有效价":"参考 → 有效价")+"；点开看计算明细";
  sm.append($("span","rd",it.ref_day||"参考"),$("span","v",it.ref),$("span","","→"),$("span","v",like(it.effective,it.ref)));if(it.unit)sm.append($("span","un",it.unit));
  const chip=$("span","chip",(it.move>=0?"+":"")+it.move.toFixed(2)+"%");chip.style.color=it.move>0?upColor():it.move<0?downColor():"var(--muted)";sm.append(chip);det.append(sm);
  const dl=$("dl");const row=(k,v)=>dl.append($("dt","",k),$("dd","",v));
  row("目标",it.close_label);row("参考",it.ref+unit+"（"+it.ref_note+"）");row("有效",it.effective+unit);row("代理",it.proxy_note);
  row("σ","日 "+(it.sigma_daily*100).toFixed(2)+"% × √"+it.remaining.toFixed(3)+" = "+(it.sigma*100).toFixed(2)+"%");
  row("σ 来源",it.sigma_note);row("涨/平/跌",(it.up*100).toFixed(2)+"% / "+(it.flat*100).toFixed(2)+"% / "+(it.down*100).toFixed(2)+"%");row("z",it.z.toFixed(3));
  det.append(dl);c.append(det);if(it.warn){const w=$("div","warn small","⚠️ "+it.warn);c.append(w)}
  if(it.predict)c.append(book(it.predict));return c}
function tick(){
  const now=Date.now()+skew;
  document.querySelectorAll(".cd").forEach(el=>{const left=Math.floor((Number(el.dataset.close)-now)/1000);
    if(left<=0){el.className="cd done";el.textContent="已到收盘";return}
    const d=Math.floor(left/86400),h=Math.floor(left%86400/3600),m=Math.floor(left%3600/60),s=left%60;
    el.className="cd"+(left<1800?" soon":"");el.textContent="⏳ "+(d?d+"天 ":"")+two(h)+":"+two(m)+":"+two(s)});
  const ago=document.getElementById("ago");if(ago&&fetchedAt)ago.textContent=Math.max(0,Math.round((Date.now()-fetchedAt)/1000))+" 秒前刷新"}
function render(d){
  // starred cards leave their own section for the one on top, in the order they were starred
  const starred=favs.map(k=>d.items.find(i=>favKey(i)===k)).filter(Boolean);
  document.getElementById("g-fav").replaceChildren(...starred.map(card));document.getElementById("h-fav").hidden=!starred.length;
  for(const g of["index","contract","crypto","ladder"]){const items=d.items.filter(i=>(i.group||"contract")===g&&!favs.includes(favKey(i)));
    document.getElementById("g-"+g).replaceChildren(...items.map(card));document.getElementById("h-"+g).hidden=!items.length}
  tick()}
async function load(){
  try{
    const r=await fetch(location.pathname.replace(/\\/$/,"")+"/data.json",{cache:"no-store"});
    if(!r.ok)throw new Error("HTTP "+r.status);
    const d=await r.json();if(d.server_ms)skew=d.server_ms-Date.now();fetchedAt=Date.now();style=d.color_style||"cn";
    last=d;render(d);
    document.getElementById("meta").replaceChildren(...(d.today?[$("span","","今天 "+d.today)]:[]),$("span","","数据 "+d.generated_at),$("span","","",),$("span","","基准 "+d.mode),$("span","","v"+d.version));
    document.getElementById("meta").children[d.today?2:1].id="ago";
    const lg=document.getElementById("legend");const sw=$("span","sw");[["涨",upColor()],["平","var(--flat)"],["跌",downColor()]].forEach(([t,col])=>{const i=$("i");i.style.background=col;sw.append(i,t)});
    const hot=$("span","sw hot");hot.append($("i"),"红框 = 优势 ≥10¢");
    const rule=$("span","","¢ 公平价 · 优势 = 公平价 − 成交价");rule.title="挂涨@买1 · 挂跌@1−卖1 · 吃涨@卖1 · 吃跌@1−买1；平盘两边各半";
    lg.replaceChildren(sw,rule,hot);
    document.getElementById("foot").textContent=d.note;tick();
  }catch(e){document.getElementById("meta").replaceChildren($("span","warn","刷新失败："+e.message+"，稍后自动重试"))}
}
const sb=document.getElementById("showbook");try{sb.checked=localStorage.getItem("showbook")!=="0"}catch(e){}
const applyBook=()=>document.body.classList.toggle("nobook",!sb.checked);applyBook();
sb.addEventListener("change",()=>{applyBook();try{localStorage.setItem("showbook",sb.checked?"1":"0")}catch(e){}});
load();setInterval(load,10000);setInterval(tick,1000);
</script></body></html>"""


class WebServer:
    """Tiny read-only HTTP server (stdlib asyncio) for the probability page.

    Routes: /health, /p/<token> (HTML), /p/<token>/data.json (JSON). Everything else is 404, the
    token is compared in constant time, and responses are no-store with a restrictive CSP.
    """
    MAX_HEADER_BYTES = 8192

    def __init__(self, bot: "Bot", port: int, token: str):
        self.bot, self.port, self.token = bot, port, token
        self.server: asyncio.base_events.Server | None = None

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
        if len(parts) in {2, 3} and parts[0] == "p" and hmac.compare_digest(parts[1], self.token):
            if len(parts) == 2:
                return 200, "text/html; charset=utf-8", WEB_PAGE.encode("utf-8")
            if parts[2] == "data.json":
                body = json.dumps(self.bot.odds_payload(), ensure_ascii=False).encode("utf-8")
                return 200, "application/json; charset=utf-8", body
        return 404, "text/plain; charset=utf-8", b"not found"

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
            if len(head) > self.MAX_HEADER_BYTES:
                raise ValueError("header too large")
            method, target, _ = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ", 2)
            status, ctype, body = self.route(method.upper(), urllib.parse.urlsplit(target).path)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ValueError):
            status, ctype, body, method = 400, "text/plain; charset=utf-8", b"bad request", "GET"
        except Exception as error:  # Never let a page request touch the bot's loops.
            LOG.warning("web request failed: %s", clean_error(error))
            status, ctype, body, method = 500, "text/plain; charset=utf-8", b"error", "GET"
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed", 500: "Internal Server Error"}[status]
        headers = (f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                   "Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\n"
                   "X-Robots-Tag: noindex\r\nContent-Security-Policy: default-src 'self'; style-src 'unsafe-inline'; "
                   "script-src 'unsafe-inline'; img-src 'none'; frame-ancestors 'none'\r\nConnection: close\r\n\r\n")
        with contextlib.suppress(Exception):
            writer.write(headers.encode("latin-1") + (b"" if method.upper() == "HEAD" else body))
            await writer.drain()
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
        reason = "首次/重新超过阈值" if not s.get("side") else "方向反转并超过阈值"
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
               style: str = "cn", context: list[str] | None = None) -> str:
    """Alert body with bold sentinels; send it with html=True. ``context`` = extra rows (HL, indices)."""
    side = "上涨" if change > 0 else "下跌"
    rows = [f"基准 {fmt(base.value)}（{baseline_brief(base)}）→ {pct_text(change, style, strong=True, digits=3)}"]
    rows += [reference_row(kind, quote.price, ref, fx, style) for kind, ref in (references or {}).items()]
    rows += [line for line in (context or []) if line]
    rows.append(f"📝 {reason}")
    return "\n".join([f"{trend_mark(change, style)} {bold(f'{side}超过 {fmt(threshold)}%｜{NAMES.get(symbol, symbol)}')}（{symbol}）",
                      quote.price_row(quote.timestamp_ms), *tree(rows),
                      "⚠️ 合约行情提示，不代表股票官方收盘结算结果。"])


class Telegram:
    def __init__(self, token: str):
        self.root = f"https://api.telegram.org/bot{token}/"
        self.lock = asyncio.Lock()
        self.next_send = 0.0

    async def call(self, method: str, payload: dict | None = None, timeout: int = 15) -> Any:
        data = await http_json(self.root + method, payload or {}, timeout)
        if not isinstance(data, dict) or not data.get("ok"):
            msg = data.get("description", "未知错误") if isinstance(data, dict) else "响应格式异常"
            retry = data.get("parameters", {}).get("retry_after", 0) if isinstance(data, dict) else 0
            raise RemoteError(f"Telegram: {clean_error(msg)}", int(retry))
        return data.get("result")

    async def paced(self, method: str, payload: dict) -> Any:
        """Serialize outgoing messages and respect Telegram's per-chat send rate."""
        async with self.lock:
            await asyncio.sleep(max(0, self.next_send - time.monotonic()))
            try:
                return await self.call(method, payload)
            except RemoteError as error:
                self.next_send = time.monotonic() + max(1.1, error.retry_after)
                raise
            finally:
                self.next_send = max(self.next_send, time.monotonic() + 1.1)

    async def send(self, chat: int, thread: int, text: str, reply_markup: dict | None = None,
                   parse_mode: str | None = None) -> None:
        # Plain text by default; HTML only for messages that were escaped with to_html().
        chunks = split_text(text)
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

    async def edit(self, chat: int, message_id: int, text: str, reply_markup: dict | None = None) -> None:
        """Rewrite a card in place after a button press so it reflects the new selection."""
        payload: dict[str, Any] = {"chat_id": chat, "message_id": message_id, "text": split_text(text)[0],
                                   "link_preview_options": {"is_disabled": True}}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        await self.paced("editMessageText", payload)


def split_text(text: str, limit: int = 3400) -> list[str]:
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit)
        cut = cut if cut > 0 else limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return chunks or [""]


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
    Command("cooldown", "持续超标每 300 秒提醒（0=关闭周期提醒）", "300"),
    Command("mode", "daily=币安上一 UTC 日日 K 收盘；exchange=币安合约在证券交易所收盘时刻的价格；manual=手动参考价",
            "daily|exchange|manual"),
    Command("setclose", "设置手动参考价，可一次发多条", "UNITREE 75 09-17 16:00",
            "示例：75 是基准，09-17 16:00 是它的收盘时间（北京，可省略）\n"
            "  末尾再写 YYYY-MM-DD 可指定适用日（默认今天）；批量：每行一组，首行可写统一适用日"),
    Command("setexchange", "记录证券交易所收盘价，可带货币（对照显示）", "SKHYNIX 258000 KRW 09-17 14:30",
            "带 HKD/CNY/KRW 等非美元货币时只展示不算涨跌；不带货币按同口径算相对涨跌"),
    Command("pause", "暂停当前订阅"),
    Command("resume", "恢复当前订阅"),
    Command("prob", "查看各标的下个收盘涨跌概率及计算过程"),
    Command("book", "对比 Predict 订单簿和模型公平价，看挂涨还是挂跌优势大"),
    Command("web", "获取概率网页链接（自动刷新）"),
    Command("calib", "用已保存的预测快照和实际收盘给概率模型打分（Brier/校准/逐日向前拟合）"),
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
        self.kospi = KospiIndex(config.kospi_index)
        self.cn = CnIndex(config.sse_index, config.holidays.get("sh", frozenset()))
        self.web_token = config.web_token or self.store.get("web_token") or ""
        if config.web_port and not self.web_token:
            self.web_token = secrets.token_urlsafe(18)
            self.store.put("web_token", self.web_token)
        self.web: WebServer | None = None
        self.vols = VolBook(config.prob_vol)
        self.predict = PredictFeed(config)
        self.touches = {spec.key: TouchMarket(store, spec) for spec in TOUCH_MARKETS}
        self.caps = {spec.key: CapMarket(store, spec) for spec in CAP_MARKETS}
        if config.touch:
            self.predict.want_info.update(spec.slug for spec in TOUCH_MARKETS)
            self.predict.ladder_keys.update(self.caps)
        self.exchange_bases: dict[str, Baseline] = {}  # exchange_close mode: held until a newer close is confirmed
        self.reference_tasks: list[asyncio.Task] = []
        self.reference_pool: ThreadPoolExecutor | None = None
        self.reference_state: dict[str, dict] = {}  # per background feed: last success, last error, duration
        self.anchors: dict[str, tuple[int, D]] = {}  # key -> (reference close ms, proxy price then)
        self.kospi_anchor_note, self.kospi_anchor_error = "", ""
        self.pred_last: dict[str, int] = {}  # index -> ms of the last saved prediction snapshot
        self.a50_anchor_note = "15:00"
        self.a50_anchor_error = ""  # why the last anchor lookup failed (shown in /diag and the odds row)
        self.a50_anchor_source = "东方财富"
        self.anchor_tries: dict[str, float] = {}
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
            "/book": self.cmd_book,
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
                   html_mode: bool = False) -> bool:
        try:
            if html_mode:
                await self.telegram.send(chat, thread, to_html(text), reply_markup, "HTML")
            else:
                await self.telegram.send(chat, thread, text, reply_markup)
            return True
        except Exception as error:
            self.log_limited("telegram_send", f"Telegram 发送失败：{clean_error(error)}")
            return False

    async def notice(self, sub_id: str, sub: dict, key: str, error: str | None) -> None:
        record_key = f"notice:{sub_id}:{key}"
        old = self.store.get(record_key, {})
        now = time.time()
        if error:
            if old.get("active") and now - old.get("sent", 0) < 1800:
                return
            text = f"⚠️ 行情监控异常｜{key}\n{error}\n该项暂停涨跌提醒；恢复后继续。\n这不代表价格没有变化。"
            if await self.tell(sub["chat"], sub["thread"], text):
                self.store.put(record_key, {"active": True, "sent": now})
        elif old.get("active"):
            if await self.tell(sub["chat"], sub["thread"], f"✅ 数据恢复｜{key}\n后续按当前基准继续监控。"):
                self.store.put(record_key, {"active": False, "sent": now})

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
            reply = handler(req) if handler else "未知命令。发送 /help 查看用法。"
            if inspect.isawaitable(reply):
                reply = await reply
            if isinstance(reply, tuple):  # (text, inline keyboard) card
                reply, markup = reply
            elif isinstance(reply, Reply):
                reply, markup, html_mode = reply.text, reply.markup, reply.html
        except (ValueError, decimal.InvalidOperation) as error:
            reply = "❌ " + clean_error(error)
        await self.tell(req.chat, req.thread, reply, markup, html_mode)

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
            if kind != "threshold":
                raise ValueError("未知操作，请重新发送命令")
            toast = self.apply_threshold(value)
            text, markup = threshold_card(D(self.settings()["threshold"]))
        except (ValueError, decimal.InvalidOperation) as error:
            await answer("❌ " + clean_error(error), alert=True)
            return
        await answer(toast)
        if message.get("message_id") and message.get("chat"):
            try:
                await self.telegram.edit(int(message["chat"]["id"]), int(message["message_id"]), text, markup)
            except RemoteError as error:
                # "message is not modified" when re-selecting the current preset is harmless.
                if "not modified" not in str(error):
                    self.log_limited("telegram_edit", f"卡片更新失败：{clean_error(error)}")

    async def public_id_notice(self, req: Request) -> None:
        now = time.monotonic()
        if now - self.command_notice.get(req.user_id, -1e9) < 10:
            return
        if len(self.command_notice) > 2000:
            self.command_notice.clear()
        self.command_notice[req.user_id] = now
        await self.tell(req.chat, req.thread, id_text(req.user_id, req.chat, req.thread) +
                        "\n把你的用户 ID 填入 Railway 的 ADMIN_USER_ID 后重新部署，再发 /subscribe。")

    # --- command handlers: each returns the reply text or raises ValueError with the usage hint ---

    def cmd_help(self, req: Request) -> str:
        return HELP

    def cmd_id(self, req: Request) -> str:
        return id_text(req.user_id, req.chat, req.thread)

    def cmd_status(self, req: Request) -> "Reply":
        return Reply(self.status(req.sub_id), html=True)

    def cmd_test(self, req: Request) -> str:
        return "✅ TG 测试消息发送成功。\n此测试仅验证推送，行情是否正常请看 /status。"

    def cmd_subscribe(self, req: Request) -> str:
        self.set_subscription(req, active=True)
        return ("✅ 当前私聊/话题已订阅。\n" + self.config_summary() +
                "\n首次观察就超过阈值，也会提醒；请用 /status 核对基准和行情。")

    def cmd_resume(self, req: Request) -> str:
        self.set_subscription(req, active=True)
        return "✅ 当前订阅已恢复。\n" + self.config_summary()

    def cmd_pause(self, req: Request) -> str:
        self.set_subscription(req, active=False)
        return "⏸ 当前订阅已暂停。"

    def cmd_unsubscribe(self, req: Request) -> str:
        subs = self.subscriptions()
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
        self.store.delete_prefix("alert:")
        return f"✅ 全局阈值已改为严格超过 ±{fmt(value)}%。下一轮按新阈值判断。"

    def cmd_cooldown(self, req: Request) -> str:
        if len(req.args) != 1 or not req.args[0].isdigit() or not 0 <= int(req.args[0]) <= 86400:
            raise ValueError("用法：/cooldown 300，范围 0～86400 秒；0 关闭周期重复提醒")
        seconds = int(req.args[0])
        self.update_settings(cooldown=seconds)
        return f"✅ 周期重复提醒间隔：{seconds} 秒（0 表示关闭）。"

    def cmd_mode(self, req: Request) -> str:
        choice = req.args[0].lower() if len(req.args) == 1 else ""
        aliases = {"daily": "binance_daily", "binance_daily": "binance_daily", "manual": "manual",
                   "exchange": "exchange_close", "exchange_close": "exchange_close", "stock": "exchange_close"}
        if choice not in aliases:
            raise ValueError("用法：/mode daily、/mode exchange 或 /mode manual")
        settings = self.update_settings(mode=aliases[choice])
        self.snapshots.clear()
        self.store.delete_prefix("alert:")
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

    async def refresh_odds_inputs(self, now_ms: int) -> None:
        """Proxy prices at each reference close and volatility estimates (cached; best effort)."""
        for symbol, ticker in self.config.tickers.items():
            self.note_live_close(symbol, ticker, now_ms)
            close_ms = self.odds_base(symbol, now_ms)[0]
            if close_ms and self.anchors.get(symbol, (0,))[0] != close_ms and self.retry_ok(symbol):
                with contextlib.suppress(Exception):
                    price, _ = await self.market.price_at(symbol, close_ms)
                    self.anchors[symbol] = (close_ms, price)
            if self.vols.due(symbol):
                self.vols.refreshed[symbol] = time.monotonic()  # one attempt per window even if it fails
                with contextlib.suppress(Exception):
                    rows = await self.market.get("/fapi/v1/klines", symbol=symbol, interval="1d", limit=31)
                    closes = [number(r[4], "收盘") for r in rows[:-1] if isinstance(r, list) and len(r) > 4]
                    self.vols.record(symbol, closes, "币安日K")
        kospi, hl = self.kospi.quote, self.hl.quotes.get("KR200")
        if kospi and hl:
            await self.kospi_anchor(kospi, hl)
        q = self.hsi.quote
        if q and q.spot is not None:
            close_date = self.hk_cash_close_date(now_ms, self.hsi.holidays)
            local = dt.datetime.fromtimestamp(q.quoted_ms / 1000, BEIJING)
            close_ms = int(dt.datetime.combine(close_date, dt.time(16, 10), BEIJING).timestamp() * 1000)
            # First futures print after the cash close = the futures level the close is anchored to.
            if (q.session_name(self.hsi.holidays) != "夜市" and local.date() == close_date and local.time() >= dt.time(16, 10)
                    and self.anchors.get("HSI", (0,))[0] != close_ms):
                self.anchors["HSI"] = (close_ms, q.last)

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
                        if (len(saved) >= 4 and saved[2] in
                                {"15:00", "15:00 五分钟K近似", "15:00 后五分钟首笔近似"}
                                and saved[3] in {"东方财富", "新浪CFD"}):
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
                        price, note = D(str(printed[1])), "15:00 后五分钟首笔近似"  # recorded when it happened
                if price is None and a50 and 0 <= a50.quoted_ms - close_ms <= 5 * 60_000:
                    price, note = a50.last, "15:00 后五分钟首笔近似"
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

    def record_predictions(self, now_ms: int) -> None:
        """Save what each index model saw and said (every 30 min), and each official close as it becomes
        known, so the model can later be scored against real outcomes (/calib)."""
        for key, odds_of in (("SSE", self.sse_odds), ("KOSPI", self.kospi_odds), ("HSI", self.hsi_odds)):
            odds = odds_of(now_ms)
            if not isinstance(odds, CloseOdds) or now_ms - self.pred_last.get(key, -PRED_EVERY_MS) < PRED_EVERY_MS:
                continue
            self.pred_last[key] = now_ms
            self.store.put(f"pred:{key}:{now_ms}", {
                "key": key, "t": now_ms, "target": odds.target.isoformat(), "mode": odds.mode, "ref": float(odds.ref),
                "eff": float(odds.effective), "move": odds.move, "beta": odds.beta, "sigma": odds.sigma_daily,
                "R": odds.remaining, "up": odds.fair_up, "proxy": odds.proxy_note})
        if self.cn.close:
            self.store.put(f"outcome:SSE:{self.cn.close.day.isoformat()}", float(self.cn.close.value))
        k = self.kospi.quote
        kst = dt.timezone(dt.timedelta(hours=9))
        if k and dt.datetime.fromtimestamp(k.quoted_ms / 1000, kst).time() >= dt.time(15, 30):
            self.store.put(f"outcome:KOSPI:{dt.datetime.fromtimestamp(k.quoted_ms / 1000, kst).date().isoformat()}", float(k.last))
        q, local = self.hsi.quote, dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
        if (q and q.spot is not None and not self.hsi.error and local.time() >= dt.time(16, 15)
                and self.hk_cash_close_date(now_ms, self.hsi.holidays) == local.date()):
            self.store.put(f"outcome:HSI:{local.date().isoformat()}", float(q.spot))

    def calibration_text(self) -> str:
        preds = [v for _, v in self.store.items("pred:")]
        outcomes = {k.removeprefix("outcome:"): float(v) for k, v in self.store.items("outcome:")}
        return "\n".join(["📐 概率模型回测（只评估，不会自动改参数）"] + calibration_report(preds, outcomes))

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

    def sse_odds(self, now_ms: int) -> CloseOdds | str | None:
        q = self.cn.quote
        if not self.config.probability or not self.config.sse_index:
            return None
        holidays = self.config.holidays.get("sh", frozenset())
        sigma, sigma_note = self.vols.get("SSE", "SSE")
        close = self.cn.close
        if q is not None and self.cn.status(now_ms, holidays) == "交易中":
            if self.cn.live_stale(now_ms):
                return f"上证实时报价已超 10 分钟未更新（最后 {stamp(q.quoted_ms, seconds=False)}），暂不输出新概率"
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
            return f"A50 休市，{stamp(reopen, seconds=False)} 开盘后恢复概率"
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
        beta = self.config.a50_beta
        move = math.log(float(a50.last / base))
        effective = close.value * D(str(math.exp(beta * move)))
        odds = close_odds("上证指数", close.value, effective, sigma, remaining, target, D("0.01"),
                          f"{ref_note}·{close.source}",
                          f"A50 {fmt(a50.last)} / {base_note} {fmt(base)} → {percent(a50.last, base):+.3f}% × β {beta:g}", sigma_note,
                          beta=beta, mode="盘后")
        return dataclasses.replace(odds, warn=warn) if warn else odds

    @staticmethod
    def kospi_close_ms(q: IndexQuote) -> int:
        kst = dt.timezone(dt.timedelta(hours=9))
        day = dt.datetime.fromtimestamp(q.quoted_ms / 1000, kst).date()
        return int(dt.datetime.combine(day, dt.time(15, 30), kst).timestamp() * 1000)

    @staticmethod
    def hk_cash_close_date(now_ms: int, holidays: frozenset = frozenset()) -> dt.date:
        local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
        day = local.date() if local.time() >= dt.time(16, 10) else local.date() - dt.timedelta(days=1)
        while not hk_trading_day(day, holidays):
            day -= dt.timedelta(days=1)
        return day

    def note_live_close(self, symbol: str, ticker: StockTicker, now_ms: int) -> None:
        """Remember the stock's own print once today's close has passed, before the daily bar confirms it,
        so the probability can move on to the next close right away (persisted across restarts)."""
        info = STOCK_MARKETS[ticker.market]
        tz = dt.timezone(dt.timedelta(hours=info.utc_offset))
        today = dt.datetime.fromtimestamp(now_ms / 1000, tz).date()
        close_ms = int(dt.datetime.combine(today, info.close_time, tz).timestamp() * 1000)
        if now_ms < close_ms + 60_000:
            return
        live, _ = self.stocks.live_quote(symbol, now_ms)  # None once the close is final (+15 min): keep the last one
        if live is None or live.quoted_ms < close_ms - 5 * 60_000:
            return  # no print from the closing auction yet
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

    def contract_odds(self, symbol: str, price: D, now_ms: int) -> CloseOdds | str | None:
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
        sigma, sigma_note = self.vols.get(symbol, ticker.market)
        unit = "" if ticker.same_unit else ((ref.currency if ref else "") or info.currency)
        live, live_why = self.stocks.live_quote(symbol, now_ms)
        closed_today = close_date == dt.datetime.fromtimestamp(now_ms / 1000, tz).date() and base_ms <= now_ms
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
            return close_odds(NAMES.get(symbol, symbol), base, live.last, sigma, remaining, target,
                              price_tick(ticker.market, base), base_note,
                              f"{info.name}现货 {fmt_price(live.last)}（{live.source}·盘中直接用现货）", sigma_note, unit)
        if anchor is None or anchor[0] != base_ms:
            return "等待币安在收盘时刻的价格"
        remaining, target = session_remaining(ticker.market, now_ms, close_date, holidays)
        move = percent(price, anchor[1])
        return close_odds(NAMES.get(symbol, symbol), base_value, base_value * price / anchor[1], sigma, remaining, target,
                          price_tick(ticker.market, base_value), base_label,
                          f"币安 {fmt_price(price)} / 收盘时刻 {fmt_price(anchor[1])} → {move:+.3f}%"
                          + (f"｜{live_why}，暂用币安" if live_why else ""), sigma_note, unit)

    def hsi_odds(self, now_ms: int) -> CloseOdds | str | None:
        q = self.hsi.quote
        if not self.config.probability or not self.config.hsi_futures:
            return None
        if q is None or q.spot is None:
            return "缺少恒指现货"
        holidays = self.hsi.holidays
        local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
        cash_open = hk_trading_day(local.date(), holidays) and dt.time(9, 30) <= local.time() < dt.time(16, 10)
        sigma, sigma_note = self.vols.get("HSI", "HSI")
        if cash_open and q.spot_prev:
            remaining, target = session_remaining("hk", now_ms, local.date() - dt.timedelta(days=1), self.config.holidays.get("hk", frozenset()))
            sigma, sigma_note = self.vols.get("HSI", "HSI", intraday=True)
            prev_day = expected_close_date("hk", now_ms, self.config.holidays.get("hk", frozenset()))
            return close_odds("恒生指数", q.spot_prev, q.spot, sigma, remaining, target, D("0.01"), f"{prev_day.strftime('%m-%d')} 收盘",
                              f"恒指现货 {fmt(q.spot)}（盘中直接用现货）", sigma_note)
        close_date = self.hk_cash_close_date(now_ms, holidays)
        anchor, anchor_note = None, "收市时"
        if q.session_name(holidays) != "夜市":
            anchor, anchor_note = q.last, "日市收市"  # No night trading yet: the close itself is the best estimate.
        elif q.source == "etnet" and q.prev_settle:
            # The night block's 前收市 is the day session it followed. Only valid when that day is the
            # cash close being mapped; the block choice in parse_etnet_futures guarantees it.
            anchor, anchor_note = q.prev_settle, "日市收市"
        elif self.anchors.get("HSI") and dt.datetime.fromtimestamp(self.anchors["HSI"][0] / 1000, BEIJING).date() == close_date:
            anchor = self.anchors["HSI"][1]
        if anchor is None:
            return "缺少期货在现货收市时的价格"
        remaining, target = session_remaining("hk", now_ms, close_date, self.config.holidays.get("hk", frozenset()))
        return close_odds("恒生指数", q.spot, q.spot * q.last / anchor, sigma, remaining, target, D("0.01"),
                          f"{close_date.strftime('%m-%d')} 收盘", f"恒指期货 {fmt(q.last)} / {anchor_note} {fmt(anchor)} → {percent(q.last, anchor):+.3f}%",
                          sigma_note, mode="盘后")

    A50_SOFT_STALE_MS = 60 * 60_000  # A50 silent longer than this: no odds at all (shorter: odds with a warning)
    HL_STALE_MS = 10 * 60_000  # an HL mark older than this (refresh failing) is not used for new probabilities

    def kospi_odds(self, now_ms: int) -> CloseOdds | str | None:
        k = self.kospi.quote
        if not self.config.probability or not self.config.kospi_index:
            return None
        if k is None:
            return "缺少 KOSPI"
        sigma, sigma_note = self.vols.get("KOSPI", "KOSPI")
        kst = dt.timezone(dt.timedelta(hours=9))
        quoted_day = dt.datetime.fromtimestamp(k.quoted_ms / 1000, kst).date()
        local = dt.datetime.fromtimestamp(now_ms / 1000, kst)
        if quoted_day == local.date() and krx_session(now_ms) == "交易中" and k.prev_close:
            holidays = self.config.holidays.get("kr", frozenset())
            remaining, target = session_remaining("kr", now_ms, quoted_day - dt.timedelta(days=1), holidays)
            sigma, sigma_note = self.vols.get("KOSPI", "KOSPI", intraday=True)
            prev_day = expected_close_date("kr", now_ms, holidays)
            return close_odds("KOSPI", k.prev_close, k.last, sigma, remaining, target, D("0.01"), f"{prev_day.strftime('%m-%d')} 收盘",
                              f"KOSPI 现货 {fmt(k.last)}（盘中直接用现货）", sigma_note)
        hl, anchor = self.hl.quotes.get("KR200"), self.anchors.get("KOSPI")
        holidays = self.config.holidays.get("kr", frozenset())
        remaining, target = session_remaining("kr", now_ms, quoted_day, holidays)
        expected = expected_close_date("kr", now_ms, holidays)
        if quoted_day < expected or (quoted_day == local.date() and local.time() < dt.time(15, 30)):
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
        effective = k.last * D(str(math.exp(beta * math.log(float(price / base)))))
        return close_odds("KOSPI", k.last, effective, sigma, remaining, target, D("0.01"),
                          f"{quoted_day.strftime('%m-%d')} 收盘",
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
            items.append((f"{NAMES.get(symbol, symbol)}｜{symbol}", self.contract_odds(symbol, quote.price, now_ms) if quote else "等待行情"))
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
            if isinstance(odds, str):
                target = self.predict_day(title, now_ms)
                items.append({**base, "missing": odds, **(day_fields(target, now_ms) if target else {})})
                continue
            close_ms, close_label = self.target_close(title, odds.target)
            ref_day = re.search(r"\b\d\d-\d\d\b", odds.ref_note)
            items.append({
                **base, **day_fields(odds.target, now_ms), "ref_day": ref_day.group(0) if ref_day else "",
                "target": odds.target.strftime("%m-%d"), "unit": odds.unit or self.card_currency(symbol),
                "close_ms": close_ms, "close_label": close_label,
                "ref": fmt(odds.ref), "ref_note": odds.ref_note, "effective": fmt(odds.effective.quantize(D("0.0001"))),
                "move": float(percent(odds.effective, odds.ref)), "proxy_note": odds.proxy_note, "warn": odds.warn,
                "sigma_daily": odds.sigma_daily, "sigma": odds.sigma, "remaining": odds.remaining, "sigma_note": odds.sigma_note,
                "z": odds.z, "up": odds.up, "flat": odds.flat, "down": odds.down,
                "fair_up": odds.fair_up, "fair_down": odds.fair_down,
            })
        if self.config.touch:
            items.extend(self.touch_payload(t, now_ms) for t in self.touches.values())
            items.extend(self.cap_payload(c, now_ms) for c in self.caps.values())
        today = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING).date()
        return {"generated_at": stamp(now_ms) + "（北京时间）", "version": VERSION, "server_ms": now_ms,
                "today": f"{today:%m-%d} {WEEKDAYS[today.weekday()]}",
                "mode": BASELINE_SHORT.get(self.settings()["mode"], self.settings()["mode"]),
                "color_style": self.config.color_style, "items": items,
                "note": ("模型参考，非投资建议。有效价 = 参考收盘 × 代理现价 ÷ 代理在参考收盘时刻的价格；"
                         "P(涨) = 1 − Φ(ln((参考+半跳)/有效)/σ剩余)，平盘两边各计一半。目标日跳过周末和已配置的交易所假期。"
                         if self.config.probability else "概率功能已关闭（PROBABILITY=off）。")}

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
        if book is not None:
            out.update(bids=[[float(p), float(q)] for p, q in book.bids], asks=[[float(p), float(q)] for p, q in book.asks],
                       age=max(0, (now_ms - book.fetched_ms) // 1000), stale=book.stale(now_ms))
            if isinstance(odds, TouchOdds):
                edges = book_edges(odds.fair_upper, book)
                best = None if book.stale(now_ms) else best_edge(edges)
                out["edges"] = [{"label": e.label.replace("涨", high).replace("跌", low), "maker": e.maker,
                                 "price": e.price, "edge": e.edge, "size": e.size, "best": e is best} for e in edges]
        elif why and spec.key in self.predict.books:
            out["error"] = why
        if self.config.predict:
            base["predict"] = out
        if isinstance(odds, str):
            return {**base, "missing": odds}
        price = touch.price
        years = max(0.0, (spec.deadline_ms - now_ms) / YEAR_MS)
        return {**base, "fair_up": odds.fair_upper, "fair_down": odds.fair_lower, "up": odds.upper, "flat": odds.none,
                "down": odds.lower, "touch": {
                    "coin": spec.symbol.removesuffix("USDT"),
                    "price": f"{price:,.2f}" if price is not None else "—",
                    "to_low": float(percent(spec.low, price)) if price else 0.0,
                    "to_high": float(percent(spec.high, price)) if price else 0.0,
                    "sigma": touch.sigma or 0.0, "years": years, "status": touch.status(),
                    "p_low": odds.lower, "p_high": odds.upper, "p_none": odds.none,
                    "error": touch.error}}

    def cap_payload(self, cap: "CapMarket", now_ms: int) -> dict:
        """Web card for a market-cap ladder: per threshold the model's P(Yes), the Yes book and its best edge."""
        spec = cap.spec
        start = dt.datetime.fromtimestamp(spec.start_ms / 1000, dt.timezone(dt.timedelta(hours=-4)))
        end = dt.datetime.fromtimestamp(spec.end_ms / 1000, dt.timezone(dt.timedelta(hours=-4)))
        bj = lambda ms: dt.datetime.fromtimestamp(ms / 1000, BEIJING).strftime("%m-%d %H:%M")
        rows_in = self.predict.ladders.get(spec.key) or [LadderRow(t, "", "", None, "") for t in spec.targets]
        rows = []
        for row in rows_in:
            fair = cap.probability(row.target, now_ms)
            meta = (self.predict.market_meta.get(row.market_id) or ({}, 0))[0] if row.market_id else {}
            if "RESOLVED" in str(meta.get("status", "")).upper() and now_ms < spec.end_ms:
                fair = 1.0  # settled before the window closed: only a touch settles early, so it was Yes
            book, why = self.predict.yes_book(row) if row.market_id else (None, "")
            out: dict[str, Any] = {"label": usd_short(row.target), "fair": fair, "error": why,
                                   "dist": float(row.target / cap.cap - 1) if cap.cap else None}
            if book is not None:
                out.update(bid=float(book.bid[0]) if book.bid else None, ask=float(book.ask[0]) if book.ask else None,
                           stale=book.stale(now_ms))
                top = max((float(p) for p, _ in (*book.bids[:1], *book.asks[:1])), default=0.0)
                if fair == 1.0 and top < 0.9:
                    # our history says touched, the market does not: sources disagree, so no "sure thing" edge
                    out["error"] = "数据显示已触及，但盘口仍低于 90¢；以 Flap.sh 为准，请核实"
                elif fair is not None:
                    edges = book_edges(fair, book)
                    best = None if book.stale(now_ms) else best_edge(edges)
                    out["edges"] = [{"label": e.label.replace("涨", "Yes").replace("跌", "No"), "maker": e.maker,
                                     "price": e.price, "edge": e.edge, "size": e.size, "best": e is best} for e in edges]
            # reached, and the book agrees (settled, gone, or ≥ 90¢): folded into one "已触及" line on the card
            out["touched"] = fair == 1.0 and "请核实" not in out["error"]
            rows.append(out)
        high, high_at = cap.window_high()
        item: dict[str, Any] = {
            "name": spec.name, "symbol": spec.key, "group": "ladder", "kind": "ladder", "close_ms": spec.end_ms,
            "close_label": f"{end:%m-%d %H:%M} ET（北京 {bj(spec.end_ms)}）截止" + (f"；{spec.trade_end}" if spec.trade_end else ""),
            "ladder": {"cap": usd_short(cap.cap), "high": usd_short(high), "high_at": stamp(high_at * 1000, seconds=False) if high_at else "",
                       "sigma": cap.sigma, "sigma_note": cap.sigma_note, "rows": rows, "error": cap.error,
                       "price": f"{cap.price:.10g}" if cap.price is not None else "—", "source": cap.source or "—",
                       "supply": fmt(cap.supply.quantize(D(1))) if cap.supply else "—",
                       "window": f"{start:%m-%d %H:%M} ET（北京 {bj(spec.start_ms)}）起",
                       "years": max(0.0, (spec.end_ms - max(now_ms, spec.start_ms)) / YEAR_MS),
                       "first_skipped": cap.history.get("first") == "skipped",
                       "metric": spec.metric, "settle": spec.settle, "bars": bool(spec.gecko),
                       "supply_note": "总量 − 销毁" if spec.supply == "rpc" else f"DexScreener {spec.metric} ÷ 价格"},
        }
        if self.config.predict:
            item["predict"] = {"url": predict_url(spec.slug, self.config.predict_ref), "error": self.predict.errors.get(spec.key, "")}
        if cap.cap is None or cap.sigma is None:
            item["missing"] = f"等待市值数据（{brief_error(cap.error, 80)}）" if cap.error else "等待市值数据"
        return item

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
            targets.update({spec.key: spec.slug for spec in TOUCH_MARKETS})
            targets.update({spec.key: spec.slug for spec in CAP_MARKETS})
        return targets

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
        out.update(bids=[[float(p), float(q)] for p, q in book.bids], asks=[[float(p), float(q)] for p, q in book.asks],
                   age=max(0, (now_ms - book.fetched_ms) // 1000), stale=book.stale(now_ms))
        if isinstance(odds, CloseOdds):
            edges = book_edges(odds.fair_up, book)
            best = None if book.stale(now_ms) or odds.warn else best_edge(edges)
            out["edges"] = [{"label": e.label, "maker": e.maker, "price": e.price, "edge": e.edge, "size": e.size,
                             "best": e is best} for e in edges]
        return out

    def cmd_book(self, req: Request) -> "Reply":
        if not self.config.predict:
            return Reply("Predict 盘口功能已关闭（PREDICT=off）。")
        now_ms = self.market.now_ms()
        odds = dict(self.odds_items(now_ms)) if self.config.probability else {}
        lines = [f"📕 {bold('Predict 盘口 vs 模型公平价')}",
                 "挂涨=在买1排队买涨；挂跌=在 1−卖1 排队买跌；吃=立即成交。优势=模型公平价−成交价（每份，¢）"]
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
            rows = book_lines(book, error, item, now_ms, url)
            if book is None:
                rows.append(url)
            if isinstance(item, str):
                rows.insert(0, f"概率暂缺：{item}")
            lines.extend(tree(rows))
        if not shown:
            lines.append("\n⏳ 还没有盘口数据，启动后约 15 秒首次获取；请稍后再试。")
        lines.append("\n⚠️ 模型只是参考；未计手续费和积分/LP 奖励，挂单不保证成交。")
        return Reply("\n".join(lines), html=True)

    def target_close(self, title: str, target: dt.date) -> tuple[int, str]:
        """Epoch ms and label of the target session's official close for an odds item."""
        market = {"恒生指数": "hk", "KOSPI": "kr", "上证指数": "sh"}.get(title)
        if market is None:
            ticker = self.config.tickers.get(title.split("｜")[-1])
            market = ticker.market if ticker else "sh"
        info = STOCK_MARKETS[market]
        close = dt.datetime.combine(target, info.close_time, dt.timezone(dt.timedelta(hours=info.utc_offset)))
        local = f"，{info.tz_name[:-2]} {close.strftime('%H:%M')}" if info.utc_offset != 8 else ""
        return (int(close.timestamp() * 1000),
                f"{close.astimezone(BEIJING).strftime('%m-%d %H:%M')} {info.name}收盘（北京时间{local}）")

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
                rows += book_lines(self.predict.books.get(key), self.predict.errors.get(key, ""), odds, now_ms)
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

    def config_summary(self) -> str:
        settings = self.settings()
        mode = BASELINE_SHORT.get(settings["mode"], settings["mode"])
        return (f"⚙️ 基准 {mode}｜阈值 ±{fmt(settings['threshold'])}%｜每 {self.config.poll} 秒"
                f"｜周期 {settings['cooldown']} 秒")

    def status(self, sub_id: str) -> str:
        """Status card with bold sentinels; send it with html_mode=True."""
        now_ms = self.market.now_ms()
        sub = self.subscriptions().get(sub_id)
        active = "🟢 已订阅" if sub and sub.get("active") else "⏸ 未订阅/已暂停"
        style = self.config.color_style
        lines = [f"📡 {bold(f'监控状态 v{VERSION}')}｜{active}", self.config_summary(),
                 f"📊 {legend(style)}｜→ 后为币安现价相对该行价格"]
        if self.settings()["mode"] == "binance_daily" and self.config.tickers:
            lines.append("💡 /mode exchange 可把基准对齐到交易所收盘时刻")
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
        used = tuple(dict.fromkeys(STOCK_MARKETS[t.market].currency for t in self.config.tickers.values()
                                   if not t.same_unit or True))
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
                    await self.notice(sub_id, sub, "币安行情接口", text)
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
        for sub_id, sub in self.subscriptions().items():
            if not sub.get("active"):
                continue
            await self.notice(sub_id, sub, "币安行情接口", None)
            for symbol in self.config.symbols:
                snapshot = collected[symbol]
                error = snapshot.get("error")
                if error and snapshot.get("pending"):
                    continue  # still loading, not a fault: no notice
                if error:
                    self.log_limited(symbol, f"{symbol}: {error}")
                    await self.notice(sub_id, sub, symbol, error)
                    continue
                await self.notice(sub_id, sub, symbol, None)
                if self.settings() != settings:
                    return
                current_sub = self.subscriptions().get(sub_id)
                if not current_sub or not current_sub.get("active"):
                    break
                quote: Quote = snapshot["quote"]
                base: Baseline = snapshot["baseline"]
                current_ms = self.market.now_ms()
                if current_ms >= base.valid_until_ms or current_ms - quote.timestamp_ms > self.config.max_age * 1000:
                    continue  # Slow Telegram/API calls must not produce stale price alerts.
                if settings["mode"] == "manual" and self.manual_baseline_for(symbol, current_ms).key != base.key:
                    continue
                change = percent(quote.price, base.value)
                state_key = f"alert:{sub_id}:{symbol}"
                old = self.store.get(state_key, {})
                passive, plan = alert_plan(old, base.key, change, time.time(), threshold,
                                            int(settings["cooldown"]), self.config.step, self.config.min_gap)
                if passive != old:
                    self.store.put(state_key, passive)
                if not plan:
                    continue
                text = alert_text(symbol, quote, base, change, threshold, plan.reason,
                                  self.references_for(symbol, current_ms), self.fx, self.config.color_style,
                                  self.context_lines(symbol, current_ms, quote.price))
                if await self.tell(sub["chat"], sub["thread"], text, html_mode=True):
                    # Only mark a price alert as delivered AFTER Telegram accepts it.
                    # Avoid resurrecting state deleted by a command during delivery.
                    if self.settings() == settings and self.subscriptions().get(sub_id, {}).get("active"):
                        self.store.put(state_key, plan.next_state)

    # --- diagnostics ---------------------------------------------------------------------------------

    def diag_probes(self, now_ms: int) -> list[tuple[str, str, Any, Any]]:
        """(group, source, fetch, check) for every feed the bot uses, each source separately."""
        probes: list[tuple[str, str, Any, Any]] = []

        def get(url: str, extra: dict[str, str]) -> Any:
            return lambda: fetch_source(url, extra)  # a probe's outcome also updates the host's cooldown

        def when(ms: int) -> str:
            return stamp(ms, seconds=False) + stale_note(ms, now_ms, BEIJING)

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
                    if name in {"东方财富", "Naver"}:
                        day, close, _ = last_completed_bar(parse_daily_bars(ticker.market, raw), info, now_ms)
                    else:
                        day, close, _ = parse_quote_close(name, ticker.market, raw, info, now_ms)
                    return f"{day.strftime('%m-%d') if day else '上一交易日（无日期）'} 收盘 {fmt(close)} {info.currency}"
                probes.append((f"交易所收盘·{short_name(symbol)}", f"{name} {ticker.market}:{ticker.code}", get(url, extra), check_stock))

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
            for name, url, extra in IndexFutures.SPOT_SOURCES:
                def check_spot(raw: bytes, name=name) -> str:
                    last, prev = IndexFutures.parse_spot(name, raw)
                    return f"{fmt(last)}（昨收 {fmt(prev) if prev else '—'}）"
                probes.append(("恒指现货", name, get(url, extra), check_spot))

        if self.config.kospi_index:
            for group, sources in (("KOSPI", KospiIndex.SOURCES), ("KOSPI200", KospiIndex.SOURCES_200)):
                for name, url, extra in sources:
                    def check_kospi(raw: bytes, name=name) -> str:
                        q = KospiIndex.parse(name, raw, now_ms)
                        return f"{fmt(q.last)}（昨收 {fmt(q.prev_close) if q.prev_close else '—'}）｜{stamp(q.quoted_ms, seconds=False)}"
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
                ok_ago = f"{int(time.time() - state['ok_at'])} 秒前成功" if state["ok_at"] else "从未成功"
                mark = "❌" if state["error"] and state["error_at"] >= state["ok_at"] else "✅"
                err = f"｜最近错误：{brief_error(state['error'], 80)}" if state["error"] else ""
                busy = f"｜本轮进行中 {running:.0f}s" if running >= 1 else ""
                lines.append(f"  {mark} {name}：{ok_ago}｜上次用时 {state['ms'] / 1000:.1f}s｜共 {state['runs']} 轮{busy}{err}")
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
        if self.config.kospi_index:
            k, hl, anchor = self.kospi.quote, self.hl.quotes.get("KR200"), self.anchors.get("KOSPI")
            lines.append(f"  KOSPI 基准：{f'{fmt(k.last)}｜{stamp(k.quoted_ms, seconds=False)}｜{k.source}' if k else '无'}"
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
            lines.append(f"  恒指期货：{f'{q.session_name(self.hsi.holidays)} {fmt(q.last)}｜{stamp(q.quoted_ms, seconds=False)}｜{q.source}' if q else '无'}"
                         + (f"｜错误：{brief_error(self.hsi.error, 80)}" if self.hsi.error else ""))
        for symbol in self.config.symbols:
            snap = self.snapshots.get(symbol) or {}
            err = self.stocks.errors.get(symbol)
            live, why = self.stocks.live_quote(symbol, now_ms)
            live_text = (f"股票实时：{fmt_price(live.last)}｜{stamp(live.quoted_ms, seconds=False)}｜{live.source}" if live
                         else f"股票实时：{why}" if why else "")
            if "error" in snap or err or live_text:
                lines.append(f"  {short_name(symbol)}：" + "｜".join(x for x in (snap.get("error"), f"交易所收盘：{err}" if err else "", live_text) if x))
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
        for group, rs in groups.items():
            lines.append(f"\n【{group}】")
            lines.extend(f"{'✅' if r.ok else '❌'} {r.name}（{r.ms} ms）：{r.detail}" for r in rs)
        return "\n".join(lines + [""] + state)

    async def cmd_diag(self, req: Request) -> str:
        await self.tell(req.chat, req.thread, "🩺 正在逐个检测数据源，约 10–30 秒…")
        results = await self.diagnose()
        return self.diag_text(results, self.diag_state(self.market.now_ms()),
                              f"🩺 数据源检测 v{VERSION}｜{stamp(self.market.now_ms())}（北京时间）")

    def reference_jobs(self) -> list[tuple[str, Any]]:
        """(name, coroutine factory) per reference feed; each has its own refresh cadence inside."""
        now = self.market.now_ms
        jobs = [("交易所收盘", lambda: self.stocks.refresh(now())), ("股票实时", lambda: self.stocks.refresh_live(now())),
                ("汇率", self.fx.refresh),
                ("恒指期货", lambda: self.hsi.refresh(now())), ("Hyperliquid", self.hl.refresh),
                ("KOSPI", lambda: self.kospi.refresh(now())), ("上证/A50", lambda: self.cn.refresh(now()))]
        if self.config.probability:
            jobs.append(("概率输入", lambda: self.refresh_odds_inputs(now())))
        if self.config.predict:
            jobs.append(("Predict 盘口", lambda: self.predict.refresh(self.predict_targets(now()))))
        if self.config.touch:
            jobs.append(("先触市场", lambda: self.refresh_touch(now())))
            jobs.append(("市值阶梯", lambda: self.refresh_caps(now())))
        return jobs

    async def refresh_caps(self, now_ms: int) -> None:
        for cap in self.caps.values():
            await cap.refresh(now_ms)

    async def refresh_touch(self, now_ms: int) -> None:
        for touch in self.touches.values():
            created = touch.spec.created_ms if touch.spec.fixed_start else (
                (self.predict.info.get(touch.spec.slug) or {}).get("created_ms") or touch.spec.created_ms)
            if created and created != touch.start_ms:
                touch.start_ms = created
                touch.times["scan"] = -1e9  # check the path from the (new) opening time at once
            await touch.refresh(now_ms)

    async def reference_loop(self, name: str, job: Any) -> None:
        """Refresh one reference feed forever, isolated from the price-alert loop and from the other feeds."""
        token = HTTP_POOL.set(self.reference_pool)  # this task's blocking requests stay off the default pool
        try:
            while not self.stopping.is_set():
                started = time.monotonic()
                state = self.reference_state.setdefault(name, {"ok_at": 0.0, "error": "", "error_at": 0.0, "ms": 0,
                                                               "runs": 0, "running_since": 0.0})
                state["running_since"] = time.time()
                ran = True
                try:
                    # False = not due yet: a skipped tick is not a run and must not overwrite the last timing
                    ran = await asyncio.wait_for(job(), timeout=REFERENCE_TIMEOUT) is not False
                    if ran:
                        state.update(ok_at=time.time(), error="")
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # failures are shown in /status by each feed; keep the last good data
                    text = clean_error(error) or type(error).__name__
                    state.update(error=text, error_at=time.time())
                    self.log_limited(f"reference:{name}", f"参考数据 {name} 刷新异常：{text}")
                finally:
                    state["running_since"] = 0.0
                if ran:
                    state.update(ms=int((time.monotonic() - started) * 1000), runs=state["runs"] + 1)
                await self.wait(REFERENCE_TICK)
        finally:
            HTTP_POOL.reset(token)

    def start_reference_tasks(self) -> None:
        self.reference_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="reference")
        self.reference_tasks = [asyncio.create_task(self.reference_loop(name, job), name=f"reference:{name}")
                                for name, job in self.reference_jobs()]

    async def monitor_loop(self) -> None:
        while not self.stopping.is_set():
            started = time.monotonic()
            try:
                await self.one_cycle()
            except Exception as error:
                self.log_limited("monitor", "监控轮次异常：" + clean_error(error))
            if time.monotonic() - self.last_log.get("heartbeat", -1e9) >= 60:
                ok = sum("quote" in value for value in self.snapshots.values())
                LOG.info("heartbeat: valid_quotes=%s/%s active_subscriptions=%s mode=%s", ok,
                         len(self.config.symbols), sum(bool(s.get("active")) for s in self.subscriptions().values()),
                         self.settings()["mode"])
                self.last_log["heartbeat"] = time.monotonic()
            await self.wait(max(0.1, self.config.poll - (time.monotonic() - started)))

    async def process_update(self, update: dict) -> None:
        message = update.get("message")
        if isinstance(message, dict):
            # Do not execute old configuration commands left over from long downtime.
            age = time.time() - float(message.get("date", time.time()))
            if -60 <= age <= 900:
                await self.process_message(message)
            return
        query = update.get("callback_query")
        if isinstance(query, dict):  # A button tap is a live intent, so it is not age-filtered.
            await self.process_callback(query)

    async def commands_loop(self) -> None:
        offset = int(self.store.get("telegram_offset", 0))
        failures = 0
        while not self.stopping.is_set():
            try:
                updates = await self.telegram.call("getUpdates", {"offset": offset, "timeout": 25,
                                                   "allowed_updates": ["message", "callback_query"]}, timeout=40)
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

    async def run(self) -> None:
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
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.stopping.set)
        self.start_reference_tasks()
        tasks = [asyncio.create_task(self.monitor_loop()), asyncio.create_task(self.commands_loop()), *self.reference_tasks]
        if self.config.web_port:
            try:
                self.web = WebServer(self, self.config.web_port, self.web_token)
                port = await self.web.start()
                LOG.info("Probability page listening on port %s (%s)", port,
                         "public URL via /web" if self.config.web_base else "no public domain yet")
            except OSError as error:
                LOG.warning("概率网页启动失败（不影响提醒）：%s", clean_error(error))
                self.web = None
        try:
            await self.stopping.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.reference_pool:
                self.reference_pool.shutdown(wait=False, cancel_futures=True)
            if self.web:
                await self.web.stop()
            LOG.info("Stopped safely")


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
        asyncio.run(Bot(config, store, Binance(config), Telegram(config.token)).run())
        return 0
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
