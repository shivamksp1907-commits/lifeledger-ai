# LifeLedger AI

LifeLedger AI is a full-stack document intelligence application that transforms unstructured personal PDFs (rental agreements, insurance policies, invoices, warranties, subscriptions, bills) into structured records, actionable task dashboards, and citable AI Q&A.

---

## Architecture Diagram

```
[ Frontend: HTML / Vanilla JS ]
             │
             │ HTTP / JSON (Bearer Token Auth)
             ▼
[ FastAPI Backend (app/main.py) ]
  ├── Auth (/api/auth/register, /api/auth/login)
  ├── Documents (/api/documents) ──► Background Task ──► pypdf (Text Extraction)
  ├── Tasks (/api/tasks)                                      │
  └── Ask (/api/ask) ◄── TF-IDF Retrieval ◄───────────────────┼──► LLM (Gemini / OpenAI)
             │                                                │
             ▼                                                ▼
     [ SQLite / Postgres ]                            [ Chunks & Obligations DB ]
```

---

## Windows PowerShell Setup Steps

1. Open PowerShell in the project directory.
2. Create a virtual environment:
   ```powershell
   python -m venv venv
   ```
3. Activate the virtual environment:
   ```powershell
   venv\Scripts\Activate.ps1
   ```
   *(Note: If script execution is disabled on your system, run `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process` first)*.
4. Install dependencies:
   ```powershell
   pip install -r requirements.txt
   pip install -r requirements-dev.txt
   ```
5. Configure environment variables:
   Copy `.env.example` to `.env` and fill in your API keys:
   ```powershell
   Copy-Item .env.example .env
   ```
   Or set them in PowerShell:
   ```powershell
   $env:SECRET_KEY="your-secret-key"
   $env:LLM_PROVIDER="gemini"
   $env:GEMINI_API_KEY="your-gemini-api-key"
   ```
6. Run the application:
   ```powershell
   uvicorn app.main:app --reload
   ```
7. Open your browser at `http://localhost:8000`.

---

## How to Run Tests

Run pytest without network access (tests mock LLM calls):
```powershell
pytest
```

---

## Getting a Free Gemini API Key

1. Go to [Google AI Studio](https://aistudio.google.com/apikey).
2. Sign in with a Google account.
3. Click **Create API key**.
4. Set `LLM_PROVIDER=gemini` and `GEMINI_API_KEY=your_key` in your `.env` file.

---

## Using a Local OpenAI-Compatible Gateway

If you are running a local gateway (e.g. LM Studio, Ollama proxy, or local router):
```env
LLM_PROVIDER=openai
LLM_BASE_URL=http://localhost:20128/v1
LLM_API_KEY=not-needed-for-local
LLM_MODEL=gpt-4o-mini
```

---

## Free Deployment Steps (Neon Postgres + Render Docker)

1. **Database**: Create a free project on [Neon Postgres](https://neon.tech) and copy your connection string (`postgresql://...`).
2. **Hosting**: Create a new Web Service on [Render](https://render.com), connect your GitHub repository, and select **Docker** as the environment.
3. **Environment Variables**: In Render dashboard, add:
   - `SECRET_KEY` = secure random string
   - `DATABASE_URL` = your Neon Postgres connection string
   - `LLM_PROVIDER` = `gemini`
   - `GEMINI_API_KEY` = your Gemini API key
4. *Note on Free Tiers*: Render free instances spin down after inactivity (cold start on first request), and Render free disks are ephemeral, which is why a persistent external Postgres database (like Neon) is required.

---

## How It Works / Design Decisions (Viva Guide)

- **Hallucination Control & Citations**: Q&A uses TF-IDF keyword retrieval over document chunks to inject precise excerpts into the LLM prompt. The system prompt restricts the LLM to answer *only* from the supplied text and cite inline as `[Doc: name, p.N]`.
- **Output Validation**: LLM extraction JSON passes through strict validation: markdown fence stripping, JSON parsing, key whitelisting, ISO date parsing (`date.fromisoformat` or `None`), and priority whitelisting.
- **Per-User Data Isolation**: Every database query touching documents, chunks, or obligations explicitly filters by `user_id`. Unauthorized access attempts return `404 Not Found` rather than `403 Forbidden` to prevent data leakage.
- **Asynchronous Processing**: PDF uploads return immediately after validation, while text extraction, chunking, and LLM structuring occur in a background task with robust rollback and error state persistence.

---

## Known Limitations

1. **No OCR**: Scanned PDFs without embedded text layers cannot be extracted (returns "No extractable text" error).
2. **Keyword Retrieval**: TF-IDF keyword matching is used instead of dense vector embeddings for simplicity and zero external dependencies.
3. **No Email Reminders**: Deadlines and tasks are tracked on the dashboard; automated email notifications are not implemented.
