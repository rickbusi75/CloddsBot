#!/usr/bin/env node
'use strict';
// Human SSH-console entry point only. Never register it as a model tool or cron.
const fs=require('node:fs');
const {join}=require('node:path');
const {LiveLedger,STARTER,planHash,worstCost}=require('./live-ledger.cjs');
const {acquire}=require('./live-lock.cjs');

const HELP='Commands: check, status, preview <gammaId> <token> <BUY|SELL> <price> <shares>, activate, trade <same arguments>, cancel <intent>, reconcile [intent], stop, resume. Financial commands require an interactive human SSH terminal and exact confirmation. No --yes or piped approval.';
const FINANCIAL=new Set(['activate','trade','cancel','resume']);
function requestFrom(args) {
  if(args.length!==5||!/^\d{1,12}$/.test(args[0])||!/^\d{16,100}$/.test(args[1])
    ||!['BUY','SELL'].includes(args[2])||args.slice(3).some(v=>!/^(?:\d+(?:\.\d+)?|\.\d+)$/.test(v)))throw Error('Invalid explicit order arguments');
  return {gammaId:args[0],token:args[1],side:args[2],price:Number(args[3]),shares:Number(args[4])};
}
function publicAccount(a) {
  return {at:a.at,collateral:a.cash,openOrders:a.orders.length,openPositions:a.positions.length,
    trades:a.trades.length,exhaustive:a.exhaustive,geography:{blocked:a.geoblock.blocked,country:a.geoblock.country}};
}
function publicPreview(p) {
  return {mode:'PREVIEW_ONLY',question:p.marketQuestion,outcome:p.outcomeLabel,rules:p.marketRules,
    plan:p.plan,approval:p.approval,maximumCost:p.plan.side==='BUY'?worstCost(p.plan):null,
    expiresAt:p.plan.expiresAt,account:publicAccount(p.account),ordersSubmitted:0};
}
class LiveOperator {
  constructor({directory,reader,transportFactory,terminal,owner='private-ssh-owner',lock=acquire}) {
    this.file=join(directory,'live.sqlite');this.lockFile=join(directory,'live.lock');
    this.reader=reader;this.transportFactory=transportFactory;this.terminal=terminal;this.lock=lock;
    this.actor={kind:'human-owner',id:owner};
  }
  localStatus(account) {
    if(!fs.existsSync(this.file))return {mode:'disabled',initialized:false,limits:STARTER,pending:0};
    const ledger=LiveLedger.openReadOnly(this.file);
    try{return ledger.db.transaction(()=>{
      const summary=ledger.summary();
      const matches=account?ledger.get('accountIdentity')===account.identity:null;
      let reconciled=false;
      if(matches&&!summary.pending)try{ledger.checkAccount(account);reconciled=true;}catch{}
      return {initialized:ledger.get('baseline')!==undefined,...summary,accountMatchesLedger:matches,accountReconciled:reconciled,
        pendingIntents:ledger.db.prepare("SELECT id,state,side,order_id AS orderId FROM live_intents WHERE state IN ('RESERVED','SIGNED','SENDING','UNKNOWN','OPEN','PARTIAL','CANCEL_PENDING')").all()};
    })();}
    finally{ledger.close();}
  }
  async confirm(phrase,report) {
    if(this.terminal.interactive!==true)throw Error('Interactive human terminal required');
    this.terminal.write(report);
    if(await this.terminal.ask(phrase)!==phrase)throw Error('Exact human confirmation required');
  }
  async run(command,args=[]) {
    if(command==='help')return {help:HELP};
    if(FINANCIAL.has(command)&&this.terminal.interactive!==true)throw Error('Interactive human terminal required');
    const order=['preview','trade'].includes(command)?requestFrom(args):null;
    if(!order&&(!['check','status','activate','cancel','reconcile','stop','resume'].includes(command)
      ||(command==='cancel'&&args.length!==1)||(command==='reconcile'&&args.length>1)
      ||(!['cancel','reconcile'].includes(command)&&args.length)))throw Error('Invalid operator command');
    if(['cancel','reconcile'].includes(command)&&args.some(id=>!/^([0-9a-f]{64})$/.test(id)))throw Error('Invalid intent ID');
    if(command==='check')return {mode:'READ_ONLY_PREFLIGHT',account:publicAccount(await this.reader.account()),ordersSubmitted:0};
    if(command==='status') {
      let account;
      try{account=await this.reader.account();}
      catch{return {ledger:this.localStatus(),account:{known:false},warning:'Account unavailable; do not submit or assume flat',ordersSubmitted:0};}
      const local=this.localStatus(account);
      return {ledger:local,account:{known:true,...publicAccount(account)},ordersSubmitted:0,
        warning:local.initialized&&!local.accountReconciled?'Account or pending orders require reconciliation before any new entry':null};
    }
    if(command==='preview')return publicPreview(await this.reader.preview(order));
    // stop remains available while another operator holds the network/TTY lock.
    if(command==='stop') {
      if(!fs.existsSync(this.file))return {mode:'disabled',initialized:false,ordersSubmitted:0};
      const ledger=new LiveLedger(this.file);
      try {ledger.halt('Owner console stop');return {mode:ledger.mode(),warning:'Entries stopped; existing orders require reconciliation/cancellation. Explicit owner exits remain possible.'};}
      finally{ledger.close();}
    }
    const release=this.lock(this.lockFile);
    let ledger;
    try {
      if(command!=='activate'&&!fs.existsSync(this.file))throw Error('Live ledger is not activated');
      if(command==='activate') {
        if(this.localStatus().initialized)throw Error('An existing ledger requires review; activation cannot reset it');
        await this.confirm('ACTIVER TEST MANUEL 20 PUSD',{mode:'DISABLED',limits:STARTER,
          warning:'Activation of a manual test ledger only. Every order needs separate confirmation. This does not change native bot simulation flags.'});
        const account=await this.reader.account();
        ledger=new LiveLedger(this.file);ledger.activate(account,this.actor);
        return {ledger:ledger.summary(),ordersSubmitted:0};
      }
      ledger=new LiveLedger(this.file);ledger.recoverAfterRestart();
      if(ledger.get('actor')!==this.actor.id)throw Error('Owner does not match the ledger');
      const transport=this.transportFactory(ledger);
      if(command==='trade') {
        const preview=await this.reader.preview(order);
        await this.confirm('CONFIRMER '+planHash(preview.plan),publicPreview(preview));
        // Transport repeats fresh account/rules/fees/expiry checks before signing.
        return await transport.submit(preview,planHash(preview.plan),this.actor);
      }
      if(command==='cancel') {
        const row=ledger.db.prepare('SELECT id,order_id AS orderId,state FROM live_intents WHERE id=?').get(args[0]);
        if(!row?.orderId)throw Error('Unknown owned intent');
        await this.confirm('ANNULER '+args[0],row);
        return await transport.cancel(args[0],this.actor);
      }
      if(command==='resume') {
        await this.confirm('REPRENDRE TEST MANUEL',{ledger:ledger.summary(),warning:'Loss limits and pending reservations will not reset.'});
        ledger.resume(await this.reader.account(),this.actor);
        return {ledger:ledger.summary(),ordersSubmitted:0};
      }
      const ids=args.length?args:ledger.db.prepare("SELECT id FROM live_intents WHERE state IN ('UNKNOWN','OPEN','PARTIAL','CANCEL_PENDING') ORDER BY at LIMIT 4").all().map(r=>r.id);
      const results=[];
      for(const id of ids)results.push(await transport.reconcile(id));
      return {results,ledger:ledger.summary(),ordersSubmitted:0};
    }finally{if(ledger)ledger.close();release();}
  }
}

async function main(argv=process.argv.slice(2)) {
  const command=argv[0]||'help';
  if(command==='help'){console.log(HELP);return;}
  const interactive=process.stdin.isTTY===true&&process.stdout.isTTY===true;
  // Refuse financial operations before loading any credentials.
  if(FINANCIAL.has(command)&&!interactive)throw Error('Interactive human terminal required');
  const write=value=>console.log(JSON.stringify(value,null,2).replace(/[\u007f-\u009f\u202a-\u202e\u2066-\u2069]/g,
    c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0')));
  let rl;
  const terminal={interactive,write,ask:async phrase=>{
    rl??=require('node:readline/promises').createInterface({input:process.stdin,output:process.stdout});
    return rl.question('Saisir exactement « '+phrase+' » : ',{signal:AbortSignal.timeout(50000)});
  }};
  let auth,reader;
  const getReader=()=>{
    if(!reader) {
      const env=require('dotenv').parse(fs.readFileSync('/app/.env'));
      auth={address:env.POLY_FUNDER_ADDRESS,privateKey:env.POLY_PRIVATE_KEY,apiKey:env.POLY_API_KEY,
        apiSecret:env.POLY_API_SECRET,apiPassphrase:env.POLY_API_PASSPHRASE,signatureType:Number(env.POLY_SIGNATURE_TYPE)};
      reader=new (require('./live-reader.cjs').PolymarketReader)(auth);
    }
    return reader;
  };
  const lazyReader={account:(...a)=>getReader().account(...a),preview:(...a)=>getReader().preview(...a)};
  const operator=new LiveOperator({directory:'/data/autonomy',reader:lazyReader,terminal,
    transportFactory:ledger=>new (require('./live-transport.cjs').LiveTransport)(ledger,getReader(),auth)});
  try{write(await operator.run(command,argv.slice(1)));}finally{rl?.close();}
}
if(require.main===module)main().catch(()=>{console.error('Opération refusée ou incomplète. Aucun renvoi automatique. Consulter status/reconcile avant tout nouvel ordre.');process.exitCode=1;});
module.exports={LiveOperator,requestFrom,main};
