import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import app


class FreshNewsTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 19, 1, 0, tzinfo=timezone.utc)

    def article(
        self,
        title: str,
        link: str,
        published: datetime,
        source: str = "테스트",
        description: str | None = None,
    ) -> app.Article:
        return app.Article(title, link, source, published, description or title)

    def test_only_today_kst_and_global_duplicates_are_selected(self):
        today = self.article("오늘 기사", "https://example.com/a?utm_source=x", self.now)
        duplicate = self.article("오늘 기사", "https://example.com/other", self.now)
        yesterday = self.article(
            "어제 기사", "https://example.com/b", self.now - timedelta(days=1)
        )
        claimed: set[str] = set()

        first = app.select_today_articles(
            [yesterday, today], 5, claimed_keys=claimed, now=self.now
        )
        second = app.select_today_articles(
            [duplicate], 5, claimed_keys=claimed, now=self.now
        )

        self.assertEqual([today], first)
        self.assertEqual([], second)

    def test_sent_history_excludes_previously_sent_article(self):
        article = self.article("전송 완료", "https://example.com/a", self.now)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            app.save_sent_history({}, app.article_keys(article), path)
            history = app.load_sent_history(path)

            selected = app.select_today_articles(
                [article], 5, excluded_keys=set(history), now=self.now
            )

        self.assertEqual([], selected)

    def test_same_event_uses_one_credible_representative(self):
        articles = [
            self.article(
                "네이버, AI 시대 개인정보 보호 아이디어 발굴…대학생 10개팀 경쟁",
                "https://www.metroseoul.co.kr/article/20260819500147",
                self.now,
                "메트로신문",
                "네이버는 개인정보보호 관련 아이디어 공모전 네이버 프라이버시 챌린지를 진행했다.",
            ),
            self.article(
                "네이버, 개인정보보호 아이디어 공모전 개최",
                "https://www.msn.com/ko-kr/news/other/naver-privacy/ar-AA2aqfvr",
                self.now - timedelta(hours=2),
                "아이뉴스24 on MSN",
                "네이버는 개인정보보호 관련 아이디어 공모전 네이버 프라이버시 챌린지를 진행했다고 밝혔다.",
            ),
            self.article(
                "네이버, AI 시대 개인정보보호 아이디어 발굴…대학생 공모전 개최",
                "https://www.msn.com/ko-kr/news/news/naver-ai/ar-AA2aqjVg",
                self.now - timedelta(hours=3),
                "이투데이 on MSN",
                "네이버가 AI 시대 개인정보보호 강화를 위한 대학생 아이디어 발굴에 나섰다.",
            ),
            self.article(
                "네이버, 개인정보보호 아이디어 공모전 '프라이버시 챌린지' 진행",
                "https://www.msn.com/ko-kr/tech/naver-challenge/ar-AA2aq9Tk",
                self.now - timedelta(hours=4),
                "아시아투데이 on MSN",
                "네이버는 개인정보보호 관련 아이디어 공모전 프라이버시 챌린지를 진행했다.",
            ),
        ]

        claimed: set[str] = set()
        selected = app.select_today_articles(
            articles, 5, claimed_keys=claimed, now=self.now
        )

        self.assertEqual(1, len(selected))
        self.assertEqual("아이뉴스24 on MSN", selected[0].source)
        self.assertTrue(
            set().union(*(app.article_keys(article) for article in articles))
            <= claimed
        )

    def test_retry_succeeds_on_third_attempt(self):
        calls = 0
        delays: list[float] = []

        def operation():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise RuntimeError(f"temporary-{calls}")
            return "success"

        result = app.retry_operation(
            "테스트", operation, sleep_func=delays.append
        )

        self.assertEqual("success", result)
        self.assertEqual(3, calls)
        self.assertEqual([2.0, 4.0], delays)

    def test_retry_stops_after_three_failures(self):
        calls = 0

        def operation():
            nonlocal calls
            calls += 1
            raise RuntimeError("persistent")

        with self.assertRaisesRegex(RuntimeError, "persistent"):
            app.retry_operation(
                "테스트", operation, sleep_func=lambda _: None
            )

        self.assertEqual(3, calls)


if __name__ == "__main__":
    unittest.main()
