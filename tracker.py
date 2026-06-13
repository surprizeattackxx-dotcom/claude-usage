#!/usr/bin/env python3
import json, glob, os, sys, time, signal, shutil
from datetime import datetime, timezone, timedelta

PROJECTS = os.path.expanduser("~/.claude/projects")
PALETTE = os.path.expanduser("~/.local/state/quickshell/user/generated/colors.json")
CONFIG = os.path.expanduser("~/.config/claude-usage/config.json")
REFRESH = 2.0
WINDOW = timedelta(hours=5)

def load_config():
    try:
        with open(CONFIG) as f: return json.load(f)
    except Exception: return {}
def save_config(d):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w") as f: json.dump(d, f, indent=2)

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
def synced_window(now):
    # real 5h window from a user-synced reset anchor (/usage), advanced in 5h
    # steps so the anchor stays valid across windows instead of silently lapsing.
    ra = load_config().get("reset_at")
    if not ra: return None
    try:
        t = datetime.fromisoformat(ra)
    except Exception:
        return None
    if t.tzinfo is None: t = t.replace(tzinfo=timezone.utc)
    while t <= now: t += WINDOW
    return t - WINDOW, t

def active_block(events, now=None):
    if now is None: now = datetime.now(timezone.utc)
    sw = synced_window(now)
    if sw:
        # synced: window edges come from the real reset, so the usage shown and
        # the countdown reset together the instant `now` crosses `end`.
        start, end = sw
        evs = [e for e in events if start <= e["t"] < end]
        return {"start": start, "end": end, "events": evs,
                "last": max((e["t"] for e in evs), default=start)}
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
        synced = synced_window(now) is not None
        resets = blk["end"] - now
        frac, ref_label = window_fraction(bc, events)
        col = C["ok"] if frac < 0.6 else (C["ter"] if frac < 0.85 else C["err"])
        rmark = f"{fg(C['ter'])}⟳{RESET} " if synced else ""
        rule(f"{BOLD}{fg(C['sec'])}5-HOUR WINDOW{RESET}", f"{fg(C['sub'])}{rmark}resets in {fg(col)}{fmt_dur(resets)}{RESET}")
        rule("  " + bar(frac, inner-12, col, C["track"]), f"{fg(col)}{frac*100:.0f}%{RESET}")
        rule(f"  {fg(C['ok'])}${bc:.2f}{RESET} {fg(C['sub'])}· {ref_label} · {fmt_tok(bt)} tok · {len(blk['events'])} msgs{RESET}")
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
        if not tok(e): continue
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

def parse_dur(s):
    # "2h32m" / "2h" / "32m" / "2:32" -> timedelta
    s = s.strip().lower()
    if ":" in s:
        h, m = s.split(":", 1); return timedelta(hours=int(h), minutes=int(m))
    import re as _re
    h = _re.search(r"(\d+)\s*h", s); m = _re.search(r"(\d+)\s*m", s)
    if not h and not m and s.isdigit(): return timedelta(minutes=int(s))
    return timedelta(hours=int(h.group(1)) if h else 0, minutes=int(m.group(1)) if m else 0)

def cost_budget(events):
    # $ cost budget the 5h window is scaled against (single-point / fallback path).
    env = os.environ.get("CLAUDE_USAGE_COST_BUDGET")
    if env:
        try: return float(env), "limit"
        except ValueError: pass
    cfg = load_config()
    if cfg.get("cost_budget"):
        return float(cfg["cost_budget"]), "limit"
    return reference_block_cost(events), "typical"

def linfit(samples):
    # least-squares official% ≈ a*cost + b  (1 pt -> proportional through origin)
    pts = [(float(c), float(p)) for c, p in samples if float(c) > 0]
    if not pts: return None
    if len(pts) == 1:
        c, p = pts[0]; return p/c, 0.0
    n = len(pts); mc = sum(c for c, _ in pts)/n; mp = sum(p for _, p in pts)/n
    den = sum((c-mc)**2 for c, _ in pts)
    if den == 0: return 0.0, mp
    a = sum((c-mc)*(p-mp) for c, p in pts)/den
    return a, mp - a*mc

def window_fraction(bc, events):
    # returns (fraction 0..1+, label). Prefers a multi-sample linear fit.
    if bc <= 0: return 0.0, "fresh"
    cfg = load_config()
    fit = cfg.get("fit")
    if fit and "a" in fit:
        frac = (fit["a"]*bc + fit["b"]) / 100.0
        return max(0.0, frac), f"fit·{fit.get('n', '?')}pt"
    budget, lbl = cost_budget(events)
    return (bc/budget if budget else 0.0), (f"${budget:.0f} {lbl}")

def block_costs(events):
    # cost-equivalent of every historical 5h block
    if not events: return []
    blocks = []; cur = None
    for e in events:
        if cur is None:
            start = e["t"].replace(minute=0, second=0, microsecond=0)
            cur = {"end": start+WINDOW, "last": e["t"], "c": cost(e)}; continue
        if e["t"] >= cur["end"] or (e["t"]-cur["last"]) > WINDOW:
            blocks.append(cur["c"]); start = e["t"].replace(minute=0,second=0,microsecond=0)
            cur = {"end": start+WINDOW, "last": e["t"], "c": cost(e)}
        else:
            cur["c"] += cost(e); cur["last"] = e["t"]
    if cur: blocks.append(cur["c"])
    return blocks

def reference_block_cost(events):
    # median cost across historical 5h blocks (fallback when uncalibrated)
    blocks = [b for b in block_costs(events) if b > 0]
    if not blocks: return 1.0
    blocks.sort()
    return blocks[len(blocks)//2]

def main():
    load_palette()
    if "--calibrate" in sys.argv:
        i = sys.argv.index("--calibrate")
        try: pct = float(sys.argv[i+1])
        except (IndexError, ValueError):
            print("usage: claude-usage --calibrate <percent>   (your real Claude usage % right now)"); return
        if pct <= 0:
            print("percent must be > 0"); return
        events, _ = refresh_events()
        blk = active_block(events)
        if not blk or not blk["events"]:
            print("no active 5-hour window to calibrate against — use Claude, then retry."); return
        bc = round(sum(cost(e) for e in blk["events"]), 2)
        cfg = load_config()
        samples = cfg.get("samples", [])
        samples.append([bc, pct])
        a, b = linfit(samples)
        cfg["samples"] = samples
        cfg["fit"] = {"a": round(a, 5), "b": round(b, 4), "n": len(samples)}
        cfg.pop("cost_budget", None)
        save_config(cfg)
        est = a*bc + b
        resid = "  ".join(f"${c:.0f}→{p:.0f}%(est {a*c+b:.0f})" for c, p in samples)
        print(f"sample added: window ${bc:.2f} = {pct:.0f}%  (now {len(samples)} pts)")
        print(f"fit: usage% ≈ {a:.3f}·$ + {b:.1f}   this window est {est:.0f}%")
        print(f"points: {resid}\nsaved to {CONFIG}")
        return
    if "--reset-calibration" in sys.argv:
        cfg = load_config()
        for k in ("samples", "fit", "cost_budget"): cfg.pop(k, None)
        save_config(cfg); print("calibration cleared (back to 'typical' fallback)"); return
    if "--set-budget" in sys.argv:
        i = sys.argv.index("--set-budget")
        try: budget = round(float(sys.argv[i+1]), 2)
        except (IndexError, ValueError):
            print("usage: claude-usage --set-budget <dollars>"); return
        cfg = load_config(); cfg["cost_budget"] = budget; save_config(cfg)
        print(f"5-hour cost budget set to ${budget:.2f}  (saved to {CONFIG})"); return
    if "--sync-reset" in sys.argv:
        i = sys.argv.index("--sync-reset")
        try: val = sys.argv[i+1]
        except IndexError: val = ""
        try:
            if val.startswith("@"):  # absolute local clock time, e.g. @12:00
                hh, mm = val[1:].split(":")
                ln = datetime.now().astimezone()
                target = ln.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
                if target <= ln: target += timedelta(days=1)
                when = target.astimezone(timezone.utc)
            else:
                when = (datetime.now(timezone.utc) + parse_dur(val)).replace(second=0, microsecond=0)
        except (ValueError, IndexError):
            print("usage: claude-usage --sync-reset <2h32m|2:32|32m | @12:00>   (from Claude's /usage)"); return
        cfg = load_config(); cfg["reset_at"] = when.isoformat(); save_config(cfg)
        print(f"reset synced: resets {when.astimezone():%H:%M} (in {fmt_dur(when-datetime.now(timezone.utc))})\nsaved to {CONFIG}"); return
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
