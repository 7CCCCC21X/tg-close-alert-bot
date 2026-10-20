"""BTC / ETH Up/Down October 2026: Up when the Binance BTC/USDT (ETH/USDT) 1-minute candle of Oct 31 '26 23:59 ET closes
above the one of Sep 30 '26 23:59 ET, Down when below, 50-50 when equal. Both candles are read strictly by their open
time and kept; the model is log-normal with the 30-day hourly σ and no drift in the price."""
import asyncio, sys, math, json, time, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
OCT, ETH = m.UPDOWN_MARKETS
UTC = dt.timezone.utc
utc_ms = lambda *a: int(dt.datetime(*a, tzinfo=UTC).timestamp() * 1000)

# --- the market, from its rules: both candles are 23:59 ET in daylight time (UTC−4) ------------------------------
assert OCT.slug == "btc-up-down-october-2026" and OCT.symbol == "BTCUSDT" and OCT.name == "BTC 10月涨跌"
assert OCT.start_ms == utc_ms(2026, 10, 1, 3, 59) and OCT.end_ms == utc_ms(2026, 11, 1, 3, 59)
assert OCT.label(OCT.start_ms) == "09-30 23:59 ET（北京 10-01 11:59）", OCT.label(OCT.start_ms)
assert OCT.label(OCT.end_ms) == "10-31 23:59 ET（北京 11-01 11:59）", OCT.label(OCT.end_ms)
# its favourite / book key is its own: the BTC 先触 card already uses the bare pair
assert OCT.key not in {s.symbol for s in m.TOUCH_MARKETS} | {s.key for s in m.TOUCH_MARKETS}
# ETH: the same two minutes on ETH/USDT ("ETH closed September at $2,696.07 on Binance"), with its own key and records;
# the bare ETH / ETHUSDT are the ETH 先触 card's, ETH-HIT-10 the ETH price ladder's
assert (ETH.key, ETH.slug, ETH.symbol, ETH.name) == ("ETH-2026-10", "eth-up-down-october-2026", "ETHUSDT", "ETH 10月涨跌")
assert (ETH.start_ms, ETH.end_ms) == (OCT.start_ms, OCT.end_ms) and ETH.label(ETH.end_ms) == OCT.label(OCT.end_ms)
others = [*m.TOUCH_MARKETS, *m.RANGE_MARKETS, *m.FLIP_MARKETS, *m.CAP_MARKETS, *m.STOCK_HIT_MARKETS]
assert ETH.key not in {s.key for s in others} | {getattr(s, "symbol", "") for s in others}
assert len({s.key for s in m.UPDOWN_MARKETS}) == len({s.slug for s in m.UPDOWN_MARKETS}) == len(m.UPDOWN_MARKETS) == 2
# US Eastern offset without a tz database: EDT from 03-08 02:00 EST to 11-01 02:00 EDT in 2026
assert [m.us_eastern_offset(utc_ms(*t)) for t in [(2026, 3, 8, 6, 59), (2026, 3, 8, 7, 0), (2026, 11, 1, 5, 59), (2026, 11, 1, 6, 0)]] == [-5, -4, -4, -5]
assert m.us_eastern_offset(utc_ms(2027, 3, 14, 7, 0)) == -4 and m.us_eastern_offset(utc_ms(2027, 3, 14, 6, 59)) == -5

# --- model ------------------------------------------------------------------------------------------------------------
s = 0.5 * math.sqrt(30 / 365)
up, flat, down, z = m.updown_odds(100_000, 100_000, 0.5, 30 / 365)
# at the line: no drift in the price puts the median σ²τ/2 below it, so Up is a little under 50%
assert abs(up - m.norm_cdf(-s / 2)) < 1e-6 and 0.47 < up < 0.5 and 0 < flat < 1e-6 and abs(up + flat + down - 1) < 1e-12, (up, flat, down)
assert abs(z + s / 2) < 1e-9
assert abs(m.updown_odds(100_000, 100_000, 0.5, 30 / 365, drift=0)[0] - 0.5) < 1e-6  # the other drift convention
assert m.updown_odds(110_000, 100_000, 0.5, 1 / 365)[0] > 0.99 and m.updown_odds(90_000, 100_000, 0.5, 1 / 365)[2] > 0.99
assert m.updown_odds(100_000.01, 100_000, 0.5, 0)[:3] == (1.0, 0.0, 0.0)
assert m.updown_odds(100_000, 100_000, 0.5, 0)[:3] == (0.0, 1.0, 0.0)  # a tie settles 50-50
assert m.UpDownOdds(0.0, 1.0, 0.0, 0.0, D(1), D(1), 0.0, True).fair_up == 0.5

NOW = utc_ms(2026, 10, 1, 8, 15)  # 10-01 16:15 Beijing
H = 3_600_000
# 721 finished hourly closes alternating ±0.3% (σ ≈ 28%), then the running hour
rows = [[NOW - (722 - i) * H, "0", "0", "0", str(100_000 * (1.003 if i % 2 else 1)), "0", NOW - (721 - i) * H - 1] for i in range(722)]
rows.append([NOW - NOW % H, "0", "0", "0", "100100", "0", NOW - NOW % H + H - 1])


class Feed:
    """Binance spot as the market sees it: a ticker, hourly bars, and 1-minute candles by open time. A missing minute
    is answered with the next one, as Binance does."""
    def __init__(self):
        self.price, self.candles, self.calls, self.down = "118500.00", {}, [], False

    async def get(self, path, **p):
        self.calls.append((path, p.get("interval"), p.get("startTime"), p.get("symbol")))
        if self.down:
            raise m.RemoteError("data-api.binance.vision: timed out")
        if path == "ticker/price":
            return {"symbol": p["symbol"], "price": self.price}
        if p["interval"] == "1h":
            return rows[-p["limit"]:]
        t = p["startTime"]
        if t in self.candles:
            return [[t, "0", "0", "0", self.candles[t], "0", t + 59_999]]
        return [[t + 60_000, "0", "0", "0", "1", "0", t + 119_999]]


async def run():
    store = m.Store(":memory:")
    mk = m.UpDownMarket(store, OCT)
    feed = Feed(); mk.get = feed.get

    async def refresh(at):  # the market's own timers run on the monotonic clock: make each part due
        mk.times.update(price=-1e9, candle=-1e9)
        await mk.refresh(at)

    # --- before the starting minute is over there is no line yet, and nothing is asked for ----------------------
    await refresh(OCT.start_ms + 30_000)
    assert mk.odds(OCT.start_ms + 30_000) == "起点价要等 09-30 23:59 ET（北京 10-01 11:59）这根 1 分钟 K 收盘后确定"
    assert not [c for c in feed.calls if c[1] == "1m"]
    # --- a neighbouring minute is never taken for the starting candle ------------------------------------------------
    await refresh(NOW)
    assert mk.close_of("start") is None and "没有返回 09-30 23:59 ET" in mk.error, mk.error
    assert mk.odds(NOW).startswith("等待币安 09-30 23:59 ET（北京 10-01 11:59）这根 1 分钟 K 的收盘价（"), mk.odds(NOW)
    assert mk.price == D("118500.00") and mk.sigma and 0.2 < mk.sigma < 0.35, mk.sigma  # the rest still refreshed
    # --- the real candle: read once, kept, and a restart reads it back without asking ------------------------------
    feed.candles[OCT.start_ms] = "117234.56"
    await refresh(NOW)
    assert mk.close_of("start") == D("117234.56") and not mk.error
    assert store.get(f"updown:{OCT.slug}:start")["open"] == OCT.start_ms
    asked = len([c for c in feed.calls if c[1] == "1m"])
    await refresh(NOW + 60_000)
    assert len([c for c in feed.calls if c[1] == "1m"]) == asked, "a kept candle is not asked for again"
    again = m.UpDownMarket(store, OCT)
    assert again.close_of("start") == D("117234.56")
    # --- the odds: price vs the line, σ over the time left until the final candle closes ----------------------------
    at = NOW + 60_000
    o = mk.odds(at)
    years = (OCT.end_ms + 60_000 - at) / m.YEAR_MS
    want = m.updown_odds(118500.0, 117234.56, mk.sigma, years)
    assert isinstance(o, m.UpDownOdds) and (o.up, o.flat, o.down) == want[:3] and o.line == D("117234.56") and o.price == D("118500.00")
    assert 0.5 < o.fair_up < 0.6 and abs(o.fair_up + o.fair_down - 1) < 1e-12 and o.years == years, o
    assert mk.advice_problem(o, at) == ""
    # model error: σ ×/÷ 1.25, and the drift convention, which dominates at the line (a few cents over a month)
    fair = lambda price, sigma, drift=-0.5: (lambda u, f, d, z: u + f / 2)(*m.updown_odds(price, 117234.56, sigma, years, drift))
    assert abs(mk.model_swing(o) - max(abs(fair(118500.0, mk.sigma * k) - o.fair_up) for k in (1.25, 1 / 1.25))) < 1e-12
    level = m.UpDownOdds(*m.updown_odds(117234.56, 117234.56, mk.sigma, years), D("117234.56"), D("117234.56"), years)
    sigma_only = max(abs(fair(117234.56, mk.sigma * k) - level.fair_up) for k in (1.25, 1 / 1.25))
    assert abs(mk.model_swing(level) - (0.5 - level.fair_up)) < 1e-6 and mk.model_swing(level) > 3 * sigma_only > 0, (level, sigma_only)
    # --- inputs gone stale: no odds at all from an old price; σ a day old: odds shown, no advice ---------------------
    late = NOW + 7 * 60_000
    feed.down = True
    await refresh(late)
    assert mk.odds(late) == "币安价格停在 10-01 16:16（6 分钟未更新：data-api.binance.vision: timed out），暂停概率", mk.odds(late)
    feed.down = False
    await refresh(late)
    assert isinstance(mk.odds(late), m.UpDownOdds)
    assert mk.advice_problem(mk.odds(late), mk.sigma_ms + m.UpDownMarket.SIGMA_STALE_MS + 1) == "波动率超过一天未更新；暂不给建议"

    # --- the web card and the Predict book ---------------------------------------------------------------------------
    class FM:
        def __init__(self, now): self.now, self.config = now, None
        def now_ms(self): return self.now
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off"})
    bot = m.Bot(cfg, store, FM(late), None)
    bot.updowns[OCT.key] = mk
    assert bot.predict_targets(late)[OCT.key] == OCT.slug and OCT.slug in bot.predict.want_info
    assert "涨跌市场" in [name for name, _ in bot.reference_jobs()]
    card = next(i for i in bot.odds_payload()["items"] if i["name"] == "BTC 10月涨跌")
    assert card["group"] == "crypto" and card["symbol"] == "BTC-2026-10" and card["close_ms"] == OCT.end_ms + 60_000
    assert card["ref"] == "117,234.56" and card["ref_rel"] == "起点" and card["effective"] == "118,500.00" and card["unit"] == "USDT"
    assert card["close_label"].startswith("10-31 23:59 ET（北京 11-01 11:59）这根 1 分钟 K 的收盘价") and not card["warn"]
    assert abs(card["sigma"] - mk.sigma * math.sqrt(card["remaining"] / 365)) < 1e-12 and card["fair_up"] == mk.odds(late).fair_up
    assert card["predict"]["url"].startswith(m.PREDICT_SITE + OCT.slug) and "edges" not in card["predict"]
    json.dumps(card)
    # the book prices the first outcome: "Up" as it is, "Down" flipped, anything else is not compared
    book = m.PredictBook(OCT.key, OCT.slug, "77", "BTC Up/Down October 2026", ((D("0.48"), D("500")),), ((D("0.50"), D("400")),), late)
    bot.predict.books[OCT.key] = book
    bot.predict.info[OCT.slug] = {"outcomes": ["Up", "Down"], "created_ms": 0}
    fair = mk.odds(late).fair_up
    card = next(i for i in bot.odds_payload()["items"] if i["name"] == "BTC 10月涨跌")
    edges = card["predict"]["edges"]
    assert [e["label"] for e in edges] == ["挂涨", "挂跌", "吃涨", "吃跌"] and abs(edges[0]["edge"] - (fair - 0.48)) < 1e-9, edges
    assert card["predict"]["need"] == max(cfg.predict_min_edge, mk.model_swing(mk.odds(late)))
    best = [e for e in edges if e["best"]]
    assert len(best) == 1 and best[0]["label"] == "挂涨", edges  # a 48¢ bid against a fair ~54¢
    bot.predict.info[OCT.slug]["outcomes"] = ["Down", "Up"]  # the same book now prices Down: Up is 50–52¢
    card = next(i for i in bot.odds_payload()["items"] if i["name"] == "BTC 10月涨跌")
    assert card["predict"]["bids"] == [[0.5, 400.0]] and card["predict"]["asks"] == [[0.52, 500.0]], card["predict"]
    bot.predict.info[OCT.slug]["outcomes"] = ["Yes", "No"]
    card = next(i for i in bot.odds_payload()["items"] if i["name"] == "BTC 10月涨跌")
    assert "edges" not in card["predict"] and card["predict"]["error"].startswith("盘口方向未确认（结果名称：Yes、No）")
    bot.predict.info[OCT.slug]["outcomes"] = ["Up", "Down"]
    # Predict's own target price, when its category shows one, must be the Binance opening close we read
    bot.predict.strikes[OCT.slug] = (D("117234.56"), time.monotonic())
    card = next(i for i in bot.odds_payload()["items"] if i["name"] == "BTC 10月涨跌")
    assert card["ref_note"].endswith("；与 Predict 目标价一致") and not card["warn"] and any(e["best"] for e in card["predict"]["edges"])
    bot.predict.strikes[OCT.slug] = (D("117500"), time.monotonic())
    card = next(i for i in bot.odds_payload()["items"] if i["name"] == "BTC 10月涨跌")
    assert card["warn"] == "Predict 目标价 117,500.00 与币安起点 117,234.56 不一致，请核实；暂不给建议", card["warn"]
    assert not any(e["best"] for e in card["predict"]["edges"])
    del bot.predict.strikes[OCT.slug]
    # σ a day old: the card says so and nothing is recommended
    bot.market.now = mk.sigma_ms + m.UpDownMarket.SIGMA_STALE_MS + 1
    mk.priced_ms = bot.market.now
    bot.predict.books[OCT.key] = m.dataclasses.replace(book, fetched_ms=bot.market.now)
    card = next(i for i in bot.odds_payload()["items"] if i["name"] == "BTC 10月涨跌")
    assert card["warn"] == "波动率超过一天未更新；暂不给建议" and not any(e["best"] for e in card["predict"]["edges"])

    # --- settlement: after the final minute the card waits for its close, then shows the result --------------------
    end = OCT.end_ms + 60_000 + 5_000
    await refresh(end)
    assert mk.odds(end).startswith("已到结算时刻，等待币安 10-31 23:59 ET（北京 11-01 11:59）这根 1 分钟 K 的收盘价（"), mk.odds(end)
    feed.candles[OCT.end_ms] = "118000.10"
    before = len(feed.calls)
    await refresh(end)
    o = mk.odds(end)
    assert o.settled and (o.up, o.flat, o.down) == (1.0, 0.0, 0.0) and o.price == D("118000.10") and not mk.error
    assert all(c[0] == "klines" and c[1] == "1m" for c in feed.calls[before:]), "settled: no more prices or σ"
    bot.market.now = end
    bot.predict.books[OCT.key] = m.dataclasses.replace(book, fetched_ms=end)
    card = next(i for i in bot.odds_payload()["items"] if i["name"] == "BTC 10月涨跌")
    assert card["fair_up"] == 1.0 and card["eff_label"] == "终点" and card["effective"] == "118,000.10" and card["warn"].startswith("已出结果")
    assert "→ 涨" in card["proxy_note"] and not any(e["best"] for e in card["predict"]["edges"]), card
    json.dumps(card)
    # a tie settles 50-50
    store.put(f"updown:{OCT.slug}:end", {"open": OCT.end_ms, "close": "117234.56"})
    o = mk.odds(end)
    assert (o.fair_up, o.fair_down) == (0.5, 0.5)
    # a record for another minute (an older rules reading) is not this market's
    store.put(f"updown:{OCT.slug}:end", {"open": OCT.end_ms - 60_000, "close": "1"})
    assert mk.close_of("end") is None

    # --- ETH 10月涨跌: its own pair, records, book, target price, card, paper trades and /edge name --------------------
    store = m.Store(":memory:")
    eth = m.UpDownMarket(store, ETH)
    feed = Feed(); eth.get = feed.get
    feed.price, feed.candles[ETH.start_ms] = "2750.00", "2696.07"
    eth.times.update(price=-1e9, candle=-1e9)
    await eth.refresh(NOW)
    assert {c[3] for c in feed.calls} == {"ETHUSDT"} and eth.close_of("start") == D("2696.07") and not eth.error, feed.calls
    assert store.get(f"updown:{ETH.slug}:start")["close"] == "2696.07" and store.get(f"updown:{OCT.slug}:start") is None
    o = eth.odds(NOW)
    assert isinstance(o, m.UpDownOdds) and (o.line, o.price) == (D("2696.07"), D("2750.00")) and 0.5 < o.fair_up < 1, o
    bot = m.Bot(cfg, store, FM(NOW), None)
    bot.updowns[ETH.key] = eth
    assert bot.predict_targets(NOW)[ETH.key] == ETH.slug and ETH.slug in bot.predict.want_info
    items = bot.odds_payload()["items"]
    names = [i["name"] for i in items]
    assert names[names.index("BTC 10月涨跌") + 1] == "ETH 10月涨跌", names
    syms = [i["symbol"] for i in items]
    assert len(syms) == len(set(syms)), syms  # every card has its own favourite key
    card = next(i for i in items if i["name"] == "ETH 10月涨跌")
    assert card["symbol"] == "ETH-2026-10" and card["group"] == "crypto" and card["close_ms"] == ETH.end_ms + 60_000
    assert card["ref"] == "2,696.07" and card["effective"] == "2,750.00" and card["fair_up"] == o.fair_up and not card["warn"]
    assert card["ref_note"] == "币安 ETHUSDT 1 分钟 K 收盘：09-30 23:59 ET，北京 10-01 11:59", card["ref_note"]
    assert card["proxy_note"] == "不用代理：币安 ETHUSDT 现价就是结算源" and card["predict"]["url"].startswith(m.PREDICT_SITE + ETH.slug)
    # Predict's book and target price are ETH's own: the BTC card stays without a book
    book = m.PredictBook(ETH.key, ETH.slug, "88", "ETH Up/Down October 2026", ((D("0.40"), D("300")),), ((D("0.45"), D("300")),), NOW)
    bot.predict.books[ETH.key] = book
    bot.predict.info[ETH.slug] = {"outcomes": ["Up", "Down"], "created_ms": 0}
    bot.predict.strikes[ETH.slug] = (D("2696.07"), time.monotonic())
    items = bot.odds_payload()["items"]
    card = next(i for i in items if i["name"] == "ETH 10月涨跌")
    assert card["ref_note"].endswith("；与 Predict 目标价一致") and not card["warn"]
    assert [e["label"] for e in card["predict"]["edges"] if e["best"]] == ["挂涨"], card["predict"]["edges"]  # 40¢ bid vs fair > 50¢
    assert "edges" not in next(i for i in items if i["name"] == "BTC 10月涨跌")["predict"]
    bot.predict.strikes[ETH.slug] = (D("2700"), time.monotonic())
    card = next(i for i in bot.odds_payload()["items"] if i["name"] == "ETH 10月涨跌")
    assert card["warn"] == "Predict 目标价 2,700.00 与币安起点 2,696.07 不一致，请核实；暂不给建议", card["warn"]
    bot.predict.strikes[ETH.slug] = (D("2696.07"), time.monotonic())
    (sim,) = [x for x in bot.sim_markets(NOW) if x.kind == "updown"]
    assert (sim.market, sim.item, sim.key, sim.fair_up, sim.settle) == (ETH.slug, "ETH 10月涨跌", ETH.key, o.fair_up, {"end": ETH.end_ms})
    assert bot.edge_target(["ETH", "10月涨跌"]) == ("key:ETH-2026-10", "ETH 10月涨跌") and bot.edge_target(["eth-2026-10"])[0] == "key:ETH-2026-10"
    assert bot.edge_target(["ETH"])[0] == "key:ETH"  # still the ETH 先触 card
    try:
        bot.edge_target(["10月涨跌"]); assert False, "two markets answer to 10月涨跌"
    except ValueError as error:
        assert "BTC 10月涨跌、ETH 10月涨跌" in str(error), error
    # settles on its own final candle
    end = ETH.end_ms + 60_000 + 5_000
    feed.candles[ETH.end_ms] = "2650.00"
    eth.times.update(price=-1e9, candle=-1e9)
    await eth.refresh(end)
    o = eth.odds(end)
    assert o.settled and (o.up, o.flat, o.down) == (0.0, 0.0, 1.0) and o.price == D("2650.00") and store.get(f"updown:{OCT.slug}:end") is None
    res = bot.sim_result({"kind": "updown", "key": ETH.key, "side": "up", "settle": {"end": ETH.end_ms}}, end)
    assert res[0] == 0.0 and res[1] == "终点 2,650.00，起点 2,696.07" and (res[2]["start"], res[2]["end"]) == (2696.07, 2650.0), res

    # --- switched off with the rest of the 加密 section ---------------------------------------------------------------
    off = m.Bot(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "BNB_TOUCH": "off"}), m.Store(":memory:"), FM(late), None)
    assert not {OCT.key, ETH.key} & set(off.predict_targets(late)) and "涨跌市场" not in [name for name, _ in off.reference_jobs()]
    assert not any(i["name"] in {"BTC 10月涨跌", "ETH 10月涨跌"} for i in off.odds_payload()["items"])
    print("UPDOWN_OK")


asyncio.run(run())
