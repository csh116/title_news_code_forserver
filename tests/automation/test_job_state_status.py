from __future__ import annotations

from kbo_card_news.automation.job_state import AutomationJob, AutomationJobRepository


def test_update_status_clears_failure_message_on_success_status(tmp_path):
    db_path = tmp_path / "automation_state.db"

    with AutomationJobRepository(db_path) as repository:
        repository.create_job(
            AutomationJob(
                job_id="job-1",
                topic_id="topic-1",
                topic_name="테스트 주제",
                status="failed",
                failure_message="No module named 'design'",
            )
        )

        updated = repository.update_status("job-1", "editor_ready", message="retry succeeded")

    assert updated.failure_message is None


def test_update_status_preserves_failure_message_on_non_success_status(tmp_path):
    db_path = tmp_path / "automation_state.db"

    with AutomationJobRepository(db_path) as repository:
        repository.create_job(
            AutomationJob(
                job_id="job-1",
                topic_id="topic-1",
                topic_name="테스트 주제",
                status="failed",
                failure_message="old failure",
            )
        )

        updated = repository.update_status("job-1", "expired", message="expired after failure")

    assert updated.failure_message == "old failure"


def test_update_status_records_explicit_failure_message(tmp_path):
    db_path = tmp_path / "automation_state.db"

    with AutomationJobRepository(db_path) as repository:
        repository.create_job(
            AutomationJob(
                job_id="job-1",
                topic_id="topic-1",
                topic_name="테스트 주제",
                status="pipeline_running",
            )
        )

        updated = repository.update_status("job-1", "failed", failure_message="build failed")

    assert updated.failure_message == "build failed"
