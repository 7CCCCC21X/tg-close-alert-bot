"""Paper trading as a review system. Whenever a card suggests a trade whose net edge reaches 10¢, 100 shares are bought on
paper (the best maker and the best taker apart, once per market and side), and every trade keeps its evidence:
- a taker is judged on the very fill it gets for those 100 shares (signal, cost and net edge are one figure; the card's
  own $100 view is kept beside it), and a thin book only fills what it holds;
- a resting order fills only as far as sellers show at or through its price (presumed, 推定成交: the most ever seen,
  never a sum of looks), partially if that is all; the expectation is kept at the order and at each fill;
- each trade records the model's inputs, the price sources (real source, code, quote and read times), proxy and anchor,
  the book, the code version and settings, at the order and at each fill;
- results are pre-settled on the bot's own data (预结算), then confirmed by Predict (已确认) or corrected (结果不一致),
  every change kept as a revision; touch markets need the whole window read, ladders a complete history."""
import asyncio, csv, io, os, sys, json, time, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
BJ = lambda mo, d, h, mi=0: int(dt.datetime(2026, mo, d, h, mi, tzinfo=m.BEIJING).timestamp() * 1000)
NOW = BJ(10, 5, 10, 0)
HSI_SLUG = "hang-seng-index-up-or-down-on-october-5-2026"
KOSPI_SLUG = "kospi-composite-index-up-or-down-on-october-5-2026"
CLOSE = BJ(10, 5, 16, 10)
FEE = 0.02  # Predict: 2% × min(p, 1 − p) when the market states no rate

# --- settings ----------------------------------------------------------------------------------------------------------
c = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x"})
assert c.sim and c.sim_edge == 0.10 and c.sim_shares == 100
assert c.sim_ways == "taker" and c.sim_markets == frozenset({"close"}) and m.sim_scope(c) == ("只吃单", "指数/个股日涨跌")  # the defaults
c = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SIM": "off", "SIM_EDGE_CENTS": "15", "SIM_SHARES": "250", "SIM_WAYS": "Both",
                       "SIM_MARKETS": "range, Ladder"})
assert not c.sim and abs(c.sim_edge - 0.15) < 1e-12 and c.sim_shares == 250 and c.sim_ways == "both" and c.sim_markets == {"range", "ladder"}
assert m.sim_scope(c) == ("挂单和吃单", "价格阶梯、市值阶梯")
c = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SIM_MARKETS": "all", "SIM_WAYS": ""})
assert c.sim_markets == frozenset(m.SIM_KINDS) and c.sim_ways == "taker" and m.sim_scope(c) == ("只吃单", "全部市场")
for bad in ({"SIM_EDGE_CENTS": "0"}, {"SIM_EDGE_CENTS": "60"}, {"SIM_SHARES": "0"}, {"SIM_WAYS": "none"}, {"SIM_MARKETS": "index"}):
    try: m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", **bad}); assert False, bad
    except ValueError: pass


# --- the arithmetic -----------------------------------------------------------------------------------------------------
def book(bids, asks, at=NOW, slug=HSI_SLUG, key="HSI", fee=None, mid="101"):
    return m.PredictBook(key, slug, mid, "t", tuple((D(p), D(q)) for p, q in bids), tuple((D(p), D(q)) for p, q in asks), at, fee)


b = book([("0.55", "300"), ("0.50", "50")], [("0.58", "400")])
assert m.side_levels(b, "up") == [(0.58, 400.0)]
assert [(round(p, 9), q) for p, q in m.side_levels(b, "down")] == [(0.45, 300.0), (0.5, 50.0)]  # No = 1 − the Yes bids
assert m.own_levels(b, "up") == [(0.55, 300.0), (0.5, 50.0)] and [(round(p, 9), q) for p, q in m.own_levels(b, "down")] == [(0.42, 400.0)]
assert m.fill_shares([(0.5, 30.0), (0.6, 100.0)], 100) == ((30 * 0.5 + 70 * 0.6) / 100, 100.0)
assert m.fill_shares([(0.5, 30.0)], 100) == (0.5, 30.0) and m.fill_shares([], 100) == (0.0, 0.0)  # a thin book fills what it has
assert m.sim_payout("up", 1.0) == 1.0 and m.sim_payout("down", 1.0) == 0.0 and m.sim_payout("down", 0.5) == 0.5
# a taker quote for a number of shares: the levels walked, the fee on the average, short when the book runs out
q = m.taker_quote(book([], [("0.58", "30"), ("0.66", "500")]), "up", 100, 200)
avg = (30 * 0.58 + 70 * 0.66) / 100
assert abs(q["avg"] - avg) < 1e-12 and q["got"] == 100 and abs(q["fee"] - FEE * (1 - avg)) < 1e-12 and not q["short"]
assert q["levels"] == [[0.58, 30.0], [0.66, 70.0]] and q["best"] == 0.58 and abs(q["cost"] - (avg + q["fee"])) < 1e-12
assert m.taker_quote(book([], [("0.58", "40")]), "up", 100, 200)["short"] and m.taker_quote(book([], []), "up", 100, 200) is None
# a resting buy: sellers at or through its price are what fills it; the most seen, never a sum of looks
order = {"price": 0.55, "side": "up", "order": 100.0, "shares": 0.0}
got, seen = m.maker_fill(order, book([("0.54", "10")], [("0.55", "30"), ("0.56", "500")]))
assert got == 30 and seen == {"through": [[0.55, 30.0]], "visible": 30.0, "queue_now": 0.0}, seen
assert m.maker_fill({**order, "shares": 30.0}, book([], [("0.55", "20")]))[0] == 30  # fewer seen later: nothing more
assert m.maker_fill({**order, "shares": 30.0}, book([], [("0.54", "50"), ("0.55", "20")]))[0] == 70
assert m.maker_fill(order, book([("0.55", "120")], [("0.57", "400")])) == (0.0, {"through": [], "visible": 0.0, "queue_now": 120.0})
assert m.maker_fill(order, book([], [("0.40", "999")]))[0] == 100  # never more than the order
# Predict's resolution, read from the market object (an outcome WON, payouts, or a resolution named on a resolved market)
outcomes = lambda *rows: [{"name": n, "indexSet": i + 1, **extra} for i, (n, extra) in enumerate(rows)]
assert m.predict_resolution({"status": "REGISTERED", "outcomes": outcomes(("Up", {}), ("Down", {}))}) is None
won = m.predict_resolution({"status": "RESOLVED", "outcomes": outcomes(("Up", {"status": "LOST"}), ("Down", {"status": "WON"}))})
assert won == {"index": 1, "name": "Down", "split": False, "how": "outcomes.status"}, won
assert m.predict_resolution({"outcomes": outcomes(("Yes", {"payout": "1"}), ("No", {"payout": "0"}))})["name"] == "Yes"
assert m.predict_resolution({"outcomes": outcomes(("Up", {"payout": 0.5}), ("Down", {"payout": 0.5}))})["split"]
assert m.predict_resolution({"status": "RESOLVED", "resolution": {"name": "No"}, "outcomes": outcomes(("Yes", {}), ("No", {}))})["index"] == 1
assert m.predict_resolution({"status": "REGISTERED", "resolution": {"name": "No"}, "outcomes": outcomes(("Yes", {}), ("No", {}))}) is None
# an old record (before evidence was kept) in today's shape: nothing invented
legacy = {"market": "old", "slug": "old", "item": "恒生指数", "kind": "close", "key": "HSI", "side": "up", "label": "挂涨",
          "maker": True, "fair": 0.70, "signal": 0.15, "opened": NOW - 86400_000, "settle": {}, "price": 0.55, "shares": 100,
          "status": "settled", "filled": NOW - 80000_000, "fill_fair": 0.62, "edge": 0.15, "payout": 1.0,
          "settled": NOW - 3600_000, "note": "10-04 收盘 24,650，对 24,600"}
up = m.sim_upgrade(legacy)
assert up["v"] == 2 and up["legacy"] and up["order"] == 100 and up["confirm"] == "local" and up["local"]["up"] == 1.0
assert up["fills"] == [{"at": NOW - 80000_000, "shares": 100, "fair": 0.62, "how": "旧规则：触价即算全部成交"}] and "v" not in legacy
assert m.sim_upgrade({**legacy, "status": "resting", "payout": None, "filled": None})["shares"] == 0.0  # had filled nothing


class FM:
    def __init__(self, now): self.now, self.config = now, None
    def now_ms(self): return self.now


async def step(bot, at, *markets):
    bot.market.now, bot.sim_ran = at, -1e9  # the trader's own pacing runs on the monotonic clock
    if markets:
        world["markets"] = list(markets)
    return await bot.sim_step(at)


world = {"markets": []}
SETTLE_HSI = {"key": "HSI", "target": "2026-10-05", "line": 24600.0, "close_ms": CLOSE}


def hsi(fair, bids, asks, at, **kw):
    return m.SimMarket(HSI_SLUG, "恒生指数", "close", "HSI", fair, book(bids, asks, at), 0.03, kw.get("hold", ""), ("涨", "跌"),
                       SETTLE_HSI, {"basis": {"fair_up": fair, "ref": 24600.0}, "sources": [{"what": "恒指期货", "source": "etnet"}],
                                    "proxy": {"proxy": "恒指期货", "anchor": 24500.0}})


async def run():
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off",
                             "WEB_PORT": "8080", "SIM_WAYS": "both", "SIM_MARKETS": "all"})  # every kind and both ways, as below
    store = m.Store(":memory:")
    bot = m.Bot(cfg, store, FM(NOW), None)
    bot.sim_markets = lambda now: world["markets"]
    assert "模拟交易" in [name for name, _ in bot.reference_jobs()] and bot.cmd_sim(None).text.count("还没有触发过") == 1
    asked = []
    answers = {}

    async def details(mid):
        asked.append(mid)
        answer = answers.get(mid, {"outcomes": ["Up", "Down"], "status": "REGISTERED", "resolved": None})
        if isinstance(answer, Exception):
            raise answer
        return answer
    bot.predict.market_details = details

    # --- HSI: fair 70¢, Yes bid 55¢×300 (挂涨 +15¢) and ask 58¢×400 (100 shares: 58¢ + 0.84¢ fee → +11.16¢) --------------
    await step(bot, NOW, hsi(0.70, [("0.55", "300")], [("0.58", "400")], NOW))
    trades = bot.sim_trades()
    assert sorted(trades) == [f"{HSI_SLUG}|up|吃", f"{HSI_SLUG}|up|挂"], sorted(trades)
    maker, taker = trades[f"{HSI_SLUG}|up|挂"], trades[f"{HSI_SLUG}|up|吃"]
    assert maker["status"] == "resting" and maker["price"] == 0.55 and maker["order"] == 100 and maker["shares"] == 0
    assert maker["queue_ahead"] == 300 and maker["filled"] is None and maker["label"] == "挂涨" and abs(maker["edge"] - 0.15) < 1e-9
    fee = FEE * 0.42
    assert taker["status"] == "filled" and taker["shares"] == 100 and abs(taker["price"] - (0.58 + fee)) < 1e-12 and taker["avg"] == 0.58
    assert abs(taker["edge"] - (0.70 - 0.58 - fee)) < 1e-12 and taker["signal"] == taker["edge"] and taker["slip"] == 0
    assert taker["fills"] == [{"at": NOW, "shares": 100.0, "fair": 0.70, "how": "吃单立即成交", "levels": [[0.58, 100.0]], "short": False}]
    # what the order was decided on: the model, its sources and proxy, the book, the card's four directions, the version
    entry = maker["entry"]
    assert entry["at"] == NOW and entry["fair"] == 0.70 and entry["need"] == 0.03 and entry["basis"] == {"fair_up": 0.70, "ref": 24600.0}
    assert entry["sources"] == [{"what": "恒指期货", "source": "etnet"}] and entry["proxy"]["anchor"] == 24500.0
    assert entry["book"] == {"bids": [[0.55, 300.0]], "asks": [[0.58, 400.0]], "fetched_ms": NOW, "market_id": "101", "fee_bps": None}
    assert [(e["label"], e["best"]) for e in entry["card"]] == [("挂涨", True), ("挂跌", False), ("吃涨", False), ("吃跌", False)]
    assert maker["version"] == {"code": m.VERSION, "sim_edge": 0.10, "sim_shares": 100, "sim_ways": "挂单和吃单", "sim_markets": "全部市场", "sim_group_usd": 300, "min_edge": 0.02, "fee_bps": 200,
                                "trade_usd": 100, "a50_beta": 0.8, "kospi_beta": 1.0, "sigma_error": m.MODEL_SIGMA_ERROR,
                                "beta_error": m.MODEL_BETA_ERROR} and maker["market_id"] == "101"
    # the edge lasting for hours buys nothing more
    await step(bot, NOW + 60_000, hsi(0.70, [("0.55", "300")], [("0.58", "400")], NOW + 60_000))
    assert len(bot.sim_trades()) == 2

    # --- one trade size: a taker is judged on its own 100 shares (the card's $100 view is kept beside it) --------------------
    same = lambda asks, at, name: m.SimMarket(name, name, "close", "X", 0.70, book([], asks, at, name, mid=name), 0.03, "", ("涨", "跌"), {})
    # 100 shares at 58¢ clear 10¢ (+11.16¢); $100 would walk into 70¢ (+6.75¢ on the card)
    await step(bot, NOW + 70_000, same([("0.58", "100"), ("0.70", "1000")], NOW + 70_000, "deep"))
    t = bot.sim_trades()["deep|up|吃"]
    assert t["shares"] == 100 and abs(t["edge"] - (0.70 - 0.58 - fee)) < 1e-12, t
    card = next(e for e in t["entry"]["card"] if e["label"] == "吃涨")
    assert abs(card["edge"] - (0.70 - 0.625 - FEE * 0.375)) < 1e-12 and card["size"] == 160, card
    # the best level alone clears the bar, the 100 shares do not (10 at 58¢, 90 at 69¢): no trade
    await step(bot, NOW + 80_000, same([("0.58", "10"), ("0.69", "1000")], NOW + 80_000, "shallow"))
    assert "shallow|up|吃" not in bot.sim_trades()
    # a thin book fills what it holds (at least half the order), and says so
    await step(bot, NOW + 90_000, same([("0.58", "60")], NOW + 90_000, "thin"))
    t = bot.sim_trades()["thin|up|吃"]
    assert t["shares"] == 60 and t["order"] == 100 and t["fills"][0]["short"], t
    # a few dust shares at a stray price are not the trade: nothing is bought, the slot stays free for the real one
    await step(bot, NOW + 95_000, same([("0.58", "5")], NOW + 95_000, "dust"))
    assert "dust|up|吃" not in bot.sim_trades()
    # a crossed snapshot (bid at or above ask) is not a book anyone could trade: neither way opens on it
    crossed = m.SimMarket("x", "x", "close", "X", 0.70, book([("0.60", "100")], [("0.58", "100")], NOW + 97_000, "x", mid="x"), 0.03, "", ("涨", "跌"), {})
    await step(bot, NOW + 97_000, crossed)
    assert not [k for k in bot.sim_trades() if k.startswith("x|")] and m.book_crossed(crossed.book)
    # a stale book or a card that holds back: no new trade and no fill read from it
    await step(bot, NOW + 100_000, m.dataclasses.replace(same([("0.40", "100")], NOW - 200_000, "stale"), market="stale"),
               m.dataclasses.replace(hsi(0.80, [("0.50", "100")], [("0.52", "100")], NOW + 100_000, hold="期货锚点是近似值"), market="held"))
    assert not [k for k in bot.sim_trades() if k.startswith(("stale|", "held|"))]
    # a bar above the edge (model error 12¢): an 11¢ taker edge is not a suggestion, so nothing is bought
    await step(bot, NOW + 110_000, m.dataclasses.replace(same([("0.58", "400")], NOW + 110_000, "wide"), need=0.12))
    assert not [k for k in bot.sim_trades() if k.startswith("wide|")]

    # --- a resting order fills only as far as sellers show at or through its price ---------------------------------------
    hsi_maker = f"{HSI_SLUG}|up|挂"
    await step(bot, NOW + 120_000, hsi(0.70, [("0.55", "120")], [("0.57", "400")], NOW + 120_000))
    t = bot.sim_trades()[hsi_maker]
    assert t["status"] == "resting" and t["shares"] == 0 and t["queue_min"] == 120  # the queue ahead shrank, nothing traded with us
    wait = {x["id"]: x["wait"] for x in bot.journal_payload()["trades"]}  # the review page says what each open trade waits for
    assert wait[hsi_maker] == "挂 55.0¢，最低卖价 57.0¢（高出 2.0¢）：要有人卖到挂价或更低才算成交；现在公平价 70.0¢", wait[hsi_maker]
    assert wait["deep|up|吃"] == "等市场出结果" and wait[f"{HSI_SLUG}|up|吃"].startswith("等 10-05 收盘（10-05 16:10）后 1 小时（10-05 17:10），按官方收盘预结算")
    await step(bot, NOW + 180_000, hsi(0.70, [("0.54", "200")], [("0.55", "30"), ("0.60", "100")], NOW + 180_000))
    await step(bot, NOW + 240_000, hsi(0.70, [("0.54", "200")], [("0.55", "30"), ("0.60", "100")], NOW + 240_000))  # same seller
    t = bot.sim_trades()[hsi_maker]
    assert t["shares"] == 30 and len(t["fills"]) == 1 and t["filled"] == NOW + 180_000 and t["status"] == "resting", t
    f = t["fills"][0]
    assert f["how"] == "推定成交" and f["seen"] == {"through": [[0.55, 30.0]], "visible": 30.0, "queue_now": 0.0} and f["fair"] == 0.70
    assert f["book"]["asks"] == [[0.55, 30.0], [0.6, 100.0]] and f["sources"] == entry["sources"] and f["basis"]["fair_up"] == 0.70
    assert m.sim_status(t) == "挂单中（推定成交 30/100 份）"
    # the market turns against the order as it fills: the fill-time expectation falls below the order-time one
    await step(bot, NOW + 300_000, hsi(0.62, [("0.53", "200")], [("0.54", "50"), ("0.55", "20")], NOW + 300_000))
    await step(bot, NOW + 360_000, hsi(0.60, [("0.52", "200")], [("0.53", "500")], NOW + 360_000))
    t = bot.sim_trades()[hsi_maker]
    assert t["status"] == "filled" and t["shares"] == 100 and [x["shares"] for x in t["fills"]] == [30, 40, 30], t["fills"]
    assert abs(m.sim_fill_expectation(t) - (30 * 0.15 + 40 * 0.07 + 30 * 0.05)) < 1e-9 and abs(t["edge"] * t["shares"] - 15) < 1e-9

    # --- KOSPI (跌 side): a maker that fills in part, a taker over two levels ----------------------------------------------
    kospi = lambda bids, asks, at: m.SimMarket(KOSPI_SLUG, "KOSPI", "close", "KOSPI", 0.20, book(bids, asks, at, KOSPI_SLUG, "KOSPI", mid="102"),
                                               0.02, "", ("涨", "跌"), {"key": "KOSPI", "target": "2026-10-05", "line": 3300.0,
                                                                     "close_ms": BJ(10, 5, 14, 30)})
    await step(bot, NOW + 400_000, kospi([("0.35", "30"), ("0.30", "500")], [("0.38", "100")], NOW + 400_000))
    t = bot.sim_trades()
    down_maker, down_taker = t[f"{KOSPI_SLUG}|down|挂"], t[f"{KOSPI_SLUG}|down|吃"]
    assert down_maker["label"] == "挂跌" and abs(down_maker["price"] - 0.62) < 1e-9 and down_maker["queue_ahead"] == 100
    k_avg = (30 * 0.65 + 70 * 0.70) / 100
    k_cost = k_avg + FEE * min(k_avg, 1 - k_avg)
    assert down_taker["shares"] == 100 and abs(down_taker["price"] - k_cost) < 1e-12 and abs(down_taker["slip"] - (k_avg - 0.65)) < 1e-12
    await step(bot, NOW + 460_000, kospi([("0.39", "25")], [("0.41", "100")], NOW + 460_000))  # a 跌 seller at 61¢ ≤ 62¢
    assert bot.sim_trades()[f"{KOSPI_SLUG}|down|挂"]["shares"] == 25
    wait = {x["id"]: x["wait"] for x in bot.journal_payload()["trades"]}
    assert wait[f"{KOSPI_SLUG}|down|挂"] == "盘口有卖到挂价的卖单，已按看到的数量推定成交；现在公平价 80.0¢", wait
    assert wait[f"{KOSPI_SLUG}|down|吃"] == "等 10-05 收盘（10-05 14:30）后 1 小时（10-05 15:30），按官方收盘预结算，再等 Predict 确认"
    assert wait[hsi_maker] == "等 10-05 收盘（10-05 16:10）后 1 小时（10-05 17:10），按官方收盘预结算，再等 Predict 确认"  # filled by now
    assert bot.sim_wait({"status": "resting"}, None, NOW) == "这个市场现在没有报价（卡片未定价或盘口没读到），挂单原地等着"
    assert bot.sim_wait({"status": "resting", "side": "up", "price": 0.55}, hsi(0.70, [], [], NOW - 600_000), NOW) == "盘口已过期，等新盘口"
    assert bot.sim_wait({"status": "resting", "side": "up", "price": 0.55}, hsi(0.70, [("0.50", "9")], [], NOW), NOW) == (
        "挂 55.0¢，盘口这一边没有卖单：要有人卖到挂价或更低才算成交；现在公平价 70.0¢")
    past = {"status": "filled", "kind": "close", "settle": SETTLE_HSI, "final_check": {"status": "REGISTERED"}}
    assert bot.sim_wait(past, None, CLOSE + 2 * 3_600_000) == "收盘（10-05 16:10）已过 1 小时，官方收盘还没读到，等 Predict 结算（Predict 市场状态：REGISTERED）"
    assert bot.sim_wait({**past, "kind": "range", "settle": {"end": CLOSE}, "final_check": None, "final_error": "HTTP 500"}, None, CLOSE + 2 * 3_600_000) == (
        "窗口已结束（10-05 16:10），本地数据还定不了结果，等 Predict 结算（读取失败：HTTP 500）")
    assert bot.sim_wait({"status": "settled"}, None, NOW) == "" and bot.sim_wait({"status": "cancelled"}, None, NOW) == ""

    # --- local pre-settlement on the official close (an hour after it), with where the close came from ----------------------
    bot.note_outcome("HSI", "2026-10-05", 24650.0, "tencent 日K")
    assert store.get("outsrc:HSI:2026-10-05")["source"] == "tencent 日K" and store.get("outcome:HSI:2026-10-05") == 24650.0
    world["markets"] = []
    await step(bot, CLOSE + 30 * 60_000)
    assert bot.sim_trades()[hsi_maker]["status"] == "filled"  # the official close is not final yet
    # Predict is asked only once a market can have a result: KOSPI (closed 14:30), not yet HSI (16:10)
    assert asked == ["102"], asked
    asked.clear()
    await step(bot, CLOSE + 61 * 60_000)
    t = bot.sim_trades()
    assert t[hsi_maker]["status"] == "settled" and t[hsi_maker]["payout"] == 1.0 and t[hsi_maker]["confirm"] == "local"
    assert t[hsi_maker]["note"] == "10-05 收盘 24,650，对 24,600" and m.sim_status(t[hsi_maker]) == "赢 +$45.00"
    assert m.sim_state(t[hsi_maker]) == "预结算" and t[hsi_maker]["local"]["evidence"] == {
        "rule": "收盘高于参考线为涨，低于为跌，相同各半", "close": 24650.0, "line": 24600.0, "source": "tencent 日K", "day": "2026-10-05"}
    assert asked == ["101"] and t[hsi_maker]["final_check"]["status"] == "REGISTERED"  # Predict not resolved yet: stays 预结算
    # --- Predict's final word: asked at most every 10 minutes per market; it confirms -------------------------------------
    answers["101"] = {"outcomes": ["Up", "Down"], "status": "RESOLVED", "resolved": {"index": 0, "name": "Up", "split": False,
                                                                                      "how": "outcomes.status"}}
    await step(bot, CLOSE + 62 * 60_000)
    assert asked == ["101"]  # not again within 10 minutes
    bot.sim_checked.clear()  # ten minutes on
    await step(bot, CLOSE + 72 * 60_000)
    t = bot.sim_trades()
    assert asked.count("101") == 2 and t[hsi_maker]["confirm"] == "confirmed" and m.sim_state(t[hsi_maker]) == "已确认", asked
    assert t[hsi_maker]["final"]["name"] == "Up" and t[hsi_maker]["final"]["up"] == 1.0 and t[hsi_maker]["revisions"] == []
    assert t[f"{HSI_SLUG}|up|吃"]["confirm"] == "confirmed" and t[f"{HSI_SLUG}|up|吃"]["payout"] == 1.0
    bot.sim_checked.clear()
    await step(bot, CLOSE + 90 * 60_000)
    assert asked.count("101") == 2  # confirmed: never asked again

    # --- a tie locally, Down on Predict: re-settled on Predict's result, the change kept --------------------------------------
    bot.note_outcome("KOSPI", "2026-10-05", 3300.0, "Yahoo ^KS11 日K")
    await step(bot, CLOSE + 91 * 60_000)
    t = bot.sim_trades()
    k_maker, k_taker = t[f"{KOSPI_SLUG}|down|挂"], t[f"{KOSPI_SLUG}|down|吃"]
    assert k_taker["payout"] == 0.5 and k_taker["confirm"] == "local" and k_maker["status"] == "settled"
    assert k_maker["shares"] == 25 and k_maker["unfilled"] == 75 and k_maker["payout"] == 0.5  # only what filled is paid
    answers["102"] = {"outcomes": ["Up", "Down"], "status": "RESOLVED", "resolved": {"index": 1, "name": "Down", "split": False, "how": "x"}}
    bot.sim_checked.clear()
    await step(bot, CLOSE + 92 * 60_000)
    k_taker = bot.sim_trades()[f"{KOSPI_SLUG}|down|吃"]
    assert k_taker["confirm"] == "mismatch" and k_taker["payout"] == 1.0 and m.sim_state(k_taker) == "结果不一致"
    assert k_taker["revisions"] == [{"at": CLOSE + 92 * 60_000, "by": "Predict 最终结果", "from": 0.5, "to": 1.0,
                                     "note": "Predict 结算：Down（本地预结算为 10-05 收盘 3,300，对 3,300）"}], k_taker["revisions"]
    assert k_taker["local"]["up"] == 0.5 and k_taker["final"]["up"] == 0.0  # both kept

    # --- the local data corrected before Predict answers: re-settled, kept as a revision ------------------------------------
    sse_slug = "sse-composite-index-up-or-down-on-october-5-2026"
    sse = m.SimMarket(sse_slug, "上证指数", "close", "SSE", 0.70, book([], [("0.55", "500")], CLOSE + 93 * 60_000, sse_slug, "SSE", mid="103"),
                      0.02, "", ("涨", "跌"), {"key": "SSE", "target": "2026-10-05", "line": 3850.0, "close_ms": BJ(10, 5, 15, 0)})
    await step(bot, CLOSE + 93 * 60_000, sse)
    bot.note_outcome("SSE", "2026-10-05", 3851.0, "腾讯实时（日K未出）")
    world["markets"] = []
    answers["103"] = m.RemoteError("HTTP 500")
    bot.sim_checked.clear()  # (it was already asked once, when it opened past its close)
    await step(bot, CLOSE + 94 * 60_000)
    t = bot.sim_trades()[f"{sse_slug}|up|吃"]
    assert t["payout"] == 1.0 and t["confirm"] == "local" and t["final_error"] == "HTTP 500"
    bot.note_outcome("SSE", "2026-10-05", 3849.5, "腾讯日K")
    await step(bot, CLOSE + 95 * 60_000)
    t = bot.sim_trades()[f"{sse_slug}|up|吃"]
    assert t["payout"] == 0.0 and t["local"]["evidence"]["source"] == "腾讯日K" and t["confirm"] == "local"
    assert [(r["by"], r["from"], r["to"]) for r in t["revisions"]] == [("本地数据更正", 1.0, 0.0)], t["revisions"]

    # --- markets the bot's data cannot decide wait for Predict ---------------------------------------------------------------
    touch = bot.touches["BNB"]; touch.start_ms = BJ(9, 1, 0)
    trade = lambda kind, key, side, **settle: {"kind": kind, "key": key, "side": side, "market": f"x#{settle.pop('mid', '')}", "settle": settle}
    assert bot.sim_result(trade("touch", "BNB", "up", deadline=touch.spec.deadline_ms), NOW) is None
    store.put(f"touch:{touch.spec.slug}", {"kind": "high", "time": NOW - 3_600_000, "hi": 901, "lo": 880, "start": touch.start_ms})
    up_res = bot.sim_result(trade("touch", "BNB", "up", deadline=touch.spec.deadline_ms), NOW)
    assert up_res[0] == 1.0 and up_res[2]["history"]["kind"] == "high" and "币安现货" in up_res[2]["source"]
    store.put(f"touch:{touch.spec.slug}", {"kind": "low", "time": NOW - 3_600_000, "hi": 720, "lo": 699, "start": touch.start_ms})
    assert bot.sim_result(trade("touch", "BNB", "up", deadline=touch.spec.deadline_ms), NOW)[0] == 0.0
    deadline = touch.spec.deadline_ms
    # read to the last full hour only: the final partial hour is unread, so "neither" is not settled on it
    store.put(f"touch:{touch.spec.slug}", {"kind": "clear", "through": deadline - 59 * 60_000, "start": touch.start_ms})
    assert bot.sim_result(trade("touch", "BNB", "up", deadline=deadline), deadline + 61 * 60_000) is None
    store.put(f"touch:{touch.spec.slug}", {"kind": "clear", "through": deadline + 60_000, "start": touch.start_ms})
    assert bot.sim_result(trade("touch", "BNB", "up", deadline=deadline), deadline + 30 * 60_000) is None  # settles an hour on
    assert bot.sim_result(trade("touch", "BNB", "up", deadline=deadline), deadline + 61 * 60_000)[:2] == (0.5, "整个窗口都没碰到两条线，按 50/50")
    oct_spec = next(s for s in m.UPDOWN_MARKETS if s.key == "BTC-2026-10")  # BTC 10月涨跌
    assert bot.sim_result(trade("updown", oct_spec.key, "up"), NOW) is None
    store.put(f"updown:{oct_spec.slug}:start", {"open": oct_spec.start_ms, "close": "117234.56"})
    store.put(f"updown:{oct_spec.slug}:end", {"open": oct_spec.end_ms, "close": "110000"})
    res = bot.sim_result(trade("updown", oct_spec.key, "up"), oct_spec.end_ms + 120_000)
    assert res[0] == 0.0 and res[2]["start"] == 117234.56 and res[2]["end"] == 110000.0
    cap = bot.caps["NIULAI"]
    cap.price, cap.supply, cap.priced_ms = D("0.1"), D("1000000000"), NOW  # $100M now
    ladder = lambda target, mid: trade("ladder", "NIULAI", "up", target=target, end=cap.spec.end_ms, mid=mid)
    assert bot.sim_result(ladder("200000000", "11"), NOW) is None
    store.put(f"cap:{cap.spec.slug}", {"high": 0.21, "at": NOW // 1000, "start": cap.spec.start_ms})  # $210M seen
    res = bot.sim_result(ladder("200000000", "11"), NOW)
    assert res[0] == 1.0 and res[2]["window_high"] == 210_000_000.0
    assert bot.sim_result(ladder("300000000", "12"), NOW) is None
    late = cap.spec.end_ms + 61 * 60_000
    # the window's history is not read to its end: "not reached" is not settled on it
    assert cap.coverage() == "窗口尚未核验到截止" and bot.sim_result(ladder("300000000", "12"), late) is None
    store.put(f"cap:{cap.spec.slug}", {"high": 0.21, "at": NOW // 1000, "start": cap.spec.start_ms, "through": cap.window_end_s,
                                       "first": "done"})
    assert cap.coverage() == "" and bot.sim_result(ladder("300000000", "12"), late)[0] == 0.0
    # a price read after the window never counts towards its high
    cap.price, cap.priced_ms = D("0.5"), cap.spec.end_ms + 120_000
    assert bot.sim_result(ladder("300000000", "12"), late)[0] == 0.0 and cap.window_high()[0] == D("0.21") * D("1000000000")
    # Predict settles what the bot's data could not: a ladder level with an incomplete history
    pons = m.SimMarket("pons#77", "$PONS FDV $10M", "ladder", "PONS", 0.30,
                       book([("0.10", "100")], [("0.12", "100")], late, "pons", "PONS", mid="77"), 0.02, "", ("Yes", "No"),
                       {"target": "10000000", "end": late - 61 * 60_000})
    await step(bot, late - 62 * 60_000, m.dataclasses.replace(pons, book=m.dataclasses.replace(pons.book, fetched_ms=late - 62 * 60_000)))
    assert bot.sim_trades()["pons#77|up|吃"]["status"] == "filled"
    answers["77"] = {"outcomes": ["Yes", "No"], "status": "RESOLVED", "resolved": {"index": 1, "name": "No", "split": False, "how": "x"}}
    world["markets"] = []
    bot.sim_checked.clear()
    await step(bot, late)
    t = bot.sim_trades()["pons#77|up|吃"]
    assert t["status"] == "settled" and t["payout"] == 0.0 and t["confirm"] == "confirmed" and "local" not in t, t
    # a contract's daily market: its exchange close is kept per day, with its source
    sym = "UNITREEUSDT"
    close_ms = int(dt.datetime(2026, 10, 5, 15, 0, tzinfo=m.BEIJING).timestamp() * 1000)
    bot.stocks.closes[sym] = m.Baseline(D("75.5"), "k", "label", 0, close_ms, "", "上交所688836·东方财富")
    bot.sim_note_closes()
    assert store.get(f"outcome:{sym}:2026-10-05") == 75.5 and store.get(f"outsrc:{sym}:2026-10-05")["source"] == "上交所688836·东方财富"

    # --- an old record is upgraded, confirmed through its slug ----------------------------------------------------------------
    store.put("sim:old|up|挂", legacy)
    async def resolve(slug):
        return {"id": "900"} if slug == "old" else None
    bot.predict.resolve = resolve
    answers["900"] = {"outcomes": ["Up", "Down"], "status": "RESOLVED", "resolved": {"index": 0, "name": "Up", "split": False, "how": "x"}}
    bot.sim_checked.clear()
    await step(bot, late + 60_000)
    old = bot.sim_trades()["old|up|挂"]
    assert old["legacy"] and old["confirm"] == "confirmed" and old["payout"] == 1.0 and store.get("sim:old|up|挂")["v"] == 2

    # --- the record: totals, the expectation at the order and at the fills, how results were confirmed --------------------
    r = bot.sim_report()
    tot = r["total"]
    settled = [t for t in bot.sim_trades().values() if t["status"] == "settled"]
    assert tot["settled"] == len(settled) and tot["confirmed"] == sum(t["confirm"] == "confirmed" for t in settled)
    # both KOSPI trades (the 25 filled shares of the maker too) were re-settled on Predict's result; SSE is 预结算
    assert tot["mismatch"] == 2 and tot["local"] == 1 and tot["confirmed"] == 4, tot
    assert abs(tot["pnl"] - sum((t["payout"] - t["price"]) * t["shares"] for t in settled)) < 1e-9
    assert abs(tot["expected"] - sum(t["edge"] * t["shares"] for t in settled)) < 1e-9
    assert abs(tot["expected_fill"] - sum(m.sim_fill_expectation(t) for t in settled)) < 1e-9 and tot["expected_fill"] < tot["expected"]
    assert tot["partial"] == 1 and tot["expired"] == 1 and tot["open"] == 2  # PONS's maker never filled; deep/thin still held
    row = next(x for x in r["rows"] if x["id"] == f"{KOSPI_SLUG}|down|吃")
    assert row["state"] == "结果不一致" and row["order"] == 100 and row["url"].startswith(m.PREDICT_SITE)
    json.dumps(r)
    text = bot.cmd_sim(None).text
    assert "已确认 4｜预结算 1｜结果不一致 2" in text and "模型预期：下单时" in text and "成交时" in text, text
    assert "部分成交 1 笔" in text and f"{bot.web_url()}/journal" in text and "·结果不一致" in text, text
    assert "已关闭" in m.Bot(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SIM": "off"}), m.Store(":memory:"), FM(NOW), None).cmd_sim(None).text
    off = m.Bot(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SIM": "off"}), m.Store(":memory:"), FM(NOW), None)
    assert "模拟交易" not in [name for name, _ in off.reference_jobs()] and not any(i["name"] == "模拟交易" for i in off.odds_payload()["items"])

    # --- the journal: every record whole, and a CSV for spreadsheets ---------------------------------------------------------
    j = bot.journal_payload()
    assert j["trades"][0]["opened"] >= j["trades"][-1]["opened"] and len(j["trades"]) == len(bot.sim_trades())
    jt = next(x for x in j["trades"] if x["id"] == hsi_maker)
    assert jt["state"] == "已确认" and jt["entry"]["proxy"]["anchor"] == 24500.0 and len(jt["fills"]) == 3 and jt["expected_fill"] < 15
    json.dumps(j, default=str)
    raw = bot.journal_csv()
    assert raw.startswith("﻿")
    rows = list(csv.reader(io.StringIO(raw[1:])))
    assert rows[0] == list(m.Bot.JOURNAL_COLUMNS) and all(len(x) == len(rows[0]) for x in rows) and len(rows) == len(j["trades"]) + 1
    line = next(x for x in rows if x[0] == f"{KOSPI_SLUG}|down|吃")
    col = dict(zip(rows[0], line))
    assert col["确认"] == "结果不一致" and col["结果不一致"] == "是" and col["本地结果"] == "50/50" and col["Predict 结果"].startswith("跌/No")
    assert "Predict 最终结果：0.5→1.0" in col["修订"] and col["市场链接"].startswith(m.PREDICT_SITE), col
    line = next(x for x in rows if x[0] == "old|up|挂")
    assert dict(zip(rows[0], line))["行情来源"] == "旧记录：无快照"

    # --- the real cards feed it, evidence included: the BTC 10月涨跌 card frames 挂涨 at 40¢ against a fair ~54¢ ----------
    real = m.Bot(cfg, m.Store(":memory:"), FM(BJ(10, 1, 16, 15)), None)
    up_mkt = real.updowns[oct_spec.key]
    real.store.put(f"updown:{oct_spec.slug}:start", {"open": oct_spec.start_ms, "close": "117234.56"})
    at = real.market.now
    up_mkt.price, up_mkt.priced_ms, up_mkt.sigma, up_mkt.sigma_ms = D("118500"), at, 0.28, at
    real.predict.books[oct_spec.key] = m.PredictBook(oct_spec.key, oct_spec.slug, "77", "t", ((D("0.40"), D("500")),), ((D("0.45"), D("500")),), at)
    real.predict.info[oct_spec.slug] = {"outcomes": ["Up", "Down"], "created_ms": 0}
    card = next(i for i in real.odds_payload()["items"] if i["name"] == "BTC 10月涨跌")
    page_best = next(e for e in card["predict"]["edges"] if e["best"])
    assert page_best["label"] == "挂涨" and page_best["edge"] >= 0.10, card["predict"]["edges"]
    mk = next(x for x in real.sim_markets(at) if x.kind == "updown")
    assert mk.fair_up == card["fair_up"] and mk.need == card["predict"]["need"] and not mk.hold
    real.sim_ran = -1e9
    await real.sim_step(at)
    (t,) = real.sim_trades().values()
    assert (t["label"], t["price"], t["status"]) == ("挂涨", 0.40, "resting") and t["settle"] == {"end": oct_spec.end_ms}
    assert t["entry"]["basis"]["line"] == 117234.56 and t["entry"]["basis"]["price"] == 118500.0 and t["entry"]["basis"]["sigma"] == 0.28
    assert t["entry"]["sources"][0] == {"what": "现货", "source": "币安现货", "symbol": "BTCUSDT", "type": "最新价", "price": 118500.0,
                                        "quoted_ms": at, "fetched_ms": at}
    sim_card = next(i for i in real.odds_payload()["items"] if i["name"] == "模拟交易")
    assert sim_card["group"] == "sim" and sim_card["sim"]["total"]["resting"] == 1 and sim_card["sim"]["rows"][0]["id"] == f"{oct_spec.slug}|up|挂"

    # a daily card: the live index, its dated reference close (real sources, codes, quote and read times)
    sbot = m.Bot(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off"}),
                 m.Store(":memory:"), FM(BJ(9, 28, 10, 5)), None)
    sat = sbot.market.now
    sbot.cn.quote = m.IndexQuote("上证指数", D("3860"), D("3850"), None, None, None, sat - 3000, "腾讯", fetched_ms=sat - 1000)
    sbot.cn.close = m.DailyClose(dt.date(2026, 9, 25), D("3850"), D("3840"), "腾讯日K", sat - 600_000)
    slug = "sse-composite-index-up-or-down-on-september-28-2026"
    sbot.predict.slugs["SSE"] = slug
    sbot.predict.books["SSE"] = m.PredictBook("SSE", slug, "901", "t", ((D("0.40"), D("100")),), ((D("0.45"), D("100")),), sat)
    smk = next(x for x in sbot.sim_markets(sat) if x.kind == "close" and x.key == "SSE")
    ev = smk.evidence
    assert ev["basis"]["direct"] and ev["basis"]["ref"] == 3850.0 and ev["basis"]["close_ms"] == BJ(9, 28, 15, 0) and ev["proxy"] is None
    assert ev["sources"] == [{"what": "上证现货", "source": "腾讯", "symbol": "sh000001", "type": "最新价", "price": 3860.0,
                              "quoted_ms": sat - 3000, "fetched_ms": sat - 1000},
                             {"what": "参考收盘", "source": "腾讯日K", "symbol": "sh000001", "type": "日K收盘", "price": 3850.0,
                              "quoted_ms": m.DailyClose(dt.date(2026, 9, 25), D("3850"), None, "", 0).close_ms,
                              "fetched_ms": sat - 600_000, "day": "2026-09-25"}], ev["sources"]
    # a contract card after hours: the Binance price is the proxy, its price at the exchange close the anchor
    kbot = m.Bot(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "SKHYNIXUSDT"}), m.Store(":memory:"), FM(sat), None)
    base_ms = BJ(9, 25, 14, 30)
    kbot.stocks.closes["SKHYNIXUSDT"] = m.Baseline(D("1768000"), "k", "l", 0, base_ms, "KRW", "韩交所000660·Naver KRX")
    kbot.snapshots["SKHYNIXUSDT"] = {"quote": m.Quote(D("1300"), sat - 2000, "mark")}
    kbot.anchors["SKHYNIXUSDT"] = (base_ms, D("1290"))
    odds = m.close_odds("SK 海力士", D("1768000"), D("1781705"), 0.03, 1.0, dt.date(2026, 9, 29), D("1000"), "09-25 14:30·Naver KRX",
                        "币安 1,300 / 收盘时刻 1,290 → +0.775%", "σ")
    ev = kbot.close_evidence("SK 海力士｜SKHYNIXUSDT", odds, sat)
    assert ev["proxy"] == {"proxy": "币安 SKHYNIXUSDT 合约", "price": 1300.0, "quoted_ms": sat - 2000, "anchor": 1290.0, "anchor_ms": base_ms,
                           "anchor_note": "收盘时刻的币安价格", "approx": False}, ev["proxy"]
    assert ev["sources"][0]["source"] == "韩交所000660·Naver KRX" and ev["sources"][1]["type"] == "标记价" and not ev["basis"]["direct"]
    # an evidence failure never stops a trade: the failure is the record
    assert sbot.evidence(lambda: 1 / 0) == {"error": "division by zero"}

    # the default scope: only the index / contract daily cards, only takers (SIM_MARKETS and SIM_WAYS widen it)
    dbot = m.Bot(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off"}),
                 m.Store(":memory:"), FM(NOW), None)
    dbot.sim_markets = lambda now: world["markets"]
    rep = dbot.sim_report()
    assert rep["ways"] == "只吃单" and rep["scope"] == "指数/个股日涨跌" and "范围：指数/个股日涨跌；只吃单。吃单按 100 份" in dbot.cmd_sim(None).text
    assert "挂单" not in dbot.cmd_sim(None).text.split("\n")[1] and dbot.sim_version()["sim_ways"] == "只吃单"
    far = m.SimMarket("will-bnb-hit-700-or-900", "BNB 先触 700/900", "touch", "BNB", 0.70, book([(0.55, 100)], [(0.58, 100)], NOW), 0.03, "",
                      ("$900", "$700"), {"low": 700, "high": 900, "deadline": CLOSE}, {})
    await step(dbot, NOW, hsi(0.70, [(0.55, 100)], [(0.58, 100)], NOW), far)  # 挂涨 +15¢ and 吃涨 +11¢ on both
    assert sorted(dbot.sim_trades()) == [f"{HSI_SLUG}|up|吃"], sorted(dbot.sim_trades())
    mbot = m.Bot(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off",
                                    "SIM_WAYS": "maker", "SIM_MARKETS": "touch"}), m.Store(":memory:"), FM(NOW), None)
    mbot.sim_markets = lambda now: world["markets"]
    await step(mbot, NOW, hsi(0.70, [(0.55, 100)], [(0.58, 100)], NOW), far)
    assert sorted(mbot.sim_trades()) == ["will-bnb-hit-700-or-900|up|挂"], sorted(mbot.sim_trades())
    assert "范围：先触价；只挂单。挂单只挂在双边都有报价、价差不超过 10¢ 的盘口，排在已有挂单之后" in mbot.cmd_sim(None).text

    # --- a resting order is placed only on a book it could fill in: two-sided, no wider than SIM_MAKER_SPREAD ---------------
    assert m.SIM_MAKER_SPREAD == 0.10 and m.sim_maker_block(book([("0.55", "100")], [("0.58", "100")])) == ""
    assert m.sim_maker_block(book([("0.55", "100")], [])) == "盘口只有一边" and m.sim_maker_block(book([], [("0.58", "100")])) == "盘口只有一边"
    assert m.sim_maker_block(book([("0.003", "50")], [("0.80", "100")])) == "买卖价差 79.7¢ 超过 10.0¢"
    assert m.sim_maker_block(book([("0.60", "50")], [("0.58", "100")])).startswith("盘口交叉") and not m.book_crossed(book([("0.55", "1")], [("0.58", "1")]))
    gbot = m.Bot(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off",
                                    "SIM_WAYS": "both", "SIM_MARKETS": "all"}), m.Store(":memory:"), FM(NOW), None)
    gbot.sim_markets = lambda now: world["markets"]
    lone = lambda name, bids, asks, fair: m.SimMarket(name, name, "close", "X", fair, book(bids, asks, NOW, name, mid=name), 0.03, "", ("涨", "跌"), {})
    # the 10-03 price-ladder case: a lone 0.3¢ bid under an 80¢ ask shows 挂涨 +58.7¢, but nobody sells into it: no order;
    # a one-sided book neither; a 10¢ spread is still a book
    await step(gbot, NOW, lone("lone", [("0.003", "50")], [("0.80", "100")], 0.59), lone("oneside", [("0.55", "100")], [], 0.70),
               lone("tight", [("0.55", "100")], [("0.65", "100")], 0.70))
    assert sorted(gbot.sim_trades()) == ["tight|up|挂"], sorted(gbot.sim_trades())

    # --- a resting order the trader no longer places is withdrawn, not left to the result ----------------------------------
    # (10-03: 54 price-ladder makers at 0.1–0.3¢ from before the ladders went taker-only sat as 挂单中 0/100 until month end)
    resting = lambda market, kind, shares=0.0: {
        "v": 2, "market": market, "slug": market.partition("#")[0], "market_id": market.partition("#")[2], "item": market, "kind": kind,
        "key": "X", "side": "down", "label": "挂No", "maker": True, "fair": 0.59, "opened": NOW - 3_600_000, "settle": {"end": BJ(11, 1, 11, 59)},
        "order": 100.0, "fills": [{"at": NOW - 1_800_000, "shares": shares, "fair": 0.5, "how": "推定成交"}] if shares else [], "revisions": [],
        "entry": {}, "version": {}, "price": 0.003, "signal": 0.587, "shares": shares, "status": "resting",
        "filled": NOW - 1_800_000 if shares else None, "queue_ahead": 50.0, "queue_min": 50.0, "edge": 0.587}
    for b in (dbot, gbot):
        b.store.put("sim:sol#11|down|挂", resting("sol#11", "range"))
        b.store.put("sim:sol#12|down|挂", resting("sol#12", "range", 30.0))
    gbot.store.put("sim:hsi-x|up|挂", resting("hsi-x", "close"))  # a kind and a way gbot still trades
    mbot.store.put("sim:hsi-y|up|挂", resting("hsi-y", "close"))  # mbot trades touch markets only
    world["markets"] = []
    for b in (dbot, gbot, mbot):
        await step(b, NOW + 60_000)
    t = dbot.sim_trades()["sol#11|down|挂"]
    assert t["status"] == "cancelled" and t["note"] == "撤单：模拟交易已改为只吃单，一份都没成交", t
    assert t["withdrawn"] == {"at": NOW + 60_000, "why": "模拟交易已改为只吃单", "unfilled": 100.0} and t["unfilled"] == 100
    assert m.sim_status(t) == "已撤单" and m.sim_state(t) == "" and not dbot.sim_due(t, BJ(12, 1, 0, 0))  # nothing to confirm, ever
    t = dbot.sim_trades()["sol#12|down|挂"]  # what had filled stays a position, the rest lapses now
    assert t["status"] == "filled" and t["shares"] == 30 and t["unfilled"] == 70 and m.sim_status(t) == "持仓"
    assert t["note"] == "撤单：模拟交易已改为只吃单；已推定成交的 30 份继续持有，其余 70 份作废", t["note"]
    g = gbot.sim_trades()
    assert g["sol#11|down|挂"]["withdrawn"]["why"] == "价格阶梯只做吃单" and g["sol#12|down|挂"]["status"] == "filled"
    assert g["hsi-x|up|挂"]["status"] == "resting" and "withdrawn" not in g["hsi-x|up|挂"]  # still traded: it stands
    assert mbot.sim_trades()["hsi-y|up|挂"]["withdrawn"]["why"] == "模拟交易范围已不含指数/个股日涨跌"
    await step(dbot, NOW + 120_000)  # withdrawn once: the record does not change again
    assert dbot.sim_trades()["sol#11|down|挂"]["withdrawn"]["at"] == NOW + 60_000
    tot = dbot.sim_report()["total"]
    assert tot["cancelled"] == 1 and tot["resting"] == 0 and tot["open"] == 2 and tot["partial"] == 1 and tot["expired"] == 0, tot
    assert "｜撤单 1 笔" in dbot.cmd_sim(None).text and "撤单" not in bot.cmd_sim(None).text  # counted only when there are any
    j = {x["id"]: x for x in dbot.journal_payload()["trades"]}
    assert j["sol#11|down|挂"]["wait"] == "" and j["sol#11|down|挂"]["text"] == "已撤单"
    assert j["sol#12|down|挂"]["wait"] == "碰到档位即 Yes；否则等窗口结束 11-01 11:59 后按 No（11-01 12:59 起）"
    assert "已撤单" in dbot.journal_csv() and json.dumps(dbot.journal_payload())

    await browser_check(bot)
    print("SIM_OK")


async def browser_check(bot):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        async_playwright = None
    chrome = next((p for p in [os.environ.get("CHROMIUM_PATH", ""), "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"]
                   if p and os.path.exists(p)), "")
    if async_playwright is None or not (chrome or os.environ.get("PLAYWRIGHT_BROWSERS_PATH")):
        print("browser check skipped (no Playwright/Chromium)")
        return
    web = m.WebServer(bot, 0, "t" * 20); web.CACHE_SECONDS = {}; port = await web.start()  # the tests change the payload and reload at once
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**({"executable_path": chrome} if chrome else {}))
        page = await browser.new_page(viewport={"width": 390, "height": 900})
        await page.goto(f"http://127.0.0.1:{port}/p/{'t' * 20}")
        await page.wait_for_selector(".meta span")
        # the 模拟交易 section starts hidden; 自定义 shows it again (and links the review page)
        assert not await page.is_visible("#g-sim .card") and not await page.is_visible("#h-sim")
        await page.click("#edit")
        assert await page.get_attribute("#journal", "href") == f"/p/{'t' * 20}/journal"
        await page.check("#secs input[data-sec=sim]")
        await page.click("#done")
        await page.wait_for_selector("#g-sim .card")
        text = await page.inner_text("#g-sim .card")
        tot = bot.sim_report()["total"]
        assert "已结算盈亏" in text and f"已结算 {tot['settled']} 笔" in text and "结果不一致 2 笔" in text and "成交时" in text, text
        assert await page.get_attribute("#g-sim a.simj", "href") == f"/p/{'t' * 20}/journal"
        await page.click("#g-sim details summary")
        assert await page.locator("#g-sim a.simrow").count() == len(bot.sim_report()["rows"])
        assert "· 结果不一致" in await page.inner_text("#g-sim .simrows")
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "no sideways scroll"
        if os.environ.get("WEB_SCREENSHOT"):
            await page.locator("#g-sim .card").screenshot(path=os.environ["WEB_SCREENSHOT"])
        await browser.close()
    await web.stop()


asyncio.run(run())
