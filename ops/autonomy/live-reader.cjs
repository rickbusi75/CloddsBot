'use strict';

const {createHash}=require('node:crypto');
const {validateAccount,validatePlan,worstCost,STARTER,planHash}=require('./live-ledger.cjs');
const HOSTS=new Set(['clob.polymarket.com','gamma-api.polymarket.com','data-api.polymarket.com','polymarket.com']);
const number=(value,label,low=0,high=Infinity)=>{
  if(value==null || value==='' || typeof value==='boolean') throw Error('Missing '+label);
  const n=Number(value);
  if(!Number.isFinite(n)||n<low||n>high)throw Error('Invalid '+label);
  return n;
};
const array=v=>typeof v==='string'?JSON.parse(v):v;
class PolymarketReader {
  constructor(auth,{fetch=global.fetch,now=()=>Date.now(),buildHeaders}={}) {
    this.auth=auth;this.fetch=fetch;this.now=now;
    this.buildHeaders=buildHeaders||((...args)=>require('../../dist/utils/polymarket-auth').buildPolymarketHeadersForUrl(...args));
  }
  async get(url,authenticated=false) {
    const parsed=new URL(url);
    if(parsed.protocol!=='https:'||!HOSTS.has(parsed.hostname)||parsed.username||parsed.password||parsed.port)throw Error('Unexpected endpoint');
    if(authenticated && parsed.hostname!=='clob.polymarket.com')throw Error('Authentication endpoint mismatch');
    const response=await this.fetch(url,{method:'GET',headers:authenticated?this.buildHeaders(this.auth,'GET',url):{},
      redirect:'error',signal:AbortSignal.timeout(15000)});
    if(!response.ok)throw Error('Polymarket read rejected: HTTP '+response.status);
    const body=await response.text();
    if(body.length>2000000)throw Error('Response too large');
    try{return JSON.parse(body);}catch{throw Error('Invalid Polymarket response');}
  }
  async pages(path) {
    const rows=[],seen=new Set();let cursor='MA==';
    for(let page=0;page<10;page++) {
      if(seen.has(cursor))throw Error('Pagination loop');seen.add(cursor);
      const data=await this.get('https://clob.polymarket.com'+path+'?next_cursor='+encodeURIComponent(cursor),true);
      if(Array.isArray(data)) {
        if(data.length>=500)throw Error('Incomplete legacy collection');
        return rows.concat(data);
      }
      if(!data||!Array.isArray(data.data)||typeof data.next_cursor!=='string')throw Error('Incomplete account pagination');
      rows.push(...data.data);
      if(data.next_cursor==='LTE=')return rows;
      cursor=data.next_cursor;
    }
    throw Error('Account pagination exceeds supported capacity');
  }
  async positions() {
    const rows=[];
    for(let page=0;page<10;page++) {
      const query=new URLSearchParams({user:this.auth.address,sizeThreshold:'0',limit:'500',offset:String(page*500)});
      const data=await this.get('https://data-api.polymarket.com/positions?'+query);
      if(!Array.isArray(data))throw Error('Unknown position schema');
      rows.push(...data);
      if(data.length<500)return rows;
    }
    throw Error('Incomplete account positions');
  }
  async book(token) {
    const book=await this.get('https://clob.polymarket.com/book?token_id='+encodeURIComponent(token));
    if(String(book.asset_id)!==token)throw Error('Wrong asset book');
    const age=this.now()-number(book.timestamp,'book timestamp');
    if(age>15000||age < -2000)throw Error('Stale book');
    if(!Array.isArray(book.asks)||!Array.isArray(book.bids))throw Error('Unknown book schema');
    const levels=side=>book[side].map(l=>({price:number(l.price,'level price',.000001,.999999),size:number(l.size,'level size',.000001)}));
    return {...book,asks:levels('asks').sort((a,b)=>a.price-b.price),bids:levels('bids').sort((a,b)=>b.price-a.price),
      min_order_size:number(book.min_order_size,'minimum order',.000001),tick_size:number(book.tick_size,'tick',.000001,.1)};
  }
  async account() {
    const at=new Date(this.now()).toISOString();
    if(!/^0x[0-9a-fA-F]{40}$/.test(this.auth.address)||![0,1,2,3].includes(this.auth.signatureType))throw Error('Account identity missing');
    const [balance,geoblock,orders,trades,positions]=await Promise.all([
      this.get('https://clob.polymarket.com/balance-allowance?asset_type=COLLATERAL&signature_type='+this.auth.signatureType,true),
      this.get('https://polymarket.com/api/geoblock'),this.pages('/data/orders'),this.pages('/data/trades'),this.positions()]);
    if(positions.length>10)throw Error('Starter position capacity exceeded');
    const allowanceValues=balance.allowances&&typeof balance.allowances==='object'?Object.values(balance.allowances):[balance.allowance];
    if(!allowanceValues.length)throw Error('Collateral allowance unknown');
    const normalizedPositions=await Promise.all(positions.map(async p=>{
      if(typeof p.asset!=='string'||!p.asset||typeof p.conditionId!=='string'||!p.conditionId)throw Error('Unknown position identity');
      const shares=number(p.size,'position size'),avg=number(p.avgPrice,'position average',0,1);
      const book=await this.book(p.asset);
      if(!book.bids.length)throw Error('Position valuation unavailable');
      let remaining=shares,bidValue=0;
      for(const l of book.bids){const q=Math.min(remaining,l.size);bidValue+=q*l.price;remaining-=q;if(remaining<.000001)break;}
      if(remaining>.000001)throw Error('Insufficient depth to value position');
      return {token:p.asset,market:p.conditionId,shares,cost:shares*avg,bidValue};
    }));
    const normalizedOrders=orders.map(o=>{
      if(typeof o.id!=='string'||typeof o.asset_id!=='string'||!['BUY','SELL'].includes(o.side))throw Error('Unknown open order');
      const size=number(o.original_size,'order size'),matched=number(o.size_matched,'matched shares',0,size);
      return {id:o.id,token:o.asset_id,side:o.side,remaining:size-matched,price:number(o.price,'order price',.000001,.999999)};
    });
    const normalizedTrades=trades.map(t=>{
      if(typeof t.id!=='string'||typeof t.taker_order_id!=='string'||!['MATCHED','MINED','CONFIRMED','RETRYING','FAILED'].includes(t.status))throw Error('Unknown trade schema');
      return {id:t.id,orderId:t.taker_order_id,status:t.status,token:String(t.asset_id),side:t.side,
        shares:number(t.size,'trade size'),price:number(t.price,'trade price',0,1)};
    });
    return validateAccount({at,cash:number(balance.balance,'collateral')/1e6,
      allowance:Math.max(...allowanceValues.map(v=>number(v,'allowance')))/1e6,
      geoblock,orders:normalizedOrders,trades:normalizedTrades,positions:normalizedPositions,exhaustive:true},this.now());
  }
  async preview(request) {
    const market=await this.get('https://gamma-api.polymarket.com/markets/'+encodeURIComponent(request.gammaId));
    if(String(market.id)!==request.gammaId||market.active!==true||market.closed!==false
      ||market.acceptingOrders!==true||market.enableOrderBook!==true)throw Error('Market not tradeable');
    if(typeof market.question!=='string'||typeof market.description!=='string'||market.description.length<40
      ||!Number.isFinite(Date.parse(market.endDate))||Date.parse(market.endDate)<=this.now())throw Error('Resolution rules or cutoff unavailable');
    const outcomes=array(market.outcomes);
    if(!Array.isArray(outcomes)||outcomes.length!==2||new Set(outcomes).size!==2)throw Error('Binary market required');
    const ids=market.version==='v2'?market.positionIds:market.version==='v1'||market.version==null?array(market.clobTokenIds):null;
    if(!Array.isArray(ids)||ids.length!==2||new Set(ids).size!==2||!ids.includes(request.token))throw Error('Unsupported asset protocol');
    if(!/^0x[0-9a-fA-F]{64}$/.test(market.conditionId))throw Error('Market condition missing');
    const [book,info,account]=await Promise.all([this.book(request.token),
      this.get('https://clob.polymarket.com/clob-markets/'+market.conditionId),this.account()]);
    const bookVersion=book.version||'v1',marketVersion=market.version||'v1';
    if(bookVersion!==marketVersion||!['v1','v2'].includes(bookVersion)
      ||(bookVersion==='v1'&&typeof book.neg_risk!=='boolean'))throw Error('Market signing protocol unknown or inconsistent');
    const rate=number(info.fd?.r,'authoritative fee rate',0,.10),exponent=number(info.fd?.e,'fee exponent');
    if(exponent!==1)throw Error('Unsupported fee schedule');
    const rulesHash=createHash('sha256').update(JSON.stringify({question:market.question,description:market.description||'',
      end:market.endDate,source:market.resolutionSource,outcomes,ids,condition:market.conditionId,
      protocol:bookVersion,negRisk:book.neg_risk===true})).digest('hex');
    const plan=validatePlan({market:market.conditionId,event:String(market.events?.[0]?.id||market.id),token:request.token,
      side:request.side,price:request.price,shares:request.shares,feeRate:rate,strategy:'explicit-human-test',
      rulesHash,
      expiresAt:new Date(this.now()+60000).toISOString()},this.now());
    if(plan.shares<book.min_order_size||Math.abs(plan.price/book.tick_size-Math.round(plan.price/book.tick_size))>1e-7)throw Error('Minimum quantity or price tick');
    if(plan.price*plan.shares<1)throw Error('Order notional below supported minimum');
    const levels=plan.side==='BUY'?book.asks:book.bids;
    let remaining=plan.shares;
    for(const level of levels)if(plan.side==='BUY'?level.price<=plan.price:level.price>=plan.price)remaining-=Math.min(remaining,level.size);
    if(remaining>1e-6)throw Error('Insufficient executable depth');
    if(plan.side==='BUY'&&worstCost(plan)>STARTER.order)throw Error('Starter order limit');
    if(plan.side==='SELL'&&!account.positions.some(p=>p.token===plan.token&&p.shares>=plan.shares))throw Error('Insufficient owned shares');
    return {plan,approval:planHash(plan),account,marketQuestion:market.question,marketRules:market.description||'',
      protocol:{version:bookVersion,negRisk:book.neg_risk===true},request,mode:'PREVIEW_ONLY',ordersSubmitted:0};
  }
}
module.exports={PolymarketReader};
