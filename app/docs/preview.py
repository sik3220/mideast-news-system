"""검토용 렌더링. LibreOffice 가 있으면 PDF 로 바꿔 화면에서 보여준다(배포용 출력이 아니다).

LibreOffice 의 줄바꿈·글꼴은 PowerPoint·Word 와 다를 수 있다. 최종 외관은 원래 프로그램에서 확인한다.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import config


class PreviewUnavailable(Exception):
    pass


def available() -> bool:
    return config.soffice_path() is not None


def to_pdf(path: Path, timeout: int = 120) -> Path:
    soffice = config.soffice_path()
    if not soffice:
        raise PreviewUnavailable("LibreOffice 가 설치되어 있지 않아 미리보기를 만들 수 없음")
    out = path.with_suffix(".pdf")
    with tempfile.TemporaryDirectory() as profile:
        try:
            subprocess.run(
                [soffice, f"-env:UserInstallation=file://{profile}", "--headless", "--convert-to", "pdf",
                 "--outdir", str(path.parent), str(path)],
                check=True, capture_output=True, timeout=timeout)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            raise PreviewUnavailable(f"미리보기 변환 실패: {type(e).__name__}") from e
    if not out.exists():
        raise PreviewUnavailable("미리보기 변환 결과가 없음")
    return out


def to_png(pdf: Path, dpi: int = 110) -> list[Path]:
    tool = shutil.which("pdftoppm")
    if not tool:
        raise PreviewUnavailable("pdftoppm 이 없어 그림으로 바꿀 수 없음")
    prefix = pdf.with_suffix("")
    subprocess.run([tool, "-png", "-r", str(dpi), str(pdf), str(prefix)], check=True, capture_output=True, timeout=120)
    return sorted(pdf.parent.glob(prefix.name + "-*.png"))
