"""Web app for the face merge demo.

Takes one webcam photo of two people (or reuses a previously segmented pair),
segments both faces, lets the users confirm them, generates a portrait of
their baby, then lets them pick a good or bad path for each age. For every age
in merge_face_generation.CHILD_AGES (in parallel) it picks a caption from
captions.json and generates the child's image from it, using the baby portrait
as a reference so it stays the same child. "Surprise me" asks Gemini 3.8 Flash
for a name.

Usage:
    python app.py            # then open http://localhost:5050
"""

import io
import json
import random
import re
import threading
import time
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory
from PIL import Image

import merge_face_generation as mfg

ROOT = Path(__file__).parent
PHOTOS_DIR = mfg.PHOTOS_DIR
FACES_DIR = ROOT / "segmented_faces"
OUTPUTS_DIR = ROOT / "outputs"
SERVED_DIRS = {"photos": PHOTOS_DIR, "faces": FACES_DIR, "outputs": OUTPUTS_DIR}

MODEL = "gemini"
SESSION_ID_RE = re.compile(r"booth_\d{8}_\d{6}_\d{3}")
RUN_ID_RE = re.compile(r"\d{6,20}")
NAME_MAX_LEN = 40

app = Flask(__name__, static_folder="static", static_url_path="/static")


def _face_paths(session_id: str) -> list[Path]:
    return [FACES_DIR / f"{session_id}_face_{label}.png" for label in "ab"]


def _output_path(session_id: str, run_id: str, age: int) -> Path:
    return OUTPUTS_DIR / f"{session_id}_run{run_id}_{MODEL}_age{age:02d}.png"


# Ages are generated in parallel threads, so updates to a session's
# used-captions file are serialized with this lock.
USED_CAPTIONS_LOCK = threading.Lock()


def _used_captions_path(session_id: str) -> Path:
    return FACES_DIR / f"{session_id}_used_captions.json"


def _used_captions(session_id: str, age: int, choice: str) -> list[str]:
    """Caption templates this session has already been shown for age/choice."""
    path = _used_captions_path(session_id)
    with USED_CAPTIONS_LOCK:
        used = json.loads(path.read_text()) if path.is_file() else {}
    return used.get(str(age), {}).get(choice, [])


def _mark_caption_used(session_id: str, age: int, choice: str, template: str) -> None:
    path = _used_captions_path(session_id)
    with USED_CAPTIONS_LOCK:
        used = json.loads(path.read_text()) if path.is_file() else {}
        seen = used.setdefault(str(age), {}).setdefault(choice, [])
        if template not in seen:
            seen.append(template)
        path.write_text(json.dumps(used, indent=2))


def _details_path(session_id: str) -> Path:
    return FACES_DIR / f"{session_id}_details.json"


def _clean_name(value) -> str:
    """Keep names short and plain: letters, digits, spaces, - ' and ."""
    text = re.sub(r"[^\w \-'.]", "", str(value or ""))
    return " ".join(text.split())[:NAME_MAX_LEN]


def _parse_details(body: dict) -> dict:
    names = body.get("names") or {}
    gender = body.get("gender")
    return {
        "names": {key: _clean_name(names.get(key)) for key in ("a", "b", "child")},
        "gender": gender if gender in mfg.CHILD_GENDERS else "any",
    }


def _baby_path(session_id: str, run_id: str) -> Path:
    return OUTPUTS_DIR / f"{session_id}_run{run_id}_{MODEL}_baby.png"


def _session_faces(body: dict):
    """Validate a request's session (and run) ids; return (session_id, run_id,
    faces) or a Flask error response."""
    session_id, run_id = str(body.get("id", "")), str(body.get("run", ""))
    if not SESSION_ID_RE.fullmatch(session_id) or not RUN_ID_RE.fullmatch(run_id):
        return jsonify(error="Unknown session"), 404
    paths = _face_paths(session_id)
    if not all(p.is_file() for p in paths):
        return jsonify(error="Unknown session"), 404
    return session_id, run_id, [Image.open(p) for p in paths]


def _people(details: dict) -> dict:
    names = details["names"]
    return dict(parent_names=(names["a"], names["b"]), child_name=names["child"], gender=details["gender"])


def _session_json(session_id: str) -> dict:
    photo = PHOTOS_DIR / f"{session_id}.jpg"
    details_path = _details_path(session_id)
    details = json.loads(details_path.read_text()) if details_path.is_file() else None
    return {
        "id": session_id,
        "photo": f"/files/photos/{photo.name}" if photo.is_file() else None,
        "faces": [f"/files/faces/{p.name}" for p in _face_paths(session_id)],
        "details": details,
    }


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/files/<kind>/<path:name>")
def files(kind: str, name: str):
    if kind not in SERVED_DIRS:
        abort(404)
    return send_from_directory(SERVED_DIRS[kind], name)


@app.get("/api/config")
def config():
    return jsonify(ages=list(mfg.CHILD_AGES), genders=list(mfg.CHILD_GENDERS), choices=list(mfg.CHILD_CHOICES))


@app.get("/api/sessions")
def sessions():
    """Previously segmented face pairs, newest first."""
    if not FACES_DIR.is_dir():
        return jsonify(sessions=[])
    ids = {p.name.removesuffix("_face_a.png") for p in FACES_DIR.glob("booth_*_face_a.png")}
    ids = [i for i in ids if SESSION_ID_RE.fullmatch(i) and all(p.is_file() for p in _face_paths(i))]
    return jsonify(sessions=[_session_json(i) for i in sorted(ids, reverse=True)])


@app.delete("/api/sessions/<session_id>")
def delete_session(session_id: str):
    """Delete a saved pair: its segmented faces plus the names and
    used-captions files stored next to them. The original photo in photos/
    and any generated images in outputs/ are kept."""
    if not SESSION_ID_RE.fullmatch(session_id):
        return jsonify(error="Unknown session"), 404
    paths = [*_face_paths(session_id), _details_path(session_id), _used_captions_path(session_id)]
    if not any(p.is_file() for p in paths):
        return jsonify(error="Unknown session"), 404
    for p in paths:
        p.unlink(missing_ok=True)
    return jsonify(deleted=session_id)


@app.post("/api/capture")
def capture():
    """Save the uploaded photo and segment the two faces in it."""
    upload = request.files.get("photo")
    if upload is None:
        return jsonify(error="No photo uploaded"), 400

    for d in (PHOTOS_DIR, FACES_DIR):
        d.mkdir(parents=True, exist_ok=True)
    now = time.time()
    session_id = time.strftime("booth_%Y%m%d_%H%M%S", time.localtime(now)) + f"_{int(now * 1000) % 1000:03d}"
    photo = Image.open(io.BytesIO(upload.read())).convert("RGB")
    photo.save(PHOTOS_DIR / f"{session_id}.jpg", quality=95)

    try:
        faces = mfg.segment_faces(photo, count=2)
    except ValueError as e:
        return jsonify(error=str(e)), 422

    for face, path in zip(faces, _face_paths(session_id)):
        face.save(path)
    return jsonify(_session_json(session_id))


@app.post("/api/surprise")
def surprise():
    """Pick a random gender and an AI-suggested name for the child.

    Body: {"names": {"a", "b"}} (the parents' names, optional).
    """
    names = (request.get_json(silent=True) or {}).get("names") or {}
    gender = random.choice(["girl", "any", "boy"])
    try:
        name = mfg.suggest_child_name(gender, (_clean_name(names.get("a")), _clean_name(names.get("b"))))
    except Exception as e:
        return jsonify(error=f"Couldn't pick a name: {e}"), 502
    return jsonify(name=_clean_name(name), gender=gender)


REPLY_MAX_LEN = 2000


@app.post("/api/judge")
def judge():
    """Judge the parents' response to a parenting challenge as good or bad.

    Body: {"situation": text, "reply": text, "age": one of CHILD_AGES,
           "names": {"child"}}.
    """
    body = request.get_json(silent=True) or {}
    age = body.get("age")
    if age not in mfg.CHILD_AGES:
        return jsonify(error=f"Age must be one of {list(mfg.CHILD_AGES)}"), 400
    situation = str(body.get("situation") or "")[:REPLY_MAX_LEN]
    reply = str(body.get("reply") or "")[:REPLY_MAX_LEN]
    child = _clean_name((body.get("names") or {}).get("child"))
    try:
        verdict = mfg.judge_response(situation, reply, age, child)
    except Exception as e:
        return jsonify(error=f"Couldn't judge the response: {e}"), 502
    return jsonify(verdict=verdict)


@app.post("/api/baby")
def baby():
    """Generate a portrait of the session's baby.

    Body: {"id": session id, "run": baby run id, "names": {"a", "b", "child"},
           "gender": one of CHILD_GENDERS}. Names and gender are saved with the
    session for reuse. "any" (Either) is settled as a random girl or boy for
    this baby, and returned as "gender" so later ages use the same one.
    """
    body = request.get_json(silent=True) or {}
    checked = _session_faces(body)
    if not isinstance(checked[0], str):
        return checked
    session_id, run_id, faces = checked

    details = _parse_details(body)
    _details_path(session_id).write_text(json.dumps(details))
    gender = details["gender"] if details["gender"] in ("girl", "boy") else random.choice(["girl", "boy"])
    try:
        image = mfg.child_at_age(MODEL, faces, 1, **_people({**details, "gender": gender}))
    except Exception as e:
        return jsonify(error=f"Generation failed: {e}"), 502

    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _baby_path(session_id, run_id)
    image.save(path)
    return jsonify(url=f"/files/outputs/{path.name}", gender=gender)


@app.post("/api/caption")
def caption():
    """Pick a caption for one age so the page can show it while the image is
    generated; pass its template to /api/generate. It only counts as used once
    its image is made.

    Body: {"id": session id, "run": run id, "age": one of CHILD_AGES,
           "choice": one of CHILD_CHOICES, "names": {"a", "b", "child"}}.
    """
    body = request.get_json(silent=True) or {}
    checked = _session_faces(body)
    if not isinstance(checked[0], str):
        return checked
    session_id = checked[0]
    age, choice = body.get("age"), body.get("choice")
    if age not in mfg.CHILD_AGES or choice not in mfg.CHILD_CHOICES:
        return jsonify(error="Unknown age or choice"), 400
    try:
        template, text = mfg.child_caption(
            age, choice, exclude=_used_captions(session_id, age, choice), **_people(_parse_details(body))
        )
    except Exception as e:
        return jsonify(error=f"Caption failed: {e}"), 502
    return jsonify(template=template, caption=text)


@app.post("/api/generate")
def generate():
    """Pick a caption for one age and generate the child's image in that moment.

    Body: {"id": session id, "run": run id, "baby_run": run id of the baby
           portrait, "age": one of CHILD_AGES, "choice": one of CHILD_CHOICES,
           "names": {"a", "b", "child"}, "gender": one of CHILD_GENDERS,
           "template": caption template from /api/caption (optional)}.
    The baby portrait is passed as a reference so every age is the same child.
    """
    body = request.get_json(silent=True) or {}
    checked = _session_faces(body)
    if not isinstance(checked[0], str):
        return checked
    session_id, run_id, faces = checked
    age, choice = body.get("age"), body.get("choice")
    if age not in mfg.CHILD_AGES:
        return jsonify(error=f"Age must be one of {list(mfg.CHILD_AGES)}"), 400
    if choice not in mfg.CHILD_CHOICES:
        return jsonify(error=f"Choice must be one of {list(mfg.CHILD_CHOICES)}"), 400
    baby_run = str(body.get("baby_run", ""))
    baby_path = _baby_path(session_id, baby_run) if RUN_ID_RE.fullmatch(baby_run) else None
    previous = (1, Image.open(baby_path)) if baby_path and baby_path.is_file() else None

    people = _people(_parse_details(body))
    template = body.get("template")
    try:
        if template in mfg.caption_templates(age, choice):
            caption = mfg.fill_caption(template, people["parent_names"], people["child_name"])
        else:
            template, caption = mfg.child_caption(
                age, choice, exclude=_used_captions(session_id, age, choice), **people
            )
    except Exception as e:
        return jsonify(error=f"Caption failed: {e}"), 502
    try:
        image = mfg.child_at_age(MODEL, faces, age, previous=previous, caption=caption, **people)
    except Exception as e:
        return jsonify(error=f"Generation failed: {e}", caption=caption), 502

    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _output_path(session_id, run_id, age)
    image.save(path)
    path.with_suffix(".json").write_text(json.dumps({"age": age, "choice": choice, "caption": caption}))
    # Only count a caption as used once its image exists, so a failed attempt
    # doesn't use it up.
    _mark_caption_used(session_id, age, choice, template)
    return jsonify(age=age, choice=choice, caption=caption, url=f"/files/outputs/{path.name}")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, threaded=True)
