import os
import requests
import time

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

def send_message(text, reply_to=None):
    payload = {
        "chat_id": GROUP_CHAT_ID,
        "text": text
    }
    if reply_to:
        payload["reply_to_message_id"] = reply_to

    r = requests.post(f"{API}/sendMessage", json=payload, timeout=20)
    r.raise_for_status()
    return r.json()["result"]["message_id"]

def send_ticker_with_reply(ticker: str):
    ticker = ticker.upper().strip()

    # 1️⃣ oddiy ticker
    msg_id = send_message(ticker)
    time.sleep(1)

    # 2️⃣ o‘sha xabarga reply qilib yana ticker
    send_message(ticker, reply_to=msg_id)

if __name__ == "__main__":
    # TEST
    send_ticker_with_reply("BTC")
