"""Same atomic SQLite costs ledger as CloddsBot's chat and existing observer."""
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import threading

from .common import iso, now, number, request


class Budget:
    def __init__(self, settings, env=None, path=None, transport=request):
        self.settings = settings
        self.env = env if env is not None else os.environ
        self.path = path or self.env.get('OBSERVER_LEDGER', '/observer/observer.sqlite')
        self.transport = transport
        self._catalog = None
        self._catalog_at = None
        self.lock = threading.Lock()

    def connection(self):
        # Do not silently create an independent ledger when the shared mount is absent.
        if not Path(self.path).is_file():
            raise RuntimeError('shared_budget_ledger_missing')
        connection = sqlite3.connect(self.path, timeout=20)
        connection.execute('PRAGMA busy_timeout=20000')
        return connection

    @staticmethod
    def total(connection, prefix):
        return connection.execute('SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM costs WHERE at LIKE ?', (prefix + '%',)).fetchone()[0]

    def usage(self):
        with self.connection() as connection:
            return {'day': self.total(connection, iso()[:10]), 'month': self.total(connection, iso()[:7]),
                    'daily_limit': self.settings['daily_ai_usd'], 'monthly_limit': self.settings['monthly_ai_usd']}

    def reserve(self, amount, provider):
        if number(amount) is None or not 0 < amount <= self.settings['max_ai_call_usd']:
            raise RuntimeError('ai_call_reservation_too_large')
        daily, monthly = number(provider.get('usage_daily')), number(provider.get('usage_monthly'))
        if daily is None or monthly is None or min(daily, monthly) < 0:
            raise RuntimeError('provider_budget_unknown')
        remaining = number(provider.get('limit_remaining'))
        if remaining is not None and remaining < amount:
            raise RuntimeError('provider_credit_exhausted')
        with self.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            fault = connection.execute("SELECT value FROM kv WHERE key='budgetFault'").fetchone()
            if fault and json.loads(fault[0]):
                raise RuntimeError('shared_ai_budget_fault')
            if (self.total(connection, iso()[:10]) + daily + amount > self.settings['daily_ai_usd']
                    or self.total(connection, iso()[:7]) + monthly + amount > self.settings['monthly_ai_usd']):
                raise RuntimeError('shared_ai_budget_exhausted')
            row = connection.execute('INSERT INTO costs(at,reserved,state) VALUES(?,?,?)', (iso(), amount, 'hub_reserved'))
            connection.commit()
            return row.lastrowid

    def settle(self, reservation, actual):
        if number(actual) is not None and actual >= 0:
            with self.connection() as connection:
                connection.execute('BEGIN IMMEDIATE')
                row = connection.execute('SELECT reserved FROM costs WHERE id=?', (reservation,)).fetchone()
                overrun = row and actual > row[0] + .000001
                connection.execute('UPDATE costs SET actual=?,state=? WHERE id=?',
                                   (actual, 'hub_overrun' if overrun else 'hub_complete', reservation))
                if overrun:
                    connection.execute("INSERT INTO kv(key,value) VALUES('budgetFault','true') ON CONFLICT(key) DO UPDATE SET value='true'")

    def completion(self, body):
        key = self.env.get('OPENROUTER_API_KEY')
        if not key:
            raise RuntimeError('openrouter_not_configured')
        if body.get('stream'):
            raise ValueError('streaming_disabled_for_accounted_calls')
        allowed = {self.settings['model']}
        if body.get('model') not in allowed:
            raise ValueError('model_not_configured')
        for message in body.get('messages', []):
            content = message.get('content')
            if isinstance(content, list) and any(not isinstance(x, dict) or x.get('type') != 'text' for x in content):
                raise ValueError('only_priced_text_inputs_supported')
        if body.get('plugins') or body.get('web_search_options'):
            raise ValueError('unpriced_provider_tools_disabled')
        auth = {'Authorization': 'Bearer ' + key}
        with self.lock:
            if not self._catalog_at or (now() - self._catalog_at).total_seconds() > 3600:
                self._catalog, _ = self.transport('https://openrouter.ai/api/v1/models', timeout=30)
                self._catalog_at = now()
        meta = next((m for m in self._catalog.get('data', []) if m['id'] == body['model']), None)
        pricing = (meta or {}).get('pricing', {})
        prompt, output = number(pricing.get('prompt')), number(pricing.get('completion'))
        if prompt is None or output is None or min(prompt, output) < 0 or any(number(pricing.get(k), 0) != 0 for k in ('request', 'image', 'web_search', 'internal_reasoning')):
            raise RuntimeError('unsupported_model_pricing')
        cap = min(int(body.get('max_tokens') or body.get('max_completion_tokens') or 1800), 2400)
        if not 1 <= cap <= 2400:
            raise ValueError('invalid_max_tokens')
        # Bound every call, including tool definitions/history. Provider price caps are
        # sent as well; bytes conservatively bound text tokens plus a framing margin.
        amount = max(0.000001, ((len(json.dumps(body).encode()) + 4096) * prompt * 2 + cap * output * 2) * 1.1)
        provider, _ = self.transport('https://openrouter.ai/api/v1/key', headers=auth)
        reservation = self.reserve(amount, provider['data'])
        payload = {k: v for k, v in body.items() if k not in ('stream', 'stream_options', 'max_completion_tokens', 'user')}
        payload.update(max_tokens=cap, stream=False,
                       provider={'allow_fallbacks': False, 'require_parameters': True,
                                 'max_price': {'prompt': prompt * 2e6, 'completion': output * 2e6}})
        response, _ = self.transport('https://openrouter.ai/api/v1/chat/completions', headers=auth, body=payload, timeout=150)
        actual = number(response.get('usage', {}).get('cost'))
        self.settle(reservation, actual)
        if actual is not None and actual > amount + 0.000001:
            raise RuntimeError('provider_exceeded_reserved_cost')
        return response
