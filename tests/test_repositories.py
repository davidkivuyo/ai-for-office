import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import hash_password
from app.db.repositories import create_conversation, create_message, list_conversations, list_messages


@pytest.mark.asyncio
async def test_conversation_persistence(db_session: AsyncSession) -> None:
    from app.db.repositories import create_user

    user = await create_user(db_session, "u1", "User One", hash_password("pw"))
    await db_session.commit()

    conv = await create_conversation(db_session, user.id, title="Test Conv")
    await db_session.commit()
    assert conv.id

    convs = await list_conversations(db_session, user.id)
    assert len(convs) == 1

    await create_message(db_session, conv.id, role="user", content="hello", model=None, node_id=None)
    await db_session.commit()
    msgs = await list_messages(db_session, conv.id)
    assert len(msgs) == 1
    assert msgs[0].content == "hello"

    # Audit metadata — use configured model instead of hard-coded
    from app.config import get_settings

    expected_model = get_settings().ollama_node_1_model
    await create_message(db_session, conv.id, role="assistant", content="hi", model=expected_model, node_id="node1", latency_ms=123)
    await db_session.commit()
    msgs = await list_messages(db_session, conv.id)
    assert msgs[1].model == expected_model
    assert msgs[1].node_id == "node1"
    assert msgs[1].latency_ms == 123
