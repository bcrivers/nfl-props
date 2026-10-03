"""nflprops CLI.

  python -m nflprops odds            pull prop lines from The Odds API -> data/props_<season>_wNN.csv
  python -m nflprops score           score a props CSV -> reports/slate_<season>_wNN.md (+ .csv)
  python -m nflprops run             odds + score in one shot
  python -m nflprops parlay 3 7 12   price a parlay from report ranks
  python -m nflprops player "Name"   quick game log
"""
import argparse
from pathlib import Path

import polars as pl

from . import data, model, report
from .markets import american_to_prob, norm_name

ROOT = Path(__file__).resolve().parent.parent


def _context(args):
    season = args.season or data.current_season()
    sched = data.load_schedule(season, args.refresh)
    week = args.week or data.next_week(sched)
    return season, week, sched


def cmd_odds(args):
    from .odds import fetch_props
    season, week, _ = _context(args)
    df = fetch_props(markets=args.markets, books=args.books, sunday_only=args.sunday)
    path = ROOT / "data" / f"props_{season}_w{week:02d}.csv"
    df.write_csv(path)
    print(f"Saved {df.height} lines -> {path}")
    return path


def cmd_score(args, props_path=None):
    season, week, sched = _context(args)
    props_path = Path(props_path or args.props or ROOT / "data" / f"props_{season}_w{week:02d}.csv")
    if not props_path.exists():
        raise SystemExit(f"No props file at {props_path}. Run `odds` or pass --props.")
    props = pl.read_csv(props_path, infer_schema_length=None)
    stats = data.load_stats(season, args.refresh)
    env = data.game_environment(sched, week)
    inj = data.load_injuries(season, args.refresh)
    last = stats.sort(["season", "week"]).tail(1).row(0, named=True)
    scored = model.score(props, stats, env, inj, season, week)
    md, csv = report.write(scored, env, season, week, f"{last['season']} week {last['week']}",
                           args.min_edge, ROOT / "reports")
    print(f"Report: {md}\nFull table: {csv}")
    if "ev" in scored.columns:
        top = scored.filter((pl.col("ev") > 0) & (pl.col("flags").fill_null("") == "")
                            & ~pl.col("longshot").fill_null(False)).head(10)
        print("\nTop clean plays:")
        for r in top.iter_rows(named=True):
            print(f"  {r['player']:<24} {r['market']:<12} {r['pick']:>5} {r['line']:<6g} {r['odds']:>+5}  "
                  f"proj {r['proj']:<6} edge {r['edge'] or 0:+.1%}  EV {r['ev']:+.2f}")


def cmd_run(args):
    cmd_score(args, cmd_odds(args))


def cmd_parlay(args):
    season, week, _ = _context(args)
    csv = Path(args.report or ROOT / "reports" / f"slate_{season}_w{week:02d}.csv")
    df = pl.read_csv(csv, infer_schema_length=None)
    legs = df.filter(pl.col("rank").is_in(args.ranks)).to_dicts()
    if len(legs) != len(args.ranks):
        raise SystemExit("Some ranks not found in the report.")
    for l in legs:
        print(f"  #{l['rank']} {l['player']} {l['pick']} {l['line']} {l['market']} @ {l['odds']:+} "
              f"(model {l['p_model']:.0%})")
    p = model.price_parlay(legs)
    print(f"\nModel hit chance {p['p']:.1%} | fair odds {p['fair_odds']:+} | "
          f"straight-parlay payout {p['odds']:+} | EV {p['ev']:+.2f} per $1")
    if args.odds:
        offered = american_to_prob(args.odds)
        ev = p["p"] * (1 / offered - 1) - (1 - p["p"])
        print(f"At the book's offered {args.odds:+}: EV {ev:+.2f} per $1")
    if p["same_game"]:
        print("Heads up: same-game legs. These are correlated, so the independence math above is only rough. "
              "Positively correlated legs (QB yds + his WR yds) hit together more often than this says; "
              "books know that and shade SGP prices down.")


def cmd_player(args):
    season = args.season or data.current_season()
    stats = data.load_stats(season, args.refresh)
    g = stats.filter(pl.col("name_key") == norm_name(args.name)).sort(["season", "week"], descending=True).head(args.n)
    if g.is_empty():
        raise SystemExit("No match.")
    cols = ["season", "week", "team", "opponent_team", "attempts", "passing_yards", "passing_tds", "carries",
            "rushing_yards", "targets", "receptions", "receiving_yards", "scrimmage_tds", "target_share"]
    with pl.Config(tbl_cols=-1, tbl_width_chars=200):
        print(g.select(cols))


def main():
    ap = argparse.ArgumentParser(prog="nflprops")
    ap.add_argument("--season", type=int)
    ap.add_argument("--week", type=int)
    ap.add_argument("--refresh", action="store_true", help="ignore today's cache and re-download")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def odds_args(p):
        p.add_argument("--markets", nargs="+", help="Odds API market keys (default: 5 core markets)")
        p.add_argument("--books", nargs="+", help="bookmaker keys, e.g. draftkings fanduel")
        p.add_argument("--sunday", action="store_true", help="Sunday games only (saves credits)")

    def score_args(p):
        p.add_argument("--props", help="props CSV (default: data/props_<season>_wNN.csv)")
        p.add_argument("--min-edge", type=float, default=0.03)

    p = sub.add_parser("odds"); odds_args(p); p.set_defaults(fn=cmd_odds)
    p = sub.add_parser("score"); score_args(p); p.set_defaults(fn=cmd_score)
    p = sub.add_parser("run"); odds_args(p); score_args(p); p.set_defaults(fn=cmd_run)
    p = sub.add_parser("parlay"); p.add_argument("ranks", type=int, nargs="+")
    p.add_argument("--odds", type=int, help="book's offered parlay price, e.g. 650"); p.add_argument("--report")
    p.set_defaults(fn=cmd_parlay)
    p = sub.add_parser("player"); p.add_argument("name"); p.add_argument("-n", type=int, default=8)
    p.set_defaults(fn=cmd_player)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
