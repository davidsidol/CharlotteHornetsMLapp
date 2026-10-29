"""
Database layer: SQLite (default) or Microsoft SQL Server.

Season-aware: every game-level table carries a `season` column such as '2025-26'.
Existing SQLite rows are migrated in place (nothing is dropped or rewritten).

The backend is picked by environment variables:
  DB_BACKEND=sqlite   DB_PATH (default hornets.db next to this file)
  DB_BACKEND=mssql    MSSQL_SERVER (e.g. IDOLAPP01\\IDOLMSSQL, later an AG listener), MSSQL_DATABASE (default
                      HornetsDB), MSSQL_DRIVER (default "ODBC Driver 18 for SQL Server"). Windows authentication
                      only: the process identity (the service account) is the SQL login. No fallback to SQLite.
Callers write SQLite-flavoured SQL with ? placeholders; the few dialect differences (LIMIT, upserts) are
translated here.
"""
import sqlite3
import os
import re
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_BACKEND = os.environ.get("DB_BACKEND", "sqlite").lower()
DB_PATH = os.environ.get("DB_PATH", os.path.join(_BASE_DIR, "hornets.db"))
MSSQL_SERVER = os.environ.get("MSSQL_SERVER", "localhost")
MSSQL_DATABASE = os.environ.get("MSSQL_DATABASE", "HornetsDB")
MSSQL_DRIVER = os.environ.get("MSSQL_DRIVER", "ODBC Driver 18 for SQL Server")
if DB_BACKEND not in ("sqlite", "mssql"):
    raise ValueError(f"DB_BACKEND must be 'sqlite' or 'mssql', not {DB_BACKEND!r}")
MSSQL = DB_BACKEND == "mssql"
if MSSQL:
    import pyodbc

# ── Seasons ───────────────────────────────────────────────────────────────────
def current_season(today=None):
    """NBA season id for a date. Oct-Dec -> 'YYYY-(YY+1)', Jan-Sep -> '(YYYY-1)-YY'."""
    d = today or datetime.now()
    start = d.year if d.month >= 10 else d.year - 1
    return f"{start}-{str(start + 1)[-2:]}"

def season_label(season):
    """'2026-27' -> '2026–27' (en dash)."""
    return str(season).replace("-", "–")

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

# ── Schema ────────────────────────────────────────────────────────────────────
# (table, columns, key, has_id). Column types are generic (TEXT / INTEGER / REAL) and mapped per backend.
# has_id tables get an auto-numbered `id` primary key and `key` is a UNIQUE constraint; otherwise `key` is the
# primary key. Upserts match on `key`.
TABLES = [
    ("player_stats", """player_id INTEGER, player_name TEXT, game_id TEXT, game_date TEXT, matchup TEXT, wl TEXT,
        pts INTEGER, reb INTEGER, ast INTEGER, min TEXT, fgm INTEGER, fga INTEGER, fg_pct REAL,
        fg3m INTEGER, fg3a INTEGER, fg3_pct REAL, ftm INTEGER, fta INTEGER, ft_pct REAL,
        stl INTEGER, blk INTEGER, tov INTEGER, plus_minus INTEGER, fetched_at TEXT,
        season TEXT, game_date_iso TEXT""", ("player_id", "game_id"), True),
    ("team_games", """game_id TEXT, game_date TEXT, matchup TEXT, wl TEXT, pts INTEGER, opp_pts INTEGER,
        fg_pct REAL, fg3_pct REAL, ft_pct REAL, reb INTEGER, ast INTEGER, stl INTEGER, blk INTEGER, tov INTEGER,
        fetched_at TEXT, season TEXT, game_date_iso TEXT""", ("game_id",), True),
    ("predictions", """player_id INTEGER, player_name TEXT, game_date TEXT, opponent TEXT,
        pred_pts REAL, pred_reb REAL, pred_ast REAL, confidence REAL, model_version TEXT, created_at TEXT,
        season TEXT""", (), True),
    ("team_info", """season TEXT, wins INTEGER, losses INTEGER, win_pct REAL, conf_rank INTEGER, div_rank INTEGER,
        updated_at TEXT""", (), True),
    ("schedule", """game_id TEXT, game_date TEXT, home_team TEXT, away_team TEXT, home_score INTEGER,
        away_score INTEGER, status TEXT, arena TEXT, fetched_at TEXT, season TEXT, game_time TEXT""",
     ("game_id",), True),
    # League-wide team results (points for/against) that feed the team game-prediction model.
    ("league_games", """season TEXT, game_id TEXT, team_abbr TEXT, game_date_iso TEXT, pts INTEGER,
        opp_pts INTEGER, home INTEGER""", ("season", "game_id", "team_abbr"), False),
    ("game_predictions", """season TEXT, game_id TEXT, game_date TEXT, game_time TEXT, opponent TEXT, home INTEGER,
        arena TEXT, pred_pts INTEGER, pred_opp_pts INTEGER, win_prob REAL, winner TEXT, model_version TEXT,
        created_at TEXT, actual_pts INTEGER, actual_opp_pts INTEGER, result TEXT, correct INTEGER,
        source TEXT""", ("season", "game_id"), False),
    # Last season's games for roster players (any team) so forecasts can start from day one.
    ("prior_games", """player_id INTEGER, season TEXT, game_id TEXT, game_date_iso TEXT, matchup TEXT,
        pts INTEGER, reb INTEGER, ast INTEGER""", ("player_id", "game_id"), False),
    ("prior_checked", "player_id INTEGER, season TEXT, checked_at TEXT", ("player_id", "season"), False),
    ("roster", """season TEXT, player_id INTEGER, player_name TEXT, number TEXT, position TEXT,
        updated_at TEXT""", ("season", "player_id"), False),
]
INDEXES = [
    ("idx_ps_season_player", "player_stats", "season, player_id, game_date_iso"),
    ("idx_tg_season", "team_games", "season, game_date_iso"),
    ("idx_sched_season", "schedule", "season, game_date"),
    ("idx_pred_season", "predictions", "season, player_id"),
]
KEYS = {t: key for t, _, key, _ in TABLES}
HAS_ID = {t for t, _, _, has_id in TABLES if has_id}
_SHORT_TEXT = {"season", "game_id", "game_date", "game_date_iso", "game_time", "fetched_at", "created_at",
               "updated_at", "checked_at", "team_abbr", "home_team", "away_team", "wl", "result", "status"}

def table_columns(table):
    cols = next(c for t, c, _, _ in TABLES if t == table)
    return [c.strip().split(None, 1) for c in cols.split(",")]

def _mssql_type(col, typ, key):
    if typ == "TEXT":
        typ = "NVARCHAR(40)" if col in _SHORT_TEXT else "NVARCHAR(64)" if col in key else "NVARCHAR(400)"
    else:
        typ = {"INTEGER": "INT", "REAL": "FLOAT"}[typ]
    return f"{typ} NOT NULL" if col in key else typ

def _create_sql(table, cols, key, has_id):
    if MSSQL:
        body = ", ".join(f"{c} {_mssql_type(c, t, key)}" for c, t in table_columns(table))
        if has_id:
            body = f"id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_{table} PRIMARY KEY, {body}"
            if key:
                body += f", CONSTRAINT UQ_{table} UNIQUE ({', '.join(key)})"
        else:
            body += f", CONSTRAINT PK_{table} PRIMARY KEY ({', '.join(key)})"
        return f"IF OBJECT_ID(N'dbo.{table}', N'U') IS NULL CREATE TABLE dbo.{table} ({body})"
    if has_id:
        cols = f"id INTEGER PRIMARY KEY AUTOINCREMENT, {cols}" + (f", UNIQUE({', '.join(key)})" if key else "")
    else:
        cols = f"{cols}, PRIMARY KEY ({', '.join(key)})"
    return f"CREATE TABLE IF NOT EXISTS {table} ({cols})"

def _index_sql(name, table, cols):
    if MSSQL:
        return (f"IF INDEXPROPERTY(OBJECT_ID(N'dbo.{table}'), N'{name}', 'IndexID') IS NULL "
                f"CREATE INDEX {name} ON dbo.{table}({cols})")
    return f"CREATE INDEX IF NOT EXISTS {name} ON {table}({cols})"

# ── Connection ────────────────────────────────────────────────────────────────
def describe():
    return f"{MSSQL_SERVER}/{MSSQL_DATABASE} (SQL Server)" if MSSQL else DB_PATH

def get_connection():
    if MSSQL:
        return pyodbc.connect(f"DRIVER={{{MSSQL_DRIVER}}};SERVER={MSSQL_SERVER};DATABASE={MSSQL_DATABASE};"
                              "Trusted_Connection=yes;TrustServerCertificate=yes;MultiSubnetFailover=yes;"
                              "APP=HornetsDashboard", timeout=30)
    return sqlite3.connect(DB_PATH, timeout=30)

_LIMIT = re.compile(r"\s+LIMIT\s+(\?|\d+)\s*$", re.I)

def _sql(sql):
    """SQLite dialect -> T-SQL: a trailing LIMIT becomes OFFSET/FETCH when the query is ordered, else TOP."""
    if not MSSQL:
        return sql
    m = _LIMIT.search(sql)
    if m:
        sql = sql[:m.start()]
        if re.search(r"\bORDER\s+BY\b", sql, re.I):
            sql += f" OFFSET 0 ROWS FETCH NEXT {m.group(1)} ROWS ONLY"
        else:
            sql = re.sub(r"^\s*SELECT\s+", f"SELECT TOP ({m.group(1)}) ", sql, count=1, flags=re.I)
    return sql

def _insert_sql(table, cols, verb="INSERT OR REPLACE"):
    """INSERT OR REPLACE (upsert on the table's key) or INSERT OR IGNORE (keep an existing row), in either dialect."""
    if not MSSQL:
        return f"{verb} INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
    key = KEYS[table]
    sql = (f"MERGE dbo.{table} WITH (HOLDLOCK) AS t USING (VALUES ({', '.join('?' * len(cols))})) "
           f"AS s ({', '.join(cols)}) ON {' AND '.join(f't.{k} = s.{k}' for k in key)} ")
    updates = [c for c in cols if c not in key]
    if verb == "INSERT OR REPLACE" and updates:
        sql += f"WHEN MATCHED THEN UPDATE SET {', '.join(f'{c} = s.{c}' for c in updates)} "
    return sql + f"WHEN NOT MATCHED THEN INSERT ({', '.join(cols)}) VALUES ({', '.join('s.' + c for c in cols)});"

# ── SQLite migrations (databases created by older builds) ─────────────────────
def _columns(cur, table):
    return {r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()}

def _add_column(cur, table, col, decl):
    if col not in _columns(cur, table):
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        logger.info(f"Migration: added {table}.{col}")

def _migrate_sqlite(cur):
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

    # Prediction track record: results are filled in after each game; 'source' is live or backtest.
    for col, decl in (("actual_pts", "INTEGER"), ("actual_opp_pts", "INTEGER"),
                      ("result", "TEXT"), ("correct", "INTEGER"), ("source", "TEXT")):
        _add_column(cur, "game_predictions", col, decl)
    cur.execute("UPDATE game_predictions SET source='live' WHERE source IS NULL")

def init_db():
    conn = get_connection()
    cur = conn.cursor()
    for table, cols, key, has_id in TABLES:
        cur.execute(_create_sql(table, cols, key, has_id))
    if not MSSQL:
        _migrate_sqlite(cur)
    for name, table, cols in INDEXES:
        cur.execute(_index_sql(name, table, cols))

    # Seed the 2025-26 roster (never overwrites anything already stored).
    now = datetime.utcnow().isoformat()
    cur.executemany(_insert_sql("roster", ROSTER_COLS, "INSERT OR IGNORE"),
                    [("2025-26", pid, name, num, pos, now) for pid, name, num, pos in LEGACY_ROSTER_2025_26])

    conn.commit()
    conn.close()
    logger.info(f"Database ready at {describe()}")

# ── Generic helpers ───────────────────────────────────────────────────────────
def query(sql, params=()):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(_sql(sql), params)
    names = [d[0] for d in cur.description]
    rows = [dict(zip(names, r)) for r in cur.fetchall()]
    conn.close()
    return rows

def execute(sql, params=()):
    conn = get_connection()
    conn.cursor().execute(_sql(sql), params)
    conn.commit()
    conn.close()

def execute_many(sql, rows):
    if not rows:
        return
    conn = get_connection()
    conn.cursor().executemany(_sql(sql), rows)
    conn.commit()
    conn.close()

# ── Writes ────────────────────────────────────────────────────────────────────
PLAYER_COLS = ["player_id", "player_name", "game_id", "game_date", "matchup", "wl", "pts", "reb", "ast", "min",
               "fgm", "fga", "fg_pct", "fg3m", "fg3a", "fg3_pct", "ftm", "fta", "ft_pct", "stl", "blk", "tov",
               "plus_minus", "fetched_at", "season", "game_date_iso"]

TEAM_GAME_COLS = ["game_id", "game_date", "matchup", "wl", "pts", "opp_pts", "fg_pct", "fg3_pct", "ft_pct",
                  "reb", "ast", "stl", "blk", "tov", "fetched_at", "season", "game_date_iso"]

SCHEDULE_COLS = ["game_id", "game_date", "home_team", "away_team", "home_score", "away_score",
                 "status", "arena", "fetched_at", "game_time", "season"]

GAME_PRED_COLS = ["season", "game_id", "game_date", "game_time", "opponent", "home", "arena", "pred_pts",
                  "pred_opp_pts", "win_prob", "winner", "model_version", "created_at",
                  "actual_pts", "actual_opp_pts", "result", "correct", "source"]

ROSTER_COLS = ["season", "player_id", "player_name", "number", "position", "updated_at"]

def upsert_player_stats(rows: list, season: str):
    execute_many(_insert_sql("player_stats", PLAYER_COLS), [tuple(r) + (season, iso_date(r[3])) for r in rows])

def upsert_team_games(rows: list, season: str):
    execute_many(_insert_sql("team_games", TEAM_GAME_COLS), [tuple(r) + (season, iso_date(r[1])) for r in rows])

def save_predictions(rows: list, season: str):
    """rows: (player_id, player_name, game_date, opponent, pts, reb, ast, confidence, model_version, created_at).
    Replaces ALL of a player's predictions for the season so stale forecasts never linger."""
    conn = get_connection()
    cur = conn.cursor()
    for pid in {r[0] for r in rows}:
        cur.execute("DELETE FROM predictions WHERE player_id=? AND season=?", (pid, season))
    if rows:
        cur.executemany("""
            INSERT INTO predictions
            (player_id, player_name, game_date, opponent, pred_pts, pred_reb, pred_ast,
             confidence, model_version, created_at, season)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, [tuple(r) + (season,) for r in rows])
    conn.commit()
    conn.close()

def clear_predictions(season: str):
    execute("DELETE FROM predictions WHERE season=?", (season,))

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
    execute_many(_insert_sql("schedule", SCHEDULE_COLS), [tuple(r) + (season,) for r in rows])

def save_league_games(season: str, rows: list):
    """rows: (game_id, team_abbr, game_date_iso, pts, opp_pts, home)."""
    execute_many(_insert_sql("league_games", ["season", "game_id", "team_abbr", "game_date_iso", "pts", "opp_pts",
                                              "home"]), [(season,) + tuple(r) for r in rows])

def save_game_predictions(season: str, rows: list):
    """Stores predictions for UPCOMING games. Rows for games that have started (or finished) are kept
    untouched, so every prediction is locked in before tip-off and can be graded afterwards.
    rows: (game_id, game_date, game_time, opponent, home, arena, pred_pts, pred_opp_pts, win_prob,
           winner, model_version, created_at)."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM game_predictions WHERE season=? AND result IS NULL AND game_id IN "
                "(SELECT game_id FROM schedule WHERE season=? AND status='Scheduled')", (season, season))
    if rows:
        cur.executemany(_insert_sql("game_predictions", GAME_PRED_COLS[:13] + ["source"]),
                        [(season,) + tuple(r) + ("live",) for r in rows])
    conn.commit()
    conn.close()

def save_prior_games(player_id: int, season: str, rows: list):
    """rows: (game_id, game_date_iso, matchup, pts, reb, ast). Marks the player as checked even if empty."""
    conn = get_connection()
    cur = conn.cursor()
    if rows:
        cur.executemany(_insert_sql("prior_games", ["player_id", "season", "game_id", "game_date_iso", "matchup",
                                                    "pts", "reb", "ast"]),
                        [(player_id, season) + tuple(r) for r in rows])
    cur.execute(_insert_sql("prior_checked", ["player_id", "season", "checked_at"]),
                (player_id, season, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

def save_roster(season: str, rows: list):
    """rows: (player_id, player_name, number, position). Replaces that season's roster."""
    conn = get_connection()
    cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    cur.execute("DELETE FROM roster WHERE season=?", (season,))
    if rows:
        cur.executemany(_insert_sql("roster", ROSTER_COLS), [(season,) + tuple(r) + (now,) for r in rows])
    conn.commit()
    conn.close()
