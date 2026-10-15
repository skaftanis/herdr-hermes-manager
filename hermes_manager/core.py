from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import shutil
import sqlite3
import unicodedata
import uuid

PLUGIN_ID = "hhl.hermes-manager"
PROFILE_TOKEN = "hermes-profile"
DIVIDER_TOKEN = "hermes-divider"
DIVIDER_TEXT = "──── HERMES ────"
PANE_TOKEN = "hermes-manager"
PROFILE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
PROFILE_MARKERS = ("config.yaml", ".env", "SOUL.md", "profile.yaml", "auth.json", "state.db")
BACKGROUND_SOURCES = ("cron", "subagent", "kanban", "oneshot", "tool")


def clean_text(value, limit=500):
    text = str(value or "")
    return " ".join("".join(c if not unicodedata.category(c).startswith("C") else " "
                            for c in text).split())[:limit]


def read_json(path, fallback=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {} if fallback is None else fallback


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
        temp.chmod(0o600)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def yaml_display_name(path):
    """Only the public, top-level display name is needed; never read credentials."""
    try:
        text = Path(path).read_text()
    except OSError:
        return ""
    match = re.search(r"^display_name:\s*(.*?)\s*$", text, re.M)
    if not match:
        return ""
    value = match.group(1)
    if value in ("", "null", "~", "|", ">"):
        return ""
    if value.startswith('"'):
        try:
            value = json.loads(value)
        except ValueError:
            return ""
    elif value.startswith("'") and value.endswith("'"):
        value = value[1:-1].replace("''", "'")
    else:
        value = value.split(" #", 1)[0]
    return clean_text(value, 60)


@dataclass(frozen=True)
class Profile:
    name: str
    home: Path
    title: str
    cwd: Path


class Settings:
    def __init__(self, root=None, config_dir=None, state_dir=None):
        self.plugin_id = os.environ.get("HERDR_PLUGIN_ID", PLUGIN_ID)
        host_config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home()/".config") / "herdr"
        self.host_config = host_config
        host_state = Path(os.environ.get("XDG_STATE_HOME") or Path.home()/".local/state") / "herdr"
        self.config_dir = Path(config_dir or os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or
                               host_config / "plugins/config" / self.plugin_id)
        self.state_dir = Path(state_dir or os.environ.get("HERDR_PLUGIN_STATE_DIR") or
                              host_state / "plugins" / self.plugin_id)
        self.config = read_json(self.config_dir / "config.json")
        self.root = Path(root or os.environ.get("HERDR_HERMES_ROOT") or
                         self.config.get("hermes_root") or Path.home() / ".hermes").expanduser().resolve()
        self.sidebar_width = max(30, min(80, int(self.config.get("sidebar_width", 46))))
        self.conversation_limit = max(10, min(200, int(self.config.get("conversation_limit", 50))))
        self.hermes_bin = Path(self.config.get("hermes_bin") or shutil.which("hermes") or Path.home() / ".local/bin/hermes").expanduser()
        self.agent_root = Path(self.config.get("agent_root") or self.root / "hermes-agent").expanduser().resolve()

    def profiles(self):
        homes = [("default", self.root)] if self.root.is_dir() and any((self.root / m).exists() for m in PROFILE_MARKERS) else []
        directory = self.root / "profiles"
        if directory.is_dir():
            for home in sorted(directory.iterdir()):
                if (home.is_dir() and home.name != "default" and PROFILE_RE.fullmatch(home.name)
                        and not (directory / ".deleted" / home.name).exists()
                        and any((home / m).is_file() or (home / m).is_symlink() for m in PROFILE_MARKERS)):
                    homes.append((home.name, home))
        result = []
        for name, home in homes:
            title = (self.config.get("profile_labels", {}).get(name) or yaml_display_name(home / "profile.yaml")
                     or name.removeprefix("project-").replace("-", " ").title())
            cwd = self.config.get("profile_cwds", {}).get(name)
            cwd = Path(cwd or Path.home()).expanduser()
            result.append(Profile(name, home, clean_text(title, 60), cwd if cwd.is_dir() else Path.home()))
        return result

    def profile(self, name):
        for profile in self.profiles():
            if profile.name == name:
                return profile
        raise ValueError("Hermes profile no longer exists: " + str(name))


class APIError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(str(message))


class Herdr:
    def __init__(self, socket_path=None, timeout=5):
        self.socket_path = str(socket_path or os.environ.get("HERDR_SOCKET_PATH") or
                               Path(os.environ.get("XDG_CONFIG_HOME") or Path.home()/".config") / "herdr/herdr.sock")
        self.timeout = timeout

    def call(self, method, params=None):
        request_id = "hermes-manager:" + uuid.uuid4().hex
        request = {"id": request_id, "method": method, "params": params or {}}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout)
            connection.connect(self.socket_path)
            connection.sendall((json.dumps(request) + "\n").encode())
            with connection.makefile("rb") as stream:
                while True:
                    line = stream.readline(16 * 1024 * 1024)
                    if not line:
                        raise APIError("disconnected", "Herdr closed the connection")
                    response = json.loads(line)
                    if response.get("id") != request_id:
                        continue
                    if "error" in response:
                        error = response["error"]
                        raise APIError(error.get("code"), error.get("message", error))
                    return response["result"]

    def snapshot(self):
        return self.call("session.snapshot")["snapshot"]

    def state_key(self):
        return hashlib.sha256(self.socket_path.encode()).hexdigest()[:16]


def load_jobs(profile):
    data = read_json(profile.home / "cron/jobs.json", [])
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        raise ValueError("Invalid cron job list")
    return sorted((j for j in jobs if isinstance(j, dict) and j.get("id")),
                  key=lambda j: (j.get("state") == "completed", clean_text(j.get("name") or j["id"]).casefold()))


def job_enabled(job):
    return bool(job.get("enabled", True)) and job.get("state") not in ("paused", "completed")


def readonly_db(profile):
    return sqlite3.connect((profile.home / "state.db").resolve().as_uri() + "?mode=ro", uri=True, timeout=2)


def load_conversations(profile, limit=50, include_background=False):
    if not (profile.home / "state.db").is_file():
        return []
    with readonly_db(profile) as connection:
        connection.row_factory = sqlite3.Row
        columns = {r[1] for r in connection.execute("PRAGMA table_info(sessions)")}
        conditions = ["1=1"]
        for flag in ("hidden", "archived"):
            if flag in columns:
                conditions.append("COALESCE(s." + flag + ",0)=0")
        params = []
        if not include_background:
            conditions.append("s.source NOT IN (" + ",".join("?" for _ in BACKGROUND_SOURCES) + ")")
            params.extend(BACKGROUND_SOURCES)
            if "parent_session_id" in columns:
                conditions.append("s.parent_session_id IS NULL")
        has_messages = connection.execute("SELECT 1 FROM sqlite_master WHERE name='messages'").fetchone()
        recent = ["s.last_activity_at"] if "last_activity_at" in columns else []
        if has_messages:
            recent.append("(SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id=s.id)")
        recent.append("s.started_at")
        activity = "COALESCE(" + ",".join(recent) + ")" if len(recent) > 1 else recent[0]
        select = ["s.id", "s.source", "s.started_at", activity + " AS activity_at"]
        select.extend("s." + c for c in ("title", "cwd", "message_count") if c in columns)
        params.append(limit)
        rows = connection.execute("SELECT " + ",".join(select) + " FROM sessions s WHERE " +
                                  " AND ".join(conditions) + " ORDER BY activity_at DESC LIMIT ?", params).fetchall()
        results = []
        for raw in rows:
            row = dict(raw)
            title = clean_text(row.get("title"), 160)
            if not title and has_messages:
                first = connection.execute("SELECT content FROM messages WHERE session_id=? AND role='user' "
                                           "AND content IS NOT NULL ORDER BY timestamp LIMIT 1", (row["id"],)).fetchone()
                if first:
                    content = first[0]
                    try:
                        parts = json.loads(content)
                        if isinstance(parts, list):
                            content = " ".join(str(p.get("text", "")) for p in parts if isinstance(p, dict))
                    except (ValueError, TypeError):
                        pass
                    title = clean_text(content, 160)
            row["title"] = title or "Untitled conversation"
            results.append(row)
        return results


def get_conversation(profile, session_id):
    if not (profile.home / "state.db").is_file():
        raise ValueError("This profile has no saved conversations")
    with readonly_db(profile) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise ValueError("Conversation was not found in this profile")
        return dict(row)


def explicit_profile(argv):
    for i, arg in enumerate(argv):
        if arg == "--":
            break
        if arg in ("-p", "--profile") and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--profile="):
            return arg.partition("=")[2]
        if arg.startswith("-p") and not arg.startswith("--") and len(arg) > 2:
            return arg[2:]
    return None


def is_qualified_resume(argv):
    return any(arg.startswith("herdr-profile:") or arg.startswith("--resume=herdr-profile:") for arg in argv)


def workspace_profile(workspace, snapshot=None):
    profile = workspace.get("tokens", {}).get(PROFILE_TOKEN)
    if profile and PROFILE_RE.fullmatch(profile):
        return profile
    # Adopt the existing (H) spaces using the restore integration's actual identity.
    if snapshot and workspace.get("label", "").startswith("(H) "):
        profiles = set()
        for pane in snapshot.get("panes", []):
            if pane.get("workspace_id") != workspace["workspace_id"]:
                continue
            value = (pane.get("agent_session") or {}).get("value", "")
            if value.startswith("herdr-profile:"):
                name = value.split(":", 2)[1]
                if PROFILE_RE.fullmatch(name):
                    profiles.add(name)
        if len(profiles) == 1:
            return profiles.pop()
    return None


def current_profile(api, settings, env=None):
    env = os.environ if env is None else env
    context = json.loads(env.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    workspace_id = context.get("workspace_id") or env.get("HERDR_WORKSPACE_ID")
    pane_id = env.get("HERDR_PANE_ID")
    if pane_id and not context.get("workspace_id"):
        try:
            workspace_id = api.call("pane.current", {"caller_pane_id": pane_id})["pane"]["workspace_id"]
        except (APIError, KeyError):
            pass
    if not workspace_id:
        return None
    workspace = api.call("workspace.get", {"workspace_id": workspace_id})["workspace"]
    name = workspace_profile(workspace)
    if not name:
        # State remains valid when display-only metadata is lost during restore.
        state = read_json(settings.state_dir / (api.state_key() + ".json"))
        name = next((p for p, info in state.get("workspaces", {}).items()
                     if info.get("workspace_id") == workspace_id), None)
    return settings.profile(name) if name else None
