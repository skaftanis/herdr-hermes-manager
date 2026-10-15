import {test} from 'node:test';
import assert from 'node:assert/strict';
import {canonical,rosterItem,TITLE} from '../hermes_manager/bots_bridge.mjs';

test('existing hidden canonical chat and compression tip are reused without a model prompt', async () => {
  const calls=[];
  const row={id:'root',resolved_id:'tip',root_title:TITLE,title:'compressed'};
  const rpc={call:async(method,params)=>{calls.push([method,params]);return {sessions:[row]};}};
  assert.equal(await canonical(rpc,'alpha',{id:'root'}),row);
  assert.deepEqual(calls,[['session.list',{profile:'alpha',title:TITLE,include_hidden:true,limit:200}]]);
});
test('failed registry lookup and unconfirmed absence never fork a known conversation', async()=>{
  await assert.rejects(canonical({call:async()=>{throw new Error('offline');}},'alpha',{id:'root'}),/offline/);
  await assert.rejects(canonical({call:async()=>({sessions:[]})},'alpha',{id:'root'}),/existing Bot Chat/);
});
test('first creation materializes native hidden title and verifies it; no prompt submitted', async()=>{
  const calls=[];let lists=0;
  const rpc={call:async(method,params)=>{
    calls.push([method,params]);
    if(method==='session.list') return {sessions:++lists>2?[{id:'winner',title:TITLE}]:[]};
    if(method==='session.create')return {session_id:'runtime',stored_session_id:'new'};
    return {title:TITLE};
  }};
  assert.equal((await canonical(rpc,'beta')).id,'winner');
  assert.ok(calls.every(([,p])=>p.profile==='beta'));
  assert.equal(calls.find(([m])=>m==='session.create')[1].hidden,true);
  assert.equal(calls.find(([m])=>m==='session.create')[1].follow_profile_config,true);
  assert.ok(!calls.some(([m])=>m==='prompt.submit'));
});
test('a concurrent canonical winner is adopted on the second lookup', async()=>{
  let n=0;
  const rpc={call:async(method)=>{assert.equal(method,'session.list');return {sessions:++n===1?[]:[{id:'desktop-winner',title:TITLE}]};}};
  assert.equal((await canonical(rpc,'alpha')).id,'desktop-winner');
});
test('bot icons use shared unread state and sanitize labels',()=>{
  const row=rosterItem({name:'alpha',ui_meta:{'hermes-bots':{title:'Test\x1bBot',shape:'star'}}},{unread:true});
  assert.equal(row.id,'alpha');assert.equal(row.icon,'✦');assert.equal(row.unread,true);
  assert.ok(!row.label.includes('\x1b'));
});
