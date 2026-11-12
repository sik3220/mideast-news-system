"""URL 중복 제거와 동일 사건 묶기. 두 가지는 별개다.

- URL 중복: 정규화한 URL 이 같으면 같은 레코드다(저장 단계에서 처리).
- 사건 묶기: 같은 언어에서 제목이 매우 비슷하고 날짜가 가까운 기사를 한 사건으로 묶는다.
  보수적으로 묶고 원본 레코드는 지우지 않는다. 후속 발표는 제목이 달라 별도 사건으로 남는다.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PREFIXES = ("utm_",)
TRACKING_KEYS = {"fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "ref_src", "oc", "ocid", "cmpid",
                 "spm", "yclid", "msclkid", "_hsenc", "_hsmi"}


def normalize_url(url: str) -> str:
    """추적 파라미터만 제거한다. 기사 식별에 쓰일 수 있는 나머지 쿼리는 순서까지 그대로 둔다."""
    p = urlsplit(url.strip())
    host = (p.hostname or "").lower()
    if p.port and p.port not in (80, 443):
        host = f"{host}:{p.port}"
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if k.lower() not in TRACKING_KEYS and not k.lower().startswith(TRACKING_PREFIXES)]
    path = p.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return urlunsplit((p.scheme.lower() or "https", host, path, urlencode(query), ""))


def title_key(title: str) -> str:
    """비교용 제목: 소문자, 문장부호·공백 제거."""
    t = unicodedata.normalize("NFKC", title).lower()
    return "".join(ch for ch in t if ch.isalnum())


def _trigrams(s: str) -> set[str]:
    return {s[i:i + 3] for i in range(len(s) - 2)} if len(s) >= 3 else {s}


def title_similarity(a: str, b: str) -> float:
    ta, tb = _trigrams(title_key(a)), _trigrams(title_key(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


SIMILARITY_THRESHOLD = 0.6
MAX_DAY_GAP = 2


def _day(a: dict) -> date | None:
    d = a.get("published_date")
    return date.fromisoformat(d) if d else None


def same_event(a: dict, b: dict) -> bool:
    if a.get("lang") != b.get("lang"):
        return False
    da, db = _day(a), _day(b)
    if da and db and abs((da - db).days) > MAX_DAY_GAP:
        return False
    return title_similarity(a["title"], b["title"]) >= SIMILARITY_THRESHOLD


def assign_events(articles: list[dict]) -> dict[int, int]:
    """기사 id -> 사건 id. 이미 사건이 정해진 기사는 그대로 두고, 새 기사만 기존 사건에 붙이거나 새 사건을 만든다.

    사건 id 는 그 사건에 처음 들어온 기사의 id 를 쓴다.
    """
    result: dict[int, int] = {}
    reps: list[dict] = []  # 사건 대표 기사
    ordered = sorted(articles, key=lambda a: (a.get("event_id") is None, a["id"]))
    for a in ordered:
        if a.get("event_id") is not None:
            result[a["id"]] = a["event_id"]
            if a["event_id"] == a["id"]:
                reps.append(a)
            continue
        match = next((r for r in reps if same_event(r, a)), None)
        if match:
            result[a["id"]] = result[match["id"]]
        else:
            result[a["id"]] = a["id"]
            reps.append(a)
    return result


def independent_sources(group: list[dict]) -> int:
    """한 사건을 보도한 독립 매체 수. 제목이 사실상 같은 재전재는 한 곳으로 센다."""
    seen_titles: list[str] = []
    domains: set[str] = set()
    for a in sorted(group, key=lambda x: x["id"]):
        key = title_key(a["title"])
        if any(key == t for t in seen_titles):
            continue
        seen_titles.append(key)
        domains.add(a.get("source_domain") or a.get("source_name") or str(a["id"]))
    return len(domains)


_SOURCE_SUFFIX = re.compile(r"\s+[-–—|]\s+[^-–—|]{1,40}$")


def strip_source_suffix(title: str, source_name: str) -> str:
    """Google 뉴스 제목 끝의 ' - 매체명' 을 떼어낸다."""
    if source_name and title.endswith(source_name):
        return _SOURCE_SUFFIX.sub("", title).strip() or title
    return title
