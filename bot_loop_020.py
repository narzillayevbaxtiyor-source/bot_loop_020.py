import os
import time
import math
import json
import asyncio
import logging
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

import requests
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ===================== LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("scalp-bot")

# ===================== ENV =====================
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
CHAT_ID = int((os.getenv("CHAT_ID") or "0").strip() or "0")  # user yoki group id (-100...)
INTERVAL = (os.getenv("INTERVAL") or "3m").strip()          # 3m
TOP_N = int(os.getenv("TOP_N", "10"))                       # top gainers count
SCAN_EVERY_SEC = int(os.getenv("SCAN_EVERY_SEC", "30"))     # scan frequency
KLINE_LIMIT = int(os.getenv("KLINE_LIMIT", "200"))

STATE_FILE = os.getenv("STATE_FILE", "state.json")

BINANCE_BASE = "https://api.binance.com"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment yo‘q. Render -> Environment ga qo‘ying.")
if CHAT_ID == 0:
    raise RuntimeError("CHAT_ID environment yo‘q. Render -> Environment ga user/group id qo‘ying.")

# ===================== HELPERS =====================
def safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default

def http_get(url: str, params: dict = None, timeout: int = 15):
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()

@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    close_time: int

    @property
    def is_green(self) -> bool:
        return self.close >= self.open

    @property
    def is_red(self) -> bool:
        return self.close < self.open

def fetch_klines(symbol: str, interval: str, limit: int) -> List[Candle]:
    data = http_get(
        f"{BINANCE_BASE}/api/v3/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=15
    )
    candles: List[Candle] = []
    for k in data:
        candles.append(
            Candle(
                open_time=int(k[0]),
                open=safe_float(k[1]),
                high=safe_float(k[2]),
                low=safe_float(k[3]),
                close=safe_float(k[4]),
                close_time=int(k[6]),
            )
        )
    return candles

def fetch_top_gainers_symbols(top_n: int) -> List[str]:
    """
    Binance 24h ticker dan TOP gainers (spot) oladi.
    Faqat USDT juftliklar.
    """
    tickers = http_get(f"{BINANCE_BASE}/api/v3/ticker/24hr", timeout=20)
    usdt = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        # stable-stable yoki aniq noaniq juftliklarni qisqartiramiz
        if sym.endswith("BUSDUSDT") or sym.endswith("USDCUSDT") or sym.endswith("TUSDUSDT"):
            continue
        pct = safe_float(t.get("priceChangePercent", 0))
        # Ba’zan juda kichik bo‘ladi; baribir qo‘yamiz
        usdt.append((sym, pct))
    usdt.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in usdt[:top_n]]

# ===================== STRATEGY STATE =====================
# Setup:
# 1) Make a high:
#    - Har qanday QIZIL shamdan keyin YASHIL sham paydo bo‘lsa: make-high START.
#    - Yashil shamlar ketma-ketligi (>=1) tugagach QIZIL sham chiqadi.
#    - Keyin narx shu qizil shamning MINIMUMini (low) kesib o‘tsa: make-high END.
#
# 2) Pullback:
#    - Make-high END bo‘lgandan keyin qizil shamlar (>=1) boshlangan davr.
#    - BUY: Pullback boshlanganidan keyin "oxirgi yopilgan shamning maksimumini" narx kesib o‘tsa
#      -> biz buni "current close > previous candle high" bilan tekshiramiz (faqat yopilgan sham).
#    - Pullback sharti: Pullback hech qachon Make-high START nuqtasidan pastga tushmasligi kerak.
#
# 3) SELL:
#    - BUY bo‘lgandan keyin "oxirgi yopilgan sham" dan so‘ng narx shu sham MINIMUMini yangilasa
#      -> biz buni "current close < previous candle low" bilan tekshiramiz (faqat yopilgan sham).

@dataclass
class SymbolState:
    phase: str = "IDLE"  # IDLE | MAKEHIGH | WAIT_END_BREAK | PULLBACK | IN_TRADE
    makehigh_start_low: float = 0.0
    last_green_close_time: int = 0

    end_red_low: float = 0.0     # make-high tugatadigan qizil sham low
    pullback_started: bool = False

    last_signal: str = ""        # "BUY" or "SELL"
    last_signal_time: int = 0

    buy_price: float = 0.0

def load_state() -> Dict[str, SymbolState]:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        st = {}
        for sym, obj in raw.items():
            st[sym] = SymbolState(**obj)
        return st
    except Exception:
        return {}

def save_state(state: Dict[str, SymbolState]) -> None:
    try:
        raw = {k: asdict(v) for k, v in state.items()}
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("state save xato: %s", e)

# ===================== SIGNAL ENGINE =====================
def eval_symbol(symbol: str, candles: List[Candle], st: SymbolState) -> Tuple[Optional[str], SymbolState]:
    """
    Return: (signal, new_state)
    signal: "BUY" | "SELL" | None
    """
    if len(candles) < 5:
        return None, st

    # Biz faqat YOPILGAN shamlar bilan ishlaymiz:
    # current = oxirgi yopilgan sham
    # prev = undan oldingi yopilgan sham
    current = candles[-1]
    prev = candles[-2]

    # --------------- PHASE: IDLE -> MAKEHIGH start ---------------
    if st.phase == "IDLE":
        # "har qanday qizil shamdan keyin yashil" -> start
        if prev.is_red and current.is_green:
            st.phase = "MAKEHIGH"
            st.makehigh_start_low = current.low  # start nuqtasi (pastga tushmasligi shart)
            st.last_green_close_time = current.close_time
        return None, st

    # --------------- PHASE: MAKEHIGH ---------------
    if st.phase == "MAKEHIGH":
        # yashil davom etsa - davom
        if current.is_green:
            st.last_green_close_time = current.close_time
            # start low ni eng pastga tushirmaymiz (birinchi start pastligi qolsin)
            return None, st

        # yashildan keyin qizil paydo bo‘ldi -> end uchun low break kutamiz
        if prev.is_green and current.is_red:
            st.phase = "WAIT_END_BREAK"
            st.end_red_low = current.low
            return None, st

        return None, st

    # --------------- PHASE: WAIT_END_BREAK (make-high end confirmation) ---------------
    if st.phase == "WAIT_END_BREAK":
        # "narx shu qizil sham minimumini kesib o'tishi" -> current close < end_red_low
        # (faqat yopilgan sham bilan)
        if current.close < st.end_red_low:
            # make-high tugadi, pullback boshlanadi
            st.phase = "PULLBACK"
            st.pullback_started = True
            return None, st
        # Agar yana yashilga qaytsa, makehigh davom etyapti deb hisoblaymiz
        if current.is_green:
            st.phase = "MAKEHIGH"
            st.last_green_close_time = current.close_time
        return None, st

    # --------------- PHASE: PULLBACK ---------------
    if st.phase == "PULLBACK":
        # pullback sharti: pastga makehigh_start_low dan tushib ketmasin
        if current.low < st.makehigh_start_low:
            # setup buzildi, reset
            st.phase = "IDLE"
            st.pullback_started = False
            st.end_red_low = 0.0
            return None, st

        # BUY: current close > previous candle high
        # ("oxirgi yopilgan sham maksimumini narx kesib o'tishi")
        if current.close > prev.high:
            st.phase = "IN_TRADE"
            st.buy_price = current.close
            st.last_signal = "BUY"
            st.last_signal_time = current.close_time
            return "BUY", st

        return None, st

    # --------------- PHASE: IN_TRADE ---------------
    if st.phase == "IN_TRADE":
        # SELL: current close < previous candle low
        if current.close < prev.low:
            st.phase = "IDLE"
            st.pullback_started = False
            st.end_red_low = 0.0
            st.last_signal = "SELL"
            st.last_signal_time = current.close_time
            return "SELL", st
        return None, st

    return None, st

def pretty_signal(symbol: str, signal: str, price: float, interval: str, st: SymbolState) -> str:
    if signal == "BUY":
        return (
            f"✅ <b>{symbol}</b>\n"
            f"📌 <b>BUY</b> ({interval})\n"
            f"💰 Price: <b>{price}</b>\n"
            f"🧠 Setup: Make-high → Pullback → break(prev high)\n"
        )
    else:
        return (
            f"🔻 <b>{symbol}</b>\n"
            f"📌 <b>SELL</b> ({interval})\n"
            f"💰 Price: <b>{price}</b>\n"
            f"🧠 Rule: after BUY → break(prev low)\n"
        )

# ===================== TELEGRAM BOT =====================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "✅ Scalp bot ishga tushdi.\n\n"
        f"• Interval: <b>{INTERVAL}</b>\n"
        f"• Top gainers: <b>{TOP_N}</b>\n"
        f"• Scan: har <b>{SCAN_EVERY_SEC}s</b>\n\n"
        "Bot signallarni shu chatga yuboradi."
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML)

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st: Dict[str, SymbolState] = context.application.bot_data.get("state", {})
    sym_list = context.application.bot_data.get("symbols", [])
    lines = [
        f"📊 Status\n• Symbols: {len(sym_list)}\n• Interval: {INTERVAL}\n"
    ]
    # kichik ko‘rinish
    show = 10
    for s in sym_list[:show]:
        ss = st.get(s)
        if ss:
            lines.append(f"• {s}: {ss.phase}")
    await update.message.reply_text("\n".join(lines))

async def scan_job(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    state: Dict[str, SymbolState] = app.bot_data.get("state", {})
    # 1) top gainers yangilash
    try:
        symbols = fetch_top_gainers_symbols(TOP_N)
        app.bot_data["symbols"] = symbols
    except Exception as e:
        log.warning("top gainers olish xato: %s", e)
        return

    symbols = app.bot_data.get("symbols", [])
    if not symbols:
        return

    any_changed = False

    for sym in symbols:
        st = state.get(sym) or SymbolState()

        try:
            candles = fetch_klines(sym, INTERVAL, KLINE_LIMIT)
        except Exception as e:
            log.warning("klines xato %s: %s", sym, e)
            continue

        # faqat yopilgan shamlar: Binance klines qaytarishda oxirgi sham yopilmagan bo‘lishi mumkin
        # shuning uchun oxirgi sham close_time > now bo‘lsa tashlaymiz
        now_ms = int(time.time() * 1000)
        if candles and candles[-1].close_time > now_ms:
            candles = candles[:-1]
        if len(candles) < 5:
            continue

        signal, new_st = eval_symbol(sym, candles, st)
        state[sym] = new_st
        any_changed = True

        if signal:
            price = candles[-1].close
            msg = pretty_signal(sym, signal, price, INTERVAL, new_st)
            try:
                await app.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode=ParseMode.HTML)
            except Exception as e:
                log.warning("telegram send xato: %s", e)

    app.bot_data["state"] = state
    if any_changed:
        save_state(state)

async def on_startup(app: Application):
    # state yuklash
    st = load_state()
    app.bot_data["state"] = st
    app.bot_data["symbols"] = []
    log.info("Application started. State loaded: %d symbols", len(st))

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CommandHandler("status", status_cmd))

    app.post_init = on_startup

    # JobQueue: har SCAN_EVERY_SEC da scan
    app.job_queue.run_repeating(scan_job, interval=SCAN_EVERY_SEC, first=5)

    log.info("🤖 Bot ishga tushdi (polling). Interval=%s TOP_N=%d", INTERVAL, TOP_N)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
