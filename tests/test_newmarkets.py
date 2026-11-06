"""The markets added in 1.43.0: HYPE 反超 SOL by December 2026 (the window opens at the market's creation, read from Predict)
and BTC 先触 $70k / $90k before May 6 2027 (a fixed window from the rules). The suites written for the earlier HYPE 反超 SOL
and BTC 先触 see the old set (tests/archived_markets.py); this one runs on the lists main.py has."""
import asyncio, sys, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import archived_markets
import main as m
D = m.D
H = m.HOUR_MS
utc = lambda *a: int(dt.datetime(*a, tzinfo=dt.timezone.utc).timestamp() * 1000)

# this process runs on main's own touch / flip lists (the other kinds stay as offline.py installed them)
m.TOUCH_MARKETS, m.FLIP_MARKETS = archived_markets.LIVE_TOUCH, archived_markets.LIVE_FLIP
assert [s.key for s in m.TOUCH_MARKETS] == ["SOL", "BTC-2027", "ETH"], [s.key for s in m.TOUCH_MARKETS]
(FLIP,) = m.FLIP_MARKETS
BTC = next(s for s in m.TOUCH_MARKETS if s.key == "BTC-2027")

# --- BTC 先触 $70k / $90k: "between November 5th 2026 at 9:00 AM ET and May 5th, 2027 at 11:59 PM ET" ------------------
assert BTC.slug == "will-btc-hit-70000-or-90000-first-before-may-6-2027" and BTC.symbol == "BTCUSDT"
assert (BTC.low, BTC.high) == (D("70000"), D("90000")) and BTC.fixed_start
assert BTC.created_ms == utc(2026, 11, 5, 14, 0)  # 09:00 EST (UTC−5: daylight time ended on Nov 1)
assert BTC.deadline_ms == utc(2027, 5, 6, 3, 59)  # 23:59 EDT (UTC−4: daylight time again from Mar 14 2027)
assert BTC.close_label() == "05-05 23:59 ET（北京 05-06 11:59）截止；都没碰到按 50/50 结算", BTC.close_label()
assert BTC.label(BTC.low) == "70k" and BTC.label(BTC.high) == "90k"
assert m.touch_outcome("$70,000", BTC) == "low" and m.touch_outcome("$90k", BTC) == "high"
assert m.TouchMarket(m.Store(":memory:"), BTC).start_ms == BTC.created_ms
# its own key: the earlier BTC 先触's unconfirmed paper trades look their card up by "BTC"; one risk group with every BTCUSDT market
assert BTC.key != archived_markets.TOUCH_BTC[0].key
assert m.market_driver("touch", "BTC-2027") == ("BTCUSDT", "BTC") == m.market_driver("range", "BTC-HIT-2026")

# --- HYPE 反超 SOL by December: "between market creation and November 30, 2026, 11:59 PM ET" ----------------------------
assert FLIP.slug == "will-hype-flip-sol-by-december-2026" and (FLIP.coin, FLIP.other) == ("HYPE", "SOL")
assert FLIP.key == "HYPE-SOL-12" and FLIP.key != archived_markets.FLIP_MARKETS[0].key and FLIP.name == "HYPE 反超 SOL"
assert FLIP.start_ms == 0 and FLIP.end_ms == utc(2026, 12, 1, 4, 59)  # 23:59 EST
assert FLIP.window() == "市场创建 – 11-30 23:59 ET（北京 12-01 12:59）", FLIP.window()
assert m.market_driver("flip", FLIP.key) == ("HYPE/SOL", "HYPE/SOL")

CREATED = utc(2026, 11, 5, 15, 30) + 12_345  # Predict's createdAt: 10:30:12.345 ET
START = utc(2026, 11, 5, 15, 31)             # the first whole minute after it
NOW = utc(2026, 11, 6, 9, 20)


class World:
    """Hyperliquid: mids and candles. HYPE flat at 30 against SOL at 160, except where a test puts a minute above it."""
    def __init__(self, now):
        self.now, self.minutes, self.calls = now, {}, []

    async def info(self, payload):
        self.calls.append(payload)
        if payload["type"] == "allMids":
            return {"HYPE": "30", "SOL": "160"}
        req = payload["req"]
        step = H if req["interval"] == "1h" else 60_000
        rows = []
        for t in range(-(-req["startTime"] // step) * step, min(req["endTime"], self.now) + 1, step):
            if req["coin"] == "SOL":
                c = h = 160.0
            elif req["interval"] == "1h":
                inside = [v for (k, v) in self.minutes.items() if t <= k < t + H]
                c, h = 30.0 * (1.004 if (t // H) % 2 else 1), max([30.0 * 1.01, *inside])
            else:
                c = h = self.minutes.get(t, 30.0)
            rows.append({"t": t, "T": t + step - 1, "s": req["coin"], "i": req["interval"], "o": str(c), "h": str(h), "l": str(c), "c": str(c)})
        return rows


async def run():
    store = m.Store(":memory:")
    fm = m.FlipMarket(store, FLIP)
    world = World(NOW)
    fm.info = world.info

    async def refresh(at):
        world.now = at
        fm.times.update(price=-1e9, vol=-1e9, scan=-1e9)
        await fm.refresh(at)

    # --- no creation time yet: prices and σ are read, the path is not (its start is unknown), no odds, no advice ----------
    await refresh(NOW)
    assert fm.ratio == 30 / 160 and fm.sigma and not fm.error, (fm.ratio, fm.sigma, fm.error)
    assert not [p for p in world.calls if p["type"] == "candleSnapshot" and p["req"]["interval"] == "1m"]
    assert fm.history == {} and fm.odds(NOW) == "等待 Predict 的市场创建时间（窗口从创建起算）"
    assert fm.advice_problem(NOW) == "等待 Predict 的市场创建时间；暂不给建议"
    assert fm.status(NOW) == "等待 Predict 的市场创建时间（窗口从创建后的第一个整分钟起）"
    fm.learn_created(0)
    assert fm.spec.start_ms == 0

    # --- Predict's createdAt: the window opens at the next whole minute; a flip in the creation minute itself does not count
    world.minutes[START - 60_000] = 170.0  # the minute 10:30 ET (opened before the market existed) closes HYPE > SOL
    fm.learn_created(CREATED)
    assert fm.spec.start_ms == START and fm.base.start_ms == 0 and fm.spec.end_ms == FLIP.end_ms
    assert fm.spec.window() == "11-05 10:31 – 11-30 23:59 ET（北京 11-05 23:31 – 12-01 12:59）", fm.spec.window()
    assert store.get(f"flip:{FLIP.slug}:created") == CREATED
    await refresh(NOW)
    hist = fm.history
    assert hist.get("kind") == "clear" and hist["start"] == START and hist["through"] >= NOW - H, hist
    odds = fm.odds(NOW)
    years = (FLIP.end_ms + 60_000 - NOW) / m.YEAR_MS
    assert isinstance(odds, float) and abs(odds - m.hit_probability(30 / 160, 1.0, fm.sigma, years)) < 1e-12
    assert fm.advice_problem(NOW) == "" and fm.status(NOW).startswith("窗口开始以来未反超")

    # --- a restart prices at once from the creation time kept in SQLite; a later minute that closes above SOL is Yes ------
    again = m.FlipMarket(store, FLIP)
    assert again.spec.start_ms == START and again.history == hist
    world.minutes[NOW + 30 * 60_000] = 161.0
    again.info = world.info
    world.now = NOW + H
    again.times.update(price=-1e9, vol=-1e9, scan=-1e9)
    await again.refresh(NOW + H)
    assert again.history.get("kind") == "flip" and again.history["time"] == NOW + 30 * 60_000 and again.odds(NOW + H) == 1.0, again.history

    # --- the bot: both cards in 加密, the flip learns its start from Predict's market info ---------------------------------
    class FM:
        def __init__(self): self.config = None
        def now_ms(self): return NOW
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off"})
    bot = m.Bot(cfg, m.Store(":memory:"), FM(), None)
    assert set(bot.touches) == {"SOL", "BTC-2027", "ETH"} and set(bot.flips) == {"HYPE-SOL-12"}
    targets = bot.predict_targets(NOW)
    assert targets["BTC-2027"] == BTC.slug and targets["HYPE-SOL-12"] == FLIP.slug
    assert {BTC.slug, FLIP.slug} <= bot.predict.want_info
    bfm = bot.flips["HYPE-SOL-12"]
    bfm.info = World(NOW).info
    bot.predict.info[FLIP.slug] = {"outcomes": ["Yes", "No"], "created_ms": CREATED}
    await bot.refresh_flip(NOW)
    assert bfm.spec.start_ms == START and bfm.history.get("kind") == "clear", (bfm.spec.start_ms, bfm.history, bfm.error)
    names = [i["name"] for i in bot.odds_payload()["items"]]
    assert "BTC 先触 70k/90k" in names and "HYPE 反超 SOL" in names, names
    card = next(i for i in bot.odds_payload()["items"] if i["name"] == "HYPE 反超 SOL")
    assert card["flip"]["window"] == "11-05 10:31 – 11-30 23:59 ET（北京 11-05 23:31 – 12-01 12:59）", card["flip"]["window"]
    assert card["close_label"].startswith("11-05 10:31 – 11-30 23:59 ET")
    print("NEWMARKETS_OK")


asyncio.run(run())
