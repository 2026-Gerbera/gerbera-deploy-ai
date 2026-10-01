"""Required deployment configuration. Never include values in diagnostics."""

import os
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class ConfigurationError(ValueError):
    """Safe configuration error that contains no supplied values."""


def required(name):
    value = os.environ.get(name)
    if not value or not value.strip():
        raise ConfigurationError(f"Required environment variable: {name}")
    return value


def database_url(migration=False):
    name = "DATABASE_URL"
    if migration and os.environ.get("DATABASE_URL_MIGRATOR"):
        name = "DATABASE_URL_MIGRATOR"
    value = required(name)
    try:
        url = make_url(value)
        if url.drivername not in {"sqlite", "sqlite+pysqlite", "mysql+pymysql"}:
            raise ValueError
        if url.get_backend_name() == "sqlite":
            if not url.database or url.database == ":memory:":
                raise ValueError
        else:
            if migration and name != "DATABASE_URL_MIGRATOR":
                raise ValueError
            account = "flaskr_migrator" if migration else "flaskr_app"
            if (
                url.username != account
                or not url.password
                or not url.host
                or url.database != "flaskr"
            ):
                raise ValueError
    except (ValueError, TypeError, ArgumentError):
        raise ConfigurationError(f"Invalid database configuration: {name}") from None
    return url


def app_config():
    if os.environ.get("DATABASE_URL_MIGRATOR"):
        raise ConfigurationError("Migration credentials must not be supplied to the application")
    secret = required("SECRET_KEY")
    if len(secret) < 32:
        raise ConfigurationError("SECRET_KEY must contain at least 32 characters")
    base = required("APP_BASE_URL")
    try:
        parsed = urlsplit(base)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or any(c.isspace() for c in base)
        ):
            raise ValueError
        if parsed.port is not None and parsed.port < 1:
            raise ValueError
    except ValueError:
        raise ConfigurationError("Invalid APP_BASE_URL origin") from None
    secure = required("SESSION_COOKIE_SECURE").lower()
    if secure not in {"true", "false"} or (secure == "true") != (parsed.scheme == "https"):
        raise ConfigurationError("SESSION_COOKIE_SECURE must match APP_BASE_URL scheme")
    hops = {}
    for key in ("PROXY_FIX_X_FOR", "PROXY_FIX_X_PROTO"):
        value = required(key)
        if value not in {"0", "1", "2"}:
            raise ConfigurationError(f"Invalid proxy hop count: {key}")
        hops[key] = int(value)
    return {
        "SECRET_KEY": secret,
        "APP_BASE_URL": base.rstrip("/"),
        "SESSION_COOKIE_SECURE": secure == "true",
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "TRUSTED_HOSTS": [parsed.hostname],
        "PREFERRED_URL_SCHEME": parsed.scheme,
        "RELEASE_ID": required("RELEASE_ID"),
        "MAX_CONTENT_LENGTH": 1024 * 1024,
        **hops,
    }
