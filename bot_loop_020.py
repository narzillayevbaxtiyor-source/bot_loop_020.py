import os
import time
import json
import requests
from typing import Dict, List, Optional, Tuple

# =========================
# START FINGERPRINT
# =========================
print("### BOT_LOOP_020 RUNNING — NO UPDATER — OK ###", flush=True)

# =========================
# CONFIG (ENV)
# =========================
BINANCE_BASE_URL = os.getenv("BINANCE_BASE_URL", "https://data-api.binance.vision").strip()

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))

# Trigger: 24h HIGH ga qolgan masofa (%)
TRIGGER_PCT = float(os.getenv("TRIGGER_PCT", "0.20"))  # 0.20%

# Cooldown: bitta symbol qayta yuborilmasin (sekund)
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", str(30 * 60)))  # default 30 min

# Filters
MIN_QUOTE_VOL = float(os.getenv("MIN_QUOTE_VOL", "2000000"))  # USDT quoteVolume filter
ONLY_USDT = os.getenv("ONLY_USDT", "1") == "1"
PAIR_SUFFIX = os.getenv("PAIR_SUFFIX", "USDT")

# Universe refresh
SPOT_REFRESH_SECONDS = int(os.getenv("SPOT_REFRESH_SECONDS", "3600"))

# Batch scanning (rate limit uchun)
SCAN_BATCH_SIZE = int(os.getenv("SCAN_BATCH_SIZE", "25"))

# Telegram (HTTP API)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# State file (Render diskida saqlanadi; redeploy bo'lsa yangidan boshlashi mumkin)
STATE_FILE = os.getenv("STATE_FILE", "state_loop_020.json").strip()

# Blacklists
BAD_PARTS = ("UPUSDT", "DOWNUSDT", "BULL", "BEAR", "3L", "3S", "5L", "5S")
STABLE_STABLE = {"BUSDUSDT", "USDCUSDT", "TUSDUSDT", "FDUSDUSDT", "DAIUSDT"}

SESSION = requests.Session()

# =========================
# HELPERS
# =========================
def fetch_json(url: str, params=None):
    r = SESSION.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json()

def tg_send(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[NO TELEGRAM ENV]", text, flush=True)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = SESSION.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    if r.status_code != 200:
        print("[TELEGRAM ERROR]", r.status_code, r.text, flush=True)

def kline_to_ohlc_24hr(t: dict) -> Tuple[float, float, float]:
    # lastPrice, highPrice, quoteVolume
    return float(t["lastPrice"]), float(t["highPrice"]), float(t.get("quoteVolume", "0") or "0")

def load_state() -> Dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_sent_ts": {}, "last_universe_refresh": 0, "symbols": []}

def save_state(st: Dict):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

def is_bad_symbol(sym: str) -> bool:
    if sym in STABLE_STABLE:
        return True
    if not sym.endswith(PAIR_SUFFIX):
        return True
    if any(x in sym for x in BAD_PARTS):
        return True
    return False

def remain_pct_to_high(last_price: float, high_price: float) -> float:
    if high_price <= 0:
        return 999.0
    return ((high_price - last_price) / high_price) * 100.0

# =========================
# SPOT USDT UNIVERSE
# =========================
def refresh_universe_if_needed(state: Dict) -> List[str]:
    now = time.time()
    symbols = state.get("symbols", [])
    last_ref = float(state.get("last_universe_refresh", 0))

    if symbols and (now - last_ref) < SPOT_REFRESH_SECONDS:
        return symbols

    info = fetch_json(f"{BINANCE_BASE_URL}/api/v3/exchangeInfo")
    out: List[str] = []

    for s in info.get("symbols", []):
        sym = s.get("symbol", "")
        if not sym:
            continue
        if ONLY_USDT and not sym.endswith(PAIR_SUFFIX):
            continue
        if is_bad_symbol(sym):
            continue
        if s.get("status") != "TRADING":
            continue
        if s.get("quoteAsset") != "USDT":
            continue

        # SPOT check
        permissions = s.get("permissions", [])
        permission_sets = s.get("permissionSets", [])
        is_spot = ("SPOT" in permissions) or any(("SPOT" in ps) for ps in permission_sets)
        if not is_spot:
            continue

        out.append(sym)

    out.sort()
    state["symbols"] = out
    state["last_universe_refresh"] = now
    save_state(state)

    print(f"[UNIVERSE] refreshed symbols={len(out)}", flush=True)
    return out

# =========================
# MAIN LOOP
# =========================
def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ TELEGRAM_TOKEN / TELEGRAM_CHAT_ID qo'yilmagan. Bot faqat logga yozadi.", flush=True)

    state = load_state()
    symbols = refresh_universe_if_needed(state)

    scan_index = 0
    tg_send("✅ 0.20% bot (loop) ishga tushdi. Faqat ticker yuboradi. (NO UPDATER)")

    while True:
        try:
            # refresh universe
            symbols = refresh_universe_if_needed(state)
            if not symbols:
                time.sleep(POLL_SECONDS)
                continue

            # 24hr tickers (1 call) — tezroq
            tickers = fetch_json(f"{BINANCE_BASE_URL}/api/v3/ticker/24hr")

            # symbol -> ticker map
            tmap = {t.get("symbol"): t for t in tickers if t.get("symbol")}

            # batch
            batch: List[str] = []
            for _ in range(min(SCAN_BATCH_SIZE, len(symbols))):
                batch.append(symbols[scan_index])
                scan_index = (scan_index + 1) % len(symbols)

            now = time.time()
            last_sent_ts: Dict[str, float] = state.get("last_sent_ts", {})

            # scan
            for sym in batch:
                t = tmap.get(sym)
                if not t:
                    continue

                try:
                    last_price, high_price, quote_vol = kline_to_ohlc_24hr(t)
                except Exception:
                    continue

                if quote_vol < MIN_QUOTE_VOL:
                    continue
                if last_price <= 0 or high_price <= 0:
                    continue

                rp = remain_pct_to_high(last_price, high_price)
                if rp < 0:
                    continue

                # trigger
                if rp <= TRIGGER_PCT:
                    last = float(last_sent_ts.get(sym, 0.0))
                    if (now - last) < COOLDOWN_SECONDS:
                        continue

                    # 🔹 faqat ticker yuboriladi (USDT olib tashlanadi)
    clean_ticker = sym.replace(PAIR_SUFFIX, "")
    tg_send(clean_ticker)

    last_sent_ts[sym] = now
    state["last_sent_ts"] = last_sent_ts
    save_state(state)

            time.sleep(POLL_SECONDS)

        except Exception as e:
            print("[ERROR]", repr(e), flush=True)
            time.sleep(max(5, POLL_SECONDS))

if __name__ == "__main__":
    main()
