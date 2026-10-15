from __future__ import annotations

import curses
import fcntl
import hashlib
import signal
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import textwrap
import unicodedata
import threading
import time

from .core import clean_text, read_json, write_json

BRIDGE = Path(__file__).with_name('bots_bridge.mjs')


def configure_bots(settings, api):
    settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_file = settings.state_dir / 'bots-runtime.json'
    config = {'root': str(settings.root), 'agent_root': str(settings.agent_root), 'python_bin': sys.executable,
              'hermes_bin': str(settings.hermes_bin), 'state_dir': str(settings.state_dir),
              'roster_file': str(settings.state_dir / 'bots-roster.json'),
              'herdr_socket': api.socket_path, 'plugin_id': settings.plugin_id,
              'profiles': [p.name for p in settings.profiles()],
              'labels': {p.name: p.title for p in settings.profiles()}}
    if read_json(config_file) != config:
        write_json(config_file, config)
    sidebar_file = settings.host_config / 'sidebar-sections.json'
    sections = read_json(sidebar_file, [])
    if not isinstance(sections, list):
        raise ValueError('Invalid sidebar-sections.json')
    # Migrate the old custom-client grid to the standard Manager pane.
    sections = [s for s in sections if s.get('state_file') != config['roster_file']]
    if sidebar_file.exists() and read_json(sidebar_file, []) != sections:
        write_json(sidebar_file, sections)
    # Hold the start lock until the PID is published. Never signal a prior process.
    with (settings.state_dir / 'bots-start.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = read_json(settings.state_dir / 'bots-worker.json')
        pid = record.get('pid')
        revision = hashlib.sha256(BRIDGE.read_bytes()).hexdigest()
        if pid:
            try:
                os.kill(int(pid), 0)
                if record.get('revision') == revision:
                    return {'running': True, 'pid': pid}
                command = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True).stdout
                if str(BRIDGE) + ' daemon ' not in command:
                    raise ValueError('The saved Bots worker PID belongs to another process; it was left alone')
                os.kill(int(pid), signal.SIGTERM)
                for _ in range(50):
                    try: os.kill(int(pid), 0)
                    except ProcessLookupError: break
                    time.sleep(0.1)
            except (ProcessLookupError, ValueError):
                pass
        node = shutil.which('node')
        if not node:
            raise ValueError('Node.js is required by the Hermes Bots bridge')
        with (settings.state_dir / 'bots-worker.log').open('ab') as log:
            os.chmod(log.name, 0o600)
            child = subprocess.Popen([node, str(BRIDGE), 'daemon', str(config_file)],
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        write_json(settings.state_dir / 'bots-worker.json', {'pid': child.pid, 'bridge': str(BRIDGE), 'revision': revision})
        return {'running': True, 'pid': child.pid}


def open_bot(settings, api, profile):
    configure_bots(settings, api)
    return api.call('plugin.pane.open', {'plugin_id': settings.plugin_id, 'entrypoint': 'bot-chat',
                     'placement': 'popup', 'width': '90%', 'height': '90%', 'focus': True,
                     # The manifest's Python script is relative to the plugin root.
                     # Hermes resolves its own profile/project directory separately.
                     'cwd': str(BRIDGE.parent.parent), 'env': {'HERDR_HERMES_BOT': profile.name}})


def message_text(value):
    if isinstance(value, str):
        return ''.join(c for c in value if c in '\n\t' or not unicodedata.category(c).startswith('C'))
    if isinstance(value, list):
        return '\n'.join(str(v.get('text') or v.get('content') or '[attachment]')
                         for v in value if isinstance(v, dict))
    return str(value or '')


class BotPopup:
    def __init__(self, settings, profile):
        self.settings, self.profile = settings, profile
        self.events = queue.Queue()
        self.messages, self.requests = [], []
        self.draft, self.cursor, self.scroll = '', 0, 0
        self.status, self.title = 'Connecting…', profile.title
        self.ready, self.busy = False, False
        self.stream = ''
        self.answers = {}
        self.request_scroll = 0
        self.outgoing = queue.Queue(maxsize=128)
        self.history_cache = None
        self.thinking = ''
        self.activity = []
        self.active_tools = {}
        self.turn_started = None
        self.selection = None
        self.selecting = False
        self.visible_text = {}

    def selected_text(self):
        if not self.selection:
            return ''
        start, end = sorted(self.selection)
        result = []
        for row in range(start[0], end[0]+1):
            text = self.visible_text.get(row, '')
            left = max(0, start[1]-2) if row == start[0] else 0
            right = max(0, end[1]-2+1) if row == end[0] else 100000
            col, chars = 0, []
            for char in text:
                width = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
                if left <= col < right:
                    chars.append(char)
                col += width
            result.append(''.join(chars).rstrip())
        return '\n'.join(result).strip()

    def copy_selection(self):
        text = self.selected_text()
        if not text:
            return
        def copy():
            try:
                subprocess.run(['/usr/bin/pbcopy'], input=text, text=True, check=True, timeout=3)
                self.events.put({'type': 'status', 'text': 'Copied selection to clipboard'})
            except (OSError, subprocess.SubprocessError):
                self.events.put({'type': 'status', 'text': 'Could not copy selection'})
        threading.Thread(target=copy, daemon=True).start()

    def start_activity(self):
        if not self.busy:
            self.thinking = ''
            self.activity = []
            self.active_tools = {}
            self.turn_started = time.monotonic()
        self.busy = True

    def activity_lines(self):
        lines = list(self.activity[-2:])
        if self.thinking and self.busy:
            lines.append('Thinking: ' + clean_text(self.thinking, 500))
        return lines[-3:]

    def send(self, value):
        if self.child.poll() is not None:
            self.status = 'Connection closed. Esc to close; reopen the bot to retry.'
            return
        try:
            self.outgoing.put_nowait(value)
        except queue.Full:
            self.status = 'Connection busy; please retry shortly.'

    def write_commands(self):
        # Pipe backpressure must never block keyboard, scrolling, or closing.
        while True:
            value = self.outgoing.get()
            try:
                self.child.stdin.write(json.dumps(value) + '\n')
                self.child.stdin.flush()
            except (OSError, ValueError):
                self.events.put({'type': 'closed'})
                return

    def read_events(self):
        for line in self.child.stdout:
            try:
                self.events.put(json.loads(line))
            except ValueError:
                pass
        self.events.put({'type': 'closed'})

    def run(self):
        node = shutil.which('node')
        log_file = self.settings.state_dir / 'bots-popup.log'
        with log_file.open('ab') as log:
            os.chmod(log_file, 0o600)
            self.child = subprocess.Popen([node, str(BRIDGE), 'chat', str(self.settings.state_dir / 'bots-runtime.json'),
                                           self.profile.name], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=log, text=True, bufsize=1)
            threading.Thread(target=self.read_events, daemon=True).start()
            threading.Thread(target=self.write_commands, daemon=True).start()
            # Native popup dismissal can hang up the PTY instead of sending Esc.
            previous = {sig: signal.getsignal(sig) for sig in (signal.SIGHUP, signal.SIGTERM)}
            def dismiss(signum, frame):
                raise SystemExit(0)
            for sig in previous:
                signal.signal(sig, dismiss)
            try:
                curses.wrapper(self.loop)
            finally:
                try:
                    self.close_bridge()
                finally:
                    for sig, handler in previous.items():
                        signal.signal(sig, handler)

    def close_bridge(self):
        # Disconnect this frontend immediately. The native Hermes server owns
        # the conversation and running turn; never signal it or a process group.
        if self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=0.15)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=0.15)
        try:
            self.child.stdin.close()
        except BrokenPipeError:
            pass

    def consume(self, item):
        kind = item.get('type')
        if kind in ('session', 'slash') or (kind == 'event' and
                (item.get('event') or {}).get('type', '').startswith('message.')):
            self.history_cache = None
        if kind == 'session':
            session = item['session']
            self.messages = [(m.get('role', 'assistant'), message_text(m.get('content') or m.get('text')))
                             for m in session.get('messages', []) if m.get('role') in ('user', 'assistant')
                             and m.get('display_kind') != 'hidden']
            self.title = item.get('title', self.title)
            self.ready, self.busy = True, bool(session.get('running'))
            if self.busy and self.turn_started is None:
                self.turn_started = time.monotonic()
            self.status = 'Connected · continuous Bot Chat'
            inflight = session.get('inflight') or {}
            self.stream = message_text(inflight.get('text'))
        elif kind in ('status', 'error'):
            self.status = clean_text(item.get('text'), 300)
            if kind == 'error': self.busy = False
        elif kind == 'closed':
            self.ready = False
            self.status = 'Disconnected. Esc to close; reopen to retry.'
        elif kind == 'request':
            request = item['request']
            if not any(r['id'] == request['id'] for r in self.requests):
                self.requests.append(request)
                if len(self.requests) == 1:
                    self.answers = dict(request.get('params', {}).get('answers') or {})
                    self.request_scroll = 0
        elif kind == 'slash':
            result = item.get('result', {})
            self.messages.append(('system', message_text(result.get('output') or result.get('message') or result)))
        elif kind == 'event':
            event = item.get('event') or {}
            payload = event.get('payload') or {}
            name = event.get('type')
            if name == 'message.start':
                self.start_activity()
                self.status = 'Thinking…'
            elif name == 'message.delta':
                self.start_activity()
                self.stream += message_text(payload.get('text'))
                self.status = 'Writing reply…'
            elif name in ('reasoning.delta', 'reasoning.available'):
                self.start_activity()
                text = message_text(payload.get('text'))
                self.thinking = ((self.thinking if name == 'reasoning.delta' else '') + text)[-2000:]
                self.status = 'Thinking…'
            elif name == 'message.interim':
                self.messages.append(('assistant', message_text(payload.get('text'))))
                self.stream = ''
            elif name == 'message.complete':
                text = message_text(payload.get('text')) or self.stream
                if text: self.messages.append(('assistant', text))
                self.stream, self.busy = '', False
                self.status = clean_text(payload.get('error') or 'Ready · continuous Bot Chat', 300)
                self.send({'type': 'refresh'})
            elif name in ('session.reclaimed', 'session.compacted'):
                self.send({'type': 'refresh'})
            elif name == 'status.update':
                if payload.get('text'):
                    self.status = clean_text(payload.get('text'), 300)
            elif name == 'tool.start':
                self.start_activity()
                tool = clean_text(payload.get('name') or 'tool', 80)
                self.active_tools[payload.get('tool_id', tool)] = tool
                self.status = 'Using ' + tool
                context = clean_text(payload.get('context') or '', 180)
                self.activity.append('→ ' + tool + (': ' + context if context else ''))
                self.activity = self.activity[-8:]
            elif name == 'tool.complete':
                tool = clean_text(payload.get('name') or 'tool', 80)
                self.active_tools.pop(payload.get('tool_id', tool), None)
                summary = clean_text(payload.get('summary') or '', 180)
                duration = payload.get('duration_s')
                elapsed = f' ({duration:.1f}s)' if isinstance(duration, (int, float)) else ''
                self.activity.append('✓ ' + tool + elapsed + (': ' + summary if summary else ''))
                self.activity = self.activity[-8:]
                self.status = 'Using ' + ', '.join(self.active_tools.values()) if self.active_tools else 'Thinking…'
            elif name == 'request.cancel':
                target = payload.get('id') or payload.get('request_id')
                self.requests = [r for r in self.requests if r['id'] != target]

    def request_lines(self):
        if not self.requests: return []
        request = self.requests[0]
        p, method = request.get('params', {}), request['method']
        if method == 'approval':
            return ['Approval required', p.get('description', ''), p.get('command', ''),
                    'Enter approve once · Esc deny']
        if method == 'clarify':
            questions = p.get('questions') or [{'qid': '', 'question': p.get('question'), 'choices': p.get('choices')}]
            q = next((q for q in questions if q.get('qid', '') not in self.answers), questions[-1])
            return [q.get('question') or 'Your answer', ' / '.join(q.get('choices') or []), 'Type answer · Enter submit · Esc skip']
        if method in ('sudo', 'secret', 'vault.unlock', 'vault.setup'):
            return [p.get('prompt') or p.get('command') or method, 'Input is hidden · Enter submit · Esc decline']
        return [method + ' requires a capability this popup cannot provide.', 'Esc dismiss; use Hermes Desktop for this action.']

    def reply(self, decline=False):
        request = self.requests[0]
        method, p = request['method'], request.get('params', {})
        if method == 'approval':
            choice = 'deny' if decline else 'once'
            if choice not in p.get('choices', ['once', 'deny']):
                self.status = 'The requested approval choice is not available'
                return
            result = {'choice': choice}
        elif method == 'clarify':
            if p.get('questions'):
                q = next((q for q in p['questions'] if q['qid'] not in self.answers), None)
                if q: self.answers[q['qid']] = '' if decline else self.draft
                self.draft, self.cursor = '', 0
                if len(self.answers) < len(p['questions']): return
                result = {'answers': self.answers.copy()}
            else: result = {'answer': '' if decline else self.draft}
        elif method in ('sudo', 'secret', 'vault.unlock', 'vault.setup'):
            result = {'value': '' if decline else self.draft}
        else:
            self.send({'type': 'reply', 'id': request['id'], 'error': 'This chat popup does not implement ' + method})
            self.requests.pop(0)
            self.draft, self.cursor = '', 0
            return
        self.send({'type': 'reply', 'id': request['id'], 'result': result})
        self.requests.pop(0)
        self.draft, self.cursor, self.answers = '', 0, {}

    def draw(self, win):
        h, w = win.getmaxyx()
        win.erase()
        self.visible_text = {}
        def put(y, text, attr=0):
            if 0 <= y < h:
                try: win.addnstr(y, 2, text, max(0, w-4), attr)
                except curses.error: pass
        put(0, '✦ ' + self.title + '   ·   @' + self.profile.name, curses.A_BOLD | curses.color_pair(1))
        spinner = '◐◓◑◒'[int(time.monotonic()*4) % 4] if self.busy else '●'
        elapsed = f' · {int(time.monotonic()-self.turn_started)}s' if self.busy and self.turn_started is not None else ''
        put(1, spinner + ' ' + self.status + elapsed, curses.color_pair(1))
        request = self.request_lines()
        request_wrapped = [line for paragraph in request for line in
                           textwrap.wrap(message_text(paragraph), max(1, w-5)) or ['']]
        request_height = max(0, h-8) if request else 0
        activity = self.activity_lines() if not request else []
        bottom = h - 5 - request_height - len(activity)
        if self.history_cache is None or self.history_cache[0] != (w, self.title, len(self.messages)):
            lines = []
            for role, content in self.messages + ([('assistant', self.stream)] if self.stream else []):
                if not content: continue
                lines.append((('You' if role == 'user' else self.title if role == 'assistant' else 'Hermes') + ':', curses.A_BOLD))
                for paragraph in content.splitlines() or ['']:
                    lines.extend((line, 0) for line in textwrap.wrap(paragraph, max(1, w-5), replace_whitespace=False) or [''])
                lines.append(('', 0))
            self.history_cache = ((w, self.title, len(self.messages)), lines)
        lines = self.history_cache[1]
        max_scroll = max(0, len(lines) - max(0, bottom-3))
        self.scroll = min(max_scroll, self.scroll)
        end = len(lines) - self.scroll
        visible = lines[max(0, end-max(0, bottom-3)):end]
        for y, (text, attr) in enumerate(visible, 3):
            put(y, text, attr)
            self.visible_text[y] = text
        if self.selection:
            start, end = sorted(self.selection)
            for row in range(start[0], end[0]+1):
                if row not in self.visible_text: continue
                left = max(2, start[1]) if row == start[0] else 2
                right = min(w-2, end[1]+1) if row == end[0] else w-2
                if right > left:
                    try: win.chgat(row, left, right-left, curses.A_REVERSE)
                    except curses.error: pass
        for y, text in enumerate(activity, h-5-len(activity)):
            put(y, text, curses.color_pair(1))
        if request:
            self.request_scroll = min(self.request_scroll, max(0, len(request_wrapped)-request_height+1))
            for y, text in enumerate(request_wrapped[self.request_scroll:self.request_scroll+request_height-1], bottom+1):
                put(y, text, curses.color_pair(2))
        put(h-4, '─' * max(0, w-4), curses.A_DIM)
        masked = self.requests and self.requests[0]['method'] in ('sudo', 'secret', 'vault.unlock', 'vault.setup')
        display = '•' * len(self.draft) if masked else self.draft
        start = max(0, self.cursor-(w-8))
        put(h-3, '> ' + display[start:].replace('\n', ' ↵ '))
        put(h-1, 'Drag to copy · Wheel/PgUp/PgDn scroll · Enter send · Esc/Ctrl+D close', curses.A_DIM)
        try: win.move(h-3, min(w-2, 4+self.cursor-start))
        except curses.error: pass
        win.refresh()

    def scroll_history(self, amount):
        self.selection = None
        if self.requests:
            self.request_scroll = max(0, self.request_scroll - amount)
        else:
            self.scroll = max(0, self.scroll + amount)

    def loop(self, win):
        curses.set_escdelay(25)
        curses.curs_set(1)
        curses.nonl()
        if curses.has_colors():
            curses.start_color(); curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1); curses.init_pair(2, curses.COLOR_YELLOW, -1)
        curses.mouseinterval(0)
        curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
        win.timeout(30); win.keypad(True)
        dirty = True
        last_draw = 0.0
        while True:
            deadline = time.monotonic() + 0.008
            for _ in range(0 if self.selecting else 25):
                try:
                    self.consume(self.events.get_nowait())
                    dirty = True
                except queue.Empty: break
                if time.monotonic() >= deadline: break
            if dirty or (self.busy and time.monotonic() - last_draw >= 0.25):
                self.draw(win)
                last_draw = time.monotonic()
                dirty = False
            try: key = win.get_wch()
            except curses.error: continue
            dirty = True
            if key == '\x04':
                break
            if key == curses.KEY_MOUSE:
                try:
                    _, x, y, _, buttons = curses.getmouse()
                except curses.error:
                    continue
                if buttons & curses.BUTTON4_PRESSED:
                    self.scroll_history(3)
                elif buttons & getattr(curses, 'BUTTON5_PRESSED', 1 << 21):
                    self.scroll_history(-3)
                elif buttons & curses.BUTTON1_PRESSED:
                    if self.selecting:
                        self.selection = (self.selection[0], (y, x))
                    elif y in self.visible_text:
                        self.selection = ((y, x), (y, x))
                        self.selecting = True
                elif buttons & curses.BUTTON1_RELEASED and self.selecting:
                    self.selection = (self.selection[0], (y, x))
                    self.selecting = False
                    self.copy_selection()
                elif self.selecting and buttons & curses.REPORT_MOUSE_POSITION:
                    self.selection = (self.selection[0], (y, x))
            elif key == curses.KEY_RESIZE:
                self.history_cache = None
            elif key == '\x1b':
                if self.requests: self.reply(decline=True)
                else: break
            elif key in ('\r', curses.KEY_ENTER):
                if self.requests: self.reply()
                elif self.ready and self.draft.strip():
                    text = self.draft
                    self.send({'type': 'send', 'text': text})
                    self.messages.append(('user', text))
                    self.start_activity()
                    self.status = 'Thinking…'
                    self.draft, self.cursor, self.scroll = '', 0, 0
            elif key == '\n':
                self.draft = self.draft[:self.cursor] + '\n' + self.draft[self.cursor:]; self.cursor += 1
            elif key == '\x12': self.send({'type': 'refresh'})
            elif key == curses.KEY_PPAGE:
                self.scroll_history(max(3, win.getmaxyx()[0]//2))
            elif key == curses.KEY_NPAGE:
                if self.requests: self.request_scroll += max(3, win.getmaxyx()[0]//2)
                else: self.scroll = max(0, self.scroll-win.getmaxyx()[0]//2)
            elif key == curses.KEY_LEFT: self.cursor = max(0, self.cursor-1)
            elif key == curses.KEY_RIGHT: self.cursor = min(len(self.draft), self.cursor+1)
            elif key == curses.KEY_HOME: self.cursor = 0
            elif key == curses.KEY_END: self.cursor = len(self.draft)
            elif key in ('\x7f', '\b', curses.KEY_BACKSPACE) and self.cursor:
                self.draft = self.draft[:self.cursor-1] + self.draft[self.cursor:]; self.cursor -= 1
            elif key == curses.KEY_DC: self.draft = self.draft[:self.cursor] + self.draft[self.cursor+1:]
            elif isinstance(key, str) and key.isprintable():
                self.draft = self.draft[:self.cursor] + key + self.draft[self.cursor:]; self.cursor += len(key)
