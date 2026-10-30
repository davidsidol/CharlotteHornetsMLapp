"""
Charlotte Hornets Dashboard — Production App
- Waitress WSGI server (Windows-compatible)
- Multi-season: every data endpoint accepts ?season=YYYY-YY (defaults to the current season)
- Auto-refresh every 4 hours via background scheduler (current season only)
- Server-Sent Events for live push to browser
- SQLite by default; SQL Server with DB_BACKEND=mssql + MSSQL_SERVER / MSSQL_DATABASE (see database.py)
"""
import logging
import threading
import time
import json
import queue
import os
import re
from datetime import datetime, date
from flask import Flask, jsonify, render_template, Response, request, stream_with_context

from database import init_db, query, current_season, season_label
from fetcher import refresh_all, PINNED_PLAYERS, HORNETS_ABBR
from ml_predictions import run_all_predictions, get_player_averages

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.environ.get("LOG_DIR", BASE_DIR)
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "hornets.log"), mode='a', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"), static_folder=os.path.join(BASE_DIR, "static"))

REFRESH_INTERVAL_HOURS = 4
STARTUP_STALE_HOURS = 1       # on boot, refresh in the background if current-season data is older than this
FEATURED_COUNT = 4            # cards on the Overview
MAX_PLAYERS = 12              # cards on the Players tab
_sse_clients = []
_sse_lock = threading.Lock()

def broadcast_update(event_type, data):
    msg = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try: q.put_nowait(msg)
            except: dead.append(q)
        for q in dead: _sse_clients.remove(q)

def _do_refresh():
    try:
        logger.info("Auto-refresh: starting data pull...")
        broadcast_update("status", {"message": "Fetching live NBA data...", "refreshing": True})
        refresh_all()
        broadcast_update("status", {"message": "Running ML predictions...", "refreshing": True})
        run_all_predictions()
        broadcast_update("status", {"message": "Data updated!", "refreshing": False})
        broadcast_update("refresh", {"ts": datetime.utcnow().isoformat()})
        logger.info("Auto-refresh: complete.")
    except Exception as e:
        logger.error(f"Refresh error: {e}")
        broadcast_update("status", {"message": f"Refresh error: {e}", "refreshing": False})

def _scheduler_loop():
    while True:
        time.sleep(REFRESH_INTERVAL_HOURS * 3600)
        _do_refresh()

def start_scheduler():
    t = threading.Thread(target=_scheduler_loop, daemon=True, name="RefreshScheduler")
    t.start()
    logger.info(f"Scheduler started — auto-refresh every {REFRESH_INTERVAL_HOURS}h")

# ── helpers ───────────────────────────────────────────────────────────────────
def _season_arg():
    s = (request.args.get("season") or "").strip()
    return s if re.fullmatch(r"\d{4}-\d{2}", s) else current_season()

def _available_seasons():
    # league_games holds every season the model learns from, so a season gets a tab even before its
    # Hornets stats are backfilled (fetcher.backfill_past_seasons fills them in on the next refresh).
    rows = query("SELECT DISTINCT season FROM team_games WHERE season IS NOT NULL "
                 "UNION SELECT DISTINCT season FROM schedule WHERE season IS NOT NULL "
                 "UNION SELECT DISTINCT season FROM league_games WHERE season IS NOT NULL AND team_abbr=?",
                 (HORNETS_ABBR,))
    return sorted({r["season"] for r in rows} | {current_season()}, reverse=True)

# A game that has not tipped off yet: later day, or today with a later (or unknown) tip time.
# Written without string concatenation so it runs on SQLite and SQL Server alike.
_NOT_TIPPED = "(game_date > ? OR (game_date = ? AND COALESCE(game_time, '00:00') > ?))"

def _now_params():
    now = datetime.now()                                  # server clock is Eastern, like the stored tip times
    today = now.strftime("%Y-%m-%d")
    return (today, today, now.strftime("%H:%M"))

# ── routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def api_status():
    cs     = current_season()
    cnt    = query("SELECT COUNT(*) as c FROM player_stats")[0]["c"]
    last   = query("SELECT MAX(fetched_at) as t FROM player_stats")[0]["t"]
    pred   = query("SELECT COUNT(*) as c FROM predictions")[0]["c"]
    tgames = query("SELECT COUNT(*) as c FROM team_games")[0]["c"]
    cur_games = query("SELECT COUNT(*) as c FROM team_games WHERE season=?", (cs,))[0]["c"]
    return jsonify({"player_stat_rows": cnt, "team_game_rows": tgames,
                    "prediction_rows": pred, "last_fetch": last, "db_ready": cnt > 0,
                    "current_season": cs, "current_season_games": cur_games,
                    "next_refresh_hours": REFRESH_INTERVAL_HOURS, "server_time": datetime.utcnow().isoformat()})

@app.route("/api/seasons")
def api_seasons():
    cs = current_season()
    return jsonify({"current": cs,
                    "seasons": [{"id": s, "label": season_label(s), "current": s == cs}
                                for s in _available_seasons()]})

@app.route("/api/team")
def api_team():
    season   = _season_arg()
    info     = query("SELECT * FROM team_info WHERE season=? ORDER BY updated_at DESC LIMIT 1", (season,))
    games    = query("SELECT * FROM team_games WHERE season=? ORDER BY game_date_iso DESC, game_id DESC", (season,))
    upcoming = query("SELECT * FROM schedule WHERE season=? AND status != 'Final' "
                     "ORDER BY game_date ASC, game_id ASC", (season,))
    return jsonify({"season": season, "is_current": season == current_season(),
                    "info": info[0] if info else {}, "recent_games": games, "upcoming": upcoming})

@app.route("/api/players")
def api_players():
    """Players who played for the Hornets this season, plus pinned players still on the roster.
    Pinned players come first, then everyone else by minutes per game."""
    season = _season_arg()
    roster = {r["player_id"]: r for r in query("SELECT * FROM roster WHERE season=?", (season,))}
    played = {r["player_id"]: r["player_name"] for r in query(
        "SELECT player_id, player_name FROM player_stats WHERE season=? GROUP BY player_id, player_name", (season,))}
    ids = dict(played)
    for pid, name in PINNED_PLAYERS.items():
        if pid in roster:
            ids.setdefault(pid, roster[pid]["player_name"] or name)

    entries = []
    for pid, pname in ids.items():
        avgs = get_player_averages(pid, 15, season)
        r = roster.get(pid, {})
        entries.append({
            "name": pname, "player_id": pid,
            "number": r.get("number") or "", "position": r.get("position") or "",
            "averages": avgs,
            "recent_games": query("SELECT * FROM player_stats WHERE player_id=? AND season=? "
                                  "ORDER BY game_date_iso DESC, game_id DESC LIMIT 20", (pid, season)),
        })
    pinned = list(PINNED_PLAYERS.keys())
    entries.sort(key=lambda e: (0, pinned.index(e["player_id"])) if e["player_id"] in pinned
                 else (1, -e["averages"].get("mpg", 0)))
    result = {}
    for i, e in enumerate(entries[:MAX_PLAYERS]):
        result[e["name"]] = {"player_id": e["player_id"], "number": e["number"], "position": e["position"],
                             "averages": e["averages"], "recent_games": e["recent_games"],
                             "rank": i, "featured": i < FEATURED_COUNT}
    return jsonify(result)

@app.route("/api/player/<int:pid>")
def api_player(pid):
    """Full season game-by-game log + season averages for one player."""
    season = _season_arg()
    games  = query("SELECT * FROM player_stats WHERE player_id=? AND season=? "
                   "ORDER BY game_date_iso DESC, game_id DESC", (pid, season))
    r = query("SELECT * FROM roster WHERE season=? AND player_id=?", (season, pid))
    name = (r[0]["player_name"] if r else None) or (games[0]["player_name"] if games else None)
    if not name:
        return jsonify({"error": "player not found"}), 404
    ro = r[0] if r else {}
    return jsonify({"player_id": pid, "name": name, "season": season,
                    "number": ro.get("number") or "", "position": ro.get("position") or "",
                    "averages": get_player_averages(pid, 200, season), "games": games})

@app.route("/api/game_predictions")
def api_game_predictions():
    """Predicted score + winner for the Hornets' next games."""
    season = _season_arg()
    return jsonify(query("SELECT * FROM game_predictions WHERE season=? AND result IS NULL AND " + _NOT_TIPPED +
                         " ORDER BY game_date ASC, game_id ASC", (season,) + _now_params()))

@app.route("/api/game_record")
def api_game_record():
    """Track record of the team game predictions: correct / wrong, accuracy, and the latest graded games."""
    season = _season_arg()
    rows = query("SELECT * FROM game_predictions WHERE season=? AND result IS NOT NULL "
                 "ORDER BY game_date DESC, game_id DESC", (season,))
    total = len(rows)
    ok = sum(r["correct"] or 0 for r in rows)
    def split(win_call):
        sub = [r for r in rows if (r["winner"] == "CHA") == win_call]
        return {"correct": sum(r["correct"] or 0 for r in sub), "total": len(sub)}
    return jsonify({"season": season, "total": total, "correct": ok, "wrong": total - ok,
                    "pct": round(100.0 * ok / total, 1) if total else None,
                    "win_calls": split(True), "loss_calls": split(False),
                    "backtest": sum(1 for r in rows if r["source"] == "backtest"),
                    "recent": rows[:5]})

@app.route("/api/model_form")
def api_model_form():
    """Hot/cold form of the team-game predictions plus the model's most confident bet for the next game."""
    from statistics import NormalDist
    from ml_predictions import margin_sd
    season = _season_arg()
    MARGIN_SD = margin_sd(season)
    rows = query("SELECT * FROM game_predictions WHERE season=? AND result IS NOT NULL "
                 "ORDER BY game_date ASC, game_id ASC", (season,))
    seq = [bool(r["correct"]) for r in rows]
    n = len(seq)
    streak, kind = 0, None
    if seq:
        kind = seq[-1]
        for v in reversed(seq):
            if v != kind:
                break
            streak += 1
    last5 = seq[-5:]
    l5 = sum(last5)
    frac = (l5 / len(last5)) if last5 else 0
    if n < 3:
        status = "none"
    elif (kind and streak >= 3) or (len(last5) >= 4 and frac >= 0.8):
        status = "hot"
    elif ((not kind) and streak >= 2) or (len(last5) >= 4 and frac <= 0.4):
        status = "cold"
    else:
        status = "steady"
    form = [{"ok": bool(r["correct"]), "opp": r["opponent"], "home": bool(r["home"]), "date": r["game_date"]}
            for r in rows[-10:]]

    best = None
    nxt = query("SELECT * FROM game_predictions WHERE season=? AND result IS NULL AND " + _NOT_TIPPED +
                " ORDER BY game_date ASC, game_id ASC LIMIT 1", (season,) + _now_params())
    if nxt:
        g = nxt[0]
        pw = float(g["win_prob"])
        pick_cha = pw >= 0.5
        conf = pw if pick_cha else 1 - pw
        nd = NormalDist()
        margin = MARGIN_SD * nd.inv_cdf(pw)               # predicted Hornets margin
        fav = abs(margin)
        by5 = 1 - nd.cdf((5 - fav) / MARGIN_SD)           # chance the favourite wins by 5+
        if conf >= 0.75:   tier = "Strong"
        elif conf >= 0.65: tier = "Solid"
        elif conf >= 0.58: tier = "Lean"
        else:              tier = "Pass"
        similar = [r for r in rows if abs(max(float(r["win_prob"]), 1 - float(r["win_prob"])) - conf) <= 0.08]
        best = {"game_date": g["game_date"], "game_time": g["game_time"], "home": bool(g["home"]),
                "opponent": g["opponent"], "pick": "CHA" if pick_cha else g["opponent"],
                "pick_is_cha": pick_cha, "confidence": round(conf * 100), "tier": tier,
                "fav_margin": round(fav, 1), "win_by_5": round(by5 * 100),
                "pred_pts": g["pred_pts"], "pred_opp_pts": g["pred_opp_pts"],
                "similar": {"correct": sum(1 for r in similar if r["correct"]), "total": len(similar)}}
    return jsonify({"season": season, "graded": n, "status": status, "streak": streak,
                    "streak_kind": ("W" if kind else "L") if seq else None,
                    "last5": {"correct": l5, "total": len(last5)},
                    "last10": {"correct": sum(seq[-10:]), "total": len(seq[-10:])},
                    "form": form, "best": best})

_LIVE_CACHE = {"ts": 0.0, "data": None}
_LIVE_LOCK = threading.Lock()

def _live_from_espn():
    import requests
    r = requests.get("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", timeout=6)
    r.raise_for_status()
    for e in r.json().get("events", []):
        c = e["competitions"][0]
        side = {x["homeAway"]: x for x in c["competitors"]}
        h, a = side["home"], side["away"]
        abbr = lambda x: x["team"]["abbreviation"]
        if "CHA" not in (abbr(h), abbr(a)):
            continue
        st = c["status"]
        state = st["type"]["state"]                      # pre | in | post
        period, clock = st.get("period") or 0, st.get("displayClock") or ""
        name = st["type"].get("name", "")
        if state == "in":
            if "HALFTIME" in name:
                label = "Halftime"
            elif "END_PERIOD" in name or clock in ("0.0", "0:00"):
                label = st["type"].get("shortDetail") or "End of period"
            else:
                q = f"Q{period}" if period <= 4 else ("OT" if period == 5 else f"{period - 4}OT")
                label = f"{q} {clock}"
        elif state == "post":
            label = "Final" + ("" if period <= 4 else ("/OT" if period == 5 else f"/{period - 4}OT"))
        else:
            label = "Scheduled"
        num = lambda x: int(x["score"]) if str(x.get("score", "")).strip().isdigit() else None
        return {"state": state, "label": label, "start": e.get("date"),
                "home": {"abbr": abbr(h), "score": num(h)}, "away": {"abbr": abbr(a), "score": num(a)},
                "cha_home": abbr(h) == "CHA", "source": "espn"}
    return {"state": "none"}

def _live_from_nba():
    from nba_api.stats.endpoints import scoreboardv3
    d = scoreboardv3.ScoreboardV3(game_date=date.today().isoformat(), timeout=10).get_dict()["scoreboard"]
    for g in d["games"]:
        h, a = g["homeTeam"], g["awayTeam"]
        if "CHA" not in (h["teamTricode"], a["teamTricode"]):
            continue
        state = {1: "pre", 2: "in", 3: "post"}.get(g["gameStatus"], "pre")
        label = (g.get("gameStatusText") or "").strip()
        m = re.match(r"PT(\d+)M([\d.]+)S", g.get("gameClock") or "")
        if state == "in" and m and g.get("period"):
            p = g["period"]
            q = f"Q{p}" if p <= 4 else ("OT" if p == 5 else f"{p - 4}OT")
            label = f"{q} {int(m.group(1))}:{int(float(m.group(2))):02d}"
        return {"state": state, "label": label, "start": g.get("gameTimeUTC"),
                "home": {"abbr": h["teamTricode"], "score": h.get("score")},
                "away": {"abbr": a["teamTricode"], "score": a.get("score")},
                "cha_home": h["teamTricode"] == "CHA", "source": "nba"}
    return {"state": "none"}

@app.route("/api/live")
def api_live():
    """Today's Hornets game: live score and clock (cached 15s; ESPN first, NBA stats as a fallback)."""
    with _LIVE_LOCK:
        if _LIVE_CACHE["data"] is not None and time.time() - _LIVE_CACHE["ts"] < 15:
            return jsonify(_LIVE_CACHE["data"])
        data = None
        for fn in (_live_from_espn, _live_from_nba):
            try:
                data = fn()
                break
            except Exception as ex:
                logger.warning("live score via %s failed: %s", fn.__name__, ex)
        if data is None:
            data = {"state": "error"}
        _LIVE_CACHE.update(ts=time.time(), data=data)
        return jsonify(data)

@app.route("/api/predictions")
def api_predictions():
    season = _season_arg()
    today  = date.today().isoformat()
    rows = query("SELECT * FROM predictions WHERE season=? AND game_date >= ? "
                 "ORDER BY player_name, game_date ASC", (season, today))
    by_player = {}
    for r in rows:
        by_player.setdefault(r["player_name"], []).append(r)
    return jsonify(by_player)

@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    threading.Thread(target=_do_refresh, daemon=True).start()
    return jsonify({"status": "refresh started"})

@app.route("/api/stream")
def api_stream():
    client_q = queue.Queue(maxsize=20)
    with _sse_lock:
        _sse_clients.append(client_q)
    def generate():
        yield "event: connected\ndata: {\"message\": \"Live connection established\"}\n\n"
        try:
            while True:
                try: yield client_q.get(timeout=25)
                except queue.Empty: yield ": heartbeat\n\n"
        except GeneratorExit:
            pass
        finally:
            with _sse_lock:
                try: _sse_clients.remove(client_q)
                except ValueError: pass
    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

def _current_season_age_hours():
    row = query("SELECT COUNT(*) AS c, MAX(fetched_at) AS t FROM team_games WHERE season=?", (current_season(),))[0]
    if not row["c"] or not row["t"]:
        return None
    try:
        return (datetime.utcnow() - datetime.fromisoformat(row["t"])).total_seconds() / 3600.0
    except Exception:
        return None

def startup():
    init_db()
    cnt = query("SELECT COUNT(*) as c FROM player_stats")[0]["c"]
    age = _current_season_age_hours()
    if cnt == 0 or age is None or age > STARTUP_STALE_HOURS:
        logger.info(f"Current season ({current_season()}) data missing or stale — refreshing in the background...")
        threading.Thread(target=_do_refresh, daemon=True, name="StartupRefresh").start()
    else:
        logger.info(f"DB ready: {cnt} player-game rows loaded.")
    start_scheduler()

startup()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False)
