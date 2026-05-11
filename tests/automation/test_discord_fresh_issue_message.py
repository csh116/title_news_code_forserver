from __future__ import annotations

import unittest

from kbo_card_news.automation.discord_bot import build_job_message
from kbo_card_news.automation.job_state import AutomationJob, AutomationJobArticle


class DiscordFreshIssueMessageTest(unittest.TestCase):
    def test_fresh_issue_message_includes_score_reasons_and_risks(self) -> None:
        job = AutomationJob(
            job_id="fresh-1",
            topic_id="fresh-1",
            topic_name="LG 오스틴 말소",
            notification_level="immediate",
            virality_potential_score=82,
            account_fit_score=90,
            metadata={
                "source": "watch_fresh_once",
                "fresh_article_count": 2,
                "context_article_count": 4,
                "source_diversity": 2,
                "matched_keywords": ["말소"],
                "score_reasons": ["신규 기사 2건", "injury 상태 변화 키워드"],
                "risk_flags": ["구단 공식 발표 확인 필요"],
                "gemini_decision": "approve",
                "gemini_confidence": 0.86,
            },
            articles=[
                AutomationJobArticle(
                    title="LG 오스틴 1군 말소",
                    source_type="news",
                    source_url="https://example.com/a",
                )
            ],
        )

        message = build_job_message(job)

        self.assertIn("[강한 이슈]", message)
        self.assertIn("점수: 82", message)
        self.assertIn("Gemini 판단: approve", message)
        self.assertIn("리스크:", message)
        self.assertLessEqual(len(message), 1700)


if __name__ == "__main__":
    unittest.main()
