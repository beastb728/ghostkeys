#!/usr/bin/env python3
"""Monkeytype typing bot with human-like stat controls.

Runs in a fresh, never-signed-in (guest) browser every time, so results are
never saved to an account or submitted anywhere.

Stats:
  --wpm          target wpm (number or range like 140-160, picked per test)
  --raw          target raw wpm (>= wpm). Gap comes from words left with errors.
  --acc          target accuracy %. Comes from typos that get corrected.
  --consistency  target consistency %. Shapes second-to-second speed swings.
  --warmup       seconds of slower start
  --fatigue      % speed drop by the end of the test
  --preset       casual | average | fast | pro | robot

Test:
  --mode         'time 15|30|60|120', 'words 10|25|50|100', 'quote all|short|medium|long|thicc'
  --language     any Monkeytype language, e.g. spanish, 'english 1k', french
  --punctuation  add punctuation (time/words modes)
  --numbers      add numbers (time/words modes)

Display:
  --ui           rich (live dashboard, default in a terminal) | plain | json
  --menu         ask for every setting interactively

Examples:
  .venv/bin/python ghostkeys.py --preset fast --mode "time 30"
  .venv/bin/python ghostkeys.py --wpm 130-145 --acc 95 --mode "quote medium" --language spanish
  .venv/bin/python ghostkeys.py --menu
"""
import argparse
import json
import math
import random
import re
import sys
import time

from playwright.sync_api import sync_playwright

PRESETS = {
    "casual":  dict(wpm="70-85",   raw_gap=4, acc=93, consistency=65, warmup=3, fatigue=6),
    "average": dict(wpm="95-110",  raw_gap=3, acc=95, consistency=70, warmup=2, fatigue=5),
    "fast":    dict(wpm="135-150", raw_gap=2, acc=96, consistency=75, warmup=2, fatigue=4),
    "pro":     dict(wpm="185-205", raw_gap=1, acc=98, consistency=80, warmup=1, fatigue=3),
    "robot":   dict(wpm="200",     raw_gap=0, acc=100, consistency=100, warmup=0, fatigue=0),
}

ROWS = ["qwertyuiop", "asdfghjkl", "zxcvbnm"]


# ---------------------------------------------------------------- helpers

def pick(spec) -> float:
    """'140-160' -> random value in range, '150' -> 150."""
    s = str(spec)
    if "-" in s:
        a, b = (float(x) for x in s.split("-", 1))
        return random.uniform(a, b)
    return float(s)


def neighbor(ch: str) -> str:
    """A key physically next to ch (realistic fat-finger)."""
    lo = ch.lower()
    for r, row in enumerate(ROWS):
        c = row.find(lo)
        if c >= 0:
            opts = [row[i] for i in (c - 1, c + 1) if 0 <= i < len(row)]
            for rr in (r - 1, r + 1):
                if 0 <= rr < len(ROWS):
                    opts += [ROWS[rr][i] for i in (c - 1, c) if 0 <= i < len(ROWS[rr])]
            out = random.choice(opts)
            return out.upper() if ch.isupper() else out
    return random.choice("asdfjkl")


def consistency_to_cov(target: float) -> float:
    """Invert Monkeytype's consistency formula: 100 * (1 - tanh(x + x^3/3 + x^5/5))."""
    if target >= 99.9:
        return 0.0
    f = lambda x: 100 * (1 - math.tanh(x + x**3 / 3 + x**5 / 5))
    lo, hi = 0.0, 3.0
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if f(mid) > target else (lo, mid)
    return (lo + hi) / 2


def speed_profile(seconds: int, cov: float, warmup: float, fatigue: float) -> list[float]:
    """Per-second speed multipliers with mean 1 and std = cov."""
    n = max(5, seconds)
    if cov == 0:
        return [1.0] * n
    base, prev = [], 0.0
    for s in range(n):
        prev = 0.55 * prev + random.gauss(0, 1)  # smooth-ish random walk
        shape = 1.0
        if warmup > 0 and s < warmup:
            shape *= 0.8 + 0.2 * (s / warmup)
        shape *= 1 - (fatigue / 100) * (s / n)
        base.append(shape * (1 + 0.15 * prev))
    mu = sum(base) / n
    sd = math.sqrt(sum((b - mu) ** 2 for b in base) / n) or 1
    return [max(0.25, 1 + (b - mu) / sd * cov) for b in base]


# ---------------------------------------------------------------- output

class Reporter:
    """Prints progress as plain text, a live rich dashboard, or JSON lines."""

    def __init__(self, ui: str):
        self.ui = ui
        self.live = None

    def emit(self, kind: str, **data) -> None:
        if self.ui == "json":
            print(json.dumps({"type": kind, **data}), flush=True)
        elif kind == "log":
            (self.live.console.print if self.live else print)(data["msg"])
        elif kind == "target" and self.ui == "plain":
            print(f"[*] target  wpm={data['wpm']:.0f}  raw={data['raw']:.0f}  "
                  f"acc={data['acc']:g}%  consistency={data['consistency']:g}%")
        elif kind == "result":
            line = (f"[+] result  wpm={data['wpm']}  raw={data['raw']}  acc={data['acc']}  "
                    f"consistency={data['consistency']}")
            (self.live.console.print if self.live else print)(line)

    def start_live(self, typist, test_label: str) -> None:
        if self.ui != "rich":
            return
        from rich.live import Live
        self.live = Live(get_renderable=lambda: dashboard(typist, test_label),
                         refresh_per_second=10, transient=False)
        self.live.start()

    def stop_live(self) -> None:
        if self.live:
            self.live.refresh()
            self.live.stop()
            self.live = None


SPARK = " ▁▂▃▄▅▆▇█"


def sparkline(values: list[float], top: float) -> str:
    top = max(top, 1)
    return "".join(SPARK[min(8, int(v / top * 8))] for v in values)


def dashboard(t, label: str):
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.console import Group

    s = t.stats()
    tg = t.targets

    def row(name, live, target, unit=""):
        live_s = f"{live:.0f}{unit}" if live is not None else "–"
        diff = "" if live is None else f"{live - target:+.0f}"
        style = "green" if live is not None and abs(live - target) <= max(3, target * 0.05) else "yellow"
        return name, Text(live_s, style=f"bold {style}"), f"{target:.0f}{unit}", Text(diff, style="dim")

    tbl = Table.grid(padding=(0, 3))
    for _ in range(4):
        tbl.add_column(justify="right")
    tbl.add_row(Text("", style="dim"), Text("live", style="dim"), Text("target", style="dim"), "")
    tbl.add_row(*row("wpm", s["wpm"], tg["wpm"]))
    tbl.add_row(*row("raw", s["raw"], tg["raw"]))
    tbl.add_row(*row("acc", s["acc"], tg["acc"], "%"))
    tbl.add_row("consistency", Text("at end", style="dim"), f"{tg['consistency']:.0f}%", "")

    hist = s["history"][-48:]
    spark = Text(sparkline(hist, max(hist + [tg["wpm"] * 1.4])), style="cyan")
    filled = int(48 * s["progress"])
    bar = Text("━" * filled, style="cyan") + Text("─" * (48 - filled), style="dim")
    body = Group(
        tbl, Text(""),
        Text("raw wpm per second", style="dim"), spark, Text(""),
        bar, Text(f"{s['elapsed']:.1f}s · {s['keys']} keys · {s['typos']} typos "
                  f"({s['left_wrong']} left in)", style="dim"),
    )
    return Panel(body, title=f"ghostkeys · {label}", border_style="cyan", width=60)


# ---------------------------------------------------------------- page

STATE_JS = """() => {
    const ws = document.querySelector('#words');
    const r = document.querySelector('#result');
    const done = !ws || ws.offsetParent === null ||
        (!!r && !r.classList.contains('hidden') && r.offsetParent !== null);
    const w = ws?.querySelector('.word.active');
    if (done || !w) return {done, idx: null, word: null};
    const word = [...w.querySelectorAll('letter')]
        .filter(l => !l.classList.contains('extra'))
        .map(l => l.dataset.char ?? l.textContent).join('');
    return {done, idx: +w.dataset.wordindex, word};
}"""

TEXT_STATS_JS = """() => {
    const ws = [...document.querySelectorAll('#words .word')];
    return {words: ws.length, chars: ws.reduce((n, w) => n + w.textContent.length + 1, 0)};
}"""


def click_button(page, text: str, timeout: int = 2500) -> None:
    page.locator("button:visible").filter(
        has_text=re.compile(rf"^\s*{re.escape(text)}\s*$")
    ).first.click(timeout=timeout)


def dismiss_cookies(page) -> None:
    try:
        page.get_by_role("button", name="reject non-essential").click(timeout=3000)
        time.sleep(0.3)
    except Exception:
        pass


def fresh_page_state() -> dict:
    """What a brand-new guest Monkeytype tab is set to."""
    return {"language": "english", "punctuation": False, "numbers": False}


def set_language(page, language: str | None, cur: dict, rep: Reporter) -> None:
    language = (language or "english").strip().lower()
    if language == cur["language"]:
        return
    try:
        click_button(page, cur["language"])          # the language button shows the current one
        page.locator("input[placeholder='Language...']").fill(language)
        time.sleep(0.3)
        page.locator("div[data-index]:visible").filter(
            has_text=re.compile(rf"^\s*{re.escape(language)}\s*$")).first.click(timeout=2500)
        time.sleep(0.8)
        page.wait_for_selector("#words .word.active")
        cur["language"] = language
    except Exception:
        page.keyboard.press("Escape")
        rep.emit("log", msg=f"[!] couldn't find language '{language}', staying on {cur['language']}")


def set_mode(page, mode: str, punctuation: bool, numbers: bool, cur: dict, rep: Reporter) -> None:
    kind, _, amount = mode.partition(" ")
    try:
        click_button(page, kind)
        time.sleep(0.4)
        if amount:
            click_button(page, amount)
            time.sleep(0.4)
        if kind in ("time", "words"):               # these toggles are hidden in quote mode
            for name, want in (("punctuation", punctuation), ("numbers", numbers)):
                if cur[name] != want:
                    click_button(page, name)
                    cur[name] = want
                    time.sleep(0.3)
        page.wait_for_selector("#words .word.active")
    except Exception:
        rep.emit("log", msg=f"[!] couldn't fully set mode '{mode}', using what's selected")


def configure(page, cfg: dict, cur: dict, rep: Reporter) -> None:
    set_language(page, cfg.get("language"), cur, rep)
    set_mode(page, cfg["mode"], bool(cfg.get("punctuation")), bool(cfg.get("numbers")), cur, rep)


def open_monkeytype(page) -> None:
    page.goto("https://monkeytype.com")
    page.wait_for_selector("#words .word.active")
    dismiss_cookies(page)


def new_test(page) -> None:
    page.reload()
    page.wait_for_selector("#words .word.active")
    time.sleep(0.5)


def read_results(page) -> dict:
    time.sleep(1.0)
    text = page.evaluate("() => document.body.innerText")
    grab = lambda pat: (m.group(1) if (m := re.search(pat, text)) else "?")
    return {
        "wpm": grab(r"\bwpm\s*\n\s*(\d+)"),
        "raw": grab(r"\braw\s*\n\s*(\d+)"),
        "acc": grab(r"\bacc\s*\n\s*(\d+%)"),
        "consistency": grab(r"consistency\s*\n\s*(\d+%)"),
    }


# ---------------------------------------------------------------- typist

class Typist:
    def __init__(self, page, wpm, raw, acc, consistency, warmup, fatigue, est_seconds):
        self.page = page
        self.targets = dict(wpm=wpm, raw=raw, acc=acc, consistency=consistency)
        self.cps = wpm * 5 / 60                        # counted chars / sec wanted
        self.wrong_ratio = max(0.0, raw / wpm - 1)     # raw-only chars per wpm char
        self.err_rate = max(0.0, 1 - acc / 100)        # incorrect keypress share
        self.r0 = 1 + 3 * self.err_rate + self.wrong_ratio
        # Per-key jitter already adds ~0.45/sqrt(keys per sec) of per-second variation,
        # so only add the remainder through the speed profile.
        cov = consistency_to_cov(consistency)
        noise2 = 0.45 ** 2 / (self.cps * self.r0)
        self.consistency_capped = cov > 0 and cov ** 2 < noise2
        cov = math.sqrt(max(0.0, cov ** 2 - noise2)) if cov > 0 else 0.0
        self.prof = speed_profile(int(est_seconds) + 5, cov, warmup, fatigue)
        self.cum = [0.0]
        for m in self.prof:
            self.cum.append(self.cum[-1] + m)
        self.t0 = None
        self.next_t = 0.0
        self.keys = 0           # keystrokes sent
        self.backspaces = 0
        self.typos = 0          # incorrect keystrokes
        self.left_wrong = 0     # words left with an error
        self.committed = 0      # chars counted toward wpm
        self.wrong_chars = 0    # chars only counted toward raw
        self.in_word = 0        # provisional progress in current word
        self.err_budget = 0.0
        self.per_sec: dict[int, int] = {}
        self.progress = 0.0     # 0..1, set by the runner
        self.current = ""       # word being typed (for the live ticker)
        self.recent: list[list] = []  # last few [word, ok]

    # -- live stats
    def elapsed(self) -> float:
        return 0.0 if self.t0 is None else time.perf_counter() - self.t0

    def stats(self) -> dict:
        el = self.elapsed()
        typed = self.keys - self.backspaces
        cur = int(el)
        return {
            "elapsed": el,
            "wpm": self.committed * 12 / el if el > 1 else None,
            "raw": (self.committed + self.wrong_chars) * 12 / el if el > 1 else None,
            "acc": 100 * (typed - self.typos) / typed if typed else None,
            "keys": self.keys, "typos": self.typos, "left_wrong": self.left_wrong,
            "history": [self.per_sec.get(s, 0) * 12 for s in range(cur)],
            "progress": self.progress,
            "current": self.current, "in_word": self.in_word, "recent": self.recent[-12:],
        }

    def _finish_word(self, word: str, ok: bool) -> None:
        self.recent = (self.recent + [[word, ok]])[-12:]
        self.current = ""

    # -- pacing
    def _expected(self, t: float) -> float:
        s = int(t)
        if s >= len(self.prof):
            return self.cps * (self.cum[-1] + (t - len(self.prof)) * self.prof[-1])
        return self.cps * (self.cum[s] + (t - s) * self.prof[s])

    def _interval(self) -> float:
        if self.t0 is None:
            return 1 / (self.cps * self.r0)
        t = time.perf_counter() - self.t0
        m = self.prof[min(int(t), len(self.prof) - 1)]
        done = self.committed + self.in_word
        r = (self.keys + 20 * self.r0) / (done + 20)       # keys per counted char
        fb = 1 + (self._expected(t) - done) / (self.cps * 3)
        fb = min(1.35, max(0.7, fb))
        return 1 / max(0.5, self.cps * r * m * fb)

    def _send(self, key: str, weight: float = 1.0) -> None:
        now = time.perf_counter()
        if self.t0 is None:
            self.t0 = self.next_t = now
        if self.next_t > now:
            time.sleep(self.next_t - now)
        if len(key) == 1:
            self.page.keyboard.type(key)
        else:
            self.page.keyboard.press(key)
        self.keys += 1
        if key == "Backspace":
            self.backspaces += 1
        else:
            sec = int(time.perf_counter() - self.t0)
            self.per_sec[sec] = self.per_sec.get(sec, 0) + 1
        base = self._interval() * weight
        self.next_t += max(base * 0.25, random.gauss(base, base * 0.18))

    # -- decisions
    def _want_typo(self) -> bool:
        self.err_budget += self.err_rate * 1.15
        if self.err_budget >= 1 and random.random() < 0.6:
            self.err_budget -= 1
            return True
        return False

    def _leave_word_wrong(self, word: str) -> bool:
        if self.wrong_ratio <= 0 or len(word) < 2:
            return False
        room = self.wrong_ratio * (self.committed + len(word) + 1) - self.wrong_chars
        return room >= len(word) + 1 and random.random() < 0.5

    # -- typing
    def type_word(self, word: str) -> None:
        self.in_word = 0
        self.current = word
        if self._leave_word_wrong(word):
            bad = random.randrange(len(word))
            for i, ch in enumerate(word):
                self._send(neighbor(ch) if i == bad else ch)
            self.err_budget -= 1
            self.typos += 1
            self.left_wrong += 1
            self._send("Space")
            self.wrong_chars += len(word) + 1
            self._finish_word(word, False)
            return

        i = 0
        while i < len(word):
            if self._want_typo():
                self._send(neighbor(word[i]))
                self.typos += 1
                lag = min(random.choice([0, 0, 1, 1, 2]), len(word) - i - 1)
                for j in range(1, lag + 1):
                    self._send(word[i + j])
                self.next_t += self._interval() * random.uniform(1.5, 3.0)  # "oops" pause
                for _ in range(lag + 1):
                    self._send("Backspace", 0.7)
            self._send(word[i])
            i += 1
            self.in_word = i
        self._send("Space")
        self.committed += len(word) + 1
        self.in_word = 0
        self._finish_word(word, True)


def run_test(page, cfg: dict, rep: Reporter, stop=None) -> dict | None:
    wpm = pick(cfg["wpm"])
    raw = max(wpm, float(cfg["raw"]) if cfg.get("raw") else wpm + float(cfg.get("raw_gap", 0)))
    kind, _, amount = cfg["mode"].partition(" ")
    text = page.evaluate(TEXT_STATS_JS)
    cps = wpm * 5 / 60
    if kind == "time":
        est = float(amount or 30)
    elif kind == "words":
        n = float(amount or 25)
        est = max(n * 6, text["chars"] if text["words"] >= n else 0) / cps
    else:  # quote: the whole quote is on the page
        est = text["chars"] / cps
    total_words = float(amount) if kind == "words" and amount else text["words"]

    t = Typist(page, wpm, raw, float(cfg["acc"]), float(cfg["consistency"]),
               float(cfg["warmup"]), float(cfg["fatigue"]), est)
    rep.emit("target", wpm=wpm, raw=raw, acc=float(cfg["acc"]),
             consistency=float(cfg["consistency"]), mode=cfg["mode"])
    if t.consistency_capped:
        rep.emit("log", msg=f"[!] {cfg['consistency']}% consistency is above what this speed "
                            "naturally allows; it will land a bit lower")

    label = cfg["mode"] + (f" · {cfg['language']}" if cfg.get("language") else "")
    rep.start_live(t, label)
    page.focus("#wordsInput")
    last_idx, stall, last_emit = None, time.perf_counter(), 0.0
    try:
        while True:
            if stop is not None and stop.is_set():
                return None
            st = page.evaluate(STATE_JS)
            if st["done"]:
                break
            if kind == "time":
                t.progress = min(1.0, t.elapsed() / est)
            elif st["idx"] is not None and total_words:
                t.progress = min(1.0, st["idx"] / total_words)
            if rep.ui == "json" and time.perf_counter() - last_emit > 0.1:
                s = t.stats()
                rep.emit("progress", **{k: s[k] for k in (
                    "elapsed", "wpm", "raw", "acc", "keys", "typos", "left_wrong", "history",
                    "progress", "current", "in_word", "recent")})
                last_emit = time.perf_counter()
            if st["word"] is None or st["idx"] == last_idx:
                if time.perf_counter() - stall > 2.0:
                    page.keyboard.press("Space")
                    stall = time.perf_counter()
                time.sleep(0.005)
                continue
            last_idx = st["idx"]
            t.type_word(st["word"])
            stall = time.perf_counter()
        t.progress = 1.0
    finally:
        rep.stop_live()

    res = read_results(page)
    rep.emit("result", **res, target=dict(wpm=round(wpm), raw=round(raw), acc=cfg["acc"],
                                          consistency=cfg["consistency"]))
    return res


# ---------------------------------------------------------------- cli

def menu(cfg: dict) -> dict:
    print("Press Enter to keep the value in [brackets].")
    for key, label in [("wpm", "WPM (e.g. 150 or 140-160)"), ("raw", "raw WPM (blank = wpm + gap)"),
                       ("acc", "accuracy %"), ("consistency", "consistency %"),
                       ("warmup", "warm-up seconds"), ("fatigue", "fatigue % slowdown"),
                       ("mode", "mode (time 30, words 25, quote short/medium/long/all)"),
                       ("language", "language (english, spanish, 'english 1k', ...)"),
                       ("punctuation", "punctuation y/n"), ("numbers", "numbers y/n"),
                       ("loops", "number of tests")]:
        cur = cfg.get(key)
        cur = ("y" if cur else "n") if isinstance(cur, bool) else (cur or "")
        v = input(f"  {label} [{cur}]: ").strip()
        if v:
            cfg[key] = v.lower().startswith("y") if key in ("punctuation", "numbers") else v
    return cfg


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preset", choices=PRESETS, default="fast")
    ap.add_argument("--wpm")
    ap.add_argument("--raw", help="target raw wpm (default: wpm + preset gap)")
    ap.add_argument("--raw-gap", type=float, help="raw = wpm + this (alternative to --raw)")
    ap.add_argument("--acc", type=float)
    ap.add_argument("--consistency", type=float)
    ap.add_argument("--warmup", type=float)
    ap.add_argument("--fatigue", type=float)
    ap.add_argument("--mode", default="time 30")
    ap.add_argument("--language", default="english")
    ap.add_argument("--punctuation", action="store_true")
    ap.add_argument("--numbers", action="store_true")
    ap.add_argument("--loops", type=int, default=1)
    ap.add_argument("--menu", action="store_true", help="choose settings interactively")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--ui", choices=["rich", "plain", "json"],
                    default="rich" if sys.stdout.isatty() else "plain")
    ap.add_argument("--serve", action="store_true",
                    help="(used by panel.py) keep one browser open and take runs as JSON on stdin")
    args = ap.parse_args()
    if args.serve:
        return serve()

    cfg = dict(PRESETS[args.preset], raw=None, mode=args.mode, loops=args.loops,
               language=args.language, punctuation=args.punctuation, numbers=args.numbers)
    for k in ("wpm", "raw", "raw_gap", "acc", "consistency", "warmup", "fatigue"):
        if getattr(args, k) is not None:
            cfg[k] = getattr(args, k)
    if args.menu:
        cfg = menu(cfg)
    rep = Reporter(args.ui)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        # Fresh context every run: no cookies, never signed in -> guest only.
        page = browser.new_context(viewport={"width": 1280, "height": 800}).new_page()
        open_monkeytype(page)
        configure(page, cfg, fresh_page_state(), rep)

        loops = int(cfg["loops"])
        for i in range(loops):
            if i:
                new_test(page)
            rep.emit("log", msg=f"--- test {i + 1}/{loops} ({cfg['mode']})")
            run_test(page, cfg, rep)

        rep.emit("done")
        if not args.headless and sys.stdin.isatty() and args.ui != "json":
            input("Press Enter to close the browser...")
        browser.close()


def serve() -> None:
    """Long-lived worker for panel.py.

    Keeps ONE Monkeytype window open between runs. Reads JSON lines on stdin:
      {"cmd": "run", "cfg": {...}}   start tests in the open window
      {"cmd": "stop"}                stop typing (window stays open)
      {"cmd": "quit"}                close the browser and exit
    """
    import queue
    import threading

    rep = Reporter("json")
    cmds: "queue.Queue[dict]" = queue.Queue()
    stop = threading.Event()

    def reader() -> None:
        for line in sys.stdin:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("cmd") == "stop":
                stop.set()
            else:
                cmds.put(msg)
        cmds.put({"cmd": "quit"})

    threading.Thread(target=reader, daemon=True).start()

    with sync_playwright() as p:
        browser = page = None
        cur: dict = {}
        dirty = False  # page shows a finished/abandoned test

        def window_alive() -> bool:
            try:
                return browser is not None and browser.is_connected() and not page.is_closed()
            except Exception:
                return False

        rep.emit("ready")
        while True:
            msg = cmds.get()
            if msg.get("cmd") == "quit":
                break
            if msg.get("cmd") != "run":
                continue
            cfg = dict(PRESETS["fast"], raw=None, mode="time 30", loops=1, language="english")
            cfg.update({k: v for k, v in msg.get("cfg", {}).items() if v is not None})
            headless = bool(cfg.get("headless"))
            stop.clear()
            rep.emit("busy")
            try:
                if not window_alive() or cur.get("headless") != headless:
                    if browser is not None:
                        try:
                            browser.close()
                        except Exception:
                            pass
                    rep.emit("log", msg="opening monkeytype...")
                    browser = p.chromium.launch(headless=headless)
                    # Fresh context: no cookies, never signed in -> guest only.
                    page = browser.new_context(viewport={"width": 1280, "height": 800}).new_page()
                    open_monkeytype(page)
                    cur = dict(fresh_page_state(), headless=headless)
                    dirty = False
                if dirty:
                    new_test(page)
                configure(page, cfg, cur, rep)

                loops = int(cfg["loops"])
                for i in range(loops):
                    if stop.is_set():
                        break
                    if i:
                        new_test(page)
                    rep.emit("log", msg=f"--- test {i + 1}/{loops} ({cfg['mode']})")
                    dirty = True
                    run_test(page, cfg, rep, stop)
                if stop.is_set():
                    new_test(page)  # leave a clean, ready test on screen
                    dirty = False
                    rep.emit("stopped")
                else:
                    rep.emit("done")
            except Exception as e:
                if not window_alive():
                    rep.emit("log", msg="[!] browser window was closed")
                else:
                    rep.emit("log", msg=f"[!] {type(e).__name__}: {str(e).splitlines()[0][:160]}")
                rep.emit("done")

        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
