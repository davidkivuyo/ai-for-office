from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Conversation, Message, User


# --- users ---
async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    result = await db.execute(select(User).where(User.username == username))
    return result.scalars().first()


async def get_user_by_id(db: AsyncSession, user_id: str) -> User | None:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalars().first()


async def create_user(db: AsyncSession, username: str, display_name: str, password_hash: str) -> User:
    user = User(username=username, display_name=display_name, password_hash=password_hash)
    db.add(user)
    await db.flush()
    return user


# --- conversations ---
async def list_conversations(db: AsyncSession, user_id: str) -> list[Conversation]:
    result = await db.execute(select(Conversation).where(Conversation.user_id == user_id).order_by(Conversation.updated_at.desc()))
    return list(result.scalars().all())


async def get_conversation(db: AsyncSession, conversation_id: str, user_id: str) -> Conversation | None:
    result = await db.execute(select(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == user_id))
    return result.scalars().first()


async def create_conversation(db: AsyncSession, user_id: str, title: str = "Untitled") -> Conversation:
    conv = Conversation(user_id=user_id, title=title)
    db.add(conv)
    await db.flush()
    return conv


# --- messages ---
async def list_messages(db: AsyncSession, conversation_id: str) -> list[Message]:
    result = await db.execute(select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at.asc()))
    return list(result.scalars().all())


async def create_message(
    db: AsyncSession,
    conversation_id: str,
    role: str,
    content: str,
    model: str | None = None,
    node_id: str | None = None,
    latency_ms: int | None = None,
) -> Message:
    msg = Message(conversation_id=conversation_id, role=role, content=content, model=model, node_id=node_id, latency_ms=latency_ms)
    db.add(msg)
    await db.flush()
    return msg
