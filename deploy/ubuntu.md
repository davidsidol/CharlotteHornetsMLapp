# Deploy the Charlotte Hornets dashboard on Ubuntu 24.04

The Rust executable serves the existing UI on port 8080, runs a four-hour refresh scheduler, and uses Eastern time for NBA season boundaries and tip-off. SQLite stores the team, player and league history, forecasts, and graded picks. A fresh database starts empty and fills in during the background refresh. The first refresh requests four seasons of history and takes longer than later updates.

## Install the executable

From an Ubuntu terminal, install the runtime dependencies.

```sh
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
git clone --branch codex/rust-ubuntu --single-branch https://github.com/davidsidol/CharlotteHornetsMLapp.git
cd CharlotteHornetsMLapp
```

Choose one of these build options.

* **Download a build.** In the repository's **Actions → Rust Ubuntu build**, open a successful run for your chosen commit. Download the artifact matching `uname -m` (`x86_64` or `aarch64`). Extract its ZIP, then run the following from the checkout, substituting the downloaded archive path.

  ```sh
  tar -xzf /path/to/hornets-ubuntu-24.04-x86_64.tar.gz
  sudo install -m 0755 hornets-dashboard /usr/local/bin/
  ```

* **Build from source.** Install a current stable Rust toolchain (Rust 1.87 or newer) with Cargo, then compile against the checked-in dependency lockfile.

  ```sh
  sudo apt-get install -y build-essential
  cargo build --locked --release
  sudo install -m 0755 target/release/hornets-dashboard /usr/local/bin/
  ```

## Prepare storage

```sh
id hornets-dashboard >/dev/null 2>&1 || sudo useradd --system --user-group --home-dir /var/lib/hornets-dashboard --shell /usr/sbin/nologin hornets-dashboard
sudo install -d -o hornets-dashboard -g hornets-dashboard -m 0750 /var/lib/hornets-dashboard
```

For a fresh installation, proceed to **Start the service**. To preserve Windows history and the original live prediction record, import a snapshot first.

## Move existing Windows data

On Windows, use a checkout of this branch and the Python environment of the running Windows app. Run the exporter with the same database settings and account as that app. Pick a **new** output filename. For SQLite, set `DB_PATH` explicitly so the exporter reads the active database.

```powershell
$env:DB_BACKEND = 'sqlite'
$env:DB_PATH = 'C:\path\to\active\hornets.db'
python export_windows_db.py C:\Temp\hornets-export.db
```

For SQL Server, stop the Windows dashboard writer for the final export. Set `DB_BACKEND=mssql`, `MSSQL_SERVER`, and `MSSQL_DATABASE` to the existing instance and database. Run the same exporter under the Windows identity with read access and the existing pyodbc/ODBC installation. It reads the tables in one serializable transaction, retains IDs, reports counts and checks the SQLite file. The Ubuntu runtime uses the exported SQLite database.

Copy the exported file to Ubuntu. Before first startup, install it into the empty state directory. The command below refuses to replace an existing Ubuntu database.

```sh
sudo sh -ec 'test ! -e /var/lib/hornets-dashboard/hornets.db; install -m 0640 -o hornets-dashboard -g hornets-dashboard "$1" /var/lib/hornets-dashboard/hornets.db' sh /path/to/hornets-export.db
```

## Start the service

```sh
sudo install -m 0644 deploy/hornets-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hornets-dashboard
systemctl status hornets-dashboard --no-pager
curl -fsS http://127.0.0.1:8080/api/status
journalctl -u hornets-dashboard -n 60 --no-pager
```

Open `http://YOUR-UBUNTU-IP:8080/` from the LAN. To change the bind address or port, set `HOST` or `PORT` in `/etc/default/hornets-dashboard` and restart the service. Before cutover, compare current and past seasons, player and team row counts, and the live/backtest record against Windows. Watch a refresh finish and check that `last_fetch` advances during the season. The endpoint returns HTTP 200 even on an empty database. Its `db_ready` field reports whether player history exists.

## Refresh and forecast behavior

* The app reads official NBA regular-season team and player league logs, team information, rosters and schedules. League player logs include previous teams for players who recently joined Charlotte. Network access to `stats.nba.com` is required for new stats. The live scoreboard uses ESPN first and NBA as a fallback. A working scoreboard alone does not establish stats-feed connectivity.
* The Rust team model fits ratings and rest adjustments using earlier seasons, tests settings against a held-out season when enough history exists, then forecasts up to five upcoming games. The player model learns minutes, per-minute rates and opponent effects, with an eight-game rolling fallback. Fitted parameters and training, baseline, and available holdout metrics are saved in the model_params table.
* The service grades saved live picks separately from retrospective predictions. New forecasts require a future Eastern tip time. Existing SQLite data can be served while upstream requests fail. The journal identifies individual feed failures.
* To trigger a refresh, use the dashboard button or `curl -X POST http://127.0.0.1:8080/api/refresh`. A second request during that refresh joins the existing work. For offline verification on a **separate database copy**, set `DB_PATH=/path/to/copy.db` and run `hornets-dashboard predict`. The `refresh` command runs a full refresh in the foreground and returns a failure code if a feed fails.
* To roll back the application cutover, stop the Ubuntu service and direct users to the original Windows service. Keep the original Windows database and a dated copy of the export. Predictions created after cutover belong to the Ubuntu database.

## Validate a build locally

```sh
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo build --release --locked
python3 tests/migration_smoke.py target/release/hornets-dashboard
```

The test uses temporary databases and NBA-shaped fixtures. It compares stored team and player predictions against the Python implementation, grades a locked live pick, calls the browser API, checks SSE, and exercises SQLite export. Python is needed for this comparison and the one-time Windows exporter.
