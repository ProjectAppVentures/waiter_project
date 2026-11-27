"""Comprueba el montaje con recursos desechables en CI."""

import http.client
import json
import os
import secrets
import socket
import subprocess
import sys
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

import pytest


ROOT = Path(__file__).resolve().parents[3]
DEV = ROOT / "scripts/dev.sh"
sys.path.insert(0, str(ROOT / "experience"))
from experience_project.qa_runtime import identity


def write_private(path, data):
    path.touch(mode=0o600)
    path.write_text(json.dumps(data), encoding="utf-8")


def config_https(config):
    secure = deepcopy(config)
    origins = secure["origins"]
    for name in ("central", "platform", "menu"):
        origins[name] = origins[name].replace("http://", "https://", 1)
    origins["organizations"] = {
        slug: origin.replace("http://", "https://", 1)
        for slug, origin in origins["organizations"].items()
    }
    return secure


def docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True, timeout=45).stdout.strip()


def new_ports():
    with ExitStack() as bindings:
        ports = []
        for _ in range(3):
            listener = bindings.enter_context(socket.socket())
            listener.bind(("127.0.0.1", 0))
            ports.append(listener.getsockname()[1])
    return tuple(ports)


class Fixture:
    def __init__(self, path):
        path.chmod(0o700)
        self.directory = path
        self.mount = "ci-wt2-" + secrets.token_hex(4)
        self.volume = f"waiter-qa-{self.mount}"
        self.config_path = path / "config.json"
        self.receipt_path = path / "montaje-qa.json"
        self.data_path = path / "perfil-datos.json"
        self.ports = new_ports()
        front_port, api_port, mysql = self.ports
        host = f"qa-a.waiter.localhost:{front_port}"
        credentials = path / "accesos.json"
        actors = ("dueno", "encargado", "cajero", "mesero", "mesero.a2", "mesero.b1")
        write_private(credentials, {"db_password": secrets.token_urlsafe(32),
                                    "accounts": {actor: secrets.token_urlsafe(20) for actor in actors}})
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        self.config = {
            "version": 1, "environment": "development", "mount_id": self.mount, "owner": "CI",
            "checkout": str(ROOT), "sha": sha, "profile": "caja-cerrada", "profile_version": 1,
            "namespace": "wt2-ci", "state_dir": str(path), "credentials_ref": str(credentials),
            "python": sys.executable,
            "db": {"host": "127.0.0.1", "port": mysql, "source": "waiter_qa_source",
                   "name": "test_waiter_qa_" + self.mount.replace("-", "_"),
                   "container_id": None, "volume": self.volume, "user": "waiter_qa"},
            "origins": {
                "central": f"http://{host}", "platform": f"http://platform.{host}", "menu": f"http://menu.{host}",
                "organizations": {f"wt2-ci-{key}": f"http://wt2-ci-{key}.{host}" for key in ("a", "b")},
                "api": f"http://127.0.0.1:{api_port}",
            },
        }
        write_private(self.config_path, self.config)

    def invoke(self, command, *extra):
        return subprocess.run(["bash", str(DEV), "qa", command, "--config", str(self.config_path), *extra],
                              cwd=ROOT, capture_output=True, text=True, timeout=440)

    def result(self, command, expected, *extra):
        execution = self.invoke(command, *extra)
        assert execution.returncode == 0, execution.stderr
        payload = json.loads(execution.stdout)
        assert payload["result"] == expected
        assert payload["timings"][-1]["phase"] == "total"
        return payload["receipt"]

    def get(self, origin, path):
        parsed = urlsplit(origin)
        connection = http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=60)
        try:
            connection.request("GET", path, headers={"Host": parsed.netloc})
            response = connection.getresponse()
            return response.status, response.getheaders(), response.read()
        finally:
            connection.close()

    def resources(self):
        return docker("ps", "-aq", "--filter", f"label=io.waiter.qa.mount={self.mount}").splitlines()


@pytest.fixture
def mount(tmp_path):
    fixture = Fixture(tmp_path.resolve())
    try:
        yield fixture
    finally:
        if fixture.receipt_path.exists():
            fixture.invoke("down")
        for container in fixture.resources():
            docker("rm", "--force", container, check=False)
        docker("volume", "rm", fixture.volume, check=False)


def test_identidad_de_django_solo_lee_el_registro_qa_de_desarrollo(monkeypatch):
    # Falla si un proceso ordinario o publicado anuncia la identidad privada de QA.
    monkeypatch.delenv("WAITER_QA_RUNTIME", raising=False)
    assert identity("development", ROOT) is None
    metadata = {"component": "api", "checkout": str(ROOT), "sha": "a" * 40,
                "mount_id": "ci-prueba", "boot_id": "b" * 32}
    monkeypatch.setenv("WAITER_QA_RUNTIME", json.dumps(metadata))
    assert identity("development", ROOT)["boot_id"] == metadata["boot_id"]
    assert identity("production", ROOT) is None
    assert identity("staging", ROOT) is None
    assert identity("development", ROOT.parent) is None


def test_rechaza_https_antes_de_tocar_recursos(mount):
    # Falla si HTTP acredita un mapa HTTPS o si el rechazo deja recursos o recibos nuevos.
    write_private(mount.config_path, config_https(mount.config))
    files_before = set(mount.directory.iterdir())
    for args in (("up", "--prepare"), ("status",)):
        execution = mount.invoke(*args)
        assert execution.returncode == 2
        assert "sólo sirve HTTP" in execution.stderr and "origen HTTPS" in execution.stderr
        assert set(mount.directory.iterdir()) == files_before
        assert not mount.resources()
        assert mount.volume not in docker("volume", "ls", "--format", "{{.Name}}").splitlines()


def test_arranque_reuso_conflicto_parada_y_retoma_propios(mount):
    # Falla si una copia se adopta por puerto, cambia de identidad durante el reuso o la parada pierde el corpus.
    result = mount.invoke("up")
    assert result.returncode == 2 and "preparación explícita" in result.stderr
    with socket.socket() as foreign:
        foreign.bind(("127.0.0.1", mount.ports[0]))
        foreign.listen()
        result = mount.invoke("up", "--prepare")
        assert result.returncode == 2 and "listener" in result.stderr
        assert foreign.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 1
        assert not mount.resources()

    docker("volume", "create", "--label", "io.waiter.qa.mount=otro-titular", mount.volume)
    try:
        result = mount.invoke("up", "--prepare")
        assert result.returncode == 2 and "ya está en uso" in result.stderr
        assert docker("volume", "inspect", "--format", '{{index .Labels "io.waiter.qa.mount"}}', mount.volume) == "otro-titular"
        assert not mount.resources()
        assert not mount.receipt_path.exists()
    finally:
        docker("volume", "rm", mount.volume)

    created = mount.result("up", "creado", "--prepare")
    assert created["state"] == "listo"
    assert [service["runtime"]["sha"] for service in created["services"].values()] == [mount.config["sha"]] * 2
    assert all(service["socket_inode"] and service["listener"]["start_ticks"] for service in created["services"].values())
    data = mount.data_path.read_bytes()
    saved = mount.receipt_path.read_bytes()
    reused = mount.result("up", "reutilizado", "--prepare")
    assert reused["database"] == created["database"] and reused["services"] == created["services"]
    assert mount.result("status", "listo") == reused
    assert mount.data_path.read_bytes() == data and mount.receipt_path.read_bytes() == saved

    origins = mount.config["origins"]
    status, _, landing = mount.get(origins["central"], "/")
    assert status == 200
    assert b"Waiter es un producto de ProjectApp" in landing
    status, _, login = mount.get(origins["central"], "/login")
    assert status == 200
    # La sonda HTTP comprueba el marco de acceso antes de que el navegador hidrate el formulario.
    assert b"Opera m" in login and b"mesas con menos carga." in login
    tenant = origins["organizations"]["wt2-ci-a"]
    status, _, org = mount.get(tenant, "/experience/api/pos/v1/org?org=wt2-ci-a")
    assert status == 200 and json.loads(org)["organization"]["slug"] == "wt2-ci-a"
    assert mount.get(origins["platform"], "/platform")[0] == 200
    status, _, menu = mount.get(origins["menu"], "/manifest.webmanifest")
    assert status == 200 and json.loads(menu)["name"] == "Carta del restaurante"
    foreign_host = f"http://qa-b.waiter.localhost:{mount.ports[0]}"
    assert mount.get(foreign_host, "/__waiter_qa/identity")[0] == 404

    for field, stale in (("sha", "f" * 40), ("boot_id", "f" * 32), ("mount_id", "otro-titular")):
        altered = deepcopy(created)
        altered["services"]["frontend"]["runtime"][field] = stale
        try:
            write_private(mount.receipt_path, altered)
            result = mount.invoke("status")
            assert result.returncode == 2 and "otro SHA, arranque o montaje" in result.stderr
        finally:
            write_private(mount.receipt_path, created)

    with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(75)"], start_new_session=True) as foreign_pid:
        altered = deepcopy(created)
        altered["services"]["frontend"]["leader"]["pid"] = foreign_pid.pid
        try:
            write_private(mount.receipt_path, altered)
            result = mount.invoke("status")
            assert result.returncode == 2 and "otro proceso" in result.stderr
            result = mount.invoke("down")
            assert result.returncode == 2 and "Parada incompleta" in result.stderr
            assert foreign_pid.poll() is None
        finally:
            write_private(mount.receipt_path, created)
            foreign_pid.terminate()

    stopped = mount.result("down", "detenido")
    assert stopped["stop_issues"] == []
    assert mount.result("status", "detenido") == stopped
    container = stopped["database"]["container_id"]
    assert docker("inspect", "--format", "{{.State.Running}}", container) == "false"
    assert docker("inspect", "--format", "{{.HostConfig.RestartPolicy.Name}}", container) == "no"
    assert docker("volume", "inspect", "--format", "{{.Name}}", mount.volume) == mount.volume
    for port in mount.ports:
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0

    resumed = mount.result("up", "reutilizado")
    assert resumed["database"] == created["database"]
    assert all(service["runtime"]["boot_id"] != created["services"][name]["runtime"]["boot_id"]
               for name, service in resumed["services"].items())
    assert mount.data_path.read_bytes() == data

    # Un recibo previo con origen HTTPS aún debe permitir parar los recursos que acredita.
    legacy_config = config_https(resumed["config"])
    write_private(mount.config_path, legacy_config)
    write_private(mount.receipt_path, {**resumed, "config": legacy_config})
    legacy_receipt = mount.receipt_path.read_bytes()
    execution = mount.invoke("status")
    assert execution.returncode == 2 and "origen HTTPS" in execution.stderr
    assert mount.receipt_path.read_bytes() == legacy_receipt
    assert mount.result("down", "detenido")["stop_issues"] == []
    assert docker("inspect", "--format", "{{.State.Running}}", container) == "false"
    assert mount.data_path.read_bytes() == data
