import asyncio, html, re, sys, time, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D


def bj(mo, d, h, mi, s=0):
    return int(dt.datetime(2026, mo, d, h, mi, s, tzinfo=m.BEIJING).timestamp() * 1000)


# --- the windows (Beijing time), trading days only ------------------------------------------------------------------
assert m.auction_running("sh", bj(9, 29, 14, 57)) and m.auction_running("sh", bj(9, 29, 14, 59, 59))
assert not m.auction_running("sh", bj(9, 29, 14, 56, 59)) and not m.auction_running("sh", bj(9, 29, 15, 0))
assert m.auction_running("kr", bj(9, 29, 14, 20)) and not m.auction_running("kr", bj(9, 29, 14, 30))
assert m.auction_running("hk", bj(9, 29, 16, 5)) and not m.auction_running("hk", bj(9, 29, 16, 10))
assert not m.auction_running("sh", bj(10, 3, 14, 58))                                        # Saturday
assert not m.auction_running("sh", bj(10, 1, 14, 58), frozenset({dt.date(2026, 10, 1)}))    # holiday
assert m.auction_running("sz", bj(9, 29, 14, 58)) and not m.auction_running("us", bj(9, 29, 14, 58))


async def run():
    sent = []

    class FakeTelegram:
        async def call(self, *a, **k): return True
        async def send(self, chat, thread, text, reply_markup=None, parse_mode=None): sent.append(text); return True

    now = {"ms": bj(9, 29, 14, 57, 20)}

    class FakeMarket:
        def now_ms(self): return now["ms"]

    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT,SKHYNIXUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off"})
    bot = m.Bot(cfg, m.Store(":memory:"), FakeMarket(), FakeTelegram())
    bot.store.put("subscriptions", {"1:0": {"chat": 1, "thread": 0, "active": True}, "2:0": {"chat": 2, "thread": 0, "active": False}})
    sse = m.close_odds("上证指数", D("3823.62"), D("3830.10"), 0.01, 0.002, dt.date(2026, 9, 29), D("0.01"), "09-28 收盘",
                       "上证现货 3,830.1（盘中直接用现货）", "σ")
    uni = m.close_odds("宇树 UNITREE", D("459.65"), D("455.00"), 0.03, 0.002, dt.date(2026, 9, 29), D("0.01"), "09-28 收盘",
                       "上交所现货 455（腾讯·盘中直接用现货）", "σ", "CNY")
    hynix = m.close_odds("SK 海力士", D("1768000"), D("1769000"), 0.02, 0.4, dt.date(2026, 9, 29), D("1000"), "09-28 收盘", "x", "σ", "KRW")
    bot.odds_items = lambda ms: [("上证指数", sse), ("宇树 UNITREE｜UNITREEUSDT", uni), ("SK 海力士｜SKHYNIXUSDT", hynix)]
    bot.predict.books["SSE"] = m.PredictBook("SSE", "s", "1", "t", ((D("0.60"), D("10")),), ((D("0.64"), D("10")),), now["ms"])

    # 14:57:20: the A-share auction has started -> one reminder to each active subscriber, with the A-share cards only
    await bot.auction_reminders(now["ms"]); await bot.drain_deliveries()
    assert len(sent) == 1, sent
    text = sent[0]
    assert "沪深收盘集合竞价（14:57–15:00） 开始" in text and "上证指数" in text and "宇树 UNITREE" in text and "SK 海力士" not in text, text
    assert "昨收 3,823.62 → 现 3,830.1（+0.17%）" in text and "昨收 459.65 CNY → 现 455 CNY（-1.01%）" in text, text
    assert "Predict 买1 60.0¢｜卖1 64.0¢" in text and "👉" in text, text
    # not again the same day
    now["ms"] = bj(9, 29, 14, 58)
    await bot.auction_reminders(now["ms"]); await bot.drain_deliveries(); assert len(sent) == 1
    # a restart that joins late does not send a stale reminder
    late = m.Bot(cfg, m.Store(":memory:"), FakeMarket(), FakeTelegram()); late.store.put("subscriptions", bot.subscriptions())
    late.odds_items = bot.odds_items
    now["ms"] = bj(9, 29, 14, 59, 30)
    await late.auction_reminders(now["ms"]); await late.drain_deliveries(); assert len(sent) == 1
    # the Korean auction the same day is its own reminder
    now["ms"] = bj(9, 29, 14, 20, 30)  # the send-time check reads the bot's own clock
    await bot.auction_reminders(now["ms"]); await bot.drain_deliveries()
    assert len(sent) == 2 and "韩交所收盘集合竞价" in sent[1] and "SK 海力士" in sent[1] and "上证指数" not in sent[1], sent[1]

    # the web card carries an "auction" tag while its market's auction runs
    now["ms"] = bj(9, 29, 14, 58)
    items = {i["name"]: i for i in bot.odds_payload()["items"]}
    assert "沪深收盘集合竞价" in items["上证指数"].get("auction", "") and "auction" not in items["SK 海力士"], items
    assert "trading" not in items["上证指数"] and items["SK 海力士"]["trading"] == "已收盘", items  # the auction tag instead
    now["ms"] = bj(9, 29, 15, 1)
    after = {i["name"]: i for i in bot.odds_payload()["items"]}
    assert "auction" not in after["上证指数"] and after["上证指数"]["trading"] == "已收盘", after["上证指数"]
    now["ms"] = bj(9, 29, 8, 30)
    assert {i["name"]: i for i in bot.odds_payload()["items"]}["上证指数"]["trading"] == "未开盘"

    # AUCTION_ALERT=off
    off = m.Bot(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "AUCTION_ALERT": "off"}), m.Store(":memory:"), FakeMarket(), FakeTelegram())
    off.store.put("subscriptions", bot.subscriptions()); off.odds_items = bot.odds_items
    await off.auction_reminders(bj(9, 29, 14, 57, 10)); await off.drain_deliveries(); assert len(sent) == 2

    # --- pre-open auctions: the windows (Beijing time), the reminder before them, the card's tag and quote pages --------
    assert m.preopen_running("hk", bj(10, 8, 9, 0)) and m.preopen_running("hk", bj(10, 8, 9, 29, 59)) and not m.preopen_running("hk", bj(10, 8, 9, 30))
    assert not m.preopen_running("hk", bj(10, 8, 8, 59, 59)) and not m.preopen_running("hk", bj(10, 10, 9, 5))      # Saturday
    assert not m.preopen_running("hk", bj(10, 1, 9, 5), frozenset({dt.date(2026, 10, 1)}))                            # holiday
    assert m.preopen_running("sh", bj(10, 8, 9, 15)) and m.preopen_running("sz", bj(10, 8, 9, 29)) and not m.preopen_running("sh", bj(10, 8, 9, 14))
    assert m.preopen_running("kr", bj(10, 8, 7, 30)) and not m.preopen_running("kr", bj(10, 8, 8, 0)) and not m.preopen_running("us", bj(10, 8, 9, 5))
    assert m.preopen_window("hk", bj(10, 8, 9, 0))[2] == "港交所开市前竞价（09:00–09:30，09:20–09:22 随机撮合，09:30 连续交易）"
    assert m.preopen_window("kr", bj(11, 19, 8, 30))[:2] == (dt.time(8, 30), dt.time(9, 0)) and "高考日" in m.preopen_window("kr", bj(11, 19, 8, 30))[2]
    assert m.preopen_window("kr", bj(11, 18, 8, 30))[:2] == (dt.time(7, 30), dt.time(8, 0))
    # the phases: orders withdrawable, then not, HK's random match, then the matched open until continuous trading
    assert [m.preopen_phase("hk", bj(10, 8, 9, mi, sec)) for mi, sec in ((0, 0), (14, 59), (15, 0), (19, 59), (20, 0), (21, 59), (22, 0), (29, 59), (30, 0))] == [
        "可撤单", "可撤单", "不可撤单", "不可撤单", "随机撮合", "随机撮合", "已撮合", "已撮合", ""]
    assert [m.preopen_phase("sh", bj(10, 8, 9, mi)) for mi in (14, 15, 19, 20, 24, 25, 29, 30)] == ["", "可撤单", "可撤单", "不可撤单", "不可撤单", "已撮合", "已撮合", ""]
    assert m.preopen_phase("sz", bj(10, 8, 9, 21)) == "不可撤单" and m.preopen_phase("kr", bj(10, 8, 7, 59)) == "可撤单" and m.preopen_phase("kr", bj(10, 8, 8, 0)) == ""
    assert m.preopen_phase("kr", bj(11, 19, 8, 30)) == "可撤单" and m.preopen_phase("kr", bj(11, 19, 7, 45)) == ""  # the late day, an hour on
    assert m.preopen_phase("hk", bj(10, 1, 9, 5), frozenset({dt.date(2026, 10, 1)})) == "" and m.preopen_phase("us", bj(10, 8, 9, 5)) == ""
    assert m.quote_pages(m.StockTicker("hk", "00625")) == [
        {"name": "富途", "url": "https://www.futunn.com/stock/00625-HK"},
        {"name": "AAStocks", "url": "https://www.aastocks.com/tc/stocks/quote/detail-quote.aspx?symbol=00625"},
        {"name": "腾讯", "url": "https://gu.qq.com/hk00625"},
        {"name": "etnet", "url": "https://www.etnet.com.hk/www/tc/stocks/realtime/quote.php?code=625"},
        {"name": "港交所", "url": "https://www.hkex.com.hk/Market-Data/Securities-Prices/Equities/Equities-Quote?sym=625&sc_lang=zh-HK"}]
    assert m.quote_pages(m.StockTicker("sh", "688836"))[0] == {"name": "腾讯", "url": "https://gu.qq.com/sh688836"}
    assert m.quote_pages(m.StockTicker("kr", "000660")) == [{"name": "Naver", "url": "https://finance.naver.com/item/main.naver?code=000660"}]
    pcfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "HK0625USDT,UNITREEUSDT,SKHYNIXUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off"})
    assert pcfg.preopen_alert and pcfg.preopen_lead == 5
    assert m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "PREOPEN_ALERT_LEAD_MINUTES": "0"}).preopen_lead == 0
    for bad in ({"PREOPEN_ALERT_LEAD_MINUTES": "61"}, {"PREOPEN_ALERT_LEAD_MINUTES": "x"}):
        try: m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", **bad}); assert False, bad
        except ValueError: pass
    sent.clear()
    pbot = m.Bot(pcfg, m.Store(":memory:"), FakeMarket(), FakeTelegram())
    pbot.store.put("subscriptions", {"1:0": {"chat": 1, "thread": 0, "active": True}})
    shein = m.close_odds("SHEIN 希音", D("38.10"), D("38.30"), 0.025, 1.0, dt.date(2026, 10, 8), D("0.01"), "10-07 收盘",
                         "币安 38.30 / 收盘时刻 38.10 → +0.525%", "σ")
    pbot.odds_items = lambda ms: [("SHEIN 希音｜HK0625USDT", shein), ("宇树 UNITREE｜UNITREEUSDT", uni), ("SK 海力士｜SKHYNIXUSDT", "等待行情")]
    pbot.predict.books["HK0625USDT"] = m.PredictBook("HK0625USDT", "s", "1", "t", ((D("0.50"), D("10")),), ((D("0.53"), D("10")),), bj(10, 8, 8, 55))
    now["ms"] = bj(10, 8, 8, 50)
    await pbot.preopen_reminders(now["ms"]); await pbot.drain_deliveries(); assert sent == []  # ten minutes early: not yet
    now["ms"] = bj(10, 8, 8, 55, 30)  # five minutes before 09:00: the HK reminder, with the HK card only
    await pbot.preopen_reminders(now["ms"]); await pbot.drain_deliveries()
    assert len(sent) == 1, sent
    plain = lambda t: html.unescape(re.sub(r"</?b>", "", t))  # the bold tags and escapes of the HTML message
    text = plain(sent[0])
    assert text.startswith("🔔 港交所开市前竞价 5 分钟后开始（09:00–09:30，09:20–09:22 随机撮合，09:30 连续交易）\n竞价一开始就有方向信息，撮合可能早于连续交易："), text
    assert "📍 SHEIN 希音｜HK0625USDT" in text and "昨收 38.1 → 估算 38.3（+0.52%）" in text and "（按币安代理估算，竞价参考价出来后会更新）" in text, text
    assert "Predict 买1 50.0¢｜卖1 53.0¢" in text and "宇树" not in text and "海力士" not in text, text
    assert text.endswith("└ 看竞价行情：富途 https://www.futunn.com/stock/00625-HK｜AAStocks https://www.aastocks.com/tc/stocks/quote/detail-quote.aspx?symbol=00625"
                         "｜腾讯 https://gu.qq.com/hk00625｜etnet https://www.etnet.com.hk/www/tc/stocks/realtime/quote.php?code=625"
                         "｜港交所 https://www.hkex.com.hk/Market-Data/Securities-Prices/Equities/Equities-Quote?sym=625&sc_lang=zh-HK"), text
    now["ms"] = bj(10, 8, 8, 56)
    await pbot.preopen_reminders(now["ms"]); await pbot.drain_deliveries(); assert len(sent) == 1  # not again the same day
    # the A-share one ten minutes later (09:10) lists the A-share card; Korea's came at 07:25 Beijing (08:25 Seoul)
    now["ms"] = bj(10, 8, 9, 10, 15)
    await pbot.preopen_reminders(now["ms"]); await pbot.drain_deliveries()
    assert len(sent) == 2 and plain(sent[1]).startswith("🔔 沪深开盘集合竞价 5 分钟后开始（09:15–09:25") and "宇树 UNITREE" in sent[1] and "SHEIN" not in sent[1], sent[1]
    assert "看竞价行情：腾讯 https://gu.qq.com/sh688836｜东方财富 https://quote.eastmoney.com/sh688836.html｜富途 https://www.futunn.com/stock/688836-SH" in sent[1], sent[1]
    now["ms"] = bj(10, 8, 7, 25, 40)
    await pbot.preopen_reminders(now["ms"]); await pbot.drain_deliveries()
    assert len(sent) == 3 and plain(sent[2]).startswith("🔔 韩交所开盘同时呼价 5 分钟后开始（首尔 08:30–09:00") and "概率暂缺：等待行情" in sent[2], sent[2]
    assert "Naver https://finance.naver.com/item/main.naver?code=000660" in sent[2]
    # a restart that joins late (3 minutes after the moment) does not send a stale reminder; a holiday has none
    late = m.Bot(pcfg, m.Store(":memory:"), FakeMarket(), FakeTelegram()); late.store.put("subscriptions", pbot.subscriptions()); late.odds_items = pbot.odds_items
    now["ms"] = bj(10, 8, 8, 58, 30)
    await late.preopen_reminders(now["ms"]); await late.drain_deliveries(); assert len(sent) == 3
    now["ms"] = bj(10, 1, 8, 55, 30)  # National Day: HKEX shut
    await late.preopen_reminders(now["ms"]); await late.drain_deliveries(); assert len(sent) == 3
    # lead 0: at the start; PREOPEN_ALERT=off: nothing
    zcfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "HK0625USDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off", "PREOPEN_ALERT_LEAD_MINUTES": "0"})
    zbot = m.Bot(zcfg, m.Store(":memory:"), FakeMarket(), FakeTelegram()); zbot.store.put("subscriptions", pbot.subscriptions()); zbot.odds_items = pbot.odds_items
    now["ms"] = bj(10, 9, 9, 0, 20)
    await zbot.preopen_reminders(now["ms"]); await zbot.drain_deliveries()
    assert len(sent) == 4 and plain(sent[3]).startswith("🔔 港交所开市前竞价 开始（09:00–09:30"), sent[3]
    ocfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "HK0625USDT", "PREOPEN_ALERT": "off"})
    obot = m.Bot(ocfg, m.Store(":memory:"), FakeMarket(), FakeTelegram()); obot.store.put("subscriptions", pbot.subscriptions()); obot.odds_items = pbot.odds_items
    now["ms"] = bj(10, 12, 8, 55, 20)
    await obot.preopen_reminders(now["ms"]); await obot.drain_deliveries(); assert len(sent) == 4
    # the web card: an orange 开市前竞价 tag while the auction runs (the contract cards only), and the quote pages always
    now["ms"] = bj(10, 8, 9, 5)
    items = {i["name"]: i for i in pbot.odds_payload()["items"]}
    assert items["SHEIN 希音"]["preopen"].startswith("港交所开市前竞价") and items["SHEIN 希音"]["preopen_phase"] == "可撤单" and "trading" not in items["SHEIN 希音"], items["SHEIN 希音"]
    assert "preopen_price" not in items["SHEIN 希音"]  # no quote read: nothing to show
    assert items["SHEIN 希音"]["pages"][0] == {"name": "富途", "url": "https://www.futunn.com/stock/00625-HK"} and len(items["SHEIN 希音"]["pages"]) == 5
    assert items["宇树 UNITREE"]["trading"] == "未开盘" and "preopen" not in items["宇树 UNITREE"] and items["宇树 UNITREE"]["pages"][0]["name"] == "腾讯"
    now["ms"] = bj(10, 8, 9, 20)
    items = {i["name"]: i for i in pbot.odds_payload()["items"]}
    assert items["宇树 UNITREE"]["preopen"].startswith("沪深开盘集合竞价") and items["宇树 UNITREE"]["preopen_phase"] == "不可撤单" and items["SHEIN 希音"]["preopen_phase"] == "随机撮合"
    now["ms"] = bj(10, 8, 9, 26)
    assert {i["name"]: i for i in pbot.odds_payload()["items"]}["宇树 UNITREE"]["preopen_phase"] == "已撮合"
    now["ms"] = bj(10, 8, 9, 31)
    items = {i["name"]: i for i in pbot.odds_payload()["items"]}
    assert "preopen" not in items["SHEIN 希音"] and items["SHEIN 希音"]["trading"] == "开盘中"

    # --- the indicative price's own message: once orders cannot be withdrawn and a card prices off it; once per venue and day
    now["ms"] = bj(10, 8, 9, 5)
    await pbot.preopen_price_alerts(now["ms"]); await pbot.drain_deliveries(); assert len(sent) == 4  # the card still rests on Binance
    iep = m.close_odds("SHEIN 希音", D("38.10"), D("38.30"), 0.025, 1.09, dt.date(2026, 10, 8), D("0.01"), "10-07 收盘",
                       "港交所开市前竞价参考价 38.3（腾讯·09:16 更新；不可撤单阶段，撮合前仍会变）", "σ")
    assert iep.preopen and iep.direct and not iep.matched
    pbot.odds_items = lambda ms: [("SHEIN 希音｜HK0625USDT", iep), ("宇树 UNITREE｜UNITREEUSDT", uni), ("SK 海力士｜SKHYNIXUSDT", "等待行情")]
    pbot.predict.books["HK0625USDT"] = m.dataclasses.replace(pbot.predict.books["HK0625USDT"], fetched_ms=now["ms"])
    await pbot.preopen_price_alerts(now["ms"]); await pbot.drain_deliveries(); assert len(sent) == 4  # 09:05: orders can still be withdrawn
    now["ms"] = bj(10, 8, 9, 16)
    pbot.predict.books["HK0625USDT"] = m.dataclasses.replace(pbot.predict.books["HK0625USDT"], fetched_ms=now["ms"])
    await pbot.preopen_price_alerts(now["ms"]); await pbot.drain_deliveries()
    assert len(sent) == 5, sent[4:]
    text = plain(sent[4])
    assert text.startswith("📊 港交所开市前竞价：不可撤单阶段，参考平衡价（09:16）\n撤不了单了，参考价比可撤单时可信"), text
    assert "📍 SHEIN 希音｜HK0625USDT\n├ 昨收 38.1 → 竞价参考 38.3（+0.52%）｜腾讯·09:16 更新；不可撤单阶段，撮合前仍会变\n├ 模型 涨 " in text, text
    assert "Predict 买1 50.0¢｜卖1 53.0¢" in text and "看竞价行情：富途 https://www.futunn.com/stock/00625-HK" in text and "宇树" not in text, text
    now["ms"] = bj(10, 8, 9, 21)
    await pbot.preopen_price_alerts(now["ms"]); await pbot.drain_deliveries(); assert len(sent) == 5  # once a day
    # the A-share venue: its cards have no indicative price (Unitree still prices off Binance): nothing
    now["ms"] = bj(10, 8, 9, 21)
    await pbot.preopen_price_alerts(now["ms"]); await pbot.drain_deliveries(); assert len(sent) == 5
    # a matched open (another day) is announced as such
    opened = m.close_odds("SHEIN 希音", D("38.10"), D("38.25"), 0.025, 1.0, dt.date(2026, 10, 9), D("0.01"), "10-08 收盘",
                          "港交所开盘价已撮合 38.25（腾讯·09:21 更新；09:30 起连续交易）", "σ")
    assert opened.matched and opened.direct and not opened.preopen and "（开盘价 38.25·σ" in opened.row()
    pbot.odds_items = lambda ms: [("SHEIN 希音｜HK0625USDT", opened)]
    now["ms"] = bj(10, 9, 9, 23)
    pbot.predict.books["HK0625USDT"] = m.dataclasses.replace(pbot.predict.books["HK0625USDT"], fetched_ms=now["ms"])
    await pbot.preopen_price_alerts(now["ms"]); await pbot.drain_deliveries()
    assert len(sent) == 6 and plain(sent[5]).startswith("📊 港交所开市前竞价：开盘价已撮合（09:23）\n开盘价定了") and "昨收 38.1 → 开盘价 38.25（+0.39%）" in plain(sent[5]), sent[5]
    # a venue whose auction is over sends none even with such odds lingering
    fresh_bot = m.Bot(pcfg, m.Store(":memory:"), FakeMarket(), FakeTelegram()); fresh_bot.store.put("subscriptions", pbot.subscriptions()); fresh_bot.odds_items = pbot.odds_items
    now["ms"] = bj(10, 8, 9, 31)
    await fresh_bot.preopen_price_alerts(now["ms"]); await fresh_bot.drain_deliveries(); assert len(sent) == 6

asyncio.run(run())
print("AUCTION_OK")
