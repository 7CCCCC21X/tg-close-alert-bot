"""Price-ladder maker alerts require fresh active LP rewards and a qualifying two-sided book.
The new stream does not hide / restrict taker alerts, alter other market kinds, or create fictitious paper fills.
"""
import asyncio
import datetime as dt
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import offline  # noqa: F401
import main as m

D = m.D
NOW = m.RANGE_MARKETS[0].start_ms + 2 * m.DAY_MS
MIN = 60_000
SPEC = m.RANGE_MARKETS[0]
MID = "reward-level"
MARKET = SPEC.slug + "#" + MID


def iso(at):
    return dt.datetime.fromtimestamp(at / 1000, dt.timezone.utc).isoformat()


def period(rate=100, start=NOW - MIN, end=NOW + m.DAY_MS):
    return {"hourlyRate": rate, "startsAt": iso(start), "endsAt": iso(end)}


def meta(current=None, **extra):
    return {"outcomes": ["Yes", "No"], "status": "REGISTERED", "trading_status": "OPEN",
            "rewards": {"current": current}, "spread_threshold": 0.20, "share_threshold": 100, **extra}


# The exact documented field is rewards.current; boosts / presence of a reward schedule do not establish activation.
assert m.predict_reward_status(meta(period()), NOW)["points_active"] is True
assert m.predict_reward_status(meta(period(start=NOW)), NOW)["points_active"] is True
assert m.predict_reward_status(meta(period(end=NOW)), NOW)["points_active"] is False
assert m.predict_reward_status(meta(period(start=NOW + 1)), NOW)["points_note"] == "积分未开始"
assert m.predict_reward_status(meta(period(end=NOW)), NOW)["points_note"] == "积分已结束"
assert m.predict_reward_status(meta(period(rate=0)), NOW)["points_active"] is False
assert m.predict_reward_status(meta(), NOW)["points_note"] == "积分未激活"
for unknown in ({"isBoosted": True}, {"rewards": {"schedule": [period()]}}, {"rewards": None},
                {"rewards": {"current": []}}, meta(period(rate="nan")), meta(period(rate="Infinity")),
                meta(period(rate=-1)), meta(period(start=NOW + 2, end=NOW + 1)),
                meta({**period(), "startsAt": "2026-10-03T12:00:00"}), meta({**period(), "endsAt": "broken"}),
                meta({"hourlyRate": 100})):
    assert m.predict_reward_status(unknown, NOW)["points_active"] is None, unknown
assert m.predict_reward_status(meta(rewards={"current": None, "schedule": [period()]}), NOW)["points_active"] is False


class Clock:
    config = None

    def __init__(self):
        self.at = NOW

    def now_ms(self):
        return self.at


class Telegram:
    def __init__(self):
        self.sent, self.fail = [], 0

    async def send(self, chat, thread, text, reply_markup=None, parse_mode=None):
        if self.fail:
            self.fail -= 1
            raise m.RemoteError("Telegram 429")
        self.sent.append(re.sub(r"</?b>", "", text))
        return True


def fixture():
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "PROBABILITY": "off",
                             "HSI_FUTURES": "off", "KOSPI_INDEX": "off",
                             "SIM_MARKETS": "range", "SIM_WAYS": "both"})  # the paper trader's default scope leaves ladders out
    clock, telegram = Clock(), Telegram()
    bot = m.Bot(cfg, m.Store(":memory:"), clock, telegram)
    bot.touches, bot.updowns, bot.flips, bot.caps = {}, {}, {}, {}
    rm = bot.ranges[SPEC.key]
    bot.ranges = {SPEC.key: rm}
    rm.price, rm.priced_ms, rm.sigma, rm.sigma_ms, rm.scanned_ms = D("112000"), NOW, 0.4, NOW, NOW
    rm.probability = lambda level, direction, now: 0.4
    rm.model_swing = lambda *args: 0.02
    rm.store.put("range:" + SPEC.slug, {"start": SPEC.start_ms, "through": NOW - NOW % 3_600_000,
                                      "open": 112000.0, "high": 115000.0, "low": 110000.0})
    book = m.PredictBook(SPEC.key, SPEC.slug, MID, "↑ $120k", ((D("0.20"), D("1000")),),
                         ((D("0.24"), D("1000")),), NOW, 200)
    row = m.LadderRow(D("120000"), MID, "↑ $120k", book)
    bot.predict.ladders[SPEC.key] = [row]
    bot.predict.market_meta[MID] = (meta(period()), time.monotonic())
    bot.store.put("subscriptions", {"1:0": {"chat": 1, "thread": 0, "active": True}})
    return bot, rm, row


def set_meta(bot, current, **extra):
    bot.predict.market_meta[MID] = (meta(current, **extra), time.monotonic())


async def step(bot, at):
    bot.market.at, bot.edge_ran = at, -1e9
    rm = bot.ranges[SPEC.key]
    rm.scanned_ms = rm.priced_ms = rm.sigma_ms = at
    row = bot.predict.ladders[SPEC.key][0]
    if row.book is not None:
        bot.predict.ladders[SPEC.key][0] = m.dataclasses.replace(row, book=m.dataclasses.replace(row.book, fetched_ms=at))
    await bot.edge_alerts(at)
    await bot.drain_deliveries()


async def run():
    bot, rm, row = fixture()
    # Every market's points reach its card: read with the fee (market details), trusted for PREDICT_POINTS_STALE_SECONDS
    other = m.PredictBook("SSE", "sse-up", "sse-1", "t", ((D("0.40"), D("100")),), ((D("0.45"), D("100")),), NOW, 200)
    async def details(url, payload=None):
        assert url.endswith("/markets/sse-1"), url
        return {"data": {"id": "sse-1", "feeRateBps": 150, "status": "OPEN", "tradingStatus": "OPEN", "spreadThreshold": 0.1, "shareThreshold": 100,
                         "outcomes": [{"name": "Up", "indexSet": 1}, {"name": "Down", "indexSet": 2}],
                         "rewards": {"current": {"hourlyRate": 55, "startsAt": iso(NOW - MIN), "endsAt": iso(NOW + m.DAY_MS)}}}}
    bot.predict.fetch = details
    assert await bot.predict.market_fee("sse-1") == 150 and bot.predict.market_meta["sse-1"][0]["rewards"]["current"]["hourlyRate"] == 55
    block = {}
    bot.book_block(block, other, 0.5, 0.02, 0.0, "", ("涨", "跌"), NOW)
    assert block["points_active"] is True and block["points_rate"] == 55 and block["points_note"] == "积分已激活", block
    assert block["points_ok"] is True and block["points_why"] == "" and block["points_spread"] == 0.1 and block["points_min_shares"] == 100
    # the programme pays, but the book is 34¢ wide against a 10¢ cap: a quote placed now earns nothing (the card shows ○)
    wide = m.PredictBook("SSE", "sse-up", "sse-1", "t", ((D("0.55"), D("100")),), ((D("0.89"), D("100")),), NOW, 200)
    block = {}; bot.book_block(block, wide, 0.5, 0.02, 0.0, "", ("涨", "跌"), NOW)
    assert block["points_active"] is True and block["points_rate"] == 55 and block["points_ok"] is False, block
    assert block["points_why"] == "价差 34.0¢ 超过积分上限 10.0¢", block["points_why"]
    # right at the cap (10¢ wide against 10¢) still earns on an ordinary card's book too: the ETH 1k/3k case ran through here
    exact = m.PredictBook("SSE", "sse-up", "sse-1", "t", ((D("0.55"), D("100")),), ((D("0.65"), D("100")),), NOW, 200)
    block = {}; bot.book_block(block, exact, 0.5, 0.02, 0.0, "", ("涨", "跌"), NOW)
    assert block["points_ok"] is True and block["points_why"] == "" and block["points_spread"] == 0.1, block
    bot.predict.market_meta["sse-1"] = (bot.predict.market_meta["sse-1"][0], time.monotonic() - m.PREDICT_POINTS_STALE_SECONDS - 1)
    block = {}; bot.book_block(block, other, 0.5, 0.02, 0.0, "", ("涨", "跌"), NOW)
    assert block["points_active"] is None and block["points_note"] == "积分状态已过期"
    bot.predict.market_meta["sse-1"] = (meta(), time.monotonic())
    block = {}; bot.book_block(block, other, 0.5, 0.02, 0.0, "", ("涨", "跌"), NOW)
    assert block["points_active"] is False and block["points_note"] == "积分未激活" and block["points_rate"] is None
    del bot.predict.market_meta["sse-1"]
    block = {}; bot.book_block(block, other, None, 0.02, 0.0, "", ("涨", "跌"), NOW)  # also without a fair price
    assert block["points_active"] is None and block["points_note"] == "积分状态暂缺" and "edges" not in block
    status = bot.range_maker_status(row, row.book, NOW)
    assert status["points_active"] is True and status["makers"] and status["maker_note"] == ""
    payload = bot.range_payload(rm, NOW)["ladder"]["rows"][0]
    assert payload["makers"] and payload["points_active"] and payload["points_rate"] == 100
    assert payload["points_spread"] == 0.20 and payload["points_min_shares"] == 100
    assert [e["label"] for e in payload["edges"] if e["best"]] == ["挂Yes"]
    mk = bot.sim_markets(NOW)[0]
    assert not mk.makers and mk.maker_alerts  # recommendation != paper fill permission
    await bot.sim_step(NOW)
    assert set(bot.sim_trades()) == {MARKET + "|up|吃"}
    assert not any(t["maker"] for t in bot.sim_trades().values())

    # Missing, stale, expired, paused, and malformed metadata fail closed while preserving all taker calculations.
    for detail, active, reason in ((meta(), False, "积分未激活"),
                                   (meta(rewards=None), None, "积分状态暂缺"),
                                   (meta(period(end=NOW)), False, "积分已结束"),
                                   (meta(period(), trading_status="CLOSED"), True, "市场暂停交易"),
                                   (meta(period(), trading_status=""), True, "市场交易状态暂缺"),
                                   (meta(period(), spread_threshold=None), True, "积分价差要求暂缺"),
                                   (meta(period(), spread_threshold=0.03), True, "超过积分上限"),
                                   (meta(period(), spread_threshold=1), True, "超过积分上限 1.0¢"),  # "1" is 1¢, never a 100% cap
                                   (meta(period(), spread_threshold=2.5), True, "价差 4.0¢ 超过积分上限 2.5¢"),  # fractional cents
                                   (meta(period(), spread_threshold=101), True, "积分价差要求暂缺")):  # not a cap in cents or a fraction
        bot.predict.market_meta[MID] = (detail, time.monotonic())
        p = bot.range_payload(rm, NOW)["ladder"]["rows"][0]
        assert p["points_active"] is active and p["makers"] is False and reason in p["maker_note"], p
        assert [e["label"] for e in p["edges"] if e["best"]] == ["吃Yes"]
    # Predict's "最大价差" is inclusive: a 4¢ book at a 4¢ cap still earns (ETH 1k/3k: 78/84 at a 6¢ cap showed as 积分已激活);
    # a cap stated in cents means the same
    for threshold, cap in ((0.04, 0.04), (4, 0.04), (100, 1.0)):  # 100 is the last value read as cents: a 100% cap never blocks
        set_meta(bot, period(), spread_threshold=threshold)
        p = bot.range_payload(rm, NOW)["ladder"]["rows"][0]
        assert p["makers"] is True and p["maker_note"] == "" and p["points_spread"] == cap, (threshold, p["maker_note"])
    bot.predict.market_meta[MID] = (meta(period()), time.monotonic() - m.PREDICT_REWARD_STALE_SECONDS - 1)
    p = bot.range_payload(rm, NOW)["ladder"]["rows"][0]
    assert p["points_active"] is None and p["makers"] is False and p["maker_note"] == "积分状态已过期"
    set_meta(bot, period())
    for book, reason in ((m.dataclasses.replace(row.book, bids=()), "缺少双边盘口"),
                         (m.dataclasses.replace(row.book, asks=()), "缺少双边盘口"),
                         (m.dataclasses.replace(row.book, bids=((D("0.3"), D(100)),)), "盘口交叉"),
                         (m.dataclasses.replace(row.book, fetched_ms=NOW - m.PREDICT_STALE_MS - 1), "盘口已过期"),
                         (m.dataclasses.replace(row.book, bids=((D("0.001"), D(100)),),
                                                asks=((D("0.999"), D(100)),)), "超过积分上限")):
        result = bot.range_maker_status(row, book, NOW)
        assert result["points_active"] is True and not result["makers"] and reason in result["maker_note"], result

    # The old taker stream can already be announced; later activation still announces the same-side maker.
    bot, rm, row = fixture()
    set_meta(bot, None)
    await step(bot, NOW)
    await step(bot, NOW + MIN)
    assert len(bot.telegram.sent) == 1 and "吃Yes" in bot.telegram.sent[-1]
    assert MARKET + "|maker" not in bot.store.get("edgealerts")
    set_meta(bot, period())
    await step(bot, NOW + 2 * MIN)
    await step(bot, NOW + 3 * MIN)
    assert len(bot.telegram.sent) == 2 and "挂Yes" in bot.telegram.sent[-1] and "积分已激活" in bot.telegram.sent[-1]
    assert "不含积分收益" in bot.telegram.sent[-1]
    maker_state = bot.store.get("edgealerts")[MARKET + "|maker"]
    assert maker_state["told"] == "up"
    for at in (NOW + 4 * MIN, NOW + 5 * MIN):
        await step(bot, at)
    assert len(bot.telegram.sent) == 2
    set_meta(bot, None)
    await step(bot, NOW + 6 * MIN)
    assert "alert" not in bot.store.get("edgealerts")[MARKET + "|maker"]  # queued opportunity removed immediately
    await step(bot, NOW + 7 * MIN)
    assert len(bot.telegram.sent) == 3 and "建议失效" in bot.telegram.sent[-1] and "积分未激活" in bot.telegram.sent[-1]
    assert bot.store.get("edgealerts")[MARKET]["told"] == "up"  # same-side taker remains valid
    set_meta(bot, period())
    await step(bot, NOW + 8 * MIN)
    await step(bot, NOW + 9 * MIN)
    assert len(bot.telegram.sent) == 3  # retains maker stream cooldown through deactivation

    # A failed send must not leak a newly inactive recommendation during the retry window.
    bot, rm, row = fixture()
    bot.telegram.fail = 10
    await step(bot, NOW)
    await step(bot, NOW + MIN)
    queued = bot.store.get("edgealerts")[MARKET + "|maker"]["alert"]
    assert queued["kind"] == "appear" and bot.edge_maker_fresh(queued, NOW + MIN)
    set_meta(bot, None)
    assert not bot.edge_maker_fresh(queued, NOW + MIN)  # send-time check before the next observation
    bot.telegram.fail = 0
    await step(bot, NOW + MIN + 10_000)
    assert not any("新机会" in s and "挂Yes" in s for s in bot.telegram.sent)
    assert any("新机会" in s and "吃Yes" in s for s in bot.telegram.sent)  # taker retry unaffected
    set_meta(bot, period(end=NOW + 2 * MIN))
    assert not bot.edge_maker_fresh(queued, NOW + 2 * MIN)

    # A queued +20c maker appearance is not delivered after dropping below the announcement bar, even though
    # it still clears the smaller suggestion bar. A valid retry uses the current quote and advantage.
    bot, _, row = fixture()
    bot.telegram.fail = 10
    await step(bot, NOW)
    await step(bot, NOW + MIN)
    queued = bot.store.get("edgealerts")[MARKET + "|maker"]["alert"]
    bot.predict.ladders[SPEC.key][0] = m.dataclasses.replace(row, book=m.dataclasses.replace(
        row.book, bids=((D("0.37"), D(1000)),), asks=((D("0.42"), D(1000)),), fetched_ms=NOW + MIN))
    assert not bot.edge_maker_fresh(queued, NOW + MIN)
    bot.predict.ladders[SPEC.key][0] = m.dataclasses.replace(row, book=m.dataclasses.replace(
        row.book, bids=((D("0.28"), D(1000)),), asks=((D("0.32"), D(1000)),), fetched_ms=NOW + MIN))
    assert bot.edge_maker_fresh(queued, NOW + MIN)
    bot.telegram.fail = 0
    await step(bot, NOW + MIN + 10_000)
    maker_message = next(s for s in bot.telegram.sent if "新机会" in s and "挂Yes" in s)
    assert "挂Yes @ 28.0¢｜净优势 +12.0¢" in maker_message and "挂Yes @ 20.0¢" not in maker_message, maker_message
    told_note = bot.store.get("edgealerts")[MARKET + "|maker"]["note"]
    assert told_note["price"] == 0.28 and abs(told_note["edge"] - 0.12) < 1e-9
    set_meta(bot, None)
    await step(bot, NOW + 2 * MIN)
    await step(bot, NOW + 3 * MIN)
    assert any("建议失效" in s and "之前提醒：挂Yes @ 28.0¢ +12.0¢" in s for s in bot.telegram.sent)

    # A withdrawn maker suggestion that failed to send must not produce an obsolete撤单 reminder after points
    # and the same-side suggestion recover, including while the maker's original cooldown remains in force.
    bot, _, row = fixture()
    await step(bot, NOW)
    await step(bot, NOW + MIN)
    set_meta(bot, None)
    await step(bot, NOW + 2 * MIN)
    bot.telegram.fail = 10
    await step(bot, NOW + 3 * MIN)
    gone = bot.store.get("edgealerts")[MARKET + "|maker"]["alert"]
    assert gone["kind"] == "gone" and bot.edge_maker_fresh(gone, NOW + 3 * MIN)
    set_meta(bot, period())
    assert not bot.edge_maker_fresh(gone, NOW + 3 * MIN)
    bot.telegram.fail = 0
    await step(bot, NOW + 3 * MIN + 10_000)
    assert "alert" not in bot.store.get("edgealerts")[MARKET + "|maker"]
    assert not any("建议失效" in s for s in bot.telegram.sent)

    # A missing model or book interrupts continuous confirmation. Recovery must hold for a new full minute.
    for missing in ("model", "book"):
        bot, rm, row = fixture()
        await step(bot, NOW)
        if missing == "model":
            rm.probability = lambda *args: None
        else:
            bot.predict.ladders[SPEC.key][0] = m.dataclasses.replace(row, book=None)
        await step(bot, NOW + 30_000)
        assert bot.store.get("edgealerts")[MARKET + "|maker"]["pending"] is None
        rm.probability = lambda *args: 0.4
        bot.predict.ladders[SPEC.key][0] = row
        await step(bot, NOW + 31_000)
        assert bot.store.get("edgealerts")[MARKET + "|maker"]["pending"]["since"] == NOW + 31_000
        await step(bot, NOW + MIN)
        assert not any("新机会" in s and "挂Yes" in s for s in bot.telegram.sent)
        await step(bot, NOW + 92_000)
        assert any("新机会" in s and "挂Yes" in s for s in bot.telegram.sent)

    # Reward metadata refreshes after 60 seconds only for price ladders. Failed refreshes retain their age,
    # rather than making old active rewards look freshly confirmed.
    feed = bot.predict
    feed.reward_keys.add(SPEC.key)
    feed.ladder_parse[SPEC.key] = m.price_level
    calls = []

    async def fetch(url, payload=None):
        calls.append(url)
        if url.endswith("/orderbook"):
            return {"data": {"bids": [[0.20, 1000]], "asks": [[0.24, 1000]]}}
        return {"data": {"id": MID, "title": "↑ $120k", "status": "REGISTERED", "tradingStatus": "OPEN",
                         "outcomes": [{"name": "Yes", "indexSet": 1}, {"name": "No", "indexSet": 2}],
                         "rewards": {"current": period()}, "spreadThreshold": 0.20, "shareThreshold": 100}}

    feed.fetch = fetch
    # A 60-second confirmation is allowed to span the normal metadata refresh. Age 61 seconds is due for a
    # refresh, not already invalid: otherwise a phase offset between the two polling loops could starve alerts.
    cycle_bot, _, cycle_row = fixture()
    cycle_bot.predict.fetch = fetch
    cycle_bot.predict.market_meta[MID] = (meta(period()), time.monotonic() - 55)
    await step(cycle_bot, NOW)
    pending_since = cycle_bot.store.get("edgealerts")[MARKET + "|maker"]["pending"]["since"]
    cycle_bot.predict.market_meta[MID] = (meta(period()), time.monotonic() - 61)
    await step(cycle_bot, NOW + 10_000)
    assert cycle_bot.store.get("edgealerts")[MARKET + "|maker"]["pending"]["since"] == pending_since
    market = {"id": MID, "conditionId": "condition", "title": "↑ $120k"}
    await cycle_bot.predict.ladder_row(SPEC.key, SPEC.slug, market)
    await step(cycle_bot, NOW + MIN)
    assert any("新机会" in s and "挂Yes" in s for s in cycle_bot.telegram.sent)
    calls.clear()
    feed.market_meta[MID] = (meta(period()), time.monotonic() - 61)
    await feed.ladder_row(SPEC.key, SPEC.slug, market)
    assert calls == [m.PREDICT_REST + "/markets/" + MID, m.PREDICT_REST + "/markets/" + MID + "/orderbook"]
    assert feed.market_meta[MID][0]["rewards"]["current"]["hourlyRate"] == 100
    assert feed.market_meta[MID][0]["spread_threshold"] == 0.20
    feed.market_meta[MID] = (meta(period()), time.monotonic() - 61)
    calls.clear()
    feed.ladder_parse["ordinary"] = m.price_level
    await feed.ladder_row("ordinary", SPEC.slug, market)
    assert calls == [m.PREDICT_REST + "/markets/" + MID + "/orderbook"]
    old_time = feed.market_meta[MID][1]

    async def broken(url, payload=None):
        if not url.endswith("/orderbook"):
            raise m.RemoteError("HTTP 503")
        return await fetch(url, payload)

    feed.fetch = broken
    await feed.ladder_row(SPEC.key, SPEC.slug, market)
    assert feed.market_meta[MID][1] == old_time
    assert bot.range_maker_status(row, row.book, NOW)["points_active"] is None


asyncio.run(run())
print("RANGE_REWARDS_OK")
