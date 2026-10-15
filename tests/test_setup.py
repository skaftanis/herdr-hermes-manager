import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from hermes_manager.core import Settings
from hermes_manager.setup import initialize, setup, uninstall, BEGIN
from hermes_manager.routing import resume_arguments
from test_manager import make_database
import sqlite3


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='hermes-manager-setup-')
        self.addCleanup(self.folder.cleanup)
        self.home = Path(self.folder.name)
        self.env = {k:v for k,v in os.environ.items() if not k.startswith(('HERDR_', 'HERMES_', 'XDG_', 'ZDOTDIR'))}
        self.env.update(HOME=str(self.home), SHELL='/bin/zsh')
        self.patcher = patch.dict(os.environ, self.env, clear=True)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.root = self.home/'.hermes'
        make_database(self.root)
        self.fake = self.home/'hermes-original'
        self.fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        self.fake.chmod(0o700)
        self.settings = Settings()
        self.settings.hermes_bin = self.fake

    @patch('hermes_manager.setup.doctor', return_value={'ok': True})
    def test_fresh_setup_idempotent_removal_preserves_user_content_and_native_binary(self, _):
        rc = self.home/'.zshrc'
        rc.write_text('export MY_SETTING=keep\n')
        config = self.settings.host_config/'config.toml'
        config.parent.mkdir(parents=True)
        config.write_text('onboarding = false\n')
        before = hashlib.sha256(self.fake.read_bytes()).hexdigest()
        setup(self.settings, 'zsh')
        setup(self.settings, 'zsh')
        self.assertEqual(rc.read_text().count(BEGIN), 1)
        self.assertEqual(config.read_text().count('[ui.sidebar.spaces]'), 1)
        self.assertIn('"$hermes-divider"', config.read_text())
        # User additions made after setup survive uninstall.
        with rc.open('a') as f: f.write('export LATER_SETTING=keep\n')
        uninstall(self.settings)
        self.assertIn('MY_SETTING=keep', rc.read_text())
        self.assertIn('LATER_SETTING=keep', rc.read_text())
        self.assertNotIn(BEGIN, rc.read_text())
        self.assertIn('onboarding = false', config.read_text())
        self.assertNotIn('$hermes-divider', config.read_text())
        self.assertEqual(before, hashlib.sha256(self.fake.read_bytes()).hexdigest())

    @patch('hermes_manager.setup.doctor', return_value={'ok': True})
    def test_custom_sidebar_format_is_preserved(self, _):
        config = self.settings.host_config/'config.toml'
        config.parent.mkdir(parents=True)
        original = '[ui.sidebar.spaces]\nrows = [["workspace"]]\n'
        config.write_text(original)
        result = setup(self.settings, 'none')
        self.assertFalse(result['divider_added'])
        self.assertIn(original.strip(), config.read_text())
        self.assertFalse((self.home/'.zshrc').exists())

    def test_initialize_follows_updated_plugin_root_without_shell_changes(self):
        initialize(self.settings)
        rc = self.home/'.zshrc'
        self.assertFalse(rc.exists())
        runtime = json.loads((self.settings.config_dir/'runtime.json').read_text())
        runtime['root'] = '/old/installation'
        (self.settings.config_dir/'runtime.json').write_text(json.dumps(runtime))
        initialize(self.settings)
        self.assertNotEqual(json.loads((self.settings.config_dir/'runtime.json').read_text())['root'], '/old/installation')

    def test_fresh_restore_decodes_profile_and_refuses_cross_profile(self):
        with sqlite3.connect(self.root/'state.db') as db:
            db.execute('INSERT INTO sessions(id,title) VALUES (?,?)', ('abc', 'A chat'))
        self.assertEqual(resume_arguments(['--resume', 'herdr-profile:default:abc'], self.settings), ['-p', 'default', '--resume', 'abc'])
        with self.assertRaises(ValueError):
            resume_arguments(['-p', 'other', '--resume', 'herdr-profile:default:abc'], self.settings)
        self.assertEqual(resume_arguments(['--resume=herdr-profile:default:fresh_'+'a'*32], self.settings), ['-p', 'default'])

    @patch('hermes_manager.setup.doctor', return_value={'ok': True})
    def test_shell_function_executes_original_and_falls_back_after_removal(self, _):
        setup(self.settings, 'bash')
        shell = self.settings.config_dir/'shell.sh'
        # Explicit profile needs no live Herdr socket; the real route/dispatcher run.
        env = dict(os.environ, HERDR_ENV='1')
        command = '. "$1"; hermes -p default --version'
        result = subprocess.run(['/bin/bash', '-c', command, 'test', str(shell)], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['-p', 'default', '--version'])

if __name__ == '__main__': unittest.main()
