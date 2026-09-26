#!/usr/bin/env python3
"""Read-only Binance futures price alerts. Python 3.12+, standard library only.

Default baseline = previous completed Binance UTC daily candle, NOT an
underlying stock exchange's official previous close. No trading API is used.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import datetime as dt
import decimal
import html
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

D = decimal.Decimal
UTC = dt.timezone.utc
BEIJING = dt.timezone(dt.timedelta(hours=8))
DAY_MS = 86_400_000
VERSION = "1.10.0"
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
        return f"{when} 交易所收盘时刻·{price_kind}".strip()
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
        """'｜收盘 09-18 15:00（北京时间）', or with the venue's local time first when it differs."""
        if not close_ms:
            return ""
        if self.utc_offset == 8:
            return close_label(close_ms)
        local = dt.datetime.fromtimestamp(close_ms / 1000, dt.timezone(dt.timedelta(hours=self.utc_offset)))
        return f"｜收盘 {local.strftime('%m-%d %H:%M')}（{self.tz_name}）＝北京 {stamp(close_ms, seconds=False)}"


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


def parse_beta(value: str) -> float:
    try:
        beta = float(value)
    except ValueError:
        raise ValueError("A50_BETA 必须是数字，如 0.8") from None
    if not 0 < beta <= 3:
        raise ValueError("A50_BETA 应在 0～3 之间")
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
    holidays: dict[str, frozenset] = field(default_factory=dict)  # market -> non-trading weekdays
    web_port: int = 0        # Read-only probability web page; 0 = disabled. Railway injects PORT.
    web_token: str = ""      # Secret path segment; generated and persisted when empty.
    web_base: str = ""       # Public base URL, e.g. https://xxx.up.railway.app
    hl_tickers: dict[str, tuple[str, str]] = field(default_factory=dict)  # symbol -> (dex, coin) on Hyperliquid
    kospi_index: bool = True  # Show the KOSPI composite index for Korea-listed underlyings.
    hl_index: dict[str, tuple[str, str]] = field(default_factory=dict)  # index name -> (dex, coin), e.g. KR200

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
            probability=e.get("PROBABILITY", "on").strip().lower() not in {"off", "0", "false", "no"},
            prob_vol=parse_prob_vol(e.get("PROB_VOL", "")),
            sse_index=e.get("SSE_INDEX", "on").strip().lower() not in {"off", "0", "false", "no"},
            a50_beta=parse_beta(e.get("A50_BETA", "0.8")),
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

    def delete_prefix(self, prefix: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM records WHERE substr(k,1,?)=?", (len(prefix), prefix))

    def close(self) -> None:
        self.conn.close()


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
        raise RemoteError(f"网络错误 ({type(error).__name__})") from None


def _http_json(url: str, payload: dict | None = None, timeout: int = 15) -> Any:
    try:
        return json.loads(_http_get(url, payload, timeout))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise RemoteError("接口未返回有效 JSON") from None


async def http_json(url: str, payload: dict | None = None, timeout: int = 15) -> Any:
    return await asyncio.to_thread(_http_json, url, payload, timeout)


async def http_get(url: str, timeout: int = 15, headers: dict[str, str] | None = None) -> bytes:
    return await asyncio.to_thread(_http_get, url, None, timeout, headers)


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
    """'09-23 15:30 韩国时间' for non-Beijing venues, else '09-23 15:00'."""
    local = re.search(r"收盘 (\S+ \S+)（([^）]+)）", ref.close_note or "")
    if local and local.group(2) != "北京时间":
        return f"{local.group(1)} {local.group(2)}"
    return stamp(ref.close_ms, seconds=False) if ref.close_ms else "上一交易日"


def reference_row(kind: str, price: D, ref: Baseline | None, fx: "FxRates | None", style: str,
                  unit_note: str = "") -> str:
    """'交易所 490.97 CNY ≈ 73.278（09-23 15:00 收·腾讯）→ 🟢 -0.11%｜当日 🔴 +0.36%'."""
    label, command, _ = PRICE_KINDS[kind]
    label = "交易所" if kind == "exchange" else label
    if ref is None:
        return f"{label} 未设置（{command}）"
    when = f"{stamp(ref.close_ms, seconds=False)} 收" if ref.close_ms else "上一交易日"
    local = re.search(r"收盘 (\S+ \S+)（([^）]+)）", ref.close_note or "")
    if local and local.group(2) != "北京时间":  # Non-Beijing venue: show its local close time.
        when = f"{local.group(1)} {local.group(2)} 收"
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
    bars: list[tuple[dt.date, D]] = []
    if market == "kr":
        # Naver: <item data="20260917|open|high|low|close|volume" /> (EUC-KR page, digits are ASCII)
        for date, close in re.findall(rb'data="(\d{8})\|[^|"]*\|[^|"]*\|[^|"]*\|([0-9.]+)\|', raw):
            bars.append((dt.datetime.strptime(date.decode(), "%Y%m%d").date(), number(close.decode(), "收盘价")))
    else:
        # Eastmoney: {"data": {"klines": ["2026-09-17,open,close,high,low,...", ...]}}
        try:
            klines = json.loads(raw)["data"]["klines"]
        except (ValueError, KeyError, TypeError):
            raise ValueError("日 K 接口返回格式异常（代码可能不存在）") from None
        for line in klines:
            parts = str(line).split(",")
            if len(parts) >= 3:
                bars.append((dt.date.fromisoformat(parts[0]), number(parts[2], "收盘价")))
    if not bars:
        raise ValueError("日 K 接口没有返回任何交易日")
    return bars


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
        for symbol in config.tickers:
            saved = store.get(f"stock_close:{symbol}") if store else None
            if saved:
                with contextlib.suppress(Exception):
                    self.closes[symbol] = Baseline(number(saved["value"], "收盘价"), saved["key"], saved["label"],
                                                   int(saved["valid_until_ms"]), int(saved["close_ms"]),
                                                   saved.get("currency", ""), saved.get("source", ""),
                                                   saved.get("close_note", ""),
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

    async def fetch(self, symbol: str, ticker: StockTicker, now_ms: int) -> Baseline:
        info = STOCK_MARKETS[ticker.market]
        failures = []
        for name, url, extra in self.sources(ticker):
            for attempt in range(self.ATTEMPTS):
                try:
                    raw = await http_get(url, headers={"User-Agent": BROWSER_UA, "Accept": "*/*", **extra})
                    if name in {"东方财富", "Naver"}:
                        day, close, prev = last_completed_bar(parse_daily_bars(ticker.market, raw), info, now_ms)
                    else:
                        day, close, prev = parse_quote_close(name, ticker.market, raw, info, now_ms)
                    return self.baseline(ticker, info, name, day, close, prev)
                except Exception as error:
                    failures.append(f"{name}: {clean_error(error)}")
                    if attempt + 1 < self.ATTEMPTS:
                        await asyncio.sleep(1.5)
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

    async def refresh(self, now_ms: int, force: bool = False) -> None:
        if not self.config.tickers or (not force and time.monotonic() - self.refreshed < self.REFRESH_SECONDS):
            return
        self.refreshed = time.monotonic()
        for index, (symbol, ticker) in enumerate(self.config.tickers.items()):
            if index:
                await asyncio.sleep(0.5)  # Spread requests out; feeds drop bursts from one IP.
            try:
                self.closes[symbol] = await self.fetch(symbol, ticker, now_ms)
                self.errors.pop(symbol, None)
                self.remember(symbol, self.closes[symbol])
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

    async def refresh(self, force: bool = False) -> None:
        if not force and time.monotonic() - self.refreshed < self.REFRESH_SECONDS:
            return
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


def parse_etnet_futures(raw: bytes, now_ms: int) -> "FuturesQuote":
    """etnet 指數期貨 page: HKEX HSI futures (日市/夜市 blocks) plus 恒生指數現貨.

    Parsed from visible text so markup changes do not matter. The block with the newest
    timestamp is used; its published 高水/低水 is kept as the basis.
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
        quoted_ms = now_ms
        if stamp_match:
            quoted = dt.datetime.strptime(stamp_match.group(1), "%Y/%m/%d %H:%M").replace(tzinfo=BEIJING)
            quoted_ms = int(quoted.timestamp() * 1000)
        candidates.append(FuturesQuote(
            f"恒指期货({month}){session}", number(m.group(1).replace(",", ""), "恒指期货"), field("前收市"),
            field("開市"), field("最高"), field("最低"), quoted_ms, "etnet", spot, "etnet", spot_prev, water))
    if not candidates:
        raise ValueError("etnet 页面没有找到恒指期货报价")
    return max(candidates, key=lambda q: q.quoted_ms)


def stale_note(quoted_ms: int, now_ms: int, tz: dt.tzinfo) -> str:
    """'｜⚠️ 非今日数据' when the quote's local calendar day is earlier than today's."""
    quoted = dt.datetime.fromtimestamp(quoted_ms / 1000, tz).date()
    today = dt.datetime.fromtimestamp(now_ms / 1000, tz).date()
    return "｜⚠️ 非今日数据" if quoted < today else ""


def hk_futures_session(now_ms: int) -> str:
    """HKEX HSI futures: day session 09:15-16:30, after-hours (夜市) 17:15-03:00 next day, HK time."""
    local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING).time()
    if local >= dt.time(17, 15) or local < dt.time(3, 0):
        return "夜市"
    if dt.time(9, 15) <= local <= dt.time(16, 30):
        return "日市"
    return "休市"


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

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.quote: FuturesQuote | None = None
        self.error = ""
        self.refreshed = -1e9

    @staticmethod
    def parse_futures(source: str, raw: bytes, now_ms: int) -> FuturesQuote:
        if source == "etnet":
            return parse_etnet_futures(raw, now_ms)
        if source == "东方财富":
            d = parse_eastmoney_quote(raw)
            quoted_ms = int(d["f86"]) * 1000 if str(d.get("f86", "")).isdigit() else now_ms
            return FuturesQuote(str(d.get("f58") or "恒指期货主力"), number(d["f43"], "恒指期货"), _opt(d.get("f60")),
                                _opt(d.get("f46")), _opt(d.get("f44")), _opt(d.get("f45")), quoted_ms, source)
        # Sina hf_HSI: last, ?, bid, ask, high, low, time, prev settle, open, open interest, ..., name, date
        match = re.search(r'="([^"]*)"', raw.decode("gbk", errors="ignore"))
        fields = match.group(1).split(",") if match else []
        if len(fields) < 15 or not fields[0]:
            raise ValueError("新浪恒指期货报价为空")
        try:
            quoted = dt.datetime.strptime(f"{fields[14]} {fields[6]}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING)
            quoted_ms = int(quoted.timestamp() * 1000)
        except ValueError:
            quoted_ms = now_ms
        return FuturesQuote(fields[13] or "恒指期货", number(fields[0], "恒指期货"), _opt(fields[7]), _opt(fields[8]),
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
        for name, url, extra in sources:
            try:
                raw = await http_get(url, headers={"User-Agent": BROWSER_UA, "Accept": "*/*", **extra})
                return parse(name, raw)
            except Exception as error:
                failures.append(f"{name}: {clean_error(error)}")
        raise ValueError("；".join(failures))

    async def refresh(self, now_ms: int, force: bool = False) -> None:
        if not self.enabled or (not force and time.monotonic() - self.refreshed < self.REFRESH_SECONDS):
            return
        self.refreshed = time.monotonic()
        try:
            quote: FuturesQuote = await self._first(self.FUTURES_SOURCES, lambda n, r: self.parse_futures(n, r, now_ms))
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
        parts = [f"📈 {bold('恒指期货 ' + hk_futures_session(q.quoted_ms))} {bold(fmt(q.last))}"]
        if q.spot is not None:
            local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING).time()
            cash_open = dt.time(9, 30) <= local <= dt.time(16, 10)
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
        self.at_cache: dict[tuple[str, int], D] = {}
        self.refreshed = -1e9

    async def price_at(self, coin: str, at_ms: int) -> D:
        """Close of the 1-minute candle ending at ``at_ms`` (the market's price at that instant)."""
        key = (coin, at_ms)
        if key in self.at_cache:
            return self.at_cache[key]
        rows = await http_json(self.URL, {"type": "candleSnapshot", "req": {
            "coin": coin, "interval": "1m", "startTime": at_ms - 5 * 60_000, "endTime": at_ms}})
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

    async def refresh(self, force: bool = False) -> None:
        if not self.tickers or (not force and time.monotonic() - self.refreshed < self.REFRESH_SECONDS):
            return
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
    """FTSE China A50 futures (SGX): day 09:00-16:30, night 17:00-04:45 Beijing time."""
    local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING).time()
    if local >= dt.time(17, 0) or local < dt.time(4, 45):
        return "夜盘"
    if dt.time(9, 0) <= local <= dt.time(16, 30):
        return "日盘"
    return "休市"


def parse_cn_index(source: str, raw: bytes, now_ms: int, name: str = "上证指数") -> IndexQuote:
    """Tencent v_sh000001 / Sina hq_str_sh000001 / Eastmoney push2 -> IndexQuote (last, prev close, time)."""
    if source == "东方财富":
        return parse_eastmoney_index(raw, now_ms, name)
    match = re.search(r'="([^"]*)"', raw.decode("gbk", errors="ignore"))
    if not match or not match.group(1).strip():
        raise ValueError(f"{source}{name}报价为空")
    fields = match.group(1).split("~" if source == "腾讯" else ",")
    try:
        if source == "腾讯":  # [3] current, [4] prev close, [5] open, [30] yyyymmddHHMMSS
            last, prev, opening = fields[3], fields[4], fields[5]
            quoted = dt.datetime.strptime(re.sub(r"\D", "", fields[30])[:12], "%Y%m%d%H%M")
        else:  # Sina: name, open, prev, current, high, low, ..., date [30], time [31]
            last, prev, opening = fields[3], fields[2], fields[1]
            quoted = dt.datetime.strptime(f"{fields[30]} {fields[31]}", "%Y-%m-%d %H:%M:%S")
    except (IndexError, ValueError):
        raise ValueError(f"{source}{name}格式异常") from None
    return IndexQuote(name, number(last, name), _opt(prev), _opt(opening), None, None,
                      int(quoted.replace(tzinfo=BEIJING).timestamp() * 1000), source)


class CnIndex:
    """Shanghai Composite (000001) plus FTSE China A50 futures (SGX) as its after-hours proxy.

    Composite: Tencent → Sina → Eastmoney. A50: Eastmoney 104.CN00Y (month-continuous contract)
    then Sina's CFD as a labelled last resort. Refreshed every 60 s, best effort.
    """
    REFRESH_SECONDS = 60
    SSE_SOURCES = (("腾讯", "https://qt.gtimg.cn/q=sh000001", {"Referer": "https://gu.qq.com/"}),
                   ("新浪", "https://hq.sinajs.cn/list=sh000001", {"Referer": "https://finance.sina.com.cn/"}),
                   ("东方财富", IndexFutures.EM + "1.000001", {"Referer": "https://quote.eastmoney.com/"}))
    A50_SOURCES = (("东方财富", IndexFutures.EM + "104.CN00Y", {"Referer": "https://quote.eastmoney.com/"}),
                   ("新浪CFD", "https://hq.sinajs.cn/list=hf_CHA50CFD", {"Referer": "https://finance.sina.com.cn/"}))
    A50_MINUTES = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=104.CN00Y&klt=1&fqt=0"
                   "&fields1=f1&fields2=f51,f52,f53&beg={beg}&end={end}")

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.quote: IndexQuote | None = None
        self.a50: IndexQuote | None = None
        self.error = ""
        self.a50_error = ""
        self.refreshed = -1e9

    @staticmethod
    def parse_a50(source: str, raw: bytes, now_ms: int) -> IndexQuote:
        if source == "东方财富":
            q = parse_eastmoney_index(raw, now_ms, "A50期货")
            return IndexQuote("A50期货", q.last, q.prev_close, q.open, q.high, q.low, q.quoted_ms, source)
        # Sina hf_: last, ?, bid, ask, high, low, time, prev settle, open, ..., name [13], date [14]
        match = re.search(r'="([^"]*)"', raw.decode("gbk", errors="ignore"))
        fields = match.group(1).split(",") if match else []
        if len(fields) < 15 or not fields[0]:
            raise ValueError("新浪 A50 报价为空")
        try:
            quoted_ms = int(dt.datetime.strptime(f"{fields[14]} {fields[6]}", "%Y-%m-%d %H:%M:%S")
                            .replace(tzinfo=BEIJING).timestamp() * 1000)
        except ValueError:
            quoted_ms = now_ms
        return IndexQuote("A50期货", number(fields[0], "A50"), _opt(fields[7]), _opt(fields[8]), _opt(fields[4]),
                          _opt(fields[5]), quoted_ms, source)

    async def _first(self, sources: tuple, parse, now_ms: int, previous):
        failures = []
        for name, url, extra in sources:
            try:
                raw = await http_get(url, headers={"User-Agent": BROWSER_UA, "Accept": "*/*", **extra})
                return parse(name, raw, now_ms), ""
            except Exception as error:
                failures.append(f"{name}: {clean_error(error)}")
        return previous, "；".join(failures)

    async def refresh(self, now_ms: int, force: bool = False) -> None:
        if not self.enabled or (not force and time.monotonic() - self.refreshed < self.REFRESH_SECONDS):
            return
        self.refreshed = time.monotonic()
        self.quote, self.error = await self._first(self.SSE_SOURCES, parse_cn_index, now_ms, self.quote)
        self.a50, self.a50_error = await self._first(self.A50_SOURCES, self.parse_a50, now_ms, self.a50)

    async def a50_at(self, day: dt.date) -> D:
        """A50 price at the Composite's 15:00 close on ``day``: close of the 14:59-15:00 minute bar."""
        url = self.A50_MINUTES.format(beg=(day - dt.timedelta(days=1)).strftime("%Y%m%d"),
                                      end=(day + dt.timedelta(days=1)).strftime("%Y%m%d"))
        raw = await http_get(url, headers={"User-Agent": BROWSER_UA, "Referer": "https://quote.eastmoney.com/"})
        try:
            klines = json.loads(raw)["data"]["klines"]
        except (ValueError, KeyError, TypeError):
            raise ValueError("A50 分钟线返回格式异常") from None
        wanted = f"{day.isoformat()} 15:00"
        for line in klines:
            parts = str(line).split(",")
            if parts[0] == wanted and len(parts) >= 3:
                return number(parts[2], "A50 15:00")
        raise ValueError(f"A50 分钟线里没有 {wanted}")

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
        return line + f"｜⚠️ 刷新失败：{brief_error(self.error)}" if self.error else line

    def a50_line(self, now_ms: int, style: str, anchor: D | None) -> str:
        if not self.enabled:
            return ""
        a = self.a50
        if a is None:
            return f"📈 A50期货 ⚠️ 获取失败（{brief_error(self.a50_error)}）" if self.a50_error else "📈 A50期货 ⏳ 等待首次获取"
        line = f"📈 {bold('A50期货 ' + a50_session(a.quoted_ms))} {bold(fmt(a.last))}"
        if anchor:
            line += f" → 上证收盘时 {bold(fmt(anchor))} {pct_text(percent(a.last, anchor), style)}"
        if a.prev_close:
            line += f"｜昨结 {fmt(a.prev_close)} {pct_text(percent(a.last, a.prev_close), style)}"
        source = a.source if "CFD" not in a.source else f"{a.source}·非交易所合约，仅参考"
        line += f"｜{stamp(a.quoted_ms, seconds=False)} {source}" + stale_note(a.quoted_ms, now_ms, BEIJING)
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

    async def refresh(self, now_ms: int, force: bool = False) -> None:
        if not self.enabled or (not force and time.monotonic() - self.refreshed < self.REFRESH_SECONDS):
            return
        self.refreshed = time.monotonic()
        self.quote, self.error = await self._fetch(self.SOURCES, now_ms, self.quote)
        self.quote200, self.error200 = await self._fetch(self.SOURCES_200, now_ms, self.quote200)

    async def _fetch(self, sources: tuple, now_ms: int, previous: IndexQuote | None) -> tuple[IndexQuote | None, str]:
        failures = []
        for name, url, extra in sources:
            try:
                raw = await http_get(url, headers={"User-Agent": BROWSER_UA, "Accept": "*/*", **extra})
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
        local = dt.datetime.fromtimestamp(q.quoted_ms / 1000, kst)
        line += f"｜{local.strftime('%m-%d %H:%M')} 韩国时间 {q.source}" + stale_note(q.quoted_ms, now_ms, kst)
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
        local = dt.datetime.fromtimestamp(q.quoted_ms / 1000, kst)
        line += f"｜{local.strftime('%m-%d %H:%M')} 韩国时间 {q.source}" + stale_note(q.quoted_ms, now_ms, kst)
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
        ]


def close_odds(name: str, ref: D, effective: D, sigma_daily: float, remaining: float, target: dt.date, tick: D,
               ref_note: str, proxy_note: str, sigma_note: str, unit: str = "") -> CloseOdds:
    sigma = max(sigma_daily * math.sqrt(max(remaining, 1e-6)), 1e-9)
    half = tick / 2
    hi = math.log(float((ref + half) / effective)) / sigma
    lo = math.log(float((ref - half) / effective)) / sigma if ref > half else -math.inf
    up, down = 1 - norm_cdf(hi), norm_cdf(lo)
    return CloseOdds(name, target, ref, ref_note, proxy_note, effective, sigma_daily, sigma_note, remaining,
                     up, max(0.0, 1 - up - down), down, unit)


def realised_vol(closes: list[D]) -> tuple[float, int]:
    """Sample standard deviation of daily log returns, and the number of returns used."""
    values = [float(c) for c in closes if c and c > 0]
    returns = [math.log(b / a) for a, b in zip(values, values[1:])]
    if len(returns) < 2:
        return 0.0, len(returns)
    mean = sum(returns) / len(returns)
    return math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)), len(returns)


class VolBook:
    """Daily volatility per asset: manual override, else recent realised volatility blended with a prior."""
    REFRESH_SECONDS = 6 * 3600

    def __init__(self, overrides: dict[str, float]):
        self.overrides = dict(overrides)
        self.estimates: dict[str, tuple[float, int, str]] = {}   # key -> (sigma, n, source)
        self.refreshed: dict[str, float] = {}

    def due(self, key: str) -> bool:
        return key not in self.overrides and time.monotonic() - self.refreshed.get(key, -1e9) >= self.REFRESH_SECONDS

    def record(self, key: str, closes: list[D], source: str) -> None:
        sigma, n = realised_vol(closes)
        self.estimates[key] = (sigma, n, source)
        self.refreshed[key] = time.monotonic()

    def get(self, key: str, prior_key: str) -> tuple[float, str]:
        if key in self.overrides:
            return self.overrides[key], "PROB_VOL 手动设定"
        prior = PRIOR_VOL[prior_key]
        sigma, n, source = self.estimates.get(key, (0.0, 0, ""))
        if n < 2:
            return prior, f"先验 {prior * 100:.1f}%，暂无历史"
        blended = math.sqrt((n * sigma ** 2 + PRIOR_WEIGHT * prior ** 2) / (n + PRIOR_WEIGHT))
        return blended, f"{source} {n} 日 {sigma * 100:.2f}% 与先验 {prior * 100:.1f}% 加权"


# --- read-only probability web page -------------------------------------------------------------
WEB_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>收盘涨跌概率</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2125;--muted:#6b737c;--line:#e3e6ea;--up:#d93a3a;--down:#1f9d55;--flat:#9aa3ad}
@media (prefers-color-scheme:dark){:root{--bg:#121417;--card:#1c1f23;--text:#e8eaed;--muted:#9aa3ad;--line:#2c3137}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
header{padding:16px;max-width:1100px;margin:0 auto}h1{font-size:20px;margin:0 0 4px}
.meta{color:var(--muted);font-size:13px}.meta span{margin-right:12px}
main{max-width:1100px;margin:0 auto;padding:0 16px 24px;display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(320px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.top{display:flex;justify-content:space-between;align-items:baseline;gap:8px}.name{font-weight:600}.target{color:var(--muted);font-size:13px;white-space:nowrap}
.odds{display:flex;justify-content:space-between;margin:10px 0 6px;font-variant-numeric:tabular-nums}
.odds b{font-size:24px}.u{color:var(--up)}.d{color:var(--down)}
.bar{display:flex;height:10px;border-radius:5px;overflow:hidden;background:var(--line)}
.bar i{display:block;height:100%}
dl{display:grid;grid-template-columns:auto 1fr;gap:2px 10px;margin:10px 0 0;font-size:13px}dt{color:var(--muted)}dd{margin:0;font-variant-numeric:tabular-nums;word-break:break-word}
.missing{color:var(--muted)}.warn{color:#c77c00}
footer{max-width:1100px;margin:0 auto;padding:0 16px 24px;color:var(--muted);font-size:12px}
</style></head><body>
<header><h1>收盘涨跌概率</h1><div class="meta" id="meta">加载中…</div></header>
<main id="cards"></main>
<footer id="foot">模型参考，非投资建议。</footer>
<script>
const $=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e};
function pct(x){return (x*100).toFixed(1)}
function card(it,style){
  const c=$("div","card"),top=$("div","top");
  top.append($("div","name",it.name),$("div","target",it.target?("目标 "+it.target+" 收盘"):""));c.append(top);
  if(it.missing){c.append($("p","missing","概率暂缺："+it.missing));return c}
  const upCls=style==="us"?"d":"u",dnCls=style==="us"?"u":"d";
  const o=$("div","odds"),a=$("div"),b=$("div");
  a.append($("span","","涨 "));const ua=$("b",upCls,pct(it.fair_up)+"¢");a.append(ua);
  b.append($("span","","跌 "));const db=$("b",dnCls,pct(it.fair_down)+"¢");b.append(db);o.append(a,b);c.append(o);
  const bar=$("div","bar"),iu=$("i"),iff=$("i"),idn=$("i");
  iu.style.width=(it.up*100)+"%";iu.style.background="var(--"+(style==="us"?"down":"up")+")";
  iff.style.width=(it.flat*100)+"%";iff.style.background="var(--flat)";
  idn.style.width=(it.down*100)+"%";idn.style.background="var(--"+(style==="us"?"up":"down")+")";
  bar.append(iu,iff,idn);c.append(bar);
  const dl=$("dl");const row=(k,v)=>{dl.append($("dt","",k),$("dd","",v))};
  row("参考收盘",it.ref+(it.unit?" "+it.unit:"")+"（"+it.ref_note+"）");
  row("有效价",it.effective+(it.unit?" "+it.unit:"")+"（"+(it.move>=0?"+":"")+it.move.toFixed(3)+"%）");
  row("代理",it.proxy_note);
  row("σ","日 "+(it.sigma_daily*100).toFixed(2)+"% × √"+it.remaining.toFixed(3)+" = "+(it.sigma*100).toFixed(2)+"%（"+it.sigma_note+"）");
  row("严格涨/平/跌",(it.up*100).toFixed(2)+"% / "+(it.flat*100).toFixed(2)+"% / "+(it.down*100).toFixed(2)+"%，z "+it.z.toFixed(3));
  c.append(dl);return c}
async function load(){
  try{
    const r=await fetch(location.pathname.replace(/\/$/,"")+"/data.json",{cache:"no-store"});
    if(!r.ok)throw new Error("HTTP "+r.status);
    const d=await r.json();const box=document.getElementById("cards");box.replaceChildren(...d.items.map(it=>card(it,d.color_style)));
    const m=document.getElementById("meta");m.replaceChildren($("span","","更新 "+d.generated_at),$("span","","基准 "+d.mode),$("span","","v"+d.version));
    document.getElementById("foot").textContent=d.note;
  }catch(e){const m=document.getElementById("meta");m.replaceChildren($("span","warn","刷新失败："+e.message+"，稍后自动重试"))}
}
load();setInterval(load,10000);
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
    Command("web", "获取概率网页链接（自动刷新）"),
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
        self.hsi = IndexFutures(config.hsi_futures)
        self.hl = Hyperliquid({**config.hl_tickers, **config.hl_index})
        self.kospi = KospiIndex(config.kospi_index)
        self.cn = CnIndex(config.sse_index)
        self.web_token = config.web_token or self.store.get("web_token") or ""
        if config.web_port and not self.web_token:
            self.web_token = secrets.token_urlsafe(18)
            self.store.put("web_token", self.web_token)
        self.web: WebServer | None = None
        self.vols = VolBook(config.prob_vol)
        self.anchors: dict[str, tuple[int, D]] = {}  # key -> (reference close ms, proxy price then)
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
            "/setexchange": self.cmd_setexchange, "/prob": self.cmd_prob, "/web": self.cmd_web,
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
        """The instant the underlying's exchange last fixed a closing price (session complete).

        Uses the fetched exchange bar when available so the baseline and the exchange close
        share one timestamp; otherwise falls back to the venue's weekday close-time calendar.
        """
        ticker = self.config.tickers.get(symbol)
        if not ticker:
            raise ValueError("该合约未配置证券交易所代码（EXCHANGE_TICKERS），无法按交易所收盘时刻取基准")
        info = STOCK_MARKETS[ticker.market]
        ref = self.stocks.closes.get(symbol)
        if ref and ref.close_ms and ref.close_ms + 15 * 60_000 <= now_ms:
            return ref.close_ms, info, f"{info.name}{ticker.code}"
        tz = dt.timezone(dt.timedelta(hours=info.utc_offset))
        local = dt.datetime.fromtimestamp(now_ms / 1000, tz)
        for back in range(0, 10):
            day = local.date() - dt.timedelta(days=back)
            candidate = dt.datetime.combine(day, info.close_time, tz)
            if day.weekday() < 5 and candidate + dt.timedelta(minutes=15) <= local:
                return int(candidate.timestamp() * 1000), info, f"{info.name}{ticker.code}·按日历推算"
        raise ValueError("找不到最近的交易所收盘时刻")

    async def exchange_time_baseline(self, symbol: str, now_ms: int) -> Baseline:
        """Binance contract price at the underlying exchange's latest close: same instant as the
        exchange close, so 相对基准 and 相对交易所 are directly comparable."""
        close_ms, info, source = self.exchange_close_ms(symbol, now_ms)
        price, kind = await self.market.price_at(symbol, close_ms)
        return Baseline(price, f"exchange_time:{close_ms}:{price}",
                        f"币安合约{kind}@{info.name}收盘时刻" + info.close_label(close_ms) + f"｜{source}",
                        close_ms + 4 * DAY_MS, close_ms)

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
            ref = self.reference_for("exchange", symbol, now_ms)
            if ref and ref.close_ms and self.anchors.get(symbol, (0,))[0] != ref.close_ms and self.retry_ok(symbol):
                with contextlib.suppress(Exception):
                    price, _ = await self.market.price_at(symbol, ref.close_ms)
                    self.anchors[symbol] = (ref.close_ms, price)
            if self.vols.due(symbol):
                self.vols.refreshed[symbol] = time.monotonic()  # one attempt per window even if it fails
                with contextlib.suppress(Exception):
                    rows = await self.market.get("/fapi/v1/klines", symbol=symbol, interval="1d", limit=31)
                    closes = [number(r[4], "收盘") for r in rows[:-1] if isinstance(r, list) and len(r) > 4]
                    self.vols.record(symbol, closes, "币安日K")
        kospi, hl = self.kospi.quote, self.hl.quotes.get("KR200")
        if kospi and hl:
            close_ms = self.kospi_close_ms(kospi)
            if self.anchors.get("KOSPI", (0,))[0] != close_ms and self.retry_ok("KOSPI"):
                with contextlib.suppress(Exception):
                    self.anchors["KOSPI"] = (close_ms, await self.hl.price_at(hl.coin, close_ms))
        q = self.hsi.quote
        if q and q.spot is not None:
            close_date = self.hk_cash_close_date(now_ms)
            local = dt.datetime.fromtimestamp(q.quoted_ms / 1000, BEIJING)
            close_ms = int(dt.datetime.combine(close_date, dt.time(16, 10), BEIJING).timestamp() * 1000)
            # First futures print after the cash close = the futures level the close is anchored to.
            if (hk_futures_session(q.quoted_ms) != "夜市" and local.date() == close_date and local.time() >= dt.time(16, 10)
                    and self.anchors.get("HSI", (0,))[0] != close_ms):
                self.anchors["HSI"] = (close_ms, q.last)

        sse = self.cn.quote
        if sse is not None:
            close_ms = self.sse_close_ms(sse)
            saved = self.anchors.get("A50") or tuple(self.store.get("anchor:A50", (0, "0")))
            if saved[0] == close_ms:
                self.anchors["A50"] = (close_ms, D(str(saved[1])))
            elif self.retry_ok("A50"):
                price = None
                with contextlib.suppress(Exception):
                    price = await self.cn.a50_at(dt.datetime.fromtimestamp(close_ms / 1000, BEIJING).date())
                a50 = self.cn.a50
                if price is None and a50 and 0 <= a50.quoted_ms - close_ms <= 5 * 60_000:
                    price = a50.last  # the first A50 print within 5 minutes after 15:00
                if price is not None:
                    self.anchors["A50"] = (close_ms, price)
                    self.store.put("anchor:A50", [close_ms, str(price)])
        if self.vols.due("SSE"):
            self.vols.refreshed["SSE"] = time.monotonic()
            with contextlib.suppress(Exception):
                raw = await http_get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,,,40,",
                                     headers={"User-Agent": BROWSER_UA, "Referer": "https://gu.qq.com/"})
                days = json.loads(raw)["data"]["sh000001"]
                rows = days.get("day") or days.get("qfqday") or []
                self.vols.record("SSE", [number(r[2], "收盘") for r in rows if len(r) > 2], "上证日K")
        for key, url, market in (("KOSPI", "https://fchart.stock.naver.com/sise.nhn?requestType=0&timeframe=day&count=40&symbol=KOSPI", "kr"),
                                 ("HSI", "https://push2his.eastmoney.com/api/qt/stock/kline/get?klt=101&fqt=0&end=20500101&lmt=40"
                                         "&fields1=f1&fields2=f51,f52,f53&secid=100.HSI", "hk")):
            if self.vols.due(key):
                self.vols.refreshed[key] = time.monotonic()  # one attempt per window even if it fails
                with contextlib.suppress(Exception):
                    raw = await http_get(url, headers={"User-Agent": BROWSER_UA, "Accept": "*/*"})
                    self.vols.record(key, [c for _, c in parse_daily_bars(market, raw)], "指数日K")

    def retry_ok(self, key: str, every: float = 60) -> bool:
        """Throttle failing anchor lookups to one attempt per minute per key."""
        now = time.monotonic()
        if now - self.anchor_tries.get(key, -1e9) < every:
            return False
        self.anchor_tries[key] = now
        return True

    def sse_close_ms(self, q: IndexQuote) -> int:
        """15:00 on the Composite's latest completed session (the quote's day once it has closed)."""
        local = dt.datetime.fromtimestamp(q.quoted_ms / 1000, BEIJING)
        day = local.date()
        if local.time() < dt.time(15, 0):  # quote from an unfinished session → the session before it
            day -= dt.timedelta(days=1)
        holidays = self.config.holidays.get("sh", frozenset())
        while day.weekday() >= 5 or day in holidays:
            day -= dt.timedelta(days=1)
        return int(dt.datetime.combine(day, dt.time(15, 0), BEIJING).timestamp() * 1000)

    def sse_odds(self, now_ms: int) -> CloseOdds | str | None:
        q = self.cn.quote
        if not self.config.probability or not self.config.sse_index:
            return None
        if q is None:
            return "缺少上证指数"
        holidays = self.config.holidays.get("sh", frozenset())
        sigma, sigma_note = self.vols.get("SSE", "SSE")
        if self.cn.status(now_ms, holidays) == "交易中" and q.prev_close:
            today = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING).date()
            remaining, target = session_remaining("sh", now_ms, today - dt.timedelta(days=1), holidays)
            return close_odds("上证指数", q.prev_close, q.last, sigma, remaining, target, D("0.01"), "昨收",
                              f"上证现货 {fmt(q.last)}（盘中直接用现货）", sigma_note)
        close_ms = self.sse_close_ms(q)
        close_date = dt.datetime.fromtimestamp(close_ms / 1000, BEIJING).date()
        remaining, target = session_remaining("sh", now_ms, close_date, holidays)
        a50, anchor = self.cn.a50, self.anchors.get("A50")
        if a50 is None:
            return "缺少 A50 期货报价"
        if not anchor or anchor[0] != close_ms:
            return "等待 A50 在上证 15:00 收盘时的价格"
        beta = self.config.a50_beta
        move = math.log(float(a50.last / anchor[1]))
        effective = q.last * D(str(math.exp(beta * move)))
        return close_odds("上证指数", q.last, effective, sigma, remaining, target, D("0.01"),
                          f"{close_date.strftime('%m-%d')} 收盘",
                          f"A50 {fmt(a50.last)} / 15:00 {fmt(anchor[1])} → {percent(a50.last, anchor[1]):+.3f}% × β {beta:g}", sigma_note)

    @staticmethod
    def kospi_close_ms(q: IndexQuote) -> int:
        kst = dt.timezone(dt.timedelta(hours=9))
        day = dt.datetime.fromtimestamp(q.quoted_ms / 1000, kst).date()
        return int(dt.datetime.combine(day, dt.time(15, 30), kst).timestamp() * 1000)

    @staticmethod
    def hk_cash_close_date(now_ms: int) -> dt.date:
        local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
        day = local.date() if local.time() >= dt.time(16, 10) else local.date() - dt.timedelta(days=1)
        while day.weekday() >= 5:
            day -= dt.timedelta(days=1)
        return day

    def contract_odds(self, symbol: str, price: D, now_ms: int) -> CloseOdds | str | None:
        ticker = self.config.tickers.get(symbol)
        if not self.config.probability or not ticker:
            return None
        ref = self.reference_for("exchange", symbol, now_ms)
        if ref is None or not ref.close_ms:
            return "缺少带收盘时刻的交易所收盘价"
        anchor = self.anchors.get(symbol)
        if not anchor or anchor[0] != ref.close_ms:
            return "等待币安在收盘时刻的价格"
        info = STOCK_MARKETS[ticker.market]
        close_date = dt.datetime.fromtimestamp(ref.close_ms / 1000, dt.timezone(dt.timedelta(hours=info.utc_offset))).date()
        remaining, target = session_remaining(ticker.market, now_ms, close_date, self.config.holidays.get(ticker.market, frozenset()))
        sigma, sigma_note = self.vols.get(symbol, ticker.market)
        move = percent(price, anchor[1])
        return close_odds(NAMES.get(symbol, symbol), ref.value, ref.value * price / anchor[1], sigma, remaining, target,
                          price_tick(ticker.market, ref.value), f"{close_when(ref)}·{short_source(ref.source)}",
                          f"币安 {fmt_price(price)} / 收盘时刻 {fmt_price(anchor[1])} → {move:+.3f}%", sigma_note,
                          "" if ticker.same_unit else (ref.currency or info.currency))

    def hsi_odds(self, now_ms: int) -> CloseOdds | str | None:
        q = self.hsi.quote
        if not self.config.probability or not self.config.hsi_futures:
            return None
        if q is None or q.spot is None:
            return "缺少恒指现货"
        local = dt.datetime.fromtimestamp(now_ms / 1000, BEIJING)
        cash_open = local.weekday() < 5 and dt.time(9, 30) <= local.time() < dt.time(16, 10)
        sigma, sigma_note = self.vols.get("HSI", "HSI")
        if cash_open and q.spot_prev:
            remaining, target = session_remaining("hk", now_ms, local.date() - dt.timedelta(days=1), self.config.holidays.get("hk", frozenset()))
            return close_odds("恒生指数", q.spot_prev, q.spot, sigma, remaining, target, D("0.01"), "昨收",
                              f"恒指现货 {fmt(q.spot)}（盘中直接用现货）", sigma_note)
        close_date = self.hk_cash_close_date(now_ms)
        anchor, anchor_note = None, "收市时"
        if hk_futures_session(q.quoted_ms) != "夜市":
            anchor, anchor_note = q.last, "日市收市"  # No night trading yet: the close itself is the best estimate.
        elif q.source == "etnet" and q.prev_settle:
            anchor, anchor_note = q.prev_settle, "日市收市"  # night block's 前收市 = the day session before it
        elif self.anchors.get("HSI") and dt.datetime.fromtimestamp(self.anchors["HSI"][0] / 1000, BEIJING).date() == close_date:
            anchor = self.anchors["HSI"][1]
        if anchor is None:
            return "缺少期货在现货收市时的价格"
        remaining, target = session_remaining("hk", now_ms, close_date, self.config.holidays.get("hk", frozenset()))
        return close_odds("恒生指数", q.spot, q.spot * q.last / anchor, sigma, remaining, target, D("0.01"),
                          f"{close_date.strftime('%m-%d')} 收盘", f"恒指期货 {fmt(q.last)} / {anchor_note} {fmt(anchor)} → {percent(q.last, anchor):+.3f}%",
                          sigma_note)

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
            remaining, target = session_remaining("kr", now_ms, quoted_day - dt.timedelta(days=1), self.config.holidays.get("kr", frozenset()))
            return close_odds("KOSPI", k.prev_close, k.last, sigma, remaining, target, D("0.01"), "昨收",
                              f"KOSPI 现货 {fmt(k.last)}（盘中直接用现货）", sigma_note)
        hl, anchor = self.hl.quotes.get("KR200"), self.anchors.get("KOSPI")
        remaining, target = session_remaining("kr", now_ms, quoted_day, self.config.holidays.get("kr", frozenset()))
        if hl is None:
            return "缺少 HL KR200 代理"
        if anchor and anchor[0] == self.kospi_close_ms(k):
            base, base_note = anchor[1], "收盘时刻"
        elif self.kospi.quote200:
            base, base_note = self.kospi.quote200.last, "KOSPI200 收盘（HL 收盘时刻价格暂缺）"
        else:
            return "等待 HL KR200 在收盘时刻的价格"
        return close_odds("KOSPI", k.last, k.last * hl.mark / base, sigma, remaining, target, D("0.01"),
                          f"{quoted_day.strftime('%m-%d')} 收盘",
                          f"HL KR200 {fmt(hl.mark)} / {base_note} {fmt(base)} → {percent(hl.mark, base):+.3f}%（KOSPI200 代理，存在基差）",
                          sigma_note)

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
        return [(title, odds) for title, odds in items if odds is not None]

    def odds_payload(self) -> dict:
        """JSON for the web page: one entry per item, numbers raw, text already plain."""
        now_ms = self.market.now_ms()
        items = []
        for title, odds in (self.odds_items(now_ms) if self.config.probability else []):
            if isinstance(odds, str):
                items.append({"name": title, "missing": odds})
                continue
            items.append({
                "name": title, "target": odds.target.strftime("%m-%d"), "unit": odds.unit,
                "ref": fmt(odds.ref), "ref_note": odds.ref_note, "effective": fmt(odds.effective.quantize(D("0.0001"))),
                "move": float(percent(odds.effective, odds.ref)), "proxy_note": odds.proxy_note,
                "sigma_daily": odds.sigma_daily, "sigma": odds.sigma, "remaining": odds.remaining, "sigma_note": odds.sigma_note,
                "z": odds.z, "up": odds.up, "flat": odds.flat, "down": odds.down,
                "fair_up": odds.fair_up, "fair_down": odds.fair_down,
            })
        return {"generated_at": stamp(now_ms) + "（北京时间）", "version": VERSION,
                "mode": BASELINE_SHORT.get(self.settings()["mode"], self.settings()["mode"]),
                "color_style": self.config.color_style, "items": items,
                "note": ("模型参考，非投资建议。有效价 = 参考收盘 × 代理现价 ÷ 代理在参考收盘时刻的价格；"
                         "P(涨) = 1 − Φ(ln((参考+半跳)/有效)/σ剩余)，平盘两边各计一半。目标日跳过周末和已配置的交易所假期。"
                         if self.config.probability else "概率功能已关闭（PROBABILITY=off）。")}

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
            lines.extend(tree(odds.detail() if isinstance(odds, CloseOdds) else [f"概率暂缺：{odds}"]))
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
            lines.append(self.cn.a50_line(now_ms, style, anchor[1] if anchor and self.cn.quote
                                           and anchor[0] == self.sse_close_ms(self.cn.quote) else None))
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

        # Best effort; failures are reported in /status and never block price alerts.
        await asyncio.gather(self.stocks.refresh(now_ms), self.fx.refresh(), self.hsi.refresh(now_ms), self.hl.refresh(),
                             self.kospi.refresh(now_ms), self.cn.refresh(now_ms))

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
            except Exception as error:
                return symbol, {"error": clean_error(error)}

        collected = dict(await asyncio.gather(*(collect(s) for s in self.config.symbols)))
        # A Telegram command may change global settings while a request is in flight.
        # Discard the old batch rather than sending alerts using a now-obsolete mode.
        if self.settings() != settings:
            return
        self.snapshots = collected
        if self.config.probability:
            with contextlib.suppress(Exception):  # Probabilities are informational; never block alerts.
                await self.refresh_odds_inputs(now_ms)
        for sub_id, sub in self.subscriptions().items():
            if not sub.get("active"):
                continue
            await self.notice(sub_id, sub, "币安行情接口", None)
            for symbol in self.config.symbols:
                snapshot = collected[symbol]
                error = snapshot.get("error")
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
        tasks = [asyncio.create_task(self.monitor_loop()), asyncio.create_task(self.commands_loop())]
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
            if self.web:
                await self.web.stop()
            LOG.info("Stopped safely")


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
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = None
    try:
        config = Config.from_env()
        if args.check:
            return asyncio.run(check_market(config))
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
