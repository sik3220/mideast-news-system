"""설명 가능한 규칙 기반 추천. 점수마다 이유 문장을 남긴다.

중요도(점수)와 사실 확인 상태는 별개다. 확인 상태는 verification() 이 따로 계산한다.
"""
from __future__ import annotations

import re
from functools import lru_cache

from .config import topics_config
from .timeutil import now_utc, parse_iso


@lru_cache(maxsize=4096)
def _pattern(term: str) -> re.Pattern:
    t = term.lower()
    if t.isascii():
        # 짧은 약어(nic, ied 등)는 낱말 전체가 일치해야 한다. 긴 낱말은 앞쪽 경계만 본다(iraq -> iraqi).
        tail = r"(?![a-z0-9])" if len(t) <= 3 else ""
        return re.compile(r"(?<![a-z0-9])" + re.escape(t) + tail)
    return re.compile(re.escape(t))


def find_term(terms: list[str], text: str) -> str | None:
    low = text.lower()
    for term in terms:
        if _pattern(str(term)).search(low):
            return str(term)
    return None


def _texts(a: dict) -> tuple[str, str]:
    title = " ".join(filter(None, [a.get("title"), a.get("ko_title")]))
    rest = " ".join(filter(None, [a.get("snippet"), a.get("ko_summary")]))
    return title, rest


def match_topic(topic_id: str, a: dict, cfg: dict) -> str | None:
    """'title' | 'mention' | None"""
    t = cfg["topics"][topic_id]
    title, rest = _texts(a)
    for phrase in t.get("ignore") or []:              # 직함처럼 주제어를 품고 있지만 그 주제가 아닌 표현은 지우고 본다
        title, rest = title.replace(phrase, " "), rest.replace(phrase, " ")
    full = title + " " + rest
    if topic_id == "korea_media" and a.get("lang") != "ko":
        return None
    has_ctx = find_term(cfg["iraq_context"], full) is not None
    for where, text in (("title", title), ("mention", rest)):
        if find_term(t.get("strong", []), text):
            return where
        if has_ctx and find_term(t.get("context", []), text):
            return where
    return None


def classify(a: dict, cfg: dict) -> tuple[str, str]:
    title, rest = _texts(a)
    category = next((c for c, words in cfg["categories"].items() if find_term(words, title)), None) \
        or next((c for c, words in cfg["categories"].items() if find_term(words, rest)), "기타")
    region = next((r for r, words in cfg["regions"].items() if find_term(words, title)), None) \
        or next((r for r, words in cfg["regions"].items() if find_term(words, rest)), None) \
        or ("이라크" if is_iraqi_source(a, cfg) else "기타 중동")     # 이라크 매체는 제목에 '이라크'를 잘 쓰지 않는다
    return category, region


def is_iraqi_source(a: dict, cfg: dict) -> bool:
    name, domain = (a.get("source_name") or "").lower(), (a.get("source_domain") or "").lower()
    for s in cfg.get("iraqi_sources") or []:
        s = str(s).lower()
        if (s == ".iq" and domain.endswith(".iq")) or (s != ".iq" and (s in name or s in domain)):
            return True
    return False


def match_themes(a: dict, cfg: dict) -> list[dict]:
    """제목이 걸리는 관심 사안 목록. needs_iraq 인 사안은 기사에 이라크 문맥이 있어야 한다.

    원문 제목만 본다(한국어 요약은 보지 않는다). 요약 전후로 추천 구성이 바뀌지 않게 하기 위해서다.
    """
    title, rest = a.get("title") or "", a.get("snippet") or ""
    has_iraq = find_term(cfg["iraq_context"], title + " " + rest) is not None
    if not has_iraq and is_iraqi_source(a, cfg):
        # 이라크 매체의 기사라도 제목이 다른 나라를 가리키면(사우디·예멘 등 역내 보도) 이라크 사안으로 보지 않는다.
        has_iraq = not any(find_term(words, title) for r, words in cfg["regions"].items() if r != "이라크")
    return [t for t in cfg.get("themes") or []
            if (has_iraq or not t.get("needs_iraq")) and find_term(t["words"], title)
            and not (t.get("exclude") and find_term(t["exclude"], title))]


def _source_text(a: dict) -> str:
    return f"{a.get('source_name') or ''} {a.get('source_domain') or ''}".lower()


def is_preferred_source(a: dict, cfg: dict) -> bool:
    text = _source_text(a)
    return any(str(s).lower() in text for s in cfg.get("preferred_sources") or [])


def is_blocked_source(a: dict, cfg: dict) -> bool:
    text = _source_text(a)
    return any(str(s).lower() in text for s in cfg.get("blocked_sources") or [])


def is_social_source(a: dict, cfg: dict) -> bool:
    """SNS·게시판 글인지. 보고서 근거로 쓸 수 없어 추천에서 빼고 여론 참고용으로만 보여준다."""
    name, domain = (a.get("source_name") or "").lower(), (a.get("source_domain") or "").lower()
    for s in cfg.get("social_sources") or []:
        s = str(s).lower()
        if s in ("x.com", "t.me"):
            if domain == s or domain.endswith("." + s):
                return True
        elif s in name or s in domain:
            return True
    return False


def _readable(name: str) -> bool:
    """한글이나 로마자가 들어 있어 그대로 읽을 수 있는 이름인지."""
    return any("a" <= ch.lower() <= "z" or "가" <= ch <= "힣" for ch in name)


def source_display(a: dict, cfg: dict) -> str:
    """화면과 문서에 적을 출처 이름. 아랍어 이름은 표(source_labels)로 바꾸고, 표에 없으면 사이트 주소로 적는다."""
    name, domain = (a.get("source_name") or "").strip(), (a.get("source_domain") or "").strip()
    for key, label in (cfg.get("source_labels") or {}).items():
        if str(key).lower() in name.lower():
            return str(label)
    if name and _readable(name):
        return name
    return domain or name or "출처 미상"


SECTIONS = ("정치", "치안", "테러", "경제", "유가", "국제")
FOCUS = "관심 주제"


def direct_topics(a: dict, cfg: dict) -> list[str]:
    must = (cfg.get("recommend") or {}).get("must_topics", [])
    return [t for t in must if match_topic(t, a, cfg) == "title"]


def section_of(a: dict, cfg: dict) -> str:
    """추천을 고를 때의 구역. 규칙으로만 정한다: 관심 주제 직접 보도 → 관심 사안이 정한 구역 → 규칙 분류.

    Claude 가 붙인 태그는 쓰지 않는다. 요약할 때마다 추천 구성이 흔들려 다시 요약하는 일을 막기 위해서다.
    """
    if direct_topics(a, cfg):
        return FOCUS
    for t in match_themes(a, cfg):
        if t.get("section"):
            return t["section"]
    category = a.get("rule_category") or a.get("category") or ""
    return category if category in SECTIONS else "기타"


def display_section(a: dict, cfg: dict) -> str:
    """화면에 보일 구역. 요약이 있으면 Claude 가 문체 규칙에 따라 붙인 태그를 따른다."""
    if direct_topics(a, cfg):
        return FOCUS
    if a.get("summary_status") == "ok" and a.get("category") in SECTIONS:
        return a["category"]
    return section_of(a, cfg)


def score_article(a: dict, n_sources: int = 1, cfg: dict | None = None, now=None) -> dict:
    """기사 하나의 주제·분류·지역·점수·추천 이유를 계산한다."""
    cfg = cfg or topics_config()
    w = cfg["weights"]
    title, rest = _texts(a)
    full = title + " " + rest
    topics = [t for t in cfg["topics"] if match_topic(t, a, cfg)]
    rule_category, region = classify(a, cfg)
    if match_topic("bismayah", a, cfg) == "title":
        rule_category = "정치"                              # 문체 규칙 8절: 비스마야는 [정치]
    # 점수는 규칙 분류로 계산한다. 화면에 보일 분류는 요약이 있으면 Claude 태그를 쓴다.
    category = rule_category

    base, reasons = 0, []
    bis = match_topic("bismayah", a, cfg)
    cab = match_topic("cabinet", a, cfg)
    if bis == "title":
        base = max(base, w["bismayah_title"])
        reasons.append("비스마야·한화·NIC를 제목에서 직접 다룸")
    elif bis == "mention":
        base = max(base, w["bismayah_mention"])
        reasons.append("비스마야·한화·NIC 언급(제목 외, 단순 언급일 수 있음)")
    if cab == "title":
        base = max(base, w["cabinet_title"])
        reasons.append("이라크 정부·내각회의를 제목에서 직접 다룸")
    elif cab == "mention":
        base = max(base, w["cabinet_mention"])
        reasons.append("이라크 내각 언급(제목 외)")
    if not reasons:
        if region == "이라크":
            if category in ("테러", "치안"):
                base, reason = w["iraq_security"], "이라크 내 치안·테러 관련"
            else:
                base, reason = w["iraq_other"], "이라크 정치·경제 동향"
        elif find_term(cfg["iraq_context"], full):
            base, reason = w["regional_iraq_link"], "이라크와 연결된 역내 동향"
        else:
            base, reason = w["regional"], "그 밖의 중동 동향"
        reasons.append(reason)

    themes = match_themes(a, cfg)
    if themes:
        floor = max((t.get("floor", 0) for t in themes), default=0)
        bonus = max((t.get("bonus", 0) for t in themes), default=0)   # 여러 사안에 걸려도 가점은 한 번만
        base = max(base, floor) + bonus
        reasons.append("관심 사안: " + ", ".join(t["label"] for t in themes))

    score = base
    hits = [t for t in a.get("search_topics", []) if t in ("bismayah", "cabinet") and t not in topics]
    if hits and find_term(cfg["iraq_context"], title):
        score += w["topic_search_hit"]
        labels = "·".join(cfg["topics"][t]["label"] for t in hits)
        reasons.append(f"‘{labels}’ 검색에서 찾음(제목에 직접 언급 없음 — 원문 확인 필요)")
    word = find_term(cfg["decision_words"], title)
    if word:
        score += w["decision"]
        reasons.append(f"구체적인 결정·시행 표현 ‘{word}’")
    domain = a.get("source_domain", "")
    if any(domain == d or domain.endswith("." + d) for d in cfg["official_domains"]):
        score += w["official"]
        reasons.append("공식기관·국영통신 발표(내용의 독립 검증은 아님)")
    if is_preferred_source(a, cfg):
        score += w.get("preferred_source", 0)
        reasons.append("보고서에서 자주 인용하는 매체")
    if n_sources >= 2:
        score += w["multi_source"]
        reasons.append(f"독립 매체 {n_sources}곳 보도")
    if a.get("published_at") and a.get("time_precision") == "datetime":
        age = (now or now_utc()) - parse_iso(a["published_at"])
        if 0 <= age.total_seconds() <= 86400:
            score += w["fresh"]
            reasons.append("최근 24시간 내 발표")
    shown = a["llm_category"] if a.get("summary_status") == "ok" and a.get("llm_category") else rule_category
    return {"topics": topics, "category": shown, "rule_category": rule_category, "region": region,
            "score": score, "reasons": reasons}


def sort_key(a: dict):
    """점수 내림차순. 동점이면 발표 시각이 확인된 최신 기사를 먼저."""
    confirmed = a.get("time_precision") == "datetime"
    return (-a["score"], not confirmed, "" if not a.get("published_at") else
            "".join(chr(255 - ord(c)) for c in a["published_at"]))


def verification(a: dict, n_sources: int) -> list[str]:
    """사실 확인 상태 표시. 추천 점수와 무관하게 항상 보여준다."""
    out = []
    out.append(f"독립 매체 {n_sources}곳 보도" if n_sources >= 2 else "단일 출처 · 미확인")
    if a.get("claim_type"):
        out.append(a["claim_type"])
    basis = {"title": "제목 기반", "snippet": "제목·검색 요약 기반", "fulltext": "본문 기반"}
    out.append(basis.get(a.get("access_level", "title"), "제목 기반"))
    if a.get("time_precision") == "observed":
        out.append("발표 시각 미확인(관측 시각)")
    elif a.get("time_precision") in ("date", "unknown"):
        out.append("발표 시각 미상")
    return out
