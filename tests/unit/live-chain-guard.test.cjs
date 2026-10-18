'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {join,resolve}=require('node:path');
const {spawnSync}=require('node:child_process');
const {tmpdir}=require('node:os');
const {createHash}=require('node:crypto');
const {Interface}=require('ethers');
const {LiveLedger,planHash}=require('../../ops/autonomy/live-ledger.cjs');
const {LiveTransport,orderHash}=require('../../ops/autonomy/live-transport.cjs');
const {LiveChainGuard,domainFor,MANAGERS}=require('../../ops/autonomy/live-chain-guard.cjs');
const {COLLATERAL}=require('../../ops/autonomy/live-chain.cjs');
// Independent verified V3 ABI, not imported from the parser being tested.
const events=new Interface(require('../fixtures/polymarket-chain-events.json').abi);
const positions=new Interface(['event TransferSingle(address indexed operator,address indexed from,address indexed to,uint256 id,uint256 value)',
  'event TransferBatch(address indexed operator,address indexed from,address indexed to,uint256[] ids,uint256[] values)']);
const hash=n=>'0x'+BigInt(n).toString(16).padStart(64,'0');
function fixture(t,protocol={version:'v1',negRisk:false}) {
  const dir=fs.mkdtempSync(join(tmpdir(),'clodds-chain-guard-'));
  let now=Date.parse('2026-10-17T12:00:00Z');
  const file=join(dir,'data/cloddsbot/autonomy/live.sqlite');let ledger=new LiveLedger(file,()=>now);
  t.after(()=>{ledger.close();fs.rmSync(dir,{recursive:true,force:true});});
  const auth={address:'0x'+'1'.repeat(40),signatureType:3,apiKey:'fixture-only'},owner={kind:'human-owner',id:'fixture-owner'};
  const identity=createHash('sha256').update('polymarket:137:3:'+auth.address).digest('hex');
  const holder=createHash('sha256').update('polygon:137:'+auth.address).digest('hex');
  let cash=20.284834,inventory=[],trades=[],block=100,order={},fail=false;
  const balances=new Map([[100,'20284834']]),tokenBalances=new Map([[100,'0']]),flows=[],receipts=new Map(),calls=[];
  const point=()=>({at:new Date(now).toISOString(),holder,chainId:137,collateral:COLLATERAL,
    blockNumber:block,blockHash:hash(block),balanceUnits:balances.get(block),decimals:6});
  const account=()=>({at:new Date(now).toISOString(),identity,cash,allowance:100,orders:[],positions:inventory,
    trades,exhaustive:true,geoblock:{blocked:false,country:'ES'}});
  const chain={checkpoint:async()=>{if(fail)throw Error('Fixture RPC unavailable');return point();},
    transfers:async(anchor,current)=>{
      const found=flows.filter(f=>f.blockNumber>anchor.blockNumber&&f.blockNumber<=current.blockNumber);
      return {transfers:found,netUnits:found.reduce((n,f)=>n+BigInt(f.deltaUnits),0n).toString()};
    },positionBalance:async(token,p,current)=>{assert.equal(token,'123');assert.equal(p.version,protocol.version);return tokenBalances.get(current.blockNumber);},
    receipt:async tx=>{if(!receipts.has(tx))throw Error('Fixture receipt unavailable');return receipts.get(tx);}};
  const plan=side=>({market:hash(222),event:'event',token:'123',side,price:.2,shares:6,feeRate:.04,
    strategy:'explicit-human-test',rulesHash:'a'.repeat(64),expiresAt:new Date(now+60000).toISOString()});
  const preview=side=>({plan:plan(side),account:account(),protocol,request:{side}});
  const reader={preview:async request=>preview(request.side),account:async()=>account(),buildHeaders:()=>({}),
    get:async url=>url.endsWith('/version')?{version:2}:order};
  const sign=async p=>{
    const domain=domainFor(protocol),buy=p.plan.side==='BUY';
    const body={owner:auth.apiKey,orderType:'FOK',deferExec:false,order:{salt:now,maker:auth.address,signer:auth.address,
      tokenId:'123',makerAmount:buy?'1200000':'6000000',takerAmount:buy?'6000000':'1200000',side:p.plan.side,
      signatureType:3,timestamp:String(now),metadata:hash(0),builder:hash(0),signature:'0x00'}};
    return {body,domain,orderId:orderHash(body,domain)};
  };
  const fetch=async(url,init)=>{
    calls.push(init.method);
    assert.equal(ledger.db.prepare('SELECT state FROM live_intents ORDER BY at DESC LIMIT 1').get().state,'SENDING');
    assert.equal(ledger.get('chainAnchor').blockNumber,block);
    // Recompute using the original side; the payload is an order, not a plan.
    const p={plan:{side:JSON.parse(init.body).order.side}};
    const correct=await sign(p);
    return new Response(JSON.stringify({success:true,orderID:correct.orderId,status:'matched'}));
  };
  let guard=new LiveChainGuard(ledger,auth,{chain}),transport=new LiveTransport(ledger,reader,auth,{chainGuard:guard,sign,fetch});
  const addFlow=(delta,tx=hash(900),from=delta<0?auth.address:domainFor(protocol).exchange,to=delta<0?domainFor(protocol).exchange:auth.address)=>{
    const flow={blockNumber:block,blockHash:hash(block),transactionHash:tx,logIndex:flows.length,from,to,
      units:String(Math.abs(delta)),deltaUnits:String(delta)};flows.push(flow);return flow;
  };
  const applyFill=(result,side='BUY')=>{
    block++;now+=1000;const buy=side==='BUY',tx=hash(900+block),exchange=domainFor(protocol).exchange;
    cash=Math.round((cash+(buy?-1.2384:1.1616))*1e6)/1e6;
    balances.set(block,String(Math.round(cash*1e6)));tokenBalances.set(block,buy?'6000000':'0');
    inventory=buy?[{token:'123',market:hash(222),shares:6,cost:1.2384,bidValue:1.14}]:[];
    trades=[...trades,{id:'trade-'+block,orderId:result.orderId,token:'123',market:hash(222),side,status:'CONFIRMED',
      shares:6,price:.2,traderSide:'TAKER',transactionHash:tx}];
    order={id:result.orderId,asset_id:'123',market:hash(222),side,status:'MATCHED',size_matched:'6'};
    const logs=[];
    for(const [name,args] of [
      ['OrderFilled',[result.orderId,auth.address,exchange,buy?0:1,123,buy?1200000:6000000,buy?6000000:1200000,38400,hash(0),hash(0)]],
      ['OrdersMatched',[result.orderId,auth.address,buy?0:1,123,buy?1200000:6000000,buy?6000000:1200000]]]) {
      const event=events.encodeEventLog(events.getEvent(name),args);logs.push({address:exchange,...event});
    }
    const token=positions.encodeEventLog(positions.getEvent('TransferSingle'),[exchange,buy?exchange:auth.address,buy?auth.address:exchange,123,6000000]);
    logs.push({address:MANAGERS[protocol.version],...token});
    receipts.set(tx,{transactionHash:tx,blockNumber:block,blockHash:hash(block),logs});
    addFlow(buy?-1238400:1161600,tx);
    return {tx,receipt:receipts.get(tx)};
  };
  return {dir,file,auth,owner,chain,account,point,plan,preview,reader,protocol,calls,balances,tokenBalances,flows,receipts,
    ledger:()=>ledger,guard:()=>guard,transport:()=>transport,addFlow,applyFill,
    activate:()=>guard.activate(account(),owner),submit:side=>{const p=preview(side);return transport.submit(p,planHash(p.plan),owner);},
    advance:n=>{block=n;balances.set(n,String(Math.round(cash*1e6)));tokenBalances.set(n,inventory.length?'6000000':'0');},
    setFailure:v=>{fail=v;},setOrder:o=>{order=o;},setAccount:(a)=>{if(a.cash!==undefined)cash=a.cash;if(a.positions)inventory=a.positions;},
    restart:()=>{ledger.close();ledger=new LiveLedger(file,()=>now);guard=new LiveChainGuard(ledger,auth,{chain});transport=new LiveTransport(ledger,reader,auth,{chainGuard:guard,sign,fetch});ledger.recoverAfterRestart();}};
}

test('activation persists a fresh wallet-bound chain baseline; guarded ledger refuses missing proofs',async t=>{
  const s=fixture(t);await s.activate();
  assert.equal(s.ledger().get('chainRequired'),true);assert.equal(s.ledger().get('chainAnchor').blockNumber,100);
  assert.throws(()=>s.ledger().reserve(s.plan('BUY'),s.account(),planHash(s.plan('BUY'))),/durable anchor/);
  assert.equal(s.ledger().summary().pending,0);
});

test('external offsetting transfers stop before signing/posting and never advance the durable anchor',async t=>{
  const s=fixture(t);await s.activate();s.advance(101);s.addFlow(1000000,hash(777));s.addFlow(-1000000,hash(778));
  await assert.rejects(s.submit('BUY'),/offsetting/);
  assert.equal(s.calls.length,0);assert.equal(s.ledger().mode(),'halted');
  assert.equal(s.ledger().get('chainAnchor').blockNumber,100);assert.equal(s.ledger().summary().pending,0);
});

test('a quota failure before submission creates no order and does not reset the baseline',async t=>{
  const s=fixture(t);await s.activate();s.setFailure(true);
  await assert.rejects(s.submit('BUY'),/RPC unavailable/);
  assert.equal(s.calls.length,0);assert.equal(s.ledger().get('chainAnchor').blockNumber,100);
});

test('standard, negative-risk and Protocol V2 fills prove order identity, cash and ERC1155 ownership',async t=>{
  for(const protocol of [{version:'v1',negRisk:false},{version:'v1',negRisk:true},{version:'v2',negRisk:false}]) {
    const s=fixture(t,protocol);await s.activate();const result=await s.submit('BUY');s.applyFill(result);
    s.account().trades.push({...s.account().trades[0],id:'failed-attempt',status:'FAILED',transactionHash:hash(999)});
    assert.equal((await s.transport().reconcile(result.intent)).state,'CONFIRMED');
    assert.equal(s.ledger().get('chainAnchor').blockNumber,101);assert.equal(s.ledger().get('lastCash'),19.046434);
    assert.equal(s.ledger().summary().pending,0);assert.equal(s.ledger().summary().exposure,1.2384);
    assert.equal(s.ledger().db.prepare('SELECT shares FROM live_inventory').get().shares,6);
    assert.equal((await s.transport().reconcile(result.intent)).state,'CONFIRMED');
  }
});

test('simulated entry and exit book actual fee losses and advance cash/inventory/anchor atomically',async t=>{
  const s=fixture(t);await s.activate();const buy=await s.submit('BUY');s.applyFill(buy);
  await s.transport().reconcile(buy.intent);
  const sell=await s.submit('SELL');s.applyFill(sell,'SELL');await s.transport().reconcile(sell.intent);
  assert.equal(s.ledger().summary().exposure,0);assert.equal(s.ledger().summary().realized,-.0768);
  assert.equal(s.ledger().get('lastCash'),20.208034);assert.equal(s.ledger().get('chainAnchor').blockNumber,102);
});

test('another order, asset, wallet, exchange or missing position transfer cannot release a reservation',async t=>{
  for(const condition of ['order','token','wallet','exchange','position']) {
    const s=fixture(t);await s.activate();const result=await s.submit('BUY'),{receipt}=s.applyFill(result);
    if(condition==='position')receipt.logs.pop();
    else if(condition==='exchange')receipt.logs[0].address='0x'+'9'.repeat(40);
    else {
      const args=[condition==='order'?hash(999):result.orderId,condition==='wallet'?'0x'+'9'.repeat(40):s.auth.address,
        domainFor(s.protocol).exchange,0,condition==='token'?124:123,1200000,6000000,38400,hash(0),hash(0)];
      Object.assign(receipt.logs[0],events.encodeEventLog(events.getEvent('OrderFilled'),args));
    }
    await assert.rejects(s.transport().reconcile(result.intent),undefined,condition);
    assert.equal(s.ledger().summary().pending,1);assert.equal(s.ledger().summary().exposure,0);
    assert.equal(s.ledger().mode(),'halted');assert.equal(s.ledger().get('chainAnchor').blockNumber,100);
  }
});

test('external cashflows outside or inside the owned receipt remain unbooked even when net zero',async t=>{
  for(const sameReceipt of [false,true]) {
    const s=fixture(t);await s.activate();const result=await s.submit('BUY'),{tx}=s.applyFill(result);
    const outsider='0x'+'9'.repeat(40);
    s.addFlow(1000000,sameReceipt?tx:hash(777),outsider,s.auth.address);
    s.addFlow(-1000000,sameReceipt?tx:hash(778),s.auth.address,outsider);
    await assert.rejects(s.transport().reconcile(result.intent),/External transfer|Unattributed/);
    assert.equal(s.ledger().summary().pending,1);assert.equal(s.ledger().summary().realized,0);
    assert.equal(s.ledger().get('lastCash'),20.284834);assert.equal(s.ledger().get('chainAnchor').blockNumber,100);
  }
});

test('a no-fill FOK cancellation advances a quiet anchor without booking an execution',async t=>{
  const s=fixture(t);await s.activate();const result=await s.submit('BUY');s.advance(101);
  s.setOrder({id:result.orderId,asset_id:'123',market:hash(222),side:'BUY',status:'CANCELED',size_matched:'0'});
  assert.equal((await s.transport().reconcile(result.intent)).state,'CANCELLED');
  assert.equal(s.ledger().summary().pending,0);assert.equal(s.ledger().get('chainAnchor').blockNumber,101);
});

test('a failed inventory commit rolls back the chain anchor with the ledger transaction',async t=>{
  const s=fixture(t);await s.activate();const result=await s.submit('BUY');s.applyFill(result);
  const row=s.ledger().db.prepare('SELECT * FROM live_intents').get(),account=s.account();
  const proof=await s.guard().verifyFill(row,account,account.trades,6,1.2384);
  const badAccount={...account,positions:[{...account.positions[0],shares:5}]};
  assert.throws(()=>s.ledger().reconcileFill(result.intent,{orderId:result.orderId,shares:6,cash:1.2384,
    final:true,authenticated:true,accountReflected:true,chain:proof},badAccount),/inventory mismatch/);
  assert.equal(s.ledger().get('chainAnchor').blockNumber,100);assert.equal(s.ledger().get('lastCash'),20.284834);
  assert.equal(s.ledger().summary().pending,1);assert.equal(s.ledger().summary().exposure,0);
  const audit=s.ledger().db.prepare("SELECT detail FROM live_events WHERE kind='CHAIN_CONFIRMED'").all();
  assert(audit.every(row=>JSON.parse(row.detail).kind!=='FILL'));
});

test('restart preserves the anchor and pending risk; valid reconciliation never resends the order',async t=>{
  const s=fixture(t);await s.activate();const result=await s.submit('BUY');s.restart();
  await assert.rejects(s.activate(),/cannot be reset/);
  s.applyFill(result);
  assert.equal(s.ledger().get('chainAnchor').blockNumber,100);assert.equal(s.ledger().summary().pending,1);
  assert.equal((await s.transport().reconcile(result.intent)).state,'CONFIRMED');assert.deepEqual(s.calls,['POST']);
  assert.equal(s.ledger().summary().baseline,20.284834);
});

test('a coherent snapshot restores the anchor, protocol and uncertain reservation without replay',async t=>{
  const s=fixture(t);await s.activate();const result=await s.submit('BUY');
  s.ledger().db.prepare("UPDATE live_intents SET state='SENDING' WHERE id=?").run(result.intent);
  const dump=spawnSync('python3',[resolve(__dirname,'../../ops/autonomy/backup-observer.py'),'--root',s.dir],{encoding:'utf8'});
  assert.equal(dump.status,0,dump.stderr);
  const restored=new LiveLedger(join(s.dir,'dumps/cloddsbot/live.sqlite'),()=>Date.parse(s.account().at));
  try {
    restored.recoverAfterRestart();assert.equal(restored.mode(),'halted');
    assert.equal(restored.get('chainAnchor').blockNumber,100);
    assert.deepEqual(restored.get('protocol:'+result.intent),s.protocol);
    assert.equal(restored.summary().reserved,1.23841);
    s.applyFill(result);
    const guard=new LiveChainGuard(restored,s.auth,{chain:s.chain});
    const transport=new LiveTransport(restored,s.reader,s.auth,{chainGuard:guard,
      fetch:async()=>assert.fail('A restored reservation must never resend an order')});
    assert.equal((await transport.reconcile(result.intent)).state,'CONFIRMED');
    assert.equal(restored.get('chainAnchor').blockNumber,101);assert.equal(restored.get('lastCash'),19.046434);
    assert.equal(s.ledger().get('chainAnchor').blockNumber,100);
  }finally{restored.close();}
});

test('quiet synchronization advances an audited anchor without submitting or resuming stopped entries',async t=>{
  const s=fixture(t);await s.activate();s.ledger().halt('Owner stop');s.advance(102);
  await s.transport().sync();
  assert.equal(s.ledger().get('chainAnchor').blockNumber,102);assert.equal(s.ledger().mode(),'halted');
  assert.equal(s.ledger().summary().baseline,20.284834);assert.equal(s.ledger().get('lastCash'),20.284834);
  assert.equal(s.ledger().summary().pending,0);assert.equal(s.calls.length,0);
});

test('quiet sync cannot skip a pending submission or replace its original anchor',async t=>{
  const s=fixture(t);await s.activate();await s.submit('BUY');s.advance(102);
  await assert.rejects(s.transport().sync(),/Pending intent/);
  assert.equal(s.ledger().get('chainAnchor').blockNumber,100);assert.equal(s.ledger().summary().pending,1);
  assert.deepEqual(s.calls,['POST']);
});

test('ERC1155 batches prove the approved asset and refuse an additional unapproved position',async t=>{
  for(const extra of [false,true]) {
    const s=fixture(t,{version:'v2',negRisk:false});await s.activate();const result=await s.submit('BUY'),{receipt}=s.applyFill(result);
    const exchange=domainFor(s.protocol).exchange;
    const batch=positions.encodeEventLog(positions.getEvent('TransferBatch'),[exchange,exchange,s.auth.address,
      extra?[123,124]:[123],extra?[6000000,1]:[6000000]]);
    receipt.logs[2]={address:MANAGERS.v2,...batch};
    if(extra)await assert.rejects(s.transport().reconcile(result.intent),/Another position/);
    else assert.equal((await s.transport().reconcile(result.intent)).state,'CONFIRMED');
    assert.equal(s.ledger().summary().pending,extra?1:0);
  }
});

test('a fee destination must be proved by the expected exchange rather than a lookalike log',async t=>{
  const feeAbi=new Interface(['event FeeCharged(address indexed receiver,uint256 amount)']);
  for(const authentic of [false,true]) {
    const s=fixture(t);await s.activate();const result=await s.submit('BUY'),{receipt,tx}=s.applyFill(result);
    const receiver='0x'+'9'.repeat(40),exchange=domainFor(s.protocol).exchange;
    s.flows[0].deltaUnits='-1200000';s.flows[0].units='1200000';s.addFlow(-38400,tx,s.auth.address,receiver);
    receipt.logs.push({address:authentic?exchange:receiver,
      ...feeAbi.encodeEventLog(feeAbi.getEvent('FeeCharged'),[receiver,38400])});
    if(authentic)assert.equal((await s.transport().reconcile(result.intent)).state,'CONFIRMED');
    else await assert.rejects(s.transport().reconcile(result.intent),/Unattributed collateral/);
    assert.equal(s.ledger().summary().pending,authentic?0:1);
  }
});
