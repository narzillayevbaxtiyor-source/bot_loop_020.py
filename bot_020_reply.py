import os
import asyncio
from telegram import Bot

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID"))

bot = Bot(token=BOT_TOKEN)

async def send_ticker(ticker: str):
    ticker = ticker.upper().strip()

    # 1️⃣ oddiy ticker yuboriladi
    msg = await bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text=ticker
    )

    # 2️⃣ o‘sha xabarga REPLY qilib yana ticker yuboriladi
    await bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text=ticker,
        reply_to_message_id=msg.message_id
    )

async def main():
    # test
    await send_ticker("BTC")

if __name__ == "__main__":
    asyncio.run(main())
