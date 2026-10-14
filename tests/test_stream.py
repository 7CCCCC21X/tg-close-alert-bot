"""WebSocket streams: a small RFC 6455 client (handshake, masked frames out, pings answered, fragments joined, long
frames), the reconnecting feed around it, Binance trades and mark prices feeding the alert loop and the crypto markets
without a request, Predict books pushed into the feed's books and ladders, and what /status says about them."""
import asyncio, base64, hashlib, json, sys, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import offline  # noqa: F401  (blocks real HTTP)
import main as m
D = m.D
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# --- an in-process WebSocket server (plain TCP: the client's TLS is only for wss) --------------------------------------
async def read_frame(reader):
    head = await reader.readexactly(2)
    fin, opcode, masked, n = bool(head[0] & 0x80), head[0] & 0x0F, bool(head[1] & 0x80), head[1] & 0x7F
    if n == 126: n = int.from_bytes(await reader.readexactly(2), "big")
    elif n == 127: n = int.from_bytes(await reader.readexactly(8), "big")
    mask = await reader.readexactly(4) if masked else b""
    payload = await reader.readexactly(n) if n else b""
    return fin, opcode, masked, (m.ws_mask(payload, mask) if masked else payload)


def frame(opcode, payload, fin=True):
    n = len(payload)
    head = bytes([(0x80 if fin else 0) | opcode]) + (bytes([n]) if n < 126 else bytes([126]) + n.to_bytes(2, "big") if n < 65536 else bytes([127]) + n.to_bytes(8, "big"))
    return head + payload


async def handshake(reader, writer, status="101"):
    head = (await reader.readuntil(b"\r\n\r\n")).decode()
    request, _, rest = head.partition("\r\n")
    key = m.header_value(rest, "sec-websocket-key")
    assert request.startswith("GET ") and m.header_value(rest, "upgrade").lower() == "websocket" and key, head
    if status != "101":
        writer.write(f"HTTP/1.1 {status} Nope\r\nContent-Length: 0\r\n\r\n".encode()); await writer.drain(); return None
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    writer.write(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode())
    await writer.drain()
    return request


async def run():
    # --- the client against an echo server: text both ways, a ping answered with the same payload, fragments joined,
    # a 126- and a 127-length frame, the peer's close
    got = {"request": "", "pong": None, "closed": False}

    async def echo(reader, writer):
        got["request"] = await handshake(reader, writer)
        if got["request"] is None:
            writer.close(); return
        while True:
            fin, opcode, masked, payload = await read_frame(reader)
            assert masked, "a client frame must be masked"
            if opcode == 0x1 and payload == b"ping me":
                writer.write(frame(0x9, b"hb")); await writer.drain(); continue
            if opcode == 0xA:
                got["pong"] = payload; continue
            if opcode == 0x1 and payload == b"fragments":
                writer.write(frame(0x1, "片".encode()[:2], fin=False) + frame(0x0, "片段".encode()[2:], fin=True)); await writer.drain(); continue
            if opcode == 0x1 and payload == b"long":
                writer.write(frame(0x1, b"x" * 300) + frame(0x1, b"y" * 70000)); await writer.drain(); continue
            if opcode == 0x1 and payload == b"bye":
                writer.write(frame(0x8, b"")); await writer.drain()
                fin, opcode, masked, payload = await read_frame(reader); got["closed"] = opcode == 0x8
                writer.close(); return
            if opcode == 0x8:
                writer.write(frame(0x8, b"")); writer.close(); return
            writer.write(frame(opcode, payload)); await writer.drain()
    server = await asyncio.start_server(echo, "127.0.0.1", 0); port = server.sockets[0].getsockname()[1]
    ws = await m.WebSocketClient.connect(f"ws://127.0.0.1:{port}/stream?streams=a@trade", {"X-Api-Key": "k"})
    assert got["request"] == "GET /stream?streams=a@trade HTTP/1.1"
    await ws.send_text("hello 你好"); assert await ws.recv(timeout=5) == "hello 你好"
    await ws.send_text("ping me"); await ws.send_text("after"); assert await ws.recv(timeout=5) == "after"  # the ping was answered on the way
    await asyncio.sleep(0.05); assert got["pong"] == b"hb"
    await ws.send_text("fragments"); assert await ws.recv(timeout=5) == "片段"
    await ws.send_text("long"); assert await ws.recv(timeout=5) == "x" * 300 and await ws.recv(timeout=5) == "y" * 70000
    await ws.send_text("a" * 70000); assert await ws.recv(timeout=5) == "a" * 70000  # a long frame out (127 length), masked
    await ws.send_text("bye"); assert await ws.recv(timeout=5) is None and ws.closed
    await asyncio.sleep(0.05); assert got["closed"]
    try: await ws.send_text("x"); assert False
    except m.RemoteError: pass
    await ws.close()
    # a refused upgrade
    async def refuse(reader, writer):
        await handshake(reader, writer, "404"); writer.close()
    bad = await asyncio.start_server(refuse, "127.0.0.1", 0); bport = bad.sockets[0].getsockname()[1]
    try: await m.WebSocketClient.connect(f"ws://127.0.0.1:{bport}/"); assert False
    except m.RemoteError as e: assert "握手失败" in str(e) and "404" in str(e), e
    bad.close()
    # masking is its own inverse and handles any length
    for n in (0, 1, 3, 4, 5, 1000):
        data = bytes(range(256)) * 4; data = data[:n]
        assert m.ws_mask(m.ws_mask(data, b"\x01\x02\x03\x04"), b"\x01\x02\x03\x04") == data

    # --- the feed: connects, takes messages, reconnects after the peer closes (a growing pause), stops when asked
    conns = {"n": 0}
    async def pusher(reader, writer):
        await handshake(reader, writer); conns["n"] += 1
        writer.write(frame(0x1, json.dumps({"n": conns["n"]}).encode()) + frame(0x1, b"not json") + frame(0x8, b"")); await writer.drain()
        with __import__("contextlib").suppress(Exception): await read_frame(reader)
        writer.close()
    push = await asyncio.start_server(pusher, "127.0.0.1", 0); pport = push.sockets[0].getsockname()[1]

    class Feed(m.StreamFeed):
        NAME = "test"; PAUSE_SECONDS = 0.05; MAX_PAUSE = 0.2
        def __init__(self): super().__init__(); self.seen = []; self.opened = 0; self.ticks = 0
        def url(self): return f"ws://127.0.0.1:{pport}/x"
        async def on_open(self, ws): self.opened += 1
        async def on_tick(self, ws): self.ticks += 1
        def on_message(self, text): self.seen.append(json.loads(text)["n"])  # "not json" raises: ignored, the feed goes on
    feed = Feed(); stopping = asyncio.Event()
    assert feed.status() == "未连接" and not feed.live()
    task = asyncio.create_task(feed.run(stopping))
    for _ in range(200):
        if conns["n"] >= 3: break
        await asyncio.sleep(0.05)
    assert conns["n"] >= 3 and feed.seen[:3] == [1, 2, 3] and feed.opened >= 3 and feed.ticks >= 3, (conns, feed.seen)
    assert feed.error == "连接被对方关闭" or feed.error == "" , feed.error  # between connections the last failure shows
    stopping.set(); await asyncio.wait_for(task, 5); assert feed.ws is None
    push.close(); server.close()
    # a feed that wants nothing stays disconnected
    class Idle(Feed):
        def wanted(self): return False
    idle = Idle(); stop2 = asyncio.Event(); t2 = asyncio.create_task(idle.run(stop2)); await asyncio.sleep(0.2); assert idle.opened == 0
    stop2.set(); await asyncio.wait_for(t2, 5)

    # --- Binance: the stream's rows stand in for the two requests while it is live and has seen every symbol trade
    cfg = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "SYMBOLS": "UNITREEUSDT,CXMTUSDT"})
    assert cfg.poll == 1 and cfg.binance_stream and cfg.predict_stream
    off = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "BINANCE_STREAM": "off", "PREDICT_STREAM": "0", "HL_STREAM": "no"})
    assert not off.binance_stream and not off.predict_stream and not off.hl_stream and cfg.hl_stream
    mk = m.Binance(cfg); st = mk.stream
    assert st.url() == "wss://fstream.binance.com/stream?streams=cxmtusdt@aggTrade/unitreeusdt@aggTrade/cxmtusdt@markPrice@1s/unitreeusdt@markPrice@1s/cxmtusdt@depth20@500ms/unitreeusdt@depth20@500ms"
    msg = lambda d: json.dumps({"stream": "x", "data": d})
    st.on_message(msg({"e": "aggTrade", "s": "UNITREEUSDT", "p": "75.5", "T": 1700000000000}))
    assert st.price("UNITREEUSDT") is None  # not live: nothing is taken from it
    class LiveWS: closed = False
    st.ws = LiveWS(); st.last_at = time.monotonic()
    assert st.price("UNITREEUSDT") == ("75.5", 1700000000000) and st.row("CXMTUSDT") is None
    try: await mk.prices(); assert False  # one symbol has not traded yet: REST, which is blocked here
    except m.RemoteError: pass
    st.on_message(msg({"e": "aggTrade", "s": "CXMTUSDT", "p": "48.1", "T": 1700000001000}))
    st.on_message(msg({"e": "markPriceUpdate", "s": "UNITREEUSDT", "p": "75.4", "i": "75.45", "E": 1700000002000}))
    st.on_message(msg({"e": "24hrMiniTicker", "s": "UNITREEUSDT", "c": "99"}))  # another stream kind: ignored
    rows = await mk.prices()
    assert rows == {"UNITREEUSDT": {"symbol": "UNITREEUSDT", "price": "75.5", "time": 1700000000000, "markPrice": "75.4", "markTime": 1700000002000, "indexPrice": "75.45"},
                    "CXMTUSDT": {"symbol": "CXMTUSDT", "price": "48.1", "time": 1700000001000}}, rows
    q = m.Quote.parse(rows["UNITREEUSDT"], "UNITREEUSDT", 1700000003000, 120); assert q.price == D("75.5") and q.index_price == D("75.45")
    st.last_at = time.monotonic() - 11  # quiet for too long: not live, REST again
    assert not st.live() and st.status() == "已连接，等待数据"
    try: await mk.prices(); assert False
    except m.RemoteError: pass
    st.last_at = time.monotonic(); assert st.status().startswith("✓ 0 秒前")
    # the spot pairs: asking once follows the pair from then on; a live price answers without a request
    sp = m.SPOT_STREAM; sp.symbols.clear(); sp.last.clear()
    try: await m.binance_spot("ticker/price", symbol="BNBUSDT"); assert False  # not live: REST (blocked)
    except m.RemoteError: pass
    assert sp.symbols == {"BNBUSDT"} and sp.url() == "wss://stream.binance.com:9443/stream?streams=bnbusdt@aggTrade" and m.spot_cadence("BNBUSDT", 5) == 5
    sp.ws = LiveWS(); sp.last_at = time.monotonic(); sp.on_message(json.dumps({"data": {"e": "aggTrade", "s": "BNBUSDT", "p": "800.1", "T": 1}}))
    assert await m.binance_spot("ticker/price", symbol="bnbusdt") == {"symbol": "BNBUSDT", "price": "800.1"} and m.spot_cadence("BNBUSDT", 5) == 1
    assert m.spot_cadence("SOLUSDT", 5) == 5
    sp.ws = None; sp.symbols.clear(); sp.last.clear()

    # --- the depth stream: the contracts' top 20 levels summed up, live only while the stream is; REST otherwise
    st.on_message(msg({"e": "depthUpdate", "E": 1700000005000, "s": "UNITREEUSDT", "b": [["75.4", "10"], ["74.0", "100"]], "a": [["75.6", "10"], ["77.0", "5"]]}))
    book = st.books["UNITREEUSDT"]; assert (book.bid, book.ask, book.within, book.near, book.levels, book.fetched_ms) == (D("75.4"), D("75.6"), D("1510"), D("1510"), 4, 1700000005000)
    assert mk.depth("UNITREEUSDT", 1700000006000) is book and mk.depth("CXMTUSDT", 1700000006000) is None
    assert mk.depth("UNITREEUSDT", 1700000005000 + 16_000) is None  # the stream's book is too old without a newer push
    st.last_at = time.monotonic() - 11; assert mk.depth("UNITREEUSDT", 1700000006000) is None; st.last_at = time.monotonic()
    await mk.refresh_depth("UNITREEUSDT", 1700000006000); assert "UNITREEUSDT" not in mk.depth_notes  # the stream has it: no request
    await mk.refresh_depth("CXMTUSDT", 1700000006000); assert mk.depth_notes["CXMTUSDT"]  # REST, blocked here
    # --- Hyperliquid books over the stream: subscriptions per market, a pushed book summed up for its symbol
    hl = m.Hyperliquid({"HK0625USDT": ("xyz", "SHEIN"), "BTCUSDT": ("", "BTC")}); hs = hl.stream; sent = []
    class SendWS:
        closed = False
        async def send_text(self, text): sent.append(json.loads(text))
    assert hs.url() == "wss://api.hyperliquid.xyz/ws" and hs.wanted()
    await hs.on_open(SendWS())
    assert sent == [{"method": "subscribe", "subscription": {"type": "l2Book", "coin": "xyz:SHEIN"}}, {"method": "subscribe", "subscription": {"type": "l2Book", "coin": "BTC"}}]
    await hs.on_tick(SendWS()); assert len(sent) == 2
    hs.on_message(json.dumps({"channel": "l2Book", "data": {"coin": "xyz:SHEIN", "time": 1700000007000, "levels": [[{"px": "6.15", "sz": "5000", "n": 3}], [{"px": "6.16", "sz": "4000", "n": 2}]]}}))
    assert hl.books["HK0625USDT"].text() == "$55.4K（点差 0.16%）" and hl.books["HK0625USDT"].fetched_ms == 1700000007000
    hs.on_message(json.dumps({"channel": "subscriptionResponse", "data": {}})); hs.on_message(json.dumps({"channel": "l2Book", "data": {"coin": "ETH", "levels": [[], []]}}))
    assert set(hl.books) == {"HK0625USDT"}
    hs.ws = LiveWS(); hs.last_at = time.monotonic()
    assert hl.proxy_price("HK0625USDT", 1700000008000) == (D("6.155"), 1700000007000, "盘口中间价")
    assert hl.proxy_price("HK0625USDT", 1700000007000 + 16_000) is None  # the book is old and there is no quote
    assert not m.Hyperliquid({}).stream.wanted()

    # --- Predict: the live keys' market ids (a ladder's levels too), a pushed book taken by every book and level with
    # that id, the subscriptions sent once per id, a refusal shown
    f = m.PredictFeed(cfg)
    book = lambda key, mid: m.PredictBook(key, f"{key}-slug", mid, "t", ((D("0.4"), D("10")),), ((D("0.5"), D("5")),), 0, 200)
    f.books["HSI"], f.books["KOSPI"], f.books["BTC"] = book("HSI", "m1"), book("KOSPI", "m2"), book("BTC", "m3")
    f.ladders["NIULAI"] = [m.LadderRow(D(200), "m4", "$200M", book("NIULAI", "m4")), m.LadderRow(D(300), "m5", "$300M", None)]
    assert f.live_ids() == []
    f.live_keys = frozenset({"HSI", "NIULAI", "KOSPI"})
    assert f.live_ids() == ["m1", "m2", "m4", "m5"]
    assert f.apply_book("m1", {"marketId": "m1", "bids": [{"price": "0.41", "quantity": "20"}], "asks": [["0.49", "7"]], "timestamp": 1}) == 1
    assert f.books["HSI"].bids == ((D("0.41"), D("20")),) and f.books["HSI"].asks == ((D("0.49"), D("7")),) and f.books["HSI"].fetched_ms > 0
    assert f.books["HSI"].slug == "HSI-slug" and f.books["HSI"].fee_bps == 200 and f.books["KOSPI"].bids == ((D("0.4"), D("10")),)  # the rest untouched
    assert f.apply_book("m4", {"orderbook": {"bids": [], "asks": [["0.9", "1"]]}}) == 1 and f.ladders["NIULAI"][0].book.asks == ((D("0.9"), D("1")),) and f.ladders["NIULAI"][0].book.bids == ()
    assert f.apply_book("m5", {"bids": [["0.1", "1"]], "asks": []}) == 0  # a level without a book yet: nothing to update
    assert f.apply_book("m1", {"marketId": "m1"}) == 0 and f.apply_book("zz", {"bids": [], "asks": []}) == 0 and f.apply_book("m1", "x") == 0
    ps = f.stream; sent = []
    class SendWS:
        closed = False
        async def send_text(self, text): sent.append(json.loads(text))
    assert ps.url() == "wss://ws.predict.fun/ws" and ps.wanted() and ps.headers() == {}
    await ps.on_open(SendWS())
    assert sent == [{"method": "subscribe", "requestId": i + 1, "params": [f"predictOrderbook/{mid}"]} for i, mid in enumerate(["m1", "m2", "m4", "m5"])]
    await ps.on_tick(SendWS()); assert len(sent) == 4  # nothing new
    f.books["SSE"] = book("SSE", "m6"); f.live_keys = f.live_keys | {"SSE"}; await ps.on_tick(SendWS())
    assert sent[-1] == {"method": "subscribe", "requestId": 5, "params": ["predictOrderbook/m6"]}
    ps.on_message(json.dumps({"type": "M", "topic": "predictOrderbook/m2", "data": {"bids": [["0.33", "3"]], "asks": []}}))
    assert f.books["KOSPI"].bids == ((D("0.33"), D("3")),)
    ps.on_message(json.dumps({"type": "R", "requestId": 1, "success": True})); assert ps.error == ""
    ps.on_message(json.dumps({"type": "R", "requestId": 2, "error": "unauthorized"})); assert ps.status() == "✗ 订阅被拒：unauthorized"
    ps.on_message("[1, 2]"); ps.on_message(json.dumps({"type": "M", "topic": "other/1", "data": {}}))  # ignored
    key = m.PredictFeed(m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "PREDICT_API_KEY": "abc"}))
    assert key.stream.headers() == {"x-api-key": "abc"}

    # --- the bot: which streams run, the /status line, the live keys handed to the feed each round
    class FT:
        async def call(self, *a, **k): return True
        async def send(self, *a, **k): pass
    bot = m.Bot(cfg, m.Store(":memory:"), mk, FT())
    assert [n for n, _ in bot.streams()] == ["币安合约", "币安现货", "Predict 盘口", "HL 盘口"]
    assert bot.stream_line() == "📶 实时推送：币安合约 ✓ 0 秒前｜币安现货 未连接｜Predict 盘口 未连接｜HL 盘口 未连接"
    assert "📶 实时推送：币安合约 ✓" in bot.status("1:0")
    bot.watch_update(["HSI"], None)
    assert bot.predict_live_keys() == frozenset({"HSI", "KOSPI", "SSE", "UNITREEUSDT", "CXMTUSDT"}) and bot.predict.live_keys == bot.predict_live_keys()
    quiet = m.Bot(off, m.Store(":memory:"), m.Binance(off), FT())
    assert quiet.streams() == [] and quiet.stream_line() == "" and "📶" not in quiet.status("1:0")
    only = m.Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "BINANCE_STREAM": "off", "HL_STREAM": "off"})
    assert [n for n, _ in m.Bot(only, m.Store(":memory:"), m.Binance(only), FT()).streams()] == ["Predict 盘口"]
    print("STREAM_OK")


asyncio.run(run())
