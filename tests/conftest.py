import pytest

from flight_recorder.settings import env_var_names


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Remove every setting from the environment so tests don't see real keys."""
    for name in env_var_names():
        monkeypatch.delenv(name, raising=False)
    return monkeypatch
