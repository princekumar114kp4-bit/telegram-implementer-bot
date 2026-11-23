"""Bot with a Flask web server already (test sample)."""
import os
import threading

import telebot
from flask import Flask

BOT_TOKEN = "123456:FAKE-TOKEN-FOR-TEST-ONLY"
bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)


@app.route("/")
def home():
    return "ok"


def run_web():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))


@bot.message_handler(commands=["start"])
def start(message):
    bot.reply_to(message, "Hello from the flask bot.")


def main():
    threading.Thread(target=run_web, daemon=True).start()
    bot.infinity_polling()


if __name__ == "__main__":
    main()
