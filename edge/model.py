"""Goals model: Poisson score grid with the Dixon-Coles low-score fix, and every market read off it.

Pure Python (the cloud job installs only `requests`). The grid is 11x11, so it is cheap enough to re-solve per game.
"""
import math

MAXG = 10


def pois_row(lam):
    out, v = [], math.exp(-lam)
    for k in range(MAXG + 1):
        out.append(v)
        v *= lam / (k + 1)
    return out


def grid(lh, la, rho=0.0):
    ph, pa = pois_row(lh), pois_row(la)
    g = [[ph[i] * pa[j] for j in range(MAXG + 1)] for i in range(MAXG + 1)]
    if rho:
        g[0][0] *= 1 - lh * la * rho
        g[0][1] *= 1 + lh * rho
        g[1][0] *= 1 + la * rho
        g[1][1] *= 1 - rho
    s = sum(max(0.0, v) for r in g for v in r)
    return [[max(0.0, v) / s for v in r] for r in g]


def outcome(g):
    h = d = a = 0.0
    for i, r in enumerate(g):
        for j, v in enumerate(r):
            if i > j:
                h += v
            elif i == j:
                d += v
            else:
                a += v
    return h, d, a


def prob(g, f):
    return sum(v for i, r in enumerate(g) for j, v in enumerate(r) if f(i, j))


def solve_grid(total, diff, rho):
    """(lambda_home, lambda_away) with lh + la = total and P(home) - P(away) = diff (bisection on the share)."""
    def f(r):
        h, _, a = outcome(grid(total * r, total * (1 - r), rho))
        return (h - a) - diff
    lo, hi = 0.03, 0.97
    if f(lo) > 0:
        return total * lo, total * (1 - lo)
    if f(hi) < 0:
        return total * hi, total * (1 - hi)
    for _ in range(40):
        m = (lo + hi) / 2
        lo, hi = (m, hi) if f(m) < 0 else (lo, m)
    r = (lo + hi) / 2
    return total * r, total * (1 - r)


def implied_total(line, p_over):
    """Expected goals such that P(Poisson total > line) = p_over."""
    k = math.floor(line)

    def over(lam):
        return 1 - sum(pois_row(lam)[:k + 1])
    lo, hi = 0.2, 8.0
    for _ in range(50):
        m = (lo + hi) / 2
        lo, hi = (m, hi) if over(m) < p_over else (lo, m)
    return (lo + hi) / 2


def ladder_total(ladder):
    """Expected goals that best fits a Kalshi ladder {line: P(over line)} (least squares over a grid of lambdas)."""
    pts = [(float(ln), p) for ln, p in (ladder or {}).items() if p is not None and 0.02 < p < 0.98]
    if len(pts) < 2:
        return None
    best = None
    for i in range(30, 700):
        lam = i / 100
        row = pois_row(lam)
        err = sum((1 - sum(row[:math.floor(ln) + 1]) - p) ** 2 for ln, p in pts)
        if best is None or err < best[0]:
            best = (err, lam)
    return best[1]


def american_prob(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if x == 0:
        return None
    return 100 / (x + 100) if x > 0 else -x / (-x + 100)


def novig(ps):
    if not ps or any(p is None or p <= 0 for p in ps):
        return None
    s = sum(ps)
    return tuple(p / s for p in ps)


def pool(comps, weights):
    """Weighted geometric pooling of (home, draw, away) triples."""
    lp, ws = [0.0, 0.0, 0.0], 0.0
    for k, p in comps.items():
        w = weights.get(k, 0.0)
        if p is None or w <= 0:
            continue
        for i in range(3):
            lp[i] += w * math.log(min(max(p[i], 1e-6), 1))
        ws += w
    if not ws:
        return None
    q = [math.exp(v / ws) for v in lp]
    s = sum(q)
    return tuple(v / s for v in q)


def temper(p, t):
    q = [max(v, 1e-9) ** t for v in p]
    s = sum(q)
    return tuple(v / s for v in q)


LEGS = {
    "home": lambda i, j: i > j, "draw": lambda i, j: i == j, "away": lambda i, j: j > i,
    "1X": lambda i, j: i >= j, "X2": lambda i, j: j >= i, "12": lambda i, j: i != j,
    "btts_y": lambda i, j: i > 0 and j > 0, "btts_n": lambda i, j: i == 0 or j == 0,
    **{f"o{x}.5": (lambda x: lambda i, j: i + j > x)(x) for x in range(0, 6)},
    **{f"u{x}.5": (lambda x: lambda i, j: i + j <= x)(x) for x in range(0, 6)},
}


def markets(lh, la, rho, ht_share=0.445):
    g = grid(lh, la, rho)
    h, d, a = outcome(g)
    cells = sorted(((v, i, j) for i, r in enumerate(g) for j, v in enumerate(r)), reverse=True)
    hrow = [sum(r) for r in g]
    acol = [sum(g[i][j] for i in range(MAXG + 1)) for j in range(MAXG + 1)]
    halves = []
    for s in (ht_share, 1 - ht_share):
        hg = grid(lh * s, la * s, 0.0)
        halves.append([(lh + la) * s, prob(hg, LEGS["o0.5"]), prob(hg, LEGS["o1.5"])])
    return {
        "H": h, "D": d, "A": a, "xh": lh, "xa": la, "tot": lh + la,
        "o15": prob(g, LEGS["o1.5"]), "o25": prob(g, LEGS["o2.5"]), "o35": prob(g, LEGS["o3.5"]),
        "btts": prob(g, LEGS["btts_y"]),
        "top": [[f"{i}-{j}", v] for v, i, j in cells[:4]],
        "tt": {"home": [sum(hrow[k + 1:]) for k in range(3)], "away": [sum(acol[k + 1:]) for k in range(3)]},
        "cs": [acol[0], hrow[0]],
        "h1": halves[0], "h2": halves[1],
        "grid": g,
    }
