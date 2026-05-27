import pytest
from pydantic import ValidationError

from backend.routers.data_management import CaseUpdatePayload
import importlib


case_data_mgmt = importlib.import_module("migrations.versions.0008_case_data_mgmt")


def test_case_data_management_revision_id_fits_alembic_version_column():
    assert len(case_data_mgmt.revision) <= 32


def test_case_update_requires_reason_and_change():
    with pytest.raises(ValidationError):
        CaseUpdatePayload(reason="fix")


def test_case_update_accepts_core_metadata_change():
    payload = CaseUpdatePayload(reason="corrected from source record", case_status="confirmed")

    assert payload.reason == "corrected from source record"
    assert payload.case_status == "confirmed"
