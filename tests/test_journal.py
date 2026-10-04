"""The paper trades' review page (/p/<token>/journal): totals with the expectation at the order and at the fills and how
results were confirmed, filters, one trade's whole record (why the model priced it, the prices behind it, proxy and
anchor, how it filled, how it was settled, version), deep links, and the CSV / JSON exports. The browser part needs
Playwright + Chromium and is skipped without them."""
import asyncio, csv, io, json, os, sys, urllib.parse, urllib.request, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP, except to 127.0.0.1)
import main as m
D = m.D
BJ = lambda mo, d, h, mi=0: int(dt.datetime(2026, mo, d, h, mi, tzinfo=m.BEIJING).timestamp() * 1000)
NOW = BJ(10, 5, 10, 0)
CLOSE = BJ(10, 5, 16, 10)
HSI_SLUG = "hang-seng-index-up-or-down-on-october-5-2026"
KOSPI_SLUG = "kospi-composite-index-up-or-down-on-october-5-2026"
TOKEN = "t" * 20


class FM:
    def __init__(self, now): self.now, self.config = now, None
    def now_ms(self): return self.now


def book(bids, asks, at, slug, key, mid):
    return m.PredictBook(key, slug, mid, "t", tuple((D(p), D(q)) for p, q in bids), tuple((D(p), D(q)) for p, q in asks), at, None)


EVIDENCE = {"basis": {"fair_up": 0.70, "ref": 24600.0, "ref_note": "10-02 收盘", "effective": 24702.5, "sigma_daily": 0.012,
                      "remaining": 0.5, "beta": 1.0, "mode": "盘后", "direct": False, "close_ms": CLOSE},
            "sources": [{"what": "恒指期货", "source": "etnet", "symbol": "恒指期货(10/2026)", "type": "期货最新价", "price": 24702.0,
                         "quoted_ms": NOW - 20_000, "fetched_ms": NOW - 5_000},
                        {"what": "参考收盘", "source": "tencent 日K", "symbol": "HSI", "type": "收盘", "price": 24600.0}],
            "proxy": {"proxy": "恒指期货", "contract": "10/2026", "anchor": 24500.0, "anchor_note": "16:10 现货收市时", "approx": False}}


async def build():
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off",
                             "SIM_WAYS": "both", "SIM_MARKETS": "all"})  # the review page is checked with maker trades too
    bot = m.Bot(cfg, m.Store(":memory:"), FM(NOW), None)
    world = {"markets": []}
    bot.sim_markets = lambda now: world["markets"]
    answers = {}

    async def details(mid):
        return answers.get(mid, {"outcomes": ["Up", "Down"], "status": "REGISTERED", "resolved": None})
    bot.predict.market_details = details

    async def step(at, *markets):
        bot.market.now, bot.sim_ran = at, -1e9
        world["markets"] = list(markets)
        bot.sim_checked.clear()
        await bot.sim_step(at)

    hsi = lambda fair, bids, asks, at: m.SimMarket(HSI_SLUG, "恒生指数", "close", "HSI", fair, book(bids, asks, at, HSI_SLUG, "HSI", "101"),
                                                   0.03, "", ("涨", "跌"), {"key": "HSI", "target": "2026-10-05", "line": 24600.0,
                                                                         "close_ms": CLOSE}, EVIDENCE)
    kospi = lambda bids, asks, at: m.SimMarket(KOSPI_SLUG, "KOSPI", "close", "KOSPI", 0.20, book(bids, asks, at, KOSPI_SLUG, "KOSPI", "102"),
                                               0.02, "", ("涨", "跌"), {"key": "KOSPI", "target": "2026-10-05", "line": 3300.0,
                                                                     "close_ms": BJ(10, 5, 14, 30)}, {"basis": {"fair_up": 0.2}})
    await step(NOW, hsi(0.70, [("0.55", "300")], [("0.58", "400")], NOW), kospi([("0.35", "30"), ("0.30", "500")], [("0.38", "100")], NOW))
    await step(NOW + 60_000, hsi(0.66, [("0.54", "10")], [("0.55", "30")], NOW + 60_000))
    await step(NOW + 120_000, hsi(0.60, [("0.52", "10")], [("0.53", "500")], NOW + 120_000))
    bot.note_outcome("HSI", "2026-10-05", 24650.0, "tencent 日K")
    bot.note_outcome("KOSPI", "2026-10-05", 3300.0, "Yahoo ^KS11 日K")
    await step(CLOSE + 61 * 60_000)
    answers["101"] = {"outcomes": ["Up", "Down"], "status": "RESOLVED", "resolved": {"index": 0, "name": "Up", "split": False, "how": "outcomes.status"}}
    answers["102"] = {"outcomes": ["Up", "Down"], "status": "RESOLVED", "resolved": {"index": 1, "name": "Down", "split": False, "how": "outcomes.status"}}
    await step(CLOSE + 72 * 60_000)
    # an open order on another market, and an old record kept before evidence was saved
    flip = m.SimMarket("hype-flip#1", "HYPE 反超 SOL", "flip", "HYPE", 0.30, book([("0.20", "200")], [("0.30", "50")], CLOSE + 73 * 60_000,
                       "will-hype-flip-sol-by-nov-26", "HYPE", "300"), 0.02, "", ("Yes", "No"), {"end": BJ(11, 1, 11, 59)},
                       {"basis": {"a": 41.2, "b": 160.5}, "sources": [{"what": "HYPE", "source": "Hyperliquid", "price": 41.2}]})
    await step(CLOSE + 73 * 60_000, flip)
    bot.store.put("sim:old|up|挂", {"market": "old", "slug": "old-slug", "item": "上证指数", "kind": "close", "key": "SSE", "side": "up",
                                   "label": "挂涨", "maker": True, "fair": 0.62, "signal": 0.12, "opened": NOW - 86_400_000, "settle": {},
                                   "price": 0.50, "shares": 100, "status": "settled", "filled": NOW - 80_000_000, "fill_fair": 0.58,
                                   "edge": 0.12, "payout": 0.0, "settled": NOW - 3_600_000, "note": "10-04 收盘 3,849，对 3,850"})
    return bot


async def run():
    bot = await build()
    trades = bot.sim_trades()
    hsi_maker = f"{HSI_SLUG}|up|挂"
    assert trades[hsi_maker]["confirm"] == "confirmed" and trades[f"{KOSPI_SLUG}|down|吃"]["confirm"] == "mismatch"
    assert trades["hype-flip#1|up|挂"]["status"] == "resting" and len(trades) == 6, sorted(trades)
    # the routes: the page, its data and the two exports; the token guards them all
    web = m.WebServer(bot, 0, TOKEN)
    status, ctype, body = web.route("GET", f"/p/{TOKEN}/journal")
    assert status == 200 and ctype.startswith("text/html") and "模拟交易复盘" in body.decode()
    status, ctype, body = web.route("GET", f"/p/{TOKEN}/journal.json")
    data = json.loads(body)
    assert status == 200 and ctype.startswith("application/json") and len(data["trades"]) == 6 and data["total"]["mismatch"] == 1
    waits = {t["id"]: t["wait"] for t in data["trades"]}  # every open trade says what it waits for; a settled one nothing
    assert waits["hype-flip#1|up|挂"] == "挂 20.0¢，最低卖价 30.0¢（高出 10.0¢）：要有人卖到挂价或更低才算成交；现在公平价 30.0¢", waits
    assert waits[hsi_maker] == "" and data["total"]["cancelled"] == 0
    status, ctype, body = web.route("GET", f"/p/{TOKEN}/journal.csv")
    text = body.decode("utf-8")
    assert status == 200 and ctype == "text/csv; charset=utf-8" and text.startswith("﻿编号,下单时间,市场")
    rows = list(csv.reader(io.StringIO(text[1:])))
    assert len(rows) == 7
    row = dict(zip(rows[0], next(r for r in rows if r[0] == f"{HSI_SLUG}|up|挂")))
    assert row["参考线"] == "24600.0" and row["口径"] == "盘后" and row["下单距收盘（小时）"] == "6.17" and row["最旧报价年龄（秒）"] == "20.0", row
    assert row["代理与锚点"] == "恒指期货；锚点 24500.0（16:10 现货收市时）" and "恒指期货=etnet 恒指期货(10/2026) @2026-10-05 09:59:40" in row["行情来源"]
    assert web.route("GET", f"/p/{'x' * 20}/journal")[0] == 404 and web.route("GET", f"/p/{TOKEN}/journal.xml")[0] == 404
    await browser_check(bot, data)
    print("JOURNAL_OK")


async def browser_check(bot, data):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        async_playwright = None
    chrome = next((p for p in [os.environ.get("CHROMIUM_PATH", ""), "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"]
                   if p and os.path.exists(p)), "")
    if async_playwright is None or not (chrome or os.environ.get("PLAYWRIGHT_BROWSERS_PATH")):
        print("browser check skipped (no Playwright/Chromium)")
        return
    web = m.WebServer(bot, 0, TOKEN); web.CACHE_SECONDS = {}; port = await web.start()
    url = f"http://127.0.0.1:{port}/p/{TOKEN}/journal"
    hsi_maker, mismatch = f"{HSI_SLUG}|up|挂", f"{KOSPI_SLUG}|down|吃"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**({"executable_path": chrome} if chrome else {}))
        page = await browser.new_page(viewport={"width": 390, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(url)
        await page.wait_for_selector("#list .tr")
        assert await page.get_attribute("#back", "href") == f"/p/{TOKEN}"
        # the exports are addressed from the page's own path, so they also work when the page was opened as /journal/
        assert await page.get_attribute("#csv", "href") == f"/p/{TOKEN}/journal.csv" and await page.get_attribute("#json", "href") == f"/p/{TOKEN}/journal.json"
        tiles = await page.inner_text("#tiles")
        tot = data["total"]
        for want in ("已结算", f"{tot['settled']} 笔", "模型预期（下单时）", "模型预期（成交时）", f"已确认 {tot['confirmed']}",
                     f"结果不一致 {tot['mismatch']}", "挂单中 1"):
            assert want in tiles, (want, tiles)
        assert await page.locator("#list .tr").count() == 6
        # filters: by how the result stands, by way of trading
        await page.click("#filters button:text-is('结果不一致')")
        assert await page.locator("#list .tr").count() == 1
        await page.click("#filters button:text-is('未成交/撤单')")
        assert await page.locator("#list .tr").count() == 1  # the KOSPI maker: no seller ever reached it
        await page.click("#filters button:text-is('挂单中')")
        assert await page.locator("#list .tr").count() == 1 and "⏳ 挂 20.0¢，最低卖价 30.0¢（高出 10.0¢）" in await page.inner_text("#list .tr .t3")
        await page.click("#filters button:text-is('持仓')")
        assert await page.locator("#list .tr").count() == sum(t["status"] == "filled" for t in data["trades"])
        await page.click("#filters button:text-is('全部') >> nth=0")
        await page.click("#filters button:text-is('吃单')")
        assert await page.locator("#list .tr").count() == 2
        await page.click("#filters button:text-is('全部') >> nth=0"); await page.click("#filters button:text-is('全部') >> nth=1")
        assert await page.locator("#list .tr").count() == 6
        # one trade's whole record
        await page.click(f"#list .tr:has-text('恒生指数 挂涨') button.row")
        det = page.locator("#list .tr.open .det")
        heads = await det.locator("h3").all_inner_texts()
        assert heads == ["判断依据（下单时）", "判断依据（成交时）", "行情来源", "代理与锚点", "成交证据", "结算证据", "版本与设置"], heads
        text = await det.inner_text()
        for want in ("建议门槛", "3.0¢", "etnet", "恒指期货(10/2026)", "16:10 现货收市时", "推定成交", "排在挂单时该价位已有的 300 份之后",
                     "看到卖单：55.0¢×30", "本地预结算", "Predict 结果", "涨 / Yes · Up", "代码版本", m.VERSION, "下单时卡片上的四个方向"):
            assert want in text, (want, text)
        assert urllib.parse.unquote(await page.evaluate("location.hash"))[1:] == hsi_maker
        # a mismatch says so and keeps the revision
        await page.click(f"#list .tr:has-text('KOSPI 吃跌') button.row")
        text = await page.locator("#list .tr.open .det").inner_text()
        assert "本地结果与 Predict 不一致，已按 Predict 的结果重新结算" in text and "Predict 最终结果" in text and "0.5 → 1" in text, text
        assert await page.locator("#list .tr.open").count() == 1  # one record open at a time
        # an old record says what it lacks
        await page.click(f"#list .tr:has-text('上证指数 挂涨') button.row")
        assert "旧版本记下的交易" in await page.locator("#list .tr.open .det").inner_text()
        await page.click(f"#list .tr:has-text('HYPE 反超 SOL') button.row")
        assert "现在等什么\n挂 20.0¢，最低卖价 30.0¢" in await page.locator("#list .tr.open .det").inner_text()
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "no sideways scroll"
        if os.environ.get("WEB_SCREENSHOT"):
            await page.screenshot(path=os.environ["WEB_SCREENSHOT"], full_page=True)
        # a link to one trade opens it straight away
        page2 = await browser.new_page(viewport={"width": 1100, "height": 900})
        await page2.goto(url + "#" + urllib.parse.quote(mismatch, safe=""))
        await page2.wait_for_selector("#list .tr.open")
        assert "KOSPI 吃跌" in await page2.inner_text("#list .tr.open .row")
        # the exports, as a browser fetches them
        csv_text = await page2.evaluate("fetch('journal.csv').then(r => r.text())")
        assert csv_text.startswith("﻿编号") or csv_text.startswith("编号"), csv_text[:20]
        got = await page2.evaluate("fetch('journal.json').then(r => r.json()).then(d => d.trades.length)")
        assert got == 6
        await page2.close()
        assert not errors, errors
        await browser.close()
    await web.stop()


asyncio.run(run())
