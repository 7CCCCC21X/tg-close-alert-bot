"""Telegram delivery: sends run beside the sampling loop, every message re-checks its data right before it goes out
(also after a cool-down in the send queue), and nothing is recorded as sent before Telegram accepted it."""
import asyncio, sys, time, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D


class RecTelegram(m.Telegram):
    """The real queue (lock, pacing, cool-down, freshness check); the network is faked."""
    def __init__(self):
        super().__init__("1:x"); self.sent, self.fail = [], {}
    async def call(self, method, payload=None, timeout=15):
        chat = (payload or {}).get("chat_id")
        if self.fail.get(chat):
            self.fail[chat] -= 1
            raise m.RemoteError("Telegram: Too Many Requests: retry after 1", 1)
        self.sent.append((chat, payload["text"]))
        return True


class Market:  # a fixed clock (Tuesday 09-29 10:00 Beijing) so no run straddles midnight
    def __init__(self, cfg):
        self.config, self.price = cfg, "76"
        self.clock = int(dt.datetime(2026, 9, 29, 10, 0, tzinfo=m.BEIJING).timestamp() * 1000)
    def now_ms(self): return self.clock
    async def sync_clock(self): pass
    async def prices(self): return {s: {"symbol": s, "price": self.price, "time": self.clock} for s in self.config.symbols}


async def run():
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ADMIN_USER_ID": "42", "SYMBOLS": "UNITREEUSDT", "BASELINE_MODE": "manual",
                             "MIN_ALERT_GAP_SECONDS": "0", "MAX_PRICE_AGE_SECONDS": "120", "PROBABILITY": "off"})
    tg = RecTelegram(); market = Market(cfg)
    bot = m.Bot(cfg, m.Store(":memory:"), market, tg)
    day = m.beijing_day(market.clock / 1000)
    bot.store.put(f"manual:UNITREEUSDT:{day}", {"value": "75", "valid_date": day})
    bot.store.put("subscriptions", {"1:0": {"chat": 1, "thread": 0, "active": True}})
    bot.reference_tasks = [object()]  # production mode: deliveries run in the background

    # --- the review's case: a cool-down in Telegram's queue outlived the quote -> dropped, never sent late ---------
    tg.next_send = time.monotonic() + 0.6  # e.g. retry_after from a 429 a moment ago
    started = time.monotonic()
    await bot.one_cycle()
    assert time.monotonic() - started < 0.3, "sampling must not wait for Telegram"
    assert bot.delivering("alert:1:0:UNITREEUSDT") and not tg.sent
    await bot.one_cycle()  # the next sample runs at once; the alert in flight is not planned twice
    assert len([t for t in bot.deliveries.values() if not t.done()]) == 1
    market.clock += 180_000                # the queue's wait lasts ~3 minutes of market time
    market.price = "76.10"
    await bot.drain_deliveries()
    assert not tg.sent, tg.sent            # the 180-second-old quote was not delivered
    assert "side" not in bot.store.get("alert:1:0:UNITREEUSDT", {}) or bot.store.get("alert:1:0:UNITREEUSDT")["side"] == 0
    await bot.one_cycle(); await bot.drain_deliveries()  # the next sample re-plans with the fresh quote
    assert len(tg.sent) == 1 and "币安 <b>76.1</b>" in tg.sent[0][1] and "（0 秒前）" in tg.sent[0][1], tg.sent
    assert bot.store.get("alert:1:0:UNITREEUSDT")["side"] == 1

    # --- the move that triggered an alert is gone by the time its turn comes: dropped -------------------------------
    market.price = "73"                     # reverse: -2.67% -> a new alert is planned
    tg.next_send = time.monotonic() + 0.4
    await bot.one_cycle()
    assert bot.delivering("alert:1:0:UNITREEUSDT")
    market.price, market.clock = "75.2", market.clock + 5_000   # back inside the band before it is sent
    await bot.one_cycle()                   # the sampler keeps the latest reading
    await bot.drain_deliveries()
    assert len(tg.sent) == 1, tg.sent       # nothing about a -2.67% move that no longer holds

    # --- a failed send is not recorded: the next cycle tries again ----------------------------------------------------
    market.price = "72.9"; market.clock += 5_000
    tg.fail[1] = 1
    await bot.one_cycle(); await bot.drain_deliveries()
    assert len(tg.sent) == 1 and bot.store.get("alert:1:0:UNITREEUSDT")["side"] != -1
    tg.next_send = 0
    await bot.one_cycle(); await bot.drain_deliveries()
    assert len(tg.sent) == 2 and "下跌超过" in tg.sent[1][1] and bot.store.get("alert:1:0:UNITREEUSDT")["side"] == -1, tg.sent

    # --- closing-auction reminder: per subscription, recorded only after Telegram accepted it -----------------------
    bj = lambda h, mi, s=0: int(dt.datetime(2026, 9, 29, h, mi, s, tzinfo=m.BEIJING).timestamp() * 1000)
    acfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off"})
    atg = RecTelegram(); amarket = Market(acfg); amarket.clock = bj(14, 57, 10)
    abot = m.Bot(acfg, m.Store(":memory:"), amarket, atg)
    abot.store.put("subscriptions", {"1:0": {"chat": 1, "thread": 0, "active": True}, "2:0": {"chat": 2, "thread": 0, "active": True}})
    sse = m.close_odds("上证指数", D("3823.62"), D("3830.10"), 0.01, 0.002, dt.date(2026, 9, 29), D("0.01"), "09-28 收盘", "x", "σ")
    abot.odds_items = lambda ms: [("上证指数", sse)]
    atg.fail[1] = 1                                     # chat 1's copy fails once
    await abot.auction_reminders(amarket.clock); await abot.drain_deliveries()
    assert [c for c, _ in atg.sent] == [2], atg.sent    # chat 2 got it; chat 1 did not, and is not marked as sent
    assert abot.store.get("auction:sh:2026-09-29:2:0") and not abot.store.get("auction:sh:2026-09-29:1:0")
    atg.next_send = 0; amarket.clock = bj(14, 57, 15)
    await abot.auction_reminders(amarket.clock); await abot.drain_deliveries()
    assert [c for c, _ in atg.sent] == [2, 1], atg.sent  # the retry goes to chat 1 only
    await abot.auction_reminders(bj(14, 57, 20)); await abot.drain_deliveries()
    assert len(atg.sent) == 2                           # nobody gets it twice
    # a copy that waited in the queue past the auction's useful life is dropped (the next cycle builds a new one)
    abot.store.delete_prefix("auction:")
    atg.sent.clear(); atg.next_send = time.monotonic() + 0.3; amarket.clock = bj(14, 57, 30)
    await abot.auction_reminders(amarket.clock)
    amarket.clock = bj(14, 58, 45)                      # 75 s later when the queue frees up
    await abot.drain_deliveries()
    assert not atg.sent and not abot.store.get("auction:sh:2026-09-29:1:0"), atg.sent
    await abot.auction_reminders(amarket.clock); await abot.drain_deliveries()
    assert sorted(c for c, _ in atg.sent) == [1, 2]     # rebuilt with current data, still inside the 2-minute window
    # a record from an older version (one key for everybody) is honoured
    old = m.Bot(acfg, m.Store(":memory:"), amarket, atg); old.odds_items = abot.odds_items
    old.store.put("subscriptions", abot.subscriptions()); old.store.put("auction:sh:2026-09-29", bj(14, 57, 5))
    n = len(atg.sent)
    await old.auction_reminders(amarket.clock); await old.drain_deliveries()
    assert len(atg.sent) == n

    # --- notices go through the same queue and are recorded only once delivered --------------------------------------
    m.NOTICE_GRACE_SECONDS = 0
    ntg = RecTelegram(); nbot = m.Bot(cfg, m.Store(":memory:"), Market(cfg), ntg)
    sub = {"chat": 5, "thread": 0, "active": True}
    ntg.fail[5] = 1
    nbot.notice("5:0", sub, "X", "boom"); await nbot.drain_deliveries()
    assert not ntg.sent and nbot.store.get("notice:5:0:X") is None
    ntg.next_send = 0; ntg.chat_next.clear()
    nbot.notice("5:0", sub, "X", "boom"); await nbot.drain_deliveries()
    assert len(ntg.sent) == 1 and nbot.store.get("notice:5:0:X")["active"]

    # --- a fault is announced only once it has lasted the grace period; a blip (fault, then fine) says nothing ------
    m.NOTICE_GRACE_SECONDS = 0.2
    gtg = RecTelegram(); gbot = m.Bot(cfg, m.Store(":memory:"), Market(cfg), gtg)
    gbot.notice("5:0", sub, "X", "timeout"); await gbot.drain_deliveries()
    gbot.notice("5:0", sub, "X", None); await gbot.drain_deliveries()      # recovered before anyone heard of it
    assert not gtg.sent and not gbot.faults
    gbot.notice("5:0", sub, "X", "timeout"); await gbot.drain_deliveries()
    assert not gtg.sent
    await asyncio.sleep(0.25)
    gbot.notice("5:0", sub, "X", "timeout"); await gbot.drain_deliveries()  # still broken after the grace: announced
    assert len(gtg.sent) == 1 and "行情监控异常" in gtg.sent[0][1], gtg.sent
    gbot.notice("5:0", sub, "X", None); await gbot.drain_deliveries()
    assert len(gtg.sent) == 2 and "数据恢复" in gtg.sent[1][1], gtg.sent
    m.NOTICE_GRACE_SECONDS = 0

    # --- a chat Telegram refuses for good pauses its subscription instead of being retried every cycle --------------
    class GoneTelegram(RecTelegram):
        async def call(self, method, payload=None, timeout=15):
            if (payload or {}).get("chat_id") == 9:
                raise m.RemoteError("HTTP 403: Forbidden: bot was blocked by the user")
            return await super().call(method, payload, timeout)
    stg = GoneTelegram()
    scfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ADMIN_USER_ID": "1", "SYMBOLS": "UNITREEUSDT", "PROBABILITY": "off"})
    sbot = m.Bot(scfg, m.Store(":memory:"), Market(scfg), stg)
    sbot.store.put("subscriptions", {"9:0": {"chat": 9, "thread": 0, "active": True}, "1:0": {"chat": 1, "thread": 0, "active": True}})
    assert not await sbot.tell(9, 0, "hello"); await sbot.drain_deliveries()
    subs = sbot.subscriptions()
    assert not subs["9:0"]["active"] and subs["9:0"]["suspended"] == "bot was blocked by the user" and subs["1:0"]["active"], subs
    assert [c for c, _ in stg.sent] == [1] and "已自动暂停" in stg.sent[0][1], stg.sent  # the administrator was told
    assert "已自动暂停" in sbot.status("9:0")
    print("DELIVERY_OK")


asyncio.run(run())
