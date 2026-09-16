# Fantasy Compare — V1

A mobile-first fantasy football decision engine. Enter two NFL players and compare their likelihood of outscoring each other using current player stats, recent role, opponent-by-position performance, injuries, game context, weather, and fresh news headlines.

## What is live in V1

- Player autocomplete: nflverse `players.csv`
- Weekly statistics: nflverse current-season player stats
- Matchup context: nflverse schedule
- Opponent-by-position adjustment: computed from current-season weekly player stats
- Injury/practice status: nflverse injuries feed
- Outdoor weather: Open-Meteo forecast
- Fresh context: recent Google News RSS headlines (supporting context only)
- PPR / Half-PPR / Standard scoring
- A probability comparison based on context-adjusted projection + observed scoring volatility
- Visible sources and generation timestamp

## Important model behavior

V1 intentionally does **not** let an arbitrary news headline silently change a projection. Headlines are shown as fresh supporting context; structured injury data can adjust the model. This avoids turning rumor/sentiment into fake mathematical precision.

The comparison probability is calculated from two approximate outcome distributions. It is decision support, not a guarantee or a calibrated betting probability.

## Run locally

Requires Python 3.11+.

```bash
cd fantasy-compare
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Then open `http://127.0.0.1:8000`.

The first comparison may take a few seconds because the server downloads current datasets. They are cached in memory afterward (default: 30 minutes).

## Environment variables

- `NFL_SEASON=2026`
- `DATA_CACHE_SECONDS=1800`

## Deploy free/cheap

This FastAPI project is suitable for Render, Railway, Fly.io, or another Python web host. A persistent database is not required for V1.

## Next upgrades

1. Calibrate the model against historical seasons and measure accuracy.
2. Add routes/snaps/target share and advanced opportunity data.
3. Add team-level defensive efficiency, pace, implied totals and offensive-line injuries.
4. Replace generic headline RSS with a licensed/structured news provider if the project grows.
5. Cache source snapshots so every comparison can be reproduced exactly later.
6. Add “safe floor vs upside ceiling” decision modes once the baseline model is validated.

## Data notes

nflverse code/data sources have their own licenses/terms. Review upstream terms before commercializing the app. NFL trademarks and team/player imagery are not bundled into this project.
