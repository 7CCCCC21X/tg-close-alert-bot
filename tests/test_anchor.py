"""Close anchors: HL KR200 takes exactly the candle ending at the KOSPI close (no neighbouring minute), and the HSI
futures price at the 16:10 cash close is caught as it happens, persisted, and used for every later futures move."""
import asyncio, sys, json, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
KST = dt.timezone(dt.timedelta(hours=9))


def kr(mo, d, h, mi): return int(dt.datetime(2026, mo, d, h, mi, tzinfo=KST).timestamp() * 1000)
def bj(mo, d, h, mi, s=0): return int(dt.datetime(2026, mo, d, h, mi, s, tzinfo=m.BEIJING).timestamp() * 1000)


class FM:
    def __init__(self, now): self.now = now
    def now_ms(self): return self.now


async def run():
    # --- KR200: asked for 15:30 KST (14:30 Beijing), HL only has the minute that opened 15:26 ---------------------
    close = kr(9, 29, 15, 30)
    asked = []
    candles = {"1m": [{"t": close - 4 * 60_000, "T": close - 3 * 60_000 - 1, "c": "1100.5"}],
               "5m": [{"t": close - 5 * 60_000, "T": close - 1, "c": "1101.25"}]}
    async def hl(url, payload=None, timeout=15):
        asked.append(payload["req"]["interval"])
        return candles[payload["req"]["interval"]]
    m.http_json = hl
    feed = m.Hyperliquid({"KR200": ("xyz", "KR200")})
    try:
        await feed.price_at("xyz:KR200", close, "1m"); assert False, "15:26's close is not the price at 15:30"
    except ValueError as error:
        assert "14:29–14:30 的 1m K 线" in str(error) and "只有 14:26 开始的" in str(error), error
    assert await feed.price_at("xyz:KR200", close, "5m") == D("1101.25")
    # a candle whose close time does not end at the close is refused too
    candles["1m"] = [{"t": close - 60_000, "T": close + 59_999, "c": "1"}]
    try:
        await feed.price_at("xyz:KR200", close, "1m"); assert False
    except ValueError:
        pass
    # the bot's anchor lookup therefore lands on the 5-minute candle, labelled as such
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "SKHYNIXUSDT"})
    bot = m.Bot(cfg, m.Store(":memory:"), FM(kr(9, 29, 17, 0)), None)
    k = m.IndexQuote("KOSPI", D("6830"), D("6910.89"), None, None, None, kr(9, 29, 15, 30), "Naver")
    h = m.HlQuote("xyz:KR200", D("1102"), None, D("1102.5"), None, None, kr(9, 29, 16, 59))
    feed.at_cache.clear(); bot.hl = feed; asked.clear()
    candles["1m"] = [{"t": close - 4 * 60_000, "c": "1100.5"}]
    await bot.kospi_anchor(k, h)
    assert asked == ["1m", "5m"] and bot.anchors["KOSPI"] == (close, D("1101.25")) and bot.kospi_anchor_note == "15:30 五分钟K", (asked, bot.anchors)

    # --- HSI: the refresh right after 16:10 is not left to the 60-second cadence ------------------------------------
    hk = cfg.holidays["hk"]
    hsi = m.IndexFutures(True, hk)
    hsi.refreshed, hsi.refreshed_ms = 1e18, bj(9, 30, 16, 9, 40)  # "just refreshed" by the monotonic clock
    assert hsi.cash_close_due(bj(9, 30, 16, 10, 2)) and not hsi.cash_close_due(bj(9, 30, 16, 9, 59))
    hsi.refreshed_ms = bj(9, 30, 16, 10, 2)
    assert not hsi.cash_close_due(bj(9, 30, 16, 10, 30))                   # once per close
    assert not hsi.cash_close_due(bj(10, 1, 16, 10, 2))                    # 10-01 is a HK holiday
    page = ("""<div><h3>恒生指數期貨(10/2026)</h3><span>日市</span><div>&#9650;24,600 +80 (+0.33%) 高水5</div>
            <div>最高: 24,650 最低: 24,500 前收市: 24,520 開市: 24,530</div></div>
            <div>恒生指數現貨 &#9650;24,595.00 +84.91 (+0.35%) 前收市: 24,510.09 開市: 24,520</div>""")
    async def etnet(url, timeout=15, headers=None):
        if "etnet" in url: return page.encode()
        raise m.RemoteError("offline")
    m.http_get = etnet
    m.SOURCE_HEALTH.hosts.clear()
    hsi.refreshed_ms = bj(9, 30, 16, 9, 40)  # last refresh before the close; the monotonic cadence says "not due"
    outcome = await hsi.refresh(bj(9, 30, 16, 10, 3))
    assert isinstance(outcome, m.Refreshed) and hsi.quote.fetched_ms == bj(9, 30, 16, 10, 3)

    # the bot records that print as the 16:10 anchor (first print only, persisted), and a restart reads it back
    store = m.Store(":memory:")
    bot = m.Bot(cfg, store, FM(bj(9, 30, 16, 10, 3)), None)
    bot.hsi.quote = hsi.quote
    bot.note_hsi_close_print(hsi.quote)
    assert store.get("anchor:HSI") == [bj(9, 30, 16, 10), "24600", "16:10 现货收市时", "10/2026", bj(9, 30, 16, 10, 3)]
    late = m.dataclasses.replace(hsi.quote, last=D("24700"), fetched_ms=bj(9, 30, 16, 12), quoted_ms=bj(9, 30, 16, 12))
    bot.note_hsi_close_print(late)
    assert store.get("anchor:HSI")[1] == "24600"
    fresh = m.Store(":memory:"); fresh_bot = m.Bot(cfg, fresh, FM(0), None)
    fresh_bot.note_hsi_close_print(late)  # a bot that missed 16:10 records its first print, labelled approximate
    assert fresh.get("anchor:HSI")[2] == "16:10 后 2 分钟首笔近似", fresh.get("anchor:HSI")
    fresh_bot.note_hsi_close_print(m.dataclasses.replace(late, fetched_ms=bj(9, 30, 16, 16), quoted_ms=bj(9, 30, 16, 16)))
    assert fresh.get("anchor:HSI")[1] == "24700"
    nothing = m.Store(":memory:"); m.Bot(cfg, nothing, FM(0), None).note_hsi_close_print(
        m.dataclasses.replace(late, fetched_ms=bj(9, 30, 16, 16), quoted_ms=bj(9, 30, 16, 16)))
    assert nothing.get("anchor:HSI") is None  # too long after the close to stand for it
    restart = m.Bot(cfg, store, FM(bj(9, 30, 21, 0)), None)
    restart.hsi.quote = m.FuturesQuote("恒指期货(10/2026)夜市", D("24846"), D("24650"), None, None, None, bj(9, 30, 21, 0),
                                       "etnet", D("24595"), "etnet", D("24510.09"), session="夜市", fetched_ms=bj(9, 30, 21, 0))
    restart.hsi_daily.daily = {dt.date(2026, 9, 30): D("24595.00")}
    o = restart.hsi_odds(bj(9, 30, 21, 0))
    assert isinstance(o, m.CloseOdds) and abs(float(o.effective) - 24595 * 1.01) < 1e-6 and not o.warn, o
    assert "/ 16:10 现货收市时 24,600 → +1.000%" in o.proxy_note, o.proxy_note
    # the CFD keeps its own family: an HKEX-contract anchor never maps a CFD print
    cfd = m.FuturesQuote("恒生指数期货", D("24900"), None, None, None, None, bj(9, 30, 21, 0), "新浪CFD", D("24595"),
                         exchange_contract=False, fetched_ms=bj(9, 30, 21, 0))
    restart.hsi.quote = cfd
    assert "缺少恒指期货在 09-30 16:10 现货收市时的价格" in restart.hsi_odds(bj(9, 30, 21, 0))
    # approximate anchors: shown, labelled, and never turned into a trade suggestion
    restart.store.delete_prefix("anchor:HSI")
    restart.hsi.quote = m.dataclasses.replace(cfd, source="etnet", exchange_contract=True,
                                              name="恒指期货(10/2026)夜市", prev_settle=D("24650"), session="夜市")
    o = restart.hsi_odds(bj(9, 30, 21, 0))
    assert isinstance(o, m.CloseOdds) and o.warn and "近似" in o.proxy_note, o
    restart.predict.slugs["HSI"] = "hang-seng-index-up-or-down-on-october-2-2026"
    restart.predict.books["HSI"] = m.PredictBook("HSI", "s", "1", "t", ((D("0.10"), D("50")),), ((D("0.12"), D("50")),), bj(9, 30, 21, 0))
    payload = restart.predict_payload("恒生指数", o, bj(9, 30, 21, 0))
    assert payload["edges"] and not any(e["best"] for e in payload["edges"]), payload
    print("ANCHOR_OK")


asyncio.run(run())
