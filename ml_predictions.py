"""
ML prediction engine — no scikit-learn required (pure Python, 3.14+).

Team model ("team_v2")
  Every team gets an offensive rating (points scored) and a defensive rating (points allowed), built game by game
  with exponential recency decay and a discounted carry-over from last season. A matchup's predicted score =
  league average + shrink * (offense edge + opponent defense edge) +/- home edge +/- a rest adjustment (days since
  each team's last game, and a back-to-back penalty); the win probability comes from a normal distribution of the
  margin. The parameters are LEARNED: a grid search walks every league game since the second stored season in date
  order (each game predicted only from games before it) and keeps the setting with the lowest log-loss. The
  old fixed setting is inside the grid, so the new one is only used to the extent it predicts past games better.
  A season is always predicted with parameters learned on earlier seasons only.

Player model ("player_v2")
  Points / rebounds / assists = forecast minutes x per-minute rate x opponent adjustment.
  Minutes and each stat's per-minute rate are recency-weighted over the player's last games (any team, any season,
  from the league-wide game logs). The adjustment scales for how many points the opponent allows and how fast the
  game is expected to be (opponent pace). The recency decays and the adjustment strengths are learned from past
  games and scored by mean absolute error per stat against the old model (plain 8-game weighted average).
  Players with no league-wide history yet fall back to that old average ("rolling_v2"), without random noise.
"""
import json
import logging
import math
from collections import defaultdict
from datetime import datetime, date
from itertools import product
from statistics import NormalDist

from database import (query, save_predictions, clear_predictions, save_game_predictions,
                      execute, execute_many, current_season, set_param, get_param, _insert_sql, GAME_PRED_COLS)

logger = logging.getLogger(__name__)

HORNETS_ABBR = "CHA"
TEAM_MODEL_VERSION = "team_v2"
PLAYER_MODEL_VERSION = "player_v2"
LEGACY_PLAYER_VERSION = "rolling_v2"
N_FORECAST = 5          # upcoming games to forecast per player
N_TEAM_FORECAST = 5
WINDOW = 10             # player games in the rolling window
LEGACY_WINDOW = 8
MIN_GAMES = 3           # no forecast for players with fewer games than this on record
MIN_MPG = 12            # tune the player model on players averaging at least this many minutes
STATS = ("pts", "reb", "ast")

# The fixed settings used before the model learned its own (team_v1): the baseline every tuned model must beat.
DEFAULT_TEAM_PARAMS = {"w_prior": 0.4, "decay": 1.0, "shrink": 0.75, "hfa": 1.4,
                       "k_rest": 0.0, "k_b2b": 0.0, "sd": 12.0}
MARGIN_SD = DEFAULT_TEAM_PARAMS["sd"]        # fallback when a season has no learned parameters yet
DEFAULT_PLAYER_PARAMS = {"decay_min": 0.85, "decay": {s: 0.85 for s in STATS}, "w_def": 0.5,
                         "w_pace": {s: 0.5 for s in STATS}}
_ND = NormalDist()
_ORD = {}

def _ord(day):
    if day not in _ORD:
        _ORD[day] = date.fromisoformat(str(day)[:10]).toordinal()
    return _ORD[day]

def _season_start(season):
    return int(str(season)[:4])

def _prev_season(season):
    y = _season_start(season) - 1
    return f"{y}-{str(y + 1)[-2:]}"

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
            games.append({"date": r["game_date"], "opponent": f"vs {r['away_team']}", "opp": r["away_team"]})
        else:
            games.append({"date": r["game_date"], "opponent": f"@ {r['home_team']}", "opp": r["home_team"]})
    return games

# ══════════════════════════════════════════════════════════════════════════════
#  TEAM MODEL
# ══════════════════════════════════════════════════════════════════════════════
def _games():
    """Every finished league game, oldest first: {season, game_id, date, home_team, away_team, hs, as_}."""
    rows = query("SELECT season, game_id, team_abbr, game_date_iso, pts, opp_pts, home FROM league_games "
                 "WHERE game_date_iso IS NOT NULL ORDER BY game_date_iso, game_id")
    by = {}
    for r in rows:
        g = by.setdefault((r["season"], r["game_id"]),
                          {"season": r["season"], "game_id": r["game_id"], "date": r["game_date_iso"]})
        if r["home"]:
            g["home_team"], g["hs"] = r["team_abbr"], r["pts"]
        else:
            g["away_team"], g["as_"] = r["team_abbr"], r["pts"]
    games = [g for g in by.values() if "home_team" in g and "away_team" in g]
    games.sort(key=lambda g: (g["date"], g["game_id"]))
    return games

class _Ratings:
    def __init__(self, p):
        self.p = p
        self.off, self.dfn, self.w = defaultdict(float), defaultdict(float), defaultdict(float)
        self.lg_s = self.lg_w = 0.0
        self.season = None
        self.last = {}                    # team -> ordinal day of its last game

    def new_season(self, season):
        if self.season is not None:
            k = self.p["w_prior"]
            for t in list(self.w):
                self.off[t] *= k; self.dfn[t] *= k; self.w[t] *= k
            self.lg_s *= k; self.lg_w *= k
        self.season = season

    def rest(self, team, day):
        """Days since the team's last game, 1 = back-to-back, capped at 4 (also after the off-season)."""
        last = self.last.get(team)
        return 4 if last is None else min(4, max(1, day - last))

    def predict(self, home, away, day):
        """(home_pts, away_pts, home margin) or None while a team has no games on record."""
        if not self.w[home] or not self.w[away]:
            return None
        p = self.p
        lg = self.lg_s / self.lg_w
        oh, dh = self.off[home] / self.w[home], self.dfn[home] / self.w[home]
        oa, da = self.off[away] / self.w[away], self.dfn[away] / self.w[away]
        rh, ra = self.rest(home, day), self.rest(away, day)
        adj = p["k_rest"] * (rh - ra) + p["k_b2b"] * ((ra == 1) - (rh == 1))
        hp = lg + p["shrink"] * ((oh - lg) + (da - lg)) + p["hfa"] + adj / 2
        ap = lg + p["shrink"] * ((oa - lg) + (dh - lg)) - p["hfa"] - adj / 2
        return hp, ap, hp - ap

    def update(self, home, away, hs, as_, day):
        d = self.p["decay"]
        for t, pf, pa in ((home, hs, as_), (away, as_, hs)):
            self.off[t] = self.off[t] * d + pf
            self.dfn[t] = self.dfn[t] * d + pa
            self.w[t] = self.w[t] * d + 1.0
            self.last[t] = day
        self.lg_s += hs + as_; self.lg_w += 2.0

    def env(self, team):
        """(defense ratio, pace ratio): points the team allows, and points in its games, vs the league (1.0 = average)."""
        if not self.w[team] or not self.lg_w:
            return 1.0, 1.0
        lg = self.lg_s / self.lg_w
        return (self.dfn[team] / self.w[team]) / lg, ((self.off[team] + self.dfn[team]) / self.w[team]) / (2 * lg)

def walk(params, games, stop_before_season=None, want_env=False):
    """Replay games in date order. Returns ({game_id: (home_pts, away_pts, margin)} for every played game, predicted
    only from games before its date), the ratings after the last game, and (want_env) {date: {team: env}} as it
    stood at the start of each game day."""
    r = _Ratings(params)
    out, env = {}, {}
    i, n = 0, len(games)
    while i < n:
        day = games[i]["date"]
        j = i
        while j < n and games[j]["date"] == day:
            j += 1
        batch = games[i:j]
        if stop_before_season and any(_season_start(g["season"]) >= _season_start(stop_before_season) for g in batch):
            break
        for g in batch:
            if g["season"] != r.season:
                r.new_season(g["season"])
        d = _ord(day)
        if want_env:
            env[day] = {t: r.env(t) for t in list(r.w)}
        for g in batch:
            m = r.predict(g["home_team"], g["away_team"], d)
            if m:
                out[g["game_id"]] = m
        for g in batch:
            r.update(g["home_team"], g["away_team"], g["hs"], g["as_"], d)
        i = j
    return out, r, env

def _logloss(rows, sd):
    ll = 0.0
    for margin, actual in rows:
        p = min(max(_ND.cdf(margin / sd), 1e-4), 1 - 1e-4)
        ll -= math.log(p if actual > 0 else 1 - p)
    return ll / len(rows)

def _accuracy(rows):
    return sum(1 for m, a in rows if (m > 0) == (a > 0)) / len(rows)

def _season_rows(preds, games, season):
    return [(preds[g["game_id"]][2], g["hs"] - g["as_"]) for g in games
            if g["season"] == season and g["game_id"] in preds and g["hs"] != g["as_"]]

def _fit_team(games, tune_seasons, stop_before):
    """Grid-search the team parameters on `tune_seasons`, each game predicted only from games before it and the
    replay stopped before `stop_before`. Returns (params, rows, log-loss, log-loss without rest terms)."""
    eval_games = [g for g in games if g["season"] in tune_seasons and g["hs"] != g["as_"]]
    def score(p):
        preds, _, _ = walk(p, games, stop_before_season=stop_before)
        return [(preds[g["game_id"]][2], g["hs"] - g["as_"]) for g in eval_games if g["game_id"] in preds]
    def best_sd(rows):
        return min((_logloss(rows, sd), sd) for sd in (10.5, 11.25, 12.0, 12.75, 13.5, 15.0, 16.5))
    best = None
    # Stage 1: the rating itself.
    for w_prior, decay, shrink, hfa in product([0.2, 0.4, 0.6], [0.94, 0.96, 0.98, 1.0],
                                               [0.5, 0.65, 0.8, 0.95], [1.0, 1.4, 1.8]):
        p = dict(DEFAULT_TEAM_PARAMS, w_prior=w_prior, decay=decay, shrink=shrink, hfa=hfa)
        rows = score(p)
        ll, sd = best_sd(rows)
        if best is None or ll < best[0]:
            best = (ll, dict(p, sd=sd), rows)
    # Stage 2: rest days and back-to-backs (zero stays possible).
    stage1 = best
    for k_rest, k_b2b in product([0.0, 0.1, 0.2, 0.3], [0.0, 0.75, 1.5, 2.25]):
        p = dict(stage1[1], k_rest=k_rest, k_b2b=k_b2b)
        rows = score(p)
        ll, sd = best_sd(rows)
        if ll < best[0]:
            best = (ll, dict(p, sd=sd), rows)
    return best[1], best[2], best[0], stage1[0]

def _holdout_check(games, tune_seasons):
    """Does the tuning procedure beat the old fixed settings on a season it did not learn from? Fits on all the
    tune seasons but the newest, then compares on the newest. None when there is too little history to check."""
    if len(tune_seasons) < 2:
        return None
    fit_seasons, hold = tune_seasons[:-1], tune_seasons[-1]
    if sum(1 for g in games if g["season"] in fit_seasons) < 300:
        return None
    params, _, _, _ = _fit_team(games, fit_seasons, hold)
    new_preds, _, _ = walk(params, games, stop_before_season=_next_season(hold))
    old_preds, _, _ = walk(DEFAULT_TEAM_PARAMS, games, stop_before_season=_next_season(hold))
    new_rows, old_rows = _season_rows(new_preds, games, hold), _season_rows(old_preds, games, hold)
    if not new_rows:
        return None
    return {"season": hold, "games": len(new_rows),
            "log_loss": round(_logloss(new_rows, params["sd"]), 4), "accuracy": round(100 * _accuracy(new_rows), 1),
            "baseline_log_loss": round(_logloss(old_rows, DEFAULT_TEAM_PARAMS["sd"]), 4),
            "baseline_accuracy": round(100 * _accuracy(old_rows), 1)}

def _next_season(season):
    y = _season_start(season) + 1
    return f"{y}-{str(y + 1)[-2:]}"

def tune_team_model(target_season, games):
    """Learn the team-model parameters for `target_season` from every season before it (the first stored season is
    only warm-up). Returns (params, metrics). The old fixed settings are returned when there is too little history,
    or when the tuning procedure fails to beat them on a held-out season (it must also win on log-loss)."""
    seasons = sorted({g["season"] for g in games})
    tune_seasons = [s for s in seasons[1:] if _season_start(s) < _season_start(target_season)]
    if sum(1 for g in games if g["season"] in tune_seasons) < 300:
        return dict(DEFAULT_TEAM_PARAMS), {}
    base = dict(DEFAULT_TEAM_PARAMS)
    params, rows, ll, no_rest_ll = _fit_team(games, tune_seasons, target_season)
    base_preds, _, _ = walk(base, games, stop_before_season=target_season)
    base_rows = [(base_preds[g["game_id"]][2], g["hs"] - g["as_"]) for g in games
                 if g["season"] in tune_seasons and g["hs"] != g["as_"] and g["game_id"] in base_preds]
    metrics = {"seasons": tune_seasons, "games": len(rows),
               "accuracy": round(100 * _accuracy(rows), 1), "log_loss": round(ll, 4),
               "baseline_accuracy": round(100 * _accuracy(base_rows), 1),
               "baseline_log_loss": round(_logloss(base_rows, base["sd"]), 4),
               "no_rest_log_loss": round(no_rest_ll, 4)}
    hold = _holdout_check(games, tune_seasons)
    if hold:
        metrics["holdout"] = hold
        if hold["log_loss"] >= hold["baseline_log_loss"]:
            metrics["used"] = "old fixed settings (tuning did not beat them on the held-out season)"
            params = base
    # Honest check: both settings on the target season's games played so far (not used for learning).
    new_preds, _, _ = walk(params, games)
    old_preds, _, _ = walk(base, games)
    new_rows, old_rows = _season_rows(new_preds, games, target_season), _season_rows(old_preds, games, target_season)
    if len(new_rows) >= 20:
        metrics["out_of_sample"] = {"season": target_season, "games": len(new_rows),
                                    "accuracy": round(100 * _accuracy(new_rows), 1),
                                    "log_loss": round(_logloss(new_rows, params["sd"]), 4),
                                    "baseline_accuracy": round(100 * _accuracy(old_rows), 1),
                                    "baseline_log_loss": round(_logloss(old_rows, base["sd"]), 4)}
    logger.info(f"Team model for {target_season}: {params} -> {metrics}")
    return params, metrics

def team_params(season):
    raw = get_param(f"team_params_{season}")
    return json.loads(raw) if raw else dict(DEFAULT_TEAM_PARAMS)

def margin_sd(season):
    """Spread of single-game margins the season's model uses to turn a margin into a win probability."""
    try:
        return float(team_params(season).get("sd", MARGIN_SD))
    except Exception:
        return MARGIN_SD

def _pick(hp, ap, margin, sd, cha_home):
    """Hornets-perspective (cha_pts, opp_pts, cha_win_prob) with no predicted ties."""
    cha, opp = (hp, ap) if cha_home else (ap, hp)
    m = margin if cha_home else -margin
    p_win = _ND.cdf(m / sd)
    a, b = int(round(cha)), int(round(opp))
    if a == b:
        if m >= 0: a += 1
        else: b += 1
    return a, b, p_win

def run_team_predictions(season, ratings, params):
    today = datetime.now().strftime("%Y-%m-%d")
    games = query(
        "SELECT game_id, game_date, game_time, home_team, away_team, arena FROM schedule "
        "WHERE season=? AND status='Scheduled' AND game_date >= ? ORDER BY game_date ASC, game_id ASC LIMIT ?",
        (season, today, N_TEAM_FORECAST))
    if ratings.season != season:
        ratings.new_season(season)
    out = []
    for g in games:
        home = g["home_team"] == HORNETS_ABBR
        opp = g["away_team"] if home else g["home_team"]
        day = _ord(g["game_date"])
        m = ratings.predict(g["home_team"], g["away_team"], day)
        ratings.last[HORNETS_ABBR] = day          # so the next forecast game sees the right rest
        if not m:
            continue
        a, b, p_win = _pick(*m, params["sd"], home)
        out.append((g["game_id"], g["game_date"], g["game_time"], opp, 1 if home else 0, g["arena"] or "",
                    a, b, round(p_win, 3), HORNETS_ABBR if a > b else opp,
                    TEAM_MODEL_VERSION, datetime.utcnow().isoformat()))
    save_game_predictions(season, out)
    logger.info(f"Team game predictions saved: {len(out)}")

def backtest_team_predictions(season, games, preds, params):
    """Predict every finished Hornets game that has no stored prediction, using ONLY games played before it
    (plus earlier seasons). Stored with source='backtest' so they are always labelled as such."""
    done = {r["game_id"] for r in query("SELECT game_id FROM game_predictions WHERE season=?", (season,))}
    rows = []
    for g in games:
        if g["season"] != season or HORNETS_ABBR not in (g["home_team"], g["away_team"]):
            continue
        if g["game_id"] in done or g["game_id"] not in preds:
            continue
        home = g["home_team"] == HORNETS_ABBR
        opp = g["away_team"] if home else g["home_team"]
        us, them = (g["hs"], g["as_"]) if home else (g["as_"], g["hs"])
        a, b, p_win = _pick(*preds[g["game_id"]], params["sd"], home)
        winner = HORNETS_ABBR if a > b else opp
        won = us > them
        correct = 1 if (winner == HORNETS_ABBR) == won else 0
        rows.append((season, g["game_id"], g["date"], None, opp, 1 if home else 0, "", a, b, round(p_win, 3), winner,
                     TEAM_MODEL_VERSION, datetime.utcnow().isoformat(),
                     us, them, "W" if won else "L", correct, "backtest"))
    if rows:
        execute_many(_insert_sql("game_predictions", GAME_PRED_COLS, "INSERT OR IGNORE"), rows)
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

def run_team_model(season, games):
    if not games:
        return
    prior = _prev_season(season)
    for s in (prior, season):
        raw = get_param(f"team_params_{s}")
        if not raw or s == season or "k_rest" not in json.loads(raw):      # (re-)learn after a model upgrade
            params, metrics = tune_team_model(s, games)
            set_param(f"team_params_{s}", json.dumps(params))
            set_param(f"team_metrics_{s}", json.dumps(metrics))
    # Back-tests made by an older model version are redone with the current one.
    execute("DELETE FROM game_predictions WHERE source='backtest' AND model_version<>?", (TEAM_MODEL_VERSION,))
    # Past seasons: one-time back-test of every game with that season's learned parameters. The earliest stored
    # season is skipped: with no season before it the model has nothing to start from.
    stored = sorted({g["season"] for g in games}, key=_season_start)
    for s in stored[1:]:
        if _season_start(s) >= _season_start(season):
            continue
        if query("SELECT 1 AS x FROM game_predictions WHERE season=? LIMIT 1", (s,)):
            continue
        if not get_param(f"team_params_{s}"):
            params, metrics = tune_team_model(s, games)
            set_param(f"team_params_{s}", json.dumps(params))
            set_param(f"team_metrics_{s}", json.dumps(metrics))
        p = team_params(s)
        preds, _, _ = walk(p, games)
        backtest_team_predictions(s, games, preds, p)
    p = team_params(season)
    preds, ratings, _ = walk(p, games)
    run_team_predictions(season, ratings, p)
    grade_team_predictions(season)                 # results for predictions locked in before tip-off
    backtest_team_predictions(season, games, preds, p)   # games played before tracking began

# ══════════════════════════════════════════════════════════════════════════════
#  PLAYER MODEL
# ══════════════════════════════════════════════════════════════════════════════
def _player_logs():
    """{player_id: [game rows, oldest first]} from the league-wide game logs (all teams, all stored seasons)."""
    logs = defaultdict(list)
    for r in query("SELECT player_id, player_name, season, game_id, opp_abbr, game_date_iso, min, pts, reb, ast "
                   "FROM league_player_games ORDER BY game_date_iso, game_id"):
        if r["min"] and r["min"] > 0:
            logs[r["player_id"]].append(r)
    return logs

def _base_stat(hist, stat, decay_min, decay):
    """Forecast minutes x per-minute rate for one stat from the player's previous games (oldest first)."""
    n = len(hist)
    minutes = weighted_avg([h["min"] for h in hist], decay_min)
    num = den = 0.0
    for k, h in enumerate(hist):
        w = decay ** (n - 1 - k)
        num += w * h[stat]
        den += w * h["min"]
    return minutes * num / den if den else 0.0

def _env_factor(stat, env, p):
    dr, pr = env
    f = 1.0 + p["w_pace"][stat] * (pr - 1.0)
    if stat == "pts":
        f += p["w_def"] * (dr - 1.0)
    return min(1.25, max(0.8, f))

def _legacy_stat(hist, stat):
    return weighted_avg([h[stat] for h in hist[-LEGACY_WINDOW:]], 0.85)

def _mean(xs):
    return sum(xs) / len(xs)

def tune_player_model(target_season, logs, env):
    """Learn the recency decays and the opponent-adjustment strengths from games before `target_season`, scored by
    mean absolute error per stat against the old plain-average model. Each forecast uses only earlier games."""
    tune, oos = [], []
    for games in logs.values():
        for i in range(MIN_GAMES, len(games)):
            g = games[i]
            hist = games[max(0, i - WINDOW):i]
            if _mean([h["min"] for h in hist]) < MIN_MPG:
                continue
            case = (hist, g, env.get(g["game_date_iso"], {}).get(g["opp_abbr"], (1.0, 1.0)))
            if _season_start(g["season"]) < _season_start(target_season):
                tune.append(case)
            elif g["season"] == target_season:
                oos.append(case)
    if len(tune) < 500:
        return json.loads(json.dumps(DEFAULT_PLAYER_PARAMS)), {}
    sub = tune[::max(1, len(tune) // 6000)]
    base_mae = {s: _mean([abs(_legacy_stat(h, s) - g[s]) for h, g, _ in sub]) for s in STATS}
    # Stage 1: how fast to forget, for minutes and for each stat's rate.
    best = None
    for dm in (0.7, 0.85, 1.0):
        chosen, total = {}, 0.0
        for s in STATS:
            sbest = min((_mean([abs(_base_stat(h, s, dm, d) - g[s]) for h, g, _ in sub]), d)
                        for d in (0.7, 0.8, 0.9, 1.0))
            chosen[s] = sbest[1]
            total += sbest[0] / base_mae[s]
        if best is None or total < best[0]:
            best = (total, dm, chosen)
    _, dm, decay = best
    base = {s: [_base_stat(h, s, dm, decay[s]) for h, _, _ in sub] for s in STATS}
    # Stage 2: how much the opponent's defense and the game's pace move the forecast.
    grid = (0.0, 0.5, 1.0, 1.5, 2.0)
    params = {"decay_min": dm, "decay": decay, "w_def": 0.0, "w_pace": {s: 0.0 for s in STATS}}
    def err(s, p):
        return _mean([abs(b * _env_factor(s, e, p) - g[s]) for b, (_, g, e) in zip(base[s], sub)])
    for s in ("reb", "ast"):
        params["w_pace"][s] = min((err(s, dict(params, w_pace=dict(params["w_pace"], **{s: w}))), w) for w in grid)[1]
    _, w_def, w_pace = min((err("pts", dict(params, w_def=wd, w_pace=dict(params["w_pace"], pts=wp))), wd, wp)
                           for wd, wp in product(grid, grid))
    params["w_def"], params["w_pace"]["pts"] = w_def, w_pace
    def mae_of(cases, fn):
        return {s: round(_mean([abs(fn(h, s, e) - g[s]) for h, g, e in cases]), 2) for s in STATS}
    new_fn = lambda h, s, e: _base_stat(h, s, params["decay_min"], params["decay"][s]) * _env_factor(s, e, params)
    old_fn = lambda h, s, e: _legacy_stat(h, s)
    metrics = {"cases": len(sub), "mae": mae_of(sub, new_fn), "baseline_mae": mae_of(sub, old_fn)}
    if len(oos) >= 100:
        step = max(1, len(oos) // 4000)
        metrics["out_of_sample"] = {"season": target_season, "cases": len(oos[::step]),
                                    "mae": mae_of(oos[::step], new_fn), "baseline_mae": mae_of(oos[::step], old_fn)}
    logger.info(f"Player model for {target_season}: {params} -> {metrics}")
    return params, metrics

def player_params():
    raw = get_param("player_params")
    return json.loads(raw) if raw else json.loads(json.dumps(DEFAULT_PLAYER_PARAMS))

def _confidence(pts_vals, pts_pred, n_cur, n):
    c = 1.0 - (std(pts_vals) / (pts_pred + 1))
    # Games from this season count fully; carried-over games from earlier seasons count half.
    sample = n_cur + 0.5 * (n - n_cur)
    c *= min(1.0, sample / float(WINDOW))
    return round(max(0.10, min(0.85, c)), 3)

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
            f"UNION SELECT {cols} FROM player_stats WHERE player_id=? AND season != ?) AS h "
            "ORDER BY game_date_iso DESC, game_id DESC LIMIT ?",
            (player_id, player_id, season, window))
        for r in older:
            if r["game_id"] not in seen and len(rows) < window:
                rows.append(r); seen.add(r["game_id"])
    return rows, n_cur

def predict_legacy(player_id, player_name, future_games, season):
    """The old plain recency-weighted average of the last 8 games, for players the league logs don't cover yet."""
    games, n_cur = _history(player_id, season, LEGACY_WINDOW)
    if len(games) < MIN_GAMES: return []
    games = list(reversed(games))          # oldest -> newest, so recent games weigh more
    pred = {s: weighted_avg([g[s] for g in games]) for s in STATS}
    conf = _confidence([g["pts"] for g in games], pred["pts"], n_cur, len(games))
    return [(player_id, player_name, fg["date"], fg["opponent"], round(pred["pts"], 1), round(pred["reb"], 1),
             round(pred["ast"], 1), conf, LEGACY_PLAYER_VERSION, datetime.utcnow().isoformat())
            for fg in future_games]

def run_player_predictions(season, games):
    future = upcoming_games(season)
    if not future:
        clear_predictions(season)
        logger.info(f"No upcoming games for {season}; forecasts cleared.")
        return
    logs = _player_logs() if games else {}
    ratings, params = None, None
    if logs:
        _, ratings, env = walk(team_params(season), games, want_env=True)
        params, metrics = tune_player_model(season, logs, env)
        set_param("player_params", json.dumps(params))
        set_param("player_metrics", json.dumps(metrics))
    players = query("SELECT player_id, player_name FROM roster WHERE season=?", (season,))
    all_rows = []
    for p in players:
        hist = logs.get(p["player_id"], [])[-WINDOW:] if logs else []
        if len(hist) < MIN_GAMES:
            rows = predict_legacy(p["player_id"], p["player_name"], future, season)
        else:
            n_cur = sum(1 for h in hist if h["season"] == season)
            conf = _confidence([h["pts"] for h in hist], _base_stat(hist, "pts", params["decay_min"],
                                                                    params["decay"]["pts"]), n_cur, len(hist))
            base = {s: _base_stat(hist, s, params["decay_min"], params["decay"][s]) for s in STATS}
            rows = []
            for fg in future:
                e = ratings.env(fg["opp"])
                f = {s: max(0.0, base[s] * _env_factor(s, e, params)) for s in STATS}
                rows.append((p["player_id"], p["player_name"], fg["date"], fg["opponent"], round(f["pts"], 1),
                             round(f["reb"], 1), round(f["ast"], 1), conf, PLAYER_MODEL_VERSION,
                             datetime.utcnow().isoformat()))
        if rows:
            logger.info(f"Generated {len(rows)} predictions for {p['player_name']}")
        all_rows.extend(rows)
    if all_rows:
        clear_predictions(season)
        save_predictions(all_rows, season)
    logger.info("Predictions saved.")

def run_all_predictions(season=None):
    season = season or current_season()
    logger.info(f"Running predictions for {season}...")
    games = []
    try:
        games = _games()
        run_team_model(season, games)
    except Exception as e:
        logger.exception(f"Team prediction error: {e}")
    try:
        run_player_predictions(season, games)
    except Exception as e:
        logger.exception(f"Player prediction error: {e}")

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
