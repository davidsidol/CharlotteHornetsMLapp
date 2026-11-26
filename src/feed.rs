use crate::db::*;
use anyhow::{anyhow, ensure, Context, Result};
use rusqlite::{params, Connection};
use serde_json::{json, Map, Value};
use std::process::Command;

pub(crate) fn download(url: &str, args: &[(&str, &str)], timeout: &str) -> Result<Value> {
    let mut cmd = Command::new("curl");
    cmd.args([
        "--disable",
        "--fail",
        "--silent",
        "--show-error",
        "--compressed",
        "--get",
        "--connect-timeout",
        "8",
        "--max-time",
        timeout,
        "-A",
        "Mozilla/5.0",
        "-H",
        "Accept: application/json, text/plain, */*",
        "-H",
        "Origin: https://www.nba.com",
        "-e",
        "https://www.nba.com/",
    ]);
    for (k, v) in args {
        cmd.args(["--data-urlencode", &format!("{k}={v}")]);
    }
    let r = cmd.arg(url).output().context("starting curl")?;
    ensure!(
        r.status.success(),
        "{}: {}",
        url,
        String::from_utf8_lossy(&r.stderr).trim()
    );
    Ok(serde_json::from_slice(&r.stdout)?)
}
fn nba(endpoint: &str, args: &[(&str, &str)]) -> Result<Value> {
    download(
        &format!("https://stats.nba.com/stats/{endpoint}"),
        args,
        "90",
    )
}
fn records(payload: Value, name: &str) -> Result<Vec<Value>> {
    let set = payload["resultSets"]
        .as_array()
        .and_then(|sets| sets.iter().find(|x| s(x, "name") == name))
        .context("missing NBA result set")?;
    let headers = set["headers"].as_array().context("missing headers")?;
    set["rowSet"]
        .as_array()
        .context("missing rows")?
        .iter()
        .map(|r| {
            let vals = r.as_array().context("invalid NBA row")?;
            ensure!(vals.len() == headers.len(), "NBA header mismatch");
            let mut obj = Map::new();
            for (h, v) in headers.iter().zip(vals) {
                obj.insert(
                    h.as_str().context("bad NBA field")?.to_lowercase(),
                    v.clone(),
                );
            }
            Ok(Value::Object(obj))
        })
        .collect()
}
fn league(season: &str, kind: &str) -> Result<Vec<Value>> {
    records(
        nba(
            "leaguegamelog",
            &[
                ("Counter", "0"),
                ("DateFrom", ""),
                ("DateTo", ""),
                ("Direction", "ASC"),
                ("LeagueID", "00"),
                ("PlayerOrTeam", kind),
                ("Season", season),
                ("SeasonType", "Regular Season"),
                ("Sorter", "DATE"),
            ],
        )?,
        "LeagueGameLog",
    )
}
pub(crate) fn store(c: &Connection, table: &str, cols: &str, rows: &[Value]) -> Result<()> {
    let fields = cols.split_whitespace().collect::<Vec<_>>();
    let sql = format!(
        "INSERT OR REPLACE INTO {table} ({}) SELECT {} FROM json_each(?)",
        fields.join(","),
        fields
            .iter()
            .map(|k| format!("json_extract(value,'$.{k}')"))
            .collect::<Vec<_>>()
            .join(",")
    );
    for chunk in rows.chunks(1000) {
        c.execute(&sql, [serde_json::to_string(chunk)?])?;
    }
    Ok(())
}
fn save_logs(c: &Connection, season: &str, players: bool, mut logs: Vec<Value>) -> Result<()> {
    logs.retain(|r| matches!(s(r, "wl"), "W" | "L"));
    let mut hornets = vec![];
    let ts = stamp();
    for r in &mut logs {
        let day = s(r, "game_date")
            .get(..10)
            .context("missing game date")?
            .to_owned();
        let date = chrono::NaiveDate::parse_from_str(&day, "%Y-%m-%d")?;
        let matchup = s(r, "matchup").to_owned();
        let abbr = s(r, "team_abbreviation").to_owned();
        ensure!(
            !s(r, "game_id").is_empty() && !abbr.is_empty(),
            "incomplete league log"
        );
        r["season"] = json!(season);
        r["team_abbr"] = json!(abbr);
        r["game_date_iso"] = json!(day);
        r["home"] = json!(i32::from(!matchup.contains('@')));
        r["opp_abbr"] = json!(matchup.split_whitespace().last());
        r["opp_pts"] = json!(num(r, "pts") - num(r, "plus_minus"));
        if players {
            ensure!(
                r["player_id"].as_i64().is_some() && !s(r, "player_name").is_empty(),
                "invalid NBA player log"
            );
        }
        if abbr == "CHA" {
            let mut h = r.clone();
            h["game_date"] = json!(date.format("%b %d, %Y").to_string());
            h["fetched_at"] = json!(ts);
            if players {
                h["min"] = json!(round(num(r, "min"), 0).to_string());
            }
            hornets.push(h);
        }
    }
    let tx = c.unchecked_transaction()?;
    if players {
        logs.retain(|r| num(r, "min") > 0.);
        store(c,"league_player_games","season game_id player_id player_name team_abbr opp_abbr game_date_iso home min pts reb ast",&logs)?;
        store(c,"player_stats","player_id player_name game_id game_date matchup wl pts reb ast min fgm fga fg_pct fg3m fg3a fg3_pct ftm fta ft_pct stl blk tov plus_minus fetched_at season game_date_iso",&hornets)?;
    } else {
        store(
            c,
            "league_games",
            "season game_id team_abbr game_date_iso pts opp_pts home",
            &logs,
        )?;
        store(c,"team_games","game_id game_date matchup wl pts opp_pts fg_pct fg3_pct ft_pct reb ast stl blk tov fetched_at season game_date_iso",&hornets)?;
    }
    tx.commit()?;
    Ok(())
}
fn roster(c: &Connection, season: &str) -> Result<()> {
    let mut list = records(
        nba(
            "commonteamroster",
            &[
                ("TeamID", "1610612766"),
                ("Season", season),
                ("LeagueID", "00"),
            ],
        )?,
        "CommonTeamRoster",
    )?;
    ensure!(!list.is_empty(), "NBA returned an empty roster");
    for r in &mut list {
        r["season"] = json!(season);
        r["player_name"] = r["player"].clone();
        r["number"] = r["num"].clone();
        r["position"] = json!(match s(r, "position") {
            "G" => "Guard",
            "F" => "Forward",
            "C" => "Center",
            "G-F" => "Guard-Forward",
            "F-G" => "Forward-Guard",
            "F-C" => "Forward-Center",
            "C-F" => "Center-Forward",
            value => value,
        });
        r["updated_at"] = json!(stamp());
    }
    let tx = c.unchecked_transaction()?;
    c.execute("DELETE FROM roster WHERE season=?", [season])?;
    store(
        c,
        "roster",
        "season player_id player_name number position updated_at",
        &list,
    )?;
    tx.commit()?;
    Ok(())
}
fn schedule(c: &Connection, season: &str) -> Result<()> {
    let p = nba(
        "scheduleleaguev2",
        &[("LeagueID", "00"), ("Season", season)],
    )?;
    let mut list = vec![];
    let ts = stamp();
    for day in p["leagueSchedule"]["gameDates"]
        .as_array()
        .context("missing gameDates")?
    {
        for g in day["games"].as_array().context("missing schedule games")? {
            let (h, a) = (&g["homeTeam"], &g["awayTeam"]);
            if !s(g, "gameId").starts_with("002")
                || ![s(h, "teamTricode"), s(a, "teamTricode")].contains(&"CHA")
            {
                continue;
            }
            let tip = s(g, "gameDateTimeEst");
            let status = match g["gameStatus"].as_i64() {
                Some(3) => "Final",
                Some(2) => "Live",
                _ => "Scheduled",
            };
            list.push(json!({"season":season,"game_id":g["gameId"],"game_date":tip.get(..10).context("missing tip date")?,"game_time":tip.get(11..16),"home_team":h["teamTricode"],"away_team":a["teamTricode"],"home_score":if status=="Scheduled"{Value::Null}else{h["score"].clone()},"away_score":if status=="Scheduled"{Value::Null}else{a["score"].clone()},"arena":g["arenaName"],"status":status,"fetched_at":ts}));
        }
    }
    let tx = c.unchecked_transaction()?;
    store(c,"schedule","season game_id game_date game_time home_team away_team home_score away_score arena status fetched_at",&list)?;
    tx.commit()?;
    Ok(())
}
fn info(c: &Connection, season: &str) -> Result<()> {
    let list = records(
        nba(
            "teaminfocommon",
            &[
                ("TeamID", "1610612766"),
                ("LeagueID", "00"),
                ("Season", season),
                ("SeasonType", "Regular Season"),
            ],
        )?,
        "TeamInfoCommon",
    )?;
    if let Some(r) = list.first() {
        let season = r["season_year"].as_str().unwrap_or(season);
        let tx = c.unchecked_transaction()?;
        c.execute("DELETE FROM team_info WHERE season=?", [season])?;
        c.execute("INSERT INTO team_info(season,wins,losses,win_pct,conf_rank,div_rank,updated_at) VALUES(?,?,?,?,?,?,?)",params![season,num(r,"w"),num(r,"l"),num(r,"pct"),num(r,"conf_rank"),num(r,"div_rank"),stamp()])?;
        tx.commit()?;
    }
    Ok(())
}
pub fn refresh(c: &Connection) -> Result<()> {
    let source = std::env::var("HORNETS_DATA_SOURCE").unwrap_or_else(|_| "nba".into());
    match source.as_str() {
        "espn" => return crate::espn::refresh(c),
        "nba" => ensure!(
            !c.query_row(
                "SELECT EXISTS(SELECT 1 FROM league_games WHERE game_id LIKE 'espn-%')",
                [],
                |r| r.get::<_, bool>(0)
            )?,
            "ESPN history requires HORNETS_DATA_SOURCE=espn"
        ),
        _ => anyhow::bail!("unknown HORNETS_DATA_SOURCE: {source}"),
    }
    let y = year();
    let current = season_id(y);
    let mut errors = vec![];
    let mut current_ready = true;
    for offset in 0..4 {
        let season = season_id(y - offset);
        for key in ["teams", "players", "roster", "schedule", "info"] {
            if key == "info" && offset != 0 {
                continue;
            }
            let marker = format!("feed_{key}_{season}");
            if offset > 0
                && c.query_row(
                    "SELECT EXISTS(SELECT 1 FROM model_params WHERE name=?)",
                    [&marker],
                    |r| r.get::<_, bool>(0),
                )?
            {
                continue;
            }
            let res = match key {
                "teams" | "players" => league(&season, if key == "teams" { "T" } else { "P" })
                    .and_then(|r| {
                        ensure!(offset == 0 || !r.is_empty(), "missing historical game logs");
                        save_logs(c, &season, key == "players", r)
                    }),
                "roster" => roster(c, &season),
                "schedule" => schedule(c, &season),
                _ => info(c, &season),
            };
            match res {
                Ok(()) => {
                    c.execute("INSERT OR REPLACE INTO model_params(name,value,updated_at) VALUES(?,'true',?)",params![marker,stamp()])?;
                }
                Err(e) => {
                    eprintln!("{key} {season}: {e:#}");
                    if offset == 0 && key != "info" {
                        current_ready = false;
                    }
                    errors.push(format!("{key} {season}"));
                }
            }
        }
        // Avoid repeating an unreachable provider for three more seasons.
        if offset == 0 && errors.len() == 5 {
            break;
        }
    }
    if current_ready {
        crate::model::run(c, &current)?;
    }
    ensure!(
        errors.is_empty(),
        "incomplete refresh ({})",
        errors.join(", ")
    );
    Ok(())
}
pub fn live() -> Result<Value> {
    let espn = (|| -> Result<Value> {
        let p = download(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
            &[],
            "6",
        )?;
        for ev in p["events"].as_array().context("missing ESPN events")? {
            let g = &ev["competitions"][0];
            let teams = g["competitors"].as_array().context("missing competitors")?;
            let side = |k: &str| -> Result<Value> {
                let t = teams
                    .iter()
                    .find(|t| s(t, "homeAway") == k)
                    .context("missing competitor")?;
                Ok(
                    json!({"abbr":t["team"]["abbreviation"],"score":s(t,"score").parse::<i64>().ok()}),
                )
            };
            let (h, a) = (side("home")?, side("away")?);
            if [s(&h, "abbr"), s(&a, "abbr")].contains(&"CHA") {
                let st = &g["status"]["type"];
                return Ok(
                    json!({"state":st["state"],"label":st["shortDetail"],"start":ev["date"],"home":h,"away":a,"cha_home":s(&h,"abbr")=="CHA","source":"espn"}),
                );
            }
        }
        Ok(json!({"state":"none"}))
    })();
    if espn.is_ok() {
        return espn;
    }
    let p = nba(
        "scoreboardv3",
        &[
            ("LeagueID", "00"),
            ("GameDate", &now().format("%F").to_string()),
        ],
    )?;
    for g in p["scoreboard"]["games"]
        .as_array()
        .ok_or_else(|| anyhow!("missing NBA scoreboard"))?
    {
        let h = &g["homeTeam"];
        let a = &g["awayTeam"];
        if [s(h, "teamTricode"), s(a, "teamTricode")].contains(&"CHA") {
            return Ok(
                json!({"state":match g["gameStatus"].as_i64(){Some(3)=>"post",Some(2)=>"in",_=>"pre"},"label":g["gameStatusText"],"home":{"abbr":h["teamTricode"],"score":h["score"]},"away":{"abbr":a["teamTricode"],"score":a["score"]},"cha_home":s(h,"teamTricode")=="CHA","start":g["gameTimeUTC"],"source":"nba"}),
            );
        }
    }
    Ok(json!({"state":"none"}))
}
