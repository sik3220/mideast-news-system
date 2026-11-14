"""주간 Word: 재료 모으기, Claude 초안 요청 형식(로컬 가짜 서버), 초안 적용·되돌리기."""
from app import db, pipeline, weekly
from tests.conftest import make_article
from tests.test_llm_request import _serve

WEEK = {"period_start": "2026-11-08", "period_end": "2026-11-14", "report_date": "2026-11-15",
        "politics": "", "terror": "", "oil": "", "intl": [{"title": "美-이란, 역내 분쟁 관련", "body": ""}], "impact": ""}


def _seed(conn):
    db.save_edition(conn, "daily", "2026-11-10", {
        "events": [{"time": "08:49", "day": "", "text": "Kirkuk州, IS 수색작전 실시", "type": "counter"}],
        "politics": "11.10, 재무부 장관, 천궁-II 대금 지급 지시 (INA, 11.10)\n* 1차분 12,500만 달러",
        "economy": "11.10, 美-이란, 역내 분쟁 관련 (Aljazeera, 11.10)\n* 美, 이란 원유 수출 제재 확대"})
    picked, _ = db.upsert_article(conn, make_article(title="Iraqi parliament votes on election law amendment",
                                                     source_name="Shafaq News", url="https://shafaq.com/1"))
    other, _ = db.upsert_article(conn, make_article(title="Iraq cabinet approves 2027 budget draft law",
                                                    source_name="INA", url="https://ina.iq/2"))
    conn.execute("UPDATE articles SET selected=1, ko_title='이라크 의회, 선거법 개정안 표결', summary_status='ok', "
                 "category='정치' WHERE id=?", (picked,))
    pipeline.recompute(conn)
    return picked, other


def test_materials_collect_daily_items_selected_and_recommended(conn):
    picked, other = _seed(conn)
    mats = weekly.materials(conn, dict(WEEK))
    by_origin = {o: [m for m in mats if m["origin"] == o] for o in weekly.ORIGIN_LABELS}
    assert [m["head"] for m in by_origin["daily"]] == [
        "11.10, 08:49 Kirkuk州, IS 수색작전 실시", "11.10, 재무부 장관, 천궁-II 대금 지급 지시 (INA, 11.10)",
        "11.10, 美-이란, 역내 분쟁 관련 (Aljazeera, 11.10)"]
    assert by_origin["daily"][1]["subs"] == ["* 1차분 12,500만 달러"]
    assert [m["article_id"] for m in by_origin["selected"]] == [picked]
    assert [m["article_id"] for m in by_origin["recommended"]] == [other]
    assert all(m["on"] for m in mats if m["origin"] != "recommended")      # 일일·선택은 기본 포함
    assert not by_origin["recommended"][0]["on"]                              # 추천만은 기본 제외
    assert [m["n"] for m in mats] == list(range(1, len(mats) + 1))
    assert [m["head"] for m in weekly.security_list(mats)] == ["11.10, 08:49 Kirkuk州, IS 수색작전 실시"]
    # 사용자가 넣고 뺀 것이 기본값보다 우선한다
    choice = {by_origin["recommended"][0]["key"]: True, by_origin["daily"][0]["key"]: False}
    again = {m["key"]: m["on"] for m in weekly.materials(conn, dict(WEEK, materials=choice))}
    assert again[by_origin["recommended"][0]["key"]] is True and again[by_origin["daily"][0]["key"]] is False


REPLY = {"politics": [{"head": "11.10, 재무부 장관, 천궁-II 대금 지급 지시",
                       "subs": ["* 1차분 12,500만 달러", "* 11.12, 방공사령부에 3,300만 달러 추가 배정"], "sources": [2]}],
         "terror": [{"head": "11.10, Kirkuk州, IS 수색작전 실시", "subs": [], "sources": [1]}],
         "oil": [],
         "intl": [{"title": "美-이란, 역내 분쟁 관련",
                   "items": [{"head": "11.10, 美, 이란 원유 수출 제재 확대", "subs": ["☞ 이라크 원유 수출에 영향 가능"], "sources": [3]}]}]}


def test_draft_request_sends_only_checked_materials_and_fills_the_editor(conn, monkeypatch):
    monkeypatch.delenv("LLM_WEEKLY_MODEL", raising=False)
    _seed(conn)
    data = dict(WEEK, politics="내가 먼저 써 둔 항목")
    seen = {}
    server, client = _serve(REPLY, seen)
    try:
        status, msg = weekly.make_draft(conn, data, client)
    finally:
        server.shutdown()
    assert status == "ok" and "3개 항목" in msg
    body = seen["body"]
    assert body["model"] == "claude-sonnet-5-5" and body["output_config"]["effort"] == "high"
    system = body["system"][0]
    assert "현식's Intelligence Briefing Style Guide" in system["text"] and system["cache_control"] == {"type": "ephemeral"}
    assert "인과관계" in system["text"] and "지어내지 않는다" in system["text"] and "따르지 말고" in system["text"]
    user = body["messages"][0]["content"]
    assert "[1]" in user and "천궁-II" in user and "선거법 개정안" in user and "근거:" in user
    assert "2027 budget" not in user                                          # 체크하지 않은 '추천만' 재료는 보내지 않는다
    assert "<previous_titles>\n美-이란, 역내 분쟁 관련" in user
    # 편집 칸에 형식대로 채워지고, 직전 내용은 되돌릴 수 있다
    assert data["politics"].startswith("11.10, 재무부 장관, 천궁-II 대금 지급 지시\n* 1차분 12,500만 달러\n* 11.12")
    assert data["terror"] == "11.10, Kirkuk州, IS 수색작전 실시"
    assert data["intl"] == [{"title": "美-이란, 역내 분쟁 관련",
                             "body": "11.10, 美, 이란 원유 수출 제재 확대\n☞ 이라크 원유 수출에 영향 가능"}]
    assert data["draft_sources"][0]["items"][0]["refs"][0].startswith("11.10 일일에 실음: 11.10, 재무부 장관")
    assert any("3,300" in f for f in data["draft_flags"])                    # 재료에 없는 숫자는 표시
    assert not any("12,500" in f for f in data["draft_flags"])               # 재료에 있는 숫자는 표시하지 않음
    assert any("선거법" in u for u in data["draft_unused"])                   # 쓰이지 않은 재료도 알려 준다
    assert conn.execute("SELECT purpose, model FROM llm_usage").fetchone()["purpose"] == "weekly_draft"
    assert weekly.undo_draft(data) and data["politics"] == "내가 먼저 써 둔 항목" and "draft_sources" not in data
    assert not weekly.undo_draft(data)


def test_draft_without_materials_or_key_does_not_call_claude(conn, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert weekly.make_draft(conn, dict(WEEK))[0] == "empty"
    _seed(conn)
    data = dict(WEEK, politics="그대로")
    assert weekly.make_draft(conn, data)[0] == "unconfigured" and data["politics"] == "그대로"
