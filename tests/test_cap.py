import asyncio, os, re, sys, math, time, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
NIU, ANSEM, PONS, MEME, CASHCAT, AI, STONK, STONKBROKER = m.CAP_MARKETS
m.CapMarket.GECKO_GAP = 0  # request spacing is tested on its own below
TOKEN = NIU.token

# --- the market as its rules state it ----------------------------------------------------------------------------
assert NIU.start_ms == int(dt.datetime(2026, 8, 17, 3, 30, tzinfo=dt.timezone.utc).timestamp() * 1000)   # 08-16 23:30 EDT
assert NIU.end_ms == int(dt.datetime(2026, 11, 1, 3, 59, tzinfo=dt.timezone.utc).timestamp() * 1000)     # 10-31 23:59 EDT
assert TOKEN == TOKEN.lower() and NIU.targets == (D("2e8"), D("3e8"), D("5e8"), D("1e9"))

# --- helpers ------------------------------------------------------------------------------------------------------
assert m.cap_target("$200M") == D("2e8") and m.cap_target("↑ $1B") == D("1e9") and m.cap_target("Will it hit $1.5B?") == D("1.5e9")
assert m.cap_target("500m") == D("5e8") and m.cap_target("Yes") is None
assert m.usd_short(D("83200000")) == "$83.2M" and m.usd_short(1e9) == "$1B" and m.usd_short(245_600_000) == "$246M" and m.usd_short(None) == "—"
# one-touch probability: certain once touched, zero without time, rises with σ and falls with distance
assert m.hit_probability(250, 200, 1.0, 0.1) == 1.0 and m.hit_probability(80, 200, 1.0, 0) == 0.0
p1, p2, p3 = m.hit_probability(80, 200, 2.5, 0.09), m.hit_probability(80, 200, 1.5, 0.09), m.hit_probability(80, 300, 2.5, 0.09)
assert 0.1 < p1 < 0.16 and p2 < p1 and p3 < p1, (p1, p2, p3)
# reflection: for a driftless log price, P(touch) ≈ 2·P(end above) when the barrier is near
s = 0.3
assert abs(m.hit_probability(100, 100.0001, s, 1) - 1) < 1e-3

# DexScreener: both answer shapes; only pairs where the token is the base; the most liquid one wins
pair = lambda base, price, liq, cap=None, dex="pancakeswap": {"dexId": dex, "baseToken": {"address": base}, "priceUsd": price,
                                                               "liquidity": {"usd": liq}, "marketCap": cap, "fdv": cap}
answer = [pair("0xother", "5", 9e9), pair(TOKEN.upper().replace("0X", "0x"), "0.083", 1e6, 83_000_000, "v2"),
          pair(TOKEN, "0.084", 3e6, 84_000_000), {"baseToken": {"address": TOKEN}, "priceUsd": "x"}]
assert m.dex_price(answer, TOKEN) == (D("0.084"), D("84000000"), D("84000000"), "pancakeswap")
assert m.dex_price({"schemaVersion": "1.0.0", "pairs": answer}, TOKEN)[0] == D("0.084")
for bad in ([], {"pairs": None}, [pair("0xother", "1", 1)]):
    try: m.dex_price(bad, TOKEN); assert False, bad
    except ValueError: pass
# GeckoTerminal
pools = {"data": [{"attributes": {"address": "0xPOOLA", "reserve_in_usd": "1000"}},
                  {"attributes": {"address": "0xPOOLB", "reserve_in_usd": "250000.5"}}, {"attributes": {}}]}
assert m.gecko_pool(pools) == "0xPOOLB"  # kept as given: Solana addresses are case-sensitive
assert m.gecko_pool(pools, "0XPOOLA") == "0xPOOLA" and m.gecko_pool(pools, "0xnone") == "0xPOOLB"  # the rules' pair wins, spelt either way
bars = {"data": {"attributes": {"ohlcv_list": [[7200, "1", "2", "0.5", "1.5", "9"], [3600, 1, 1.2, 0.9, 1.1, 3], ["x"]]}}}
assert m.gecko_bars(bars) == [(3600, 1.0, 1.2, 0.9, 1.1), (7200, 1.0, 2.0, 0.5, 1.5)]
assert m.gecko_bars({"data": {}}) == [] and m.gecko_bars(None) == []
assert m.rpc_uint({"jsonrpc": "2.0", "id": 1, "result": "0x3b9aca00"}) == 10 ** 9 and m.rpc_uint({"result": "0x"}) == 0
try: m.rpc_uint({"error": {"code": -32000, "message": "execution reverted"}}); assert False
except ValueError as error: assert "execution reverted" in str(error)

# ANSEM (Solana / pump.fun) and PONS (Robinhood chain, settled on its DexScreener pair), windows in EDT
assert ANSEM.start_ms == int(dt.datetime(2026, 8, 17, 17, 0, tzinfo=dt.timezone.utc).timestamp() * 1000) and ANSEM.chain == "solana"
assert ANSEM.token == "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump" and ANSEM.supply == "fdv" and ANSEM.gecko == "solana"
assert PONS.start_ms == int(dt.datetime(2026, 8, 31, 10, 0, tzinfo=dt.timezone.utc).timestamp() * 1000) and PONS.chain == "robinhood"
assert PONS.pair == "0x10cc6bd38112cac182db90b6a71d8bb5939526ba" and PONS.gecko == "" and ANSEM.end_ms == PONS.end_ms == NIU.end_ms
# MEME, CASHCAT and AI: Robinhood chain, settled on the rules' DexScreener pairs, their levels read from Predict (none listed)
for cs, c_start, c_pair, c_token in ((MEME, dt.datetime(2026, 9, 4, 10, 0, tzinfo=dt.timezone.utc), "0xc6e298e137f2905398db87e6eae49ede64d231fee37330fa433fec917f4618b6",
                                  "0x385F4f8ae47651ce5F58F5265395a669f8281e18"),
                                 (CASHCAT, dt.datetime(2026, 9, 2, 5, 0, tzinfo=dt.timezone.utc), "0xa70fc67c9f69da90b63a0e4c05d229954574e313",
                                  "0x020bfC650A365f8BB26819deAAbF3E21291018b4"),
                                 (AI, dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.timezone.utc), "0xcbdfea90430a30ee4469c9902e120a77e7c7e4711d5643671c1d1957f2f1ce27",
                                  "0x2E8c31162b855A2ffa90F6F8634643Ad6F111e18")):
    assert cs.start_ms == int(c_start.timestamp() * 1000) and cs.end_ms == PONS.end_ms and cs.pair == c_pair and cs.token == c_token, cs.key
    assert cs.chain == "robinhood" and cs.supply == "fdv" and cs.gecko == "" and cs.metric == "FDV" and cs.settle == "DexScreener"
    assert cs.targets == () and cs.name == f"${cs.key} FDV" and cs.slug.startswith(f"what-fdv-will-{cs.key.lower()}-hit-before-")
# STONK (Solana, settled on its DexScreener STONK/SOL pair; bars from the mint's most liquid GeckoTerminal pool, since the
# pair is spelt in lower case) and STONKBROKER (Robinhood chain, no bars): levels read from Predict
assert STONK.start_ms == int(dt.datetime(2026, 9, 6, 8, 0, tzinfo=dt.timezone.utc).timestamp() * 1000) and STONK.end_ms == PONS.end_ms
assert STONK.chain == "solana" and STONK.pair == "afrddtgywcveqb1gxcahr8i48o6qtxyqksdvkeludehg" and STONK.gecko == "solana"
assert all(s.gecko_pool == "" for s in m.CAP_MARKETS) and [s.key for s in m.CAP_MARKETS if not s.gecko] == ["PONS", "MEME", "CASHCAT", "AI", "STONKBROKER"]
assert STONK.token == "6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rUJHacpMpUNgx" and STONK.supply == "fdv" and STONK.targets == ()
assert STONK.name == "$STONK FDV" and STONK.slug == "what-fdv-will-stonk-hit-before-november-2026" and STONK.settle == "DexScreener"
assert STONKBROKER.start_ms == int(dt.datetime(2026, 9, 1, 7, 45, tzinfo=dt.timezone.utc).timestamp() * 1000) and STONKBROKER.end_ms == PONS.end_ms
assert STONKBROKER.chain == "robinhood" and STONKBROKER.pair == "0xd33c8fd38b06e989cdbd4dffdefab71c4bdd415b24964c8d69e38ff35b068f92"
assert STONKBROKER.token == "0xe934e36A439C94017B64a3FecE66AF12099aBF50" and STONKBROKER.supply == "fdv" and STONKBROKER.gecko == ""
assert STONKBROKER.name == "$STONKBROKER FDV" and STONKBROKER.slug == "what-fdv-will-stonkbroker-hit-by-november-2026" and STONKBROKER.targets == ()
assert len({s.key for s in m.CAP_MARKETS}) == 8 and len({s.slug for s in m.CAP_MARKETS}) == 8
# a named pair's answer ({"pair": {...}}), the base matched without regard to case, FDV kept apart from market cap
single = {"schemaVersion": "1.0.0", "pair": {"dexId": "uniswap", "baseToken": {"address": PONS.token.lower()}, "priceUsd": "0.8",
                                           "liquidity": {"usd": 5e5}, "marketCap": 6e8, "fdv": 7.2e8}}
assert m.dex_price(single, PONS.token) == (D("0.8"), D("6E+8"), D("7.2E+8"), "uniswap")

H = 3600
NOW = int(dt.datetime(2026, 9, 29, 12, 10, tzinfo=m.BEIJING).timestamp() * 1000)
NOW_S = NOW // 1000
START_S = NIU.start_ms // 1000
FIRST_HOUR = START_S - START_S % H            # 03:00 UTC: the window opens half way through it


def world(peak_hour=None, peak_price=0.25, first_minutes=None):
    """A fake of the four services: price 0.083 USD, 1e9 supply, 1.5e7 burned (so 9.85e8 circulate)."""
    calls = []

    async def get(url, payload=None):
        calls.append((url, payload))
        if "dexscreener" in url:
            return [pair(TOKEN, "0.083", 2e6, 83_000_000)]
        if payload is not None:  # BSC eth_call
            data = payload["params"][0]["data"]
            assert payload["method"] == "eth_call" and payload["params"][0]["to"] == TOKEN
            if data == "0x313ce567": return {"result": hex(18)}
            if data == "0x18160ddd": return {"result": hex(10 ** 9 * 10 ** 18)}
            if data.startswith("0x70a08231") and data.endswith("dead"): return {"result": hex(15_000_000 * 10 ** 18)}
            if data.startswith("0x70a08231"): return {"result": "0x0"}
            raise AssertionError(data)
        if url.endswith("/pools?page=1"):
            return pools
        assert "/pools/0xPOOLB/ohlcv/" in url and f"token={TOKEN}" in url and "currency=usd" in url, url
        q = dict(part.split("=") for part in url.split("?")[1].split("&"))
        before, limit = int(q["before_timestamp"]), int(q["limit"])
        if "/ohlcv/minute" in url:
            rows = first_minutes if first_minutes is not None else [
                [FIRST_HOUR + i * 60, 0.05, 0.3 if i < 30 else 0.06, 0.05, 0.05, 1] for i in range(60)]  # 0.3 before the opening only
            return {"data": {"attributes": {"ohlcv_list": [r for r in rows if r[0] < before][-limit:][::-1]}}}
        hours = range(FIRST_HOUR - 800 * H, NOW_S, H)  # up to the running hour
        rows = [[t, 0.08, peak_price if t == peak_hour else 0.09 + (t // H % 2) * 0.001, 0.07, 0.08 * (1.02 if t // H % 2 else 1), 5]
                for t in hours if t < before]
        return {"data": {"attributes": {"ohlcv_list": rows[-limit:][::-1]}}}
    return get, calls


async def run():
    # --- CapMarket: price, supply (total − burned), σ, and the window's high --------------------------------------
    store = m.Store(":memory:")
    cap = m.CapMarket(store, NIU)
    peak = FIRST_HOUR + 10 * 24 * H  # a spike to 0.25 USD ten days in
    cap.get, calls = world(peak_hour=peak)
    await cap.refresh(NOW)
    assert cap.error == "", cap.error
    assert cap.price == D("0.083") and cap.supply == D("985000000") and cap.cap == D("0.083") * D("985000000"), (cap.price, cap.supply)
    assert cap.sigma and 0.5 < cap.sigma < 3 and cap.sigma_note == "30 日小时收盘", (cap.sigma, cap.sigma_note)
    hist = cap.history
    assert hist["first"] == "done" and hist["high"] == 0.25 and hist["at"] == peak, hist  # 0.3 before 03:30 does not count
    assert hist["through"] == NOW_S - NOW_S % H, hist  # every finished hour read; the running one is not persisted
    high, at = cap.window_high()
    assert high == D("0.25") * D("985000000") and at == peak
    # thresholds below the high are settled; the others priced by the model
    assert cap.probability(D("2e8"), NOW) == 1.0 and 0 < cap.probability(D("5e8"), NOW) < 0.5
    assert cap.probability(D("1e9"), NOW) < cap.probability(D("5e8"), NOW)
    # the next scan reads one page and only new hours
    n = len(calls); cap.times["scan"] = -1e9
    await cap.scan(NOW + 2 * H * 1000)
    assert sum("/ohlcv/hour" in u for u, _ in calls[n:]) == 1 and not any("/ohlcv/minute" in u for u, _ in calls[n:])
    assert cap.history["through"] == (NOW_S - NOW_S % H) + H  # the fake's last bar (the hour running at NOW) has finished
    # nothing to scan before the window opens
    early = m.CapMarket(m.Store(":memory:"), NIU); early.get, _ = world()
    await early.scan(NIU.start_ms - 60_000); assert early.history == {}

    # paging back through more than 1000 hours when the store is empty (the window is ~1030 hours old here)
    deep = m.CapMarket(m.Store(":memory:"), NIU)
    deep.get, dcalls = world(peak_hour=FIRST_HOUR + 5 * H, peak_price=0.4)
    await deep.scan(NOW)
    assert sum("/ohlcv/hour" in u for u, _ in dcalls) == 2 and deep.history["high"] == 0.4, deep.history

    # the first partial hour's minutes are unavailable: counted as skipped, not guessed
    skip = m.CapMarket(m.Store(":memory:"), NIU); skip.get, _ = world(first_minutes=[])
    await skip.scan(NOW); assert skip.history["first"] == "skipped"
    assert skip.coverage() == "开窗那一小时的分钟 K 未取得"  # an incomplete history never settles "not reached"

    # --- after the window: its last hour (it ends with the window's last minute) counts, later hours never do ----------
    final_hour = NOW_S - NOW_S % H - 3 * H
    ended = m.dataclasses.replace(NIU, slug="cap-ended", end_ms=(final_hour + 59 * 60) * 1000)  # a "23:59" end
    done = m.CapMarket(m.Store(":memory:"), ended); done.get, _ = world(peak_hour=final_hour + H, peak_price=0.9)  # spike after
    assert done.window_end_s == final_hour + H
    await done.scan(NOW)
    assert done.history["through"] == done.window_end_s and done.history["high"] < 0.1 and done.coverage() == "", done.history
    inside = m.CapMarket(m.Store(":memory:"), ended); inside.get, _ = world(peak_hour=final_hour, peak_price=0.3)
    await inside.scan(NOW)
    assert inside.history["high"] == 0.3 and inside.history["at"] == final_hour and inside.coverage() == ""
    # a price read after the window is not part of its high (nor is the running hour of a later day)
    done.price, done.priced_ms, done.supply, done.sigma = D("0.9"), NOW, D("985000000"), 1.0
    assert done.window_high()[0] < D("0.1") * D("985000000") and done.probability(D("5e8"), NOW) == 0.0
    done.priced_ms = ended.end_ms + 30_000  # read during the window's last minute: it counts
    assert done.window_high()[0] == D("0.9") * D("985000000")
    # without any GeckoTerminal source (PONS) the history is only what the bot saw: never complete
    assert m.CapMarket(m.Store(":memory:"), PONS).coverage() == "没有 K 线来源：窗口最高只含机器人看到的价格"

    # a failing supply RPC falls back to DexScreener's market cap; the error is shown
    fb = m.CapMarket(m.Store(":memory:"), NIU)
    base_get, _ = world()
    async def no_rpc(url, payload=None):
        if payload is not None: raise m.RemoteError("网络错误 (URLError)")
        return await base_get(url, payload)
    fb.get = no_rpc
    await fb.refresh(NOW)
    assert fb.supply is None and fb.cap == D("83000000") and "供应量" in fb.error, (fb.cap, fb.error)

    # --- Predict: every market of the ladder, oriented to "Yes" ----------------------------------------------------
    class FakeMarket:
        def now_ms(self): return NOW
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off"})
    bot = m.Bot(cfg, m.Store(":memory:"), FakeMarket(), None)
    assert bot.predict_targets(NOW)["NIULAI"] == NIU.slug and "NIULAI" in bot.predict.ladder_keys
    assert {"MEME", "CASHCAT", "AI", "STONK", "STONKBROKER"} <= bot.predict.ladder_keys and bot.predict_targets(NOW)["STONK"] == STONK.slug
    meme = bot.cap_payload(bot.caps["MEME"], NOW)["ladder"]  # no levels of its own: the card waits for Predict's titles
    assert meme["rows"] == [] and meme["waiting"] == "等待 Predict 档位", meme
    markets = [("11", "$200M", "Yes"), ("12", "$300M", "No"), ("13", "$500M", "Yes"), ("14", "$1B", "Yes"), ("15", "Other", "Yes")]

    async def fetch(url, payload=None):
        if url == m.PREDICT_GRAPHQL:
            v = payload["variables"]
            if "id" in v:
                return {"data": {"category": {"id": "90", "__typename": "MultiCategory"} if v["id"] == NIU.slug else None}}
            return {"data": {"markets": {"edges": [{"node": {"id": i, "conditionId": "0x" + i, "title": t, "question": t}} for i, t, _ in markets]}}}
        for i, t, first in markets:
            if url.endswith(f"/markets/{i}"):
                names = [first, "No" if first == "Yes" else "Yes"]
                return {"data": {"id": i, "status": "OPEN", "outcomes": [{"name": names[0], "indexSet": 1}, {"name": names[1], "indexSet": 2}]}}
        if url.endswith("/markets/14/orderbook") or url.endswith("/markets/0x14/orderbook"):
            raise m.RemoteError("HTTP 404: 接口请求失败")
        for i, _, _ in markets:
            if url.endswith(f"/markets/{i}/orderbook"):
                return {"data": {"bids": [["0.10", "100"]], "asks": [["0.14", "50"]]}}
        if "/categories/" in url:
            raise m.RemoteError("HTTP 404: 接口请求失败")
        raise AssertionError(url)
    bot.predict.fetch = fetch
    await bot.predict.refresh({"NIULAI": NIU.slug}, force=True)
    rows = bot.predict.ladders["NIULAI"]
    assert [r.target for r in rows] == [D("2e8"), D("3e8"), D("5e8"), D("1e9")], rows  # "Other" has no threshold: left out
    assert rows[3].book is None and "404" in rows[3].error and "NIULAI" not in bot.predict.errors
    yes, why = bot.predict.yes_book(rows[0])
    assert why == "" and yes.bid == (D("0.10"), D("100"))
    flipped, _ = bot.predict.yes_book(rows[1])  # "No" listed first: the book is No's, Yes = 1 − it
    assert flipped.bid == (D("0.86"), D("50")) and flipped.ask == (D("0.90"), D("100")), flipped
    bot.predict.market_meta["13"] = ({"outcomes": ["Up", "Down"]}, time.monotonic())
    assert bot.predict.yes_book(rows[2])[0] is None and "方向未确认" in bot.predict.yes_book(rows[2])[1]

    # --- the web item ------------------------------------------------------------------------------------------------
    bot.caps["NIULAI"] = cap
    bot.predict.ladders["NIULAI"] = [m.dataclasses.replace(r, book=m.dataclasses.replace(r.book, fetched_ms=NOW)) if r.book else r
                                     for r in bot.predict.ladders["NIULAI"]]  # fresh on the fake clock
    item = bot.cap_payload(cap, NOW)
    assert item["kind"] == "ladder" and item["group"] == "ladder" and item["close_ms"] == NIU.end_ms and "missing" not in item, item
    assert item["close_label"] == "10-31 23:59 ET（北京 11-01 11:59）截止；Predict 交易至北京 11-01 07:59", item["close_label"]
    L = item["ladder"]
    assert L["cap"] == "$81.8M" and L["high"] == "$246M" and L["supply"] == "985,000,000" and L["window"] == "08-16 23:30 ET（北京 08-17 11:30）起", L
    r200, r300, r500, r1b = L["rows"]
    assert [r["label"] for r in L["rows"]] == ["$200M", "$300M", "$500M", "$1B"]
    # touched by our (approximate) history, yet the book still trades at 10-14¢: flagged, never a +90¢ "edge"
    assert r200["fair"] == 1.0 and r200["bid"] == 0.10 and "edges" not in r200 and "请核实" in r200["error"], r200
    assert r200["touched"] is False  # a disagreement stays a row of its own, with the warning
    assert abs(r300["dist"] - (3e8 / (0.083 * 985e6) - 1)) < 1e-9 and r300["touched"] is False
    assert L["first_skipped"] is False
    # the same threshold with the book already at 98-99¢ agrees with the history: priced normally
    agree = bot.predict.ladders["NIULAI"][:]
    agree[0] = m.dataclasses.replace(agree[0], book=m.dataclasses.replace(agree[0].book, bids=((D("0.98"), D("10")),), asks=((D("0.99"), D("10")),)))
    bot.predict.ladders["NIULAI"] = agree
    r = bot.cap_payload(cap, NOW)["ladder"]["rows"][0]
    assert r["error"] == "" and [e["label"] for e in r["edges"] if e["best"]] == ["挂Yes"], r
    assert r["touched"] is True  # reached and the book agrees: folded into the "已触及" line
    # a reached level whose market is settled (its book is gone) is folded too
    gone = bot.predict.ladders["NIULAI"][:]
    gone[0] = m.dataclasses.replace(gone[0], book=None, error="HTTP 404: 接口请求失败")
    bot.predict.ladders["NIULAI"] = gone
    assert bot.cap_payload(cap, NOW)["ladder"]["rows"][0]["touched"] is True
    assert r300["bid"] == 0.86 and {e["label"] for e in r300["edges"]} == {"挂Yes", "挂No", "吃Yes", "吃No"}
    assert r["need"] >= r["swing"] >= 0 and r["hold"] == "" and r["sides"] == ["Yes", "No"] and r["fee_bps"] is not None
    assert r["bids"] == [[0.98, 10.0]] and r["asks"] == [[0.99, 10.0]] and r["fetched_ms"] == NOW and r["notional"] == 100, r
    # a book within the model's error of the fair price: nothing is recommended (the page greys the closest direction,
    # ranked like best_edge: a maker wins a tie)
    near = bot.predict.ladders["NIULAI"][:]
    fair300 = r300["fair"]
    assert 0.006 < fair300 < 0.5, fair300
    # Yes 买1 half a cent under the fair price, 卖1 1.5¢ over it: 挂Yes +0.5¢, 挂No +1.5¢, both under the 2¢ minimum
    # (the market lists "No" first, so its own book is No's: No bid = 1 − Yes ask, No ask = 1 − Yes bid)
    yes_bid, yes_ask = round(fair300 - 0.005, 6), round(fair300 + 0.015, 6)
    near[1] = m.dataclasses.replace(near[1], book=m.dataclasses.replace(
        near[1].book, bids=((D(str(1 - yes_ask)), D("40")),), asks=((D(str(1 - yes_bid)), D("40")),)))
    bot.predict.ladders["NIULAI"] = near
    q = bot.cap_payload(cap, NOW)["ladder"]["rows"][1]
    assert abs(q["bid"] - yes_bid) < 1e-9 and abs(q["ask"] - yes_ask) < 1e-9, q
    assert not any(e["best"] for e in q["edges"]) and q["need"] >= m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x"}).predict_min_edge
    top = max(q["edges"], key=lambda e: (round(e["edge"], 4), e["maker"]))
    assert top["label"] == "挂No" and abs(top["edge"] - 0.015) < 1e-6 and "miss" not in q, q
    assert q["swing"] == cap.model_swing(D("3e8"), NOW, fair300)
    # a stale book: no suggestion and no "closest" either (the card says 过期)
    near[1] = m.dataclasses.replace(near[1], book=m.dataclasses.replace(near[1].book, fetched_ms=NOW - m.PREDICT_STALE_MS - 1))
    q = bot.cap_payload(cap, NOW)["ladder"]["rows"][1]
    assert q["stale"] and not any(e["best"] for e in q["edges"])
    # once the window is over nothing is suggested any more (only what happened inside it counts)
    after = bot.cap_payload(cap, NIU.end_ms + 120_000)["ladder"]["rows"]
    assert all(r["hold"] == "窗口已结束，等待结算" for r in after if "bids" in r) and any("bids" in r for r in after), after
    assert "方向未确认" in r500["error"] and "edges" not in r500 and 0 < r500["fair"] < 1
    assert "404" in r1b["error"] and "bid" not in r1b
    # before any data: the default thresholds, and the card says what it waits for
    fresh = m.CapMarket(m.Store(":memory:"), NIU)
    bare = m.Bot(cfg, m.Store(":memory:"), FakeMarket(), None).cap_payload(fresh, NOW)
    assert bare["missing"] == "等待市值数据" and [r["label"] for r in bare["ladder"]["rows"]] == ["$200M", "$300M", "$500M", "$1B"]

    # --- FDV from DexScreener (no RPC), a named pair, no bars: prior σ, live-observed high --------------------------------
    pons_calls = []
    async def pons_get(url, payload=None):
        pons_calls.append(url)
        assert payload is None and url == f"https://api.dexscreener.com/latest/dex/pairs/robinhood/{PONS.pair}", url
        return {"pairs": [{"dexId": "uniswap", "baseToken": {"address": PONS.token}, "priceUsd": price["v"],
                           "liquidity": {"usd": 5e5}, "fdv": float(D(price["v"]) * D("985000000"))}]}
    price = {"v": "0.52"}
    pons = m.CapMarket(m.Store(":memory:"), PONS); pons.get = pons_get
    await pons.refresh(NOW)
    assert pons.error == "" and pons.supply == D("985000000") and pons.cap == D("0.52") * D("985000000"), (pons.error, pons.supply)
    assert pons.sigma == PONS.prior_sigma and "先验" in pons.sigma_note and all("geckoterminal" not in u for u in pons_calls)
    assert "自采价格 0.0 / 12 小时" in pons.sigma_note, pons.sigma_note  # the progress towards a measured σ
    t0 = NOW // 1000
    assert m.sampled_coverage([[t0, 1.0], [t0 + 300, 1.01], [t0 + 7200, 1.0]])[1] == 300  # the 2-hour gap is skipped
    price["v"] = "0.61"; pons.times["price"] = -1e9
    await pons.refresh(NOW + 60_000)
    price["v"] = "0.55"; pons.times["price"] = -1e9
    await pons.refresh(NOW + 120_000)
    high, at = pons.window_high()
    assert high == D("0.61") * D("985000000") and at == (NOW + 60_000) // 1000, (high, at)  # the 0.61 seen a minute ago
    assert pons.probability(D("6e8"), NOW) == 1.0 and 0 < pons.probability(D("7e8"), NOW) < 1
    # no bars: the price is read every 10 s (three times as often), and the window's unobserved stretches are kept: from its
    # opening to the first sample, then every break longer than 2 minutes between samples (the bot down, the feed failing)
    assert pons.price_seconds == 10 and m.CapMarket(m.Store(":memory:"), NIU).price_seconds == 30
    gs = m.Store(":memory:"); g = m.CapMarket(gs, PONS); g.price = D("0.5")
    T0 = PONS.start_ms + 3 * H * 1000
    g.observe(T0)
    # the window before the bot began monitoring the ladder is "not watched", not a sampling gap
    assert g.history["gaps"] == [] and g.history["gap_s"] == 0 and g.history["seen"] == T0 // 1000 and g.history["monitored_from"] == T0 // 1000
    g.observe(T0 + 10_000)
    assert g.history["seen"] == T0 // 1000 and g.history["gaps"] == []  # the mark is persisted once a minute, not every sample
    g.observe(T0 + 70_000)
    assert g.history["seen"] == (T0 + 70_000) // 1000 and g.history["gaps"] == []
    g.observe(T0 + 70_000 + 10 * 60_000)  # ten minutes without a sample
    assert g.history["gaps"] == [[(T0 + 70_000) // 1000, (T0 + 670_000) // 1000]] and g.history["gap_s"] == 600
    g2 = m.CapMarket(gs, PONS); g2.price = D("0.5")  # a restart half an hour later: the gap is read from the persisted mark
    g2.observe(T0 + 670_000 + 30 * 60_000)
    assert len(g2.history["gaps"]) == 2 and g2.history["gap_s"] == 600 + 1800
    note = g2.gaps_note()
    assert note == (f"机器人从 {m.stamp(T0, seconds=False)} 起才监控这张卡，开窗后的前 3.0 小时没有任何记录；监控以来有 2 段没有采样，共 40 分钟"
                    f"（最长 30 分钟：{m.stamp(T0 + 670_000, seconds=False)} → {m.stamp(T0 + 670_000 + 1800_000, seconds=False)}）"
                    "；这些时段碰没碰到档位无法判断（Predict 已结算的档位除外）"), note
    assert bot.cap_payload(g2, NOW)["ladder"]["gaps"] == note and bot.cap_payload(g2, NOW)["ladder"]["monitored_from"] == m.stamp(T0, seconds=False)
    # a few short breaks (restarts) are summed up, not timetabled
    g3s = m.Store(":memory:"); g3 = m.CapMarket(g3s, PONS); g3.price = D("0.5"); g3.observe(T0); t = T0
    for k in range(3):  # ten minutes of samples every minute, then a 3-minute break, three times over
        for _ in range(10):
            t += 60_000; g3.observe(t)
        t += 180_000; g3.observe(t)
    assert g3.gaps_note().endswith("监控以来有 3 次短暂中断（重启或行情接口失败），共 9 分钟，最长 3 分钟；这些时段碰没碰到档位无法判断（Predict 已结算的档位除外）"), g3.gaps_note()
    bars = m.CapMarket(m.Store(":memory:"), NIU); bars.price = D("0.5"); bars.observe(T0 + 3 * H * 1000)
    assert "gaps" not in bars.history and "seen" not in bars.history and bars.gaps_note() == ""  # hourly bars cover the window
    assert bot.cap_payload(pons, NOW)["ladder"]["gaps"].startswith("机器人从"), bot.cap_payload(pons, NOW)["ladder"]["gaps"]  # pons above: watched from its first sample
    # a record from before the coverage marks (or one the first build of them gave the whole window as a gap) is rebuilt from the
    # 5-minute samples: monitoring began at the first sample, breaks over 15 minutes between samples are the gaps
    old = m.Store(":memory:"); S0 = PONS.start_ms // 1000 + 20 * 3600
    old.put(f"capsamples:{PONS.slug}", [[S0 + i * 300, 0.5] for i in range(12)] + [[S0 + 3600 + 3000 + i * 300, 0.5] for i in range(6)])
    old.put(f"cap:{PONS.slug}", {"start": PONS.start_ms, "high": 0.6, "at": S0 + 600, "seen_high": 0.6, "seen_at": S0 + 600,
                                 "gaps": [[PONS.start_ms // 1000, S0 + 7000]], "gap_s": S0 + 7000 - PONS.start_ms // 1000, "seen": S0 + 8350})
    rebuilt = m.CapMarket(old, PONS); rebuilt.price = D("0.5"); rebuilt.observe((S0 + 8400) * 1000)
    hist = rebuilt.history
    assert hist["monitored_from"] == S0 and hist["coverage_v"] == 2 and hist["gaps"] == [[S0 + 3300, S0 + 6600]] and hist["gap_s"] == 3300, hist
    assert rebuilt.gaps_note().startswith(f"机器人从 {m.stamp(S0 * 1000, seconds=False)} 起才监控这张卡，开窗后的前 20.0 小时没有任何记录；监控以来有 1 段没有采样，共 55 分钟"), rebuilt.gaps_note()
    pons_item = bot.cap_payload(pons, NOW)
    assert pons_item["name"] == "$PONS FDV" and pons_item["ladder"]["metric"] == "FDV" and pons_item["ladder"]["settle"] == "DexScreener"
    assert pons_item["ladder"]["bars"] is False and "FDV ÷ 价格" in pons_item["ladder"]["supply_note"], pons_item["ladder"]
    # a market Predict has settled before the window closed counts as reached, whatever our data says
    bot.predict.ladders["PONS"] = [m.LadderRow(D("9e8"), "31", "$900M", None, "HTTP 404: 接口请求失败")]
    bot.predict.market_meta["31"] = ({"outcomes": ["Yes", "No"], "status": "RESOLVED"}, time.monotonic())
    row = bot.cap_payload(pons, NOW)["ladder"]["rows"][0]
    assert row["fair"] == 1.0 and row["touched"] is True, row
    # GeckoTerminal rate-limits (429): the σ saved from the last good fetch stands in, and the bars are not asked
    # for again on every refresh, only after RETRY_SECONDS
    rl_store = m.Store(":memory:")
    ok = m.CapMarket(rl_store, NIU); ok.get, _ = world()
    await ok.refresh(NOW)
    good = ok.sigma
    assert good and rl_store.get(f"capsigma:{NIU.slug}")[0] == good
    limited = m.CapMarket(rl_store, NIU)
    ok_get, _ = world()
    gecko = []
    async def limited_get(url, payload=None):
        if "geckoterminal" in url:
            gecko.append(url)
            raise m.RemoteError("HTTP 429: 接口限流，等待后重试", 30)
        return await ok_get(url, payload)
    limited.get = limited_get
    await limited.refresh(NOW + 60_000)
    assert limited.sigma == good and "保存" in limited.sigma_note and "波动率" not in limited.error, (limited.sigma_note, limited.error)
    assert "missing" not in bot.cap_payload(limited, NOW + 60_000)
    n = len(gecko)
    for _ in range(5):
        limited.times["price"] = -1e9
        await limited.refresh(NOW + 90_000)
    assert len(gecko) == n, gecko[n:]  # no GeckoTerminal request at all during the back-off
    limited.times["vol"] -= m.CapMarket.RETRY_SECONDS  # five minutes later it tries again
    await limited.refresh(NOW + 400_000)
    assert len(gecko) > n
    # with nothing saved: a labelled prior instead of a blank card, the reason kept for the tooltip
    fresh_rl = m.CapMarket(m.Store(":memory:"), NIU); fresh_rl.get = limited_get
    await fresh_rl.refresh(NOW)
    assert fresh_rl.sigma == NIU.prior_sigma and fresh_rl.sigma_kind == "prior" and "429" in fresh_rl.vol_error, fresh_rl.vol_error
    assert "波动率" not in fresh_rl.error
    item = bot.cap_payload(fresh_rl, NOW)
    assert "missing" not in item and item["ladder"]["sigma_kind"] == "prior" and "429" in item["ladder"]["vol_error"]

    # own 5-minute samples: σ measured once 12 hours are covered (gaps over an hour skipped)
    assert m.sampled_sigma([[i * 300, 1.0] for i in range(100)]) is None  # 8 hours
    alt = [[i * 300, 1.01 if i % 2 else 1.0] for i in range(200)]       # ±1% every 5 minutes, 16.6 hours
    sig, hours = m.sampled_sigma(alt + [[10 ** 6, 5.0]])                 # a far-away point after a gap: ignored
    expected = math.log(1.01) * math.sqrt(365 * 86400 / 300)
    assert abs(sig - expected) / expected < 1e-9 and abs(hours - 199 * 300 / 3600) < 1e-9, (sig, hours)
    own = m.CapMarket(m.Store(":memory:"), PONS)
    for i in range(160):  # 13+ hours of 5-minute refreshes
        own.price = D("0.50") * (D("1.02") if i % 2 else 1)
        own.sample(NOW + i * 300_000)
        own.sample(NOW + i * 300_000 + 30_000)  # within 5 minutes: not sampled again
    assert len(own.store.get(f"capsamples:{PONS.slug}")) == 160
    own.fallback_sigma(NOW + 160 * 300_000)
    want = math.log(1.02) * math.sqrt(365 * 86400 / 300)  # ±2% every 5 minutes ≈ 640% a year
    assert own.sigma_kind == "samples" and "自采" in own.sigma_note and abs(own.sigma - want) / want < 1e-9, (own.sigma, own.sigma_note)
    assert bot.cap_payload(own, NOW)["ladder"]["sigma_kind"] == "samples"

    # GeckoTerminal requests are spaced out across ladders
    m.CapMarket.GECKO_GAP = 0.2
    spaced = [m.CapMarket(m.Store(":memory:"), NIU), m.CapMarket(m.Store(":memory:"), ANSEM)]
    stamps = []
    async def turn(c):
        await c.gecko_turn(); stamps.append(time.monotonic())
    await asyncio.gather(*(turn(c) for c in spaced * 2))
    gaps = [b - a for a, b in zip(sorted(stamps), sorted(stamps)[1:])]
    assert min(gaps) >= 0.19, gaps
    m.CapMarket.GECKO_GAP = 0

    # the observed high and a later GeckoTerminal scan share one record
    mix = m.CapMarket(m.Store(":memory:"), NIU); mix.get, _ = world()
    mix.price = D("0.5"); mix.observe(NOW)
    assert mix.history["high"] == 0.5 and "through" not in mix.history
    await mix.scan(NOW)
    assert mix.history["high"] == 0.5 and mix.history["through"] == NOW_S - NOW_S % H, mix.history

    # a short history first (the feed, or the pool it picked, served the last two hours only), the whole window later: the
    # record notes where its bars begin, and a scan that meets older bars reads the window again, so the early high is in
    short = m.CapMarket(m.Store(":memory:"), NIU); full_get, _ = world(peak_hour=FIRST_HOUR + 5 * H, peak_price=0.4)
    async def short_get(url, payload=None):
        data = await full_get(url, payload)
        if "/ohlcv/hour" in url:
            data["data"]["attributes"]["ohlcv_list"] = data["data"]["attributes"]["ohlcv_list"][:2]
        return data
    short.get = short_get
    await short.scan(NOW)
    assert short.history["high"] < 0.4 and short.history["bars_from"] == NOW_S - NOW_S % H - H and short.history["through"] == NOW_S - NOW_S % H, short.history
    note = short.backfill_note(NOW)
    assert note == f"K 线最早到 {m.stamp((NOW_S - NOW_S % H - H) * 1000, seconds=False)}，开窗到那时的 {(NOW_S - NOW_S % H - H - START_S) / H:.0f} 小时没有记录", note
    short_item = bot.cap_payload(short, NOW)["ladder"]
    assert short_item["coverage"] == note and short_item["pool"] == "0xPOOLB" and short_item["bars"] is True
    assert short_item["pool_note"] == "最活跃的池子 0xPOOL…OOLB" and pons.pool_note() == ""  # NIULAI names no pair; STONK's lower-case pair is matched below
    stonk = m.CapMarket(m.Store(":memory:"), STONK); stonk.pool = "AfrDdTgYwCvEqB1GxCaHr8i48O6QtXyQkSdVkELuDeHg"  # the rules' pair, spelt as GeckoTerminal does
    assert stonk.pool_note() == "规则交易对的池子 AfrDdT…DeHg"
    stonk.pool = "SomeOtherPool1111"; assert stonk.pool_note() == "最活跃的池子 SomeOt…1111（GeckoTerminal 没列出规则交易对）"
    short.get = full_get
    await short.scan(NOW)
    assert short.history["high"] == 0.4 and short.history["bars_from"] == FIRST_HOUR - 800 * H and short.backfill_note(NOW) == "", short.history
    assert bot.cap_payload(short, NOW)["ladder"]["coverage"] == ""
    # a record from an older build (no "bars_from") is read from the opening once, so a high it missed is found
    old = m.CapMarket(m.Store(":memory:"), NIU); old.get, _ = world(peak_hour=FIRST_HOUR + 5 * H, peak_price=0.4)
    old.store.put(f"cap:{NIU.slug}", {"start": NIU.start_ms, "high": 0.1, "at": 0, "through": NOW_S - NOW_S % H, "first": "done"})
    await old.scan(NOW)
    assert old.history["high"] == 0.4 and old.history["bars_from"] == FIRST_HOUR - 800 * H, old.history
    # not scanned yet: the note says so; read to the last finished hour: nothing to say; hours finished since: how many
    fresh = m.CapMarket(m.Store(":memory:"), NIU)
    assert fresh.backfill_note(NOW) == "历史 K 线尚未回填（启动后约 5 分钟内读取）" and fresh.backfill_note(NIU.start_ms - 1000) == ""
    assert cap.backfill_note(NOW) == "" and cap.backfill_note(NOW + 2 * H * 1000) == ""  # within two scans of the hour's end: no complaint yet
    later = NOW + (3 * H + 1200) * 1000
    assert cap.backfill_note(later) == f"已核验到 {m.stamp(((NOW_S - NOW_S % H) + H) * 1000, seconds=False)}，之后 2 小时尚未读取", cap.backfill_note(later)
    assert pons.backfill_note(NOW) == "" and bot.cap_payload(pons, NOW)["ladder"]["pool"] == ""  # no bars: the sampling gaps say it instead
    # the bars' high and the bot's own samples' high are kept apart: a changed pool drops the other pool's bars (and reads the
    # window again), the samples stay; a record from an older build keeps its high as the bars' until then
    moved = m.CapMarket(m.Store(":memory:"), NIU); moved.get, mcalls = world(peak_hour=FIRST_HOUR + 5 * H, peak_price=0.4)
    moved.store.put(f"cap:{NIU.slug}", {"start": NIU.start_ms, "high": 9.9, "at": 123, "through": NOW_S - NOW_S % H, "first": "done", "pool": "0xOLDPOOL",
                                        "bars_from": FIRST_HOUR - 800 * H, "spike": [123, 9.9, 1.0]})
    moved.price = D("0.5"); moved.observe(NOW)
    assert moved.history["seen_high"] == 0.5 and moved.history["bar_high"] == 9.9 and moved.history["high"] == 9.9
    await moved.scan(NOW)
    h = moved.history
    assert h["pool"] == "0xPOOLB" and h["bar_high"] == 0.4 and h["bar_at"] == FIRST_HOUR + 5 * H and h["seen_high"] == 0.5, h
    assert h["high"] == 0.5 and h["at"] == NOW_S and h["first"] == "done" and sum("/ohlcv/hour" in x for x, _ in mcalls) == 2, h  # read from the opening again
    assert cap.history["pool"] == "0xPOOLB" and cap.history["bar_high"] == 0.25 and cap.history["high"] == 0.25
    # a high set by a wick (more than double the bar's open and close) is said so; a tamer peak is not
    assert cap.spike_note().startswith(f"窗口最高来自 {m.stamp(peak * 1000, seconds=False)} 那一小时的插针（最高 0.25，开收盘最高 0.08"), cap.spike_note()
    assert bot.cap_payload(cap, NOW)["ladder"]["spike"] == cap.spike_note()
    tame = m.CapMarket(m.Store(":memory:"), NIU); tame.get, _ = world(peak_hour=peak, peak_price=0.15)
    await tame.scan(NOW); assert tame.history["bar_high"] == 0.15 and "spike" not in tame.history and tame.spike_note() == ""
    assert pons.spike_note() == "" and bot.cap_payload(pons, NOW)["ladder"]["spike"] == ""



async def browser_check():
    """The cap ladder's table (shared with the price ladders): the best maker and the best taker per level, in colour when
    suggested, grey otherwise (the bar itself is never shown); 盘口过期 / 暂不建议 as text."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        async_playwright = None
    chrome = next((p for p in [os.environ.get("CHROMIUM_PATH", ""), "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"]
                   if p and os.path.exists(p)), "")
    if async_playwright is None or not (chrome or os.environ.get("PLAYWRIGHT_BROWSERS_PATH")):
        print("browser check skipped (no Playwright/Chromium)")
        return
    now = int(dt.datetime(2026, 10, 2, 1, 16, tzinfo=m.BEIJING).timestamp() * 1000)

    class FakeMarket:
        def __init__(self): self.config = None
        def now_ms(self): return now
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT", "HSI_FUTURES": "off", "KOSPI_INDEX": "off"})
    bot = m.Bot(cfg, m.Store(":memory:"), FakeMarket(), None)
    cap = bot.caps["NIULAI"]
    cap.price, cap.priced_ms, cap.supply, cap.sigma, cap.sigma_kind = D("0.0996"), now, D("985000000"), 3.31, "bars"
    edge = lambda label, maker, price, value, best=False: {"label": label, "maker": maker, "price": price, "edge": value, "size": 100.0,
                                                          "gross": value, "fee": 0.0, "slip": 0.0, "short": False, "best": best}
    def row(label, fair, bid, ask, edges, need, **extra):
        # the page prices the four directions itself, from the depth (Yes side), the fee and the trade size
        return {"label": label, "fair": fair, "error": "", "dist": 1.0, "bid": bid, "ask": ask, "stale": False, "edges": edges,
                "bids": [[bid, 5000.0]], "asks": [[ask, 5000.0]], "fee_bps": 200, "notional": 100, "sides": ["Yes", "No"],
                "fetched_ms": now, "need": need, "swing": need, "hold": "", "touched": False, "makers": True, **extra}
    e200 = [edge("挂Yes", True, 0.352, -0.048), edge("挂No", True, 0.638, 0.058), edge("吃Yes", False, 0.362, -0.065), edge("吃No", False, 0.648, 0.041)]
    e500 = [edge("挂Yes", True, 0.06, -0.024), edge("挂No", True, 0.932, 0.0318), edge("吃Yes", False, 0.068, -0.033), edge("吃No", False, 0.94, 0.023)]
    e1b = [edge("挂Yes", True, 0.025, -0.021), edge("挂No", True, 0.974, 0.022, True), edge("吃Yes", False, 0.026, -0.022), edge("吃No", False, 0.975, 0.020)]
    item = bot.cap_payload(cap, now)
    item["ladder"]["rows"] = [row("$200M", 0.304, 0.352, 0.362, e200, 0.067, dist=0.52),  # distances of different widths: the columns must still align
                              row("$500M", 0.036, 0.06, 0.068, e500, 0.0324, dist=2.8),
                              row("$1B", 0.004, 0.025, 0.026, e1b, 0.02, points_active=True, points_ok=True, points_rate=50, points_note="积分已激活",
                                  points_spread=0.06, points_min_shares=100),
                              row("$2B", 0.001, 0.01, 0.02, e1b[:1], 0.02, stale=True, points_active=False, points_ok=False, points_note="积分未激活",
                                  points_why="积分未激活", dist=15),
                              row("$3B", 0.004, 0.025, 0.026, e1b, 0.02, hold="σ 是先验值，只作参考", fetched_ms=now - 30_000, dist=24),
                              {"label": "$5B", "fair": 0.001, "error": "", "dist": 4.0, "touched": False}]  # no Predict book: cap_payload's bare row
    item["ladder"]["gaps"] = "窗口内有 2 段没有采样，共 1.5 小时（最长 1.0 小时：09-20 10:00 → 09-20 11:00）；这些时段碰没碰到档位无法判断"
    payload = bot.odds_payload()
    payload["items"] = [item]
    bot.odds_payload = lambda: payload
    web = m.WebServer(bot, 0, "t" * 20); web.CACHE_SECONDS = {}; port = await web.start()  # the tests change the payload and reload at once
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**({"executable_path": chrome} if chrome else {}))
        page = await browser.new_page(viewport={"width": 390, "height": 900})
        errs = []; page.on("pageerror", lambda e: errs.append(str(e)))  # a script error anywhere on the page fails the check at the end
        await page.goto(f"http://127.0.0.1:{port}/p/{'t' * 20}")
        await page.wait_for_selector("#g-ladder .pg")
        assert "⚠️ 窗口内有 2 段没有采样，共 1.5 小时" in await page.inner_text("#g-ladder .lstat")  # the unobserved stretches, on the card
        # the same four-column table as the price ladders: 目标 / 模型 / 挂单 (the best maker) / 吃单 (the best taker); blue =
        # suggested, grey = not big enough (the bar itself is never shown), 盘口过期 / 暂不建议 as text
        assert await page.locator("#g-ladder .pg .lh").all_inner_texts() == ["目标", "模型", "挂单", "吃单"]
        cells = await page.locator("#g-ladder .pg .paction").evaluate_all(
            "els => els.map(e => [e.className.replace('pcell paction', '').trim(), e.innerText.replace(/\\s+/g, ' ').trim(), e.title])")
        assert [c[:2] for c in cells] == [["", "No 63.8¢ +5.8¢ 低于门槛"], ["", "No 64.8¢ +4.1¢ 低于门槛"],    # $200M: nothing clears 6.7¢ (said on the cell)
                                         ["", "No 93.2¢ +3.2¢ 低于门槛"], ["", "No 94.0¢ +2.3¢ 低于门槛"],    # $500M: 3.2¢ under a 3.24¢ bar
                                         ["pos", "No 97.4¢ +2.2¢"], ["pos", "No 97.5¢ +2.1¢"],              # $1B: both clear 2¢
                                         ["", "No 98.0¢ +1.9¢ 盘口过期"], ["", "No 99.0¢ +0.9¢ 盘口过期"],    # $2B: a stale book
                                         ["", "No 97.4¢ +2.2¢ 暂不建议"], ["", "No 97.5¢ +2.1¢ 暂不建议"],      # $3B: a prior σ
                                         ["", "—"], ["", "—"]], cells                                          # $5B: no book
        assert cells[10][2] == cells[11][2] == "Predict 暂无盘口", cells[10:]  # not a points-gate tooltip
        assert cells[0][2] == "挂No @ 63.8¢；净优势 +5.8¢，挂单排队，成交不保证；未过建议门槛 6.7¢（σ ×/÷1.25 的模型误差）", cells[0][2]
        assert cells[8][2].endswith("；σ 是先验值，只作参考"), cells[8][2]
        assert "低于门槛" in await page.inner_text("#g-ladder") and "6.7¢" not in await page.inner_text("#g-ladder .pg")  # the bar itself only in the tooltip / note
        # the red-frame bar can be this card's own (hotmap, per browser): 2¢ makes the $1B 挂No +2.2¢ a red frame and a strip entry
        await page.evaluate("localStorage.setItem('hotmap', JSON.stringify({'card:NIULAI': 2}))")
        await page.reload(); await page.wait_for_selector("#g-ladder .pg")
        strip = await page.inner_text("#opps")  # one card, listed on both rows: the $1B maker and taker
        assert await page.locator("#g-ladder .card.hot").count() == 1 and strip.startswith("🔥 机会 2") and "$1B 挂No 97.4" in strip and "$1B 吃No 97.5" in strip, strip
        assert "另有 1 处单独设置" in await page.inner_text("#legend")
        # a level's own bar wins over the card's: 3¢ on $1B puts it back under; the box shows the inherited 2 as its placeholder
        await page.click("#g-ladder .pg .lt:text-is('$1B')")
        hotrow = page.locator("#g-ladder .lrow .hotrow input")
        assert await hotrow.get_attribute("placeholder") == "2"
        await hotrow.fill("3"); await hotrow.dispatch_event("change"); await page.wait_for_timeout(200)
        assert await page.locator("#g-ladder .card.hot").count() == 0 and await page.evaluate("JSON.parse(localStorage.getItem('hotmap'))") == {"card:NIULAI": 2, "row:NIULAI#$1B": 3}
        await page.click("#g-ladder .pg .lt:text-is('$1B')")  # close the row again
        await page.evaluate("localStorage.removeItem('hotmap')"); await page.reload(); await page.wait_for_selector("#g-ladder .pg")
        assert await page.locator("#g-ladder .card.hot").count() == 0
        # 自定义: the section title and every card's edit bar carry a ¢ box of their own (栏目 < 卡 < 档位); an empty box inherits,
        # and shows what it inherits as its placeholder
        hotmap = lambda: page.evaluate("JSON.parse(localStorage.getItem('hotmap') || '{}')")
        set_box = lambda sel, v: page.locator(sel).fill(v)
        await page.click("#edit")
        sec, crd = "#h-ladder .hotset input", "#g-ladder .card .ctl .hotset input"
        assert await page.get_attribute(sec, "placeholder") == "10" and await page.input_value(sec) == ""  # the global 10¢
        await set_box(sec, "2"); await page.dispatch_event(sec, "change"); await page.wait_for_timeout(200)
        assert await hotmap() == {"sec:ladder": 2} and await page.get_attribute(crd, "placeholder") == "2"  # the card inherits the section's
        await set_box(crd, "4"); await page.dispatch_event(crd, "change"); await page.wait_for_timeout(200)
        assert await hotmap() == {"sec:ladder": 2, "card:NIULAI": 4}
        await page.click("#done")
        assert await page.locator("#g-ladder .card.hot").count() == 0 and "另有 2 处单独设置" in await page.inner_text("#legend")  # 4¢ > 2.2¢
        await page.click("#edit"); await set_box(crd, ""); await page.dispatch_event(crd, "change"); await page.wait_for_timeout(200)
        assert await hotmap() == {"sec:ladder": 2}
        await page.click("#done")
        assert await page.locator("#g-ladder .card.hot").count() == 1  # the section's 2¢ again
        await page.evaluate("localStorage.removeItem('hotmap')"); await page.reload(); await page.wait_for_selector("#g-ladder .pg")
        assert await page.locator("#g-ladder .card.hot").count() == 0 and "另有" not in await page.inner_text("#legend")
        # points on a cap-ladder level: ● with the rate when a quote would earn, a faint ○ when not, nothing while unknown
        assert await page.locator("#g-ladder .pg .ptarget .ppoints").evaluate_all("els => els.map(e => [e.className, e.textContent, e.title])") == \
            [["ppoints active", "● 50 PP/h", "积分可得：这个市场的挂单每小时发 50 PP（价差不超过 6.0¢、至少 100 份）"], ["ppoints off", "○", "积分未激活"]]
        # the book's age: the oldest level's (30 s)
        assert re.fullmatch(r"行情 \d 秒前\s+盘口 3\d 秒前", await page.inner_text("#g-ladder .ages")), await page.inner_text("#g-ladder .ages")
        # tap a level: its note (distance, model, book, points), its four directions as chips, then a chip's details; tap again to close
        await page.click("#g-ladder .pg .lt:text-is('$1B')")
        note = await page.inner_text("#g-ladder .lrow .lnote")
        assert note.startswith("$1B：市值还要涨 100.0% 才碰到 · 模型 Yes 0.4¢ · Yes 盘口 2.5 / 2.6") and "50 PP/小时" in note, note
        chips = page.locator("#g-ladder .lrow .edge")
        assert await chips.count() == 4 and await page.locator("#g-ladder .lrow .edge.best").inner_text() == "挂No 97.4\n+2.2¢"
        cols = await page.evaluate("getComputedStyle(document.querySelector('#g-ladder .lrow .edges')).gridTemplateColumns")
        assert len(cols.split()) == 2, cols  # a phone: two chips per row, as on the price ladders
        await chips.nth(1).click()
        det = await page.inner_text("#g-ladder .lrow .edet")
        assert det.startswith("挂No @ 97.4¢：挂单排队") and "这个价位已有 5,000 份在排队" in det and "→ 满足" in det and "不标红框" in det, det
        await page.click("#g-ladder .pg .lt:text-is('$1B')")
        assert await page.locator("#g-ladder .lrow").count() == 0
        await page.click("#g-ladder .pg .lt:text-is('$3B')")
        assert await page.locator("#g-ladder .lrow .edge.best").count() == 0
        await page.locator("#g-ladder .lrow .edge").nth(1).click()
        assert "暂不建议：σ 是先验值，只作参考" in await page.inner_text("#g-ladder .lrow .edet")
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "no sideways scroll"
        # the filter bar sees every level: 有建议 keeps the card (its $1B level); 3 小时内收盘 drops it (30 days left)
        await page.click("#fchips button:text-is('有建议')")
        assert await page.locator("#g-flat .card.lad").count() == 1
        await page.click("#fchips button:text-is('有建议')")  # back to the sections: the card in its own 500px grid
        # in its own 500px card the table never sticks out either, points pills included
        wide = """[...document.querySelectorAll('#g-ladder .card *')].filter(e => { const c = e.closest('.card').getBoundingClientRect(),
                  r = e.getBoundingClientRect(); return r.width && (r.right > c.right + 0.5 || r.left < c.left - 0.5) }).length"""
        lefts = lambda sel: page.eval_on_selector_all(sel, "els => els.map(e => Math.round(e.getBoundingClientRect().left))")
        for width in (1300, 900, 620):
            await page.set_viewport_size({"width": width, "height": 900})
            assert await page.evaluate(wide) == 0, width
            # on a wide card the level, its distance and its points pill are three aligned columns ($1B / $2B carry pills)
            pl, dl = await lefts("#g-ladder .pg .ptarget .ppoints"), await lefts("#g-ladder .pg .ptarget .pdist")
            assert len(pl) == 2 and len(set(pl)) == 1 and len(dl) == 6 and len(set(dl)) == 1 and pl[0] > dl[0], (width, pl, dl)
        assert await page.evaluate("document.querySelector('#g-ladder .ages').compareDocumentPosition(document.querySelector('#g-ladder .pb')) & 2")  # ages after the table
        await page.set_viewport_size({"width": 390, "height": 900})
        # a phone: the pill sits under the level, at its left edge, on every row (no inline pill on the short labels)
        rect = "e => { const r = e.getBoundingClientRect(); return [r.left, r.top, r.bottom] }"
        for label in ("$1B", "$2B"):
            lt = await page.locator(f"#g-ladder .ptarget:has(.lt:text-is('{label}')) .lt").evaluate(rect)
            pp = await page.locator(f"#g-ladder .ptarget:has(.lt:text-is('{label}')) .ppoints").evaluate(rect)
            assert abs(lt[0] - pp[0]) < 1 and pp[1] >= lt[2] - 1, (label, lt, pp)
        # in a grid of ordinary cards (filtered, or starred) a ladder takes the whole row: its table never sticks out
        await page.click("#fchips button:text-is('有建议')")
        over = """[...document.querySelectorAll('#g-flat .card *')].filter(e => { const c = e.closest('.card').getBoundingClientRect(),
                  r = e.getBoundingClientRect(); return r.width && (r.right > c.right + 0.5 || r.left < c.left - 0.5) }).length"""
        for width in (620, 900, 1300, 390):
            await page.set_viewport_size({"width": width, "height": 900})
            assert await page.evaluate(over) == 0, width
        await page.click("#fchips button:text-is('3 小时内收盘')")
        assert await page.locator("#g-flat .card").count() == 0 and "没有符合条件的卡片" in await page.inner_text("#g-flat")
        await page.evaluate("filt=[];sortBy='';keep('filt',filt);keep('sort',sortBy)")  # back from the flat view
        # the cap unknown (every distance null) and five earning levels: the compact view is exactly those five; a level
        # without a distance is never forced in as the price's neighbour
        for r in item["ladder"]["rows"]:
            r["dist"] = None
            if r["label"] in ("$500M", "$2B", "$3B", "$5B"):
                r.update(points_active=True, points_ok=True, points_note="积分已激活", points_rate=10, points_why="")
        item["ladder"]["rows"].append({"label": "$4B", "fair": 0.002, "error": "", "dist": None, "touched": False})
        await page.reload(); await page.wait_for_selector("#g-ladder .pg")
        assert await page.locator("#g-ladder .pg .lt").all_inner_texts() == ["$500M", "$1B", "$2B", "$3B", "$5B"]
        assert await page.inner_text("#g-ladder .price-tools button") == "全部 7 档（+2）"
        # two or more cards to a row: every card the same height, capped; a long table scrolls inside the card under a sticky header
        await page.set_viewport_size({"width": 1300, "height": 900})
        item["ladder"]["rows"] = [row(f"${n}M", 0.3, 0.352, 0.362, e200, 0.067, dist=n / 10) for n in range(1, 15)]
        await page.reload(); await page.wait_for_selector("#g-ladder .pg")
        await page.click("#g-ladder .price-tools button")  # all 14 levels
        pg = "document.querySelector('#g-ladder .pg')"
        card_h = await page.evaluate("document.querySelector('#g-ladder .card').getBoundingClientRect().height")
        assert card_h <= 460 and await page.evaluate(f"{pg}.querySelectorAll('.ptarget').length") == 14, card_h
        assert await page.evaluate(f"{pg}.scrollHeight > {pg}.clientHeight + 40") and await page.evaluate(f"getComputedStyle({pg}).overflowY") == "auto"
        await page.evaluate(f"{pg}.scrollTop = 150")
        assert await page.evaluate(f"Math.abs({pg}.querySelector('.lh').getBoundingClientRect().top - {pg}.getBoundingClientRect().top) < 1")  # the header stays
        scrolled = await page.evaluate(f"{pg}.scrollTop"); assert scrolled > 100, scrolled  # 150, clamped to the table's own range
        await page.evaluate("load()"); await page.wait_for_timeout(300)  # a data refresh rebuilds the card: the table keeps its place
        assert await page.evaluate(f"{pg}.scrollTop") == scrolled and await page.evaluate(f"{pg}.querySelectorAll('.ptarget').length") == 14
        assert await page.evaluate("document.querySelector('#g-ladder .ages').getBoundingClientRect().top") > await page.evaluate(f"{pg}.getBoundingClientRect().bottom") - 1
        await page.set_viewport_size({"width": 390, "height": 900})
        assert await page.evaluate(f"getComputedStyle({pg}).overflowY") == "visible"  # a phone: the page scrolls, not the card
        item["ladder"]["rows"] = []; item["ladder"]["waiting"] = "等待 Predict 档位"  # a new spec before Predict lists its levels
        await page.reload(); await page.wait_for_selector("#g-ladder .card")
        assert "等待 Predict 档位" in await page.inner_text("#g-ladder .lstat") and await page.locator("#g-ladder .pg").count() == 0
        assert "暂无待触及的档位" not in await page.inner_text("#g-ladder .card")
        assert not errs, errs
        await browser.close()
    await web.stop()


asyncio.run(run())
asyncio.run(browser_check())
print("CAP_OK")
