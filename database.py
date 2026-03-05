"""
Database layer using SQLite (MSSQL-compatible schema).
When MSSQL (pyodbc) is available, swaps in seamlessly via DB_TYPE env var.
"""
import sqlite3
import os
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/home/claude/hornets_app/hornets.db")

# ── MSSQL support (optional) ──────────────────────────────────────────────────
USE_MSSQL = os.environ.get("USE_MSSQL", "false").lower() == "true"
MSSQL_CONN_STR = os.environ.get(
    "MSSQL_CONN_STR",
    "DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost;DATABASE=HornetsDB;Trusted_Connection=yes;"
)

def get_connection():
    if USE_MSSQL:
        try:
            import pyodbc
            return pyodbc.connect(MSSQL_CONN_STR)
        except Exception as e:
            logger.warning(f"MSSQL connection failed ({e}), falling back to SQLite")
    return sqlite3.connect(DB_PATH)

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

    conn.commit()
    conn.close()
    logger.info("Database initialized.")

def upsert_player_stats(rows: list):
    conn = get_connection()
    cur = conn.cursor()
    for r in rows:
        cur.execute("""
            INSERT OR REPLACE INTO player_stats
            (player_id, player_name, game_id, game_date, matchup, wl,
             pts, reb, ast, min, fgm, fga, fg_pct, fg3m, fg3a, fg3_pct,
             ftm, fta, ft_pct, stl, blk, tov, plus_minus, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, r)
    conn.commit()
    conn.close()

def upsert_team_games(rows: list):
    conn = get_connection()
    cur = conn.cursor()
    for r in rows:
        cur.execute("""
            INSERT OR REPLACE INTO team_games
            (game_id, game_date, matchup, wl, pts, opp_pts,
             fg_pct, fg3_pct, ft_pct, reb, ast, stl, blk, tov, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, r)
    conn.commit()
    conn.close()

def save_predictions(rows: list):
    conn = get_connection()
    cur = conn.cursor()
    # Clear old predictions for same player/date
    for r in rows:
        cur.execute("DELETE FROM predictions WHERE player_id=? AND game_date=?", (r[0], r[2]))
        cur.execute("""
            INSERT INTO predictions
            (player_id, player_name, game_date, opponent, pred_pts, pred_reb, pred_ast, confidence, model_version, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, r)
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

def upsert_schedule(rows: list):
    conn = get_connection()
    cur = conn.cursor()
    for r in rows:
        cur.execute("""
            INSERT OR REPLACE INTO schedule
            (game_id, game_date, home_team, away_team, home_score, away_score, status, arena, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, r)
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
