import asyncio, sys, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m

class FakeTelegram:
    def __init__(self): self.sent = []
    async def send(self, chat, thread, text, reply_markup=None, parse_mode=None): self.sent.append(text)

class FakeMarket:
    def __init__(self, cfg): self.config = cfg; self.price = "76"
    def now_ms(self): return int(time.time() * 1000)
    async def sync_clock(self): pass
    async def prices(self):
        return {s: {"symbol": s, "price": self.price, "time": self.now_ms()} for s in self.config.symbols}

async def run():
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ADMIN_USER_ID": "42", "SYMBOLS": "UNITREEUSDT", "BASELINE_MODE": "manual", "MIN_ALERT_GAP_SECONDS": "0"})
    store = m.Store(":memory:"); tg = FakeTelegram(); bot = m.Bot(cfg, store, FakeMarket(cfg), tg)
    today = m.beijing_day()
    store.put("subscriptions", {"1:0": {"chat": 1, "thread": 0, "active": True}})
    await bot.one_cycle()
    assert len(tg.sent) == 1 and "行情监控异常" in tg.sent[0] and "缺少" in tg.sent[0], tg.sent  # no manual baseline yet
    store.put(f"manual:UNITREEUSDT:{today}", {"value": "75", "valid_date": today})
    await bot.one_cycle()
    assert "数据恢复" in tg.sent[1] and "上涨超过 1%" in tg.sent[2] and "+1.333%" in tg.sent[2], tg.sent
    assert store.get("alert:1:0:UNITREEUSDT")["side"] == 1
    await bot.one_cycle(); assert len(tg.sent) == 3  # dedup: no repeat within cooldown
    bot.market.price = "73"; await bot.one_cycle()
    assert "下跌超过" in tg.sent[3] and "方向反转" in tg.sent[3], tg.sent[3:]
    print("CYCLE_OK")
asyncio.run(run())
