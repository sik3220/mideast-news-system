"""로컬 개인용 대시보드. 실행: uvicorn app.main:app --host 127.0.0.1 --port 8000"""
from __future__ import annotations

import json
import threading
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import config, db, demo, draft, enrich, llm, pipeline, timeutil
from .docs import daily_ppt, preview, weekly_word
from .docs.items import items_to_text

BASE = Path(__file__).resolve().parent
app = FastAPI(title="이라크·중동 업무 뉴스 대시보드", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.filters["when"] = lambda a: timeutil.display(a["published_at"], a["published_date"], a["time_precision"])
templates.env.filters["md"] = lambda d: timeutil.md(d) if d else "날짜 미상"
templates.env.filters["local"] = lambda s: (timeutil.parse_iso(s).astimezone(timeutil.BAGHDAD).strftime("%m.%d %H:%M")
                                            if s else "없음")
LANG_NAMES = {"ko": "한국어", "en": "영어", "ar": "아랍어"}
CATEGORIES = ["정치", "치안", "테러", "경제", "국제", "기타"]


def render(request: Request, name: str, **ctx):
    cfg = config.topics_config()
    ctx.update(demo=config.demo_mode(), llm_ready=config.llm_configured(), llm_model=config.llm_model(),
               topics={k: v["label"] for k, v in cfg["topics"].items()}, categories=CATEGORIES,
               regions=list(cfg["regions"]) + ["기타 중동"], lang_names=LANG_NAMES,
               preview_ready=preview.available())
    return templates.TemplateResponse(request, name, ctx)


def period(request: Request) -> tuple[str, str]:
    today = timeutil.today_baghdad()
    d_to = request.query_params.get("to") or today.isoformat()
    d_from = request.query_params.get("from") or (date.fromisoformat(d_to) - timedelta(days=1)).isoformat()
    try:
        if date.fromisoformat(d_from) > date.fromisoformat(d_to):
            d_from = d_to
    except ValueError:
        raise HTTPException(400, "날짜 형식이 올바르지 않음")
    return d_from, d_to


def back(path: str, **params) -> RedirectResponse:
    query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
    return RedirectResponse(f"{path}?{query}" if query else path, status_code=303)


@app.on_event("startup")
def startup() -> None:
    with db.session() as conn:
        if config.demo_mode():
            demo.seed(conn)
        # 추천 규칙(config/topics.yaml)을 고친 뒤 다시 켜면 저장된 기사에도 바로 반영되게 한다.
        pipeline.recompute(conn, config.demo_mode())
        llm.recheck_flags(conn)


# ───────────────────────── 대시보드 ─────────────────────────

@app.get("/")
def index(request: Request):
    d_from, d_to = period(request)
    q = request.query_params
    filters = {"q": q.get("q", ""), "category": q.get("category", ""), "region": q.get("region", ""),
               "topic": q.get("topic", ""), "selected": q.get("selected", ""),
               "undated": (q.getlist("undated") or ["1"])[-1]}
    with db.session() as conn:
        show_all = q.get("all") == "1"
        articles, matched, trimmed = pipeline.dashboard_list(conn, d_from, d_to, filters, show_all, config.demo_mode())
        run = None if config.demo_mode() else pipeline.last_run(conn)
        total = conn.execute("SELECT COUNT(*) c FROM articles WHERE is_demo=?", (int(config.demo_mode()),)).fetchone()["c"]
        selected_count = conn.execute("SELECT COUNT(*) c FROM articles WHERE selected=1 AND is_demo=?",
                                      (int(config.demo_mode()),)).fetchone()["c"]
    unsummarized = sum(llm.needs_summary(a) for a in articles)
    stale = sum(a["summary_status"] == "ok" and llm.needs_summary(a) for a in articles)
    return render(request, "index.html", d_from=d_from, d_to=d_to, f=filters, articles=articles, run=run,
                  sections=pipeline.sections(articles), stale=stale,
                  auto=bool(unsummarized) and config.llm_configured() and config.auto_summarize()
                  and q.get("noauto") != "1" and not show_all,   # 전체 보기는 수백 건이라 자동으로 돌리지 않는다
                  total=total, selected_count=selected_count, unsummarized=unsummarized,
                  msg=q.get("msg", ""), batch=llm.MAX_PER_BATCH, matched=matched, trimmed=trimmed,
                  show_all=show_all, narrowed=any(filters[k] for k in pipeline.NARROWING),
                  qs=urlencode({"from": d_from, "to": d_to, **{k: v for k, v in filters.items() if v},
                                **({"all": "1"} if show_all else {})}))


@app.post("/refresh")
def refresh(d_from: str = Form(...), d_to: str = Form(...), q: str = Form("")):
    if config.demo_mode():
        return back("/", **{"from": d_from, "to": d_to, "msg": "데모 모드에서는 실제 수집을 하지 않습니다."})
    with db.session() as conn:
        run_id = pipeline.run_collection(conn, d_from, d_to, q)
        run = conn.execute("SELECT status, new_count FROM runs WHERE id=?", (run_id,)).fetchone()
        msg = f"{pipeline.STATUS_TEXT[run['status']]} · 새 기사 {run['new_count']}건"
        if config.llm_configured() and config.auto_summarize() and run["status"] in ("ok", "partial"):
            # 첫 화면에 나올 추천분만 바로 한국어로 만들고 같은 사건을 묶는다.
            msg += " · " + enrich.message(enrich.enrich_view(conn, d_from, d_to, {}, False))
    return back("/", **{"from": d_from, "to": d_to, "q": q, "msg": msg})


@app.post("/summarize")
async def summarize(request: Request):
    form = await request.form()
    params = {k: form.get(k, "") for k in ("from", "to", "q", "category", "region", "topic", "selected", "all")}
    if not config.llm_configured():
        return back("/", **params, msg="요약 설정 필요: ANTHROPIC_API_KEY 가 없습니다.")
    with db.session() as conn:
        # 화면에 보이는 목록만 처리한다(추천 보기에서는 추천분만).
        total = enrich.enrich_view(conn, params["from"], params["to"], params, params["all"] == "1",
                                   config.demo_mode())
    return back("/", **params, msg=enrich.message(total))


_enrich_lock = threading.Lock()


@app.post("/api/enrich")
async def api_enrich(request: Request):
    """화면이 열릴 때 스스로 부른다: 보이는 기사 중 미번역·예전 문체를 번역하고 중복을 묶는다."""
    body = await request.json()
    filters = {k: str(body.get(k) or "") for k in ("q", "category", "region", "topic", "selected")}
    d_from, d_to = str(body.get("from")), str(body.get("to"))
    date.fromisoformat(d_from), date.fromisoformat(d_to)
    if not config.llm_configured():
        return JSONResponse({"status": "unconfigured"})
    if not _enrich_lock.acquire(blocking=False):
        return JSONResponse({"status": "busy"})          # 다른 창에서 이미 처리 중
    try:
        def work():
            with db.session() as conn:
                return enrich.enrich_view(conn, d_from, d_to, filters, bool(body.get("all")), config.demo_mode())
        total = await run_in_threadpool(work)
    finally:
        _enrich_lock.release()
    return JSONResponse({"status": "done", "changed": total["ok"] + total["merged"], "remaining": total["remaining"],
                         "message": enrich.message(total)})


@app.post("/api/article/{article_id}/select")
async def select(article_id: int, request: Request):
    body = await request.json()
    with db.session() as conn:
        cur = conn.execute("UPDATE articles SET selected=? WHERE id=?", (int(bool(body.get("selected"))), article_id))
        if not cur.rowcount:
            raise HTTPException(404)
        count = conn.execute("SELECT COUNT(*) c FROM articles WHERE selected=1 AND is_demo=?",
                             (int(config.demo_mode()),)).fetchone()["c"]
    return JSONResponse({"ok": True, "selected_count": count})


@app.post("/events/merge")
def events_merge(d_from: str = Form(...), d_to: str = Form(...)):
    """추천 화면에서 체크한 기사들을 같은 사건으로 직접 묶는다."""
    with db.session() as conn:
        ids = [a["id"] for a in _selected(conn, d_from, d_to)]
        if len(ids) < 2:
            return back("/", **{"from": d_from, "to": d_to, "msg": "묶으려면 이 기간의 기사를 2건 이상 체크하세요."})
        for a in ids:                                  # 직접 묶으면 예전에 떼어 놓은 기록은 지운다
            for b in ids:
                if a < b:
                    conn.execute("DELETE FROM event_splits WHERE a=? AND b=?", (a, b))
        merged = pipeline.merge_events(conn, ids, "직접 묶음")
        pipeline.recompute(conn, config.demo_mode())
    msg = f"체크한 기사 {len(ids)}건을 같은 사건으로 묶었습니다." if merged else "이미 같은 사건으로 묶여 있습니다."
    return back("/", **{"from": d_from, "to": d_to, "msg": msg})


@app.post("/article/{article_id}/split")
def article_split(article_id: int, back_qs: str = Form("")):
    with db.session() as conn:
        if not db.get_article(conn, article_id):
            raise HTTPException(404)
        pipeline.split_article(conn, article_id)
        pipeline.recompute(conn, config.demo_mode())
    return back(f"/article/{article_id}", back=back_qs, msg="묶음에서 뺐습니다. 자동 묶기가 다시 합치지 않습니다.")


@app.get("/article/{article_id}")
def article_page(article_id: int, request: Request):
    with db.session() as conn:
        a = db.get_article(conn, article_id)
        if not a:
            raise HTTPException(404, "기사를 찾을 수 없음")
        group = [db.row_to_article(r) for r in conn.execute(
            "SELECT * FROM articles WHERE event_id=? AND id<>? ORDER BY published_at", (a["event_id"], a["id"]))]
    from . import dedupe, ranking
    n = dedupe.independent_sources(group + [a])
    a["verification"] = ranking.verification(a, n)
    return render(request, "article.html", a=a, related=group, back_qs=request.query_params.get("back", ""),
                  msg=request.query_params.get("msg", ""))


@app.post("/article/{article_id}/summarize")
def article_summarize(article_id: int, back_qs: str = Form("")):
    with db.session() as conn:
        a = db.get_article(conn, article_id)
        if not a:
            raise HTTPException(404)
        result = llm.summarize_article(conn, a, force=True)
        pipeline.recompute(conn, config.demo_mode())
    msg = {"ok": "요약을 만들었습니다.", "failed": "요약 실패 — 원문 정보는 그대로 있습니다.",
           "unconfigured": "요약 설정 필요: ANTHROPIC_API_KEY 가 없습니다."}.get(result, result)
    return back(f"/article/{article_id}", back=back_qs, msg=msg)


@app.post("/article/{article_id}/edit")
def article_edit(article_id: int, ko_title: str = Form(""), ko_summary: str = Form(""), category: str = Form(""),
                 note: str = Form(""), back_qs: str = Form("")):
    """사용자가 한국어 제목·요약·분류·메모를 직접 고친다."""
    with db.session() as conn:
        conn.execute("UPDATE articles SET ko_title=?, ko_summary=?, note=? WHERE id=?",
                     (ko_title.strip() or None, ko_summary.strip() or None, note.strip(), article_id))
        if ko_title.strip():
            # 사용자가 직접 쓴 한국어 제목은 요약이 있는 것으로 취급한다(재요약으로 덮어쓰지 않게).
            conn.execute("UPDATE articles SET summary_status='ok', summary_basis=COALESCE(summary_basis,'사용자 작성') "
                         "WHERE id=? AND summary_status<>'ok'", (article_id,))
            conn.execute("UPDATE articles SET summary_ver=999 WHERE id=?", (article_id,))   # 다시 요약해 덮어쓰지 않게
        if category in CATEGORIES:
            conn.execute("UPDATE articles SET category=? WHERE id=? AND summary_status='ok'", (category, article_id))
        pipeline.recompute(conn, config.demo_mode())
    return back(f"/article/{article_id}", back=back_qs, msg="저장했습니다.")


# ───────────────────────── 보고서 초안 ─────────────────────────

def _selected(conn, d_from: str, d_to: str) -> list[dict]:
    listed = pipeline.list_articles(conn, d_from, d_to, selected_only=True, demo=config.demo_mode())
    flat = []
    for a in listed:
        flat.append(a)
        flat += a["related"]
    return flat


@app.get("/draft")
def draft_page(request: Request):
    d_from, d_to = period(request)
    key = f"{d_from}~{d_to}"
    with db.session() as conn:
        selected = _selected(conn, d_from, d_to)
        current = draft.load(conn, key)
        history = draft.versions(conn, key)
    return render(request, "draft.html", d_from=d_from, d_to=d_to, selected=selected, draft=current,
                  history=history, msg=request.query_params.get("msg", ""))


@app.post("/draft/generate")
def draft_generate(d_from: str = Form(...), d_to: str = Form(...), confirm: str = Form("")):
    key = f"{d_from}~{d_to}"
    with db.session() as conn:
        current = draft.load(conn, key)
        if current and current["content"].strip() and not confirm:
            return back("/draft", **{"from": d_from, "to": d_to,
                                     "msg": "이미 편집한 초안이 있습니다. 다시 만들려면 확인란을 선택하세요(기존 내용은 이력에 보존됩니다)."})
        selected = _selected(conn, d_from, d_to)
        if not selected:
            return back("/draft", **{"from": d_from, "to": d_to, "msg": "선택한 기사가 없습니다."})
        run = pipeline.last_run(conn)
        failed = [s["label"] for s in (run or {}).get("sources", []) if s["status"] == "failed"]
        text = draft.build_report(selected, d_from, d_to, failed)
        draft.save(conn, key, text, [a["id"] for a in selected], reason="재생성 전 보존")
    return back("/draft", **{"from": d_from, "to": d_to, "msg": "초안을 만들었습니다."})


@app.post("/draft/save")
def draft_save(d_from: str = Form(...), d_to: str = Form(...), content: str = Form("")):
    with db.session() as conn:
        draft.save(conn, f"{d_from}~{d_to}", content.replace("\r\n", "\n"))
    return back("/draft", **{"from": d_from, "to": d_to, "msg": "저장했습니다."})


# ───────────────────────── 문서 생성 공통 ─────────────────────────

def _history(conn, kind: str, key: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM generated_files WHERE kind=? AND key=? ORDER BY id DESC LIMIT 10", (kind, key))
    return [dict(r) | {"warnings": json.loads(r["warnings"])} for r in rows]


def _record(conn, kind: str, key: str, filename: str, version: str, warnings: list[str]) -> None:
    conn.execute("INSERT INTO generated_files (kind, key, filename, template_version, created_at, warnings) "
                 "VALUES (?,?,?,?,?,?)", (kind, key, filename, version, timeutil.iso(timeutil.now_utc()),
                                          json.dumps(warnings, ensure_ascii=False)))


def _generated_dir() -> Path:
    d = config.data_dir() / "generated"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _file_row(file_id: int) -> tuple[dict, Path]:
    with db.session() as conn:
        row = conn.execute("SELECT * FROM generated_files WHERE id=?", (file_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    path = _generated_dir() / row["filename"]
    if not path.exists():
        raise HTTPException(404, "생성 파일이 지워졌습니다. 다시 생성하세요.")
    return dict(row), path


@app.get("/files/{file_id}")
def download(file_id: int):
    row, path = _file_row(file_id)
    d = date.fromisoformat(row["key"][:10])
    if row["kind"] == "daily":
        name = f"일일_정세현황_{d.month}.{d.day}.pptx"
    else:
        r = date.fromisoformat(row["key"][:10]) + timedelta(days=7)
        name = f"주간_종합_상황보고_{r.month}.{r.day}.docx"
    return FileResponse(path, filename=name)


@app.get("/files/{file_id}/preview")
def file_preview(file_id: int):
    _, path = _file_row(file_id)
    try:
        pdf = path.with_suffix(".pdf")
        if not pdf.exists():
            pdf = preview.to_pdf(path)
    except preview.PreviewUnavailable as e:
        raise HTTPException(503, f"미리보기 제한: {e}. 파일을 내려받아 확인하세요.")
    return FileResponse(pdf, media_type="application/pdf", content_disposition_type="inline")


def _article_item(a: dict, with_source: bool) -> dict:
    day = timeutil.md(a["published_date"]) if a.get("published_date") else "날짜 미상"
    head = f"{day}, {a.get('ko_title') or a['title']}"
    if with_source:
        head += f" ({a.get('source_name') or a.get('source_domain')}, {day})"
    return {"head": head, "subs": [f"* {a['ko_summary']}"] if a.get("ko_summary") else []}


# ───────────────────────── 일일 PPT ─────────────────────────

def _daily_default() -> dict:
    return {"events": [], "no_event_text": "특이사항 없음", "politics": "", "economy": ""}


@app.get("/daily")
def daily_page(request: Request):
    day = request.query_params.get("date") or timeutil.today_baghdad().isoformat()
    with db.session() as conn:
        data = db.get_edition(conn, "daily", day) or _daily_default()
        saved = db.get_edition(conn, "daily", day) is not None
        history = _history(conn, "daily", day)
        locations = {r["name"]: {"x": r["x"], "y": r["y"]} for r in conn.execute("SELECT * FROM locations")}
        selected = _selected(conn, day, day)
    return render(request, "daily.html", day=day, data=data, saved=saved, history=history, locations=locations,
                  selected_count=len(selected), template_ok=config.DAILY_TEMPLATE.exists(),
                  base_ok=config.DAILY_BASE_IMAGE.exists(), type_labels=daily_ppt.TYPE_LABELS,
                  type_colors=daily_ppt.TYPE_COLORS, msg=request.query_params.get("msg", ""),
                  version=config.DAILY_TEMPLATE_VERSION)


def _daily_from_form(form) -> dict:
    try:
        events = json.loads(form.get("events_json") or "[]")
    except json.JSONDecodeError:
        raise HTTPException(400, "사건 목록 형식 오류")
    clean = []
    for e in events:
        if not isinstance(e, dict):
            continue
        item = {"time": str(e.get("time") or "")[:5], "place": str(e.get("place") or "")[:60],
                "text": str(e.get("text") or "")[:200],
                "type": e.get("type") if e.get("type") in daily_ppt.TYPE_COLORS else "terror"}
        if isinstance(e.get("x"), (int, float)) and isinstance(e.get("y"), (int, float)):
            item["x"], item["y"] = min(1, max(0, float(e["x"]))), min(1, max(0, float(e["y"])))
        clean.append(item)
    return {"events": clean, "no_event_text": (form.get("no_event_text") or "").strip(),
            "politics": (form.get("politics") or "").replace("\r\n", "\n"),
            "economy": (form.get("economy") or "").replace("\r\n", "\n")}


@app.get("/daily/base.png")
def daily_base():
    if not config.DAILY_BASE_IMAGE.exists():
        raise HTTPException(404)
    return FileResponse(config.DAILY_BASE_IMAGE)


@app.post("/daily/save")
async def daily_save(request: Request):
    form = await request.form()
    day, action = form.get("date"), form.get("action", "save")
    date.fromisoformat(day)
    data = _daily_from_form(form)
    msg = "저장했습니다."
    with db.session() as conn:
        if action == "import":
            pol, eco = [], []
            for a in _selected(conn, day, day):
                (pol if a["category"] in ("정치", "치안", "테러") else eco).append(_article_item(a, with_source=True))
            if not pol and not eco:
                msg = "이 날짜에 선택한 기사가 없습니다."
            else:
                data["politics"] = "\n\n".join(filter(None, [data["politics"].strip(), items_to_text(pol)]))
                data["economy"] = "\n\n".join(filter(None, [data["economy"].strip(), items_to_text(eco)]))
                msg = f"선택한 기사 {len(pol) + len(eco)}건을 본문 끝에 덧붙였습니다. 문장을 다듬어 주세요."
        db.save_edition(conn, "daily", day, data)
        for e in data["events"]:
            if e["place"] and "x" in e:
                conn.execute("INSERT INTO locations (name, x, y, updated_at) VALUES (?,?,?,?) ON CONFLICT(name) "
                             "DO UPDATE SET x=excluded.x, y=excluded.y, updated_at=excluded.updated_at",
                             (e["place"], e["x"], e["y"], timeutil.iso(timeutil.now_utc())))
        if action == "generate":
            payload = dict(data, date=day)
            for e in payload["events"]:
                e.setdefault("x", None)
                e.setdefault("y", None)
            filename = f"daily_{day}_{timeutil.now_utc().strftime('%H%M%S')}.pptx"
            try:
                warnings = daily_ppt.generate(payload, _generated_dir() / filename)
            except (FileNotFoundError, ValueError) as e:
                return back("/daily", date=day, msg=f"생성 실패: {e}")
            _record(conn, "daily", day, filename, config.DAILY_TEMPLATE_VERSION, warnings)
            msg = "PPTX 를 만들었습니다." + (f" 확인할 점 {len(warnings)}건이 있습니다." if warnings else "")
    return back("/daily", date=day, msg=msg)


# ───────────────────────── 주간 Word ─────────────────────────

def _week_default(conn, start: str) -> dict:
    s = date.fromisoformat(start)
    data = {"period_start": start, "period_end": (s + timedelta(days=6)).isoformat(),
            "report_date": (s + timedelta(days=7)).isoformat(), "politics": "", "stats": {}, "stats_basis": "",
            "terror": "", "oil": "", "oil_prices": [{"date": "", "dubai": "", "brent": "", "wti": ""},
                                                    {"date": "", "dubai": "", "brent": "", "wti": ""}],
            "oil_source": "", "intl": [{"title": "", "body": ""}], "impact": ""}
    # 주제 소제목과 영향 문단은 주마다 이어 쓰는 경우가 많아, 직전 편집본이 있으면 출발점으로 가져온다.
    prev = conn.execute("SELECT data FROM doc_editions WHERE kind='weekly' AND key<? ORDER BY key DESC LIMIT 1",
                        (start,)).fetchone()
    if prev:
        old = json.loads(prev["data"])
        data["intl"] = [{"title": g.get("title", ""), "body": ""} for g in old.get("intl", [])] or data["intl"]
        data["impact"] = old.get("impact", "")
        data["carried"] = True
    return data


def _default_week_start() -> str:
    # 원본 기준: 7일 구간이 끝난 다음 날 작성. 오늘이 작성일이라고 보고 직전 7일을 기본값으로 한다.
    return (timeutil.today_baghdad() - timedelta(days=7)).isoformat()


@app.get("/weekly")
def weekly_page(request: Request):
    start = request.query_params.get("start") or _default_week_start()
    date.fromisoformat(start)
    with db.session() as conn:
        stored = db.get_edition(conn, "weekly", start)
        data = stored or _week_default(conn, start)
        history = _history(conn, "weekly", start)
        selected = _selected(conn, data["period_start"], data["period_end"])
    errors, warnings = weekly_word.check(data)
    while len(data["oil_prices"]) < 2:      # 입력 칸은 원본처럼 최소 2일치를 보여준다
        data["oil_prices"].append({"date": "", "dubai": "", "brent": "", "wti": ""})
    total = None
    try:
        total = weekly_word.stats_total(data["stats"])
    except (KeyError, ValueError, TypeError):
        pass
    return render(request, "weekly.html", start=start, data=data, saved=stored is not None, history=history,
                  selected_count=len(selected), errors=errors, warnings=warnings, total=total,
                  stat_fields=weekly_word.STAT_FIELDS, oil_fields=weekly_word.OIL_FIELDS,
                  template_ok=config.WEEKLY_TEMPLATE.exists(), msg=request.query_params.get("msg", ""),
                  version=config.WEEKLY_TEMPLATE_VERSION)


def _weekly_from_form(form) -> dict:
    nl = lambda k: (form.get(k) or "").replace("\r\n", "\n")
    prices = []
    for d, du, br, wt in zip(form.getlist("oil_date"), form.getlist("oil_dubai"), form.getlist("oil_brent"),
                             form.getlist("oil_wti")):
        if any(v.strip() for v in (d, du, br, wt)):
            prices.append({"date": d.strip(), "dubai": du.strip().lstrip("$"), "brent": br.strip().lstrip("$"),
                           "wti": wt.strip().lstrip("$")})
    intl = [{"title": t.strip(), "body": b.replace("\r\n", "\n")}
            for t, b in zip(form.getlist("intl_title"), form.getlist("intl_body")) if t.strip() or b.strip()]
    return {"period_start": form.get("period_start"), "period_end": form.get("period_end"),
            "report_date": form.get("report_date"), "politics": nl("politics"),
            "stats": {k: (form.get(f"stat_{k}") or "").strip() for k, _ in weekly_word.STAT_FIELDS},
            "stats_basis": (form.get("stats_basis") or "").strip(), "terror": nl("terror"), "oil": nl("oil"),
            "oil_prices": prices, "oil_source": (form.get("oil_source") or "").strip(),
            "intl": intl or [{"title": "", "body": ""}], "impact": nl("impact")}


@app.post("/weekly/save")
async def weekly_save(request: Request):
    form = await request.form()
    start, action = form.get("start"), form.get("action", "save")
    data = _weekly_from_form(form)
    for k in ("period_start", "period_end", "report_date"):
        date.fromisoformat(data[k])
    msg = "저장했습니다."
    with db.session() as conn:
        if action == "import":
            buckets = {"politics": [], "terror": [], "intl": []}
            for a in _selected(conn, data["period_start"], data["period_end"]):
                item = _article_item(a, with_source=False)
                if a["region"] != "이라크" or a["category"] == "국제":
                    buckets["intl"].append(item)
                elif a["category"] in ("치안", "테러"):
                    buckets["terror"].append(item)
                else:
                    buckets["politics"].append(item)
            n = sum(map(len, buckets.values()))
            if not n:
                msg = "이 기간에 선택한 기사가 없습니다."
            else:
                for k in ("politics", "terror"):
                    data[k] = "\n\n".join(filter(None, [data[k].strip(), items_to_text(buckets[k])]))
                g = data["intl"][0]
                g["body"] = "\n\n".join(filter(None, [g["body"].strip(), items_to_text(buckets["intl"])]))
                msg = (f"선택한 기사 {n}건을 각 구역 끝에 덧붙였습니다. 통계 표는 자동으로 채우지 않습니다 "
                       "(추천 기사 수는 주간 사건 집계가 아닙니다).")
        db.save_edition(conn, "weekly", start, data)
        if action == "generate":
            filename = f"weekly_{start}_{timeutil.now_utc().strftime('%H%M%S')}.docx"
            try:
                warnings = weekly_word.generate(data, _generated_dir() / filename)
            except weekly_word.WeeklyInputError as e:
                return back("/weekly", start=start, msg=f"생성하지 않음 — 입력 확인 필요: {e}")
            except (FileNotFoundError, ValueError) as e:
                return back("/weekly", start=start, msg=f"생성 실패: {e}")
            _record(conn, "weekly", start, filename, config.WEEKLY_TEMPLATE_VERSION, warnings)
            msg = "DOCX 를 만들었습니다." + (f" 확인할 점 {len(warnings)}건이 있습니다." if warnings else "")
    return back("/weekly", start=start, msg=msg)
