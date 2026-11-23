"""Bot with both a Flask web server and a pinger already (test sample)."""
import os
import threading
import time

import requests
import telebot
from flask import Flask

BOT_TOKEN = "123456:FAKE-TOKEN-FOR-TEST-ONLY"
bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)


@app.route("/health")
def health():
    return "ok"


def run_web():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))


def ping_self():
    while True:
        url = os.environ.get("RENDER_EXTERNAL_URL", "")
        if url:
            requests.get(url, timeout=20)
        time.sleep(5 * 60)


@bot.message_handler(commands=["start"])
def start(message):
    bot.reply_to(message, "Hello from the both bot.")


def main():
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=ping_self, daemon=True).start()
    bot.infinity_polling()


if __name__ == "__main__":
    main()
