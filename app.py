"""키워드별 최신 뉴스를 수집하고 간단히 요약하는 콘솔 프로그램."""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import ssl
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from io import TextIOBase
from pathlib import Path
from typing import Callable, Iterable, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree


BING_NEWS_URL = "https://www.bing.com/news/search"
GOOGLE_NEWS_URL = "https://news.google.com/rss/search"
KAKAO_TOKEN_URL = "https://kauth.kakao.com/oauth/token"
KAKAO_MEMO_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
USER_AGENT = "Mozilla/5.0 (compatible; KeywordNewsReader/1.0)"
DEFAULT_KEYWORDS = [
    "개인정보보호위원회",
    "개인정보 유출 사고",
    "개보위",
    "개인정보보호법",
    "AI 보안",
]
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
SENTENCE_RE = re.compile(r"(?<=[.!?。]|[다요죠])\s+")
TEAMS_MAX_PAYLOAD_BYTES = 25_000
DEFAULT_REPORT_URL = "https://csh5751.github.io/privacy-news-report/"
KST = timezone(timedelta(hours=9))
HISTORY_RETENTION_DAYS = 30
LOG_RETENTION_DAYS = 30
MAX_RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2.0
STORY_TOKEN_RE = re.compile(r"[가-힣A-Za-z0-9]+")
# 날짜 표기("19일", "2026년")는 사건을 구분하지 못하므로 비교에서 제외한다.
DATE_TOKEN_RE = re.compile(r"^\d+[일월년]?$")
STORY_STOPWORDS = {
    "ai", "개인정보", "개인정보보호", "보호", "관련", "대한", "위한",
    "시대", "뉴스", "최신", "진행", "개최", "밝혔다",
}
# 같은 사건으로 볼 제목 유사도. IDF 가중 기준이라 흔한 단어만 겹쳐도 묶이지 않는다.
STORY_MATCH_RATIO = 0.45
# 개인정보·보안 사건임을 알려주는 표현. 하나 걸릴 때마다 점수를 올린다.
RELEVANCE_SIGNALS = (
    "유출", "해킹", "침해", "과징금", "제재", "처분", "시정명령", "시정조치",
    "조사", "의결", "고시", "시행령", "개정", "입법", "취약점", "랜섬웨어",
    "피싱", "스미싱", "악성코드", "디도스", "위반", "집단소송", "손해배상",
    "보상안", "사과", "개인정보", "프라이버시", "정보보호", "가명정보",
    "마이데이터", "cpo", "gdpr", "개보위", "감독", "보안사고", "정보통신망법",
    # 보안 자체가 주제인 기사도 남긴다. 홍보성 기사는 아래 노이즈 점수로 걸러진다.
    "보안", "사이버", "암호화", "인증", "사찰", "감시",
)
# 홍보·시장 뉴스 표현. 신호가 없으면 이쪽이 걸린 기사는 걸러낸다.
RELEVANCE_NOISE = (
    "출시", "선보", "실적", "매출", "영업이익", "주가", "수주", "협약", "mou",
    "수상", "임명", "채용", "세미나", "컨퍼런스", "웨비나", "공모전", "이벤트",
    "할인", "프로모션", "솔루션", "간담회", "포럼", "박람회", "출범", "체결",
)
RELEVANCE_MIN_SCORE = 1
# 필터가 후보를 이만큼 아래로 깎으면 신호어 목록이 현실을 못 따라간 것으로 보고 되돌린다.
RELEVANCE_MIN_KEPT_RATIO = 0.15
# Bing 피드는 관련도 순으로 12건 남짓만 돌려주므로 Google 피드로 최신 기사를 보충한다.
DEFAULT_WINDOW_HOURS = 30
# 관련성 판단과 사건 묶기는 분류에 가까운 작업이라 상위 모델이 필요하지 않다.
# 이 모델은 adaptive thinking과 effort를 지원하지 않으므로(4.6 이후 기능) 보내지 않는다.
CURATION_MODEL = "claude-haiku-4-5"
CURATION_MAX_CANDIDATES = 400
CURATION_DESCRIPTION_LIMIT = 200
CURATION_MAX_TOKENS = 16_000
# 뉴스 요청 제한 시간과 별개다. 수백 건을 판단하는 호출은 수 분이 걸릴 수 있다.
CURATION_TIMEOUT_SECONDS = 300.0
GOOGLE_TITLE_SUFFIX_RE = re.compile(r"\s+[-–]\s+[^-–]{2,40}$")
SOURCE_TIERS = (
    (100, ("연합뉴스", "kbs", "mbc", "sbs", "jtbc", "ytn")),
    (95, ("조선일보", "중앙일보", "동아일보", "한겨레", "경향신문", "한국일보")),
    (90, ("한국경제", "매일경제", "서울경제", "머니투데이", "이데일리")),
    (85, ("전자신문", "아이뉴스24", "zdnet", "지디넷", "디지털데일리", "보안뉴스")),
    (75, ("뉴스1", "뉴시스", "이투데이", "아시아투데이")),
)
AGGREGATOR_HOSTS = (
    "msn.com", "nate.com", "zum.com", "daum.net", "news.naver.com",
)
# Google 피드는 재배포 매체를 "네이트"처럼 출처명으로만 주기 때문에 호스트만
# 봐서는 걸러지지 않는다. 이름으로도 판별한다.
AGGREGATOR_SOURCE_TERMS = ("네이트", "msn", "zum", "줌뉴스", "다음뉴스", "daum")
# 개인 블로그·카페 글은 언론 보도가 아니므로 후보에서 제외한다.
UGC_SOURCE_TERMS = ("블로그", "카페", "브런치", "blog", "tistory", "brunch")
T = TypeVar("T")


@dataclass(frozen=True)
class Article:
    """RSS에서 가져온 뉴스 한 건."""

    title: str
    link: str
    source: str
    published: datetime | None
    description: str
    # 이 기사를 찾아낸 검색 키워드. 한 기사가 여러 키워드에 걸릴 수 있다.
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class Story:
    """같은 사건을 다룬 기사들을 하나로 묶은 보고 단위."""

    section: str
    headline: str
    summary: str
    importance: int
    representative: Article
    related: tuple[Article, ...] = ()

    @property
    def articles(self) -> tuple[Article, ...]:
        return (self.representative, *self.related)


@dataclass(frozen=True)
class RunStats:
    """한 번의 실행에서 후보가 어떻게 걸러졌는지 기록한다."""

    collected: int = 0
    fresh: int = 0
    already_sent: int = 0
    irrelevant: int = 0
    folded: int = 0
    stories: int = 0
    curator: str = ""
    failed_keywords: int = 0


@dataclass(frozen=True)
class Report:
    """한 번의 실행이 만들어낸 보고서 전체."""

    sections: dict[str, list[Story]]
    stats: RunStats
    # 지난 브리핑에서 이미 안내해 본문에서는 뺀 사건들.
    revisited: tuple[Story, ...] = ()
    # 그 가운데 목록 길이 제한으로 싣지 못한 사건 수.
    revisited_hidden: int = 0

    @property
    def total(self) -> int:
        return sum(len(stories) for stories in self.sections.values())


@dataclass(frozen=True)
class KakaoConfig:
    rest_api_key: str
    client_secret: str
    refresh_token: str
    link_url: str


class PermanentError(RuntimeError):
    """재시도해도 결과가 달라지지 않는 실패(설정 오류, 거절 등)."""

    retryable = False


class TimestampedTee(TextIOBase):
    """콘솔 출력은 유지하면서 완성된 각 줄을 시각과 함께 로그에 기록한다."""

    def __init__(self, original: TextIOBase, log_file: TextIOBase, level: str):
        self.original = original
        self.log_file = log_file
        self.level = level
        self.buffer = ""

    def write(self, value: str) -> int:
        self.original.write(value)
        self.original.flush()
        self.buffer += value
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            timestamp = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")
            self.log_file.write(f"[{timestamp}] [{self.level}] {line}\n")
            self.log_file.flush()
        return len(value)

    def flush(self) -> None:
        self.original.flush()
        if self.buffer:
            timestamp = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")
            self.log_file.write(f"[{timestamp}] [{self.level}] {self.buffer}\n")
            self.buffer = ""
        self.log_file.flush()

    @property
    def encoding(self) -> str | None:
        return getattr(self.original, "encoding", None)


def logs_directory() -> Path:
    """사용자별 실행 로그 디렉터리를 반환한다."""
    return history_path().parent / "logs"


def cleanup_old_logs(directory: Path) -> None:
    """보관 기간이 지난 실행 로그만 삭제한다."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOG_RETENTION_DAYS)
    for path in directory.glob("news-report-*.log"):
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            if modified < cutoff:
                path.unlink()
        except OSError:
            continue


def retry_operation(
    name: str,
    operation: Callable[[], T],
    attempts: int = MAX_RETRY_ATTEMPTS,
    delay_seconds: float = RETRY_DELAY_SECONDS,
    sleep_func: Callable[[float], None] = time.sleep,
) -> T:
    """실패한 작업을 최초 시도 포함 최대 지정 횟수만큼 실행한다."""
    if attempts < 1:
        raise ValueError("재시도 횟수는 1 이상이어야 합니다.")
    for attempt in range(1, attempts + 1):
        try:
            result = operation()
            if attempt > 1:
                print(f"[재시도 성공] {name}: {attempt}/{attempts}회차")
            return result
        except RuntimeError as exc:
            # 설정 오류처럼 다시 시도해도 같은 결과가 나오는 실패는 즉시 포기한다.
            if attempt >= attempts or not getattr(exc, "retryable", True):
                print(
                    f"[최종 실패] {name}: {attempt}/{attempts}회차 - {exc}",
                    file=sys.stderr,
                )
                raise
            delay = delay_seconds * attempt
            print(
                f"[재시도] {name}: {attempt}/{attempts}회차 실패 - {exc}; "
                f"{delay:g}초 후 다시 시도",
                file=sys.stderr,
            )
            sleep_func(delay)
    raise RuntimeError(f"{name} 재시도가 예기치 않게 종료됐습니다.")


def clean_text(value: str | None) -> str:
    """HTML 태그와 엔티티를 제거하고 공백을 정돈한다."""
    if not value:
        return ""
    text = html.unescape(value)
    text = TAG_RE.sub(" ", text)
    return SPACE_RE.sub(" ", text).strip()


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        if re.fullmatch(r"\d{14}", value):
            parsed = datetime.strptime(value, "%Y%m%d%H%M%S").replace(
                tzinfo=timezone.utc
            )
        else:
            parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def normalize_news_link(value: str) -> str:
    """Bing 중계 URL에 포함된 실제 기사 URL을 반환한다."""
    try:
        parsed = urlparse(value)
        if parsed.hostname and parsed.hostname.lower().endswith("bing.com"):
            target = parse_qs(parsed.query).get("url", [""])[0]
            target_parsed = urlparse(target)
            if target_parsed.scheme in {"http", "https"} and target_parsed.netloc:
                return target
    except (TypeError, ValueError):
        pass
    return value


def fetch_with_curl(url: str, timeout: float) -> bytes:
    """Python 인증서 저장소에 문제가 있을 때 시스템 curl로 데이터를 받는다."""
    command = ["curl"]
    if sys.platform == "win32":
        # 인증서 체인은 검증하되, 이 PC에서 실패하는 폐기 목록 조회만 생략한다.
        command.append("--ssl-no-revoke")
    command.extend(
        [
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            "--max-time",
            str(max(1, int(timeout))),
            "--user-agent",
            USER_AGENT,
            url,
        ]
    )
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            timeout=timeout + 2,
        )
        return completed.stdout
    except FileNotFoundError as exc:
        raise RuntimeError("HTTPS 인증서 오류가 발생했고 curl도 설치되어 있지 않습니다.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("뉴스 요청 시간이 초과되었습니다.") from exc
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"뉴스 서버에 연결할 수 없습니다: {message}") from exc


def post_with_curl(url: str, data: bytes, timeout: float) -> None:
    """시스템 curl을 이용해 JSON을 POST한다."""
    command = ["curl"]
    if sys.platform == "win32":
        command.append("--ssl-no-revoke")
    command.extend(
        [
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            "--max-time",
            str(max(1, int(timeout))),
            "--header",
            "Content-Type: application/json",
            "--data-binary",
            "@-",
            url,
        ]
    )
    try:
        subprocess.run(
            command,
            input=data,
            check=True,
            capture_output=True,
            timeout=timeout + 2,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("HTTPS 인증서 오류가 발생했고 curl도 설치되어 있지 않습니다.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Teams 전송 시간이 초과되었습니다.") from exc
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Teams 전송에 실패했습니다: {message}") from exc


def post_kakao_form(
    url: str,
    fields: dict[str, str],
    timeout: float,
    access_token: str | None = None,
) -> dict[str, object]:
    """카카오 API에 폼 요청을 보내고 JSON 응답을 반환한다."""
    data = urlencode(fields).encode("utf-8")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        "User-Agent": USER_AGENT,
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    request = Request(url, data=data, headers=headers, method="POST")

    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"카카오 API HTTP {exc.code} 오류: {detail[:300]}") from exc
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        if not isinstance(reason, ssl.SSLError):
            raise RuntimeError(f"카카오 API에 연결할 수 없습니다: {reason}") from exc

        command = ["curl"]
        if sys.platform == "win32":
            command.append("--ssl-no-revoke")
        command.extend(
            [
                "--fail-with-body",
                "--silent",
                "--show-error",
                "--max-time",
                str(max(1, int(timeout))),
                "--header",
                "Content-Type: application/x-www-form-urlencoded;charset=utf-8",
            ]
        )
        if access_token:
            command.extend(["--header", f"Authorization: Bearer {access_token}"])
        command.extend(["--data-binary", "@-", url])
        try:
            completed = subprocess.run(
                command,
                input=data,
                check=True,
                capture_output=True,
                timeout=timeout + 2,
            )
            payload = completed.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired) as curl_exc:
            raise RuntimeError("카카오 API HTTPS 요청에 실패했습니다.") from curl_exc
        except subprocess.CalledProcessError as curl_exc:
            detail = curl_exc.stdout.decode("utf-8", errors="replace")
            raise RuntimeError(f"카카오 API 요청 실패: {detail[:300]}") from curl_exc
    except TimeoutError as exc:
        raise RuntimeError("카카오 API 요청 시간이 초과되었습니다.") from exc

    try:
        result = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("카카오 API 응답을 해석할 수 없습니다.") from exc
    if not isinstance(result, dict):
        raise RuntimeError("카카오 API 응답 형식이 올바르지 않습니다.")
    return result


def read_feed(url: str, timeout: float) -> ElementTree.Element:
    """RSS 피드를 받아 파싱한 루트 엘리먼트를 반환한다."""
    request = Request(url, headers={"User-Agent": USER_AGENT})

    raw_data: bytes
    try:
        with urlopen(request, timeout=timeout) as response:
            raw_data = response.read()
    except HTTPError as exc:
        raise RuntimeError(f"뉴스 서버가 HTTP {exc.code} 오류를 반환했습니다.") from exc
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLError):
            raw_data = fetch_with_curl(url, timeout)
        else:
            raise RuntimeError(f"뉴스 서버에 연결할 수 없습니다: {reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError("뉴스 요청 시간이 초과되었습니다.") from exc

    try:
        return ElementTree.fromstring(raw_data)
    except ElementTree.ParseError as exc:
        raise RuntimeError("뉴스 응답을 해석할 수 없습니다.") from exc


def fetch_bing_news(
    keyword: str, timeout: float = 10.0, max_results: int = 20
) -> list[Article]:
    """Bing News 피드에서 키워드 뉴스를 최신순으로 가져온다."""
    params = urlencode(
        {
            "q": keyword,
            "format": "rss",
            "setlang": "ko-kr",
            "cc": "KR",
            # 기본 정렬은 관련도순이라 몇 달 전 기사가 섞인다. 날짜순으로 강제한다.
            "qft": 'sortbydate="1"',
        }
    )
    root = read_feed(f"{BING_NEWS_URL}?{params}", timeout)

    articles: list[Article] = []
    for item in root.findall("./channel/item")[:max_results]:
        title = clean_text(item.findtext("title"))
        link = normalize_news_link((item.findtext("link") or "").strip())
        source = ""
        for child in item:
            if child.tag.endswith("Source"):
                source = clean_text(child.text)
                break
        if title and link:
            articles.append(
                Article(
                    title=title,
                    link=link,
                    source=source or "출처 미상",
                    published=parse_date(item.findtext("pubDate")),
                    description=clean_text(item.findtext("description")),
                )
            )
    return articles


def strip_source_suffix(title: str, source: str) -> str:
    """Google 피드가 제목 끝에 붙이는 " - 언론사" 표기를 제거한다."""
    if source and title.endswith(f" - {source}"):
        return title[: -len(source) - 3].strip()
    return GOOGLE_TITLE_SUFFIX_RE.sub("", title).strip() or title


def fetch_google_news(
    keyword: str,
    timeout: float = 10.0,
    max_results: int = 100,
    window_hours: int = DEFAULT_WINDOW_HOURS,
) -> list[Article]:
    """Google News 피드에서 지정한 시간 창 안의 키워드 뉴스를 가져온다."""
    # when:2d 처럼 일 단위만 받으므로 시간 창을 올림해서 넘긴다.
    days = max(1, -(-window_hours // 24))
    params = urlencode(
        {
            "q": f"{keyword} when:{days}d",
            "hl": "ko",
            "gl": "KR",
            "ceid": "KR:ko",
        }
    )
    root = read_feed(f"{GOOGLE_NEWS_URL}?{params}", timeout)

    articles: list[Article] = []
    for item in root.findall("./channel/item")[:max_results]:
        raw_title = clean_text(item.findtext("title"))
        link = (item.findtext("link") or "").strip()
        source = clean_text(item.findtext("source")) or "출처 미상"
        title = strip_source_suffix(raw_title, source)
        if title and link:
            articles.append(
                Article(
                    title=title,
                    link=link,
                    source=source,
                    published=parse_date(item.findtext("pubDate")),
                    # Google 피드의 description은 링크 앵커뿐이라 요약으로 쓸 수 없다.
                    description="",
                )
            )
    return articles


def fetch_news(
    keyword: str,
    timeout: float = 10.0,
    max_results: int = 20,
    window_hours: int = DEFAULT_WINDOW_HOURS,
) -> list[Article]:
    """모든 소스에서 키워드 뉴스를 모은다. 전부 실패할 때만 오류를 낸다."""
    collected: list[Article] = []
    failures: list[str] = []
    sources: list[tuple[str, Callable[[], list[Article]]]] = [
        (
            "Google",
            lambda: fetch_google_news(
                keyword, timeout=timeout, window_hours=window_hours
            ),
        ),
        (
            "Bing",
            lambda: fetch_bing_news(keyword, timeout=timeout, max_results=max_results),
        ),
    ]
    for name, fetcher in sources:
        try:
            found = fetcher()
        except RuntimeError as exc:
            failures.append(f"{name}: {exc}")
            print(f"[소스 실패] {keyword} / {name}: {exc}", file=sys.stderr)
            continue
        print(f"[소스] {keyword} / {name}: {len(found)}건")
        # 어떤 키워드가 찾아낸 기사인지 남겨 보고서 구획을 나누는 데 쓴다.
        collected.extend(replace(article, keywords=(keyword,)) for article in found)

    if failures and not collected:
        raise RuntimeError(f"모든 뉴스 소스가 실패했습니다 ({'; '.join(failures)})")
    return collected


def summarize(article: Article, max_length: int = 180) -> str:
    """RSS 설명에서 짧은 추출 요약을 만든다."""
    text = article.description
    if not text or text == article.title:
        return article.title

    sentences = [part.strip() for part in SENTENCE_RE.split(text) if part.strip()]
    summary = ""
    for sentence in sentences:
        candidate = f"{summary} {sentence}".strip()
        if len(candidate) > max_length:
            break
        summary = candidate

    if not summary:
        summary = text[: max_length - 1].rstrip() + "…"
    elif len(summary) < len(text) and not summary.endswith("…"):
        summary += "…"
    return summary


def merge_duplicate_articles(articles: Iterable[Article]) -> list[Article]:
    """같은 링크나 제목의 기사를 하나로 합치고 정보가 가장 많은 쪽을 남긴다.

    소스마다 같은 기사를 다른 링크로 돌려주고 설명을 붙이는 쪽도 다르므로,
    합칠 때 설명이 있는 기사를 우선해서 요약 재료를 잃지 않는다.
    """
    groups: list[list[Article]] = []
    index: dict[str, int] = {}
    for article in articles:
        keys = (
            f"link:{article.link}",
            f"title:{SPACE_RE.sub(' ', article.title).strip().casefold()}",
        )
        target = next((index[key] for key in keys if key in index), None)
        if target is None:
            target = len(groups)
            groups.append([])
        groups[target].append(article)
        for key in keys:
            index.setdefault(key, target)

    merged: list[Article] = []
    for group in groups:
        best = max(
            group,
            key=lambda item: (
                len(clean_text(item.description)),
                representative_score(item),
            ),
        )
        # 여러 키워드에 걸린 기사는 그 사실을 모두 남긴다. 대표 사본의 키워드만
        # 남기면 어느 구획에 넣을지 결정할 근거를 잃는다.
        keywords = tuple(
            dict.fromkeys(
                keyword for item in group for keyword in item.keywords
            )
        )
        merged.append(replace(best, keywords=keywords))
    return merged


def article_keys(article: Article) -> set[str]:
    """URL 추적값과 제목 표기 차이를 정리한 중복 판별 키를 만든다."""
    keys = {f"title:{SPACE_RE.sub(' ', article.title).strip().casefold()}"}
    try:
        parsed = urlparse(article.link)
        query = [
            (name, value)
            for name, values in parse_qs(parsed.query, keep_blank_values=True).items()
            if not name.casefold().startswith("utm_")
            for value in values
        ]
        normalized = parsed._replace(
            scheme=parsed.scheme.casefold(),
            netloc=parsed.netloc.casefold(),
            path=parsed.path.rstrip("/") or "/",
            query=urlencode(sorted(query)),
            fragment="",
        ).geturl()
        keys.add(f"url:{normalized}")
    except (TypeError, ValueError):
        keys.add(f"url:{article.link}")
    return keys


def story_tokens(value: str) -> set[str]:
    """기사 비교에 쓸 핵심 단어를 추출한다."""
    return {
        token.casefold()
        for token in STORY_TOKEN_RE.findall(clean_text(value))
        if len(token) >= 2
        and token.casefold() not in STORY_STOPWORDS
        and not DATE_TOKEN_RE.match(token)
    }


def is_aggregator(article: Article) -> bool:
    """기사를 실어주기만 하는 재배포 매체인지 판단한다."""
    hostname = (urlparse(article.link).hostname or "").casefold()
    if any(
        hostname == host or hostname.endswith(f".{host}")
        for host in AGGREGATOR_HOSTS
    ):
        return True
    source = article.source.casefold()
    return any(term in source for term in AGGREGATOR_SOURCE_TERMS)


def prefer_original_outlet(story: Story) -> Story:
    """대표 기사가 재배포 매체면 같은 사건의 원 언론사 기사로 바꾼다.

    독자가 원문 대신 네이트·MSN 같은 중계 페이지로 이동하는 것을 막는다.
    """
    if not is_aggregator(story.representative):
        return story
    originals = [item for item in story.related if not is_aggregator(item)]
    if not originals:
        return story
    replacement = max(originals, key=representative_score)
    remaining = tuple(
        item for item in story.articles if item is not replacement
    )
    return replace(story, representative=replacement, related=remaining)


def is_user_generated(article: Article) -> bool:
    """개인 블로그·카페 글인지 판단한다."""
    haystack = f"{article.source} {article.title} {urlparse(article.link).hostname or ''}"
    return any(term in haystack.casefold() for term in UGC_SOURCE_TERMS)


def relevance_score(article: Article) -> int:
    """개인정보·보안 사건일 가능성을 점수로 매긴다. 높을수록 관련도가 높다."""
    text = f"{article.title} {article.description}".casefold()
    signals = sum(1 for term in RELEVANCE_SIGNALS if term in text)
    noise = sum(1 for term in RELEVANCE_NOISE if term in text)
    return signals * 2 - noise


def filter_relevant(articles: list[Article]) -> list[Article]:
    """홍보·시장 뉴스를 걸러낸다. 너무 많이 걸러지면 원본을 그대로 둔다."""
    if not articles:
        return []
    news = [article for article in articles if not is_user_generated(article)]
    kept = [
        article for article in news if relevance_score(article) >= RELEVANCE_MIN_SCORE
    ]
    if len(kept) < len(articles) * RELEVANCE_MIN_KEPT_RATIO:
        print(
            f"[관련성] {len(articles)}건 중 {len(kept)}건만 남아 필터를 건너뜁니다. "
            "신호어 목록을 점검하세요.",
            file=sys.stderr,
        )
        return articles
    print(f"[관련성] {len(articles)}건 → {len(kept)}건")
    return kept


def token_weights(articles: Iterable[Article]) -> dict[str, float]:
    """후보 전체에서 흔한 단어의 비중을 낮추는 IDF 가중치를 만든다.

    "보안", "AI", "정부"처럼 검색 키워드 때문에 어디에나 나오는 단어는
    사건을 구분하지 못한다. 반대로 "네이버클라우드", "독파모"처럼 드문 단어가
    같은 사건임을 알려주므로 그쪽에 무게를 준다.
    """
    titles = [story_tokens(article.title) for article in articles]
    total = len(titles)
    if not total:
        return {}
    frequency: dict[str, int] = {}
    for tokens in titles:
        for token in tokens:
            frequency[token] = frequency.get(token, 0) + 1
    return {
        token: math.log(total / (1 + count)) + 1.0
        for token, count in frequency.items()
    }


def same_story(
    left: Article, right: Article, weights: dict[str, float] | None = None
) -> bool:
    """서로 다른 언론사의 기사가 같은 사건을 다루는지 판단한다."""
    left_title = story_tokens(left.title)
    right_title = story_tokens(right.title)
    shared_title = left_title & right_title
    if shared_title:
        scale = weights or {}

        def mass(tokens: set[str]) -> float:
            return sum(scale.get(token, 1.0) for token in tokens)

        smaller = min(mass(left_title), mass(right_title))
        if smaller and mass(shared_title) / smaller >= STORY_MATCH_RATIO:
            return True

    normalized_left = "".join(STORY_TOKEN_RE.findall(left.title)).casefold()
    normalized_right = "".join(STORY_TOKEN_RE.findall(right.title)).casefold()
    if SequenceMatcher(None, normalized_left, normalized_right).ratio() >= 0.62:
        return True

    left_detail = story_tokens(f"{left.title} {left.description}")
    right_detail = story_tokens(f"{right.title} {right.description}")
    shared_detail = left_detail & right_detail
    smaller_detail = min(len(left_detail), len(right_detail))
    return (
        smaller_detail >= 4
        and len(shared_detail) >= 4
        and len(shared_detail) / smaller_detail >= 0.5
    )


def representative_score(article: Article) -> tuple[int, int, float]:
    """언론사 신뢰도, 원문성, 정보량, 최신성으로 대표 기사 순위를 정한다."""
    source = article.source.casefold()
    credibility = 50
    for score, names in SOURCE_TIERS:
        if any(name in source for name in names):
            credibility = score
            break

    if is_aggregator(article):
        credibility -= 15
    else:
        credibility += 10

    information = min(len(clean_text(article.description)), 400)
    published = article.published or datetime.min.replace(tzinfo=timezone.utc)
    return credibility, information, published.timestamp()


def cluster_stories(articles: Iterable[Article]) -> list[list[Article]]:
    """제목·본문 유사도로 같은 사건을 다룬 기사들을 묶는다."""
    pool = list(articles)
    weights = token_weights(pool)
    clusters: list[list[Article]] = []
    for article in pool:
        for cluster in clusters:
            if any(same_story(article, existing, weights) for existing in cluster):
                cluster.append(article)
                break
        else:
            clusters.append([article])
    return clusters


def partition_recent_articles(
    articles: Iterable[Article],
    window_hours: int = DEFAULT_WINDOW_HOURS,
    excluded_keys: set[str] | None = None,
    now: datetime | None = None,
) -> tuple[list[Article], list[Article]]:
    """시간 창 안의 기사를 (아직 안 보낸 기사, 이미 보낸 기사)로 나눈다.

    이미 보낸 쪽도 버리지 않고 돌려준다. 브리핑 끝에 간단히 함께 보여주려면
    개수만으로는 부족하고 기사 자체가 필요하다.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    start = current - timedelta(hours=window_hours)
    excluded = excluded_keys or set()

    unsent: list[Article] = []
    reported: list[Article] = []
    for article in articles:
        if article.published is None:
            continue
        published = article.published.astimezone(timezone.utc)
        # 발행 시각이 미래로 밀린 피드가 있어 약간의 여유를 둔다.
        if published < start or published > current + timedelta(minutes=10):
            continue
        target = reported if article_keys(article) & excluded else unsent
        target.append(article)

    def finalize(pool: list[Article]) -> list[Article]:
        # 창 안에서만 합친다. 먼저 합치면 창 밖의 사본이 대표로 뽑혀 기사가 통째로 사라진다.
        merged = merge_duplicate_articles(pool)
        merged.sort(
            key=lambda item: item.published
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return merged

    return finalize(unsent), finalize(reported)


def select_recent_articles(
    articles: Iterable[Article],
    window_hours: int = DEFAULT_WINDOW_HOURS,
    excluded_keys: set[str] | None = None,
    now: datetime | None = None,
) -> list[Article]:
    """시간 창 안에 있고 아직 보고하지 않은 기사를 최신순으로 모은다."""
    unsent, _ = partition_recent_articles(
        articles, window_hours=window_hours, excluded_keys=excluded_keys, now=now
    )
    return unsent


CURATION_SCHEMA = {
    "type": "object",
    "properties": {
        "stories": {
            "type": "array",
            "description": "보고할 사건 목록. 중요한 사건을 먼저 놓는다.",
            "items": {
                "type": "object",
                "properties": {
                    "headline": {
                        "type": "string",
                        "description": "사건을 한 줄로 설명하는 한국어 제목",
                    },
                    "summary": {
                        "type": "string",
                        "description": "기사에 실제로 담긴 사실만 담은 두 문장 이내 한국어 요약",
                    },
                    "importance": {
                        "type": "integer",
                        "description": "실무 중요도. 5가 가장 높다.",
                        "enum": [1, 2, 3, 4, 5],
                    },
                    "representative": {
                        "type": "integer",
                        "description": "대표로 삼을 후보 기사 번호",
                    },
                    "related": {
                        "type": "array",
                        "description": "같은 사건을 다룬 다른 후보 기사 번호",
                        "items": {"type": "integer"},
                    },
                },
                "required": [
                    "headline",
                    "summary",
                    "importance",
                    "representative",
                    "related",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["stories"],
    "additionalProperties": False,
}

CURATION_SYSTEM_PROMPT = """\
당신은 기업 개인정보보호·정보보안 담당자를 위한 뉴스 큐레이터입니다.
검색 피드에서 모은 후보 기사 목록을 받아, 담당자가 아침에 읽어야 할 것만 골라
주제별로 정리합니다.

선별 기준:
- 개인정보 보호나 정보보안 거버넌스 관점에서 실무적 의미가 있는 기사만 남깁니다.
- 단순 신제품·서비스 출시, 주가·실적, 인사, 수상, 홍보성 보도자료, 검색어만
  걸린 무관한 기사는 제외합니다. 남길 것이 적으면 적게 남기세요.

사건 묶기:
- 여러 언론사가 같은 사건을 보도한 경우 하나의 story로 묶고 나머지는 related에 넣습니다.
- 같은 기관·기업이 등장해도 사건이 다르면(예: 소명 절차와 과징금 처분) 별도 story로 둡니다.
- 대표 기사는 원 언론사 기사를 고릅니다. 네이트, 다음, MSN처럼 남의 기사를 실어주는
  매체나 제목만 있는 기사는 피하고, 내용이 구체적인 기사를 대표로 삼습니다.

중요도:
- 5: 대규모 유출 사고, 과징금·제재 처분, 법령·고시 개정 확정
- 3~4: 조사 착수, 제도 예고, 주요 기관의 정책 발표, 중대한 취약점 공개
- 1~2: 행사·공모전, 협약, 일반 동향 소개

요약:
- 후보 목록에 실제로 나온 사실만 씁니다. 추측하거나 없는 수치를 만들지 마세요.
- 제목만 있고 설명이 없는 후보는 제목에서 확인되는 사실만 요약에 씁니다.

번호는 반드시 후보 목록에 있는 번호만 사용하고, 한 기사를 두 story에 넣지 마세요."""


def format_candidates(articles: list[Article]) -> str:
    """후보 기사를 번호가 붙은 한 줄짜리 목록으로 만든다."""
    lines: list[str] = []
    for index, article in enumerate(articles):
        published = (
            article.published.astimezone(KST).strftime("%m-%d %H:%M")
            if article.published
            else "시각 미상"
        )
        parts = [f"[{index}]", article.title, f"| {article.source} | {published}"]
        description = clean_text(article.description)
        if description and description != article.title:
            parts.append(f"| {description[:CURATION_DESCRIPTION_LIMIT]}")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def build_stories(
    payload: dict[str, object], articles: list[Article], limit: int
) -> list[Story]:
    """모델이 돌려준 사건 목록을 검증해 Story로 바꾼다."""
    entries = payload.get("stories")
    if not isinstance(entries, list):
        raise RuntimeError("큐레이션 응답에 stories가 없습니다.")

    stories: list[Story] = []
    used: set[int] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        index = entry.get("representative")
        if not isinstance(index, int) or not 0 <= index < len(articles):
            continue
        if index in used:
            continue
        related_indexes = [
            value
            for value in entry.get("related", [])
            if isinstance(value, int)
            and 0 <= value < len(articles)
            and value != index
            and value not in used
        ]
        used.add(index)
        used.update(related_indexes)
        importance = entry.get("importance")
        stories.append(
            prefer_original_outlet(
                Story(
                    # 구획은 검색 키워드에서 나중에 채운다.
                    section="",
                    headline=clean_text(str(entry.get("headline", "")))
                    or articles[index].title,
                    summary=clean_text(str(entry.get("summary", ""))),
                    importance=importance if isinstance(importance, int) else 3,
                    representative=articles[index],
                    related=tuple(articles[value] for value in related_indexes),
                )
            )
        )

    if not stories:
        raise RuntimeError("큐레이션 결과에서 유효한 기사를 찾지 못했습니다.")
    stories.sort(key=story_heat, reverse=True)
    return stories[:limit]


def curate_with_claude(
    articles: list[Article],
    limit: int,
    api_key: str,
    timeout: float = CURATION_TIMEOUT_SECONDS,
    model: str = CURATION_MODEL,
) -> list[Story]:
    """Claude로 후보 기사를 선별·묶고 주제별로 정리한다."""
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "anthropic 패키지가 없습니다. pip install anthropic으로 설치하세요."
        ) from exc

    candidates = articles[:CURATION_MAX_CANDIDATES]
    if len(articles) > CURATION_MAX_CANDIDATES:
        print(
            f"[큐레이션] 후보가 많아 최신 {CURATION_MAX_CANDIDATES}건만 넘깁니다"
            f"(전체 {len(articles)}건).",
            file=sys.stderr,
        )

    # api_key를 직접 넘기면 SDK의 자격증명 체인이 건너뛰어져
    # ANTHROPIC_WORKSPACE_ID가 헤더에 붙지 않으므로 직접 지정한다.
    workspace_id = get_anthropic_workspace_id()
    headers = {"anthropic-workspace-id": workspace_id} if workspace_id else {}
    # retry_operation이 바깥에서 한 번 더 감싸므로 SDK 재시도는 1회로 둔다.
    client = anthropic.Anthropic(
        api_key=api_key,
        timeout=timeout,
        max_retries=1,
        default_headers=headers,
    )
    prompt = (
        f"후보 기사 {len(candidates)}건입니다. "
        f"이 가운데 보고할 만한 사건을 최대 {limit}개까지 골라 정리하세요.\n\n"
        f"{format_candidates(candidates)}"
    )
    try:
        with client.messages.stream(
            model=model,
            max_tokens=CURATION_MAX_TOKENS,
            system=CURATION_SYSTEM_PROMPT,
            output_config={
                "format": {"type": "json_schema", "schema": CURATION_SCHEMA}
            },
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            response = stream.get_final_message()
    except anthropic.APIStatusError as exc:
        message = f"Claude API {exc.status_code} 오류: {exc.message}"
        # 429와 5xx는 잠시 뒤 성공할 수 있지만, 나머지 4xx는 요청·설정 자체의 문제다.
        if exc.status_code == 429 or exc.status_code >= 500:
            raise RuntimeError(message) from exc
        raise PermanentError(message) from exc
    except anthropic.APIConnectionError as exc:
        raise RuntimeError(f"Claude API에 연결할 수 없습니다: {exc}") from exc

    if response.stop_reason == "refusal":
        raise PermanentError("Claude가 큐레이션 요청을 거절했습니다.")
    usage = response.usage
    print(
        f"[큐레이션] 모델 {model} · 입력 {usage.input_tokens:,} 토큰 · "
        f"출력 {usage.output_tokens:,} 토큰"
    )

    text = next((block.text for block in response.content if block.type == "text"), "")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("큐레이션 응답이 JSON이 아닙니다.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("큐레이션 응답 형식이 올바르지 않습니다.")
    return build_stories(payload, candidates, limit)


HEAT_COVERAGE_CAP = 20
HEAT_RELAY_PENALTY = 5
# 이미 안내한 사건은 목록만 짧게 붙인다. 길어지면 새 사건이 묻힌다.
REVISITED_LIMIT = 8


def story_heat(story: Story) -> tuple[int, float]:
    """지금 가장 이슈인 사건이 위로 오도록 순위 점수를 만든다.

    중요도가 주된 기준이지만, 몇 개 매체가 달려들었는지도 화제성을 그대로
    보여주는 신호다. 그래서 보도량이 많은 사건은 중요도가 한 단계 낮아도
    앞설 수 있게 두고, 같으면 최신 기사를 먼저 놓는다.

    원문 링크를 구할 수 없어 재배포본이 대표로 남은 사건은 조금 낮춘다.
    단독 보도를 버리지는 않되, 같은 급이면 원문이 있는 사건에 자리를 준다.
    """
    coverage = min(len(story.articles), HEAT_COVERAGE_CAP)
    score = story.importance * 10 + coverage
    if is_aggregator(story.representative):
        score -= HEAT_RELAY_PENALTY
    published = story.representative.published or datetime.min.replace(
        tzinfo=timezone.utc
    )
    return score, published.timestamp()


def coverage_importance(outlets: int) -> int:
    """몇 개 언론사가 보도했는지로 중요도를 어림한다.

    여러 매체가 같은 사건을 동시에 다루면 그만큼 사안이 크다는 뜻이다.
    모델 없이 쓸 수 있는 신호 중에서는 최신순보다 훨씬 쓸모 있다.
    """
    for threshold, score in ((10, 5), (5, 4), (3, 3), (2, 2)):
        if outlets >= threshold:
            return score
    return 1


def curate_heuristically(articles: list[Article], limit: int) -> list[Story]:
    """Claude를 쓸 수 없을 때 휴리스틱으로 사건을 묶고 순위를 매긴다."""
    stories: list[Story] = []
    for cluster in cluster_stories(articles):
        representative = max(cluster, key=representative_score)
        related = tuple(item for item in cluster if item is not representative)
        stories.append(
            prefer_original_outlet(
                Story(
                    section="",
                    headline=representative.title,
                    summary=summarize(representative, max_length=200),
                    importance=coverage_importance(len(cluster)),
                    representative=representative,
                    related=related,
                )
            )
        )

    stories.sort(key=story_heat, reverse=True)
    return stories[:limit]


HEURISTIC_CURATOR = "규칙 기반 정리"


def curate(
    articles: list[Article],
    limit: int,
    api_key: str | None,
) -> tuple[list[Story], str]:
    """Claude 큐레이션을 시도하고 실패하면 휴리스틱으로 되돌린다.

    반환값의 두 번째 항목은 실제로 어느 방식이 쓰였는지를 나타낸다. 보고서에
    표시할 안내가 사실과 어긋나지 않도록 호출한 쪽이 이 값을 그대로 써야 한다.
    """
    if not articles:
        return [], HEURISTIC_CURATOR
    if not api_key:
        print("[큐레이션] API 키가 없어 휴리스틱으로 정리합니다.", file=sys.stderr)
        return curate_heuristically(articles, limit), HEURISTIC_CURATOR
    try:
        stories = retry_operation(
            "Claude 큐레이션",
            lambda: curate_with_claude(articles, limit, api_key),
            attempts=2,
        )
        return stories, CURATION_MODEL
    except RuntimeError as exc:
        print(
            f"[큐레이션] Claude 정리에 실패해 휴리스틱으로 대체합니다: {exc}",
            file=sys.stderr,
        )
        return curate_heuristically(articles, limit), HEURISTIC_CURATOR


OTHER_SECTION = "기타"


def story_section(story: Story, keywords: list[str]) -> str:
    """사건이 속할 구획을 정한다.

    여러 키워드에 걸리면 그 사건을 가장 많이 찾아낸 키워드를 쓴다. 키워드
    순서만으로 정하면 목록 앞에 있는 키워드가 무관한 사건까지 가져간다.
    """
    hits: dict[str, int] = {}
    for article in story.articles:
        for keyword in article.keywords:
            hits[keyword] = hits.get(keyword, 0) + 1
    if not hits:
        return OTHER_SECTION
    ranked = [keyword for keyword in keywords if keyword in hits]
    if not ranked:
        return OTHER_SECTION
    # 동률이면 키워드를 준 순서를 따른다.
    return max(ranked, key=lambda keyword: (hits[keyword], -keywords.index(keyword)))


def group_sections(
    stories: Iterable[Story], keywords: list[str]
) -> dict[str, list[Story]]:
    """Story를 검색 키워드별 구획으로 묶고 화제성 순으로 정렬한다.

    구획 안에서도, 구획끼리도 지금 가장 이슈인 사건이 앞에 온다. 한 기사가
    여러 키워드에 걸려도 구획 하나에만 넣으므로 같은 사건이 두 번 나오지 않는다.
    """
    grouped: dict[str, list[Story]] = {keyword: [] for keyword in keywords}
    for story in stories:
        section = story_section(story, keywords)
        grouped.setdefault(section, []).append(replace(story, section=section))
    for entries in grouped.values():
        entries.sort(key=story_heat, reverse=True)

    # 가장 이슈인 사건을 품은 구획을 앞에 놓고, 결과가 없는 구획은 뒤로 보낸다.
    # 빈 구획도 남겨 어떤 주제를 검색했는지 보이게 한다.
    def section_heat(entries: list[Story]) -> tuple[int, float]:
        return max((story_heat(story) for story in entries), default=(0, 0.0))

    return dict(
        sorted(grouped.items(), key=lambda item: section_heat(item[1]), reverse=True)
    )


def history_path() -> Path:
    """전송 이력을 저장할 사용자별 로컬 파일 경로를 반환한다."""
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "PrivacyNewsReport" / "sent_articles.json"


def load_sent_history(path: Path | None = None) -> dict[str, str]:
    """최근 전송 이력을 읽고 보관 기간이 지난 항목을 제외한다."""
    target = path or history_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        records = payload.get("sent", {})
        if not isinstance(records, dict):
            return {}
    except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(days=HISTORY_RETENTION_DAYS)
    retained: dict[str, str] = {}
    for key, value in records.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        try:
            recorded_at = datetime.fromisoformat(value)
            if recorded_at.tzinfo is None:
                recorded_at = recorded_at.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if recorded_at.astimezone(timezone.utc) >= cutoff:
            retained[key] = value
    return retained


def save_sent_history(
    records: dict[str, str], new_keys: set[str], path: Path | None = None
) -> None:
    """성공적으로 전송한 기사 키를 원자적으로 로컬 이력에 저장한다."""
    target = path or history_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    recorded_at = datetime.now(timezone.utc).isoformat()
    updated = dict(records)
    updated.update({key: recorded_at for key in new_keys})
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"version": 1, "sent": updated}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(target)


def format_date(value: datetime | None) -> str:
    if value is None:
        return "시각 미상"
    korea_time = value.astimezone(KST)
    return korea_time.strftime("%Y-%m-%d %H:%M KST")


CURATOR_LABELS = {"claude-haiku-4-5": "Claude Haiku 4.5"}


def curator_label(curator: str) -> str:
    return CURATOR_LABELS.get(curator, curator or HEURISTIC_CURATOR)


def notice_lines(stats: RunStats) -> list[str]:
    """보고서 최상단에 넣을 처리 내역 안내를 만든다.

    폴백으로 정리된 실행에 모델이 처리했다고 적으면 안 되므로, 실제로 쓰인
    큐레이터를 그대로 적는다.
    """
    trimmed: list[str] = []
    if stats.already_sent:
        trimmed.append(f"이미 보낸 {stats.already_sent}건")
    if stats.irrelevant:
        trimmed.append(f"관련 없는 {stats.irrelevant}건")
    if stats.folded:
        trimmed.append(f"같은 사건 중복 보도 {stats.folded}건")

    first = f"수집 {stats.collected}건 → 사건 {stats.stories}건"
    if trimmed:
        first += f" · {', '.join(trimmed)}을 빼고 정리했습니다."

    if stats.curator == HEURISTIC_CURATOR:
        second = (
            "모델을 쓸 수 없어 규칙 기반으로 정리했습니다. "
            "관련성 판단이 들어가지 않아 무관한 기사가 섞일 수 있습니다."
        )
    else:
        # 한 줄에 들어가도록 짧게 쓴다. 길어지면 PC 화면에서 단어 몇 개만 다음 줄로 넘어간다.
        second = (
            f"{curator_label(stats.curator)}가 관련성을 판단해 무관한 기사를 걸러내고, "
            "같은 사건 보도를 하나로 묶어 대표 기사·중요도·요약을 정했습니다."
        )
    lines = [first, second]
    if stats.failed_keywords:
        lines.append(f"주제 {stats.failed_keywords}개는 수집에 실패했습니다.")
    return lines


def story_summary(story: Story, max_length: int = 200) -> str:
    """큐레이션 요약이 없으면 원문 설명에서 뽑아 쓴다."""
    if story.summary:
        return story.summary[:max_length]
    return summarize(story.representative, max_length=max_length)


def display_sections(sections: dict[str, list[Story]]) -> None:
    for title, stories in sections.items():
        print(f"\n{'=' * 72}\n{title} ({len(stories)}건)\n{'=' * 72}")
        if not stories:
            print("검색된 뉴스가 없습니다.")
            continue
        for number, story in enumerate(stories, start=1):
            article = story.representative
            print(f"\n{number}. [중요도 {story.importance}] {story.headline}")
            print(f"   출처: {article.source} | {format_date(article.published)}")
            print(f"   요약: {story_summary(story)}")
            print(f"   링크: {article.link}")
            if story.related:
                names = ", ".join(item.source for item in story.related)
                print(f"   관련 보도 {len(story.related)}건: {names}")


def revisited_teams_block(
    revisited: tuple[Story, ...], hidden: int = 0
) -> list[str]:
    """이미 안내한 사건을 한 줄씩만 붙인다."""
    if not revisited:
        return []
    lines = [
        "<hr>",
        f"<h3>이미 안내한 사건 ({len(revisited)}건)</h3>",
        "<p><small>지난 브리핑에서 보낸 사건입니다. 보도가 이어지고 있어 제목만 함께 적었습니다."
        "</small></p>",
        "<ul>",
    ]
    for story in revisited:
        article = story.representative
        lines.append(
            "<li>"
            f'<a href="{html.escape(article.link, quote=True)}">'
            f"{html.escape(story.headline)}</a> "
            f"<small>{html.escape(article.source)} · 보도 {len(story.articles)}건</small>"
            "</li>"
        )
    lines.append("</ul>")
    if hidden > 0:
        lines.append(
            f"<p><small>이 밖에 사건 {hidden}건은 전체 보고서에서 확인하세요.</small></p>"
        )
    return lines


def display_revisited(revisited: tuple[Story, ...]) -> None:
    if not revisited:
        return
    print(f"\n{'=' * 72}\n이미 안내한 사건 ({len(revisited)}건)\n{'=' * 72}")
    for number, story in enumerate(revisited, start=1):
        article = story.representative
        print(f"{number}. {story.headline}")
        print(f"   {article.source} · 보도 {len(story.articles)}건 · {article.link}")


def build_teams_message(
    sections: dict[str, list[Story]],
    stats: RunStats | None = None,
    report_url: str | None = None,
    revisited: tuple[Story, ...] = (),
    revisited_hidden: int = 0,
) -> dict[str, str]:
    """큐레이션한 구획 전체를 Teams 일반 메시지용 HTML 본문 하나로 만든다."""
    collected_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    total = sum(len(stories) for stories in sections.values())
    lines = [
        "<h2>📰 개인정보 보호 · AI 보안 뉴스 브리핑</h2>",
        f"<p><em>수집 시각: {html.escape(collected_at)} · 새 사건 {total}건</em></p>",
    ]
    if report_url:
        link = html.escape(report_url, quote=True)
        lines.append(
            f'<p>📄 <a href="{link}"><strong>전체 HTML 보고서 보기</strong></a>'
            "<br><small>보고서 페이지는 이미 보낸 기사까지 포함한 전체 목록입니다.</small></p>"
        )
    if stats is not None:
        notice = "<br>".join(html.escape(line) for line in notice_lines(stats))
        lines.append(f"<blockquote><small>{notice}</small></blockquote>")
    if not total:
        lines.append("<p>보고할 새 뉴스가 없습니다.</p>")
        lines.extend(revisited_teams_block(revisited, revisited_hidden))
        return {"text": "".join(lines)}

    for title, stories in sections.items():
        lines.extend(
            [
                "<hr>",
                f"<h3>{html.escape(title)} ({len(stories)}건)</h3>",
            ]
        )
        if not stories:
            lines.append("<p>검색된 뉴스가 없습니다.</p>")
            continue
        lines.append("<ol>")
        for story in stories:
            article = story.representative
            headline = html.escape(story.headline)
            link = html.escape(article.link, quote=True)
            source = html.escape(article.source)
            published = html.escape(format_date(article.published))
            summary = html.escape(story_summary(story, max_length=160))
            entry = [
                "<li>",
                f'<strong><a href="{link}">{headline}</a></strong><br>',
                f"<small>{source} · {published} · 중요도 {story.importance}</small><br>",
                summary,
            ]
            if story.related:
                names = html.escape(
                    ", ".join(item.source for item in story.related[:4])
                )
                entry.append(f"<br><small>관련 보도: {names}</small>")
            entry.append("</li>")
            lines.append("".join(entry))
        lines.append("</ol>")

    lines.extend(revisited_teams_block(revisited, revisited_hidden))
    return {"text": "".join(lines)}


def send_to_teams(
    webhook_url: str,
    report: Report,
    timeout: float,
    report_url: str | None = None,
) -> None:
    """큐레이션한 뉴스가 담긴 일반 메시지 하나를 Teams로 전송한다."""
    data = json.dumps(
        build_teams_message(
            report.sections,
            report.stats,
            report_url,
            report.revisited,
            report.revisited_hidden,
        ),
        ensure_ascii=False,
    ).encode("utf-8")
    if len(data) > TEAMS_MAX_PAYLOAD_BYTES:
        raise RuntimeError(
            f"Teams 메시지가 너무 큽니다({len(data):,}바이트). "
            "-n 옵션으로 주제별 기사 수를 줄여 주세요."
        )
    request = Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status >= 300:
                raise RuntimeError(f"Teams가 HTTP {response.status} 오류를 반환했습니다.")
    except HTTPError as exc:
        raise RuntimeError(f"Teams가 HTTP {exc.code} 오류를 반환했습니다.") from exc
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLError):
            post_with_curl(webhook_url, data, timeout)
        else:
            raise RuntimeError(f"Teams에 연결할 수 없습니다: {reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError("Teams 전송 시간이 초과되었습니다.") from exc


def parse_keywords(raw_keywords: list[str]) -> list[str]:
    """쉼표로 묶인 인자를 포함해 키워드 목록을 정리한다."""
    keywords: list[str] = []
    seen: set[str] = set()
    for raw in raw_keywords:
        for value in raw.split(","):
            keyword = value.strip()
            if keyword and keyword.casefold() not in seen:
                seen.add(keyword.casefold())
                keywords.append(keyword)
    return keywords


def get_user_setting(name: str) -> str | None:
    """프로세스 또는 Windows 사용자 환경에서 설정값을 읽는다."""
    value = os.environ.get(name)
    if value:
        return value.strip()
    if sys.platform != "win32":
        return None

    # 작업 스케줄러 서비스가 변경 전 환경을 캐시한 경우를 보완한다.
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value).strip() or None
    except (FileNotFoundError, OSError):
        return None


def save_user_setting(name: str, value: str) -> None:
    """현재 프로세스와 Windows 사용자 환경에 갱신된 설정을 저장한다."""
    os.environ[name] = value
    if sys.platform != "win32":
        return
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            "Environment",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    except OSError as exc:
        raise RuntimeError(f"사용자 환경변수 {name} 갱신에 실패했습니다.") from exc


def get_teams_webhook_url() -> str | None:
    return get_user_setting("TEAMS_WEBHOOK_URL")


def get_anthropic_api_key() -> str | None:
    return get_user_setting("ANTHROPIC_API_KEY")


def get_report_url() -> str:
    """HTML 보고서가 올라가는 주소를 반환한다."""
    return get_user_setting("REPORT_URL") or DEFAULT_REPORT_URL


def get_anthropic_workspace_id() -> str | None:
    """조직 계정의 identity-linked 키는 워크스페이스 ID를 함께 보내야 한다."""
    return get_user_setting("ANTHROPIC_WORKSPACE_ID")


def get_kakao_config() -> KakaoConfig | None:
    names = {
        "rest_api_key": "KAKAO_REST_API_KEY",
        "client_secret": "KAKAO_CLIENT_SECRET",
        "refresh_token": "KAKAO_REFRESH_TOKEN",
    }
    values = {field: get_user_setting(name) for field, name in names.items()}
    configured = [value is not None for value in values.values()]
    if not any(configured):
        return None
    if not all(configured):
        missing = [name for field, name in names.items() if not values[field]]
        raise RuntimeError(f"카카오 환경변수가 부족합니다: {', '.join(missing)}")
    return KakaoConfig(
        rest_api_key=values["rest_api_key"] or "",
        client_secret=values["client_secret"] or "",
        refresh_token=values["refresh_token"] or "",
        link_url=get_user_setting("KAKAO_LINK_URL") or get_report_url(),
    )


def refresh_kakao_access_token(config: KakaoConfig, timeout: float) -> str:
    result = post_kakao_form(
        KAKAO_TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "client_id": config.rest_api_key,
            "refresh_token": config.refresh_token,
            "client_secret": config.client_secret,
        },
        timeout,
    )
    access_token = result.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("카카오 액세스 토큰이 응답에 없습니다.")
    new_refresh_token = result.get("refresh_token")
    if isinstance(new_refresh_token, str) and new_refresh_token:
        save_user_setting("KAKAO_REFRESH_TOKEN", new_refresh_token)
    return access_token


def build_kakao_text(sections: dict[str, list[Story]]) -> str:
    """HTML 뉴스 보고서를 안내하는 카카오 메시지를 만든다."""
    collected_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    story_count = sum(len(stories) for stories in sections.values())
    filled = sum(1 for stories in sections.values() if stories)
    return (
        "개인정보 보호 · AI 보안 뉴스 브리핑\n"
        f"{filled}개 주제에서 새 사건 {story_count}건을 정리했습니다.\n"
        f"업데이트: {collected_at}\n"
        "아래 버튼을 눌러 전체 HTML 보고서를 확인하세요."
    )[:200]


def send_topics_to_kakao(
    config: KakaoConfig,
    sections: dict[str, list[Story]],
    timeout: float,
) -> None:
    """액세스 토큰을 갱신하고 HTML 보고서 링크를 한 번만 보낸다."""
    access_token = refresh_kakao_access_token(config, timeout)
    template = {
        "object_type": "text",
        "text": build_kakao_text(sections),
        "link": {
            "web_url": config.link_url,
            "mobile_web_url": config.link_url,
        },
        "button_title": "HTML 뉴스 보고서 보기",
    }
    result = post_kakao_form(
        KAKAO_MEMO_URL,
        {"template_object": json.dumps(template, ensure_ascii=False)},
        timeout,
        access_token=access_token,
    )
    if result.get("result_code") != 0:
        raise RuntimeError(f"카카오 메시지 전송 실패: {result}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="키워드별 최신 뉴스를 수집해 요약해서 보여줍니다."
    )
    parser.add_argument(
        "keywords",
        nargs="*",
        help="검색할 키워드. 생략하면 개인정보 보호·AI 보안 기본 주제를 검색합니다.",
    )
    parser.add_argument(
        "-n",
        "--limit",
        type=int,
        default=12,
        help="보고서 전체에 담을 사건 수 (기본값: 12)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="뉴스 요청 제한 시간(초, 기본값: 10)",
    )
    parser.add_argument(
        "--window-hours",
        type=int,
        default=DEFAULT_WINDOW_HOURS,
        help=f"이 시간 안에 게시된 기사만 후보로 씁니다 (기본값: {DEFAULT_WINDOW_HOURS})",
    )
    parser.add_argument(
        "--no-claude",
        action="store_true",
        help="Claude 큐레이션을 건너뛰고 휴리스틱으로만 정리합니다.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0,
        metavar="MINUTES",
        help="지정한 분 간격으로 반복 실행합니다. 0이면 한 번만 실행합니다.",
    )
    parser.add_argument(
        "--no-teams",
        action="store_true",
        help="TEAMS_WEBHOOK_URL이 설정되어 있어도 Teams 전송을 생략합니다.",
    )
    parser.add_argument(
        "--kakao",
        action="store_true",
        help="카카오 환경변수가 설정된 경우 카카오톡 전송을 명시적으로 사용합니다.",
    )
    parser.add_argument(
        "--no-kakao",
        action="store_true",
        help="카카오톡 전송을 생략합니다(기본 동작이며 이전 명령과의 호환용).",
    )
    return parser


def collect_candidates(
    keywords: list[str],
    timeout: float,
    window_hours: int,
) -> tuple[list[Article], int]:
    """모든 키워드의 기사를 하나의 풀로 모은다. 반환값은 (기사, 실패 키워드 수)."""
    pool: list[Article] = []
    failures = 0
    for keyword in keywords:
        print(f"[수집] {keyword}")
        try:
            pool.extend(
                retry_operation(
                    f"뉴스 수집({keyword})",
                    lambda keyword=keyword: fetch_news(
                        keyword, timeout=timeout, window_hours=window_hours
                    ),
                )
            )
        except RuntimeError as exc:
            failures += 1
            print(f"[경고] {keyword}: {exc}", file=sys.stderr)
    return pool, failures


def build_report(
    keywords: list[str],
    limit: int,
    timeout: float,
    window_hours: int,
    api_key: str | None,
    excluded_keys: set[str] | None = None,
) -> Report:
    """후보를 모아 큐레이션한 구획과 처리 내역을 만든다."""
    pool, failures = collect_candidates(keywords, timeout, window_hours)
    candidates, seen = partition_recent_articles(
        pool, window_hours=window_hours, excluded_keys=excluded_keys or set()
    )
    print(
        f"[후보] 수집 {len(pool)}건 → 최근 {window_hours}시간 내 신규 {len(candidates)}건"
    )

    relevant = filter_relevant(candidates)
    stories, curator = curate(relevant, limit, api_key)
    sections = group_sections(stories, keywords)
    filled = sum(1 for entries in sections.values() if entries)
    reported = [story for entries in sections.values() for story in entries]

    # 이미 보낸 기사는 모델을 다시 부르지 않는다. 한 번 안내한 사건이라
    # 제목과 보도량만 있으면 되고, 묶는 데는 규칙만으로 충분하다.
    # 몇 개 사건을 못 실었는지 알려면 자르기 전에 전체를 묶어야 한다.
    clustered = curate_heuristically(seen, len(seen)) if seen else []
    revisited = clustered[:REVISITED_LIMIT]

    stats = RunStats(
        collected=len(pool),
        fresh=len(candidates) + len(seen),
        already_sent=len(seen),
        irrelevant=len(candidates) - len(relevant),
        folded=sum(len(story.articles) for story in reported) - len(reported),
        stories=len(reported),
        curator=curator,
        failed_keywords=failures,
    )
    print(f"[정리] 구획 {filled}/{len(sections)}개 · 사건 {stats.stories}건")
    if clustered:
        print(
            f"[재확인] 이미 보낸 사건 {len(clustered)}건 가운데 "
            f"{len(revisited)}건을 함께 적습니다."
        )
    return Report(
        sections=sections,
        stats=stats,
        revisited=tuple(revisited),
        revisited_hidden=len(clustered) - len(revisited),
    )


def run_news_cycle(
    keywords: list[str],
    limit: int,
    timeout: float,
    window_hours: int,
    webhook_url: str | None,
    kakao_config: KakaoConfig | None,
    api_key: str | None,
) -> int:
    """뉴스 수집·큐레이션과 콘솔 출력 및 Teams 전송을 한 차례 수행한다."""
    print(f"{', '.join(keywords)} 주제의 새 뉴스를 가져오는 중입니다...")
    history = load_sent_history()
    report = build_report(
        keywords, limit, timeout, window_hours, api_key, set(history)
    )
    sections, stats = report.sections, report.stats
    failures = stats.failed_keywords
    display_sections(sections)
    display_revisited(report.revisited)
    for line in notice_lines(stats):
        print(f"[안내] {line}")

    # 본문에 실린 사건만 이력에 남긴다. 이미 안내한 목록은 다시 기록할 필요가 없다.
    reported_keys: set[str] = set()
    for stories in sections.values():
        for story in stories:
            for article in story.articles:
                reported_keys.update(article_keys(article))

    # 사건이 하나도 없으면 "전부 0건" 알림을 보내지 않는다. HTML 보고서는 그래도 갱신된다.
    if not report.total:
        print("\n보고할 새 뉴스가 없어 전송을 생략합니다.")
        return 1 if keywords and failures == len(keywords) else 0

    if webhook_url:
        try:
            retry_operation(
                "Teams 전송",
                lambda: send_to_teams(
                    webhook_url, report, timeout, get_report_url()
                ),
            )
            print(f"[Teams] 사건 {report.total}건을 본문 하나로 전송 완료")
        except RuntimeError as exc:
            print(f"\n[Teams] 전송 실패: {exc}", file=sys.stderr)
            return 1

    if kakao_config:
        try:
            retry_operation(
                "카카오톡 전송",
                lambda: send_topics_to_kakao(kakao_config, sections, timeout),
            )
            print("[KakaoTalk] HTML 뉴스 보고서 링크 전송 완료")
        except RuntimeError as exc:
            print(f"\n[KakaoTalk] 전송 실패: {exc}", file=sys.stderr)
            return 1

    if reported_keys and (webhook_url or kakao_config):
        try:
            save_sent_history(history, reported_keys)
            print(f"[이력] 보고한 기사 {len(reported_keys)}개 식별값 저장 완료")
        except OSError as exc:
            print(f"\n[이력] 저장 실패: {exc}", file=sys.stderr)
            return 1

    return 1 if keywords and failures == len(keywords) else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit < 1:
        print("오류: 기사 수는 1 이상이어야 합니다.", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print("오류: 요청 제한 시간은 0보다 커야 합니다.", file=sys.stderr)
        return 2
    if args.interval < 0:
        print("오류: 반복 간격은 0 이상이어야 합니다.", file=sys.stderr)
        return 2
    if args.window_hours < 1:
        print("오류: 시간 창은 1 이상이어야 합니다.", file=sys.stderr)
        return 2

    keywords = parse_keywords(args.keywords) or DEFAULT_KEYWORDS.copy()
    api_key = None if args.no_claude else get_anthropic_api_key()
    if not args.no_claude and not api_key:
        print(
            "안내: ANTHROPIC_API_KEY가 없어 휴리스틱 정리로 실행합니다.",
            file=sys.stderr,
        )
    webhook_url = None if args.no_teams else get_teams_webhook_url()
    if not webhook_url and not args.no_teams:
        print(
            "오류: TEAMS_WEBHOOK_URL이 설정되어 있지 않습니다. "
            "Teams 전송 없이 실행하려면 --no-teams를 사용하세요.",
            file=sys.stderr,
        )
        return 2
    try:
        kakao_config = (
            get_kakao_config() if args.kakao and not args.no_kakao else None
        )
    except RuntimeError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    if args.kakao and not kakao_config:
        print(
            "안내: 카카오 환경변수가 없어 카카오톡 전송을 생략합니다.",
            file=sys.stderr,
        )

    while True:
        result = run_news_cycle(
            keywords,
            args.limit,
            args.timeout,
            args.window_hours,
            webhook_url,
            kakao_config,
            api_key,
        )
        if args.interval == 0:
            return result
        next_run = datetime.now() + timedelta(minutes=args.interval)
        print(
            f"\n다음 실행: {next_run.strftime('%Y-%m-%d %H:%M:%S')} "
            "(중지: Ctrl+C)"
        )
        try:
            time.sleep(args.interval * 60)
        except KeyboardInterrupt:
            print("\n정기 실행을 종료합니다.")
            return 0


def run_with_logging(argv: list[str] | None = None) -> int:
    """실행별 상세 로그를 남기고 미처리 예외도 종료 코드 1로 기록한다."""
    directory = logs_directory()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        cleanup_old_logs(directory)
        started_at = datetime.now(KST)
        log_path = directory / started_at.strftime("news-report-%Y%m%d-%H%M%S.log")
        log_file = log_path.open("a", encoding="utf-8", buffering=1)
    except OSError as exc:
        print(f"[로그] 로그 파일을 준비할 수 없습니다: {exc}", file=sys.stderr)
        return 1

    original_stdout = sys.stdout
    original_stderr = sys.stderr
    sys.stdout = TimestampedTee(original_stdout, log_file, "INFO")
    sys.stderr = TimestampedTee(original_stderr, log_file, "ERROR")
    result = 1
    try:
        print(f"[로그] 실행 로그: {log_path}")
        print(f"[시작] 인자: {argv if argv is not None else sys.argv[1:]}")
        result = main(argv)
        print(f"[종료] 종료 코드: {result}")
    except BaseException:
        print("[예외] 처리되지 않은 예외가 발생했습니다.", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        result = 1
        print(f"[종료] 종료 코드: {result}", file=sys.stderr)
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        log_file.close()
    return result


if __name__ == "__main__":
    raise SystemExit(run_with_logging())
