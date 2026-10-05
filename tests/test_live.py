"""Contract odds switch from the Binance proxy to the stock's own realtime quote while the stock trades."""
import asyncio, sys, json, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
tz8, kst = dt.timezone(dt.timedelta(hours=8)), dt.timezone(dt.timedelta(hours=9))
def bj(mo, d, h, mi, s=0): return int(dt.datetime(2026, mo, d, h, mi, s, tzinfo=tz8).timestamp() * 1000)
def kr(mo, d, h, mi): return int(dt.datetime(2026, mo, d, h, mi, tzinfo=kst).timestamp() * 1000)

def tencent(code, cur, prev, when):
    return (f'v_{code}="1~宇树科技~688836~{cur}~{prev}~75.20~' + "~".join(["0"] * 24) + f'~{when}~x";').encode("gbk")

# --- parsing ---------------------------------------------------------------------------------------
q = m.parse_stock_live("腾讯", "sh", tencent("sh688836", "78.00", "76.50", "20260928100003"), 0)
assert (q.last, q.prev_close, q.quoted_ms, q.source) == (D("78.00"), D("76.50"), bj(9, 28, 10, 0, 3), "腾讯"), q
sina = ('var hq_str_sh688836="宇树科技,75.20,76.50,78.10,' + ",".join(["0"] * 26) + ',2026-09-28,10:00:05,00";').encode("gbk")
q = m.parse_stock_live("新浪", "sh", sina, 0)
assert (q.last, q.prev_close, q.quoted_ms) == (D("78.10"), D("76.50"), bj(9, 28, 10, 0, 5)), q
sina_hk = ('var hq_str_rt_hk00625="SHEIN,希音,38.00,38.10,38.20,37.50,37.76,' + ",".join(["0"] * 10) + ',2026/09/28,10:15:00";').encode("gbk")
q = m.parse_stock_live("新浪", "hk", sina_hk, 0)
assert (q.last, q.prev_close, q.quoted_ms) == (D("37.76"), D("38.10"), bj(9, 28, 10, 15)), q
naver = json.dumps({"datas": [{"stockName": "SK하이닉스", "closePrice": "1,870,000", "compareToPreviousClosePrice": "13,000",
                               "compareToPreviousPrice": {"code": "2"}, "localTradedAt": "2026-09-28T10:00:00+09:00",
                               "marketStatus": "OPEN"}]}).encode()
q = m.parse_stock_live("Naver", "kr", naver, 0)
assert (q.last, q.prev_close, q.quoted_ms, q.source) == (D("1870000"), D("1857000"), kr(9, 28, 10, 0), "Naver"), q
for bad in [b'v_sh688836="";', tencent("sh688836", "0.00", "76.50", "20260928092000")]:
    try: m.parse_stock_live("腾讯", "sh", bad, 0); assert False, bad
    except ValueError: pass
hk_src = m.StockMarket.live_sources(m.StockTicker("hk", "00625"))
assert [n for n, _, _ in hk_src] == ["腾讯", "新浪"] and hk_src[0][1].endswith("q=r_hk00625") and hk_src[1][1].endswith("list=rt_hk00625")
assert m.StockMarket.live_sources(m.StockTicker("sh", "688825"))[0][1].endswith("q=sh688825")
assert "domestic/stock/000660" in m.StockMarket.live_sources(m.StockTicker("kr", "000660"))[0][1]

# --- session window: first continuous minute until the close is final --------------------------------
hol = frozenset({dt.date(2026, 10, 1)})
assert m.stock_live_window("sh", bj(9, 28, 9, 29)) is None and m.stock_live_window("sh", bj(9, 28, 9, 30))
assert m.stock_live_window("sh", bj(9, 28, 15, 14)) and m.stock_live_window("sh", bj(9, 28, 15, 15)) is None
assert m.stock_live_window("hk", bj(9, 28, 16, 20)) and m.stock_live_window("hk", bj(9, 28, 16, 25)) is None
assert m.stock_live_window("kr", kr(9, 28, 9, 0)) and m.stock_live_window("kr", kr(9, 28, 15, 45)) is None
assert m.stock_live_window("sh", bj(9, 26, 10, 0)) is None and m.stock_live_window("sh", bj(10, 1, 10, 0), hol) is None


async def run():
    class FakeMarket(m.Binance):
        def __init__(self, c): super().__init__(c); self.now = bj(9, 28, 10, 1)
        def now_ms(self): return self.now
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "PROB_VOL": "UNITREEUSDT=3.5"})
    bot = m.Bot(cfg, m.Store(":memory:"), FakeMarket(cfg), None)
    sh = m.STOCK_MARKETS["sh"]
    bot.stocks.closes["UNITREEUSDT"] = m.StockMarket.baseline(m.StockTicker("sh", "688836"), sh, "腾讯", dt.date(2026, 9, 24), D("76.50"))
    bot.anchors["UNITREEUSDT"] = (bj(9, 24, 15, 0), D("10.60"))
    now = bot.market.now

    # Before the open (and on holidays / weekends) the Binance proxy is the only live price
    o = bot.contract_odds("UNITREEUSDT", D("10.80"), bj(9, 28, 9, 0))
    assert isinstance(o, m.CloseOdds) and o.proxy_note.startswith("币安 10.8 / 收盘时刻 10.6") and "现货" not in o.proxy_note, o

    # The session runs: fetch the stock's own quote, Tencent first
    calls = []
    async def fake_get(url, timeout=15, headers=None):
        calls.append(url)
        if "gtimg" in url: return tencent("sh688836", "78.00", "76.50", "20260928100003")
        raise AssertionError(url)
    m.http_get = fake_get
    assert await bot.stocks.refresh_live(bj(9, 28, 9, 0)) is False and not calls  # not trading yet: no request
    await bot.stocks.refresh_live(now)
    assert len(calls) == 1 and bot.stocks.live["UNITREEUSDT"].last == D("78.00")
    assert await bot.stocks.refresh_live(now) is False and len(calls) == 1          # QUOTE_REFRESH_SECONDS cadence (5 s)
    assert cfg.quote_refresh == 1 and m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "QUOTE_REFRESH_SECONDS": "10"}).quote_refresh == 10
    try: m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "QUOTE_REFRESH_SECONDS": "0"}); assert False
    except ValueError as e: assert "QUOTE_REFRESH_SECONDS" in str(e), e
    o = bot.contract_odds("UNITREEUSDT", D("10.80"), now)
    assert isinstance(o, m.CloseOdds) and o.effective == D("78.00") and o.ref == D("76.50") and o.mode == "盘中", o
    assert o.proxy_note == "上交所现货 78（腾讯·盘中直接用现货）" and o.ref_note.endswith("·腾讯") and o.unit == "CNY", o
    assert o.target == dt.date(2026, 9, 28) and abs(o.remaining - 209 / 240) < 1e-9, o.remaining

    # The stored close is not the previous session's (e.g. still pending): use the live quote's previous close
    bot.stocks.closes["UNITREEUSDT"] = m.StockMarket.baseline(m.StockTicker("sh", "688836"), sh, "腾讯", dt.date(2026, 9, 23), D("75.00"))
    bot.anchors["UNITREEUSDT"] = (bj(9, 23, 15, 0), D("10.40"))
    o = bot.contract_odds("UNITREEUSDT", D("10.80"), now)
    assert o.ref == D("76.50") and o.ref_note == "昨收（实时行情）", o
    bot.stocks.closes["UNITREEUSDT"] = m.StockMarket.baseline(m.StockTicker("sh", "688836"), sh, "腾讯", dt.date(2026, 9, 24), D("76.50"))
    bot.anchors["UNITREEUSDT"] = (bj(9, 24, 15, 0), D("10.60"))

    # Lunch break does not age the quote; after 10 minutes of silence in-session we fall back to Binance, labelled
    bot.stocks.live["UNITREEUSDT"] = m.parse_stock_live("腾讯", "sh", tencent("sh688836", "78.00", "76.50", "20260928113000"), 0)
    assert isinstance(bot.contract_odds("UNITREEUSDT", D("10.80"), bj(9, 28, 13, 5)), m.CloseOdds)
    assert bot.contract_odds("UNITREEUSDT", D("10.80"), bj(9, 28, 13, 5)).effective == D("78.00")
    o = bot.contract_odds("UNITREEUSDT", D("10.80"), bj(9, 28, 13, 15))
    assert "现货行情已超 10 分钟未更新" in o.proxy_note and "暂用币安" in o.proxy_note and o.proxy_note.startswith("币安"), o.proxy_note
    # The last print just after 15:00 stands until the close is final at 15:15
    bot.stocks.live["UNITREEUSDT"] = m.parse_stock_live("腾讯", "sh", tencent("sh688836", "79.00", "76.50", "20260928150002"), 0)
    o = bot.contract_odds("UNITREEUSDT", D("10.80"), bj(9, 28, 15, 14))
    assert o.effective == D("79.00") and o.fair_up > 0.99, o
    # Once the closing print is noted, the card moves on to the next close measured from it, without
    # waiting for the daily bar (which only confirms it after 15:15)
    bot.note_live_close("UNITREEUSDT", cfg.tickers["UNITREEUSDT"], bj(9, 28, 15, 14))
    assert bot.odds_base("UNITREEUSDT", bj(9, 28, 15, 14))[:2] == (bj(9, 28, 15, 0), D("79.00"))
    assert bot.contract_odds("UNITREEUSDT", D("10.80"), bj(9, 28, 15, 14)) == "等待币安在收盘时刻的价格"
    bot.anchors["UNITREEUSDT"] = (bj(9, 28, 15, 0), D("10.80"))
    o = bot.contract_odds("UNITREEUSDT", D("10.80"), bj(9, 28, 15, 14))
    assert o.target == dt.date(2026, 9, 29) and o.ref == D("79.00") and o.effective == D("79.00") and "日K待确认" in o.ref_note, o
    o = bot.contract_odds("UNITREEUSDT", D("10.90"), bj(9, 28, 15, 40))  # live window over, bar still pending
    assert o.target == dt.date(2026, 9, 29) and o.ref == D("79.00") and o.effective > D("79.00"), o
    bot.anchors["UNITREEUSDT"] = (bj(9, 24, 15, 0), D("10.60"))
    # Yesterday's quote at the open is not today's price
    bot.stocks.live["UNITREEUSDT"] = m.parse_stock_live("腾讯", "sh", tencent("sh688836", "76.50", "75.00", "20260924150002"), 0)
    o = bot.contract_odds("UNITREEUSDT", D("10.80"), bj(9, 28, 9, 31))
    assert o.proxy_note.endswith("｜现货今日尚未开盘成交，暂用币安"), o.proxy_note
    # Every source failing is reported next to the Binance fallback
    bot.stocks.live.clear()
    async def down(url, timeout=15, headers=None): raise m.RemoteError("HTTP 502")
    m.http_get = down
    await bot.stocks.refresh_live(now, force=True)
    o = bot.contract_odds("UNITREEUSDT", D("10.80"), now)
    assert "现货行情未取得（腾讯: HTTP 502" in o.proxy_note and "暂用币安" in o.proxy_note, o.proxy_note
    # No Binance anchor either: say what is missing
    bot.anchors.clear()
    assert bot.contract_odds("UNITREEUSDT", D("10.80"), now) == "等待币安在收盘时刻的价格"

    # HK: a lagging Tencent quote (e.g. the 15-minute delayed feed) is not accepted; Sina's fresh print is used
    hcfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "HK0625USDT"})
    hbot = m.Bot(hcfg, m.Store(":memory:"), FakeMarket(hcfg), None)
    hbot.stocks.closes["HK0625USDT"] = m.StockMarket.baseline(m.StockTicker("hk", "00625", True), m.STOCK_MARKETS["hk"], "腾讯",
                                                              dt.date(2026, 9, 25), D("35.10"))
    hbot.anchors["HK0625USDT"] = (bj(9, 25, 16, 10), D("35.63"))
    lagging = ('v_r_hk00625="100~希音~00625~35.20~35.10~35.00~' + "~".join(["0"] * 24) + '~2026/09/28 09:33:00~x";').encode("gbk")
    fresh = ('var hq_str_rt_hk00625="SHEIN,希音,35.00,35.10,35.60,34.90,35.40,' + ",".join(["0"] * 10) + ',2026/09/28,09:48:50";').encode("gbk")
    async def hk_get(url, timeout=15, headers=None):
        return lagging if "gtimg" in url else fresh
    m.http_get = hk_get
    t = bj(9, 28, 9, 49)
    await hbot.stocks.refresh_live(t, force=True)
    o = hbot.contract_odds("HK0625USDT", D("35.60"), t)
    assert o.effective == D("35.40") and "新浪" in o.proxy_note and o.ref == D("35.10") and o.unit == "", o
    assert hbot.card_currency("HK0625USDT") == "HKD" and hbot.card_currency("") == ""  # the web card still names it
    async def all_lag(url, timeout=15, headers=None): return lagging
    m.http_get = all_lag
    await hbot.stocks.refresh_live(t, force=True)
    o = hbot.contract_odds("HK0625USDT", D("35.60"), t)
    assert "现货行情已超 10 分钟未更新" in o.proxy_note and "报价停在 09-28 09:33" in hbot.stocks.live_errors["HK0625USDT"], hbot.stocks.live_errors

    # Several A-share / HK stocks: one Tencent request for all of them per round (1-second polling); Sina is asked only
    # for the codes Tencent left stale or unknown, and only for those
    m.SOURCE_HEALTH.hosts.clear()
    assert m.split_quote_records("腾讯", b'v_sh688836="1~a";\nv_r_hk00625="2~b";\nv_pv_none_match="1";') == {"sh688836": "1~a", "r_hk00625": "2~b", "pv_none_match": "1"}
    assert m.split_quote_records("新浪", 'var hq_str_sh688836="x,1";\nvar hq_str_rt_hk00625="";'.encode("gbk")) == {"sh688836": "x,1", "rt_hk00625": ""}
    assert m.StockMarket.live_batch_sources([m.StockTicker("sh", "688836"), m.StockTicker("hk", "00625")])[0][1].endswith("q=sh688836,r_hk00625")
    assert m.StockMarket.live_batch_sources([m.StockTicker("sh", "688836"), m.StockTicker("hk", "00625")])[1][1].endswith("list=sh688836,rt_hk00625")
    bcfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT,CXMTUSDT,HK0625USDT"})
    bbot = m.Bot(bcfg, m.Store(":memory:"), FakeMarket(bcfg), None)
    hk_rec = lambda when: ('v_r_hk00625="100~希音~00625~35.20~35.10~35.00~' + "~".join(["0"] * 24) + f'~{when}~x";').encode("gbk")
    three = tencent("sh688836", "78.00", "76.50", "20260928100003") + tencent("sh688825", "54.00", "55.00", "20260928100002") + hk_rec("2026/09/28 10:00:01")
    calls = []
    async def batch_get(url, timeout=15, headers=None):
        calls.append(url)
        if "gtimg" in url: return three
        raise AssertionError(url)
    m.http_get = batch_get
    t = bj(9, 28, 10, 0, 5)
    await bbot.stocks.refresh_live(t, force=True)
    codes = lambda url: sorted(url.split("=")[-1].split(","))  # the request lists the tickers in EXCHANGE_TICKERS order
    assert len(calls) == 1 and codes(calls[0]) == ["r_hk00625", "sh688825", "sh688836"], calls
    assert [bbot.stocks.live[s].last for s in ("UNITREEUSDT", "CXMTUSDT", "HK0625USDT")] == [D("78.00"), D("54.00"), D("35.20")] and not bbot.stocks.live_errors
    stale_one = tencent("sh688836", "78.00", "76.50", "20260928100003") + tencent("sh688825", "54.00", "55.00", "20260925150001") + hk_rec("2026/09/28 10:00:01")
    sina_one = ('var hq_str_sh688825="长鑫科技,55.00,55.00,54.50,' + ",".join(["0"] * 26) + ',2026-09-28,10:00:04,00";').encode("gbk")
    calls.clear()
    async def mixed_get(url, timeout=15, headers=None):
        calls.append(url)
        if "gtimg" in url: return stale_one
        if "sinajs" in url: return sina_one
        raise AssertionError(url)
    m.http_get = mixed_get
    await bbot.stocks.refresh_live(t, force=True)
    assert len(calls) == 2 and calls[1].endswith("list=sh688825"), calls
    assert bbot.stocks.live["CXMTUSDT"].last == D("54.50") and bbot.stocks.live["CXMTUSDT"].source == "新浪" and not bbot.stocks.live_errors, bbot.stocks.live_errors
    calls.clear()  # a code neither feed knows is reported as such; the others are unaffected
    async def missing_get(url, timeout=15, headers=None):
        calls.append(url)
        if "gtimg" in url: return tencent("sh688836", "78.00", "76.50", "20260928100003") + b'v_pv_none_match="1";'
        if "sinajs" in url: return b'var hq_str_sh688825="";\nvar hq_str_rt_hk00625="";'
        raise AssertionError(url)
    m.http_get = missing_get
    await bbot.stocks.refresh_live(t, force=True)
    assert codes(calls[1]) == ["rt_hk00625", "sh688825"] and bbot.stocks.live["UNITREEUSDT"].last == D("78.00"), calls
    assert "腾讯: 腾讯行情为空" in bbot.stocks.live_errors["CXMTUSDT"] and "新浪: 新浪行情为空" in bbot.stocks.live_errors["HK0625USDT"], bbot.stocks.live_errors

    # KOSPI trading: today's 15:30 anchor is in the future, so HL is not asked for it (no bogus error)
    asked = []
    async def hl_json(url, payload=None, timeout=15): asked.append(payload); return []
    m.http_json = hl_json
    k = m.IndexQuote("KOSPI", D("6933.77"), D("7080.92"), None, None, None, kr(9, 28, 10, 53), "Naver")
    kbot = m.Bot(cfg, m.Store(":memory:"), FakeMarket(cfg), None)
    class H: coin, fetched_ms, mark = "xyz:KR200", kr(9, 28, 10, 53), D("1099.2")
    await kbot.kospi_anchor(k, H)
    assert not asked and kbot.kospi_anchor_error == "" and "KOSPI" not in kbot.anchors
    print("LIVE_OK")


asyncio.run(run())
