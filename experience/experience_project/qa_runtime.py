"""Identidad declarada por el lanzador antes de iniciar Django."""

import hashlib
import json
import os
import re


def identity(environment, checkout):
    if environment != "development":
        return None
    try:
        raw = json.loads(os.environ.get("WAITER_QA_RUNTIME", "null"))
    except ValueError:
        return None
    if not isinstance(raw, dict) or raw.get("component") != "api" or raw.get("checkout") != str(checkout):
        return None
    patterns = {"sha": r"(?:[a-f0-9]{40}|[a-f0-9]{64})", "mount_id": r"[a-z0-9]+(?:-[a-z0-9]+)*",
                "boot_id": r"[a-f0-9]{32}"}
    for key, pattern in patterns.items():
        value = raw.get(key)
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            return None
    if len(raw["mount_id"]) > 32:
        return None
    return {"component": "api", **{key: raw[key] for key in patterns},
            "checkout_digest": hashlib.sha256(str(checkout).encode()).hexdigest()}
