from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


url = settings.database_url
if url.startswith('postgres://'):
    url = url.replace('postgres://', 'postgresql+psycopg://', 1)
elif url.startswith('postgresql://'):
    url = url.replace('postgresql://', 'postgresql+psycopg://', 1)
if url.startswith('sqlite:'):
    Path('data').mkdir(exist_ok=True)
engine = create_engine(url, connect_args={'check_same_thread': False, 'timeout': 30} if url.startswith('sqlite:') else {}, pool_pre_ping=True)
if url.startswith('sqlite:'):
    @event.listens_for(engine, 'connect')
    def sqlite_config(connection, _):
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA journal_mode=WAL')

SessionLocal = sessionmaker(engine, expire_on_commit=False)


def get_db():
    with SessionLocal() as db:
        yield db
