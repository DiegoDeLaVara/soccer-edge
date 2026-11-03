"""Online Poisson team ratings: per-team attack and defence, per-league base rate and home advantage.

After every finished match each side's ratings move by LR x (goals - expected goals). Replaying 2019-26 this way
scored 1.0037 log-loss on the last 12 months of results, level with the full Dixon-Coles refit of the PC app,
so the cloud job keeps itself current from results alone.
"""
import gzip
import json
import math

LR = 0.035
HOME_LR = 0.1      # fraction of LR for a league's home advantage
BASE_LR = 0.05     # fraction of LR for a league's goal rate
FORM_N = 6


class Ratings:
    def __init__(self, st=None):
        st = st or {}
        self.att = st.get("att", {})
        self.de = st.get("de", {})
        self.n = st.get("n", {})
        self.mu = st.get("mu", {})
        self.home = st.get("home", {})
        self.rho = st.get("rho", -0.05)
        self.form = st.get("form", {})
        self.names = st.get("names", {})
        self.leagues = st.get("leagues", {})        # ESPN league id -> [slug, name]
        self.ht = st.get("ht", {})                  # league slug -> share of goals in the first half
        self.h2h = st.get("h2h", {})                # "lo|hi" team ids -> meetings since 2012 (wins from lo's side)

    def lambdas(self, h, a, lg, neutral=False):
        m = self.mu.get(lg, 0.27)
        ha = 0.0 if neutral else self.home.get(lg, 0.25)
        lh = math.exp(m + ha + self.att.get(h, 0.0) + self.de.get(a, 0.0))
        la = math.exp(m + self.att.get(a, 0.0) + self.de.get(h, 0.0))
        return lh, la

    def known(self, t):
        return self.n.get(t, 0)

    def add(self, m):
        """m: {h, a, lg, neutral, hg, ag, date, hn, an}"""
        h, a, lg = m["h"], m["a"], m["lg"]
        lh, la = self.lambdas(h, a, lg, m.get("neutral"))
        eh, ea = m["hg"] - lh, m["ag"] - la
        self.att[h] = self.att.get(h, 0.0) + LR * eh
        self.de[a] = self.de.get(a, 0.0) + LR * eh
        self.att[a] = self.att.get(a, 0.0) + LR * ea
        self.de[h] = self.de.get(h, 0.0) + LR * ea
        if not m.get("neutral"):
            self.home[lg] = self.home.get(lg, 0.25) + LR * HOME_LR * eh
        self.mu[lg] = self.mu.get(lg, 0.27) + LR * BASE_LR * (eh + ea)
        for t, opp, ha, gf, ga in ((h, m.get("an", ""), "H", m["hg"], m["ag"]), (a, m.get("hn", ""), "A", m["ag"], m["hg"])):
            self.n[t] = self.n.get(t, 0) + 1
            res = "W" if gf > ga else "D" if gf == ga else "L"
            self.form[t] = (self.form.get(t, []) + [[m.get("date", ""), opp, ha, f"{gf}-{ga}", res]])[-FORM_N:]
        lo, hi = sorted((h, a), key=str)
        r = self.h2h.setdefault(f"{lo}|{hi}", {"n": 0, "w": 0, "d": 0, "l": 0, "g": 0, "btts": 0, "o25": 0, "last": []})
        glo, ghi = (m["hg"], m["ag"]) if lo == h else (m["ag"], m["hg"])
        r["n"] += 1
        r["w" if glo > ghi else "d" if glo == ghi else "l"] += 1
        r["g"] += m["hg"] + m["ag"]
        r["btts"] += int(m["hg"] > 0 and m["ag"] > 0)
        r["o25"] += int(m["hg"] + m["ag"] > 2)
        r["last"] = [[m.get("date", ""), m.get("hn", ""), f"{m['hg']}-{m['ag']}", m.get("an", "")]] + r["last"][:4]
        if m.get("hn"):
            self.names[h] = m["hn"]
        if m.get("an"):
            self.names[a] = m["an"]

    def meetings(self, h, a):
        """Head to head from the home team's side, in the page's format (None if they have not met since 2012)."""
        lo, hi = sorted((h, a), key=str)
        r = self.h2h.get(f"{lo}|{hi}")
        if not r or not r["n"]:
            return None
        w, l = (r["w"], r["l"]) if lo == h else (r["l"], r["w"])
        n = r["n"]
        return {"n": n, "w": w, "d": r["d"], "l": l, "gpg": round(r["g"] / n, 2), "btts": round(100 * r["btts"] / n),
                "o25": round(100 * r["o25"] / n), "last": r["last"]}

    def dump(self, r=5):
        q = lambda d: {k: round(v, r) for k, v in d.items()}
        return {"att": q(self.att), "de": q(self.de), "n": self.n, "mu": q(self.mu), "home": q(self.home),
                "rho": self.rho, "form": self.form, "names": self.names, "leagues": self.leagues, "ht": self.ht,
                "h2h": self.h2h}


def load(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        st = json.load(f)
    return Ratings(st), st


def save(path, R, extra):
    out = {**R.dump(), **extra}
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    tmp.replace(path)
