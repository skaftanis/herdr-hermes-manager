import curses
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from hermes_manager.core import Profile, write_json
from hermes_manager.ui import Sidebar


class SidebarBotsTests(unittest.TestCase):
    def sidebar(self):
        manager = Mock()
        sidebar = Sidebar(manager, Profile('alpha', Path('/tmp'), 'Alpha', Path('/tmp')))
        self.addCleanup(sidebar.executor.shutdown, wait=False)
        sidebar.bots = [{'id': str(i), 'label': 'Bot '+str(i), 'icon': '✦', 'unread': i == 0, 'busy': i == 1} for i in range(11)]
        sidebar.conversations = [{'id': str(i), 'title': 'Chat'} for i in range(50)]
        sidebar.jobs = [{'id': str(i), 'name': 'Job', 'enabled': True} for i in range(7)]
        return sidebar

    @patch('hermes_manager.ui.curses.color_pair', return_value=0)
    def test_grid_stays_above_footer_and_every_bot_can_be_reached(self, _):
        for height, width in [(24, 25), (35, 46), (60, 46), (90, 70)]:
            sidebar = self.sidebar()
            sidebar.screen = Mock()
            sidebar.screen.getmaxyx.return_value = (height, width)
            sidebar.put = Mock()
            sidebar.section = 'bots'
            for selected in range(11):
                sidebar.selected['bots'] = selected
                sidebar.draw()
                bots = [h for h in sidebar.hitboxes if h[3] == 'bot']
                self.assertTrue(any(h[4] == selected for h in bots))
                self.assertTrue(all(0 <= h[0] < height-7 for h in bots))
                other_rows = {h[0] for h in sidebar.hitboxes if h[3] in ('job', 'toggle', 'conversation')}
                self.assertFalse(other_rows.intersection(h[0] for h in bots))

    def test_tab_navigation_and_click_open_selected_profile(self):
        sidebar = self.sidebar()
        sidebar.handle(9)
        self.assertEqual(sidebar.section, 'conversations')
        sidebar.handle(9)
        self.assertEqual(sidebar.section, 'bots')
        sidebar.open_bot = Mock()
        sidebar.hitboxes = [(20, 1, 12, 'bot', 5)]
        sidebar.mouse(3, 20, curses.BUTTON1_PRESSED)
        self.assertEqual(sidebar.current()['id'], '5')
        sidebar.open_bot.assert_called_once()

    def test_roster_falls_back_to_local_profiles_and_ignores_removed_profiles(self):
        sidebar = self.sidebar()
        with tempfile.TemporaryDirectory() as folder:
            sidebar.manager.settings.state_dir = Path(folder)
            sidebar.manager.settings.profiles.return_value = [sidebar.profile]
            self.assertEqual(sidebar.load_bots()[0]['id'], 'alpha')
            write_json(Path(folder)/'bots-roster.json', [
                {'id': 'alpha', 'label': 'Alpha', 'unread': True}, {'id': 'removed'}])
            self.assertEqual(len(sidebar.load_bots()), 1)
            self.assertTrue(sidebar.load_bots()[0]['unread'])

    @patch('hermes_manager.bots.open_bot')
    def test_bot_launch_is_profile_scoped(self, opened):
        sidebar = self.sidebar()
        sidebar.selected['bots'] = 4
        sidebar.action = lambda fn, message: fn()
        sidebar.open_bot()
        sidebar.manager.settings.profile.assert_called_once_with('4')
        self.assertEqual(opened.call_args[0][2], sidebar.manager.settings.profile.return_value)

if __name__ == '__main__':
    unittest.main()
