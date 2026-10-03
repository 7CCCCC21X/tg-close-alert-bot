import asyncio, sys, json, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
KST = dt.timezone(dt.timedelta(hours=9))


def kr(y, mo, d, h, mi):
    return int(dt.datetime(y, mo, d, h, mi, tzinfo=KST).timestamp() * 1000)


# Naver's daily chart blends in Nextrade after-hours trades: 09-28 "closes" at 1,761,000 (NXT, 20:00),
# while the KRX closing auction was 1,768,000 (Naver's 기준가 for 09-29).
CHART = ('<chartdata><item data="20260925|1740000|1780000|1730000|1750000|100" />'
         '<item data="20260928|1757000|1788000|1736000|1761000|100" />'
         '<item data="20260929|1768000|1790000|1760000|1769000|100" /></chartdata>').encode()


def realtime(price, change, direction, at):
    return json.dumps({"datas": [{"itemCode": "000660", "stockName": "SK하이닉스", "closePrice": price,
                                  "compareToPreviousClosePrice": change, "compareToPreviousPrice": {"code": direction},
                                  "fluctuationsRatio": "0.06", "marketStatus": "OPEN", "localTradedAt": at}]}).encode()


def yahoo(bars, offset=32400):
    """A Yahoo v8 chart body: bars are (date, close); stamps at 09:00 local like Yahoo's."""
    stamps = [int(dt.datetime(d.year, d.month, d.day, 9, tzinfo=dt.timezone(dt.timedelta(seconds=offset))).timestamp()) for d, _ in bars]
    return json.dumps({"chart": {"result": [{"meta": {"gmtoffset": offset}, "timestamp": stamps,
                                             "indicators": {"quote": [{"open": [c for _, c in bars], "close": [c for _, c in bars]}]}}],
                                 "error": None}}).encode()


async def run():
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "SKHYNIXUSDT"})
    stocks = m.StockMarket(cfg, m.Store(":memory:"))
    ticker = cfg.tickers["SKHYNIXUSDT"]
    answers = {}

    async def fake_get(url, timeout=15, headers=None):
        for key, body in answers.items():
            if key in url:
                if isinstance(body, Exception):
                    raise body
                return body
        raise AssertionError(url)
    m.http_get = fake_get

    # 09-29 15:27, KRX trading: 1,769,000 (+1,000) -> the 09-28 close is today's 기준가 1,768,000, not the chart's 1,761,000
    answers = {"fchart": CHART, "polling": realtime("1,769,000", "1,000", "2", "2026-09-29T15:27:10+09:00")}
    base = await stocks.fetch("SKHYNIXUSDT", ticker, kr(2026, 9, 29, 15, 27))
    assert base.value == D("1768000") and base.close_ms == kr(2026, 9, 28, 15, 30) and "Naver KRX" in base.source, base

    # 09-29 16:00, after the KRX close, NXT still trading: the day's close is KRX's own last price
    answers = {"fchart": CHART, "polling": realtime("1,769,000", "1,000", "2", "2026-09-29T15:30:00+09:00")}
    base = await stocks.fetch("SKHYNIXUSDT", ticker, kr(2026, 9, 29, 15, 36))
    assert base.value == D("1769000") and base.close_ms == kr(2026, 9, 29, 15, 30) and base.prev_value == D("1768000"), base

    # 16:20: Naver's quote now follows Nextrade after-hours trades (1,786,000): the KRX close kept above stands
    answers = {"fchart": CHART, "polling": realtime("1,786,000", "18,000", "2", "2026-09-29T16:20:00+09:00")}
    base = await stocks.fetch("SKHYNIXUSDT", ticker, kr(2026, 9, 29, 16, 20))
    assert base.value == D("1769000") and base.prev_value == D("1768000") and "Naver KRX" in base.source, base
    # without a KRX close captured in time the chart is used, and says it includes NXT
    fresh = m.StockMarket(cfg, m.Store(":memory:"))
    base = await fresh.fetch("SKHYNIXUSDT", ticker, kr(2026, 9, 29, 16, 20))
    assert base.value == D("1769000") and "含 NXT" in base.source, base  # the chart's 09-29 bar
    # 15:31: the auction may not be settled yet: nothing is kept
    early = m.StockMarket(cfg, m.Store(":memory:"))
    answers = {"fchart": CHART, "polling": realtime("1,770,000", "2,000", "2", "2026-09-29T15:30:05+09:00")}
    await early.fetch("SKHYNIXUSDT", ticker, kr(2026, 9, 29, 15, 31))
    assert early.store.get("krx_close:000660:2026-09-29") is None

    # a falling day: change is negative (code 5 = 하락)
    answers = {"fchart": CHART, "polling": realtime("1,760,000", "8,000", "5", "2026-09-29T15:30:00+09:00")}
    stocks.store.delete_prefix("krx_close:")
    base = await stocks.fetch("SKHYNIXUSDT", ticker, kr(2026, 9, 29, 15, 35))
    assert base.value == D("1760000") and base.prev_value == D("1768000"), base

    # the realtime quote fails: the chart's value stands, and says it is the chart's (with NXT), not a KRX figure
    answers = {"fchart": CHART, "polling": m.RemoteError("网络错误 (URLError)")}
    base = await stocks.fetch("SKHYNIXUSDT", ticker, kr(2026, 9, 29, 15, 27))
    assert base.value == D("1761000") and base.source.endswith("·Naver 日K·含 NXT"), base
    # an undated realtime answer cannot replace anything either: same label
    answers = {"fchart": CHART, "polling": realtime("1,769,000", "1,000", "2", "")}
    base = await m.StockMarket(cfg, m.Store(":memory:")).fetch("SKHYNIXUSDT", ticker, kr(2026, 9, 29, 15, 27))
    assert base.value == D("1761000") and base.source.endswith("·Naver 日K·含 NXT"), base

    # --- Yahoo 000660.KS: KRX-only daily bars (no NXT) come first ----------------------------------------------------
    ybars = yahoo([(dt.date(2026, 9, 25), 1750000.0), (dt.date(2026, 9, 28), 1768000.0), (dt.date(2026, 9, 29), 1765000.0),
                   (dt.date(2026, 9, 30), None)])
    answers = {"finance.yahoo.com": ybars, "fchart": CHART,
               "polling": realtime("1,785,000", "17,000", "2", "2026-09-29T19:50:00+09:00")}  # NXT after-hours print
    ystocks = m.StockMarket(cfg, m.Store(":memory:"))
    base = await ystocks.fetch("SKHYNIXUSDT", ticker, kr(2026, 9, 29, 19, 50))
    assert base.value == D("1765000") and base.prev_value == D("1768000") and base.source.endswith("·Yahoo"), base
    assert base.close_ms == kr(2026, 9, 29, 15, 30), base
    # 15:36: Yahoo's 09-29 bar is not final yet, but the KRX close captured from 15:33 is newer -> it wins
    answers = {"finance.yahoo.com": ybars, "fchart": CHART, "polling": realtime("1,765,000", "3,000", "5", "2026-09-29T15:30:00+09:00")}
    base = await m.StockMarket(cfg, m.Store(":memory:")).fetch("SKHYNIXUSDT", ticker, kr(2026, 9, 29, 15, 36))
    assert base.value == D("1765000") and base.close_ms == kr(2026, 9, 29, 15, 30) and "Naver KRX" in base.source, base
    # a KRX close captured that day wins over a Yahoo bar that would carry an after-hours print
    answers = {"finance.yahoo.com": yahoo([(dt.date(2026, 9, 28), 1768000.0), (dt.date(2026, 9, 29), 1782000.0)]),
               "polling": m.RemoteError("x")}
    cap = m.StockMarket(cfg, m.Store(":memory:")); cap.store.put("krx_close:000660:2026-09-29", ["1765000", "1768000"])
    base = await cap.fetch("SKHYNIXUSDT", ticker, kr(2026, 9, 29, 19, 50))
    assert base.value == D("1765000") and "Naver KRX" in base.source, base
    # the same figure as Yahoo's: nothing is replaced, so the close stays Yahoo's
    cap.store.put("krx_close:000660:2026-09-29", ["1782000", "1768000"])
    base = await cap.fetch("SKHYNIXUSDT", ticker, kr(2026, 9, 29, 19, 50))
    assert base.value == D("1782000") and base.source.endswith("·Yahoo"), base
    # 10:00 the next day: Yahoo's 09-29 close (the 09-30 bar has no close yet)
    answers = {"finance.yahoo.com": ybars, "fchart": CHART, "polling": m.RemoteError("x")}
    base = await ystocks.fetch("SKHYNIXUSDT", ticker, kr(2026, 9, 30, 10, 0))
    assert base.value == D("1765000") and base.source.endswith("·Yahoo"), base
    assert [c for _, _, c in m.parse_yahoo_daily(ybars)] == [D("1750000.00"), D("1768000.00"), D("1765000.00")]
    for bad in (b"garbage", b'{"chart":{"result":null}}'):
        try: m.parse_yahoo_daily(bad); assert False
        except ValueError: pass

    # --- KOSPI: the dated daily-chart close wins over a realtime figure frozen before the closing auction -------------
    class FakeMarket:
        def now_ms(self): return kr(2026, 9, 29, 10, 0)
    bot = m.Bot(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT"}), m.Store(":memory:"), FakeMarket(), None)
    kospi_chart = ('<item data="20260925|0|0|0|7080.92|1" /><item data="20260928|0|0|0|6910.89|1" />'
                   '<item data="20260929|0|0|0|6830.00|1" />').encode()
    answers = {"fchart": kospi_chart}
    await bot.kospi.refresh_daily(kr(2026, 9, 29, 10, 0))
    assert bot.kospi.daily == {dt.date(2026, 9, 25): D("7080.92"), dt.date(2026, 9, 28): D("6910.89")}, bot.kospi.daily  # 09-29 not final
    ref, note = bot.kospi_ref(dt.date(2026, 9, 28), D("6889.74"))
    assert ref == D("6910.89") and note == "09-28 收盘（日K；实时行情为 6,889.74）", note
    assert bot.kospi_ref(dt.date(2026, 9, 28), D("6910.89")) == (D("6910.89"), "09-28 收盘")      # they agree: plain label
    assert bot.kospi_ref(dt.date(2026, 9, 29), D("6824.90")) == (D("6824.90"), "09-29 收盘")      # not in the chart yet
    # the day's bar is looked for every minute after the close, every 10 minutes otherwise
    n = bot.kospi.daily_refreshed
    await bot.kospi.refresh_daily(kr(2026, 9, 29, 10, 5)); assert bot.kospi.daily_refreshed == n
    bot.kospi.daily_refreshed -= 61
    await bot.kospi.refresh_daily(kr(2026, 9, 29, 15, 50))
    assert bot.kospi.daily[dt.date(2026, 9, 29)] == D("6830.00")
    # Yahoo ^KS11 (what Predict settles on) comes before Naver's chart
    kbot = m.Bot(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT"}), m.Store(":memory:"), FakeMarket(), None)
    answers = {"finance.yahoo.com": yahoo([(dt.date(2026, 9, 28), 6911.5), (dt.date(2026, 9, 29), 6831.2)]), "fchart": kospi_chart}
    await kbot.kospi.refresh_daily(kr(2026, 9, 29, 16, 0))
    assert kbot.kospi.daily == {dt.date(2026, 9, 28): D("6911.50"), dt.date(2026, 9, 29): D("6831.20")}, kbot.kospi.daily
    # during the session the KOSPI card measures from the dated close
    k = m.IndexQuote("KOSPI", D("6824.90"), D("6889.74"), None, None, None, kr(2026, 9, 29, 10, 0), "Naver")
    bot.kospi.quote = k
    odds = bot.kospi_odds(kr(2026, 9, 29, 10, 0))
    assert odds.ref == D("6910.89") and "实时行情为 6,889.74" in odds.ref_note and odds.effective == D("6824.90"), odds

    # --- Korea rolls over at 15:33 KST (14:33 Beijing), not 15:30: the auction ends at a random moment, feeds lag ---
    bot.kospi.daily = {dt.date(2026, 9, 28): D("6910.89")}
    bot.kospi.quote = m.IndexQuote("KOSPI", D("6850.00"), D("6910.89"), None, None, None, kr(2026, 9, 29, 15, 32), "Naver")
    early = bot.kospi_odds(kr(2026, 9, 29, 15, 32))
    assert isinstance(early, m.CloseOdds) and early.target == dt.date(2026, 9, 29) and early.direct, early
    late = bot.kospi_odds(kr(2026, 9, 29, 15, 33))
    assert not (isinstance(late, m.CloseOdds) and late.target == dt.date(2026, 9, 29)), late  # moved on (or waiting for the proxy)
    # SK Hynix: its closing print is taken as the day's close only from 15:33
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "SKHYNIXUSDT"})
    hbot = m.Bot(cfg, m.Store(":memory:"), FakeMarket(), None)
    tk = cfg.tickers["SKHYNIXUSDT"]
    hbot.stocks.live["SKHYNIXUSDT"] = m.dataclasses.replace(
        m.IndexQuote("SK", D("1769000"), D("1768000"), None, None, None, kr(2026, 9, 29, 15, 30), "Naver"))
    hbot.note_live_close("SKHYNIXUSDT", tk, kr(2026, 9, 29, 15, 32))
    assert hbot.store.get("live_close:SKHYNIXUSDT") is None
    hbot.note_live_close("SKHYNIXUSDT", tk, kr(2026, 9, 29, 15, 33))
    assert hbot.store.get("live_close:SKHYNIXUSDT")[:2] == [kr(2026, 9, 29, 15, 30), "1769000"]
    assert hbot.store.get("krx_close:000660:2026-09-29") == ["1769000", "1768000"]
    hbot.stocks.live["SKHYNIXUSDT"] = m.IndexQuote("SK", D("1786000"), D("1768000"), None, None, None, kr(2026, 9, 29, 15, 41), "Naver")
    hbot.note_live_close("SKHYNIXUSDT", tk, kr(2026, 9, 29, 15, 42))
    assert hbot.store.get("live_close:SKHYNIXUSDT")[1] == "1769000"  # the 15:41 NXT print is not taken

asyncio.run(run())
print("KRX_OK")

# price-line labels: a close before today is 昨收, today's own close (after the session) 今收
bj = lambda y, mo, d, h: int(dt.datetime(y, mo, d, h, tzinfo=m.BEIJING).timestamp() * 1000)
assert m.ref_relative("09-28", dt.date(2026, 9, 29), bj(2026, 9, 29, 10)) == "昨收"
assert m.ref_relative("09-25", dt.date(2026, 9, 28), bj(2026, 9, 28, 10)) == "昨收"   # Friday's close on Monday
assert m.ref_relative("09-29", dt.date(2026, 9, 30), bj(2026, 9, 29, 17)) == "今收"
assert m.ref_relative("12-31", dt.date(2027, 1, 4), bj(2027, 1, 4, 10)) == "昨收"     # across the year end
assert m.ref_relative("", dt.date(2026, 9, 29), bj(2026, 9, 29, 10)) == "参考"
print("LABELS_OK")


# --- HSI: dated daily closes (Tencent, Eastmoney fallback) win over a realtime spot read before the final close ----------
async def hsi():
    bj2 = lambda d, h, mi: int(dt.datetime(2026, 9, d, h, mi, tzinfo=m.BEIJING).timestamp() * 1000)
    tencent = json.dumps({"data": {"hkHSI": {"day": [["2026-09-26", "24400", "24510.09", "0", "0", "1"],
                                                     ["2026-09-29", "24500", "24529.24", "0", "0", "1"],
                                                     ["2026-09-30", "24530", "24600.00", "0", "0", "1"]]}}}).encode()
    calls = []
    async def fake_get(url, timeout=15, headers=None):
        calls.append(url)
        if "gtimg" in url:
            if isinstance(state["tencent"], Exception): raise state["tencent"]
            return state["tencent"]
        if "eastmoney" in url:
            return json.dumps({"data": {"klines": ["2026-09-26,24400,24510.09", "2026-09-29,24500,24529.24"]}}).encode()
        raise AssertionError(url)
    m.http_get = fake_get
    state = {"tencent": tencent}
    feed = m.DailyCloses("hk", (("tencent", "https://web.ifzq.gtimg.cn/x"), ("eastmoney", "https://push2his.eastmoney.com/x")))
    await feed.refresh(bj2(30, 10, 0))   # 09-30 session running: its bar is not final
    assert feed.daily == {dt.date(2026, 9, 26): D("24510.09"), dt.date(2026, 9, 29): D("24529.24")} and feed.error == "", feed.daily
    ref, note = m.dated_ref(feed.daily, dt.date(2026, 9, 29), D("24523.57"))
    assert ref == D("24529.24") and note == "09-29 收盘（日K；实时行情为 24,523.57）", note
    # after 16:25 the day's bar is looked for every minute
    n = len(calls); await feed.refresh(bj2(30, 16, 26)); assert len(calls) == n  # within the minute
    feed.refreshed -= 61; await feed.refresh(bj2(30, 16, 26))
    assert feed.daily[dt.date(2026, 9, 30)] == D("24600.00")
    # Tencent down: Eastmoney
    state["tencent"] = m.RemoteError("网络错误 (URLError)")
    feed2 = m.DailyCloses("hk", feed.sources); await feed2.refresh(bj2(30, 10, 0))
    assert feed2.daily[dt.date(2026, 9, 29)] == D("24529.24")
    # a failed round keeps the closes already known
    feed.refreshed -= 601; await feed.refresh(bj2(30, 12, 0))
    assert feed.daily[dt.date(2026, 9, 30)] == D("24600.00"), feed.daily
    # Tencent answers but lags (no 09-30 bar) after the close: Eastmoney is asked for it, Tencent keeps its days
    state["tencent"] = json.dumps({"data": {"hkHSI": {"day": [["2026-09-29", "24500", "24529.24", "0", "0", "1"]]}}}).encode()
    em30 = json.dumps({"data": {"klines": ["2026-09-29,24500,24529.00", "2026-09-30,24530,24601.00"]}}).encode()
    lag = m.DailyCloses("hk", feed.sources)
    m.http_get = lambda url, timeout=15, headers=None: fake_get(url) if "gtimg" in url else asyncio.sleep(0, em30)
    await lag.refresh(bj2(30, 16, 30))
    assert lag.daily == {dt.date(2026, 9, 29): D("24529.24"), dt.date(2026, 9, 30): D("24601.00")}, lag.daily
    m.http_get = fake_get

    # the cash index: a failed spot round keeps the last value for a few minutes instead of blanking the card
    hf = m.IndexFutures()
    clock, spot_ok = {"ms": bj2(30, 11, 0)}, {"v": True}
    async def feeds(url, timeout=15, headers=None):
        if "134.HSI_M" in url:  # the futures (Eastmoney: no cash index with it)
            return json.dumps({"data": {"f43": 24600, "f60": 24500, "f86": clock["ms"] // 1000}}).encode()
        if "100.HSI" in url and spot_ok["v"]:
            return json.dumps({"data": {"f43": 24529.24, "f60": 24400, "f86": clock["ms"] // 1000}}).encode()
        raise m.RemoteError("网络错误 (TimeoutError)")
    m.http_get = feeds; m.SOURCE_HEALTH.hosts.clear()
    await hf.refresh(clock["ms"], force=True)
    assert hf.quote.source == "东方财富" and hf.quote.spot == D("24529.24") and hf.quote.spot_ms == clock["ms"], hf.quote
    spot_ok["v"] = False; clock["ms"] = bj2(30, 11, 1)
    await hf.refresh(clock["ms"], force=True)
    assert hf.quote.spot == D("24529.24") and hf.quote.spot_prev == D("24400") and "TimeoutError" in hf.spot_error, hf.quote
    assert hf.quote.spot_ms == bj2(30, 11, 0)  # kept with its own time, so it ages
    hf.spot_at -= hf.SPOT_KEEP_SECONDS + 1
    clock["ms"] = bj2(30, 11, 7)
    await hf.refresh(clock["ms"], force=True); assert hf.quote.spot is None  # too old: not passed off as current
    m.http_get = fake_get; m.SOURCE_HEALTH.hosts.clear()

    # anchors are per contract family: a Sina CFD night print is never divided by an HKEX-contract anchor
    cfd = m.FuturesQuote("恒指CFD", D("24650.5"), None, None, None, None, bj2(30, 22, 0), "新浪CFD", D("24600"),
                         exchange_contract=False, session="夜市")
    assert m.hsi_anchor_key(cfd) == "HSI:cfd" and m.hsi_anchor_key(hf.quote) == "HSI"

asyncio.run(hsi())
print("HSI_OK")
