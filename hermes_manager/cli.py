from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .core import (APIError, Herdr, Settings, current_profile, explicit_profile,
                   is_qualified_resume, load_conversations, load_jobs, read_json)
from .control import Manager


def routed_arguments(argv, settings, api, env=None):
    env = os.environ if env is None else env
    if env.get("HERDR_ENV") != "1" or explicit_profile(argv) is not None or is_qualified_resume(argv):
        return argv[:]
    try:
        profile = current_profile(api, settings, env)
    except (APIError, OSError, ValueError, KeyError) as exc:
        state = read_json(settings.state_dir / (api.state_key()+".json"))
        managed = any(w.get("workspace_id") == env.get("HERDR_WORKSPACE_ID")
                      for w in state.get("workspaces", {}).values())
        if managed:
            raise ValueError("Cannot verify this workspace's Hermes profile. Use hermes -p PROFILE explicitly. " + str(exc))
        return argv[:]
    return ["-p", profile.name, *argv] if profile else argv[:]


def route(argv, inspect=False):
    settings = Settings()
    routed = routed_arguments(argv, settings, Herdr(timeout=2))
    if inspect:
        print(json.dumps({"argv": routed, "profile": explicit_profile(routed)}, ensure_ascii=False))
        return
    delegate = settings.config.get('hermes_delegate')
    # Preserve an existing local restore integration if this Mac has one; fresh
    # installs execute Hermes directly and do not require that integration.
    previous = settings.host_config / 'hermes-profile-restore/launcher.py'
    if not delegate and previous.is_file():
        delegate = [sys.executable, str(previous)]
    if not delegate:
        from .routing import resume_arguments
        routed = resume_arguments(routed, settings)
        delegate = [os.environ.get('HERDR_MANAGER_HERMES_BIN') or str(settings.hermes_bin)]
    if not isinstance(delegate, list) or not delegate or not all(isinstance(v, str) for v in delegate):
        raise ValueError('hermes_delegate must be an argv array')
    try:
        with open(delegate[0], 'rb') as executable:
            header = executable.read(8192)
    except OSError:
        header = b''
    if (b'hermes_manager.py' in header and b'route' in header) or any(Path(v).name == 'hermes_manager.py' for v in delegate[1:]):
        raise ValueError('Hermes launcher loops back to this plugin. Set hermes_bin to the original Hermes executable.')
    env = dict(os.environ)
    if env.get('HERDR_ENV') == '1': env['HERMES_HOME'] = str(settings.root)
    os.execvpe(delegate[0], [*delegate, *routed], env)


def main():
    argv = sys.argv[1:]
    try:
        if argv and argv[0] in ("route", "route-inspect"):
            rest = argv[1:]
            if rest[:1] == ["--"]:
                rest = rest[1:]
            route(rest, inspect=argv[0] == "route-inspect")
            return
        parser = argparse.ArgumentParser(description="Hermes profile manager for Herdr")
        parser.add_argument("command", choices=("initialize", "doctor", "sync", "refresh", "sidebar", "focus", "new-chat", "open", "inspect", "bots-sync", "bot-open", "bot-popup"))
        parser.add_argument("--force", action="store_true")
        parser.add_argument("--profile")
        parser.add_argument("--session")
        parser.add_argument("--socket")
        parser.add_argument("--root")
        parser.add_argument("--no-focus", action="store_true")
        args = parser.parse_args(argv)
        settings, api = Settings(root=args.root), Herdr(args.socket)
        manager = Manager(settings, api)
        if args.command == 'doctor':
            from .setup import doctor
            result = doctor(settings)
            print(json.dumps(result))
            if not result['ok']: raise SystemExit(1)
            return
        if args.command == 'initialize':
            from .setup import initialize
            from .bots import configure_bots
            initialize(settings)
            result = manager.sync(force=True)
            result['bots'] = configure_bots(settings, api)
        elif args.command == "bots-sync":
            from .bots import configure_bots
            result = configure_bots(settings, api)
        elif args.command == "sync":
            result = manager.sync(force=args.force)
        elif args.command == "refresh":
            result = manager.refresh_sidebars()
        elif args.command == "focus":
            result = manager.focus_sidebar()
        else:
            name = args.profile or (os.environ.get("HERDR_HERMES_PROFILE") if args.command == "sidebar" else os.environ.get("HERDR_HERMES_BOT") if args.command == "bot-popup" else None)
            profile = settings.profile(name) if name else current_profile(api, settings)
            if not profile:
                raise ValueError("Select a Hermes profile workspace or supply --profile")
            if args.command == "bot-popup":
                from .bots import BotPopup
                BotPopup(settings, profile).run()
                return
            if args.command == "bot-open":
                from .bots import open_bot
                result = open_bot(settings, api, profile)
            elif args.command == "sidebar":
                from .ui import Sidebar
                Sidebar(manager, profile).run()
                return
            elif args.command == "inspect":
                result = {"profile": profile.name, "home": str(profile.home),
                          "jobs": [{"id": j["id"], "name": j.get("name"), "enabled": j.get("enabled"),
                                    "state": j.get("state")} for j in load_jobs(profile)],
                          "conversations": load_conversations(profile, settings.conversation_limit)}
            else:
                result = manager.open_chat(profile, args.session, focus=not args.no_focus)
        print(json.dumps(result, ensure_ascii=False))
    except (APIError, OSError, ValueError, KeyError) as exc:
        print("Hermes Manager: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
