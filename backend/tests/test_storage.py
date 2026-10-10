"""Security-focused tests for the artifact storage layer.

The threat model: artifacts are a customer's most sensitive data (screenshots of
their app, recordings, their .ipa). These tests assert the guarantees that keep
them private — signed, time-limited access and no path traversal.
"""

import time

import pytest

from tamam.storage import LocalArtifactStorage, S3ArtifactStorage, _validate_key


def _store(tmp_path) -> LocalArtifactStorage:
    return LocalArtifactStorage(root=tmp_path, secret=b"test-secret-32-bytes-long-xxxxxx", ttl_s=3600)


# ---------- key validation (path traversal) ----------

@pytest.mark.parametrize(
    "bad",
    [
        "../etc/passwd",
        "runs/../../etc/passwd",
        "/etc/passwd",
        "runs/../secret",
        "",
        "runs/\x00/x",
        "a b/c",  # space
    ],
)
def test_validate_key_rejects_traversal_and_junk(bad):
    with pytest.raises(ValueError):
        _validate_key(bad)


def test_validate_key_accepts_our_keys():
    for good in ["runs/abc-123/step-4.png", "builds/proj-1/app.ipa", "runs/x/recording.mp4"]:
        assert _validate_key(good) == good


async def test_local_store_blocks_escape_on_access(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        await store.get_bytes("../outside.txt")


# ---------- signed URL: unforgeable + time-limited ----------

async def test_roundtrip_and_signed_url_shape(tmp_path):
    store = _store(tmp_path)
    src = tmp_path / "src.png"
    src.write_bytes(b"\x89PNG data")
    await store.put(src, "runs/r1/step-1.png")
    assert (tmp_path / "runs/r1/step-1.png").read_bytes() == b"\x89PNG data"

    url = store.signed_url("runs/r1/step-1.png")
    assert url.startswith("/api/artifacts/runs/r1/step-1.png?exp=")
    assert "sig=" in url


def test_signature_verifies(tmp_path):
    store = _store(tmp_path)
    exp = int(time.time()) + 60
    sig = store._sign("runs/r1/step-1.png", exp)
    assert store.verify("runs/r1/step-1.png", exp, sig) is True


def test_tampered_signature_rejected(tmp_path):
    store = _store(tmp_path)
    exp = int(time.time()) + 60
    sig = store._sign("runs/r1/step-1.png", exp)
    assert store.verify("runs/r1/step-1.png", exp, "not-a-real-signature") is False
    assert store.verify("runs/r1/step-1.png", exp, "0" * len(sig)) is False
    # cannot swap the key under a signature minted for a different one
    assert store.verify("runs/r1/OTHER.png", exp, sig) is False
    # cannot extend the expiry
    assert store.verify("runs/r1/step-1.png", exp + 10_000, sig) is False


def test_expired_signature_rejected(tmp_path):
    store = _store(tmp_path)
    past = int(time.time()) - 1
    sig = store._sign("runs/r1/step-1.png", past)
    assert store.verify("runs/r1/step-1.png", past, sig) is False


def test_different_secret_cannot_forge(tmp_path):
    store = _store(tmp_path)
    forger = LocalArtifactStorage(root=tmp_path, secret=b"attacker-secret-different-000000", ttl_s=3600)
    exp = int(time.time()) + 60
    forged = forger._sign("runs/r1/step-1.png", exp)
    assert store.verify("runs/r1/step-1.png", exp, forged) is False


# ---------- delete: right-to-deletion (the binary is actually gone) ----------

async def test_local_delete_removes_file(tmp_path):
    store = _store(tmp_path)
    src = tmp_path / "src.ipa"
    src.write_bytes(b"PK\x03\x04build")
    await store.put(src, "builds/p1/app.ipa")
    assert (tmp_path / "builds/p1/app.ipa").exists()

    await store.delete("builds/p1/app.ipa")
    assert not (tmp_path / "builds/p1/app.ipa").exists()


async def test_local_delete_is_idempotent(tmp_path):
    """Deleting an already-gone object must not raise — right-to-deletion cannot
    fail just because the binary is already absent."""
    store = _store(tmp_path)
    await store.delete("builds/p1/never-existed.ipa")  # no error


async def test_local_delete_blocks_escape(tmp_path):
    """delete goes through the same key validation as read — no traversal."""
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        await store.delete("../outside.ipa")


async def test_s3_delete_calls_delete_object(tmp_path):
    calls = {}

    class FakeClient:
        def delete_object(self, Bucket, Key):
            calls["bucket"] = Bucket
            calls["key"] = Key
            return {}

    store = S3ArtifactStorage(
        endpoint_url="http://x", bucket="b", region="auto",
        access_key_id="k", secret_access_key="s", ttl_s=60,
    )
    store._S3ArtifactStorage__client = FakeClient()  # skip lazy boto3 init
    await store.delete("builds/p1/app.ipa")
    assert calls == {"bucket": "b", "key": "builds/p1/app.ipa"}


async def test_s3_delete_validates_key():
    store = S3ArtifactStorage(
        endpoint_url="http://x", bucket="b", region="auto",
        access_key_id="k", secret_access_key="s", ttl_s=60,
    )
    with pytest.raises(ValueError):
        await store.delete("../../etc/passwd")


# ---------- S3 upload sets a content type (the "recording stays preparing" bug) ----------

async def test_s3_put_sets_content_type(tmp_path, monkeypatch):
    """boto3.upload_file infers no content type; without ExtraArgs objects store as
    binary/octet-stream and the browser won't play a recording. Assert we pass the
    guessed type for the artifacts we produce."""
    calls = {}

    class FakeClient:
        def upload_file(self, filename, bucket, key, ExtraArgs=None):
            calls[key] = ExtraArgs

    store = S3ArtifactStorage(
        endpoint_url="http://x", bucket="b", region="auto",
        access_key_id="k", secret_access_key="s", ttl_s=60,
    )
    monkeypatch.setattr(store, "_client", lambda: FakeClient())

    src = tmp_path / "f"
    src.write_bytes(b"x")
    await store.put(src, "runs/r/recording.mp4")
    await store.put(src, "runs/r/step-1.png")

    assert calls["runs/r/recording.mp4"] == {"ContentType": "video/mp4"}
    assert calls["runs/r/step-1.png"] == {"ContentType": "image/png"}
