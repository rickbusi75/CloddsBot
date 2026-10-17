'use strict';

const Sqlite = require('better-sqlite3');
const {mkdirSync, chmodSync} = require('node:fs');
const {dirname} = require('node:path');
const {createHash} = require('node:crypto');

const STARTER = Object.freeze({capital:20, order:1.5, exposure:5, dailyLoss:2, drawdown:2, ordersPerDay:4});
const ACTIVE = ['RESERVED','SIGNED','SENDING','UNKNOWN','OPEN','PARTIAL','CANCEL_PENDING'];
const money = n => Math.round(n * 1e6) / 1e6;
const finite = (value, label, minimum=0) => {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < minimum) throw Error('Invalid '+label);
  return value;
};
const stable = value => JSON.stringify(value, Object.keys(value).sort());
function planHash(plan) {
  const fields = ['market','event','token','side','price','shares','feeRate','strategy','rulesHash','expiresAt'];
  return createHash('sha256').update(stable(Object.fromEntries(fields.map(k=>[k,plan[k]])))).digest('hex');
}
function validateAccount(account, now) {
  if (!account || !Array.isArray(account.positions) || !Array.isArray(account.orders)
    || !Array.isArray(account.trades) || account.exhaustive !== true) throw Error('Account exposure unknown');
  const at = Date.parse(account.at);
  if (!Number.isFinite(at) || now-at > 15000 || now-at < -2000) throw Error('Stale account snapshot');
  if (account.geoblock?.blocked !== false || account.geoblock?.country !== 'ES') throw Error('Geographic check failed');
  finite(account.cash,'cash');
  finite(account.allowance,'allowance');
  for (const p of account.positions) {
    if (!p.token || !p.market || p.market !== String(p.market)) throw Error('Unknown position schema');
    finite(p.shares,'position shares'); finite(p.cost,'position cost'); finite(p.bidValue,'position value');
  }
  for (const o of account.orders) {
    if (!o.id || !o.token || !['BUY','SELL'].includes(o.side)) throw Error('Unknown order schema');
    finite(o.remaining,'remaining shares'); finite(o.price,'order price',.000001);
  }
  return account;
}
function validatePlan(plan, now) {
  if (!plan || typeof plan !== 'object') throw Error('Missing plan');
  for(const key of ['market','event','token','strategy']) if(typeof plan[key]!=='string'||!plan[key]) throw Error('Missing '+key);
  if(typeof plan.rulesHash!=='string'||!/^([0-9a-f]{64})$/.test(plan.rulesHash))throw Error('Missing resolution rules hash');
  if (!['BUY','SELL'].includes(plan.side)) throw Error('Unsupported side');
  finite(plan.price,'price',.000001); finite(plan.shares,'shares',.000001); finite(plan.feeRate,'fee rate');
  if (plan.price >= 1 || plan.feeRate > .10) throw Error('Unsupported price or fees');
  if(Math.abs(plan.price*100-Math.round(plan.price*100))>.000001)throw Error('Signer currently requires cent price precision');
  if (!Number.isFinite(Date.parse(plan.expiresAt)) || Date.parse(plan.expiresAt) <= now
      || Date.parse(plan.expiresAt)-now > 60000) throw Error('Plan expired or lifetime too long');
  if (Math.abs(plan.shares*100-Math.round(plan.shares*100))>.000001) throw Error('Unsupported share precision');
  return plan;
}
function worstCost(plan) {
  return money(plan.shares*plan.price + plan.shares*plan.feeRate*plan.price*(1-plan.price) + .00001);
}

class LiveLedger {
  constructor(file, now=()=>Date.now()) {
    mkdirSync(dirname(file),{recursive:true,mode:0o700});
    this.db=new Sqlite(file,{timeout:30000});
    chmodSync(file,0o600);
    this.now=now;
    this.db.pragma('journal_mode = WAL');
    // SENDING must be durable before any external request, including power loss.
    this.db.pragma('synchronous = FULL');
    this.db.exec(`CREATE TABLE IF NOT EXISTS live_config(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS live_intents(id TEXT PRIMARY KEY,at TEXT NOT NULL,market TEXT NOT NULL,
        event TEXT NOT NULL,token TEXT NOT NULL,side TEXT NOT NULL,price REAL NOT NULL,shares REAL NOT NULL,
        reserve REAL NOT NULL,state TEXT NOT NULL,order_id TEXT UNIQUE,plan TEXT NOT NULL,error TEXT,
        filled_shares REAL NOT NULL DEFAULT 0,actual_cash REAL,finished_at TEXT);
      CREATE TABLE IF NOT EXISTS live_events(id INTEGER PRIMARY KEY,intent TEXT,at TEXT NOT NULL,
        kind TEXT NOT NULL,detail TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS live_realized(id TEXT PRIMARY KEY,at TEXT NOT NULL,pnl REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS live_inventory(token TEXT PRIMARY KEY,market TEXT NOT NULL,event TEXT NOT NULL,
        shares REAL NOT NULL,cost REAL NOT NULL);`);
    this.db.prepare("INSERT OR IGNORE INTO live_config VALUES('mode','\"disabled\"')").run();
  }
  close(){this.db.close();}
  get(key,fallback){const row=this.db.prepare('SELECT value FROM live_config WHERE key=?').get(key);return row?JSON.parse(row.value):fallback;}
  put(key,value){this.db.prepare('INSERT OR REPLACE INTO live_config VALUES(?,?)').run(key,JSON.stringify(value));}
  event(intent,kind,detail={}){this.db.prepare('INSERT INTO live_events(intent,at,kind,detail) VALUES(?,?,?,?)').run(intent,new Date(this.now()).toISOString(),kind,JSON.stringify(detail));}
  mode(){return this.get('mode','disabled');}
  // Calling code must expose this only to an explicit authenticated owner action.
  activate(account, actor) {
    validateAccount(account,this.now());
    if (!actor || actor.kind!=='human-owner' || typeof actor.id!=='string' || !actor.id) throw Error('Explicit owner activation required');
    if (account.orders.length || account.positions.length) throw Error('Starter activation requires a flat account');
    if (account.cash<STARTER.capital || account.allowance<STARTER.order) throw Error('Starter capital unavailable');
    this.db.transaction(()=>{
      if(this.get('baseline')!==undefined) throw Error('An existing live ledger cannot be reset');
      this.put('baseline',account.cash); this.put('lastCash',account.cash);
      this.put('baselineTrades',account.trades.map(t=>t.id).sort());
      this.put('mode','manual'); this.put('limits',STARTER); this.put('actor',actor.id);
      this.event(null,'ACTIVATED',{mode:'manual',limits:STARTER});
    }).immediate();
  }
  halt(reason='owner stop') {
    this.db.transaction(()=>{this.put('mode','halted');this.put('haltReason',String(reason).slice(0,200));this.event(null,'HALTED',{reason:String(reason).slice(0,200)});}).immediate();
  }
  summary() {
    const pending=this.db.prepare(`SELECT COUNT(*) count,COALESCE(SUM(CASE WHEN side='BUY' THEN reserve ELSE 0 END),0) reserved
      FROM live_intents WHERE state IN (${ACTIVE.map(()=>'?').join(',')})`).get(...ACTIVE);
    const exposure=this.db.prepare('SELECT COALESCE(SUM(cost),0) value FROM live_inventory').get().value;
    const day=new Date(this.now()).toISOString().slice(0,10);
    const loss=this.db.prepare('SELECT COALESCE(-SUM(MIN(pnl,0)),0) value FROM live_realized WHERE at LIKE ?').get(day+'%').value;
    const realized=this.db.prepare('SELECT COALESCE(SUM(pnl),0) value FROM live_realized').get().value;
    const orders=this.db.prepare('SELECT COUNT(*) value FROM live_intents WHERE side=\'BUY\' AND at LIKE ? AND state NOT IN (\'RESERVED\',\'REJECTED\')').get(day+'%').value;
    return {mode:this.mode(),limits:STARTER,baseline:this.get('baseline',null),realized,dailyLoss:loss,
      exposure:money(exposure),reserved:money(pending.reserved),pending:pending.count,ordersToday:orders,haltReason:this.get('haltReason',null)};
  }
  checkAccount(account) {
    validateAccount(account,this.now());
    const knownOrders=new Set(this.db.prepare('SELECT order_id FROM live_intents WHERE order_id IS NOT NULL').all().map(r=>r.order_id));
    if(account.orders.some(o=>!knownOrders.has(o.id))) throw Error('Untracked open order');
    const inventory=this.db.prepare('SELECT * FROM live_inventory').all();
    const actual=new Map(account.positions.map(p=>[p.token,p]));
    if(actual.size!==account.positions.length) throw Error('Duplicate position rows');
    for(const p of inventory) if(!actual.has(p.token)||Math.abs(actual.get(p.token).shares-p.shares)>.00011) throw Error('Inventory reconciliation required');
    if(account.positions.some(p=>!inventory.some(i=>i.token===p.token))) throw Error('Untracked account position');
    if(Math.abs(account.cash-this.get('lastCash',account.cash))>.00002) throw Error('Cash reconciliation required');
  }
  reserve(plan,account,approval) {
    validatePlan(plan,this.now()); validateAccount(account,this.now());
    const id=planHash(plan);
    if(approval!==id) throw Error('Exact plan approval required');
    return this.db.transaction(()=>{
      if(this.mode()!=='manual' && !(this.mode()==='halted' && plan.side==='SELL')) throw Error('Live mode disabled or halted');
      if(this.db.prepare('SELECT 1 FROM live_intents WHERE id=?').get(id)) throw Error('Duplicate intent');
      this.checkAccount(account);
      const state=this.summary();
      if(state.pending) throw Error('An intent still needs reconciliation');
      const equity=account.cash+account.positions.reduce((sum,p)=>sum+p.bidValue,0);
      if(plan.side==='BUY') {
        if(state.ordersToday>=STARTER.ordersPerDay) throw Error('Daily order limit');
        if(state.dailyLoss>=STARTER.dailyLoss || equity<=this.get('baseline')-STARTER.drawdown) throw Error('Loss stop');
        const cost=worstCost(plan);
        if(cost>STARTER.order || cost>account.cash || cost>account.allowance) throw Error('Order/cash/allowance limit');
        if(state.exposure+state.reserved+cost>STARTER.exposure) throw Error('Total exposure limit');
        if(this.db.prepare('SELECT 1 FROM live_inventory WHERE event=?').get(plan.event)) throw Error('Correlated event exposure');
        if(state.dailyLoss+state.exposure+state.reserved+cost>STARTER.dailyLoss) throw Error('Remaining daily loss budget');
        if(account.cash-state.reserved-cost<this.get('baseline')-STARTER.drawdown) throw Error('Remaining drawdown budget');
      } else {
        const position=account.positions.find(p=>p.token===plan.token);
        if(!position || position.market!==plan.market || plan.shares>position.shares+.000001) throw Error('Sell exceeds owned inventory');
      }
      const reserve=plan.side==='BUY'?worstCost(plan):0;
      this.db.prepare('INSERT INTO live_intents(id,at,market,event,token,side,price,shares,reserve,state,plan) VALUES(?,?,?,?,?,?,?,?,?,?,?)')
        .run(id,new Date(this.now()).toISOString(),plan.market,plan.event,plan.token,plan.side,plan.price,plan.shares,reserve,'RESERVED',JSON.stringify(plan));
      this.event(id,'RESERVED',{reserve});
      return id;
    }).immediate();
  }
  signed(id,orderId) {
    if(!/^0x[0-9a-fA-F]{64}$/.test(orderId)) throw Error('Invalid order hash');
    this.db.transaction(()=>{
      const result=this.db.prepare("UPDATE live_intents SET state='SIGNED',order_id=? WHERE id=? AND state='RESERVED'").run(orderId,id);
      if(result.changes!==1) throw Error('Invalid signing transition');
      this.event(id,'SIGNED',{orderId});
    }).immediate();
  }
  sending(id,account) {
    validateAccount(account,this.now());
    this.db.transaction(()=>{
      const row=this.db.prepare('SELECT plan,side FROM live_intents WHERE id=?').get(id);
      if(!row) throw Error('Unknown intent');
      if(this.mode()!=='manual' && !(this.mode()==='halted' && row.side==='SELL')) throw Error('Stopped before submission');
      this.checkAccount(account);
      validatePlan(JSON.parse(row.plan),this.now());
      const r=this.db.prepare("UPDATE live_intents SET state='SENDING' WHERE id=? AND state='SIGNED'").run(id);
      if(r.changes!==1) throw Error('Intent already sent or invalid state');
      this.event(id,'SENDING');
    }).immediate();
  }
  acknowledge(id,orderId,status) {
    this.db.transaction(()=>{
      const row=this.db.prepare('SELECT * FROM live_intents WHERE id=?').get(id);
      if(!row||row.state!=='SENDING'||row.order_id!==orderId||!['OPEN','PARTIAL','FILLED'].includes(status)) throw Error('Unverifiable order acknowledgement');
      // Even FILLED acknowledgement remains pending until actual account balances match.
      this.db.prepare('UPDATE live_intents SET state=? WHERE id=?').run(status==='FILLED'?'OPEN':status,id);
      this.event(id,'ACKNOWLEDGED',{status});
    }).immediate();
  }
  unknown(id) {
    this.db.transaction(()=>{
      const r=this.db.prepare("UPDATE live_intents SET state='UNKNOWN',error='Outcome unknown; do not retry' WHERE id=? AND state IN ('SENDING','OPEN','PARTIAL','CANCEL_PENDING')").run(id);
      if(r.changes) {this.put('mode','halted');this.put('haltReason','Order outcome unknown; reconciliation required');this.event(id,'UNKNOWN');}
    }).immediate();
  }
  reject(id,reason,explicitExchangeRejection=false) {
    this.db.transaction(()=>{
      const row=this.db.prepare('SELECT state FROM live_intents WHERE id=?').get(id);
      if(row?.state==='SENDING' && explicitExchangeRejection!==true) throw Error('An uncertain submission cannot release its reservation');
      const r=this.db.prepare("UPDATE live_intents SET state='REJECTED',error=?,finished_at=? WHERE id=? AND state IN ('RESERVED','SIGNED','SENDING')")
        .run(String(reason).slice(0,200),new Date(this.now()).toISOString(),id);
      if(r.changes!==1) throw Error('Invalid rejection transition');
      this.event(id,'REJECTED');
    }).immediate();
  }
  recoverAfterRestart() {
    this.db.transaction(()=>{
      const uncertain=this.db.prepare("SELECT id FROM live_intents WHERE state IN ('SENDING','CANCEL_PENDING')").all();
      for(const row of uncertain) this.unknown(row.id);
      const unsent=this.db.prepare("SELECT id FROM live_intents WHERE state IN ('RESERVED','SIGNED')").all();
      for(const row of unsent) this.reject(row.id,'Process stopped before submission');
    }).immediate();
  }
  resume(account,actor) {
    validateAccount(account,this.now());
    if(actor?.kind!=='human-owner'||actor.id!==this.get('actor')) throw Error('Explicit owner action required');
    this.db.transaction(()=>{
      if(this.mode()!=='halted'||this.summary().pending) throw Error('Unresolved order or incompatible mode');
      this.checkAccount(account);
      if(this.summary().dailyLoss>=STARTER.dailyLoss) throw Error('Daily loss stop remains active');
      this.put('mode','manual');this.put('haltReason',null);this.event(null,'RESUMED');
    }).immediate();
  }
  cancelRequested(id) {
    this.db.transaction(()=>{
      const r=this.db.prepare("UPDATE live_intents SET state='CANCEL_PENDING' WHERE id=? AND state IN ('OPEN','PARTIAL','UNKNOWN')").run(id);
      if(r.changes!==1)throw Error('Cancellation not applicable');
      this.event(id,'CANCEL_REQUESTED');
    }).immediate();
  }
  confirmUnfilledCancellation(id,proof,account) {
    validateAccount(account,this.now());
    this.db.transaction(()=>{
      const row=this.db.prepare('SELECT * FROM live_intents WHERE id=?').get(id);
      if(!row||!['CANCEL_PENDING','UNKNOWN'].includes(row.state)||proof.orderId!==row.order_id||proof.authenticated!==true
        ||proof.status!=='CANCELED'||proof.sizeMatched!==0)throw Error('Cancellation not verified');
      if(account.orders.some(o=>o.id===row.order_id)||account.trades.some(t=>t.orderId===row.order_id&&t.status!=='FAILED'))throw Error('Cancellation raced with a fill');
      this.checkAccount(account);
      this.db.prepare("UPDATE live_intents SET state='CANCELLED',finished_at=? WHERE id=?").run(new Date(this.now()).toISOString(),id);
      this.event(id,'CANCEL_CONFIRMED_NO_FILL');
    }).immediate();
  }
  reconcileFill(id,proof,account) {
    validateAccount(account,this.now());
    finite(proof.shares,'filled shares',.000001); finite(proof.cash,'fill cash',.000001);
    if(proof.final!==true || proof.authenticated!==true || proof.accountReflected!==true) throw Error('Fill not confirmed and reflected');
    return this.db.transaction(()=>{
      const row=this.db.prepare('SELECT * FROM live_intents WHERE id=?').get(id);
      if(!row||!['SENDING','UNKNOWN','OPEN','PARTIAL','CANCEL_PENDING'].includes(row.state)||row.order_id!==proof.orderId) throw Error('Unknown fill');
      if(proof.shares>row.shares+.000001) throw Error('Fill exceeds request');
      if(account.orders.some(o=>o.id===row.order_id)) throw Error('Order still open');
      const before=this.get('lastCash');
      const position=account.positions.find(p=>p.token===row.token);
      const old=this.db.prepare('SELECT * FROM live_inventory WHERE token=?').get(row.token);
      if(row.side==='BUY') {
        if(proof.cash>row.reserve+.00001 || Math.abs(account.cash-(before-proof.cash))>.00002) throw Error('Buy cash mismatch');
        if(!position||Math.abs(position.shares-((old?.shares||0)+proof.shares))>.00011) throw Error('Buy inventory mismatch');
        this.db.prepare('INSERT OR REPLACE INTO live_inventory VALUES(?,?,?,?,?)').run(row.token,row.market,row.event,
          (old?.shares||0)+proof.shares,(old?.cost||0)+proof.cash);
      } else {
        if(!old||proof.shares>old.shares+.000001||Math.abs(account.cash-(before+proof.cash))>.00002) throw Error('Sell cash mismatch');
        const remaining=money(old.shares-proof.shares);
        if(Math.abs((position?.shares||0)-remaining)>.00011) throw Error('Sell inventory mismatch');
        const cost=old.cost*(proof.shares/old.shares);
        if(remaining>.000001) this.db.prepare('UPDATE live_inventory SET shares=?,cost=? WHERE token=?').run(remaining,money(old.cost-cost),row.token);
        else this.db.prepare('DELETE FROM live_inventory WHERE token=?').run(row.token);
        this.db.prepare('INSERT INTO live_realized VALUES(?,?,?)').run(id,new Date(this.now()).toISOString(),money(proof.cash-cost));
      }
      this.put('lastCash',account.cash);
      this.db.prepare("UPDATE live_intents SET state='CONFIRMED',filled_shares=?,actual_cash=?,finished_at=? WHERE id=?")
        .run(proof.shares,proof.cash,new Date(this.now()).toISOString(),id);
      this.event(id,'FILL_CONFIRMED',{shares:proof.shares,cash:proof.cash});
    }).immediate();
  }
}

module.exports={LiveLedger,STARTER,planHash,worstCost,validateAccount,validatePlan};
