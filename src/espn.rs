//! ESPN regular-season schedule and box scores. Event IDs carry a provider prefix.
use crate::db::*;
use crate::feed::{download, store};
use anyhow::{bail, ensure, Context, Result};
use chrono::{DateTime, NaiveDateTime};
use rusqlite::{params, Connection};
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, HashSet};
use std::thread;

const BASE: &str = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba";
fn fetch(path: &str, args: &[(&str, &str)]) -> Result<Value> {
    download(&format!("{BASE}/{path}"), args, "22")
}
fn list<'a>(v: &'a Value, k: &str) -> Result<&'a Vec<Value>> {
    v[k].as_array().with_context(|| format!("missing ESPN {k}"))
}
fn string<'a>(v: &'a Value, k: &str) -> Result<&'a str> {
    v[k].as_str()
        .filter(|s| !s.is_empty())
        .with_context(|| format!("missing ESPN {k}"))
}
fn player_id(p: &Value) -> Result<i64> {
    let id = string(p, "id")?.parse::<i64>()?;
    // Preserve the NBA IDs of the four cards pinned by the existing UI.
    Ok(match id {
        4433287 => 1641706,
        5061575 => 1642851,
        4395651 => 1629632,
        4396971 => 1629675,
        _ => 10_000_000 + id,
    })
}
#[derive(Clone)]
struct Game {
    id: String,
    key: String,
    day: String,
    time: String,
    home: String,
    away: String,
    scores: [Option<i64>; 2],
    status: &'static str,
    arena: String,
}
impl Game {
    fn hornets(&self) -> bool {
        self.home == "CHA" || self.away == "CHA"
    }
    fn matchup(&self, home: bool) -> String {
        if home {
            format!("{} vs. {}", self.home, self.away)
        } else {
            format!("{} @ {}", self.away, self.home)
        }
    }
}
fn parse_event(ev: &Value) -> Result<Option<Game>> {
    let comp = &ev["competitions"][0];
    if ev["seasonType"]["type"] != 2
        || s(&comp["type"], "slug") == "commissioners-cup"
        || comp["notes"].as_array().into_iter().flatten().any(|n| {
            let h = s(n, "headline").to_lowercase();
            h.contains("championship") && (h.contains("cup") || h.contains("in-season"))
        })
    {
        return Ok(None);
    }
    let status_name = s(&comp["status"]["type"], "name");
    if matches!(
        status_name,
        "STATUS_POSTPONED" | "STATUS_CANCELED" | "STATUS_CANCELLED"
    ) {
        return Ok(None);
    }
    let sides = list(comp, "competitors")?;
    let side = |key: &str| {
        sides
            .iter()
            .find(|v| s(v, "homeAway") == key)
            .context("missing home/away team")
    };
    let (h, a) = (side("home")?, side("away")?);
    let raw_date = string(ev, "date")?;
    let date = DateTime::parse_from_rfc3339(raw_date)
        .map(|d| d.to_utc())
        .or_else(|_| {
            NaiveDateTime::parse_from_str(raw_date, "%Y-%m-%dT%H:%MZ").map(|d| d.and_utc())
        })?
        .with_timezone(&chrono_tz::America::New_York);
    let status = match s(&comp["status"]["type"], "state") {
        "post" if comp["status"]["type"]["completed"] == true => "Final",
        "in" => "Live",
        "pre" => "Scheduled",
        _ => return Ok(None),
    };
    let id = string(ev, "id")?.to_owned();
    let scores = [h, a].map(|v| v["score"]["value"].as_f64().map(|x| x as i64));
    if status == "Final" {
        ensure!(
            scores.iter().all(Option::is_some),
            "missing final score for {id}"
        );
    }
    Ok(Some(Game {
        id: id.clone(),
        key: format!("espn-{id}"),
        day: date.format("%F").to_string(),
        time: date.format("%H:%M").to_string(),
        home: string(&h["team"], "abbreviation")?.into(),
        away: string(&a["team"], "abbreviation")?.into(),
        scores,
        status,
        arena: s(&comp["venue"], "fullName").into(),
    }))
}
fn calendar(team: &str, end: i32) -> Result<Vec<Game>> {
    let payload = fetch(
        &format!("teams/{team}/schedule"),
        &[("season", &end.to_string()), ("seasontype", "2")],
    )?;
    let mut games = vec![];
    for ev in list(&payload, "events")? {
        if let Some(g) = parse_event(ev)? {
            games.push(g);
        }
    }
    ensure!(!games.is_empty(), "empty schedule for team {team} ({end})");
    Ok(games)
}
fn schedules(teams: &[String], end: i32) -> Result<Vec<Game>> {
    let mut games = BTreeMap::new();
    for batch in teams.chunks(6) {
        thread::scope(|scope| -> Result<()> {
            let tasks: Vec<_> = batch
                .iter()
                .map(|team| scope.spawn(move || calendar(team, end)))
                .collect();
            for task in tasks {
                for g in task.join().expect("schedule worker")? {
                    games.insert(g.key.clone(), g);
                }
            }
            Ok(())
        })?;
    }
    ensure!(
        games.values().any(Game::hornets),
        "no Charlotte schedule returned"
    );
    Ok(games.into_values().collect())
}
fn split_shots(v: &str) -> Result<(i64, i64)> {
    let (a, b) = v.split_once('-').context("missing made-attempted pair")?;
    Ok((a.parse()?, b.parse()?))
}
fn shooting(row: &mut Value, stats: &Value) -> Result<()> {
    for (key, m, a, pct) in [
        ("fieldGoalsMade-fieldGoalsAttempted", "fgm", "fga", "fg_pct"),
        (
            "threePointFieldGoalsMade-threePointFieldGoalsAttempted",
            "fg3m",
            "fg3a",
            "fg3_pct",
        ),
        ("freeThrowsMade-freeThrowsAttempted", "ftm", "fta", "ft_pct"),
    ] {
        let (made, attempts) = split_shots(string(stats, key)?)?;
        row[m] = json!(made);
        row[a] = json!(attempts);
        row[pct] = json!(if attempts == 0 {
            0.
        } else {
            made as f64 / attempts as f64
        });
    }
    Ok(())
}
fn integer(stats: &Value, key: &str) -> Result<i64> {
    Ok(string(stats, key)?.parse()?)
}
fn boxscore(c: &Connection, season: &str, g: &Game, payload: &Value) -> Result<()> {
    let bx = &payload["boxscore"];
    let mut logs = vec![];
    let mut players = vec![];
    let mut team_games = vec![];
    let stamp = stamp();
    for side in list(bx, "players")? {
        let abbr = string(&side["team"], "abbreviation")?;
        ensure!(abbr == g.home || abbr == g.away, "unexpected team {abbr}");
        let home = abbr == g.home;
        let opp = if home { &g.away } else { &g.home };
        let [h, a] = g.scores;
        let won = (h > a) == home;
        let mut played = 0;
        for grp in list(side, "statistics")? {
            let keys = list(grp, "keys")?;
            for line in list(grp, "athletes")? {
                if line["didNotPlay"] == true {
                    continue;
                }
                let vals = list(line, "stats")?;
                if vals.is_empty() {
                    continue;
                }
                ensure!(keys.len() == vals.len(), "unexpected player stat layout");
                let stats = Value::Object(
                    keys.iter()
                        .zip(vals)
                        .map(|(k, v)| (k.as_str().unwrap_or("").into(), v.clone()))
                        .collect::<Map<_, _>>(),
                );
                let min = string(&stats, "minutes")?;
                // ESPN sometimes includes inactive placeholders without a player ID or minutes.
                if min == "--"
                    && vals
                        .iter()
                        .skip(1)
                        .all(|v| matches!(v.as_str(), Some("0" | "0-0" | "--" | "+0" | "-0")))
                {
                    continue;
                }
                let minutes = if let Some((m, s)) = min.split_once(':') {
                    m.parse::<f64>()? + s.parse::<f64>()? / 60.
                } else {
                    min.parse()?
                };
                let athlete = &line["athlete"];
                let mut row = json!({"season":season,"game_id":g.key,"player_id":player_id(athlete)?,"player_name":string(athlete,"displayName")?,"team_abbr":abbr,"opp_abbr":opp,"game_date_iso":g.day,"home":home,"min":minutes,"pts":integer(&stats,"points")?,"reb":integer(&stats,"rebounds")?,"ast":integer(&stats,"assists")?});
                if minutes > 0. {
                    logs.push(row.clone());
                }
                played += 1;
                if abbr == "CHA" {
                    row["min"] = json!(min);
                    row["game_date"] = json!(chrono::NaiveDate::parse_from_str(&g.day, "%F")?
                        .format("%b %d, %Y")
                        .to_string());
                    row["matchup"] = json!(g.matchup(home));
                    row["wl"] = json!(if won { "W" } else { "L" });
                    row["fetched_at"] = json!(stamp);
                    shooting(&mut row, &stats)?;
                    for (k, src) in [("stl", "steals"), ("blk", "blocks"), ("tov", "turnovers")] {
                        row[k] = json!(integer(&stats, src)?);
                    }
                    row["plus_minus"] = json!(s(&stats, "plusMinus").parse::<i64>().ok());
                    players.push(row);
                }
            }
        }
        ensure!(played > 0, "empty box score for {abbr}");
    }
    ensure!(list(bx, "players")?.len() == 2, "incomplete box score");
    if g.hornets() {
        let cha = list(bx, "teams")?
            .iter()
            .find(|t| t["team"]["abbreviation"] == "CHA")
            .context("missing Charlotte team totals")?;
        let stats = Value::Object(
            list(cha, "statistics")?
                .iter()
                .map(|v| (s(v, "name").into(), v["displayValue"].clone()))
                .collect(),
        );
        let home = g.home == "CHA";
        let us = g.scores[usize::from(!home)];
        let them = g.scores[usize::from(home)];
        let mut row = json!({"season":season,"game_id":g.key,"game_date_iso":g.day,"game_date":chrono::NaiveDate::parse_from_str(&g.day,"%F")?.format("%b %d, %Y").to_string(),"matchup":g.matchup(home),"wl":if us>them{"W"}else{"L"},"pts":us,"opp_pts":them,"fetched_at":stamp});
        shooting(&mut row, &stats)?;
        for (k, src) in [
            ("reb", "totalRebounds"),
            ("ast", "assists"),
            ("stl", "steals"),
            ("blk", "blocks"),
            ("tov", "totalTurnovers"),
        ] {
            row[k] = json!(integer(&stats, src)?);
        }
        team_games.push(row);
    }
    let tx = c.unchecked_transaction()?;
    store(c,"league_player_games","season game_id player_id player_name team_abbr opp_abbr game_date_iso home min pts reb ast",&logs)?;
    store(c,"player_stats","player_id player_name game_id game_date matchup wl pts reb ast min fgm fga fg_pct fg3m fg3a fg3_pct ftm fta ft_pct stl blk tov plus_minus fetched_at season game_date_iso",&players)?;
    store(c,"team_games","game_id game_date matchup wl pts opp_pts fg_pct fg3_pct ft_pct reb ast stl blk tov fetched_at season game_date_iso",&team_games)?;
    tx.commit()?;
    Ok(())
}
fn write_schedule(c: &Connection, season: &str, games: &[Game]) -> Result<()> {
    let mut league = vec![];
    let mut schedule = vec![];
    let ts = stamp();
    for g in games {
        if g.hornets() {
            schedule.push(json!({"season":season,"game_id":g.key,"home_team":g.home,"away_team":g.away,"game_date":g.day,"game_time":g.time,"status":g.status,"arena":g.arena,"home_score":g.scores[0],"away_score":g.scores[1],"fetched_at":ts}));
        }
        if g.status == "Final" {
            for (i, t) in [&g.home, &g.away].iter().enumerate() {
                league.push(json!({"season":season,"game_id":g.key,"team_abbr":t,"game_date_iso":g.day,"home":i==0,"pts":g.scores[i],"opp_pts":g.scores[1-i]}));
            }
        }
    }
    let tx = c.unchecked_transaction()?;
    c.execute(
        "DELETE FROM schedule WHERE season=? AND status!='Final'",
        [season],
    )?;
    store(c,"schedule","season game_id home_team away_team game_date game_time status arena home_score away_score fetched_at",&schedule)?;
    store(
        c,
        "league_games",
        "season game_id team_abbr game_date_iso home pts opp_pts",
        &league,
    )?;
    tx.commit()?;
    Ok(())
}
fn team_info(c: &Connection, season: &str, end: i32) -> Result<()> {
    let payload = download(
        "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings",
        &[("season", &end.to_string()), ("level", "3")],
        "22",
    )?;
    for conference in list(&payload, "children")? {
        for division in list(conference, "children")? {
            let entries = list(&division["standings"], "entries")?;
            let Some(cha) = entries.iter().find(|t| t["team"]["abbreviation"] == "CHA") else {
                continue;
            };
            // The feed's array order is not the standings order. Conference seeds
            // order teams with tied records, including the NBA tiebreakers.
            let stat = |team: &Value, key: &str| -> Result<f64> {
                list(team, "stats")?
                    .iter()
                    .find(|s| s["name"] == key)
                    .and_then(|s| s["value"].as_f64())
                    .with_context(|| format!("missing standings {key}"))
            };
            let seed = stat(cha, "playoffSeed")?;
            let pct = stat(cha, "winPercent")?;
            let mut rank = 1;
            ensure!(
                entries.len() == 5,
                "incomplete Southeast Division standings"
            );
            for other in entries {
                let other_pct = stat(other, "winPercent")?;
                if other_pct > pct || (other_pct == pct && stat(other, "playoffSeed")? < seed) {
                    rank += 1;
                }
            }
            let tx = c.unchecked_transaction()?;
            c.execute("DELETE FROM team_info WHERE season=?", [season])?;
            c.execute("INSERT INTO team_info(season,wins,losses,win_pct,conf_rank,div_rank,updated_at) VALUES(?,?,?,?,?,?,?)",
                params![season,stat(cha,"wins")?,stat(cha,"losses")?,pct,seed,rank,stamp()])?;
            tx.commit()?;
            return Ok(());
        }
    }
    bail!("missing Charlotte standings ({season})")
}
fn current_roster(c: &Connection, season: &str) -> Result<()> {
    let r = fetch("teams/30/roster", &[])?;
    let mut entries = vec![];
    for p in list(&r, "athletes")? {
        entries.push(json!({"season":season,"player_id":player_id(p)?,"player_name":string(p,"fullName")?,"number":p["jersey"],"position":p["position"]["displayName"],"updated_at":stamp()}));
    }
    ensure!(!entries.is_empty(), "empty Charlotte roster");
    let tx = c.unchecked_transaction()?;
    c.execute("DELETE FROM roster WHERE season=?", [season])?;
    store(
        c,
        "roster",
        "season player_id player_name number position updated_at",
        &entries,
    )?;
    tx.commit()?;
    Ok(())
}
fn load_boxes(c: &Connection, season: &str, games: &[&Game]) -> Result<usize> {
    let mut failed = 0;
    for batch in games.chunks(6) {
        thread::scope(|scope| {
            let tasks: Vec<_> = batch
                .iter()
                .map(|&g| {
                    (
                        g,
                        scope.spawn(move || fetch("summary", &[("event", &g.id)])),
                    )
                })
                .collect();
            for (g, task) in tasks {
                if let Err(e) = task
                    .join()
                    .expect("box worker")
                    .and_then(|v| boxscore(c, season, g, &v))
                {
                    eprintln!("ESPN box {} ({season}): {e:#}", g.id);
                    failed += 1;
                }
            }
        });
    }
    Ok(failed)
}
pub fn refresh(c: &Connection) -> Result<()> {
    let mixed=c.query_row("SELECT EXISTS(SELECT 1 FROM league_games WHERE game_id NOT LIKE 'espn-%' UNION ALL SELECT 1 FROM team_games WHERE game_id NOT LIKE 'espn-%')",[],|r|r.get::<_,bool>(0))?;
    ensure!(
        !mixed,
        "choose a separate SQLite database for ESPN event IDs"
    );
    let data = fetch("teams", &[("limit", "50")])?;
    let teams = list(&data["sports"][0]["leagues"][0], "teams")?
        .iter()
        .map(|v| string(&v["team"], "id").map(str::to_owned))
        .collect::<Result<Vec<_>>>()?;
    ensure!(teams.len() == 30, "expected 30 NBA teams");
    let y = year();
    let mut league_boxes = vec![];
    let mut failures = 0;
    let mut metadata_errors = Vec::new();
    for offset in 0..4 {
        let season = season_id(y - offset);
        let key = format!("espn_complete_{season}");
        if offset > 0
            && c.query_row(
                "SELECT EXISTS(SELECT 1 FROM model_params WHERE name=?)",
                [&key],
                |r| r.get::<_, bool>(0),
            )?
        {
            continue;
        }
        let end = y - offset + 1;
        let games = schedules(&teams, end)?;
        write_schedule(c, &season, &games)?;
        team_info(c, &season, end)?;
        if offset == 0 {
            current_roster(c, &season)?;
        }
        let done: HashSet<_> = rows(
            c,
            "SELECT DISTINCT game_id FROM league_player_games WHERE season=?",
            [&season],
        )?
        .into_iter()
        .map(|v| s(&v, "game_id").to_owned())
        .collect();
        let pending: Vec<_> = games
            .into_iter()
            .filter(|g| g.status == "Final" && !done.contains(&g.key))
            .collect();
        let (cha, others): (Vec<_>, Vec<_>) = pending.into_iter().partition(Game::hornets);
        let failed = load_boxes(c, &season, &cha.iter().collect::<Vec<_>>())?;
        failures += failed;
        eprintln!(
            "ESPN {season}: processed {} new Charlotte box scores, {failed} failed",
            cha.len()
        );
        league_boxes.push((season, offset, key, failed, others));
    }
    for (season, offset, key, cha_failed, games) in league_boxes {
        let failed = load_boxes(c, &season, &games.iter().collect::<Vec<_>>())?;
        failures += failed;
        if offset > 0 && failed + cha_failed == 0 {
            c.execute(
                "INSERT OR REPLACE INTO model_params(name,value,updated_at) VALUES(?,'true',?)",
                params![key, stamp()],
            )?;
        }
        eprintln!(
            "ESPN {season}: processed {} other league box scores, {failed} failed",
            games.len()
        );
    }
    for offset in 1..4 {
        let end = y - offset + 1;
        let season = season_id(end - 1);
        if let Err(e) = crate::roster_history::fill(c, &season, end) {
            metadata_errors.push(format!("{season} roster: {e:#}"));
        }
        let ranked: bool = c.query_row(
            "SELECT EXISTS(SELECT 1 FROM team_info WHERE season=? AND div_rank BETWEEN 1 AND 5)",
            [&season],
            |r| r.get(0),
        )?;
        if !ranked {
            if let Err(e) = team_info(c, &season, end) {
                metadata_errors.push(format!("{season} standings: {e:#}"));
            }
        }
    }
    crate::model::run(c, &season_id(y))?;
    ensure!(metadata_errors.is_empty(), "{}", metadata_errors.join(", "));
    ensure!(failures == 0, "{failures} ESPN box scores need retry");
    Ok(())
}
