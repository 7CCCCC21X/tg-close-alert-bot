import asyncio, sys, time, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
def ms(d, h, mi): return int(dt.datetime(2026, 9, d, h, mi, tzinfo=m.BEIJING).timestamp() * 1000)
# Page shaped like the screenshot (values from etnet 2026-09-23); markup deliberately noisy.
PAGE = """<html><head><script>var x = "恒生指數期貨(09/2026) 日市 99,999 +1 (+1%)";</script><style>.a{}</style></head><body>
<select><option>恒生指數期貨(09/2026)</option><option>恒生指數期貨(10/2026)</option></select>
<div class="col"><h3>恒生指數期貨(09/2026)</h3><span class="tag">日市</span>
<div><span class="down">&#9660;24,822</span> <span>-233</span> <span>(-0.93%)</span> <span>低水12</span></div>
<table><tr><td>最高:</td><td>25,134</td><td>最低:</td><td>24,802</td></tr>
<tr><td>前收市:</td><td>25,055</td><td>開市:</td><td>25,130</td></tr></table>
<div>HSI FHSI 2026/09/23 15:59 etnet.com.hk &copy; copyright</div></div>
<div class="col"><h3>恒生指數期貨(09/2026)</h3><span>夜市</span>
<div>&#9650;25,133 +78 (+0.31%) 高水45</div><div>最高: 25,304 最低: 25,054 前收市: 25,055 開市: 25,072</div>
<div>HSI FHSI 2026/09/23 02:59</div></div>
<div>未平倉 (到期日：29/09/2026)</div>
<div>恒生指數現貨 &#9660;24,834.12 -253.63 (-1.01%) 最高: 25,072.25 最低: 24,811.85 前收市: 25,087.75 開市: 25,064.84</div>
<div>15分鐘時段記錄(日市)</div></body></html>"""
q = m.parse_etnet_futures(PAGE.encode("utf-8"), ms(23, 16, 40))
assert q.name == "恒指期货(09/2026)日市" and q.last == D("24822") and q.prev_settle == D("25055") and q.change == D("-233"), q
assert q.open == D("25130") and q.high == D("25134") and q.low == D("24802") and q.quoted_ms == ms(23, 15, 59), q
assert q.spot == D("24834.12") and q.spot_prev == D("25087.75") and q.water == D("-12") and q.basis == D("-12") and q.exchange_contract, q
# during the night session the newer night block wins
night = PAGE.replace("2026/09/23 02:59", "2026/09/23 20:05")
qn = m.parse_etnet_futures(night.encode("utf-8"), ms(23, 20, 6))
assert qn.name.endswith("夜市") and qn.last == D("25133") and qn.basis == D("45"), qn
# big5 page and unchanged price (no sign)
flat = PAGE.replace("&#9660;24,822</span> <span>-233</span> <span>(-0.93%)</span> <span>低水12", "24,822</span> <span>0</span> <span>(0.00%)</span> <span>平水")
qf = m.parse_etnet_futures(flat.encode("big5hkscs", errors="ignore"), 0)
assert qf.last == D("24822") and qf.basis == 0, qf
try: m.parse_etnet_futures(b"<html>maintenance</html>", 0); assert False
except ValueError as e: assert "etnet" in str(e)
# line: matches etnet numbers exactly
h = m.IndexFutures(); h.quote = q
line = h.line(ms(23, 16, 40), "cn")
assert line == ("📈 " + m.bold("恒指期货 日市（已收市）") + " " + m.bold("24,822") + " → 恒指收盘 " + m.bold("24,834.12") + " 🟢 -0.05%（低水 12）"
                "｜恒指当日 🟢 -1.01%｜期货前收 " + m.bold("25,055") + " 🟢 -0.93%（-233）｜09-23 15:59 etnet"), line
# Sina CFD fallback is labelled
sina = 'var hq_str_hf_HSI="24818.880,,24818,24819,25300,24814,16:40:05,25050.880,25119,0,0,0,0,恒生指数期货,2026-09-23";'.encode("gbk")
h.quote = m.IndexFutures.parse_futures("新浪CFD", sina, 0)
assert not h.quote.exchange_contract and h.line(ms(23, 16, 40), "cn").endswith("新浪CFD·非港交所合约，仅参考"), h.line(ms(23, 16, 40), "cn")
# --- the live page draws block times inside the chart images: no timestamps in the text ------------------
# Saturday 2026-09-26 18:30 (the user's screenshot): 09/25 day block and the night that ended Sat 03:00.
LIVE = """<div><h3>恒生指數期貨(09/2026)</h3><span>日市</span><div>&#9660;24,522 -169 (-0.68%) 高水12</div>
<div>最高: 24,643 最低: 24,265 前收市: 24,691 開市: 24,630</div><img alt="chart"></div>
<div><h3>恒生指數期貨(09/2026)</h3><span>夜市</span><div>&#9660;24,501 -21 (-0.09%) 低水9</div>
<div>最高: 24,582 最低: 24,451 前收市: 24,522 開市: 24,528</div><img alt="chart"></div>
<div>未平倉 (到期日：29/09/2026)</div>
<div>恒生指數現貨 &#9660;24,510.09 -251.04 (-1.01%) 最高: 24,537.14 最低: 24,275.56 前收市: 24,761.13 開市: 24,523.50</div>"""
hk = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x"}).holidays["hk"]
sat = ms(26, 18, 30)
ql = m.parse_etnet_futures(LIVE.encode("utf-8"), sat, hk)
assert ql.session == "夜市" and ql.last == D("24501") and ql.prev_settle == D("24522") and ql.water == D("-9"), ql
assert ql.quoted_ms == ms(26, 3, 0), m.stamp(ql.quoted_ms)  # when Friday's night session ended, not "now"
assert m.hk_futures_session(sat) == "休市" and m.hk_futures_session(ms(26, 2, 0)) == "夜市" and m.hk_futures_session(ms(27, 20, 0)) == "休市"
hx = m.IndexFutures(True, hk); hx.quote = ql
assert m.to_html(hx.line(sat, "cn")).startswith("📈 <b>恒指期货 夜市（已收市）</b> <b>24,501</b> → 恒指收盘 <b>24,510.09</b> 🟢 -0.04%（低水 9）"), hx.line(sat, "cn")
assert "｜09-26 03:00 etnet" in hx.line(sat, "cn")
# during the day session (Monday 10:00) last night's block shares the day block's 前收市 -> day block is newer
MON = LIVE.replace("24,522 -169 (-0.68%) 高水12", "24,600 +78 (+0.32%) 高水5").replace("前收市: 24,691", "前收市: 24,522")
qd = m.parse_etnet_futures(MON.encode("utf-8"), ms(28, 10, 0), hk)
assert qd.session == "日市" and qd.last == D("24600") and qd.quoted_ms == ms(28, 10, 0), qd
# Friday 17:05, before the night opens: the night block is still Thursday's (前收市 = 09-24 close 24,691)
FRI = LIVE.replace("前收市: 24,522 開市: 24,528", "前收市: 24,691 開市: 24,700")
qf2 = m.parse_etnet_futures(FRI.encode("utf-8"), ms(25, 17, 5), hk)
assert qf2.session == "日市" and qf2.last == D("24522") and qf2.quoted_ms == ms(25, 16, 30), (qf2, m.stamp(qf2.quoted_ms))
# Friday 20:00 during the night: the night block (前收市 = today's day close) is live
qn2 = m.parse_etnet_futures(LIVE.encode("utf-8"), ms(25, 20, 0), hk)
assert qn2.session == "夜市" and qn2.quoted_ms == ms(25, 20, 0)
# the probability no longer double-counts the day move: anchor = day close 24,522, not the 09-24 close 24,691
class _M:
    def now_ms(self): return sat
bot = m.Bot(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "HK0625USDT", "KOSPI_INDEX": "off", "SSE_INDEX": "off"}),
            m.Store(":memory:"), _M(), None)
bot.hsi.quote = ql
o = bot.hsi_odds(sat)
assert isinstance(o, m.CloseOdds) and o.ref == D("24510.09") and o.target == dt.date(2026, 9, 28), o
assert abs(float(o.effective) - 24510.09 * 24501 / 24522) < 1e-6 and "恒指期货 24,501 / 日市收市 24,522 → -0.086%" in o.proxy_note, o
assert 0.45 < o.fair_up < 0.5, o.fair_up  # a -0.09% move is close to a coin flip, not 23.5¢

async def run():
    calls = []
    async def fake_get(url, timeout=15, headers=None):
        calls.append(url)
        if "etnet" in url: return PAGE.encode("utf-8")
        raise AssertionError("spot must not be fetched separately: " + url)
    m.http_get = fake_get
    x = m.IndexFutures(); await x.refresh(ms(23, 16, 40))
    assert x.quote.source == "etnet" and x.quote.spot == D("24834.12") and len(calls) == 1 and not x.error, (calls, x.error)
    print("ETNET_OK")
asyncio.run(run())
