import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from hermes_manager import inbox
from hermes_manager.core import Settings
from hermes_manager.ui import Sidebar
from hermes_manager.core import Profile


def stamp(offset=0):
    return datetime.fromtimestamp(time.time() - offset).strftime("%Y-%m-%d_%H-%M-%S")


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "profiles/alpha"
        (self.home / "cron/output/job1").mkdir(parents=True)
        (self.home / "cron/output/job2").mkdir(parents=True)
        (self.home / "config.yaml").write_text("")
        (self.home / "cron/jobs.json").write_text(json.dumps({"jobs": [
            {"id": "job1", "name": "Carousel", "deliver": "telegram:x", "last_status": "delivery_failed",
             "last_delivery_error": "Chat not found"},
            {"id": "job2", "name": "Health", "deliver": "slack"}]}))
        self.settings = Settings(self.root, self.root / "cfg", self.root / "state")
        self.run1 = self.home / "cron/output/job1" / (stamp(10) + ".md")
        self.run1.write_text("# Cron Job: Carousel\n\n**Job ID:** job1\n**Run Time:** now\n\n## Prompt\n\ndo it\n\n## Response\n\nHello **world**\n")
        self.run2 = self.home / "cron/output/job2" / (stamp(5) + ".md")
        self.run2.write_text("# Cron Job: Health\n\n**Job ID:** job2\n**Mode:** no_agent (script)\n**Status:** silent (empty output)\n")
        old = self.home / "cron/output/job2" / "2020-01-01_00-00-00.md"
        old.write_text("# Cron Job: Health\n\n## Response\n\nancient\n")

    def test_lists_runs_keeps_silent_as_read_and_flags_delivery_failure(self):
        items = inbox.load_inbox(self.settings)
        self.assertEqual([i["job_name"] for i in items], ["Health", "Carousel"])
        self.assertEqual([i["job_name"] for i in inbox.load_inbox(self.settings, include_silent=False)], ["Carousel"])
        quiet, item = items
        self.assertTrue(quiet["silent"] and not quiet["unread"])
        self.assertEqual(quiet["preview"], "Ran · nothing to report")
        self.assertTrue(item["unread"] and item["delivery_failed"])
        self.assertEqual(item["delivery_error"], "Chat not found")
        self.assertEqual(item["preview"], "Hello world")
        self.assertEqual(inbox.unread_summary(items), (1, 1))

    def test_profile_filter_only_returns_that_profile(self):
        other = self.root / "profiles/beta"
        (other / "cron/output/jobx").mkdir(parents=True)
        (other / "config.yaml").write_text("")
        (other / "cron/output/jobx" / (stamp(2) + ".md")).write_text("# Cron Job: X\n\n## Response\n\nbeta only\n")
        self.assertEqual({i["profile"] for i in inbox.load_inbox(self.settings)}, {"alpha", "beta"})
        self.assertEqual({i["profile"] for i in inbox.load_inbox(self.settings, "beta")}, {"beta"})
        self.assertEqual({i["profile"] for i in inbox.load_inbox(self.settings, "alpha")}, {"alpha"})
        popup = inbox.InboxPopup(self.settings, "beta/jobx/" + next((other / "cron/output/jobx").iterdir()).name)
        self.assertEqual({i["profile"] for i in popup.items}, {"beta"})

    def test_mark_read_and_unread_roundtrip(self):
        key = inbox.load_inbox(self.settings)[1]["id"]
        inbox.mark_read(self.settings, [key])
        self.assertFalse(inbox.load_inbox(self.settings)[1]["unread"])
        inbox.mark_read(self.settings, [key], read=False)
        self.assertTrue(inbox.load_inbox(self.settings)[1]["unread"])
        inbox.mark_all_read(self.settings, inbox.load_inbox(self.settings))
        self.assertFalse(inbox.load_inbox(self.settings)[1]["unread"])

    def test_first_use_does_not_flood_with_old_runs(self):
        older = self.home / "cron/output/job1" / (stamp(3 * 86400) + ".md")
        older.write_text("# Cron Job: Carousel\n\n## Response\n\nold news\n")
        items = {Path(i["path"]).name: i for i in inbox.load_inbox(self.settings)}
        self.assertFalse(items[older.name]["unread"])

    def test_item_lookup_rejects_traversal(self):
        with self.assertRaises(ValueError):
            inbox.item_for_key(self.settings, "alpha/../../etc/passwd")

    def test_conversation_links_session_and_trims_prompt(self):
        item = inbox.load_inbox(self.settings)[1]
        with sqlite3.connect(str(self.home / "state.db")) as c:
            c.executescript("CREATE TABLE sessions(id TEXT,started_at REAL);"
                            "CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,"
                            "tool_calls TEXT,tool_name TEXT,timestamp REAL);")
            c.execute("INSERT INTO sessions VALUES('cron_job1_x',?)", (item["run_at"] - 20,))
            c.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES('cron_job1_x','user',?,1)", ("p" * 900,))
            c.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES('cron_job1_x','assistant','Done',2)")
        session, messages = inbox.load_conversation(self.settings, item)
        self.assertEqual(session, "cron_job1_x")
        self.assertEqual([m[0] for m in messages], ["PROMPT", "AGENT"])
        self.assertIn("more characters of prompt hidden", messages[0][1])
        lines = inbox.conversation_lines(self.settings, item, 60)
        self.assertTrue(all(len(l) <= 70 for l in lines))

    def test_run_without_session_explains_itself(self):
        item = inbox.load_inbox(self.settings)[1]
        session, message = inbox.load_conversation(self.settings, item)
        self.assertIsNone(session)
        self.assertIn("No agent conversation", message)

    def test_reply_hands_the_exact_cron_session_to_the_chat(self):
        item = inbox.load_inbox(self.settings)[1]
        with sqlite3.connect(str(self.home / "state.db")) as c:
            c.executescript("CREATE TABLE sessions(id TEXT,started_at REAL);")
            c.execute("INSERT INTO sessions VALUES('cron_job1_x',?)", (item["run_at"] - 20,))
        popup = inbox.InboxPopup(self.settings, item["id"])
        popup.start_reply()
        self.assertEqual(popup.chat[1], "cron_job1_x")
        self.assertEqual(popup.chat[0].name, "alpha")
        self.assertFalse(popup.running)

    def test_reply_on_script_only_run_explains_instead_of_chatting(self):
        popup = inbox.InboxPopup(self.settings, inbox.load_inbox(self.settings)[1]["id"])
        popup.start_reply()
        self.assertIsNone(popup.chat)
        self.assertTrue(popup.running)
        self.assertIn("script-only", popup.status)

    def test_sidebar_inbox_navigation_opens_popup(self):
        sidebar = Sidebar(Mock(), Profile("alpha", self.home, "Alpha", self.home))
        self.addCleanup(sidebar.executor.shutdown, wait=False)
        sidebar.inbox = [{"id": "alpha/job1/a.md", "unread": True}]
        sidebar.action = Mock()
        for _ in range(3):
            sidebar.handle(9)
        self.assertEqual(sidebar.section, "inbox")
        sidebar.handle(10)
        sidebar.action.assert_called_once()


if __name__ == "__main__":
    unittest.main()
