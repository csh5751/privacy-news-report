"""뉴스를 수집해 GitHub Pages용 단일 HTML 보고서를 생성한다."""

from __future__ import annotations

import argparse
import html
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import app


KST = timezone(timedelta(hours=9))


def article_html(article: app.Article) -> str:
    title = html.escape(article.title)
    link = html.escape(article.link, quote=True)
    source = html.escape(article.source)
    published = html.escape(app.format_date(article.published))
    summary = html.escape(app.summarize(article, max_length=240))
    return f"""
      <article class="article">
        <h3><a href="{link}" target="_blank" rel="noopener noreferrer">{title}</a></h3>
        <p class="meta">{source}<span aria-hidden="true"> · </span>{published}</p>
        <p class="summary">{summary}</p>
        <a class="article-link" href="{link}" target="_blank" rel="noopener noreferrer">원문 보기 <span aria-hidden="true">→</span></a>
      </article>"""


def topic_html(keyword: str, articles: list[app.Article]) -> str:
    keyword_text = html.escape(keyword)
    if articles:
        contents = "".join(article_html(article) for article in articles)
    else:
        contents = '<p class="empty">검색된 뉴스가 없습니다.</p>'
    return f"""
    <details class="topic" open>
      <summary>
        <span>{keyword_text}</span>
        <span class="count">{len(articles)}건</span>
      </summary>
      <div class="articles">{contents}
      </div>
    </details>"""


def build_page(topics: dict[str, list[app.Article]], generated_at: datetime) -> str:
    total = sum(len(articles) for articles in topics.values())
    sections = "".join(topic_html(keyword, articles) for keyword, articles in topics.items())
    updated = html.escape(generated_at.astimezone(KST).strftime("%Y년 %m월 %d일 %H:%M KST"))
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="개인정보 보호와 AI 보안 관련 최신 뉴스 모음">
  <meta name="color-scheme" content="light dark">
  <title>개인정보 보호 · AI 보안 뉴스</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f4f7fb; --surface: #ffffff; --text: #172033; --muted: #64748b;
      --line: #dce4ef; --accent: #2563eb; --accent-soft: #eaf1ff;
      --shadow: 0 10px 28px rgba(30, 64, 175, .08);
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--text); font-family: Pretendard, "Noto Sans KR", system-ui, sans-serif; line-height: 1.65; }}
    .wrap {{ width: min(1120px, calc(100% - 32px)); margin: 0 auto; }}
    header {{ padding: 64px 0 38px; background: linear-gradient(135deg, #172554, #1d4ed8 65%, #0ea5e9); color: white; }}
    header h1 {{ margin: 0 0 12px; font-size: clamp(1.8rem, 5vw, 3rem); line-height: 1.2; letter-spacing: -.04em; }}
    header p {{ margin: 0; color: #dbeafe; }}
    .stats {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 22px; }}
    .badge {{ padding: 7px 12px; border: 1px solid rgba(255,255,255,.25); border-radius: 999px; background: rgba(255,255,255,.12); font-size: .9rem; }}
    main {{ padding: 28px 0 56px; }}
    .topic {{ margin-bottom: 18px; overflow: hidden; border: 1px solid var(--line); border-radius: 18px; background: var(--surface); box-shadow: var(--shadow); }}
    .topic > summary {{ display: flex; justify-content: space-between; align-items: center; gap: 16px; padding: 19px 22px; cursor: pointer; color: var(--accent); font-size: 1.18rem; font-weight: 800; list-style: none; }}
    .topic > summary::-webkit-details-marker {{ display: none; }}
    .topic > summary::before {{ content: "▸"; transition: transform .15s ease; }}
    .topic[open] > summary::before {{ transform: rotate(90deg); }}
    .topic > summary > span:first-of-type {{ margin-right: auto; }}
    .count {{ padding: 3px 9px; border-radius: 999px; background: var(--accent-soft); font-size: .78rem; }}
    .articles {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); border-top: 1px solid var(--line); }}
    .article {{ min-width: 0; padding: 22px; border-bottom: 1px solid var(--line); }}
    .article:nth-child(odd) {{ border-right: 1px solid var(--line); }}
    .article h3 {{ margin: 0 0 8px; font-size: 1.04rem; line-height: 1.45; }}
    .article h3 a {{ color: var(--text); text-decoration: none; }}
    .article h3 a:hover, .article-link:hover {{ color: var(--accent); text-decoration: underline; }}
    .meta {{ margin: 0 0 10px; color: var(--muted); font-size: .82rem; }}
    .summary {{ margin: 0 0 13px; color: var(--text); font-size: .92rem; }}
    .article-link {{ color: var(--accent); font-size: .86rem; font-weight: 700; text-decoration: none; }}
    .empty {{ padding: 24px; color: var(--muted); }}
    footer {{ padding: 24px 0 42px; color: var(--muted); text-align: center; font-size: .82rem; }}
    @media (max-width: 720px) {{
      header {{ padding-top: 44px; }} .wrap {{ width: min(100% - 20px, 1120px); }}
      .articles {{ grid-template-columns: 1fr; }} .article:nth-child(odd) {{ border-right: 0; }}
      .topic > summary, .article {{ padding: 17px; }}
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{ --bg: #0b1220; --surface: #111b2e; --text: #e5edf8; --muted: #94a3b8; --line: #24324a; --accent: #7db2ff; --accent-soft: #172b4d; --shadow: none; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="wrap">
      <h1>개인정보 보호 · AI 보안 뉴스</h1>
      <p>주요 개인정보 보호 정책과 보안 이슈를 한곳에서 확인하세요.</p>
      <div class="stats">
        <span class="badge">주제 {len(topics)}개</span>
        <span class="badge">기사 {total}건</span>
        <span class="badge">업데이트 {updated}</span>
      </div>
    </div>
  </header>
  <main class="wrap">{sections}
  </main>
  <footer class="wrap">공개 뉴스 검색 결과를 자동으로 수집한 요약입니다. 정확한 내용은 원문을 확인하세요.</footer>
</body>
</html>
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GitHub Pages용 뉴스 보고서를 생성합니다.")
    parser.add_argument("keywords", nargs="*", help="검색 키워드. 생략하면 app.py 기본값 사용")
    parser.add_argument("-n", "--limit", type=int, default=5, help="주제별 기사 수")
    parser.add_argument("--timeout", type=float, default=20.0, help="요청 제한 시간(초)")
    parser.add_argument("--output", default="site/index.html", help="생성할 HTML 경로")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.limit < 1 or args.timeout <= 0:
        print("오류: limit는 1 이상, timeout은 0보다 커야 합니다.", file=sys.stderr)
        return 2

    keywords = app.parse_keywords(args.keywords) or app.DEFAULT_KEYWORDS.copy()
    topics: dict[str, list[app.Article]] = {}
    claimed_keys: set[str] = set()
    claimed_articles: list[app.Article] = []
    failures = 0
    for keyword in keywords:
        print(f"[수집] {keyword}")
        try:
            articles = app.unique_articles(
                app.fetch_news(keyword, timeout=args.timeout, max_results=max(args.limit * 3, 10))
            )
            topics[keyword] = app.select_today_articles(
                articles,
                args.limit,
                claimed_keys=claimed_keys,
                claimed_articles=claimed_articles,
            )
        except RuntimeError as exc:
            failures += 1
            topics[keyword] = []
            print(f"[경고] {keyword}: {exc}", file=sys.stderr)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(build_page(topics, datetime.now(timezone.utc)), encoding="utf-8")
    print(f"[완료] {output.resolve()} ({sum(map(len, topics.values()))}건)")
    return 1 if failures == len(keywords) else 0


if __name__ == "__main__":
    raise SystemExit(main())
