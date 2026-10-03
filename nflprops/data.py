"""Pulls free nflverse data (player stats, schedule + Vegas lines, injuries) with a daily cache."""
from datetime import date
from pathlib import Path

import nflreadpy as nfl
import polars as pl

from .markets import norm_name

CACHE = Path(__file__).resolve().parent.parent / "data" / "cache"


def current_season(today: date | None = None) -> int:
    today = today or date.today()
    return today.year if today.month >= 8 else today.year - 1


def _cached(name: str, loader, refresh: bool) -> pl.DataFrame:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{name}_{date.today().isoformat()}.parquet"
    if path.exists() and not refresh:
        return pl.read_parquet(path)
    for old in CACHE.glob(f"{name}_*.parquet"):
        old.unlink()
    df = loader()
    df.write_parquet(path)
    return df


def load_stats(season: int, refresh: bool = False) -> pl.DataFrame:
    def loader():
        df = nfl.load_player_stats([season - 1, season])
        return (
            df.filter(pl.col("season_type") == "REG")
            .with_columns(
                (pl.col("rushing_yards").fill_null(0) + pl.col("receiving_yards").fill_null(0)).alias("rush_rec_yards"),
                (pl.col("rushing_tds").fill_null(0) + pl.col("receiving_tds").fill_null(0)).alias("scrimmage_tds"),
                pl.col("player_display_name").map_elements(norm_name, return_dtype=pl.Utf8).alias("name_key"),
            )
        )
    return _cached(f"stats_{season}", loader, refresh)


def load_schedule(season: int, refresh: bool = False) -> pl.DataFrame:
    return _cached(f"schedule_{season}", lambda: nfl.load_schedules([season]), refresh)


def load_injuries(season: int, refresh: bool = False) -> pl.DataFrame:
    def loader():
        try:
            return nfl.load_injuries([season])
        except Exception:
            # Injury file can lag early in the week; run without it rather than fail
            return pl.DataFrame(schema={"gsis_id": pl.Utf8, "week": pl.Int32, "report_status": pl.Utf8,
                                        "report_primary_injury": pl.Utf8})
    return _cached(f"injuries_{season}", loader, refresh)


def next_week(schedule: pl.DataFrame) -> int:
    """First regular-season week that still has unplayed games."""
    open_games = schedule.filter((pl.col("game_type") == "REG") & pl.col("home_score").is_null())
    if open_games.is_empty():
        raise SystemExit("No unplayed regular-season games left in the schedule.")
    return int(open_games["week"].min())


def game_environment(schedule: pl.DataFrame, week: int) -> pl.DataFrame:
    """One row per team for the week: opponent, home/away, Vegas implied team total.

    nflverse spread_line is from the home team's view (positive = home favored).
    """
    g = schedule.filter((pl.col("week") == week) & (pl.col("game_type") == "REG") & pl.col("home_score").is_null())
    home = g.select(
        pl.col("home_team").alias("team"), pl.col("away_team").alias("opp"), pl.lit(True).alias("is_home"),
        "game_id", "gameday", "gametime", "total_line", "spread_line", "roof", "wind", "temp",
        ((pl.col("total_line") + pl.col("spread_line")) / 2).alias("implied_total"),
    )
    away = g.select(
        pl.col("away_team").alias("team"), pl.col("home_team").alias("opp"), pl.lit(False).alias("is_home"),
        "game_id", "gameday", "gametime", "total_line", "spread_line", "roof", "wind", "temp",
        ((pl.col("total_line") - pl.col("spread_line")) / 2).alias("implied_total"),
    )
    return pl.concat([home, away])
