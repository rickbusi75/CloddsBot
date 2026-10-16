import type { Platform, PlatformCredentials } from '../types.js';

/** Resolve settings at call time. Never include raw credentials in model prompts. */
export function getEnvironmentTradingCredentials(env: NodeJS.ProcessEnv = process.env): Map<Platform, PlatformCredentials> {
  const result = new Map<Platform, PlatformCredentials>();
  if (env.POLY_API_KEY && env.POLY_API_SECRET && env.POLY_API_PASSPHRASE && env.POLY_PRIVATE_KEY && env.POLY_FUNDER_ADDRESS) {
    const signatureType = Number(env.POLY_SIGNATURE_TYPE || 0);
    if ([0, 1, 2, 3].includes(signatureType)) result.set('polymarket', { platform: 'polymarket', data: {
      apiKey: env.POLY_API_KEY, apiSecret: env.POLY_API_SECRET, apiPassphrase: env.POLY_API_PASSPHRASE,
      privateKey: env.POLY_PRIVATE_KEY, funderAddress: env.POLY_FUNDER_ADDRESS, signatureType,
    } });
  }
  if (env.HYPERLIQUID_WALLET && env.HYPERLIQUID_PRIVATE_KEY) result.set('hyperliquid', { platform: 'hyperliquid', data: {
    walletAddress: env.HYPERLIQUID_WALLET, privateKey: env.HYPERLIQUID_PRIVATE_KEY,
  } });
  return result;
}
