"""수집 공통: 허용 호스트로 제한한 HTTP 요청과 제공자 결과 형식.

서버가 요청하는 URL 은 제공자별 허용 호스트와 공인 주소로 제한하고, 리디렉션도 매번 다시 검증한다.
기사 본문은 가져오지 않는다(메타데이터·검색 요약·원문 링크만 저장).
"""
from __future__ import annotations

import ipaddress
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

TIMEOUT = 20
MAX_BYTES = 5_000_000
MAX_REDIRECTS = 3
USER_AGENT = "Mozilla/5.0 (compatible; news-dashboard/1.0)"


class FetchError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def check_url(url: str, allowed_hosts: set[str]) -> None:
    p = urlsplit(url)
    if p.scheme != "https":
        raise FetchError(f"https 가 아닌 주소: {p.scheme}")
    host = (p.hostname or "").lower()
    if host not in allowed_hosts:
        raise FetchError(f"허용하지 않은 호스트: {host}")
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        # 프록시 뒤에서는 로컬 DNS 가 안 될 수 있다. 호스트는 이미 허용 목록으로 제한했다.
        if os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"):
            return
        raise FetchError(f"호스트를 찾을 수 없음: {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise FetchError(f"공인 주소가 아님: {host}")


def safe_get(url: str, allowed_hosts: set[str], timeout: int = TIMEOUT) -> bytes:
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(MAX_REDIRECTS + 1):
        check_url(url, allowed_hosts)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with opener.open(req, timeout=timeout) as resp:
                data = resp.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise FetchError("응답이 너무 큼")
                return data
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = urljoin(url, e.headers["Location"])
                continue
            raise FetchError(f"HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise FetchError(f"연결 실패: {getattr(e, 'reason', e)}") from e
    raise FetchError("리디렉션이 너무 많음")


@dataclass
class SearchSpec:
    provider: str
    query: str
    lang: str = ""
    topic: str | None = None

    @property
    def label(self) -> str:
        return f"{self.provider}{' · ' + self.lang if self.lang else ''} · {self.query}"


@dataclass
class SearchResult:
    spec: SearchSpec
    status: str = "ok"          # ok | empty | failed
    articles: list[dict] = field(default_factory=list)
    error: str | None = None
