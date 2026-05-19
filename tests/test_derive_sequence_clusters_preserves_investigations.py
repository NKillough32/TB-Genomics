from scripts.derive_sequence_clusters import _reconcile_stale_cluster


class _Result:
    def __init__(self, value=False):
        self.value = value

    def scalar(self):
        return self.value


class _FakeDb:
    def __init__(self, has_investigation: bool):
        self.has_investigation = has_investigation
        self.statements: list[str] = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)
        if "SELECT EXISTS" in sql:
            return _Result(self.has_investigation)
        return _Result(False)


def _summary():
    return {
        "cluster_membership_changes": {
            "stale_clusters_deleted": 0,
            "stale_clusters_superseded_with_investigation": 0,
        }
    }


def test_stale_cluster_with_investigation_is_superseded_not_deleted():
    db = _FakeDb(has_investigation=True)
    summary = _summary()

    _reconcile_stale_cluster(db, "00000000-0000-0000-0000-000000000001", summary)

    joined_sql = "\n".join(db.statements)
    assert "DELETE FROM case_clusters" in joined_sql
    assert "UPDATE clusters" in joined_sql
    assert "investigation_status = 'superseded'" in joined_sql
    assert "DELETE FROM clusters" not in joined_sql
    assert summary["cluster_membership_changes"]["stale_clusters_superseded_with_investigation"] == 1


def test_stale_cluster_without_investigation_can_be_deleted():
    db = _FakeDb(has_investigation=False)
    summary = _summary()

    _reconcile_stale_cluster(db, "00000000-0000-0000-0000-000000000001", summary)

    joined_sql = "\n".join(db.statements)
    assert "DELETE FROM case_clusters" in joined_sql
    assert "DELETE FROM clusters" in joined_sql
    assert "UPDATE clusters" not in joined_sql
    assert summary["cluster_membership_changes"]["stale_clusters_deleted"] == 1
