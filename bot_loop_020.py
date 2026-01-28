import os
import json
import time
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, List

import aiohttp
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("setup-bot")

# ===================== ENV =====================
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
CHAT_ID = (os.getenv("CHAT_ID") or "").strip()  # -100... (group) yoki 6248061970 (user)
SYMBOLS_RAW = (os.getenv("SYMBOLS") or "BTCUSDT").strip()  # comma separated
INTERVAL = (os.getenv("INTERVAL") or "3m").strip()
SCAN_LOG_EVERY_SEC = int(os.getenv("SCAN_LOG_EVERY_SEC") or "0")  # 0 = off
BINANCE_WS_BASE = (os.getenv("BINANCE_WS_BASE") or "wss://stream.binance.com:9443").strip()
# Alternativ: wss://data-stream.binance.vision

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env yo‘q")
if not CHAT_ID:
    raise RuntimeError("CHAT_ID env yo‘q")

SYMBOLS = [s.strip().upper() for s in SYMBOLS_RAW.split(",") if s.strip()]
if not SYMBOLS:
    raise RuntimeError("SYMBOLS bo‘sh")

# ===================== STRATEGY STATE =====================
def is_green(open_: float, close: float) -> bool:
    # sizning A javobingiz: close >= open ham yashil (doji ham yashil)
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

    last_closed: Optional[Candle] = None      # oxirgi yopilgan sham
    prev_closed: Optional[Candle] = None      # undan oldingi yopilgan sham

    cycle_no: int = 0                         # 1 BUY, 1 SELL, 2 BUY, 2 SELL...
    last_buy_ts: Optional[int] = None
    last_sell_ts: Optional[int] = None

    # BUY dan keyin SELL uchun referens: oxirgi yopilgan sham low
    sell_ref_low: Optional[float] = None

STATES: Dict[str, SymbolState] = {s: SymbolState() for s in SYMBOLS}

# ===================== TELEGRAM APP =====================
app = Application.builder().token(BOT_TOKEN).build()

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "✅ Setup bot ishga tushdi.\n\n"
        f"Symbols: <b>{', '.join(SYMBOLS)}</b>\n"
        f"TF: <b>{INTERVAL}</b>\n"
        "Signal: intrabar (A)\n\n"
        "Buy: MakeHigh end -> Pullback -> last closed high break\n"
        "Sell: Buy’dan keyin last closed low break"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML)

app.add_handler(CommandHandler("start", cmd_start))

async def tg_send(text: str):
    try:
        await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        log.warning("Telegram send error: %s", e)

# ===================== CORE LOGIC =====================
def reset_setup(st: SymbolState):
    st.state = "SEARCH_MAKE_HIGH_START"
    st.makeHighStartLow = None
    st.endRedLow = None
    st.sell_ref_low = None

def on_candle_close(symbol: str, closed: Candle):
    st = STATES[symbol]
    st.prev_closed = st.last_closed
    st.last_closed = closed

    # START: prev red -> last green
    if st.state == "SEARCH_MAKE_HIGH_START" and st.prev_closed:
        if is_red(st.prev_closed.o, st.prev_closed.c) and is_green(closed.o, closed.c):
            st.state = "IN_MAKE_HIGH"
            st.makeHighStartLow = closed.l
            st.endRedLow = None

    # IN_MAKE_HIGH: birinchi qizil sham candidate
    if st.state == "IN_MAKE_HIGH":
        if is_red(closed.o, closed.c) and st.endRedLow is None:
            st.endRedLow = closed.l

    # BUY dan keyin SELL referens har yangi yopilgan shamda yangilanadi
    if st.state == "IN_BUY":
        st.sell_ref_low = closed.l

async def maybe_trigger_events(symbol: str, current: Candle):
    """
    Intrabar tekshiruv.
    current = hozir shakllanayotgan (yopilmagan) sham ham bo‘lishi mumkin.
    """
    st = STATES[symbol]
    lc = st.last_closed

    # 1) MakeHigh END: endRedLow belgilangan bo'lsa va intrabar low uni buzsa
    if st.state == "IN_MAKE_HIGH" and st.endRedLow is not None:
        if current.l < st.endRedLow:
            st.state = "IN_PULLBACK"
            # endRedLow end bo'ldi, pullback boshlandi

    # 2) Pullback invalidation: pullback makeHighStartLow dan pastga tushmasin
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
            st.last_buy_ts = current.t
            st.sell_ref_low = lc.l  # start reference
            await tg_send(
                f"🟢 <b>{st.cycle_no} BUY</b>\n"
                f"📌 <b>{symbol}</b> | TF {INTERVAL}\n"
                f"Break: last_closed.high = <b>{lc.h}</b>\n"
                f"Pullback OK (min > makeHighStartLow)"
            )
            return

    # 4) SELL: BUY dan keyin last_closed.low break (intrabar)
    if st.state == "IN_BUY":
        ref = st.sell_ref_low if st.sell_ref_low is not None else (lc.l if lc else None)
        if ref is None:
            return
        if current.l < ref:
            st.last_sell_ts = current.t
            await tg_send(
                f"🔴 <b>{st.cycle_no} SELL</b>\n"
                f"📌 <b>{symbol}</b> | TF {INTERVAL}\n"
                f"Break: ref_low = <b>{ref}</b>"
            )
            # SELL dan keyin qaytadan setup qidiramiz
            reset_setup(st)
            return

# ===================== BINANCE WS =====================
def build_stream_url() -> str:
    # combined streams
    streams = "/".join([f"{s.lower()}@kline_{INTERVAL}" for s in SYMBOLS])
    return f"{BINANCE_WS_BASE}/stream?streams={streams}"

async def ws_loop():
    url = build_stream_url()
    log.info("WS URL: %s", url)

    last_log = 0.0
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(url, heartbeat=30) as ws:
                    log.info("WS connected")
                    async for msg in ws:
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

                            # intrabar tekshiruv
                            await maybe_trigger_events(symbol, cndl)

                            # close event
                            if cndl.is_final:
                                on_candle_close(symbol, cndl)

                            # optional heartbeat log
                            if SCAN_LOG_EVERY_SEC > 0:
                                now = time.time()
                                if now - last_log >= SCAN_LOG_EVERY_SEC:
                                    st = STATES[symbol]
                                    log.info("[%s] state=%s cycle=%s", symbol, st.state, st.cycle_no)
                                    last_log = now

                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break

        except Exception as e:
            log.warning("WS error, reconnecting: %s", e)
            await asyncio.sleep(3)

# ===================== MAIN =====================
async def on_startup(app_: Application):
    await tg_send("✅ Bot ishga tushdi (polling)")

async def main():
    app.post_init = on_startup
    # ws background task
    asyncio.get_running_loop().create_task(ws_loop())
    # polling (Render uchun eng oson va barqaror)
    await app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
