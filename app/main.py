import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

from fastapi import Body, Cookie, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, engines
from . import avatar_prompts
from .persona import QUESTIONS, REQUIRED, build_system_prompt

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("twin")
app = FastAPI(title="Digital Twin")

# ---------------- database ----------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL,
  salt TEXT NOT NULL, created REAL);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created REAL);
CREATE TABLE IF NOT EXISTS twins(id INTEGER PRIMARY KEY, user_id INTEGER UNIQUE NOT NULL, slug TEXT UNIQUE NOT NULL,
  display_name TEXT, headshot TEXT, voice_full TEXT, voice_ref TEXT, voice_seconds REAL DEFAULT 0,
  consent_code TEXT, consent_verified INTEGER DEFAULT 0, consent_transcript TEXT, consent_at REAL,
  speech_transcript TEXT, answers TEXT DEFAULT '{}', writing_samples TEXT DEFAULT '', public INTEGER DEFAULT 0,
  created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS memories(id INTEGER PRIMARY KEY, twin_id INTEGER NOT NULL, kind TEXT NOT NULL,
  prompt TEXT, content TEXT NOT NULL, created REAL);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, twin_id INTEGER NOT NULL, conversation TEXT NOT NULL,
  role TEXT NOT NULL, content TEXT NOT NULL, audio TEXT, video TEXT, video_status TEXT, video_error TEXT,
  is_owner INTEGER DEFAULT 0, created REAL);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(twin_id, conversation, id);
CREATE TABLE IF NOT EXISTS dreams(id INTEGER PRIMARY KEY, twin_id INTEGER NOT NULL, prompt TEXT, style TEXT,
  status TEXT, call_id TEXT, video TEXT, title TEXT, narration TEXT, error TEXT, created REAL, finished REAL);
"""
_db_lock = threading.Lock()


@contextmanager
def db():
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        with _db_lock:
            yield con
            con.commit()
    finally:
        con.close()


MIGRATIONS = [("users", "is_guest", "INTEGER DEFAULT 0"), ("twins", "photo", "TEXT"),
              ("twins", "avatar_options", "TEXT"), ("twins", "avatar_status", "TEXT"),
              ("twins", "avatar_count", "INTEGER DEFAULT 0"), ("twins", "voice_job", "TEXT")]
with db() as c:
    c.executescript(SCHEMA)
    for _tbl, _col, _typ in MIGRATIONS:
        if _col not in [r[1] for r in c.execute(f"PRAGMA table_info({_tbl})")]:
            c.execute(f"ALTER TABLE {_tbl} ADD COLUMN {_col} {_typ}")

with db() as c:  # jobs that were running when the server last stopped can't finish: free them up
    c.execute("UPDATE twins SET avatar_status='failed: interrupted, please try again', "
              "avatar_count=MAX(0, COALESCE(avatar_count,0)-1) WHERE avatar_status='generating'")
    c.execute("""UPDATE twins SET voice_job='{"status": "failed", "error": "Interrupted, please record again."}'
                 WHERE voice_job LIKE '%processing%'""")

GUEST_TTL_DAYS = 7
GUEST_DAILY_REPLIES = 30
GUEST_AVATAR_RUNS = 4
USER_AVATAR_RUNS = 15
GUESTS_PER_IP_PER_DAY = 5
GUEST_DREAMS = 1          # total
USER_DREAMS_PER_MONTH = 10


def row(r):
    return dict(r) if r else None


# ---------------- auth ----------------
def hash_pw(pw: str, salt: str) -> str:
    return hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1).hex()


def current_user(sid: str | None = Cookie(default=None)):
    if not sid:
        return None
    with db() as c:
        return row(c.execute("SELECT u.id, u.email, u.is_guest FROM sessions s JOIN users u ON u.id=s.user_id "
                             "WHERE s.token=?", (sid,)).fetchone())


def require_user(user=Depends(current_user)):
    if not user:
        raise HTTPException(401, "Please log in")
    return user


def start_session(resp: Response, user_id: int):
    tok = secrets.token_urlsafe(32)
    with db() as c:
        c.execute("INSERT INTO sessions VALUES(?,?,?)", (tok, user_id, time.time()))
    resp.set_cookie("sid", tok, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)


def check_credentials(email: str, password: str) -> str:
    email = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or email.endswith("@guest.invalid"):
        raise HTTPException(400, "Enter a valid email")
    if len(password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    return email


def create_account(email: str, password: str, name: str, is_guest: bool) -> int:
    salt = secrets.token_hex(16)
    with db() as c:
        cur = c.execute("INSERT INTO users(email,pw_hash,salt,created,is_guest) VALUES(?,?,?,?,?)",
                        (email, hash_pw(password, salt), salt, time.time(), int(is_guest)))
        uid = cur.lastrowid
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "twin"
        c.execute("INSERT INTO twins(user_id,slug,display_name,created,updated) VALUES(?,?,?,?,?)",
                  (uid, f"{base}-{secrets.token_hex(3)}", name.strip()[:80], time.time(), time.time()))
    return uid


@app.post("/api/register")
def register(resp: Response, email: str = Body(...), password: str = Body(...), name: str = Body(...)):
    email = check_credentials(email, password)
    try:
        uid = create_account(email, password, name, is_guest=False)
    except sqlite3.IntegrityError:
        raise HTTPException(400, "That email is already registered")
    start_session(resp, uid)
    return {"ok": True}


_guest_ips: dict[str, list[float]] = {}


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


@app.post("/api/guest")
def start_guest(request: Request, resp: Response, name: str = Body("", embed=True)):
    """Try it without signing up: a throwaway account the person can save later."""
    cleanup_guests()
    ip, now = client_ip(request), time.time()
    recent = [t for t in _guest_ips.get(ip, []) if now - t < 86400]
    if len(recent) >= GUESTS_PER_IP_PER_DAY:
        raise HTTPException(429, "Too many guest twins from this network today. Create a free account instead.")
    _guest_ips[ip] = recent + [now]
    uid = create_account(f"guest-{secrets.token_hex(8)}@guest.invalid", secrets.token_urlsafe(24),
                         name.strip() or "Guest", is_guest=True)
    start_session(resp, uid)
    return {"ok": True}


@app.post("/api/claim")
def claim_guest(email: str = Body(...), password: str = Body(...), user=Depends(current_user)):
    """Turn a guest twin into a real account, keeping everything they built."""
    if not user or not user["is_guest"]:
        raise HTTPException(400, "Only guest twins can be saved this way")
    email = check_credentials(email, password)
    salt = secrets.token_hex(16)
    try:
        with db() as c:
            c.execute("UPDATE users SET email=?, pw_hash=?, salt=?, is_guest=0 WHERE id=?",
                      (email, hash_pw(password, salt), salt, user["id"]))
    except sqlite3.IntegrityError:
        raise HTTPException(400, "That email is already registered. Log in instead.")
    return {"ok": True}


def cleanup_guests():
    cutoff = time.time() - GUEST_TTL_DAYS * 86400
    with db() as c:
        old = [r["id"] for r in c.execute("SELECT id FROM users WHERE is_guest=1 AND created<?", (cutoff,))]
    for uid in old:
        delete_user(uid)


@app.post("/api/login")
def login(resp: Response, email: str = Body(...), password: str = Body(...)):
    with db() as c:
        u = row(c.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone())
    if not u or not hmac.compare_digest(u["pw_hash"], hash_pw(password, u["salt"])):
        raise HTTPException(401, "Wrong email or password")
    start_session(resp, u["id"])
    return {"ok": True}


@app.post("/api/logout")
def logout(resp: Response, sid: str | None = Cookie(default=None)):
    if sid:
        with db() as c:
            c.execute("DELETE FROM sessions WHERE token=?", (sid,))
    resp.delete_cookie("sid")
    return {"ok": True}


# ---------------- twin helpers ----------------
def my_twin(user) -> dict:
    with db() as c:
        return row(c.execute("SELECT * FROM twins WHERE user_id=?", (user["id"],)).fetchone())


def twin_status(t: dict) -> dict:
    answers = json.loads(t["answers"] or "{}")
    answered = sum(1 for q in QUESTIONS if str(answers.get(q[0], "")).strip())
    has_required = all(str(answers.get(r, "")).strip() for r in REQUIRED)
    return {
        "headshot": bool(t["headshot"]), "consent": bool(t["consent_verified"]), "voice": bool(t["voice_ref"]),
        "voice_seconds": round(t["voice_seconds"] or 0), "answered": answered, "total_questions": len(QUESTIONS),
        "profile": has_required,
        "ready": bool(t["headshot"] and t["voice_ref"] and has_required),
    }


def public_twin(t: dict) -> dict:
    return {"slug": t["slug"], "display_name": t["display_name"], "public": bool(t["public"]),
            "headshot_url": f"/media/{t['headshot']}" if t["headshot"] else None,
            "lipsync_available": engines.sadtalker_available()}


def touch(twin_id: int, **fields):
    fields["updated"] = time.time()
    keys = ", ".join(f"{k}=?" for k in fields)
    with db() as c:
        c.execute(f"UPDATE twins SET {keys} WHERE id=?", (*fields.values(), twin_id))


async def save_upload(f: UploadFile, dst_dir: str) -> str:
    data = await f.read()
    if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"File too large (max {config.MAX_UPLOAD_MB} MB)")
    if not data:
        raise HTTPException(400, "Empty file")
    path = os.path.join(dst_dir, f"upload-{uuid.uuid4().hex}")
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def twin_private_dir(t) -> str:
    d = os.path.join(config.PRIVATE_DIR, str(t["id"]))
    os.makedirs(d, exist_ok=True)
    return d


# ---------------- account / onboarding ----------------
@app.get("/api/me")
def me(user=Depends(current_user)):
    if not user:
        return {"user": None}
    t = my_twin(user)
    guest = bool(user["is_guest"])
    return {"user": {"email": None if guest else user["email"], "is_guest": guest,
                     "guest_days_left": GUEST_TTL_DAYS if guest else None},
            "twin": public_twin(t), "status": twin_status(t), "avatar": avatar_state(t),
            "answers": json.loads(t["answers"] or "{}"), "writing_samples": t["writing_samples"] or ""}


@app.get("/api/questions")
def questions():
    return [{"id": q[0], "section": q[1], "question": q[2], "hint": q[3], "required": q[0] in REQUIRED}
            for q in QUESTIONS]


# ---------------- face → avatar ----------------


def avatar_state(t: dict) -> dict:
    opts = json.loads(t["avatar_options"] or "{}")  # {key: {"file":..., "label":...}}
    chosen = next((k for k, o in opts.items() if o["file"] == t["headshot"]), "photo" if t["headshot"] else None)
    return {"status": t["avatar_status"] or ("none" if not t["photo"] else "idle"), "chosen_key": chosen,
            "has_photo": bool(t["photo"]), "available": engines.avatars_available(),
            "options": [{"style": k, "label": o["label"], "url": f"/media/{o['file']}"} for k, o in opts.items()],
            "chosen_url": f"/media/{t['headshot']}" if t["headshot"] else None}


def _generate_avatars(twin_id: int, photo: str, jobs: dict, seed: int):
    """jobs: {key: (prompt, label)}. New results are added to the twin's existing avatar options."""
    try:
        results = engines.make_avatars(photo, {k: p for k, (p, _) in jobs.items()}, seed)
        with db() as c:
            cur = json.loads(c.execute("SELECT avatar_options FROM twins WHERE id=?", (twin_id,)).fetchone()[0] or "{}")
        for key, png in results.items():
            name = f"{uuid.uuid4().hex}.png"
            with open(os.path.join(config.MEDIA_DIR, name), "wb") as fh:
                fh.write(png)
            cur[key] = {"file": name, "label": jobs[key][1]}
        touch(twin_id, avatar_options=json.dumps(cur), avatar_status="ready")
    except Exception as e:
        log.exception("avatar generation failed")
        with db() as c:  # a failed run doesn't count against the person's limit
            c.execute("UPDATE twins SET avatar_count=MAX(0, COALESCE(avatar_count,0)-1) WHERE id=?", (twin_id,))
        touch(twin_id, avatar_status=f"failed: {str(e)[:200]}")


def start_avatar_job(t: dict, user, jobs: dict | None = None) -> None:
    limit = GUEST_AVATAR_RUNS if user["is_guest"] else USER_AVATAR_RUNS
    if (t["avatar_count"] or 0) >= limit:
        raise HTTPException(429, f"You've used all {limit} avatar generations"
                                 + (". Save your twin to get more." if user["is_guest"] else "."))
    if t["avatar_status"] == "generating" and time.time() - (t["updated"] or 0) < 900:
        raise HTTPException(409, "Still creating your last avatar. One moment.")
    if jobs is None:  # starter set
        presets = avatar_prompts.preset_prompts()
        jobs = {k: (p, avatar_prompts.STYLES[k][0]) for k, p in presets.items()}
    touch(t["id"], avatar_status="generating", avatar_count=(t["avatar_count"] or 0) + 1)
    seed = secrets.randbelow(10_000)
    threading.Thread(target=_generate_avatars, args=(t["id"], t["photo"], jobs, seed), daemon=True).start()


@app.post("/api/twin/headshot")
async def upload_headshot(file: UploadFile = File(...), user=Depends(require_user)):
    """Upload a face photo. It stays private; it's turned into avatar options the person picks from."""
    t = my_twin(user)
    raw = await save_upload(file, twin_private_dir(t))
    photo = os.path.join(twin_private_dir(t), "photo.png")
    try:
        engines.normalize_headshot(raw, photo)
    except RuntimeError:
        raise HTTPException(400, "Couldn't read that image. Try a JPG or PNG.")
    finally:
        os.remove(raw)
    touch(t["id"], photo=photo, avatar_options=None, avatar_status="idle")
    # Use the photo straight away so the person can carry on; they can switch to an avatar when it's ready.
    choose_avatar("photo", user)
    t = my_twin(user)
    if engines.avatars_available() and t["avatar_status"] != "generating":
        start_avatar_job(t, user)
    return avatar_state(my_twin(user))


@app.get("/api/avatar/catalog")
def avatar_catalog():
    return avatar_prompts.catalog()


@app.post("/api/twin/avatar/design")
def design_avatar(style: str = Body(...), outfit: str = Body("keep"), accessories: list[str] = Body([]),
                  background: str = Body("plain"), extra: str = Body(""), user=Depends(require_user)):
    """Custom avatar: style + outfit + accessories + background (+ a short free-text detail)."""
    t = my_twin(user)
    if not t["photo"]:
        raise HTTPException(400, "Add a photo first")
    if not engines.avatars_available():
        raise HTTPException(501, "Avatar design needs the cloud GPU backend")
    try:
        prompt, label = avatar_prompts.build_prompt(style, outfit, accessories, background, extra)
    except ValueError as e:
        raise HTTPException(400, str(e))
    start_avatar_job(t, user, {f"custom-{uuid.uuid4().hex[:6]}": (prompt, label)})
    return avatar_state(my_twin(user))


@app.get("/api/twin/avatar")
def get_avatar(user=Depends(require_user)):
    return avatar_state(my_twin(user))


@app.get("/api/twin/photo")
def my_photo(user=Depends(require_user)):
    t = my_twin(user)
    if not t["photo"] or not os.path.exists(t["photo"]):
        raise HTTPException(404)
    return FileResponse(t["photo"], headers={"Cache-Control": "private, no-store"})


@app.post("/api/twin/avatar/regenerate")
def regenerate_avatar(user=Depends(require_user)):
    t = my_twin(user)
    if not t["photo"]:
        raise HTTPException(400, "Upload a photo first")
    if t["avatar_status"] == "generating":
        return avatar_state(t)
    start_avatar_job(t, user)
    return avatar_state(my_twin(user))


@app.post("/api/twin/avatar/choose")
def choose_avatar(style: str = Body(..., embed=True), user=Depends(require_user)):
    t = my_twin(user)
    if style == "photo":
        if not t["photo"]:
            raise HTTPException(400, "Upload a photo first")
        name = f"{uuid.uuid4().hex}.png"
        shutil.copyfile(t["photo"], os.path.join(config.MEDIA_DIR, name))
    else:
        opt = json.loads(t["avatar_options"] or "{}").get(style)
        if not opt:
            raise HTTPException(400, "Unknown avatar")
        name = opt["file"]
    touch(t["id"], headshot=name)
    return avatar_state(my_twin(user))


WORDS = ("apple river piano tiger silver garden rocket orange mango planet violet thunder candle "
         "harbor falcon maple ocean pepper lantern meadow copper willow cactus marble").split()


def welcome_line(name: str) -> str:
    first = (name or "").split()[0] if (name or "").strip() else "there"
    return (f"Hey {first}! Welcome to your AI twin. I learn every day with you, I understand you, "
            "and I become you. Use me as your twin, all day, every day. Let's get started!")


@app.get("/api/twin/consent-script")
def consent_script(user=Depends(require_user)):
    """One short read-aloud: proves it's their own voice (fresh random code words) AND gives the voice sample."""
    t = my_twin(user)
    code = " ".join(secrets.choice(WORDS) for _ in range(3))
    touch(t["id"], consent_code=code)
    script = (f"My name is {t['display_name']}, and I'm creating an AI twin of my own voice. I give my consent. "
              f"My code words are {code}. Hey, it's me! I'm recording this so my twin can sound just like me. "
              "Honestly, I'm a little curious how this turns out. Let's see if it gets my voice right.")
    return {"code": code, "script": script}


def heard_code(transcript: str, code: str) -> bool:
    words = re.findall(r"[a-z]+", transcript.lower())
    return all(w in words for w in code.split())


@app.post("/api/twin/voice")
async def upload_voice(file: UploadFile = File(...), user=Depends(require_user)):
    """Voice sample + consent in one recording. Every recording must contain the current code words,
    so nobody can pass consent once and then upload someone else's audio."""
    t = my_twin(user)
    if not t["consent_code"]:
        raise HTTPException(400, "Load the script to read first")
    d = twin_private_dir(t)
    raw = await save_upload(file, d)
    full, ref = os.path.join(d, "voice_full.wav"), os.path.join(d, "voice_ref.wav")
    try:
        engines.to_wav(raw, full)
    except RuntimeError:
        raise HTTPException(400, "Couldn't read that audio")
    finally:
        os.remove(raw)
    secs = engines.audio_duration(full)
    if secs < config.MIN_VOICE_SECONDS:
        raise HTTPException(400, f"That was {secs:.0f}s. Please read the whole script (about 15 seconds).")
    # The GPU part (hearing the code words, cloning, the welcome greeting) runs in the background:
    # a cold GPU can take longer than a web request is allowed to stay open.
    touch(t["id"], voice_job=json.dumps({"status": "processing", "started": time.time()}))
    threading.Thread(target=_process_voice, args=(t["id"], full, ref, secs, t["consent_code"], t["display_name"]),
                     daemon=True).start()
    return {"status": "processing", "seconds": round(secs)}


def _process_voice(twin_id, full, ref, secs, code, display_name):
    def done(**job):
        touch(twin_id, voice_job=json.dumps(job))
    try:
        heard = engines.transcribe(full)
        if not heard_code(heard, code):
            touch(twin_id, consent_verified=0, consent_transcript=heard)
            return done(status="failed", error=f"I couldn't hear all three code words. I heard: \"{heard[:300]}\". "
                                                 "Please read the new script, clearly.")
        engines.make_voice_reference(full, ref)
        touch(twin_id, voice_full=full, voice_ref=ref, voice_seconds=secs, consent_verified=1,
              consent_transcript=heard, consent_at=time.time(), speech_transcript=heard,
              consent_code=None)  # code words are single-use
        welcome_url = None
        try:  # the magic moment: their twin greets them in their own voice
            name = f"{uuid.uuid4().hex}.wav"
            engines.speak(welcome_line(display_name), ref, os.path.join(config.MEDIA_DIR, name))
            welcome_url = f"/media/{name}"
        except Exception:
            log.exception("welcome voice failed")
        done(status="ready", seconds=round(secs), welcome_audio_url=welcome_url, welcome_text=welcome_line(display_name))
    except Exception as e:
        log.exception("voice processing failed")
        done(status="failed", error=f"Something went wrong learning your voice ({str(e)[:120]}). Please try again.")


@app.get("/api/twin/voice/status")
def voice_job_status(user=Depends(require_user)):
    return json.loads(my_twin(user)["voice_job"] or '{"status": "none"}')


@app.put("/api/twin/profile")
def save_profile(payload: dict = Body(...), user=Depends(require_user)):
    t = my_twin(user)
    fields = {}
    if "answers" in payload:
        valid = {q[0] for q in QUESTIONS}
        fields["answers"] = json.dumps({k: str(v)[:5000] for k, v in payload["answers"].items() if k in valid})
    if "writing_samples" in payload:
        fields["writing_samples"] = str(payload["writing_samples"])[:20000]
    if "display_name" in payload and str(payload["display_name"]).strip():
        fields["display_name"] = str(payload["display_name"]).strip()[:80]
    if "public" in payload:
        if payload["public"] and user["is_guest"]:
            raise HTTPException(403, "Save your twin with an email before sharing it publicly")
        fields["public"] = int(bool(payload["public"]))
    if fields:
        touch(t["id"], **fields)
    return {"status": twin_status(my_twin(user))}


@app.get("/api/twin/memories")
def list_memories(user=Depends(require_user)):
    t = my_twin(user)
    with db() as c:
        return [row(r) for r in c.execute("SELECT * FROM memories WHERE twin_id=? ORDER BY id DESC", (t["id"],))]


@app.post("/api/twin/memories")
def add_memory(kind: str = Body(...), content: str = Body(...), prompt: str = Body(""), user=Depends(require_user)):
    if kind not in ("fact", "correction") or not content.strip():
        raise HTTPException(400, "Bad memory")
    t = my_twin(user)
    with db() as c:
        c.execute("INSERT INTO memories(twin_id,kind,prompt,content,created) VALUES(?,?,?,?,?)",
                  (t["id"], kind, prompt.strip()[:2000], content.strip()[:4000], time.time()))
    return {"ok": True}


@app.delete("/api/twin/memories/{mid}")
def del_memory(mid: int, user=Depends(require_user)):
    t = my_twin(user)
    with db() as c:
        c.execute("DELETE FROM memories WHERE id=? AND twin_id=?", (mid, t["id"]))
    return {"ok": True}


@app.delete("/api/account")
def delete_account(resp: Response, user=Depends(require_user)):
    """Right to be forgotten: removes the voice, face, profile, memories and chats."""
    delete_user(user["id"])
    resp.delete_cookie("sid")
    return {"ok": True}


def delete_user(user_id: int) -> None:
    with db() as c:
        t = row(c.execute("SELECT * FROM twins WHERE user_id=?", (user_id,)).fetchone())
        media = []
        if t:
            for r in c.execute("SELECT audio, video FROM messages WHERE twin_id=?", (t["id"],)):
                media += [m for m in (r["audio"], r["video"]) if m]
            media += [o["file"] for o in json.loads(t["avatar_options"] or "{}").values()]
            media += [t["headshot"]] if t["headshot"] else []
            media += [r["video"] for r in c.execute("SELECT video FROM dreams WHERE twin_id=?", (t["id"],)) if r["video"]]
            for tbl in ("messages", "memories", "dreams"):
                c.execute(f"DELETE FROM {tbl} WHERE twin_id=?", (t["id"],))
            c.execute("DELETE FROM twins WHERE id=?", (t["id"],))
        c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM users WHERE id=?", (user_id,))
    for m in media:
        try:
            os.remove(os.path.join(config.MEDIA_DIR, m))
        except FileNotFoundError:
            pass
    if t:
        shutil.rmtree(os.path.join(config.PRIVATE_DIR, str(t["id"])), ignore_errors=True)


# ---------------- talking to a twin ----------------
def load_twin_for_chat(slug: str, user) -> tuple[dict, bool]:
    with db() as c:
        t = row(c.execute("SELECT * FROM twins WHERE slug=?", (slug,)).fetchone())
    if not t:
        raise HTTPException(404, "Twin not found")
    is_owner = bool(user and user["id"] == t["user_id"])
    if not (is_owner or t["public"]):
        raise HTTPException(403, "This twin is private")
    return t, is_owner


@app.get("/api/twins/{slug}")
def get_twin(slug: str, user=Depends(current_user)):
    t, is_owner = load_twin_for_chat(slug, user)
    return {**public_twin(t), "is_owner": is_owner, "status": twin_status(t)}


def _video_done(msg_id, status, err):
    with db() as c:
        c.execute("UPDATE messages SET video_status=?, video_error=? WHERE id=?", (status, err, msg_id))


def respond(t: dict, is_owner: bool, conversation: str, text: str, voice: bool, video: bool) -> dict:
    st = twin_status(t)
    if not st["profile"]:
        raise HTTPException(409, "This twin hasn't finished setup yet")
    with db() as c:
        guest = c.execute("SELECT is_guest FROM users WHERE id=?", (t["user_id"],)).fetchone()["is_guest"]
        if guest:
            used = c.execute("SELECT COUNT(*) FROM messages WHERE twin_id=? AND role='assistant' AND created>?",
                             (t["id"], time.time() - 86400)).fetchone()[0]
            if used >= GUEST_DAILY_REPLIES:
                raise HTTPException(429, f"Guest twins get {GUEST_DAILY_REPLIES} replies a day. "
                                         "Save your twin with an email to keep talking.")
    conversation = re.sub(r"[^A-Za-z0-9_-]", "", conversation)[:64] or "default"
    with db() as c:
        hist = [row(r) for r in c.execute(
            "SELECT role, content FROM messages WHERE twin_id=? AND conversation=? ORDER BY id DESC LIMIT 12",
            (t["id"], conversation))][::-1]
        mems = [row(r) for r in c.execute("SELECT * FROM memories WHERE twin_id=? ORDER BY id", (t["id"],))]
    system = build_system_prompt(t, json.loads(t["answers"] or "{}"), mems, voice_mode=voice)
    if is_owner:
        system += ("\n\n(The person chatting now is the real you, testing your twin. "
                   "Stay in character so they can judge how well you mimic them.)")
    try:
        reply = engines.chat(system, hist, text, voice_mode=voice)
    except Exception as e:
        log.exception("LLM failed")
        raise HTTPException(503, f"The local brain (Ollama model {config.LLM_MODEL}) isn't responding: {e}")

    audio_name = video_name = video_status = None
    if voice and t["voice_ref"]:
        audio_name = f"{uuid.uuid4().hex}.wav"
        try:
            engines.speak(reply, t["voice_ref"], os.path.join(config.MEDIA_DIR, audio_name))
        except Exception:
            log.exception("TTS failed")
            audio_name = None
    with db() as c:
        c.execute("INSERT INTO messages(twin_id,conversation,role,content,is_owner,created) VALUES(?,?,?,?,?,?)",
                  (t["id"], conversation, "user", text, int(is_owner), time.time()))
        cur = c.execute("INSERT INTO messages(twin_id,conversation,role,content,audio,is_owner,created) "
                        "VALUES(?,?,?,?,?,?,?)", (t["id"], conversation, "assistant", reply, audio_name,
                                                   int(is_owner), time.time()))
        msg_id = cur.lastrowid
    if video and audio_name and t["headshot"] and engines.sadtalker_available():
        video_name, video_status = f"{uuid.uuid4().hex}.mp4", "rendering"
        with db() as c:
            c.execute("UPDATE messages SET video=?, video_status=? WHERE id=?", (video_name, video_status, msg_id))
        engines.enqueue_video(msg_id, os.path.join(config.MEDIA_DIR, t["headshot"]),
                              os.path.join(config.MEDIA_DIR, audio_name),
                              os.path.join(config.MEDIA_DIR, video_name), _video_done)
    return {"message_id": msg_id, "reply": reply, "audio_url": f"/media/{audio_name}" if audio_name else None,
            "video_status": video_status}


@app.post("/api/twins/{slug}/chat")
def chat(slug: str, text: str = Body(...), conversation: str = Body("default"), voice: bool = Body(False),
         video: bool = Body(False), user=Depends(current_user)):
    t, is_owner = load_twin_for_chat(slug, user)
    if not text.strip():
        raise HTTPException(400, "Say something")
    return respond(t, is_owner, conversation, text.strip()[:2000], voice, video)


@app.post("/api/twins/{slug}/talk")
async def talk(slug: str, file: UploadFile = File(...), conversation: str = Form("default"),
               video: bool = Form(False), user=Depends(current_user)):
    t, is_owner = load_twin_for_chat(slug, user)
    raw = await save_upload(file, os.path.join(config.DATA_DIR, "tmp"))
    try:
        wav = raw + ".wav"
        engines.to_wav(raw, wav, 16000)
        heard = engines.transcribe(wav)
    finally:
        for p in (raw, raw + ".wav"):
            if os.path.exists(p):
                os.remove(p)
    if not heard:
        raise HTTPException(400, "I didn't catch that")
    return {"heard": heard, **respond(t, is_owner, conversation, heard, voice=True, video=video)}


@app.get("/api/messages/{mid}/video")
def video_status(mid: int):
    with db() as c:
        m = row(c.execute("SELECT video, video_status, video_error FROM messages WHERE id=?", (mid,)).fetchone())
    if not m or not m["video"]:
        raise HTTPException(404)
    return {"status": m["video_status"], "url": f"/media/{m['video']}" if m["video_status"] == "ready" else None,
            "error": m["video_error"]}


# ---------------- Dream Life reels ----------------
DREAM_STYLES = {"realistic": "Ultra-realistic", "game": "Game cinematic", "3d": "3D animated", "anime": "Anime",
                "illustrated": "Illustrated"}
DREAM_IDEAS = [
    "I'm living in New York: morning coffee at my favourite café, a jog by the Hudson, then work downtown in a sharp suit",
    "I'm giving the keynote at a huge tech conference, the crowd is on its feet",
    "I'm scoring the winning goal in a packed stadium",
    "I'm on a beach in Bali at sunset, then a rooftop dinner with friends",
    "I'm a chef opening my own restaurant on opening night",
]


def dream_row(r: dict) -> dict:
    return {"id": r["id"], "prompt": r["prompt"], "style": r["style"], "status": r["status"], "title": r["title"],
            "narration": r["narration"], "error": r["error"], "created": r["created"],
            "video_url": f"/media/{r['video']}" if r["video"] else None}


def refresh_dream(r: dict) -> dict:
    if r["status"] != "rendering":
        return r
    try:
        res = engines.poll_dream(r["call_id"])
        if res is None:
            if time.time() - r["created"] > 3600:
                raise RuntimeError("timed out")
            return r
        name = f"{uuid.uuid4().hex}.mp4"
        with open(os.path.join(config.MEDIA_DIR, name), "wb") as fh:
            fh.write(res["mp4"])
        upd = dict(status="ready", video=name, title=res.get("title"), narration=res.get("narration"))
    except Exception as e:
        log.exception("dream %s failed", r["id"])
        upd = dict(status="failed", error=str(e)[:300])
    with db() as c:
        c.execute(f"UPDATE dreams SET {', '.join(k + '=?' for k in upd)}, finished=? WHERE id=?",
                  (*upd.values(), time.time(), r["id"]))
    return {**r, **upd}


@app.get("/api/dreams/ideas")
def dream_ideas():
    return {"ideas": DREAM_IDEAS, "styles": [{"id": k, "label": v} for k, v in DREAM_STYLES.items()],
            "available": engines.dreams_available()}


@app.post("/api/twin/dreams")
def create_dream(dream: str = Body(...), style: str = Body("realistic"), user=Depends(require_user)):
    t = my_twin(user)
    if not engines.dreams_available():
        raise HTTPException(501, "Dream reels need the cloud GPU backend")
    if not (t["photo"] and t["voice_ref"]):
        raise HTTPException(400, "Add your photo and voice first")
    dream = re.sub(r"\s+", " ", dream).strip()
    if len(dream) < 10:
        raise HTTPException(400, "Tell your twin a bit more about the dream")
    try:
        avatar_prompts.clean_extra(dream[:160]) and avatar_prompts.clean_extra(dream[160:500])
    except ValueError:
        raise HTTPException(400, "Let's keep dreams safe for work (nothing explicit, violent or hateful).")
    style = style if style in DREAM_STYLES else "realistic"
    with db() as c:
        if c.execute("SELECT COUNT(*) FROM dreams WHERE twin_id=? AND status='rendering'", (t["id"],)).fetchone()[0]:
            raise HTTPException(409, "Your twin is already filming a dream. Wait for it to finish.")
        if user["is_guest"]:
            n = c.execute("SELECT COUNT(*) FROM dreams WHERE twin_id=?", (t["id"],)).fetchone()[0]
            if n >= GUEST_DREAMS:
                raise HTTPException(429, "Guests get one dream reel. Save your twin to make more.")
        else:
            n = c.execute("SELECT COUNT(*) FROM dreams WHERE twin_id=? AND created>?",
                          (t["id"], time.time() - 30 * 86400)).fetchone()[0]
            if n >= USER_DREAMS_PER_MONTH:
                raise HTTPException(429, f"You've made {USER_DREAMS_PER_MONTH} dream reels this month.")
    answers = json.loads(t["answers"] or "{}")
    hint = "; ".join(str(answers.get(k, "")) for k in ("talk_style", "phrases") if answers.get(k))[:300]
    call_id = engines.start_dream(t["photo"], t["voice_ref"], t["display_name"], dream[:500], style, hint,
                                  secrets.randbelow(10_000))
    with db() as c:
        cur = c.execute("INSERT INTO dreams(twin_id,prompt,style,status,call_id,created) VALUES(?,?,?,?,?,?)",
                        (t["id"], dream[:500], style, "rendering", call_id, time.time()))
        r = row(c.execute("SELECT * FROM dreams WHERE id=?", (cur.lastrowid,)).fetchone())
    return dream_row(r)


@app.get("/api/twin/dreams")
def list_dreams(user=Depends(require_user)):
    t = my_twin(user)
    with db() as c:
        rows = [row(r) for r in c.execute("SELECT * FROM dreams WHERE twin_id=? ORDER BY id DESC", (t["id"],))]
    return [dream_row(refresh_dream(r)) for r in rows]


@app.delete("/api/twin/dreams/{did}")
def delete_dream(did: int, user=Depends(require_user)):
    t = my_twin(user)
    with db() as c:
        r = row(c.execute("SELECT * FROM dreams WHERE id=? AND twin_id=?", (did, t["id"])).fetchone())
        c.execute("DELETE FROM dreams WHERE id=? AND twin_id=?", (did, t["id"]))
    if r and r["video"]:
        try:
            os.remove(os.path.join(config.MEDIA_DIR, r["video"]))
        except FileNotFoundError:
            pass
    return {"ok": True}


@app.post("/api/twin/voice-test")
def voice_test(text: str = Body(..., embed=True), user=Depends(require_user)):
    t = my_twin(user)
    if not t["voice_ref"]:
        raise HTTPException(400, "Upload your voice first")
    name = f"{uuid.uuid4().hex}.wav"
    engines.speak(text[:400], t["voice_ref"], os.path.join(config.MEDIA_DIR, name))
    return {"audio_url": f"/media/{name}"}


@app.get("/api/health")
def health():
    import httpx
    if engines.CLOUD:
        return {"backend": "modal", "llm_model": config.LLM_MODEL, "lipsync": True}
    try:
        tags = [m["name"] for m in httpx.get(f"{config.OLLAMA_URL}/api/tags", timeout=3).json()["models"]]
        llm = config.LLM_MODEL in tags or any(n.startswith(config.LLM_MODEL) for n in tags)
    except Exception:
        llm = False
    return {"llm_model": config.LLM_MODEL, "llm_ready": llm, "lipsync": engines.sadtalker_available()}


@app.post("/api/warm")
def warm():
    """Called when someone opens a chat: boots a cloud GPU while they type, to hide the cold start."""
    try:
        engines.warm_gpu()
    except Exception:
        log.exception("warm-up failed")
    return {"ok": True}


@app.on_event("startup")
def warm_models():
    """Local mode: load the voice and hearing models in the background so the first reply isn't slow."""
    if engines.CLOUD:
        return

    def _warm():
        try:
            engines._load_tts()
            engines.transcribe(os.path.join(os.path.dirname(__file__), "static", "silence.wav"))
            log.info("Models warm")
        except Exception:
            log.exception("Model warm-up failed")
    threading.Thread(target=_warm, daemon=True).start()


# ---------------- static ----------------
app.mount("/media", StaticFiles(directory=config.MEDIA_DIR), name="media")
STATIC = os.path.join(os.path.dirname(__file__), "static")


@app.middleware("http")
async def no_stale_ui(request, call_next):
    if "/." in request.url.path:  # never serve dotfiles (.env, .vercel, ...)
        return Response(status_code=404)
    resp = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path == "/" or request.url.path.startswith("/t/"):
        resp.headers["Cache-Control"] = "no-cache"  # always revalidate so UI updates show up
    return resp


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
@app.get("/t/{slug}")
def spa(slug: str | None = None):
    return FileResponse(os.path.join(STATIC, "index.html"))
