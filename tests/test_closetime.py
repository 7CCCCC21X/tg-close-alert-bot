import asyncio, sys, time, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m

class FakeTelegram:
    def __init__(self): self.sent = []
    async def call(self, *a, **k): return True
    async def send(self, chat, thread, text, reply_markup=None, parse_mode=None): self.sent.append(text)

async def run():
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ADMIN_USER_ID": "42", "BASELINE_MODE": "manual"})
    store = m.Store(":memory:"); tg = FakeTelegram(); bot = m.Bot(cfg, store, m.Binance(cfg), tg)
    now_ms = bot.market.now_ms(); today = m.beijing_day(now_ms / 1000); year = today[:4]
    async def ask(text):
        tg.sent.clear(); await bot.process_message({"text": text, "chat": {"id": 1}, "from": {"id": 42}, "date": time.time()}); return tg.sent[-1]
    def rec(sym, day=today): return store.get(f"manual:{sym}:{day}")
    yday = (dt.date.fromisoformat(today) - dt.timedelta(days=1)); yd = f"{yday.month:02d}-{yday.day:02d}"

    # per-entry close time with short date
    r = await ask(f"/setclose UNITREE 75 {yd} 16:00")
    assert rec("UNITREEUSDT") == {"value": "75", "valid_date": today, "close_at": f"{yday.isoformat()}T16:00"}, rec("UNITREEUSDT")
    assert f"收盘 {yd} 16:00（北京时间）" in r, r
    # full-date close time + explicit applicable day
    r = await ask(f"/setclose SHEIN 40 {yday.isoformat()} 15:30 2026-12-31")
    assert rec("HK0625USDT", "2026-12-31")["close_at"] == f"{yday.isoformat()}T15:30" and "适用日 2026-12-31" in r, r
    # applicable day first, then close time
    r = await ask(f"/setclose CXMT 8 2026-12-30 {yd} 15:00"); assert rec("CXMTUSDT", "2026-12-30")["close_at"].endswith("T15:00")
    # global header: day + close time, per-line override
    r = await ask(f"/setclose 2026-12-29 {yd} 16:00\nUNITREE 70\nSHEIN 39 {yd} 15:30\nCXMT 7")
    assert rec("UNITREEUSDT", "2026-12-29")["close_at"].endswith("T16:00") and rec("HK0625USDT", "2026-12-29")["close_at"].endswith("T15:30")
    assert rec("CXMTUSDT", "2026-12-29")["close_at"].endswith("T16:00") and r.count("收盘") == 3, r
    # global close time only (today applies)
    r = await ask(f"/setclose {yd} 14:30 SKHYNIX 1300"); assert rec("SKHYNIXUSDT")["close_at"].endswith("T14:30") and rec("SKHYNIXUSDT")["valid_date"] == today
    # no close time -> record without close_at, reply says so
    r = await ask("/setclose SKHYNIX 1301"); assert "close_at" not in rec("SKHYNIXUSDT") and "未填收盘时间" in r
    # validation: future time, bad format, leftover junk
    tomorrow = dt.date.fromisoformat(today) + dt.timedelta(days=1)
    r = await ask(f"/setclose UNITREE 75 {tomorrow.isoformat()} 09:00"); assert "不能晚于当前时间" in r, r
    r = await ask("/setclose UNITREE 75 13-40 16:00"); assert "收盘时间格式" in r, r
    r = await ask("/setclose UNITREE 75 09-17 25:00"); assert "收盘时间格式" in r, r
    r = await ask("/setclose UNITREE 75 foo"); assert "无法识别「foo」" in r, r
    r = await ask("/setclose UNITREE 75 16:00"); assert "无法识别「16:00」" in r, r  # lone time is ambiguous
    # status + alert text carry the close time; old records without close_at still work
    store.put(f"manual:UNITREEUSDT:{today}", {"value": "75", "valid_date": today, "close_at": f"{yday.isoformat()}T16:00"})
    base = m.manual_baseline(rec("UNITREEUSDT"), now_ms)
    assert f"收盘 {yd} 16:00（北京时间）" in base.label and base.close_ms > 0, base
    old = m.manual_baseline({"value": "1", "valid_date": today}, now_ms); assert old.close_ms == 0 and "收盘" not in old.label
    bad = m.manual_baseline({"value": "1", "valid_date": today, "close_at": "garbage"}, now_ms); assert bad.close_ms == 0
    t = m.alert_text("UNITREEUSDT", m.Quote(m.D("76"), now_ms), base, m.D("1.333"), m.D("1"), "x"); assert "·16:00 收" in t
    bot.snapshots["UNITREEUSDT"] = {"quote": m.Quote(m.D("76"), now_ms), "baseline": base}
    assert "·16:00 收" in await ask("/status")
    # daily baseline label includes the candle close time (Beijing 08:00)
    boundary = now_ms // m.DAY_MS * m.DAY_MS
    rows = [[boundary - m.DAY_MS, "1", "2", "0.5", "1.5", "0", boundary - 1]]
    d = m.daily_baseline(rows, now_ms); assert d.close_ms == boundary and "收盘" in d.label and " 08:00（北京时间，即 UTC 00:00）" in d.label, d.label
    print("CLOSETIME_OK")
asyncio.run(run())
