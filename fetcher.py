"""
Data fetcher: pulls Hornets + player data from the NBA API for a given season.
No pandas dependency — uses raw JSON from nba_api.

refresh_all(season) refreshes one season (defaults to the current one). Past seasons
are never touched by the scheduled refresh, so their stored stats stay frozen.
"""
import logging
import time
from datetime import datetime

from database import (
    upsert_player_stats, upsert_team_games, save_team_info,
    upsert_schedule, save_roster, save_prior_games, save_league_games, current_season, iso_date, query
)

logger = logging.getLogger(__name__)

HORNETS_ID = 1610612766
HORNETS_ABBR = "CHA"

# Featured players the dashboard always keeps on the Overview while they are on the roster.
# (The other featured slots are filled by minutes played, since rosters change every year.)
PINNED_PLAYERS = {
    1641706: "Brandon Miller",
    1642851: "Kon Knueppel",
    1629632: "Coby White",
    1629675: "Naz Reid",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
}
TIMEOUT = 45

POSITION_NAMES = {
    "G": "Guard", "F": "Forward", "C": "Center",
    "G-F": "Guard-Forward", "F-G": "Forward-Guard",
    "F-C": "Forward-Center", "C-F": "Center-Forward",
}

def _safe_int(v):
    try: return int(v) if v == v and v is not None else 0
    except: return 0

def _safe_float(v):
    try: return float(v) if v == v and v is not None else 0.0
    except: return 0.0

def _fmt_log_date(iso):
    """'2026-10-21T00:00:00' -> 'Oct 21, 2026' (same style as the stored 2025-26 rows)."""
    try:
        return datetime.strptime(str(iso)[:10], "%Y-%m-%d").strftime("%b %d, %Y")
    except Exception:
        return str(iso)

def fetch_roster(season):
    from nba_api.stats.endpoints import commonteamroster
    try:
        data = commonteamroster.CommonTeamRoster(
            team_id=HORNETS_ID, season=season, headers=HEADERS, timeout=TIMEOUT
        ).get_normalized_dict()
        rows = []
        for p in data.get("CommonTeamRoster", []):
            pos = p.get("POSITION") or ""
            num = p.get("NUM")
            rows.append((int(p["PLAYER_ID"]), str(p["PLAYER"]),
                         "" if num in (None, "") else str(num),
                         POSITION_NAMES.get(pos, pos)))
        if rows:  # never wipe the stored roster on an empty/failed response
            save_roster(season, rows)
        logger.info(f"Saved {len(rows)} roster entries for {season}")
    except Exception as e:
        logger.error(f"Error fetching roster for {season}: {e}")

def fetch_player_stats(season):
    """One call returns every player's Hornets game logs for the season."""
    from nba_api.stats.endpoints import playergamelogs
    fetched_at = datetime.utcnow().isoformat()
    try:
        data = playergamelogs.PlayerGameLogs(
            team_id_nullable=HORNETS_ID, season_nullable=season,
            season_type_nullable="Regular Season", headers=HEADERS, timeout=TIMEOUT
        ).get_normalized_dict()
        logs = data.get("PlayerGameLogs", [])
        rows = []
        for g in logs:
            rows.append((
                int(g["PLAYER_ID"]), str(g["PLAYER_NAME"]),
                str(g["GAME_ID"]), _fmt_log_date(g["GAME_DATE"]), str(g["MATCHUP"]), str(g.get("WL") or ""),
                _safe_int(g["PTS"]), _safe_int(g["REB"]), _safe_int(g["AST"]),
                str(int(round(_safe_float(g.get("MIN"))))),
                _safe_int(g["FGM"]), _safe_int(g["FGA"]), _safe_float(g["FG_PCT"]),
                _safe_int(g["FG3M"]), _safe_int(g["FG3A"]), _safe_float(g["FG3_PCT"]),
                _safe_int(g["FTM"]), _safe_int(g["FTA"]), _safe_float(g["FT_PCT"]),
                _safe_int(g["STL"]), _safe_int(g["BLK"]), _safe_int(g["TOV"]),
                _safe_int(g["PLUS_MINUS"]), fetched_at
            ))
        if rows:
            upsert_player_stats(rows, season)
        logger.info(f"Saved {len(rows)} player-game rows for {season}")
    except Exception as e:
        logger.error(f"Error fetching player stats for {season}: {e}")

def fetch_team_games(season):
    from nba_api.stats.endpoints import teamgamelog
    fetched_at = datetime.utcnow().isoformat()
    try:
        data = teamgamelog.TeamGameLog(
            team_id=HORNETS_ID, season=season, headers=HEADERS, timeout=TIMEOUT
        ).get_normalized_dict()
        rows = []
        for g in data.get("TeamGameLog", []):
            rows.append((
                str(g["Game_ID"]), str(g["GAME_DATE"]), str(g["MATCHUP"]), str(g["WL"]),
                _safe_int(g["PTS"]), 0,
                _safe_float(g["FG_PCT"]), _safe_float(g["FG3_PCT"]), _safe_float(g["FT_PCT"]),
                _safe_int(g["REB"]), _safe_int(g["AST"]), _safe_int(g["STL"]),
                _safe_int(g["BLK"]), _safe_int(g["TOV"]), fetched_at
            ))
        if rows:
            upsert_team_games(rows, season)
        logger.info(f"Saved {len(rows)} team games for {season}")
    except Exception as e:
        logger.error(f"Error fetching team games for {season}: {e}")

def fetch_team_info():
    """TeamInfoCommon ignores its season argument and always reports the CURRENT season,
    so the row is stored under the SEASON_YEAR the API returns (older seasons stay as saved)."""
    from nba_api.stats.endpoints import teaminfocommon
    try:
        data = teaminfocommon.TeamInfoCommon(
            team_id=HORNETS_ID, headers=HEADERS, timeout=TIMEOUT
        ).get_normalized_dict()
        info = (data.get("TeamInfoCommon") or [{}])[0]
        season = str(info.get("SEASON_YEAR") or current_season())
        save_team_info((
            season,
            _safe_int(info.get("W", 0)), _safe_int(info.get("L", 0)),
            _safe_float(info.get("PCT", 0.0)),
            _safe_int(info.get("CONF_RANK", 0)), _safe_int(info.get("DIV_RANK", 0)),
            datetime.utcnow().isoformat()
        ))
        logger.info(f"Team info saved for {season}")
    except Exception as e:
        logger.error(f"Error fetching team info: {e}")

def fetch_schedule(season):
    """Full regular-season schedule, played AND upcoming, from the NBA schedule feed."""
    from nba_api.stats.endpoints import scheduleleaguev2
    fetched_at = datetime.utcnow().isoformat()
    status_names = {1: "Scheduled", 2: "Live", 3: "Final"}
    try:
        data = scheduleleaguev2.ScheduleLeagueV2(
            season=season, headers=HEADERS, timeout=60
        ).get_dict()
        rows = []
        for day in data["leagueSchedule"]["gameDates"]:
            for g in day.get("games", []):
                gid = str(g.get("gameId", ""))
                if not gid.startswith("002"):          # regular season only
                    continue
                home, away = g["homeTeam"], g["awayTeam"]
                if HORNETS_ABBR not in (home.get("teamTricode"), away.get("teamTricode")):
                    continue
                status = status_names.get(g.get("gameStatus"), "Scheduled")
                tip = str(g.get("gameDateTimeEst") or "")      # e.g. 2026-10-23T19:00:00Z (Eastern time)
                played = status != "Scheduled"
                rows.append((
                    gid, tip[:10], home.get("teamTricode"), away.get("teamTricode"),
                    _safe_int(home.get("score")) if played else None,
                    _safe_int(away.get("score")) if played else None,
                    status, g.get("arenaName") or "", fetched_at, tip[11:16]
                ))
        if rows:
            upsert_schedule(rows, season)
        logger.info(f"Saved {len(rows)} schedule entries for {season}")
    except Exception as e:
        logger.error(f"Error fetching schedule for {season}: {e}")

def prev_season(season):
    y = int(str(season)[:4]) - 1
    return f"{y}-{str(y + 1)[-2:]}"

def fetch_prior_games(season, window=8):
    """For roster players with fewer than `window` games this season, pull their previous-season
    game log (with ANY team) once, so ML forecasts can start before they have played much."""
    from nba_api.stats.endpoints import playergamelog
    prior = prev_season(season)
    roster = query("SELECT player_id, player_name FROM roster WHERE season=?", (season,))
    done = 0
    for p in roster:
        pid = p["player_id"]
        cur = query("SELECT COUNT(*) AS c FROM player_stats WHERE player_id=? AND season=?", (pid, season))[0]["c"]
        if cur >= window:
            continue
        if query("SELECT 1 AS x FROM prior_checked WHERE player_id=? AND season=?", (pid, prior)):
            continue
        try:
            logs = playergamelog.PlayerGameLog(
                player_id=pid, season=prior, headers=HEADERS, timeout=TIMEOUT
            ).get_normalized_dict().get("PlayerGameLog", [])
            rows = [(str(g["Game_ID"]), iso_date(g["GAME_DATE"]), str(g.get("MATCHUP", "")),
                     _safe_int(g["PTS"]), _safe_int(g["REB"]), _safe_int(g["AST"])) for g in logs]
            save_prior_games(pid, prior, rows)
            done += 1
            logger.info(f"Prior-season ({prior}) games for {p['player_name']}: {len(rows)}")
        except Exception as e:
            logger.error(f"Error fetching prior-season games for {p['player_name']}: {e}")
        time.sleep(0.7)
    logger.info(f"Prior-season check complete ({done} players fetched).")

def fetch_league_games(season, refresh=True):
    """Every team's game results for the season (points for and against) - one call.
    refresh=False skips the call when the season is already stored (used for last season)."""
    from nba_api.stats.endpoints import leaguegamelog
    if not refresh and query("SELECT 1 AS x FROM league_games WHERE season=? LIMIT 1", (season,)):
        return
    try:
        data = leaguegamelog.LeagueGameLog(
            season=season, season_type_all_star="Regular Season",
            player_or_team_abbreviation="T", headers=HEADERS, timeout=60
        ).get_normalized_dict()
        rows = []
        for g in data.get("LeagueGameLog", []):
            if not g.get("WL"):                      # skip games not finished yet
                continue
            pts = _safe_int(g["PTS"])
            opp = pts - int(round(_safe_float(g["PLUS_MINUS"])))
            rows.append((str(g["GAME_ID"]), str(g["TEAM_ABBREVIATION"]), iso_date(g["GAME_DATE"]),
                         pts, opp, 0 if "@" in str(g.get("MATCHUP", "")) else 1))
        if rows:
            save_league_games(season, rows)
        logger.info(f"Saved {len(rows)} league team-game rows for {season}")
    except Exception as e:
        logger.error(f"Error fetching league games for {season}: {e}")

def refresh_all(season=None):
    season = season or current_season()
    logger.info(f"Starting full data refresh for {season}...")
    if season == current_season():
        fetch_team_info()
        time.sleep(0.6)
    fetch_roster(season)
    time.sleep(0.6)
    fetch_team_games(season)
    time.sleep(0.6)
    fetch_player_stats(season)
    time.sleep(0.6)
    fetch_schedule(season)
    time.sleep(0.6)
    fetch_prior_games(season)
    time.sleep(0.6)
    fetch_league_games(season)
    fetch_league_games(prev_season(season), refresh=False)
    logger.info(f"Data refresh complete for {season}.")
