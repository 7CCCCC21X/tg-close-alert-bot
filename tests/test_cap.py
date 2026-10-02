import asyncio, os, sys, math, time, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
NIU, ANSEM, PONS = m.CAP_MARKETS
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
    assert r["miss"] is None and r["need"] >= r["swing"] >= 0  # this row has a best direction: no "miss" beside it
    # a book within the model's error of the fair price: nothing is recommended, the closest direction is kept for the
    # card (grey, with the bar it missed); ranked like best_edge (a maker wins a tie)
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
    assert q["miss"] == {**top, "best": False} and q["miss"]["label"] == "挂No" and abs(q["miss"]["edge"] - 0.015) < 1e-6, q
    assert q["swing"] == cap.model_swing(D("3e8"), NOW, fair300)
    # a stale book: no suggestion and no "closest" either (the card says 过期)
    near[1] = m.dataclasses.replace(near[1], book=m.dataclasses.replace(near[1].book, fetched_ms=NOW - m.PREDICT_STALE_MS - 1))
    q = bot.cap_payload(cap, NOW)["ladder"]["rows"][1]
    assert q["stale"] and q["miss"] is None and not any(e["best"] for e in q["edges"])
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



async def browser_check():
    """The 最优 cell: a passing direction in colour; otherwise the closest one in grey (the bar itself is never shown);
    过期 / ⚠️ as before."""
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
    def row(label, fair, bid, ask, edges, need, miss=None, **extra):
        return {"label": label, "fair": fair, "error": "", "dist": 1.0, "bid": bid, "ask": ask, "stale": False, "edges": edges,
                "need": need, "swing": need, "miss": miss, "touched": False, **extra}
    e200 = [edge("挂Yes", True, 0.352, -0.048), edge("挂No", True, 0.638, 0.058), edge("吃Yes", False, 0.362, -0.065), edge("吃No", False, 0.648, 0.041)]
    e500 = [edge("挂Yes", True, 0.06, -0.024), edge("挂No", True, 0.932, 0.0318), edge("吃Yes", False, 0.068, -0.033), edge("吃No", False, 0.94, 0.023)]
    e1b = [edge("挂Yes", True, 0.025, -0.021), edge("挂No", True, 0.974, 0.022, True), edge("吃Yes", False, 0.026, -0.022), edge("吃No", False, 0.975, 0.020)]
    item = bot.cap_payload(cap, now)
    item["ladder"]["rows"] = [row("$200M", 0.304, 0.352, 0.362, e200, 0.067, e200[1]),
                              row("$500M", 0.036, 0.06, 0.068, e500, 0.0324, e500[1]),
                              row("$1B", 0.004, 0.025, 0.026, e1b, 0.02),
                              row("$2B", 0.001, 0.01, 0.02, e1b[:1], 0.02, stale=True)]
    payload = bot.odds_payload()
    payload["items"] = [item]
    bot.odds_payload = lambda: payload
    web = m.WebServer(bot, 0, "t" * 20); port = await web.start()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**({"executable_path": chrome} if chrome else {}))
        page = await browser.new_page(viewport={"width": 390, "height": 900})
        await page.goto(f"http://127.0.0.1:{port}/p/{'t' * 20}")
        await page.wait_for_selector("#g-ladder .lg")
        cells = await page.eval_on_selector_all("#g-ladder .lg .lb:not(.lk)", "els => els.map(e => [e.className, e.innerText])")
        assert cells == [["lb miss", "挂No 63.8 +5.8¢"], ["lb miss", "挂No 93.2 +3.2¢"], ["lb pos", "挂No 97.4 +2.2¢"], ["lb", "过期"]], cells
        tip = await page.get_attribute("#g-ladder .lg .lb.miss", "title")
        assert tip == "挂No @ 63.8¢ 净优势 +5.8¢，不够大，不建议", tip
        assert "门槛" not in await page.inner_text("#g-ladder")
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "no sideways scroll"
        await browser.close()
    await web.stop()


asyncio.run(run())
asyncio.run(browser_check())
print("CAP_OK")
