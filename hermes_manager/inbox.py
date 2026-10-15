"""Cron inbox: every saved cron run across all Hermes profiles, with read/unread state.

Cron output is always written to <profile>/cron/output/<job_id>/<timestamp>.md, whether or not
the chat delivery worked. The inbox reads those files, so failed or local-only deliveries still
show up. Runs with no output ("silent") are kept so every run is accounted for, but never count as unread.
"""
from __future__ import annotations

import curses
import fcntl
import json
import os
import re
import signal
import sqlite3
import textwrap
import time
import unicodedata
from datetime import datetime
from pathlib import Path

from .core import clean_text, read_json, write_json, readonly_db

INBOX_DAYS = 14
INBOX_LIMIT = 150
FILE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})\.md\Z")
SILENT_RE = re.compile(r"^\s*\[?\s*(SILENT|NO[_ ]REPLY)\s*\]?\s*$", re.I)
_PARSE_CACHE = {}


def run_time(name, fallback):
    match = FILE_RE.fullmatch(name)
    if not match:
        return fallback
    try:
        return datetime(*map(int, match.groups())).timestamp()
    except ValueError:
        return fallback


def parse_run(text):
    """Split a cron output file into header fields and the user-facing body."""
    header, rest = {}, text
    head, _, remainder = text.partition("\n\n## ")
    for line in head.splitlines():
        match = re.match(r"\*\*(.+?):\*\*\s*(.*)", line)
        if match:
            header[match[1].strip().lower()] = match[2].strip()
        elif line.startswith("# Cron Job:"):
            header["name"] = line.split(":", 1)[1].strip()
    body, error = "", ""
    marker = "\n## Response\n"
    index = text.rfind(marker)
    if index >= 0:
        body = text[index + len(marker):].strip()
    elif "\n## Prompt\n" in text or text.startswith("# Cron Job:") and remainder.startswith("Prompt"):
        # Agent run without a response; any trailing error section is the useful part.
        body = ""
    else:
        body = head.split("\n", 1)[1].strip() if "\n" in head else ""
        # Drop header lines from script output.
        body = "\n".join(l for l in body.splitlines() if not re.match(r"\*\*.+?:\*\*", l)).strip()
        if remainder:
            body = (body + "\n\n## " + remainder).strip()
    match = re.search(r"\n## Error\n+(.*?)(?=\n## |\Z)", text, re.S)
    if match:
        error = match[1].strip().strip("`").strip()
    return header, body, error


def parse_file(path, mtime):
    cached = _PARSE_CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    text = path.read_text(errors="replace")
    header, body, error = parse_run(text)
    status = header.get("status", "")
    silent = "silent" in status.lower() or bool(SILENT_RE.match(body)) and not error
    failed = bool(error) or "fail" in status.lower()
    result = {"header": header, "body": body, "error": error, "silent": silent, "failed": failed, "status": status}
    if len(_PARSE_CACHE) > 2000:
        _PARSE_CACHE.clear()
    _PARSE_CACHE[path] = (mtime, result)
    return result


def preview(parsed):
    if parsed["silent"] and not parsed["error"]:
        return "Ran · nothing to report"
    source = parsed["error"] or parsed["body"] or parsed["status"] or "(no output)"
    for line in source.splitlines():
        cleaned = clean_text(re.sub(r"[*_`#>]+", "", line), 160).strip()
        if cleaned:
            return cleaned
    return "(no output)"


def jobs_by_id(profile):
    data = read_json(profile.home / "cron/jobs.json", [])
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    return {j["id"]: j for j in jobs if isinstance(j, dict) and j.get("id")}


def make_item(profile, job, job_id, path, stat, latest):
    parsed = parse_file(path, stat.st_mtime)
    delivery_failed = bool(latest and job.get("last_status") == "delivery_failed")
    return {
        "id": profile.name + "/" + job_id + "/" + path.name,
        "profile": profile.name, "profile_title": profile.title, "job_id": job_id,
        "job_name": clean_text(parsed["header"].get("name") or job.get("name") or job_id, 120),
        "path": str(path), "run_at": run_time(path.name, stat.st_mtime),
        "silent": parsed["silent"], "failed": parsed["failed"] or (latest and job.get("last_status") == "error"),
        "delivery_failed": delivery_failed,
        "delivery_error": clean_text(job.get("last_delivery_error"), 400) if delivery_failed else "",
        "deliver": clean_text(job.get("deliver") or "origin", 80),
        "preview": preview(parsed), "title": clean_text(parsed["header"].get("name") or job.get("name") or job_id, 120),
    }


def state_file(settings):
    return settings.state_dir / "inbox-state.json"


def load_state(settings):
    state = read_json(state_file(settings), {})
    if not isinstance(state, dict) or "baseline" not in state:
        # First use: show the last 24h as unread, not the whole archive.
        state = {"baseline": time.time() - 86400, "read": {}}
        write_json(state_file(settings), state)
    state.setdefault("read", {})
    return state


def update_state(settings, change):
    settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (settings.state_dir / "inbox-state.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_state(settings)
        change(state)
        write_json(state_file(settings), state)
    return state


def mark_read(settings, keys, read=True):
    def change(state):
        for key in keys:
            if read:
                state["read"][key] = time.time()
            else:
                state["read"].pop(key, None)
    return update_state(settings, change)


def mark_all_read(settings, items):
    return mark_read(settings, [i["id"] for i in items if i.get("unread")])


def load_inbox(settings, only=None, days=INBOX_DAYS, limit=INBOX_LIMIT, include_silent=True):
    """Runs newest first. ``only`` (a profile name) restricts the list to that one profile."""
    state = load_state(settings)
    read, baseline = state["read"], float(state.get("baseline", 0))
    cutoff, items = time.time() - days * 86400, []
    for profile in (p for p in settings.profiles() if only is None or p.name == only):
        output = profile.home / "cron/output"
        if not output.is_dir():
            continue
        jobs = jobs_by_id(profile)
        for directory in output.iterdir():
            if not directory.is_dir():
                continue
            files = []
            for path in directory.glob("*.md"):
                try:
                    files.append((path, path.stat()))
                except OSError:
                    continue
            files.sort(key=lambda f: f[0].name, reverse=True)
            job = jobs.get(directory.name, {})
            for index, (path, stat) in enumerate(files):
                if run_time(path.name, stat.st_mtime) < cutoff:
                    break
                try:
                    item = make_item(profile, job, directory.name, path, stat, index == 0)
                except OSError:
                    continue
                if item["silent"] and not item["delivery_failed"] and not include_silent:
                    continue
                item["unread"] = (not item["silent"] or item["delivery_failed"]) and \
                    item["id"] not in read and item["run_at"] >= baseline
                items.append(item)
    items.sort(key=lambda i: i["run_at"], reverse=True)
    return items[:limit]


def item_for_key(settings, key):
    parts = key.split("/")
    if len(parts) != 3:
        raise ValueError("Invalid inbox item")
    profile = settings.profile(parts[0])
    path = (profile.home / "cron/output" / parts[1] / parts[2])
    if not path.is_file() or path.parent.parent != profile.home / "cron/output":
        raise ValueError("Cron output no longer exists")
    jobs = jobs_by_id(profile)
    stat = path.stat()
    item = make_item(profile, jobs.get(parts[1], {}), parts[1], path, stat, False)
    item["unread"] = key not in load_state(settings)["read"]
    return item


def unread_summary(items):
    unread = [i for i in items if i.get("unread")]
    return len(unread), sum(1 for i in unread if i["failed"] or i["delivery_failed"])


def find_session(profile, job_id, at):
    """The cron agent session for a run: the newest one for the job that started before the output was written."""
    if not (profile.home / "state.db").is_file():
        return None
    like = "cron_" + job_id + "_%"
    with readonly_db(profile) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT id, started_at FROM sessions WHERE id LIKE ? AND started_at<=? "
                                 "ORDER BY started_at DESC LIMIT 1", (like, at + 15)).fetchone()
        if row is None or at - row["started_at"] > 6 * 3600:
            return None
        return row["id"]


def message_content(value):
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return value
        if isinstance(parsed, list) and all(isinstance(p, dict) for p in parsed):
            return "\n".join(str(p.get("text") or p.get("content") or "[attachment]") for p in parsed)
        return value
    return str(value or "")


def tool_summary(raw):
    try:
        calls = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return ""
    names = []
    for call in calls or []:
        function = call.get("function", call) if isinstance(call, dict) else {}
        names.append(str(function.get("name") or "tool") + "(" + clean_text(function.get("arguments"), 120) + ")")
    return "\n".join(names)


def load_conversation(settings, item):
    """List of (label, text) for the cron run's agent session, or (None, reason)."""
    profile = settings.profile(item["profile"])
    session_id = find_session(profile, item["job_id"], item["run_at"])
    if not session_id:
        return None, "No agent conversation for this run (script-only job, or the session was removed)."
    with readonly_db(profile) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT role, content, tool_calls, tool_name FROM messages WHERE session_id=? "
                                  "ORDER BY timestamp, id", (session_id,)).fetchall()
    messages = []
    for row in rows:
        text = message_content(row["content"]).strip()
        if row["role"] == "user" and len(text) > 700:
            text = text[:700].rstrip() + "\n… [" + str(len(text) - 700) + " more characters of prompt hidden]"
        elif row["role"] == "tool":
            text = (text[:600].rstrip() + " …") if len(text) > 600 else text
        calls = tool_summary(row["tool_calls"]) if row["tool_calls"] else ""
        if row["role"] == "assistant" and calls:
            text = (text + "\n" if text else "") + "→ " + calls.replace("\n", "\n→ ")
        if not text:
            continue
        label = {"user": "PROMPT", "assistant": "AGENT", "tool": "TOOL" + (" " + row["tool_name"] if row["tool_name"] else "")}
        messages.append((label.get(row["role"], row["role"].upper()), text))
    return session_id, messages


def cell_width(char):
    return 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def wrap_text(text, width):
    lines = []
    for raw in text.replace("\t", "    ").splitlines() or [""]:
        raw = "".join(c for c in raw if not unicodedata.category(c).startswith("C"))
        if not raw.strip():
            lines.append("")
            continue
        indent = re.match(r"\s*(?:[-*•]\s+|\d+[.)]\s+)?", raw)[0]
        lines.extend(textwrap.wrap(raw, max(10, width), subsequent_indent=" " * min(len(indent), 8),
                                   drop_whitespace=False, break_long_words=True) or [""])
    return lines


def meta_lines(item):
    when = datetime.fromtimestamp(item["run_at"]).strftime("%a %d %b %H:%M")
    state = "FAILED" if item["failed"] else "quiet (no output)" if item["silent"] else "ok"
    lines = [item["profile_title"] + "  ·  " + when + "  ·  run " + state + "  ·  deliver: " + item["deliver"]]
    if item["delivery_failed"]:
        lines.append("DELIVERY FAILED: " + (item["delivery_error"] or "unknown error"))
    return lines


def output_lines(item, width):
    parsed = parse_file(Path(item["path"]), os.stat(item["path"]).st_mtime)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", parsed["body"] or "(this run produced no message)")
    if parsed["error"]:
        text = "ERROR\n" + parsed["error"] + "\n\n" + (parsed["body"] or "")
    return wrap_text(text.strip(), width)


def conversation_lines(settings, item, width):
    session_id, messages = load_conversation(settings, item)
    if session_id is None:
        return wrap_text(messages, width)
    lines = ["session " + session_id, ""]
    for label, text in messages:
        lines.append("── " + label + " " + "─" * max(0, min(width, 60) - len(label) - 4))
        lines.extend(wrap_text(text, width))
        lines.append("")
    return lines


def open_inbox(settings, api, key):
    return api.call("plugin.pane.open", {"plugin_id": settings.plugin_id, "entrypoint": "inbox-popup",
                                         "placement": "popup", "width": "90%", "height": "90%", "focus": True,
                                         "cwd": str(Path(__file__).parent.parent),
                                         "env": {"HERDR_HERMES_INBOX_RUN": key}})


class InboxPopup:
    def __init__(self, settings, key):
        self.settings = settings
        # Only the runs of the profile this notification belongs to.
        self.items = load_inbox(settings, key.split("/", 1)[0], include_silent=False)
        found = [i for i, item in enumerate(self.items) if item["id"] == key]
        if not found:
            self.items.insert(0, item_for_key(settings, key))
        self.index = found[0] if found else 0
        self.tab, self.scroll, self.lines, self.layout_key = "output", 0, [], None
        self.status = ""
        self.running = True
        self.chat = None
        self.returned = False

    @property
    def item(self):
        return self.items[self.index]

    def visit(self, index):
        self.index = max(0, min(len(self.items) - 1, index))
        self.scroll, self.layout_key = 0, None
        mark_read(self.settings, [self.item["id"]])
        self.item["unread"] = False

    def build(self, width):
        key = (self.item["id"], self.tab, width)
        if key == self.layout_key:
            return
        try:
            if self.tab == "output":
                self.lines = output_lines(self.item, width)
            else:
                self.lines = conversation_lines(self.settings, self.item, width)
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.lines = wrap_text("Could not load: " + str(exc), width)
        self.layout_key = key

    def put(self, win, y, x, text, attr=0):
        height, width = win.getmaxyx()
        if y < 0 or y >= height - 0 or x >= width - 1:
            return
        out, cells = "", 0
        for char in text:
            size = cell_width(char)
            if cells + size > width - x - 1:
                break
            out += char
            cells += size
        try:
            win.addstr(y, x, out, attr)
        except curses.error:
            pass

    def draw(self, win):
        win.erase()
        height, width = win.getmaxyx()
        item = self.item
        bad = item["failed"] or item["delivery_failed"]
        position = str(self.index + 1) + "/" + str(len(self.items))
        self.put(win, 0, 1, ("● " if item["unread"] else "") + item["title"], curses.A_BOLD | curses.color_pair(4 if bad else 1))
        self.put(win, 0, max(1, width - len(position) - 2), position, curses.A_DIM)
        for offset, line in enumerate(meta_lines(item)):
            self.put(win, 1 + offset, 1, line, curses.color_pair(4) if line.startswith("DELIVERY") else curses.A_DIM)
        top = 1 + len(meta_lines(item))
        tabs = [("output", "Notification"), ("conversation", "Cron conversation")]
        x = 1
        for name, label in tabs:
            attr = curses.A_REVERSE | curses.A_BOLD if self.tab == name else curses.A_DIM
            self.put(win, top, x, " " + label + " ", attr)
            x += len(label) + 3
        self.put(win, top + 1, 1, "─" * max(0, width - 3), curses.A_DIM)
        body_top, body_height = top + 2, height - top - 5
        self.build(width - 4)
        self.scroll = max(0, min(self.scroll, max(0, len(self.lines) - body_height)))
        for row, line in enumerate(self.lines[self.scroll:self.scroll + body_height]):
            heading = line.startswith("── ") or line.startswith("ERROR")
            self.put(win, body_top + row, 2, line, curses.color_pair(3) | curses.A_BOLD if heading else 0)
        if len(self.lines) > body_height:
            end = min(len(self.lines), self.scroll + body_height)
            self.put(win, body_top + body_height, max(1, width - 22), str(self.scroll + 1) + "-" + str(end) + " / " + str(len(self.lines)), curses.A_DIM)
        self.put(win, height - 3, 1, "─" * max(0, width - 3), curses.A_DIM)
        self.put(win, height - 2, 1, "j/k scroll  n/p next/prev  u unread  Tab conversation  r reply  m mark unread  Esc close"
                 if width > 90 else "j/k scroll n/p item u unread Tab tab r reply Esc", curses.A_DIM)
        if self.status:
            self.put(win, height - 1, 1, self.status, curses.A_DIM)
        win.refresh()

    def start_reply(self):
        """Continue this run's agent session in place; the chat takes over the popup until Esc."""
        profile = self.settings.profile(self.item["profile"])
        session = find_session(profile, self.item["job_id"], self.item["run_at"])
        if not session:
            self.tab, self.scroll = "conversation", 0
            self.status = "Nothing to reply to: this run has no agent conversation (script-only job)."
            return
        self.chat = (profile, session, self.item["title"])
        self.running = False

    def next_unread(self):
        for step in range(1, len(self.items) + 1):
            index = (self.index + step) % len(self.items)
            if self.items[index].get("unread"):
                return self.visit(index)
        self.status = "No unread notifications"

    def handle(self, win, key):
        height = win.getmaxyx()[0]
        page = max(3, height - 8)
        self.status = ""
        if key == 27 and self.sgr_mouse(win):
            return
        if key in (27, ord("q")):
            self.running = False
        elif key == curses.KEY_MOUSE:
            try:
                button = curses.getmouse()[4]
            except curses.error:
                return
            if button & curses.BUTTON4_PRESSED:
                self.scroll -= 3
            elif button & getattr(curses, "BUTTON5_PRESSED", 0):
                self.scroll += 3
        elif key in (curses.KEY_DOWN, ord("j")):
            self.scroll += 1
        elif key in (curses.KEY_UP, ord("k")):
            self.scroll -= 1
        elif key in (curses.KEY_NPAGE, ord(" ")):
            self.scroll += page
        elif key == curses.KEY_PPAGE:
            self.scroll -= page
        elif key in (curses.KEY_HOME, ord("g")):
            self.scroll = 0
        elif key in (curses.KEY_END, ord("G")):
            self.scroll = 10 ** 6
        elif key in (ord("n"), curses.KEY_RIGHT):
            self.visit(self.index + 1)
        elif key in (ord("p"), curses.KEY_LEFT):
            self.visit(self.index - 1)
        elif key == ord("u"):
            self.next_unread()
        elif key in (ord("r"), ord("\n"), ord("\r"), curses.KEY_ENTER):
            self.start_reply()
        elif key in (ord("\t"), ord("c")):
            self.tab, self.scroll = ("conversation" if self.tab == "output" else "output"), 0
        elif key == ord("m"):
            mark_read(self.settings, [self.item["id"]], read=False)
            self.item["unread"] = True
            self.status = "Marked unread"

    def sgr_mouse(self, win):
        win.timeout(20)
        sequence = ""
        try:
            while len(sequence) < 60:
                value = win.getch()
                if value == -1:
                    break
                if value > 255:
                    curses.ungetch(value)
                    break
                sequence += chr(value)
                if sequence[-1] in "Mm":
                    break
        finally:
            win.timeout(200)
        if not sequence:
            return False
        match = re.fullmatch(r"\[<(\d+);(\d+);(\d+)([Mm])", sequence)
        if match and match[4] == "M" and int(match[1]) & 64:
            self.scroll += -3 if int(match[1]) % 2 == 0 else 3
        return True

    def loop(self, win):
        # ncurses waits 1000ms after Esc by default; Esc must close at once.
        curses.set_escdelay(25)
        curses.curs_set(0)
        win.keypad(True)
        win.timeout(200)
        curses.start_color()
        curses.use_default_colors()
        for pair, color in ((1, curses.COLOR_CYAN), (3, curses.COLOR_YELLOW), (4, curses.COLOR_RED)):
            curses.init_pair(pair, color, -1)
        curses.mouseinterval(0)
        curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
        print("\033[?1000h\033[?1006h", end="", flush=True)
        try:
            self.visit(self.index)
            while self.running:
                self.draw(win)
                key = win.getch()
                if key != -1:
                    self.handle(win, key)
        finally:
            print("\033[?1000l\033[?1006l", end="", flush=True)

    def run(self):
        from .bots import BotPopup
        while True:
            self.run_viewer()
            if not self.chat:
                return
            profile, session, title = self.chat
            self.chat, self.running, self.layout_key = None, True, None
            BotPopup(self.settings, profile, session=session, label=title).run()
            # The conversation changed: rebuild the transcript when we come back.
            self.tab = "conversation"
            self.scroll = 10 ** 6

    def run_viewer(self):
        # Native popup dismissal can hang up the PTY rather than send Esc.
        def dismiss(signum, frame):
            raise SystemExit(0)
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGHUP, signal.SIGTERM)}
        for sig in previous:
            signal.signal(sig, dismiss)
        try:
            curses.wrapper(self.loop)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
