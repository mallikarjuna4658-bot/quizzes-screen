"""AI Quiz backend: topic and PDF -> multiple-choice questions.
Run locally:  pip install -r requirements.txt && python app.py
Deploy:       gunicorn app:app
"""
import json, os, random, re, sqlite3
import fitz  # PyMuPDF
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

BASE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE, ".env")
load_dotenv(ENV_PATH, override=True)
API_KEY = os.environ.get("OPENAI_API_KEY", "").strip().strip('"').strip("'")
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",")]
DB_PATH = os.environ.get("DB_PATH", "quiz.db")
MAX_CHARS = 14000          # PDF text sent to the AI
MAX_COUNT = 15

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
CORS(app, origins=ORIGINS)
limiter = Limiter(get_remote_address, app=app, default_limits=["200/hour"], storage_uri="memory://")


# ---------- storage: every generated question is kept so topics can be reused ----------
def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

with db() as c:
    c.execute("""CREATE TABLE IF NOT EXISTS pool(
        id INTEGER PRIMARY KEY, topic_key TEXT, question TEXT UNIQUE, data TEXT,
        created TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pool_key ON pool(topic_key)")


def topic_key(topic, level, stream, difficulty):
    norm = re.sub(r"\s+", " ", topic.strip().lower())
    return f"{level or 'any'}|{stream or 'any'}|{difficulty}|{norm}"


# ---------- AI ----------
class AIError(Exception):
    pass

def call_ai(prompt):
    if not API_KEY:
        raise AIError("Server is missing OPENAI_API_KEY. Add it to your .env file.")
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions", timeout=90,
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": MODEL, "temperature": 0.8,
                  "response_format": {"type": "json_object"},
                  "messages": [
                      {"role": "system", "content": "You write accurate multiple-choice quizzes. Reply with JSON only."},
                      {"role": "user", "content": prompt}]})
    except requests.RequestException:
        raise AIError("Could not reach the AI service. Try again.")
    if not r.ok:
        try:
            detail = r.json()["error"]["message"]
        except Exception:
            detail = r.text[:200]
        print(f"[OpenAI {r.status_code}] model={MODEL} -> {detail}", flush=True)
        if r.status_code == 401:
            raise AIError("OpenAI rejected the API key. Check OPENAI_API_KEY in your .env file.")
        if r.status_code == 429:
            raise AIError("OpenAI limit reached. Check your billing or credits, or wait a minute.")
        if r.status_code == 404:
            raise AIError(f'Model "{MODEL}" was not found. Change OPENAI_MODEL in .env.')
        raise AIError(f"AI service error ({r.status_code}): {detail[:160]}")
    try:
        return r.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError):
        raise AIError("AI sent an empty reply. Try again.")

def parse_questions(raw):
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.I).strip()
    try:
        arr = json.loads(raw)
    except ValueError:
        m = re.search(r"\[[\s\S]*\]", raw)
        if not m:
            raise AIError("AI reply was not a valid quiz. Try again.")
        arr = json.loads(m.group(0))
    if isinstance(arr, dict):
        arr = arr.get("questions", [])
    out = []
    for q in arr if isinstance(arr, list) else []:
        opts = q.get("options") if isinstance(q, dict) else None
        if not (q.get("question") and isinstance(opts, list) and len(opts) >= 2):
            continue
        opts = [str(o) for o in opts[:4]]
        ci = q.get("correctIndex")
        ci = ci if isinstance(ci, int) and 0 <= ci < len(opts) else 0
        out.append({"q": str(q["question"]), "o": opts, "c": ci,
                    "e": str(q.get("explanation") or f'The correct answer is "{opts[ci]}".')})
    if not out:
        raise AIError("AI returned no usable questions. Try again.")
    return out

FORMAT = """Rules:
- Every question has exactly 4 options and exactly one correct answer.
- Difficulty: {difficulty}.
- Output ONLY a JSON object like {{"questions":[ ...items... ]}}.

Item format:
{{"question":"...","options":["...","...","...","..."],"correctIndex":0,"explanation":"1-2 sentences on why the answer is correct"}}"""

def avoid_block(seen):
    if not seen:
        return ""
    lines = "\n".join(f"- {s}" for s in seen[-15:])
    return f"\n\nThe student already saw these. Write completely NEW questions, do not repeat or reword them:\n{lines}"

def topic_prompt(topic, level, stream, n, difficulty, seen):
    who = f"a Class {level} student" if level else "a student"
    if stream in ("science", "commerce", "arts"):
        who += f" in the {stream} stream"
    return (f'Write exactly {n} multiple-choice quiz questions for {who} studying "{topic}". '
            f'Match the level of that student and stay strictly on "{topic}".'
            + avoid_block(seen) + "\n\n" + FORMAT.format(difficulty=difficulty))

def pdf_prompt(text, n, difficulty, seen):
    return (f"Read the study material and write exactly {n} multiple-choice questions that test understanding of it. "
            f'Use only facts from the material.\n\nMATERIAL:\n"""\n{text}\n"""'
            + avoid_block(seen) + "\n\n" + FORMAT.format(difficulty=difficulty))


# ---------- helpers ----------
def clamp_count(v):
    try:
        return max(3, min(MAX_COUNT, int(v)))
    except (TypeError, ValueError):
        return 8

def clean_difficulty(v):
    return v if v in ("Easy", "Mixed", "Hard") else "Mixed"

def clean_seen(v):
    return [str(s)[:300] for s in v[:60]] if isinstance(v, list) else []

def sample_text(text):
    if len(text) <= MAX_CHARS:
        return text
    n = 6
    size, step = MAX_CHARS // n, len(text) // n   # spread across the whole document
    return "\n...\n".join(text[k * step:k * step + size] for k in range(n))


# ---------- routes ----------
@app.get("/api/health")
def health():
    return jsonify(ok=True)

@app.post("/api/quiz/topic")
@limiter.limit("30/hour")
def quiz_topic():
    d = request.get_json(silent=True) or {}
    topic = str(d.get("topic", "")).strip()[:200]
    if len(topic) < 2:
        return jsonify(error="Type a topic first."), 400
    n, diff, seen = clamp_count(d.get("count")), clean_difficulty(d.get("difficulty")), clean_seen(d.get("seen"))
    level = d.get("classLevel") if isinstance(d.get("classLevel"), int) else None
    stream = d.get("stream") if d.get("stream") in ("science", "commerce", "arts", "general") else None
    key = topic_key(topic, level, stream, diff)

    # reuse saved questions the student hasn't seen yet
    with db() as c:
        rows = c.execute("SELECT data FROM pool WHERE topic_key=?", (key,)).fetchall()
    fresh = [json.loads(r["data"]) for r in rows if json.loads(r["data"])["q"] not in seen]
    if len(fresh) >= n:
        return jsonify(questions=random.sample(fresh, n), cached=True)

    try:
        qs = parse_questions(call_ai(topic_prompt(topic, level, stream, n, diff, seen)))
    except AIError as e:
        return jsonify(error=str(e)), 502
    with db() as c:
        for q in qs:
            c.execute("INSERT OR IGNORE INTO pool(topic_key,question,data) VALUES(?,?,?)", (key, q["q"], json.dumps(q)))
    return jsonify(questions=qs, cached=False)

@app.post("/api/quiz/pdf")
@limiter.limit("10/hour")
def quiz_pdf():
    f = request.files.get("file")
    if not f:
        return jsonify(error="Choose a PDF first."), 400
    n, diff = clamp_count(request.form.get("count")), clean_difficulty(request.form.get("difficulty"))
    try:
        seen = clean_seen(json.loads(request.form.get("seen", "[]")))
    except ValueError:
        seen = []
    try:
        with fitz.open(stream=f.read(), filetype="pdf") as doc:
            text = re.sub(r"[ \t]+", " ", "\n".join(p.get_text() for p in doc)).strip()
    except Exception:
        return jsonify(error="That file could not be read as a PDF."), 400
    if len(text) < 80:
        return jsonify(error="No readable text found. This looks like a scanned PDF (images only)."), 422
    try:
        qs = parse_questions(call_ai(pdf_prompt(sample_text(text), n, diff, seen)))
    except AIError as e:
        return jsonify(error=str(e)), 502
    return jsonify(questions=qs)

@app.errorhandler(413)
def too_big(_):
    return jsonify(error="That PDF is over 25 MB. Try a smaller one."), 413

@app.errorhandler(429)
def slow_down(_):
    return jsonify(error="Too many quizzes in a short time. Please wait a bit."), 429

def startup_check():
    print("-" * 50)
    print(f".env path : {ENV_PATH}  ({'found' if os.path.exists(ENV_PATH) else 'NOT FOUND'})")
    print(f"API key   : {'loaded (' + str(len(API_KEY)) + ' characters)' if API_KEY else 'MISSING - add OPENAI_API_KEY=... to that file'}")
    print(f"Model     : {MODEL}")
    print("-" * 50, flush=True)

startup_check()

if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)), debug=True)