import asyncio, sys, time, datetime as dt
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

asyncio.run(run())
print("AUCTION_OK")
