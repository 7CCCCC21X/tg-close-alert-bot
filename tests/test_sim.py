"""Paper trading: whenever a card suggests a trade whose net edge reaches 10¢, 100 shares are bought on paper (the best
maker and the best taker apart, once per market and side); a resting order counts only once the book trades through
its price; positions settle on the market's real result; the record answers "does a 10¢ edge make money?"."""
import asyncio, os, sys, json, time, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
BJ = lambda mo, d, h, mi=0: int(dt.datetime(2026, mo, d, h, mi, tzinfo=m.BEIJING).timestamp() * 1000)
NOW = BJ(10, 5, 10, 0)
HSI_SLUG = "hang-seng-index-up-or-down-on-october-5-2026"
CLOSE = BJ(10, 5, 16, 10)

# --- settings ----------------------------------------------------------------------------------------------------------
c = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x"})
assert c.sim and c.sim_edge == 0.10 and c.sim_shares == 100
c = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SIM": "off", "SIM_EDGE_CENTS": "15", "SIM_SHARES": "250"})
assert not c.sim and abs(c.sim_edge - 0.15) < 1e-12 and c.sim_shares == 250
for bad in ({"SIM_EDGE_CENTS": "0"}, {"SIM_EDGE_CENTS": "60"}, {"SIM_SHARES": "0"}):
    try: m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", **bad}); assert False, bad
    except ValueError: pass

# --- the arithmetic -----------------------------------------------------------------------------------------------------
def book(bids, asks, at=NOW, slug=HSI_SLUG, key="HSI", fee=None):
    return m.PredictBook(key, slug, "1", "t", tuple((D(p), D(q)) for p, q in bids), tuple((D(p), D(q)) for p, q in asks), at, fee)
b = book([("0.55", "300"), ("0.50", "50")], [("0.58", "400")])
assert m.side_levels(b, "up") == [(0.58, 400.0)]
assert [(round(p, 9), q) for p, q in m.side_levels(b, "down")] == [(0.45, 300.0), (0.5, 50.0)]  # No = 1 − the Yes bids
assert m.fill_shares([(0.5, 30.0), (0.6, 100.0)], 100) == ((30 * 0.5 + 70 * 0.6) / 100, 100.0)
assert m.fill_shares([(0.5, 30.0)], 100) == (0.5, 30.0) and m.fill_shares([], 100) == (0.0, 0.0)  # a thin book fills what it has
assert m.sim_payout("up", 1.0) == 1.0 and m.sim_payout("down", 1.0) == 0.0 and m.sim_payout("down", 0.5) == 0.5


class FM:
    def __init__(self, now): self.now, self.config = now, None
    def now_ms(self): return self.now


async def step(bot, at):
    bot.market.now, bot.sim_ran = at, -1e9  # the trader's own pacing runs on the monotonic clock
    return await bot.sim_step(at)


async def run():
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off"})
    store = m.Store(":memory:")
    bot = m.Bot(cfg, store, FM(NOW), None)
    assert "模拟交易" in [name for name, _ in bot.reference_jobs()] and bot.cmd_sim(None).text.count("还没有触发过") == 1

    # --- a daily HSI market: fair 70¢, Yes bid 55¢ (挂涨 +15¢) and ask 58¢ (吃涨 +11.2¢ after the fee) ---------------
    settle = {"key": "HSI", "target": "2026-10-05", "line": 24600.0, "close_ms": CLOSE}
    hsi = m.SimMarket(HSI_SLUG, "恒生指数", "close", "HSI", 0.70, book([("0.55", "300")], [("0.58", "400")]), 0.03, "", ("涨", "跌"), settle)
    world = {"markets": [hsi]}
    bot.sim_markets = lambda now: world["markets"]
    await step(bot, NOW)
    trades = bot.sim_trades()
    assert sorted(trades) == [f"{HSI_SLUG}|up|吃", f"{HSI_SLUG}|up|挂"], sorted(trades)
    maker, taker = trades[f"{HSI_SLUG}|up|挂"], trades[f"{HSI_SLUG}|up|吃"]
    assert maker["status"] == "resting" and maker["price"] == 0.55 and maker["shares"] == 100 and maker["label"] == "挂涨" and maker["filled"] is None
    assert abs(maker["edge"] - 0.15) < 1e-9 and abs(maker["signal"] - 0.15) < 1e-9
    fee = 0.02 * 0.42  # Predict: 2% × min(p, 1 − p)
    assert taker["status"] == "filled" and abs(taker["price"] - (0.58 + fee)) < 1e-9 and taker["shares"] == 100 and taker["filled"] == NOW
    assert abs(taker["edge"] - (0.70 - 0.58 - fee)) < 1e-9 and taker["label"] == "吃涨"
    # the edge lasting for hours buys nothing more
    await step(bot, NOW + 60_000)
    assert len(bot.sim_trades()) == 2
    # the market moving toward the order without reaching it: still resting; trading through it: filled
    world["markets"] = [m.dataclasses.replace(hsi, book=book([("0.53", "300")], [("0.56", "100")], NOW + 120_000))]
    await step(bot, NOW + 120_000)
    assert bot.sim_trades()[f"{HSI_SLUG}|up|挂"]["status"] == "resting"
    world["markets"] = [m.dataclasses.replace(hsi, fair_up=0.62, book=book([("0.52", "300")], [("0.55", "100")], NOW + 180_000))]
    await step(bot, NOW + 180_000)
    filled = bot.sim_trades()[f"{HSI_SLUG}|up|挂"]
    assert filled["status"] == "filled" and filled["filled"] == NOW + 180_000 and filled["fill_fair"] == 0.62, filled
    # a stale book or a card that holds back: no new trade and no fill read from it
    world["markets"] = [m.SimMarket("other", "上证指数", "close", "SSE", 0.80, book([("0.50", "100")], [("0.52", "100")], NOW - 200_000, "other", "SSE"),
                                    0.02, "", ("涨", "跌"), {}),
                        m.SimMarket("held", "KOSPI", "close", "KOSPI", 0.80, book([("0.50", "100")], [("0.52", "100")], NOW + 180_000, "held", "KOSPI"),
                                    0.02, "期货锚点是近似值", ("涨", "跌"), {})]
    await step(bot, NOW + 200_000)
    assert len(bot.sim_trades()) == 2
    # a bar above the edge (model error 12¢): a 11¢ taker edge is not a suggestion, so nothing is bought
    world["markets"] = [m.dataclasses.replace(hsi, market="wide", need=0.12, book=book([], [("0.58", "400")], NOW + 200_000, "wide"))]
    await step(bot, NOW + 200_000)
    assert not [k for k in bot.sim_trades() if k.startswith("wide|")]

    # --- the 跌 side, a thin book, and a resting order that never fills --------------------------------------------------
    # fair 20¢: 挂跌 at 1 − 卖1 = 62¢ (+18¢), 吃跌 at 1 − 买1 = 65¢ for only 30 shares, then 1 − 0.30 = 70¢
    kospi_slug = "kospi-composite-index-up-or-down-on-october-5-2026"
    kospi = m.SimMarket(kospi_slug, "KOSPI", "close", "KOSPI", 0.20, book([("0.35", "30"), ("0.30", "500")], [("0.38", "100")], NOW + 240_000, kospi_slug, "KOSPI"),
                        0.02, "", ("涨", "跌"), {"key": "KOSPI", "target": "2026-10-05", "line": 3300.0, "close_ms": BJ(10, 5, 14, 30)})
    world["markets"] = [kospi]
    await step(bot, NOW + 240_000)
    t = bot.sim_trades()
    down_maker, down_taker = t[f"{kospi_slug}|down|挂"], t[f"{kospi_slug}|down|吃"]
    assert down_maker["label"] == "挂跌" and abs(down_maker["price"] - 0.62) < 1e-9 and down_maker["status"] == "resting"
    avg = (30 * 0.65 + 70 * 0.70) / 100
    assert down_taker["shares"] == 100 and abs(down_taker["price"] - (avg + 0.02 * min(avg, 1 - avg))) < 1e-9, down_taker

    # --- settlement on the official close, an hour after the close; a tie pays half --------------------------------------
    store.put("outcome:HSI:2026-10-05", 24650.0)
    world["markets"] = []
    await step(bot, CLOSE + 30 * 60_000)
    assert bot.sim_trades()[f"{HSI_SLUG}|up|挂"]["status"] == "filled"  # the official close is not final yet
    await step(bot, CLOSE + 61 * 60_000)
    t = bot.sim_trades()
    assert t[f"{HSI_SLUG}|up|挂"]["status"] == "settled" and t[f"{HSI_SLUG}|up|挂"]["payout"] == 1.0
    assert t[f"{HSI_SLUG}|up|挂"]["note"] == "10-05 收盘 24,650，对 24,600" and m.sim_status(t[f"{HSI_SLUG}|up|挂"]) == "赢 +$45.00"
    assert t[f"{HSI_SLUG}|up|吃"]["payout"] == 1.0
    store.put("outcome:KOSPI:2026-10-05", 3300.0)  # a tie: both sides paid ½
    await step(bot, CLOSE + 62 * 60_000)
    t = bot.sim_trades()
    assert t[f"{kospi_slug}|down|吃"]["status"] == "settled" and t[f"{kospi_slug}|down|吃"]["payout"] == 0.5
    assert t[f"{kospi_slug}|down|挂"]["status"] == "expired" and "挂单一直没成交" in t[f"{kospi_slug}|down|挂"]["note"]

    # --- the other kinds settle on the bot's own data ---------------------------------------------------------------------
    touch = bot.touches["BNB"]; touch.start_ms = BJ(9, 1, 0)
    trade = lambda kind, key, side, **settle: {"kind": kind, "key": key, "side": side, "market": f"x#{settle.pop('mid', '')}", "settle": settle}
    assert bot.sim_result(trade("touch", "BNB", "up", deadline=touch.spec.deadline_ms), NOW) is None
    store.put(f"touch:{touch.spec.slug}", {"kind": "high", "time": NOW - 3_600_000, "hi": 901, "lo": 880, "start": touch.start_ms})
    assert bot.sim_result(trade("touch", "BNB", "up", deadline=touch.spec.deadline_ms), NOW)[0] == 1.0
    store.put(f"touch:{touch.spec.slug}", {"kind": "low", "time": NOW - 3_600_000, "hi": 720, "lo": 699, "start": touch.start_ms})
    assert bot.sim_result(trade("touch", "BNB", "up", deadline=touch.spec.deadline_ms), NOW)[0] == 0.0
    deadline = touch.spec.deadline_ms
    store.put(f"touch:{touch.spec.slug}", {"kind": "clear", "through": deadline - 59 * 60_000, "start": touch.start_ms})
    assert bot.sim_result(trade("touch", "BNB", "up", deadline=deadline), deadline + 30 * 60_000) is None
    assert bot.sim_result(trade("touch", "BNB", "up", deadline=deadline), deadline + 61 * 60_000) == (0.5, "截止前两条线都没碰到，按 50/50")
    (oct_spec,) = m.UPDOWN_MARKETS
    assert bot.sim_result(trade("updown", oct_spec.key, "up"), NOW) is None
    store.put(f"updown:{oct_spec.slug}:start", {"open": oct_spec.start_ms, "close": "117234.56"})
    store.put(f"updown:{oct_spec.slug}:end", {"open": oct_spec.end_ms, "close": "110000"})
    assert bot.sim_result(trade("updown", oct_spec.key, "up"), oct_spec.end_ms + 120_000)[0] == 0.0
    cap = bot.caps["NIULAI"]
    cap.price, cap.supply = D("0.1"), D("1000000000")  # $100M now
    assert bot.sim_result(trade("ladder", "NIULAI", "up", target="200000000", end=cap.spec.end_ms, mid="11"), NOW) is None
    store.put(f"cap:{cap.spec.slug}", {"high": 0.21, "at": NOW // 1000, "start": cap.spec.start_ms})  # $210M seen
    assert bot.sim_result(trade("ladder", "NIULAI", "up", target="200000000", end=cap.spec.end_ms, mid="11"), NOW)[0] == 1.0
    assert bot.sim_result(trade("ladder", "NIULAI", "up", target="300000000", end=cap.spec.end_ms, mid="12"), NOW) is None
    late = cap.spec.end_ms + 61 * 60_000
    assert bot.sim_result(trade("ladder", "NIULAI", "up", target="300000000", end=cap.spec.end_ms, mid="12"), late)[0] == 0.0
    # a contract's daily market: its exchange close is kept per day as it comes in
    sym = "UNITREEUSDT"
    ticker = cfg.tickers[sym]
    close_ms = int(dt.datetime(2026, 10, 5, 15, 0, tzinfo=m.BEIJING).timestamp() * 1000)
    bot.stocks.closes[sym] = m.Baseline(D("75.5"), "k", "label", 0, close_ms)
    bot.sim_note_closes()
    assert store.get(f"outcome:{sym}:2026-10-05") == 75.5 and ticker.market in m.STOCK_MARKETS

    # --- the record: totals, the model's own expectation, by kind and by maker / taker ------------------------------------
    r = bot.sim_report()
    tot = r["total"]
    # HSI maker +45.00, HSI taker 100 × (1 − 0.5884), KOSPI taker 100 × (0.5 − cost); the KOSPI maker never filled
    k_cost = avg + 0.02 * min(avg, 1 - avg)
    want = 45 + 100 * (1 - (0.58 + fee)) + 100 * (0.5 - k_cost)
    assert tot["settled"] == 3 and tot["wins"] == 2 and tot["losses"] == 0 and tot["ties"] == 1 and tot["expired"] == 1, tot
    assert abs(tot["pnl"] - want) < 1e-6 and abs(tot["cost"] - (55 + 100 * (0.58 + fee) + 100 * k_cost)) < 1e-6, tot
    assert abs(tot["expected"] - 100 * (0.15 + (0.70 - 0.58 - fee) + (0.80 - k_cost))) < 1e-6 and abs(tot["roi"] - want / tot["cost"]) < 1e-12
    assert [g["name"] for g in r["modes"]] == ["挂单", "吃单"] and r["modes"][0]["settled"] == 1 and r["modes"][0]["expired"] == 1
    assert [g["name"] for g in r["kinds"]] == ["指数/个股日涨跌"] and r["rows"][0]["item"] == "KOSPI" and r["rows"][-1]["item"] == "恒生指数"
    assert all(row["url"].startswith(m.PREDICT_SITE) for row in r["rows"])
    json.dumps(r)
    text = bot.cmd_sim(None).text
    assert "已结算 3 笔：赢 2｜输 0｜平 1" in text and "未成交作废 1 笔" in text and "模型预期" in text and "挂单 1 笔 +$45.00" in text, text
    assert "10-05 10:00 恒生指数 挂涨 55.0¢×100（净优势 +15.0¢）→ 赢 +$45.00" in text, text
    assert "已关闭" in m.Bot(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SIM": "off"}), m.Store(":memory:"), FM(NOW), None).cmd_sim(None).text
    off = m.Bot(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SIM": "off"}), m.Store(":memory:"), FM(NOW), None)
    assert "模拟交易" not in [name for name, _ in off.reference_jobs()] and not any(i["name"] == "模拟交易" for i in off.odds_payload()["items"])

    # --- the real cards feed it: the BTC 10月涨跌 card frames 挂涨 at 40¢ against a fair ~54¢ -------------------------------
    real = m.Bot(cfg, m.Store(":memory:"), FM(BJ(10, 1, 16, 15)), None)
    up = real.updowns[oct_spec.key]
    real.store.put(f"updown:{oct_spec.slug}:start", {"open": oct_spec.start_ms, "close": "117234.56"})
    at = real.market.now
    up.price, up.priced_ms, up.sigma, up.sigma_ms = D("118500"), at, 0.28, at
    real.predict.books[oct_spec.key] = m.PredictBook(oct_spec.key, oct_spec.slug, "77", "t", ((D("0.40"), D("500")),), ((D("0.45"), D("500")),), at)
    real.predict.info[oct_spec.slug] = {"outcomes": ["Up", "Down"], "created_ms": 0}
    card = next(i for i in real.odds_payload()["items"] if i["name"] == "BTC 10月涨跌")
    page_best = next(e for e in card["predict"]["edges"] if e["best"])
    assert page_best["label"] == "挂涨" and page_best["edge"] >= 0.10, card["predict"]["edges"]
    mk = next(x for x in real.sim_markets(at) if x.kind == "updown")
    assert mk.fair_up == card["fair_up"] and mk.need == card["predict"]["need"] and not mk.hold
    await step(real, at)
    assert [(t["label"], t["price"], t["status"]) for t in real.sim_trades().values()] == [("挂涨", 0.40, "resting")]
    sim_card = next(i for i in real.odds_payload()["items"] if i["name"] == "模拟交易")
    assert sim_card["group"] == "sim" and sim_card["sim"]["total"]["resting"] == 1

    await browser_check(bot)
    print("SIM_OK")


async def browser_check(bot):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        async_playwright = None
    chrome = next((p for p in [os.environ.get("CHROMIUM_PATH", ""), "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"]
                   if p and os.path.exists(p)), "")
    if async_playwright is None or not (chrome or os.environ.get("PLAYWRIGHT_BROWSERS_PATH")):
        print("browser check skipped (no Playwright/Chromium)")
        return
    web = m.WebServer(bot, 0, "t" * 20); port = await web.start()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**({"executable_path": chrome} if chrome else {}))
        page = await browser.new_page(viewport={"width": 390, "height": 900})
        await page.goto(f"http://127.0.0.1:{port}/p/{'t' * 20}")
        await page.wait_for_selector("#g-sim .card")
        assert await page.inner_text("#h-sim") == "模拟交易"
        text = await page.inner_text("#g-sim .card")
        pnl = bot.sim_report()["total"]["pnl"]
        assert "已结算盈亏" in text and f"+${pnl:.2f}" in text and "已结算 3 笔：赢 2 · 输 0 · 平 1" in text, text
        assert "未成交作废 1 笔" in text and "模型预期" in text, text
        await page.click("#g-sim details summary")
        rows = await page.locator("#g-sim a.simrow").count()
        assert rows == len(bot.sim_report()["rows"]) and "赢 +$45.00" in await page.inner_text("#g-sim .simrows")
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "no sideways scroll"
        if os.environ.get("WEB_SCREENSHOT"):
            await page.locator("#g-sim .card").screenshot(path=os.environ["WEB_SCREENSHOT"])
        await browser.close()
    await web.stop()


asyncio.run(run())
