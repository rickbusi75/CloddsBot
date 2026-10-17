/** Metaculus read-only forecasting feed. */
import { EventEmitter } from 'events';
import { Market } from '../../types';
import { metaculusPost, metaculusSearch, metaculusSummary, MetaculusPost } from './api';

export interface MetaculusFeed extends EventEmitter {
  connect(): Promise<void>;
  disconnect(): void;
  searchMarkets(query: string): Promise<Market[]>;
  getMarket(id: string): Promise<Market | null>;
  getTournaments(): Promise<Array<{ id: number; name: string; questionCount: number }>>;
  getTournamentQuestions(id: number, options?: { maxResults?: number; status?: string }): Promise<Market[]>;
}

function convert(post: MetaculusPost): Market {
  const summary = metaculusSummary(post);
  const probability = summary.probability;
  const resolution = post.question?.resolution;
  return {
    id: String(post.id), slug: String(post.id), platform: 'metaculus', question: post.title,
    description: post.question?.description,
    // Unavailable community forecasts must never become invented 50% prices.
    outcomes: probability === null ? [] : [
      { id: `${post.id}-yes`, name: 'Yes', price: probability, volume24h: 0 },
      { id: `${post.id}-no`, name: 'No', price: 1 - probability, volume24h: 0 },
    ],
    volume24h: summary.predictions, liquidity: 0,
    endDate: post.scheduled_close_time ? new Date(post.scheduled_close_time) : undefined,
    resolved: post.resolved === true,
    resolutionValue: resolution === 'yes' ? 1 : resolution === 'no' ? 0 : undefined,
    tags: [], url: summary.url, createdAt: new Date(post.created_at), updatedAt: new Date(),
  };
}

export async function createMetaculusFeed(): Promise<MetaculusFeed> {
  const feed = new EventEmitter() as MetaculusFeed;
  feed.connect = async () => {
    if (!process.env.METACULUS_TOKEN) throw new Error('Configure METACULUS_TOKEN in Environment Setup.');
  };
  feed.disconnect = () => {};
  feed.searchMarkets = async query => (await metaculusSearch(query)).map(convert);
  feed.getMarket = async id => {
    try { return convert(await metaculusPost(id)); }
    catch (err) {
      if ((err as Error).message === 'Metaculus API error: 404') return null;
      throw err;
    }
  };
  feed.getTournaments = async () => {
    throw new Error('Metaculus retired its tournaments API. Browse https://www.metaculus.com/tournaments/');
  };
  feed.getTournamentQuestions = async (id, options) => {
    if (!Number.isSafeInteger(id) || id <= 0) throw new Error('Invalid project ID');
    return (await metaculusSearch('', options?.status ?? 'open', options?.maxResults ?? 20, id)).map(convert);
  };
  return feed;
}
