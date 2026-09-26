import asyncio, sys, time, json, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
tz8 = dt.timezone(dt.timedelta(hours=8))
def ms(y, mo, d, h, mi, tz=tz8): return int(dt.datetime(y, mo, d, h, mi, tzinfo=tz).timestamp() * 1000)
sh, hk = m.STOCK_MARKETS["sh"], m.STOCK_MARKETS["hk"]

# --- Tencent quotes (GBK). A-share: [3] current [4] prev close [30] yyyymmddHHMMSS
tx_sh = 'v_sh688836="1~宇树科技~688836~76.50~75.00~75.20~' + "~".join(["0"] * 24) + '~20260918150003~x~y";'
tx_sh = tx_sh.encode("gbk")
fields = tx_sh.decode("gbk").split('="')[1].split("~"); assert fields[30] == "20260918150003", fields[30]
assert m.parse_quote_close("腾讯", "sh", tx_sh, sh, ms(2026, 9, 18, 14, 0))[:2] == (None, D("75.00"))            # session running -> prev close
assert m.parse_quote_close("腾讯", "sh", tx_sh, sh, ms(2026, 9, 18, 15, 20))[:2] == (dt.date(2026, 9, 18), D("76.50"))  # final
assert m.parse_quote_close("腾讯", "sh", tx_sh, sh, ms(2026, 9, 19, 10, 0))[:2] == (dt.date(2026, 9, 18), D("76.50"))   # weekend
tx_hk = ('v_hk00625="100~希音~00625~37.76~38.10~38.00~' + "~".join(["0"] * 24) + '~2026/09/18 16:08:11~x";').encode("gbk")
assert m.parse_quote_close("腾讯", "hk", tx_hk, hk, ms(2026, 9, 18, 16, 30))[:2] == (dt.date(2026, 9, 18), D("37.76"))
assert m.parse_quote_close("腾讯", "hk", tx_hk, hk, ms(2026, 9, 18, 16, 20))[:2] == (None, D("38.10"))  # closing auction not final yet
assert m.parse_quote_close("腾讯", "hk", tx_hk, hk, ms(2026, 9, 18, 15, 0))[:2] == (None, D("38.10"))
# --- Sina quotes. A-share: name,open,prev[2],current[3],...,date[30],time[31]; HK rt_hk: prev[3], current[6], date[17], time[18]
sina_sh = ('var hq_str_sh688836="宇树科技,75.20,75.00,76.50,' + ",".join(["0"] * 26) + ',2026-09-18,15:00:03,00";').encode("gbk")
assert m.parse_quote_close("新浪", "sh", sina_sh, sh, ms(2026, 9, 18, 16, 0))[:2] == (dt.date(2026, 9, 18), D("76.50"))
assert m.parse_quote_close("新浪", "sh", sina_sh, sh, ms(2026, 9, 18, 11, 0))[:2] == (None, D("75.00"))
sina_hk = ('var hq_str_rt_hk00625="SHEIN,希音,38.00,38.10,38.20,37.50,37.76,' + ",".join(["0"] * 10) + ',2026/09/18,16:08:11";').encode("gbk")
assert m.parse_quote_close("新浪", "hk", sina_hk, hk, ms(2026, 9, 18, 17, 0))[:2] == (dt.date(2026, 9, 18), D("37.76"))
for bad in [b'v_sh688836="";', b"garbage", b'var hq_str_sh688836="a,b";']:
    try: m.parse_quote_close("腾讯" if bad.startswith(b"v_") else "新浪", "sh", bad, sh, ms(2026, 9, 18, 17, 0)); assert False, bad
    except ValueError: pass

# --- sources order and headers
src = m.StockMarket.sources(m.StockTicker("hk", "00625"))
assert [n for n, _, _ in src] == ["东方财富", "腾讯", "新浪"] and "secid=116.00625" in src[0][1] and src[1][1].endswith("q=hk00625") and src[2][1].endswith("list=rt_hk00625")
assert all("Referer" in h for _, _, h in src)
assert [n for n, _, _ in m.StockMarket.sources(m.StockTicker("kr", "000660"))] == ["Naver"]

async def run():
    m.StockMarket.ATTEMPTS = 2
    sleeps = []
    real_sleep = asyncio.sleep
    async def fast_sleep(s): sleeps.append(s); await real_sleep(0)
    m.asyncio.sleep = fast_sleep
    calls = []
    async def fake_get(url, timeout=15, headers=None):
        calls.append(url)
        if "eastmoney" in url: raise m.RemoteError("网络错误 (RemoteDisconnected)")
        if "qt.gtimg.cn" in url: return tx_sh
        raise AssertionError("should not reach sina")
    m.http_get = fake_get
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT"})
    store = m.Store(":memory:")
    sm = m.StockMarket(cfg, store)
    now = ms(2026, 9, 18, 17, 0)
    await sm.refresh(now, force=True)
    c = sm.closes["UNITREEUSDT"]
    assert c.value == D("76.50") and c.source == "上交所688836·腾讯" and c.label == "证券交易所收盘价｜2026-09-18 上交所", c
    assert [u for u in calls if "eastmoney" in u].__len__() == 2 and sum("gtimg" in u for u in calls) == 1  # retried once, then fell back
    assert 1.5 in sleeps and "UNITREEUSDT" not in sm.errors
    # persisted: a fresh StockMarket (restart) starts with the last good close
    saved = store.get("stock_close:UNITREEUSDT"); assert saved["value"] == "76.50" and saved["source"] == "上交所688836·腾讯"
    sm2 = m.StockMarket(cfg, store); assert sm2.closes["UNITREEUSDT"].value == D("76.50") and sm2.closes["UNITREEUSDT"].close_ms == c.close_ms
    # all sources fail -> error lists each source once, close kept
    async def all_fail(url, timeout=15, headers=None): raise m.RemoteError("网络错误 (RemoteDisconnected)")
    m.http_get = all_fail
    await sm2.refresh(now, force=True)
    e = sm2.errors["UNITREEUSDT"]; assert e.count("东方财富") == 1 and "腾讯" in e and "新浪" in e and sm2.closes["UNITREEUSDT"].value == D("76.50"), e
    # session running: prev close with unknown date
    m.http_get = fake_get
    await sm2.refresh(ms(2026, 9, 18, 11, 0), force=True)
    c = sm2.closes["UNITREEUSDT"]; assert c.value == D("75.00") and c.close_ms == 0 and "上一交易日" in c.label and "UNITREEUSDT" not in sm2.errors, c
    row = m.reference_row("exchange", D("76"), c, m.FxRates({"CNY": D("7.1")}), "cn")
    assert "（上一交易日·腾讯）" in row, row
    print("STOCK2_OK")
asyncio.run(run())
