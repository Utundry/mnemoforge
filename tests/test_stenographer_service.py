from pathlib import Path

import pytest

from app.services.stenographer_service import ProtocolViolation, StenographerStore


def test_work_session_state_machine_requires_single_active_work():
    store = StenographerStore(Path(":memory:"))
    try:
        work = store.start_work_session(
            project="alpha",
            task_id="task-1",
            agent_id="codex",
            session_id="sess-1",
            work_id="work-1",
        )
        assert work.status == "active"
        state = store.get_state(agent_id="codex", session_id="sess-1")
        assert state.state == "active_work"
        assert "record_stenographer_span" in state.next_valid_tools
        assert state.closeout_ready is False
        assert set(state.closeout_missing) == {"verification", "changed_files", "next_step"}

        with pytest.raises(ProtocolViolation) as exc_info:
            store.start_work_session(
                project="alpha",
                task_id="task-2",
                agent_id="codex",
                session_id="sess-1",
                work_id="work-2",
            )
        assert exc_info.value.code == "active_work_exists"

        for kind, content in (
            ("verification", "Manual check passed."),
            ("changed_files", "tests/test_stenographer_service.py"),
            ("next_step", "No next step."),
        ):
            store.record_span(
                project="alpha",
                task_id="task-1",
                agent_id="codex",
                session_id="sess-1",
                work_id="work-1",
                kind=kind,
                content=content,
            )

        ended = store.end_work_session(
            work_id="work-1",
            task_id="task-1",
            agent_id="codex",
            session_id="sess-1",
            status="completed",
            result="done",
        )
        assert ended.status == "completed"
        assert store.get_state(agent_id="codex", session_id="sess-1").state == "no_active_work"
    finally:
        store.close()


def test_completed_work_requires_explicit_closeout_spans():
    store = StenographerStore(Path(":memory:"))
    try:
        store.start_work_session(
            project="alpha",
            task_id="task-1",
            agent_id="codex",
            session_id="sess-1",
            work_id="work-1",
        )
        with pytest.raises(ProtocolViolation) as exc_info:
            store.end_work_session(
                work_id="work-1",
                task_id="task-1",
                agent_id="codex",
                session_id="sess-1",
                status="completed",
            )
        assert exc_info.value.code == "closeout_required"
        assert exc_info.value.required_next_tool == "record_stenographer_span"
        assert set(exc_info.value.state["closeout_missing"]) == {"verification", "changed_files", "next_step"}

        store.record_span(
            project="alpha",
            task_id="task-1",
            agent_id="codex",
            session_id="sess-1",
            work_id="work-1",
            kind="verification",
            content="pytest passed",
        )
        state = store.get_state(agent_id="codex", session_id="sess-1")
        assert state.protocol_violations == ["closeout_incomplete"]
        assert state.closeout_missing == ["changed_files", "next_step"]
    finally:
        store.close()


def test_parent_work_must_be_parked_before_child_work():
    store = StenographerStore(Path(":memory:"))
    try:
        store.start_work_session(
            project="alpha",
            task_id="parent-task",
            agent_id="codex",
            session_id="sess-1",
            work_id="parent-work",
        )
        parked = store.park_work_session(
            work_id="parent-work",
            agent_id="codex",
            session_id="sess-1",
            reason="Need focused child work.",
            child_task_id="child-task",
            child_work_id="child-work",
        )
        assert parked.status == "parked"

        child = store.start_work_session(
            project="alpha",
            task_id="child-task",
            agent_id="codex",
            session_id="sess-1",
            work_id="child-work",
            parent_work_id="parent-work",
            spawn_reason="Need focused child work.",
        )
        assert child.parent_work_id == "parent-work"

        with pytest.raises(ProtocolViolation) as exc_info:
            store.resume_work_session(
                work_id="parent-work",
                agent_id="codex",
                session_id="sess-1",
                child_work_id="child-work",
            )
        assert exc_info.value.code == "active_work_exists"

        for kind, content in (
            ("verification", "Child checks passed."),
            ("changed_files", "child.py"),
            ("next_step", "Resume parent."),
        ):
            store.record_span(
                project="alpha",
                task_id="child-task",
                agent_id="codex",
                session_id="sess-1",
                work_id="child-work",
                kind=kind,
                content=content,
            )

        store.end_work_session(
            work_id="child-work",
            task_id="child-task",
            agent_id="codex",
            session_id="sess-1",
            status="completed",
        )
        resumed = store.resume_work_session(
            work_id="parent-work",
            agent_id="codex",
            session_id="sess-1",
            child_work_id="child-work",
            result="Child completed.",
        )
        assert resumed.status == "active"
    finally:
        store.close()


def test_stenographer_span_requires_active_work_and_redacts_secret():
    store = StenographerStore(Path(":memory:"))
    try:
        with pytest.raises(ProtocolViolation) as exc_info:
            store.record_span(
                project="alpha",
                task_id="task-1",
                agent_id="codex",
                session_id="sess-1",
                kind="verification",
                content="13 passed",
            )
        assert exc_info.value.code == "work_session_required"

        store.start_work_session(
            project="alpha",
            task_id="task-1",
            agent_id="codex",
            session_id="sess-1",
            work_id="work-1",
        )
        span = store.record_span(
            project="alpha",
            task_id="task-1",
            agent_id="codex",
            session_id="sess-1",
            kind="verification",
            source="pytest",
            content="API_KEY=super-secret-value\n13 passed",
        )
        assert span.work_id == "work-1"
        assert span.excluded_from_learning is True
        assert "[REDACTED:api_key]" in span.content
        assert "api_key" in span.redaction_report

        duplicate = store.record_span(
            project="alpha",
            task_id="task-1",
            agent_id="codex",
            session_id="sess-1",
            kind="verification",
            source="pytest",
            content="API_KEY=super-secret-value\n13 passed",
        )
        assert duplicate.span_id == span.span_id
    finally:
        store.close()
