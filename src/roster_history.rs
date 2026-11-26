//! Charlotte's season roster includes players who departed before the final game.
use crate::{
    db::{rows, s, stamp},
    feed::download_bytes,
};
use anyhow::{ensure, Context, Result};
use rusqlite::{params, Connection};
use scraper::{Html, Selector};
use std::collections::HashMap;
use unicode_normalization::UnicodeNormalization;

fn name_key(name: &str) -> String {
    let key: String = name
        .nfkd()
        .filter(char::is_ascii_alphanumeric)
        .map(|c| c.to_ascii_lowercase())
        .collect();
    match key.as_str() {
        "natemensah" => "nathanmensah".into(),
        "xaviertillman" => "xaviertillmansr".into(),
        _ => key,
    }
}

pub fn fill(c: &Connection, season: &str, end: i32) -> Result<()> {
    let missing = rows(
        c,
        "SELECT DISTINCT p.player_id,p.player_name FROM player_stats p
        LEFT JOIN roster r ON r.season=p.season AND r.player_id=p.player_id
        WHERE p.season=? AND (coalesce(r.number,'')='' OR coalesce(r.position,'')='')",
        [season],
    )?;
    if missing.is_empty() {
        return Ok(());
    }
    let url = format!("https://www.basketball-reference.com/teams/CHO/{end}.html");
    let page = String::from_utf8(download_bytes(&url, &[], "22")?)?;
    let html = Html::parse_document(&page);
    let selector = |v: &str| Selector::parse(v).expect("static roster selector");
    let names = selector("[data-stat=player]");
    let numbers = selector("[data-stat=number]");
    let positions = selector("[data-stat=pos]");
    let mut archive = HashMap::new();
    for row in html.select(&selector("#roster tbody tr")) {
        let cell = |sel: &Selector| {
            row.select(sel)
                .next()
                .map(|c| c.text().collect::<String>().trim().to_string())
                .unwrap_or_default()
        };
        let name = cell(&names);
        let number = cell(&numbers);
        let pos = cell(&positions);
        let position = match pos.as_str() {
            "C" => "Center",
            "PG" => "Point Guard",
            "SG" => "Shooting Guard",
            "SF" => "Small Forward",
            "PF" => "Power Forward",
            other => other,
        };
        if !name.is_empty() && !number.is_empty() && !position.is_empty() {
            ensure!(
                archive
                    .insert(name_key(&name), (number, position.to_owned()))
                    .is_none(),
                "ambiguous roster entry {name} ({season})"
            );
        }
    }
    let tx = c.unchecked_transaction()?;
    for row in missing {
        let name = s(&row, "player_name");
        let (number, position) = archive
            .get(&name_key(name))
            .with_context(|| format!("missing archived roster entry for {name} ({season})"))?;
        c.execute(
            "INSERT INTO roster(season,player_id,player_name,number,position,updated_at)
            VALUES(?,?,?,?,?,?) ON CONFLICT(season,player_id) DO UPDATE SET
            number=excluded.number, position=excluded.position, updated_at=excluded.updated_at",
            params![
                season,
                row["player_id"].as_i64(),
                name,
                number,
                position,
                stamp()
            ],
        )?;
    }
    tx.commit()?;
    Ok(())
}
