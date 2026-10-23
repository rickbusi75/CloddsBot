/**
 * n8n Integration Skill - Manage local n8n instance from chat
 *
 * Commands:
 * /n8n                        - Help / status
 * /n8n status                 - Connection + API status
 * /n8n list [limit]           - List workflows (id, name, active)
 * /n8n get <id>               - Get full workflow JSON
 * /n8n create <json>          - Create a workflow from inline JSON
 * /n8n update <id> <json>     - Update an existing workflow
 * /n8n activate <id>          - Activate a workflow
 * /n8n deactivate <id>        - Deactivate a workflow
 * /n8n run <id>               - Trigger a manual run
 * /n8n executions [limit]     - Recent executions summary
 *
 * Configuration (bot.env):
 * - N8N_API_KEY: n8n API key (required)
 * - N8N_BASE_URL: defaults to http://172.17.0.1:5678/api/v1
 */

interface N8nRequestResult {
  status: number;
  ok: boolean;
  data: unknown;
  error?: string;
}

function n8nConfig(): { base: string; apiKey: string } {
  const base = (process.env.N8N_BASE_URL || 'http://172.17.0.1:5678/api/v1').replace(/\/+$/, '');
  const apiKey = process.env.N8N_API_KEY || '';
  return { base, apiKey };
}

async function n8nRequest(
  method: string,
  path: string,
  body?: unknown,
  timeoutMs = 30000
): Promise<N8nRequestResult> {
  const { base, apiKey } = n8nConfig();
  if (!apiKey) {
    return { status: 0, ok: false, data: null, error: 'N8N_API_KEY missing in bot.env' };
  }
  const url = base + (path.startsWith('/') ? path : `/${path}`);
  try {
    const res = await fetch(url, {
      method,
      headers: {
        'X-N8N-API-KEY': apiKey,
        'Content-Type': 'application/json',
        accept: 'application/json',
      },
      body: method === 'GET' || method === 'HEAD' ? undefined : body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    });
    const text = await res.text();
    let data: unknown = text;
    try {
      data = JSON.parse(text);
    } catch {
      // keep raw text
    }
    return { status: res.status, ok: res.ok, data };
  } catch (err) {
    return {
      status: 0,
      ok: false,
      data: null,
      error: err instanceof Error ? err.message : String(err),
    };
  }
}

function truncate(value: string, max = 3800): string {
  if (value.length <= max) return value;
  return value.slice(0, max) + `\n… (${value.length - max} chars truncated)`;
}

function parseJsonArg(raw: string): { ok: true; value: unknown } | { ok: false; error: string } {
  const text = raw.trim();
  if (!text) return { ok: false, error: 'JSON vide' };
  try {
    return { ok: true, value: JSON.parse(text) };
  } catch (err) {
    return { ok: false, error: `JSON invalide: ${err instanceof Error ? err.message : String(err)}` };
  }
}

function shortWorkflow(w: Record<string, unknown>): string {
  const id = w.id ?? '?';
  const name = w.name ?? '?';
  const active = w.active === true ? '🟢 actif' : '⚪️ inactif';
  const nodes = Array.isArray(w.nodes) ? w.nodes.length : '?';
  const tags = Array.isArray((w as { tags?: unknown }).tags)
    ? ` tags=${(w.tags as unknown[]).length}`
    : '';
  const updatedAt = typeof w.updatedAt === 'string' ? w.updatedAt.slice(0, 16) : '';
  return `- \`${id}\` ${active} **${name}** (nodes: ${nodes}${tags}${updatedAt ? `, maj: ${updatedAt}` : ''})`;
}

async function handle(args: string): Promise<string> {
  const parts = args.trim().split(/\s+/);
  const cmd = (parts[0] || 'help').toLowerCase();
  const rest = parts.slice(1);

  switch (cmd) {
    case 'help':
    case '': {
      return `**n8n — intégration du labo**

/n8n status               État de la connexion à l'API n8n
/n8n list [limit]         Liste des workflows (défaut 20)
/n8n get <id>             Détail complet d'un workflow (JSON)
/n8n create <json>        Créer un workflow (name, nodes, connections)
/n8n update <id> <json>   Mettre à jour un workflow
/n8n activate <id>        Activer un workflow
/n8n deactivate <id>      Désactiver un workflow
/n8n run <id>             Exécuter un workflow manuellement
/n8n executions [limit]   Dernières exécutions (défaut 10)

Config: N8N_API_KEY + N8N_BASE_URL dans bot.env. L'agent peut aussi
utiliser son outil n8n_api pour des appels précis.`;
    }

    case 'status': {
      const r = await n8nRequest('GET', '/workflows?limit=1');
      if (!r.ok) {
        return `🔴 n8n injoignable (${r.error || `HTTP ${r.status}`}). Vérifier N8N_BASE_URL/N8N_API_KEY et le port publié 172.17.0.1:5678.`;
      }
      return `🟢 n8n joignable. API OK (HTTP ${r.status}).`;
    }

    case 'list':
    case 'ls': {
      const limit = Math.min(Math.max(parseInt(rest[0] || '20', 10) || 20, 1), 100);
      const r = await n8nRequest('GET', `/workflows?limit=${limit}`);
      if (!r.ok) return `❌ Erreur n8n (${r.error || `HTTP ${r.status}`})`;
      const data = r.data as { data?: Array<Record<string, unknown>> };
      const items = data?.data || [];
      if (!items.length) return 'Aucun workflow.';
      const lines = items.map((w) => shortWorkflow(w));
      return `**Workflows n8n** (${items.length})\n\n${lines.join('\n')}`;
    }

    case 'get': {
      const id = rest[0];
      if (!id) return 'Usage: /n8n get <id>';
      const r = await n8nRequest('GET', `/workflows/${id}`);
      if (!r.ok) return `❌ HTTP ${r.status}: ${truncate(JSON.stringify(r.data))}`;
      return truncate('```json\n' + JSON.stringify(r.data, null, 2) + '\n```');
    }

    case 'create': {
      const parsed = parseJsonArg(rest.join(' '));
      if (!parsed.ok) return `❌ ${parsed.error}. Usage: /n8n create {"name":"...","nodes":[...],"connections":{}}`;
      const payload = parsed.value as Record<string, unknown>;
      if (!payload.name) return '❌ Le JSON doit contenir au moins "name".';
      const r = await n8nRequest('POST', '/workflows', payload);
      if (!r.ok) return `❌ Création refusée (HTTP ${r.status}): ${truncate(JSON.stringify(r.data))}`;
      const w = r.data as Record<string, unknown>;
      return `✅ Workflow créé: \`${w.id}\` **${w.name}** (inactif par défaut). Activer avec /n8n activate ${w.id}`;
    }

    case 'update': {
      const id = rest[0];
      const parsed = parseJsonArg(rest.slice(1).join(' '));
      if (!id || !parsed.ok) return 'Usage: /n8n update <id> <json>';
      const r = await n8nRequest('PUT', `/workflows/${id}`, parsed.value);
      if (!r.ok) return `❌ HTTP ${r.status}: ${truncate(JSON.stringify(r.data))}`;
      const w = r.data as Record<string, unknown>;
      return `✅ Workflow \`${w.id}\` mis à jour.`;
    }

    case 'activate':
    case 'deactivate': {
      const id = rest[0];
      if (!id) return `Usage: /n8n ${cmd} <id>`;
      const r = await n8nRequest('POST', `/workflows/${id}/${cmd}`);
      if (!r.ok) return `❌ HTTP ${r.status}: ${truncate(JSON.stringify(r.data))}`;
      return `✅ Workflow \`${id}\` ${cmd === 'activate' ? 'activé 🟢' : 'désactivé ⚪️'}.`;
    }

    case 'run': {
      const id = rest[0];
      if (!id) return 'Usage: /n8n run <id>';
      const r = await n8nRequest('POST', `/workflows/${id}/run`, {});
      if (!r.ok) return `❌ HTTP ${r.status}: ${truncate(JSON.stringify(r.data))}`;
      return `✅ Exécution déclenchée pour \`${id}\`.`;
    }

    case 'executions':
    case 'execs': {
      const limit = Math.min(Math.max(parseInt(rest[0] || '10', 10) || 10, 1), 50);
      const r = await n8nRequest('GET', `/executions?limit=${limit}`);
      if (!r.ok) return `❌ HTTP ${r.status}: ${truncate(JSON.stringify(r.data))}`;
      const data = r.data as { data?: Array<Record<string, unknown>> };
      const items = data?.data || [];
      if (!items.length) return 'Aucune exécution récente.';
      const icon: Record<string, string> = { success: '✅', error: '❌', running: '⏳', waiting: '⏸️' };
      const lines = items.map((e) => {
        const wf = (e.workflowData as Record<string, unknown> | undefined)?.name ?? e.workflowId ?? '?';
        const st = String(e.status ?? '?');
        const at = typeof e.startedAt === 'string' ? e.startedAt.replace('T', ' ').slice(0, 19) : '';
        return `${icon[st] || '•'} \`${e.id}\` ${st} — ${wf}${at ? ` (${at})` : ''}`;
      });
      return `**Exécutions récentes** (${items.length})\n\n${lines.join('\n')}`;
    }

    default:
      return `Sous-commande inconnue: \`${cmd}\`. Tape /n8n pour l'aide.`;
  }
}

export default {
  name: 'n8n',
  description: 'Manage the local n8n instance: list, create, update, activate workflows and inspect executions',
  commands: ['/n8n'],
  requires: { env: ['N8N_API_KEY'] },
  handle,
};
