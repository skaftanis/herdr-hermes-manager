from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shlex
import subprocess
import time

from .core import (APIError, DIVIDER_TEXT, DIVIDER_TOKEN, PANE_TOKEN, PROFILE_TOKEN, Herdr, Settings, clean_text,
                   current_profile, get_conversation, job_enabled, load_jobs, read_json,
                   workspace_profile, write_json)


@contextmanager
def sync_lock(settings, api, wait=False):
    settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (settings.state_dir / (api.state_key() + ".lock")).open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def manager_pane(pane):
    return bool(pane.get("tokens", {}).get(PANE_TOKEN)) or pane.get("label") == "Hermes Manager"


def pane_path(root, pane_id, path=None, width=1.0):
    path = [] if path is None else path
    if root.get("type") == "pane":
        return (path, width) if root.get("pane_id") == pane_id else None
    if root.get("type") != "split":
        return None
    ratio = root.get("ratio", 0.5)
    horizontal = root.get("direction") == "right"
    return (pane_path(root["first"], pane_id, path + [0], width * ratio if horizontal else width)
            or pane_path(root["second"], pane_id, path + [1], width * (1-ratio) if horizontal else width))


def node_at(root, path):
    for branch in path:
        root = root["first" if branch == 0 else "second"]
    return root


class Manager:
    def __init__(self, settings=None, api=None):
        self.settings = settings or Settings()
        self.api = api or Herdr()
        self.state_file = self.settings.state_dir / (self.api.state_key() + ".json")

    def sync(self, force=False, periodic=False):
        with sync_lock(self.settings, self.api, wait=force) as acquired:
            if not acquired:
                return {"skipped": "sync in progress"}
            state = read_json(self.state_file)
            gap = 10 if periodic else 0.25
            if not force and time.time() - state.get("synced_at", 0) < gap:
                return {"skipped": "recent sync"}
            installed = self.api.call("plugin.list", {"plugin_id": self.settings.plugin_id}).get("plugins", [])
            if not any(p.get("plugin_id") == self.settings.plugin_id and p.get("enabled") for p in installed):
                return {"skipped": "plugin disabled"}
            snapshot = self.api.snapshot()
            profiles = self.settings.profiles()
            workspaces = snapshot["workspaces"]
            saved = state.get("workspaces", {})
            chosen, used = {}, set()
            created, panels, errors = [], [], []
            for profile in profiles:
                workspace = next((w for w in workspaces if w["workspace_id"] not in used
                                  and workspace_profile(w, snapshot) == profile.name), None)
                if workspace is None:
                    old_id = saved.get(profile.name, {}).get("workspace_id")
                    workspace = next((w for w in workspaces if w["workspace_id"] == old_id
                                      and w.get("label", "").startswith("(H) ") and old_id not in used), None)
                if workspace is None:
                    workspace = next((w for w in workspaces if w.get("label") == "(H) " + profile.title
                                      and w["workspace_id"] not in used), None)
                if workspace is None:
                    result = self.api.call("workspace.create", {
                        "cwd": str(profile.cwd), "label": "(H) " + profile.title, "focus": False,
                        "env": {"HERDR_HERMES_PROFILE": profile.name},
                    })
                    workspace = result["workspace"]
                    workspaces.append(workspace)
                    snapshot["tabs"].append(result["tab"])
                    snapshot["panes"].append(result["root_pane"])
                    created.append(profile.name)
                workspace_id = workspace["workspace_id"]
                used.add(workspace_id)
                chosen[profile.name] = {"workspace_id": workspace_id, "label": workspace["label"]}
                divider = DIVIDER_TEXT if profile == profiles[0] else None
                current_tokens = workspace.get("tokens", {})
                if (current_tokens.get(PROFILE_TOKEN) != profile.name
                        or current_tokens.get(DIVIDER_TOKEN) != divider):
                    self.api.call("workspace.report_metadata", {
                        "workspace_id": workspace_id, "source": self.settings.plugin_id,
                        "tokens": {PROFILE_TOKEN: profile.name, DIVIDER_TOKEN: divider},
                    })
                # Save identity before pane launches: new shells can immediately resolve their profile.
                state["workspaces"] = {**saved, **chosen}
                write_json(self.state_file, state)
                for tab in [t for t in snapshot["tabs"] if t["workspace_id"] == workspace_id]:
                    try:
                        panel = self.ensure_sidebar(profile, tab["tab_id"], snapshot, state)
                        if panel:
                            panels.append(panel)
                    except (APIError, OSError, KeyError) as exc:
                        errors.append(tab["tab_id"] + ": " + str(exc))
            ordered = [chosen[p.name]["workspace_id"] for p in profiles]
            before = [w["workspace_id"] for w in workspaces]
            desired = [w for w in before if w not in used] + ordered
            if before != desired:
                self.api.call("workspace.move_block", {"workspace_ids": ordered})
            state.update(workspaces=chosen, synced_at=time.time())
            write_json(self.state_file, state)
            return {"profiles": len(profiles), "created": created, "sidebars_created": panels,
                    "workspaces": chosen, "errors": errors}

    def ensure_sidebar(self, profile, tab_id, snapshot=None, state=None):
        snapshot = snapshot or self.api.snapshot()
        panes = [p for p in snapshot["panes"] if p["tab_id"] == tab_id]
        state = state if state is not None else read_json(self.state_file)
        for existing in [p for p in panes if manager_pane(p)]:
            info = self.api.call("pane.process_info", {"pane_id": existing["pane_id"]})["process_info"]
            processes = info.get("foreground_processes", [])
            if any(self.sidebar_process(p) for p in processes):
                self.arrange_sidebar(existing["pane_id"],tab_id)
                return None
            recent = state.get("launch_attempts", {}).get(tab_id, [])
            if recent and time.time()-recent[-1] < 3:
                return None
            # Normal plugin terminal commands are not resumed by Herdr 0.9.3. Only
            # replace our own empty/shell-only restored pane, never another running app.
            if not existing.get("agent") and all(self.shell_process(p) for p in processes):
                self.api.call("pane.close", {"pane_id": existing["pane_id"]})
            else:
                # A user has started something else in this pane; retain it and its process.
                self.api.call("pane.rename", {"pane_id": existing["pane_id"], "label": "Terminal"})
                self.api.call("pane.report_metadata", {"pane_id": existing["pane_id"],
                    "source": self.settings.plugin_id, "tokens": {PANE_TOKEN: None}})
        if not panes:
            return None
        attempts = state.setdefault("launch_attempts", {})
        recent = [t for t in attempts.get(tab_id, []) if time.time() - t < 120]
        if len(recent) >= 3:
            raise APIError("sidebar_backoff", "Sidebar could not stay open; inspect the plugin log")
        attempts[tab_id] = recent + [time.time()]
        write_json(self.state_file, state)
        layout = next((l for l in snapshot.get("layouts", []) if l["tab_id"] == tab_id), None)
        if not layout:
            layout = self.api.call("pane.layout", {"pane_id": panes[0]["pane_id"]})["layout"]
        rects = {p["pane_id"]: p["rect"] for p in layout["panes"]}
        candidates = [p for p in panes if not manager_pane(p)]
        # Split a wide content terminal, then swap into the far-right full-height slot.
        # Other plugins' sidebars and all running processes survive the arrangement.
        target = max(candidates, key=lambda p: rects.get(p["pane_id"], {}).get("width", 0))
        result = self.api.call("plugin.pane.open", {
            "plugin_id": self.settings.plugin_id, "entrypoint": "manager", "placement": "split",
            "target_pane_id": target["pane_id"],
            "direction": "right", "focus": False,
            "env": {"HERDR_HERMES_PROFILE": profile.name, "HERDR_HERMES_ROOT": str(self.settings.root)},
        })
        opened = result["plugin_pane"]
        pane_id = opened.get("pane_id") or opened.get("pane", {}).get("pane_id")
        if not pane_id:
            raise APIError("missing_pane", "Herdr did not return the manager pane id")
        self.api.call("pane.rename", {"pane_id": pane_id, "label": "Hermes Manager"})
        self.api.call("pane.report_metadata", {
            "pane_id": pane_id, "source": self.settings.plugin_id,
            "tokens": {PANE_TOKEN: profile.name},
        })
        self.arrange_sidebar(pane_id, tab_id)
        return pane_id

    @staticmethod
    def sidebar_process(process):
        argv=process.get("argv") or process.get("cmdline") or []
        text=" ".join(argv) if isinstance(argv,list) else str(argv)
        return "hermes_manager.py" in text and "sidebar" in text

    @staticmethod
    def shell_process(process):
        name=Path(process.get("name") or "").name.lstrip("-")
        argv=process.get("argv") or []
        if not name and isinstance(argv,list) and argv:
            name=Path(argv[0]).name.lstrip("-")
        return name in ("zsh","bash","sh","fish","nu")

    def arrange_sidebar(self, pane_id, tab_id):
        layout = self.api.call("pane.layout", {"pane_id": pane_id})["layout"]
        area = layout["area"]
        full_height = [p for p in layout["panes"] if p["rect"]["height"] >= area["height"] - 1]
        if full_height:
            rightmost = max(full_height, key=lambda p: p["rect"]["x"])
            if rightmost["pane_id"] != pane_id:
                self.api.call("pane.swap", {"source_pane_id": pane_id, "target_pane_id": rightmost["pane_id"]})
        exported = self.api.call("layout.export", {"tab_id": tab_id})
        tree = exported.get("root") or exported.get("layout", {}).get("root")
        if not tree:
            raise APIError("missing_layout", "Herdr did not return the tab layout")
        found = pane_path(tree, pane_id)
        if not found or not found[0]:
            return
        path, _ = found
        parent_path = path[:-1]
        parent = node_at(tree, parent_path)
        if parent.get("direction") != "right":
            return
        # Calculate the actual parent width without changing unrelated split ratios.
        parent_width = float(area["width"])
        cursor = tree
        for branch in parent_path:
            if cursor.get("direction") == "right":
                parent_width *= cursor["ratio"] if branch == 0 else 1 - cursor["ratio"]
            cursor = cursor["first" if branch == 0 else "second"]
        desired = min(self.settings.sidebar_width, max(26, area["width"] * 0.35))
        share = max(0.1, min(0.8, desired / max(1, parent_width)))
        ratio = share if path[-1] == 0 else 1 - share
        self.api.call("layout.set_split_ratio", {"tab_id": tab_id, "path": [bool(branch) for branch in parent_path], "ratio": ratio})

    def resolve_workspace(self, profile):
        snapshot = self.api.snapshot()
        workspace = next((w for w in snapshot["workspaces"] if workspace_profile(w, snapshot) == profile.name), None)
        if not workspace:
            self.sync(force=True)
            snapshot = self.api.snapshot()
            workspace = next((w for w in snapshot["workspaces"] if workspace_profile(w, snapshot) == profile.name), None)
        if not workspace:
            raise ValueError("No Herdr workspace for this Hermes profile")
        return workspace

    def open_chat(self, profile, session_id=None, focus=True):
        cwd = profile.cwd
        title = "New conversation"
        argv = [str(self.settings.hermes_bin), "-p", profile.name]
        if session_id:
            conversation = get_conversation(profile, session_id)
            saved_cwd = Path(conversation.get("cwd") or str(cwd)).expanduser()
            if saved_cwd.is_dir():
                cwd = saved_cwd
            title = clean_text(conversation.get("title") or "Conversation", 60)
            argv.extend(["--resume", session_id])
        workspace = self.resolve_workspace(profile)
        result = self.api.call("tab.create", {
            "workspace_id": workspace["workspace_id"], "cwd": str(cwd), "label": title,
            "focus": False, "env": {"HERDR_HERMES_PROFILE": profile.name},
        })
        pane_id = result["root_pane"]["pane_id"]
        # This submits only the launch command, never a prompt or approval answer.
        self.api.call("pane.send_input", {"pane_id": pane_id, "text": shlex.join(argv), "keys": ["enter"]})
        self.ensure_sidebar(profile, result["tab"]["tab_id"])
        if focus:
            self.api.call("pane.focus", {"pane_id": pane_id})
        return {"profile": profile.name, "tab_id": result["tab"]["tab_id"], "pane_id": pane_id}

    def cron_command(self, profile, action, job_id, **fields):
        if action not in ("pause", "resume", "edit"):
            raise ValueError("Unsupported cron operation")
        job = next((j for j in load_jobs(profile) if j["id"] == job_id), None)
        if not job:
            raise ValueError("Cron job no longer exists in this profile")
        if action == "resume" and job.get("state") == "completed":
            raise ValueError("This job has completed; create a new schedule to run it again")
        argv = [str(self.settings.hermes_bin), "-p", profile.name, "cron", action, job_id]
        for key, value in fields.items():
            if key not in ("name", "schedule"):
                raise ValueError("Unsupported cron field")
            argv.extend(["--" + key, str(value)])
        env = dict(os.environ)
        # Named selection is resolved from the fleet root, not a caller's inherited profile.
        env["HERMES_HOME"] = str(self.settings.root)
        env.pop("HERMES_SESSION_KEY", None)
        env.pop("HERDR_AGENT", None)
        result = subprocess.run(argv, env=env, cwd=str(profile.cwd), capture_output=True,
                                text=True, timeout=30)
        if result.returncode:
            raise RuntimeError(clean_text(result.stderr or result.stdout, 600) or "Hermes cron command failed")
        updated = next((j for j in load_jobs(profile) if j["id"] == job_id), None)
        if action == "pause" and updated and job_enabled(updated):
            raise RuntimeError("Hermes returned without pausing this job")
        if action == "resume" and updated and not job_enabled(updated):
            raise RuntimeError("Hermes returned without enabling this job")
        return clean_text(result.stdout, 600)

    def focus_sidebar(self):
        profile = current_profile(self.api, self.settings)
        if not profile:
            raise ValueError("Select a Hermes profile workspace first")
        context = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
        tab_id = context.get("tab_id") or os.environ.get("HERDR_TAB_ID")
        self.sync(force=True)
        snapshot = self.api.snapshot()
        pane = next((p for p in snapshot["panes"] if p["tab_id"] == tab_id and manager_pane(p)), None)
        if pane:
            self.api.call("pane.focus", {"pane_id": pane["pane_id"]})
        return {"profile": profile.name, "pane_id": pane["pane_id"] if pane else None}

    def refresh_sidebars(self):
        """Load local code/config changes without restarting any Hermes conversation."""
        with sync_lock(self.settings,self.api,wait=True):
            snapshot=self.api.snapshot()
            state=read_json(self.state_file)
            refreshed=[]
            focused_tab=None
            for pane in [p for p in snapshot["panes"] if manager_pane(p)]:
                info=self.api.call("pane.process_info",{"pane_id":pane["pane_id"]})["process_info"]
                if not any(self.sidebar_process(p) for p in info.get("foreground_processes",[])):
                    continue
                if pane.get("focused"):
                    focused_tab=pane["tab_id"]
                self.api.call("pane.close",{"pane_id":pane["pane_id"]})
                state.setdefault("launch_attempts",{}).pop(pane["tab_id"],None)
                refreshed.append(pane["pane_id"])
            state["synced_at"]=0
            write_json(self.state_file,state)
        result=self.sync(force=True)
        if focused_tab:
            replacement=next((p for p in self.api.snapshot()["panes"] if p["tab_id"]==focused_tab and manager_pane(p)),None)
            if replacement:
                self.api.call("pane.focus",{"pane_id":replacement["pane_id"]})
        result["sidebars_refreshed"]=len(refreshed)
        return result
