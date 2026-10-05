import asyncio
import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

Path('tmp').mkdir(exist_ok=True)
os.environ['DATABASE_URL'] = f'sqlite:///./tmp/test-{uuid.uuid4().hex}.db'
os.environ['APP_ENV'] = 'development'
os.environ['BETA_ENABLED'] = 'true'

from app import main
from app.auth import limiter
from app.config import settings
from app.db import Base, SessionLocal, engine


@pytest.fixture
def client(monkeypatch):
    async def dormant_worker():
        await asyncio.Event().wait()
    monkeypatch.setattr(main, 'assignment_worker', dormant_worker)
    monkeypatch.setattr(settings, 'gemini_key', '')
    monkeypatch.setattr(settings, 'openai_key', '')
    monkeypatch.setattr(settings, 'ai_provider', 'gemini')
    monkeypatch.setattr(settings, 'beta_enabled', True)
    limiter.hits.clear()
    Base.metadata.drop_all(engine)
    with TestClient(main.app) as instance:
        yield instance


def register(client, email='student@example.test'):
    response = client.post('/api/auth/register', json={'email': email, 'password': 'test-password-123', 'name': 'Test Student'})
    assert response.status_code == 200, response.text
    user = response.json()
    client.headers['X-CSRF-Token'] = user['csrf']
    return user


def enroll(client, course='ucsd-math-183', **kwargs):
    kwargs.setdefault('mode', 'catalog')
    response = client.post('/api/courses', json={'course_id': course, **kwargs})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def student(client):
    return register(client)
