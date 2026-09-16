from __future__ import annotations

import io
import math
import os
import re
import time
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

import httpx
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

APP_NAME = "Fantasy Compare"
SEASON = int(os.getenv("NFL_SEASON", "2026"))
CACHE_SECONDS = int(os.getenv("DATA_CACHE_SECONDS", "1800"))
HTTP_TIMEOUT = 20.0

URLS = {
    "players": "https://github.com/nflverse/nflverse-data/releases/download/players/players.csv",
    "stats": f"https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{SEASON}.csv",
    "injuries": f"https://github.com/nflverse/nflverse-data/releases/download/injuries/injuries_{SEASON}.csv",
    "schedule": "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv",
}

# Coordinates are team home-market approximations, used only for outdoor game weather.
TEAM_COORDS = {
    "ARI": (33.5276, -112.2626), "ATL": (33.7554, -84.4008), "BAL": (39.2780, -76.6227),
    "BUF": (42.7738, -78.7870), "CAR": (35.2258, -80.8528), "CHI": (41.8623, -87.6167),
    "CIN": (39.0954, -84.5160), "CLE": (41.5061, -81.6995), "DAL": (32.7473, -97.0945),
    "DEN": (39.7439, -105.0201), "DET": (42.3400, -83.0456), "GB": (44.5013, -88.0622),
    "HOU": (29.6847, -95.4107), "IND": (39.7601, -86.1639), "JAX": (30.3239, -81.6373),
    "KC": (39.0489, -94.4839), "LA": (33.9535, -118.3392), "LAC": (33.9535, -118.3392),
    "LV": (36.0908, -115.1830), "MIA": (25.9580, -80.2389), "MIN": (44.9736, -93.2575),
    "NE": (42.0909, -71.2643), "NO": (29.9511, -90.0812), "NYG": (40.8135, -74.0745),
    "NYJ": (40.8135, -74.0745), "PHI": (39.9008, -75.1675), "PIT": (40.4468, -80.0158),
    "SEA": (47.5952, -122.3316), "SF": (37.4030, -121.9700), "TB": (27.9759, -82.5033),
    "TEN": (36.1665, -86.7713), "WAS": (38.9078, -76.8645),
}

FANTASY_POSITIONS = {"QB", "RB", "WR", "TE"}

SCORING = {
    "standard": {"rec": 0.0},
    "half-ppr": {"rec": 0.5},
    "ppr": {"rec": 1.0},
}

app = FastAPI(title=APP_NAME)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

class CompareRequest(BaseModel):
    player_a: str
    player_b: str
    scoring: str = "half-ppr"

_cache: dict[str, tuple[float, Any]] = {}


def cache_get(key: str):
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < CACHE_SECONDS:
        return hit[1]
    return None


def cache_set(key: str, value: Any):
    _cache[key] = (time.time(), value)
    return value


def fetch_csv(name: str) -> pd.DataFrame:
    cached = cache_get(name)
    if cached is not None:
        return cached
    url = URLS[name]
    with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True, headers={"User-Agent": "FantasyCompare/0.1"}) as client:
        r = client.get(url)
        r.raise_for_status()
    df = pd.read_csv(io.BytesIO(r.content), low_memory=False)
    return cache_set(name, df)


def normalize_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def player_name_col(df: pd.DataFrame) -> str:
    for c in ["display_name", "full_name", "player_name", "name"]:
        if c in df.columns:
            return c
    raise RuntimeError("Player dataset has no supported name column")


def position_col(df: pd.DataFrame) -> str:
    for c in ["position", "position_group", "pos"]:
        if c in df.columns:
            return c
    raise RuntimeError("Player dataset has no supported position column")


def team_col(df: pd.DataFrame) -> str | None:
    for c in ["team_abbr", "team", "recent_team"]:
        if c in df.columns:
            return c
    return None


def resolve_player(raw_name: str) -> dict[str, Any]:
    players = fetch_csv("players")
    nc = player_name_col(players)
    pc = position_col(players)
    tc = team_col(players)
    work = players.copy()
    work = work[work[pc].astype(str).isin(FANTASY_POSITIONS)]
    q = normalize_name(raw_name)
    exact = work[work[nc].astype(str).map(normalize_name) == q]
    if exact.empty:
        contains = work[work[nc].astype(str).str.lower().str.contains(re.escape(raw_name.lower()), na=False)]
        if len(contains) != 1:
            raise HTTPException(404, f"Could not uniquely resolve player: {raw_name}")
        row = contains.iloc[0]
    else:
        # Prefer active/current records when duplicate names exist.
        if "status" in exact.columns:
            active = exact[exact["status"].astype(str).str.upper().isin(["ACT", "ACTIVE"])]
            row = (active if not active.empty else exact).iloc[0]
        else:
            row = exact.iloc[0]
    gsis = row.get("gsis_id")
    team = row.get(tc) if tc else None
    return {
        "id": None if pd.isna(gsis) else str(gsis),
        "name": str(row[nc]),
        "position": str(row[pc]),
        "team": None if team is None or pd.isna(team) else str(team),
        "headshot": None if "headshot" not in row or pd.isna(row.get("headshot")) else str(row.get("headshot")),
    }


def fantasy_points(row: pd.Series, scoring: str) -> float:
    rec_pts = SCORING[scoring]["rec"]
    def v(name):
        x = row.get(name, 0)
        return 0.0 if pd.isna(x) else float(x)
    return (
        v("passing_yards") * 0.04 + v("passing_tds") * 4 - v("interceptions") * 2
        + v("rushing_yards") * 0.1 + v("rushing_tds") * 6
        + v("receiving_yards") * 0.1 + v("receiving_tds") * 6 + v("receptions") * rec_pts
        + v("passing_2pt_conversions") * 2 + v("rushing_2pt_conversions") * 2 + v("receiving_2pt_conversions") * 2
        - v("fumbles_lost") * 2
    )


def current_week(schedule: pd.DataFrame) -> int:
    sched = schedule[(schedule["season"] == SEASON) & (schedule["game_type"] == "REG")].copy()
    sched["gameday"] = pd.to_datetime(sched["gameday"], errors="coerce")
    today = pd.Timestamp.now(tz="UTC").tz_localize(None).normalize()
    future = sched[sched["gameday"] >= today]
    if not future.empty:
        return int(future.sort_values(["gameday", "week"]).iloc[0]["week"])
    played = sched[sched["home_score"].notna()]
    return int(played["week"].max()) if not played.empty else 1


def get_player_stats(player: dict, scoring: str) -> tuple[pd.DataFrame, dict[str, float]]:
    stats = fetch_csv("stats").copy()
    idcol = "player_id" if "player_id" in stats.columns else ("gsis_id" if "gsis_id" in stats.columns else None)
    if idcol and player["id"]:
        rows = stats[stats[idcol].astype(str) == player["id"]].copy()
    else:
        nc = "player_display_name" if "player_display_name" in stats.columns else "player_name"
        rows = stats[stats[nc].astype(str).map(normalize_name) == normalize_name(player["name"])].copy()
    if rows.empty:
        return rows, {}
    if "season_type" in rows.columns:
        rows = rows[rows["season_type"].astype(str).str.upper().isin(["REG", "REGULAR"])]
    rows = rows.sort_values("week")
    rows["fantasy_calc"] = rows.apply(lambda r: fantasy_points(r, scoring), axis=1)
    recent = rows.tail(4)
    # Exponentially emphasize latest weeks without allowing one box score to dominate.
    weights = pd.Series(range(1, len(recent) + 1), index=recent.index, dtype=float)
    def wavg(col):
        if col not in recent.columns or recent[col].dropna().empty: return 0.0
        vals = pd.to_numeric(recent[col], errors="coerce").fillna(0)
        return float((vals * weights).sum() / weights.sum())
    m = {
        "games": float(len(rows)),
        "season_fp": float(rows["fantasy_calc"].mean()),
        "recent_fp": wavg("fantasy_calc"),
        "recent_targets": wavg("targets"),
        "recent_receptions": wavg("receptions"),
        "recent_carries": wavg("carries"),
        "recent_rec_yards": wavg("receiving_yards"),
        "recent_rush_yards": wavg("rushing_yards"),
        "recent_pass_yards": wavg("passing_yards"),
        "recent_total_tds": wavg("receiving_tds") + wavg("rushing_tds") + wavg("passing_tds"),
    }
    return rows, m


def get_matchup(player: dict, week: int, schedule: pd.DataFrame) -> dict[str, Any]:
    team = player["team"]
    # If player master data has no current team, use stats recent_team.
    if not team:
        stats = fetch_csv("stats")
        idcol = "player_id" if "player_id" in stats.columns else "gsis_id"
        if player["id"] and idcol in stats.columns:
            r = stats[stats[idcol].astype(str) == player["id"]]
            if not r.empty and "recent_team" in r.columns:
                team = str(r.sort_values("week").iloc[-1]["recent_team"])
                player["team"] = team
    s = schedule[(schedule["season"] == SEASON) & (schedule["game_type"] == "REG") & (schedule["week"] == week)]
    g = s[(s["home_team"] == team) | (s["away_team"] == team)]
    if g.empty:
        return {"week": week, "opponent": None, "home": None, "game": None}
    row = g.iloc[0]
    home = row["home_team"] == team
    opp = row["away_team"] if home else row["home_team"]
    return {"week": week, "opponent": str(opp), "home": bool(home), "game": row.to_dict()}


def opponent_position_factor(position: str, opponent: str, scoring: str, week: int) -> tuple[float, str]:
    stats = fetch_csv("stats").copy()
    if "opponent_team" not in stats.columns or "position" not in stats.columns:
        return 1.0, "Opponent-by-position history unavailable in source data."
    hist = stats[(stats["opponent_team"] == opponent) & (stats["position"] == position) & (stats["week"] < week)].copy()
    allpos = stats[(stats["position"] == position) & (stats["week"] < week)].copy()
    if hist.empty or allpos.empty:
        return 1.0, "Not enough opponent-by-position history yet."
    hist["fp"] = hist.apply(lambda r: fantasy_points(r, scoring), axis=1)
    allpos["fp"] = allpos.apply(lambda r: fantasy_points(r, scoring), axis=1)
    # Sum by opponent-game for position rooms, then compare to league average.
    if "week" in hist.columns:
        allowed = hist.groupby("week")["fp"].sum().mean()
        league = allpos.groupby(["opponent_team", "week"])["fp"].sum().mean()
    else:
        allowed, league = hist["fp"].mean(), allpos["fp"].mean()
    if not league or math.isnan(league): return 1.0, "Neutral matchup baseline."
    raw = allowed / league
    factor = max(0.86, min(1.14, raw))
    direction = "favorable" if factor > 1.035 else "difficult" if factor < 0.965 else "roughly neutral"
    return factor, f"{opponent} has been a {direction} matchup for {position}s based on fantasy production allowed this season."


def injury_context(player: dict, week: int) -> dict[str, Any]:
    try:
        inj = fetch_csv("injuries")
    except Exception:
        return {"factor": 1.0, "label": "No injury feed available", "detail": "The injury dataset could not be loaded.", "source": URLS["injuries"]}
    names = []
    for c in ["full_name", "player_name", "name"]:
        if c in inj.columns: names.append(c)
    rows = pd.DataFrame()
    if "gsis_id" in inj.columns and player["id"]:
        rows = inj[inj["gsis_id"].astype(str) == player["id"]]
    if rows.empty and names:
        rows = inj[inj[names[0]].astype(str).map(normalize_name) == normalize_name(player["name"])]
    if rows.empty:
        return {"factor": 1.0, "label": "No current injury flag", "detail": "No matching injury report entry found.", "source": URLS["injuries"]}
    if "week" in rows.columns:
        eligible = rows[pd.to_numeric(rows["week"], errors="coerce") <= week]
        if not eligible.empty: rows = eligible
    row = rows.iloc[-1]
    status = " ".join(str(row.get(c, "")) for c in ["report_status", "practice_status", "injury_status"] if c in rows.columns).lower()
    if any(k in status for k in ["out", "doubtful"]): factor = 0.72
    elif "questionable" in status: factor = 0.90
    elif any(k in status for k in ["limited", "did not participate", "dnp"]): factor = 0.94
    else: factor = 1.0
    label = status.strip().title() if status.strip() else "Listed on injury report"
    return {"factor": factor, "label": label, "detail": "Latest matching practice/injury designation from nflverse.", "source": URLS["injuries"]}


def weather_context(matchup: dict) -> dict[str, Any]:
    g = matchup.get("game") or {}
    roof = str(g.get("roof") or "").lower()
    if roof in {"dome", "closed"}:
        return {"factor": 1.0, "label": "Indoor game", "detail": "Weather adjustment is not applied in a closed/dome environment.", "source": None}
    home = g.get("home_team")
    coords = TEAM_COORDS.get(str(home))
    gameday = str(g.get("gameday") or "")[:10]
    if not coords or not gameday:
        return {"factor": 1.0, "label": "Weather neutral", "detail": "No reliable venue forecast mapping was available.", "source": None}
    lat, lon = coords
    url = ("https://api.open-meteo.com/v1/forecast?latitude=" + str(lat) + "&longitude=" + str(lon)
           + "&hourly=temperature_2m,precipitation_probability,wind_speed_10m,wind_gusts_10m&temperature_unit=fahrenheit&wind_speed_unit=mph"
           + "&timezone=auto&start_date=" + gameday + "&end_date=" + gameday)
    try:
        with httpx.Client(timeout=10.0) as client:
            data = client.get(url).json()
        hourly = data.get("hourly", {})
        temps = [x for x in hourly.get("temperature_2m", []) if x is not None]
        winds = [x for x in hourly.get("wind_speed_10m", []) if x is not None]
        gusts = [x for x in hourly.get("wind_gusts_10m", []) if x is not None]
        pops = [x for x in hourly.get("precipitation_probability", []) if x is not None]
        if not temps: raise ValueError("empty forecast")
        maxwind = max(winds or [0]); maxgust = max(gusts or [0]); maxpop = max(pops or [0]); min_temp = min(temps)
        factor = 1.0
        flags = []
        if maxwind >= 20: factor *= 0.95; flags.append(f"winds up to {maxwind:.0f} mph")
        if maxgust >= 30: factor *= 0.97; flags.append(f"gusts up to {maxgust:.0f} mph")
        if maxpop >= 60: factor *= 0.98; flags.append(f"{maxpop:.0f}% precipitation risk")
        if min_temp <= 25: factor *= 0.98; flags.append(f"temperatures near {min_temp:.0f}°F")
        return {"factor": factor, "label": "Weather watch" if flags else "Weather looks manageable", "detail": ", ".join(flags) if flags else "No major weather penalty detected in the game-day forecast.", "source": url}
    except Exception:
        return {"factor": 1.0, "label": "Forecast unavailable", "detail": "Weather data could not be loaded; no weather penalty applied.", "source": url}


def news_context(player: dict) -> list[dict[str, Any]]:
    # Headlines are supporting context only in V1; they do not directly change projection unless injury feed confirms status.
    q = quote_plus(f'"{player["name"]}" NFL when:2d')
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
            text = client.get(url).text
        root = ET.fromstring(text)
        out = []
        for item in root.findall(".//item")[:4]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            source = item.find("source")
            source_name = source.text.strip() if source is not None and source.text else "News report"
            out.append({"title": title, "url": link, "published": pub, "source": source_name})
        return out
    except Exception:
        return []


def normal_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def project(player: dict, scoring: str, week: int, schedule: pd.DataFrame) -> dict[str, Any]:
    rows, m = get_player_stats(player, scoring)
    if not m:
        raise HTTPException(422, f"No {SEASON} weekly stats found for {player['name']} yet.")
    matchup = get_matchup(player, week, schedule)
    opp = matchup.get("opponent")
    match_factor, match_reason = opponent_position_factor(player["position"], opp, scoring, week) if opp else (1.0, "No upcoming opponent was found.")
    injury = injury_context(player, week)
    weather = weather_context(matchup)

    # Blend stable season baseline with recent usage/performance. Recent box score gets only 45% weight.
    baseline = 0.55 * m["season_fp"] + 0.45 * m["recent_fp"]
    usage_bonus = 0.0
    if player["position"] in {"WR", "TE"}:
        usage_bonus = max(-1.5, min(2.0, (m["recent_targets"] - 5.0) * 0.22))
    elif player["position"] == "RB":
        opportunities = m["recent_carries"] + m["recent_targets"]
        usage_bonus = max(-1.5, min(2.3, (opportunities - 14.0) * 0.12))
    elif player["position"] == "QB":
        usage_bonus = max(-0.8, min(1.3, (m["recent_pass_yards"] - 220.0) * 0.004))

    projection = max(0.1, (baseline + usage_bonus) * match_factor * injury["factor"] * weather["factor"])
    recent_sd = float(rows["fantasy_calc"].tail(6).std(ddof=0)) if len(rows) > 1 else max(3.0, projection * 0.3)
    sd = max(3.0, min(10.0, recent_sd if not math.isnan(recent_sd) else projection * 0.3))
    floor = max(0.0, projection - 1.1 * sd)
    ceiling = projection + 1.4 * sd

    reasons = [
        {"factor": "Recent role", "impact": usage_bonus, "detail": f"Recent weighted usage: {m['recent_targets']:.1f} targets, {m['recent_carries']:.1f} carries per game."},
        {"factor": "Matchup", "impact": (match_factor - 1) * projection, "detail": match_reason},
        {"factor": "Health", "impact": (injury["factor"] - 1) * projection, "detail": injury["label"] + ". " + injury["detail"]},
        {"factor": "Weather", "impact": (weather["factor"] - 1) * projection, "detail": weather["label"] + ". " + weather["detail"]},
    ]
    return {
        "player": player,
        "week": week,
        "opponent": opp,
        "projection": round(projection, 2),
        "floor": round(floor, 2),
        "median": round(projection, 2),
        "ceiling": round(ceiling, 2),
        "sd": sd,
        "metrics": {k: round(v, 2) for k, v in m.items()},
        "reasons": reasons,
        "injury": injury,
        "weather": weather,
        "news": news_context(player),
        "sources": [
            {"label": "nflverse weekly player stats", "url": URLS["stats"], "updated": "Loaded at comparison time"},
            {"label": "nflverse injuries", "url": URLS["injuries"], "updated": "Loaded at comparison time"},
            {"label": "nflverse schedule", "url": URLS["schedule"], "updated": "Loaded at comparison time"},
        ] + ([{"label": "Open-Meteo forecast", "url": weather["source"], "updated": "Loaded at comparison time"}] if weather.get("source") else []),
    }


@app.get("/", response_class=HTMLResponse)
def home():
    path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    return HTMLResponse(open(path, "r", encoding="utf-8").read())


@app.get("/api/health")
def health():
    return {"ok": True, "season": SEASON, "name": APP_NAME}


@app.get("/api/players")
def players_search(q: str = Query(min_length=1, max_length=60)):
    df = fetch_csv("players")
    nc, pc, tc = player_name_col(df), position_col(df), team_col(df)
    w = df[df[pc].astype(str).isin(FANTASY_POSITIONS)].copy()
    needle = q.lower().strip()
    w = w[w[nc].astype(str).str.lower().str.contains(re.escape(needle), na=False)]
    if "status" in w.columns:
        active = w[w["status"].astype(str).str.upper().isin(["ACT", "ACTIVE"])]
        if not active.empty: w = active
    results = []
    seen = set()
    for _, row in w.head(12).iterrows():
        name = str(row[nc])
        if name in seen: continue
        seen.add(name)
        team = row.get(tc) if tc else None
        results.append({"name": name, "position": str(row[pc]), "team": None if team is None or pd.isna(team) else str(team)})
    return {"players": results}


@app.post("/api/compare")
def compare(req: CompareRequest):
    scoring = req.scoring.lower()
    if scoring not in SCORING:
        raise HTTPException(400, "Scoring must be standard, half-ppr, or ppr")
    if normalize_name(req.player_a) == normalize_name(req.player_b):
        raise HTTPException(400, "Choose two different players")
    schedule = fetch_csv("schedule")
    week = current_week(schedule)
    a = project(resolve_player(req.player_a), scoring, week, schedule)
    b = project(resolve_player(req.player_b), scoring, week, schedule)
    mean_diff = a["projection"] - b["projection"]
    combined_sd = math.sqrt(a["sd"] ** 2 + b["sd"] ** 2)
    p_a = normal_cdf(mean_diff / combined_sd) if combined_sd else 0.5
    p_a = max(0.08, min(0.92, p_a))
    p_b = 1 - p_a
    winner_key = "a" if p_a >= p_b else "b"
    confidence_gap = abs(p_a - 0.5)
    confidence = "High" if confidence_gap >= 0.22 else "Moderate" if confidence_gap >= 0.10 else "Low"
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "season": SEASON,
        "week": week,
        "scoring": scoring,
        "a": a,
        "b": b,
        "probability_a": round(p_a * 100, 1),
        "probability_b": round(p_b * 100, 1),
        "winner": winner_key,
        "confidence": confidence,
        "method": "Normal outcome comparison using each player's context-adjusted projection and recent scoring volatility.",
        "disclaimer": "Decision support, not certainty. News headlines are supporting context in V1 and do not alter the projection unless structured injury data confirms a status change.",
    }
