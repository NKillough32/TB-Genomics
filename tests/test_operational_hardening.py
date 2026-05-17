from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from backend import data_safety, database
from backend.auth import (
    AuthenticatedUser,
    configured_token_identities,
    configured_tokens,
    get_current_user,
    require_roles,
)
from backend.app import _cors_origins, app
from backend.database import Base
from backend.models import (
    CaseContactLink,
    CaseLocationEvent,
    Contact,
    Exposure,
    Location,
)
from backend.routers.case_assets import get_outbreaker_image
from backend.routers.case_overview import data_readiness
from backend.routers.epidemiology import (
    CaseContactLinkCreate,
    CaseContactLinkUpdate,
    CaseLocationEventCreate,
    CaseLocationEventUpdate,
    create_case_contact_link,
    create_case_location_event,
    update_case_contact_link,
    update_case_location_event,
)
from backend.routers.ingest import _require_ingest_api_key


def test_ingest_api_key_is_optional_for_local_development(monkeypatch):
    monkeypatch.delenv("TB_INGEST_API_KEY", raising=False)

    assert _require_ingest_api_key("") is None


def test_ingest_api_key_rejects_missing_or_wrong_key_when_configured(monkeypatch):
    monkeypatch.setenv("TB_INGEST_API_KEY", "expected-key")

    with pytest.raises(HTTPException) as missing:
        _require_ingest_api_key("")
    assert missing.value.status_code == 401

    with pytest.raises(HTTPException) as wrong:
        _require_ingest_api_key("wrong-key")
    assert wrong.value.status_code == 401


def test_ingest_api_key_accepts_configured_key(monkeypatch):
    monkeypatch.setenv("TB_INGEST_API_KEY", "expected-key")

    assert _require_ingest_api_key("expected-key") is None


def test_auth_token_configuration_parses_roles(monkeypatch):
    monkeypatch.setenv("TB_AUTH_TOKENS", "viewer-token=viewer;ops-token=operator,analyst")

    assert configured_tokens() == {
        "viewer-token": ("viewer",),
        "ops-token": ("operator", "analyst"),
    }


def test_auth_token_configuration_can_include_audit_subject(monkeypatch):
    monkeypatch.setenv("TB_AUTH_TOKENS", "ops-token=lab-api|operator,analyst")

    identities = configured_token_identities()

    assert identities["ops-token"].subject == "lab-api"
    assert identities["ops-token"].roles == ("operator", "analyst")
    assert configured_tokens()["ops-token"] == ("operator", "analyst")


def test_rbac_resolves_token_subject_for_audit(monkeypatch):
    monkeypatch.setenv("TB_AUTH_REQUIRED", "1")
    monkeypatch.setenv("TB_AUTH_TOKENS", "ops-token=lab-api|operator")

    user = get_current_user(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials="ops-token")
    )

    assert user.subject == "lab-api"
    assert user.roles == ("operator",)


def test_init_db_runs_alembic_instead_of_create_all(monkeypatch):
    calls = []

    monkeypatch.setenv("TB_AUTO_MIGRATE", "1")
    monkeypatch.setattr(database.command, "upgrade", lambda config, target: calls.append(target))
    monkeypatch.setattr(
        database.Base.metadata,
        "create_all",
        lambda *args, **kwargs: pytest.fail("create_all should not run at startup"),
    )

    database.init_db()

    assert calls == ["head"]


def test_rbac_blocks_missing_token_when_enabled(monkeypatch):
    monkeypatch.setenv("TB_AUTH_REQUIRED", "1")
    monkeypatch.setenv("TB_AUTH_TOKENS", "viewer-token=viewer")

    with pytest.raises(HTTPException) as blocked:
        get_current_user(None)

    assert blocked.value.status_code == 401


def test_rbac_resolves_bearer_token_roles_when_enabled(monkeypatch):
    monkeypatch.setenv("TB_AUTH_REQUIRED", "1")
    monkeypatch.setenv("TB_AUTH_TOKENS", "viewer-token=viewer")

    user = get_current_user(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials="viewer-token")
    )

    assert user.roles == ("viewer",)
    assert user.auth_disabled is False


def test_rbac_role_hierarchy_rejects_viewer_for_operator_action():
    dependency = require_roles("operator")

    with pytest.raises(HTTPException) as blocked:
        dependency(AuthenticatedUser(subject="viewer", roles=("viewer",)))

    assert blocked.value.status_code == 403
    assert blocked.value.detail["error"] == "insufficient_role"


def test_rbac_role_hierarchy_allows_admin_for_operator_action():
    dependency = require_roles("operator")

    user = dependency(AuthenticatedUser(subject="admin", roles=("admin",)))

    assert user.roles == ("admin",)


def test_non_operational_dataset_blocks_sensitive_actions(monkeypatch):
    monkeypatch.delenv("TB_ALLOW_NON_OPERATIONAL_ACTIONS", raising=False)
    monkeypatch.setattr(
        data_safety,
        "get_data_safety_status",
        lambda _db: {
            "mode": "non_operational",
            "operational_safe": False,
            "total_cases": 10,
            "synthetic_case_count": 10,
            "synthetic_seed_events": 1,
            "message": "Synthetic/demo data detected.",
        },
    )

    with pytest.raises(HTTPException) as blocked:
        data_safety.enforce_operational_dataset(object(), "generate_report")

    assert blocked.value.status_code == 409
    assert blocked.value.detail["error"] == "blocked_non_operational_dataset"
    assert blocked.value.detail["action"] == "generate_report"


def test_non_operational_dataset_override_is_explicit(monkeypatch):
    monkeypatch.setenv("TB_ALLOW_NON_OPERATIONAL_ACTIONS", "1")
    status = {
        "mode": "non_operational",
        "operational_safe": False,
        "total_cases": 10,
        "synthetic_case_count": 10,
        "synthetic_seed_events": 1,
        "message": "Synthetic/demo data detected.",
    }
    monkeypatch.setattr(data_safety, "get_data_safety_status", lambda _db: status)

    assert data_safety.enforce_operational_dataset(object(), "generate_report") == status


def test_extracted_case_asset_route_remains_registered():
    assert any(route.path == "/cases/outbreaker-image/{filename}" for route in app.routes)


def test_outbreaker_image_rejects_path_like_names():
    assert get_outbreaker_image(r"outbreaker_..\secret.png") == {"error": "Invalid file"}
    assert get_outbreaker_image("outbreaker_/secret.png") == {"error": "Invalid file"}


def test_extracted_case_overview_routes_remain_registered():
    registered_paths = {route.path for route in app.routes}

    assert {
        "/cases/",
        "/cases/kpis",
        "/cases/regions",
        "/cases/summary",
        "/cases/data-readiness",
        "/cases/data-safety",
        "/cases/outbreaker-status",
        "/cases/audit-trail",
    }.issubset(registered_paths)


def test_extracted_case_report_routes_remain_registered_once():
    registered_paths = [route.path for route in app.routes]

    for path in {
        "/cases/outbreak-report",
        "/cases/outbreak-report.html",
        "/cases/outbreak-report.full.html",
        "/cases/resistance-validation/approve",
        "/cases/resistance-validation/status",
    }:
        assert registered_paths.count(path) == 1


def test_extracted_case_lookup_routes_remain_registered_once():
    registered_paths = [route.path for route in app.routes]

    for path in {
        "/cases/search",
        "/cases/case-history/{case_id}",
    }:
        assert registered_paths.count(path) == 1


def test_cors_origins_are_configurable(monkeypatch):
    monkeypatch.setenv("TB_CORS_ORIGINS", "https://tb.example.nhs.uk, http://localhost:8081 ")

    assert _cors_origins() == ["https://tb.example.nhs.uk", "http://localhost:8081"]


def test_cors_origins_keep_local_defaults(monkeypatch):
    monkeypatch.delenv("TB_CORS_ORIGINS", raising=False)

    assert _cors_origins() == ["http://localhost:8081", "http://127.0.0.1:8081"]


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value


class _MappingResult:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def first(self):
        return self.row


class _ReadinessDb:
    def execute(self, statement, params=None):
        sql = str(statement)
        if "to_regclass" in sql:
            return _ScalarResult(True)
        return _MappingResult(
            {
                "total_cases": 10,
                "sequenced_cases": 8,
                "qc_complete_cases": 7,
                "missing_geography": 2,
                "missing_dates": 1,
                "lineage_called_cases": 6,
                "resistance_called_cases": 5,
            }
        )


def test_data_readiness_reports_missingness_and_coverage():
    result = data_readiness(_ReadinessDb())

    assert result["total_cases"] == 10
    assert result["sequencing_coverage"]["percent"] == 80.0
    assert result["qc_completeness"]["percent"] == 70.0
    assert result["missing_geography"] == 2
    assert result["missing_dates"] == 1
    assert result["missing_lineage_calls"] == 4
    assert result["missing_resistance_calls"] == 5
    assert result["status"] == "needs_review"


def test_schema_declares_structured_epidemiology_tables():
    schema = (Path(__file__).resolve().parents[1] / "db" / "schema.sql").read_text(encoding="utf-8")

    for table_name in {
        "exposures",
        "contacts",
        "locations",
        "case_location_events",
        "case_contact_links",
    }:
        assert f"CREATE TABLE IF NOT EXISTS {table_name}" in schema


def test_orm_metadata_covers_schema_tables_for_alembic_autogenerate():
    expected_tables = {
        "cases",
        "tb_interpretation",
        "consensus_sequences",
        "sequencing_runs",
        "sample_qc_metrics",
        "analysis_provenance",
        "clusters",
        "case_clusters",
        "audit_log",
        "pipeline_validation_signoffs",
        "cluster_investigations",
        "contacts",
        "locations",
        "exposures",
        "case_location_events",
        "case_contact_links",
    }

    assert expected_tables.issubset(set(Base.metadata.tables))


def test_ni_exports_is_gitignored():
    gitignore = (Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8")

    assert "ni_exports/" in gitignore


def test_env_example_documents_deployment_runtime_knobs():
    env_example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(
        encoding="utf-8"
    )

    for name in {
        "TB_AUTO_MIGRATE",
        "TB_CORS_ORIGINS",
        "TB_ALLOW_MOCK_OUTBREAKER",
        "TB_OUTBREAKER_TIMEOUT_SEC",
        "TBPROFILER_WSL_FALLBACK",
        "LINEAGE_DR_FASTA",
        "TB_LINEAGE_DR_FASTA",
    }:
        assert f"{name}=" in env_example


def test_epidemiology_routes_remain_registered():
    registered_paths = {route.path for route in app.routes}

    assert {
        "/epidemiology/exposures",
        "/epidemiology/exposures/{exposure_id}",
        "/epidemiology/contacts",
        "/epidemiology/contacts/{contact_id}",
        "/epidemiology/locations",
        "/epidemiology/locations/{location_id}",
        "/epidemiology/case-location-events",
        "/epidemiology/case-location-events/{event_id}",
        "/epidemiology/case-contact-links",
        "/epidemiology/case-contact-links/{link_id}",
    }.issubset(registered_paths)


def test_actionable_report_routes_remain_registered():
    registered_paths = {route.path for route in app.routes}

    assert {
        "/reports/actionable-surveillance",
        "/reports/actionable-surveillance.html",
    }.issubset(registered_paths)


def test_epidemiology_sqlalchemy_models_match_table_names():
    assert Exposure.__tablename__ == "exposures"
    assert Contact.__tablename__ == "contacts"
    assert Location.__tablename__ == "locations"
    assert CaseLocationEvent.__tablename__ == "case_location_events"
    assert CaseContactLink.__tablename__ == "case_contact_links"


def test_alembic_revision_files_are_present_and_linked():
    versions = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    baseline = (versions / "0001_baseline.py").read_text(encoding="utf-8")
    epidemiology = (versions / "0002_epidemiology_tables.py").read_text(encoding="utf-8")

    assert 'revision: str = "0001_baseline"' in baseline
    assert 'revision: str = "0002_epidemiology_tables"' in epidemiology
    assert 'down_revision: Union[str, Sequence[str], None] = "0001_baseline"' in epidemiology
    for table_name in {
        "contacts",
        "locations",
        "exposures",
        "case_location_events",
        "case_contact_links",
    }:
        assert f"CREATE TABLE IF NOT EXISTS {table_name}" in epidemiology


class _EpiCreateDb:
    def __init__(self):
        self.objects = []

    def add(self, obj):
        self.objects.append(obj)

    def flush(self):
        for obj in self.objects:
            for attr in ("location_id", "contact_id", "exposure_id", "event_id", "link_id"):
                if hasattr(obj, attr) and getattr(obj, attr) is None:
                    setattr(obj, attr, uuid4())

    def commit(self):
        self.flush()

    def refresh(self, obj):
        self.flush()

    def get(self, model, primary_key):
        return None


class _EpiUpdateDb(_EpiCreateDb):
    def __init__(self, item):
        super().__init__()
        self.item = item

    def get(self, model, primary_key):
        return self.item


def test_create_case_location_event_creates_inline_location():
    db = _EpiCreateDb()
    case_id = UUID("11111111-1111-4111-8111-111111111111")

    event = create_case_location_event(
        CaseLocationEventCreate(
            case_id=case_id,
            location_name="Clinic A",
            event_type="exposure",
            confidence="medium",
            notes="Interview reported overlap",
        ),
        db,
        AuthenticatedUser(subject="ops", roles=("operator",)),
    )

    assert event.case_id == case_id
    assert event.location_id is not None
    assert any(isinstance(obj, Location) and obj.location_name == "Clinic A" for obj in db.objects)
    assert any(isinstance(obj, CaseLocationEvent) for obj in db.objects)


def test_create_case_contact_link_creates_inline_contact():
    db = _EpiCreateDb()
    case_id = UUID("11111111-1111-4111-8111-111111111111")

    link = create_case_contact_link(
        CaseContactLinkCreate(
            case_id=case_id,
            contact_label="household contact 1",
            relationship_type="household",
            link_type="reported_contact",
            confidence="high",
        ),
        db,
        AuthenticatedUser(subject="ops", roles=("operator",)),
    )

    assert link.case_id == case_id
    assert link.contact_id is not None
    assert any(isinstance(obj, Contact) and obj.contact_label == "household contact 1" for obj in db.objects)
    assert any(isinstance(obj, CaseContactLink) for obj in db.objects)


def test_update_case_location_event_updates_editable_fields():
    event = CaseLocationEvent(
        event_id=uuid4(),
        case_id=uuid4(),
        location_id=uuid4(),
        event_type="exposure",
        confidence="low",
    )
    db = _EpiUpdateDb(event)

    updated = update_case_location_event(
        event.event_id,
        CaseLocationEventUpdate(event_type="venue_overlap", confidence="high"),
        db,
        AuthenticatedUser(subject="ops", roles=("operator",)),
    )

    assert updated.event_type == "venue_overlap"
    assert updated.confidence == "high"


def test_update_case_contact_link_updates_editable_fields():
    link = CaseContactLink(
        link_id=uuid4(),
        case_id=uuid4(),
        contact_id=uuid4(),
        link_type="reported_contact",
        confidence="low",
    )
    db = _EpiUpdateDb(link)

    updated = update_case_contact_link(
        link.link_id,
        CaseContactLinkUpdate(link_type="household_contact", confidence="medium"),
        db,
        AuthenticatedUser(subject="ops", roles=("operator",)),
    )

    assert updated.link_type == "household_contact"
    assert updated.confidence == "medium"
