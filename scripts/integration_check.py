#!/usr/bin/env python3
"""Exercise real Herdr APIs and Hermes cron commands with disposable profiles."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hermes_manager.core import Herdr, Settings, load_jobs
from hermes_manager.control import Manager, manager_pane
from hermes_manager.cli import routed_arguments

HERDR = os.environ.get("HERDR_TEST_BINARY") or shutil.which("herdr") or str(Path.home()/".local/bin/herdr")
HERMES = os.environ.get("HERMES_TEST_BINARY") or shutil.which("hermes") or str(Path.home()/".local/bin/hermes")


def run(command, env, timeout=30):
    process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid,signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        raise RuntimeError("Test command timed out: "+str(command)+"\n"+stdout+stderr)
    if process.returncode:
        raise RuntimeError("Command failed: " + str(command) + "\n" + stderr + stdout)
    return stdout


def wait_for(test, seconds=15):
    deadline = time.monotonic()+seconds
    while time.monotonic()<deadline:
        try:
            result = test()
            if result:
                return result
        except (OSError, RuntimeError, KeyError):
            pass
        time.sleep(0.15)
    raise RuntimeError("Timed out waiting for integration check")


def main():
    with tempfile.TemporaryDirectory(prefix="herdr-hermes-check-") as directory:
        temp = Path(directory)
        root, host = temp/"hermes", temp/"herdr"
        root.mkdir(); host.mkdir()
        # Hermes scopes dependency generations to the fleet root. Share only the existing
        # installed runtime; profile state, jobs, config, and credentials remain disposable.
        (root/"installs").symlink_to(Path.home()/".hermes/installs",target_is_directory=True)
        (host/"config.toml").write_text('onboarding = false\n[ui.toast]\ndelivery = "off"\n')
        for name in ("default", "alpha", "beta"):
            home = root if name=="default" else root/"profiles"/name
            home.mkdir(parents=True, exist_ok=True)
            (home/"config.yaml").write_text("model:\n  default: test-model\n  provider: openrouter\n")
            (home/".env").write_text("")
            with sqlite3.connect(str(home/"state.db")) as c:
                c.executescript("CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,started_at REAL,title TEXT,cwd TEXT,"
                               "message_count INTEGER,parent_session_id TEXT,hidden INTEGER DEFAULT 0,archived INTEGER DEFAULT 0);"
                               "CREATE TABLE messages(session_id TEXT,role TEXT,content TEXT,timestamp REAL);")
                c.execute("INSERT INTO sessions(id,source,started_at,title,cwd) VALUES(?,?,?,?,?)",
                          (name+"-chat","cli",time.time(),name+" saved conversation",str(temp)))
        # A harmless executable records argv for the new conversation terminal.
        fake = temp/"hermes-recorder"
        fake.write_text('#!/usr/bin/python3\nimport json,os,sys,time\n'
                        'if "cron" in sys.argv:\n    os.execv('+repr(HERMES)+', ['+repr(HERMES)+', *sys.argv[1:]])\n'
                        'open('+repr(str(temp/"launch.json"))+',"w").write(json.dumps({"argv":sys.argv[1:],'
                        '"workspace":os.environ.get("HERDR_WORKSPACE_ID")}))\ntime.sleep(60)\n')
        fake.chmod(0o700)
        env = dict(os.environ)
        for key in list(env):
            if key.startswith("HERDR_") or key.startswith("HERMES_"):
                env.pop(key,None)
        env.update(HERDR_CONFIG_PATH=str(host/"config.toml"), HERDR_SOCKET_PATH=str(host/"herdr.sock"),
                   XDG_CONFIG_HOME=str(temp/"config"), XDG_STATE_HOME=str(temp/"state"),
                   HERMES_HOME=str(root), HERMES_RUNTIME_DIR=str(Path.home()/".hermes/tools"))
        log = (temp/"server.log").open("w")
        server = subprocess.Popen([HERDR,"server"],env=env,stdout=log,stderr=log)
        api = Herdr(host/"herdr.sock")
        try:
            try:
                wait_for(api.snapshot)
            except Exception:
                print("Test server exit:",server.poll(),"log:",(temp/"server.log").read_text(),file=sys.stderr)
                raise
            # This registry is isolated by HERDR_CONFIG_PATH, so no live plugins are touched.
            manifest = temp/"plugin"
            manifest.mkdir()
            plugin_id = "hhl.hermes-manager-integration"
            (manifest/"herdr-plugin.toml").write_text(
                'id = "'+plugin_id+'"\nname = "Hermes test"\nversion = "0.1.0"\n'
                'min_herdr_version = "0.9.3"\nplatforms = ["macos"]\n'
                '[[panes]]\nid = "manager"\ntitle = "Hermes Manager"\nplacement = "split"\n'
                'command = ["/usr/bin/python3", '+json.dumps(str(ROOT/"hermes_manager.py"))+', "sidebar", "--root", '
                +json.dumps(str(root))+']\n')
            api.call("plugin.link",{"path":str(manifest),"enabled":True})
            panel_config = temp/"config/herdr/plugins/config"/plugin_id
            panel_config.mkdir(parents=True,exist_ok=True)
            (panel_config/"config.json").write_text(json.dumps({"hermes_bin":str(fake)}))
            with patch_env({"HERDR_PLUGIN_ID":plugin_id,"HERDR_SOCKET_PATH":str(host/"herdr.sock"),
                            "HERMES_RUNTIME_DIR":str(Path.home()/".hermes/tools")}):
                settings = Settings(root, temp/"plugin-config", temp/"plugin-state")
                settings.hermes_bin = fake
                manager = Manager(settings,api)
                # Create a regular workspace and an existing Hermes space that must be adopted.
                ordinary = api.call("workspace.create",{"label":"Project","cwd":str(temp),"focus":False})["workspace"]
                adopted = api.call("workspace.create",{"label":"(H) Alpha","cwd":str(temp),"focus":False})["workspace"]
                first = manager.sync(force=True)
                assert not first["errors"],first
                snap = api.snapshot()
                by_profile={w.get("tokens",{}).get("hermes-profile"):w for w in snap["workspaces"]
                            if w.get("tokens",{}).get("hermes-profile")}
                assert set(by_profile)=={"default","alpha","beta"},by_profile
                assert by_profile["alpha"]["workspace_id"]==adopted["workspace_id"]
                assert [w["workspace_id"] for w in snap["workspaces"]][-3:]==[
                    by_profile[p]["workspace_id"] for p in ("default","alpha","beta")]
                before=len(snap["panes"])
                assert not manager.sync(force=True)["sidebars_created"]
                assert len(api.snapshot()["panes"])==before
                for profile in by_profile:
                    workspace_id=by_profile[profile]["workspace_id"]
                    panel=next(p for p in snap["panes"] if p["workspace_id"]==workspace_id and manager_pane(p))
                    wait_for(lambda: "HERMES" in api.call("pane.read",{"pane_id":panel["pane_id"],
                        "source":"visible","lines":80}).get("read", {}).get("text", ""))
                    shell=next(p for p in snap["panes"] if p["workspace_id"]==workspace_id and not manager_pane(p))
                    routed=routed_arguments([],settings,api,{"HERDR_ENV":"1","HERDR_WORKSPACE_ID":workspace_id,
                                                            "HERDR_PANE_ID":shell["pane_id"]})
                    assert routed==["-p",profile],routed
                opened=manager.open_chat(settings.profile("alpha"),"alpha-chat",focus=False)
                launched=wait_for(lambda: json.loads((temp/"launch.json").read_text()) if (temp/"launch.json").exists() else None)
                assert launched["argv"]==["-p","alpha","--resume","alpha-chat"],launched
                assert launched["workspace"]==adopted["workspace_id"],launched
                # Real Hermes cron management, A -> B -> A, without executing any jobs.
                settings.hermes_bin=Path(HERMES)
                for profile in ("alpha","beta"):
                    run([HERMES,"-p",profile,"cron","create","every 12h","Integration test only",
                         "--name",profile+" test","--paused","--deliver","local"],env)
                a,b=settings.profile("alpha"),settings.profile("beta")
                aid,bid=load_jobs(a)[0]["id"],load_jobs(b)[0]["id"]
                manager.cron_command(a,"resume",aid)
                assert load_jobs(a)[0]["enabled"] and not load_jobs(b)[0]["enabled"]
                manager.cron_command(b,"resume",bid)
                assert load_jobs(a)[0]["enabled"] and load_jobs(b)[0]["enabled"]
                manager.cron_command(a,"pause",aid)
                assert not load_jobs(a)[0]["enabled"] and load_jobs(b)[0]["enabled"]
                manager.cron_command(b,"pause",bid)
                old_schedule=load_jobs(a)[0]["schedule"]
                manager.cron_command(a,"edit",aid,schedule="every 24h")
                assert load_jobs(a)[0]["schedule"] != old_schedule
                snap=api.snapshot()
                panel=next(p for p in snap["panes"] if p["workspace_id"]==adopted["workspace_id"] and manager_pane(p))
                panel_id=panel["pane_id"]
                def visible():
                    return api.call("pane.read",{"pane_id":panel_id,"source":"visible","lines":100})["read"]["text"]
                text=wait_for(lambda: visible() if "alpha test" in visible() else None)
                job_y=next(i for i,line in enumerate(text.splitlines()) if "alpha test" in line)+1
                click=lambda x,y: api.call("pane.send_text",{"pane_id":panel_id,
                      "text":"\x1b[<0;"+str(x)+";"+str(y)+"M\x1b[<0;"+str(x)+";"+str(y)+"m"})
                click(3,job_y)
                try:
                    wait_for(lambda: load_jobs(a)[0]["enabled"])
                except Exception:
                    print("Mouse toggle screen:\n"+visible(),file=sys.stderr)
                    raise
                # Restore the fixture to paused before verifying conversation clicks.
                manager.cron_command(a,"pause",aid)
                text=visible()
                chat_y=next(i for i,line in enumerate(text.splitlines()) if "alpha saved conversation" in line)+1
                tabs_before=len(api.snapshot()["tabs"])
                click(4,chat_y)
                wait_for(lambda: len(api.snapshot()["tabs"])>tabs_before)
                # Restart only this disposable server, retaining the same isolated saved state.
                api.call("server.stop")
                server.wait(timeout=10)
                server=subprocess.Popen([HERDR,"server"],env=env,stdout=log,stderr=log)
                wait_for(api.snapshot)
                result=manager.sync(force=True)
                assert not result["errors"],result
                restored=api.snapshot()
                assert len([w for w in restored["workspaces"] if w.get("tokens",{}).get("hermes-profile")])==3
                for panel in [p for p in restored["panes"] if manager_pane(p)]:
                    wait_for(lambda: "HERMES" in api.call("pane.read",{"pane_id":panel["pane_id"],
                             "source":"visible","lines":80})["read"]["text"])
                print(json.dumps({"ok":True,"checks":["adoption","workspace order","idempotent sync",
                    "sidebar rendering","per-workspace routing","new-tab conversation launch",
                    "real Hermes cron A-B-A isolation","schedule editing","mouse cron toggle",
                    "mouse conversation launch","server restart restoration"]}))
        finally:
            try:
                api.call("server.stop")
            except Exception:
                pass
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.terminate(); server.wait(timeout=5)
            log.close()
            output = ROOT / ".test-output"
            output.mkdir(exist_ok=True)
            shutil.copy2(temp/"server.log",output/"integration-server.log")


@contextmanager
def patch_env(values):
    old=dict(os.environ)
    os.environ.update(values)
    try:
        yield
    finally:
        os.environ.clear(); os.environ.update(old)


if __name__=="__main__":
    main()
