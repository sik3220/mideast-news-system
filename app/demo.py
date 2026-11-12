"""데모 모드(DEMO=1)용 가상 데이터. 실제 기사가 아니며 화면 전체에 데모 표시가 붙는다.

실제 수집 데이터와 섞이지 않도록 is_demo=1 로만 저장하고, 데모 모드에서만 조회한다.
"""
from __future__ import annotations

from datetime import timedelta

from . import db, pipeline
from .dedupe import normalize_url
from .timeutil import baghdad_date, iso, now_utc

SAMPLES = [
    ("en", "[DEMO] Sample headline about a Bismayah contract procedure", "Example Wire", "example.com", ""),
    ("ar", "[تجريبي] عنوان تجريبي عن جلسة مجلس الوزراء العراقي", "Example Arabic", "example.org", ""),
    ("ko", "[데모] 이라크 내각 관련 가상 기사 제목", "예시일보", "example.net", "데모용 검색 요약 문장입니다."),
    ("en", "[DEMO] Sample headline about Iraq security incident", "Example Daily", "example.edu", ""),
    ("en", "[DEMO] Sample headline about Iraq security incident", "Example Mirror", "example.info", ""),
    ("en", "[DEMO] Sample regional headline about Hormuz shipping", "Example Wire", "example.com", ""),
]


def seed(conn) -> None:
    if conn.execute("SELECT 1 FROM articles WHERE is_demo=1 LIMIT 1").fetchone():
        return
    now = now_utc()
    for i, (lang, title, source, domain, snippet) in enumerate(SAMPLES):
        when = now - timedelta(hours=3 * i + 1)
        url = f"https://{domain}/demo/{i + 1}"
        db.upsert_article(conn, {
            "url": url, "url_norm": normalize_url(url), "title": title, "snippet": snippet,
            "source_name": source, "source_domain": domain, "lang": lang, "provider": "demo",
            "published_at": iso(when), "published_date": baghdad_date(when), "time_precision": "datetime",
            "is_demo": 1})
    pipeline.recompute(conn, demo=True)
