import asyncio, os, re, sys, math, time, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
BTC, ETH, SOL, HYPE = m.RANGE_MARKETS
H = 3_600_000
utc = lambda *a: int(dt.datetime(*a, tzinfo=dt.timezone.utc).timestamp() * 1000)

# --- the four markets as their rules state them -------------------------------------------------------------------
assert BTC.start_ms == utc(2026, 10, 1, 4, 0) and BTC.end_ms == utc(2026, 11, 1, 3, 59)  # 10-01 00:00 → 10-31 23:59 EDT
assert all(s.start_ms == BTC.start_ms and s.end_ms == BTC.end_ms for s in m.RANGE_MARKETS)
assert [s.slug for s in m.RANGE_MARKETS] == [f"what-price-will-{c}-hit-in-october-2026" for c in ("bitcoin", "ethereum", "solana", "hyperliquid")]
assert [(s.symbol, s.venue) for s in m.RANGE_MARKETS] == [("BTCUSDT", "spot"), ("ETHUSDT", "spot"), ("SOLUSDT", "spot"), ("HYPEUSDT", "futures")]
# the rules each category came with: BTC's "final Low ≤", the others' "final High ≥" (each market's own rules decide)
assert [s.default_dir for s in m.RANGE_MARKETS] == ["down", "up", "up", "up"]
assert [s.name for s in m.RANGE_MARKETS] == ["BTC 10月价格", "ETH 10月价格", "SOL 10月价格", "HYPE 10月价格"]
assert BTC.label(BTC.start_ms) == "10-01 00:00 ET（北京 10-01 12:00）" and BTC.label(BTC.end_ms) == "10-31 23:59 ET（北京 11-01 11:59）"

# --- a level from a market's title or question --------------------------------------------------------------------
levels = {"↑ 130,000": "130000", "↓ $100k": "100000", "$4.5K": "4500", "130K": "130000", "↑ 0.5": "0.5", "↓ 87.5": "87.5",
          "Will Solana reach $250 in October 2026?": "250", "Will Bitcoin dip to $95,000 in October?": "95000",
          "Will Ethereum hit $5,000 in October 2026?": "5000", "HYPE ↑ $60": "60", "Will Hyperliquid reach $62.5?": "62.5",
          "October 2026 ↑ 130k": "130000", "BTC above 130000 by Oct 31, 2026": "130000", "↑ $102,500": "102500"}
for title, want in levels.items():
    assert m.price_level(title) == D(want), (title, m.price_level(title))
for title in ("Yes", "October 2026", "Other", ""):
    assert m.price_level(title) is None, title
assert [m.level_label(v) for v in (130000, D("4500"), 250, 62.5, 102500, 0.85, 2800)] == \
    ["$130k", "$4.5k", "$250", "$62.5", "$102.5k", "$0.85", "$2.8k"]

# --- its direction: the market's own rules first, then its title or question; nothing guessed ---------------------
RULES = ('This market will resolve to "Yes" if any Binance 1 minute candle for {pair} during the month specified in the title '
         "(from 00:00 AM ET on the first day to 11:59 PM ET on the last) has a final {kind} price equal to or {cmp} than the "
         'price specified in the title. Otherwise, this market will resolve to "No".')
LOW, HIGH = RULES.format(pair="BTC/USDT", kind="Low", cmp="lower"), RULES.format(pair="ETH/USDT", kind="High", cmp="greater")
assert m.level_direction(LOW) == ("down", "规则") and m.level_direction(HIGH) == ("up", "规则")
assert m.level_direction(HIGH, "↓ 2,800") == ("down", "冲突")  # an arrow against the rules: shown as the arrow says, never suggested
assert m.level_direction(LOW, "↑ 130,000", "Will Bitcoin reach $130,000 in October 2026?") == ("up", "冲突")
assert m.level_direction(LOW, "$95,000", "Will Bitcoin reach $95,000 in October?") == ("down", "规则")  # a word is weaker than the rules
assert m.level_direction('has a final "High" price equal to or greater than') == ("up", "规则")  # quoted, as some rules spell it
assert m.level_direction("any 1 minute candle has a Low price equal to or less than the price") == ("down", "规则")
assert m.level_direction(LOW + " " + HIGH, "↓ $100k") == ("down", "标题")  # rules naming both say nothing about this market
assert m.level_direction("", "↑ 130,000") == ("up", "标题") and m.level_direction("", "$95k", "Will Bitcoin dip to $95,000?") == ("down", "标题")
assert m.level_direction("", "$250", "Will Solana reach $250 in October?") == ("up", "标题")
assert m.level_direction("", "$130k", "Will Bitcoin hit $130k in October?") == ("", "")  # "hit" says neither

# --- the touch model both ways: the textbook first-passage formula for a Brownian motion with drift μ = −σ²/2 ---------
def first_passage(barrier, mu, sigma, years):
    """P(the running max of μt + σW reaches barrier > 0 before T); a downward barrier by symmetry."""
    if barrier < 0:
        return first_passage(-barrier, -mu, sigma, years)
    s = sigma * math.sqrt(years)
    return m.norm_cdf((-barrier + mu * years) / s) + math.exp(2 * mu * barrier / sigma ** 2) * m.norm_cdf((-barrier - mu * years) / s)
for spot, level, sigma, years in ((112000, 100000, 0.45, 0.08), (3.1, 2.2, 1.1, 0.5), (60, 59.5, 0.9, 0.01)):
    want = first_passage(math.log(level / spot), -sigma ** 2 / 2, sigma, years)
    assert abs(m.low_probability(spot, level, sigma, years) - want) < 1e-12, (spot, level)
    up = first_passage(math.log(spot * spot / level / spot), -sigma ** 2 / 2, sigma, years)
    assert abs(m.hit_probability(spot, spot * spot / level, sigma, years) - up) < 1e-12
assert m.low_probability(100, 100, 0.5, 0.1) == 1.0 and m.low_probability(90, 100, 0.5, 0.1) == 1.0  # at or below already
assert m.low_probability(110, 100, 0.5, 0) == 0.0 and m.low_probability(110, 100, 0, 0.1) == 0.0
p = [m.low_probability(112000, level, 0.4, 0.08) for level in (110000, 105000, 100000, 90000)]
assert p == sorted(p, reverse=True) and 0.8 < p[0] < 1 and p[-1] < 0.1, p
assert m.low_probability(112000, 100000, 0.6, 0.08) > p[2] and m.low_probability(112000, 100000, 0.4, 0.04) < p[2]
assert abs(m.low_probability(100, 99.9999, 0.3, 1) - 1) < 1e-3  # a level next to the price is all but certain

# --- RangeMarket: a fake Binance (spot and futures answer alike) ------------------------------------------------------
START = BTC.start_ms
NOW = START + 50 * H + 10 * 60_000   # 10-03 02:10 EDT, the window's 51st hour running
PEAK, DIP, RUN = START + 10 * H, START + 30 * H, NOW - NOW % H


def bars(base=112000.0, peak=118500.0, dip=108000.0, running_high=119200.0):
    """Hourly klines from long before the window to the running hour: closes alternate ±0.4% (σ ≈ 37%); a spike to
    125k and a slump to 90k the hour before the window opens (never counted), the month's high ten hours in, its low
    thirty hours in, and a new high in the running hour."""
    out = []
    for t in range(START - 800 * H, NOW, H):
        close = base * (1.004 if t // H % 2 else 1.0)
        high, low = close * 1.001, close * 0.999
        if t == START - H: high, low = 125000.0, 90000.0
        if t == PEAK: high = peak
        if t == DIP: low = dip
        if t == RUN: high = running_high
        out.append([t, f"{close:.2f}", f"{high:.2f}", f"{low:.2f}", f"{close:.2f}", "1", t + H - 1, "0", 1, "0", "0", "0"])
    return out


def world(price="112050.4", rows=None):
    calls = []
    rows = bars() if rows is None else rows

    async def get(path, **params):
        calls.append((path, params))
        if path == "ticker/price":
            return {"symbol": params["symbol"], "price": price}
        assert path == "klines" and params["interval"] == "1h", (path, params)
        if "startTime" in params:  # Binance: bars opening inside [startTime, endTime], oldest first
            picked = [r for r in rows if params["startTime"] <= r[0] <= params["endTime"]]
            return picked[:params["limit"]]
        return rows[-params["limit"]:]
    return get, calls


async def run():
    store = m.Store(":memory:")
    rm = m.RangeMarket(store, BTC)
    rm.get, calls = world()
    await rm.refresh(NOW)
    assert rm.error == "" and rm.price == D("112050.4") and rm.priced_ms == NOW and rm.sigma_ms == NOW, rm.error
    assert 0.33 < rm.sigma < 0.42, rm.sigma
    # finished hours persisted; the running hour counts at once but is kept apart (it is read again until it is over)
    OPEN = float(next(r for r in bars() if r[0] == START)[1])  # the window's first price (not the 125k / 90k hour before it)
    assert rm.history == {"start": START, "through": RUN, "open": OPEN, "high": 118500.0, "high_at": PEAK, "low": 108000.0,
                          "low_at": DIP}, rm.history
    assert rm.reference() == OPEN
    assert rm.running == {"open": RUN, "high": 119200.0, "low": float(bars()[-1][3])} and rm.scanned_ms == NOW
    assert rm.extremes() == (119200.0, 108000.0) and rm.marks() == {"high": 119200.0, "high_at": RUN, "low": 108000.0, "low_at": DIP}
    assert rm.reached(D("119000"), "up") and not rm.reached(D("120000"), "up")
    assert rm.reached(D("108000"), "down") and not rm.reached(D("107999"), "down")
    # P(Yes): 1 once reached; else the touch model from the live price over what is left of the window
    years = (rm.window_end - NOW) / m.YEAR_MS
    assert rm.probability(D("118000"), "up", NOW) == 1.0 and rm.probability(D("108500"), "down", NOW) == 1.0
    assert rm.probability(D("125000"), "up", NOW) == m.hit_probability(112050.4, 125000.0, rm.sigma, years)
    assert rm.probability(D("100000"), "down", NOW) == m.low_probability(112050.4, 100000.0, rm.sigma, years)
    assert 0 < rm.probability(D("125000"), "up", NOW) < rm.probability(D("120000"), "up", NOW) < 1
    assert 0 < rm.probability(D("100000"), "down", NOW) < rm.probability(D("105000"), "down", NOW) < 1
    swing = rm.model_swing(D("125000"), "up", NOW, rm.probability(D("125000"), "up", NOW))
    assert 0.01 < swing < 0.2 and rm.model_swing(D("118000"), "up", NOW, 1.0) == 0.0, swing
    # the live price is part of the extremes while it is read inside the window
    rm.price = D("119500")
    assert rm.extremes()[0] == 119500.0 and rm.marks()["high_at"] == RUN and rm.probability(D("119400"), "up", NOW) == 1.0
    rm.price = D("112050.4")
    # a stale price prices nothing (levels reached stay reached)
    assert rm.probability(D("125000"), "up", NOW + rm.PRICE_STALE_MS + 1) is None and rm.probability(D("118000"), "up", NOW + rm.PRICE_STALE_MS + 1) == 1.0
    assert rm.advice_problem(NOW) == "" and not rm.complete()

    # the next scan reads only the new hours, and persists the hour that was running
    n = len(calls)
    await rm.scan(NOW + 2 * H)
    assert [c for c in calls[n:] if c[0] == "klines"] == [("klines", {"symbol": "BTCUSDT", "interval": "1h", "startTime": RUN,
                                                                   "endTime": NOW + 2 * H - 1, "limit": 1000})], calls[n:]
    assert rm.history["high"] == 119200.0 and rm.history["high_at"] == RUN and rm.history["through"] == NOW - NOW % H + H, rm.history
    assert rm.running == {}  # the fake has no newer bar: nothing running
    # the store survives a restart; a record from another window is ignored
    again = m.RangeMarket(store, BTC)
    assert again.history["high"] == 119200.0 and again.extremes() == (119200.0, 108000.0)
    saved = store.get(f"range:{BTC.slug}")
    store.put(f"range:{BTC.slug}", {"start": START - 31 * 24 * H, "through": START, "high": 1.0})
    assert again.history == {} and again.extremes() == (None, None)
    store.put(f"range:{BTC.slug}", saved)

    # why nothing may be suggested: extremes not read up to now, σ a day old, the window over
    rm.scanned_ms = NOW - rm.SCAN_STALE_MS - 1
    assert rm.advice_problem(NOW).startswith("本月最高/最低核验停在 10-03 ") and rm.advice_problem(NOW).endswith("；暂不给建议")
    rm.scanned_ms, rm.sigma_ms = NOW, NOW - m.DAY_MS - 1
    assert rm.advice_problem(NOW) == "波动率超过一天未更新；暂不给建议"
    rm.sigma_ms = NOW
    assert rm.advice_problem(rm.window_end) == "窗口已结束，等待结算"
    # once the window is over an open level is worth 0, but only when every hour of it has been read
    assert rm.probability(D("125000"), "up", rm.window_end) is None and rm.probability(D("118000"), "up", rm.window_end) == 1.0
    # nothing to read before the window opens
    early = m.RangeMarket(m.Store(":memory:"), BTC)
    early.get, ecalls = world()
    await early.scan(START - 60_000)
    early.sigma_ms = START - 60_000
    assert early.history == {} and ecalls == [] and early.advice_problem(START - 60_000) == ""  # no extremes to wait for yet

    # --- after the window: its last hour counts, later hours never do; the record is then complete ------------------
    ended = m.dataclasses.replace(BTC, slug="range-ended", end_ms=START + 20 * H - 60_000)  # "…23:59": the 20th hour's last minute
    done = m.RangeMarket(m.Store(":memory:"), ended)
    done.get, _ = world(rows=bars(peak=118500.0) + [])
    await done.scan(NOW)
    assert done.history["through"] == done.window_end == START + 20 * H and done.complete(), done.history
    # an error object instead of candles is not "no trades": the window stays unread
    broken = m.RangeMarket(m.Store(":memory:"), ended)
    async def error_object(path, **params):
        return {"code": -1003, "msg": "Too many requests"}
    broken.get = error_object
    try:
        await broken.scan(NOW); assert False
    except m.RemoteError as error:
        assert "格式异常" in str(error) and not broken.complete() and broken.history == {}
    assert done.history["high"] == 118500.0 and done.history["low"] > 108000.0 and done.running == {}, done.history  # the dip came later
    done.price, done.priced_ms, done.sigma, done.sigma_ms = D("100000"), NOW, 0.4, NOW  # read after the window: not part of it
    assert done.extremes()[1] > 100000.0 and done.probability(D("105000"), "down", NOW) == 0.0
    assert done.probability(D("118000"), "up", NOW) == 1.0  # reached inside the window
    done.priced_ms = ended.end_ms + 30_000  # read during the window's last minute: it counts
    assert done.extremes()[1] == 100000.0
    # the last hour itself counts
    last = m.RangeMarket(m.Store(":memory:"), m.dataclasses.replace(ended, slug="range-last", end_ms=PEAK + H - 60_000))
    last.get, _ = world()
    await last.scan(NOW)
    assert last.history["high"] == 118500.0 and last.history["high_at"] == PEAK and last.complete()

    # --- a failing source: the error is shown; the other parts still refresh ---------------------------------------
    flaky = m.RangeMarket(m.Store(":memory:"), BTC)
    good, _ = world()
    async def no_price(path, **params):
        if path == "ticker/price":
            raise m.RemoteError("HTTP 451: 部署所在地或接口访问受限")
        return await good(path, **params)
    flaky.get = no_price
    await flaky.refresh(NOW)
    assert flaky.price is None and flaky.sigma and flaky.history["high"] == 118500.0 and flaky.error.startswith("价格：HTTP 451"), flaky.error
    assert flaky.probability(D("125000"), "up", NOW) is None and flaky.probability(D("118000"), "up", NOW) == 1.0
    # σ that cannot be measured is asked for again a minute later (not on every 5-second tick); once known, 5 minutes
    novol = m.RangeMarket(m.Store(":memory:"), BTC)
    asked = []
    async def no_vol(path, **params):
        if path == "klines" and "startTime" not in params:
            asked.append(params)
            raise m.RemoteError("HTTP 451: 部署所在地或接口访问受限")
        return await good(path, **params)
    novol.get = no_vol
    await novol.refresh(NOW)
    await novol.refresh(NOW + 5_000)
    assert len(asked) == 1 and novol.sigma is None and "波动率：HTTP 451" in novol.error, novol.error
    assert abs(novol.times["vol"] - (time.monotonic() - m.RangeMarket.VOL_SECONDS + 60)) < 5
    novol.sigma = 0.4
    novol.times["vol"] = -1e9
    await novol.refresh(NOW + 10_000)
    assert len(asked) == 2 and abs(novol.times["vol"] - (time.monotonic() - m.RangeMarket.VOL_SECONDS + 300)) < 5
    assert "波动率：HTTP 451" in novol.error  # the reason stays on the card until σ is read again
    novol.get, novol.times["vol"] = good, -1e9
    await novol.refresh(NOW + 15_000)
    assert novol.error == "" and 0.33 < novol.sigma < 0.42

    # --- HYPE is a USDⓈ-M perpetual: its requests go to Binance futures, the others' to spot ----------------------
    urls = []
    async def fake_http(url, payload=None, timeout=15):
        urls.append(url)
        return {"code": -1121, "msg": "Invalid symbol."} if "BAD" in url else {"symbol": "HYPEUSDT", "price": "45.12"}
    async def fake_source(url, *a, **k):
        urls.append(url)
        return '{"symbol": "ETHUSDT", "price": "4100.5"}'
    real_http, real_source = m.http_json, m.fetch_source
    m.http_json, m.fetch_source = fake_http, fake_source
    try:
        assert (await m.RangeMarket(m.Store(":memory:"), HYPE).get("ticker/price", symbol="HYPEUSDT"))["price"] == "45.12"
        assert (await m.RangeMarket(m.Store(":memory:"), ETH).get("ticker/price", symbol="ETHUSDT"))["price"] == "4100.5"
        try:
            await m.binance_futures("ticker/price", symbol="BAD"); assert False
        except m.RemoteError as error:
            assert str(error) == "币安合约错误 -1121: Invalid symbol.", error
    finally:
        m.http_json, m.fetch_source = real_http, real_source
    assert urls[0] == "https://fapi.binance.com/fapi/v1/ticker/price?symbol=HYPEUSDT" and "/api/v3/ticker/price?symbol=ETHUSDT" in urls[1], urls

    # --- the bot: Predict levels, directions, the card --------------------------------------------------------------
    class FakeMarket:
        def __init__(self): self.config, self.paths = None, []
        def now_ms(self): return NOW
        async def get(self, path, **params):
            self.paths.append((path, params))
            return {"symbol": params.get("symbol"), "price": "45.12"}
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off"})
    market = FakeMarket()
    bot = m.Bot(cfg, m.Store(":memory:"), market, None)
    targets = bot.predict_targets(NOW)
    assert all(targets[s.key] == s.slug and s.key in bot.predict.ladder_keys for s in m.RANGE_MARKETS)
    assert all(bot.predict.ladder_parse[s.key] is m.price_level for s in m.RANGE_MARKETS)
    # HYPE goes through the bot's own futures feed (BINANCE_BASE_URL, its rate-limit cooldown)
    assert (await bot.ranges["HYPE-HIT-10"].get("ticker/price", symbol="HYPEUSDT"))["price"] == "45.12"
    assert market.paths == [("/fapi/v1/ticker/price", {"symbol": "HYPEUSDT"})]
    q = lambda what: f"Will Bitcoin {what} in October 2026?"
    markets = [  # id, title, question, rules, outcome order
        ("21", "↑ 120,000", q("reach $120,000"), HIGH, "Yes"),
        ("22", "↓ 105,000", q("dip to $105,000"), LOW, "No"),        # lists "No" first: its book is No's
        ("23", "$118,000", "Bitcoin $118,000 in October 2026?", HIGH, "Yes"),  # only the rules say ↑
        ("24", "↓ 108,500", q("dip to $108,500"), LOW, "Yes"),
        ("25", "$100,000", "Bitcoin $100,000 in October 2026?", "", "Yes"),   # nothing says: below the month's first price, so ↓
        ("26", "Other", "Other", "", "Yes"),                                   # no level: left out
        ("29", "↑ 125,000", q("reach $125,000"), LOW, "Yes"),                 # a ↑ title under "Low ≤" rules: flagged, never suggested
        ("30", "↑ 160,000", q("reach $160,000"), HIGH, "Yes"),                # far out: a lone Yes ask at 99.8¢, no bids
        ("31", "↑ 110,000", q("reach $110,000"), HIGH, "Yes"),                # reached; its book is empty (no quotes at all)
    ]
    books = {"21": (["0.20", "500"], ["0.24", "500"]), "22": (["0.50", "500"], ["0.55", "500"]), "23": (["0.97", "500"], ["0.99", "500"]),
             "24": (["0.30", "500"], ["0.35", "500"]), "25": (["0.05", "500"], ["0.08", "500"]), "26": (["0.5", "1"], ["0.6", "1"]),
             "29": (["0.10", "500"], ["0.12", "500"]), "30": ([], ["0.998", "50"]), "31": ([], [])}

    async def fetch(url, payload=None):
        if url == m.PREDICT_GRAPHQL:
            v = payload["variables"]
            if "id" in v:
                return {"data": {"category": {"id": "77", "__typename": "MultiCategory"} if v["id"] == BTC.slug else None}}
            return {"data": {"markets": {"edges": [{"node": {"id": i, "conditionId": "0x" + i, "title": t, "question": qq}}
                                                   for i, t, qq, _, _ in markets]}}}
        for i, t, qq, rules, first in markets:
            if url.endswith(f"/markets/{i}"):
                names = [first, "No" if first == "Yes" else "Yes"]
                return {"data": {"id": i, "status": "OPEN", "title": t, "question": qq, "description": rules,
                                 "outcomes": [{"name": names[0], "indexSet": 1}, {"name": names[1], "indexSet": 2}]}}
            if url.endswith(f"/markets/{i}/orderbook"):
                return {"data": {"bids": [books[i][0]] if books[i][0] else [], "asks": [books[i][1]] if books[i][1] else []}}
        if "/categories/" in url:
            raise m.RemoteError("HTTP 404: 接口请求失败")
        raise AssertionError(url)
    bot.predict.fetch = fetch
    await bot.predict.refresh({"BTC-HIT-10": BTC.slug}, force=True)
    rows = bot.predict.ladders["BTC-HIT-10"]
    assert [(r.target, r.market_id) for r in rows] == [(D("100000"), "25"), (D("105000"), "22"), (D("108500"), "24"),
                                                       (D("110000"), "31"), (D("118000"), "23"), (D("120000"), "21"),
                                                       (D("125000"), "29"), (D("160000"), "30")], rows
    assert rows[1].question == q("dip to $105,000") and "BTC-HIT-10" not in bot.predict.errors
    assert bot.predict.market_meta["21"][0]["rules"] == HIGH and bot.predict.market_meta["23"][0]["question"].startswith("Bitcoin $118,000")
    assert [bot.range_level(rm, r) for r in rows] == [("down", "推断"), ("down", "规则"), ("down", "规则"), ("up", "规则"), ("up", "规则"),
                                                      ("up", "规则"), ("up", "冲突"), ("up", "规则")]
    # a level nothing labels is guessed from the side of the month's first price it sits on (a ↓ level above that price
    # would have been reached at once); with no price at all, the category's default
    bare_row = m.LadderRow(D("130000"), "", "$130,000", None)
    assert bot.range_level(rm, bare_row) == ("up", "推断")
    assert bot.range_level(bot.ranges["BTC-HIT-10"], bare_row) == ("down", "默认")  # BTC's rules said "Low ≤"
    assert bot.range_level(bot.ranges["ETH-HIT-10"], bare_row) == ("up", "默认")
    bot.predict.ladders["BTC-HIT-10"] = [m.dataclasses.replace(r, book=m.dataclasses.replace(r.book, fetched_ms=NOW)) for r in rows]
    bot.ranges["BTC-HIT-10"] = rm

    item = bot.range_payload(rm, NOW)
    assert {k: item[k] for k in ("name", "symbol", "group", "kind", "close_ms", "quote_ms", "source")} == {
        "name": "BTC 10月价格", "symbol": "BTC-HIT-10", "group": "levels", "kind": "ladder", "close_ms": BTC.end_ms, "quote_ms": NOW,
        "source": "币安现货"} and "missing" not in item, item
    assert item["close_label"] == "10-31 23:59 ET（北京 11-01 11:59）这根 1 分钟 K 为止", item["close_label"]
    assert item["predict"]["url"].startswith("https://predict.fun/zh-cn/market/what-price-will-bitcoin-hit-in-october-2026")
    L = item["ladder"]
    assert {k: L[k] for k in ("kind", "price", "high", "low", "symbol", "venue", "window", "hold", "error")} == {
        "kind": "price", "price": "$112,050", "high": "$119,200", "low": "$108,000", "symbol": "BTCUSDT", "venue": "币安现货",
        "window": "10-01 00:00 ET（北京 10-01 12:00）起", "hold": "", "error": ""}, L
    assert L["high_at"] == m.stamp(RUN, seconds=False) and L["low_at"] == m.stamp(DIP, seconds=False) and L["through"] == m.stamp(NOW - NOW % H + H, seconds=False)
    assert abs(L["years"] - years) < 1e-12 and L["sigma"] == rm.sigma and "waiting" not in L
    r160, r125, r120, r118, r110, r1085, r105, r100 = L["rows"]  # high to low: the price sits between the ↑ and the ↓ levels
    assert [r["label"] for r in L["rows"]] == ["↑ $160k", "↑ $125k", "↑ $120k", "↑ $118k", "↑ $110k", "↓ $108.5k", "↓ $105k", "↓ $100k"]
    assert [(r["dir"], r["dir_source"]) for r in L["rows"]] == [("up", "规则"), ("up", "冲突"), ("up", "规则"), ("up", "规则"), ("up", "规则"),
                                                              ("down", "规则"), ("down", "规则"), ("down", "推断")]
    assert [r["dir_note"] == "" for r in L["rows"]] == [True, False, True, True, True, True, True, False]
    # only taker edges may be suggested on a price ladder: ↑ $120k's best is 吃Yes although 挂Yes is listed with a larger edge
    assert r120["makers"] is False and [e["label"] for e in r120["edges"] if e["best"]] == ["吃Yes"], r120["edges"]
    assert max(r120["edges"], key=lambda e: e["edge"])["label"] == "挂Yes"
    # the far level: a lone ask at 99.8¢ gives "挂No at 0.2¢ for +99.6¢" on paper; it is never best, framed or traded
    assert r160["fair"] < 0.01 and r160["bid"] is None and r160["ask"] == 0.998 and not any(e["best"] for e in r160["edges"]), r160
    assert {e["label"]: round(e["edge"], 3) for e in r160["edges"] if e["maker"]} == {"挂No": round(1 - r160["fair"] - 0.002, 3)}
    # reached, and the book has no quotes at all: nothing disputes it (the market is likely settled already)
    assert r110["fair"] == 1.0 and r110["touched"] is True and r110["error"] == "" and r110["bid"] is None and r110["ask"] is None, r110
    assert L["spot"] == 112050.4  # the page draws the price's own line between the ↑ and the ↓ levels
    # a title arrow against the rules text: priced as the title says, shown with the warning, never suggested
    assert r125["hold"] == r125["dir_note"] == m.RANGE_GUESS["冲突"] and r125["edges"] and not any(e["best"] for e in r125["edges"]), r125
    assert r125["fair"] == rm.probability(D("125000"), "up", NOW)
    # an open ↑ level: the touch model, the Yes book, the four directions, a suggestion
    assert r120["fair"] == rm.probability(D("120000"), "up", NOW) and abs(r120["dist"] - (120000 / 112050.4 - 1)) < 1e-12
    assert r120["bid"] == 0.20 and r120["ask"] == 0.24 and r120["hold"] == "" and r120["touched"] is False
    assert {e["label"] for e in r120["edges"]} == {"挂Yes", "挂No", "吃Yes", "吃No"} and any(e["best"] for e in r120["edges"]), r120
    assert r120["need"] >= r120["swing"] > 0 and r120["swing"] == rm.model_swing(D("120000"), "up", NOW, r120["fair"])
    # reached, and the book agrees: folded into the "已触及" line
    assert r118["fair"] == 1.0 and r118["touched"] is True and r118["error"] == ""
    # reached by our candles, but the book still trades at 30-35¢: flagged, never a +65¢ "edge"
    assert r1085["fair"] == 1.0 and "请核实" in r1085["error"] and "edges" not in r1085 and r1085["touched"] is False, r1085
    # an open ↓ level of a market listing "No" first: the book turned to Yes (Yes bid = 1 − No ask)
    assert r105["bid"] == 0.45 and r105["ask"] == 0.50 and r105["fair"] == rm.probability(D("105000"), "down", NOW) and r105["dist"] < 0
    # a level whose direction is only a guess: priced and shown, never suggested
    assert r100["hold"] == r100["dir_note"] == "这个市场的规则和标题都没写明上破还是下破，按档位在月初价格之上（↑）还是之下（↓）推断；只作参考"
    assert r100["edges"] and not any(e["best"] for e in r100["edges"])
    assert r100["fair"] == rm.probability(D("100000"), "down", NOW)

    # a market Predict settled before the window closed: its own result when readable, else reached (only a touch
    # settles one early); nothing is suggested on it any more
    row21 = next(r for r in bot.predict.ladders["BTC-HIT-10"] if r.market_id == "21")
    meta21 = bot.predict.market_meta["21"][0]
    bot.predict.market_meta["21"] = ({**meta21, "status": "RESOLVED"}, time.monotonic())
    assert bot.range_fair(rm, row21, "up", NOW) == 1.0
    assert bot.range_fair(rm, row21, "up", rm.window_end) is None  # after it: the data decides
    for name, want in (("Yes", 1.0), ("No", 0.0), ("50/50", 0.5)):
        resolved = {"index": None if name == "50/50" else 0, "name": name, "split": name == "50/50", "how": "outcomes.status"}
        bot.predict.market_meta["21"] = ({**meta21, "status": "RESOLVED", "resolved": resolved}, time.monotonic())
        assert bot.range_fair(rm, row21, "up", NOW) == want, name
    r = next(x for x in bot.range_payload(rm, NOW)["ladder"]["rows"] if x["label"] == "↑ $120k")
    assert r["hold"] == "Predict 已结算" and not any(e["best"] for e in r["edges"]) and r["fair"] == 0.5, r
    assert next(mk for mk in bot.sim_markets(NOW) if mk.market == f"{BTC.slug}#21").hold == "Predict 已结算"
    bot.predict.market_meta["21"] = (meta21, time.monotonic())
    # extremes not read up to now: every level waits, reached ones still show as reached
    rm.scanned_ms = NOW - rm.SCAN_STALE_MS - 1
    held = bot.range_payload(rm, NOW)["ladder"]
    assert held["hold"].startswith("本月最高/最低核验停在") and all(not any(e["best"] for e in r.get("edges", [])) for r in held["rows"])
    assert all(r["hold"] == held["hold"] for r in held["rows"] if "edges" in r)
    assert next(r for r in held["rows"] if r["label"] == "↑ $118k")["touched"] is True
    rm.scanned_ms = NOW
    # once the window is over nothing is suggested; open levels are unknown until its last hours are read, then 0
    after = bot.range_payload(rm, rm.window_end + 60_000)["ladder"]
    by = {r["label"]: r for r in after["rows"]}
    assert after["hold"] == "窗口已结束，等待结算" and by["↑ $120k"]["fair"] is None and by["↑ $118k"]["fair"] == 1.0
    assert "edges" not in by["↑ $120k"] and by["↑ $120k"]["bid"] == 0.20
    # a stale Binance price: the card says so; reached levels stay reached
    stale = bot.range_payload(rm, NOW + rm.PRICE_STALE_MS + 1)
    by = {r["label"]: r for r in stale["ladder"]["rows"]}
    assert stale["missing"].startswith("币安价格停在 ") and by["↑ $118k"]["fair"] == 1.0 and by["↑ $120k"]["fair"] is None
    # before any data: the card says what it waits for, and that Predict's levels are not in yet
    bare = bot.range_payload(bot.ranges["ETH-HIT-10"], NOW)
    assert bare["missing"] == "等待币安行情" and bare["ladder"]["rows"] == [] and bare["ladder"]["waiting"] == "等待 Predict 档位", bare
    assert bare["ladder"]["price"] == bare["ladder"]["high"] == "—" and bare["source"] == "币安现货"
    assert bot.range_payload(bot.ranges["HYPE-HIT-10"], NOW)["source"] == "币安合约"
    bot.predict.errors["SOL-HIT-10"] = f"Predict 上还没有这个市场（{SOL.slug}）"
    unlisted = bot.range_payload(bot.ranges["SOL-HIT-10"], NOW)  # the reason is on the card's Predict line, once
    assert unlisted["ladder"]["waiting"] == "等待 Predict 档位" and unlisted["predict"]["error"] == f"Predict 上还没有这个市场（{SOL.slug}）"
    del bot.predict.errors["SOL-HIT-10"]
    sol = m.RangeMarket(m.Store(":memory:"), SOL)
    sol.error = "价格：网络错误 (URLError)"
    assert bot.range_payload(sol, NOW)["missing"] == "等待币安行情（价格：网络错误 (URLError)）"

    # the page's list: the four cards (and the STRC stock market) in their own section, between 加密 and 市值阶梯
    payload = bot.odds_payload()
    groups = [i["group"] for i in payload["items"]]
    names = [i["name"] for i in payload["items"] if i["group"] == "levels"]
    assert names == ["BTC 10月价格", "ETH 10月价格", "SOL 10月价格", "HYPE 10月价格", "STRC 触及 $100"] and groups.index("levels") > groups.index("crypto"), groups
    assert max(i for i, g in enumerate(groups) if g == "levels") < groups.index("ladder")
    m.json.dumps(payload)
    # BNB_TOUCH=off switches them off with the other crypto markets
    off = m.Bot(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off",
                                   "BNB_TOUCH": "off"}), m.Store(":memory:"), FakeMarket(), None)
    assert not any(i["group"] == "levels" for i in off.odds_payload()["items"])
    assert "BTC-HIT-10" not in off.predict_targets(NOW) and "BTC-HIT-10" not in off.predict.ladder_keys
    assert "价格阶梯" not in [name for name, _ in off.reference_jobs()] and "价格阶梯" in [name for name, _ in bot.reference_jobs()]

    # --- the paper trader sees every priced level exactly as the card does -----------------------------------------
    sims = {mk.market: mk for mk in bot.sim_markets(NOW) if mk.kind == "range"}
    assert set(sims) == {f"{BTC.slug}#{i}" for i in ("21", "22", "23", "24", "25", "29", "30", "31")}, sorted(sims)
    assert sims[f"{BTC.slug}#29"].hold == "标题与规则的方向相反"
    assert all(not mk.makers for mk in sims.values()) and sims[f"{BTC.slug}#30"].hold == ""
    # the paper trader buys the taker at ↑ $120k (+25¢) and never the far level's 挂No at 0.2¢; the alerts say the same
    await bot.sim_step(NOW)
    assert bot.sim_trades() == {}  # price ladders are outside the paper trader's default scope (SIM_MARKETS=close)
    bot.config = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off",
                                    "SIM_MARKETS": "range", "SIM_WAYS": "both"})  # even with makers allowed: a ladder level takes none
    bot.sim_ran = -1e9
    await bot.sim_step(NOW)
    trades = bot.sim_trades()
    assert set(trades) == {f"{BTC.slug}#21|up|吃"}, sorted(trades)
    assert trades[f"{BTC.slug}#21|up|吃"]["maker"] is False and trades[f"{BTC.slug}#21|up|吃"]["edge"] > 0.2
    await bot.edge_alerts(NOW)
    bot.edge_ran = -1e9
    await bot.edge_alerts(NOW + 61_000)  # the edge held for the confirmation time: announced
    alerts = bot.store.get("edgealerts", {})
    text = alerts[f"{BTC.slug}#21"]["alert"]["text"].replace(m.B0, "").replace(m.B1, "")  # without the bold markers
    assert text.startswith("🟢 新机会｜BTC 10月价格 ↑ $120k\n吃Yes @ 24.0¢｜净优势 +25.2¢") and "挂" not in text, text
    assert not any(st.get("alert") or st.get("pending") or st.get("told") for mkt, st in alerts.items() if mkt != f"{BTC.slug}#21"), alerts
    s120, s1085, s100 = sims[f"{BTC.slug}#21"], sims[f"{BTC.slug}#24"], sims[f"{BTC.slug}#25"]
    assert s120.item == "BTC 10月价格 ↑ $120k" and s120.key == "BTC-HIT-10" and s120.hold == "" and s120.sides == ("Yes", "No")
    assert s120.fair_up == r120["fair"] and s120.need == r120["need"] and s120.settle == {"target": "120000", "dir": "up", "end": BTC.end_ms}
    assert s1085.hold == "数据显示已触及，但盘口仍低于 90¢" and s100.hold == "方向是推断的" and sims[f"{BTC.slug}#23"].hold == ""
    ev = s120.evidence
    assert ev["basis"]["level"] == 120000.0 and ev["basis"]["dir"] == "up" and ev["basis"]["dir_source"] == "规则" and ev["proxy"] is None, ev
    assert ev["basis"]["window_high"] == 119200.0 and ev["basis"]["window_low"] == 108000.0 and ev["basis"]["close_ms"] == rm.window_end
    assert [(e["what"], e["source"], e["price"]) for e in ev["sources"]] == [("现价", "币安现货", 112050.4), ("本月最高", "币安小时 K", 119200.0),
                                                                           ("本月最低", "币安小时 K", 108000.0)], ev["sources"]
    # results from the bot's own candles: reached → Yes at once; not reached → No once the whole window is read
    trade = lambda target, direction, mid="21": {"kind": "range", "key": "BTC-HIT-10", "market": f"{BTC.slug}#{mid}",
                                                 "settle": {"target": target, "dir": direction, "end": BTC.end_ms}}
    up, note, proof = bot.sim_result(trade("118000", "up", "23"), NOW)
    assert up == 1.0 and note == "↑ $118k 已触及（BTCUSDT 本月最高 119,200）" and proof["high"] == 119200.0 and proof["high_at"] == RUN, (note, proof)
    assert bot.sim_result(trade("108500", "down", "24"), NOW) is None  # reached by our candles, but the book trades it at 30-35¢: Predict decides
    assert bot.sim_result(trade("110000", "up", "31"), NOW)[0] == 1.0  # an empty book disputes nothing
    assert proof["rule"] == "窗口内任一 1 分钟 K 的最高价 ≥ $118k 即 Yes" and proof["source"].startswith("币安现货 BTCUSDT 小时 K")
    up, note, _ = bot.sim_result(trade("108500", "down", "no-such-market"), NOW)  # no book to dispute it
    assert up == 1.0 and note == "↓ $108.5k 已触及（BTCUSDT 本月最低 108,000）", note
    assert bot.sim_result(trade("120000", "up"), NOW) is None  # open
    assert bot.sim_result(trade("120000", "up"), rm.window_end + bot.SIM_SETTLE_MS + 1) is None  # the window not read to its end
    bot.ranges["BTC-HIT-10"] = done  # a window read to its end without 120k
    up, note, proof = bot.sim_result(trade("120000", "up"), done.window_end + bot.SIM_SETTLE_MS + 1)
    assert up == 0.0 and note == "整个窗口都没到 $120k" and proof["through"] == done.window_end, (note, proof)
    assert bot.sim_result(trade("120000", "up"), done.window_end + 1) is None  # settled an hour after the close
    bot.ranges["BTC-HIT-10"] = rm
    assert bot.sim_result({**trade("1", "up"), "key": "GONE"}, NOW) is None
    assert m.SIM_KINDS["range"] == "价格阶梯"
    # Predict's own word on a level: "Yes" / "No"
    assert bot.resolution_up(trade("120000", "up"), {"name": "Yes"}) == 1.0 and bot.resolution_up(trade("120000", "up"), {"name": "No"}) == 0.0

    # --- the background job: every pair refreshed, failures named --------------------------------------------------
    for key, r in bot.ranges.items():
        r.get, _ = world(rows=bars())
    bot.ranges["SOL-HIT-10"].get = no_price
    result = await bot.refresh_ranges(NOW)
    assert result.status == "partial" and "SOL-HIT-10：价格：HTTP 451" in result.error, result
    return bot


async def browser_check(bot):
    """The price ladder card: ↑ levels above the price, ↓ below, signed distances, reached levels as chips, the default
    direction and the 请核实 warning marked; no sideways scroll on a phone or a wide screen."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        async_playwright = None
    chrome = next((p for p in [os.environ.get("CHROMIUM_PATH", ""), "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"]
                   if p and os.path.exists(p)), "")
    if async_playwright is None or not (chrome or os.environ.get("PLAYWRIGHT_BROWSERS_PATH")):
        print("browser check skipped (no Playwright/Chromium)")
        return
    bot.market.now_ms = lambda: NOW
    payload = bot.odds_payload()
    jia = {"name": "甲", "symbol": "JIA", "group": "index", "missing": "等待行情", "predict": {"url": "https://predict.fun/zh-cn/market/x", "error": ""}}
    book = m.PredictBook("JIA", "x", "1", "x", ((D("0.30"), D("500")),), ((D("0.95"), D("500")),), NOW, 200)
    bot.book_block(jia["predict"], book, 0.5, bot.edge_need(0.0), 0.0, "", ("涨", "跌"), NOW)  # 挂跌 @ 5¢ +45¢, 挂涨 @ 30¢ +20¢; no taker edge
    assert jia["predict"]["points_active"] is None and jia["predict"]["points_note"] == "积分状态暂缺"  # every block carries its points
    jia["predict"].update(points_active=True, points_ok=True, points_rate=120, points_note="积分已激活", points_spread=0.06, points_min_shares=100)
    payload["items"] = [jia, *(i for i in payload["items"] if i["group"] == "levels")]
    btc = next(i for i in payload["items"] if i["name"] == "BTC 10月价格")
    tk = next(e for e in next(r for r in btc["ladder"]["rows"] if r["label"] == "↑ $120k")["edges"] if e["best"])
    bot.odds_payload = lambda: payload
    web = m.WebServer(bot, 0, "t" * 20); web.CACHE_SECONDS = {}; port = await web.start()  # the tests change the payload and reload at once
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**({"executable_path": chrome} if chrome else {}))
        page = await browser.new_page(viewport={"width": 390, "height": 900})
        await page.add_init_script("if(!localStorage.getItem('levelsDefault')){localStorage.setItem('hideSec','[\"sim\"]');localStorage.setItem('levelsDefault','1')}")
        await page.goto(f"http://127.0.0.1:{port}/p/{'t' * 20}")
        await page.wait_for_selector("#g-levels .pg")  # (价格阶梯 is hidden by default: the init script above shows it)
        assert await page.inner_text("#h-levels .hn") == "价格阶梯"
        card = page.locator("#g-levels .card").first
        assert await card.locator(".name").inner_text() == "BTC 10月价格"
        assert re.sub(r"\s+", " ", await card.locator(".price-top").inner_text()).strip() == "现价 $112,050"
        assert re.sub(r"\s+", " ", await card.locator(".price-range").inner_text()).strip() == "本月最高 $119,200 最低 $108,000"
        assert await card.locator(".price-model summary").inner_text() == "规则与计算明细"
        assert not await card.locator(".price-model").evaluate("e => e.open")
        labels = await card.locator(".pg .lt").all_inner_texts()
        assert labels == ["↑ $160k", "↑ $125k", "↑ $120k", "↓ $108.5k", "↓ $105k", "↓ $100k"], labels  # ↑ $118k, ↑ $110k reached: chips
        # the price's own line sits between the ↑ levels and the ↓ levels
        assert await card.locator(".pg .lt, .pg .lspot").all_inner_texts() == ["↑ $160k", "↑ $125k", "↑ $120k", "现价 $112,050", "↓ $108.5k",
                                                                               "↓ $105k", "↓ $100k"]
        # Distances stay with each target, including on a phone; points unknown here (no rewards in the fixture): no pill
        dists = await card.locator(".pg .pdist").all_inner_texts()
        assert dists == ["+43%", "+12%", "+7.1%", "−3.2%", "−6.3%", "−11%"], dists
        assert await card.locator(".pg .ppoints").count() == 0
        assert await card.locator(".pg .lh").all_inner_texts() == ["目标", "模型", "挂单", "吃单"]
        # 甲 (an index card) carries its points on its Predict line: ● with the hourly rate
        pill = await page.locator("#g-index .quote .ppoints.active").inner_text()
        assert pill == "● 120 PP/h", repr(pill)
        assert "每小时发 120 PP" in await page.locator("#g-index .quote .ppoints.active").get_attribute("title")
        assert not await card.locator(".touched").evaluate("e => e.open")
        await card.locator(".touched summary").click()
        assert await card.locator(".touched .tchip").all_inner_texts() == ["↑ $118k", "↑ $110k"]
        assert await card.locator(".touched .tchip").first.get_attribute("title") == "本月最高价已到 $118k"
        await card.locator(".touched summary").click()
        assert await card.locator(".pg .ptarget:has(.lt:text-is('↓ $100k'))").get_attribute("title") == \
            "这个市场的规则和标题都没写明上破还是下破，按档位在月初价格之上（↑）还是之下（↓）推断；只作参考"
        cells = await card.locator(".pg .paction").evaluate_all("els => els.map(e => [e.className, e.title, e.innerText])")
        assert "pos" not in cells[11][0] and "hot" not in cells[11][0], cells
        assert "hot" in cells[5][0] and "pos" not in cells[3][0], cells  # ↑ $120k's taker at 24¢; ↑ $125k disputed
        # An unqualified maker does not fill the overview with a hypothetical 99¢ advantage.
        assert "disabled" in cells[0][0] and cells[0][2] == "—" and "积分状态暂缺" in cells[0][1], cells[0]
        assert "Yes" in cells[1][2] and "99.8¢" in cells[1][2] and "未过建议门槛" in cells[1][1] and "未过门槛" not in cells[1][2], cells[1]
        await card.locator(".pg .lt:text-is('↑ $160k')").click()
        chips = card.locator(".lrow .edge")
        assert await chips.count() == 2 and await card.locator(".lrow .edge.best, .lrow .edge.pos").count() == 0
        assert (await chips.nth(0).inner_text()).startswith("挂No 0.2\n+99.")
        await chips.nth(0).click()
        det = await card.locator(".lrow .edet").inner_text()
        far = next(r for r in btc["ladder"]["rows"] if r["label"] == "↑ $160k")
        assert (far.get("maker_note") or "这类档位的挂单不算建议") in det and "→ 不计入" in det, det
        await card.locator(".pg .lt:text-is('↑ $160k')").click()
        # tap the ⚠️ level: its note says why (a tooltip is no use on a phone); no directions are offered on it
        await card.locator(".pg .lt:text-is('↓ $108.5k')").click()
        note = await card.locator(".lrow .lnote").inner_text()
        assert note.startswith("↓ $108.5k：现价还要跌 3.2% 才碰到 · 模型 Yes 100.0¢ · Yes 盘口 30.0 / 35.0") and "⚠️ 数据显示已触及，但盘口仍低于 90¢" in note, note
        assert await card.locator(".lrow .edge").count() == 0
        await card.locator(".pg .lt:text-is('↓ $108.5k')").click()
        assert await card.locator(".lrow").count() == 0
        # the disputed level: its note, then its four directions, none of them suggested
        await card.locator(".pg .lt:text-is('↑ $125k')").click()
        note = await card.locator(".lrow .lnote").inner_text()
        assert "现价还要涨 11.6% 才碰到" in note and "标题的箭头和规则写的方向相反" in note, note
        assert await card.locator(".lrow .edge").count() == 4 and await card.locator(".lrow .edge.best").count() == 0
        await card.locator(".lrow .edge").nth(0).click()
        assert "暂不建议：标题的箭头和规则写的方向相反" in await card.locator(".lrow .edet").inner_text()
        await card.locator(".pg .lt:text-is('↑ $125k')").click()
        # the strip on top lists every red-framed suggestion; tapping one jumps to its card (hidden while customising)
        strip = await page.inner_text("#opps")
        # makers (挂单) and takers (吃单) are listed apart: 甲's maker under 挂单, then BTC's taker under 吃单
        assert strip.startswith("🔥 机会 2") and "挂单 1" in strip and "吃单 1" in strip, strip
        assert "甲挂跌 5.0+45.0¢" in strip.replace("\n", "") and f"↑ $120k {tk['label']} {tk['price'] * 100:.1f}" in strip, strip
        assert strip.index("挂单 1") < strip.index("甲") < strip.index("吃单 1") < strip.index("BTC 10月价格") and m.cents(tk["edge"], True) in strip
        # each side on a row of its own: the heading and 挂单 on the first, 吃单 on the second (one below the other on screen)
        rows = page.locator("#opps .orow")
        assert await rows.count() == 2 and (await rows.nth(0).inner_text()).startswith("🔥 机会 2\n挂单 1") and "吃单" not in await rows.nth(0).inner_text()
        assert (await rows.nth(1).inner_text()).startswith("吃单 1") and await page.locator("#opps > .ok, #opps > .og, #opps > .opp").count() == 0
        assert (await rows.nth(0).bounding_box())["y"] + (await rows.nth(0).bounding_box())["height"] <= (await rows.nth(1).bounding_box())["y"]
        # however many there are, every one is listed (no "还有 N 个"): nine makers and seven takers wrap onto more lines,
        # largest first, all inside the strip at phone width; the next render puts the page's own back
        await page.evaluate("""() => { hots = Array.from({length: 9}, (_, i) => ({key: "X" + i, name: "市场" + i, group: "index", url: "",
            maker: {text: "挂跌 50.0", edge: 0.3 - i / 100, points: 1}, taker: i < 7 ? {text: "吃跌 52.0", edge: 0.2 - i / 100} : null})); drawOpps() }""")
        strip = await page.inner_text("#opps")
        assert strip.startswith("🔥 机会 16\n挂单 9") and "吃单 7" in strip and "还有" not in strip, strip
        assert await rows.nth(0).locator(".opp b").all_inner_texts() == [f"市场{i}" for i in range(9)]
        assert await rows.nth(1).locator(".opp b").all_inner_texts() == [f"市场{i}" for i in range(7)]
        assert (await rows.nth(0).bounding_box())["height"] > 3 * (await rows.nth(0).locator(".opp").first.bounding_box())["height"]
        assert await page.evaluate("""[...document.querySelectorAll('#opps .opp')].every(b => { const s = document.getElementById('opps')
            .getBoundingClientRect(), r = b.getBoundingClientRect(); return r.left >= s.left - 0.5 && r.right <= s.right + 0.5 })""")
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        await page.evaluate("render(last)")
        assert (await page.inner_text("#opps")).startswith("🔥 机会 2\n挂单 1") and await page.locator("#opps .opp").count() == 2
        # each entry is a link to the market on Predict (a new tab); the tap also brings the card into view here
        opp = page.locator("#opps .opp").nth(1)
        assert (await opp.get_attribute("href") or "").startswith("https://predict.fun/") and await opp.get_attribute("target") == "_blank"
        await opp.click(); await page.wait_for_timeout(300)
        assert await card.evaluate("c => c.classList.contains('flash') && c.style.scrollMarginTop !== ''")
        for extra in page.context.pages[1:]:  # the new tab the link opened (no network here): closed again
            await extra.close()
        # 自定义: leaving makers out drops 甲 (its edges are maker ones); a section can be left out; takers can go too; hidden while customising
        await page.click("#edit"); assert await page.is_hidden("#opps")
        await page.uncheck("#opp-maker"); await page.click("#done")
        strip = await page.inner_text("#opps")
        assert strip.startswith("🔥 机会 1") and "挂单" not in strip and "甲" not in strip and "吃单 1" in strip, strip
        assert await page.evaluate("localStorage.getItem('oppMakers')") == "false"
        await page.click("#edit"); await page.uncheck("#oppsrc input[data-opp=levels]"); await page.click("#done")
        assert await page.is_hidden("#opps") and await page.evaluate("localStorage.getItem('oppOff')") == '["levels"]'
        await page.click("#edit"); await page.check("#opp-maker"); await page.click("#done")
        assert (await page.inner_text("#opps")).startswith("🔥 机会 1") and "甲" in await page.inner_text("#opps")
        await page.click("#edit"); await page.uncheck("#opp-taker"); await page.check("#oppsrc input[data-opp=levels]"); await page.click("#done")
        strip = await page.inner_text("#opps")
        assert strip.startswith("🔥 机会 1") and "吃单" not in strip and "BTC 10月价格" not in strip, strip
        assert await page.evaluate("localStorage.getItem('oppTakers')") == "false"
        await page.click("#edit"); await page.click("#reset"); await page.click("#reset")
        await page.check("#secs input[data-sec=levels]"); await page.click("#done")  # the reset hides 价格阶梯 again (its default)
        assert (await page.inner_text("#opps")).startswith("🔥 机会 2")
        assert await page.evaluate("['oppOff', 'oppMakers', 'oppTakers', 'oppPoints'].map(k => localStorage.getItem(k))") == [None] * 4
        # a maker whose market does not earn points now stays out of the strip (the card keeps its red frame); 自定义 can list it anyway
        jia["predict"].update(points_ok=False, points_why="价差 34.0¢ 超过积分上限 6.0¢")
        await page.reload(); await page.wait_for_selector("#g-levels .pg")
        strip = await page.inner_text("#opps")
        assert strip.startswith("🔥 机会 1") and "挂单" not in strip and "甲" not in strip and await page.locator(".card.hot:has-text('甲')").count() == 1, strip
        await page.click("#edit"); await page.uncheck("#opp-points"); await page.click("#done")
        strip = await page.inner_text("#opps")
        assert strip.startswith("🔥 机会 2") and "挂单 1" in strip and "甲" in strip and await page.evaluate("localStorage.getItem('oppPoints')") == "false", strip
        await page.click("#edit"); await page.check("#opp-points"); await page.click("#done")
        jia["predict"].update(points_ok=True, points_why="")
        # the "只列吃单" switch of earlier builds carries over as "no makers", once
        await page.evaluate("localStorage.setItem('oppTaker', 'true'); localStorage.removeItem('oppMakers')")
        await page.reload(); await page.wait_for_selector("#g-levels .pg")
        assert "甲" not in await page.inner_text("#opps") and await page.evaluate("[localStorage.getItem('oppMakers'), localStorage.getItem('oppTaker')]") == ["false", None]
        await page.click("#edit"); await page.check("#opp-maker"); await page.click("#done")
        assert (await page.inner_text("#opps")).startswith("🔥 机会 2")
        # a folded section still counts its red-framed cards in its title; the strip unfolds it before jumping in
        await page.click("#h-levels .fold")
        assert not await page.is_visible("#g-levels") and await page.inner_text("#h-levels .fs") == "5 张 · 🔥 1"
        await page.locator("#opps .opp").nth(1).click(); await page.wait_for_timeout(300)
        assert await page.is_visible("#g-levels .card") and await card.evaluate("c => c.classList.contains('flash')")
        assert await page.evaluate("localStorage.getItem('folded')") == "[]"
        await card.locator(".price-model summary").click()
        text = await card.locator(".price-model dl").inner_text()
        assert "币安现货 BTCUSDT" in text and "最低价 ≤ 档位即 Yes" in text and "已核至" in text, text
        over = """[...document.querySelectorAll('#g-levels .card *')].filter(e => { const c = e.closest('.card').getBoundingClientRect(),
                  r = e.getBoundingClientRect(); return r.width && (r.right > c.right + 0.5 || r.left < c.left - 0.5) }).length"""
        for width in (390, 1300):
            await page.set_viewport_size({"width": width, "height": 900})
            assert await page.evaluate(over) == 0, width
            assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), width
        if os.environ.get("WEB_SCREENSHOT"):
            path = __import__("pathlib").Path(os.environ["WEB_SCREENSHOT"])
            for width, suffix in ((390, "markets-mobile"), (1300, "markets-desktop")):
                await page.set_viewport_size({"width": width, "height": 900})
                await page.locator("#g-levels").screenshot(path=str(path.with_name(path.stem + "-" + suffix + path.suffix)))
        # Active, inactive and unknown points states: only the eligible maker enters filters, highlights and the strip.
        # Many ordinary levels fold out, but every suggestion stays in the compact view.
        copy = __import__("copy").deepcopy
        stress = copy(btc)
        template = next(r for r in btc["ladder"]["rows"] if r["label"] == "↑ $120k")
        def uirow(label, level, **extra):
            r = copy(template)
            r.update(label=label, level=level, dir="up", dist=level / 112050 - 1, fair=.8, bid=.5, ask=.52,
                     bids=[[.5, 5000]], asks=[[.52, 5000]], need=.285, swing=.285, error="", hold="", dir_note="",
                     stale=False, touched=False, points_active=None, points_ok=None, points_note="积分状态暂缺", points_why="积分状态暂缺",
                     makers=False, maker_note="积分状态暂缺：不给挂单建议")
            r.update(extra)
            return r
        stress["ladder"]["rows"] = [uirow("↑ $200k", 200000, points_active=True, points_ok=True, points_note="积分已激活", points_rate=200, points_why="",
                                         makers=True, maker_note=""),
                                   uirow("↑ $190k", 190000, points_active=False, points_ok=False, points_note="积分未激活", points_why="积分未激活",
                                         maker_note="积分未激活：不给挂单建议"),
                                   uirow("↑ $185k", 185000, points_active=True, points_ok=False, points_note="积分已激活", points_rate=150,
                                         points_why="价差 34.0¢ 超过积分上限 6.0¢", maker_note="价差 34.0¢ 超过积分上限 6.0¢"),
                                   uirow("↑ $180k", 180000, fair=None),  # unknown points, a book without a model price
                                   uirow("↑ $175k", 175000, points_active=True, points_ok=True, points_note="积分已激活", points_rate=60, points_why="",
                                         makers=True, maker_note="", fair=.5, bid=.5, ask=.52, bids=[[.5, 5000]], asks=[[.52, 5000]]),  # earns, no edge
                                   *[uirow("↑ $" + str(n) + "k", n * 1000, points_active=True, points_ok=True, points_note="积分已激活", points_rate=40,
                                           points_why="", makers=True, maker_note="") for n in (170, 165, 160)],  # five earning levels: past the pad
                                   uirow("↑ $158k", 158000, stale=True, fair=None),  # a stale book and no model price
                                   *[uirow("↑ $" + str(n) + "k", n * 1000) for n in range(155, 115, -5)],
                                   uirow("↑ $114k", 114000, fair=.74, bid=.49, ask=.50, bids=[[.49, 5000]], asks=[[.50, 5000]], need=.02, swing=.02),
                                   uirow("↓ $100k", 100000, dir="down")]
        payload["items"] = [stress]
        await page.reload()
        await page.wait_for_selector("#g-levels .pg")
        card = page.locator("#g-levels .card").first
        assert await card.locator(".pg .ptarget").count() == 7
        visible = await card.locator(".pg .lt").all_inner_texts()
        # the compact view: the five levels that earn points now, ↑ $114k's taker suggestion, and ↓ $100k as the price's lower neighbour
        # (the earning levels alone fill the five, so nothing else is padded in); ↑ $185k pays but cannot earn now, so it stays out
        assert visible == ["↑ $200k", "↑ $175k", "↑ $170k", "↑ $165k", "↑ $160k", "↑ $114k", "↓ $100k"], visible
        assert (await card.locator(".pg .lspot").inner_text()).startswith("现价") and await card.locator(".pg .lspot").count() == 1
        # level / distance / points line up as three columns on a wide card; on a phone the pill sits under the level
        await page.set_viewport_size({"width": 900, "height": 900})
        for sel in (".pg .ptarget .ppoints", ".pg .ptarget .pdist"):
            lefts = await card.locator(sel).evaluate_all("els => els.map(e => Math.round(e.getBoundingClientRect().left))")
            assert len(lefts) >= 5 and len(set(lefts)) == 1, (sel, lefts)
        await page.set_viewport_size({"width": 390, "height": 900})
        rect = "e => { const r = e.getBoundingClientRect(); return [r.left, r.top, r.bottom] }"
        lt = await card.locator(".ptarget:has(.lt:text-is('↑ $200k')) .lt").evaluate(rect)
        pp = await card.locator(".ptarget:has(.lt:text-is('↑ $200k')) .ppoints").evaluate(rect)
        assert abs(lt[0] - pp[0]) < 1 and pp[1] >= lt[2] - 1, (lt, pp)
        await page.set_viewport_size({"width": 1300, "height": 900})
        assert "挂Yes 50.0" in await page.inner_text("#opps")
        active = card.locator(".ptarget:has(.lt:text-is('↑ $200k'))")
        assert await active.locator(".ppoints.active").inner_text() == "● 200 PP/h"  # colour says active, the number the rate
        await active.click()
        await card.locator(".lrow .edge.best").click()
        assert "→ 满足" in await card.locator(".lrow .edet").inner_text()
        assert "200 PP/小时" in await card.locator(".lrow .lnote").inner_text()
        await active.click()
        await card.locator(".price-tools button").click()
        assert await card.locator(".pg .ptarget").count() == len(stress["ladder"]["rows"])
        await card.locator(".ptarget:has(.lt:text-is('↑ $190k'))").click()
        assert "积分未激活" in await card.locator(".lrow .lnote").inner_text()
        assert await card.locator(".lrow .edge.best").count() == 0
        await card.locator(".lrow .edge").first.click()
        assert "积分未激活：不给挂单建议" in await card.locator(".lrow .edet").inner_text()
        await card.locator(".ptarget:has(.lt:text-is('↑ $190k'))").click()
        assert await card.locator(".ptarget:has(.lt:text-is('↑ $190k')) .ppoints.off").get_attribute("title") == "积分未激活"  # a faint ○, no text
        # a programme that pays, on a book too wide to earn: ○ keeps the rate, the tooltip says why (your BTC 70k/90k case)
        paid = card.locator(".ptarget:has(.lt:text-is('↑ $185k')) .ppoints.off")
        assert await paid.inner_text() == "○ 150 PP/h" and await paid.get_attribute("title") == "有积分（每小时 150 PP），但现在拿不到：价差 34.0¢ 超过积分上限 6.0¢"
        assert await card.locator(".ptarget:has(.lt:text-is('↑ $180k')) .ppoints").count() == 0  # unknown: nothing
        unknown = card.locator(".ptarget:has(.lt:text-is('↑ $180k')) + .pmodel + .paction")
        assert await unknown.inner_text() == "—" and "积分状态暂缺" in await unknown.get_attribute("title")
        nomodel = card.locator(".ptarget:has(.lt:text-is('↑ $180k')) + .pmodel + .paction + .paction")
        assert await nomodel.inner_text() == "模型暂缺" and await nomodel.get_attribute("title") == "模型价暂缺，等待行情或 σ"  # a book, no fair price
        assert await card.locator(".ptarget:has(.lt:text-is('↑ $180k')) + .pmodel").inner_text() == "—"
        stale = card.locator(".ptarget:has(.lt:text-is('↑ $158k')) + .pmodel + .paction + .paction")
        assert await stale.inner_text() == "盘口过期" and await stale.get_attribute("title") == "盘口过期，等待新盘口"  # the tooltip follows the text
        await page.click("#fchips button:text-is('仅挂单')")
        assert await page.locator("#g-flat .card.price-lad").count() == 1
        assert "↑ $200k" in await page.locator("#g-flat .pg").inner_text()
        await page.click("#fchips button:text-is('仅挂单')")
        await card.locator(".price-tools button").click()
        assert await card.locator(".pg .ptarget").count() == 7
        for width in (320, 390, 900, 1300):
            await page.set_viewport_size({"width": width, "height": 900})
            assert await page.evaluate(over) == 0, width
            assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), width
        if os.environ.get("WEB_SCREENSHOT"):
            await page.set_viewport_size({"width": 390, "height": 900})
            await page.screenshot(path=os.environ["WEB_SCREENSHOT"], full_page=True)
            await page.set_viewport_size({"width": 1300, "height": 900})
            path = __import__("pathlib").Path(os.environ["WEB_SCREENSHOT"])
            await page.screenshot(path=str(path.with_name(path.stem + "-desktop" + path.suffix)), full_page=True)
        await browser.close()
    await web.stop()


def rm_sigma(bot):
    return bot.ranges["BTC-HIT-10"].sigma


bot = asyncio.run(run())
asyncio.run(browser_check(bot))
print("RANGE_OK")
