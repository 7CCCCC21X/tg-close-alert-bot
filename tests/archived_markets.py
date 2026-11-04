"""Markets that have ended and were taken out of main.py (TOUCH / UPDOWN / FLIP / CAP / RANGE_MARKETS), kept here as fixtures.

offline.py puts them back into main before any suite runs, so the 涨跌 / 反超 / 市值阶梯 cards and the monthly price ladder keep
their test coverage (and the fixtures, times and card lists the suites were written against) while the bot no longer shows them.
"""
import main
from main import D, CapSpec, FlipSpec, RangeSpec, TouchSpec, UpDownSpec, et_ms

UPDOWN_MARKETS = (
    # "the Binance 1 minute candle for BTC/USDT Sep 30 '26 11:59 PM in the ET timezone" against the one for
    # "Oct 31 '26 11:59 PM ET" (both EDT: daylight time ends on Nov 1)
    UpDownSpec("BTC-2026-10", "btc-up-down-october-2026", "BTCUSDT", "BTC 10月涨跌",
               et_ms(2026, 9, 30, 23, 59, -4), et_ms(2026, 10, 31, 23, 59, -4)),
    # the same two minutes on ETH/USDT ("ETH closed September at $2,696.07 on Binance": the starting candle's close)
    UpDownSpec("ETH-2026-10", "eth-up-down-october-2026", "ETHUSDT", "ETH 10月涨跌",
               et_ms(2026, 9, 30, 23, 59, -4), et_ms(2026, 10, 31, 23, 59, -4)),
)

FLIP_MARKETS = (
    # "at any point between October 2, 2026, 04:00 AM ET and October 31, 2026, 11:59 PM ET" (both EDT): the HYPE/USDC
    # and SOL/USDC 1-minute closes with the same timestamp on Hyperliquid
    FlipSpec("HYPE-SOL", "will-hype-flip-sol-by-nov-26", "HYPE 反超 SOL", "HYPE", "SOL",
             et_ms(2026, 10, 2, 4, 0, -4), et_ms(2026, 10, 31, 23, 59, -4)),
)

CAP_MARKETS = (
    # "from 11:30 PM ET on August 16, 2026 to 11:59 PM ET on October 31, 2026" (both EDT); settles on Flap.sh
    CapSpec("NIULAI", "what-marketcap-will-niu-lai-hit-before-nov-2026", "$牛来 市值", "0xbeea1d618e533a387d941f58a7d4c9b7bd377777",
            et_ms(2026, 8, 16, 23, 30, -4), et_ms(2026, 10, 31, 23, 59, -4),
            (D("2e8"), D("3e8"), D("5e8"), D("1e9")), "Predict 交易至北京 11-01 07:59"),
    # "between market creation on August 17, 13:00 PM ET to 11:59 PM ET on October 31, 2026"; FDV = price × total supply
    CapSpec("ANSEM", "what-fdv-will-ansem-hit-before-nov-2026", "$ANSEM FDV", "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump",
            et_ms(2026, 8, 17, 13, 0, -4), et_ms(2026, 10, 31, 23, 59, -4),
            tuple(D(f"{n}e8") for n in (5, 6, 7, 8, 9)) + (D("1e9"),),
            chain="solana", supply="fdv", gecko="solana", metric="FDV", settle="pump.fun"),
    # "between market creation on August 31, 2026 at 06:00 AM ET to October 31, 2026 at 11:59 PM ET"; the rules' own
    # source is this DexScreener pair; FDV = (total supply − burned) × price
    CapSpec("PONS", "what-fdv-will-pons-hit-before-nov-2026", "$PONS FDV", "0x39dBED3a2bd333467115dE45665cC57F813C4571",
            et_ms(2026, 8, 31, 6, 0, -4), et_ms(2026, 10, 31, 23, 59, -4),
            (D("7e8"), D("8e8"), D("9e8"), D("1e9")),
            chain="robinhood", pair="0x10cc6bd38112cac182db90b6a71d8bb5939526ba", supply="fdv", gecko="",
            metric="FDV", settle="DexScreener"),
    # three more Robinhood-chain FDV ladders settled on the rules' own DexScreener pairs (FDV = (total − burned) × price);
    # their levels are not listed here: the card shows the ones Predict's market titles carry
    # "between market creation on September 4 at 6:00 AM ET, 2026 to October 31, 2026 at 11:59 PM ET" (MEME/USDG)
    CapSpec("MEME", "what-fdv-will-meme-hit-before-november-2026", "$MEME FDV", "0x385F4f8ae47651ce5F58F5265395a669f8281e18",
            et_ms(2026, 9, 4, 6, 0, -4), et_ms(2026, 10, 31, 23, 59, -4), (),
            chain="robinhood", pair="0xc6e298e137f2905398db87e6eae49ede64d231fee37330fa433fec917f4618b6", supply="fdv", gecko="",
            metric="FDV", settle="DexScreener"),
    # "between market creation on September 2, 2026 at 01:00 AM ET to October 31, 2026 at 11:59 PM ET" (CASHCAT/WETH)
    CapSpec("CASHCAT", "what-fdv-will-cashcat-hit-before-november-2026", "$CASHCAT FDV", "0x020bfC650A365f8BB26819deAAbF3E21291018b4",
            et_ms(2026, 9, 2, 1, 0, -4), et_ms(2026, 10, 31, 23, 59, -4), (),
            chain="robinhood", pair="0xa70fc67c9f69da90b63a0e4c05d229954574e313", supply="fdv", gecko="", metric="FDV", settle="DexScreener"),
    # "between market creation on September 1, 2026 at 5:00 AM ET to October 31, 2026 at 11:59 PM ET" (the AI pair)
    CapSpec("AI", "what-fdv-will-ai-hit-before-nov-2026", "$AI FDV", "0x2E8c31162b855A2ffa90F6F8634643Ad6F111e18",
            et_ms(2026, 9, 1, 5, 0, -4), et_ms(2026, 10, 31, 23, 59, -4), (),
            chain="robinhood", pair="0xcbdfea90430a30ee4469c9902e120a77e7c7e4711d5643671c1d1957f2f1ce27", supply="fdv", gecko="",
            metric="FDV", settle="DexScreener"),
    # "between market creation on September 6 at 04:00 AM ET, 2026 to October 31, 2026 at 11:59 PM ET" (STONK/SOL on Solana);
    # the rules' own DexScreener pair settles it, FDV = (total − burned) × price; the pair is spelt as the rules' link spells it
    # (DexScreener reads it either way), which GeckoTerminal would not accept as a pool path; the hourly bars (σ, the window's
    # high since 09-06) come from the mint's pool whose address matches it without regard to case, else its most liquid pool
    CapSpec("STONK", "what-fdv-will-stonk-hit-before-november-2026", "$STONK FDV", "6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rUJHacpMpUNgx",
            et_ms(2026, 9, 6, 4, 0, -4), et_ms(2026, 10, 31, 23, 59, -4), (),
            chain="solana", pair="afrddtgywcveqb1gxcahr8i48o6qtxyqksdvkeludehg", supply="fdv", gecko="solana", metric="FDV", settle="DexScreener"),
    # "between market creation on September 1, 2026 at 3:45 AM ET to October 31, 2026 at 11:59 PM ET" (STONKBROKER/WETH)
    CapSpec("STONKBROKER", "what-fdv-will-stonkbroker-hit-by-november-2026", "$STONKBROKER FDV", "0xe934e36A439C94017B64a3FecE66AF12099aBF50",
            et_ms(2026, 9, 1, 3, 45, -4), et_ms(2026, 10, 31, 23, 59, -4), (),
            chain="robinhood", pair="0xd33c8fd38b06e989cdbd4dffdefab71c4bdd415b24964c8d69e38ff35b068f92", supply="fdv", gecko="",
            metric="FDV", settle="DexScreener"),
)

RANGE_OCTOBER = (
    # "from 00:00 AM ET on the first day to 11:59 PM ET on the last" (October: EDT throughout); Binance 1m candles
    RangeSpec("BTC-HIT-10", "what-price-will-bitcoin-hit-in-october-2026", "BTCUSDT", "spot", "BTC 10月价格",
              et_ms(2026, 10, 1, 0, 0, -4), et_ms(2026, 10, 31, 23, 59, -4), "down"),
    RangeSpec("ETH-HIT-10", "what-price-will-ethereum-hit-in-october-2026", "ETHUSDT", "spot", "ETH 10月价格",
              et_ms(2026, 10, 1, 0, 0, -4), et_ms(2026, 10, 31, 23, 59, -4)),
    RangeSpec("SOL-HIT-10", "what-price-will-solana-hit-in-october-2026", "SOLUSDT", "spot", "SOL 10月价格",
              et_ms(2026, 10, 1, 0, 0, -4), et_ms(2026, 10, 31, 23, 59, -4)),
    # HYPEUSDT on Binance futures (the rules link binance.com/en/futures/HYPEUSDT)
    RangeSpec("HYPE-HIT-10", "what-price-will-hyperliquid-hit-in-october-2026", "HYPEUSDT", "futures", "HYPE 10月价格",
              et_ms(2026, 10, 1, 0, 0, -4), et_ms(2026, 10, 31, 23, 59, -4)),
)


TOUCH_BTC = (
    # "between August 25th, 2026 at 10:00 AM ET and October 25th, 2026 at 11:59 PM ET" (both EDT)
    TouchSpec("BTC", "will-btc-hit-70000-or-90000-first", "BTCUSDT", D("70000"), D("90000"),
              et_ms(2026, 8, 25, 10, 0, -4), et_ms(2026, 10, 25, 23, 59, -4), -4, fixed_start=True),
)


TOUCH_BNB = (
    TouchSpec("BNB", "will-bnb-hit-700-or-900", "BNBUSDT", D("700"), D("900")),
)


def install() -> None:
    """Put the ended markets back into main, in their old order (the October ladders before the live ones)."""
    if any(s.key == RANGE_OCTOBER[0].key for s in main.RANGE_MARKETS):
        return
    main.UPDOWN_MARKETS, main.FLIP_MARKETS, main.CAP_MARKETS = UPDOWN_MARKETS, FLIP_MARKETS, CAP_MARKETS
    main.RANGE_MARKETS = (*RANGE_OCTOBER, *main.RANGE_MARKETS)
    main.TOUCH_MARKETS = (*TOUCH_BNB, main.TOUCH_MARKETS[0], *TOUCH_BTC, *main.TOUCH_MARKETS[1:])  # BNB, SOL, BTC, ETH
