import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { Wallet, TypedDataEncoder, concat, hexlify, toUtf8Bytes, toBeHex } from 'ethers';
import { buildSignedOrderV2, SignatureType, CTF_EXCHANGE_V2, POSITION_EXCHANGE, getPolymarketSigningDomain } from '../../src/utils/polymarket-order-signer';
import { buildPolymarketHeadersForUrl, buildPolymarketHmacSignature } from '../../src/utils/polymarket-auth';

const key = '0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80';
const funder = '0x2222222222222222222222222222222222222222';
const params = { tokenId: '12345678901234567890', price: 0.5, size: 5, side: 'buy' as const };
const orderFields = [
  ['salt','uint256'], ['maker','address'], ['signer','address'], ['tokenId','uint256'],
  ['makerAmount','uint256'], ['takerAmount','uint256'], ['side','uint8'], ['signatureType','uint8'],
  ['timestamp','uint256'], ['metadata','bytes32'], ['builder','bytes32'],
].map(([name,type]) => ({ name,type }));
const nestedFields = [
  ['contents','Order'], ['name','string'], ['version','string'], ['chainId','uint256'],
  ['verifyingContract','address'], ['salt','bytes32'],
].map(([name,type]) => ({ name,type }));
const zero = '0x' + '00'.repeat(32);

for (const signingDomain of [
  { exchange: CTF_EXCHANGE_V2, version: '2' as const },
  { exchange: POSITION_EXCHANGE, version: '3' as const },
]) for (const side of ['buy','sell'] as const) {
  test(`Deposit Wallet ${side} domain ${signingDomain.version} matches independent ethers ERC-7739 signature`, async () => {
    const result = buildSignedOrderV2({ ...params, side, signingDomain }, { privateKey: key, funderAddress: funder, signatureType: SignatureType.POLY_1271 });
    assert.equal(result.order.maker, funder);
    assert.equal(result.order.signer, funder);
    const contents = { ...result.order, side: side === 'buy' ? 0 : 1 };
    const domain = { name: 'Polymarket CTF Exchange', version: signingDomain.version, chainId: 137, verifyingContract: signingDomain.exchange };
    const inner = await new Wallet(key).signTypedData(domain, { Order: orderFields, TypedDataSign: nestedFields }, {
      contents, name: 'DepositWallet', version: '1', chainId: 137, verifyingContract: funder, salt: zero,
    });
    const description = new TypedDataEncoder({ Order: orderFields }).encodeType('Order');
    const expected = concat([
      inner, TypedDataEncoder.hashDomain(domain), TypedDataEncoder.hashStruct('Order', { Order: orderFields }, contents),
      hexlify(toUtf8Bytes(description)), toBeHex(toUtf8Bytes(description).length, 2),
    ]);
    assert.equal(result.order.signature, expected);
    assert.equal(result.order.signatureType, 3);
  });
}

test('Deposit Wallet refuses a missing/invalid funder and malformed bytes32 metadata', () => {
  for (const funderAddress of [undefined, 'not-an-address']) {
    assert.throws(() => buildSignedOrderV2(params, { privateKey: key, funderAddress, signatureType: SignatureType.POLY_1271 }));
  }
  assert.throws(() => buildSignedOrderV2({ ...params, metadata: '0x1234' }, { privateKey: key }));
});

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });
test('market protocol selects the correct signing domain; unknown or unavailable contexts fail closed', async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ version: 'v2', neg_risk: true }));
  assert.deepEqual(await getPolymarketSigningDomain(params.tokenId), { exchange: POSITION_EXCHANGE, version: '3' });
  globalThis.fetch = async () => new Response(JSON.stringify({ neg_risk: false }));
  assert.deepEqual(await getPolymarketSigningDomain(params.tokenId), { exchange: CTF_EXCHANGE_V2, version: '2' });
  globalThis.fetch = async () => new Response(JSON.stringify({ version: 'v99' }));
  await assert.rejects(getPolymarketSigningDomain(params.tokenId));
  globalThis.fetch = async () => new Response('', { status: 503 });
  await assert.rejects(getPolymarketSigningDomain(params.tokenId));
});

test('L2 auth uses the owner signer, excludes query parameters and preserves exact body signing', () => {
  const auth = { address: funder, privateKey: key, apiKey: 'test-key', apiSecret: Buffer.from('test-secret').toString('base64'), apiPassphrase: 'test-passphrase' };
  const headers = buildPolymarketHeadersForUrl(auth, 'GET', 'https://clob.polymarket.com/balance-allowance?asset_type=COLLATERAL&signature_type=3', undefined, 123);
  assert.equal(headers.POLY_ADDRESS, new Wallet(key).address);
  assert.equal(headers.POLY_SIGNATURE, buildPolymarketHmacSignature(auth.apiSecret, '123', 'GET', '/balance-allowance'));
  const body = { order: { size: '5' } };
  const post = buildPolymarketHeadersForUrl(auth, 'POST', 'https://clob.polymarket.com/order', body, 123);
  assert.equal(post.POLY_SIGNATURE, buildPolymarketHmacSignature(auth.apiSecret, '123', 'POST', '/order', JSON.stringify(body)));
});
