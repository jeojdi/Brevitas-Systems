import base64
import json
from dataclasses import replace

import pytest
from cryptography.fernet import Fernet

from api.auth import constant_time_equal, hash_key, verify_key_hash
from api.security import credential_cipher_from_environment
from brevitas.security import (
    BoundedTTLKeyCache,
    EnvelopeCipher,
    EnvelopeDecryptionError,
    EnvelopeError,
    ExternalManagedKMS,
    KMSConfigurationError,
    KMSUnavailable,
    LegacyFernetDecryptor,
    LocalTestKMS,
    RotationRecord,
    WrappedDataKey,
    kms_from_environment,
    redact,
    redact_exception,
    rotate_envelopes,
)


class MockManagedKMS(LocalTestKMS):
    provider = "mock-cloud-kms"
    is_managed = True
    algorithm = "MOCK-KMS-AEAD"

    def __init__(self, key=b"m" * 32):
        super().__init__(key, environ={"BREVITAS_ENV": "test"})
        self.available = True
        self.unwrap_calls = 0

    def wrap_data_key(self, **kwargs):
        if not self.available:
            raise RuntimeError("provider SDK leaked sk-real-secret-value")
        return super().wrap_data_key(**kwargs)

    def unwrap_data_key(self, wrapped_key, *, encryption_context):
        self.unwrap_calls += 1
        if not self.available:
            raise RuntimeError("provider SDK leaked sk-real-secret-value")
        return super().unwrap_data_key(
            wrapped_key, encryption_context=encryption_context
        )


def cipher(kms=None, *, version="7", legacy=None, cache=None):
    kms = kms or MockManagedKMS()
    return EnvelopeCipher(
        kms,
        key_id="credential-primary",
        key_version=version,
        wrap_algorithm=kms.algorithm,
        legacy_decryptor=legacy,
        cache=cache,
    )


def decode_document(value):
    encoded = value.split(":", 2)[2]
    return json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))


def encode_document(document):
    raw = json.dumps(document, separators=(",", ":"), sort_keys=True).encode()
    return "bvt-envelope:v1:" + base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_envelope_round_trip_has_versioned_kms_metadata_without_plaintext():
    service = cipher()
    encrypted = service.encrypt_text(
        "sk-provider-private-value", context={"organization_id": "org-1"}
    )

    assert service.decrypt_text(
        encrypted, context={"organization_id": "org-1"}
    ) == "sk-provider-private-value"
    assert "sk-provider-private-value" not in encrypted
    metadata = service.inspect_metadata(encrypted)
    assert metadata == {
        "schema": "brevitas.envelope",
        "version": 1,
        "key_provider": "mock-cloud-kms",
        "key_id": "credential-primary",
        "key_version": "7",
        "wrap_algorithm": "MOCK-KMS-AEAD",
        "data_algorithm": "AES-256-GCM",
        "context_digest": metadata["context_digest"],
    }
    assert len(metadata["context_digest"]) == 64


def test_tamper_and_context_swaps_fail_closed_without_secret_errors():
    service = cipher()
    encrypted = service.encrypt_text("bvt_private_value", context={"tenant": "one"})
    document = decode_document(encrypted)
    payload = bytearray(base64.urlsafe_b64decode(
        document["ciphertext"] + "=" * (-len(document["ciphertext"]) % 4)
    ))
    payload[-1] ^= 1
    document["ciphertext"] = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()

    with pytest.raises(EnvelopeDecryptionError) as caught:
        service.decrypt_text(encode_document(document), context={"tenant": "one"})
    assert "bvt_private_value" not in str(caught.value)
    with pytest.raises(EnvelopeDecryptionError):
        service.decrypt_text(encrypted, context={"tenant": "two"})
    with pytest.raises(EnvelopeError, match="context is invalid"):
        service.encrypt_text("credential", context={"tenant": "one\nother"})


def test_credential_plaintext_has_a_hard_encryption_size_limit():
    with pytest.raises(EnvelopeError, match="size limit"):
        cipher().encrypt_bytes(b"x" * (1024 * 1024 + 1))


def test_kms_outage_is_content_free_and_uncached_decryption_fails_closed():
    kms = MockManagedKMS()
    service = cipher(kms)
    encrypted = service.encrypt_text("credential", context={"record": "1"})
    kms.available = False

    with pytest.raises(KMSUnavailable) as encrypt_error:
        service.encrypt_text("other credential")
    assert "sk-real-secret-value" not in str(encrypt_error.value)
    with pytest.raises(KMSUnavailable) as decrypt_error:
        service.decrypt_text(encrypted, context={"record": "1"})
    assert "sk-real-secret-value" not in str(decrypt_error.value)


def test_external_adapter_collapses_provider_exceptions():
    adapter = ExternalManagedKMS(
        "vendor-kms",
        wrap=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("Bearer private")),
        unwrap=lambda **_kwargs: b"x" * 32,
    )
    with pytest.raises(KMSUnavailable, match="managed KMS wrap operation unavailable") as caught:
        adapter.wrap_data_key(
            key_id="key", key_version="1", plaintext_key=b"d" * 32,
            encryption_context={},
        )
    assert "private" not in str(caught.value)


def test_production_requires_real_injected_managed_kms_and_complete_identity():
    production = {
        "BREVITAS_ENV": "production",
        "BREVITAS_KMS_REQUIRED": "true",
        "BREVITAS_KMS_PROVIDER": "mock-cloud-kms",
        "BREVITAS_KMS_KEY_ID": "credential-primary",
        "BREVITAS_KMS_KEY_VERSION": "7",
    }
    with pytest.raises(KMSConfigurationError, match="adapter is unavailable"):
        kms_from_environment(environ=production)
    with pytest.raises(KMSConfigurationError, match="forbidden in production"):
        LocalTestKMS(b"x" * 32, environ=production)

    adapter, settings = kms_from_environment(
        adapter=MockManagedKMS(), environ=production
    )
    assert adapter.is_managed is True
    assert settings.required is True


@pytest.mark.parametrize("version", [
    "latest", "CURRENT", "active", "default", "alias/credential-key", "v7-latest",
])
def test_production_rejects_ambiguous_or_alias_key_versions(version):
    production = {
        "BREVITAS_ENV": "production",
        "BREVITAS_KMS_PROVIDER": "mock-cloud-kms",
        "BREVITAS_KMS_KEY_ID": "arn:vendor:kms:us-east-1:123:key/credential",
        "BREVITAS_KMS_KEY_VERSION": version,
        "BREVITAS_KMS_ALGORITHM": "MOCK-KMS-AEAD",
    }
    with pytest.raises(KMSConfigurationError, match="must be immutable"):
        kms_from_environment(adapter=MockManagedKMS(), environ=production)


@pytest.mark.parametrize(("field", "unsafe"), [
    ("BREVITAS_KMS_PROVIDER", "mock-cloud-kms\nsecret"),
    ("BREVITAS_KMS_KEY_ID", "credential key with spaces"),
    ("BREVITAS_KMS_KEY_VERSION", "7\nnext"),
    ("BREVITAS_KMS_ALGORITHM", "MOCK KMS AEAD"),
])
def test_kms_configuration_bounds_and_sanitizes_metadata(field, unsafe):
    production = {
        "BREVITAS_ENV": "production",
        "BREVITAS_KMS_PROVIDER": "mock-cloud-kms",
        "BREVITAS_KMS_KEY_ID": "credential-primary",
        "BREVITAS_KMS_KEY_VERSION": "7",
        "BREVITAS_KMS_ALGORITHM": "MOCK-KMS-AEAD",
        field: unsafe,
    }
    with pytest.raises(KMSConfigurationError) as caught:
        kms_from_environment(adapter=MockManagedKMS(), environ=production)
    assert unsafe not in str(caught.value)


@pytest.mark.parametrize(("field", "replacement"), [
    ("algorithm", "UNEXPECTED-WRAP"),
    ("key_version", "8"),
])
def test_adapter_returned_key_metadata_must_match_explicit_active_settings(field, replacement):
    kms = MockManagedKMS()
    actual_wrap = kms.wrap_data_key

    def mismatched_wrap(**kwargs):
        return replace(actual_wrap(**kwargs), **{field: replacement})

    kms.wrap_data_key = mismatched_wrap
    with pytest.raises(KMSUnavailable, match="inconsistent key metadata"):
        cipher(kms).encrypt_text("credential")


def test_environment_configured_wrap_algorithm_is_enforced_on_adapter_output():
    kms = MockManagedKMS()
    production = {
        "BREVITAS_ENV": "production",
        "BREVITAS_KMS_PROVIDER": "mock-cloud-kms",
        "BREVITAS_KMS_KEY_ID": "credential-primary",
        "BREVITAS_KMS_KEY_VERSION": "7",
        "BREVITAS_KMS_ALGORITHM": "CONFIGURED-WRAP",
    }
    service = credential_cipher_from_environment(adapter=kms, environ=production)
    with pytest.raises(KMSUnavailable, match="inconsistent key metadata"):
        service.encrypt_text("credential")


def test_environment_factory_never_generates_an_implicit_local_master_key():
    local = {
        "BREVITAS_ENV": "development",
        "BREVITAS_KMS_PROVIDER": "local-test",
        "BREVITAS_KMS_KEY_ID": "development-only",
        "BREVITAS_KMS_KEY_VERSION": "1",
    }
    with pytest.raises(KMSConfigurationError, match="explicit local test"):
        credential_cipher_from_environment(environ=local)

    local["BREVITAS_LOCAL_KMS_KEY"] = base64.b64encode(b"l" * 32).decode()
    service = credential_cipher_from_environment(environ=local)
    encrypted = service.encrypt_text("local-only")
    assert service.decrypt_text(encrypted) == "local-only"


def test_legacy_fernet_is_decrypt_only_and_rotation_reencrypts_to_current_key():
    legacy_key = Fernet.generate_key()
    legacy_value = Fernet(legacy_key).encrypt(b"legacy-secret").decode()
    service = cipher(legacy=LegacyFernetDecryptor([legacy_key]))

    result = service.decrypt_with_metadata(legacy_value)
    assert result.plaintext == b"legacy-secret"
    assert result.needs_rotation is True
    replacement = service.reencrypt(legacy_value)
    assert replacement.startswith("bvt-envelope:v1:")
    assert service.decrypt_text(replacement) == "legacy-secret"

    with pytest.raises(EnvelopeError):
        service.decrypt_text("legacy-secret")


def test_rotation_workflow_supports_dry_run_and_bounded_persistence():
    kms = MockManagedKMS()
    old = cipher(kms, version="6")
    current = cipher(kms, version="7")
    record = RotationRecord(old.encrypt_text("rotate-me"), {})

    planned = rotate_envelopes([record], cipher=current, dry_run=True)
    assert planned.reencrypted == 1
    persisted = []
    applied = rotate_envelopes(
        [record], cipher=current, dry_run=False,
        persist=lambda position, value: persisted.append((position, value)),
    )
    assert applied.reencrypted == 1
    assert current.decrypt_text(persisted[0][1]) == "rotate-me"
    assert current.decrypt_with_metadata(persisted[0][1]).needs_rotation is False


def test_data_key_cache_has_hard_size_ttl_and_lru_bounds():
    now = [100.0]
    cache = BoundedTTLKeyCache(max_entries=2, ttl_seconds=5, clock=lambda: now[0])
    cache.put("one", b"1" * 32)
    cache.put("two", b"2" * 32)
    assert cache.get("one") == b"1" * 32
    cache.put("three", b"3" * 32)
    assert len(cache) == 2
    assert cache.get("two") is None
    now[0] += 6
    assert cache.get("one") is None
    assert len(cache) == 0
    with pytest.raises(ValueError):
        BoundedTTLKeyCache(max_entries=1025, ttl_seconds=5)
    with pytest.raises(ValueError):
        BoundedTTLKeyCache(max_entries=2, ttl_seconds=901)


def test_cached_data_keys_expire_before_kms_outage_can_be_hidden():
    now = [10.0]
    kms = MockManagedKMS()
    cache = BoundedTTLKeyCache(max_entries=2, ttl_seconds=2, clock=lambda: now[0])
    service = cipher(kms, cache=cache)
    encrypted = service.encrypt_text("credential")
    assert service.decrypt_text(encrypted) == "credential"
    assert kms.unwrap_calls == 1
    kms.available = False
    assert service.decrypt_text(encrypted) == "credential"
    now[0] += 3
    with pytest.raises(KMSUnavailable):
        service.decrypt_text(encrypted)


def test_recursive_redaction_covers_headers_urls_sequences_and_exceptions():
    error = RuntimeError("request failed with Bearer real-access-token")
    error.request = {
        "headers": {
            "Authorization": "Bearer secret",
            "X-Brevitas-Key": "bvt_super_private",
            "Content-Type": "application/json",
        },
        "url": "https://user:password@example.com/v1/whsec_private_value?token=secret&page=2#fragment",
    }
    value = {
        "provider_api_key": "sk-provider-secret",
        "nested": [
            {"password": "guess-me", "safe": "ok"},
            ("Bearer another-secret", error),
        ],
    }
    cleaned = redact(value)
    encoded = json.dumps(cleaned)

    for secret in (
        "real-access-token", "bvt_super_private", "sk-provider-secret",
        "guess-me", "another-secret", "password@example", "whsec_private_value", "token=secret",
    ):
        assert secret not in encoded
    assert cleaned["nested"][0]["safe"] == "ok"
    assert cleaned["nested"][1][1]["type"] == "RuntimeError"
    assert cleaned["nested"][1][1]["attributes"]["request"]["headers"]["Content-Type"] == "application/json"


def test_recursive_redaction_collapses_attacker_controlled_mapping_and_query_keys():
    jwt = "abcdefgh.ijklmnop.qrstuvwx"
    cleaned = redact({
        "authorization=Bearer mapping-secret": "value",
        jwt: "value",
        "https://user@example.com/path?token=secret": "value",
        "safe_field": "safe-value",
    })
    encoded = json.dumps(cleaned)
    assert "mapping-secret" not in encoded
    assert jwt not in encoded
    assert "token=secret" not in encoded
    assert "[REDACTED_KEY]" in cleaned
    assert cleaned["safe_field"] == "safe-value"

    safe_url = redact({
        "url": (
            "https://example.com/path?Bearer%20query-secret=value&"
            f"{jwt}=value&token=secret&page=2"
        ),
    })["url"]
    assert "query-secret" not in safe_url
    assert jwt not in safe_url
    assert "token" not in safe_url
    assert "secret" not in safe_url
    assert "page=2" in safe_url


def test_redaction_allowlist_never_overrides_secret_field_detection():
    cleaned = redact(
        {"status_code": 503, "provider": "openai", "detail": "private", "token": "x"},
        safe_fields={"status_code", "provider", "token"},
    )
    assert cleaned == {
        "status_code": 503,
        "provider": "openai",
        "detail": "[REDACTED]",
        "token": "[REDACTED]",
    }
    exception = redact_exception(ValueError("api_key=sk-top-secret"))
    assert "sk-top-secret" not in json.dumps(exception)


def test_api_key_and_internal_secret_comparisons_use_safe_contracts():
    raw = "bvt_test_value"
    assert verify_key_hash(raw, hash_key(raw)) is True
    assert verify_key_hash(raw, "not-a-hash") is False
    assert constant_time_equal("internal-token", "internal-token") is True
    assert constant_time_equal("internal-token", "different-token") is False
    assert constant_time_equal("internal-token", b"internal-token") is False


def test_browser_sources_do_not_reference_server_supabase_or_provider_env_secrets():
    sources = [
        open("src/lib/supabase.ts", encoding="utf-8").read(),
        open("dashboard/src/lib/supabase.js", encoding="utf-8").read(),
    ]
    forbidden = (
        "SUPABASE_" + "SERVICE_ROLE_KEY",
        "STRIPE_" + "SECRET_KEY",
        "OPENAI_" + "API_KEY",
        "ANTHROPIC_" + "API_KEY",
    )
    for source in sources:
        assert all(name not in source for name in forbidden)
