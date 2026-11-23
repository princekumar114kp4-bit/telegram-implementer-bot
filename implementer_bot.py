"""Implementer bot (Hinglish messages).

The owner sends a bot .py file. The bot shows what the file already has (web server, pinger)
and 3 buttons. A tap adds a web server and/or a keep-alive pinger to a NEW copy of that file and
sends the copy back as a document. The original file is never changed.

Buttons (owner only): "Status dekho" shows what the last file has; "Pichli file lo" sends the last
updated file again. Typed /status and /last still work (quietly, no hint is shown).

This bot also keeps itself awake on Render: a small Flask web server ("/" and "/health" answer
"ok") and a pinger that requests its own RENDER_EXTERNAL_URL every PING_MINUTES minutes.

Env:
  BOT_TOKEN       Telegram bot token (required, never printed or stored)
  OWNER_ID        numeric Telegram id of the owner (only this user may use the bot)
  DATA_DIR        optional; where uploads and results are kept (default: data folder next to this file)
  PORT            optional; web server port (Render sets it; default 10000)
  RENDER_EXTERNAL_URL  set by Render for a Web Service; the pinger calls it (skipped when unset)
  PING_MINUTES    optional; minutes between self-pings, 1 to 60 (default 5)
"""

import ast
import logging
import os
import re
import sys
import time
import threading

import telebot
from telebot import types

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_BYTES = 300 * 1024

BTN_WEB = "Web server add karo"
BTN_PINGER = "Pinger add karo"
BTN_BOTH = "Dono add karo"
OPTIONS = (BTN_WEB, BTN_PINGER, BTN_BOTH)
BTN_STATUS = "Status dekho"      # same as the typed /status
BTN_LAST = "Pichli file lo"      # same as the typed /last
INFO_BUTTONS = (BTN_STATUS, BTN_LAST)
RESULT_NOTE = "ℹ️ Ye naya file hai. Ise Render par khud dobara deploy karna hoga. Purani file waisi hi rahegi."

# Button fill colours. Telegram's "primary" is blue, "success" is green (the Bot API accepts only
# "danger", "success", "primary" on reply keyboard buttons). The three options are blue, the two
# info buttons are green. The label text does not change.
BUTTON_STYLE = {BTN_WEB: "primary", BTN_PINGER: "primary", BTN_BOTH: "primary",
                BTN_STATUS: "success", BTN_LAST: "success"}

RULE = "━━━━━━━━━━━━━━"

# Look of the bot's messages (text only). Button labels above stay exactly as they are.
WELCOME = ("👋 Namaste! Main Bot Implementer hoon.\n"
           f"{RULE}\n"
           "📎 Apni bot ki .py file bhejo.\n"
           "➕ Main web server aur/ya pinger add karke ek NAYI file bhejunga.\n"
           "🔒 Aapki original file main nahi chhedunga.\n\n"
           "File bhejne ke baad neeche ke option chuno:\n"
           "🌐 Web server add karo\n"
           "📡 Pinger add karo\n"
           "🧩 Dono add karo\n\n"
           "Kabhi bhi neeche ke buttons se:\n"
           "📊 Status dekho: file me kya pehle se hai\n"
           "🔁 Pichli file lo: pichli updated file dobara")
STATUS_TEXT = {
    "sure": "✅ pehle se hai",
    "unsure": "❔ shayad hai (pakka nahi)",
    "none": "➕ nahi hai, add ho sakta hai",
}

# ---- Code injected into the member's file ----------------------------------------------
# Only top-level code: one daemon thread per block, each wrapped in try/except, every name
# starts with _hyv_ so nothing clashes with the member's names. No token, no full URL is printed.

WEB_BLOCK = '''# >>> hyv-injected:web (added by the implementer bot: starts one background web server)
try:
    import threading as _hyv_threading
    import os as _hyv_os
    from http.server import BaseHTTPRequestHandler as _hyv_Base, ThreadingHTTPServer as _hyv_Server

    class _hyv_WebHandler(_hyv_Base):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/health"):
                code, body = 200, b"ok"
            else:
                code, body = 404, b"not found"
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    def _hyv_serve_web():
        try:
            _hyv_port = int(_hyv_os.environ.get("PORT", "10000"))
            _hyv_Server(("0.0.0.0", _hyv_port), _hyv_WebHandler).serve_forever()
        except Exception:
            print("web server could not start", flush=True)

    _hyv_threading.Thread(target=_hyv_serve_web, daemon=True).start()
except Exception:
    print("web server could not start", flush=True)
# <<< hyv-injected:web
'''

PINGER_BLOCK = '''# >>> hyv-injected:pinger (added by the implementer bot: pings RENDER_EXTERNAL_URL in a background thread)
try:
    import threading as _hyv_threading2
    import os as _hyv_os2
    import time as _hyv_time
    import urllib.request as _hyv_urlreq

    def _hyv_ping_minutes():
        try:
            _hyv_m = int(_hyv_os2.environ.get("PING_MINUTES", "5").strip())
        except ValueError:
            return 5
        return _hyv_m if 1 <= _hyv_m <= 60 else 5

    def _hyv_ping_loop():
        _hyv_time.sleep(30)
        while True:
            try:
                _hyv_url = _hyv_os2.environ.get("RENDER_EXTERNAL_URL", "").strip()
                if _hyv_url.lower().startswith(("http://", "https://")):
                    try:
                        with _hyv_urlreq.urlopen(_hyv_url, timeout=30) as _hyv_resp:
                            _hyv_code = str(_hyv_resp.status)
                    except _hyv_urlreq.HTTPError as _hyv_err:
                        _hyv_code = str(_hyv_err.code)
                    except Exception:
                        _hyv_code = "down"
                    print("keep-alive ping:", _hyv_code, flush=True)
            except Exception:
                pass
            _hyv_time.sleep(_hyv_ping_minutes() * 60)

    _hyv_threading2.Thread(target=_hyv_ping_loop, daemon=True).start()
except Exception:
    print("pinger could not start", flush=True)
# <<< hyv-injected:pinger
'''

# ---- Detection ------------------------------------------------------------------------
# Conservative: any match counts as "already there". "sure" means a clear sign; "unsure"
# means a weak sign, and the bot still does not add the code and says so.

WEB_SURE = [
    r"hyv-injected:web",                                        # our own block, added before
    r"^\s*(?:from|import)\s+(?:flask|fastapi|bottle|aiohttp|tornado|sanic|quart|waitress"
    r"|gunicorn|uvicorn|http\.server|socketserver)\b",           # web framework / http server import
    r"\bFlask\s*\(",                                             # Flask app object
    r"HTTPServer\b|\bserve_forever\s*\(",                        # stdlib http server (also ThreadingHTTPServer)
    r"\bstart_web\s*\(",                                         # keep_alive.start_web() style
    r"\.run\([^)]*0\.0\.0\.0",                                   # app.run(host="0.0.0.0", ...)
]
WEB_UNSURE = [
    r"0\.0\.0\.0",                                               # bind-all address somewhere
    r"(?:environ(?:\.get)?|getenv)\s*\(\s*[\"']PORT[\"']",       # reads PORT from the environment
]
PINGER_SURE = [
    r"hyv-injected:pinger",                                      # our own block, added before
    r"RENDER_EXTERNAL_URL",                                      # Render's own URL variable
    r"PING_URLS|PING_MINUTES",
    r"keep[_\- ]?alive",
    r"self[_\- ]?ping",
    r"\bping_(?:loop|thread|self|url|urls|every)\b",
]
PINGER_UNSURE = [
    r"\bpinger\b",                                               # the word alone, e.g. in a comment
    r"\bping\b",                                                 # e.g. a /ping command, or a word in text
]
PERIODIC_THREAD = r"\bThread\s*\(|\.Thread\s*\("
PERIODIC_SLEEP = r"\bsleep\s*\("
PERIODIC_HTTP = r"\brequests\.(?:get|head|post)\s*\(|\burlopen\s*\(|\bhttpx\.\w+\s*\(|\baiohttp\b"


def _any(patterns, source, flags):
    return any(re.search(p, source, flags) for p in patterns)


def web_server_status(source):
    """Does the file already have a web server? Returns "sure", "unsure" or "none".

    Looks for: our own marker; imports of flask, fastapi, bottle, aiohttp, tornado, sanic, quart,
    waitress, gunicorn, uvicorn, http.server, socketserver; Flask( ; HTTPServer / ThreadingHTTPServer /
    serve_forever(; start_web(; app.run(... 0.0.0.0 ...).
    "unsure" when only these show up: the text 0.0.0.0, or a PORT read from the environment.
    """
    if _any(WEB_SURE, source, re.M):
        return "sure"
    if _any(WEB_UNSURE, source, re.M):
        return "unsure"
    return "none"


def pinger_status(source):
    """Does the file already have a keep-alive pinger? Returns "sure", "unsure" or "none".

    Looks for: our own marker; RENDER_EXTERNAL_URL; PING_URLS / PING_MINUTES; keep_alive /
    keep-alive / keepalive; self-ping names; ping_loop / ping_thread / ping_url names.
    "unsure" when only these show up: the word pinger or ping on its own, or a background thread that
    sleeps and makes a web request (a periodic request job).
    """
    if _any(PINGER_SURE, source, re.I | re.M):
        return "sure"
    if _any(PINGER_UNSURE, source, re.I | re.M):
        return "unsure"
    if (re.search(PERIODIC_THREAD, source) and re.search(PERIODIC_SLEEP, source)
            and re.search(PERIODIC_HTTP, source)):
        return "unsure"
    return "none"


# ---- Injection ------------------------------------------------------------------------

def inject(source, add_web, add_pinger):
    """Returns the new file text with the chosen blocks inserted after the file's leading imports.

    The blocks go in as top-level code. Before returning, the result is parsed again and checked:
    exactly the new blocks were added and every existing top-level statement is unchanged.
    Raises ValueError when the check fails, SyntaxError when the result would not parse.
    """
    blocks = [b for b, on in ((WEB_BLOCK, add_web), (PINGER_BLOCK, add_pinger)) if on]
    if not blocks:
        return source
    tree = ast.parse(source)
    body = tree.body
    k = 0
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        k = 1  # module docstring stays first
    while k < len(body) and isinstance(body[k], (ast.Import, ast.ImportFrom)):
        k += 1
    at = body[k - 1].end_lineno if k else 0  # keep this many lines before the block

    block_text = "\n" + "".join(blocks) + "\n"
    lines = source.splitlines(keepends=True)
    head, tail = lines[:at], lines[at:]
    if head and not head[-1].endswith("\n"):
        head[-1] += "\n"
    new_source = "".join(head) + block_text + "".join(tail)

    new_tree = ast.parse(new_source)
    first = at + 1
    last = at + block_text.count("\n")
    ours = [n for n in new_tree.body if first <= n.lineno <= last]
    rest = [n for n in new_tree.body if not (first <= n.lineno <= last)]
    same = [ast.dump(n) for n in rest] == [ast.dump(n) for n in body]
    if len(ours) != len(blocks) or not same:
        raise ValueError("injection check failed")
    return new_source


def read_source(data):
    """(text, None) when the bytes are a Python file that parses, otherwise (None, message).
    The file is only parsed, never run."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, "Ye file UTF-8 text me nahi hai. Check karke dobara bhejo."
    try:
        ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None, "Is file me syntax error hai. Pehle use theek karke dobara bhejo."
    return text, None


# ---- The option logic (no Telegram calls: easy to test) ------------------------------

def _skip_phrase(kind, status):
    name = "Web server" if kind == "web" else "Pinger"
    if status == "sure":
        return f"{name} ka code pehle se hai"
    return f"{name} jaisa code lagta hai (pakka nahi)"


def plan(source, choice):
    """Returns (new_source, reply_text). new_source is None when nothing needs adding."""
    web = web_server_status(source)
    pin = pinger_status(source)
    want_web = choice in (BTN_WEB, BTN_BOTH)
    want_pin = choice in (BTN_PINGER, BTN_BOTH)
    add_web = want_web and web == "none"
    add_pin = want_pin and pin == "none"
    skipped = []
    if want_web and not add_web:
        skipped.append(_skip_phrase("web", web))
    if want_pin and not add_pin:
        skipped.append(_skip_phrase("pinger", pin))

    if not add_web and not add_pin:
        return None, ". ".join(skipped) + ". Isliye maine kuch add nahi kiya."

    if add_web and add_pin:
        text = "Web server aur pinger dono add kar diye."
    elif add_web:
        text = "Web server add kar diya."
    else:
        text = "Pinger add kar diya (default 5 minute)."
        if web == "none":
            text += " Pinger tabhi kaam karega jab bot par web server bhi chale."
    for phrase in skipped:
        text += f" {phrase}, isliye use chhoda."
    return inject(source, add_web, add_pin), text


def status_block(source):
    """Two lines (web server, pinger) saying what the file already has. Text only."""
    return (f"🌐 Web server: {STATUS_TEXT[web_server_status(source)]}\n"
            f"📡 Pinger: {STATUS_TEXT[pinger_status(source)]}")


def card(title, lines=(), footer=""):
    """A status card: title, a rule, short lines, then an optional footer after a blank line."""
    text = "\n".join([title, RULE, *lines])
    return f"{text}\n\n{footer}" if footer else text


# ---- Files on disk -------------------------------------------------------------------

def safe_name(name):
    """A safe .py file name for the disk. Keeps letters, digits, dot, dash and underscore."""
    base = os.path.basename(name or "")
    stem = base[:-3] if base.lower().endswith(".py") else base
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")[:60]
    return (stem or "bot") + ".py"


def save_unique(folder, name, data):
    """Writes a new file. Never overwrites: a name that already exists gets a counter. Returns the path."""
    os.makedirs(folder, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem, ext = os.path.splitext(name)
    for n in range(1, 10000):
        path = os.path.join(folder, f"{stamp}-{n:03d}-{stem}{ext}")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path
    raise OSError("no free file name")


def result_name(name):
    stem = name[:-3] if name.lower().endswith(".py") else name
    return f"{stem}-updated.py"


def display_name(stored):
    """The name the member sees: a saved name without its time stamp in front."""
    return re.sub(r"^\d{8}-\d{6}-\d{3}-", "", stored)


def latest_result(folder):
    """Path of the newest saved result (.py) in the folder, or None. Newest by file time; the name
    breaks a tie. (Names alone are not enough: two saves in the same second can share a counter.)"""
    try:
        paths = [os.path.join(folder, n) for n in os.listdir(folder) if n.lower().endswith(".py")]
        if not paths:
            return None
        return max(paths, key=lambda p: (os.path.getmtime(p), p))
    except OSError:
        return None


# ---- Telegram side -------------------------------------------------------------------

def _button(label):
    return types.KeyboardButton(label, style=BUTTON_STYLE[label])


def option_keyboard():
    """Reply keyboard: the 3 options, one per row (as before), then a row with the 2 info buttons.
    Each button has a fill colour (style) from BUTTON_STYLE."""
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(_button(BTN_WEB))
    kb.row(_button(BTN_PINGER))
    kb.row(_button(BTN_BOTH))
    kb.row(_button(BTN_STATUS), _button(BTN_LAST))
    return kb


class Implementer:
    def __init__(self, bot, owner_id, data_dir):
        self.bot = bot
        self.owner = owner_id
        self.data_dir = data_dir
        self.pending = {}  # chat_id -> (path of the saved original, display name)
        self.lock = threading.Lock()

    def is_owner(self, user_id):
        return self.owner is not None and user_id == self.owner

    def refuse(self, chat_id):
        self.bot.send_message(chat_id, "🔒 Ye bot private hai.")

    def user_dir(self, chat_id, sub):
        return os.path.join(self.data_dir, str(int(chat_id)), sub)

    def on_start(self, message):
        chat_id = message.chat.id
        uid = message.from_user.id
        if self.owner is None:
            # Same as the pinger bot: shows the user's own id, so the owner can set OWNER_ID.
            self.bot.send_message(chat_id, f"🆔 Aapka Telegram ID: {uid}\n\n"
                                  "Render me OWNER_ID naam ki env variable me ye ID daal, "
                                  "service restart karo, phir /start likho.")
            return
        if not self.is_owner(uid):
            return self.refuse(chat_id)
        self.bot.send_message(chat_id, WELCOME, reply_markup=option_keyboard())

    def on_document(self, message):
        chat_id = message.chat.id
        if not self.is_owner(message.from_user.id):
            return self.refuse(chat_id)
        doc = message.document
        raw_name = doc.file_name or ""
        if not raw_name.lower().endswith(".py"):
            return self.bot.send_message(chat_id, "⚠️ Sirf .py file bhejo. Dusri file ko main nahi chhedunga.")
        if (doc.file_size or 0) > MAX_BYTES:
            return self.bot.send_message(chat_id, "⚠️ Ye file bahut badi hai. Limit 300 KB hai.")
        try:
            info = self.bot.get_file(doc.file_id)
            data = self.bot.download_file(info.file_path)
        except Exception:
            return self.bot.send_message(chat_id, "⚠️ File download nahi ho payi. Dobara bhejo.")
        if not isinstance(data, bytes) or len(data) > MAX_BYTES:
            return self.bot.send_message(chat_id, "⚠️ File sahi se nahi aayi. Dobara bhejo.")
        source, err = read_source(data)
        if err:
            return self.bot.send_message(chat_id, "⚠️ " + err)
        name = safe_name(raw_name)
        path = save_unique(self.user_dir(chat_id, "uploads"), name, data)
        with self.lock:
            self.pending[chat_id] = (path, name)
        self.bot.send_message(chat_id, card(f"📄 File mil gayi: {name}", [status_block(source)],
                                            "Ab neeche se option chuno."),
                              reply_markup=option_keyboard())

    def on_status(self, message):
        chat_id = message.chat.id
        if not self.is_owner(message.from_user.id):
            return self.refuse(chat_id)
        with self.lock:
            pending = self.pending.get(chat_id)
        if pending is None:
            return self.bot.send_message(chat_id, "📭 Abhi koi file nahi hai. Pehle apni bot ki .py file bhejo.")
        path, name = pending
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return self.bot.send_message(chat_id, "⚠️ Wo file ab nahi mil rahi. Dobara bhejo.")
        source, err = read_source(data)
        if err:
            return self.bot.send_message(chat_id, "⚠️ " + err)
        self.bot.send_message(chat_id, card(f"📊 Status: {name}", [status_block(source)],
                                            "Neeche se option chuno, ya nayi .py file bhejo."),
                              reply_markup=option_keyboard())

    def on_last(self, message):
        chat_id = message.chat.id
        if not self.is_owner(message.from_user.id):
            return self.refuse(chat_id)
        path = latest_result(self.user_dir(chat_id, "results"))
        if path is None:
            return self.bot.send_message(chat_id, "📭 Abhi koi updated file nahi bani hai. "
                                         "Pehle file bhejo aur option chuno.")
        with open(path, "rb") as f:
            self.bot.send_document(chat_id, f, visible_file_name=display_name(os.path.basename(path)),
                                   caption=card("🔁 Pichli updated file", [], RESULT_NOTE))

    def on_text(self, message):
        chat_id = message.chat.id
        if not self.is_owner(message.from_user.id):
            return self.refuse(chat_id)
        text = (message.text or "").strip()
        if text.startswith("/"):
            return  # commands are handled by their own handlers; unknown ones are ignored
        # The two info buttons go to the same handlers as the typed commands.
        if text == BTN_STATUS:
            return self.on_status(message)
        if text == BTN_LAST:
            return self.on_last(message)
        with self.lock:
            pending = self.pending.get(chat_id)
        if text not in OPTIONS:
            if pending is None:
                return self.bot.send_message(chat_id, "📭 Pehle apni bot ki .py file bhejo.")
            return self.bot.send_message(chat_id, "👇 Neeche ke button me se option chuno, "
                                         "ya nayi .py file bhejo.")
        if pending is None:
            return self.bot.send_message(chat_id, "📭 Pehle apni bot ki .py file bhejo.")
        path, name = pending
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return self.bot.send_message(chat_id, "⚠️ Wo file ab nahi mil rahi. Dobara bhejo.")
        source, err = read_source(data)
        if err:
            return self.bot.send_message(chat_id, "⚠️ " + err)
        try:
            new_source, reply = plan(source, text)
        except Exception:
            return self.bot.send_message(chat_id, "⚠️ Kuch gadbad ho gayi, file nahi badli. "
                                         "Dobara try karo.")
        if new_source is None:
            return self.bot.send_message(chat_id, f"ℹ️ File nahi badli.\n\n{reply}")
        out_name = result_name(name)
        out_path = save_unique(self.user_dir(chat_id, "results"), out_name, new_source.encode("utf-8"))
        with open(out_path, "rb") as f:
            self.bot.send_document(chat_id, f, visible_file_name=out_name,
                                   caption=card(f"✅ Naya file taiyaar hai: {out_name}", [reply], RESULT_NOTE))


def register_handlers(bot, impl):
    @bot.message_handler(commands=["start", "help"])
    def _hyv_start(message):
        impl.on_start(message)

    @bot.message_handler(commands=["status"])
    def _hyv_status(message):
        impl.on_status(message)

    @bot.message_handler(commands=["last"])
    def _hyv_last(message):
        impl.on_last(message)

    @bot.message_handler(content_types=["document"])
    def _hyv_document(message):
        impl.on_document(message)

    @bot.message_handler(content_types=["text"])
    def _hyv_text(message):
        impl.on_text(message)


class _HideToken(logging.Filter):
    """Library log lines can contain the request address, which holds the token. Remove it."""

    def __init__(self, token):
        super().__init__()
        self.token = token

    def filter(self, record):
        try:
            text = record.getMessage()
        except Exception:
            text = "log line"
        if self.token:
            text = text.replace(self.token, "<hidden>")
        record.msg = text
        record.args = ()
        return True


def hide_token(token):
    for name in ("TeleBot", "telebot", "urllib3"):
        logging.getLogger(name).addFilter(_HideToken(token))
    logging.getLogger("urllib3").setLevel(logging.CRITICAL)


# ---- This bot's own web server and pinger (keeps it awake on Render) ------------------
# Same idea as the injected blocks above, for this file itself. The markers below are the
# ones the detection logic looks for, so a copy of this file sent back to the bot is seen
# as already having both and nothing is added twice. Nothing here stops the bot if it fails.

# >>> hyv-injected:web (this bot's own web server)
def web_port():
    raw = os.environ.get("PORT", "").strip()
    return int(raw) if raw.isdecimal() and 0 < int(raw) < 65536 else 10000


def start_web_server():
    """Flask web server in a daemon thread. "/" and "/health" answer "ok". Returns the thread or None."""
    try:
        from flask import Flask, Response
        app = Flask(__name__)
        logging.getLogger("werkzeug").setLevel(logging.ERROR)

        @app.route("/")
        @app.route("/health")
        def health():
            return Response("ok", status=200, mimetype="text/plain")

        port = web_port()

        def run():
            try:
                # host 0.0.0.0 is needed, otherwise Render does not see the port
                app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
            except Exception:
                print("web server could not start", flush=True)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        print(f"web server on port {port}", flush=True)
        return thread
    except Exception:
        print("web server could not start", flush=True)
        return None
# <<< hyv-injected:web

# >>> hyv-injected:pinger (this bot's own keep-alive pinger)
def ping_minutes():
    raw = os.environ.get("PING_MINUTES", "").strip()
    return int(raw) if raw.isdecimal() and 1 <= int(raw) <= 60 else 5


def self_ping_once(url):
    """One request to our own address. Only the status is logged, never the address."""
    code = "down"
    try:
        import requests
        resp = requests.get(url, timeout=30, stream=True)
        code = str(resp.status_code)
        resp.close()
    except Exception:
        code = "down"  # error text is not logged: it can contain the address
    print(f"keep-alive ping: {code}", flush=True)


def self_ping_loop(url):
    time.sleep(30)  # give the web server a moment to start
    while True:
        try:
            self_ping_once(url)
        except Exception:
            print("keep-alive ping: error", flush=True)
        time.sleep(ping_minutes() * 60)


def start_self_pinger():
    """Daemon thread that pings RENDER_EXTERNAL_URL every PING_MINUTES. Returns the thread or None."""
    try:
        url = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
        if not url.lower().startswith(("http://", "https://")):
            print("RENDER_EXTERNAL_URL is not set: self-ping skipped", flush=True)
            return None
        thread = threading.Thread(target=self_ping_loop, args=(url,), daemon=True)
        thread.start()
        print(f"self-ping every {ping_minutes()} minute(s)", flush=True)
        return thread
    except Exception:
        print("pinger could not start", flush=True)
        return None
# <<< hyv-injected:pinger


def parse_owner(raw):
    raw = (raw or "").strip()
    return int(raw) if raw.isdecimal() else None


def data_dir():
    return os.environ.get("DATA_DIR", "").strip() or os.path.join(HERE, "data")


def main():
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        print("BOT_TOKEN is not set. Stopping.", flush=True)
        return 1
    raw_owner = os.environ.get("OWNER_ID", "")
    owner = parse_owner(raw_owner)
    if owner is None:
        print("OWNER_ID is not set or not numeric: only /start with your ID works.", flush=True)
    hide_token(token)
    start_web_server()  # before polling, so Render sees the port open
    start_self_pinger()
    bot = telebot.TeleBot(token)
    try:
        bot.remove_webhook()
    except Exception:
        pass
    impl = Implementer(bot, owner, data_dir())
    register_handlers(bot, impl)
    while True:
        try:
            bot.infinity_polling(skip_pending=True)
        except Exception as e:
            print(f"poll error: {type(e).__name__}", flush=True)
        time.sleep(5)


if __name__ == "__main__":
    sys.exit(main() or 0)
