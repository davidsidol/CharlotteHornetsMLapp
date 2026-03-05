"""
Charlotte Hornets Dashboard — Production App
- Waitress WSGI server (Windows-compatible)
- Auto-refresh every 4 hours via background scheduler
- Server-Sent Events for live push to browser
- MSSQL-ready (set USE_MSSQL=true + MSSQL_CONN_STR env vars)
"""
import logging
import threading
import time
import json
import queue
import os
from datetime import datetime
from flask import Flask, jsonify, render_template, Response, stream_with_context

from database import init_db, query
from fetcher import refresh_all
from ml_predictions import run_all_predictions, get_player_averages

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(BASE_DIR, "hornets.log")),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"), static_folder=os.path.join(BASE_DIR, "static"))

PLAYERS = {
    1630163: "LaMelo Ball",
    1641706: "Brandon Miller",
    1642851: "Kon Knueppel",
    1628970: "Miles Bridges",
}

REFRESH_INTERVAL_HOURS = 4
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

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def api_status():
    cnt    = query("SELECT COUNT(*) as c FROM player_stats")[0]["c"]
    last   = query("SELECT MAX(fetched_at) as t FROM player_stats")[0]["t"]
    pred   = query("SELECT COUNT(*) as c FROM predictions")[0]["c"]
    tgames = query("SELECT COUNT(*) as c FROM team_games")[0]["c"]
    return jsonify({"player_stat_rows": cnt, "team_game_rows": tgames,
                    "prediction_rows": pred, "last_fetch": last, "db_ready": cnt > 0,
                    "next_refresh_hours": REFRESH_INTERVAL_HOURS, "server_time": datetime.utcnow().isoformat()})

@app.route("/api/team")
def api_team():
    info     = query("SELECT * FROM team_info ORDER BY updated_at DESC LIMIT 1")
    games    = query("SELECT * FROM team_games ORDER BY game_date DESC LIMIT 30")
    schedule = query("SELECT * FROM schedule ORDER BY game_date DESC LIMIT 50")
    return jsonify({"info": info[0] if info else {}, "recent_games": games, "schedule": schedule})

@app.route("/api/players")
def api_players():
    result = {}
    for pid, pname in PLAYERS.items():
        recent   = query("SELECT * FROM player_stats WHERE player_id=? ORDER BY game_date DESC LIMIT 20", (pid,))
        averages = get_player_averages(pid, 15)
        result[pname] = {"recent_games": recent, "averages": averages, "player_id": pid}
    return jsonify(result)

@app.route("/api/predictions")
def api_predictions():
    rows = query("SELECT * FROM predictions ORDER BY player_name, game_date ASC")
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

def startup():
    init_db()
    cnt = query("SELECT COUNT(*) as c FROM player_stats")[0]["c"]
    if cnt == 0:
        logger.info("Empty DB — running initial fetch...")
        _do_refresh()
    else:
        logger.info(f"DB ready: {cnt} player-game rows loaded.")
    start_scheduler()

startup()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False)
