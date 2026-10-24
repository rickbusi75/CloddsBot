import contextlib
import json
import os
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .common import digest

SCHEMA = """
CREATE TABLE IF NOT EXISTS hub_meta(key text PRIMARY KEY,value jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS sources(id text PRIMARY KEY,data jsonb NOT NULL,status text NOT NULL DEFAULT 'new',
 last_success timestamptz,last_error text,checked_at timestamptz);
CREATE TABLE IF NOT EXISTS markets(id text PRIMARY KEY,event_id text NOT NULL,theme text NOT NULL,question text NOT NULL,
 data jsonb NOT NULL,updated_at timestamptz NOT NULL DEFAULT now(),last_researched timestamptz,
 active boolean NOT NULL DEFAULT true,version text NOT NULL);
CREATE INDEX IF NOT EXISTS market_theme ON markets(theme,active,last_researched);
CREATE TABLE IF NOT EXISTS market_snapshots(id bigserial PRIMARY KEY,market_id text NOT NULL,at timestamptz NOT NULL DEFAULT now(),
 version text NOT NULL,data jsonb NOT NULL,UNIQUE(market_id,version));
CREATE TABLE IF NOT EXISTS evidence(id text PRIMARY KEY,source_id text NOT NULL,url text NOT NULL,title text NOT NULL,
 published_at timestamptz,observed_at timestamptz NOT NULL DEFAULT now(),theme text NOT NULL,data jsonb NOT NULL);
CREATE INDEX IF NOT EXISTS evidence_theme ON evidence(theme,observed_at DESC);
CREATE TABLE IF NOT EXISTS jobs(id text PRIMARY KEY,kind text NOT NULL,status text NOT NULL DEFAULT 'running',
 started_at timestamptz NOT NULL DEFAULT now(),finished_at timestamptz,result jsonb,error text);
CREATE TABLE IF NOT EXISTS leases(name text PRIMARY KEY,owner text NOT NULL,expires_at timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS research(id text PRIMARY KEY,market_id text NOT NULL,theme text NOT NULL,status text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),finished_at timestamptz,context jsonb NOT NULL,result jsonb,error text);
CREATE INDEX IF NOT EXISTS research_recent ON research(created_at DESC);
CREATE TABLE IF NOT EXISTS forecasts(id text PRIMARY KEY,market_id text NOT NULL,event_id text NOT NULL,theme text NOT NULL,
 at timestamptz NOT NULL DEFAULT now(),probability double precision NOT NULL,baseline double precision NOT NULL,
 outcome text NOT NULL,result double precision,resolved_at timestamptz,model text NOT NULL,strategy text NOT NULL);
CREATE TABLE IF NOT EXISTS order_intents(id text PRIMARY KEY,research_id text NOT NULL UNIQUE,market_id text NOT NULL,
 event_id text NOT NULL,outcome text NOT NULL,side text NOT NULL,mode text NOT NULL,status text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),data jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS paper_positions(id text PRIMARY KEY,intent_id text NOT NULL UNIQUE,market_id text NOT NULL,
 event_id text NOT NULL,outcome text NOT NULL,token text NOT NULL,shares double precision NOT NULL,
 cost double precision NOT NULL,fees double precision NOT NULL,mark double precision NOT NULL,
 payout double precision,opened_at timestamptz NOT NULL DEFAULT now(),closed_at timestamptz);
CREATE TABLE IF NOT EXISTS account_snapshots(id bigserial PRIMARY KEY,at timestamptz NOT NULL DEFAULT now(),data jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS api_usage(id bigserial PRIMARY KEY,source_id text NOT NULL,at timestamptz NOT NULL DEFAULT now(),
 credits double precision NOT NULL DEFAULT 0,remaining double precision,data jsonb);
CREATE TABLE IF NOT EXISTS notifications(key text PRIMARY KEY,text text NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),
 status text NOT NULL DEFAULT 'pending',sent_at timestamptz,attempts int NOT NULL DEFAULT 0,error text);
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS provider_message_id text;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS attempted_at timestamptz;
CREATE TABLE IF NOT EXISTS execution_events(id text PRIMARY KEY,intent_id text NOT NULL,at timestamptz NOT NULL DEFAULT now(),data jsonb NOT NULL);
"""


class Store:
    def __init__(self, dsn=None):
        self.dsn = dsn or os.environ['DATABASE_URL']

    def connect(self):
        return psycopg.connect(self.dsn, row_factory=dict_row, autocommit=True, connect_timeout=10)

    def init(self, registry):
        with self.connect() as connection:
            connection.execute(SCHEMA)
            for source in registry:
                connection.execute('INSERT INTO sources(id,data) VALUES(%s,%s) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                                   (source['id'], Jsonb(source)))

    def rows(self, sql, params=()):
        with self.connect() as connection:
            return connection.execute(sql, params).fetchall()

    def one(self, sql, params=()):
        rows = self.rows(sql, params)
        return rows[0] if rows else None

    def execute(self, sql, params=()):
        with self.connect() as connection:
            return connection.execute(sql, params).rowcount

    def get(self, key, default=None):
        row = self.one('SELECT value FROM hub_meta WHERE key=%s', (key,))
        return row['value'] if row else default

    def put(self, key, value):
        self.execute('INSERT INTO hub_meta VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, Jsonb(value)))

    def lease(self, name, ttl=600):
        owner = uuid.uuid4().hex
        row = self.one("INSERT INTO leases VALUES(%s,%s,now()+(%s * interval '1 second')) ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at WHERE leases.expires_at<now() RETURNING owner", (name, owner, ttl))
        return owner if row else None

    def release(self, name, owner):
        self.execute('DELETE FROM leases WHERE name=%s AND owner=%s', (name, owner))

    @contextlib.contextmanager
    def job(self, kind):
        owner = self.lease('job:' + kind)
        if not owner:
            yield None
            return
        job_id = uuid.uuid4().hex
        self.execute('INSERT INTO jobs(id,kind) VALUES(%s,%s)', (job_id, kind))
        try:
            result = {}
            yield result
            self.execute("UPDATE jobs SET status='ok',finished_at=now(),result=%s WHERE id=%s", (Jsonb(result), job_id))
        except Exception as error:
            self.execute("UPDATE jobs SET status='error',finished_at=now(),error=%s WHERE id=%s", (type(error).__name__, job_id))
            raise
        finally:
            self.release('job:' + kind, owner)

    def evidence(self, source_id, item):
        value = dict(item)
        key = digest([source_id, value['url'], value.get('title'), value.get('text'), value.get('published_at')])
        self.execute('INSERT INTO evidence(id,source_id,url,title,published_at,theme,data) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                     (key, source_id, value['url'], value.get('title', '')[:500], value.get('published_at'), value.get('theme', 'other'), Jsonb(value)))
        return key

    def source_status(self, source, status, error=None):
        self.execute("UPDATE sources SET status=%s,last_error=%s,checked_at=now(),last_success=CASE WHEN %s='ok' THEN now() ELSE last_success END WHERE id=%s", (status, error, status, source))
