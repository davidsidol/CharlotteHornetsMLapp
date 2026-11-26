use crate::{api::cdf, db::*};
use anyhow::{Context, Result};
use chrono::{Datelike, NaiveDate};
use rusqlite::{params, Connection};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet, HashMap};

#[derive(Clone, Copy)]
struct Settings {
    prior: f64,
    decay: f64,
    shrink: f64,
    home: f64,
    rest: f64,
    b2b: f64,
    sd: f64,
}
const BASE: Settings = Settings {
    prior: 0.4,
    decay: 1.,
    shrink: 0.75,
    home: 1.4,
    rest: 0.,
    b2b: 0.,
    sd: 12.,
};
struct Game {
    season: String,
    id: String,
    date: String,
    day: i32,
    h: usize,
    a: usize,
    hp: f64,
    ap: f64,
}
#[derive(Clone, Default)]
struct Team {
    off: f64,
    def: f64,
    weight: f64,
    last: i32,
}
struct Ratings {
    teams: Vec<Team>,
    total: f64,
    weight: f64,
    season: String,
    p: Settings,
}
impl Ratings {
    fn new(p: Settings, n: usize) -> Self {
        Self {
            p,
            teams: vec![Team::default(); n],
            total: 0.,
            weight: 0.,
            season: String::new(),
        }
    }
    fn enter(&mut self, season: &str) {
        if self.season != season {
            for t in &mut self.teams {
                t.off *= self.p.prior;
                t.def *= self.p.prior;
                t.weight *= self.p.prior;
            }
            self.total *= self.p.prior;
            self.weight *= self.p.prior;
            self.season = season.into();
        }
    }
    fn predict(&self, h: usize, a: usize, day: i32) -> Option<(f64, f64)> {
        let (h, a) = (self.teams.get(h)?, self.teams.get(a)?);
        if h.weight * a.weight == 0. {
            return None;
        }
        let lg = self.total / self.weight;
        let rh = (day - h.last).clamp(1, 4);
        let ra = (day - a.last).clamp(1, 4);
        let adjust = self.p.home
            + (self.p.rest * f64::from(rh - ra)
                + self.p.b2b * (i32::from(ra == 1) - i32::from(rh == 1)) as f64)
                / 2.;
        Some((
            lg + self.p.shrink * (h.off / h.weight + a.def / a.weight - 2. * lg) + adjust,
            lg + self.p.shrink * (a.off / a.weight + h.def / h.weight - 2. * lg) - adjust,
        ))
    }
    fn update(&mut self, g: &Game) {
        for (idx, pf, pa) in [(g.h, g.hp, g.ap), (g.a, g.ap, g.hp)] {
            let t = &mut self.teams[idx];
            t.off = t.off * self.p.decay + pf;
            t.def = t.def * self.p.decay + pa;
            t.weight = t.weight * self.p.decay + 1.;
            t.last = g.day;
        }
        self.total += g.hp + g.ap;
        self.weight += 2.;
    }
    fn env(&self, idx: usize) -> [f64; 2] {
        self.teams
            .get(idx)
            .filter(|t| t.weight > 0. && self.total > 0.)
            .map(|t| {
                let scale = self.weight / (self.total * t.weight);
                [t.def * scale, (t.off + t.def) * scale / 2.]
            })
            .unwrap_or([1.; 2])
    }
}
fn ordinal(s: &str) -> Result<i32> {
    Ok(NaiveDate::parse_from_str(s, "%Y-%m-%d")?.num_days_from_ce())
}
fn walk<'g>(
    games: &'g [Game],
    n: usize,
    p: Settings,
    mut visit: impl FnMut(&'g Game, &Ratings, (f64, f64)),
) -> Ratings {
    let mut ratings = Ratings::new(p, n);
    for batch in games.chunk_by(|a, b| a.day == b.day) {
        ratings.enter(&batch[0].season);
        for g in batch {
            if let Some(score) = ratings.predict(g.h, g.a, g.day) {
                visit(g, &ratings, score)
            }
        }
        for g in batch {
            ratings.update(g)
        }
    }
    ratings
}
fn score(games: &[Game], n: usize, p: Settings, first: &str) -> (f64, f64) {
    let scales = [10.5, 11.25, 12., 12.75, 13.5, 15., 16.5];
    let mut losses = [0.; 7];
    let mut count = 0;
    walk(games, n, p, |g, _, (hp, ap)| {
        if g.season.as_str() >= first && g.hp != g.ap {
            for (i, sd) in scales.iter().enumerate() {
                let q = cdf((hp - ap) / sd).clamp(0.0001, 0.9999);
                losses[i] -= if g.hp > g.ap { q } else { 1. - q }.ln();
            }
            count += 1;
        }
    });
    losses
        .into_iter()
        .zip(scales)
        .map(|(l, sd)| (l / count.max(1) as f64, sd))
        .min_by(|a, b| a.0.total_cmp(&b.0))
        .unwrap()
}
fn fit(games: &[Game], n: usize, first: &str) -> (Settings, f64) {
    let mut best = (f64::INFINITY, BASE);
    let check = |p: Settings, best: &mut (f64, Settings)| {
        let (l, sd) = score(games, n, p, first);
        if l < best.0 {
            *best = (l, Settings { sd, ..p });
        }
    };
    for prior in [0.2, 0.4, 0.6] {
        for decay in [0.94, 0.96, 0.98, 1.] {
            for shrink in [0.5, 0.65, 0.8, 0.95] {
                for home in [1., 1.4, 1.8] {
                    check(
                        Settings {
                            prior,
                            decay,
                            shrink,
                            home,
                            ..BASE
                        },
                        &mut best,
                    );
                }
            }
        }
    }
    let no_rest_loss = best.0;
    let stage = best.1;
    for rest in [0., 0.1, 0.2, 0.3] {
        for b2b in [0., 0.75, 1.5, 2.25] {
            check(Settings { rest, b2b, ..stage }, &mut best)
        }
    }
    (best.1, no_rest_loss)
}
fn metrics(games: &[Game], n: usize, p: Settings, first: &str) -> Value {
    let (mut count, mut hits, mut loss) = (0, 0, 0.);
    walk(games, n, p, |g, _, (h, a)| {
        if g.season.as_str() >= first && g.hp != g.ap {
            count += 1;
            hits += i32::from((h > a) == (g.hp > g.ap));
            let prob = cdf((h - a) / p.sd).clamp(0.0001, 0.9999);
            loss -= if g.hp > g.ap { prob } else { 1. - prob }.ln();
        }
    });
    let denom = count.max(1) as f64;
    json!({"games":count,"accuracy":round(100.*hits as f64/denom,1),"log_loss":round(loss/denom,4)})
}
fn comparison(games: &[Game], n: usize, p: Settings, first: &str) -> Value {
    let mut result = metrics(games, n, p, first);
    let baseline = metrics(games, n, BASE, first);
    result["baseline_accuracy"] = baseline["accuracy"].clone();
    result["baseline_log_loss"] = baseline["log_loss"].clone();
    result
}
fn tune(active: &[Game], n: usize, target: &str) -> (Settings, Value) {
    let games = &active[..active.partition_point(|g| g.season.as_str() < target)];
    let seasons: BTreeSet<_> = games.iter().map(|g| g.season.as_str()).collect();
    let Some(&first) = seasons.iter().nth(1) else {
        return (BASE, json!({}));
    };
    if games.iter().filter(|g| g.season.as_str() >= first).count() < 300 {
        return (BASE, json!({}));
    }
    let (mut params, no_rest) = fit(games, n, first);
    let mut report = comparison(games, n, params, first);
    report["seasons"] = json!(seasons.iter().skip(1).collect::<Vec<_>>());
    report["no_rest_log_loss"] = json!(round(no_rest, 4));
    let hold = seasons.last().unwrap();
    let train = &games[..games.partition_point(|g| g.season.as_str() < *hold)];
    if train.iter().filter(|g| g.season.as_str() >= first).count() >= 300 {
        let (trial, _) = fit(train, n, first);
        let mut check = comparison(games, n, trial, hold);
        check["season"] = json!(hold);
        if num(&check, "games") > 0. {
            if num(&check, "log_loss") >= num(&check, "baseline_log_loss") {
                params = BASE;
                report["used"] =
                    json!("old fixed settings (tuning did not beat them on the held-out season)");
            }
            report["holdout"] = check;
        }
    }
    let mut check = comparison(active, n, params, target);
    if num(&check, "games") >= 20. {
        check["season"] = json!(target);
        report["out_of_sample"] = check;
    }
    (params, report)
}
fn pick((hp, ap): (f64, f64), home: bool, sd: f64) -> (i64, i64, f64) {
    let (u, v) = if home { (hp, ap) } else { (ap, hp) };
    let (mut a, mut b) = (round(u, 0) as i64, round(v, 0) as i64);
    if a == b {
        if u >= v {
            a += 1
        } else {
            b += 1
        }
    }
    (a, b, round(cdf((u - v) / sd), 3))
}
fn save_param(c: &Connection, key: &str, v: Value) -> Result<()> {
    c.execute(
        "INSERT OR REPLACE INTO model_params(name,value,updated_at) VALUES(?,?,?)",
        params![key, v.to_string(), stamp()],
    )?;
    Ok(())
}
pub fn run(c: &Connection, current: &str) -> Result<()> {
    let data=rows(c,"SELECT h.season,h.game_id,h.game_date_iso,h.team_abbr h,a.team_abbr a,h.pts hp,a.pts ap FROM league_games h JOIN league_games a ON h.game_id=a.game_id AND h.season=a.season AND a.home=0 WHERE h.home=1 AND h.season<=? ORDER BY h.game_date_iso,h.game_id",[current])?;
    let mut teams = HashMap::new();
    let mut id = |t: &str| {
        let i = teams.len();
        *teams.entry(t.to_owned()).or_insert(i)
    };
    let mut games = vec![];
    for g in data {
        games.push(Game {
            season: s(&g, "season").into(),
            id: s(&g, "game_id").into(),
            date: s(&g, "game_date_iso").into(),
            day: ordinal(s(&g, "game_date_iso"))?,
            h: id(s(&g, "h")),
            a: id(s(&g, "a")),
            hp: num(&g, "hp"),
            ap: num(&g, "ap"),
        });
    }
    let names: HashMap<_, _> = teams.iter().map(|(k, v)| (*v, k.as_str())).collect();
    let targets: BTreeSet<_> = games
        .iter()
        .map(|g| g.season.as_str())
        .skip_while(|s| Some(*s) == games.first().map(|g| g.season.as_str()))
        .chain([current])
        .collect();
    let upcoming = future(c, current, "schedule", "status='Scheduled'", 5)?;
    for target in targets {
        let mut back = vec![];
        let mut env = HashMap::new();
        let active = &games[..games.partition_point(|g| g.season.as_str() <= target)];
        let (p, metrics) = tune(active, teams.len(), target);
        let mut r = walk(active, teams.len(), p, |g, r, score| {
            if target == current {
                for team in [g.h, g.a] {
                    env.insert((g.day, team), r.env(team));
                }
            }
            if g.season == target && (names[&g.h] == "CHA" || names[&g.a] == "CHA") {
                back.push((g, score));
            }
        });
        r.enter(target);
        let tx = c.unchecked_transaction()?;
        save_param(
            c,
            &format!("team_params_{target}"),
            json!({"sd":p.sd,"decay":p.decay,"shrink":p.shrink,"hfa":p.home,"k_rest":p.rest,"k_b2b":p.b2b,"w_prior":p.prior}),
        )?;
        save_param(c, &format!("team_metrics_{target}"), metrics)?;
        for (g, score) in back {
            let home = names[&g.h] == "CHA";
            let opp = names[&if home { g.a } else { g.h }];
            let (u, v) = if home { (g.hp, g.ap) } else { (g.ap, g.hp) };
            let (a, b, prob) = pick(score, home, p.sd);
            let winner = if a > b { "CHA" } else { opp };
            let result = if u > v { "W" } else { "L" };
            c.execute("INSERT OR IGNORE INTO game_predictions(season,game_id,game_date,home,opponent,arena,pred_pts,pred_opp_pts,win_prob,winner,model_version,created_at,source) VALUES(?,?,?,?,?,'',?,?,?,?,'team_v2',?,'backtest')",params![target,g.id,g.date,home,opp,a,b,prob,winner,stamp()])?;
            c.execute("UPDATE game_predictions SET actual_pts=?,actual_opp_pts=?,result=?,correct=((winner='CHA')=?) WHERE season=? AND game_id=? AND result IS NULL",params![u,v,result,u>v,target,g.id])?;
        }
        // Also grade live picks when history was too short to produce a replay prediction.
        c.execute("UPDATE game_predictions AS p SET actual_pts=g.pts,actual_opp_pts=g.opp_pts,result=CASE WHEN g.pts>g.opp_pts THEN 'W' ELSE 'L' END,correct=((p.winner='CHA')=(g.pts>g.opp_pts)) FROM league_games g WHERE g.season=p.season AND g.game_id=p.game_id AND g.team_abbr='CHA' AND p.season=? AND p.result IS NULL",[target])?;
        if target == current {
            for g in &upcoming {
                let home = s(g, "home_team") == "CHA";
                let opp = if home {
                    s(g, "away_team")
                } else {
                    s(g, "home_team")
                };
                let day = ordinal(s(g, "game_date"))?;
                if let (Some(&h), Some(&a)) =
                    (teams.get(s(g, "home_team")), teams.get(s(g, "away_team")))
                {
                    let score = r.predict(h, a, day);
                    r.teams[if home { h } else { a }].last = day;
                    if let Some(score) = score {
                        let (a, b, prob) = pick(score, home, p.sd);
                        c.execute("INSERT INTO game_predictions(season,game_id,game_date,game_time,opponent,home,arena,pred_pts,pred_opp_pts,win_prob,winner,model_version,created_at,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,'team_v2',?,'live') ON CONFLICT(season,game_id) DO UPDATE SET game_date=excluded.game_date,game_time=excluded.game_time,opponent=excluded.opponent,home=excluded.home,arena=excluded.arena,pred_pts=excluded.pred_pts,pred_opp_pts=excluded.pred_opp_pts,win_prob=excluded.win_prob,winner=excluded.winner,created_at=excluded.created_at,model_version=excluded.model_version WHERE game_predictions.result IS NULL",params![target,s(g,"game_id"),s(g,"game_date"),s(g,"game_time"),opp,home,s(g,"arena"),a,b,prob,if a>b{"CHA"}else{opp},stamp()])?;
                    }
                }
            }
            players(c, target, &upcoming, &r, &teams, &env)?;
        }
        tx.commit()?;
    }
    Ok(())
}

#[derive(Clone)]
struct Log {
    minutes: f64,
    stats: [f64; 3],
    season: String,
    env: [f64; 2],
}
#[derive(Clone, Copy)]
struct PlayerParams {
    minutes: f64,
    rate: [f64; 3],
    pace: [f64; 3],
    def: f64,
}
const PLAYER: PlayerParams = PlayerParams {
    minutes: 0.85,
    rate: [0.85; 3],
    pace: [0.5; 3],
    def: 0.5,
};
fn weighted(hist: &[Log], decay: f64, get: impl Fn(&Log) -> f64) -> f64 {
    let (mut total, mut weight) = (0., 0.);
    for h in hist {
        total = total * decay + get(h);
        weight = weight * decay + 1.;
    }
    total / weight
}
fn base(hist: &[Log], stat: usize, p: PlayerParams) -> f64 {
    let minutes = weighted(hist, p.minutes, |h| h.minutes);
    minutes * weighted(hist, p.rate[stat], |h| h.stats[stat])
        / weighted(hist, p.rate[stat], |h| h.minutes)
}
fn factor(i: usize, env: [f64; 2], p: PlayerParams) -> f64 {
    (1. + p.pace[i] * (env[1] - 1.) + if i == 0 { p.def * (env[0] - 1.) } else { 0. })
        .clamp(0.8, 1.25)
}
fn player_fit(logs: &BTreeMap<i64, Vec<Log>>, season: &str) -> (PlayerParams, Value) {
    let mut holdout = vec![];
    let mut cases = vec![];
    for list in logs.values() {
        for i in 3..list.len() {
            let h = &list[i.saturating_sub(10)..i];
            if weighted(h, 1., |r| r.minutes) >= 12. {
                if list[i].season.as_str() < season {
                    cases.push((h, &list[i]));
                } else if list[i].season == season {
                    holdout.push((h, &list[i]));
                }
            }
        }
    }
    if cases.len() < 500 {
        return (PLAYER, json!({}));
    }
    let sub: Vec<_> = cases.iter().step_by((cases.len() / 6000).max(1)).collect();
    let mut best = (f64::INFINITY, PLAYER);
    for minutes in [0.7, 0.85, 1.] {
        let mut p = PlayerParams { minutes, ..PLAYER };
        let mut loss = 0.;
        for i in 0..3 {
            let baseline = sub
                .iter()
                .map(|(h, g)| {
                    (weighted(&h[h.len().saturating_sub(8)..], 0.85, |r| r.stats[i]) - g.stats[i])
                        .abs()
                })
                .sum::<f64>();
            let mut lowest = f64::INFINITY;
            for d in [0.7, 0.8, 0.9, 1.] {
                let mut trial = p;
                trial.rate[i] = d;
                let err = sub
                    .iter()
                    .map(|(h, g)| (base(h, i, trial) - g.stats[i]).abs())
                    .sum::<f64>();
                if err < lowest {
                    lowest = err;
                    p.rate[i] = d;
                }
            }
            loss += lowest / baseline.max(1e-9);
        }
        if loss < best.0 {
            best = (loss, p);
        }
    }
    let mut p = best.1;
    for i in 0..3 {
        let predictions: Vec<_> = sub.iter().map(|(h, g)| (base(h, i, p), g)).collect();
        let mut best = f64::INFINITY;
        let grid = [0., 0.5, 1., 1.5, 2.];
        for &def in if i == 0 { &grid[..] } else { &grid[..1] } {
            for pace in grid {
                let mut trial = p;
                if i == 0 {
                    trial.def = def
                }
                trial.pace[i] = pace;
                let err = predictions
                    .iter()
                    .map(|(v, g)| (v * factor(i, g.env, trial) - g.stats[i]).abs())
                    .sum::<f64>();
                if err < best {
                    best = err;
                    p = trial;
                }
            }
        }
    }
    let summary = |cases: &[&(&[Log], &Log)]| {
        let error = |stat, legacy: bool| {
            let total: f64 = cases
                .iter()
                .map(|(h, g)| {
                    let pred = if legacy {
                        weighted(&h[h.len().saturating_sub(8)..], 0.85, |l| l.stats[stat])
                    } else {
                        base(h, stat, p) * factor(stat, g.env, p)
                    };
                    (pred - g.stats[stat]).abs()
                })
                .sum();
            round(total / cases.len() as f64, 2)
        };
        let scores =
            |legacy| json!({"pts":error(0,legacy),"reb":error(1,legacy),"ast":error(2,legacy)});
        json!({"cases":cases.len(),"mae":scores(false),"baseline_mae":scores(true)})
    };
    let mut report = summary(&sub);
    if holdout.len() >= 100 {
        let sample = holdout
            .iter()
            .step_by((holdout.len() / 4000).max(1))
            .collect::<Vec<_>>();
        let mut evaluation = summary(&sample);
        evaluation["season"] = json!(season);
        report["out_of_sample"] = evaluation;
    }
    (p, report)
}
fn players(
    c: &Connection,
    season: &str,
    future: &[Value],
    r: &Ratings,
    teams: &HashMap<String, usize>,
    env: &HashMap<(i32, usize), [f64; 2]>,
) -> Result<()> {
    c.execute("DELETE FROM predictions WHERE season=?", [season])?;
    if future.is_empty() {
        return Ok(());
    }
    let mut logs = BTreeMap::<i64, Vec<Log>>::new();
    for g in rows(c,"SELECT player_id,season,opp_abbr,game_date_iso,min,pts,reb,ast FROM league_player_games WHERE min>0 AND season<=? ORDER BY game_date_iso,game_id,player_id",[season])?{
 let day=ordinal(s(&g,"game_date_iso"))?;let environment=teams.get(s(&g,"opp_abbr")).and_then(|idx|env.get(&(day,*idx))).copied().unwrap_or([1.;2]);
 logs.entry(g["player_id"].as_i64().context("invalid player id")?).or_default().push(Log{minutes:num(&g,"min"),season:s(&g,"season").into(),stats:["pts","reb","ast"].map(|k|num(&g,k)),env:environment});}
    let (p, metrics) = player_fit(&logs, season);
    save_param(c, "player_metrics", metrics)?;
    let named = |v: [f64; 3]| json!({"pts":v[0],"reb":v[1],"ast":v[2]});
    save_param(
        c,
        "player_params",
        json!({"decay_min":p.minutes,"decay":named(p.rate),"w_def":p.def,"w_pace":named(p.pace)}),
    )?;
    for pl in rows(
        c,
        "SELECT player_id,player_name FROM roster WHERE season=?",
        [season],
    )? {
        let pid = pl["player_id"].as_i64().context("invalid roster id")?;
        let mut h = logs.remove(&pid).unwrap_or_default();
        let legacy = h.len() < 3;
        if legacy {
            let old=rows(c,"SELECT * FROM (SELECT game_id,season,game_date_iso,pts,reb,ast FROM player_stats WHERE player_id=?1 AND season<=?2 UNION SELECT game_id,season,game_date_iso,pts,reb,ast FROM prior_games WHERE player_id=?1 AND season<?2) ORDER BY game_date_iso DESC,game_id DESC LIMIT 8",params![pid,season])?;
            h = old
                .iter()
                .rev()
                .map(|g| Log {
                    minutes: 1.,
                    stats: ["pts", "reb", "ast"].map(|k| num(g, k)),
                    season: s(g, "season").into(),
                    env: [1.; 2],
                })
                .collect();
        }
        if h.len() < 3 {
            continue;
        }
        let h = &h[h.len().saturating_sub(10)..];
        let n = h.len() as f64;
        let cur = h.iter().filter(|g| g.season == season).count() as f64;
        let stats = [0, 1, 2].map(|i| {
            if legacy {
                weighted(h, 0.85, |g| g.stats[i])
            } else {
                base(h, i, p)
            }
        });
        let mean = weighted(h, 1., |g| g.stats[0]);
        let sd = weighted(h, 1., |g| (g.stats[0] - mean).powi(2)).sqrt();
        let conf = round(
            ((1. - sd / (stats[0] + 1.)) * (cur + (n - cur) * 0.5) / 10.).clamp(0.1, 0.85),
            3,
        );
        for g in future {
            let home = s(g, "home_team") == "CHA";
            let opp = if home {
                s(g, "away_team")
            } else {
                s(g, "home_team")
            };
            let e = teams.get(opp).map(|i| r.env(*i)).unwrap_or([1.; 2]);
            let vals =
                [0, 1, 2].map(|i| round(stats[i] * if legacy { 1. } else { factor(i, e, p) }, 1));
            c.execute("INSERT INTO predictions(player_id,player_name,game_date,opponent,pred_pts,pred_reb,pred_ast,confidence,model_version,created_at,season) VALUES(?,?,?,?,?,?,?,?,?,?,?)",params![pid,s(&pl,"player_name"),s(g,"game_date"),format!("{} {opp}",if home{"vs"}else{"@"}),vals[0],vals[1],vals[2],conf,if legacy{"rolling_v2"}else{"player_v2"},stamp(),season])?;
        }
    }
    Ok(())
}
