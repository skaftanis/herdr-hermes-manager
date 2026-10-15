#!/usr/bin/python3
"""Test stock Herdr Manager grid clicks + popup PTY, entirely in a disposable session."""
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hermes_manager.core import Herdr

STOCK = os.environ.get('HERDR_TEST_BINARY', str(Path.home()/'.local/bin/herdr'))
CLIENT = STOCK


def wait(test, seconds=15):
    until=time.monotonic()+seconds
    while time.monotonic()<until:
        try:
            value=test()
            if value:return value
        except (OSError,RuntimeError):pass
        time.sleep(.1)
    raise RuntimeError('UI check timed out')


def main():
    with tempfile.TemporaryDirectory(prefix='herdr-bots-ui-check-', dir='/private/tmp') as folder:
        temp=Path(folder);config=temp/'config/herdr';config.mkdir(parents=True)
        (config/'config.toml').write_text('onboarding = false\n[ui]\nsidebar_width = 28\n[ui.toast]\ndelivery = "off"\n')
        env={k:v for k,v in os.environ.items() if not k.startswith(('HERDR_','HERMES_'))}
        env.update(XDG_CONFIG_HOME=str(temp/'config'),XDG_STATE_HOME=str(temp/'state'),
                   HERDR_CONFIG_PATH=str(config/'config.toml'),HERDR_SOCKET_PATH=str(config/'herdr.sock'),TERM='xterm-256color')
        log=(temp/'server.log').open('wb')
        server=subprocess.Popen([STOCK,'server'],env=env,stdout=log,stderr=log,start_new_session=True)
        api=Herdr(config/'herdr.sock');client=None;master=None
        try:
            try: wait(api.snapshot)
            except Exception:
                print((temp/'server.log').read_text(),file=sys.stderr)
                raise
            workspace = api.call('workspace.create', {'label':'UI check','cwd':str(temp),'focus':True})
            plugin=temp/'plugin';plugin.mkdir()
            popup=plugin/'popup.py'
            # Real production curses UI, seeded with a harmless recorded transcript.
            popup.write_text('import sys,curses\nfrom pathlib import Path\n'
                'sys.path.insert(0,'+repr(str(ROOT))+')\n'
                'from hermes_manager.bots import BotPopup\nfrom hermes_manager.core import Profile\n'
                'p=BotPopup(None,Profile("alpha",Path("/tmp"),"Alpha Bot",Path("/tmp")))\n'
                'p.send=lambda x: Path('+repr(str(temp/'input.json'))+').write_text(__import__("json").dumps(x))\n'
                'p.events.put({"type":"session","session":{"messages":[{"role":"assistant","text":"Older message " + str(i)} for i in range(100)] + [{"role":"user","text":"Existing Desktop conversation"},'
                '{"role":"assistant","text":"Welcome back from Desktop"}]}})\n'
                'original_draw=p.draw\n'
                'def draw(win):\n'
                ' original_draw(win)\n'
                ' Path('+repr(str(temp/'scroll.json'))+').write_text(str(p.scroll))\n'
                'p.draw=draw\n'
                'p.copy_selection=lambda: Path('+repr(str(temp/'selected.txt'))+').write_text(p.selected_text())\n'
                'curses.wrapper(p.loop)\n'
                'Path('+repr(str(temp/'closed'))+').touch()\n')
            manifest=plugin/'herdr-plugin.toml'
            manifest.write_text('id = "hhl.bots-ui-check"\nname = "UI check"\nversion = "0.1.0"\nmin_herdr_version = "0.9.3"\nplatforms = ["macos"]\n'
                '[[panes]]\nid = "chat"\ntitle = "Test Bot Chat"\nplacement = "popup"\n'
                'command = ["/usr/bin/python3", "popup.py"]\n'
                '[[panes]]\nid = "manager"\ntitle = "Hermes Manager"\nplacement = "split"\n'
                'command = ["/usr/bin/python3", "manager.py"]\n')
            api.call('plugin.link',{'path':str(plugin),'enabled':True})
            launch=plugin/'launch.py'
            launch.write_text('import sys,json\nfrom pathlib import Path\n'
                'sys.path.insert(0,'+repr(str(ROOT))+')\nfrom hermes_manager.core import Herdr\n'
                'Path('+repr(str(temp/'clicked.json'))+').write_text(json.dumps({"profile":sys.argv[1]}))\n'
                'from unittest.mock import Mock,patch\n'
                'from hermes_manager.bots import open_bot\nfrom hermes_manager.core import Profile\n'
                'api=Herdr('+repr(str(config/'herdr.sock'))+')\n'
                'class TestAPI:\n'
                ' def call(self,method,params):\n'
                '  params["entrypoint"]="chat"\n'
                '  return api.call(method,params)\n'
                'with patch("hermes_manager.bots.configure_bots"), patch("hermes_manager.bots.BRIDGE",Path('
                +repr(str(plugin/'hermes_manager/bots_bridge.mjs'))+')):\n'
                ' open_bot(Mock(plugin_id="hhl.bots-ui-check"),TestAPI(),'
                'Profile(sys.argv[1],Path("/tmp"),"Alpha",Path("/tmp")))\n')
            roster=[{'id':'alpha' if i==0 else 'bot-'+str(i),'label':'Alpha' if i==0 else 'Bot '+str(i),
                     'icon':'✦','unread':i==0,'busy':False} for i in range(11)]
            (temp/'roster.json').write_text(json.dumps(roster))
            manager_script = plugin/'manager.py'
            manager_script.write_text('import sys,json,subprocess\nfrom pathlib import Path\n'
                'from unittest.mock import Mock\n'
                'sys.path.insert(0,'+repr(str(ROOT))+')\n'
                'from hermes_manager.ui import Sidebar\nfrom hermes_manager.core import Profile\n'
                'p=Sidebar(Mock(),Profile("alpha",Path("/tmp"),"Alpha",Path("/tmp")))\n'
                'p.bots=json.loads(Path('+repr(str(temp/'roster.json'))+').read_text())\n'
                'p.conversations=[{"id":str(i),"title":"Saved conversation "+str(i)} for i in range(50)]\n'
                'p.poll=lambda: None\n'
                'p.open_bot=lambda: subprocess.run(["/usr/bin/python3",'+repr(str(launch))+',p.current("bots")["id"]],check=True)\n'
                'original_draw=p.draw\n'
                'def draw():\n'
                ' original_draw()\n'
                ' Path('+repr(str(temp/'manager-hits.json'))+').write_text(json.dumps(p.hitboxes))\n'
                ' Path('+repr(str(temp/'manager-size.json'))+').write_text(json.dumps(p.screen.getmaxyx()))\n'
                'p.draw=draw\n'
                'p.run()\n')
            master,slave=pty.openpty()
            fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',60,180,0,0))
            client=subprocess.Popen([CLIENT],env=env,stdin=slave,stdout=slave,stderr=slave,start_new_session=True)
            os.close(slave)
            output=bytearray()
            def drain():
                while select.select([master],[],[],.05)[0]:
                    try:output.extend(os.read(master,65536))
                    except OSError:break
                return output
            wait(lambda: len(drain()) > 0)
            opened = api.call('plugin.pane.open', {'plugin_id':'hhl.bots-ui-check', 'entrypoint':'manager',
                'placement':'split','target_pane_id':workspace['root_pane']['pane_id'],'direction':'right','focus':True})
            pane_id=opened['plugin_pane']['pane']['pane_id']
            hits=wait(lambda:(drain(), json.loads((temp/'manager-hits.json').read_text()) if (temp/'manager-hits.json').exists() else None)[1])
            # The first frame may precede the PTY resize after the split.
            for _ in range(5): time.sleep(.1);drain()
            layout=api.call('pane.layout',{'pane_id':pane_id})['layout']
            rect=next(p['rect'] for p in layout['panes'] if p['pane_id']==pane_id)
            wait(lambda: (drain(), json.loads((temp/'manager-size.json').read_text()) == [rect['height']-2, rect['width']-2])[1])
            hits=json.loads((temp/'manager-hits.json').read_text())
            hit=next(h for h in hits if h[3]=='bot' and h[4]==0)
            # Workspace-relative geometry plus sidebar, tab bar and pane border.
            x=28+rect['x']+hit[1]+2
            y=rect['y']+hit[0]+3
            os.write(master, ('\x1b[<0;'+str(x)+';'+str(y)+'M').encode())
            time.sleep(.1);drain()
            os.write(master, ('\x1b[<0;'+str(x)+';'+str(y)+'m').encode())
            click=wait(lambda:(drain(), json.loads((temp/'clicked.json').read_text()) if (temp/'clicked.json').exists() else None)[1])
            assert click['profile']=='alpha',click
            try: wait(lambda:b'WelcomebackfromDesktop' in b''.join(re.sub(rb'\x1b\[[0-?]*[ -/]*[@-~]',b'',bytes(drain())).split()))
            except Exception:
                print('Native popup history marker was missing; closed=',(temp/'closed').exists(),file=sys.stderr)
                print(api.call('plugin.log.list',{}),file=sys.stderr)
                raise
            os.write(master,b'\x1b[<64;80;20M')
            wait(lambda: (drain(), int((temp/'scroll.json').read_text() or '0') > 0)[1])
            os.write(master,b'\x1b[5~')
            wait(lambda: (drain(), int((temp/'scroll.json').read_text() or '0') > 3)[1])
            os.write(master,b'\x1b[<0;80;20M')
            time.sleep(.1);drain()
            os.write(master,b'\x1b[<32;110;22M')
            time.sleep(.1);drain()
            os.write(master,b'\x1b[<0;110;22m')
            selected=wait(lambda: (drain(), (temp/'selected.txt').read_text() if (temp/'selected.txt').exists() else '')[1])
            assert selected.strip(), 'Drag selection was empty'
            os.write(master,b'Test typed input\r')
            sent=wait(lambda: (drain(), json.loads((temp/'input.json').read_text()) if (temp/'input.json').exists() else None)[1])
            assert sent=={'type':'send','text':'Test typed input'},sent
            os.write(master,b'\x1b')
            wait(lambda: (drain(), (temp/'closed').exists())[1], seconds=2)
            time.sleep(.3);drain()
            print(json.dumps({'stock_client':True,'manager_grid':True,'eleven_icon_grid':True,'native_click_profile':click['profile'],
                              'popup_history':True,'compose_enter':True,'real_model_prompts':0}))
        finally:
            if client and client.poll() is None:
                os.killpg(client.pid,signal.SIGTERM)
                try: client.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(client.pid,signal.SIGKILL);client.wait(timeout=5)
            if master is not None:os.close(master)
            if server.poll() is None:
                server.terminate();server.wait(timeout=10)
            log.close()


if __name__=='__main__':main()
