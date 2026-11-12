"""화면에 보일 기사를 한국어로 요약하고 같은 사건끼리 묶는다. 수집 직후와 요약 버튼이 함께 쓴다."""
from __future__ import annotations

from . import llm, pipeline

MAX_ROUNDS = 2
MAX_SUMMARIES = 40


def enrich_view(conn, d_from: str, d_to: str, filters: dict, show_all: bool, demo: bool = False, client=None) -> dict:
    """보이는 목록을 요약 → 같은 사건 묶기 → 다시 뽑기. 묶여서 빈 자리에 올라온 기사도 한 번 더 처리한다."""
    total = {"ok": 0, "failed": 0, "merged": 0, "remaining": 0, "unconfigured": 0, "first_error": None}
    tried: set[int] = set()          # 이번 실행에서 실패한 기사를 같은 실행 안에서 다시 부르지 않는다
    for _ in range(MAX_ROUNDS):
        shown, _, _ = pipeline.dashboard_list(conn, d_from, d_to, filters, show_all, demo)
        budget = min(llm.MAX_PER_BATCH, MAX_SUMMARIES - total["ok"] - total["failed"])
        todo = [a for a in shown if a["id"] not in tried]
        tried.update(a["id"] for a in todo if a["summary_status"] != "ok")
        counts = llm.summarize_batch(conn, todo, client, limit=budget)
        for k in ("ok", "failed", "unconfigured"):
            total[k] += counts[k]
        total["remaining"] = counts["remaining"]
        total["first_error"] = total["first_error"] or counts["first_error"]
        if counts["unconfigured"]:
            break
        pipeline.recompute(conn, demo)
        shown, _, _ = pipeline.dashboard_list(conn, d_from, d_to, filters, show_all, demo)
        grouped = llm.group_same_events(conn, shown, client)
        total["merged"] += grouped["merged"]
        total["first_error"] = total["first_error"] or grouped["error"]
        pipeline.recompute(conn, demo)
        if not counts["ok"] and not grouped["merged"]:
            break
    return total


def message(total: dict) -> str:
    if total["unconfigured"]:
        return "요약 설정 필요: ANTHROPIC_API_KEY 가 없습니다"
    parts = [f"한국어 요약 {total['ok']}건"]
    if total["failed"]:
        parts.append(f"실패 {total['failed']}건")
    if total["merged"]:
        parts.append(f"같은 사건으로 묶음 {total['merged']}건")
    if total["remaining"]:
        parts.append(f"남은 미요약 {total['remaining']}건")
    if total["first_error"]:
        parts.append(f"오류: {total['first_error']}")
    return " · ".join(parts)
