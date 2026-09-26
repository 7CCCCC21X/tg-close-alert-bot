"""A50 proxy sources: Eastmoney quote -> same-contract K line -> Sina CFD, and anchors that never mix contracts."""
import asyncio, sys, json, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D


def bj(mo, d, h, mi): return int(dt.datetime(2026, mo, d, h, mi, tzinfo=m.BEIJING).timestamp() * 1000)


# --- contract codes: any SGX A50 contract (CN...), never another instrument ---------------------------------
assert all(m.a50_code_ok(c) for c in ("CN00Y", "cn2610", "CN00Z", "", None))
assert not any(m.a50_code_ok(c) for c in ("HSI00Y", "000001", "IF00Y"))
assert m.a50_family("东方财富K线") == m.a50_family("东方财富") == "东方财富" and m.a50_family("新浪CFD") == "新浪CFD"
q = m.CnIndex.parse_a50("东方财富", json.dumps({"data": {"f43": 14196.5, "f57": "CN2610", "f60": 14181, "f86": bj(9, 26, 5, 14) // 1000}}).encode(), 0)
assert q.last == D("14196.5") and q.source == "东方财富"
try: m.CnIndex.parse_a50("东方财富", json.dumps({"data": {"f43": 1, "f57": "HSI00Y", "f86": 1}}).encode(), 0); assert False
except ValueError as e: assert "不是 A50" in str(e)
# latest finished bar of the same contract's 1-minute K line
KL = json.dumps({"data": {"code": "CN00Y", "klines": ["2026-09-26 05:14,14190,14192.0", "2026-09-26 05:15,14192,14196.5"]}}).encode()
k = m.CnIndex.parse_a50("东方财富K线", KL, 0)
assert k.last == D("14196.5") and k.quoted_ms == bj(9, 26, 5, 15) and k.prev_close is None and k.source == "东方财富K线", k
for bad in (json.dumps({"data": {"code": "HSI00Y", "klines": ["2026-09-26 05:15,1,2"]}}).encode(),
            json.dumps({"data": {"code": "CN00Y", "klines": []}}).encode(), b"<html>"):
    try: m.CnIndex.parse_a50("东方财富K线", bad, 0); assert False, bad
    except ValueError: pass

NOW = bj(9, 26, 18, 30)  # Saturday evening, A50 closed since 05:15
CFD = 'var hq_str_hf_CHA50CFD="14201.0,,1,1,14250,14100,18:30:00,14181,14190,0,0,0,0,富时A50,2026-09-26";'.encode("gbk")


def feed(quote_ok=True, kline_ok=True, one_min_ok=False, five_min_ok=True, calls=None):
    async def get(url, timeout=15, headers=None):
        if calls is not None: calls.append(url)
        if "push2.eastmoney.com" in url and "104.CN00Y" in url:
            if quote_ok: return json.dumps({"data": {"f43": 14196.5, "f57": "CN00Y", "f60": 14181, "f86": bj(9, 26, 5, 14) // 1000}}).encode()
            raise m.RemoteError("HTTP 403: 访问被拒绝")
        if "lmt=30" in url:
            if kline_ok: return KL
            raise m.RemoteError("网络错误 (TimeoutError)")
        if "klt=1&" in url and "beg=" in url:  # 1-minute history around 09-24
            rows = ["2026-09-24 15:00,14170,14175.0"] if one_min_ok else []
            return json.dumps({"data": {"code": "CN00Y", "klines": rows}}).encode()
        if "klt=5&" in url:
            rows = ["2026-09-24 15:00,14170,14180.0"] if five_min_ok else []
            return json.dumps({"data": {"code": "CN00Y", "klines": rows}}).encode()
        if "hf_CHA50CFD" in url: return CFD
        raise m.RemoteError("offline")
    return get


class FM:
    def now_ms(self): return NOW


def make_bot(store=None):
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "EXCHANGE_TICKERS": "off",
                             "HSI_FUTURES": "off", "KOSPI_INDEX": "off", "HL_TICKERS": "off", "HL_INDEX": "off"})
    bot = m.Bot(cfg, store or m.Store(":memory:"), FM(), None)
    bot.cn.close = m.DailyClose(dt.date(2026, 9, 24), D("3888.37"), D("3936.52"), "腾讯日K", NOW)
    return bot


async def run():
    # 1) Eastmoney quote blocked, its K line answers: same futures -> odds are produced, and the reason is visible
    m.http_get = feed(quote_ok=False)
    bot = make_bot()
    await bot.cn.refresh(NOW, force=True)
    assert bot.cn.a50.source == "东方财富K线" and "东方财富: HTTP 403" in bot.cn.a50_skipped and not bot.cn.a50_error
    await bot.refresh_odds_inputs(NOW)
    assert bot.anchors["A50"][1] == D("14180.0") and bot.a50_anchor_note == "15:00 五分钟K近似" and bot.a50_anchor_source == "东方财富"
    o = bot.sse_odds(NOW)
    assert isinstance(o, m.CloseOdds) and o.ref == D("3888.37") and o.target == dt.date(2026, 9, 28), o
    assert "A50 14,196.5 / 15:00 五分钟K近似 14,180 → +0.116%" in o.proxy_note, o.proxy_note
    line = m.to_html(bot.cn.a50_line(NOW, "cn", bot.anchors["A50"][1], bot.a50_anchor_note))
    assert "上证收盘附近 <b>14,180</b>" in line and "（15:00 五分钟K近似）" in line and "09-26 05:15 东方财富K线" in line, line
    assert "｜⚠️ 前序源未取到：东方财富: HTTP 403" in line and "报价已超" not in line, line

    # 2) only the Sina CFD answers: different instrument -> no odds, and the message says why
    m.http_get = feed(quote_ok=False, kline_ok=False)
    await bot.cn.refresh(NOW, force=True)
    assert bot.cn.a50.source == "新浪CFD" and "东方财富K线: 网络错误" in bot.cn.a50_skipped
    msg = bot.sse_odds(NOW)
    assert msg.startswith("A50 锚点来自东方财富期货，当前只有新浪CFD报价（东方财富未取到：东方财富: HTTP 403") and msg.endswith("不同合约不能混算，暂不输出概率"), msg
    status_line = m.to_html(bot.cn.a50_line(NOW, "cn", None))
    assert "新浪CFD·非交易所合约，仅参考" in status_line and "前序源未取到：东方财富: HTTP 403" in status_line

    # 3) Eastmoney quote works again -> odds straight away, the skipped note disappears
    m.http_get = feed()
    await bot.cn.refresh(NOW, force=True)
    assert bot.cn.a50.source == "东方财富" and not bot.cn.a50_skipped
    assert isinstance(bot.sse_odds(NOW), m.CloseOdds)

    # 4) restart: the anchor and its family come back from the store without refetching
    calls = []
    m.http_get = feed(calls=calls)
    bot2 = make_bot(bot.store); bot2.cn.a50 = bot.cn.a50
    await bot2.refresh_odds_inputs(NOW)
    assert bot2.anchors["A50"] == bot.anchors["A50"] and bot2.a50_anchor_source == "东方财富" and bot2.a50_anchor_note == "15:00 五分钟K近似"
    # only the upgrade attempt (exact 1-minute bar) is made; the stored 5-minute anchor is not refetched
    assert not [u for u in calls if "klt=5&" in u] and [u for u in calls if "klt=1&" in u and "beg=" in u]
    assert isinstance(bot2.sse_odds(NOW), m.CloseOdds)

    # 5) the approximate anchor is upgraded once the 1-minute history is back
    m.http_get = feed(one_min_ok=True)
    bot.anchor_tries.clear()
    await bot.refresh_odds_inputs(NOW)
    assert bot.anchors["A50"][1] == D("14175.0") and bot.a50_anchor_note == "15:00" and bot.store.get("anchor:A50")[2:] == ["15:00", "东方财富"]

    # 6) a CFD first-print anchor is upgraded to the futures' own 5-minute bar (1-minute gone)
    bot3 = make_bot()
    close_ms = bot3.sse_close_ms()
    bot3.anchors["A50"] = (close_ms, D("14190"))
    bot3.a50_anchor_note, bot3.a50_anchor_source = "15:00 后五分钟首笔近似", "新浪CFD"
    m.http_get = feed()
    await bot3.refresh_odds_inputs(NOW)
    assert bot3.anchors["A50"] == (close_ms, D("14180.0")) and bot3.a50_anchor_source == "东方财富", (bot3.anchors, bot3.a50_anchor_source)
    assert bot3.a50_anchor_note == "15:00 五分钟K近似"
    print("A50_OK")


asyncio.run(run())
