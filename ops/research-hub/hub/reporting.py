"""One consolidated daily report, computed from recorded facts without LLM calls."""
import datetime as dt
import json
import sqlite3
from zoneinfo import ZoneInfo

from .common import now, number, stamp, UpstreamError
from shadow_executor.status import summary as shadow_summary

THEMES = {'sports': 'sport', 'weather': 'météo', 'crypto': 'crypto', 'disasters': 'catastrophes',
          'economics': 'économie', 'geopolitics': 'géopolitique', 'politics': 'politique',
          'science': 'sciences', 'business': 'entreprises', 'other': 'autres événements'}


def owner(engine):
    try:
        with engine.budget.connection() as c:
            row = c.execute("SELECT value FROM kv WHERE key='digestOwner'").fetchone()
            return json.loads(row[0]) if row else 'observer'
    except (RuntimeError, sqlite3.Error, ValueError):
        return 'unavailable'


def build(engine):
    local = now().astimezone(ZoneInfo(engine.settings['timezone']))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    p = engine.paper_portfolio()
    account = engine.store.one('SELECT at,data FROM account_snapshots ORDER BY id DESC LIMIT 1')
    lines = ['CloddsBot + n8n — ' + local.strftime('%d/%m/%Y'), 'Compte réel']
    if engine.paused():
        lines.insert(1, 'Recherche en pause ; suivi des positions et bilans maintenus.')
    if account:
        a = account['data']
        stale = (now() - account['at']).total_seconds() > 900 or bool(engine.store.get('account_error'))
        available = number(a.get('available_cash'), number(a.get('cash')) if a['open_orders'] == 0 else None)
        cash_text = f'{available:.2f} pUSD' if available is not None else 'à rapprocher'
        lines += [f"Disponible hors frais : {cash_text} ; positions : {a['position_value']:.2f} pUSD (indicatif)",
                  f"{len(a['positions'])} position(s), {a['open_orders']} ordre(s) ouvert(s). Lecture : " + account['at'].astimezone(ZoneInfo(engine.settings['timezone'])).strftime('%H:%M')]
        if a.get('reserved_buys'):
            lines += [f"Solde : {a['cash']:.2f} ; réservé par les achats ouverts : {a['reserved_buys']:.2f} pUSD (hors frais)"]
        if a.get('reservation_exceeds_balance'):
            lines += ['Engagements supérieurs au solde lu : rapprochement requis.']
        for position in sorted(a['positions'], key=lambda x: number(x.get('currentValue'), 0), reverse=True)[:3]:
            lines += ['• ' + str(position.get('title', 'Position'))[:100] + ' — ' + str(position.get('outcome', ''))
                      + f" : {number(position.get('currentValue'), 0):.2f} pUSD"]
        if stale:
            lines += ['Dernier état non confirmé : lecture trop ancienne ou erreur actuelle.']
        flows = a.get('flows') or {}
        if flows.get('complete') and number(flows.get('deposits')) is not None:
            net = number(a['cash'], 0) + number(a['position_value'], 0) - number(flows['deposits'], 0)
            lines += [f"Résultat net latent : {net:+.2f} pUSD (cash {a['cash']:.2f} + positions {a['position_value']:.2f} − dépôts déduits {flows['deposits']:.2f})",
                      'Position non liquidée ; pas un gain établi.']
        else:
            lines += ['Gain net réel : rapprochement des apports/retraits encore incomplet.']
    else:
        lines += ['Lecture du compte indisponible.']
    lines += ['', 'Simulations séparées']
    lines += [f"n8n : valeur {p['equity']:.2f} / départ {p['capital']:.2f} ; réalisé {p['realized']:+.2f}, latent {p['unrealized']:+.2f}",
              f"n8n engagé : {p['exposure']:.2f} ; {p['open']} position(s)"]
    observer = engine.observer_reference()
    op = observer.get('portfolio')
    if op:
        lines += [f"Observer : valeur {op['equity']:.2f} / départ {op['capital']:.2f} ; réalisé {op['realized']:+.2f}, latent {op['unrealized']:+.2f}",
                  f"Observer engagé : {op['exposure']:.2f} ; {op['open']} position(s)"]
        when = stamp(observer.get('at'))
        if not when or (now() - when).total_seconds() > 3600:
            lines += ['État observer ancien : valorisation non confirmée.']
        metrics = observer.get('forecasts', {})
        if metrics.get('resolved') and number(metrics.get('brier')) is not None:
            lines += [f"Brier observer : {metrics['brier']:.4f} ; référence marché {metrics['marketBrier']:.4f} ({metrics['resolved']} prévisions / {metrics['markets']} marchés)"]
    scores = engine.store.one('SELECT count(result) n,count(DISTINCT market_id) FILTER (WHERE result IS NOT NULL) markets,avg((probability-result)^2) brier,avg((baseline-result)^2) baseline FROM forecasts')
    if scores['n']:
        lines += [f"Brier n8n : {scores['brier']:.4f} ; référence marché {scores['baseline']:.4f} ({scores['n']} prévisions / {scores['markets']} marchés)"]
    shadow = shadow_summary(engine.store)
    if shadow['installed']:
        state = 'en observation' if shadow.get('fresh') else 'suivi à vérifier'
        lines += [f"Exécuteur dédié {state} : {shadow.get('candidates', 0)} intention(s) simulée(s), aucun envoi d'ordre possible."]
    counts = engine.store.rows("SELECT theme,count(*) n FROM research WHERE created_at>=%s AND status='complete' GROUP BY theme ORDER BY n DESC", (start,))
    lines += ['', 'Recherche du jour : ' + (', '.join(f"{THEMES.get(x['theme'],x['theme'])} {x['n']}" for x in counts) or 'aucune analyse terminée')]
    flagged = engine.store.get('recheck_flagged') or []
    if flagged:
        lines += ['Nouvelles publications à relire (fait nouveau) :']
        for item in flagged[:3]:
            lines += ['• ' + str(item.get('question', item.get('research_id', '')))[:90]
                      + ' — termes : ' + ', '.join(item.get('matched_terms', [])[:4])]
    latest = engine.store.rows("SELECT m.question,r.result FROM research r JOIN markets m ON m.id=r.market_id WHERE r.status='complete' AND r.finished_at>=%s ORDER BY r.finished_at DESC LIMIT 2", (start,))
    for row in latest:
        lines += ['• ' + row['question'][:100] + ' : ' + row['result']['action'], 'Motif de l’agent : ' + str(row['result'].get('reason', ''))[:360]]
    try:
        budget = engine.budget.usage()
        lines += [f"IA partagée (journal + réservations, UTC) : {budget['day']:.4f} $ jour ; {budget['month']:.4f}/{budget['monthly_limit']:.2f} $ mois"]
    except Exception:
        lines += ['Budget IA partagé indisponible.']
    problems = engine.store.rows("SELECT id,status FROM sources WHERE status IN ('error','configuration_required') ORDER BY id")
    if problems:
        lines += ['Sources à vérifier : ' + ', '.join(x['id'] for x in problems)]
    issues = engine.store.one("SELECT count(*) n FROM notifications WHERE status IN ('uncertain','failed')")
    if issues['n']:
        lines += [f"Envois à vérifier : {issues['n']} (aucune réémission aveugle)."]
    lines += ['Nouvelle chaîne en simulation. Aucun ordre réel soumis par elle.']
    return '\n'.join(lines)[:3800]


def send(engine, key, prefix=''):
    if not engine.settings.get('digest_enabled') or owner(engine) != 'research-hub':
        return {'delivery': 'not_owner'}
    if not engine.env.get('TELEGRAM_BOT_TOKEN') or not engine.settings.get('telegram_chat_id'):
        return {'delivery': 'configuration_required'}
    text = (prefix + build(engine))[:3900]
    engine.store.execute("INSERT INTO notifications(key,text) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET text=excluded.text WHERE notifications.status='pending'", (key, text))
    claimed = engine.store.one("UPDATE notifications SET status='sending',attempts=attempts+1,attempted_at=now(),error=NULL WHERE key=%s AND status='pending' RETURNING text", (key,))
    if not claimed:
        return {'delivery': 'already_attempted'}
    try:
        result, _ = engine.request('https://api.telegram.org/bot' + engine.env['TELEGRAM_BOT_TOKEN'] + '/sendMessage',
            body={'chat_id': engine.settings['telegram_chat_id'], 'text': claimed['text'], 'disable_web_page_preview': True})
        message_id = (result.get('result') or {}).get('message_id')
        if result.get('ok') is not True or not isinstance(message_id, int):
            raise RuntimeError('telegram_delivery_not_confirmed')
        engine.store.execute("UPDATE notifications SET status='sent',sent_at=now(),provider_message_id=%s,error=NULL WHERE key=%s", (str(message_id), key))
        return {'delivery': 'sent', 'message_id': message_id}
    except Exception as error:
        status = 'pending' if isinstance(error, UpstreamError) and str(error) == 'upstream_http_429' else 'uncertain'
        engine.store.execute('UPDATE notifications SET status=%s,error=%s WHERE key=%s', (status, 'delivery_not_confirmed', key))
        raise


def scheduled(engine):
    local = now().astimezone(ZoneInfo(engine.settings['timezone']))
    if not engine.settings.get('digest_enabled') or local.hour != engine.settings['digest_hour']:
        return {'delivery': 'not_scheduled'}
    return send(engine, 'daily:' + local.date().isoformat())


def status(engine):
    return {'enabled': bool(engine.settings.get('digest_enabled')), 'owner': owner(engine),
            'hour': engine.settings['digest_hour'], 'timezone': engine.settings['timezone'],
            'last': engine.store.one('SELECT key,status,sent_at,provider_message_id FROM notifications ORDER BY created_at DESC LIMIT 1')}
