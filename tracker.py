#!/usr/bin/env python3
import json, glob, os, sys, time, signal, shutil
from datetime import datetime, timezone, timedelta

PROJECTS = os.path.expanduser("~/.claude/projects")
PALETTE = os.path.expanduser("~/.local/state/quickshell/user/generated/colors.json")
REFRESH = 2.0
WINDOW = timedelta(hours=5)

# $/million tokens: input, output, cache_write(1h-ish), cache_read
PRICING = {
    "opus":   (15.0, 75.0, 18.75, 1.50),
    "sonnet": (3.0,  15.0, 3.75,  0.30),
    "haiku":  (0.80, 4.0,  1.0,   0.08),
}
def price_for(model):
    m = (model or "").lower()
    for k in PRICING:
        if k in m: return PRICING[k]
    return PRICING["sonnet"]

def short_model(model):
    m = (model or "?").lower()
    for k in ("opus", "sonnet", "haiku"):
        if k in m: return k
    return m.split("/")[-1][:8]

PAL = {}
def load_palette():
    global PAL
    try:
        with open(PALETTE) as f: PAL = json.load(f)
    except Exception:
        PAL = {}
def hx(name, fallback):
    v = PAL.get(name, fallback)
    return v if isinstance(v, str) and v.startswith("#") else fallback
def rgb(h):
    h = h.lstrip("#")
    return int(h[0:2],16), int(h[2:4],16), int(h[4:6],16)
def fg(h): r,g,b = rgb(h); return f"\033[38;2;{r};{g};{b}m"
def bg(h): r,g,b = rgb(h); return f"\033[48;2;{r};{g};{b}m"
RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"

# ---- parsing with mtime cache ----
_cache = {}  # path -> (mtime, size, [events])
_seen_uuid = set()

def parse_file(path):
    events = []
    try:
        with open(path, errors="ignore") as fh:
            for ln in fh:
                if '"usage"' not in ln: continue
                try: d = json.loads(ln)
                except Exception: continue
                if d.get("type") != "assistant": continue
                msg = d.get("message", {})
                u = msg.get("usage")
                if not u: continue
                uid = d.get("uuid")
                ts = d.get("timestamp")
                if not ts: continue
                try:
                    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except Exception:
                    continue
                cwd = d.get("cwd") or ""
                proj = os.path.basename(cwd) if cwd else os.path.basename(os.path.dirname(path))
                events.append({
                    "uuid": uid, "t": t, "model": msg.get("model"),
                    "proj": proj or "?",
                    "in": u.get("input_tokens", 0) or 0,
                    "out": u.get("output_tokens", 0) or 0,
                    "cw": u.get("cache_creation_input_tokens", 0) or 0,
                    "cr": u.get("cache_read_input_tokens", 0) or 0,
                })
    except Exception:
        pass
    return events

def refresh_events():
    files = glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))
    for p in files:
        try: st = os.stat(p)
        except OSError: continue
        c = _cache.get(p)
        if c and c[0] == st.st_mtime and c[1] == st.st_size: continue
        _cache[p] = (st.st_mtime, st.st_size, parse_file(p))
    # flatten + dedupe by uuid
    all_ev = []
    seen = set()
    latest_file = None; latest_mtime = -1
    for p, (mt, sz, evs) in _cache.items():
        if mt > latest_mtime: latest_mtime, latest_file = mt, p
        for e in evs:
            if e["uuid"] in seen: continue
            seen.add(e["uuid"]); all_ev.append(e)
    all_ev.sort(key=lambda e: e["t"])
    return all_ev, latest_file

def cost(e):
    pi, po, pw, pr = price_for(e["model"])
    return (e["in"]*pi + e["out"]*po + e["cw"]*pw + e["cr"]*pr) / 1_000_000

def tok(e): return e["in"] + e["out"] + e["cw"] + e["cr"]

def fmt_tok(n):
    if n >= 1_000_000: return f"{n/1_000_000:.2f}M"
    if n >= 1_000: return f"{n/1_000:.1f}k"
    return str(int(n))

def fmt_dur(td):
    s = int(td.total_seconds())
    if s < 0: s = 0
    h, s = divmod(s, 3600); m = s // 60
    return f"{h}h {m:02d}m" if h else f"{m}m"

# ---- 5h block model (ccusage-style) ----
def active_block(events):
    if not events: return None
    blocks = []
    cur = None
    for e in events:
        if cur is None:
            start = e["t"].replace(minute=0, second=0, microsecond=0)
            cur = {"start": start, "end": start + WINDOW, "events": [e], "last": e["t"]}
            continue
        if e["t"] >= cur["end"] or (e["t"] - cur["last"]) > WINDOW:
            blocks.append(cur)
            start = e["t"].replace(minute=0, second=0, microsecond=0)
            cur = {"start": start, "end": start + WINDOW, "events": [e], "last": e["t"]}
        else:
            cur["events"].append(e); cur["last"] = e["t"]
    if cur: blocks.append(cur)
    now = datetime.now(timezone.utc)
    last = blocks[-1]
    if now < last["end"]:
        return last
    return None

def bar(frac, width, fill_c, track_c):
    frac = max(0.0, min(1.0, frac))
    n = int(round(frac * width))
    return fg(fill_c) + "█"*n + fg(track_c) + "─"*(width-n) + RESET

# ---- render ----
def render():
    events, latest = refresh_events()
    now = datetime.now(timezone.utc)
    nowl = datetime.now()
    cols = max(64, min(shutil.get_terminal_size((100, 40)).columns - 2, 116))

    C = {
        "bg": hx("surface", "#16131a"),
        "panel": hx("surface_container", "#221f26"),
        "track": hx("surface_container_highest", "#38343c"),
        "text": hx("on_surface", "#e6e0e9"),
        "sub": hx("on_surface_variant", "#cac4cf"),
        "pri": hx("primary", "#d0bcff"),
        "sec": hx("secondary", "#ccc2dc"),
        "ter": hx("tertiary", "#efb8c8"),
        "err": hx("error", "#f2b8b5"),
        "ok": hx("success", "#a3d9a5"),
        "out": hx("outline", "#938f99"),
    }
    out = []
    def line(s=""): out.append(s)

    inner = cols - 2
    def rule(left, right=""):
        rl = strip_len(left); rr = strip_len(right)
        mid = inner - rl - rr
        line(fg(C["out"]) + "│" + RESET + left + " "*max(1, mid) + right + fg(C["out"]) + "│" + RESET)

    # header
    title = f"{BOLD}{fg(C['pri'])}  CLAUDE USAGE{RESET}"
    clock = f"{fg(C['sub'])}{nowl:%a %H:%M:%S}{RESET}"
    line(fg(C["out"]) + "╭" + "─"*inner + "╮" + RESET)
    rule(title, clock)
    line(fg(C["out"]) + "├" + "─"*inner + "┤" + RESET)

    # ---- 5h window ----
    blk = active_block(events)
    if blk:
        bt = sum(tok(e) for e in blk["events"])
        bc = sum(cost(e) for e in blk["events"])
        resets = blk["end"] - now
        # calibrate bar against env limit or historical peak token-block
        env_lim = os.environ.get("CLAUDE_USAGE_TOKEN_LIMIT")
        if env_lim and env_lim.isdigit():
            limit = int(env_lim); ref_label = "limit"
        else:
            limit = max(bt, reference_block_tokens(events)); ref_label = "typical"
        frac = bt / limit if limit else 0
        col = C["ok"] if frac < 0.6 else (C["ter"] if frac < 0.85 else C["err"])
        rule(f"{BOLD}{fg(C['sec'])}5-HOUR WINDOW{RESET}", f"{fg(C['sub'])}resets in {fg(col)}{fmt_dur(resets)}{RESET}")
        rule("  " + bar(frac, inner-12, col, C["track"]), f"{fg(col)}{frac*100:.0f}%{RESET}")
        rule(f"  {fg(C['text'])}{fmt_tok(bt)} tok{RESET}  {fg(C['sub'])}·{RESET}  {fg(C['ok'])}${bc:.2f}{RESET}  {fg(C['sub'])}· {len(blk['events'])} msgs · {DIM}vs {fmt_tok(limit)} {ref_label}{RESET}")
    else:
        rule(f"{BOLD}{fg(C['sec'])}5-HOUR WINDOW{RESET}", f"{fg(C['sub'])}idle{RESET}")
        rule(f"  {fg(C['sub'])}no activity in the last 5h{RESET}")
    line(fg(C["out"]) + "│" + " "*inner + "│" + RESET)

    # ---- today ----
    today = nowl.date()
    tev = [e for e in events if e["t"].astimezone().date() == today]
    tt = sum(tok(e) for e in tev); tc = sum(cost(e) for e in tev)
    by_model = {}
    for e in tev:
        k = short_model(e["model"]); by_model.setdefault(k, 0); by_model[k] += tok(e)
    mstr = "  ".join(f"{fg(C['ter'])}{k}{RESET} {fg(C['sub'])}{fmt_tok(v)}{RESET}"
                     for k,v in sorted(by_model.items(), key=lambda x:-x[1]))
    rule(f"{BOLD}{fg(C['sec'])}TODAY{RESET}", f"{fg(C['text'])}{fmt_tok(tt)} tok{RESET}  {fg(C['ok'])}${tc:.2f}{RESET}")
    rule("  " + (mstr or f"{fg(C['sub'])}nothing yet{RESET}"))
    line(fg(C["out"]) + "│" + " "*inner + "│" + RESET)

    # ---- live session ----
    sev = _cache.get(latest, (0,0,[]))[2] if latest else []
    sev = [e for e in sev]
    if sev:
        sev.sort(key=lambda e: e["t"])
        st_ = sum(tok(e) for e in sev); sc = sum(cost(e) for e in sev)
        span = (sev[-1]["t"] - sev[0]["t"]).total_seconds()/60 or 1
        rate = st_/span
        proj = sev[-1]["proj"]; mdl = short_model(sev[-1]["model"])
        age = now - sev[-1]["t"]
        live = f"{fg(C['ok'])}●{RESET}" if age < timedelta(minutes=3) else f"{fg(C['sub'])}○{RESET}"
        rule(f"{BOLD}{fg(C['sec'])}LIVE SESSION{RESET} {live}", f"{fg(C['pri'])}{proj}{RESET} {fg(C['sub'])}· {mdl}{RESET}")
        rule(f"  {fg(C['text'])}{fmt_tok(st_)} tok{RESET}  {fg(C['ok'])}${sc:.2f}{RESET}  {fg(C['sub'])}· {len(sev)} msgs · {fmt_tok(rate)}/min{RESET}")
    else:
        rule(f"{BOLD}{fg(C['sec'])}LIVE SESSION{RESET}", f"{fg(C['sub'])}—{RESET}")
    line(fg(C["out"]) + "│" + " "*inner + "│" + RESET)

    # ---- all-time per project ----
    proj_tok = {}; proj_cost = {}
    for e in events:
        proj_tok[e["proj"]] = proj_tok.get(e["proj"], 0) + tok(e)
        proj_cost[e["proj"]] = proj_cost.get(e["proj"], 0) + cost(e)
    top = sorted(proj_tok.items(), key=lambda x:-x[1])[:6]
    grand_t = sum(proj_tok.values()); grand_c = sum(proj_cost.values())
    first = events[0]["t"].astimezone() if events else nowl
    rule(f"{BOLD}{fg(C['sec'])}ALL-TIME{RESET} {fg(C['sub'])}since {first:%b %d}{RESET}",
         f"{fg(C['text'])}{fmt_tok(grand_t)}{RESET}  {fg(C['ok'])}${grand_c:.0f}{RESET}")
    mx = top[0][1] if top else 1
    for name, t in top:
        nm = (name[:16]).ljust(16)
        b = bar(t/mx, inner-40, C["pri"], C["track"])
        rule(f"  {fg(C['text'])}{nm}{RESET} {b} {fg(C['sub'])}{fmt_tok(t):>6} ${proj_cost[name]:.0f}{RESET}")

    line(fg(C["out"]) + "╰" + "─"*inner + "╯" + RESET)
    line(f"{DIM}{fg(C['sub'])}  q quit · refresh {REFRESH:.0f}s · costs = est. API-equiv{RESET}")
    return "\n".join(out)

import re
_ansi = re.compile(r"\033\[[0-9;]*m")
def strip_len(s): return len(_ansi.sub("", s))

def reference_block_tokens(events):
    # median tokens across historical 5h blocks (bar = this window vs a typical one)
    if not events: return 1
    blocks = []; cur = None
    for e in events:
        if cur is None:
            start = e["t"].replace(minute=0, second=0, microsecond=0)
            cur = {"end": start+WINDOW, "last": e["t"], "t": tok(e)}; continue
        if e["t"] >= cur["end"] or (e["t"]-cur["last"]) > WINDOW:
            blocks.append(cur["t"]); start = e["t"].replace(minute=0,second=0,microsecond=0)
            cur = {"end": start+WINDOW, "last": e["t"], "t": tok(e)}
        else:
            cur["t"] += tok(e); cur["last"] = e["t"]
    if cur: blocks.append(cur["t"])
    blocks = [b for b in blocks if b > 0]
    if not blocks: return 1
    blocks.sort()
    return blocks[len(blocks)//2]

def main():
    load_palette()
    if "--once" in sys.argv:
        print(render()); return
    sys.stdout.write("\033[?1049h\033[?25l")  # alt screen, hide cursor
    sys.stdout.flush()
    def cleanup(*_):
        sys.stdout.write("\033[?25h\033[?1049l"); sys.stdout.flush(); sys.exit(0)
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)
    # non-blocking q-to-quit
    import select, termios, tty
    fd = sys.stdin.fileno()
    old = None
    try:
        old = termios.tcgetattr(fd); tty.setcbreak(fd)
    except Exception:
        pass
    try:
        while True:
            load_palette()
            frame = render()
            sys.stdout.write("\033[H\033[J" + frame)
            sys.stdout.flush()
            t0 = time.time()
            while time.time() - t0 < REFRESH:
                if old and select.select([fd], [], [], 0.2)[0]:
                    ch = sys.stdin.read(1)
                    if ch in ("q", "Q"): cleanup()
    finally:
        if old:
            try: termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except Exception: pass
        cleanup()

if __name__ == "__main__":
    main()
