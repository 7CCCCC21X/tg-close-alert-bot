"""宏观: the WTI / gold / silver / SPY October ladders priced on Pyth's own feed, GC's December settlement ladder on Yahoo's
contract quotes, and the valuation ladders (Anthropic, OpenAI IPO) that only show Predict's books."""
import asyncio, json, math, sys, time, datetime as dt, urllib.parse
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
WTI, XAU, XAG, SPY, GC = m.MACRO_MARKETS
ANTH, OAI = m.QUOTE_MARKETS
utc = lambda *a: int(dt.datetime(*a, tzinfo=dt.timezone.utc).timestamp() * 1000)
et = lambda y, mo, d, h, mi, off=-4: m.et_ms(y, mo, d, h, mi, off)
H = 3_600_000
XAU_ID = "765d2ba906dbc32ca17cc11f5310a89e9ee1f6420508c63861f2f8ba4ee34bb2"
XAG_ID = "f2fb02c32b055c805e7238d628e5e9dadef274376114eb1f012337cabe93871e"
SPY_ID = "19e09bb805456ada3979a7d1cbb4b6d63babc3a0f8e8a9509f68afa5c4c11cd5"
CLL_ID = "ab" * 32

# --- the specs: the windows the rules describe, in sessions ------------------------------------------------------------------
assert [s.key for s in m.MACRO_MARKETS] == ["WTI-HIT-10", "XAU-HIT-10", "XAG-HIT-10", "SPY-HIT-10", "GC-DEC"]
assert all(s.group == "macro" for s in m.MACRO_MARKETS) and all(q.group == "macro" for q in m.QUOTE_MARKETS)
assert [s.slug for s in m.MACRO_MARKETS] == ["what-price-will-wti-hit-in-october-2026", "what-price-will-xauusd-hit-in-october-2026",
                                              "what-price-will-xagusd-hit-in-october-2026", "what-price-will-spy-hit-in-october-2026",
                                              "what-will-gold-gc-hit-by-end-of-december"]
assert [q.slug for q in m.QUOTE_MARKETS] == ["will-anthropics-valuation-hit-by-december-31", "what-will-openais-ipo-valuation-be"]
assert (WTI.venue, WTI.feed, WTI.calendar, WTI.symbol) == ("pyth", "CLL", "ice", "WTI")
assert (XAU.feed, XAU.calendar) == ("Metal.XAU/USD", "metal") and (XAG.feed, XAG.calendar) == ("Metal.XAG/USD", "metal")
assert (SPY.feed, SPY.calendar) == ("Equity.US.SPY/USD", "us")
assert (GC.venue, GC.feed, GC.calendar, GC.one_way) == ("cme", "GC=F", "settle", "up") and GC.start_ms == utc(2026, 3, 2, 13, 22)
assert all(s.one_way == "" for s in (WTI, XAU, XAG, SPY)) and all(s.default_dir == "up" for s in m.MACRO_MARKETS)
# the first October session's open to the last one's close (end_ms is the open of the window's last minute, as for every ladder)
assert WTI.start_ms == et(2026, 9, 30, 20, 0) and WTI.end_ms + 60_000 == et(2026, 10, 30, 18, 0)
assert XAU.start_ms == XAG.start_ms == et(2026, 9, 30, 18, 0) and XAU.end_ms + 60_000 == XAG.end_ms + 60_000 == et(2026, 10, 30, 17, 0)
assert SPY.start_ms == et(2026, 10, 1, 9, 30) and SPY.end_ms + 60_000 == et(2026, 10, 30, 16, 0)
assert GC.end_ms == et(2026, 12, 31, 23, 59, -5)
assert m.RANGE_MARKETS[0].group == "levels" and m.STOCK_HIT_MARKETS[0].group == "levels" and m.RANGE_MARKETS[0].feed == ""  # the old ladders stay put
assert (ANTH.kind, OAI.kind) == ("valuation", "bracket") and ANTH.end_ms == et(2026, 12, 31, 23, 59, -5) and OAI.end_ms == et(2027, 12, 31, 23, 59, -5)
every = [*m.TOUCH_MARKETS, *m.UPDOWN_MARKETS, *m.FLIP_MARKETS, *m.CAP_MARKETS, *m.RANGE_MARKETS, *m.STOCK_HIT_MARKETS, *m.MACRO_MARKETS, *m.QUOTE_MARKETS]
assert len({s.key for s in every}) == len(every) and len({s.slug for s in every}) == len(every)

# --- the calendars: when a touch can happen, how much trading time is left -----------------------------------------------------
mon, tue, sat = dt.date(2026, 10, 5), dt.date(2026, 10, 6), dt.date(2026, 10, 10)
assert m.macro_session("metal", tue) == (et(2026, 10, 5, 18, 0), et(2026, 10, 6, 17, 0))
assert m.macro_session("metal", mon) == (et(2026, 10, 4, 18, 0), et(2026, 10, 5, 17, 0))  # Sunday evening opens Monday's session
assert m.macro_session("ice", tue) == (et(2026, 10, 5, 20, 0), et(2026, 10, 6, 18, 0)) and m.macro_session("ice", mon) == (et(2026, 10, 4, 18, 0), et(2026, 10, 5, 18, 0))
assert m.macro_session("us", tue) == m.us_session(tue) == (et(2026, 10, 6, 9, 30), et(2026, 10, 6, 16, 0))
assert m.macro_session("metal", sat) is None and m.macro_session("ice", dt.date(2026, 12, 25)) is None and m.macro_session("metal", dt.date(2026, 10, 12)) is not None
assert m.macro_session("nope", tue) is None and m.macro_session("metal", dt.date(2026, 11, 2)) == (et(2026, 11, 1, 18, 0, -5), et(2026, 11, 2, 17, 0, -5))  # EST after the switch
assert m.macro_session_at("metal", et(2026, 10, 5, 19, 0)) == (et(2026, 10, 5, 18, 0), et(2026, 10, 6, 17, 0))  # Monday evening: Tuesday's session
assert m.macro_session_at("metal", et(2026, 10, 5, 17, 30)) is None  # the daily break
assert m.macro_session_at("metal", et(2026, 10, 10, 12, 0)) is None and m.macro_session_at("ice", et(2026, 10, 4, 18, 30)) is not None
assert m.macro_session_at("ice", et(2026, 10, 4, 17, 30)) is None and m.macro_session_at("ice", et(2026, 10, 5, 19, 0)) is None  # ICE's 18:00–20:00 break
assert m.macro_session_state("metal", et(2026, 10, 5, 17, 30)) == ("休市", et(2026, 10, 5, 18, 0))
assert m.macro_session_state("metal", et(2026, 10, 9, 17, 30)) == ("休市", et(2026, 10, 11, 18, 0))  # Friday close → Sunday evening
assert m.macro_session_state("metal", et(2026, 10, 6, 14, 0)) == ("交易中", 0)
assert m.macro_session_state("us", et(2026, 10, 6, 12, 0)) == ("交易中", 0) and m.macro_session_state("us", et(2026, 10, 6, 17, 0)) == ("休市", et(2026, 10, 7, 9, 30))
y = lambda n: n / 252
assert abs(m.macro_trading_years("metal", et(2026, 10, 5, 12, 0), et(2026, 10, 6, 17, 0)) - y(5 / 23 + 1)) < 1e-12
assert abs(m.macro_trading_years("metal", et(2026, 10, 9, 12, 0), et(2026, 10, 13, 17, 0)) - y(5 / 23 + 2)) < 1e-12  # the weekend counts nothing
assert abs(m.macro_trading_years("ice", et(2026, 10, 4, 18, 0), et(2026, 10, 5, 18, 0)) - y(24 / 22)) < 1e-12  # Monday's longer session
assert m.macro_trading_years("us", et(2026, 10, 6, 14, 0), et(2026, 10, 6, 16, 0)) == m.us_trading_years(et(2026, 10, 6, 14, 0), et(2026, 10, 6, 16, 0)) > 0
assert m.macro_trading_years("metal", 10, 5) == 0.0 and m.macro_trading_years("metal", et(2026, 10, 10, 12, 0), et(2026, 10, 11, 12, 0)) == 0.0
# CME's settlement days and gold's Active Month (Yahoo's contract symbol)
assert m.settlement_days(dt.date(2026, 11, 23), dt.date(2026, 11, 30)) == [dt.date(2026, 11, d) for d in (23, 24, 25, 27, 30)]  # Thanksgiving: none
assert m.settlement_days(dt.date(2026, 12, 24), dt.date(2027, 1, 4)) == [dt.date(2026, 12, 24), dt.date(2026, 12, 28), dt.date(2026, 12, 29), dt.date(2026, 12, 30),
                                                                           dt.date(2026, 12, 31), dt.date(2027, 1, 4)]
assert m.gc_active_contract(dt.date(2026, 10, 15)) == "GCZ26.CMX" and m.gc_active_contract(dt.date(2026, 11, 27)) == "GCZ26.CMX"
assert m.gc_active_contract(dt.date(2026, 11, 30)) == "GCG27.CMX" and m.gc_active_contract(dt.date(2026, 12, 31)) == "GCG27.CMX"  # the First Position Date
assert m.gc_active_contract(dt.date(2026, 3, 2)) == "GCJ26.CMX" and m.gc_active_contract(dt.date(2026, 3, 31)) == "GCM26.CMX"
assert m.gc_active_contract(dt.date(2027, 1, 28)) == "GCG27.CMX" and m.gc_active_contract(dt.date(2027, 1, 29)) == "GCJ27.CMX"

# --- Pyth: the feed listing, the latest price, the candles ---------------------------------------------------------------------
FEEDS = [{"id": XAU_ID, "attributes": {"asset_type": "Metal", "base": "XAU", "quote_currency": "USD", "symbol": "Metal.XAU/USD",
                                       "display_symbol": "XAU/USD", "generic_symbol": "XAUUSD"}},
         {"id": XAG_ID, "attributes": {"asset_type": "Metal", "base": "XAG", "symbol": "Metal.XAG/USD", "display_symbol": "XAG/USD", "generic_symbol": "XAGUSD"}},
         {"id": SPY_ID, "attributes": {"asset_type": "Equity", "base": "SPY", "symbol": "Equity.US.SPY/USD", "display_symbol": "SPY/USD"}},
         {"id": "0x" + CLL_ID, "attributes": {"asset_type": "Commodities", "base": "CLL", "symbol": "Commodities.CLL", "display_symbol": "CLL/USD", "generic_symbol": "CLL"}},
         {"id": "bad", "attributes": {"symbol": "Metal.XAU/USD"}}, "junk", {"id": "cd" * 32}]
assert m.pyth_feed_match(FEEDS, "Metal.XAU/USD") == (XAU_ID, "Metal.XAU/USD") and m.pyth_feed_match(FEEDS, "Equity.US.SPY/USD") == (SPY_ID, "Equity.US.SPY/USD")
assert m.pyth_feed_match(FEEDS, "CLL") == (CLL_ID, "Commodities.CLL")  # by its short name; the 0x prefix dropped
assert m.pyth_feed_match(FEEDS, "Metal.XPT/USD") is None and m.pyth_feed_match({}, "CLL") is None and m.pyth_feed_match([], "CLL") is None
assert m.pyth_feed_match(FEEDS, "CLL", pinned="0x" + "AB" * 32) == (CLL_ID, "Commodities.CLL") and m.pyth_feed_match(FEEDS, "CLL", pinned="cd" * 32) is None
assert m.pyth_feed_match(FEEDS + [{"id": "cd" * 32, "attributes": {"base": "CLL", "symbol": "Commodities.CLL2"}}], "CLL") is None  # ambiguous
LATEST = {"binary": {"encoding": "hex", "data": ["00"]}, "parsed": [
    {"id": XAU_ID, "price": {"price": "432112345678", "conf": "123", "expo": -8, "publish_time": 1760000000}, "ema_price": {}},
    {"id": XAG_ID, "price": {"price": "x", "expo": -8, "publish_time": 1}}, {"id": CLL_ID, "price": None}, {"id": SPY_ID, "price": {"price": "-5", "expo": 0, "publish_time": 1}}]}
assert m.parse_pyth_latest(LATEST) == {XAU_ID: (4321.12345678, 1760000000000)}
assert m.parse_pyth_latest("nope") == {} and m.parse_pyth_latest({"parsed": "x"}) == {} and m.parse_pyth_latest({"parsed": [{"id": "0x" + XAU_ID, "price": {"price": "1", "expo": 0, "publish_time": 2}}]}) == {XAU_ID: (1.0, 2000)}
HIST = {"s": "ok", "t": [1759280400, 1759276800], "o": [2.0, 1.0], "h": [4.0, 3.0], "l": [1.5, 0.5], "c": [3.5, 2.5], "v": [0, 0]}
assert m.parse_pyth_history(HIST) == [(1759276800000, 1.0, 3.0, 0.5, 2.5), (1759280400000, 2.0, 4.0, 1.5, 3.5)]  # oldest first
assert m.parse_pyth_history({"s": "no_data", "nextTime": 1}) == []
for bad in ({"s": "error", "errmsg": "boom"}, {"s": "ok", "t": [1], "o": [1], "h": [1], "l": [1]}, {"s": "ok", "t": [1, 2], "o": [1], "h": [1], "l": [1], "c": [1]}, "x", {}):
    try: m.parse_pyth_history(bad); assert False, bad
    except ValueError: pass

# --- the valuation ladders' titles --------------------------------------------------------------------------------------------------
assert m.valuation_level("Will Anthropic's valuation hit $500B by December 31?") == D("5e11") and m.valuation_level("$1T") == D("1e12")
assert m.valuation_level("1.5 trillion") == D("1.5e12") and m.valuation_level("Anthropic hits $1,000B") == D("1e12") and m.valuation_level("December 31") is None
assert m.valuation_level("$350 billion") == D("3.5e11") and m.valuation_level("") is None and m.valuation_level("Yes") is None
assert m.valuation_amounts("$500B - $750B") == [D("5e11"), D("7.5e11")]
assert m.bracket_bounds("Less than $500B") == (D(0), D("5e11")) and m.bracket_level("Less than $500B") == D(0)
assert m.bracket_bounds("$500B - $750B") == (D("5e11"), D("7.5e11")) and m.bracket_bounds("$2T or more") == (D("2e12"), None)
assert m.bracket_bounds("More than $2 trillion") == (D("2e12"), None) and m.bracket_bounds("Not listed") is None and m.bracket_level("x") is None
assert [m.bracket_label(t) for t in ("Less than $500B", "$500B - $750B", "$1T-$1.5T", "$2T or more", "Other")] == ["< $500B", "$500B–$750B", "$1T–$1.5T", "≥ $2T", "Other"]
assert m.usd_short(D("1.5e12")) == "$1.5T" and m.usd_short(5e11) == "$500B" and m.usd_short(83_200_000) == "$83.2M" and m.usd_short(1e9) == "$1B"

# --- a level's direction from Pyth's rules (they name both ways) and the title's arrow -----------------------------------------------
PYTH_RULES = ('This market will resolve to "Yes" if, at any point after market creation and during a trading session of October 2026, any '
              '1-minute candle for Gold (XAUUSD) has a final "High" or "Low" price equal to or beyond (above for ↑ High Prices, below for '
              '↓ Low Prices) the listed price. Otherwise, this market will resolve to "No".')
assert m.level_direction(PYTH_RULES) == ("", "") and m.level_direction(PYTH_RULES, "↑ 4,500") == ("up", "标题") and m.level_direction(PYTH_RULES, "↓ $3,900") == ("down", "标题")
assert m.level_direction('has a final "High" price equal to or greater than') == ("up", "规则")  # one-way rules still decide
assert m.price_level("↑ 4,500") == D(4500) and m.price_level("↓ $62.5") == D("62.5") and m.price_level("↑ 700") == D(700)

# --- Config: the section switch and pinned feed ids ---------------------------------------------------------------------------------
cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "PYTH_FEEDS": "CLL=0x" + "ab" * 32 + "; Metal.XAU/USD=" + "cd" * 32})
assert cfg.macro and cfg.pyth_feeds == {"CLL": "ab" * 32, "Metal.XAU/USD": "cd" * 32}
assert not m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "MACRO": "off"}).macro and m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x"}).pyth_feeds == {}
for bad in ("CLL", "CLL=xyz", "=" + "ab" * 32):
    try: m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "PYTH_FEEDS": bad}); assert False, bad
    except ValueError: pass

# --- the fixtures: Pyth and Yahoo as the bot sees them -----------------------------------------------------------------------------
NOW = et(2026, 10, 6, 14, 20)  # Tuesday 14:20 EDT: the metals and SPY trade, so does ICE (the 14:00 candle is the running one)
clock = {"now": NOW}
SPECIAL = {("metal", et(2026, 10, 2, 16, 0)): (4300.0, 4400.0, 4295.0), ("metal", et(2026, 10, 1, 2, 0)): (4300.0, 4305.0, 4200.0),
           ("metal", et(2026, 10, 6, 14, 0)): (4320.0, 4330.0, 4310.0), ("metal", et(2026, 10, 6, 15, 0)): (4320.0, 4335.0, 4315.0),
           ("us", et(2026, 10, 1, 9, 0)): (680.0, 700.0, 679.0), ("us", et(2026, 10, 1, 9, 30)): (680.0, 690.0, 679.0)}


def candles(calendar, res_ms, start_s, end_s):
    """Candles Pyth would serve between two instants: one per `res_ms` while the market trades (plus the metals' 17:00 break
    candle on 10-05 with a High of 4600, which must not count), none after the fake clock."""
    base = 4300.0 if calendar == "metal" else 680.0
    out = []
    t = start_s * 1000 - start_s * 1000 % res_ms
    while t < end_s * 1000 and t <= clock["now"]:
        trading = m.macro_session_at(calendar, t) is not None or (calendar == "us" and 4 * H <= t - m.et_wall_ms(m.et_date(t), 0, 0) < 20 * H
                                                                   and m.et_date(t).weekday() < 5)
        if trading or t == et(2026, 10, 5, 17, 0):
            o, h, l = SPECIAL.get((calendar, t), (base, base + 5, base - 5))
            if t == et(2026, 10, 5, 17, 0):
                h = 4600.0
            out.append((t // 1000, o, h, l, base))
        t += res_ms
    return out


def daily(start_s, end_s):
    """Daily candles at 00:00 UTC, closes alternating ±0.5% (about 16% annualised); today's (unfinished) one included."""
    out, day, i = [], dt.datetime.fromtimestamp(start_s, dt.timezone.utc).date(), 0
    while True:
        t = utc(day.year, day.month, day.day, 0, 0) // 1000
        if t >= end_s or t * 1000 > clock["now"]:
            break
        if day.weekday() < 5:
            close = 4300 * (1.005 if i % 2 else 0.995); i += 1
            out.append((t, 4300.0, close + 5, close - 5, close))
        day += dt.timedelta(days=1)
    return out


def udf(rows):
    return json.dumps({"s": "ok", "t": [r[0] for r in rows], "o": [r[1] for r in rows], "h": [r[2] for r in rows], "l": [r[3] for r in rows],
                       "c": [r[4] for r in rows], "v": [0] * len(rows)} if rows else {"s": "no_data"}).encode()


def yahoo(symbol, stamps, rows, price=None, when=None):
    col = lambda i: [None if r is None else r[i] for r in rows]
    meta = {"currency": "USD", "symbol": symbol, "gmtoffset": -14400, "regularMarketPrice": price, "regularMarketTime": when}
    return json.dumps({"chart": {"result": [{"meta": meta, "timestamp": stamps, "indicators": {"quote": [{"open": col(0), "high": col(1), "low": col(2), "close": col(3)}]}}],
                                 "error": None}}).encode()


calls = []
PRICES = {XAU_ID: 4321.5, SPY_ID: 683.25}


async def fake_fetch(url, extra=None, record=True):
    calls.append(url)
    parts = urllib.parse.urlsplit(url); q = urllib.parse.parse_qs(parts.query)
    if parts.netloc == "hermes.pyth.network" and parts.path.endswith("/price_feeds"):
        return json.dumps(FEEDS).encode()
    if parts.netloc == "hermes.pyth.network" and parts.path.endswith("/updates/price/latest"):
        fid = q["ids[]"][0]
        return json.dumps({"parsed": [{"id": fid, "price": {"price": str(int(PRICES[fid] * 1e8)), "expo": -8, "publish_time": (clock["now"] - 20_000) // 1000}}]}).encode()
    if parts.netloc == "benchmarks.pyth.network":
        symbol, res, start, end = q["symbol"][0], q["resolution"][0], int(q["from"][0]), int(q["to"][0])
        assert symbol in {"Metal.XAU/USD", "Equity.US.SPY/USD"}, symbol
        if res == "D":
            return udf(daily(start, end))
        return udf(candles("metal" if "XAU" in symbol else "us", int(res) * 60_000, start, end))
    if parts.netloc == "query1.finance.yahoo.com":
        symbol = urllib.parse.unquote(parts.path.rsplit("/", 1)[-1])
        if q["interval"][0] == "1m":
            assert symbol == GC_CONTRACT[0], symbol
            return yahoo(symbol, [], [], price=4350.0, when=(clock["now"] - 30_000) // 1000)
        assert symbol == "GC=F", symbol
        return yahoo(symbol, *GC_DAILY)
    raise AssertionError(f"unexpected url {url}")


GC_CONTRACT = ["GCZ26.CMX"]
GC_DAILY = [[], []]  # (stamps, rows) of GC=F's daily bars, filled once the clock is set
m.fetch_source = fake_fetch


def gc_bars(today):
    """A year of GC=F daily bars stamped at the Globex open (18:00 ET of the day before), closes alternating ±0.2% around 4340; the
    2026-06-10 settlement 4500 (the window's high so far), a 4600 one on 2026-02-20 (before the market existed)."""
    stamps, rows, day, i = [], [], today - dt.timedelta(days=365), 0
    while day <= today:
        if day.weekday() < 5 and day not in m.US_MARKET_HOLIDAYS:
            close = 4340 * (1.002 if i % 2 else 0.998); i += 1
            close = {dt.date(2026, 6, 10): 4500.0, dt.date(2026, 2, 20): 4600.0, dt.date(2026, 10, 14): 4340.0}.get(day, close)
            stamps.append(m.et_wall_ms(day - dt.timedelta(days=1), 18, 0) // 1000); rows.append((close - 3, close + 4, close - 6, close))
        day += dt.timedelta(days=1)
    return stamps, rows


class FakeMarket:
    config = None
    def now_ms(self): return clock["now"]
    async def prices(self): return {}   # /diag lists a probe for the Binance feeds; none is run here
    async def get(self, *a, **k): return {}


class FakeTelegram:
    def __init__(self): self.sent = []
    async def call(self, *a, **k): return True
    async def send(self, chat, thread, text, reply_markup=None, parse_mode=None): self.sent.append(text)


async def run():
    # --- a Pyth ladder: the feed looked up once, the price, σ, the window's extremes in sessions only ---------------------------
    store = m.Store(":memory:")
    rm = m.PythRangeMarket(store, XAU)
    assert not rm.known() and rm.feed_symbol == "Metal.XAU/USD" and rm.feed_id == "" and rm.resolution == 60 and rm.calendar == "metal"
    assert rm.missing_note(NOW) == "等待 Pyth 行情源确认" and rm.probability(D(4500), "up", NOW) is None and rm.remaining_years(NOW) > 0
    assert rm.lookup_url() == "https://hermes.pyth.network/v2/price_feeds?query=XAU"
    await rm.refresh(NOW)
    assert rm.known() and rm.feed_id == XAU_ID and store.get("pyth:feed:Metal.XAU/USD") == {"id": XAU_ID, "symbol": "Metal.XAU/USD"}
    assert rm.error == "" and rm.price == D("4321.5") and rm.priced_ms == NOW - 20_000 and rm.fetched_ms == NOW, rm.error
    assert rm.sigma is not None and 0.1 < rm.sigma < 0.25 and rm.sigma_ms == NOW, rm.sigma
    hist = rm.history
    assert hist["high"] == 4400.0 and hist["high_at"] == et(2026, 10, 2, 16, 0) and hist["low"] == 4200.0 and hist["low_at"] == et(2026, 10, 1, 2, 0), hist
    assert hist["open"] == 4300.0 and hist["through"] == et(2026, 10, 6, 14, 0) and hist["start"] == XAU.start_ms
    assert rm.running == {"open": et(2026, 10, 6, 14, 0), "high": 4330.0, "low": 4310.0} and rm.scanned_ms == NOW
    assert rm.extremes() == (4400.0, 4200.0)  # the break candle's 4600 did not count
    assert rm.reached(D(4400), "up") and not rm.reached(D(4401), "up") and rm.reached(D(4200), "down") and not rm.reached(D(4199), "down")
    assert rm.probability(D(4400), "up", NOW) == 1.0 and rm.probability(D(4200), "down", NOW) == 1.0
    years = m.macro_trading_years("metal", NOW, XAU.end_ms + 60_000)
    assert rm.remaining_years(NOW) == years and years > 0
    p = rm.probability(D(4500), "up", NOW)
    assert p == m.hit_probability(4321.5, 4500.0, rm.sigma, years) and 0 < p < 1
    assert rm.probability(D(4000), "down", NOW) == m.low_probability(4321.5, 4000.0, rm.sigma, years)
    assert rm.model_swing(D(4500), "up", NOW, p) > 0 and rm.advice_problem(NOW) == "" and rm.missing_note(NOW) == ""
    # the live price counts among the extremes only while Pyth published it inside a session of the window
    rm.price, rm.priced_ms = D("4450"), NOW - 20_000
    assert rm.extremes()[0] == 4450.0 and rm.reached(D(4450), "up")
    rm.priced_ms = et(2026, 10, 5, 17, 30)  # the daily break
    assert rm.extremes()[0] == 4400.0
    rm.price, rm.priced_ms = D("4321.5"), NOW - 20_000
    assert sum("price_feeds" in u for u in calls) == 1 and sum("updates/price/latest" in u for u in calls) == 1
    assert any("resolution=D" in u for u in calls) and any("resolution=60" in u for u in calls) and all("Metal.XAU%2FUSD" in u for u in calls if "benchmarks" in u)
    # the card's words
    extras = rm.card_extras(NOW)
    assert extras["session"].startswith("交易中（周日 18:00 至周五 17:00 ET") and extras["feed"] == "Metal.XAU/USD" and extras["feed_id"] == XAU_ID
    assert rm.source_name() == "Pyth（Metal.XAU/USD）" and rm.settle_name() == "Pyth 1 分钟 K" and rm.extremes_name() == "Pyth 60 分钟 K"
    assert rm.close_label() == "10-30 17:00 ET（北京 10-31 05:00）10 月最后一个交易时段收盘为止" and rm.window_label().startswith("09-30 18:00 ET（北京 10-01 06:00）起")
    # stale: nothing read for 5 minutes, or a price published over 10 minutes ago while the market trades; between sessions it stands
    rm.fetched_ms, rm.priced_ms = NOW, NOW - 11 * 60_000
    assert rm.price_stale(NOW) and rm.probability(D(4500), "up", NOW) is None and "暂停概率" in rm.missing_note(NOW)
    rm.priced_ms = NOW - 60_000
    assert not rm.price_stale(NOW)
    SAT = et(2026, 10, 10, 12, 0)
    rm.fetched_ms, rm.priced_ms = SAT, et(2026, 10, 9, 16, 59)
    assert not rm.price_stale(SAT) and rm.card_extras(SAT)["session"].startswith("休市，10-11 18:00 ET（北京 10-12 06:00）开盘")
    assert rm.price_seconds(NOW) == 30 and rm.price_seconds(SAT) == 120
    # a restart knows the feed at once; a pinned id wins over the lookup
    assert m.PythRangeMarket(store, XAU).known() and m.PythRangeMarket(store, XAU).feed_id == XAU_ID
    pinned = m.PythRangeMarket(m.Store(":memory:"), XAU, "0x" + CLL_ID)
    assert pinned.feed_id == CLL_ID and pinned.feed_symbol == "Metal.XAU/USD" and pinned.known() and pinned.pinned_id == CLL_ID
    assert m.PythRangeMarket(store, XAU, "cd" * 32).feed_symbol == "Metal.XAU/USD"  # a pinned id different from the saved one: the symbol is the spec's
    # the later scan: the running candle finished, a new one running; the high stands
    clock["now"] = NOW2 = NOW + 2 * H
    rm.times["scan"] = -1e9
    await rm.refresh(NOW2)
    hist = rm.history
    assert hist["high"] == 4400.0 and hist["through"] == et(2026, 10, 6, 16, 0) and rm.running["open"] == et(2026, 10, 6, 16, 0), hist
    # a scan on Saturday: no candles since Friday 17:00; the span is read through to an hour ago and stays there
    clock["now"] = SAT
    rm.times["scan"] = rm.times["vol"] = -1e9  # both due again (σ is re-read every 30 minutes in real time)
    await rm.refresh(SAT)
    assert rm.history["through"] == et(2026, 10, 10, 11, 0) and rm.running == {} and rm.error == "", (rm.history, rm.error)
    assert rm.advice_problem(SAT) == ""
    clock["now"] = NOW

    # --- SPY: half-hour candles, the regular session only (the 09:00 pre-market High of 700 does not count) ------------------------
    srm = m.PythRangeMarket(m.Store(":memory:"), SPY)
    assert srm.resolution == 30 and srm.feed_symbol == "Equity.US.SPY/USD"
    await srm.refresh(NOW)
    assert srm.error == "" and srm.price == D("683.25") and srm.history["high"] == 690.0 and srm.history["high_at"] == et(2026, 10, 1, 9, 30), (srm.error, srm.history)
    assert srm.running["open"] == et(2026, 10, 6, 14, 0) and srm.history["through"] == et(2026, 10, 6, 14, 0)
    assert srm.remaining_years(NOW) == m.us_trading_years(NOW, SPY.end_ms + 60_000) and srm.card_extras(NOW)["session"].startswith("交易中（常规时段 09:30–16:00 ET）")
    assert any("resolution=30" in u for u in calls)

    # --- a lookup that finds nothing: the card waits, the failure is named, nothing else is asked for --------------------------------
    wrm = m.PythRangeMarket(m.Store(":memory:"), m.RangeSpec("XPT-HIT-10", "x", "XPTUSD", "pyth", "铂金", XAU.start_ms, XAU.end_ms, group="macro", feed="Metal.XPT/USD", calendar="metal"))
    before = len(calls)
    await wrm.refresh(NOW)
    assert not wrm.known() and "Hermes 没有列出 Metal.XPT/USD" in wrm.error and len(calls) == before + 1 and wrm.missing_note(NOW).startswith("等待 Pyth 行情源确认（")

    # --- GC: the active contract's price, the settlements so far from GC=F, one look a day ----------------------------------------
    GC_NOW = et(2026, 10, 15, 12, 0)  # before today's 13:30 settlement
    clock["now"] = GC_NOW
    GC_DAILY[:] = gc_bars(dt.date(2026, 10, 15))
    gstore = m.Store(":memory:")
    grm = m.SettleRangeMarket(gstore, GC)
    assert grm.range_word == "窗口内" and grm.settle_ms(dt.date(2026, 10, 15)) == et(2026, 10, 15, 13, 30)
    assert m.SettleRangeMarket.bar_date(m.et_wall_ms(dt.date(2026, 10, 14), 18, 0)) == dt.date(2026, 10, 15) == m.SettleRangeMarket.bar_date(et(2026, 10, 15, 0, 0)) == m.SettleRangeMarket.bar_date(utc(2026, 10, 15, 0, 0))
    await grm.refresh(GC_NOW)
    assert grm.error == "" and grm.contract == "GCZ26.CMX" and grm.price == D("4350.0000") and grm.fetched_ms == GC_NOW, grm.error
    assert grm.sigma is not None and 0.02 < grm.sigma < 0.1, grm.sigma  # ±0.2% a day
    hist = grm.history
    assert hist["high"] == 4500.0 and m.SettleRangeMarket.bar_date(hist["high_at"]) == dt.date(2026, 6, 10) and hist["through"] == et(2026, 10, 15, 0, 0), hist
    assert grm.settlements[-1] == ("2026-10-14", 4340.0) and len(grm.settlements) == 5
    assert grm.marks()["high"] == 4500.0 and grm.extremes()[0] == 4500.0
    grm.price = D("4700")  # the live price is never a settlement
    assert grm.extremes()[0] == 4500.0 and not grm.reached(D(4600), "up")
    grm.price = D("4350")
    assert grm.reached(D(4500), "up") and grm.probability(D(4500), "up", GC_NOW) == 1.0 and grm.probability(D(4340), "up", GC_NOW) == 1.0
    ahead = m.settlement_days(dt.date(2026, 10, 15), dt.date(2026, 12, 31))
    want_days = len(ahead) - 1 + 1.5 / 24
    assert abs(grm.days_left(GC_NOW) - want_days) < 1e-9 and abs(grm.remaining_years(GC_NOW) - want_days / 252) < 1e-12
    assert dt.date(2026, 11, 26) not in ahead and dt.date(2026, 12, 25) not in ahead and dt.date(2026, 12, 31) in ahead
    p = grm.probability(D(4800), "up", GC_NOW)
    shift = math.exp(0.5826 * grm.sigma * math.sqrt(1 / 252))
    assert p == m.hit_probability(4350.0, 4800.0 * shift, grm.sigma, grm.remaining_years(GC_NOW)) and 0 < p < 1
    assert p < m.hit_probability(4350.0, 4800.0, grm.sigma, grm.remaining_years(GC_NOW))  # a daily look misses the path between looks
    assert grm.model_swing(D(4800), "up", GC_NOW, p) > 0 and grm.advice_problem(GC_NOW) == "" and grm.missing_note(GC_NOW) == ""
    extras = grm.card_extras(GC_NOW)
    assert extras["session"].startswith("活跃月合约 GCZ26.CMX（Yahoo），下一次结算 10-15 13:30 ET") and "GCG27.CMX 从 11-30" in extras["session"], extras["session"]
    assert "最近结算 10-12 " in extras["extremes_note"] and extras["question"].startswith("GC 活跃月合约在 12-31 前")
    assert extras["extremes_tail"] == "；现价不是结算价，不计入" and rm.card_extras(NOW)["extremes_tail"].startswith("，加上正在走的这根 60 分钟 K")
    assert grm.close_label() == "12-31 的 CME 结算为止：任一日活跃月结算价 ≥ 档位即 Yes，到期未达到为 No" and grm.settle_name() == "CME 官方结算价"
    assert grm.source_name() == "Yahoo 行情（GCZ26.CMX）" and any("GCZ26.CMX" in u for u in calls) and any("GC%3DF" in u for u in calls)
    # after the First Position Date the February contract is quoted; after the last settlement nothing is priced until it is read
    clock["now"] = DEC = et(2026, 12, 1, 12, 0, -5)
    GC_CONTRACT[0] = "GCG27.CMX"
    grm.times["price"] = -1e9
    await grm.refresh(DEC)
    assert grm.contract == "GCG27.CMX" and grm.failures["price"] == "" and grm.card_extras(DEC)["session"].startswith("活跃月合约 GCG27.CMX")
    END = et(2026, 12, 31, 15, 0, -5)
    grm.fetched_ms = END
    assert grm.days_left(END) == 0.0 and grm.probability(D(4800), "up", END) is None and grm.missing_note(END) == "最后一个结算日的结算价次日才读到，等待结算"
    assert grm.probability(D(4500), "up", END) == 1.0  # reached stays reached
    assert grm.probability(D(4800), "up", GC.end_ms + 60_000) is None  # the window is over but its last day is not read yet
    clock["now"] = NOW
    GC_CONTRACT[0] = "GCZ26.CMX"

    # --- the bot: cards in the 宏观 section, their /edge group, the book-only ladders -----------------------------------------------
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ADMIN_USER_ID": "42", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off"})
    tg = FakeTelegram()
    bot = m.Bot(cfg, m.Store(":memory:"), FakeMarket(), tg)
    assert set(bot.ranges) >= {s.key for s in m.MACRO_MARKETS} and set(bot.quotes) == {"ANTHROPIC-VAL", "OPENAI-IPO"}
    assert isinstance(bot.ranges["XAU-HIT-10"], m.PythRangeMarket) and isinstance(bot.ranges["GC-DEC"], m.SettleRangeMarket) and isinstance(bot.ranges["BTC-HIT-10"], m.RangeMarket)
    assert bot.predict.ladder_parse["ANTHROPIC-VAL"] is m.valuation_level and bot.predict.ladder_parse["OPENAI-IPO"] is m.bracket_level and bot.predict.ladder_parse["XAU-HIT-10"] is m.price_level
    assert {"ANTHROPIC-VAL", "OPENAI-IPO", "XAU-HIT-10", "GC-DEC"} <= bot.predict.ladder_keys and "XAU-HIT-10" in bot.predict.reward_keys
    targets = bot.predict_targets(NOW)
    assert targets["XAU-HIT-10"] == XAU.slug and targets["GC-DEC"] == GC.slug and targets["OPENAI-IPO"] == OAI.slug and targets["ANTHROPIC-VAL"] == ANTH.slug
    assert bot.edge_target(["宏观"]) == ("group:macro", "宏观") and bot.edge_target(["macro"]) == ("group:macro", "宏观") and bot.edge_target(["商品"]) == ("group:macro", "宏观")
    assert bot.edge_target(["黄金 XAUUSD"]) == ("key:XAU-HIT-10", "黄金 XAUUSD 10月价格") and bot.edge_target(["OpenAI"]) == ("key:OPENAI-IPO", "OpenAI IPO 估值")
    assert "宏观" in bot.edge_text() and m.market_driver("range", "XAU-HIT-10") == ("XAUUSD", "XAUUSD") and m.market_driver("range", "GC-DEC") == ("GC", "GC")
    assert dict(bot.edge_markets())["ANTHROPIC-VAL"] == "Anthropic 估值（12-31 前）"
    none = m.Bot(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "MACRO": "off"}), m.Store(":memory:"), FakeMarket(), tg)
    assert not any(k in none.ranges for k in (s.key for s in m.MACRO_MARKETS)) and none.quotes == {} and "GC-DEC" not in none.predict_targets(NOW)
    # the gold card from a priced market and Predict's levels (titles with arrows, the category's both-way rules)
    xrm = bot.ranges["XAU-HIT-10"]
    await xrm.refresh(NOW)
    assert xrm.error == "" and xrm.price == D("4321.5")
    book = lambda mid, price, bid, ask: m.PredictBook("XAU-HIT-10", XAU.slug, mid, f"↑ {price}", ((D(bid), D(100)),), ((D(ask), D(100)),), NOW - 5000, 200)
    rows = [m.LadderRow(D(4500), "x1", "↑ 4,500", book("x1", "4,500", "0.20", "0.26"), "", "Will XAUUSD hit ↑ 4,500 in October 2026?"),
            m.LadderRow(D(4000), "x2", "↓ 4,000", book("x2", "4,000", "0.10", "0.14"), "", ""),
            m.LadderRow(D(4400), "x3", "↑ 4,400", book("x3", "4,400", "0.95", "0.99"), "", "")]
    bot.predict.ladders["XAU-HIT-10"] = rows
    meta = {"outcomes": ["Yes", "No"], "created_ms": 0, "status": "OPEN", "fee_bps": 200, "resolved": None, "rules": PYTH_RULES, "question": "",
            "trading_status": "OPEN", "rewards": None, "spread_threshold": None, "share_threshold": None}
    for mid in ("x1", "x2", "x3"):
        bot.predict.market_meta[mid] = (dict(meta), time.monotonic())
    item = bot.range_payload(xrm, NOW)
    assert item["group"] == "macro" and item["kind"] == "ladder" and item["ladder"]["kind"] == "price" and item["name"] == "黄金 XAUUSD 10月价格"
    assert item["ladder"]["feed"] == "Metal.XAU/USD" and item["ladder"]["venue"] == "Pyth" and item["ladder"]["symbol"] == "XAUUSD" and item["ladder"]["price"] == "$4,322"
    assert item["ladder"]["high"] == "$4,400" and item["ladder"]["low"] == "$4,200" and item["close_ms"] == XAU.end_ms and "missing" not in item
    by = {r["label"]: r for r in item["ladder"]["rows"]}
    assert set(by) == {"↑ $4.5k", "↓ $4k", "↑ $4.4k"} and [r["label"] for r in item["ladder"]["rows"]] == ["↑ $4.5k", "↑ $4.4k", "↓ $4k"]
    assert by["↑ $4.5k"]["dir"] == "up" and by["↑ $4.5k"]["dir_source"] == "标题" and by["↑ $4.5k"]["dir_note"] == "" and by["↓ $4k"]["dir"] == "down"
    assert by["↑ $4.4k"]["touched"] and by["↑ $4.4k"]["fair"] == 1.0 and by["↑ $4.5k"]["fair"] == xrm.probability(D(4500), "up", NOW)
    assert by["↑ $4.5k"]["edges"] and by["↑ $4.5k"]["hold"] == "" and by["↓ $4k"]["fair"] == xrm.probability(D(4000), "down", NOW)
    sims = [mk for mk in bot.sim_markets(NOW) if mk.key == "XAU-HIT-10"]
    assert len(sims) == 3 and all(mk.kind == "range" for mk in sims) and {bot.edge_group(mk) for mk in sims} == {"macro"}
    assert bot.edge_bar(sims[0], {"group:macro": 3.0}) == 0.03 and bot.edge_bar(sims[0], {"group:levels": 3.0}) == cfg.edge_alert_edge
    # the gold settlement card: every level is an ↑ one by the rules, whatever the title says
    grm = bot.ranges["GC-DEC"]
    clock["now"] = GC_NOW
    await grm.refresh(GC_NOW)
    bot.predict.ladders["GC-DEC"] = [m.LadderRow(D(4800), "g1", "$4,800", m.PredictBook("GC-DEC", GC.slug, "g1", "$4,800", ((D("0.3"), D(50)),), ((D("0.36"), D(50)),), GC_NOW - 5000, 200), "", "What will Gold (GC) hit by end of December?")]
    bot.predict.market_meta["g1"] = (dict(meta, rules="equal to or above the listed price"), time.monotonic())
    gitem = bot.range_payload(grm, GC_NOW)
    assert gitem["group"] == "macro" and gitem["ladder"]["rows"][0]["dir"] == "up" and gitem["ladder"]["rows"][0]["dir_source"] == "规则"
    assert gitem["ladder"]["rows"][0]["fair"] == grm.probability(D(4800), "up", GC_NOW) and gitem["ladder"]["question"].startswith("GC 活跃月合约")
    clock["now"] = NOW
    # the book-only ladders: the brackets' books and the probability they imply, no fair price, nothing suggested
    qbook = lambda mid, title, bid, ask: m.PredictBook("OPENAI-IPO", OAI.slug, mid, title, ((D(bid), D(40)),), ((D(ask), D(40)),), NOW - 3000, None)
    bot.predict.ladders["OPENAI-IPO"] = [m.LadderRow(D(0), "o1", "Less than $500B", qbook("o1", "Less than $500B", "0.10", "0.14"), "", ""),
                                         m.LadderRow(D("5e11"), "o2", "$500B - $1T", qbook("o2", "$500B - $1T", "0.40", "0.46"), "", ""),
                                         m.LadderRow(D("1e12"), "o3", "$1T or more", qbook("o3", "$1T or more", "0.40", "0.44"), "", ""),
                                         m.LadderRow(D("2e12"), "o4", "$2T or more", None, "HTTP 404", "")]
    for mid in ("o1", "o2", "o3", "o4"):
        bot.predict.market_meta[mid] = (dict(meta, rules=""), time.monotonic())
    qitem = bot.quote_payload(OAI, NOW)
    assert qitem["group"] == "macro" and qitem["kind"] == "ladder" and qitem["ladder"]["kind"] == "quote" and qitem["ladder"]["bracket"] and qitem["close_ms"] == OAI.end_ms
    assert [r["label"] for r in qitem["ladder"]["rows"]] == ["≥ $2T", "≥ $1T", "$500B–$1T", "< $500B"] and all(r["fair"] is None for r in qitem["ladder"]["rows"])
    q = {r["label"]: r for r in qitem["ladder"]["rows"]}
    assert q["< $500B"]["bid"] == 0.10 and q["< $500B"]["ask"] == 0.14 and abs(q["< $500B"]["mid"] - 0.12) < 1e-12 and q["< $500B"]["bids"] == [[0.1, 40.0]]
    assert q["≥ $2T"]["mid"] is None and "bids" not in q["≥ $2T"] and q["≥ $2T"]["error"] == "" and q["< $500B"]["title"] == "Less than $500B"
    assert abs(qitem["ladder"]["implied_total"] - (0.12 + 0.43 + 0.42)) < 1e-12 and "edges" not in q["< $500B"] and "need" not in q["< $500B"]
    assert qitem["ladder"]["note"].startswith("没有可观测的行情源") and qitem["predict"]["url"].startswith("https://predict.fun/") and OAI.slug in qitem["predict"]["url"]
    assert not any(mk.key == "OPENAI-IPO" for mk in bot.sim_markets(NOW))  # nothing to suggest, alert or paper-trade
    bot.predict.ladders["ANTHROPIC-VAL"] = [m.LadderRow(D("5e11"), "a1", "Will Anthropic's valuation hit $500B by December 31?",
                                                        m.PredictBook("ANTHROPIC-VAL", ANTH.slug, "a1", "$500B", ((D("0.6"), D(10)),), (), NOW - 3000, None), "", "")]
    bot.predict.market_meta["a1"] = (dict(meta, rules=""), time.monotonic())
    aitem = bot.quote_payload(ANTH, NOW)
    assert aitem["ladder"]["rows"][0]["label"] == "≥ $500B" and aitem["ladder"]["rows"][0]["mid"] == 0.6 and aitem["ladder"]["implied_total"] is None and not aitem["ladder"]["bracket"]
    empty = bot.quote_payload(ANTH, NOW) if bot.predict.ladders.pop("ANTHROPIC-VAL") else None
    assert empty["ladder"]["rows"] == [] and empty["ladder"]["waiting"] == "等待 Predict 档位"
    # the whole page: every 宏观 card present, in the section, with its market key
    payload = bot.odds_payload()
    macro = [i for i in payload["items"] if i["group"] == "macro"]
    assert [i["name"] for i in macro] == ["WTI 原油 10月价格", "黄金 XAUUSD 10月价格", "白银 XAGUSD 10月价格", "标普500 SPY 10月价格", "黄金期货 GC 12月底前结算价",
                                          "Anthropic 估值（12-31 前）", "OpenAI IPO 估值"]
    assert [i["ekey"] for i in macro] == ["WTI-HIT-10", "XAU-HIT-10", "XAG-HIT-10", "SPY-HIT-10", "GC-DEC", "ANTHROPIC-VAL", "OPENAI-IPO"]
    assert macro[0]["missing"] == "等待 Pyth 行情源确认" and "missing" not in macro[1] and macro[6]["ladder"]["kind"] == "quote"
    json.dumps(payload)
    # Predict's rows for a bracket: the lowest range starts at 0 and is still a level
    feed = bot.predict
    async def details(mid): return dict(meta, rules="")
    async def orderbook(market): return ((D("0.3"), D(10)),), ((D("0.35"), D(5)),), market["id"]
    feed.market_details, feed.orderbook = details, orderbook
    row = await feed.ladder_row("OPENAI-IPO", OAI.slug, {"id": "n1", "title": "Less than $500B", "question": "What will OpenAI's IPO valuation be?"})
    assert row is not None and row.target == D(0) and row.book.bids[0] == (D("0.3"), D(10)) and row.title == "Less than $500B"
    assert await feed.ladder_row("OPENAI-IPO", OAI.slug, {"id": "n2", "title": "Other", "question": "What will OpenAI's IPO valuation be?"}) is None
    row = await feed.ladder_row("ANTHROPIC-VAL", ANTH.slug, {"id": "n3", "title": "December 31", "question": "Will Anthropic's valuation hit $750B by December 31?"})
    assert row is not None and row.target == D("7.5e11")  # the date is not a level; the question names it
    # /diag: Pyth's feeds (the lookup while unknown, the price and candles once known) and Yahoo's gold contract
    probes = bot.diag_probes(NOW)
    names = {(g, n) for g, n, _, _ in probes}
    assert ("宏观·XAUUSD", "Pyth Hermes 最新价") in names and ("宏观·XAUUSD", "Pyth Benchmarks 日 K") in names and ("宏观·WTI", "Pyth Hermes 行情源查找") in names
    assert ("宏观·GC", "Yahoo GCZ26.CMX 1 分钟 K") in names and ("宏观·XAU", "Pyth Hermes 最新价") not in names
    check = next(c for g, n, _, c in probes if (g, n) == ("宏观·XAUUSD", "Pyth Hermes 最新价"))
    assert check(await fake_fetch(xrm.latest_url())).startswith("Metal.XAU/USD 4,321.5（发布 ")
    check = next(c for g, n, _, c in probes if (g, n) == ("宏观·WTI", "Pyth Hermes 行情源查找"))
    assert check(json.dumps(FEEDS).encode()) == f"Commodities.CLL（id {CLL_ID[:10]}…）"
    # the page itself carries the section and the quote-only renderer
    assert 'macro:"宏观"' in m.WEB_PAGE and "function quoteLadder" in m.WEB_PAGE and '["levels","ladder","macro","sim"]' in m.WEB_PAGE and "SEC_SORTS.macro" in m.WEB_PAGE
    assert 'quote=L.kind==="quote"' in m.MINI_JS
    print("MACRO_OK")


asyncio.run(run())
