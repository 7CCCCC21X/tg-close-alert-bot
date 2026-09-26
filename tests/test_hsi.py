import asyncio, sys, time, json, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
def ms(h, mi, d=18): return int(dt.datetime(2026, 9, d, h, mi, tzinfo=m.BEIJING).timestamp() * 1000)
# session labels (HK time == Beijing time)
assert m.hk_futures_session(ms(22, 1)) == "夜市" and m.hk_futures_session(ms(2, 30)) == "夜市" and m.hk_futures_session(ms(17, 15)) == "夜市"
assert m.hk_futures_session(ms(10, 0)) == "日市" and m.hk_futures_session(ms(16, 30)) == "日市" and m.hk_futures_session(ms(16, 45)) == "休市" and m.hk_futures_session(ms(5, 0)) == "休市"
# Eastmoney futures + spot
now = ms(22, 1)
em = json.dumps({"rc": 0, "data": {"f43": 24706.0, "f44": 24755.0, "f45": 24610.0, "f46": 24739.0, "f57": "HSI_M", "f58": "恒指主连", "f60": 24736.0, "f86": now // 1000}}).encode()
q = m.IndexFutures.parse_futures("东方财富", em, 0)
assert q.name == "恒指主连" and q.last == D("24706") and q.prev_settle == D("24736") and q.change == D("-30") and q.quoted_ms == now and q.high == D("24755"), q
assert m.IndexFutures.parse_spot("东方财富", json.dumps({"data": {"f43": 24750.78, "f60": 24604.29}}).encode()) == (D("24750.78"), D("24604.29"))
for bad in [b'{"data":null}', b'{"data":{"f43":"-"}}', b"garbage"]:
    try: m.IndexFutures.parse_futures("东方财富", bad, 0); assert False, bad
    except ValueError: pass
# Sina hf_HSI: last,?,bid,ask,high,low,time,prev settle,open,OI,?,?,?,name,date
sina = 'var hq_str_hf_HSI="24706.000,,24705.000,24707.000,24755.000,24610.000,22:01:05,24736.000,24739.000,120000,0,0,0,恒生指数期货,2026-09-18";'.encode("gbk")
q2 = m.IndexFutures.parse_futures("新浪CFD", sina, 0)
assert q2.last == D("24706") and q2.prev_settle == D("24736") and q2.open == D("24739") and q2.low == D("24610") and q2.name == "恒生指数期货" and q2.quoted_ms == ms(22, 1) + 5000, q2
assert m.IndexFutures.parse_spot("腾讯", ('v_hkHSI="100~恒生指数~HSI~24750.78~24604.29~' + "~".join(["0"] * 30) + '";').encode("gbk")) == (D("24750.78"), D("24604.29"))
assert m.IndexFutures.parse_spot("新浪", 'var hq_str_rt_hkHSI="HSI,恒生指数,24604.29,24604.29,24800,24500,24750.78,0";'.encode("gbk")) == (D("24750.78"), D("24604.29"))
try: m.IndexFutures.parse_spot("腾讯", b'v_hkHSI="";'); assert False
except ValueError: pass
# line formatting
fq = m.FuturesQuote("恒指主连", D("24706"), D("24736"), D("24739"), D("24755"), D("24610"), now, "东方财富", D("24750.78"), "", D("24604.29"))
h = m.IndexFutures(); h.quote = fq
line = h.line(now, "cn")
assert line == "📈 " + m.bold("恒指期货 夜市") + " " + m.bold("24,706") + " → 恒指收盘 " + m.bold("24,750.78") + " 🟢 -0.18%（低水 45）｜恒指当日 🔴 +0.60%｜期货前收 " + m.bold("24,736") + " 🟢 -0.12%（-30）｜09-18 22:01 东方财富", line
assert h.line(ms(22, 1, 19), "cn").endswith("｜09-18 22:01 东方财富｜⚠️ 非今日数据")
h.quote = m.FuturesQuote("恒指主连", D("24800"), D("24736"), None, None, None, ms(10, 0), "新浪", D("24750.78"))
day = h.line(ms(10, 0), "cn"); assert "恒指期货 日市" in day and "→ 恒指 " + m.bold("24,750.78") + " 🔴 +0.20%（高水 49）" in day and "期货前收 " + m.bold("24,736") + " 🔴 +0.26%（+64）" in day and "恒指当日" not in day, day
h.quote = m.FuturesQuote("x", D("1"), None, None, None, None, now, "s"); assert "水" not in h.line(now, "cn")
h.quote = None; h.error = "东方财富: HTTP 403"; assert h.line(now, "cn") == "📈 恒指期货 ⚠️ 获取失败（东方财富: HTTP 403）"
h.error = ""; assert "等待首次获取" in h.line(now, "cn"); assert m.IndexFutures(False).line(now, "cn") == ""

async def run():
    calls = []
    async def fake_get(url, timeout=15, headers=None):
        calls.append(url)
        if "134.HSI_M" in url: raise m.RemoteError("网络错误 (RemoteDisconnected)")
        if "hf_HSI" in url: return sina
        if "100.HSI" in url: raise m.RemoteError("HTTP 403: 访问被拒绝")
        if "hkHSI" in url and "gtimg" in url: return ('v_hkHSI="100~恒生指数~HSI~24750.78~24604.29~' + "~".join(["0"] * 30) + '";').encode("gbk")
        raise AssertionError(url)
    m.http_get = fake_get
    h = m.IndexFutures(); await h.refresh(now)
    assert h.quote and h.quote.source == "新浪CFD" and h.quote.spot == D("24750.78") and h.quote.spot_prev == D("24604.29") and h.quote.basis == D("-44.78") and not h.error, h.quote
    n = len(calls); await h.refresh(now); assert len(calls) == n  # 60 s cache
    async def all_fail(url, timeout=15, headers=None): raise m.RemoteError("网络错误 (TimeoutError)")
    m.http_get = all_fail; await h.refresh(now, force=True)
    assert h.quote.last == D("24706") and "东方财富" in h.error and "新浪" in h.error and "刷新失败" in h.line(now, "cn")
    m.http_get = fake_get
    # Bot: status shows the HSI line; alerts for HK underlyings include it, others do not
    class FakeTelegram:
        def __init__(self): self.sent = []
        async def call(self, *a, **k): return True
        async def send(self, chat, thread, text, reply_markup=None, parse_mode=None): self.sent.append(text)
    class FakeMarket:
        def __init__(self, c): self.config = c
        def now_ms(self): return int(time.time() * 1000)
        async def sync_clock(self): pass
        async def prices(self): return {s: {"symbol": s, "price": "100", "time": self.now_ms()} for s in self.config.symbols}
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ADMIN_USER_ID": "42", "SYMBOLS": "HK0625USDT,UNITREEUSDT", "BASELINE_MODE": "manual",
                             "MIN_ALERT_GAP_SECONDS": "0", "EXCHANGE_TICKERS": "HK0625USDT=hk:00625:same,UNITREEUSDT=sh:688836"})
    assert cfg.hsi_futures and not m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "HSI_FUTURES": "off"}).hsi_futures
    store = m.Store(":memory:"); tg = FakeTelegram(); bot = m.Bot(cfg, store, FakeMarket(cfg), tg)
    bot.stocks.enabled = False; bot.config.tickers  # stock fetch would call fake_get with unknown urls -> AssertionError caught per symbol
    today = m.beijing_day()
    for s in cfg.symbols: store.put(f"manual:{s}:{today}", {"value": "98", "valid_date": today})
    store.put("subscriptions", {"1:0": {"chat": 1, "thread": 0, "active": True}})
    await bot.one_cycle()
    hk = [t for t in tg.sent if "HK0625USDT" in t and "上涨超过" in t][0]; un = [t for t in tg.sent if "UNITREEUSDT" in t and "上涨超过" in t][0]
    assert "📈 <b>恒指期货 夜市</b> <b>24,706</b> → 恒指" in hk and "低水 45" in hk and hk.index("恒指期货") < hk.index("📝"), hk
    assert "恒指期货" not in un
    tg.sent.clear(); await bot.process_message({"text": "/status", "chat": {"id": 1}, "from": {"id": 42}, "date": time.time()})
    st = tg.sent[-1]; assert "📈 <b>恒指期货 夜市</b>" in st and st.index("恒指期货") < st.index("📍"), st
    print("HSI_OK")
asyncio.run(run())
