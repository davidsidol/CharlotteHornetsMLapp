"""
Data fetcher: pulls Hornets + player data from NBA API.
No pandas dependency — uses raw JSON from nba_api.
"""
import logging
import time
from datetime import datetime
import requests
from bs4 import BeautifulSoup

from database import (
    upsert_player_stats, upsert_team_games, save_team_info,
    upsert_schedule, query
)

logger = logging.getLogger(__name__)

HORNETS_ID = 1610612766
SEASON = "2025-26"

PLAYERS = {
    1630163: "LaMelo Ball",
    1641706: "Brandon Miller",
    1642851: "Kon Knueppel",
    1628970: "Miles Bridges",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
}

def _safe_int(v):
    try: return int(v) if v == v and v is not None else 0
    except: return 0

def _safe_float(v):
    try: return float(v) if v == v and v is not None else 0.0
    except: return 0.0

def fetch_player_stats():
    from nba_api.stats.endpoints import playergamelog
    fetched_at = datetime.utcnow().isoformat()
    for pid, pname in PLAYERS.items():
        try:
            gl = playergamelog.PlayerGameLog(player_id=pid, season=SEASON)
            data = gl.get_normalized_dict()
            games = data.get("PlayerGameLog", [])
            rows = []
            for g in games:
                rows.append((
                    pid, pname,
                    str(g["Game_ID"]), str(g["GAME_DATE"]), str(g["MATCHUP"]), str(g["WL"]),
                    _safe_int(g["PTS"]), _safe_int(g["REB"]), _safe_int(g["AST"]),
                    str(g.get("MIN", "0")),
                    _safe_int(g["FGM"]), _safe_int(g["FGA"]), _safe_float(g["FG_PCT"]),
                    _safe_int(g["FG3M"]), _safe_int(g["FG3A"]), _safe_float(g["FG3_PCT"]),
                    _safe_int(g["FTM"]), _safe_int(g["FTA"]), _safe_float(g["FT_PCT"]),
                    _safe_int(g["STL"]), _safe_int(g["BLK"]), _safe_int(g["TOV"]),
                    _safe_int(g["PLUS_MINUS"]), fetched_at
                ))
            upsert_player_stats(rows)
            logger.info(f"Saved {len(rows)} games for {pname}")
            time.sleep(0.6)
        except Exception as e:
            logger.error(f"Error fetching stats for {pname}: {e}")

def fetch_team_games():
    from nba_api.stats.endpoints import teamgamelog
    fetched_at = datetime.utcnow().isoformat()
    try:
        tgl = teamgamelog.TeamGameLog(team_id=HORNETS_ID, season=SEASON)
        data = tgl.get_normalized_dict()
        games = data.get("TeamGameLog", [])
        rows = []
        for g in games:
            rows.append((
                str(g["Game_ID"]), str(g["GAME_DATE"]), str(g["MATCHUP"]), str(g["WL"]),
                _safe_int(g["PTS"]), 0,
                _safe_float(g["FG_PCT"]), _safe_float(g["FG3_PCT"]), _safe_float(g["FT_PCT"]),
                _safe_int(g["REB"]), _safe_int(g["AST"]), _safe_int(g["STL"]),
                _safe_int(g["BLK"]), _safe_int(g["TOV"]), fetched_at
            ))
        upsert_team_games(rows)
        logger.info(f"Saved {len(rows)} team games")
    except Exception as e:
        logger.error(f"Error fetching team games: {e}")

def fetch_team_info():
    from nba_api.stats.endpoints import teaminfocommon
    try:
        ti = teaminfocommon.TeamInfoCommon(team_id=HORNETS_ID)
        data = ti.get_normalized_dict()
        info = data.get("TeamInfoCommon", [{}])[0]
        save_team_info((
            str(info.get("SEASON_YEAR", SEASON)),
            _safe_int(info.get("W", 0)), _safe_int(info.get("L", 0)),
            _safe_float(info.get("PCT", 0.0)),
            _safe_int(info.get("CONF_RANK", 0)), _safe_int(info.get("DIV_RANK", 0)),
            datetime.utcnow().isoformat()
        ))
        logger.info("Team info saved")
    except Exception as e:
        logger.error(f"Error fetching team info: {e}")

def fetch_schedule_from_nba():
    from nba_api.stats.endpoints import leaguegamefinder
    fetched_at = datetime.utcnow().isoformat()
    try:
        lgf = leaguegamefinder.LeagueGameFinder(
            team_id_nullable=HORNETS_ID, season_nullable=SEASON,
            season_type_nullable="Regular Season"
        )
        data = lgf.get_normalized_dict()
        games = data.get("LeagueGameFinderResults", [])
        rows = []
        for g in games:
            matchup = str(g.get("MATCHUP", ""))
            is_home = "vs." in matchup
            opp = matchup.split(" ")[-1]
            pts = _safe_int(g.get("PTS", 0)) or None
            rows.append((
                str(g["GAME_ID"]), str(g["GAME_DATE"]),
                "Charlotte Hornets" if is_home else opp,
                opp if is_home else "Charlotte Hornets",
                pts if is_home else None, pts if not is_home else None,
                "Final" if g.get("WL") else "Scheduled",
                "Spectrum Center" if is_home else "", fetched_at
            ))
        upsert_schedule(rows)
        logger.info(f"Saved {len(rows)} schedule entries")
    except Exception as e:
        logger.error(f"Error fetching schedule: {e}")

def refresh_all():
    logger.info("Starting full data refresh...")
    fetch_team_info()
    fetch_team_games()
    fetch_player_stats()
    fetch_schedule_from_nba()
    logger.info("Data refresh complete.")
