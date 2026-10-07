"""Edge alerts on Telegram: a card's suggestion reaching 10¢ is announced (新机会) once it has held a minute; later the
announced side no longer being suggested (建议失效) or the other side being suggested instead (方向反转) is announced
too, with a reminder to check any order placed on it. A stale book or a card that holds back changes nothing; the same
side is not announced as new again for 15 minutes; a failed send is retried while the announcement is young."""
import asyncio, re, sys, time, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
BJ = lambda mo, d, h, mi=0, s=0: int(dt.datetime(2026, mo, d, h, mi, s, tzinfo=m.BEIJING).timestamp() * 1000)
NOW = BJ(10, 5, 10, 0)
MIN = 60_000
SLUG = "hang-seng-index-up-or-down-on-october-5-2026"

# --- settings ----------------------------------------------------------------------------------------------------------
c = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x"})
assert c.edge_alert and c.edge_alert_edge == 0.10 and c.edge_alert_confirm == 60 and c.edge_alert_cooldown == 900
c = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "EDGE_ALERT": "off", "EDGE_ALERT_CENTS": "15",
                       "EDGE_ALERT_CONFIRM_SECONDS": "0", "EDGE_ALERT_COOLDOWN_SECONDS": "3600"})
assert not c.edge_alert and abs(c.edge_alert_edge - 0.15) < 1e-12 and c.edge_alert_confirm == 0 and c.edge_alert_cooldown == 3600
for bad in ({"EDGE_ALERT_CENTS": "0"}, {"EDGE_ALERT_CENTS": "60"}, {"EDGE_ALERT_CONFIRM_SECONDS": "-1"},
            {"EDGE_ALERT_COOLDOWN_SECONDS": "x"}):
    try: m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", **bad}); assert False, bad
    except ValueError: pass

# --- the state machine on its own ----------------------------------------------------------------------------------------
E = lambda side, edge, maker=True: m.BookEdge(side, maker, 0.5, edge, 100.0, edge)
watch = lambda st, sides, best, at: m.edge_watch(st, sides, best, at, 0.10, MIN, 15 * MIN)
up = lambda e: {"up": e, "down": None}
down = lambda e: {"up": None, "down": e}
none = {"up": None, "down": None}
st = {}
assert watch(st, None, None, NOW) == "" and st["pending"] is None                    # nothing known: nothing moves
assert watch(st, down(E("跌", 0.12)), E("跌", 0.12), NOW) == ""                       # must hold a minute first
assert watch(st, down(E("跌", 0.12)), E("跌", 0.12), NOW + 30_000) == ""
assert watch(st, down(E("跌", 0.05)), E("跌", 0.05), NOW + 40_000) == ""             # under the line: starts again
assert st["pending"] is None
assert watch(st, down(E("跌", 0.11)), E("跌", 0.11), NOW + 50_000) == ""
assert watch(st, None, None, NOW + 70_000) == "" and st["pending"] is None            # a stale book in between: again
assert watch(st, down(E("跌", 0.11)), E("跌", 0.11), NOW + 80_000) == ""
assert watch(st, down(E("跌", 0.11)), E("跌", 0.11), NOW + 140_000) == "appear:down"
assert st["told"] == "down" and st["seq"] == 1 and st["last"] == {"down": NOW + 140_000}
# announced: a dip under 10¢ is no change while the side is still suggested; nothing at all for a minute is
assert watch(st, down(E("跌", 0.03)), E("跌", 0.03), NOW + 4 * MIN) == ""
assert watch(st, none, None, NOW + 5 * MIN) == "" and st["pending"]["want"] == "gone:down"
assert watch(st, down(E("跌", 0.03)), E("跌", 0.03), NOW + 5 * MIN + 30_000) == "" and st["pending"] is None  # back: no change
assert watch(st, none, None, NOW + 6 * MIN) == ""
assert watch(st, none, None, NOW + 7 * MIN) == "gone:down" and st["told"] is None and st["seq"] == 2
# the same side again within 15 minutes of its announcement: not new; after that, it is
assert watch(st, down(E("跌", 0.20)), E("跌", 0.20), NOW + 8 * MIN) == ""
assert watch(st, down(E("跌", 0.20)), E("跌", 0.20), NOW + 10 * MIN) == "" and st["pending"] is None
assert watch(st, down(E("跌", 0.20)), E("跌", 0.20), NOW + 17 * MIN) == "" and st["pending"] is None  # 14m40s on
assert watch(st, down(E("跌", 0.20)), E("跌", 0.20), NOW + 18 * MIN) == "" and st["pending"]["since"] == NOW + 18 * MIN
assert watch(st, down(E("跌", 0.20)), E("跌", 0.20), NOW + 19 * MIN) == "appear:down" and st["seq"] == 3
# the other side suggested instead: a flip; the new side counts as announced only at 10¢ or more
assert watch(st, up(E("涨", 0.04)), E("涨", 0.04), NOW + 20 * MIN) == ""
assert watch(st, up(E("涨", 0.04)), E("涨", 0.04), NOW + 21 * MIN) == "flip:up" and st["told"] is None and "up" not in st["last"]
flip = {"told": "down", "last": {}}
assert watch(flip, up(E("涨", 0.13)), E("涨", 0.13), NOW) == "" and watch(flip, up(E("涨", 0.13)), E("涨", 0.13), NOW + MIN) == "flip:up"
assert flip["told"] == "up" and flip["last"] == {"up": NOW + MIN} and flip["seq"] == 1
# both sides suggested (a wide spread): the announced one still is, no change
assert watch(flip, {"up": E("涨", 0.12), "down": E("跌", 0.15)}, E("跌", 0.15), NOW + 5 * MIN) == "" and flip["pending"] is None
# no confirmation wanted: at once
quick = {}
assert m.edge_watch(quick, up(E("涨", 0.10)), E("涨", 0.10), NOW, 0.10, 0, 0) == "appear:up"


# --- the bot: markets as the cards price them, two chats (one active) --------------------------------------------------
class FM:
    def __init__(self, now): self.now, self.config = now, None
    def now_ms(self): return self.now


class FakeTelegram:
    def __init__(self): self.sent, self.fail = [], 0
    async def call(self, *a, **k): return True
    async def send(self, chat, thread, text, reply_markup=None, parse_mode=None):
        if self.fail:
            self.fail -= 1
            raise m.RemoteError("Telegram 429")
        assert parse_mode == "HTML"
        self.sent.append((chat, re.sub(r"</?b>", "", text)))
        return True


def book(bids, asks, at):
    return m.PredictBook("HSI", SLUG, "1", "t", tuple((D(p), D(q)) for p, q in bids), tuple((D(p), D(q)) for p, q in asks), at, 200)


def hsi(fair, bids, asks, at, hold=""):
    return m.SimMarket(SLUG, "恒生指数", "close", "HSI", fair, book(bids, asks, at), 0.02, hold, ("涨", "跌"),
                       {"key": "HSI", "target": "2026-10-05", "line": 24600.0, "close_ms": BJ(10, 5, 16, 10)})


async def step(bot, at, *markets):
    bot.market.now, bot.edge_ran = at, -1e9  # the alerts' own pacing runs on the monotonic clock
    bot.sim_markets = lambda now: list(markets)
    result = await bot.edge_alerts(at)
    await bot.drain_deliveries()
    return result


async def run():
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off"})
    tg, store = FakeTelegram(), m.Store(":memory:")
    bot = m.Bot(cfg, store, FM(NOW), tg)
    assert "优势提醒" in [n for n, _ in bot.reference_jobs()]
    off = m.Bot(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "EDGE_ALERT": "off"}), m.Store(":memory:"), FM(NOW), tg)
    assert "优势提醒" not in [n for n, _ in off.reference_jobs()]
    assert "🔔 优势提醒：Predict 建议净优势 ≥10¢ 持续 60 秒提醒" in bot.status("1:0") and "优势提醒" not in off.status("1:0")
    nopredict = m.Bot(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "PREDICT": "off"}), m.Store(":memory:"), FM(NOW), tg)
    assert "优势提醒" not in [n for n, _ in nopredict.reference_jobs()]
    store.put("subscriptions", {"1:0": {"chat": 1, "thread": 0, "active": True}, "2:0": {"chat": 2, "thread": 0, "active": False}})

    # HSI: model 涨 40¢, Yes bid 45¢ / ask 48¢ → 挂跌 @ 52¢ +8¢ (under the 10¢ line): nothing to announce, nothing saved
    assert await step(bot, NOW, hsi(0.40, [("0.45", "300")], [("0.48", "300")], NOW)) == m.Refreshed("ok")
    assert await step(bot, NOW + 2 * MIN, hsi(0.40, [("0.45", "300")], [("0.48", "300")], NOW + 2 * MIN)) == m.Refreshed("ok")
    assert tg.sent == [] and store.get("edgealerts") is None
    assert await bot.edge_alerts(NOW + 2 * MIN) is False  # paced: not due again yet
    # the model moves to 涨 28¢: 挂跌 @ 52¢ = 72 − 52 = +20¢ (吃跌 @ 55¢: 72 − 55 − 0.9 fee = +16.1¢)
    t = NOW + 3 * MIN
    await step(bot, t, hsi(0.28, [("0.45", "300")], [("0.48", "300")], t))
    assert tg.sent == []  # it has to hold a minute
    await step(bot, t + MIN, hsi(0.28, [("0.45", "300")], [("0.48", "300")], t + MIN))
    assert len(tg.sent) == 1 and tg.sent[0][0] == 1, tg.sent  # the inactive chat gets nothing
    text = tg.sent[0][1]
    assert text.startswith("🟢 新机会｜恒生指数（10-05）\n挂跌 @ 52.0¢｜净优势 +20.0¢（模型 72.0¢）\n挂单：排队等成交，不保证成交"), text
    assert "也可以：吃跌 @ 55.0¢ +16.1¢" in text and text.endswith(m.predict_url(SLUG, "B00EA")), text
    # it stays: nothing more, however long; a dip under 10¢ while still suggested: nothing either
    for i in range(2, 5):
        await step(bot, t + i * MIN, hsi(0.28, [("0.45", "300")], [("0.48", "300")], t + i * MIN))
    await step(bot, t + 5 * MIN, hsi(0.45, [("0.45", "300")], [("0.48", "300")], t + 5 * MIN))  # 挂跌 +3¢
    await step(bot, t + 7 * MIN, hsi(0.45, [("0.45", "300")], [("0.48", "300")], t + 7 * MIN))
    assert len(tg.sent) == 1
    # a stale book and a card that holds back say nothing about it
    await step(bot, t + 8 * MIN, hsi(0.52, [("0.45", "300")], [("0.48", "300")], t))
    await step(bot, t + 10 * MIN, hsi(0.52, [("0.45", "300")], [("0.48", "300")], t + 10 * MIN, hold="期货锚点是近似值"))
    await step(bot, t + 12 * MIN)  # the card is gone for a while (e.g. its odds are missing)
    assert len(tg.sent) == 1 and store.get("edgealerts")[SLUG]["pending"] is None
    # the model moves to 涨 49¢: 跌 is no longer suggested (挂跌 51 − 52 = −1¢), 涨 is (挂涨 49 − 45 = +4¢): a flip,
    # once it has held a minute
    u = t + 13 * MIN
    await step(bot, u, hsi(0.49, [("0.45", "300")], [("0.48", "300")], u))
    tg.fail = 1  # the first try fails: retried on the next look
    await step(bot, u + MIN, hsi(0.49, [("0.45", "300")], [("0.48", "300")], u + MIN))
    assert len(tg.sent) == 1 and tg.fail == 0
    await step(bot, u + MIN + 20_000, hsi(0.49, [("0.45", "300")], [("0.48", "300")], u + MIN))
    assert len(tg.sent) == 2, tg.sent
    text = tg.sent[1][1]
    first = m.hhmm(t + MIN)
    assert text.startswith(f"🔄 方向反转｜恒生指数（10-05）\n之前提醒：挂跌 @ 52.0¢ +20.0¢（{first}）\n"
                           "现在建议另一边：挂涨 @ 45.0¢｜净优势 +4.0¢（模型 49.0¢）\n⚠️ 如果按之前的提醒挂了单，请检查是否撤单或改价"), text
    await step(bot, u + 2 * MIN, hsi(0.49, [("0.45", "300")], [("0.48", "300")], u + 2 * MIN))
    assert len(tg.sent) == 2  # recorded once delivered: not sent again
    st = store.get("edgealerts")[SLUG]
    assert st["told"] is None and st["note"] is None and st["seq"] == 2  # +4¢ is no new opportunity: nothing announced

    # 挂跌 back above 10¢ within 15 minutes of its announcement: not new yet; after that it is
    v = u + 3 * MIN
    await step(bot, v, hsi(0.30, [("0.45", "300")], [("0.48", "300")], v))
    await step(bot, v + MIN, hsi(0.30, [("0.45", "300")], [("0.48", "300")], v + MIN))
    assert len(tg.sent) == 3 and tg.sent[2][1].startswith("🟢 新机会"), tg.sent  # 18 minutes after the first: allowed
    # then nothing suggested on any side: 建议失效, with the reminder about the order
    w = v + 2 * MIN
    await step(bot, w, hsi(0.465, [("0.45", "300")], [("0.48", "300")], w))  # 挂跌 +1.5¢ (under 2¢), 挂涨 +1.5¢
    await step(bot, w + MIN, hsi(0.465, [("0.45", "300")], [("0.48", "300")], w + MIN))
    assert len(tg.sent) == 4, tg.sent
    text = tg.sent[3][1]
    assert text.startswith(f"⚪ 建议失效｜恒生指数（10-05）\n之前提醒：挂跌 @ 52.0¢ +18.0¢（{m.hhmm(v + MIN)}）\n"
                           "现在这一边最好是 挂跌 +1.5¢，不到建议门槛 2.0¢，不再建议\n⚠️ 如果按之前的提醒挂了单"), text

    # a newer announcement replaces one not delivered yet; an old one is not sent at all
    x = w + 20 * MIN
    tg.fail = 10
    await step(bot, x, hsi(0.20, [("0.45", "300")], [("0.48", "300")], x))
    await step(bot, x + MIN, hsi(0.20, [("0.45", "300")], [("0.48", "300")], x + MIN))      # 新机会: fails
    tg.fail = 10
    await step(bot, x + 2 * MIN, hsi(0.465, [("0.45", "300")], [("0.48", "300")], x + 2 * MIN))
    await step(bot, x + 3 * MIN, hsi(0.465, [("0.45", "300")], [("0.48", "300")], x + 3 * MIN))  # 建议失效: fails too
    tg.fail = 0
    await step(bot, x + 4 * MIN, hsi(0.465, [("0.45", "300")], [("0.48", "300")], x + 4 * MIN))
    assert len(tg.sent) == 5 and tg.sent[4][1].startswith("⚪ 建议失效"), tg.sent  # only the latest
    store.put("edgesent:" + SLUG + ":1:0", 0)  # pretend it never arrived: ten minutes on it is too old to send
    await step(bot, x + 14 * MIN, hsi(0.465, [("0.45", "300")], [("0.48", "300")], x + 14 * MIN))
    assert len(tg.sent) == 5

    # the state survives a restart: a later change still refers to what was announced
    y = x + 30 * MIN
    await step(bot, y, hsi(0.20, [("0.45", "300")], [("0.48", "300")], y))
    await step(bot, y + MIN, hsi(0.20, [("0.45", "300")], [("0.48", "300")], y + MIN))
    assert len(tg.sent) == 6 and tg.sent[5][1].startswith("🟢 新机会")
    again = m.Bot(cfg, store, FM(y + 2 * MIN), tg)
    await step(again, y + 2 * MIN, hsi(0.465, [("0.45", "300")], [("0.48", "300")], y + 2 * MIN))
    await step(again, y + 3 * MIN, hsi(0.465, [("0.45", "300")], [("0.48", "300")], y + 3 * MIN))
    assert len(tg.sent) == 7 and f"之前提醒：挂跌 @ 52.0¢ +28.0¢（{m.hhmm(y + MIN)}）" in tg.sent[6][1], tg.sent[6]

    # other kinds use their card's names: a ladder level names its threshold, and Yes / No
    z = y + 5 * MIN
    lad = m.SimMarket("pons#77", "$牛来 市值 $300M", "ladder", "NIULAI", 0.30, m.PredictBook("NIULAI", "niulai-fdv", "77", "t",
                      ((D("0.10"), D("100")),), ((D("0.12"), D("100")),), z, 200), 0.02, "", ("Yes", "No"), {})
    await step(again, z, lad)
    await step(again, z + MIN, m.dataclasses.replace(lad, book=m.dataclasses.replace(lad.book, fetched_ms=z + MIN)))
    assert len(tg.sent) == 8 and tg.sent[7][1].startswith("🟢 新机会｜$牛来 市值 $300M\n挂Yes @ 10.0¢｜净优势 +20.0¢（模型 30.0¢）"), tg.sent[7]
    assert tg.sent[7][1].endswith(m.predict_url("niulai-fdv", "B00EA"))
    # /status counts the day's interruptions by kind (the bot's clock still stands on 10-05; the next day it is "昨日")
    assert "📣 今日优势提醒 8 条（新机会 4｜失效 3｜反转 1）｜昨日 0 条" in bot.status("1:0"), bot.status("1:0")
    assert store.get("alerts:2026-10-05") == {"appear": 4, "flip": 1, "gone": 3}
    # a market no card has priced for a day is forgotten, with its delivery records
    await step(again, z + m.DAY_MS + 2 * MIN)
    assert SLUG not in store.get("edgealerts") and "pons#77" not in store.get("edgealerts")
    assert not list(store.items("edgesent:"))
    assert "📣 今日优势提醒 0 条｜昨日 8 条" in again.status("1:0"), again.status("1:0")

    # --- digest mode: 新机会 gathered into one message per period, by driver; 失效 / 反转 still at once --------------------
    dcfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off",
                              "EDGE_ALERT_DIGEST_MINUTES": "30"})
    assert dcfg.edge_alert_digest == 30 and m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x"}).edge_alert_digest == 0
    try: m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "EDGE_ALERT_DIGEST_MINUTES": "2000"}); assert False
    except ValueError: pass
    dtg, dstore = FakeTelegram(), m.Store(":memory:")
    dbot = m.Bot(dcfg, dstore, FM(NOW), dtg)
    dstore.put("subscriptions", {"1:0": {"chat": 1, "thread": 0, "active": True}})
    assert "摘要模式：新机会每 30 分钟合并一条" in dbot.status("1:0")
    level = lambda mid, label, at: m.SimMarket(f"pons#{mid}", f"$牛来 市值 {label}", "ladder", "NIULAI", 0.30,
                                               m.PredictBook("NIULAI", "niulai-fdv", mid, "t", ((D("0.10"), D("100")),), ((D("0.12"), D("100")),), at, 200),
                                               0.02, "", ("Yes", "No"), {"target": "300000000" if label == "$300M" else "500000000", "end": BJ(11, 1, 0)})
    d0 = NOW
    await step(dbot, d0, level("1", "$300M", d0), level("2", "$500M", d0), hsi(0.28, [("0.45", "300")], [("0.48", "300")], d0))
    assert dtg.sent == []
    await step(dbot, d0 + MIN, level("1", "$300M", d0 + MIN), level("2", "$500M", d0 + MIN), hsi(0.28, [("0.45", "300")], [("0.48", "300")], d0 + MIN))
    assert len(dtg.sent) == 1, dtg.sent  # three 新机会 confirmed: one message, not three
    text = dtg.sent[0][1]
    assert text.startswith("📬 机会摘要｜最近 30 分钟出现、现在仍成立的新机会 3 个（2 组）\n▪ $牛来 市值（2 个）：同一标的，一次行情一起变\n"
                           "· $300M：挂Yes @ 10.0¢ +20.0¢（模型 30.0¢）\n· $500M：挂Yes @ 10.0¢ +20.0¢（模型 30.0¢）\n"
                           + m.predict_url("niulai-fdv", "B00EA") + "\n▪ 恒生指数（10-05）（1 个）\n· 恒生指数：挂跌 @ 52.0¢ +20.0¢（模型 72.0¢）\n"
                           + m.predict_url(SLUG, "B00EA")), text
    assert text.endswith("建议失效 / 方向反转仍会即时提醒。") and dstore.get("alerts:2026-10-05") == {"digest": 1, "digest_items": 3}
    assert all(dstore.get(f"edgesent:{k}:1:0") == 1 for k in ("pons#1", "pons#2", SLUG)) and dstore.get("edgedigest:1:0") == d0 + MIN
    # the HSI flip goes at once; a new 新机会 inside the period waits for the next digest, which lists it alone
    await step(dbot, d0 + 2 * MIN, hsi(0.49, [("0.45", "300")], [("0.48", "300")], d0 + 2 * MIN), level("1", "$300M", d0 + 2 * MIN), level("2", "$500M", d0 + 2 * MIN))
    await step(dbot, d0 + 3 * MIN, hsi(0.49, [("0.45", "300")], [("0.48", "300")], d0 + 3 * MIN), level("1", "$300M", d0 + 3 * MIN),
               level("2", "$500M", d0 + 3 * MIN), level("3", "$1B", d0 + 3 * MIN))
    assert len(dtg.sent) == 2 and dtg.sent[1][1].startswith("🔄 方向反转｜恒生指数（10-05）"), dtg.sent[1:]
    await step(dbot, d0 + 4 * MIN, level("1", "$300M", d0 + 4 * MIN), level("2", "$500M", d0 + 4 * MIN), level("3", "$1B", d0 + 4 * MIN))
    await step(dbot, d0 + 10 * MIN, level("1", "$300M", d0 + 10 * MIN), level("2", "$500M", d0 + 10 * MIN), level("3", "$1B", d0 + 10 * MIN))
    assert len(dtg.sent) == 2  # the $1B 新机会 is confirmed but held for the digest
    await step(dbot, d0 + 32 * MIN, level("1", "$300M", d0 + 32 * MIN), level("2", "$500M", d0 + 32 * MIN), level("3", "$1B", d0 + 32 * MIN))
    assert len(dtg.sent) == 3 and dtg.sent[2][1].startswith("📬 机会摘要｜最近 30 分钟出现、现在仍成立的新机会 1 个（1 组）\n▪ $牛来 市值（1 个）\n· $1B：挂Yes"), dtg.sent[2]
    assert "📣 今日优势提醒 3 条（反转 1｜摘要 2 条含 4 个）｜昨日 0 条｜摘要模式" in dbot.status("1:0"), dbot.status("1:0")
    # a chance gone before the digest is sent is left out of it; nothing left = nothing sent, the period stays open
    await step(dbot, d0 + 33 * MIN, level("1", "$300M", d0 + 33 * MIN), level("2", "$500M", d0 + 33 * MIN), level("3", "$1B", d0 + 33 * MIN),
               hsi(0.28, [("0.45", "300")], [("0.48", "300")], d0 + 33 * MIN))
    await step(dbot, d0 + 34 * MIN, level("1", "$300M", d0 + 34 * MIN), level("2", "$500M", d0 + 34 * MIN), level("3", "$1B", d0 + 34 * MIN),
               hsi(0.28, [("0.45", "300")], [("0.48", "300")], d0 + 34 * MIN))  # HSI 挂跌 is new again (cooldown long over)
    await step(dbot, d0 + 63 * MIN, level("1", "$300M", d0 + 63 * MIN), level("2", "$500M", d0 + 63 * MIN), level("3", "$1B", d0 + 63 * MIN))
    assert len(dtg.sent) == 3  # at the next period the HSI card is gone: its pending line is dropped and no digest goes out

    # --- /edge: the bar per section, market or level (most specific wins), announced accordingly ------------------------
    class AnyTelegram(FakeTelegram):
        async def send(self, chat, thread, text, reply_markup=None, parse_mode=None):
            self.sent.append((chat, re.sub(r"</?b>", "", text))); return True
    acfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ADMIN_USER_ID": "42", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off"})
    atg = AnyTelegram(); abot = m.Bot(acfg, m.Store(":memory:"), FM(NOW), atg)
    abot.store.put("subscriptions", {"1:0": {"chat": 1, "thread": 0, "active": True}})
    async def ask(text):
        atg.sent.clear(); await abot.process_message({"text": text, "chat": {"id": 1}, "from": {"id": 42}, "date": time.time()}); return atg.sent[-1][1]
    mk_hsi, mk_lad = hsi(0.40, [("0.45", "300")], [("0.48", "300")], NOW), lad
    mk_unitree = m.dataclasses.replace(mk_hsi, market="uni", item="宇树 UNITREE", key="UNITREEUSDT")
    assert abot.edge_group(mk_hsi) == "index" and abot.edge_group(mk_unitree) == "contract" and abot.edge_group(mk_lad) == "ladder"
    assert abot.edge_level(mk_lad) == "300m" and abot.edge_level(mk_hsi) == ""
    r = await ask("/edge"); assert r.startswith("🔔 优势提醒门槛：默认 ≥10¢") and "没有单独设置" in r, r
    r = await ask("/edge 恒指 5"); assert r.startswith("✅ 恒生指数 的提醒门槛改为 ≥5¢") and abot.settings()["edge_bars"] == {"key:HSI": 5.0}, r
    assert abs(abot.edge_bar(mk_hsi) - 0.05) < 1e-12 and abs(abot.edge_bar(mk_unitree) - 0.10) < 1e-12
    r = await ask("/edge 市值阶梯 6"); assert "市值阶梯 的提醒门槛改为 ≥6¢" in r and abs(abot.edge_bar(mk_lad) - 0.06) < 1e-12, r
    r = await ask("/edge 牛来 300M 3"); assert "$牛来 市值 300M 的提醒门槛改为 ≥3¢" in r and abs(abot.edge_bar(mk_lad) - 0.03) < 1e-12, r
    other = m.dataclasses.replace(mk_lad, item="$牛来 市值 $500M"); assert abs(abot.edge_bar(other) - 0.06) < 1e-12  # the section's bar
    r = await ask("/edge 7"); assert "默认门槛改为 ≥7¢" in r and abs(abot.edge_bar(mk_unitree) - 0.07) < 1e-12, r
    assert abs(abot.edge_default() - 0.07) < 1e-12  # the alert text measures "its own bar" against this, not the env default
    r = await ask("/edge 宇树 4.5¢"); assert abs(abot.edge_bar(mk_unitree) - 0.045) < 1e-12, r  # a symbol alias, the ¢ sign tolerated
    r = await ask("/edge 牛来 300M off"); assert "已取消 $牛来 市值 300M 的单独门槛" in r and abs(abot.edge_bar(mk_lad) - 0.06) < 1e-12, r
    r = await ask("/edge 牛来 off"); assert r.startswith("ℹ️ $牛来 市值 没有单独的门槛"), r
    for bad in ("/edge 牛来 99", "/edge 不存在的市场 5", "/edge 牛来 abc 3", "/edge x"):
        assert "❌" in await ask(bad), bad
    assert "档位 > 市场 > 栏目 > 默认" in await ask("/edge") and "恒生指数：≥5¢" in await ask("/edge")
    assert "单独门槛：恒生指数 5¢" in abot.status("1:0") and "/edge 调整" in abot.status("1:0")
    # the HSI 挂跌 +8¢ that stayed under the 10¢ default is announced with the 5¢ bar, and says which bar applied
    await step(abot, NOW, mk_hsi); await step(abot, NOW + MIN, hsi(0.40, [("0.45", "300")], [("0.48", "300")], NOW + MIN))
    assert any(t.startswith("🟢 新机会｜恒生指数（10-05）\n挂跌 @ 52.0¢｜净优势 +8.0¢") and "提醒门槛 5.0¢" in t for _, t in atg.sent), atg.sent
    r = await ask("/edge 清空"); assert "已取消全部单独门槛" in r and abot.settings()["edge_bars"] == {} and abs(abot.edge_bar(mk_hsi) - 0.10) < 1e-12
    assert abs(abot.edge_default() - 0.10) < 1e-12


asyncio.run(run())
print("EDGEALERT_OK")
