from __future__ import annotations

from dataclasses import dataclass

from kbo_card_news.automation.job_state import AutomationJob, AutomationJobRepository


SUPPORTED_ACTIONS = {"produce", "discard"}


@dataclass(slots=True)
class DiscordComponentAction:
    action: str
    job_id: str


@dataclass(slots=True)
class DiscordActionResult:
    ok: bool
    message: str
    job: AutomationJob | None = None
    dry_run: bool = False


def parse_component_custom_id(custom_id: str) -> DiscordComponentAction:
    parts = str(custom_id or "").strip().split(":", 2)
    if len(parts) != 3 or parts[0] != "kbo":
        raise ValueError(f"unsupported component custom_id: {custom_id}")
    action = parts[1]
    job_id = parts[2]
    if action not in SUPPORTED_ACTIONS:
        raise ValueError(f"unsupported component action: {action}")
    if not job_id:
        raise ValueError("component custom_id has no job_id")
    return DiscordComponentAction(action=action, job_id=job_id)


def handle_component_action(
    repository: AutomationJobRepository,
    custom_id: str,
    *,
    actor: str = "",
    dry_run: bool = False,
) -> DiscordActionResult:
    action = parse_component_custom_id(custom_id)
    job = repository.get_job(action.job_id)
    if job is None:
        return DiscordActionResult(
            ok=False,
            message=f"job을 찾을 수 없습니다: {action.job_id}",
            dry_run=dry_run,
        )
    if action.action == "produce":
        return _approve_job(repository, job, actor=actor, dry_run=dry_run)
    if action.action == "discard":
        return _skip_job(repository, job, actor=actor, dry_run=dry_run)
    raise ValueError(f"unsupported component action: {action.action}")


def _approve_job(
    repository: AutomationJobRepository,
    job: AutomationJob,
    *,
    actor: str,
    dry_run: bool,
) -> DiscordActionResult:
    if job.status in {"approved", "pipeline_running", "editor_ready", "render_ready", "publish_approved", "published"}:
        return DiscordActionResult(
            ok=True,
            message=f"이미 제작 진행 중입니다: {job.topic_name}",
            job=job,
            dry_run=dry_run,
        )
    if job.status in {"skipped", "expired", "failed"}:
        return DiscordActionResult(
            ok=False,
            message=f"제작할 수 없는 상태입니다: {job.status}",
            job=job,
            dry_run=dry_run,
        )
    if dry_run:
        return DiscordActionResult(
            ok=True,
            message=f"[dry-run] approved로 변경 예정: {job.topic_name}",
            job=job,
            dry_run=True,
        )
    updated = repository.update_status(
        job.job_id,
        "approved",
        message=f"approved by Discord button: {actor or 'unknown'}",
        metadata={"approved_by": "discord_button", "discord_actor": actor},
    )
    return DiscordActionResult(
        ok=True,
        message=f"제작 승인됨: {updated.topic_name}",
        job=updated,
    )


def _skip_job(
    repository: AutomationJobRepository,
    job: AutomationJob,
    *,
    actor: str,
    dry_run: bool,
) -> DiscordActionResult:
    if job.status in {"pipeline_running", "editor_ready", "render_ready", "publish_approved", "published"}:
        return DiscordActionResult(
            ok=False,
            message=f"폐기할 수 없는 상태입니다: {job.status}",
            job=job,
            dry_run=dry_run,
        )
    if job.status == "skipped":
        return DiscordActionResult(
            ok=True,
            message=f"이미 폐기된 후보입니다: {job.topic_name}",
            job=job,
            dry_run=dry_run,
        )
    if dry_run:
        return DiscordActionResult(
            ok=True,
            message=f"[dry-run] skipped로 변경 예정: {job.topic_name}",
            job=job,
            dry_run=True,
        )
    updated = repository.update_status(
        job.job_id,
        "skipped",
        message=f"skipped by Discord button: {actor or 'unknown'}",
        metadata={"skip_reason": "discord_discard_button", "discord_actor": actor},
    )
    return DiscordActionResult(
        ok=True,
        message=f"폐기됨: {updated.topic_name}",
        job=updated,
    )
