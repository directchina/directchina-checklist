"""Read only Timeweb Cloud cookies from a Linux Chromium profile (v10)."""

import hashlib
import json
import os
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .core import CheckError

NAMES = ("refresh_token", "device_token", "qrator_msid2")
HOSTS = (".timeweb.cloud", "timeweb.cloud")
STATE = Path(__file__).resolve().parent.parent / ".state"


@contextmanager
def cloud_session(profile):
    """Serialize refreshes and persist rotated credentials without touching Chromium."""
    try:
        import fcntl
    except ImportError:
        raise CheckError(
            "Чтение сессии Chromium поддерживается только в Linux"
        ) from None
    cookies = chromium_cookies(profile)
    source = hashlib.sha256(
        json.dumps([item for item in cookies if item[1] == "refresh_token"]).encode()
    ).hexdigest()
    profile_id = hashlib.sha256(str(Path(profile).resolve()).encode()).hexdigest()[:16]
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = STATE / f"cloud-{profile_id}.json"
    lock = os.open(STATE / f"cloud-{profile_id}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(lock, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CheckError(
                "Другой обход уже обновляет сессию Cloud; повторите позже"
            ) from None
        if path.exists():
            try:
                cached = json.loads(path.read_text())
                if cached["source"] == source:
                    saved = cached["cookies"]
                    if (
                        not isinstance(saved, list)
                        or not saved
                        or not all(
                            isinstance(c, list)
                            and len(c) == 4
                            and all(isinstance(v, str) for v in c)
                            and c[0] in HOSTS
                            and c[1] in NAMES
                            and c[2].startswith("/")
                            for c in saved
                        )
                        or not any(c[1] == "refresh_token" and c[3] for c in saved)
                    ):
                        raise ValueError
                    cookies = saved
            except (ValueError, KeyError, TypeError):
                raise CheckError(
                    "Повреждён локальный файл .state/cloud-*.json; удалите его и войдите заново в Chromium"
                ) from None
        # Check that persistence is possible BEFORE consuming the refresh token.
        fd, temporary = tempfile.mkstemp(prefix="cloud-", dir=STATE)
        try:
            with os.fdopen(fd, "w") as output:

                def save(updated):
                    output.seek(0)
                    json.dump({"source": source, "cookies": updated}, output)
                    output.truncate()
                    output.flush()
                    os.fsync(output.fileno())
                    os.replace(temporary, path)

                yield cookies, save
        finally:
            Path(temporary).unlink(missing_ok=True)


def chromium_cookies(profile):
    profile = Path(profile).expanduser()
    database = profile / "Network" / "Cookies"
    if not database.exists():
        database = profile / "Cookies"
    if not database.is_file():
        raise CheckError(
            "Не найдены cookies Chromium; проверьте TIMEWEB_CLOUD_CHROMIUM_PROFILE"
        )
    result = []
    try:
        # Never copy unrelated cookies or write to the browser profile.
        db = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            version = int(
                db.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0]
            )
            rows = db.execute(
                "SELECT host_key, name, path, value, encrypted_value, expires_utc "
                "FROM cookies WHERE host_key IN (?, ?) AND name IN (?, ?, ?)",
                (*HOSTS, *NAMES),
            ).fetchall()
        finally:
            db.close()
        for host, name, path, value, encrypted, expires in rows:
            if expires and expires / 1_000_000 - 11644473600 <= time.time():
                continue
            if encrypted:
                if encrypted[:3] != b"v10":
                    raise CheckError(
                        "Шифрование cookies Chromium не поддерживается: нужен Linux-профиль v10 "
                        "или TIMEWEB_CLOUD_AUTH=password"
                    )
                key = hashlib.pbkdf2_hmac("sha1", b"peanuts", b"saltysalt", 1, 16)
                decryptor = Cipher(
                    algorithms.AES(key), modes.CBC(b" " * 16)
                ).decryptor()
                padded = decryptor.update(encrypted[3:]) + decryptor.finalize()
                unpadder = padding.PKCS7(128).unpadder()
                plain = unpadder.update(padded) + unpadder.finalize()
                if version >= 24:
                    if plain[:32] != hashlib.sha256(host.encode()).digest():
                        raise ValueError("Cookie domain hash mismatch")
                    plain = plain[32:]
                value = plain.decode("utf-8")
            if value:
                result.append((host, name, path, value))
    except (sqlite3.Error, OSError, ValueError, TypeError, IndexError):
        raise CheckError(
            "Не удалось прочитать cookies Timeweb Cloud из Chromium"
        ) from None
    if not any(name == "refresh_token" for _, name, _, _ in result):
        raise CheckError(
            "Нет действующей сессии Timeweb Cloud; войдите в кабинет через Chromium"
        )
    return result
