"""
One-time copy of a SQLite hornets.db into the SQL Server database named by the MSSQL_* settings.

Usage (as an account with rights on the target database):
  set DB_BACKEND=mssql
  set MSSQL_SERVER=IDOLAPP01\\IDOLMSSQL
  python migrate_sqlite_to_mssql.py path\\to\\hornets.db [--replace]

Creates the tables if needed. Refuses to touch tables that already hold rows unless --replace is given,
in which case those tables are emptied first. Row ids are copied as they are. Prints the row count of every
table on both sides.
"""
import sqlite3
import sys

import database as db

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    replace = "--replace" in sys.argv
    if not db.MSSQL or len(args) != 1:
        sys.exit(__doc__)
    src = sqlite3.connect(f"file:{args[0]}?mode=ro", uri=True)
    db.init_db()
    dst = db.get_connection()
    cur = dst.cursor()
    cur.fast_executemany = True
    tables = [t for t, _, _, _ in db.TABLES]
    # init_db seeds the legacy 2025-26 roster, so an empty database still has those four rows.
    busy = [t for t in tables if cur.execute(f"SELECT COUNT(*) FROM dbo.{t}").fetchone()[0]
            and not (t == "roster" and cur.execute("SELECT COUNT(*) FROM dbo.roster").fetchone()[0]
                     <= len(db.LEGACY_ROSTER_2025_26))]
    if busy and not replace:
        sys.exit(f"Target tables already hold rows: {', '.join(busy)}. Re-run with --replace to overwrite them.")
    for t in tables:
        have = {r[1] for r in src.execute(f"PRAGMA table_info({t})")}
        cols = (["id"] if t in db.HAS_ID else []) + [c for c, _ in db.table_columns(t) if c in have]
        rows = [tuple(r) for r in src.execute(f"SELECT {', '.join(cols)} FROM {t}")]
        cur.execute(f"DELETE FROM dbo.{t}")
        if rows:
            if t in db.HAS_ID:
                cur.execute(f"SET IDENTITY_INSERT dbo.{t} ON")
            cur.executemany(f"INSERT INTO dbo.{t} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", rows)
            if t in db.HAS_ID:
                cur.execute(f"SET IDENTITY_INSERT dbo.{t} OFF")    # the identity seed moves past the copied ids
        dst.commit()
        n = cur.execute(f"SELECT COUNT(*) FROM dbo.{t}").fetchone()[0]
        print(f"{t:18} sqlite {len(rows):6}  mssql {n:6}  {'OK' if n == len(rows) else 'MISMATCH'}")
    dst.close()

if __name__ == "__main__":
    main()
