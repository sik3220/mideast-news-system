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
          "impact": "영향 문단 하나\n\n영향 문단 둘"}


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
             "그룹 / 건설에 미치는 영향", "영향 문단 하나", "영향 문단 둘"]
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
def test_word_refuses_missing_numbers_instead_of_guessing(tmp_path):
    for broken in (dict(WEEKLY, stats=dict(WEEKLY["stats"], ied="")), dict(WEEKLY, oil_prices=[]),
                   dict(WEEKLY, oil_prices=[{"date": "11.17", "dubai": "", "brent": "81", "wti": "79"}])):
        with pytest.raises(weekly_word.WeeklyInputError):
            weekly_word.generate(broken, tmp_path / "w.docx")
        assert not (tmp_path / "w.docx").exists()


def test_oil_description_is_checked_against_table():
    falling = [{"date": "11.17", "dubai": "80", "brent": "81", "wti": "79"},
               {"date": "11.18", "dubai": "79", "brent": "80", "wti": "78"}]
    errors, warnings = weekly_word.check(dict(WEEKLY, oil_prices=falling))
    assert errors == [] and any("상승" in w and "11.18" in w for w in warnings)
    assert weekly_word.check(WEEKLY) == ([], [])
