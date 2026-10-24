"""
Charlotte Hornets Dashboard — Production App
- Waitress WSGI server (Windows-compatible)
- Multi-season: every data endpoint accepts ?season=YYYY-YY (defaults to the current season)
- Auto-refresh every 4 hours via background scheduler (current season only)
- Server-Sent Events for live push to browser
- MSSQL-ready (set USE_MSSQL=true + MSSQL_CONN_STR env vars)
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
from fetcher import refresh_all, PINNED_PLAYERS
from ml_predictions import run_all_predictions, get_player_averages

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(BASE_DIR, "hornets.log"), mode='a'),
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
    rows = query("SELECT DISTINCT season FROM team_games WHERE season IS NOT NULL "
                 "UNION SELECT DISTINCT season FROM schedule WHERE season IS NOT NULL")
    return sorted({r["season"] for r in rows} | {current_season()}, reverse=True)

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
    today  = date.today().isoformat()
    return jsonify(query("SELECT * FROM game_predictions WHERE season=? AND game_date >= ? "
                         "ORDER BY game_date ASC, game_id ASC", (season, today)))

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
