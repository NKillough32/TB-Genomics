#!/usr/bin/env python3
"""Upload sequencing output files from an Azure VM into the TB ingest API.

This script is designed to run on a schedule (Task Scheduler/cron) and only uploads
new or changed files based on a local state file.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import requests


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_patterns(raw: str) -> List[str]:
    parts = [part.strip() for part in raw.split(",")]
    return [part for part in parts if part]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_state(path: Path) -> Dict[str, Dict[str, str]]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_state(path: Path, state: Dict[str, Dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)


def iter_candidate_files(source_dir: Path, patterns: List[str]) -> Iterable[Path]:
    for root, _, files in os.walk(source_dir):
        root_path = Path(root)
        for filename in files:
            if any(fnmatch.fnmatch(filename, pattern) for pattern in patterns):
                yield root_path / filename


def file_state(path: Path) -> Dict[str, str]:
    stat = path.stat()
    return {
        "sha256": sha256_file(path),
        "size": str(stat.st_size),
        "mtime": str(int(stat.st_mtime)),
    }


def upload_file(
    ingest_url: str,
    path: Path,
    timeout_seconds: int,
    max_retries: int,
    headers: Dict[str, str],
) -> Tuple[bool, str]:
    last_error = "unknown upload error"

    for attempt in range(1, max_retries + 1):
        try:
            with path.open("rb") as handle:
                files = {"file": (path.name, handle, "application/octet-stream")}
                response = requests.post(
                    ingest_url,
                    files=files,
                    headers=headers,
                    timeout=timeout_seconds,
                )
            if 200 <= response.status_code < 300:
                return True, response.text.strip() or "ok"
            last_error = f"HTTP {response.status_code}: {response.text.strip()}"
        except Exception as exc:
            last_error = str(exc)

        if attempt < max_retries:
            time.sleep(min(2 ** attempt, 10))

    return False, last_error


def build_headers(api_key: str, bearer_token: str) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    if api_key:
        headers["X-API-Key"] = api_key
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    return headers


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload sequencing files to TB ingest API")

    parser.add_argument(
        "--source-dir",
        default=os.getenv("TB_SOURCE_DIR", "./exports"),
        help="Directory containing files to upload.",
    )
    parser.add_argument(
        "--ingest-url",
        default=os.getenv("TB_INGEST_URL", "http://localhost:8000/ingest/file"),
        help="Ingest API endpoint URL.",
    )
    parser.add_argument(
        "--patterns",
        default=os.getenv("TB_FILE_PATTERNS", "*.csv,*.fasta,*.fastq,*.fastq.gz,*.vcf"),
        help="Comma-separated filename patterns to upload.",
    )
    parser.add_argument(
        "--state-file",
        default=os.getenv("TB_STATE_FILE", "./uploads/.azure_ingest_state.json"),
        help="Path to the state file used to track uploaded files.",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=int(os.getenv("TB_INTERVAL_SECONDS", "300")),
        help="Polling interval when running continuously.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(os.getenv("TB_TIMEOUT_SECONDS", "120")),
        help="HTTP timeout per upload request.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=int(os.getenv("TB_MAX_RETRIES", "3")),
        help="Retries per file upload.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        default=env_bool("TB_ONCE", False),
        help="Run one pass and exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=env_bool("TB_DRY_RUN", False),
        help="Print actions without uploading.",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("TB_API_KEY", ""),
        help="Optional API key sent as X-API-Key.",
    )
    parser.add_argument(
        "--bearer-token",
        default=os.getenv("TB_AUTH_BEARER", ""),
        help="Optional bearer token sent as Authorization: Bearer.",
    )

    return parser.parse_args()


def run_once(args: argparse.Namespace) -> int:
    source_dir = Path(args.source_dir).resolve()
    state_file = Path(args.state_file).resolve()
    patterns = parse_patterns(args.patterns)

    if not source_dir.exists() or not source_dir.is_dir():
        print(f"ERROR: source dir not found: {source_dir}")
        return 2
    if not patterns:
        print("ERROR: no filename patterns configured")
        return 2

    state = load_state(state_file)
    new_state = dict(state)
    headers = build_headers(args.api_key, args.bearer_token)

    uploaded = 0
    skipped = 0
    failed = 0

    candidates = sorted(iter_candidate_files(source_dir, patterns))
    print(f"Scanning {len(candidates)} candidate file(s) in {source_dir}")

    for path in candidates:
        rel = str(path.relative_to(source_dir)).replace("\\", "/")
        current = file_state(path)
        previous = state.get(rel)

        if previous == current:
            skipped += 1
            continue

        if args.dry_run:
            print(f"DRY-RUN upload: {rel}")
            new_state[rel] = current
            uploaded += 1
            continue

        ok, detail = upload_file(
            ingest_url=args.ingest_url,
            path=path,
            timeout_seconds=args.timeout_seconds,
            max_retries=args.max_retries,
            headers=headers,
        )

        if ok:
            print(f"UPLOADED: {rel}")
            new_state[rel] = current
            uploaded += 1
        else:
            print(f"FAILED: {rel} -> {detail}")
            failed += 1

    save_state(state_file, new_state)

    print(
        "Done: "
        f"uploaded={uploaded}, skipped={skipped}, failed={failed}, "
        f"state={state_file}"
    )
    return 1 if failed else 0


def main() -> int:
    args = parse_args()

    if args.once:
        return run_once(args)

    print("Starting continuous mode. Press Ctrl+C to stop.")
    while True:
        exit_code = run_once(args)
        if exit_code not in (0, 1):
            return exit_code
        time.sleep(max(args.interval_seconds, 5))


if __name__ == "__main__":
    sys.exit(main())
