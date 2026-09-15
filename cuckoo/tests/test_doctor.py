"""Contract tests for the read-only Osprey doctor command."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

from tests_support import doctor_fixture_environment


ROOT = Path(__file__).resolve().parents[2]
DOCTOR = ROOT / "scripts" / "osprey-doctor.sh"


def _fake_runtime(tmp_path: Path, *, curl_ok: bool = True) -> dict[str, str]:
    """Provide deterministic host tools while retaining the real Python binary."""

    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "docker").write_text("#!/bin/sh\n[ \"$1 $2\" = 'compose version' ]\n", encoding="utf-8")
    (tools / "ip").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (tools / "ss").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    curl_status = "exit 0" if curl_ok else "exit 1"
    (tools / "curl").write_text(f"#!/bin/sh\n{curl_status}\n", encoding="utf-8")
    for tool in tools.iterdir():
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    current_path = os.environ.get("PATH", "")
    return {"PATH": f"{tools}:{current_path}"}


def _config(path: Path) -> None:
    path.write_text(json.dumps({"host": "192.0.2.10"}) + "\n", encoding="utf-8")


def _run(tmp_path: Path, *args: str, curl_ok: bool = True) -> subprocess.CompletedProcess[str]:
    config = tmp_path / "cuckoo.json"
    _config(config)
    environment = os.environ.copy()
    environment.update(doctor_fixture_environment(tmp_path, curl_ok=curl_ok))
    return subprocess.run(
        [str(DOCTOR), "--config", str(config), *args],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_healthy_doctor_reports_callback_and_frigate(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "--host",
        "192.0.2.10",
        "--camera",
        "192.0.2.20",
        "--callback",
        "http://192.0.2.10:8000/health",
        "--frigate",
        "http://frigate.example.test:5000",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK      callback URL responded" in result.stdout
    assert "OK      Frigate API responded" in result.stdout


def test_unreachable_callback_is_a_required_error(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "--host",
        "192.0.2.10",
        "--callback",
        "http://192.0.2.10:8000/health",
        curl_ok=False,
    )

    assert result.returncode == 1
    assert "ERROR   callback URL did not respond" in result.stdout


def test_invalid_host_and_callback_url_report_independent_errors(tmp_path: Path) -> None:
    result = _run(tmp_path, "--host", "0.0.0.0", "--callback", "ftp://invalid")

    assert result.returncode == 1
    assert "ERROR   --host must be a concrete LAN address" in result.stdout
    assert "ERROR   callback URL must start with http:// or https://" in result.stdout


def test_invalid_config_and_port_are_reported_without_short_circuiting(tmp_path: Path) -> None:
    config = tmp_path / "invalid.json"
    config.write_text("not-json\n", encoding="utf-8")
    environment = os.environ.copy()
    environment.update(_fake_runtime(tmp_path))
    result = subprocess.run(
        [str(DOCTOR), "--host", "192.0.2.10", "--config", str(config), "--ports", "bad"],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "ERROR   configuration is invalid" in result.stdout
    assert "ERROR   invalid port in --ports: bad" in result.stdout


def test_unreachable_frigate_is_a_warning_when_other_checks_pass(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "--host",
        "192.0.2.10",
        "--frigate",
        "http://frigate.example.test:5000",
        curl_ok=False,
    )

    assert result.returncode == 0
    assert "WARNING Frigate API did not respond" in result.stdout


def test_missing_container_runtime_is_required_error(tmp_path: Path) -> None:
    config = tmp_path / "cuckoo.json"
    _config(config)
    tools = tmp_path / "minimal-tools"
    tools.mkdir()
    for name, body in {
        "python3": "#!/bin/sh\nexec /run/current-system/sw/bin/python3 \"$@\"\n",
        "ss": "#!/bin/sh\nexit 0\n",
    }.items():
        path = tools / name
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    result = subprocess.run(
        [str(DOCTOR), "--host", "192.0.2.10", "--config", str(config)],
        cwd=ROOT,
        env={**os.environ, "PATH": str(tools)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "ERROR   neither Docker Compose nor Podman Compose is available" in result.stdout
