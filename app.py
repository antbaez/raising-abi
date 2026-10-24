"""Web app for the face merge demo.

Takes one webcam photo of two people (or reuses a previously segmented pair),
segments both faces, lets the users confirm them, generates a portrait of
their baby, then lets them pick a good or bad path for each age. For every age
in merge_face_generation.CHILD_AGES (in parallel) it picks a caption from
captions.json and generates the child's image from it, using the baby portrait
as a reference so it stays the same child. "Surprise me" asks Gemini 3.8 Flash
for a name. The baby monitor's readings come from the Arduino toy over USB
serial (ABI_Face_Feedback.ino), streamed to the page from /api/arduino. At
the end, everything is combined into one keepsake image that phones on the same
Wi-Fi can save by scanning a QR code; phones can open only those keepsake pages.

Usage:
    python app.py            # then open http://localhost:5050
"""

import base64
import io
import json
import os
import queue
import random
import re
import secrets
import socket
import threading
import time
from pathlib import Path

import serial
from flask import Flask, abort, jsonify, request, send_from_directory
from PIL import Image
from serial.tools import list_ports

import keepsake
import merge_face_generation as mfg

ROOT = Path(__file__).parent
PHOTOS_DIR = mfg.PHOTOS_DIR
FACES_DIR = ROOT / "segmented_faces"
OUTPUTS_DIR = ROOT / "outputs"
KEEPSAKES_DIR = OUTPUTS_DIR / "keepsakes"
SERVED_DIRS = {"photos": PHOTOS_DIR, "faces": FACES_DIR, "outputs": OUTPUTS_DIR}
PORT = 5050

MODEL = "gemini"
SESSION_ID_RE = re.compile(r"booth_\d{8}_\d{6}_\d{3}")
RUN_ID_RE = re.compile(r"\d{6,20}")
NAME_MAX_LEN = 40

app = Flask(__name__, static_folder="static", static_url_path="/static")

LOCAL_ADDRESSES = {"127.0.0.1", "::1"}
KEEPSAKE_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{16}")


@app.before_request
def local_only():
    """The app listens on the network so phones can fetch their keepsake, but
    everything except the keepsake pages (/k/...) stays on this computer:
    other devices can't see or delete anyone's photos."""
    if request.remote_addr not in LOCAL_ADDRESSES and not request.path.startswith("/k/"):
        abort(403)


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


# --- Keepsake ----------------------------------------------------------------


def _lan_address() -> str:
    """This computer's address on the local network, for the phone link.
    KEEPSAKE_HOST overrides it."""
    if os.environ.get("KEEPSAKE_HOST"):
        return os.environ["KEEPSAKE_HOST"]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("8.8.8.8", 80))  # picks the outgoing interface; sends nothing
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


@app.post("/api/keepsake")
def make_keepsake():
    """Combine the segmented faces, the baby portrait and every age's image and
    caption into one image, saved under a random token so only the QR code's
    link finds it.

    Body: {"id": session id, "run": run id, "baby_run": run id of the baby
           portrait, "names": {"a", "b", "child"}}.
    Returns {"url": the image, "share": the link for phones, "qr": a data: URL
    of the QR code for "share"}.
    """
    body = request.get_json(silent=True) or {}
    checked = _session_faces(body)
    if not isinstance(checked[0], str):
        return checked
    session_id, run_id, faces = checked
    baby_run = str(body.get("baby_run", ""))
    baby_path = _baby_path(session_id, baby_run) if RUN_ID_RE.fullmatch(baby_run) else None
    if not baby_path or not baby_path.is_file():
        return jsonify(error="The baby portrait isn't ready"), 409
    ages = []
    for age in mfg.CHILD_AGES:
        path = _output_path(session_id, run_id, age)
        if not path.is_file() or not path.with_suffix(".json").is_file():
            return jsonify(error=f"Age {age} isn't ready yet"), 409
        info = json.loads(path.with_suffix(".json").read_text())
        ages.append({**info, "image": Image.open(path)})

    names = _parse_details(body)["names"]
    image = keepsake.build(faces, Image.open(baby_path), ages, (names["a"], names["b"]), names["child"])
    KEEPSAKES_DIR.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(12)
    image.save(KEEPSAKES_DIR / f"{token}.png")
    (KEEPSAKES_DIR / f"{token}.json").write_text(json.dumps({"session": session_id, "run": run_id, "child": names["child"]}))
    share = f"http://{_lan_address()}:{PORT}/k/{token}"
    qr = "data:image/png;base64," + base64.b64encode(keepsake.qr_png(share)).decode()
    return jsonify(url=f"/k/{token}.png", share=share, qr=qr)


KEEPSAKE_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Raising {child}</title>
<style>
  body {{ margin: 0; padding: 16px; background: #08080d; color: #f1f5f9;
         font: 17px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; text-align: center; }}
  img {{ display: block; width: 100%; max-width: 540px; margin: 0 auto 16px; border-radius: 12px; }}
  a {{ display: inline-block; padding: 14px 28px; border-radius: 999px; color: #fff; font-weight: 700;
       text-decoration: none; background: linear-gradient(135deg, #6366f1, #ec4899); }}
  p {{ color: #a3abbd; max-width: 540px; margin: 16px auto 0; }}
</style>
</head>
<body>
<img src="/k/{token}.png" alt="Your Raising ABI keepsake">
<a href="/k/{token}.png" download="{filename}">Save image</a>
<p>On iPhone, you can also press and hold the image, then tap “Save to Photos”.</p>
</body>
</html>
"""


@app.get("/k/<token>")
def keepsake_page(token: str):
    """The page a phone opens from the QR code."""
    if not KEEPSAKE_TOKEN_RE.fullmatch(token) or not (KEEPSAKES_DIR / f"{token}.png").is_file():
        abort(404)
    info_path = KEEPSAKES_DIR / f"{token}.json"
    child = json.loads(info_path.read_text()).get("child", "") if info_path.is_file() else ""
    child = _clean_name(child)
    slug = re.sub(r"[^a-z0-9]+", "-", child.lower()).strip("-")
    return KEEPSAKE_PAGE.format(
        child=child or "ABI", token=token, filename=f"raising-{slug or 'abi'}.png",
    )


@app.get("/k/<token>.png")
def keepsake_image(token: str):
    if not KEEPSAKE_TOKEN_RE.fullmatch(token):
        abort(404)
    return send_from_directory(KEEPSAKES_DIR, f"{token}.png")


# --- Arduino baby monitor ----------------------------------------------------

# The toy prints a JSON "[DATA] {...}" line every 100ms. A background thread
# reads them and hands each one to every open /api/arduino stream. Set
# ARDUINO_PORT to pick the serial port; otherwise the first USB serial device
# is used. Only one program can hold the port, so close the Arduino IDE's
# Serial Monitor while the app runs.
ARDUINO_BAUD = 115200
ARDUINO_RETRY_SECONDS = 2
ARDUINO_LISTENERS: list[queue.Queue] = []
ARDUINO_LISTENERS_LOCK = threading.Lock()
arduino_status = {"connected": False}


def _arduino_port() -> str | None:
    if os.environ.get("ARDUINO_PORT"):
        return os.environ["ARDUINO_PORT"]
    ports = [p.device for p in list_ports.comports() if "usbmodem" in p.device or "usbserial" in p.device]
    return ports[0] if ports else None


def _arduino_publish(message: dict) -> None:
    with ARDUINO_LISTENERS_LOCK:
        listeners = list(ARDUINO_LISTENERS)
    for q in listeners:
        q.put(message)


def _arduino_set_connected(connected: bool) -> None:
    if arduino_status["connected"] != connected:
        arduino_status["connected"] = connected
        _arduino_publish({"connected": connected})


def _read_arduino() -> None:
    """Read the toy's [DATA] lines forever, reconnecting when it's unplugged."""
    while True:
        port = _arduino_port()
        if port:
            try:
                with serial.Serial(port, ARDUINO_BAUD, timeout=1) as conn:
                    print(f"Arduino connected on {port}")
                    _arduino_set_connected(True)
                    while True:
                        line = conn.readline().decode(errors="ignore").strip()
                        if not line.startswith("[DATA]"):
                            continue
                        try:
                            reading = json.loads(line.removeprefix("[DATA]"))
                        except ValueError:
                            continue  # a line cut off mid-way
                        _arduino_publish({"connected": True, "reading": reading})
            except (serial.SerialException, OSError) as e:
                print(f"Arduino on {port} disconnected: {e}")
        _arduino_set_connected(False)
        time.sleep(ARDUINO_RETRY_SECONDS)


@app.get("/api/arduino")
def arduino_stream():
    """Server-sent events: {"connected": bool, "reading": [DATA] fields} for
    each line from the toy, plus {"connected": bool} when it (dis)connects."""
    q = queue.Queue()
    with ARDUINO_LISTENERS_LOCK:
        ARDUINO_LISTENERS.append(q)

    def events():
        try:
            yield f"data: {json.dumps(arduino_status)}\n\n"
            while True:
                try:
                    yield f"data: {json.dumps(q.get(timeout=5))}\n\n"
                except queue.Empty:
                    yield ": ping\n\n"  # notices closed pages so they're dropped
        finally:
            with ARDUINO_LISTENERS_LOCK:
                ARDUINO_LISTENERS.remove(q)

    return app.response_class(events(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    threading.Thread(target=_read_arduino, daemon=True).start()
    # All interfaces, so phones on the same Wi-Fi can open their keepsake;
    # local_only() keeps every other page to this computer.
    app.run(host="0.0.0.0", port=PORT, threaded=True)
