import asyncio, os, sys, time, json, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
m.CapMarket.GECKO_GAP = 0  # no real GeckoTerminal here
D = m.D

# config + slugs
c = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x"})
assert c.predict and c.predict_poll == 15 and c.predict_api_key == "" and c.predict_ref == "B00EA"
assert m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "PREDICT_REF_CODE": ""}).predict_ref == ""
assert m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "PREDICT_REF_CODE": " AB12 "}).predict_ref == "AB12"
try: m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "PREDICT_REF_CODE": "a&b=c"}); assert False
except ValueError: pass
assert m.predict_url("cxmt-up-or-down-on-september-28-2026", "B00EA") == "https://predict.fun/zh-cn/market/cxmt-up-or-down-on-september-28-2026?ref=B00EA"
assert m.predict_url("cxmt-up-or-down-on-september-28-2026") == "https://predict.fun/zh-cn/market/cxmt-up-or-down-on-september-28-2026"
assert c.predict_slugs["HSI"] == "hang-seng-index" and c.predict_slugs["SKHYNIXUSDT"] == "sk-hynix-inc" and len(c.predict_slugs) == 7
assert not m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "PREDICT": "off"}).predict
assert m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "PREDICT_SLUGS": "hsi=Hang-Seng-Index"}).predict_slugs == {"HSI": "hang-seng-index"}
for bad in ["HSI", "HSI=bad slug", "HSI=-x"]:
    try: m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "PREDICT_SLUGS": bad}); assert False, bad
    except ValueError: pass
# the site's URL format (the user's links)
want = {"HSI": "hang-seng-index-up-or-down-on-september-28-2026", "KOSPI": "kospi-composite-index-up-or-down-on-september-28-2026",
        "SSE": "sse-composite-index-up-or-down-on-september-28-2026", "UNITREEUSDT": "unitree-up-or-down-on-september-28-2026",
        "HK0625USDT": "shein-up-or-down-on-september-28-2026", "CXMTUSDT": "cxmt-up-or-down-on-september-28-2026",
        "SKHYNIXUSDT": "sk-hynix-inc-up-or-down-on-september-28-2026"}
for key, slug in want.items():
    assert m.predict_slug(c.predict_slugs[key], dt.date(2026, 9, 28)) == slug
assert m.predict_slug("cxmt", dt.date(2026, 10, 8)) == "cxmt-up-or-down-on-october-8-2026"

# levels: REST [price, size] strings and WS-style objects, unsorted, zero sizes dropped
bids = m.predict_levels([["0.80", "400"], ["0.81", "110.5"], {"price": 0.79, "size": 100}, ["0.5", "0"], ["x", "1"], ["1.2", "3"]], True)
assert bids == ((D("0.81"), D("110.5")), (D("0.80"), D("400")), (D("0.79"), D("100"))), bids
asks = m.predict_levels([["0.99", "2000"], ["0.85", "12"], ["0.94", "157.4"]], False)
assert asks[0] == (D("0.85"), D("12"))
assert m.predict_markets({"data": {"markets": {"edges": [{"node": {"id": 7, "conditionId": "0xab", "title": "Up?", "question": "q"}}]}}}) \
    == [{"id": "7", "conditionId": "0xab", "title": "Up?", "question": "q"}]

# edges: the screenshot's HSI book (买1 81¢, 卖1 85¢) with the model at 涨 78¢
now = int(time.time() * 1000)
book = m.PredictBook("HSI", want["HSI"], "7", "HSI", bids, asks, now)
edges = {e.label: e for e in m.book_edges(0.78, book)}
assert abs(edges["挂涨"].edge - (0.78 - 0.81)) < 1e-9 and abs(edges["挂跌"].price - 0.15) < 1e-9 and abs(edges["挂跌"].edge - 0.07) < 1e-9
assert abs(edges["吃涨"].edge - (0.78 - 0.85)) < 1e-9 and abs(edges["吃跌"].price - 0.19) < 1e-9 and abs(edges["吃跌"].edge - 0.03) < 1e-9
assert m.best_edge(list(edges.values())).label == "挂跌"
assert m.best_edge(m.book_edges(0.9, book)).label == "挂涨" and m.best_edge([e for e in m.book_edges(0.9, book) if not e.maker]).label == "吃涨"
assert m.best_edge(m.book_edges(0.83, book)).label in {"挂涨", "挂跌"}  # both makers +2¢; never a negative side
assert m.best_edge([e for e in m.book_edges(0.83, book) if not e.maker]) is None
one_sided = m.PredictBook("HSI", "s", "7", "t", bids, (), now)
assert {e.label for e in m.book_edges(0.5, one_sided)} == {"挂涨", "吃跌"}


async def run():
    class FakeTelegram:
        def __init__(self): self.sent = []
        async def call(self, *a, **k): return True
        async def send(self, chat, thread, text, reply_markup=None, parse_mode=None): self.sent.append(text)
    class FakeMarket:
        def now_ms(self): return int(dt.datetime(2026, 9, 28, 10, 5, tzinfo=m.BEIJING).timestamp() * 1000)
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ADMIN_USER_ID": "42", "SYMBOLS": "UNITREEUSDT,CXMTUSDT",
                             "HSI_FUTURES": "off", "KOSPI_INDEX": "off", "PREDICT_API_KEY": "k123"})
    tg = FakeTelegram(); bot = m.Bot(cfg, m.Store(":memory:"), FakeMarket(), tg)
    now_ms = bot.market.now_ms()
    # SSE trading on 09-28: model targets today's close
    bot.cn.quote = m.IndexQuote("上证指数", D("3860"), D("3850"), None, None, None, now_ms, "腾讯")
    bot.cn.close = m.DailyClose(dt.date(2026, 9, 25), D("3850"), D("3840"), "腾讯日K", 0)
    targets = bot.predict_targets(now_ms)
    assert targets["SSE"] == want["SSE"] and targets["HSI"] == want["HSI"] and targets["KOSPI"] == want["KOSPI"], targets
    assert targets["UNITREEUSDT"] == want["UNITREEUSDT"] and targets["CXMTUSDT"] == want["CXMTUSDT"] and "SKHYNIXUSDT" not in targets
    # fake Predict: SSE resolves through GraphQL, CXMT only through REST /categories, UNITREE is not listed, HSI network down
    calls = []
    async def fetch(url, payload=None):
        calls.append((url, payload and payload["variables"]))
        assert bot.predict.headers() == {"x-api-key": "k123"}
        if url == m.PREDICT_GRAPHQL:
            v = payload["variables"]
            if "id" in v:
                if v["id"] == want["HSI"]:
                    raise m.RemoteError("网络错误 (URLError)")
                return {"data": {"category": {"id": "55"} if v["id"] == want["SSE"] else None}}
            assert v == {"f": {"categoryId": "55"}}
            return {"data": {"markets": {"edges": [{"node": {"id": "901", "conditionId": "0xc", "title": "SSE up?", "question": "q"}}]}}}
        if url.endswith("/categories/" + want["CXMTUSDT"]):
            return {"success": True, "data": {"id": 3, "slug": want["CXMTUSDT"], "markets": [{"id": 902, "conditionId": "0xd", "title": "CXMT up?"}]}}
        if url.endswith("/categories/" + want["HSI"]):
            raise m.RemoteError("网络错误 (TimeoutError)")
        if "/categories/" in url:
            raise m.RemoteError("HTTP 404: 接口请求失败")
        if url.endswith("/markets/901/orderbook"):
            return {"success": True, "data": {"marketId": 901, "bids": [["0.81", "400"], ["0.80", "110"]], "asks": [["0.85", "12"], ["0.94", "157.4"]]}}
        if url.endswith("/markets/902/orderbook"):
            raise m.RemoteError("HTTP 404: 接口请求失败")
        if url.endswith("/markets/0xd/orderbook"):
            return {"data": {"bids": [], "asks": [["0.4", "50"]]}}
        raise AssertionError(url)
    bot.predict.fetch = fetch
    outcome = await bot.predict.refresh(targets)
    # HSI's network failure makes the round partial; markets not listed yet are answers, not failures
    assert isinstance(outcome, m.Refreshed) and outcome.status == "partial" and outcome.error.startswith("HSI：网络错误"), outcome
    assert await bot.predict.refresh(targets) is False  # not due yet
    books, errors = bot.predict.books, bot.predict.errors
    assert books["SSE"].bid == (D("0.81"), D("400"))
    assert books["CXMTUSDT"].ask == (D("0.4"), D("50")) and bot.predict.book_keys["902"] == "0xd"  # id 404 → conditionId
    assert "还没有这个市场" in errors["UNITREEUSDT"] and "还没有这个市场" in errors["KOSPI"]
    assert "网络错误" in errors["HSI"] and want["HSI"] not in bot.predict.markets  # unknown ≠ not listed: retried next tick
    assert bot.predict.markets[want["UNITREEUSDT"]][0] is None  # cached miss: not looked up again next tick
    n = len(calls); bot.predict.refreshed = -1e9
    await bot.predict.refresh(targets)
    assert not any(want["UNITREEUSDT"] in u for u, _ in calls[n:]) and any(u.endswith("/markets/0xd/orderbook") for u, _ in calls[n:])
    assert not any("/markets/902/orderbook" in u for u, _ in calls[n:]), "the key that answered is tried first"
    # target price (目标价): GraphQL marketData.startPrice on the category's own type
    feed = m.PredictFeed(cfg)
    ks = "kospi-composite-index-up-or-down-on-september-29-2026"
    asked = []
    async def gql(url, payload=None):
        asked.append(payload["query"])
        if "__typename" in payload["query"]:
            return {"data": {"category": {"id": "77", "__typename": "StockUpDownCategory"}}}
        if "... on StockUpDownCategory" in payload["query"]:
            return {"data": {"category": {"marketData": [{"marketId": "990", "startPrice": 6910.89}]}}}
        if "markets(" in payload["query"]:
            return {"data": {"markets": {"edges": [{"node": {"id": "990", "conditionId": "0xe", "title": "KOSPI up?"}}]}}}
        raise AssertionError(payload)
    feed.fetch = gql
    market = await feed.resolve(ks)
    assert market["id"] == "990" and feed.types[ks] == "StockUpDownCategory"
    assert await feed.strike(ks, "990") == D("6910.89") and await feed.strike(ks, "990") == D("6910.89")
    assert sum("startPrice" in q for q in asked) == 1, "cached for a while"
    async def no_field(url, payload=None):
        raise m.RemoteError('Predict GraphQL 错误：[{"message": "Cannot query field \\"marketData\\" on type \\"X\\""}]')
    other = "x-up-or-down-on-september-29-2026"
    feed.types[other] = "X"; feed.fetch = no_field
    assert await feed.strike(other, "1") is None and "X" in feed.no_strike
    # the odds are measured against it; a proxy-mapped estimate moves with it, a live print does not
    kcfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "SKHYNIXUSDT"})
    kbot = m.Bot(kcfg, m.Store(":memory:"), FakeMarket(), tg)
    kbot.predict.strikes[ks] = (D("6910.89"), time.monotonic())
    after = m.close_odds("KOSPI", D("6889.74"), D("6899.514"), 0.012, 1.0, dt.date(2026, 9, 29), D("0.01"), "09-28 收盘",
                         "HL KR200 1,000 / 收盘时刻 999 → +0.14%（KOSPI200 代理）", "σ", mode="盘后")
    s = kbot.settle_on_strike("KOSPI", after)
    assert s.ref == D("6910.89") and abs(s.effective - D("6899.514") * D("6910.89") / D("6889.74")) < D("1e-9"), s
    assert abs(s.fair_up - after.fair_up) < 1e-6 and "Predict 目标价" in s.ref_note and "6,889.74" in s.ref_note, s
    live = m.close_odds("KOSPI", D("6889.74"), D("6899.514"), 0.012, 0.5, dt.date(2026, 9, 29), D("0.01"), "09-28 收盘",
                        "KOSPI 现货 6,899.514（盘中直接用现货）", "σ")
    s = kbot.settle_on_strike("KOSPI", live)
    assert s.ref == D("6910.89") and s.effective == D("6899.514") and s.fair_up < live.fair_up, s
    assert kbot.settle_on_strike("KOSPI", m.dataclasses.replace(after, target=dt.date(2026, 9, 30))) == m.dataclasses.replace(after, target=dt.date(2026, 9, 30))
    kbot.predict.strikes[ks] = (D("1.0"), time.monotonic())  # nonsense (other unit): shown, not used
    s = kbot.settle_on_strike("KOSPI", after)
    assert s.ref == after.ref and "未采用" in s.ref_note, s
    # /book: SSE shows the edges and the best side; missing items say why
    tg.sent.clear()
    await bot.process_message({"text": "/book", "chat": {"id": 1}, "from": {"id": 42}, "date": time.time()})
    text = tg.sent[-1]
    assert "Predict 盘口 vs 模型公平价" in text and "📍 上证指数" in text and "买1 81.0¢×400" in text and "卖1 85.0¢×12" in text, text
    assert "挂涨 81.0¢" in text and "挂跌 15.0¢" in text and "吃涨 85.0¢" in text and "吃跌 19.0¢" in text and "👉" in text, text
    assert "还没有这个市场" in text and m.PREDICT_SITE + want["UNITREEUSDT"] + "?ref=B00EA" in text and m.PREDICT_SITE + want["SSE"] + "?ref=B00EA" in text and "网络错误" in text
    assert "中间 83.0¢" in text and "<b>" in text
    sse = bot.sse_odds(now_ms)
    best = m.best_edge(m.book_edges(sse.fair_up, books["SSE"]))
    assert best and f"{best.label}</b> @ {m.cents(best.price)}" in text, text
    # /prob carries the book too (no link there)
    tg.sent.clear()
    await bot.process_message({"text": "/prob", "chat": {"id": 1}, "from": {"id": 42}, "date": time.time()})
    assert "📕 Predict 涨 买1 81.0¢" in tg.sent[-1] and m.PREDICT_SITE not in tg.sent[-1]
    # web payload
    payload = bot.odds_payload()
    item = next(i for i in payload["items"] if i["name"] == "上证指数")
    p = item["predict"]
    assert p["url"] == m.PREDICT_SITE + want["SSE"] + "?ref=B00EA" and p["bids"][0] == [0.81, 400.0] and not p["stale"] and p["error"] == ""
    assert [e["label"] for e in p["edges"]] == ["挂涨", "挂跌", "吃涨", "吃跌"] and sum(e["best"] for e in p["edges"]) == 1
    uni = next(i for i in payload["items"] if i["name"] == "宇树 UNITREE")
    assert uni["missing"] == "等待行情" and "还没有这个市场" in uni["predict"]["error"] and "edges" not in uni["predict"]
    json.dumps(payload)
    # a stale book is shown but never recommended
    sse_book = books["SSE"]
    bot.predict.books["SSE"] = m.dataclasses.replace(books["SSE"], fetched_ms=now_ms - 200_000)
    assert not any(e["best"] for e in bot.predict_payload("上证指数", sse, now_ms)["edges"])
    assert "盘口过期，不给建议" in "\n".join(m.book_lines(bot.predict.books["SSE"], "", sse, now_ms))
    # the target day moves on: yesterday's book is dropped at once
    bot.predict.refreshed = -1e9
    moved = {**targets, "SSE": "sse-composite-index-up-or-down-on-september-29-2026"}
    await bot.predict.refresh(moved)
    assert "SSE" not in bot.predict.books and bot.predict.slugs["SSE"].endswith("september-29-2026")
    # PREDICT=off: no job, no targets, command says so
    off = m.Bot(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "PREDICT": "off"}), m.Store(":memory:"), FakeMarket(), tg)
    assert off.predict_targets(now_ms) == {} and "Predict 盘口" not in [n for n, _ in off.reference_jobs()]
    assert "已关闭" in off.cmd_book(None).text
    # web page renders the block (optional browser check)
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        async_playwright = None
    chrome = next((p for p in [os.environ.get("CHROMIUM_PATH", ""), "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"]
                   if p and os.path.exists(p)), "")
    if async_playwright is None or not (chrome or os.environ.get("PLAYWRIGHT_BROWSERS_PATH")):
        print("browser check skipped (no Playwright/Chromium)")
        return
    bot.predict.books["SSE"] = m.dataclasses.replace(sse_book, fetched_ms=now_ms)
    bot.predict.slugs["SSE"] = want["SSE"]
    bot.predict.errors.pop("SSE", None)
    web = m.WebServer(bot, 0, "t" * 20); web.CACHE_SECONDS = {}; port = await web.start()  # the tests change the payload and reload at once
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**({"executable_path": chrome} if chrome else {}))
        page = await browser.new_page(viewport={"width": 390, "height": 900})
        await page.goto(f"http://127.0.0.1:{port}/p/{'t' * 20}")
        await page.wait_for_selector(".pb .edges")
        text = await page.inner_text("#g-index")
        assert "Predict ↗" in text and "买1 81.0¢×400" in text and "挂跌 15.0" in text and "+24.9¢" in text, text
        # a best edge of 10¢ or more puts a red frame round the card
        assert await page.locator("#g-index .card.hot .edge.best.hot").count() == 1
        assert await page.locator(".edge.best").count() == 1
        # the market opens from its own Predict ↗ button, in a new tab; the four chips are buttons that open their details
        href = m.PREDICT_SITE + want["SSE"] + "?ref=B00EA"
        link = page.locator("#g-index .pb a.open")
        assert await link.get_attribute("href") == href and await link.get_attribute("target") == "_blank"
        assert await page.locator("#g-index .pb .edges button.edge.best").count() == 1 and await page.locator("#g-index .pb .edges button.edge").count() == 4
        # framed = suggested, grey = not big enough: no threshold on the card itself, only in a tapped chip's details
        assert "门槛" not in await page.inner_text("#g-index .card")
        p = next(i for i in bot.odds_payload()["items"] if i["name"] == "上证指数")["predict"]
        best = next(e for e in p["edges"] if e["best"])
        await page.click("#g-index .edge.best")
        det = await page.inner_text("#g-index .edet")
        assert f"{best['label']} @ {m.cents(best['price'])}：挂单排队" in det and f"毛优势 {m.cents(best['gross'], True)}" in det, det
        assert f"净优势 {m.cents(best['edge'], True)} · 建议门槛 {m.cents(p['need'])}" in det and "→ 满足" in det and "高亮门槛 10¢ → 标红框" in det, det
        assert len(page.context.pages) == 1 and await page.get_attribute("#g-index .edge.best", "aria-expanded") == "true"
        grey = page.locator("#g-index .edge:not(.pos)")
        assert await grey.count() >= 1
        await grey.first.click()  # another chip swaps the details; a grey one says why it is not suggested
        det = await page.inner_text("#g-index .edet")
        assert "→ 不满足" in det and "高亮门槛" not in det and await page.locator("#g-index .edet").count() == 1, det
        await grey.first.click(); assert await page.locator("#g-index .edet").count() == 0  # tapped again: closed
        await page.context.route("https://predict.fun/**", lambda route: route.fulfill(body="predict", content_type="text/html"))
        async with page.context.expect_page() as popup:
            await link.click()
        tab = await popup.value; await tab.wait_for_load_state()
        assert tab.url == href, tab.url
        await tab.close()
        assert "还没有这个市场" in await page.inner_text("#g-contract")
        # the header toggle hides every Predict block, and the choice survives a reload
        await page.click("#showbook"); assert not await page.is_visible("#g-index .pb")
        await page.reload(); await page.wait_for_selector(".card .odds")
        assert not await page.is_checked("#showbook") and not await page.is_visible("#g-index .pb")
        await page.click("#showbook"); assert await page.is_visible("#g-index .pb")
        wide = await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert wide, "no horizontal scroll at phone width"
        # ✎ 自定义 moves the red-frame bar (default 10¢): 30¢ clears this +24.9¢ card, 20¢ brings it back
        assert "红框 = 净优势 ≥10¢" in await page.inner_text("#legend")
        await page.click("#edit"); assert await page.input_value("#hotin") == "10"
        await page.fill("#hotin", "30"); await page.press("#hotin", "Tab")
        assert await page.locator(".card.hot").count() == 0 and await page.locator(".edge.hot").count() == 0
        assert "红框 = 净优势 ≥30¢" in await page.inner_text("#legend") and await page.evaluate("localStorage.getItem('hot')") == "30"
        await page.reload(); await page.wait_for_selector(".pb .edges")
        assert await page.locator(".card.hot").count() == 0
        await page.click("#edit"); assert await page.input_value("#hotin") == "30"
        await page.fill("#hotin", ""); await page.press("#hotin", "Tab")  # an emptied box keeps the bar it had
        assert await page.input_value("#hotin") == "30"
        await page.fill("#hotin", "99"); await page.press("#hotin", "Tab")  # kept within 1–50¢
        assert await page.input_value("#hotin") == "50"
        await page.fill("#hotin", "20"); await page.press("#hotin", "Tab"); await page.click("#done")
        assert await page.locator("#g-index .card.hot .edge.best.hot").count() == 1
        assert (await page.get_attribute("#g-index .card.hot", "title")).startswith("净优势 ≥20¢：")
        if os.environ.get("WEB_SCREENSHOT"):
            await page.screenshot(path=os.environ["WEB_SCREENSHOT"], full_page=True)
        await browser.close()
    await web.stop()

asyncio.run(run())
print("PREDICT_OK")
