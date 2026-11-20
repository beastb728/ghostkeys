# ghostkeys

A Playwright bot that types [Monkeytype](https://monkeytype.com) tests with
human-like stats you control: WPM, raw, accuracy, consistency, warm-up and fatigue.
Built to explore how far a scripted typist can go and how closely it can hit target numbers.

It **only runs as a guest**: every run opens a fresh browser that has never been
signed in, so results are never saved to an account or sent to a leaderboard.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

## Control panel

```bash
.venv/bin/python panel.py
```

Open <http://127.0.0.1:8765>. Pick a preset or drag the sliders, choose the test, and
press **Start typing** (or <kbd>Enter</kbd>; <kbd>Esc</kbd> stops). You get live WPM,
raw and accuracy against your targets, the words being typed, and a per-second speed
chart. One Monkeytype window stays open between runs. Every result is appended to
`results.jsonl`.

## Command line

```bash
.venv/bin/python ghostkeys.py --preset fast --mode "time 30"
.venv/bin/python ghostkeys.py --wpm 130-145 --raw-gap 3 --acc 95 --consistency 72 --loops 3
.venv/bin/python ghostkeys.py --mode "quote medium" --language spanish
.venv/bin/python ghostkeys.py --menu            # answer prompts instead of flags
```

In a terminal it shows a live dashboard. Add `--ui plain` for plain text.

| Option | What it does |
|---|---|
| `--wpm` | Target WPM, or a range like `140-160` picked per test |
| `--raw` / `--raw-gap` | Target raw WPM, or raw = WPM + gap. Comes from words left with an error |
| `--acc` | Target accuracy %. Comes from adjacent-key typos that get corrected |
| `--consistency` | Target consistency %. Shapes second-to-second speed swings |
| `--warmup` | Seconds of slower start (80% → 100% speed) |
| `--fatigue` | % slowdown by the end of the test |
| `--preset` | `casual`, `average`, `fast`, `pro`, `robot` |
| `--mode` | `time 15/30/60/120`, `words 10/25/50/100`, `quote all/short/medium/long/thicc` |
| `--language` | Any Monkeytype language name, e.g. `french`, `english 1k` |
| `--punctuation`, `--numbers` | Extra characters (time and words modes) |
| `--loops` | Number of tests in a row |
| `--headless` | Don't show the browser window |

## How it works

- **Typing:** reads the active word and its index from the page in one call, and sends
  real keyboard events through Playwright.
- **WPM:** keystrokes are scheduled on a clock and a feedback loop keeps the counted
  characters on track for the target, so typo overhead doesn't drag the score down.
- **Consistency:** Monkeytype's formula is inverted to get the speed variation needed, and
  a per-second speed profile (with warm-up and fatigue) is scaled to exactly that, minus
  the variation per-key timing jitter already adds.
- **Accuracy and raw:** an error budget decides when to make a corrected typo; raw above
  WPM comes from occasionally leaving a word wrong.

Results usually land within about 5% of target on 30s+ tests. Very short tests swing more.
