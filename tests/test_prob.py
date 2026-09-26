import asyncio, sys, time, math, json, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
kst = dt.timezone(dt.timedelta(hours=9))
def bj(y, mo, d, h, mi): return int(dt.datetime(y, mo, d, h, mi, tzinfo=m.BEIJING).timestamp() * 1000)
def kr(y, mo, d, h, mi): return int(dt.datetime(y, mo, d, h, mi, tzinfo=kst).timestamp() * 1000)

# --- reproduce the worked examples ------------------------------------------------------------
eff = D("7080.92") * D("1107.15") / D("1126.60")
assert abs(float(eff) - 6958.6726) < 0.001, eff
o = m.close_odds("KOSPI", D("7080.92"), eff, 0.03022993, 1.0, dt.date(2026, 9, 28), D("0.01"), "", "", "")
assert abs(o.z - (-0.57609)) < 1e-4 and abs(o.up * 100 - 28.2278) < 0.01 and abs(o.down * 100 - 71.7722) < 0.01, (o.z, o.up, o.down)
assert f"{o.fair_up * 100:.1f}" == "28.2" and f"{o.fair_down * 100:.1f}" == "71.8"
eff = D("24510.09") * D("24501") / D("24504")
assert abs(float(eff) - 24507.0893) < 0.001
h = m.close_odds("恒生指数", D("24510.09"), eff, 0.00979369, 1.0, dt.date(2026, 9, 28), D("0.01"), "", "", "")
assert f"{h.fair_up * 100:.1f}" == "49.5" and f"{h.fair_down * 100:.1f}" == "50.5", (h.up, h.down)
# flat is split in half; the three add to 1
k = m.close_odds("SK", D("1857000"), D("1850000"), 0.03, 1.0, dt.date(2026, 9, 28), m.price_tick("kr", D("1857000")), "", "", "")
assert abs(k.up + k.flat + k.down - 1) < 1e-12 and k.flat > 0.001 and abs(k.fair_up - (k.up + k.flat / 2)) < 1e-12
# sensitivity table from the write-up (same price, different σ)
for sig, up in ((0.016642, 14.77), (0.043580, 34.47)):
    assert abs(m.close_odds("x", D("7080.92"), D("7080.92") * D("1107.15") / D("1126.60"), sig, 1, dt.date.today(), D("0.01"), "", "", "").up * 100 - up) < 0.01

# --- ticks, sessions, volatility ----------------------------------------------------------------
assert m.price_tick("kr", D("1857000")) == 1000 and m.price_tick("kr", D("45000")) == 50 and m.price_tick("hk", D("35.95")) == D("0.05")
assert m.price_tick("hk", D("15")) == D("0.02") and m.price_tick("sh", D("488")) == D("0.01") and m.price_tick("hk", D("1"), index=True) == D("0.01")
frac, target = m.session_remaining("kr", kr(2026, 9, 25, 10, 0), dt.date(2026, 9, 24))
assert target == dt.date(2026, 9, 25) and abs(frac - 330 / 390) < 1e-9, frac               # 09:00–15:30 = 390 min, 330 left
frac, target = m.session_remaining("hk", bj(2026, 9, 25, 12, 30), dt.date(2026, 9, 24))
assert abs(frac - 180 / 330) < 1e-9 and target == dt.date(2026, 9, 25)                        # lunch break counts nothing
frac, target = m.session_remaining("sh", bj(2026, 9, 25, 8, 0), dt.date(2026, 9, 24)); assert frac == 1 and target == dt.date(2026, 9, 25)
frac, target = m.session_remaining("sh", bj(2026, 9, 25, 15, 5), dt.date(2026, 9, 24)); assert abs(frac - 1 / 240) < 1e-9  # closed, not final yet
frac, target = m.session_remaining("sh", bj(2026, 9, 25, 15, 20), dt.date(2026, 9, 25)); assert frac == 1 and target == dt.date(2026, 9, 28)  # Fri → Mon
frac, target = m.session_remaining("kr", kr(2026, 9, 26, 12, 0), dt.date(2026, 9, 23)); assert frac == 1 and target == dt.date(2026, 9, 28)  # Saturday
s, n = m.realised_vol([D(100), D(101), D(99), D(100)]); assert n == 3 and 0.01 < s < 0.02
book = m.VolBook({"HSI": 0.012})
assert book.get("HSI", "HSI") == (0.012, "PROB_VOL 手动设定") and book.get("X", "sh")[0] == 0.035 and "暂无历史" in book.get("X", "sh")[1]
book.record("X", [D(100 * (1.02 if i % 2 else 1)) for i in range(21)], "币安日K")
sig, note = book.get("X", "sh"); assert 0.025 < sig < 0.035 and "币安日K 20 日" in note, (sig, note)
assert not book.due("HSI") and not book.due("X") and book.due("Y")
assert m.parse_prob_vol("unitreeusdt=3.5, HSI=1.2") == {"UNITREEUSDT": 0.035, "HSI": 0.012}
for bad in ["X=abc", "X=0", "X=150"]:
    try: m.parse_prob_vol(bad); assert False, bad
    except ValueError: pass
c = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x"}); assert c.probability and c.prob_vol == {}
assert not m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "PROBABILITY": "off"}).probability

# --- Bot integration ---------------------------------------------------------------------------
async def run():
    class FakeTelegram:
        def __init__(self): self.sent = []
        async def call(self, *a, **k): return True
        async def send(self, chat, thread, text, reply_markup=None, parse_mode=None): self.sent.append(text)
    class FakeMarket(m.Binance):
        def __init__(self, c): super().__init__(c); self.now = kr(2026, 9, 26, 12, 0); self.calls = []
        def now_ms(self): return self.now
        async def sync_clock(self): pass
        async def prices(self): return {"SKHYNIXUSDT": {"symbol": "SKHYNIXUSDT", "price": "1340", "time": self.now}}
        async def get(self, path, **p):
            self.calls.append((path, p.get("interval"), p.get("startTime")))
            if p.get("interval") == "1m": return [[p["startTime"], "1", "1", "1", "1350", "5", p["startTime"] + 59999]]
            if p.get("interval") == "1d": return [[0, 0, 0, 0, str(1300 + (i % 2) * 20), 0, 0] for i in range(32)]
            return []
    hl_calls = []
    async def fake_json(url, payload=None, timeout=15):
        hl_calls.append(payload["type"])
        if payload["type"] == "candleSnapshot":
            t = payload["req"]["endTime"]; return [{"t": t - 120000, "c": "1120"}, {"t": t - 60000, "c": "1126.60"}]
        return [{"universe": [{"name": "xyz:KR200"}, {"name": "xyz:SKHX"}]}, [{"markPx": "1107.15", "prevDayPx": "1120"}, {"markPx": "1340", "prevDayPx": "1350"}]]
    kospi_json = json.dumps({"datas": [{"closePrice": "7,080.92", "compareToPreviousClosePrice": "63.01", "compareToPreviousPrice": {"code": "2"},
                                         "fluctuationsRatio": "0.90", "localTradedAt": "2026-09-23T15:30:00+09:00", "marketStatus": "CLOSE"}]}).encode()
    async def fake_get(url, timeout=15, headers=None):
        if "index/KOSPI" in url: return kospi_json
        if "index/KPI200" in url: return kospi_json.replace(b"7,080.92", b"1,126.12")
        raise m.RemoteError("skip")
    m.http_json = fake_json; m.http_get = fake_get
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ADMIN_USER_ID": "42", "SYMBOLS": "SKHYNIXUSDT", "HSI_FUTURES": "off",
                             "FX_RATES": "KRW=1390", "PROB_VOL": "KOSPI=3.022993", "ALERT_THRESHOLD_PCT": "0.3", "MIN_ALERT_GAP_SECONDS": "0"})
    store0 = None
    store = m.Store(":memory:"); tg = FakeTelegram(); bot = m.Bot(cfg, store, FakeMarket(cfg), tg)
    krm = m.STOCK_MARKETS["kr"]
    bot.stocks.closes["SKHYNIXUSDT"] = m.StockMarket.baseline(m.StockTicker("kr", "000660"), krm, "Naver", dt.date(2026, 9, 23), D("1857000"), D("1841000"))
    store.put("subscriptions", {"1:0": {"chat": 1, "thread": 0, "active": True}})
    await bot.one_cycle()
    alert = [x for x in tg.sent if "下跌超过 0.3%" in x]; assert alert, tg.sent
    assert "🎲 09-28收 涨 <b>" in alert[0] and alert[0].index("🎲") < alert[0].index("📝"), alert[0]
    close_ms = kr(2026, 9, 23, 15, 30)
    assert bot.anchors["SKHYNIXUSDT"] == (close_ms, D("1350")), bot.anchors
    assert bot.anchors["KOSPI"] == (close_ms, D("1126.60")) and "candleSnapshot" in hl_calls, bot.anchors
    assert "SKHYNIXUSDT" in bot.vols.estimates and bot.vols.estimates["SKHYNIXUSDT"][1] == 30
    ko = bot.kospi_odds(bot.market.now_ms())
    assert isinstance(ko, m.CloseOdds) and ko.target == dt.date(2026, 9, 28) and f"{ko.fair_up * 100:.1f}" == "28.2", ko
    co = bot.contract_odds("SKHYNIXUSDT", D("1340"), bot.market.now_ms())
    assert isinstance(co, m.CloseOdds) and co.unit == "KRW" and abs(float(co.effective) - 1857000 * 1340 / 1350) < 1e-6 and co.target == dt.date(2026, 9, 28), co
    assert "币安 1,340 / 收盘时刻 1,350 → -0.741%" in co.proxy_note
    # /status rows
    tg.sent.clear(); await bot.process_message({"text": "/status", "chat": {"id": 1}, "from": {"id": 42}, "date": time.time()})
    st = tg.sent[-1]
    assert "🎲 KOSPI 09-28收 涨 <b>28.2¢</b>｜跌 <b>71.8¢</b>（有效 6,958.67·σ 3.02%）" in st, st
    assert "└ 🎲 09-28收 涨 <b>" in st or "├ 🎲 09-28收 涨 <b>" in st, st
    # /prob detail
    tg.sent.clear(); await bot.process_message({"text": "/prob", "chat": {"id": 1}, "from": {"id": 42}, "date": time.time()})
    pr = tg.sent[-1]
    assert "<b>📍 KOSPI</b>" in pr and "参考收盘 <b>7,080.92</b>（09-23 收盘）→ 目标 09-28 收盘" in pr and "σ 日 3.02%（PROB_VOL 手动设定）× √1.000 = 3.02%｜z -0.576" in pr, pr
    assert "HL KR200 1,107.15 / 收盘时刻 1,126.6 → -1.726%（KOSPI200 代理，存在基差）" in pr and "公平价 涨 <b>28.2¢</b> / 跌 <b>71.8¢</b>" in pr, pr
    assert "<b>📍 SK 海力士｜SKHYNIXUSDT</b>" in pr and "参考收盘 <b>1,857,000 KRW</b>（09-23 15:30 韩国时间·Naver）" in pr and "币安日K 30 日" in pr, pr
    assert "📍 恒生指数" not in pr  # HSI disabled
    # missing anchor / stale reference are explained instead of guessed
    bot.anchors.pop("SKHYNIXUSDT")
    assert bot.contract_odds("SKHYNIXUSDT", D("1340"), bot.market.now_ms()) == "等待币安在收盘时刻的价格"
    assert bot.odds_row("等待币安在收盘时刻的价格") == "🎲 概率暂缺：等待币安在收盘时刻的价格"
    bot.config = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ADMIN_USER_ID": "42", "SYMBOLS": "SKHYNIXUSDT", "PROBABILITY": "off"})
    assert bot.contract_odds("SKHYNIXUSDT", D("1340"), 0) is None and bot.kospi_odds(0) is None
    print("PROB_OK")

# --- HSI: night session anchored to the day-session close ------------------------------------------
async def hsi():
    class FakeTelegram:
        async def call(self, *a, **k): return True
        async def send(self, *a, **k): pass
    class FakeMarket:
        def __init__(self): self.config = None
        def now_ms(self): return bj(2026, 9, 25, 21, 0)
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "HK0625USDT", "PROB_VOL": "HSI=0.979369"})
    bot = m.Bot(cfg, m.Store(":memory:"), FakeMarket(), FakeTelegram())
    now = bj(2026, 9, 25, 21, 0)
    bot.hsi.quote = m.FuturesQuote("恒指期货(09/2026)夜市", D("24501"), D("24504"), None, None, None, bj(2026, 9, 25, 20, 59), "etnet", D("24510.09"), "etnet", D("24760"))
    o = bot.hsi_odds(now)
    assert isinstance(o, m.CloseOdds) and f"{o.fair_up * 100:.1f}" == "49.5" and o.target == dt.date(2026, 9, 28) and "日市收市 24,504" in o.proxy_note, o
    # between the day close and the night open: effective = the close → 50/50
    bot.hsi.quote = m.FuturesQuote("恒指期货(09/2026)日市", D("24527"), D("24691"), None, None, None, bj(2026, 9, 25, 16, 29), "etnet", D("24510.09"), "etnet", D("24760"))
    o = bot.hsi_odds(bj(2026, 9, 25, 17, 0)); assert abs(o.fair_up - 0.5) < 1e-9 and o.effective == D("24510.09"), o
    # cash session: live index against yesterday's close, remaining time shrinks
    bot.hsi.quote = m.FuturesQuote("x", D("24600"), D("24691"), None, None, None, bj(2026, 9, 28, 10, 0), "etnet", D("24600"), "etnet", D("24510.09"))
    o = bot.hsi_odds(bj(2026, 9, 28, 10, 0))
    assert o.ref == D("24510.09") and o.effective == D("24600") and o.target == dt.date(2026, 9, 28) and abs(o.remaining - 300 / 330) < 1e-9 and o.fair_up > 0.5, o
    print("HSI_PROB_OK")
asyncio.run(run()); asyncio.run(hsi())
