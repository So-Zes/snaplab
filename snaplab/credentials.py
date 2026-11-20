"""Read a Windows generic credential without logging or persisting its contents."""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


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


def read_system_credential(target: str) -> str | None:
    try:
        return _system_keyring().get_password("SnapLab", target)
    except Exception:
        return None


def write_credential(target: str, value: str) -> None:
    """Save only to Windows Credential Manager, macOS Keychain or Secret Service."""
    if os.name != "nt":
        try:
            _system_keyring().set_password("SnapLab", target, value)
        except Exception:
            raise ValueError("Unlock the OS credential store, then try again. On Linux, install and unlock a Secret Service keyring.") from None
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
