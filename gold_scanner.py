"""Gold CRT+SMC signal scanner (cloud version). It only sends notifications - it never trades.
Rules (same as the backtest, New York session only):
 1) 1h range candle (UTC hour). 2) price sweeps its high (short) or low (long) within the next 2 hours.
 3) on 1m: market-structure shift (close beyond the last swing) back inside the range, with a Fair Value Gap.
 4) LIMIT entry at 50% of the FVG. Stop beyond the sweep extreme. Target = the other end of the 1h range. Min RR 1.5.
 5) Only setups whose MSS candle closes 12:00-16:59 UTC, Mon-Fri. Pending order valid 60 min."""
import json, os, sys, time, datetime as dt
import urllib.request

RISK_USD = float(os.environ.get('RISK_USD', 15))
OZ_PER_LOT = 100.0
W_HOURS, SCAN, ENT, BUF, MINSTOP, MINRR, FILLMAX, HOLD = 2, 120, 0.5, 0.00005, 0.0003, 1.5, 60, 480
ENTRY_H0, ENTRY_H1 = 12, 17
STATE_FILE = os.environ.get('GOLD_STATE', 'gold_state.json')
NTFY_TOPIC = os.environ.get('NTFY_TOPIC', '')
MIN = 60000

# ---------------------------------------------------------------- data
def http_json(url, timeout=20):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 gold-scanner'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def fetch_bars():
    """Closed 1m bars, oldest first, as (open_ms, o, h, l, c). Tries several exchanges."""
    errs, now = [], int(time.time() * 1000)
    try:
        j = http_json('https://fapi.binance.com/fapi/v1/klines?symbol=XAUUSDT&interval=1m&limit=480')
        bars = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in j if int(r[0]) + MIN <= now]
        if len(bars) >= 200: return bars, 'binance XAUUSDT'
    except Exception as e: errs.append(f'binance {e}')
    try:
        j = http_json('https://api.bybit.com/v5/market/kline?category=linear&symbol=XAUUSDT&interval=1&limit=480')
        rows = [r for r in j['result']['list'] if int(r[0]) + MIN <= now]
        bars = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in reversed(rows)]
        if len(bars) >= 200: return bars, 'bybit XAUUSDT'
    except Exception as e: errs.append(f'bybit {e}')
    try:
        out = []
        after = ''
        for _ in range(2):
            j = http_json('https://www.okx.com/api/v5/market/candles?instId=XAUT-USDT-SWAP&bar=1m&limit=300' + after)
            rows = [r for r in j['data'] if r[8] == '1']
            out += rows
            if not j['data']: break
            after = '&after=' + j['data'][-1][0]
        seen, bars = set(), []
        for r in sorted(out, key=lambda r: int(r[0])):
            if r[0] in seen: continue
            seen.add(r[0]); bars.append((int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])))
        if len(bars) >= 200: return bars, 'okx XAUT-USDT'
    except Exception as e: errs.append(f'okx {e}')
    raise RuntimeError('; '.join(errs))

# ---------------------------------------------------------------- strategy (mirror of the backtest)
def prep(o, h, l, c, n=3):
    L = len(l); piv = [False] * L
    for q in range(n, L - n):
        v = l[q]
        if v < min(l[q - n:q]) and v < min(l[q + 1:q + n + 1]): piv[q] = True
    return dict(o=o, h=h, l=l, c=c, piv=piv)

def setup_short(sr, e, Hc, Lc):
    """Works on a 'short' series. Returns dict(k, entry, stop, tp, rr) or None."""
    o, h, l, c, piv = sr['o'], sr['h'], sr['l'], sr['c'], sr['piv']
    N = len(h); end = min(e + W_HOURS * 60, N - 1)
    j = -1
    for k in range(e + 1, end + 1):
        if l[k] < Lc: return None
        if h[k] > Hc: j = k; break
    if j < 0: return None
    E = h[j]; Ei = j
    for k in range(j, min(j + SCAN, N)):
        if h[k] > E: E = h[k]; Ei = k
        if l[k] < Lc: return None
        pl = None
        for q in range(min(Ei - 1, k - 3), max(j - 20, 3), -1):
            if piv[q]: pl = l[q]; break
        if pl is None: continue
        if k > Ei and c[k] < pl and c[k] < Hc:
            fv = None
            for t in range(k, Ei + 1, -1):
                if l[t - 2] > h[t]: fv = (t, l[t - 2], h[t]); break
            if fv is None: continue
            t, top, bot = fv
            entry = bot + ENT * (top - bot)
            stop = E + BUF * abs(entry)
            risk = stop - entry
            if risk <= 0 or risk < MINSTOP * abs(entry): continue
            rr = (entry - Lc) / risk
            if rr < MINRR: continue
            return dict(k=k, entry=entry, stop=stop, tp=Lc, rr=rr)
    return None

def find_candidates(bars):
    """All setups visible in the given closed 1m bars. Each: key, dir ('S'/'L'), entry, stop, tp, rr, k_ts."""
    n = len(bars)
    ts = [b[0] for b in bars]
    O = [b[1] for b in bars]; H = [b[2] for b in bars]; L = [b[3] for b in bars]; C = [b[4] for b in bars]
    S = prep(O, H, L, C)
    LS = prep([-x for x in O], [-x for x in L], [-x for x in H], [-x for x in C])
    out = []
    first = {}
    for i in range(n):
        hs = ts[i] // 3600000 * 3600000
        if ts[i] == hs: first[hs] = i
    for hs, s in sorted(first.items()):
        e = s + 59
        if e >= n or ts[e] - ts[s] != 59 * MIN: continue
        if dt.datetime.fromtimestamp(hs / 1000, dt.timezone.utc).weekday() >= 5: continue
        Hc = max(H[s:e + 1]); Lc = min(L[s:e + 1])
        cands = []
        a = setup_short(S, e, Hc, Lc)
        if a: cands.append(('S', a))
        b = setup_short(LS, e, -Lc, -Hc)
        if b: cands.append(('L', dict(k=b['k'], entry=-b['entry'], stop=-b['stop'], tp=-b['tp'], rr=b['rr'])))
        if not cands: continue
        cands.sort(key=lambda x: x[1]['k'])
        d, st = cands[0]
        hr = (ts[st['k']] // 3600000) % 24
        if not (ENTRY_H0 <= hr < ENTRY_H1): continue
        out.append(dict(key=str(hs), dir=d, entry=st['entry'], stop=st['stop'], tp=st['tp'], rr=st['rr'], k_ts=ts[st['k']]))
    return out

def advance(sg, bars):
    """Replay new bars on a tracked signal (same fill/exit logic as the backtest). Returns list of (event, ts, price)."""
    ev = []
    S = sg['dir'] == 'S'
    for (t, o, h, l, c) in bars:
        if sg['status'] in ('closed',): break
        if t <= sg['checked']: continue
        sg['checked'] = t
        stop_hit = (h >= sg['stop']) if S else (l <= sg['stop'])
        tp_hit = (l <= sg['tp']) if S else (h >= sg['tp'])
        if sg['status'] == 'pending':
            if (h >= sg['entry']) if S else (l <= sg['entry']):
                sg['status'] = 'open'; sg['fill_t'] = t; ev.append(('FILLED', t, sg['entry']))
                if stop_hit: sg['status'] = 'closed'; sg['result'] = 'sl'; ev.append(('STOP', t, sg['stop']))
                continue
            if tp_hit: sg['status'] = 'closed'; sg['result'] = 'cancel_target'; ev.append(('CANCEL_TARGET', t, sg['tp']))
            elif (t - sg['k_ts']) // MIN >= FILLMAX: sg['status'] = 'closed'; sg['result'] = 'cancel_expired'; ev.append(('CANCEL_EXPIRED', t, c))
        elif sg['status'] == 'open':
            if stop_hit: sg['status'] = 'closed'; sg['result'] = 'sl'; ev.append(('STOP', t, sg['stop']))
            elif tp_hit: sg['status'] = 'closed'; sg['result'] = 'tp'; ev.append(('TP', t, sg['tp']))
            elif (t - sg['fill_t']) // MIN >= HOLD: sg['status'] = 'closed'; sg['result'] = 'timeout'; ev.append(('TIMEOUT', t, c))
    return ev

def new_signal(cand, bars):
    return dict(cand, status='pending', checked=cand['k_ts'], fill_t=0, result='', notified=False, created=int(time.time()))

def process(bars, state):
    """Core step: mutate state, return list of (title, body) notifications."""
    notes = []
    sigs = state.setdefault('signals', {})
    last_close = bars[-1][4]
    # 1) update tracked signals
    for key, sg in list(sigs.items()):
        if sg['status'] == 'closed': continue
        for evn, t, px in advance(sg, bars):
            if not sg['notified']: continue          # we never told the user about this one
            notes.append(msg_event(sg, evn, t, px))
    # 2) new setups
    for cand in find_candidates(bars):
        if cand['key'] in sigs: continue
        sg = new_signal(cand, bars)
        sigs[cand['key']] = sg
        evs = advance(sg, bars)                       # what happened between the MSS candle and now
        if sg['status'] == 'pending':
            sg['notified'] = True
            notes.append(msg_enter(sg, last_close))
        else:
            sg['result'] = sg['result'] or 'missed'   # already filled/cancelled before we could alert - silent
            if sg['status'] == 'open': sg['result'] = 'missed_open'
            # an 'open' one we did not alert on is simply ignored
            sg['status'] = 'closed'
    # prune old
    cut = int(time.time()) - 2 * 86400
    for key in [k for k, v in sigs.items() if v['status'] == 'closed' and v.get('created', 0) < cut]: del sigs[key]
    return notes

# ---------------------------------------------------------------- messages (Bulgarian)
try:
    from zoneinfo import ZoneInfo
    SOF = ZoneInfo('Europe/Sofia')
except Exception:
    SOF = dt.timezone(dt.timedelta(hours=3))
def bg(ms): return dt.datetime.fromtimestamp(ms / 1000, SOF).strftime('%H:%M')
def px(x): return f'{x:,.2f}'

def size_text(entry, stop):
    dist = abs(stop - entry); oz = RISK_USD / dist; lots = oz / OZ_PER_LOT
    lr = max(0.01, round(lots, 2)); real = lr * OZ_PER_LOT * dist
    return dist, f'за риск ${RISK_USD:.0f}: {oz:.2f} oz = {lots:.3f} лота. При {lr:.2f} лота губиш ~${real:.0f} на стопа.'

def msg_enter(sg, last):
    S = sg['dir'] == 'S'; dist, size = size_text(sg['entry'], sg['stop'])
    title = 'GOLD SELL LIMIT' if S else 'GOLD BUY LIMIT'
    until = sg['k_ts'] + (FILLMAX + 1) * MIN
    body = (f"ЗЛАТО {'ШОРТ' if S else 'ЛОНГ'} - лимитна поръчка (не маркет)\n"
            f"ВХОД ({'SELL' if S else 'BUY'} LIMIT): {px(sg['entry'])}\n"
            f"СТОП: {px(sg['stop'])}  (разстояние ${dist:.2f})\n"
            f"ЦЕЛ (TP): {px(sg['tp'])}  (RR {sg['rr']:.1f})\n"
            f"Размер {size}\n"
            f"Цена сега: {px(last)}\n"
            f"Валидна до {bg(until)} (БГ). Изтрий поръчката, ако цената стигне ЦЕЛТА преди да влезе или мине времето.")
    return title, body

def msg_event(sg, evn, t, p):
    risk = RISK_USD
    name = 'ШОРТ' if sg['dir'] == 'S' else 'ЛОНГ'
    if evn == 'FILLED': return 'GOLD ORDER FILLED', f"ЗЛАТО {name}: цената докосна входа {px(sg['entry'])} ({bg(t)} БГ) - сделката е отворена (ако поръчката ти е влязла). Стоп {px(sg['stop'])}, цел {px(sg['tp'])}."
    if evn == 'TP': return 'GOLD TARGET HIT', f"ЗЛАТО {name}: ЦЕЛТА {px(sg['tp'])} е достигната ({bg(t)} БГ). Печалба ~${risk * sg['rr']:.0f}. Затвори/провери сделката."
    if evn == 'STOP': return 'GOLD STOP HIT', f"ЗЛАТО {name}: СТОП {px(sg['stop'])} е ударен ({bg(t)} БГ). Загуба ~${risk:.0f}."
    if evn == 'CANCEL_TARGET': return 'GOLD CANCEL ORDER', f"ЗЛАТО {name}: цената стигна целта без да влезе. ИЗТРИЙ лимитната поръчка ({px(sg['entry'])})."
    if evn == 'CANCEL_EXPIRED': return 'GOLD CANCEL ORDER', f"ЗЛАТО {name}: 60 минути минаха, поръчката не е влязла. ИЗТРИЙ лимитната поръчка ({px(sg['entry'])})."
    if evn == 'TIMEOUT': return 'GOLD CLOSE TRADE', f"ЗЛАТО {name}: 8 часа от влизането. Затвори сделката на пазара (~{px(p)})."
    return 'GOLD', evn

# ---------------------------------------------------------------- main
def notify(title, body, priority='high'):
    if not NTFY_TOPIC: print('NOTIFY', title, body); return
    try:
        req = urllib.request.Request(f'https://ntfy.sh/{NTFY_TOPIC}', data=body.encode('utf-8'), headers={'Title': title, 'Priority': priority}, method='POST')
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e: print('notify failed', e)

def run_once():
    """One scan. Returns True if there is still an active (pending/open) signal."""
    state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
    now = dt.datetime.now(dt.timezone.utc); today = now.strftime('%Y-%m-%d')
    lines = [f"=== Gold scan {now.strftime('%Y-%m-%d %H:%M UTC')} ==="]
    try:
        bars, src = fetch_bars()
    except Exception as e:
        lines.append(f'data error: {str(e)[:300]}'); text = '\n'.join(lines); print(text); open('gold_latest.txt', 'w').write(text + '\n')
        if state.get('_err') != now.strftime('%Y-%m-%d %H') and now.weekday() < 5 and 12 <= now.hour < 18:
            notify('Gold scanner problem', 'Не успях да взема данни за златото. Виж GitHub Actions.', 'default'); state['_err'] = now.strftime('%Y-%m-%d %H')
            json.dump(state, open(STATE_FILE, 'w'), indent=1)
        return True
    lines.append(f'source: {src}; last closed bar {dt.datetime.fromtimestamp(bars[-1][0]/1000, dt.timezone.utc):%H:%M} UTC close {px(bars[-1][4])}')
    notes = process(bars, state)
    act = [v for v in state.get('signals', {}).values() if v['status'] != 'closed']
    lines += [f"{v['dir']} {v['status']} entry {px(v['entry'])} stop {px(v['stop'])} tp {px(v['tp'])}" for v in act] or ['no active setups']
    if notes: lines += ['', '*** ALERTS ***'] + [f'{t}: {b}' for t, b in notes]
    text = '\n'.join(lines); print(text); open('gold_latest.txt', 'w').write(text + '\n')
    for t, b in notes: notify(t, b)
    if now.weekday() < 5 and now.hour >= 11 and state.get('_hb') != today and not notes:
        state['_hb'] = today
        notify('Gold scanner alive', f'OK ({src}). Цена {px(bars[-1][4])}. Сесия Ню Йорк 15:00-20:00 БГ. Няма активен сигнал.', 'default')
    state.pop('_err', None)
    json.dump(state, open(STATE_FILE, 'w'), indent=1)
    return bool(act)

def git_push():
    if os.environ.get('GIT_PUSH') != '1': return
    import subprocess
    try:
        subprocess.run(['git', 'config', 'user.name', 'gold-bot'], check=False)
        subprocess.run(['git', 'config', 'user.email', 'gold-bot@users.noreply.github.com'], check=False)
        subprocess.run(['git', 'add', STATE_FILE, 'gold_latest.txt'], check=False)
        if subprocess.run(['git', 'diff', '--cached', '--quiet']).returncode != 0:
            subprocess.run(['git', 'commit', '-m', 'gold ' + dt.datetime.now(dt.timezone.utc).strftime('%F_%H:%M')], check=False)
            subprocess.run(['git', 'pull', '--rebase', '-q'], check=False)
            subprocess.run(['git', 'push', '-q'], check=False)
    except Exception as e: print('git push failed', e)

def loop():
    """Scan every minute. Started ~11:55 UTC it runs until 17:40; started ~17:35 it keeps going until no active signal (max 23:55)."""
    start = dt.datetime.now(dt.timezone.utc)
    extend = (start.hour, start.minute) >= (17, 30)
    until = start.replace(hour=17, minute=40, second=0, microsecond=0) if not extend else start.replace(hour=23, minute=55, second=0, microsecond=0)
    last_push = time.time(); active = False
    while True:
        try: active = run_once()
        except Exception as e: print('scan error', repr(e)[:200])
        now = dt.datetime.now(dt.timezone.utc)
        if time.time() - last_push > 900: git_push(); last_push = time.time()
        if now >= until or (extend and not active and now.hour >= 17): break
        time.sleep(max(1, 65 - now.second) if now.second < 65 else 1)
    git_push()

def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'loop': loop()
    else: run_once()

if __name__ == '__main__': main()
