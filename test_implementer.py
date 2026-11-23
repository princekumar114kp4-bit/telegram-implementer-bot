"""Local tests for the implementer bot. No real Telegram calls, no real token, no hosting.

Run: python3 -I /workspace/agent/implementer-bot-2026-11-23/test_implementer.py
"""

import hashlib
import http.server
import importlib.util
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
FAILS = []
COUNT = [0]


def check(name, cond, detail=""):
    COUNT[0] += 1
    if not cond:
        FAILS.append(name)
    print(("PASS " if cond else "FAIL ") + name + (("  [" + str(detail)[:200] + "]") if detail and not cond else ""))


spec = importlib.util.spec_from_file_location("implementer_bot", os.path.join(HERE, "implementer_bot.py"))
ib = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ib)

SAMPLES = os.path.join(HERE, "samples")
WORK = tempfile.mkdtemp(prefix="impl-test-")


def read(path):
    with open(path, "rb") as f:
        return f.read()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def compiles(path):
    r = subprocess.run([PY, "-I", "-m", "py_compile", path], capture_output=True, text=True)
    pyc_dir = os.path.join(os.path.dirname(path), "__pycache__")
    if os.path.isdir(pyc_dir):
        shutil.rmtree(pyc_dir, ignore_errors=True)
    return r.returncode == 0, r.stderr[-300:]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def env_without_render(**extra):
    # Drop Render variables and proxy variables: the local test server must be reached directly
    env = {k: v for k, v in os.environ.items()
           if k not in ("RENDER_EXTERNAL_URL", "PING_MINUTES", "PORT") and "proxy" not in k.lower()}
    env.update(extra)
    return env


# ---- 1. Detection ----------------------------------------------------------------------
print("== detection")
NOTIFIER = '''import threading
import time
import requests

def poll_news():
    while True:
        requests.get("https://news.example.com/api", timeout=10)
        time.sleep(600)

threading.Thread(target=poll_news).start()
'''
PING_CMD = 'import telebot\nbot = telebot.TeleBot("x")\n@bot.message_handler(commands=["ping"])\ndef ping(m):\n    bot.reply_to(m, "pong")\n'
BIND_ALL = 'import socket\nHOST = "0.0.0.0"\n'

expected = {
    "plain_bot.py": ("none", "none"),
    "flask_web_bot.py": ("sure", "none"),
    "pinger_bot.py": ("none", "sure"),
    "both_bot.py": ("sure", "sure"),
}
for name, (w, p) in expected.items():
    src = read(os.path.join(SAMPLES, name)).decode()
    check(f"detect {name} web={w} pinger={p}",
          ib.web_server_status(src) == w and ib.pinger_status(src) == p,
          (ib.web_server_status(src), ib.pinger_status(src)))
check("detect notifier thread is unsure pinger", ib.pinger_status(NOTIFIER) == "unsure", ib.pinger_status(NOTIFIER))
check("detect notifier has no web", ib.web_server_status(NOTIFIER) == "none")
check("detect /ping command is unsure pinger", ib.pinger_status(PING_CMD) == "unsure", ib.pinger_status(PING_CMD))
check("detect bare word pinger in a comment is unsure, not sure",
      ib.pinger_status("# a pinger note\nx = 1\n") == "unsure")
check("detect 0.0.0.0 alone is unsure web",ib.web_server_status(BIND_ALL) == "unsure", ib.web_server_status(BIND_ALL))
check("detect plain empty file", ib.web_server_status("x = 1\n") == "none" and ib.pinger_status("x = 1\n") == "none")

# ---- 2. Each option on each sample -----------------------------------------------------
print("== options on samples")
BTN = {"a": ib.BTN_WEB, "b": ib.BTN_PINGER, "c": ib.BTN_BOTH}
tmp_out = os.path.join(WORK, "outputs")
os.makedirs(tmp_out)
made = {}  # (sample, option) -> path of result file

for name in expected:
    original = read(os.path.join(SAMPLES, name))
    before = sha(original)
    src = original.decode()
    for opt, label in BTN.items():
        new_src, reply = ib.plan(src, label)
        if new_src is None:
            check(f"{name} option {opt}: nothing added, message given", reply and "kuch add nahi kiya" in reply, reply)
            made[(name, opt)] = None
            continue
        path = os.path.join(tmp_out, f"{name}-{opt}.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_src)
        made[(name, opt)] = path
        ok, err = compiles(path)
        check(f"{name} option {opt}: result compiles with -I", ok, err)
        check(f"{name} option {opt}: reply text set", bool(reply), reply)
    check(f"{name}: original file untouched", sha(read(os.path.join(SAMPLES, name))) == before)

# Expected outcome per sample and option: which blocks were added
def added_markers(path):
    if path is None:
        return set()
    text = read(path).decode()
    out = set()
    if "# >>> hyv-injected:web" in text:
        out.add("web")
    if "# >>> hyv-injected:pinger" in text:
        out.add("pinger")
    return out


expect_added = {
    ("plain_bot.py", "a"): {"web"}, ("plain_bot.py", "b"): {"pinger"}, ("plain_bot.py", "c"): {"web", "pinger"},
    ("flask_web_bot.py", "a"): set(), ("flask_web_bot.py", "b"): {"pinger"}, ("flask_web_bot.py", "c"): {"pinger"},
    ("pinger_bot.py", "a"): {"web"}, ("pinger_bot.py", "b"): set(), ("pinger_bot.py", "c"): {"web"},
    ("both_bot.py", "a"): set(), ("both_bot.py", "b"): set(), ("both_bot.py", "c"): set(),
}
for key, want in expect_added.items():
    got = added_markers(made[key])
    check(f"{key[0]} option {key[1]}: adds {sorted(want) or 'nothing'}", got == want, sorted(got))

# Messages for the skip cases
check("flask option a says web server already there",
      "Web server ka code pehle se hai" in ib.plan(read(os.path.join(SAMPLES, "flask_web_bot.py")).decode(), ib.BTN_WEB)[1])
check("pinger option b says pinger already there",
      "Pinger ka code pehle se hai" in ib.plan(read(os.path.join(SAMPLES, "pinger_bot.py")).decode(), ib.BTN_PINGER)[1])
check("flask option c says it skipped web and added pinger",
      "Web server ka code pehle se hai, isliye use chhoda" in ib.plan(read(os.path.join(SAMPLES, "flask_web_bot.py")).decode(), ib.BTN_BOTH)[1])
check("pinger option c says it skipped pinger and added web",
      "Pinger ka code pehle se hai, isliye use chhoda" in ib.plan(read(os.path.join(SAMPLES, "pinger_bot.py")).decode(), ib.BTN_BOTH)[1])
both_msg = ib.plan(read(os.path.join(SAMPLES, "both_bot.py")).decode(), ib.BTN_BOTH)[1]
check("both_bot option c: says web and pinger are already there",
      "Web server ka code pehle se hai" in both_msg and "Pinger ka code pehle se hai" in both_msg, both_msg)
check("unsure notifier option b is not added and says not sure",
      ib.plan(NOTIFIER, ib.BTN_PINGER)[0] is None and "pakka nahi" in ib.plan(NOTIFIER, ib.BTN_PINGER)[1])

# Idempotent: running again on a result adds nothing
for (name, opt), path in made.items():
    if path is None:
        continue
    again, reply = ib.plan(read(path).decode(), BTN[opt])
    check(f"{name} option {opt}: second run adds nothing", again is None, reply)

# Existing code is unchanged: AST check for the plain sample with both blocks
plain_src = read(os.path.join(SAMPLES, "plain_bot.py")).decode()
both_added = made[("plain_bot.py", "c")]
import ast as _ast
orig_dump = [_ast.dump(n) for n in _ast.parse(plain_src).body]
new_tree = _ast.parse(read(both_added).decode()).body
kept = [n for n in new_tree if not (isinstance(n, _ast.Try) and n.lineno and "hyv" in _ast.get_source_segment(read(both_added).decode(), n))]
check("plain + both: existing top-level code unchanged", [_ast.dump(n) for n in kept] == orig_dump)

# ---- 3. Edge layouts for the insert point ----------------------------------------------
print("== insert point edge cases")
edge_cases = {
    "crlf_no_trailing_newline": 'import os\r\nimport sys\r\n\r\nprint("x")',
    "future_and_docstring": '"""Doc."""\nfrom __future__ import annotations\nimport os\n\n\ndef f() -> int:\n    return 1\n',
    "no_imports": 'VALUE = 3\nprint(VALUE)\n',
    "imports_after_code": 'x = 1\nimport os\nprint(x, os)\n',
}
for case, src in edge_cases.items():
    for label in (ib.BTN_BOTH,):
        try:
            new_src, _ = ib.plan(src, label)
            p = os.path.join(tmp_out, f"edge-{case}.py")
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.write(new_src)
            ok, err = compiles(p)
            check(f"edge {case}: both added and compiles", ok and new_src.count("hyv-injected:") == 4, err)
        except Exception as e:
            check(f"edge {case}: both added and compiles", False, type(e).__name__)

# ---- 4. Live run of the injected web block ----------------------------------------------
print("== live web block")


def wait_port(port, seconds=15):
    end = time.time() + seconds
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def curl_body(port, path):
    # urllib with no proxies, so the sandbox HTTP proxy variables do not get in the way
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
            return r.read().decode(), str(r.status)
    except urllib.error.HTTPError as e:
        return e.read().decode(), str(e.code)
    except OSError as e:
        return type(e).__name__, "000"


for label_name, key in (("web only", ("plain_bot.py", "a")), ("web+pinger", ("plain_bot.py", "c"))):
    path = made[key]
    port = free_port()
    proc = subprocess.Popen([PY, "-I", path], cwd=WORK, env=env_without_render(PORT=str(port)),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        up = wait_port(port)
        check(f"web block ({label_name}): port {port} opens", up)
        if up:
            body, code = curl_body(port, "/health")
            check(f"web block ({label_name}): /health answers ok", body == "ok" and code == "200", (body, code))
            body, code = curl_body(port, "/")
            check(f"web block ({label_name}): / answers ok", body == "ok" and code == "200", (body, code))
            body, code = curl_body(port, "/nope")
            check(f"web block ({label_name}): other path is 404", code == "404", code)
        check(f"web block ({label_name}): bot process still alive", proc.poll() is None)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

# Port already taken: the bot must still start (failure never stops it)
busy = socket.socket()
busy.bind(("127.0.0.1", 0))
busy.listen(1)
busy_port = busy.getsockname()[1]
proc = subprocess.Popen([PY, "-I", made[("plain_bot.py", "a")]], cwd=WORK,
                        env=env_without_render(PORT=str(busy_port)),
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
time.sleep(4)
check("web block: busy port does not stop the bot", proc.poll() is None)
proc.terminate()
proc.wait(timeout=10)
busy.close()

# ---- 5. Live run of the injected pinger block -------------------------------------------
print("== live pinger block (about 40 seconds)")
hits = []


class Counter(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        hits.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format, *args):
        pass


local = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Counter)
threading.Thread(target=local.serve_forever, daemon=True).start()
local_url = f"http://127.0.0.1:{local.server_address[1]}/"

pinger_path = made[("plain_bot.py", "b")]
env_on = env_without_render(RENDER_EXTERNAL_URL=local_url, PING_MINUTES="1", PORT=str(free_port()))
env_off = env_without_render(PORT=str(free_port()))  # RENDER_EXTERNAL_URL empty: no pings at all
p_on = subprocess.Popen([PY, "-I", "-u", pinger_path], cwd=WORK, env=env_on, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True)
off_path = os.path.join(WORK, "pinger_off.py")
shutil.copy(pinger_path, off_path)
p_off = subprocess.Popen([PY, "-I", "-u", off_path], cwd=WORK, env=env_off, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True)
deadline = time.time() + 50
while time.time() < deadline and not hits:
    time.sleep(1)
time.sleep(2)
hits_after_on = list(hits)
check("pinger block: hits the URL from RENDER_EXTERNAL_URL", len(hits_after_on) >= 1, len(hits_after_on))
check("pinger block: first hit is the root path", hits_after_on[:1] == ["/"], hits_after_on[:1])
p_on.terminate()
out_on = ""
try:
    out_on, _ = p_on.communicate(timeout=10)
except subprocess.TimeoutExpired:
    p_on.kill()
    out_on, _ = p_on.communicate()
check("pinger block: logs status only, never the full URL",
      "keep-alive ping: 200" in out_on and "127.0.0.1" not in out_on and local_url not in out_on,
      out_on[-200:])
# The URL-less copy must stay silent and never call the local server
time.sleep(1)
p_off.terminate()
try:
    p_off.communicate(timeout=10)
except subprocess.TimeoutExpired:
    p_off.kill()
    p_off.communicate()
check("pinger block: no URL set means no pings", len(hits) == len(hits_after_on),
      (len(hits), len(hits_after_on)))
local.shutdown()

# PING_MINUTES parsing, run inside the test process (block starts a daemon thread that sleeps first)
block_ns = {}
old_env = os.environ.get("PING_MINUTES")
for raw, want in (("abc", 5), ("0", 5), ("61", 5), ("1", 1), ("60", 60), ("", 5)):
    os.environ["PING_MINUTES"] = raw
    ns = {}
    exec(compile(ib.PINGER_BLOCK, "<block>", "exec"), ns)
    check(f"PING_MINUTES={raw!r} gives {want}", ns["_hyv_ping_minutes"]() == want, ns["_hyv_ping_minutes"]())
if old_env is None:
    os.environ.pop("PING_MINUTES", None)
else:
    os.environ["PING_MINUTES"] = old_env

# ---- 6. The bot's own logic with a fake Telegram object --------------------------------
print("== bot logic with a fake Telegram object")


class FakeBot:
    def __init__(self):
        self.sent = []
        self.docs = []
        self.files = {}
        self.downloads = 0

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))

    def send_document(self, chat_id, f, visible_file_name=None, caption=None):
        self.docs.append((chat_id, f.read(), visible_file_name, caption))

    def get_file(self, file_id):
        return types.SimpleNamespace(file_path="files/" + file_id)

    def download_file(self, path):
        self.downloads += 1
        return self.files[path]


def msg(chat_id, user_id, text=None, doc=None):
    m = types.SimpleNamespace(chat=types.SimpleNamespace(id=chat_id), from_user=types.SimpleNamespace(id=user_id),
                              text=text, document=None)
    if doc is not None:
        m.document = types.SimpleNamespace(file_name=doc[0], file_id=doc[1], file_size=len(doc[2]))
    return m


OWNER = 555001
OTHER = 999002
CHAT = 555001
data_root = os.path.join(WORK, "data")
fb = FakeBot()
impl = ib.Implementer(fb, OWNER, data_root)
plain_bytes = read(os.path.join(SAMPLES, "plain_bot.py"))
flask_bytes = read(os.path.join(SAMPLES, "flask_web_bot.py"))
fb.files["files/F1"] = plain_bytes
fb.files["files/F2"] = flask_bytes
fb.files["files/F3"] = b"def broken(:\n"

impl.on_document(msg(999, OTHER, doc=("bot.py", "F1", plain_bytes)))
check("other user document: refused", fb.sent[-1][1] == "Ye bot private hai." and fb.downloads == 0, fb.sent[-1][1])
impl.on_text(msg(999, OTHER, text=ib.BTN_BOTH))
check("other user button: refused", fb.sent[-1][1] == "Ye bot private hai.")
check("other user: nothing saved", not os.path.exists(os.path.join(data_root, "999")))

impl.on_document(msg(CHAT, OWNER, doc=("notes.txt", "F1", plain_bytes)))
check("owner non-.py file: plain refusal", "Sirf .py" in fb.sent[-1][1], fb.sent[-1][1])
impl.on_document(msg(CHAT, OWNER, doc=("bad.py", "F3", fb.files["files/F3"])))
check("owner broken .py: syntax message, nothing saved",
      "syntax error" in fb.sent[-1][1] and not os.path.exists(os.path.join(data_root, str(CHAT), "uploads")))

impl.on_document(msg(CHAT, OWNER, doc=("my bot.py", "F1", plain_bytes)))
sent_text, sent_markup = fb.sent[-1][1], fb.sent[-1][2]
check("owner .py: asks for an option", "Ab neeche se option chuno" in sent_text, sent_text)
kb = json.loads(sent_markup.to_json())["keyboard"]
check("owner .py: keyboard has exactly 3 rows, one button each",
      kb == [[{"text": ib.BTN_WEB}], [{"text": ib.BTN_PINGER}], [{"text": ib.BTN_BOTH}]], kb)
uploads = os.listdir(os.path.join(data_root, str(CHAT), "uploads"))
check("owner .py: saved in the per-chat folder", len(uploads) == 1 and uploads[0].endswith("-my_bot.py"), uploads)
saved = os.path.join(data_root, str(CHAT), "uploads", uploads[0])
check("owner .py: saved copy is the same bytes", read(saved) == plain_bytes)

impl.on_text(msg(CHAT, OWNER, text=ib.BTN_PINGER))
check("tap pinger: a file was sent back", len(fb.docs) == 1)
cid, data, vname, caption = fb.docs[-1]
check("tap pinger: file name is the updated name", vname == "my_bot-updated.py", vname)
check("tap pinger: returned file compiles", b"hyv-injected:pinger" in data and b"hyv-injected:web" not in data)
check("tap pinger: caption says redeploy", "dobara deploy" in caption and "Pinger add kar diya" in caption, caption)
check("tap pinger: original upload still untouched", read(saved) == plain_bytes)
returned = os.path.join(WORK, "returned_pinger.py")
with open(returned, "wb") as f:
    f.write(data)
ok, err = compiles(returned)
check("tap pinger: returned file compiles with -I", ok, err)

impl.on_text(msg(CHAT, OWNER, text=ib.BTN_WEB))
cid, data, vname, caption = fb.docs[-1]
check("tap web on plain: returns web block", b"hyv-injected:web" in data and b"hyv-injected:pinger" not in data)
check("tap web: result is new file, not overwritten", len(os.listdir(os.path.join(data_root, str(CHAT), "results"))) == 2)

impl.on_document(msg(CHAT, OWNER, doc=("my bot.py", "F2", flask_bytes)))
impl.on_text(msg(CHAT, OWNER, text=ib.BTN_WEB))
check("flask file, tap web: no document sent, already-there message",
      "Web server ka code pehle se hai" in fb.sent[-1][1] and len(fb.docs) == 2, fb.sent[-1][1])
impl.on_text(msg(CHAT, OWNER, text=ib.BTN_BOTH))
cid, data, vname, caption = fb.docs[-1]
check("flask file, tap both: adds pinger only and says web was skipped",
      b"hyv-injected:pinger" in data and b"hyv-injected:web" not in data and "Web server ka code pehle se hai, isliye use chhoda" in caption,
      caption)

impl.on_document(msg(CHAT, OWNER, doc=("same.py", "F1", plain_bytes)))
impl.on_document(msg(CHAT, OWNER, doc=("same.py", "F1", plain_bytes)))
same_files = [n for n in os.listdir(os.path.join(data_root, str(CHAT), "uploads")) if n.endswith("-same.py")]
check("same name uploaded twice: two separate files, no overwrite", len(same_files) == 2, same_files)

impl.on_text(msg(777, OWNER, text=ib.BTN_BOTH))
check("button with no file: asks for a file", "Pehle apni bot ki .py file bhejo" in fb.sent[-1][1], fb.sent[-1][1])
impl.on_text(msg(CHAT, OWNER, text="hello there"))
check("owner text while file pending: asks for a button",
      "Neeche ke button" in fb.sent[-1][1], fb.sent[-1][1])
before_count = len(fb.sent)
impl.on_text(msg(CHAT, OWNER, text="/whatever"))
check("unknown command: ignored", len(fb.sent) == before_count)

# Owner id unset: /start shows the user's id only (same as the pinger bot), everything else is ignored
impl_noowner = ib.Implementer(fb, None, data_root)
impl_noowner.on_start(msg(4242, 4242, text="/start"))
check("no OWNER_ID: /start shows own id", "Aapka Telegram ID: 4242" in fb.sent[-1][1], fb.sent[-1][1])
check("parse_owner: numeric ok, junk none", ib.parse_owner(" 123 ") == 123 and ib.parse_owner("abc") is None
      and ib.parse_owner("") is None)

# Token is hidden in library log lines
logger = logging.getLogger("TeleBot")
ib.hide_token("123456:SECRET-TEST-TOKEN")
captured = []


class Grab(logging.Handler):
    def emit(self, record):
        captured.append(record.getMessage())


logger.addHandler(Grab())
logger.error("request to /bot123456:SECRET-TEST-TOKEN/getUpdates failed")
check("log lines hide the token", captured and "SECRET-TEST-TOKEN" not in captured[-1] and "<hidden>" in captured[-1],
      captured[-1:] if captured else "no log")

# ---- 7. The bot's own web server and pinger (no Telegram, local only) --------------------
print("== this bot's own web server and pinger")
own_path = os.path.join(HERE, "implementer_bot.py")
own_src = read(own_path).decode()
check("own file: detected as having a web server", ib.web_server_status(own_src) == "sure", ib.web_server_status(own_src))
check("own file: detected as having a pinger", ib.pinger_status(own_src) == "sure", ib.pinger_status(own_src))
own_new, own_reply = ib.plan(own_src, ib.BTN_BOTH)
check("own file sent to itself: nothing added twice", own_new is None and "pehle se hai" in own_reply, own_reply)

saved_env = {k: os.environ.get(k) for k in ("PORT", "PING_MINUTES", "RENDER_EXTERNAL_URL", "NO_PROXY", "no_proxy")}
# The sandbox sets proxy variables; the local test address must bypass them (Render has none)
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

# Starts in a background thread with PORT from the environment
own_port = free_port()
os.environ["PORT"] = str(own_port)
check("own web server: thread started", ib.start_web_server() is not None)
up = wait_port(own_port)
check("own web server: port opens", up)
if up:
    body, code = curl_body(own_port, "/health")
    check("own web server: /health answers ok", body == "ok" and code == "200", (body, code))
    body, code = curl_body(own_port, "/")
    check("own web server: / answers ok", body == "ok" and code == "200", (body, code))
    body, code = curl_body(own_port, "/nope")
    check("own web server: other path is 404", code == "404", code)

# Port already taken: starting must not raise
busy2 = socket.socket()
busy2.bind(("127.0.0.1", 0))
busy2.listen(1)
os.environ["PORT"] = str(busy2.getsockname()[1])
try:
    check("own web server: busy port does not raise", ib.start_web_server() is not None)
finally:
    busy2.close()
time.sleep(2)

# PING_MINUTES: 1 to 60, anything else is 5
for raw, want in (("abc", 5), ("0", 5), ("61", 5), ("1", 1), ("60", 60), ("", 5), (" 7 ", 7)):
    os.environ["PING_MINUTES"] = raw
    check(f"own ping_minutes PING_MINUTES={raw!r} gives {want}", ib.ping_minutes() == want, ib.ping_minutes())
os.environ.pop("PING_MINUTES", None)
check("own ping_minutes unset gives 5", ib.ping_minutes() == 5)

# No RENDER_EXTERNAL_URL: skipped cleanly, no thread
os.environ.pop("RENDER_EXTERNAL_URL", None)
check("own pinger: no URL, skipped with no thread", ib.start_self_pinger() is None)

# With a URL: first ping (after its 30 second start delay) reaches the root path
own_hits = []


class OwnCounter(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        own_hits.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format, *args):
        pass


own_local = http.server.ThreadingHTTPServer(("127.0.0.1", 0), OwnCounter)
threading.Thread(target=own_local.serve_forever, daemon=True).start()
os.environ["RENDER_EXTERNAL_URL"] = f"http://127.0.0.1:{own_local.server_address[1]}/"
os.environ["PING_MINUTES"] = "1"
check("own pinger: thread started with URL", ib.start_self_pinger() is not None)
deadline = time.time() + 50
while time.time() < deadline and not own_hits:
    time.sleep(1)
check("own pinger: first ping reaches the root path", own_hits[:1] == ["/"], own_hits[:1])
own_local.shutdown()

for k, v in saved_env.items():
    if v is None:
        os.environ.pop(k, None)
    else:
        os.environ[k] = v

# The file runs as a script: with no token it prints the message and stops with code 1
env_no_token = env_without_render()
for k in ("BOT_TOKEN", "OWNER_ID"):
    env_no_token.pop(k, None)
r = subprocess.run([PY, "-I", own_path], cwd=WORK, env=env_no_token, capture_output=True, text=True, timeout=60)
check("own file runs as a script: stops with code 1 when BOT_TOKEN is missing",
      r.returncode == 1 and "BOT_TOKEN is not set" in r.stdout, (r.returncode, r.stdout[-200:], r.stderr[-200:]))
check("own file: compiles with -I", compiles(own_path)[0])

# ---- Summary ----------------------------------------------------------------------------
print()
print(f"{COUNT[0] - len(FAILS)} of {COUNT[0]} checks passed")
if FAILS:
    print("FAILED: " + "; ".join(FAILS))
    sys.exit(1)
print("ALL PASSED")
