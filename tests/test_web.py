import asyncio, gzip, os, sys, time, json, datetime as dt
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
        await phone.tap("#g-crypto .card .star")  # each tap scrolls its star into view; the drag below starts from the top
    await phone.evaluate("scrollTo(0, 0)"); await phone.wait_for_timeout(100)
    cdp = await ctx.new_cdp_session(phone)
    async def touch(kind, x=0, y=0):
        await cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": [] if kind == "touchEnd" else [{"x": x, "y": y}]})
        await phone.wait_for_timeout(10)
    g = await phone.locator("#g-fav .card:nth-child(1) .grip").bounding_box()
    t = await phone.locator("#g-fav .card:nth-child(3)").bounding_box()
    x, y = g["x"] + g["width"] / 2, g["y"] + g["height"] / 2
    y0 = await phone.evaluate("scrollY")  # wherever the taps left the page: the drag itself must not scroll it
    await touch("touchStart", x, y)
    for i in range(1, 13):
        await touch("touchMove", x, y + (t["y"] + t["height"] * 0.8 - y) * i / 12)
    await touch("touchEnd")
    assert await phone.evaluate("scrollY") == y0 and await phone.evaluate("localStorage.getItem('favs')") == '["SOLUSDT","BTCUSDT","BNBUSDT"]', (y0, await phone.evaluate("scrollY"), await phone.evaluate("localStorage.getItem('favs')"), g, t)
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
    assert await page.is_visible("#custom") and await page.inner_text("#edit") == "✓ 完成" and await page.locator(".card .ctl").count() == 23
    lad = "#g-ladder .card"
    assert await page.is_disabled(f"{lad}:nth-child(1) .ctl button[title='前移']") and await page.is_disabled(f"{lad}:nth-child(8) .ctl button[title='后移']")
    await page.click(f"{lad}:nth-child(1) .ctl button[title='后移']")
    assert await names("ladder") == ["$ANSEM FDV", "$牛来 市值", "$PONS FDV", "$MEME FDV", "$CASHCAT FDV", "$AI FDV", "$STONK FDV", "$STONKBROKER FDV"]
    assert json.loads(await stored("order")) == {"ladder": ["ANSEM", "NIULAI", "PONS", "MEME", "CASHCAT", "AI", "STONK", "STONKBROKER"]}
    await page.click("#g-crypto .card:nth-child(4) .ctl button[title='前移']")
    crypto = ["BNB 先触 700/900", "SOL 先触 60/140", "ETH 先触 1k/3k", "BTC 先触 70k/90k", "BTC 10月涨跌", "ETH 10月涨跌", "HYPE 反超 SOL"]
    assert await names("crypto") == crypto
    # hidden cards and sections show faded while customising and are gone once done, also after a reload
    await page.click("#g-index .card .ctl button:has-text('隐藏')")
    await page.click("#g-contract .card .ctl button:has-text('隐藏')")
    assert await page.locator(".card.off").count() == 2 and "已隐藏 2 张卡片" in await page.inner_text("#hidn")
    # the panel's own checklist shows the same state and hides / shows a card too
    assert await page.eval_on_selector_all("#cards input:not(:checked)", "els => els.map(e => e.dataset.card)") == ["上证指数", "UNITREEUSDT"]
    assert await page.eval_on_selector_all("#cards .cgrp > .mut", "els => els.map(e => e.textContent)") == ["指数：", "合约标的：", "加密：", "价格阶梯：", "市值阶梯：", "模拟交易："]
    await page.uncheck("#cards input[data-card=BNBUSDT]")
    assert await page.locator(".card.off").count() == 3 and await stored("hidden") == '["上证指数","UNITREEUSDT","BNBUSDT"]'
    assert await page.inner_text("#g-crypto .card:nth-child(1) .ctl button:nth-last-child(1)") == "显示"
    await page.check("#cards input[data-card=BNBUSDT]")
    assert await page.locator(".card.off").count() == 2 and await stored("hidden") == '["上证指数","UNITREEUSDT"]'
    # 整行显示: a ladder section can put one card per row (the wide table of the starred and filtered views); kept across reloads
    await page.set_viewport_size({"width": 1300, "height": 900})
    gw = lambda sel: page.evaluate(f"document.querySelector('{sel}').getBoundingClientRect().width")
    wide = lambda g: page.evaluate(f"document.getElementById('g-{g}').classList.contains('wide')")
    assert await wide("ladder") and await gw("#g-ladder .card") < await gw("#g-ladder") / 2
    hs = lambda: page.eval_on_selector_all("#g-ladder .card", "els => els.map(e => Math.round(e.getBoundingClientRect().height))")
    assert len(await hs()) == 8 and len(set(await hs())) == 1 and 0 < (await hs())[0] <= 460, await hs()  # two-up: one height for every cap card
    await page.check("#onerow input[data-row=ladder]")
    assert len(set(await hs())) > 1, await hs()  # one per row: each card its own height again
    assert not await wide("ladder") and await wide("levels") and abs(await gw("#g-ladder .card") - await gw("#g-ladder")) < 1
    assert await stored("oneRow") == '["ladder"]'
    await page.reload(); await page.wait_for_selector("#g-crypto .card")
    assert not await wide("ladder") and abs(await gw("#g-ladder .card") - await gw("#g-ladder")) < 1
    await page.click("#edit"); await page.uncheck("#onerow input[data-row=ladder]")
    assert await wide("ladder") and await gw("#g-ladder .card") < await gw("#g-ladder") / 2 and await stored("oneRow") == "[]"
    await page.set_viewport_size({"width": 390, "height": 900})
    await page.uncheck("#secs input[data-sec=ladder]")
    assert await page.inner_text("#h-ladder .hn") == "市值阶梯（已隐藏）" and await page.is_visible("#g-ladder.off .card")
    await page.click("#done")
    assert not await page.is_visible("#custom") and await page.locator(".ctl").count() == 0 and await page.inner_text("#edit") == "✎ 自定义"
    for g in ["index", "contract", "ladder", "sim"]:  # 模拟交易 is hidden by default (its review page has its own link)
        assert not await page.is_visible(f"#h-{g}") and not await page.is_visible(f"#g-{g}"), g
    await page.reload(); await page.wait_for_selector("#g-crypto .card")
    assert not await page.is_visible("#h-index") and not await page.is_visible("#h-ladder") and await names("crypto") == crypto
    assert [await stored(k) for k in ("hidden", "hideSec")] == ['["上证指数","UNITREEUSDT"]', '["sim","ladder"]']
    # bringing them back: one card by its own button, the rest with 全部显示, the section by its tick box
    await page.click("#edit")
    assert await names("ladder") == ["$ANSEM FDV", "$牛来 市值", "$PONS FDV", "$MEME FDV", "$CASHCAT FDV", "$AI FDV", "$STONK FDV", "$STONKBROKER FDV"]
    await page.click("#g-contract .card .ctl button:has-text('显示')")
    assert await page.locator(".card.off").count() == 1 and await stored("hidden") == '["上证指数"]'
    await page.click("#showall")
    assert await page.locator(".card.off").count() == 0 and await page.is_disabled("#showall")
    await page.check("#secs input[data-sec=ladder]")
    assert await page.inner_text("#h-ladder .hn") == "市值阶梯" and await stored("hideSec") == '["sim"]'
    # ↑ ↓ beside a section title move the whole section (the tick boxes follow); sections without cards are skipped
    heads = lambda: page.eval_on_selector_all(".wrap > h2:not([hidden])", "els => els.map(e => e.id.slice(2))")
    assert await heads() == ["index", "contract", "crypto", "levels", "ladder", "sim"]
    assert await page.is_disabled("#h-index button[title='栏目上移']") and await page.is_disabled("#h-sim button[title='栏目下移']")
    await page.click("#h-crypto button[title='栏目上移']"); await page.click("#h-crypto button[title='栏目上移']")
    assert await heads() == ["crypto", "index", "contract", "levels", "ladder", "sim"]
    assert await stored("secs") == '["crypto","index","contract","levels","ladder","sim"]'
    assert await page.eval_on_selector_all("#secs input", "els => els.map(e => e.dataset.sec)") == ["crypto", "index", "contract", "levels", "ladder", "sim"]
    await page.click("#g-contract .card .star")  # 合约标的 is now empty: ↓ on 指数 goes straight past it
    await page.click("#h-index button[title='栏目下移']")
    assert await heads() == ["fav", "crypto", "levels", "index", "ladder", "sim"], await heads()
    assert await stored("secs") == '["crypto","contract","levels","index","ladder","sim"]', await stored("secs")
    await page.click("#g-fav .card .star")
    await page.reload(); await page.wait_for_selector("#g-crypto .card"); await page.click("#edit")
    assert await heads() == ["crypto", "contract", "levels", "index", "ladder", "sim"]
    # a section title dragged by its ⠿ past the section below moves the whole section (kept, like the ↑ ↓ moves)
    await page.set_viewport_size({"width": 1300, "height": 2200})
    await page.evaluate("document.getElementById('h-contract').scrollIntoView({block: 'center'})")  # clear of the sticky filter bar
    x, y, _ = await box("#h-contract .grip")
    _, _, lv = await box("#g-levels")
    await page.mouse.move(x, y); await page.mouse.down()
    await page.mouse.move(x, lv["y"] + lv["height"] - 4, steps=12)
    assert await heads() == ["crypto", "levels", "contract", "index", "ladder", "sim"], await heads()
    await page.mouse.up()
    assert await page.evaluate("drag === null") and await stored("secs") == '["crypto","levels","contract","index","ladder","sim"]'
    assert await page.eval_on_selector_all("#secs input", "els => els.map(e => e.dataset.sec)") == ["crypto", "levels", "contract", "index", "ladder", "sim"]
    await page.set_viewport_size({"width": 390, "height": 900})
    # 恢复默认布局 takes a second click within 4 s and leaves the stars alone (folded sections open again too)
    await page.evaluate("keep('folded', ['index']); keep('oneRow', ['ladder'])")
    await page.click("#g-crypto .card:nth-child(4) .star")
    await page.click("#reset")
    assert await page.inner_text("#reset") == "再点一次确认" and await names("ladder") == ["$ANSEM FDV", "$牛来 市值", "$PONS FDV", "$MEME FDV", "$CASHCAT FDV", "$AI FDV", "$STONK FDV", "$STONKBROKER FDV"]
    await page.evaluate("armed = Date.now() - 5000")  # the 4 seconds ran out: the next click only asks again
    await page.click("#reset")
    assert await page.inner_text("#reset") == "再点一次确认" and await stored("order") is not None
    await page.click("#reset")
    assert await page.inner_text("#reset") == "恢复默认布局" and await heads() == ["fav", "index", "contract", "crypto", "levels", "ladder", "sim"]
    assert await stored("oneRow") is None and await page.evaluate("document.getElementById('g-ladder').classList.contains('wide')")
    assert await names("ladder") == ["$牛来 市值", "$ANSEM FDV", "$PONS FDV", "$MEME FDV", "$CASHCAT FDV", "$AI FDV", "$STONK FDV", "$STONKBROKER FDV"] and await names("crypto") == [*crypto[:3], *crypto[4:]]
    assert await stored("order") is None and await stored("secs") is None and await stored("favs") == '["BTCUSDT"]'
    assert await stored("folded") is None
    assert await names("fav") == ["BTC 先触 70k/90k"]
    await page.click("#done"); await page.click("#g-fav .card .star")
    assert not await page.is_visible("#h-fav") and await names("crypto") == ["BNB 先触 700/900", "SOL 先触 60/140", "BTC 先触 70k/90k",
                                                                            "ETH 先触 1k/3k", "BTC 10月涨跌", "ETH 10月涨跌", "HYPE 反超 SOL"]


async def until(page, expr, tries=200):
    """Poll ``expr`` (a JS expression) until it is true: wait_for_function would eval a string, which the page's CSP forbids."""
    for _ in range(tries):
        if await page.evaluate(expr):
            return
        await page.wait_for_timeout(50)
    raise AssertionError(f"timed out waiting for {expr}")


async def watch_check(bot, page):
    """The chime (🔔): a short beep when an opportunity enters the strip that was not there, for every one or only the
    watched markets, kept in this browser; the watched markets (自选) and the Telegram limit are the bot's, changed from
    自定义 through POST /watch and shared with /watch."""
    await page.add_init_script("window.__beeps=0;window.__plays=0;window.__notes=[];class FakeCtx{constructor(){this.state='suspended';this.currentTime=0;this.destination={}}"
                               "resume(){this.state='running'}createGain(){return{gain:{setValueAtTime(){},exponentialRampToValueAtTime(){}},connect(x){return x}}}"
                               "createOscillator(){window.__beeps++;return{frequency:{},connect(g){return g},start(){},stop(){}}}"
                               "decodeAudioData(buf,ok){ok({duration:.3})}createBufferSource(){window.__plays++;return{connect(g){return g},start(){}}}}window.AudioContext=FakeCtx;"
                               "localStorage.setItem('soundLen','\"short\"');"  # one pass per beep: the note counts below
                               "window.__asked=0;window.__shown=[];class FakeNote{constructor(t,o){window.__shown.push([t,o.body])}close(){}}FakeNote.permission='default';"
                               "FakeNote.requestPermission=()=>{window.__asked++;FakeNote.permission='granted';return Promise.resolve('granted')};window.Notification=FakeNote")
    await page.reload(); await page.wait_for_selector(".card .odds")
    await page.click("#edit")
    assert (await page.inner_text("#sound")).startswith("🔔 声音提醒：\n开启\n全部机会都响\n提示音") and not await page.is_checked("#snd-on") and await page.is_checked("#snd-all")
    # the tone, volume and length: built-in patterns (so many notes each), the viewer's own clip, 试听
    assert await page.eval_on_selector_all("#snd-kind option", "o => o.map(x => x.value)") == ["ding", "triple", "beep", "alarm", "bell"]  # no clip yet: no 自定义文件
    assert await page.input_value("#snd-vol") == "70" and await page.input_value("#snd-len") == "short" and await page.input_value("#snd-kind") == "ding"
    await page.click("#snd-test"); assert await page.evaluate("window.__beeps") == 2
    await page.select_option("#snd-kind", "triple"); assert await page.evaluate("window.__beeps") == 5 and await page.evaluate("localStorage.getItem('soundKind')") == '"triple"'  # it plays once picked
    await page.select_option("#snd-len", "long"); assert await page.evaluate("window.__beeps") == 5 + 3 * 7  # 三连音 spans 0.44 s + 0.3 s gap: 7 passes fill 5 s
    await page.select_option("#snd-len", "short"); n = await page.evaluate("window.__beeps")
    await page.evaluate("soundVol = 0; beep()"); assert await page.evaluate("window.__beeps") == n  # muted: nothing
    await page.evaluate("soundVol = 70")
    await page.set_input_files("#snd-file", {"name": "ding.wav", "mimeType": "audio/wav", "buffer": b"RIFF" + bytes(40)}); await until(page, "soundKind === 'file'")
    assert await page.evaluate("window.__plays") == 1 and await page.evaluate("fileName") == "ding.wav" and (await page.evaluate("localStorage.getItem('soundFile')")).startswith("data:audio/wav;base64,")
    assert await page.input_value("#snd-kind") == "file" and "ding.wav" in await page.inner_text("#snd-kind option[value=file]")
    await page.select_option("#snd-len", "long"); assert await page.evaluate("window.__plays") == 4  # a clip: 3 plays for 长
    await page.set_input_files("#snd-file", {"name": "big.mp3", "mimeType": "audio/mpeg", "buffer": bytes(700 * 1024)})
    await until(page, "document.getElementById('snd-fmsg').textContent.startsWith('文件太大')"); assert await page.evaluate("fileName") == "ding.wav"  # refused, the old one stays
    await page.click("#snd-fdel"); assert await page.evaluate("[soundKind, localStorage.getItem('soundFile')]") == ["ding", None] and await page.locator("#snd-kind option").count() == 5
    await page.select_option("#snd-len", "short"); await page.evaluate("window.__beeps = 0")
    # the system notification: asked once, then shown while the tab is not looked at
    await page.check("#snd-notify"); await until(page, "oppNotify === true"); assert await page.evaluate("window.__asked") == 1
    names = await page.eval_on_selector_all("#watch input[data-watch]", "els => els.map(e => e.dataset.watch)")
    assert names[:4] == ["SSE", "UNITREEUSDT", "BNB", "SOL"] and "SIM" not in names and not await page.is_checked("#watch-tg"), names
    await page.check("#watch input[data-watch=UNITREEUSDT]"); await until(page, "watchSaving === 0 && watchMsg === '已保存'")
    assert bot.watch_state() == ({"UNITREEUSDT": "宇树 UNITREE"}, False) and await page.inner_text("#watchmsg") == "已保存"
    await page.check("#watch-tg"); await until(page, "watchSaving === 0 && watch.tg_only === true")
    assert bot.watch_state() == ({"UNITREEUSDT": "宇树 UNITREE"}, True)
    # a refused change is undone on the page and said so
    await page.evaluate("saveWatch({keys: ['NOPE']})"); await until(page, "watchMsg.startsWith('保存失败')")
    assert await page.evaluate("watch.keys") == ["UNITREEUSDT"] and "不认识的市场" in await page.inner_text("#watchmsg")
    # the chime: turning it on sounds once (and unlocks audio); the strip's new entries sound, the same ones again do not
    await page.check("#snd-on"); assert await page.evaluate("window.__beeps") == 2 and await page.evaluate("localStorage.getItem('oppSound')") == "true"  # two notes
    await page.click("#done")
    opp = lambda key, ekey, name: f"{{key:'{key}',ekey:'{ekey}',name:'{name}',group:'crypto',url:'',maker:null,taker:{{text:'吃涨 40.0',edge:.12}}}}"
    await page.evaluate("document.hasFocus = () => false")  # the window is not looked at: the notification shows
    await page.evaluate(f"hots.push({opp('UNITREEUSDT', 'UNITREEUSDT', '宇树 UNITREE')}, {opp('SOLUSDT', 'SOL', 'SOL 先触 60/140')}); drawOpps()")
    assert await page.evaluate("window.__beeps") == 4 and await page.inner_text("#opps .obell") == "🔔"
    assert await page.evaluate("window.__shown") == [["🔥 新机会 2", "宇树 UNITREE 吃涨 40.0 +12.0¢ · SOL 先触 60/140 吃涨 40.0 +12.0¢"]]
    await page.evaluate("oppNotify = false; keep('oppNotify', false)")
    assert await page.eval_on_selector_all("#opps .opp", "els => els.map(e => e.classList.contains('w'))") == [True, False]  # ★ on the watched one
    await page.evaluate("drawOpps()"); assert await page.evaluate("window.__beeps") == 4  # the same entries: quiet
    await page.click("#opps .obell"); assert await page.inner_text("#opps .obell") == "🔕" and await page.evaluate("oppSound") is False
    await page.evaluate("delete oppSeenAt['t:SOLUSDT']; drawOpps()"); assert await page.evaluate("window.__beeps") == 4  # off: quiet
    await page.click("#opps .obell"); assert await page.evaluate("window.__beeps") == 6  # on again (its own beep)
    await page.evaluate("oppSoundAll = false; delete oppSeenAt['t:SOLUSDT']; delete oppSeenAt['t:UNITREEUSDT']; drawOpps()")
    assert await page.evaluate("window.__beeps") == 8  # only the watched market sounds, SOL alone would not
    await page.evaluate("delete oppSeenAt['t:SOLUSDT']; drawOpps()"); assert await page.evaluate("window.__beeps") == 8
    await page.reload(); await page.wait_for_selector(".card .odds")
    assert await page.evaluate("[watch.keys, watch.tg_only, oppSound]") == [["UNITREEUSDT"], True, True]  # the bot's list, this browser's chime
    await page.click("#edit"); await page.click("#watch button.cb:nth-of-type(2)"); await until(page, "watchSaving === 0 && watch.keys.length === 0")  # 清空自选
    assert "自选为空" in await page.inner_text("#watchmsg") or await page.inner_text("#watchmsg") == "已保存", await page.evaluate("[watchMsg, watch, watchSaving]")
    await page.uncheck("#watch-tg"); await until(page, "watchSaving === 0 && watch.tg_only === false")
    # a starred card's fair price moving by the set amount sounds too (and notifies), measured from the last such alert
    await page.fill("#snd-fav", "5"); await page.dispatch_event("#snd-fav", "change")
    assert await page.evaluate("[favMove, localStorage.getItem('favMove')]") == [5, "5"]
    await page.click("#done"); await page.click("#g-index .card .star")  # 上证指数: the one card with a model here
    # the ⭐ 收藏 title carries the sound switch and the same box; each starred card a 🔔 box of its own (empty = the title's)
    assert await page.input_value("#h-fav .mv input") == "5" and await page.inner_text("#h-fav .hbell") == "🔔" and await page.get_attribute("#g-fav .card .mv input", "placeholder") == "5"
    assert await page.input_value("#g-fav .card .mv input") == "" and await page.locator("#g-index .card .mv").count() == 0  # only starred cards
    await page.click("#h-fav .hbell"); assert await page.evaluate("oppSound") is False and await page.inner_text("#h-fav .hbell") == "🔕"
    await page.click("#h-fav .hbell"); assert await page.evaluate("oppSound") is True
    await page.evaluate("document.hasFocus = () => false; oppNotify = true; Notification.permission = 'granted'; window.__shown = []; window.__beeps = 0")  # (the reload above reset the stub)
    assert await page.evaluate("favBase['上证指数'] === last.items[0].fair_up") and await page.evaluate("favBase['宇树 UNITREE']") is None
    await page.evaluate("last.items[0].fair_up += 0.03; render(last)"); assert await page.evaluate("[window.__beeps, window.__shown.length]") == [0, 0]  # 3¢: not yet
    await page.evaluate("last.items[0].fair_up += 0.03; render(last)"); assert await page.evaluate("window.__beeps") == 2  # 6¢ since the start
    shown = await page.evaluate("window.__shown"); assert len(shown) == 1 and shown[0][0] == "📈 收藏变动 1" and "上证指数 公平价 35.0¢ → 41.0¢（+6.0¢）" == shown[0][1], shown
    await page.evaluate("last.items[0].fair_up -= 0.04; render(last)"); assert await page.evaluate("window.__beeps") == 2  # 4¢ from the alerted 41¢: not yet
    await page.fill("#g-fav .card .mv input", "2"); await page.dispatch_event("#g-fav .card .mv input", "change")  # this card's own bar
    assert await page.evaluate("favMoveMap") == {"上证指数": 2} and await page.evaluate("localStorage.getItem('favmove')") == '{"上证指数":2}'
    await page.evaluate("last.items[0].fair_up -= 0.025; render(last)"); assert await page.evaluate("window.__beeps") == 4  # 6.5¢ under the alerted 41¢: well over 2¢
    await page.fill("#h-fav .mv input", "0"); await page.dispatch_event("#h-fav .mv input", "change")  # the title's bar off: the card's own still counts
    assert await page.evaluate("[favMove, localStorage.getItem('favMove')]") == [0, "0"]  # (the panel's box is redrawn when 自定义 opens)
    await page.evaluate("last.items[0].fair_up += 0.03; render(last)"); assert await page.evaluate("window.__beeps") == 6
    await page.fill("#g-fav .card .mv input", ""); await page.dispatch_event("#g-fav .card .mv input", "change"); assert await page.evaluate("favMoveMap") == {}
    await page.evaluate("last.items[0].fair_up += 0.09; render(last)"); assert await page.evaluate("window.__beeps") == 6  # nothing set any more: quiet
    await page.click("#g-fav .card .star"); await page.click("#edit"); await page.fill("#snd-fav", "0"); await page.dispatch_event("#snd-fav", "change")
    await page.evaluate("oppNotify = false; keep('oppNotify', false)")
    await page.uncheck("#snd-on"); await page.click("#done")
    assert bot.watch_state() == ({}, False) and await page.evaluate("localStorage.getItem('oppSound')") == "false" and await page.evaluate("favs") == []
    await page.evaluate("localStorage.removeItem('soundLen')")


async def float_check(browser, page, port, token):
    """⧉ 悬浮: the starred cards' odds in a document picture-in-picture window the page draws on every render (a real popup
    stands in for it here), the /mini page in a popup where there is no such API, and /mini on its own."""
    await page.reload(); await page.wait_for_selector(".card .odds")
    await page.click("#g-index .card .star"); await page.click("#g-ladder .card .star")  # 上证指数, $牛来 市值
    assert await page.eval_on_selector_all(".floatbtn", "b => b.map(x => x.textContent)") == ["⧉ 悬浮", "⧉ 悬浮"]  # the header's and the ⭐ 收藏 title's
    if not await page.evaluate("typeof window.documentPictureInPicture === 'object'"):  # headless Chromium has had it since 116
        print("document picture-in-picture check skipped (no API in this Chromium)")
    else:
        async with page.context.expect_page() as popup:  # the picture-in-picture window is a page of its own to Playwright
          await page.click("#float")
        pip = await popup.value; await until(page, "floatPip !== null")
        assert await page.eval_on_selector_all(".floatbtn", "b => b.map(x => x.textContent)") == ["⧉ 悬浮中", "⧉ 悬浮中"]
        assert await pip.title() == "收藏 · 概率" and await pip.locator("#mini .mcard").count() == 2
        text = await pip.inner_text("#mini")
        assert text.startswith("上证指数\n10-08 下周四\n已收盘\n⏳ 7天 16:5") and "涨 35.0¢\n65.0¢ 跌\n参考 3,850.12 → 隐含 3,819.64（-0.79%）\n$牛来 市值" in text, text
        assert await pip.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(242, 244, 248)"
        await page.evaluate("last.items[0].fair_up = 0.42; last.items[0].fair_down = 0.58; render(last)")  # every render of the page redraws it
        assert "涨 42.0¢\n58.0¢ 跌" in await pip.inner_text("#mini .mcard")
        await page.click("#theme"); await page.click("#theme")  # 浅色, then 深色: the small window follows
        assert await pip.evaluate("document.documentElement.dataset.theme") == "dark" and await pip.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(13, 16, 20)"
        await page.click("#theme"); assert await pip.evaluate("document.documentElement.dataset.theme") is None
        await page.click("#float"); assert len(page.context.pages) == 2  # already open: brought to the front, not opened again
        await pip.close(); await until(page, "floatPip === null")
        assert await page.eval_on_selector_all(".floatbtn", "b => b.map(x => x.textContent)") == ["⧉ 悬浮", "⧉ 悬浮"]
    # no picture-in-picture API (Firefox, Safari, phones): a small popup window with /mini
    await page.evaluate("delete window.documentPictureInPicture; window.__opened = null; window.open = (u, n, f) => {window.__opened = [u, n, f]; return {closed: false}}")
    await page.click("#float"); assert await page.evaluate("window.__opened") == [f"/p/{token}/mini", "mini", "popup=yes,width=340,height=520"]
    # /mini on its own: this browser's stars, refreshed by itself, a link back
    ctx = await browser.new_context(viewport={"width": 340, "height": 520})
    await ctx.add_init_script("if (!localStorage.getItem('favs')) localStorage.setItem('favs', JSON.stringify(['上证指数', 'UNITREEUSDT']))")
    mini = await ctx.new_page(); await mini.goto(f"http://127.0.0.1:{port}/p/{token}/mini"); await mini.wait_for_selector(".mcard")
    text = await mini.inner_text("body")
    assert text.startswith("上证指数\n10-08 下周四\n已收盘\n⏳ 7天 16:5") and "涨 35.0¢\n65.0¢ 跌" in text and "宇树 UNITREE\n10-08 下周四\n已收盘\n概率暂缺：等待行情" in text and text.endswith("每 5 秒刷新 · 主页面 ↗"), text
    assert await mini.get_attribute("#mback", "href") == f"/p/{token}" and await mini.locator("#mini .mcard").count() == 2
    await mini.evaluate("localStorage.setItem('favs', '[]'); dispatchEvent(new StorageEvent('storage', {key: 'favs'}))")  # the main page's stars change: redrawn
    assert await mini.inner_text("#mini") == "还没有收藏：在主页面点卡片的 ☆"
    await ctx.close()
    await page.click("#g-fav .card .star"); await page.click("#g-fav .card .star"); assert await page.evaluate("favs") == []


async def auction_check(page):
    """The pre-open auction's indicative price in the 可撤单 phase (the odds still rest on the proxy): an orange line of its
    own on the card, with its move from the reference close, brought by the event stream and taken down with the phase;
    the mini window shows the tag and the line too."""
    await page.reload(); await page.wait_for_selector(".card .odds")
    live = lambda **f: page.evaluate("patchLive({items: [{name: '上证指数', symbol: '', " + ", ".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in f.items()) + "}]})")
    await live(preopen="沪深开盘集合竞价（09:15–09:25）", preopen_phase="可撤单", preopen_price="3,760.00", preopen_at=1759194000000, auction="", trading="")
    card = page.locator("#g-index .card")
    assert await card.locator(".tag.auc").inner_text() == "竞价·可撤单" and await card.locator(".aucline").count() == 1
    text = await card.locator(".aucline").inner_text()
    assert text.startswith("📍 竞价参考价 3,760.00\n-2.34%") and "可撤单阶段：还能撤单，常是试探；概率仍按上面的有效价算，撮合前仍会变" in text, text
    assert await card.locator(".aucline .age").count() == 1 and await card.locator(".odds").count() == 1  # the odds stay as they were
    box = await page.evaluate("const d = document.createElement('div'); miniRender(d, last.items, {favs: ['上证指数'], hots: new Set(), style: 'cn', skew: 0}); d.innerText")
    assert "竞价·可撤单" in box and "📍 竞价参考价 3,760.00（-2.34%） 可撤单阶段，概率暂不按它算" in box, box
    await live(preopen="沪深开盘集合竞价（09:15–09:25）", preopen_phase="不可撤单", preopen_price="3,765.00", preopen_at=1759194060000, eff_label="竞价", effective="3,765.00", auction="", trading="")
    assert await card.locator(".aucline").count() == 0 and await card.locator(".tag.auc").inner_text() == "竞价·不可撤单"  # the odds rest on it now: the summary shows it
    await live(preopen="", preopen_phase="", preopen_price="", preopen_at=0, eff_label="隐含", auction="", trading="已收盘")
    assert await card.locator(".tag.auc").count() == 0 and await card.locator(".aucline").count() == 0 and await card.locator(".tag.lunch").inner_text() == "已收盘"
    # the two venues' depth in the details, from the event stream too
    await live(depth={"proxy": "hl", "mode": "auto", "binance": {"within": 8040, "near": 8040, "spread_bps": 7.6, "bid": 1340, "ask": 1341, "age": 1, "levels": 2}, "hl": {"within": 107360, "near": 107360, "spread_bps": 14.9, "bid": 1341, "ask": 1343, "age": 0, "levels": 2}})
    await card.locator("details summary").first.click()
    assert "深度\n±1% 币安 $8.04K（点差 0.08%） · HL $107K（点差 0.15%） · 盘后概率代理 Hyperliquid（自动，深 1.5 倍才切换）" in await card.inner_text(), await card.inner_text()
    await live(depth={"proxy": "binance", "mode": "binance", "binance": None, "hl": {"error": "HTTP 500"}})
    assert "深度\n±1% 币安 未读到 · HL 读取失败 · 盘后概率代理 币安" in await card.inner_text()
    await page.reload(); await page.wait_for_selector(".card .odds")


async def browser_check(async_playwright, chrome, port, token, bot):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**({"executable_path": chrome} if chrome else {}))
        url = f"http://127.0.0.1:{port}/p/{token}"
        # a fresh browser hides 价格阶梯 (and 模拟交易) but shows 市值阶梯; ticking it in 自定义 brings it back
        fresh = await browser.new_page(viewport={"width": 390, "height": 900})
        await fresh.goto(url); await fresh.wait_for_selector("#g-crypto .card")
        assert not await fresh.is_visible("#h-levels") and await fresh.is_visible("#g-ladder .card")
        assert await fresh.evaluate("localStorage.getItem('hideSec')") == '["sim","levels"]' and await fresh.evaluate("localStorage.getItem('levelsDefault')") == "1"
        await fresh.click("#edit"); await fresh.check("#secs input[data-sec=levels]"); await fresh.click("#done")
        assert await fresh.is_visible("#g-levels .card") and await fresh.evaluate("localStorage.getItem('hideSec')") == '["sim"]'
        await fresh.close()
        # a browser that ran 1.21.5 (which hid 市值阶梯 by mistake) gets 市值阶梯 back and 价格阶梯 hidden, once
        older = await browser.new_page(viewport={"width": 390, "height": 900})
        await older.add_init_script("if(!localStorage.getItem('levelsDefault')&&!localStorage.getItem('hideSec')){"
                                    "localStorage.setItem('hideSec','[\"sim\",\"ladder\"]');localStorage.setItem('ladderDefault','1')}")
        await older.goto(url); await older.wait_for_selector("#g-crypto .card")
        assert await older.is_visible("#g-ladder .card") and not await older.is_visible("#h-levels")
        assert await older.evaluate("[localStorage.getItem('hideSec'), localStorage.getItem('ladderDefault'), localStorage.getItem('levelsDefault')]") == ['["sim","levels"]', None, "1"]
        await older.close()
        page = await browser.new_page(viewport={"width": 390, "height": 900})
        await page.add_init_script("if(!localStorage.getItem('levelsDefault')){localStorage.setItem('hideSec','[\"sim\"]');localStorage.setItem('levelsDefault','1')}")  # 价格阶梯 on, as below; one-shot so a reload keeps what the test changes
        await page.goto(url)
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
        assert await page.evaluate("load() === load()") and await page.evaluate("(async () => { await load(); return loading === null })()")  # one fetch at a time
        # a phone: the filter bar is one rail that scrolls sideways (one row high), wrapping into rows only on a wider screen
        fb = "document.getElementById('fbar')"
        assert await page.evaluate(f"getComputedStyle({fb}).overflowX") == "auto" and await page.evaluate(f"{fb}.scrollWidth > {fb}.clientWidth")
        assert await page.evaluate(f"{fb}.getBoundingClientRect().height") < 44, await page.evaluate(f"{fb}.getBoundingClientRect().height")
        assert await page.evaluate(f"getComputedStyle({fb}).position") == "sticky"
        await page.set_viewport_size({"width": 1300, "height": 900})
        assert await page.evaluate(f"getComputedStyle({fb}).overflowX") == "visible" and await page.evaluate(f"{fb}.scrollWidth <= {fb}.clientWidth")
        await page.set_viewport_size({"width": 390, "height": 900})
        assert await page.inner_text("#h-index") == "指数" and await page.is_visible("#h-contract")
        # the theme button: 自动 → 浅色 → 深色 → 自动, kept in this browser and applied before the first paint; the skeleton
        # cards are gone; the back-to-top button shows once scrolled
        html = "document.documentElement"
        assert await page.inner_text("#theme") == "◐ 自动" and await page.evaluate(f"{html}.dataset.theme") is None
        await page.click("#theme")
        assert await page.inner_text("#theme") == "☀ 浅色" and await page.evaluate(f"{html}.dataset.theme") == "light"
        await page.click("#theme")
        assert await page.evaluate(f"{html}.dataset.theme") == "dark" and await page.evaluate("localStorage.getItem('theme')") == '"dark"'
        assert await page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(13, 16, 20)"
        await page.reload(); await page.wait_for_selector(".card .odds")
        assert await page.evaluate(f"{html}.dataset.theme") == "dark" and await page.inner_text("#theme") == "☾ 深色"
        await page.click("#theme")
        assert await page.evaluate(f"{html}.dataset.theme") is None and await page.evaluate("localStorage.getItem('theme')") == '"auto"'
        assert await page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(242, 244, 248)"
        assert await page.locator(".skel").count() == 0
        assert not await page.evaluate("document.getElementById('totop').classList.contains('show')")
        await page.evaluate("scrollTo(0, 1500)"); await page.wait_for_timeout(100)
        assert await page.evaluate("document.getElementById('totop').classList.contains('show')")
        await page.click("#totop"); await page.wait_for_timeout(1000)
        assert await page.evaluate("scrollY") == 0 and not await page.evaluate("document.getElementById('totop').classList.contains('show')")
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
        # a section title folds its cards away: the title stays, with the count; kept in this browser across reloads
        await page.click("#h-crypto .fold")
        assert not await page.is_visible("#g-crypto") and await page.is_visible("#h-crypto") and await page.inner_text("#h-crypto .fs") == "7 张"
        assert await page.get_attribute("#h-crypto .fold", "aria-expanded") == "false" and await page.evaluate("localStorage.getItem('folded')") == '["crypto"]'
        await page.reload(); await page.wait_for_selector("#g-index .card")
        assert not await page.is_visible("#g-crypto") and await page.inner_text("#h-index") == "指数" and await page.is_visible("#g-index .card")
        await page.click("#h-crypto .fold")
        assert await page.is_visible("#g-crypto .card") and await page.locator("#h-crypto .fs").count() == 0 and await page.evaluate("localStorage.getItem('folded')") == "[]"
        await watch_check(bot, page)
        await float_check(browser, page, port, token)
        await auction_check(page)
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
    assert names == ["上证指数", "宇树 UNITREE", "BNB 先触 700/900", "SOL 先触 60/140", "BTC 先触 70k/90k", "ETH 先触 1k/3k", "BTC 10月涨跌", "ETH 10月涨跌",
                     "HYPE 反超 SOL", "BTC 10月价格", "ETH 10月价格", "SOL 10月价格", "HYPE 10月价格", "STRC 触及 $100", "$牛来 市值", "$ANSEM FDV", "$PONS FDV", "$MEME FDV", "$CASHCAT FDV", "$AI FDV", "$STONK FDV", "$STONKBROKER FDV", "模拟交易"], names
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
    st, head, body = await request(port, f"GET /p/{token}/mini HTTP/1.1\r\nHost: x\r\n\r\n".encode())  # the starred cards alone, for a small window
    assert st == 200 and "text/html" in head and "<title>收藏 · 概率</title>".encode() in body and b"function miniRender" in body and "Cache-Control: no-store" in head
    st, head, body = await request(port, f"GET /p/{token}/data.json?x=1 HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert st == 200 and json.loads(body)["items"][0]["name"] == "上证指数" and "application/json" in head
    assert "Content-Encoding" not in head and "Vary: Accept-Encoding" in head  # no Accept-Encoding: plain
    # the event stream: the daily cards' live numbers as server-sent events (the first at once), behind the same token
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(f"GET /p/{token}/events HTTP/1.1\r\nHost: x\r\nAccept: text/event-stream\r\n\r\n".encode()); await w.drain()
    shead = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 5)
    assert shead.startswith(b"HTTP/1.1 200") and b"text/event-stream" in shead and b"Cache-Control: no-store" in shead and b"Content-Length" not in shead, shead
    assert await asyncio.wait_for(r.readuntil(b"\n\n"), 5) == b"retry: 2000\n\n"
    event = await asyncio.wait_for(r.readuntil(b"\n\n"), 5)
    assert event.startswith(b"event: live\ndata: ") and event.count(b"\n") == 3, event[:80]
    live = json.loads(event[len(b"event: live\ndata: "):])
    assert live["server_ms"] == bot.market.now_ms() and live["items"][0]["name"] == "上证指数", live
    assert {"fair_up", "fair_down", "effective", "quote_ms", "move", "proxy_note", "warn"} <= set(live["items"][0]) and "ref_note" not in live["items"][0]
    assert {k: live["items"][1][k] for k in ("name", "symbol", "missing")} == {"name": "宇树 UNITREE", "symbol": "UNITREEUSDT", "missing": "等待行情"} and len(live["items"]) == 2, live["items"]
    assert {k: live["items"][1][k] for k in ("auction", "preopen", "preopen_phase", "preopen_price", "preopen_at")} == {"auction": "", "preopen": "", "preopen_phase": "", "preopen_price": "", "preopen_at": 0}  # the session tags ride along
    assert live["items"][1]["trading"] == "已收盘" and live["items"][0]["trading"] == "已收盘", live["items"]
    assert live["items"][1]["depth"] == {"proxy": "binance", "mode": "auto", "binance": None, "hl": None} and "depth" not in live["items"][0]  # no book read here
    assert "predict" not in live["items"][0]  # no book here (Predict is not polled in this test); with one it rides along, without edges or age
    assert live["items"][0]["fair_up"] == json.loads(body)["items"][0]["fair_up"] and web.streams == 1
    w.close(); await w.wait_closed()
    st, _, _ = await request(port, b"GET /p/" + b"x" * len(token) + b"/events HTTP/1.1\r\nHost: x\r\n\r\n"); assert st == 404
    st, _, _ = await request(port, f"POST /p/{token}/events HTTP/1.1\r\nHost: x\r\n\r\n".encode()); assert st == 405
    st, _, body = await request(port, f"HEAD /p/{token} HTTP/1.1\r\n\r\n".encode()); assert st == 200 and body == b""
    # gzip for a client that accepts it (the page on every open, data.json every 10 seconds per tab); plain when it does not; a HEAD carries the headers only
    st, head, body = await request(port, f"GET /p/{token} HTTP/1.1\r\nHost: x\r\nAccept-Encoding: gzip, deflate, br\r\n\r\n".encode())
    assert st == 200 and "Content-Encoding: gzip" in head and f"Content-Length: {len(body)}" in head and "text/html" in head, head
    assert len(body) < len(m.WEB_PAGE.encode()) / 2 and gzip.decompress(body) == m.WEB_PAGE.encode()
    st, head, body = await request(port, f"GET /p/{token}/data.json HTTP/1.1\r\nHost: x\r\naccept-encoding: identity;q=1, gzip;q=0.5\r\n\r\n".encode())
    assert st == 200 and "Content-Encoding: gzip" in head and json.loads(gzip.decompress(body))["items"][0]["name"] == "上证指数"
    st, head, body = await request(port, f"GET /p/{token}/data.json HTTP/1.1\r\nHost: x\r\nAccept-Encoding: gzip;q=0\r\n\r\n".encode())
    assert st == 200 and "Content-Encoding" not in head and json.loads(body)["items"][0]["name"] == "上证指数"
    st, head, body = await request(port, f"GET /p/{token}/data.json HTTP/1.1\r\nHost: x\r\nAccept-Encoding: br\r\n\r\n".encode())
    assert st == 200 and "Content-Encoding" not in head and json.loads(body)["items"]
    st, head, body = await request(port, f"HEAD /p/{token} HTTP/1.1\r\nAccept-Encoding: gzip\r\n\r\n".encode())
    assert st == 200 and body == b"" and "Content-Encoding: gzip" in head
    st, head, body = await request(port, b"GET /health HTTP/1.1\r\nAccept-Encoding: gzip\r\n\r\n"); assert body == b"ok" and "Content-Encoding" not in head  # too small to be worth it
    assert m.accepts_gzip("Accept-Encoding: *\r\n") and not m.accepts_gzip("Accept-Encoding: gzip;q=abc\r\n") and not m.accepts_gzip("X-Accept-Encoding: gzip\r\n")
    for raw in [b"GET /p/wrongtokenwrongtoken HTTP/1.1\r\n\r\n", f"GET /p/{token}/other HTTP/1.1\r\n\r\n".encode(), b"GET /etc/passwd HTTP/1.1\r\n\r\n"]:
        st, _, _ = await request(port, raw); assert st == 404, raw
    st, _, _ = await request(port, f"POST /p/{token} HTTP/1.1\r\n\r\n".encode()); assert st == 405
    # the one write: POST /watch (JSON) sets the watched markets (自选) and whether the Telegram alerts keep to them;
    # data.json carries them, and each card's market key (the one /edge and /watch know it by)
    d = json.loads((await request(port, f"GET /p/{token}/data.json HTTP/1.1\r\n\r\n".encode()))[2])
    assert d["watch"] == {"keys": [], "names": {}, "tg_only": False, "tg": True} and [i["ekey"] for i in d["items"][:4]] == ["SSE", "UNITREEUSDT", "BNB", "SOL"], d["watch"]
    assert d["items"][-1]["ekey"] == "SIM" and {i["ekey"] for i in d["items"][:-1]} <= {k for k, _ in bot.edge_markets()}
    post = lambda body, ctype="Content-Type: application/json\r\n": request(port, f"POST /p/{token}/watch HTTP/1.1\r\nHost: x\r\n{ctype}Content-Length: {len(body)}\r\n\r\n".encode() + body)
    st, head, body = await post(b'{"keys": ["UNITREEUSDT", "SOL"], "tg_only": true}')
    assert st == 200 and "application/json" in head and json.loads(body) == {"keys": ["UNITREEUSDT", "SOL"], "names": {"UNITREEUSDT": "宇树 UNITREE", "SOL": "SOL"}, "tg_only": True, "tg": True}, body
    assert bot.watch_state() == ({"UNITREEUSDT": "宇树 UNITREE", "SOL": "SOL"}, True) and store.get("settings")["watch_keys"] == {"UNITREEUSDT": "宇树 UNITREE", "SOL": "SOL"}
    d = json.loads((await request(port, f"GET /p/{token}/data.json HTTP/1.1\r\n\r\n".encode()))[2]); assert d["watch"]["keys"] == ["UNITREEUSDT", "SOL"] and d["watch"]["tg_only"]  # the cached answer was dropped
    st, _, body = await post(b'{"tg_only": false}'); assert st == 200 and json.loads(body)["keys"] == ["UNITREEUSDT", "SOL"] and not bot.watch_state()[1]  # a field left out stays as it is
    for bad, why in [(b'{"keys": ["NOPE"]}', "不认识的市场：NOPE"), (b'{"keys": "x"}', "keys 要是"), (b'{"tg_only": 1}', "tg_only"), (b'[1]', "JSON 对象"), (b'{bad', ""), (b'\xff', "")]:
        st, _, body = await post(bad); assert st == 400 and why in json.loads(body)["error"], (bad, body)
    st, _, body = await post(b'{"keys": []}', ""); assert st == 400 and "JSON" in json.loads(body)["error"]  # a form post (no JSON content type) is refused
    assert bot.watch_state() == ({"UNITREEUSDT": "宇树 UNITREE", "SOL": "SOL"}, False)  # nothing refused changed anything
    st, _, _ = await request(port, f"POST /p/{token}/watch HTTP/1.1\r\nContent-Type: application/json\r\nContent-Length: 99999\r\n\r\n".encode()); assert st == 400  # too big
    st, _, _ = await request(port, f"POST /p/{token}/watch HTTP/1.1\r\nContent-Type: application/json\r\nContent-Length: x\r\n\r\n".encode()); assert st == 400
    st, _, _ = await request(port, f"POST /p/{token}/data.json HTTP/1.1\r\nContent-Length: 0\r\n\r\n".encode()); assert st == 405
    st, _, _ = await request(port, b"POST /p/" + b"x" * len(token) + b"/watch HTTP/1.1\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"); assert st == 405
    assert bot.watch_update([], False) == {"keys": [], "names": {}, "tg_only": False, "tg": True}
    d = json.loads((await request(port, f"GET /p/{token}/data.json HTTP/1.1\r\n\r\n".encode()))[2]); assert d["watch"]["keys"] == []
    st, _, _ = await request(port, b"GARBAGE\r\n\r\n"); assert st == 400
    # within the cache interval the last good answer is served; a payload crash after it returns 500 without killing the server
    orig = bot.odds_payload; bot.odds_payload = lambda: 1 / 0
    st, _, _ = await request(port, f"GET /p/{token}/data.json HTTP/1.1\r\n\r\n".encode()); assert st == 200
    web.cache.pop("data.json", None)
    st, _, _ = await request(port, f"GET /p/{token}/data.json HTTP/1.1\r\n\r\n".encode()); assert st == 500
    bot.odds_payload = orig; web.cache.pop("data.json", None)
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
        web.CACHE_SECONDS = {}  # the browser check changes the payload and reloads at once
        await browser_check(async_playwright, chrome, port, token, bot)
    await web.stop()
    print("WEB_OK")
asyncio.run(run())
