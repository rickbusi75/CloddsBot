'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {mkdtempSync,rmSync,statSync}=require('node:fs');
const {tmpdir}=require('node:os');
const {join,dirname}=require('node:path');
const {LiveLedger,STARTER,planHash}=require('../../ops/autonomy/live-ledger.cjs');

function setup(t) {
  const dir=mkdtempSync(join(tmpdir(),'clodds-live-test-'));
  assert.equal(dirname(dir),tmpdir());
  let instant=Date.parse('2026-10-17T12:00:00Z');
  const file=join(dir,'live.sqlite');
  const ledger=new LiveLedger(file,()=>instant);
  t.after(()=>{try{ledger.close();}catch{}rmSync(dir,{recursive:true,force:true});});
  const account=()=>({at:new Date(instant).toISOString(),cash:20.284834,allowance:100,
    positions:[],orders:[],trades:[],exhaustive:true,geoblock:{blocked:false,country:'ES'}});
  const plan=(extra={})=>({market:'market',event:'event',token:'token',side:'BUY',price:.2,shares:6,feeRate:.04,
    strategy:'explicit-human-test',rulesHash:'a'.repeat(64),expiresAt:new Date(instant+60000).toISOString(),...extra});
  const owner={kind:'human-owner',id:'test-owner'};
  const activate=()=>ledger.activate(account(),owner);
  const submit=(p,a=account())=>{const id=ledger.reserve(p,a,planHash(p));ledger.signed(id,'0x'+'a'.repeat(64));ledger.sending(id,a);return id;};
  return {ledger,file,account,plan,owner,activate,submit,tick:ms=>{instant+=ms;}};
}
test('live mode defaults disabled, file is private and activation is explicit',t=>{
  const s=setup(t);
  assert.equal(s.ledger.mode(),'disabled');
  assert.equal(s.ledger.db.pragma('synchronous',{simple:true}),2);
  if(process.platform!=='win32')assert.equal(statSync(s.file).mode&0o777,0o600);
  assert.throws(()=>s.ledger.activate(s.account(),{}),/owner activation/);
  const p=s.plan();assert.throws(()=>s.ledger.reserve(p,s.account(),planHash(p)),/disabled/);
  s.activate();assert.equal(s.ledger.mode(),'manual');assert.deepEqual(s.ledger.summary().limits,STARTER);
  assert.throws(()=>s.activate(),/cannot be reset/);
});
test('unknown, stale, geographically restricted or non-flat account cannot activate',t=>{
  const s=setup(t);
  for(const changes of [{cash:19},{allowance:0},{exhaustive:false},{at:'2020-01-01'},
    {geoblock:{blocked:true,country:'ES'}},{geoblock:{blocked:false,country:'FR'}},
    {positions:[{token:'token',market:'market',shares:1,cost:.2,bidValue:.2}]},
    {orders:[{id:'external',token:'token',side:'BUY',price:.2,remaining:5}]}]) {
    assert.throws(()=>s.ledger.activate({...s.account(),...changes},s.owner));
  }
  assert.equal(s.ledger.mode(),'disabled');
});
test('exact plan approval binds the price, quantity, asset, side, fees and expiry',t=>{
  const s=setup(t);s.activate();const p=s.plan(),hash=planHash(p);
  for(const change of [{price:.21},{shares:6.1},{token:'other'},{side:'SELL'},{feeRate:.05},{rulesHash:'b'.repeat(64)},{expiresAt:'2026-10-17T12:00:59Z'}]) {
    assert.notEqual(planHash({...p,...change}),hash);
    assert.throws(()=>s.ledger.reserve({...p,...change},s.account(),hash),/approval/);
  }
  assert.equal(s.ledger.summary().pending,0);
});
test('reservations include worst-case fee and survive another process connection',t=>{
  const s=setup(t);s.activate();const p=s.plan();
  const id=s.ledger.reserve(p,s.account(),planHash(p));
  const other=new LiveLedger(s.file,()=>Date.parse(s.account().at));
  try {
    assert.equal(other.summary().reserved,1.23841);
    assert.throws(()=>other.reserve(s.plan({market:'second'}),s.account(),planHash(s.plan({market:'second'}))),/reconciliation/);
    assert.throws(()=>other.reserve(p,s.account(),planHash(p)),/Duplicate/);
  }finally{other.close();}
  assert.equal(s.ledger.db.prepare('SELECT state FROM live_intents WHERE id=?').get(id).state,'RESERVED');
});
test('invalid order numbers, expiry, fee schedules and excessive size are rejected before reservation',t=>{
  const s=setup(t);s.activate();
  for(const change of [{price:NaN},{shares:Infinity},{price:0},{price:1},{shares:-1},{feeRate:.11},
    {shares:6.00001},{expiresAt:'2026-10-17T12:00:00Z'},{expiresAt:'2026-10-17T12:10:00Z'},
    {price:.3},{strategy:''}]) {
    const p=s.plan(change);assert.throws(()=>s.ledger.reserve(p,s.account(),planHash(p)));
  }
  assert.equal(s.ledger.summary().reserved,0);
});
test('no untracked account order, position or cash change is accepted',t=>{
  const s=setup(t);s.activate();const p=s.plan();
  for(const change of [{cash:20},{orders:[{id:'other',token:'token',side:'BUY',price:.2,remaining:5}]},
    {positions:[{token:'other',market:'other',shares:5,cost:1,bidValue:.9}]}]) {
    assert.throws(()=>s.ledger.reserve(p,{...s.account(),...change},planHash(p)));
  }
  assert.equal(s.ledger.summary().pending,0);
});
test('submission is recorded before transport and cannot be sent a second time',t=>{
  const s=setup(t);s.activate();const p=s.plan();const id=s.submit(p);
  assert.equal(s.ledger.db.prepare('SELECT state FROM live_intents WHERE id=?').get(id).state,'SENDING');
  assert.throws(()=>s.ledger.sending(id,s.account()),/already sent/);
  assert.throws(()=>s.ledger.reject(id,'timeout'),/uncertain submission/);
  assert.equal(s.ledger.summary().reserved,1.23841);
});
test('stop or expired plan prevents sending after signing',t=>{
  const s=setup(t);s.activate();const p=s.plan();const id=s.ledger.reserve(p,s.account(),planHash(p));
  s.ledger.signed(id,'0x'+'a'.repeat(64));s.ledger.halt();
  assert.throws(()=>s.ledger.sending(id,s.account()),/Stopped/);
  s.ledger.put('mode','manual');s.tick(61000);
  assert.throws(()=>s.ledger.sending(id,s.account()),/expired/);
});
test('restart clears unsent reservations but halts uncertain submissions without replay',t=>{
  const s=setup(t);s.activate();const p=s.plan();const id=s.ledger.reserve(p,s.account(),planHash(p));
  s.ledger.recoverAfterRestart();assert.equal(s.ledger.summary().pending,0);
  const next=s.plan({token:'next'});const nextId=s.submit(next);
  s.ledger.recoverAfterRestart();
  assert.equal(s.ledger.mode(),'halted');assert.equal(s.ledger.summary().pending,1);
  assert.equal(s.ledger.db.prepare('SELECT state FROM live_intents WHERE id=?').get(nextId).state,'UNKNOWN');
  assert.equal(s.ledger.db.prepare('SELECT state FROM live_intents WHERE id=?').get(id).state,'REJECTED');
  assert.throws(()=>s.ledger.resume(s.account(),s.owner),/Unresolved/);
});
function filledBuy(s) {
  const p=s.plan();const id=s.submit(p);
  s.ledger.acknowledge(id,'0x'+'a'.repeat(64),'FILLED');
  const after={...s.account(),cash:19.046434,positions:[{token:'token',market:'market',shares:6,cost:1.2384,bidValue:1.14}]};
  s.ledger.reconcileFill(id,{orderId:'0x'+'a'.repeat(64),shares:6,cash:1.2384,final:true,authenticated:true,accountReflected:true},after);
  return after;
}
test('buy acknowledgement alone cannot release reservation or claim a financial fill',t=>{
  const s=setup(t);s.activate();const id=s.submit(s.plan());
  s.ledger.acknowledge(id,'0x'+'a'.repeat(64),'FILLED');
  assert.equal(s.ledger.summary().pending,1);assert.equal(s.ledger.summary().exposure,0);
  assert.throws(()=>s.ledger.reconcileFill(id,{shares:6,cash:1.2},s.account()),/not confirmed/);
});
test('confirmed buy, restart, sell and realized fee-inclusive loss form one durable round trip',t=>{
  const s=setup(t);s.activate();const bought=filledBuy(s);
  assert.equal(s.ledger.summary().exposure,1.2384);assert.equal(s.ledger.summary().pending,0);
  const sell=s.plan({side:'SELL',price:.19});const id=s.ledger.reserve(sell,bought,planHash(sell));
  s.ledger.signed(id,'0x'+'b'.repeat(64));s.ledger.sending(id,bought);
  const after={...s.account(),cash:20.158034};
  s.ledger.reconcileFill(id,{orderId:'0x'+'b'.repeat(64),shares:6,cash:1.1116,final:true,authenticated:true,accountReflected:true},after);
  assert.equal(s.ledger.summary().exposure,0);assert.equal(s.ledger.summary().dailyLoss,.1268);
  assert.equal(s.ledger.summary().realized,-.1268);
  const reopened=new LiveLedger(s.file,()=>Date.parse(s.account().at));
  try{assert.equal(reopened.summary().dailyLoss,.1268);}finally{reopened.close();}
});
test('partial fill records only actual cash and net shares after remaining order is gone',t=>{
  const s=setup(t);s.activate();const id=s.submit(s.plan());
  s.ledger.acknowledge(id,'0x'+'a'.repeat(64),'PARTIAL');
  const account={...s.account(),cash:19.664834,positions:[{token:'token',market:'market',shares:3,cost:.62,bidValue:.57}],
    orders:[{id:'0x'+'a'.repeat(64),token:'token',side:'BUY',remaining:3,price:.2}]};
  const proof={orderId:'0x'+'a'.repeat(64),shares:3,cash:.62,final:true,authenticated:true,accountReflected:true};
  assert.throws(()=>s.ledger.reconcileFill(id,proof,account),/still open/);
  s.ledger.reconcileFill(id,proof,{...account,orders:[]});
  assert.equal(s.ledger.summary().exposure,.62);assert.equal(s.ledger.summary().pending,0);
});
test('mismatched fill cash or inventory rolls back completely',t=>{
  const s=setup(t);s.activate();const id=s.submit(s.plan());
  const proof={orderId:'0x'+'a'.repeat(64),shares:6,cash:1.2,final:true,authenticated:true,accountReflected:true};
  const account={...s.account(),cash:19.084834,positions:[{token:'token',market:'market',shares:5,cost:1.2,bidValue:1}]};
  assert.throws(()=>s.ledger.reconcileFill(id,proof,account),/inventory mismatch/);
  assert.equal(s.ledger.summary().exposure,0);assert.equal(s.ledger.get('lastCash'),20.284834);
  assert.equal(s.ledger.summary().reserved,1.23841);
});
test('loss and total drawdown stops block entries and do not reset on owner resume',t=>{
  const s=setup(t);s.activate();
  s.ledger.db.prepare('INSERT INTO live_realized VALUES(?,?,?)').run('loss',s.account().at,-2);
  const p=s.plan();assert.throws(()=>s.ledger.reserve(p,s.account(),planHash(p)),/Loss stop/);
  s.ledger.halt();assert.throws(()=>s.ledger.resume(s.account(),s.owner),/loss stop/);
  s.ledger.db.prepare('DELETE FROM live_realized').run();s.ledger.put('mode','manual');s.ledger.put('lastCash',18.284834);
  assert.throws(()=>s.ledger.reserve(p,{...s.account(),cash:18.284834},planHash(p)),/Loss stop/);
});
test('exposure and same-event constraints include actual fee-inclusive cost',t=>{
  const s=setup(t);s.activate();const account=filledBuy(s);
  const p=s.plan({token:'second'});
  assert.throws(()=>s.ledger.reserve(p,account,planHash(p)),/Correlated/);
  s.ledger.db.prepare('UPDATE live_inventory SET cost=4.5').run();
  const independent=s.plan({token:'second',event:'another'});
  assert.throws(()=>s.ledger.reserve(independent,account,planHash(independent)),/exposure limit/);
});
test('an explicit risk-reducing exit remains possible after an owner halt',t=>{
  const s=setup(t);s.activate();const account=filledBuy(s);s.ledger.halt();
  const p=s.plan({side:'SELL',price:.19});const id=s.ledger.reserve(p,account,planHash(p));
  s.ledger.signed(id,'0x'+'b'.repeat(64));s.ledger.sending(id,account);
  assert.equal(s.ledger.db.prepare('SELECT state FROM live_intents WHERE id=?').get(id).state,'SENDING');
});
test('worst-case outstanding loss is reserved before a second independent entry',t=>{
  const s=setup(t);s.activate();const account=filledBuy(s);
  const p=s.plan({token:'other',event:'independent'});
  assert.throws(()=>s.ledger.reserve(p,account,planHash(p)),/Remaining daily loss budget/);
  assert.equal(s.ledger.summary().pending,0);
});
test('fees deducted from received shares do not create fictitious inventory',t=>{
  const s=setup(t);s.activate();const id=s.submit(s.plan());
  const account={...s.account(),cash:19.084834,positions:[{token:'token',market:'market',shares:5.808,cost:1.2,bidValue:1.10352}]};
  s.ledger.reconcileFill(id,{orderId:'0x'+'a'.repeat(64),shares:5.808,cash:1.2,final:true,authenticated:true,accountReflected:true},account);
  assert.equal(s.ledger.db.prepare('SELECT shares FROM live_inventory').get().shares,5.808);
  const sell=s.plan({side:'SELL',price:.19,shares:6});
  assert.throws(()=>s.ledger.reserve(sell,account,planHash(sell)),/owned inventory/);
});
test('zero-fill cancellation cannot discard a fill that already reached the account',t=>{
  const s=setup(t);s.activate();const id=s.submit(s.plan());
  s.ledger.acknowledge(id,'0x'+'a'.repeat(64),'OPEN');s.ledger.cancelRequested(id);
  const account={...s.account(),trades:[{orderId:'0x'+'a'.repeat(64),status:'CONFIRMED'}]};
  assert.throws(()=>s.ledger.confirmUnfilledCancellation(id,{orderId:'0x'+'a'.repeat(64),status:'CANCELED',sizeMatched:0,authenticated:true},account),/raced/);
  assert.equal(s.ledger.summary().pending,1);
});
