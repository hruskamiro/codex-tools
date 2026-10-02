"""Passphrase encryption for portable codex-manager profile bundles."""

from __future__ import annotations

import os
import struct
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


MAGIC = b"CODEXPROFILE\x00"
FORMAT_VERSION = 1
SALT_SIZE = 16
NONCE_SIZE = 12
TAG_SIZE = 16
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
CHUNK_SIZE = 1024 * 1024
HEADER = struct.Struct(f">{len(MAGIC)}sBIII{SALT_SIZE}s{NONCE_SIZE}s")


class BundleError(ValueError):
    """Raised when an encrypted profile bundle is invalid."""


def _derive_key(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(
        passphrase.encode("utf-8")
    )


def encrypt_file(source: Path, target: Path, passphrase: str) -> None:
    """Encrypt source into target using a password-derived AES-256-GCM key."""
    salt = os.urandom(SALT_SIZE)
    nonce = os.urandom(NONCE_SIZE)
    header = HEADER.pack(
        MAGIC,
        FORMAT_VERSION,
        SCRYPT_N,
        SCRYPT_R,
        SCRYPT_P,
        salt,
        nonce,
    )
    key = _derive_key(passphrase, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(header)

    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with source.open("rb") as source_handle, os.fdopen(
            descriptor, "wb"
        ) as target_handle:
            descriptor = -1
            target_handle.write(header)
            while chunk := source_handle.read(CHUNK_SIZE):
                target_handle.write(encryptor.update(chunk))
            target_handle.write(encryptor.finalize())
            target_handle.write(encryptor.tag)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        target.unlink(missing_ok=True)
        raise


def decrypt_file(source: Path, target: Path, passphrase: str) -> None:
    """Decrypt source into target and authenticate it before returning."""
    size = source.stat().st_size
    if size < HEADER.size + TAG_SIZE:
        raise BundleError("file is too short to be a profile bundle")

    with source.open("rb") as source_handle:
        raw_header = source_handle.read(HEADER.size)
        magic, version, n, r, p, salt, nonce = HEADER.unpack(raw_header)
        if magic != MAGIC:
            raise BundleError("not a codex-manager profile bundle")
        if version != FORMAT_VERSION:
            raise BundleError(f"unsupported encrypted bundle version: {version}")
        if (n, r, p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
            raise BundleError("invalid bundle key-derivation parameters")

        source_handle.seek(-TAG_SIZE, os.SEEK_END)
        tag = source_handle.read(TAG_SIZE)
        ciphertext_size = size - HEADER.size - TAG_SIZE
        source_handle.seek(HEADER.size)

        key = _derive_key(passphrase, salt, n, r, p)
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(raw_header)

        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as target_handle:
                descriptor = -1
                remaining = ciphertext_size
                while remaining:
                    chunk = source_handle.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        raise BundleError("profile bundle is truncated")
                    remaining -= len(chunk)
                    target_handle.write(decryptor.update(chunk))
                target_handle.write(decryptor.finalize())
        except InvalidTag as exc:
            if descriptor >= 0:
                os.close(descriptor)
            target.unlink(missing_ok=True)
            raise BundleError("incorrect passphrase or corrupted profile bundle") from exc
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            target.unlink(missing_ok=True)
            raise
