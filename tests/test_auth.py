import pytest

from app.auth.service import hash_password, verify_password, create_access_token, decode_token


def test_hash_verify():
    h = hash_password("secret123")
    assert verify_password("secret123", h)
    assert not verify_password("wrong", h)


def test_jwt_roundtrip():
    tok = create_access_token(subject="user-123")
    assert decode_token(tok) == "user-123"
    assert decode_token("bad.token.here") is None


@pytest.mark.asyncio
async def test_register_login_me(app_client):
    # Register
    r = await app_client.post("/api/auth/register", json={"username": "alice", "password": "wonderland123", "display_name": "Alice"})
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["user"]["username"] == "alice"
    token = data["access_token"]

    # Duplicate should 400
    r2 = await app_client.post("/api/auth/register", json={"username": "alice", "password": "other12345678", "display_name": "Alice2"})
    assert r2.status_code == 400

    # Login
    r3 = await app_client.post("/api/auth/login", json={"username": "alice", "password": "wonderland123"})
    assert r3.status_code == 200
    token2 = r3.json()["access_token"]
    assert token2

    # Wrong password
    r4 = await app_client.post("/api/auth/login", json={"username": "alice", "password": "wrongpass123"})
    assert r4.status_code == 401

    # Me with token
    app_client.headers["Authorization"] = f"Bearer {token}"
    r5 = await app_client.get("/api/auth/me")
    assert r5.status_code == 200
    assert r5.json()["username"] == "alice"

    # Me without token 401
    app_client.headers.pop("Authorization")
    r6 = await app_client.get("/api/auth/me")
    assert r6.status_code == 401


@pytest.mark.asyncio
async def test_password_not_plain(app_client):
    r = await app_client.post("/api/auth/register", json={"username": "bob", "password": "s3cret123456", "display_name": "Bob"})
    assert r.status_code == 201
    # Verify DB stores hash not plain — query test database directly
    from sqlalchemy import select

    from app.db.models import User

    engine = app_client.app.state.engine  # type: ignore[attr-defined]
    # Use the app's engine/session_factory to query the same in-memory DB
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        result = await session.execute(select(User).where(User.username == "bob"))
        user = result.scalars().first()
        assert user is not None
        stored_hash = user.password_hash
        assert stored_hash != "s3cret123456"
        assert verify_password("s3cret123456", stored_hash)

    r2 = await app_client.post("/api/auth/login", json={"username": "bob", "password": "s3cret123456"})
    assert r2.status_code == 200
