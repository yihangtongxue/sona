"""Local authenticated encryption; no system credential UI for normal access.

On POSIX, the random master key is protected by owner-only filesystem access.
On Windows, DPAPI additionally wraps it for the current account. This does not
protect against code already running as that account or a complete POSIX backup
containing both the master key and ciphertext.
"""

from __future__ import annotations

import os
import stat
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .file_lock import FileLocked, exclusive_file_lock


LOCAL_KEY_PREFIX = "local-v1:"
_HEADER = b"SONAKEY1"
_MASTER_HEADER = b"SONAMASTER1:"


class CredentialError(ValueError):
    """Safe to display without exposing backend exceptions or secret material."""


def _windows_protect(data: bytes, *, decrypt: bool = False) -> bytes:
    """Use current-user DPAPI, explicitly forbidding interactive prompts."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = Blob(len(data), buffer)
    result = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                         ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    try:
        if not function(ctypes.byref(source), None, None, None, None, 0x1,
                        ctypes.byref(result)):  # CRYPTPROTECT_UI_FORBIDDEN
            raise OSError("DPAPI operation failed")
        return ctypes.string_at(result.data, result.size)
    finally:
        if result.data:
            kernel32.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))


class LocalCredentialStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.master_path = directory / "master.key"
        self._mutex = RLock()

    def _path(self, reference: str) -> Path:
        local = reference.startswith(LOCAL_KEY_PREFIX)
        identifier = reference.removeprefix(LOCAL_KEY_PREFIX)
        if str(uuid.UUID(identifier)) != identifier:
            raise ValueError("Invalid credential reference")
        origin = "local" if local else "legacy"
        return self.directory / f"{origin}-{identifier}.enc"

    @staticmethod
    def _private(path: Path, *, directory: bool = False) -> None:
        info = path.lstat()
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected(info.st_mode):
            raise OSError("Credential path must not be a link or special file")
        if os.name != "nt":
            if info.st_uid != os.getuid():
                raise OSError("Credential path is owned by another user")
            path.chmod(0o700 if directory else 0o600)

    @contextmanager
    def _locked(self):
        with self._mutex:
            try:
                self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                self._private(self.directory, directory=True)
                lock_path = self.directory / ".store.lock"
                descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR
                                     | getattr(os, "O_NOFOLLOW", 0), 0o600)
                os.close(descriptor)
                self._private(lock_path)
                with exclusive_file_lock(lock_path):
                    yield
            except CredentialError:
                raise
            except FileLocked:
                raise CredentialError("正在处理其他密钥操作，请稍后重试。") from None
            except Exception:
                raise CredentialError(
                    "无法访问本地加密密钥，请检查应用数据目录权限；若文件损坏，请恢复凭据备份。"
                ) from None

    def _read_file(self, path: Path, limit: int = 32768) -> bytes:
        self._private(path)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError("Credential is not a regular file")
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Credential file too large")
        return data

    def _write_file(self, path: Path, data: bytes) -> None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.directory, prefix=".credential-",
                                             delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
            if os.name != "nt":
                descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _master_key(self, *, create: bool = False) -> bytes:
        try:
            stored = self._read_file(self.master_path)
        except FileNotFoundError:
            # Never replace a lost master key while ciphertext still exists.
            if not create or any(self.directory.glob("*.enc")):
                raise CredentialError("本地解密密钥丢失，请恢复完整的 credentials 目录备份。") from None
            key = AESGCM.generate_key(bit_length=256)
            mode = b"dpapi:" if os.name == "nt" else b"owner:"
            payload = _windows_protect(key) if os.name == "nt" else key
            self._write_file(self.master_path, _MASTER_HEADER + mode + payload)
            return key
        prefix = _MASTER_HEADER + (b"dpapi:" if os.name == "nt" else b"owner:")
        if not stored.startswith(prefix):
            raise CredentialError("本地解密密钥格式无效或来自其他系统，请恢复本机凭据备份。")
        key = stored[len(prefix):]
        if os.name == "nt":
            key = _windows_protect(key, decrypt=True)
        if len(key) != 32:
            raise CredentialError("本地解密密钥已损坏，请恢复凭据备份。")
        return key

    def read(self, reference: str) -> str:
        with self._locked():
            try:
                encrypted = self._read_file(self._path(reference))
            except FileNotFoundError:
                return ""
            if not encrypted.startswith(_HEADER):
                raise CredentialError("本地 API Key 文件已损坏，请恢复备份或重新填写。")
            nonce = encrypted[len(_HEADER):len(_HEADER) + 12]
            ciphertext = encrypted[len(_HEADER) + 12:]
            try:
                plaintext = AESGCM(self._master_key()).decrypt(
                    nonce, ciphertext, reference.encode("utf-8"),
                )
                return plaintext.decode("utf-8")
            except CredentialError:
                raise
            except Exception:
                raise CredentialError("本地 API Key 解密失败，请恢复备份或重新填写。") from None

    def save(self, reference: str, secret: str) -> None:
        with self._locked():
            path = self._path(reference)
            if not secret or len(secret) > 4096:
                raise CredentialError("API Key 不能为空或超过 4096 个字符。")
            key = self._master_key(create=True)
            nonce = os.urandom(12)
            encrypted = AESGCM(key).encrypt(nonce, secret.encode("utf-8"), reference.encode("utf-8"))
            self._write_file(path, _HEADER + nonce + encrypted)

    def delete(self, reference: str) -> None:
        with self._locked():
            self._path(reference).unlink(missing_ok=True)
