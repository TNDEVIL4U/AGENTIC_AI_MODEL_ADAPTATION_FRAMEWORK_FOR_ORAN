"""Phase H (Rules 8 and 13): model artifacts are size-checked before anything deserializes them,
and every registered version carries the lineage an operator needs to trace it back."""

from __future__ import annotations

import pytest

from oran_adapt.core.config import Settings
from oran_adapt.core.errors import ArtifactError
from oran_adapt.core.integrity import check_size, path_size


def test_path_size_counts_a_file_and_a_whole_directory(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"x" * 100)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.bin").write_bytes(b"y" * 50)

    assert path_size(str(tmp_path / "a.bin")) == 100
    assert path_size(str(tmp_path)) == 150


def test_an_oversized_artifact_is_refused_before_loading(tmp_path):
    artifact = tmp_path / "model.joblib"
    artifact.write_bytes(b"\0" * 2048)

    assert check_size(str(artifact), 4096) == 2048
    with pytest.raises(ArtifactError) as info:
        check_size(str(artifact), 1024, stage="candidate")
    assert info.value.code == "ARTIFACT_ERROR"
    assert info.value.context["size_bytes"] == 2048
    assert info.value.context["max_bytes"] == 1024


def test_the_artifact_size_limit_is_configurable():
    assert Settings(_env_file=None).artifact_max_bytes == 2 * 1024**3
    assert Settings(_env_file=None, artifact_max_bytes=4096).artifact_max_bytes == 4096
    with pytest.raises(ValueError):
        Settings(_env_file=None, artifact_max_bytes=10)


def test_cli_onboarding_refuses_an_oversized_model_file(tmp_path, monkeypatch, capsys):
    from oran_adapt import cli

    big = tmp_path / "model.joblib"
    big.write_bytes(b"\0" * 4096)
    monkeypatch.setattr(
        cli, "get_settings", lambda: Settings(_env_file=None, artifact_max_bytes=1024)
    )

    code = cli.main(
        [
            "model",
            "onboard",
            "--model-id",
            "m",
            "--model-file",
            str(big),
            "--framework",
            "sklearn",
            "--task-type",
            "regressor",
            "--target",
            "y",
            "--dataset",
            "d",
            "--training-csv",
            str(tmp_path / "missing.csv"),
        ]
    )
    assert code == 1
    assert "exceeds the size limit" in capsys.readouterr().out
