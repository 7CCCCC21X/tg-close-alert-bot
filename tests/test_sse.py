import asyncio, sys, time, math, json, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
def bj(y, mo, d, h, mi): return int(dt.datetime(2026, mo, d, h, mi, tzinfo=m.BEIJING).timestamp() * 1000)

# --- config: holidays, beta ---------------------------------------------------------------
c = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x"})
cn = c.holidays["sh"]
assert dt.date(2026, 9, 25) in cn and all(dt.date(2026, 10, d) in cn for d in range(1, 8)) and dt.date(2026, 10, 8) not in cn
assert c.holidays["sz"] is cn and dt.date(2026, 10, 9) in c.holidays["kr"] and c.sse_index and c.a50_beta == 0.8
c2 = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "HOLIDAYS_CN": "2026-12-31", "A50_BETA": "0.75", "SSE_INDEX": "off"})
assert c2.holidays["sh"] == frozenset({dt.date(2026, 12, 31)}) and c2.a50_beta == 0.75 and not c2.sse_index
for bad in [{"HOLIDAYS_CN": "2026/10/01"}, {"A50_BETA": "x"}, {"A50_BETA": "5"}]:
    try: m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", **bad}); assert False, bad
    except ValueError: pass

# --- holidays in session_remaining ----------------------------------------------------------
frac, target = m.session_remaining("sh", bj(2026, 9, 30, 15, 20), dt.date(2026, 9, 30), cn)
assert target == dt.date(2026, 10, 8) and frac == 1 + 0.5 * 5, (frac, target)          # 5 weekday holidays skipped
frac, target = m.session_remaining("sh", bj(2026, 9, 25, 10, 0), dt.date(2026, 9, 24), cn)
assert target == dt.date(2026, 9, 28) and frac == 1.5, (frac, target)                   # today (9/25) is itself a holiday
frac, target = m.session_remaining("sh", bj(2026, 9, 28, 10, 30), dt.date(2026, 9, 24), cn)
assert target == dt.date(2026, 9, 28) and abs(frac - 180 / 240) < 1e-9                  # 60 min morning + 120 min afternoon left

# --- parsers ------------------------------------------------------------------------------------
tx = ('v_sh000001="1~上证指数~000001~3850.12~3830.00~3835.00~' + "~".join(["0"] * 24) + '~20260930150003~x";').encode("gbk")
q = m.parse_cn_index("腾讯", tx, 0)
assert q.last == D("3850.12") and q.prev_close == D("3830.00") and q.open == D("3835.00") and q.quoted_ms == bj(2026, 9, 30, 15, 0), q
sina = ('var hq_str_sh000001="上证指数,3835.00,3830.00,3850.12,3860,3820,' + ",".join(["0"] * 24) + ',2026-09-30,15:00:03,00";').encode("gbk")
qs = m.parse_cn_index("新浪", sina, 0); assert qs.last == D("3850.12") and qs.prev_close == D("3830.00") and qs.quoted_ms == bj(2026, 9, 30, 15, 0) + 3000
qe = m.parse_cn_index("东方财富", json.dumps({"data": {"f43": 3850.12, "f57": "000001", "f60": 3830.0, "f86": bj(2026, 9, 30, 15, 0) // 1000}}).encode(), 0)
assert qe.last == D("3850.12") and qe.source == "东方财富"
# the answer must be the Composite itself (sh000001), not another index that shares the digits
for source, raw in [("腾讯", tx.replace(b"v_sh000001", b"v_sz000001")), ("腾讯", tx.replace("~000001~".encode(), b"~000002~")),
                    ("新浪", sina.replace(b"hq_str_sh000001", b"hq_str_sh000300")),
                    ("东方财富", json.dumps({"data": {"f43": 1, "f57": "000300"}}).encode()),
                    ("东方财富", json.dumps({"data": {"f43": 1}}).encode())]:
    try: m.parse_cn_index(source, raw, 0); assert False, (source, raw[:40])
    except ValueError as e: assert "不是上证指数" in str(e), e
# dated daily bars (Tencent / Eastmoney), code checked
tday = json.dumps({"data": {"sh000001": {"day": [["2026-09-23", "3860", "3870.10", "0", "0", "0"], ["2026-09-24", "3871", "3888.37", "0", "0", "0"]]}}}).encode()
assert m.parse_cn_daily("腾讯日K", tday) == [(dt.date(2026, 9, 23), D("3870.10")), (dt.date(2026, 9, 24), D("3888.37"))]
eday = json.dumps({"data": {"code": "000001", "market": 1, "klines": ["2026-09-24,3871,3888.37", "2026-09-23,3860,3870.10"]}}).encode()
assert m.parse_cn_daily("东方财富日K", eday)[-1] == (dt.date(2026, 9, 24), D("3888.37"))
for source, raw in [("腾讯日K", json.dumps({"data": {"sz399001": {"day": []}}}).encode()),
                    ("东方财富日K", json.dumps({"data": {"code": "000001", "market": 0, "klines": ["2026-09-24,1,2"]}}).encode()),
                    ("腾讯日K", json.dumps({"data": {"sh000001": {"day": []}}}).encode()), ("腾讯日K", b"<html>")]:
    try: m.parse_cn_daily(source, raw); assert False, raw
    except ValueError: pass
# the A50 proxy answer must be the A50 contract
for source, raw in [("东方财富", json.dumps({"data": {"f43": 1, "f57": "HSI00Y"}}).encode()),
                    ("新浪CFD", 'var hq_str_hf_HSI="1,,1,1,1,1,22:01:05,1,1,0,0,0,0,恒指,2026-09-30";'.encode("gbk"))]:
    try: m.CnIndex.parse_a50(source, raw, 0); assert False
    except ValueError as e: assert "不是 A50" in str(e), e
for source, raw in [("东方财富", json.dumps({"data": {"f43": 14120, "f57": "CN00Y"}}).encode()),
                    ("新浪CFD", 'var hq_str_hf_CHA50CFD="14118.5,,1,1,14200,14000,??,14100,14150,0,0,0,0,富时A50,2026-09-30";'.encode("gbk"))]:
    try: m.CnIndex.parse_a50(source, raw, 0); assert False
    except ValueError as e: assert "时间" in str(e), e
# A50 sessions follow the SGX week: Friday's night runs to Saturday 05:15, then closed until Monday 09:00
assert m.a50_session(bj(2026, 9, 26, 5, 6)) == "夜盘" and m.a50_session(bj(2026, 9, 26, 5, 15)) == "休市"
assert m.a50_session(bj(2026, 9, 27, 21, 0)) == "休市" and m.a50_session(bj(2026, 9, 28, 2, 0)) == "休市"
assert m.a50_session(bj(2026, 9, 28, 8, 59)) == "休市" and m.a50_session(bj(2026, 9, 28, 9, 30)) == "日盘"
assert m.a50_last_session_end(bj(2026, 9, 26, 16, 0)) == bj(2026, 9, 26, 5, 15)
assert m.a50_last_session_end(bj(2026, 9, 28, 8, 30)) == bj(2026, 9, 26, 5, 15)
assert m.a50_last_session_end(bj(2026, 9, 28, 16, 40)) == bj(2026, 9, 28, 16, 30)
# the calendar's latest expected close honours holidays: 09-25 is shut, so 09-24 until 09-28's close is final
cnh = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x"}).holidays["sh"]
for when, want in [(bj(2026, 9, 25, 16, 0), 24), (bj(2026, 9, 27, 12, 0), 24), (bj(2026, 9, 28, 15, 10), 24), (bj(2026, 9, 28, 15, 20), 28)]:
    assert m.expected_close_date("sh", when, cnh) == dt.date(2026, 9, want), (when, want)
a = m.CnIndex.parse_a50("东方财富", json.dumps({"data": {"f43": 14120, "f60": 14100, "f86": bj(2026, 9, 30, 22, 1) // 1000, "f58": "A50期指当月连续"}}).encode(), 0)
assert a.last == D("14120") and a.prev_close == D("14100") and a.name == "A50期货"
acfd = m.CnIndex.parse_a50("新浪CFD", 'var hq_str_hf_CHA50CFD="14118.5,,1,1,14200,14000,22:01:05,14100,14150,0,0,0,0,富时A50,2026-09-30";'.encode("gbk"), 0)
assert acfd.last == D("14118.5") and acfd.quoted_ms == bj(2026, 9, 30, 22, 1) + 5000
assert m.a50_session(bj(2026, 9, 30, 22, 0)) == "夜盘" and m.a50_session(bj(2026, 10, 1, 5, 14)) == "夜盘" and m.a50_session(bj(2026, 9, 30, 10, 0)) == "日盘" and m.a50_session(bj(2026, 9, 30, 16, 45)) == "夜盘"

async def run():
    minutes = json.dumps({"data": {"code": "CN00Y", "klines": ["2026-09-30 14:59,14150,14155,1,1", "2026-09-30 15:00,14155,14160,1,1", "2026-09-30 15:01,14160,14158,1,1"]}}).encode()
    calls = []
    days, d = [], dt.date(2026, 9, 30)
    while len(days) < 40:
        if d.weekday() < 5 and d not in cn: days.append(d)
        d -= dt.timedelta(days=1)
    daily_rows = [[str(day), "0", str(3800 + (i % 2) * 38), "0", "0", "0"] for i, day in enumerate(reversed(days))]
    daily_rows[-1][2] = "3850.12"
    async def fake_get(url, timeout=15, headers=None):
        calls.append(url)
        if "q=sh000001" in url: return tx
        if "104.CN00Y" in url and "klt=1" in url: return minutes
        if "104.CN00Y" in url: return json.dumps({"data": {"f43": 14020, "f60": 14100, "f86": bj(2026, 9, 30, 22, 1) // 1000}}).encode()
        if "fqkline" in url: return json.dumps({"data": {"sh000001": {"day": daily_rows}}}).encode()
        raise m.RemoteError("skip")
    async def no_json(*a, **k): raise m.RemoteError("skip")
    m.http_get = fake_get; m.http_json = no_json
    x = m.CnIndex(); await x.a50_at(dt.date(2026, 9, 30)) == D("14160")
    assert await x.a50_at(dt.date(2026, 9, 30)) == D("14160") and "beg=20260929&end=20261001" in calls[-1]
    try: await x.a50_at(dt.date(2026, 9, 29)); assert False
    except ValueError as e: assert "15:00" in str(e)
    class FakeTelegram:
        def __init__(self): self.sent = []
        async def call(self, *a, **k): return True
        async def send(self, chat, thread, text, reply_markup=None, parse_mode=None): self.sent.append(text)
    class FakeMarket(m.Binance):
        def __init__(self, c): super().__init__(c)
        def now_ms(self): return bj(2026, 9, 30, 22, 5)
        async def sync_clock(self): pass
        async def prices(self): return {"UNITREEUSDT": {"symbol": "UNITREEUSDT", "price": "73", "time": self.now_ms()}}
        async def get(self, path, **p): return []
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ADMIN_USER_ID": "42", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off",
                             "HL_TICKERS": "off", "HL_INDEX": "off", "FX_RATES": "CNY=7", "BASELINE_MODE": "manual"})
    store = m.Store(":memory:"); tg = FakeTelegram(); bot = m.Bot(cfg, store, FakeMarket(cfg), tg)
    await bot.one_cycle()
    close_ms = bj(2026, 9, 30, 15, 0)
    assert bot.anchors["A50"] == (close_ms, D("14160")) and store.get("anchor:A50") == [close_ms, "14160", "15:00", "东方财富"], bot.anchors
    assert bot.vols.estimates["SSE"][1] == 39 and bot.vols.estimates["SSE"][2] == "上证日K"
    o = bot.sse_odds(bot.market.now_ms())
    move = math.log(14020 / 14160)
    assert isinstance(o, m.CloseOdds) and o.target == dt.date(2026, 10, 8) and o.remaining == 3.5, o
    assert abs(float(o.effective) - 3850.12 * math.exp(0.8 * move)) < 1e-6 and o.fair_up < 0.5, o
    assert "A50 14,020 / 15:00 14,160 → -0.989% × β 0.8" in o.proxy_note
    # restart: anchor restored from the store without refetching
    bot2 = m.Bot(cfg, store, FakeMarket(cfg), tg); bot2.cn.quote = bot.cn.quote; bot2.cn.a50 = bot.cn.a50; bot2.cn.close = bot.cn.close
    n = len(calls); await bot2.refresh_odds_inputs(bot2.market.now_ms())
    assert bot2.anchors["A50"] == (close_ms, D("14160")) and not [u for u in calls[n:] if "CN00Y&klt=1&" in u]
    # /status lines
    tg.sent.clear(); await bot.process_message({"text": "/status", "chat": {"id": 1}, "from": {"id": 42}, "date": time.time()})
    st = tg.sent[-1]
    assert "🇨🇳 <b>上证 已收盘</b> <b>3,850.12</b> → 昨收 <b>3,830</b> 🔴 +0.53%（+20.12）｜09-30 15:00 腾讯｜收盘 09-30 <b>3,850.12</b>（腾讯日K确认）" in st, st
    assert "📈 <b>A50期货 夜盘</b> <b>14,020</b> → 上证收盘时 <b>14,160</b> 🟢 -0.99%｜昨结 14,100 🟢 -0.57%｜09-30 22:01 东方财富" in st, st
    assert "🎲 上证 10-08收 涨 <b>" in st and st.index("🇨🇳") < st.index("📍"), st
    # /prob and the Shanghai contract's alert
    tg.sent.clear(); await bot.process_message({"text": "/prob", "chat": {"id": 1}, "from": {"id": 42}, "date": time.time()})
    pr = tg.sent[-1]; assert "<b>📍 上证指数</b>" in pr and "→ 目标 10-08 收盘" in pr and "× √3.500" in pr and "上证日K 39 日" in pr, pr
    # during the session: live index vs the dated close (09-30 is the session before 10-08)
    bot.cn.quote = m.IndexQuote("上证指数", D("3860"), D("3850.12"), None, None, None, bj(2026, 10, 8, 10, 0), "腾讯")
    o = bot.sse_odds(bj(2026, 10, 8, 10, 0)); assert o.ref == D("3850.12") and o.effective == D("3860") and o.target == dt.date(2026, 10, 8) and o.remaining < 1, o
    assert o.ref_note == "09-30 收盘", o.ref_note
    # a live quote that stopped updating gives no new probability; the lunch break does not count
    assert "上证实时报价已超 10 分钟未更新（最后 10-08 10:00）" in bot.sse_odds(bj(2026, 10, 8, 10, 11))
    bot.cn.quote = m.IndexQuote("上证指数", D("3860"), D("3850.12"), None, None, None, bj(2026, 10, 8, 11, 30), "腾讯")
    assert isinstance(bot.sse_odds(bj(2026, 10, 8, 12, 45)), m.CloseOdds)
    assert isinstance(bot.sse_odds(bj(2026, 10, 8, 13, 5)), m.CloseOdds) and "未更新" in bot.sse_odds(bj(2026, 10, 8, 13, 11))
    # holiday: status says 休市
    assert bot.cn.status(bj(2026, 10, 2, 10, 0), cfg.holidays["sh"]) == "休市"

    # --- the review's example: 09-24 closed at 3888.37, 09-25 holiday, 09-26/27 weekend, 09-28 reopens ---------
    c24 = bj(2026, 9, 24, 15, 0)
    x = m.CnIndex(True, cn); bot.cn = x
    bars24 = [["2026-09-22", "0", "3860.00", "0", "0", "0"], ["2026-09-23", "0", "3870.10", "0", "0", "0"], ["2026-09-24", "0", "3888.37", "0", "0", "0"]]
    feed = {"rows": bars24}
    async def get24(url, timeout=15, headers=None):
        if "fqkline" in url: return json.dumps({"data": {"sh000001": {"day": feed["rows"]}}}).encode()
        raise m.RemoteError("down")
    m.http_get = get24
    # 09-28 08:30 (before the open): a realtime feed may still show a stale or "natural yesterday" price; it is ignored
    now = bj(2026, 9, 28, 8, 30)
    x.quote = m.IndexQuote("上证指数", D("3850.00"), D("3830"), None, None, None, bj(2026, 9, 27, 23, 0), "新浪")
    x.a50 = m.IndexQuote("A50期货", D("14280"), D("14250"), None, None, None, bj(2026, 9, 26, 5, 6), "东方财富")  # Friday night's last print
    await x.refresh_daily(now)
    assert x.close == m.DailyClose(dt.date(2026, 9, 24), D("3888.37"), D("3870.10"), "腾讯日K", now) and x.confirmed(now)
    bot.anchors["A50"] = (c24, D("14200"))
    o = bot.sse_odds(now)
    assert isinstance(o, m.CloseOdds) and o.ref == D("3888.37") and o.ref_note == "09-24 收盘·腾讯日K" and o.target == dt.date(2026, 9, 28), o
    assert abs(float(o.effective) - 3888.37 * math.exp(0.8 * math.log(14280 / 14200))) < 1e-6, o
    assert "｜收盘 09-24 <b>3,888.37</b>（腾讯日K确认）" in m.to_html(x.line(now, "cn", cn))
    # the same holds on the holiday and over the weekend
    for when in (bj(2026, 9, 25, 11, 0), bj(2026, 9, 26, 12, 0), bj(2026, 9, 27, 20, 0)):
        assert x.confirmed(when) and bot.sse_close_ms() == c24
    assert bot.sse_odds(bj(2026, 9, 28, 15, 5)) == "收盘价待确认（等待 09-28 上证日 K，日 K 最新为 09-24）"
    assert "收盘价待确认" in m.to_html(x.line(bj(2026, 9, 28, 15, 5), "cn", cn))
    # 09-28 15:20: the 09-28 close is due but the daily bar is not out yet -> 收盘价待确认, no probability
    now = bj(2026, 9, 28, 15, 20)
    x.quote = m.IndexQuote("上证指数", D("3901.55"), D("3888.37"), None, None, None, bj(2026, 9, 28, 15, 0), "腾讯")
    x.a50 = m.IndexQuote("A50期货", D("14300"), D("14250"), None, None, None, bj(2026, 9, 28, 15, 19), "东方财富")
    await x.refresh_daily(now)
    assert not x.confirmed(now) and x.close.day == dt.date(2026, 9, 24)
    assert bot.sse_odds(now) == "收盘价待确认（等待 09-28 上证日 K，日 K 最新为 09-24）", bot.sse_odds(now)
    assert "｜<b>收盘价待确认</b>（等待 09-28 日 K）" in m.to_html(x.line(now, "cn", cn))
    # the bar arrives -> confirmed; an older answer later never steps the close back
    feed["rows"] = bars24 + [["2026-09-28", "0", "3901.55", "0", "0", "0"]]
    await x.refresh_daily(now)
    assert x.close.day == dt.date(2026, 9, 28) and x.close.value == D("3901.55") and x.close.prev == D("3888.37")
    feed["rows"] = bars24
    await x.refresh_daily(now, force=True); assert x.close.day == dt.date(2026, 9, 28)
    # confirmed: the daily bars are re-read only every 10 minutes
    reads = []
    async def counting(url, timeout=15, headers=None):
        reads.append(url); return await get24(url, timeout, headers)
    m.http_get = counting
    await x.refresh_daily(now); assert not reads
    # before today's bar is final (15:15) today's row is ignored even if the feed already has it
    y = m.CnIndex(True, cn); m.http_get = get24; feed["rows"] = bars24 + [["2026-09-28", "0", "3899", "0", "0", "0"]]
    await y.refresh_daily(bj(2026, 9, 28, 15, 5)); assert y.close.day == dt.date(2026, 9, 24)
    # Tencent answering for the wrong code falls through to Eastmoney
    async def wrong_code(url, timeout=15, headers=None):
        if "fqkline" in url: return json.dumps({"data": {"sz399001": {"day": bars24}}}).encode()
        if "secid=1.000001" in url: return json.dumps({"data": {"code": "000001", "market": 1, "klines": ["2026-09-24,0,3888.37"]}}).encode()
        raise m.RemoteError("down")
    m.http_get = wrong_code; z = m.CnIndex(True, cn); await z.refresh_daily(bj(2026, 9, 27, 10, 0))
    assert z.close.source == "东方财富日K" and z.close.value == D("3888.37") and not z.daily_error
    async def all_down(url, timeout=15, headers=None): raise m.RemoteError("down")
    m.http_get = all_down; w = m.CnIndex(True, cn); await w.refresh_daily(bj(2026, 9, 27, 10, 0))
    assert w.close is None and "腾讯日K" in w.daily_error and "东方财富日K" in w.daily_error
    bot.cn = w; w.quote = x.quote
    assert bot.sse_odds(bj(2026, 9, 27, 10, 0)) == "收盘价待确认（等待 09-24 上证日 K）"
    assert "｜<b>收盘价待确认</b>（等待 09-24 日 K；日 K 获取失败：腾讯日K: " in m.to_html(w.line(bj(2026, 9, 27, 10, 0), "cn", cn))

    # --- A50 freshness (after the confirmed 09-28 close) ----------------------------------------------------
    bot.cn = x; bot.anchors["A50"] = (bj(2026, 9, 28, 15, 0), D("14290"))
    x.a50 = m.IndexQuote("A50期货", D("14350"), D("14300"), None, None, None, bj(2026, 9, 28, 20, 40), "东方财富")
    assert bot.sse_odds(bj(2026, 9, 28, 20, 45)).proxy_note.startswith("A50 14,350 / 15:00 14,290")
    assert bot.sse_odds(bj(2026, 9, 28, 20, 51)) == "A50 报价已超 10 分钟未更新（最后 09-28 20:40），暂不输出新概率"
    assert "｜⚠️ 报价已超 10 分钟未更新" in x.a50_line(bj(2026, 9, 28, 20, 51), "cn", None)
    # outside A50 sessions (16:30-16:45) an older print is expected, not stale
    x.a50 = m.IndexQuote("A50期货", D("14320"), D("14300"), None, None, None, bj(2026, 9, 28, 16, 29), "东方财富")
    o = bot.sse_odds(bj(2026, 9, 28, 16, 40)); assert isinstance(o, m.CloseOdds), o
    # an A50 print from before the close cannot map the after-hours move
    x.a50 = m.IndexQuote("A50期货", D("14320"), D("14300"), None, None, None, bj(2026, 9, 28, 14, 58), "东方财富")
    assert bot.sse_odds(bj(2026, 9, 28, 15, 30)) == "A50 报价已超 10 分钟未更新（最后 09-28 14:58），暂不输出新概率"
    assert "A50 报价已超 10 分钟未更新" in bot.sse_odds(bj(2026, 9, 28, 16, 40))
    # Without a dated anchor, a quote's previous close cannot prove the 15:00 A50 price.
    bot.anchors.pop("A50")
    x.a50 = m.IndexQuote("A50期货", D("14200"), D("14300"), None, None, None, bj(2026, 9, 28, 22, 0), "东方财富")
    assert "缺少 09-28 15:00 的 A50 锚点（东方财富 1 分钟及 5 分钟 K均未取得" in bot.sse_odds(bj(2026, 9, 28, 22, 5))
    x.a50 = m.IndexQuote("A50期货", D("14250"), D("14300"), None, None, None, bj(2026, 9, 28, 16, 29), "东方财富")
    assert "缺少 09-28 15:00 的 A50 锚点（东方财富 1 分钟及 5 分钟 K均未取得" in bot.sse_odds(bj(2026, 9, 28, 16, 40))
    # A50 trading on the 09-25 holiday with no 09-24 15:00 anchor -> explained, no guess
    x.close = m.DailyClose(dt.date(2026, 9, 24), D("3888.37"), D("3870.10"), "腾讯日K", 0)
    x.a50 = m.IndexQuote("A50期货", D("14250"), D("14300"), None, None, None, bj(2026, 9, 25, 11, 58), "东方财富")
    assert "缺少 09-24 15:00 的 A50 锚点（东方财富 1 分钟及 5 分钟 K均未取得" in bot.sse_odds(bj(2026, 9, 25, 12, 0))
    assert x.a50_stale(bj(2026, 9, 26, 16, 0))  # Friday afternoon/night trading is missing
    assert "A50 报价已超 10 分钟未更新" in bot.sse_odds(bj(2026, 9, 26, 16, 0))
    x.a50 = m.IndexQuote("A50期货", D("14297"), D("14300"), None, None, None, bj(2026, 9, 26, 5, 6), "东方财富")
    assert not x.a50_stale(bj(2026, 9, 26, 16, 0))
    # After a late deployment, the one-minute history no longer covers Thursday. Recover from
    # the dated five-minute bar, keep its approximate precision across restarts, and never mix CFDs.
    anchor_rows = {1: [], 5: ["2026-09-24 14:55,14325,14321", "2026-09-24 15:00,14321,14319"]}
    async def anchor_get(url, timeout=15, headers=None):
        if "104.CN00Y" in url and "klt=" in url:
            interval = 5 if "klt=5" in url else 1
            return json.dumps({"data": {"code": "CN00Y", "klines": anchor_rows[interval]}}).encode()
        raise m.RemoteError("other references unavailable")
    m.http_get = anchor_get
    bot.anchors.pop("A50", None); bot.anchor_tries.pop("A50", None)
    await bot.refresh_odds_inputs(bj(2026, 9, 26, 16, 0))
    assert bot.anchors["A50"] == (c24, D("14319"))
    assert store.get("anchor:A50") == [c24, "14319", "15:00 五分钟K近似", "东方财富"]
    o = bot.sse_odds(bj(2026, 9, 26, 16, 0))
    assert isinstance(o, m.CloseOdds) and o.ref == D("3888.37") and o.target == dt.date(2026, 9, 28), o
    assert "15:00 五分钟K近似 14,319" in o.proxy_note and "上证收盘附近" in m.to_html(x.a50_line(bj(2026, 9, 26, 16, 0), "cn", D("14319"), bot.a50_anchor_note))
    bot3 = m.Bot(cfg, store, FakeMarket(cfg), tg); bot3.cn.close = x.close; bot3.cn.a50 = x.a50
    await bot3.refresh_odds_inputs(bj(2026, 9, 26, 16, 0))
    assert bot3.anchors["A50"] == (c24, D("14319")) and bot3.a50_anchor_note == "15:00 五分钟K近似"
    # v1.12.0 saved only [time, price]. Recover it, but require the dated feed to confirm its source.
    old_store = m.Store(":memory:"); old_store.put("anchor:A50", [c24, "14319"])
    old_bot = m.Bot(cfg, old_store, FakeMarket(cfg), tg); old_bot.cn.close = x.close; old_bot.cn.a50 = x.a50
    await old_bot.refresh_odds_inputs(bj(2026, 9, 26, 16, 0))
    assert old_store.get("anchor:A50") == [c24, "14319", "15:00 五分钟K近似", "东方财富"]
    assert isinstance(old_bot.sse_odds(bj(2026, 9, 26, 16, 0)), m.CloseOdds)
    async def no_anchor_data(url, timeout=15, headers=None): raise m.RemoteError("no historical A50")
    missing_store = m.Store(":memory:"); missing_store.put("anchor:A50", [c24, "14319"])
    missing_bot = m.Bot(cfg, missing_store, FakeMarket(cfg), tg); missing_bot.cn.close = x.close; missing_bot.cn.a50 = x.a50
    m.http_get = no_anchor_data
    await missing_bot.refresh_odds_inputs(bj(2026, 9, 26, 16, 0))
    assert missing_bot.anchors["A50"] == (c24, D("14319")) and missing_bot.a50_anchor_source == "未知"
    assert "旧版 A50 锚点未记录合约来源" in missing_bot.sse_odds(bj(2026, 9, 26, 16, 0))
    m.http_get = anchor_get
    x.a50 = m.IndexQuote("A50期货", D("14297"), D("14300"), None, None, None, bj(2026, 9, 26, 5, 6), "新浪CFD")
    assert bot.sse_odds(bj(2026, 9, 26, 16, 0)) == "A50 锚点来自东方财富期货，当前只有新浪CFD报价；不同合约不能混算，暂不输出概率"
    # the same futures via Eastmoney's K line is not a different contract
    x.a50 = m.IndexQuote("A50期货", D("14297"), None, None, None, None, bj(2026, 9, 26, 5, 6), "东方财富K线")
    assert isinstance(bot.sse_odds(bj(2026, 9, 26, 16, 0)), m.CloseOdds)
    x.a50 = m.IndexQuote("A50期货", D("14297"), D("14300"), None, None, None, bj(2026, 9, 26, 5, 6), "东方财富")
    anchor_rows[1] = ["2026-09-24 15:00,14320,14318"]
    bot.anchor_tries.pop("A50-exact", None)
    await bot.refresh_odds_inputs(bj(2026, 9, 26, 16, 0))
    assert bot.anchors["A50"] == (c24, D("14318")) and bot.a50_anchor_note == "15:00"
    assert store.get("anchor:A50") == [c24, "14318", "15:00", "东方财富"]
    async def wrong_anchor_code(url, timeout=15, headers=None):
        return json.dumps({"data": {"code": "OTHER", "klines": anchor_rows[5]}}).encode()
    m.http_get = wrong_anchor_code
    try: await x.a50_five_minute_at(dt.date(2026, 9, 24)); assert False
    except ValueError as e: assert "代码异常" in str(e)
    x.close = m.DailyClose(dt.date(2026, 9, 28), D("3901.55"), D("3888.37"), "腾讯日K", 0)
    # no A50 at all -> no probability, labelled
    x.a50 = None
    assert bot.sse_odds(bj(2026, 9, 28, 16, 45)) == "暂无 A50 报价，暂不输出概率"
    print("SSE_OK")
asyncio.run(run())
