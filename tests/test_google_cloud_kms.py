from types import SimpleNamespace

import pytest

from brevitas.security import KMSConfigurationError, KMSUnavailable, WrappedDataKey
from brevitas.security.google_cloud_kms import (
    ALGORITHM,
    GoogleCloudKMS,
    PROVIDER,
)


KEY_ID = (
    "projects/divine-camera-465917-j7/locations/global/"
    "keyRings/brevitas-staging/cryptoKeys/credential-envelope"
)


def crc32c(value: bytes) -> int:
    return sum(value) % (2**32)


class FakeClient:
    def __init__(self) -> None:
        self.encrypt_request = None
        self.decrypt_request = None

    def encrypt(self, *, request, retry, timeout):
        self.encrypt_request = (request, retry, timeout)
        ciphertext = b"google-wrapped-data-key"
        return SimpleNamespace(
            name=f"{KEY_ID}/cryptoKeyVersions/1",
            ciphertext=ciphertext,
            ciphertext_crc32c=crc32c(ciphertext),
            verified_plaintext_crc32c=True,
            verified_additional_authenticated_data_crc32c=True,
        )

    def decrypt(self, *, request, retry, timeout):
        self.decrypt_request = (request, retry, timeout)
        plaintext = b"p" * 32
        return SimpleNamespace(
            plaintext=plaintext,
            plaintext_crc32c=crc32c(plaintext),
        )


def test_google_cloud_kms_wraps_an_immutable_version_with_aad_and_checksums():
    client = FakeClient()
    adapter = GoogleCloudKMS(client, crc32c=crc32c, timeout_seconds=0.5)

    wrapped = adapter.wrap_data_key(
        key_id=KEY_ID,
        key_version="1",
        plaintext_key=b"p" * 32,
        encryption_context={"schema": "v1", "purpose": "credential"},
    )

    assert wrapped == WrappedDataKey(
        PROVIDER, KEY_ID, "1", ALGORITHM, b"google-wrapped-data-key"
    )
    request, retry, timeout = client.encrypt_request
    assert request["name"] == f"{KEY_ID}/cryptoKeyVersions/1"
    assert request["additional_authenticated_data"] == (
        b'{"purpose":"credential","schema":"v1"}'
    )
    assert request["plaintext_crc32c"] == crc32c(b"p" * 32)
    assert request["additional_authenticated_data_crc32c"] == crc32c(
        request["additional_authenticated_data"]
    )
    assert retry is None
    assert timeout == 0.5


def test_google_cloud_kms_unwraps_using_the_key_resource_and_same_aad():
    client = FakeClient()
    adapter = GoogleCloudKMS(client, crc32c=crc32c)
    wrapped = WrappedDataKey(
        PROVIDER, KEY_ID, "1", ALGORITHM, b"google-wrapped-data-key"
    )

    assert adapter.unwrap_data_key(
        wrapped,
        encryption_context={"purpose": "credential", "schema": "v1"},
    ) == b"p" * 32
    request, retry, timeout = client.decrypt_request
    assert request["name"] == KEY_ID
    assert request["additional_authenticated_data"] == (
        b'{"purpose":"credential","schema":"v1"}'
    )
    assert request["ciphertext_crc32c"] == crc32c(wrapped.ciphertext)
    assert retry is None
    assert timeout == 0.75


def test_google_cloud_kms_rejects_ambiguous_versions_and_wrong_metadata():
    adapter = GoogleCloudKMS(FakeClient(), crc32c=crc32c)
    with pytest.raises(KMSConfigurationError, match="key version is invalid"):
        adapter.wrap_data_key(
            key_id=KEY_ID,
            key_version="latest",
            plaintext_key=b"p" * 32,
            encryption_context={},
        )
    with pytest.raises(KMSConfigurationError, match="wrapped key is invalid"):
        adapter.unwrap_data_key(
            WrappedDataKey("aws-kms", KEY_ID, "1", ALGORITHM, b"ciphertext"),
            encryption_context={},
        )


def test_google_cloud_kms_fails_closed_on_response_integrity_mismatch():
    class WrongVersionClient(FakeClient):
        def encrypt(self, *, request, retry, timeout):
            response = super().encrypt(request=request, retry=retry, timeout=timeout)
            response.name = f"{KEY_ID}/cryptoKeyVersions/2"
            return response

    adapter = GoogleCloudKMS(WrongVersionClient(), crc32c=crc32c)
    with pytest.raises(KMSUnavailable, match="wrap integrity check failed"):
        adapter.wrap_data_key(
            key_id=KEY_ID,
            key_version="1",
            plaintext_key=b"p" * 32,
            encryption_context={},
        )


def test_google_cloud_kms_collapses_provider_exceptions():
    class BrokenClient(FakeClient):
        def encrypt(self, *, request, retry, timeout):
            raise RuntimeError("credential-bearing provider detail")

    adapter = GoogleCloudKMS(BrokenClient(), crc32c=crc32c)
    with pytest.raises(KMSUnavailable) as error:
        adapter.wrap_data_key(
            key_id=KEY_ID,
            key_version="1",
            plaintext_key=b"p" * 32,
            encryption_context={},
        )
    assert "credential-bearing" not in str(error.value)
