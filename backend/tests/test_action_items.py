import hashlib
import os
from collections.abc import Iterator
from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from app.core.config import get_settings
from app.core.security import hash_password
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.action_item import (
    ActionItem,
    ActionItemAssignment,
    ActionItemEvidence,
    ActionItemEvidenceScan,
    ActionItemStatusHistory,
)
from app.models.auth import Role, User
from app.models.meeting import (
    Meeting,
    MeetingStatus,
    MinutesDecision,
    MinutesDiscussion,
    MinutesRevision,
)
from app.models.organization import OrganizationUnit, ReportingLine, UserUnitMembership
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


@pytest.fixture
def action_item_context() -> Iterator[tuple[Session, dict[str, User], dict[str, UUID]]]:
    use_clamav_e2e_database = os.getenv("SITARA_CLAMAV_E2E") == "1"
    if use_clamav_e2e_database:
        database_url = os.getenv("SITARA_CLAMAV_E2E_DATABASE_URL")
        if not database_url:
            pytest.fail("SITARA_CLAMAV_E2E_DATABASE_URL is required for ClamAV E2E")
        parsed_url = make_url(database_url)
        if (
            parsed_url.drivername != "postgresql+psycopg"
            or parsed_url.host != "sitara-clamav-e2e-postgres"
            or parsed_url.database != "sitara_clamav_e2e"
            or parsed_url.username != "sitara_e2e"
        ):
            pytest.fail("ClamAV E2E requires its dedicated disposable PostgreSQL target")
        engine = create_engine(database_url)
        with engine.connect() as connection:
            (
                current_database,
                current_user,
                revision,
                user_count,
                task_count,
                evidence_count,
                scan_count,
            ) = connection.execute(
                text(
                    """
                    SELECT current_database(), current_user,
                        (SELECT version_num FROM alembic_version),
                        (SELECT count(*) FROM users),
                        (SELECT count(*) FROM action_items),
                        (SELECT count(*) FROM action_item_evidence),
                        (SELECT count(*) FROM action_item_evidence_scans)
                    """
                )
            ).one()
        if (
            current_database != "sitara_clamav_e2e"
            or current_user != "sitara_e2e"
            or revision != "0011_evidence_scan_quarantine"
            or user_count != 0
            or task_count != 0
            or evidence_count != 0
            or scan_count != 0
        ):
            engine.dispose()
            pytest.fail("ClamAV E2E PostgreSQL target must be empty and migrated to 0011")
    else:
        engine = create_engine(
            "sqlite+pysqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        event.listen(
            engine,
            "connect",
            lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"),
        )
        Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)

    role_codes = {"SEKRETARIAT", "REKTOR", "WAREK", "PIC", "SUPER_ADMIN"}
    roles: dict[str, Role] = {}
    for code in role_codes:
        role = session.scalar(select(Role).where(Role.role_code == code))
        if role is None:
            role = Role(role_code=code, role_name=code)
            session.add(role)
        roles[code] = role
    session.add_all(roles.values())
    users: dict[str, User] = {}
    role_assignments = {
        "secretariat": ("SEKRETARIAT",),
        "pic_main": ("PIC",),
        "pic_support": ("PIC",),
        "pic_unassigned": ("PIC",),
        "pic_no_membership": ("PIC",),
        "rector": ("REKTOR",),
        "warek": ("WAREK",),
        "other_rector": ("REKTOR",),
        "secretariat_leader": ("SEKRETARIAT", "WAREK"),
        "super_admin": ("SUPER_ADMIN",),
        "mixed_admin_secretariat": ("SUPER_ADMIN", "SEKRETARIAT"),
    }
    for name, user_roles in role_assignments.items():
        user = User(
            email=f"{name}@example.test",
            full_name=name.replace("_", " ").title(),
            password_hash=hash_password("synthetic-password"),
            is_active=name != "pic_unassigned",
        )
        user.roles.extend(roles[code] for code in user_roles)
        users[name] = user
    session.add_all(users.values())

    unit = OrganizationUnit(unit_code="TEST-UNIT", name="Synthetic Test Unit")
    session.add(unit)
    session.flush()
    now = datetime.now(UTC)
    members = (
        "secretariat",
        "pic_main",
        "pic_support",
        "pic_unassigned",
        "rector",
        "warek",
        "other_rector",
        "secretariat_leader",
    )
    for name in members:
        session.add(
            UserUnitMembership(
                user_id=users[name].id,
                unit_id=unit.id,
                valid_from=now.replace(year=now.year - 1),
            )
        )
    session.add(
        ReportingLine(
            manager_user_id=users["rector"].id,
            subordinate_user_id=users["pic_main"].id,
            valid_from=now.replace(year=now.year - 1),
        )
    )
    session.add(
        ReportingLine(
            manager_user_id=users["warek"].id,
            subordinate_user_id=users["pic_support"].id,
            valid_from=now.replace(year=now.year - 1),
        )
    )

    meeting_id, revision_id = uuid4(), uuid4()
    discussion_id, decision_id = uuid4(), uuid4()
    non_action_decision_id, other_meeting_id = uuid4(), uuid4()
    other_revision_id, other_discussion_id, other_decision_id = uuid4(), uuid4(), uuid4()
    session.add_all(
        [
            Meeting(
                id=meeting_id,
                meeting_number="TEST-RAPIM-001",
                title="Synthetic action item meeting",
                meeting_date=date(2026, 10, 21),
                created_by_id=users["secretariat"].id,
                status=MeetingStatus.LIVE,
                notetaker_id=users["secretariat"].id,
            ),
            Meeting(
                id=other_meeting_id,
                meeting_number="TEST-RAPIM-002",
                title="Other synthetic meeting",
                meeting_date=date(2026, 10, 21),
                created_by_id=users["secretariat"].id,
                status=MeetingStatus.LIVE,
                notetaker_id=users["secretariat"].id,
            ),
        ]
    )
    session.flush()
    session.add_all(
        [
            MinutesRevision(
                id=revision_id,
                meeting_id=meeting_id,
                revision_number=1,
                format_version=2,
                content=None,
                created_by_id=users["secretariat"].id,
            ),
            MinutesRevision(
                id=other_revision_id,
                meeting_id=other_meeting_id,
                revision_number=1,
                format_version=2,
                content=None,
                created_by_id=users["secretariat"].id,
            ),
        ]
    )
    session.flush()
    session.add_all(
        [
            MinutesDiscussion(
                id=discussion_id,
                meeting_id=meeting_id,
                minutes_revision_id=revision_id,
                agenda_id=_create_agenda(session, meeting_id),
                discussion_number=1,
                content="Synthetic discussion",
            ),
            MinutesDiscussion(
                id=other_discussion_id,
                meeting_id=other_meeting_id,
                minutes_revision_id=other_revision_id,
                agenda_id=_create_agenda(session, other_meeting_id),
                discussion_number=1,
                content="Other synthetic discussion",
            ),
        ]
    )
    session.flush()
    session.add_all(
        [
            MinutesDecision(
                id=decision_id,
                meeting_id=meeting_id,
                minutes_revision_id=revision_id,
                discussion_id=discussion_id,
                decision_number=1,
                decision_text="Complete the synthetic task",
                output_expected="Synthetic output",
                is_action_item=True,
            ),
            MinutesDecision(
                id=non_action_decision_id,
                meeting_id=meeting_id,
                minutes_revision_id=revision_id,
                discussion_id=discussion_id,
                decision_number=2,
                decision_text="Record only, no follow-up",
                is_action_item=False,
            ),
            MinutesDecision(
                id=other_decision_id,
                meeting_id=other_meeting_id,
                minutes_revision_id=other_revision_id,
                discussion_id=other_discussion_id,
                decision_number=1,
                decision_text="Other task source",
                is_action_item=True,
            ),
        ]
    )
    session.commit()
    app.dependency_overrides[get_db] = lambda: session
    refs = {
        "meeting_id": meeting_id,
        "revision_id": revision_id,
        "decision_id": decision_id,
        "non_action_decision_id": non_action_decision_id,
        "other_meeting_id": other_meeting_id,
        "other_revision_id": other_revision_id,
        "other_decision_id": other_decision_id,
    }
    try:
        yield session, users, refs
    finally:
        app.dependency_overrides.clear()
        session.close()
        engine.dispose()


def _create_agenda(session: Session, meeting_id: UUID, number: int = 1) -> UUID:
    from app.models.meeting import Agenda

    agenda = Agenda(meeting_id=meeting_id, agenda_number=number, title="Synthetic agenda")
    session.add(agenda)
    session.flush()
    return agenda.id


def _login(client: TestClient, user: User) -> None:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "synthetic-password"},
    )
    assert response.status_code == 200
    csrf_token = client.cookies.get("sitara_csrf")
    assert csrf_token
    client.headers.update({"X-CSRF-Token": csrf_token})


def _task_payload(refs: dict[str, UUID], **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "meeting_id": str(refs["meeting_id"]),
        "source_minutes_revision_id": str(refs["revision_id"]),
        "source_decision_id": str(refs["decision_id"]),
        "title": "Synthetic action item",
        "description": "Complete the agreed task",
        "output_expected": "Reviewed deliverable",
        "due_date": "2026-11-30",
        "priority": "Penting",
        "main_pic_user_id": None,
    }
    payload.update(overrides)
    return payload


def _create_task(client: TestClient, refs: dict[str, UUID], pic: User) -> dict[str, object]:
    response = client.post(
        "/api/v1/action-items",
        json=_task_payload(refs, main_pic_user_id=str(pic.id)),
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_secretariat_can_create_multiple_tasks_from_one_actionable_decision(
    action_item_context: tuple[Session, dict[str, User], dict[str, UUID]],
) -> None:
    session, users, refs = action_item_context
    payload = _task_payload(refs, main_pic_user_id=str(users["pic_main"].id))

    with TestClient(app) as client:
        _login(client, users["secretariat"])
        first = client.post("/api/v1/action-items", json=payload)
        second_payload = {**payload, "title": "Second task from the same decision"}
        second = client.post("/api/v1/action-items", json=second_payload)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["source_decision_id"] == second.json()["source_decision_id"]
    assert first.json()["official_status"] == "BELUM_DIMULAI"
    assert session.query(ActionItem).count() == 2
    assert session.query(ActionItemAssignment).count() == 2


def test_action_item_creation_rejects_non_actionable_and_cross_meeting_sources(
    action_item_context: tuple[Session, dict[str, User], dict[str, UUID]],
) -> None:
    _, users, refs = action_item_context
    with TestClient(app) as client:
        _login(client, users["secretariat"])
        non_action = client.post(
            "/api/v1/action-items",
            json=_task_payload(
                refs,
                source_decision_id=str(refs["non_action_decision_id"]),
                main_pic_user_id=str(users["pic_main"].id),
            ),
        )
        cross_meeting = client.post(
            "/api/v1/action-items",
            json=_task_payload(
                refs,
                source_decision_id=str(refs["other_decision_id"]),
                main_pic_user_id=str(users["pic_main"].id),
            ),
        )

    assert non_action.status_code == 422
    assert cross_meeting.status_code == 422


def test_action_item_creation_rejects_decision_from_another_revision_of_same_meeting(
    action_item_context: tuple[Session, dict[str, User], dict[str, UUID]],
) -> None:
    session, users, refs = action_item_context
    other_revision = MinutesRevision(
        meeting_id=refs["meeting_id"],
        revision_number=2,
        format_version=2,
        content=None,
        created_by_id=users["secretariat"].id,
    )
    session.add(other_revision)
    session.flush()
    discussion = MinutesDiscussion(
        meeting_id=refs["meeting_id"],
        minutes_revision_id=other_revision.id,
        agenda_id=_create_agenda(session, refs["meeting_id"], number=2),
        discussion_number=1,
        content="Different revision discussion",
    )
    session.add(discussion)
    session.flush()
    decision = MinutesDecision(
        meeting_id=refs["meeting_id"],
        minutes_revision_id=other_revision.id,
        discussion_id=discussion.id,
        decision_number=1,
        decision_text="Decision from another revision",
        is_action_item=True,
    )
    session.add(decision)
    session.commit()

    with TestClient(app) as client:
        _login(client, users["secretariat"])
        response = client.post(
            "/api/v1/action-items",
            json=_task_payload(
                refs,
                source_decision_id=str(decision.id),
                main_pic_user_id=str(users["pic_main"].id),
            ),
        )

    assert response.status_code == 422


def test_secretariat_may_select_any_official_status_and_each_change_is_audited(
    action_item_context: tuple[Session, dict[str, User], dict[str, UUID]],
) -> None:
    session, users, refs = action_item_context
    with TestClient(app) as client:
        _login(client, users["secretariat"])
        created = _create_task(client, refs, users["pic_main"])
        blocked = client.patch(
            f"/api/v1/action-items/{created['id']}/status",
            json={"expected_version": 1, "to_status": "TERKENDALA", "note": "Menunggu data"},
        )
        reset = client.patch(
            f"/api/v1/action-items/{created['id']}/status",
            json={"expected_version": 2, "to_status": "BELUM_DIMULAI"},
        )

    assert blocked.status_code == 200, blocked.text
    assert reset.status_code == 200, reset.text
    history = (
        session.query(ActionItemStatusHistory)
        .filter_by(action_item_id=UUID(created["id"]))
        .order_by(ActionItemStatusHistory.event_number)
        .all()
    )
    assert [row.new_status for row in history] == ["BELUM_DIMULAI", "TERKENDALA", "BELUM_DIMULAI"]


def test_completion_request_must_be_reviewed_and_rejection_preserves_status(
    action_item_context: tuple[Session, dict[str, User], dict[str, UUID]],
) -> None:
    _, users, refs = action_item_context
    with TestClient(app) as client:
        _login(client, users["secretariat"])
        created = _create_task(client, refs, users["pic_main"])
        client.cookies.clear()
        client.headers.clear()
        _login(client, users["pic_main"])
        report = client.post(
            f"/api/v1/action-items/{created['id']}/progress-updates",
            json={"report_text": "Mohon diperiksa", "completion_requested": True},
        )
        client.cookies.clear()
        client.headers.clear()
        _login(client, users["secretariat"])
        review = client.post(
            f"/api/v1/action-items/{created['id']}/completion-verifications",
            json={
                "expected_version": 1,
                "progress_update_id": report.json()["id"],
                "outcome": "NEEDS_REVISION",
                "review_note": "Mohon lengkapi output",
            },
        )

    assert report.status_code == 201
    assert review.status_code == 200, review.text
    assert review.json()["official_status"] == "BELUM_DIMULAI"
    assert review.json()["status_history"][-1]["new_status"] == "BELUM_DIMULAI"
    assert review.json()["completion_verifications"][-1]["outcome"] == "NEEDS_REVISION"


def test_create_rolls_back_task_assignment_and_initial_status_on_database_failure(
    action_item_context: tuple[Session, dict[str, User], dict[str, UUID]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.schemas.action_items import ActionItemCreate
    from app.services.action_items import create_action_item

    session, users, refs = action_item_context
    payload = ActionItemCreate.model_validate(
        _task_payload(refs, main_pic_user_id=str(users["pic_main"].id))
    )

    def fail_commit() -> None:
        raise SQLAlchemyError("synthetic commit failure")

    monkeypatch.setattr(session, "commit", fail_commit)
    with pytest.raises(SQLAlchemyError, match="synthetic commit failure"):
        create_action_item(session, users["secretariat"], payload)

    assert session.query(ActionItem).count() == 0
    assert session.query(ActionItemAssignment).count() == 0
    assert session.query(ActionItemStatusHistory).count() == 0


@pytest.mark.parametrize(
    "user_name", ["pic_main", "rector", "super_admin", "mixed_admin_secretariat"]
)
def test_only_operational_secretariat_can_create_action_items(
    action_item_context: tuple[Session, dict[str, User], dict[str, UUID]], user_name: str
) -> None:
    _, users, refs = action_item_context
    with TestClient(app) as client:
        _login(client, users[user_name])
        response = client.post(
            "/api/v1/action-items",
            json=_task_payload(refs, main_pic_user_id=str(users["pic_main"].id)),
        )

    assert response.status_code == 403


def test_write_requires_csrf(
    action_item_context: tuple[Session, dict[str, User], dict[str, UUID]],
) -> None:
    _, users, refs = action_item_context
    with TestClient(app) as client:
        _login(client, users["secretariat"])
        client.headers.pop("X-CSRF-Token")
        response = client.post(
            "/api/v1/action-items",
            json=_task_payload(refs, main_pic_user_id=str(users["pic_main"].id)),
        )

    assert response.status_code == 403


def test_assignment_history_keeps_old_main_and_adds_supporting_pic(
    action_item_context: tuple[Session, dict[str, User], dict[str, UUID]],
) -> None:
    session, users, refs = action_item_context
    with TestClient(app) as client:
        _login(client, users["secretariat"])
        created = _create_task(client, refs, users["pic_main"])
        supporting = client.post(
            f"/api/v1/action-items/{created['id']}/assignments",
            json={
                "assignee_user_id": str(users["pic_support"].id),
                "assignment_role": "SUPPORTING",
                "expected_version": 1,
                "reason": "Dukungan pelaksanaan",
            },
        )
        replaced = client.post(
            f"/api/v1/action-items/{created['id']}/assignments",
            json={
                "assignee_user_id": str(users["pic_no_membership"].id),
                "assignment_role": "MAIN",
                "expected_version": 2,
                "reason": "Perubahan penanggung jawab",
            },
        )

    assert supporting.status_code == 201, supporting.text
    assert replaced.status_code == 422
    history = (
        session.query(ActionItemAssignment).filter_by(action_item_id=UUID(created["id"])).all()
    )
    assert len(history) == 2
    assert sum(row.ended_at is None and row.assignment_role == "MAIN" for row in history) == 1
    assert sum(row.ended_at is None and row.assignment_role == "SUPPORTING" for row in history) == 1


def test_secretariat_can_replace_main_and_preserves_assignment_history(
    action_item_context: tuple[Session, dict[str, User], dict[str, UUID]],
) -> None:
    session, users, refs = action_item_context
    with TestClient(app) as client:
        _login(client, users["secretariat"])
        created = _create_task(client, refs, users["pic_main"])
        replaced = client.post(
            f"/api/v1/action-items/{created['id']}/assignments",
            json={
                "assignee_user_id": str(users["pic_support"].id),
                "assignment_role": "MAIN",
                "expected_version": 1,
                "reason": "Rotasi penanggung jawab",
            },
        )

    assert replaced.status_code == 201, replaced.text
    rows = session.query(ActionItemAssignment).filter_by(action_item_id=UUID(created["id"])).all()
    assert len(rows) == 2
    ended = next(row for row in rows if row.ended_at is not None)
    active = next(row for row in rows if row.ended_at is None)
    assert ended.assignee_user_id == users["pic_main"].id
    assert ended.end_reason == "Rotasi penanggung jawab"
    assert active.assignee_user_id == users["pic_support"].id
    assert active.assignment_role == "MAIN"


def test_pic_can_report_progress_but_cannot_change_official_status_or_read_unassigned_task(
    action_item_context: tuple[Session, dict[str, User], dict[str, UUID]],
) -> None:
    session, users, refs = action_item_context
    with TestClient(app) as client:
        _login(client, users["secretariat"])
        created = _create_task(client, refs, users["pic_main"])
        client.cookies.clear()
        client.headers.clear()
        _login(client, users["pic_main"])
        progress = client.post(
            f"/api/v1/action-items/{created['id']}/progress-updates",
            json={"report_text": "Pekerjaan dimulai", "progress_percent": 20},
        )
        status_change = client.patch(
            f"/api/v1/action-items/{created['id']}/status",
            json={"expected_version": 1, "to_status": "SELESAI"},
        )
        client.cookies.clear()
        client.headers.clear()
        _login(client, users["pic_support"])
        unassigned_detail = client.get(f"/api/v1/action-items/{created['id']}")

    assert progress.status_code == 201, progress.text
    assert status_change.status_code == 403
    assert unassigned_detail.status_code == 404
    task = session.get(ActionItem, UUID(created["id"]))
    assert task is not None and task.official_status == "BELUM_DIMULAI"
    assert session.query(ActionItemStatusHistory).filter_by(action_item_id=task.id).count() == 1


def test_reporting_leader_can_read_scoped_task_but_other_leader_cannot(
    action_item_context: tuple[Session, dict[str, User], dict[str, UUID]],
) -> None:
    _, users, refs = action_item_context
    with TestClient(app) as client:
        _login(client, users["secretariat"])
        created = _create_task(client, refs, users["pic_main"])
        client.cookies.clear()
        client.headers.clear()
        _login(client, users["rector"])
        scoped = client.get(f"/api/v1/action-items/{created['id']}")
        client.cookies.clear()
        client.headers.clear()
        _login(client, users["other_rector"])
        outside_scope = client.get(f"/api/v1/action-items/{created['id']}")

    assert scoped.status_code == 200
    assert outside_scope.status_code == 404


def test_rektor_and_warek_can_submit_progress_within_active_reporting_scope(
    action_item_context: tuple[Session, dict[str, User], dict[str, UUID]],
) -> None:
    _, users, refs = action_item_context
    with TestClient(app) as client:
        _login(client, users["secretariat"])
        created = _create_task(client, refs, users["pic_main"])
        supporting = client.post(
            f"/api/v1/action-items/{created['id']}/assignments",
            json={
                "assignee_user_id": str(users["pic_support"].id),
                "assignment_role": "SUPPORTING",
                "expected_version": 1,
                "reason": "Dukungan pelaksanaan",
            },
        )
        assert supporting.status_code == 201

        client.cookies.clear()
        client.headers.clear()
        _login(client, users["rector"])
        rector_update = client.post(
            f"/api/v1/action-items/{created['id']}/progress-updates",
            json={"report_text": "Progres dari Rektor", "completion_requested": True},
        )
        client.cookies.clear()
        client.headers.clear()
        _login(client, users["warek"])
        warek_update = client.post(
            f"/api/v1/action-items/{created['id']}/progress-updates",
            json={"report_text": "Progres PIC pendukung", "progress_percent": 40},
        )
        warek_list = client.get("/api/v1/action-items")
        client.cookies.clear()
        client.headers.clear()
        _login(client, users["other_rector"])
        outside_scope = client.post(
            f"/api/v1/action-items/{created['id']}/progress-updates",
            json={"report_text": "Tidak dalam scope"},
        )

    assert rector_update.status_code == 201, rector_update.text
    assert rector_update.json()["submitted_as"] == "LEADERSHIP"
    assert rector_update.json()["completion_requested"] is True
    assert warek_update.status_code == 201, warek_update.text
    assert warek_update.json()["submitted_as"] == "LEADERSHIP"
    listed_item = next(item for item in warek_list.json() if item["id"] == created["id"])
    assert listed_item["official_status"] == "BELUM_DIMULAI"
    assert outside_scope.status_code == 404


def test_secretariat_with_leadership_role_cannot_report_outside_reporting_scope(
    action_item_context: tuple[Session, dict[str, User], dict[str, UUID]],
) -> None:
    _, users, refs = action_item_context
    with TestClient(app) as client:
        _login(client, users["secretariat"])
        created = _create_task(client, refs, users["pic_main"])
        client.cookies.clear()
        client.headers.clear()
        _login(client, users["secretariat_leader"])
        response = client.post(
            f"/api/v1/action-items/{created['id']}/progress-updates",
            json={"report_text": "Tidak memiliki jalur pelaporan aktif"},
        )

    assert response.status_code == 403


def test_progress_evidence_upload_download_is_private_and_authorized(
    action_item_context: tuple[Session, dict[str, User], dict[str, UUID]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path,
) -> None:
    import logging

    monkeypatch.setenv("ACTION_ITEM_EVIDENCE_STORAGE_PATH", str(tmp_path / "private-evidence"))
    get_settings.cache_clear()
    session, users, refs = action_item_context
    with TestClient(app) as client:
        _login(client, users["secretariat"])
        created = _create_task(client, refs, users["pic_main"])
        client.cookies.clear()
        client.headers.clear()
        _login(client, users["rector"])
        progress = client.post(
            f"/api/v1/action-items/{created['id']}/progress-updates",
            json={"report_text": "Menyampaikan bukti", "completion_requested": True},
        )
        assert progress.status_code == 201, progress.text
        upload = client.post(
            f"/api/v1/action-items/{created['id']}/progress-updates/"
            f"{progress.json()['id']}/evidence/files",
            files={"file": ("hasil.pdf", BytesIO(b"%PDF-1.7\n%%EOF"), "application/x-untrusted")},
        )
        rejected_upload = client.post(
            f"/api/v1/action-items/{created['id']}/progress-updates/"
            f"{progress.json()['id']}/evidence/files",
            files={"file": ("rejected.pdf", BytesIO(b"%PDF-1.7\n%%EOF"), "application/pdf")},
        )
        link = client.post(
            f"/api/v1/action-items/{created['id']}/progress-updates/"
            f"{progress.json()['id']}/evidence/links",
            json={"url": "https://1drv.ms/s/synthetic"},
        )
        evidence_id = upload.json()["id"]
        rejected_evidence_id = rejected_upload.json()["id"]
        assert "storage_key" not in upload.json()
        download = client.get(f"/api/v1/action-items/{created['id']}/evidence/{evidence_id}")
        client.cookies.clear()
        client.headers.clear()
        _login(client, users["other_rector"])
        forbidden_download = client.get(
            f"/api/v1/action-items/{created['id']}/evidence/{evidence_id}"
        )

    assert upload.status_code == 201, upload.text
    assert upload.json()["content_type"] == "application/pdf"
    assert rejected_upload.status_code == 201, rejected_upload.text
    assert link.status_code == 201, link.text
    assert upload.json()["scan_status"] == "PENDING"
    assert download.status_code == 423
    assert download.content != b"%PDF-1.7\n%%EOF"
    assert forbidden_download.status_code == 404
    from app.services.action_items import record_evidence_scan_result

    record_evidence_scan_result(
        session,
        UUID(evidence_id),
        clean=True,
        scanner_name="synthetic-test-scanner",
        scanner_version="1",
        scanned_sha256=upload.json()["sha256"],
    )
    record_evidence_scan_result(
        session,
        UUID(rejected_evidence_id),
        clean=False,
        scanner_name="synthetic-test-scanner",
        scanner_version="1",
        scanned_sha256=rejected_upload.json()["sha256"],
    )
    client.cookies.clear()
    client.headers.clear()
    with TestClient(app) as client:
        _login(client, users["rector"])
        clean_download = client.get(f"/api/v1/action-items/{created['id']}/evidence/{evidence_id}")
        rejected_download = client.get(
            f"/api/v1/action-items/{created['id']}/evidence/{rejected_evidence_id}"
        )

    assert clean_download.status_code == 200
    assert clean_download.content == b"%PDF-1.7\n%%EOF"
    assert clean_download.headers["content-disposition"] == 'attachment; filename="hasil.pdf"'
    assert clean_download.headers["cache-control"] == "private, no-store"
    assert clean_download.headers["x-content-type-options"] == "nosniff"
    assert rejected_download.status_code == 423

    file_evidence = session.get(ActionItemEvidence, UUID(evidence_id))
    assert file_evidence is not None and file_evidence.storage_key is not None
    scan = session.scalar(
        select(ActionItemEvidenceScan)
        .where(ActionItemEvidenceScan.evidence_id == file_evidence.id)
        .order_by(ActionItemEvidenceScan.event_number.desc())
        .limit(1)
    )
    assert scan is not None
    stored_file = tmp_path / "private-evidence" / file_evidence.storage_key
    scan.scanned_sha256 = "0" * 64
    session.commit()
    with TestClient(app) as client:
        _login(client, users["rector"])
        mismatched_scan_download = client.get(
            f"/api/v1/action-items/{created['id']}/evidence/{evidence_id}"
        )
    assert mismatched_scan_download.status_code == 423

    scan.scanned_sha256 = upload.json()["sha256"]
    session.commit()
    stored_file.write_bytes(b"%PDF-1.7\nchanged after scan\n%%EOF")
    caplog.clear()
    with (
        caplog.at_level(logging.ERROR, logger="app.api.routes.action_items"),
        TestClient(app) as client,
    ):
        _login(client, users["rector"])
        modified_file_download = client.get(
            f"/api/v1/action-items/{created['id']}/evidence/{evidence_id}"
        )
    assert modified_file_download.status_code == 500
    assert b"changed after scan" not in modified_file_download.content
    assert f"action_item_id={created['id']}" in caplog.text
    assert f"evidence_id={evidence_id}" in caplog.text
    assert str(stored_file) not in caplog.text
    assert "hasil.pdf" not in caplog.text
    assert upload.json()["sha256"] not in caplog.text
    stored_file.unlink()
    with TestClient(app) as client:
        _login(client, users["rector"])
        missing_file_download = client.get(
            f"/api/v1/action-items/{created['id']}/evidence/{evidence_id}"
        )
    assert missing_file_download.status_code == 500

    assert not (tmp_path / "private-evidence" / "hasil.pdf").exists()
    get_settings.cache_clear()


@pytest.fixture
def isolated_evidence_storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    if os.getenv("SITARA_CLAMAV_DAEMON_E2E") == "1":
        storage_root = Path(os.getenv("ACTION_ITEM_EVIDENCE_STORAGE_PATH", ""))
        if storage_root != Path("/var/lib/sitara/evidence") or not storage_root.is_dir():
            pytest.fail("Daemon E2E requires its dedicated tmpfs evidence storage")
        get_settings.cache_clear()
        yield storage_root
        get_settings.cache_clear()
        return

    storage_root = tmp_path / "private-evidence"
    monkeypatch.setenv("ACTION_ITEM_EVIDENCE_STORAGE_PATH", str(storage_root))
    get_settings.cache_clear()
    try:
        yield storage_root
    finally:
        get_settings.cache_clear()


@pytest.mark.skipif(
    os.getenv("SITARA_CLAMAV_E2E") != "1",
    reason="requires an isolated migrated PostgreSQL database and live ClamAV",
)
def test_clamav_live_evidence_pipeline(
    action_item_context: tuple[Session, dict[str, User], dict[str, UUID]],
    isolated_evidence_storage: Path,
) -> None:
    import clamd
    from app import evidence_scanner_worker as worker

    session, users, refs = action_item_context
    settings = get_settings()
    scanner = clamd.ClamdNetworkSocket(host=settings.clamav_host, port=settings.clamav_port)
    assert scanner.ping() == "PONG"

    benign_content = b"SITARA isolated ClamAV evidence test\nSynthetic content only.\n"
    changed_content = b"SITARA evidence before checksum validation\n"
    outage_content = b"SITARA evidence while scanner is unavailable\n"
    eicar_content = (
        bytes.fromhex(
            "58354f2150254041505b345c505a58353428505e2937434329377d24"
            "45494341522d5354414e444152442d414e544956495255532d"
            "544553542d46494c452124482b482a"
        )
        + b"\n"
    )
    contents = {
        "safe.txt": benign_content,
        "eicar.txt": eicar_content,
        "outage.txt": outage_content,
        "mutated.txt": changed_content,
    }

    with TestClient(app) as client:
        _login(client, users["secretariat"])
        created = _create_task(client, refs, users["pic_main"])
        client.cookies.clear()
        client.headers.clear()
        _login(client, users["pic_main"])
        progress = client.post(
            f"/api/v1/action-items/{created['id']}/progress-updates",
            json={"report_text": "Mengunggah bukti sintetis untuk tes ClamAV"},
        )
        assert progress.status_code == 201, progress.text

        uploaded: dict[str, dict[str, str]] = {}
        upload_url = (
            f"/api/v1/action-items/{created['id']}/progress-updates/"
            f"{progress.json()['id']}/evidence/files"
        )
        for filename, content in contents.items():
            response = client.post(
                upload_url,
                files={"file": (filename, BytesIO(content), "text/plain")},
            )
            assert response.status_code == 201, response.text
            evidence = response.json()
            assert evidence["scan_status"] == "PENDING"
            assert evidence["sha256"] == hashlib.sha256(content).hexdigest()
            uploaded[filename] = evidence

        client.cookies.clear()
        client.headers.clear()
        anonymous_download = client.get(
            f"/api/v1/action-items/{created['id']}/evidence/{uploaded['safe.txt']['id']}"
        )
        _login(client, users["pic_support"])
        unauthorized_download = client.get(
            f"/api/v1/action-items/{created['id']}/evidence/{uploaded['safe.txt']['id']}"
        )
        client.cookies.clear()
        client.headers.clear()
        _login(client, users["pic_main"])
        pending_download = client.get(
            f"/api/v1/action-items/{created['id']}/evidence/{uploaded['safe.txt']['id']}"
        )

    assert anonymous_download.status_code == 401
    assert unauthorized_download.status_code == 404
    assert pending_download.status_code == 423

    evidence_rows = {
        filename: session.get(ActionItemEvidence, UUID(response["id"]))
        for filename, response in uploaded.items()
    }
    assert all(evidence is not None and evidence.storage_key for evidence in evidence_rows.values())
    evidence_paths = {
        filename: isolated_evidence_storage / evidence.storage_key
        for filename, evidence in evidence_rows.items()
        if evidence is not None and evidence.storage_key is not None
    }
    pending_ids = {row[0] for row in worker._pending_evidence(session)}
    assert pending_ids == {UUID(response["id"]) for response in uploaded.values()}

    outage_evidence = evidence_rows["outage.txt"]
    mutated_evidence = evidence_rows["mutated.txt"]
    safe_evidence = evidence_rows["safe.txt"]
    eicar_evidence = evidence_rows["eicar.txt"]
    assert all(
        evidence is not None
        for evidence in (outage_evidence, mutated_evidence, safe_evidence, eicar_evidence)
    )

    unavailable_scanner = clamd.ClamdNetworkSocket(
        host=settings.clamav_host,
        port=1,
    )
    with pytest.raises(clamd.ConnectionError):
        worker.scan_one(
            session,
            unavailable_scanner,
            outage_evidence.id,
            outage_evidence.storage_key,
            outage_evidence.sha256,
        )
    outage_scan = session.scalar(
        select(ActionItemEvidenceScan)
        .where(ActionItemEvidenceScan.evidence_id == outage_evidence.id)
        .order_by(ActionItemEvidenceScan.event_number.desc())
        .limit(1)
    )
    assert outage_scan is not None and outage_scan.scan_status == "PENDING"

    mutated_path = evidence_paths["mutated.txt"]
    mutated_path.write_bytes(b"modified before scanner validation\n")
    assert (
        worker.scan_one(
            session,
            scanner,
            mutated_evidence.id,
            mutated_evidence.storage_key,
            mutated_evidence.sha256,
        )
        is False
    )
    mutated_scan = session.scalar(
        select(ActionItemEvidenceScan)
        .where(ActionItemEvidenceScan.evidence_id == mutated_evidence.id)
        .order_by(ActionItemEvidenceScan.event_number.desc())
        .limit(1)
    )
    assert mutated_scan is not None and mutated_scan.scan_status == "PENDING"
    mutated_path.write_bytes(changed_content)

    for evidence in (safe_evidence, mutated_evidence, eicar_evidence):
        assert worker.scan_one(
            session,
            scanner,
            evidence.id,
            evidence.storage_key,
            evidence.sha256,
        )

    latest_scans: dict[str, ActionItemEvidenceScan] = {}
    for filename, evidence in evidence_rows.items():
        assert evidence is not None
        scan = session.scalar(
            select(ActionItemEvidenceScan)
            .where(ActionItemEvidenceScan.evidence_id == evidence.id)
            .order_by(ActionItemEvidenceScan.event_number.desc())
            .limit(1)
        )
        assert scan is not None
        latest_scans[filename] = scan
        if filename != "outage.txt":
            assert scan.scanned_sha256 == evidence.sha256
            assert scan.scanner_name == "ClamAV"
            assert scan.scanner_version

    assert latest_scans["safe.txt"].scan_status == "CLEAN"
    assert latest_scans["mutated.txt"].scan_status == "CLEAN"
    assert latest_scans["eicar.txt"].scan_status == "REJECTED"
    assert latest_scans["outage.txt"].scan_status == "PENDING"

    with TestClient(app) as client:
        client.cookies.clear()
        client.headers.clear()
        _login(client, users["pic_main"])
        clean_download = client.get(
            f"/api/v1/action-items/{created['id']}/evidence/{uploaded['safe.txt']['id']}"
        )
        rejected_download = client.get(
            f"/api/v1/action-items/{created['id']}/evidence/{uploaded['eicar.txt']['id']}"
        )
        outage_download = client.get(
            f"/api/v1/action-items/{created['id']}/evidence/{uploaded['outage.txt']['id']}"
        )
        mutated_download = client.get(
            f"/api/v1/action-items/{created['id']}/evidence/{uploaded['mutated.txt']['id']}"
        )
        safe_path = evidence_paths["safe.txt"]
        safe_path.write_bytes(b"X" + benign_content[1:])
        changed_after_scan_download = client.get(
            f"/api/v1/action-items/{created['id']}/evidence/{uploaded['safe.txt']['id']}"
        )
        safe_path.write_bytes(benign_content)

    assert clean_download.status_code == 200
    assert clean_download.content == benign_content
    assert rejected_download.status_code == 423
    assert outage_download.status_code == 423
    assert mutated_download.status_code == 200
    assert mutated_download.content == changed_content
    assert changed_after_scan_download.status_code == 500
    assert changed_after_scan_download.content != benign_content
    assert {path.name for path in isolated_evidence_storage.iterdir()} == {
        evidence.storage_key for evidence in evidence_rows.values() if evidence is not None
    }


@pytest.mark.skipif(
    os.getenv("SITARA_CLAMAV_DAEMON_E2E") != "1",
    reason="requires the disposable daemon E2E Docker harness",
)
def test_clamav_daemon_retries_pending_evidence_after_scanner_recovery(
    action_item_context: tuple[Session, dict[str, User], dict[str, UUID]],
    isolated_evidence_storage: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging
    import time

    session, users, refs = action_item_context
    control_path = Path(os.getenv("SITARA_CLAMAV_E2E_CONTROL_PATH", ""))
    if (
        control_path != Path("/tmp/sitara-clamav-e2e-control")
        or not control_path.is_dir()
        or isolated_evidence_storage != Path("/var/lib/sitara/evidence")
    ):
        pytest.fail("Daemon E2E requires its dedicated tmpfs control and evidence paths")

    benign_content = b"SITARA disposable daemon scan: benign synthetic text\n"
    changed_content = b"SITARA synthetic evidence changed after clean scan\n"
    eicar_content = (
        bytes.fromhex(
            "58354f2150254041505b345c505a58353428505e2937434329377d24"
            "45494341522d5354414e444152442d414e544956495255532d"
            "544553542d46494c452124482b482a"
        )
        + b"\n"
    )

    with TestClient(app) as client:
        _login(client, users["secretariat"])
        created = _create_task(client, refs, users["pic_main"])
        client.cookies.clear()
        client.headers.clear()
        _login(client, users["pic_main"])
        progress = client.post(
            f"/api/v1/action-items/{created['id']}/progress-updates",
            json={"report_text": "Bukti sintetis untuk daemon ClamAV"},
        )
        assert progress.status_code == 201, progress.text
        upload_url = (
            f"/api/v1/action-items/{created['id']}/progress-updates/"
            f"{progress.json()['id']}/evidence/files"
        )
        safe_response = client.post(
            upload_url,
            files={"file": ("daemon-safe.txt", BytesIO(benign_content), "text/plain")},
        )
        eicar_response = client.post(
            upload_url,
            files={"file": ("daemon-eicar.txt", BytesIO(eicar_content), "text/plain")},
        )
        assert safe_response.status_code == 201, safe_response.text
        assert eicar_response.status_code == 201, eicar_response.text
        safe_evidence = safe_response.json()
        eicar_evidence = eicar_response.json()
        assert safe_evidence["scan_status"] == eicar_evidence["scan_status"] == "PENDING"
        assert safe_evidence["sha256"] == hashlib.sha256(benign_content).hexdigest()
        assert eicar_evidence["sha256"] == hashlib.sha256(eicar_content).hexdigest()

        client.cookies.clear()
        client.headers.clear()
        anonymous_download = client.get(
            f"/api/v1/action-items/{created['id']}/evidence/{safe_evidence['id']}"
        )
        _login(client, users["pic_support"])
        unauthorized_download = client.get(
            f"/api/v1/action-items/{created['id']}/evidence/{safe_evidence['id']}"
        )
        client.cookies.clear()
        client.headers.clear()
        _login(client, users["pic_main"])
        pending_download = client.get(
            f"/api/v1/action-items/{created['id']}/evidence/{safe_evidence['id']}"
        )
    assert anonymous_download.status_code == 401
    assert unauthorized_download.status_code == 404
    assert pending_download.status_code == 423

    evidence_ids = {
        "safe": UUID(safe_evidence["id"]),
        "eicar": UUID(eicar_evidence["id"]),
    }

    def latest_scans() -> dict[str, ActionItemEvidenceScan | None]:
        session.expire_all()
        return {
            name: session.scalar(
                select(ActionItemEvidenceScan)
                .where(ActionItemEvidenceScan.evidence_id == evidence_id)
                .order_by(ActionItemEvidenceScan.event_number.desc())
                .limit(1)
            )
            for name, evidence_id in evidence_ids.items()
        }

    outage_started = time.monotonic()
    outage_observations = 0
    while time.monotonic() - outage_started < 2.2:
        scans = latest_scans()
        assert all(scan is not None and scan.scan_status == "PENDING" for scan in scans.values())
        assert all(
            len(
                list(
                    session.scalars(
                        select(ActionItemEvidenceScan).where(
                            ActionItemEvidenceScan.evidence_id == evidence_id
                        )
                    )
                )
            )
            == 1
            for evidence_id in evidence_ids.values()
        )
        outage_observations += 1
        time.sleep(0.1)
    outage_seconds = time.monotonic() - outage_started
    (control_path / "outage-verified").write_text("pending\n", encoding="utf-8")
    print(
        "daemon-e2e outage polling: statuses=PENDING/PENDING, "
        f"observations={outage_observations}, duration={outage_seconds:.1f}s"
    )

    recovery_deadline = time.monotonic() + 30
    while not (control_path / "recovery-ready").exists():
        if time.monotonic() >= recovery_deadline:
            pytest.fail("Disposable daemon did not signal scanner recovery within 30 seconds")
        time.sleep(0.1)

    scan_deadline = time.monotonic() + 30
    recovery_started = time.monotonic()
    scans = latest_scans()
    while not all(
        scan is not None and scan.scan_status in {"CLEAN", "REJECTED"} for scan in scans.values()
    ):
        if time.monotonic() >= scan_deadline:
            pytest.fail("Daemon did not reach terminal scan results within 30 seconds of recovery")
        time.sleep(0.1)
        scans = latest_scans()

    safe_scan = scans["safe"]
    eicar_scan = scans["eicar"]
    assert safe_scan is not None and safe_scan.scan_status == "CLEAN"
    assert eicar_scan is not None and eicar_scan.scan_status == "REJECTED"
    assert safe_scan.scanned_sha256 == safe_evidence["sha256"]
    assert eicar_scan.scanned_sha256 == eicar_evidence["sha256"]
    assert safe_scan.scanner_name == eicar_scan.scanner_name == "ClamAV"
    assert safe_scan.scanner_version and eicar_scan.scanner_version
    print(
        "daemon-e2e recovery polling: statuses=CLEAN/REJECTED, "
        "checksums=matched, "
        f"duration={time.monotonic() - recovery_started:.1f}s"
    )

    safe_record = session.get(ActionItemEvidence, evidence_ids["safe"])
    eicar_record = session.get(ActionItemEvidence, evidence_ids["eicar"])
    assert safe_record is not None and safe_record.storage_key is not None
    assert eicar_record is not None and eicar_record.storage_key is not None
    safe_path = isolated_evidence_storage / safe_record.storage_key

    with TestClient(app) as client:
        _login(client, users["pic_main"])
        clean_download = client.get(
            f"/api/v1/action-items/{created['id']}/evidence/{safe_evidence['id']}"
        )
        rejected_download = client.get(
            f"/api/v1/action-items/{created['id']}/evidence/{eicar_evidence['id']}"
        )
    assert clean_download.status_code == 200
    assert clean_download.content == benign_content
    assert rejected_download.status_code == 423

    safe_path.write_bytes(changed_content)
    caplog.clear()
    with (
        caplog.at_level(logging.ERROR, logger="app.api.routes.action_items"),
        TestClient(app) as client,
    ):
        _login(client, users["pic_main"])
        changed_download = client.get(
            f"/api/v1/action-items/{created['id']}/evidence/{safe_evidence['id']}"
        )
    assert changed_download.status_code == 500
    assert changed_content not in changed_download.content
    assert f"action_item_id={created['id']}" in caplog.text
    assert f"evidence_id={safe_evidence['id']}" in caplog.text
    assert str(safe_path) not in caplog.text
    assert "daemon-safe.txt" not in caplog.text
    assert safe_evidence["sha256"] not in caplog.text


def test_super_admin_cannot_upload_or_download_evidence(
    action_item_context: tuple[Session, dict[str, User], dict[str, UUID]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("ACTION_ITEM_EVIDENCE_STORAGE_PATH", str(tmp_path / "private-evidence"))
    get_settings.cache_clear()
    _, users, refs = action_item_context
    with TestClient(app) as client:
        _login(client, users["secretariat"])
        created = _create_task(client, refs, users["pic_main"])
        client.cookies.clear()
        client.headers.clear()
        _login(client, users["pic_main"])
        progress = client.post(
            f"/api/v1/action-items/{created['id']}/progress-updates",
            json={"report_text": "Bukti untuk pengujian akses"},
        )
        evidence = client.post(
            f"/api/v1/action-items/{created['id']}/progress-updates/"
            f"{progress.json()['id']}/evidence/files",
            files={"file": ("hasil.pdf", BytesIO(b"%PDF-1.7\n%%EOF"), "application/pdf")},
        )
        client.cookies.clear()
        client.headers.clear()
        _login(client, users["super_admin"])
        upload = client.post(
            f"/api/v1/action-items/{created['id']}/progress-updates/"
            f"{progress.json()['id']}/evidence/files",
            files={"file": ("lain.pdf", BytesIO(b"%PDF-1.7\n%%EOF"), "application/pdf")},
        )
        download = client.get(
            f"/api/v1/action-items/{created['id']}/evidence/{evidence.json()['id']}"
        )

    assert upload.status_code == 404
    assert download.status_code == 404
    get_settings.cache_clear()


def test_upload_transaction_failure_removes_private_file(
    action_item_context: tuple[Session, dict[str, User], dict[str, UUID]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from app.services.action_items import create_file_evidence
    from sqlalchemy.exc import OperationalError

    session, users, refs = action_item_context
    storage_root = tmp_path / "private-evidence"
    monkeypatch.setenv("ACTION_ITEM_EVIDENCE_STORAGE_PATH", str(storage_root))
    get_settings.cache_clear()
    with TestClient(app) as client:
        _login(client, users["secretariat"])
        created = _create_task(client, refs, users["pic_main"])
        client.cookies.clear()
        client.headers.clear()
        _login(client, users["pic_main"])
        progress = client.post(
            f"/api/v1/action-items/{created['id']}/progress-updates",
            json={"report_text": "Simulasi rollback metadata"},
        )

    def fail_commit() -> None:
        raise OperationalError("insert evidence", {}, RuntimeError("synthetic failure"))

    monkeypatch.setattr(session, "commit", fail_commit)
    with pytest.raises(OperationalError):
        create_file_evidence(
            session,
            users["pic_main"],
            UUID(created["id"]),
            UUID(progress.json()["id"]),
            "hasil.pdf",
            b"%PDF-1.7\n%%EOF",
        )
    assert list(storage_root.iterdir()) == []
    get_settings.cache_clear()


def test_progress_evidence_enforces_types_size_and_five_item_limit(
    action_item_context: tuple[Session, dict[str, User], dict[str, UUID]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("ACTION_ITEM_EVIDENCE_STORAGE_PATH", str(tmp_path / "private-evidence"))
    get_settings.cache_clear()
    _, users, refs = action_item_context
    with TestClient(app) as client:
        _login(client, users["secretariat"])
        created = _create_task(client, refs, users["pic_main"])
        client.cookies.clear()
        client.headers.clear()
        _login(client, users["pic_main"])
        progress = client.post(
            f"/api/v1/action-items/{created['id']}/progress-updates",
            json={"report_text": "Melampirkan bukti"},
        )
        evidence_path = (
            f"/api/v1/action-items/{created['id']}/progress-updates/"
            f"{progress.json()['id']}/evidence"
        )
        invalid_file = client.post(
            f"{evidence_path}/files",
            files={"file": ("payload.exe", BytesIO(b"MZ synthetic"), "application/octet-stream")},
        )
        oversized_file = client.post(
            f"{evidence_path}/files",
            files={
                "file": (
                    "oversized.pdf",
                    BytesIO(b"%PDF-" + b"x" * (10 * 1024 * 1024)),
                    "application/pdf",
                )
            },
        )
        uploaded_links = [
            client.post(
                f"{evidence_path}/links",
                json={"url": f"https://example.test/evidence/{index}"},
            )
            for index in range(5)
        ]
        sixth_item = client.post(
            f"{evidence_path}/links",
            json={"url": "https://example.test/evidence/overflow"},
        )
        insecure_link = client.post(
            f"{evidence_path}/links",
            json={"url": "http://example.test/evidence"},
        )

    assert all(response.status_code == 201 for response in uploaded_links)
    assert sixth_item.status_code == 409
    assert insecure_link.status_code == 422
    assert invalid_file.status_code == 415
    assert oversized_file.status_code == 413
    get_settings.cache_clear()


def test_source_revision_change_marks_task_stale_without_moving_provenance(
    action_item_context: tuple[Session, dict[str, User], dict[str, UUID]],
) -> None:
    session, users, refs = action_item_context
    with TestClient(app) as client:
        _login(client, users["secretariat"])
        created = _create_task(client, refs, users["pic_main"])

    newer_revision = MinutesRevision(
        meeting_id=refs["meeting_id"],
        revision_number=2,
        format_version=2,
        content=None,
        created_by_id=users["secretariat"].id,
    )
    session.add(newer_revision)
    session.commit()
    with TestClient(app) as client:
        _login(client, users["secretariat"])
        response = client.get(f"/api/v1/action-items/{created['id']}")

    assert response.status_code == 200
    assert response.json()["source_revision_is_stale"] is True
    task = session.get(ActionItem, UUID(created["id"]))
    assert task is not None and task.source_minutes_revision_id == refs["revision_id"]


def test_completion_requires_verification_and_is_bound_to_report(
    action_item_context: tuple[Session, dict[str, User], dict[str, UUID]],
) -> None:
    session, users, refs = action_item_context
    with TestClient(app) as client:
        _login(client, users["secretariat"])
        created = _create_task(client, refs, users["pic_main"])
        direct_completion = client.patch(
            f"/api/v1/action-items/{created['id']}/status",
            json={"expected_version": 1, "to_status": "SELESAI"},
        )
        client.cookies.clear()
        client.headers.clear()
        _login(client, users["pic_main"])
        report = client.post(
            f"/api/v1/action-items/{created['id']}/progress-updates",
            json={"report_text": "Hasil telah diserahkan", "completion_requested": True},
        )
        client.cookies.clear()
        client.headers.clear()
        _login(client, users["secretariat"])
        verified = client.post(
            f"/api/v1/action-items/{created['id']}/completion-verifications",
            json={
                "expected_version": 1,
                "progress_update_id": report.json()["id"],
                "outcome": "VERIFIED",
            },
        )

    assert direct_completion.status_code == 409
    assert report.status_code == 201
    assert verified.status_code == 200, verified.text
    payload = verified.json()
    assert payload["official_status"] == "SELESAI"
    status_event = payload["status_history"][-1]
    verification = payload["completion_verifications"][-1]
    assert status_event["verification_id"] == verification["id"]
    assert verification["progress_update_id"] == report.json()["id"]
    task = session.get(ActionItem, UUID(created["id"]))
    assert task is not None and task.official_status == "SELESAI"
