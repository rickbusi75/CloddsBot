'use strict';
// Offline compatibility checks for security upgrades. No credentials, exchange
// requests, SMTP connection, or real message delivery.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {mkdtemp,writeFile,unlink,rm}=require('node:fs/promises');
const {tmpdir}=require('node:os');
const {join}=require('node:path');
const {once}=require('node:events');
const {spawnSync}=require('node:child_process');

test('literal skill directories still report add, change and unlink after the watcher upgrade',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'clodds-watch-security-'));
  const watcher=require('chokidar').watch(dir,{ignoreInitial:true,
    awaitWriteFinish:{stabilityThreshold:50,pollInterval:10}});
  const file=join(dir,'SKILL.md');
  const wait=event=>once(watcher,event,{signal:AbortSignal.timeout(5000)});
  try {
    await wait('ready');
    let event=wait('add');await writeFile(file,'first');assert.equal((await event)[0],file);
    event=wait('change');await writeFile(file,'updated content');assert.equal((await event)[0],file);
    event=wait('unlink');await unlink(file);assert.equal((await event)[0],file);
  }finally{await watcher.close();await rm(dir,{recursive:true,force:true});}
});

test('patched image parser still decodes SVG and resizes PNG with the supported media API',async()=>{
  const sharp=require('sharp');
  const svg=Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"><rect width="16" height="16" fill="red"/></svg>');
  const output=await sharp(svg).resize(8,8).png().toBuffer();
  const metadata=await sharp(output).metadata();
  assert.equal(metadata.width,8);assert.equal(metadata.height,8);assert.equal(metadata.format,'png');
  const [major,minor,patch]=sharp.versions.rsvg.split('.').map(Number);
  assert(major>2||(major===2&&(minor>63||(minor===63&&patch>=2))),'Patched librsvg required');
});

test('email transport preserves addressing and body without opening an SMTP connection',async()=>{
  const transport=require('nodemailer').createTransport({jsonTransport:true});
  const result=await transport.sendMail({from:'sender@example.invalid',to:'receiver@example.invalid',
    subject:'Offline test',text:'No real message is sent.'});
  const message=JSON.parse(result.message);
  assert.equal(message.to[0].address,'receiver@example.invalid');assert.equal(message.text,'No real message is sent.');
  transport.close();
});

test('CLI banner with a narrow whitespace-breaking width terminates',()=>{
  const result=spawnSync(process.execPath,['-e',
    "const f=require('figlet');f.textSync('Clodds Bot',{width:1,whitespaceBreak:true});"],{timeout:3000,encoding:'utf8'});
  assert.equal(result.status,0,result.stderr);assert.equal(result.error,undefined);
});

test('patched audio metadata parser still recognizes a local PCM WAV without network access',async()=>{
  const wav=Buffer.alloc(46);
  wav.write('RIFF',0);wav.writeUInt32LE(38,4);wav.write('WAVEfmt ',8);wav.writeUInt32LE(16,16);
  wav.writeUInt16LE(1,20);wav.writeUInt16LE(1,22);wav.writeUInt32LE(8000,24);
  wav.writeUInt32LE(16000,28);wav.writeUInt16LE(2,32);wav.writeUInt16LE(16,34);
  wav.write('data',36);wav.writeUInt32LE(2,40);
  const {parseBuffer}=await import('music-metadata');
  const parsed=await parseBuffer(wav,{mimeType:'audio/wav',size:wav.length});
  assert.equal(parsed.format.sampleRate,8000);assert.equal(parsed.format.numberOfChannels,1);
});

test('exchange clients retain constructor and read methods without making requests',()=>{
  const binance=new (require('binance').USDMClient)({});
  const bybit=new (require('bybit-api').RestClientV5)({});
  assert.equal(typeof binance.getBalance,'function');
  assert.equal(typeof bybit.getWalletBalance,'function');
  assert.equal(typeof require('@grpc/grpc-js').credentials.createSsl,'function');
});
