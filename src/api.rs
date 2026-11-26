use crate::db::*;
use anyhow::Result;
use rusqlite::{params, Connection};
use serde_json::{json, Map, Value};

fn averages(games: &[Value]) -> Value {
    if games.is_empty() {
        return json!({});
    }
    let mut avg = json!({"games":games.len()});
    for key in [
        "pts", "reb", "ast", "stl", "blk", "tov", "fg_pct", "fg3_pct", "ft_pct",
    ] {
        let values: Vec<_> = games
            .iter()
            .filter(|g| !g[key].is_null())
            .map(|g| num(g, key))
            .collect();
        avg[key] = json!(round(
            values.iter().sum::<f64>() * if key.ends_with("pct") { 100. } else { 1. }
                / values.len().max(1) as f64,
            1
        ));
    }
    let mins = games
        .iter()
        .map(|g| match s(g, "min").split_once(':') {
            Some((m, sec)) => {
                m.parse::<f64>().unwrap_or(0.) + sec.parse::<f64>().unwrap_or(0.) / 60.
            }
            None => num(g, "min"),
        })
        .sum::<f64>();
    avg["mpg"] = json!(round(mins / games.len() as f64, 1));
    avg
}
const PINNED: [i64; 4] = [1641706, 1642851, 1629632, 1629675];
fn players(c: &Connection, season: &str) -> Result<Value> {
    let roster=rows(c,"WITH played AS (SELECT player_id,max(player_name) AS player_name FROM player_stats WHERE season=?1 GROUP BY player_id), ids AS (SELECT player_id FROM played UNION SELECT player_id FROM roster WHERE season=?1 AND player_id IN (?2,?3,?4,?5)) SELECT ids.player_id,coalesce(p.player_name,r.player_name) AS name,coalesce(r.number,'') AS number,coalesce(r.position,'') AS position FROM ids LEFT JOIN played p USING(player_id) LEFT JOIN roster r ON r.season=?1 AND r.player_id=ids.player_id",params![season,PINNED[0],PINNED[1],PINNED[2],PINNED[3]])?;
    let mut cards = vec![];
    for mut p in roster {
        let id = p["player_id"].as_i64().unwrap();
        let g=rows(c,"SELECT * FROM player_stats WHERE season=? AND player_id=? ORDER BY game_date_iso DESC,game_id DESC LIMIT 20",params![season,id])?;
        p["averages"] = averages(&g[..15.min(g.len())]);
        p["recent_games"] = json!(g);
        cards.push(p);
    }
    let rank = |p: &Value| {
        PINNED
            .iter()
            .position(|i| Some(*i) == p["player_id"].as_i64())
            .unwrap_or(4)
    };
    cards.sort_by(|a, b| {
        rank(a)
            .cmp(&rank(b))
            .then_with(|| num(&b["averages"], "mpg").total_cmp(&num(&a["averages"], "mpg")))
    });
    let mut out = Map::new();
    for (rank, mut p) in cards.into_iter().take(12).enumerate() {
        let name = p.as_object_mut().unwrap().remove("name").unwrap();
        p["rank"] = json!(rank);
        p["featured"] = json!(rank < 4);
        out.insert(name.as_str().unwrap().into(), p);
    }
    Ok(Value::Object(out))
}
fn correct(r: &Value) -> bool {
    num(r, "correct") == 1.
}
fn record(rows: &[Value], season: &str) -> Value {
    let tally = |wins: bool| {
        let v: Vec<_> = rows
            .iter()
            .filter(|r| (s(r, "winner") == "CHA") == wins)
            .collect();
        json!({"total":v.len(),"correct":v.iter().filter(|r|correct(r)).count()})
    };
    let ok = rows.iter().filter(|r| correct(r)).count();
    let n = rows.len();
    json!({"season":season,"total":n,"correct":ok,"wrong":n-ok,"pct":if n>0{Some(round(100.*ok as f64/n as f64,1))}else{None},"win_calls":tally(true),"loss_calls":tally(false),"backtest":rows.iter().filter(|r|s(r,"source")=="backtest").count(),"recent":rows.iter().take(5).collect::<Vec<_>>()})
}
fn form(c: &Connection, rows: &[Value], season: &str) -> Result<Value> {
    let count = |n| rows.iter().take(n).filter(|r| correct(r)).count();
    let n = rows.len();
    let k = rows.first().map(correct);
    let streak = rows.iter().take_while(|r| Some(correct(r)) == k).count();
    let frac = count(5) as f64 / n.clamp(1, 5) as f64;
    let status = if n < 3 {
        "none"
    } else if (k == Some(true) && streak >= 3) || (n >= 4 && frac >= 0.8) {
        "hot"
    } else if (k == Some(false) && streak >= 2) || (n >= 4 && frac <= 0.4) {
        "cold"
    } else {
        "steady"
    };
    let mut best = Value::Null;
    if let Some(g) = future(c, season, "game_predictions", "result IS NULL", 1)?.first() {
        let p = num(g, "win_prob").clamp(0.000001, 0.999999);
        let conf = p.max(1. - p);
        let cha = p >= 0.5;
        let sd = rows_param(c, &format!("team_params_{season}"))?["sd"]
            .as_f64()
            .unwrap_or(12.);
        let (mut lo, mut hi) = (0., 8.);
        for _ in 0..48 {
            let z = (lo + hi) / 2.;
            if cdf(z) < conf {
                lo = z
            } else {
                hi = z
            }
        }
        let fav = sd * (lo + hi) / 2.;
        let similar: Vec<_> = rows
            .iter()
            .filter(|r| {
                let q = num(r, "win_prob");
                (q.max(1. - q) - conf).abs() <= 0.08
            })
            .collect();
        best = json!({"game_date":g["game_date"],"game_time":g["game_time"],"home":num(g,"home")!=0.,"opponent":g["opponent"],"pick":if cha{"CHA"}else{s(g,"opponent")},"pick_is_cha":cha,"confidence":round(100.*conf,0),"tier":if conf>=0.75{"Strong"}else if conf>=0.65{"Solid"}else if conf>=0.58{"Lean"}else{"Pass"},"fav_margin":round(fav,1),"win_by_5":round(100.*cdf((fav-5.)/sd),0),"pred_pts":g["pred_pts"],"pred_opp_pts":g["pred_opp_pts"],"similar":{"correct":similar.iter().filter(|r|correct(r)).count(),"total":similar.len()}});
    }
    Ok(
        json!({"season":season,"graded":n,"status":status,"streak":streak,"streak_kind":k.map(|v|if v{"W"}else{"L"}),"last5":{"correct":count(5),"total":n.min(5)},"last10":{"correct":count(10),"total":n.min(10)},"form":rows.iter().take(10).collect::<Vec<_>>().into_iter().rev().map(|r|json!({"ok":correct(r),"opp":r["opponent"],"home":num(r,"home")!=0.,"date":r["game_date"]})).collect::<Vec<_>>(),"best":best}),
    )
}
pub fn cdf(z: f64) -> f64 {
    0.5 * libm::erfc(-z / std::f64::consts::SQRT_2)
}
pub fn rows_param(c: &Connection, key: &str) -> Result<Value> {
    let r = rows(c, "SELECT value FROM model_params WHERE name=?", [key])?;
    Ok(r.first()
        .and_then(|r| serde_json::from_str(s(r, "value")).ok())
        .unwrap_or(Value::Null))
}
pub fn get(c: &Connection, path: &str, season: &str) -> Result<Option<Value>> {
    let cs = season_id(year());
    let value=match path{
 "status"=>{let mut r=rows(c,"SELECT (SELECT count(*) FROM player_stats) player_stat_rows,(SELECT max(fetched_at) FROM player_stats) last_fetch,(SELECT count(*) FROM team_games) team_game_rows,(SELECT count(*) FROM predictions) prediction_rows,(SELECT count(*) FROM team_games WHERE season=?) current_season_games",[&cs])?.remove(0);r["db_ready"]=json!(num(&r,"player_stat_rows")>0.);r["current_season"]=json!(cs);r["next_refresh_hours"]=json!(4);r["server_time"]=json!(stamp());r},
 "seasons"=>{let list=rows(c,"SELECT season FROM team_games UNION SELECT season FROM schedule UNION SELECT season FROM league_games WHERE team_abbr='CHA' UNION SELECT ? ORDER BY season DESC",[&cs])?;
 json!({"current":cs,"seasons":list.iter().filter_map(|r|r["season"].as_str()).map(|v|json!({"id":v,"label":v.replace("-","–"),"current":v==cs})).collect::<Vec<_>>()})},
 "team"=>json!({"season":season,"is_current":season==cs,"info":rows(c,"SELECT * FROM team_info WHERE season=? ORDER BY updated_at DESC LIMIT 1",[season])?.pop().unwrap_or(json!({})),"recent_games":rows(c,"SELECT * FROM team_games WHERE season=? ORDER BY game_date_iso DESC,game_id DESC",[season])?,"upcoming":rows(c,"SELECT * FROM schedule WHERE season=? AND status!='Final' ORDER BY game_date,game_id",[season])?}),
 "players"=>players(c,season)?,
 "predictions"=>grouped(rows(c,"SELECT * FROM predictions WHERE season=? AND game_date>=? ORDER BY player_name,game_date",[season,&now().format("%F").to_string()])?),
 "game_predictions"=>json!(future(c,season,"game_predictions","result IS NULL",999)?),
 "game_record"|"model_form"=>{let r=rows(c,"SELECT * FROM game_predictions WHERE season=? AND result IS NOT NULL ORDER BY game_date DESC,game_id DESC",[season])?;if path=="game_record"{record(&r,season)}else{form(c,&r,season)?}},
 _ if path.starts_with("player/")=>{let Ok(id)=path[7..].parse::<u64>()else{return Ok(None)};let games=rows(c,"SELECT * FROM player_stats WHERE season=? AND player_id=? ORDER BY game_date_iso DESC,game_id DESC",params![season,id])?;let r=rows(c,"SELECT * FROM roster WHERE season=? AND player_id=?",params![season,id])?.pop().unwrap_or(Value::Null);let name=r["player_name"].as_str().or_else(||games.first()?.get("player_name")?.as_str());if name.is_none(){return Ok(None)}json!({"player_id":id,"name":name,"season":season,"position":s(&r,"position"),"number":s(&r,"number"),"averages":averages(&games[..games.len().min(200)]),"games":games})},
 _=>return Ok(None)};
    Ok(Some(value))
}
