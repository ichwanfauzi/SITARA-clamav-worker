# ClamAV evidence scanning worker

SITARA keeps uploaded action-item evidence quarantined until a separate worker records a trusted ClamAV result. The existing `PENDING` scan event in PostgreSQL is the durable work queue: if the worker or ClamAV is unavailable, no clean result is written and downloads remain blocked.

## Components and flow

1. The API stores an allowed file under the private evidence storage directory, stores its SHA-256, and creates the initial `PENDING` scan event.
2. The `evidence-scanner` process polls PostgreSQL for file evidence whose latest scan event remains `PENDING`.
3. It verifies the on-disk SHA-256, submits the bytes to ClamAV over the private Compose network, and records `CLEAN` only for ClamAV `OK`; a detected signature becomes `REJECTED`.
4. Scanner, network, filesystem, or database failures are logged and leave the evidence quarantined for retry.
5. The existing download path continues to require the latest scan result to be `CLEAN` and the checksum to match.

**Deployment constraint:** run exactly one `evidence-scanner` replica. The queue is durable but does not yet use leases/claims, so multiple worker replicas could scan the same item concurrently. The database transition guards prevent a second terminal result from being appended, but the extra scan is wasteful.

## Local setup

- Docker Compose defines PostgreSQL, ClamAV, and the scanner worker. Start PostgreSQL and ClamAV, apply migrations, then start the worker using the steps below. First ClamAV startup can take a while while signatures are initialized.
- Create the shared host directory from the repository root: `New-Item -ItemType Directory -Force .\data\evidence`.
- When running the API directly on Windows, configure `ACTION_ITEM_EVIDENCE_STORAGE_PATH` to the same directory using an absolute Windows path (for example `C:\path\to\SITARA\data\evidence`). The worker sees that directory inside the container as `/var/lib/sitara/evidence`.
- The host-run API should connect to PostgreSQL at `localhost:5432`; the worker connects to `postgres:5432` inside Compose. Set `SCANNER_DATABASE_URL` if the local database credentials differ from the Compose defaults.
- The PostgreSQL service does not publish a host port. The scanner's default `DATABASE_URL` points to `postgres:5432/sitara_dev` on the Compose network; if `SCANNER_DATABASE_URL` is set, keep it pointed at the intended Compose database rather than `localhost`.
- Apply migrations once, before starting the worker. The worker image includes `alembic.ini` and `migrations`; `script_location = %(here)s/migrations` resolves to `/app/migrations` in the image. From the repository root:
  ```powershell
  docker compose build evidence-scanner
  docker compose up -d --wait postgres clamav
  docker compose run --rm --no-deps evidence-scanner alembic history
  docker compose run --rm --no-deps evidence-scanner alembic upgrade head
  docker compose run --rm --no-deps evidence-scanner alembic current
  docker compose up -d evidence-scanner
  ```
  `alembic upgrade head` can be repeated safely after an interrupted or completed upgrade. Do not add it to the worker startup command: run one migration process before starting the single worker replica. Do not point this command at a production database without a reviewed migration and rollout plan.
- Install backend dependencies after updating `pyproject.toml`: `python -m pip install -e ".[dev]"`.
- Check worker and ClamAV logs with `docker compose logs -f clamav evidence-scanner`.

## Isolated end-to-end verification

`backend/tests/test_action_items.py::test_clamav_live_evidence_pipeline` is opt-in with `SITARA_CLAMAV_E2E=1`. It requires a disposable PostgreSQL database named `sitara_clamav_e2e`, user `sitara_e2e`, and network alias `sitara-clamav-e2e-postgres`, already migrated to `0011_evidence_scan_quarantine`. The fixture refuses another URL or a database containing users, action items, or evidence; it does not create the PostgreSQL schema itself.

Run the test runner on the Compose network without publishing PostgreSQL ports. The test writes uploads only beneath pytest's temporary directory, constructs the EICAR test vector in memory, and expects `clamav` on the internal network. Do not point `SITARA_CLAMAV_E2E_DATABASE_URL` at `sitara_dev`, a persistent test database, or production.

For a real daemon-process check, run `backend/scripts/test-clamav-daemon-e2e.ps1` from the repository root in PowerShell. It builds the worker and a temporary test-runner image, creates the exact disposable database on tmpfs and a dedicated internal Docker network, and keeps evidence/control files in tmpfs mounts inside the disposable worker container. It temporarily stops only this Compose project's running `evidence-scanner`, disconnects the existing ClamAV container from the disposable network to simulate an outage, then reconnects it so the same daemon process can retry the PENDING queue. The script verifies the exact test database URL and migration head, uses bounded health/state polling, and removes its containers, network, temporary image, and temporary storage in `finally`; the original Compose worker is restarted if it was running before the test. It refuses to start if another evidence-scanner container is already running. PostgreSQL and ClamAV ports are not published.

PostgreSQL data uses a tmpfs mount without persistent mounts. The disposable worker container uses tmpfs mounts for evidence and test coordination; the test source directory is the only read-only bind mount. Pytest runs using `docker exec` inside the same container whose PID 1 is `python -m app.evidence_scanner_worker`, so the daemon and API fixture share only that container's disposable storage.

The daemon test is opt-in as `SITARA_CLAMAV_DAEMON_E2E=1` and is launched by the PowerShell harness. It creates safe and EICAR evidence through the FastAPI test client, but only the separate `python -m app.evidence_scanner_worker` container polls and records scan results. The earlier live pipeline test remains useful for direct scanner-function coverage; it does not substitute for this daemon test.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `CLAMAV_HOST` | `clamav` | ClamAV hostname visible to the worker |
| `CLAMAV_PORT` | `3310` | ClamAV TCP port; not published to the host |
| `CLAMAV_TIMEOUT_SECONDS` | `30` | Scanner network timeout |
| `EVIDENCE_SCAN_POLL_SECONDS` | `5` | Delay between empty polls / retry delay |
| `ACTION_ITEM_EVIDENCE_STORAGE_PATH` | unset | Required private evidence directory |
| `DATABASE_URL` | unset | Required database connection URL |

## Security notes and limitations

- Do not expose ClamAV TCP port 3310 publicly.
- Keep evidence storage private and shared read-only with the scanner container.
- Never mark an evidence item clean manually or expose a public scan-result endpoint.
- ClamAV signature detection is not a guarantee that a file is safe; retain extension/content validation, access control, checksum checks, and quarantine behavior.
- A changed or missing file remains quarantined; investigate and remediate it rather than bypassing the scan.
- Queue retry is currently polling-based without a retry limit or dead-letter table. Monitor worker logs and disk/DB health.
