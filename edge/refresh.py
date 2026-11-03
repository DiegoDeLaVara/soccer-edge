"""One refresh of the soccer board. The cloud job runs it every ~20 minutes (also runnable locally).

  python -m edge.refresh --data out

Reads/writes in --data: ratings.json.gz (rolling team ratings), log.json (every prediction and paper bet, graded) and
data.js (what the page shows).
"""
import argparse
import json
import math
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import model as M
from . import sources as S
from .ratings import Ratings, load, save

ROOT = Path(__file__).resolve().parents[1]
NOW = time.time()
DAY = 86400
BIG = ["uefa.champions", "eng.1", "esp.1", "ger.1", "ita.1", "fra.1", "uefa.europa", "uefa.europa.conf",
       "usa.1", "mex.1", "por.1", "ned.1", "bra.1", "arg.1", "eng.fa", "eng.league_cup", "eng.2", "sco.1",
       "tur.1", "bel.1", "conmebol.libertadores", "conmebol.sudamericana", "concacaf.champions"]
VOID = ("AET", "PEN", "ABANDON", "POSTPONED", "CANCEL", "SUSPENDED", "FORFEIT")
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("America/Chicago")
except Exception:  # Windows without tzdata
    TZ = timezone(timedelta(hours=-6))


def fee(p):
    return 0.07 * p * (1 - p)


def kev(p, ask):
    """Expected return per $ buying a Kalshi contract at `ask` (taker fee included)."""
    return p / (ask + fee(ask)) - 1 if ask and 0 < ask < 1 else None


def dec(american):
    try:
        a = float(american)
    except (TypeError, ValueError):
        return None
    return 1 + (a / 100 if a > 0 else 100 / -a) if a else None


def amer(d):
    return f"+{round((d - 1) * 100)}" if d >= 2 else f"-{round(100 / (d - 1))}"


def units(p, cost, cfg, mult):
    b = (1 - cost) / cost
    k = (p * b - (1 - p)) / b
    if k <= 0 or mult <= 0:
        return 0.0
    u = k * cfg["kelly_frac"] * mult / cfg["unit_pct"]
    return round(min(max(u, cfg["min_units"]), cfg["max_units"]) * 4) / 4


def rating(ev):
    return "Value" if ev >= 0.02 else "Fair price" if ev >= -0.02 else "Overpriced"


def r3(x):
    return None if x is None else round(float(x), 3)


def local_day(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")


# ------------------------------------------------------------------ results -> ratings
def update_ratings(R, seen, games):
    new = sorted((g for g in games if g["done"] and g["id"] not in seen and g["hg"] is not None), key=lambda g: g["ts"])
    n = 0
    for g in new:
        seen.add(g["id"])
        if any(v in g["st"] for v in VOID):
            continue
        R.add({"h": g["hid"], "a": g["aid"], "lg": g["ls"], "neutral": g["neutral"], "hg": g["hg"], "ag": g["ag"],
               "date": local_day(g["ts"]), "hn": g["h"], "an": g["a"]})
        n += 1
    return n


# ------------------------------------------------------------------ Kalshi
def kalshi_for(games, log):
    """game id -> {'sides': {home, draw, away: quote}, 'ladder': {line: quote}, 'btts': quote, 'ticker'}"""
    out = {}
    for slug in sorted({g["ls"] for g in games} & set(S.SERIES)):
        series = S.SERIES[slug]
        mine = [g for g in games if g["ls"] == slug and not g["done"]]
        try:
            evs = S.kalshi_events(series)
            side = {}
            for kind in ("TOTAL", "BTTS"):
                for e2 in S.kalshi_events(series.replace("GAME", kind)):
                    side.setdefault(e2["event_ticker"].split("-", 1)[1], {})[kind] = e2.get("markets") or []
        except Exception as e:  # noqa: BLE001
            log.append(f"kalshi {series}: {e}")
            continue
        for e in evs:
            mk = e.get("markets") or []
            tie = [m for m in mk if m["ticker"].endswith("-TIE")]
            teams = [m for m in mk if not m["ticker"].endswith("-TIE")]
            if len(teams) != 2:
                continue
            for g in mine:
                day = datetime.fromtimestamp(g["ts"], timezone.utc).strftime("%y%b%d").upper()
                if day not in e["event_ticker"] and abs(g["ts"] - S.market_ts(e)) > 36 * 3600:
                    continue
                hm = [m for m in teams if S.same(m.get("yes_sub_title", ""), g["h"])]
                am = [m for m in teams if S.same(m.get("yes_sub_title", ""), g["a"])]
                if len(hm) != 1 or len(am) != 1 or hm[0] is am[0]:
                    continue
                blk = {"ticker": e["event_ticker"], "sides": {"home": S.quote(hm[0]), "away": S.quote(am[0]),
                                                               "draw": S.quote(tie[0]) if tie else None}}
                x = side.get(e["event_ticker"].split("-", 1)[1], {})
                lad = {}
                for m in x.get("TOTAL", []):
                    ln = S.f(m.get("floor_strike"))
                    if ln is not None:
                        lad[f"{ln:.1f}"] = S.quote(m)
                if lad:
                    blk["ladder"] = lad
                if x.get("BTTS"):
                    blk["btts"] = S.quote(x["BTTS"][0])
                out[g["id"]] = blk
                break
    return out


def mid(q):
    if not q or q.get("bid") is None or q.get("ask") is None or q["ask"] - q["bid"] > 0.1 or not q["ask"]:
        return None
    return (q["bid"] + q["ask"]) / 2


# ------------------------------------------------------------------ one game
def predict(g, R, ka, cfg):
    lh, la = R.lambdas(g["hid"], g["aid"], g["ls"], g["neutral"])
    known = min(R.known(g["hid"]), R.known(g["aid"]))
    line = g.get("line")
    mkt = M.novig([M.american_prob(line.get(k)) for k in ("home", "draw", "away")]) if line else None
    k = (ka or {}).get("sides") or {}
    kal = M.novig([mid(k.get(s)) for s in ("home", "draw", "away")]) if k.get("draw") else None
    if known < 5 and not mkt and not kal:
        return None
    mod = M.outcome(M.grid(lh, la, R.rho))
    w = dict(cfg["blend"])
    if known < 5:
        w["model"] = 0.0
    t = cfg["temperature_market"] if mkt else cfg["temperature_model"]
    comps = {"model": mod, "market": mkt, "kalshi": kal}
    final = M.temper(M.pool(comps, w), t)
    fair_ex = M.temper(M.pool({**comps, "kalshi": None}, w), t) if (mkt or w["model"]) else final
    total, src_tot = lh + la, "model"
    tots = []
    if line and line.get("total") and line.get("over") and line.get("under"):
        po, pu = M.american_prob(line["over"]), M.american_prob(line["under"])
        if po and pu:
            tots.append(M.implied_total(float(line["total"]), po / (po + pu)))
    lad = {ln: mid(q) for ln, q in ((ka or {}).get("ladder") or {}).items()}
    kt = M.ladder_total(lad)
    if kt:
        tots.append(kt)
    if tots:
        total, src_tot = sum(tots) / len(tots), "market"
    fh, fa = M.solve_grid(total, final[0] - final[2], R.rho)
    mk = M.markets(fh, fa, R.rho, R.ht.get(g["ls"], 0.445))
    mk["H"], mk["D"], mk["A"] = final
    mk["fair_ex"] = fair_ex
    kb = mid((ka or {}).get("btts"))
    if kb:      # Kalshi's both-teams-to-score price beat the model on log-loss (0.670 vs 0.681), so it leads
        mk["btts"] = cfg["btts_market_w"] * kb + (1 - cfg["btts_market_w"]) * mk["btts"]
    mk["src"] = {"dc": [r3(x) for x in mod], "market": [r3(x) for x in mkt] if mkt else None,
                 "exchange": [r3(x) for x in kal] if kal else None}
    mk["conf"] = "high" if known >= 40 else "medium" if known >= 12 else "low"
    mk["tot_src"] = src_tot
    return mk


def leg_p(mk, key):
    H, D, A = mk["H"], mk["D"], mk["A"]
    fixed = {"home": H, "draw": D, "away": A, "1X": H + D, "X2": A + D, "12": H + A}
    if key in fixed:
        return fixed[key]
    if key in ("btts_y", "btts_n"):
        return mk["btts"] if key == "btts_y" else 1 - mk["btts"]
    return M.prob(mk["grid"], M.LEGS[key])


def options(g, mk, ka):
    """Every bet you can place on this game: Kalshi (ask + fee) and DraftKings (via ESPN), with expected return."""
    out = []
    fe = mk["fair_ex"]
    pex = {"home": fe[0], "draw": fe[1], "away": fe[2]}
    nm = {"home": f"{g['h']} to win", "draw": "Draw", "away": f"{g['a']} to win"}
    no = {"home": ("X2", f"{g['a']} or draw"), "away": ("1X", f"{g['h']} or draw"), "draw": ("12", "Either team wins")}
    for s, q in ((ka or {}).get("sides") or {}).items():
        if not q:
            continue
        if q.get("ask"):
            out.append({"key": s, "label": nm[s], "venue": "Kalshi", "px": q["ask"], "p": pex[s], "ev": kev(pex[s], q["ask"]),
                        "ticker": q["ticker"], "side": "yes"})
        if q.get("bid"):
            key, lab = no[s]
            p = 1 - pex[s]
            out.append({"key": key, "label": lab, "venue": "Kalshi", "px": round(1 - q["bid"], 2), "p": p,
                        "ev": kev(p, round(1 - q["bid"], 2)), "ticker": q["ticker"], "side": "no"})
    for ln, q in ((ka or {}).get("ladder") or {}).items():
        x = int(float(ln))
        if f"o{x}.5" not in M.LEGS:
            continue
        po = leg_p(mk, f"o{x}.5")
        if q.get("ask"):
            out.append({"key": f"o{x}.5", "label": f"Over {x}.5 goals", "venue": "Kalshi", "px": q["ask"], "p": po,
                        "ev": kev(po, q["ask"]), "ticker": q["ticker"], "side": "yes"})
        if q.get("bid"):
            out.append({"key": f"u{x}.5", "label": f"Under {x}.5 goals", "venue": "Kalshi", "px": round(1 - q["bid"], 2),
                        "p": 1 - po, "ev": kev(1 - po, round(1 - q["bid"], 2)), "ticker": q["ticker"], "side": "no"})
    q = (ka or {}).get("btts")
    if q:
        pb = leg_p(mk, "btts_y")
        if q.get("ask"):
            out.append({"key": "btts_y", "label": "Both teams score", "venue": "Kalshi", "px": q["ask"], "p": pb,
                        "ev": kev(pb, q["ask"]), "ticker": q["ticker"], "side": "yes"})
        if q.get("bid"):
            out.append({"key": "btts_n", "label": "Not both teams score", "venue": "Kalshi", "px": round(1 - q["bid"], 2),
                        "p": 1 - pb, "ev": kev(1 - pb, round(1 - q["bid"], 2)), "ticker": q["ticker"], "side": "no"})
    line = g.get("line") or {}
    book = line.get("book") or "DraftKings"
    fin = {"home": mk["H"], "draw": mk["D"], "away": mk["A"]}
    for s in ("home", "draw", "away"):
        d = dec(line.get(s))
        if d:
            out.append({"key": s, "label": nm[s], "venue": book, "px": d, "p": fin[s], "ev": fin[s] * d - 1})
    if line.get("total") is not None:
        ln = float(line["total"])
        if ln - math.floor(ln) == 0.5:
            po = leg_p(mk, f"o{int(ln)}.5")
            for side, p, k in (("over", po, f"o{int(ln)}.5"), ("under", 1 - po, f"u{int(ln)}.5")):
                d = dec(line.get(side))
                if d:
                    out.append({"key": k, "label": f"{side.title()} {ln} goals", "venue": book, "px": d, "p": p, "ev": p * d - 1})
    return [o for o in out if o["ev"] is not None]


def price_txt(o):
    return f"{round(o['px'] * 100)}¢" if o["venue"] == "Kalshi" else amer(o["px"])


def picks(g, mk, line):
    keys = [("home", f"{g['h']} to win"), ("draw", "Draw"), ("away", f"{g['a']} to win"), ("1X", f"{g['h']} or draw"),
            ("X2", f"{g['a']} or draw"), ("12", "Either team wins (no draw)"), ("btts_y", "Both teams score"),
            ("btts_n", "Not both teams score")]
    keys += [(f"{s}{x}", f"{'Over' if s == 'o' else 'Under'} {x} goals") for x in ("1.5", "2.5", "3.5") for s in ("o", "u")]
    rows = []
    for k, lab in keys:
        p = min(max(leg_p(mk, k), 1e-6), 1 - 1e-6)
        book = dec((line or {}).get(k)) if k in ("home", "draw", "away") else None
        if line and line.get("total") is not None and k[1:] == f"{float(line['total']):.1f}":
            book = dec(line.get("over" if k[0] == "o" else "under"))
        tier = "strong" if p >= 0.75 else "lean" if p >= 0.6 else "toss-up"
        rows.append([lab, r3(p), round(1 / p, 2), round(book, 2) if book else None, r3(p * book - 1) if book else None, tier, k])
    rows.sort(key=lambda x: -x[1])
    val = [r for r in rows if r[4] is not None and r[4] >= 0.03]
    return {"top": [r[:6] for r in rows[:3]], "val": max(val, key=lambda r: r[4])[:6] if val else None, "best": rows[0]}


def flags(g, mk):
    out = []
    mv = (g.get("line") or {}).get("move")
    if mv and all(mv[k][0] and mv[k][1] for k in ("home", "away", "draw")):
        o = M.novig([M.american_prob(mv[k][0]) for k in ("home", "draw", "away")])
        c = M.novig([M.american_prob(mv[k][1]) for k in ("home", "draw", "away")])
        if o and c:
            for i, (s, name) in enumerate((("home", g["h"]), ("draw", "Draw"), ("away", g["a"]))):
                d = c[i] - o[i]
                if d >= 0.05:
                    out.append(["high" if d >= 0.1 else "mid", f"Line moved toward {name}",
                                f"DraftKings: {o[i] * 100:.0f}% at open, {c[i] * 100:.0f}% now", name,
                                "Money or news pushed the price; already in the fair price shown."])
    return out


# ------------------------------------------------------------------ paper log
def log_bets(L, g, opts, cfg, books):
    """Log strategy picks the first time they qualify; keep their latest pre-kickoff price for closing-line value."""
    st = cfg["strategies"]
    for b in L["bets"]:
        if b["eid"] == g["id"] and b.get("result") is None and NOW < g["ts"]:
            o = next((o for o in opts if o["key"] == b["key"] and o["venue"] == b["venue"]), None)
            if o:
                b["close_px"], b["close_p"] = o["px"], r3(o["p"])
    have = {(b["eid"], b["strat"], b["key"]) for b in L["bets"]}
    hours = (g["ts"] - NOW) / 3600
    if hours <= 0.1:
        return
    s = st["kalshi_value"]
    for o in opts:
        if o["venue"] != "Kalshi" or o["ev"] < s["min_ev"] or not s["min_price"] <= o["px"] <= s["max_price"] or hours > s["max_hours"]:
            continue
        if (g["id"], "kalshi_value", o["key"]) in have:
            continue
        ya, na = books(o["ticker"])        # the nested quote can be stale: confirm on the live order book
        ask = ya if o["side"] == "yes" else na
        if not ask or kev(o["p"], ask) < s["min_ev"]:
            continue
        L["bets"].append({"eid": g["id"], "strat": "kalshi_value", "key": o["key"], "label": o["label"], "venue": "Kalshi",
                          "game": f"{g['h']} v {g['a']}", "lg": g["lg"], "t0": g["ts"], "ts": NOW, "px": ask, "p": r3(o["p"]),
                          "ev": r3(kev(o["p"], ask)), "units": units(o["p"], ask + fee(ask), cfg, s["kelly_mult"]),
                          "close_px": ask, "result": None})


def log_best(L, g, best, cfg):
    if not best or best["rating"] != "Value" or (g["ts"] - NOW) < 360:
        return
    if any(b["eid"] == g["id"] and b["strat"] == "best_value" for b in L["bets"]):
        return
    o = best
    cost = o["px"] + fee(o["px"]) if o["venue"] == "Kalshi" else 1 / o["px"]
    L["bets"].append({"eid": g["id"], "strat": "best_value", "key": o["key"], "label": o["label"], "venue": o["venue"],
                      "game": f"{g['h']} v {g['a']}", "lg": g["lg"], "t0": g["ts"], "ts": NOW, "px": o["px"], "p": r3(o["p"]),
                      "ev": r3(o["ev"]), "units": units(o["p"], cost, cfg, cfg["strategies"]["best_value"]["kelly_mult"]),
                      "close_px": o["px"], "result": None})


def hit(key, hg, ag):
    return M.LEGS[key](hg, ag) if key in M.LEGS else None


def settle(L, done):
    for b in L["bets"]:
        if b.get("result") is not None:
            continue
        g = done.get(b["eid"])
        if not g:
            if NOW - b["t0"] > 5 * DAY:
                b["result"] = "void"
            continue
        if any(v in g["st"] for v in VOID):
            b["result"] = "void"
            continue
        won = hit(b["key"], g["hg"], g["ag"])
        b["result"] = "win" if won else "loss"
        cost = b["px"] + fee(b["px"]) if b["venue"] == "Kalshi" else 1 / b["px"]
        b["pnl"] = round(b["units"] * ((1 - cost) / cost if won else -1), 3) if b["units"] else 0.0
        b["roi1"] = round((1 - cost) / cost if won else -1, 4)     # per 1 unit, for unsized paper bets
        if b.get("close_px"):
            ccost = b["close_px"] + fee(b["close_px"]) if b["venue"] == "Kalshi" else 1 / b["close_px"]
            b["clv"] = round(ccost / cost - 1, 4)                   # >0: we paid less than the last pre-kickoff price
    for eid, p in L["preds"].items():
        g = done.get(eid)
        if not g or "gr" in p or any(v in g["st"] for v in VOID):
            continue
        hg, ag = g["hg"], g["ag"]
        r = 0 if hg > ag else 1 if hg == ag else 2
        p["gr"] = {"ll": round(-math.log(max(p["p"][r], 1e-9)), 4),
                   "mll": round(-math.log(max(p["mk"][r], 1e-9)), 4) if p.get("mk") else None,
                   "pick_hit": int(max(range(3), key=lambda i: p["p"][i]) == r),
                   "ou25_hit": int((p["o25"] > 0.5) == (hg + ag > 2)), "btts_hit": int((p["btts"] > 0.5) == (hg > 0 and ag > 0)),
                   "score_hit": int(p["ps"] == f"{hg}-{ag}"), "best_hit": int(bool(hit(p["best"][0], hg, ag))),
                   "score": f"{hg}-{ag}"}


def perf(L, backtest):
    def agg(rows):
        n = len(rows)
        if not n:
            return {"n": 0}
        m = [r for r in rows if r["gr"]["mll"] is not None]
        return {"n": n, "pick_accuracy": sum(r["gr"]["pick_hit"] for r in rows) / n,
                "ou25_accuracy": sum(r["gr"]["ou25_hit"] for r in rows) / n,
                "btts_accuracy": sum(r["gr"]["btts_hit"] for r in rows) / n,
                "exact_score": sum(r["gr"]["score_hit"] for r in rows) / n,
                "logloss": sum(r["gr"]["ll"] for r in rows) / n,
                "market_logloss": sum(r["gr"]["mll"] for r in m) / len(m) if m else None,
                "logloss_with_market": sum(r["gr"]["ll"] for r in m) / len(m) if m else None, "n_market": len(m)}
    graded = [p for p in L["preds"].values() if "gr" in p]
    recent = [p for p in graded if p["t0"] >= NOW - 30 * DAY]
    tiers = {}
    for p in graded:
        t = tiers.setdefault(p["best"][2], [0, 0, 0.0])
        t[0] += 1
        t[1] += p["gr"]["best_hit"]
        t[2] += p["best"][1]
    by_pick = [{"kind": "best", "tier": k, "n": v[0], "hit": v[1] / v[0], "model_said": v[2] / v[0], "roi": None}
               for k, v in sorted(tiers.items(), key=lambda kv: ["strong", "lean", "toss-up"].index(kv[0]))]

    def strat(name):
        bs = [b for b in L["bets"] if b["strat"] == name and b.get("result") in ("win", "loss")]
        if not bs:
            return {"bets": 0}
        cl = [b["clv"] for b in bs if b.get("clv") is not None]
        rated = {}
        for b in bs:
            r = rated.setdefault("Value", {"bets": 0, "won": 0, "roi": 0.0})
            r["bets"] += 1
            r["won"] += b["result"] == "win"
            r["roi"] += b["roi1"]
        for r in rated.values():
            r["won"] /= r["bets"]
            r["roi"] /= r["bets"]
        return {"bets": len(bs), "won": sum(b["result"] == "win" for b in bs) / len(bs),
                "roi": sum(b["roi1"] for b in bs) / len(bs), "units_pnl": round(sum(b.get("pnl") or 0 for b in bs), 2),
                "avg_ask": sum(b["px"] for b in bs) / len(bs), "profit": sum(b["roi1"] * b["px"] for b in bs),
                "clv_n": len(cl), "clv": sum(cl) / len(cl) if cl else None,
                "beat_close": sum(c > 0 for c in cl) / len(cl) if cl else None, "by_rating": rated}
    kv = strat("kalshi_value")
    return {"live30": agg(recent), "liveAll": agg(graded), "bt": backtest, "weights": None, "picks": by_pick,
            "kalshi": kv, "best_bets": strat("best_value"),
            "open_bets": [b for b in L["bets"] if b.get("result") is None][-40:]}


# ------------------------------------------------------------------ main
def run(data, cfg, verbose=True):
    data.mkdir(parents=True, exist_ok=True)
    notes = []
    f = data / "ratings.json.gz"
    R, st = load(f if f.exists() else ROOT / "state" / "ratings.json.gz")
    if not R.h2h and f.exists():       # rolling state from before head-to-head existed: take it from the repo's build
        R.h2h = load(ROOT / "state" / "ratings.json.gz")[0].h2h
    seen_list = list(st.get("seen", []))
    seen = set(seen_list)
    lf = data / "log.json"
    L = json.loads(lf.read_text(encoding="utf-8")) if lf.exists() else {"preds": {}, "bets": []}

    games = {}
    today = datetime.now(TZ).date()
    for k in range(-3, 3):
        ymd = (today + timedelta(days=k)).strftime("%Y%m%d")
        try:
            for e in S.espn_day(ymd):
                g = S.espn_game(e, R.leagues)
                if g:
                    games[g["id"]] = g
        except Exception as e:  # noqa: BLE001
            notes.append(f"espn {ymd}: {e}")
    games = list(games.values())
    added = update_ratings(R, seen, games)
    show_days = [today.isoformat(), (today + timedelta(days=1)).isoformat()]
    board = [g for g in games if local_day(g["ts"]) in show_days]
    # pre-match only: once a game kicks off Kalshi trades in-play, which must not leak into fair prices or bets
    ka = kalshi_for([g for g in board if not g["done"] and NOW < g["ts"] < NOW + cfg["horizon_hours"] * 3600
                     and g["st"] in ("STATUS_SCHEDULED", "STATUS_TBD")], notes)

    book_cache = {}

    def books(t):
        if t not in book_cache:
            try:
                book_cache[t] = S.kalshi_book(t)
            except Exception:  # noqa: BLE001
                book_cache[t] = (None, None)
        return book_cache[t]

    days = {d: [] for d in show_days}
    for g in sorted(board, key=lambda g: g["ts"]):
        out = {"id": g["id"], "lg": g["lg"], "ls": g["ls"], "ts": g["ts"], "st": g["st"], "done": g["done"],
               "h": g["h"], "a": g["a"], "hg": g["hg"], "ag": g["ag"], "ht": None, "fl": []}
        pre = L["preds"].get(g["id"])
        started = g["done"] or g["st"] not in ("STATUS_SCHEDULED", "STATUS_TBD") or g["ts"] <= NOW
        try:
            if started and pre:
                mk = None          # locked at kickoff: show what we said then
            elif started:
                mk = None          # first seen after kickoff: nothing honest to show
            else:
                mk = predict(g, R, ka.get(g["id"]), cfg)
        except Exception as e:  # noqa: BLE001
            notes.append(f"predict {g['h']} v {g['a']}: {e}")
            mk = None
        if mk:
            pk = picks(g, mk, g.get("line"))
            opts = options(g, mk, ka.get(g["id"]))
            opts.sort(key=lambda o: -o["ev"])
            cand = [o for o in opts if 0.55 <= o["p"] <= 0.88]
            best = max(cand, key=lambda o: o["ev"]) if cand else None
            if best:
                best["rating"] = rating(best["ev"])
            out["p"] = {"H": r3(mk["H"]), "D": r3(mk["D"]), "A": r3(mk["A"]),
                        "pick": ["home", "draw", "away"][max(range(3), key=lambda i: (mk["H"], mk["D"], mk["A"])[i])],
                        "ps": mk["top"][0][0], "xh": r3(mk["xh"]), "xa": r3(mk["xa"]), "rho": R.rho, "tot": r3(mk["tot"]),
                        "o15": r3(mk["o15"]), "o25": r3(mk["o25"]), "o35": r3(mk["o35"]), "btts": r3(mk["btts"]),
                        "top": [[s, r3(v)] for s, v in mk["top"]], "h1": [r3(x) for x in mk["h1"]], "h2": [r3(x) for x in mk["h2"]],
                        "tt": {s: [r3(x) for x in v] for s, v in mk["tt"].items()}, "cs": [r3(x) for x in mk["cs"]],
                        "conf": mk["conf"], "late": False, "locked": False, "src": mk["src"]}
            out["pk"] = {"top": pk["top"], "val": pk["val"]}
            if opts:
                row = lambda o: [o["label"], o["venue"], price_txt(o), r3(o["p"]), r3(o["ev"]), rating(o["ev"])]
                out["bb"] = {"best": row(best) if best else None, "list": [row(o) for o in opts[:8]]}
            k = ka.get(g["id"])
            if k:
                fe = mk["fair_ex"]
                sides = {}
                for i, s in enumerate(("home", "draw", "away")):
                    q = k["sides"].get(s)
                    if q:
                        e = fe[i] - q["ask"] - fee(q["ask"]) if q.get("ask") else None
                        sides[s] = [q.get("bid"), q.get("ask"), r3(fe[i]), r3(e)]
                out["ka"] = {"t": k["ticker"], "f": [s for s, v in sides.items() if v[3] is not None and v[3] >= 0.03], "s": sides}
            out["fl"] = flags(g, mk)
            if not started:
                L["preds"][g["id"]] = {"t0": g["ts"], "ls": g["ls"], "h": g["h"], "a": g["a"],
                                       "p": [r3(mk["H"]), r3(mk["D"]), r3(mk["A"])],
                                       "mk": mk["src"]["market"], "o25": r3(mk["o25"]), "btts": r3(mk["btts"]),
                                       "ps": mk["top"][0][0], "best": [pk["best"][6], pk["best"][1], pk["best"][5]],
                                       "card": {k2: out[k2] for k2 in ("p", "pk", "bb", "ka") if k2 in out}}
                log_bets(L, g, opts, cfg, books)
                log_best(L, g, best, cfg)
        elif pre and pre.get("card"):
            out.update(pre["card"])
            if "p" in out:
                out["p"] = {**out["p"], "locked": True}
        if pre and pre.get("gr"):
            gr = pre["gr"]
            out["gr"] = {"pick_hit": gr["pick_hit"], "score_hit": gr["score_hit"], "ou25_hit": gr["ou25_hit"], "btts_hit": gr["btts_hit"]}
        mv = (g.get("line") or {}).get("move")
        k = ka.get(g["id"])
        kx = {s: [r3(mid(q) or q.get("last")), round((q.get("vol") or 0) * (mid(q) or q.get("last") or 0))]
              for s, q in k["sides"].items() if q} if k else None
        if mv or kx:
            out["mo"] = {"dk": [], "ln": {s: [amer(dec(v[0])) if dec(v[0]) else None, amer(dec(v[1])) if dec(v[1]) else None]
                                          for s, v in mv.items()} if mv else None,
                         "ks": kx if kx and len(kx) == 3 else None, "pm": None}
        hh = R.meetings(g["hid"], g["aid"])
        if hh:
            out["h2h"] = hh
        out["form"] = {side: {"f": "".join(x[4] for x in R.form.get(tid, [])[-5:]),
                              "last": [[x[0][5:], x[1], x[2], x[3], x[4]] for x in reversed(R.form.get(tid, [])[-5:])], "v": None}
                       for side, tid in (("home", g["hid"]), ("away", g["aid"]))}
        days[local_day(g["ts"])].append(out)

    settle(L, {g["id"]: g for g in games if g["done"]})
    # keep the log small: graded predictions for 60 days (cards dropped once graded), bets for 180 days
    for eid in list(L["preds"]):
        p = L["preds"][eid]
        if "gr" in p:
            p.pop("card", None)
        if p["t0"] < NOW - 60 * DAY:
            del L["preds"][eid]
    L["bets"] = [b for b in L["bets"] if b["t0"] >= NOW - 180 * DAY]

    board_data = {"generated": NOW, "days": days, "big": BIG, "ev": None, "perf": perf(L, st.get("backtest")),
                  "cloud": {"ratings_added": added, "kalshi_games": len(ka), "notes": notes[-10:],
                            "games": sum(len(v) for v in days.values())}}
    seen_keep = (seen_list + [i for i in sorted(seen - set(seen_list))])[-8000:]     # ids already in the ratings
    save(data / "ratings.json.gz", R, {"seen": seen_keep, "backtest": st.get("backtest"), "built": st.get("built"),
                                       "updated": NOW})
    lf.write_text(json.dumps(L, separators=(",", ":")), encoding="utf-8")
    (data / "data.js").write_text("window.BOARD=" + json.dumps(board_data, separators=(",", ":")) + ";\n", encoding="utf-8")
    (data / "board.json").write_text(json.dumps(board_data["cloud"]), encoding="utf-8")
    if verbose:
        print(f"{sum(len(v) for v in days.values())} games, {added} results added, Kalshi matched {len(ka)}, "
              f"open bets {sum(1 for b in L['bets'] if b.get('result') is None)}; notes: {notes[:5]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="out")
    a = ap.parse_args()
    cfg = json.loads((ROOT / "config.json").read_text())
    try:
        run(Path(a.data), cfg)
    except Exception:
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
