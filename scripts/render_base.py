"""일일 PPT 원본 양식에서 지도 위치 지정 화면용 바탕 그림을 다시 만든다. 양식을 바꿨을 때 실행한다.

실행: python scripts/render_base.py   (LibreOffice 와 pdftoppm 필요)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config  # noqa: E402
from app.docs import daily_ppt  # noqa: E402

daily_ppt.render_base_image(config.DAILY_BASE_IMAGE)
print("만든 파일:", config.DAILY_BASE_IMAGE)
