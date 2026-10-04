"""Session exceptions: HKEX half days (12:10 close, futures 12:30, no night session) and the KRX CSAT day (one hour
late) are one calendar fact that every session helper follows; the holiday table warns before it runs out; a failed FX
round is retried soon; a close that is due is fetched every minute."""
import asyncio, sys, json, time, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
KST = dt.timezone(dt.timedelta(hours=9))


def bj(mo, d, h, mi, s=0, y=2026): return int(dt.datetime(y, mo, d, h, mi, s, tzinfo=m.BEIJING).timestamp() * 1000)
def kr(mo, d, h, mi, s=0, y=2026): return int(dt.datetime(y, mo, d, h, mi, s, tzinfo=KST).timestamp() * 1000)


# --- configuration: defaults, overrides, and the process-wide calendar they configure --------------------------------
cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x"})
assert dt.date(2026, 12, 24) in cfg.hk_half_days and dt.date(2026, 12, 31) in cfg.hk_half_days and dt.date(2026, 11, 19) in cfg.kr_late_days
assert m.CALENDAR.half_day("hk", dt.date(2026, 12, 24)) and m.CALENDAR.late_day("kr", dt.date(2026, 11, 19))
assert not m.CALENDAR.half_day("kr", dt.date(2026, 12, 24)) and not m.CALENDAR.late_day("hk", dt.date(2026, 11, 19))
assert dt.date(2026, 12, 25) in cfg.holidays["hk"] and dt.date(2027, 1, 1) in cfg.holidays["kr"] and dt.date(2026, 12, 31) in cfg.holidays["kr"]
assert dt.date(2026, 12, 28) not in cfg.holidays["hk"]  # Boxing Day 2026 is a Saturday: no weekday off
m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "KR_LATE_DAYS": "2026-11-20", "HK_HALF_DAYS": ""})
assert m.CALENDAR.late_day("kr", dt.date(2026, 11, 20)) and not m.CALENDAR.late_day("kr", dt.date(2026, 11, 19)) and not m.CALENDAR.hk_half
try: m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "HK_HALF_DAYS": "24/12/2026"}); assert False
except ValueError as e: assert "HK_HALF_DAYS" in str(e)
m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x"})  # defaults again

# --- close times and sessions per day ---------------------------------------------------------------------------------
csat, before, half = dt.date(2026, 11, 19), dt.date(2026, 11, 18), dt.date(2026, 12, 24)
assert m.CALENDAR.close_time("kr", csat) == dt.time(16, 30) and m.CALENDAR.close_time("kr", before) == dt.time(15, 30)
assert m.CALENDAR.close_time("hk", half) == dt.time(12, 10) and m.CALENDAR.close_time("hk", dt.date(2026, 12, 23)) == dt.time(16, 10)
assert m.CALENDAR.close_time("sh", half) == dt.time(15, 0)
assert m.CALENDAR.sessions("kr", csat) == ((dt.time(10, 0), dt.time(16, 30)),) and m.CALENDAR.sessions("hk", half) == ((dt.time(9, 30), dt.time(12, 0)),)
assert m.CALENDAR.kr_time(m.KRX_NXT_AFTER, csat) == dt.time(16, 40) and m.CALENDAR.kr_time(m.KRX_SETTLED, before) == m.KRX_SETTLED
assert m.CALENDAR.close_time_for(m.STOCK_MARKETS["kr"], csat) == dt.time(16, 30)
assert m.CALENDAR.note("hk", half) == "半日市" and m.CALENDAR.note("kr", csat) and m.CALENDAR.note("kr", before) == ""

# --- a daily bar is final 15 minutes after the day's real close, not the usual one ----------------------------------
bars = [(before, D("100")), (csat, D("101"))]
assert m.finished_bars(bars, "kr", kr(11, 19, 15, 50)) == [(before, D("100"))]       # 15:50 on the CSAT day: still trading
assert m.finished_bars(bars, "kr", kr(11, 19, 16, 44)) == [(before, D("100"))]
assert m.finished_bars(bars, "kr", kr(11, 19, 16, 45)) == bars
assert m.finished_bars([(before, D("100"))], "kr", kr(11, 18, 15, 46)) == [(before, D("100"))]  # an ordinary day: 15:45
assert m.last_completed_bar(bars, m.STOCK_MARKETS["kr"], kr(11, 19, 16, 0))[0] == before
assert m.last_completed_bar(bars, m.STOCK_MARKETS["kr"], kr(11, 19, 16, 46))[0] == csat
hk_bars = [(dt.date(2026, 12, 23), D("40")), (half, D("41"))]
assert m.finished_bars(hk_bars, "hk", bj(12, 24, 12, 26)) == hk_bars and m.finished_bars(hk_bars, "hk", bj(12, 24, 12, 20)) == hk_bars[:1]
assert m.expected_close_date("hk", bj(12, 24, 12, 30), cfg.holidays["hk"]) == half
assert m.expected_close_date("hk", bj(12, 24, 12, 20), cfg.holidays["hk"]) == dt.date(2026, 12, 23)
assert m.expected_close_date("kr", kr(11, 19, 15, 50), cfg.holidays["kr"]) == before
assert m.expected_close_date("kr", kr(11, 19, 16, 45), cfg.holidays["kr"]) == csat

# --- the realtime window and the session state follow the day's hours -----------------------------------------------
assert m.stock_live_window("kr", kr(11, 19, 9, 30)) is None and m.stock_live_window("kr", kr(11, 19, 10, 0))
assert m.stock_live_window("kr", kr(11, 19, 16, 40)) and m.stock_live_window("kr", kr(11, 19, 16, 46)) is None
assert m.stock_live_window("hk", bj(12, 24, 12, 20)) and m.stock_live_window("hk", bj(12, 24, 12, 30)) is None
assert m.session_state("kr", kr(11, 19, 9, 30)) == "未开盘" and m.session_state("kr", kr(11, 19, 16, 0)) == "开盘中"
assert m.session_state("hk", bj(12, 24, 13, 30)) == "已收盘" and m.session_state("hk", bj(12, 23, 13, 30)) == "开盘中"
assert m.krx_session(kr(11, 19, 16, 0)) == "交易中" and m.krx_session(kr(11, 18, 16, 0)) == "已收盘"
assert m.krx_session(kr(11, 21, 10, 0)) == "休市" and m.krx_session(kr(11, 20, 10, 0), frozenset({dt.date(2026, 11, 20)})) == "休市"

# --- HSI futures: no night session after a half day, the day session ends 12:30 -----------------------------------
hol = cfg.holidays["hk"]
assert m.hk_futures_session(bj(12, 24, 12, 0), hol) == "日市" and m.hk_futures_session(bj(12, 24, 12, 31), hol) == "休市"
assert m.hk_futures_session(bj(12, 24, 18, 0), hol) == "休市" and m.hk_futures_session(bj(12, 25, 2, 0), hol) == "休市"
assert m.hk_futures_session(bj(12, 23, 18, 0), hol) == "夜市" and m.hk_futures_session(bj(12, 24, 2, 0), hol) == "夜市"
assert m.hk_session_end("日市", bj(12, 24, 13, 0), hol) == bj(12, 24, 12, 30)
assert m.hk_session_end("夜市", bj(12, 24, 18, 0), hol) == bj(12, 24, 3, 0)  # the latest night that happened: 12-23's
assert m.hk_cash_close_date(bj(12, 24, 12, 15), hol) == half and m.hk_cash_close_date(bj(12, 24, 12, 5), hol) == dt.date(2026, 12, 23)
assert m.hk_cash_close_date(bj(12, 28, 10, 0), hol) == half  # 12-25 holiday, weekend
fut = m.IndexFutures(True, hol)
assert fut.cash_open(bj(12, 24, 12, 5)) and not fut.cash_open(bj(12, 24, 12, 10)) and fut.cash_open(bj(12, 23, 15, 0))

# --- probability horizon: a half day has little session left; after 12:25 the next close is the next trading day ---
share, target = m.session_remaining("hk", bj(12, 24, 11, 0), None, hol)
assert target == half and abs(share - 60 / 330) < 1e-9, (share, target)
share, target = m.session_remaining("hk", bj(12, 24, 12, 30), None, hol)
assert target == dt.date(2026, 12, 28) and share == 1.5, (share, target)  # 12-25 is a holiday, then the weekend
share, target = m.session_remaining("kr", kr(11, 19, 16, 0), None, cfg.holidays["kr"])
assert target == csat and abs(share - 30 / 390) < 1e-9, (share, target)

# --- closing auctions: 12:00–12:10 on a half day, 16:20–16:30 KST on the CSAT day -----------------------------------
assert m.auction_window("hk", bj(12, 24, 12, 5))[:2] == (dt.time(12, 0), dt.time(12, 10)) and "半日市" in m.auction_window("hk", bj(12, 24, 12, 5))[2]
assert m.auction_running("hk", bj(12, 24, 12, 5), hol) and not m.auction_running("hk", bj(12, 24, 16, 5), hol)
assert m.auction_window("kr", bj(11, 19, 15, 25))[:2] == (dt.time(15, 20), dt.time(15, 30))
assert m.auction_running("kr", bj(11, 19, 15, 25)) and not m.auction_running("kr", bj(11, 19, 14, 25))
assert m.auction_running("kr", bj(11, 18, 14, 25)) and m.auction_window("sh", bj(11, 19, 14, 58)) == m.AUCTIONS["sh"]

# --- the holiday table warns before it runs out ---------------------------------------------------------------------
assert m.calendar_warning(cfg.holidays, dt.date(2026, 10, 4)) == ""
assert "2027-01-01" in m.calendar_warning(cfg.holidays, dt.date(2026, 12, 10)) and "HOLIDAYS_CN/HK/KR" in m.calendar_warning(cfg.holidays, dt.date(2026, 12, 10))
assert "已过期" in m.calendar_warning(cfg.holidays, dt.date(2027, 1, 5))
assert "未配置" in m.calendar_warning({"sh": frozenset()}, dt.date(2026, 10, 4))
assert m.calendar_until({"hk": frozenset({dt.date(2027, 1, 1)}), "kr": frozenset({dt.date(2026, 12, 25)})}) == dt.date(2026, 12, 25)


def naver(close, prev_change, at):
    return json.dumps({"datas": [{"closePrice": close, "compareToPreviousClosePrice": prev_change, "compareToPreviousPrice": {"code": "2"},
                                  "fluctuationsRatio": "1.0", "marketStatus": "OPEN", "localTradedAt": at}]}).encode()


async def run():
    # --- the CSAT day: a 15:33 KST print is intraday, not the KRX close; nothing is persisted as the close ------------
    kcfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "SKHYNIXUSDT", "EXCHANGE_TICKERS": "SKHYNIXUSDT=kr:000660"})
    store = m.Store(":memory:"); stocks = m.StockMarket(kcfg, store)
    ticker = kcfg.tickers["SKHYNIXUSDT"]
    quote = {"raw": naver("1,900,000", "20,000", "2026-11-19T15:33:00+09:00")}
    async def fake_fetch(url, extra=None, record=True): return quote["raw"]
    saved_fetch = m.fetch_source; m.fetch_source = fake_fetch
    try:
        got = await stocks.krx_official(ticker, before, D("1880000"), D("1870000"), kr(11, 19, 15, 36), "Yahoo")
        assert got[0] == before and got[1] == D("1880000") and store.get("krx_close:000660:2026-11-19") is None, got
        # 16:33 KST on the CSAT day: now it is the KRX close
        quote["raw"] = naver("1,900,000", "20,000", "2026-11-19T16:33:00+09:00")
        got = await stocks.krx_official(ticker, before, D("1880000"), D("1870000"), kr(11, 19, 16, 36), "Yahoo")
        assert got[0] == csat and got[1] == D("1900000") and store.get("krx_close:000660:2026-11-19") == ["1900000", "1880000"], got
        # an ordinary day keeps the old rule: a 15:33 print after 15:33 is the close
        quote["raw"] = naver("1,850,000", "10,000", "2026-11-18T15:33:00+09:00")
        got = await stocks.krx_official(ticker, dt.date(2026, 11, 17), D("1840000"), None, kr(11, 18, 15, 36), "Yahoo")
        assert got[0] == before and got[1] == D("1850000"), got
        # Naver unavailable: the chart's close stands and /diag learns why it was not corrected
        async def broken(url, extra=None, record=True): raise m.RemoteError("网络错误 (TimeoutError)")
        m.fetch_source = broken
        got = await stocks.krx_official(ticker, before, D("1880000"), None, kr(11, 19, 17, 0), "Yahoo")
        assert got[:2] == (before, D("1880000")) and "KRX" in stocks.notes["000660"] and "TimeoutError" in stocks.notes["000660"]
    finally:
        m.fetch_source = saved_fetch
    # --- the exchange close baseline of a special day carries its real close time -------------------------------------
    base = m.StockMarket.baseline(ticker, m.STOCK_MARKETS["kr"], "Yahoo", csat, D("1900000"))
    assert base.close_ms == kr(11, 19, 16, 30) and "16:30" in base.close_text, base
    hk = m.StockMarket.baseline(m.StockTicker("hk", "00625", True), m.STOCK_MARKETS["hk"], "腾讯", half, D("41"))
    assert hk.close_ms == bj(12, 24, 12, 10)

    # --- a due close is fetched every minute (for two hours), otherwise every ten ---------------------------------------
    assert not stocks.close_pending(kr(11, 19, 16, 40)) and stocks.close_pending(kr(11, 19, 16, 50))
    assert not stocks.close_pending(kr(11, 19, 19, 0))  # two hours overdue: back to the normal cadence
    stocks.closes["SKHYNIXUSDT"] = base
    assert not stocks.close_pending(kr(11, 19, 16, 50))

    # --- a failed FX round is retried after RETRY_SECONDS, not six hours later -----------------------------------------
    async def no_json(url, payload=None, timeout=15): raise m.RemoteError("网络错误 (TimeoutError)")
    saved_json = m.http_json; m.http_json = no_json
    try:
        fx = m.FxRates()
        result = await fx.refresh()
        assert isinstance(result, m.Refreshed) and result.status == "failed" and fx.error
        assert await fx.refresh() is False  # not due yet
        assert abs((time.monotonic() - fx.refreshed) - (fx.REFRESH_SECONDS - fx.RETRY_SECONDS)) < 2
    finally:
        m.http_json = saved_json

    # --- a holiday evening is not a "waiting for the bar" evening for the dated closes -----------------------------------
    feed = m.DailyCloses("hk", (("tencent", "https://web.ifzq.gtimg.cn/x"),), hol)
    calls = []
    async def counting(url, extra=None, record=True):
        calls.append(url); raise m.RemoteError("offline")
    m.fetch_source = counting
    try:
        await feed.refresh(bj(12, 25, 16, 30)); await feed.refresh(bj(12, 25, 16, 31) + 61_000)
        assert len(calls) == 1, calls  # 12-25 is a holiday: the 60-second cadence does not apply
    finally:
        m.fetch_source = saved_fetch
    print("CALENDAR_OK")


asyncio.run(run())
