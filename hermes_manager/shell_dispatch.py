"""Stable entrypoint copied into plugin config, following the active installed root."""
import json
import os
from pathlib import Path
import sys

if __name__ == '__main__':
    runtime = json.loads(Path(__file__).with_name('runtime.json').read_text())
    env = dict(os.environ)
    env.update(HERDR_PLUGIN_CONFIG_DIR=runtime['config_dir'], HERDR_PLUGIN_STATE_DIR=runtime['state_dir'],
               HERDR_HERMES_ROOT=runtime['hermes_root'], HERDR_MANAGER_HERMES_BIN=runtime['hermes_bin'])
    os.execve(runtime['python'], [runtime['python'], str(Path(runtime['root'])/'hermes_manager.py'), 'route', '--', *sys.argv[1:]], env)
