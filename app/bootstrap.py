"""Versioned SQL migrations and explicit, synthetic demo seed data."""

from pathlib import Path

import psycopg

from app.db import database_url

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def migrate(conn):
    conn.execute("SELECT pg_advisory_xact_lock(682071)")
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY)")
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        if not conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name = %s", (migration.name,)
        ).fetchone():
            conn.execute(migration.read_text())
            conn.execute("INSERT INTO schema_migrations VALUES (%s)", (migration.name,))


def seed(conn):
    conn.execute("""INSERT INTO practices VALUES
        ('maine', 'America/New_York'), ('hawaii', 'Pacific/Honolulu') ON CONFLICT DO NOTHING""")
    conn.execute("""INSERT INTO users (id, role, practice_id) VALUES
        ('office-maine', 'office', 'maine'), ('office-colleague', 'office', 'maine'),
        ('office-hawaii', 'office', 'hawaii'),
        ('member-1', 'member', NULL), ('member-2', 'member', NULL),
        ('member-3', 'member', NULL), ('member-uncovered', 'member', NULL),
        ('reviewer-1', 'reviewer', NULL), ('reviewer-2', 'reviewer', NULL),
        ('admin-1', 'admin', NULL) ON CONFLICT DO NOTHING""")
    conn.execute("""INSERT INTO users (id, role, practice_id, principal_type) VALUES
        ('partner-maine', 'office', 'maine', 'service') ON CONFLICT DO NOTHING""")
    conn.execute("""INSERT INTO members VALUES ('member-1', 'member-1'), ('member-2', 'member-2'),
        ('member-3', 'member-3'), ('member-uncovered', 'member-uncovered')
        ON CONFLICT DO NOTHING""")
    conn.execute("""INSERT INTO practice_members VALUES
        ('maine', 'member-1'), ('maine', 'member-2'), ('maine', 'member-uncovered'),
        ('hawaii', 'member-1') ON CONFLICT DO NOTHING""")
    conn.execute("""INSERT INTO coverages (member_id, starts_on, ends_on) VALUES
        ('member-1', '2020-01-01', '2040-12-31'),
        ('member-2', '2020-01-01', '2040-12-31'),
        ('member-3', '2020-01-01', '2040-12-31') ON CONFLICT DO NOTHING""")
    conn.execute("""INSERT INTO reviewer_patients VALUES ('reviewer-1', 'member-2')
        ON CONFLICT DO NOTHING""")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", action="store_true", help="Load synthetic demo identities")
    args = parser.parse_args()
    with psycopg.connect(database_url()) as connection:
        migrate(connection)
        if args.seed:
            seed(connection)
