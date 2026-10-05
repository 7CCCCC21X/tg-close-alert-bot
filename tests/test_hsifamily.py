"""HSI after hours: every source family keeps its own 16:10 print, the odds use a family that has one (never mixing a
CFD print with an HKEX anchor), a missed CFD anchor comes back from Sina's 5-minute bars, the cash index is read every
3 seconds in session, and 10-19 (the day after Chung Yeung) is an HKEX holiday."""
import asyncio, sys, json, time, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D


def bj(mo, d, h, mi, s=0): return int(dt.datetime(2026, mo, d, h, mi, s, tzinfo=m.BEIJING).timestamp() * 1000)


class FM:
    def __init__(self, now): self.now, self.config = now, None
    def now_ms(self): return self.now


DAY_PAGE = ("""<div><h3>恒生指數期貨(10/2026)</h3><span>日市</span><div>&#9650;{last} +80 (+0.33%) 高水5</div>
           <div>最高: 24,650 最低: 24,500 前收市: 24,520 開市: 24,530</div></div>
           <div>恒生指數現貨 &#9650;24,595.00 +84.91 (+0.35%) 前收市: 24,510.09 開市: 24,520</div>""")
NIGHT_PAGE = ("""<div><h3>恒生指數期貨(10/2026)</h3><span>日市</span><div>&#9650;24,610 +90 (+0.37%) 高水15</div>
             <div>最高: 24,650 最低: 24,500 前收市: 24,520 開市: 24,530</div></div>
             <div><h3>恒生指數期貨(10/2026)</h3><span>夜市</span><div>&#9650;{last} +90 (+0.37%) 高水20</div>
             <div>最高: 24,750 最低: 24,600 前收市: 24,610 開市: 24,615</div></div>
             <div>恒生指數現貨 &#9650;24,595.00 +84.91 (+0.35%) 前收市: 24,510.09 開市: 24,520</div>""")


def cfd(last, when, day="2026-09-30"):
    return f'var hq_str_hf_HSI="{last},,{last},{last},24900,24400,{when},24520,24530,0,0,0,{day},恒生指数期货,1";'.encode("gbk")


def tencent_hsi(last, when):
    return ('v_hkHSI="100~恒生指数~HSI~' + last + '~24510.09~' + "~".join(["0"] * 25) + f'~{when}~x";').encode("gbk")


async def run():
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "EXCHANGE_TICKERS": "off", "KOSPI_INDEX": "off",
                             "SSE_INDEX": "off", "HL_TICKERS": "off", "HL_INDEX": "off", "PREDICT": "off", "BNB_TOUCH": "off", "SIM": "off"})
    hk = cfg.holidays["hk"]

    # --- 10-19 (Monday, the day after Chung Yeung) is an HKEX holiday: Friday 10-16's after-hours odds target 10-20 ----------
    assert dt.date(2026, 10, 19) in hk and not m.hk_trading_day(dt.date(2026, 10, 19), hk)
    assert m.session_remaining("hk", bj(10, 16, 16, 30), dt.date(2026, 10, 16), hk) == (1.5, dt.date(2026, 10, 20))
    assert m.hk_futures_session(bj(10, 19, 10, 0), hk) == "休市" and m.hk_futures_session(bj(10, 16, 22, 0), hk) == "夜市"

    # --- 16:10:03 (the close refresh): etnet AND the Sina CFD are read; each family records its own print -------------------
    feed = {"etnet": DAY_PAGE.format(last="24,600").encode(), "cfd": cfd("24603.5", "16:10:01")}
    calls = []

    async def get(url, timeout=15, headers=None):
        calls.append(url)
        if "etnet" in url:
            if isinstance(feed["etnet"], Exception): raise feed["etnet"]
            return feed["etnet"]
        if "hf_HSI" in url:
            if isinstance(feed["cfd"], Exception): raise feed["cfd"]
            return feed["cfd"]
        if "getMink" in url and "symbol=HSI" in url:
            if isinstance(feed.get("bars"), Exception) or "bars" not in feed: raise feed.get("bars") or m.RemoteError("HTTP 404: 接口请求失败")
            return feed["bars"]
        if "hkHSI" in url and "gtimg" in url and "tencent" in feed: return feed["tencent"]
        raise m.RemoteError("offline")
    m.http_get = get
    m.SOURCE_HEALTH.hosts.clear()
    store = m.Store(":memory:")
    now = bj(9, 30, 16, 10, 3)
    bot = m.Bot(cfg, store, FM(now), None)
    bot.hsi_daily.daily = {dt.date(2026, 9, 30): D("24595.00"), dt.date(2026, 9, 29): D("24510.09")}
    assert isinstance(await bot.hsi.refresh(now), m.Refreshed) and bot.hsi.quote.source == "etnet"
    assert set(bot.hsi.families) == {"HSI", "HSI:cfd"} and [u.split("/")[2] for u in calls] == ["www.etnet.com.hk", "hq.sinajs.cn"], calls
    await bot.refresh_odds_inputs(now)
    assert store.get("anchor:HSI") == [bj(9, 30, 16, 10), "24600", "16:10 现货收市时", "10/2026", now], store.get("anchor:HSI")
    assert store.get("anchor:HSI:cfd") == [bj(9, 30, 16, 10), "24603.5", "16:10 现货收市时", "", now], store.get("anchor:HSI:cfd")
    assert not [u for u in calls if "getMink" in u]  # an exact print is there: no history needed

    # --- 21:00, the night session: etnet answers -> the HKEX contract maps onto its own 16:10 print ------------------------
    now = bj(9, 30, 21, 0); bot.market.now = now
    feed["etnet"], feed["cfd"] = NIGHT_PAGE.format(last="24,846").encode(), cfd("24849.5", "20:59:58")
    await bot.hsi.refresh(now, force=True)
    o = bot.hsi_odds(now)
    assert isinstance(o, m.CloseOdds) and not o.warn and bot.hsi_used.source == "etnet", o
    assert abs(float(o.effective) - 24595 * 24846 / 24600) < 1e-6 and o.proxy_note.startswith("恒指期货 24,846 / 16:10 现货收市时 24,600"), o.proxy_note
    assert bot.odds_quote_ms("恒生指数", o) == bot.hsi_used.quoted_ms

    # --- etnet starts failing: the CFD takes over with ITS OWN 16:10 print (no "概率暂缺", no mixing of families) --------------
    feed["etnet"] = m.RemoteError("HTTP 403: 访问被拒绝")
    now = bj(9, 30, 21, 20); bot.market.now = now
    feed["cfd"] = cfd("24880", "21:19:59")
    await bot.hsi.refresh(now, force=True)
    assert bot.hsi.quote.source == "新浪CFD" and "etnet: HTTP 403" in bot.hsi.skipped, bot.hsi.skipped
    o = bot.hsi_odds(now)
    assert isinstance(o, m.CloseOdds) and not o.warn and bot.hsi_used.source == "新浪CFD", o
    assert abs(float(o.effective) - 24595 * 24880 / 24603.5) < 1e-6, o.effective  # CFD over the CFD's own anchor
    assert o.proxy_note.startswith("恒指期货·新浪CFD 24,880 / 16:10 现货收市时 24,603.5"), o.proxy_note
    assert "｜⚠️ 前序源未取到：etnet: HTTP 403" in bot.hsi.line(now, "cn"), bot.hsi.line(now, "cn")
    diag = "\n".join(bot.diag_state(now))
    assert "恒指锚点：港交所合约 24,600（16:10 现货收市时）" in diag and "新浪CFD 24,603.5（16:10 现货收市时）" in diag, diag
    assert "恒指概率：涨 " in diag and "恒指期货·新浪CFD 24,880" in diag, diag

    # --- a short outage with no CFD anchor: etnet's last quote (still current) keeps its exact anchor -------------------------
    store.delete_prefix("anchor:HSI:cfd")
    now = bj(9, 30, 21, 5); bot.market.now = now
    bot.hsi.families["HSI"] = m.dataclasses.replace(bot.hsi.families["HSI"], quoted_ms=bj(9, 30, 21, 0))
    o = bot.hsi_odds(now)
    assert isinstance(o, m.CloseOdds) and bot.hsi_used.source == "etnet" and not o.warn, (o, bot.hsi_used)
    # ... but not once it is older than the 10 minutes a live quote may be quiet
    now = bj(9, 30, 21, 11); bot.market.now = now
    feed["cfd"] = cfd("24885", "21:10:59")
    await bot.hsi.refresh(now, force=True)
    msg = bot.hsi_odds(now)
    assert isinstance(msg, str) and msg.startswith("当前只有新浪CFD报价（港交所合约：etnet: "), msg
    assert "etnet: 连续失败" in msg or "etnet: HTTP 403" in msg, msg  # etnet in cooldown after two failures: said so
    assert "缺少恒指期货在 09-30 16:10 现货收市时的价格" in msg, msg

    # --- the CFD's 16:10 price comes back from Sina's 5-minute bars (approximate: shown, no trade suggestion) ---------------
    feed["bars"] = m.RemoteError("HTTP 404: 接口请求失败")
    await bot.refresh_odds_inputs(now)
    assert bot.hsi_cfd_error.startswith("HTTP 404") and store.get("anchor:HSI:cfd") is None
    msg = bot.hsi_odds(now)
    assert msg.endswith("；新浪5分钟K也未取到（HTTP 404: 接口请求失败）"), msg
    n = len([u for u in calls if "getMink" in u])
    await bot.refresh_odds_inputs(now)
    assert len([u for u in calls if "getMink" in u]) == n  # a failed lookup waits five minutes
    feed["bars"] = ('var _HSI_5=([{"d":"2026-09-30 16:05:00","c":"24590.0"},{"d":"2026-09-30 16:10:00","c":"24601.5"},'
                    '{"d":"2026-09-30 16:15:00","c":"24620.0"}]);').encode()
    bot.anchor_tries.clear()
    await bot.refresh_odds_inputs(now)
    assert store.get("anchor:HSI:cfd") == [bj(9, 30, 16, 10), "24601.5", "16:10 五分钟K近似", "", now] and not bot.hsi_cfd_error
    o = bot.hsi_odds(now)
    assert isinstance(o, m.CloseOdds) and o.warn and bot.hsi_used.source == "新浪CFD", o
    assert abs(float(o.effective) - 24595 * 24885 / 24601.5) < 1e-6 and "/ 16:10 五分钟K近似 24,601.5" in o.proxy_note, o.proxy_note

    # --- the closer anchor wins: etnet with only its 16:30 close vs a CFD with its own exact print -------------------------------
    fresh = m.Store(":memory:")
    bot2 = m.Bot(cfg, fresh, FM(bj(9, 30, 21, 0)), None)
    bot2.hsi_daily.daily = dict(bot.hsi_daily.daily)
    night = m.FuturesQuote("恒指期货(10/2026)夜市", D("24846"), D("24610"), None, None, None, bj(9, 30, 21, 0), "etnet", D("24595"),
                           "etnet", D("24510.09"), session="夜市", fetched_ms=bj(9, 30, 21, 0))
    cfd_q = m.FuturesQuote("恒生指数期货", D("24849.5"), D("24520"), None, None, None, bj(9, 30, 20, 59), "新浪CFD",
                           exchange_contract=False, fetched_ms=bj(9, 30, 21, 0))
    bot2.hsi.quote, bot2.hsi.families = night, {"HSI": night, "HSI:cfd": cfd_q}
    o = bot2.hsi_odds(bj(9, 30, 21, 0))
    assert isinstance(o, m.CloseOdds) and o.warn and "日市 16:30 收市（近似" in o.proxy_note  # no CFD anchor: etnet's 16:30 close
    fresh.put("anchor:HSI:cfd", [bj(9, 30, 16, 10), "24603.5", "16:10 现货收市时", "", bj(9, 30, 16, 10, 3)])
    o = bot2.hsi_odds(bj(9, 30, 21, 0))
    assert isinstance(o, m.CloseOdds) and not o.warn and bot2.hsi_used is cfd_q and "恒指期货·新浪CFD" in o.proxy_note, o
    fresh.put("anchor:HSI", [bj(9, 30, 16, 10), "24600", "16:10 现货收市时", "10/2026", bj(9, 30, 16, 10, 3)])
    o = bot2.hsi_odds(bj(9, 30, 21, 0))
    assert bot2.hsi_used is night and o.proxy_note.startswith("恒指期货 24,846"), o.proxy_note  # both exact: the one in use

    # --- in session the cash index alone is read every 3 seconds, and a newer one is kept over the futures page's --------------
    m.SOURCE_HEALTH.hosts.clear(); calls.clear()
    feed["etnet"] = DAY_PAGE.format(last="24,600").encode()
    feed["tencent"] = tencent_hsi("24612.30", "2026/09/30 10:00:02")
    h = m.IndexFutures(True, hk)
    t0 = bj(9, 30, 10, 0)
    await h.refresh(t0)
    assert h.quote.spot == D("24595.00") and h.quote.spot_source == "etnet" and "hf_HSI" not in "".join(calls)  # no CFD in session
    out = await h.refresh(t0 + 3_000)
    assert isinstance(out, m.Refreshed) and h.quote.spot == D("24612.30") and h.quote.spot_source == "腾讯", h.quote
    assert h.quote.last == D("24600") and h.quote.water is None  # the futures stay; etnet's premium belonged to its own spot
    assert await h.refresh(t0 + 4_000) is False  # the next look waits its 3 seconds
    # the timed feeds failing is not a fault while the page's own cash index stands; they are asked again in 30 s, not 3
    feed.pop("tencent"); h.spot_refreshed = -1e9; n = len(calls)
    assert (await h.refresh(t0 + 6_000)).status == "ok" and len(calls) > n and h.spot_refreshed > time.monotonic() + 20
    assert await h.refresh(t0 + 9_000) is False
    feed["tencent"] = tencent_hsi("24612.30", "2026/09/30 10:00:02"); h.spot_refreshed = -1e9
    await h.refresh(t0 + 20_000, force=True)  # the futures page again, its spot unchanged since 10:00:00
    assert h.quote.spot == D("24612.30") and h.quote.spot_source == "腾讯", h.quote
    hb = m.Bot(cfg, m.Store(":memory:"), FM(t0 + 20_000), None); hb.hsi = h
    o = hb.hsi_odds(t0 + 20_000)
    assert isinstance(o, m.CloseOdds) and o.effective == D("24612.30") and hb.odds_quote_ms("恒生指数", o) == bj(9, 30, 10, 0, 2), o
    # --- /diag probes the CFD's 5-minute bars for the latest close (whether a missed anchor could be recovered) ---------------
    pcfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "EXCHANGE_TICKERS": "off", "KOSPI_INDEX": "off",
                              "SSE_INDEX": "off", "HL_TICKERS": "off", "HL_INDEX": "off", "PREDICT": "off", "BNB_TOUCH": "off", "SIM": "off"})
    class PM(FM):  # diag_probes names the Binance calls it would make
        async def prices(self): return {}
        async def server_time(self): return 0
        async def get(self, path, **p): return []
    pbot = m.Bot(pcfg, m.Store(":memory:"), PM(bj(9, 30, 21, 0)), None)
    probe = next(p for p in pbot.diag_probes(bj(9, 30, 21, 0)) if p[0] == "恒指锚点")
    assert probe[1] == "新浪CFD 5分钟K 09-30 16:10", probe[1]
    assert probe[3](feed["bars"]) == "09-30 16:10 收 24,601.5（返回 3 根：09-30 16:05～09-30 16:15）"
    try: probe[3](feed["bars"].replace(b"16:10:00", b"16:11:00")); assert False
    except ValueError as e: assert "没有 2026-09-30 16:10 这一根" in str(e), e
    print("HSIFAMILY_OK")


asyncio.run(run())
