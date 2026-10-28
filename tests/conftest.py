"""Keep automatic approval tests away from the operator's live Codex daemon."""
import pytest


@pytest.fixture(autouse=True)
def isolated_codex_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv('COVENANT_CODEX_REMOTE', 'unix://' + str(tmp_path / 'unused-codex.sock'))
