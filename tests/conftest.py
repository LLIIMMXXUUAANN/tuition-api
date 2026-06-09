import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

AUTH = {"X-Internal-Secret": settings.internal_api_secret}


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def auth_headers():
    return AUTH
