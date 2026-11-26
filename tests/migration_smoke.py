"""Offline end-to-end check using NBA-shaped fixtures and the original Python models."""
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def upstream():
    """Stand in for curl for this child process only."""
    args = dict(arg.split("=", 1) for arg in sys.argv[1:] if "=" in arg)
    endpoint = sys.argv[-1].split("/")[-1]
    if endpoint == "scoreboard":
        return {"events": []}
    season = args["Season"]
    year = int(season[:4])
    prefix = f"002{year % 100:02d}"
    if endpoint == "leaguegamelog":
        rows = []
        for day in range(1, 7):
            for team, opp, pts, margin in [("CHA", "BOS", 110+day, 5), ("BOS", "CHA", 105+day, -5)]:
                player = args["PlayerOrTeam"] == "P"
                rows.append(dict(GAME_ID=prefix+f"{day:05d}", GAME_DATE=f"{year}-10-{day:02d}",
                                 TEAM_ABBREVIATION=team, MATCHUP=f"{team} {'vs.' if team=='CHA' else '@'} {opp}",
                                 WL="W" if margin > 0 else "L", PTS=day+10 if player else pts,
                                 PLUS_MINUS=margin, REB=5, AST=3, MIN=32, FG_PCT=0.465,
                                 PLAYER_ID=1641706 if team=="CHA" else 100, PLAYER_NAME=team+" player"))
        name = "LeagueGameLog"
    elif endpoint == "scheduleleaguev2":
        tip = (date.today()+timedelta(days=3)).isoformat()
        return {"leagueSchedule":{"gameDates":[{"games":[dict(gameId=prefix+"00800",
                homeTeam={"teamTricode":"CHA"}, awayTeam={"teamTricode":"BOS"},
                gameStatus=1, gameDateTimeEst=tip+"T19:00:00Z", arenaName="Charlotte")]}]}}
    elif endpoint == "commonteamroster":
        name = "CommonTeamRoster"
        rows = [dict(PLAYER_ID=1641706, PLAYER="CHA player", NUM="24", POSITION="F")]
    elif endpoint == "teaminfocommon":
        name = "TeamInfoCommon"
        rows = [dict(W=6, L=0, PCT=1, CONF_RANK=1, DIV_RANK=1, SEASON_YEAR=season)]
    else:
        raise ValueError(endpoint)
    return {"resultSets":[{"name":name,"headers":list(rows[0]),"rowSet":[list(r.values()) for r in rows]}]}


def run(binary, folder):
    import database as db
    import ml_predictions as ml
    path = folder/"hornets.db"
    shim = folder/"curl"
    shim.write_text("#!/usr/bin/env python3\n" + Path(__file__).read_text())
    shim.chmod(0o755)
    env = dict(os.environ, DB_BACKEND="sqlite", DB_PATH=str(path), PATH=str(folder)+os.pathsep+os.environ["PATH"])
    subprocess.run([binary, "refresh"], env=env, check=True)
    season = db.current_season()
    reference = folder/"reference.db"
    with sqlite3.connect(path) as orig, sqlite3.connect(reference) as copy:
        orig.backup(copy)
    db.DB_PATH = str(reference)
    with sqlite3.connect(reference) as c:
        for table in ["game_predictions", "predictions", "model_params"]:
            c.execute(f"DELETE FROM {table}")
    ml.run_all_predictions(season)
    with sqlite3.connect(path) as actual, sqlite3.connect(reference) as expected:
        for table, cols in [
            ("predictions", "player_id,game_date,opponent,pred_pts,pred_reb,pred_ast,confidence,model_version"),
            ("game_predictions", "season,game_id,pred_pts,pred_opp_pts,win_prob,winner,result,correct,source"),
        ]:
            query = f"SELECT {cols} FROM {table} ORDER BY 1,2"
            got = actual.execute(query).fetchall()
            want = expected.execute(query).fetchall()
            assert got and got == want, (table, got, want)
        # An actual live call must keep its winner and prediction when later graded.
        gid = actual.execute("SELECT game_id FROM game_predictions WHERE source='live'").fetchone()[0]
        actual.execute("UPDATE game_predictions SET winner='BOS',pred_pts=91 WHERE game_id=?", (gid,))
        actual.execute("UPDATE schedule SET status='Final' WHERE game_id=?", (gid,))
        for team, h, points, opp in [("CHA",1,108,99),("BOS",0,99,108)]:
            actual.execute("INSERT INTO league_games VALUES(?,?,?,?,?,?,?)",
                           (season,gid,team,date.today().isoformat(),points,opp,h))
        actual.commit()
    subprocess.run([binary,"predict"],env=env,check=True)
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT winner,pred_pts,source,result,correct FROM game_predictions WHERE game_id=?",(gid,)).fetchone()==("BOS",91,"live","W",0)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        port = sock.getsockname()[1]
    env.update(HOST="127.0.0.1",PORT=str(port))
    base=f"http://127.0.0.1:{port}"
    def get(route):
        with urlopen(base+route,timeout=5) as res:
            return json.load(res)
    proc=subprocess.Popen([binary],env=env)
    try:
        for _ in range(100):
            try:
                if get("/api/status")["db_ready"]:
                    break
            except OSError:
                time.sleep(0.05)
        assert get("/api/seasons")["current"]==season
        assert len(get("/api/team")["recent_games"])==6
        assert get("/api/players")["CHA player"]["averages"]["games"]==6
        assert get("/api/player/1641706")["averages"]["mpg"]==32
        for route in ["predictions","game_predictions","game_record","model_form","live"]:
            get("/api/"+route)
        for route in ["/", "/static/HornetsLogo_bg.png"]:
            with urlopen(base+route) as res:
                assert res.read(50)
        with urlopen(base+"/api/stream",timeout=3) as res:
            assert res.readline().strip()==b"event: connected"
        try:
            get("/api/player/99999")
            raise AssertionError("unknown player accepted")
        except HTTPError as e:
            assert e.code==404
        with urlopen(Request(base+"/api/refresh",method="POST")) as res:
            assert json.load(res)["status"]=="refresh started"
        with urlopen(Request(base+"/api/refresh",method="POST")) as res:
            assert json.load(res)["status"]=="refresh already running"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    # Windows SQLite export must preserve the snapshot and refuse overwrites.
    export=folder/"windows-export.db"
    cmd=[sys.executable,str(ROOT/"export_windows_db.py"),str(export)]
    subprocess.run(cmd,env=env,check=True)
    assert subprocess.run(cmd,env=env,capture_output=True).returncode != 0
    with sqlite3.connect(export) as c:
        assert c.execute("SELECT count(*) FROM player_stats").fetchone()[0]==24
    print("Refresh, Python model comparisons, grading, HTTP/SSE and SQLite export passed.")


if __name__ == "__main__":
    if Path(sys.argv[0]).name=="curl":
        print(json.dumps(upstream()))
    else:
        binary=str(Path(sys.argv[1]).resolve())
        with tempfile.TemporaryDirectory() as tmp:
            run(binary, Path(tmp))
