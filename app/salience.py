"""화제성: 같은 기간에 여러 매체가 함께 다룬 낱말을 찾아 그날의 주요 사안을 가려낸다.

키워드 가점만으로는 "오늘 무엇이 큰 사안인가"를 알 수 없다. 대신 여러 매체의 제목에 동시에 등장하는
낱말(예: 'Jurf', 'smuggling', 'Sirik')은 그 기간의 주요 사안을 가리킨다. 너무 흔한 낱말(Iraq, Iran, Trump 처럼
대부분의 기사에 나오는 것)은 구별력이 없어 제외한다.

API 를 쓰지 않는 단순 집계이며, 추천 이유에 어떤 낱말을 몇 개 매체가 다뤘는지 그대로 남긴다.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict

_WORD = re.compile(r"[A-Za-z][A-Za-z'’\-]+|[가-힣]{2,}|[؀-ۿ]{3,}")
STOP = set("""
about after again against amid among also been being between could from have into more most must over said says say
that their them then there these they this those under very were what when where which while will with would your
news report reports update updated live latest video watch analysis opinion explainer world middle east region
government minister ministry official officials president prime security forces military army state states united
country countries talks deal attack attacks strike strikes killed kills year years week weeks today first last than
iraq iraqi iraq's iran iranian iran's baghdad israel israeli american trump trump's
이라크 이란 미국 한국 정부 대통령 총리 관련 대한 위한 위해 통해 따라 오늘 이번 지난 속보 종합 단독 뉴스 기자 것으로 있다 없다 있어 없어 한다 했다 된다
العراق العراقي العراقية ايران إيران بغداد على الى إلى في من عن مع بعد قبل هذا هذه التي الذي وزير رئيس الوزراء
""".split())


def tokens(title: str) -> set[str]:
    out = set()
    for w in _WORD.findall(title or ""):
        w = w.lower().strip("'’-")
        if w in STOP or (w.isascii() and len(w) < 4):
            continue
        out.add(w[:-2] if w.isascii() and w.endswith("'s") else w)
    return out


def add_salience(articles: list[dict], rule: dict) -> None:
    """기간 안의 기사들에 화제성 가점을 더한다(a['score'], a['reasons'], a['burst'] 를 바꾼다).

    rule: {min_sources, max_share, max_bonus}. 언어별로 따로 센다(낱말이 다르므로).
    """
    min_sources = int(rule.get("min_sources", 4))
    max_share = float(rule.get("max_share", 0.12))
    max_bonus = float(rule.get("max_bonus", 20))
    by_lang: dict[str, list[dict]] = defaultdict(list)
    for a in articles:
        a["burst"] = None
        by_lang[a.get("lang") or ""].append(a)
    for group in by_lang.values():
        sources: dict[str, set[str]] = defaultdict(set)
        toks = {}
        for a in group:
            toks[a["id"]] = tokens(a["title"])
            src = a.get("source_domain") or a.get("source_name") or str(a["id"])
            for t in toks[a["id"]]:
                sources[t].add(src)
        upper = max(min_sources * 2, max_share * len({a.get("source_domain") or a["id"] for a in group}) * 4)
        df = {t: len(s) for t, s in sources.items() if min_sources <= len(s) <= upper}
        for a in group:
            mine = [(df[t], t) for t in toks[a["id"]] if t in df]
            if not mine:
                continue
            n, t = max(mine)
            bonus = min(max_bonus, round(5 * math.log2(n)))
            a["burst"] = t
            a["score"] += bonus
            a["reasons"] = a["reasons"] + [f"여러 매체가 다룬 화제 ‘{t}’ (매체 {n}곳)"]
