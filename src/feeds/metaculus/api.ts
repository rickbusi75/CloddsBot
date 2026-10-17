/** Authenticated, read-only access to the current Metaculus posts API. */
export interface MetaculusPost {
  id: number;
  title: string;
  status: string;
  resolved?: boolean;
  created_at: string;
  scheduled_close_time?: string;
  forecasts_count?: number;
  question?: {
    id: number;
    type: string;
    description?: string;
    resolution?: string | number | null;
    aggregations?: Record<string, { latest?: { centers?: number[] } | null }>;
  };
}

const BASE = 'https://www.metaculus.com/api/';

export async function metaculusGet<T>(path: string): Promise<T> {
  const token = process.env.METACULUS_TOKEN;
  if (!token) throw new Error('Configure METACULUS_TOKEN in Environment Setup.');
  const url = new URL(path, BASE);
  // Never send the credential to a pagination URL outside the Metaculus API.
  if (url.origin !== new URL(BASE).origin || !url.pathname.startsWith('/api/')) {
    throw new Error('Invalid Metaculus API URL');
  }
  const response = await fetch(url, {
    headers: { Accept: 'application/json', Authorization: `Token ${token}` },
    redirect: 'error', signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) throw new Error(`Metaculus API error: ${response.status}`);
  return response.json() as Promise<T>;
}

export function metaculusSummary(post: MetaculusPost) {
  const q = post.question;
  const aggregation = q?.aggregations?.recency_weighted ?? q?.aggregations?.unweighted;
  const center = aggregation?.latest?.centers?.[0];
  const probability = q?.type === 'binary' && typeof center === 'number'
    && Number.isFinite(center) && center >= 0 && center <= 1 ? center : null;
  return {
    id: post.id, question_id: q?.id, title: post.title, status: post.status,
    probability, probability_available: probability !== null,
    predictions: post.forecasts_count ?? 0,
    url: `https://www.metaculus.com/questions/${post.id}/`,
  };
}

export async function metaculusSearch(query: string, status = 'open', limit = 20, project?: number) {
  const safeLimit = Math.max(1, Math.min(100, Number.isFinite(limit) ? Math.floor(limit) : 20));
  const params = new URLSearchParams({ search: query, statuses: status,
    forecast_type: 'binary', with_cp: 'true', limit: String(safeLimit) });
  if (project !== undefined) params.set('project', String(project));
  const data = await metaculusGet<{ results: MetaculusPost[] }>(`posts/?${params}`);
  return data.results.filter(p => p.question?.type === 'binary').slice(0, safeLimit);
}

export function metaculusPost(id: string) {
  if (!/^\d+$/.test(id)) throw new Error('Invalid Metaculus post ID');
  return metaculusGet<MetaculusPost>(`posts/${id}/?with_cp=true`);
}
