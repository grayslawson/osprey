import pytest


@pytest.fixture(autouse=True)
def _legacy_security_unless_a_test_says_otherwise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keeps resolve_options from writing a marker into the cwd; tests of the
    secure defaults delete or override this variable."""
    monkeypatch.setenv("OSPREY_SECURITY", "legacy")
