import os
import json
import time
import httpx
from datetime import date
from dotenv import load_dotenv

load_dotenv(override=True)

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").lower()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:20128/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")

SSL_VERIFY = os.getenv("SSL_VERIFY", "true").lower() not in ["false", "0", "no"]

TIMEOUT = 90.0

VALID_DOC_TYPES = [
    "Rental Agreement", "Employment Contract", "Insurance Policy", "Invoice",
    "Warranty", "Subscription", "Bill", "Bank Statement", "Certificate",
    "Tax Document", "Legal Notice", "Other"
]

def _call_llm_raw(prompt: str, system_instruction: str = "", json_mode: bool = False) -> str:
    """Helper to call Gemini or OpenAI with 1 retry on 429/5xx or invalid json."""
    if LLM_PROVIDER == "openai":
        if not LLM_API_KEY and "localhost" not in LLM_BASE_URL:
            raise ValueError("LLM_API_KEY environment variable is missing.")
        url = f"{LLM_BASE_URL.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json"
        }
        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": LLM_MODEL,
            "messages": messages,
            "temperature": 0.0
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        for attempt in range(2):
            try:
                with httpx.Client(timeout=TIMEOUT, verify=SSL_VERIFY) as client:
                    resp = client.post(url, json=payload, headers=headers)
                    if resp.status_code in [429, 500, 502, 503, 504] and attempt == 0:
                        time.sleep(2)
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    return data["choices"][0]["message"]["content"]
            except Exception as e:
                if attempt == 1:
                    raise RuntimeError(f"OpenAI LLM call failed: {str(e)}")
                time.sleep(2)
        return ""
    else:
        # Default Gemini
        if not GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY environment variable is missing.")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
        headers = {"Content-Type": "application/json"}

        contents = []
        if system_instruction:
            contents.append({"role": "user", "parts": [{"text": f"System Instruction: {system_instruction}"}]})
            contents.append({"role": "model", "parts": [{"text": "Understood."}]})
        contents.append({"role": "user", "parts": [{"text": prompt}]})

        payload = {
            "contents": contents,
            "generationConfig": {
                "temperature": 0.0
            }
        }
        if json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"

        for attempt in range(2):
            try:
                with httpx.Client(timeout=TIMEOUT, verify=SSL_VERIFY) as client:
                    resp = client.post(url, json=payload, headers=headers)
                    if resp.status_code in [429, 500, 502, 503, 504] and attempt == 0:
                        time.sleep(2)
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    candidates = data.get("candidates", [])
                    if not candidates:
                        raise RuntimeError("No candidates returned from Gemini.")
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if not parts:
                        raise RuntimeError("No content parts returned from Gemini.")
                    return parts[0].get("text", "")
            except Exception as e:
                if attempt == 1:
                    raise RuntimeError(f"Gemini LLM call failed: {str(e)}")
                time.sleep(2)
        return ""

def extract(text: str) -> dict:
    today_str = date.today().isoformat()
    truncated_text = text[:30000]

    system_instruction = (
        "You are an AI document analysis assistant. Extract structured JSON from the provided document text. "
        "Use only information in the text, never invent dates. Compute relative deadlines only when the base date exists in the document, otherwise null."
    )

    prompt = f"""Today's date is {today_str}.
Analyze the following document text (which contains [Page N] markers) and output ONLY valid JSON matching this exact schema:
{{
  "doc_type": "Rental Agreement" | "Employment Contract" | "Insurance Policy" | "Invoice" | "Warranty" | "Subscription" | "Bill" | "Bank Statement" | "Certificate" | "Tax Document" | "Legal Notice" | "Other",
  "summary": "2-3 plain sentences summarizing the document",
  "key_facts": {{"label": "value"}},
  "obligations": [
    {{
      "title": "Short title of obligation or due task",
      "deadline": "YYYY-MM-DD or null",
      "priority": "high" | "medium" | "low",
      "page": 1,
      "quote": "short exact supporting sentence from text"
    }}
  ]
}}

Document Text:
{truncated_text}
"""

    raw_resp = _call_llm_raw(prompt, system_instruction=system_instruction, json_mode=True)

    # Clean markdown fences if any
    cleaned = raw_resp.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
    except Exception:
        # Retry once if json parse failed
        raw_resp = _call_llm_raw(prompt + "\n\nError: previous response was not valid JSON. Return ONLY valid JSON.", system_instruction=system_instruction, json_mode=True)
        cleaned = raw_resp.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        elif cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
        parsed = json.loads(cleaned)

    # Validation and sanitization
    doc_type = parsed.get("doc_type", "Other")
    if doc_type not in VALID_DOC_TYPES:
        doc_type = "Other"

    summary = str(parsed.get("summary", ""))[:1000]

    raw_key_facts = parsed.get("key_facts", {})
    key_facts = {}
    if isinstance(raw_key_facts, dict):
        for k, v in raw_key_facts.items():
            key_facts[str(k)[:100]] = str(v)[:500]

    raw_obligations = parsed.get("obligations", [])
    obligations = []
    if isinstance(raw_obligations, list):
        for ob in raw_obligations:
            if not isinstance(ob, dict):
                continue
            title = ob.get("title")
            if not title or not isinstance(title, str):
                continue
            title = title.strip()[:200]
            if not title:
                continue

            deadline_str = ob.get("deadline")
            deadline = None
            if deadline_str and isinstance(deadline_str, str):
                try:
                    deadline = date.fromisoformat(deadline_str.strip()).isoformat()
                except Exception:
                    deadline = None

            priority = ob.get("priority", "medium")
            if priority not in ["high", "medium", "low"]:
                priority = "medium"

            page = ob.get("page")
            try:
                page = int(page) if page is not None else None
            except Exception:
                page = None

            quote = str(ob.get("quote", ""))[:500]

            obligations.append({
                "title": title,
                "deadline": deadline,
                "priority": priority,
                "page": page,
                "quote": quote
            })

    return {
        "doc_type": doc_type,
        "summary": summary,
        "key_facts": key_facts,
        "obligations": obligations
    }

def answer(question: str, context: str) -> str:
    system_instruction = (
        "You are an AI assistant answering questions about user documents. "
        "Answer ONLY from the supplied excerpts. Cite inline as [Doc: name, p.N]. "
        "Say clearly when the answer is not in the excerpts. Add no legal advice."
    )

    prompt = f"""Excerpts from user documents:
{context}

Question: {question}

Answer concisely and accurately based ONLY on the excerpts above. Include inline citations like [Doc: document_name, p.X]. If the answer cannot be found in the excerpts, state clearly: "I couldn't find anything relevant in your documents."
"""

    return _call_llm_raw(prompt, system_instruction=system_instruction, json_mode=False)
