# 🐝 Charlotte Hornets ML Dashboard

A real-time NBA analytics dashboard for the Charlotte Hornets, featuring live stats, game logs, and machine learning performance predictions.

![Python](https://img.shields.io/badge/Python-3.9%2B-blue) ![Flask](https://img.shields.io/badge/Flask-3.x-green) ![License](https://img.shields.io/badge/license-MIT-blue)

## Features

- **Live Team Stats** — Win/loss record, conference rank, recent game results
- **Player Dashboards** — Full season averages and game logs for LaMelo Ball, Brandon Miller, Kon Knueppel, and Miles Bridges
- **ML Predictions** — Exponentially weighted rolling average model forecasting PTS, REB, and AST for upcoming games with confidence scores
- **Auto-Refresh** — Data pulls from the NBA API every 4 hours automatically
- **Live Push** — Server-Sent Events (SSE) push updates to the browser in real time
- **Windows Native** — Runs on Waitress WSGI server, no nginx or gunicorn required

## Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.9+ / Flask |
| WSGI Server | Waitress (Windows-compatible) |
| Database | SQLite (MSSQL-ready via env vars) |
| Data Source | `nba_api` — official NBA stats endpoints |
| ML Model | Exponentially weighted rolling averages |
| Frontend | Vanilla JS + CSS (no framework) |

## Quick Start

### 1. Clone the repo
```bash
git clone https://github.com/davidsidol/CharlotteHornetsMLapp.git
cd CharlotteHornetsMLapp
```

### 2. Install dependencies
```bash
python install.py
```
Or manually:
```bash
pip install flask waitress requests beautifulsoup4 nba_api
```

### 3. Start the server

**Windows** — double-click `start.bat`

**Mac/Linux:**
```bash
python serve.py
```

### 4. Open the dashboard
Navigate to **http://localhost:8080** in your browser.

On first launch the app fetches fresh data from the NBA API (~30 seconds).

## Project Structure

```
CharlotteHornetsMLapp/
├── app.py               # Flask app, routes, SSE, scheduler
├── database.py          # SQLite layer (MSSQL-ready)
├── fetcher.py           # NBA API data fetcher
├── ml_predictions.py    # Rolling average prediction engine
├── serve.py             # Waitress WSGI launcher
├── install.py           # One-time dependency installer
├── start.bat            # Windows one-click launcher
├── stop.bat             # Windows one-click stopper
├── requirements.txt     # Python dependencies
└── templates/
    └── index.html       # Full dashboard UI
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Main dashboard UI |
| `/api/status` | GET | Server + DB health check |
| `/api/team` | GET | Team record, recent games, schedule |
| `/api/players` | GET | Player stats and averages |
| `/api/predictions` | GET | ML forecasts by player |
| `/api/refresh` | POST | Trigger manual data refresh |
| `/api/stream` | GET | SSE live update stream |

## MSSQL Support

To swap SQLite for Microsoft SQL Server, set these environment variables before starting:

```bash
set USE_MSSQL=true
set MSSQL_CONN_STR=DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost;DATABASE=HornetsDB;Trusted_Connection=yes;
```

## Tracked Players

| Player | Position | Jersey |
|--------|----------|--------|
| LaMelo Ball | Point Guard | #2 |
| Brandon Miller | Small Forward | #24 |
| Kon Knueppel | Shooting Guard | #21 |
| Miles Bridges | Small Forward | #0 |

## ML Model

The prediction engine uses **exponentially weighted rolling averages** (no external ML libraries required, fully compatible with Python 3.15+):

- Looks back over the last 8 games per player
- Recent games are weighted more heavily (decay factor = 0.85)
- Confidence score derived from scoring consistency (lower variance = higher confidence)
- Predictions generated for next 5 scheduled games
- Model auto-retrains every 4 hours with fresh data

## License

MIT
