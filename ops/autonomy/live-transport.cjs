'use strict';
const {planHash,validateAccount}=require('./live-ledger.cjs');

function orderHash(body,domain) {
  const {TypedDataEncoder}=require('ethers');
  const types={Order:[['salt','uint256'],['maker','address'],['signer','address'],['tokenId','uint256'],
    ['makerAmount','uint256'],['takerAmount','uint256'],['side','uint8'],['signatureType','uint8'],
    ['timestamp','uint256'],['metadata','bytes32'],['builder','bytes32']].map(([name,type])=>({name,type}))};
  return TypedDataEncoder.hash({name:'Polymarket CTF Exchange',version:domain.version,chainId:137,verifyingContract:domain.exchange},
    types,{...body.order,side:body.order.side==='BUY'?0:1});
}
class LiveTransport {
  constructor(ledger,reader,auth,{fetch=global.fetch,sign}={}) {
    this.ledger=ledger;this.reader=reader;this.auth=auth;this.fetch=fetch;
    this.sign=sign||((preview)=>{
      const signer=require('../../dist/utils/polymarket-order-signer');
      if(!['v1','v2'].includes(preview.protocol.version))throw Error('Unsupported market protocol');
      const domain=preview.protocol.version==='v2'?{exchange:signer.POSITION_EXCHANGE,version:'3'}:
        {exchange:preview.protocol.negRisk?signer.NEG_RISK_CTF_EXCHANGE_V2:signer.CTF_EXCHANGE_V2,version:'2'};
      const body=signer.buildSignedOrderV2({tokenId:preview.plan.token,price:preview.plan.price,
        size:preview.plan.shares,side:preview.plan.side.toLowerCase(),signingDomain:domain},
        {privateKey:auth.privateKey,funderAddress:auth.address,signatureType:auth.signatureType});
      body.owner=auth.apiKey;body.orderType='FOK';body.deferExec=false;
      return {body,domain,orderId:orderHash(body,domain)};
    });
  }
  checkActor(actor) {
    if(actor?.kind!=='human-owner'||actor.id!==this.ledger.get('actor'))throw Error('Explicit authenticated owner action required');
  }
  async submit(approved,approval,actor) {
    this.checkActor(actor);
    if(approval!==planHash(approved.plan))throw Error('Exact plan approval required');
    if(this.ledger.mode()!=='manual'&&!(this.ledger.mode()==='halted'&&approved.plan.side==='SELL'))throw Error('Live mode disabled');
    const fresh=await this.reader.preview(approved.request);
    fresh.plan.expiresAt=approved.plan.expiresAt;
    if(planHash(fresh.plan)!==approval)throw Error('Market, fees or resolution rules changed since approval');
    const version=await this.reader.get('https://clob.polymarket.com/version');
    if(version.version!==2)throw Error('Unsupported exchange order format');
    const id=this.ledger.reserve(fresh.plan,fresh.account,approval);
    let signed;
    try {
      signed=await this.sign(fresh);
      if(signed.body.orderType!=='FOK'||signed.body.owner!==this.auth.apiKey)throw Error('Unexpected order format');
      const order=signed.body.order,plan=fresh.plan;
      const cost=String(Math.round(plan.price*plan.shares*1e6)),shares=String(Math.round(plan.shares*1e6));
      if(order.tokenId!==plan.token||order.side!==plan.side||order.maker?.toLowerCase()!==this.auth.address.toLowerCase()
        ||order.makerAmount!==(plan.side==='BUY'?cost:shares)||order.takerAmount!==(plan.side==='BUY'?shares:cost)
        ||order.signatureType!==this.auth.signatureType||orderHash(signed.body,signed.domain)!==signed.orderId)throw Error('Signed payload differs from approved plan');
      this.ledger.signed(id,signed.orderId);
      const current=await this.reader.account();
      validateAccount(current,this.ledger.now());
      this.ledger.sending(id,current);
    } catch {
      this.ledger.reject(id,'Pre-submission verification failed');
      throw Error('Order not submitted: preflight failed');
    }
    // No automatic retries: uncertainty retains the reservation and stops entries.
    const url='https://clob.polymarket.com/order';
    try {
      const response=await this.fetch(url,{method:'POST',redirect:'error',signal:AbortSignal.timeout(15000),
        headers:{...this.reader.buildHeaders(this.auth,'POST',url,signed.body),'Content-Type':'application/json'},body:JSON.stringify(signed.body)});
      const data=await response.json();
      if(!response.ok||data.success===false||data.errorMsg) {
        // A duplicate hash may already be live; never treat it as a safe rejection.
        const message=String(data.errorMsg||'');
        const definitelyRejected=![408,429,500,502,503,504].includes(response.status)
          && !/duplicate|already|exist|matched/i.test(message)
          && (response.status===401||response.status===403||/invalid signature|not enough balance|invalid price|invalid size|min(imum)? order/i.test(message));
        if(definitelyRejected)this.ledger.reject(id,'Exchange explicitly rejected the order',true);
        else this.ledger.unknown(id);
        return {intent:id,state:definitelyRejected?'REJECTED':'UNKNOWN',orderId:signed.orderId};
      }
      const returned=data.orderID||data.order_id;
      if(returned!==signed.orderId)throw Error('Unverifiable order ID');
      this.ledger.acknowledge(id,returned,data.status==='matched'?'FILLED':'OPEN');
      return {intent:id,state:'AWAITING_RECONCILIATION',orderId:returned};
    } catch {
      this.ledger.unknown(id);
      return {intent:id,state:'UNKNOWN',orderId:signed.orderId};
    }
  }
  async cancel(id,actor) {
    this.checkActor(actor);
    const row=this.ledger.db.prepare('SELECT * FROM live_intents WHERE id=?').get(id);
    if(!row?.order_id)throw Error('Unknown owned order');
    this.ledger.cancelRequested(id);
    const url='https://clob.polymarket.com/order',body={orderID:row.order_id};
    try {
      const response=await this.fetch(url,{method:'DELETE',redirect:'error',signal:AbortSignal.timeout(15000),
        headers:{...this.reader.buildHeaders(this.auth,'DELETE',url,body),'Content-Type':'application/json'},body:JSON.stringify(body)});
      const data=await response.json();
      if(!response.ok||!Array.isArray(data.canceled)||!data.canceled.includes(row.order_id))throw Error('Cancellation outcome unknown');
      return {intent:id,state:'CANCEL_PENDING_RECONCILIATION'};
    } catch {this.ledger.unknown(id);return {intent:id,state:'UNKNOWN'};}
  }
  async reconcile(id) {
    const row=this.ledger.db.prepare('SELECT * FROM live_intents WHERE id=?').get(id);
    if(!row?.order_id)throw Error('Unknown owned order');
    if(row.state==='CONFIRMED'||row.state==='CANCELLED'||row.state==='REJECTED')return {intent:id,state:row.state};
    const order=await this.reader.get('https://clob.polymarket.com/data/order/'+encodeURIComponent(row.order_id),true);
    if(order.id!==row.order_id||order.asset_id!==row.token||order.market!==row.market||order.side!==row.side)throw Error('Order identity mismatch');
    const matched=Number(order.size_matched);
    if(!Number.isFinite(matched)||matched<0||matched>row.shares+.000001)throw Error('Invalid matched quantity');
    const account=await this.reader.account();
    if(account.orders.some(o=>o.id===row.order_id))return {intent:id,state:'OPEN',matched};
    if(order.status==='CANCELED'&&matched===0) {
      this.ledger.confirmUnfilledCancellation(id,{orderId:row.order_id,status:'CANCELED',sizeMatched:0,authenticated:true},account);
      return {intent:id,state:'CANCELLED'};
    }
    if(!['MATCHED','CANCELED'].includes(order.status)||matched===0)return {intent:id,state:'AWAITING_RECONCILIATION'};
    const trades=account.trades.filter(t=>t.orderId===row.order_id);
    if(!trades.length||trades.some(t=>t.status!=='CONFIRMED'||t.token!==row.token||t.side!==row.side))return {intent:id,state:'AWAITING_CONFIRMATION'};
    if(Math.abs(trades.reduce((n,t)=>n+t.shares,0)-matched)>.00011)throw Error('Trade and order quantities disagree');
    const old=this.ledger.db.prepare('SELECT * FROM live_inventory WHERE token=?').get(row.token);
    const actual=account.positions.find(p=>p.token===row.token);
    const shares=row.side==='BUY'?(actual?.shares||0)-(old?.shares||0):(old?.shares||0)-(actual?.shares||0);
    const cash=row.side==='BUY'?this.ledger.get('lastCash')-account.cash:account.cash-this.ledger.get('lastCash');
    if(shares<=0||shares>matched+.00011||cash<=0)return {intent:id,state:'AWAITING_ACCOUNT_BALANCES'};
    this.ledger.reconcileFill(id,{orderId:row.order_id,shares,cash,final:true,authenticated:true,accountReflected:true},account);
    return {intent:id,state:'CONFIRMED',shares,cash};
  }
}
module.exports={LiveTransport,orderHash};
