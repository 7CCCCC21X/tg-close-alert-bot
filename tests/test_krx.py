import asyncio, sys, json, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
KST = dt.timezone(dt.timedelta(hours=9))


def kr(y, mo, d, h, mi):
    return int(dt.datetime(y, mo, d, h, mi, tzinfo=KST).timestamp() * 1000)


# Naver's daily chart blends in Nextrade after-hours trades: 09-28 "closes" at 1,761,000 (NXT, 20:00),
# while the KRX closing auction was 1,768,000 (Naver's 기준가 for 09-29).
CHART = ('<chartdata><item data="20260925|1740000|1780000|1730000|1750000|100" />'
         '<item data="20260928|1757000|1788000|1736000|1761000|100" />'
         '<item data="20260929|1768000|1790000|1760000|1769000|100" /></chartdata>').encode()


def realtime(price, change, direction, at):
    return json.dumps({"datas": [{"itemCode": "000660", "stockName": "SK하이닉스", "closePrice": price,
                                  "compareToPreviousClosePrice": change, "compareToPreviousPrice": {"code": direction},
                                  "fluctuationsRatio": "0.06", "marketStatus": "OPEN", "localTradedAt": at}]}).encode()


async def run():
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "SKHYNIXUSDT"})
    stocks = m.StockMarket(cfg)
    ticker = cfg.tickers["SKHYNIXUSDT"]
    answers = {}

    async def fake_get(url, timeout=15, headers=None):
        for key, body in answers.items():
            if key in url:
                if isinstance(body, Exception):
                    raise body
                return body
        raise AssertionError(url)
    m.http_get = fake_get

    # 09-29 15:27, KRX trading: 1,769,000 (+1,000) -> the 09-28 close is today's 기준가 1,768,000, not the chart's 1,761,000
    answers = {"fchart": CHART, "polling": realtime("1,769,000", "1,000", "2", "2026-09-29T15:27:10+09:00")}
    base = await stocks.fetch("SKHYNIXUSDT", ticker, kr(2026, 9, 29, 15, 27))
    assert base.value == D("1768000") and base.close_ms == kr(2026, 9, 28, 15, 30) and "Naver KRX" in base.source, base

    # 09-29 16:00, after the KRX close, NXT still trading: the day's close is KRX's own last price
    answers = {"fchart": CHART, "polling": realtime("1,769,000", "1,000", "2", "2026-09-29T15:30:00+09:00")}
    base = await stocks.fetch("SKHYNIXUSDT", ticker, kr(2026, 9, 29, 16, 0))
    assert base.value == D("1769000") and base.close_ms == kr(2026, 9, 29, 15, 30) and base.prev_value == D("1768000"), base

    # a falling day: change is negative (code 5 = 하락)
    answers = {"fchart": CHART, "polling": realtime("1,760,000", "8,000", "5", "2026-09-29T15:30:00+09:00")}
    base = await stocks.fetch("SKHYNIXUSDT", ticker, kr(2026, 9, 29, 16, 0))
    assert base.value == D("1760000") and base.prev_value == D("1768000"), base

    # the realtime quote fails: the chart's value stands (and still says where it came from)
    answers = {"fchart": CHART, "polling": m.RemoteError("网络错误 (URLError)")}
    base = await stocks.fetch("SKHYNIXUSDT", ticker, kr(2026, 9, 29, 15, 27))
    assert base.value == D("1761000") and base.source.endswith("·Naver"), base

    # --- KOSPI: the dated daily-chart close wins over a realtime figure frozen before the closing auction -------------
    class FakeMarket:
        def now_ms(self): return kr(2026, 9, 29, 10, 0)
    bot = m.Bot(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT"}), m.Store(":memory:"), FakeMarket(), None)
    kospi_chart = ('<item data="20260925|0|0|0|7080.92|1" /><item data="20260928|0|0|0|6910.89|1" />'
                   '<item data="20260929|0|0|0|6830.00|1" />').encode()
    answers = {"fchart": kospi_chart}
    await bot.kospi.refresh_daily(kr(2026, 9, 29, 10, 0))
    assert bot.kospi.daily == {dt.date(2026, 9, 25): D("7080.92"), dt.date(2026, 9, 28): D("6910.89")}, bot.kospi.daily  # 09-29 not final
    ref, note = bot.kospi_ref(dt.date(2026, 9, 28), D("6889.74"))
    assert ref == D("6910.89") and note == "09-28 收盘（日K；实时行情为 6,889.74）", note
    assert bot.kospi_ref(dt.date(2026, 9, 28), D("6910.89")) == (D("6910.89"), "09-28 收盘")      # they agree: plain label
    assert bot.kospi_ref(dt.date(2026, 9, 29), D("6824.90")) == (D("6824.90"), "09-29 收盘")      # not in the chart yet
    # the day's bar is looked for every minute after the close, every 10 minutes otherwise
    n = bot.kospi.daily_refreshed
    await bot.kospi.refresh_daily(kr(2026, 9, 29, 10, 5)); assert bot.kospi.daily_refreshed == n
    bot.kospi.daily_refreshed -= 61
    await bot.kospi.refresh_daily(kr(2026, 9, 29, 15, 50))
    assert bot.kospi.daily[dt.date(2026, 9, 29)] == D("6830.00")
    # during the session the KOSPI card measures from the dated close
    k = m.IndexQuote("KOSPI", D("6824.90"), D("6889.74"), None, None, None, kr(2026, 9, 29, 10, 0), "Naver")
    bot.kospi.quote = k
    odds = bot.kospi_odds(kr(2026, 9, 29, 10, 0))
    assert odds.ref == D("6910.89") and "实时行情为 6,889.74" in odds.ref_note and odds.effective == D("6824.90"), odds

asyncio.run(run())
print("KRX_OK")
