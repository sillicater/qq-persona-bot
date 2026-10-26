"""SQLite 存储层。单进程单连接 + 锁，够用且简单。"""
import sqlite3
import threading
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        REAL NOT NULL,
  chat_id   TEXT NOT NULL,
  role      TEXT NOT NULL,          -- user / bot
  text      TEXT NOT NULL,
  tokens    INTEGER DEFAULT 0,
  summarized INTEGER DEFAULT 0,
  episode_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_msg_chat ON messages(chat_id, id);

CREATE TABLE IF NOT EXISTS episodes (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_start     REAL,
  ts_end       REAL,
  chat_id      TEXT,
  summary      TEXT NOT NULL,
  topics       TEXT DEFAULT '',
  tone         TEXT DEFAULT '',
  salience     REAL DEFAULT 0.5,
  n_msgs       INTEGER DEFAULT 0,
  recall_count INTEGER DEFAULT 0,
  last_recalled REAL,
  dormant      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS facts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  key         TEXT UNIQUE NOT NULL,
  value       TEXT NOT NULL,
  category    TEXT DEFAULT '其他',
  confidence  REAL DEFAULT 0.8,
  salience    REAL DEFAULT 0.6,
  ts_created  REAL,
  ts_updated  REAL,
  last_used   REAL
);

CREATE TABLE IF NOT EXISTS fact_history (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        REAL,
  key       TEXT,
  old_value TEXT,
  new_value TEXT,
  reason    TEXT
);

CREATE TABLE IF NOT EXISTS persona_state (
  id         INTEGER PRIMARY KEY CHECK (id = 1),
  state_json TEXT NOT NULL,
  updated_ts REAL
);

CREATE TABLE IF NOT EXISTS persona_history (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      REAL,
  diff    TEXT,
  reason  TEXT
);

CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT
);

CREATE TABLE IF NOT EXISTS reminders (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      REAL,
  chat_id TEXT,
  due     REAL,
  text    TEXT,
  sent    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS life_events (
  id   INTEGER PRIMARY KEY AUTOINCREMENT,
  ts   REAL,
  day  TEXT,
  text TEXT,
  kind TEXT DEFAULT '日常'
);

CREATE TABLE IF NOT EXISTS life_threads (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  title   TEXT,
  note    TEXT,
  status  TEXT DEFAULT 'active',
  updated REAL
);

CREATE TABLE IF NOT EXISTS files (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      REAL,
  chat_id TEXT,
  name    TEXT,
  size    TEXT
);

CREATE TABLE IF NOT EXISTS memes (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  term    TEXT UNIQUE,
  meaning TEXT,
  example TEXT,
  ts      REAL
);

CREATE TABLE IF NOT EXISTS alarms (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      REAL,
  chat_id TEXT,
  due     REAL,
  reason  TEXT,
  fired   INTEGER DEFAULT 0
);
"""

_conn = None
_lock = threading.RLock()


def init(path):
    global _conn
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(str(path), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.executescript(_SCHEMA)
        _migrate()
        _conn.commit()


def _migrate():
    """老库补列：间隔重复需要的三个字段。"""
    cols = {r["name"] for r in _conn.execute("PRAGMA table_info(facts)")}
    for name, ddl in (
        ("interval", "REAL DEFAULT 1"),
        ("ease", "REAL DEFAULT 2.5"),
        ("due_at", "REAL DEFAULT 0"),
    ):
        if name not in cols:
            _conn.execute(f"ALTER TABLE facts ADD COLUMN {name} {ddl}")


def q(sql, args=()):
    with _lock:
        return _conn.execute(sql, args).fetchall()


def one(sql, args=()):
    with _lock:
        return _conn.execute(sql, args).fetchone()


def run(sql, args=()):
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur.lastrowid


def meta_get(k, default=None):
    row = one("SELECT v FROM meta WHERE k=?", (k,))
    return row["v"] if row else default


def meta_set(k, v):
    run("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
