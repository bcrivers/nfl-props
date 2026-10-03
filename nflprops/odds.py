"""Pulls NFL player-prop lines from The Odds API (free tier: 500 credits/month).

Cost = (number of markets) x (number of regions) per game. With the default 5 markets and
one region, a full Sunday slate is roughly 70 credits. Set ODDS_API_KEY in your environment.
"""
import os
from datetime import datetime, timedelta, timezone

import polars as pl
import requests

from .markets import ODDS_API_MARKETS

BASE = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl"
DEFAULT_MARKETS = ["player_pass_yds", "player_rush_yds", "player_reception_yds",
                   "player_receptions", "player_anytime_td"]
DEFAULT_BOOKS = ["draftkings", "fanduel", "betmgm", "caesars"]


def fetch_props(markets=None, books=None, days_ahead: int = 7, sunday_only: bool = False) -> pl.DataFrame:
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        raise SystemExit("Set ODDS_API_KEY first (free key at https://the-odds-api.com).")
    markets = markets or DEFAULT_MARKETS
    books = books or DEFAULT_BOOKS

    events = requests.get(f"{BASE}/events", params={"apiKey": key}, timeout=30)
    events.raise_for_status()
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=days_ahead)

    rows = []
    remaining = None
    for ev in events.json():
        start = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        if not (now < start < horizon):
            continue
        # Sunday in US Eastern is still Sunday in UTC for 1pm/4pm games; SNF spills into Monday UTC
        if sunday_only and start.astimezone(timezone(timedelta(hours=-4))).weekday() != 6:
            continue
        r = requests.get(
            f"{BASE}/events/{ev['id']}/odds",
            params={"apiKey": key, "regions": "us", "markets": ",".join(markets),
                    "oddsFormat": "american", "bookmakers": ",".join(books)},
            timeout=30,
        )
        if r.status_code != 200:
            print(f"  skip {ev['away_team']} @ {ev['home_team']}: HTTP {r.status_code}")
            continue
        remaining = r.headers.get("x-requests-remaining", remaining)
        game = f"{ev['away_team']} @ {ev['home_team']}"
        for bk in r.json().get("bookmakers", []):
            for mk in bk.get("markets", []):
                ours = ODDS_API_MARKETS.get(mk["key"])
                if not ours:
                    continue
                for o in mk.get("outcomes", []):
                    rows.append(dict(
                        player=o.get("description"), market=ours, side=o["name"].lower(),
                        line=o.get("point", 0.5), price=o["price"], book=bk["key"],
                        game=game, commence=ev["commence_time"],
                    ))
    if remaining is not None:
        print(f"Odds API credits remaining this month: {remaining}")
    if not rows:
        raise SystemExit("No prop lines returned. Props usually post by Thursday/Friday.")

    long = pl.DataFrame(rows).with_columns(
        pl.col("side").replace({"yes": "over", "no": "under"})
    )
    # One row per player/market/line/book with both sides
    wide = long.pivot(on="side", index=["player", "market", "line", "book", "game", "commence"],
                      values="price", aggregate_function="first")
    for col in ("over", "under"):
        if col not in wide.columns:
            wide = wide.with_columns(pl.lit(None, dtype=pl.Int64).alias(col))
    return wide.rename({"over": "over_odds", "under": "under_odds"})


GAME_MARKETS = ["h2h", "spreads", "totals"]


def fetch_game_lines(books=None, days_ahead: int = 7, sunday_only: bool = False) -> pl.DataFrame:
    """Moneyline (h2h), spreads, and totals for the slate. One cheap call across all games."""
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        raise SystemExit("Set ODDS_API_KEY first (free key at https://the-odds-api.com).")
    books = books or DEFAULT_BOOKS
    r = requests.get(
        f"{BASE}/odds",
        params={"apiKey": key, "regions": "us", "markets": ",".join(GAME_MARKETS),
                "oddsFormat": "american", "bookmakers": ",".join(books)},
        timeout=30,
    )
    r.raise_for_status()
    rem = r.headers.get("x-requests-remaining")
    if rem is not None:
        print(f"Odds API credits remaining this month: {rem}")
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=days_ahead)

    rows = []
    for ev in r.json():
        start = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        if not (now < start < horizon):
            continue
        if sunday_only and start.astimezone(timezone(timedelta(hours=-4))).weekday() != 6:
            continue
        home, away = ev["home_team"], ev["away_team"]
        for bk in ev.get("bookmakers", []):
            rec = dict(home=home, away=away, book=bk["key"], commence=ev["commence_time"],
                       ml_home=None, ml_away=None, spread_home=None, spread_home_odds=None,
                       spread_away=None, spread_away_odds=None, total=None, over_odds=None, under_odds=None)
            for mk in bk.get("markets", []):
                for o in mk.get("outcomes", []):
                    nm = o["name"]
                    if mk["key"] == "h2h":
                        if nm == home: rec["ml_home"] = o["price"]
                        elif nm == away: rec["ml_away"] = o["price"]
                    elif mk["key"] == "spreads":
                        if nm == home: rec["spread_home"], rec["spread_home_odds"] = o.get("point"), o["price"]
                        elif nm == away: rec["spread_away"], rec["spread_away_odds"] = o.get("point"), o["price"]
                    elif mk["key"] == "totals":
                        rec["total"] = o.get("point")
                        if nm == "Over": rec["over_odds"] = o["price"]
                        elif nm == "Under": rec["under_odds"] = o["price"]
            rows.append(rec)
    if not rows:
        raise SystemExit("No game lines returned.")
    return pl.DataFrame(rows)
