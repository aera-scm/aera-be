"""Lambda bundle build inputs (SRD 6.16, 7.1)."""

from pathlib import Path

from build_lambda import BUILD_ONLY, bundle_problems, runtime_requirements


def test_runtime_requirements_are_pinned_and_exclude_the_cdk_toolchain() -> None:
    requirements = runtime_requirements()

    assert requirements and all("==" in r for r in requirements)
    names = {r.split("==")[0] for r in requirements}
    assert not names & BUILD_ONLY
    assert {"pydantic", "aws-lambda-powertools", "httpx", "pypdf", "boto3"} <= names


def test_a_directory_is_a_bundle_only_with_code_and_dependencies(tmp_path: Path) -> None:
    assert bundle_problems(tmp_path)
    assert not bundle_problems(Path(__file__).parent / "fixtures" / "lambda-bundle")
