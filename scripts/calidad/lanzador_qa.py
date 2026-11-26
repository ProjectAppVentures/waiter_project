"""Arranca, verifica y detiene una copia QA con recibo de pertenencia."""

import argparse
import fcntl
import json
import os
import secrets
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

sys.dont_write_bytecode = True
from perfil_caja_cerrada import validate_passwords
from recursos_qa import (assert_identity, await_service, http_json, launch, now, port_occupied, stop_service,
                         verify_service)
from seed_perfil_qa import (MANIFEST, MOUNT_LABEL, ROOT, ProfileError, capture, docker_command, inspect_mysql,
                           mysql_container, private_json, publish, require, validate_config)


RECEIPT = "montaje-qa.json"
RESOLVED = "perfil-config.json"
PROVISION_LABEL = "io.waiter.qa.provision"


@contextmanager
def locked(directory, *, write):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        require(info.st_uid == os.geteuid() and not info.st_mode & 0o077,
                "El directorio del montaje debe ser privado y del titular.")
        flags = (os.O_RDWR | os.O_CREAT) if write else os.O_RDONLY
        try:
            lock = os.open(directory / "lanzador.lock", flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        except FileNotFoundError:
            raise ProfileError("El montaje aún no tiene un recibo de arranque.") from None
        try:
            info = os.fstat(lock)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
                    "El bloqueo del montaje debe ser privado y del titular.")
            try:
                fcntl.flock(lock, (fcntl.LOCK_EX if write else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ProfileError("Otra operación tiene bloqueado el montaje.") from None
            yield fd
        finally:
            os.close(lock)
    finally:
        os.close(fd)


def private_storage(path):
    require(path.resolve() == path, "El almacenamiento del montaje no admite enlaces.")
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
            "Los logs, medios y correo deben estar en directorios privados del montaje.")


class Mount:
    def __init__(self, config, directory_fd, source):
        self.config = config
        self.source = source
        self.directory = Path(config["state_dir"])
        self.directory_fd = directory_fd
        self.docker = docker_command()
        self.front = ROOT / "frontend"
        self.api = ROOT / "experience"
        self.ports = {"frontend": urlsplit(config["origins"]["central"]).port,
                      "api": urlsplit(config["origins"]["api"]).port, "mysql": config["db"]["port"]}
        require(len(set(self.ports.values())) == 3, "Next, API y MySQL requieren puertos distintos.")
        self.container_name = f"waiter-qa-{config['mount_id']}"
        self.receipt = private_json(self.directory / RECEIPT, missing_ok=True)
        self.timings = []
        self.record_operation = False
        if self.receipt is not None:
            require(self.receipt.get("version") == 1 and self.receipt.get("config") == config,
                    "El recibo pertenece a otra configuración o montaje.")

    def save(self):
        publish(self.directory / RECEIPT, self.receipt, self.directory_fd)

    def execute(self, action, prepare):
        try:
            with self.phase("total"):
                return self.up(prepare) if action == "up" else getattr(self, action)()
        finally:
            if self.record_operation:
                self.receipt["last_operation"] = self.timings.copy()
                self.save()

    @contextmanager
    def phase(self, name):
        started_at, started = now(), time.monotonic()
        try:
            yield
        finally:
            self.timings.append({"phase": name, "started_at": started_at, "finished_at": now(),
                                 "seconds": round(time.monotonic() - started, 3)})

    def new_receipt(self):
        self.receipt = {
            "version": 1, "state": "preparando", "config": self.config,
            "branch": capture(["git", "-C", str(ROOT), "rev-parse", "--abbrev-ref", "HEAD"]).strip(),
            "ports": self.ports, "database": None, "services": {},
            "profile_config": str(self.directory / RESOLVED), "profile_manifest": str(self.directory / MANIFEST),
            "storage": {"logs": str(self.directory / "logs"), "media": str(self.directory / "media"),
                        "mail": str(self.directory / "mail"), "api_cache": "locmem",
                        "next_cache": str(self.front / ".next")},
        }
        self.save()

    def ensure_free(self, keys):
        for key in keys:
            require(not port_occupied(self.ports[key]), f"El puerto de {key} ya tiene un listener.")

    def check_frontend(self):
        for name in (".env", ".env.local", ".env.development", ".env.development.local"):
            path = self.front / name
            require(not path.exists() and not path.is_symlink(), "El frontend QA requiere un checkout sin .env heredado.")
        require(not (self.front / ".next").is_symlink(), "La caché de Next debe pertenecer a la copia.")

    def credentials(self):
        data = private_json(Path(self.config["credentials_ref"]))
        require(isinstance(data.get("db_password"), str) and data["db_password"],
                "Falta la referencia válida de acceso a MySQL.")
        require(validate_passwords(data.get("accounts")), "Faltan credenciales ficticias del perfil.")
        return data["db_password"]

    def check_new_database(self):
        volume = self.config["db"]["volume"]
        containers = capture([*self.docker, "ps", "-a", "--format", "{{.Names}}"]).splitlines()
        volumes = capture([*self.docker, "volume", "ls", "--format", "{{.Name}}"]).splitlines()
        require(self.container_name not in containers and volume not in volumes,
                "El nombre de contenedor o volumen ya está en uso. Se conserva el recurso existente.")

    def resolve_config(self, container_id):
        return {**self.config, "db": {**self.config["db"], "container_id": container_id}}

    def register_db(self, config):
        container, volume = mysql_container(config)
        require(container["HostConfig"]["RestartPolicy"]["Name"] == "no",
                "El MySQL de QA debe estar configurado sin reinicio automático.")
        self.receipt["database"] = {"container_id": container["Id"], "volume": volume["Name"],
                                    "volume_created_at": volume["CreatedAt"]}
        self.save()

    def database(self):
        require(self.receipt.get("database"), "Falta el recibo de pertenencia de MySQL.")
        saved = self.receipt["database"]
        config = self.resolve_config(saved["container_id"])
        container, volume = mysql_container(config)
        require(volume["CreatedAt"] == saved["volume_created_at"] and volume["Name"] == saved["volume"]
                and container["HostConfig"]["RestartPolicy"]["Name"] == "no",
                "El volumen o la política de reinicio difiere del montaje acreditado.")
        if container["State"]["Running"]:
            inspect_mysql(config)
        return config, container

    def wait_mysql(self, container_id):
        command = [*self.docker, "exec", container_id, "mysqladmin", "--protocol=tcp",
                   "--host=127.0.0.1", "ping", "--silent"]
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            except subprocess.TimeoutExpired:
                pass
            else:
                if result.returncode == 0:
                    return
            time.sleep(0.5)
        raise ProfileError("Se agotó la espera de disponibilidad de MySQL.")

    def create_database(self, password):
        db = self.config["db"]
        label = f"{MOUNT_LABEL}={self.config['mount_id']}"
        provision = secrets.token_hex(16)
        reservation = f"{PROVISION_LABEL}={provision}"
        capture([*self.docker, "volume", "create", "--label", label, "--label", reservation, db["volume"]])
        volume, = json.loads(capture([*self.docker, "volume", "inspect", db["volume"]]))
        require((volume["Labels"] or {}).get(PROVISION_LABEL) == provision,
                "Otro proceso reservó el volumen. Se conserva intacto.")
        self.ensure_free(("mysql",))
        root_password = password if db["user"] == "root" else secrets.token_urlsafe(32)
        variables = {"MYSQL_DATABASE": db["source"], "MYSQL_ROOT_PASSWORD": root_password}
        if db["user"] == "root":
            variables["MYSQL_ROOT_HOST"] = "%"
        else:
            variables.update({"MYSQL_USER": db["user"], "MYSQL_PASSWORD": password})
        env_args = [flag for key in variables for flag in ("--env", key)]
        container_id = capture([
            *self.docker, "run", "--detach", "--restart=no", "--name", self.container_name,
            "--label", label, "--label", reservation,
            "--mount", f"type=volume,source={db['volume']},target=/var/lib/mysql",
            "--publish", f"127.0.0.1:{db['port']}:3306", *env_args,
            "mysql:8.4", "--character-set-server=utf8mb4", "--collation-server=utf8mb4_0900_ai_ci",
        ], env={**os.environ, **variables}, timeout=180).strip()
        config = self.resolve_config(container_id)
        self.register_db(config)
        return config, root_password

    def grant_profile_database(self, config, root_password):
        db = config["db"]
        if db["user"] != "root":
            schema = db["name"].replace("_", "\\_")
            grant = f"GRANT ALL ON `{schema}`.* TO '{db['user']}'@'%'"
            capture([*self.docker, "exec", "--env", "MYSQL_PWD", db["container_id"],
                     "mysql", "--user=root", "--execute", grant],
                    env={**os.environ, "MYSQL_PWD": root_password})

    def data_config(self, config, *, register=False):
        data = {key: value for key, value in config.items() if key != "python"}
        path = self.directory / RESOLVED
        existing = private_json(path, missing_ok=True)
        if existing is None and register:
            publish(path, data, self.directory_fd)
        else:
            require(existing == data, "La referencia del perfil difiere del montaje acreditado.")
        return path

    def profile(self, config, *, prepare=False, register=False, external=False):
        path = self.source if external else self.data_config(config, register=register)
        command = "prepare" if prepare else "verify"
        execution = subprocess.run(
            [config["python"], str(ROOT / "scripts/calidad/seed_perfil_qa.py"), command, "--config", str(path)],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
        if execution.returncode:
            diagnostic = next((line for line in execution.stderr.splitlines() if line.startswith("Perfil QA: ")), "")
            raise ProfileError(diagnostic or "El perfil de datos devolvió un conflicto.")
        data = json.loads(execution.stdout)
        require(data["result"] == ("creado" if prepare else "verificado")
                and data["manifest"]["config"] == {key: value for key, value in config.items() if key != "python"},
                "El manifiesto de datos no acredita el perfil esperado.")
        return data["manifest"]

    def services(self, config, password):
        origins = config["origins"]
        public = [origins[key] for key in ("central", "platform", "menu")] + list(origins["organizations"].values())
        db = config["db"]
        system = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TZ") if key in os.environ}
        api_env = {
            **system, "DJANGO_SETTINGS_MODULE": "experience_project.settings", "DJANGO_ENV": "development",
            "DJANGO_DEBUG": "false", "PYTHON_DOTENV_DISABLED": "1", "WAITER_QA_STATE_DIR": str(self.directory),
            "DJANGO_DB_ENGINE": "django.db.backends.mysql", "DJANGO_DB_NAME": db["name"],
            "DB_HOST": db["host"], "DB_PORT": str(db["port"]), "DB_USER": db["user"], "DB_PASSWORD": password,
            "POS_URL": origins["central"], "PLATFORM_URL": origins["platform"], "DINER_PUBLIC_URL": origins["menu"],
            "DJANGO_CORS_ALLOWED_ORIGINS": ",".join(public),
            "DJANGO_ALLOWED_HOSTS": ",".join(["127.0.0.1", "localhost", *[urlsplit(url).hostname for url in public]]),
            "EMAIL_BACKEND": "django.core.mail.backends.filebased.EmailBackend",
            "DINER_DEMO_ENABLED": "false", "DINER_EMAIL_ENABLED": "false",
        }
        front_env = {
            **system, "NODE_ENV": "development", "NEXT_TELEMETRY_DISABLED": "1",
            "NEXT_PUBLIC_WAITER_QA_CENTRAL_HOST": urlsplit(origins["central"]).hostname,
            "EXPERIENCE_ORIGIN": origins["api"],
        }
        versions = (
            ("api", [config["python"], "manage.py", "runserver", f"127.0.0.1:{self.ports['api']}", "--noreload"],
             self.api, api_env),
            ("frontend", [str(self.front / "node_modules/.bin/next"), "dev", "--hostname", "127.0.0.1",
                          "--port", str(self.ports["frontend"])], self.front, front_env),
        )
        for name, argv, cwd, env in versions:
            with self.phase(f"arranque_{name}"):
                service = launch(config, name, self.ports[name], argv, cwd, self.directory / "logs" / f"{name}.log", env)
                self.receipt["services"][name] = service
                self.save()
            with self.phase(f"espera_{name}"):
                await_service(service, origins)
                self.save()
        with self.phase("disponibilidad"):
            self.check_proxy()

    def check_proxy(self):
        origins = self.config["origins"]
        port = self.ports["frontend"]
        api = self.receipt["services"]["api"]
        for path, origin in (("/experience/api/health/", origins["central"]), ("/api/health/", origins["menu"])):
            payload, cache = http_json(port, path, origin=origin)
            assert_identity(payload.get("qa"), cache, api)
        org_path = "/experience/api/pos/v1/org"
        for slug, origin in origins["organizations"].items():
            for path, headers in ((org_path, {"X-Waiter-Org": slug}), (f"{org_path}?org={slug}", None)):
                payload, _ = http_json(port, path, origin=origin, headers=headers)
                require(payload.get("organization", {}).get("slug") == slug,
                        "El proxy no resolvió la organización del perfil.")

    def registered_service(self, name):
        service = self.receipt["services"][name]
        runtime = service["runtime"]
        cwd = self.front if name == "frontend" else self.api
        require(runtime.get("component") == name and service["cwd"] == str(cwd)
                and service["port"] == self.ports[name]
                and all(runtime.get(key) == self.config[key] for key in ("sha", "checkout", "mount_id")),
                f"El recibo de {name} acredita otro SHA, arranque o montaje.")
        return service

    def verify_running(self):
        config, container = self.database()
        require(container["State"]["Running"] and self.receipt["services"].keys() == {"api", "frontend"},
                "El recibo tiene servicios incompletos. Conserva el montaje para su diagnóstico.")
        with self.phase("verificacion_datos"):
            self.profile(config)
        with self.phase("disponibilidad"):
            for name in ("api", "frontend"):
                verify_service(self.registered_service(name), config["origins"])
            self.check_proxy()
        validate_config(self.config, unassigned_container=True)

    def up(self, prepare):
        self.check_frontend()
        if self.receipt and self.receipt["state"] == "listo":
            self.verify_running()
            return "reutilizado"
        deferred = self.receipt is not None
        require(not deferred or self.receipt["state"] == "detenido",
                "La copia tiene una operación parcial. Se conservan los recursos y el corpus.")
        self.ensure_free(("api", "frontend"))
        require(os.access(self.config["python"], os.X_OK) and (self.front / "node_modules/.bin/next").is_file(),
                "Faltan las dependencias de Python o Next de la copia.")
        password = self.credentials()
        new_profile = not deferred and self.config["db"]["container_id"] is None
        if new_profile:
            require(prepare, "El perfil aún no existe. Se requiere una preparación explícita.")
            require(not (self.directory / MANIFEST).exists() and not (self.directory / RESOLVED).exists(),
                    "Ya hay archivos de perfil. Se conservan para su diagnóstico.")
            self.check_new_database()
            self.ensure_free(("mysql",))
        elif deferred:
            config, container = self.database()
            require(not container["State"]["Running"], "El MySQL del montaje ya está activo fuera de su recibo.")
            self.ensure_free(("mysql",))
        else:
            config = self.resolve_config(self.config["db"]["container_id"])
            inspect_mysql(config)
            with self.phase("verificacion_datos"):
                self.profile(config, external=True)
        for name in ("logs", "media", "mail"):
            private_storage(self.directory / name)
        if deferred:
            self.receipt.update(state="preparando", services={})
            self.save()
        else:
            self.new_receipt()
        self.record_operation = True
        try:
            with self.phase("arranque_mysql"):
                if new_profile:
                    config, root_password = self.create_database(password)
                elif deferred:
                    capture([*self.docker, "start", config["db"]["container_id"]], timeout=60)
                else:
                    self.register_db(config)
            if new_profile or deferred:
                with self.phase("espera_mysql"):
                    self.wait_mysql(config["db"]["container_id"])
                with self.phase("preparacion_datos" if new_profile else "verificacion_datos"):
                    if new_profile:
                        self.grant_profile_database(config, root_password)
                    self.profile(config, prepare=new_profile, register=new_profile)
            else:
                self.data_config(config, register=True)
            self.services(config, password)
            validate_config(self.config, unassigned_container=True)
        except (Exception, KeyboardInterrupt):
            self.receipt["state"] = "incompleto"
            raise
        self.receipt["state"] = "listo"
        return "creado" if not deferred else "reutilizado"

    def status(self):
        require(self.receipt is not None, "El montaje aún no tiene un recibo operativo.")
        if self.receipt["state"] == "detenido":
            _, container = self.database()
            require(not container["State"]["Running"], "El MySQL del montaje está activo fuera de su recibo.")
            self.ensure_free(self.ports)
            return "detenido"
        require(self.receipt["state"] == "listo", "El montaje está incompleto. Se conserva su último recibo.")
        self.check_frontend()
        self.verify_running()
        return "listo"

    def down(self):
        require(self.receipt is not None, "El montaje aún no tiene un recibo operativo.")
        self.receipt["state"] = "deteniendo"
        self.save()
        self.record_operation = True
        errors = []
        for name in ("frontend", "api"):
            try:
                with self.phase(f"parada_{name}"):
                    if name in self.receipt["services"]:
                        stop_service(self.registered_service(name))
                    else:
                        self.ensure_free((name,))
            except ProfileError as exc:
                errors.append(str(exc))
        try:
            with self.phase("parada_mysql"):
                if self.receipt.get("database"):
                    config, container = self.database()
                    if container["State"]["Running"]:
                        capture([*self.docker, "stop", "--time", "15", config["db"]["container_id"]], timeout=30)
                    _, stopped = self.database()
                    require(not stopped["State"]["Running"], "El MySQL propio sigue activo.")
                else:
                    self.check_new_database()
                self.ensure_free(("mysql",))
        except ProfileError as exc:
            errors.append(str(exc))
        self.receipt.update(state="incompleto" if errors else "detenido", stopped_at=now(), stop_issues=errors)
        require(not errors, "Parada incompleta. " + " ".join(errors))
        return "detenido"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("up", "status", "down"))
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    try:
        require(not args.prepare or args.action == "up", "Sólo up admite --prepare.")
        raw = private_json(args.config)
        config = validate_config(raw, check_checkout=args.action != "down", unassigned_container=True)
        python = Path(raw.get("python", str(ROOT / "experience/venv/bin/python")))
        require(python.is_absolute() and Path(os.path.abspath(python)) == python,
                "Se requiere una ruta absoluta para el intérprete Python de la copia.")
        # Invocar el venv por su enlace de entrada conserva su instalación aislada.
        config["python"] = str(python)
        with locked(Path(config["state_dir"]), write=args.action != "status") as fd:
            mount = Mount(config, fd, args.config.absolute())
            result = mount.execute(args.action, args.prepare)
        print(json.dumps({"result": result, "receipt": mount.receipt, "timings": mount.timings}, ensure_ascii=False))
        return 0
    except ProfileError as exc:
        print(f"Montaje QA: {exc}", file=sys.stderr)
        return 2
    except (Exception, KeyboardInterrupt) as exc:
        print(f"Montaje QA: falló {args.action} ({type(exc).__name__}). Revisa el recibo y los logs privados.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
