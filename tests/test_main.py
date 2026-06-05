"""主应用测试"""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    """测试客户端"""
    return TestClient(app)


def test_root_endpoint(client):
    """测试根路径"""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert "service" in data
    assert "version" in data
    assert "status" in data


def test_service_info_endpoint(client):
    """测试服务信息端点"""
    response = client.get("/info")
    assert response.status_code == 200
    data = response.json()
    assert "service" in data
    assert "version" in data
    assert "features" in data


def test_health_check(client):
    """测试健康检查"""
    response = client.get("/monitoring/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "timestamp" in data