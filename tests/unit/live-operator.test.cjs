'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {join,resolve}=require('node:path');
const {tmpdir}=require('node:os');
const {spawn}=require('node:child_process');
const {once}=require('node:events');
const {LiveLedger,planHash}=require('../../ops/autonomy/live-ledger.cjs');
const {LiveOperator,requestFrom,main}=require('../../ops/autonomy/live-operator.cjs');
const {acquire}=require('../../ops/autonomy/live-lock.cjs');
function fixture(t) {
  const dir=fs.mkdtempSync(join(tmpdir(),'clodds-operator-'));
  t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
  const owner='test-human-owner',file=join(dir,'live.sqlite'),shown=[];
  let overrides={},reads=0,submitted=0,reconciled=0;
  const args=['12345','12345678901234567890','BUY','.2','6'];
  const account=()=>({at:new Date().toISOString(),identity:'c'.repeat(64),cash:20.284834,allowance:100,
    positions:[],orders:[],trades:[],exhaustive:true,geoblock:{blocked:false,country:'ES'},...overrides});
  const reader={account:async()=>{reads++;return account();},preview:async request=>{
    reads++;const plan={market:'market',event:'event',token:request.token,side:request.side,
      price:request.price,shares:request.shares,feeRate:.04,strategy:'explicit-human-test',rulesHash:'a'.repeat(64),
      expiresAt:new Date(Date.now()+60000).toISOString()};
    return {plan,approval:planHash(plan),account:account(),request,marketQuestion:'Fixture only',
      outcomeLabel:'Yes',marketRules:'Fixture resolution rules; no real market.'};
  }};
  const terminal={interactive:true,write:value=>shown.push(value),ask:async phrase=>phrase};
  const factory=ledger=>({sync:async()=>{},activate:async(a,o)=>ledger.activate(a,o),resume:async(a,o)=>ledger.resume(a,o),submit:async(p,approval,actor)=>{
    assert.equal(actor.id,owner);const id=ledger.reserve(p.plan,p.account,approval);
    ledger.signed(id,'0x'+'a'.repeat(64));ledger.sending(id,account());
    ledger.acknowledge(id,'0x'+'a'.repeat(64),'OPEN');submitted++;
    return {intent:id,state:'AWAITING_RECONCILIATION'};
  },reconcile:async id=>{reconciled++;return {intent:id,state:'AWAITING_CONFIRMATION'};},
  cancel:async()=>{throw Error('Unexpected test cancellation');}});
  const operator=new LiveOperator({directory:dir,reader,transportFactory:factory,terminal,owner});
  return {dir,file,args,reader,terminal,operator,shown,account,owner,
    setAccount:a=>{overrides=a;},counts:()=>({reads,submitted,reconciled})};
}

test('status, unavailable account and preview never create or activate a ledger',async t=>{
  const s=fixture(t);let status=await s.operator.run('status');
  assert.equal(status.ledger.mode,'disabled');assert.equal(status.account.known,true);
  s.reader.account=async()=>{throw Error('upstream unavailable');};
  status=await s.operator.run('status');assert.equal(status.account.known,false);
  assert.equal(Object.hasOwn(status.account,'collateral'),false);
  const preview=await s.operator.run('preview',s.args);
  assert.equal(preview.outcome,'Yes');assert.equal(preview.ordersSubmitted,0);
  assert.equal(fs.existsSync(s.file),false);assert.equal(fs.existsSync(join(s.dir,'live.lock')),false);
});

test('noninteractive financial commands and --yes fail before credentials or ledger access',async t=>{
  const s=fixture(t);s.terminal.interactive=false;
  for(const command of ['activate','trade','resume','cancel'])await assert.rejects(s.operator.run(command,s.args),/Interactive/);
  await assert.rejects(main(['activate']),/Interactive/);
  assert.equal(s.counts().reads,0);assert.equal(fs.existsSync(s.file),false);
  assert.throws(()=>requestFrom([...s.args,'--yes']),/arguments/);
  for(const args of [[...s.args.slice(0,3),'NaN','6'],[...s.args.slice(0,3),'1e-2','6']])assert.throws(()=>requestFrom(args));
});

test('activation and each order require separate exact confirmation; acknowledgement retains risk',async t=>{
  const s=fixture(t);s.terminal.ask=async()=>'yes';
  await assert.rejects(s.operator.run('activate'),/Exact human/);assert.equal(fs.existsSync(s.file),false);
  s.terminal.ask=async phrase=>phrase;
  assert.equal((await s.operator.run('activate')).ledger.mode,'manual');
  assert.equal(s.counts().submitted,0);
  s.terminal.ask=async()=>'CONFIRMER';
  await assert.rejects(s.operator.run('trade',s.args),/Exact human/);assert.equal(s.counts().submitted,0);
  s.terminal.ask=async phrase=>phrase;
  const result=await s.operator.run('trade',s.args);assert.equal(result.state,'AWAITING_RECONCILIATION');
  assert.equal(s.counts().submitted,1);assert.equal(s.operator.localStatus().pending,1);
  await assert.rejects(s.operator.run('activate'),/existing ledger/);
});

test('an unsuccessful activation can be corrected without resetting any baseline',async t=>{
  const s=fixture(t);s.setAccount({cash:19});
  await assert.rejects(s.operator.run('activate'),/capital unavailable/);
  assert.equal(s.operator.localStatus().initialized,false);
  s.setAccount({});assert.equal((await s.operator.run('activate')).ledger.baseline,20.284834);
  assert.equal(s.counts().submitted,0);
});

test('status flags a wallet switch or external cash movement without modifying the ledger',async t=>{
  const s=fixture(t);await s.operator.run('activate');
  const baseline=fs.readFileSync(s.file);
  s.setAccount({identity:'d'.repeat(64)});
  let result=await s.operator.run('status');
  assert.equal(result.ledger.accountMatchesLedger,false);assert.equal(result.ledger.accountReconciled,false);
  s.setAccount({cash:21});result=await s.operator.run('status');
  assert.equal(result.ledger.accountMatchesLedger,true);assert.equal(result.ledger.accountReconciled,false);
  assert.equal(s.counts().submitted,0);assert.deepEqual(fs.readFileSync(s.file),baseline);
});

test('owner stop during confirmation prevents submission; another operator cannot recover an active attempt',async t=>{
  const s=fixture(t);await s.operator.run('activate');
  let answer;
  s.terminal.ask=phrase=>new Promise(resolve=>{answer=()=>resolve(phrase);});
  const pending=s.operator.run('trade',s.args);
  await new Promise(resolve=>setImmediate(resolve));
  const other=new LiveOperator({directory:s.dir,reader:s.reader,transportFactory:()=>({}),terminal:s.terminal,owner:s.owner});
  await assert.rejects(other.run('reconcile'),/operator is active/);
  assert.equal((await other.run('stop')).mode,'halted');
  answer();await assert.rejects(pending,/halted/);
  assert.equal(s.counts().submitted,0);assert.equal(s.operator.localStatus().pending,0);
});

test('reconciliation after process loss halts an uncertain order without sending or resetting it',async t=>{
  const s=fixture(t);await s.operator.run('activate');
  const p=(await s.reader.preview(requestFrom(s.args))).plan;
  const ledger=new LiveLedger(s.file),id=ledger.reserve(p,s.account(),planHash(p));
  ledger.signed(id,'0x'+'a'.repeat(64));ledger.sending(id,s.account());ledger.close();
  s.terminal.interactive=false;
  const result=await s.operator.run('reconcile');
  assert.equal(result.ledger.mode,'halted');assert.equal(result.ledger.pending,1);
  assert.equal(result.ledger.reserved,1.23841);assert.equal(s.counts().submitted,0);assert.equal(s.counts().reconciled,1);
});

test('kernel ownership rejects another process and releases after SIGKILL without stale-file deletion',
  {skip:process.platform!=='linux'},async t=>{
    const s=fixture(t),file=join(s.dir,'process.lock'),modulePath=resolve(__dirname,'../../ops/autonomy/live-lock.cjs');
    const child=spawn(process.execPath,['-e',`require(${JSON.stringify(modulePath)}).acquire(${JSON.stringify(file)});process.stdout.write('held');setInterval(()=>{},1000);`],{stdio:['ignore','pipe','pipe']});
    t.after(()=>{if(child.exitCode===null)child.kill('SIGKILL');});
    await once(child.stdout,'data',{signal:AbortSignal.timeout(5000)});
    assert.throws(()=>acquire(file),/operator is active/);
    const inode=fs.statSync(file).ino,exited=once(child,'exit');child.kill('SIGKILL');await exited;
    const release=acquire(file);release();assert.equal(fs.statSync(file).ino,inode);
  });
