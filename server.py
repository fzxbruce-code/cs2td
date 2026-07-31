# -*- coding: utf-8 -*-
# BUFFGO 局域网服务：静态文件 + SQLite 共享排行榜 + 游戏统计
# 启动：python server.py   然后同事访问 http://<你的局域网IP>:8000/
import http.server, socketserver, sqlite3, json, os, time, threading
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone, timedelta

DIR = os.path.dirname(os.path.abspath(__file__))
DB  = os.path.join(DIR, 'leaderboard.db')
PORT = 8000
_lock = threading.Lock()

TZ = timezone(timedelta(hours=8))  # UTC+8

def init_db():
    con = sqlite3.connect(DB)
    con.execute('CREATE TABLE IF NOT EXISTS lb(name TEXT PRIMARY KEY, wave INTEGER, ts INTEGER)')
    con.execute('''CREATE TABLE IF NOT EXISTS game_sessions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nick TEXT,
        wave INTEGER,
        kills INTEGER,
        combos INTEGER,
        max_lv INTEGER,
        duration INTEGER,
        started_at INTEGER,
        ended_at INTEGER,
        day TEXT
    )''')
    con.commit(); con.close()

def get_lb():
    con = sqlite3.connect(DB)
    cur = con.execute('SELECT name, wave, ts FROM lb ORDER BY wave DESC, ts ASC LIMIT 100')
    rows = [{'name': r[0], 'wave': r[1], 'ts': r[2]} for r in cur.fetchall()]
    con.close(); return rows

def upsert(name, wave):
    ts = int(time.time())
    with _lock:
        con = sqlite3.connect(DB)
        cur = con.execute('SELECT wave FROM lb WHERE name=?', (name,))
        row = cur.fetchone()
        if row is None:
            con.execute('INSERT INTO lb(name, wave, ts) VALUES(?,?,?)', (name, wave, ts))
        elif wave > row[0]:
            con.execute('UPDATE lb SET wave=?, ts=? WHERE name=?', (wave, ts, name))
        con.commit(); con.close()
    return get_lb()

def remove(name):
    with _lock:
        con = sqlite3.connect(DB)
        con.execute('DELETE FROM lb WHERE name=?', (name,))
        con.commit(); con.close()
    return get_lb()

def record_session(data):
    """记录一局游戏数据"""
    nick = str(data.get('nick', '')).strip()[:10] or '匿名'
    wave = int(data.get('wave', 0))
    kills = int(data.get('kills', 0))
    combos = int(data.get('combos', 0))
    max_lv = int(data.get('maxLv', 0))
    duration = int(data.get('duration', 0))
    started_at = int(data.get('startedAt', 0))
    ended_at = int(data.get('endedAt', 0))
    day = datetime.now(TZ).strftime('%Y-%m-%d')
    with _lock:
        con = sqlite3.connect(DB)
        con.execute('''INSERT INTO game_sessions(nick, wave, kills, combos, max_lv, duration, started_at, ended_at, day)
            VALUES(?,?,?,?,?,?,?,?,?)''', (nick, wave, kills, combos, max_lv, duration, started_at, ended_at, day))
        con.commit(); con.close()
    return {'ok': True}

def get_stats():
    """获取统计数据：累计 + 每日"""
    con = sqlite3.connect(DB)

    # 累计统计
    total = con.execute('SELECT COUNT(*) FROM game_sessions').fetchone()[0]
    total_players = con.execute('SELECT COUNT(DISTINCT nick) FROM game_sessions').fetchone()[0]
    total_avg_wave = con.execute('SELECT CAST(AVG(wave) AS REAL) FROM game_sessions').fetchone()[0] or 0
    total_avg_duration = con.execute('SELECT CAST(AVG(duration) AS REAL) FROM game_sessions').fetchone()[0] or 0
    total_max_wave = con.execute('SELECT MAX(wave) FROM game_sessions').fetchone()[0] or 0
    total_kills = con.execute('SELECT SUM(kills) FROM game_sessions').fetchone()[0] or 0

    # 每日统计（最近30天）
    daily_rows = con.execute('''
        SELECT day,
               COUNT(*) as plays,
               COUNT(DISTINCT nick) as players,
               CAST(AVG(wave) AS REAL) as avg_wave,
               CAST(AVG(duration) AS REAL) as avg_duration,
               MAX(wave) as max_wave,
               SUM(kills) as kills
        FROM game_sessions
        GROUP BY day
        ORDER BY day DESC
        LIMIT 30
    ''').fetchall()
    daily = []
    for r in daily_rows:
        daily.append({
            'day': r[0], 'plays': r[1], 'players': r[2],
            'avg_wave': round(r[3] or 0, 1),
            'avg_duration': round(r[4] or 0),
            'max_wave': r[5] or 0,
            'kills': r[6] or 0
        })

    # 最近20局明细
    recent = con.execute('''
        SELECT nick, wave, kills, combos, max_lv, duration, started_at, ended_at, day
        FROM game_sessions
        ORDER BY id DESC
        LIMIT 20
    ''').fetchall()
    recent_list = []
    for r in recent:
        recent_list.append({
            'nick': r[0], 'wave': r[1], 'kills': r[2], 'combos': r[3],
            'maxLv': r[4], 'duration': r[5],
            'startedAt': r[6], 'endedAt': r[7], 'day': r[8]
        })

    con.close()

    return {
        'total': {
            'plays': total,
            'players': total_players,
            'avg_wave': round(total_avg_wave, 1),
            'avg_duration': round(total_avg_duration),
            'max_wave': total_max_wave,
            'total_kills': total_kills
        },
        'daily': daily,
        'recent': recent_list
    }


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=DIR, **k)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith('/api/lb'):
            return self._json(get_lb())
        if parsed.path.startswith('/api/stats'):
            return self._json(get_stats())
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith('/api/lb'):
            try:
                ln = int(self.headers.get('Content-Length', 0) or 0)
                data = json.loads(self.rfile.read(ln) or b'{}')
                name = str(data.get('name', '')).strip()[:10]
                wave = int(data.get('wave', 0))
                if not name:
                    return self._json({'error': 'bad name'}, 400)
                return self._json(upsert(name, wave))
            except Exception as e:
                return self._json({'error': str(e)}, 400)
        if parsed.path.startswith('/api/session'):
            try:
                ln = int(self.headers.get('Content-Length', 0) or 0)
                data = json.loads(self.rfile.read(ln) or b'{}')
                return self._json(record_session(data))
            except Exception as e:
                return self._json({'error': str(e)}, 400)
        return self._json({'error': 'not found'}, 404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith('/api/lb'):
            try:
                ln = int(self.headers.get('Content-Length', 0) or 0)
                data = json.loads(self.rfile.read(ln) or b'{}')
                name = str(data.get('name', '')).strip()[:10]
                if not name:
                    return self._json({'error': 'bad name'}, 400)
                return self._json(remove(name))
            except Exception as e:
                return self._json({'error': str(e)}, 400)
        return self._json({'error': 'not found'}, 404)

    def log_message(self, *a):
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == '__main__':
    init_db()
    with Server(('0.0.0.0', PORT), Handler) as httpd:
        print('BUFFGO 服务已启动: http://0.0.0.0:%d  (Ctrl+C 停止)' % PORT)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print('已停止')
