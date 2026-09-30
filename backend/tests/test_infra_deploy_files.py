"""Static checks of the deployment files: compose, Dockerfile, CI, .env.example, Terraform."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "docker-compose.yml"
ENV_EXAMPLE = REPO / ".env.example"
DOCKERFILE = REPO / "backend" / "Dockerfile"
CI = REPO / ".github" / "workflows" / "ci.yml"
TERRAFORM = REPO / "infra" / "terraform"

yaml = pytest.importorskip("yaml")


def _documented() -> set[str]:
    """Names in .env.example, set (NAME=) or documented as comments (# NAME=)."""
    return set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", ENV_EXAMPLE.read_text(), re.M))


def _compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text())


# --------------------------------------------------------------------------- .env.example


def test_every_variable_compose_interpolates_is_documented() -> None:
    referenced = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", COMPOSE.read_text()))
    assert referenced, "compose should interpolate settings"
    assert referenced - _documented() == set()


def test_every_container_variable_is_documented() -> None:
    names: set[str] = set()
    for service in _compose()["services"].values():
        env = service.get("environment") or {}
        names |= set(env)
    tf = (TERRAFORM / "ecs.tf").read_text()
    names |= set(re.findall(r"^\s+([A-Z][A-Z0-9_]+)\s+=", tf, re.M))
    # Settings the Temporal worker reads (backoffice.workflows.worker).
    worker = (REPO / "backend/src/backoffice/workflows/worker.py").read_text()
    names |= set(re.findall(r'_(?:opt|flag)\(env, "([A-Z0-9_]+)"\)', worker))
    names |= {"BACKOFFICE_WORKFLOW_SERVICES", "MIGRATION_DATABASE_URL", "DATABASE_URL"}
    # Variables only the third-party images read are theirs to document.
    third_party = {
        "POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PWD", "POSTGRES_SEEDS", "DB", "DB_PORT",
        "DEFAULT_NAMESPACE", "DEFAULT_NAMESPACE_RETENTION", "TEMPORAL_CORS_ORIGINS",
        "MINIO_SITE_REGION", "REDISCLI_AUTH", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
        "AWS_CONFIG_FILE",
    }
    assert (names - third_party) - _documented() == set()


def test_env_example_holds_no_secrets() -> None:
    for name, value in re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", ENV_EXAMPLE.read_text(), re.M):
        if re.search(r"PASSWORD|SECRET|TOKEN|_KEY$|API_KEY", name):
            assert value == "" or value.startswith("change-me"), f"{name} looks like a real secret"


# --------------------------------------------------------------------------- compose


def test_compose_has_the_whole_stack() -> None:
    services = _compose()["services"]
    assert {"postgres", "migrate", "temporal", "temporal-ui", "redis", "minio", "minio-setup", "api", "worker"} <= set(services)
    assert "pgvector/pgvector:pg16" in services["postgres"]["image"]
    assert "temporalio/auto-setup" in services["temporal"]["image"]
    assert services["api"]["command"][:2] == ["uvicorn", "backoffice.api.app:app"]
    assert services["worker"]["command"] == ["python", "-m", "backoffice.workflows.worker"]
    assert services["migrate"]["command"] == ["python", "-m", "backoffice_db", "migrate"]


def test_compose_publishes_ports_on_localhost_only() -> None:
    for name, service in _compose()["services"].items():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name} exposes {port} beyond localhost"


def test_app_connects_as_the_rls_login_not_the_superuser() -> None:
    compose = COMPOSE.read_text()
    app_url = re.search(r"DATABASE_URL: (postgresql://[^:]+):", compose)
    assert app_url and app_url.group(1) == "postgresql://backoffice_api"


def test_services_start_after_migrations_and_bucket_setup() -> None:
    services = _compose()["services"]
    for name in ("api", "worker"):
        deps = services[name]["depends_on"]
        assert deps["migrate"]["condition"] == "service_completed_successfully"
        assert deps["minio-setup"]["condition"] == "service_completed_successfully"


# --------------------------------------------------------------------------- Dockerfile


def test_dockerfile_is_slim_non_root_and_serves_uvicorn() -> None:
    text = DOCKERFILE.read_text()
    assert re.search(r"^FROM python:3\.11-slim$", text, re.M)
    user = re.findall(r"^USER (\S+)", text, re.M)
    assert user and not user[-1].startswith(("root", "0"))
    assert 'pip install -e ".[workflows,documents,qr,vision]"' in text
    assert 'CMD ["uvicorn", "backoffice.api.app:app"' in text
    assert text.index("USER ") > text.index("pip install")  # installs as root, runs as the user


# --------------------------------------------------------------------------- CI


def test_ci_has_the_required_jobs_and_pinned_actions() -> None:
    workflow = yaml.safe_load(CI.read_text())
    assert {"backend", "web", "mobile", "sql"} <= set(workflow["jobs"])
    uses = re.findall(r"uses:\s*(\S+)", CI.read_text())
    assert uses and all(re.fullmatch(r"[\w.-]+/[\w.-]+@v\d+", u) for u in uses), uses


def test_ci_sql_job_applies_migrations_in_order_on_pgvector() -> None:
    job = yaml.safe_load(CI.read_text())["jobs"]["sql"]
    assert job["services"]["postgres"]["image"] == "pgvector/pgvector:pg16"
    script = "\n".join(step.get("run", "") for step in job["steps"])
    assert "for file in db/migrations/*.sql" in script and "ON_ERROR_STOP=1" in script


def test_ci_uses_the_requested_runtimes() -> None:
    text = CI.read_text()
    assert 'python-version: "3.11"' in text and "node-version: 22" in text
    assert 'pip install -e "./backend[dev]"' in text


# --------------------------------------------------------------------------- Terraform


def test_terraform_defaults_to_spain_and_only_eu_regions() -> None:
    variables = (TERRAFORM / "variables.tf").read_text()
    assert re.search(r'variable "region" \{.*?default\s+= "eu-south-2"', variables, re.S)
    eu = set(re.findall(r'^\s+"(eu-[a-z]+-\d)",', variables, re.M))
    assert eu == {"eu-west-1", "eu-west-3", "eu-central-1", "eu-north-1", "eu-south-1", "eu-south-2"}
    assert not {"eu-west-2", "eu-central-2"} & eu  # London and Zurich are not in the EU


def test_terraform_never_names_a_non_eu_region() -> None:
    for path in TERRAFORM.rglob("*.tf"):
        text = path.read_text()
        assert not re.search(r'"(?:us|ap|sa|ca|me|af|il|mx)-[a-z]+-\d"', text), path.name


def test_every_bucket_blocks_public_access() -> None:
    s3 = (TERRAFORM / "s3.tf").read_text()
    buckets = set(re.findall(r'resource "aws_s3_bucket" "(\w+)"', s3))
    blocked = set(re.findall(r'resource "aws_s3_bucket_public_access_block" "(\w+)"', s3))
    assert buckets and buckets == blocked


def test_evidence_bucket_is_locked_versioned_and_kms_encrypted() -> None:
    s3 = (TERRAFORM / "s3.tf").read_text()
    evidence = s3.split('resource "aws_s3_bucket" "evidence"', 1)[1].split("resource ", 1)[0]
    assert "object_lock_enabled = true" in evidence
    assert 'mode = "GOVERNANCE"' in s3 and 'sse_algorithm     = "aws:kms"' in s3
    assert '"s3:BypassGovernanceRetention"' in s3  # denied to all but the deletion role
