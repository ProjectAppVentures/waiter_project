"""Prepara o verifica un perfil de QA en un MySQL reservado para su copia."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from urllib.parse import urlsplit

sys.dont_write_bytecode = True
from perfil_caja_cerrada import ProfileError, create, inventory, validate_passwords

ROOT = Path(__file__).resolve().parents[2]
SOURCE_DB = "waiter_qa_source"
PROFILE = "caja-cerrada"
MOUNT_LABEL = "io.waiter.qa.mount"
MANIFEST = "perfil-datos.json"
SLUG = r"[a-z0-9]+(?:-[a-z0-9]+)*"
INITIAL_ACCESS = {"sessions": 0, "attendances": 0, "login_passes": 0}


def require(condition, message):
    if not condition:
        raise ProfileError(message)


def matches(value, pattern, limit):
    return isinstance(value, str) and len(value) <= limit and re.fullmatch(pattern, value)


def private_path(value):
    path = Path(value)
    require(path.is_absolute() and path.resolve() == path, "Se esperaba una ruta absoluta y sin enlaces.")
    return path


def private_json(path, *, missing_ok=False):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise ProfileError("Falta un archivo privado requerido por el perfil.") from None
    with os.fdopen(fd, encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
            "El archivo de QA debe ser regular, privado y del operador.",
        )
        try:
            data = json.load(stream)
        except ValueError:
            raise ProfileError("Un archivo privado del perfil no tiene JSON válido.") from None
    require(isinstance(data, dict), "Se esperaba un objeto JSON en un archivo del perfil.")
    return data


def capture(command):
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True, timeout=15).stdout
    except (subprocess.SubprocessError, OSError):
        raise ProfileError(f"No se pudo verificar la identidad mediante {command[0]}.") from None


def validate_origins(origins, namespace):
    require(isinstance(origins, dict), "Falta el mapa de orígenes.")
    try:
        central = urlsplit(origins["central"])
        api = urlsplit(origins["api"])
        host, port = central.hostname, central.port
        valid = (
            central.scheme in ("http", "https")
            and matches(host, r"qa-[a-z0-9]+\.waiter\.localhost", 63)
            and port and origins["central"] == f"{central.scheme}://{host}:{port}"
            and api.scheme == "http" and api.hostname == "127.0.0.1"
            and api.port and origins["api"] == f"http://127.0.0.1:{api.port}"
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        valid = False
    require(valid, "El mapa requiere un origen central QA y un API privado con puerto.")
    base = f"{host}:{port}"
    scheme = central.scheme
    require(
        origins == {
            "central": origins["central"], "api": origins["api"],
            "platform": f"{scheme}://platform.{base}", "menu": f"{scheme}://menu.{base}",
            "organizations": {f"{namespace}-{key}": f"{scheme}://{namespace}-{key}.{base}" for key in ("a", "b")},
        },
        "Los orígenes de organización, plataforma y menú deben compartir el origen central QA.",
    )


def load_config(path):
    raw = private_json(path)
    require(
        type(raw.get("version")) is int and raw["version"] == 1
        and raw.get("profile") == PROFILE
        and type(raw.get("profile_version")) is int and raw["profile_version"] == 1,
        "Versión de configuración o perfil incompatible.",
    )
    require(
        raw.get("environment") == "development" and os.getenv("DJANGO_ENV", "development") == "development",
        "El perfil exige modo development.",
    )
    mount = raw.get("mount_id")
    namespace = raw.get("namespace")
    require(matches(mount, SLUG, 32) and matches(namespace, SLUG, 24), "Montaje o namespace inválido.")
    require(isinstance(raw.get("owner"), str) and raw["owner"].strip(), "Falta el titular del montaje.")
    require(raw.get("checkout") == str(ROOT), "El lanzador del perfil no pertenece al checkout acreditado.")
    git = ["git", "--no-optional-locks", "-C", str(ROOT)]
    head = capture([*git, "rev-parse", "HEAD"]).strip()
    require(raw.get("sha") == head, "El SHA del perfil difiere del checkout.")
    require(
        not capture([*git, "status", "--porcelain", "--untracked-files=no"]),
        "El checkout del perfil tiene cambios en archivos versionados.",
    )
    try:
        state_dir = private_path(raw["state_dir"])
        credentials = private_path(raw["credentials_ref"])
    except (KeyError, TypeError):
        raise ProfileError("Falta la ruta de estado o la referencia protegida de acceso.") from None
    require(credentials.parent == state_dir and credentials.name != MANIFEST,
            "La referencia protegida debe estar dentro del directorio de estado.")
    db = raw.get("db")
    require(
        isinstance(db, dict) and db.keys() == {"host", "port", "source", "name", "container_id", "volume", "user"}
        and db["host"] == "127.0.0.1" and db["source"] == SOURCE_DB
        and db["name"] == "test_waiter_qa_" + mount.replace("-", "_")
        and type(db["port"]) is int and 1 <= db["port"] <= 65535
        and matches(db["container_id"], r"[a-f0-9]{64}", 64)
        and matches(db["volume"], r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", 100)
        and matches(db["user"], r"[a-zA-Z0-9_]+", 32),
        "Se requiere MySQL por loopback con origen y destino de prueba explícitos y recursos identificados.",
    )
    validate_origins(raw.get("origins"), namespace)
    # Los campos operativos del lanzador pueden convivir con el contrato de datos.
    keys = ("version", "environment", "mount_id", "owner", "checkout", "sha", "profile",
            "profile_version", "namespace", "state_dir", "credentials_ref", "db", "origins")
    return {key: raw[key] for key in keys}


@contextmanager
def lock_state(directory, *, write):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        require(info.st_uid == os.geteuid() and not info.st_mode & 0o077,
                "El directorio de estado debe ser privado y del operador.")
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if write else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ProfileError("Otra operación del perfil tiene el montaje bloqueado.") from None
        yield fd
    finally:
        os.close(fd)


def publish(path, data, directory_fd):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
            os.replace(temporary, path)
            os.fsync(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)


def inspect_mysql(config):
    db = config["db"]
    socket = os.getenv("DOCKER_HOST") or "unix:///var/run/docker.sock"
    require(socket.startswith("unix:///"), "Se requiere un socket Docker local.")
    docker = ["docker", "--host", socket]
    container, = json.loads(capture([*docker, "inspect", "--type", "container", db["container_id"]]))
    volume, = json.loads(capture([*docker, "volume", "inspect", db["volume"]]))
    mount_id = config["mount_id"]
    require(
        container["Id"] == db["container_id"] and container["State"]["Running"]
        and (container["Config"]["Labels"] or {}).get(MOUNT_LABEL) == mount_id
        and volume["Name"] == db["volume"] and volume["Driver"] == "local"
        and (volume["Labels"] or {}).get(MOUNT_LABEL) == mount_id,
        "El contenedor o volumen MySQL no pertenece al montaje.",
    )
    mounts = [row for row in container["Mounts"] if row["Destination"] == "/var/lib/mysql"]
    require(
        len(mounts) == 1 and mounts[0]["Type"] == "volume" and mounts[0]["Name"] == volume["Name"]
        and mounts[0]["Source"] == volume["Mountpoint"] and mounts[0]["RW"],
        "La ruta de datos MySQL no usa el volumen acreditado.",
    )
    binding = [{"HostIp": db["host"], "HostPort": str(db["port"])}]
    require(container["NetworkSettings"]["Ports"].get("3306/tcp") == binding,
            "MySQL no escucha exclusivamente en el puerto local acreditado.")
    return {"container_id": container["Id"], "volume": volume["Name"], "volume_created_at": volume["CreatedAt"]}


def configure_django(config, db_password):
    db = config["db"]
    # El comando sólo importa Django después de fijar un entorno propio. No hereda proveedores ni archivos .env.
    os.environ.clear()
    os.environ.update({
        "DJANGO_SETTINGS_MODULE": "experience_project.settings",
        "DJANGO_ENV": "development",
        "DJANGO_DEBUG": "false",
        "PYTHON_DOTENV_DISABLED": "1",
        "DJANGO_DB_ENGINE": "django.db.backends.mysql",
        "DJANGO_DB_NAME": SOURCE_DB,
        "DJANGO_TEST_DB_NAME": db["name"],
        "DB_HOST": db["host"],
        "DB_PORT": str(db["port"]),
        "DB_USER": db["user"],
        "DB_PASSWORD": db_password,
        "DINER_DEMO_ENABLED": "false",
    })
    sys.path.insert(0, str(ROOT / "experience"))
    import django
    from django.conf import settings

    django.setup()
    os.environ.pop("DB_PASSWORD")
    mail = {"BACKEND": "django.core.mail.backends.filebased.EmailBackend",
            "OPTIONS": {"file_path": str(Path(config["state_dir"]) / "mail")}}
    settings.MAILERS = {"default": mail, "waiter": mail}
    settings.MEDIA_ROOT = Path(config["state_dir"]) / "media"


def mysql_identity(connection, name):
    with connection.cursor() as cursor:
        cursor.execute("SELECT DATABASE(), VERSION(), @@server_uuid")
        selected, version, server_uuid = cursor.fetchone()
    require(selected == name and version.startswith("8.4.") and server_uuid,
            "La conexión efectiva no corresponde al MySQL 8.4 acreditado.")
    return server_uuid


def schema_identity(connection):
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    require(not executor.migration_plan(executor.loader.graph.leaf_nodes()),
            "El esquema del corpus tiene migraciones pendientes.")
    applied = sorted(executor.loader.applied_migrations)
    return hashlib.sha256(json.dumps(applied).encode()).hexdigest()


def create_schema(connection, name):
    # Reserva atómica. CREATE falla si apareció una base entre el inventario y la preparación.
    with connection.cursor() as cursor:
        quoted_name = connection.ops.quote_name(name)
        cursor.execute(f"CREATE DATABASE {quoted_name} CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
    # El runner de Django prepara la base reservada. keepdb impide que borre un esquema.
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        connection.creation.create_test_db(verbosity=0, autoclobber=False, keepdb=True)


def run_profile(config, command):
    directory = Path(config["state_dir"])
    path = directory / MANIFEST
    with lock_state(directory, write=command == "prepare") as fd:
        saved = private_json(path, missing_ok=True)
        if saved is None:
            require(command == "prepare", "El montaje aún no tiene un manifiesto de datos.")
        else:
            require(saved.get("state") == "listo", "Hay una preparación parcial. Se conserva el corpus.")
            require(saved.keys() == {"version", "state", "config", "database",
                                     "schema_version", "inventory", "initial_access"}
                    and saved.get("version") == 1 and saved.get("config") == config
                    and saved.get("initial_access") == INITIAL_ACCESS,
                    "La configuración o versión difiere del manifiesto.")

        identity = inspect_mysql(config)
        credentials = private_json(Path(config["credentials_ref"]))
        require(isinstance(credentials.get("db_password"), str) and credentials["db_password"],
                "Falta la referencia válida de acceso a la base.")
        configure_django(config, credentials["db_password"])
        from django.db import connection, transaction

        passwords = credentials.get("accounts")
        require(validate_passwords(passwords), "Faltan credenciales ficticias válidas para los actores del perfil.")
        identity["server_uuid"] = mysql_identity(connection, SOURCE_DB)
        name = config["db"]["name"]
        if saved is not None:
            require(saved.get("database") == identity, "La base efectiva difiere de la base del manifiesto.")
            connection.close()
            connection.settings_dict["NAME"] = name
            require(mysql_identity(connection, name) == identity["server_uuid"], "Cambió el servidor MySQL.")
            require(saved.get("schema_version") == schema_identity(connection), "Cambió el esquema del perfil.")
            require(saved.get("inventory") == inventory(config, passwords), "El estado del perfil difiere del manifiesto.")
            return ("reutilizado" if command == "prepare" else "verificado"), saved

        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s", [name])
            require(cursor.fetchone() is None, "El destino ya existe sin un manifiesto válido.")
        publish(path, {"version": 1, "state": "preparando", "config": config, "database": identity}, fd)
        create_schema(connection, name)
        require(mysql_identity(connection, name) == identity["server_uuid"], "Cambió el servidor MySQL.")
        schema = schema_identity(connection)
        with transaction.atomic():
            create(config, passwords)
            data = inventory(config, passwords, initial=True)
        manifest = {"version": 1, "state": "listo", "config": config, "database": identity,
                    "schema_version": schema, "inventory": data, "initial_access": INITIAL_ACCESS}
        publish(path, manifest, fd)
        return "creado", manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "verify"))
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        result, manifest = run_profile(config, args.command)
    except ProfileError as exc:
        print(f"Perfil QA: {exc}", file=sys.stderr)
        return 2
    except (Exception, SystemExit) as exc:
        # Las excepciones del driver o del entorno pueden incluir material de acceso.
        error = exc.__class__.__name__
        print(f"Perfil QA: falló {args.command} ({error}). Revisa la base y el estado privado del montaje.",
              file=sys.stderr)
        return 1
    print(json.dumps({"result": result, "manifest": manifest}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
