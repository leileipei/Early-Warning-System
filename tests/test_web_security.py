from io import BytesIO

import pytest
from fastapi import Depends, FastAPI, File, Request, UploadFile
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from app.web_security import ensure_csrf_token, require_csrf, rotate_csrf_token


@pytest.fixture()
def csrf_client():
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="csrf-test-secret")
    app.state.submit_calls = 0

    @app.get("/token")
    def token(request: Request):
        return {"token": ensure_csrf_token(request)}

    @app.post("/submit", dependencies=[Depends(require_csrf)])
    def submit(request: Request):
        request.app.state.submit_calls += 1
        return {"ok": True}

    @app.post("/upload", dependencies=[Depends(require_csrf)])
    async def upload(file: UploadFile = File(...)):
        return {"filename": file.filename, "content": (await file.read()).decode()}

    @app.post("/rotate", dependencies=[Depends(require_csrf)])
    def rotate(request: Request):
        return {"token": rotate_csrf_token(request)}

    return TestClient(app)


def test_csrf_accepts_matching_session_form_token(csrf_client):
    token = csrf_client.get("/token").json()["token"]

    response = csrf_client.post("/submit", data={"_csrf_token": token})

    assert response.status_code == 200


@pytest.mark.parametrize("data", [{}, {"_csrf_token": "wrong"}])
def test_csrf_rejects_missing_or_wrong_token(csrf_client, data):
    csrf_client.get("/token")

    response = csrf_client.post("/submit", data=data)

    assert response.status_code == 403
    assert response.json() == {"detail": "请求安全校验失败"}
    assert csrf_client.app.state.submit_calls == 0


def test_csrf_rejects_token_from_another_session(csrf_client):
    other = TestClient(csrf_client.app)
    foreign_token = other.get("/token").json()["token"]
    csrf_client.get("/token")

    response = csrf_client.post("/submit", data={"_csrf_token": foreign_token})

    assert response.status_code == 403


def test_csrf_accepts_multipart_upload(csrf_client):
    token = csrf_client.get("/token").json()["token"]

    response = csrf_client.post(
        "/upload",
        data={"_csrf_token": token},
        files={"file": ("rules.json", BytesIO(b"{}"), "application/json")},
    )

    assert response.status_code == 200
    assert response.json() == {"filename": "rules.json", "content": "{}"}


def test_rotate_csrf_token_invalidates_previous_token(csrf_client):
    original = csrf_client.get("/token").json()["token"]
    rotated = csrf_client.post("/rotate", data={"_csrf_token": original}).json()["token"]

    assert rotated != original
    assert csrf_client.post("/submit", data={"_csrf_token": original}).status_code == 403
    assert csrf_client.post("/submit", data={"_csrf_token": rotated}).status_code == 200
