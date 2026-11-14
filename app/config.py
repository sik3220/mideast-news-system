"""환경 설정. API 키는 서버 환경변수로만 읽고 화면·로그에 값을 내보내지 않는다."""
from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value and key.strip() not in os.environ:
            os.environ[key.strip()] = value


_load_dotenv()


def data_dir() -> Path:
    d = Path(os.environ.get("DATA_DIR") or ROOT / "data")
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return data_dir() / "news.db"


def demo_mode() -> bool:
    return os.environ.get("DEMO", "").strip() in ("1", "true", "yes")


def llm_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def auto_summarize() -> bool:
    """수집 직후 추천분을 자동으로 요약·묶기 할지. .env 에 AUTO_SUMMARIZE=0 이면 끈다."""
    return os.environ.get("AUTO_SUMMARIZE", "1").strip().lower() not in ("0", "false", "no")


def llm_model() -> str:
    """번역·요약 모델. 제목을 보고서 문체로 옮기는 단순 작업이라 값싼 모델을 기본으로 한다."""
    return os.environ.get("LLM_MODEL") or "claude-haiku-5-5"


def llm_group_model() -> str:
    """같은 사건 묶기 모델. 판단이 필요하고 수집당 한두 번뿐이라 한 단계 위 모델을 쓴다."""
    return os.environ.get("LLM_GROUP_MODEL") or "claude-sonnet-5-5"


def llm_body_model() -> str:
    """붙여 넣은 본문을 요약하는 모델. 기사 전체에서 핵심을 뽑는 일이라 한 단계 위 모델을 쓴다(기사당 1회)."""
    return os.environ.get("LLM_BODY_MODEL") or "claude-sonnet-5-5"


def llm_weekly_model() -> str:
    """주간 초안을 엮는 모델. 한 주치 재료에서 인과·경과를 판단하는 일이고 주 1회뿐이라 한 단계 위 모델을 쓴다."""
    return os.environ.get("LLM_WEEKLY_MODEL") or "claude-sonnet-5-5"


def llm_research_model() -> str:
    """체크한 기사의 본문을 웹에서 찾아 읽고 쓰는 모델. 웹 검색·읽기 도구를 쓸 수 있는 모델이어야 한다."""
    return os.environ.get("LLM_RESEARCH_MODEL") or "claude-sonnet-5-5"


def auto_research() -> bool:
    """기사를 체크하면 본문을 웹에서 찾아 세부 내용을 채울지. .env 에 AUTO_RESEARCH=0 이면 끈다."""
    return os.environ.get("AUTO_RESEARCH", "1").strip().lower() not in ("0", "false", "no")


def research_daily_max() -> int:
    """하루에 본문을 찾아볼 기사 수 상한(비용 보호). 0 이면 상한 없음."""
    try:
        return int(os.environ.get("RESEARCH_DAILY_MAX") or 15)
    except ValueError:
        return 15


def summarize_korean() -> bool:
    """한국어 기사도 문체 규칙대로 다시 쓸지. .env 에 SUMMARIZE_KOREAN=0 이면 원래 제목 그대로 둔다."""
    return os.environ.get("SUMMARIZE_KOREAN", "1").strip().lower() not in ("0", "false", "no")


def daily_budget() -> float:
    """하루 Claude 사용 추정 금액 한도(달러). 넘으면 자동 요약·묶기를 멈춘다. 0 이면 한도 없음."""
    try:
        return float(os.environ.get("DAILY_BUDGET_USD") or 0.5)
    except ValueError:
        return 0.5


STYLE_GUIDE = ROOT / "config" / "style_guide.md"
_style_cache: tuple[float, str] | None = None


def style_guide_text() -> str:
    """문체 규칙 전문. 파일을 고치면 다음 요약부터 반영된다."""
    global _style_cache
    mtime = STYLE_GUIDE.stat().st_mtime
    if _style_cache is None or _style_cache[0] != mtime:
        _style_cache = (mtime, STYLE_GUIDE.read_text(encoding="utf-8"))
    return _style_cache[1]


SEARCH_GOALS = ROOT / "config" / "search_goals.md"


def search_goals_text() -> str:
    """담당자의 분야별 검색 목표. 본문을 읽고 쓸 때와 주간 초안을 엮을 때 무엇을 우선 뽑을지의 기준이다."""
    try:
        return SEARCH_GOALS.read_text(encoding="utf-8")
    except OSError:
        return ""


def soffice_path() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


TEMPLATE_DIR = ROOT / "doc_templates"
DAILY_TEMPLATE = TEMPLATE_DIR / "daily_template.pptx"
DAILY_BASE_IMAGE = TEMPLATE_DIR / "daily_base_slide1.png"
WEEKLY_TEMPLATE = TEMPLATE_DIR / "weekly_template.docx"
# 원본 양식을 바꾸면 이 값을 올린다. 생성 이력에 함께 기록된다.
DAILY_TEMPLATE_VERSION = "daily-2026.11.10"
WEEKLY_TEMPLATE_VERSION = "weekly-2026.11.12"


@lru_cache
def topics_config() -> dict:
    return yaml.safe_load((ROOT / "config" / "topics.yaml").read_text(encoding="utf-8"))
