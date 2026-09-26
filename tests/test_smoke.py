import asyncio, re, sys, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m

class FakeTelegram:
    def __init__(self): self.calls = []; self.sent = []
    async def call(self, method, payload=None, timeout=15):
        self.calls.append((method, payload))
        return {"url": ""} if method == "getWebhookInfo" else {"username": "TestBot"} if method == "getMe" else True
    async def send(self, chat, thread, text, reply_markup=None, parse_mode=None): self.sent.append((chat, thread, text))

async def run():
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ADMIN_USER_ID": "42"})
    store = m.Store(":memory:")
    tg = FakeTelegram()
    bot = m.Bot(cfg, store, m.Binance(cfg), tg)
    bot.username = "TestBot"

    # menu payload validity
    await bot.register_menu()
    methods = [c[0] for c in tg.calls]
    assert methods == ["setMyCommands", "setChatMenuButton"], methods
    cmds = tg.calls[0][1]["commands"]
    assert len(cmds) == 16 and "diag" in [c["command"] for c in cmds]
    for c in cmds:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", c["command"]), c
        assert 1 <= len(c["description"]) <= 256, c
    assert tg.calls[1][1] == {"menu_button": {"type": "commands"}}
    assert all(f"/{c['command']}" in bot.handlers for c in cmds)

    def msg(text, uid=42, chat=-100, thread=7):
        return {"text": text, "chat": {"id": chat}, "message_thread_id": thread, "from": {"id": uid}, "date": time.time()}
    async def ask(text, **kw):
        tg.sent.clear(); await bot.process_message(msg(text, **kw)); return tg.sent[-1][2] if tg.sent else None

    # non-admin: only id/help/start reply with the ID notice; others silent
    r = await ask("/start", uid=5); assert "ADMIN_USER_ID" in r and "用户 ID：5" in r, r
    assert await ask("/help", uid=6) and await ask("/subscribe", uid=7) is None and await ask("/status", uid=8) is None
    # admin flows
    assert (await ask("/help")).startswith("📡"); assert "/setclose UNITREE 75 09-17 16:00" in await ask("/start")
    assert await ask("/id") == "你的用户 ID：42\n聊天 ID：-100\n话题 ID：7"
    assert await ask("/subscribe@TestBot") and bot.subscriptions()["-100:7"]["active"]
    assert await ask("/subscribe@OtherBot") is None
    assert "暂停" in await ask("/pause") and not bot.subscriptions()["-100:7"]["active"]
    assert "恢复" in await ask("/resume") and bot.subscriptions()["-100:7"]["active"]
    assert "📏" in await ask("/threshold"); assert "❌" in await ask("/threshold 0")
    assert "±2.5%" in await ask("/threshold 2.5%") and bot.settings()["threshold"] == "2.5"
    assert "❌" in await ask("/cooldown x"); assert "600 秒" in await ask("/cooldown 600") and bot.settings()["cooldown"] == 600
    assert "❌" in await ask("/mode"); assert "❌" in await ask("/mode foo")
    assert "UNITREE 价格" in await ask("/mode manual") and bot.settings()["mode"] == "manual"
    assert "❌" in await ask("/setclose UNITREE"); assert "❌" in await ask("/setclose XXX 1")
    r = await ask("/setclose 宇树 75 2026-09-18"); assert "UNITREEUSDT 手动参考价：75" in r and "不是手动模式" not in r, r
    assert store.get("manual:UNITREEUSDT:2026-09-18") == {"value": "75", "valid_date": "2026-09-18"}
    assert "日K" in await ask("/mode daily") and bot.settings()["mode"] == "binance_daily"
    assert "不是手动模式" in await ask("/setclose CXMT 8")
    assert (await ask("/status")).startswith(f"📡 <b>监控状态 v{m.VERSION}</b>｜🟢 已订阅"); assert await ask("/price") == await ask("/status")
    assert "测试" in await ask("/test"); assert "未知命令" in await ask("/nope")
    assert "已取消" in await ask("/unsubscribe") and "-100:7" not in bot.subscriptions()
    assert await ask("hello") is None
    # alert text + wait helper
    q = m.Quote(m.D("76"), 1_700_000_000_000); b = m.Baseline(m.D("75"), "k", "label", 0)
    t = m.alert_text("UNITREEUSDT", q, b, m.percent(q.price, b.value), m.D("1"), "首次")
    assert "上涨超过 1%｜宇树 UNITREE" in t and "+1.333%" in t, t
    t0 = time.monotonic(); await bot.wait(0.05); assert time.monotonic() - t0 >= 0.05
    bot.stopping.set(); t0 = time.monotonic(); await bot.wait(5); assert time.monotonic() - t0 < 1
    print(m.HELP); print("SMOKE_OK")

asyncio.run(run())
