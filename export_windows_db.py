"""Export a new portable SQLite snapshot from the configured Windows backend."""
import argparse
from pathlib import Path
import sqlite3
from contextlib import closing

import database as db


def export(target):
    # Exclusive creation protects an earlier export, including concurrent invocations.
    with target.open("xb"):
        pass
    try:
        with closing(sqlite3.connect(target)) as dest:
            if db.MSSQL:
                with closing(db.get_connection()) as src:
                    # Transaction-wide consistent read. Stop dashboard writes before export.
                    cursor = src.cursor()
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                    dest.executescript(Path(__file__).with_name("src").joinpath("schema.sql").read_text())
                    for table, _, _, has_id in db.TABLES:
                        names = (["id"] if has_id else []) + [n for n, _ in db.table_columns(table)]
                        columns = ", ".join(names)
                        cursor.execute(f"SELECT {columns} FROM dbo.{table}")
                        insert = f"INSERT INTO {table} ({columns}) VALUES ({', '.join('?' for _ in names)})"
                        count = 0
                        while batch := cursor.fetchmany(2000):
                            dest.executemany(insert, map(tuple, batch))
                            count += len(batch)
                        actual = dest.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                        if actual != count:
                            raise ValueError(f"{table} row count mismatch")
                        print(f"{table}: {actual}")
                    dest.commit()
                    src.rollback()
            else:
                source = Path(db.DB_PATH).resolve().as_uri() + "?mode=ro"
                with closing(sqlite3.connect(source, uri=True)) as src:
                    src.backup(dest)
            if dest.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("SQLite snapshot failed integrity check")
    except BaseException:
        target.unlink()
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    export(parser.parse_args().destination)
