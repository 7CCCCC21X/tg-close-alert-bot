"""Reference feeds refresh in their own tasks; the 5-second alert loop only reads their last good results."""
import asyncio, sys, time, threading, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D


class FakeTelegram:
    def __init__(self): self.sent = []
    async def call(self, *a, **k): return True
    async def send(self, chat, thread, text, reply_markup=None, parse_mode=None): self.sent.append(text)


class FakeMarket(m.Binance):
    def now_ms(self): return int(time.time() * 1000)
    async def sync_clock(self): pass
    async def prices(self): return {"UNITREEUSDT": {"symbol": "UNITREEUSDT", "price": "73.13", "time": self.now_ms()}}
    async def get(self, path, **p):
        if path == "/fapi/v1/klines" and p.get("interval") == "1m":
            return [[p["startTime"], "72", "72", "72", "72.00", "5", p["startTime"] + 59_999]]
        return []


async def no_http(*a, **k): raise m.RemoteError("offline test")


def make_bot():
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ADMIN_USER_ID": "42", "SYMBOLS": "UNITREEUSDT",
                             "EXCHANGE_TICKERS": "UNITREEUSDT=sh:688836", "MIN_ALERT_GAP_SECONDS": "0"})
    tg = FakeTelegram(); bot = m.Bot(cfg, m.Store(":memory:"), FakeMarket(cfg), tg)
    day = m.expected_close_date("sh", bot.market.now_ms(), cfg.holidays["sh"])
    bot.stocks.closes["UNITREEUSDT"] = m.StockMarket.baseline(m.StockTicker("sh", "688836"), m.STOCK_MARKETS["sh"], "东方财富",
                                                              day, D("490"), D("489"))
    bot.store.put("subscriptions", {"1:0": {"chat": 1, "thread": 0, "active": True}})
    return bot, tg


async def run():
    m.http_get = no_http; m.http_json = no_http
    m.REFERENCE_TICK, m.REFERENCE_TIMEOUT = 0.05, 0.3

    # --- hung and failing feeds never delay the alert loop -------------------------------------------------
    bot, tg = make_bot()
    calls = {"hsi": 0, "kospi": 0, "cn": 0}
    async def hang(now_ms, force=False):
        calls["hsi"] += 1
        await asyncio.sleep(3600)
    async def broken(now_ms, force=False):
        calls["kospi"] += 1
        raise RuntimeError("feed exploded")
    async def fine(now_ms, force=False):
        calls["cn"] += 1
    bot.hsi.refresh, bot.kospi.refresh, bot.cn.refresh = hang, broken, fine
    bot.start_reference_tasks()
    assert [t.get_name() for t in bot.reference_tasks] == ["reference:交易所收盘", "reference:汇率", "reference:恒指期货",
                                                            "reference:Hyperliquid", "reference:KOSPI", "reference:上证/A50", "reference:概率输入"]
    started = time.monotonic()
    await bot.one_cycle()
    assert time.monotonic() - started < 1.0, "the alert loop must not wait for reference feeds"
    alert = [t for t in tg.sent if "上涨超过" in t]
    assert alert and "基准 72" in alert[0], tg.sent
    await asyncio.sleep(1.0)
    # the hung feed is abandoned after REFERENCE_TIMEOUT and retried; the failing one keeps retrying; others keep running
    assert calls["hsi"] >= 2 and calls["kospi"] >= 5 and calls["cn"] >= 5, calls
    assert all(not t.done() for t in bot.reference_tasks), "a failing feed must not end its task"
    # an alert cycle while all that happens is still fast
    started = time.monotonic(); await bot.one_cycle(); assert time.monotonic() - started < 1.0
    bot.stopping.set()
    await asyncio.wait_for(asyncio.gather(*bot.reference_tasks, return_exceptions=True), 2)
    bot.reference_pool.shutdown(wait=False)

    # --- reference requests run on their own thread pool -----------------------------------------------------
    bot, _ = make_bot()
    seen = []
    async def probe():
        seen.append(await m._blocking(lambda: threading.current_thread().name))
        bot.stopping.set()
    bot.reference_pool = m.ThreadPoolExecutor(max_workers=2, thread_name_prefix="reference")
    await asyncio.wait_for(bot.reference_loop("probe", probe), 2)
    assert seen and seen[0].startswith("reference"), seen
    assert not (await m._blocking(lambda: threading.current_thread().name)).startswith("reference")  # default pool elsewhere
    bot.reference_pool.shutdown(wait=False)

    # --- /diag refresh health: skipped (not-due) ticks are not runs; a first run still going is not "✅" ------
    bot, _ = make_bot()
    ticks = {"n": 0}
    async def throttled():
        ticks["n"] += 1
        if ticks["n"] == 1:
            await asyncio.sleep(0.2)  # the one real fetch
            return None
        if ticks["n"] >= 4: bot.stopping.set()
        return False               # not due: nothing fetched
    bot.reference_pool = m.ThreadPoolExecutor(max_workers=1, thread_name_prefix="reference")
    await asyncio.wait_for(bot.reference_loop("汇率", throttled), 2)
    state = bot.reference_state["汇率"]
    assert state["runs"] == 1 and state["ms"] >= 150, state  # the real fetch's timing is kept
    started_run = asyncio.Event()
    async def slow_first():
        started_run.set(); await asyncio.sleep(3600)
    bot.stopping.clear()
    task = asyncio.create_task(bot.reference_loop("交易所收盘", slow_first))
    await started_run.wait()
    text = "\n".join(bot.diag_state(bot.market.now_ms()))
    assert "⏳ 交易所收盘：首轮进行中" in text and "✅ 汇率" in text and "上次用时 0.2s｜共 1 轮" in text, text
    task.cancel(); bot.reference_pool.shutdown(wait=False)
    # hosts moved to the back after repeated failures are listed
    m.SOURCE_HEALTH.hosts.clear()
    for _ in range(2): m.SOURCE_HEALTH.record("https://push2.eastmoney.com/api", "HTTP 502: 接口请求失败")
    text = "\n".join(bot.diag_state(bot.market.now_ms()))
    assert "⏸️ push2.eastmoney.com：连续失败 2 次" in text and "HTTP 502" in text, text
    m.SOURCE_HEALTH.hosts.clear()

    # --- without background tasks (tests, one-off runs) one_cycle refreshes inline --------------------------
    bot, tg = make_bot()
    hits = []
    async def count(now_ms, force=False): hits.append(1)
    bot.cn.refresh = count
    await bot.one_cycle(); await bot.one_cycle()
    assert len(hits) == 2 and [t for t in tg.sent if "上涨超过" in t]

    # --- an undated fallback quote repeating the dated close keeps its date -----------------------------------
    bot, _ = make_bot()
    dated = bot.stocks.closes["UNITREEUSDT"]
    async def undated(symbol, ticker, now_ms):
        return m.StockMarket.baseline(ticker, m.STOCK_MARKETS["sh"], "腾讯", None, value["v"])
    bot.stocks.fetch = undated
    value = {"v": D("490")}
    await bot.stocks.refresh(bot.market.now_ms(), force=True)
    assert bot.stocks.closes["UNITREEUSDT"] is dated
    value["v"] = D("491")  # a different price is newer information: take it (undated)
    await bot.stocks.refresh(bot.market.now_ms(), force=True)
    assert bot.stocks.closes["UNITREEUSDT"].value == D("491") and bot.stocks.closes["UNITREEUSDT"].close_ms == 0

    # --- fresh start without saved closes: "still loading" is shown, but no fault notice is sent ------------
    bot, tg = make_bot()
    bot.stocks.closes.clear()
    await bot.one_cycle()
    # inline mode has already tried (and failed) the feed, so this is a real fault
    assert "尚未由带日期的日 K 确认" in bot.snapshots["UNITREEUSDT"]["error"] and "pending" not in bot.snapshots["UNITREEUSDT"]
    bot, tg = make_bot()
    bot.stocks.closes.clear()
    bot.reference_tasks = [object()]  # pretend the background tasks exist and have not finished a fetch yet
    await bot.one_cycle()
    assert bot.snapshots["UNITREEUSDT"] == {"error": "等待首次获取上交所688836收盘价（后台刷新中）", "pending": True}, bot.snapshots
    assert not tg.sent, tg.sent
    bot.stocks.errors["UNITREEUSDT"] = "东方财富: 网络错误"  # the first fetch failed -> now it is a real fault
    await bot.one_cycle()
    assert "尚未由带日期的日 K 确认" in bot.snapshots["UNITREEUSDT"]["error"] and any("行情监控异常" in t for t in tg.sent), tg.sent

    # --- the real run(): reference tasks start with the monitor, alerts flow, everything stops cleanly -----
    bot, tg = make_bot()
    async def tg_call(method, payload=None, timeout=15):
        if method == "getWebhookInfo": return {}
        if method == "getMe": return {"username": "test_bot"}
        if method == "getUpdates": await asyncio.sleep(0.2); return []
        return True
    bot.telegram.call = tg_call
    bot.hsi.refresh = hang
    runner = asyncio.create_task(bot.run())
    for _ in range(40):
        await asyncio.sleep(0.1)
        if [t for t in tg.sent if "上涨超过" in t]: break
    assert [t for t in tg.sent if "上涨超过" in t], tg.sent
    assert len(bot.reference_tasks) == 7 and all(not t.done() for t in bot.reference_tasks)
    bot.stopping.set()
    await asyncio.wait_for(runner, 3)
    assert all(t.done() for t in bot.reference_tasks) and bot.reference_pool._shutdown
    print("REFLOOP_OK")


asyncio.run(run())
