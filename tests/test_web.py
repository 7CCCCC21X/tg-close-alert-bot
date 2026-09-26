import asyncio, os, sys, time, json, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
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

async def browser_check(async_playwright, chrome, port, token):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**({"executable_path": chrome} if chrome else {}))
        page = await browser.new_page(viewport={"width": 390, "height": 900})
        await page.goto(f"http://127.0.0.1:{port}/p/{token}")
        await page.wait_for_selector(".card .odds")
        text = await page.inner_text(".wrap")
        assert "上证指数" in text and "目标 10-08 15:00 上交所收盘（北京时间）" in text and "涨35.0¢" in text and "概率暂缺：等待行情" in text, text
        cd = await page.inner_text(".cd")
        # server clock is 09-30 22:05 and the close is 10-08 15:00 → 7 days 16:55 left (ticking down)
        assert cd.startswith("⏳ 7天 16:5"), cd
        await page.wait_for_timeout(2300)
        assert await page.inner_text(".cd") != cd, "countdown must tick"
        assert "数据 09-30 22:05:00" in await page.inner_text("#meta") and "秒前刷新" in await page.inner_text("#meta")
        assert await page.inner_text("#h-index") == "指数" and await page.is_visible("#h-contract")
        assert "UNITREEUSDT" in await page.inner_text("#g-contract") and "上证指数" in await page.inner_text("#g-index")
        await page.click("#g-index details summary"); assert await page.is_visible("#g-index dl")
        await page.wait_for_timeout(10500)  # survives one data refresh
        assert await page.is_visible("#g-index dl"), "open details must stay open across refresh"
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
    assert names == ["上证指数", "宇树 UNITREE"], names
    assert payload["items"][0]["group"] == "index" and payload["items"][1]["symbol"] == "UNITREEUSDT" and payload["items"][1]["group"] == "contract"
    sse = payload["items"][0]
    assert sse["target"] == "10-08" and 0 < sse["fair_up"] < 0.5 and abs(sse["up"] + sse["flat"] + sse["down"] - 1) < 1e-9 and sse["ref"] == "3,850.12"
    assert payload["items"][1] == {"name": "宇树 UNITREE", "symbol": "UNITREEUSDT", "group": "contract", "missing": "等待行情"} and payload["color_style"] == "cn"
    assert sse["close_ms"] == int(dt.datetime(2026, 10, 8, 15, 0, tzinfo=m.BEIJING).timestamp() * 1000) and sse["close_label"] == "10-08 15:00 上交所收盘（北京时间）"
    assert payload["server_ms"] == bot.market.now_ms()
    kst = dt.timezone(dt.timedelta(hours=9))
    assert bot.target_close("KOSPI", dt.date(2026, 9, 28)) == (int(dt.datetime(2026, 9, 28, 15, 30, tzinfo=kst).timestamp() * 1000), "09-28 15:30 韩交所收盘（韩国时间）")
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
