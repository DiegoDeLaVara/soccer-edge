"""Public data, no keys: ESPN (fixtures, results, DraftKings lines) and Kalshi (prices)."""
import re
import time
import unicodedata
from datetime import datetime

import requests

ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
# ESPN league -> Kalshi series of the match result market (totals / BTTS share the suffix: KX...TOTAL, KX...BTTS)
SERIES = {
    "eng.1": "KXEPLGAME", "esp.1": "KXLALIGAGAME", "ita.1": "KXSERIEAGAME", "ger.1": "KXBUNDESLIGAGAME",
    "fra.1": "KXLIGUE1GAME", "uefa.champions": "KXUCLGAME", "uefa.europa": "KXUELGAME", "usa.1": "KXMLSGAME",
    "mex.1": "KXLIGAMXGAME", "bra.1": "KXBRASILEIROGAME", "bra.2": "KXBRASILEIROBGAME", "esp.2": "KXLALIGA2GAME",
    "eng.3": "KXEFLL1GAME", "eng.4": "KXEFLL2GAME", "eng.5": "KXENGNLGAME", "col.1": "KXDIMAYORGAME",
    "uru.1": "KXURYPDGAME", "arg.2": "KXARGNACBGAME", "usa.nwsl": "KXNWSLGAME", "eng.w.1": "KXEWSLGAME",
    "uefa.nations": "KXUEFANLGAME", "fifa.friendly": "KXINTLFRIENDLYGAME", "usa.usl.1": "KXUSLGAME",
    "jpn.2": "KXJ2LEAGUEGAME", "kor.1": "KXKLEAGUEGAME", "eng.2": "KXEFLCHAMPIONSHIPGAME",
    "uefa.europa.conf": "KXUECLGAME", "ned.1": "KXEREDIVISIEGAME", "por.1": "KXLIGAPORTUGALGAME",
}

S = requests.Session()
S.headers.update({"User-Agent": "soccer-edge/0.1", "Accept-Encoding": "gzip"})
_last = [0.0]


def get(url, params=None, tries=5, gap=0.08):
    for k in range(tries):
        w = _last[0] + gap - time.monotonic()
        if w > 0:
            time.sleep(w)
        _last[0] = time.monotonic()
        try:
            r = S.get(url, params=params, timeout=30)
        except requests.RequestException:
            time.sleep(1 + k)
            continue
        if r.status_code == 200:
            r.encoding = "utf-8"
            return r.json()
        if r.status_code in (400, 404):
            return None
        time.sleep((5 if r.status_code == 429 else 1.5) * (k + 1))
    return None


def tsf(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() if iso else None


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ ESPN
def espn_day(ymd):
    return (get(ESPN, {"dates": ymd, "limit": 900}) or {}).get("events", [])


def espn_game(e, leagues):
    """One ESPN event -> flat game dict (fixture, live state or result, DraftKings line)."""
    c = e["competitions"][0]
    side = {x["homeAway"]: x for x in c["competitors"]}
    if "home" not in side or "away" not in side:
        return None
    lid = re.search(r"~l:(\d+)", e.get("uid", ""))
    lid = lid.group(1) if lid else ""
    slug, name = leagues.get(lid, [f"l{lid}", ""])
    st = c["status"]["type"]
    g = {"id": e["id"], "ts": tsf(e["date"]), "ls": slug, "lg": name or (e.get("season") or {}).get("slug", ""),
         "st": st.get("name", ""), "done": bool(st.get("completed")), "neutral": bool(c.get("neutralSite")),
         "hid": side["home"]["team"]["id"], "aid": side["away"]["team"]["id"],
         "h": side["home"]["team"].get("displayName", ""), "a": side["away"]["team"].get("displayName", ""),
         "hg": None, "ag": None}
    if st.get("state") in ("in", "post"):
        g["hg"], g["ag"] = int(f(side["home"].get("score")) or 0), int(f(side["away"].get("score")) or 0)
    for o in [o for o in c.get("odds") or [] if o]:
        if "Live" in ((o.get("provider") or {}).get("name") or ""):
            continue
        ml = o.get("moneyline") or {}
        close = lambda k: f(((ml.get(k) or {}).get("close") or {}).get("odds"))
        h = (o.get("homeTeamOdds") or {}).get("moneyLine") or close("home")
        a = (o.get("awayTeamOdds") or {}).get("moneyLine") or close("away")
        d = (o.get("drawOdds") or {}).get("moneyLine") or close("draw")
        if h is None or a is None or d is None:
            continue
        line = {"book": (o.get("provider") or {}).get("name", "DraftKings"), "home": h, "draw": d, "away": a,
                "total": o.get("overUnder"), "over": o.get("overOdds"), "under": o.get("underOdds")}
        tot = (o.get("total") or {})
        if line["over"] is None and tot:
            line["over"] = f(((tot.get("over") or {}).get("close") or {}).get("odds"))
            line["under"] = f(((tot.get("under") or {}).get("close") or {}).get("odds"))
        opens = {}
        for k in ("home", "away", "draw"):
            x = ml.get(k) or {}
            opens[k] = [f((x.get("open") or {}).get("odds")), f((x.get("close") or {}).get("odds"))]
        if any(v[0] is not None for v in opens.values()):
            line["move"] = opens
        g["line"] = line
        break
    return g


# ------------------------------------------------------------------ Kalshi
def kalshi_events(series):
    out, cursor = [], None
    for _ in range(5):
        p = {"limit": 200, "status": "open", "series_ticker": series, "with_nested_markets": "true"}
        if cursor:
            p["cursor"] = cursor
        j = get(f"{KALSHI}/events", p)
        if not j:
            break
        out += j.get("events") or []
        cursor = j.get("cursor")
        if not cursor or not j.get("events"):
            break
    return out


def kalshi_book(ticker):
    """Best YES ask and best NO ask you can buy right now (Kalshi lists bids only; a YES bid is a NO offer)."""
    j = get(f"{KALSHI}/markets/{ticker}/orderbook", {"depth": 5})
    ob = (j or {}).get("orderbook_fp") or {}
    yes = [(f(p), f(q)) for p, q in ob.get("yes_dollars") or []]
    no = [(f(p), f(q)) for p, q in ob.get("no_dollars") or []]
    yes_ask = round(1 - max(no)[0], 2) if no else None
    no_ask = round(1 - max(yes)[0], 2) if yes else None
    return yes_ask, no_ask


def quote(m):
    return {"ticker": m["ticker"], "bid": f(m.get("yes_bid_dollars")), "ask": f(m.get("yes_ask_dollars")),
            "last": f(m.get("last_price_dollars")), "vol": f(m.get("volume_fp")), "oi": f(m.get("open_interest_fp"))}


def market_ts(e):
    for m in e.get("markets") or []:
        t = tsf(m.get("occurrence_datetime") or m.get("expected_expiration_time"))
        if t:
            return t - 2 * 3600
    return 0


# ------------------------------------------------------------------ names
STOP = {"fc", "cf", "sc", "ac", "afc", "cd", "ud", "sd", "the", "de", "club", "calcio", "if", "fk", "bk", "sk", "sv", "vfb",
        "vfl", "tsg", "rc", "rcd", "ca", "cr", "se", "ec", "1"}


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return [t for t in re.findall(r"[a-z0-9]+", s) if t not in STOP]


def same(a, b):
    """Team names from two sources refer to the same team (token containment, Kalshi abbreviates)."""
    x, y = norm(a), norm(b)
    if not x or not y:
        return False
    if " ".join(x) == " ".join(y):
        return True
    sx, sy = set(x), set(y)
    small, big = (sx, sy) if len(sx) <= len(sy) else (sy, sx)
    return small <= big or (len(small & big) >= 2) or any(len(t) >= 5 and t in big for t in small if len(small) == 1)
