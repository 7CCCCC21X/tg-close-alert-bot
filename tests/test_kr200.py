import asyncio, sys, time, json, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
tz9 = dt.timezone(dt.timedelta(hours=9))
def ms(h, mi, d=23): return int(dt.datetime(2026, 9, d, h, mi, tzinfo=tz9).timestamp() * 1000)
cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x"})
assert cfg.hl_index == {"KR200": ("xyz", "KR200")} and m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "HL_INDEX": "off"}).hl_index == {}
def naver(code, close, cmp, dcode, ratio):
    return json.dumps({"datas": [{"itemCode": code, "stockName": code, "closePrice": close, "compareToPreviousClosePrice": cmp,
        "compareToPreviousPrice": {"code": dcode}, "fluctuationsRatio": ratio, "localTradedAt": "2026-09-23T16:15:00+09:00", "marketStatus": "CLOSE"}]}).encode()
async def run():
    async def fake_get(url, timeout=15, headers=None):
        if url.endswith("/index/KOSPI"): return naver("KOSPI", "7,080.92", "63.01", "2", "0.90")
        if url.endswith("/index/KPI200"): return naver("KPI200", "1,105.30", "9.80", "2", "0.89")
        raise m.RemoteError("HTTP 404: 接口请求失败")
    m.http_get = fake_get
    k = m.KospiIndex(); await k.refresh(ms(17, 0))
    assert k.quote.last == D("7080.92") and k.quote200.last == D("1105.30") and not k.error and not k.error200
    hl = m.HlQuote("xyz:KR200", D("1121.7"), D("1122.6"), None, D("1104.6"), D("-0.000175"), 0)
    line = k.line200(ms(17, 0), "cn", hl)
    assert line == ("🇰🇷 " + m.bold("KOSPI200 已收盘") + " " + m.bold("1,105.3") + " → 昨收 " + m.bold("1,095.5") + " 🔴 +0.89%"
                    "｜🌊 HL KR200 " + m.bold("1,121.7") + " → 相对 KOSPI200 🔴 +1.48%（24h 🔴 +1.55%）｜09-23 16:15 韩国时间 Naver"), line
    assert "🌊 HL 未找到市场 xyz:KR200" in k.line200(ms(17, 0), "cn", None, "未找到市场 xyz:KR200（该 dex 共 3 个市场），可用 HL_TICKERS 指定")
    assert k.line200(ms(17, 0), "cn", None).endswith("韩国时间 Naver") and "HL" not in k.line200(ms(17, 0), "cn", None)
    # KPI200 failing keeps the composite working and reports separately
    async def half(url, timeout=15, headers=None):
        if url.endswith("/index/KPI200"): raise m.RemoteError("HTTP 503: 接口请求失败")
        return await fake_get(url)
    m.http_get = half; await k.refresh(ms(17, 0), force=True)
    assert k.quote200.last == D("1105.30") and "HTTP 503" in k.error200 and not k.error and "刷新失败" in k.line200(ms(17, 0), "cn", hl)
    k2 = m.KospiIndex(); await k2.refresh(ms(17, 0), force=True); assert k2.quote200 is None and "获取失败" in k2.line200(ms(17, 0), "cn", None)
    # Bot: HL fetcher covers KR200 via the same xyz request; status shows the line under KOSPI; KR alerts carry it
    async def fake_json(url, payload=None, timeout=15):
        assert payload.get("dex") == "xyz"
        return [{"universe": [{"name": "xyz:KR200"}, {"name": "xyz:SKHX"}]}, [{"markPx": "1121.7", "prevDayPx": "1104.6"}, {"markPx": "1353", "prevDayPx": "1340"}]]
    m.http_json = fake_json; m.http_get = fake_get
    class FakeTelegram:
        def __init__(self): self.sent = []
        async def call(self, *a, **k): return True
        async def send(self, chat, thread, text, reply_markup=None, parse_mode=None): self.sent.append(text)
    class FakeMarket:
        def __init__(self, c): self.config = c
        def now_ms(self): return int(time.time() * 1000)
        async def sync_clock(self): pass
        async def prices(self): return {s: {"symbol": s, "price": "100", "time": self.now_ms()} for s in self.config.symbols}
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ADMIN_USER_ID": "42", "SYMBOLS": "SKHYNIXUSDT,HK0625USDT", "BASELINE_MODE": "manual",
                             "MIN_ALERT_GAP_SECONDS": "0", "HSI_FUTURES": "off", "FX_RATES": "KRW=1390,HKD=7.8"})
    store = m.Store(":memory:"); tg = FakeTelegram(); bot = m.Bot(cfg, store, FakeMarket(cfg), tg)
    assert set(bot.hl.tickers) == {"SKHYNIXUSDT", "HK0625USDT", "KR200"}
    today = m.beijing_day()
    for s in cfg.symbols: store.put(f"manual:{s}:{today}", {"value": "98", "valid_date": today})
    store.put("subscriptions", {"1:0": {"chat": 1, "thread": 0, "active": True}})
    await bot.one_cycle()
    kr = [t for t in tg.sent if "SKHYNIXUSDT" in t and "上涨超过" in t][0]; hk = [t for t in tg.sent if "HK0625USDT" in t and "上涨超过" in t][0]
    assert "🇰🇷 <b>KOSPI200 已收盘</b> <b>1,105.3</b>" in kr and "🌊 HL KR200 <b>1,121.7</b> → 相对 KOSPI200 🔴 +1.48%" in kr and "KOSPI200" not in hk, kr
    tg.sent.clear(); await bot.process_message({"text": "/status", "chat": {"id": 1}, "from": {"id": 42}, "date": time.time()})
    st = tg.sent[-1]; assert st.index("<b>KOSPI 已收盘</b>") < st.index("<b>KOSPI200 已收盘</b>") < st.index("📍"), st
    print("KR200_OK")
asyncio.run(run())
