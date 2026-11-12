# 원본 양식 넣는 곳

원본 양식은 저장소에 올리지 않는다. 이 폴더에 아래 이름으로 직접 넣는다.

| 파일 이름 | 넣을 파일 |
|---|---|
| `daily_template.pptx` | 일일 PPT 원본 (개발 기준: 2026.11.10자) |
| `weekly_template.docx` | 주간 Word 원본 (개발 기준: 2026.11.12자) |

넣은 뒤 지도 위치 지정 화면의 바탕 그림을 만든다(LibreOffice 와 pdftoppm 필요).

```bash
python scripts/render_base.py
```

파일이 없으면 해당 화면에 "원본 양식 파일이 없어 만들 수 없습니다"가 표시되고, 나머지 기능은 그대로 동작한다.
