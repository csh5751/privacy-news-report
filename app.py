"""키워드별 최신 뉴스를 수집하고 간단히 요약하는 콘솔 프로그램."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import ssl
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree


BING_NEWS_URL = "https://www.bing.com/news/search"
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


@dataclass(frozen=True)
class Article:
    """RSS에서 가져온 뉴스 한 건."""

    title: str
    link: str
    source: str
    published: datetime | None
    description: str


@dataclass(frozen=True)
class KakaoConfig:
    rest_api_key: str
    client_secret: str
    refresh_token: str
    link_url: str


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


def fetch_news(
    keyword: str, timeout: float = 10.0, max_results: int = 20
) -> list[Article]:
    """무료 Bing News 검색 피드에서 키워드에 해당하는 뉴스를 가져온다."""
    params = urlencode(
        {
            "q": keyword,
            "format": "rss",
            "setlang": "ko-kr",
            "cc": "KR",
        }
    )
    url = f"{BING_NEWS_URL}?{params}"
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
        root = ElementTree.fromstring(raw_data)
    except ElementTree.ParseError as exc:
        raise RuntimeError("뉴스 응답을 해석할 수 없습니다.") from exc

    articles: list[Article] = []
    for item in root.findall("./channel/item")[:max_results]:
        title = clean_text(item.findtext("title"))
        link = normalize_news_link((item.findtext("link") or "").strip())
        description = clean_text(item.findtext("description"))
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
                    description=description,
                )
            )
    return articles


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


def unique_articles(articles: Iterable[Article]) -> list[Article]:
    """같은 링크나 제목의 중복 기사를 제거한다."""
    result: list[Article] = []
    seen: set[str] = set()
    for article in articles:
        key = article.link or article.title.casefold()
        if key not in seen:
            seen.add(key)
            result.append(article)
    return result


def format_date(value: datetime | None) -> str:
    if value is None:
        return "시각 미상"
    korea_time = value.astimezone(timezone(timedelta(hours=9)))
    return korea_time.strftime("%Y-%m-%d %H:%M KST")


def display_topic(keyword: str, articles: list[Article], limit: int) -> None:
    print(f"\n{'=' * 72}\n주제: {keyword} ({min(len(articles), limit)}건)\n{'=' * 72}")
    if not articles:
        print("검색된 뉴스가 없습니다.")
        return

    for number, article in enumerate(articles[:limit], start=1):
        print(f"\n{number}. {article.title}")
        print(f"   출처: {article.source} | {format_date(article.published)}")
        print(f"   요약: {summarize(article)}")
        print(f"   링크: {article.link}")


def build_teams_message(topics: dict[str, list[Article]]) -> dict[str, str]:
    """모든 주제를 Teams 일반 메시지용 HTML 본문 하나로 만든다."""
    collected_at = datetime.now(timezone(timedelta(hours=9))).strftime(
        "%Y-%m-%d %H:%M KST"
    )
    lines = [
        "<h2>📰 개인정보 보호 · AI 보안 최신 뉴스</h2>",
        f"<p><em>수집 시각: {html.escape(collected_at)}</em></p>",
    ]
    for keyword, articles in topics.items():
        lines.extend(
            [
                "<hr>",
                f"<h3>{html.escape(keyword)} ({len(articles)}건)</h3>",
            ]
        )
        if not articles:
            lines.append("<p>검색된 뉴스가 없습니다.</p>")
            continue

        lines.append("<ol>")
        for article in articles:
            title = html.escape(article.title)
            link = html.escape(article.link, quote=True)
            source = html.escape(article.source)
            published = html.escape(format_date(article.published))
            summary = html.escape(summarize(article, max_length=140))
            lines.append(
                "<li>"
                f'<strong><a href="{link}">{title}</a></strong><br>'
                f"<small>{source} · {published}</small><br>"
                f"{summary}"
                "</li>"
            )
        lines.append("</ol>")

    return {"text": "".join(lines)}


def send_to_teams(
    webhook_url: str,
    topics: dict[str, list[Article]],
    timeout: float,
) -> None:
    """모든 키워드의 뉴스가 담긴 일반 메시지 하나를 Teams로 전송한다."""
    data = json.dumps(
        build_teams_message(topics), ensure_ascii=False
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
        link_url=get_user_setting("KAKAO_LINK_URL")
        or "https://developers.kakao.com",
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


def build_kakao_text(keyword: str, articles: list[Article]) -> str:
    """카카오 텍스트 템플릿의 200자 제한에 맞는 주제 요약을 만든다."""
    header = f"[{keyword}] 최신 뉴스\n"
    footer = datetime.now(timezone(timedelta(hours=9))).strftime("\n%Y-%m-%d")
    lines: list[str] = []
    if not articles:
        lines.append("검색된 뉴스가 없습니다.")
    else:
        for number, article in enumerate(articles, start=1):
            line = f"{number}. {article.title}"
            candidate = header + "\n".join(lines + [line]) + footer
            if len(candidate) > 200:
                break
            lines.append(line)
        if not lines:
            available = 200 - len(header) - len(footer) - 2
            lines.append(articles[0].title[:available] + "…")
    return (header + "\n".join(lines) + footer)[:200]


def send_topics_to_kakao(
    config: KakaoConfig,
    topics: dict[str, list[Article]],
    timeout: float,
) -> None:
    """액세스 토큰을 갱신하고 주제별 요약을 나와의 채팅으로 보낸다."""
    access_token = refresh_kakao_access_token(config, timeout)
    for keyword, articles in topics.items():
        template = {
            "object_type": "text",
            "text": build_kakao_text(keyword, articles),
            "link": {
                "web_url": config.link_url,
                "mobile_web_url": config.link_url,
            },
            "button_title": "자세히 보기",
        }
        result = post_kakao_form(
            KAKAO_MEMO_URL,
            {"template_object": json.dumps(template, ensure_ascii=False)},
            timeout,
            access_token=access_token,
        )
        if result.get("result_code") != 0:
            raise RuntimeError(f"카카오 메시지 전송 실패: {result}")
        time.sleep(0.2)


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
        default=5,
        help="주제별로 표시할 기사 수 (기본값: 5)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="뉴스 요청 제한 시간(초, 기본값: 10)",
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
        "--no-kakao",
        action="store_true",
        help="카카오 환경변수가 설정되어 있어도 카카오톡 전송을 생략합니다.",
    )
    return parser


def run_news_cycle(
    keywords: list[str],
    limit: int,
    timeout: float,
    webhook_url: str | None,
    kakao_config: KakaoConfig | None,
) -> int:
    """뉴스 수집과 콘솔 출력 및 Teams 전송을 한 차례 수행한다."""
    print(f"{', '.join(keywords)} 주제의 최신 뉴스를 가져오는 중입니다...")
    failures = 0
    topics: dict[str, list[Article]] = {}
    for keyword in keywords:
        try:
            articles = unique_articles(
                fetch_news(
                    keyword,
                    timeout=timeout,
                    max_results=max(limit * 3, 10),
                )
            )
            articles.sort(
                key=lambda item: item.published or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            selected = articles[:limit]
            topics[keyword] = selected
            display_topic(keyword, selected, limit)
        except RuntimeError as exc:
            failures += 1
            print(f"\n[주제: {keyword}] 처리 실패: {exc}", file=sys.stderr)

    if webhook_url and topics:
        try:
            send_to_teams(webhook_url, topics, timeout)
            print(f"[Teams] {len(topics)}개 주제를 본문 하나로 전송 완료")
        except RuntimeError as exc:
            print(f"\n[Teams] 전송 실패: {exc}", file=sys.stderr)
            return 1

    if kakao_config and topics:
        try:
            send_topics_to_kakao(kakao_config, topics, timeout)
            print(f"[KakaoTalk] {len(topics)}개 주제 전송 완료")
        except RuntimeError as exc:
            print(f"\n[KakaoTalk] 전송 실패: {exc}", file=sys.stderr)
            return 1

    return 1 if failures == len(keywords) else 0


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

    keywords = parse_keywords(args.keywords) or DEFAULT_KEYWORDS.copy()
    webhook_url = None if args.no_teams else get_teams_webhook_url()
    if not webhook_url and not args.no_teams:
        print(
            "오류: TEAMS_WEBHOOK_URL이 설정되어 있지 않습니다. "
            "Teams 전송 없이 실행하려면 --no-teams를 사용하세요.",
            file=sys.stderr,
        )
        return 2
    try:
        kakao_config = None if args.no_kakao else get_kakao_config()
    except RuntimeError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    if not kakao_config and not args.no_kakao:
        print(
            "안내: 카카오 환경변수가 없어 카카오톡 전송을 생략합니다.",
            file=sys.stderr,
        )

    while True:
        result = run_news_cycle(
            keywords,
            args.limit,
            args.timeout,
            webhook_url,
            kakao_config,
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


if __name__ == "__main__":
    raise SystemExit(main())
