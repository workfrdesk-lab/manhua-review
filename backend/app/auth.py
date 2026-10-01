from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.db import get_db
from app.models import Session, User
from app.rate_limit import enforce_auth_limit
from app.security import (
    expiry,
    hash_password,
    new_token,
    normalize_email,
    token_digest,
    verify_password,
)


def require_origin(request: Request) -> None:
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if request.headers.get("origin") not in get_settings().allowed_origins:
            raise HTTPException(status_code=403, detail="Untrusted origin")


router = APIRouter(prefix="/api/v1/auth", tags=["auth"], dependencies=[Depends(require_origin)])
DUMMY_HASH = hash_password(new_token())


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)
    display_name: str | None = Field(default=None, min_length=1, max_length=120)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    display_name: str | None
    is_admin: bool
    created_at: datetime


def set_auth_cookies(response: Response, session_token: str, csrf_token: str) -> None:
    settings = get_settings()
    common = {
        "secure": settings.secure_cookies or settings.app_env == "production",
        "samesite": "lax",
        "path": "/",
        "max_age": settings.session_ttl_days * 86400,
    }
    response.set_cookie(settings.session_cookie_name, session_token, httponly=True, **common)
    response.set_cookie(settings.csrf_cookie_name, csrf_token, **common)


def clear_auth_cookies(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")


async def create_session(
    request: Request, response: Response, db: AsyncSession, user: User
) -> None:
    settings = get_settings()
    raw_token = new_token()
    csrf_token = new_token()
    db.add(
        Session(
            user_id=user.id,
            token_hash=token_digest(raw_token),
            csrf_hash=token_digest(csrf_token),
            expires_at=expiry(settings.session_ttl_days),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent", "")[:512],
        )
    )
    await db.commit()
    set_auth_cookies(response, raw_token, csrf_token)


async def current_session(request: Request, db: AsyncSession) -> Session:
    settings = get_settings()
    raw_token = request.cookies.get(settings.session_cookie_name)
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required"
        )
    result = await db.execute(
        select(Session)
        .where(Session.token_hash == token_digest(raw_token))
        .where(Session.expires_at > datetime.now(UTC))
    )
    session = result.scalar_one_or_none()
    if not session or not session.user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required"
        )
    return session


async def require_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    return (await current_session(request, db)).user


async def require_csrf(request: Request, db: AsyncSession = Depends(get_db)) -> None:
    settings = get_settings()
    cookie = request.cookies.get(settings.csrf_cookie_name)
    header = request.headers.get("x-csrf-token")
    if not cookie or not header or not secrets_compare(cookie, header):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF validation failed")
    session = await current_session(request, db)
    if not secrets_compare(session.csrf_hash, token_digest(header)):
        raise HTTPException(status_code=403, detail="CSRF validation failed")


def secrets_compare(left: str, right: str) -> bool:
    import secrets

    return secrets.compare_digest(left, right)


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(enforce_auth_limit)],
)
async def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    email = normalize_email(str(payload.email))
    existing = await db.scalar(select(User).where(User.email == email))
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")
    hashed = await run_in_threadpool(hash_password, payload.password)
    user = User(email=email, password_hash=hashed, display_name=payload.display_name)
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Email already registered") from None
    await create_session(request, response, db, user)
    await db.refresh(user)
    return user


@router.post("/login", response_model=UserResponse, dependencies=[Depends(enforce_auth_limit)])
async def login(
    payload: LoginRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)
):
    email = normalize_email(str(payload.email))
    user = await db.scalar(select(User).where(User.email == email))
    valid = await run_in_threadpool(
        verify_password, payload.password, user.password_hash if user else DUMMY_HASH
    )
    if not user or not valid or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password"
        )
    await create_session(request, response, db, user)
    return user


@router.get("/me", response_model=UserResponse)
async def me(user: User = Depends(require_user)):
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_csrf),
):
    settings = get_settings()
    raw_token = request.cookies.get(settings.session_cookie_name)
    if raw_token:
        await db.execute(delete(Session).where(Session.token_hash == token_digest(raw_token)))
        await db.commit()
    clear_auth_cookies(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return None
