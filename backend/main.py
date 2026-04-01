import uuid
import random
import os
import json
import psycopg2
import psycopg2.extras
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import socketio

# Initialize FastAPI
app = FastAPI()

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize Socket.IO
sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
socket_app = socketio.ASGIApp(sio, app)

# ──────────────────────────────────────────────
# Storage: Postgres if DATABASE_URL is set, else local JSON file
# ──────────────────────────────────────────────

DATABASE_URL = os.environ.get("DATABASE_URL")
QUIZZES_FILE = os.path.join(os.path.dirname(__file__), "quizzes.json")

def get_db_conn():
    """Return a Postgres connection, fixing Railway's postgres:// prefix."""
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url)

def ensure_table():
    """Create the quizzes table if it doesn't already exist."""
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS quizzes (
            id TEXT PRIMARY KEY,
            data JSONB NOT NULL
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

if DATABASE_URL:
    print("AhoorQuiz: Using PostgreSQL for persistent quiz storage.")
    ensure_table()
else:
    print("AhoorQuiz: DATABASE_URL not set — using local quizzes.json file.")


# ──────────────────────────────────────────────
# Quiz normalization
# ──────────────────────────────────────────────

def normalize_quiz_data(quiz):
    if not isinstance(quiz, dict):
        return quiz

    questions = quiz.get("questions", [])
    if not isinstance(questions, list):
        return quiz

    normalized_questions = []
    for q in questions:
        if not isinstance(q, dict):
            continue

        options = q.get("options", [])
        if isinstance(options, dict):
            sorted_keys = sorted(options.keys())
            options_list = [options[k] for k in sorted_keys]
            answer = q.get("answer") or q.get("Answer") or q.get("correctAnswer") or q.get("CorrectAnswer")
            if isinstance(answer, str) and answer.upper() in options:
                answer = options[answer.upper()]
            elif isinstance(answer, str) and answer in options:
                answer = options[answer]
            q["options"] = options_list
            q["answer"] = answer
        else:
            if "answer" not in q or not q["answer"]:
                q["answer"] = q.get("Answer") or q.get("correctAnswer") or q.get("CorrectAnswer") or ""

        q_type = str(q.get("type", "multi")).lower()
        if q_type in ["single", "multi"]:
            q["type"] = "multi"
        elif q_type in ["boolean", "tf", "t/f"]:
            q["type"] = "tf"
        elif q_type == "multi-select":
            q["type"] = "multi-select"
        elif q_type == "open":
            q["type"] = "open"
        else:
            opts = q.get("options", [])
            if isinstance(opts, list) and len(opts) > 0:
                is_tf = len(opts) == 2 and all(str(o) in ["True", "False", "Yes", "No"] for o in opts)
                q["type"] = "tf" if is_tf else "multi"
            else:
                q["type"] = "open"

        normalized_questions.append(q)

    quiz["questions"] = normalized_questions
    return quiz


# ──────────────────────────────────────────────
# Load / Save quizzes (Postgres or file)
# ──────────────────────────────────────────────

def load_quizzes():
    if DATABASE_URL:
        try:
            conn = get_db_conn()
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT data FROM quizzes ORDER BY data->>'title'")
            rows = cur.fetchall()
            cur.close()
            conn.close()
            return [normalize_quiz_data(dict(row["data"])) for row in rows]
        except Exception as e:
            print(f"DB load error: {e}")
            return []
    else:
        if not os.path.exists(QUIZZES_FILE):
            return []
        with open(QUIZZES_FILE, 'r') as f:
            try:
                quizzes = json.load(f)
                return [normalize_quiz_data(q) for q in quizzes]
            except Exception:
                return []


def save_quiz_to_db(quiz):
    """Upsert a single quiz into Postgres."""
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO quizzes (id, data) VALUES (%s, %s)
        ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data
        """,
        (quiz["id"], json.dumps(quiz))
    )
    conn.commit()
    cur.close()
    conn.close()


def delete_quiz_from_db(quiz_id):
    """Delete a quiz from Postgres by ID."""
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM quizzes WHERE id = %s", (quiz_id,))
    conn.commit()
    cur.close()
    conn.close()


def save_quizzes_to_file(quizzes):
    normalized = [normalize_quiz_data(q) for q in quizzes]
    with open(QUIZZES_FILE, 'w') as f:
        json.dump(normalized, f, indent=4)


# ──────────────────────────────────────────────
# In-memory game state
# ──────────────────────────────────────────────

games = {}  # pin -> game_data

# ──────────────────────────────────────────────
# Frontend
# ──────────────────────────────────────────────

@app.get("/")
async def get_frontend():
    frontend_path = os.path.join(os.path.dirname(__file__), "../frontend/index.html")
    response = FileResponse(frontend_path)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


# ──────────────────────────────────────────────
# Socket.IO Event Handlers
# ──────────────────────────────────────────────

@sio.event
async def connect(sid, environ):
    print(f"Client connected: {sid}")
    await sio.emit("all_quizzes", {"quizzes": load_quizzes()}, room=sid)


@sio.event
async def disconnect(sid):
    print(f"Client disconnected: {sid}")


@sio.event
async def create_game(sid, data):
    pin = str(random.randint(100000, 999999))
    quiz_id = data.get("quiz_id")
    quizzes = load_quizzes()

    selected_quiz = next((q for q in quizzes if q["id"] == quiz_id), None)
    if not selected_quiz:
        selected_quiz = quizzes[0] if quizzes else {"title": "Empty Quiz", "questions": []}

    games[pin] = {
        "host_sid": sid,
        "players": {},
        "state": "LOBBY",
        "title": selected_quiz.get("title", "Untitled Quiz"),
        "questions": selected_quiz.get("questions", []),
        "current_question": 0,
        "bounty_sid": None,
        "completed_players": set()
    }
    await sio.emit("game_created", {"pin": pin, "title": games[pin]["title"]}, room=sid)
    await sio.enter_room(sid, pin)
    print(f"Game created with PIN: {pin} using quiz: {games[pin]['title']}")


@sio.event
async def save_quiz(sid, data):
    new_quiz = data.get("quiz")
    if not new_quiz:
        return

    if not new_quiz.get("id"):
        new_quiz["id"] = str(uuid.uuid4())

    new_quiz = normalize_quiz_data(new_quiz)

    if DATABASE_URL:
        save_quiz_to_db(new_quiz)
    else:
        quizzes = load_quizzes()
        existing = next((q for q in quizzes if q["id"] == new_quiz.get("id")), None)
        if existing:
            quizzes = [new_quiz if q["id"] == new_quiz["id"] else q for q in quizzes]
        else:
            quizzes.append(new_quiz)
        save_quizzes_to_file(quizzes)

    all_quizzes = load_quizzes()
    await sio.emit("quiz_saved", {"quiz": new_quiz}, room=sid)
    await sio.emit("all_quizzes", {"quizzes": all_quizzes})
    print(f"Quiz saved: {new_quiz['title']}")


@sio.event
async def delete_quiz(sid, data):
    quiz_id = data.get("quiz_id")
    if not quiz_id:
        return

    if DATABASE_URL:
        delete_quiz_from_db(quiz_id)
    else:
        quizzes = load_quizzes()
        quizzes = [q for q in quizzes if q["id"] != quiz_id]
        save_quizzes_to_file(quizzes)

    all_quizzes = load_quizzes()
    await sio.emit("all_quizzes", {"quizzes": all_quizzes})
    print(f"Quiz deleted: {quiz_id}")


@sio.event
async def join_game(sid, data):
    pin = str(data.get("pin"))
    nickname = data.get("nickname")
    is_rehost = data.get("is_host", False)

    if pin in games:
        if is_rehost:
            games[pin]["host_sid"] = sid
            print(f"DEBUG: Host reconnected for game {pin}")

        games[pin]["players"][sid] = {
            "nickname": nickname,
            "score": 0,
            "streak": 0,
            "bet": 0
        }
        await sio.enter_room(sid, pin)
        await sio.emit("player_joined", {"nickname": nickname, "players": list(games[pin]["players"].values())}, room=pin)
        await sio.emit("join_success", {"pin": pin}, room=sid)
        print(f"DEBUG: Player {nickname} ({sid}) joined game {pin}")
    else:
        print(f"DEBUG: Player {nickname} failed to join invalid PIN {pin}")
        await sio.emit("error", {"message": "Invalid PIN"}, room=sid)


@sio.event
async def start_game(sid, data):
    pin = str(data.get("pin"))
    if pin in games and games[pin]["host_sid"] == sid:
        games[pin]["state"] = "QUESTION"
        games[pin]["current_question"] = 0
        games[pin]["completed_players"] = set()
        question = games[pin]["questions"][0]
        print(f"DEBUG: Emitting first question: {question.get('question')}, type: {question.get('type')}")
        await sio.emit("next_question", {
            "question": question.get("question", "Untitled Question"),
            "options": question.get("options", []),
            "type": question.get("type", "multi"),
            "index": 0
        }, room=pin)


@sio.event
async def submit_answer(sid, data):
    pin = str(data.get("pin"))
    answer = data.get("answer")
    bet = data.get("bet", 0)
    print(f"DEBUG: submit_answer from {sid} for game {pin}, answer: {answer}")

    if pin in games and sid in games[pin]["players"]:
        player = games[pin]["players"][sid]
        game = games[pin]
        question = game["questions"][game["current_question"]]

        is_correct = False
        if question["type"] == "multi-select":
            is_correct = set(answer) == set(question["answer"])
        elif question["type"] == "open":
            is_correct = str(answer).strip().lower() == str(question["answer"]).strip().lower()
        else:
            is_correct = answer == question["answer"]

        points = 1000 if is_correct else 0

        bet_bonus = 0
        if is_correct:
            bet_bonus = int(player["score"] * (bet / 100))
            player["streak"] += 1
        else:
            bet_bonus = -int(player["score"] * (bet / 100))
            player["streak"] = 0

        player["score"] += points + bet_bonus
        game["completed_players"].add(sid)

        await sio.emit("answer_received", {
            "is_host_update": True,
            "count": len(game["completed_players"])
        }, room=game["host_sid"])

        await sio.emit("answer_received", {"is_correct": is_correct, "new_score": player["score"]}, room=sid)

        leaderboard = sorted(
            [{"nickname": p["nickname"], "score": p["score"]} for p in game["players"].values()],
            key=lambda x: x["score"], reverse=True
        )
        await sio.emit("update_leaderboard", {"leaderboard": leaderboard}, room=pin)


@sio.event
async def next_question(sid, data):
    pin = str(data.get("pin"))
    if pin in games and games[pin]["host_sid"] == sid:
        game = games[pin]
        game["current_question"] += 1
        game["completed_players"] = set()
        if game["current_question"] < len(game["questions"]):
            question = game["questions"][game["current_question"]]
            await sio.emit("next_question", {
                "question": question["question"],
                "options": question.get("options", []),
                "type": question["type"],
                "index": game["current_question"]
            }, room=pin)
        else:
            await sio.emit("game_over", {}, room=pin)
            # Delete the game so nobody can accidentally rejoin a finished game
            del games[pin]


# Run with uvicorn
if __name__ == "__main__":
    import uvicorn
    print("AhoorQuiz Backend STARTING...")
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(socket_app, host="0.0.0.0", port=port)
