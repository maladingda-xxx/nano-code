import pytest

import itsdangerous
from itsdangerous.encoding import base64_encode


def test_base64_encoded_size_matches_encoding():
    size = getattr(itsdangerous, "base64_encoded_size", None)
    assert callable(size)
    for length in range(12):
        assert size(length) == len(base64_encode(b"a" * length))
    with pytest.raises(ValueError):
        size(-1)
