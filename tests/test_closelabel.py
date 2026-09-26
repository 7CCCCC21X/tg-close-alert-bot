import sys, datetime as dt, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
tz8, tz9 = dt.timezone(dt.timedelta(hours=8)), dt.timezone(dt.timedelta(hours=9))
sh, hk, kr = m.STOCK_MARKETS["sh"], m.STOCK_MARKETS["hk"], m.STOCK_MARKETS["kr"]
assert hk.close_time == dt.time(16, 10) and kr.close_time == dt.time(15, 30) and sh.close_time == dt.time(15, 0)
day = dt.date(2026, 9, 18)
b_sh = m.StockMarket.baseline(m.StockTicker("sh", "688836"), sh, "腾讯", day, D("514.98"))
assert b_sh.close_text == "｜收盘 09-18 15:00（北京时间）", b_sh.close_text
b_hk = m.StockMarket.baseline(m.StockTicker("hk", "00625", True), hk, "东方财富", day, D("37.76"))
assert b_hk.close_text == "｜收盘 09-18 16:10（北京时间）" and b_hk.currency == "", b_hk.close_text
b_kr = m.StockMarket.baseline(m.StockTicker("kr", "000660"), kr, "Naver", day, D("1842000"))
assert b_kr.close_text == "｜收盘 09-18 15:30（韩国时间）＝北京 09-18 14:30", b_kr.close_text
assert dt.datetime.fromtimestamp(b_kr.close_ms / 1000, tz9).strftime("%H:%M") == "15:30"
unknown = m.StockMarket.baseline(m.StockTicker("kr", "000660"), kr, "Naver", None, D("1"))
assert unknown.close_text == "" and unknown.close_ms == 0
# reference line carries the venue-local text; manual records keep the Beijing-only format
line = m.reference_row("exchange", D("1332"), b_kr, m.FxRates({"KRW": D("1382.55")}), "cn")
assert "（09-18 15:30 韩国时间 收·Naver）" in line, line
manual = m.Baseline(D("1"), "k", "l", 0, int(dt.datetime(2026, 9, 17, 16, 0, tzinfo=tz8).timestamp() * 1000), "HKD")
assert "（09-17 16:00 收）→ ⚪ 无 HKD 汇率" in m.reference_row("exchange", D("1"), manual, m.FxRates(), "cn")
# Binance daily label spells out the UTC day boundary
now_ms = int(dt.datetime(2026, 9, 18, 17, 39, tzinfo=tz8).timestamp() * 1000)
boundary = now_ms // m.DAY_MS * m.DAY_MS
d = m.daily_baseline([[boundary - m.DAY_MS, "1", "2", "0.5", "76.53", "0", boundary - 1]], [], now_ms) if False else \
    m.daily_baseline([[boundary - m.DAY_MS, "1", "2", "0.5", "76.53", "0", boundary - 1]], now_ms)
assert d.label == "币安日 K 昨收｜2026-09-17（UTC 日）｜收盘 09-18 08:00（北京时间，即 UTC 00:00）", d.label
# HK bar is final only after the closing auction (16:10 + 15 min)
bars = [(dt.date(2026, 9, 17), D("38.1")), (dt.date(2026, 9, 18), D("37.76"))]
def ms(h, mi): return int(dt.datetime(2026, 9, 18, h, mi, tzinfo=tz8).timestamp() * 1000)
assert m.last_completed_bar(bars, hk, ms(16, 20))[:2] == (dt.date(2026, 9, 17), D("38.1"))
assert m.last_completed_bar(bars, hk, ms(16, 26))[:2] == (dt.date(2026, 9, 18), D("37.76"))
# persisted close_note survives a restart
cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "SKHYNIXUSDT"})
store = m.Store(":memory:"); sm = m.StockMarket(cfg, store); sm.remember("SKHYNIXUSDT", b_kr)
assert m.StockMarket(cfg, store).closes["SKHYNIXUSDT"].close_text == b_kr.close_text
print("CLOSELABEL_OK")
