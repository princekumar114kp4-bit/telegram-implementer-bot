"""Bot with a keep-alive pinger already, no web server (test sample)."""
import os
import threading
import time

import requests
import telebot

BOT_TOKEN = "123456:FAKE-TOKEN-FOR-TEST-ONLY"
bot = telebot.TeleBot(BOT_TOKEN)


def keep_alive_loop():
    while True:
        url = os.environ.get("RENDER_EXTERNAL_URL", "")
        if url:
            requests.get(url, timeout=20)
        time.sleep(5 * 60)


@bot.message_handler(commands=["start"])
def start(message):
    bot.reply_to(message, "Hello from the pinger bot.")


def main():
    threading.Thread(target=keep_alive_loop, daemon=True).start()
    print("sample bot running", flush=True)
    time.sleep(120)


if __name__ == "__main__":
    main()
