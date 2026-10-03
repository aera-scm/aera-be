"""Console upload to the web bucket (SRD 6.19): only dev, only a real build, index never cached."""

from pathlib import Path

import deploy_console
import pytest

from infra.environments import EnvironmentRefusedError


def test_srd_6_19_only_dev_is_deployed_from_here(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    with pytest.raises(EnvironmentRefusedError):
        deploy_console.plan_upload("final", tmp_path)


def test_srd_6_19_a_folder_without_a_console_build_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="index.html"):
        deploy_console.plan_upload("dev", tmp_path)


def test_srd_6_19_index_is_never_cached_and_hashed_assets_are_immutable(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    (tmp_path / "assets" / "index-a1b2c3.js").write_text("js", encoding="utf-8")
    (tmp_path / "brand").mkdir()
    (tmp_path / "brand" / "logo.png").write_bytes(b"png")

    uploads = {
        key: (kind, cache) for key, _, kind, cache in deploy_console.plan_upload("dev", tmp_path)
    }

    assert uploads["index.html"] == ("text/html; charset=utf-8", "no-cache")
    assert uploads["assets/index-a1b2c3.js"] == (
        "text/javascript; charset=utf-8",
        "public, max-age=31536000, immutable",
    )
    assert uploads["brand/logo.png"] == ("image/png", "public, max-age=3600")
