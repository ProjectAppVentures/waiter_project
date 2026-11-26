"""Pertenencia de procesos y lecturas HTTP para el lanzador QA en Linux."""

import hashlib
import http.client
import json
import os
import signal
import stat
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from seed_perfil_qa import ProfileError, require


PROC = Path("/proc")
IDENTITY_PATH = "/__waiter_qa/identity"


def now():
    return datetime.now(timezone.utc).isoformat()


def host_boot():
    return (PROC / "sys/kernel/random/boot_id").read_text().strip()


def process(pid):
    try:
        directory = PROC / str(pid)
        fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return {"pid": pid, "group": int(fields[2]), "session": int(fields[3]), "start_ticks": int(fields[19]),
                "uid": directory.stat().st_uid, "cwd": os.readlink(directory / "cwd")}
    except (OSError, ValueError, IndexError):
        return None


def listening(port):
    sockets = {}
    for table in ("tcp", "tcp6"):
        path = PROC / "net" / table
        if table == "tcp6" and not path.exists():
            continue
        for line in path.read_text().splitlines()[1:]:
            fields = line.split()
            address, number = fields[1].split(":")
            if fields[3] == "0A" and int(number, 16) == port:
                sockets[fields[9]] = address
    return sockets


def owned_members(service):
    if service["host_boot"] != host_boot():
        return []
    anchors = (service["leader"], service.get("listener"))
    anchor = next((row for row in anchors if row and row["uid"] == os.geteuid() and process(row["pid"]) == row), None)
    if not anchor:
        return []
    members = (process(int(entry.name)) for entry in PROC.iterdir() if entry.name.isdecimal())
    return [row for row in members if row and row["uid"] == anchor["uid"] and row["group"] == anchor["group"]
            and row["session"] == anchor["session"]]


def service_listener(service):
    sockets = listening(service["port"])
    if not sockets:
        return None
    component = service["runtime"]["component"]
    require(list(sockets.values()) == ["0100007F"], f"El listener de {component} no está aislado en loopback.")
    inode = next(iter(sockets))
    for member in owned_members(service):
        directory = PROC / str(member["pid"]) / "fd"
        try:
            fd_names = list(directory.iterdir())
        except OSError:
            continue
        for fd in fd_names:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target == f"socket:[{inode}]" and member["cwd"] == service["cwd"] and process(member["pid"]) == member:
                return member, inode
    raise ProfileError(f"El listener de {component} pertenece a otro proceso.")


def launch(config, component, port, command, cwd, log, env):
    require(not listening(port), f"Puerto de {component} ocupado por otro listener.")
    runtime = {"component": component, "sha": config["sha"], "checkout": config["checkout"],
               "mount_id": config["mount_id"], "boot_id": uuid4().hex}
    started = now()
    fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "ab") as output:
        info = os.fstat(output.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
                "El log debe ser privado y pertenecer al montaje.")
        child = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                 env={**env, "WAITER_QA_RUNTIME": json.dumps(runtime)}, start_new_session=True)
    leader = process(child.pid)
    require(leader and leader["group"] == child.pid and leader["session"] == child.pid and leader["cwd"] == str(cwd),
            f"{component} terminó antes de acreditar su proceso de arranque.")
    return {"port": port, "cwd": str(cwd), "log": str(log), "host_boot": host_boot(), "started_at": started,
            "runtime": runtime, "leader": leader}


def http_json(port, path, *, origin=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    request_headers = {} if origin is None else {"Host": urlsplit(origin).netloc}
    try:
        connection.request("GET", path, headers={**request_headers, **(headers or {}), "Cache-Control": "no-cache"})
        response = connection.getresponse()
        require(response.status == 200, f"La sonda {path.split('?')[0]} respondió HTTP {response.status}.")
        return json.loads(response.read(1024 * 1024)), response.getheader("Cache-Control", "")
    except (UnicodeError, ValueError, http.client.HTTPException):
        raise ProfileError("La sonda recibió una respuesta inválida.") from None
    finally:
        connection.close()


def expected_identity(service):
    identity = service["runtime"].copy()
    checkout = identity.pop("checkout")
    return {**identity, "checkout_digest": hashlib.sha256(checkout.encode()).hexdigest()}


def assert_identity(payload, cache_control, service):
    component = service["runtime"]["component"]
    require(payload == expected_identity(service) and "no-store" in cache_control,
            f"La respuesta de {component} acredita otro SHA, arranque o montaje.")


def verify_http(service, origins):
    port = service["port"]
    if service["runtime"]["component"] == "api":
        payload, cache = http_json(port, "/api/health/")
        assert_identity(payload.get("qa"), cache, service)
        return
    public = [origins[key] for key in ("central", "platform", "menu")] + list(origins["organizations"].values())
    for origin in public:
        payload, cache = http_json(port, IDENTITY_PATH, origin=origin)
        assert_identity(payload, cache, service)


def await_service(service, origins):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        require(owned_members(service), f"Terminó {service['runtime']['component']}. Revisa su log privado.")
        listener = service_listener(service)
        if listener:
            try:
                verify_http(service, origins)
            except OSError:
                pass
            else:
                require(listener == service_listener(service), "El listener cambió durante la comprobación HTTP.")
                service["listener"], service["socket_inode"] = listener
                return
        time.sleep(0.5)
    raise ProfileError(f"Se agotó la espera de disponibilidad de {service['runtime']['component']}.")


def verify_service(service, origins):
    check_anchors(service)
    require(service_listener(service) == (service.get("listener"), service.get("socket_inode")),
            f"El PID, el instante de arranque o el socket de {service['runtime']['component']} cambió.")
    verify_http(service, origins)
    require(service_listener(service) == (service["listener"], service["socket_inode"]),
            "El listener cambió durante la comprobación HTTP.")


def check_anchors(service):
    component = service["runtime"]["component"]
    if service["host_boot"] == host_boot():
        for anchor in (service["leader"], service.get("listener")):
            current = process(anchor["pid"]) if anchor else None
            require(not current or current == anchor, f"El PID registrado de {component} pertenece a otro proceso.")


def stop_service(service):
    component = service["runtime"]["component"]
    check_anchors(service)
    members = owned_members(service)
    for member in members:
        try:
            # El descriptor fija la identidad aunque el PID se recicle antes de enviar la señal.
            pid_fd = os.pidfd_open(member["pid"])
        except ProcessLookupError:
            continue
        try:
            if process(member["pid"]) == member:
                signal.pidfd_send_signal(pid_fd, signal.SIGTERM)
        except ProcessLookupError:
            pass
        finally:
            os.close(pid_fd)
    def still_running():
        return any(process(member["pid"]) == member for member in members) or owned_members(service)

    deadline = time.monotonic() + 15
    while still_running() and time.monotonic() < deadline:
        time.sleep(0.25)
    require(not still_running() and not listening(service["port"]),
            f"{component} sigue activo o su puerto tiene un listener ajeno.")
