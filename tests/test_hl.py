import asyncio, sys, time, json
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
# config parsing
cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x"})
assert cfg.hl_tickers == {"UNITREEUSDT": ("xyz", "UNITREE"), "HK0625USDT": ("xyz", "SHEIN"), "CXMTUSDT": ("xyz", "CXMT"), "SKHYNIXUSDT": ("xyz", "SKHX")}, cfg.hl_tickers
c2 = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "SKHYNIXUSDT,BTCUSDT", "HL_TICKERS": "skhynixusdt=XYZ:skhx, BTCUSDT=BTC, FOO=xyz:X"})
assert c2.hl_tickers == {"SKHYNIXUSDT": ("xyz", "SKHX"), "BTCUSDT": ("", "BTC")}, c2.hl_tickers
assert m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "HL_TICKERS": "off"}).hl_tickers == {}
try: m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "HL_TICKERS": "SKHYNIXUSDT=a:b:c"}); assert False
except ValueError as e: assert "HL_TICKERS" in str(e)
# parse metaAndAssetCtxs
data = [{"universe": [{"name": "xyz:SKHX", "szDecimals": 3, "maxLeverage": 10}, {"name": "xyz:CXMT", "szDecimals": 2},
                      {"name": "xyz:OLD", "isDelisted": True}, {"name": "xyz:BAD"}]},
        [{"funding": "0.0000125", "openInterest": "1234.5", "prevDayPx": "1340.0", "dayNtlVlm": "1", "premium": "0.001", "oraclePx": "1352.9", "markPx": "1353.45", "midPx": "1353.5", "impactPxs": ["1353", "1354"]},
         {"funding": "-0.00002", "prevDayPx": "8.5", "oraclePx": "8.1", "markPx": "8.0", "midPx": "8.02"},
         {"markPx": "1"}, {"markPx": "n/a"}]]
q = m.Hyperliquid.parse_dex(data)
assert set(q) == {"SKHX", "CXMT"} and q["SKHX"].coin == "xyz:SKHX" and q["SKHX"].mark == D("1353.45") and q["SKHX"].funding == D("0.0000125")
assert q["SKHX"].day_change.quantize(D("0.001")) == D("1.004") and q["CXMT"].oracle == D("8.1")
for bad in [{}, [], [{"x": 1}, []], "nope"]:
    try: m.Hyperliquid.parse_dex(bad); assert False, bad
    except ValueError: pass

async def run():
    calls = []
    async def fake_json(url, payload=None, timeout=15):
        calls.append(payload); assert url == m.Hyperliquid.URL and payload["type"] == "metaAndAssetCtxs"
        if payload.get("dex") == "xyz": return data
        if "dex" not in payload: return [{"universe": [{"name": "BTC"}]}, [{"markPx": "60000", "prevDayPx": "59000"}]]
        raise m.RemoteError("HTTP 422: 接口请求失败")
    m.http_json = fake_json
    hl = m.Hyperliquid({"SKHYNIXUSDT": ("xyz", "SKHX"), "UNITREEUSDT": ("xyz", "UNITREE"), "BTCUSDT": ("", "BTC"), "ETHUSDT": ("other", "ETH")})
    await hl.refresh()
    assert sorted(c.get("dex", "") for c in calls) == ["", "other", "xyz"]  # one request per dex
    assert hl.quotes["SKHYNIXUSDT"].mark == D("1353.45") and hl.quotes["BTCUSDT"].coin == "BTC"
    assert "未找到市场 xyz:UNITREE" in hl.notes["UNITREEUSDT"] and "HL_TICKERS" in hl.notes["UNITREEUSDT"], hl.notes
    assert hl.notes["ETHUSDT"].startswith("获取失败（HTTP 422")
    n = len(calls); await hl.refresh(); assert len(calls) == n  # 30 s cache
    # lines
    line = hl.line("SKHYNIXUSDT", D("1332.79"), "cn")
    assert line == "HL " + m.bold("1,353.45") + "（24h 🔴 +1.00%·费率 0.00125%/h） → 🟢 -1.53%", line
    assert hl.line("SKHYNIXUSDT", None, "cn").endswith("→ ⚪ 无汇率")
    assert hl.line("UNITREEUSDT", D(1), "cn").startswith("HL 未找到市场 xyz:UNITREE")
    assert hl.line("ETHUSDT", D(1), "cn") == "HL 获取失败（HTTP 422: 接口请求失败）" and hl.line("NOPE", D(1), "cn") == ""
    # failure after success keeps the quote and appends the warning
    async def dead(url, payload=None, timeout=15): raise m.RemoteError("网络错误 (TimeoutError)")
    m.http_json = dead; await hl.refresh(force=True)
    assert hl.quotes["SKHYNIXUSDT"].mark == D("1353.45") and "⚠️ 获取失败（网络错误" in hl.line("SKHYNIXUSDT", D(1), "cn")
    m.http_json = fake_json; await hl.refresh(force=True); assert "SKHYNIXUSDT" not in hl.notes

    # Bot: HKD quanto contract converted to USD before comparing; status + alert carry the line
    class FakeTelegram:
        def __init__(self): self.sent = []
        async def call(self, *a, **k): return True
        async def send(self, chat, thread, text, reply_markup=None, parse_mode=None): self.sent.append(text)
    class FakeMarket:
        def __init__(self, c): self.config = c
        def now_ms(self): return int(time.time() * 1000)
        async def sync_clock(self): pass
        async def prices(self): return {s: {"symbol": s, "price": "39", "time": self.now_ms()} for s in self.config.symbols}
    shein = [{"universe": [{"name": "xyz:SHEIN"}]}, [{"markPx": "4.90", "prevDayPx": "5.0", "oraclePx": "4.85"}]]
    async def fake2(url, payload=None, timeout=15): return shein
    m.http_json = fake2
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ADMIN_USER_ID": "42", "SYMBOLS": "HK0625USDT", "BASELINE_MODE": "manual",
                             "MIN_ALERT_GAP_SECONDS": "0", "EXCHANGE_TICKERS": "HK0625USDT=hk:00625:same", "HSI_FUTURES": "off", "FX_RATES": "HKD=7.8"})
    store = m.Store(":memory:"); tg = FakeTelegram(); bot = m.Bot(cfg, store, FakeMarket(cfg), tg)
    async def no_stock(*a, **k): raise m.RemoteError("skip")
    m.http_get = no_stock
    today = m.beijing_day(); store.put(f"manual:HK0625USDT:{today}", {"value": "38", "valid_date": today})
    store.put("subscriptions", {"1:0": {"chat": 1, "thread": 0, "active": True}})
    await bot.one_cycle()
    alert = [t for t in tg.sent if "上涨超过" in t][0]
    # 39 HKD / 7.8 = 5.0 USD vs HL mark 4.90 -> +2.041%
    assert "HL <b>4.9</b>（24h 🟢 -2.00%） → 🔴 +2.04%（HKD 折美元）" in alert, alert
    assert alert.index("HL <b>") < alert.index("📝")
    tg.sent.clear(); await bot.process_message({"text": "/status", "chat": {"id": 1}, "from": {"id": 42}, "date": time.time()})
    st = tg.sent[-1]; assert "HL <b>4.9</b>" in st and st.index("HL <b>") > st.index("交易所"), st
    print("HL_OK")
asyncio.run(run())
