"""수집 실행 → 저장 → 사건 묶기 → 점수 계산. 조회용 목록 구성도 여기서 한다."""
from __future__ import annotations

import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from . import db, dedupe, ranking
from .collect import gdelt, google_news
from .collect.base import SearchResult, SearchSpec
from .config import topics_config
from .timeutil import in_period, iso, now_utc

STATUS_TEXT = {
    "ok": "수집 성공",
    "partial": "일부 출처 실패 — 성공한 결과만 반영",
    "failed": "수집 실패 — 모든 출처에서 응답을 받지 못함",
    "empty": "빈 결과 — 검색은 성공했으나 자료 없음",
    "unconfigured": "검색 미설정 — config/topics.yaml 에 검색어가 없음",
    "running": "수집 중",
}


def build_specs(keyword: str = "", cfg: dict | None = None) -> list[SearchSpec]:
    cfg = cfg or topics_config()
    specs = [SearchSpec("google_news", s["q"], s.get("lang", "en"), s.get("topic")) for s in cfg.get("searches") or []]
    specs += [SearchSpec("gdelt", s["q"], "", s.get("topic")) for s in cfg.get("gdelt_searches") or []]
    if keyword.strip():
        specs += [SearchSpec("google_news", keyword.strip(), lang) for lang in ("ko", "en", "ar")]
    return specs


def _default_search(spec: SearchSpec, d_from: str, d_to: str) -> SearchResult:
    if spec.provider == "gdelt":
        return gdelt.search(spec, d_from, d_to)
    return google_news.search(spec, d_from, d_to)


def run_collection(conn, d_from: str, d_to: str, keyword: str = "", specs=None, search=_default_search) -> int:
    """수집을 한 번 실행하고 run id 를 돌려준다. 일부 출처가 실패해도 성공한 결과는 저장한다."""
    specs = build_specs(keyword) if specs is None else specs
    cur = conn.execute("INSERT INTO runs (started_at, date_from, date_to, keyword) VALUES (?,?,?,?)",
                       (iso(now_utc()), d_from, d_to, keyword))
    run_id = cur.lastrowid
    if not specs:
        conn.execute("UPDATE runs SET status='unconfigured', finished_at=? WHERE id=?", (iso(now_utc()), run_id))
        conn.commit()
        return run_id

    def guarded(spec):
        try:
            return search(spec, d_from, d_to)
        except Exception as e:  # 제공자 하나의 예외가 전체 수집을 멈추지 않게 한다
            return SearchResult(spec, "failed", error=f"{type(e).__name__}: {e}")

    parallel = [s for s in specs if s.provider != "gdelt"]
    serial = [s for s in specs if s.provider == "gdelt"]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(guarded, parallel))
    results += [guarded(s) for s in serial]  # GDELT 는 호출 간격 제한 때문에 순서대로

    total_new = 0
    for r in results:
        new = 0
        for a in r.articles:
            _, created = db.upsert_article(conn, a)
            new += created
        total_new += new
        conn.execute(
            "INSERT INTO run_sources (run_id, provider, label, topic, status, count, new_count, error) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, r.spec.provider, r.spec.label, r.spec.topic, r.status, len(r.articles), new, r.error))
    failed = sum(r.status == "failed" for r in results)
    found = sum(len(r.articles) for r in results)
    status = "failed" if failed == len(results) else "partial" if failed else "empty" if not found else "ok"
    conn.execute("UPDATE runs SET status=?, finished_at=?, new_count=? WHERE id=?",
                 (status, iso(now_utc()), total_new, run_id))
    recompute(conn)
    conn.commit()
    return run_id


def recompute(conn, demo: bool = False) -> None:
    """새 기사를 사건에 배정하고 전체 점수·이유를 다시 계산한다. 사용자 필드는 건드리지 않는다."""
    articles = db.all_articles(conn, demo)
    events = dedupe.assign_events(articles)
    groups: dict[int, list[dict]] = defaultdict(list)
    for a in articles:
        a["event_id"] = events[a["id"]]
        groups[a["event_id"]].append(a)
    for a in articles:
        n = dedupe.independent_sources(groups[a["event_id"]])
        a["llm_category"] = a["category"] if a["summary_status"] == "ok" else None
        s = ranking.score_article(a, n)
        conn.execute("UPDATE articles SET event_id=?, topics=?, category=?, region=?, score=?, reasons=? WHERE id=?",
                     (a["event_id"], json.dumps(s["topics"]), s["category"], s["region"], s["score"],
                      json.dumps(s["reasons"], ensure_ascii=False), a["id"]))


def last_run(conn) -> dict | None:
    row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    run = dict(row)
    run["sources"] = [dict(r) for r in conn.execute("SELECT * FROM run_sources WHERE run_id=? ORDER BY id", (run["id"],))]
    run["status_text"] = STATUS_TEXT.get(run["status"], run["status"])
    ok = conn.execute("SELECT finished_at FROM runs WHERE status IN ('ok','partial','empty') ORDER BY id DESC LIMIT 1").fetchone()
    run["last_success_at"] = ok["finished_at"] if ok else None
    return run


def list_articles(conn, d_from: str, d_to: str, *, keyword: str = "", category: str = "", region: str = "",
                  topic: str = "", selected_only: bool = False, include_undated: bool = True,
                  demo: bool = False) -> list[dict]:
    """필터를 적용한 추천 목록. 같은 사건의 기사는 대표 기사 아래 related 로 묶되 모두 보존한다."""
    articles = db.all_articles(conn, demo)
    groups: dict[int, list[dict]] = defaultdict(list)
    for a in articles:
        groups[a["event_id"] or a["id"]].append(a)
    kw = keyword.strip().lower()
    cfg = topics_config()
    out = []
    for a in articles:
        within = in_period(a["published_at"], a["published_date"], a["time_precision"], d_from, d_to)
        if within is False or (within is None and not include_undated):
            continue
        if ranking.is_blocked_source(a, cfg):
            continue
        if category and a["category"] != category:
            continue
        if region and a["region"] != region:
            continue
        if topic and topic not in a["topics"] and topic not in a["search_topics"]:
            continue
        if selected_only and not a["selected"]:
            continue
        if kw and kw not in " ".join(filter(None, [a["title"], a["ko_title"], a["snippet"], a["ko_summary"]])).lower():
            continue
        group = groups[a["event_id"] or a["id"]]
        a["n_sources"] = dedupe.independent_sources(group)
        a["verification"] = ranking.verification(a, a["n_sources"])
        a["undated"] = within is None
        out.append(a)
    # 관심 주제 탭에서는 그 주제를 직접 다룬 기사를 먼저, 검색에만 걸린 기사를 뒤에 둔다.
    out.sort(key=lambda a: (bool(topic) and topic not in a["topics"], ranking.sort_key(a)))
    # 사건별로 가장 점수가 높은 기사를 대표로 세우고 나머지는 related 로 붙인다.
    shown: dict[int, dict] = {}
    result = []
    for a in out:
        key = a["event_id"] or a["id"]
        if key in shown:
            shown[key]["related"].append(a)
        else:
            a["related"] = []
            shown[key] = a
            result.append(a)
    return result


def period_scale(d_from: str, d_to: str, cfg: dict | None = None) -> float:
    """기간이 길면 추천 수도 늘린다. 기준은 이틀에 limit 건이고, 최대 max_scale 배까지."""
    from datetime import date
    rule = (cfg or topics_config()).get("recommend") or {}
    days = (date.fromisoformat(d_to) - date.fromisoformat(d_from)).days + 1
    return min(float(rule.get("max_scale", 3)), max(1.0, days / 2))


def pick_recommended(events: list[dict], cfg: dict | None = None, scale: float = 1.0) -> list[dict]:
    """점수순 사건 목록에서 첫 화면에 보일 것만 고른다. 보고서 구성대로 구역별 정원을 채운다.

    1) 관심 주제를 제목에서 직접 다룬 사건(주제별 상한 적용)  2) 구역별 정원만큼 점수순
    3) 남는 자리는 구역 구분 없이 점수순  4) 사용자가 선택한 사건은 제한과 무관하게 포함
    """
    cfg = cfg or topics_config()
    rule = cfg.get("recommend") or {}
    limit = round(int(rule.get("limit", 20)) * scale)
    floor = float(rule.get("min_score", 0))
    caps = {k: round(v * scale) for k, v in (rule.get("must_cap") or {}).items()}
    picked: dict[int, dict] = {}
    over_cap: set[int] = set()                           # 주제 상한을 넘은 사건: 다른 구역 자리도 차지하지 않게 한다

    def take(a: dict, reason: str) -> None:
        a["pick_reason"] = reason
        picked[a["id"]] = a

    used: dict[str, int] = defaultdict(int)
    for a in events:                                     # events 는 이미 점수순
        direct = ranking.direct_topics(a, cfg)
        if not direct:
            continue
        free = [t for t in direct if t not in caps]      # 상한이 없는 주제(비스마야 등)는 항상 포함
        open_ = [t for t in direct if t in caps and used[t] < caps[t]]
        if free or open_:
            for t in direct:
                used[t] += 1
            take(a, "관심 주제 직접 보도")
        else:
            over_cap.add(a["id"])
    theme_cap = round(int(rule.get("theme_cap", 0)) * scale)
    for section, quota in (rule.get("quota") or {}).items():
        pool = [a for a in events if a["id"] not in picked and a["id"] not in over_cap
                and a["score"] >= floor and ranking.section_of(a, cfg) == section]
        seen: dict[str, int] = defaultdict(int)
        taken = 0
        for a in pool:
            if taken >= round(int(quota) * scale):
                break
            capped = {t["id"]: round(int(t["cap"]) * scale) if "cap" in t else theme_cap for t in ranking.match_themes(a, cfg)
                      if "cap" in t or "floor" not in t}
            if any(cap and seen[t] >= cap for t, cap in capped.items()):
                continue                                 # 같은 사안이 이미 충분히 들어갔다
            for t in capped:
                seen[t] += 1
            take(a, f"{section} 구역 상위")          # 정원은 limit 을 넘더라도 채운다
            taken += 1
    for skip_over_cap in (True, False):                  # 남는 자리: 먼저 일반 사건, 그래도 남으면 상한 초과분
        for a in events:
            if len(picked) >= limit:
                break
            if a["id"] not in picked and not (skip_over_cap and a["id"] in over_cap):
                take(a, "점수 상위")
    for a in events:                                     # 선택한 기사는 제한과 무관하게 남긴다
        if a["id"] not in picked and (a["selected"] or any(r["selected"] for r in a["related"])):
            take(a, "선택한 기사")
    return sorted(picked.values(), key=ranking.sort_key)


NARROWING = ("q", "category", "region", "topic", "selected")


def dashboard_list(conn, d_from: str, d_to: str, f: dict, show_all: bool, demo: bool = False) -> tuple[list[dict], int, bool]:
    """대시보드에 보일 목록. 반환: (보일 사건, 조건에 맞는 전체 사건 수, 추천만 추렸는지).

    사용자가 탭·필터·검색으로 직접 좁힌 경우에는 추리지 않는다(그 조건에서 빠지는 기사가 없어야 한다).
    """
    events = list_articles(conn, d_from, d_to, keyword=f.get("q", ""), category=f.get("category", ""),
                           region=f.get("region", ""), topic=f.get("topic", ""),
                           selected_only=bool(f.get("selected")), include_undated=f.get("undated", "1") == "1",
                           demo=demo)
    if show_all or any(f.get(k) for k in NARROWING):
        return events, len(events), False
    shown = pick_recommended(events, scale=period_scale(d_from, d_to))
    return shown, len(events), len(shown) < len(events)


def splits(conn) -> set[tuple[int, int]]:
    return {(r["a"], r["b"]) for r in conn.execute("SELECT a, b FROM event_splits")}


def merge_events(conn, article_ids: list[int], note: str) -> int:
    """기사들이 속한 사건을 하나로 합친다. 원본 기사는 그대로 두고 사건 번호만 바꾼다. 반환: 합쳐 없어진 사건 수."""
    rows = {r["id"]: r["event_id"] or r["id"] for r in conn.execute(
        f"SELECT id, event_id FROM articles WHERE id IN ({','.join('?' * len(article_ids))})", article_ids)}
    events = list(dict.fromkeys(rows.values()))
    if len(events) < 2:
        return 0
    target = min(events)
    for e in events:
        if e != target:
            conn.execute("UPDATE articles SET event_id=?, group_note=? WHERE event_id=? OR id=?", (target, note, e, e))
    return len(events) - 1


def split_article(conn, article_id: int) -> None:
    """기사 하나를 묶음에서 떼어 독립 사건으로 만들고, 자동 묶기가 다시 합치지 않게 기록한다."""
    row = conn.execute("SELECT event_id FROM articles WHERE id=?", (article_id,)).fetchone()
    event = row["event_id"] or article_id
    others = [r["id"] for r in conn.execute("SELECT id FROM articles WHERE event_id=? AND id<>?", (event, article_id))]
    if event == article_id and others:           # 대표 기사를 떼면 남은 기사들에 새 사건 번호를 준다
        conn.execute("UPDATE articles SET event_id=? WHERE event_id=? AND id<>?", (min(others), event, article_id))
    conn.execute("UPDATE articles SET event_id=?, group_note=NULL WHERE id=?", (article_id, article_id))
    for o in others:
        conn.execute("INSERT OR IGNORE INTO event_splits (a, b) VALUES (?,?)", (min(o, article_id), max(o, article_id)))


SECTION_ORDER = [ranking.FOCUS, "정치", "치안 · 테러", "경제", "국제", "기타"]


def sections(events: list[dict], cfg: dict | None = None) -> list[tuple[str, list[dict]]]:
    """보고서처럼 구역별로 나눈다. 구역 판정은 추천 선별과 같은 규칙(ranking.section_of)을 쓴다."""
    cfg = cfg or topics_config()
    by: dict[str, list[dict]] = defaultdict(list)
    for a in events:
        by[ranking.section_of(a, cfg)].append(a)
    return [(name, by[name]) for name in SECTION_ORDER if by[name]]
