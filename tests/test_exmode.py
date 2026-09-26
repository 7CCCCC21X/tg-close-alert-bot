import asyncio, sys, time, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
tz8, tz9 = dt.timezone(dt.timedelta(hours=8)), dt.timezone(dt.timedelta(hours=9))
def ms(y, mo, d, h, mi, tz=tz8): return int(dt.datetime(y, mo, d, h, mi, tzinfo=tz).timestamp() * 1000)
# config accepts the new mode
assert m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "BASELINE_MODE": "exchange_close"}).baseline_mode == "exchange_close"
try: m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "BASELINE_MODE": "foo"}); assert False
except ValueError as e: assert "exchange_close" in str(e)

async def run():
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ADMIN_USER_ID": "42", "SYMBOLS": "UNITREEUSDT,SKHYNIXUSDT,BTCUSDT",
                             "BASELINE_MODE": "exchange_close", "MIN_ALERT_GAP_SECONDS": "0", "HSI_FUTURES": "off", "KOSPI_INDEX": "off", "HL_TICKERS": "off"})
    close_ms = ms(2026, 9, 23, 15, 0)
    # --- Binance.price_at: trade candle, zero-volume -> mark candle, nothing -> error; cached
    b = m.Binance(cfg); calls = []
    async def fake_get(path, **p):
        calls.append((path, p.get("symbol")))
        assert p["interval"] == "1m" and p["startTime"] == close_ms - 60_000 and p["limit"] == 1
        if p["symbol"] == "UNITREEUSDT" and path == "/fapi/v1/klines": return [[close_ms - 60_000, "73.30", "73.40", "73.20", "73.36", "12.5", close_ms - 1]]
        if p["symbol"] == "SKHYNIXUSDT" and path == "/fapi/v1/klines": return [[close_ms - 60_000, "1", "1", "1", "1", "0", close_ms - 1]]  # no trades
        if p["symbol"] == "SKHYNIXUSDT": return [[close_ms - 60_000, "1330", "1331", "1329", "1330.5", "0", close_ms - 1]]
        return []
    b.get = fake_get
    assert await b.price_at("UNITREEUSDT", close_ms) == (D("73.36"), "成交价")
    assert await b.price_at("SKHYNIXUSDT", close_ms) == (D("1330.5"), "标记价")
    n = len(calls); await b.price_at("UNITREEUSDT", close_ms); assert len(calls) == n
    try: await b.price_at("BTCUSDT", close_ms); assert False
    except ValueError as e: assert "K 线" in str(e)
    await b.price_at("UNITREEUSDT", close_ms + 60_000 * 5) if False else None
    # --- exchange_close_ms: only a close confirmed by a dated exchange bar, never a calendar guess
    class FakeTelegram:
        def __init__(self): self.sent = []
        async def call(self, *a, **k): return True
        async def send(self, chat, thread, text, reply_markup=None, parse_mode=None): self.sent.append(text)
    class FakeMarket(m.Binance):
        def __init__(self, c): super().__init__(c); self.price = "73.13"
        def now_ms(self): return int(time.time() * 1000)
        async def sync_clock(self): pass
        async def prices(self): return {s: {"symbol": s, "price": self.price, "time": self.now_ms()} for s in self.config.symbols}
    store = m.Store(":memory:"); tg = FakeTelegram(); bot = m.Bot(cfg, store, FakeMarket(cfg), tg)
    sh = m.STOCK_MARKETS["sh"]
    bot.stocks.closes["UNITREEUSDT"] = m.StockMarket.baseline(m.StockTicker("sh", "688836"), sh, "腾讯", dt.date(2026, 9, 23), D("490.97"), D("489.2"))
    got, info, src = bot.exchange_close_ms("UNITREEUSDT", ms(2026, 9, 23, 15, 40))
    assert got == close_ms and info is sh and src == "上交所688836", (got, src)
    # bar is today's but only 10 minutes old -> not final yet -> no confirmed close (no calendar guess)
    assert bot.exchange_close_ms("UNITREEUSDT", ms(2026, 9, 23, 15, 10))[0] == 0
    bot.stocks.closes.clear()
    assert bot.exchange_close_ms("UNITREEUSDT", ms(2026, 9, 26, 10, 0))[0] == 0  # 9/25 is a holiday: never inferred
    try: bot.exchange_close_ms("BTCUSDT", close_ms); assert False
    except ValueError as e: assert "EXCHANGE_TICKERS" in str(e)
    # --- baseline: nothing confirmed and nothing held -> paused with a clear reason
    bot.market.get = fake_get
    try: await bot.exchange_time_baseline("UNITREEUSDT", ms(2026, 9, 23, 15, 40)); assert False
    except m.PendingData as e: assert "等待首次获取上交所688836收盘价" in str(e), e  # first fetch not finished yet
    bot.stocks.errors["UNITREEUSDT"] = "东方财富: 网络错误"
    try: await bot.exchange_time_baseline("UNITREEUSDT", ms(2026, 9, 23, 15, 40)); assert False
    except ValueError as e: assert not isinstance(e, m.PendingData) and "尚未由带日期的日 K 确认" in str(e) and "不按日历推算" in str(e), e
    bot.stocks.errors.clear()
    # --- confirmed 09-23 close -> baseline, persisted
    bot.stocks.closes["UNITREEUSDT"] = m.StockMarket.baseline(m.StockTicker("sh", "688836"), sh, "腾讯", dt.date(2026, 9, 23), D("490.97"), D("489.2"))
    base = await bot.exchange_time_baseline("UNITREEUSDT", ms(2026, 9, 23, 15, 40))
    assert base.value == D("73.36") and base.label == "币安合约成交价@上交所收盘时刻｜收盘 09-23 15:00（北京时间）｜上交所688836" and base.close_ms == close_ms, base.label
    assert store.get("exchange_base:UNITREEUSDT")["close_ms"] == close_ms
    # --- long holiday: 09-24 is the last session before 09-25 (holiday) + weekend; held until 09-28's close is confirmed
    c24 = ms(2026, 9, 24, 15, 0)
    async def get24(path, **p):
        assert p["startTime"] == c24 - 60_000
        return [[c24 - 60_000, "74", "74", "74", "74.10", "3", c24 - 1]] if path == "/fapi/v1/klines" else []
    bot.market.get = get24
    bot.stocks.closes["UNITREEUSDT"] = m.StockMarket.baseline(m.StockTicker("sh", "688836"), sh, "腾讯", dt.date(2026, 9, 24), D("495"), D("490.97"))
    b24 = await bot.exchange_time_baseline("UNITREEUSDT", ms(2026, 9, 24, 15, 20))
    assert b24.value == D("74.10") and b24.close_ms == c24
    async def boom(path, **p): raise AssertionError("no Binance request while the held baseline is current")
    bot.market.get = boom
    for when in (ms(2026, 9, 25, 12, 0), ms(2026, 9, 26, 10, 0), ms(2026, 9, 27, 23, 0), ms(2026, 9, 28, 9, 0), ms(2026, 9, 28, 15, 10)):
        held = await bot.exchange_time_baseline("UNITREEUSDT", when)
        assert held.key == b24.key and held.value == D("74.10") and when < held.valid_until_ms, (when, held)
        assert "待确认" not in held.label, held.label  # holiday + weekend: 09-24 is still the latest expected close
    # 09-28 15:20: the 09-28 close is expected but the feed still shows 09-24 -> keep 09-24, say so
    held = await bot.exchange_time_baseline("UNITREEUSDT", ms(2026, 9, 28, 15, 20))
    assert held.key == b24.key and "09-28 收盘待确认，沿用此基准" in held.label, held.label
    assert m.baseline_brief(held) == "09-24 15:00 交易所收盘时刻·成交价·⏳ 09-28 收盘待确认", m.baseline_brief(held)
    assert m.baseline_brief(b24) == "09-24 15:00 交易所收盘时刻·成交价"
    # a feed outage (undated fallback) keeps the held baseline too
    bot.stocks.closes["UNITREEUSDT"] = m.StockMarket.baseline(m.StockTicker("sh", "688836"), sh, "腾讯", None, D("495"))
    assert (await bot.exchange_time_baseline("UNITREEUSDT", ms(2026, 9, 28, 16, 0))).key == b24.key
    # restart: restored from the store without asking Binance again
    bot_r = m.Bot(cfg, store, FakeMarket(cfg), tg); bot_r.market.get = boom
    assert (await bot_r.exchange_time_baseline("UNITREEUSDT", ms(2026, 9, 27, 10, 0))).key == b24.key
    # a different ticker in the config ignores the stored record
    cfg_other = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "EXCHANGE_TICKERS": "UNITREEUSDT=sh:600000"})
    assert m.Bot(cfg_other, store, FakeMarket(cfg_other), tg).held_exchange_base("UNITREEUSDT", cfg_other.tickers["UNITREEUSDT"]) is None
    # the 09-28 close is confirmed -> replaced
    c28 = ms(2026, 9, 28, 15, 0)
    async def get28(path, **p):
        return [[c28 - 60_000, "75", "75", "75", "75.50", "3", c28 - 1]] if path == "/fapi/v1/klines" else []
    bot.market.get = get28
    bot.stocks.closes["UNITREEUSDT"] = m.StockMarket.baseline(m.StockTicker("sh", "688836"), sh, "东方财富", dt.date(2026, 9, 28), D("500"), D("495"))
    b28 = await bot.exchange_time_baseline("UNITREEUSDT", ms(2026, 9, 28, 15, 30))
    assert b28.close_ms == c28 and b28.value == D("75.50") and "待确认" not in b28.label
    # a confirmed newer close whose Binance price is unavailable pauses rather than using the superseded baseline
    bot.stocks.closes["UNITREEUSDT"] = m.StockMarket.baseline(m.StockTicker("sh", "688836"), sh, "东方财富", dt.date(2026, 9, 29), D("501"), D("500"))
    async def nothing(path, **p): return []
    bot.market.get = nothing
    try: await bot.exchange_time_baseline("UNITREEUSDT", ms(2026, 9, 29, 15, 30)); assert False
    except ValueError as e: assert "K 线" in str(e)
    # --- live cycle with a confirmed close
    async def no_http(*a, **k): raise m.RemoteError("skip")
    m.http_get = no_http; m.http_json = no_http
    bot = m.Bot(cfg, m.Store(":memory:"), FakeMarket(cfg), tg); store = bot.store
    real_now = bot.market.now_ms()
    last_day = m.expected_close_date("sh", real_now, cfg.holidays["sh"])
    bot.stocks.closes["UNITREEUSDT"] = m.StockMarket.baseline(m.StockTicker("sh", "688836"), sh, "腾讯", last_day, D("490"), D("489"))
    exp_close = bot.exchange_close_ms("UNITREEUSDT", real_now)[0]
    async def live_get(path, **p):
        if p.get("symbol") == "UNITREEUSDT" and path == "/fapi/v1/klines": return [[p["startTime"], "73", "73", "73", "72.00", "5", p["startTime"] + 59_999]]
        return []
    bot.market.get = live_get
    store.put("subscriptions", {"1:0": {"chat": 1, "thread": 0, "active": True}})
    await bot.one_cycle()
    alert = [t for t in tg.sent if "UNITREEUSDT" in t and "上涨超过" in t]; assert alert, tg.sent
    assert "基准 72" in alert[0] and "+1.569%" in alert[0] and "交易所收盘时刻·成交价" in alert[0], alert[0]
    assert bot.snapshots["UNITREEUSDT"]["baseline"].close_ms == exp_close
    assert "error" in bot.snapshots["BTCUSDT"] and "EXCHANGE_TICKERS" in bot.snapshots["BTCUSDT"]["error"]
    # /mode command and summary
    async def ask(text):
        tg.sent.clear(); await bot.process_message({"text": text, "chat": {"id": 1}, "from": {"id": 42}, "date": time.time()}); return tg.sent[-1]
    r = await ask("/mode daily"); assert bot.settings()["mode"] == "binance_daily"
    r = await ask("/mode exchange"); assert bot.settings()["mode"] == "exchange_close" and "同一时点" in r and "BTCUSDT" in r and "EXCHANGE_TICKERS" in r, r
    assert "基准 交易所收盘时刻" in (await ask("/status"))
    assert "❌" in await ask("/mode nope") and "/mode exchange" in await ask("/mode nope")
    assert "/mode daily|exchange|manual" in await ask("/help")
    print("EXMODE_OK")
asyncio.run(run())

# default mode follows the ticker configuration
assert m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x"}).baseline_mode == "exchange_close"
assert m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "EXCHANGE_TICKERS": "off"}).baseline_mode == "binance_daily"
assert m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "BASELINE_MODE": "binance_daily"}).baseline_mode == "binance_daily"
print("DEFAULT_OK")
