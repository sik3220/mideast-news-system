"""브리프 11절의 확인 항목: 중복, 후속 발표 보존, 날짜 경계, 일부 실패, 선택·초안 유지, 복사본 내용."""
from types import SimpleNamespace

import pytest

from app import db, dedupe, draft, llm, pipeline, ranking, timeutil
from app.collect import gdelt, google_news
from app.collect.base import FetchError, SearchResult, SearchSpec, safe_get
from tests.conftest import make_article

FIX = __import__("pathlib").Path(__file__).parent / "fixtures"


def test_same_url_is_not_stored_twice(conn):
    id1, new1 = db.upsert_article(conn, make_article("https://example.com/a?id=7&utm_source=x"))
    id2, new2 = db.upsert_article(conn, make_article("https://EXAMPLE.com/a/?utm_medium=y&id=7#top"))
    assert (new1, new2) == (True, False) and id1 == id2
    assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 1


def test_url_normalization_keeps_identifying_query():
    assert dedupe.normalize_url("https://a.com/view?id=1") != dedupe.normalize_url("https://a.com/view?id=2")
    assert dedupe.normalize_url("https://a.com/x?fbclid=1&id=3") == "https://a.com/x?id=3"


def test_followup_is_kept_and_similar_titles_are_grouped_without_loss(conn):
    db.upsert_article(conn, make_article("https://a.com/1", "NIC says Bismayah payment paperwork finalized"))
    db.upsert_article(conn, make_article("https://b.com/1", "NIC says Bismayah payment paperwork finalised",
                                         source_domain="b.com"))
    db.upsert_article(conn, make_article("https://a.com/2", "Hanwha confirms receipt of Bismayah funds in Iraq",
                                         published_at="2026-11-11T09:00:00Z", published_date="2026-11-11"))
    pipeline.recompute(conn)
    rows = db.all_articles(conn)
    assert len(rows) == 3                                   # 원본 레코드는 모두 남는다
    assert len({r["event_id"] for r in rows}) == 2          # 후속 발표는 별도 사건
    listed = pipeline.list_articles(conn, "2026-11-10", "2026-11-11")
    assert len(listed) == 2 and sum(len(a["related"]) for a in listed) == 1
    grouped = next(a for a in listed if a["related"])
    assert grouped["n_sources"] == 2


def test_reprinted_wire_copy_is_one_source():
    group = [dict(make_article("https://a.com/1", "Same headline"), id=1),
             dict(make_article("https://b.com/1", "Same headline", source_domain="b.com"), id=2)]
    assert dedupe.independent_sources(group) == 1


def test_baghdad_day_boundary(conn):
    # 11.9 21:30 UTC = 바그다드 11.10 00:30
    a = make_article(published_at="2026-11-09T21:30:00Z", published_date="2026-11-10")
    assert timeutil.in_period(a["published_at"], a["published_date"], "datetime", "2026-11-10", "2026-11-10") is True
    assert timeutil.in_period(a["published_at"], a["published_date"], "datetime", "2026-11-09", "2026-11-09") is False
    # 11.10 20:59 UTC = 바그다드 11.10 23:59, 21:00 UTC 는 이미 11.11
    assert timeutil.in_period("2026-11-10T20:59:59Z", None, "datetime", "2026-11-10", "2026-11-10") is True
    assert timeutil.in_period("2026-11-10T21:00:00Z", None, "datetime", "2026-11-10", "2026-11-10") is False
    assert timeutil.baghdad_date(timeutil.parse_iso("2026-11-09T21:30:00Z")) == "2026-11-10"


def test_date_only_and_unknown_are_distinguished(conn):
    assert timeutil.in_period(None, "2026-11-10", "date", "2026-11-10", "2026-11-10") is True
    assert timeutil.in_period(None, None, "unknown", "2026-11-10", "2026-11-10") is None
    assert "시각 미상" in timeutil.display(None, "2026-11-10", "date")
    db.upsert_article(conn, make_article("https://a.com/u", "Iraq undated item", published_at=None,
                                         published_date=None, time_precision="unknown"))
    pipeline.recompute(conn)
    shown = pipeline.list_articles(conn, "2026-11-10", "2026-11-10")
    assert shown[0]["undated"] and "발표 시각 미상" in shown[0]["verification"]
    assert pipeline.list_articles(conn, "2026-11-10", "2026-11-10", include_undated=False) == []


def _fake_search(fail=(), empty=()):
    def search(spec, d_from, d_to):
        if spec.query in fail:
            return SearchResult(spec, "failed", error="HTTP 503")
        if spec.query in empty:
            return SearchResult(spec, "empty")
        return SearchResult(spec, "ok", [make_article(f"https://ok.com/{spec.query}", f"Iraq news {spec.query}")])
    return search


def test_partial_failure_keeps_successful_results(conn):
    specs = [SearchSpec("google_news", "one", "en"), SearchSpec("google_news", "two", "en"),
             SearchSpec("gdelt", "three")]
    run_id = pipeline.run_collection(conn, "2026-11-10", "2026-11-10", specs=specs, search=_fake_search(fail={"two"}))
    run = pipeline.last_run(conn)
    assert run["id"] == run_id and run["status"] == "partial"
    assert [s["status"] for s in run["sources"]] == ["ok", "failed", "ok"]
    assert run["sources"][1]["error"] == "HTTP 503"
    assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 2


def test_run_statuses_are_distinct(conn):
    specs = [SearchSpec("google_news", "one", "en")]
    run = lambda **kw: (pipeline.run_collection(conn, "2026-11-10", "2026-11-10", **kw), pipeline.last_run(conn))[1]
    assert run(specs=specs, search=_fake_search(fail={"one"}))["status"] == "failed"
    assert run(specs=specs, search=_fake_search(empty={"one"}))["status"] == "empty"
    assert run(specs=[])["status"] == "unconfigured"
    assert run(specs=specs, search=_fake_search())["status"] == "ok"
    assert len(set(pipeline.STATUS_TEXT.values())) == len(pipeline.STATUS_TEXT)


def test_provider_exception_does_not_stop_collection(conn):
    def search(spec, d_from, d_to):
        if spec.query == "boom":
            raise RuntimeError("unexpected")
        return SearchResult(spec, "ok", [make_article()])
    pipeline.run_collection(conn, "2026-11-10", "2026-11-10",
                            specs=[SearchSpec("google_news", "boom", "en"), SearchSpec("google_news", "fine", "en")],
                            search=search)
    assert pipeline.last_run(conn)["status"] == "partial"


def test_collection_does_not_overwrite_user_fields(conn):
    aid, _ = db.upsert_article(conn, make_article())
    conn.execute("UPDATE articles SET selected=1, note='메모', ko_title='직접 쓴 제목', summary_status='ok' WHERE id=?", (aid,))
    pipeline.run_collection(conn, "2026-11-10", "2026-11-10", specs=[SearchSpec("google_news", "x", "en")],
                            search=lambda s, a, b: SearchResult(s, "ok", [make_article(title="Changed title")]))
    a = db.get_article(conn, aid)
    assert (a["selected"], a["note"], a["ko_title"], a["title"]) == (1, "메모", "직접 쓴 제목", "Iraq cabinet approves budget")


def test_google_news_fixture_parses():
    for lang in ("en", "ko", "ar"):
        items = google_news.parse((FIX / f"gnews_{lang}.xml").read_bytes(), SearchSpec("google_news", "q", lang, "bismayah"))
        assert len(items) == 6
        a = items[0]
        assert a["url"].startswith("https://news.google.com/") and a["time_precision"] == "datetime"
        assert a["source_name"] and not a["title"].endswith(a["source_name"])
        assert a["search_topics"] == ["bismayah"] and a["lang"] == lang


def test_gdelt_seen_time_is_not_publication_time():
    data = b'{"articles":[{"url":"https://x.com/a","title":"Iraq cabinet","seendate":"20261110T093000Z","domain":"x.com","language":"English"},{"url":"https://y.pl/a","title":"Sejm","seendate":"20261110T093000Z","domain":"y.pl","language":"Polish"}]}'
    items = gdelt.parse(data, SearchSpec("gdelt", "q"))
    assert len(items) == 1 and items[0]["time_precision"] == "observed"
    assert "발표 시각 아님" in timeutil.display(items[0]["published_at"], items[0]["published_date"], "observed")
    with pytest.raises(FetchError):
        gdelt.parse(b"Please limit requests to one every 5 seconds", SearchSpec("gdelt", "q"))


def test_server_requests_are_restricted():
    for url in ("http://news.google.com/rss", "https://evil.example/rss", "https://127.0.0.1/x", "file:///etc/passwd"):
        with pytest.raises(FetchError):
            safe_get(url, {"news.google.com"})


def test_ranking_prefers_direct_topic_over_mention():
    direct = ranking.score_article(make_article(title="Iraq's NIC signs Bismayah annex with Hanwha"))
    mention = ranking.score_article(make_article(title="Iraq housing market overview", snippet="projects such as Bismayah"))
    plain = ranking.score_article(make_article(title="Jordan tourism rises"))
    assert direct["score"] > mention["score"] > plain["score"]
    assert "bismayah" in direct["topics"] and any("제목에서 직접" in r for r in direct["reasons"])
    assert any("단순 언급" in r for r in mention["reasons"])
    # 'NIC' 는 이라크 문맥이 없으면 주제로 보지 않는다
    assert "bismayah" not in ranking.score_article(make_article(title="NIC bank shares rise in Kenya"))["topics"]
    assert "bismayah" not in ranking.score_article(make_article(title="A nice day in Baghdad"))["topics"]


def test_ranking_tie_prefers_confirmed_recent_time():
    a = dict(make_article(), id=1, score=10)
    b = dict(make_article(published_at="2026-11-11T09:00:00Z"), id=2, score=10)
    c = dict(make_article(published_at="2026-11-12T09:00:00Z", time_precision="observed"), id=3, score=10)
    assert [x["id"] for x in sorted([a, c, b], key=ranking.sort_key)] == [2, 1, 3]


def test_importance_and_verification_are_separate():
    a = make_article(title="Iraq's NIC signs Bismayah annex with Hanwha")
    assert ranking.score_article(a, n_sources=1)["score"] >= 50
    assert "단일 출처 · 미확인" in ranking.verification(dict(a, access_level="title"), 1)


# ───────── 요약 점검 ─────────

import re as _re

_ARTICLE = _re.compile(r'<article id="(\d+)">.*?제목: (.*?)\n', _re.S)


def _namespace(parse):
    """Haiku 는 client.messages, fallback 지원 모델은 client.beta.messages 로 호출된다. 둘 다 받는다."""
    m = SimpleNamespace(parse=parse)
    return SimpleNamespace(messages=m, beta=SimpleNamespace(messages=m))


def _batch(text, make):
    items = []
    for aid, title in _ARTICLE.findall(text):
        s = make(title)
        if s is not None:
            items.append(llm.BatchItem(id=int(aid), title=s.ko_title, subs=[x for x in s.ko_summary.split("\n") if x],
                                       tag=s.category, stage=s.stage, claim_type=s.claim_type, followups=s.followups))
    usage = SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return SimpleNamespace(stop_reason="end_turn", parsed_output=llm.Batch(items=items), usage=usage)


def _fake_client(summary):
    return _namespace(lambda **kw: _batch(kw["messages"][0]["content"], lambda title: summary))


def test_tentative_input_is_not_strengthened_to_signed():
    """예시 점검: '협의 예정' 입력에 모델이 '체결 완료'로 답한 경우를 규칙이 잡아낸다.
    이 검사만으로 LLM 요약의 사실성이 보장되는 것은 아니다."""
    bad = llm.Summary(ko_title="NIC·한화, 비스마야 부속합의서 체결 완료", ko_summary="양측이 계약을 체결했다.",
                      category="경제", stage="체결", claim_type="언론 보도", followups=[])
    fixed, flags = llm.guard("NIC와 한화, 다음 주 비스마야 부속합의서 협의 예정", bad)
    assert fixed.stage == "협의"
    assert any("체결" in f for f in flags)
    ok = llm.Summary(ko_title="NIC·한화, 부속합의서 협의 예정", ko_summary="양측이 다음 주 협의할 예정이다.",
                     category="경제", stage="협의", claim_type="언론 보도", followups=[])
    assert llm.guard("NIC와 한화, 다음 주 비스마야 부속합의서 협의 예정", ok)[1] == []


def test_numbers_missing_from_source_are_flagged():
    s = llm.Summary(ko_title="NIC, 1.1억 달러 지급 절차 완료 발표", ko_summary="미수금 111 가운데 일부다.",
                    category="경제", stage="완료", claim_type="공식 발표", followups=[])
    _, flags = llm.guard("Iraq finalizes $111M Bismayah payment paperwork", s)
    assert [f for f in flags if "숫자" in f] == ["요약의 숫자 ‘1.1’ 이 원문 제목에 없음 — 원문에서 확인"]


def test_summary_is_labelled_as_title_based_and_failure_keeps_original(conn):
    aid, _ = db.upsert_article(conn, make_article(title="Iraq cabinet approves 2027 budget draft"))
    good = llm.Summary(ko_title="이라크 내각, 2027년 예산안 승인", ko_summary="이라크 내각이 2027년 예산안을 승인했다.",
                       category="정치", stage="승인", claim_type="언론 보도", followups=["의회 제출 일정"])
    assert llm.summarize_article(conn, db.get_article(conn, aid), _fake_client(good)) == "ok"
    a = db.get_article(conn, aid)
    assert a["summary_basis"] == "제목 기반" and a["access_level"] == "title" and a["summary_flags"] == []
    assert "제목 기반" in ranking.verification(a, 1) and "본문 기반" not in ranking.verification(a, 1)
    assert llm.summarize_article(conn, a, _fake_client(good)) == "skipped"   # 이미 처리한 기사는 다시 요약하지 않는다

    bid, _ = db.upsert_article(conn, make_article("https://example.com/b", "Another Iraq item"))
    def boom(**kw):
        raise TimeoutError("timed out")
    broken = _namespace(boom)
    assert llm.summarize_article(conn, db.get_article(conn, bid), broken) == "failed"
    b = db.get_article(conn, bid)
    assert b["summary_status"] == "failed" and b["title"] == "Another Iraq item" and b["url"]


def test_missing_key_is_reported_as_unconfigured(conn, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    aid, _ = db.upsert_article(conn, make_article())
    assert llm.summarize_article(conn, db.get_article(conn, aid)) == "unconfigured"


# ───────── 선택·초안 ─────────

def test_selection_and_draft_survive_reopen(data_dir):
    with db.session() as conn:
        aid, _ = db.upsert_article(conn, make_article())
        pipeline.recompute(conn)
        conn.execute("UPDATE articles SET selected=1 WHERE id=?", (aid,))
        draft.save(conn, "2026-11-10~2026-11-10", "내가 고친 초안", [aid])
    with db.session() as conn:                      # 새 연결 = 앱을 다시 연 상황
        assert db.get_article(conn, aid)["selected"] == 1
        d = draft.load(conn, "2026-11-10~2026-11-10")
        assert d["content"] == "내가 고친 초안" and d["article_ids"] == [aid]


def test_regenerating_preserves_previous_edit(conn):
    key = "2026-11-10~2026-11-10"
    draft.save(conn, key, "사용자가 편집한 내용")
    draft.save(conn, key, "새로 만든 초안", reason="재생성 전 보존")
    assert draft.load(conn, key)["content"] == "새로 만든 초안"
    assert draft.versions(conn, key)[0]["content"] == "사용자가 편집한 내용"


def test_copied_draft_has_links_and_unverified_marks(conn):
    db.upsert_article(conn, make_article("https://example.com/nic", "Iraq's NIC signs Bismayah annex with Hanwha"))
    db.upsert_article(conn, make_article("https://example.com/oil", "Iraq oil"))
    pipeline.recompute(conn)
    conn.execute("UPDATE articles SET selected=1")
    selected = pipeline.list_articles(conn, "2026-11-10", "2026-11-10", selected_only=True)
    text = draft.build_report(selected, "2026-11-10", "2026-11-10")
    assert "원문: https://example.com/nic" in text and "원문: https://example.com/oil" in text
    assert text.count("[미확인] 단일 출처") == 2 and "제목 기반" in text
    assert "대상 기간: 2026-11-10 ~ 2026-11-10 (바그다드 시간)" in text
    assert "비스마야·한화·NIC\n- Iraq's NIC signs" in text


def test_draft_states_search_scope_when_no_bismayah_item(conn):
    db.upsert_article(conn, make_article("https://example.com/oil", "Iraq oil"))
    pipeline.recompute(conn)
    conn.execute("UPDATE articles SET selected=1")
    selected = pipeline.list_articles(conn, "2026-11-10", "2026-11-10", selected_only=True)
    text = draft.build_report(selected, "2026-11-10", "2026-11-10", failed_sources=["gdelt · x"])
    assert draft.NOT_FOUND in text and "검색 실패 출처 1건" in text


# ───────── 첫 화면 추천 선별 ─────────

def _many(conn, n, title):
    """제목이 서로 충분히 달라 한 사건으로 묶이지 않는 기사 n건."""
    import hashlib
    for i in range(n):
        tail = hashlib.sha256(f"{title}{i}".encode()).hexdigest()[:40]
        db.upsert_article(conn, make_article(f"https://example.com/{tail}", f"{title} {tail}"))


def test_recommended_is_limited_and_balanced_across_categories(conn):
    _many(conn, 40, "Iraq attack")                 # 치안이 압도적으로 많다
    _many(conn, 5, "Iraq oil")                      # 경제
    _many(conn, 5, "Iraq parliament")          # 정치
    pipeline.recompute(conn)
    shown, matched, trimmed = pipeline.dashboard_list(conn, "2026-11-10", "2026-11-10", {}, False)
    assert matched == 50 and trimmed and len(shown) == 20
    cats = [a["category"] for a in shown]
    # 정원: 정치 4, 치안 3, 경제 3. 남는 자리는 점수순(치안이 가장 높음)으로 채운다.
    assert cats.count("경제") == 3 and cats.count("정치") == 4 and cats.count("치안") == 13
    everything, _, cut = pipeline.dashboard_list(conn, "2026-11-10", "2026-11-10", {}, True)
    assert len(everything) == 50 and not cut                     # 전체 보기


def test_direct_topic_and_selected_articles_are_never_dropped(conn):
    _many(conn, 30, "Iraq attack")
    db.upsert_article(conn, make_article("https://example.com/b", "Jordan paper profiles Bismayah housing"))
    low, _ = db.upsert_article(conn, make_article("https://example.com/low", "Jordan tourism rises"))
    conn.execute("UPDATE articles SET selected=1 WHERE id=?", (low,))
    pipeline.recompute(conn)
    shown, _, _ = pipeline.dashboard_list(conn, "2026-11-10", "2026-11-10", {}, False)
    by_title = {a["title"]: a["pick_reason"] for a in shown}
    assert by_title["Jordan paper profiles Bismayah housing"] == "관심 주제 직접 보도"
    assert by_title["Jordan tourism rises"] == "선택한 기사" and len(shown) == 21


def test_filters_and_small_lists_are_not_trimmed(conn):
    _many(conn, 30, "Iraq attack")
    pipeline.recompute(conn)
    shown, matched, trimmed = pipeline.dashboard_list(conn, "2026-11-10", "2026-11-10", {"category": "치안"}, False)
    assert len(shown) == matched == 30 and not trimmed
    conn.execute("DELETE FROM articles WHERE id > 5")
    shown, matched, trimmed = pipeline.dashboard_list(conn, "2026-11-10", "2026-11-10", {}, False)
    assert len(shown) == matched == 5 and not trimmed


# ───────── 추천분 자동 요약과 같은 사건 묶기 ─────────

class FakeLLM:
    """요약은 '요약: 원제목', 묶기는 미리 정한 답을 돌려주는 가짜 클라이언트. 호출 횟수와 요약한 기사 수를 센다."""

    def __init__(self, groups=None, fail_titles=(), drop_titles=()):
        self.groups, self.fail_titles, self.drop_titles = groups or (lambda text: []), fail_titles, drop_titles
        self.summary_calls, self.group_calls, self.summarized = 0, 0, 0
        ns = _namespace(self.parse)
        self.messages, self.beta = ns.messages, ns.beta

    def parse(self, **kw):
        text = kw["messages"][0]["content"]
        if kw["output_format"] is llm.EventGroups:
            self.group_calls += 1
            groups = [llm.EventGroup(ids=g, reason="같은 일") for g in self.groups(text)]
            return SimpleNamespace(stop_reason="end_turn", parsed_output=llm.EventGroups(groups=groups), usage=None)
        self.summary_calls += 1
        if any(f in text for f in self.fail_titles):
            raise TimeoutError("timed out")

        def make(title):
            if any(d in title for d in self.drop_titles):
                return None                                   # 응답에서 이 기사만 빠뜨린다
            self.summarized += 1
            return llm.Summary(ko_title="요약: " + title[:30], ko_summary="하위 한 줄", category="기타",
                               stage="해당없음", claim_type="언론 보도", followups=[])
        return _batch(text, make)


def test_view_is_summarized_once_and_missing_items_are_reported(conn):
    from app import enrich
    _many(conn, 6, "Iraq attack")
    db.upsert_article(conn, make_article("https://example.com/bad", "Iraq oil BROKEN item"))
    pipeline.recompute(conn)
    fake = FakeLLM(drop_titles=("BROKEN",))
    total = enrich.enrich_view(conn, "2026-11-10", "2026-11-10", {}, False, client=fake)
    assert (total["ok"], total["failed"]) == (6, 1) and "빠져" in total["first_error"]
    assert fake.summary_calls == 2                        # 7건을 한 번에 보내고, 빠진 1건만 따로 한 번 더 보냈다
    shown, _, _ = pipeline.dashboard_list(conn, "2026-11-10", "2026-11-10", {}, False)
    assert sum(a["ko_title"] is not None and a["ko_title"].startswith("요약: ") for a in shown) == 6
    assert next(a for a in shown if "BROKEN" in a["title"])["summary_status"] == "failed"
    again = FakeLLM()
    enrich.enrich_view(conn, "2026-11-10", "2026-11-10", {}, False, client=again)
    assert again.summarized == 1                          # 이미 요약한 6건은 다시 보내지 않는다
    assert "요약 6건" in enrich.message(total) and "실패 1건" in enrich.message(total)


def test_a_failed_call_fails_only_its_own_batch(conn, monkeypatch):
    monkeypatch.setattr(llm, "BATCH_SIZE", 3)
    _many(conn, 5, "Iraq attack")
    db.upsert_article(conn, make_article("https://example.com/bad", "Iraq oil BROKEN item"))
    pipeline.recompute(conn)
    counts = llm.summarize_batch(conn, db.all_articles(conn), FakeLLM(fail_titles=("BROKEN",)))
    # 묶음 2회 + 실패한 묶음의 3건을 한 건씩 재시도: 고장 난 1건만 실패로 남고 나머지 2건은 살아난다.
    assert counts["calls"] == 5 and counts["ok"] == 5 and counts["failed"] == 1 and "TimeoutError" in counts["first_error"]


def test_same_event_articles_are_merged_and_slot_is_refilled(conn):
    from app import enrich
    _many(conn, 25, "Iraq attack")
    pipeline.recompute(conn)
    first, _, _ = pipeline.dashboard_list(conn, "2026-11-10", "2026-11-10", {}, False)
    dup = [a["id"] for a in first[:3]]
    fake = FakeLLM(groups=lambda text: [dup] if all(f"[{i}]" in text for i in dup) else [])
    total = enrich.enrich_view(conn, "2026-11-10", "2026-11-10", {}, False, client=fake)
    assert total["merged"] == 2
    shown, matched, _ = pipeline.dashboard_list(conn, "2026-11-10", "2026-11-10", {}, False)
    assert matched == 23 and len(shown) == 20             # 빈 자리는 다음 기사로 채워진다
    rep = next(a for a in shown if a["id"] in dup)
    assert {rep["id"], *[r["id"] for r in rep["related"]]} == set(dup)   # 원본은 묶음 아래에 그대로 남는다
    assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 25
    assert all(a["ko_title"] for a in shown)              # 새로 올라온 기사도 한국어로 처리됨
    pipeline.recompute(conn)                              # 다음 수집 때도 묶음이 유지된다
    assert pipeline.dashboard_list(conn, "2026-11-10", "2026-11-10", {}, False)[1] == 23


def test_grouping_answer_is_validated():
    arts = [dict(make_article(), id=1, published_date="2026-11-10"), dict(make_article(), id=2, published_date="2026-11-11"),
            dict(make_article(), id=3, published_date="2026-11-20"), dict(make_article(), id=4, published_date="2026-11-10")]
    g = lambda *ids: llm.EventGroup(ids=list(ids), reason="같은 일")
    assert llm.valid_groups([g(1, 2, 3, 99), g(2, 4), g(4)], arts) == [([1, 2], "같은 일")]   # 모르는 번호·먼 날짜·중복 사용·1건 묶음 제외
    assert llm.valid_groups([g(1, 2, 4)], arts, splits={(1, 2)}) == [([1, 4], "같은 일")]      # 사용자가 떼어 놓은 쌍은 묶지 않는다


def test_without_key_view_is_left_untouched(conn, monkeypatch):
    from app import enrich
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    _many(conn, 3, "Iraq attack")
    pipeline.recompute(conn)
    total = enrich.enrich_view(conn, "2026-11-10", "2026-11-10", {}, False)
    assert total["ok"] == 0 and total["unconfigured"] == 3 and "설정 필요" in enrich.message(total)


def test_small_spelled_out_numbers_are_not_flagged_and_flags_are_unique():
    s = llm.Summary(ko_title="이라크 총리실, 제27차 정례 각료회의 결정사항 공개", ko_summary="제27차 회의 결정사항을 게시했다. 규모는 350억이다.",
                    category="정치", stage="해당없음", claim_type="공식 발표", followups=[])
    _, flags = llm.guard("مقررات جلسة مجلس الوزراء الاعتيادية السابعة والعشرين 2026", s)
    assert flags == ["요약의 숫자 ‘350’ 이 원문 제목에 없음 — 원문에서 확인"]


def test_manual_merge_and_split_are_respected_by_auto_grouping(conn):
    from app import enrich
    _many(conn, 4, "Iraq attack")
    pipeline.recompute(conn)
    ids = [a["id"] for a in pipeline.dashboard_list(conn, "2026-11-10", "2026-11-10", {}, False)[0]]
    assert pipeline.merge_events(conn, ids[:3], "직접 묶음") == 2
    pipeline.recompute(conn)
    shown, matched, _ = pipeline.dashboard_list(conn, "2026-11-10", "2026-11-10", {}, False)
    assert matched == 2 and conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 4   # 원본은 그대로
    rep = next(a for a in shown if a["related"])
    assert {r["group_note"] for r in rep["related"]} == {"직접 묶음"}

    pipeline.split_article(conn, rep["id"])               # 대표 기사를 떼어도 나머지 묶음은 유지된다
    pipeline.recompute(conn)
    shown, matched, _ = pipeline.dashboard_list(conn, "2026-11-10", "2026-11-10", {}, False)
    assert matched == 3 and sorted(len(a["related"]) for a in shown) == [0, 0, 1]

    fake = FakeLLM(groups=lambda text: [ids])              # 모델이 전부 다시 묶으라고 해도
    enrich.enrich_view(conn, "2026-11-10", "2026-11-10", {}, False, client=fake)
    events = {r["id"]: r["event_id"] for r in conn.execute("SELECT id, event_id FROM articles")}
    former = [i for i in ids[:3] if i != rep["id"]]
    assert events[former[0]] == events[former[1]]          # 직접 묶은 나머지 둘은 그대로 한 사건
    assert events[rep["id"]] != events[former[0]]          # 떼어 놓은 기사는 예전 묶음으로 돌아가지 않는다


def test_negated_or_tentative_wording_is_not_flagged():
    s = llm.Summary(ko_title="Masgarian 사령관, 차기 주이라크 이란 대사 거론", ko_summary="임명 여부는 확정되지 않음",
                    category="국제", stage="해당없음", claim_type="언론 보도", followups=[])
    assert llm.guard("Inside story: Iran's next envoy to Iraq? Meet Commander Masgarian", s)[1] == []
    s2 = s.model_copy(update={"ko_summary": "대사 임명 확정"})
    assert any("확정" in f for f in llm.guard("Iran's next envoy to Iraq?", s2)[1])


def test_summaries_from_an_older_instruction_are_redone_once_but_user_text_is_kept(conn):
    from app import enrich
    _many(conn, 2, "Iraq attack")
    pipeline.recompute(conn)
    a, b = [r["id"] for r in conn.execute("SELECT id FROM articles ORDER BY id")]
    conn.execute("UPDATE articles SET ko_title='예전 문체', summary_status='ok', summary_ver=1 WHERE id=?", (a,))
    conn.execute("UPDATE articles SET ko_title='내가 쓴 제목', summary_status='ok', summary_ver=999 WHERE id=?", (b,))
    fake = FakeLLM()
    enrich.enrich_view(conn, "2026-11-10", "2026-11-10", {}, False, client=fake)
    assert db.get_article(conn, a)["ko_title"].startswith("요약: ") and db.get_article(conn, b)["ko_title"] == "내가 쓴 제목"
    enrich.enrich_view(conn, "2026-11-10", "2026-11-10", {}, False, client=fake)
    assert fake.summarized == 1


def test_everything_shown_is_translated_in_one_run_even_beyond_one_batch(conn, monkeypatch):
    from app import enrich
    monkeypatch.setattr(llm, "BATCH_SIZE", 6)             # 한 번에 보내는 건수보다 보이는 기사가 많은 상황
    _many(conn, 30, "Iraq attack")
    pipeline.recompute(conn)
    total = enrich.enrich_view(conn, "2026-11-10", "2026-11-10", {}, False, client=FakeLLM())
    shown, _, _ = pipeline.dashboard_list(conn, "2026-11-10", "2026-11-10", {}, False)
    assert total["remaining"] == 0 and len(shown) == 20 and all(a["ko_title"] for a in shown)


def test_stored_flags_are_rechecked_with_current_rules(conn):
    aid, _ = db.upsert_article(conn, make_article(title="مقررات جلسة مجلس الوزراء الاعتيادية السابعة والعشرين"))
    conn.execute("""UPDATE articles SET ko_title='이라크 총리 언론실, 제27차 정례 각료회의 결정사항 공개', ko_summary='',
                    category='정치', stage='해당없음', claim_type='공식 발표', summary_status='ok',
                    summary_flags='["숫자 확인 필요: ‘27’ 가 원문에 그대로 나오지 않음", "숫자 확인 필요: ‘27’ 가 원문에 그대로 나오지 않음"]'
                    WHERE id=?""", (aid,))
    llm.recheck_flags(conn)
    assert db.get_article(conn, aid)["summary_flags"] == []


# ───────── 보고서 구성에 맞춘 추천 ─────────

def test_interest_themes_raise_score_and_explain_why():
    plain = ranking.score_article(make_article(title="Iraq to build a new airport in Baghdad"))
    themed = ranking.score_article(make_article(title="Iraq PM aide: no deadline for disarming militias this year"))
    assert themed["score"] > plain["score"] and any("무장단체 무기통제" in r for r in themed["reasons"])
    # 이라크 문맥이 필요한 사안은 다른 나라 기사에 걸리지 않는다
    assert not any("관심 사안" in r for r in ranking.score_article(make_article(title="Lebanon militias clash in Beirut"))["reasons"])


def test_us_iran_news_without_iraq_is_recommended_in_international_section(conn):
    _many(conn, 30, "Iraq attack")
    db.upsert_article(conn, make_article("https://example.com/v", "Vance weighs direct talks with Iran security chief"))
    db.upsert_article(conn, make_article("https://example.com/o", "Oil up 1% as Brent and WTI rebound"))
    pipeline.recompute(conn)
    shown, _, _ = pipeline.dashboard_list(conn, "2026-11-10", "2026-11-10", {}, False)
    by = {a["title"]: a for a in shown}
    assert by["Vance weighs direct talks with Iran security chief"]["pick_reason"] == "국제 구역 상위"
    assert by["Oil up 1% as Brent and WTI rebound"]["pick_reason"] == "유가 구역 상위"
    names = dict(pipeline.sections(shown))
    assert [a["title"] for a in names["국제"]] == ["Vance weighs direct talks with Iran security chief"]


def test_cabinet_is_capped_but_bismayah_is_not(conn):
    import hashlib
    for i in range(6):
        h = hashlib.sha256(str(i).encode()).hexdigest()[:30]
        db.upsert_article(conn, make_article(f"https://example.com/c{i}", f"Iraqi cabinet {h}"))
        db.upsert_article(conn, make_article(f"https://example.com/b{i}", f"Bismayah {h[::-1]}"))
    _many(conn, 30, "Iraq attack")
    pipeline.recompute(conn)
    shown, _, _ = pipeline.dashboard_list(conn, "2026-11-10", "2026-11-10", {}, False)
    titles = [a["title"] for a in shown]
    assert sum(t.startswith("Iraqi cabinet") for t in titles) == 2      # 내각회의는 상한 2건
    assert sum(t.startswith("Bismayah") for t in titles) == 6           # 비스마야는 전부
    assert len(shown) == 20


def test_blocked_sources_are_hidden_and_preferred_sources_get_a_bonus(conn):
    db.upsert_article(conn, make_article("https://www.britannica.com/x", "Iraq history", source_name="Britannica",
                                         source_domain="britannica.com"))
    db.upsert_article(conn, make_article("https://shafaq.com/y", "Iraq budget talks", source_name="Shafaq News",
                                         source_domain="shafaq.com"))
    pipeline.recompute(conn)
    listed = pipeline.list_articles(conn, "2026-11-10", "2026-11-10")
    assert [a["source_name"] for a in listed] == ["Shafaq News"]
    assert "보고서에서 자주 인용하는 매체" in listed[0]["reasons"]


def test_longer_periods_show_proportionally_more(conn):
    assert pipeline.period_scale("2026-11-10", "2026-11-10") == 1 and pipeline.period_scale("2026-11-10", "2026-11-11") == 1
    assert pipeline.period_scale("2026-11-05", "2026-11-11") == 3          # 7일 → 최대 3배
    for day in ("2026-11-08", "2026-11-09", "2026-11-10", "2026-11-11"):
        import hashlib
        for i in range(30):
            h = hashlib.sha256(f"{day}{i}".encode()).hexdigest()[:40]
            db.upsert_article(conn, make_article(f"https://example.com/{h}", f"Iraq attack {h}",
                                                 published_at=f"{day}T09:00:00Z", published_date=day))
    pipeline.recompute(conn)
    assert len(pipeline.dashboard_list(conn, "2026-11-10", "2026-11-11", {}, False)[0]) == 20
    assert len(pipeline.dashboard_list(conn, "2026-11-08", "2026-11-11", {}, False)[0]) == 40


# ───────── 문체 규칙 적용과 요금 절감 ─────────

def test_summarizing_does_not_reshuffle_the_recommendation(conn):
    """Claude 가 붙인 태그가 규칙 분류와 달라도 추천 구성은 그대로다(다시 요약할 기사가 생기지 않는다)."""
    from app import enrich
    _many(conn, 40, "Iraq attack")
    _many(conn, 10, "Iraq oil")
    _many(conn, 10, "Iraq parliament")
    pipeline.recompute(conn)
    before = [a["id"] for a in pipeline.dashboard_list(conn, "2026-11-10", "2026-11-10", {}, False)[0]]
    fake = FakeLLM()                                       # 모든 기사에 '기타' 태그를 붙인다
    total = enrich.enrich_view(conn, "2026-11-10", "2026-11-10", {}, False, client=fake)
    after = [a["id"] for a in pipeline.dashboard_list(conn, "2026-11-10", "2026-11-10", {}, False)[0]]
    assert sorted(before) == sorted(after) and total["remaining"] == 0
    assert fake.summarized == len(before) == 20 and fake.summary_calls == 2      # 20건 = 10건씩 두 번 호출
    assert fake.group_calls == 1


def test_tags_drive_display_sections_and_bismayah_is_politics(conn):
    db.upsert_article(conn, make_article("https://example.com/b", "Hanwha Bismayah payment paperwork finalized"))
    aid, _ = db.upsert_article(conn, make_article("https://example.com/s", "Iraq army raises alert level"))
    oid, _ = db.upsert_article(conn, make_article("https://example.com/o", "Brent and WTI oil prices slip"))
    pipeline.recompute(conn)
    rows = {a["title"]: a for a in db.all_articles(conn)}
    assert rows["Hanwha Bismayah payment paperwork finalized"]["category"] == "정치"     # 문체 규칙 8절
    assert rows["Brent and WTI oil prices slip"]["category"] == "유가"
    conn.execute("UPDATE articles SET category='치안', summary_status='ok', ko_title='이라크軍, 경계태세 격상', summary_ver=3 WHERE id=?", (aid,))
    pipeline.recompute(conn)
    names = dict(pipeline.sections(pipeline.list_articles(conn, "2026-11-10", "2026-11-10")))
    assert [a["id"] for a in names["치안"]] == [aid] and [a["id"] for a in names["유가"]] == [oid]
    assert "치안 · 테러" not in names


def test_sub_items_are_stored_as_lines_and_used_in_draft(conn):
    aid, _ = db.upsert_article(conn, make_article("https://example.com/k", "Iraqi army declares Level C alert amid KH tension"))
    pipeline.recompute(conn)
    s = llm.Summary(ko_title="이라크軍, KH와의 긴장 고조 속 최고경계태세(Level C) 발령",
                    ko_summary="Al-Zaidi 총리, 밀수 연루 의혹 조사 지시\nKH 측, 승인 없는 진입 시 발포 위협",
                    category="치안", stage="해당없음", claim_type="언론 보도", followups=[])
    assert llm.summarize_article(conn, db.get_article(conn, aid), _fake_client(s)) == "ok"
    a = db.get_article(conn, aid)
    assert a["ko_summary"].split("\n") == ["Al-Zaidi 총리, 밀수 연루 의혹 조사 지시", "KH 측, 승인 없는 진입 시 발포 위협"]
    conn.execute("UPDATE articles SET selected=1")
    text = draft.build_report(pipeline.list_articles(conn, "2026-11-10", "2026-11-10", selected_only=True), "2026-11-10", "2026-11-10")
    assert "  * Al-Zaidi 총리, 밀수 연루 의혹 조사 지시\n  * KH 측, 승인 없는 진입 시 발포 위협" in text and "[치안]" in text


def test_user_corrections_become_examples_for_later_summaries(conn):
    aid, _ = db.upsert_article(conn, make_article(title="Iraq PM orders probe"))
    conn.execute("UPDATE articles SET ko_title='이라크 총리가 조사를 명령했다', ko_summary='', summary_status='ok' WHERE id=?", (aid,))
    assert llm.style_examples(conn) == ""
    llm.record_style_edit(conn, db.get_article(conn, aid), "Al-Zaidi 총리, 조사 지시", "")
    llm.record_style_edit(conn, db.get_article(conn, aid), "이라크 총리가 조사를 명령했다", "")     # 바뀐 것이 없으면 남기지 않는다
    text = llm.style_examples(conn)
    assert "원문 제목: Iraq PM orders probe" in text and "고치기 전: 이라크 총리가 조사를 명령했다" in text
    assert "고친 후: Al-Zaidi 총리, 조사 지시" in text and text.count("원문 제목:") == 1
    blocks = llm.system_blocks(text)
    assert "cache_control" in blocks[0] and blocks[1]["text"] == text


def test_usage_is_recorded_and_daily_budget_stops_automatic_runs(conn, monkeypatch):
    from app import enrich
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder")
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.001")
    _many(conn, 12, "Iraq attack")
    pipeline.recompute(conn)
    fake = FakeLLM()
    llm.summarize_batch(conn, db.all_articles(conn)[:10], fake)      # 호출 1회: 입력 1000 + 출력 200 토큰(Haiku 단가)
    u = llm.usage_today(conn)
    assert u["calls"] == 1 and abs(u["cost"] - (1000 * 0.10 + 200 * 0.50) / 1_000_000) < 1e-12
    assert not llm.budget_exceeded(conn)
    for _ in range(6):
        llm.record_usage(conn, {"model": "claude-haiku-5-5", "purpose": "summary", "input": 1000, "output": 200,
                                "cache_read": 0, "cache_write": 0})
    assert llm.budget_exceeded(conn)
    monkeypatch.setattr(llm, "_client", lambda: fake)
    before = fake.summary_calls
    total = enrich.enrich_view(conn, "2026-11-10", "2026-11-10", {}, False)          # 자동 실행: 한도 초과로 멈춘다
    assert total["over_budget"] and fake.summary_calls == before and "한도" in enrich.message(total)
    total = enrich.enrich_view(conn, "2026-11-10", "2026-11-10", {}, False, ignore_budget=True)   # 직접 누른 경우
    assert total["ok"] == 2
    assert llm.estimate_cost({"model": "unknown", "input": 1, "output": 1, "cache_read": 0, "cache_write": 0}, {}) is None


def test_korean_articles_can_be_left_as_they_are(conn, monkeypatch):
    aid, _ = db.upsert_article(conn, make_article("https://example.com/ko", "이라크 내각 예산안 의결", lang="ko"))
    a = db.get_article(conn, aid)
    assert llm.needs_summary(a)                              # 기본: 한국어 기사도 문체 규칙대로 다시 쓴다
    monkeypatch.setenv("SUMMARIZE_KOREAN", "0")
    assert not llm.needs_summary(a)


def test_baghdad_time_works_without_timezone_database(monkeypatch):
    """윈도우처럼 시간대 데이터가 없는 환경에서도 같은 결과가 나온다(고정 UTC+3)."""
    import importlib, zoneinfo
    def missing(key):
        raise zoneinfo.ZoneInfoNotFoundError(key)
    monkeypatch.setattr(zoneinfo, "ZoneInfo", missing)
    fallback = importlib.reload(timeutil)
    try:
        assert fallback.baghdad_date(fallback.parse_iso("2026-11-09T21:30:00Z")) == "2026-11-10"
        assert fallback.day_bounds_utc("2026-11-10", "2026-11-10") == ("2026-11-09T21:00:00Z", "2026-11-10T21:00:00Z")
    finally:
        monkeypatch.undo()
        importlib.reload(timeutil)


# ───────── 화제성(여러 매체가 함께 다룬 사안) ─────────

def test_stories_covered_by_many_outlets_rise_and_are_not_repeated(conn):
    for i in range(8):                                    # 같은 사안을 8개 매체가 서로 다른 제목으로 보도
        db.upsert_article(conn, make_article(f"https://s{i}.example/a", f"Iraq probe into Jurf smuggling widens, outlet {i} angle {i * 37}",
                                             source_domain=f"s{i}.example", source_name=f"Outlet {i}"))
    db.upsert_article(conn, make_article("https://lone.example/a", "Iraq minister opens a museum wing", source_domain="lone.example"))
    _many(conn, 30, "Iraq attack")
    pipeline.recompute(conn)
    listed = pipeline.list_articles(conn, "2026-11-10", "2026-11-10")
    hot = [a for a in listed if "Jurf" in a["title"]]
    lone = next(a for a in listed if "museum" in a["title"])
    assert all(a["burst"] in ("jurf", "smuggling", "probe", "widens", "outlet", "angle") for a in hot) and lone["burst"] is None
    assert any("여러 매체가 다룬 화제" in r and "매체 8곳" in r for r in hot[0]["reasons"])
    assert min(a["score"] for a in hot) > lone["score"]
    shown, _, _ = pipeline.dashboard_list(conn, "2026-11-10", "2026-11-10", {}, False)
    assert 1 <= sum("Jurf" in a["title"] for a in shown) <= 2      # 같은 화제는 추천에 2건까지만


def test_ubiquitous_words_are_not_treated_as_a_story():
    from app import salience
    assert salience.tokens("Iraq says Iran talks with Trump continue in Baghdad") == {"continue"}
    arts = [dict(make_article(title=f"Iraq Iran unique{i}", source_domain=f"d{i}.x"), id=i, score=10, reasons=[]) for i in range(10)]
    salience.add_salience(arts, {"min_sources": 4})
    assert all(a["burst"] is None and a["score"] == 10 for a in arts)


def test_social_posts_are_not_recommended_but_stay_in_the_full_list(conn):
    """Facebook 같은 SNS 글은 추천에서 빠지고(여론 참고용), 전체 목록에는 남는다. 선택하면 다시 보인다."""
    from app.config import topics_config
    fb = make_article(title="Iraq cabinet approves 2027 budget draft law", source_name="Facebook", source_domain="facebook.com")
    news = make_article(title="Iraqi parliament votes on election law amendment", source_name="Shafaq News",
                        source_domain="shafaq.com", url="https://shafaq.com/a/1")
    fid, _ = db.upsert_article(conn, fb)
    nid, _ = db.upsert_article(conn, news)
    pipeline.recompute(conn)
    events = pipeline.list_articles(conn, "2026-11-01", "2026-11-30")
    assert {a["id"] for a in events} == {fid, nid}
    assert next(a for a in events if a["id"] == fid)["social"] is True
    assert events[-1]["id"] == fid                                   # SNS 글은 맨 뒤
    picked = {a["id"] for a in pipeline.pick_recommended(events, topics_config())}
    assert nid in picked and fid not in picked
    conn.execute("UPDATE articles SET selected=1 WHERE id=?", (fid,))
    events = pipeline.list_articles(conn, "2026-11-01", "2026-11-30")
    assert fid in {a["id"] for a in pipeline.pick_recommended(events, topics_config())}


def test_arabic_source_names_are_shown_in_a_readable_form():
    from app.config import topics_config
    cfg = topics_config()
    assert ranking.source_display({"source_name": "مكتب رئيس مجلس الوزراء", "source_domain": "pmo.iq"}, cfg) == "이라크 총리실"
    assert ranking.source_display({"source_name": "شفق نيوز", "source_domain": "shafaq.com"}, cfg) == "Shafaq News"
    assert ranking.source_display({"source_name": "وكالة مجهولة", "source_domain": "example.iq"}, cfg) == "example.iq"
    assert ranking.source_display({"source_name": "Reuters", "source_domain": "reuters.com"}, cfg) == "Reuters"
    assert ranking.source_display({"source_name": "연합뉴스", "source_domain": "yna.co.kr"}, cfg) == "연합뉴스"
    assert not ranking.is_social_source({"source_name": "Max News", "source_domain": "max.com"}, cfg)
    assert ranking.is_social_source({"source_name": "X", "source_domain": "x.com"}, cfg)


def test_arabic_pm_title_alone_does_not_make_an_article_a_cabinet_meeting_item():
    """아랍어 '총리'(رئيس مجلس الوزراء)에 '내각'이 들어 있다고 해서 총리의 모든 동정이 내각회의 주제가 되지는 않는다."""
    from app.config import topics_config
    cfg = topics_config()
    speech = make_article(title="أهم ما جاء في كلمة رئيس مجلس الوزراء السيد علي فالح الزيدي أثناء حفل افتتاح كاتدرائية في بغداد",
                          source_name="مكتب رئيس مجلس الوزراء", lang="ar")
    session = make_article(title="رئيس مجلس الوزراء يترأس الجلسة الاعتيادية لمجلس الوزراء في بغداد", lang="ar")
    decision = make_article(title="مجلس الوزراء العراقي يقر مشروع قانون الموازنة", lang="ar")
    assert ranking.match_topic("cabinet", speech, cfg) is None
    assert ranking.match_topic("cabinet", session, cfg) == "title"
    assert ranking.match_topic("cabinet", decision, cfg) == "title"


def test_search_strategy_keywords_are_searched_and_regional_news_from_iraqi_outlets_is_not_iraqi(conn):
    """담당자의 검색 전략 키워드가 검색어에 들어 있고, 이라크 매체가 전한 예멘·사우디 기사는 이라크 사안이 아니다."""
    from app.config import topics_config, search_goals_text
    cfg = topics_config()
    queries = " ".join(s["q"] for s in cfg["searches"])
    for kw in ("قرارات مجلس الوزراء", "القوات الأمنية", "الإنذار الأمني", "كتائب حزب الله", "الحشد الشعبي", "جرف الصخر", "اعتقال",
               "تهريب", "هجوم إرهابي", "عبوة ناسفة", "جهاز مكافحة الإرهاب", "Trump", "IRGC", "Basmaya", "ملحق العقد",
               "المستحقات المالية", "استئناف العمل", "هيئة الاستثمار الوطنية", "한화건설"):
        assert kw in queries, kw
    assert "내각회의 의결사항" in search_goals_text() and "실제 송금" in search_goals_text()
    yemen = make_article(title="القوات المسلحة اليمنية تستهدف شركة أرامكو بطائرة مسيرة", source_name="Shafaq News",
                         source_domain="shafaq.com", lang="ar")
    pm = make_article(title="الزيدي يستقبل السفير الإيراني ويبحث معه العلاقات الثنائية", source_name="INA",
                      source_domain="ina.iq", lang="ar")
    assert ranking.classify(yemen, cfg)[1] == "걸프·사우디"
    assert "iraq_attack" not in [t["id"] for t in ranking.match_themes(yemen, cfg)]
    assert ranking.classify(pm, cfg)[1] == "이라크" and "pm_action" in [t["id"] for t in ranking.match_themes(pm, cfg)]
    assert ranking.match_topic("bismayah", make_article(title="Basmaya housing units handed over"), cfg) == "title"
    tunisia = make_article(title="الإطار التنسيقي بتونس يرفض مشروع قانون المالية 2027 ويدعو لمسيرة", source_name="الجزيرة",
                           source_domain="aljazeera.net", lang="ar")
    assert "parties" not in [t["id"] for t in ranking.match_themes(tunisia, cfg)]       # 다른 나라의 '조정기구'는 아니다
    iraqi = make_article(title="الإطار التنسيقي يعقد اجتماعاً لبحث ملف حصر السلاح", source_name="Shafaq News",
                         source_domain="shafaq.com", lang="ar")
    assert "parties" in [t["id"] for t in ranking.match_themes(iraqi, cfg)]
