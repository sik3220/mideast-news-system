"""일일 PPTX 생성. 원본 파일을 열어 날짜·사건 목록·표식·본문만 바꾸고 나머지 도형은 그대로 둔다.

원본 구조(슬라이드 2장):
  1장  날짜 상자(id 28), 사건 목록 상자(id 29), 번호가 든 원형 표식(사건마다 지도 위 1개 + 목록 줄 옆 1개)
  2장  '정치 / 치안' 본문 상자(id 14), '경제 / 국제' 본문 상자(id 8)
"""
from __future__ import annotations

import copy
import math
import re
from datetime import date
from pathlib import Path

from lxml import etree
from pptx import Presentation

from .. import config
from .items import estimate_lines, parse_items, text_units

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS = {"a": A, "p": P}

ID_DATE, ID_LIST, ID_POLITICS, ID_ECONOMY = 28, 29, 14, 8
TYPE_COLORS = {"terror": "FF0000", "counter": "00B0F0"}   # 범례: 빨강=테러상황, 파랑=대테러작전
TYPE_LABELS = {"terror": "테러상황", "counter": "대테러작전"}
LIST_LINE_EMU = 222107          # 사건 목록 한 줄 높이(원본 1줄 상자 높이에서 안쪽 여백을 뺀 값)
LIST_FONT_EMU = 139700          # 11pt
LIST_RIGHT_MARGIN = 180000
EMU_PER_PT = 12700


def _q(tag: str) -> str:
    prefix, name = tag.split(":")
    return f"{{{NS[prefix]}}}{name}"


def _shape(slide, shape_id: int):
    for sh in slide.shapes:
        if sh.shape_id == shape_id:
            return sh
    raise ValueError(f"원본 양식에서 도형(id {shape_id})을 찾지 못함 — 양식이 바뀌었는지 확인 필요")


def _make_run(rpr_proto, text: str, size: int | None = None):
    r = etree.Element(_q("a:r"))
    rpr = copy.deepcopy(rpr_proto)
    rpr.tag = _q("a:rPr")
    for attr in ("err", "dirty"):
        rpr.attrib.pop(attr, None)
    if size:
        rpr.set("sz", str(size))
    r.append(rpr)
    t = etree.SubElement(r, _q("a:t"))
    t.text = text
    return r


def _is_number_marker(sh) -> bool:
    geom = sh._element.find(".//" + _q("a:prstGeom"))
    return (geom is not None and geom.get("prst") == "ellipse" and sh.has_text_frame
            and sh.text_frame.text.strip().isdigit())


def _set_marker(el, number: int, color: str, left: int, top: int, shape_id: int) -> None:
    cnv = el.find(".//" + _q("p:cNvPr"))
    cnv.set("id", str(shape_id))
    for ext in cnv.findall(_q("a:extLst")):
        cnv.remove(ext)  # 복제한 도형이 같은 creationId 를 갖지 않게 한다
    off = el.find(".//" + _q("a:off"))
    off.set("x", str(int(left)))
    off.set("y", str(int(top)))
    el.find(_q("p:spPr")).find(_q("a:solidFill")).find(_q("a:srgbClr")).set("val", color)
    el.find(".//" + _q("a:t")).text = str(number)


def date_label(d_from: str, d_to: str | None = None) -> str:
    """날짜 상자 문구. 하루면 '2026. 11. 9', 주말 합본이면 원본처럼 '2026. 11. 12 ~ 13'."""
    a = date.fromisoformat(d_from)
    if not d_to or d_to == d_from:
        return f"{a.year}. {a.month}. {a.day}"
    b = date.fromisoformat(d_to)
    tail = f"{b.day}" if (a.year, a.month) == (b.year, b.month) else f"{b.month}. {b.day}"
    return f"{a.year}. {a.month}. {a.day} ~ {tail}"


def _fill_slide1(prs, slide, data: dict, warnings: list[str]) -> None:
    d = date.fromisoformat(data["date"])
    date_box = _shape(slide, ID_DATE)
    runs = date_box._element.findall(".//" + _q("a:r"))
    runs[0].find(_q("a:t")).text = date_label(data["date"], data.get("date_to")) + " "
    for r in runs[1:]:
        r.getparent().remove(r)

    list_box = _shape(slide, ID_LIST)
    markers = [sh for sh in slide.shapes if _is_number_marker(sh)]
    if not markers:
        raise ValueError("원본 양식에서 번호 표식을 찾지 못함")
    # 목록 상자 안쪽에 놓인 표식이 '목록 옆 표식', 나머지가 '지도 위 표식'이다.
    in_list = [m for m in markers if list_box.left <= m.left <= list_box.left + 400000
               and list_box.top <= m.top <= list_box.top + list_box.height]
    proto = copy.deepcopy((in_list or markers)[0]._element)
    marker_w, marker_h = markers[0].width, markers[0].height
    dx = (in_list[0].left - list_box.left) if in_list else 92291
    dy = (in_list[0].top - list_box.top) if in_list else 100108
    sp_tree = slide.shapes._spTree
    for m in markers:
        sp_tree.remove(m._element)

    events = [e for e in data.get("events", []) if (e.get("text") or "").strip()]
    tx = list_box._element.find(_q("p:txBody"))
    paras = tx.findall(_q("a:p"))
    para_proto = copy.deepcopy(paras[0])
    for p in paras:
        tx.remove(p)
    run_proto = para_proto.find(_q("a:r")).find(_q("a:rPr"))
    ppr = para_proto.find(_q("a:pPr"))
    end_proto = para_proto.find(_q("a:endParaRPr"))
    mar_l = int(ppr.get("marL", "0"))

    lines = [" ".join(x for x in ((e.get("day") or "").strip(), (e.get("time") or "").strip(), e["text"].strip()) if x)
             for e in events]
    if not events:
        lines = [(data.get("no_event_text") or "").strip()]
    max_width = prs.slide_width - list_box.left - LIST_RIGHT_MARGIN
    inner_max = max_width - mar_l - 91440 * 2
    needed = max((text_units(ln) * LIST_FONT_EMU for ln in lines), default=0)
    list_box.width = int(min(max_width, max(1500000, needed + mar_l + 91440 * 2 + 80000)))
    row_counts = [max(1, math.ceil(text_units(ln) * LIST_FONT_EMU / inner_max)) for ln in lines]
    list_box.height = 91440 + LIST_LINE_EMU * sum(row_counts)
    for i, rows in enumerate(row_counts, start=1):
        if rows > 1 and events:
            warnings.append(f"사건 {i}: 문장이 길어 줄바꿈 예상 — 아래 사건의 목록 표식 위치가 어긋날 수 있으니 "
                            "문장을 줄이거나 생성 후 확인")

    for line in lines:
        p = etree.SubElement(tx, _q("a:p"))
        p.append(copy.deepcopy(ppr))
        if not events:
            p.find(_q("a:pPr")).set("marL", "0")
            p.find(_q("a:pPr")).set("indent", "0")
        p.append(_make_run(run_proto, line))
        if end_proto is not None:
            p.append(copy.deepcopy(end_proto))

    next_id = max(sh.shape_id for sh in slide.shapes) + 1
    row = 0
    for i, e in enumerate(events, start=1):
        color = TYPE_COLORS.get(e.get("type"), TYPE_COLORS["terror"])
        side = copy.deepcopy(proto)
        _set_marker(side, i, color, list_box.left + dx, list_box.top + dy + row * LIST_LINE_EMU, next_id)
        sp_tree.append(side)
        next_id += 1
        row += row_counts[i - 1]
        if e.get("x") is None or e.get("y") is None:
            warnings.append(f"사건 {i}: 지도 위치가 지정되지 않아 지도 표식을 넣지 않음")
            continue
        on_map = copy.deepcopy(proto)
        _set_marker(on_map, i, color, float(e["x"]) * prs.slide_width - marker_w / 2,
                    float(e["y"]) * prs.slide_height - marker_h / 2, next_id)
        sp_tree.append(on_map)
        next_id += 1
    if list_box.top + list_box.height > prs.slide_height - 100000:
        warnings.append("사건 목록이 슬라이드 아래쪽을 벗어남 — 사건 수나 문장 길이를 줄여야 함")


_SOURCE_TAIL = re.compile(r"^(.*?)(\s*\([^()]*\))\s*$")


def _fill_body_box(shape, text: str, label: str, warnings: list[str]) -> None:
    tx = shape._element.find(_q("p:txBody"))
    paras = tx.findall(_q("a:p"))
    proto = copy.deepcopy(paras[0])
    for p in paras:
        tx.remove(p)
    ppr = proto.find(_q("a:pPr"))
    rprs = [r.find(_q("a:rPr")) for r in proto.findall(_q("a:r"))]
    head_rpr = rprs[0]
    head_size = int(head_rpr.get("sz", "1200"))
    sub_size = next((int(r.get("sz")) for r in rprs if r.get("sz") and int(r.get("sz")) < head_size), head_size - 100)
    br_proto = proto.find(_q("a:br"))
    end_proto = proto.find(_q("a:endParaRPr"))

    items = parse_items(text)
    width_pt = (shape.width - int(ppr.get("marL", "0")) - 40000) / EMU_PER_PT
    height_pt = 0.0
    for it in items:
        p = etree.SubElement(tx, _q("a:p"))
        p.append(copy.deepcopy(ppr))
        m = _SOURCE_TAIL.match(it["head"])
        head, source = (m.group(1), m.group(2)) if m else (it["head"], "")
        p.append(_make_run(head_rpr, " " + head.strip() + (" " if source else "")))
        if source:
            p.append(_make_run(head_rpr, source.strip(), sub_size))   # 원본: (출처, 날짜)는 한 단계 작은 글자
        head_lines = estimate_lines(it["head"], head_size / 100, width_pt)
        height_pt += head_lines * head_size / 100 * 1.8 + 18        # 줄간격 150% + 문단 앞 12pt·뒤 6pt
        for sub in it["subs"]:
            br = copy.deepcopy(br_proto) if br_proto is not None else etree.Element(_q("a:br"))
            p.append(br)
            p.append(_make_run(head_rpr, "  " + sub, sub_size))
            height_pt += estimate_lines("  " + sub, sub_size / 100, width_pt) * sub_size / 100 * 1.8
        if end_proto is not None:
            p.append(copy.deepcopy(end_proto))
    if not items:
        p = etree.SubElement(tx, _q("a:p"))
        p.append(copy.deepcopy(ppr))
        if end_proto is not None:
            p.append(copy.deepcopy(end_proto))
    box_pt = shape.height / EMU_PER_PT
    if height_pt > box_pt:
        warnings.append(f"‘{label}’ 분량 초과 예상: 약 {height_pt:.0f}pt / 상자 {box_pt:.0f}pt — "
                        "글자 크기는 줄이지 않았으니 내용을 줄이거나 생성 후 직접 조정")


def generate(data: dict, out_path: Path, template: Path | None = None) -> list[str]:
    """data: {date, events:[{time,text,place,type,x,y}], no_event_text, politics, economy}. 경고 목록을 돌려준다."""
    template = template or config.DAILY_TEMPLATE
    if not template.exists():
        raise FileNotFoundError(f"일일 PPT 원본 양식이 없음: {template}")
    prs = Presentation(str(template))
    if len(prs.slides) != 2:
        raise ValueError("원본 양식이 2장이 아님 — 양식이 바뀌었는지 확인 필요")
    warnings: list[str] = []
    _fill_slide1(prs, prs.slides[0], data, warnings)
    _fill_body_box(_shape(prs.slides[1], ID_POLITICS), data.get("politics", ""), "정치 / 치안", warnings)
    _fill_body_box(_shape(prs.slides[1], ID_ECONOMY), data.get("economy", ""), "경제 / 국제", warnings)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out_path))
    return warnings


def read_markers(path: Path) -> dict:
    """생성한 파일에서 사건 목록과 표식 번호를 다시 읽는다(번호 일치 검증용)."""
    prs = Presentation(str(path))
    slide = prs.slides[0]
    list_box = _shape(slide, ID_LIST)
    side, on_map = [], []
    for sh in slide.shapes:
        if not _is_number_marker(sh):
            continue
        n = int(sh.text_frame.text.strip())
        color = sh._element.find(_q("p:spPr")).find(_q("a:solidFill")).find(_q("a:srgbClr")).get("val")
        inside = (list_box.left <= sh.left <= list_box.left + 400000
                  and list_box.top <= sh.top <= list_box.top + list_box.height)
        (side if inside else on_map).append({"number": n, "color": color, "left": sh.left, "top": sh.top})
    return {"lines": [p.text for p in list_box.text_frame.paragraphs], "side": side, "map": on_map,
            "date": _shape(slide, ID_DATE).text_frame.text,
            "politics": _shape(prs.slides[1], ID_POLITICS).text_frame.text,
            "economy": _shape(prs.slides[1], ID_ECONOMY).text_frame.text}


def compose_base_image(out_png: Path, template: Path | None = None, width: int = 1600) -> None:
    """LibreOffice 없이 만드는 지도 바탕 그림: 원본 1장의 그림(지도·확대 지도·로고)을 슬라이드 좌표대로 붙인다.

    범례·Kurds 영역 같은 도형은 그려지지 않지만, 그림의 위치와 자르기를 그대로 따르므로
    이 그림 위에서 지정한 표식 위치는 생성되는 PPT 의 위치와 같다.
    """
    import io

    from PIL import Image
    template = template or config.DAILY_TEMPLATE
    prs = Presentation(str(template))
    slide = prs.slides[0]
    scale = width / prs.slide_width
    canvas = Image.new("RGB", (width, round(prs.slide_height * scale)), "white")
    # 큰 그림(지도)을 먼저, 작은 그림(확대 지도)을 나중에 붙여 원본처럼 위에 오게 한다.
    pictures = sorted((sh for sh in slide.shapes if sh.shape_type == 13), key=lambda sh: -(sh.width * sh.height))
    for sh in pictures:
        img = Image.open(io.BytesIO(sh.image.blob)).convert("RGB")
        w, h = img.size
        box = (round(w * sh.crop_left), round(h * sh.crop_top), round(w * (1 - sh.crop_right)), round(h * (1 - sh.crop_bottom)))
        img = img.crop(box).resize((max(1, round(sh.width * scale)), max(1, round(sh.height * scale))))
        canvas.paste(img, (round(sh.left * scale), round(sh.top * scale)))
    out_png.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_png, "PNG")


def ensure_base_image() -> str | None:
    """바탕 그림이 없으면 만든다. LibreOffice 가 있으면 전체 렌더링, 없으면 그림 합성. 반환: 만든 방식 또는 None."""
    from . import preview
    if config.DAILY_BASE_IMAGE.exists() or not config.DAILY_TEMPLATE.exists():
        return None
    if preview.available():
        try:
            render_base_image(config.DAILY_BASE_IMAGE)
            return "rendered"
        except Exception:
            pass                       # 변환 도구가 일부만 있는 경우 등 → 합성으로 넘어간다
    compose_base_image(config.DAILY_BASE_IMAGE)
    return "composed"


def render_base_image(out_png: Path, template: Path | None = None) -> None:
    """지도 위치 지정 화면에 쓸 바탕 그림: 1장에서 날짜·사건 목록·번호 표식을 뺀 모습."""
    import tempfile

    from . import preview
    template = template or config.DAILY_TEMPLATE
    prs = Presentation(str(template))
    slide = prs.slides[0]
    for sh in list(slide.shapes):
        if _is_number_marker(sh) or sh.shape_id == ID_LIST:
            slide.shapes._spTree.remove(sh._element)
    _shape(slide, ID_DATE).text_frame.paragraphs[0].runs[0].text = " "
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "base.pptx"
        prs.save(str(src))
        pages = preview.to_png(preview.to_pdf(src), dpi=160)
        out_png.write_bytes(pages[0].read_bytes())
