"""Encryption for Gmail refresh tokens at rest.

**Be precise about what this buys.** The key lives in the same environment as
the process that uses it, so anything able to run code in the app can decrypt
every token regardless. This protects against ONE thing: disclosure of the
database on its own -- a Supabase dump, a leaked backup, a stray `pg_dump` in
someone's Downloads folder, a read-only credential handed to the wrong person.
That is a realistic threat and this is a real mitigation for it. It is not
defence in depth against an application compromise, and describing it that way
would be a lie that stops someone thinking about the real problem.

A refresh token is a long-lived credential to send mail as a real person from a
real institutional mailbox. It is the most dangerous thing this system stores.

`key_version` exists from the first migration on purpose. Rotating a Fernet key
without recording which key encrypted which row leaves a re-encrypt command that
cannot tell what it has already done -- so it either skips rows or corrupts
them, and there is no way to tell which afterwards.
"""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class TokenKeyError(ImproperlyConfigured):
    """The key configuration is unusable. Always fatal, never caught."""


def _fernet():
    from cryptography.fernet import Fernet

    return Fernet


def _parse_keys(raw: str) -> dict[int, str]:
    """Parse GMAIL_TOKEN_KEY into {version: key}.

    Accepts either a bare Fernet key (which is version 1) or a comma-separated
    list of `version:key` pairs. Mixing the two forms is refused rather than
    guessed at: a half-versioned list is far more likely to be a typo than an
    intention, and guessing wrong makes tokens undecryptable.
    """
    entries = [part.strip() for part in (raw or "").split(",") if part.strip()]
    if not entries:
        return {}

    versioned = [":" in e for e in entries]
    if any(versioned) and not all(versioned):
        raise TokenKeyError(
            "GMAIL_TOKEN_KEY mixes bare and versioned keys. Use either one bare "
            "key, or a comma-separated list of `version:key` pairs."
        )

    if not any(versioned):
        if len(entries) > 1:
            raise TokenKeyError(
                "GMAIL_TOKEN_KEY lists several keys but none carry a version. "
                "Write them as `2:<new-key>,1:<old-key>` so rotation can tell "
                "which key encrypted which row."
            )
        return {1: entries[0]}

    keys: dict[int, str] = {}
    for entry in entries:
        version_text, _, key = entry.partition(":")
        try:
            version = int(version_text)
        except ValueError:
            raise TokenKeyError(
                f"GMAIL_TOKEN_KEY entry {entry!r} does not start with an integer "
                "version."
            )
        if version in keys:
            raise TokenKeyError(f"GMAIL_TOKEN_KEY declares version {version} twice.")
        keys[version] = key.strip()
    return keys


def _keys() -> dict[int, str]:
    return _parse_keys(getattr(settings, "GMAIL_TOKEN_KEY", "") or "")


def is_configured() -> bool:
    return bool(_keys())


def current_version() -> int:
    """The version new tokens are encrypted with: the highest configured."""
    keys = _keys()
    if not keys:
        raise TokenKeyError(
            "GMAIL_TOKEN_KEY is not set, so Gmail refresh tokens cannot be "
            "stored. Generate one:  python -c 'from cryptography.fernet import "
            "Fernet; print(Fernet.generate_key().decode())'"
        )
    return max(keys)


def encrypt(plaintext: str) -> tuple[bytes, int]:
    """Encrypt with the current key. Returns (ciphertext, key_version)."""
    version = current_version()
    fernet = _fernet()(_keys()[version].encode())
    return fernet.encrypt(plaintext.encode()), version


def decrypt(ciphertext, key_version: int) -> str:
    """Decrypt a value stored under `key_version`.

    A missing key is an operator error worth shouting about: it means a key was
    dropped from the environment while rows encrypted with it still exist, and
    those rows are now unreadable. Failing loudly here is what turns that into
    "put the old key back" rather than "every member has to reconnect Gmail and
    nobody knows why".
    """
    keys = _keys()
    key = keys.get(key_version)
    if key is None:
        raise TokenKeyError(
            f"No Gmail token key for version {key_version}. Rows encrypted with "
            f"it cannot be read. Configured versions: {sorted(keys) or 'none'}. "
            "Restore the retired key as `<version>:<key>` in GMAIL_TOKEN_KEY."
        )

    if isinstance(ciphertext, memoryview):        # psycopg returns bytea as this
        ciphertext = ciphertext.tobytes()
    return _decrypt_with(key, ciphertext)


def _decrypt_with(key: str, ciphertext: bytes) -> str:
    from cryptography.fernet import InvalidToken

    try:
        return _fernet()(key.encode()).decrypt(ciphertext).decode()
    except InvalidToken as exc:
        # Deliberately does not include the ciphertext or the key in the message.
        raise TokenKeyError(
            "A stored Gmail token failed to decrypt with the key recorded for "
            "it. The key was changed without re-encrypting, or the row is "
            "corrupt. The member must reconnect Gmail."
        ) from exc


def self_test() -> None:
    """Prove the configured key actually works. Called by `manage.py check_db`.

    A boot check that passes while every send will fail on a bad key is a boot
    check that lies -- and this failure would otherwise surface as one member's
    mail silently not going out.
    """
    token, version = encrypt("check_db round trip")
    if decrypt(token, version) != "check_db round trip":
        raise TokenKeyError("Gmail token encryption did not round-trip.")
