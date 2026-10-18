'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {mkdtempSync,rmSync}=require('node:fs');
const {tmpdir}=require('node:os');
const {join,dirname}=require('node:path');
const {LiveLedger,planHash}=require('../../ops/autonomy/live-ledger.cjs');
const {LiveTransport,orderHash}=require('../../ops/autonomy/live-transport.cjs');
function fixture(t,reply) {
  const dir=mkdtempSync(join(tmpdir(),'clodds-live-transport-'));assert.equal(dirname(dir),tmpdir());
  const now=Date.parse('2026-10-17T12:00:00Z');
  const ledger=new LiveLedger(join(dir,'live.sqlite'),()=>now);
  t.after(()=>{ledger.close();rmSync(dir,{recursive:true,force:true});});
  const auth={address:'0x'+'1'.repeat(40),apiKey:'unit-test-only',signatureType:3};
  const owner={kind:'human-owner',id:'test-owner'};
  let account={at:new Date(now).toISOString(),identity:'c'.repeat(64),cash:20.284834,allowance:100,geoblock:{blocked:false,country:'ES'},
    positions:[],orders:[],trades:[],exhaustive:true};
  const plan={market:'0x'+'2'.repeat(64),event:'event',token:'123',side:'BUY',price:.2,shares:6,feeRate:.04,
    strategy:'explicit-human-test',rulesHash:'a'.repeat(64),expiresAt:new Date(now+60000).toISOString()};
  const preview={plan,request:{gammaId:'market',token:'123',side:'BUY',price:.2,shares:6},protocol:{version:'v1',negRisk:false}};
  const domain={exchange:'0xE111180000d2663C0091e4f400237545B87B996B',version:'2'};
  const sign=async fresh=>{
    const order={salt:1,maker:auth.address,signer:auth.address,tokenId:plan.token,makerAmount:'1200000',takerAmount:'6000000',
      side:'BUY',signatureType:3,timestamp:String(now),metadata:'0x'+'0'.repeat(64),builder:'0x'+'0'.repeat(64),signature:'0x00'};
    const body={owner:auth.apiKey,orderType:'FOK',deferExec:false,order};
    return {body,domain,orderId:orderHash(body,domain)};
  };
  let order={};
  const reader={preview:async()=>({...preview,plan:{...plan},account}),account:async()=>account,
    get:async url=>url.endsWith('/version')?{version:2}:order,buildHeaders:()=>({'X-Test':'unit-only'})};
  const calls=[];
  const fetch=async(url,init)=>{
    calls.push({url,init});assert.equal(url,'https://clob.polymarket.com/order');assert.equal(init.redirect,'error');
    const current=ledger.db.prepare('SELECT state FROM live_intents WHERE id=?').get(planHash(plan));
    assert.equal(current.state,init.method==='POST'?'SENDING':'CANCEL_PENDING');
    if(reply)return reply(JSON.parse(init.body),init);
    const signed=await sign(preview);
    return new Response(JSON.stringify({success:true,orderID:signed.orderId,status:'matched'}));
  };
  // This suite isolates HTTP/signing/account economics. The real chain guard
  // and its durable ledger integration have their own network-free suite.
  const chainGuard={quiet:async source=>({account:typeof source==='function'?await source():source}),
    verifyFill:async()=>null,activate:async(a,o)=>ledger.activate(a,o),resume:async(a,o)=>ledger.resume(a,o)};
  const transport=new LiveTransport(ledger,reader,auth,{fetch,sign,chainGuard});
  ledger.activate(account,owner);
  return {ledger,reader,transport,preview,owner,calls,sign,plan,
    setAccount:a=>{account={...account,...a};},setOrder:o=>{order=o;},account:()=>account};
}
test('transport requires owner and exact plan before any signing or posting',async t=>{
  const s=fixture(t);
  await assert.rejects(s.transport.submit(s.preview,planHash(s.plan),{kind:'human-owner',id:'intruder'}),/owner/);
  await assert.rejects(s.transport.submit(s.preview,'wrong',s.owner),/approval/);
  assert.equal(s.calls.length,0);assert.equal(s.ledger.summary().pending,0);
});
test('transport records reservation and sends once, then waits for verified account reconciliation',async t=>{
  const s=fixture(t);const result=await s.transport.submit(s.preview,planHash(s.plan),s.owner);
  assert.equal(result.state,'AWAITING_RECONCILIATION');assert.equal(s.calls.length,1);
  assert.equal(s.ledger.summary().pending,1);assert.equal(s.ledger.summary().exposure,0);
  assert.equal(s.ledger.summary().reserved,1.23841);
  await assert.rejects(s.transport.submit(s.preview,planHash(s.plan),s.owner),/Duplicate/);
  assert.equal(s.calls.length,1);
});
test('timeout has no automatic retry and preserves unknown exposure across restart',async t=>{
  const s=fixture(t,async()=>{throw Error('Lost response after possible acceptance');});
  const result=await s.transport.submit(s.preview,planHash(s.plan),s.owner);
  assert.equal(result.state,'UNKNOWN');assert.equal(s.calls.length,1);assert.equal(s.ledger.mode(),'halted');
  s.ledger.recoverAfterRestart();assert.equal(s.ledger.summary().pending,1);
  await assert.rejects(s.transport.submit(s.preview,planHash(s.plan),s.owner),/disabled/);
  assert.equal(s.calls.length,1);
});
test('an explicit exchange rejection can release the reservation; duplicate or 503 cannot',async t=>{
  const rejected=fixture(t,async()=>new Response(JSON.stringify({success:false,errorMsg:'not enough balance / allowance'}),{status:400}));
  assert.equal((await rejected.transport.submit(rejected.preview,planHash(rejected.plan),rejected.owner)).state,'REJECTED');
  assert.equal(rejected.ledger.summary().pending,0);
  const duplicate=fixture(t,async()=>new Response(JSON.stringify({success:false,errorMsg:'order already exists'}),{status:400}));
  assert.equal((await duplicate.transport.submit(duplicate.preview,planHash(duplicate.plan),duplicate.owner)).state,'UNKNOWN');
  const unavailable=fixture(t,async()=>new Response('{}',{status:503}));
  assert.equal((await unavailable.transport.submit(unavailable.preview,planHash(unavailable.plan),unavailable.owner)).state,'UNKNOWN');
});
test('changed rules, fees or exchange format invalidate approval without a submission',async t=>{
  const s=fixture(t);
  s.reader.preview=async()=>({...s.preview,account:s.account(),plan:{...s.plan,feeRate:.05}});
  await assert.rejects(s.transport.submit(s.preview,planHash(s.plan),s.owner),/changed/);
  s.reader.preview=async()=>({...s.preview,account:s.account(),plan:{...s.plan}});
  s.reader.get=async()=>({version:1});
  await assert.rejects(s.transport.submit(s.preview,planHash(s.plan),s.owner),/format/);
  assert.equal(s.calls.length,0);
});
test('signed payload exceeding the approved amount is rejected locally',async t=>{
  const s=fixture(t);const original=s.sign;
  s.transport.sign=async fresh=>{const signed=await original(fresh);signed.body.order.makerAmount='2000000';signed.orderId=orderHash(signed.body,signed.domain);return signed;};
  await assert.rejects(s.transport.submit(s.preview,planHash(s.plan),s.owner),/preflight/);
  assert.equal(s.calls.length,0);assert.equal(s.ledger.summary().pending,0);
});
test('a mismatched exchange acknowledgement halts rather than trusting an unrelated order ID',async t=>{
  const s=fixture(t,async()=>new Response(JSON.stringify({success:true,status:'matched',orderID:'0x'+'b'.repeat(64)})));
  assert.equal((await s.transport.submit(s.preview,planHash(s.plan),s.owner)).state,'UNKNOWN');
  assert.equal(s.ledger.mode(),'halted');
});
test('a confirmed trade and reflected balances are required before booking an actual fill',async t=>{
  const s=fixture(t);const r=await s.transport.submit(s.preview,planHash(s.plan),s.owner);
  const trade={id:'trade',orderId:r.orderId,token:s.plan.token,side:'BUY',status:'MINED',shares:6,price:.2,
    market:s.plan.market,traderSide:'TAKER',transactionHash:'0x'+'c'.repeat(64)};
  s.setOrder({id:r.orderId,asset_id:s.plan.token,market:s.plan.market,side:'BUY',status:'MATCHED',size_matched:'6'});
  s.setAccount({trades:[trade]});
  assert.equal((await s.transport.reconcile(r.intent)).state,'AWAITING_CONFIRMATION');
  s.setAccount({trades:[{...trade,status:'CONFIRMED'}]});
  assert.equal((await s.transport.reconcile(r.intent)).state,'AWAITING_ACCOUNT_BALANCES');
  s.setAccount({cash:19.046434,positions:[{token:s.plan.token,market:s.plan.market,shares:6,cost:1.2384,bidValue:1.14}]});
  assert.equal((await s.transport.reconcile(r.intent)).state,'CONFIRMED');
  assert.equal(s.ledger.summary().exposure,1.2384);assert.equal(s.ledger.summary().pending,0);
  assert.equal((await s.transport.reconcile(r.intent)).state,'CONFIRMED');
});
test('cancellation acknowledgement alone never releases risk; final no-fill proof does',async t=>{
  const s=fixture(t);const r=await s.transport.submit(s.preview,planHash(s.plan),s.owner);
  s.transport.fetch=async(url,init)=>{
    assert.equal(init.method,'DELETE');assert.equal(JSON.parse(init.body).orderID,r.orderId);
    return new Response(JSON.stringify({canceled:[r.orderId]}));
  };
  assert.equal((await s.transport.cancel(r.intent,s.owner)).state,'CANCEL_PENDING_RECONCILIATION');
  assert.equal(s.ledger.summary().pending,1);
  s.setOrder({id:r.orderId,asset_id:s.plan.token,market:s.plan.market,side:'BUY',status:'CANCELED',size_matched:'0'});
  assert.equal((await s.transport.reconcile(r.intent)).state,'CANCELLED');
  assert.equal(s.ledger.summary().pending,0);
});

test('a FOK automatically cancelled without a fill releases risk only after authenticated final evidence',async t=>{
  const s=fixture(t);const r=await s.transport.submit(s.preview,planHash(s.plan),s.owner);
  s.setOrder({id:r.orderId,asset_id:s.plan.token,market:s.plan.market,side:'BUY',status:'CANCELED',size_matched:'0'});
  s.setAccount({trades:[{id:'failed-attempt',orderId:r.orderId,token:s.plan.token,side:'BUY',status:'FAILED',shares:6,price:.2}]});
  assert.equal((await s.transport.reconcile(r.intent)).state,'CANCELLED');
  assert.equal(s.calls.length,1);assert.equal(s.ledger.summary().pending,0);
  s.ledger.checkAccount(s.account());
});
test('fill arriving during cancellation is reconciled as a fill, never discarded',async t=>{
  const s=fixture(t);const r=await s.transport.submit(s.preview,planHash(s.plan),s.owner);
  s.ledger.cancelRequested(r.intent);
  s.setOrder({id:r.orderId,asset_id:s.plan.token,market:s.plan.market,side:'BUY',status:'MATCHED',size_matched:'6'});
  s.setAccount({cash:19.046434,positions:[{token:s.plan.token,market:s.plan.market,shares:6,cost:1.2384,bidValue:1.14}],
    trades:[{id:'trade',orderId:r.orderId,token:s.plan.token,side:'BUY',status:'CONFIRMED',shares:6,price:.2,
      market:s.plan.market,traderSide:'TAKER',transactionHash:'0x'+'c'.repeat(64)}]});
  assert.equal((await s.transport.reconcile(r.intent)).state,'CONFIRMED');
  assert.equal(s.ledger.summary().exposure,1.2384);
});

test('a simultaneous withdrawal cannot be booked as extra cost within the reservation',async t=>{
  const s=fixture(t);const r=await s.transport.submit(s.preview,planHash(s.plan),s.owner);
  s.setOrder({id:r.orderId,asset_id:s.plan.token,market:s.plan.market,side:'BUY',status:'MATCHED',size_matched:'6'});
  // Better execution costs 1.10 plus fees, but a simultaneous withdrawal makes
  // the observed delta 1.20: below the reservation, yet unrelated to this trade.
  s.setAccount({cash:19.084834,positions:[{token:s.plan.token,market:s.plan.market,shares:6,cost:1.2,bidValue:1.14}],
    trades:[{id:'trade',orderId:r.orderId,token:s.plan.token,side:'BUY',status:'CONFIRMED',shares:6,price:1.1/6,
      market:s.plan.market,traderSide:'TAKER',transactionHash:'0x'+'c'.repeat(64)}]});
  await assert.rejects(s.transport.reconcile(r.intent),/economics mismatch/);
  assert.equal(s.ledger.mode(),'halted');assert.equal(s.ledger.summary().pending,1);
  assert.equal(s.ledger.summary().exposure,0);
});

test('unrelated completed trades cannot hide behind a flat position and unchanged cash',async t=>{
  const s=fixture(t);const r=await s.transport.submit(s.preview,planHash(s.plan),s.owner);
  s.setOrder({id:r.orderId,asset_id:s.plan.token,market:s.plan.market,side:'BUY',status:'MATCHED',size_matched:'6'});
  s.setAccount({trades:[{id:'external',orderId:'external-order',token:'other',side:'BUY',status:'CONFIRMED',shares:1,price:.2}]});
  await assert.rejects(s.transport.reconcile(r.intent),/Untracked account trade/);
  assert.equal(s.ledger.summary().pending,1);assert.equal(s.ledger.summary().realized,0);
});

test('fees withheld from received shares reconcile without rounding away residual holdings',async t=>{
  const s=fixture(t);const r=await s.transport.submit(s.preview,planHash(s.plan),s.owner);
  s.setOrder({id:r.orderId,asset_id:s.plan.token,market:s.plan.market,side:'BUY',status:'MATCHED',size_matched:'6'});
  s.setAccount({cash:19.084834,positions:[{token:s.plan.token,market:s.plan.market,shares:5.808,cost:1.2,bidValue:1.1}],
    trades:[{id:'trade',orderId:r.orderId,token:s.plan.token,side:'BUY',status:'CONFIRMED',shares:6,price:.2,
      market:s.plan.market,traderSide:'TAKER',transactionHash:'0x'+'c'.repeat(64)}]});
  assert.equal((await s.transport.reconcile(r.intent)).state,'CONFIRMED');
  assert.equal(s.ledger.db.prepare('SELECT shares FROM live_inventory').get().shares,5.808);
  s.ledger.checkAccount(s.account());
});

test('buy and exit reconciliation books only trade proceeds and refuses a simultaneous deposit as profit',async t=>{
  const s=fixture(t);const r=await s.transport.submit(s.preview,planHash(s.plan),s.owner);
  const buy={id:'buy',orderId:r.orderId,token:s.plan.token,side:'BUY',status:'CONFIRMED',shares:6,price:.2,
    market:s.plan.market,traderSide:'TAKER',transactionHash:'0x'+'c'.repeat(64)};
  s.setOrder({id:r.orderId,asset_id:s.plan.token,market:s.plan.market,side:'BUY',status:'MATCHED',size_matched:'6'});
  s.setAccount({cash:19.046434,positions:[{token:s.plan.token,market:s.plan.market,shares:6,cost:1.2384,bidValue:1.14}],trades:[buy]});
  await s.transport.reconcile(r.intent);
  const sellPlan={...s.plan,side:'SELL',price:.19};
  const id=s.ledger.reserve(sellPlan,s.account(),planHash(sellPlan)),orderId='0x'+'b'.repeat(64);
  s.ledger.signed(id,orderId);s.ledger.sending(id,s.account());s.ledger.acknowledge(id,orderId,'FILLED');
  s.setOrder({id:orderId,asset_id:s.plan.token,market:s.plan.market,side:'SELL',status:'MATCHED',size_matched:'6'});
  const sell={...buy,id:'sell',orderId,side:'SELL',price:.19};
  // 1.14 gross minus fee rounded to five decimals: 1.10306.
  s.setAccount({cash:20.649494,positions:[],trades:[buy,sell]});
  await assert.rejects(s.transport.reconcile(id),/economics mismatch/);
  assert.equal(s.ledger.summary().realized,0);assert.equal(s.ledger.summary().pending,1);
  s.setAccount({cash:20.149494});
  assert.equal((await s.transport.reconcile(id)).state,'CONFIRMED');
  assert.equal(s.ledger.summary().realized,-.13534);assert.equal(s.ledger.summary().dailyLoss,.13534);
  assert.equal(s.ledger.summary().exposure,0);s.ledger.checkAccount(s.account());
  assert.equal(s.ledger.mode(),'halted');
});
