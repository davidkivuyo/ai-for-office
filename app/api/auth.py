from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import get_current_user
from app.auth.service import create_access_token, hash_password, verify_password
from app.db.models import User
from app.db.repositories import create_user, get_user_by_username
from app.db.session import get_db
from app.schemas.user import TokenOut, UserCreate, UserLogin, UserOut

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=TokenOut, status_code=201)
async def register(payload: UserCreate, db: AsyncSession = Depends(get_db)):
    existing = await get_user_by_username(db, payload.username)
    if existing:
        raise HTTPException(status_code=400, detail="Username already exists")
    hashed = hash_password(payload.password)
    user = await create_user(db, username=payload.username, display_name=payload.display_name or payload.username, password_hash=hashed)
    await db.commit()
    await db.refresh(user)
    token = create_access_token(subject=user.id)
    return TokenOut(access_token=token, user=UserOut.model_validate(user))


@router.post("/login", response_model=TokenOut)
async def login(payload: UserLogin, db: AsyncSession = Depends(get_db)):
    user = await get_user_by_username(db, payload.username)
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User inactive")
    token = create_access_token(subject=user.id)
    return TokenOut(access_token=token, user=UserOut.model_validate(user))


@router.get("/me", response_model=UserOut)
async def me(current: User = Depends(get_current_user)):
    return UserOut.model_validate(current)
