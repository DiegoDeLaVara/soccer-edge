# Soccer Edge

A soccer prediction and betting board that refreshes itself every ~20 minutes in GitHub Actions and publishes a
phone-friendly page on GitHub Pages. No PC or Claude session needs to be running. It is the cloud copy of the PC app in
`trading_bot_V2/soccer` (same page, same research), built the same way as Tennis Edge.

## What runs where

| Piece | Where | What |
|---|---|---|
| `tools/build_history.py` | local, once | Replays ~320k ESPN results (2012-26, from the PC app's database) into `state/ratings.json.gz` and records the walk-forward backtest |
| `edge/refresh.py` | cloud, every 20 min | New results -> ratings; today's and tomorrow's games from ESPN (with DraftKings lines); Kalshi match, totals and both-teams-to-score prices; fair prices, best bets, paper record, grading |
| `site/index.html` | GitHub Pages | Best bets, all games, top picks, Kalshi, signals, model record |

Rolling state (`ratings.json.gz`, `log.json`) lives on the `gh-pages` branch next to `data.js`; the branch is
force-pushed each run so it never grows.

## Model

Online Poisson team ratings (attack, defence, league goal rate and home advantage, nudged after every result) feed a
Dixon-Coles score grid. Walk-forward over the last 12 months (24,007 matches, 105 leagues): log-loss 1.0034 vs 1.0691
for a base-rate guess, winner picked 50.6% of the time, level with the PC app's full refit.

When a game has a DraftKings line, the fair price leans almost entirely on it (weights: line 50, Kalshi 10, ratings 1),
because on 1,158 graded live games neither the ratings nor Kalshi improved on the line. The total-goals line comes from
the DraftKings total and Kalshi's over/under ladder, and both-teams-to-score is 90% Kalshi's price, which beat the
model. Every market (scores, totals, halves, team totals, combos) is read off the one re-solved grid.

## What the research found (PC app, 2026)

No rule beat closing prices: 70%+ picks won 76% but returned -1.7%, and model "value" bets returned about -9%. On
Kalshi (4,720 settled games) every taker rule lost and makers were adversely selected. So the two strategies here,
Kalshi below fair and best bet rated Value, are paper-tracked with closing-line value until their record says
otherwise.

## Local commands

```
python -m tools.build_history C:/TENNIS/trading_bot_V2/soccer/data/soccer.db   # rebuild ratings from history
python -m edge.refresh --data out                                              # one refresh into ./out
```

Not financial advice.
