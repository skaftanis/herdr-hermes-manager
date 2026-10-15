from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import curses
from datetime import datetime
import locale
import os
import re
import textwrap
import time
import unicodedata

from .core import clean_text, job_enabled, load_conversations, load_jobs, read_json
from .control import manager_pane
from .bots import open_bot


def relative_time(value):
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        seconds = time.time() - float(value)
        if seconds < -60:
            return "in " + duration(-seconds)
        if seconds < 60:
            return "just now"
        return duration(seconds) + " ago"
    except (ValueError, TypeError, OverflowError):
        return "--"


def duration(seconds):
    if seconds < 3600:
        return str(max(1, int(seconds / 60))) + "m"
    if seconds < 86400:
        return str(int(seconds / 3600)) + "h"
    return str(int(seconds / 86400)) + "d"


def schedule_text(job):
    return clean_text(job.get("schedule_display") or job.get("schedule") or "No schedule", 160)


class Sidebar:
    def __init__(self, manager, profile):
        self.manager, self.profile = manager, profile
        self.jobs, self.conversations, self.bots = [], [], []
        self.section = "jobs"
        self.selected = {"jobs": 0, "conversations": 0, "bots": 0}
        self.offset = {"jobs": 0, "conversations": 0, "bots": 0}
        self.bot_columns = 3
        self.include_background = False
        self.detail = None
        self.detail_offset = 0
        self.status = "Loading profile..."
        self.status_error = False
        self.hitboxes = []
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.refresh_future = self.action_future = self.sync_future = None
        self.last_refresh = 0
        self.last_sync = time.monotonic()
        self.running = True

    def run(self):
        locale.setlocale(locale.LC_ALL, "")
        try:
            curses.wrapper(self.loop)
        finally:
            self.executor.shutdown(wait=False)

    def refresh(self):
        if self.refresh_future is None:
            def load():
                return (load_jobs(self.profile), load_conversations(
                    self.profile, self.manager.settings.conversation_limit, self.include_background), self.load_bots())
            self.refresh_future = self.executor.submit(load)
            self.last_refresh = time.monotonic()

    def action(self, fn, success):
        if self.action_future is not None:
            return
        self.status, self.status_error = "Working...", False
        self.action_future = (self.executor.submit(fn), success)

    def poll(self):
        if self.refresh_future and self.refresh_future.done():
            try:
                # Keep selection on the same job/chat if new activity changes the ordering.
                chosen = {s: self.current(s).get("id") if self.current(s) else None
                          for s in ("jobs", "conversations", "bots")}
                self.jobs, self.conversations, self.bots = self.refresh_future.result()
                for section in chosen:
                    rows = self.rows(section)
                    self.selected[section] = next((i for i, row in enumerate(rows) if row.get("id") == chosen[section]),
                                                  min(self.selected[section], max(0, len(rows)-1)))
                if self.status == "Loading profile...":
                    self.status = "Ready"
            except Exception as exc:
                self.status, self.status_error = "Refresh failed: " + str(exc), True
            self.refresh_future = None
        if self.action_future and self.action_future[0].done():
            future, success = self.action_future
            try:
                future.result()
                self.status, self.status_error = success, False
                self.last_refresh = 0
            except Exception as exc:
                self.status, self.status_error = str(exc), True
            self.action_future = None
        if self.sync_future and self.sync_future.done():
            try:
                result = self.sync_future.result()
                if result.get("skipped") == "plugin disabled":
                    self.running = False
            except Exception:
                pass
            self.sync_future = None
        now = time.monotonic()
        if now - self.last_refresh >= 3:
            self.refresh()
        if now - self.last_sync >= 12 and self.sync_future is None:
            self.sync_future = self.executor.submit(self.manager.sync, periodic=True)
            self.last_sync = now

    def rows(self, section=None):
        return {"jobs": self.jobs, "conversations": self.conversations, "bots": self.bots}[section or self.section]

    def load_bots(self):
        profiles = self.manager.settings.profiles()
        roster = read_json(self.manager.settings.state_dir / 'bots-roster.json', [])
        saved = {b['id']: b for b in roster if isinstance(b, dict) and 'id' in b} if isinstance(roster, list) else {}
        return [saved.get(p.name, {'id': p.name, 'label': p.title, 'icon': '✦'}) for p in profiles]

    def open_bot(self):
        bot = self.current('bots')
        if bot:
            profile = self.manager.settings.profile(bot['id'])
            self.action(lambda: open_bot(self.manager.settings, self.manager.api, profile),
                        'Opened Bot Chat: ' + bot['label'])

    def current(self, section=None):
        section = section or self.section
        rows = self.rows(section)
        return rows[self.selected[section]] if rows and self.selected[section] < len(rows) else None

    def toggle(self):
        job = self.current("jobs")
        if not job:
            return
        if job.get("state") == "completed":
            self.status = "Completed jobs cannot be enabled again"
            return
        action = "pause" if job_enabled(job) else "resume"
        self.action(lambda: self.manager.cron_command(self.profile, action, job["id"]),
                    "Job paused" if action == "pause" else "Job enabled")

    def open_conversation(self):
        row = self.current("conversations")
        if row:
            session_id = row["id"]
            self.action(lambda: self.manager.open_chat(self.profile, session_id), "Conversation opened in a new tab")

    def return_to_terminal(self):
        current = os.environ.get("HERDR_PANE_ID")
        tab = os.environ.get("HERDR_TAB_ID")
        try:
            panes = self.manager.api.call("pane.list", {"workspace_id": os.environ.get("HERDR_WORKSPACE_ID")})["panes"]
            candidate = next((p for p in panes if p["tab_id"] == tab and p["pane_id"] != current
                              and p.get("agent") and not manager_pane(p)), None)
            candidate = candidate or next((p for p in panes if p["tab_id"] == tab
                                           and p["pane_id"] != current and not manager_pane(p)), None)
            if candidate:
                self.manager.api.call("pane.focus", {"pane_id": candidate["pane_id"]})
        except Exception as exc:
            self.status, self.status_error = str(exc), True

    def loop(self, screen):
        self.screen = screen
        curses.curs_set(0)
        screen.keypad(True)
        screen.timeout(150)
        curses.start_color()
        curses.use_default_colors()
        for pair, color in ((1, curses.COLOR_CYAN), (2, curses.COLOR_GREEN),
                            (3, curses.COLOR_YELLOW), (4, curses.COLOR_RED), (5, curses.COLOR_BLUE)):
            curses.init_pair(pair, color, -1)
        curses.mouseinterval(0)
        curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
        # Tell Herdr this terminal consumes mouse input, including clicks and wheel events.
        print("\033[?1000h\033[?1006h", end="", flush=True)
        try:
            while self.running:
                self.poll()
                self.draw()
                key = screen.getch()
                if key != -1:
                    self.handle(key)
        finally:
            print("\033[?1000l\033[?1006l", end="", flush=True)

    def put(self, y, x, text, attr=0, width=None):
        height, cols = self.screen.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= cols:
            return
        width = min(cols - x - 1, width if width is not None else cols - x - 1)
        if width <= 0:
            return
        text = clean_text(text, 10000)
        # addnstr clips bytes rather than terminal cells for some curses builds.
        clipped, cells = "", 0
        for char in text:
            size = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
            if cells + size > width:
                break
            clipped += char
            cells += size
        try:
            self.screen.addstr(y, x, clipped, attr)
        except curses.error:
            pass

    def line(self, y):
        self.put(y, 1, "─" * max(0, self.screen.getmaxyx()[1]-3), curses.A_DIM)

    def draw(self):
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        self.hitboxes = []
        self.put(0, 1, "HERMES  /  " + self.profile.title, curses.color_pair(1) | curses.A_BOLD)
        self.put(1, 1, self.profile.name, curses.A_DIM)
        self.line(2)
        if height < 24 or width < 25:
            self.put(4, 1, "Enlarge this sidebar to use controls")
            self.screen.refresh()
            return
        if self.detail:
            self.draw_detail(height, width)
        else:
            content_bottom = height - 7
            self.bot_columns = max(2, min(4, (width-2)//12))
            bot_rows = max(1, (len(self.bots)+self.bot_columns-1)//self.bot_columns)
            bots_height = min(2 + bot_rows*2, max(4, (height-10)//3))
            bots_y = content_bottom - bots_height
            jobs_height = min(max(4, len(self.jobs)*3+2), max(4, (bots_y-3)//2))
            chats_y = 3 + jobs_height + 1
            enabled = sum(job_enabled(j) for j in self.jobs)
            self.put(3, 1, "CRON JOBS  " + str(enabled) + "/" + str(len(self.jobs)) + " on",
                     curses.color_pair(1) | (curses.A_BOLD if self.section == "jobs" else 0))
            self.render_rows("jobs", 4, jobs_height-1, 3, width)
            self.line(chats_y-1)
            title = "ALL CONVERSATIONS" if self.include_background else "RECENT CONVERSATIONS"
            self.put(chats_y, 1, title + "  " + str(len(self.conversations)),
                     curses.color_pair(1) | (curses.A_BOLD if self.section == "conversations" else 0))
            self.render_rows("conversations", chats_y+1, bots_y-chats_y-1, 2, width)
            self.line(bots_y)
            self.render_bots(bots_y+1, bots_height-1, width)
            self.line(content_bottom)
            self.put(height-6, 1, "Tab switch  Space toggle  Enter open", curses.A_DIM)
            self.put(height-5, 1, "n new chat   d details   e edit job", curses.A_DIM)
            self.put(height-4, 1, "b bots  r refresh  a all  Esc terminal", curses.A_DIM)
            self.hitboxes.extend([(height-5, 1, 12, "new", None),
                                  (height-4, 1, 6, "bots", None),
                                  (height-4, 9, 17, "refresh", None),
                                  (height-4, 20, 24, "all", None)])
        self.put(height-2, 1, self.status, curses.color_pair(4) if self.status_error else curses.A_DIM)
        self.screen.refresh()

    def render_bots(self, y, available, width):
        columns = self.bot_columns
        rows = max(1, (available-1)//2)
        capacity = rows * columns
        selected = self.selected['bots']
        offset = (selected // capacity) * capacity
        self.offset['bots'] = offset
        title = 'BOTS  ' + str(len(self.bots))
        if len(self.bots) > capacity:
            title += '  ' + str(offset+1) + '-' + str(min(offset+capacity, len(self.bots)))
        self.put(y, 1, title, curses.color_pair(1) | (curses.A_BOLD if self.section == 'bots' else 0))
        cell_width = (width-2)//columns
        for index, bot in enumerate(self.bots[offset:offset+capacity], offset):
            cell = index-offset
            row, x = y+1+(cell//columns)*2, 1+(cell%columns)*cell_width
            active = self.section == 'bots' and selected == index
            attr = curses.A_REVERSE if active else 0
            icon = bot.get('icon') or '✦'
            badges = (' ●' if bot.get('unread') else '') + (' ↻' if bot.get('busy') else '')
            self.put(row, x, icon + badges, attr | curses.color_pair(3 if bot.get('unread') else 1), cell_width-1)
            self.put(row+1, x, bot.get('label') or bot['id'], attr, cell_width-1)
            self.hitboxes.extend((r, x, x+cell_width-1, 'bot', index) for r in (row, row+1))

    def render_rows(self, section, start_y, available, row_height, width):
        rows = self.rows(section)
        capacity = max(0, available // row_height)
        if not capacity:
            return
        selected = self.selected[section]
        offset = self.offset[section]
        if selected < offset:
            offset = selected
        if selected >= offset+capacity:
            offset = selected-capacity+1
        offset = max(0, min(offset, max(0, len(rows)-capacity)))
        self.offset[section] = offset
        if not rows:
            self.put(start_y+1, 2, "No scheduled jobs" if section == "jobs" else "No saved conversations", curses.A_DIM)
            return
        for i, row in enumerate(rows[offset:offset+capacity], offset):
            y = start_y + (i-offset)*row_height
            active = self.section == section and i == selected
            attr = curses.A_REVERSE if active else 0
            if section == "jobs":
                completed = row.get("state") == "completed"
                on = job_enabled(row)
                badge = "[done]" if completed else "[ ON ]" if on else "[off ]"
                self.put(y, 1, badge, attr | (curses.A_DIM if completed else curses.color_pair(2 if on else 3)))
                self.put(y, 8, row.get("name") or row["id"], attr | curses.A_BOLD)
                self.put(y+1, 3, schedule_text(row), curses.A_DIM)
                state = row.get("last_status") or row.get("state") or "scheduled"
                timing = "next " + relative_time(row["next_run_at"]) if row.get("next_run_at") and on else row.get("state", "paused")
                self.put(y+2, 3, str(timing) + " · " + str(state),
                         curses.color_pair(4) if row.get("last_error") else curses.A_DIM)
                self.hitboxes.append((y, 1, 7, "toggle", i))
                self.hitboxes.extend((line, 8 if line == y else 1, width-1, "job", i)
                                     for line in range(y, y+3))
            else:
                self.put(y, 2, row["title"], attr | curses.A_BOLD)
                self.put(y+1, 3, row.get("source", "") + " · " + relative_time(row.get("activity_at")), curses.A_DIM)
                self.hitboxes.extend((line, 1, width-1, "conversation", i) for line in (y, y+1))
        if len(rows) > capacity:
            self.put(start_y+available-1, max(1, width-17), str(offset+1)+"-"+str(min(len(rows),offset+capacity))+" / "+str(len(rows)), curses.A_DIM)

    def draw_detail(self, height, width):
        row = self.detail
        lines = []
        if self.section == "jobs":
            items = [("JOB", row.get("name") or row["id"]), ("Status", "ON" if job_enabled(row) else row.get("state", "paused")),
                     ("Schedule", schedule_text(row)), ("Next run", row.get("next_run_at")),
                     ("Last run", row.get("last_run_at")), ("Last result", row.get("last_status")),
                     ("Delivery", row.get("deliver")), ("Skills", ", ".join(row.get("skills") or [])),
                     ("Error", row.get("last_error") or row.get("last_delivery_error")), ("Prompt", row.get("prompt")),
                     ("Script", row.get("script")), ("ID", row["id"])]
        else:
            items = [("CONVERSATION", row.get("title")), ("Source", row.get("source")),
                     ("Activity", relative_time(row.get("activity_at"))), ("Directory", row.get("cwd")), ("ID", row["id"])]
        for name, value in items:
            if value is not None and value != "":
                lines.extend(textwrap.wrap(name + ": " + clean_text(value, 10000), max(10,width-4)) or [""])
                lines.append("")
        self.detail_offset = max(0, min(self.detail_offset, max(0,len(lines)-(height-8))))
        for i, line in enumerate(lines[self.detail_offset:self.detail_offset+height-8]):
            self.put(i+4, 2, line)
        self.line(height-4)
        self.put(height-3, 1, "Esc back   Space toggle   e edit" if self.section == "jobs" else "Esc back   Enter open conversation", curses.A_DIM)

    def edit_job(self):
        job = self.current("jobs")
        if not job or self.action_future:
            return
        value = self.prompt("Schedule", schedule_text(job))
        if value is not None and value.strip() and value != schedule_text(job):
            self.action(lambda: self.manager.cron_command(self.profile, "edit", job["id"], schedule=value.strip()), "Schedule updated")
            self.detail = None

    def prompt(self, label, initial):
        value, position = initial, len(initial)
        curses.curs_set(1)
        try:
            while True:
                height, width = self.screen.getmaxyx()
                self.draw()
                self.put(height-5, 1, label + " (Enter save, Esc cancel)", curses.color_pair(1))
                self.screen.move(height-4, 1)
                self.screen.clrtoeol()
                offset = max(0, position-(width-5))
                self.put(height-4, 1, value[offset:], curses.A_REVERSE)
                self.screen.move(height-4, min(width-2,1+position-offset))
                self.screen.refresh()
                try:
                    key = self.screen.get_wch()
                except curses.error:
                    continue
                if key in ("\n", "\r", curses.KEY_ENTER):
                    return value
                if key == "\x1b":
                    return None
                if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    if position:
                        value, position = value[:position-1]+value[position:], position-1
                elif key == curses.KEY_LEFT:
                    position = max(0,position-1)
                elif key == curses.KEY_RIGHT:
                    position = min(len(value),position+1)
                elif key == curses.KEY_HOME:
                    position = 0
                elif key == curses.KEY_END:
                    position = len(value)
                elif isinstance(key,str) and key.isprintable() and len(value)<300:
                    value, position = value[:position]+key+value[position:], position+1
        finally:
            curses.curs_set(0)

    def move(self, delta):
        if self.detail:
            self.detail_offset = max(0, self.detail_offset+delta)
        else:
            self.selected[self.section] = max(0,min(max(0,len(self.rows())-1),self.selected[self.section]+delta))

    def handle(self, key):
        if key == 27 and self.sgr_mouse():
            return
        if key == curses.KEY_MOUSE:
            try:
                _, x, y, _, button = curses.getmouse()
            except curses.error:
                return
            self.mouse(x,y,button)
        elif key in (curses.KEY_UP, ord("k")):
            self.move(-self.bot_columns if self.section == 'bots' else -1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.move(self.bot_columns if self.section == 'bots' else 1)
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT) and self.section == 'bots':
            self.move(-1 if key == curses.KEY_LEFT else 1)
        elif key == curses.KEY_PPAGE:
            self.move(-5)
        elif key == curses.KEY_NPAGE:
            self.move(5)
        elif key == ord("\t"):
            sections = ('jobs', 'conversations', 'bots')
            self.section = sections[(sections.index(self.section)+1) % len(sections)]
            self.detail = None
        elif key == ord('b'):
            self.section, self.detail = 'bots', None
        elif key in (ord(" "), ord("x")) and self.section == "jobs":
            self.toggle()
        elif key in (10,13,curses.KEY_ENTER):
            if self.section == "conversations":
                self.open_conversation()
            elif self.section == 'bots':
                self.open_bot()
            else:
                self.detail, self.detail_offset = self.current(), 0
        elif key == ord("d") and self.section != 'bots':
            self.detail, self.detail_offset = self.current(), 0
        elif key == ord("e") and self.section == "jobs":
            self.edit_job()
        elif key == ord("n"):
            self.new_chat()
        elif key == ord("r"):
            self.last_refresh = 0
        elif key == ord("a"):
            self.include_background = not self.include_background
            self.last_refresh = 0
        elif key in (27, ord("q")):
            if self.detail:
                self.detail = None
            else:
                self.return_to_terminal()

    def new_chat(self):
        self.action(lambda: self.manager.open_chat(self.profile), "New profile conversation opened")

    def sgr_mouse(self):
        """Decode SGR reports when the installed macOS terminfo only recognizes X10.

        Modern Herdr/outer terminals use SGR coordinates; ncurses versions and terminfo
        entries disagree on kmous. Normal KEY_MOUSE reports still use curses.getmouse().
        """
        self.screen.timeout(20)
        sequence = ""
        try:
            while len(sequence)<60:
                value=self.screen.getch()
                if value == -1:
                    break
                if value>255:
                    curses.ungetch(value)
                    break
                sequence += chr(value)
                if sequence[-1] in "Mm":
                    break
        finally:
            self.screen.timeout(150)
        if not sequence:
            return False
        match=re.fullmatch(r"\[<(\d+);(\d+);(\d+)([Mm])",sequence)
        if match:
            code,x,y=int(match[1]),int(match[2])-1,int(match[3])-1
            if match[4]=="M" and not code & 32:
                if code & 64:
                    self.mouse(x,y,curses.BUTTON4_PRESSED if code%2==0 else getattr(curses,"BUTTON5_PRESSED",0))
                elif code & 3 == 0:
                    self.mouse(x,y,curses.BUTTON1_PRESSED)
        return True

    def mouse(self,x,y,button):
        if button & (curses.BUTTON4_PRESSED | getattr(curses,"BUTTON5_PRESSED",0)):
            for row_y,_,_,action,_ in self.hitboxes:
                if row_y==y:
                    if action in ("job","toggle"):
                        self.section="jobs"
                    elif action=="conversation":
                        self.section="conversations"
                    elif action=='bot':
                        self.section='bots'
                    break
            self.move(-1 if button & curses.BUTTON4_PRESSED else 1)
            return
        if not button & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED | curses.BUTTON1_DOUBLE_CLICKED):
            return
        for row_y,x1,x2,action,index in self.hitboxes:
            if row_y==y and x1<=x<=x2:
                if action in ("toggle","job"):
                    self.section,self.selected["jobs"]="jobs",index
                    if action=="toggle":
                        self.toggle()
                    elif button & curses.BUTTON1_DOUBLE_CLICKED:
                        self.detail,self.detail_offset=self.current(),0
                elif action=="conversation":
                    self.section,self.selected["conversations"]="conversations",index
                    self.open_conversation()
                elif action=='bot':
                    self.section, self.selected['bots'] = 'bots', index
                    self.open_bot()
                elif action=='bots':
                    self.section, self.detail = 'bots', None
                elif action=="new":
                    self.new_chat()
                elif action=="refresh":
                    self.last_refresh=0
                elif action=="all":
                    self.include_background=not self.include_background
                    self.last_refresh=0
                return
