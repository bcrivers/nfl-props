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
