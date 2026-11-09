import pytest

from itsdangerous import URLSafeSerializer, URLSafeTimedSerializer


@pytest.mark.parametrize("serializer", [URLSafeSerializer, URLSafeTimedSerializer])
@pytest.mark.parametrize("payload", [{"id": 42}, "a" * 1000])
def test_url_safe_round_trip(serializer, payload):
    codec = serializer("fixture-key")
    token = codec.dumps(payload)
    assert codec.loads(token) == payload
