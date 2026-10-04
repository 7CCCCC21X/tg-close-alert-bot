"""STRC 触及 $100: a US stock's "hits $X by <date>" market (STOCK_HIT_MARKETS) priced from Yahoo's chart feed in trading time."""
import asyncio, json, math, sys, time, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
STRC, = m.STOCK_HIT_MARKETS
utc = lambda *a: int(dt.datetime(*a, tzinfo=dt.timezone.utc).timestamp() * 1000)
et = lambda y, mo, d, h, mi, off=-4: m.et_ms(y, mo, d, h, mi, off)
RULES = ('This market will resolve to "Yes" if any TradingView 1 minute candle for STRC between market creation and the listed '
         'date, 11:59 PM ET, has a final “High” value of at least $100. Otherwise, this market will resolve to "No."\n\n'
         "The resolution source for this market is TradingView, specifically the STRC “High” values available at "
         "https://www.tradingview.com/chart/?symbol=NASDAQ%3ASTRC, with the chart settings on \"1m\" for one-minute candles.")

# --- the spec: the window comes from Predict, the level from the title, the direction from the rules ------------------
assert STRC.key == "STRC-100" and STRC.slug == "strc-hits-100-by-20260618001620693" and STRC.symbol == "STRC" and STRC.venue == "nasdaq"
assert STRC.name == "STRC 触及 $100" and STRC.start_ms == 0 and STRC.end_ms == 0 and STRC.default_dir == "up"
assert m.price_level("STRC hits $100 by December 31?") == D("100") and m.price_level("STRC 达到 $100，截止于 12 月 31 日") == D("100")
assert m.level_direction(RULES) == ("up", "规则") and m.level_direction(RULES, "STRC hits $100 by December 31?") == ("up", "规则")

# --- US Eastern dates and sessions --------------------------------------------------------------------------------------
assert m.et_date(utc(2026, 10, 6, 3, 59)) == dt.date(2026, 10, 5) and m.et_date(utc(2026, 10, 6, 4, 0)) == dt.date(2026, 10, 6)  # EDT
assert m.et_date(utc(2026, 12, 1, 4, 59)) == dt.date(2026, 11, 30) and m.et_date(utc(2026, 12, 1, 5, 0)) == dt.date(2026, 12, 1)  # EST
assert m.et_wall_ms(dt.date(2026, 10, 6), 9, 30) == utc(2026, 10, 6, 13, 30) and m.et_wall_ms(dt.date(2026, 12, 31), 23, 59) == utc(2027, 1, 1, 4, 59)
assert m.et_wall_ms(dt.date(2026, 11, 1), 16, 0) == utc(2026, 11, 1, 21, 0)  # the day the clocks go back: 16:00 EST
assert m.us_session(dt.date(2026, 10, 6)) == (utc(2026, 10, 6, 13, 30), utc(2026, 10, 6, 20, 0))
assert m.us_session(dt.date(2026, 11, 27)) == (utc(2026, 11, 27, 14, 30), utc(2026, 11, 27, 18, 0))  # early close 13:00 EST
assert m.us_session(dt.date(2026, 11, 26)) is None and m.us_session(dt.date(2026, 10, 10)) is None and m.us_session(dt.date(2027, 7, 5)) is None
assert m.us_session_state(et(2026, 10, 6, 14, 0)) == ("交易中", 0)
assert m.us_session_state(et(2026, 10, 9, 17, 0)) == ("已收盘", et(2026, 10, 12, 9, 30))  # Friday evening: Monday (NYSE trades Columbus Day)
assert m.us_session_state(et(2026, 11, 25, 16, 30, -5)) == ("已收盘", et(2026, 11, 27, 9, 30, -5))   # Thanksgiving in between
assert m.us_session_state(et(2026, 10, 6, 9, 0)) == ("已收盘", et(2026, 10, 6, 9, 30))
# trading time left: the rest of this session, then whole sessions (an early close is its share) through the deadline's date
y = lambda n: n / 252
end_day = lambda y_, mo, d, off=-4: m.et_ms(y_, mo, d, 23, 59, off) + 60_000
assert abs(m.us_trading_years(et(2026, 10, 6, 14, 0), end_day(2026, 10, 6)) - y(2 / 6.5)) < 1e-12
assert abs(m.us_trading_years(et(2026, 10, 6, 14, 0), end_day(2026, 10, 7)) - y(2 / 6.5 + 1)) < 1e-12
assert abs(m.us_trading_years(et(2026, 11, 23, 0, 0, -5), end_day(2026, 11, 27, -5)) - y(3 + 3.5 / 6.5)) < 1e-12
assert m.us_trading_years(et(2026, 10, 6, 17, 0), end_day(2026, 10, 6)) == 0.0 and m.us_trading_years(10, 5) == 0.0
assert abs(m.us_trading_years(et(2026, 10, 10, 12, 0), end_day(2026, 10, 13)) - y(2)) < 1e-12  # a weekend: Monday and Tuesday count

# --- the deadline a title names ------------------------------------------------------------------------------------------
created = utc(2026, 6, 18, 0, 16, 20)
dec31 = m.et_wall_ms(dt.date(2026, 12, 31), 23, 59)
for text in ("STRC hits $100 by December 31?", "STRC hits $100 by Dec. 31st, 2026", "Will STRC hit $100 before December 31, 2026?",
             "STRC $100 by end of December", "STRC hits $100 by December", "STRC 达到 $100，截止于 12 月 31 日", "截止 2026-12-31",
             "2026年12月31日前 STRC 触及 $100"):
    assert m.deadline_from_text([text], created) == dec31, text
assert m.deadline_from_text(["STRC hits $100 by March 15?"], created) == m.et_wall_ms(dt.date(2027, 3, 15), 23, 59)  # a date already past: next year
assert m.deadline_from_text(["", "STRC hits $100", RULES], created) == 0  # "the listed date" names none
assert m.deadline_from_text(["STRC hits $100", "STRC hits $100 by October 31?"], created) == m.et_wall_ms(dt.date(2026, 10, 31), 23, 59)
assert m.deadline_from_text(["by February 30"], created) == 0  # not a date
assert m.deadline_from_text(["x by 2026-11-15"], 0) == m.et_wall_ms(dt.date(2026, 11, 15), 23, 59)
assert m.parse_deadlines("STRC-100=2026-12-31; other=2027-01-15") == {"STRC-100": dec31, "OTHER": m.et_wall_ms(dt.date(2027, 1, 15), 23, 59)}
assert m.parse_deadlines("") == {}
for bad in ("STRC-100", "STRC-100=soon", "=2026-12-31"):
    try: m.parse_deadlines(bad); assert False, bad
    except ValueError: pass
cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "LADDER_DEADLINES": "STRC-100=2026-11-30"})
assert cfg.ladder_deadlines == {"STRC-100": m.et_wall_ms(dt.date(2026, 11, 30), 23, 59)}

# --- Yahoo's chart feed ---------------------------------------------------------------------------------------------------
NOW = et(2026, 10, 6, 14, 0)  # Tuesday 14:00 EDT, the session running
TODAY = dt.date(2026, 10, 6)


def chart(stamps, rows, price=None, when=None, gmtoffset=-14400):
    """A Yahoo v8 chart body: rows = (open, high, low, close) per stamp (None = a gap)."""
    col = lambda i: [None if r is None else r[i] for r in rows]
    meta = {"currency": "USD", "symbol": "STRC", "gmtoffset": gmtoffset, "regularMarketPrice": price, "regularMarketTime": when}
    return json.dumps({"chart": {"result": [{"meta": meta, "timestamp": stamps,
                                             "indicators": {"quote": [{"open": col(0), "high": col(1), "low": col(2), "close": col(3), "volume": [1] * len(rows)}]}}],
                                 "error": None}}).encode()


def minutes(day, highs, last_high=None):
    """1-minute bars of a session from 09:30 ET: closes 99.5, highs as given (the running minute last)."""
    o = m.us_session(day)[0] // 1000
    return [o + 60 * i for i in range(len(highs))], [(99.5, h, 99.3, 99.5) for h in highs]


def days(first, last, highs=None):
    """Daily bars (one per session) with closes alternating ±0.3% around 99.5 and the given highs by date."""
    stamps, rows, day, i = [], [], first, 0
    while day <= last:
        if m.us_session(day):
            close = 99.5 * (1.003 if i % 2 else 1.0)
            high = (highs or {}).get(day, close * 1.001)
            stamps.append(m.us_session(day)[0] // 1000); rows.append((close * 0.999, high, close * 0.996, close)); i += 1
        day += dt.timedelta(days=1)
    return stamps, rows


meta, bars = m.parse_yahoo_chart(chart(*minutes(TODAY, [99.6, 99.8, 99.7]), price=99.6, when=NOW // 1000 - 30))
assert meta == {"price": 99.6, "time_ms": NOW - 30_000, "offset": -14400} and len(bars) == 3 and bars[1] == (et(2026, 10, 6, 9, 31) , 99.5, 99.8, 99.3, 99.5)
stamps, rows = minutes(TODAY, [99.6, 99.8, 99.7]); rows[1] = None
assert [b[0] for b in m.parse_yahoo_chart(chart(stamps, rows))[1]] == [stamps[0] * 1000, stamps[2] * 1000]  # a gap is dropped
for raw in (b"{}", b"not json", json.dumps({"chart": {"result": None, "error": {"code": "Not Found", "description": "No data found, symbol may be delisted"}}}).encode()):
    try: m.parse_yahoo_chart(raw); assert False, raw
    except ValueError as error: assert "Yahoo" in str(error) or "delisted" in str(error), error


async def run():
    # --- the market: window from Predict, price and today's highs from 1-minute bars, σ and the finished days from daily bars
    store = m.Store(":memory:")
    rm = m.StockRangeMarket(store, STRC)
    assert not rm.known() and rm.start_ms == 0 and rm.end_ms == 0 and rm.probability(D("100"), "up", NOW) is None
    assert rm.advice_problem(NOW) == "等待 Predict 的创建时间和截止日期；暂不给建议" and rm.missing_note(NOW) == "等待 Predict 的创建时间和截止日期"
    row = m.LadderRow(D("100"), "41", "STRC hits $100 by December 31?", None, "", "Will STRC hit $100 by December 31?")
    meta41 = {"created_ms": created, "question": row.question, "rules": RULES, "outcomes": ["Yes", "No"], "status": "OPEN"}
    rm.learn([row], {"41": (meta41, time.monotonic())})
    assert rm.known() and rm.start_ms == created and rm.end_ms == dec31 and rm.window_end == dec31 + 60_000 and rm.title == row.title
    assert store.get(f"range:{STRC.slug}:window") == {"start": created, "end": dec31, "title": row.title}
    assert m.StockRangeMarket(store, STRC).end_ms == dec31  # a restart prices at once
    assert m.StockRangeMarket(store, STRC, deadline_override=99).end_ms == 99  # LADDER_DEADLINES wins
    assert rm.missing_note(NOW) == "等待 Yahoo 行情" and rm.remaining_years(NOW) == m.us_trading_years(NOW, dec31 + 60_000)

    calls = []
    answers = {"1m": chart(*minutes(TODAY, [99.6, 99.8, 99.7]), price=99.6, when=NOW // 1000 - 30),
               "1d": chart(*days(dt.date(2025, 10, 6), TODAY, {dt.date(2026, 7, 10): 99.95, dt.date(2026, 6, 10): 100.5}))}  # 06-10 is before the window
    async def fake_source(url, extra=None):
        calls.append(url)
        if "query1" in url and "fail1" in answers:
            raise m.RemoteError("HTTP 429: 接口限流，等待后重试")
        return answers["1m" if "interval=1m" in url else "1d"]
    real = m.fetch_source; m.fetch_source = fake_source
    try:
        await rm.refresh(NOW)
        assert rm.error == "" and len(calls) == 2 and calls[0].startswith("https://query1.finance.yahoo.com/v8/finance/chart/STRC?interval=1m&range=1d&includePrePost=false")
        assert "interval=1d&range=1y" in calls[1]
        assert rm.price == D("99.6000") and rm.priced_ms == NOW - 30_000 and rm.fetched_ms == NOW
        assert rm.running == {"open": et(2026, 10, 6, 9, 30), "high": 99.8, "low": 99.3}
        hist = rm.history
        assert hist["start"] == created and hist["high"] == 99.95 and m.et_date(hist["high_at"]) == dt.date(2026, 7, 10), hist
        assert hist["through"] == m.et_wall_ms(TODAY, 0, 0) and hist["open"] is not None and rm.scanned_ms == NOW
        assert rm.marks()["high"] == 99.95 and rm.extremes()[0] == 99.95 and not rm.reached(D("100"), "up")
        closes = [D(str(r[3])) for s, r in zip(*days(dt.date(2025, 10, 6), TODAY)) if m.et_date(s * 1000) < TODAY][-61:]
        assert abs(rm.sigma - m.realised_vol(closes)[0] * math.sqrt(252)) < 1e-12 and 0.03 < rm.sigma < 0.08, rm.sigma
        assert rm.sigma_ms == NOW and rm.advice_problem(NOW) == "" and rm.missing_note(NOW) == ""
        fair = rm.probability(D("100"), "up", NOW)
        # the running session's remainder counts only the in-session share of the daily variance (its opening gap is behind it)
        share, days_ = rm.share
        assert 0.3 <= share <= 1 and days_ >= 10 and rm.remaining_years(NOW) == m.us_trading_years(NOW, dec31 + 60_000, share), rm.share
        assert rm.remaining_years(NOW) < m.us_trading_years(NOW, dec31 + 60_000) and rm.remaining_years(NOW) > m.us_trading_years(NOW, dec31 + 60_000, 0.0)
        assert fair == m.hit_probability(99.6, 100.0, rm.sigma, rm.remaining_years(NOW)) and 0.5 < fair < 1, fair
        assert rm.model_swing(D("100"), "up", NOW, fair) > 0
        # the live price is the last trade: valid while the market is closed, stale only when Yahoo stops answering
        assert not rm.price_stale(NOW + 4 * 60_000) and rm.price_stale(NOW + 5 * 60_000 + 1)
        sat = et(2026, 10, 10, 12, 0)
        rm.fetched_ms = sat
        assert rm.probability(D("100"), "up", sat) == m.hit_probability(99.6, 100.0, rm.sigma, m.us_trading_years(sat, dec31 + 60_000))
        assert rm.missing_note(sat + 6 * 60_000).startswith("Yahoo 行情停在 ") and rm.probability(D("100"), "up", sat + 6 * 60_000) is None
        rm.fetched_ms = NOW
        extras = rm.card_extras(NOW)
        assert extras["range_word"] == "窗口内" and "TradingView" in extras["rule_note"]
        assert extras["session"] == f"美股交易中（常规时段 09:30–16:00 ET）；今日剩余时段按 {share * 100:.0f}% 方差计（{days_} 日开盘跳空已扣除）", extras["session"]
        assert rm.card_extras(sat)["session"] == "美股已收盘，10-12 09:30 ET（北京 10-12 21:30）开盘；收盘期间现价为最后成交价，概率按剩余交易时段计算"
        assert rm.close_label() == "12-31 23:59 ET（北京 01-01 12:59）前触及即 Yes，到期未触及为 No（TradingView 1 分钟 K 的最高价 ≥ 档位算触及）"
        assert rm.window_label() == "市场创建 06-17 20:16 ET（北京 06-18 08:16）起" and rm.sigma_note() == "60 个交易日收盘，按 252 个交易日年化"
        assert rm.source_name() == "Yahoo 行情（NASDAQ:STRC）" and "TradingView" in rm.extremes_source()
        # the running session reaches the level: Yes at once (the finished days are persisted, today counts live)
        answers["1m"] = chart(*minutes(TODAY, [99.6, 99.8, 100.02]), price=99.9, when=NOW // 1000)
        rm.times["price"] = -1e9
        await rm.refresh(NOW + 60_000)
        assert rm.reached(D("100"), "up") and rm.probability(D("100"), "up", NOW + 60_000) == 1.0 and rm.marks()["high"] == 100.02
        # query1 down: query2 answers (the host cooldown keeps the failing one behind)
        answers["fail1"] = True; calls.clear()
        rm.times["price"] = -1e9
        await rm.refresh(NOW + 120_000)
        assert rm.error == "" and [u[:30] for u in calls] == ["https://query1.finance.yahoo.c", "https://query2.finance.yahoo.c"], calls
        del answers["fail1"]
        # no daily bars: σ is the failure, the price still reads; it is asked again in a minute
        answers["1d"] = chart([], [])
        fresh = m.StockRangeMarket(store, STRC)
        await fresh.refresh(NOW)
        assert fresh.price == D("99.9000") and fresh.sigma is None and fresh.error.startswith("日 K：") and "不够估 σ" in fresh.error, fresh.error
        assert abs(fresh.times["scan"] - (time.monotonic() - fresh.SCAN_SECONDS + 60)) < 5 and fresh.missing_note(NOW).startswith("等待 Yahoo 行情（日 K：")
        # after the deadline: the last session read closes the window; an open level is 0 once it is
        answers["1d"] = chart(*days(dt.date(2026, 1, 5), dt.date(2026, 12, 31)))
        answers["1m"] = chart(*minutes(dt.date(2026, 12, 31), [99.6]), price=99.6, when=m.us_session(dt.date(2026, 12, 31))[1] // 1000)
        later = m.StockRangeMarket(m.Store(":memory:"), STRC); later.learn([row], {"41": (meta41, time.monotonic())})
        after = m.et_ms(2027, 1, 2, 12, 0, -5)
        await later.refresh(after)
        assert later.complete() and later.history["through"] == later.window_end and later.probability(D("100"), "up", after) == 0.0
        assert later.advice_problem(after) == "窗口已结束，等待结算"
    finally:
        m.fetch_source = real

    # --- the bot: Predict lists the market, the card prices it in the 价格阶梯 section, the paper trader sees it ----------
    class FakeMarket:
        config = None
        def now_ms(self): return NOW
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off"})
    bot = m.Bot(cfg, m.Store(":memory:"), FakeMarket(), None)
    srm = bot.ranges["STRC-100"]
    assert isinstance(srm, m.StockRangeMarket) and bot.predict_targets(NOW)["STRC-100"] == STRC.slug and "STRC-100" in bot.predict.ladder_keys
    parse = bot.predict.ladder_parse["STRC-100"]
    assert parse is not m.price_level and bot.predict.ladder_pick["STRC-100"] == srm.choose and "STRC-100" in bot.predict.reward_keys
    # Predict titles the category's markets by their deadline alone: a bare day number is no price; "$100" anywhere is
    assert [parse(x) for x in ("December 31", "June 30", "STRC hits $100 by December 31?", "STRC 达到 $100，截止于 12 月 31 日", "$1.5k by Dec 31", "")] == \
        [D("100"), D("100"), D("100"), D("100"), D("1500"), D("100")]
    assert m.hit_level_parser("some-other-slug")("December 31") is None and m.hit_level_parser("strc-hits-100-by-x")("$100") == D("100")
    # of the markets listed (one per deadline), the card keeps the latest still ahead; a pinned deadline wins when listed
    dated = [m.LadderRow(D("100"), mid, title, None, "", f"Will STRC hit $100 by {title}?") for mid, title in
             (("61", "June 30"), ("62", "September 30"), ("63", "December 31"), ("64", "March 31"))]
    metas = {mid: ({"created_ms": m.et_ms(2026, 6, 18, 0, 16, -4), "question": "", "rules": ""}, 0) for mid in ("61", "62", "63", "64")}
    assert [r.market_id for r in srm.choose(dated, metas)] == ["64"]  # 2027-03-31: the latest ahead
    assert [r.market_id for r in srm.choose(dated[:3], metas)] == ["63"] and [r.market_id for r in srm.choose(dated[:1], metas)] == ["61"]
    nov = m.StockRangeMarket(m.Store(":memory:"), STRC, m.et_wall_ms(dt.date(2026, 11, 30), 23, 59))
    assert [r.market_id for r in nov.choose(dated, metas)] == ["64"]  # 11-30 is not listed: the latest ahead
    dec = m.StockRangeMarket(m.Store(":memory:"), STRC, m.et_wall_ms(dt.date(2026, 12, 31), 23, 59))
    assert [r.market_id for r in dec.choose(dated, metas)] == ["63"]
    pinned = m.Bot(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "LADDER_DEADLINES": "STRC-100=2026-11-30"}), m.Store(":memory:"), FakeMarket(), None)
    assert pinned.ranges["STRC-100"].end_ms == m.et_wall_ms(dt.date(2026, 11, 30), 23, 59)

    async def fetch(url, payload=None):
        if url == m.PREDICT_GRAPHQL:
            v = payload["variables"]
            if "id" in v:
                return {"data": {"category": {"id": "9", "__typename": "SingleCategory"} if v["id"] == STRC.slug else None}}
            return {"data": {"markets": {"edges": [{"node": {"id": "41", "conditionId": "0x41", "title": row.title, "question": row.question}}]}}}
        if url.endswith("/markets/41"):
            return {"data": {"id": "41", "status": "OPEN", "title": row.title, "question": row.question, "description": RULES,
                             "createdAt": "2026-06-18T00:16:20.693Z", "tradingStatus": "OPEN", "feeRateBps": 200,
                             "outcomes": [{"name": "Yes", "indexSet": 1}, {"name": "No", "indexSet": 2}]}}
        if url.endswith("/markets/41/orderbook"):
            return {"data": {"bids": [["0.55", "400"]], "asks": [["0.60", "400"]]}}
        if "/categories/" in url:
            raise m.RemoteError("HTTP 404: 接口请求失败")
        raise AssertionError(url)
    bot.predict.fetch = fetch
    await bot.predict.refresh({"STRC-100": STRC.slug}, force=True)
    rows = bot.predict.ladders["STRC-100"]
    assert [(r.target, r.market_id, r.title) for r in rows] == [(D("100"), "41", row.title)] and "STRC-100" not in bot.predict.errors
    assert bot.range_level(srm, rows[0]) == ("up", "规则")
    waiting = bot.range_payload(srm, NOW)  # before refresh_ranges: the window is not learnt yet
    assert waiting["missing"] == "等待 Predict 的创建时间和截止日期" and waiting["close_ms"] == 0 and waiting["ladder"]["hold"].startswith("等待 Predict")
    assert waiting["ladder"]["question"].startswith("STRC 在截止日（待 Predict 确认）之前是否触及 "), waiting["ladder"]["question"]
    answers = {"1m": chart(*minutes(TODAY, [99.6, 99.8, 99.7]), price=99.6, when=NOW // 1000 - 30),
               "1d": chart(*days(dt.date(2025, 10, 6), TODAY, {dt.date(2026, 7, 10): 99.95}))}
    async def fake_source(url, extra=None):
        return answers["1m" if "interval=1m" in url else "1d"]
    m.fetch_source = fake_source
    try:
        result = await bot.refresh_ranges(NOW)
    finally:
        m.fetch_source = real
    assert srm.known() and srm.start_ms == created + 693 and srm.end_ms == dec31 and srm.price == D("99.6000")  # createdAt keeps its milliseconds
    bot.predict.ladders["STRC-100"] = [m.dataclasses.replace(r, book=m.dataclasses.replace(r.book, fetched_ms=NOW)) for r in rows]
    item = bot.range_payload(srm, NOW)
    assert {k: item[k] for k in ("name", "symbol", "group", "kind", "close_ms", "quote_ms", "source")} == {
        "name": "STRC 触及 $100", "symbol": "STRC-100", "group": "levels", "kind": "ladder", "close_ms": dec31, "quote_ms": NOW - 30_000,
        "source": "Yahoo 行情（NASDAQ:STRC）"} and "missing" not in item, item
    assert item["close_label"] == "12-31 23:59 ET（北京 01-01 12:59）前触及即 Yes，到期未触及为 No（TradingView 1 分钟 K 的最高价 ≥ 档位算触及）"
    assert item["predict"]["url"].startswith(f"https://predict.fun/zh-cn/market/{STRC.slug}")
    L = item["ladder"]
    assert {k: L[k] for k in ("kind", "price", "high", "low", "symbol", "venue", "window", "hold", "error", "range_word", "sigma_note")} == {
        "kind": "price", "price": "$99.60", "high": "$99.95", "low": f"${min(99.5 * 0.996, 99.3):,.2f}", "symbol": "STRC", "venue": "Yahoo 行情，NASDAQ:",
        "window": "市场创建 06-17 20:16 ET（北京 06-18 08:16）起", "hold": "", "error": "", "range_word": "窗口内",
        "sigma_note": "60 个交易日收盘，按 252 个交易日年化"}, L
    assert L["session"].startswith("美股交易中（常规时段 09:30–16:00 ET）；今日剩余时段按 ") and "TradingView" in L["rule_note"] and "创建当日按整日计" in L["extremes_note"]
    assert L["question"] == "STRC 在 12-31 23:59 ET（北京 01-01 12:59）之前是否触及 $100：窗口内任一 1 分钟 K 的最高价达到即 Yes，到期没碰到为 No", L["question"]
    assert L["spot"] == 99.6 and abs(L["years"] - srm.remaining_years(NOW)) < 1e-12 and L["sigma"] == srm.sigma and srm.share
    r100, = L["rows"]
    assert r100["label"] == "↑ $100" and (r100["dir"], r100["dir_source"]) == ("up", "规则") and r100["dir_note"] == ""
    assert r100["fair"] == srm.probability(D("100"), "up", NOW) and abs(r100["dist"] - (100 / 99.6 - 1)) < 1e-12 and r100["touched"] is False
    assert r100["bid"] == 0.55 and r100["ask"] == 0.60 and {e["label"] for e in r100["edges"]} == {"挂Yes", "挂No", "吃Yes", "吃No"}
    assert r100["makers"] is False and r100["hold"] == ""  # a maker needs active points, as on every price ladder
    mk = next(x for x in bot.sim_markets(NOW) if x.market == f"{STRC.slug}#41")
    assert mk.kind == "range" and mk.key == "STRC-100" and mk.fair_up == r100["fair"] and mk.settle["end"] == dec31 and mk.hold == ""
    assert mk.item == "STRC 触及 $100 ↑ $100" and mk.makers is False and mk.sides == ("Yes", "No")
    evidence = mk.evidence
    assert evidence["basis"]["close_ms"] == dec31 + 60_000 and evidence["basis"]["years"] == L["years"]
    assert [s["what"] for s in evidence["sources"]] == ["现价", "窗口内最高", "窗口内最低"] and [s["source"] for s in evidence["sources"]] == ["Yahoo 行情（NASDAQ:STRC）", "Yahoo 日 K（常规时段）", "Yahoo 日 K（常规时段）"]
    # the page: the card sits in 价格阶梯 after the four Binance ladders
    names = [i["name"] for i in bot.odds_payload()["items"] if i["group"] == "levels"]
    assert names == ["BTC 10月价格", "ETH 10月价格", "SOL 10月价格", "HYPE 10月价格", "STRC 触及 $100"], names
    m.json.dumps(bot.odds_payload())
    print("STOCKHIT_OK")
asyncio.run(run())
