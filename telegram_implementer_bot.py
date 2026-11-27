"""Implementer bot (Hinglish messages).

The owner sends a bot .py file. The bot shows what the file already has (web server, pinger)
and 3 buttons. A tap adds a web server and/or a keep-alive pinger to a NEW copy of that file and
sends the copy back as a document. The original file is never changed.

Buttons (owner only): "Status dekho" shows what the last file has; "Pichli file lo" sends the last
updated file again. Typed /status and /last still work (quietly, no hint is shown).
It uses no outside service and no API key. Old web server or pinger code is found and removed by a built-in
check of the file's syntax tree. A repair pass fixes common problems (tabs, BOM, CRLF, NBSP, smart
quotes, a hard-coded token, app.run host, missing imports) and reports every change. "/check" (or the
words "pehle check" plus an option) shows what would change without making a file.

This bot also keeps itself awake on Render: a small Flask web server ("/" and "/health" answer
"ok") and a pinger that requests its own RENDER_EXTERNAL_URL every PING_MINUTES minutes.

Env:
  BOT_TOKEN       Telegram bot token (required, never printed or stored)
  OWNER_ID        numeric Telegram id of the owner (only this user may use the bot)
  DATA_DIR        optional; where uploads and results are kept (default: data folder next to this file)
  PORT            optional; web server port (Render sets it; default 10000)
  RENDER_EXTERNAL_URL  set by Render for a Web Service; the pinger calls it (skipped when unset)
  PING_MINUTES    optional; minutes between self-pings, 1 to 60 (default 5)
  HYV_ADMIN_IDS   optional; extra Telegram ids (comma separated) allowed to use Database Export/Import
                  in a bot that gets the feature added
"""

import ast
import logging
import io
import os
import re
import sys
import time
import threading

import telebot
from telebot import types
from telebot.apihelper import ApiException

HERE = os.path.dirname(os.path.abspath(__file__))
# Biggest .py file the bot takes (Telegram lets a bot download at most 20 MB).
MAX_BYTES = 20 * 1024 * 1024


def size_limit_text():
    """Plain Hinglish limit, for messages."""
    return f"{MAX_BYTES // (1024 * 1024)} MB"


BTN_WEB = "🌐 Web server add karo"
BTN_PINGER = "📡 Pinger add karo"
BTN_BOTH = "🧩 Dono add karo"
BTN_DB = "🗄 Database Export/Import"   # owner/admin only backup feature
OPTIONS = (BTN_WEB, BTN_PINGER, BTN_BOTH, BTN_DB)
BTN_STATUS = "📊 Status dekho"   # same as the typed /status
BTN_LAST = "🔁 Pichli file lo"   # same as the typed /last
MENU_BUTTONS = OPTIONS + (BTN_STATUS, BTN_LAST)
INFO_BUTTONS = (BTN_STATUS, BTN_LAST)
CHECK_WORDS = {"web": BTN_WEB, "pinger": BTN_PINGER, "both": BTN_BOTH, "dono": BTN_BOTH,
               "db": BTN_DB, "database": BTN_DB}
# Labels from before the emoji were added. A tap with an old label still works.
LEGACY_LABELS = {"Web server add karo": BTN_WEB, "Pinger add karo": BTN_PINGER,
                 "Dono add karo": BTN_BOTH, "Status dekho": BTN_STATUS, "Pichli file lo": BTN_LAST}
# Telegram message effects for the final result in a private chat: 🎉, 🔥, 👍 (tried in this order).
EFFECT_IDS = ("5046509860389126442", "5104841245755180586", "5107584321108051014")
RESULT_NOTE = "ℹ️ Ye naya file hai. Ise Render par khud dobara deploy karna hoga. Purani file waisi hi rahegi."

# Button fill colours. Telegram's "primary" is blue, "success" is green (the Bot API accepts only
# "danger", "success", "primary" on reply keyboard buttons). The three options are blue, the two
# info buttons are green. The label text does not change.
BUTTON_STYLE = {BTN_WEB: "primary", BTN_PINGER: "primary", BTN_BOTH: "primary", BTN_DB: "primary",
                BTN_STATUS: "success", BTN_LAST: "success"}

RULE = "━━━━━━━━━━━━━━"

# ---- Premium look: Unicode fonts for headings and key words ----------------------------
# Only ASCII letters and digits change. Every other character (emoji, dots, slashes, underscores,
# Hinglish accents) stays as it is. Never use these on file names, paths, commands, code or
# button callback data. Button labels stay plain text (emoji only).

_BOLD = (0x1D400, 0x1D41A, 0x1D7CE)     # bold A-Z, a-z, 0-9
_BOLD_ITALIC = (0x1D468, 0x1D482, None)  # bold italic A-Z, a-z (digits unchanged)
_SCRIPT = (0x1D4D0, 0x1D4EA, None)      # bold script A-Z, a-z (digits unchanged)
_SMALL = {"a": "ᴀ", "b": "ʙ", "c": "ᴄ", "d": "ᴅ", "e": "ᴇ", "f": "ꜰ", "g": "ɢ", "h": "ʜ", "i": "ɪ",
          "j": "ᴊ", "k": "ᴋ", "l": "ʟ", "m": "ᴍ", "n": "ɴ", "o": "ᴏ", "p": "ᴘ", "q": "ǫ", "r": "ʀ",
          "s": "ꜱ", "t": "ᴛ", "u": "ᴜ", "v": "ᴠ", "w": "ᴡ", "x": "x", "y": "ʏ", "z": "ᴢ"}


def _fancy(text, up, low, digit):
    out = []
    for ch in str(text):
        if "A" <= ch <= "Z":
            out.append(chr(up + ord(ch) - 65))
        elif "a" <= ch <= "z":
            out.append(chr(low + ord(ch) - 97))
        elif digit is not None and "0" <= ch <= "9":
            out.append(chr(digit + ord(ch) - 48))
        else:
            out.append(ch)
    return "".join(out)


def fx_bold(text):
    return _fancy(text, *_BOLD)


def fx_bold_italic(text):
    return _fancy(text, *_BOLD_ITALIC)


def fx_script(text):
    return _fancy(text, *_SCRIPT)


def fx_small(text):
    """Small capitals. Letters only (A-Z become small letters); everything else is unchanged."""
    return "".join(_SMALL.get(ch.lower(), ch) if ch.isascii() and ch.isalpha() else ch for ch in str(text))


# Look of the bot's messages (text only). Heading words use the fonts above; the rest is plain.
WELCOME = (f"👋 {fx_bold('Namaste!')} Main Bot Implementer hoon.\n"
           f"{RULE}\n"
           "📎 Apni bot ki .py file bhejo.\n"
           "➕ Main web server aur/ya pinger add karke ek NAYI file bhejunga.\n"
           "🔁 Purana web server ya pinger ho to use hata ke naya daalta hai (apne aap, bahar kisi service ke bina).\n"
           "🔒 Aapki original file main nahi chhedunga.\n"
           f"📏 File ki limit {size_limit_text()} hai.\n\n"
           f"🧭 {fx_script('Options')}\n"
           "File bhejne ke baad neeche ke option chuno:\n"
           f"{BTN_WEB}\n"
           f"{BTN_PINGER}\n"
           f"{BTN_BOTH}\n"
           f"{BTN_DB}\n\n"
           f"⚡ {fx_small('Quick buttons')}\n"
           "Kabhi bhi neeche ke buttons se:\n"
           f"{BTN_STATUS}: file me kya pehle se hai\n"
           f"{BTN_LAST}: pichli updated file dobara\n\n"
           "🔍 /check likho (ya 'pehle check web' jaisa) to bina file banaye dekh lo ki kya badlega.")
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

BACKUP_TELEBOT = r'''# >>> hyv-injected:backup (added by the implementer bot: owner/admin Export and Import of data files)
import hashlib as _hyv_bk_hash
import inspect as _hyv_bk_inspect
import json as _hyv_bk_json
import os as _hyv_bk_os
import re as _hyv_bk_re
import shutil as _hyv_bk_shutil
import sqlite3 as _hyv_bk_sqlite
import tempfile as _hyv_bk_tmp
import time as _hyv_bk_time
import zipfile as _hyv_bk_zip
from telebot import types as _hyv_bk_types

_HYV_BK_HERE = _hyv_bk_os.path.dirname(_hyv_bk_os.path.abspath(__file__))
_HYV_BK_SELF = _hyv_bk_os.path.basename(__file__)
_HYV_BK_MAX = 50 * 1024 * 1024
_HYV_BK_EXT = (".db", ".sqlite", ".sqlite3", ".json", ".csv", ".txt", ".pkl", ".pickle", ".dat", ".yaml", ".yml", ".xml")
_HYV_BK_DB_EXT = (".db", ".sqlite", ".sqlite3")
_HYV_BK_SKIP_DIRS = {"__pycache__", "venv", ".venv", "env", ".git", "node_modules"}
_HYV_BK_SECRET = _hyv_bk_re.compile(r"(\.env|token|secret|passw|credential|api_?key|private)", _hyv_bk_re.I)
_HYV_BK_CODE_PATH = _hyv_bk_re.compile(r"""["']([^"'\s]+\.(?:db|sqlite3?|json|csv|txt|pkl|pickle|dat|ya?ml|xml))["']""")
_HYV_BK_BTN_EXPORT = "📤 Export"
_HYV_BK_BTN_IMPORT = "📥 Import"
_HYV_BK_BTN_BACK = "🔙 Back"
_HYV_BK_BTN_DB = "🗄 Database Export/Import"   # the button the admin taps to open the backup menu
_HYV_BK_DB_LABELS = (_HYV_BK_BTN_DB, _HYV_BK_BTN_EXPORT, _HYV_BK_BTN_IMPORT)
_HYV_BK_BTN_STYLE = "primary"   # colour of the Database button (blue), same as the bot's option buttons
_hyv_bk_pending = {}   # admin user id -> "import" or staged restore info


_HYV_BK_ADMINS = {__HYV_ADMIN_ID__}   # Telegram id of the member who sent the bot file (set when added)


def _hyv_bk_is_admin(uid):
    ids = set(str(x) for x in _HYV_BK_ADMINS)
    for raw in _hyv_bk_os.environ.get("HYV_ADMIN_IDS", "").split(","):
        if raw.strip():
            ids.add(raw.strip())
    return str(uid) in ids


def _hyv_bk_excluded(rel):
    parts = rel.replace("\\", "/").split("/")
    if any(p in _HYV_BK_SKIP_DIRS or p.startswith("hyv_safety") for p in parts[:-1]):
        return True
    name = parts[-1]
    return (name == _HYV_BK_SELF or name.endswith((".zip", ".pyc")) or bool(_HYV_BK_SECRET.search(name)))


def _hyv_bk_sha(path):
    h = _hyv_bk_hash.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _hyv_bk_collect():
    picked = {}
    for root, dirs, files in _hyv_bk_os.walk(_HYV_BK_HERE):
        dirs[:] = [d for d in dirs if d not in _HYV_BK_SKIP_DIRS and not d.startswith("hyv_safety")]
        for f in files:
            full = _hyv_bk_os.path.join(root, f)
            rel = _hyv_bk_os.path.relpath(full, _HYV_BK_HERE).replace(_hyv_bk_os.sep, "/")
            if not _hyv_bk_excluded(rel) and f.lower().endswith(_HYV_BK_EXT):
                picked[rel] = full
    try:
        with open(__file__, "r", encoding="utf-8", errors="ignore") as fh:
            code = fh.read()
    except OSError:
        code = ""
    for m in _HYV_BK_CODE_PATH.finditer(code):
        p = m.group(1)
        if _hyv_bk_os.path.isabs(p):
            continue
        for base in (_HYV_BK_HERE, _hyv_bk_os.getcwd()):
            full = _hyv_bk_os.path.normpath(_hyv_bk_os.path.join(base, p))
            rel = _hyv_bk_os.path.relpath(full, _HYV_BK_HERE).replace(_hyv_bk_os.sep, "/")
            if (_hyv_bk_os.path.isfile(full) and not rel.startswith("..") and not _hyv_bk_excluded(rel)):
                picked[rel] = full
    return picked


def _hyv_bk_make_zip(picked, out_path):
    tmp = _hyv_bk_tmp.mkdtemp(prefix="hyvbk_")
    try:
        entries = []
        for i, (rel, full) in enumerate(sorted(picked.items())):
            src = full
            if rel.lower().endswith(_HYV_BK_DB_EXT):
                src = _hyv_bk_os.path.join(tmp, "snap%d" % i)
                s = _hyv_bk_sqlite.connect(full)
                try:
                    d = _hyv_bk_sqlite.connect(src)
                    try:
                        s.backup(d)   # reads -wal/-shm too, so the copy is consistent
                    finally:
                        d.close()
                finally:
                    s.close()
            entries.append((rel, src))
        manifest = {"app": "hyv-backup", "version": 1,
                    "created": _hyv_bk_time.strftime("%Y-%m-%d %H:%M:%S"), "files": []}
        with _hyv_bk_zip.ZipFile(out_path, "w", _hyv_bk_zip.ZIP_DEFLATED) as z:
            for rel, src in entries:
                manifest["files"].append({"path": rel, "size": _hyv_bk_os.path.getsize(src),
                                          "sha256": _hyv_bk_sha(src)})
                z.write(src, rel)
            z.writestr("manifest.json", _hyv_bk_json.dumps(manifest, indent=2, ensure_ascii=False))
        return manifest
    finally:
        _hyv_bk_shutil.rmtree(tmp, ignore_errors=True)


def _hyv_bk_stage(zip_path):
    """Check the backup fully and unpack it into a temp folder. Nothing in the bot folder is touched."""
    stage = _hyv_bk_tmp.mkdtemp(prefix="hyvbk_in_")
    try:
        if _hyv_bk_os.path.getsize(zip_path) > _HYV_BK_MAX:
            raise ValueError("file 50 MB se badi hai")
        with _hyv_bk_zip.ZipFile(zip_path) as z:
            if z.testzip() is not None:
                raise ValueError("zip file kharab hai")
            manifest = _hyv_bk_json.loads(z.read("manifest.json").decode("utf-8"))
            if manifest.get("app") != "hyv-backup" or not isinstance(manifest.get("files"), list):
                raise ValueError("ye hyv backup file nahi hai")
            names = set(z.namelist())
            total = 0
            staged = []
            for item in manifest["files"]:
                rel = item.get("path")
                if (not isinstance(rel, str) or not rel or rel.startswith(("/", "\\")) or ":" in rel
                        or "\\" in rel or ".." in rel.split("/") or _hyv_bk_excluded(rel)):
                    raise ValueError("unsafe path in backup")
                if rel not in names:
                    raise ValueError("file missing in backup: " + rel)
                info = z.getinfo(rel)
                total += info.file_size
                if info.file_size != item.get("size") or total > _HYV_BK_MAX:
                    raise ValueError("size check failed")
                target = _hyv_bk_os.path.normpath(_hyv_bk_os.path.join(stage, rel))
                if not target.startswith(stage + _hyv_bk_os.sep):
                    raise ValueError("unsafe path in backup")
                _hyv_bk_os.makedirs(_hyv_bk_os.path.dirname(target), exist_ok=True)
                with z.open(rel) as src, open(target, "wb") as dst:
                    _hyv_bk_shutil.copyfileobj(src, dst)
                if _hyv_bk_sha(target) != item.get("sha256"):
                    raise ValueError("checksum mismatch: " + rel)
                staged.append((rel, target))
        return {"stage": stage, "files": staged, "created": manifest.get("created", "?")}
    except Exception:
        _hyv_bk_shutil.rmtree(stage, ignore_errors=True)
        raise


def _hyv_bk_restore(info):
    stamp = _hyv_bk_time.strftime("%Y%m%d_%H%M%S")
    safety = _hyv_bk_os.path.join(_HYV_BK_HERE, "hyv_safety", "before_import_" + stamp)
    for rel, _ in info["files"]:
        cur = _hyv_bk_os.path.join(_HYV_BK_HERE, rel)
        for path in (cur, cur + "-wal", cur + "-shm"):
            if _hyv_bk_os.path.isfile(path):
                dst = _hyv_bk_os.path.join(safety, _hyv_bk_os.path.relpath(path, _HYV_BK_HERE))
                _hyv_bk_os.makedirs(_hyv_bk_os.path.dirname(dst), exist_ok=True)
                _hyv_bk_shutil.copy2(path, dst)
    for rel, tmp_path in info["files"]:
        dst = _hyv_bk_os.path.join(_HYV_BK_HERE, rel)
        _hyv_bk_os.makedirs(_hyv_bk_os.path.dirname(dst), exist_ok=True)
        if rel.lower().endswith(_HYV_BK_DB_EXT):
            for side in ("-wal", "-shm"):
                if _hyv_bk_os.path.isfile(dst + side):
                    _hyv_bk_os.remove(dst + side)   # old sidecar must not replay over the restored db
        _hyv_bk_shutil.copy2(tmp_path, dst)
    return safety


def _hyv_bk_bar(done, total=5):
    return "▰" * done + "▱" * (total - done)


def _hyv_bk_admin_chat(chat_id):
    """True when the chat is the private chat of an admin (in a private chat chat_id is the user id)."""
    return chat_id is not None and not isinstance(chat_id, bool) and _hyv_bk_is_admin(chat_id)


def _hyv_bk_btn(label):
    """The Database button as the dict telebot keeps in a keyboard row, with its colour (style). A telebot version
    whose KeyboardButton does not take style gets the style added to the dict, so it still reaches Telegram."""
    try:
        return _hyv_bk_types.KeyboardButton(label, style=_HYV_BK_BTN_STYLE).to_dict()
    except TypeError:
        btn = _hyv_bk_types.KeyboardButton(label).to_dict()
        btn["style"] = _HYV_BK_BTN_STYLE
        return btn


def _hyv_bk_tb_markup(markup):
    """The same reply keyboard with the Database button in one extra last row. The original object is not changed."""
    if not isinstance(markup, _hyv_bk_types.ReplyKeyboardMarkup):
        return markup
    rows = [list(row) for row in (markup.keyboard or [])]
    texts = [b.get("text") if isinstance(b, dict) else getattr(b, "text", b) for row in rows for b in row]
    if any(t in _HYV_BK_DB_LABELS for t in texts):
        return markup
    rows.append([_hyv_bk_btn(_HYV_BK_BTN_DB)])
    return _hyv_bk_types.ReplyKeyboardMarkup(
        resize_keyboard=markup.resize_keyboard, one_time_keyboard=markup.one_time_keyboard,
        selective=markup.selective, row_width=markup.row_width,
        input_field_placeholder=markup.input_field_placeholder, is_persistent=markup.is_persistent, keyboard=rows)


def _hyv_bk_tb_wrap(fn, sig):
    def send(*args, **kwargs):
        try:
            bound = sig.bind_partial(*args, **kwargs)
        except TypeError:
            return fn(*args, **kwargs)
        if _hyv_bk_admin_chat(bound.arguments.get("chat_id")) and bound.arguments.get("reply_markup") is not None:
            bound.arguments["reply_markup"] = _hyv_bk_tb_markup(bound.arguments["reply_markup"])
        return fn(*bound.args, **bound.kwargs)
    send.__name__ = fn.__name__
    return send


def _hyv_bk_patch_telebot(bot):
    """pyTelegramBotAPI: the send_* methods of this bot object that take a reply keyboard add the Database row
    when the chat is an admin's private chat."""
    for name in dir(type(bot)):
        fn = getattr(bot, name, None) if name.startswith("send_") else None
        if fn is None or not callable(fn):
            continue
        sig = _hyv_bk_inspect.signature(fn)
        if "reply_markup" in sig.parameters:
            setattr(bot, name, _hyv_bk_tb_wrap(fn, sig))


def _hyv_bk_register(bot):
    _hyv_bk_patch_telebot(bot)
    def admin_private(m):
        return m.chat.type == "private" and _hyv_bk_is_admin(m.from_user.id)

    def menu(chat_id, text):
        kb = _hyv_bk_types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        kb.add(_hyv_bk_types.KeyboardButton(_HYV_BK_BTN_EXPORT), _hyv_bk_types.KeyboardButton(_HYV_BK_BTN_IMPORT))
        kb.add(_hyv_bk_types.KeyboardButton(_HYV_BK_BTN_BACK))
        bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")

    def show_menu(m):
        _hyv_bk_pending.pop(m.from_user.id, None)
        menu(m.chat.id, "🗄 <b>Database Export / Import</b>\n━━━━━━━━━━━━━━\n"
                        "📤 <b>Export</b> — sab data ek zip file mein\n"
                        "📥 <b>Import</b> — zip file se data wapas laao\n\n"
                        "ℹ️ Import ke baad bot ko restart karna hoga.")

    def export(m):
        if not _hyv_bk_is_admin(m.from_user.id):
            return
        pm = bot.send_message(m.chat.id, "📦 <b>Export shuru</b>\n" + _hyv_bk_bar(0), parse_mode="HTML")
        try:
            for step in range(1, 5):
                bot.edit_message_text("📦 <b>Export ho raha hai</b>\n" + _hyv_bk_bar(step), m.chat.id,
                                      pm.message_id, parse_mode="HTML")
            picked = _hyv_bk_collect()
            if not picked:
                bot.edit_message_text("⚠️ Export ke liye koi data file nahi mili.", m.chat.id, pm.message_id)
                return
            tmp = _hyv_bk_tmp.mkdtemp(prefix="hyvbk_out_")
            try:
                out = _hyv_bk_os.path.join(tmp, "hyv_backup_%s.zip" % _hyv_bk_time.strftime("%Y%m%d_%H%M%S"))
                manifest = _hyv_bk_make_zip(picked, out)
                if _hyv_bk_os.path.getsize(out) > _HYV_BK_MAX:
                    bot.edit_message_text("⚠️ Backup 50 MB se badi hai, Telegram par nahi bhej sakte.",
                                          m.chat.id, pm.message_id)
                    return
                bot.edit_message_text("📦 <b>Export</b>\n" + _hyv_bk_bar(5) + "\n✅ Ho gaya", m.chat.id,
                                      pm.message_id, parse_mode="HTML")
                with open(out, "rb") as fh:
                    bot.send_document(m.chat.id, fh, caption="🗄 <b>Backup ready</b>\n📄 Files: %d\n"
                                      "🕒 %s" % (len(manifest["files"]), manifest["created"]),
                                      parse_mode="HTML", visible_file_name=_hyv_bk_os.path.basename(out))
            finally:
                _hyv_bk_shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            bot.edit_message_text("⚠️ <b>Export nahi ho paya.</b> Thodi der baad dobara try karo.",
                                  m.chat.id, pm.message_id, parse_mode="HTML")

    def start_import(m):
        if not _hyv_bk_is_admin(m.from_user.id):
            return
        _hyv_bk_pending[m.from_user.id] = "import"
        menu(m.chat.id, "📥 <b>Import</b>\n━━━━━━━━━━━━━━\nAb hyv backup wali .zip file bhejo.\n"
                        "Cancel karne ke liye 🔙 Back dabao.")

    def on_document(m):
        state = _hyv_bk_pending.get(m.from_user.id)
        if state != "import":
            return
        if (m.document.file_size or 0) > _HYV_BK_MAX:
            bot.send_message(m.chat.id, "⚠️ File 50 MB se badi hai.")
            return
        tmp = _hyv_bk_tmp.mkdtemp(prefix="hyvbk_dl_")
        try:
            file_info = bot.get_file(m.document.file_id)
            data = bot.download_file(file_info.file_path)
            zip_path = _hyv_bk_os.path.join(tmp, "upload.zip")
            with open(zip_path, "wb") as fh:
                fh.write(data)
            info = _hyv_bk_stage(zip_path)
        except Exception:
            _hyv_bk_shutil.rmtree(tmp, ignore_errors=True)
            bot.send_message(m.chat.id, "❌ <b>Ye file import ke liye sahi nahi hai (bot 20 MB tak ki file leta hai).</b>\n"
                                        "Hyv backup wali .zip bhejo.", parse_mode="HTML")
            return
        _hyv_bk_shutil.rmtree(tmp, ignore_errors=True)
        _hyv_bk_pending[m.from_user.id] = info
        kb = _hyv_bk_types.InlineKeyboardMarkup()
        kb.row(_hyv_bk_types.InlineKeyboardButton("✅ Haan, restore karo", callback_data="hyvbk:yes"),
               _hyv_bk_types.InlineKeyboardButton("❌ Cancel", callback_data="hyvbk:no"))
        bot.send_message(m.chat.id, "🔎 <b>Backup check ho gaya</b>\n━━━━━━━━━━━━━━\n"
                                    "📄 Files: %d\n🕒 Backup time: %s\n\n"
                                    "⚠️ Current data pehle safety copy mein save hoga.\n"
                                    "Restore karein?" % (len(info["files"]), info["created"]),
                         parse_mode="HTML", reply_markup=kb)

    def on_confirm(c):
        if not _hyv_bk_is_admin(c.from_user.id):
            return
        info = _hyv_bk_pending.get(c.from_user.id)
        chat = c.message.chat.id
        if c.data == "hyvbk:no" or not isinstance(info, dict):
            _hyv_bk_pending.pop(c.from_user.id, None)
            if isinstance(info, dict):
                _hyv_bk_shutil.rmtree(info["stage"], ignore_errors=True)
            bot.answer_callback_query(c.id, "Cancel ho gaya")
            bot.edit_message_text("❌ Import cancel ho gaya.", chat, c.message.message_id)
            return
        bot.answer_callback_query(c.id, "Restore shuru")
        pm = bot.send_message(chat, "♻️ <b>Restore shuru</b>\n" + _hyv_bk_bar(0), parse_mode="HTML")
        try:
            for step in range(1, 5):
                bot.edit_message_text("♻️ <b>Restore ho raha hai</b>\n" + _hyv_bk_bar(step), chat,
                                      pm.message_id, parse_mode="HTML")
            safety = _hyv_bk_restore(info)
            bot.edit_message_text("✅ <b>Restore ho gaya</b>\n" + _hyv_bk_bar(5) + "\n\n"
                                  "🛟 Purana data safety copy mein hai: <code>%s</code>\n\n"
                                  "🔁 <b>Ab bot ko restart karo</b>, tab naya data chalega." %
                                  _hyv_bk_os.path.relpath(safety, _HYV_BK_HERE), chat, pm.message_id,
                                  parse_mode="HTML")
        except Exception:
            bot.edit_message_text("⚠️ <b>Restore nahi ho paya.</b> Kuch files nahi badli.", chat,
                                  pm.message_id, parse_mode="HTML")
        finally:
            _hyv_bk_pending.pop(c.from_user.id, None)
            _hyv_bk_shutil.rmtree(info["stage"], ignore_errors=True)

    def back(m):
        _hyv_bk_pending.pop(m.from_user.id, None)
        bot.send_message(m.chat.id, "🔙 Wapas aa gaye.", reply_markup=_hyv_bk_types.ReplyKeyboardRemove())

    before_m = len(bot.message_handlers)
    before_c = len(bot.callback_query_handlers)
    bot.message_handler(func=lambda m: admin_private(m) and m.text in ("/backup", "/hyvbackup", _HYV_BK_BTN_DB))(show_menu)
    bot.message_handler(func=lambda m: admin_private(m) and m.text == _HYV_BK_BTN_EXPORT)(export)
    bot.message_handler(func=lambda m: admin_private(m) and m.text == _HYV_BK_BTN_IMPORT)(start_import)
    bot.message_handler(func=lambda m: admin_private(m) and m.text == _HYV_BK_BTN_BACK)(back)
    bot.message_handler(content_types=["document"], func=lambda m: (
        m.chat.type == "private" and _hyv_bk_pending.get(m.from_user.id) == "import"))(on_document)
    bot.callback_query_handler(func=lambda c: (c.data or "").startswith("hyvbk:") and
                               _hyv_bk_is_admin(c.from_user.id))(on_confirm)
    # run before the bot's own handlers, but only for the exact buttons above (other messages pass through)
    new_m = bot.message_handlers[before_m:]
    del bot.message_handlers[before_m:]
    bot.message_handlers[0:0] = new_m
    new_c = bot.callback_query_handlers[before_c:]
    del bot.callback_query_handlers[before_c:]
    bot.callback_query_handlers[0:0] = new_c
# <<< hyv-injected:backup
'''

BACKUP_PTB = r'''# >>> hyv-injected:backup (added by the implementer bot: owner/admin Export and Import of data files)
import asyncio
from telegram import InlineKeyboardButton as _hyv_bk_IKB, InlineKeyboardMarkup as _hyv_bk_IKM
from telegram import KeyboardButton as _hyv_bk_KB, ReplyKeyboardMarkup as _hyv_bk_RKM
from telegram import ReplyKeyboardRemove as _hyv_bk_RKR
from telegram.ext import ApplicationHandlerStop as _hyv_bk_Stop
from telegram.ext import CallbackQueryHandler as _hyv_bk_CQH, MessageHandler as _hyv_bk_MH
from telegram.ext import filters as _hyv_bk_filters
import hashlib as _hyv_bk_hash
import inspect as _hyv_bk_inspect
import json as _hyv_bk_json
import os as _hyv_bk_os
import re as _hyv_bk_re
import shutil as _hyv_bk_shutil
import sqlite3 as _hyv_bk_sqlite
import tempfile as _hyv_bk_tmp
import time as _hyv_bk_time
import zipfile as _hyv_bk_zip

_HYV_BK_HERE = _hyv_bk_os.path.dirname(_hyv_bk_os.path.abspath(__file__))
_HYV_BK_SELF = _hyv_bk_os.path.basename(__file__)
_HYV_BK_MAX = 50 * 1024 * 1024
_HYV_BK_EXT = (".db", ".sqlite", ".sqlite3", ".json", ".csv", ".txt", ".pkl", ".pickle", ".dat", ".yaml", ".yml", ".xml")
_HYV_BK_DB_EXT = (".db", ".sqlite", ".sqlite3")
_HYV_BK_SKIP_DIRS = {"__pycache__", "venv", ".venv", "env", ".git", "node_modules"}
_HYV_BK_SECRET = _hyv_bk_re.compile(r"(\.env|token|secret|passw|credential|api_?key|private)", _hyv_bk_re.I)
_HYV_BK_CODE_PATH = _hyv_bk_re.compile(r"""["']([^"'\s]+\.(?:db|sqlite3?|json|csv|txt|pkl|pickle|dat|ya?ml|xml))["']""")
_HYV_BK_BTN_EXPORT = "📤 Export"
_HYV_BK_BTN_IMPORT = "📥 Import"
_HYV_BK_BTN_BACK = "🔙 Back"
_HYV_BK_BTN_DB = "🗄 Database Export/Import"   # the button the admin taps to open the backup menu
_HYV_BK_DB_LABELS = (_HYV_BK_BTN_DB, _HYV_BK_BTN_EXPORT, _HYV_BK_BTN_IMPORT)
_HYV_BK_BTN_STYLE = "primary"   # colour of the Database button (blue), same as the bot's option buttons
_hyv_bk_pending = {}   # admin user id -> "import" or staged restore info


_HYV_BK_ADMINS = {__HYV_ADMIN_ID__}   # Telegram id of the member who sent the bot file (set when added)


def _hyv_bk_is_admin(uid):
    ids = set(str(x) for x in _HYV_BK_ADMINS)
    for raw in _hyv_bk_os.environ.get("HYV_ADMIN_IDS", "").split(","):
        if raw.strip():
            ids.add(raw.strip())
    return str(uid) in ids


def _hyv_bk_excluded(rel):
    parts = rel.replace("\\", "/").split("/")
    if any(p in _HYV_BK_SKIP_DIRS or p.startswith("hyv_safety") for p in parts[:-1]):
        return True
    name = parts[-1]
    return (name == _HYV_BK_SELF or name.endswith((".zip", ".pyc")) or bool(_HYV_BK_SECRET.search(name)))


def _hyv_bk_sha(path):
    h = _hyv_bk_hash.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _hyv_bk_collect():
    picked = {}
    for root, dirs, files in _hyv_bk_os.walk(_HYV_BK_HERE):
        dirs[:] = [d for d in dirs if d not in _HYV_BK_SKIP_DIRS and not d.startswith("hyv_safety")]
        for f in files:
            full = _hyv_bk_os.path.join(root, f)
            rel = _hyv_bk_os.path.relpath(full, _HYV_BK_HERE).replace(_hyv_bk_os.sep, "/")
            if not _hyv_bk_excluded(rel) and f.lower().endswith(_HYV_BK_EXT):
                picked[rel] = full
    try:
        with open(__file__, "r", encoding="utf-8", errors="ignore") as fh:
            code = fh.read()
    except OSError:
        code = ""
    for m in _HYV_BK_CODE_PATH.finditer(code):
        p = m.group(1)
        if _hyv_bk_os.path.isabs(p):
            continue
        for base in (_HYV_BK_HERE, _hyv_bk_os.getcwd()):
            full = _hyv_bk_os.path.normpath(_hyv_bk_os.path.join(base, p))
            rel = _hyv_bk_os.path.relpath(full, _HYV_BK_HERE).replace(_hyv_bk_os.sep, "/")
            if (_hyv_bk_os.path.isfile(full) and not rel.startswith("..") and not _hyv_bk_excluded(rel)):
                picked[rel] = full
    return picked


def _hyv_bk_make_zip(picked, out_path):
    tmp = _hyv_bk_tmp.mkdtemp(prefix="hyvbk_")
    try:
        entries = []
        for i, (rel, full) in enumerate(sorted(picked.items())):
            src = full
            if rel.lower().endswith(_HYV_BK_DB_EXT):
                src = _hyv_bk_os.path.join(tmp, "snap%d" % i)
                s = _hyv_bk_sqlite.connect(full)
                try:
                    d = _hyv_bk_sqlite.connect(src)
                    try:
                        s.backup(d)   # reads -wal/-shm too, so the copy is consistent
                    finally:
                        d.close()
                finally:
                    s.close()
            entries.append((rel, src))
        manifest = {"app": "hyv-backup", "version": 1,
                    "created": _hyv_bk_time.strftime("%Y-%m-%d %H:%M:%S"), "files": []}
        with _hyv_bk_zip.ZipFile(out_path, "w", _hyv_bk_zip.ZIP_DEFLATED) as z:
            for rel, src in entries:
                manifest["files"].append({"path": rel, "size": _hyv_bk_os.path.getsize(src),
                                          "sha256": _hyv_bk_sha(src)})
                z.write(src, rel)
            z.writestr("manifest.json", _hyv_bk_json.dumps(manifest, indent=2, ensure_ascii=False))
        return manifest
    finally:
        _hyv_bk_shutil.rmtree(tmp, ignore_errors=True)


def _hyv_bk_stage(zip_path):
    """Check the backup fully and unpack it into a temp folder. Nothing in the bot folder is touched."""
    stage = _hyv_bk_tmp.mkdtemp(prefix="hyvbk_in_")
    try:
        if _hyv_bk_os.path.getsize(zip_path) > _HYV_BK_MAX:
            raise ValueError("file 50 MB se badi hai")
        with _hyv_bk_zip.ZipFile(zip_path) as z:
            if z.testzip() is not None:
                raise ValueError("zip file kharab hai")
            manifest = _hyv_bk_json.loads(z.read("manifest.json").decode("utf-8"))
            if manifest.get("app") != "hyv-backup" or not isinstance(manifest.get("files"), list):
                raise ValueError("ye hyv backup file nahi hai")
            names = set(z.namelist())
            total = 0
            staged = []
            for item in manifest["files"]:
                rel = item.get("path")
                if (not isinstance(rel, str) or not rel or rel.startswith(("/", "\\")) or ":" in rel
                        or "\\" in rel or ".." in rel.split("/") or _hyv_bk_excluded(rel)):
                    raise ValueError("unsafe path in backup")
                if rel not in names:
                    raise ValueError("file missing in backup: " + rel)
                info = z.getinfo(rel)
                total += info.file_size
                if info.file_size != item.get("size") or total > _HYV_BK_MAX:
                    raise ValueError("size check failed")
                target = _hyv_bk_os.path.normpath(_hyv_bk_os.path.join(stage, rel))
                if not target.startswith(stage + _hyv_bk_os.sep):
                    raise ValueError("unsafe path in backup")
                _hyv_bk_os.makedirs(_hyv_bk_os.path.dirname(target), exist_ok=True)
                with z.open(rel) as src, open(target, "wb") as dst:
                    _hyv_bk_shutil.copyfileobj(src, dst)
                if _hyv_bk_sha(target) != item.get("sha256"):
                    raise ValueError("checksum mismatch: " + rel)
                staged.append((rel, target))
        return {"stage": stage, "files": staged, "created": manifest.get("created", "?")}
    except Exception:
        _hyv_bk_shutil.rmtree(stage, ignore_errors=True)
        raise


def _hyv_bk_restore(info):
    stamp = _hyv_bk_time.strftime("%Y%m%d_%H%M%S")
    safety = _hyv_bk_os.path.join(_HYV_BK_HERE, "hyv_safety", "before_import_" + stamp)
    for rel, _ in info["files"]:
        cur = _hyv_bk_os.path.join(_HYV_BK_HERE, rel)
        for path in (cur, cur + "-wal", cur + "-shm"):
            if _hyv_bk_os.path.isfile(path):
                dst = _hyv_bk_os.path.join(safety, _hyv_bk_os.path.relpath(path, _HYV_BK_HERE))
                _hyv_bk_os.makedirs(_hyv_bk_os.path.dirname(dst), exist_ok=True)
                _hyv_bk_shutil.copy2(path, dst)
    for rel, tmp_path in info["files"]:
        dst = _hyv_bk_os.path.join(_HYV_BK_HERE, rel)
        _hyv_bk_os.makedirs(_hyv_bk_os.path.dirname(dst), exist_ok=True)
        if rel.lower().endswith(_HYV_BK_DB_EXT):
            for side in ("-wal", "-shm"):
                if _hyv_bk_os.path.isfile(dst + side):
                    _hyv_bk_os.remove(dst + side)   # old sidecar must not replay over the restored db
        _hyv_bk_shutil.copy2(tmp_path, dst)
    return safety


def _hyv_bk_bar(done, total=5):
    return "▰" * done + "▱" * (total - done)


async def _hyv_bk_async_run(fn, *args):
    return await asyncio.to_thread(fn, *args)


def _hyv_bk_admin_chat(chat_id):
    """True when the chat is the private chat of an admin (in a private chat chat_id is the user id)."""
    return chat_id is not None and not isinstance(chat_id, bool) and _hyv_bk_is_admin(chat_id)


def _hyv_bk_btn(label):
    """The Database button with its colour (style). A python-telegram-bot version without the style field
    gets it in api_kwargs (sent to Telegram as it is); if that fails too, a plain button."""
    try:
        return _hyv_bk_KB(label, style=_HYV_BK_BTN_STYLE)
    except TypeError:
        pass
    try:
        return _hyv_bk_KB(label, api_kwargs={"style": _HYV_BK_BTN_STYLE})
    except TypeError:
        return _hyv_bk_KB(label)


def _hyv_bk_db_markup(markup):
    """The same reply keyboard with the Database button in one extra last row. The original object is not changed."""
    if not isinstance(markup, _hyv_bk_RKM):
        return markup
    rows = [list(row) for row in markup.keyboard]
    texts = [getattr(b, "text", b) for row in rows for b in row]
    if any(t in _HYV_BK_DB_LABELS for t in texts):
        return markup
    rows.append([_hyv_bk_btn(_HYV_BK_BTN_DB)])
    extra = {"is_persistent": markup.is_persistent} if hasattr(markup, "is_persistent") else {}
    return _hyv_bk_RKM(rows, resize_keyboard=markup.resize_keyboard, one_time_keyboard=markup.one_time_keyboard,
                       selective=markup.selective, input_field_placeholder=markup.input_field_placeholder, **extra)


def _hyv_bk_ptb_wrap(fn, sig):
    async def send(self, *args, **kwargs):
        try:
            bound = sig.bind_partial(self, *args, **kwargs)
        except TypeError:
            return await fn(self, *args, **kwargs)
        if _hyv_bk_admin_chat(bound.arguments.get("chat_id")) and bound.arguments.get("reply_markup") is not None:
            bound.arguments["reply_markup"] = _hyv_bk_db_markup(bound.arguments["reply_markup"])
        return await fn(*bound.args, **bound.kwargs)
    send.__name__ = fn.__name__
    send.__qualname__ = fn.__qualname__
    return send


def _hyv_bk_patch_ptb_bot(bot):
    """python-telegram-bot: the bot object gets a subclass (same name, no new fields) whose send_* methods
    that take a reply keyboard add the Database row when the chat is an admin's private chat."""
    base = type(bot)
    if getattr(base, "_hyv_bk_patched", False):
        return
    ns = {"__slots__": (), "_hyv_bk_patched": True}
    for name in dir(base):
        fn = getattr(base, name, None)
        if not name.startswith("send_") or not _hyv_bk_inspect.isfunction(fn):
            continue
        sig = _hyv_bk_inspect.signature(fn)
        if "reply_markup" in sig.parameters:
            ns[name] = _hyv_bk_ptb_wrap(fn, sig)
    bot.__class__ = type(base.__name__, (base,), ns)


def _hyv_bk_register(app):
    _hyv_bk_patch_ptb_bot(app.bot)
    def is_admin(update):
        chat = update.effective_chat
        user = update.effective_user
        return bool(chat and user and chat.type == "private" and _hyv_bk_is_admin(user.id))

    def menu_markup():
        return _hyv_bk_RKM([[_hyv_bk_KB(_HYV_BK_BTN_EXPORT), _hyv_bk_KB(_HYV_BK_BTN_IMPORT)],
                            [_hyv_bk_KB(_HYV_BK_BTN_BACK)]], resize_keyboard=True)

    async def show_menu(update, context):
        _hyv_bk_pending.pop(update.effective_user.id, None)
        await context.bot.send_message(update.effective_chat.id,
                                       "🗄 <b>Database Export / Import</b>\n━━━━━━━━━━━━━━\n"
                                       "📤 <b>Export</b> — sab data ek zip file mein\n"
                                       "📥 <b>Import</b> — zip file se data wapas laao\n\n"
                                       "ℹ️ Import ke baad bot ko restart karna hoga.",
                                       parse_mode="HTML", reply_markup=menu_markup())

    async def export(update, context):
        chat_id = update.effective_chat.id
        pm = await context.bot.send_message(chat_id, "📦 <b>Export shuru</b>\n" + _hyv_bk_bar(0),
                                            parse_mode="HTML")
        try:
            for step in range(1, 5):
                await context.bot.edit_message_text("📦 <b>Export ho raha hai</b>\n" + _hyv_bk_bar(step),
                                                    chat_id=chat_id, message_id=pm.message_id,
                                                    parse_mode="HTML")
            picked = await _hyv_bk_async_run(_hyv_bk_collect)
            if not picked:
                await context.bot.edit_message_text("⚠️ Export ke liye koi data file nahi mili.",
                                                    chat_id=chat_id, message_id=pm.message_id)
                return
            tmp = _hyv_bk_tmp.mkdtemp(prefix="hyvbk_out_")
            try:
                out = _hyv_bk_os.path.join(tmp, "hyv_backup_%s.zip" % _hyv_bk_time.strftime("%Y%m%d_%H%M%S"))
                manifest = await _hyv_bk_async_run(_hyv_bk_make_zip, picked, out)
                if _hyv_bk_os.path.getsize(out) > _HYV_BK_MAX:
                    await context.bot.edit_message_text("⚠️ Backup 50 MB se badi hai, Telegram par nahi bhej sakte.",
                                                        chat_id=chat_id, message_id=pm.message_id)
                    return
                await context.bot.edit_message_text("📦 <b>Export</b>\n" + _hyv_bk_bar(5) + "\n✅ Ho gaya",
                                                    chat_id=chat_id, message_id=pm.message_id,
                                                    parse_mode="HTML")
                with open(out, "rb") as fh:
                    await context.bot.send_document(
                        chat_id, document=fh, filename=_hyv_bk_os.path.basename(out),
                        caption="🗄 <b>Backup ready</b>\n📄 Files: %d\n🕒 %s" % (len(manifest["files"]),
                                                                              manifest["created"]),
                        parse_mode="HTML")
            finally:
                _hyv_bk_shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            await context.bot.edit_message_text("⚠️ <b>Export nahi ho paya.</b> Thodi der baad dobara try karo.",
                                                chat_id=chat_id, message_id=pm.message_id, parse_mode="HTML")

    async def start_import(update, context):
        _hyv_bk_pending[update.effective_user.id] = "import"
        await context.bot.send_message(update.effective_chat.id,
                                       "📥 <b>Import</b>\n━━━━━━━━━━━━━━\nAb hyv backup wali .zip file bhejo.\n"
                                       "Cancel karne ke liye 🔙 Back dabao.",
                                       parse_mode="HTML", reply_markup=menu_markup())

    async def back(update, context):
        _hyv_bk_pending.pop(update.effective_user.id, None)
        await context.bot.send_message(update.effective_chat.id, "🔙 Wapas aa gaye.",
                                       reply_markup=_hyv_bk_RKR())

    async def on_document(update, context):
        msg = update.effective_message
        chat_id = update.effective_chat.id
        if (msg.document.file_size or 0) > _HYV_BK_MAX:
            await context.bot.send_message(chat_id, "⚠️ File 50 MB se badi hai.")
            return
        tmp = _hyv_bk_tmp.mkdtemp(prefix="hyvbk_dl_")
        try:
            zip_path = _hyv_bk_os.path.join(tmp, "upload.zip")
            tg_file = await msg.document.get_file()
            await tg_file.download_to_drive(zip_path)
            info = await _hyv_bk_async_run(_hyv_bk_stage, zip_path)
        except Exception:
            _hyv_bk_shutil.rmtree(tmp, ignore_errors=True)
            await context.bot.send_message(chat_id, "❌ <b>Ye file import ke liye sahi nahi hai (bot 20 MB tak ki file leta hai).</b>\n"
                                                    "Hyv backup wali .zip bhejo.", parse_mode="HTML")
            return
        _hyv_bk_shutil.rmtree(tmp, ignore_errors=True)
        _hyv_bk_pending[update.effective_user.id] = info
        kb = _hyv_bk_IKM([[_hyv_bk_IKB("✅ Haan, restore karo", callback_data="hyvbk:yes"),
                           _hyv_bk_IKB("❌ Cancel", callback_data="hyvbk:no")]])
        await context.bot.send_message(chat_id, "🔎 <b>Backup check ho gaya</b>\n━━━━━━━━━━━━━━\n"
                                                "📄 Files: %d\n🕒 Backup time: %s\n\n"
                                                "⚠️ Current data pehle safety copy mein save hoga.\n"
                                                "Restore karein?" % (len(info["files"]), info["created"]),
                                       parse_mode="HTML", reply_markup=kb)

    async def on_text(update, context):
        if not is_admin(update):
            return
        text = update.effective_message.text or ""
        if text in ("/backup", "/hyvbackup", _HYV_BK_BTN_DB):
            await show_menu(update, context)
        elif text == _HYV_BK_BTN_EXPORT:
            await export(update, context)
        elif text == _HYV_BK_BTN_IMPORT:
            await start_import(update, context)
        elif text == _HYV_BK_BTN_BACK:
            await back(update, context)
        else:
            return
        raise _hyv_bk_Stop()

    async def on_any_document(update, context):
        if not is_admin(update) or _hyv_bk_pending.get(update.effective_user.id) != "import":
            return
        await on_document(update, context)
        raise _hyv_bk_Stop()

    async def on_confirm(update, context):
        query = update.callback_query
        if not query or not _hyv_bk_is_admin(query.from_user.id):
            return
        await query.answer()
        chat_id = query.message.chat.id
        info = _hyv_bk_pending.get(query.from_user.id)
        if query.data == "hyvbk:no" or not isinstance(info, dict):
            _hyv_bk_pending.pop(query.from_user.id, None)
            if isinstance(info, dict):
                _hyv_bk_shutil.rmtree(info["stage"], ignore_errors=True)
            await query.edit_message_text("❌ Import cancel ho gaya.")
            raise _hyv_bk_Stop()
        pm = await context.bot.send_message(chat_id, "♻️ <b>Restore shuru</b>\n" + _hyv_bk_bar(0),
                                            parse_mode="HTML")
        try:
            for step in range(1, 5):
                await context.bot.edit_message_text("♻️ <b>Restore ho raha hai</b>\n" + _hyv_bk_bar(step),
                                                    chat_id=chat_id, message_id=pm.message_id,
                                                    parse_mode="HTML")
            safety = await _hyv_bk_async_run(_hyv_bk_restore, info)
            await context.bot.edit_message_text(
                "✅ <b>Restore ho gaya</b>\n" + _hyv_bk_bar(5) + "\n\n"
                "🛟 Purana data safety copy mein hai: <code>%s</code>\n\n"
                "🔁 <b>Ab bot ko restart karo</b>, tab naya data chalega." %
                _hyv_bk_os.path.relpath(safety, _HYV_BK_HERE), chat_id=chat_id,
                message_id=pm.message_id, parse_mode="HTML")
        except Exception:
            await context.bot.edit_message_text("⚠️ <b>Restore nahi ho paya.</b> Kuch files nahi badli.",
                                                chat_id=chat_id, message_id=pm.message_id, parse_mode="HTML")
        finally:
            _hyv_bk_pending.pop(query.from_user.id, None)
            _hyv_bk_shutil.rmtree(info["stage"], ignore_errors=True)
        raise _hyv_bk_Stop()

    # group -1 runs before the bot's own handlers; the handler stops the chain only for the exact buttons
    app.add_handler(_hyv_bk_CQH(on_confirm, pattern=r"^hyvbk:"), group=-1)
    app.add_handler(_hyv_bk_MH(_hyv_bk_filters.Document.ALL & _hyv_bk_filters.ChatType.PRIVATE,
                               on_any_document), group=-1)
    app.add_handler(_hyv_bk_MH(_hyv_bk_filters.TEXT & _hyv_bk_filters.ChatType.PRIVATE, on_text), group=-1)
# <<< hyv-injected:backup
'''


WEB_SURE = [
    r"hyv-injected:web",                                        # our own block, added before
    r"^\s*(?:from|import)\s+(?:flask|fastapi|bottle|aiohttp|tornado|sanic|quart|waitress"
    r"|gunicorn|uvicorn|http\.server|socketserver)\b",           # web framework / http server import
    r"\bFlask\s*\(",                                             # Flask app object
    r"HTTPServer\b|\bserve_forever\s*\(",                        # stdlib http server (also ThreadingHTTPServer)
    r"\bstart_web\s*\(",                                         # keep_alive.start_web() style
    r"\.run\([^)]*0\.0\.0\.0",                                   # app.run(host="0.0.0.0", ...)
    r"\bweb\.(?:run_app|Application|AppRunner|TCPSite)\b",       # aiohttp server calls
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


OWN_URL = re.compile(r"localhost|127\.0\.0\.1|RENDER_EXTERNAL_URL|PING_URL|SELF_URL|0\.0\.0\.0", re.I)


def _loop_pinger(node, text):
    """A def that pings its own address on a timer: a while True loop with a sleep, or a def that
    schedules itself again with threading.Timer. Both must make a web request to the bot's own address
    (localhost, 127.0.0.1, RENDER_EXTERNAL_URL, ...). Other periodic web requests are not pingers."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    if not (re.search(PERIODIC_HTTP, text) and OWN_URL.search(text)):
        return False
    forever = any(isinstance(c, ast.While) and isinstance(c.test, ast.Constant) and c.test.value is True
                  for c in ast.walk(node))
    rescheduled = any(isinstance(c, ast.Call) and _call_name(c) == "Timer"
                      and any(isinstance(a, ast.Name) and a.id == node.name for a in c.args)
                      for c in ast.walk(node))
    return (forever and bool(re.search(PERIODIC_SLEEP, text))) or rescheduled


def _has_loop_pinger(source):
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False
    lines = source.splitlines()
    return any(_loop_pinger(n, "\n".join(lines[_span(n)[0] - 1:n.end_lineno]))
               for n in ast.walk(tree) if isinstance(n, DEF_KINDS))


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
    if _any(PINGER_SURE, source, re.I | re.M) or _has_loop_pinger(source):
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

BACKUP_MARK = "hyv-injected:backup"


def backup_library(source):
    """telebot, python-telegram-bot v20+, or None (unsupported or unknown)."""
    if re.search(r"^\s*(from|import)\s+(aiogram|pyrogram|telethon)\b", source, re.M):
        return None
    if re.search(r"^\s*(import telebot\b|from telebot\b)", source, re.M):
        return "telebot"
    if re.search(r"^\s*(from|import)\s+telegram\b", source, re.M) and \
            re.search(r"\bApplicationBuilder\b|\bApplication\b", source):
        return "ptb"
    return None


def _assign_parts(node):
    """(name, value) for `name = call` and `name: T = call` statements, else None."""
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) \
            and isinstance(node.value, ast.Call):
        return node.targets[0].id, node.value
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and isinstance(node.value, ast.Call):
        return node.target.id, node.value
    return None


def _bot_statement(tree, lib):
    """The first statement (anywhere in the file) that creates the bot (telebot) or the Application (PTB)."""
    found = []
    for node in ast.walk(tree):
        parts = _assign_parts(node)
        if parts is None:
            continue
        func = parts[1].func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if (lib == "telebot" and name in ("TeleBot", "SafeTeleBot")) or (lib == "ptb" and name == "build"):
            found.append(node)
    return min(found, key=lambda n: n.lineno) if found else None


class _HypStrip(ast.NodeTransformer):
    """Removes the register lines (by their dump) so the rest can be compared with the original."""
    def __init__(self, reg_dump):
        self.reg_dump = reg_dump

    def visit_Expr(self, node):
        return None if ast.dump(node) == self.reg_dump else node


def backup_block(lib, admin_id):
    """The telebot or PTB template, with the admin id filled in."""
    block = BACKUP_TELEBOT if lib == "telebot" else BACKUP_PTB
    return block.replace("__HYV_ADMIN_ID__", str(int(admin_id)))


def inject_backup(source, lib, admin_id):
    """Adds the backup block after the imports and one register line after the bot is created.
    Raises ValueError when the bot object is not found or the checks fail."""
    tree = ast.parse(source)
    stmt = _bot_statement(tree, lib)
    if stmt is None:
        raise ValueError("bot object not found")
    var = _assign_parts(stmt)[0]
    reg_line = " " * stmt.col_offset + f"_hyv_bk_register({var})  # hyv-injected:backup-register\n"
    lines = source.splitlines(keepends=True)
    end = stmt.end_lineno
    if not lines[end - 1].endswith("\n"):
        lines[end - 1] += "\n"
    src1 = "".join(lines[:end]) + reg_line + "".join(lines[end:])
    body = ast.parse(src1).body
    k = 0
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        k = 1
    while k < len(body) and isinstance(body[k], (ast.Import, ast.ImportFrom)):
        k += 1
    at = body[k - 1].end_lineno if k else 0
    block = backup_block(lib, admin_id)
    block_text = "\n" + block + "\n"
    l1 = src1.splitlines(keepends=True)
    head, tail = l1[:at], l1[at:]
    if head and not head[-1].endswith("\n"):
        head[-1] += "\n"
    new_source = "".join(head) + block_text + "".join(tail)

    new_tree = ast.parse(new_source)
    first = at + 1
    last = at + block_text.count("\n")
    ours = [n for n in new_tree.body if first <= n.lineno <= last]
    reg_dump = ast.dump(ast.parse(reg_line.strip()).body[0])
    regs = [n for n in ast.walk(new_tree) if isinstance(n, ast.Expr) and ast.dump(n) == reg_dump]
    stripped = _HypStrip(reg_dump).visit(ast.parse(new_source))
    stripped.body = [n for n in stripped.body if not (first <= n.lineno <= last)]
    same = ast.dump(stripped) == ast.dump(ast.parse(source)) or ast.dump(
        ast.Module(body=[n for n in stripped.body], type_ignores=[])) == ast.dump(
        ast.Module(body=tree.body, type_ignores=[]))
    if len(ours) != len(ast.parse(block).body) or len(regs) != 1 or not same:
        raise ValueError("injection check failed")
    return new_source


def backup_done_text(admin_id, extra=""):
    text = (f"Database Export/Import add kar diya. Admin ID {admin_id} (jisne file bheji) ko hi button dikhega. "
            "Button har reply keyboard me runtime se judta hai, isliye bot ke kisi bhi menu me neeche dikhega. "
            "Extra admin ke liye Render me HYV_ADMIN_IDS env variable me comma se ID daal sakte ho. "
            "Naya file Render par deploy karke bot restart karna.")
    return f"{text} {extra}".strip()


def _backup_strip(source):
    """The source without the injected block and its register line (the exact reverse of inject_backup),
    or None when the source has no block. Used to replace an older block with the current template."""
    m = re.search(r"^# >>> hyv-injected:backup\b[^\n]*\n", source, re.M)
    if m is None:
        return None
    e = re.search(r"^# <<< hyv-injected:backup\b[^\n]*(\n|$)", source[m.start():], re.M)
    if e is None:
        return None
    start, end = m.start(), m.start() + e.end()
    head, tail = source[:start], source[end:]
    if head.endswith("\n\n"):      # the blank line inserted before the block
        head = head[:-1]
    if tail.startswith("\n"):       # the blank line inserted after the block
        tail = tail[1:]
    out = head + tail
    return re.sub(r"^[ \t]*_hyv_bk_register\(\w+\)[ \t]*# hyv-injected:backup-register[ \t]*\n", "", out, flags=re.M)


def plan_backup(source, admin_id=None):
    """Returns (new_source, reply_text) for the Database Export/Import option.
    Only the built-in templates are used: pyTelegramBotAPI (telebot) and python-telegram-bot.
    admin_id is the Telegram id of the member who sent the file; it is written into the injected code."""
    already = "Database Export/Import pehle se is file mein hai. Isliye maine kuch add nahi kiya."
    if not isinstance(admin_id, int) or isinstance(admin_id, bool) or admin_id <= 0:
        return None, ("File bhejne wale ka Telegram ID nahi mila, isliye Database Export/Import nahi jod paya. "
                      "File waisi hi rahegi.")
    if BACKUP_MARK in source:
        # An older block from an earlier version is swapped for the current template. An identical one is left alone.
        stripped = _backup_strip(source)
        lib = backup_library(stripped) if stripped is not None else None
        if stripped is None or lib is None:
            return None, already
        try:
            new_source = inject_backup(stripped, lib, admin_id)
        except ValueError:
            return None, "Database Export/Import ka purana code badalne mein dikkat aayi. File waisi hi rahegi."
        if new_source == source:
            return None, already
        return new_source, ("Database Export/Import ka purana code naye version se badal diya. "
                            "Admin ID %d ko button dikhega. Button har reply keyboard me runtime se judta hai, sirf admin ke liye. "
                            "Naya file Render par deploy karke bot restart karna." % admin_id)
    lib = backup_library(source)
    if lib is None:
        return None, ("Ye bot library abhi support nahi hai (Database Export/Import sirf pyTelegramBotAPI "
                      "aur python-telegram-bot ke liye hai). File waisi hi rahegi.")
    try:
        return inject_backup(source, lib, admin_id), backup_done_text(admin_id)
    except ValueError:
        return None, "Is file mein bot ka main object nahi mila. File waisi hi rahegi."


# ---- Removing old web server / pinger code (built in, no network) -------------------
# The code finds the old statements in the file's own syntax tree, deletes only those statements
# (and the code that depends on them), checks the result, and then the normal injection adds the
# new block. Anything that cannot be done exactly leaves the file untouched.

REMOVE_FORBID = re.compile(
    r"infinity_polling|run_polling|start_polling|\.idle\s*\(|asyncio\.run|\bTeleBot\s*\(|Application\.builder"
    r"|\bUpdater\s*\(|add_handler|message_handler|callback_query_handler|CommandHandler|MessageHandler"
    r"|__name__\s*==|hyv-injected:backup|_hyv_bk_|_HYV_BK_"
    r"|webhook|WEBHOOK|set_webhook|delete_webhook")   # a webhook setup is never removed (it is reported)
REMOVE_MAX_PICKS = 200
DEF_KINDS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


class RemovalError(Exception):
    """A safe Hinglish reason why the old code could not be removed exactly."""


def _span(node):
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    return start, node.end_lineno


def _stmts(tree):
    return [n for n in ast.walk(tree) if isinstance(n, ast.stmt)]


def _parents(tree):
    return {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}


def _bound(node):
    """Names a single statement binds itself (not names bound inside its child statements)."""
    out = set()
    if isinstance(node, DEF_KINDS):
        out.add(node.name)
    elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for t in targets:
            out |= {x.id for x in ast.walk(t) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)}
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for a in node.names:
            out.add(a.asname or a.name.split(".")[0])
    return out


def _own_names(node):
    """Names a statement loads in its own expressions (decorators, tests, values), not in child statements."""
    names, stack = set(), list(ast.iter_child_nodes(node))
    while stack:
        c = stack.pop()
        if isinstance(c, ast.stmt):
            continue
        if isinstance(c, ast.Name) and isinstance(c.ctx, ast.Load):
            names.add(c.id)
        stack.extend(ast.iter_child_nodes(c))
    return names


def _marker_ranges(lines, kind):
    out, start = [], None
    for i, line in enumerate(lines, 1):
        if f"# >>> hyv-injected:{kind}" in line:
            start = i
        elif f"# <<< hyv-injected:{kind}" in line and start is not None:
            out.append((start, i))
            start = None
    return out


def _base_match(node, kind, head, text, markers, targets):
    """Is this statement plainly part of the old web server or pinger? head is the text a def is judged
    by (its decorators and header), text is the whole statement, targets the names used as a thread
    target somewhere in the file. Sure patterns only, plus a thread target that sleeps and makes web
    requests."""
    s, e = _span(node)
    if any(a <= s and e <= b for a, b in markers):
        return True
    if kind == "web":
        return _any(WEB_SURE, head, re.M)
    if _any(PINGER_SURE, head, re.I | re.M) or _loop_pinger(node, text):
        return True
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return (node.name in targets
                and bool(re.search(PERIODIC_SLEEP, text)) and bool(re.search(PERIODIC_HTTP, text)))
    return False


def _related(tree, lines, kinds):
    """Statements that belong to the chosen kinds: the plain matches, plus every statement that
    depends on a name those matches bind (for example the thread start that calls run_web)."""
    targets = set(re.findall(r"target\s*=\s*(\w+)", "\n".join(lines)))
    stmts = _stmts(tree)
    own = {n: _own_names(n) for n in stmts}
    segs = {n: "\n".join(lines[_span(n)[0] - 1:_span(n)[1]]) for n in stmts}
    heads = {}
    for n in stmts:
        heads[n] = segs[n]
        if isinstance(n, DEF_KINDS) and n.body and n.body[0].lineno > n.lineno:
            heads[n] = "\n".join(lines[_span(n)[0] - 1:n.body[0].lineno - 1])
    forbidden = {n for n in stmts if REMOVE_FORBID.search(segs[n])}
    # docstrings and other bare strings are text, never code to delete
    forbidden |= {n for n in stmts if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                  and isinstance(n.value.value, str)}
    parents = _parents(tree)
    counts = _binder_counts(stmts)
    uses = {}                                 # name -> statements that load it in their own expressions
    for n in stmts:
        for name in own[n]:
            uses.setdefault(name, []).append(n)
    rel = set()
    for kind in kinds:
        markers = _marker_ranges(lines, kind)
        cur = {n for n in stmts if n not in forbidden
               and _base_match(n, kind, heads[n], segs[n], markers, targets)}
        base = set(cur)
        changed = True
        while changed:
            changed = False
            names = _trigger_names(cur, counts)
            func_names = {c.name for c in cur if isinstance(c, DEF_KINDS)}
            for n in stmts:
                if n in cur or n in forbidden:
                    continue
                # a user's def that only calls the old functions (a route that returns keep_alive()) is user
                # code: it is not pulled in by those names, so the dangling-reference check refuses instead
                if isinstance(n, DEF_KINDS):
                    by_name = bool(own[n] & (names - func_names))
                else:
                    by_name = bool(own[n] & names)
                # a def used only from related code (a helper that only run_web() calls) goes too
                by_users = (isinstance(n, DEF_KINDS) and counts.get(n.name, 0) == 1
                            and _only_used_inside(n.name, uses.get(n.name, []), n, cur, parents))
                # a def is a unit when all of its body is related (a thread target, for example)
                by_body = isinstance(n, DEF_KINDS) and bool(n.body) and all(b in base for b in n.body)
                # the sleep line of a pinger loop belongs to the pinger
                by_sleep = (kind == "pinger" and parents.get(n) in cur
                            and not any(hasattr(n, a) for a in ("body", "handlers", "orelse", "finalbody"))
                            and bool(re.search(PERIODIC_SLEEP, segs[n])))
                if by_name or by_users or by_body or by_sleep:
                    cur.add(n)
                    changed = True
        rel |= cur
    return rel


def _only_used_inside(name, users, defn, cur, parents):
    """True when the name of a def is used somewhere, and every use is inside a related statement."""
    found = False
    for u in users:
        if u is defn:
            continue
        found = True
        p, inside = u, False
        while p is not None:
            if p in cur:
                inside = True
                break
            p = parents.get(p)
        if not inside:
            return False
    return found


def _candidate_lines(source, kinds):
    """(start, end, first line) for every statement that belongs to these kinds."""
    lines = source.splitlines()
    tree = ast.parse(source)
    rel = _related(tree, lines, kinds)
    return [(*_span(n), lines[_span(n)[0] - 1].strip()[:80]) for n in _stmts(tree) if n in rel]


def _binder_counts(stmts):
    """How many statements bind each name (imports are shared, so they are not counted here)."""
    counts = {}
    for n in stmts:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            continue
        for name in _bound(n):
            counts[name] = counts.get(name, 0) + 1
    return counts


def _trigger_names(nodes, counts):
    """Names bound by these statements that no other statement in the file binds. Only these can pull
    a dependant in or count as a dangling reference, so a shared name (an import, or a name set twice)
    never drags unrelated code along."""
    out = set()
    for n in nodes:
        if not isinstance(n, (ast.Import, ast.ImportFrom)):
            out |= {x for x in _bound(n) if counts.get(x, 0) == 1}
    return out


def _label(node):
    if isinstance(node, DEF_KINDS):
        return ("class " if isinstance(node, ast.ClassDef) else "function ") + node.name
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return "import line"
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        names = sorted(_bound(node))
        return ("setting " + ", ".join(names[:2])) if names else "setting line"
    return "code line"


def _remove_code(source, picks, kinds):
    """Deletes the picked statements (and what depends on them). Returns (new_source, removed) where
    removed is a list of (start, end, label). Raises RemovalError when anything is unsafe."""
    lines = source.splitlines(keepends=True)
    plain = [ln.rstrip("\r\n") for ln in lines]
    tree = ast.parse(source)
    rel = _related(tree, plain, kinds)
    stmts = _stmts(tree)
    parents = _parents(tree)
    spans = {n: _span(n) for n in stmts}
    by_span = {}
    for n in stmts:
        if n in rel:
            by_span.setdefault(spans[n], n)

    def gone(n):
        while n is not None:
            if n in removed:
                return True
            n = parents.get(n)
        return False

    if not picks:
        raise RemovalError("Purana web server ya pinger code nahi mila.")
    counts = _binder_counts(stmts)
    removed = set()
    for pick in picks[:REMOVE_MAX_PICKS]:
        if pick not in by_span:
            raise RemovalError(f"Line {pick[0]}-{pick[1]} purana web server ya pinger code nahi hai.")
        removed.add(by_span[pick])

    markers = {k: _marker_ranges(plain, k) for k in kinds}
    own = {n: _own_names(n) for n in stmts}
    changed = True
    while changed:
        changed = False
        names = _trigger_names(removed, counts)
        for n in stmts:                       # dependants of removed names
            if gone(n) or not (own[n] & names):
                continue
            if n not in rel:
                raise RemovalError(f"Line {spans[n][0]} ka code hataye gaye code par depend karta hai, "
                                   "aur wo web server ya pinger ka code nahi hai.")
            removed.add(n)
            changed = True
            break
        if changed:
            continue
        for k in kinds:                       # a marker block goes as a whole
            for a, b in markers[k]:
                top = [n for n in stmts if parents.get(n) is tree and a <= spans[n][0] and spans[n][1] <= b]
                if any(gone(n) for n in top):
                    for n in top:
                        if n not in removed:
                            removed.add(n)
                            changed = True

    for n in removed:                         # overbroad: nothing unrelated inside a removed if/while/with/try
        if isinstance(n, DEF_KINDS):
            continue                          # a def is one unit: its body is part of it
        for d in ast.walk(n):
            if d is not n and isinstance(d, ast.stmt) and d not in rel:
                raise RemovalError(f"Lines {spans[n][0]}-{spans[n][1]} me purana code ke saath aur code bhi hai. "
                                   "Chhote hisse chuno.")

    top = [n for n in stmts if n in removed and not gone(parents.get(n))]
    for n in top:
        s, e = spans[n]
        p = parents.get(n)
        indent = re.match(r"[ \t]*", plain[s - 1]).group(0)
        if len(indent) != n.col_offset:
            raise RemovalError(f"Line {s} par ek se zyada code hai, isliye ise safely nahi hata sakta.")
        if p is not None and not isinstance(p, ast.Module) and p.lineno == s:
            raise RemovalError(f"Line {s} par ek hi line me block hai, isliye ise safely nahi hata sakta.")
        anc = set()
        q = p
        while q is not None:
            anc.add(q)
            q = parents.get(q)
        for m in stmts:
            if m in removed or m in anc or gone(m) or m is n:
                continue
            if not (spans[m][1] < s or spans[m][0] > e):
                raise RemovalError(f"Line {s} ka code kisi aur code ke saath ek hi line par hai. Ise safely nahi hata sakta.")

    drop, repl = set(), {}
    for n in top:
        s, e = spans[n]
        drop.update(range(s, e + 1))
    for k in kinds:
        for a, b in markers[k]:
            if any(parents.get(n) is tree and a <= spans[n][0] and spans[n][1] <= b and n in removed for n in stmts):
                drop.update(range(a, b + 1))
    for p in ast.walk(tree):
        body = getattr(p, "body", None)
        if isinstance(p, ast.Module) or not isinstance(body, list) or not body or gone(p) or p in removed:
            continue
        if all(b in removed for b in body):
            s = spans[body[0]][0]
            repl[s] = re.match(r"[ \t]*", plain[s - 1]).group(0) + "pass"

    # A removed block that sat after a blank line also takes the blank lines behind it,
    # so removing and adding the same block again leaves the file exactly the same.
    for s in [s for s in sorted(drop) if s - 1 not in drop]:
        e = s
        while e + 1 in drop:
            e += 1
        if s > 1 and not plain[s - 2].strip():
            k = e + 1
            while k <= len(lines) and k not in drop and not plain[k - 1].strip():
                drop.add(k)
                k += 1

    out = []
    for i, line in enumerate(lines, 1):
        if i in repl:
            out.append(repl[i] + "\n")
        elif i not in drop:
            out.append(line)
    new_source = "".join(out)

    try:
        new_tree = ast.parse(new_source)
        compile(new_source, "<bot>", "exec")
    except (SyntaxError, ValueError):
        raise RemovalError("Purana code hatane ke baad file compile nahi hoti.")
    removed_defs = {n.name for n in removed if isinstance(n, DEF_KINDS)}
    orig_top = {n.name for n in tree.body if isinstance(n, DEF_KINDS)}
    new_top = {n.name for n in new_tree.body if isinstance(n, DEF_KINDS)}
    if not (orig_top - removed_defs) <= new_top:
        raise RemovalError("Bot ka koi kaam ka function ya class bhi hat gaya.")
    if len(REMOVE_FORBID.findall(new_source)) != len(REMOVE_FORBID.findall(source)):
        raise RemovalError("Bot ka polling ya main code badal gaya.")
    loads, kept_bound, removed_names = set(), set(), set()
    for n in _stmts(new_tree):
        loads |= _own_names(n)
        kept_bound |= _bound(n)
    for n in removed:
        removed_names |= _bound(n)
    if (removed_names - kept_bound) & loads:
        users = sorted(spans[n][0] for n in stmts if n not in removed and not gone(n) and own[n] & removed_names)
        where = f" (purani file ki line {', '.join(str(x) for x in users[:3])})" if users else ""
        raise RemovalError(f"Hataye gaye code ka koi hissa baaki code me use ho raha hai{where}.")
    info = sorted((spans[n][0], spans[n][1], _label(n)) for n in top)
    return new_source, info


def _replace_targets(source, web, pin, want_web, want_pin):
    """Kinds whose old code must be removed first: the option wants it, the code is sure to be
    there, and we can point at it. An unsure match is never removed (it is only reported).
    Empty list means the old path (add only, or skip) applies."""
    targets = []
    for kind, want, status in (("web", want_web, web), ("pinger", want_pin, pin)):
        if want and status == "sure" and _candidate_lines(source, [kind]):
            targets.append(kind)
    return targets


def _marker_lines(text, kind):
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines, 1) if f"# >>> hyv-injected:{kind}" in ln)
    end = next(i for i, ln in enumerate(lines, 1) if f"# <<< hyv-injected:{kind}" in ln)
    return start, end


def _top_picks(source, kinds):
    """Spans of the outermost related statements of these kinds (their inner statements go with them)."""
    lines = source.splitlines()
    tree = ast.parse(source)
    rel = _related(tree, lines, kinds)
    parents = _parents(tree)

    def inside(n):
        p = parents.get(n)
        while p is not None:
            if p in rel:
                return True
            p = parents.get(p)
        return False

    return sorted({_span(n) for n in rel if not inside(n)})


def _where(source, kind, status):
    """Line numbers (up to 5) where the sure or unsure pattern of this kind shows up. Text only."""
    if kind == "web":
        pats, flags = (WEB_SURE if status == "sure" else WEB_UNSURE), re.M
    else:
        pats, flags = (PINGER_SURE if status == "sure" else PINGER_UNSURE), re.I | re.M
    hits = [i for i, ln in enumerate(source.splitlines(), 1) if any(re.search(p, ln, flags) for p in pats)]
    return ("line " + ", ".join(str(i) for i in hits[:5])) if hits else ""


def _skip_phrase(kind, status, where=""):
    name = "Web server" if kind == "web" else "Pinger"
    spot = f" {where} par" if where else ""
    if status == "sure":
        return f"{name} ka code{spot} hai, par use exact hata nahi paya"
    return f"{name} jaisa code{spot} lagta hai (pakka nahi), isliye use nahi chheda"


# ---- Automatic repair pass (built in, runs on every returned file) -------------------------
# Each fix is tried on a copy. It is kept only if the copy compiles and its statement shape (the
# kinds and names of the statements, imports aside) matches the file before it. Otherwise it is
# dropped. Warnings are only reported; nothing is changed for them.

NBSP = " "
SMART_QUOTES = str.maketrans({"“": '"', "”": '"', "„": '"', "‘": "'", "’": "'"})
TOKEN_LITERAL = re.compile(r"""(["'])\d{6,12}:[A-Za-z0-9_-]{30,}\1""")
TOKEN_ENV = 'os.environ.get("BOT_TOKEN", "")'
POLL_CALLS = ("infinity_polling", "polling", "run_polling", "start_polling")
TOKEN_WARN = ("Token file me likha tha, ab bot use BOT_TOKEN environment variable se leta hai. "
              "Purana token ab surakshit nahi hai: BotFather me jaakar naya token banao, "
              "aur Render me BOT_TOKEN me naya token daalo.")
PIP_NAMES = {"telebot": "pyTelegramBotAPI", "telegram": "python-telegram-bot", "flask": "Flask",
             "dotenv": "python-dotenv", "yaml": "PyYAML", "PIL": "Pillow", "bs4": "beautifulsoup4",
             "dateutil": "python-dateutil", "cv2": "opencv-python", "jwt": "PyJWT"}


def _shape(tree):
    return sorted((type(n).__name__, getattr(n, "name", "")) for n in ast.walk(tree)
                  if isinstance(n, ast.stmt) and not isinstance(n, (ast.Import, ast.ImportFrom)))


def _parse_ok(src):
    try:
        compile(src, "<bot>", "exec")
        return True
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False


def _call_name(node):
    f = node.func
    return f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")


def _fix_app_run(text):
    """Flask app.run: host 0.0.0.0 and a PORT from the environment. Only plain one-line calls are changed.
    Returns (new_text, label) or None."""
    tree = ast.parse(text)
    apps = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Name) and n.value.func.id == "Flask"
            for t in n.targets if isinstance(t, ast.Name)}
    if not apps:
        return None
    lines = text.split("\n")
    labels = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "run"
                and isinstance(n.func.value, ast.Name) and n.func.value.id in apps):
            continue
        if n.lineno != n.end_lineno:
            continue
        name, i = n.func.value.id, n.lineno - 1
        ln = lines[i]
        if not n.args and not n.keywords:
            new = re.sub(r"\b%s\.run\(\s*\)" % re.escape(name),
                         f'{name}.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))', ln, count=1)
            if new != ln:
                lines[i] = new
                labels.append("host 0.0.0.0 aur port PORT env se")
            continue
        host = next((k for k in n.keywords if k.arg == "host"), None)
        port = next((k for k in n.keywords if k.arg == "port"), None)
        if host is not None and isinstance(host.value, ast.Constant) and host.value.value != "0.0.0.0":
            new = re.sub(r"\bhost\s*=\s*(['\"])[^'\"]*\1", 'host="0.0.0.0"', ln, count=1)
            if new != ln:
                lines[i] = ln = new
                labels.append("host 0.0.0.0 kar diya")
        if (port is not None and isinstance(port.value, ast.Constant) and isinstance(port.value.value, int)
                and "os.environ" not in ln):
            new = re.sub(r"\bport\s*=\s*(\d+)", r'port=int(os.environ.get("PORT", \1))', ln, count=1)
            if new != ln:
                lines[i] = new
                labels.append("port PORT env se")
    if not labels:
        return None
    return "\n".join(lines), "Flask app.run: " + ", ".join(dict.fromkeys(labels))


def _fix_imports(text):
    """Adds a missing import of os, threading or time (used but never imported). Returns (text, names) or None."""
    tree = ast.parse(text)
    bound, used = set(), set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            bound |= {a.asname or a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.Name):
            (bound if isinstance(n.ctx, ast.Store) else used).add(n.id)
        elif isinstance(n, DEF_KINDS):
            bound.add(n.name)
        elif isinstance(n, ast.arg):
            bound.add(n.arg)
    missing = [m for m in ("os", "threading", "time") if m in used and m not in bound]
    if not missing:
        return None
    imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    first = tree.body[0] if tree.body else None
    if imports:
        pos = imports[-1].end_lineno
    elif isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) \
            and isinstance(first.value.value, str):
        pos = first.end_lineno
    else:
        pos = 0
    lines = text.split("\n")
    lines[pos:pos] = [f"import {m}" for m in missing]
    return "\n".join(lines), missing


def _untab(ln):
    lead = re.match(r"[ \t]*", ln).group(0)
    return lead.expandtabs(4) + ln[len(lead):]


def auto_repair(source):
    """Returns (new_source, fixes, warnings). fixes: Hinglish lines for what was changed. warnings: what
    only needs a look (nothing is changed for those). A fix that breaks the file is dropped."""
    fixes, warns = [], []
    try:
        shape = _shape(ast.parse(source))
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        shape = None
    text = source

    def apply(label, cand):
        nonlocal text, shape
        if cand is None or cand == text or not _parse_ok(cand):
            return False
        new_shape = _shape(ast.parse(cand))
        if shape is not None and new_shape != shape:
            return False
        text, shape = cand, new_shape
        fixes.append(label)
        return True

    if "﻿" in text:
        apply("Ek ajeeb nishaan (BOM) hata diya", text.replace("﻿", ""))
    if "\r" in text:
        apply("Windows ki line ending (CRLF) normal kar di", text.replace("\r\n", "\n").replace("\r", "\n"))
    if re.search(r"(?m)^[ \t]*\t", text):
        apply("Indent me tab ko spaces se badal diya", "\n".join(_untab(ln) for ln in text.split("\n")))
    if NBSP in text and not _parse_ok(text):
        apply("Code me ajeeb space (NBSP) normal space se badal di", text.replace(NBSP, " "))
    if not _parse_ok(text) and any(ord(ch) in SMART_QUOTES for ch in text):
        apply("Code me smart quotes normal quotes se badal diye", text.translate(SMART_QUOTES))
    if TOKEN_LITERAL.search(text):
        apply("Token ko file se hata kar BOT_TOKEN environment variable se lene laga", TOKEN_LITERAL.sub(TOKEN_ENV, text))
        warns.append(TOKEN_WARN)
    res = _fix_app_run(text) if _parse_ok(text) else None
    if res is not None:
        apply(res[1], res[0])
    res = _fix_imports(text) if _parse_ok(text) else None
    if res is not None:
        apply("Chhuta hua import jod diya: " + ", ".join(res[1]), res[0])

    if not _parse_ok(text):
        return text, fixes, warns
    tree = ast.parse(text)
    polls = sorted(n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call) and _call_name(n) in POLL_CALLS)
    if len(polls) > 1:
        warns.append("Bot chalane wali line (polling) " + ", ".join(f"line {x}" for x in polls)
                     + " par hai. Ek hi rakho. Maine kuch nahi hataya.")
    hooks = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            name = n.id
        elif isinstance(n, ast.Attribute):
            name = n.attr
        elif isinstance(n, ast.keyword) and n.arg:
            name = n.arg
        elif isinstance(n, DEF_KINDS):
            name = n.name
        else:
            continue
        if "webhook" in name.lower():
            hooks.add(n.lineno)
    if hooks and polls:
        warns.append(f"Webhook (line {min(hooks)}) aur polling dono lage hain. Telegram me ek hi chalta hai. "
                     "Maine kuch nahi badla, ise check karna.")
    slow = []
    for fn in ast.walk(tree):
        if isinstance(fn, ast.AsyncFunctionDef):
            for c in ast.walk(fn):
                if isinstance(c, ast.Call) and ((isinstance(c.func, ast.Attribute) and c.func.attr == "sleep"
                                                 and isinstance(c.func.value, ast.Name) and c.func.value.id == "time")
                                                or (isinstance(c.func, ast.Name) and c.func.id == "sleep")):
                    slow.append(f"{fn.name} me line {c.lineno}")
    if slow:
        warns.append("Async function me time.sleep hai (" + "; ".join(slow[:3]) + "). Ye bot ko ruka deta hai, "
                     "asyncio.sleep behtar hai. Maine nahi badla.")
    return text, fixes, warns


def scan_requirements(source):
    """pip package names for the third-party imports of this file. No versions."""
    tree = ast.parse(source)
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            mods.add(n.module.split(".")[0])
    std = getattr(sys, "stdlib_module_names", frozenset())
    return sorted({PIP_NAMES.get(m, m) for m in mods if m not in std and m != "__future__"})


def _repair_lines(fixes, warns):
    out = []
    if fixes:
        out.append("Apne aap ye theek kiya:")
        out.extend(f"- {f}" for f in fixes)
    if warns:
        out.append("Dhyan do (maine ye nahi badla):")
        out.extend(f"- {w}" for w in warns)
    return out


def _finish(new_source, text):
    """Runs the repair pass on a finished file and adds its report to the text."""
    fixed, fixes, warns = auto_repair(new_source)
    extra = _repair_lines(fixes, warns)
    return fixed, "\n".join([text, *extra]) if extra else text


def plan_backup_checked(source, admin_id):
    new_source, text = plan_backup(source, admin_id)
    if new_source is None:
        return None, text
    return _finish(new_source, text)


def plan(source, choice, admin_id=None):
    """Returns (new_source, reply_text). new_source is None when the file is left as it is.
    The old web server or pinger code of the chosen kinds is removed first (only if it is found exactly),
    then the new block is added, then the repair pass runs on the result."""
    if choice == BTN_DB:
        return plan_backup_checked(source, admin_id)
    web = web_server_status(source)
    pin = pinger_status(source)
    want_web = choice in (BTN_WEB, BTN_BOTH)
    want_pin = choice in (BTN_PINGER, BTN_BOTH)
    targets = _replace_targets(source, web, pin, want_web, want_pin)
    add_web = want_web and (web == "none" or "web" in targets)
    add_pin = want_pin and (pin == "none" or "pinger" in targets)
    skipped = []
    if want_web and not add_web:
        skipped.append(_skip_phrase("web", web, _where(source, "web", web)))
    if want_pin and not add_pin:
        skipped.append(_skip_phrase("pinger", pin, _where(source, "pinger", pin)))
    if not add_web and not add_pin:
        return None, ". ".join(skipped) + ". Isliye maine kuch add nahi kiya. File waisi hi rahegi."

    lines, base = [], source
    if targets:
        try:
            base, removed = _remove_code(source, _top_picks(source, targets), targets)
        except RemovalError as e:
            return None, f"{e} Isliye kuch nahi badla. File waisi hi rahegi."
        if removed:
            lines.append("Purana code hata diya (purani file ki line):")
            for s, e, label in removed:
                lines.append(f"- line {s}" if s == e else f"- line {s}-{e}: {label}")
    try:
        out = inject(base, add_web, add_pin)
    except (ValueError, SyntaxError):
        return None, "Naya code jodne ki jaanch fail hui. Isliye kuch nahi badla. File waisi hi rahegi."
    for kind in ("web", "pinger"):
        if out.count(f"# >>> hyv-injected:{kind}") > 1 or out.count(f"# <<< hyv-injected:{kind}") > 1:
            return None, "Naya code do baar lag raha tha. Isliye kuch nahi badla. File waisi hi rahegi."

    if add_web and add_pin:
        head = "Web server aur pinger dono naye daal diye."
    elif add_web:
        head = "Web server naya daal diya."
    else:
        head = "Pinger naya daal diya (default 5 minute)."
    lines.insert(0, head)
    if add_web:
        s, e = _marker_lines(out, "web")
        lines.append(f"Naya web server: line {s}-{e}")
    if add_pin:
        s, e = _marker_lines(out, "pinger")
        lines.append(f"Naya pinger: line {s}-{e} (default 5 minute)")
        if web_server_status(out) == "none":
            lines.append("Pinger tabhi kaam karega jab bot par web server bhi chale.")
    for phrase in skipped:
        lines.append(f"{phrase}, isliye use chhoda.")
    lines.append("Render par deploy karne se pehle ek baar test karna.")
    return _finish(out, "\n".join(lines))


def check_text(source, choice, admin_id=None):
    """Dry run for /check: the same plan, nothing is saved. Returns the reply text."""
    new_source, reply = plan(source, choice, admin_id)
    if new_source is None:
        return reply
    pkgs = scan_requirements(new_source)
    return reply + ("\nPackages (requirements.txt): " + ", ".join(pkgs) if pkgs else "")


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
    """Reply button with its colour. A telebot version without the style field gets a plain button."""
    try:
        return types.KeyboardButton(label, style=BUTTON_STYLE[label])
    except TypeError:
        return types.KeyboardButton(label)


def option_keyboard():
    """Reply keyboard: the 4 options, one per row (as before), then a row with the 2 info buttons.
    Each button has a fill colour (style) from BUTTON_STYLE."""
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(_button(BTN_WEB))
    kb.row(_button(BTN_PINGER))
    kb.row(_button(BTN_BOTH))
    kb.row(_button(BTN_DB))
    kb.row(_button(BTN_STATUS), _button(BTN_LAST))
    return kb


def too_big_text():
    return f"Ye file {size_limit_text()} se badi hai. Chhoti .py file bhejo."


def canonical_label(text):
    """Maps an old (no emoji) button label to the current one. Other text is returned unchanged."""
    return LEGACY_LABELS.get(text, text)


def warn(body):
    """Error notice: warning emoji, a bold word, then the body text (body is kept as it is)."""
    return f"⚠️ {fx_bold('Galti')}: {body}"


# ---- Animation and effects -------------------------------------------------------------

STAGE_LABELS = ("File padh raha hoon", "Web server aur pinger check kar raha hoon",
                "Naya file bana raha hoon")
BAR_LEN = 5


def bar(done, total=BAR_LEN):
    done = max(0, min(total, done))
    return "▰" * done + "▱" * (total - done)


class Progress:
    """A short progress message, edited in steps while the bot works, plus a typing or upload action.
    Every Telegram call here is wrapped: if one fails (for example "message is not modified"), the
    real work still goes on. The message is deleted when the work is done."""

    def __init__(self, bot, chat_id, pause=0.5):
        self.bot = bot
        self.chat_id = chat_id
        self.pause = pause
        self.msg_id = None

    def _try(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            return None

    def _wait(self):
        if self.pause > 0:
            time.sleep(self.pause)

    def _text(self, i):
        return f"{bar(round(BAR_LEN * (i + 1) / len(STAGE_LABELS)))}\n{STAGE_LABELS[i]}..."

    def action(self, kind="typing"):
        self._try(self.bot.send_chat_action, self.chat_id, kind)

    def start(self):
        self.action("typing")
        sent = self._try(self.bot.send_message, self.chat_id, self._text(0))
        self.msg_id = getattr(sent, "message_id", None)
        self._wait()

    def step(self, i):
        if self.msg_id is not None:
            self._try(self.bot.edit_message_text, self._text(i), chat_id=self.chat_id, message_id=self.msg_id)
        self._wait()

    def finish(self):
        if self.msg_id is not None:
            self._try(self.bot.delete_message, self.chat_id, self.msg_id)
            self.msg_id = None


def send_document_file(bot, chat_id, path, visible_name, caption, private=False):
    """Sends a saved file. In a private chat it tries a message effect first (🎉, then 🔥, then 👍).
    If Telegram refuses the effect, the file is sent normally, with no note about it.
    Used only for the final result of an option tap."""
    def attempt(**extra):
        with open(path, "rb") as f:
            return bot.send_document(chat_id, f, visible_file_name=visible_name, caption=caption, **extra)

    if private:
        for effect in EFFECT_IDS:
            try:
                return attempt(message_effect_id=effect)
            except ApiException:
                continue
    return attempt()


class Implementer:
    def __init__(self, bot, owner_id, data_dir, pause=0.5):
        self.bot = bot
        self.owner = owner_id
        self.data_dir = data_dir
        self.pause = pause  # seconds between animation steps (tests use 0)
        self.pending = {}  # chat_id -> (path of the saved original, display name)
        self.lock = threading.Lock()

    def is_owner(self, user_id):
        return self.owner is not None and user_id == self.owner

    def refuse(self, chat_id):
        self.bot.send_message(chat_id, f"🔒 {fx_bold('Ye bot private hai.')}")

    def user_dir(self, chat_id, sub):
        return os.path.join(self.data_dir, str(int(chat_id)), sub)

    def on_start(self, message):
        chat_id = message.chat.id
        uid = message.from_user.id
        if self.owner is None:
            # Same as the pinger bot: shows the user's own id, so the owner can set OWNER_ID.
            self.bot.send_message(chat_id, f"🆔 {fx_bold('Aapka Telegram ID')}: {uid}\n\n"
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
            return self.bot.send_message(chat_id, warn("Sirf .py file bhejo. Dusri file ko main nahi chhedunga."))
        if (doc.file_size or 0) > MAX_BYTES:
            return self.bot.send_message(chat_id, warn(too_big_text()))
        prog = Progress(self.bot, chat_id, self.pause)
        prog.start()
        try:
            try:
                info = self.bot.get_file(doc.file_id)
                data = self.bot.download_file(info.file_path)
            except Exception:
                return self.bot.send_message(chat_id, warn("File download nahi ho payi. Dobara bhejo."))
            if isinstance(data, bytes) and len(data) > MAX_BYTES:
                return self.bot.send_message(chat_id, warn(too_big_text()))
            if not isinstance(data, bytes):
                return self.bot.send_message(chat_id, warn("File sahi se nahi aayi. Dobara bhejo."))
            source, err = read_source(data)
            if err:
                return self.bot.send_message(chat_id, warn(err))
            prog.step(1)
            name = safe_name(raw_name)
            path = save_unique(self.user_dir(chat_id, "uploads"), name, data)
            with self.lock:
                self.pending[chat_id] = (path, name)
            self.bot.send_message(chat_id, card(f"📄 {fx_bold('File mil gayi')}: {name}", [status_block(source)],
                                                "Ab neeche se option chuno."),
                                  reply_markup=option_keyboard())
        finally:
            prog.finish()

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
            return self.bot.send_message(chat_id, warn("Wo file ab nahi mil rahi. Dobara bhejo."))
        source, err = read_source(data)
        if err:
            return self.bot.send_message(chat_id, warn(err))
        self.bot.send_message(chat_id, card(f"📊 {fx_bold('Status')}: {name}", [status_block(source)],
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
                                   caption=card(f"🔁 {fx_bold('Pichli updated file')}", [], RESULT_NOTE))

    def on_check(self, message, raw):
        """/check or "pehle check <option>": shows what the change would do. Nothing is saved or sent as a file."""
        chat_id = message.chat.id
        if not self.is_owner(message.from_user.id):
            return self.refuse(chat_id)
        low = raw.lower()
        rest = raw[len("/check"):] if low.startswith("/check") else raw[len("pehle check"):]
        word = rest.strip().lower()
        choice = CHECK_WORDS.get(word) or (canonical_label(rest.strip()) if canonical_label(rest.strip()) in OPTIONS else None)
        if choice is None:
            return self.bot.send_message(chat_id, card(f"🔍 {fx_bold('Pehle check')}", [
                "/check web  ➜ web server", "/check pinger  ➜ pinger", "/check both  ➜ dono",
                "/check db  ➜ Database Export/Import"], "Ye sirf dikhata hai, file nahi banati."))
        with self.lock:
            pending = self.pending.get(chat_id)
        if pending is None:
            return self.bot.send_message(chat_id, "📭 Pehle apni bot ki .py file bhejo.")
        path, name = pending
        try:
            with open(path, "rb") as f:
                source, err = read_source(f.read())
        except OSError:
            return self.bot.send_message(chat_id, warn("Wo file ab nahi mil rahi. Dobara bhejo."))
        if err:
            return self.bot.send_message(chat_id, warn(err))
        try:
            reply = check_text(source, choice, message.from_user.id)
        except Exception:
            return self.bot.send_message(chat_id, warn("Kuch gadbad ho gayi, file nahi badli."))
        self.bot.send_message(chat_id, card(f"🔍 {fx_bold('Pehle check')}: {name}", [reply],
                                            "Ye sirf check tha, file nahi bani. Asli file ke liye option button dabao."),
                              reply_markup=option_keyboard())

    def on_text(self, message):
        chat_id = message.chat.id
        if not self.is_owner(message.from_user.id):
            return self.refuse(chat_id)
        raw = (message.text or "").strip()
        if raw.lower().startswith("/check") or raw.lower().startswith("pehle check"):
            return self.on_check(message, raw)
        # Old labels (no emoji) map to the current ones, so both work.
        text = canonical_label(raw)
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
        private = message.chat.type == "private"
        prog = Progress(self.bot, chat_id, self.pause)
        prog.start()
        try:
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                return self.bot.send_message(chat_id, warn("Wo file ab nahi mil rahi. Dobara bhejo."))
            source, err = read_source(data)
            if err:
                return self.bot.send_message(chat_id, warn(err))
            prog.step(1)
            try:
                new_source, reply = plan(source, text, message.from_user.id)
            except Exception:
                return self.bot.send_message(chat_id, warn("Kuch gadbad ho gayi, file nahi badli. "
                                                           "Dobara try karo."))
            if new_source is None:
                return self.bot.send_message(chat_id, f"ℹ️ {fx_bold('File nahi badli.')}\n\n{reply}")
            prog.step(2)
            out_name = result_name(name)
            out_path = save_unique(self.user_dir(chat_id, "results"), out_name, new_source.encode("utf-8"))
            pkgs = scan_requirements(new_source)
            prog.action("upload_document")
            send_document_file(self.bot, chat_id, out_path, out_name,
                               card(f"✅ {fx_bold('Naya file taiyaar hai')}: {out_name}", [reply], RESULT_NOTE),
                               private)
            if pkgs:
                # Sent from memory only, so it never lands in the results folder (/last stays the bot file).
                self.bot.send_document(chat_id, io.BytesIO(("\n".join(pkgs) + "\n").encode("utf-8")),
                                       visible_file_name="requirements.txt",
                                       caption=card(f"📦 {fx_bold('requirements.txt')}", [", ".join(pkgs)],
                                                    "Ye packages Render par chahiye. Version Render ke hisaab se chalega."))
        finally:
            prog.finish()


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

    @bot.message_handler(commands=["check"])
    def _hyv_check(message):
        impl.on_check(message, message.text or "/check")

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
