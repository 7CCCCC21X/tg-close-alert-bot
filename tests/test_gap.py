
import asyncio, sys, time, json, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
tz8 = dt.timezone(dt.timedelta(hours=8))
def ms(y, mo, d, h, mi): return int(dt.datetime(y, mo, d, h, mi, tzinfo=tz8).timestamp() * 1000)
hk, sh = m.STOCK_MARKETS["hk"], m.STOCK_MARKETS["sh"]
# previous close from bars
bars = [(dt.date(2026, 9, 16), D("40.1")), (dt.date(2026, 9, 17), D("39.57")), (dt.date(2026, 9, 18), D("37.76"))]
assert m.last_completed_bar(bars, hk, ms(2026, 9, 18, 17, 0)) == (dt.date(2026, 9, 18), D("37.76"), D("39.57"))
assert m.last_completed_bar(bars, hk, ms(2026, 9, 18, 12, 0)) == (dt.date(2026, 9, 17), D("39.57"), D("40.1"))
assert m.last_completed_bar(bars[:1], hk, ms(2026, 9, 18, 12, 0)) == (dt.date(2026, 9, 16), D("40.1"), None)
# previous close from Tencent quote (final session) and none while running
tx = ("v_hk00625=\"100~希音~00625~37.76~39.57~38.00~" + "~".join(["0"] * 24) + "~2026/09/18 16:08:11~x\";").encode("gbk")
assert m.parse_quote_close("腾讯", "hk", tx, hk, ms(2026, 9, 18, 17, 0)) == (dt.date(2026, 9, 18), D("37.76"), D("39.57"))
assert m.parse_quote_close("腾讯", "hk", tx, hk, ms(2026, 9, 18, 15, 0)) == (None, D("39.57"), None)
# reference lines show the stock's own day move, so the contract/stock gap reads as premium
b = m.StockMarket.baseline(m.StockTicker("hk", "00625", True), hk, "腾讯", dt.date(2026, 9, 18), D("37.76"), D("39.57"))
row = m.reference_row("exchange", D("38.42"), b, m.FxRates(), "cn")
assert "→ 🔴 +1.75%｜当日 🟢 -4.57%" in row, row
kr = m.STOCK_MARKETS["kr"]
bk = m.StockMarket.baseline(m.StockTicker("kr", "000660"), kr, "Naver", dt.date(2026, 9, 18), D("1841000"), D("1745000"))
row = m.reference_row("exchange", D("1332.79"), bk, m.FxRates({"KRW": D("1382.55")}), "cn")
assert "｜当日 🔴 +5.50%" in row and "韩国时间 收·Naver）" in row, row
assert "当日" not in m.reference_row("exchange", D("1"), m.StockMarket.baseline(m.StockTicker("sh", "1"), sh, "x", None, D("2")), m.FxRates(), "cn")
# persistence keeps prev_value
cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "HK0625USDT"})
store = m.Store(":memory:"); m.StockMarket(cfg, store).remember("HK0625USDT", b)
assert m.StockMarket(cfg, store).closes["HK0625USDT"].prev_value == D("39.57")
# Binance index price flows into the quote and the price line
now = 1_800_000_000_000
q = m.Quote.parse({"symbol": "HK0625USDT", "price": "38.42", "time": now - 508_000, "markPrice": "38.42", "markTime": now - 1000, "indexPrice": "37.80"}, "HK0625USDT", now, 120)
assert q.index_price == D("37.80") and ("币安 " + m.bold("38.42") + "（标记价·508 秒无成交）｜指数 37.8｜") in q.price_row(now), q.price_row(now)
q2 = m.Quote.parse({"symbol": "X", "price": "1", "time": now, "indexPrice": "bad"}, "X", now, 120); assert q2.index_price is None
q3 = m.Quote.parse({"symbol": "X", "price": "1", "time": now}, "X", now, 120); assert "指数" not in q3.price_row(now)
async def run():
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "HK0625USDT"})
    b = m.Binance(cfg)
    async def fake_get(path, **p):
        if path == "/fapi/v2/ticker/price": return [{"symbol": "HK0625USDT", "price": "38.42", "time": 1}]
        return [{"symbol": "HK0625USDT", "markPrice": "38.42", "indexPrice": "37.80", "time": 9}]
    b.get = fake_get; rows = await b.prices(); assert rows["HK0625USDT"]["indexPrice"] == "37.80"
    print("GAP_OK")
asyncio.run(run())
