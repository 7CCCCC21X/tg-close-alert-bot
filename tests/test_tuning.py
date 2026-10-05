"""1.29.0: the model and interface changes from the formula / interface review.

Formula: a running closing auction keeps a fixed share of variance; the touch models' error allows for the drift
convention; GeckoTerminal hourly bars are weighted by the time they span; a US stock's running session counts only the
in-session share of the daily variance; /calib reports standardised residuals by remaining share; a crossed snapshot fills
no resting paper order; the book keeps 20 levels so "too thin" is not "cut off at 5".
Interface: a ladder level whose book failed keeps its last one; Binance spot backs off after a rate limit; Telegram sends
run in parallel with the global lock only reserving slots; command-poll sends are queued; long messages are measured as
Telegram counts them and cut between blocks; a stuck sampling loop ends the process; a closed US market is polled
slowly; data.json carries no server-side edge chips."""
import asyncio, math, random, sys, time, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
m.CapMarket.GECKO_GAP = 0
D = m.D
UTC = dt.timezone.utc
bj = lambda *a: int(dt.datetime(*a, tzinfo=m.BEIJING).timestamp() * 1000)
kst = dt.timezone(dt.timedelta(hours=9))
kr = lambda *a: int(dt.datetime(*a, tzinfo=kst).timestamp() * 1000)
et = lambda *a: m.et_ms(*a, -4)  # EDT
levels = lambda rows: tuple((D(str(p)), D(str(q))) for p, q in rows)
NOW = int(dt.datetime(2026, 10, 6, 18, 0, tzinfo=UTC).timestamp() * 1000)  # Tuesday 14:00 EDT

# --- a running closing auction is worth a fixed share of the session's variance, not one minute -------------------------
assert abs(m.session_remaining("hk", bj(2026, 9, 25, 16, 5), dt.date(2026, 9, 24))[0] - 5 / 330) < 1e-9
assert abs(m.session_remaining("hk", bj(2026, 9, 25, 15, 59), dt.date(2026, 9, 24))[0] - 1 / 330) < 1e-9   # continuous trading: as before
assert abs(m.session_remaining("hk", bj(2026, 9, 25, 16, 12), dt.date(2026, 9, 24))[0] - 1 / 330) < 1e-9   # matched: the close is known
assert abs(m.session_remaining("sh", bj(2026, 9, 25, 14, 58), dt.date(2026, 9, 24))[0] - 3 / 240) < 1e-9
assert abs(m.session_remaining("sh", bj(2026, 9, 25, 15, 5), dt.date(2026, 9, 24))[0] - 1 / 240) < 1e-9
assert abs(m.session_remaining("kr", kr(2026, 9, 25, 15, 29), dt.date(2026, 9, 24))[0] - 5 / 390) < 1e-9
hsi = m.close_odds("恒生指数", D("26000"), D("26026"), 0.013, m.session_remaining("hk", bj(2026, 9, 25, 16, 5), dt.date(2026, 9, 24))[0],
                   dt.date(2026, 9, 25), D("0.01"), "", "", "")
assert 0.65 < hsi.fair_up < 0.8, hsi.fair_up  # a 0.1% lead at 16:05 is no longer a 98.8% 涨

# --- the touch formulas generalise over the log drift; the default reproduces the old closed forms -----------------------
for spot, level, sigma, years in ((80.0, 200.0, 2.5, 0.09), (100.0, 130.0, 0.6, 0.25)):
    h, s = math.log(level / spot), sigma * math.sqrt(years)
    assert abs(m.hit_probability(spot, level, sigma, years) - (m.norm_cdf((-h - s * s / 2) / s) + spot / level * m.norm_cdf((-h + s * s / 2) / s))) < 1e-12
    assert abs(m.hit_probability(spot, level, sigma, years, 0.0) - 2 * m.norm_cdf(-h / s)) < 1e-12          # reflection principle
    assert abs(m.low_probability(level, spot, sigma, years, 0.0) - 2 * m.norm_cdf(-h / s)) < 1e-12
    assert m.hit_probability(spot, level, sigma, years, 0.0) > m.hit_probability(spot, level, sigma, years)  # no −σ²/2 pull


def sigma_only(fn, *args):
    base = fn(*args)
    return max(abs(fn(*args[:2], args[2] * k, *args[3:]) - base) for k in (m.MODEL_SIGMA_ERROR, 1 / m.MODEL_SIGMA_ERROR))


# market cap: σ 300% a year, the drift convention is worth more than σ ×/÷ 1.25
NIU = m.CAP_MARKETS[0]
cap = m.CapMarket(m.Store(":memory:"), NIU)
cap.price, cap.supply, cap.sigma, cap.priced_ms = D("0.30"), D("985000000"), 3.0, NOW
years = (NIU.end_ms - NOW) / m.YEAR_MS
fair = cap.probability(D("5e8"), NOW)
assert fair == m.hit_probability(float(cap.cap), 5e8, 3.0, years) and 0 < fair < 1
swing = cap.model_swing(D("5e8"), NOW, fair)
drift_only = abs(m.hit_probability(float(cap.cap), 5e8, 3.0, years, 0.0) - fair)
assert swing == max(sigma_only(m.hit_probability, float(cap.cap), 5e8, 3.0, years), drift_only) and drift_only > sigma_only(m.hit_probability, float(cap.cap), 5e8, 3.0, years), (swing, drift_only)
# price ladder (both directions) and the flip market: the drift variant is part of the error
BTC = m.RANGE_MARKETS[0]
rm = m.RangeMarket(m.Store(":memory:"), BTC)
rm.price, rm.sigma, rm.priced_ms = D("112050"), 0.5, NOW
ry = rm.remaining_years(NOW)
assert ry > 0, ry
for level, direction, fn in ((D("125000"), "up", m.hit_probability), (D("100000"), "down", m.low_probability)):
    f = rm.probability(level, direction, NOW)
    want = max(sigma_only(fn, 112050.0, float(level), 0.5, ry), abs(fn(112050.0, float(level), 0.5, ry, 0.0) - f))
    assert abs(rm.model_swing(level, direction, NOW, f) - want) < 1e-12 and want > 0
HYPE = m.FLIP_MARKETS[0]
fm = m.FlipMarket(m.Store(":memory:"), HYPE)
fm.prices, fm.sigma, fm.priced_ms = {"HYPE": D("30"), "SOL": D("120")}, 0.9, NOW
p = fm.odds(NOW)
fy = (HYPE.end_ms + 60_000 - NOW) / m.YEAR_MS
assert isinstance(p, float) and abs(fm.model_swing(NOW) - max(sigma_only(m.hit_probability, 0.25, 1.0, 0.9, fy), abs(m.hit_probability(0.25, 1.0, 0.9, fy, 0.0) - p))) < 1e-12
# first-touch: a driftless log price (μ = σ²/2) is the third variant
BNB = m.TOUCH_MARKETS[0]
touch = m.TouchMarket(m.Store(":memory:"), BNB)
touch.price, touch.sigma, touch.priced_ms = D("800"), 0.6, NOW
odds = touch.odds(NOW)
ty = (BNB.deadline_ms - NOW) / m.YEAR_MS
variants = [m.first_touch(800.0, 700.0, 900.0, 0.6 * k, 0.0, ty).fair_upper for k in (m.MODEL_SIGMA_ERROR, 1 / m.MODEL_SIGMA_ERROR)]
variants.append(m.first_touch(800.0, 700.0, 900.0, 0.6, 0.18, ty).fair_upper)
assert abs(touch.model_swing(NOW) - max(abs(v - odds.fair_upper) for v in variants)) < 1e-12 and abs(variants[2] - odds.fair_upper) > 0.005

# --- hourly bars weighted by the time they span: a missing hour no longer inflates σ ------------------------------------
rng = random.Random(3)
true_sigma, step = 2.0, 2.0 / math.sqrt(365 * 24)
price, t0, bars = 1.0, 1_700_000_000, []
for i in range(721):
    price *= math.exp(rng.gauss(0, step))
    bars.append((t0 + i * 3600, price, price, price, price))
full, hours, missing = m.bars_sigma(bars)
closes = [b[4] for b in bars]
rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
mean = sum(rets) / len(rets)
assert abs(full - math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1) * 24 * 365)) < 1e-9  # the old formula when nothing is missing
assert hours == 720 and missing == 0 and abs(full / true_sigma - 1) < 0.15, (full, hours, missing)
thin = [b for i, b in enumerate(bars) if i % 3]  # every third hour had no trade (bars 1 … 719 remain: 718 hours, 479 steps)
thinned, hours, missing = m.bars_sigma(thin)
naive = [math.log(b[4] / a[4]) for a, b in zip(thin, thin[1:])]
naive_sigma = math.sqrt(sum((r - sum(naive) / len(naive)) ** 2 for r in naive) / (len(naive) - 1) * 24 * 365)
assert abs(thinned / full - 1) < 0.1 and naive_sigma / full > 1.1 and missing == 239 and hours == 718, (thinned, naive_sigma, missing, hours)

# --- a US stock's running session counts only the in-session share of the daily variance -------------------------------
assert m.us_trading_years(et(2026, 10, 6, 14, 0), et(2026, 10, 6, 16, 0), 0.5) == m.us_trading_years(et(2026, 10, 6, 14, 0), et(2026, 10, 6, 16, 0)) / 2
later = m.us_trading_years(et(2026, 10, 6, 14, 0), et(2026, 10, 7, 16, 0), 0.5)
assert abs(later - (0.5 * 2 / 6.5 + 1) / 252) < 1e-12, later  # tomorrow still has its gap ahead: a whole day
assert m.us_trading_years(et(2026, 10, 6, 17, 0), et(2026, 10, 7, 16, 0), 0.5) == m.us_trading_years(et(2026, 10, 6, 17, 0), et(2026, 10, 7, 16, 0))  # closed: nothing to scale
STRC = m.STOCK_HIT_MARKETS[0]
srm = m.StockRangeMarket(m.Store(":memory:"), STRC)
assert srm.price_seconds(et(2026, 10, 6, 14, 0)) == 30 and srm.price_seconds(et(2026, 10, 6, 16, 5)) == 30  # trading, then the closing print
assert srm.price_seconds(et(2026, 10, 6, 16, 20)) == 120 and srm.price_seconds(et(2026, 10, 10, 12, 0)) == 120  # evening, Saturday

# --- /calib: standardised residuals, by bucket of the remaining share -----------------------------------------------------
rng = random.Random(11)
preds, outcomes = [], {}
for i in range(60):
    day, R = (dt.date(2026, 3, 1) + dt.timedelta(days=i)).isoformat(), (0.2, 0.7, 1.5)[i % 3]
    close = 7000 * math.exp(rng.gauss(0, 0.01 * math.sqrt(R)))
    outcomes[f"KOSPI:{day}"] = close
    preds.append({"key": "KOSPI", "t": i, "target": day, "mode": "盘中", "ref": 7000.0, "eff": 7000.0, "move": 0.0, "beta": 1.0,
                  "sigma": 0.01, "R": R, "up": 0.5})
text = "\n".join(m.calibration_report(preds, outcomes))
line = next(l for l in text.splitlines() if "标准化残差" in l)
rms = float(line.split("均方根 ")[1].split("（")[0])
assert 0.75 < rms < 1.25 and "60 条/60 日" in line, line
buckets = next(l for l in text.splitlines() if "按剩余方差份额 R 分桶" in l)
assert "R<0.25：20 条/20 日" in buckets and "0.5–1：20 条/20 日" in buckets and "R≥1：20 条/20 日" in buckets and "0.25–0.5" not in buckets, buckets
assert "残差 σ " not in line and "残差 σ " not in buckets  # the proxy fit's own figure keeps its name

# --- the book keeps 20 levels; the page gets 10 ---------------------------------------------------------------------------
deep = m.predict_levels([[f"0.{50 + i:02d}", "10"] for i in range(25)], True)
assert len(deep) == m.PREDICT_DEPTH == 20 and deep[0][0] == D("0.74")
book = m.PredictBook("SSE", "s", "901", "SSE?", levels([]), levels([(0.50 + i / 100, 10) for i in range(15)]), NOW, 200)
avg, shares, short = m.taker_fill(tuple((float(p), float(q)) for p, q in book.asks), 60.0)  # $60 ≈ 110 shares: beyond 5 levels, inside 15
assert not short and shares > 50, (avg, shares, short)

# --- long messages: measured as Telegram counts them, cut between blocks, never through a tag or an entity -------------
block = "<b>" + "甲" * 30 + "</b> " + "乙" * 20
text = "\n\n".join([block] * 40)                      # raw 2398 characters, 2118 as rendered
assert m.rendered_len(text, True) == 2118 and m.rendered_len(text) == 2398 and m.rendered_len("a &amp; b", True) == 5
assert len(m.split_text(text, 2200, html_mode=True)) == 1 and len(m.split_text(text, 2200)) == 2
chunks = m.split_text(text, 1000, html_mode=True)
assert len(chunks) == 3 and all(m.rendered_len(c, True) <= 1000 for c in chunks) and "\n\n".join(chunks) == text, [len(c) for c in chunks]
assert all(c.count("<b>") == c.count("</b>") and not c.startswith("\n") for c in chunks)  # whole blocks
tagged = ("<b>" + "x" * 3 + "</b>") * 100
pieces = m.split_text(tagged, 250, html_mode=True)
assert len(pieces) >= 2 and all(c.count("<") == c.count(">") for c in pieces) and "".join(pieces) == tagged
assert all(m.rendered_len(c, True) <= 250 for c in pieces)
plain = "x" * 50 + "\n" + "y" * 50 + "\n\n" + "z" * 30
assert m.split_text(plain, 120) == ["x" * 50 + "\n" + "y" * 50, "z" * 30]  # the blank line in the last 40%: the block boundary wins

# --- data.json leaves the server's edge chips out --------------------------------------------------------------------------
payload = {"items": [{"predict": {"edges": [{"x": 1}], "fair": 0.7, "bids": [[0.5, 1]]},
                      "ladder": {"rows": [{"edges": [{"x": 1}], "fair": 0.5}, "odd"]}}, {"missing": "x"}]}
trimmed = m.page_payload(payload)
assert trimmed["items"][0]["predict"] == {"fair": 0.7, "bids": [[0.5, 1]]} and trimmed["items"][0]["ladder"]["rows"][0] == {"fair": 0.5}


class FM:
    def __init__(self, now): self.now, self.config = now, None
    def now_ms(self): return self.now


class FakeTelegram:
    def __init__(self): self.sent = []
    async def call(self, *a, **k): return True
    async def send(self, chat, thread, text, reply_markup=None, parse_mode=None): self.sent.append(text)


async def run():
    # --- a ladder level whose book failed keeps the one it had, and the card / status learn of the failure ----------------
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off"})
    feed = m.PredictFeed(cfg)
    feed.ladder_keys.add("NIULAI")
    markets = [("11", "$200M"), ("12", "$300M")]
    broken: dict[str, Exception] = {}
    statuses: dict[str, str] = {}  # market id -> status its details report (OPEN unless set)
    asked: list[str] = []          # orderbook requests made

    async def fetch(url, payload=None):
        if url == m.PREDICT_GRAPHQL:
            v = payload["variables"]
            if "id" in v:
                return {"data": {"category": {"id": "90", "__typename": "MultiCategory"}}}
            return {"data": {"markets": {"edges": [{"node": {"id": i, "conditionId": "0x" + i, "title": t, "question": t}} for i, t in markets]}}}
        for i, _ in markets:
            if url.endswith(f"/markets/{i}"):
                return {"data": {"id": i, "status": statuses.get(i, "OPEN"), "outcomes": [{"name": "Yes", "indexSet": 1}, {"name": "No", "indexSet": 2}]}}
            if url.endswith(f"/markets/{i}/orderbook") or url.endswith(f"/markets/0x{i}/orderbook"):
                asked.append(url)
                if i in broken:
                    raise broken[i]
                return {"data": {"bids": [["0.10", "100"]], "asks": [["0.14", "50"]]}}
        raise AssertionError(url)
    feed.fetch = fetch
    await feed.refresh({"NIULAI": NIU.slug}, force=True)
    first = feed.ladders["NIULAI"]
    assert [r.target for r in first] == [D("2e8"), D("3e8")] and all(r.book and not r.error for r in first) and "NIULAI" not in feed.errors
    broken["12"] = m.RemoteError("网络错误 (TimeoutError)")
    await feed.refresh({"NIULAI": NIU.slug}, force=True)
    rows = feed.ladders["NIULAI"]
    assert rows[1].book is first[1].book and rows[1].error == "网络错误 (TimeoutError)" and rows[0].book is not first[0].book, rows
    assert feed.errors["NIULAI"] == "1/2 档盘口刷新失败（$300M：网络错误 (TimeoutError)）；显示上次盘口", feed.errors
    assert feed.yes_book(rows[1]) == (first[1].book, "")  # priced from the kept book (stale once PREDICT_STALE_MS passes)
    broken["12"] = m.RemoteError("HTTP 404: 接口请求失败")  # no book for this market: an answer, not a failure
    await feed.refresh({"NIULAI": NIU.slug}, force=True)
    assert "NIULAI" not in feed.errors and feed.ladders["NIULAI"][1].error.startswith("HTTP 404")
    broken.clear()
    await feed.refresh({"NIULAI": NIU.slug}, force=True)
    assert "NIULAI" not in feed.errors and not feed.ladders["NIULAI"][1].error
    # a level Predict has settled has no live book any more: its details say so, its book is not asked for, nothing is
    # reported; a closed market's HTTP 400 is likewise an answer; an open market's HTTP 400 is a failure, and named
    statuses["12"], broken["12"] = "RESOLVED", m.RemoteError("HTTP 400: Market is closed")
    feed.market_meta.clear(); asked.clear()
    await feed.refresh({"NIULAI": NIU.slug}, force=True)
    rows = feed.ladders["NIULAI"]
    assert rows[1].book is None and not rows[1].error and "NIULAI" not in feed.errors, (rows, feed.errors)
    assert not [u for u in asked if "/12/" in u or "0x12" in u] and [u for u in asked if "/11/" in u], asked
    statuses["12"] = "CLOSED"; feed.market_meta.clear()
    await feed.refresh({"NIULAI": NIU.slug}, force=True)
    assert "NIULAI" not in feed.errors and feed.ladders["NIULAI"][1].error == "HTTP 400: Market is closed" and feed.book_absent(feed.ladders["NIULAI"][1])
    statuses.pop("12"); feed.market_meta.clear()
    await feed.refresh({"NIULAI": NIU.slug}, force=True)
    assert feed.errors["NIULAI"] == "1/2 档盘口刷新失败（$300M：HTTP 400: Market is closed）；显示上次盘口", feed.errors
    assert not feed.book_absent(feed.ladders["NIULAI"][1]) and m.PredictFeed.market_closed({"status": "PENDING"}) is False
    assert m.PredictFeed.market_closed({"status": "OPEN", "trading_status": "CLOSED"}) and m.PredictFeed.market_settled({"status": "RESOLVED"})
    assert not m.PredictFeed.market_settled({"status": "CLOSED"}) and m.PredictFeed.market_closed({"status": "Cancelled"})
    broken.clear(); statuses.clear()
    # what an API says in an error body reaches the message (Predict's REST is NestJS-style: message / error)
    assert m.http_error_text({"statusCode": 400, "message": "Market is closed", "error": "Bad Request"}) == "Market is closed"
    assert m.http_error_text({"message": ["a must be x", "b too"]}) == "a must be x；b too" and m.http_error_text({"error": {"message": "nope"}}) == "nope"
    assert m.http_error_text([1]) == "" and m.http_error_text({"description": "Bad Request: chat not found"}) == "Bad Request: chat not found"

    # --- Binance spot: a rate limit pauses every spot request; a plain failure still moves to the next host ---------------
    calls = []
    real_fetch = m.fetch_source

    async def limited(url, extra=None, record=True):
        calls.append(url)
        if "data-api.binance.vision" in url:
            raise m.RemoteError("HTTP 429: 接口限流，等待后重试", 30)
        return b'{"price": "700.5"}'
    m.fetch_source = limited
    try:
        try:
            await m.binance_spot("ticker/price", symbol="BNBUSDT"); assert False
        except m.RemoteError as error:
            assert error.retry_after == 30 and "429" in str(error) and len(calls) == 1, (error, calls)  # no rotation on a rate limit
        try:
            await m.binance_spot("ticker/price", symbol="BNBUSDT"); assert False
        except m.RemoteError as error:
            assert "币安现货接口限流冷却中" in str(error) and error.retry_after >= 29 and len(calls) == 1, error
        m.SPOT_BACKOFF["until"] = 0.0

        async def flaky(url, extra=None, record=True):
            calls.append(url)
            if "data-api.binance.vision" in url:
                raise m.RemoteError("网络错误 (TimeoutError)")
            return b'{"price": "700.5"}'
        m.fetch_source = flaky
        calls.clear()
        assert (await m.binance_spot("ticker/price", symbol="BNBUSDT"))["price"] == "700.5" and len(calls) == 2 and "api.binance.com" in calls[1]
        for _ in range(2):  # a host that keeps failing goes to the back of the line
            m.SOURCE_HEALTH.record("https://data-api.binance.vision/api/v3/x", "timeout")
        calls.clear()
        assert (await m.binance_spot("ticker/price", symbol="BNBUSDT"))["price"] == "700.5" and len(calls) == 1 and "api.binance.com" in calls[0]
    finally:
        m.fetch_source = real_fetch
        m.SOURCE_HEALTH.hosts.clear()
        m.SPOT_BACKOFF["until"] = 0.0

    # --- Telegram: one chat's slow send does not hold the others; at most PARALLEL requests in flight ----------------------
    class SlowTelegram(m.Telegram):
        def __init__(self):
            super().__init__("1:x"); self.done, self.flying, self.peak = [], 0, 0
        async def call(self, method, payload=None, timeout=15):
            self.flying += 1; self.peak = max(self.peak, self.flying)
            try:
                if payload["chat_id"] == 1:
                    await asyncio.sleep(0.6)
                self.done.append((payload["chat_id"], time.monotonic()))
                return True
            finally:
                self.flying -= 1
    tg = SlowTelegram()
    t0 = time.monotonic()
    await asyncio.gather(tg.paced("sendMessage", {"chat_id": 1, "text": "slow"}), tg.paced("sendMessage", {"chat_id": 2, "text": "quick"}))
    when = dict(tg.done)
    assert when[2] - t0 < 0.35 < when[1] - t0, {c: round(t - t0, 3) for c, t in when.items()}
    assert tg.chat_next[2] > t0 and tg.next_send >= t0 + 2 * tg.GLOBAL_GAP - 1e-6  # the per-chat gap and the global slots still count
    tg = SlowTelegram()
    await asyncio.gather(*(tg.paced("sendMessage", {"chat_id": 1, "text": "slow"}) for _ in range(6)))  # one chat: in order, one at a time
    assert tg.peak == 1

    class ManySlow(SlowTelegram):
        async def call(self, method, payload=None, timeout=15):
            return await super().call(method, {**payload, "chat_id": 1} if payload["chat_id"] < 100 else payload)
    tg = ManySlow()
    await asyncio.gather(*(tg.paced("sendMessage", {"chat_id": c, "text": "x"}) for c in range(1, 8)))
    assert tg.peak == tg.PARALLEL == 4, tg.peak

    # --- command poll: a card edit is queued behind the running loops, inline without them ------------------------------------
    class RecTelegram(m.Telegram):
        def __init__(self): super().__init__("1:x"); self.calls = []
        async def call(self, method, payload=None, timeout=15): self.calls.append((method, payload)); return True
    ccfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ADMIN_USER_ID": "42"})
    rtg = RecTelegram(); cbot = m.Bot(ccfg, m.Store(":memory:"), m.Binance(ccfg), rtg)
    tap = {"id": "q1", "data": "threshold:2", "from": {"id": 42}, "message": {"message_id": 5, "chat": {"id": -100}, "date": time.time()}}
    cbot.reference_tasks = [object()]  # production: the background loops run
    await cbot.process_update({"update_id": 1, "callback_query": tap})
    assert [mth for mth, _ in rtg.calls] == ["answerCallbackQuery"] and cbot.delivering("edit:1"), rtg.calls
    await cbot.drain_deliveries()
    assert [mth for mth, _ in rtg.calls] == ["answerCallbackQuery", "editMessageText"]
    n = len(rtg.calls)
    await cbot.process_message({"text": "/id", "chat": {"id": 7}, "from": {"id": 9}, "date": time.time()})  # a stranger's /id
    assert len(rtg.calls) == n and cbot.delivering("reply:2")
    await cbot.drain_deliveries()
    assert rtg.calls[-1][0] == "sendMessage" and "你的用户 ID" in rtg.calls[-1][1]["text"] or "ID" in rtg.calls[-1][1]["text"]

    # --- a crossed snapshot fills no resting paper order ----------------------------------------------------------------------
    scfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off",
                              "SIM_WAYS": "both", "SIM_MARKETS": "all"})
    sbot = m.Bot(scfg, m.Store(":memory:"), FM(NOW), None)
    world = {"markets": []}
    sbot.sim_markets = lambda now: world["markets"]

    async def no_details(mid):
        raise m.RemoteError("offline")
    sbot.predict.market_details = no_details

    def mk(bids, asks, at):
        return m.SimMarket("x", "X", "close", "X", 0.70, m.PredictBook("X", "x", "901", "X?", levels(bids), levels(asks), at, 200), 0.03, "", ("涨", "跌"), {})

    async def step(at, market):
        sbot.market.now, sbot.sim_ran, world["markets"] = at, -1e9, [market]
        await sbot.sim_step(at)
    await step(NOW, mk([(0.55, 300)], [(0.58, 400)], NOW))
    resting = sbot.sim_trades()["x|up|挂"]
    assert resting["status"] == "resting" and resting["shares"] == 0 and resting["price"] == 0.55
    await step(NOW + 10_000, mk([(0.60, 100)], [(0.50, 200)], NOW + 10_000))  # crossed: sellers "through" 55¢ are a mid-update snapshot
    assert sbot.sim_trades()["x|up|挂"]["shares"] == 0 and sbot.sim_trades()["x|up|挂"]["status"] == "resting"
    await step(NOW + 20_000, mk([(0.52, 100)], [(0.54, 60)], NOW + 20_000))  # a real seller through the price: the presumed fill as before
    assert sbot.sim_trades()["x|up|挂"]["shares"] == 60

    # --- the watchdog: a sampling loop that never finishes a cycle ends the process; a healthy one is left alone -----------
    class Market(m.Binance):
        def now_ms(self): return int(time.time() * 1000)
        async def sync_clock(self): pass
        async def prices(self): return {"UNITREEUSDT": {"symbol": "UNITREEUSDT", "price": "73.13", "time": self.now_ms()}}
        async def get(self, path, **p): return []

    async def tg_call(method, payload=None, timeout=15):
        if method == "getWebhookInfo": return {}
        if method == "getMe": return {"username": "test_bot"}
        if method == "getUpdates": await asyncio.sleep(0.2); return []
        return True
    wcfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ADMIN_USER_ID": "42", "SYMBOLS": "UNITREEUSDT", "BASELINE_MODE": "manual",
                              "HSI_FUTURES": "off", "KOSPI_INDEX": "off", "SSE_INDEX": "off", "PREDICT": "off", "BNB_TOUCH": "off",
                              "SIM": "off", "PROBABILITY": "off", "WEB": "off", "POLL_SECONDS": "3"})
    m.WATCHDOG_SECONDS, m.Bot.WATCHDOG_TICK = 0.3, 0.05
    wbot = m.Bot(wcfg, m.Store(":memory:"), Market(wcfg), FakeTelegram())
    wbot.telegram.call = tg_call
    async def stuck():
        await asyncio.sleep(3600)
    wbot.one_cycle = stuck
    started = time.monotonic()
    assert await asyncio.wait_for(wbot.run(), 5) == 1 and 0.3 <= time.monotonic() - started < 3  # exit 1: Railway restarts the bot
    m.WATCHDOG_SECONDS = 5  # longer than the 3-second poll: a loop that keeps cycling is left alone
    hbot = m.Bot(wcfg, m.Store(":memory:"), Market(wcfg), FakeTelegram())
    hbot.telegram.call = tg_call
    runner = asyncio.create_task(hbot.run())
    await asyncio.sleep(0.8)
    assert not runner.done() and hbot.last_cycle > hbot.started - 1, runner  # cycles keep coming: the watchdog stays quiet
    hbot.stopping.set()
    assert await asyncio.wait_for(runner, 3) == 0
    print("TUNING_OK")


asyncio.run(run())
