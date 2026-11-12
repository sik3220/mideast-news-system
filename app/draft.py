"""선택한 기사로 한국어 보고서 초안을 조립한다. 각 항목은 실제 선택한 기사와 연결된다.

초안은 규칙으로 조립한다(LLM 을 다시 부르지 않는다). 한국어 요약이 없는 기사는 원문 제목을 그대로 쓰고 표시한다.
"""
from __future__ import annotations

import json
from datetime import date

from .timeutil import iso, md, now_utc

CATEGORY_ORDER = ["정치", "치안", "테러", "경제", "국제", "기타"]
NOT_FOUND = "선택 기간·성공적으로 검색한 출처에서 신규 주요 자료를 찾지 못함"


def _published(a: dict) -> str:
    if a.get("published_date"):
        suffix = "" if a["time_precision"] == "datetime" else " 관측" if a["time_precision"] == "observed" else ""
        return md(a["published_date"]) + suffix
    return "발표일 미상"


def unverified_notes(a: dict) -> list[str]:
    notes = []
    if a.get("n_sources", 1) < 2:
        notes.append("단일 출처")
    notes.append("제목·검색 요약 기반" if a.get("access_level") == "snippet" else
                 "본문 기반" if a.get("access_level") == "fulltext" else "제목 기반")
    if a.get("claim_type") in ("당사자 주장", "발언 인용"):
        notes.append(a["claim_type"])
    notes += a.get("summary_flags", [])
    return notes


def item_lines(a: dict) -> list[str]:
    head = a.get("ko_title") or a["title"]
    day = md(a["published_date"]) if a.get("published_date") else "날짜 미상"
    source = a.get("source_name") or a.get("source_domain") or "출처 미상"
    lines = [f"- {day}, {head} ({source}, {_published(a)})"]
    if a.get("ko_summary"):
        lines.append(f"  * {a['ko_summary']}")
    elif not a.get("ko_title"):
        lines.append("  * (한국어 요약 없음 — 원문 제목 그대로)")
    notes = unverified_notes(a)
    if notes and (a.get("n_sources", 1) < 2 or a.get("summary_flags")):
        lines.append(f"  * [미확인] {' / '.join(notes)}")
    else:
        lines.append(f"  * [근거] {' / '.join(notes)}")
    lines.append(f"  원문: {a['url']}")
    return lines


def build_report(articles: list[dict], d_from: str, d_to: str, failed_sources: list[str] | None = None) -> str:
    """브리프의 공통 초안 형식. articles 는 선택한 기사(n_sources 포함)."""
    today = date.today()
    out = [f"이라크·중동 동향 | {today.year}.{today.month:02d}.{today.day:02d}",
           f"대상 기간: {d_from} ~ {d_to} (바그다드 시간)", "",
           "핵심 요약: (직접 작성)", ""]
    for cat in CATEGORY_ORDER:
        items = [a for a in articles if (a.get("category") or "기타") == cat]
        if not items:
            continue
        out.append(f"[{cat}]")
        for a in items:
            out += item_lines(a)
        out.append("")
    out.append("비스마야·한화·NIC")
    bis = [a for a in articles if "bismayah" in a.get("topics", [])]
    if bis:
        out += [f"- {a.get('ko_title') or a['title']} (위 [{a.get('category') or '기타'}] 항목 참조)" for a in bis]
    else:
        scope = NOT_FOUND
        if failed_sources:
            scope += f" (검색 실패 출처 {len(failed_sources)}건은 범위에서 제외됨)"
        out.append(f"- {scope}")
    out.append("")
    out.append("후속 확인사항")
    followups = [f for a in articles for f in a.get("followups", [])]
    out += [f"- {f}" for f in followups] or ["- (직접 작성)"]
    return "\n".join(out).rstrip() + "\n"


def load(conn, key: str) -> dict | None:
    row = conn.execute("SELECT * FROM drafts WHERE key=?", (key,)).fetchone()
    return dict(row) | {"article_ids": json.loads(row["article_ids"])} if row else None


def save(conn, key: str, content: str, article_ids: list[int] | None = None, reason: str = "저장") -> None:
    """저장 전의 내용은 버전으로 남긴다. 재생성으로 사용자의 편집이 사라지지 않게 한다."""
    old = load(conn, key)
    now = iso(now_utc())
    if old and old["content"] != content:
        conn.execute("INSERT INTO draft_versions (key, content, saved_at, reason) VALUES (?,?,?,?)",
                     (key, old["content"], now, reason))
    ids = json.dumps(article_ids if article_ids is not None else (old["article_ids"] if old else []))
    conn.execute(
        """INSERT INTO drafts (key, content, article_ids, updated_at) VALUES (?,?,?,?)
           ON CONFLICT(key) DO UPDATE SET content=excluded.content, article_ids=excluded.article_ids,
             updated_at=excluded.updated_at""", (key, content, ids, now))


def versions(conn, key: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM draft_versions WHERE key=? ORDER BY id DESC LIMIT 20", (key,))]
