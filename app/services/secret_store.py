from __future__ import annotations

import subprocess
import sys


_SERVICE_NAME = "EnergyFlowStudio.Automation"


def keychain_available() -> bool:
    return sys.platform == "darwin"


def save_secret(secret_ref: str, secret_value: str) -> bool:
    ref = str(secret_ref or "").strip()
    value = str(secret_value or "")
    if not ref:
        return False
    if not keychain_available():
        return False
    try:
        subprocess.run(
            [
                "security",
                "add-generic-password",
                "-U",
                "-a",
                ref,
                "-s",
                _SERVICE_NAME,
                "-w",
                value,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return False
    return True


def load_secret(secret_ref: str) -> str:
    ref = str(secret_ref or "").strip()
    if not ref:
        return ""
    if not keychain_available():
        return ""
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-a",
                ref,
                "-s",
                _SERVICE_NAME,
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return ""
    return str(result.stdout or "").strip()


def delete_secret(secret_ref: str) -> bool:
    ref = str(secret_ref or "").strip()
    if not ref:
        return False
    if not keychain_available():
        return False
    try:
        subprocess.run(
            [
                "security",
                "delete-generic-password",
                "-a",
                ref,
                "-s",
                _SERVICE_NAME,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return False
    return True
