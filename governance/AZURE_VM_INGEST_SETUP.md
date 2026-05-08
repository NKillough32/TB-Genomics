# Azure VM Sequencing Integration Setup

This guide shows how to connect sequencing outputs generated on an Azure VM to the TB Genomics PoC ingest API.

## 1. Prerequisites

- Azure VM has Python 3.10+ and outbound network access.
- API host is reachable from the VM.
- API endpoint available: POST /ingest/file.
- requests package installed on VM.

Install requests on VM:

```powershell
python -m pip install requests
```

## 2. Network and Security Requirements

Use these controls before enabling automated uploads:

1. Restrict API access to trusted networks only (VNet/VPN/private link preferred).
2. If public access is used, allowlist VM outbound IP and enable TLS.
3. Add authentication for ingest endpoint (API key or bearer token).
4. Store secrets in Azure Key Vault and inject at runtime.

Current code note:
- Ingest API key auth is now supported in the backend.
- Set TB_INGEST_API_KEY on the API host to require X-API-Key for ingest endpoints.

Backend example (PowerShell on API host):

```powershell
$env:TB_INGEST_API_KEY = "CHANGE_THIS_TO_A_LONG_RANDOM_SECRET"
uvicorn backend.app:app --reload
```

## 3. Connector Script

Use the connector script:

- scripts/azure_vm_ingest_connector.py

The script:

1. Scans source files (CSV/FASTA/FASTQ/VCF patterns).
2. Uploads only new or changed files.
3. Keeps state in a local JSON file.
4. Retries transient upload failures.

## 4. One-Time Test Run

From repo root on VM:

```powershell
python scripts/azure_vm_ingest_connector.py --once --source-dir C:\sequencing\out --ingest-url https://YOUR-API-HOST/ingest/file
```

## 5. Configure with Environment Variables

Use environment variables for repeatable runs:

```powershell
$env:TB_SOURCE_DIR = "C:\sequencing\out"
$env:TB_INGEST_URL = "https://YOUR-API-HOST/ingest/file"
$env:TB_FILE_PATTERNS = "*.csv,*.fasta,*.fastq,*.fastq.gz,*.vcf"
$env:TB_STATE_FILE = "C:\tb-connector\state.json"
$env:TB_TIMEOUT_SECONDS = "120"
$env:TB_MAX_RETRIES = "3"
$env:TB_ONCE = "1"

python scripts/azure_vm_ingest_connector.py
```

Optional auth headers:

```powershell
$env:TB_API_KEY = "YOUR_API_KEY"
# or
$env:TB_AUTH_BEARER = "YOUR_BEARER_TOKEN"
```

When TB_INGEST_API_KEY is set on the backend, configure TB_API_KEY on the VM connector
with the same value.

## 6. Scheduling on Azure VM

### Windows VM (Task Scheduler)

Create a scheduled task running every 5 minutes:

Program/script:

```text
C:\Path\To\Python\python.exe
```

Arguments:

```text
C:\Path\To\TB-Genomics-main\scripts\azure_vm_ingest_connector.py --once --source-dir C:\sequencing\out --ingest-url https://YOUR-API-HOST/ingest/file --state-file C:\tb-connector\state.json
```

### Linux VM (cron)

```bash
*/5 * * * * /usr/bin/python3 /opt/TB-Genomics-main/scripts/azure_vm_ingest_connector.py --once --source-dir /data/sequencing/out --ingest-url https://YOUR-API-HOST/ingest/file --state-file /var/lib/tb-connector/state.json >> /var/log/tb-connector.log 2>&1
```

## 7. Operational Checks

After enabling schedule:

1. Confirm uploaded files appear in uploads/. 
2. Confirm audit trail captures ingest actions.
3. Verify reruns skip unchanged files.
4. Simulate network outage and confirm retry behavior.

## 8. Recommended Next Hardening Changes

1. Require auth on /ingest/file.
2. Enforce file-size and extension allowlists server-side.
3. Add checksum verification endpoint or metadata logging.
4. Add malware scanning and PHI policy checks before processing.
