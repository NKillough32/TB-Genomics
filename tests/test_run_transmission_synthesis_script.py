import json


def test_run_transmission_synthesis_writes_export(monkeypatch, tmp_path):
    from scripts import run_transmission_synthesis as script

    class FakeSession:
        closed = False

        def close(self):
            self.closed = True

    session = FakeSession()
    export_path = tmp_path / "exports" / "synthesis_output.json"

    monkeypatch.setattr(script, "EXPORT_PATH", export_path)
    monkeypatch.setattr(script, "SessionLocal", lambda: session)
    monkeypatch.setattr(
        script,
        "build_transmission_synthesis",
        lambda db, cluster_id=None: {
            "generated_at": "2026-05-19T00:00:00Z",
            "summary": {"cluster_count": 1, "pair_count": 2},
            "clusters": [{"cluster_id": "cluster-1"}],
            "pairs": [{"source": "a", "target": "b", "confidence_code": "strong_support"}],
        },
    )

    assert script.main() == 0
    assert session.closed is True

    payload = json.loads(export_path.read_text(encoding="utf-8"))
    assert payload["summary"]["cluster_count"] == 1
    assert payload["pairs"][0]["confidence_code"] == "strong_support"
