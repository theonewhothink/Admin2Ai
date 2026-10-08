"""Contract between the phone's offline queue and the evidence upload endpoint (§43).

The mobile app (mobile/src/offline) sends ``POST /api/evidence/upload`` as
multipart/form-data with the phone-computed sha256 and the original bytes, and
deletes its local copy only when the server's receipt carries the same hash.
These tests pin that contract from the backend side:

* the vocabularies the phone uses are real ``SourceKind`` / ``EvidenceFormat`` values;
* the golden wire fixture produced by the TypeScript encoder parses with a
  standard multipart parser, and its ``sha256`` field equals
  ``Evidence.hash_bytes`` of the file part;
* the fields build a valid ``Evidence`` record.

When the mobile toolchain is installed (``mobile/node_modules``), the mobile
unit tests and the TypeScript type-check also run from here, so a single
``pytest tests/test_mobile_*.py`` covers the whole module.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest

from backoffice.domain.models import Evidence, EvidenceFormat, SourceKind

MOBILE = Path(__file__).resolve().parents[2] / "mobile"
CONTRACT = MOBILE / "contracts" / "evidence-upload.json"
FIXTURE = MOBILE / "contracts" / "fixtures" / "evidence-upload.multipart"


def _contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def _parse_multipart(raw: bytes) -> tuple[dict[str, str], dict[str, object]]:
    """Parse a multipart/form-data body with the stdlib parser (no python-multipart needed)."""
    first_line = raw.split(b"\r\n", 1)[0]
    assert first_line.startswith(b"--"), "body must start with a boundary delimiter"
    boundary = first_line[2:].decode("ascii")
    message = BytesParser(policy=policy.HTTP).parsebytes(
        f"Content-Type: multipart/form-data; boundary={boundary}\r\n\r\n".encode("ascii") + raw
    )
    assert message.is_multipart()
    fields: dict[str, str] = {}
    file_part: dict[str, object] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True)
        assert isinstance(payload, bytes)
        filename = part.get_filename()
        if filename is not None:
            file_part = {"name": name, "filename": filename, "content_type": part.get_content_type(), "bytes": payload}
        else:
            fields[str(name)] = payload.decode("utf-8")
    return fields, file_part


def test_contract_vocabularies_are_backend_enums() -> None:
    contract = _contract()
    source_values = {s.value for s in SourceKind}
    format_values = {f.value for f in EvidenceFormat}
    assert set(contract["source_kinds"]) <= source_values
    assert set(contract["formats"]) <= format_values
    assert contract["source_kinds"] == ["mobile_scan", "mobile_share"]
    assert set(contract["required_fields"]) <= set(contract["fields"])
    assert contract["receipt"]["required"] == ["sha256"]


def test_golden_fixture_hash_matches_evidence_hash() -> None:
    contract = _contract()
    fields, file_part = _parse_multipart(FIXTURE.read_bytes())

    for required in contract["required_fields"]:
        assert fields.get(required), f"missing field {required}"
    assert file_part["name"] == contract["file_field"]
    data = file_part["bytes"]
    assert isinstance(data, bytes) and data.startswith(b"%PDF")

    # The phone's hash is lower-case hex sha256 of the original bytes, exactly Evidence.hash_bytes.
    assert fields["sha256"] == Evidence.hash_bytes(data)

    captured_at = datetime.fromisoformat(fields["captured_at"])
    assert captured_at.tzinfo is not None, "captured_at must carry the phone's UTC offset"
    assert json.loads(fields["hints"]) == {"title": "Your invoice is ready"}


def test_fixture_fields_build_a_valid_evidence_record() -> None:
    fields, file_part = _parse_multipart(FIXTURE.read_bytes())
    evidence = Evidence(
        tenant_id="tenant_demo",
        source_kind=SourceKind(fields["source"]),
        format=EvidenceFormat(fields["format"]),
        sha256=fields["sha256"],
        filename=str(file_part["filename"]),
        mime_type=str(file_part["content_type"]),
        metadata={"client_item_id": fields["client_item_id"], "captured_at": fields["captured_at"]},
    )
    assert evidence.source_kind is SourceKind.MOBILE_SHARE
    assert evidence.format is EvidenceFormat.PDF
    assert evidence.mime_type == "application/pdf"


def test_tampered_bytes_do_not_match_the_declared_hash() -> None:
    """The server must hash what it stored; a changed byte must produce a different receipt."""
    fields, file_part = _parse_multipart(FIXTURE.read_bytes())
    data = bytearray(file_part["bytes"])  # type: ignore[arg-type]
    data[0] ^= 0xFF
    assert Evidence.hash_bytes(bytes(data)) != fields["sha256"]


def _mobile_toolchain_ready() -> bool:
    return (MOBILE / "node_modules" / ".bin").is_dir() and shutil.which("npx") is not None


@pytest.mark.skipif(not _mobile_toolchain_ready(), reason="mobile/node_modules not installed (run npm install in mobile/)")
def test_mobile_unit_tests_pass() -> None:
    result = subprocess.run(
        ["npx", "jest", "--ci", "--silent"],
        cwd=MOBILE,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]


@pytest.mark.skipif(not _mobile_toolchain_ready(), reason="mobile/node_modules not installed (run npm install in mobile/)")
def test_mobile_typecheck_passes() -> None:
    result = subprocess.run(
        ["npx", "tsc", "--noEmit"],
        cwd=MOBILE,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
