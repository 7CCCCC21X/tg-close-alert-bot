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
import json
import logging
import os
import re
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

D = decimal.Decimal
UTC = dt.timezone.utc
BEIJING = dt.timezone(dt.timedelta(hours=8))
DAY_MS = 86_400_000
VERSION = "1.0.0"
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


def percent(price: D, baseline: D) -> D:
    return (number(price) - number(baseline)) / baseline * D(100)


def beijing_day(now: float | None = None) -> str:
    return dt.datetime.fromtimestamp(time.time() if now is None else now, BEIJING).date().isoformat()


def stamp(ms: int | float) -> str:
    return dt.datetime.fromtimestamp(float(ms) / 1000, BEIJING).strftime("%m-%d %H:%M:%S")


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

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        e = dict(os.environ if env is None else env)
        symbols = tuple(dict.fromkeys(s.strip().upper() for s in
                        e.get("SYMBOLS", ",".join(NAMES)).split(",") if s.strip()))
        if not symbols or len(symbols) > 30 or any(not re.fullmatch(r"[A-Z0-9_]{3,40}", s) for s in symbols):
            raise ValueError("SYMBOLS 应为 1～30 个逗号分隔的币安合约代码")
        mode = e.get("BASELINE_MODE", "binance_daily").strip()
        if mode not in {"binance_daily", "manual"}:
            raise ValueError("BASELINE_MODE 只能是 binance_daily 或 manual")
        url = e.get("BINANCE_BASE_URL", "https://fapi.binance.com").rstrip("/")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
            raise ValueError("BINANCE_BASE_URL 必须是无账号、查询参数的 HTTPS 根地址")
        threshold = number(e.get("ALERT_THRESHOLD_PCT", "1"), "ALERT_THRESHOLD_PCT")
        if not D("0.01") <= threshold <= D(100):
            raise ValueError("ALERT_THRESHOLD_PCT 必须在 0.01～100 之间，1 表示 1%")
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


def _http_json(url: str, payload: dict | None = None, timeout: int = 15) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": f"CloseAlert/{VERSION}", "Accept": "application/json",
        **({"Content-Type": "application/json"} if data is not None else {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise RemoteError("接口返回的数据过大")
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise RemoteError("接口未返回有效 JSON") from None
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


async def http_json(url: str, payload: dict | None = None, timeout: int = 15) -> Any:
    return await asyncio.to_thread(_http_json, url, payload, timeout)


@dataclass(frozen=True)
class Quote:
    price: D
    timestamp_ms: int

    @classmethod
    def parse(cls, row: dict, symbol: str, now_ms: int, max_age: int) -> "Quote":
        if row.get("symbol") != symbol:
            raise ValueError(f"行情代码不匹配：{symbol}")
        value = number(row.get("price"), "最新成交价")
        try:
            timestamp = int(row["time"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("行情缺少有效时间戳，停止涨跌提醒") from None
        if timestamp <= 0 or timestamp > now_ms + 30_000:
            raise ValueError("行情时间戳异常，停止涨跌提醒")
        age = (now_ms - timestamp) / 1000
        if age > max_age:
            raise ValueError(f"最新成交价已过期（{int(age)} 秒未更新），暂不发涨跌提醒")
        return cls(value, timestamp)


@dataclass(frozen=True)
class Baseline:
    value: D
    key: str
    label: str
    valid_until_ms: int


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
                            f"币安日 K 昨收｜{day}（UTC）", boundary + DAY_MS)
    raise ValueError("没有完整的上一 UTC 日日 K（可能新上市或接口数据未就绪）；不使用旧基准")


def manual_baseline(record: dict | None, now_ms: int) -> Baseline:
    today = beijing_day(now_ms / 1000)
    if not record or record.get("valid_date") != today:
        raise ValueError(f"缺少 {today} 的手动基准；请用 /setclose 设置，不能沿用过期价格")
    value = number(record.get("value"), "手动基准")
    until = dt.datetime.combine(dt.date.fromisoformat(today) + dt.timedelta(days=1),
                                dt.time(), BEIJING)
    return Baseline(value, f"manual:{today}:{value}",
                    f"手动参考价｜适用日 {today}（北京时间）", int(until.timestamp() * 1000))


class Binance:
    def __init__(self, config: Config):
        self.config = config
        self.cached: dict[str, Baseline] = {}
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
        data = await self.get("/fapi/v2/ticker/price")
        if not isinstance(data, list):
            raise ValueError("币安最新价接口没有返回合约列表")
        return {r["symbol"]: r for r in data if isinstance(r, dict) and r.get("symbol") in self.config.symbols}

    async def baseline(self, symbol: str, now_ms: int) -> Baseline:
        old = self.cached.get(symbol)
        if old and old.valid_until_ms - DAY_MS <= now_ms < old.valid_until_ms:
            return old
        boundary = now_ms // DAY_MS * DAY_MS
        rows = await self.get("/fapi/v1/klines", symbol=symbol, interval="1d", endTime=boundary - 1, limit=3)
        baseline = daily_baseline(rows, now_ms)
        self.cached[symbol] = baseline
        return baseline


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

    async def send(self, chat: int, thread: int, text: str) -> None:
        # Plain text avoids HTML/Markdown escaping problems in symbols/errors.
        for chunk in split_text(text):
            async with self.lock:
                await asyncio.sleep(max(0, self.next_send - time.monotonic()))
                payload: dict[str, Any] = {"chat_id": chat, "text": chunk,
                                          "link_preview_options": {"is_disabled": True}}
                if thread:
                    payload["message_thread_id"] = thread
                try:
                    await self.call("sendMessage", payload)
                except RemoteError as error:
                    self.next_send = time.monotonic() + max(1.1, error.retry_after)
                    raise
                self.next_send = time.monotonic() + 1.1


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


HELP = """📡 合约昨收偏离提醒

/subscribe  在当前私聊/群组话题订阅
/unsubscribe  取消当前订阅
/status  查看四个合约、基准与数据状态
/threshold 1  改为严格超过 ±1% 提醒
/cooldown 300  持续超标每 300 秒提醒（0=关闭周期提醒）
/mode daily  自动使用币安上一 UTC 日日 K 收盘
/mode manual  使用手动同口径参考价
/setclose UNITREE 75 2026-09-18
  示例：75 是基准；日期是适用日，不是收盘发生日
/pause  暂停当前订阅
/resume  恢复当前订阅
/test  发送测试消息，不代表行情正常
/id  查看你的用户 ID、聊天 ID、话题 ID
/help  显示说明

⚠️ 默认并非股票交易所正式昨收，也不是滚动 24h 涨跌幅。
手动价必须与币安合约显示数值采用同一计价口径；不自动换汇。
只有管理员能订阅或修改设置；不会自动下单、撤单。"""


class Bot:
    def __init__(self, config: Config, store: Store, market: Binance, telegram: Telegram):
        self.config, self.store, self.market, self.telegram = config, store, market, telegram
        self.snapshots: dict[str, dict] = {}
        self.stopping = asyncio.Event()
        self.started = time.time()
        self.last_cycle = 0.0
        self.last_log: dict[str, float] = {}
        self.command_notice: dict[int, float] = {}
        self.username = ""

    def settings(self) -> dict:
        saved = self.store.get("settings", {})
        return {"threshold": str(self.config.threshold), "cooldown": self.config.cooldown,
                "mode": self.config.baseline_mode, **saved}

    def subscriptions(self) -> dict:
        return self.store.get("subscriptions", {})

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

    async def tell(self, chat: int, thread: int, text: str) -> bool:
        try:
            await self.telegram.send(chat, thread, text)
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

    async def process_message(self, message: dict) -> None:
        text = message.get("text", "").strip()
        if not text.startswith("/"):
            return
        parts = text.split()
        raw_command = parts[0].lower()
        if "@" in raw_command and self.username and raw_command.split("@", 1)[1] != self.username.lower():
            return
        command = raw_command.split("@", 1)[0]
        chat, thread = target_from_message(message)
        sender = message.get("from") or {}
        # Only /id and public help work before administrator configuration.
        if command in {"/id", "/start", "/help"} and not is_admin(message, self.config):
            uid = int(sender.get("id") or 0)
            now = time.monotonic()
            if now - self.command_notice.get(uid, -1e9) < 10:
                return
            if len(self.command_notice) > 2000:
                self.command_notice.clear()
            self.command_notice[uid] = now
            await self.tell(chat, thread, f"你的用户 ID：{uid}\n聊天 ID：{chat}\n话题 ID：{thread}\n"
                            "把你的用户 ID 填入 Railway 的 ADMIN_USER_ID 后重新部署，再发 /subscribe。")
            return
        if not is_admin(message, self.config):
            return  # Do not expose settings or allow arbitrary group members to alter them.
        sub_id = subscription_key(chat, thread)
        try:
            if command in {"/start", "/help"}:
                reply = HELP
            elif command == "/id":
                reply = f"你的用户 ID：{sender['id']}\n聊天 ID：{chat}\n话题 ID：{thread}"
            elif command in {"/subscribe", "/resume", "/pause", "/unsubscribe"}:
                subs = self.subscriptions()
                if command == "/unsubscribe":
                    subs.pop(sub_id, None)
                    self.store.delete_prefix(f"alert:{sub_id}:")
                    self.store.delete_prefix(f"notice:{sub_id}:")
                    reply = "✅ 已取消当前私聊/话题的订阅。"
                else:
                    subs[sub_id] = {"chat": chat, "thread": thread, "active": command != "/pause"}
                    reply = "⏸ 当前订阅已暂停。" if command == "/pause" else (
                        "✅ 当前私聊/话题已订阅。\n" + self.config_summary() +
                        "\n首次观察就超过阈值，也会提醒；请用 /status 核对基准和行情。")
                self.store.put("subscriptions", subs)
            elif command == "/threshold":
                if len(parts) != 2:
                    raise ValueError("用法：/threshold 1（1 表示 1%）")
                value = number(parts[1].rstrip("%"), "阈值")
                if not D("0.01") <= value <= D(100):
                    raise ValueError("阈值必须在 0.01～100 之间")
                settings = self.settings()
                settings["threshold"] = str(value)
                self.store.put("settings", settings)
                self.store.delete_prefix("alert:")
                reply = f"✅ 全局阈值已改为严格超过 ±{fmt(value)}%。下一轮按新阈值判断。"
            elif command == "/cooldown":
                if len(parts) != 2 or not parts[1].isdigit() or not 0 <= int(parts[1]) <= 86400:
                    raise ValueError("用法：/cooldown 300，范围 0～86400 秒；0 关闭周期重复提醒")
                settings = self.settings()
                settings["cooldown"] = int(parts[1])
                self.store.put("settings", settings)
                reply = f"✅ 周期重复提醒间隔：{parts[1]} 秒（0 表示关闭）。"
            elif command == "/mode":
                if len(parts) != 2 or parts[1].lower() not in {"daily", "binance_daily", "manual"}:
                    raise ValueError("用法：/mode daily 或 /mode manual")
                settings = self.settings()
                settings["mode"] = "manual" if parts[1].lower() == "manual" else "binance_daily"
                self.store.put("settings", settings)
                self.snapshots.clear()
                self.store.delete_prefix("alert:")
                reply = "✅ " + self.config_summary()
                if settings["mode"] == "manual":
                    reply += "\n请逐个设置参考价。缺失/过期的合约不发涨跌提醒。\n/setclose UNITREE 75 " + beijing_day(self.market.now_ms() / 1000)
            elif command == "/setclose":
                if len(parts) not in {3, 4}:
                    raise ValueError("用法：/setclose UNITREE 75 [适用日期 YYYY-MM-DD]\n价格须与币安显示值同口径；不自动换汇")
                symbol = self.resolve_symbol(parts[1])
                value = number(parts[2], "手动参考价")
                day = parts[3] if len(parts) == 4 else beijing_day(self.market.now_ms() / 1000)
                day = dt.date.fromisoformat(day).isoformat()
                self.store.put(f"manual:{symbol}:{day}", {"value": str(value), "valid_date": day})
                self.snapshots.pop(symbol, None)
                reply = (f"✅ {symbol} 参考价：{fmt(value)}\n适用日：{day}（北京时间）"
                         "\n这是你输入的参考价，未独立核验，不自动换汇。")
                if self.settings()["mode"] != "manual":
                    reply += "\n当前仍为日 K 模式；发 /mode manual 后才会使用此价格。"
            elif command in {"/status", "/price"}:
                reply = self.status(sub_id)
            elif command == "/test":
                reply = "✅ TG 测试消息发送成功。\n此测试仅验证推送，行情是否正常请看 /status。"
            else:
                reply = "未知命令。发送 /help 查看用法。"
        except (ValueError, decimal.InvalidOperation) as error:
            reply = "❌ " + clean_error(error)
        await self.tell(chat, thread, reply)

    def config_summary(self) -> str:
        settings = self.settings()
        mode = "币安上一 UTC 日日 K 收盘（非股票正式昨收）" if settings["mode"] == "binance_daily" else "手动同口径参考价（每日核对）"
        return (f"基准：{mode}\n触发：严格超过 ±{fmt(settings['threshold'])}%｜每 {self.config.poll} 秒检查"
                f"\n周期提醒：{settings['cooldown']} 秒（0=关闭）")

    def status(self, sub_id: str) -> str:
        now_ms = self.market.now_ms()
        sub = self.subscriptions().get(sub_id)
        active = "已订阅" if sub and sub.get("active") else "未订阅/已暂停"
        lines = [f"📡 监控状态 v{VERSION}｜{active}", self.config_summary()]
        for symbol in self.config.symbols:
            snapshot = self.snapshots.get(symbol)
            lines.append(f"\n{NAMES.get(symbol, symbol)}｜{symbol}")
            if not snapshot:
                lines.append("等待首次采样或基准切换后的刷新")
                continue
            if "error" in snapshot:
                lines.append("⚠️ " + snapshot["error"])
                continue
            quote, base = snapshot["quote"], snapshot["baseline"]
            age = (now_ms - quote.timestamp_ms) / 1000
            if age > self.config.max_age or now_ms >= base.valid_until_ms:
                lines.append("⚠️ 缓存已过期，等待有效的新行情/基准；不应据此判断当前涨跌")
                continue
            change = percent(quote.price, base.value)
            lines.extend([f"最新成交：{fmt(quote.price)}｜基准：{fmt(base.value)}",
                          f"相对基准：{change:+.3f}%", base.label,
                          f"行情时间：{stamp(quote.timestamp_ms)}（北京）｜{max(0, int(age))} 秒前"])
        lines.append("\n仅价格提醒；不会自动撤单/交易。日 K 于北京时间 08:00 换日。")
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

        async def collect(symbol: str) -> tuple[str, dict]:
            try:
                if symbol not in rows:
                    raise ValueError("接口没有此合约；核对 SYMBOLS、上市状态和接口可用性。不会替换为其他合约")
                quote = Quote.parse(rows[symbol], symbol, now_ms, self.config.max_age)
                if settings["mode"] == "binance_daily":
                    base = await self.market.baseline(symbol, now_ms)
                else:
                    day = beijing_day(now_ms / 1000)
                    base = manual_baseline(self.store.get(f"manual:{symbol}:{day}"), now_ms)
                return symbol, {"quote": quote, "baseline": base}
            except Exception as error:
                return symbol, {"error": clean_error(error)}

        collected = dict(await asyncio.gather(*(collect(s) for s in self.config.symbols)))
        # A Telegram command may change global settings while a request is in flight.
        # Discard the old batch rather than sending alerts using a now-obsolete mode.
        if self.settings() != settings:
            return
        self.snapshots = collected
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
                if settings["mode"] == "manual":
                    day = beijing_day(current_ms / 1000)
                    fresh_base = manual_baseline(self.store.get(f"manual:{symbol}:{day}"), current_ms)
                    if fresh_base.key != base.key:
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
                side = "📈 上涨" if change > 0 else "📉 下跌"
                text = (f"{side}超过 {fmt(threshold)}%｜{NAMES.get(symbol, symbol)}\n{symbol}\n\n"
                        f"当前成交价：{fmt(quote.price)}\n参考收盘价：{fmt(base.value)}\n"
                        f"相对基准：{change:+.3f}%\n\n{base.label}\n原因：{plan.reason}\n"
                        f"行情时间：{stamp(quote.timestamp_ms)}（北京时间）\n"
                        "⚠️ 合约行情提示，不代表股票官方收盘结算结果。")
                if await self.tell(sub["chat"], sub["thread"], text):
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
            delay = max(0.1, self.config.poll - (time.monotonic() - started))
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def commands_loop(self) -> None:
        offset = int(self.store.get("telegram_offset", 0))
        failures = 0
        while not self.stopping.is_set():
            try:
                updates = await self.telegram.call("getUpdates", {"offset": offset, "timeout": 25,
                                                   "allowed_updates": ["message"]}, timeout=40)
                if not isinstance(updates, list):
                    raise ValueError("Telegram 更新格式异常")
                failures = 0
                for update in updates:
                    if self.stopping.is_set():
                        break
                    message = update.get("message")
                    if isinstance(message, dict):
                        # Do not execute old configuration commands left over from long downtime.
                        age = time.time() - float(message.get("date", time.time()))
                        if -60 <= age <= 900:
                            try:
                                await self.process_message(message)
                            except Exception as error:
                                self.log_limited("command", "命令处理异常：" + clean_error(error))
                    offset = int(update["update_id"]) + 1
                    self.store.put("telegram_offset", offset)
            except Exception as error:
                failures += 1
                self.log_limited("poll", "TG 轮询异常：" + clean_error(error))
                retry_after = getattr(error, "retry_after", 0)
                delay = max(retry_after, min(60, 2 ** min(failures, 6)))
                try:
                    await asyncio.wait_for(self.stopping.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

    async def run(self) -> None:
        # Refuse to silently remove a webhook that may belong to another service.
        webhook = await self.telegram.call("getWebhookInfo")
        if webhook.get("url"):
            raise ValueError("该 Bot Token 已绑定 Webhook。请为本项目使用独立机器人，或在原服务移除 Webhook 后重试")
        me = await self.telegram.call("getMe")
        self.username = me.get("username", "")
        LOG.info("Starting @%s; symbols=%s; administrator_configured=%s", self.username,
                 ",".join(self.config.symbols), bool(self.config.admin_id))
        if not self.config.admin_id:
            LOG.warning("ADMIN_USER_ID 尚未配置：只能使用 /id；没有任何自动订阅")
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.stopping.set)
        tasks = [asyncio.create_task(self.monitor_loop()), asyncio.create_task(self.commands_loop())]
        try:
            await self.stopping.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
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
            print(f"OK {symbol}: last={quote.price}, daily_close={base.value}, "
                  f"change={percent(quote.price, base.value):+.4f}%, quote_time={stamp(quote.timestamp_ms)}, {base.label}")
        except Exception as error:
            failed = True
            print(f"FAIL {symbol}: {clean_error(error)}")
    print("口径：币安上一 UTC 日日 K，不是股票交易所正式昨收。")
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
