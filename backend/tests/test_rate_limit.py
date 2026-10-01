from unittest.mock import AsyncMock, patch

import pytest
from redis.exceptions import ConnectionError

from app.main import app
from app.rate_limit import enforce_auth_limit


@pytest.mark.parametrize(
    ("result", "expected"), [([1, 60], 401), ([31, 42], 429), (ConnectionError(), 503)]
)
@pytest.mark.parametrize("endpoint", ["login", "register"])
def test_limiter(client, result, expected, endpoint):
    app.dependency_overrides.pop(enforce_auth_limit)
    redis = AsyncMock()
    if isinstance(result, Exception):
        redis.eval.side_effect = result
    else:
        redis.eval.return_value = result
    context = AsyncMock()
    context.__aenter__.return_value = redis
    with patch("app.rate_limit.Redis.from_url", return_value=context):
        response = client.post(
            f"/api/v1/auth/{endpoint}",
            json={"email": "limit@example.com", "password": "correct horse battery staple"},
        )
    if endpoint == "register" and expected == 401:
        expected = 201
    assert response.status_code == expected
    assert response.headers["content-type"].startswith("application/json")
    if expected == 429:
        assert response.headers["retry-after"] == "42"
    redis.eval.assert_awaited_once()
