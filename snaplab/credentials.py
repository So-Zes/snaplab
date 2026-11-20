"""API keys and other secrets in the OS credential store: Windows Credential Manager, macOS Keychain or Linux
Secret Service. A Linux PC without a running Secret Service (a server, a minimal desktop) uses a file only its
owner can read, ~/.snaplab/secrets.json (mode 0600), like other command-line tools keep their tokens.
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
from pathlib import Path
from ctypes import wintypes


def _windows() -> bool:
    return os.name == "nt"


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _advapi32():
    return ctypes.WinDLL("advapi32", use_last_error=True)


def read_generic_credential(target: str) -> str | None:
    """The password is a UTF-16LE blob, as written by Credential Manager's UI."""
    advapi = _advapi32()
    pointer = ctypes.POINTER(_Credential)
    advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.POINTER(pointer)]
    advapi.CredReadW.restype = wintypes.BOOL
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    advapi.CredFree.restype = None
    credential = pointer()
    if not advapi.CredReadW(target, 1, 0, ctypes.byref(credential)):  # CRED_TYPE_GENERIC
        return None  # Includes not found, access denied and unavailable logon session.
    try:
        if not credential:
            return None
        value = credential.contents
        if not value.CredentialBlob or not value.CredentialBlobSize:
            return None
        raw = ctypes.string_at(value.CredentialBlob, value.CredentialBlobSize)
        return raw.decode("utf-16-le").rstrip("\0").strip() or None
    except UnicodeError:
        return None
    finally:
        advapi.CredFree(credential)


def _system_keyring():
    """Only OS-backed stores; never accept a plaintext or third-party fallback."""
    import sys
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring
    else:
        from keyring.backends.SecretService import Keyring
    backend = Keyring()
    if backend.priority <= 0:
        raise OSError("The OS credential store is unavailable")
    return backend


def _file() -> Path:
    return Path(os.environ.get("SNAPLAB_SECRETS_FILE") or (Path.home() / ".snaplab" / "secrets.json"))


def _file_read() -> dict:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:      # missing, unreadable or not JSON: no saved values
        return {}


def _file_write(data: dict) -> None:
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def read_system_credential(target: str) -> str | None:
    try:
        value = _system_keyring().get_password("SnapLab", target)
        if value:
            return value
    except Exception:
        pass
    value = _file_read().get(target)
    return value if isinstance(value, str) and value else None


def write_credential(target: str, value: str) -> None:
    """Save only to Windows Credential Manager, macOS Keychain or Secret Service."""
    if not _windows():
        try:
            _system_keyring().set_password("SnapLab", target, value)
            return
        except Exception:
            if sys.platform == "darwin":
                raise ValueError("Unlock the macOS Keychain, then try again.") from None
        try:
            data = _file_read()
            data[target] = value
            _file_write(data)
        except OSError:
            raise ValueError("SnapLab could not save the key in ~/.snaplab/secrets.json. Check that the folder is writable.") from None
        return
    raw = value.encode("utf-16-le")
    if len(raw) > 2560:
        raise ValueError("The key is too long for Windows Credential Manager.")
    blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    credential = _Credential(Type=1, TargetName=target, CredentialBlobSize=len(raw),
                             CredentialBlob=blob, Persist=2, UserName="SnapLab")
    advapi = _advapi32()
    advapi.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    advapi.CredWriteW.restype = wintypes.BOOL
    if not advapi.CredWriteW(ctypes.byref(credential), 0):
        raise ValueError("Windows Credential Manager could not save the key. Sign in normally and try again.")


def read_credential(target: str) -> str | None:
    """Any platform: Windows Credential Manager, macOS Keychain or Secret Service."""
    return read_generic_credential(target) if _windows() else read_system_credential(target)


def delete_credential(target: str) -> None:
    """Remove a value SnapLab saved; a missing one is not an error."""
    if not _windows():
        try:
            _system_keyring().delete_password("SnapLab", target)
        except Exception:
            pass
        data = _file_read()
        if data.pop(target, None) is not None:
            _file_write(data)
        return
    advapi = _advapi32()
    advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    advapi.CredDeleteW.restype = wintypes.BOOL
    advapi.CredDeleteW(target, 1, 0)
