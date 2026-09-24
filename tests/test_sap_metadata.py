"""IR-02 prerequisite: synthetic XML never substitutes for official EDMX."""

import json
from pathlib import Path
from typing import Any

import download_sap_metadata as metadata
import pytest
from sap_transport import APIS, SapError


def synthetic_xml(api: str) -> bytes:
    entities = "".join(
        f'<EntitySet Name="{name}" EntityType="Synthetic.Item"/>'
        for name in metadata.ENTITY_SETS[api]
    )
    return (
        f'<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx" Version="1.0">'
        '<edmx:DataServices><Schema xmlns="http://schemas.microsoft.com/ado/2008/09/edm" '
        'Namespace="Synthetic"><EntityContainer Name="Synthetic">'
        f"{entities}</EntityContainer></Schema></edmx:DataServices></edmx:Edmx>"
    ).encode()


def test_ir_02_missing_official_inventory_fails(tmp_path: Path) -> None:
    with pytest.raises(SapError):
        metadata.check_inventory(tmp_path)


def test_ir_02_repository_inventory_is_not_claimed_complete() -> None:
    # Remove this assertion only when the owner supplies the six official files.
    with pytest.raises(SapError):
        metadata.check_inventory(Path("sap-mirror/metadata"))


def test_download_provenance_hashes_and_tampering(tmp_path: Path) -> None:
    def fetch(path: str, key: str, accept: str) -> bytes:
        assert path.endswith("/$metadata") and accept == "application/xml"
        return synthetic_xml(path.split("/")[-2])

    metadata.download(tmp_path, "synthetic-test-value", fetch=fetch)
    assert metadata.check_inventory(tmp_path) == 6
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert len(manifest["files"]) == 6
    assert "synthetic-test-value" not in json.dumps(manifest)
    (tmp_path / f"{APIS[0]}.edmx").write_bytes(b"tampered")
    with pytest.raises(SapError):
        metadata.check_inventory(tmp_path)


@pytest.mark.parametrize(
    "body", [b"<html/>", b"invalid", b'<!DOCTYPE x [<!ENTITY x "x">]><x/>', b"<Edmx/>"]
)
def test_ir_02_reject_non_edmx(body: bytes) -> None:
    with pytest.raises(SapError):
        metadata.validate_edmx(body, APIS[0])


def test_failed_download_does_not_publish_partial_inventory(tmp_path: Path) -> None:
    def fetch(path: str, key: str, accept: str) -> bytes:
        raise SapError("HTTP 403")

    with pytest.raises(SapError):
        metadata.download(tmp_path, "synthetic-test-value", fetch=fetch)
    assert list(tmp_path.iterdir()) == []


def test_cli_check_fails_safely(tmp_path: Path, capsys: Any) -> None:
    assert metadata.main(["--check", "--directory", str(tmp_path)], environ={}) == 1
    assert "incomplete" in capsys.readouterr().err
