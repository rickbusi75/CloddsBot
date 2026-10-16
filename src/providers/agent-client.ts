import Anthropic from '@anthropic-ai/sdk';

export type AiProvider = 'anthropic' | 'openrouter';

export function getAiProvider(env: NodeJS.ProcessEnv = process.env): AiProvider {
  const explicit = env.CLODDS_AI_PROVIDER?.trim();
  if (explicit && explicit !== 'anthropic' && explicit !== 'openrouter') {
    throw new Error('CLODDS_AI_PROVIDER must be anthropic or openrouter');
  }
  return explicit as AiProvider || (env.OPENROUTER_API_KEY && !env.ANTHROPIC_API_KEY ? 'openrouter' : 'anthropic');
}

export function hasAiCredential(env: NodeJS.ProcessEnv = process.env): boolean {
  return Boolean(getAiProvider(env) === 'openrouter' ? env.OPENROUTER_API_KEY : env.ANTHROPIC_API_KEY);
}

export function resolveRequestModel(model: string, env: NodeJS.ProcessEnv = process.env): string {
  if (getAiProvider(env) === 'anthropic') return model.replace(/^anthropic\//, '');
  // Auxiliary tasks use the selected model when their built-in Claude ID is unqualified.
  const selected = model.includes('/') ? model : env.CLODDS_MODEL;
  if (!selected || !/^[\w.:-]+\/[\w./:-]+$/.test(selected)) {
    throw new Error('Choose an OpenRouter model using its full provider/model ID in AI settings.');
  }
  return selected;
}

/** Native Messages API preserves tool calls, images and SDK streaming without translation. */
export function createAgentClient(env: NodeJS.ProcessEnv = process.env, fetchImpl?: typeof fetch): Anthropic {
  let cached: Anthropic | undefined;
  let signature = '';
  const resolveClient = () => {
    const provider = getAiProvider(env);
    const key = provider === 'openrouter' ? env.OPENROUTER_API_KEY : env.ANTHROPIC_API_KEY;
    if (!key) throw new Error(`AI not configured: set ${provider === 'openrouter' ? 'OPENROUTER_API_KEY' : 'ANTHROPIC_API_KEY'} in settings.`);
    const nextSignature = provider + ':' + key;
    if (!cached || signature !== nextSignature) {
      cached = new Anthropic({
        apiKey: provider === 'openrouter' ? null : key,
        authToken: provider === 'openrouter' ? key : null,
        baseURL: provider === 'openrouter' ? 'https://openrouter.ai/api' : 'https://api.anthropic.com',
        ...(fetchImpl ? { fetch: fetchImpl } : {}),
      });
      signature = nextSignature;
    }
    return cached;
  };
  return new Proxy({} as Anthropic, {
    get(_target, property) {
      const client = resolveClient();
      if (property === 'messages') {
        return new Proxy(client.messages, {
          get(resource, method) {
            const value = Reflect.get(resource, method, resource);
            if (method === 'create' || method === 'stream') {
              return (params: Anthropic.MessageCreateParams, ...args: unknown[]) =>
                value.call(resource, { ...params, model: resolveRequestModel(params.model, env) }, ...args);
            }
            return typeof value === 'function' ? value.bind(resource) : value;
          },
        });
      }
      const value = Reflect.get(client, property, client);
      return typeof value === 'function' ? value.bind(client) : value;
    },
  });
}

export async function listOpenRouterModels(fetchImpl: typeof fetch = fetch) {
  const response = await fetchImpl('https://openrouter.ai/api/v1/models', { signal: AbortSignal.timeout(15_000) });
  if (!response.ok) throw new Error(`OpenRouter model catalog unavailable (HTTP ${response.status})`);
  const body = await response.json() as { data?: Array<{ id: string; name: string; supported_parameters?: string[] }> };
  if (!Array.isArray(body.data)) throw new Error('Invalid OpenRouter model catalog');
  return body.data.filter(m => typeof m.id === 'string' && typeof m.name === 'string').map(m => ({
    id: m.id, name: m.name, tools: m.supported_parameters?.includes('tools') ?? false,
  })).sort((a, b) => a.id.localeCompare(b.id));
}
