import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import app


GOOGLE_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title>개인정보위, A사에 과징금 12억원 부과 - 연합뉴스</title>
    <link>https://news.google.com/rss/articles/ABC?oc=5</link>
    <pubDate>Wed, 02 Sep 2026 04:36:39 GMT</pubDate>
    <description>&lt;a href="https://news.google.com/x"&gt;제목&lt;/a&gt;</description>
    <source url="https://www.yna.co.kr">연합뉴스</source>
  </item>
  <item>
    <title>제목만 있는 기사</title>
    <link>https://news.google.com/rss/articles/DEF?oc=5</link>
    <pubDate>Wed, 02 Sep 2026 03:00:00 GMT</pubDate>
    <source url="https://example.com">테스트신문</source>
  </item>
</channel></rss>
"""


def article(
    title: str,
    link: str,
    published: datetime,
    source: str = "테스트",
    description: str = "",
    keywords: tuple[str, ...] = (),
) -> app.Article:
    return app.Article(title, link, source, published, description, keywords)


class FeedParsingTests(unittest.TestCase):
    def test_google_feed_strips_source_suffix_and_reads_source_tag(self):
        original = app.read_feed
        app.read_feed = lambda url, timeout: __import__(
            "xml.etree.ElementTree", fromlist=["ElementTree"]
        ).fromstring(GOOGLE_FEED)
        try:
            articles = app.fetch_google_news("개인정보", timeout=1)
        finally:
            app.read_feed = original

        self.assertEqual(2, len(articles))
        self.assertEqual("개인정보위, A사에 과징금 12억원 부과", articles[0].title)
        self.assertEqual("연합뉴스", articles[0].source)
        # Google의 description은 앵커뿐이라 요약 재료로 쓰지 않는다.
        self.assertEqual("", articles[0].description)

    def test_strip_source_suffix_keeps_titles_with_internal_dashes(self):
        self.assertEqual(
            "개인정보위, A사에 과징금",
            app.strip_source_suffix("개인정보위, A사에 과징금 - 연합뉴스", "연합뉴스"),
        )
        self.assertEqual("단독", app.strip_source_suffix("단독", "연합뉴스"))

    def test_fetch_news_survives_a_single_source_failure(self):
        good = [article("살아남은 기사", "https://a.test/1", datetime.now(timezone.utc))]
        original_google, original_bing = app.fetch_google_news, app.fetch_bing_news
        app.fetch_google_news = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("구글 실패")
        )
        app.fetch_bing_news = lambda *a, **k: good
        try:
            collected = app.fetch_news("개인정보", timeout=1)
        finally:
            app.fetch_google_news, app.fetch_bing_news = original_google, original_bing

        self.assertEqual(1, len(collected))
        self.assertEqual("살아남은 기사", collected[0].title)
        # 어느 키워드가 찾아낸 기사인지 기록해 구획을 나누는 근거로 쓴다.
        self.assertEqual(("개인정보",), collected[0].keywords)

    def test_fetch_news_raises_only_when_every_source_fails(self):
        def boom(*args, **kwargs):
            raise RuntimeError("실패")

        original_google, original_bing = app.fetch_google_news, app.fetch_bing_news
        app.fetch_google_news = boom
        app.fetch_bing_news = boom
        try:
            with self.assertRaisesRegex(RuntimeError, "모든 뉴스 소스가 실패"):
                app.fetch_news("개인정보", timeout=1)
        finally:
            app.fetch_google_news, app.fetch_bing_news = original_google, original_bing


class CandidateSelectionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 2, 1, 0, tzinfo=timezone.utc)

    def test_window_keeps_yesterday_evening_and_drops_older(self):
        # 조간 실행 시 전날 저녁 기사를 놓치지 않아야 한다.
        last_evening = article(
            "전날 저녁", "https://a.test/1", self.now - timedelta(hours=8)
        )
        stale = article("오래된 기사", "https://a.test/2", self.now - timedelta(days=4))

        selected = app.select_recent_articles(
            [stale, last_evening], window_hours=30, now=self.now
        )

        self.assertEqual([last_evening], selected)

    def test_undated_and_future_articles_are_dropped(self):
        undated = app.Article("시각 미상", "https://a.test/1", "테스트", None, "")
        future = article("미래 기사", "https://a.test/2", self.now + timedelta(hours=3))

        self.assertEqual(
            [], app.select_recent_articles([undated, future], now=self.now)
        )

    def test_sent_history_excludes_previously_reported_article(self):
        reported = article("전송 완료", "https://a.test/1", self.now)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            app.save_sent_history({}, app.article_keys(reported), path)
            history = app.load_sent_history(path)

            selected = app.select_recent_articles(
                [reported], excluded_keys=set(history), now=self.now
            )

        self.assertEqual([], selected)

    def test_duplicate_titles_merge_and_keep_the_described_copy(self):
        # Google은 설명을 주지 않으므로 같은 제목이면 Bing 쪽을 남겨야 한다.
        bare = article("같은 제목", "https://news.google.com/rss/articles/X", self.now)
        described = article(
            "같은 제목",
            "https://www.yna.co.kr/view/1",
            self.now,
            "연합뉴스",
            "과징금 12억원이 부과됐다.",
        )

        merged = app.merge_duplicate_articles([bare, described])

        self.assertEqual([described], merged)

    def test_stale_copy_does_not_swallow_the_fresh_one(self):
        # 같은 제목이라도 창 밖 사본이 대표로 뽑혀 기사가 사라지면 안 된다.
        stale_but_detailed = article(
            "같은 제목",
            "https://www.yna.co.kr/view/1",
            self.now - timedelta(days=4),
            "연합뉴스",
            "긴 설명이 붙어 있는 오래된 사본입니다.",
        )
        fresh = article(
            "같은 제목", "https://news.google.com/rss/articles/X", self.now
        )

        selected = app.select_recent_articles(
            [stale_but_detailed, fresh], window_hours=30, now=self.now
        )

        self.assertEqual([fresh], selected)

    def test_same_story_ignores_date_tokens(self):
        left = article("19일 개인정보위 전체회의", "https://a.test/1", self.now)
        right = article("18일 방통위 상임위원 간담회", "https://a.test/2", self.now)

        # 날짜 표기만 다른 두 기사가 유사도로 묶이면 안 된다.
        self.assertFalse(app.same_story(left, right))


class RelevanceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 3, 1, 0, tzinfo=timezone.utc)

    def test_promotional_articles_score_below_real_incidents(self):
        incident = article(
            "티빙, 개인정보 유출 공식 사과…피해 보상안 발표", "https://a.test/1", self.now
        )
        promo = article(
            "파수 AI, 가상화 방식의 소스코드 보안 솔루션 출시", "https://a.test/2", self.now
        )
        event = article(
            "기업 AI ROI, 해법 찾는다…'NABS 2026' 개최", "https://a.test/3", self.now
        )

        self.assertGreaterEqual(app.relevance_score(incident), 1)
        self.assertLess(app.relevance_score(promo), 1)
        self.assertLess(app.relevance_score(event), 1)

    def test_blog_and_cafe_posts_are_dropped(self):
        blog = article(
            "티빙, 쿠팡 과징금 유탄 맞나 : 네이버 블로그",
            "https://blog.naver.com/x",
            self.now,
            "네이버 블로그",
        )
        news = article(
            "개인정보위, 쿠팡에 과징금 부과", "https://www.yna.co.kr/1", self.now, "연합뉴스"
        )

        self.assertTrue(app.is_user_generated(blog))
        self.assertFalse(app.is_user_generated(news))
        self.assertEqual([news], app.filter_relevant([blog, news]))

    def test_filter_is_skipped_when_it_would_empty_the_pool(self):
        # 신호어가 현실을 못 따라가도 후보를 통째로 날리면 안 된다.
        unmatched = [
            article(f"홍보성 제목 {index} 출시", f"https://a.test/{index}", self.now)
            for index in range(10)
        ]

        self.assertEqual(unmatched, app.filter_relevant(unmatched))


class ClusteringTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 3, 1, 0, tzinfo=timezone.utc)

    def variants(self) -> list[app.Article]:
        titles = [
            "네이버, 독자 '사이버보안 AI 모델' 개발…정부 사업 선정",
            "정부 보안특화 AI 모델 개발 사업자로 네이버 선정",
            "네이버클라우드, 과기정통부 사이버보안 AI 개발 사업 선정",
            "네이버클라우드, SKT 제치고 '보안 특화 AI' 개발 사업자 선정",
        ]
        return [
            article(title, f"https://a.test/{index}", self.now)
            for index, title in enumerate(titles)
        ]

    def test_one_event_reported_by_many_outlets_forms_a_single_cluster(self):
        clusters = app.cluster_stories(self.variants())

        self.assertEqual(1, len(clusters))
        self.assertEqual(4, len(clusters[0]))

    def test_common_words_alone_do_not_merge_distinct_events(self):
        # "보안"과 "AI"만 겹치는 서로 다른 사건은 묶이지 않아야 한다.
        left = article("보안 AI 모델 개발 사업자 선정", "https://a.test/1", self.now)
        right = article("보안 AI 시장 규모 전망 발표", "https://a.test/2", self.now)
        filler = [
            article(f"보안 AI 관련 소식 {i}", f"https://a.test/f{i}", self.now)
            for i in range(8)
        ]

        weights = app.token_weights([left, right, *filler])

        self.assertFalse(app.same_story(left, right, weights))

    def test_coverage_count_drives_heuristic_importance(self):
        self.assertEqual(5, app.coverage_importance(15))
        self.assertEqual(4, app.coverage_importance(5))
        self.assertEqual(3, app.coverage_importance(3))
        self.assertEqual(1, app.coverage_importance(1))

    def test_widely_covered_event_outranks_a_newer_single_report(self):
        widely_covered = self.variants()
        lone_but_newer = article(
            "단독 보도 한 건", "https://a.test/9", self.now + timedelta(hours=2)
        )

        stories = app.curate_heuristically([*widely_covered, lone_but_newer], limit=5)

        self.assertEqual(4, len(stories[0].articles))
        self.assertGreater(stories[0].importance, stories[-1].importance)


class CurationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 2, 1, 0, tzinfo=timezone.utc)
        self.keywords = ["개인정보보호법", "AI 보안"]
        self.candidates = [
            article(
                "A사 과징금 12억",
                "https://a.test/1",
                self.now,
                "연합뉴스",
                keywords=("개인정보보호법",),
            ),
            article(
                "A사에 과징금 부과",
                "https://a.test/2",
                self.now,
                "이투데이",
                keywords=("개인정보보호법",),
            ),
            article(
                "공모전 개최",
                "https://a.test/3",
                self.now,
                "전자신문",
                keywords=("AI 보안",),
            ),
        ]

    def payload(self):
        return {
            "stories": [
                {
                    "headline": "개인정보위, A사에 과징금 12억원 부과",
                    "summary": "개인정보위가 A사에 과징금을 부과했다.",
                    "importance": 5,
                    "representative": 0,
                    "related": [1],
                },
                {
                    "headline": "공모전 개최",
                    "summary": "공모전이 열린다.",
                    "importance": 1,
                    "representative": 2,
                    "related": [],
                },
            ]
        }

    def test_build_stories_groups_related_articles(self):
        stories = app.build_stories(self.payload(), self.candidates, limit=12)

        self.assertEqual(2, len(stories))
        self.assertEqual(5, stories[0].importance)
        self.assertEqual(self.candidates[0], stories[0].representative)
        self.assertEqual((self.candidates[1],), stories[0].related)
        # 중요도 내림차순으로 정렬된다.
        self.assertEqual([5, 1], [story.importance for story in stories])

    def test_build_stories_drops_out_of_range_and_reused_indexes(self):
        payload = {
            "stories": [
                {
                    "headline": "첫 사건",
                    "summary": "",
                    "importance": 4,
                    "representative": 0,
                    "related": [99, 1],
                },
                {
                    "headline": "같은 기사를 재사용",
                    "summary": "",
                    "importance": 4,
                    "representative": 1,
                    "related": [],
                },
                {
                    "headline": "없는 기사",
                    "summary": "",
                    "importance": 4,
                    "representative": 42,
                    "related": [],
                },
            ]
        }

        stories = app.build_stories(payload, self.candidates, limit=12)

        self.assertEqual(1, len(stories))
        self.assertEqual((self.candidates[1],), stories[0].related)

    def test_build_stories_honours_limit(self):
        stories = app.build_stories(self.payload(), self.candidates, limit=1)

        self.assertEqual(1, len(stories))
        self.assertEqual(5, stories[0].importance)

    def test_build_stories_rejects_payload_without_usable_articles(self):
        with self.assertRaises(RuntimeError):
            app.build_stories({"stories": []}, self.candidates, limit=12)

    def test_build_stories_rejects_payload_missing_stories(self):
        with self.assertRaisesRegex(RuntimeError, "stories"):
            app.build_stories({"sections": []}, self.candidates, limit=12)

    def test_curate_falls_back_to_heuristics_when_claude_fails(self):
        original = app.curate_with_claude

        def boom(*args, **kwargs):
            raise RuntimeError("Claude API 500 오류")

        app.curate_with_claude = boom
        try:
            stories, curator = app.curate(
                self.candidates, limit=12, api_key="test-key"
            )
        finally:
            app.curate_with_claude = original

        self.assertTrue(stories)
        # 폴백으로 정리했으면 안내에 모델이 처리했다고 적히지 않아야 한다.
        self.assertEqual(app.HEURISTIC_CURATOR, curator)

    def test_curate_reports_the_model_when_it_succeeds(self):
        original = app.curate_with_claude
        expected = app.build_stories(self.payload(), self.candidates, limit=12)
        app.curate_with_claude = lambda *args, **kwargs: expected
        try:
            stories, curator = app.curate(
                self.candidates, limit=12, api_key="test-key"
            )
        finally:
            app.curate_with_claude = original

        self.assertEqual(expected, stories)
        self.assertEqual(app.CURATION_MODEL, curator)

    def test_curate_uses_heuristics_without_an_api_key(self):
        stories, curator = app.curate(self.candidates, limit=12, api_key=None)

        self.assertTrue(stories)
        self.assertEqual(app.HEURISTIC_CURATOR, curator)
        # 유사한 제목의 두 기사는 한 사건으로 묶이고 신뢰도 높은 쪽이 대표가 된다.
        merged = next(story for story in stories if story.related)
        self.assertEqual("연합뉴스", merged.representative.source)

    def test_curate_returns_nothing_for_an_empty_pool(self):
        stories, curator = app.curate([], limit=12, api_key="test-key")

        self.assertEqual([], stories)
        self.assertEqual(app.HEURISTIC_CURATOR, curator)

    def test_group_sections_keeps_empty_keywords_at_the_end(self):
        stories = app.build_stories(self.payload(), self.candidates, limit=12)

        sections = app.group_sections(stories, ["개인정보보호법", "AI 보안", "개보위"])

        self.assertEqual(1, len(sections["개인정보보호법"]))
        self.assertEqual(1, len(sections["AI 보안"]))
        # 결과가 없는 키워드도 구획으로 남지만 뒤로 밀린다.
        self.assertEqual([], sections["개보위"])
        self.assertEqual("개보위", list(sections)[-1])
        self.assertEqual("개인정보보호법", sections["개인정보보호법"][0].section)

    def test_hotter_section_comes_first_regardless_of_keyword_order(self):
        cool = article("작은 사건", "https://a.test/1", self.now, keywords=("앞 키워드",))
        hot = article("큰 사건", "https://a.test/2", self.now, keywords=("뒤 키워드",))
        payload = {
            "stories": [
                {
                    "headline": "작은 사건",
                    "summary": "",
                    "importance": 2,
                    "representative": 0,
                    "related": [],
                },
                {
                    "headline": "큰 사건",
                    "summary": "",
                    "importance": 5,
                    "representative": 1,
                    "related": [],
                },
            ]
        }
        stories = app.build_stories(payload, [cool, hot], limit=12)

        sections = app.group_sections(stories, ["앞 키워드", "뒤 키워드"])

        # 키워드 순서보다 화제성이 앞선다.
        self.assertEqual(["뒤 키워드", "앞 키워드"], list(sections))

    def test_a_story_matching_two_keywords_lands_in_one_section_only(self):
        # 개인정보보호위원회와 개보위는 같은 기관이라 한 기사가 둘 다에 걸린다.
        overlapping = article(
            "개인정보위 전체회의 의결",
            "https://a.test/9",
            self.now,
            "연합뉴스",
            keywords=("개보위", "개인정보보호위원회"),
        )
        payload = {
            "stories": [
                {
                    "headline": "개인정보위 전체회의 의결",
                    "summary": "의결했다.",
                    "importance": 4,
                    "representative": 0,
                    "related": [],
                }
            ]
        }
        stories = app.build_stories(payload, [overlapping], limit=12)

        sections = app.group_sections(stories, ["개인정보보호위원회", "개보위"])

        # 앞선 키워드의 구획에만 들어가고 중복 노출되지 않는다.
        self.assertEqual(1, len(sections["개인정보보호위원회"]))
        self.assertEqual([], sections["개보위"])

    def test_stories_without_a_known_keyword_go_to_the_other_section(self):
        orphan = article("출처 불명 사건", "https://a.test/8", self.now)
        payload = {
            "stories": [
                {
                    "headline": "출처 불명 사건",
                    "summary": "",
                    "importance": 3,
                    "representative": 0,
                    "related": [],
                }
            ]
        }
        stories = app.build_stories(payload, [orphan], limit=12)

        sections = app.group_sections(stories, ["개인정보보호법"])

        self.assertEqual([], sections["개인정보보호법"])
        self.assertEqual(1, len(sections[app.OTHER_SECTION]))

    def test_merge_keeps_every_keyword_that_found_the_article(self):
        first = article(
            "같은 제목", "https://a.test/1", self.now, keywords=("개보위",)
        )
        second = article(
            "같은 제목",
            "https://b.test/1",
            self.now,
            "연합뉴스",
            "설명이 있는 사본",
            keywords=("개인정보보호법",),
        )

        merged = app.merge_duplicate_articles([first, second])

        self.assertEqual(1, len(merged))
        self.assertEqual("설명이 있는 사본", merged[0].description)
        self.assertEqual({"개보위", "개인정보보호법"}, set(merged[0].keywords))


class OutputTests(unittest.TestCase):
    def setUp(self):
        now = datetime(2026, 9, 2, 1, 0, tzinfo=timezone.utc)
        self.story = app.Story(
            section="제재·과징금",
            headline="개인정보위, A사에 과징금 12억원 부과",
            summary="개인정보위가 A사에 과징금을 부과했다.",
            importance=5,
            representative=article(
                "A사 과징금 12억", "https://a.test/1", now, "연합뉴스"
            ),
            related=(article("A사 과징금", "https://a.test/2", now, "이투데이"),),
        )

    def test_teams_message_carries_headline_summary_and_related_sources(self):
        text = app.build_teams_message({"제재·과징금": [self.story]})["text"]

        self.assertIn("제재·과징금 (1건)", text)
        self.assertIn("개인정보위, A사에 과징금 12억원 부과", text)
        self.assertIn("개인정보위가 A사에 과징금을 부과했다.", text)
        self.assertIn("중요도 5", text)
        self.assertIn("이투데이", text)
        self.assertIn('href="https://a.test/1"', text)

    def test_teams_message_reports_an_empty_run(self):
        text = app.build_teams_message({})["text"]

        self.assertIn("보고할 새 뉴스가 없습니다.", text)

    def test_story_summary_falls_back_to_the_article_description(self):
        now = datetime(2026, 9, 2, 1, 0, tzinfo=timezone.utc)
        story = app.Story(
            section="구획",
            headline="제목",
            summary="",
            importance=3,
            representative=article(
                "제목", "https://a.test/1", now, "연합뉴스", "본문 설명입니다."
            ),
        )

        self.assertIn("본문 설명입니다.", app.story_summary(story))

    def test_kakao_text_counts_sections_and_stories(self):
        text = app.build_kakao_text({"제재·과징금": [self.story]})

        self.assertIn("1개 주제", text)
        self.assertIn("새 사건 1건", text)
        self.assertLessEqual(len(text), 200)


class HeatOrderTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 3, 1, 0, tzinfo=timezone.utc)

    def story(self, importance: int, outlets: int, minutes_ago: int = 0) -> app.Story:
        published = self.now - timedelta(minutes=minutes_ago)
        articles = [
            article(f"기사 {importance}-{index}", f"https://a.test/{importance}{index}", published)
            for index in range(outlets)
        ]
        return app.Story(
            section="",
            headline=f"중요도{importance} 보도{outlets}",
            summary="",
            importance=importance,
            representative=articles[0],
            related=tuple(articles[1:]),
        )

    def test_importance_leads_the_ranking(self):
        low = self.story(3, 4)
        high = self.story(5, 4)

        self.assertGreater(app.story_heat(high), app.story_heat(low))

    def test_wide_coverage_can_outrank_one_importance_step(self):
        widely_covered = self.story(4, 20)
        lone_report = self.story(5, 1)

        # 20개 매체가 다룬 사건이 단독 보도된 한 단계 위 사건보다 화제성이 높다.
        self.assertGreater(app.story_heat(widely_covered), app.story_heat(lone_report))

    def test_coverage_cannot_outrank_two_importance_steps(self):
        widely_covered = self.story(3, 20)
        important = self.story(5, 1)

        self.assertGreater(app.story_heat(important), app.story_heat(widely_covered))

    def test_relay_only_story_yields_to_an_equal_original(self):
        original = self.story(4, 3)
        relay = app.Story(
            section="",
            headline="재배포본만 있는 단독",
            summary="",
            importance=4,
            representative=article(
                "단독 기사", "https://news.nate.com/view/1", self.now, "네이트"
            ),
            related=tuple(
                article(f"r{i}", f"https://news.nate.com/view/{i}", self.now, "네이트")
                for i in range(1, 3)
            ),
        )

        self.assertGreater(app.story_heat(original), app.story_heat(relay))
        # 다만 한 단계 위 사건을 밀어낼 정도로 깎지는 않는다.
        self.assertGreater(app.story_heat(relay), app.story_heat(self.story(3, 3)))

    def test_newer_story_wins_a_tie(self):
        older = self.story(4, 5, minutes_ago=120)
        newer = self.story(4, 5, minutes_ago=5)

        self.assertGreater(app.story_heat(newer), app.story_heat(older))


class AggregatorTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 3, 1, 0, tzinfo=timezone.utc)

    def test_redistributors_are_detected_by_source_name(self):
        # Google 피드는 링크가 재배포 매체인지 알려주지 않고 출처명만 준다.
        by_name = article("기사", "https://news.google.com/rss/articles/X", self.now, "네이트")
        by_host = article("기사", "https://v.daum.net/v/1", self.now, "어떤신문")
        original = article("기사", "https://www.yna.co.kr/view/1", self.now, "연합뉴스")

        self.assertTrue(app.is_aggregator(by_name))
        self.assertTrue(app.is_aggregator(by_host))
        self.assertFalse(app.is_aggregator(original))

    def test_representative_swaps_to_the_original_outlet(self):
        relay = article("기사", "https://news.google.com/rss/articles/X", self.now, "네이트")
        original = article("기사", "https://www.yna.co.kr/view/1", self.now, "연합뉴스")
        story = app.Story("", "제목", "", 4, relay, (original,))

        fixed = app.prefer_original_outlet(story)

        self.assertEqual("연합뉴스", fixed.representative.source)
        self.assertEqual((relay,), fixed.related)
        # 기사가 사라지거나 늘어나지 않는다.
        self.assertEqual(set(story.articles), set(fixed.articles))

    def test_representative_is_kept_when_only_redistributors_exist(self):
        relay = article("기사", "https://www.msn.com/x", self.now, "이투데이 on MSN")
        other = article("기사2", "https://news.nate.com/view/1", self.now, "네이트")
        story = app.Story("", "제목", "", 4, relay, (other,))

        self.assertEqual(story, app.prefer_original_outlet(story))

    def test_curation_replaces_a_relayed_representative(self):
        relay = article("기사", "https://news.nate.com/view/1", self.now, "네이트")
        original = article("기사", "https://www.yna.co.kr/view/1", self.now, "연합뉴스")
        payload = {
            "stories": [
                {
                    "headline": "제목",
                    "summary": "",
                    "importance": 4,
                    "representative": 0,
                    "related": [1],
                }
            ]
        }

        stories = app.build_stories(payload, [relay, original], limit=12)

        self.assertEqual("연합뉴스", stories[0].representative.source)


class RevisitedTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 3, 1, 0, tzinfo=timezone.utc)
        self.new_story = app.Story(
            section="개인정보 유출 사고",
            headline="새 사건",
            summary="처음 안내하는 사건.",
            importance=5,
            representative=article("새 사건", "https://a.test/new", self.now, "한국일보"),
        )
        self.old_story = app.Story(
            section="",
            headline="이미 안내한 사건",
            summary="",
            importance=4,
            representative=article("이미 안내", "https://a.test/old", self.now, "연합뉴스"),
            related=(article("이미 안내 2", "https://a.test/old2", self.now, "중앙일보"),),
        )

    def test_partition_keeps_the_already_reported_articles(self):
        fresh = article("새 기사", "https://a.test/1", self.now)
        seen = article("보낸 기사", "https://a.test/2", self.now)
        excluded = app.article_keys(seen)

        unsent, reported = app.partition_recent_articles(
            [fresh, seen], excluded_keys=excluded, now=self.now
        )

        self.assertEqual([fresh], unsent)
        self.assertEqual([seen], reported)

    def test_partition_still_drops_articles_outside_the_window(self):
        stale = article("오래된 기사", "https://a.test/1", self.now - timedelta(days=4))

        unsent, reported = app.partition_recent_articles([stale], now=self.now)

        self.assertEqual([], unsent)
        self.assertEqual([], reported)

    def test_revisited_block_lists_titles_after_the_new_stories(self):
        stats = app.RunStats(
            collected=300, already_sent=3, stories=1, curator=app.CURATION_MODEL
        )

        text = app.build_teams_message(
            {"개인정보 유출 사고": [self.new_story]},
            stats,
            revisited=(self.old_story,),
        )["text"]

        self.assertIn("이미 안내한 사건 (1건)", text)
        self.assertIn("보도 2건", text)
        self.assertIn('href="https://a.test/old"', text)
        # 새 사건이 먼저, 이미 안내한 목록이 뒤에 온다.
        self.assertLess(text.index("새 사건"), text.index("이미 안내한 사건"))

    def test_revisited_block_reports_hidden_events_not_articles(self):
        stats = app.RunStats(
            collected=300, already_sent=90, stories=1, curator=app.CURATION_MODEL
        )

        text = app.build_teams_message(
            {"구획": [self.new_story]},
            stats,
            revisited=(self.old_story,),
            revisited_hidden=12,
        )["text"]

        # 기사 수가 아니라 못 실은 사건 수를 알린다.
        self.assertIn("사건 12건은 전체 보고서에서", text)
        self.assertNotIn("90건은 전체 보고서에서", text)

    def test_revisited_block_is_absent_when_nothing_was_hidden(self):
        text = app.build_teams_message({"구획": [self.new_story]})["text"]

        self.assertNotIn("이미 안내한 사건", text)

    def test_revisited_block_shows_even_when_there_is_no_new_story(self):
        stats = app.RunStats(
            collected=300, already_sent=2, stories=0, curator=app.CURATION_MODEL
        )

        text = app.build_teams_message({}, stats, revisited=(self.old_story,))["text"]

        self.assertIn("보고할 새 뉴스가 없습니다.", text)
        self.assertIn("이미 안내한 사건 (1건)", text)


class PageTests(unittest.TestCase):
    def setUp(self):
        import generate_site

        self.site = generate_site
        self.now = datetime(2026, 9, 3, 1, 0, tzinfo=timezone.utc)
        self.story = app.Story(
            section="개인정보 유출 사고",
            headline="티빙, 개인정보 유출 공식 사과",
            summary="사과했다.",
            importance=5,
            representative=article("티빙 사과", "https://a.test/new", self.now, "한국일보"),
        )
        self.old = app.Story(
            section="",
            headline="이미 안내한 사건",
            summary="",
            importance=4,
            representative=article("이미 안내", "https://a.test/old", self.now, "연합뉴스"),
            related=(article("이미 안내 2", "https://a.test/old2", self.now, "중앙일보"),),
        )

    def page(self, **kwargs) -> str:
        return self.site.build_page(
            {"개인정보 유출 사고": [self.story]}, self.now, **kwargs
        )

    def test_page_lists_already_reported_stories_last_and_collapsed(self):
        page = self.page(revisited=(self.old,), revisited_hidden=50)

        self.assertIn("이미 안내한 사건", page)
        self.assertIn("보도 2건", page)
        self.assertIn("이 밖에 사건 50건", page)
        # 새 사건 구획보다 뒤에 오고, 기본으로 접혀 있어야 한다.
        self.assertLess(page.index("개인정보 유출 사고"), page.index("이미 안내한 사건"))
        self.assertIn('class="topic seen"', page)
        self.assertNotIn('class="topic seen" open', page)

    def test_page_omits_the_section_when_nothing_was_held_back(self):
        self.assertNotIn("이미 안내한 사건", self.page())

    def test_page_omits_the_remainder_line_when_all_fit(self):
        page = self.page(revisited=(self.old,), revisited_hidden=0)

        self.assertIn("이미 안내한 사건", page)
        self.assertNotIn("이 밖에 사건", page)

    def test_page_escapes_titles_and_links(self):
        nasty = app.Story(
            section="",
            headline='<script>alert("x")</script>',
            summary="",
            importance=3,
            representative=article("t", 'https://a.test/?q="x"', self.now, "출처"),
        )

        page = self.page(revisited=(nasty,))

        self.assertNotIn("<script>alert", page)
        self.assertNotIn('href="https://a.test/?q="x""', page)


class WebhookRequirementTests(unittest.TestCase):
    def run_main(self, argv: list[str]) -> int:
        original = app.get_teams_webhook_url
        app.get_teams_webhook_url = lambda: None
        # 수집까지 가지 않도록 사이클을 막아 인자 검증만 확인한다.
        original_cycle = app.run_news_cycle
        app.run_news_cycle = lambda *a, **k: 0
        try:
            return app.main(argv)
        finally:
            app.get_teams_webhook_url = original
            app.run_news_cycle = original_cycle

    def test_missing_webhook_is_fatal_when_teams_is_the_only_output(self):
        self.assertEqual(2, self.run_main([]))

    def test_missing_webhook_only_warns_when_a_report_is_also_built(self):
        # 전송 설정이 없다고 페이지 배포까지 막으면 안 된다.
        self.assertEqual(0, self.run_main(["--site", "site/index.html"]))


class HistoryLocationTests(unittest.TestCase):
    def test_history_path_honours_the_override(self):
        import os

        original = os.environ.get("NEWS_HISTORY_FILE")
        os.environ["NEWS_HISTORY_FILE"] = "data/sent_articles.json"
        try:
            self.assertEqual(Path("data/sent_articles.json"), app.history_path())
            # 로그는 리포에 커밋할 것이 아니므로 이력 경로를 따라가지 않는다.
            self.assertNotIn("data", app.logs_directory().parts)
        finally:
            if original is None:
                del os.environ["NEWS_HISTORY_FILE"]
            else:
                os.environ["NEWS_HISTORY_FILE"] = original


class NoticeTests(unittest.TestCase):
    def stats(self, **overrides) -> app.RunStats:
        values = {
            "collected": 354,
            "fresh": 195,
            "already_sent": 24,
            "irrelevant": 39,
            "folded": 130,
            "stories": 12,
            "curator": app.CURATION_MODEL,
        }
        values.update(overrides)
        return app.RunStats(**values)

    def test_notice_reports_what_was_left_out_and_who_curated(self):
        lines = app.notice_lines(self.stats())

        self.assertIn("수집 354건", lines[0])
        self.assertIn("사건 12건", lines[0])
        self.assertIn("이미 보낸 24건", lines[0])
        self.assertIn("관련 없는 39건", lines[0])
        self.assertIn("같은 사건 중복 보도 130건", lines[0])
        self.assertIn("Claude Haiku 4.5", lines[1])

    def test_notice_does_not_credit_the_model_on_a_fallback_run(self):
        lines = app.notice_lines(self.stats(curator=app.HEURISTIC_CURATOR))
        text = " ".join(lines)

        self.assertNotIn("Claude", text)
        self.assertNotIn("Haiku", text)
        self.assertIn("규칙 기반", text)

    def test_notice_omits_counts_that_are_zero(self):
        lines = app.notice_lines(
            self.stats(already_sent=0, irrelevant=0, folded=0)
        )

        self.assertNotIn("이미 보낸", lines[0])
        self.assertNotIn("관련 없는", lines[0])
        self.assertNotIn("중복 보도", lines[0])

    def test_notice_mentions_failed_keywords(self):
        self.assertNotIn(
            "수집에 실패", " ".join(app.notice_lines(self.stats()))
        )
        self.assertIn(
            "주제 2개는 수집에 실패",
            " ".join(app.notice_lines(self.stats(failed_keywords=2))),
        )

    def test_teams_message_puts_the_notice_above_the_first_section(self):
        now = datetime(2026, 9, 3, 1, 0, tzinfo=timezone.utc)
        story = app.Story(
            section="개인정보 유출 사고",
            headline="티빙, 개인정보 유출 공식 사과",
            summary="사과했다.",
            importance=5,
            representative=article("티빙 사과", "https://a.test/1", now, "한국일보"),
        )

        text = app.build_teams_message(
            {"개인정보 유출 사고": [story]}, self.stats()
        )["text"]

        self.assertIn("Claude Haiku 4.5", text)
        self.assertLess(text.index("Claude Haiku 4.5"), text.index("<h3>"))

    def test_teams_message_without_stats_has_no_notice(self):
        text = app.build_teams_message({})["text"]

        self.assertNotIn("Claude", text)


class ReportLinkTests(unittest.TestCase):
    def test_teams_message_links_the_html_report_near_the_top(self):
        now = datetime(2026, 9, 3, 1, 0, tzinfo=timezone.utc)
        story = app.Story(
            section="개인정보 유출 사고",
            headline="티빙, 개인정보 유출 공식 사과",
            summary="사과했다.",
            importance=5,
            representative=article("티빙 사과", "https://a.test/1", now, "한국일보"),
        )
        url = "https://example.github.io/report/"

        text = app.build_teams_message(
            {"개인정보 유출 사고": [story]}, report_url=url
        )["text"]

        self.assertIn(f'href="{url}"', text)
        self.assertLess(text.index(url), text.index("<h3>"))

    def test_teams_message_omits_the_link_when_no_url_is_given(self):
        self.assertNotIn("전체 HTML 보고서", app.build_teams_message({})["text"])

    def test_report_url_falls_back_to_the_published_page(self):
        self.assertEqual(app.DEFAULT_REPORT_URL, app.get_report_url())
        self.assertTrue(app.get_report_url().startswith("https://"))

    def test_report_url_is_escaped_in_the_link(self):
        text = app.build_teams_message(
            {}, report_url='https://a.test/?q="x"&y=1'
        )["text"]

        self.assertNotIn('?q="x"', text)
        self.assertIn("&amp;y=1", text)


class RetryTests(unittest.TestCase):
    def test_retry_succeeds_on_third_attempt(self):
        calls = 0
        delays: list[float] = []

        def operation():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise RuntimeError(f"temporary-{calls}")
            return "success"

        result = app.retry_operation("테스트", operation, sleep_func=delays.append)

        self.assertEqual("success", result)
        self.assertEqual(3, calls)
        self.assertEqual([2.0, 4.0], delays)

    def test_retry_gives_up_immediately_on_a_permanent_failure(self):
        calls = 0

        def operation():
            nonlocal calls
            calls += 1
            raise app.PermanentError("워크스페이스 ID 누락")

        with self.assertRaisesRegex(app.PermanentError, "워크스페이스"):
            app.retry_operation("테스트", operation, sleep_func=lambda _: None)

        # 설정 오류는 재시도해도 같은 결과라 한 번만 호출해야 한다.
        self.assertEqual(1, calls)

    def test_retry_stops_after_three_failures(self):
        calls = 0

        def operation():
            nonlocal calls
            calls += 1
            raise RuntimeError("persistent")

        with self.assertRaisesRegex(RuntimeError, "persistent"):
            app.retry_operation("테스트", operation, sleep_func=lambda _: None)

        self.assertEqual(3, calls)


class ParserTests(unittest.TestCase):
    def test_kakao_is_opt_in(self):
        parser = app.build_parser()

        self.assertFalse(parser.parse_args([]).kakao)
        self.assertTrue(parser.parse_args(["--kakao"]).kakao)

    def test_claude_is_on_by_default_and_can_be_disabled(self):
        parser = app.build_parser()

        self.assertFalse(parser.parse_args([]).no_claude)
        self.assertTrue(parser.parse_args(["--no-claude"]).no_claude)
        self.assertEqual(
            app.DEFAULT_WINDOW_HOURS, parser.parse_args([]).window_hours
        )


if __name__ == "__main__":
    unittest.main()
