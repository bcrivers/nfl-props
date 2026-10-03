"""Market definitions and odds math."""
import math
import re

# market key -> how to model it
# stat: column in nflverse player stats (or a derived column we build)
# dist: "lognormal" for yardage (right-skewed), "normal" for volume, "poisson" for low counts, "anytime" for TD yes/no
# pos: position group used for opponent-defense adjustment
# cv_floor: minimum coefficient of variation (small samples understate variance)
# env: how much game environment (implied team total) moves the projection
MARKETS = {
    "pass_yds":         dict(stat="passing_yards",        dist="lognormal",  pos="QB",  cv_floor=0.28, env=0.5),
    "pass_tds":         dict(stat="passing_tds",          dist="poisson", pos="QB",  cv_floor=0.0,  env=1.0),
    "pass_completions": dict(stat="completions",          dist="normal",  pos="QB",  cv_floor=0.20, env=0.3),
    "pass_attempts":    dict(stat="attempts",             dist="normal",  pos="QB",  cv_floor=0.18, env=0.2),
    "interceptions":    dict(stat="passing_interceptions",dist="poisson", pos="QB",  cv_floor=0.0,  env=0.0),
    "rush_yds":         dict(stat="rushing_yards",        dist="lognormal",  pos=None,  cv_floor=0.45, env=0.5),
    "rush_attempts":    dict(stat="carries",              dist="normal",  pos=None,  cv_floor=0.30, env=0.5),
    "receptions":       dict(stat="receptions",           dist="poisson", pos=None,  cv_floor=0.0,  env=0.3),
    "rec_yds":          dict(stat="receiving_yards",      dist="lognormal",  pos=None,  cv_floor=0.50, env=0.5),
    "rush_rec_yds":     dict(stat="rush_rec_yards",       dist="lognormal",  pos=None,  cv_floor=0.40, env=0.5),
    "anytime_td":       dict(stat="scrimmage_tds",        dist="anytime", pos=None,  cv_floor=0.0,  env=1.0),
}

# The Odds API market keys -> our keys
ODDS_API_MARKETS = {
    "player_pass_yds": "pass_yds",
    "player_pass_tds": "pass_tds",
    "player_pass_completions": "pass_completions",
    "player_pass_attempts": "pass_attempts",
    "player_pass_interceptions": "interceptions",
    "player_rush_yds": "rush_yds",
    "player_rush_attempts": "rush_attempts",
    "player_receptions": "receptions",
    "player_reception_yds": "rec_yds",
    "player_rush_reception_yds": "rush_rec_yds",
    "player_anytime_td": "anytime_td",
}


def american_to_prob(odds: float) -> float:
    odds = float(odds)
    return 100 / (odds + 100) if odds > 0 else -odds / (-odds + 100)


def american_to_decimal(odds: float) -> float:
    odds = float(odds)
    return 1 + odds / 100 if odds > 0 else 1 + 100 / -odds


def prob_to_american(p: float) -> int:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return round(-100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)


def decimal_to_american(d: float) -> int:
    return round((d - 1) * 100) if d >= 2 else round(-100 / (d - 1))


def devig(p_over: float, p_under: float) -> tuple[float, float]:
    """Remove the book's margin when both sides are posted (multiplicative method)."""
    total = p_over + p_under
    return p_over / total, p_under / total


def normal_over(mean: float, sd: float, line: float) -> float:
    if sd <= 0:
        return 1.0 if mean > line else 0.0
    z = (line - mean) / sd
    return 0.5 * (1 - math.erf(z / math.sqrt(2)))


def lognormal_over(mean: float, sd: float, line: float) -> float:
    """Yardage is right-skewed: a few big games pull the mean above the median, and books set
    lines near the median. Matching a lognormal to mean/sd avoids a built-in lean toward overs."""
    if mean <= 0 or line <= 0:
        return 1.0 if mean > line else 0.0
    sigma2 = math.log(1 + (sd / mean) ** 2)
    mu = math.log(mean) - sigma2 / 2
    z = (math.log(line) - mu) / math.sqrt(sigma2)
    return 0.5 * (1 - math.erf(z / math.sqrt(2)))


def poisson_over(lam: float, line: float) -> float:
    """P(X > line). For a 4.5 line that's P(X >= 5)."""
    k_max = math.floor(line)
    cdf = sum(math.exp(-lam) * lam**k / math.factorial(k) for k in range(k_max + 1))
    return max(0.0, 1 - cdf)


def norm_name(name: str) -> str:
    name = name.lower()
    name = re.sub(r"[.'’\-]", "", name)
    name = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", name)
    return re.sub(r"\s+", " ", name).strip()
