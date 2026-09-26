import asyncio, sys, time, json, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D

# --- config parsing
cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x"})
assert cfg.tickers == {"UNITREEUSDT": m.StockTicker("sh", "688836"), "HK0625USDT": m.StockTicker("hk", "00625", True),
                       "CXMTUSDT": m.StockTicker("sh", "688825"), "SKHYNIXUSDT": m.StockTicker("kr", "000660")}, cfg.tickers
c2 = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "HK0625USDT,BTCUSDT", "EXCHANGE_TICKERS": "HK0625USDT=HK:00625:same, FOO=sz:000001"})
assert c2.tickers == {"HK0625USDT": m.StockTicker("hk", "00625", True)}, c2.tickers
assert m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "EXCHANGE_TICKERS": "off"}).tickers == {}
for bad in ["UNITREEUSDT=nyse:UNI", "UNITREEUSDT=688836", "=sh:1"]:
    try: m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "EXCHANGE_TICKERS": bad}); assert False, bad
    except ValueError as e: assert "EXCHANGE_TICKERS" in str(e)

# --- URLs (primary source per market)
url = lambda t: m.StockMarket.sources(t)[0][1]
assert url(m.StockTicker("sh", "688836")).endswith("secid=1.688836") and "push2his.eastmoney.com" in url(m.StockTicker("sh", "688836"))
assert url(m.StockTicker("hk", "00625")).endswith("secid=116.00625") and url(m.StockTicker("sz", "000001")).endswith("secid=0.000001")
assert url(m.StockTicker("kr", "000660")).endswith("symbol=000660") and "fchart.stock.naver.com" in url(m.StockTicker("kr", "000660"))

# --- parsers
em = json.dumps({"rc": 0, "data": {"code": "688836", "name": "宇树科技", "klines": [
    "2026-09-16,150.00,152.30,155.00,149.00,1000,2000,3.9", "2026-09-17,152.00,151.10,153.00,150.50,900,1800,1.6",
    "2026-09-18,151.00,149.80,152.00,149.00,500,1000,2.0"]}}).encode()
bars = m.parse_daily_bars("sh", em); assert bars[-1] == (dt.date(2026, 9, 18), D("149.80")) and len(bars) == 3
nv = ('<?xml version="1.0" encoding="EUC-KR" ?><protocol><chartdata symbol="000660" name="SK하이닉스" count="3" timeframe="day" precision="0" origintime="19961226">'
      '<item data="20260916|1700000|1760000|1690000|1730000|3000000" /><item data="20260917|1740000|1760000|1720000|1745000|2500000" />'
      '<item data="20260918|1750000|1830000|1740000|1825000|2000000" /></chartdata></protocol>').encode("euc-kr")
bars_kr = m.parse_daily_bars("kr", nv); assert bars_kr[1] == (dt.date(2026, 9, 17), D("1745000")) and len(bars_kr) == 3
for bad in [b'{"data":null}', b"garbage", b"<protocol></protocol>"]:
    try: m.parse_daily_bars("kr" if bad.startswith(b"<") else "sh", bad); assert False
    except ValueError: pass

# --- completed-session selection (Shanghai closes 15:00 local; final from 15:15)
sh = m.STOCK_MARKETS["sh"]; tz8 = dt.timezone(dt.timedelta(hours=8))
def ms(y, mo, d, h, mi, tz=tz8): return int(dt.datetime(y, mo, d, h, mi, tzinfo=tz).timestamp() * 1000)
assert m.last_completed_bar(bars, sh, ms(2026, 9, 18, 14, 0))[:2] == (dt.date(2026, 9, 17), D("151.10"))   # session running -> yesterday
assert m.last_completed_bar(bars, sh, ms(2026, 9, 18, 15, 10))[:2] == (dt.date(2026, 9, 17), D("151.10"))  # within 15 min grace
assert m.last_completed_bar(bars, sh, ms(2026, 9, 18, 15, 20))[:2] == (dt.date(2026, 9, 18), D("149.80"))  # final
assert m.last_completed_bar(bars, sh, ms(2026, 9, 19, 9, 0))[:2] == (dt.date(2026, 9, 18), D("149.80"))    # next morning
kr = m.STOCK_MARKETS["kr"]; tz9 = dt.timezone(dt.timedelta(hours=9))
assert m.last_completed_bar(bars_kr, kr, ms(2026, 9, 18, 15, 40, tz9))[:2] == (dt.date(2026, 9, 17), D("1745000"))
assert m.last_completed_bar(bars_kr, kr, ms(2026, 9, 18, 15, 46, tz9))[:2] == (dt.date(2026, 9, 18), D("1825000"))
try: m.last_completed_bar([(dt.date(2026, 9, 18), D(1))], sh, ms(2026, 9, 18, 10, 0)); assert False
except ValueError: pass

# --- StockMarket with mocked HTTP, then Bot display
async def run():
    m.StockMarket.ATTEMPTS = 1
    calls = []
    async def fake_get(url, timeout=15, headers=None):
        calls.append((url, headers))
        if "secid=1.688836" in url: return em
        if "symbol=000660" in url: return nv
        raise m.RemoteError("HTTP 404: 接口请求失败")
    m.http_get = fake_get
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ADMIN_USER_ID": "42", "SYMBOLS": "UNITREEUSDT,HK0625USDT,SKHYNIXUSDT",
                             "MIN_ALERT_GAP_SECONDS": "0"})
    stocks = m.StockMarket(cfg)
    now = ms(2026, 9, 18, 17, 0)
    await stocks.refresh(now)
    assert calls and all(h["User-Agent"].startswith("Mozilla") for _, h in calls)
    u = stocks.closes["UNITREEUSDT"]; assert u.value == D("149.80") and u.currency == "CNY" and u.source == "上交所688836·东方财富"
    assert m.stamp(u.close_ms, seconds=False) == "09-18 15:00", m.stamp(u.close_ms)
    k = stocks.closes["SKHYNIXUSDT"]; assert k.value == D("1825000") and k.currency == "KRW" and m.stamp(k.close_ms, seconds=False) == "09-18 14:30"
    assert "HK0625USDT" not in stocks.closes and "HTTP 404" in stocks.errors["HK0625USDT"]
    n = len(calls); await stocks.refresh(now); assert len(calls) == n  # cached within 10 min
    await stocks.refresh(now, force=True); assert len(calls) == 2 * n
    # keep last good close when a later refresh fails
    async def failing(url, timeout=15, headers=None): raise m.RemoteError("网络错误 (TimeoutError)")
    m.http_get = failing; await stocks.refresh(now, force=True)
    assert stocks.closes["UNITREEUSDT"].value == D("149.80") and "网络错误" in stocks.errors["UNITREEUSDT"]
    m.http_get = fake_get; await stocks.refresh(now, force=True); assert "UNITREEUSDT" not in stocks.errors

    # Bot: status shows auto closes, failures and manual override
    class FakeTelegram:
        def __init__(self): self.sent = []
        async def call(self, *a, **k): return True
        async def send(self, chat, thread, text, reply_markup=None, parse_mode=None): self.sent.append(text)
    class FakeMarket:
        def __init__(self, c): self.config = c
        def now_ms(self): return int(time.time() * 1000)
        async def sync_clock(self): pass
        async def prices(self): return {s: {"symbol": s, "price": "100", "time": self.now_ms()} for s in self.config.symbols}
    store = m.Store(":memory:"); tg = FakeTelegram(); bot = m.Bot(cfg, store, FakeMarket(cfg), tg)
    bot.stocks = stocks
    async def ask(text):
        tg.sent.clear(); await bot.process_message({"text": text, "chat": {"id": 1}, "from": {"id": 42}, "date": time.time()}); return tg.sent[-1]
    # daily mode needs a Binance baseline; inject snapshots directly after a cycle attempt
    nowm = bot.market.now_ms(); base = m.Baseline(D("100"), "k", "币安日 K 昨收", nowm + m.DAY_MS)
    for s in cfg.symbols: bot.snapshots[s] = {"quote": m.Quote(D("100"), nowm), "baseline": base}
    st = await ask("/status")
    assert "交易所 <b>149.8 CNY</b>" in st and "无 CNY 汇率" in st and "收·东方财富）" in st, st
    assert "<b>1,825,000 KRW</b>" in st and "韩国时间 收·Naver）" in st, st
    assert "交易所 ⚠️ 获取失败（东方财富: HTTP 404" in st and "/setexchange" in st, st
    await ask("/setexchange UNITREE 150.5 CNY 09-17 15:00")
    st = await ask("/status"); assert "<b>150.5 CNY</b>" in st and "东方财富）" not in st.split("UNITREEUSDT")[1].split("SHEIN")[0], st
    # alerts include auto close; one_cycle triggers refresh (cached) without breaking
    store.put("subscriptions", {"1:0": {"chat": 1, "thread": 0, "active": True}})
    bot.market.prices = lambda: asyncio.sleep(0, {s: {"symbol": s, "price": "100", "time": bot.market.now_ms()} for s in cfg.symbols})
    bot.snapshots.clear(); tg.sent.clear()
    await bot.one_cycle()  # daily baseline fetch fails (no Binance) -> per-symbol notices, but refresh must not raise
    assert stocks.closes["SKHYNIXUSDT"].value == D("1825000")
    print("STOCKS_OK")
asyncio.run(run())
