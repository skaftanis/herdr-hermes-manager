#!/usr/bin/env python3
"""Remove managed shell/config blocks and disable the plugin, preserving user data."""
import argparse
import json
from hermes_manager.core import Herdr, Settings
from hermes_manager.setup import uninstall


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline', action='store_true', help='Skip the Herdr API when no server is running')
    args = parser.parse_args()
    settings = Settings()
    if not args.offline:
        Herdr().call('plugin.disable', {'plugin_id': settings.plugin_id})
    print(json.dumps(uninstall(settings)))

if __name__ == '__main__': main()
