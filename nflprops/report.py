"""Writes the slate report: a markdown file built to be dropped into a Claude Project, plus a full CSV."""
from datetime import datetime
from pathlib import Path

import polars as pl

from .model import suggest_parlays


def _fmt_odds(o):
    return f"+{o}" if o and o > 0 else str(o)


def _pct(x):
    return "" if x is None else f"{x:.0%}"


def write(scored: pl.DataFrame, env: pl.DataFrame, season: int, week: int, stats_through: str,
          min_edge: float, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"slate_{season}_w{week:02d}.csv"
    md_path = out_dir / f"slate_{season}_w{week:02d}.md"

    scored = scored.with_row_index("rank", offset=1)
    scored.write_csv(csv_path)

    ok = scored.filter(pl.col("note").fill_null("") == "") if "note" in scored.columns else scored
    plays = ok.filter((pl.col("ev") > 0) & (pl.col("edge").fill_null(0) >= min_edge))
    long_mask = pl.col("longshot").fill_null(False) if "longshot" in plays.columns else pl.lit(False)
    longshots = plays.filter(long_mask).sort("edge", descending=True)
    plays = plays.filter(~long_mask)
    clean = plays.filter(pl.col("flags").fill_null("") == "")
    flagged = plays.filter(pl.col("flags").fill_null("") != "")
    unmatched = scored.filter(pl.col("note").fill_null("") != "") if "note" in scored.columns else scored.head(0)

    L = []
    L.append(f"# NFL Props Slate: {season} Week {week}")
    L.append(f"Generated {datetime.now():%a %b %d %Y %I:%M %p}. Player stats through {stats_through}. "
             f"{scored.height} lines scored, {plays.height} clear the {min_edge:.0%} edge bar.")
    L.append("")
    L.append("Columns: proj = model projection. p_model = model chance the pick hits. p_fair = market's no-vig "
             "chance. edge = p_model minus p_fair. EV = expected profit per $1 at the listed odds. "
             "rank = row in the CSV, use it with the parlay command.")
    L.append("")

    L.append("## Game environment")
    L.append("| Game | Total | Implied (away / home) | Roof | Wind |")
    L.append("|---|---|---|---|---|")
    games = env.filter(~pl.col("is_home")).join(
        env.filter(pl.col("is_home")).select("game_id", pl.col("implied_total").alias("home_it")), on="game_id"
    ).sort(["gameday", "gametime"])
    for g in games.iter_rows(named=True):
        L.append(f"| {g['team']} @ {g['opp']} ({g['gameday']}) | {g['total_line']} | "
                 f"{g['implied_total']:.1f} / {g['home_it']:.1f} | {g['roof'] or ''} | {g['wind'] or ''} |")
    L.append("")

    def table(df, title, extra_flags=False):
        L.append(f"## {title}")
        if df.is_empty():
            L.append("_None._\n")
            return
        hdr = "| rank | Player | Market | Pick | Odds (book) | proj | last 5 | hit | p_model | p_fair | edge | EV |"
        L.append(hdr + (" Flags |" if extra_flags else ""))
        L.append("|" + "---|" * (12 + (1 if extra_flags else 0)))
        for r in df.iter_rows(named=True):
            line = (f"| {r['rank']} | {r['player']} ({r['pos']}, {r['team']} vs {r['opp']}) | {r['market']} | "
                    f"{r['pick']} {r['line']:g} | {_fmt_odds(r['odds'])} ({r['book']}) | {r['proj']} | "
                    f"{r['last5']} | {r['hit_last5']} | {_pct(r['p_model'])} | {_pct(r['p_fair'])} | "
                    f"{_pct(r['edge'])} | {r['ev']:+.2f} |")
            L.append(line + (f" {r['flags']} |" if extra_flags else ""))
        L.append("")

    table(clean.head(25), "Top plays (no flags)")
    table(flagged.head(15), "Edges with flags (verify before betting)", extra_flags=True)
    table(longshots.head(10), "Longshot TD lottery (odds longer than +400, for fun money only)", extra_flags=True)

    L.append("## Suggested cross-game parlays")
    L.append("Built only from unflagged legs in different games, assuming independence. "
             "Fair = model's true odds; payout = what the book pays if you parlay the listed prices.")
    L.append("")
    parlays = suggest_parlays(scored, min_edge)
    if not parlays:
        L.append("_Not enough clean legs this week._")
    for i, p in enumerate(parlays, 1):
        legs = "; ".join(f"#{l['rank']} {l['player']} {l['pick']} {l['line']:g} {l['market']}" for l in p["legs"])
        L.append(f"{i}. {legs}. Hit {p['p']:.0%}, fair {_fmt_odds(p['fair_odds'])}, "
                 f"pays {_fmt_odds(p['odds'])}, EV {p['ev']:+.2f}")
    L.append("")

    if not unmatched.is_empty():
        L.append("## Not scored")
        for r in unmatched.iter_rows(named=True):
            L.append(f"- {r['player']} {r['market']} {r['line']}: {r['note']}")
        L.append("")

    md_path.write_text("\n".join(L))
    # Stable names so a Claude Project chat always knows where to look
    (out_dir / "latest.md").write_text("\n".join(L))
    scored.write_csv(out_dir / "latest.csv")
    return md_path, csv_path
