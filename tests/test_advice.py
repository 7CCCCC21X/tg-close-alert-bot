"""Trade suggestions only from inputs that hold: first-touch and market-cap cards stop recommending on a stale price,
an unverified or ambiguous path, and edges are net of fees, depth and model error."""
import asyncio, sys, math, time, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
m.CapMarket.GECKO_GAP = 0
D = m.D
BNB = m.TOUCH_MARKETS[0]
NIU = m.CAP_MARKETS[0]
NOW = int(dt.datetime(2026, 9, 29, 12, 0, tzinfo=m.BEIJING).timestamp() * 1000)
H = 3_600_000


class FM:
    def now_ms(self): return NOW


def best(item):
    return [e for e in (item.get("predict") or {}).get("edges") or [] if e["best"]]


async def run():
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off"})
    bot = m.Bot(cfg, m.Store(":memory:"), FM(), None)
    touch = bot.touches["BNB"]
    touch.start_ms = NOW - 30 * 24 * H
    touch.price, touch.priced_ms, touch.sigma, touch.sigma_ms = D("812.5"), NOW - 20_000, 0.5, NOW - H
    bot.store.put(f"touch:{BNB.slug}", {"kind": "clear", "through": NOW - H, "start": touch.start_ms})
    bot.predict.info[BNB.slug] = {"outcomes": ["$900", "$700"], "created_ms": touch.start_ms}
    bot.predict.books["BNB"] = m.PredictBook("BNB", BNB.slug, "1", "BNB", ((D("0.10"), D("500")),), ((D("0.12"), D("500")),), NOW)
    item = bot.touch_payload(touch, NOW)
    assert best(item) and not item["touch"]["hold"], item  # verified path, fresh price: the best edge is shown

    # the review's case 1: the price stopped 24 hours ago -> no probability at all, and nothing highlighted
    touch.priced_ms = NOW - 24 * H
    item = bot.touch_payload(touch, NOW)
    assert "missing" in item and "币安价格停在" in item["missing"] and "暂停概率" in item["missing"] and not best(item), item
    touch.priced_ms = NOW - 20_000
    # the review's case 2: one minute spanned both lines -> the order is unknown: shown, never recommended
    bot.store.put(f"touch:{BNB.slug}", {"kind": "ambiguous", "time": NOW - 5 * H, "hi": 905, "lo": 695, "start": touch.start_ms})
    item = bot.touch_payload(touch, NOW)
    assert item["predict"]["edges"] and not best(item) and "先后不明" in item["touch"]["hold"], item
    # the path not checked yet (or the window start unknown), or the check stuck hours ago: no suggestion either
    bot.store.put(f"touch:{BNB.slug}", {})
    assert not best(bot.touch_payload(touch, NOW)) and "尚未核验" in touch.advice_problem(NOW)
    bot.store.put(f"touch:{BNB.slug}", {"kind": "clear", "through": NOW - 5 * H, "start": touch.start_ms})
    assert not best(bot.touch_payload(touch, NOW)) and "核验停在" in touch.advice_problem(NOW)
    bot.store.put(f"touch:{BNB.slug}", {"kind": "clear", "through": NOW - H, "start": touch.start_ms})
    touch.start_ms = 0
    assert "开盘时间未知" in touch.advice_problem(NOW)
    touch.start_ms = NOW - 30 * 24 * H
    touch.sigma_ms = NOW - 30 * H
    assert "波动率" in touch.advice_problem(NOW) and not best(bot.touch_payload(touch, NOW))
    touch.sigma_ms = NOW - H
    assert best(bot.touch_payload(touch, NOW))

    # --- market-cap ladder: the same input check -----------------------------------------------------------------
    cap = bot.caps["NIULAI"]
    cap.price, cap.supply, cap.sigma, cap.sigma_kind, cap.priced_ms = D("0.083"), D("985000000"), 2.0, "bars", NOW - 30_000
    bot.store.put(f"cap:{NIU.slug}", {"start": NIU.start_ms, "high": 0.25, "at": 1, "through": NOW // 1000})
    rows = [m.LadderRow(t, str(i), f"${t}", m.PredictBook("NIULAI", NIU.slug, str(i), "t", ((D("0.10"), D("100")),),
                                                         ((D("0.14"), D("50")),), NOW)) for i, t in enumerate((D("2e8"), D("5e8")), 1)]
    bot.predict.ladders["NIULAI"] = rows
    for i in ("1", "2"):
        bot.predict.market_meta[i] = ({"outcomes": ["Yes", "No"], "status": "OPEN"}, time.monotonic())
    item = bot.cap_payload(cap, NOW)
    r200, r500 = item["ladder"]["rows"]
    assert "missing" not in item and r200["fair"] == 1.0 and 0 < r500["fair"] < 1 and r500["edges"], item
    cap.priced_ms = NOW - 24 * H  # DexScreener has failed for a day
    item = bot.cap_payload(cap, NOW)
    r200, r500 = item["ladder"]["rows"]
    assert "价格停在" in item["missing"] and "暂停概率与建议" in item["missing"], item
    assert r200["fair"] == 1.0 and r500["fair"] is None and "edges" not in r500, (r200, r500)  # reached stays reached
    print("ADVICE_GATE_OK")


# --- net edges: fee, depth, maker vs taker, and a threshold that covers the model's own error ---------------------------
assert abs(m.taker_fee(0.5, 200) - 0.01) < 1e-12 and abs(m.taker_fee(0.9, 200) - 0.002) < 1e-12 and m.taker_fee(0.5, 0) == 0
levels = ((0.85, 12.0), (0.94, 157.4))
avg, shares, short = m.taker_fill(levels, 100)
assert abs(shares - (12 + (100 - 12 * 0.85) / 0.94)) < 1e-9 and abs(avg - 100 / shares) < 1e-12 and not short, (avg, shares)
assert m.taker_fill(levels, 1000)[2] and m.taker_fill(levels, 0) == (0.85, 12.0, False) and m.taker_fill((), 10)[2]
book = m.PredictBook("HSI", "s", "7", "t", ((D("0.495"), D("40")), (D("0.45"), D("1000"))),
                     ((D("0.50"), D("40")), (D("0.53"), D("1000"))), NOW)
costs = m.EdgeCosts(200, 100)
e = {x.label: x for x in m.book_edges(0.514, book, costs)}
# taking 涨: $100 = 40 at 50¢ then 150.9 at 53¢ -> avg ≈ 52.4¢, fee 2% × min(p, 1 − p) ≈ 0.95¢
avg_up = 100 / (40 + (100 - 20) / 0.53)
assert abs(e["吃涨"].gross - 0.014) < 1e-12 and abs(e["吃涨"].slip - (avg_up - 0.50)) < 1e-12, e["吃涨"]
assert abs(e["吃涨"].fee - 0.02 * (1 - avg_up)) < 1e-12 and abs(e["吃涨"].edge - (0.514 - avg_up - e["吃涨"].fee)) < 1e-12
assert e["吃涨"].edge < 0 < e["吃涨"].gross   # +1.4¢ on the screen, a loss once fee and depth are paid
assert e["挂涨"].edge == e["挂涨"].gross and e["挂涨"].fee == 0  # makers pay no fee
assert m.best_edge(list(e.values()), 0.02) is None  # nothing clears 2¢ net (挂涨 +1.9¢ is inside the threshold)
old_best = m.best_edge(m.book_edges(0.514, book))     # the old gross view suggested a trade anyway
assert old_best and old_best.label == "挂涨"
# the market's own fee rate wins over the default; 0 bps -> no fee
free = m.dataclasses.replace(book, fee_bps=0)
assert [x.fee for x in m.book_edges(0.514, free, costs) if not x.maker] == [0, 0]
# model error: σ ×/÷ 1.25 for a live print; for a proxy-mapped estimate also β ± 0.25
live = m.close_odds("KOSPI", D("6889.74"), D("6899.51"), 0.012, 0.5, dt.date(2026, 9, 29), D("0.01"), "r", "KOSPI 现货（盘中直接用现货）", "σ")
proxy = m.close_odds("KOSPI", D("6889.74"), D("6960"), 0.012, 1.0, dt.date(2026, 9, 29), D("0.01"), "r", "HL KR200 代理", "σ", mode="盘后")
assert 0 < m.model_swing(live) < 0.05 and m.model_swing(proxy) > 0.05, (m.model_swing(live), m.model_swing(proxy))
flat = m.close_odds("x", D("100"), D("100"), 0.01, 1.0, dt.date(2026, 9, 29), D("0.01"), "r", "直接用现货", "σ")
assert m.model_swing(flat) < 1e-6  # a coin flip stays a coin flip whatever σ is
# settings
c = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x"})
assert (c.predict_fee_bps, c.predict_trade_usd, c.predict_min_edge) == (200, 100.0, 0.02)
c = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "PREDICT_FEE_BPS": "150", "PREDICT_TRADE_USD": "500", "PREDICT_MIN_EDGE_CENTS": "3.5"})
assert (c.predict_fee_bps, c.predict_trade_usd, abs(c.predict_min_edge - 0.035) < 1e-12) == (150, 500.0, True)
for bad in ({"PREDICT_FEE_BPS": "-1"}, {"PREDICT_TRADE_USD": "0"}, {"PREDICT_MIN_EDGE_CENTS": "x"}):
    try: m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", **bad}); assert False, bad
    except ValueError: pass


async def bot_view():
    class Clock:
        def now_ms(self): return int(dt.datetime(2026, 9, 29, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=9))).timestamp() * 1000)
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ADMIN_USER_ID": "42", "SYMBOLS": "SKHYNIXUSDT", "HSI_FUTURES": "off",
                             "SSE_INDEX": "off", "PREDICT_TRADE_USD": "100"})
    sent = []
    class TG:
        async def call(self, *a, **k): return True
        async def send(self, chat, thread, text, reply_markup=None, parse_mode=None): sent.append(text)
    bot = m.Bot(cfg, m.Store(":memory:"), Clock(), TG())
    now = bot.market.now_ms()
    odds = m.close_odds("KOSPI", D("6889.74"), D("6960"), 0.012, 0.3, dt.date(2026, 9, 29), D("0.01"), "09-28 收盘",
                        "KOSPI 现货 6,960（盘中直接用现货）", "σ")
    bot.kospi_odds = lambda ms: odds
    slug = "kospi-composite-index-up-or-down-on-september-29-2026"
    bot.predict.slugs["KOSPI"] = slug
    # fair 涨 ≈ 96%: 吃涨 at 88¢ is +8¢ gross; the book is thin above it
    bot.predict.books["KOSPI"] = m.PredictBook("KOSPI", slug, "9", "t", ((D("0.80"), D("50")),),
                                               ((D("0.88"), D("20")), (D("0.97"), D("500"))), now, fee_bps=200)
    item = next(i for i in bot.odds_payload()["items"] if i["name"] == "KOSPI")
    p = item["predict"]
    take = next(x for x in p["edges"] if x["label"] == "吃涨")
    assert take["slip"] > 0.05 and take["fee"] > 0 and take["edge"] < take["gross"] - 0.05, take  # $100 eats into 97¢
    assert p["need"] >= 0.02 and p["notional"] == 100.0, p
    best = [x for x in p["edges"] if x["best"]]
    assert len(best) == 1 and best[0]["label"] == "挂涨" and best[0]["edge"] > p["need"], p["edges"]
    await bot.process_message({"text": "/book", "chat": {"id": 1}, "from": {"id": 42}, "date": time.time()})
    text = sent[-1]
    assert "净优势门槛" in text and "👉 挂单 <b>挂涨</b> @ 80.0¢" in text and "按 $100 吃单：含深度与手续费" in text, text
    assert "👉 吃单" not in text, text  # the taker side does not clear the threshold once depth and fee are paid
    # with a deep book the taker side clears it too, and each side is suggested on its own terms
    bot.predict.books["KOSPI"] = m.dataclasses.replace(bot.predict.books["KOSPI"], asks=((D("0.88"), D("5000")),))
    await bot.process_message({"text": "/book", "chat": {"id": 1}, "from": {"id": 42}, "date": time.time()})
    text = sent[-1]
    assert "👉 挂单 <b>挂涨</b>" in text and "👉 吃单 <b>吃涨</b> @ 88.0¢" in text and "$100 约 114 份" in text, text
    print("ADVICE_OK")


asyncio.run(bot_view())
