import uuid
import random
import os
import json
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

# Quizzes File Path
QUIZZES_FILE = os.path.join(os.path.dirname(__file__), "quizzes.json")

def load_quizzes():
    if not os.path.exists(QUIZZES_FILE):
        return []
    with open(QUIZZES_FILE, 'r') as f:
        return json.load(f)

def save_quizzes(quizzes):
    with open(QUIZZES_FILE, 'w') as f:
        json.dump(quizzes, f, indent=4)

# In-memory game state
games = {} # pin -> game_data

# Serve frontend
@app.get("/")
async def get_frontend():
    frontend_path = os.path.join(os.path.dirname(__file__), "../frontend/index.html")
    return FileResponse(frontend_path)

# Socket.IO Event Handlers 
@sio.event
async def connect(sid, environ):
    print(f"Client connected: {sid}")
    # Send all available quizzes to the connected client
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
    if not new_quiz: return
    
    quizzes = load_quizzes()
    # If ID exists, update, otherwise append
    existing = next((q for q in quizzes if q["id"] == new_quiz["id"]), None)
    if existing:
        quizzes = [new_quiz if q["id"] == new_quiz["id"] else q for q in quizzes]
    else:
        new_quiz["id"] = str(uuid.uuid4())
        quizzes.append(new_quiz)
    
    save_quizzes(quizzes)
    await sio.emit("quiz_saved", {"quiz": new_quiz}, room=sid)
    await sio.emit("all_quizzes", {"quizzes": quizzes}) # Broadcast update to everyone
    print(f"Quiz saved: {new_quiz['title']}")

    await sio.emit("all_quizzes", {"quizzes": quizzes}) # Broadcast update to everyone
    print(f"Quiz saved: {new_quiz['title']}")

@sio.event
async def delete_quiz(sid, data):
    quiz_id = data.get("quiz_id")
    if not quiz_id: return
    
    quizzes = load_quizzes()
    # Filter out the quiz with matching ID
    original_len = len(quizzes)
    quizzes = [q for q in quizzes if q["id"] != quiz_id]
    
    if len(quizzes) < original_len:
        save_quizzes(quizzes)
        await sio.emit("all_quizzes", {"quizzes": quizzes}) # Broadcast update
        print(f"Quiz deleted: {quiz_id}")

@sio.event
async def join_game(sid, data):
    pin = data.get("pin")
    nickname = data.get("nickname")
    
    if pin in games:
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
    pin = data.get("pin")
    if pin in games and games[pin]["host_sid"] == sid:
        games[pin]["state"] = "QUESTION"
        games[pin]["current_question"] = 0
        games[pin]["completed_players"] = set()
        question = games[pin]["questions"][0]
        await sio.emit("next_question", {
            "question": question["question"],
            "options": question.get("options", []),
            "type": question["type"],
            "index": 0
        }, room=pin)

@sio.event
async def submit_answer(sid, data):
    pin = data.get("pin")
    answer = data.get("answer")
    bet = data.get("bet", 0)
    print(f"DEBUG: submit_answer from {sid} for game {pin}, answer: {answer}")
    
    if pin in games and sid in games[pin]["players"]:
        player = games[pin]["players"][sid]
        game = games[pin]
        question = game["questions"][game["current_question"]]
        
        # Calculate score
        is_correct = False
        if question["type"] == "multi-select":
            # For multi-select, match sorted lists or sets
            is_correct = set(answer) == set(question["answer"])
        elif question["type"] == "open":
            # For open-ended, case-insensitive match
            is_correct = str(answer).strip().lower() == str(question["answer"]).strip().lower()
        else:
            # For single choice (multi, tf)
            is_correct = answer == question["answer"]
            
        points = 1000 if is_correct else 0
        
        # Apply betting twist
        bet_bonus = 0
        if is_correct:
            bet_bonus = int(player["score"] * (bet / 100))
            player["streak"] += 1
        else:
            bet_bonus = -int(player["score"] * (bet / 100))
            player["streak"] = 0
            
        player["score"] += points + bet_bonus
        
        # Track completion
        game["completed_players"].add(sid)
        
        # Notify host of progress
        await sio.emit("answer_received", {
            "is_host_update": True, 
            "count": len(game["completed_players"])
        }, room=game["host_sid"])

        # Notify player of their status
        await sio.emit("answer_received", {"is_correct": is_correct, "new_score": player["score"]}, room=sid)
        
        # Emit updated leaderboard
        leaderboard = sorted(
            [{"nickname": p["nickname"], "score": p["score"]} for p in game["players"].values()],
            key=lambda x: x["score"], reverse=True
        )
        await sio.emit("update_leaderboard", {"leaderboard": leaderboard}, room=pin)

@sio.event
async def next_question(sid, data):
    pin = data.get("pin")
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

# Run with uvicorn
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(socket_app, host="0.0.0.0", port=port)
