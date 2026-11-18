import pytest

from itsdangerous import URLSafeSerializer, URLSafeTimedSerializer


@pytest.mark.parametrize("serializer", [URLSafeSerializer, URLSafeTimedSerializer])
@pytest.mark.parametrize("payload", [{"id": 42}, "a" * 1000])
def test_url_safe_round_trip(serializer, payload):
    codec = serializer("fixture-key")
    token = codec.dumps(payload)
    assert codec.loads(token) == payload


# Captured from ItsDangerous 2.2.0 with fixture-key and payload "a" * 64.
# The timed signer used Unix timestamp 1735689600.
@pytest.mark.parametrize(
    ("serializer", "token"),
    [
        (URLSafeSerializer, ".eJxTSqQQKAEANbUYhQ.JUiRb-uB8DPnQ7njmW-zHBYuxKQ"),
        (URLSafeTimedSerializer, ".eJxTSqQQKAEANbUYhQ.Z3SFgA.OGHpfI9jHAHjf7tXj6DK3MfrMds"),
    ],
    ids=["untimed", "timed"],
)
def test_loads_compressed_payload_from_2_2_0(serializer, token):
    assert serializer("fixture-key").loads(token) == "a" * 64
