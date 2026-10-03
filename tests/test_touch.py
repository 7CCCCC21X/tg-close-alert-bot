import asyncio, sys, math, time, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
BNB, SOL, BTC, ETH = m.TOUCH_MARKETS

# --- model: symmetric-ish band, long horizon -> both sides ~50%, none ~0; short -> mostly none ------------------
o = m.first_touch(800, 700, 900, 0.6, 0, 1.0)
assert abs(o.lower + o.upper + o.none - 1) < 1e-9 and o.none < 1e-3 and abs(o.lower - 0.5) < 0.02, o
o = m.first_touch(800, 700, 900, 0.6, 0, 0.001)
assert o.none > 0.999 and abs(o.fair_lower - 0.5) < 1e-3 and abs(o.fair_upper - 0.5) < 1e-3, o
o = m.first_touch(880, 700, 900, 0.3, 0, 0.1)  # close to $900: that side dominates
assert o.upper > 0.75 and o.lower < 0.03 and abs(o.fair_upper - (o.upper + o.none / 2)) < 1e-12, o
assert m.first_touch(690, 700, 900, 0.5, 0, 1) == m.TouchOdds(1.0, 0.0, 0.0)
assert m.first_touch(905, 700, 900, 0.5, 0, 1) == m.TouchOdds(0.0, 1.0, 0.0)
assert m.first_touch(800, 700, 900, 0.5, 0, 0) == m.TouchOdds(0.0, 0.0, 1.0)
# the two numerical branches (images for short horizons, eigenfunctions for long) agree where they meet
a, v = math.log(900 / 700), 0.6 ** 2
t_edge = 0.08 * a * a / v
lo_, hi_ = m.first_touch(800, 700, 900, 0.6, 0, t_edge * 0.999), m.first_touch(800, 700, 900, 0.6, 0, t_edge * 1.001)
assert abs(lo_.lower - hi_.lower) < 2e-3 and abs(lo_.upper - hi_.upper) < 2e-3, (lo_, hi_)

# --- outcome names -> which side the orderbook prices -------------------------------------------------------------
assert m.touch_outcome("$700", BNB) == "low" and m.touch_outcome("Yes, $900 first", BNB) == "high" and m.touch_outcome("Yes", BNB) == ""
assert m.touch_outcome("700 or 900", BNB) == ""
# whole numbers only: $60 is not found inside $160, nor $14 inside $140
assert m.touch_outcome("$60", SOL) == "low" and m.touch_outcome("$140", SOL) == "high" and m.touch_outcome("$160", SOL) == ""
# SOL's creation time comes from its rules when Predict gives none
assert SOL.symbol == "SOLUSDT" and SOL.created_ms == int(dt.datetime(2026, 3, 12, 13, 27, 8, 415000, tzinfo=dt.timezone.utc).timestamp() * 1000)
assert m.TouchMarket(m.Store(":memory:"), SOL).start_ms == SOL.created_ms
# BTC: its own window, both ends in EDT (UTC−4); outcome names with thousands separators or "k" are recognised
assert BTC.created_ms == int(dt.datetime(2026, 8, 25, 14, 0, tzinfo=dt.timezone.utc).timestamp() * 1000) and BTC.fixed_start
assert BTC.deadline_ms == int(dt.datetime(2026, 10, 26, 3, 59, tzinfo=dt.timezone.utc).timestamp() * 1000)
assert BTC.close_label() == "10-25 23:59 ET（北京 10-26 11:59）截止；都没碰到按 50/50 结算", BTC.close_label()
assert BNB.close_label() == "12-31 23:59 ET（北京 01-01 12:59）截止；都没碰到按 50/50 结算"
assert BTC.label(BTC.low) == "70k" and BNB.label(BNB.high) == "900" and SOL.label(SOL.low) == "60"
# ETH: 1k / 3k, deadline 12-31 ET, window from Predict's creation time (none in the rules)
assert ETH.symbol == "ETHUSDT" and ETH.label(ETH.low) == "1k" and ETH.label(ETH.high) == "3k" and ETH.created_ms == 0
assert ETH.deadline_ms == BNB.deadline_ms and not ETH.fixed_start
assert m.touch_outcome("$1,000", ETH) == "low" and m.touch_outcome("3k", ETH) == "high" and m.touch_outcome("$13,000", ETH) == ""
assert m.touch_outcome("$70,000", BTC) == "low" and m.touch_outcome("90k first", BTC) == "high" and m.touch_outcome("$7,000", BTC) == ""

# --- volatility: 721 hourly closes alternating ±1% -> σ ≈ 1% × √8760 -----------------------------------------------
NOW = int(dt.datetime(2026, 9, 29, 12, 0, tzinfo=m.BEIJING).timestamp() * 1000)
H = 3_600_000
rows = [[NOW - (722 - i) * H, "0", "0", "0", str(800 * (1.01 if i % 2 else 1)), "0", NOW - (721 - i) * H - 1] for i in range(722)]
sigma = m.realized_vol(rows, NOW)
assert abs(sigma - 0.00995 * 1.0003 * math.sqrt(8760)) / sigma < 0.02, sigma
# Binance's answer to limit=721 ends in the running hour: only 720 finished bars, not enough (the old bug)
try: m.realized_vol((rows + [[NOW, "0", "0", "0", "812", "0", NOW + H - 1]])[-721:], NOW); assert False
except ValueError: pass
try: m.realized_vol(rows[:100], NOW); assert False
except ValueError: pass


async def run():
    store = m.Store(":memory:")
    t = m.TouchMarket(store, BNB)
    start = NOW - 5 * H
    hits = {}  # hour open -> (hi, lo) in that hour's minute 17

    async def get(path, **p):
        if path == "ticker/price":
            return {"symbol": "BNBUSDT", "price": "812.50"}
        if p["interval"] == "1h" and p.get("limit") == 722:
            # like Binance: the newest `limit` bars, the last one still open (its close time is in the future)
            return (rows + [[NOW, "0", "0", "0", "812", "0", NOW + H - 1]])[-722:]
        if p["interval"] == "1h":
            first = p["startTime"]
            out = []
            for h in range(first, min(p["endTime"], NOW), H):
                hi, lo = hits.get(h, (820, 800))
                out.append([h, "0", str(hi), str(lo), "810", "0", h + H - 1])
            return out
        base = p["startTime"]
        hi, lo = hits.get(base, (820, 800))
        return [[base + i * 60_000, "0", str(hi if i == 17 else 820), str(lo if i == 17 else 800), "810", "0", base + (i + 1) * 60_000 - 1]
                for i in range(60)]
    t.get = get

    # unknown opening time: prices, σ, and says it assumed no earlier touch
    await t.refresh(NOW)
    assert t.price == D("812.50") and t.sigma and t.error == "" and "开盘时间未知" in t.status()
    odds = t.odds(NOW)
    assert isinstance(odds, m.TouchOdds) and 0 < odds.fair_upper < 1

    # known opening time, nothing touched: clear through the last finished hour, persisted
    t.start_ms = start; t.times["scan"] = -1e9
    await t.refresh(NOW)
    assert t.history["kind"] == "clear" and t.history["through"] >= NOW - H and "未触线" in t.status(), t.history

    # a later hour reaches $900 at minute 17: the result is fixed at 100/0 and never re-scanned
    hour = NOW - H
    hits[hour] = (905, 800)
    t.times["scan"] = -1e9; t.history  # noqa
    store.put(f"touch:{BNB.slug}", {"kind": "clear", "through": hour, "start": start})
    await t.scan(NOW + H)
    h = t.history
    assert h["kind"] == "high" and h["time"] == hour + 17 * 60_000 and t.odds(NOW) == m.TouchOdds(0.0, 1.0, 0.0), h
    assert "先触及 $900" in t.status()

    # a minute that spans both barriers cannot be ordered: flagged for a manual check
    store.put(f"touch:{BNB.slug}", {"kind": "clear", "through": hour, "start": start})
    hits[hour] = (905, 695)
    await t.scan(NOW + H)
    assert t.history["kind"] == "ambiguous" and t.status().startswith("需人工核对"), t.history
    # a new opening time discards the old answer
    t.start_ms = start - H
    assert t.history == {}

    # --- past the deadline: the hour it falls in is read minute by minute, through the deadline's own minute -----------
    tail_hour = NOW - 3 * H
    spec = m.dataclasses.replace(BNB, slug="tail-test", deadline_ms=tail_hour + 59 * 60_000)  # 23:59-style deadline
    minutes = {"hits": {}, "upto": 60, "asked": []}

    async def tail_get(path, **p):
        if p["interval"] == "1h":
            return [[h, "0", "820", "800", "810", "0", h + H - 1] for h in range(p["startTime"], min(p["endTime"], NOW), H)]
        minutes["asked"].append((p["startTime"], p["endTime"]))
        out = []
        for i in range(minutes["upto"]):
            opened = p["startTime"] + i * 60_000
            hi, lo = minutes["hits"].get(i, (820, 800))
            out.append([opened, "0", str(hi), str(lo), "810", "0", opened + 59_999])
        return out
    tail = m.TouchMarket(m.Store(":memory:"), spec); tail.get = tail_get; tail.start_ms = spec.deadline_ms - 6 * H
    assert tail.window_end == spec.deadline_ms + 60_000
    await tail.scan(spec.deadline_ms + 30_000)  # the deadline's minute is still running: the tail waits
    assert tail.history["through"] == tail_hour and not tail.verified_clear() and minutes["asked"] == [], tail.history
    await tail.scan(spec.deadline_ms + 120_000)
    assert minutes["asked"] == [(tail_hour, spec.deadline_ms + 59_999)] and tail.verified_clear(), tail.history
    assert tail.status() == "整个窗口都已核验：两条线都没碰到"
    # a touch in that last hour (minute 40) is found, never settled as "neither"
    tail.store.put(f"touch:{spec.slug}", {"kind": "clear", "through": tail_hour, "start": tail.start_ms})
    minutes["hits"][40] = (901, 805)
    await tail.scan(spec.deadline_ms + 120_000)
    assert tail.history["kind"] == "high" and tail.history["time"] == tail_hour + 40 * 60_000 and not tail.verified_clear()
    # a feed that does not reach the deadline's minute leaves the window unverified (read as far as it goes)
    minutes["hits"].clear(); minutes["upto"] = 50
    tail.store.put(f"touch:{spec.slug}", {"kind": "clear", "through": tail_hour, "start": tail.start_ms})
    await tail.scan(spec.deadline_ms + 120_000)
    assert tail.history["through"] == tail_hour + 50 * 60_000 and not tail.verified_clear(), tail.history
    minutes["upto"] = 10  # the next look starts there and reaches the deadline's minute
    await tail.scan(spec.deadline_ms + 180_000)
    assert minutes["asked"][-1][0] == tail_hour + 50 * 60_000 and tail.verified_clear(), tail.history

    # --- Bot: the Predict book is oriented to "$900 first" whatever the outcome order ------------------------------------
    class FakeMarket:
        def now_ms(self): return NOW
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off"})
    bot = m.Bot(cfg, m.Store(":memory:"), FakeMarket(), None)
    assert bot.predict_targets(NOW)["BNB"] == BNB.slug and bot.predict_targets(NOW)["SOL"] == SOL.slug
    assert {BNB.slug, SOL.slug} <= bot.predict.want_info
    bot.touches["BNB"].get = get; hits.clear()
    async def quiet(now_ms): pass
    bot.touches["SOL"].refresh = quiet
    bot.predict.info[BNB.slug] = {"outcomes": ["$700", "$900"], "created_ms": start}
    await bot.refresh_touch(NOW)
    assert bot.touches["BNB"].start_ms == start and bot.touches["BNB"].history["kind"] == "clear"
    bot.predict.books["BNB"] = m.PredictBook("BNB", BNB.slug, "1", "BNB", ((D("0.40"), D("100")),), ((D("0.45"), D("50")),), NOW)
    book, why = bot.touch_book(BNB)
    assert why == "" and book.bid == (D("0.55"), D("50")) and book.ask == (D("0.60"), D("100")), book  # 1 − $700 prices
    item = bot.touch_payload(bot.touches["BNB"], NOW)
    assert item["name"] == "BNB 先触 700/900" and item["touch"]["coin"] == "BNB"
    assert item["group"] == "crypto" and item["labels"] == ["$900", "$700"] and item["touch"]["price"] == "812.50", item
    assert abs(item["fair_up"] + item["fair_down"] - 1) < 1e-9 and item["predict"]["bids"][0] == [0.55, 50.0]
    assert {e["label"] for e in item["predict"]["edges"]} == {"挂900", "挂700", "吃900", "吃700"}, item["predict"]["edges"]
    bot.predict.info[BNB.slug]["outcomes"] = ["$900", "$700"]
    assert bot.touch_book(BNB)[0].bid == (D("0.40"), D("100"))
    bot.predict.info[BNB.slug]["outcomes"] = ["Yes", "No"]
    assert bot.touch_book(BNB)[0] is None and "方向未确认" in bot.touch_payload(bot.touches["BNB"], NOW)["predict"]["error"]
    # Predict REST market details -> outcome order and creation time
    async def fetch(url, payload=None):
        assert url.endswith("/markets/1")
        return {"success": True, "data": {"id": 1, "createdAt": "2026-09-20T08:00:00.000Z",
                                          "outcomes": [{"name": "$900", "indexSet": 2}, {"name": "$700", "indexSet": 1}]}}
    bot.predict.fetch = fetch
    await bot.predict.market_info(BNB.slug, "1")
    assert bot.predict.info[BNB.slug] == {"outcomes": ["$700", "$900"],
                                             "created_ms": int(dt.datetime(2026, 9, 20, 8, tzinfo=dt.timezone.utc).timestamp() * 1000)}
    btc = bot.touch_payload(bot.touches["BTC"], NOW)
    assert btc["name"] == "BTC 先触 70k/90k" and btc["labels"] == ["$90k", "$70k"] and btc["close_ms"] == BTC.deadline_ms, btc
    # the rules' window start wins over Predict's creation time
    bot.predict.info[BTC.slug] = {"outcomes": ["$70,000", "$90,000"], "created_ms": BTC.created_ms - 86_400_000}
    for t in bot.touches.values():
        t.refresh = quiet
    await bot.refresh_touch(NOW)
    assert bot.touches["BTC"].start_ms == BTC.created_ms
    # before the window opens there is nothing to scan; the horizon counts from the opening
    early = m.TouchMarket(m.Store(":memory:"), BTC); early.get = get
    await early.scan(BTC.created_ms - H); assert early.history == {}
    early.price, early.sigma, early.priced_ms = D("80000"), 0.5, BTC.created_ms
    assert early.odds(BTC.created_ms - 30 * 86_400_000) == early.odds(BTC.created_ms)
    sol = bot.touch_payload(bot.touches["SOL"], NOW)
    assert sol["name"] == "SOL 先触 60/140" and sol["labels"] == ["$140", "$60"] and sol["missing"].startswith("等待币安行情"), sol
    assert not m.Bot(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "BNB_TOUCH": "off"}), m.Store(":memory:"), FakeMarket(), None).predict_targets(NOW).get("BNB")

asyncio.run(run())
print("TOUCH_OK")
