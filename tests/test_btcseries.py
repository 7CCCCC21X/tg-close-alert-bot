"""BTC 涨跌: Predict's rolling BTC up/down series (5 minutes / 1 hour / 1 day), a new market every period, each window
named for its time. The bot builds the slug of the window running now, reads exactly the Binance candles that fix its
line and its result (kept in SQLite), prices it like the monthly up/down card, shows the three cards in a section of
their own (hidden until ticked in 自定义), and paper-trades them in a block of their own."""
import asyncio, sys, math, json, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
UTC = dt.timezone.utc
utc_ms = lambda *a: int(dt.datetime(*a, tzinfo=UTC).timestamp() * 1000)
FIVE, HOUR, DAY = m.BTC_SERIES
MIN, H = 60_000, 3_600_000

# --- the three series and their windows, from the market pages given --------------------------------------------------
assert [(s.key, s.cycle, s.symbol, s.name, s.tie, s.interval, s.line_field) for s in m.BTC_SERIES] == [
    ("BTC-5M", "5m", "BTCUSDT", "BTC 5分钟涨跌", "split", "5m", "close"), ("BTC-1H", "1h", "BTCUSDT", "BTC 1小时涨跌", "up", "1h", "open"),
    ("BTC-1D", "1d", "BTCUSDT", "BTC 1天涨跌", "split", "1m", "close")]
others = [*m.TOUCH_MARKETS, *m.UPDOWN_MARKETS, *m.RANGE_MARKETS, *m.FLIP_MARKETS, *m.CAP_MARKETS, *m.STOCK_HIT_MARKETS, *m.MACRO_MARKETS, *m.QUOTE_MARKETS]
assert not {s.key for s in m.BTC_SERIES} & ({s.key for s in others} | {getattr(s, "symbol", "") for s in others})
assert m.SIM_KINDS["btc"] == "BTC 涨跌" and m.SERIES_CYCLES == {"5m": "5分钟", "1h": "1小时", "1d": "1天"}

NOW = utc_ms(2026, 10, 20, 3, 42, 10)  # 10-19 23:42:10 ET (EDT), 10-20 11:42:10 Beijing: the three pages were open then
w5 = m.series_window(FIVE, NOW)
assert w5.slug == "btc-updown-5m-1792467600" and (w5.start_ms, w5.end_ms) == (utc_ms(2026, 10, 20, 3, 40), utc_ms(2026, 10, 20, 3, 45))
assert (w5.line_at, w5.final_at, w5.interval, w5.interval_ms) == (w5.start_ms - 5 * MIN, w5.start_ms, "5m", 5 * MIN)
assert w5.label() == "23:40–23:45 ET（北京 11:40–11:45）" and w5.label(plain=True) == "23:40–23:45 ET，北京 11:40–11:45" and w5.short() == "10-20 11:40"
w1 = m.series_window(HOUR, NOW)
assert w1.slug == "bitcoin-up-or-down-october-19-2026-11pm-et" and (w1.start_ms, w1.end_ms) == (utc_ms(2026, 10, 20, 3), utc_ms(2026, 10, 20, 4))
assert (w1.line_at, w1.final_at, w1.interval) == (w1.start_ms, w1.start_ms, "1h")  # the hour's own candle: its open is the line, its close the result
assert w1.label() == "10-19 23:00–00:00 ET（北京 10-20 11:00–12:00）" and w1.short() == "10-20 11:00"
wd = m.series_window(DAY, NOW)
assert wd.slug == "bitcoin-up-or-down-on-october-20-2026" and wd.start_ms == utc_ms(2026, 10, 19, 16) and wd.final_at == utc_ms(2026, 10, 20, 16)
assert wd.end_ms == wd.final_at + MIN and wd.line_at == wd.start_ms and wd.interval == "1m"  # 12:00 ET both days (EDT = 16:00 UTC)
assert wd.label() == "10-19 12:00 → 10-20 12:00 ET（北京 10-20 00:00 → 10-21 00:00）" and wd.short() == "10-20 ET"
# the hour slugs around noon and midnight, and across the end of daylight time (11-01 02:00 EDT -> 01:00 EST)
hour_slug = lambda *t: m.series_window(HOUR, utc_ms(*t)).slug
assert hour_slug(2026, 10, 20, 4, 30) == "bitcoin-up-or-down-october-20-2026-12am-et"
assert hour_slug(2026, 10, 20, 16, 5) == "bitcoin-up-or-down-october-20-2026-12pm-et" and hour_slug(2026, 10, 20, 17, 30) == "bitcoin-up-or-down-october-20-2026-1pm-et"
assert hour_slug(2026, 11, 1, 5, 30) == "bitcoin-up-or-down-november-1-2026-1am-et" and hour_slug(2026, 11, 1, 7, 30) == "bitcoin-up-or-down-november-1-2026-2am-et"
assert hour_slug(2026, 11, 1, 6, 30) == "bitcoin-up-or-down-november-1-2026-1am-et"  # the repeated 1 am hour (EST) carries the same name as EDT's
# the day windows are back to back and share their 12:00 candle; the one across the switch is 25 hours long
assert m.series_window(DAY, utc_ms(2026, 10, 20, 16, 0, 30)).slug == "bitcoin-up-or-down-on-october-20-2026"  # its 12:00 candle is still open
nxt = m.series_window(DAY, utc_ms(2026, 10, 20, 16, 1))
assert nxt.slug == "bitcoin-up-or-down-on-october-21-2026" and nxt.start_ms == wd.final_at and nxt.line_at == wd.final_at
dst = m.series_window(DAY, utc_ms(2026, 11, 1, 10))
assert (dst.slug, dst.start_ms, dst.final_at) == ("bitcoin-up-or-down-on-november-1-2026", utc_ms(2026, 10, 31, 16), utc_ms(2026, 11, 1, 17))
for spec, w in ((FIVE, w5), (HOUR, w1), (DAY, wd), (DAY, dst)):
    assert m.series_window_from(spec, w.start_ms) == w  # a window rebuilt from its start (the paper trader's record)
assert m.series_window(FIVE, w5.end_ms).start_ms == w5.end_ms and m.series_window(HOUR, w1.end_ms).start_ms == w1.end_ms  # back to back

# --- settings -------------------------------------------------------------------------------------------------------------
c = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x"})
assert c.btc_updown and c.btc_updown_alerts == frozenset({"1h", "1d"}) and c.sim_markets == frozenset({"close", "btc"})
assert m.sim_scope(c) == ("只吃单", "指数/个股日涨跌、BTC 涨跌")
assert m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "BTC_UPDOWN": "off", "BTC_UPDOWN_ALERTS": "off"}).btc_updown_alerts == frozenset()
assert not m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "BTC_UPDOWN": "off"}).btc_updown
assert m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "BTC_UPDOWN_ALERTS": "all"}).btc_updown_alerts == frozenset({"5m", "1h", "1d"})
assert m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "BTC_UPDOWN_ALERTS": "5M, 1d"}).btc_updown_alerts == frozenset({"5m", "1d"})
assert m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SIM_MARKETS": "close"}).sim_markets == frozenset({"close"})
try:
    m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "BTC_UPDOWN_ALERTS": "15m"}); assert False
except ValueError:
    pass
assert m.parse_series_alerts("") == frozenset() and m.parse_series_alerts("none") == frozenset()

# --- the paper trader's view of a window --------------------------------------------------------------------------------
settle = {"end": w5.end_ms, "start": w5.start_ms, "cycle": "5m", "tie": "split", "line": 118100.0}
assert m.market_driver("btc", "BTC-5M", settle, "BTC 5分钟涨跌 10-20 11:40") == (f"BTCUSDT@5m:{w5.start_ms}", "BTC 5分钟涨跌 10-20 11:40")
assert m.market_driver("btc", "BTC-1H", {**settle, "cycle": "1h"}, "")[0] == f"BTCUSDT@1h:{w5.start_ms}"
assert m.scenario_result("btc", settle, "up", None) == 1.0 and m.scenario_result("btc", settle, "down", None) == 0.0
assert m.scenario_result("btc", settle, "flat", None) == 0.5 and m.scenario_result("btc", {**settle, "tie": "up"}, "flat", None) == 1.0
assert m.scenario_text("BTC 5分钟涨跌 10-20 11:40", {"btc"}, "down", None) == "BTC 5分钟涨跌 10-20 11:40 收跌"
pos = [{"kind": "btc", "side": "up", "settle": settle, "price": 0.6, "shares": 100.0, "market": w5.slug, "status": "filled", "item": "x", "key": "BTC-5M"}]
worst, event = m.group_worst_case(pos, "BTC 5分钟涨跌 10-20 11:40")
assert abs(worst + 60) < 1e-9 and event == "BTC 5分钟涨跌 10-20 11:40 收跌"
assert m.sim_groups(pos)[0]["driver"] == f"BTCUSDT@5m:{w5.start_ms}"

# --- Binance as the markets see it ------------------------------------------------------------------------------------------
# 721 finished hourly closes alternating ±0.3% (σ ≈ 28%) before the 5-minute window opens, then the running hour
S0 = w5.start_ms - w5.start_ms % H
rows = [[S0 - (721 - i) * H, "0", "0", "0", str(100_000 * (1.003 if i % 2 else 1)), "0", S0 - (720 - i) * H - 1] for i in range(721)]
rows.append([S0, "0", "0", "0", "100100", "0", S0 + H - 1])


class Feed:
    """Binance spot: a ticker, hourly bars, and candles by (interval, open time) -> (open, close). A missing candle is
    answered with the next one, as Binance does."""
    def __init__(self):
        self.price, self.candles, self.calls, self.down = "118500.00", {}, [], False

    async def get(self, path, **p):
        self.calls.append((path, p.get("interval"), p.get("startTime"), p.get("symbol")))
        if self.down:
            raise m.RemoteError("data-api.binance.vision: timed out")
        if path == "ticker/price":
            return {"symbol": p["symbol"], "price": self.price}
        if p["interval"] == "1h" and "startTime" not in p:
            return rows[-p["limit"]:]
        t, iv = p["startTime"], m.INTERVAL_MS[p["interval"]]
        if (p["interval"], t) in self.candles:
            o, c = self.candles[(p["interval"], t)]
            return [[t, o, "0", "0", c, "0", t + iv - 1]]
        return [[t + iv, "1", "0", "0", "1", "0", t + 2 * iv - 1]]


class FM:
    def __init__(self, now): self.now, self.config = now, None
    def now_ms(self): return self.now


async def run():
    store = m.Store(":memory:")
    feed = Feed()
    spot = m.SpotFeed(); spot.get = feed.get
    mk = m.SeriesMarket(store, FIVE, spot)

    async def refresh(market, at):  # the market's own timers run on the monotonic clock: make every part due
        market.times["candle"] = -1e9
        spot.times.clear()
        await market.refresh(at)

    # --- the 5-minute window: its line is the previous candle's close, read once that candle is over -------------------
    S = w5.start_ms
    await refresh(mk, S + 1_000)
    assert mk.window == w5 and mk.odds(S + 1_000) == "起点价要等 10-19 23:35 ET（北京 10-20 11:35）这根 5 分钟 K 线收盘后确定"
    assert not [c for c in feed.calls if c[2] is not None] and mk.price == D("118500.00") and 0.2 < mk.sigma < 0.35  # the rest refreshed
    await refresh(mk, S + 3_000)  # due, but Binance answers with the next candle: never taken for it
    assert mk.line_of(w5) is None and "没有返回 10-19 23:35 ET" in mk.error
    assert mk.odds(S + 3_000).startswith("等待币安 10-19 23:35 ET（北京 10-20 11:35）这根 5 分钟 K 线的收盘价（")
    feed.candles[("5m", w5.line_at)] = ("118000.00", "118100.00")
    await refresh(mk, S + 4_000)
    assert mk.line_of(w5) == D("118100.00") and not mk.error
    rec = store.get(f"series:{w5.slug}")
    assert rec["line"] == "118100.00" and rec["line_at"] == w5.line_at and (rec["cycle"], rec["start"], rec["end"]) == ("5m", S, w5.end_ms) and "final" not in rec
    asked = len([c for c in feed.calls if c[2] is not None])
    await refresh(mk, NOW)
    assert len([c for c in feed.calls if c[2] is not None]) == asked, "a kept line is not asked for again (the final is not due)"
    # --- the odds: the price against the line over the time left, 50/50 for an equal close -----------------------------
    o = mk.odds(NOW)
    years = (w5.end_ms - NOW) / m.YEAR_MS
    want = m.updown_odds(118500.0, 118100.0, mk.sigma, years)
    assert isinstance(o, m.SeriesOdds) and (o.up, o.flat, o.down) == want[:3] and o.line == D("118100.00") and o.price == D("118500.00")
    assert o.fair_up == o.up + o.flat / 2 and abs(o.fair_up + o.fair_down - 1) < 1e-12 and o.years == years and o.tie == "split" and not o.settled
    assert o.fair_up > 0.99, o  # +0.34% with under three minutes left
    assert mk.advice_problem(o, NOW) == "" and mk.model_swing(o) > 0
    level = m.SeriesOdds(*m.updown_odds(118100.0, 118100.0, mk.sigma, years), D("118100"), D("118100"), years, "split")
    assert 0.49 < level.fair_up < 0.5 and 0 < mk.model_swing(level) < 0.001  # at the line: σ²τ/2 is nothing over five minutes
    assert mk.advice_problem(o, mk.sigma_ms + m.SeriesMarket.SIGMA_STALE_MS + 1) == "波动率超过一天未更新；暂不给建议"
    # a stale quote prices nothing (a minute is long for a 5-minute window)
    feed.down = True
    await refresh(mk, NOW + 61_000)
    assert mk.odds(NOW + 61_000) == "币安价格停在 10-20 11:42:10（61 秒未更新：data-api.binance.vision: timed out），暂停概率", mk.odds(NOW + 61_000)
    feed.down = False
    await refresh(mk, NOW + 62_000)
    assert isinstance(mk.odds(NOW + 62_000), m.SeriesOdds)
    # the last seconds: priced, but no suggestion (the book is refreshed every 15 s)
    late = w5.end_ms - 15_000
    await refresh(mk, late)
    assert isinstance(mk.odds(late), m.SeriesOdds) and mk.advice_problem(mk.odds(late), late) == "窗口只剩 15 秒，盘口的刷新跟不上；暂不给建议"
    # --- the window ends: the next one takes over, the ended one's result is read from its candle once it is over ------
    E = w5.end_ms
    assert mk.odds(E, w5) == "窗口已结束，等待币安 10-19 23:40 ET（北京 10-20 11:40）这根 5 分钟 K 线的收盘价"
    await refresh(mk, E + 1_000)
    nw = mk.window
    assert nw.slug == f"btc-updown-5m-{E // 1000}" and nw.start_ms == E and [w.slug for w in mk.pending] == [w5.slug]
    assert mk.final_of(w5) is None and mk.line_of(nw) is None  # the 23:40 candle is over at 23:45:00; asked for after the grace
    assert mk.odds(E + 1_000) == "起点价要等 10-19 23:40 ET（北京 10-20 11:40）这根 5 分钟 K 线收盘后确定"
    feed.candles[("5m", S)] = ("118100.00", "118050.00")
    await refresh(mk, E + 3_000)
    assert mk.final_of(w5) == D("118050.00") and mk.line_of(nw) == D("118050.00") and mk.pending == [] and not mk.error
    assert store.get(f"series:{w5.slug}")["final"] == "118050.00" and store.get(f"series:{nw.slug}")["line"] == "118050.00"
    done = mk.odds(E + 3_000, w5)
    assert done.settled and (done.up, done.flat, done.down, done.fair_up) == (0.0, 0.0, 1.0, 0.0) and done.price == D("118050.00")
    assert mk.advice_problem(done, E + 3_000) == "已出结果，以 Predict 结算为准；不再给建议" and mk.model_swing(done) == 0.0
    again = m.SeriesMarket(m.Store(":memory:"), FIVE, spot)  # a restart reads a kept window back, and never asks for it again
    again.store = store
    assert again.line_of(w5) == D("118100.00") and again.final_of(w5) == D("118050.00")
    # an equal close settles 50/50 here (and as 涨 on the hourly market, below)
    store.put(f"series:{w5.slug}", {**store.get(f"series:{w5.slug}"), "final": "118100.00"})
    assert mk.odds(E + 3_000, w5).fair_up == 0.5
    # a window that ended before the bot looked (a restart): asked for on request, read on the next round
    old = m.series_window_from(FIVE, S - 10 * MIN)
    feed.candles[("5m", old.line_at)], feed.candles[("5m", old.final_at)] = ("117000.00", "117010.00"), ("117010.00", "117020.00")
    mk.want(old); mk.want(old); mk.want(nw)  # once; never the running window
    assert [w.slug for w in mk.pending] == [old.slug]
    await refresh(mk, E + 10_000)
    assert (mk.line_of(old), mk.final_of(old)) == (D("117010.00"), D("117020.00")) and mk.pending == []
    # kept three days, then pruned
    store.put("series:btc-updown-5m-1", {"line": "1", "final": "2", "end": E - 4 * m.DAY_MS})
    mk.times["prune"] = -1e9
    await refresh(mk, E + 11_000)
    assert store.get("series:btc-updown-5m-1") is None and store.get(f"series:{old.slug}") is not None

    # --- the hourly window: the hour's own candle, its open the line as soon as it trades; close >= open is 涨 ------------
    hk = m.SeriesMarket(store, HOUR, spot)
    T = w1.start_ms
    await refresh(hk, T + 1_000)
    assert hk.odds(T + 1_000) == "起点价要等 10-19 23:00 ET（北京 10-20 11:00）这根 1 小时 K 线开盘后确定"
    feed.candles[("1h", T)] = ("118200.00", "118500.00")
    await refresh(hk, T + 3_000)
    assert hk.line_of(w1) == D("118200.00") and hk.final_of(w1) is None  # the running candle's open; its close only once the hour is over
    await refresh(hk, NOW)
    ho = hk.odds(NOW)
    assert isinstance(ho, m.SeriesOdds) and ho.tie == "up" and ho.fair_up == ho.up + ho.flat and ho.line == D("118200.00")
    await refresh(hk, w1.end_ms + 3_000)
    assert hk.window.slug == "bitcoin-up-or-down-october-20-2026-12am-et" and hk.final_of(w1) == D("118500.00")
    assert hk.odds(w1.end_ms + 3_000, w1).fair_up == 1.0
    feed.candles[("1h", T)] = ("118200.00", "118200.00")  # an equal close: 涨 on this series
    store.put(f"series:{w1.slug}", {**store.get(f"series:{w1.slug}"), "final": "118200.00"})
    tie = hk.odds(w1.end_ms + 3_000, w1)
    assert (tie.up, tie.flat, tie.down, tie.fair_up) == (0.0, 1.0, 0.0, 1.0)

    # --- the daily window: the two 12:00 ET 1-minute candles, each read once its minute is over ------------------------------
    dk = m.SeriesMarket(store, DAY, spot)
    await refresh(dk, NOW)
    assert dk.window == wd and "没有返回 10-19 12:00 ET（北京 10-20 00:00）这根 1 分钟 K 线" in dk.error
    feed.candles[("1m", wd.line_at)] = ("117000.00", "117234.56")
    await refresh(dk, NOW + 1_000)
    assert dk.line_of(wd) == D("117234.56") and isinstance(dk.odds(NOW + 1_000), m.SeriesOdds)
    assert dk.odds(NOW + 1_000).years == (wd.end_ms - NOW - 1_000) / m.YEAR_MS
    feed.candles[("1m", wd.final_at)] = ("118000.00", "118001.00")
    await refresh(dk, wd.final_at + 30_000)  # the final minute still running: the window is still this one, nothing read
    assert dk.window == wd and dk.final_of(wd) is None
    await refresh(dk, wd.end_ms + 3_000)
    assert dk.window.slug == "bitcoin-up-or-down-on-october-21-2026" and dk.final_of(wd) == D("118001.00") and dk.line_of(dk.window) == D("118001.00")
    assert dk.odds(wd.end_ms + 3_000, wd).fair_up == 1.0

    # --- the bot: targets, cards, the section, the paper trader's block ------------------------------------------------------
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off", "WEB_PORT": "8080"})
    bstore = m.Store(":memory:")
    bot = m.Bot(cfg, bstore, FM(NOW), None)
    bot.spot.get = feed.get
    assert list(bot.series) == ["BTC-5M", "BTC-1H", "BTC-1D"] and "BTC 涨跌" in [n for n, _ in bot.reference_jobs()]
    targets = bot.predict_targets(NOW)
    assert {k: targets[k] for k in bot.series} == {"BTC-5M": w5.slug, "BTC-1H": w1.slug, "BTC-1D": wd.slug}
    assert {w5.slug, w1.slug, wd.slug} <= bot.predict.want_info and {w5.slug, w1.slug, wd.slug} <= bot.predict.quick
    later = bot.predict_targets(w5.end_ms + 1_000)  # the next 5-minute window: the old slug is forgotten, the new one looked up at once
    assert later["BTC-5M"] == f"btc-updown-5m-{w5.end_ms // 1000}" and w5.slug not in bot.predict.want_info and later["BTC-5M"] in bot.predict.quick
    bot.predict_targets(NOW)
    assert bot.edge_group(m.SimMarket(w5.slug, "x", "btc", "BTC-5M", 0.5, m.PredictBook("BTC-5M", w5.slug, "1", "t", (), (), NOW), 0.02, "", ("涨", "跌"), {})) == "btc"
    assert bot.edge_target(["BTC涨跌"]) == ("group:btc", "BTC 涨跌") and bot.edge_target(["比特币涨跌"]) == ("group:btc", "BTC 涨跌")
    assert bot.edge_target(["BTC"])[0] == "key:BTC"  # the BTC 先触 card keeps its name
    assert bot.edge_target(["BTC 1小时涨跌"]) == ("key:BTC-1H", "BTC 1小时涨跌") and ("BTC-5M", "BTC 5分钟涨跌") in bot.edge_markets()
    assert bot.watch_update(keys=["BTC-1H"]) == {"keys": ["BTC-1H"], "names": {"BTC-1H": "BTC 1小时涨跌"}, "tg_only": False, "tg": True}
    bot.watch_update(keys=[])
    result = await bot.refresh_series(NOW)
    assert result.status == "ok" and bot.series["BTC-5M"].line_of(w5) == D("118100.00")
    cards = {i["name"]: i for i in bot.odds_payload()["items"]}
    card = cards["BTC 5分钟涨跌"]
    assert card["group"] == "btc" and card["symbol"] == "BTC-5M" and card["ekey"] == "BTC-5M" and card["close_ms"] == w5.end_ms
    assert card["close_label"].startswith("23:40–23:45 ET（北京 11:40–11:45）；终点 5 分钟 K 的收盘价高于起点为涨") and card["series"] == {
        "cycle": "5m", "period": "5分钟", "slug": w5.slug, "window": "23:40–23:45 ET，北京 11:40–11:45"}
    assert card["ref"] == "118,100.00" and card["ref_rel"] == "起点" and card["effective"] == "118,500.00" and card["unit"] == "USDT" and not card["warn"]
    assert card["ref_note"] == "币安 BTCUSDT 10-19 23:35 ET（北京 10-20 11:35）这根 5 分钟 K 线的收盘价" and "Chainlink" in card["proxy_note"]
    assert abs(card["sigma"] - bot.series["BTC-5M"].sigma * math.sqrt(card["remaining"] / 365)) < 1e-12 and card["fair_up"] == bot.series["BTC-5M"].odds(NOW).fair_up
    assert card["predict"]["url"] == m.PREDICT_SITE + w5.slug + "?ref=B00EA" and "edges" not in card["predict"]
    assert cards["BTC 1小时涨跌"]["group"] == "btc" and cards["BTC 1天涨跌"]["close_ms"] == wd.end_ms and cards["BTC 1天涨跌"]["ref"] == "117,234.56"
    assert cards["BTC 1小时涨跌"]["close_label"].startswith("10-19 23:00–00:00 ET（北京 10-20 11:00–12:00）；这根 1 小时 K 的收盘价不低于开盘价为涨")
    json.dumps(cards["BTC 5分钟涨跌"])
    # the book prices the first outcome: "Up" as it is, "Down" flipped; another window's book is never used for this one
    book = m.PredictBook("BTC-5M", w5.slug, "77", "Bitcoin Up or Down", ((D("0.55"), D("500")),), ((D("0.60"), D("400")),), NOW)
    bot.predict.books["BTC-5M"] = book
    bot.predict.info[w5.slug] = {"outcomes": ["Up", "Down"], "created_ms": 0}
    card = {i["name"]: i for i in bot.odds_payload()["items"]}["BTC 5分钟涨跌"]
    edges = card["predict"]["edges"]
    assert [e["label"] for e in edges] == ["挂涨", "挂跌", "吃涨", "吃跌"] and abs(edges[0]["edge"] - (card["fair_up"] - 0.55)) < 1e-9
    assert [e["label"] for e in edges if e["best"]] == ["挂涨"] and edges[2]["edge"] > 0.3, edges  # a 55¢ bid and a 60¢ ask against a fair ~100¢
    bot.predict.info[w5.slug]["outcomes"] = ["Down", "Up"]  # the same book now prices Down
    card = {i["name"]: i for i in bot.odds_payload()["items"]}["BTC 5分钟涨跌"]
    assert abs(card["predict"]["edges"][0]["edge"] - (card["fair_up"] - 0.40)) < 1e-9
    bot.predict.info[w5.slug]["outcomes"] = ["Up", "Down"]
    bot.predict.books["BTC-5M"] = m.PredictBook("BTC-5M", "btc-updown-5m-1792467300", "76", "old", book.bids, book.asks, NOW)
    card = {i["name"]: i for i in bot.odds_payload()["items"]}["BTC 5分钟涨跌"]
    assert "edges" not in card["predict"] and not card["predict"]["error"] and not bot.sim_markets(NOW)
    bot.predict.books["BTC-5M"] = book
    # Predict's own start price, when its category shows one, must match the line (0.02% for Chainlink's mid against Binance's trade price)
    bot.predict.strikes[w5.slug] = (D("118110.00"), 0.0)
    card = {i["name"]: i for i in bot.odds_payload()["items"]}["BTC 5分钟涨跌"]
    assert card["warn"] == "" and card["ref_note"].endswith("；与 Predict 起点价一致")
    bot.predict.strikes[w5.slug] = (D("118200.00"), 0.0)
    card = {i["name"]: i for i in bot.odds_payload()["items"]}["BTC 5分钟涨跌"]
    assert card["warn"] == "Predict 起点价 118,200.00 与币安起点 118,100.00 不一致，请核实；暂不给建议" and card["predict"]["hold"] == card["warn"]
    bot.predict.strikes.pop(w5.slug)
    # the paper trader's market: one per window, the window named in the item, the 5-minute one without Telegram alerts
    (sim,) = bot.sim_markets(NOW)
    assert (sim.market, sim.item, sim.kind, sim.key, sim.sides) == (w5.slug, "BTC 5分钟涨跌 10-20 11:40", "btc", "BTC-5M", ("涨", "跌"))
    assert sim.settle == {"end": w5.end_ms, "start": w5.start_ms, "cycle": "5m", "tie": "split", "line": 118100.0} and sim.alerts is False
    assert sim.fair_up == card["fair_up"] and dict(sim.evidence)["basis"]["slug"] == w5.slug and dict(sim.evidence)["sources"][1]["what"] == "起点"
    assert dict(sim.evidence)["sources"][1]["quoted_ms"] == w5.line_at and dict(sim.evidence)["sources"][1]["source"] == "币安 5 分钟 K"
    hbook = m.PredictBook("BTC-1H", w1.slug, "78", "Bitcoin Up or Down", ((D("0.50"), D("500")),), ((D("0.52"), D("400")),), NOW)
    bot.predict.books["BTC-1H"] = hbook
    bot.predict.info[w1.slug] = {"outcomes": ["Up", "Down"], "created_ms": 0}
    sims = {mk.key: mk for mk in bot.sim_markets(NOW)}
    assert sims["BTC-1H"].alerts is True and sims["BTC-1H"].settle["tie"] == "up" and sims["BTC-1H"].item == "BTC 1小时涨跌 10-20 11:00"
    allc = m.Bot(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "BTC_UPDOWN_ALERTS": "all"}), bstore, FM(NOW), None)
    allc.spot, allc.series = bot.spot, bot.series
    allc.predict.books, allc.predict.info = bot.predict.books, bot.predict.info
    assert {mk.key: mk.alerts for mk in allc.sim_markets(NOW)} == {"BTC-5M": True, "BTC-1H": True}
    # the Telegram edge alerts leave the 5-minute market alone (a 40¢ edge there) and watch the hourly one (fair ~93¢ against a 50¢ bid)
    bot.edge_ran = -1e9
    await bot.edge_alerts(NOW)
    state = bstore.get("edgealerts", {})
    assert w5.slug not in state and state[w1.slug]["pending"]["want"] == "appear:up", state
    allc.edge_ran = -1e9
    await allc.edge_alerts(NOW)
    assert bstore.get("edgealerts", {})[w5.slug]["pending"]["want"] == "appear:up"
    bstore.put("edgealerts", {})
    # --- a paper trade: opened on the 5-minute window, settled on its candle, confirmed by Predict, reported apart -----------
    bot.predict.books.pop("BTC-1H")  # only the 5-minute market offers a book now
    bot.sim_ran = -1e9
    details = []

    async def market_details(mid):
        details.append(mid)
        return {"outcomes": ["Up", "Down"], "status": "REGISTERED", "resolved": None, "created_ms": 0, "fee_bps": None}
    bot.predict.market_details = market_details
    await bot.sim_step(NOW)
    trades = bot.sim_trades()
    tid = f"{w5.slug}|up|吃"
    assert list(trades) == [tid], trades
    t = trades[tid]
    assert (t["kind"], t["key"], t["item"], t["market"], t["slug"], t["status"]) == ("btc", "BTC-5M", "BTC 5分钟涨跌 10-20 11:40", w5.slug, w5.slug, "filled")
    assert t["driver"] == f"BTCUSDT@5m:{w5.start_ms}" and t["driver_name"] == "BTC 5分钟涨跌 10-20 11:40" and t["settle"] == sim.settle
    assert t["price"] > 0.60 and t["shares"] == 100 and t["entry"]["basis"]["line"] == 118100.0 and t["entry"]["sources"][0]["source"] == "币安现货"
    assert bot.sim_wait(t, sim, NOW) == "等窗口结束 10-20 11:45，按币安 K 线结算（结束后几秒内），再等 Predict 确认"
    assert bot.sim_result(t, NOW) is None and not bot.series["BTC-5M"].pending  # still running: nothing to settle yet, nothing to ask for
    main, apart = bot.sim_report(), bot.sim_report(kinds=bot.SIM_APART)
    assert main["total"]["trades"] == 0 and main["rows"] == [] and apart["total"]["open"] == 1 and apart["rows"][0]["id"] == tid
    assert apart["scope"] == "BTC 5分钟 / 1小时 / 1天涨跌" and apart["note"] == "" and apart["intro"].startswith("BTC 涨跌（5 分钟 / 1 小时 / 1 天）的模拟交易单独记在这里")
    assert [k["name"] for k in apart["kinds"]] == ["BTC 5分钟"] and apart["groups"][0]["name"] == "BTC 5分钟涨跌 10-20 11:40" and apart["groups"][0]["event"] == "BTC 5分钟涨跌 10-20 11:40 收跌"
    items = bot.odds_payload()["items"]
    assert [i["name"] for i in items[-2:]] == ["模拟交易", "模拟交易 · BTC 涨跌"] and items[-1]["symbol"] == "SIM-BTC" and items[-1]["group"] == "sim"
    assert items[-1]["sim"]["rows"][0]["id"] == tid and items[-2]["sim"]["total"]["trades"] == 0
    text = bot.cmd_sim(None).text
    assert "BTC 涨跌（5 分钟 / 1 小时 / 1 天）" in text and "：单独统计，不计入上面的数字" in text and "BTC 5分钟涨跌 10-20 11:40 吃涨 60.8¢×100 → 持仓" in text, text
    assert text.index("还没有触发过") < text.index("🪙") and "持仓 1 笔" in text.split("🪙")[1]
    # the window ends: the candle settles it (跌), Predict confirms
    feed.candles[("5m", w5.start_ms)] = ("118100.00", "118050.00")
    await bot.refresh_series(w5.end_ms + 3_000)
    bot.sim_ran = -1e9
    bot.market.now = w5.end_ms + 3_000
    await bot.sim_step(w5.end_ms + 3_000)
    t = bot.sim_trades()[tid]
    assert t["status"] == "settled" and t["payout"] == 0.0 and t["confirm"] == "local" and t["local"]["up"] == 0.0
    assert t["note"] == "终点 118,050.00，起点 118,100.00" and t["local"]["evidence"]["rule"] == "终点收盘价高于起点为涨，低于为跌，相同50/50"
    assert t["local"]["evidence"]["source"] == "币安 5 分钟 K" and details == ["77"]
    bot.predict.market_details = lambda mid: _resolved()

    async def _resolved():
        return {"outcomes": ["Up", "Down"], "status": "RESOLVED", "created_ms": 0, "fee_bps": None,
                "resolved": {"index": 1, "name": "Down", "split": False, "how": "outcomes.status"}}
    bot.sim_checked.clear()
    bot.sim_ran = -1e9
    await bot.sim_step(w5.end_ms + 4_000)
    t = bot.sim_trades()[tid]
    assert t["confirm"] == "confirmed" and t["final"]["name"] == "Down"
    apart = bot.sim_report(kinds=bot.SIM_APART)
    assert apart["total"]["settled"] == 1 and apart["total"]["losses"] == 1 and apart["kinds"] == [{"name": "BTC 5分钟", **m.sim_stats([t])}]
    assert bot.sim_report()["total"]["settled"] == 0 and bot.sim_recent_stats(bot.SIM_APART)["今天"]["settled"] == 1 and bot.sim_recent_stats()["今天"]["settled"] == 0
    text = bot.cmd_sim(None).text
    assert "已结算 1 笔：赢 0｜输 1｜平 0（已确认 1｜预结算 0｜结果不一致 0）" in text.split("🪙")[1] and "BTC 5分钟 1 笔" not in text  # one cycle: no breakdown line
    assert {x["id"]: x for x in bot.journal_payload()["trades"]}[tid]["driver_name"] == "BTC 5分钟涨跌 10-20 11:40"
    assert "BTC 涨跌" in bot.journal_csv()
    assert [x["key"] for x in bot.journal_payload()["kinds"]] == ["btc"]
    # a restart that never saw the window: the trade asks for its candles, the next refresh reads them and settles it
    fresh = m.Bot(cfg, bstore, FM(w5.end_ms + 60_000), None)
    fresh.spot.get = feed.get
    bstore.put(f"sim:{tid}", {**t, "status": "filled", "confirm": None, "payout": None, "local": None, "final": None})
    for key in bstore.keys("series:"):
        bstore.delete_keys([key])
    fresh.sim_cache = None
    pending_trade = fresh.sim_trades()[tid]
    assert fresh.sim_result(pending_trade, w5.end_ms + 60_000) is None and [w.slug for w in fresh.series["BTC-5M"].pending] == [w5.slug]
    await fresh.refresh_series(w5.end_ms + 60_000)
    assert fresh.sim_result(pending_trade, w5.end_ms + 60_000)[0] == 0.0 and fresh.series["BTC-5M"].pending == []
    # the hourly series' equal close pays 涨 in the paper trader too
    bstore.put(f"series:{w1.slug}", {"line": "118200.00", "final": "118200.00", "cycle": "1h", "start": w1.start_ms, "end": w1.end_ms})
    htrade = {"kind": "btc", "key": "BTC-1H", "market": w1.slug, "slug": w1.slug, "side": "up", "settle": {"end": w1.end_ms, "start": w1.start_ms, "cycle": "1h", "tie": "up"}}
    up, note, proof = fresh.sim_result(htrade, w1.end_ms + 5_000)
    assert up == 1.0 and note == "终点 118,200.00，起点 118,200.00" and proof["rule"].endswith("相同算涨")
    # --- switched off: no markets, no job, no targets, no cards, no block (unless records remain) --------------------------------
    off = m.Bot(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "BTC_UPDOWN": "off"}), m.Store(":memory:"), FM(NOW), None)
    assert off.series == {} and "BTC 涨跌" not in [n for n, _ in off.reference_jobs()] and not {"BTC-5M", "BTC-1H", "BTC-1D"} & set(off.predict_targets(NOW))
    names = [i["name"] for i in off.odds_payload()["items"]]
    assert "BTC 5分钟涨跌" not in names and "模拟交易 · BTC 涨跌" not in names and "🪙" not in off.cmd_sim(None).text
    kept = m.Bot(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "BTC_UPDOWN": "off"}), bstore, FM(NOW), None)  # records of it remain
    assert kept.series_sim_shown() and kept.sim_report(kinds=kept.SIM_APART)["note"] == "BTC 涨跌已关闭（BTC_UPDOWN=off），只显示已有记录"
    assert kept.sim_result(kept.sim_trades()[tid], NOW) is None  # Predict's own result settles what is left
    narrow = m.Bot(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SIM_MARKETS": "close"}), m.Store(":memory:"), FM(NOW), None)
    assert not narrow.series_sim_shown() and "模拟交易 · BTC 涨跌" not in [i["name"] for i in narrow.odds_payload()["items"]]
    narrow2 = m.Bot(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SIM_MARKETS": "close"}), bstore, FM(NOW), None)
    assert narrow2.sim_report(kinds=narrow2.SIM_APART)["note"] == "模拟交易范围不含 BTC 涨跌（SIM_MARKETS 加 btc 才开仓），只显示已有记录"
    assert narrow2.sim_withdraw_reason({"kind": "btc"}, None) == "模拟交易已改为只吃单"
    # --- the page: the section, hidden until ticked; the journal knows the kind --------------------------------------------------
    assert 'btc:"BTC 涨跌"' in m.WEB_PAGE and '<h2 id="h-btc">BTC 涨跌</h2>' in m.WEB_PAGE and '["sim","levels","macro","btc"]' in m.WEB_PAGE
    assert 'localStorage.getItem("btcDefault")' in m.WEB_PAGE and '"crypto","btc","levels"' in m.WEB_PAGE and 'btc:"BTC 涨跌"' in m.JOURNAL_PAGE
    assert "s.intro||" in m.WEB_PAGE and "if(s.note)" in m.WEB_PAGE


asyncio.run(run())
print("BTCSERIES_OK")
