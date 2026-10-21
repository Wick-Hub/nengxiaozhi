"""骨架冒烟测试：健康检查、元数据、问答链路协议。

运行：pytest -q（需已安装 dev 依赖 httpx）
说明：端到端链路依赖 tests/conftest.py 预设的 LLM_MOCK=1，
     更细的断言见 test_api_endpoints.py / test_pipeline.py。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health() -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "store" in body and "llm" in body


def test_meta() -> None:
    resp = client.get("/api/meta")
    assert resp.status_code == 200
    data = resp.json()["data"]
    # 知识可见角色字典：「全部」必须首位（管理页默认勾选第一项）
    assert data["roles"][0] == "全部"
    assert "访客" in data["roles"]
    assert "离退休人员" in data["roles"]
    # 认证角色单列，不混入知识可见范围
    assert "管理员" in data["authRoles"]


def test_chat_end_to_end() -> None:
    resp = client.post("/api/chat", json={"question": "一卡通丢了怎么办", "role": "本科生"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert isinstance(data["answer"], str) and data["answer"]
    assert data["sessionId"].startswith("S")
    assert data["refused"] is False


def test_chat_stream_protocol() -> None:
    with client.stream(
        "POST", "/api/chat/stream", json={"question": "校园网怎么开通"}
    ) as resp:
        assert resp.status_code == 200
        text = "".join(resp.iter_text())
    assert '"type": "delta"' in text
    assert '"type": "done"' in text
