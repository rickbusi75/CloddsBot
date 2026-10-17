'use strict';
// Read-only Polygon evidence. No signer, private key, wallet client or send method.
const {createHash}=require('node:crypto');
const ENDPOINTS=Object.freeze(['https://polygon.gateway.tenderly.co','https://polygon.drpc.org']);
const HISTORY_ENDPOINTS=Object.freeze(['https://polygon.gateway.tenderly.co','https://polygon-bor-rpc.publicnode.com']);
const ALLOWED=new Set([...ENDPOINTS,...HISTORY_ENDPOINTS]);
const COLLATERAL='0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb';
const TRANSFER='0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef';
const METHODS=new Set(['eth_chainId','eth_getBlockByNumber','eth_call','eth_getCode','eth_getLogs','eth_getTransactionReceipt']);
const hex=n=>'0x'+BigInt(n).toString(16);
const address=v=>{
  if(typeof v!=='string'||!/^0x[0-9a-fA-F]{40}$/.test(v))throw Error('Invalid chain address');
  return v.toLowerCase();
};
const hash=v=>{
  if(typeof v!=='string'||!/^0x[0-9a-fA-F]{64}$/.test(v))throw Error('Invalid chain hash');
  return v.toLowerCase();
};
const quantity=v=>{
  if(typeof v!=='string'||!/^0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)$/.test(v))throw Error('Invalid chain quantity');
  const n=Number(BigInt(v));if(!Number.isSafeInteger(n))throw Error('Chain quantity exceeds capacity');return n;
};
const word=v=>{
  if(typeof v!=='string'||!/^0x[0-9a-fA-F]{64}$/.test(v))throw Error('Invalid chain ABI word');
  return BigInt(v);
};
const same=(a,b)=>{if(JSON.stringify(a)!==JSON.stringify(b))throw Error('Chain providers disagree');return a;};
const rpcError=(message,endpoint,method,extra={})=>Object.assign(Error(message),
  {provider:new URL(endpoint).hostname,rpcMethod:method,...extra});

class ChainReader {
  constructor(wallet,{fetch=global.fetch,now=()=>Date.now(),endpoints=ENDPOINTS,historyEndpoints=HISTORY_ENDPOINTS,minIntervalMs=250}={}) {
    this.wallet=address(wallet);this.topic='0x'+this.wallet.slice(2).padStart(64,'0');
    this.holder=createHash('sha256').update('polygon:137:'+this.wallet).digest('hex');
    for(const pair of [endpoints,historyEndpoints])if(!Array.isArray(pair)||pair.length!==2||new Set(pair).size!==2||pair.some(e=>!ALLOWED.has(e)))throw Error('Two allowlisted chain endpoints required');
    if(!Number.isInteger(minIntervalMs)||minIntervalMs<0||minIntervalMs>10000)throw Error('Invalid RPC pacing');
    this.endpoints=[...endpoints];this.historyEndpoints=[...historyEndpoints];this.fetch=fetch;this.now=now;this.sequence=0;
    this.interval=minIntervalMs;this.queues=new Map();this.next=new Map();
  }
  providers(method){return ['eth_getLogs','eth_getTransactionReceipt'].includes(method)?this.historyEndpoints:this.endpoints;}
  async rpc(endpoint,method,params=[]) {
    if(!METHODS.has(method)||!this.providers(method).includes(endpoint))throw Error('Only allowlisted read-only RPC methods');
    const previous=this.queues.get(endpoint)||Promise.resolve();let release;
    this.queues.set(endpoint,new Promise(resolve=>{release=resolve;}));
    await previous;
    const delay=Math.max(0,(this.next.get(endpoint)||0)-Date.now());
    if(delay)await new Promise(resolve=>setTimeout(resolve,delay));
    this.next.set(endpoint,Date.now()+this.interval);release();
    const id=++this.sequence;
    const response=await this.fetch(endpoint,{method:'POST',redirect:'error',signal:AbortSignal.timeout(15000),
      headers:{'Content-Type':'application/json'},body:JSON.stringify({jsonrpc:'2.0',id,method,params})});
    if(!response.ok) {
      const retry=Number(response.headers.get('retry-after'));
      throw rpcError('Chain RPC unavailable',endpoint,method,{httpStatus:response.status,
        ...(Number.isInteger(retry)&&retry>0&&retry<=3600?{retryAfterSeconds:retry}:{})});
    }
    const reader=response.body?.getReader();if(!reader)throw Error('Missing chain response body');
    const chunks=[];let size=0;
    try {
      for(;;){const {done,value}=await reader.read();if(done)break;size+=value.byteLength;
        if(size>2000000){await reader.cancel();throw Error('Chain response exceeds capacity');}chunks.push(Buffer.from(value));}
    }finally{reader.releaseLock();}
    let body;try{body=JSON.parse(Buffer.concat(chunks).toString('utf8'));}catch{throw Error('Invalid chain response');}
    if(body?.jsonrpc!=='2.0'||body.id!==id||body.error||!Object.hasOwn(body,'result'))throw rpcError('Chain RPC read failed',endpoint,method,
      Number.isInteger(body?.error?.code)?{rpcCode:body.error.code}:{});
    return body.result;
  }
  async quorum(method,params,normalize=v=>v) {
    const results=await Promise.all(this.providers(method).map(async e=>normalize(await this.rpc(e,method,params))));
    return same(...results);
  }
  normalizeBlock(block) {
    if(!block)throw Error('Chain block unavailable');
    return {number:quantity(block.number),hash:hash(block.hash),timestamp:quantity(block.timestamp)};
  }
  async block(number) {return this.quorum('eth_getBlockByNumber',[hex(number),false],b=>this.normalizeBlock(b));}
  validateCheckpoint(value,fresh=false) {
    if(!value||value.holder!==this.holder||value.chainId!==137||value.collateral!==COLLATERAL
      ||!Number.isSafeInteger(value.blockNumber)||value.blockNumber<0||!/^\d+$/.test(value.balanceUnits||''))throw Error('Wrong or invalid chain checkpoint');
    hash(value.blockHash);
    // Historical evidence may take longer than the separate 15-second account
    // preflight. This observation window does not authorize a financial action.
    if(fresh&&(!Number.isFinite(Date.parse(value.at))||this.now()-Date.parse(value.at)>120000||this.now()-Date.parse(value.at)<-2000))throw Error('Stale chain checkpoint');
    return value;
  }
  async confirmCheckpoint(value,fresh=false) {
    this.validateCheckpoint(value,fresh);
    const heads=await Promise.all(this.endpoints.map(async e=>this.normalizeBlock(await this.rpc(e,'eth_getBlockByNumber',['finalized',false]))));
    if(heads.some(h=>h.number<value.blockNumber||this.now()/1000-h.timestamp>180||h.timestamp-this.now()/1000>30))throw Error('Checkpoint finality unavailable');
    const block=await this.block(value.blockNumber);
    if(block.hash!==value.blockHash)throw Error('Chain checkpoint changed');
    const balance=await this.quorum('eth_call',[{to:COLLATERAL,data:'0x70a08231'+this.wallet.slice(2).padStart(64,'0')},hex(value.blockNumber)],v=>word(v).toString());
    if(balance!==value.balanceUnits)throw Error('Checkpoint balance not verified');
    return value;
  }
  async checkpoint() {
    const chains=await this.quorum('eth_chainId',[],quantity);if(chains!==137)throw Error('Wrong chain');
    const heads=await Promise.all(this.endpoints.map(async e=>this.normalizeBlock(await this.rpc(e,'eth_getBlockByNumber',['finalized',false]))));
    const block=await this.block(Math.min(...heads.map(h=>h.number)));
    if(this.now()/1000-block.timestamp>180||block.timestamp-this.now()/1000>30)throw Error('Finalized chain head stale');
    const tag=hex(block.number);
    const [balance,decimals,code]=await Promise.all([
      this.quorum('eth_call',[{to:COLLATERAL,data:'0x70a08231'+this.wallet.slice(2).padStart(64,'0')},tag],v=>word(v).toString()),
      this.quorum('eth_call',[{to:COLLATERAL,data:'0x313ce567'},tag],v=>word(v).toString()),
      this.quorum('eth_getCode',[COLLATERAL,tag],v=>{
        if(typeof v!=='string'||!/^0x(?:[a-fA-F0-9]{2})+$/.test(v))throw Error('Collateral contract unavailable');return v.toLowerCase();})]);
    if(decimals!=='6')throw Error('Unexpected collateral precision');
    same(block,await this.block(block.number));
    return {at:new Date(this.now()).toISOString(),holder:this.holder,chainId:137,collateral:COLLATERAL,
      blockNumber:block.number,blockHash:block.hash,balanceUnits:balance,decimals:6};
  }
  normalizeLog(log) {
    if(!log||log.removed!==false||!Array.isArray(log.topics)||log.topics.length>4
      ||typeof log.data!=='string'||!/^0x(?:[a-fA-F0-9]{2})*$/.test(log.data)||log.data.length>200000)throw Error('Invalid or removed chain log');
    return {address:address(log.address),topics:log.topics.map(hash),data:log.data.toLowerCase(),
      blockNumber:quantity(log.blockNumber),blockHash:hash(log.blockHash),transactionHash:hash(log.transactionHash),logIndex:quantity(log.logIndex)};
  }
  transfer(log) {
    const normalized=this.normalizeLog(log);
    if(normalized.address!==COLLATERAL||normalized.topics[0]!==TRANSFER||normalized.topics.length!==3
      ||normalized.topics.slice(1).some(t=>!t.startsWith('0x'+'0'.repeat(24))))throw Error('Unexpected collateral transfer');
    const from='0x'+normalized.topics[1].slice(-40),to='0x'+normalized.topics[2].slice(-40);
    if(from!==this.wallet&&to!==this.wallet)throw Error('Unrelated collateral transfer');
    const units=word(normalized.data);
    return {blockNumber:normalized.blockNumber,blockHash:normalized.blockHash,transactionHash:normalized.transactionHash,
      logIndex:normalized.logIndex,from,to,units:units.toString(),
      deltaUnits:((to===this.wallet?units:0n)-(from===this.wallet?units:0n)).toString()};
  }
  async receipt(transactionHash,checkpoint) {
    hash(transactionHash);await this.confirmCheckpoint(checkpoint,true);
    const receipt=await this.quorum('eth_getTransactionReceipt',[transactionHash],r=>{
      if(!r||r.status!=='0x1'||hash(r.transactionHash)!==transactionHash.toLowerCase()||!Array.isArray(r.logs)||r.logs.length>2000)throw Error('Successful chain receipt unavailable');
      const logs=r.logs.map(l=>this.normalizeLog(l)).sort((a,b)=>a.logIndex-b.logIndex);
      const number=quantity(r.blockNumber),blockHash=hash(r.blockHash),seen=new Set();
      for(const log of logs){if(log.blockNumber!==number||log.blockHash!==blockHash||log.transactionHash!==transactionHash.toLowerCase()||seen.has(log.logIndex))throw Error('Receipt log identity mismatch');seen.add(log.logIndex);}
      return {transactionHash:hash(r.transactionHash),blockNumber:number,blockHash,
        from:address(r.from),to:r.to===null?null:address(r.to),logs};
    });
    if(receipt.blockNumber>checkpoint.blockNumber)throw Error('Receipt not finalized at checkpoint');
    if((await this.block(receipt.blockNumber)).hash!==receipt.blockHash)throw Error('Receipt block is not canonical');
    if((await this.block(checkpoint.blockNumber)).hash!==checkpoint.blockHash)throw Error('Checkpoint block changed');
    return receipt;
  }
  async transfers(anchor,checkpoint) {
    this.validateCheckpoint(anchor);this.validateCheckpoint(checkpoint,true);
    const span=checkpoint.blockNumber-anchor.blockNumber;
    if(span<0||span>65536)throw Error('Chain audit range exceeds capacity');
    await this.confirmCheckpoint(anchor);await this.confirmCheckpoint(checkpoint,true);
    const all=[];
    for(let start=anchor.blockNumber+1;start<=checkpoint.blockNumber;start+=4096) {
      this.validateCheckpoint(checkpoint,true);
      const end=Math.min(start+4095,checkpoint.blockNumber);
      const results=await Promise.all(this.historyEndpoints.map(async endpoint=>{
        const unique=new Map();
        for(const topics of [[TRANSFER,this.topic],[TRANSFER,null,this.topic]]) {
          const logs=await this.rpc(endpoint,'eth_getLogs',[{address:COLLATERAL,fromBlock:hex(start),toBlock:hex(end),topics}]);
          if(!Array.isArray(logs)||logs.length>5000)throw Error('Chain logs incomplete or too large');
          const seen=new Set();
          for(const raw of logs) {
            const log=this.transfer(raw),id=log.blockHash+':'+log.logIndex;
            if(log.blockNumber<start||log.blockNumber>end||seen.has(id))throw Error('Chain log range or duplicate');seen.add(id);
            if(unique.has(id))same(unique.get(id),log);else unique.set(id,log);
          }
        }
        return [...unique.values()].sort((a,b)=>a.blockNumber-b.blockNumber||a.logIndex-b.logIndex);
      }));
      all.push(...same(...results));if(all.length>5000)throw Error('Chain audit activity exceeds capacity');
    }
    const byTransaction=Map.groupBy(all,l=>l.transactionHash);
    for(const [transactionHash,logs] of byTransaction) {
      this.validateCheckpoint(checkpoint,true);
      const receipt=await this.receipt(transactionHash,checkpoint);
      const reflected=receipt.logs.filter(l=>l.address===COLLATERAL&&l.topics[0]===TRANSFER
        &&(l.topics[1]===this.topic||l.topics[2]===this.topic)).map(l=>this.transfer({...l,removed:false,
          blockNumber:hex(l.blockNumber),logIndex:hex(l.logIndex)}));
      same(logs,reflected);
    }
    const net=all.reduce((n,l)=>n+BigInt(l.deltaUnits),0n);
    if(BigInt(anchor.balanceUnits)+net!==BigInt(checkpoint.balanceUnits))throw Error('Chain balance and transfer history disagree');
    if((await this.block(checkpoint.blockNumber)).hash!==checkpoint.blockHash)throw Error('Chain checkpoint changed');
    this.validateCheckpoint(checkpoint,true);
    return {fromBlock:anchor.blockNumber,toBlock:checkpoint.blockNumber,netUnits:net.toString(),transfers:all,
      beforeBalanceUnits:anchor.balanceUnits,afterBalanceUnits:checkpoint.balanceUnits};
  }
}
module.exports={ChainReader,ENDPOINTS,HISTORY_ENDPOINTS,COLLATERAL,TRANSFER};
