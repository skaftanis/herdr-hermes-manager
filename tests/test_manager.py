import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from hermes_manager.core import (APIError, Profile, Settings, clean_text, get_conversation,
                                 load_conversations, load_jobs, workspace_profile)
from hermes_manager.control import Manager, pane_path
from hermes_manager.cli import routed_arguments


def make_database(home):
    home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(home / "state.db")) as c:
        c.executescript("CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,started_at REAL,"
                        "last_activity_at REAL,title TEXT,cwd TEXT,message_count INTEGER,"
                        "parent_session_id TEXT,hidden INTEGER DEFAULT 0,archived INTEGER DEFAULT 0);"
                        "CREATE TABLE messages(session_id TEXT,role TEXT,content TEXT,timestamp REAL);")


class ProfileFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        make_database(self.root)
        self.profile = Profile("default", self.root, "Default", self.root)

    def tearDown(self):
        self.temp.cleanup()

    def settings(self):
        return Settings(self.root, self.root / "plugin-config", self.root / "plugin-state")


class DataTests(ProfileFixture, unittest.TestCase):
    def test_discovery_ignores_deleted_and_ghost_profiles(self):
        named = self.root / "profiles"
        for name in ("alpha", "deleted", "ghost", ".internal", "../../escape"):
            if "/" in name:
                continue
            (named/name).mkdir(parents=True)
            if name != "ghost":
                (named/name/"config.yaml").write_text("{}")
        (named/".deleted").mkdir()
        (named/".deleted/deleted").write_text("deleted")
        self.assertEqual([p.name for p in self.settings().profiles()], ["default", "alpha"])

    def test_profile_labels_read_only_public_metadata(self):
        (self.root/"profile.yaml").write_text("display_name: 'My Hermes'\ndescription: secret\n")
        self.assertEqual(self.settings().profile("default").title,"My Hermes")

    def test_conversations_order_by_activity_and_hide_background(self):
        with sqlite3.connect(str(self.root/"state.db")) as c:
            c.executemany("INSERT INTO sessions(id,source,started_at,title,hidden) VALUES(?,?,?,?,?)",[
                ("old","cli",1,"Older chat",0),("new","desktop",10,"Newer chat",0),
                ("cron","cron",100,"Cron noise",0),("hidden","cli",1000,"Hidden",1)])
            c.execute("INSERT INTO messages VALUES('old','user','New activity',20)")
        before = (self.root/"state.db").read_bytes()
        self.assertEqual([r["id"] for r in load_conversations(self.profile)], ["old","new"])
        self.assertEqual([r["id"] for r in load_conversations(self.profile,include_background=True)], ["cron","old","new"])
        self.assertEqual(before,(self.root/"state.db").read_bytes())

    def test_session_lookup_cannot_cross_profiles(self):
        other = self.root/"profiles/other"
        make_database(other)
        with sqlite3.connect(str(other/"state.db")) as c:
            c.execute("INSERT INTO sessions(id,source,started_at,title) VALUES('private','cli',1,'Other')")
        with self.assertRaises(ValueError):
            get_conversation(self.profile,"private")

    def test_session_ids_are_parameters(self):
        with self.assertRaises(ValueError):
            get_conversation(self.profile,"' OR 1=1 --")

    def test_unnamed_chat_uses_first_user_message(self):
        with sqlite3.connect(str(self.root/"state.db")) as c:
            c.execute("INSERT INTO sessions(id,source,started_at) VALUES('chat','cli',1)")
            c.execute("INSERT INTO messages VALUES('chat','user',?,2)",
                      (json.dumps([{"type":"text","text":"A real conversation"}]),))
        self.assertEqual(load_conversations(self.profile)[0]["title"],"A real conversation")

    def test_cron_data_is_scoped_to_profile(self):
        (self.root/"cron").mkdir()
        (self.root/"cron/jobs.json").write_text(json.dumps({"jobs":[{"id":"one","enabled":False,"state":"paused"}]}))
        other = self.root/"profiles/other"
        other.mkdir(parents=True)
        self.assertEqual(len(load_jobs(self.profile)),1)
        self.assertEqual(load_jobs(Profile("other",other,"Other",other)),[])

    def test_control_sequences_are_never_rendered(self):
        self.assertNotIn("\x1b",clean_text("Title\x1b[2J\nnext"))


class RoutingAPI:
    def __init__(self, profile):
        self.profile = profile
    def call(self, method, params=None):
        if method == "pane.current":
            return {"pane":{"workspace_id":"managed"}}
        if method == "workspace.get":
            return {"workspace":{"workspace_id":"managed","tokens":{"hermes-profile":self.profile} if self.profile else {}}}
        raise AssertionError(method)
    def state_key(self):
        return "test"


class RoutingTests(ProfileFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        make_database(self.root/"profiles/alpha")
        self.env = {"HERDR_ENV":"1","HERDR_WORKSPACE_ID":"managed","HERDR_PANE_ID":"managed:p1"}

    def test_bare_and_subcommand_invocations_follow_workspace(self):
        for argv in ([],["cron","list"],["--resume","latest"]):
            self.assertEqual(routed_arguments(argv,self.settings(),RoutingAPI("alpha"),self.env),["-p","alpha",*argv])

    def test_explicit_profile_always_wins(self):
        for argv in (["-p","default"],["--profile=default"],["-pdefault"]):
            self.assertEqual(routed_arguments(argv,self.settings(),RoutingAPI("alpha"),self.env),argv)

    def test_qualified_restore_is_left_to_existing_restore_launcher(self):
        argv=["--resume","herdr-profile:default:session"]
        self.assertEqual(routed_arguments(argv,self.settings(),RoutingAPI("alpha"),self.env),argv)

    def test_unmanaged_workspace_and_outside_herdr_keep_normal_behavior(self):
        self.assertEqual(routed_arguments([],self.settings(),RoutingAPI(None),self.env),[])
        self.assertEqual(routed_arguments([],self.settings(),RoutingAPI("alpha"),{}),[])

    def test_moved_pane_uses_its_current_workspace(self):
        # The inherited workspace id can be stale after Herdr moves a running pane.
        self.env["HERDR_WORKSPACE_ID"]="old"
        self.assertEqual(routed_arguments([],self.settings(),RoutingAPI("alpha"),self.env),["-p","alpha"])


class WorkspaceTests(unittest.TestCase):
    def test_only_hermes_spaces_are_adopted(self):
        pane={"workspace_id":"w1","agent_session":{"value":"herdr-profile:alpha:chat"}}
        snapshot={"panes":[pane]}
        self.assertEqual(workspace_profile({"workspace_id":"w1","label":"(H) Alpha"},snapshot),"alpha")
        self.assertIsNone(workspace_profile({"workspace_id":"w1","label":"Project"},snapshot))

    def test_mixed_profiles_are_not_guessed(self):
        snapshot={"panes":[{"workspace_id":"w1","agent_session":{"value":"herdr-profile:"+p+":chat"}}
                            for p in ("alpha","beta")]}
        self.assertIsNone(workspace_profile({"workspace_id":"w1","label":"(H) Mixed"},snapshot))

    def test_layout_path_accounts_for_nested_horizontal_splits(self):
        leaf=lambda p:{"type":"pane","pane_id":p}
        root={"type":"split","direction":"right","ratio":0.8,"first":leaf("main"),
              "second":{"type":"split","direction":"down","ratio":0.5,"first":leaf("a"),"second":leaf("b")}}
        path,width=pane_path(root,"b")
        self.assertEqual(path,[1,1])
        self.assertAlmostEqual(width,0.2)


if __name__ == "__main__":
    unittest.main()
