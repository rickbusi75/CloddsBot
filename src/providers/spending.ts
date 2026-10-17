import Sqlite from 'better-sqlite3';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';

/** Shared with the observer: reserve BEFORE sending, including failed requests.
 * Conservative reservations also work for streaming without consuming its body.
 * Disabled unless explicitly configured, preserving upstream behavior by default.
 */
export function reserveAiSpend(path: string, amount: number, provider: Record<string, unknown>,
  daily = .60, monthly = 20, now = new Date()): void {
  if (!Number.isFinite(amount) || amount <= 0 || amount > .05) throw new Error('AI request exceeds its cost ceiling');
  const dailyUsage = Number(provider.usage_daily), monthlyUsage = Number(provider.usage_monthly);
  if (provider.usage_daily == null || provider.usage_monthly == null || !Number.isFinite(dailyUsage) || !Number.isFinite(monthlyUsage) || dailyUsage < 0 || monthlyUsage < 0) {
    throw new Error('AI provider budget unavailable');
  }
  const db = new Sqlite(path, { timeout: 30000 });
  try {
    db.exec('CREATE TABLE IF NOT EXISTS costs(id INTEGER PRIMARY KEY, at TEXT, reserved REAL, actual REAL, state TEXT)');
    db.transaction(() => {
      const at = now.toISOString();
      const spent = (prefix: string) => (db.prepare('SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) AS total FROM costs WHERE at LIKE ?')
        .get(prefix + '%') as { total: number }).total;
      // Add both: provider totals can omit in-flight reservations and include
      // other clients. Double counting settled requests is deliberately conservative.
      if (spent(at.slice(0, 10)) + dailyUsage + amount > daily || spent(at.slice(0, 7)) + monthlyUsage + amount > monthly
        || (provider.limit_remaining != null && Number(provider.limit_remaining) < amount)) throw new Error('AI budget exhausted');
      db.prepare('INSERT INTO costs(at,reserved,state) VALUES(?,?,?)').run(at, amount, 'chat_reserved');
    }).immediate();
  } finally { db.close(); }
}

export function createBudgetFetch(env: NodeJS.ProcessEnv, base: typeof fetch = fetch): typeof fetch {
  return async (input, init) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    if (env.CLODDS_AI_BUDGET_ENABLED !== 'true' || !url.startsWith('https://openrouter.ai/api/') || !url.endsWith('/messages')) return base(input, init);
    if (typeof init?.body !== 'string') throw new Error('Cannot budget non-JSON AI request');
    const body = JSON.parse(init.body);
    // Image/video billing requires a dedicated estimator; reject before spending.
    if (JSON.stringify(body).includes('"type":"image"')) throw new Error('Image requests require an explicit AI cost estimator');
    const auth = { Authorization: 'Bearer ' + env.OPENROUTER_API_KEY };
    const [keyResponse, modelsResponse] = await Promise.all([
      base('https://openrouter.ai/api/v1/key', { headers: auth, signal: AbortSignal.timeout(15000) }),
      base('https://openrouter.ai/api/v1/models', { signal: AbortSignal.timeout(15000) }),
    ]);
    if (!keyResponse.ok || !modelsResponse.ok) throw new Error('AI budget lookup failed');
    const key = await keyResponse.json() as { data: Record<string, unknown> };
    const models = await modelsResponse.json() as { data: Array<{ id: string; pricing: Record<string, string> }> };
    const pricing = models.data.find(m => m.id === body.model)?.pricing;
    if (!pricing) throw new Error('AI model pricing unavailable');
    // Catalog minima can come from different providers: permit a bounded margin
    // so one actual endpoint can satisfy BOTH token price ceilings.
    const prompt = Number(pricing.prompt) * 2, output = Number(pricing.completion) * 2;
    if (!Number.isFinite(prompt) || !Number.isFinite(output) || prompt < 0 || output < 0
      || ['request', 'image', 'web_search', 'internal_reasoning'].some(k => Number(pricing[k] || 0) !== 0)) throw new Error('Unsupported AI pricing');
    const max = Number(body.max_tokens);
    if (!Number.isFinite(max) || max <= 0) throw new Error('AI output must be bounded');
    const amount = Math.max(.000001, ((Buffer.byteLength(init.body) + 2048) * prompt * 2 + max * output) * 1.1);
    const directory = join(env.CLODDS_STATE_DIR || env.HOME || '.', 'autonomy');
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    reserveAiSpend(join(directory, 'observer.sqlite'), amount, key.data);
    // Route only at the catalog's price ceilings, without hidden paid fallbacks.
    body.provider = { allow_fallbacks: false, max_price: { prompt: prompt * 1e6, completion: output * 1e6 } };
    return base(input, { ...init, body: JSON.stringify(body) });
  };
}
