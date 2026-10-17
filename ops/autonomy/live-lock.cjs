'use strict';
const fs=require('node:fs');
const {dirname}=require('node:path');
const {spawnSync}=require('node:child_process');

// Linux flock attaches to the shared open-file description. The parent retains
// this descriptor after the flock child exits; process death releases it.
function acquire(file) {
  if(process.platform!=='linux')throw Error('Manual live operations require Linux flock');
  fs.mkdirSync(dirname(file),{recursive:true,mode:0o700});
  const fd=fs.openSync(file,fs.constants.O_CREAT|fs.constants.O_RDWR|fs.constants.O_NOFOLLOW,0o600);
  try {
    if(!fs.fstatSync(fd).isFile())throw Error('Invalid lock file');
    const result=spawnSync('flock',['--nonblock','--conflict-exit-code','73','3'],
      {stdio:['ignore','pipe','pipe',fd],timeout:3000});
    if(result.status===73)throw Error('Another manual operator is active');
    if(result.error||result.status!==0)throw Error('Kernel lock unavailable');
    let closed=false;
    return ()=>{if(!closed){closed=true;fs.closeSync(fd);}};
  }catch(error){fs.closeSync(fd);throw error;}
}
module.exports={acquire};
