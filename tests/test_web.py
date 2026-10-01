import asyncio, os, sys, time, json, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
m.CapMarket.GECKO_GAP = 0  # no real GeckoTerminal here
D = m.D
# config
c = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "PORT": "8080", "RAILWAY_PUBLIC_DOMAIN": "bot.up.railway.app"})
assert c.web_port == 8080 and c.web_base == "https://bot.up.railway.app" and c.web_token == ""
assert m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "PORT": "8080", "WEB": "off"}).web_port == 0
assert m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x"}).web_port == 0
c3 = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "WEB_PORT": "9000", "WEB_BASE_URL": "https://x.example/", "WEB_TOKEN": "abcdefghijklmnop"})
assert c3.web_port == 9000 and c3.web_base == "https://x.example" and c3.web_token == "abcdefghijklmnop"
for bad in [{"WEB_TOKEN": "short"}, {"WEB_TOKEN": "has space in it...."}, {"WEB_PORT": "70000"}]:
    try: m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", **bad}); assert False, bad
    except ValueError: pass

async def request(port, raw):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(raw); await w.drain()
    data = await r.read(); w.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), head.decode(), body

async def layout_check(browser, page):
    """Starred cards move by dragging ⠿ (mouse, pen or touch); 自定义 reorders, hides and restores any card or section.
    Everything is kept in this browser's localStorage, like the stars."""
    names = lambda g: page.eval_on_selector_all(f"#g-{g} .card .name", "els => els.map(e => e.textContent)")
    stored = lambda k: page.evaluate(f"localStorage.getItem('{k}')")
    async def box(sel):
        b = await page.locator(sel).bounding_box()
        return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2, b
    for g in ["contract", "ladder", "crypto", "crypto"]:
        await page.click(f"#g-{g} .card .star")  # each click scrolls its card into view
    await page.evaluate("scrollTo(0, 0)")  # the starred cards sit on top: drag them where the pointer can reach
    assert await names("fav") == ["宇树 UNITREE", "$牛来 市值", "BNB 先触 700/900", "SOL 先触 60/140"]
    assert await page.locator("#g-fav .card .grip").count() == 4 and await page.locator("#g-crypto .grip").count() == 0
    # a short card held over the upper part of a tall one stays put (no flip-flop); past its middle it moves
    x, y, _ = await box("#g-fav .card:nth-child(1) .grip")
    _, _, tall = await box("#g-fav .card:nth-child(2)")
    await page.mouse.move(x, y); await page.mouse.down()
    orders = set()
    for i in range(1, 13):
        await page.mouse.move(x, y + (tall["y"] + tall["height"] * 0.3 - y) * i / 12)
        orders.add(tuple(await names("fav")))
    assert len(orders) == 1, orders
    await page.mouse.move(x, tall["y"] + tall["height"] * 0.75, steps=4)
    assert await names("fav") == ["$牛来 市值", "宇树 UNITREE", "BNB 先触 700/900", "SOL 先触 60/140"]
    await page.mouse.up()
    assert await stored("favs") == '["NIULAI","UNITREEUSDT","BNBUSDT","SOLUSDT"]'
    # up past several cards; the 10-second refresh landing mid-drag waits for the drop instead of rebuilding the cards
    x, y, _ = await box("#g-fav .card:nth-child(4) .grip")
    _, _, first = await box("#g-fav .card:nth-child(1)")
    await page.mouse.move(x, y); await page.mouse.down()
    await page.mouse.move(x, first["y"] + 8, steps=15)
    dragged = await page.query_selector("#g-fav .card.dragging")
    await page.evaluate("load()")
    assert await dragged.evaluate("e => e.isConnected") and await page.evaluate("pending !== null")
    await page.mouse.up()
    assert await page.evaluate("pending === null && drag === null") and await page.locator(".card.dragging").count() == 0
    order = ["SOL 先触 60/140", "$牛来 市值", "宇树 UNITREE", "BNB 先触 700/900"]
    assert await names("fav") == order and await stored("favs") == '["SOLUSDT","NIULAI","UNITREEUSDT","BNBUSDT"]'
    await page.reload(); await page.wait_for_selector("#g-fav .card")
    assert await names("fav") == order
    # held near the bottom edge, the page scrolls on by itself; a drag the window loses ends instead of hanging
    await page.set_viewport_size({"width": 390, "height": 520}); await page.evaluate("scrollTo(0, 0)")
    x, y, _ = await box("#g-fav .card:nth-child(1) .grip")
    await page.mouse.move(x, y); await page.mouse.down(); await page.mouse.move(x, 512, steps=5)
    await page.wait_for_timeout(400)
    assert await page.evaluate("scrollY") > 50
    await page.evaluate("dispatchEvent(new Event('blur'))")
    assert await page.evaluate("drag === null") and await page.locator(".card.dragging").count() == 0
    await page.mouse.up(); await page.set_viewport_size({"width": 390, "height": 900})
    # touch: the grip takes the finger without scrolling the page; the rest of a card still scrolls it
    ctx = await browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True)
    phone = await ctx.new_page(); await phone.goto(page.url); await phone.wait_for_selector(".card .odds")
    for _ in range(3):
        await phone.tap("#g-crypto .card .star")
    cdp = await ctx.new_cdp_session(phone)
    async def touch(kind, x=0, y=0):
        await cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": [] if kind == "touchEnd" else [{"x": x, "y": y}]})
        await phone.wait_for_timeout(10)
    g = await phone.locator("#g-fav .card:nth-child(1) .grip").bounding_box()
    t = await phone.locator("#g-fav .card:nth-child(3)").bounding_box()
    x, y = g["x"] + g["width"] / 2, g["y"] + g["height"] / 2
    await touch("touchStart", x, y)
    for i in range(1, 13):
        await touch("touchMove", x, y + (t["y"] + t["height"] * 0.8 - y) * i / 12)
    await touch("touchEnd")
    assert await phone.evaluate("scrollY") == 0 and await phone.evaluate("localStorage.getItem('favs')") == '["SOLUSDT","BTCUSDT","BNBUSDT"]'
    b = await phone.locator("#g-index .card .odds").bounding_box()
    await touch("touchStart", b["x"] + 20, b["y"] + 5)
    for i in range(1, 9):
        await touch("touchMove", b["x"] + 20, b["y"] + 5 - 25 * i)
    await touch("touchEnd"); await phone.wait_for_timeout(200)
    assert await phone.evaluate("scrollY") > 0
    await ctx.close()

    # ✎ 自定义: every card gets ⠿ ◀ ▶ and 隐藏; whole sections can be switched off
    await page.evaluate("localStorage.removeItem('favs')"); await page.reload(); await page.wait_for_selector(".card .odds")
    assert not await page.is_visible("#custom") and await page.locator(".ctl").count() == 0
    await page.click("#edit")
    assert await page.is_visible("#custom") and await page.inner_text("#edit") == "✓ 完成" and await page.locator(".card .ctl").count() == 10
    lad = "#g-ladder .card"
    assert await page.is_disabled(f"{lad}:nth-child(1) .ctl button[title='前移']") and await page.is_disabled(f"{lad}:nth-child(3) .ctl button[title='后移']")
    await page.click(f"{lad}:nth-child(1) .ctl button[title='后移']")
    assert await names("ladder") == ["$ANSEM FDV", "$牛来 市值", "$PONS FDV"]
    assert json.loads(await stored("order")) == {"ladder": ["ANSEM", "NIULAI", "PONS"]}
    await page.click("#g-crypto .card:nth-child(4) .ctl button[title='前移']")
    crypto = ["BNB 先触 700/900", "SOL 先触 60/140", "ETH 先触 1k/3k", "BTC 先触 70k/90k", "BTC 10月涨跌"]
    assert await names("crypto") == crypto
    # hidden cards and sections show faded while customising and are gone once done, also after a reload
    await page.click("#g-index .card .ctl button:has-text('隐藏')")
    await page.click("#g-contract .card .ctl button:has-text('隐藏')")
    assert await page.locator(".card.off").count() == 2 and "已隐藏 2 张卡片" in await page.inner_text("#hidn")
    await page.uncheck("#secs input[data-sec=ladder]")
    assert await page.inner_text("#h-ladder .hn") == "市值阶梯（已隐藏）" and await page.is_visible("#g-ladder.off .card")
    await page.click("#done")
    assert not await page.is_visible("#custom") and await page.locator(".ctl").count() == 0 and await page.inner_text("#edit") == "✎ 自定义"
    for g in ["index", "contract", "ladder"]:
        assert not await page.is_visible(f"#h-{g}") and not await page.is_visible(f"#g-{g}"), g
    await page.reload(); await page.wait_for_selector("#g-crypto .card")
    assert not await page.is_visible("#h-index") and not await page.is_visible("#h-ladder") and await names("crypto") == crypto
    assert [await stored(k) for k in ("hidden", "hideSec")] == ['["上证指数","UNITREEUSDT"]', '["ladder"]']
    # bringing them back: one card by its own button, the rest with 全部显示, the section by its tick box
    await page.click("#edit")
    assert await names("ladder") == ["$ANSEM FDV", "$牛来 市值", "$PONS FDV"]
    await page.click("#g-contract .card .ctl button:has-text('显示')")
    assert await page.locator(".card.off").count() == 1 and await stored("hidden") == '["上证指数"]'
    await page.click("#showall")
    assert await page.locator(".card.off").count() == 0 and await page.is_disabled("#showall")
    await page.check("#secs input[data-sec=ladder]")
    assert await page.inner_text("#h-ladder .hn") == "市值阶梯" and await stored("hideSec") == "[]"
    # ↑ ↓ beside a section title move the whole section (the tick boxes follow); sections without cards are skipped
    heads = lambda: page.eval_on_selector_all(".wrap > h2:not([hidden])", "els => els.map(e => e.id.slice(2))")
    assert await heads() == ["index", "contract", "crypto", "ladder"]
    assert await page.is_disabled("#h-index button[title='栏目上移']") and await page.is_disabled("#h-ladder button[title='栏目下移']")
    await page.click("#h-crypto button[title='栏目上移']"); await page.click("#h-crypto button[title='栏目上移']")
    assert await heads() == ["crypto", "index", "contract", "ladder"] and await stored("secs") == '["crypto","index","contract","ladder"]'
    assert await page.eval_on_selector_all("#secs input", "els => els.map(e => e.dataset.sec)") == ["crypto", "index", "contract", "ladder"]
    await page.click("#g-contract .card .star")  # 合约标的 is now empty: ↓ on 指数 goes straight past it
    await page.click("#h-index button[title='栏目下移']")
    assert await heads() == ["fav", "crypto", "ladder", "index"], await heads()
    assert await stored("secs") == '["crypto","contract","ladder","index"]', await stored("secs")
    await page.click("#g-fav .card .star")
    await page.reload(); await page.wait_for_selector("#g-crypto .card"); await page.click("#edit")
    assert await heads() == ["crypto", "contract", "ladder", "index"]
    # 恢复默认布局 takes a second click within 4 s and leaves the stars alone
    await page.click("#g-crypto .card:nth-child(4) .star")
    await page.click("#reset")
    assert await page.inner_text("#reset") == "再点一次确认" and await names("ladder") == ["$ANSEM FDV", "$牛来 市值", "$PONS FDV"]
    await page.evaluate("armed = Date.now() - 5000")  # the 4 seconds ran out: the next click only asks again
    await page.click("#reset")
    assert await page.inner_text("#reset") == "再点一次确认" and await stored("order") is not None
    await page.click("#reset")
    assert await page.inner_text("#reset") == "恢复默认布局" and await heads() == ["fav", "index", "contract", "crypto", "ladder"]
    assert await names("ladder") == ["$牛来 市值", "$ANSEM FDV", "$PONS FDV"] and await names("crypto") == [*crypto[:3], crypto[4]]
    assert await stored("order") is None and await stored("secs") is None and await stored("favs") == '["BTCUSDT"]'
    assert await names("fav") == ["BTC 先触 70k/90k"]
    await page.click("#done"); await page.click("#g-fav .card .star")
    assert not await page.is_visible("#h-fav") and await names("crypto") == ["BNB 先触 700/900", "SOL 先触 60/140", "BTC 先触 70k/90k",
                                                                            "ETH 先触 1k/3k", "BTC 10月涨跌"]


async def browser_check(async_playwright, chrome, port, token):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**({"executable_path": chrome} if chrome else {}))
        page = await browser.new_page(viewport={"width": 390, "height": 900})
        await page.goto(f"http://127.0.0.1:{port}/p/{token}")
        await page.wait_for_selector(".card .odds")
        text = await page.inner_text(".wrap")
        assert "上证指数" in text and "10-08 下周四" in text and "涨35.0¢" in text and "概率暂缺：等待行情" in text, text
        # the full close time sits on the countdown's tooltip (and in the details), not as another line
        assert await page.get_attribute(".cd", "title") == "目标 10-08 15:00 上交所收盘（北京时间）"
        assert "09-30" in text and "目标" not in await page.inner_text("#g-index .card"), text
        cd = await page.inner_text(".cd")
        # server clock is 09-30 22:05 and the close is 10-08 15:00 → 7 days 16:55 left (ticking down)
        assert cd.startswith("⏳ 7天 16:5"), cd
        await page.wait_for_timeout(2300)
        assert await page.inner_text(".cd") != cd, "countdown must tick"
        assert "数据 09-30 22:05:00" in await page.inner_text("#meta") and "秒前刷新" in await page.inner_text("#meta")
        assert await page.inner_text("#h-index") == "指数" and await page.is_visible("#h-contract")
        assert await page.get_attribute("#g-contract .name", "title") == "UNITREEUSDT" and "上证指数" in await page.inner_text("#g-index")
        await page.click("#g-index details summary"); assert await page.is_visible("#g-index dl")
        await page.wait_for_timeout(10500)  # survives one data refresh
        assert await page.is_visible("#g-index dl"), "open details must stay open across refresh"
        # ☆ moves a card into the starred section on top; the choice survives a reload; ★ puts it back
        assert not await page.is_visible("#h-fav") and await page.locator("#g-contract .card").count() == 1
        await page.click("#g-contract .card .star")
        assert await page.is_visible("#h-fav") and await page.inner_text("#g-fav .name") == "宇树 UNITREE"
        assert await page.locator("#g-contract .card").count() == 0 and await page.inner_text("#g-fav .star") == "★"
        assert await page.evaluate("document.getElementById('g-fav').compareDocumentPosition(document.getElementById('g-index')) & 4")
        await page.reload(); await page.wait_for_selector("#g-fav .card")
        assert await page.inner_text("#g-fav .name") == "宇树 UNITREE" and not await page.is_visible("#h-contract")
        assert await page.evaluate("localStorage.getItem('favs')") == '["UNITREEUSDT"]'  # stored locally, by ticker
        # stars saved by the earlier build ("name|symbol") are carried over
        await page.evaluate("localStorage.setItem('favs', JSON.stringify(['宇树 UNITREE|UNITREEUSDT', '上证指数|']))")
        await page.reload(); await page.wait_for_selector("#g-fav .card")
        assert await page.locator("#g-fav .card").count() == 2 and await page.locator("#g-index .card").count() == 0
        await page.click("#g-fav .card:nth-child(2) .star")
        await page.click("#g-fav .card .star")
        assert not await page.is_visible("#h-fav") and await page.locator("#g-contract .card").count() == 1
        await layout_check(browser, page)
        if os.environ.get("WEB_SCREENSHOT"):
            await page.screenshot(path=os.environ["WEB_SCREENSHOT"], full_page=True)
        await browser.close()


async def run():
    class FakeTelegram:
        def __init__(self): self.sent = []
        async def call(self, *a, **k): return True
        async def send(self, chat, thread, text, reply_markup=None, parse_mode=None): self.sent.append(text)
    class FakeMarket:
        def __init__(self): self.config = None
        def now_ms(self): return int(dt.datetime(2026, 9, 30, 22, 5, tzinfo=m.BEIJING).timestamp() * 1000)
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ADMIN_USER_ID": "42", "SYMBOLS": "UNITREEUSDT", "WEB_PORT": "0",
                             "HSI_FUTURES": "off", "KOSPI_INDEX": "off"})
    # WEB_PORT=0 means disabled; force an ephemeral port by constructing the server directly
    store = m.Store(":memory:"); tg = FakeTelegram(); bot = m.Bot(cfg, store, FakeMarket(), tg)
    assert bot.web_token == "" and "网页未开启" in bot.cmd_web(None)
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ADMIN_USER_ID": "42", "SYMBOLS": "UNITREEUSDT", "WEB_PORT": "18080",
                             "HSI_FUTURES": "off", "KOSPI_INDEX": "off"})
    bot = m.Bot(cfg, store, FakeMarket(), tg)
    token = bot.web_token; assert len(token) >= 20 and store.get("web_token") == token
    assert m.Bot(cfg, store, FakeMarket(), tg).web_token == token  # persisted across restarts
    assert "还没有公网域名" in bot.cmd_web(None) and f"/p/{token}" in bot.cmd_web(None)
    bot.config = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ADMIN_USER_ID": "42", "SYMBOLS": "UNITREEUSDT", "WEB_PORT": "18080",
                                    "HSI_FUTURES": "off", "KOSPI_INDEX": "off", "RAILWAY_PUBLIC_DOMAIN": "bot.up.railway.app"})
    assert bot.cmd_web(None).endswith(f"https://bot.up.railway.app/p/{token}")
    # data: SSE odds + one contract missing
    bot.cn.quote = m.IndexQuote("上证指数", D("3850.12"), D("3830"), None, None, None, int(dt.datetime(2026, 9, 30, 15, 0, tzinfo=m.BEIJING).timestamp() * 1000), "腾讯")
    bot.cn.a50 = m.IndexQuote("A50期货", D("14020"), D("14100"), None, None, None, bot.market.now_ms(), "东方财富")
    bot.cn.close = m.DailyClose(dt.date(2026, 9, 30), D("3850.12"), D("3830"), "腾讯日K", 0)
    bot.anchors["A50"] = (bot.sse_close_ms(), D("14160"))
    payload = bot.odds_payload()
    names = [i["name"] for i in payload["items"]]
    assert names == ["上证指数", "宇树 UNITREE", "BNB 先触 700/900", "SOL 先触 60/140", "BTC 先触 70k/90k", "ETH 先触 1k/3k", "BTC 10月涨跌",
                     "$牛来 市值", "$ANSEM FDV", "$PONS FDV"], names
    bnb = payload["items"][2]
    assert bnb["group"] == "crypto" and bnb["labels"] == ["$900", "$700"] and bnb["missing"].startswith("等待币安行情"), bnb
    assert payload["items"][0]["group"] == "index" and payload["items"][1]["symbol"] == "UNITREEUSDT" and payload["items"][1]["group"] == "contract"
    sse = payload["items"][0]
    assert sse["target"] == "10-08" and 0 < sse["fair_up"] < 0.5 and abs(sse["up"] + sse["flat"] + sse["down"] - 1) < 1e-9 and sse["ref"] == "3,850.12"
    uni = payload["items"][1]
    assert {k: uni[k] for k in ("name", "symbol", "group", "missing")} == {"name": "宇树 UNITREE", "symbol": "UNITREEUSDT", "group": "contract", "missing": "等待行情"} and payload["color_style"] == "cn"
    # every card says which trading day it prices, relative to Beijing today
    assert sse["day"] == "2026-10-08" and sse["day_label"] == "10-08 周四" and sse["ref_day"] == "09-30", sse
    assert uni["day_label"].endswith(("周一", "周二", "周三", "周四", "周五")) and uni["day_ahead"] >= 0 and payload["today"], (uni, payload["today"])
    now = int(dt.datetime(2026, 9, 28, 15, 5, tzinfo=m.BEIJING).timestamp() * 1000)
    assert m.day_fields(dt.date(2026, 9, 28), now)["day_tag"] == "今天" and m.day_fields(dt.date(2026, 9, 29), now)["day_tag"] == "明天"
    assert m.day_fields(dt.date(2026, 9, 30), now)["day_tag"] == "后天" and m.day_fields(dt.date(2026, 10, 2), now)["day_tag"] == "本周五"
    assert m.day_fields(dt.date(2026, 10, 8), now)["day_tag"] == "下周四" and m.day_fields(dt.date(2026, 10, 8), now)["day_ahead"] == 10
    assert sse["close_ms"] == int(dt.datetime(2026, 10, 8, 15, 0, tzinfo=m.BEIJING).timestamp() * 1000) and sse["close_label"] == "10-08 15:00 上交所收盘（北京时间）"
    assert payload["server_ms"] == bot.market.now_ms()
    kst = dt.timezone(dt.timedelta(hours=9))
    assert bot.target_close("KOSPI", dt.date(2026, 9, 28)) == (int(dt.datetime(2026, 9, 28, 15, 30, tzinfo=kst).timestamp() * 1000), "09-28 14:30 韩交所收盘（北京时间，韩国 15:30）")
    assert bot.target_close("宇树 UNITREE｜UNITREEUSDT", dt.date(2026, 9, 28))[1] == "09-28 15:00 上交所收盘（北京时间）"
    assert bot.target_close("恒生指数", dt.date(2026, 9, 28))[1] == "09-28 16:10 港交所收盘（北京时间）"
    json.dumps(payload)  # serialisable
    # real HTTP
    web = m.WebServer(bot, 0, token); port = await web.start()
    st, head, body = await request(port, b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n"); assert st == 200 and body == b"ok"
    st, head, body = await request(port, f"GET /p/{token} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert st == 200 and b"<title>" in body and "text/html" in head and "Cache-Control: no-store" in head and "Referrer-Policy: no-referrer" in head
    st, head, body = await request(port, f"GET /p/{token}/data.json?x=1 HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert st == 200 and json.loads(body)["items"][0]["name"] == "上证指数" and "application/json" in head
    st, _, body = await request(port, f"HEAD /p/{token} HTTP/1.1\r\n\r\n".encode()); assert st == 200 and body == b""
    for raw in [b"GET /p/wrongtokenwrongtoken HTTP/1.1\r\n\r\n", f"GET /p/{token}/other HTTP/1.1\r\n\r\n".encode(), b"GET /etc/passwd HTTP/1.1\r\n\r\n"]:
        st, _, _ = await request(port, raw); assert st == 404, raw
    st, _, _ = await request(port, f"POST /p/{token} HTTP/1.1\r\n\r\n".encode()); assert st == 405
    st, _, _ = await request(port, b"GARBAGE\r\n\r\n"); assert st == 400
    # a payload crash returns 500 without killing the server
    orig = bot.odds_payload; bot.odds_payload = lambda: 1 / 0
    st, _, _ = await request(port, f"GET /p/{token}/data.json HTTP/1.1\r\n\r\n".encode()); assert st == 500
    bot.odds_payload = orig
    st, _, _ = await request(port, b"GET /health HTTP/1.1\r\n\r\n"); assert st == 200
    # /web via Telegram command
    tg.sent.clear(); await bot.process_message({"text": "/web", "chat": {"id": 1}, "from": {"id": 42}, "date": time.time()})
    assert f"https://bot.up.railway.app/p/{token}" in tg.sent[-1]
    # headless browser render (optional: skipped when Playwright/Chromium is not installed, e.g. in the Docker build)
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        async_playwright = None
    chrome = next((p for p in [os.environ.get("CHROMIUM_PATH", ""), "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"]
                   if p and os.path.exists(p)), "")
    if async_playwright is None or not (chrome or os.environ.get("PLAYWRIGHT_BROWSERS_PATH")):
        print("browser check skipped (no Playwright/Chromium)")
    else:
        await browser_check(async_playwright, chrome, port, token)
    await web.stop()
    print("WEB_OK")
asyncio.run(run())
