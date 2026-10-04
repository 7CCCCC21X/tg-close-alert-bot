"""Guards added after the review: a first-touch market sees a spike inside the running hour (and defers to a book that
already trades the question as settled), a deadline is never read out of "market" or "decision", and one broken web
card never takes the whole page down."""
import asyncio, sys, json, time, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
BNB = m.TOUCH_MARKETS[0]
H = 3_600_000
NOW = int(dt.datetime(2026, 9, 29, 12, 30, tzinfo=m.BEIJING).timestamp() * 1000)
HOUR = NOW - NOW % H  # the hour still running at NOW

# --- deadlines: whole month words only -------------------------------------------------------------------------------
after = int(dt.datetime(2026, 6, 18, tzinfo=dt.timezone.utc).timestamp() * 1000)
for text in ["trades at $100 before market close on any day", "Resolves by decision of the committee", "the market 1 minute candle",
             "until the marketplace reopens", "by Decentralized vote"]:
    assert m.deadline_from_text([text], after) == 0, text
oct31, dec31 = m.et_wall_ms(dt.date(2026, 10, 31), 23, 59), m.et_wall_ms(dt.date(2026, 12, 31), 23, 59)
assert m.deadline_from_text(["hits $100 by October 31, 2026"], after) == oct31
assert m.deadline_from_text(["by Oct. 31"], after) == oct31 and m.deadline_from_text(["before market close on December 31"], after) == dec31
assert m.deadline_from_text(["by the end of September"], after) == m.et_wall_ms(dt.date(2026, 9, 30), 23, 59)
assert m.deadline_from_text(["截止于 12 月 31 日"], after) == dec31 and m.deadline_from_text(["Sept 5, 2026"], after) == m.et_wall_ms(dt.date(2026, 9, 5), 23, 59)
assert m.deadline_from_text(["in January"], after) == 0  # no "by": not a deadline

# --- a book that already trades the question as settled ---------------------------------------------------------------
def book(bids, asks):
    return m.PredictBook("BNB", BNB.slug, "1", "BNB", tuple((D(p), D(q)) for p, q in bids), tuple((D(p), D(q)) for p, q in asks), NOW)
assert m.book_decided(book([("0.92", "10")], [("0.95", "10")])) == "up" and m.book_decided(book([("0.05", "10")], [("0.08", "10")])) == "down"
assert m.book_decided(book([("0.55", "10")], [("0.60", "10")])) == "" and m.book_decided(None) == "" and m.book_decided(book([], [("0.95", "1")])) == ""


class FakeMarket:
    def now_ms(self): return NOW


async def run():
    # --- the running hour is checked minute by minute; the minute still forming decides nothing ----------------------------
    store = m.Store(":memory:"); t = m.TouchMarket(store, BNB); t.start_ms = NOW - 5 * H
    spike = {"minute": 30}  # which minute of the running hour reaches $906 (30 = the one forming at 12:30)
    asked = []

    async def get(path, **p):
        if path == "ticker/price":
            return {"symbol": "BNBUSDT", "price": spike.get("price", "812.5")}
        if p["interval"] == "1h" and p.get("limit") == 722:
            return [[NOW - (722 - i) * H, "0", "0", "0", str(800 * (1.01 if i % 2 else 1)), "0", NOW - (721 - i) * H - 1] for i in range(722)]
        asked.append((p["interval"], p["startTime"], p.get("endTime")))
        if p["interval"] == "1h":
            out = [[h, "0", "820", "800", "810", "0", h + H - 1] for h in range(p["startTime"], HOUR, H)]
            return out + [[HOUR, "0", "906", "800", "810", "0", HOUR + H - 1]]  # the running hour, high so far 906
        out = []
        for i in range(31):
            opened = p["startTime"] + i * 60_000
            out.append([opened, "0", "906" if i == spike["minute"] else "820", "800", "810", "0", opened + 59_999])
        return out
    t.get = get
    await t.scan(NOW)
    assert t.history["kind"] == "clear" and t.history["through"] == HOUR, t.history  # only the forming minute spiked
    assert [a for a in asked if a[0] == "1m"] == [("1m", HOUR, NOW)], asked
    spike["minute"] = 12
    await t.scan(NOW)
    assert t.history["kind"] == "high" and t.history["time"] == HOUR + 12 * 60_000, t.history
    assert t.odds(NOW) == m.TouchOdds(0.0, 1.0, 0.0) and "先触及 $900" in t.status()
    # the live price standing at a line brings the next path check forward
    store.put(f"touch:{BNB.slug}", {"kind": "clear", "through": HOUR, "start": t.start_ms})
    t.times["scan"] = time.monotonic(); spike["price"] = "905"
    n = len(asked); await t.refresh(NOW)
    assert any(a[0] == "1h" for a in asked[n:]) and t.history["kind"] == "high"

    # --- a book bidding 92¢ for "$900 first" while the model still sees 50/50: no 42¢ suggestion, a hold instead ----------
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "PROBABILITY": "off"})
    bot = m.Bot(cfg, m.Store(":memory:"), FakeMarket(), None)
    touch = bot.touches["BNB"]; touch.start_ms = NOW - 5 * H
    touch.price, touch.priced_ms, touch.sigma, touch.sigma_ms = D("812.5"), NOW, 0.5, NOW
    bot.store.put(f"touch:{BNB.slug}", {"kind": "clear", "through": NOW - 60_000, "start": touch.start_ms})
    bot.predict.info[BNB.slug] = {"outcomes": ["$700", "$900"], "created_ms": touch.start_ms}
    bot.predict.books["BNB"] = book([("0.05", "100")], [("0.08", "50")])  # $700 side at 5–8¢, i.e. $900 first bid 92¢
    item = bot.touch_payload(touch, NOW)
    assert item["predict"]["bids"][0] == [0.92, 50.0] and "定局" in item["predict"]["hold"] and item["touch"]["hold"] == item["predict"]["hold"], item["predict"]["hold"]
    assert not any(e["best"] for e in item["predict"]["edges"]) and 0.3 < item["fair_up"] < 0.7
    bot.predict.books["BNB"] = book([("0.40", "100")], [("0.45", "50")])  # an open question: advice flows again
    item = bot.touch_payload(touch, NOW)
    assert item["predict"]["hold"] == "" and item["touch"]["hold"] == ""

    # --- one broken card leaves a placeholder, the rest of the page stands, and the JSON still serialises ----------------
    whole = bot.odds_payload()
    names = [i["name"] for i in whole["items"]]
    assert "BNB 先触 700/900" in names and len(names) > 5
    def broken(touch, now_ms): raise KeyError("someNewField")
    bot.touch_payload = broken
    page = bot.odds_payload()
    placeholders = [i for i in page["items"] if i.get("missing", "").startswith("卡片构建失败")]
    assert len(placeholders) == len(bot.touches) and placeholders[0]["name"] == "BNB 先触" and "someNewField" in placeholders[0]["missing"], placeholders
    assert len(page["items"]) == len(whole["items"])
    json.dumps(page, ensure_ascii=False, default=str)
    print("GUARD_OK")


asyncio.run(run())

# --- small guards: entities and bold tags survive splitting, a thousands separator is not a separator -----------------
long = "&amp;".join(["x" * 7] * 600)  # cuts would otherwise land inside an entity
for chunk in m.split_text(long, 100):
    assert not __import__("re").search(r"&[a-z]*$", chunk) or chunk.endswith(";"), chunk[-10:]
assert "".join(m.split_text(long, 100)) == long
assert m.balance_bold(["<b>open", "still open", "closed</b> tail"]) == ["<b>open</b>", "<b>still open</b>", "<b>closed</b> tail"]
assert m.balance_bold(["<b>a</b>", "b"]) == ["<b>a</b>", "b"]
entries = m.parse_close_entries("SKHYNIX 258,000 KRW", NOW)
assert len(entries) == 1 and entries[0].value == D("258000") and entries[0].currency == "KRW", entries
print("GUARD2_OK")
