from collections.abc import AsyncGenerator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings


def database_url_for_sqlalchemy(value: str) -> str:
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+psycopg://", 1)
    if value.startswith("sqlite://") and "+aiosqlite" not in value:
        return value.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return value


def build_session_factory(settings: Settings) -> tuple[object, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        database_url_for_sqlalchemy(settings.database_url.get_secret_value())
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    session_factory = request.app.state.db_session_factory
    async with session_factory() as session:
        yield session
