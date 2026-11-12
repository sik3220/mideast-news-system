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
    out = []
    for a in articles:
        within = in_period(a["published_at"], a["published_date"], a["time_precision"], d_from, d_to)
        if within is False or (within is None and not include_undated):
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


def pick_recommended(events: list[dict], cfg: dict | None = None) -> list[dict]:
    """점수순 사건 목록에서 첫 화면에 보일 것만 고른다.

    관심 주제를 제목에서 직접 다룬 사건과 사용자가 선택한 사건은 항상 넣고,
    남는 자리는 분야를 돌아가며 각 분야의 상위 사건으로 채워 한 분야가 목록을 독차지하지 않게 한다.
    """
    cfg = cfg or topics_config()
    rule = cfg.get("recommend") or {}
    limit = int(rule.get("limit", 20))
    picked: dict[int, dict] = {}

    def take(a: dict, reason: str) -> None:
        a["pick_reason"] = reason
        picked[a["id"]] = a

    for a in events:                                     # events 는 이미 점수순
        direct = [t for t in rule.get("must_topics", []) if ranking.match_topic(t, a, cfg) == "title"]
        if direct and len(picked) < limit:
            take(a, "관심 주제 직접 보도")
    floor = float(rule.get("min_score", 0))
    queues = {c: [a for a in events if a["category"] == c and a["id"] not in picked and a["score"] >= floor]
              for c in rule.get("categories", [])}
    while len(picked) < limit and any(queues.values()):
        for cat, queue in queues.items():
            if queue and len(picked) < limit:
                take(queue.pop(0), f"{cat} 분야 상위")
    for a in events:                                     # 그래도 자리가 남으면 나머지에서 점수순
        if len(picked) >= limit:
            break
        if a["id"] not in picked:
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
    shown = pick_recommended(events)
    return shown, len(events), len(shown) < len(events)
