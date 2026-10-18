'use strict';
const {createHash}=require('node:crypto');
const {Interface}=require('ethers');
const {ChainReader,COLLATERAL}=require('./live-chain.cjs');
const {validateAccount}=require('./live-ledger.cjs');
const EXCHANGES=Object.freeze({standard:'0xe111180000d2663c0091e4f400237545b87b996b',
  negative:'0xe2222d279d744050d28e00520010520000310f59',position:'0xe3333700ca9d93003f00f0f71f8515005f6c00aa'});
const exchangeAbi=new Interface([
  'event OrderFilled(bytes32 indexed orderHash,address indexed maker,address indexed taker,uint8 side,uint256 tokenId,uint256 makerAmountFilled,uint256 takerAmountFilled,uint256 fee,bytes32 builder,bytes32 metadata)',
  'event OrdersMatched(bytes32 indexed takerOrderHash,address indexed takerOrderMaker,uint8 side,uint256 tokenId,uint256 makerAmountFilled,uint256 takerAmountFilled)',
  'event FeeCharged(address indexed receiver,uint256 amount)']);
const positionAbi=new Interface([
  'event TransferSingle(address indexed operator,address indexed from,address indexed to,uint256 id,uint256 value)',
  'event TransferBatch(address indexed operator,address indexed from,address indexed to,uint256[] ids,uint256[] values)']);
const MANAGERS=Object.freeze({v1:'0x4d97dcd97ec945f40cf65f87097ace5ea0476045',v2:'0x006f54f7f9a22e0000cc2ab60031000000ae9fef'});
function units(value) {
  if(typeof value!=='number'||!Number.isFinite(value)||value<0||!Number.isSafeInteger(Math.round(value*1e6))
    ||Math.abs(value*1e6-Math.round(value*1e6))>.000001)throw Error('Unsupported collateral/share precision');
  return BigInt(Math.round(value*1e6));
}
function domainFor(protocol) {
  if(protocol?.version==='v2')return {exchange:EXCHANGES.position,version:'3'};
  if(protocol?.version==='v1'&&typeof protocol.negRisk==='boolean')return {exchange:protocol.negRisk?EXCHANGES.negative:EXCHANGES.standard,version:'2'};
  throw Error('Unknown signing protocol');
}
function checkpointValid(point,account,now,holder) {
  validateAccount(account,now);
  if(!point||point.chainId!==137||point.decimals!==6||point.collateral!==COLLATERAL||point.holder!==holder
    ||!Number.isSafeInteger(point.blockNumber)||point.blockNumber<0||!/^0x[0-9a-f]{64}$/.test(point.blockHash||'')
    ||!/^\d+$/.test(point.balanceUnits||'')||BigInt(point.balanceUnits)!==units(account.cash))throw Error('Chain checkpoint and account disagree');
  const age=now-Date.parse(point.at);
  if(!Number.isFinite(age)||age>15000||age< -2000)throw Error('Financial chain checkpoint stale');
  return point;
}
function receiptFill(receipt,row,protocol,wallet) {
  const exchange=domainFor(protocol).exchange,manager=MANAGERS[protocol.version];
  const fills=[],matches=[],allowed=new Set([exchange,wallet]);
  for(const log of receipt.logs)if(log.address===exchange) {
    let parsed;try{parsed=exchangeAbi.parseLog(log);}catch{}
    if(!parsed)continue;
    const a=parsed.args;
    if(parsed.name==='FeeCharged')allowed.add(a.receiver.toLowerCase());
    if(parsed.name==='OrderFilled') {
      if(a.taker.toLowerCase()===wallet)allowed.add(a.maker.toLowerCase());
      if(a.maker.toLowerCase()===wallet&&a.orderHash.toLowerCase()!==row.order_id)throw Error('Another wallet order shares the fill receipt');
      if(a.orderHash.toLowerCase()===row.order_id)fills.push(a);
    }
    if(parsed.name==='OrdersMatched') {
      if(a.takerOrderMaker.toLowerCase()===wallet&&a.takerOrderHash.toLowerCase()!==row.order_id)throw Error('Unowned taker execution in receipt');
      if(a.takerOrderHash.toLowerCase()===row.order_id)matches.push(a);
    }
  }
  if(!fills.length||fills.length!==matches.length)throw Error('Approved taker order not proved on chain');
  const side=row.side==='BUY'?0n:1n;
  for(const a of [...fills,...matches])if(a.side!==side||a.tokenId.toString()!==row.token)throw Error('Chain fill asset or side mismatch');
  if(fills.some(a=>a.maker.toLowerCase()!==wallet)||matches.some(a=>a.takerOrderMaker.toLowerCase()!==wallet))throw Error('Chain fill belongs to another wallet');
  const total=(rows,key)=>rows.reduce((n,r)=>n+r[key],0n);
  const making=total(fills,'makerAmountFilled'),taking=total(fills,'takerAmountFilled'),fee=total(fills,'fee');
  if(making!==total(matches,'makerAmountFilled')||taking!==total(matches,'takerAmountFilled'))throw Error('Chain matching events disagree');
  const shares=side===0n?taking:making,gross=side===0n?making:taking;
  if(shares<=0n||gross<=0n||fee>=gross)throw Error('Unsupported chain fill amounts');
  const bound=units(row.price)*shares;
  if(side===0n?gross*1000000n>bound:gross*1000000n<bound)throw Error('Chain price violates approval');
  let transferredShares=0n;
  for(const log of receipt.logs)if(Object.values(MANAGERS).includes(log.address)) {
    let p;try{p=positionAbi.parseLog(log);}catch{}
    if(!p)continue;
    const from=p.args.from.toLowerCase(),to=p.args.to.toLowerCase();
    if(from!==wallet&&to!==wallet)continue;
    if(log.address!==manager)throw Error('Unexpected position protocol in receipt');
    const ids=p.name==='TransferSingle'?[p.args.id]:p.args.ids;
    const amounts=p.name==='TransferSingle'?[p.args.value]:p.args[4];
    if(ids.length!==amounts.length||ids.length>100)throw Error('Unsupported position batch');
    for(let i=0;i<ids.length;i++) {
      if(ids[i].toString()!==row.token)throw Error('Another position moved in fill receipt');
      transferredShares+=(to===wallet?amounts[i]:0n)-(from===wallet?amounts[i]:0n);
    }
  }
  if(transferredShares!==(side===0n?shares:-shares))throw Error('Position transfer and fill events disagree');
  return {shares,cashDelta:side===0n?-(gross+fee):gross-fee,allowed};
}

class LiveChainGuard {
  constructor(ledger,auth,{chain=new ChainReader(auth.address)}={}) {
    this.ledger=ledger;this.chain=chain;this.wallet=auth.address.toLowerCase();
    this.holder=createHash('sha256').update('polygon:137:'+this.wallet).digest('hex');
    this.identity=createHash('sha256').update('polymarket:137:'+auth.signatureType+':'+this.wallet).digest('hex');
  }
  check(point,account) {
    if(account.identity!==this.identity)throw Error('Chain guard account identity mismatch');
    return checkpointValid(point,account,this.ledger.now(),this.holder);
  }
  anchor() {
    const point=this.ledger.get('chainAnchor');
    if(this.ledger.get('chainRequired')!==true||!point)throw Error('Verified chain baseline missing; manual review required');
    if(point.holder!==this.holder)throw Error('Chain baseline belongs to another wallet');
    return point;
  }
  async activate(account,actor) {
    const point=await this.chain.checkpoint();this.check(point,account);
    this.ledger.activate(account,actor,point);
  }
  async knownPositions(point,account,extra=null) {
    const rows=this.ledger.db.prepare('SELECT * FROM live_inventory').all();
    const targets=rows.map(row=>{
      const intent=this.ledger.db.prepare("SELECT id FROM live_intents WHERE token=? AND state='CONFIRMED' ORDER BY at DESC LIMIT 1").get(row.token);
      return {token:row.token,protocol:this.ledger.get('protocol:'+intent?.id)};
    });
    if(extra&&!targets.some(t=>t.token===extra.token))targets.push(extra);
    for(const target of targets) {
      const actual=BigInt(await this.chain.positionBalance(target.token,target.protocol,point));
      const api=account.positions.find(p=>p.token===target.token);
      if(actual!==units(api?.shares||0))throw Error('Onchain position and account disagree');
    }
  }
  async capture() {
    const anchor=this.anchor();let point=await this.chain.checkpoint();
    const activity=await this.chain.transfers(anchor,point);
    // Close a slow historical scan with fresh evidence rather than changing the
    // anchor or accepting a stale financial account snapshot.
    for(let pass=0;pass<2&&this.ledger.now()-Date.parse(point.at)>5000;pass++) {
      const latest=await this.chain.checkpoint(),tail=await this.chain.transfers(point,latest);
      activity.transfers.push(...tail.transfers);
      activity.netUnits=(BigInt(activity.netUnits)+BigInt(tail.netUnits)).toString();
      activity.toBlock=latest.blockNumber;activity.afterBalanceUnits=latest.balanceUnits;point=latest;
    }
    return {anchor,point,activity};
  }
  async quiet(accountSource,extra=null) {
    const {anchor,point,activity}=await this.capture();
    if(activity.transfers.length) {
      this.ledger.halt('Unreconciled blockchain movement; anchor retained');
      throw Error('Unreconciled blockchain activity, including offsetting movements');
    }
    const account=typeof accountSource==='function'?await accountSource():accountSource;
    this.check(point,account);await this.knownPositions(point,account,extra);
    this.check(point,account);
    return {kind:'QUIET',anchor,checkpoint:point,movements:0,account};
  }
  async resume(accountSource,actor) {
    const proof=await this.quiet(accountSource);this.ledger.resume(proof.account,actor,proof);
  }
  async sync(accountSource) {
    const proof=await this.quiet(accountSource);this.ledger.syncChain(proof.account,proof);
  }
  async verifyFill(row,accountSource,trades,shares,cash) {
    const protocol=this.ledger.get('protocol:'+row.id);
    domainFor(protocol);
    const {anchor,point,activity}=await this.capture();
    const account=typeof accountSource==='function'?await accountSource():accountSource;
    this.check(point,account);
    const freshTrades=account.trades.filter(t=>t.orderId===row.order_id&&t.status!=='FAILED');
    const byId=list=>Object.fromEntries(list.map(t=>[t.id,JSON.stringify(t)]));
    const oldEvidence=byId(trades),freshEvidence=byId(freshTrades);
    if(Object.keys(oldEvidence).length!==Object.keys(freshEvidence).length||Object.entries(oldEvidence).some(([id,value])=>freshEvidence[id]!==value))throw Error('Trade evidence changed during chain audit');
    const hashes=[...new Set(trades.map(t=>t.transactionHash.toLowerCase()))];
    if(!hashes.length||hashes.length>16)throw Error('Unsupported fill receipt capacity');
    if(activity.transfers.some(t=>!hashes.includes(t.transactionHash)))throw Error('External transfer cannot become fill P&L');
    let filled=0n,delta=0n;
    for(const tx of hashes) {
      const receipt=await this.chain.receipt(tx,point),proved=receiptFill(receipt,row,protocol,this.wallet);
      const transfers=activity.transfers.filter(t=>t.transactionHash===tx);
      if(!transfers.length||transfers.reduce((n,t)=>n+BigInt(t.deltaUnits),0n)!==proved.cashDelta)throw Error('Receipt cash and execution disagree');
      for(const transfer of transfers) {
        const counterparties=[transfer.from,transfer.to].filter(a=>a!==this.wallet);
        if(counterparties.some(a=>!proved.allowed.has(a)))throw Error('Unattributed collateral movement in fill receipt');
      }
      filled+=proved.shares;delta+=proved.cashDelta;
    }
    if(filled!==units(shares)||delta!==(row.side==='BUY'?-units(cash):units(cash))||delta.toString()!==activity.netUnits)throw Error('Onchain fill and account deltas disagree');
    const before=BigInt(await this.chain.positionBalance(row.token,protocol,anchor));
    const after=BigInt(await this.chain.positionBalance(row.token,protocol,point));
    if(after-before!==(row.side==='BUY'?filled:-filled))throw Error('Onchain inventory delta mismatch');
    await this.knownPositions(point,account,{token:row.token,protocol});this.check(point,account);
    return {kind:'FILL',anchor,checkpoint:point,orderId:row.order_id,token:row.token,
      sharesUnits:filled.toString(),cashDeltaUnits:delta.toString(),transactions:hashes,account};
  }
}
module.exports={LiveChainGuard,domainFor,checkpointValid,receiptFill,units,EXCHANGES,MANAGERS};
