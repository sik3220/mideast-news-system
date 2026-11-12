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


def llm_model() -> str:
    return os.environ.get("LLM_MODEL") or "claude-opus-5-5"


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
