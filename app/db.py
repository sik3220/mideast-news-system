"""SQLite 저장. 수집 작업은 기사의 사용자 필드(선택·메모)와 요약을 덮어쓰지 않는다."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import config
from .timeutil import iso, now_utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
  id INTEGER PRIMARY KEY,
  url TEXT NOT NULL,
  url_norm TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  snippet TEXT NOT NULL DEFAULT '',
  source_name TEXT NOT NULL DEFAULT '',
  source_domain TEXT NOT NULL DEFAULT '',
  lang TEXT NOT NULL DEFAULT '',
  provider TEXT NOT NULL DEFAULT '',
  published_at TEXT,
  published_date TEXT,
  time_precision TEXT NOT NULL DEFAULT 'unknown',
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  access_level TEXT NOT NULL DEFAULT 'title',   -- title | snippet | fulltext
  search_topics TEXT NOT NULL DEFAULT '[]',     -- 이 기사를 찾아낸 관심 주제 검색
  event_id INTEGER,
  topics TEXT NOT NULL DEFAULT '[]',
  category TEXT NOT NULL DEFAULT '',
  region TEXT NOT NULL DEFAULT '',
  score REAL NOT NULL DEFAULT 0,
  reasons TEXT NOT NULL DEFAULT '[]',
  ko_title TEXT, ko_summary TEXT, stage TEXT, claim_type TEXT,
  followups TEXT NOT NULL DEFAULT '[]',
  summary_status TEXT NOT NULL DEFAULT 'none',  -- none | ok | failed
  summary_error TEXT,
  summary_basis TEXT,
  summary_flags TEXT NOT NULL DEFAULT '[]',
  selected INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT '',
  is_demo INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_articles_event ON articles(event_id);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
  date_from TEXT, date_to TEXT, keyword TEXT, status TEXT NOT NULL DEFAULT 'running',
  new_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS run_sources (
  id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, provider TEXT NOT NULL, label TEXT NOT NULL,
  topic TEXT, status TEXT NOT NULL,             -- ok | empty | failed
  count INTEGER NOT NULL DEFAULT 0, new_count INTEGER NOT NULL DEFAULT 0, error TEXT
);
CREATE TABLE IF NOT EXISTS drafts (
  id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, content TEXT NOT NULL,
  article_ids TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS draft_versions (
  id INTEGER PRIMARY KEY, key TEXT NOT NULL, content TEXT NOT NULL, saved_at TEXT NOT NULL, reason TEXT
);
CREATE TABLE IF NOT EXISTS doc_editions (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL, data TEXT NOT NULL,
  updated_at TEXT NOT NULL, UNIQUE(kind, key)
);
CREATE TABLE IF NOT EXISTS generated_files (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL, filename TEXT NOT NULL,
  template_version TEXT NOT NULL, created_at TEXT NOT NULL, warnings TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS locations (
  name TEXT PRIMARY KEY, x REAL NOT NULL, y REAL NOT NULL, updated_at TEXT NOT NULL
);
"""

JSON_FIELDS = ("topics", "reasons", "followups", "search_topics", "summary_flags")


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def session(path: Path | None = None):
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def row_to_article(row: sqlite3.Row) -> dict:
    a = dict(row)
    for f in JSON_FIELDS:
        a[f] = json.loads(a[f] or "[]")
    return a


def upsert_article(conn: sqlite3.Connection, a: dict) -> tuple[int, bool]:
    """정규화 URL 기준으로 저장한다. 이미 있으면 원본 레코드를 유지하고 관측 시각만 갱신한다.

    반환: (기사 id, 새로 저장했는지)
    """
    now = iso(now_utc())
    row = conn.execute("SELECT id, search_topics, published_at, snippet FROM articles WHERE url_norm=?",
                       (a["url_norm"],)).fetchone()
    if row:
        topics = sorted(set(json.loads(row["search_topics"])) | set(a.get("search_topics", [])))
        conn.execute("UPDATE articles SET last_seen_at=?, search_topics=? WHERE id=?",
                     (now, json.dumps(topics), row["id"]))
        # 발표 시각·요약문은 비어 있던 경우에만 채운다. 수정일로 발표일을 덮어쓰지 않는다.
        if not row["published_at"] and a.get("published_at"):
            conn.execute("UPDATE articles SET published_at=?, published_date=?, time_precision=? WHERE id=?",
                         (a["published_at"], a.get("published_date"), a.get("time_precision", "unknown"), row["id"]))
        if not row["snippet"] and a.get("snippet"):
            conn.execute("UPDATE articles SET snippet=?, access_level='snippet' WHERE id=? AND access_level='title'",
                         (a["snippet"], row["id"]))
        return row["id"], False
    cur = conn.execute(
        """INSERT INTO articles (url, url_norm, title, snippet, source_name, source_domain, lang, provider,
             published_at, published_date, time_precision, first_seen_at, last_seen_at, access_level,
             search_topics, is_demo)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (a["url"], a["url_norm"], a["title"], a.get("snippet", ""), a.get("source_name", ""),
         a.get("source_domain", ""), a.get("lang", ""), a.get("provider", ""), a.get("published_at"),
         a.get("published_date"), a.get("time_precision", "unknown"), now, now,
         a.get("access_level", "snippet" if a.get("snippet") else "title"),
         json.dumps(sorted(set(a.get("search_topics", [])))), int(a.get("is_demo", 0))))
    return cur.lastrowid, True


def get_article(conn, article_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
    return row_to_article(row) if row else None


def all_articles(conn, demo: bool = False) -> list[dict]:
    rows = conn.execute("SELECT * FROM articles WHERE is_demo=?", (int(demo),)).fetchall()
    return [row_to_article(r) for r in rows]


def get_edition(conn, kind: str, key: str) -> dict | None:
    row = conn.execute("SELECT data FROM doc_editions WHERE kind=? AND key=?", (kind, key)).fetchone()
    return json.loads(row["data"]) if row else None


def save_edition(conn, kind: str, key: str, data: dict) -> None:
    conn.execute(
        """INSERT INTO doc_editions (kind, key, data, updated_at) VALUES (?,?,?,?)
           ON CONFLICT(kind, key) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at""",
        (kind, key, json.dumps(data, ensure_ascii=False), iso(now_utc())))
