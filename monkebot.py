#!/usr/bin/env python3
"""Monkeytype typing bot.

Drives a real Chromium via Playwright, reads the active word from the page and
types it with real (trusted) key events at a target WPM with human-ish jitter.

Usage:
    .venv/bin/python monkebot.py --wpm 180
    .venv/bin/python monkebot.py --wpm 250 --jitter 0.35 --mode "time 30"
"""
import argparse
import random
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE_DIR = Path(__file__).parent / ".browser-profile"  # keeps login/settings


def char_delay(wpm: float) -> float:
    # Standard WPM = (chars / 5) per minute, spaces included.
    return 60.0 / (wpm * 5)


def dismiss_cookies(page) -> None:
    btn = page.get_by_role("button", name="reject non-essential")
    try:
        btn.click(timeout=3000)
    except Exception:
        pass


def set_mode(page, mode: str | None) -> None:
    """mode like 'time 30', 'words 25'. Clicks the test-config buttons."""
    if not mode:
        return
    kind, _, amount = mode.partition(" ")

    def click_visible(text: str) -> None:
        page.locator("button:visible").filter(
            has_text=re.compile(rf"^\s*{text}\s*$")
        ).first.click(timeout=2000)

    try:
        click_visible(kind)
        if amount:
            time.sleep(0.2)
            click_visible(amount)
        page.wait_for_selector("#words .word.active")
    except Exception:
        print(f"[!] couldn't set mode '{mode}', using whatever is selected")


STATE_JS = """() => {
    const ws = document.querySelector('#words');
    const r = document.querySelector('#result');
    const done = !ws || ws.offsetParent === null ||
        (!!r && !r.classList.contains('hidden') && r.offsetParent !== null);
    const w = ws?.querySelector('.word.active');
    if (done || !w) return {done, idx: null, word: null};
    // Read word + index in ONE call so they can never be out of sync.
    const word = [...w.querySelectorAll('letter')]
        .filter(l => !l.classList.contains('extra'))
        .map(l => l.dataset.char ?? l.textContent).join('');
    return {done, idx: w.dataset.wordindex, word};
}"""


def page_state(page) -> dict:
    return page.evaluate(STATE_JS)


def run_test(page, wpm: float, jitter: float) -> None:
    delay = char_delay(wpm)
    page.focus("#wordsInput")  # focus the hidden input
    last_idx = None
    # Schedule keystrokes on an absolute clock so per-call overhead doesn't drag WPM down.
    next_t = time.perf_counter()

    def key(k: str, press: bool = False) -> None:
        nonlocal next_t
        wait = next_t - time.perf_counter()
        if wait > 0:
            time.sleep(wait)
        page.keyboard.press(k) if press else page.keyboard.type(k)
        next_t += max(delay * 0.2, random.gauss(delay, delay * jitter))

    stall_since = time.perf_counter()
    while True:
        st = page_state(page)
        if st["done"]:
            break
        if st["word"] is None or st["idx"] == last_idx:
            # DOM hasn't advanced yet. If it's stuck (e.g. last word needs a space), nudge it.
            if time.perf_counter() - stall_since > 2.0:
                page.keyboard.press("Space")
                stall_since = time.perf_counter()
            time.sleep(0.005)
            continue
        last_idx = st["idx"]
        for ch in st["word"]:
            key(ch)
        key("Space", press=True)
        stall_since = time.perf_counter()
    time.sleep(1.0)  # let the result screen render
    text = page.evaluate("() => document.body.innerText")
    m_wpm = re.search(r"wpm\s*\n\s*(\d+)", text)
    m_acc = re.search(r"acc\s*\n\s*(\d+%)", text)
    print(f"[+] done  wpm={m_wpm and m_wpm.group(1)}  acc={m_acc and m_acc.group(1)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wpm", type=float, default=150, help="target words per minute")
    ap.add_argument("--jitter", type=float, default=0.3, help="per-key timing noise (fraction)")
    ap.add_argument("--mode", default=None, help="e.g. 'time 15', 'time 30', 'words 25'")
    ap.add_argument("--loops", type=int, default=1, help="how many tests to run")
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR, headless=args.headless, viewport={"width": 1280, "height": 800}
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://monkeytype.com")
        page.wait_for_selector("#words .word.active")
        dismiss_cookies(page)
        set_mode(page, args.mode)

        for i in range(args.loops):
            if i:
                page.reload()  # fresh test; mode is remembered by the site
                page.wait_for_selector("#words .word.active")
                time.sleep(0.5)
            print(f"[*] test {i + 1}/{args.loops} @ {args.wpm} wpm")
            run_test(page, args.wpm, args.jitter)
            time.sleep(1)

        if not args.headless:
            input("Press Enter to close the browser...")
        ctx.close()


if __name__ == "__main__":
    main()
