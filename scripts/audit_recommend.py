"""추천 점검: 실제 보고서에 실린 항목이 수집·추천되는지 표로 보여준다.

실행: python scripts/audit_recommend.py            (기준: tests/fixtures/report_items.yaml, 데이터: data/news.db)
      python scripts/audit_recommend.py 기준파일.yaml

기준 파일의 기간에 대해 먼저 "새 기사 수집"을 해 두어야 한다. API 는 부르지 않는다.
가중치·검색어(config/topics.yaml)를 고친 뒤 다시 돌려 좋아졌는지 숫자로 확인하는 용도다.
"""
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db, pipeline  # noqa: E402


def audit(conn, spec: dict) -> list[dict]:
    d_from, d_to = spec["period"]["from"], spec["period"]["to"]
    events = pipeline.list_articles(conn, d_from, d_to)
    rank = {a["id"]: i + 1 for i, a in enumerate(events)}
    scale = pipeline.period_scale(d_from, d_to) if spec.get("scale_by_period", True) else 1.0
    recommended = {a["id"] for a in pipeline.pick_recommended(list(events), scale=scale)}
    spec["_shown"] = len(recommended)
    rows = []
    for item in spec["items"]:
        pattern = re.compile(item["find"], re.I)
        reps = {}
        for a in events:
            if any(pattern.search(x["title"]) for x in [a, *a["related"]]):
                reps[a["id"]] = a
        best = min(reps.values(), key=lambda a: rank[a["id"]]) if reps else None
        rows.append({"name": item["name"], "found": len(reps), "recommended": sum(i in recommended for i in reps),
                     "best_rank": rank[best["id"]] if best else None, "best_title": best["title"] if best else ""})
    return rows


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "tests/fixtures/report_items.yaml"
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    with db.session() as conn:
        pipeline.recompute(conn)
        rows = audit(conn, spec)
        total = len(pipeline.list_articles(conn, spec["period"]["from"], spec["period"]["to"]))
    print(f"기간 {spec['period']['from']} ~ {spec['period']['to']} · 사건 {total}건 · 추천 {spec['_shown']}건\n")
    print(f"{'항목':<34}{'수집':>5}{'추천':>5}{'최고순위':>8}  대표 기사")
    for r in rows:
        mark = "O" if r["recommended"] else ("-" if r["found"] else "X")
        print(f"{mark} {r['name']:<32}{r['found']:>5}{r['recommended']:>5}{(r['best_rank'] or '-'):>8}  {r['best_title'][:60]}")
    hit, found = sum(bool(r["recommended"]) for r in rows), sum(bool(r["found"]) for r in rows)
    print(f"\n추천에 포함된 항목 {hit}/{len(rows)} · 수집된 항목 {found}/{len(rows)}   (O 추천됨, - 수집됐지만 밀림, X 수집 안 됨)")


if __name__ == "__main__":
    main()
