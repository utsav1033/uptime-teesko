# Prompt: build UPTIME TEESKO

Paste everything below this line into the agent, from inside the `uptime-teesko` repo.

---

You are working in the `uptime-teesko` repo. Build the terminal tool described in `README.md`, which is the complete spec. Read it fully before writing anything; every section is binding, especially §1 (constraints), §5 (meanings table) and §7 (animation).

**Start from `uptime_teesko.py`.** It already does everything in the spec except §7 (animation). Keep its structure: `rows()` reads `~/.openclaw/openclaw.json` for the gate models, `probe()` does one request per row in a thread pool, `render()` draws the table, `main()` handles keys and flags. Do not rewrite it from scratch; add to it.

**Your job, in order:**

1. Implement §7 exactly:
   - Banner reveal: six lines, one per 50 ms, light blue.
   - Per-row braille spinner (`⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`, 80 ms) in the STATUS cell while that row's probe is in flight; rows repaint in place as results arrive, out of order. Use cursor-up / clear-line / cursor-down; never redraw the whole screen mid-pass.
   - Two-blink flash (120 ms on/off) on a row whose state changed since the last pass; nothing flashes on the first pass.
   - Watch-mode footer with a once-per-second countdown; at zero, spinners on DOWN rows only.
   - Clean quit: reset colours, restore cursor and terminal mode.
   - The spinner is drawn by one daemon thread; probes run in the pool. Animation must never delay a result. Skip all animation when stdout is not a TTY.
2. Keep the budget: chat probes stay `max_tokens: 1`, prompt `ok`; watch mode re-probes only DOWN rows; verify with `--json` that a pass is ≤ 200 tokens.
3. Run every check in §8 (acceptance) and paste the real output of each into your final message. If one fails, fix it, don't argue with it.
4. Keep it one file, Python 3 standard library only. No new files except updating `README.md` if you find the spec wrong in some detail, in which case say what you changed and why.

**Do not:** add a server, a cron, a window, Slack, or any dependency. Do not print or log any key. Do not touch `~/.openclaw/openclaw.json` or `.env`.

**Done means:** `./install.sh && uptime-teesko` shows the blue banner revealing line by line, spinners on every row for a couple of seconds, then a table that is all green on a healthy day; `uptime-teesko --once | cat` is plain text with exit 0; Ctrl-C leaves the terminal usable.
