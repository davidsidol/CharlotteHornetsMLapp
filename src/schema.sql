CREATE TABLE IF NOT EXISTS player_stats (id INTEGER PRIMARY KEY AUTOINCREMENT, player_id INTEGER, player_name TEXT, game_id TEXT, game_date TEXT, matchup TEXT, wl TEXT,
        pts INTEGER, reb INTEGER, ast INTEGER, min TEXT, fgm INTEGER, fga INTEGER, fg_pct REAL,
        fg3m INTEGER, fg3a INTEGER, fg3_pct REAL, ftm INTEGER, fta INTEGER, ft_pct REAL,
        stl INTEGER, blk INTEGER, tov INTEGER, plus_minus INTEGER, fetched_at TEXT,
        season TEXT, game_date_iso TEXT, UNIQUE(player_id, game_id));
CREATE TABLE IF NOT EXISTS team_games (id INTEGER PRIMARY KEY AUTOINCREMENT, game_id TEXT, game_date TEXT, matchup TEXT, wl TEXT, pts INTEGER, opp_pts INTEGER,
        fg_pct REAL, fg3_pct REAL, ft_pct REAL, reb INTEGER, ast INTEGER, stl INTEGER, blk INTEGER, tov INTEGER,
        fetched_at TEXT, season TEXT, game_date_iso TEXT, UNIQUE(game_id));
CREATE TABLE IF NOT EXISTS predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, player_id INTEGER, player_name TEXT, game_date TEXT, opponent TEXT,
        pred_pts REAL, pred_reb REAL, pred_ast REAL, confidence REAL, model_version TEXT, created_at TEXT,
        season TEXT);
CREATE TABLE IF NOT EXISTS team_info (id INTEGER PRIMARY KEY AUTOINCREMENT, season TEXT, wins INTEGER, losses INTEGER, win_pct REAL, conf_rank INTEGER, div_rank INTEGER,
        updated_at TEXT);
CREATE TABLE IF NOT EXISTS schedule (id INTEGER PRIMARY KEY AUTOINCREMENT, game_id TEXT, game_date TEXT, home_team TEXT, away_team TEXT, home_score INTEGER,
        away_score INTEGER, status TEXT, arena TEXT, fetched_at TEXT, season TEXT, game_time TEXT, UNIQUE(game_id));
CREATE TABLE IF NOT EXISTS league_games (season TEXT, game_id TEXT, team_abbr TEXT, game_date_iso TEXT, pts INTEGER,
        opp_pts INTEGER, home INTEGER, PRIMARY KEY (season, game_id, team_abbr));
CREATE TABLE IF NOT EXISTS game_predictions (season TEXT, game_id TEXT, game_date TEXT, game_time TEXT, opponent TEXT, home INTEGER,
        arena TEXT, pred_pts INTEGER, pred_opp_pts INTEGER, win_prob REAL, winner TEXT, model_version TEXT,
        created_at TEXT, actual_pts INTEGER, actual_opp_pts INTEGER, result TEXT, correct INTEGER,
        source TEXT, PRIMARY KEY (season, game_id));
CREATE TABLE IF NOT EXISTS prior_games (player_id INTEGER, season TEXT, game_id TEXT, game_date_iso TEXT, matchup TEXT,
        pts INTEGER, reb INTEGER, ast INTEGER, PRIMARY KEY (player_id, game_id));
CREATE TABLE IF NOT EXISTS prior_checked (player_id INTEGER, season TEXT, checked_at TEXT, PRIMARY KEY (player_id, season));
CREATE TABLE IF NOT EXISTS roster (season TEXT, player_id INTEGER, player_name TEXT, number TEXT, position TEXT,
        updated_at TEXT, PRIMARY KEY (season, player_id));
CREATE TABLE IF NOT EXISTS league_player_games (season TEXT, game_id TEXT, player_id INTEGER, player_name TEXT, team_abbr TEXT,
        opp_abbr TEXT, game_date_iso TEXT, home INTEGER, min REAL, pts INTEGER, reb INTEGER, ast INTEGER, PRIMARY KEY (season, game_id, player_id));
CREATE TABLE IF NOT EXISTS model_params (name TEXT, value TEXT, updated_at TEXT, PRIMARY KEY (name));
