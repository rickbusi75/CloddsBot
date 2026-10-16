import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createAgentClient, getAiProvider, resolveRequestModel, listOpenRouterModels } from '../../src/providers/agent-client';

const env = () => ({ CLODDS_AI_PROVIDER: 'openrouter', OPENROUTER_API_KEY: 'local-mock-key', CLODDS_MODEL: 'vendor/full-model:variant' });
const reply = (content: unknown[] = [{ type: 'text', text: 'ok' }]) => ({
  id: 'msg_local', type: 'message', role: 'assistant', model: 'vendor/full-model:variant', content,
  stop_reason: 'end_turn', stop_sequence: null, usage: { input_tokens: 1, output_tokens: 1 },
});

test('OpenRouter preserves full model IDs and uses the selected model for auxiliary tasks', () => {
  assert.equal(resolveRequestModel('vendor/model:free', env()), 'vendor/model:free');
  assert.equal(resolveRequestModel('~vendor/model-latest', env()), '~vendor/model-latest');
  assert.equal(resolveRequestModel('claude-hardcoded-aux', env()), env().CLODDS_MODEL);
  assert.equal(resolveRequestModel('anthropic/claude-test', { CLODDS_AI_PROVIDER: 'anthropic' }), 'claude-test');
  assert.throws(() => getAiProvider({ CLODDS_AI_PROVIDER: 'unknown' }));
  assert.throws(() => resolveRequestModel('unqualified', { CLODDS_AI_PROVIDER: 'openrouter' }));
});

test('no credential is required to construct the client, but a request fails before network access', async () => {
  let calls = 0;
  const client = createAgentClient({ CLODDS_AI_PROVIDER: 'openrouter' }, (async () => { calls++; throw new Error('unexpected network'); }) as typeof fetch);
  await assert.rejects(async () => client.messages.create({ model: 'vendor/test', max_tokens: 10, messages: [] }), /OPENROUTER_API_KEY/);
  assert.equal(calls, 0);
});

test('native Messages request retains tools, images, tool results and Bearer authentication', async () => {
  const seen: Array<{ url: string; headers: Headers; body: any }> = [];
  const mockFetch = (async (url, options) => {
    seen.push({ url: String(url), headers: new Headers(options?.headers), body: JSON.parse(String(options?.body)) });
    return Response.json(reply([{ type: 'tool_use', id: 'call_test', name: 'lookup', input: { symbol: 'BTC' } }]));
  }) as typeof fetch;
  const client = createAgentClient({ ...env(), ANTHROPIC_API_KEY: 'unused-mock-anthropic' }, mockFetch);
  const tools = [{ name: 'lookup', description: 'Market lookup', input_schema: { type: 'object' as const, properties: { symbol: { type: 'string' } } } }];
  const messages: any[] = [{ role: 'user', content: [{ type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'mock' } }, { type: 'text', text: 'lookup' }] }];
  const result = await client.messages.create({ model: env().CLODDS_MODEL, max_tokens: 10, tools, messages });
  assert.equal(seen[0].url, 'https://openrouter.ai/api/v1/messages');
  assert.equal(seen[0].headers.get('authorization'), 'Bearer local-mock-key');
  assert.equal(seen[0].headers.get('x-api-key'), null);
  assert.deepEqual(seen[0].body.tools, tools);
  assert.deepEqual(seen[0].body.messages, messages);
  assert.equal(seen[0].body.model, env().CLODDS_MODEL);
  messages.push({ role: 'assistant', content: result.content }, { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'call_test', content: '123' }] });
  await client.messages.create({ model: env().CLODDS_MODEL, max_tokens: 10, tools, messages });
  assert.deepEqual(seen[1].body.messages, messages);
});

test('changing the provider or key updates the next request without an implicit fallback', async () => {
  const mutable: NodeJS.ProcessEnv = env();
  const headers: Headers[] = [];
  const mockFetch = (async (_url, options) => { headers.push(new Headers(options?.headers)); return Response.json(reply()); }) as typeof fetch;
  const client = createAgentClient(mutable, mockFetch);
  const params = { model: 'vendor/model', max_tokens: 10, messages: [] };
  await client.messages.create(params);
  mutable.OPENROUTER_API_KEY = 'rotated-local-mock-key';
  await client.messages.create(params);
  assert.equal(headers[1].get('authorization'), 'Bearer rotated-local-mock-key');
  mutable.CLODDS_AI_PROVIDER = 'anthropic'; mutable.ANTHROPIC_API_KEY = 'direct-local-mock-key';
  await client.messages.create({ ...params, model: 'anthropic/claude-test' });
  assert.equal(headers[2].get('x-api-key'), 'direct-local-mock-key');
  assert.equal(headers[2].get('authorization'), null);
});

test('SDK streaming emits incremental text and returns the final message', async () => {
  const events = [
    ['message_start', { type: 'message_start', message: reply([]) }],
    ['content_block_start', { type: 'content_block_start', index: 0, content_block: { type: 'text', text: '' } }],
    ['content_block_delta', { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: 'Bon' } }],
    ['content_block_delta', { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: 'jour' } }],
    ['content_block_stop', { type: 'content_block_stop', index: 0 }],
    ['message_delta', { type: 'message_delta', delta: { stop_reason: 'end_turn', stop_sequence: null }, usage: { output_tokens: 2 } }],
    ['message_stop', { type: 'message_stop' }],
  ];
  const mockFetch = (async (_url, options) => {
    assert.equal(JSON.parse(String(options?.body)).stream, true);
    return new Response(events.map(([name, data]) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`).join(''), { headers: { 'Content-Type': 'text/event-stream' } });
  }) as typeof fetch;
  const stream = createAgentClient(env(), mockFetch).messages.stream({ model: env().CLODDS_MODEL, max_tokens: 10, messages: [] });
  const chunks: string[] = []; stream.on('text', text => chunks.push(text));
  const final = await stream.finalMessage();
  assert.deepEqual(chunks, ['Bon', 'jour']);
  assert.deepEqual(final.content, [{ type: 'text', text: 'Bonjour' }]);
});

test('model catalog retains IDs and exposes tool capability without filtering choices', async () => {
  const mockFetch = (async () => Response.json({ data: [{ id: 'vendor/tool-model', name: 'Tool model', supported_parameters: ['tools'] }, { id: 'vendor/chat-model', name: 'Chat model' }] })) as typeof fetch;
  const models = await listOpenRouterModels(mockFetch);
  assert.equal(models.length, 2);
  assert.equal(models.find(m => m.id === 'vendor/tool-model')?.tools, true);
  assert.equal(models.find(m => m.id === 'vendor/chat-model')?.tools, false);
});
