"""
ML prediction engine — no scikit-learn required.
Uses exponentially weighted rolling averages to forecast PTS, REB, AST.
Compatible with Python 3.15+.
"""
import logging
import random
from datetime import datetime

from database import query, save_predictions

logger = logging.getLogger(__name__)

MODEL_VERSION = "rolling_v1"

PLAYERS = {
    1630163: "LaMelo Ball",
    1641706: "Brandon Miller",
    1642851: "Kon Knueppel",
    1628970: "Miles Bridges",
}

FUTURE_GAMES = [
    {"date": "2025-04-14", "opponent": "MIL"},
    {"date": "2025-04-16", "opponent": "ATL"},
    {"date": "2025-04-18", "opponent": "PHI"},
    {"date": "2025-04-20", "opponent": "NYK"},
    {"date": "2025-04-22", "opponent": "MIA"},
]

def weighted_avg(values, decay=0.85):
    if not values: return 0.0
    weights = [decay ** i for i in range(len(values) - 1, -1, -1)]
    total_w = sum(weights)
    return sum(v * w for v, w in zip(values, weights)) / total_w

def std(vals):
    if len(vals) < 2: return 5.0
    mean = sum(vals) / len(vals)
    return (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5

def predict_for_player(player_id, player_name, window=8):
    games = query(
        "SELECT game_date, pts, reb, ast FROM player_stats WHERE player_id=? ORDER BY game_date DESC LIMIT ?",
        (player_id, window)
    )
    if not games: return []

    pts_vals = [g["pts"] for g in games]
    reb_vals = [g["reb"] for g in games]
    ast_vals = [g["ast"] for g in games]

    pts_pred = weighted_avg(pts_vals)
    reb_pred = weighted_avg(reb_vals)
    ast_pred = weighted_avg(ast_vals)

    pts_std = std(pts_vals)
    confidence = max(0.15, min(0.85, 1.0 - (pts_std / (pts_pred + 1))))

    return [(
        player_id, player_name, fg["date"], fg["opponent"],
        round(max(0.0, pts_pred + random.uniform(-2, 2)), 1),
        round(max(0.0, reb_pred + random.uniform(-1, 1)), 1),
        round(max(0.0, ast_pred + random.uniform(-1, 1)), 1),
        round(confidence, 3), MODEL_VERSION, datetime.utcnow().isoformat()
    ) for fg in FUTURE_GAMES]

def run_all_predictions():
    logger.info("Running predictions...")
    all_rows = []
    for pid, pname in PLAYERS.items():
        rows = predict_for_player(pid, pname)
        all_rows.extend(rows)
        logger.info(f"Generated {len(rows)} predictions for {pname}")
    if all_rows:
        save_predictions(all_rows)
    logger.info("Predictions saved.")

def get_player_averages(player_id, n_games=10):
    games = query(
        "SELECT pts, reb, ast, fg_pct, fg3_pct, ft_pct, stl, blk, tov FROM player_stats WHERE player_id=? ORDER BY game_date DESC LIMIT ?",
        (player_id, n_games)
    )
    if not games: return {}
    def avg(key):
        vals = [g[key] for g in games if g[key] is not None]
        return round(sum(vals) / len(vals), 1) if vals else 0.0
    return {
        "pts": avg("pts"), "reb": avg("reb"), "ast": avg("ast"),
        "fg_pct": round(avg("fg_pct") * 100, 1),
        "fg3_pct": round(avg("fg3_pct") * 100, 1),
        "ft_pct": round(avg("ft_pct") * 100, 1),
        "stl": avg("stl"), "blk": avg("blk"), "tov": avg("tov"),
        "games": len(games)
    }
