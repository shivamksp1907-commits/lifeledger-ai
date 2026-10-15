import os
import io
import json
import datetime
from datetime import date
from typing import Optional, List
from fastapi import FastAPI, Depends, HTTPException, status, UploadFile, File, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr, Field
from pypdf import PdfReader
from dotenv import load_dotenv

from app.database import engine, Base, get_db
from app.models import User, Document, Chunk, Obligation
from app.auth import hash_password, verify_password, create_access_token, get_current_user
from app import llm

load_dotenv(override=True)

# Create database tables on startup
Base.metadata.create_all(bind=engine)

app = FastAPI(title="LifeLedger AI", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Pydantic Schemas ---
class RegisterRequest(BaseModel):
    email: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str

class TaskUpdate(BaseModel):
    status: str = Field(..., pattern="^(open|done)$")

class AskRequest(BaseModel):
    question: str

# --- Helper for TF-IDF Scoring ---
import math
from collections import Counter
import re

STOPWORDS = {"the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "with", "by", "as", "is", "are", "was", "were", "of", "it", "its", "that", "this", "from", "which", "who", "whom", "whose", "what", "where", "when", "why", "how", "all", "each", "every", "both", "some", "any", "no", "not", "so", "if", "then", "else", "than", "too", "very", "just", "about", "into", "over", "after", "before", "between", "under", "again", "further", "once", "here", "there", "when", "where", "why", "how", "all", "any", "both", "each", "few", "more", "most", "other", "some", "such", "than", "too", "very", "s", "t", "can", "will", "just", "don", "should", "now"}

def tokenize(text: str) -> List[str]:
    tokens = re.findall(r'\b[a-z0-9]+\b', text.lower())
    return [t for t in tokens if t not in STOPWORDS and len(t) > 1]

def score_chunks(question: str, chunks_with_docs):
    q_tokens = tokenize(question)
    if not q_tokens:
        return []
    total_chunks = len(chunks_with_docs)
    if total_chunks == 0:
        return []

    df = Counter()
    chunk_tokens_list = []
    for chunk, doc_name in chunks_with_docs:
        c_tokens = tokenize(chunk.content)
        unique_c_tokens = set(c_tokens)
        chunk_tokens_list.append((chunk, doc_name, c_tokens))
        for t in unique_c_tokens:
            df[t] += 1

    scored = []
    for chunk, doc_name, c_tokens in chunk_tokens_list:
        if not c_tokens:
            scored.append((0.0, chunk, doc_name))
            continue
        c_counts = Counter(c_tokens)
        total_c = len(c_tokens)
        score = 0.0
        for qt in set(q_tokens):
            if qt in c_counts:
                tf = c_counts[qt] / total_c
                doc_freq = df.get(qt, 0)
                idf = math.log(1.0 + (total_chunks / (1.0 + doc_freq)))
                score += tf * idf
        scored.append((score, chunk, doc_name))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [(s, c, d) for s, c, d in scored if s > 0.0][:5]

# --- Background Processing Pipeline ---
def process_document_background(document_id: int):
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        doc = db.query(Document).filter(Document.id == document_id).first()
        if not doc:
            return

        # Fetch chunks content or file bytes? We stored chunks or need to read from stored file?
        # Wait, the prompt says: POST /api/documents creates row immediately. Where do we store PDF bytes?
        # Let's save PDF bytes temporarily or read during upload before background task starts.
        # Even better: pass file bytes or store in a temp/uploads dir, or read chunks directly during upload and store in DB!
        # Wait! Step 6 says:
        # 1. Extract text per page. If no page has text, mark failed with the message "No extractable text (scanned PDF? OCR is not supported yet)".
        # 2. Split each page into ~1200-character chunks and store them with page numbers.
        # 3. Call llm.extract on the [Page N]-tagged text.
        # 4. Save doc_type, summary, key_facts and obligations; set status=done.
        # 5. On ANY exception: roll back, set status=failed, and store a short error message in summary.
        pass
    except Exception as e:
        pass
    finally:
        db.close()

# Let's refine the upload route to perform step 1 & 2 during upload or pass PDF bytes to background task.
def run_pipeline(document_id: int, file_bytes: bytes):
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        doc = db.query(Document).filter(Document.id == document_id).first()
        if not doc:
            return

        try:
            reader = PdfReader(io.BytesIO(file_bytes))
            page_texts = []
            has_text = False
            for idx, page in enumerate(reader.pages, start=1):
                txt = page.extract_text() or ""
                page_texts.append((idx, txt))
                if txt.strip():
                    has_text = True

            if not has_text:
                doc.status = "failed"
                doc.summary = "No extractable text (scanned PDF? OCR is not supported yet)"
                db.commit()
                return

            # Store chunks (~1200 chars per chunk)
            formatted_llm_text = ""
            for page_num, txt in page_texts:
                formatted_llm_text += f"\n[Page {page_num}]\n{txt}\n"
                if not txt.strip():
                    continue
                chunk_size = 1200
                for i in range(0, len(txt), chunk_size):
                    chunk_content = txt[i:i+chunk_size]
                    db.add(Chunk(document_id=doc.id, page=page_num, content=chunk_content))
            db.commit()

            # Call LLM extract
            extracted = llm.extract(formatted_llm_text)

            doc.doc_type = extracted.get("doc_type", "Other")
            doc.summary = extracted.get("summary", "")
            doc.key_facts = json.dumps(extracted.get("key_facts", {}))

            # Save obligations
            for ob in extracted.get("obligations", []):
                deadline_val = None
                d_str = ob.get("deadline")
                if d_str:
                    try:
                        deadline_val = date.fromisoformat(d_str)
                    except Exception:
                        deadline_val = None

                obligation = Obligation(
                    document_id=doc.id,
                    user_id=doc.user_id,
                    title=ob.get("title", "Untitled Task"),
                    deadline=deadline_val,
                    priority=ob.get("priority", "medium"),
                    source_page=ob.get("page"),
                    quote=ob.get("quote", ""),
                    status="open"
                )
                db.add(obligation)

            doc.status = "done"
            db.commit()

        except Exception as ex:
            db.rollback()
            doc = db.query(Document).filter(Document.id == document_id).first()
            if doc:
                doc.status = "failed"
                doc.summary = f"Processing failed: {str(ex)[:200]}"
                db.commit()
    finally:
        db.close()

# --- API Routes ---
@app.get("/health")
def health_check():
    return {"ok": True}

@app.post("/api/auth/register")
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Email must contain @")
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")

    user = User(email=email, password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token({"sub": user.id})
    return {"access_token": token, "token_type": "bearer"}

@app.post("/api/auth/login")
def login(body: LoginRequest, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    user = db.query(User).filter(User.email == email).first()

    # Same message for unknown email and wrong password to prevent user enumeration
    cred_error = HTTPException(status_code=401, detail="Invalid email or password")

    if not user:
        raise cred_error
    if not verify_password(body.password, user.password_hash):
        raise cred_error

    token = create_access_token({"sub": user.id})
    return {"access_token": token, "token_type": "bearer"}

@app.post("/api/documents")
def upload_document(
    file: UploadFile = File(...),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    # Check filename / extension or content
    filename = file.filename or "document.pdf"
    file_bytes = file.file.read()

    # Check max 10 MB
    if len(file_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File size exceeds 10 MB limit")

    # Check %PDF magic bytes
    if not file_bytes.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="Invalid file format. Only PDF files are supported.")

    doc = Document(
        user_id=current_user.id,
        name=filename,
        status="processing"
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    background_tasks.add_task(run_pipeline, doc.id, file_bytes)

    return {
        "id": doc.id,
        "name": doc.name,
        "status": doc.status,
        "created_at": doc.created_at.isoformat()
    }

@app.get("/api/documents")
def list_documents(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    docs = db.query(Document).filter(Document.user_id == current_user.id).order_by(Document.created_at.desc()).all()
    result = []
    for d in docs:
        try:
            kf = json.loads(d.key_facts) if d.key_facts else {}
        except Exception:
            kf = {}
        result.append({
            "id": d.id,
            "name": d.name,
            "doc_type": d.doc_type,
            "summary": d.summary,
            "key_facts": kf,
            "status": d.status,
            "created_at": d.created_at.isoformat()
        })
    return result

@app.get("/api/documents/{doc_id}")
def get_document(doc_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    doc = db.query(Document).filter(Document.id == doc_id, Document.user_id == current_user.id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    try:
        kf = json.loads(doc.key_facts) if doc.key_facts else {}
    except Exception:
        kf = {}

    obligations = []
    for ob in doc.obligations:
        obligations.append({
            "id": ob.id,
            "title": ob.title,
            "deadline": ob.deadline.isoformat() if ob.deadline else None,
            "priority": ob.priority,
            "source_page": ob.source_page,
            "quote": ob.quote,
            "status": ob.status
        })

    return {
        "id": doc.id,
        "name": doc.name,
        "doc_type": doc.doc_type,
        "summary": doc.summary,
        "key_facts": kf,
        "status": doc.status,
        "created_at": doc.created_at.isoformat(),
        "obligations": obligations
    }

@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    doc = db.query(Document).filter(Document.id == doc_id, Document.user_id == current_user.id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    db.delete(doc)
    db.commit()
    return {"ok": True}

@app.get("/api/tasks")
def list_tasks(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tasks = db.query(Obligation).filter(Obligation.user_id == current_user.id).all()
    today = date.today()

    task_list = []
    for t in tasks:
        days_left = None
        if t.deadline:
            delta = (t.deadline - today).days
            days_left = delta

        # Document name
        doc = db.query(Document).filter(Document.id == t.document_id).first()
        doc_name = doc.name if doc else "Unknown"

        task_list.append({
            "id": t.id,
            "title": t.title,
            "document_name": doc_name,
            "deadline": t.deadline.isoformat() if t.deadline else None,
            "days_left": days_left,
            "priority": t.priority,
            "source_page": t.source_page,
            "quote": t.quote,
            "status": t.status
        })

    # Sorting rule: open before done, then soonest deadline first, no-date last
    def sort_key(item):
        status_rank = 0 if item["status"] == "open" else 1
        deadline = item["deadline"]
        if deadline is None:
            deadline_rank = 1
            date_val = datetime.date.max
        else:
            deadline_rank = 0
            date_val = datetime.date.fromisoformat(deadline)
        return (status_rank, deadline_rank, date_val)

    task_list.sort(key=sort_key)
    return task_list

@app.patch("/api/tasks/{task_id}")
def update_task(task_id: int, body: TaskUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    task = db.query(Obligation).filter(Obligation.id == task_id, Obligation.user_id == current_user.id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    task.status = body.status
    db.commit()
    return {"ok": True, "id": task.id, "status": task.status}

@app.post("/api/ask")
def ask_documents(body: AskRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty")

    # Fetch all chunks for user's documents
    chunks_query = (
        db.query(Chunk, Document.name)
        .join(Document, Chunk.document_id == Document.id)
        .filter(Document.user_id == current_user.id)
        .all()
    )

    scored = score_chunks(question, chunks_query)
    if not scored:
        return {
            "answer": "I couldn't find anything relevant in your documents.",
            "sources": []
        }

    # Build context for LLM
    context_parts = []
    sources_raw = []
    seen_sources = set()

    for score, chunk, doc_name in scored:
        context_parts.append(f"[Doc: {doc_name}, p.{chunk.page}]\n{chunk.content}\n")
        source_key = (doc_name, chunk.page)
        if source_key not in seen_sources:
            seen_sources.add(source_key)
            sources_raw.append({"document": doc_name, "page": chunk.page})

    context = "\n".join(context_parts)

    try:
        answer_text = llm.answer(question, context)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM error: {str(e)}")

    return {
        "answer": answer_text,
        "sources": sources_raw
    }

# Serve static frontend
if os.path.exists("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")
else:
    @app.get("/")
    def root():
        return {"message": "LifeLedger AI API is running. static/ folder not found."}
