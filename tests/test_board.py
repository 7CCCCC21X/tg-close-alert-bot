"""The board's filter / sort bar, the trade-size calculator, tap-to-open edge details, data ages and the stale-data
warning (web page; the browser part needs Playwright + Chromium and is skipped without them)."""
import asyncio, copy, os, re, sys, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D

NOW = int(dt.datetime(2026, 9, 28, 10, 5, tzinfo=m.BEIJING).timestamp() * 1000)  # Monday, the SSE session is open
SLUG = "sse-composite-index-up-or-down-on-september-28-2026"


class FakeMarket:
    def now_ms(self): return NOW


def levels(rows):
    return tuple((D(str(p)), D(str(q))) for p, q in rows)


cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off"})
bot = m.Bot(cfg, m.Store(":memory:"), FakeMarket(), None)
bot.cn.quote = m.IndexQuote("上证指数", D("3860"), D("3850"), None, None, None, NOW - 3000, "腾讯")
bot.cn.close = m.DailyClose(dt.date(2026, 9, 25), D("3850"), D("3840"), "腾讯日K", 0)
bot.predict.slugs["SSE"] = SLUG
sse_book = m.PredictBook("SSE", SLUG, "901", "SSE up?", levels([(0.81, 400), (0.80, 110)]), levels([(0.85, 12), (0.94, 157.4)]),
                         NOW - 7000, 200)
bot.predict.books["SSE"] = sse_book

# --- the payload: the depth, fee and trade size behind the edges, the bar a suggestion must clear, and the ages --------
payload = bot.odds_payload()
assert payload["notional"] == 100
sse = next(i for i in payload["items"] if i["name"] == "上证指数")
p = sse["predict"]
assert sse["quote_ms"] == NOW - 3000 and sse["source"] == "现货", (sse["quote_ms"], sse["source"])
assert p["fetched_ms"] == NOW - 7000 and p["age"] == 7 and p["fee_bps"] == 200 and p["sides"] == ["涨", "跌"] and p["notional"] == 100, p
assert p["fair"] == sse["fair_up"] and p["need"] == max(cfg.predict_min_edge, p["swing"]) and p["hold"] == "", p
assert p["bids"] == [[0.81, 400.0], [0.80, 110.0]] and p["asks"] == [[0.85, 12.0], [0.94, 157.4]]
assert [(e["label"], e["up"], e["maker"]) for e in p["edges"]] == [("挂涨", True, True), ("挂跌", False, True), ("吃涨", True, False), ("吃跌", False, False)]
best = m.best_edge(m.book_edges(p["fair"], sse_book, bot.edge_costs()), p["need"])
assert [e["label"] for e in p["edges"] if e["best"]] == ["挂跌"] == [best.label]
# a warning on the odds holds the suggestion back: the edges stay, the reason goes with them
held = {}
bot.book_block(held, sse_book, p["fair"], p["need"], p["swing"], "代理过期", ("涨", "跌"), NOW)
assert held["hold"] == "代理过期" and not any(e["best"] for e in held["edges"]) and len(held["edges"]) == 4
# no fair price (the odds are missing): the depth only
bare = {}
bot.book_block(bare, sse_book, None, 0.02, 0.0, "", ("涨", "跌"), NOW)
assert "edges" not in bare and "fair" not in bare and bare["bids"] == p["bids"] and bare["fee_bps"] == 200


def variant(name, fair, bids, asks, close_in=None, fetched_ago=5000):
    """A copy of the SSE card with its own book, priced by the server's own code."""
    it = copy.deepcopy(sse)
    it["name"] = it["symbol"] = name
    it["fair_up"], it["fair_down"] = fair, 1 - fair
    book = m.PredictBook(name, "x", "1", name, levels(bids), levels(asks), NOW - fetched_ago, 200)
    out = {"url": p["url"], "error": ""}
    bot.book_block(out, book, fair, bot.edge_need(0.0), 0.0, "", ("涨", "跌"), NOW)
    it["predict"] = out
    if close_in is not None:
        it["close_ms"] = NOW + close_in
    return it, book


# 甲: 挂跌 +8¢ (and 吃跌 +4.1¢) · 乙: only 吃涨 +14.1¢ (no bids) · 丙: nothing clears 2¢ · 丁: 挂跌 +12¢, closes in an hour
jia, jia_book = variant("甲", 0.40, [(0.45, 300)], [(0.48, 300)])
yi, yi_book = variant("乙", 0.70, [], [(0.55, 1000)])
bing, _ = variant("丙", 0.50, [(0.49, 100)], [(0.51, 100)])
ding, _ = variant("丁", 0.30, [(0.40, 1000)], [(0.42, 1000)], close_in=3600_000)
old, _ = variant("戊", 0.30, [(0.40, 1000)], [(0.42, 1000)], fetched_ago=120_000)  # a two-minute-old book: no suggestion
assert old["predict"]["stale"] and not any(e["best"] for e in old["predict"]["edges"])
assert [e["label"] for e in jia["predict"]["edges"] if e["best"]] == ["挂跌"] and [e["label"] for e in yi["predict"]["edges"] if e["best"]] == ["吃涨"]
assert not any(e["best"] for e in bing["predict"]["edges"]) and [e["label"] for e in ding["predict"]["edges"] if e["best"]] == ["挂跌"]
payload["items"] = [sse, jia, yi, bing, ding, old]
bot.odds_payload = lambda: payload


async def browser_check():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        async_playwright = None
    chrome = next((x for x in [os.environ.get("CHROMIUM_PATH", ""), "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"]
                   if x and os.path.exists(x)), "")
    if async_playwright is None or not (chrome or os.environ.get("PLAYWRIGHT_BROWSERS_PATH")):
        print("browser check skipped (no Playwright/Chromium)")
        return
    web = m.WebServer(bot, 0, "t" * 20); web.CACHE_SECONDS = {}; port = await web.start()  # the tests change the payload and reload at once
    url = f"http://127.0.0.1:{port}/p/{'t' * 20}"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**({"executable_path": chrome} if chrome else {}))
        page = await browser.new_page(viewport={"width": 390, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(url)
        await page.wait_for_selector("#g-index .pb .edges")
        names = lambda grid: page.eval_on_selector_all(f"{grid} .card .name", "els => els.map(e => e.textContent)")
        assert await names("#g-index") == ["上证指数", "甲", "乙", "丙", "丁", "戊"], await names("#g-index")

        # the page prices the four directions itself, from the depth: at the server's trade size it gets the server's numbers
        js = await page.evaluate("last.items.map(it => edgesFor(it.predict, it.predict.fair))")
        for it, mine in zip(payload["items"], js):
            theirs = it["predict"]["edges"]
            assert [e["label"] for e in mine] == [e["label"] for e in theirs], (it["name"], mine)
            for a, b in zip(mine, theirs):
                for k in ("price", "edge", "size", "gross", "fee", "slip"):
                    assert abs(a[k] - b[k]) < 1e-12, (it["name"], a["label"], k, a[k], b[k])
                assert a["short"] == b["short"] and a["maker"] == b["maker"] and a["up"] == b["up"], (it["name"], a, b)
        assert await page.locator("#g-index .card.hot").count() == 3  # SSE +24.9¢, 乙 +14.1¢, 丁 +12¢ (甲 +8¢ is under 10¢)

        # ages: when the price and the book were read, and what the price is
        ages = await page.inner_text("#g-index .card:first-child .ages")
        assert re.fullmatch(r"行情 \d 秒前\s+盘口 \d{1,2} 秒前\s+现货", ages), ages
        assert await page.locator("#g-index .card:nth-child(6) .ages .age.old").count() == 1  # 戊's book: 2 分钟前, flagged
        assert "盘口 2 分钟前" in await page.inner_text("#g-index .card:nth-child(6) .ages")
        assert await page.locator("#g-index .card:nth-child(6) .edge.best").count() == 0

        # the trade size: 500 U walks deeper (SSE's asks hold $158 in all: 深度不足), 50 U less deep; the page's numbers
        # are the server's book_edges for that size
        async def same_as_server(amount):
            mine = await page.evaluate("last.items.map(it => edgesFor(it.predict, it.predict.fair))")
            for it, book, got in ((sse, sse_book, mine[0]), (jia, jia_book, mine[1]), (yi, yi_book, mine[2])):
                want = m.book_edges(it["predict"]["fair"], book, m.EdgeCosts(200, amount))
                for a, b in zip(got, want):
                    assert abs(a["edge"] - b.edge) < 1e-12 and abs(a["size"] - b.size) < 1e-12 and a["short"] == b.short, (amount, a, b)
        await page.click("#amts button:text-is('500')")
        assert await page.evaluate("amount") == 500 and await page.evaluate("localStorage.getItem('amount')") == "500"
        await same_as_server(500)
        sse_chips = page.locator("#g-index .card:first-child .edge")
        assert await sse_chips.nth(2).inner_text() == "吃涨 85.0\n" + m.cents(m.book_edges(p["fair"], sse_book, m.EdgeCosts(200, 500))[2].edge, True) + "\n深度不足"
        await sse_chips.nth(2).click()
        det = await page.inner_text("#g-index .card:first-child .edet")
        assert "按 $500 吃单：均价" in det and "盘口只够买 169 份，金额超出已读取的深度" in det, det
        await page.click("#amts button:text-is('50')")
        await same_as_server(50)
        await page.fill("#amtin", "37"); await page.press("#amtin", "Enter")
        assert await page.evaluate("amount") == 37
        await same_as_server(37)
        await page.click("#amts button:text-is('100')")  # the server's own size: stored as 0 (follows PREDICT_TRADE_USD)
        assert await page.evaluate("amount") == 0 and await page.input_value("#amtin") == ""
        await same_as_server(100)
        det = await page.inner_text("#g-index .card:first-child .edet")
        assert "按 $100 吃单" in det, det

        # an open detail survives the 10-second refresh (the cards are rebuilt underneath it)
        await sse_chips.nth(1).click()
        before = await page.inner_text("#g-index .card:first-child .edet")
        assert before.startswith("挂跌 @ 15.0¢：挂单排队") and "→ 满足" in before and "高亮门槛 10¢ → 标红框" in before, before
        await page.evaluate("load()")
        await page.wait_for_function("document.querySelector('#g-index .card .edet') !== null")
        assert await page.inner_text("#g-index .card:first-child .edet") == before
        assert await page.get_attribute("#g-index .card:first-child .edge.sel", "aria-expanded") == "true"

        # filters: every chip that is on must hold for one and the same suggestion
        flat = lambda: names("#g-flat")
        await page.click("#fchips button:text-is('有建议')")
        assert await page.is_visible("#g-flat") and not await page.is_visible("#g-index")
        assert await flat() == ["上证指数", "甲", "乙", "丁"], await flat()  # 丙: nothing clears the bar; 戊: stale book
        await page.click("#fchips button:text-is('仅 No/跌')")
        assert await flat() == ["上证指数", "甲", "丁"], await flat()
        await page.click("#fchips button:text-is('仅挂单')")
        await page.select_option("#sortsel", "edge")  # the user's daily view: No, maker, largest net edge first
        assert await flat() == ["上证指数", "丁", "甲"], await flat()
        assert (await page.inner_text("#h-flat")).startswith("筛选结果 3 张 · 按净优势")
        await page.reload(); await page.wait_for_selector("#g-flat .card")  # kept in this browser
        assert await flat() == ["上证指数", "丁", "甲"] and await page.input_value("#sortsel") == "edge"
        await page.click("#fchips button:text-is('仅 No/跌')")
        await page.click("#fchips button:text-is('仅吃单')")  # 仅挂单 and 仅吃单 exclude each other
        pressed = await page.eval_on_selector_all("#fchips button.on", "els => els.map(e => e.textContent)")
        assert pressed == ["有建议", "仅吃单"], pressed
        assert await flat() == ["上证指数", "乙", "丁", "甲"], await flat()  # 吃跌 +20.3, 吃涨 +14.1, 吃跌 +9.2, 吃跌 +4.1
        await page.click("#h-flat button:text-is('清除筛选')")
        assert not await page.is_visible("#g-flat") and await page.is_visible("#g-index")
        assert await page.evaluate("localStorage.getItem('filt')") == "[]" and await page.input_value("#sortsel") == ""
        await page.click("#fchips button:text-is('3 小时内收盘')")
        assert await flat() == ["丁"], await flat()
        await page.click("#fchips button:text-is('3 小时内收盘')")
        await page.select_option("#sortsel", "time")  # sorting alone lists every card, soonest close first
        assert await flat() == ["丁", "上证指数", "甲", "乙", "丙", "戊"], await flat()
        await page.select_option("#sortsel", "")
        assert await page.is_visible("#g-index") and not await page.is_visible("#g-flat")
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "no sideways scroll"

        # a failed refresh: the cards stay, marked as old data with the last good time; a minute on, the suggestions lose
        # their highlight; 立即重试 fetches at once
        await page.route("**/data.json", lambda route: route.fulfill(status=500, body="down"))
        await page.evaluate("load()")
        await page.wait_for_selector("#stale:not([hidden])")
        msg = await page.inner_text("#stalemsg")
        assert re.fullmatch(r"⚠️ 刷新失败（HTTP 500），当前为旧数据：最后成功 \d\d:\d\d:\d\d（\d+ 秒前），\d+ 秒后撤掉建议高亮", msg), msg
        assert not await page.evaluate("document.body.classList.contains('olddata')")
        assert await page.locator("#g-index .card").count() == 6
        hot, plain = page.locator("#g-index .card.hot").first, page.locator("#g-index .card:not(.hot)").first
        look = lambda el: el.evaluate("e => getComputedStyle(e).boxShadow + ' ' + getComputedStyle(e).borderTopColor + ' ' + getComputedStyle(e).borderTopWidth")
        await page.mouse.move(0, 0)  # off the cards: a hovered card has a deeper shadow
        assert await look(hot) != await look(plain)  # the red frame and its glow
        await page.evaluate("okAt -= 61000; drawStale()")
        assert await page.evaluate("document.body.classList.contains('olddata')")
        await page.wait_for_timeout(300)  # the frame fades over 0.2 s
        assert (await page.inner_text("#stalemsg")).endswith("；建议高亮已撤掉")
        assert await look(hot) == await look(plain), (await look(hot), await look(plain))  # the frame is gone: the card looks like any other
        await page.unroute("**/data.json")
        await page.click("#retry")
        await page.wait_for_selector("#stale", state="hidden")
        assert not await page.evaluate("document.body.classList.contains('olddata')")
        assert await look(page.locator("#g-index .card.hot").first) != await look(plain)

        # never loaded at all: the header says so, the banner too
        page2 = await browser.new_page(viewport={"width": 390, "height": 700})
        await page2.route("**/data.json", lambda route: route.fulfill(status=503, body="down"))
        await page2.goto(url)
        await page2.wait_for_selector("#stale:not([hidden])")
        assert "刷新失败：HTTP 503" in await page2.inner_text("#meta") and "还没取到数据" in await page2.inner_text("#stalemsg")
        await page2.close()
        assert not errors, errors
        if os.environ.get("WEB_SCREENSHOT"):
            await page.screenshot(path=os.environ["WEB_SCREENSHOT"], full_page=True)
        await browser.close()
    await web.stop()


asyncio.run(browser_check())
print("BOARD_OK")
