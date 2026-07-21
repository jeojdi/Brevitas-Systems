#!/usr/bin/env python3
"""Launch a command with a PostgreSQL URI translated to libpq environment.

The URI is read from a caller-owned file descriptor so a password-bearing URI
never enters the process argument list.  DR shell entrypoints use descriptor 3
and leave standard input available for psql/pg_dump/pg_restore pipelines.
"""

from __future__ import annotations

import os
import re
import sys
from urllib.parse import parse_qsl, unquote, urlsplit


QUERY_ENV = {
    "application_name": "PGAPPNAME",
    "channel_binding": "PGCHANNELBINDING",
    "client_encoding": "PGCLIENTENCODING",
    "gssencmode": "PGGSSENCMODE",
    "gsslib": "PGGSSLIB",
    "keepalives": "PGKEEPALIVES",
    "keepalives_count": "PGKEEPALIVESCOUNT",
    "keepalives_idle": "PGKEEPALIVESIDLE",
    "keepalives_interval": "PGKEEPALIVESINTERVAL",
    "krbsrvname": "PGKRBSRVNAME",
    "load_balance_hosts": "PGLOADBALANCEHOSTS",
    "options": "PGOPTIONS",
    "requirepeer": "PGREQUIREPEER",
    "sslcert": "PGSSLCERT",
    "sslcompression": "PGSSLCOMPRESSION",
    "sslcrl": "PGSSLCRL",
    "sslcrldir": "PGSSLCRLDIR",
    "sslkey": "PGSSLKEY",
    "ssl_max_protocol_version": "PGSSLMAXPROTOCOLVERSION",
    "ssl_min_protocol_version": "PGSSLMINPROTOCOLVERSION",
    "sslmode": "PGSSLMODE",
    "sslrootcert": "PGSSLROOTCERT",
    "sslsni": "PGSSLSNI",
    "target_session_attrs": "PGTARGETSESSIONATTRS",
    "tcp_user_timeout": "PGTCPUSER_TIMEOUT",
}

LIBPQ_ENV = set(QUERY_ENV.values()) | {
    "PGAPPNAME",
    "PGCHANNELBINDING",
    "PGCLIENTENCODING",
    "PGCONNECT_TIMEOUT",
    "PGDATABASE",
    "PGGSSENCMODE",
    "PGGSSLIB",
    "PGHOST",
    "PGHOSTADDR",
    "PGLOCALEDIR",
    "PGMAXPROTOCOLVERSION",
    "PGMINPROTOCOLVERSION",
    "PGOPTIONS",
    "PGPASSWORD",
    "PGPASSFILE",
    "PGPORT",
    "PGREALM",
    "PGREQUIRESSL",
    "PGREQUIREAUTH",
    "PGSERVICE",
    "PGSERVICEFILE",
    "PGSSLCERTMODE",
    "PGSSLKEYLOGFILE",
    "PGSSLNEGOTIATION",
    "PGSYSCONFDIR",
    "PGTARGETSESSIONATTRS",
    "PGUSER",
}


class DatabaseUrlError(ValueError):
    pass


def decode_component(value: str | None, label: str) -> str:
    if value is None:
        return ""
    try:
        decoded = unquote(value, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise DatabaseUrlError(f"{label} is not valid UTF-8") from error
    if not decoded or any(character in decoded for character in "\x00\r\n"):
        raise DatabaseUrlError(f"{label} is empty or contains a control character")
    return decoded


def parse_database_url(raw: str, connect_timeout: str) -> dict[str, str]:
    if not raw or len(raw) > 65536:
        raise DatabaseUrlError("credential is empty or exceeds its safety bound")
    if any(character in raw for character in "\x00\r\n"):
        raise DatabaseUrlError("credential contains a control character")
    if re.search(r"%(?![0-9A-Fa-f]{2})", raw):
        raise DatabaseUrlError("credential contains invalid percent encoding")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as error:
        raise DatabaseUrlError("authority or port is invalid") from error
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise DatabaseUrlError("scheme must be postgres or postgresql")
    if parsed.fragment:
        raise DatabaseUrlError("fragments are not supported")
    if not parsed.path.startswith("/") or parsed.path == "/":
        raise DatabaseUrlError("an explicit database name is required")
    if not parsed.hostname:
        raise DatabaseUrlError("an explicit network hostname is required")
    if parsed.username is None:
        raise DatabaseUrlError("an explicit database user is required")
    if parsed.password is None:
        raise DatabaseUrlError("an explicit database password is required")
    if port is None:
        raise DatabaseUrlError("an explicit network port is required")
    decoded_host = decode_component(parsed.hostname, "host")
    if "," in decoded_host:
        raise DatabaseUrlError("multi-host authorities are not supported")
    if decoded_host.startswith("/"):
        raise DatabaseUrlError("Unix-domain socket hosts are not supported")

    values: dict[str, str] = {
        "PGDATABASE": decode_component(parsed.path[1:], "database name"),
        "PGCONNECT_TIMEOUT": connect_timeout,
    }
    values["PGHOST"] = decoded_host
    if not 1 <= port <= 65535:
        raise DatabaseUrlError("port is outside the valid range")
    values["PGPORT"] = str(port)
    values["PGUSER"] = decode_component(parsed.username, "user")
    values["PGPASSWORD"] = decode_component(parsed.password, "password")

    query_items: list[tuple[str, str]] = []
    if parsed.query:
        try:
            query_items = parse_qsl(
                parsed.query,
                keep_blank_values=True,
                strict_parsing=True,
                encoding="utf-8",
                errors="strict",
            )
        except (UnicodeDecodeError, ValueError) as error:
            raise DatabaseUrlError("query parameters are invalid") from error
    seen: set[str] = set()
    for key, value in query_items:
        if key in seen:
            raise DatabaseUrlError("duplicate query parameters are not supported")
        seen.add(key)
        environment_name = QUERY_ENV.get(key)
        if environment_name is None:
            raise DatabaseUrlError(f"unsupported PostgreSQL query parameter: {key}")
        if not value or any(character in value for character in "\x00\r\n"):
            raise DatabaseUrlError(f"query parameter {key} has an invalid value")
        values[environment_name] = value
    return values


def main() -> int:
    if len(sys.argv) < 7 or sys.argv[1] != "--database-url-fd" \
            or sys.argv[3] != "--connect-timeout" or sys.argv[5] != "--":
        print(
            "ERROR: libpq launcher requires a credential FD, timeout, and command",
            file=sys.stderr,
        )
        return 2
    try:
        credential_fd = int(sys.argv[2])
    except ValueError:
        print("ERROR: libpq launcher credential FD is invalid", file=sys.stderr)
        return 2
    connect_timeout = sys.argv[4]
    if not connect_timeout.isdigit() or not 1 <= int(connect_timeout) <= 60:
        print("ERROR: libpq launcher timeout must be between 1 and 60 seconds", file=sys.stderr)
        return 2
    command = sys.argv[6:]
    if not command:
        print("ERROR: libpq launcher command is missing", file=sys.stderr)
        return 2
    try:
        with os.fdopen(credential_fd, "r", encoding="utf-8", errors="strict") as stream:
            database_url = stream.read(65537)
        # Bash here-strings append exactly one transport newline.
        if database_url.endswith("\n"):
            database_url = database_url[:-1]
        values = parse_database_url(database_url, connect_timeout)
    except (DatabaseUrlError, OSError, UnicodeDecodeError) as error:
        print(f"ERROR: invalid PostgreSQL database URL: {error}", file=sys.stderr)
        return 2

    environment = os.environ.copy()
    for name in LIBPQ_ENV:
        environment.pop(name, None)
    # Do not propagate a named source credential after it has been decomposed.
    for name, value in list(environment.items()):
        if value == database_url:
            environment.pop(name, None)
    environment.update(values)
    try:
        os.execvpe(command[0], command, environment)
    except OSError as error:
        print(f"ERROR: unable to execute database command {command[0]}: {error.strerror}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
