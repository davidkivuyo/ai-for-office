from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import get_current_user
from app.db.models import User
from app.db.repositories import create_conversation, get_conversation, list_conversations, list_messages, update_conversation_title
from app.db.session import get_db
from app.schemas.conversation import ConversationCreate, ConversationOut, ConversationUpdate, ConversationWithMessages

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


@router.get("", response_model=list[ConversationOut])
async def list_convs(current: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[ConversationOut]:
    convs = await list_conversations(db, current.id)
    return [ConversationOut.model_validate(c) for c in convs]


@router.post("", response_model=ConversationOut, status_code=201)
async def create_conv(payload: ConversationCreate, current: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> ConversationOut:
    conv = await create_conversation(db, current.id, title=payload.title)
    await db.commit()
    await db.refresh(conv)
    return ConversationOut.model_validate(conv)


@router.get("/{conversation_id}", response_model=ConversationWithMessages)
async def get_conv(conversation_id: str, current: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> ConversationWithMessages:
    conv = await get_conversation(db, conversation_id, current.id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    msgs = await list_messages(db, conv.id)
    from app.schemas.conversation import MessageOut

    return ConversationWithMessages(
        id=conv.id,
        user_id=conv.user_id,
        title=conv.title,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        messages=[MessageOut.model_validate(m) for m in msgs],
    )


@router.patch("/{conversation_id}", response_model=ConversationOut)
async def rename_conv(conversation_id: str, payload: ConversationUpdate, current: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> ConversationOut:
    conv = await get_conversation(db, conversation_id, current.id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title must not be empty")
    if len(title) > 256:
        raise HTTPException(status_code=400, detail="Title too long")
    await update_conversation_title(db, conv, title)
    await db.commit()
    await db.refresh(conv)
    return ConversationOut.model_validate(conv)


@router.delete("/{conversation_id}", status_code=204)
async def delete_conv(conversation_id: str, current: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> None:
    conv = await get_conversation(db, conversation_id, current.id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    await db.delete(conv)
    await db.commit()
    return None
