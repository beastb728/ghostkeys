#!/usr/bin/env python3
"""Monkeytype typing bot with human-like stat controls.

Runs in a fresh, never-signed-in (guest) browser every time, so results are
never saved to an account or submitted anywhere.

Knobs:
  --wpm          target wpm (number or range like 140-160, picked per test)
  --raw          target raw wpm (>= wpm). Gap comes from words left with errors.
  --acc          target accuracy %. Comes from typos that get corrected.
  --consistency  target consistency %. Shapes second-to-second speed swings.
  --warmup       seconds of slower start
  --fatigue      % speed drop by the end of the test
  --preset       casual | average | fast | pro | robot
  --menu         ask for every setting interactively

Examples:
  .venv/bin/python monkebot.py --preset fast --mode "time 30"
  .venv/bin/python monkebot.py --wpm 130-145 --raw 150 --acc 95 --consistency 72 --loops 3
  .venv/bin/python monkebot.py --menu
"""
import argparse
import math
import random
import re
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
    return {done, idx: w.dataset.wordindex, word};
}"""


def dismiss_cookies(page) -> None:
    try:
        page.get_by_role("button", name="reject non-essential").click(timeout=3000)
    except Exception:
        pass


def set_mode(page, mode: str | None) -> None:
    if not mode:
        return
    kind, _, amount = mode.partition(" ")

    def click_visible(text: str) -> None:
        page.locator("button:visible").filter(
            has_text=re.compile(rf"^\s*{re.escape(text)}\s*$")
        ).first.click(timeout=2000)

    try:
        click_visible(kind)
        if amount:
            time.sleep(0.2)
            click_visible(amount)
        page.wait_for_selector("#words .word.active")
    except Exception:
        print(f"[!] couldn't set mode '{mode}', using whatever is selected")


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
        self.cps = wpm * 5 / 60                        # counted chars / sec wanted
        self.wrong_ratio = max(0.0, raw / wpm - 1)     # raw-only chars per wpm char
        self.err_rate = max(0.0, 1 - acc / 100)        # incorrect keypress share
        self.r0 = 1 + 3 * self.err_rate + self.wrong_ratio
        # Per-key jitter already adds ~0.45/sqrt(keys per sec) of per-second variation,
        # so only add the remainder through the speed profile.
        cov = consistency_to_cov(consistency)
        noise2 = 0.45 ** 2 / (self.cps * self.r0)
        if cov > 0 and cov ** 2 < noise2:
            print(f"[!] consistency {consistency}% is above what this speed naturally allows; "
                  "it will land a bit lower")
        cov = math.sqrt(max(0.0, cov ** 2 - noise2)) if cov > 0 else 0.0
        self.prof = speed_profile(int(est_seconds) + 5, cov, warmup, fatigue)
        self.cum = [0.0]
        for m in self.prof:
            self.cum.append(self.cum[-1] + m)
        self.t0 = None
        self.next_t = 0.0
        self.keys = 0           # keystrokes sent
        self.committed = 0      # chars counted toward wpm
        self.wrong_chars = 0    # chars only counted toward raw
        self.in_word = 0        # provisional progress in current word
        self.err_budget = 0.0

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
        if self._leave_word_wrong(word):
            bad = random.randrange(len(word))
            for i, ch in enumerate(word):
                self._send(neighbor(ch) if i == bad else ch)
            self.err_budget -= 1
            self._send("Space")
            self.wrong_chars += len(word) + 1
            return

        i = 0
        while i < len(word):
            if self._want_typo():
                self._send(neighbor(word[i]))
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


def run_test(page, cfg: dict, mode: str | None) -> dict:
    wpm = pick(cfg["wpm"])
    raw = max(wpm, float(cfg["raw"]) if cfg.get("raw") else wpm + float(cfg.get("raw_gap", 0)))
    kind, _, amount = (mode or "time 30").partition(" ")
    amount = float(amount or 30)
    est = amount if kind == "time" else amount * 6 / (wpm * 5 / 60)
    t = Typist(page, wpm, raw, float(cfg["acc"]), float(cfg["consistency"]),
               float(cfg["warmup"]), float(cfg["fatigue"]), est)
    print(f"[*] target  wpm={wpm:.0f}  raw={raw:.0f}  acc={cfg['acc']}%  "
          f"consistency={cfg['consistency']}%")

    page.focus("#wordsInput")
    last_idx, stall = None, time.perf_counter()
    while True:
        st = page.evaluate(STATE_JS)
        if st["done"]:
            break
        if st["word"] is None or st["idx"] == last_idx:
            if time.perf_counter() - stall > 2.0:
                page.keyboard.press("Space")
                stall = time.perf_counter()
            time.sleep(0.005)
            continue
        last_idx = st["idx"]
        t.type_word(st["word"])
        stall = time.perf_counter()

    res = read_results(page)
    print(f"[+] result  wpm={res['wpm']}  raw={res['raw']}  acc={res['acc']}  "
          f"consistency={res['consistency']}")
    return res


# ---------------------------------------------------------------- cli

def menu(cfg: dict) -> dict:
    print("Press Enter to keep the value in [brackets].")
    for key, label in [("wpm", "WPM (e.g. 150 or 140-160)"), ("raw", "raw WPM (blank = wpm + gap)"),
                       ("acc", "accuracy %"), ("consistency", "consistency %"),
                       ("warmup", "warm-up seconds"), ("fatigue", "fatigue % slowdown"),
                       ("mode", "mode (time 15/30/60, words 10/25/50)"), ("loops", "number of tests")]:
        v = input(f"  {label} [{cfg.get(key) or ''}]: ").strip()
        if v:
            cfg[key] = v
    return cfg


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preset", choices=PRESETS, default="fast")
    ap.add_argument("--wpm")
    ap.add_argument("--raw", help="target raw wpm (default: wpm + preset gap)")
    ap.add_argument("--acc", type=float)
    ap.add_argument("--consistency", type=float)
    ap.add_argument("--warmup", type=float)
    ap.add_argument("--fatigue", type=float)
    ap.add_argument("--mode", default="time 30", help="'time 15', 'time 30', 'words 25', ...")
    ap.add_argument("--loops", type=int, default=1)
    ap.add_argument("--menu", action="store_true", help="choose settings interactively")
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    cfg = dict(PRESETS[args.preset], raw=None, mode=args.mode, loops=args.loops)
    for k in ("wpm", "raw", "acc", "consistency", "warmup", "fatigue"):
        if getattr(args, k) is not None:
            cfg[k] = getattr(args, k)
    if args.menu:
        cfg = menu(cfg)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        # Fresh context every run: no cookies, never signed in -> guest only.
        page = browser.new_context(viewport={"width": 1280, "height": 800}).new_page()
        page.goto("https://monkeytype.com")
        page.wait_for_selector("#words .word.active")
        dismiss_cookies(page)
        set_mode(page, cfg["mode"])

        for i in range(int(cfg["loops"])):
            if i:
                page.reload()
                page.wait_for_selector("#words .word.active")
                time.sleep(0.5)
            print(f"--- test {i + 1}/{cfg['loops']} ({cfg['mode']})")
            run_test(page, cfg, cfg["mode"])

        if not args.headless:
            input("Press Enter to close the browser...")
        browser.close()


if __name__ == "__main__":
    main()
