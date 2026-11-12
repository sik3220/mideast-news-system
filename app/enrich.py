"""화면에 보일 기사를 한국어로 요약하고 같은 사건끼리 묶는다. 수집 직후와 요약 버튼이 함께 쓴다."""
from __future__ import annotations

from . import llm, pipeline

MAX_ROUNDS = 40          # 번역으로 분류가 바뀌면 추천 구성이 조금씩 달라지므로 여유 있게 둔다. 실제 상한은 MAX_SUMMARIES
MAX_SUMMARIES = 80


def enrich_view(conn, d_from: str, d_to: str, filters: dict, show_all: bool, demo: bool = False, client=None) -> dict:
    """보이는 목록이 전부 번역되고 더 묶을 것이 없을 때까지 반복한다.

    번역으로 분류가 바뀌거나 묶기로 자리가 비면 새 기사가 목록에 올라오므로, 한 번으로 끝나지 않는다.
    보이는 기사가 모두 번역된 뒤에만 묶기를 실행한다.
    """
    total = {"ok": 0, "failed": 0, "merged": 0, "remaining": 0, "unconfigured": 0, "first_error": None}
    tried: set[int] = set()          # 이번 실행에서 실패한 기사를 같은 실행 안에서 다시 부르지 않는다
    group_done = False
    for _ in range(MAX_ROUNDS):
        shown, _, _ = pipeline.dashboard_list(conn, d_from, d_to, filters, show_all, demo)
        budget = min(llm.MAX_PER_BATCH, MAX_SUMMARIES - total["ok"] - total["failed"])
        todo = [a for a in shown if a["id"] not in tried and llm.needs_summary(a)][:max(0, budget)]
        tried.update(a["id"] for a in todo)       # 실제로 보낸 기사만 '시도함'으로 표시한다
        counts = llm.summarize_batch(conn, todo, client, limit=budget)
        for k in ("ok", "failed", "unconfigured"):
            total[k] += counts[k]
        total["first_error"] = total["first_error"] or counts["first_error"]
        if counts["unconfigured"]:
            break
        if counts["ok"]:
            pipeline.recompute(conn, demo)
            group_done = False
            continue                 # 분류가 바뀌어 새로 올라온 기사가 있을 수 있다
        if group_done:
            break
        grouped = llm.group_same_events(conn, shown, client)
        total["merged"] += grouped["merged"]
        total["first_error"] = total["first_error"] or grouped["error"]
        group_done = True
        if not grouped["merged"]:
            break
        pipeline.recompute(conn, demo)
        group_done = False
    shown, _, _ = pipeline.dashboard_list(conn, d_from, d_to, filters, show_all, demo)
    total["remaining"] = sum(llm.needs_summary(a) for a in shown)
    return total


def message(total: dict) -> str:
    if total["unconfigured"]:
        return "요약 설정 필요: ANTHROPIC_API_KEY 가 없습니다"
    parts = [f"번역 {total['ok']}건"]
    if total["failed"]:
        parts.append(f"실패 {total['failed']}건")
    if total["merged"]:
        parts.append(f"중복 묶음 {total['merged']}건")
    if total["remaining"]:
        parts.append(f"미번역 {total['remaining']}건 남음")
    if total["first_error"]:
        parts.append(f"오류: {total['first_error']}")
    return " · ".join(parts)
