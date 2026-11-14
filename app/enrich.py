"""화면에 보일 기사를 문체 규칙대로 요약하고 같은 사건끼리 묶는다. 수집 직후와 화면이 함께 쓴다."""
from __future__ import annotations

from . import llm, pipeline

MAX_SUMMARIES = llm.MAX_PER_BATCH     # 한 번 실행에서 요약하는 기사 수 상한
MAX_ROUNDS = 3                        # 요약 → 묶기 → (묶여서 빈 자리에 올라온 기사) 요약


def enrich_view(conn, d_from: str, d_to: str, filters: dict, show_all: bool, demo: bool = False, client=None,
                ignore_budget: bool = False) -> dict:
    """보이는 목록을 요약하고, 같은 사건을 묶고, 묶여서 새로 올라온 기사만 한 번 더 요약한다.

    추천 구성은 규칙으로만 정하므로 요약 자체로는 목록이 바뀌지 않는다(ranking.section_of).
    하루 추정 사용액이 한도를 넘으면 실행하지 않는다(사용자가 버튼으로 직접 누른 경우는 실행).
    """
    total = {"ok": 0, "failed": 0, "merged": 0, "remaining": 0, "unconfigured": 0, "calls": 0,
             "first_error": None, "over_budget": False}
    tried: set[int] = set()          # 이번 실행에서 이미 보낸 기사는 다시 보내지 않는다
    grouped_once = False
    for _ in range(MAX_ROUNDS):
        if not ignore_budget and client is None and llm.budget_exceeded(conn):
            total["over_budget"] = True
            break
        shown, _, _ = pipeline.dashboard_list(conn, d_from, d_to, filters, show_all, demo)
        budget = MAX_SUMMARIES - total["ok"] - total["failed"]
        todo = [a for a in shown if a["id"] not in tried and llm.needs_summary(a)][:max(0, budget)]
        tried.update(a["id"] for a in todo)
        counts = llm.summarize_batch(conn, todo, client, limit=budget)
        for k in ("ok", "failed", "unconfigured", "calls"):
            total[k] += counts[k]
        total["first_error"] = total["first_error"] or counts["first_error"]
        if counts["unconfigured"]:
            break
        if counts["ok"]:
            pipeline.recompute(conn, demo)
        if grouped_once:
            break
        shown, _, _ = pipeline.dashboard_list(conn, d_from, d_to, filters, show_all, demo)
        grouped = llm.group_same_events(conn, shown, client)
        grouped_once = True
        total["calls"] += 1 if len(shown) >= 2 else 0
        total["merged"] += grouped["merged"]
        total["first_error"] = total["first_error"] or grouped["error"]
        if not grouped["merged"]:
            break
        pipeline.recompute(conn, demo)      # 묶여서 빈 자리에 새 기사가 올라온다 → 한 번 더 요약
    shown, _, _ = pipeline.dashboard_list(conn, d_from, d_to, filters, show_all, demo)
    total["remaining"] = sum(llm.needs_summary(a) for a in shown)
    return total


def message(total: dict) -> str:
    if total["unconfigured"]:
        return "요약 설정 필요: ANTHROPIC_API_KEY 가 없습니다"
    if total["over_budget"] and not total["ok"]:
        return "오늘 Claude 사용 한도에 도달해 자동 요약을 멈췄습니다"
    parts = [f"요약 {total['ok']}건 (호출 {total['calls']}회)"]
    if total["failed"]:
        parts.append(f"실패 {total['failed']}건")
    if total["merged"]:
        parts.append(f"중복 묶음 {total['merged']}건")
    if total["remaining"]:
        parts.append(f"미요약 {total['remaining']}건 남음")
    if total["over_budget"]:
        parts.append("오늘 사용 한도 도달")
    if total["first_error"]:
        parts.append(f"오류: {total['first_error']}")
    return " · ".join(parts)
