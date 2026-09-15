import unittest
from datetime import datetime, timezone

import app
import generate_site


class FeedbackTests(unittest.TestCase):
    def article(self, title="개인정보 유출 사고 조사", link="https://news.example/item?utm_source=x"):
        return app.Article(title, link, "신뢰신문", datetime.now(timezone.utc), "사고 조사 결과", ("개인정보 유출 사고",))

    def story(self, title="개인정보 유출 사고 조사"):
        return app.Story("개인정보 유출 사고", title, "사고 조사 결과", 3, self.article(title))

    def test_article_id_ignores_tracking_parameters(self):
        first = self.article(link="https://news.example/item?utm_source=x")
        second = self.article(link="https://news.example/item")
        self.assertEqual(app.article_id(first), app.article_id(second))

    def test_teams_links_to_confirmation_page_not_api(self):
        body = app.build_teams_message({"개인정보 유출 사고": [self.story()]}, report_url="https://example.github.io/report/", feedback_enabled=True)["text"]
        self.assertIn("👍 도움돼요", body)
        self.assertIn("vote=up", body)
        self.assertNotIn("/feedback-worker", body)

    def test_page_only_renders_buttons_when_api_is_configured(self):
        plain = generate_site.build_page({"주제": [self.story()]}, datetime.now(timezone.utc))
        enabled = generate_site.build_page({"주제": [self.story()]}, datetime.now(timezone.utc), feedback_api_url="https://worker.example")
        self.assertNotIn('data-vote="up"', plain)
        self.assertIn('data-vote="up"', enabled)
        self.assertIn("https://worker.example", enabled)

    def test_exact_downvote_excludes_article(self):
        story = self.story()
        signals = [{"article_id": app.article_id(story.representative), "vote": "down", "reason": "irrelevant", "count": 1, "title": story.headline, "topic": story.section, "source": "신뢰신문"}]
        self.assertEqual(app.apply_feedback([story], signals, [story.section]), [])

    def test_upvote_boosts_story_score(self):
        story = self.story()
        signals = [{"article_id": app.article_id(story.representative), "vote": "up", "reason": "", "count": 1, "title": story.headline, "topic": story.section, "source": "신뢰신문"}]
        result = app.apply_feedback([story], signals, [story.section])
        self.assertGreater(result[0].feedback_score, 0)


if __name__ == "__main__":
    unittest.main()
