# AI Quiz backend

1. `python -m venv venv && source venv/bin/activate` (Windows: `venv\Scripts\activate`)
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and put your Gemini key in it
4. `python app.py` runs the API on http://localhost:5000
5. Open `ai-quiz.html` (set `API_BASE` at the top of its script if the backend is elsewhere)

Endpoints
- `GET  /api/health`
- `POST /api/quiz/topic`  JSON `{topic, classLevel, stream, count, difficulty, seen[]}`
- `POST /api/quiz/pdf`    multipart `file, count, difficulty, seen`

Deploy (Render): build `pip install -r requirements.txt`, start `gunicorn app:app`,
env vars from `.env.example`. Set `ALLOWED_ORIGINS` to your site URL.
Note: SQLite on Render's free tier is wiped on redeploy. Questions still work, the saved pool just restarts.
