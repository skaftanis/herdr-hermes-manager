"""Read only the native read watermarks; never transcripts, secrets, or DB writes."""
import json
from pathlib import Path
import re
import sqlite3
import sys


def activity(root, profile, session_id):
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', profile):
        raise ValueError('Invalid profile')
    home = Path(root) if profile == 'default' else Path(root) / 'profiles' / profile
    db = (home / 'state.db').resolve()
    with sqlite3.connect(db.as_uri()+'?mode=ro', uri=True, timeout=1) as connection:
        row = connection.execute('SELECT last_read_at, last_activity_at, started_at FROM sessions WHERE id=?',
                                 (session_id,)).fetchone()
        if not row:
            return None
        latest = connection.execute('SELECT MAX(timestamp) FROM messages WHERE session_id=?', (session_id,)).fetchone()[0]
        last_active = row[1] or latest or row[2] or 0
        # Mirrors SessionDB.session_unread: NULL means read but not yet tracked.
        return {'tracked': row[0] is not None, 'unread': row[0] is not None and last_active > row[0]}


if __name__ == '__main__':
    request = json.load(sys.stdin)
    result = {}
    for profile, session_id in request['sessions'].items():
        try:
            result[profile] = activity(request['root'], profile, session_id)
        except (OSError, sqlite3.Error, ValueError):
            result[profile] = None
    print(json.dumps(result))
