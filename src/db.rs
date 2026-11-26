use anyhow::{bail, Result};
use chrono::{Datelike, Utc};
use rusqlite::{types::ValueRef, Connection, Params};
use serde_json::{json, Map, Value};
use std::time::Duration;

pub fn now() -> chrono::DateTime<chrono_tz::Tz> {
    Utc::now().with_timezone(&chrono_tz::America::New_York)
}
pub fn season_id(y: i32) -> String {
    format!("{y}-{:02}", (y + 1) % 100)
}
pub fn year() -> i32 {
    let d = now();
    d.year() - i32::from(d.month() < 10)
}
pub fn stamp() -> String {
    Utc::now().naive_utc().to_string().replace(' ', "T")
}
pub fn connect(path: &str) -> Result<Connection> {
    let c = Connection::open(path)?;
    c.busy_timeout(Duration::from_secs(30))?;
    Ok(c)
}
pub fn initialize(c: &Connection) -> Result<()> {
    if std::env::var("DB_BACKEND").is_ok_and(|x| x != "sqlite") {
        bail!("This Ubuntu service requires DB_BACKEND=sqlite")
    }
    c.execute_batch("PRAGMA journal_mode=WAL;")?;
    let tx = c.unchecked_transaction()?;
    tx.execute_batch(include_str!("schema.sql"))?;
    migrate(c)?;
    tx.execute_batch(include_str!("indexes.sql"))?;
    tx.commit()?;
    Ok(())
}
pub fn rows(c: &Connection, sql: &str, args: impl Params) -> Result<Vec<Value>> {
    let mut q = c.prepare(sql)?;
    let names: Vec<String> = q.column_names().iter().map(|x| x.to_string()).collect();
    let result = q
        .query_map(args, |row| {
            let mut obj = Map::new();
            for (i, key) in names.iter().enumerate() {
                let val = match row.get_ref(i)? {
                    ValueRef::Null => Value::Null,
                    ValueRef::Integer(v) => json!(v),
                    ValueRef::Real(v) => json!(v),
                    ValueRef::Text(v) => json!(String::from_utf8_lossy(v)),
                    ValueRef::Blob(_) => Value::Null,
                };
                obj.insert(key.clone(), val);
            }
            Ok(Value::Object(obj))
        })?
        .collect::<rusqlite::Result<_>>()?;
    Ok(result)
}
pub fn s<'a>(v: &'a Value, key: &str) -> &'a str {
    v[key].as_str().unwrap_or("")
}
pub fn num(v: &Value, key: &str) -> f64 {
    v[key]
        .as_f64()
        .or_else(|| s(v, key).parse().ok())
        .unwrap_or(0.)
}
pub fn round(x: f64, n: i32) -> f64 {
    let p = 10f64.powi(n);
    (x * p).round_ties_even() / p
}
pub fn grouped(list: Vec<Value>) -> Value {
    let mut m = Map::new();
    for row in list {
        m.entry(s(&row, "player_name"))
            .or_insert_with(|| json!([]))
            .as_array_mut()
            .unwrap()
            .push(row);
    }
    Value::Object(m)
}
pub const FUTURE: &str = "(game_date>?2 OR (game_date=?2 AND COALESCE(game_time,'00:00')>?3))";
pub fn future(
    c: &Connection,
    season: &str,
    table: &str,
    cond: &str,
    limit: usize,
) -> Result<Vec<Value>> {
    let d = now();
    rows(c,&format!("SELECT * FROM {table} WHERE season=?1 AND {cond} AND {FUTURE} ORDER BY game_date,game_id LIMIT {limit}"),[season,&d.format("%F").to_string(),&d.format("%H:%M").to_string()])
}

fn migrate(c: &Connection) -> Result<()> {
    for (table, columns) in [
        ("player_stats", &["season TEXT", "game_date_iso TEXT"][..]),
        ("team_games", &["season TEXT", "game_date_iso TEXT"][..]),
        ("predictions", &["season TEXT"][..]),
        ("schedule", &["season TEXT", "game_time TEXT"][..]),
        (
            "game_predictions",
            &[
                "actual_pts INTEGER",
                "actual_opp_pts INTEGER",
                "result TEXT",
                "correct INTEGER",
                "source TEXT",
            ][..],
        ),
    ] {
        let existing = rows(c, &format!("PRAGMA table_info({table})"), [])?;
        for spec in columns {
            let name = spec.split_whitespace().next().unwrap();
            if !existing.iter().any(|r| s(r, "name") == name) {
                c.execute_batch(&format!("ALTER TABLE {table} ADD COLUMN {spec}"))?;
            }
        }
    }
    for table in ["player_stats", "team_games", "schedule"] {
        c.execute_batch(&format!("UPDATE {table} SET season='20'||substr(game_id,4,2)||'-'||printf('%02d',(cast(substr(game_id,4,2) as integer)+1)%100) WHERE season IS NULL AND game_id LIKE '002%'"))?;
    }
    for table in ["player_stats", "team_games"] {
        let pending = rows(
            c,
            &format!("SELECT id,game_date FROM {table} WHERE game_date_iso IS NULL"),
            [],
        )?;
        for g in pending {
            let text = s(&g, "game_date");
            let date = ["%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"]
                .iter()
                .find_map(|fmt| chrono::NaiveDate::parse_from_str(text, fmt).ok());
            if let Some(d) = date {
                c.execute(
                    &format!("UPDATE {table} SET game_date_iso=? WHERE id=?"),
                    rusqlite::params![d.to_string(), g["id"].as_i64()],
                )?;
            }
        }
    }
    c.execute_batch("UPDATE predictions SET season='2025-26' WHERE season IS NULL; UPDATE game_predictions SET source='live' WHERE source IS NULL;")?;
    Ok(())
}
