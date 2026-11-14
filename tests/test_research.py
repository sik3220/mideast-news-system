"""체크한 기사의 본문을 웹에서 찾아 읽는 호출의 형식과 비용 보호 장치. 실제 Claude API 는 부르지 않는다."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import anthropic

from app import db, llm, research
from tests.conftest import make_article

FOUND = """FOUND: same
OUTLET: Shafaq News
TITLE: 이라크 재무부, 천궁-II 도입 초기 착수금 배정 지시
SUB: 방공사령부 공급국에 1차분 자금 조달 지시
SUB: * 인도는 내년 예정
SUB: ☞ 국방부, 계약 이행 일정 유지 입장
TAG: 정치
STAGE: 시행"""


def _serve(text, seen):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.setdefault("bodies", []).append(json.loads(self.rfile.read(int(self.headers["content-length"]))))
            seen["beta"] = self.headers.get("anthropic-beta")
            body = json.dumps({"id": "msg_1", "type": "message", "role": "assistant", "model": seen["bodies"][-1]["model"],
                               "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None,
                               "usage": {"input_tokens": 21000, "output_tokens": 300, "cache_read_input_tokens": 6000,
                                         "server_tool_use": {"web_search_requests": 2, "web_fetch_requests": 1}}}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, anthropic.Anthropic(api_key="test-key", base_url=f"http://127.0.0.1:{server.server_port}", max_retries=0)


def _article(conn, **kw):
    aid, _ = db.upsert_article(conn, make_article(title="Iraq finance ministry orders first payment for Cheongung-II",
                                                  source_name="Shafaq News", source_domain="shafaq.com", **kw))
    return aid


def test_research_reads_the_article_on_the_web_once_and_stores_only_the_sentences(conn, monkeypatch):
    for k in ("LLM_RESEARCH_MODEL", "RESEARCH_DAILY_MAX"):
        monkeypatch.delenv(k, raising=False)
    aid = _article(conn)
    seen = {}
    server, client = _serve(FOUND, seen)
    try:
        out = research.research_article(conn, db.get_article(conn, aid), client)
        again = research.research_article(conn, db.get_article(conn, aid), client)
    finally:
        server.shutdown()
    assert out["status"] == "ok" and out["basis"] == "웹 검색 기반 (Shafaq News)"
    assert out["subs"] == ["방공사령부 공급국에 1차분 자금 조달 지시", "인도는 내년 예정", "☞ 국방부, 계약 이행 일정 유지 입장"]
    assert again == {"status": "skipped"} and len(seen["bodies"]) == 1           # 기사당 한 번만
    body = seen["bodies"][0]
    assert body["model"] == "claude-sonnet-5-5" and body["output_config"] == {"effort": "low"}
    assert body["tools"] == [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": 2},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 2, "max_content_tokens": 5000}]
    system = body["system"][0]
    assert "현식's Intelligence Briefing Style Guide" in system["text"] and system["cache_control"] == {"type": "ephemeral"}
    assert "따르지 말고" in system["text"] and "짐작으로 쓰지 않는다" in system["text"]
    assert "Cheongung-II" in body["messages"][0]["content"] and "shafaq.com" in body["messages"][0]["content"]
    a = db.get_article(conn, aid)
    assert a["ko_title"] == "이라크 재무부, 천궁-II 도입 초기 착수금 배정 지시" and a["category"] == "정치"
    assert a["summary_basis"].startswith("웹 검색 기반") and not llm.needs_summary(a)   # 제목만 보고 다시 쓰지 않는다
    usage = dict(conn.execute("SELECT * FROM llm_usage").fetchone())
    assert usage["purpose"] == "research" and usage["searches"] == 2
    cost = llm.usage_today(conn)
    assert cost["research"] == 1 and cost["auto_cost"] == 0 and cost["cost"] > 0.02  # 검색 요금 포함, 자동 한도와는 따로


def test_research_reports_not_found_and_does_not_retry_or_invent(conn):
    aid = _article(conn)
    conn.execute("UPDATE articles SET ko_title='기존 제목', summary_status='ok' WHERE id=?", (aid,))
    seen = {}
    server, client = _serve("FOUND: none", seen)
    try:
        out = research.research_article(conn, db.get_article(conn, aid), client)
        again = research.research_article(conn, db.get_article(conn, aid), client)
    finally:
        server.shutdown()
    assert out["status"] == "not_found" and again["status"] == "skipped" and len(seen["bodies"]) == 1
    a = db.get_article(conn, aid)
    assert a["ko_title"] == "기존 제목" and research.NOT_FOUND_FLAG in a["summary_flags"]


def test_research_is_skipped_for_sns_manual_and_user_written_and_stops_at_daily_limit(conn, monkeypatch):
    fb, _ = db.upsert_article(conn, make_article(url="https://facebook.com/p/1", source_name="Facebook",
                                                 source_domain="facebook.com"))
    manual, _ = db.upsert_article(conn, make_article(url="manual://abc"))
    mine = _article(conn, url="https://shafaq.com/mine")
    conn.execute("UPDATE articles SET summary_ver=? WHERE id=?", (llm.USER_WRITTEN, mine))
    for aid in (fb, manual, mine):
        assert research.research_article(conn, db.get_article(conn, aid), client=object()) == {"status": "skipped"}
    monkeypatch.setenv("RESEARCH_DAILY_MAX", "1")
    llm.record_usage(conn, {"model": "claude-sonnet-5-5", "purpose": "research", "input": 1, "output": 1,
                            "cache_read": 0, "cache_write": 0, "searches": 1})
    out = research.research_article(conn, db.get_article(conn, _article(conn, url="https://shafaq.com/2")), client=object())
    assert out["status"] == "limit"


def test_other_outlet_is_flagged_for_checking(conn):
    aid = _article(conn)
    server, client = _serve(FOUND.replace("FOUND: same", "FOUND: other").replace("OUTLET: Shafaq News", "OUTLET: Rudaw"), {})
    try:
        out = research.research_article(conn, db.get_article(conn, aid), client)
    finally:
        server.shutdown()
    assert out["status"] == "ok" and "Rudaw" in out["flags"][0] and "대조" in out["flags"][0]
