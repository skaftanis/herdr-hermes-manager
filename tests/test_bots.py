import unittest
from unittest.mock import Mock, patch
from pathlib import Path
from hermes_manager.bots import BotPopup, message_text, open_bot, configure_bots, BRIDGE
from hermes_manager.core import Profile
from hermes_manager.bots_activity import activity
import sqlite3
import tempfile
import subprocess
import sys
import time
import hashlib
from types import SimpleNamespace
from hermes_manager.core import read_json, write_json


class BotPopupTests(unittest.TestCase):
    def test_bot_open_from_running_manager_with_pre_packaging_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            profile = Profile('alpha', root, 'Alpha', root)
            # Exactly the old Settings shape: no agent_root or host_config.
            settings = SimpleNamespace(root=root, state_dir=root/'state', config={},
                hermes_bin=root/'hermes', plugin_id='hhl.hermes-manager', profiles=lambda: [profile])
            write_json(settings.state_dir/'bots-worker.json', {'pid': 123,
                'revision': hashlib.sha256(BRIDGE.read_bytes()).hexdigest()})
            api = Mock(socket_path=str(root/'herdr.sock'))
            with patch('hermes_manager.bots.os.kill'), patch.dict('os.environ', {'XDG_CONFIG_HOME':str(root/'config')}):
                open_bot(settings, api, profile)
            runtime = read_json(settings.state_dir/'bots-runtime.json')
            self.assertEqual(runtime['agent_root'], str(root/'hermes-agent'))
            self.assertEqual(runtime['profiles'], ['alpha'])
            api.call.assert_called_once()
            self.assertEqual(api.call.call_args[0][1]['env']['HERDR_HERMES_BOT'], 'alpha')

    def test_selection_extracts_multiline_unicode_in_either_direction(self):
        p = self.popup()
        p.visible_text = {3: 'Hello κόσμε', 4: 'Second line'}
        p.selection = ((3, 8), (4, 7))
        self.assertEqual(p.selected_text(), 'κόσμε\nSecond')
        p.selection = tuple(reversed(p.selection))
        self.assertEqual(p.selected_text(), 'κόσμε\nSecond')
        p.scroll_history(3)
        self.assertIsNone(p.selection)

    def test_live_thinking_and_tool_activity_then_completion(self):
        p = self.popup()
        def event(kind, **payload):
            p.consume({'type': 'event', 'event': {'type': kind, 'payload': payload}})
        event('message.start')
        self.assertTrue(p.busy)
        event('reasoning.delta', text='Checking the available files.')
        self.assertIn('Checking the available files.', p.activity_lines()[-1])
        event('tool.start', tool_id='one', name='terminal', context='List project files')
        self.assertEqual(p.status, 'Using terminal')
        event('tool.complete', tool_id='one', name='terminal', duration_s=1.2, summary='Found three files')
        self.assertIn('Found three files', p.activity[-1])
        self.assertFalse(p.active_tools)
        event('message.complete', text='Done')
        self.assertFalse(p.busy)
        self.assertFalse(any(line.startswith('Thinking:') for line in p.activity_lines()))
        event('message.start')
        self.assertEqual(p.thinking, '')
        self.assertEqual(p.activity, [])

    def test_activity_buffers_stay_bounded(self):
        p = self.popup()
        for i in range(30):
            p.consume({'type': 'event', 'event': {'type': 'reasoning.delta', 'payload': {'text': 'x'*300}}})
            p.consume({'type': 'event', 'event': {'type': 'tool.start', 'payload': {'name': 'search', 'tool_id': 'same'}}})
        self.assertLessEqual(len(p.thinking), 2000)
        self.assertLessEqual(len(p.activity), 8)
        self.assertLessEqual(len(p.activity_lines()), 3)

    def test_send_never_writes_to_pipe_on_ui_thread(self):
        p = BotPopup(Mock(), Profile('alpha', Path('/tmp'), 'Alpha', Path('/tmp')))
        p.child = Mock()
        p.child.poll.return_value = None
        p.send({'type': 'send', 'text': 'hello'})
        p.child.stdin.write.assert_not_called()
        self.assertEqual(p.outgoing.get_nowait()['text'], 'hello')

    def test_scroll_both_directions_and_clamp(self):
        p = self.popup()
        p.scroll_history(12)
        self.assertEqual(p.scroll, 12)
        p.scroll_history(-30)
        self.assertEqual(p.scroll, 0)

    def test_close_bridge_does_not_wait_for_stalled_connection(self):
        p = self.popup()
        p.child = subprocess.Popen([sys.executable, '-c',
            'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print("ready",flush=True); time.sleep(30)'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(p.child.stdout.readline().strip(), 'ready')
            start = time.monotonic()
            p.close_bridge()
            self.assertLess(time.monotonic() - start, 0.6)
            self.assertIsNotNone(p.child.poll())
        finally:
            if p.child.poll() is None:
                p.child.kill()
                p.child.wait()
            p.child.stdout.close()

    def test_popup_launch_can_resolve_manifest_script_outside_profile_directory(self):
        with patch('hermes_manager.bots.configure_bots'):
            api = Mock()
            settings = Mock(plugin_id='hhl.hermes-manager')
            profile = Profile('alpha', Path('/tmp/alpha'), 'Alpha', Path('/tmp'))
            open_bot(settings, api, profile)
            params = api.call.call_args[0][1]
            self.assertEqual(params['env']['HERDR_HERMES_BOT'], 'alpha')
            self.assertEqual(Path(params['cwd']), BRIDGE.parent.parent)
            self.assertTrue((Path(params['cwd']) / 'hermes_manager.py').is_file())

    def popup(self):
        p=BotPopup(Mock(),Profile('alpha',Path('/tmp/alpha'),'Alpha',Path('/tmp')))
        p.send=Mock()
        return p

    def test_snapshot_preserves_history_and_hides_internal_messages(self):
        p=self.popup()
        p.consume({'type':'session','session':{'messages':[{'role':'user','text':'hello'},
                   {'role':'assistant','text':'hi'},{'role':'user','text':'internal','display_kind':'hidden'}]}})
        self.assertEqual(p.messages,[('user','hello'),('assistant','hi')])
        self.assertTrue(p.ready)
        p.send.assert_not_called()

    def test_stream_seals_and_requests_authoritative_history(self):
        p=self.popup()
        p.consume({'type':'event','event':{'type':'message.delta','payload':{'text':'answer'}}})
        self.assertEqual(p.stream,'answer')
        p.consume({'type':'event','event':{'type':'message.complete','payload':{'text':'answer'}}})
        self.assertEqual(p.messages,[('assistant','answer')])
        p.send.assert_called_once_with({'type':'refresh'})

    def test_approval_waits_for_user_and_can_be_denied(self):
        p=self.popup()
        p.consume({'type':'request','request':{'id':'srq-1','method':'approval',
                  'params':{'choices':['once','deny'],'command':'some command'}}})
        p.send.assert_not_called()
        p.reply(decline=True)
        p.send.assert_called_once_with({'type':'reply','id':'srq-1','result':{'choice':'deny'}})

    def test_clarify_batch_is_answered_without_profile_or_session_switch(self):
        p=self.popup()
        p.requests=[{'id':'q','method':'clarify','params':{'questions':[
            {'qid':'one','question':'First?'},{'qid':'two','question':'Second?'}]}}]
        p.draft='first';p.reply();p.send.assert_not_called()
        p.draft='second';p.reply()
        p.send.assert_called_once_with({'type':'reply','id':'q','result':{'answers':{'one':'first','two':'second'}}})

    def test_attachments_render_without_raw_image_data(self):
        self.assertEqual(message_text([{'type':'text','text':'hello'},{'type':'image_url','image_url':'secret'}]),'hello\n[attachment]')

    def test_shared_read_watermark_changes_badge_and_is_profile_scoped(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for name,read in [('alpha',1.0),('beta',5.0)]:
                home=root/'profiles'/name;home.mkdir(parents=True)
                with sqlite3.connect(home/'state.db') as db:
                    db.executescript('CREATE TABLE sessions(id TEXT,last_read_at REAL,last_activity_at REAL,started_at REAL);'
                                     'CREATE TABLE messages(session_id TEXT,timestamp REAL);')
                    db.execute('INSERT INTO sessions VALUES (?,?,?,?)',('same-id',read,3.0,0.0))
            self.assertEqual(activity(root,'alpha','same-id'),{'tracked':True,'unread':True})
            self.assertEqual(activity(root,'beta','same-id'),{'tracked':True,'unread':False})
            with sqlite3.connect(root/'profiles/alpha/state.db') as db:
                # Simulates the native PATCH API, never the plugin reader.
                db.execute('UPDATE sessions SET last_read_at=6')
            self.assertFalse(activity(root,'alpha','same-id')['unread'])


if __name__=='__main__': unittest.main()
