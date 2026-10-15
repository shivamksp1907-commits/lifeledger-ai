import io
import os
import datetime
from datetime import date
import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from app.database import Base, engine, SessionLocal
from app.main import app
from app import llm

client = TestClient(app)

@pytest.fixture(autouse=True)
def setup_db():
    # Recreate tables before each test
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield

def create_sample_pdf(text_content: str = "This is a rental agreement. Rent is due on 2026-12-31. Pay rent to landlord.") -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer)
    c.drawString(100, 750, text_content)
    c.save()
    return buffer.getvalue()

def register_and_login(client, email="test@example.com", password="password123"):
    resp = client.post("/api/auth/register", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]

def test_register_and_login_success_and_failure():
    # Register success
    resp = client.post("/api/auth/register", json={"email": "student@college.edu", "password": "securepassword123"})
    assert resp.status_code == 200
    assert "access_token" in resp.json()

    # Duplicate email -> 409
    resp = client.post("/api/auth/register", json={"email": "student@college.edu", "password": "otherpassword123"})
    assert resp.status_code == 409

    # Invalid email (no @) -> 400
    resp = client.post("/api/auth/register", json={"email": "invalidemail", "password": "securepassword123"})
    assert resp.status_code == 400

    # Short password (<8) -> 400
    resp = client.post("/api/auth/register", json={"email": "other@college.edu", "password": "short"})
    assert resp.status_code == 400

    # Login success
    resp = client.post("/api/auth/login", json={"email": "student@college.edu", "password": "securepassword123"})
    assert resp.status_code == 200
    assert "access_token" in resp.json()

    # Wrong password -> 401
    resp = client.post("/api/auth/login", json={"email": "student@college.edu", "password": "wrongpassword"})
    assert resp.status_code == 401

    # Unknown email -> 401 (same message/status)
    resp = client.post("/api/auth/login", json={"email": "unknown@college.edu", "password": "securepassword123"})
    assert resp.status_code == 401

def test_upload_rejects_non_pdf_and_oversized(monkeypatch):
    token = register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}

    # Non-PDF file
    txt_file = io.BytesIO(b"Hello this is text, not PDF")
    resp = client.post("/api/documents", headers=headers, files={"file": ("test.txt", txt_file, "text/plain")})
    assert resp.status_code == 400
    assert "PDF" in resp.json()["detail"]

    # Oversized file (>10MB)
    large_bytes = b"%PDF-1.4" + b"A" * (11 * 1024 * 1024)
    large_file = io.BytesIO(large_bytes)
    resp = client.post("/api/documents", headers=headers, files={"file": ("large.pdf", large_file, "application/pdf")})
    assert resp.status_code == 400
    assert "exceeds" in resp.json()["detail"]

def test_pdf_processing_end_to_end(monkeypatch):
    token = register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}

    # Mock llm.extract
    monkeypatch.setattr(llm, "extract", lambda text: {
        "doc_type": "Rental Agreement",
        "summary": "Sample rental agreement summary.",
        "key_facts": {"Rent": "$1200", "Landlord": "John"},
        "obligations": [
            {
                "title": "Pay Monthly Rent",
                "deadline": "2026-12-31",
                "priority": "high",
                "page": 1,
                "quote": "Rent is due on 2026-12-31."
            }
        ]
    })

    pdf_bytes = create_sample_pdf()
    resp = client.post("/api/documents", headers=headers, files={"file": ("lease.pdf", io.BytesIO(pdf_bytes), "application/pdf")})
    assert resp.status_code == 200
    doc_data = resp.json()
    doc_id = doc_data["id"]

    # Check tasks list
    resp_tasks = client.get("/api/tasks", headers=headers)
    assert resp_tasks.status_code == 200
    tasks = resp_tasks.json()
    assert len(tasks) == 1
    assert tasks[0]["title"] == "Pay Monthly Rent"
    assert tasks[0]["priority"] == "high"
    assert tasks[0]["document_name"] == "lease.pdf"

    # Get document detail
    resp_doc = client.get(f"/api/documents/{doc_id}", headers=headers)
    assert resp_doc.status_code == 200
    detail = resp_doc.json()
    assert detail["doc_type"] == "Rental Agreement"
    assert len(detail["obligations"]) == 1

def test_task_ordering(monkeypatch):
    token = register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}

    monkeypatch.setattr(llm, "extract", lambda text: {
        "doc_type": "Bill",
        "summary": "Bills summary",
        "key_facts": {},
        "obligations": [
            {"title": "Later Task", "deadline": "2026-12-31", "priority": "low", "page": 1, "quote": "a"},
            {"title": "Soon Task", "deadline": "2026-10-20", "priority": "high", "page": 1, "quote": "b"},
            {"title": "No Date Task", "deadline": None, "priority": "medium", "page": 1, "quote": "c"}
        ]
    })

    pdf_bytes = create_sample_pdf()
    client.post("/api/documents", headers=headers, files={"file": ("bills.pdf", io.BytesIO(pdf_bytes), "application/pdf")})

    resp = client.get("/api/tasks", headers=headers)
    tasks = resp.json()
    assert len(tasks) == 3
    # Order: open before done, soonest deadline first, no-date last
    assert tasks[0]["title"] == "Soon Task"
    assert tasks[1]["title"] == "Later Task"
    assert tasks[2]["title"] == "No Date Task"

    # Mark soon task done
    soon_id = tasks[0]["id"]
    client.patch(f"/api/tasks/{soon_id}", headers=headers, json={"status": "done"})

    resp2 = client.get("/api/tasks", headers=headers)
    tasks2 = resp2.json()
    # Done task should move to the bottom
    assert tasks2[-1]["title"] == "Soon Task"
    assert tasks2[-1]["status"] == "done"

def test_user_data_isolation(monkeypatch):
    token_a = register_and_login(client, email="usera@example.com", password="password123")
    token_b = register_and_login(client, email="userb@example.com", password="password123")

    monkeypatch.setattr(llm, "extract", lambda text: {
        "doc_type": "Invoice",
        "summary": "User A invoice",
        "key_facts": {},
        "obligations": [{"title": "Invoice Task", "deadline": "2026-11-01", "priority": "medium", "page": 1, "quote": "pay"}]
    })

    pdf_bytes = create_sample_pdf()
    resp = client.post("/api/documents", headers={"Authorization": f"Bearer {token_a}"}, files={"file": ("doc_a.pdf", io.BytesIO(pdf_bytes), "application/pdf")})
    doc_id = resp.json()["id"]

    # User B tries to read User A's document -> 404
    resp_b_get = client.get(f"/api/documents/{doc_id}", headers={"Authorization": f"Bearer {token_b}"})
    assert resp_b_get.status_code == 404

    # User B tries to delete User A's document -> 404
    resp_b_del = client.delete(f"/api/documents/{doc_id}", headers={"Authorization": f"Bearer {token_b}"})
    assert resp_b_del.status_code == 404

    # Get User A's task id
    tasks_a = client.get("/api/tasks", headers={"Authorization": f"Bearer {token_a}"}).json()
    task_id = tasks_a[0]["id"]

    # User B tries to patch User A's task -> 404
    resp_b_patch = client.patch(f"/api/tasks/{task_id}", headers={"Authorization": f"Bearer {token_b}"}, json={"status": "done"})
    assert resp_b_patch.status_code == 404

def test_ask_returns_citation_and_fallback(monkeypatch):
    token = register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}

    monkeypatch.setattr(llm, "extract", lambda text: {
        "doc_type": "Insurance Policy",
        "summary": "Insurance policy summary",
        "key_facts": {"Provider": "Acme Insurance"},
        "obligations": []
    })

    monkeypatch.setattr(llm, "answer", lambda question, context: "Your policy is with Acme Insurance [Doc: insurance.pdf, p.1].")

    pdf_bytes = create_sample_pdf("Acme Insurance policy details and coverage.")
    client.post("/api/documents", headers=headers, files={"file": ("insurance.pdf", io.BytesIO(pdf_bytes), "application/pdf")})

    # Ask matching question
    resp = client.post("/api/ask", headers=headers, json={"question": "Who is my insurance provider?"})
    assert resp.status_code == 200
    data = resp.json()
    assert "Acme Insurance" in data["answer"]
    assert len(data["sources"]) == 1
    assert data["sources"][0]["document"] == "insurance.pdf"
    assert data["sources"][0]["page"] == 1

    # Ask irrelevant question where no chunks score > 0
    resp_empty = client.post("/api/ask", headers=headers, json={"question": "Quantum mechanics relativity rocket launch?"})
    assert resp_empty.status_code == 200
    data_empty = resp_empty.json()
    assert data_empty["answer"] == "I couldn't find anything relevant in your documents."
    assert data_empty["sources"] == []

def test_llm_extract_validation():
    # Test LLM output validation and sanitization
    # We can mock _call_llm_raw or test llm.extract with mocked httpx
    pass
