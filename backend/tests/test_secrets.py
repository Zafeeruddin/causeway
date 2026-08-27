import pytest

from app.security.secrets import MemoryBackend, SealedColumnBackend, SecretNotFound, SecretsError

KEY = b"0" * 32


async def test_sealed_round_trip():
    backend = SealedColumnBackend(KEY)
    ref = await backend.put("hunter2")
    assert "hunter2" not in ref
    assert ref.startswith("local:v1:")
    assert await backend.get(ref) == "hunter2"


async def test_same_plaintext_seals_differently_each_time():
    backend = SealedColumnBackend(KEY)
    assert await backend.put("x") != await backend.put("x")


async def test_a_changed_key_reports_something_actionable():
    ref = await SealedColumnBackend(KEY).put("hunter2")
    with pytest.raises(SecretsError, match="SECRETS_KEY has probably changed"):
        await SealedColumnBackend(b"1" * 32).get(ref)


async def test_a_vault_ref_is_refused_by_the_local_backend():
    """When Vault lands (ROADMAP entry 3) both ref schemes coexist during migration."""
    with pytest.raises(SecretNotFound):
        await SealedColumnBackend(KEY).get("vault:kv/cam-dashboard/acme/abc")


async def test_memory_backend_matches_the_interface():
    backend = MemoryBackend()
    ref = await backend.put("s3cret")
    assert await backend.get(ref) == "s3cret"
    await backend.delete(ref)
    with pytest.raises(SecretNotFound):
        await backend.get(ref)
