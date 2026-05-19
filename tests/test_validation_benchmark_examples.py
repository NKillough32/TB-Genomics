import json
import re
from pathlib import Path


def test_readme_listed_validation_benchmark_files_exist_and_parse():
    benchmark_dir = Path("examples/validation_benchmarks")
    readme = benchmark_dir / "README.md"
    listed_files = re.findall(r"- ([a-z0-9_]+\.json)", readme.read_text(encoding="utf-8"))

    assert listed_files

    for filename in listed_files:
        path = benchmark_dir / filename
        assert path.exists(), filename
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload.get("scenario")
        assert payload.get("expected_classification")
        assert isinstance(payload.get("pairs"), list)
