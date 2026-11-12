import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DEMO", raising=False)
    return tmp_path


@pytest.fixture
def conn(data_dir):
    from app import db
    c = db.connect()
    yield c
    c.close()


def make_article(url="https://example.com/a", title="Iraq cabinet approves budget", **kw):
    from app.dedupe import normalize_url
    base = {"url": url, "url_norm": normalize_url(url), "title": title, "snippet": "", "source_name": "Example",
            "source_domain": "example.com", "lang": "en", "provider": "test",
            "published_at": "2026-11-10T09:00:00Z", "published_date": "2026-11-10", "time_precision": "datetime"}
    base.update(kw)
    return base
