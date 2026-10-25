"""
ML prediction engine — no scikit-learn required.
Uses exponentially weighted rolling averages to forecast PTS, REB, AST for the
Hornets' next scheduled games. Compatible with Python 3.14+.

The 8-game window follows a player across seasons, so returning players are
forecast from their latest games even in the first weeks of a new season.
"""
import logging
import math
import random
from datetime import datetime

from database import (query, save_predictions, clear_predictions, save_game_predictions,
                      execute, execute_many, current_season)

logger = logging.getLogger(__name__)

MODEL_VERSION = "rolling_v2"
WINDOW = 8          # games in the rolling window
MIN_GAMES = 3       # no forecast for players with fewer games than this on record
N_FORECAST = 5      # upcoming games to forecast
HORNETS_ABBR = "CHA"

def weighted_avg(values, decay=0.85):
    """values must be in chronological order (oldest first); the newest game weighs most."""
    if not values: return 0.0
    weights = [decay ** i for i in range(len(values) - 1, -1, -1)]
    total_w = sum(weights)
    return sum(v * w for v, w in zip(values, weights)) / total_w

def std(vals):
    if len(vals) < 2: return 5.0
    mean = sum(vals) / len(vals)
    return (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5

def _minutes(v):
    """Stored minutes can be '34' or '34:12'."""
    try:
        s = str(v)
        if ":" in s:
            m, sec = s.split(":")[:2]
            return int(m) + int(sec) / 60.0
        return float(s)
    except Exception:
        return 0.0

def upcoming_games(season, limit=N_FORECAST):
    today = datetime.now().strftime("%Y-%m-%d")
    rows = query(
        "SELECT game_date, home_team, away_team FROM schedule "
        "WHERE season=? AND status='Scheduled' AND game_date >= ? "
        "ORDER BY game_date ASC, game_id ASC LIMIT ?",
        (season, today, limit)
    )
    games = []
    for r in rows:
        if r["home_team"] == HORNETS_ABBR:
            games.append({"date": r["game_date"], "opponent": f"vs {r['away_team']}"})
        else:
            games.append({"date": r["game_date"], "opponent": f"@ {r['home_team']}"})
    return games

def _history(player_id, season, window):
    """Newest-first game rows: this season's games first, topped up from earlier games
    (previous season on any team) until the window is full. Returns (rows, n_current)."""
    cols = "game_id, game_date_iso, pts, reb, ast"
    rows = query(f"SELECT {cols} FROM player_stats WHERE player_id=? AND season=? "
                 "ORDER BY game_date_iso DESC, game_id DESC LIMIT ?", (player_id, season, window))
    n_cur = len(rows)
    if n_cur < window:
        seen = {r["game_id"] for r in rows}
        older = query(
            f"SELECT {cols} FROM (SELECT {cols} FROM prior_games WHERE player_id=? "
            f"UNION SELECT {cols} FROM player_stats WHERE player_id=? AND season != ?) "
            "ORDER BY game_date_iso DESC, game_id DESC LIMIT ?",
            (player_id, player_id, season, window))
        for r in older:
            if r["game_id"] not in seen and len(rows) < window:
                rows.append(r); seen.add(r["game_id"])
    return rows, n_cur

def predict_for_player(player_id, player_name, future_games, window=WINDOW, season=None):
    season = season or current_season()
    games, n_cur = _history(player_id, season, window)
    if len(games) < MIN_GAMES: return []
    games = list(reversed(games))          # oldest -> newest, so recent games weigh more

    pts_vals = [g["pts"] for g in games]
    reb_vals = [g["reb"] for g in games]
    ast_vals = [g["ast"] for g in games]

    pts_pred = weighted_avg(pts_vals)
    reb_pred = weighted_avg(reb_vals)
    ast_pred = weighted_avg(ast_vals)

    pts_std = std(pts_vals)
    confidence = 1.0 - (pts_std / (pts_pred + 1))
    # Games from this season count fully; carried-over games from last season count half.
    sample = n_cur + 0.5 * (len(games) - n_cur)
    confidence *= min(1.0, sample / float(window))
    confidence = max(0.10, min(0.85, confidence))

    return [(
        player_id, player_name, fg["date"], fg["opponent"],
        round(max(0.0, pts_pred + random.uniform(-2, 2)), 1),
        round(max(0.0, reb_pred + random.uniform(-1, 1)), 1),
        round(max(0.0, ast_pred + random.uniform(-1, 1)), 1),
        round(confidence, 3), MODEL_VERSION, datetime.utcnow().isoformat()
    ) for fg in future_games]

# ── Team game predictions (score + winner) ───────────────────────────────────
TEAM_MODEL_VERSION = "team_v1"
N_TEAM_FORECAST = 5
PRIOR_SEASON_WEIGHT = 0.4    # a last-season game counts 0.4 of a this-season game
HOME_EDGE = 1.4              # points added to the home team, removed from the visitor (~2.8 pt swing)
MARGIN_SD = 12.0             # typical NBA single-game margin spread, for win probability
SHRINK = 0.75                # pull team ratings toward the league average (rosters change every summer)

def _prev_season(season):
    y = int(str(season)[:4]) - 1
    return f"{y}-{str(y + 1)[-2:]}"

def team_ratings(season, before=None):
    """Per-team points scored / allowed per game, blending this season with a discounted last season.
    `before` (ISO date) limits this season's games to those played before that date (for back-tests)."""
    if before:
        rows = query("SELECT team_abbr, pts, opp_pts, season FROM league_games "
                     "WHERE (season=? AND game_date_iso < ?) OR season=?",
                     (season, before, _prev_season(season)))
    else:
        rows = query("SELECT team_abbr, pts, opp_pts, season FROM league_games WHERE season IN (?, ?)",
                     (season, _prev_season(season)))
    agg, tot_w, tot_pts = {}, 0.0, 0.0
    for r in rows:
        w = 1.0 if r["season"] == season else PRIOR_SEASON_WEIGHT
        a = agg.setdefault(r["team_abbr"], [0.0, 0.0, 0.0])
        a[0] += w; a[1] += w * r["pts"]; a[2] += w * r["opp_pts"]
        tot_w += w; tot_pts += w * r["pts"]
    if not tot_w:
        return {}, 0.0
    return ({t: {"off": a[1] / a[0], "def": a[2] / a[0]} for t, a in agg.items()}, tot_pts / tot_w)

def predict_matchup(ratings, league_avg, home, opp):
    """Predicted (hornets_pts, opp_pts, hornets_win_prob) or None when a team has no ratings yet."""
    if HORNETS_ABBR not in ratings or opp not in ratings:
        return None
    c, o = ratings[HORNETS_ABBR], ratings[opp]
    cha = league_avg + SHRINK * ((c["off"] - league_avg) + (o["def"] - league_avg)) + (HOME_EDGE if home else -HOME_EDGE)
    oth = league_avg + SHRINK * ((o["off"] - league_avg) + (c["def"] - league_avg)) + (-HOME_EDGE if home else HOME_EDGE)
    margin = cha - oth
    p_win = 0.5 * (1.0 + math.erf(margin / (MARGIN_SD * math.sqrt(2.0))))
    a, b = int(round(cha)), int(round(oth))
    if a == b:                                   # never predict a tie
        if margin >= 0: a += 1
        else: b += 1
    return a, b, p_win

def run_team_predictions(season):
    today = datetime.now().strftime("%Y-%m-%d")
    games = query(
        "SELECT game_id, game_date, game_time, home_team, away_team, arena FROM schedule "
        "WHERE season=? AND status='Scheduled' AND game_date >= ? ORDER BY game_date ASC, game_id ASC LIMIT ?",
        (season, today, N_TEAM_FORECAST))
    ratings, league_avg = team_ratings(season)
    out = []
    for g in games:
        home = g["home_team"] == HORNETS_ABBR
        opp = g["away_team"] if home else g["home_team"]
        m = predict_matchup(ratings, league_avg, home, opp)
        if not m:
            continue
        a, b, p_win = m
        out.append((g["game_id"], g["game_date"], g["game_time"], opp, 1 if home else 0, g["arena"] or "",
                    a, b, round(p_win, 3), HORNETS_ABBR if a > b else opp,
                    TEAM_MODEL_VERSION, datetime.utcnow().isoformat()))
    save_game_predictions(season, out)
    logger.info(f"Team game predictions saved: {len(out)}")

def backtest_team_predictions(season):
    """Predict every finished Hornets game that has no stored prediction, using ONLY games played before it
    (plus last season). Stored with source='backtest' so they are always labelled as such."""
    done = {r["game_id"] for r in query("SELECT game_id FROM game_predictions WHERE season=?", (season,))}
    games = query("SELECT game_id, game_date_iso, pts, opp_pts, home FROM league_games "
                  "WHERE season=? AND team_abbr=? ORDER BY game_date_iso, game_id", (season, HORNETS_ABBR))
    todo = [g for g in games if g["game_id"] not in done]
    if not todo:
        return 0
    cache, rows = {}, []
    for g in todo:
        d = g["game_date_iso"]
        if d not in cache:
            cache[d] = team_ratings(season, d)
        ratings, league_avg = cache[d]
        opp_row = query("SELECT team_abbr FROM league_games WHERE season=? AND game_id=? AND team_abbr != ?",
                        (season, g["game_id"], HORNETS_ABBR))
        if not opp_row:
            continue
        opp = opp_row[0]["team_abbr"]
        m = predict_matchup(ratings, league_avg, bool(g["home"]), opp)
        if not m:
            continue
        a, b, p_win = m
        winner = HORNETS_ABBR if a > b else opp
        won = g["pts"] > g["opp_pts"]
        correct = 1 if (winner == HORNETS_ABBR) == won else 0
        rows.append((season, g["game_id"], d, None, opp, g["home"], "", a, b, round(p_win, 3), winner,
                     TEAM_MODEL_VERSION, datetime.utcnow().isoformat(),
                     g["pts"], g["opp_pts"], "W" if won else "L", correct, "backtest"))
    if rows:
        execute_many("INSERT OR IGNORE INTO game_predictions (season, game_id, game_date, game_time, opponent, home, "
                     "arena, pred_pts, pred_opp_pts, win_prob, winner, model_version, created_at, "
                     "actual_pts, actual_opp_pts, result, correct, source) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    logger.info(f"Back-tested {len(rows)} finished games for {season}")
    return len(rows)

def grade_team_predictions(season):
    """Fill in the actual result for predicted games that have since finished."""
    pending = query("SELECT game_id, winner FROM game_predictions WHERE season=? AND result IS NULL", (season,))
    graded = 0
    for r in pending:
        lg = query("SELECT pts, opp_pts FROM league_games WHERE season=? AND game_id=? AND team_abbr=?",
                   (season, r["game_id"], HORNETS_ABBR))
        if not lg:
            continue                              # not finished yet
        a, b = lg[0]["pts"], lg[0]["opp_pts"]
        won = a > b
        correct = 1 if (r["winner"] == HORNETS_ABBR) == won else 0
        execute("UPDATE game_predictions SET actual_pts=?, actual_opp_pts=?, result=?, correct=? "
                "WHERE season=? AND game_id=?", (a, b, "W" if won else "L", correct, season, r["game_id"]))
        graded += 1
    logger.info(f"Graded {graded} team predictions for {season}")

def run_all_predictions(season=None):
    season = season or current_season()
    logger.info(f"Running predictions for {season}...")
    try:
        run_team_predictions(season)
        backtest_team_predictions(season)          # games played before tracking began
        grade_team_predictions(season)             # results for predictions made before tip-off
        prev = _prev_season(season)
        if not query("SELECT 1 AS x FROM game_predictions WHERE season=? LIMIT 1", (prev,)):
            backtest_team_predictions(prev)        # one-time record for the completed season
    except Exception as e:
        logger.error(f"Team prediction error: {e}")
    future = upcoming_games(season)
    if not future:
        clear_predictions(season)
        logger.info(f"No upcoming games for {season}; forecasts cleared.")
        return
    players = query("SELECT player_id, player_name FROM roster WHERE season=?", (season,))
    all_rows = []
    for p in players:
        rows = predict_for_player(p["player_id"], p["player_name"], future, season=season)
        if rows:
            logger.info(f"Generated {len(rows)} predictions for {p['player_name']}")
        all_rows.extend(rows)
    if all_rows:
        clear_predictions(season)
        save_predictions(all_rows, season)
    logger.info("Predictions saved.")

def get_player_averages(player_id, n_games=10, season=None):
    sql = ("SELECT pts, reb, ast, fg_pct, fg3_pct, ft_pct, stl, blk, tov, min "
           "FROM player_stats WHERE player_id=?")
    params = [player_id]
    if season:
        sql += " AND season=?"
        params.append(season)
    sql += " ORDER BY game_date_iso DESC, game_id DESC LIMIT ?"
    params.append(n_games)
    games = query(sql, tuple(params))
    if not games: return {}
    def avg(key):
        vals = [g[key] for g in games if g[key] is not None]
        return round(sum(vals) / len(vals), 1) if vals else 0.0
    mins = [_minutes(g["min"]) for g in games]
    return {
        "pts": avg("pts"), "reb": avg("reb"), "ast": avg("ast"),
        "fg_pct": round(avg("fg_pct") * 100, 1),
        "fg3_pct": round(avg("fg3_pct") * 100, 1),
        "ft_pct": round(avg("ft_pct") * 100, 1),
        "stl": avg("stl"), "blk": avg("blk"), "tov": avg("tov"),
        "mpg": round(sum(mins) / len(mins), 1) if mins else 0.0,
        "games": len(games)
    }
