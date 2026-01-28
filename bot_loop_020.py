import os
import json
import time
import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, Optional, List, Tuple

import aiohttp
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("top10-setup-bot")

# ===================== ENV =====================
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
CHAT_ID = (os.getenv("CHAT_ID") or "").strip()  # -100... (group) yoki user id
INTERVAL = (os.getenv("INTERVAL") or "3m").strip()

TOP_N = int(os.getenv("TOP_N") or "10")
REFRESH_SEC = int(os.getenv("REFRESH_SEC") or "120")  # top10 yangilash
QUOTE = (os.getenv("QUOTE") or "USDT").strip().upper()

BINANCE_REST = (os.getenv("BINANCE_REST") or "https://api.binance.com").strip()
BINANCE_WS_BASE = (os.getenv("BINANCE_WS_BASE") or "wss://stream.binance.com:9443").strip()
# Agar Saudi/VPS blok bo'lsa:
# BINANCE_REST=https://data-api.binance.vision
# BINANCE_WS_BASE=wss://data-stream.binance.vision

SCAN_LOG_EVERY_SEC = int(os.getenv("SCAN_LOG_EVERY_SEC") or "0")  # 0=off

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env yo‘q")
if not CHAT_ID:
    raise RuntimeError("CHAT_ID env yo‘q")

# ===================== STRATEGY =====================
def is_green(open_: float, close: float) -> bool:
    # Siz: doji ham yashil (A)
    return close >= open_

def is_red(open_: float, close: float) -> bool:
    return close < open_

@dataclass
class Candle:
    t: int
    o: float
    h: float
    l: float
    c: float
    is_final: bool

@dataclass
class SymbolState:
    state: str = "SEARCH_MAKE_HIGH_START"  # SEARCH_MAKE_HIGH_START | IN_MAKE_HIGH | IN_PULLBACK | IN_BUY
    makeHighStartLow: Optional[float] = None
    endRedLow: Optional[float] = None

    last_closed: Optional[Candle] = None
    prev_closed: Optional[Candle] = None

    cycle_no: int = 0
    sell_ref_low: Optional[float] = None

def reset_setup(st: SymbolState):
    st.state = "SEARCH_MAKE_HIGH_START"
    st.makeHighStartLow = None
    st.endRedLow = None
    st.sell_ref_low = None

# ===================== TELEGRAM =====================
app = Application.builder().token(BOT_TOKEN).build()

async def tg_send(text: str):
    try:
        await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        log.warning("Telegram send error: %s", e)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "✅ Bot ishga tushdi.\n\n"
        f"TF: <b>{INTERVAL}</b>\n"
        f"Top gainers: <b>Top {TOP_N} ({QUOTE} spot)</b>\n"
        f"Refresh: <b>{REFRESH_SEC}s</b>\n"
        "Signal: intrabar (A)\n\n"
        "MakeHigh → Pullback → BUY\n"
        "BUY: pullback ichida last_closed.high break\n"
        "SELL: BUY’dan keyin ref_low break (last closed low)"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML)

async def cmd_symbols(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbols = context.application.bot_data.get("symbols", [])
    if not symbols:
        await update.message.reply_text("Hali top gainers olinmadi.")
        return
    await update.message.reply_text("📌 Hozirgi Top gainers:\n" + ", ".join(symbols))

app.add_handler(CommandHandler("start", cmd_start))
app.add_handler(CommandHandler("symbols", cmd_symbols))

# ===================== TOP GAINERS =====================
async def fetch_top_gainers(session: aiohttp.ClientSession) -> List[str]:
    """
    Binance 24hr ticker: /api/v3/ticker/24hr
    spotda percent change bo'yicha top N.
    Filtr: symbol QUOTE bilan tugasin (USDT) va spot bo'lsin (endpoint spot).
    """
    url = f"{BINANCE_REST}/api/v3/ticker/24hr"
    async with session.get(url, timeout=20) as r:
        data = await r.json()

    # data: list of tickers
    rows = []
    for t in data:
        s = (t.get("symbol") or "")
        if not s.endswith(QUOTE):
            continue
        # Leveraged tokenlar (UP/DOWN/BEAR/BULL)ni chiqarib tashlaymiz
        if s.endswith(("UP" + QUOTE, "DOWN" + QUOTE, "BULL" + QUOTE, "BEAR" + QUOTE)):
            continue
        try:
            pct = float(t.get("priceChangePercent", 0.0))
        except:
            continue
        # Volume filt (ixtiyoriy): juda o'lik coinlar bo'lmasin
        try:
            qvol = float(t.get("quoteVolume", 0.0))
        except:
            qvol = 0.0
        rows.append((pct, qvol, s))

    # percent desc, so'ng quoteVolume desc
    rows.sort(key=lambda x: (x[0], x[1]), reverse=True)
    top = [s for _, __, s in rows[:TOP_N]]
    return top

def build_stream_url(symbols: List[str]) -> str:
    streams = "/".join([f"{s.lower()}@kline_{INTERVAL}" for s in symbols])
    return f"{BINANCE_WS_BASE}/stream?streams={streams}"

# ===================== STATE STORE =====================
STATES: Dict[str, SymbolState] = {}

def ensure_states(symbols: List[str]):
    # yangi symbol kirsa state yaratamiz
    for s in symbols:
        if s not in STATES:
            STATES[s] = SymbolState()
    # chiqib ketgan symbol state'ni o'chirmaymiz (ixtiyoriy). xohlasangiz tozalash mumkin.

def on_candle_close(symbol: str, closed: Candle):
    st = STATES[symbol]
    st.prev_closed = st.last_closed
    st.last_closed = closed

    # Make a high start: prev red -> last green
    if st.state == "SEARCH_MAKE_HIGH_START" and st.prev_closed:
        if is_red(st.prev_closed.o, st.prev_closed.c) and is_green(closed.o, closed.c):
            st.state = "IN_MAKE_HIGH"
            st.makeHighStartLow = closed.l
            st.endRedLow = None

    # IN_MAKE_HIGH: birinchi qizil sham candidate
    if st.state == "IN_MAKE_HIGH":
        if is_red(closed.o, closed.c) and st.endRedLow is None:
            st.endRedLow = closed.l

    # BUY holatida sell_ref_low har yopilgan shamda yangilanadi
    if st.state == "IN_BUY":
        st.sell_ref_low = closed.l

async def maybe_trigger_events(symbol: str, current: Candle):
    st = STATES[symbol]
    lc = st.last_closed

    # 1) MakeHigh END: endRedLow bor bo'lsa va intrabar low uni buzsa
    if st.state == "IN_MAKE_HIGH" and st.endRedLow is not None:
        if current.l < st.endRedLow:
            st.state = "IN_PULLBACK"

    # 2) Pullback invalid: pullback makeHighStartLow dan pastga tushmasin
    if st.state == "IN_PULLBACK" and st.makeHighStartLow is not None:
        if current.l < st.makeHighStartLow:
            reset_setup(st)
            return

    # 3) BUY: Pullback ichida last_closed.high break (intrabar)
    if st.state == "IN_PULLBACK":
        if lc is None:
            return
        if current.h > lc.h:
            st.state = "IN_BUY"
            st.cycle_no += 1
            st.sell_ref_low = lc.l
            await tg_send(
                f"🟢 <b>{st.cycle_no} BUY</b>\n"
                f"📌 <b>{symbol}</b> | TF {INTERVAL}\n"
                f"Break: last_closed.high = <b>{lc.h}</b>\n"
                f"Pullback OK"
            )
            return

    # 4) SELL: BUY’dan keyin ref_low break (intrabar)
    if st.state == "IN_BUY":
        ref = st.sell_ref_low if st.sell_ref_low is not None else (lc.l if lc else None)
        if ref is None:
            return
        if current.l < ref:
            await tg_send(
                f"🔴 <b>{st.cycle_no} SELL</b>\n"
                f"📌 <b>{symbol}</b> | TF {INTERVAL}\n"
                f"Break: ref_low = <b>{ref}</b>"
            )
            reset_setup(st)
            return

# ===================== WS LOOP (auto reconnect on top10 change) =====================
async def ws_loop():
    last_symbols: List[str] = []
    last_refresh = 0.0
    ws: Optional[aiohttp.ClientWebSocketResponse] = None
    session: Optional[aiohttp.ClientSession] = None
    last_log = 0.0

    try:
        session = aiohttp.ClientSession()
        while True:
            now = time.time()

            # refresh top gainers
            if (now - last_refresh) >= REFRESH_SEC or not last_symbols:
                try:
                    new_symbols = await fetch_top_gainers(session)
                    last_refresh = now
                    if new_symbols and new_symbols != last_symbols:
                        last_symbols = new_symbols
                        ensure_states(last_symbols)
                        app.bot_data["symbols"] = last_symbols

                        await tg_send("📈 <b>Top gainers yangilandi</b>\n" + ", ".join(last_symbols))

                        # reconnect WS
                        if ws is not None and not ws.closed:
                            await ws.close()
                        ws = None
                except Exception as e:
                    log.warning("Top gainers fetch error: %s", e)

            # ensure WS connected
            if ws is None or ws.closed:
                if not last_symbols:
                    await asyncio.sleep(2)
                    continue
                url = build_stream_url(last_symbols)
                log.info("Connecting WS: %s", url)
                try:
                    ws = await session.ws_connect(url, heartbeat=30)
                    log.info("WS connected")
                except Exception as e:
                    log.warning("WS connect error: %s", e)
                    await asyncio.sleep(3)
                    continue

            # read one message with timeout (so we can refresh periodically)
            try:
                msg = await ws.receive(timeout=5)
            except asyncio.TimeoutError:
                msg = None

            if msg is None:
                continue

            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                payload = data.get("data", {})
                k = payload.get("k", {})
                symbol = (k.get("s") or "").upper()
                if symbol not in STATES:
                    continue

                cndl = Candle(
                    t=int(k.get("t", 0)),
                    o=float(k.get("o", 0)),
                    h=float(k.get("h", 0)),
                    l=float(k.get("l", 0)),
                    c=float(k.get("c", 0)),
                    is_final=bool(k.get("x", False)),
                )

                await maybe_trigger_events(symbol, cndl)

                if cndl.is_final:
                    on_candle_close(symbol, cndl)

                if SCAN_LOG_EVERY_SEC > 0:
                    if time.time() - last_log >= SCAN_LOG_EVERY_SEC:
                        st = STATES[symbol]
                        log.info("[%s] state=%s cycle=%s", symbol, st.state, st.cycle_no)
                        last_log = time.time()

            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                log.warning("WS closed/error, reconnecting...")
                try:
                    await ws.close()
                except:
                    pass
                ws = None
                await asyncio.sleep(2)

    finally:
        if ws is not None and not ws.closed:
            try:
                await ws.close()
            except:
                pass
        if session is not None:
            await session.close()

# ===================== MAIN =====================
async def on_startup(app_: Application):
    await tg_send("✅ Bot ishga tushdi (polling)")

async def main():
    app.post_init = on_startup
    asyncio.get_running_loop().create_task(ws_loop())
    await app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
