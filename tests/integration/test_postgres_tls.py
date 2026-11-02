"""TLS on Postgres: `Database.postgres(..., sslmode=..., sslrootcert=...)`.

These tests need a server with TLS turned on, its certificate made out to
`localhost` and signed by the authority in `DJOX_POSTGRES_ROOT_CERT`
(`scripts/make-test-certificates.sh` makes both). They are skipped otherwise.
"""

import os
from urllib.parse import urlsplit

import pytest

import djox

POSTGRES_URL = os.environ.get("DJOX_POSTGRES_URL", "")
ROOT_CERTIFICATE = os.environ.get("DJOX_POSTGRES_ROOT_CERT", "")

pytestmark = pytest.mark.skipif(
    not (POSTGRES_URL and ROOT_CERTIFICATE),
    reason="set DJOX_POSTGRES_URL and DJOX_POSTGRES_ROOT_CERT to run the TLS tests",
)

# Is this connection encrypted? Postgres says so in `pg_stat_ssl`.
IS_ENCRYPTED = "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()"


def url_with_host(host: str) -> str:
    """`POSTGRES_URL`, connecting to `host` instead (the same server)."""
    server = urlsplit(POSTGRES_URL)
    login = f"{server.username}:{server.password}@" if server.password else f"{server.username}@"
    port = f":{server.port}" if server.port else ""
    return server._replace(netloc=f"{login}{host}{port}").geturl()


async def test_verify_full_checks_the_authority_and_the_name() -> None:
    with djox.Database.postgres(
        url_with_host("localhost"), sslmode="verify-full", sslrootcert=ROOT_CERTIFICATE
    ) as database:
        assert await database.fetch_one(IS_ENCRYPTED) == (True,)


async def test_streams_are_encrypted_too() -> None:
    with djox.Database.postgres(
        url_with_host("localhost"), sslmode="verify-full", sslrootcert=ROOT_CERTIFICATE
    ) as database:
        stream = database.stream_rows(IS_ENCRYPTED)
        assert await stream.next_chunk() == [(True,)]


def test_verify_full_refuses_a_certificate_made_out_to_another_name() -> None:
    # The certificate says `localhost`; `127.0.0.1` is the same server under another name.
    with pytest.raises(djox.OperationalError):
        djox.Database.postgres(url_with_host("127.0.0.1"), sslmode="verify-full", sslrootcert=ROOT_CERTIFICATE)


async def test_verify_ca_checks_the_authority_only() -> None:
    with djox.Database.postgres(
        url_with_host("127.0.0.1"), sslmode="verify-ca", sslrootcert=ROOT_CERTIFICATE
    ) as database:
        assert await database.fetch_one(IS_ENCRYPTED) == (True,)


def test_verify_full_refuses_an_authority_it_doesnt_trust() -> None:
    # The operating system doesn't trust the tests' made-up authority.
    with pytest.raises(djox.OperationalError):
        djox.Database.postgres(url_with_host("localhost"), sslmode="verify-full", sslrootcert="system")


async def test_prefer_encrypts_without_checking() -> None:
    # libpq's default: TLS when the server offers it, any certificate.
    with djox.Database.postgres(url_with_host("127.0.0.1")) as database:
        assert await database.fetch_one(IS_ENCRYPTED) == (True,)


async def test_disable_connects_in_plain_text() -> None:
    with djox.Database.postgres(url_with_host("localhost"), sslmode="disable") as database:
        assert await database.fetch_one(IS_ENCRYPTED) == (False,)


async def test_sslmode_in_the_url_works_for_the_modes_the_driver_knows() -> None:
    with djox.Database.postgres(url_with_host("localhost") + "?sslmode=require") as database:
        assert await database.fetch_one(IS_ENCRYPTED) == (True,)


def test_tls_settings_are_checked() -> None:
    with pytest.raises(ValueError, match="not an sslmode"):
        djox.Database.postgres(POSTGRES_URL, sslmode="on")
    with pytest.raises(ValueError, match="can't read"):
        djox.Database.postgres(POSTGRES_URL, sslmode="verify-full", sslrootcert="/no/such/root.crt")
    with pytest.raises(ValueError, match="sslcert and sslkey go together"):
        djox.Database.postgres(POSTGRES_URL, sslcert="client.crt")
    with pytest.raises(ValueError, match="separate settings"):
        djox.Database.postgres(url_with_host("localhost") + "?sslmode=verify-full")
