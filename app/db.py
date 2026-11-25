import os
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def database_url() -> str:
    return os.environ["DATABASE_URL"]


def make_pool() -> ConnectionPool:
    return ConnectionPool(
        database_url(),
        min_size=1,
        max_size=10,
        open=False,
        kwargs={"row_factory": dict_row},
        configure=configure_connection,
    )


def configure_connection(conn: Connection) -> None:
    conn.execute("SET TIME ZONE 'UTC'")
    conn.commit()


def get_db(request: Request) -> Iterator[Connection]:
    # A request succeeds only after this transaction (including its audit) commits.
    with request.app.state.pool.connection() as conn:
        if request.method == "GET":
            # Header and procedures share one committed snapshot during a concurrent decision.
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        yield conn


DB = Annotated[Connection, Depends(get_db, scope="function")]
