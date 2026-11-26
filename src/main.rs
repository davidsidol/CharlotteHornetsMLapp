mod api;
mod db;
mod feed;
mod model;
use anyhow::{Context, Result};
use axum::{
    extract::{Path, Query, State},
    http::{header, StatusCode},
    response::{
        sse::{Event, KeepAlive},
        Html, IntoResponse, Response, Sse,
    },
    routing::{get, post},
    Json, Router,
};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    convert::Infallible,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::{Duration, Instant},
};
use tokio::sync::{broadcast, Mutex};

#[derive(Clone)]
struct App {
    path: String,
    events: broadcast::Sender<(&'static str, Value)>,
    busy: Arc<AtomicBool>,
    live: Arc<Mutex<Option<(Instant, Value)>>>,
}
impl App {
    fn send(&self, name: &'static str, data: Value) {
        let _ = self.events.send((name, data));
    }
    fn refresh(&self) -> bool {
        if self.busy.swap(true, Ordering::AcqRel) {
            return false;
        }
        let app = self.clone();
        self.send(
            "status",
            json!({"message":"Fetching NBA data…","refreshing":true}),
        );
        tokio::spawn(async move {
            let path = app.path.clone();
            let result =
                tokio::task::spawn_blocking(move || feed::refresh(&db::connect(&path)?)).await;
            let msg = match result {
                Ok(Ok(())) => "Data updated!".into(),
                Ok(Err(e)) => format!("Refresh error: {e:#}"),
                Err(e) => format!("Refresh error: {e}"),
            };
            eprintln!("{msg}");
            app.busy.store(false, Ordering::Release);
            app.send("status", json!({"message":msg,"refreshing":false}));
            app.send("refresh", json!({"ts":db::stamp()}));
        });
        true
    }
}
async fn api_get(
    State(app): State<App>,
    Path(path): Path<String>,
    Query(args): Query<HashMap<String, String>>,
) -> Response {
    let season = args
        .get("season")
        .map(|s| s.trim())
        .filter(|s| {
            let b = s.as_bytes();
            b.len() == 7 && b[4] == b'-' && b[..4].iter().chain(&b[5..]).all(u8::is_ascii_digit)
        })
        .map(str::to_owned)
        .unwrap_or_else(|| db::season_id(db::year()));
    match tokio::task::spawn_blocking(move || api::get(&db::connect(&app.path)?, &path, &season))
        .await
    {
        Ok(Ok(Some(v))) => Json(v).into_response(),
        Ok(Ok(None)) => (StatusCode::NOT_FOUND, Json(json!({"error":"not found"}))).into_response(),
        e => {
            eprintln!("API error: {e:?}");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error":"database query failed"})),
            )
                .into_response()
        }
    }
}
async fn stream(State(app): State<App>) -> Response {
    let mut rx = app.events.subscribe();
    let stream = async_stream::stream! {
    yield Ok::<_,Infallible>(Event::default().event("connected").data(r#"{"message":"Live connection established"}"#));
    loop{match rx.recv().await{Ok((name,data))=>yield Ok(Event::default().event(name).data(data.to_string())),Err(broadcast::error::RecvError::Lagged(_))=>yield Ok(Event::default().event("refresh").data("{}")),Err(_)=>break}}
    };
    let mut res = Sse::new(stream)
        .keep_alive(KeepAlive::new().interval(Duration::from_secs(25)))
        .into_response();
    res.headers_mut()
        .insert("x-accel-buffering", "no".parse().unwrap());
    res
}
async fn live(State(app): State<App>) -> Json<Value> {
    let mut cache = app.live.lock().await;
    if let Some((ts, v)) = cache.as_ref() {
        if ts.elapsed() < Duration::from_secs(15) {
            return Json(v.clone());
        }
    }
    let v = tokio::task::spawn_blocking(feed::live)
        .await
        .ok()
        .and_then(Result::ok)
        .unwrap_or(json!({"state":"error"}));
    *cache = Some((Instant::now(), v.clone()));
    Json(v)
}
#[tokio::main]
async fn main() -> Result<()> {
    let path = std::env::var("DB_PATH").unwrap_or_else(|_| "hornets.db".into());
    let c = db::connect(&path)?;
    db::initialize(&c)
        .context("initializing SQLite (use a current-schema database or a fresh file)")?;
    match std::env::args().nth(1).as_deref() {
        Some("predict") => return model::run(&c, &db::season_id(db::year())),
        Some("refresh") => return feed::refresh(&c),
        Some("init") => return Ok(()),
        None => {}
        Some(s) => {
            anyhow::bail!("unknown command {s}; use init, refresh, predict or no argument to serve")
        }
    }
    let stale=c.query_row("SELECT (coalesce(julianday('now')-julianday(max(fetched_at)),1)>1.0/24) OR NOT EXISTS(SELECT 1 FROM player_stats WHERE season=?1) FROM team_games WHERE season=?1",[db::season_id(db::year())],|r|r.get::<_,bool>(0))?;
    drop(c);
    let app = App {
        path,
        events: broadcast::channel(32).0,
        busy: Arc::new(AtomicBool::new(false)),
        live: Default::default(),
    };
    let addr = format!(
        "{}:{}",
        std::env::var("HOST").unwrap_or_else(|_| "0.0.0.0".into()),
        std::env::var("PORT").unwrap_or_else(|_| "8080".into())
    );
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    eprintln!("Hornets dashboard listening on {addr}");
    if stale {
        app.refresh();
    }
    let scheduled = app.clone();
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(Duration::from_secs(4 * 3600)).await;
            scheduled.refresh();
        }
    });
    let router=Router::new().route("/",get(||async{Html(include_str!("../templates/index.html"))})).route("/static/HornetsLogo_bg.png",get(||async{([(header::CONTENT_TYPE,"image/png")],include_bytes!("../static/HornetsLogo_bg.png").as_slice())})).route("/api/stream",get(stream)).route("/api/live",get(live)).route("/api/refresh",post(|State(app):State<App>|async move{Json(json!({"status":if app.refresh(){"refresh started"}else{"refresh already running"}}))})).route("/api/{*path}",get(api_get)).with_state(app);
    axum::serve(listener, router).await?;
    Ok(())
}
