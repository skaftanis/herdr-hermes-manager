import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
import {spawn,spawnSync} from 'node:child_process';
import readline from 'node:readline';
import net from 'node:net';
import {pathToFileURL,fileURLToPath} from 'node:url';

export const TITLE = 'Bot Chat';
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const emit = value => process.stdout.write(JSON.stringify(value) + '\n');
const clean = value => String(value ?? '').replace(/[\x00-\x1f\x7f]/g, ' ').slice(0, 100);
const atomic = (file, value) => {
  fs.mkdirSync(path.dirname(file), {recursive: true, mode: 0o700});
  const tmp = file + '.' + crypto.randomUUID();
  fs.writeFileSync(tmp, JSON.stringify(value), {mode: 0o600});
  fs.renameSync(tmp, file);
};

export function candidates(root) {
  let rows;
  try { rows = JSON.parse(fs.readFileSync(path.join(root, 'spawn-ledger.json'), 'utf8')); }
  catch { return []; }
  if (!Array.isArray(rows)) return [];
  return rows.filter(r => ['serve', 'dashboard'].includes(r.purpose) && r.isolated !== true &&
    ['', 'localhost', '127.0.0.1', '0.0.0.0', '::', '::1'].includes(r.host ?? '') &&
    Number.isInteger(r.port) && r.port > 0 && r.port <= 65535)
    .sort((a,b) => (b.registered_at ?? 0) - (a.registered_at ?? 0));
}

class RPC {
  constructor(ws) {
    this.ws = ws; this.pending = new Map(); this.onEvent = () => {}; this.onRequest = () => {};
    ws.on('message', bytes => {
      // Hermes can batch newline-delimited streaming frames into one WS frame.
      for (const line of bytes.toString().split('\n').filter(Boolean)) {
        let frame; try { frame = JSON.parse(line); } catch { continue; }
        if (frame.id != null && this.pending.has(frame.id)) {
          const p = this.pending.get(frame.id); this.pending.delete(frame.id); clearTimeout(p.timer);
          frame.error ? p.reject(new Error(frame.error.message)) : p.resolve(frame.result);
        } else if (frame.method === 'event') this.onEvent(frame.params);
        else if (frame.method && frame.id != null) this.onRequest(frame);
      }
    });
    ws.on('error', () => {});
    ws.on('close', () => {
      for (const p of this.pending.values()) { clearTimeout(p.timer); p.reject(new Error('Hermes disconnected')); }
      this.pending.clear();
    });
  }
  call(method, params = {}, timeout = 45000) {
    const id = crypto.randomUUID();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error(`${method} timed out`)); }, timeout);
      this.pending.set(id, {resolve, reject, timer});
      this.ws.send(JSON.stringify({jsonrpc:'2.0', id, method, params}), err => {
        if (err) { clearTimeout(timer); this.pending.delete(id); reject(err); }
      });
    });
  }
  respond(id, result, error) {
    this.ws.send(JSON.stringify({jsonrpc:'2.0', id, ...(error ? {error:{code:-32601,message:error}} : {result})}));
  }
  close() { this.ws.close(); }
}

async function dial(config, record) {
  const base = `http://127.0.0.1:${record.port}`;
  const html = await fetch(base + '/', {signal:AbortSignal.timeout(2500)}).then(r => r.text());
  const match = html.match(/window\.__HERMES_SESSION_TOKEN__\s*=\s*("(?:[^"\\]|\\.)*")/);
  if (!match) throw new Error('Hermes did not publish a local session token');
  const token = JSON.parse(match[1]);
  const require = createRequire(import.meta.url);
  const {WebSocket} = require('ws');
  const ws = new WebSocket(`${base.replace('http:', 'ws:')}/api/ws?token=${encodeURIComponent(token)}`);
  const rpc = new RPC(ws);
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => { ws.terminate(); reject(new Error('Hermes connection timed out')); }, 5000);
    ws.once('open', () => { clearTimeout(timer); resolve(); });
    ws.once('error', () => { clearTimeout(timer); reject(new Error('Hermes connection failed')); });
  });
  // Validate the transport with the method we actually use, never trust a stale ledger.
  try { await rpc.call('profiles.list', {include_sessions:false}, 10000); }
  catch (error) { rpc.close(); throw error; }
  return {rpc, base, token};
}

export async function connect(config, start = true) {
  for (const row of candidates(config.root)) {
    try { return await dial(config, row); } catch { /* stale discovery is not authority */ }
  }
  if (!start) throw new Error('Hermes backend is unavailable');
  const log = fs.openSync(path.join(config.state_dir, 'bots-backend.log'), 'a', 0o600);
  const env = {...process.env};
  for (const key of Object.keys(env)) if (key.startsWith('HERDR_') || key.startsWith('HERMES_')) delete env[key];
  env.HERMES_HOME = config.root;
  const child = spawn(config.hermes_bin, ['-p','default','serve','--host','127.0.0.1','--port','0'],
    {cwd:fs.existsSync(config.agent_root) ? config.agent_root : config.root,env,detached:true,stdio:['ignore',log,log]});
  child.unref(); fs.closeSync(log);
  let spawnError; child.once('error', err => { spawnError = err; });
  for (let i=0;i<90;i++) {
    if (spawnError) throw new Error('Could not start Hermes backend: ' + spawnError.message);
    await sleep(1000);
    for (const row of candidates(config.root)) {
      try { return await dial(config, row); } catch { /* bounded warm-up */ }
    }
  }
  throw new Error('Hermes backend did not become ready. See bots-backend.log.');
}

export async function canonical(rpc, profile, known) {
  const lookup = async () => {
    const result = await rpc.call('session.list', {profile,title:TITLE,include_hidden:true,limit:200});
    return (result.sessions ?? []).find(s => (s.root_title || s.title) === TITLE);
  };
  let row = await lookup();
  if (!row && known?.id) throw new Error('Could not confirm the existing Bot Chat; retry without creating another conversation.');
  if (!row) {
    // Adopt before minting; exact title is the only canonical identity.
    row = await lookup();
    if (!row) {
      const created = await rpc.call('session.create', {profile,title:TITLE,hidden:true,follow_profile_config:true,source:'tui'});
      await rpc.call('session.title', {profile,session_id:created.session_id,title:TITLE});
      row = await lookup();
      if (!row) throw new Error('Could not register Bot Chat; no prompt was sent.');
    }
  }
  return row;
}

export function rosterItem(profile, row, label) {
  const meta = profile.ui_meta?.['hermes-bots'] ?? {};
  const shapes = {star:'✦',diamond:'◇',circle:'◉',square:'▣',hexagon:'⬡',triangle:'△',sparkle:'✦',squircle:'▢',hex:'⬡',cross:'✣'};
  return {id:profile.name,label:clean(meta.title || profile.display_name || label || profile.name.replace(/^project-/,'')),
    icon:shapes[meta.shape] || '✦',color:/^#[0-9a-f]{6}$/i.test(meta.color) ? meta.color : undefined,unread:row?.unread === true,busy:row?.running === true};
}

async function markRead(connection, profile, stored) {
  const result = await fetch(`${connection.base}/api/sessions/${encodeURIComponent(stored)}`, {
    method:'PATCH',headers:{'Authorization':`Bearer ${connection.token}`,'Content-Type':'application/json'},
    body:JSON.stringify({profile,unread:false}),signal:AbortSignal.timeout(5000)
  });
  if (!result.ok) throw new Error('Could not update the shared read badge');
}

async function pluginEnabled(config) {
  if (!config.herdr_socket) return true;
  return new Promise(resolve => {
    const socket = net.createConnection(config.herdr_socket);
    const id = crypto.randomUUID(); let data = '';
    const finish = value => { socket.destroy(); resolve(value); };
    socket.setTimeout(3000, () => finish(null));
    socket.on('error', () => finish(null));
    socket.on('connect', () => socket.write(JSON.stringify({id,method:'plugin.list',params:{}})+'\n'));
    socket.on('data', chunk => {
      data += chunk.toString();
      if (!data.includes('\n')) return;
      try {
        const frame = JSON.parse(data.split('\n')[0]);
        if (frame.error) return finish(null);
        const plugin = frame.result?.plugins?.find(p => p.plugin_id === config.plugin_id);
        finish(plugin?.enabled === true);
      } catch { finish(null); }
    });
    socket.on('close', () => resolve(null));
  });
}

async function daemon(initialConfig) {
  let config = initialConfig;
  let connection;
  while (true) {
    try {
      config = JSON.parse(fs.readFileSync(path.join(config.state_dir,'bots-runtime.json'),'utf8'));
      const enabled = await pluginEnabled(config);
      if (enabled === false) { atomic(config.roster_file,[]); connection?.rpc.close(); return; }
      if (enabled === null) { await sleep(3000); continue; }
      connection ??= await connect(config);
      const {rpc} = connection;
      const {profiles} = await rpc.call('profiles.list', {});
      const rows = [], sessions = {};
      for (const p of profiles ?? []) {
        if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(p.name)) continue;
        const list = await rpc.call('session.list', {profile:p.name,title:TITLE,include_hidden:true,limit:200});
        const row = (list.sessions ?? []).find(s => (s.root_title || s.title) === TITLE);
        const item = rosterItem(p,row,config.labels?.[p.name]);
        if (row) sessions[p.name] = row.resolved_id || row.id;
        if (row) {
          const live = await rpc.call('session.active_list', {profile:p.name});
          item.busy = (live.sessions ?? []).some(s => (s.session_key === row.id || s.session_key === row.resolved_id || s.title === TITLE) && ['working','starting','waiting','streaming'].includes(s.status));
        }
        rows.push(item);
      }
      const probe = spawnSync(config.python_bin || 'python3', [path.join(path.dirname(fileURLToPath(import.meta.url)), 'bots_activity.py')],
        {input:JSON.stringify({root:config.root,sessions}),encoding:'utf8',timeout:10000});
      if (probe.status !== 0) throw new Error('Could not read Bot notification watermarks');
      const activity = JSON.parse(probe.stdout);
      for (const item of rows) {
        const state = activity[item.id];
        if (!state) continue;
        item.unread = state.unread;
        // NULL already means read in Hermes. Initialize tracking through its native
        // API so future replies have a shared, persistent badge on both surfaces.
        if (!state.tracked) await markRead(connection,item.id,sessions[item.id]);
      }
      const rank = id => config.profiles.includes(id) ? config.profiles.indexOf(id) : config.profiles.length;
      rows.sort((a,b) => rank(a.id) - rank(b.id) || a.id.localeCompare(b.id));
      atomic(config.roster_file,rows);
      atomic(path.join(config.state_dir,'bots-health.json'), {ok:true,updated_at:Date.now(),profiles:rows.length});
    } catch (error) {
      connection?.rpc.close(); connection = null;
      atomic(path.join(config.state_dir,'bots-health.json'),{ok:false,error:error.message,updated_at:Date.now()});
    }
    await sleep(3000);
  }
}

export async function chat(config, profile) {
  if (!config.profiles.includes(profile)) throw new Error('Unknown Hermes profile');
  emit({type:'status',text:'Connecting to Hermes…'});
  const connection = await connect(config);
  const {rpc} = connection;
  const {profiles} = await rpc.call('profiles.list', {});
  const bot = (profiles ?? []).find(p => p.name === profile);
  if (!bot) throw new Error('Hermes profile was removed');
  if (!bot.ui_meta?.['hermes-bots']) {
    const result = await rpc.call('profiles.configure', {name:profile,
      ui_meta:{'hermes-bots':{shape:'star',title:bot.display_name || config.labels?.[profile] || profile}},
      ui_meta_expected_revisions:{'hermes-bots':bot.ui_meta_revisions?.['hermes-bots'] ?? 0}});
    if (result.applied?.ui_meta_conflicts) throw new Error('Bot metadata changed concurrently. Retry.');
  }
  const row = await canonical(rpc,profile,bot.canonical_session);
  const stored = row.resolved_id || row.id;
  let runtime;
  rpc.onEvent = event => {
    if (runtime && event?.session_id && event.session_id !== runtime) return;
    if (event?.type === 'request.cancel') { const id = event.payload?.id || event.payload?.request_id; openRequests.delete(id); }
    emit({type:'event',event});
    if (event?.type === 'message.complete' || event?.type === 'turn.end') markRead(connection,profile,stored).catch(() => {});
  };
  const openRequests = new Map();
  rpc.onRequest = frame => {
    if (runtime && frame.params?.session_id && frame.params.session_id !== runtime) return;
    openRequests.set(frame.id,frame); emit({type:'request',request:frame});
  };
  await rpc.call('client.capabilities',{server_requests:true});
  const resumed = await rpc.call('session.resume',{profile,session_id:stored,source:'tui',inline_images:false});
  runtime = resumed.session_id;
  emit({type:'session',session:resumed,title:rosterItem(bot,row,config.labels?.[profile]).label});
  await markRead(connection,profile,stored).catch(() => emit({type:'status',text:'Connected; shared badge could not be cleared.'}));
  for (const frame of resumed.open_requests ?? []) {
    // Replayed requests retain their original ids and method, never auto-answer.
    if (frame.id && frame.method) { openRequests.set(frame.id,frame); emit({type:'request',request:frame}); }
  }
  const reader = readline.createInterface({input:process.stdin});
  for await (const line of reader) {
    try {
      const input = JSON.parse(line);
      if (input.type === 'reply') {
        if (!openRequests.has(input.id)) throw new Error('This question is no longer pending');
        rpc.respond(input.id,input.result,input.error); openRequests.delete(input.id);
      } else if (input.type === 'send' && String(input.text).trim()) {
        const text = String(input.text);
        if (text.startsWith('/')) {
          // Bot Chats are continuous: reset gestures compact the same session.
          if (/^\/title\s+\S/.test(text)) throw new Error('Bot Chat keeps its canonical title. Rename the bot in Hermes Desktop.');
          if (/^\/(resume|branch|fork|profile|delete)(\s|$)/.test(text)) throw new Error('This popup stays in this Bot Chat. Use a workspace terminal for other sessions or profiles.');
          const command = /^\/(new|reset|clear)(\s|$)/.test(text) ? '/compress' : text;
          const result = await rpc.call('slash.exec',{profile,session_id:runtime,command});
          if (result.type === 'skill' && result.message) {
            await rpc.call('prompt.submit',{profile,session_id:runtime,text:result.message,surface:'tui'});
          } else emit({type:'slash',result});
        } else {
          await rpc.call('prompt.submit',{profile,session_id:runtime,text,surface:'tui',queued:true});
        }
      } else if (input.type === 'refresh') {
        const snapshot = await rpc.call('session.resume',{profile,session_id:stored,source:'tui',inline_images:false});
        runtime = snapshot.session_id; emit({type:'session',session:snapshot});
        await markRead(connection,profile,stored).catch(() => {});
      }
    } catch (error) { emit({type:'error',text:error.message}); }
  }
  rpc.close();
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const [mode, configFile, profile] = process.argv.slice(2);
  const config = JSON.parse(fs.readFileSync(configFile,'utf8'));
  (mode === 'daemon' ? daemon(config) : chat(config,profile)).catch(error => {
    emit({type:'error',text:error.message}); process.exitCode = 1;
  });
}
