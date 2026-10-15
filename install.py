#!/usr/bin/env python3
"""Configure an installed plugin without replacing any executable."""
import argparse
import json
from hermes_manager.core import Herdr, Settings
from hermes_manager.control import Manager
from hermes_manager.setup import setup
from hermes_manager.bots import configure_bots


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shell', choices=['zsh', 'bash', 'none'])
    parser.add_argument('--activate', action='store_true')
    args = parser.parse_args()
    settings = Settings()
    result = setup(settings, args.shell)
    if args.activate:
        api = Herdr()
        # Never relink a GitHub-managed checkout as a local plugin.
        result['sync'] = Manager(settings, api).sync(force=True)
        result['bots'] = configure_bots(settings, api)
        result['reload'] = api.call('server.reload_config')
    print(json.dumps(result))
    if result.get('sync', {}).get('errors'): raise SystemExit(1)

if __name__ == '__main__': main()
