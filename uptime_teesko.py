#!/usr/bin/env python3
"""UPTIME TEESKO — is every model and service Almanac depends on up right now?

    uptime-teesko              one pass, then keys: r re-check · w watch DOWN rows · q quit
    uptime-teesko --once       one pass, print, exit (scripts, pre-release check)
    uptime-teesko --watch 30   start in watch mode (re-probe only DOWN rows every 30 s)
    uptime-teesko --all        also probe the per-call-priced providers (Serper, Exa)
    uptime-teesko --json       machine-readable one pass

Rows come from ~/.openclaw/openclaw.json (gates, narrator, subagents) plus the connector
writers, the LiteLLM key, the media-litellm relay, and the Cartesia key. Probes are one-token completions:
a full pass is ≈135 tokens. Nothing runs unless you run it. Stdlib only. Keys are
read from .env (here, else the almaclaw worktree) and never printed. Spec: README.md.

Animation (README §7) is plain ANSI and only when stdout is a TTY: the banner reveals
line by line, each row shows a braille spinner while its probe is in flight and is
repainted in place as results land, rows whose state changed blink twice, and watch
mode counts down in the footer. One daemon thread draws; probes run in the pool.
"""
from __future__ import annotations

import json
import os
import re
import select
import sys
import termios
import threading
import time
import tty
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
# .env next to the script; fall back to the almaclaw worktree's .env so the same keys work.
ENV_FILE = next((p for p in (HERE / ".env", Path.home() / "Documents/almaclaw-trace/.env") if p.exists()), HERE / ".env")
OPENCLAW_JSON = Path.home() / ".openclaw" / "openclaw.json"
PROXY = "https://litellm.memfold.ai/v1"
MEDIA_RELAY = "https://media-litellm.memfold.ai/v1"
TIMEOUT_S = 15

BANNER = """\
             ██   ▀▀                    ██                     ▄▄
██ ██ ████▄ ▀██▀▀ ██  ███▄███▄ ▄█▀█▄   ▀██▀▀ ▄█▀█▄ ▄█▀█▄ ▄█▀▀▀ ██ ▄█▀ ▄███▄
██ ██ ██ ██  ██   ██  ██ ██ ██ ██▄█▀    ██   ██▄█▀ ██▄█▀ ▀███▄ ████   ██ ██
▀██▀█ ████▀  ██   ██▄ ██ ██ ██ ▀█▄▄▄    ██   ▀█▄▄▄ ▀█▄▄▄ ▄▄▄█▀ ██ ▀█▄ ▀███▀
      ██
      ▀▀"""

# ── colours ──────────────────────────────────────────────────────────────────
TTY = sys.stdout.isatty()
def c(code: str, s: str) -> str:
    return f"\x1b[{code}m{s}\x1b[0m" if TTY else s
BLUE = "38;5;117"; GREEN = "38;5;114"; YELLOW = "38;5;221"; RED = "38;5;203"; DIM = "2"; BOLD = "1"
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"  # braille spinner frames, 80 ms each
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
def vis(s: str) -> int:
    """Visible width of a string that may carry ANSI codes."""
    return len(ANSI_RE.sub("", s))
def pad(s: str, width: int) -> str:
    return s + " " * max(0, width - vis(s))

# ── what each row means for the user, in plain English ───────────────────────
FEATURE_BY_LAYER = {
    "actionGate": "shortcut chooser",
    "waitGate": "done-talking check",
    "decider": "background decider",
    "compaction": "keeps chats short",
    "narrator": "spoken reply",
    "subagents": "background workers",
    "model": "default model",
}
HOT_LAYERS = {"actionGate", "waitGate", "decider"}
SEP = " • "  # feature phrases are short and joined with a middle dot
CONNECTOR_MODELS = [
    ("gpt-5.6-sol-fast", SEP.join(["screen reading", "notes", "diagrams"])),
    ("gpt-5.6-sol", "stalk judge"),
    ("google/gemma-4-31b-it", "backup shortcut chooser"),
]

# ── what a status code means, and who fixes it ───────────────────────────────
MEANINGS = {
    200: "OK",
    401: "litellm key invalid or revoked — rotate LITELLM_API_KEY in .env and the ALMANAC_LITELLM_API_KEY release secret, then rebuild the DMG",
    402: "provider credit exhausted — top up that provider's account",
    403: "this key is not allowed to use this model — litellm key permissions",
    404: "model removed or renamed at the provider — repoint the route on the proxy",
    429: "rate limited — wait, or raise the limit on the proxy",
    "cooldown": "provider behind it is failing (all deployments in cooldown) — add a fallback route on the proxy; until then every turn that needs it dies",
    "5xx": "provider or proxy error, usually passes in minutes",
    "timeout": f"no answer in {TIMEOUT_S} s — provider hung",
    "unreachable": "proxy unreachable — network or DNS on this Mac",
    "cartesia-401": "CARTESIA_API_KEY in .env is invalid — the dev stack cannot hear you",
}


def read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def rows(include_paid: bool) -> list[dict]:
    """One row per dependency. Gate models are read from openclaw.json at run time."""
    out: list[dict] = []
    layers: dict[str, list[str]] = {}
    try:
        cfg = json.loads(OPENCLAW_JSON.read_text())
        def walk(node, path):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k in ("model", "primary") and isinstance(v, str) and v not in ("ink-2-latest", "sonic-3.5"):
                        model = v.split("/", 1)[1] if v.startswith("litellm/") else v
                        layer = next((p for p in reversed(path) if p in FEATURE_BY_LAYER), "model")
                        layers.setdefault(model, []).append(layer)
                    walk(v, path + [k])
            elif isinstance(node, list):
                for x in node:
                    walk(x, path)
        walk(cfg, [])
    except Exception:
        pass
    for model, ls in layers.items():
        ls = list(dict.fromkeys(ls))
        out.append({"model": model, "kind": "chat", "hot": any(l in HOT_LAYERS for l in ls),
                    "feature": SEP.join(FEATURE_BY_LAYER[l] for l in ls)})
    for model, feature in CONNECTOR_MODELS:
        if model not in layers:
            out.append({"model": model, "kind": "chat", "hot": False, "feature": feature})
    out.append({"model": "litellm key", "kind": "key", "hot": True, "feature": SEP.join(["LiteLLM API key", "every model call", "DMG bakes its own copy"])})
    out.append({"model": "media-litellm relay", "kind": "relay", "hot": True, "feature": SEP.join(["STT", "TTS", "DMG app", "same key"])})
    out.append({"model": "cartesia key", "kind": "cartesia", "hot": True, "feature": SEP.join(["Cartesia STT key", "dev stack"])})
    out.append({"model": "serper", "kind": "serper" if include_paid else "skip", "hot": False, "feature": SEP.join(["web search", "stalk", "compare"])})
    out.append({"model": "exa", "kind": "exa" if include_paid else "skip", "hot": False, "feature": SEP.join(["article fetch", "save", "visualise"])})
    return out


def http(url: str, headers: dict, data: bytes | None = None, stream: bool = False):
    """One request. Returns (status, body, exc, ms, headers, ttft_ms). With stream=True the body is
    the concatenated SSE lines and ttft_ms is when the first streamed chunk arrived (the model started answering)."""
    t0 = time.time()
    ms = lambda: int((time.time() - t0) * 1000)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=TIMEOUT_S) as r:
            hdrs = {k.lower(): v for k, v in r.headers.items()}
            if not stream:
                return r.status, r.read(1500).decode(errors="ignore"), None, ms(), hdrs, None
            chunks, ttft = [], None
            for raw in r:
                line = raw.decode(errors="ignore").strip()
                if not line.startswith("data:"):
                    continue
                chunks.append(line)
                if ttft is None:
                    ttft = ms()
                if line == "data: [DONE]":
                    break
            return r.status, "\n".join(chunks)[-3000:], None, ms(), hdrs, ttft
    except urllib.error.HTTPError as e:
        return e.code, e.read(1500).decode(errors="ignore"), None, ms(), {k.lower(): v for k, v in e.headers.items()}, None
    except Exception as e:  # noqa: BLE001
        return None, str(e)[:200], ("timeout" if "timed out" in str(e).lower() else "unreachable"), ms(), {}, None


def probe(row: dict, env: dict) -> dict:
    auth = {"Authorization": f"Bearer {env.get('LITELLM_API_KEY', '')}", "Content-Type": "application/json"}
    kind = row["kind"]
    if kind == "skip":
        return {**row, "state": "skip", "code": "skip", "ms": 0, "meaning": "not probed: each call costs credits (use --all)", "detail": "", "tokens": None, "ttft": None, "rpm_left": None, "rpm_limit": None}
    if kind == "chat":
        body = json.dumps({"model": row["model"], "max_tokens": 1, "stream": True, "stream_options": {"include_usage": True},
                           "messages": [{"role": "user", "content": "ok"}]}).encode()
        code, text, exc, ms, hdrs, ttft = http(f"{PROXY}/chat/completions", auth, body, stream=True)
    elif kind == "key":
        code, text, exc, ms, hdrs, ttft = http(f"{PROXY}/models", auth)
    elif kind == "relay":
        code, text, exc, ms, hdrs, ttft = http(f"{MEDIA_RELAY}/models", auth)
        if code in (404, 405) and not exc:
            code = 200  # the relay answered; it just has no /models route
    elif kind == "cartesia":
        # The stack's STT endpoint; a plain GET with a valid key gets 400/426 (needs websocket), a bad key gets 401.
        code, text, exc, ms, hdrs, ttft = http("https://api.cartesia.ai/stt/turns/websocket", {"X-API-Key": env.get("CARTESIA_API_KEY", ""), "Cartesia-Version": "2025-04-16"})
        if code in (400, 426) and not exc:
            code = 200
    elif kind == "serper":
        code, text, exc, ms, hdrs, ttft = http("https://google.serper.dev/search", {"X-API-KEY": env.get("SERPER_API_KEY", ""), "Content-Type": "application/json"}, json.dumps({"q": "ok", "num": 1}).encode())
    else:  # exa
        code, text, exc, ms, hdrs, ttft = http("https://api.exa.ai/search", {"x-api-key": env.get("EXA_API_KEY", ""), "Content-Type": "application/json"}, json.dumps({"query": "ok", "numResults": 1}).encode())

    if exc:
        state, meaning = "down", MEANINGS[exc]
    elif code == 200:
        state, meaning = "up", MEANINGS[200]
    elif code == 429 and "No deployments available" in text:
        state, meaning = "down", MEANINGS["cooldown"]
    elif code == 429:
        state, meaning = "warn", MEANINGS[429]
    elif code == 401 and kind == "cartesia":
        state, meaning = "down", MEANINGS["cartesia-401"]
    elif code in MEANINGS:
        state, meaning = "down", MEANINGS[code]
    elif 500 <= (code or 0) < 600:
        state, meaning = "down", MEANINGS["5xx"]
    else:
        state, meaning = "down", f"HTTP {code}"
    detail = ""
    if state != "up":
        m = re.search(r'"message"\s*:\s*"([^"]{0,200})', text or "")
        detail = (m.group(1) if m else (text or "")[:200]).replace("\\n", " ").strip()
        detail = re.sub(r"sk-[^\s,]*", "sk-…", detail)  # never echo key material, even the proxy's masked form
    tokens = None
    if kind == "chat" and state == "up":
        m = re.search(r'"total_tokens"\s*:\s*(\d+)', text or "")
        tokens = int(m.group(1)) if m else None
    num = lambda k: (lambda v: float(v) if v not in (None, "") else None)(hdrs.get(k))
    details = {  # what the proxy tells us per call (None when the endpoint does not say)
        "ttft": ttft,
        "upstream_ms": num("x-litellm-response-duration-ms"),
        "rpm_left": num("x-ratelimit-remaining-requests"), "rpm_limit": num("x-ratelimit-limit-requests"),
        "tpm_left": num("x-ratelimit-remaining-tokens"), "tpm_limit": num("x-ratelimit-limit-tokens"),
        "cost_usd": num("x-litellm-response-cost"),
        "key_rpm_left": num("x-ratelimit-api_key-remaining-requests"), "key_rpm_limit": num("x-litellm-key-rpm-limit"),
        "key_spend_usd": num("x-litellm-key-spend"),
        "provider": (hdrs.get("x-litellm-model-api-base") or "").split("//")[-1].split("/")[0] or None,
    }
    return {**row, "state": state, "code": code if code is not None else exc, "ms": ms, "meaning": meaning, "detail": detail, "tokens": tokens, **details}


def run_pass(only: list[dict] | None = None, include_paid: bool = False) -> list[dict]:
    env = read_env()
    targets = only if only is not None else rows(include_paid)
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(lambda r: probe(r, env), targets))


# ── rendering ────────────────────────────────────────────────────────────────
def wrap(s: str, width: int) -> list[str]:
    words, lines, cur = s.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width and cur:
            lines.append(cur); cur = w
        else:
            cur = f"{cur} {w}".strip()
    return lines + [cur] if cur else lines


def frame(results: list[dict], stamp: str, watching: int | None, countdown: int | None = None,
          spin: str = SPIN[0], flash_off: frozenset = frozenset()) -> list[str]:
    """The whole screen as a list of lines. Rows in state "pending" get the spinner and "…";
    rows named in flash_off get a blank STATUS cell (one "off" frame of the change flash)."""
    cols = (os.get_terminal_size().columns or 120) if TTY else 120
    w_status, w_model, w_feat, w_res, w_ms, w_limit = 6, 24, 34, 12, 6, 13  # STATUS first, no wider than "● skip"
    w_mean = max(30, cols - (w_status + w_model + w_feat + 6))
    out = c(BLUE, BANNER).split("\n") + [""]
    down = sum(1 for r in results if r["state"] == "down")
    pending = sum(1 for r in results if r["state"] == "pending" and r["kind"] != "skip")
    probed = sum(1 for r in results if r["state"] != "skip")
    tokens = sum(r.get("tokens") or 15 for r in results if r["kind"] == "chat")  # real usage, else the 15-token estimate
    if pending:
        out.append(f"{c(BOLD, 'UPTIME TEESKO')}   {c(DIM, f'checking {pending} rows…')}")
    else:
        head = c(BOLD, "UPTIME TEESKO") + "   " + (c(RED, f"{down} DOWN of {probed}") if down else c(GREEN, f"all {probed} probed rows up"))
        key = next((r for r in results if r.get("key_rpm_limit") is not None and r.get("key_spend_usd") is not None), None)
        key_note = ""
        if key:
            left = f"{int(key['key_rpm_left'])}/" if key.get("key_rpm_left") is not None else ""
            key_note = f" · key {left}{int(key['key_rpm_limit'])} rpm · spent ${key['key_spend_usd']:.2f}"
        out.append(f"{head}   {c(DIM, f'checked {stamp} · pass ≈ {tokens} tokens{key_note}')}")
    out.append("")
    out.append(c(DIM, f"{'STATUS':<{w_status}} {'MODEL':<{w_model}} {'FEATURE':<{w_feat}} {'RESULT':<{w_res}} {'MS':>{w_ms}} {'TTFT':>{w_ms}}  LIMIT"))
    for r in results:
        colour = {"up": GREEN, "warn": YELLOW, "down": RED, "skip": DIM, "pending": DIM}[r["state"]]
        verdict = {"up": "WORKING", "warn": "RATE LIMITED", "down": "NOT WORKING", "skip": "SKIPPED", "pending": "…"}[r["state"]]
        code = {"unreachable": "off", "timeout": "t/o"}.get(r["code"], r["code"])  # keep STATUS narrow
        if r["model"] in flash_off:
            status = ""
        elif r["state"] == "pending":
            status = c(DIM, spin)
        else:
            status = c(colour, f"{'○' if r['state'] == 'skip' else '●'} {code}")
        feature = r["feature"] + (" (HOT)" if r.get("hot") else "")
        feat_lines = wrap(feature, w_feat - 1) or [""]
        probed_row = r["state"] not in ("skip", "pending")
        ms_s = f"{r['ms']}" if probed_row and r.get("ms") else ""
        ttft_s = f"{r['ttft']}" if probed_row and r.get("ttft") else ""
        limit_s = f"{int(r['rpm_left'])}/{int(r['rpm_limit'])} rpm" if probed_row and r.get("rpm_limit") else ""
        first = True
        for i in range(len(feat_lines)):
            tail = f"{pad(c(colour if r['state'] != 'up' else '0', verdict), w_res)} {ms_s:>{w_ms}} {ttft_s:>{w_ms}}  {c(DIM, limit_s)}" if first else ""
            out.append(f"{pad(status if first else '', w_status)} "
                       f"{pad(c(BOLD, r['model']) if first else '', w_model)} "
                       f"{feat_lines[i]:<{w_feat}} {tail}")
            first = False
        # the fix and the provider's raw error, dimmed, under rows that are not working
        notes = []
        if r["state"] in ("down", "warn"):
            notes.append("↳ " + r["meaning"])
        elif r["state"] == "skip":
            notes.append("↳ use --all to probe")
        if r["detail"]:
            notes.append("↳ " + r["detail"])
        for note in notes:
            for line in wrap(note, w_mean):
                out.append(" " * (w_status + w_model + w_feat + 3) + c(DIM, line))
    out.append("")
    if watching and pending:
        tail = f"watching {down + pending} DOWN rows · checking…"
    elif watching:
        tail = f"watching {down} DOWN rows · next check in {countdown if countdown is not None else watching}s · r = re-check all · q = quit"
    else:
        tail = "r = re-check all · w = watch DOWN rows · q = quit"
    out.append(c(DIM, tail))
    return out


def render(results: list[dict], stamp: str, watching: int | None) -> str:
    return "\n".join(frame(results, stamp, watching))


# ── in-place painting (TTY only) ─────────────────────────────────────────────
class Screen:
    """Keeps the lines currently on screen and repaints only the ones that changed:
    cursor up, clear line, reprint, cursor down. The cursor always rests on the line
    below the frame, so every move is relative to the bottom and survives scrolling."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.lock = threading.Lock()

    def paint(self, new: list[str]) -> None:
        if not TTY:
            return
        with self.lock:
            old, n_old, n_new = self.lines, len(self.lines), len(new)
            rows_avail = os.get_terminal_size().lines or 10**6
            out: list[str] = []
            for i in range(min(n_old, n_new)):
                if old[i] != new[i]:
                    up = n_old - i
                    if up >= rows_avail:
                        continue  # scrolled off the top; nothing to repaint there
                    out.append(f"\x1b[{up}A\r\x1b[2K{new[i]}\x1b[{up}B\r")
            if n_new > n_old:
                out.append("".join(f"\x1b[2K{line}\n" for line in new[n_old:]))
            elif n_new < n_old:
                up = n_old - n_new
                out.append(f"\x1b[{up}A" + "\r\x1b[2K\n" * up + f"\x1b[{up}A\r")
            if out:
                sys.stdout.write("".join(out)); sys.stdout.flush()
            self.lines = list(new)


def live_pass(screen: Screen, targets: list[dict], prev: list[dict] | None, include_paid: bool,
              watch: int | None) -> tuple[list[dict], str]:
    """One pass with animation: rows show a spinner until their probe returns, then repaint
    in place, out of order. prev is None on the first pass (banner reveal, no flash)."""
    env = read_env()
    names = {t["model"] for t in targets}
    def pend(r: dict) -> dict:
        return {**r, "state": "pending", "code": "", "ms": 0, "meaning": "…", "detail": "", "tokens": None, "ttft": None, "rpm_left": None, "rpm_limit": None}
    results = [pend(t) for t in targets] if prev is None else [pend(r) if r["model"] in names else r for r in prev]
    before = {r["model"]: r["state"] for r in (prev or [])}
    guard = threading.Lock()   # results list vs the drawing thread
    stop = threading.Event()
    stamp = time.strftime("%H:%M:%S")

    def draw(i: int) -> None:
        with guard:
            lines = frame(results, stamp, watch, spin=SPIN[i % len(SPIN)])
        screen.paint(lines)

    if prev is None:  # banner reveal: six lines, one per 50 ms, then the rest of the frame
        lines = frame(results, stamp, watch)
        for k in range(1, 7):
            screen.paint(lines[:k]); time.sleep(0.05)
        screen.paint(lines)
    else:
        draw(0)

    def spinner() -> None:  # draws only; never touches a probe
        i = 0
        while not stop.wait(0.08):
            i += 1; draw(i)
    th = threading.Thread(target=spinner, daemon=True); th.start()

    pool = ThreadPoolExecutor(max_workers=8)
    try:
        for f in as_completed([pool.submit(probe, t, env) for t in targets]):
            res = f.result()
            with guard:
                results = [res if r["model"] == res["model"] else r for r in results]
    finally:
        stop.set(); th.join(); pool.shutdown(wait=False)
    stamp = time.strftime("%H:%M:%S")

    changed = frozenset(r["model"] for r in results if before.get(r["model"]) and before[r["model"]] != r["state"])
    if changed:  # two blinks, 120 ms on / 120 ms off, in the new colour
        for k in range(4):
            screen.paint(frame(results, stamp, watch, flash_off=changed if k % 2 == 0 else frozenset()))
            time.sleep(0.12)
    still_down = any(r["state"] == "down" for r in results)
    screen.paint(frame(results, stamp, watch if still_down else None, countdown=watch))
    return results, stamp


def interactive(include_paid: bool, watch: int | None) -> int:
    """TTY session: animated passes, keys, watch countdown, clean exit."""
    screen = Screen()
    sys.stdout.write("\x1b[2J\x1b[H\x1b[?25l\x1b[?7l"); sys.stdout.flush()  # clear, hide cursor, no wrap
    try:
        results, stamp = live_pass(screen, rows(include_paid), None, include_paid, watch)
        countdown = watch
        while True:
            key = read_key(1.0 if watch else 3600)
            if key in ("q", "\x03"):
                return 0
            if key == "r":
                results, stamp = live_pass(screen, rows(include_paid), results, include_paid, watch)
                countdown = watch
            elif key == "w" and not watch and any(r["state"] == "down" for r in results):
                watch = countdown = 30
            elif watch and key is None:
                countdown = (countdown or watch) - 1
                if countdown <= 0:
                    down_rows = [r for r in results if r["state"] == "down"]
                    results, stamp = live_pass(screen, down_rows, results, include_paid, watch)
                    countdown = watch
            if watch and not any(r["state"] == "down" for r in results):
                watch = countdown = None  # recovery: summary goes green, footer returns to the keys
            screen.paint(frame(results, stamp, watch, countdown=countdown))
    except KeyboardInterrupt:
        sys.stdout.write("\x1b[0m\x1b[?25h\x1b[?7h"); sys.stdout.flush()
        os._exit(130)  # don't wait for in-flight probes
    finally:
        sys.stdout.write("\x1b[0m\x1b[?25h\x1b[?7h"); sys.stdout.flush()


def read_key(timeout: float) -> str | None:
    if not TTY:
        time.sleep(timeout); return None
    fd = sys.stdin.fileno(); old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        return sys.stdin.read(1) if r else None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def main(argv: list[str]) -> int:
    once = "--once" in argv or "--json" in argv
    include_paid = "--all" in argv
    watch = int(argv[argv.index("--watch") + 1]) if "--watch" in argv and argv.index("--watch") + 1 < len(argv) else None
    if TTY and not once:
        return interactive(include_paid, watch)
    results = run_pass(include_paid=include_paid)
    stamp = time.strftime("%H:%M:%S")
    if "--json" in argv:
        tokens = sum(r["tokens"] or 0 for r in results)
        print(json.dumps({"checked": stamp, "tokens": tokens, "rows": results}, indent=1)); return 1 if any(r["state"] == "down" for r in results) else 0
    print("\x1b[2J\x1b[H" if TTY and not once else "", end="")
    print(render(results, stamp, watch))
    if once:
        return 1 if any(r["state"] == "down" for r in results) else 0
    while True:
        key = read_key(float(watch) if watch else 3600)
        if key in ("q", "\x03"):
            return 0
        if key == "w" and not watch:
            watch = 30
        if key == "r":
            results = run_pass(include_paid=include_paid); stamp = time.strftime("%H:%M:%S")
        elif watch and (key is None or key == "w"):
            down_rows = [r for r in results if r["state"] == "down"]
            if not down_rows:
                watch = None
            else:
                fresh = {r["model"]: r for r in run_pass(down_rows)}
                results = [fresh.get(r["model"], r) for r in results]; stamp = time.strftime("%H:%M:%S")
        else:
            continue
        print("\x1b[2J\x1b[H" if TTY else "", end="")
        print(render(results, stamp, watch))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
