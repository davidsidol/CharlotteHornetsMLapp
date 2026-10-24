"""
Database layer using SQLite (MSSQL-compatible schema).
When MSSQL (pyodbc) is available, swaps in seamlessly via DB_TYPE env var.

Season-aware: every game-level table carries a `season` column such as '2025-26'.
Existing rows are migrated in place (nothing is dropped or rewritten).
"""
import sqlite3
import os
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(_BASE_DIR, "hornets.db"))

# ── MSSQL support (optional) ──────────────────────────────────────────────────
USE_MSSQL = os.environ.get("USE_MSSQL", "false").lower() == "true"
MSSQL_CONN_STR = os.environ.get(
    "MSSQL_CONN_STR",
    "DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost;DATABASE=HornetsDB;Trusted_Connection=yes;"
)

# ── Seasons ───────────────────────────────────────────────────────────────────
def current_season(today=None):
    """NBA season id for a date. Oct-Dec -> 'YYYY-(YY+1)', Jan-Sep -> '(YYYY-1)-YY'."""
    d = today or datetime.now()
    start = d.year if d.month >= 10 else d.year - 1
    return f"{start}-{str(start + 1)[-2:]}"

def season_label(season):
    """'2026-27' -> '2026–27' (en dash)."""
    return str(season).replace("-", "\u2013")

def iso_date(text):
    """Parse the NBA's stored date text ('APR 12, 2026', 'Oct 30, 2025', '2026-04-12') -> '2026-04-12'.
    NBA game ids are NOT chronological (NBA Cup games get the highest ids), so ordering uses this."""
    s = str(text or "").strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:10] if fmt == "%Y-%m-%d" else s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None

# Rosters for the original 2025-26 build (the four featured players it tracked).
LEGACY_ROSTER_2025_26 = [
    (1630163, "LaMelo Ball",    "2",  "Point Guard"),
    (1641706, "Brandon Miller", "24", "Small Forward"),
    (1642851, "Kon Knueppel",   "21", "Shooting Guard"),
    (1628970, "Miles Bridges",  "0",  "Small Forward"),
]

def get_connection():
    if USE_MSSQL:
        try:
            import pyodbc
            return pyodbc.connect(MSSQL_CONN_STR)
        except Exception as e:
            logger.warning(f"MSSQL connection failed ({e}), falling back to SQLite")
    return sqlite3.connect(DB_PATH)

def _columns(cur, table):
    return {r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()}

def _add_column(cur, table, col, decl):
    if col not in _columns(cur, table):
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        logger.info(f"Migration: added {table}.{col}")

def init_db():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS player_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER,
            player_name TEXT,
            game_id TEXT,
            game_date TEXT,
            matchup TEXT,
            wl TEXT,
            pts INTEGER,
            reb INTEGER,
            ast INTEGER,
            min TEXT,
            fgm INTEGER,
            fga INTEGER,
            fg_pct REAL,
            fg3m INTEGER,
            fg3a INTEGER,
            fg3_pct REAL,
            ftm INTEGER,
            fta INTEGER,
            ft_pct REAL,
            stl INTEGER,
            blk INTEGER,
            tov INTEGER,
            plus_minus INTEGER,
            fetched_at TEXT,
            UNIQUE(player_id, game_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS team_games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id TEXT UNIQUE,
            game_date TEXT,
            matchup TEXT,
            wl TEXT,
            pts INTEGER,
            opp_pts INTEGER,
            fg_pct REAL,
            fg3_pct REAL,
            ft_pct REAL,
            reb INTEGER,
            ast INTEGER,
            stl INTEGER,
            blk INTEGER,
            tov INTEGER,
            fetched_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER,
            player_name TEXT,
            game_date TEXT,
            opponent TEXT,
            pred_pts REAL,
            pred_reb REAL,
            pred_ast REAL,
            confidence REAL,
            model_version TEXT,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS team_info (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            season TEXT,
            wins INTEGER,
            losses INTEGER,
            win_pct REAL,
            conf_rank INTEGER,
            div_rank INTEGER,
            updated_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id TEXT UNIQUE,
            game_date TEXT,
            home_team TEXT,
            away_team TEXT,
            home_score INTEGER,
            away_score INTEGER,
            status TEXT,
            arena TEXT,
            fetched_at TEXT
        )
    """)

    # League-wide team results (points for/against) that feed the team game-prediction model.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS league_games (
            season TEXT,
            game_id TEXT,
            team_abbr TEXT,
            game_date_iso TEXT,
            pts INTEGER,
            opp_pts INTEGER,
            home INTEGER,
            PRIMARY KEY (season, game_id, team_abbr)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS game_predictions (
            season TEXT,
            game_id TEXT,
            game_date TEXT,
            game_time TEXT,
            opponent TEXT,
            home INTEGER,
            arena TEXT,
            pred_pts INTEGER,
            pred_opp_pts INTEGER,
            win_prob REAL,
            winner TEXT,
            model_version TEXT,
            created_at TEXT,
            PRIMARY KEY (season, game_id)
        )
    """)

    # Last season's games for roster players (any team) so forecasts can start from day one.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS prior_games (
            player_id INTEGER,
            season TEXT,
            game_id TEXT,
            game_date_iso TEXT,
            matchup TEXT,
            pts INTEGER,
            reb INTEGER,
            ast INTEGER,
            PRIMARY KEY (player_id, game_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS prior_checked (
            player_id INTEGER,
            season TEXT,
            checked_at TEXT,
            PRIMARY KEY (player_id, season)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS roster (
            season TEXT,
            player_id INTEGER,
            player_name TEXT,
            number TEXT,
            position TEXT,
            updated_at TEXT,
            PRIMARY KEY (season, player_id)
        )
    """)

    # ── Migration: season columns ────────────────────────────────────────────
    # NBA game ids encode the season: '00225xxxxx' = 2025-26, '00226xxxxx' = 2026-27.
    season_from_gid = (
        "'20' || substr(game_id,4,2) || '-' || "
        "printf('%02d', (CAST(substr(game_id,4,2) AS INTEGER) + 1) % 100)"
    )
    for table in ("player_stats", "team_games", "schedule"):
        _add_column(cur, table, "season", "TEXT")
        cur.execute(f"UPDATE {table} SET season = {season_from_gid} "
                    f"WHERE season IS NULL AND game_id LIKE '002%'")
    _add_column(cur, "schedule", "game_time", "TEXT")
    _add_column(cur, "predictions", "season", "TEXT")

    # Sortable ISO date for player_stats / team_games (backfilled from the stored date text).
    for table in ("player_stats", "team_games"):
        _add_column(cur, table, "game_date_iso", "TEXT")
        pending = cur.execute(f"SELECT id, game_date FROM {table} WHERE game_date_iso IS NULL").fetchall()
        if pending:
            cur.executemany(f"UPDATE {table} SET game_date_iso=? WHERE id=?",
                            [(iso_date(d), i) for i, d in pending])
            logger.info(f"Migration: backfilled {len(pending)} {table}.game_date_iso values")
    # Predictions that existed before seasons were tracked belong to the 2025-26 build.
    cur.execute("UPDATE predictions SET season = '2025-26' WHERE season IS NULL")

    cur.execute("CREATE INDEX IF NOT EXISTS idx_ps_season_player ON player_stats(season, player_id, game_date_iso)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tg_season ON team_games(season, game_date_iso)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sched_season ON schedule(season, game_date)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pred_season ON predictions(season, player_id)")

    # Seed the 2025-26 roster (never overwrites anything already stored).
    now = datetime.utcnow().isoformat()
    for pid, name, num, pos in LEGACY_ROSTER_2025_26:
        cur.execute("INSERT OR IGNORE INTO roster (season, player_id, player_name, number, position, updated_at) "
                    "VALUES ('2025-26', ?, ?, ?, ?, ?)", (pid, name, num, pos, now))

    conn.commit()
    conn.close()
    logger.info("Database initialized.")

# ── Writes ────────────────────────────────────────────────────────────────────
_PLAYER_COLS = ("player_id, player_name, game_id, game_date, matchup, wl, pts, reb, ast, min, "
                "fgm, fga, fg_pct, fg3m, fg3a, fg3_pct, ftm, fta, ft_pct, stl, blk, tov, "
                "plus_minus, fetched_at, season, game_date_iso")

def upsert_player_stats(rows: list, season: str):
    conn = get_connection()
    cur = conn.cursor()
    sql = f"INSERT OR REPLACE INTO player_stats ({_PLAYER_COLS}) VALUES ({','.join('?' * 26)})"
    cur.executemany(sql, [tuple(r) + (season, iso_date(r[3])) for r in rows])
    conn.commit()
    conn.close()

def upsert_team_games(rows: list, season: str):
    conn = get_connection()
    cur = conn.cursor()
    sql = """
        INSERT OR REPLACE INTO team_games
        (game_id, game_date, matchup, wl, pts, opp_pts,
         fg_pct, fg3_pct, ft_pct, reb, ast, stl, blk, tov, fetched_at, season, game_date_iso)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    cur.executemany(sql, [tuple(r) + (season, iso_date(r[1])) for r in rows])
    conn.commit()
    conn.close()

def save_predictions(rows: list, season: str):
    """rows: (player_id, player_name, game_date, opponent, pts, reb, ast, confidence, model_version, created_at).
    Replaces ALL of a player's predictions for the season so stale forecasts never linger."""
    conn = get_connection()
    cur = conn.cursor()
    for pid in {r[0] for r in rows}:
        cur.execute("DELETE FROM predictions WHERE player_id=? AND season=?", (pid, season))
    cur.executemany("""
        INSERT INTO predictions
        (player_id, player_name, game_date, opponent, pred_pts, pred_reb, pred_ast,
         confidence, model_version, created_at, season)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, [tuple(r) + (season,) for r in rows])
    conn.commit()
    conn.close()

def clear_predictions(season: str):
    conn = get_connection()
    conn.execute("DELETE FROM predictions WHERE season=?", (season,))
    conn.commit()
    conn.close()

def save_team_info(row: tuple):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM team_info WHERE season=?", (row[0],))
    cur.execute("""
        INSERT INTO team_info (season, wins, losses, win_pct, conf_rank, div_rank, updated_at)
        VALUES (?,?,?,?,?,?,?)
    """, row)
    conn.commit()
    conn.close()

def upsert_schedule(rows: list, season: str):
    """rows: (game_id, game_date, home_team, away_team, home_score, away_score,
              status, arena, fetched_at, game_time)"""
    conn = get_connection()
    cur = conn.cursor()
    sql = """
        INSERT OR REPLACE INTO schedule
        (game_id, game_date, home_team, away_team, home_score, away_score,
         status, arena, fetched_at, game_time, season)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """
    cur.executemany(sql, [tuple(r) + (season,) for r in rows])
    conn.commit()
    conn.close()

def save_league_games(season: str, rows: list):
    """rows: (game_id, team_abbr, game_date_iso, pts, opp_pts, home)."""
    conn = get_connection()
    conn.executemany("INSERT OR REPLACE INTO league_games (season, game_id, team_abbr, game_date_iso, pts, opp_pts, home) "
                     "VALUES (?,?,?,?,?,?,?)", [(season,) + tuple(r) for r in rows])
    conn.commit()
    conn.close()

def save_game_predictions(season: str, rows: list):
    """Replaces the season's team game predictions. rows: (game_id, game_date, game_time, opponent, home, arena,
    pred_pts, pred_opp_pts, win_prob, winner, model_version, created_at)."""
    conn = get_connection()
    conn.execute("DELETE FROM game_predictions WHERE season=?", (season,))
    conn.executemany("INSERT INTO game_predictions (season, game_id, game_date, game_time, opponent, home, arena, "
                     "pred_pts, pred_opp_pts, win_prob, winner, model_version, created_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [(season,) + tuple(r) for r in rows])
    conn.commit()
    conn.close()

def save_prior_games(player_id: int, season: str, rows: list):
    """rows: (game_id, game_date_iso, matchup, pts, reb, ast). Marks the player as checked even if empty."""
    conn = get_connection()
    cur = conn.cursor()
    cur.executemany("INSERT OR REPLACE INTO prior_games (player_id, season, game_id, game_date_iso, matchup, pts, reb, ast) "
                    "VALUES (?,?,?,?,?,?,?,?)", [(player_id, season) + tuple(r) for r in rows])
    cur.execute("INSERT OR REPLACE INTO prior_checked (player_id, season, checked_at) VALUES (?,?,?)",
                (player_id, season, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

def save_roster(season: str, rows: list):
    """rows: (player_id, player_name, number, position). Replaces that season's roster."""
    conn = get_connection()
    cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    cur.execute("DELETE FROM roster WHERE season=?", (season,))
    cur.executemany("INSERT OR REPLACE INTO roster (season, player_id, player_name, number, position, updated_at) "
                    "VALUES (?,?,?,?,?,?)", [(season,) + tuple(r) + (now,) for r in rows])
    conn.commit()
    conn.close()

def query(sql, params=()):
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows
