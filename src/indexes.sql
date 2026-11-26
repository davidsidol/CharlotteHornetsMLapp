CREATE INDEX IF NOT EXISTS idx_ps_season_player ON player_stats(season, player_id, game_date_iso);
CREATE INDEX IF NOT EXISTS idx_tg_season ON team_games(season, game_date_iso);
CREATE INDEX IF NOT EXISTS idx_sched_season ON schedule(season, game_date);
CREATE INDEX IF NOT EXISTS idx_pred_season ON predictions(season, player_id);
CREATE INDEX IF NOT EXISTS idx_lpg_player ON league_player_games(player_id, game_date_iso);
