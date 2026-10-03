"""Projects player stats and scores prop lines against the market.

The model is intentionally simple and transparent:
  1. Recency-weighted average of the player's last 12 games (prior season discounted)
  2. Opponent adjustment: what this defense allows to the position, shrunk toward league average
  3. Game environment: Vegas implied team total vs league average
  4. A distribution (normal / Poisson) around the projection gives P(over)
  5. Compare to the market's no-vig probability and the best available price
"""
import itertools
import math

import polars as pl

from . import markets as M

LEAGUE_TEAM_TOTAL = 22.5
DECAY = 0.85            # weight multiplier per game back
PRIOR_SEASON_WT = 0.6   # extra discount on last season's games
OPP_SHRINK_GAMES = 6    # opponent factor regresses toward 1.0 by this many phantom games
POSITIONS = ["QB", "RB", "WR", "TE"]
ASSUMED_TD_VIG = 1.08   # anytime TD rarely posts a "No"; assume ~8% hold to estimate fair price
TD_PRIOR_CARRIES = 80   # TD-per-touch rates regress toward position average by this many phantom touches
TD_PRIOR_TARGETS = 50
LONGSHOT_ODDS = 400
# Final probability = market baseline nudged by the model (books know depth charts and inactives).
MODEL_WEIGHT = 0.30     # anytime TDs longer than this stay out of top plays and parlays


def opponent_factors(stats: pl.DataFrame, season: int) -> dict:
    cols = sorted({m["stat"] for m in M.MARKETS.values()})
    cur = stats.filter((pl.col("season") == season) & pl.col("position").is_in(POSITIONS))
    if cur.is_empty():
        return {}
    per_game = cur.group_by(["opponent_team", "position", "week"]).agg([pl.col(c).fill_null(0).sum() for c in cols])
    opp = per_game.group_by(["opponent_team", "position"]).agg([pl.col(c).mean() for c in cols] + [pl.len().alias("n")])
    lg = per_game.group_by("position").agg([pl.col(c).mean().alias(f"lg_{c}") for c in cols])
    opp = opp.join(lg, on="position")
    out = {}
    for row in opp.iter_rows(named=True):
        n = row["n"]
        for c in cols:
            lg_v = row[f"lg_{c}"]
            if not lg_v:
                continue
            raw = row[c] / lg_v
            shrunk = 1 + (raw - 1) * n / (n + OPP_SHRINK_GAMES)
            out[(row["opponent_team"], row["position"], c)] = min(max(shrunk, 0.8), 1.2)
    return out


def td_rates(stats: pl.DataFrame) -> dict:
    """League TD per carry and TD per target by position (both seasons)."""
    g = stats.group_by("position").agg(
        pl.col("rushing_tds").sum(), pl.col("carries").sum(),
        pl.col("receiving_tds").sum(), pl.col("targets").sum(),
    )
    out = {}
    for r in g.iter_rows(named=True):
        out[r["position"]] = (
            (r["rushing_tds"] or 0) / r["carries"] if r["carries"] else 0.0,
            (r["receiving_tds"] or 0) / r["targets"] if r["targets"] else 0.0,
        )
    return out


def project_td(player_games: pl.DataFrame, pos: str, season: int, rates: dict, env_ratio: float):
    """Anytime TD: projected touches x shrunk TD-per-touch rate.

    Counting raw TDs lets one fluky score make a blocking TE look like a red-zone weapon.
    Volume is far more stable than scoring, so project volume, then apply a conservative rate.
    """
    g = player_games.sort(["season", "week"], descending=True).head(12)
    if g.is_empty():
        return None
    seasons = g["season"].to_list()
    w = [DECAY**i * (1 if s == season else PRIOR_SEASON_WT) for i, s in enumerate(seasons)]
    carries = [float(v or 0) for v in g["carries"].to_list()]
    targets = [float(v or 0) for v in g["targets"].to_list()]
    proj_c, _ = _weighted(carries, w)
    proj_t, _ = _weighted(targets, w)

    hist = player_games  # full two seasons for the rate
    lg_rush, lg_rec = rates.get(pos, (0.03, 0.04))
    rush_rate = ((hist["rushing_tds"].fill_null(0).sum()) + lg_rush * TD_PRIOR_CARRIES) / \
                ((hist["carries"].fill_null(0).sum()) + TD_PRIOR_CARRIES)
    rec_rate = ((hist["receiving_tds"].fill_null(0).sum()) + lg_rec * TD_PRIOR_TARGETS) / \
               ((hist["targets"].fill_null(0).sum()) + TD_PRIOR_TARGETS)
    env = min(max(1 + (env_ratio - 1) * 0.5, 0.85), 1.2)
    lam = (proj_c * rush_rate + proj_t * rec_rate) * env
    recent = [float(v or 0) for v in g["scrimmage_tds"].to_list()[:5]]
    return dict(proj=lam, sd=0.0, base=proj_c + proj_t, n_games=len(seasons),
                n_cur=sum(1 for s in seasons if s == season), recent=recent)


def consolidate_lines(props: pl.DataFrame) -> pl.DataFrame:
    """Collapse multiple books into one row per player/market/line: consensus fair prob + best prices."""
    p = props.with_columns(
        pl.col("player").map_elements(M.norm_name, return_dtype=pl.Utf8).alias("name_key"),
        pl.col("over_odds").cast(pl.Float64, strict=False),
        pl.col("under_odds").cast(pl.Float64, strict=False),
        pl.col("line").cast(pl.Float64),
    )
    if "book" not in p.columns:
        p = p.with_columns(pl.lit("manual").alias("book"))
    if "game" not in p.columns:
        p = p.with_columns(pl.lit(None, dtype=pl.Utf8).alias("game"))

    def fair_over(o, u, market):
        if o is None:
            return None
        po = M.american_to_prob(o)
        if u is None:
            return po / ASSUMED_TD_VIG if market == "anytime_td" else po / 1.045
        return M.devig(po, M.american_to_prob(u))[0]

    p = p.with_columns(
        pl.struct(["over_odds", "under_odds", "market"]).map_elements(
            lambda s: fair_over(s["over_odds"], s["under_odds"], s["market"]), return_dtype=pl.Float64
        ).alias("fair_over_book")
    )
    # Best price = highest American odds for that side
    best_over = p.sort("over_odds", descending=True, nulls_last=True).group_by(["name_key", "market", "line"]).first() \
        .select("name_key", "market", "line", pl.col("over_odds").alias("best_over"), pl.col("book").alias("best_over_book"))
    best_under = p.sort("under_odds", descending=True, nulls_last=True).group_by(["name_key", "market", "line"]).first() \
        .select("name_key", "market", "line", pl.col("under_odds").alias("best_under"), pl.col("book").alias("best_under_book"))
    agg = p.group_by(["name_key", "market", "line"]).agg(
        pl.col("player").first(), pl.col("game").first(),
        pl.col("fair_over_book").mean().alias("fair_over"), pl.col("book").n_unique().alias("n_books"),
    )
    return agg.join(best_over, on=["name_key", "market", "line"]).join(best_under, on=["name_key", "market", "line"])


def _weighted(values, weights):
    tw = sum(weights)
    mean = sum(v * w for v, w in zip(values, weights)) / tw
    var = sum(w * (v - mean) ** 2 for v, w in zip(values, weights)) / tw
    n = len(values)
    if n > 1:
        var *= n / (n - 1)
    return mean, math.sqrt(var)


def project(player_games: pl.DataFrame, market: str, season: int, opp_factor: float, env_ratio: float):
    spec = M.MARKETS[market]
    g = player_games
    if spec["pos"] == "QB":
        g = g.filter(pl.col("attempts") >= 10)
    g = g.sort(["season", "week"], descending=True).head(12)
    if g.is_empty():
        return None
    vals = [float(v or 0) for v in g[spec["stat"]].to_list()]
    seasons = g["season"].to_list()
    weights = [DECAY**i * (1 if s == season else PRIOR_SEASON_WT) for i, s in enumerate(seasons)]
    mean, sd = _weighted(vals, weights)
    env = 1 + spec["env"] * (env_ratio - 1) * 0.5
    env = min(max(env, 0.85), 1.2)
    proj = mean * opp_factor * env
    sd = max(sd, spec["cv_floor"] * proj, 0.5)
    return dict(proj=proj, sd=sd, base=mean, n_games=len(vals),
                n_cur=sum(1 for s in seasons if s == season), recent=vals[:5])


def model_prob_over(market: str, proj: float, sd: float, line: float) -> float:
    dist = M.MARKETS[market]["dist"]
    if dist == "normal":
        return M.normal_over(proj, sd, line)
    if dist == "lognormal":
        return M.lognormal_over(proj, sd, line)
    if dist == "poisson":
        return M.poisson_over(max(proj, 0.01), line)
    return 1 - math.exp(-max(proj, 0.0))  # anytime TD: P(at least one)


def score(props: pl.DataFrame, stats: pl.DataFrame, env: pl.DataFrame, injuries: pl.DataFrame,
          season: int, week: int) -> pl.DataFrame:
    lines = consolidate_lines(props)
    opp_f = opponent_factors(stats, season)
    rates = td_rates(stats)
    env_by_team = {r["team"]: r for r in env.iter_rows(named=True)}
    inj = injuries.filter(pl.col("week") == week) if "week" in injuries.columns else injuries
    inj_by_id = {r["gsis_id"]: r for r in inj.iter_rows(named=True)}

    # Most recent row per name -> identity (handles same-name players by recency)
    latest = stats.sort(["season", "week"], descending=True).group_by("name_key").first()
    ident = {r["name_key"]: r for r in latest.iter_rows(named=True)}

    out = []
    for row in lines.iter_rows(named=True):
        market = row["market"]
        base = dict(player=row["player"], market=market, line=row["line"], game=row["game"],
                    n_books=row["n_books"])
        if market not in M.MARKETS:
            out.append(base | dict(note=f"unsupported market {market}"))
            continue
        who = ident.get(row["name_key"])
        if who is None:
            out.append(base | dict(note="no stats match (rookie with no games, or name mismatch)"))
            continue
        team, pos, pid = who["team"], who["position"], who["player_id"]
        game = env_by_team.get(team)
        if game is None:
            out.append(base | dict(team=team, note="team not on this week's slate (bye or traded?)"))
            continue
        spec = M.MARKETS[market]
        opp_pos = spec["pos"] or pos
        of = opp_f.get((game["opp"], opp_pos, spec["stat"]), 1.0)
        env_ratio = (game["implied_total"] or LEAGUE_TEAM_TOTAL) / LEAGUE_TEAM_TOTAL
        pg = stats.filter(pl.col("player_id") == pid)
        if market == "anytime_td":
            pr = project_td(pg, pos, season, rates, env_ratio)
        else:
            pr = project(pg, market, season, of, env_ratio)
        if pr is None:
            out.append(base | dict(team=team, note="no qualifying games"))
            continue

        p_raw = model_prob_over(market, pr["proj"], pr["sd"], row["line"])
        fair_over = row["fair_over"]
        p_over = p_raw if fair_over is None else MODEL_WEIGHT * p_raw + (1 - MODEL_WEIGHT) * fair_over
        sides = []
        for side, p_model, odds, book, fair in (
            ("over", p_over, row["best_over"], row["best_over_book"], fair_over),
            ("under", 1 - p_over, row["best_under"], row["best_under_book"],
             None if fair_over is None else 1 - fair_over),
        ):
            if odds is None:
                continue
            dec = M.american_to_decimal(odds)
            ev = p_model * (dec - 1) - (1 - p_model)
            sides.append(dict(side=side, p_model=p_model, odds=int(odds), book=book,
                              fair=fair, edge=None if fair is None else p_model - fair, ev=ev))
        if not sides:
            continue
        best = max(sides, key=lambda s: s["ev"])

        flags = []
        ir = inj_by_id.get(pid)
        if ir and ir.get("report_status"):
            flags.append(f"{ir['report_status']} ({ir.get('report_primary_injury') or '?'})")
        cur_vals = pg.filter(pl.col("season") == season)[spec["stat"]].drop_nulls()
        prev_vals = pg.filter(pl.col("season") == season - 1)[spec["stat"]].drop_nulls()
        if market != "anytime_td" and len(cur_vals) >= 2 and len(prev_vals) >= 4:
            c_avg, p_avg = cur_vals.mean(), prev_vals.mean()
            if max(c_avg, p_avg) > 0 and abs(c_avg - p_avg) / max(c_avg, p_avg) > 0.4:
                flags.append(f"role change ({p_avg:.0f} last season -> {c_avg:.0f} this season)")
        if market != "anytime_td" and row["line"] > 0 and not (0.6 <= pr["proj"] / row["line"] <= 1.67):
            flags.append("line far from history: new role?")
        if (market in ("rec_yds", "rush_yds", "rush_rec_yds") and row["line"] < 15.5) or \
           (market == "receptions" and row["line"] < 1.5):
            flags.append("fringe player line")
        if pr["n_cur"] < 3:
            flags.append(f"only {pr['n_cur']} game{'s' if pr['n_cur'] != 1 else ''} this season")
        if best["edge"] is not None and (abs(best["edge"]) > 0.15 or
                                         (best["fair"] and best["p_model"] > 2 * best["fair"])):
            flags.append("huge gap vs market: check news/role")
        if game.get("wind") and game["wind"] >= 15 and market.startswith("pass"):
            flags.append(f"wind {game['wind']} mph")
        if pos in ("WR", "TE", "RB") and market in ("receptions", "rec_yds", "rush_rec_yds"):
            ts = pg.filter(pl.col("season") == season).sort("week", descending=True)["target_share"].drop_nulls()
            if len(ts) >= 4:
                l3, rest = ts.head(3).mean(), ts.slice(3).mean()
                if rest and abs(l3 - rest) > 0.05:
                    flags.append(f"target share {'up' if l3 > rest else 'down'} ({rest:.0%} -> {l3:.0%} last 3)")

        hits = sum(1 for v in pr["recent"] if v > row["line"])
        out.append(base | dict(
            team=team, pos=pos, opp=game["opp"], home=game["is_home"],
            implied_total=round(game["implied_total"], 1) if game["implied_total"] else None,
            proj=round(pr["proj"], 1), base_avg=round(pr["base"], 1), opp_adj=round(of, 3),
            n_games=pr["n_games"], last5=", ".join(f"{v:g}" for v in pr["recent"]),
            hit_last5=f"{hits}/{len(pr['recent'])}",
            pick=best["side"], odds=best["odds"], book=best["book"],
            p_model=round(best["p_model"], 3),
            p_fair=None if best["fair"] is None else round(best["fair"], 3),
            edge=None if best["edge"] is None else round(best["edge"], 3),
            ev=round(best["ev"], 3), flags="; ".join(flags), note="",
            longshot=bool(market == "anytime_td" and best["side"] == "over" and best["odds"] > LONGSHOT_ODDS),
        ))
    df = pl.DataFrame(out, infer_schema_length=None)
    if "ev" in df.columns:
        df = df.sort("ev", descending=True, nulls_last=True)
    return df


def suggest_parlays(scored: pl.DataFrame, min_edge: float, top_n: int = 15, max_legs: int = 3,
                    limit: int = 8) -> list[dict]:
    """Cross-game parlays from the strongest single legs. Same-game combos are skipped on purpose:
    books price SGPs with their own correlation model, so straight multiplication misprices them."""
    if "ev" not in scored.columns:
        return []
    legs = scored.filter(
        (pl.col("ev") > 0) & (pl.col("edge") >= min_edge) & (pl.col("flags").fill_null("") == "")
        & ~pl.col("longshot").fill_null(False)
    ).head(top_n).to_dicts()
    combos = []
    for k in range(2, max_legs + 1):
        for combo in itertools.combinations(legs, k):
            if len({c["game"] or c["team"] for c in combo}) < k:
                continue
            p = math.prod(c["p_model"] for c in combo)
            dec = math.prod(M.american_to_decimal(c["odds"]) for c in combo)
            combos.append(dict(legs=combo, p=p, odds=M.decimal_to_american(dec),
                               fair_odds=M.prob_to_american(p), ev=p * (dec - 1) - (1 - p)))
    combos.sort(key=lambda c: c["ev"], reverse=True)
    return combos[:limit]


def price_parlay(legs: list[dict]) -> dict:
    p = math.prod(l["p_model"] for l in legs)
    dec = math.prod(M.american_to_decimal(l["odds"]) for l in legs)
    games = [l.get("game") or l.get("team") for l in legs]
    return dict(p=p, odds=M.decimal_to_american(dec), fair_odds=M.prob_to_american(p),
                ev=p * (dec - 1) - (1 - p), same_game=len(set(games)) < len(games))


TEAM_ABBR = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL", "Buffalo Bills": "BUF",
    "Carolina Panthers": "CAR", "Chicago Bears": "CHI", "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE",
    "Dallas Cowboys": "DAL", "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX", "Kansas City Chiefs": "KC",
    "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC", "Los Angeles Rams": "LA", "Miami Dolphins": "MIA",
    "Minnesota Vikings": "MIN", "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT", "San Francisco 49ers": "SF",
    "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB", "Tennessee Titans": "TEN", "Washington Commanders": "WAS",
}


def _team_scoring(schedule: pl.DataFrame, season: int, week: int) -> dict:
    """Points scored and allowed per team so far this season (regular season, played games)."""
    g = schedule.filter((pl.col("game_type") == "REG") & (pl.col("week") < week)
                        & pl.col("home_score").is_not_null())
    rows = []
    for r in g.iter_rows(named=True):
        rows.append(dict(team=r["home_team"], pf=r["home_score"], pa=r["away_score"]))
        rows.append(dict(team=r["away_team"], pf=r["away_score"], pa=r["home_score"]))
    if not rows:
        return {}
    df = pl.DataFrame(rows).group_by("team").agg(
        pl.col("pf").mean().alias("pf"), pl.col("pa").mean().alias("pa"), pl.len().alias("n"))
    return {r["team"]: r for r in df.iter_rows(named=True)}


def score_game_lines(lines: pl.DataFrame, schedule: pl.DataFrame, season: int, week: int) -> pl.DataFrame:
    """Reference view: market line next to a simple model total and win probability.

    NOT an edge finder. Game lines are the sharpest markets in sports; a box-score model will not
    beat them. This exists so you see context (implied win %, model's total) when you bet.
    """
    import math
    sc = _team_scoring(schedule, season, week)
    lg_pts = 0.0
    if sc:
        lg_pts = sum(v["pf"] for v in sc.values()) / len(sc)
    else:
        lg_pts = LEAGUE_TEAM_TOTAL

    # Consensus market numbers across books, then best price per side
    agg = lines.group_by(["home", "away"]).agg(
        pl.col("ml_home").median(), pl.col("ml_away").median(),
        pl.col("spread_home").median(), pl.col("total").median(),
        pl.col("commence").first(), pl.col("book").n_unique().alias("n_books"),
    )
    out = []
    for r in agg.iter_rows(named=True):
        home, away = TEAM_ABBR.get(r["home"], r["home"]), TEAM_ABBR.get(r["away"], r["away"])
        h, a = sc.get(home), sc.get(away)
        # Model projected points: average of (team offense) and (opponent defense allowed)
        if h and a:
            proj_home = (h["pf"] + a["pa"]) / 2
            proj_away = (a["pf"] + h["pa"]) / 2
        else:
            proj_home = proj_away = lg_pts
        proj_home += 1.3  # modest home-field bump
        proj_away -= 1.3
        model_total = proj_home + proj_away
        model_margin = proj_home - proj_away  # positive = home favored
        # Win prob from margin: NFL games have ~13.5 pt SD on final margin
        model_home_wp = 0.5 * (1 + math.erf(model_margin / (13.5 * math.sqrt(2))))

        # Market no-vig win prob from the moneylines
        mkt_home_wp = None
        if r["ml_home"] is not None and r["ml_away"] is not None:
            ph, pa_ = M.american_to_prob(r["ml_home"]), M.american_to_prob(r["ml_away"])
            mkt_home_wp = M.devig(ph, pa_)[0]

        out.append(dict(
            game=f"{away} @ {home}", commence=r["commence"], n_books=r["n_books"],
            spread_home=r["spread_home"], total=r["total"],
            ml_home=int(r["ml_home"]) if r["ml_home"] is not None else None,
            ml_away=int(r["ml_away"]) if r["ml_away"] is not None else None,
            mkt_home_wp=None if mkt_home_wp is None else round(mkt_home_wp, 3),
            model_total=round(model_total, 1),
            model_spread_home=round(-model_margin, 1),  # shown in spread convention (favorite negative)
            model_home_wp=round(model_home_wp, 3),
            games_played=(h["n"] if h else 0),
        ))
    df = pl.DataFrame(out, infer_schema_length=None)
    return df.sort("commence")
