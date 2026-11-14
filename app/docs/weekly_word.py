"""주간 DOCX 생성. 원본 파일을 열어 제목·항목·표 값만 바꾸고 번호 매기기·표 서식·머리글은 그대로 둔다.

원본 구조:
  제목, 작성일
  1. 이라크 국내 상황 → 정국 / 치안 → '정치권 동향' 항목들 → '이라크 주간 테러 상황' 표 + 사건 항목
     경제 → '국제유가 관련 동향' 항목 + 가격표
  2. 국제사회 → 주제 소제목 + 항목들
  3. 그룹 / 건설에 미치는 영향 → 문단들

통계와 유가는 사용자가 입력한 값만 쓴다. 값이 비어 있으면 생성하지 않고 오류를 낸다.
"""
from __future__ import annotations

import copy
import re
from datetime import date
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from lxml import etree

from .. import config
from .items import parse_items, wrap_sub_line

STAT_FIELDS = [("attack", "무장세력공격"), ("ied", "IED"), ("assassination", "암 살"),
               ("protest", "시 위"), ("shooting", "총 격"), ("suicide", "자살폭탄테러")]
OIL_FIELDS = [("dubai", "두바이유"), ("brent", "브렌트유"), ("wti", "서부텍사스유(WTI)")]
SUB_FONT_HALF_PT = 26  # 원본 하위 줄은 13pt(머리줄 14pt)
SUB_LINE_WIDTH = 68   # 원본 하위 줄의 최대 폭(전각 2, 반각 1 기준)
H_POLITICS, H_TERROR, H_ECONOMY, H_OIL, H_INTL, H_IMPACT = (
    "정치권 동향", "이라크 주간 테러 상황", "경제", "국제유가 관련 동향", "국제사회", "그룹 / 건설에 미치는 영향")


class WeeklyInputError(ValueError):
    pass


def _text(el) -> str:
    return "".join(t.text or "" for t in el.iter(qn("w:t")))


def _set_runs_text(runs, text: str) -> None:
    """첫 번째 글자 run 에 전체 문장을 넣고 나머지는 비운다(서식은 첫 run 것을 따른다)."""
    text_runs = [r for r in runs if r.find(qn("w:t")) is not None]
    if not text_runs:
        raise ValueError("글자를 넣을 위치를 찾지 못함")
    first = text_runs[0].find(qn("w:t"))
    first.text = text
    first.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    for r in text_runs[1:]:
        r.getparent().remove(r)


def _run(rpr_proto, text: str, line_break: bool = False, size: int | None = None):
    r = etree.Element(qn("w:r"))
    if rpr_proto is not None:
        rpr = copy.deepcopy(rpr_proto)
        if size:
            for tag in ("w:sz", "w:szCs"):
                el = rpr.find(qn(tag))
                if el is None:
                    el = etree.SubElement(rpr, qn(tag))
                el.set(qn("w:val"), str(size))
        r.append(rpr)
    if line_break:
        etree.SubElement(r, qn("w:br"))
    t = etree.SubElement(r, qn("w:t"))
    t.text = text
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    return r


def _paragraph(proto, lines: list[str], sub_size: int | None = None):
    """proto 의 문단 서식과 첫 run 서식을 따르는 새 문단. lines[1:] 은 같은 문단 안의 줄바꿈으로 넣는다."""
    p = etree.Element(qn("w:p"))
    ppr = proto.find(qn("w:pPr"))
    if ppr is not None:
        p.append(copy.deepcopy(ppr))
    first_run = proto.find(qn("w:r"))
    rpr = first_run.find(qn("w:rPr")) if first_run is not None else None
    for i, line in enumerate(lines):
        p.append(_run(rpr, line, line_break=i > 0, size=sub_size if i > 0 else None))
    return p


def _item_paragraph(proto, item: dict):
    lines = [item["head"]]
    for sub in item.get("subs", []):
        lines += wrap_sub_line(sub, SUB_LINE_WIDTH)
    return _paragraph(proto, lines, sub_size=SUB_FONT_HALF_PT)


def _fmt_price(value) -> str:
    return f"${float(value):.2f}" if str(value or "").strip() else ""


def check(data: dict) -> tuple[list[str], list[str]]:
    """(생성을 막는 오류, 경고). 통계 합계와 유가 설명의 방향을 입력값과 대조한다."""
    errors, warnings = [], []
    stats = data.get("stats") or {}
    # 빈 칸은 생성을 막지 않는다(빈 채로 나가고 경고만 한다). 숫자가 아닌 값만 막는다.
    blank = [label for key, label in STAT_FIELDS if str(stats.get(key) or "").strip() == ""]
    if blank:
        warnings.append(f"테러 통계 빈 칸 {len(blank)}곳 — 문서에도 빈 칸으로 들어감 (없으면 0 을 입력)")
    for key, label in STAT_FIELDS:
        v = str(stats.get(key) or "").strip()
        if v and not v.isdigit():
            errors.append(f"테러 통계 ‘{label}’ 는 0 이상의 정수여야 함")
    prices = data.get("oil_prices") or []
    if not prices:
        warnings.append("유가 표가 비어 있음 — 문서에도 빈 표로 들어감")
    complete = bool(prices)
    for row in prices:
        if not (row.get("date") or "").strip():
            warnings.append("유가 표에 날짜가 빈 행이 있음")
            complete = False
        for key, label in OIL_FIELDS:
            v = str(row.get(key) or "").strip()
            if not v:
                warnings.append(f"유가 {row.get('date') or '?'} ‘{label}’ 값이 비어 있음")
                complete = False
                continue
            try:
                if float(v) <= 0:
                    raise ValueError
            except ValueError:
                errors.append(f"유가 {row.get('date') or '?'} ‘{label}’ 값이 숫자가 아님")
    if not errors and complete and len(prices) >= 2:
        warnings += _oil_direction_warnings(data.get("oil", ""), prices)
    for key, label in (("politics", "정치권 동향"), ("impact", "그룹 / 건설에 미치는 영향")):
        if not (data.get(key) or "").strip():
            warnings.append(f"‘{label}’ 내용이 비어 있음")
    return errors, warnings


def _oil_direction_warnings(oil_text: str, prices: list[dict]) -> list[str]:
    """가격 설명의 '상승/하락'이 표의 전일 대비 변화와 어긋나면 알린다(단순 규칙)."""
    out = []
    by_date = {str(p["date"]).strip(): p for p in prices}
    order = [str(p["date"]).strip() for p in prices]
    for item in parse_items(oil_text):
        m = re.match(r"\s*(\d{1,2}\.\d{1,2})", item["head"])
        if not m or m.group(1) not in by_date or order.index(m.group(1)) == 0:
            continue
        cur, prev = by_date[m.group(1)], by_date[order[order.index(m.group(1)) - 1]]
        diffs = [float(cur[k]) - float(prev[k]) for k, _ in OIL_FIELDS]
        text = item["head"] + " ".join(item["subs"])
        if "상승" in text and "하락" not in text and all(d < 0 for d in diffs):
            out.append(f"유가 설명({m.group(1)})은 ‘상승’인데 표의 세 유종은 모두 전일보다 낮음")
        if "하락" in text and "상승" not in text and all(d > 0 for d in diffs):
            out.append(f"유가 설명({m.group(1)})은 ‘하락’인데 표의 세 유종은 모두 전일보다 높음")
    return out


def stats_total(stats: dict) -> int:
    return sum(int(stats[k]) for k, _ in STAT_FIELDS)


def _stats_total_text(stats: dict) -> str:
    """모든 칸이 채워졌을 때만 합계를 적는다. 빈 칸이 있으면 합계도 비운다."""
    try:
        return str(stats_total(stats))
    except (KeyError, ValueError, TypeError):
        return ""


def _find(children, text: str, start: int = 0) -> int:
    for i in range(start, len(children)):
        if children[i].tag == qn("w:p") and _text(children[i]).strip() == text:
            return i
    raise ValueError(f"원본 양식에서 ‘{text}’ 제목을 찾지 못함 — 양식이 바뀌었는지 확인 필요")


def _is_item(el) -> bool:
    """항목 문단(번호 목록 2단계) 또는 그에 딸린 번호 없는 이어짐 문단."""
    if el.tag != qn("w:p"):
        return False
    ilvl = el.find(f"{qn('w:pPr')}/{qn('w:numPr')}/{qn('w:ilvl')}")
    return ilvl is None or ilvl.get(qn("w:val")) == "1"


def _replace_between(body, start_el, stop_el, new_elements, keep=lambda el: False) -> None:
    el = start_el.getnext()
    while el is not None and el is not stop_el:
        nxt = el.getnext()
        if not keep(el):
            body.remove(el)
        el = nxt
    anchor = stop_el
    for new in new_elements:
        anchor.addprevious(new)


def generate(data: dict, out_path: Path, template: Path | None = None) -> list[str]:
    """data 키: period_start, period_end, report_date, politics, stats{}, terror, oil,
    oil_prices[{date,dubai,brent,wti}], intl[{title, body}], impact. 경고 목록을 돌려준다."""
    template = template or config.WEEKLY_TEMPLATE
    if not template.exists():
        raise FileNotFoundError(f"주간 Word 원본 양식이 없음: {template}")
    errors, warnings = check(data)
    if errors:
        raise WeeklyInputError(" / ".join(errors))
    doc = Document(str(template))
    body = doc.element.body
    ch = list(body.iterchildren())

    i_pol = _find(ch, H_POLITICS)
    i_ter = _find(ch, H_TERROR, i_pol)
    i_eco = _find(ch, H_ECONOMY, i_ter)
    i_oil = _find(ch, H_OIL, i_eco)
    i_intl = _find(ch, H_INTL, i_oil)
    i_imp = _find(ch, H_IMPACT, i_intl)
    tables = [el for el in ch if el.tag == qn("w:tbl")]
    if len(tables) != 2:
        raise ValueError("원본 양식의 표가 2개가 아님 — 양식이 바뀌었는지 확인 필요")
    stat_tbl, oil_tbl = tables
    item_proto = copy.deepcopy(next(el for el in ch[i_pol + 1:i_ter] if _is_item(el)
                                    and el.find(f"{qn('w:pPr')}/{qn('w:numPr')}") is not None))
    sub_heading_proto = copy.deepcopy(ch[i_intl + 1])
    impact_proto = copy.deepcopy(next(el for el in ch[i_imp + 1:] if el.tag == qn("w:p")))
    sect = body.find(qn("w:sectPr"))

    # 제목과 작성일
    start, end = date.fromisoformat(data["period_start"]), date.fromisoformat(data["period_end"])
    report = date.fromisoformat(data["report_date"])
    fmt = lambda d: f"΄{d.year % 100}.{d.month}.{d.day}"
    _set_runs_text(ch[0].findall(qn("w:r"))[1:], f"({fmt(start)} ~ {fmt(end)})")
    _set_runs_text(ch[1].findall(qn("w:r")), f"{report.year}. {report.month}. {report.day}.")

    def items(text):
        return [_item_paragraph(item_proto, it) for it in parse_items(text)]

    # 뒤에서부터 바꿔야 앞쪽 기준 문단이 흔들리지 않는다.
    impact_paras = [_paragraph(impact_proto, [it["head"] + "".join(" " + s for s in it["subs"])])
                    for it in parse_items(data.get("impact", ""))]
    _replace_between(body, ch[i_imp], sect, impact_paras, keep=lambda el: el.tag != qn("w:p"))

    intl_new = []
    for group in data.get("intl") or []:
        if not (group.get("title") or "").strip() and not (group.get("body") or "").strip():
            continue
        intl_new.append(_paragraph(sub_heading_proto, [(group.get("title") or "").strip() + " "]))
        intl_new += items(group.get("body", ""))
    _replace_between(body, ch[i_intl], ch[i_imp], intl_new)

    _replace_between(body, ch[i_oil], oil_tbl, items(data.get("oil", "")))
    _fill_oil_table(oil_tbl, data.get("oil_prices") or [])

    # 테러 표 뒤의 빈 문단 2개는 원본 간격이므로 남긴다.
    _replace_between(body, stat_tbl, ch[i_eco], items(data.get("terror", "")),
                     keep=lambda el: el.tag == qn("w:p") and not _text(el).strip())
    _fill_stats_table(stat_tbl, data.get("stats") or {})

    _replace_between(body, ch[i_pol], ch[i_ter], items(data.get("politics", "")))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return warnings


def _cell_set(tc, text: str) -> None:
    _set_runs_text(list(tc.iter(qn("w:r"))), text)


def _fill_stats_table(tbl, stats: dict) -> None:
    rows = tbl.findall(qn("w:tr"))
    header = [_text(tc).strip() for tc in rows[0].findall(qn("w:tc"))]
    expected = ["구분", "계"] + [label for _, label in STAT_FIELDS]
    if header != expected:
        raise ValueError(f"테러 통계 표의 열이 원본과 다름: {header}")
    cells = rows[1].findall(qn("w:tc"))
    _cell_set(cells[1], _stats_total_text(stats))
    for i, (key, _) in enumerate(STAT_FIELDS):
        v = str(stats.get(key) or "").strip()
        _cell_set(cells[i + 2], str(int(v)) if v else "")


def _fill_oil_table(tbl, prices: list[dict]) -> None:
    rows = tbl.findall(qn("w:tr"))
    data_rows = rows[1:]
    prices = prices or [{}, {}]                  # 가격을 안 넣었으면 원본처럼 두 줄을 빈 칸으로 둔다
    while len(data_rows) < len(prices):          # 행이 모자라면 마지막 행을 복제
        new = copy.deepcopy(data_rows[-1])
        data_rows[-1].addnext(new)
        data_rows.append(new)
    for extra in data_rows[len(prices):]:
        tbl.remove(extra)
    for tr, row in zip(data_rows, prices):
        cells = tr.findall(qn("w:tc"))
        _cell_set(cells[0], str(row.get("date") or "").strip())
        for i, (key, _) in enumerate(OIL_FIELDS):
            _cell_set(cells[i + 1], _fmt_price(row.get(key)))


def read_back(path: Path) -> dict:
    """생성한 파일에서 표 값과 본문을 다시 읽는다(검증용)."""
    doc = Document(str(path))
    stat, oil = doc.tables[0], doc.tables[1]
    uniq = lambda row: [c.text.strip() for c in row.cells]
    return {"paragraphs": [p.text for p in doc.paragraphs],
            "stats": dict(zip(uniq(stat.rows[0]), uniq(stat.rows[1]))),
            "oil": [uniq(r) for r in oil.rows]}
