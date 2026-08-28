
from app.config import Settings


def test_settings_defaults():
    s = Settings()
    assert s.ai_timeout_seconds == 120
    assert s.ai_max_output_tokens == 1024


def test_ollama_nodes_two_defaults():
    s = Settings()
    nodes = s.ollama_nodes()
    ids = {n.id for n in nodes}
    assert "node1" in ids and "node2" in ids


def test_config_respects_env(monkeypatch):
    monkeypatch.setenv("OLLAMA_NODE_1_MODEL", "custom:7b")
    monkeypatch.setenv("OLLAMA_NODE_3_URL", "http://10.0.0.23:11434")
    monkeypatch.setenv("OLLAMA_NODE_3_MODEL", "qwen3.5:9b")
    monkeypatch.setenv("OLLAMA_NODE_3_ENABLED", "true")
    from app.config import get_settings

    s = get_settings()
    nodes = {n.id: n for n in s.ollama_nodes()}
    assert nodes["node1"].model == "custom:7b"
    assert "node3" in nodes
    assert nodes["node3"].model == "qwen3.5:9b"


def test_future_scaling_three_nodes(monkeypatch):
    monkeypatch.setenv("OLLAMA_NODE_3_URL", "http://10.0.0.23:11434")
    monkeypatch.setenv("OLLAMA_NODE_3_MODEL", "qwen3.5:9b")
    from app.config import get_settings as _get_settings

    s = _get_settings()
    assert any(n.id == "node3" for n in s.ollama_nodes())


def test_ollama_nodes_from_dotenv_only(tmp_path, monkeypatch):
    # Regression: generic node declared only in .env (DotEnvSettingsSource) must be discovered,
    # not only via os.environ. Ensures ollama_nodes() reads from Settings values.
    monkeypatch.delenv("OLLAMA_NODE_3_URL", raising=False)
    monkeypatch.delenv("OLLAMA_NODE_3_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_NODE_3_ENABLED", raising=False)
    env_file = tmp_path / ".env.dotenv"
    env_file.write_text(
        "SECRET_KEY=test-secret-key-for-unit-tests-32chars!!\n"
        "APP_ENV=test\n"
        "OLLAMA_NODE_3_URL=http://10.0.0.24:11434\n"
        "OLLAMA_NODE_3_MODEL=qwen3.5:9b\n"
        "OLLAMA_NODE_3_ENABLED=true\n"
    )
    s = Settings(_env_file=str(env_file))
    nodes = {n.id: n for n in s.ollama_nodes()}
    assert "node3" in nodes
    assert nodes["node3"].url == "http://10.0.0.24:11434"
    assert nodes["node3"].model == "qwen3.5:9b"
    # Env override still works: os.environ should take precedence over dotenv
    monkeypatch.setenv("OLLAMA_NODE_3_URL", "http://10.0.0.25:11434")
    s2 = Settings(_env_file=str(env_file))
    nodes2 = {n.id: n for n in s2.ollama_nodes()}
    assert nodes2["node3"].url == "http://10.0.0.25:11434"
