import asyncio, sys, time, json, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
tz9 = dt.timezone(dt.timedelta(hours=9))
def ms(h, mi, d=23): return int(dt.datetime(2026, 9, d, h, mi, tzinfo=tz9).timestamp() * 1000)
assert m.krx_session(ms(10, 0)) == "交易中" and m.krx_session(ms(15, 30)) == "交易中" and m.krx_session(ms(15, 31)) == "已收盘" and m.krx_session(ms(8, 59)) == "已收盘"
# Naver payloads: rising and falling (direction code 5 = falling, compareToPreviousClosePrice unsigned)
def naver(close, cmp, code, ratio, status="CLOSE"):
    return json.dumps({"pollingInterval": 5000, "datas": [{"itemCode": "KOSPI", "stockName": "코스피", "closePrice": close, "compareToPreviousClosePrice": cmp,
        "compareToPreviousPrice": {"code": code, "text": "x", "name": "x"}, "fluctuationsRatio": ratio, "openPrice": "3,360.10", "highPrice": "3,380.50",
        "lowPrice": "3,355.20", "localTradedAt": "2026-09-23T15:30:12+09:00", "marketStatus": status}], "resultCode": "success"}).encode()
up = m.parse_naver_index(naver("3,371.89", "12.34", "2", "0.37"), 0)
assert up.last == D("3371.89") and up.change == D("12.34") and up.prev_close == D("3359.55") and up.open == D("3360.10") and up.status == "已收盘" and up.quoted_ms == ms(15, 30) + 12000, up
down = m.parse_naver_index(naver("3,300.00", "12.34", "5", "-0.37", "OPEN"), 0)
assert down.change == D("-12.34") and down.prev_close == D("3312.34") and down.status == "交易中", down
down2 = m.parse_naver_index(naver("3,300.00", "-12.34", "2", "-0.37"), 0); assert down2.change == D("-12.34")
for bad in [b'{"datas":[]}', b"garbage", b'{"datas":[{"closePrice":"abc"}]}']:
    try: m.parse_naver_index(bad, 0); assert False, bad
    except ValueError: pass
em = json.dumps({"data": {"f43": 3371.89, "f44": 3380.5, "f45": 3355.2, "f46": 3360.1, "f58": "韩国KOSPI", "f60": 3359.55, "f86": ms(15, 30) // 1000}}).encode()
e = m.parse_eastmoney_index(em, 0, "韩国KOSPI"); assert e.last == D("3371.89") and e.prev_close == D("3359.55") and e.source == "东方财富" and e.status == ""
# line
k = m.KospiIndex(); k.quote = up
line = k.line(ms(16, 0), "cn")
assert line == "🇰🇷 " + m.bold("KOSPI 已收盘") + " " + m.bold("3,371.89") + " → 昨收 " + m.bold("3,359.55") + " 🔴 +0.37%（+12.34）｜09-23 15:30 韩国时间 Naver", line
assert k.line(ms(9, 0, 24), "cn").endswith("｜09-23 15:30 韩国时间 Naver｜⚠️ 非今日数据")
k.quote = e; assert "KOSPI 已收盘" in k.line(ms(16, 0), "cn") and "韩国时间 东方财富" in k.line(ms(16, 0), "cn")
k.quote = None; k.error = "Naver: HTTP 403"; assert k.line(0, "cn") == "🇰🇷 KOSPI ⚠️ 获取失败（Naver: HTTP 403）"
assert m.KospiIndex(False).line(0, "cn") == ""

async def run():
    calls = []
    async def fake_get(url, timeout=15, headers=None):
        calls.append(url)
        if "naver" in url: raise m.RemoteError("网络错误 (RemoteDisconnected)")
        if "100.KS11" in url: return em
        raise AssertionError(url)
    m.http_get = fake_get
    k = m.KospiIndex(); await k.refresh(ms(16, 0))
    assert k.quote and k.quote.source == "东方财富" and not k.error and [u for u in calls if "naver" in u]
    n = len(calls); await k.refresh(ms(16, 0)); assert len(calls) == n
    async def dead(url, timeout=15, headers=None): raise m.RemoteError("网络错误 (TimeoutError)")
    m.http_get = dead; await k.refresh(ms(16, 0), force=True)
    assert k.quote.last == D("3371.89") and "Naver" in k.error and "东方财富" in k.error and "刷新失败" in k.line(ms(16, 0), "cn")
    # Bot: status line under HSI; Korea alerts carry it, HK alerts do not
    async def get2(url, timeout=15, headers=None):
        if "100.KS11" in url: return em
        raise m.RemoteError("skip")
    m.http_get = get2
    async def no_json(url, payload=None, timeout=15): raise m.RemoteError("skip")
    m.http_json = no_json
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
                             "MIN_ALERT_GAP_SECONDS": "0", "HSI_FUTURES": "off", "HL_TICKERS": "off"})
    assert cfg.kospi_index and not m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "KOSPI_INDEX": "off"}).kospi_index
    store = m.Store(":memory:"); tg = FakeTelegram(); bot = m.Bot(cfg, store, FakeMarket(cfg), tg)
    today = m.beijing_day()
    for s in cfg.symbols: store.put(f"manual:{s}:{today}", {"value": "98", "valid_date": today})
    store.put("subscriptions", {"1:0": {"chat": 1, "thread": 0, "active": True}})
    await bot.one_cycle()
    kr = [t for t in tg.sent if "SKHYNIXUSDT" in t and "上涨超过" in t][0]; hk = [t for t in tg.sent if "HK0625USDT" in t and "上涨超过" in t][0]
    assert "🇰🇷 <b>KOSPI" in kr and "KOSPI" not in hk, (kr, hk)
    tg.sent.clear(); await bot.process_message({"text": "/status", "chat": {"id": 1}, "from": {"id": 42}, "date": time.time()})
    st = tg.sent[-1]; assert "🇰🇷 <b>KOSPI" in st and st.index("KOSPI") < st.index("📍"), st
    print("KOSPI_OK")
asyncio.run(run())
