import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from groq import Groq
from pydantic import BaseModel, Field
from pypdf import PdfReader

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("hiremeai")

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
MODEL_NAME = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

app = FastAPI(
    title="HireMeAI",
    description="Interactive AI Candidate Digital Twin & Semantic Resume Querying Engine",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Schemas ---

class Experience(BaseModel):
    company: Optional[str] = None
    role: Optional[str] = None
    duration: Optional[str] = None
    description: Optional[str] = None
    skills_used: List[str] = Field(default_factory=list)


class Resume(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    total_experience_years: Optional[float] = None
    skills: List[str] = Field(default_factory=list)
    experiences: List[Experience] = Field(default_factory=list)
    education: List[str] = Field(default_factory=list)
    projects: List[str] = Field(default_factory=list)
    certifications: List[str] = Field(default_factory=list)


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    question: str
    session_id: Optional[str] = None


class ChatResponse(BaseModel):
    answer: str
    session_id: str


# --- In-Memory State & Caching ---
# Caches parsed candidate profile and multi-turn conversation sessions
candidate_profile: Optional[Resume] = None
chat_sessions: Dict[str, List[Dict[str, str]]] = {}


def extract_pdf_text_from_bytes(file_bytes: bytes) -> str:
    """Extract raw text from PDF file bytes."""
    import io

    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        return text.strip()
    except Exception as e:
        logger.error(f"Failed to extract PDF text: {e}")
        raise HTTPException(status_code=400, detail="Invalid PDF file or unable to extract text.")


def parse_resume_to_schema(resume_text: str) -> Resume:
    """Extract structured candidate information from resume text using Groq LLM."""
    if not client:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY is not configured.")

    schema = Resume.model_json_schema()
    system_prompt = f"""You are an expert resume parsing engine.
Extract candidate information based on contextual meaning rather than rigid headings.
Return ONLY valid JSON strictly matching this schema:
{schema}

Guidelines:
1. Do not fabricate or hallucinate information.
2. If an attribute is missing, set it to null.
3. If an array attribute has no entries, return an empty array [].
4. Consolidate internships under experiences.
5. Capture technical and soft skills highlighted across the document.
"""

    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Parse the following resume content:\n\n{resume_text}"},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        raw_output = response.choices[0].message.content
        data = json.loads(raw_output)
        return Resume(**data)
    except Exception as e:
        logger.error(f"Error parsing resume via LLM: {e}")
        raise HTTPException(status_code=500, detail=f"Resume parsing failed: {str(e)}")


def query_candidate_agent(question: str, resume: Resume, session_history: List[Dict[str, str]]) -> str:
    """Generate interview response grounded solely on candidate profile."""
    if not client:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY is not configured.")

    system_prompt = f"""You are an AI representative acting on behalf of the candidate during a job interview.
You must ground your answers strictly on the verified candidate data below.

Candidate Profile:
{resume.model_dump_json(indent=2)}

Operating Rules:
1. Grounding: Answer strictly using facts present in the candidate profile.
2. Anti-Hallucination: If asked about experiences, skills, or achievements not in the profile, explicitly state:
   "I don't have enough verified information to answer that."
3. Tone: Professional, articulate, and confident as an interviewee speaking with an HR recruiter or hiring manager.
4. Voice: Speak in first person ("I have experience with...", "My background includes...").
"""

    messages = [{"role": "system", "content": system_prompt}]
    # Append recent context (last 6 messages) for multi-turn conversational awareness
    messages.extend(session_history[-6:])
    messages.append({"role": "user", "content": question})

    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            temperature=0.3,
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Error during agent interview inference: {e}")
        raise HTTPException(status_code=500, detail=f"Candidate agent query failed: {str(e)}")


# --- API Endpoints ---

@app.get("/")
def health_check():
    return {
        "status": "healthy",
        "service": "HireMeAI API",
        "version": "1.0.0",
        "candidate_loaded": candidate_profile is not None,
        "candidate_name": candidate_profile.name if candidate_profile else None,
    }


@app.post("/upload")
async def upload_resume(file: UploadFile = File(...)):
    """Upload and parse a new resume PDF, updating the active candidate profile."""
    global candidate_profile

    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF resumes are supported.")

    file_bytes = await file.read()
    raw_text = extract_pdf_text_from_bytes(file_bytes)

    if not raw_text:
        raise HTTPException(status_code=400, detail="The uploaded PDF contained no readable text.")

    candidate_profile = parse_resume_to_schema(raw_text)
    chat_sessions.clear()

    return {
        "message": "Resume successfully uploaded and parsed.",
        "candidate": candidate_profile,
    }


@app.get("/profile", response_model=Resume)
def get_candidate_profile():
    """Retrieve structured parsed profile of the active candidate."""
    if not candidate_profile:
        raise HTTPException(
            status_code=404,
            detail="No candidate profile loaded. Please upload a resume PDF using POST /upload first."
        )
    return candidate_profile


@app.post("/chat", response_model=ChatResponse)
def chat_with_candidate(request: ChatRequest):
    """Ask interview questions to the candidate's AI digital twin."""
    global candidate_profile

    if candidate_profile is None:
        raise HTTPException(
            status_code=400,
            detail="No resume profile loaded yet. Please upload a resume PDF using POST /upload first."
        )

    session_id = request.session_id or str(uuid4())
    history = chat_sessions.setdefault(session_id, [])

    answer = query_candidate_agent(request.question, candidate_profile, history)

    # Update conversation history
    history.append({"role": "user", "content": request.question})
    history.append({"role": "assistant", "content": answer})

    return ChatResponse(answer=answer, session_id=session_id)


