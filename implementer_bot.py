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
  AI_API_KEY      optional; AI key used only when the Database Export/Import option needs AI
  AI_PROVIDER     optional; openai, openrouter, groq, gemini or anthropic (default: from the key prefix)
  AI_MODEL        optional; model name, overrides the model saved with /aimodel
  AI_BASE_URL     optional; API base address for an OpenAI-compatible provider
  AI_REFERER      optional; sent as HTTP-Referer to OpenRouter (not sent when unset)
  HYV_ADMIN_IDS   optional; extra Telegram ids (comma separated) allowed to use Database Export/Import
                  in a bot that gets the feature added
"""

import ast
import difflib
import json
import logging
import os
import re
import sys
import time
import threading

import requests
import telebot
from telebot import types
from telebot.apihelper import ApiException

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_BYTES = 300 * 1024

BTN_WEB = "🌐 Web server add karo"
BTN_PINGER = "📡 Pinger add karo"
BTN_BOTH = "🧩 Dono add karo"
BTN_DB = "🗄 Database Export/Import"   # owner/admin only backup feature
OPTIONS = (BTN_WEB, BTN_PINGER, BTN_BOTH, BTN_DB)
BTN_STATUS = "📊 Status dekho"   # same as the typed /status
BTN_LAST = "🔁 Pichli file lo"   # same as the typed /last
BTN_AI = "🤖 AI key set karo"   # owner only: saves the AI key (needed only by the backup option)
MENU_BUTTONS = OPTIONS + (BTN_STATUS, BTN_LAST, BTN_AI)
INFO_BUTTONS = (BTN_STATUS, BTN_LAST)
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
                BTN_STATUS: "success", BTN_LAST: "success", BTN_AI: "primary"}

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
           "🔒 Aapki original file main nahi chhedunga.\n\n"
           f"🧭 {fx_script('Options')}\n"
           "File bhejne ke baad neeche ke option chuno:\n"
           f"{BTN_WEB}\n"
           f"{BTN_PINGER}\n"
           f"{BTN_BOTH}\n"
           f"{BTN_DB}\n\n"
           f"⚡ {fx_small('Quick buttons')}\n"
           "Kabhi bhi neeche ke buttons se:\n"
           f"{BTN_STATUS}: file me kya pehle se hai\n"
           f"{BTN_LAST}: pichli updated file dobara")
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


def _hyv_bk_register(bot):
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
    bot.message_handler(func=lambda m: admin_private(m) and m.text in ("/backup", "/hyvbackup"))(show_menu)
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


def _hyv_bk_register(app):
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
        if text in ("/backup", "/hyvbackup"):
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
    rest = [n for n in new_tree.body if not (first <= n.lineno <= last)]
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
            "Extra admin ke liye Render me HYV_ADMIN_IDS env variable me comma se ID daal sakte ho. "
            "Naya file Render par deploy karke bot restart karna.")
    return f"{text} {extra}".strip()


def plan_backup(source, admin_id=None, ai_cfg=None, http=None):
    """Returns (new_source, reply_text) for the Database Export/Import option.
    The telebot and python-telegram-bot templates come first. The AI is used only when the library is
    not supported or the bot object is not found, and an AI key is set. admin_id is the Telegram id of
    the member who sent the file; it is written into the injected code."""
    if BACKUP_MARK in source:
        return None, "Database Export/Import pehle se is file mein hai. Isliye maine kuch add nahi kiya."
    if not isinstance(admin_id, int) or isinstance(admin_id, bool) or admin_id <= 0:
        return None, ("File bhejne wale ka Telegram ID nahi mila, isliye Database Export/Import nahi jod paya. "
                      "File waisi hi rahegi.")
    lib = backup_library(source)
    if lib is not None:
        try:
            return inject_backup(source, lib, admin_id), backup_done_text(admin_id)
        except ValueError:
            reason = "Is file mein bot ka main object nahi mila"
    else:
        reason = ("Ye bot library abhi support nahi hai (templates sirf pyTelegramBotAPI "
                  "aur python-telegram-bot ke liye hain)")
    if ai_cfg is None:
        return None, (f"{reason}. AI key bhi set nahi hai. '{BTN_AI}' se key daal kar dobara try karo. "
                      "File waisi hi rahegi.")
    new_source, err = ai_inject_backup(source, admin_id, ai_cfg, http, lib)
    if new_source is None:
        return None, f"{err} File waisi hi rahegi."
    return new_source, backup_done_text(
        admin_id, "AI ne ye code banaya hai, isliye Render par deploy karke pehle ek baar test karna.")


# ---- AI (optional). The owner adds an API key with the AI button. The key is never printed,
# logged or echoed back. It is kept in a 0600 file in the data folder, or read from AI_API_KEY.

AI_PROVIDERS = ("openai", "gemini", "anthropic", "openrouter", "groq")
AI_NAME = {"openai": "OpenAI", "gemini": "Google Gemini", "anthropic": "Anthropic Claude",
           "openrouter": "OpenRouter", "groq": "Groq"}
AI_DEFAULT_MODEL = {"openai": "gpt-4o-mini", "gemini": "gemini-2.0-flash", "anthropic": "claude-3-5-haiku-latest",
                    "openrouter": "anthropic/claude-sonnet-5.5", "groq": "llama-3.3-70b-versatile"}
# OpenAI-style providers (chat/completions with a Bearer key). Only the address differs.
AI_OPENAI_STYLE = {"openai": "https://api.openai.com/v1", "openrouter": "https://openrouter.ai/api/v1",
                   "groq": "https://api.groq.com/openai/v1"}
AI_DEFAULT_BASE = AI_OPENAI_STYLE["openai"]
AI_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,119}")
AI_CONFIG_FILE = "ai_config.json"
AI_TIMEOUT = 60           # seconds per call
AI_TRIES = 3              # first try plus 2 retries
AI_MAX_TOKENS = 8192      # answer size asked from the AI
AI_SRC_MAX = 30 * 1024    # the bot file sent to the AI must be this small
AI_OUT_MAX = 400 * 1024   # the AI answer must be this small
AI_KEY_PROMPT = "Ab apni API key bhejo. Ye message main turant delete kar dunga, aur key kisi ko nahi dikhaunga."
# Hosts the code this bot injects may call. The AI provider hosts (AI_OPENAI_STYLE, Gemini, Anthropic)
# are NOT here: they are used only by this bot's own AI calls, never by a member's injected code.
AI_ALLOWED_HOSTS = ("api.telegram.org", "localhost", "127.0.0.1")
# Token and key shapes. sk-or-v1-... (OpenRouter) is listed on its own, the generic sk- rule covers it too.
AI_SECRET = re.compile(r"\d{8,12}:[A-Za-z0-9_-]{30,}|sk-or-[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9_-]{16,}"
                       r"|gsk_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}")
AI_BAD_CALLS = re.compile(r"\b(os\.system|subprocess|eval\s*\(|exec\s*\(|__import__|pickle\.loads)")
AI_BAD_IMPORTS = re.compile(r"^\s*(import|from)\s+(socket|urllib|http\.client|httpx|aiohttp|requests)\b", re.M)
AI_URL_HOST = re.compile(r"https?://([^/\s\"'):]+)", re.I)
AI_SYSTEM = ("You edit one Python Telegram bot file for its owner. Reply with the complete updated file inside "
             "one ```python block and nothing else.")


class AIError(Exception):
    """A safe message for the owner. Never contains the key or the raw provider reply."""

    def __init__(self, text, retry=False):
        super().__init__(text)
        self.retry = retry


def mask_key(key):
    """Only the first 4 and the last 4 characters."""
    if not key or len(key) < 12:
        return "****"
    return f"{key[:4]}...{key[-4:]}"


def detect_provider(key):
    """Provider from the key prefix. The order matters: sk-ant- and sk-or- are checked before the
    generic sk- (OpenAI) rule, otherwise an OpenRouter key would be sent to OpenAI."""
    if key.startswith("sk-ant-"):
        return "anthropic"
    if key.startswith("sk-or-"):
        return "openrouter"
    if key.startswith("gsk_"):
        return "groq"
    if key.startswith("AIza"):
        return "gemini"
    if key.startswith("sk-"):
        return "openai"
    return None


def redact_secrets(text):
    """Any token or key shape in a text is replaced before the text is shown or logged."""
    return AI_SECRET.sub("<hidden>", str(text))


def _read_saved_ai(data_dir):
    try:
        with open(os.path.join(data_dir, AI_CONFIG_FILE), "r", encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        return {}


def load_ai_config(data_dir):
    """The AI settings in use, or None. Env vars (AI_API_KEY, AI_PROVIDER, AI_MODEL, AI_BASE_URL)
    win over the saved file. Returns a dict with provider, key, model and base_url."""
    saved = _read_saved_ai(data_dir)
    env_key = os.environ.get("AI_API_KEY", "").strip()
    key = env_key or str(saved.get("key") or "").strip()
    if not key:
        return None
    env_provider = os.environ.get("AI_PROVIDER", "").strip().lower()
    provider = env_provider if env_provider in AI_PROVIDERS else ""
    if not provider and not env_key and saved.get("provider") in AI_PROVIDERS:
        provider = saved["provider"]
    if not provider:
        provider = detect_provider(key) or ""
    if provider not in AI_PROVIDERS:
        return None
    model = os.environ.get("AI_MODEL", "").strip() or str(saved.get("model") or "") or AI_DEFAULT_MODEL[provider]
    base = os.environ.get("AI_BASE_URL", "").strip() or saved.get("base_url") or None
    return {"provider": provider, "key": key, "model": model, "base_url": base}


def _write_ai_file(data_dir, saved):
    """Written with mode 0600 (only the bot's own user can read it), to a temp file, then renamed."""
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, AI_CONFIG_FILE)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(saved, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def env_ai_model():
    """True when the AI_MODEL env variable is set (it wins over the model saved on disk)."""
    return bool(os.environ.get("AI_MODEL", "").strip())


def save_ai_config(data_dir, cfg):
    _write_ai_file(data_dir, {"provider": cfg["provider"], "key": cfg["key"], "model": cfg.get("model"),
                              "base_url": cfg.get("base_url")})


def save_ai_model(data_dir, model):
    """Changes only the saved model (None means the provider's default). The saved key, if any, is
    kept as it is. An env key is never written to disk by this."""
    saved = _read_saved_ai(data_dir)
    saved["model"] = model
    _write_ai_file(data_dir, saved)


def delete_ai_config(data_dir):
    """True when a saved key file was removed."""
    try:
        os.remove(os.path.join(data_dir, AI_CONFIG_FILE))
        return True
    except OSError:
        return False


def ai_status_line(data_dir):
    cfg = load_ai_config(data_dir)
    if cfg is None:
        return "🤖 AI: set nahi hai"
    where = "Render env AI_API_KEY" if os.environ.get("AI_API_KEY", "").strip() else "bot ki disk"
    return (f"🤖 AI: {AI_NAME[cfg['provider']]}, model {cfg.get('model') or AI_DEFAULT_MODEL[cfg['provider']]}, "
            f"key {mask_key(cfg['key'])} ({where})")


def ai_error_text(provider, code):
    """Hinglish reason for a non-200 answer. Never contains the key or the raw provider reply."""
    name = AI_NAME[provider]
    if code == 401:
        return f"Key galat ya expire/delete ho gayi hai, {name} ki site se nayi key lo."
    if code == 402:
        return f"{name} par credits khatam ho gaye hain. Balance top up karo, phir try karo."
    if code == 403:
        return f"{name} ne is key ko ye kaam karne ki permission nahi di (access ya region ki dikkat)."
    if code == 404:
        return f"Model naam galat hai, ya {name} par ye model available nahi hai. Model check karke dobara try karo."
    if code == 429:
        return f"{name} par limit khatam ho gayi ya bahut zyada requests hain. Thodi der baad ya credits check karke try karo."
    if code == 400:
        return f"{name} ne request sahi nahi maani (model ya settings galat ho sakti hai)."
    if code >= 500:
        return f"{name} ki service abhi down ya busy hai. Thodi der baad try karo."
    return f"{name} ne jawab nahi diya (status {code})."


def ai_request(cfg, system, user, http, max_tokens=AI_MAX_TOKENS, timeout=AI_TIMEOUT):
    """One call to the chosen provider. Returns the reply text. Raises AIError (safe text only)."""
    provider, key = cfg["provider"], cfg["key"]
    model = cfg.get("model") or AI_DEFAULT_MODEL[provider]
    if provider in AI_OPENAI_STYLE:
        base = (cfg.get("base_url") or AI_OPENAI_STYLE[provider]).rstrip("/")
        url = base + "/chat/completions"
        headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
        if provider == "openrouter":
            headers["X-Title"] = "Bot Implementer"
            referer = os.environ.get("AI_REFERER", "").strip()
            if referer.lower().startswith("https://"):
                headers["HTTP-Referer"] = referer
        body = {"model": model, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    elif provider == "gemini":
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"maxOutputTokens": max_tokens}}
    else:
        url = "https://api.anthropic.com/v1/messages"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
        body = {"model": model, "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": user}]}
    name = AI_NAME[provider]
    try:
        resp = http.post(url, headers=headers, json=body, timeout=timeout)
    except Exception as e:
        # The exception text can hold the address or headers, so only its kind is used.
        if "timeout" in type(e).__name__.lower():
            raise AIError(f"{name} ne time out ho gaya (jawab late aaya). Thodi der baad try karo.", retry=True)
        raise AIError(f"{name} tak network dikkat hai, connection nahi ho paya. Net check karke try karo.", retry=True)
    code = getattr(resp, "status_code", 0)
    if code != 200:
        raise AIError(ai_error_text(provider, code), retry=(code == 429 or code >= 500))
    try:
        data = resp.json()
        if provider in AI_OPENAI_STYLE:
            text = data["choices"][0]["message"]["content"]
        elif provider == "gemini":
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
        else:
            text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise AIError("AI ka jawab samajh nahi aaya.", retry=True)
    if not isinstance(text, str) or not text.strip():
        raise AIError("AI ne khaali jawab diya.", retry=True)
    return text


def ai_check_key(cfg, http):
    """One tiny test call (with the model in cfg). Returns None when the key and model work, else a safe reason."""
    try:
        ai_request(cfg, "Reply with the single word OK.", "Say OK.", http, max_tokens=64, timeout=30)
    except AIError as e:
        return redact_secrets(e)
    return None


def extract_code(reply):
    m = re.search(r"```[ \t]*(?:python|py)?[ \t]*\n(.*?)```", reply, re.S | re.I)
    return (m.group(1) if m else reply).strip("\n") + "\n"


def check_ai_output(original, new, admin_id):
    """None when the AI's file is acceptable, else a Hinglish reason. It must compile, carry the markers
    and the admin id, keep every original function and class, change only a few original lines, and add
    no secret, no unknown web address and no dangerous call."""
    if not new.strip():
        return "AI ne khaali file di."
    if len(new.encode("utf-8")) > AI_OUT_MAX:
        return "AI ki file bahut badi hai."
    try:
        tree = ast.parse(new)
        compile(new, "<ai-output>", "exec")
    except (SyntaxError, ValueError):
        return "AI ka code compile nahi hua (syntax galat hai)."
    if new.count("# >>> hyv-injected:backup") != 1 or new.count("# <<< hyv-injected:backup") != 1:
        return "AI ne backup ka start aur end marker sahi nahi lagaya."
    if "_hyv_bk_" not in new:
        return "AI ne backup ka code nahi likha."
    admin_ok = any(
        isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_HYV_BK_ADMINS" for t in n.targets)
        and any(isinstance(c, ast.Constant) and c.value == admin_id for c in ast.walk(n.value))
        for n in ast.walk(tree))
    if not admin_ok:
        return "AI ne admin ID sahi jagah nahi lagayi."
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    old_names = {n.name for n in ast.parse(original).body if isinstance(n, kinds)}
    new_names = {n.name for n in tree.body if isinstance(n, kinds)}
    gone = sorted(old_names - new_names)
    if gone:
        return f"AI ne bot ka purana code hata diya ({', '.join(gone[:3])})."
    old_lines, new_lines = original.splitlines(), new.splitlines()
    removed, added = 0, []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            for line in old_lines[i1:i2]:
                if re.match(r"\s*(async\s+def|def|class)\s", line):
                    return "AI ne purane function ya class ki line badal di."
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added.extend(new_lines[j1:j2])
    if removed > 6:
        return "AI ne purane code me bahut badlaav kiya."
    added_text = "\n".join(added)
    if AI_SECRET.search(added_text):
        return "AI ne code me token ya key likh di."
    if AI_BAD_CALLS.search(added_text) or AI_BAD_IMPORTS.search(added_text):
        return "AI ne khatarnak call (system, subprocess ya network library) likh diya."
    if any(host.lower() not in AI_ALLOWED_HOSTS for host in AI_URL_HOST.findall(added_text)):
        return "AI ne ajeeb web address use kiya."
    return None


def ai_prompt(source, admin_id, lib, reason=None):
    ref = backup_block(lib or "telebot", admin_id)
    lib_name = {"telebot": "pyTelegramBotAPI (telebot)", "ptb": "python-telegram-bot v20+"}.get(
        lib, "unknown or not supported by the templates")
    rules = [
        "Add a Database Export/Import feature to the bot file below, using the bot's own library.",
        f"The bot library: {lib_name}. Use that library's own handler, keyboard and reply calls. Do not switch libraries.",
        f"Put the Telegram id {admin_id} (the member who sent the file) in the line `_HYV_BK_ADMINS = {{{admin_id}}}`. "
        "Only that id and the env variable HYV_ADMIN_IDS (comma separated) make an admin. Do not look for OWNER_ID "
        "or any other admin variable.",
        "Keep ALL original code. Only add new code, plus at most a few lines of hook edits (for example one line "
        "that registers the new handlers after the bot object is created).",
        "Start the new block with the line `# >>> hyv-injected:backup (added by the implementer bot)` and end it "
        "with `# <<< hyv-injected:backup`. Use exactly one of each.",
        "Every new name must start with `_hyv_bk_` or `_HYV_BK_`.",
        "Admin only: the button `🗄 Database Export/Import` shows only to admins and opens a menu with "
        "`📤 Export`, `📥 Import` and `🔙 Back`.",
        "Export: a zip of the bot folder's data files (.db .sqlite .sqlite3 .json .csv .txt .pkl .yaml .yml .xml) "
        "with manifest.json holding path, size and sha256 for each file. Copy databases with the sqlite3 backup API. "
        "Never include .env, token or secret files, and never the bot's own .py file.",
        "Import: the admin sends a zip. Check the manifest and every sha256. Refuse absolute paths and '..' (zip-slip). "
        "Keep a safety copy of the current files in a hyv_safety folder. Ask for confirmation before replacing "
        "anything. Tell the admin to restart the bot.",
        "Never change the bot's normal behaviour. Wrap the new handlers so an error is caught and reported as a "
        "short message.",
        "No hard-coded tokens or keys. No web addresses except the bot library's own Telegram calls. No eval, exec, "
        "subprocess, os.system, socket or extra HTTP libraries.",
        "Output: the full updated file in one ```python block, and nothing else.",
    ]
    text = "Task:\n" + "\n".join(f"- {r}" for r in rules)
    text += ("\n\nWorking reference block (telebot style). Use its ideas and adapt the calls to the bot's library:\n"
             "```python\n" + ref + "\n```")
    text += "\n\nThe bot file:\n```python\n" + source + "\n```"
    if reason:
        text += f"\n\nYour previous answer was rejected because: {reason} Fix that, keep the rules, and send the full file again."
    return text


def ai_inject_backup(source, admin_id, cfg, http=None, lib=None):
    """Asks the AI for the injection. Every answer is checked; a rejected answer goes back with the reason,
    up to AI_TRIES times. Returns (new_source, None) or (None, reason). Never raises."""
    if len(source.encode("utf-8")) > AI_SRC_MAX:
        return None, "Ye file AI ke liye bahut badi hai (limit 30 KB)."
    http = http if http is not None else requests
    reason = None
    for _ in range(AI_TRIES):
        try:
            reply = ai_request(cfg, AI_SYSTEM, ai_prompt(source, admin_id, lib, reason), http)
        except AIError as e:
            if not e.retry:
                return None, f"AI se kaam nahi ho paya: {e}"
            reason = str(e)
            continue
        new_source = extract_code(reply)
        reason = check_ai_output(source, new_source, admin_id)
        if reason is None:
            return new_source, None
    return None, f"AI ne {AI_TRIES} koshish ke baad bhi sahi code nahi diya (aakhri wajah: {reason})."


def plan(source, choice, admin_id=None, ai_cfg=None, http=None):
    """Returns (new_source, reply_text). new_source is None when nothing needs adding."""
    if choice == BTN_DB:
        return plan_backup(source, admin_id, ai_cfg, http)
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
    """Reply keyboard: the 4 options, one per row (as before), then a row with the 2 info buttons.
    Each button has a fill colour (style) from BUTTON_STYLE."""
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(_button(BTN_WEB))
    kb.row(_button(BTN_PINGER))
    kb.row(_button(BTN_BOTH))
    kb.row(_button(BTN_DB))
    kb.row(_button(BTN_STATUS), _button(BTN_LAST))
    kb.row(_button(BTN_AI))
    return kb


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
        self.ai_wait = set()  # chat ids asked for the AI key: their next text is the key
        self.ai_model_wait = set()  # chat ids asked for an AI model name: their next text is the model
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
            return self.bot.send_message(chat_id, warn("Ye file bahut badi hai. Limit 300 KB hai."))
        prog = Progress(self.bot, chat_id, self.pause)
        prog.start()
        try:
            try:
                info = self.bot.get_file(doc.file_id)
                data = self.bot.download_file(info.file_path)
            except Exception:
                return self.bot.send_message(chat_id, warn("File download nahi ho payi. Dobara bhejo."))
            if not isinstance(data, bytes) or len(data) > MAX_BYTES:
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

    def on_ai_button(self, message):
        chat_id = message.chat.id
        with self.lock:
            self.ai_wait.add(chat_id)
            self.ai_model_wait.discard(chat_id)
        self.bot.send_message(chat_id, card(f"🤖 {fx_bold('AI key')}", [ai_status_line(self.data_dir)], AI_KEY_PROMPT),
                              reply_markup=option_keyboard())

    def on_ai_key(self, message):
        chat_id = message.chat.id
        with self.lock:
            self.ai_wait.discard(chat_id)
        key = (message.text or "").strip()
        try:
            self.bot.delete_message(chat_id, message.message_id)  # the key must not stay in the chat
        except Exception:
            pass
        provider = detect_provider(key)
        if provider is None:
            return self.bot.send_message(chat_id, warn("Ye key pehchaan nahi aayi. OpenRouter (sk-or-), Groq (gsk_), "
                                                       "OpenAI (sk-), Google Gemini (AIza) ya Anthropic (sk-ant-) "
                                                       "ki key bhejo. Key save nahi hui."),
                                         reply_markup=option_keyboard())
        cfg = {"provider": provider, "key": key, "model": AI_DEFAULT_MODEL[provider], "base_url": None}
        reason = ai_check_key(cfg, requests)
        if reason is not None:
            return self.bot.send_message(chat_id, warn(f"{AI_NAME[provider]} pehchana. {reason} Key save nahi hui. "
                                                       "'AI key set karo' dabakar dobara bhejo."),
                                         reply_markup=option_keyboard())
        save_ai_config(self.data_dir, cfg)
        self.bot.send_message(chat_id, card(f"✅ {fx_bold('AI key save ho gayi')}", [ai_status_line(self.data_dir)],
                                            f"🧠 Ye {AI_NAME[provider]} ka model use hoga. Model badalna ho to "
                                            "/aimodel likho, phir model ka naam bhejo (jaise anthropic/claude-sonnet-5.5). "
                                            "Default wapas lene ke liye /aimodel default likho.\n"
                                            "Ye key chat me nahi dikhegi. Hatane ke liye /aiclear likho."),
                              reply_markup=option_keyboard())

    def on_aimodel(self, message):
        """/aimodel: shows the model in use and waits for a name. /aimodel NAME sets it at once,
        /aimodel default goes back to the provider's default. Each new name gets a test call first."""
        chat_id = message.chat.id
        if not self.is_owner(message.from_user.id):
            return self.refuse(chat_id)
        parts = (message.text or "").split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        cfg = load_ai_config(self.data_dir)
        if cfg is None:
            return self.bot.send_message(chat_id, warn("Pehle 'AI key set karo' se AI key daalo, phir model badal sakte ho."),
                                         reply_markup=option_keyboard())
        if not arg:
            with self.lock:
                self.ai_model_wait.add(chat_id)
                self.ai_wait.discard(chat_id)
            return self.bot.send_message(chat_id, card(f"🧠 {fx_bold('AI model')}", [ai_status_line(self.data_dir)],
                                                       "Naya model ka naam bhejo (jaise anthropic/claude-sonnet-5.5). "
                                                       "Default ke liye: default likho. Cancel karne ke liye koi menu button dabao."),
                                         reply_markup=option_keyboard())
        self.set_ai_model(chat_id, cfg, arg)

    def on_ai_model_text(self, message):
        chat_id = message.chat.id
        with self.lock:
            self.ai_model_wait.discard(chat_id)
        self.set_ai_model(chat_id, load_ai_config(self.data_dir), (message.text or "").strip())

    def set_ai_model(self, chat_id, cfg, name):
        if cfg is None:
            return self.bot.send_message(chat_id, warn("AI key nahi hai. Pehle 'AI key set karo' se key daalo."),
                                         reply_markup=option_keyboard())
        if env_ai_model():
            return self.bot.send_message(chat_id, warn("Render me AI_MODEL env variable set hai, wo disk wali setting "
                                                       "se pehle chalta hai. Model wahan khud badlo."),
                                         reply_markup=option_keyboard())
        if name.lower() == "default":
            save_ai_model(self.data_dir, None)
            return self.bot.send_message(chat_id, card(f"✅ {fx_bold('Model default ho gaya')}",
                                                       [ai_status_line(self.data_dir)]), reply_markup=option_keyboard())
        if not AI_MODEL_NAME.fullmatch(name):
            return self.bot.send_message(chat_id, warn("Model ka naam galat lag raha hai (jaise anthropic/claude-sonnet-5.5). "
                                                       "Purana model chal raha hai."), reply_markup=option_keyboard())
        reason = ai_check_key(dict(cfg, model=name), requests)
        if reason is not None:
            return self.bot.send_message(chat_id, warn(f"{reason} Model save nahi hua, purana model chalta rahega."),
                                         reply_markup=option_keyboard())
        save_ai_model(self.data_dir, name)
        self.bot.send_message(chat_id, card(f"✅ {fx_bold('Model badal gaya')}", [ai_status_line(self.data_dir)]),
                              reply_markup=option_keyboard())

    def on_ai_clear(self, message):
        chat_id = message.chat.id
        if not self.is_owner(message.from_user.id):
            return self.refuse(chat_id)
        with self.lock:
            self.ai_wait.discard(chat_id)
            self.ai_model_wait.discard(chat_id)
        removed = delete_ai_config(self.data_dir)
        text = "Bot ki disk se AI key hata di." if removed else "Bot ki disk par koi AI key nahi thi."
        if os.environ.get("AI_API_KEY", "").strip():
            text += " Render me AI_API_KEY env variable khud hata do."
        self.bot.send_message(chat_id, text, reply_markup=option_keyboard())

    def on_text(self, message):
        chat_id = message.chat.id
        if not self.is_owner(message.from_user.id):
            return self.refuse(chat_id)
        raw = (message.text or "").strip()
        if chat_id in self.ai_wait:
            if raw and not raw.startswith("/") and canonical_label(raw) not in MENU_BUTTONS:
                return self.on_ai_key(message)
            with self.lock:
                self.ai_wait.discard(chat_id)
        if chat_id in self.ai_model_wait:
            if raw and not raw.startswith("/") and canonical_label(raw) not in MENU_BUTTONS:
                return self.on_ai_model_text(message)
            with self.lock:
                self.ai_model_wait.discard(chat_id)
        # Old labels (no emoji) map to the current ones, so both work.
        text = canonical_label(raw)
        if text.startswith("/"):
            return  # commands are handled by their own handlers; unknown ones are ignored
        # The two info buttons go to the same handlers as the typed commands.
        if text == BTN_STATUS:
            return self.on_status(message)
        if text == BTN_LAST:
            return self.on_last(message)
        if text == BTN_AI:
            return self.on_ai_button(message)
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
                new_source, reply = plan(source, text, message.from_user.id, load_ai_config(self.data_dir), requests)
            except Exception:
                return self.bot.send_message(chat_id, warn("Kuch gadbad ho gayi, file nahi badli. "
                                                           "Dobara try karo."))
            if new_source is None:
                return self.bot.send_message(chat_id, f"ℹ️ {fx_bold('File nahi badli.')}\n\n{reply}")
            prog.step(2)
            out_name = result_name(name)
            out_path = save_unique(self.user_dir(chat_id, "results"), out_name, new_source.encode("utf-8"))
            prog.action("upload_document")
            send_document_file(self.bot, chat_id, out_path, out_name,
                               card(f"✅ {fx_bold('Naya file taiyaar hai')}: {out_name}", [reply], RESULT_NOTE),
                               private)
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

    @bot.message_handler(commands=["aiclear"])
    def _hyv_aiclear(message):
        impl.on_ai_clear(message)

    @bot.message_handler(commands=["aimodel"])
    def _hyv_aimodel(message):
        impl.on_aimodel(message)

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
