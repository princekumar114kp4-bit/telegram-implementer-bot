"""Simple reminder bot (test sample: nothing extra added yet)."""
import time

import telebot

BOT_TOKEN = "123456:FAKE-TOKEN-FOR-TEST-ONLY"
bot = telebot.TeleBot(BOT_TOKEN)


@bot.message_handler(commands=["start"])
def start(message):
    bot.reply_to(message, "Hello, reminder bot ready.")


def main():
    print("sample bot running", flush=True)
    time.sleep(120)


if __name__ == "__main__":
    main()
