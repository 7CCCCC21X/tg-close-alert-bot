"""Prediction snapshots and /calib: saved when the model answers, scored only against official closes."""
import asyncio, sys, math, random, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D

# --- pure report: a proxy whose true coefficient is 0.6, while the live model uses β 1 ---------------
rng = random.Random(7)
preds, outcomes = [], {}
start = dt.date(2026, 6, 1)
for i in range(30):
    day = (start + dt.timedelta(days=i)).isoformat()
    ref = 7000.0
    move = rng.gauss(0, 0.012)
    close = ref * math.exp(0.6 * move + rng.gauss(0, 0.008))
    outcomes[f"KOSPI:{day}"] = close
    for j in range(4):  # several snapshots of the same target day
        eff = ref * math.exp(1.0 * move)
        sigma, R = 0.02, 1.0
        up = 1 - m.norm_cdf(math.log(ref / eff) / sigma)
        preds.append({"key": "KOSPI", "t": i * 10 + j, "target": day, "mode": "盘后", "ref": ref, "eff": eff,
                      "move": move, "beta": 1.0, "sigma": sigma, "R": R, "up": up})
preds.append({"key": "KOSPI", "t": 999, "target": "2026-12-31", "mode": "盘后", "ref": 7000.0, "eff": 7000.0,
              "move": 0.0, "beta": 1.0, "sigma": 0.02, "R": 1.0, "up": 0.5})  # not resolved yet
text = "\n".join(m.calibration_report(preds, outcomes))
assert "📐 KOSPI·盘后：快照 121 条（31 个目标日），已有结果 120 条 / 30 日" in text, text
assert "现行模型：Brier " in text and "校准：" in text, text
b = float(text.split("系数 b ")[1].split("（")[0]); assert 0.4 < b < 0.8, text
assert "逐日向前检验（20 日 80 条）：拟合模型 Brier" in text, text
k = float(text.split("残差 σ ")[1].split("%")[0]); assert 0.5 < k < 1.2, text  # true residual 0.8%/day
assert "95% 区间" in text and "区间按日数算" in text, text
few = "\n".join(m.calibration_report([p for p in preds if p["target"] < "2026-06-08"], outcomes))
# a rate's Wilson interval: 60 of 100 is 50–69%, not "60 give or take a point"; counted in days, 3 of 3 says little
lo, hi = m.wilson(60, 100)
assert abs(lo - 0.502) < 0.002 and abs(hi - 0.691) < 0.002 and m.wilson(0, 0) == (0.0, 1.0) and m.wilson(3, 3)[0] < 0.5 and m.wilson(3, 3)[1] == 1.0
# against the market it traded against: the Predict middle saved beside each snapshot (here: the model's number pulled 15
# points toward ½, which scores worse); rows without one are left out of that line
pulled = [dict(p, mkt=p["up"] + (0.5 - p["up"]) * 0.3) if i % 2 else p for i, p in enumerate(preds)]
text = "\n".join(m.calibration_report(pulled, outcomes))
line = next(x for x in text.splitlines() if x.startswith("  对比 Predict 盘口中间价"))
assert line.startswith("  对比 Predict 盘口中间价（60 条/30 日）：模型 Brier ") and "模型更接近结果的占" in line and "没有赢过市场" not in line, line
assert "对比 Predict" not in "\n".join(m.calibration_report(preds, outcomes))  # no middles saved: no comparison
assert "至少要 15 个才下结论" in few, few
assert m.calibration_report([], {}) == ["📐 还没有保存的预测快照（每个指数每 30 分钟存一条，需要概率功能开启）"]


# --- the bot saves snapshots every 30 min and closes as they become known ----------------------------
async def run():
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off"})
    store = m.Store(":memory:")
    bot = m.Bot(cfg, store, m.Binance(cfg), None)
    kst = dt.timezone(dt.timedelta(hours=9))
    t0 = int(dt.datetime(2026, 9, 26, 12, 0, tzinfo=kst).timestamp() * 1000)
    odds = m.close_odds("KOSPI", D("7080.92"), D("6984.76"), 0.0341, 1.0, dt.date(2026, 9, 28), D("0.01"),
                        "09-23 收盘", "HL KR200", "x", beta=1.0, mode="盘后")
    assert abs(odds.move - math.log(6984.76 / 7080.92)) < 1e-12
    bot.kospi_odds = lambda now: odds
    bot.sse_odds = lambda now: "暂缺"
    bot.kospi.quote = m.IndexQuote("KOSPI", D("7080.92"), None, None, None, None,
                                   int(dt.datetime(2026, 9, 23, 19, 15, tzinfo=kst).timestamp() * 1000), "Naver")
    for minutes in (0, 10, 29, 30, 61):
        bot.record_predictions(t0 + minutes * 60_000)
    saved = [v for _, v in store.items("pred:")]
    assert [p["t"] for p in saved] == [t0, t0 + 30 * 60_000, t0 + 61 * 60_000], saved
    assert saved[0]["target"] == "2026-09-28" and saved[0]["mode"] == "盘后" and abs(saved[0]["up"] - odds.fair_up) < 1e-12
    assert store.get("outcome:KOSPI:2026-09-23") == 7080.92 and not store.items("outcome:SSE")
    bot.kospi.quote = m.IndexQuote("KOSPI", D("7100"), None, None, None, None,
                                   int(dt.datetime(2026, 9, 28, 14, 0, tzinfo=kst).timestamp() * 1000), "Naver")
    bot.record_predictions(t0 + 90 * 60_000)
    assert not store.get("outcome:KOSPI:2026-09-28")  # before 15:30 the live level is not a close
    store.put("outcome:KOSPI:2026-09-28", 7100.0)
    text = bot.calibration_text()
    assert text.startswith("📐 概率模型回测（只评估，不会自动改参数）") and "已有结果 3 条 / 1 日" in text, text

    # the review's case: the page showed 涨 ~42% against Predict's target price, the snapshot must not say ~57%
    kcfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "SKHYNIXUSDT", "HSI_FUTURES": "off", "SSE_INDEX": "off"})
    kbot = m.Bot(kcfg, m.Store(":memory:"), m.Binance(kcfg), None)
    live = m.close_odds("KOSPI", D("6889.74"), D("6899.514"), 0.012, 0.5, dt.date(2026, 9, 29), D("0.01"), "09-28 收盘",
                        "KOSPI 现货 6,899.514（盘中直接用现货）", "σ")
    kbot.kospi_odds = lambda now: live
    kbot.sse_odds = kbot.hsi_odds = lambda now: None
    slug = "kospi-composite-index-up-or-down-on-september-29-2026"
    kbot.predict.strikes[slug] = (D("6910.89"), __import__("time").monotonic())
    kbot.record_predictions(t0)
    snap = [v for _, v in kbot.store.items("pred:KOSPI")][0]
    page = next(i for i in kbot.odds_payload()["items"] if i["name"] == "KOSPI")
    assert abs(snap["up"] - page["fair_up"]) < 1e-12 and snap["up"] < 0.45 and snap["up_raw"] > 0.55, (snap, page["fair_up"])
    assert snap["ref"] == 6910.89 and snap["strike"] == 6910.89 and snap["ref_raw"] == 6889.74, snap
    assert snap["slug"] == slug and snap["url"] == m.PREDICT_SITE + slug + "?ref=B00EA", snap
    # scored as displayed: a 6,905 close is "down" against the 6,910.89 target the card priced
    kbot.store.put(f"outcome:KOSPI:2026-09-29", 6905.0)
    report = kbot.calibration_text()
    assert "其中 1 条按 Predict 目标价评估" in report and "实际 0%" in report, report
    # the snapshot keeps the Predict middle the card was compared with (a fresh book for that very market)
    assert snap["mkt"] is None
    kbot.predict.books["KOSPI"] = m.PredictBook("KOSPI", slug, "1", "t", ((D("0.40"), D("100")),), ((D("0.44"), D("100")),), t0, None)
    kbot.pred_last.clear()
    kbot.record_predictions(t0 + 60_000)
    snap = [v for _, v in kbot.store.items("pred:KOSPI")][-1]
    assert abs(snap["mkt"] - 0.42) < 1e-12 and snap["t"] == t0 + 60_000, snap
    kbot.predict.books["KOSPI"] = m.dataclasses.replace(kbot.predict.books["KOSPI"], slug="another-day")
    kbot.pred_last.clear(); kbot.record_predictions(t0 + 120_000)
    assert [v for _, v in kbot.store.items("pred:KOSPI")][-1]["mkt"] is None  # a book for another market is not this one's
    report = kbot.calibration_text()
    assert "对比 Predict 盘口中间价（1 条/1 日）：模型 Brier 0.1" in report and "市场 0.176" in report, report

    # --- the all-market snapshots: every priced market every 2 hours, scored by kind once it has a result ------------------
    class FM:
        def __init__(self, now): self.now, self.config = now, None
        def now_ms(self): return self.now
    BJ = lambda mo, d, h, mi=0: int(dt.datetime(2026, mo, d, h, mi, tzinfo=m.BEIJING).timestamp() * 1000)
    now, close = BJ(10, 5, 10, 0), BJ(10, 5, 16, 10)
    mcfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off"})
    mbot = m.Bot(mcfg, m.Store(":memory:"), FM(now), None)
    hsi_slug = "hang-seng-index-up-or-down-on-october-5-2026"
    hbook = lambda at: m.PredictBook("HSI", hsi_slug, "1", "t", ((D("0.45"), D("300")),), ((D("0.48"), D("300")),), at, 200)
    hsi = lambda at, fair=0.28: m.SimMarket(hsi_slug, "恒生指数", "close", "HSI", fair, hbook(at), 0.02, "", ("涨", "跌"),
                                            {"key": "HSI", "target": "2026-10-05", "line": 24600.0, "close_ms": close})
    lad = m.SimMarket("pons#77", "$PONS FDV $300M", "ladder", "PONS", 0.30, m.PredictBook("PONS", "pons-fdv", "77", "t", (), (), now - 600_000, 200),
                      0.02, "σ 是先验值", ("Yes", "No"), {"target": "300000000", "end": BJ(11, 1, 11, 59)})
    mbot.sim_markets = lambda at: [hsi(at), lad]
    assert "市场快照" in [n for n, _ in mbot.reference_jobs()]
    assert await mbot.record_marks(now) == m.Refreshed("ok") and await mbot.record_marks(now + 3_600_000) is False  # 2 hours apart
    rows = [v for _, v in mbot.store.items("mark:")]
    assert [(r["market"], r["kind"], r["up"], r["mkt"] and round(r["mkt"], 9), r["hold"], r["driver"]) for r in rows] == [
        (hsi_slug, "close", 0.28, 0.465, "", "HSI@2026-10-05"), ("pons#77", "ladder", 0.30, None, "σ 是先验值", "PONS")], rows
    assert rows[0]["settle"]["line"] == 24600.0 and rows[0]["t"] == now and rows[0]["need"] == 0.02
    assert await mbot.record_marks(now + 2 * 3_600_000) == m.Refreshed("ok") and len(mbot.store.items("mark:")) == 4
    text = mbot.calibration_text()
    assert "📊 全市场快照（每 2 小时一条" in text and "指数/个股日涨跌：快照 2 条 / 1 个市场，还没有已出结果的市场" in text, text
    assert "市值阶梯：快照 2 条 / 1 个市场，还没有已出结果的市场" in text
    # the close comes in under the line: 跌; the model's 28¢ beat the market's 46.5¢ (scored once per market)
    mbot.note_outcome("HSI", "2026-10-05", 24550.0, "tencent 日K")
    mbot.market.now = close + 61 * 60_000
    text = mbot.calibration_text()
    assert "指数/个股日涨跌：1 个市场已出结果（2 条，共 1 个市场）：模型 Brier 0.078（抛硬币 0.250）" in text, text
    assert "对比 Predict 盘口中间价（2 条/1 个市场）：模型 Brier 0.078 / 对数损失 0.329｜市场 0.216 / 0.625｜模型更接近结果的占 100%" in text, text
    print("CALIB_OK")


asyncio.run(run())
