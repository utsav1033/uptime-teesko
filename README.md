# UPTIME TEESKO — build spec

One terminal command, `uptime-teesko`, that shows whether every model and service
Almanac depends on is up right now. Nothing runs unless you run it. This file is the
whole spec: hand it to a person or an agent and the result should match.

```
             ██   ▀▀                    ██                     ▄▄
██ ██ ████▄ ▀██▀▀ ██  ███▄███▄ ▄█▀█▄   ▀██▀▀ ▄█▀█▄ ▄█▀█▄ ▄█▀▀▀ ██ ▄█▀ ▄███▄
██ ██ ██ ██  ██   ██  ██ ██ ██ ██▄█▀    ██   ██▄█▀ ██▄█▀ ▀███▄ ████   ██ ██
▀██▀█ ████▀  ██   ██▄ ██ ██ ██ ▀█▄▄▄    ██   ▀█▄▄▄ ▀█▄▄▄ ▄▄▄█▀ ██ ▀█▄ ▀███▀
      ██
      ▀▀
```

## 1. Constraints (non-negotiable)

- Python 3, standard library only. One file: `uptime_teesko.py` at the root of this repo. No pip, no build.
- Launcher: `~/.local/bin/uptime-teesko` → `exec python3 <that file> "$@"`.
- Keys come from `.env` beside the script, else the almaclaw worktree's `.env` (`LITELLM_API_KEY`, `CARTESIA_API_KEY`, `SERPER_API_KEY`, `EXA_API_KEY`). Never printed, never logged; key fragments in provider error text are redacted.
- Budget: a full pass ≤ 200 tokens. Every chat probe is `max_tokens: 1`, prompt `ok`. No background process, no cron, no server. Watch mode re-probes only rows that are DOWN.
- Works in an 80-column terminal without wrapping the banner; degrades to no colour and no animation when stdout is not a TTY.

## 2. What the screen shows

```
UPTIME TEESKO   all 10 probed rows up   checked 16:52:26 · pass ≈ 70 tokens · key 100 rpm · spent $120.11

STATUS MODEL                    FEATURE                            RESULT           MS   TTFT  LIMIT
● 200  cerebras/qwen-3.8-27b    background decider • done-talking  WORKING        1252   1251
                                check • shortcut chooser (HOT)
● 200  gemma-4-31b              default model                      WORKING        2194   2194  27/30 rpm
● 200  gpt-5.6-luna             background workers • spoken reply  WORKING        1810   1777  3996/3997 rpm
● 200  cerebras/gpt-oss-120b    keeps chats short                  WORKING        1321   1300
● 200  gpt-5.6-sol-fast         screen reading • notes • diagrams  WORKING        3270   3231  649/650 rpm
● 200  gpt-5.6-sol              stalk judge                        WORKING        3977   3439  4016/4017 rpm
● 200  google/gemma-4-31b-it    backup shortcut chooser            WORKING        2187   2187  28/30 rpm
● 200  litellm key              LiteLLM API key • every model      WORKING        1174
                                call • DMG bakes its own copy (HOT)
● 200  media-litellm relay      STT • TTS • DMG app • same key     WORKING         291
                                (HOT)
● 200  cartesia key             Cartesia STT key • dev stack (HOT) WORKING          78
○ skip serper                   web search • stalk • compare       SKIPPED
                                                                   ↳ use --all to probe
○ skip exa                      article fetch • save • visualise   SKIPPED
                                                                   ↳ use --all to probe

r = re-check all · w = watch DOWN rows · q = quit
```

A DOWN row:

```
● 429  gemma-4-31b              shortcut chooser (HOT)                   NOT WORKING
                                                                         ↳ provider behind it is failing (all deployments in cooldown) —
                                                                         add a fallback route on the proxy; until then every turn that needs it dies
                                                                         ↳ No deployments available for selected model … cooldown_list=[dc75cf48…]
```

### Columns

| Column | Content |
|---|---|
| STATUS | First column, 6 wide. `●` + HTTP code, coloured (see §4); `off` for unreachable, `t/o` for timeout. `○ skip` for rows deliberately not probed. |
| MODEL | The id exactly as the proxy knows it. Non-model rows use plain names (`litellm key`, `media-litellm relay`, `cartesia key`). Bold. |
| FEATURE | Short phrases, no sentences, joined with ` • `. Rows every turn touches carry `(HOT)`. |
| RESULT | One word: `WORKING`, `NOT WORKING`, `RATE LIMITED`, `SKIPPED`. Under a row that is not working, dimmed: `↳` + the fix sentence from §5, then `↳` + the provider's raw error line, so the terminal is the log. |
| MS | Round-trip latency of the probe from this Mac, milliseconds. |
| TTFT | Time to first streamed chunk, milliseconds. Chat probes stream so this is when the model started answering; blank for non-chat rows. |
| LIMIT | Remaining/limit requests per minute for that model on the proxy, from the `x-ratelimit-*` headers; blank when the provider sends none. |

The summary line adds the key-level view from the same headers: `key 100 rpm · spent $120.11`. `--json` carries everything the proxy said per row: `ms`, `ttft`, `upstream_ms` (provider time as measured by the proxy), `rpm_left/rpm_limit`, `tpm_left/tpm_limit`, `provider` host, `tokens`.

Wrap FEATURE and the dimmed notes to their column width; never truncate.

## 3. Rows (where they come from)

- **From `~/.openclaw/openclaw.json`, at run time:** every `model`/`primary` under `agents.*` except the voice models. Layer = nearest ancestor key among `actionGate, waitGate, decider, compaction, narrator, subagents, model`. FEATURE per layer:
  - actionGate → "shortcut chooser" (HOT)
  - waitGate → "done-talking check" (HOT)
  - decider → "background decider" (HOT)
  - compaction → "keeps chats short"
  - narrator → "spoken reply"
  - subagents → "background workers"
  - model → "default model"
  A model used by several layers is one row; phrases joined with ` • `. Adding a model to a gate makes it appear here with no code change.
- **Connector writers** (constants in connector sources, listed in the script): `gpt-5.6-sol-fast` — "screen reading • notes • diagrams"; `gpt-5.6-sol` — "stalk judge".
- **Fallback route:** `google/gemma-4-31b-it` — "backup shortcut chooser". Shown so the escape hatch is visibly alive.
- **litellm key** (HOT): the LiteLLM API key; every model call uses it and the DMG bakes its own copy. `GET https://litellm.memfold.ai/v1/models` with the bearer key. This row catches a revoked release key.
- **media-litellm relay** (HOT): STT and TTS for the DMG app, via media-litellm.memfold.ai on that same key. `GET https://media-litellm.memfold.ai/v1/models` with the bearer key; 404/405 counts as up (the host answered).
- **cartesia key** (HOT): the Cartesia STT key the dev stack uses. `GET https://api.cartesia.ai/stt/turns/websocket` with `X-API-Key` + `Cartesia-Version: 2025-04-16`; 400/426 counts as up (needs a websocket), 401 = key invalid.
- **serper, exa**: `○ skip` by default (per-call pricing). `--all` probes them with one minimal search each.
- **Not rows:** `claude-opus-4-8`, `claude-haiku-4-5` (Claude desktop picker names, not proxy models); `ink-2-latest`, `sonic-3.5` (covered by the relay and cartesia rows).

Chat probe: `POST https://litellm.memfold.ai/v1/chat/completions` body `{"model": <id>, "max_tokens": 1, "stream": true, "stream_options": {"include_usage": true}, "messages": [{"role": "user", "content": "ok"}]}`, 15 s timeout. Streaming is what makes TTFT measurable; the token budget is unchanged. All probes run in parallel (thread pool, 8 workers); a pass takes as long as the slowest row.

## 4. Colours (ANSI 256, with 16-colour fallback)

| Use | Code |
|---|---|
| Banner, title | light blue `38;5;117` (fallback `94`) |
| UP | green `38;5;114` (fallback `32`) |
| WARN (429 rate limit) | yellow `38;5;221` (fallback `33`) |
| DOWN | red `38;5;203` (fallback `31`) |
| skip, raw error, hints | dim `2` |

No colour when stdout is not a TTY (`--once` piped, `--json`).

## 5. Meanings (fixed table; no model involved)

| Result | State | Meaning shown |
|---|---|---|
| 200 | UP | OK |
| 401 | DOWN | litellm key invalid or revoked — rotate `LITELLM_API_KEY` in `.env` and the `ALMANAC_LITELLM_API_KEY` release secret, then rebuild the DMG |
| 401 on cartesia key | DOWN | `CARTESIA_API_KEY` in `.env` is invalid — the dev stack cannot hear you |
| 402 | DOWN | provider credit exhausted — top up that provider's account |
| 403 | DOWN | this key is not allowed to use this model — litellm key permissions |
| 404 | DOWN | model removed or renamed at the provider — repoint the route on the proxy |
| 429 with "No deployments available" | DOWN | provider behind it is failing (all deployments in cooldown) — add a fallback route on the proxy; until then every turn that needs it dies |
| 429 otherwise | WARN | rate limited — wait, or raise the limit on the proxy |
| 5xx | DOWN | provider or proxy error, usually passes in minutes |
| timeout (15 s) | DOWN | no answer in 15 s — provider hung |
| connection error | DOWN | proxy unreachable — network or DNS on this Mac |

Raw error line: the `message` field of the JSON error body, else the first 200 chars of the body, `\n` collapsed.

## 6. Keys and modes

| Input | Behaviour |
|---|---|
| (start) | banner, one full pass, table, then wait for a key |
| `r` | re-check every row now (one full pass) |
| `w` | enter watch mode: every 30 s re-probe only DOWN rows; leave watch mode automatically when none are DOWN |
| `q` or Ctrl-C | quit |
| `--once` | one pass, print, exit. Exit code 1 if any row is DOWN, else 0 (for scripts and the pre-release check) |
| `--json` | one pass as JSON `{checked, rows[]}`, same exit codes |
| `--watch N` | start in watch mode with N seconds |
| `--all` | include serper and exa |

Key reading: raw/cbreak mode via `termios` + `tty`, `select` with a timeout so watch mode ticks without a thread.

## 7. Animation (simple, cheap, all in the terminal)

Every animation is plain ANSI: cursor moves, a few frames, no libraries. All of it is skipped when stdout is not a TTY.

1. **Banner reveal, 0.3 s total.** Print the six banner lines one per 50 ms, top to bottom, in light blue. Then the title line.
2. **Probe spinner, per row.** While a pass runs, each row's STATUS cell shows a spinner cycling `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏` at 80 ms, dim. Rows are drawn immediately with `…` in MEANING; as each probe returns (results arrive out of order), that row's line is repainted in place (`\x1b[<n>A` up, `\x1b[2K` clear, reprint, `\x1b[<n>B` down). The summary line updates last.
3. **State change flash.** When a row's state differs from the previous pass, its STATUS cell blinks twice (on 120 ms, off 120 ms) in the new colour, then stays. Nothing flashes on the first pass.
4. **Watch countdown.** In watch mode the footer shows `watching 2 DOWN rows · next check in 30s`, the number counting down once a second (repaint the footer line only). At zero, spinners appear on the DOWN rows only.
5. **Recovery.** When the last DOWN row turns UP in watch mode, the summary line repaints green with `all N probed rows up`, and the footer returns to `r = re-check all · w = watch DOWN rows · q = quit`.
6. **Quit.** Reset colours (`\x1b[0m`), restore the cursor, restore the terminal mode, print nothing else.

Frame budget: never more than one repaint per 80 ms; never repaint the whole screen except after a full pass. Animations must not delay a probe result — the spinner thread only draws, probes run in the pool.

## 8. Acceptance

- `uptime-teesko` shows the banner in light blue and a table with the rows in §3, all UP on a healthy day, in under 5 s.
- Killing the network turns every row red with `proxy unreachable`; restoring it and pressing `r` turns them green with a two-blink flash on each.
- `uptime-teesko --once | cat` prints plain text, no colour codes, no spinner frames, exit 0.
- `uptime-teesko --once` on a day the litellm key is dead: every chat row and the `litellm key` row show `● 401` and the rotate-the-key meaning; exit 1.
- A pass never exceeds 200 tokens; verify with `--json` (each chat row's probe is one output token).
- Ctrl-C at any point leaves the terminal usable (mode restored, cursor visible).
