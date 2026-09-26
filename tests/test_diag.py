"""/diag and --diag: every source probed separately, failures say whether the request or the content broke."""
import asyncio, sys, time, json, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D


def bj(mo, d, h, mi): return int(dt.datetime(2026, mo, d, h, mi, tzinfo=m.BEIJING).timestamp() * 1000)


NOW = bj(9, 26, 18, 30)
KL5 = json.dumps({"data": {"code": "CN00Y", "klines": ["2026-09-24 14:55,1,14170", "2026-09-24 15:00,14170,14180.0"]}}).encode()
TX_SSE = ('v_sh000001="1~上证指数~000001~3888.37~3936.52~3930~' + "~".join(["0"] * 24) + '~20260924161400~x";').encode("gbk")
DAILY = json.dumps({"data": {"sh000001": {"day": [["2026-09-23", "0", "3936.52"], ["2026-09-24", "0", "3888.37"]]}}}).encode()


async def fake_get(url, timeout=15, headers=None):
    if "fapi" in url: raise AssertionError("Binance goes through market.get")
    if "push2.eastmoney.com" in url and "104.CN00Y" in url:  # the A50 quote answers with an unexpected code
        return json.dumps({"data": {"f43": 14196.5, "f57": "XX00Y", "f58": "某合约", "f86": NOW // 1000 - 50000}}).encode()
    if "lmt=30" in url:
        return json.dumps({"data": {"code": "CN00Y", "klines": ["2026-09-26 05:15,14192,14196.5"]}}).encode()
    if "klt=1&" in url and "beg=" in url:
        return json.dumps({"data": {"code": "CN00Y", "klines": []}}).encode()  # 1-minute history gone
    if "klt=5&" in url: return KL5
    if "hf_CHA50CFD" in url: raise m.RemoteError("网络错误 (URLError: timed out)")
    if "qt.gtimg.cn/q=sh000001" in url: return TX_SSE
    if "hq.sinajs.cn/list=sh000001" in url: return b'<html><body>Forbidden by policy</body></html>'
    if "fqkline" in url and "sh000001" in url: return DAILY
    if "secid=1.000001" in url and "kline" in url: raise m.RemoteError("HTTP 403: 访问被拒绝")
    if "secid=1.000001" in url: await asyncio.sleep(60)  # hangs -> per-probe timeout
    raise m.RemoteError("offline")


class FM(m.Binance):
    def now_ms(self): return NOW
    async def get(self, path, **p):
        if path == "/fapi/v1/time": return {"serverTime": int(time.time() * 1000) + 1200}
        raise m.RemoteError("HTTP 451: 部署所在地或接口访问受限")
    async def prices(self):
        return {"UNITREEUSDT": {"symbol": "UNITREEUSDT", "price": "73.5", "time": NOW}}


class TG:
    def __init__(self): self.sent = []
    async def call(self, *a, **k): return True
    async def send(self, chat, thread, text, reply_markup=None, parse_mode=None): self.sent.append(text)


async def run():
    m.http_get = fake_get
    async def no_json(*a, **k): raise m.RemoteError("网络错误 (URLError: [Errno -2] Name or service not known)")
    m.http_json = no_json
    m.DIAG_TIMEOUT = 0.5
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ADMIN_USER_ID": "42", "SYMBOLS": "UNITREEUSDT",
                             "EXCHANGE_TICKERS": "UNITREEUSDT=sh:688836", "HSI_FUTURES": "off", "KOSPI_INDEX": "off",
                             "HL_TICKERS": "UNITREEUSDT=xyz:UNITREE", "HL_INDEX": "off"})
    tg = TG(); bot = m.Bot(cfg, m.Store(":memory:"), FM(cfg), tg)
    bot.cn.close = m.DailyClose(dt.date(2026, 9, 24), D("3888.37"), D("3936.52"), "腾讯日K", NOW)
    results = await bot.diagnose()
    by = {(r.group, r.name.split(" ")[0]): r for r in results}
    groups = {r.group for r in results}
    assert groups == {"币安", "交易所收盘·UNITREE", "上证实时", "上证日K", "A50实时", "A50锚点", "Hyperliquid", "汇率"}, groups

    assert by[("币安", "服务器时间")].ok and "+1.2 秒" in by[("币安", "服务器时间")].detail
    assert by[("币安", "合约最新价")].detail == "UNITREE 73.5"
    # A50 quote rejected: the exact fields are shown so the cause is obvious
    a50 = by[("A50实时", "东方财富")]
    assert not a50.ok and "不是 A50 合约" in a50.detail and "f57=XX00Y" in a50.detail and "f58=某合约" in a50.detail, a50
    kl = by[("A50实时", "东方财富K线")]
    assert kl.ok and kl.detail.startswith("14,196.5｜09-26 05:15（休市）"), kl
    assert by[("A50实时", "新浪CFD")].detail == "请求失败：网络错误 (URLError: timed out)"
    one, five = by[("A50锚点", "东方财富1分钟K")], by[("A50锚点", "东方财富5分钟K")]
    assert not one.ok and "没有 2026-09-24 15:00 这一根（返回 0 根：无数据）" in one.detail, one
    assert five.ok and five.detail == "09-24 15:00 收 14,180（返回 2 根：09-24 14:55～09-24 15:00）", five
    assert by[("上证实时", "腾讯")].ok and "3,888.37（昨收 3,936.52）｜09-24 16:14｜⚠️ 非今日数据" in by[("上证实时", "腾讯")].detail
    sina = by[("上证实时", "新浪")]
    assert not sina.ok and "内容异常" in sina.detail and "返回：Forbidden by policy" in sina.detail, sina  # the body is shown
    assert by[("上证实时", "东方财富")].detail == "请求超时（>0.5 秒）"
    assert by[("上证日K", "腾讯日K")].detail == "最新完结 09-24 收盘 3,888.37（共 2 根）"
    assert "HTTP 403" in by[("上证日K", "东方财富日K")].detail
    assert "Name or service not known" in by[("Hyperliquid", "dex")].detail and "Name or service" in by[("汇率", "Frankfurter")].detail

    # report: groups that lost every source are called out first
    text = m.Bot.diag_text(results, bot.diag_state(NOW), "T")
    assert "🚨 整组全部失败（该数据当前拿不到）：交易所收盘·UNITREE、Hyperliquid、汇率" in text, text
    partial = [l for l in text.splitlines() if l.startswith("⚠️ 其余失败的源")][0]
    assert partial == ("⚠️ 其余失败的源（同组有别的源顶上）：上证实时·新浪、上证实时·东方财富、上证日K·东方财富日K、"
                       "A50实时·东方财富、A50实时·新浪CFD、A50锚点·东方财富1分钟K 09-24 15:00、A50锚点·新浪5分钟K 09-24 15:00"), partial
    assert "【A50实时】\n❌ 东方财富（" in text and "🔄 后台刷新：未启动" in text and "📌 当前使用中的数据" in text

    # every source down -> points at the deployment's network, not at the feeds
    down = [m.ProbeResult("币安", "x", False, 1, "请求失败：网络错误 (URLError: Tunnel connection failed: 403 Forbidden)"),
            m.ProbeResult("汇率", "y", False, 1, "请求失败：网络错误 (URLError: Tunnel connection failed: 403 Forbidden)")]
    t2 = m.Bot.diag_text(down, [], "T")
    assert "🚨 所有数据源都失败：多半是部署环境本身连不上外网" in t2 and "Tunnel connection failed: 403 Forbidden" in t2 and "整组全部失败" not in t2

    # background refresh health shows up in the state part
    bot.reference_state = {"上证/A50": {"ok_at": time.time() - 12, "error": "", "error_at": 0, "ms": 1500, "runs": 40},
                           "交易所收盘": {"ok_at": 0, "error": "TimeoutError", "error_at": time.time(), "ms": 300000, "runs": 2}}
    st = "\n".join(bot.diag_state(NOW))
    assert "✅ 上证/A50：12 秒前成功｜上次用时 1.5s｜共 40 轮" in st and "❌ 交易所收盘：从未成功｜上次用时 300.0s｜共 2 轮｜最近错误：TimeoutError" in st, st
    assert "⏳ 汇率：尚未运行" in st and "上证收盘：09-24 3,888.37（腾讯日K）" in st, st

    # /diag from Telegram (admin only): a "working on it" note, then the report
    await bot.process_message({"text": "/diag", "chat": {"id": 1}, "from": {"id": 42}, "date": time.time()})
    assert tg.sent[0].startswith("🩺 正在逐个检测数据源") and tg.sent[-1].startswith(f"🩺 数据源检测 v{m.VERSION}"), tg.sent
    assert "【A50锚点】" in tg.sent[-1]
    n = len(tg.sent)
    await bot.process_message({"text": "/diag", "chat": {"id": 1}, "from": {"id": 7}, "date": time.time()})
    assert len(tg.sent) == n, "non-admins must not trigger probes"

    # the raw network reason survives into RemoteError (was just "URLError" before)
    import urllib.error, urllib.request
    saved = urllib.request.urlopen
    def refuse(*a, **k): raise urllib.error.URLError("Tunnel connection failed: 403 Forbidden")
    urllib.request.urlopen = refuse
    try: m._http_get("https://example.invalid/"); assert False
    except m.RemoteError as e: assert str(e) == "网络错误 (URLError: Tunnel connection failed: 403 Forbidden)", e
    urllib.request.urlopen = saved
    # GBK answers (Sina/Tencent) are shown readably, not as mojibake
    assert "富时中国A50期货" in m.raw_snippet('var hq_str_hf_CHA50CFD="1,,2026-09-26,富时中国A50期货";'.encode("gbk"))
    assert m.raw_snippet("上证指数".encode("utf-8")) == "上证指数"
    print("DIAG_OK")


asyncio.run(run())
