"""GDELT DOC API 검색. 키가 필요 없다. 5초당 1회 제한이 있어 호출 사이에 간격을 둔다.

GDELT 의 seendate 는 GDELT 가 기사를 관측한 시각이지 발표 시각이 아니다. 'observed' 로 구분해 저장한다.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

from ..dedupe import normalize_url
from ..timeutil import baghdad_date, day_bounds_utc, iso
from .base import FetchError, SearchResult, SearchSpec, safe_get

HOSTS = {"api.gdeltproject.org"}
MIN_INTERVAL = 6.0
LANG_MAP = {"korean": "ko", "english": "en", "arabic": "ar"}
_last_call = 0.0


def build_url(query: str, d_from: str, d_to: str) -> str:
    start, end = day_bounds_utc(d_from, d_to)
    fmt = lambda s: s.replace("-", "").replace(":", "").replace("T", "").replace("Z", "")
    query = f"{query} (sourcelang:english OR sourcelang:arabic OR sourcelang:korean)"
    return ("https://api.gdeltproject.org/api/v2/doc/doc?query=" + quote(query)
            + f"&mode=artlist&format=json&maxrecords=50&sort=datedesc"
            + f"&startdatetime={fmt(start)}&enddatetime={fmt(end)}")


def parse(data: bytes, spec: SearchSpec) -> list[dict]:
    text = data.decode("utf-8", errors="replace").strip()
    if not text.startswith("{"):
        # 제한 초과·잘못된 검색어일 때 JSON 대신 안내문이 온다.
        raise FetchError(text[:120] or "빈 응답")
    out = []
    for item in json.loads(text).get("articles", []):
        url, title = item.get("url", ""), (item.get("title") or "").strip()
        lang = LANG_MAP.get((item.get("language") or "").lower())
        if not url.startswith("http") or not title or not lang:
            continue   # 한·영·아랍어가 아닌 결과는 버린다
        article = {
            "url": url, "url_norm": normalize_url(url), "title": title, "snippet": "",
            "source_name": item.get("domain", ""),
            "source_domain": (item.get("domain") or urlsplit(url).hostname or "").removeprefix("www."),
            "lang": lang,
            "provider": "gdelt", "search_topics": [spec.topic] if spec.topic else [],
            "published_at": None, "published_date": None, "time_precision": "unknown",
        }
        try:
            dt = datetime.strptime(item.get("seendate", ""), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            article.update(published_at=iso(dt), published_date=baghdad_date(dt), time_precision="observed")
        except ValueError:
            pass
        out.append(article)
    return out


def search(spec: SearchSpec, d_from: str, d_to: str, http_get=safe_get, wait: bool = True) -> SearchResult:
    global _last_call
    if wait:
        delay = MIN_INTERVAL - (time.monotonic() - _last_call)
        if delay > 0:
            time.sleep(delay)
    try:
        try:
            data = http_get(build_url(spec.query, d_from, d_to), HOSTS)
        except FetchError as e:
            if "429" not in str(e) or not wait:
                raise
            time.sleep(MIN_INTERVAL)      # 호출 제한에 걸리면 한 번만 다시 시도
            data = http_get(build_url(spec.query, d_from, d_to), HOSTS)
        articles = parse(data, spec)
    except FetchError as e:
        hint = " (GDELT 호출 제한 — 잠시 뒤 다시 수집)" if "429" in str(e) or "limit requests" in str(e) else ""
        return SearchResult(spec, "failed", error=str(e) + hint)
    except json.JSONDecodeError as e:
        return SearchResult(spec, "failed", error=f"응답 형식 오류: {e}")
    finally:
        _last_call = time.monotonic()
    return SearchResult(spec, "ok" if articles else "empty", articles)
