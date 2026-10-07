"""Common risk and markouts in the paper trader. Ten markets can be one risk: the open positions are grouped by the
event that settles them (a token's ladder, a Binance pair, an index on a day), the single move that hurts each group
most is priced, SIM_GROUP_USD keeps a group's worst move under a cap (a refused buy is recorded), and the sources the
positions rest on are counted. After a fill, where the market's middle stands 1 / 5 / 30 minutes later is kept: a fill
the market then runs away from was someone's informed exit."""
import asyncio, csv, io, sys, json, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
BJ = lambda mo, d, h, mi=0: int(dt.datetime(2026, mo, d, h, mi, tzinfo=m.BEIJING).timestamp() * 1000)
NOW = BJ(10, 7, 10, 0)
END = BJ(11, 1, 11, 59)

# --- the driver: the one event a market settles on ----------------------------------------------------------------------
assert m.market_driver("close", "HSI", {"target": "2026-10-07"}, "恒生指数") == ("HSI@2026-10-07", "恒生指数（10-07）")
assert m.market_driver("close", "UNITREEUSDT", {"target": "2026-10-08"}, "宇树 UNITREE") == ("UNITREEUSDT@2026-10-08", "宇树 UNITREE（10-08）")
assert m.market_driver("close", "HSI", {"target": "2026-10-08"}, "恒生指数")[0] != m.market_driver("close", "HSI", {"target": "2026-10-07"})[0]
# BTC 先触, BTC 10月涨跌 and the BTC price ladder all follow BTCUSDT; STRC is its own; a flip its pair; a ladder its token
assert m.market_driver("touch", "BTC", {}, "BTC 先触 70k/90k") == ("BTCUSDT", "BTC")
assert m.market_driver("updown", "BTC-2026-10", {}, "BTC 10月涨跌") == ("BTCUSDT", "BTC")
assert m.market_driver("range", "BTC-HIT-10", {"target": "120000", "dir": "up"}, "BTC 10月价格 ↑ $120k") == ("BTCUSDT", "BTC")
assert m.market_driver("updown", "ETH-2026-10", {}, "ETH 10月涨跌") == ("ETHUSDT", "ETH") == m.market_driver("touch", "ETH", {}, "")
assert m.market_driver("range", "STRC-100", {}, "STRC 触及 $100") == ("STRC", "STRC")
assert m.market_driver("flip", "HYPE-SOL", {}, "HYPE 反超 SOL") == ("HYPE/SOL", "HYPE/SOL")
assert m.market_driver("ladder", "PONS", {"target": "700000000"}, "$PONS FDV $700M") == ("PONS", "$PONS FDV")
assert m.market_driver("ladder", "NEW", {}, "$NEW FDV $1M") == ("NEW", "$NEW FDV")  # no spec: named from the card
assert m.trade_driver({"driver": "X", "driver_name": "x!"}) == ("X", "x!")
assert m.trade_driver({"kind": "touch", "key": "ETH"}) == ("ETHUSDT", "ETH")  # saved before the driver was kept

# --- the single move that hurts a group most ---------------------------------------------------------------------------
pos = lambda kind, side, price, shares=100, **settle: {"kind": kind, "side": side, "settle": settle, "price": price, "shares": shares}
nos = [pos("ladder", "down", 0.84, target="300000000"), pos("ladder", "down", 0.90, target="500000000"),
       pos("ladder", "down", 0.95, target="1000000000")]
worst, event = m.group_worst_case(nos, "$PONS FDV")
assert abs(worst + 269) < 1e-9 and event == "$PONS FDV 涨到 $1B", (worst, event)  # three markets, one pump
# the pump to $500M only: the $1B No still pays; a Yes at $1B is a hedge, so the worst move stops at $500M
worst, event = m.group_worst_case([*nos, pos("ladder", "up", 0.05, target="1000000000")], "$PONS FDV")
assert abs(worst - (-84 - 90 + 5 - 5)) < 1e-9 and event == "$PONS FDV 涨到 $500M", (worst, event)
# Yes holders lose when nothing is touched
worst, event = m.group_worst_case([pos("ladder", "up", 0.3, target="300000000"), pos("ladder", "up", 0.1, target="1000000000")], "$PONS FDV")
assert abs(worst + 40) < 1e-9 and event == "$PONS FDV 都没触及", (worst, event)
# a price ladder's ↓ levels and a first-touch market fall together on a crash
mixed = [pos("range", "down", 0.8, target="60000", dir="down"), pos("range", "up", 0.2, target="90000", dir="up"),
         pos("touch", "up", 0.55, low=70000, high=90000)]
worst, event = m.group_worst_case(mixed, "BTC")
assert abs(worst + 155) < 1e-9 and event == "BTC 跌到 $60k", (worst, event)
assert m.group_worst_case([pos("close", "up", 0.6), pos("close", "up", 0.7, shares=50)], "恒生指数（10-07）") == (-95.0, "恒生指数（10-07）收跌")
assert m.group_worst_case([pos("updown", "down", 0.4)], "BTC") == (-40.0, "BTC 收涨")
assert m.group_worst_case([pos("touch", "down", 0.45)], "BNB") == (-45.0, "BNB 先触高线")
assert m.group_worst_case([pos("flip", "up", 0.3)], "HYPE/SOL") == (-30.0, "HYPE/SOL 没反超")
assert m.scenario_result("close", {}, "flat", None) == 0.5 and m.scenario_result("flip", {}, "flat", None) == 0.0
assert m.scenario_result("range", {"target": "60000", "dir": "down"}, "down", 65000.0) == 0.0  # the crash stopped above it
assert m.scenario_result("range", {"target": "60000", "dir": "down"}, "down", None) == 1.0

# --- groups and sources over trade records (a resting order waits beside the positions; old records derive their driver)
trade = lambda tid, kind, key, side, price, shares, status="filled", **extra: {
    "market": tid.split("|")[0], "item": extra.pop("item", key), "kind": kind, "key": key, "side": side, "price": price,
    "shares": shares, "order": extra.pop("order", shares), "status": status, "settle": extra.pop("settle", {}), "edge": 0.1,
    "fair": price + 0.1, "maker": tid.endswith("挂"), "opened": NOW,
    "entry": {"sources": [{"what": "价格", "source": s} for s in extra.pop("sources", [])]}, **extra}
records = [trade("pons#1|down|吃", "ladder", "PONS", "down", 0.84, 100, item="$PONS FDV $300M", settle={"target": "300000000"}, sources=["DexScreener"]),
           trade("pons#2|down|吃", "ladder", "PONS", "down", 0.90, 100, item="$PONS FDV $500M", settle={"target": "500000000"}, sources=["DexScreener"]),
           trade("pons#3|down|挂", "ladder", "PONS", "down", 0.95, 0, "resting", order=100, item="$PONS FDV $1B", settle={"target": "1000000000"}, sources=["DexScreener"]),
           trade("hsi|up|吃", "close", "HSI", "up", 0.6, 100, item="恒生指数", settle={"target": "2026-10-07"}, sources=["etnet", "tencent 日K"]),
           trade("old|up|挂", "touch", "BNB", "up", 0.5, 100, item="BNB 先触 700/900", sources=["币安现货"]),
           trade("done|up|吃", "close", "KOSPI", "up", 0.5, 100, "settled", payout=1.0)]
groups = m.sim_groups(records)
assert [(g["name"], g["positions"], g["resting"]) for g in groups] == [("$PONS FDV", 2, 1), ("恒生指数（10-07）", 1, 0), ("BNB", 1, 0)], groups
pons = groups[0]
assert pons["driver"] == "PONS" and abs(pons["cost"] - 174) < 1e-9 and abs(pons["worst"] + 174) < 1e-9 and pons["event"] == "$PONS FDV 涨到 $500M"
assert abs(pons["share"] - 174 / 284) < 1e-9 and pons["resting_usd"] == 95 and pons["items"] == ["$PONS FDV $300M", "$PONS FDV $500M", "$PONS FDV $1B"]
assert groups[1]["event"] == "恒生指数（10-07）收跌" and groups[1]["worst"] == -60 and groups[2]["event"] == "BNB 先触低线"
sources = m.sim_sources(records)
assert [(s["source"], s["trades"], s["cost"]) for s in sources] == [("DexScreener", 3, 269.0), ("etnet", 1, 60.0), ("tencent 日K", 1, 60.0), ("币安现货", 1, 50.0)], sources
assert sources[0]["items"] == ["$PONS FDV $300M", "$PONS FDV $500M", "$PONS FDV $1B"]
assert m.sim_groups([]) == [] and m.sim_sources([]) == []
assert m.sim_stats(records)["markout"] == {}
# the markouts' middle for one side
bk = lambda bids, asks: m.PredictBook("K", "s", "1", "t", tuple((D(p), D(q)) for p, q in bids), tuple((D(p), D(q)) for p, q in asks), NOW)
assert m.side_mid(bk([("0.20", "1")], [("0.26", "1")]), "up") == 0.23 and abs(m.side_mid(bk([("0.20", "1")], [("0.26", "1")]), "down") - 0.77) < 1e-12
assert m.side_mid(bk([], [("0.26", "1")]), "up") == 0.26 and m.side_mid(bk([("0.20", "1")], []), "down") == 0.8 and m.side_mid(bk([], []), "up") is None


class FM:
    def __init__(self, now): self.now, self.config = now, None
    def now_ms(self): return self.now


def book(bids, asks, at, mid):
    return m.PredictBook("PONS", "what-fdv-will-pons-hit-before-nov-2026", mid, "t", tuple((D(p), D(q)) for p, q in bids),
                         tuple((D(p), D(q)) for p, q in asks), at, 200)


LEVELS = (("300000000", "$300M", "1"), ("500000000", "$500M", "2"), ("700000000", "$700M", "3"), ("1000000000", "$1B", "4"))


def ladder(at, bids=(("0.14", "500"),), asks=(("0.16", "500"),), fair=0.02):
    """Four $PONS levels, each suggesting 吃No: fair No 98¢ against a No cost of 86¢ + fee (net +11.7¢)."""
    return [m.SimMarket(f"what-fdv-will-pons-hit-before-nov-2026#{mid}", f"$PONS FDV {label}", "ladder", "PONS", fair, book(bids, asks, at, mid),
                        0.02, "", ("Yes", "No"), {"target": target, "end": END},
                        {"basis": {"cap": 7e7, "target": float(target)}, "sources": [{"what": "价格", "source": "DexScreener"},
                                                                                     {"what": "供应量", "source": "DexScreener FDV ÷ 价格"}]})
            for target, label, mid in LEVELS]


async def run():
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off",
                             "WEB_PORT": "8080", "SIM_MARKETS": "all"})
    assert cfg.sim_group_usd == 300 and m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SIM_GROUP_USD": "0"}).sim_group_usd == 0
    for bad in ({"SIM_GROUP_USD": "-1"}, {"SIM_GROUP_USD": "x"}):
        try: m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", **bad}); assert False, bad
        except ValueError: pass
    store = m.Store(":memory:")
    bot = m.Bot(cfg, store, FM(NOW), None)
    world = {"markets": []}
    bot.sim_markets = lambda now: world["markets"]

    async def step(at, markets):
        bot.market.now, bot.sim_ran = at, -1e9
        world["markets"] = list(markets)
        await bot.sim_step(at)

    # --- four No suggestions on one ladder: three are bought ($86 each), the fourth would take the pump past $1B to −$345
    await step(NOW, ladder(NOW))
    trades = bot.sim_trades()
    assert sorted(t["item"] for t in trades.values()) == ["$PONS FDV $300M", "$PONS FDV $500M", "$PONS FDV $700M"], sorted(trades)
    cost = sum(t["price"] * t["shares"] for t in trades.values())
    t3 = trades["what-fdv-will-pons-hit-before-nov-2026#3|down|吃"]
    assert (t3["driver"], t3["driver_name"]) == ("PONS", "$PONS FDV") and abs(t3["price"] - (0.86 + 0.02 * 0.14)) < 1e-12
    r = bot.sim_report()
    (g,) = r["groups"]
    assert (g["name"], g["positions"], g["markets"], g["resting"]) == ("$PONS FDV", 3, 3, 0) and abs(g["cost"] - cost) < 1e-9
    assert abs(g["worst"] + cost) < 1e-9 and g["event"] == "$PONS FDV 涨到 $700M" and g["share"] == 1.0 and g["kinds"] == ["ladder"], g
    assert [(s["source"], s["trades"]) for s in r["sources"]] == [("DexScreener", 3), ("DexScreener FDV ÷ 价格", 3)]
    (b,) = r["blocks"]
    assert b["item"] == "$PONS FDV $1B" and b["label"] == "吃No" and abs(b["price"] - t3["price"]) < 1e-12 and b["at"] == NOW and b["first"] == NOW
    assert b["why"].startswith("$PONS FDV 涨到 $1B时这组仓位合计将亏 $345.12，超过组上限 $300（$PONS FDV 已有 3 笔") and r["group_cap"] == 300, b["why"]
    assert (b["driver"], b["name"], b["id"]) == ("PONS", "$PONS FDV", "what-fdv-will-pons-hit-before-nov-2026#4|down|吃")
    text = bot.cmd_sim(None).text
    assert "🧩 最坏单一事件：$PONS FDV 涨到 $700M → −$258.84（$PONS FDV 3 笔，占持仓成本 100%）" in text, text
    assert "📡 持仓依赖的数据源：DexScreener 3 笔 $259｜DexScreener FDV ÷ 价格 3 笔 $259" in text, text
    assert "⛔ 组上限 $300 一周内拦下 1 笔；最近 10-07 10:00 $PONS FDV $1B 吃No @ 86.3¢：" in text, text
    # the review page carries the groups, the sources and the refused buys; the CSV the driver and the markouts
    j = bot.journal_payload()
    assert j["groups"][0]["event"] == "$PONS FDV 涨到 $700M" and j["blocks"][0]["item"] == "$PONS FDV $1B" and j["group_cap"] == 300
    assert all(t["driver_name"] == "$PONS FDV" for t in j["trades"])
    rows = list(csv.reader(io.StringIO(bot.journal_csv()[1:])))
    assert rows[0][-4:] == ["共同风险组", "成交后1分钟市价变动", "成交后5分钟市价变动", "成交后30分钟市价变动"]
    assert [r[-4] for r in rows[1:]] == ["$PONS FDV"] * 3 and all(r[-3:] == ["", "", ""] for r in rows[1:])
    json.dumps(j)

    # --- markouts: the No middle a minute after the fills has moved from 85¢ to 77¢ (sellers hit the old quotes) ---------
    gen = store.touched.get("simblock", 0)
    await step(NOW + 60_000, ladder(NOW + 60_000, [("0.20", "500")], [("0.26", "500")]))
    t = bot.sim_trades()["what-fdv-will-pons-hit-before-nov-2026#1|down|吃"]
    mo = t["markout"]
    assert set(mo) == {"1m"} and mo["1m"]["after_s"] == 60 and abs(mo["1m"]["mid"] - 0.77) < 1e-12 and abs(mo["1m"]["move"] - (0.77 - 0.86)) < 1e-12, mo
    assert mo["1m"]["fair"] == 0.98 and mo["1m"]["fair_move"] == 0.0 and mo["1m"]["at"] == NOW + 60_000
    # the $1B No is refused again at its new price: a changed reason is recorded at once (keeping the first time), the
    # same reason within ten minutes is not written again
    (b,) = bot.sim_report()["blocks"]
    assert b["at"] == NOW + 60_000 and b["first"] == NOW and "$339.24" in b["why"] and store.touched.get("simblock", 0) == gen + 1, b
    await step(NOW + 120_000, ladder(NOW + 120_000, [("0.20", "500")], [("0.26", "500")]))
    assert store.touched.get("simblock", 0) == gen + 1 and bot.sim_report()["blocks"][0]["at"] == NOW + 60_000
    # no book (stale) at the 5-minute mark: taken at the next look, with the real interval; the model's view is kept too
    await step(NOW + 300_000, ladder(NOW, [("0.20", "500")], [("0.26", "500")]))
    assert "5m" not in bot.sim_trades()["what-fdv-will-pons-hit-before-nov-2026#1|down|吃"]["markout"]
    await step(NOW + 420_000, ladder(NOW + 420_000, [("0.30", "500")], [("0.34", "500")], fair=0.10))
    mo = bot.sim_trades()["what-fdv-will-pons-hit-before-nov-2026#1|down|吃"]["markout"]
    assert mo["5m"]["after_s"] == 420 and abs(mo["5m"]["mid"] - 0.68) < 1e-12 and abs(mo["5m"]["fair_move"] - (0.90 - 0.98)) < 1e-12, mo
    await step(NOW + 1_800_000, ladder(NOW + 1_800_000, [("0.10", "500")], [("0.12", "500")]))
    mo = bot.sim_trades()["what-fdv-will-pons-hit-before-nov-2026#1|down|吃"]["markout"]
    assert abs(mo["30m"]["move"] - (0.89 - 0.86)) < 1e-12 and set(mo) == {"1m", "5m", "30m"}
    stats = bot.sim_report()["total"]["markout"]
    assert stats["1m"]["n"] == 3 and abs(stats["1m"]["avg"] + 0.09) < 1e-12 and abs(stats["5m"]["avg"] + 0.18) < 1e-12 and abs(stats["30m"]["avg"] - 0.03) < 1e-12, stats
    assert abs(stats["5m"]["fair"] + 0.08) < 1e-12
    text = bot.cmd_sim(None).text
    assert "📈 成交后市场走向（盘口中间价 − 成交价，每份）：1 分钟 -9.0¢（3 笔）｜5 分钟 -18.0¢（3 笔）｜30 分钟 +3.0¢（3 笔）；持续为负" in text, text
    rows = list(csv.reader(io.StringIO(bot.journal_csv()[1:])))
    assert rows[1][-3:] == ["-0.09", "-0.18", "0.03"], rows[1][-3:]
    card = next(i for i in bot.odds_payload()["items"] if i["name"] == "模拟交易")["sim"]
    assert card["groups"][0]["name"] == "$PONS FDV" and card["total"]["markout"]["1m"]["n"] == 3 and card["blocks"][0]["label"] == "吃No"

    # --- a resting order counts as if filled for the cap; a position on the other side of the event adds nothing ---------
    cfg150 = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off",
                                "SIM_MARKETS": "all", "SIM_WAYS": "both", "SIM_GROUP_USD": "150"})
    rbot = m.Bot(cfg150, m.Store(":memory:"), FM(NOW), None)
    resting = {"pons#9|down|挂": trade("pons#9|down|挂", "ladder", "PONS", "down", 0.90, 0, "resting", order=100, settle={"target": "300000000"})}
    one_b = ladder(NOW)[3]
    assert rbot.sim_group_room(resting, one_b, "down", 0.70, 100).startswith("$PONS FDV 涨到 $1B时这组仓位合计将亏 $160.00，超过组上限 $150")
    assert rbot.sim_group_room(resting, one_b, "up", 0.30, 100) == ""  # Yes at $1B: the pump pays it, the crash costs 30
    assert rbot.sim_group_room(resting, one_b, "down", 0.50, 100) == "" and rbot.sim_group_room({}, one_b, "down", 0.95, 100) == ""
    bot.config = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off",
                                    "SIM_MARKETS": "all", "SIM_GROUP_USD": "0"})
    await step(NOW + 1_900_000, ladder(NOW + 1_900_000))  # no cap: the fourth No is bought
    assert len(bot.sim_trades()) == 4 and bot.sim_report()["groups"][0]["positions"] == 4
    print("RISK_OK")


asyncio.run(run())
