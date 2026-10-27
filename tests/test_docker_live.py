"""Live checks that need Docker and network. Run with: pytest -m docker tests/test_docker_live.py"""
import pytest

from swe_env_forge.sandbox import DockerSandbox, docker_available

pytestmark = pytest.mark.docker

PING = ("python3 -c \"import socket; s = socket.create_connection(('127.0.0.1', 6379), 5); "
        "s.sendall(b'PING\\r\\n'); print(s.recv(64).decode().strip())\"")


@pytest.fixture(autouse=True)
def _needs_docker():
    if not docker_available():
        pytest.skip("Docker is not available")


@pytest.mark.parametrize("network", [True, False])
def test_redis_and_postgres_sidecars_on_localhost(network):
    services = [{"name": "cache", "image": "redis:7-alpine", "ports": [6379]},
                {"name": "db", "image": "postgres:16-alpine", "ports": [5432]}]
    with DockerSandbox("python:3.11-bookworm", "/tmp", network=network, services=services) as sb:
        assert sb.exec(PING, timeout=30).output.strip() == "+PONG"
        # the Postgres server answers its startup handshake on localhost
        probe = ("python3 -c \"import socket; s = socket.create_connection(('127.0.0.1', 5432), 5); "
                 "s.sendall(bytes([0,0,0,8,4,210,22,47])); print(s.recv(1))\"")   # SSLRequest -> b'N' or b'S'
        assert sb.exec(probe, timeout=30).output.strip() in ("b'N'", "b'S'")
        internet = sb.exec("python3 -c \"import urllib.request; urllib.request.urlopen('https://pypi.org', timeout=5)\"",
                           timeout=30)
        assert internet.ok is network      # services work either way; the internet only with network


def test_services_are_removed_on_close():
    import subprocess

    sb = DockerSandbox("python:3.11-bookworm", "/tmp", services=[{"name": "cache", "image": "redis:7-alpine", "ports": [6379]}])
    names = [*sb.service_names, sb.name]
    sb.close()
    out = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True).stdout
    assert not any(n in out.split() for n in names)


def test_failing_service_is_a_clear_error():
    from swe_env_forge.sandbox import SandboxError

    with pytest.raises(SandboxError, match="did not open port"):
        DockerSandbox("python:3.11-bookworm", "/tmp", service_timeout=5,
                      services=[{"name": "bad", "image": "redis:7-alpine", "ports": [6390]}])
