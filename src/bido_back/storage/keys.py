"""Blob key rules shared by every storage backend."""

import re

_MAX_KEY_LENGTH = 1024
# Control characters and backslashes never belong in a key (backslash would be
# a path separator on Windows and is ambiguous in S3 consoles).
_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f\\]")


class InvalidBlobKey(ValueError):
    """The key is empty, absolute, too long, or escapes the storage root."""


class BlobNotFound(LookupError):
    """No blob is stored under the key."""


def validate_key(key: str) -> str:
    """Return `key` unchanged if it is a safe relative path, else raise.

    The message never echoes the key: keys can carry filenames from emails.
    """
    if not key or len(key) > _MAX_KEY_LENGTH:
        raise InvalidBlobKey("blob key is empty or too long")
    if _FORBIDDEN.search(key):
        raise InvalidBlobKey("blob key contains forbidden characters")
    if key.startswith("/"):
        raise InvalidBlobKey("blob key must be relative")
    parts = key.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise InvalidBlobKey("blob key has an empty, '.' or '..' segment")
    return key
