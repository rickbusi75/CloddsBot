'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {PolymarketReader}=require('../../ops/autonomy/live-reader.cjs');
const NOW=Date.parse('2026-10-17T12:00:00Z');
const auth={address:'0x'+'1'.repeat(40),signatureType:3};
const condition='0x'+'2'.repeat(64);
const book={asset_id:'token',version:'v2',neg_risk:false,timestamp:String(NOW),min_order_size:'5',tick_size:'.01',
  asks:[{price:'.2',size:'100'}],bids:[{price:'.19',size:'100'}]};
function reader(overrides={},clock=()=>NOW) {
  const calls=[];
  const fetch=async(url,init)=>{
    const u=new URL(url);calls.push({url,init});
    assert.equal(init.method,'GET');assert.equal(init.redirect,'error');
    const defaults={
      '/balance-allowance':{balance:'20284834',allowances:{exchange:'100000000'}},
      '/api/geoblock':{blocked:false,country:'ES'},
      '/data/orders':{data:[],next_cursor:'LTE='},'/data/trades':{data:[],next_cursor:'LTE='},
      '/positions':[], '/book':book,
      '/markets/market':{id:'market',active:true,closed:false,acceptingOrders:true,enableOrderBook:true,version:'v2',
        conditionId:condition,outcomes:'["Yes","No"]',positionIds:['token','no-token'],clobTokenIds:'["legacy","legacy-no"]',question:'Test',
        description:'A detailed example resolution rule used only in isolated tests.',endDate:'2026-10-18T12:00:00Z'},
      ['/clob-markets/'+condition]:{fd:{r:.04,e:1}}
    };
    const data=Object.hasOwn(overrides,u.pathname)?overrides[u.pathname]:defaults[u.pathname];
    if(data===undefined)throw Error('Unexpected test endpoint');
    return data instanceof Response?data:new Response(JSON.stringify(typeof data==='function'?data(u):data));
  };
  return {client:new PolymarketReader(auth,{fetch,now:clock,buildHeaders:()=>({'X-Test-Auth':'test-only'})}),calls};
}
test('account preflight is exhaustive, authenticated, read-only and does not infer allowance',async()=>{
  const s=reader();const account=await s.client.account();
  assert.equal(account.cash,20.284834);assert.equal(account.allowance,100);
  assert.equal(account.positions.length,0);assert.equal(account.orders.length,0);assert(account.exhaustive);
  for(const call of s.calls) {
    if(new URL(call.url).hostname==='clob.polymarket.com')assert.equal(call.init.headers['X-Test-Auth'],'test-only');
    else assert.deepEqual(call.init.headers,{});
  }
  const broken=reader({'/balance-allowance':{balance:'20284834'}});
  await assert.rejects(broken.client.account(),/allowance/);
});
test('endpoint or redirect policy prevents leaking authentication',async()=>{
  const s=reader();
  for(const url of ['http://clob.polymarket.com/data/orders','https://evil.test/data/orders','https://a@clob.polymarket.com/data/orders']) {
    await assert.rejects(s.client.get(url,true),/endpoint/);
  }
  await assert.rejects(s.client.get('https://gamma-api.polymarket.com/markets',true),/mismatch/);
  assert.equal(s.calls.length,0);
});
test('pagination checks every page and refuses ambiguous completion or a cursor loop',async()=>{
  const s=reader({'/data/orders':u=>u.searchParams.get('next_cursor')==='MA=='?
    {data:[{id:'one'}],next_cursor:'next'}:{data:[{id:'two'}],next_cursor:'LTE='}});
  assert.deepEqual((await s.client.pages('/data/orders')).map(r=>r.id),['one','two']);
  await assert.rejects(reader({'/data/orders':{data:[],next_cursor:'MA=='}}).client.pages('/data/orders'),/loop/);
  await assert.rejects(reader({'/data/orders':{data:[]}}).client.pages('/data/orders'),/pagination/);
  await assert.rejects(reader({'/data/orders':[]}).client.pages('/data/orders'),/completion cursor/);
});
test('API outages, invalid schemas and missing geoblock do not become an empty account',async()=>{
  for(const overrides of [{'/data/orders':new Response('',{status:503})},{'/positions':{error:'unknown'}},
    {'/api/geoblock':{}},{'/api/geoblock':{blocked:true,country:'ES'}},
    {'/balance-allowance':{balance:null,allowance:'10000000'}}]) {
    await assert.rejects(reader(overrides).client.account());
  }
});

test('confirmed taker trades retain transaction identity; maker attribution and duplicate trades fail closed',async()=>{
  const trade={id:'trade',taker_order_id:'order',market:condition,asset_id:'token',side:'BUY',size:'6',price:'.2',
    status:'TRADE_STATUS_CONFIRMED',trader_side:'TAKER',transaction_hash:'0x'+'c'.repeat(64)};
  const result=await reader({'/data/trades':{data:[trade],next_cursor:'LTE='}}).client.account();
  assert.equal(result.trades[0].status,'CONFIRMED');assert.equal(result.trades[0].orderId,'order');
  assert.equal(result.trades[0].transactionHash,trade.transaction_hash);
  for(const t of [{...trade,trader_side:'MAKER',maker_orders:[{order_id:'owned-maker'}]},
    {...trade,transaction_hash:null},{...trade,side:'unknown'}]) {
    await assert.rejects(reader({'/data/trades':{data:[t],next_cursor:'LTE='}}).client.account());
  }
  await assert.rejects(reader({'/data/trades':{data:[trade,trade],next_cursor:'LTE='}}).client.account(),/duplicate trade/);
});
test('stale or insufficient-depth position marks block account valuation',async()=>{
  const p={asset:'token',conditionId:condition,size:6,avgPrice:.2};
  const s=reader({'/positions':[p]});const a=await s.client.account();assert.equal(a.positions[0].bidValue,1.1400000000000001);
  await assert.rejects(reader({'/positions':[p],'/book':{...book,timestamp:'1'}}).client.account(),/Stale/);
  await assert.rejects(reader({'/positions':[p],'/book':{...book,bids:[{price:'.19',size:'1'}]}}).client.account(),/depth/);
});
test('a slow account snapshot becomes stale before it can authorize risk',async()=>{
  let calls=0;
  const s=reader({},()=>NOW+(calls++>0?20000:0));
  await assert.rejects(s.client.account(),/Stale/);
});
test('preview validates V2 token identity, fees, tick and executable size with no signing or posting',async()=>{
  const s=reader();const preview=await s.client.preview({gammaId:'market',token:'token',side:'BUY',price:.2,shares:6});
  assert.equal(preview.mode,'PREVIEW_ONLY');assert.equal(preview.ordersSubmitted,0);
  assert.equal(preview.plan.feeRate,.04);assert.match(preview.approval,/^[a-f0-9]{64}$/);
  assert.equal(preview.plan.market,condition);
  assert.equal(preview.protocol.version,'v2');
  assert(s.calls.every(c=>c.init.method==='GET'));
  await assert.rejects(s.client.preview({gammaId:'market',token:'legacy',side:'BUY',price:.2,shares:6}),/asset protocol/);
  await assert.rejects(reader({'/book':{...book,version:'v1'}}).client.preview({gammaId:'market',token:'token',side:'BUY',price:.2,shares:6}),/signing protocol/);
});
test('preview rejects missing fee schedule, thin depth, excessive budget and unowned sales',async()=>{
  const request={gammaId:'market',token:'token',side:'BUY',price:.2,shares:6};
  await assert.rejects(reader({['/clob-markets/'+condition]:{}}).client.preview(request),/fee rate/);
  await assert.rejects(reader({'/book':{...book,asks:[{price:'.2',size:'1'}]}}).client.preview(request),/depth/);
  await assert.rejects(reader().client.preview({...request,shares:8}),/order limit/);
  await assert.rejects(reader().client.preview({...request,side:'SELL',price:.19}),/owned shares/);
});
