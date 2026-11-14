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
# 2장 본문의 글자 크기(담당자가 정한 서식 규칙, 1/100 pt). 양식 파일의 상태와 무관하게 항상 이 크기로 쓴다.
HEAD_SIZE = 1200            # 제목(머리줄) 12pt
SOURCE_SIZE = 1100          # 머리줄 끝의 (출처, 날짜) 11pt
SUB_SIZE = 1100             # '*'·'☞' 로 시작하는 세부 내용 11pt


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
    # 목록 상자 안쪽에 놓인 표식이 '목록 옆 표식', 나머지가 '지도 위 표식'이다.
    markers, in_list = _list_markers(slide, list_box)
    if not markers:
        raise ValueError("원본 양식에서 번호 표식을 찾지 못함")
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
    head_rpr = rprs[0]                               # 글꼴·굵기·색은 양식의 서식을 복제하고, 크기만 규칙대로 지정한다
    head_size, sub_size = HEAD_SIZE, SUB_SIZE
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
        p.append(_make_run(head_rpr, " " + head.strip() + (" " if source else ""), head_size))
        if source:
            p.append(_make_run(head_rpr, source.strip(), SOURCE_SIZE))   # (출처, 날짜)는 11pt
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


_FONT_FILES = {   # (보통, 굵게) — 맑은 고딕이 있는 윈도우를 먼저, 그다음 맥·리눅스의 한글 글꼴
    False: ["C:/Windows/Fonts/malgun.ttf", "/System/Library/Fonts/AppleSDGothicNeo.ttc",
            "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"],
    True: ["C:/Windows/Fonts/malgunbd.ttf", "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
           "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"],
}


def _font(size_px: float, bold: bool):
    from PIL import ImageFont
    for path in (_FONT_FILES[True] if bold else []) + _FONT_FILES[False]:
        try:
            return ImageFont.truetype(path, max(6, round(size_px)))
        except OSError:
            continue
    return None                                     # 한글 글꼴이 없으면 글자는 그리지 않는다


def _rgba(fill_el):
    """solidFill 에서 색을 읽는다. 직접 적힌 색(srgbClr, sysClr, 검정/흰색)만 읽고 테마 색은 추측하지 않는다."""
    if fill_el is None:
        return None
    c = fill_el.find(_q("a:srgbClr"))
    val = c.get("val") if c is not None else None
    if val is None:
        c = fill_el.find(_q("a:sysClr"))
        val = c.get("lastClr") if c is not None else None
    if val is None:
        c = fill_el.find(_q("a:prstClr"))
        val = {"black": "000000", "white": "FFFFFF"}.get(c.get("val")) if c is not None else None
    if val is None:
        return None
    alpha = c.find(_q("a:alpha"))
    a = round(255 * int(alpha.get("val")) / 100000) if alpha is not None else 255
    return (int(val[0:2], 16), int(val[2:4], 16), int(val[4:6], 16), a)


def _bezier(p0, p1, p2, p3, n=12):
    out = []
    for i in range(1, n + 1):
        t = i / n
        u = 1 - t
        out.append((u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
                    u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1]))
    return out


def _path_points(cust, w: float, h: float) -> list[list[tuple[float, float]]]:
    """custGeom 경로를 도형 크기(w×h) 기준 좌표의 다각형들로 푼다."""
    polys = []
    for path in cust.findall(".//" + _q("a:path")):
        sx, sy = w / float(path.get("w") or w), h / float(path.get("h") or h)
        cur: list[tuple[float, float]] = []
        for seg in path:
            pts = [(float(pt.get("x")) * sx, float(pt.get("y")) * sy) for pt in seg.findall(_q("a:pt"))]
            name = etree.QName(seg).localname
            if name == "moveTo":
                if len(cur) > 2:
                    polys.append(cur)
                cur = pts[:1]
            elif name == "lnTo":
                cur += pts
            elif name == "cubicBezTo" and cur and len(pts) == 3:
                cur += _bezier(cur[-1], *pts)
        if len(cur) > 2:
            polys.append(cur)
    return polys


def _draw_text(layer, tx, box, scale: float) -> None:
    """도형 안 글자를 대략 같은 자리에 그린다(문단별 한 줄, 정렬·크기·굵기·색 반영)."""
    from PIL import ImageDraw
    body = tx.find(_q("a:bodyPr"))
    ins = [int(body.get(k, d)) * scale for k, d in (("lIns", 91440), ("tIns", 45720), ("rIns", 91440), ("bIns", 45720))]
    x0, y0, x1, y1 = box[0] + ins[0], box[1] + ins[1], box[2] - ins[2], box[3] - ins[3]
    lines = []
    for para in tx.findall(_q("a:p")):
        runs = para.findall(_q("a:r"))
        rpr = runs[0].find(_q("a:rPr")) if runs else para.find(_q("a:endParaRPr"))
        size_pt = int(rpr.get("sz", "1800")) / 100 if rpr is not None else 18
        text = "".join(r.findtext(_q("a:t")) or "" for r in runs)
        ppr = para.find(_q("a:pPr"))
        before = ppr.find(_q("a:spcBef") + "/" + _q("a:spcPts")) if ppr is not None else None
        gap = int(before.get("val")) / 100 * EMU_PER_PT * scale if before is not None else 0
        color = _rgba(rpr.find(_q("a:solidFill"))) if rpr is not None else None
        lines.append({"text": text, "px": size_pt * EMU_PER_PT * scale, "gap": gap,
                      "bold": rpr is not None and rpr.get("b") == "1", "color": color or (0, 0, 0, 255),
                      "align": ppr.get("algn", "l") if ppr is not None else "l"})
    while lines and not lines[-1]["text"].strip():
        lines.pop()
    if not lines:
        return
    lines[0]["gap"] = 0                             # 첫 문단의 '단락 앞' 간격은 적용되지 않는다
    total = sum(ln["px"] * 1.2 + ln["gap"] for ln in lines)
    y = y0 + (max(0, (y1 - y0 - total) / 2) if body.get("anchor") == "ctr" else 0)
    draw = ImageDraw.Draw(layer)
    for ln in lines:
        y += ln["gap"]
        font = _font(ln["px"], ln["bold"])
        if font is not None and ln["text"].strip():
            width = draw.textlength(ln["text"], font=font)
            x = x0 + (x1 - x0 - width) / 2 if ln["align"] == "ctr" else x0
            draw.text((x, y + ln["px"] * 0.6), ln["text"], font=font, fill=ln["color"], anchor="lm")
        y += ln["px"] * 1.2


def _draw_shape(canvas, el, to_slide, scale: float) -> None:
    """도형 하나(sp, cxnSp)를 그린다. 그리지 못하는 속성은 건너뛴다."""
    from PIL import Image, ImageDraw
    sp_pr = el.find(_q("p:spPr"))
    xfrm = sp_pr.find(_q("a:xfrm")) if sp_pr is not None else None
    if xfrm is None:
        return
    off, ext = xfrm.find(_q("a:off")), xfrm.find(_q("a:ext"))
    (l, t), (r, b) = to_slide(int(off.get("x")), int(off.get("y"))), to_slide(
        int(off.get("x")) + int(ext.get("cx")), int(off.get("y")) + int(ext.get("cy")))
    box = (l * scale, t * scale, r * scale, b * scale)
    w, h = box[2] - box[0], box[3] - box[1]
    fill = _rgba(sp_pr.find(_q("a:solidFill")))
    ln = sp_pr.find(_q("a:ln"))
    line = _rgba(ln.find(_q("a:solidFill"))) if ln is not None else None
    line_w = max(1, round(int(ln.get("w", "9525")) * scale)) if ln is not None else 1
    geom = sp_pr.find(_q("a:prstGeom"))
    kind = geom.get("prst") if geom is not None else None
    cust = sp_pr.find(_q("a:custGeom"))
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    if cust is not None:
        rot = math.radians(int(xfrm.get("rot", "0")) / 60000)
        cx, cy = w / 2, h / 2
        for poly in _path_points(cust, w, h):
            pts = [(box[0] + cx + (x - cx) * math.cos(rot) - (y - cy) * math.sin(rot),
                    box[1] + cy + (x - cx) * math.sin(rot) + (y - cy) * math.cos(rot)) for x, y in poly]
            draw.polygon(pts, fill=fill, outline=line)
    elif kind == "line":
        if line is None:
            return
        xs = (box[2], box[0]) if xfrm.get("flipH") == "1" else (box[0], box[2])
        ys = (box[3], box[1]) if xfrm.get("flipV") == "1" else (box[1], box[3])
        draw.line([(xs[0], ys[0]), (xs[1], ys[1])], fill=line, width=line_w)
    elif kind == "ellipse":
        draw.ellipse(box, fill=fill, outline=line, width=line_w if line else 0)
    elif kind in ("rect", "roundRect", "wedgeRectCallout"):
        if fill or line:
            radius = min(w, h) * 0.04 if kind == "roundRect" else 0
            draw.rounded_rectangle(box, radius=radius, fill=fill, outline=line, width=line_w if line else 0)
    else:
        return
    tx = el.find(_q("p:txBody"))
    if tx is not None:
        _draw_text(layer, tx, box, scale)
    canvas.alpha_composite(layer)


def compose_base_image(out_png: Path, template: Path | None = None, width: int = 1600, slide_index: int = 0) -> None:
    """LibreOffice 없이 만드는 지도 바탕 그림: 원본 1장의 그림과 도형을 쌓인 순서대로 그린다.

    지도·확대 지도, Kurds 영역, 범례, Baghdad·Bismayah 표시, 제목을 그리고
    날짜 상자·사건 목록 상자·번호 표식은 뺀다(편집 화면이 그 자리에 직접 보여준다).
    슬라이드 좌표를 그대로 따르므로 이 그림 위에서 지정한 표식 위치는 생성되는 PPT 의 위치와 같다.
    """
    import io

    from PIL import Image
    template = template or config.DAILY_TEMPLATE
    prs = Presentation(str(template))
    slide = prs.slides[slide_index]
    scale = width / prs.slide_width
    canvas = Image.new("RGBA", (width, round(prs.slide_height * scale)), (255, 255, 255, 255))
    if slide_index == 0:
        skip = {sh.shape_id for sh in slide.shapes if _is_number_marker(sh)} | {ID_DATE, ID_LIST}
    else:
        skip = {ID_POLITICS, ID_ECONOMY}                 # 2장: 본문 상자는 편집 화면이 직접 그린다

    def walk(parent, to_slide):
        for el in parent:
            name = etree.QName(el).localname
            cnv = el.find(".//" + _q("p:cNvPr"))
            if cnv is not None and int(cnv.get("id")) in skip:
                continue
            if name == "pic":
                sh = next(s for s in slide.shapes if s._element is el)
                img = Image.open(io.BytesIO(sh.image.blob)).convert("RGBA")
                w, h = img.size
                img = img.crop((round(w * sh.crop_left), round(h * sh.crop_top),
                                round(w * (1 - sh.crop_right)), round(h * (1 - sh.crop_bottom))))
                img = img.resize((max(1, round(sh.width * scale)), max(1, round(sh.height * scale))))
                canvas.alpha_composite(img, (round(sh.left * scale), round(sh.top * scale)))
            elif name in ("sp", "cxnSp"):
                _draw_shape(canvas, el, to_slide, scale)
            elif name == "grpSp":
                x = el.find(_q("p:grpSpPr")).find(_q("a:xfrm"))
                o, e, co, ce = (x.find(_q(t)) for t in ("a:off", "a:ext", "a:chOff", "a:chExt"))
                ox, oy, cox, coy = int(o.get("x")), int(o.get("y")), int(co.get("x")), int(co.get("y"))
                kx, ky = int(e.get("cx")) / int(ce.get("cx")), int(e.get("cy")) / int(ce.get("cy"))
                walk(el, lambda px, py, f=to_slide: f(ox + (px - cox) * kx, oy + (py - coy) * ky))

    walk(slide.shapes._spTree, lambda px, py: (px, py))
    out_png.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(out_png, "PNG")


def _list_markers(slide, list_box):
    """번호 표식 전체와, 그 가운데 사건 목록 상자 안쪽에 놓인 '목록 옆 표식'."""
    markers = [sh for sh in slide.shapes if _is_number_marker(sh)]
    in_list = [m for m in markers if list_box.left <= m.left <= list_box.left + 400000
               and list_box.top <= m.top <= list_box.top + list_box.height]
    return markers, in_list


def slide1_layout(template: Path | None = None) -> dict:
    """편집 화면이 날짜·사건 목록·표식을 실제 슬라이드와 같은 자리·크기로 보여주기 위한 배치 값.

    가로 값은 슬라이드 폭, 세로 위치는 슬라이드 높이에 대한 비율이고 크기(w, h, 글자)는 모두 폭에 대한 비율이다.
    """
    prs = Presentation(str(template or config.DAILY_TEMPLATE))
    slide = prs.slides[0]
    W, H = prs.slide_width, prs.slide_height
    date_box, list_box = _shape(slide, ID_DATE), _shape(slide, ID_LIST)
    markers, in_list = _list_markers(slide, list_box)
    ppr = list_box._element.find(".//" + _q("a:pPr"))
    mar_l, indent = int(ppr.get("marL", "0")), int(ppr.get("indent", "0"))
    date_sz = int(date_box._element.find(".//" + _q("a:rPr")).get("sz", "1300"))
    mk_sz = int(markers[0]._element.find(".//" + _q("a:rPr")).get("sz", "1100")) if markers else 1100
    return {
        "date": {"x": (date_box.left + 91440) / W, "y": date_box.top / H, "h": date_box.height / W,
                 "font": date_sz / 100 * EMU_PER_PT / W},
        "list": {"x": list_box.left / W, "y": list_box.top / H, "max_w": (W - list_box.left - LIST_RIGHT_MARGIN) / W,
                 "pad_x": 91440 / W, "pad_y": 45720 / W, "line_h": LIST_LINE_EMU / W, "font": LIST_FONT_EMU / W,
                 "text_x": (mar_l + indent) / W, "hang": -indent / W},
        "marker": {"w": markers[0].width / W if markers else 168147 / W,
                   "h": markers[0].height / W if markers else 164855 / W, "font": mk_sz / 100 * EMU_PER_PT / W,
                   "dx": ((in_list[0].left - list_box.left) if in_list else 92291) / W,
                   "dy": ((in_list[0].top - list_box.top) if in_list else 100108) / W},
    }


def ensure_base_image() -> str | None:
    """바탕 그림을 준비한다. LibreOffice 가 있으면 전체 렌더링을 한 번만, 없으면 켤 때마다 다시 합성한다
    (합성 방식이 바뀌어도 옛 그림이 남지 않게). 반환: 만든 방식 또는 None."""
    from . import preview
    if not config.DAILY_TEMPLATE.exists():
        return None
    if preview.available():
        if config.DAILY_BASE_IMAGE.exists():
            return None
        try:
            render_base_image(config.DAILY_BASE_IMAGE)
            return "rendered"
        except Exception:
            pass                       # 변환 도구가 일부만 있는 경우 등 → 합성으로 넘어간다
    compose_base_image(config.DAILY_BASE_IMAGE)
    return "composed"


def ensure_base_image2() -> None:
    """2장 미리보기의 바탕(제목과 구역 표지). 가볍게 합성하므로 켤 때마다 다시 만든다."""
    if config.DAILY_TEMPLATE.exists():
        compose_base_image(config.DAILY_BASE_IMAGE2, slide_index=1)


def slide2_layout(template: Path | None = None) -> dict:
    """편집 화면이 2장 본문을 실제 슬라이드와 같은 자리·서식으로 보여주기 위한 값. 생성기(_fill_body_box)와 같은 출처다.

    위치 x·w 와 크기(h, 글자, 여백)는 슬라이드 폭에 대한 비율, y 는 슬라이드 높이에 대한 비율이다.
    """
    prs = Presentation(str(template or config.DAILY_TEMPLATE))
    slide = prs.slides[1]
    W, H = prs.slide_width, prs.slide_height
    out = {}
    for key, shape_id in (("politics", ID_POLITICS), ("economy", ID_ECONOMY)):
        sh = _shape(slide, shape_id)
        tx = sh._element.find(_q("p:txBody"))
        body, para = tx.find(_q("a:bodyPr")), tx.find(_q("a:p"))
        ppr = para.find(_q("a:pPr"))
        head, sub = HEAD_SIZE, SUB_SIZE                  # 생성기와 같은 규칙 값
        pts = lambda path, default: int(e.get("val")) / 100 if (e := ppr.find(path)) is not None else default
        out[key] = {"x": sh.left / W, "y": sh.top / H, "w": sh.width / W, "h": sh.height / W,
                    "pad_l": int(body.get("lIns", "91440")) / W, "mar_l": int(ppr.get("marL", "0")) / W,
                    "head": head / 100 * EMU_PER_PT / W, "sub": sub / 100 * EMU_PER_PT / W,
                    "before": pts(_q("a:spcBef") + "/" + _q("a:spcPts"), 0) * EMU_PER_PT / W,
                    "after": pts(_q("a:spcAft") + "/" + _q("a:spcPts"), 0) * EMU_PER_PT / W,
                    "line": pts(_q("a:lnSpc") + "/" + _q("a:spcPct"), 100000) / 1000 * 1.2,
                    "center": body.get("anchor") == "ctr"}
    return out


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
