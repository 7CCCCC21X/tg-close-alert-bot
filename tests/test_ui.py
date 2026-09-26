import asyncio, sys, time, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D

# --- html rendering & marks
assert m.to_html(m.bold("a<b") + " & c") == "<b>a&lt;b</b> &amp; c"
assert m.trend_mark(D("1"), "cn") == "🔴" and m.trend_mark(D("-1"), "cn") == "🟢" and m.trend_mark(D("0.0001"), "cn") == "⚪"
assert m.trend_mark(D("1"), "us") == "🟢" and m.trend_mark(D("-1"), "us") == "🔴"
assert m.pct_text(D("1.5"), "cn") == "🔴 +1.50%" and m.pct_text(D("-2"), "us", strong=True) == "🔴 " + m.bold("-2.00%") and m.pct_text(D("1"), "cn", digits=3) == "🔴 +1.000%"

# --- config
c = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "COLOR_STYLE": "US", "FX_RATES": "cny=7.12, HKD=7.79"})
assert c.color_style == "us" and c.fx_manual == {"CNY": D("7.12"), "HKD": D("7.79")}
assert c.tickers["HK0625USDT"].same_unit and not c.tickers["UNITREEUSDT"].same_unit
for bad in [{"COLOR_STYLE": "red"}, {"FX_RATES": "XYZ=1"}, {"FX_RATES": "CNY=abc"}]:
    try: m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", **bad}); assert False, bad
    except ValueError: pass

# --- FxRates
fx = m.FxRates({"HKD": D("7.8")})
assert fx.rate("USDT") == 1 and fx.rate("HKD") == D("7.8") and fx.rate("CNY") is None and fx.rate("CNH") is None
fx.rates = {"CNY": D("7.1"), "KRW": D("1390")}
assert fx.rate("cny") == D("7.1") and fx.rate("CNH") == D("7.1") and fx.rate("JPY") is None
assert "7.8 HKD（手动）" in fx.summary() and "7.1 CNY" in fx.summary()

async def run():
    calls = []
    responses = {}
    async def fake_json(url, payload=None, timeout=15):
        calls.append(url)
        r = responses.get(url.split("/")[2])
        if isinstance(r, Exception): raise r
        return r
    m.http_json = fake_json
    responses["api.frankfurter.app"] = {"amount": 1.0, "base": "USD", "date": "2026-09-18", "rates": {"CNY": 7.1219, "HKD": 7.7912, "KRW": 1388.4, "JPY": 146.2}}
    f = m.FxRates(); await f.refresh()
    assert f.rate("KRW") == D("1388.4") and "Frankfurter" in f.source and f.updated == "2026-09-18" and not f.error, f.__dict__
    n = len(calls); await f.refresh(); assert len(calls) == n  # cached 6h
    # primary fails -> fallback
    responses["api.frankfurter.app"] = m.RemoteError("HTTP 503: 接口请求失败")
    responses["open.er-api.com"] = {"result": "success", "time_last_update_utc": "Fri, 18 Sep 2026 00:02:31 +0000", "rates": {"USD": 1, "CNY": 7.13, "HKD": 7.8, "KRW": 1390, "TWD": 32.1, "XAU": 0.0004}}
    await f.refresh(force=True)
    assert f.rate("TWD") == D("32.1") and "XAU" not in f.rates and f.source == "open.er-api.com" and not f.error
    # both fail -> keep old rates, record error
    responses["open.er-api.com"] = {"rates": "nope"}
    await f.refresh(force=True)
    assert f.rate("CNY") == D("7.13") and "HTTP 503" in f.error and "rates" in f.error and "汇率获取失败" in f.summary()

    # --- reference rows
    ex = m.Baseline(D("514.98"), "k", "证券交易所收盘价｜2026-09-18 上交所", 0, int(time.time() * 1000), "CNY", "上交所688836·自动")
    row = m.reference_row("exchange", D("76.41"), ex, f, "cn")
    assert row.startswith("交易所 " + m.bold("514.98 CNY") + " ≈ 72.227（") and "收·自动）→ 🔴 +5.79%" in row, row
    hk = m.Baseline(D("37.76"), "k", "l", 0, 0, "", "港交所00625·自动")
    assert m.reference_row("exchange", D("38.52"), hk, f, "cn") == "交易所 " + m.bold("37.76") + "（上一交易日·自动）→ 🔴 +2.01%"
    assert "→ ⚪ 无 SGD 汇率" in m.reference_row("exchange", D("1"), m.Baseline(D("100"), "k", "l", 0, 0, "SGD"), m.FxRates(), "cn")
    assert m.reference_row("exchange", D("1"), None, f, "cn") == "交易所 未设置（/setexchange）"

    # --- Bot status + alert through Telegram (HTML mode)
    class FakeTelegram:
        def __init__(self): self.sent = []
        async def call(self, *a, **k): return True
        async def send(self, chat, thread, text, reply_markup=None, parse_mode=None): self.sent.append((text, parse_mode))
    class FakeMarket:
        def __init__(self, c): self.config = c; self.price = "76.41"
        def now_ms(self): return int(time.time() * 1000)
        async def sync_clock(self): pass
        async def prices(self): return {s: {"symbol": s, "price": self.price, "time": self.now_ms()} for s in self.config.symbols}
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ADMIN_USER_ID": "42", "SYMBOLS": "UNITREEUSDT", "BASELINE_MODE": "manual",
                             "MIN_ALERT_GAP_SECONDS": "0", "EXCHANGE_TICKERS": "off", "FX_RATES": "CNY=7.13", "HSI_FUTURES": "off", "KOSPI_INDEX": "off", "HL_TICKERS": "off"})
    store = m.Store(":memory:"); tg = FakeTelegram(); bot = m.Bot(cfg, store, FakeMarket(cfg), tg)
    today = m.beijing_day()
    async def ask(text):
        tg.sent.clear(); await bot.process_message({"text": text, "chat": {"id": 1}, "from": {"id": 42}, "date": time.time()}); return tg.sent[-1]
    assert (await ask("/help"))[1] is None  # plain text commands untouched
    await ask("/setclose UNITREE 75"); await ask("/setexchange UNITREE 514.98 CNY 09-17 15:00")
    store.put("subscriptions", {"1:0": {"chat": 1, "thread": 0, "active": True}})
    tg.sent.clear(); await bot.one_cycle()
    alert = [t for t, mode in tg.sent if "上涨超过" in t]; assert alert, tg.sent
    a = alert[0]; mode = [mode for t, mode in tg.sent if t == a][0]; assert mode == "HTML"
    assert a.startswith("🔴 <b>上涨超过 1%｜宇树 UNITREE</b>（UNITREEUSDT）\n币安 <b>76.41</b>｜"), a
    assert f"├ 基准 75（手动·适用 {today[5:]}）→ 🔴 <b>+1.880%</b>" in a and "├ 交易所 <b>514.98 CNY</b> ≈ 72.227（" in a and "→ 🔴 +5.79%" in a, a
    assert "└ 📝 " in a and "\x01" not in a and a.endswith("⚠️ 合约行情提示，不代表股票官方收盘结算结果。"), a
    st, mode = await ask("/status")
    assert mode == "HTML" and st.startswith("📡 <b>监控状态 v1.12.1</b>｜🟢 已订阅\n⚙️ 基准 手动参考价｜阈值 ±1%｜每 5 秒｜周期 300 秒\n📊 🔴 涨 🟢 跌 ⚪ 平"), st
    assert "<b>📍 宇树 UNITREE｜UNITREEUSDT</b>\n├ 币安 <b>76.41</b>｜" in st and "💱 1 USD = 7.13 CNY（手动）" in st, st
    # error text with html-sensitive characters is escaped
    bot.snapshots["UNITREEUSDT"] = {"error": "接口 <bad> & broken"}
    st, _ = await ask("/status"); assert "└ ⚠️ 接口 &lt;bad&gt; &amp; broken" in st, st
    print("UI_OK")
asyncio.run(run())
