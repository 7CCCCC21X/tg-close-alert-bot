"""Stale or undated quotes never price new odds: parsers keep the market's own time (0 = unknown, never "now"), a
fallback is chosen by freshness rather than by "it parsed", dated closes never step back, and the background refresh
health says skipped / ok / partial / failed truthfully."""
import asyncio, sys, json, time, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
KST = dt.timezone(dt.timedelta(hours=9))


def kr(mo, d, h, mi, s=0): return int(dt.datetime(2026, mo, d, h, mi, s, tzinfo=KST).timestamp() * 1000)
def bj(mo, d, h, mi, s=0): return int(dt.datetime(2026, mo, d, h, mi, s, tzinfo=m.BEIJING).timestamp() * 1000)


def naver(close, prev_change, at=None):
    item = {"closePrice": close, "compareToPreviousClosePrice": prev_change, "compareToPreviousPrice": {"code": "2"},
            "fluctuationsRatio": "1.0", "marketStatus": "OPEN"}
    if at:
        item["localTradedAt"] = at
    return json.dumps({"datas": [item]}).encode()


def em(last, prev, at_ms=None):
    data = {"f43": last, "f60": prev, "f57": "000001", "f58": "x"}
    if at_ms:
        data["f86"] = at_ms // 1000
    return json.dumps({"data": data}).encode()


# --- parsers keep the feed's own time; an answer without one is "unknown", never "now" -----------------------------
NOW = kr(9, 29, 15, 20)
q = m.parse_naver_index(naver("7,150.00", "70.00"), NOW)
assert q.quoted_ms == 0 and q.fetched_ms == NOW, q
assert m.parse_naver_index(naver("7,150.00", "70.00", "2026-09-29T15:19:30+09:00"), NOW).quoted_ms == kr(9, 29, 15, 19, 30)
assert m.parse_eastmoney_index(em(7150, 7080), NOW, "KOSPI").quoted_ms == 0
assert m.IndexFutures.parse_futures("东方财富", em(24600, 24500), NOW).quoted_ms == 0
tx = ('v_hkHSI="100~恒生指数~HSI~24600.12~24510.09~' + "~".join(["0"] * 25) + '~2026/09/30 10:05:03~x";').encode("gbk")
s = m.IndexFutures.parse_spot_quote("腾讯", tx, NOW)
assert (s.last, s.prev_close, s.quoted_ms) == (D("24600.12"), D("24510.09"), bj(9, 30, 10, 5, 3)), s
assert m.IndexFutures.parse_spot("腾讯", tx) == (D("24600.12"), D("24510.09"))  # the two-value form stays
assert m.local_ms("20260930100503") == bj(9, 30, 10, 5, 3) and m.local_ms("0") == 0 and m.local_ms("") == 0
# the shared rule
assert m.quote_problem(0, NOW, True, 0) == "报价时间未知"
assert m.quote_problem(NOW - 9 * 60_000, NOW, True, 0) == "" and "已超 10 分钟未更新" in m.quote_problem(NOW - 11 * 60_000, NOW, True, 0)
assert "早于最近一个交易时段" in m.quote_problem(NOW - 3 * 3600_000, NOW, False, NOW - 60_000)
assert m.quote_problem(NOW - 50 * 60_000, NOW, False, NOW - 60_000) == ""  # a thin final hour of the last session is fine
lunch = (dt.time(11, 30), dt.time(13, 0))
assert m.quote_problem(bj(9, 30, 11, 29), bj(9, 30, 13, 5), True, 0, lunch) == ""  # the lunch break does not age a quote


class FM:
    def __init__(self, now): self.now = now
    def now_ms(self): return self.now


def make_bot(now, **env):
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "SKHYNIXUSDT", "PROB_VOL": "KOSPI=2,HSI=1", **env})
    return m.Bot(cfg, m.Store(":memory:"), FM(now), None)


async def run():
    # --- KOSPI: Naver froze at 09:30 with KOSPI +1%; at 15:20 the shrinking σ used to turn that into ~100% ----------
    bot = make_bot(NOW)
    bot.kospi.quote = m.IndexQuote("KOSPI", D("7150"), D("7080"), None, None, None, kr(9, 29, 9, 30), "Naver")
    o = bot.kospi_odds(NOW)
    assert o == "KOSPI 实时报价停在 09-29 08:30，已超 10 分钟未更新，暂不输出新概率", o
    bot.kospi.quote = m.dataclasses.replace(bot.kospi.quote, quoted_ms=kr(9, 29, 15, 19))
    assert bot.kospi_odds(NOW).fair_up > 0.99  # the same reading, fresh, is a near-certain close up
    bot.kospi.quote = m.dataclasses.replace(bot.kospi.quote, quoted_ms=kr(9, 28, 15, 30))  # yesterday's page, mid-session
    assert "停在 09-28 14:30" in bot.kospi_odds(NOW)
    bot.kospi.quote = m.dataclasses.replace(bot.kospi.quote, quoted_ms=0)
    assert bot.kospi_odds(NOW) == "KOSPI 实时报价时间未知，暂不输出新概率"
    assert bot.kospi_odds(kr(9, 29, 17, 0)).startswith("KOSPI 报价时间未知")  # after hours the date cannot be told either

    # --- fallbacks: the first source answering stale does not end the search ---------------------------------------
    calls = []
    answers = {}
    async def fake_get(url, timeout=15, headers=None):
        calls.append(url)
        for key, body in answers.items():
            if key in url:
                if isinstance(body, Exception):
                    raise body
                return body
        raise m.RemoteError("offline")
    m.http_get = fake_get
    m.SOURCE_HEALTH.hosts.clear()
    k = m.KospiIndex(True)
    answers = {"index/KOSPI": naver("7,150.00", "70.00", "2026-09-29T09:30:00+09:00"),
               "100.KS11": em(7101.5, 7080, kr(9, 29, 15, 19)), "index/KPI200": naver("1,100.00", "5.00", "2026-09-29T15:19:00+09:00")}
    outcome = await k.refresh(NOW)
    assert k.quote.source == "东方财富" and k.quote.last == D("7101.5") and not k.error, (k.quote, k.error)
    assert any("naver" in u for u in calls) and any("100.KS11" in u for u in calls)
    assert isinstance(outcome, m.Refreshed), outcome
    # every source stale: the newest quote is kept for display, the reason is the error, and the round is not "ok"
    answers["100.KS11"] = em(7101.5, 7080, kr(9, 29, 9, 20))
    k.refreshed = -1e9
    outcome = await k.refresh(NOW)
    assert k.quote.quoted_ms == kr(9, 29, 15, 19), k.quote  # the earlier fresh one is newer than both stale answers
    assert "Naver: 报价停在 09-29 08:30" in k.error and "东方财富: 报价停在 09-29 08:20" in k.error, k.error
    assert outcome.status == "partial" and "KOSPI：" in outcome.error, outcome
    # an undated answer is not taken as current either: the next source is asked
    answers = {"index/KOSPI": naver("7,150.00", "70.00"), "100.KS11": em(7102, 7080, kr(9, 29, 15, 19, 50)),
               "index/KPI200": naver("1,100.00", "5.00", "2026-09-29T15:19:00+09:00")}
    k.refreshed = -1e9
    await k.refresh(NOW)
    assert k.quote.last == D("7102") and not k.error, (k.quote, k.error)

    # --- KOSPI daily closes: a lagging answer never drops a confirmed close; Yahoo outranks Naver ---------------------
    def yahoo(bars):
        stamps = [int(dt.datetime(d.year, d.month, d.day, 9, tzinfo=KST).timestamp()) for d, _ in bars]
        return json.dumps({"chart": {"result": [{"meta": {"gmtoffset": 32400}, "timestamp": stamps,
                                                 "indicators": {"quote": [{"open": [c for _, c in bars], "close": [c for _, c in bars]}]}}]}}).encode()
    chart = ('<item data="20260925|0|0|0|7080.92|1" /><item data="20260928|0|0|0|6910.89|1" />'
             '<item data="20260929|0|0|0|6830.00|1" />').encode()
    d = m.KospiIndex(True)
    answers = {"finance.yahoo.com": m.RemoteError("HTTP 503"), "fchart": chart}
    assert await d.refresh_daily(kr(9, 29, 15, 50)) == ""
    assert d.daily[dt.date(2026, 9, 29)] == D("6830.00") and d.daily_rank[dt.date(2026, 9, 29)] == 1
    answers = {"finance.yahoo.com": yahoo([(dt.date(2026, 9, 25), 7080.92), (dt.date(2026, 9, 28), 6911.5)]), "fchart": chart}
    d.daily_refreshed -= 601
    await d.refresh_daily(kr(9, 29, 16, 10))  # Yahoo lags (no 09-29 yet): its answer must not erase 09-29
    assert d.daily[dt.date(2026, 9, 29)] == D("6830.00") and d.daily[dt.date(2026, 9, 28)] == D("6911.50"), d.daily
    answers = {"finance.yahoo.com": yahoo([(dt.date(2026, 9, 28), 6911.5), (dt.date(2026, 9, 29), 6831.2)]), "fchart": chart}
    d.daily_refreshed -= 601
    await d.refresh_daily(kr(9, 29, 16, 20))
    assert d.daily[dt.date(2026, 9, 29)] == D("6831.20") and d.daily_rank[dt.date(2026, 9, 29)] == 0  # the settlement source
    answers = {"finance.yahoo.com": m.RemoteError("HTTP 503"), "fchart": chart}
    d.daily_refreshed -= 601
    await d.refresh_daily(kr(9, 29, 16, 30))
    assert d.daily[dt.date(2026, 9, 29)] == D("6831.20"), d.daily  # Naver's figure does not overwrite Yahoo's
    assert m.merge_closes({}, {}, [(dt.date(2026, 9, i), None, D(i)) for i in range(1, 30)], 0, keep=5)[0] == \
        {dt.date(2026, 9, i): D(i) for i in range(25, 30)}

    # --- exchange closes: a lagging dated answer is passed over; the stored close never steps back -------------------
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT"})
    stocks = m.StockMarket(cfg, m.Store(":memory:"))
    m.StockMarket.ATTEMPTS = 1
    lag = json.dumps({"data": {"klines": ["2026-09-23,1,488.00", "2026-09-24,1,490.97"]}}).encode()
    tencent = ('v_sh688836="1~宇树科技~688836~501.20~490.97~495~' + "~".join(["0"] * 24) + '~20260928150003~x";').encode("gbk")
    answers = {"push2his.eastmoney.com": lag, "qt.gtimg.cn": tencent}
    m.SOURCE_HEALTH.hosts.clear()
    when = bj(9, 28, 17, 0)  # 09-28's close is due (09-25 was a holiday)
    await stocks.refresh(when, force=True)
    close = stocks.closes["UNITREEUSDT"]
    assert close.value == D("501.20") and close.source.endswith("腾讯") and "UNITREEUSDT" not in stocks.errors, (close, stocks.errors)
    answers = {"push2his.eastmoney.com": lag, "qt.gtimg.cn": m.RemoteError("HTTP 502"), "hq.sinajs.cn": m.RemoteError("HTTP 502")}
    outcome = await stocks.refresh(when, force=True)  # every source lags or fails: keep 09-28, say so
    assert stocks.closes["UNITREEUSDT"] is close and "09-24" in stocks.errors["UNITREEUSDT"], stocks.errors
    assert outcome.status == "failed" and "UNITREEUSDT" in outcome.error, outcome

    # --- HSI: yesterday's cash index never prices today's session --------------------------------------------------
    hk = cfg.holidays["hk"]
    hbot = make_bot(bj(9, 30, 9, 31))
    hbot.hsi_daily.daily = {dt.date(2026, 9, 29): D("24510.09")}
    page = m.FuturesQuote("恒指期货(10/2026)日市", D("24580"), D("24520"), None, None, None, bj(9, 30, 9, 31), "etnet",
                          D("24510.09"), "etnet", D("24761.13"), session="日市", spot_ms=bj(9, 30, 9, 29))
    hbot.hsi.quote = page
    assert hbot.hsi_odds(bj(9, 30, 9, 31)) == "恒指现货停在 09-30 09:29，早于今日开盘，暂不输出新概率"
    # a restart that first sees the page after the open cannot tell its age, but its 前收 gives the day away
    hbot.hsi.quote = m.dataclasses.replace(page, spot_ms=bj(9, 30, 10, 0))
    o = hbot.hsi_odds(bj(9, 30, 10, 0))
    assert o == "恒指现货的前收 24,761.13 不是 09-29 收盘 24,510.09，不是 09-30 的行情，暂不输出新概率", o
    hbot.hsi.quote = m.dataclasses.replace(page, spot=D("24600"), spot_prev=D("24510.09"), spot_ms=bj(9, 30, 10, 0))
    assert isinstance(hbot.hsi_odds(bj(9, 30, 10, 0)), m.CloseOdds)
    assert "已超 10 分钟未更新" in hbot.hsi_odds(bj(9, 30, 10, 11)) and isinstance(hbot.hsi_odds(bj(9, 30, 13, 5)), m.CloseOdds) is False
    hbot.hsi.quote = m.dataclasses.replace(page, spot=D("24600"), spot_prev=D("24510.09"), spot_ms=bj(9, 30, 11, 58))
    assert isinstance(hbot.hsi_odds(bj(9, 30, 13, 5)), m.CloseOdds)  # the lunch break does not age it

    # a frozen etnet page ages (its values stop changing) and the next sources are asked; a timed one takes over
    LIVE = """<div><h3>恒生指數期貨(10/2026)</h3><span>日市</span><div>&#9650;24,600 +80 (+0.33%) 高水5</div>
    <div>最高: 24,650 最低: 24,500 前收市: 24,520 開市: 24,530</div></div>
    <div>恒生指數現貨 &#9650;24,595.00 +84.91 (+0.35%) 最高: 24,640 最低: 24,480 前收市: 24,510.09 開市: 24,520</div>"""
    sina = lambda at: f'var hq_str_hf_HSI="24611.0,,1,1,24700,24500,{at},24520,24530,0,0,0,2026-09-30,恒生指数期货,1";'.encode("gbk")
    feed = m.IndexFutures(True, hk)
    feed.dated_close = lambda day: D("24510.09") if day == dt.date(2026, 9, 29) else None
    answers = {"etnet": LIVE.encode(), "134.HSI_M": m.RemoteError("HTTP 502"), "hf_HSI": sina("10:12:30"),
               "100.HSI": m.RemoteError("HTTP 502"), "hkHSI": m.RemoteError("HTTP 502"), "rt_hkHSI": m.RemoteError("HTTP 502")}
    m.SOURCE_HEALTH.hosts.clear()
    assert isinstance(await feed.refresh(bj(9, 30, 10, 0), force=True), m.Refreshed)
    assert feed.quote.source == "etnet" and feed.quote.quoted_ms == bj(9, 30, 10, 0) and feed.quote.spot_ms == bj(9, 30, 10, 0)
    await feed.refresh(bj(9, 30, 10, 5), force=True)
    assert feed.quote.source == "etnet" and feed.quote.quoted_ms == bj(9, 30, 10, 0)  # unchanged values keep their first-seen time
    calls.clear()
    await feed.refresh(bj(9, 30, 10, 13), force=True)
    assert feed.quote.source == "新浪CFD" and not feed.error, (feed.quote, feed.error)  # the frozen page was passed over
    assert any("hf_HSI" in u for u in calls)
    # the CFD carries no cash index: the timed feeds are asked; when they all fail the last good one stays a while
    assert feed.quote.spot == D("24595.00") and feed.quote.spot_ms == bj(9, 30, 10, 0) and "HTTP 502" in feed.spot_error, feed.quote

    # --- background refresh health: every HSI source failing is not "✅ 0 秒前成功" -----------------------------------
    rbot = make_bot(bj(9, 30, 10, 20))
    answers = {}
    m.SOURCE_HEALTH.hosts.clear()
    rbot.reference_pool = m.ThreadPoolExecutor(max_workers=1, thread_name_prefix="reference")
    m.REFERENCE_TICK = 0.01
    runs = {"n": 0}
    real = rbot.refresh_hsi
    async def once(now_ms):
        runs["n"] += 1
        if runs["n"] >= 2:
            rbot.stopping.set()
        return await real(now_ms)
    await asyncio.wait_for(rbot.reference_loop("恒指期货", lambda: once(rbot.market.now_ms())), 5)
    state = rbot.reference_state["恒指期货"]
    assert state["status"] == "failed" and state["ok_at"] == 0 and "etnet" in state["error"], state
    text = "\n".join(rbot.diag_state(rbot.market.now_ms()))
    assert "❌ 恒指期货：从未成功" in text and "✅ 恒指期货" not in text, text
    # a partial round (futures ok, cash index missing) says so, and moves the success time
    m.Bot.note_refresh(state, m.Refreshed("partial", "恒指现货：腾讯: HTTP 502"))
    text = "\n".join(rbot.diag_state(rbot.market.now_ms()))
    assert "⚠️ 恒指期货：部分成功（0 秒前）" in text and "未取得：恒指现货：腾讯: HTTP 502" in text, text
    m.Bot.note_refresh(state, m.Refreshed("failed", "etnet: HTTP 502"))
    assert "❌ 恒指期货：上一轮失败，最近成功 0 秒前" in "\n".join(rbot.diag_state(rbot.market.now_ms()))
    rbot.reference_pool.shutdown(wait=False)
    assert m.refreshed([], 0) == m.Refreshed("ok") and m.refreshed(["a", "", "a"], 0) == m.Refreshed("failed", "a")
    assert m.refreshed(["b"], 2).status == "partial"
    print("FRESH_OK")


asyncio.run(run())
