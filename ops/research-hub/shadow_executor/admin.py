"""Admin-only initialization; never imported by the worker process."""
import datetime as dt

from psycopg import sql
from psycopg.types.json import Jsonb

from .worker import SCHEMA


def create_role(c, password, role='research_shadow'):
    if c.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (role,)).fetchone():
        raise RuntimeError('shadow_role_already_exists_do_not_overwrite')
    c.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT PASSWORD {}')
              .format(sql.Identifier(role), sql.Literal(password)))


def grant_access(c, role='research_shadow'):
    c.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(c.info.dbname), sql.Identifier(role)))
    c.execute('REVOKE ALL ON SCHEMA execution_shadow FROM PUBLIC')
    c.execute(sql.SQL('GRANT USAGE ON SCHEMA public,execution_shadow TO {}').format(sql.Identifier(role)))
    c.execute(sql.SQL('GRANT SELECT ON public.research,public.markets,public.account_snapshots,public.hub_meta TO {}').format(sql.Identifier(role)))
    c.execute(sql.SQL('GRANT SELECT,INSERT ON execution_shadow.intents TO {}').format(sql.Identifier(role)))
    c.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE ON execution_shadow.state TO {}').format(sql.Identifier(role)))


def initialize(c, password, role='research_shadow'):
    if c.execute("SELECT to_regnamespace('execution_shadow') present").fetchone()['present']:
        raise RuntimeError('shadow_schema_already_exists_do_not_overwrite')
    create_role(c, password, role)
    c.execute(SCHEMA)
    c.execute("INSERT INTO execution_shadow.state VALUES('started_at',%s)",
              (Jsonb(dt.datetime.now(dt.timezone.utc).isoformat()),))
    grant_access(c, role)
