import asyncio, sys, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
now = 1_800_000_000_000
row = {"symbol": "HK0625USDT", "price": "38.49", "time": now - 185_000, "markPrice": "38.52", "markTime": now - 1_000}
q = m.Quote.parse(row, "HK0625USDT", now, 120)
assert q.source == "mark" and q.price == D("38.52") and q.timestamp_ms == now - 1_000 and q.last_price == D("38.49") and q.kind == "标记价"
line = q.price_row(now)
assert line.startswith("币安 " + m.bold("38.52") + "（标记价·185 秒无成交）｜"), line
fresh = m.Quote.parse({**row, "time": now - 5_000}, "HK0625USDT", now, 120)
assert fresh.source == "last" and fresh.price == D("38.49") and fresh.price_row(now).startswith("币安 " + m.bold("38.49") + "｜")
for bad, msg in [({**row, "markTime": now - 200_000}, "标记价 200 秒"), ({"symbol": "HK0625USDT", "price": "1", "time": now - 185_000}, "已过期（185 秒"),
                 ({**row, "markPrice": "abc"}, "标记价必须是数字"), ({**row, "markTime": None}, "缺少有效时间戳")]:
    try: m.Quote.parse(bad, "HK0625USDT", now, 120); assert False, bad
    except ValueError as e: assert msg in str(e), (msg, str(e))

async def run():
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "HK0625USDT,UNITREEUSDT"})
    b = m.Binance(cfg)
    async def fake_get(path, **params):
        if path == "/fapi/v2/ticker/price":
            return [{"symbol": "HK0625USDT", "price": "38.49", "time": 1}, {"symbol": "UNITREEUSDT", "price": "76", "time": 2}, {"symbol": "BTCUSDT", "price": "1", "time": 3}]
        if path == "/fapi/v1/premiumIndex":
            return [{"symbol": "HK0625USDT", "markPrice": "38.52", "indexPrice": "38.5", "time": 9}, {"symbol": "BTCUSDT", "markPrice": "1", "time": 9}]
        raise AssertionError(path)
    b.get = fake_get
    rows = await b.prices()
    assert set(rows) == {"HK0625USDT", "UNITREEUSDT"} and rows["HK0625USDT"]["markPrice"] == "38.52" and rows["HK0625USDT"]["markTime"] == 9
    assert "markPrice" not in rows["UNITREEUSDT"]
    # mark feed failing must not break last-trade rows
    async def half(path, **params):
        if path == "/fapi/v1/premiumIndex": raise m.RemoteError("HTTP 429: 接口限流")
        return await fake_get(path)
    b.get = half; rows = await b.prices(); assert rows["HK0625USDT"]["price"] == "38.49" and "markPrice" not in rows["HK0625USDT"]
    async def dead(path, **params): raise m.RemoteError("网络错误 (TimeoutError)")
    b.get = dead
    try: await b.prices(); assert False
    except m.RemoteError: pass
    # end to end: stale last trade + fresh mark still alerts, message says 标记价
    class FakeTelegram:
        def __init__(self): self.sent = []
        async def call(self, *a, **k): return True
        async def send(self, chat, thread, text, reply_markup=None, parse_mode=None): self.sent.append(text)
    class FakeMarket:
        def __init__(self, c): self.config = c
        def now_ms(self): return int(time.time() * 1000)
        async def sync_clock(self): pass
        async def prices(self):
            t = self.now_ms()
            return {"HK0625USDT": {"symbol": "HK0625USDT", "price": "38.49", "time": t - 300_000, "markPrice": "40.5", "markTime": t - 500}}
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ADMIN_USER_ID": "42", "SYMBOLS": "HK0625USDT", "BASELINE_MODE": "manual",
                             "MIN_ALERT_GAP_SECONDS": "0", "EXCHANGE_TICKERS": "off"})
    store = m.Store(":memory:"); tg = FakeTelegram(); bot = m.Bot(cfg, store, FakeMarket(cfg), tg)
    today = m.beijing_day(); store.put(f"manual:HK0625USDT:{today}", {"value": "39.6", "valid_date": today})
    store.put("subscriptions", {"1:0": {"chat": 1, "thread": 0, "active": True}})
    await bot.one_cycle()
    alert = [t for t in tg.sent if "上涨超过" in t]; assert alert, tg.sent
    assert "币安 <b>40.5</b>（标记价·299 秒无成交）" in alert[0] and "+2.273%" in alert[0], alert[0]
    tg.sent.clear(); await bot.process_message({"text": "/status", "chat": {"id": 1}, "from": {"id": 42}, "date": time.time()})
    assert "<b>40.5</b>（标记价" in tg.sent[-1] and "已过期" not in tg.sent[-1], tg.sent[-1]
    print("MARK_OK")
asyncio.run(run())
