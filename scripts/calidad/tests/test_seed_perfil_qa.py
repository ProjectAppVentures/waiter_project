"""Comprobaciones de la receta con contenedor y volumen desechables de CI."""

import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path

import MySQLdb
import pytest


ROOT = Path(__file__).resolve().parents[3]
SEED = ROOT / "scripts/calidad/seed_perfil_qa.py"
ACTORS = ("dueno", "encargado", "cajero", "mesero", "mesero.a2", "mesero.b1")


def docker(*args, env=None, check=True):
    return subprocess.run(["docker", *args], env=env, check=check, text=True, capture_output=True, timeout=180)


def write_private(path, data):
    path.touch(mode=0o600)
    path.write_text(json.dumps(data), encoding="utf-8")


class Target:
    def __init__(self, directory, config, db_password):
        self.config_path = directory / "config.json"
        self.manifest_path = directory / "perfil-datos.json"
        self.config = config
        self.db_password = db_password
        write_private(self.config_path, config)

    def invoke(self, command="prepare", *, config=None, environment="development"):
        write_private(self.config_path, self.config if config is None else config)
        return subprocess.run(
            [sys.executable, str(SEED), command, "--config", str(self.config_path)],
            cwd=ROOT, env={**os.environ, "DJANGO_ENV": environment},
            capture_output=True, text=True, timeout=120,
        )

    def sql(self, query, params=(), *, source=False):
        db = self.config["db"]
        database = db["source"] if source else db["name"]
        connection = MySQLdb.connect(host=db["host"], port=db["port"], user=db["user"],
                                    passwd=self.db_password, db=database, connect_timeout=2, autocommit=True)
        try:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                return cursor.fetchall()
        finally:
            connection.close()

    def schema_exists(self, name=None):
        return bool(self.sql("SELECT 1 FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s",
                             [name or self.config["db"]["name"]], source=True))


@pytest.fixture
def target(tmp_path):
    directory = tmp_path.resolve()
    directory.chmod(0o700)
    mount = f"ci-{secrets.token_hex(5)}"
    namespace = "perfil-ci"
    label = f"io.waiter.qa.mount={mount}"
    volume = f"waiter-qa-{mount}"
    source = "waiter_qa_source"
    db_password = secrets.token_urlsafe(32)
    env = {**os.environ, "MYSQL_ROOT_PASSWORD": db_password}
    docker("volume", "create", "--label", label, volume)
    container = None
    try:
        container = docker(
            "run", "--detach", "--rm", "--name", volume, "--label", label,
            "--mount", f"type=volume,source={volume},target=/var/lib/mysql",
            "--publish", "127.0.0.1::3306", "--env", "MYSQL_ROOT_PASSWORD",
            "--env", "MYSQL_ROOT_HOST=%", "--env", f"MYSQL_DATABASE={source}", "mysql:8.4",
            env=env,
        ).stdout.strip()
        port = int(docker("port", container, "3306/tcp").stdout.strip().rsplit(":", 1)[1])
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        credentials = directory / "accesos.json"
        write_private(credentials, {"db_password": db_password,
                                    "accounts": {actor: secrets.token_urlsafe(20) for actor in ACTORS}})
        host = "qa-a.waiter.localhost:3100"
        config = {
            "version": 1, "environment": "development", "mount_id": mount, "owner": "CI",
            "checkout": str(ROOT), "sha": sha, "profile": "caja-cerrada", "profile_version": 1,
            "namespace": namespace, "state_dir": str(directory), "credentials_ref": str(credentials),
            "db": {"host": "127.0.0.1", "port": port, "source": source,
                   "name": f"test_waiter_qa_{mount.replace('-', '_')}", "container_id": container,
                   "volume": volume, "user": "root"},
            "origins": {
                "central": f"http://{host}", "platform": f"http://platform.{host}", "menu": f"http://menu.{host}",
                "organizations": {f"{namespace}-{key}": f"http://{namespace}-{key}.{host}" for key in ("a", "b")},
                "api": "http://127.0.0.1:8100",
            },
        }
        instance = Target(directory, config, db_password)
        deadline = time.monotonic() + 90
        while True:
            try:
                instance.sql("SELECT 1", source=True)
                break
            except MySQLdb.OperationalError:
                if time.monotonic() >= deadline:
                    pytest.fail("MySQL de CI no quedó disponible", pytrace=False)
                time.sleep(1)
        yield instance
    finally:
        if container:
            docker("rm", "--force", container, check=False)
        docker("volume", "rm", volume, check=False)


def receipt(result, expected):
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["result"] == expected
    return data["manifest"]


def private_snapshot(target):
    # El contenido sensible sólo se compara en memoria dentro de la prueba.
    queries = (
        "SELECT id, password, last_login FROM accounts_account ORDER BY id",
        "SELECT id, token_hash, restaurant_id, expires FROM accounts_session ORDER BY id",
        "SELECT id, restaurant_id, check_in, check_out FROM accounts_attendance ORDER BY id",
        "SELECT id, token_hash FROM accounts_loginpass ORDER BY id",
        "SELECT id, token FROM tables_table ORDER BY id",
    )
    evidence = tuple(target.sql(query) for query in queries)
    return hashlib.sha256(repr(evidence).encode()).digest()


def test_crea_y_reutiliza_la_misma_copia_sin_rotar_accesos(target):
    # Falla si la receta prepara caja abierta, mezcla sedes o un reuso cambia el corpus o rota accesos.
    assert not target.schema_exists()
    manifest = receipt(target.invoke(), "creado")
    inventory = manifest["inventory"]
    a1, a2, b1 = inventory["restaurants"]
    assert a1["organization_id"] == a2["organization_id"] != b1["organization_id"]
    assert all(site["active"] and site["cash"] == "cerrada" for site in (a1, a2, b1))
    actors = inventory["actors"]
    expected = {
        "dueno": ("owner", []), "encargado": ("admin", [a2["id"]]), "cajero": ("cashier", [a1["id"]]),
        "mesero": ("waiter", [a1["id"]]), "mesero.a2": ("waiter", [a2["id"]]),
        "mesero.b1": ("waiter", [b1["id"]]),
    }
    assert {key: (actor["role"], actor["assigned"]) for key, actor in actors.items()} == expected
    assert all(actor["active"] and actor["activated"] and actor["schedule"] == [None, None] for actor in actors.values())
    assert set(actors["dueno"]["permissions"]) == {str(a1["id"]), str(a2["id"])}
    assert set(actors["mesero"]["permissions"]) == {str(a1["id"])}
    assert actors["mesero"]["permissions"][str(a1["id"])]["views"] == ["tables"]
    assert inventory["m01"] == {"actor": "mesero", "restaurant_id": a1["id"], "path": "/caja"}
    assert inventory["qr"]["restaurant_id"] == a1["id"]
    assert "{token}" in inventory["qr"]["url_template"]

    for table in ("sales_cashshift", "sales_cashmove", "sales_order", "sales_payment", "sales_course",
                  "notifications_notification", "accounts_loginpass", "accounts_session", "accounts_attendance"):
        assert target.sql(f"SELECT COUNT(*) FROM {table}") == ((0,),)
    assert not (target.manifest_path.parent / "mail").exists()

    private_before = private_snapshot(target)
    assert receipt(target.invoke(), "reutilizado") == manifest
    unchanged = private_snapshot(target) == private_before
    assert unchanged, "El primer reuso cambió una clave o un registro de acceso"

    # Un acceso posterior legítimo es independiente de la apertura de caja.
    actor, site = actors["mesero"]["id"], a1["id"]
    target.sql("INSERT INTO accounts_session (account_id, restaurant_id, token_hash, expires, created_at) "
               "VALUES (%s, %s, %s, UTC_TIMESTAMP(6) + INTERVAL 1 HOUR, UTC_TIMESTAMP(6))",
               [actor, site, secrets.token_hex(32)])
    target.sql("INSERT INTO accounts_attendance (account_id, restaurant_id, check_in) "
               "VALUES (%s, %s, UTC_TIMESTAMP(6))", [actor, site])
    target.sql("INSERT INTO accounts_loginpass (account_id, token_hash, expires, created_at) "
               "VALUES (%s, %s, UTC_TIMESTAMP(6) + INTERVAL 2 MINUTE, UTC_TIMESTAMP(6))",
               [actor, secrets.token_hex(32)])
    access = private_snapshot(target)
    manifest_inode = target.manifest_path.stat().st_ino
    assert receipt(target.invoke(), "reutilizado") == manifest
    assert receipt(target.invoke("verify"), "verificado") == manifest
    unchanged = private_snapshot(target) == access
    assert unchanged, "Se alteró el acceso existente durante la verificación o el reuso"
    assert target.manifest_path.stat().st_ino == manifest_inode
    assert target.sql("SELECT COUNT(*) FROM sales_cashshift") == ((0,),)


def test_rechaza_incompatibilidad_y_no_adopta_destinos_existentes(target):
    # Falla si se toca una base ajena, se reanuda una siembra parcial o se repara estado ajeno al manifiesto.
    result = target.invoke(environment="production")
    assert result.returncode == 2 and "development" in result.stderr
    old = {**target.config, "profile_version": 2}
    result = target.invoke(config=old)
    assert result.returncode == 2 and "incompatible" in result.stderr
    foreign = deepcopy(target.config)
    foreign["mount_id"] = "otro-montaje"
    foreign["db"]["name"] = "test_waiter_qa_otro_montaje"
    result = target.invoke(config=foreign)
    assert result.returncode == 2 and "no pertenece" in result.stderr
    assert not target.schema_exists(foreign["db"]["name"])
    invalid = deepcopy(target.config)
    invalid["db"]["name"] = invalid["db"]["source"]
    result = target.invoke(config=invalid)
    assert result.returncode == 2 and "destino de prueba" in result.stderr
    assert not target.schema_exists()

    write_private(target.manifest_path, {"state": "preparando"})
    result = target.invoke()
    assert result.returncode == 2 and "parcial" in result.stderr
    assert not target.schema_exists()
    assert json.loads(target.manifest_path.read_text()) == {"state": "preparando"}
    target.manifest_path.unlink()

    # Un esquema existente sin manifiesto puede contener trabajo previo de otro montaje.
    name = target.config["db"]["name"]
    target.sql(f"CREATE DATABASE `{name}`", source=True)
    target.sql("CREATE TABLE control (id INTEGER PRIMARY KEY)")
    target.sql("INSERT INTO control VALUES (7)")
    result = target.invoke()
    assert result.returncode == 2 and "ya existe" in result.stderr
    assert target.sql("SELECT id FROM control") == ((7,),)
    assert not target.manifest_path.exists()
    target.sql(f"DROP DATABASE `{name}`", source=True)

    manifest = receipt(target.invoke(), "creado")
    site_id = manifest["inventory"]["m01"]["restaurant_id"]
    target.sql("UPDATE tenancy_restaurant SET active = 0 WHERE id = %s", [site_id])
    result = target.invoke()
    assert result.returncode == 2 and "difiere del manifiesto" in result.stderr
    assert json.loads(target.manifest_path.read_text()) == manifest
    assert target.sql("SELECT active FROM tenancy_restaurant WHERE id = %s", [site_id]) == ((0,),)
