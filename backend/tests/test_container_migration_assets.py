from pathlib import Path


def test_evidence_scanner_image_includes_alembic_configuration_and_scripts() -> None:
    backend_dir = Path(__file__).resolve().parents[1]
    dockerfile = (backend_dir / "Dockerfile").read_text(encoding="utf-8")
    alembic_ini = (backend_dir / "alembic.ini").read_text(encoding="utf-8")

    assert "COPY alembic.ini ./alembic.ini" in dockerfile
    assert "COPY migrations ./migrations" in dockerfile
    assert "script_location = %(here)s/migrations" in alembic_ini
