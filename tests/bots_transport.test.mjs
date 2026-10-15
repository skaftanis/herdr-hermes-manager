// Exercise the entire frontend bridge against a disposable native-protocol peer.
// The real Hermes model/gateway is never prompted.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createRequire} from 'node:module';
import {spawn} from 'node:child_process';
import readline from 'node:readline';
import {once} from 'node:events';

const require=createRequire(import.meta.url);
const {WebSocketServer}=require('ws');

test('popup reuses native history, sends to captured profile, waits for approval, streams and clears shared badge',async()=>{
  const temp=fs.mkdtempSync(path.join(os.tmpdir(),'hermes-bots-transport-'));
  const calls=[],patches=[],history=[{role:'user',text:'Existing Desktop conversation'},{role:'assistant',text:'Existing reply'}];
  let wsPeer,submitted=false;
  const server=http.createServer((req,res)=>{
    if(req.method==='GET')return res.end('<script>window.__HERMES_SESSION_TOKEN__="fixture-only";</script>');
    let body='';req.on('data',chunk=>body+=chunk);req.on('end',()=>{
      patches.push(JSON.parse(body));res.setHeader('Content-Type','application/json');res.end('{"ok":true}');
    });
  });
  const wss=new WebSocketServer({server});
  wss.on('connection',ws=>{
    wsPeer=ws;
    ws.on('message',bytes=>{
      const f=JSON.parse(bytes.toString());
      if(f.id==='srq-fixture' && !f.method){
        assert.equal(f.result.choice,'once');
        history.push({role:'assistant',text:'A streamed reply'});
        ws.send(JSON.stringify({method:'event',params:{type:'message.delta',session_id:'runtime-alpha',payload:{text:'A streamed '}}})+'\n'+
                JSON.stringify({method:'event',params:{type:'message.complete',session_id:'runtime-alpha',payload:{text:'A streamed reply'}}}));
        return;
      }
      calls.push(f);
      const reply=result=>ws.send(JSON.stringify({jsonrpc:'2.0',id:f.id,result}));
      if(f.method==='profiles.list')reply({profiles:[{name:'alpha',ui_meta:{'hermes-bots':{shape:'star'}},canonical_session:{id:'root-alpha'}}]});
      else if(f.method==='session.list'){
        assert.equal(f.params.profile,'alpha');assert.equal(f.params.title,'Bot Chat');assert.equal(f.params.include_hidden,true);
        reply({sessions:[{id:'root-alpha',resolved_id:'tip-alpha',root_title:'Bot Chat',title:'compressed'}]});
      }else if(f.method==='session.resume'){
        assert.equal(f.params.profile,'alpha');assert.equal(f.params.session_id,'tip-alpha');
        reply({session_id:'runtime-alpha',stored_session_id:'tip-alpha',messages:history,message_count:history.length,info:{model:'fixture'}});
      }else if(f.method==='prompt.submit'){
        assert.equal(f.params.profile,'alpha');assert.equal(f.params.session_id,'runtime-alpha');assert.equal(f.params.text,'Test input');
        submitted=true;history.push({role:'user',text:f.params.text});reply({status:'streaming'});
        ws.send(JSON.stringify({id:'srq-fixture',method:'approval',params:{session_id:'runtime-alpha',command:'fixture command',choices:['once','deny']}}));
      }else if(f.method==='slash.exec'){
        assert.equal(f.params.command,'/compress');assert.equal(f.params.session_id,'runtime-alpha');reply({output:'Compacted same chat'});
      }else reply({});
    });
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const port=server.address().port;
  fs.writeFileSync(path.join(temp,'spawn-ledger.json'),JSON.stringify([{pid:process.pid,purpose:'serve',host:'127.0.0.1',port}]));
  const config={root:temp,agent_root:temp,state_dir:temp,profiles:['alpha'],labels:{alpha:'Alpha'},hermes_bin:'/not-used'};
  const configFile=path.join(temp,'config.json');fs.writeFileSync(configFile,JSON.stringify(config));
  const child=spawn(process.execPath,['hermes_manager/bots_bridge.mjs','chat',configFile,'alpha'],{stdio:['pipe','pipe','pipe']});
  let errors='';child.stderr.on('data',b=>errors+=b);
  const frames=[];let resolveFrame;
  const reader=readline.createInterface({input:child.stdout});
  reader.on('line',line=>{frames.push(JSON.parse(line));resolveFrame?.();});
  const take=async predicate=>{
    for(let i=0;i<100;i++){
      const idx=frames.findIndex(predicate);if(idx>=0)return frames.splice(idx,1)[0];
      await new Promise(resolve=>{resolveFrame=resolve;setTimeout(resolve,50);});
    }
    throw new Error('Timed out; '+errors+' frames='+JSON.stringify(frames));
  };
  const send=x=>child.stdin.write(JSON.stringify(x)+'\n');
  try{
    const first=await take(f=>f.type==='session');assert.equal(first.session.messages[0].text,'Existing Desktop conversation');
    assert.equal(submitted,false);assert.ok(!calls.some(f=>f.method==='session.create'));
    send({type:'send',text:'Test input'});
    await take(f=>f.type==='request');
    await new Promise(resolve=>setTimeout(resolve,100));
    assert.ok(!frames.some(f=>f.event?.type==='message.complete'),'approval must wait for user');
    send({type:'reply',id:'srq-fixture',result:{choice:'once'}});
    const done=await take(f=>f.event?.type==='message.complete');assert.equal(done.event.payload.text,'A streamed reply');
    send({type:'send',text:'/new'});await take(f=>f.type==='slash');
    send({type:'refresh'});const refresh=await take(f=>f.type==='session');assert.equal(refresh.session.messages.length,4);
    assert.ok(patches.length>=1);assert.ok(patches.every(p=>p.profile==='alpha' && p.unread===false));
    assert.equal(calls.filter(f=>f.method==='prompt.submit').length,1);
  }finally{
    child.stdin.end();await Promise.race([once(child,'exit'),new Promise(r=>setTimeout(r,1000))]);
    if(child.exitCode===null)child.kill();
    wss.clients.forEach(ws=>ws.terminate());await new Promise(r=>wss.close(r));await new Promise(r=>server.close(r));
    fs.rmSync(temp,{recursive:true,force:true});
  }
});
