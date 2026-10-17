#!/usr/bin/env node
'use strict';
// This entry point has no mutation, signing or live-order transport.
const {readFileSync}=require('node:fs');
const {parse}=require('dotenv');
const {PolymarketReader}=require('./live-reader.cjs');
async function main() {
  const env=parse(readFileSync('/app/.env'));
  const auth={address:env.POLY_FUNDER_ADDRESS,privateKey:env.POLY_PRIVATE_KEY,apiKey:env.POLY_API_KEY,
    apiSecret:env.POLY_API_SECRET,apiPassphrase:env.POLY_API_PASSPHRASE,signatureType:Number(env.POLY_SIGNATURE_TYPE)};
  const reader=new PolymarketReader(auth);
  const account=await reader.account();
  console.log(JSON.stringify({checkedAt:new Date().toISOString(),mode:'READ_ONLY_PREFLIGHT',
    collateral:account.cash,openPositions:account.positions.length,openOrders:account.orders.length,
    trades:account.trades.length,exposureAtCost:account.positions.reduce((n,p)=>n+p.cost,0),
    exhaustive:account.exhaustive,geography:{blocked:account.geoblock.blocked,country:account.geoblock.country},
    ordersSubmitted:0,liveEnabled:false}));
}
if(require.main===module)main().catch(()=>{console.error('Live preflight failed; account state must be treated as unknown.');process.exitCode=1;});
module.exports={main};
