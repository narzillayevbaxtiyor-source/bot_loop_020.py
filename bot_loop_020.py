import os
import time
import requests
from typing import List, Dict, Optional

# =========================
# ENV / CONFIG
# =========================
BINANCE_BASE_URL = (os.getenv("BINANCE_BASE_URL") or "https://data-api.binance.vision").strip()

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))          # har nechchi sekundda batch aylansin
SCAN_BATCH_SIZE = int(os.getenv("SCAN_BATCH_SIZE", "25"))    # nechta sym bir aylanishda

SPOT_REFRESH_SECONDS = int(os.getenv("SPOT_REFRESH_SECONDS", "3600"))

# 4H high ga qolgan masofa (%)
TRIGGER_PCT = float(os.getenv("TRIGGER_PCT", "0.20"))        # 0.20%

# bir coin qayta-qayta kelmasin (sekund)
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "1800"))  # 30 min

# Filterlar
ONLY_USDT = (os.getenv("ONLY_USDT", "1").strip() == "1")
MIN_QUOTE_VOL = float(os.getenv("MIN_QUOTE_VOL", "0"))         # xohlasang: 2000000
PAIR_SUFFIX = (os.getenv("PAIR_SUFFIX") or "USDT").strip().upper()

# Telegram
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

# Exclude
BAD_PARTS = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S", "5L", "5S")
STABLE_STABLE = {"BUSDUSDT", "USDCUSDT", "TUSDUSDT", "FDUSDUSDT", "DAIUSDT"}

SESSION = requests.Session()


# =========================
# HELPERS
# =========================
def fetch_json(path: str, params=None):
    url = f"{BINANCE_BASE_URL}{path}"
    r = SESSION.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json()

def tg_send(text: str):
    # Token/chat bo'lmasa -> print
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = SESSION.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True
    }, timeout=20)
    if r.status_code != 200:
        print("[TELEGRAM ERROR]", r.status_code, r.text)

def to_ticker(symbol: str) -> str:
    # masalan BTCUSDT -> BTC
    if symbol.endswith(PAIR_SUFFIX):
        return symbol[:-len(PAIR_SUFFIX)]
    return symbol

def basic_filter(sym: str) -> bool:
    if ONLY_USDT and not sym.endswith(PAIR_SUFFIX):
        return False
    if sym in STABLE_STABLE:
        return False
    # Leveraged tokenlar: UPUSDT, DOWNUSDT, ... va ichida BULL/BEAR va 3L/3S...
    if any(part in sym for part in BAD_PARTS):
        return False
    return True


# =========================
# UNIVERSE
# =========================
def load_usdt_spot_symbols() -> List[str]:
    info = fetch_json("/api/v3/exchangeInfo")
    out = []
    for s in info.get("symbols", []):
        sym = s.get("symbol", "")
        if not sym:
            continue
        if s.get("status") != "TRADING":
            continue
        if ONLY_USDT and s.get("quoteAsset") != "USDT":
            continue
        if not basic_filter(sym):
            continue

        # spot permission
        permissions = s.get("permissions", [])
        permission_sets = s.get("permissionSets", [])
        is_spot = ("SPOT" in permissions) or any(("SPOT" in ps) for ps in permission_sets)
        if not is_spot:
            continue

        out.append(sym)

    out.sort()
    return out


# =========================
# CORE CHECK (4H HIGH remain %)
# =========================
def get_4h_high(symbol: str) -> Optional[float]:
    # ongoing 4h candle (limit=1)
    kl = fetch_json("/api/v3/klines", {"symbol": symbol, "interval": "4h", "limit": 1})
    if not kl:
        return None
    high = float(kl[0][2])
    return high if high > 0 else None

def get_last_price(symbol: str) -> Optional[float]:
    d = fetch_json("/api/v3/ticker/price", {"symbol": symbol})
    return float(d["price"]) if "price" in d else None

def get_24h_quote_volume(symbol: str) -> Optional[float]:
    # 24h stats
    d = fetch_json("/api/v3/ticker/24hr", {"symbol": symbol})
    # quoteVolume = USDT hajm
    try:
        return float(d.get("quoteVolume", "0"))
    except Exception:
        return None


def main():
    tg_send("✅ 0.20 bot: 4H high ga TRIGGER_PCT qolganda guruhga faqat TICKER yuboradi (USDTsiz).")

    symbols: List[str] = []
    last_refresh = 0.0
    scan_index = 0

    last_sent: Dict[str, float] = {}  # symbol -> ts

    while True:
        try:
            now = time.time()

            # refresh universe
            if (not symbols) or (now - last_refresh > SPOT_REFRESH_SECONDS):
                symbols = load_usdt_spot_symbols()
                last_refresh = now
                scan_index = 0
                print(f"[INFO] symbols loaded: {len(symbols)}")

            if not symbols:
                time.sleep(POLL_SECONDS)
                continue

            # batch
            batch = []
            for _ in range(min(SCAN_BATCH_SIZE, len(symbols))):
                batch.append(symbols[scan_index])
                scan_index = (scan_index + 1) % len(symbols)

            for sym in batch:
                # cooldown
                if (now - last_sent.get(sym, 0.0)) < COOLDOWN_SECONDS:
                    continue

                # volume filter
                if MIN_QUOTE_VOL and MIN_QUOTE_VOL > 0:
                    qv = get_24h_quote_volume(sym)
                    if qv is None or qv < MIN_QUOTE_VOL:
                        continue

                high_4h = get_4h_high(sym)
                if not high_4h:
                    continue

                price = get_last_price(sym)
                if not price:
                    continue

                remain_pct = ((high_4h - price) / high_4h) * 100.0

                # 0..TRIGGER_PCT ichida bo'lsa -> signal
                if 0 <= remain_pct <= TRIGGER_PCT:
                    ticker = to_ticker(sym)
                    tg_send(ticker)  # ✅ faqat ticker (BTC)
                    last_sent[sym] = now
                    print(f"[HIT] {sym} remain={remain_pct:.4f}%")

        except Exception as e:
            print("[ERROR]", e)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
