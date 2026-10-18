'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {ChainReader,ENDPOINTS,HISTORY_ENDPOINTS,COLLATERAL,TRANSFER}=require('../../ops/autonomy/live-chain.cjs');
const wallet='0x'+'1'.repeat(40),other='0x'+'2'.repeat(40),topic=a=>'0x'+a.slice(2).padStart(64,'0');
const hex=n=>'0x'+BigInt(n).toString(16),word=n=>'0x'+BigInt(n).toString(16).padStart(64,'0');
const hash=n=>'0x'+Number(n).toString(16).padStart(64,'0');
const NOW=Date.parse('2026-10-17T15:00:00Z');
function fixture() {
  let head=100,now=NOW,mutate=v=>v;const balances=new Map([[100,20284834n],[101,20284834n]]),logs=[],calls=[];
  const block=n=>({number:hex(n),hash:hash(n),timestamp:hex(NOW/1000-5)});
  const fetch=async(endpoint,init)=>{
    const request=JSON.parse(init.body),{method,params,id}=request;
    calls.push({endpoint,method,params});assert.equal(init.redirect,'error');
    let result;
    if(method==='eth_chainId')result='0x89';
    else if(method==='eth_getBlockByNumber')result=block(params[0]==='finalized'?head:Number(BigInt(params[0])));
    else if(method==='eth_call')result=params[0].data==='0x313ce567'?word(6):word(balances.get(Number(BigInt(params[1])))??20284834n);
    else if(method==='eth_getCode')result='0x6000';
    else if(method==='eth_getLogs') {
      const filter=params[0];assert.equal(filter.address,COLLATERAL);
      result=logs.filter(l=>Number(BigInt(l.blockNumber))>=Number(BigInt(filter.fromBlock))&&Number(BigInt(l.blockNumber))<=Number(BigInt(filter.toBlock))
        &&filter.topics.every((t,i)=>t===null||t===l.topics[i]));
    } else if(method==='eth_getTransactionReceipt') {
      const included=logs.filter(l=>l.transactionHash===params[0]);
      result=included.length?{status:'0x1',transactionHash:params[0],blockNumber:included[0].blockNumber,
        blockHash:included[0].blockHash,from:other,to:COLLATERAL,logs:included}:null;
    } else throw Error('Unexpected fixture RPC method');
    const body=mutate({jsonrpc:'2.0',id,result},endpoint,method,params);
    return new Response(JSON.stringify(body));
  };
  const reader=new ChainReader(wallet,{fetch,now:()=>now,minIntervalMs:0});
  const add=(from,to,units,index=0,transaction=900)=>{
    const log={address:COLLATERAL,topics:[TRANSFER,topic(from),topic(to)],data:word(units),removed:false,
      blockNumber:'0x65',blockHash:hash(101),transactionHash:hash(transaction),logIndex:hex(index)};
    logs.push(log);return log;
  };
  return {reader,calls,logs,balances,add,head:n=>{head=n;},now:n=>{now=n;},mutate:f=>{mutate=f;}};
}

test('chain checkpoint agrees on finalized block, six decimals and exact collateral units',async()=>{
  const s=fixture(),p=await s.reader.checkpoint();
  assert.equal(p.chainId,137);assert.equal(p.balanceUnits,'20284834');assert.equal(p.blockHash,hash(100));
  assert.equal(p.decimals,6);assert.equal(Object.hasOwn(p,'wallet'),false);
  assert.equal(new Set(s.calls.map(c=>c.endpoint)).size,2);
});

test('chain reader cannot broadcast, change endpoints, or authenticate with private credentials',async()=>{
  const s=fixture();
  await assert.rejects(s.reader.rpc(ENDPOINTS[0],'eth_sendRawTransaction',['0x00']),/read-only/);
  await assert.rejects(s.reader.rpc('https://example.com','eth_call',[]),/read-only/);
  assert.throws(()=>new ChainReader(wallet,{endpoints:[ENDPOINTS[0],ENDPOINTS[0]]}),/Two allowlisted/);
  assert.equal(s.calls.length,0);
});

test('wrong chain, divergent balance, missing code and stale finalized head fail closed',async()=>{
  for(const condition of ['chain','balance','code','stale','precision','rpcid']) {
    const s=fixture();
    s.mutate((r,e,m)=>{
      if(condition==='chain'&&m==='eth_chainId')r.result='0x1';
      if(condition==='balance'&&e===ENDPOINTS[1]&&m==='eth_call')r.result=word(7);
      if(condition==='code'&&m==='eth_getCode')r.result='0x';
      if(condition==='stale'&&m==='eth_getBlockByNumber')r.result.timestamp=hex(NOW/1000-181);
      if(condition==='precision'&&m==='eth_call')r.result=word(18);
      if(condition==='rpcid')r.id++;
      return r;
    });
    await assert.rejects(s.reader.checkpoint(),undefined,condition);
  }
});

test('balanced deposit and withdrawal remain visible even when the account cash is unchanged',async()=>{
  const s=fixture(),before=await s.reader.checkpoint();s.head(101);
  s.add(other,wallet,1000000n,0,900);s.add(wallet,other,1000000n,1,901);
  const after=await s.reader.checkpoint(),activity=await s.reader.transfers(before,after);
  assert.equal(activity.netUnits,'0');assert.equal(activity.transfers.length,2);
  assert.deepEqual(activity.transfers.map(t=>t.deltaUnits),['1000000','-1000000']);
  assert.equal(s.calls.filter(c=>c.method==='eth_getTransactionReceipt').length,4);
});

test('incoming, outgoing and self-transfers keep integer precision and match observed balances',async()=>{
  const s=fixture(),before=await s.reader.checkpoint();s.head(101);
  s.add(other,wallet,2000000n,0,900);s.add(wallet,other,1238400n,1,901);s.add(wallet,wallet,5n,2,902);
  s.balances.set(101,21046434n);
  const activity=await s.reader.transfers(before,await s.reader.checkpoint());
  assert.equal(activity.netUnits,'761600');assert.equal(activity.transfers.length,3);
  assert.equal(activity.transfers[2].deltaUnits,'0');
});

test('divergent, removed, duplicated, unrelated or balance-incomplete transfer history is rejected',async()=>{
  for(const condition of ['divergent','removed','duplicate','unrelated','balance']) {
    const s=fixture(),before=await s.reader.checkpoint();s.head(101);
    const log=s.add(other,wallet,1000000n);s.balances.set(101,21284834n);
    s.mutate((r,e,m)=>{
      if(m==='eth_getLogs'&&r.result.length) {
        if(condition==='divergent'&&e!==ENDPOINTS[0])r.result=[];
        if(condition==='removed')r.result=r.result.map(l=>({...l,removed:true}));
        if(condition==='duplicate')r.result=[...r.result,...r.result];
        if(condition==='unrelated')r.result=r.result.map(l=>({...l,topics:[TRANSFER,topic(other),topic(other)]}));
      }
      return r;
    });
    if(condition==='balance')s.balances.set(101,22284834n);
    await assert.rejects(s.reader.transfers(before,await s.reader.checkpoint()),undefined,condition);
  }
});

test('missing, failed, divergent and noncanonical receipts never verify a transfer',async()=>{
  for(const condition of ['missing','failed','divergent','canonical']) {
    const s=fixture();s.head(101);s.add(other,wallet,1000000n);
    const p=await s.reader.checkpoint();
    s.mutate((r,e,m)=>{
      if(m==='eth_getTransactionReceipt') {
        if(condition==='missing')r.result=null;
        if(condition==='failed')r.result.status='0x0';
        if(condition==='divergent'&&e!==ENDPOINTS[0])r.result.from=wallet;
        if(condition==='canonical')r.result.blockHash=hash(999);
      }
      return r;
    });
    await assert.rejects(s.reader.receipt(hash(900),p),undefined,condition);
  }
});

test('forged balances, wrong holders, unfinalized checkpoints and overlong ranges are refused',async()=>{
  const s=fixture(),before=await s.reader.checkpoint();s.head(101);
  const after=await s.reader.checkpoint();
  await assert.rejects(s.reader.transfers({...before,balanceUnits:'1'},after),/balance not verified/);
  await assert.rejects(s.reader.transfers({...before,holder:'wrong'},after),/checkpoint/);
  await assert.rejects(s.reader.transfers(before,{...after,blockNumber:102,blockHash:hash(102)}),/finality/);
  await assert.rejects(s.reader.transfers(before,{...after,blockNumber:70000}),/capacity/);
});

test('RPC errors, oversized bodies and unknown schemas are never interpreted as empty history',async()=>{
  for(const body of [{jsonrpc:'2.0',id:1,error:{code:-32005}},'x'.repeat(2000001),null]) {
    const s=new ChainReader(wallet,{fetch:async()=>new Response(typeof body==='string'?body:JSON.stringify(body))});
    await assert.rejects(s.checkpoint());
  }
  const unavailable=new ChainReader(wallet,{fetch:async()=>new Response('Do not expose upstream body',{status:403})});
  await assert.rejects(unavailable.checkpoint(),error=>error.httpStatus===403&&error.rpcMethod==='eth_chainId'
    &&!error.message.includes('upstream body'));
});

test('bounded historical evidence can outlast account preflight but stale observations are refused',async()=>{
  const s=fixture(),before=await s.reader.checkpoint();s.head(101);
  const after=await s.reader.checkpoint();s.now(NOW+20000);
  assert.equal((await s.reader.transfers(before,after)).netUnits,'0');
  s.now(NOW+120001);
  await assert.rejects(s.reader.transfers(before,after),/Stale chain checkpoint/);
});

test('chunk boundaries have no gaps and every historical log/receipt uses two history providers',async()=>{
  const s=fixture(),before=await s.reader.checkpoint();s.head(4199);
  const incoming=s.add(other,wallet,1000000n,0,900),outgoing=s.add(wallet,other,1000000n,1,901);
  Object.assign(incoming,{blockNumber:hex(4196),blockHash:hash(4196)});
  Object.assign(outgoing,{blockNumber:hex(4197),blockHash:hash(4197)});
  const activity=await s.reader.transfers(before,await s.reader.checkpoint());
  assert.equal(activity.transfers.length,2);assert.equal(activity.netUnits,'0');
  const logCalls=s.calls.filter(c=>c.method==='eth_getLogs');
  assert.equal(logCalls.length,8);
  assert.deepEqual([...new Set(logCalls.map(c=>[c.params[0].fromBlock,c.params[0].toBlock].join(':')))],
    [hex(101)+':'+hex(4196),hex(4197)+':'+hex(4199)]);
  for(const method of ['eth_getLogs','eth_getTransactionReceipt'])assert.deepEqual(
    new Set(s.calls.filter(c=>c.method===method).map(c=>c.endpoint)),new Set(HISTORY_ENDPOINTS));
});

test('CTF and Protocol V2 owned balances are independently read at the canonical checkpoint',async()=>{
  const s=fixture(),point=await s.reader.checkpoint();
  s.mutate((r,e,m,p)=>{if(m==='eth_call'&&p[0].data.startsWith('0x00fdd58e'))r.result=word(6000000);return r;});
  assert.equal(await s.reader.positionBalance('123',{version:'v1'},point),'6000000');
  assert.equal(await s.reader.positionBalance('123',{version:'v2'},point),'6000000');
  const calls=s.calls.filter(c=>c.method==='eth_call'&&c.params[0].data.startsWith('0x00fdd58e'));
  assert.equal(calls.length,4);assert.equal(new Set(calls.map(c=>c.params[0].to)).size,2);
  assert(calls.every(c=>c.params[0].data.endsWith(BigInt(123).toString(16).padStart(64,'0'))));
});

test('unknown position protocols, invalid IDs and changed canonical blocks are refused',async()=>{
  const s=fixture(),point=await s.reader.checkpoint();const count=s.calls.length;
  for(const token of ['-1','1e8',(1n<<256n).toString()])await assert.rejects(s.reader.positionBalance(token,{version:'v1'},point),/position ID/);
  await assert.rejects(s.reader.positionBalance('123',{version:'unknown'},point),/protocol/);
  assert.equal(s.calls.length,count);
  s.mutate((r,e,m)=>{if(m==='eth_getBlockByNumber')r.result.hash=hash(999);return r;});
  await assert.rejects(s.reader.positionBalance('123',{version:'v1'},point),/checkpoint changed/);
});
