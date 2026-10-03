# nflprops

Small-stakes NFL Sunday prop finder. Pulls free nflverse stats, grabs prop lines, projects each player, and writes a slate report you upload to a Claude Project for a second look.

## What it does

1. **Stats** from nflverse (free, updated nightly in season): player game logs for this season and last, schedule with Vegas spread/total, injury reports.
2. **Lines** from The Odds API (free tier, about 500 credits/month) or a CSV you fill in by hand.
3. **Projection** per player: recency-weighted average of the last 12 games, adjusted for what the opponent allows to that position and for the Vegas implied team total.
4. **Scoring**: model probability vs. the market's no-vig probability, best available price across books, EV per $1.
5. **Report**: top clean plays, flagged edges (injury, small sample, role change, big gap vs market), and cross-game parlay ideas.

## Setup (macOS)

```bash
# Python 3.11+ (skip if you already have it)
brew install python

cd ~/path/to/nfl-props
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Odds API key (optional but recommended). Free key at https://the-odds-api.com, then:

```bash
echo 'export ODDS_API_KEY="your-key-here"' >> ~/.zshrc
source ~/.zshrc
```

## Weekly routine

```bash
source .venv/bin/activate

# Saturday: pull Sunday-only props and score them (~70 credits for 5 markets)
python -m nflprops run --sunday

# Sunday morning: refresh stats + injuries, re-score the same lines (no credits used)
python -m nflprops --refresh score

# Price a parlay using ranks from the report
python -m nflprops parlay 4 9 12
python -m nflprops parlay 4 9 12 --odds 600   # compare to the book's actual parlay price

# Quick game log
python -m nflprops player "Amon-Ra St. Brown"
```

Output lands in `reports/slate_<season>_wNN.md` and `.csv`. Upload the `.md` to the Claude Project.

## No API key? Manual mode

Copy lines from your book into a CSV and score it:

```
player,market,line,over_odds,under_odds,book
Josh Allen,pass_yds,239.5,-115,-105,draftkings
Bijan Robinson,anytime_td,0.5,-150,,fanduel
```

```bash
python -m nflprops score --props data/my_props.csv
```

Markets: `pass_yds pass_tds pass_completions pass_attempts interceptions rush_yds rush_attempts receptions rec_yds rush_rec_yds anytime_td`. Leave `under_odds` blank if only one side is posted. Multiple books for the same line are fine; it averages the no-vig price and picks the best odds.

`data/sample_props.csv` has made-up Week 4 lines for testing.

## Odds API credits

Cost is markets x regions per game. Defaults: 5 markets, 1 region, 4 books (books are free, markets are not). A full 13-game Sunday is about 65 credits. Add markets with `--markets player_pass_tds player_rush_attempts` and watch the remaining-credits line it prints.

## Reading the report

- **edge**: model probability minus the market's fair probability. 3%+ is the default bar.
- **EV**: expected profit per $1 at the best listed price.
- **Flags** mean "check before betting." A 15%+ gap vs the market almost always means the market knows something the model doesn't: injury, a role change, a backup QB.
- **Parlays** are cross-game only and assume independence. Same-game legs are correlated; the `parlay` command warns you.

## Honest limits

- It's a box-score model. It doesn't know about inactives announced 90 minutes before kickoff, depth chart shuffles, or coaching tendencies. That's what the Claude review step is for.
- Early season (weeks 1-3) leans on last season's games, discounted. Rookies with no games are skipped.
- Books are good at this. Treat output as a way to pick which fun bets to make, not a money printer.

## Tuning knobs (`nflprops/model.py`, `nflprops/markets.py`)

- `DECAY`: how fast older games fade (0.85 per game)
- `PRIOR_SEASON_WT`: discount on last season (0.6)
- `OPP_SHRINK_GAMES`: how skeptical to be of early-season defense numbers (6)
- `cv_floor` per market: minimum variance, keeps small samples from looking too certain
- `env` per market: how much the Vegas team total moves the projection
