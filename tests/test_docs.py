"""일일 PPT·주간 Word 생성 검증. 원본 양식 파일이 없으면 건너뛴다."""
import pytest

from app import config
from app.docs import daily_ppt, weekly_word
from app.docs.items import parse_items, wrap_sub_line, display_width

needs_ppt = pytest.mark.skipif(not config.DAILY_TEMPLATE.exists(), reason="일일 PPT 원본 양식 없음")
needs_doc = pytest.mark.skipif(not config.WEEKLY_TEMPLATE.exists(), reason="주간 Word 원본 양식 없음")

DAILY = {"date": "2026-11-12", "no_event_text": "특이사항 없음",
         "events": [{"time": "08:49", "text": "Kirkuk州, 시험 사건 하나", "type": "counter", "x": 0.32, "y": 0.34},
                    {"time": "", "text": "Baghdad, 시험 사건 둘", "type": "terror", "x": 0.2, "y": 0.8},
                    {"time": "21:05", "text": "Basra州, 시험 사건 셋", "type": "terror", "x": None, "y": None}],
         "politics": "11.12, 시험 주체, 머리줄 (Shafaq, 11.12)\n* 하위 줄\n\n11.12, 둘째 항목 (INA, 11.12)",
         "economy": "11.12, 경제 항목 (Aljazeera, 11.12)\n☞ 평가 줄"}

WEEKLY = {"period_start": "2026-11-12", "period_end": "2026-11-18", "report_date": "2026-11-19",
          "politics": "11.12, 시험 주체, 정치 항목\n* 하위 줄\n\n11.13, 둘째 항목",
          "stats": {"attack": "1", "ied": "0", "assassination": "2", "protest": "3", "shooting": "0", "suicide": "4"},
          "stats_basis": "시험", "terror": "11.13, 치안 항목\n* 하위 줄",
          "oil": "11.17, 시험 설명\n\n11.18, 전일 대비 소폭 상승",
          "oil_prices": [{"date": "11.17", "dubai": "80", "brent": "81.5", "wti": "79.25"},
                         {"date": "11.18", "dubai": "81", "brent": "82", "wti": "80"}],
          "oil_source": "시험", "intl": [{"title": "시험 주제", "body": "11.12, 국제 항목\n* 하위 줄"}],
          "impact": "영향 문단 하나 — 임직원 안전조치 강화\n\n영향 문단 둘 — 비상대응 태세 유지"}


def test_parse_items():
    items = parse_items("- 11.5, 머리줄\n* 하위 하나\n이어지는 줄\n☞평가\n\n11.6, 둘째")
    assert items == [{"head": "11.5, 머리줄", "subs": ["* 하위 하나 이어지는 줄", "☞ 평가"]},
                     {"head": "11.6, 둘째", "subs": []}]


def test_sub_lines_wrap_like_original():
    lines = wrap_sub_line("* " + "가나다라 " * 20, 68)
    assert len(lines) > 1 and all(display_width(ln) <= 68 for ln in lines)
    assert lines[1].startswith("  ") and not lines[1].startswith("   ")
    assert wrap_sub_line("☞ " + "가나다라 " * 20, 68)[1].startswith("   ")


@needs_ppt
def test_ppt_event_numbers_match_map_markers(tmp_path):
    out = tmp_path / "d.pptx"
    warnings = daily_ppt.generate(DAILY, out)
    r = daily_ppt.read_markers(out)
    assert r["date"].strip() == "2026. 11. 12"
    assert r["lines"] == ["08:49 Kirkuk州, 시험 사건 하나", "Baghdad, 시험 사건 둘", "21:05 Basra州, 시험 사건 셋"]
    assert [m["number"] for m in sorted(r["side"], key=lambda m: m["top"])] == [1, 2, 3]   # 목록 순서 = 번호
    side = {m["number"]: m["color"] for m in r["side"]}
    on_map = {m["number"]: m["color"] for m in r["map"]}
    assert set(on_map) == {1, 2}                         # 위치를 지정한 사건만 지도에 찍힌다
    assert all(side[n] == c for n, c in on_map.items())  # 같은 번호는 같은 색
    assert side == {1: "00B0F0", 2: "FF0000", 3: "FF0000"}
    assert any("사건 3" in w and "위치" in w for w in warnings)


@needs_ppt
def test_ppt_keeps_template_shapes_and_stays_editable(tmp_path):
    from pptx import Presentation
    out = tmp_path / "d.pptx"
    daily_ppt.generate(DAILY, out)
    orig, gen = Presentation(str(config.DAILY_TEMPLATE)), Presentation(str(out))
    assert len(gen.slides) == 2
    fixed = lambda prs, i: {(s.shape_id, s.name, s.left, s.top, s.width, s.height) for s in prs.slides[i].shapes
                            if not daily_ppt._is_number_marker(s) and s.shape_id != daily_ppt.ID_LIST}
    assert fixed(orig, 0) == fixed(gen, 0) and fixed(orig, 1) == fixed(gen, 1)
    ids = [s.shape_id for s in gen.slides[0].shapes]
    assert len(ids) == len(set(ids))                     # 도형 id 중복 없음
    r = daily_ppt.read_markers(out)
    assert "시험 주체, 머리줄" in r["politics"] and "* 하위 줄" in r["politics"] and "☞ 평가 줄" in r["economy"]
    # 다시 열어 글자를 고칠 수 있다(그림이 아니라 편집 가능한 글상자)
    box = next(s for s in gen.slides[1].shapes if s.shape_id == daily_ppt.ID_POLITICS)
    box.text_frame.paragraphs[0].runs[0].text = "수정"
    gen.save(str(tmp_path / "edited.pptx"))


@needs_ppt
def test_ppt_no_events_and_overflow_warning(tmp_path):
    out = tmp_path / "d.pptx"
    data = dict(DAILY, events=[], politics="\n\n".join(f"11.12, 항목 {i} 머리줄 (출처, 11.12)\n* 하위 줄" for i in range(9)))
    warnings = daily_ppt.generate(data, out)
    r = daily_ppt.read_markers(out)
    assert r["side"] == [] and r["map"] == [] and r["lines"] == ["특이사항 없음"]
    assert any("분량 초과" in w for w in warnings)


@needs_doc
def test_word_totals_and_prices_match_input(tmp_path):
    out = tmp_path / "w.docx"
    warnings = weekly_word.generate(WEEKLY, out)
    r = weekly_word.read_back(out)
    assert r["stats"] == {"구분": "건수", "계": "10", "무장세력공격": "1", "IED": "0", "암 살": "2", "시 위": "3",
                          "총 격": "0", "자살폭탄테러": "4"}
    assert int(r["stats"]["계"]) == sum(int(v) for k, v in r["stats"].items() if k not in ("구분", "계"))
    assert r["oil"] == [["구 분", "두바이유", "브렌트유", "서부텍사스유(WTI)"],
                        ["11.17", "$80.00", "$81.50", "$79.25"], ["11.18", "$81.00", "$82.00", "$80.00"]]
    assert warnings == []
    p = r["paragraphs"]
    assert p[0] == "건설, 이라크 주간 종합 상황보고(΄26.11.12 ~ ΄26.11.18)" and "2026. 11. 19." in p[1]
    order = ["이라크 국내 상황", "정국 / 치안", "정치권 동향", "11.12, 시험 주체, 정치 항목\n* 하위 줄", "11.13, 둘째 항목",
             "이라크 주간 테러 상황", "11.13, 치안 항목\n* 하위 줄", "경제", "국제유가 관련 동향", "11.17, 시험 설명",
             "11.18, 전일 대비 소폭 상승", "국제사회", "시험 주제", "11.12, 국제 항목\n* 하위 줄",
             "그룹 / 건설에 미치는 영향", "영향 문단 하나 — 임직원 안전조치 강화", "영향 문단 둘 — 비상대응 태세 유지"]
    assert [x.strip() for x in p if x.strip()][2:] == order
    assert not any("Al-Zaidi" in x for x in p)           # 원본의 지난 내용이 남지 않는다


@needs_doc
def test_word_keeps_template_formatting(tmp_path):
    from docx import Document
    out = tmp_path / "w.docx"
    weekly_word.generate(WEEKLY, out)
    orig, gen = Document(str(config.WEEKLY_TEMPLATE)), Document(str(out))
    sec = lambda d: (d.sections[0].page_width, d.sections[0].page_height, d.sections[0].left_margin, d.sections[0].top_margin)
    assert sec(orig) == sec(gen) and len(gen.tables) == 2
    num = lambda p: p._p.pPr.numPr is not None and (p._p.pPr.numPr.numId.val, p._p.pPr.numPr.ilvl.val)
    o = {p.text.strip(): num(p) for p in orig.paragraphs if p.text.strip()}
    g = {p.text.strip(): num(p) for p in gen.paragraphs if p.text.strip()}
    for heading in ("이라크 국내 상황", "정국 / 치안", "정치권 동향", "이라크 주간 테러 상황", "경제", "국제사회"):
        assert o[heading] == g[heading]
    item = next(p for p in gen.paragraphs if p.text.startswith("11.12, 시험 주체"))
    assert num(item) == (2, 1) and item.runs[0].font.size.pt == 14 and item.runs[1].font.size.pt == 13
    item.runs[0].text = "수정"                            # 편집 가능
    gen.save(str(tmp_path / "edited.docx"))


@needs_doc
def test_word_leaves_blank_numbers_blank_and_refuses_only_non_numbers(tmp_path):
    """빈 칸은 추측해 채우지 않고 빈 채로 내보내며 경고한다. 숫자가 아닌 값만 생성을 막는다."""
    out = tmp_path / "blank.docx"
    warnings = weekly_word.generate(dict(WEEKLY, stats=dict(WEEKLY["stats"], ied=""), oil_prices=[]), out)
    assert any("빈 칸" in w for w in warnings) and any("유가 표가 비어" in w for w in warnings)
    from docx import Document
    doc = Document(str(out))
    stat = [c.text.strip() for c in doc.tables[0].rows[1].cells]
    assert stat[1] == "" and "" in stat[2:]                          # 합계도 비운다
    assert all(c.text.strip() == "" for row in doc.tables[1].rows[1:] for c in row.cells)
    for broken in (dict(WEEKLY, stats=dict(WEEKLY["stats"], ied="많음")),
                   dict(WEEKLY, oil_prices=[{"date": "11.17", "dubai": "약 80", "brent": "81", "wti": "79"}])):
        with pytest.raises(weekly_word.WeeklyInputError):
            weekly_word.generate(broken, tmp_path / "w.docx")
        assert not (tmp_path / "w.docx").exists()


def test_oil_description_is_checked_against_table():
    falling = [{"date": "11.17", "dubai": "80", "brent": "81", "wti": "79"},
               {"date": "11.18", "dubai": "79", "brent": "80", "wti": "78"}]
    errors, warnings = weekly_word.check(dict(WEEKLY, oil_prices=falling))
    assert errors == [] and any("상승" in w and "11.18" in w for w in warnings)
    assert weekly_word.check(WEEKLY) == ([], [])


@needs_ppt
def test_weekend_deck_uses_date_range_and_dated_event_lines(tmp_path):
    out = tmp_path / "d.pptx"
    data = dict(DAILY, date="2026-11-12", date_to="2026-11-13",
                events=[{"day": "11.12", "time": "16:01", "text": "Baghdad州, 시험 사건", "type": "terror", "x": 0.328, "y": 0.505}])
    daily_ppt.generate(data, out)
    r = daily_ppt.read_markers(out)
    assert r["date"].strip() == "2026. 11. 12 ~ 13" and r["lines"] == ["11.12 16:01 Baghdad州, 시험 사건"]
    assert daily_ppt.date_label("2026-10-31", "2026-11-01") == "2026. 10. 31 ~ 11. 1"


def test_daily_import_follows_the_report_convention():
    from app.main import _daily_import
    base = dict(published_date="2026-11-12", published_at="2026-11-12T08:00:00Z", source_name="Shafaq", ko_summary="")
    sel = [dict(base, category="치안", ko_title="이라크軍, 최고경계태세 발령", title="x"),
           dict(base, category="경제", ko_title="CBI, 환율 인상 결정", title="x"),
           dict(base, category="유가", ko_title="브렌트유 소폭 상승", title="x"),
           dict(base, category="국제", ko_title="Trump 대통령, 이란과 합의 불필요 표명", title="x", source_name="Aljazeera"),
           dict(base, category="국제", ko_title="IRGC, 보복 경고", title="x", source_name="Aljazeera",
                published_date="2026-11-13", published_at="2026-11-13T08:00:00Z")]
    got = _daily_import(sel, "2026-11-12", "2026-11-13")
    pol, intl = got["politics"], got["intl"]
    assert got["skipped_oil"] == 1 and got["events"] == []
    assert [i["head"] for i in pol] == ["11.12, 이라크軍, 최고경계태세 발령 (Shafaq, 11.12)", "11.12, CBI, 환율 인상 결정 (Shafaq, 11.12)"]
    assert intl == [{"head": "11.12~13, 美-이란, 역내 분쟁 관련 (Aljazeera, 11.13)",
                     "subs": ["* (11.12) Trump 대통령, 이란과 합의 불필요 표명", "* (11.13) IRGC, 보복 경고"]}]
    assert _daily_import(sel[3:4], "2026-11-12")["intl"][0] == {"head": "11.12, 美-이란, 역내 분쟁 관련 (Aljazeera, 11.12)",
                                                            "subs": ["* Trump 대통령, 이란과 합의 불필요 표명"]}


@needs_ppt
def test_map_base_image_can_be_made_without_libreoffice(tmp_path):
    """그림 합성으로 만든 바탕: 지도 그림이 원본 슬라이드와 같은 비율 위치에 놓인다."""
    from PIL import Image
    from pptx import Presentation
    out = tmp_path / "base.png"
    daily_ppt.compose_base_image(out, width=1600)
    img = Image.open(out)
    assert img.size == (1600, 900)                                   # 16:9 슬라이드와 같은 비율
    prs = Presentation(str(config.DAILY_TEMPLATE))
    big = max((s for s in prs.slides[0].shapes if s.shape_type == 13), key=lambda s: s.width * s.height)
    cx, cy = (big.left + big.width / 2) / prs.slide_width, (big.top + big.height / 2) / prs.slide_height
    inside = img.crop((int(cx * 1600) - 150, int(cy * 900) - 150, int(cx * 1600) + 150, int(cy * 900) + 150))
    assert len(set(inside.getdata())) > 20                           # 지도 한가운데에는 그림이 있다
    assert img.getpixel((1590, 450)) == (255, 255, 255)              # 지도 밖 오른쪽 끝은 빈 바탕
    # 실제 보고서의 Kirkuk 표식 위치(설정의 기본값)가 지도 그림 안에 들어온다
    kx, ky = config.topics_config()["map_locations"]["Kirkuk"]
    assert big.left / prs.slide_width < kx < (big.left + big.width) / prs.slide_width
    assert big.top / prs.slide_height < ky < (big.top + big.height) / prs.slide_height


@needs_ppt
def test_map_base_image_draws_kurds_area_and_legend_but_not_the_text_boxes(tmp_path):
    """합성 바탕에 Kurds 영역(노랑)과 범례의 빨강·파랑 원이 있고, 날짜·사건 목록 자리는 비어 있다."""
    from PIL import Image
    from pptx import Presentation
    out = tmp_path / "base.png"
    daily_ppt.compose_base_image(out, width=1600)
    img = Image.open(out)
    prs = Presentation(str(config.DAILY_TEMPLATE))
    shapes = {s.shape_id: s for s in prs.slides[0].shapes}

    def center(sh):
        return (round((sh.left + sh.width / 2) / prs.slide_width * 1600),
                round((sh.top + sh.height / 2) / prs.slide_height * 900))

    r, g, b = img.getpixel((500, 205))                               # ERBIL 글자 아래, Kurds 영역 안
    assert r > 230 and g > 230 and b < 200                           # 노란 계열
    r, g, b = img.getpixel(center(shapes[35]))                       # 범례: 테러상황(빨강)
    assert r > 200 and g < 170 and b < 170
    r, g, b = img.getpixel(center(shapes[34]))                       # 범례: 대테러작전(파랑)
    assert b > 200 and r < 170
    for sid in (daily_ppt.ID_DATE, daily_ppt.ID_LIST):               # 화면이 직접 보여주는 칸은 그리지 않는다
        assert img.getpixel(center(shapes[sid])) == (255, 255, 255)


@needs_ppt
def test_slide1_layout_matches_template_shapes():
    """편집 화면이 쓰는 배치 값은 양식의 도형 값 그대로다(표식 크기 = 슬라이드 폭의 약 1.84%)."""
    from pptx import Presentation
    prs = Presentation(str(config.DAILY_TEMPLATE))
    slide = prs.slides[0]
    box = next(s for s in slide.shapes if s.shape_id == daily_ppt.ID_LIST)
    marker = next(s for s in slide.shapes if daily_ppt._is_number_marker(s))
    lay = daily_ppt.slide1_layout()
    assert lay["marker"]["w"] == marker.width / prs.slide_width
    assert 0.017 < lay["marker"]["w"] < 0.02
    assert lay["list"]["x"] == box.left / prs.slide_width
    assert lay["list"]["y"] == box.top / prs.slide_height
    assert lay["list"]["line_h"] == daily_ppt.LIST_LINE_EMU / prs.slide_width


def test_daily_download_name_uses_the_usual_file_name():
    from app.main import download_name
    assert download_name("daily", "2026-11-09") == "일일정보보고(11.9).pptx"
    assert download_name("daily", "2026-11-14") == "일일정보보고(11.14).pptx"
    assert download_name("weekly", "2026-11-12") == "건설_이라크 주간 종합상황보고(11월 19일).docx"


def test_table_values_that_look_wrong_are_warned_about():
    """기간 밖 날짜, 다른 유종과 크게 다른 값, 한 유형에 몰린 통계, 빈 '영향' 구역을 알린다(생성은 막지 않는다)."""
    data = dict(WEEKLY, impact="..", stats={k: ("10" if k == "attack" else "0") for k in WEEKLY["stats"]},
                oil_prices=[{"date": "11.25", "dubai": "90", "brent": "80", "wti": "82"},
                            {"date": "11.18", "dubai": "89", "brent": "78", "wti": "21"}])
    errors, warnings = weekly_word.check(data)
    text = " / ".join(warnings)
    assert errors == []
    assert "11.25 가 보고 기간" in text and "‘서부텍사스유(WTI)’ $21" in text
    assert "한 유형에만 몰려" in text and "영향’이 비어 있거나 너무 짧음" in text


def test_glossary_uses_report_notation_for_currency_and_security_office():
    from app import llm
    assert llm.apply_glossary("16조 디나르 해외 차입 계획 부인") == "16조 IQD 해외 차입 계획 부인"
    assert llm.apply_glossary("안보공보실(Security Media Cell), 공습 발표") == "보안당국, 공습 발표"


def test_oil_sentences_are_checked_against_the_table_and_for_trend_claims():
    """사용자가 준 실제 값(11.12 → 11.13: 두바이유 상승, 브렌트유 보합, WTI 하락)으로 유가 문장을 대조한다."""
    prices = [{"date": "11.12", "dubai": "90.10", "brent": "95.82", "wti": "88.74"},
              {"date": "11.13", "dubai": "92.04", "brent": "95.84", "wti": "88.56"}]
    move = weekly_word.price_moves(prices)[0]
    assert move["overall"] == "혼조" and [d for d, _ in move["moves"].values()] == ["상승", "보합", "하락"]
    bad = "11.13, 세계 공급과잉 조짐 확산에 대한 시장의 해석으로 국제유가는 2주 연속 주간 하락"
    w = weekly_word._oil_direction_warnings(bad, prices)
    assert any("기간 추세 표현" in x for x in w) and any("엇갈림" in x and "혼조" in x for x in w)
    ok = "11.13, 공급과잉 우려와 중동 긴장이 맞서며 국제유가는 혼조"
    assert weekly_word._oil_direction_warnings(ok, prices) == []
    other_day = weekly_word._oil_direction_warnings("11.10, 긴장 완화 기대감으로 국제유가는 하락", prices)
    assert other_day == ["유가 설명(11.10)의 날짜가 표에 없어 방향을 확인하지 못함"]


def test_daily_import_puts_terror_on_slide1_and_non_iraq_items_in_the_international_box(conn):
    """테러는 1장 사건으로, 이라크 밖의 사안은 태그와 무관하게 경제/국제로. 전날 선택분도 가져오고 중복은 넣지 않는다."""
    from app import db, main
    from tests.conftest import make_article

    def add(url, day, **fields):
        aid, _ = db.upsert_article(conn, make_article(url=url, title=url, published_date=day, published_at=day + "T08:00:00Z",
                                                      source_name="Shafaq News", source_domain="shafaq.com"))
        cols = dict(selected=1, summary_status="ok", **fields)
        conn.execute(f"UPDATE articles SET {', '.join(k + '=?' for k in cols)} WHERE id=?", (*cols.values(), aid))
        return aid

    add("https://x/terror", "2026-11-14", category="테러", region="이라크",
        ko_title="이라크軍 항공대, Kirkuk州에서 공습으로 IS 조직원 3명 사살, 조직원 이송 담당 1명 체포")
    add("https://x/pol", "2026-11-14", category="정치", region="이라크", ko_title="Al-Zaidi 총리, 제4순환도로 조기 완공 지시")
    add("https://x/iran", "2026-11-13", category="정치", region="이란", ko_title="Trump 대통령, 이란과 합의 불필요 표명")   # 전날, 태그는 정치
    add("https://x/oil", "2026-11-14", category="유가", region="기타 중동", ko_title="국제유가 혼조")
    add("https://x/old", "2026-11-13", category="정치", region="이라크", ko_title="재무부 장관, 천궁-II 대금 지급 지시")
    db.save_edition(conn, "daily", "2026-11-13", {"events": [], "politics": "11.13, 재무부 장관, 천궁-II 대금 지급 지시 (INA, 11.13)",
                                                   "economy": ""})                    # 전날 보고에 이미 실린 것
    data = main._daily_default()
    msg = main._apply_daily_import(conn, data, "2026-11-14")
    assert data["events"] == [{"time": "", "day": "", "place": "Kirkuk", "type": "counter", "x": 0.321, "y": 0.336,
                               "text": "Kirkuk州, 이라크軍 항공대, 공습으로 IS 조직원 3명 사살, 조직원 이송 담당 1명 체포"}]
    assert data["politics"] == "11.14, Al-Zaidi 총리, 제4순환도로 조기 완공 지시 (Shafaq News, 11.14)"
    assert "IS 조직원" not in data["politics"] and "천궁" not in data["politics"]
    assert data["economy"] == "11.14, 美-이란, 역내 분쟁 관련 (Shafaq News, 11.14)\n* Trump 대통령, 이란과 합의 불필요 표명"
    assert "1장 사건 1건" in msg and "정치 / 치안 1건" in msg and "경제 / 국제 1건" in msg and "유가 1건" in msg
    again = main._apply_daily_import(conn, data, "2026-11-14")                         # 두 번 가져와도 그대로
    assert len(data["events"]) == 1 and data["politics"].count("제4순환도로") == 1 and data["economy"].count("Trump") == 1
    assert "새로 넣은 항목이 없습니다" in again and "이미 있는 항목 3건" in again
