#!/usr/bin/env python3
"""Install the staged Git contents in a clean HOME with stock Herdr and fake Hermes."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hermes_manager.core import Herdr


def wait(test, seconds=15):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        try:
            result=test()
            if result: return result
        except (OSError, RuntimeError, KeyError): pass
        time.sleep(.1)
    raise RuntimeError('Clean-install check timed out')


def main():
    binary=os.environ.get('HERDR_TEST_BINARY') or shutil.which('herdr')
    if not binary: raise RuntimeError('Stock Herdr is required')
    with tempfile.TemporaryDirectory(prefix='hm-package-', dir='/private/tmp' if sys.platform == 'darwin' else None) as folder:
        temp=Path(folder);source=temp/'source';source.mkdir()
        subprocess.run(['git', 'checkout-index', '--all', '--prefix='+str(source)+'/'],cwd=ROOT,check=True)
        subprocess.run(['npm','ci','--offline','--ignore-scripts','--no-audit','--no-fund'],cwd=source,check=True,stdout=subprocess.DEVNULL)
        home=temp/'home';home.mkdir()
        fake=home/'hermes-original'
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n');fake.chmod(0o700)
        checksum=hashlib.sha256(fake.read_bytes()).hexdigest()
        fleet=home/'.hermes'
        for profile in ['default','research']:
            path=fleet if profile=='default' else fleet/'profiles'/profile
            path.mkdir(parents=True,exist_ok=True)
            (path/'config.yaml').write_text('{}\n')
            with sqlite3.connect(path/'state.db') as db:
                db.executescript('CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,started_at REAL,last_activity_at REAL,title TEXT,cwd TEXT,message_count INTEGER,parent_session_id TEXT,hidden INTEGER DEFAULT 0,archived INTEGER DEFAULT 0); CREATE TABLE messages(session_id TEXT,role TEXT,content TEXT,timestamp REAL);')
        config=home/'.config/herdr';config.mkdir(parents=True)
        (config/'config.toml').write_text('onboarding = false\n[ui.toast]\ndelivery = "off"\n')
        plugin_config=config/'plugins/config/hhl.hermes-manager';plugin_config.mkdir(parents=True)
        (plugin_config/'config.json').write_text(json.dumps({'hermes_bin':str(fake)}))
        (home/'.zshrc').write_text('export KEEP_THIS=yes\n')
        env={k:v for k,v in os.environ.items() if not k.startswith(('HERDR_','HERMES_','XDG_'))}
        env.update(HOME=str(home), SHELL='/bin/zsh', HERDR_CONFIG_PATH=str(config/'config.toml'),
            HERDR_SOCKET_PATH=str(config/'herdr.sock'), XDG_CONFIG_HOME=str(home/'.config'), XDG_STATE_HOME=str(home/'.local/state'))
        def run(*args):
            return subprocess.run([sys.executable,*args],cwd=source,env=env,check=True,capture_output=True,text=True)
        run('install.py','--shell','zsh')
        server_log=(temp/'server.log').open('wb')
        server=subprocess.Popen([binary,'server'],env=env,stdout=server_log,stderr=server_log)
        api=Herdr(config/'herdr.sock')
        try:
            wait(api.snapshot)
            api.call('plugin.link',{'path':str(source),'enabled':True})
            result=json.loads(run('hermes_manager.py','sync','--force').stdout)
            assert not result['errors'],result
            assert result['profiles']==2,result
            def rendered():
                panels=[p for p in api.snapshot()['panes'] if p.get('label')=='Hermes Manager']
                return len(panels)==2 and all('BOTS' in api.call('pane.read',{'pane_id':p['pane_id'],'source':'visible','lines':100})['read']['text'] for p in panels)
            wait(rendered)
            api.call('plugin.disable',{'plugin_id':'hhl.hermes-manager'})
            run('uninstall.py','--offline')
            assert 'KEEP_THIS=yes' in (home/'.zshrc').read_text()
            assert 'hhl.hermes-manager' not in (home/'.zshrc').read_text()
            assert checksum==hashlib.sha256(fake.read_bytes()).hexdigest()
            assert (fleet/'profiles/research/state.db').exists()
            print(json.dumps({'clean_staged_checkout':True,'fresh_home_install':True,'actual_manifest':True,
                'profiles':2,'manager_bots_rendered':True,'uninstall_preserves_user_data':True,'real_model_prompts':0}))
        finally:
            if server.poll() is None:
                server.terminate()
                try: server.wait(timeout=10)
                except subprocess.TimeoutExpired: server.kill();server.wait()
            server_log.close()

if __name__=='__main__': main()
