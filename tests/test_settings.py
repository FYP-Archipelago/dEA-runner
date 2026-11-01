"""Deployment settings: one set, read from the environment by the service, masters and islands."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestrator.settings import ServiceSettings, redact_url


def test_the_defaults_are_the_agreed_limits():
    settings = ServiceSettings()
    assert settings.max_concurrent_runs == 2
    assert settings.max_containers == 15
    assert settings.master_image == "archipelago-runner:latest"
    assert settings.island_image == "archipelago-island:latest"


def test_settings_come_from_the_environment_by_name():
    settings = ServiceSettings.from_env(
        {
            "REDIS_URL": "redis://:pw@redis:6379/0",
            "MAX_CONCURRENT_RUNS": "3",
            "BYE_TIMEOUT_SECONDS": "7.5",
            "HOME": "/root",  # anything else in the environment is ignored
        }
    )
    assert settings.redis_url == "redis://:pw@redis:6379/0"
    assert settings.max_concurrent_runs == 3
    assert settings.bye_timeout_seconds == 7.5
    assert settings.heartbeat_seconds == ServiceSettings().heartbeat_seconds


def test_a_bad_value_in_the_environment_is_an_error():
    with pytest.raises(ValidationError, match="max_concurrent_runs"):
        ServiceSettings.from_env({"MAX_CONCURRENT_RUNS": "two"})
    with pytest.raises(ValidationError, match="max_concurrent_runs"):
        ServiceSettings.from_env({"MAX_CONCURRENT_RUNS": "0"})


@pytest.mark.parametrize("disabled", [None, 0, -1, 86_401])
def test_the_time_limit_is_always_on(disabled):
    """A hung island never says goodbye; the time limit is what ends its run."""
    assert ServiceSettings().run_time_limit_seconds == 3600
    with pytest.raises(ValidationError, match="run_time_limit_seconds"):
        ServiceSettings(run_time_limit_seconds=disabled)


def test_an_empty_time_limit_in_the_environment_is_an_error():
    with pytest.raises(ValidationError, match="run_time_limit_seconds"):
        ServiceSettings.from_env({"RUN_TIME_LIMIT_SECONDS": ""})


def test_the_containers_a_run_starts_get_the_same_settings():
    """The service passes to_env() to a run's master, and the master to its islands."""
    settings = ServiceSettings(redis_url="redis://:pw@redis:6379/0", island_memory="1g", heartbeat_seconds=0.5)
    assert ServiceSettings.from_env(settings.to_env()) == settings


def test_the_password_never_reaches_the_logged_settings():
    settings = ServiceSettings(redis_url="redis://runner:s3cret@redis:6379/0")
    assert settings.described()["redis_url"] == "redis://runner:***@redis:6379/0"
    assert "s3cret" not in str(settings.described())


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        ("redis://127.0.0.1:6379/0", "redis://127.0.0.1:6379/0"),
        ("redis://:pw@redis:6379/0", "redis://:***@redis:6379/0"),
        ("rediss://user:p%40ss@host/1", "rediss://user:***@host/1"),
        ("redis://:pw@[::1]:6379/0", "redis://:***@[::1]:6379/0"),
    ],
)
def test_redact_url_hides_only_the_password(url, shown):
    assert redact_url(url) == shown
