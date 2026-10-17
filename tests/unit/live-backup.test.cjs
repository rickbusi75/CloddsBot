'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {mkdtempSync,rmSync,statSync,writeFileSync}=require('node:fs');
const {join,dirname,resolve}=require('node:path');
const {tmpdir}=require('node:os');
const {spawnSync}=require('node:child_process');
const Sqlite=require('better-sqlite3');
const {LiveLedger,planHash}=require('../../ops/autonomy/live-ledger.cjs');
function fixture(t) {
  const root=mkdtempSync(join(tmpdir(),'clodds-live-backup-'));assert.equal(dirname(root),tmpdir());
  t.after(()=>rmSync(root,{recursive:true,force:true}));
  const now=Date.parse('2026-10-17T12:00:00Z');
  const source=join(root,'data/cloddsbot/autonomy/live.sqlite');
  const ledger=new LiveLedger(source,()=>now);
  const account={at:new Date(now).toISOString(),cash:20.284834,allowance:100,positions:[],orders:[],trades:[],exhaustive:true,geoblock:{blocked:false,country:'ES'}};
  ledger.activate(account,{kind:'human-owner',id:'unit-test-only'});
  const plan={market:'market',event:'event',token:'token',side:'BUY',price:.2,shares:6,feeRate:.04,
    strategy:'explicit-human-test',rulesHash:'a'.repeat(64),expiresAt:new Date(now+60000).toISOString()};
  const id=ledger.reserve(plan,account,planHash(plan));ledger.signed(id,'0x'+'a'.repeat(64));ledger.sending(id,account);
  const observer=new Sqlite(join(root,'data/cloddsbot/autonomy/observer.sqlite'));
  observer.exec('CREATE TABLE evidence(id INTEGER PRIMARY KEY); INSERT INTO evidence VALUES(1)');observer.close();
  const run=()=>spawnSync('python3',[resolve(__dirname,'../../ops/autonomy/backup-observer.py'),'--root',root],{encoding:'utf8'});
  return {root,source,ledger,run};
}
test('coherent backup with a live WAL retains reservations and an uncertain order after restore',t=>{
  const s=fixture(t);
  try {
    const result=s.run();assert.equal(result.status,0,result.stderr);
    const target=join(s.root,'dumps/cloddsbot/live.sqlite');
    if(process.platform!=='win32')assert.equal(statSync(target).mode&0o777,0o600);
    const restored=new LiveLedger(target);
    try {
      assert.equal(restored.db.pragma('integrity_check',{simple:true}),'ok');
      assert.equal(restored.summary().reserved,1.23841);assert.equal(restored.summary().pending,1);
      restored.recoverAfterRestart();assert.equal(restored.mode(),'halted');
      assert.equal(restored.summary().reserved,1.23841);
    }finally{restored.close();}
    const research=new Sqlite(join(s.root,'dumps/cloddsbot/observer.sqlite'),{readonly:true});
    try{assert.equal(research.prepare('SELECT COUNT(*) count FROM evidence').get().count,1);}finally{research.close();}
  }finally{s.ledger.close();}
});
test('a corrupt source cannot overwrite the previous valid live snapshot',t=>{
  const s=fixture(t);
  assert.equal(s.run().status,0);s.ledger.close();
  writeFileSync(s.source,'invalid sqlite source');
  assert.notEqual(s.run().status,0);
  const prior=new Sqlite(join(s.root,'dumps/cloddsbot/live.sqlite'),{readonly:true});
  try {
    assert.equal(prior.pragma('integrity_check',{simple:true}),'ok');
    assert.equal(prior.prepare('SELECT COUNT(*) count FROM live_intents').get().count,1);
  }finally{prior.close();}
});

test('hard process termination after durable SENDING cannot erase or replay a possible submission',t=>{
  const s=fixture(t);s.ledger.close();
  const modulePath=resolve(__dirname,'../../ops/autonomy/live-ledger.cjs');
  const child=spawnSync(process.execPath,['-e',`
    const {LiveLedger}=require(${JSON.stringify(modulePath)});
    const ledger=new LiveLedger(${JSON.stringify(s.source)});
    if(ledger.summary().pending!==1)process.exit(2);
    ledger.event(null,'SIMULATED_EXCHANGE_ACCEPTANCE');
    process.kill(process.pid,'SIGKILL');
  `],{encoding:'utf8'});
  assert.notEqual(child.status,0);
  const restored=new LiveLedger(s.source);
  try {
    assert.equal(restored.db.pragma('integrity_check',{simple:true}),'ok');
    assert.equal(restored.db.prepare('SELECT state FROM live_intents').get().state,'SENDING');
    assert.equal(restored.db.prepare("SELECT COUNT(*) n FROM live_events WHERE kind='SIMULATED_EXCHANGE_ACCEPTANCE'").get().n,1);
    restored.recoverAfterRestart();
    assert.equal(restored.mode(),'halted');assert.equal(restored.summary().pending,1);
    assert.equal(restored.summary().reserved,1.23841);
    assert.equal(restored.db.prepare('SELECT state FROM live_intents').get().state,'UNKNOWN');
  }finally{restored.close();}
});
