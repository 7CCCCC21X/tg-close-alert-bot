import asyncio, sys, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m

class FakeTelegram:
    def __init__(self): self.sent = []
    async def call(self, *a, **k): return True
    async def send(self, chat, thread, text, reply_markup=None, parse_mode=None): self.sent.append(text)

async def run():
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ADMIN_USER_ID": "42"})
    store = m.Store(":memory:"); tg = FakeTelegram(); bot = m.Bot(cfg, store, m.Binance(cfg), tg)
    today = m.beijing_day()
    async def ask(text):
        tg.sent.clear(); await bot.process_message({"text": text, "chat": {"id": 1}, "from": {"id": 42}, "date": time.time()}); return tg.sent[-1]
    def rec(sym, day=today): return store.get(f"manual:{sym}:{day}")

    # single form unchanged
    r = await ask("/setclose UNITREE 75"); assert "✅ UNITREEUSDT 手动参考价：75｜适用日 " + today in r, r
    assert rec("UNITREEUSDT") == {"value": "75", "valid_date": today} and "不是手动模式" in r
    # multi-line with leading global date
    r = await ask("/setclose 2026-09-20\nSHEIN 40\n长鑫 8.5\nSKHYNIX 1300 2026-09-21")
    assert rec("HK0625USDT", "2026-09-20")["value"] == "40" and rec("CXMTUSDT", "2026-09-20")["value"] == "8.5"
    assert rec("SKHYNIXUSDT", "2026-09-21")["value"] == "1300" and r.count("✅") == 3, r
    # comma separated, date on same line as first pair
    r = await ask("/setclose 2026-09-22 UNITREE 76, SHEIN 41，CXMT 9")
    assert r.count("✅") == 3 and rec("UNITREEUSDT", "2026-09-22")["value"] == "76" and rec("CXMTUSDT", "2026-09-22")["value"] == "9"
    # atomic: one bad line rejects everything
    before = store.get("manual:HK0625USDT:2026-09-23")
    r = await ask("/setclose 2026-09-23\nSHEIN 42\nFOO 1"); assert "❌" in r and "未知合约" in r and rec("HK0625USDT", "2026-09-23") is None, r
    r = await ask("/setclose 2026-09-23\nSHEIN 42\nCXMT abc"); assert "CXMT 价格必须是数字" in r and rec("HK0625USDT", "2026-09-23") is None, r
    r = await ask("/setclose SHEIN 42 2026/09/23"); assert "YYYY-MM-DD" in r, r
    r = await ask("/setclose"); assert "批量" in r and f"/setclose {today}" in r, r
    r = await ask("/setclose UNITREE"); assert "❌" in r
    r = await ask("/setclose 2026-09-23"); assert "❌" in r
    r = await ask("/setclose UNITREE 1\nUNITREE 2"); assert "两条不同的记录" in r, r
    r = await ask("/setclose UNITREE 3\nUNITREE 3"); assert r.count("✅") == 1
    # manual mode: template in /mode reply, "missing today" hint
    r = await ask("/mode manual"); assert f"/setclose {today}\nUNITREE 价格 MM-DD HH:MM\nSHEIN 价格 MM-DD HH:MM" in r, r
    r = await ask(f"/setclose {today}\nSHEIN 40\nCXMT 8"); assert "尚未设置手动参考价：SKHYNIXUSDT" in r and "UNITREEUSDT" not in r.split("尚未设置")[1], r
    r = await ask("/setclose SKHYNIX 1300"); assert "尚未设置" not in r, r
    assert "批量" in await ask("/help")
    print("BATCH_OK")
asyncio.run(run())
