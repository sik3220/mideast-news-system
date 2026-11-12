"""Google 뉴스 RSS 검색. 키가 필요 없다. 제목·매체·발표 시각·링크만 제공하고 본문 요약은 없다."""
from __future__ import annotations

import html
import re
from datetime import date, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlsplit

from defusedxml import ElementTree

from ..dedupe import normalize_url, strip_source_suffix
from ..timeutil import baghdad_date, iso
from .base import FetchError, SearchResult, SearchSpec, safe_get

HOSTS = {"news.google.com"}
EDITIONS = {
    "ko": "hl=ko&gl=KR&ceid=KR:ko",
    "en": "hl=en-US&gl=US&ceid=US:en",
    "ar": "hl=ar&gl=EG&ceid=EG:ar",
}
_TAGS = re.compile(r"<[^>]+>")


def build_url(query: str, lang: str, d_from: str, d_to: str) -> str:
    # after/before 는 날짜 단위이고 시간대가 불명확하므로 하루씩 넓게 요청한 뒤 저장 단계에서 정확히 거른다.
    after = (date.fromisoformat(d_from) - timedelta(days=1)).isoformat()
    before = (date.fromisoformat(d_to) + timedelta(days=2)).isoformat()
    q = f"{query} after:{after} before:{before}"
    return f"https://news.google.com/rss/search?q={quote(q)}&{EDITIONS.get(lang, EDITIONS['en'])}"


def parse(xml_bytes: bytes, spec: SearchSpec) -> list[dict]:
    root = ElementTree.fromstring(xml_bytes)
    out = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        raw_title = html.unescape((item.findtext("title") or "").strip())
        if not link or not raw_title:
            continue
        src = item.find("source")
        source_name = (src.text or "").strip() if src is not None else ""
        source_url = src.get("url", "") if src is not None else ""
        title = strip_source_suffix(raw_title, source_name)
        # description 은 대개 제목과 매체명만 담고 있다. 제목과 다른 내용이 있을 때만 요약문으로 쓴다.
        desc = html.unescape(_TAGS.sub(" ", html.unescape(item.findtext("description") or "")))
        desc = re.sub(r"\s+", " ", desc.replace("\xa0", " ")).strip()
        snippet = "" if desc.startswith(title[:30]) else desc[:500]
        article = {
            "url": link, "url_norm": normalize_url(link), "title": title, "snippet": snippet,
            "source_name": source_name, "source_domain": (urlsplit(source_url).hostname or "").removeprefix("www."),
            "lang": spec.lang, "provider": "google_news",
            "search_topics": [spec.topic] if spec.topic else [],
            "published_at": None, "published_date": None, "time_precision": "unknown",
        }
        pub = item.findtext("pubDate")
        if pub:
            try:
                dt = parsedate_to_datetime(pub)
                article.update(published_at=iso(dt), published_date=baghdad_date(dt), time_precision="datetime")
            except (TypeError, ValueError):
                pass
        out.append(article)
    return out


def search(spec: SearchSpec, d_from: str, d_to: str, http_get=safe_get) -> SearchResult:
    try:
        data = http_get(build_url(spec.query, spec.lang, d_from, d_to), HOSTS)
        articles = parse(data, spec)
    except FetchError as e:
        return SearchResult(spec, "failed", error=str(e))
    except ElementTree.ParseError as e:
        return SearchResult(spec, "failed", error=f"응답 형식 오류: {e}")
    return SearchResult(spec, "ok" if articles else "empty", articles)
