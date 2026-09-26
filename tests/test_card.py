import asyncio, sys, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m

class RecTelegram(m.Telegram):
    """Real send/edit/paced logic, fake network."""
    def __init__(self):
        super().__init__("1:x"); self.calls = []; self.fail_edit = False
    async def call(self, method, payload=None, timeout=15):
        self.calls.append((method, payload))
        if method == "editMessageText" and self.fail_edit:
            raise m.RemoteError("Telegram: Bad Request: message is not modified")
        return True
    def of(self, method): return [p for mth, p in self.calls if mth == method]

async def run():
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ADMIN_USER_ID": "42"})
    tg = RecTelegram(); bot = m.Bot(cfg, m.Store(":memory:"), m.Binance(cfg), tg); bot.username = "TestBot"
    def msg(text, uid=42): return {"text": text, "chat": {"id": -100}, "message_thread_id": 7, "from": {"id": uid}, "date": time.time()}
    def tap(data, uid=42, mid=555): return {"id": "q1", "data": data, "from": {"id": uid}, "message": {"message_id": mid, "chat": {"id": -100}, "date": time.time()}}

    # /threshold with no args -> card with keyboard, current 1% ticked
    await bot.process_update({"update_id": 1, "message": msg("/threshold")})
    sent = tg.of("sendMessage"); assert len(sent) == 1, tg.calls
    kb = sent[0]["reply_markup"]["inline_keyboard"]; flat = [b for row in kb for b in row]
    assert len(kb) == 2 and len(flat) == len(m.THRESHOLD_PRESETS) and sent[0]["message_thread_id"] == 7
    assert [b["text"] for b in flat if b["text"].startswith("✅")] == ["✅ ±1%"], flat
    assert all(len(b["callback_data"].encode()) <= 64 for b in flat)
    assert "跟上一日收盘价" in sent[0]["text"] and "±1%" in sent[0]["text"]

    # non-admin tap: rejected toast, nothing changes
    await bot.process_update({"update_id": 2, "callback_query": tap("threshold:5", uid=9)})
    assert tg.of("answerCallbackQuery")[-1]["text"] == "仅管理员可以修改设置" and bot.settings()["threshold"] == "1"
    assert not tg.of("editMessageText")

    # admin tap: setting applied, toast, card edited with new tick, alert state cleared
    bot.store.put("alert:-100:7:UNITREEUSDT", {"side": 1})
    await bot.process_update({"update_id": 3, "callback_query": tap("threshold:2")})
    assert bot.settings()["threshold"] == "2" and bot.store.get("alert:-100:7:UNITREEUSDT") is None
    assert "±2%" in tg.of("answerCallbackQuery")[-1]["text"]
    ed = tg.of("editMessageText")[-1]; assert ed["message_id"] == 555 and ed["chat_id"] == -100 and "±2%" in ed["text"]
    ticked = [b["text"] for row in ed["reply_markup"]["inline_keyboard"] for b in row if b["text"].startswith("✅")]
    assert ticked == ["✅ ±2%"], ticked

    # bad/unknown callback data -> alert toast, no crash; "not modified" edit error swallowed
    await bot.process_update({"update_id": 4, "callback_query": tap("threshold:abc")})
    assert tg.of("answerCallbackQuery")[-1]["show_alert"] and "❌" in tg.of("answerCallbackQuery")[-1]["text"]
    await bot.process_update({"update_id": 5, "callback_query": tap("nope:1")})
    assert "未知操作" in tg.of("answerCallbackQuery")[-1]["text"]
    tg.fail_edit = True; await bot.process_update({"update_id": 6, "callback_query": tap("threshold:2")})
    assert bot.settings()["threshold"] == "2"

    # typed form still works; custom value not in presets shows no tick
    await bot.process_update({"update_id": 7, "message": msg("/threshold 0.8%")})
    assert "±0.8%" in tg.of("sendMessage")[-1]["text"] and bot.settings()["threshold"] == "0.8"
    await bot.process_update({"update_id": 8, "message": msg("/threshold")})
    assert not any(b["text"].startswith("✅") for row in tg.of("sendMessage")[-1]["reply_markup"]["inline_keyboard"] for b in row)
    await bot.process_update({"update_id": 9, "message": msg("/threshold 1 2")})
    assert "❌" in tg.of("sendMessage")[-1]["text"]
    # stale message ignored, long text: markup only on last chunk
    n = len(tg.calls); await bot.process_update({"update_id": 10, "message": {**msg("/threshold"), "date": time.time() - 5000}}); assert len(tg.calls) == n
    await tg.send(1, 0, "x\n" * 4000, {"inline_keyboard": []})
    chunks = tg.of("sendMessage")[-3:]; assert len(chunks) >= 2 and "reply_markup" in chunks[-1] and "reply_markup" not in chunks[-2]
    print("CARD_OK")
asyncio.run(run())
