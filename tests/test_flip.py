"""Will HYPE flip SOL by Nov 2026: Yes once any Hyperliquid 1-minute candle between Oct 2 04:00 ET and Oct 31 23:59 ET
closes with HYPE above SOL at the same timestamp. The path is checked on hourly bars, opening an hour's 1-minute candles
only when a flip was possible in it; the chance of a flip is the ratio's single-barrier touch probability."""
import asyncio, os, sys, math, json, time, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
(SPEC,) = m.FLIP_MARKETS
H = m.HOUR_MS
UTC = dt.timezone.utc
utc = lambda *a: int(dt.datetime(*a, tzinfo=UTC).timestamp() * 1000)

# --- the market as its rules state it (both ends EDT) -----------------------------------------------------------------
assert SPEC.slug == "will-hype-flip-sol-by-nov-26" and (SPEC.coin, SPEC.other) == ("HYPE", "SOL") and SPEC.key == "HYPE-SOL"
assert SPEC.start_ms == utc(2026, 10, 2, 8, 0) and SPEC.end_ms == utc(2026, 11, 1, 3, 59)
assert SPEC.window() == "10-02 04:00 – 10-31 23:59 ET（北京 10-02 16:00 – 11-01 11:59）", SPEC.window()
assert SPEC.key not in {s.key for s in (*m.TOUCH_MARKETS, *m.UPDOWN_MARKETS, *m.CAP_MARKETS)}
assert [m.short_price(D(x)) for x in ("90.2515", "121.805", "0.0123456", "3.5")] == ["90.25", "121.80", "0.01235", "3.5"]
assert m.short_price(None) == "—"

# --- σ of the ratio: HYPE alternating ±0.5% an hour against a flat SOL -> 0.5% × √8760; moving together -> 0 ---------
NOW = utc(2026, 10, 3, 10, 20)
bars = lambda coin, f: [{"t": NOW - (760 - i) * H, "c": str(f(i))} for i in range(761)]
hype = bars("HYPE", lambda i: 45 * (1.005 if i % 2 else 1))
sigma = m.ratio_vol(hype, bars("SOL", lambda i: 180), NOW)
assert abs(sigma - math.log(1.005) * math.sqrt(8760)) / sigma < 0.01, sigma
assert m.ratio_vol(hype, bars("SOL", lambda i: 180 * (1.005 if i % 2 else 1)), NOW) < 1e-9
try: m.ratio_vol(hype[:100], bars("SOL", lambda i: 180), NOW); assert False
except ValueError: pass


class World:
    """Hyperliquid's info endpoint: mids, and hourly / 1-minute candles by open time (the running one included)."""
    def __init__(self, now):
        self.now, self.mids, self.hours, self.minutes, self.calls, self.down = now, {"HYPE": "45", "SOL": "180"}, {}, {}, [], False

    def bar(self, coin, iv, t):
        if iv == "1h":
            if (coin, t) in self.hours:
                return self.hours[coin, t]
            c = 45 * (1.005 if (t // H) % 2 else 1) if coin == "HYPE" else 180
            return (c, c * 1.01, c * 0.99, c)
        c = self.minutes.get((coin, t), 45 if coin == "HYPE" else 180)
        return (c, c, c, c)

    async def info(self, payload):
        self.calls.append(payload)
        if self.down:
            raise m.RemoteError("api.hyperliquid.xyz: timed out")
        if payload["type"] == "allMids":
            return dict(self.mids, BTC="100000")
        req = payload["req"]
        step = H if req["interval"] == "1h" else 60_000
        first = -(-req["startTime"] // step) * step
        rows = []
        for t in range(first, min(req["endTime"], self.now) + 1, step):
            o, h, l, c = self.bar(req["coin"], req["interval"], t)
            rows.append({"t": t, "T": t + step - 1, "s": req["coin"], "i": req["interval"], "o": str(o), "h": str(h), "l": str(l), "c": str(c)})
        return rows

    def minute_calls(self):
        return [p for p in self.calls if p["type"] == "candleSnapshot" and p["req"]["interval"] == "1m"]


async def run():
    store = m.Store(":memory:")
    fm = m.FlipMarket(store, SPEC)
    world = World(SPEC.start_ms - 3 * H)
    fm.info = world.info

    async def refresh(at):  # the market's own timers run on the monotonic clock: make each part due
        world.now = at
        fm.times.update(price=-1e9, vol=-1e9, scan=-1e9)
        await fm.refresh(at)

    # --- before the window opens: priced from the full window, nothing to check yet ------------------------------------
    await refresh(SPEC.start_ms - 3 * H)
    assert fm.ratio == 0.25 and fm.sigma and not fm.error and fm.history == {}, (fm.ratio, fm.sigma, fm.error)
    years = (SPEC.end_ms + 60_000 - SPEC.start_ms) / m.YEAR_MS
    assert fm.odds(world.now) == m.hit_probability(0.25, 1.0, fm.sigma, years) and fm.advice_problem(world.now) == ""
    assert fm.status(world.now) == "窗口北京 10-02 16:00 开始"
    # --- far apart: the hourly bars rule out every hour, no 1-minute candle is read --------------------------------------
    await refresh(NOW)
    hist = fm.history
    assert hist["kind"] == "clear" and hist["through"] == utc(2026, 10, 3, 10) and not world.minute_calls(), (hist, world.minute_calls())
    assert abs(hist["top"] - 45 * 1.005 / 180) < 1e-12, hist
    assert fm.status(NOW).startswith("窗口开始以来未反超（核至 10-03 18:00）；窗口内最高比值 0.2512"), fm.status(NOW)
    p = fm.odds(NOW)
    assert 0 <= p < 1e-6 and fm.advice_problem(NOW) == "" and fm.model_swing(NOW) < 1e-6, p  # HYPE would have to quadruple
    # --- inputs gone stale --------------------------------------------------------------------------------------------------
    world.down = True
    await refresh(NOW + 6 * 60_000)
    assert fm.odds(NOW + 6 * 60_000).startswith("Hyperliquid 价格停在 10-03 18:20（6 分钟未更新：") and "timed out" in fm.error
    world.down = False
    # advice needs the path checked up to nearly now and a σ measured within a day
    assert fm.advice_problem(utc(2026, 10, 3, 13, 1)) == "反超核验停在 10-03 18:00；暂不给建议"
    fm.sigma_ms, kept = NOW - m.FlipMarket.SIGMA_STALE_MS - 1, fm.sigma_ms
    assert fm.advice_problem(NOW) == "波动率超过一天未更新；暂不给建议"
    fm.sigma_ms = kept
    assert m.FlipMarket(m.Store(":memory:"), SPEC).advice_problem(NOW) == "窗口开始以来是否反超尚未核验；暂不给建议"

    # --- close together: an hour where a flip was possible is opened minute by minute ------------------------------------
    world.mids = {"HYPE": "178", "SOL": "180"}
    h1 = utc(2026, 10, 3, 14)
    world.hours[("HYPE", h1)] = (175, 181, 174, 179)
    world.hours[("SOL", h1)] = (180, 182, 178.5, 180)
    world.minutes[("HYPE", h1 + 17 * 60_000)] = 179.9  # close, not above: no flip
    store.put(f"flip:{SPEC.slug}", {**fm.history, "through": utc(2026, 10, 3, 14)})
    t1 = h1 + 40 * 60_000
    await refresh(t1)
    assert fm.history["kind"] == "clear" and fm.history["through"] == utc(2026, 10, 3, 14), fm.history  # running hour: not done
    asked = world.minute_calls()
    assert {c["req"]["coin"] for c in asked} == {"HYPE", "SOL"} and asked[0]["req"]["startTime"] == h1, asked
    assert 0.5 < fm.odds(t1) < 1, fm.odds(t1)  # 1.1% away with weeks to go
    # the flip itself: one minute closes with HYPE above SOL (a mid above SOL without a closed minute is not enough)
    world.minutes[("HYPE", h1 + 50 * 60_000)] = 180.25
    world.minutes[("SOL", h1 + 50 * 60_000)] = 180.2
    await refresh(h1 + 50 * 60_000 + 30_000)  # that minute has not closed yet
    assert fm.history["kind"] == "clear"
    await refresh(h1 + 51 * 60_000 + 5_000)
    hist = fm.history
    assert hist["kind"] == "flip" and hist["time"] == h1 + 50 * 60_000 and (hist["a"], hist["b"]) == (180.25, 180.2), hist
    assert fm.odds(h1 + 52 * 60_000) == 1.0 and fm.model_swing(h1 + 52 * 60_000) == 0.0
    assert fm.status(0) == "已于 10-03 22:50 这一分钟反超：HYPE 180.25 > SOL 180.2", fm.status(0)
    n = len(world.calls)
    await refresh(h1 + 70 * 60_000)
    assert not [c for c in world.calls[n:] if c["type"] == "candleSnapshot" and c["req"]["interval"] == "1m"]  # decided: no more checking

    # --- the card, the book (oriented by outcome name), and the paper trader --------------------------------------------
    class FM:
        def __init__(self, now): self.now, self.config = now, None
        def now_ms(self): return self.now
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off"})
    fresh = m.Store(":memory:")
    bot = m.Bot(cfg, fresh, FM(NOW), None)
    live = bot.flips[SPEC.key]
    live.info = World(NOW).info
    live.prices, live.priced_ms, live.sigma, live.sigma_ms = {"HYPE": D("162"), "SOL": D("180")}, NOW, 0.9, NOW
    fresh.put(f"flip:{SPEC.slug}", {"kind": "clear", "through": utc(2026, 10, 3, 10), "start": SPEC.start_ms, "top": 0.9, "top_at": utc(2026, 10, 3, 9)})
    assert bot.predict_targets(NOW)[SPEC.key] == SPEC.slug and SPEC.slug in bot.predict.want_info
    assert "反超市场" in [name for name, _ in bot.reference_jobs()]
    card = next(i for i in bot.odds_payload()["items"] if i["name"] == "HYPE 反超 SOL")
    want = m.hit_probability(0.9, 1.0, 0.9, (SPEC.end_ms + 60_000 - NOW) / m.YEAR_MS)
    assert card["group"] == "crypto" and card["symbol"] == "HYPE-SOL" and card["labels"] == ["Yes", "No"] and abs(card["fair_up"] - want) < 1e-12
    f = card["flip"]
    assert f["a"] == "162.00" and f["b"] == "180.00" and abs(f["ratio"] - 0.9) < 1e-12 and abs(f["gap"] - (1 / 0.9 - 1)) < 1e-12 and not f["hold"], f
    assert card["close_ms"] == SPEC.end_ms + 60_000 and card["close_label"].endswith("任一 1 分钟 K 收盘 HYPE > SOL 即 Yes")
    bot.predict.books[SPEC.key] = m.PredictBook(SPEC.key, SPEC.slug, "9", "t", ((D("0.40"), D("500")),), ((D("0.44"), D("500")),), NOW)
    bot.predict.info[SPEC.slug] = {"outcomes": ["Yes", "No"], "created_ms": 0}
    card = next(i for i in bot.odds_payload()["items"] if i["name"] == "HYPE 反超 SOL")
    edges = card["predict"]["edges"]
    assert [e["label"] for e in edges] == ["挂Yes", "挂No", "吃Yes", "吃No"] and abs(edges[0]["edge"] - (want - 0.40)) < 1e-9, edges
    assert card["predict"]["need"] == max(cfg.predict_min_edge, live.model_swing(NOW))
    bot.predict.info[SPEC.slug]["outcomes"] = ["No", "Yes"]
    card = next(i for i in bot.odds_payload()["items"] if i["name"] == "HYPE 反超 SOL")
    assert card["predict"]["bids"] == [[0.56, 500.0]] and card["predict"]["asks"] == [[0.6, 500.0]], card["predict"]
    bot.predict.info[SPEC.slug]["outcomes"] = ["HYPE", "SOL"]
    card = next(i for i in bot.odds_payload()["items"] if i["name"] == "HYPE 反超 SOL")
    assert "edges" not in card["predict"] and card["predict"]["error"].startswith("盘口方向未确认")
    bot.predict.info[SPEC.slug]["outcomes"] = ["Yes", "No"]
    json.dumps(card)
    # the paper trader sees the same market as the card
    mk = next(x for x in bot.sim_markets(NOW) if x.kind == "flip")
    assert abs(mk.fair_up - want) < 1e-12 and mk.sides == ("Yes", "No") and mk.settle == {"end": SPEC.end_ms} and not mk.hold
    trade = {"kind": "flip", "key": SPEC.key, "side": "up", "market": SPEC.slug, "settle": {"end": SPEC.end_ms}}
    assert bot.sim_result(trade, NOW) is None
    # our data says it flipped, the book still trades far below: flagged, never a "sure thing" edge
    fresh.put(f"flip:{SPEC.slug}", {"kind": "flip", "time": NOW - 60_000, "a": 181.0, "b": 180.0, "start": SPEC.start_ms, "top": 1.005, "top_at": NOW - 60_000})
    card = next(i for i in bot.odds_payload()["items"] if i["name"] == "HYPE 反超 SOL")
    assert card["fair_up"] == 1.0 and card["flip"]["hold"].startswith("数据显示已反超，但盘口仍低于 90¢") and not any(e["best"] for e in card["predict"]["edges"])
    assert bot.sim_result(trade, NOW)[0] == 1.0
    # no flip through the whole window: No, an hour after it ends
    fresh.put(f"flip:{SPEC.slug}", {"kind": "clear", "through": SPEC.end_ms + 60_000, "start": SPEC.start_ms, "top": 0.4, "top_at": NOW})
    assert bot.sim_result(trade, SPEC.end_ms + 30 * 60_000) is None and bot.sim_result(trade, SPEC.end_ms + 2 * H)[:2] == (0.0, "窗口内没有反超")
    assert live.odds(SPEC.end_ms + 2 * H) == 0.0
    # 加密 switched off: no card, no market, no job
    off = m.Bot(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "BNB_TOUCH": "off"}), m.Store(":memory:"), FM(NOW), None)
    assert SPEC.key not in off.predict_targets(NOW) and "反超市场" not in [name for name, _ in off.reference_jobs()]
    assert not any(i["name"] == "HYPE 反超 SOL" for i in off.odds_payload()["items"])
    await layout_check()
    print("FLIP_OK")


async def layout_check():
    """The card as it appeared live (HYPE 90.2515, SOL 121.805, an empty book) at phone and desktop widths: nothing sticks
    out of any card (the summary wraps instead), the prices are shortened, an empty book says so."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        async_playwright = None
    chrome = next((p for p in [os.environ.get("CHROMIUM_PATH", ""), "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"]
                   if p and os.path.exists(p)), "")
    if async_playwright is None or not (chrome or os.environ.get("PLAYWRIGHT_BROWSERS_PATH")):
        print("browser check skipped (no Playwright/Chromium)")
        return
    now = utc(2026, 10, 2, 13, 52)

    class FM:
        def __init__(self): self.config = None
        def now_ms(self): return now
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off"})
    store = m.Store(":memory:")
    bot = m.Bot(cfg, store, FM(), None)
    fm = bot.flips[SPEC.key]
    fm.prices, fm.priced_ms, fm.sigma, fm.sigma_ms = {"HYPE": D("90.2515"), "SOL": D("121.805")}, now, 0.52, now
    bot.predict.books[SPEC.key] = m.PredictBook(SPEC.key, SPEC.slug, "9", "t", (), (), now)
    bot.predict.info[SPEC.slug] = {"outcomes": ["Yes", "No"], "created_ms": 0}
    bot.predict.slugs[SPEC.key] = SPEC.slug
    touch = bot.touches["BNB"]
    touch.price, touch.priced_ms, touch.sigma, touch.sigma_ms = D("776.55"), now, 0.6, now
    web = m.WebServer(bot, 0, "t" * 20); web.CACHE_SECONDS = {}; port = await web.start()  # the tests change the payload and reload at once
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**({"executable_path": chrome} if chrome else {}))
        for width in (390, 620, 900, 1300):
            page = await browser.new_page(viewport={"width": width, "height": 900})
            await page.goto(f"http://127.0.0.1:{port}/p/{'t' * 20}")
            await page.wait_for_selector("#g-crypto .card")
            out = await page.evaluate("""() => [...document.querySelectorAll('.card')].flatMap(c => {
                const edge = c.getBoundingClientRect().right;
                return [...c.querySelectorAll('*')].filter(e => { const b = e.getBoundingClientRect(); return b.width && b.right > edge + 0.5; })
                  .map(e => (c.querySelector('.name') || {}).textContent + ': ' + e.className); })""")
            assert not out, (width, out)
            card = page.locator("#g-crypto .card", has_text="HYPE 反超 SOL")
            summary = await card.locator("summary").inner_text()
            assert "90.25" in summary and "121.80" in summary and "90.2515" not in summary and "+35.0%" in summary, summary
            assert await card.locator(".quote").inner_text() == "Predict ↗\n暂无挂单"
            await page.close()
        await browser.close()
    await web.stop()


asyncio.run(run())
