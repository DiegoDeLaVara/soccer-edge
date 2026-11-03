"""Build state/ratings.json.gz by replaying the PC soccer app's match history (ESPN results since 2012).

  python -m tools.build_history C:/TENNIS/trading_bot_V2/soccer/data/soccer.db

Also writes the walk-forward record of the last 12 months (each match predicted before its result is added), which the
page shows as the backtest, the low-score correction rho and each league's first-half goal share.
"""
import math
import sqlite3
import sys
import time
from pathlib import Path

from edge import model as M
from edge.ratings import Ratings, save

ROOT = Path(__file__).resolve().parents[1]


def main(db):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
    c.row_factory = sqlite3.Row
    R = Ratings()
    R.leagues = {r["id"]: [r["slug"], r["name"]] for r in c.execute("select slug, id, name from leagues")}
    rows = c.execute("""select id, league, ts, date, neutral, home_id, away_id, home, away, coalesce(hg90, hg) hg,
        coalesce(ag90, ag) ag, hthg, htag from matches where completed = 1 and hg is not null order by ts""").fetchall()
    now = time.time()
    test_from = now - 365 * 86400
    rho_rows, bt = [], {"n": 0, "ll": 0.0, "pick": 0, "ou": 0, "btts": 0, "base": 0.0}
    ht = {}
    leagues = set()
    for r in rows:
        m = {"h": r["home_id"], "a": r["away_id"], "lg": r["league"], "neutral": bool(r["neutral"]),
             "hg": int(r["hg"]), "ag": int(r["ag"]), "date": r["date"][:10], "hn": r["home"], "an": r["away"]}
        if r["ts"] >= now - 730 * 86400:
            lh, la = R.lambdas(m["h"], m["a"], m["lg"], m["neutral"])
            rho_rows.append((lh, la, m["hg"], m["ag"]))
            if r["ts"] >= test_from and R.known(m["h"]) >= 10 and R.known(m["a"]) >= 10:
                mk = M.markets(lh, la, R.rho)
                res = 0 if m["hg"] > m["ag"] else 1 if m["hg"] == m["ag"] else 2
                p = (mk["H"], mk["D"], mk["A"])
                bt["n"] += 1
                bt["ll"] += -math.log(max(p[res], 1e-9))
                bt["base"] += -math.log([0.45, 0.27, 0.28][res])
                bt["pick"] += int(max(range(3), key=lambda i: p[i]) == res)
                bt["ou"] += int((mk["o25"] > 0.5) == (m["hg"] + m["ag"] > 2))
                bt["btts"] += int((mk["btts"] > 0.5) == (m["hg"] > 0 and m["ag"] > 0))
                leagues.add(m["lg"])
        if r["hthg"] is not None and r["ts"] >= now - 3 * 365 * 86400:
            s = ht.setdefault(m["lg"], [0, 0])
            s[0] += r["hthg"] + r["htag"]
            s[1] += m["hg"] + m["ag"]
        R.add(m)
    # low-score correction: best rho for the last two years' expected goals
    best = None
    for k in range(-15, 6):
        rho = k / 100
        ll = 0.0
        for lh, la, hg, ag in rho_rows:
            if hg <= 1 and ag <= 1:
                t = {(0, 0): 1 - lh * la * rho, (0, 1): 1 + lh * rho, (1, 0): 1 + la * rho, (1, 1): 1 - rho}[(hg, ag)]
                ll -= math.log(max(t, 1e-9))
        if best is None or ll < best[0]:
            best = (ll, rho)
    R.rho = best[1]
    R.ht = {lg: round(a / b, 3) for lg, (a, b) in ht.items() if b >= 150}
    n = bt["n"]
    backtest = {"matches": n, "leagues": len(leagues), "logloss": round(bt["ll"] / n, 4),
                "logloss_baseline": round(bt["base"] / n, 4), "pick_accuracy": round(bt["pick"] / n, 4),
                "ou25_accuracy": round(bt["ou"] / n, 4), "btts_accuracy": round(bt["btts"] / n, 4)}
    seen = [r["id"] for r in rows if r["ts"] >= now - 6 * 86400]
    (ROOT / "state").mkdir(exist_ok=True)
    save(ROOT / "state" / "ratings.json.gz", R, {"seen": seen, "backtest": backtest, "built": now, "updated": now})
    print(f"{len(rows)} matches replayed, {len(R.att)} teams, rho {R.rho}, backtest {backtest}")


if __name__ == "__main__":
    main(sys.argv[1])
