#!/usr/bin/env python3
"""Local control panel for ghostkeys.

    .venv/bin/python panel.py          # then open http://127.0.0.1:8765

Sliders for every setting, Start/Stop, live stats while it types, and a table
of target vs. actual results. One Monkeytype window stays open between runs;
"close browser" closes it. Every result is also appended to results.jsonl.
Only listens on 127.0.0.1.
"""
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
RESULTS_FILE = HERE / "results.jsonl"
PORT = 8765

state = {"running": False, "browser": False, "progress": None, "target": None,
         "results": [], "log": []}
lock = threading.Lock()
worker: subprocess.Popen | None = None
last_settings: dict = {}


def load_history() -> None:
    if not RESULTS_FILE.exists():
        return
    rows = []
    for line in RESULTS_FILE.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    state["results"] = rows[-50:]


def bot_cfg(s: dict) -> dict:
    wmin, wmax = int(s["wpm_min"]), int(s["wpm_max"])
    lo, hi = min(wmin, wmax), max(wmin, wmax)
    return {
        "wpm": f"{lo}-{hi}" if lo != hi else str(lo),
        "raw": None, "raw_gap": float(s.get("raw_gap", 0)),
        "acc": float(s["acc"]), "consistency": float(s["consistency"]),
        "warmup": float(s["warmup"]), "fatigue": float(s["fatigue"]),
        "mode": f"{s['mode']} {s['amount']}",
        "language": (s.get("language") or "english").strip(),
        "punctuation": bool(s.get("punctuation")), "numbers": bool(s.get("numbers")),
        "loops": max(1, int(s.get("loops", 1))), "headless": bool(s.get("headless")),
    }


def log(msg: str) -> None:
    state["log"] = (state["log"] + [msg])[-30:]


def pump(p: subprocess.Popen) -> None:
    global worker
    for line in p.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            ev = {"type": "log", "msg": line}
        with lock:
            kind = ev["type"]
            if kind == "progress":
                state["progress"] = ev
            elif kind == "target":
                state["target"] = ev
                state["progress"] = None
            elif kind == "result":
                row = {**ev, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "settings": last_settings}
                state["results"] = (state["results"] + [row])[-50:]
                with RESULTS_FILE.open("a") as f:
                    f.write(json.dumps(row) + "\n")
            elif kind == "busy":
                state.update(running=True, browser=True)
            elif kind == "done":
                state["running"] = False
                log("done · monkeytype window left open")
            elif kind == "stopped":
                state["running"] = False
                log("[x] stopped · monkeytype window left open")
            elif kind == "log":
                log(ev["msg"])
    p.wait()
    with lock:
        state.update(running=False, browser=False)
        log("monkeytype window closed")
        if worker is p:
            worker = None


def send(msg: dict) -> bool:
    p = worker
    if p is None or p.poll() is not None:
        return False
    try:
        p.stdin.write(json.dumps(msg) + "\n")
        p.stdin.flush()
        return True
    except (BrokenPipeError, OSError):
        return False


def ensure_worker() -> None:
    global worker
    if worker is not None and worker.poll() is None:
        return
    worker = subprocess.Popen(
        [sys.executable, "-u", str(HERE / "ghostkeys.py"), "--serve"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, cwd=HERE)
    threading.Thread(target=pump, args=(worker,), daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self._send(200, (HERE / "panel.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/status":
            with lock:
                self._send(200, json.dumps(state).encode())
        else:
            self._send(404, b"{}")

    def do_POST(self):
        global last_settings
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/start":
            with lock:
                if state["running"]:
                    return self._send(409, b'{"error":"already running"}')
                state.update(running=True, progress=None, target=None)
                if not state["browser"]:
                    log("starting...")
            last_settings = body
            ensure_worker()
            send({"cmd": "run", "cfg": bot_cfg(body)})
            self._send(200, b"{}")
        elif self.path == "/stop":
            send({"cmd": "stop"})
            self._send(200, b"{}")
        elif self.path == "/close":
            if not send({"cmd": "quit"}) and worker is not None:
                worker.terminate()
            self._send(200, b"{}")
        elif self.path == "/clear":
            with lock:
                state["results"] = []
            self._send(200, b"{}")
        else:
            self._send(404, b"{}")


def shutdown() -> None:
    if send({"cmd": "quit"}):
        try:
            worker.wait(timeout=5)
        except subprocess.TimeoutExpired:
            worker.terminate()


if __name__ == "__main__":
    load_history()
    print(f"ghostkeys panel on http://127.0.0.1:{PORT}  (Ctrl-C to quit)")
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()
