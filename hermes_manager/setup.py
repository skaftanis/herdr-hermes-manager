"""Portable, reversible shell/config setup; never replace the Hermes executable."""
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

from .core import Settings, read_json, write_json

ROOT = Path(__file__).resolve().parents[1]
BEGIN = '# >>> hhl.hermes-manager >>>'
END = '# <<< hhl.hermes-manager <<<'
PATTERN = re.compile(r'\n?' + re.escape(BEGIN) + r'\n.*?' + re.escape(END) + r'\n?', re.S)
DIVIDER = '''[ui.sidebar.spaces]
rows = [
  [{ token = "$hermes-divider", fg = "#7a83a8", bold = true }],
  ["state_icon", "workspace"],
  ["branch", "git_status"],
]
'''


def remove_block(text):
    return PATTERN.sub('\n', text)


def replace_block(text, body):
    return remove_block(text).rstrip() + '\n\n' + BEGIN + '\n' + body.rstrip() + '\n' + END + '\n'


def write_text(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(value)
    path.chmod(0o600)


def initialize(settings=None):
    settings = settings or Settings()
    if not (settings.config_dir/'config.json').exists():
        write_json(settings.config_dir/'config.json', {'sidebar_width': 46, 'conversation_limit': 50,
            'profile_labels': {}, 'profile_cwds': {}})
    # Installed roots can move on plugin update; shell integration follows this pointer.
    runtime = {'root': str(ROOT), 'python': sys.executable, 'config_dir': str(settings.config_dir),
               'state_dir': str(settings.state_dir), 'hermes_root': str(settings.root),
               'hermes_bin': str(settings.hermes_bin)}
    runtime_path = settings.config_dir/'runtime.json'
    if read_json(runtime_path) != runtime:
        write_json(runtime_path, runtime)
    dispatch = settings.config_dir/'shell_dispatch.py'
    contents = (ROOT/'hermes_manager/shell_dispatch.py').read_text()
    if not dispatch.exists() or dispatch.read_text() != contents:
        write_text(dispatch, contents)
    return runtime


def doctor(settings=None):
    settings = settings or Settings()
    node = shutil.which('node')
    checks = {'python_3_9': sys.version_info >= (3, 9), 'hermes': settings.hermes_bin.is_file(),
              'hermes_profiles': bool(settings.profiles()), 'node_22': False, 'websocket_dependency': False}
    if node:
        version = subprocess.run([node, '-p', 'process.versions.node.split(".")[0]'], capture_output=True, text=True)
        checks['node_22'] = version.returncode == 0 and int(version.stdout.strip() or 0) >= 22
        result = subprocess.run([node, '--input-type=module', '-e',
            "import {createRequire} from 'node:module';const r=createRequire(process.argv[1]);r('ws');", str(ROOT/'package.json')],
            capture_output=True, text=True)
        checks['websocket_dependency'] = result.returncode == 0
    return {'ok': all(checks.values()), 'checks': checks}


def setup(settings=None, shell=None):
    settings = settings or Settings()
    checks = doctor(settings)
    if not checks['ok']:
        raise ValueError('Missing requirements: ' + ', '.join(k for k,v in checks['checks'].items() if not v))
    initialize(settings)
    shell = shell or Path(os.environ.get('SHELL', '/bin/zsh')).name
    if shell not in ('zsh', 'bash', 'none'):
        raise ValueError('Automatic shell setup supports zsh/bash. Use --shell none for other shells.')
    backup = settings.host_config/'hermes-manager-backups'/datetime.now().strftime('%Y%m%d-%H%M%S-%f-setup')
    backup.mkdir(parents=True, mode=0o700)
    config_path = settings.host_config/'config.toml'
    config = config_path.read_text() if config_path.exists() else ''
    if config_path.exists(): shutil.copy2(config_path, backup/'config.toml')
    # Custom Spaces formats are respected; the divider can be added manually.
    unowned_config = remove_block(config)
    additions = '' if re.search(r'^\s*\[ui\.sidebar\.spaces\]', unowned_config, re.M) else DIVIDER
    if 'hhl.hermes-manager.sidebar' not in unowned_config:
        key = 'prefix+h' if '"prefix+h"' not in unowned_config else 'prefix+alt+h'
        if '"'+key+'"' not in unowned_config:
            additions += '\n[[keys.command]]\nkey = "'+key+'"\ntype = "plugin_action"\ncommand = "hhl.hermes-manager.sidebar"\ndescription = "Focus Hermes Manager"\n'
    if additions:
        write_text(config_path, replace_block(config, additions))
    source_path = settings.config_dir/'shell.sh'
    dispatcher = settings.config_dir/'shell_dispatch.py'
    runtime_path = settings.config_dir/'runtime.json'
    shell_text = '''# Hermes Manager: only intercept interactive calls inside Herdr.
hermes() {
  if [ "${HERDR_ENV:-}" = "1" ] && [ -f RUNTIME ]; then
    PYTHON DISPATCH "$@"
  else
    command hermes "$@"
  fi
}
'''.replace('RUNTIME', shlex.quote(str(runtime_path))).replace('PYTHON', shlex.quote(sys.executable)).replace('DISPATCH', shlex.quote(str(dispatcher)))
    write_text(source_path, shell_text)
    record_path = settings.config_dir/'setup.json'
    record = read_json(record_path)
    rc = None
    if shell != 'none':
        rc = (Path(os.environ.get('ZDOTDIR') or Path.home())/'.zshrc' if shell == 'zsh' else Path.home()/'.bashrc')
        text = rc.read_text() if rc.exists() else ''
        if rc.exists(): shutil.copy2(rc, backup/'shell-rc')
        write_text(rc, replace_block(text, '[ ! -r '+shlex.quote(str(source_path))+' ] || . '+shlex.quote(str(source_path))))
        record['shell_rcs'] = sorted(set(record.get('shell_rcs', []) + [str(rc)]))
    record.update(backup=str(backup), host_config=str(config_path))
    write_json(record_path, record)
    return {'ok': True, 'backup': str(backup), 'shell_rc': str(rc) if rc else None,
            'shell_source': str(source_path), 'divider_added': bool(additions and DIVIDER in additions)}


def uninstall(settings=None):
    settings = settings or Settings()
    record = read_json(settings.config_dir/'setup.json')
    changed = []
    paths = [Path(p) for p in record.get('shell_rcs', [])]
    paths.append(settings.host_config/'config.toml')
    for path in paths:
        if not path.exists(): continue
        original = path.read_text()
        updated = remove_block(original)
        if original != updated:
            write_text(path, updated)
            changed.append(str(path))
    # Loaded shell functions fall back to the normal executable when runtime is absent.
    (settings.config_dir/'runtime.json').unlink(missing_ok=True)
    (settings.config_dir/'shell.sh').unlink(missing_ok=True)
    return {'removed_managed_blocks': changed, 'profiles_and_conversations_preserved': True}
