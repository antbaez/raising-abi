"""Segment the face in each of two photos and generate the face of their
baby with Gemini (Nano Banana) and/or Runway, using two approaches:

    one-step   both faces -> baby of the merged face, in a single prompt
    two-step   both faces -> merged adult face -> baby of that merged face  (disabled, see PIPELINES)

Photos are read from the photos/ directory next to this script. The largest
face in each photo is used.

Usage:
    python merge_face_generation.py                       # two newest photos in photos/
    python merge_face_generation.py alice.jpg bob.jpg     # photos/alice.jpg + photos/bob.jpg
    python merge_face_generation.py alice.jpg bob.jpg --model gemini   # only Gemini (or: runway)
    python merge_face_generation.py alice.jpg bob.jpg --out outputs

Outputs, e.g. for alice.jpg + bob.jpg:
  in --faces-dir (default segmented_faces/):
    alice_face.png, bob_face.png                    segmented input faces
  in --out (default outputs/):
    alice_and_bob_<model>_onestep_baby.png          one-step result
    alice_and_bob_<model>_twostep_merged.png        two-step intermediate merged face (when enabled)
    alice_and_bob_<model>_twostep_baby.png          two-step result (when enabled)

API keys are read from the environment or a .env file next to this script:
    GEMINI_API_KEY        Gemini API key
    RUNWAYML_API_SECRET   Runway API key
"""

import argparse
import base64
import io
import json
import random
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable

import mediapipe as mp
import numpy as np
from dotenv import load_dotenv
from google import genai
from mediapipe.tasks import python as mp_tasks
from mediapipe.tasks.python import vision
from PIL import Image
from runwayml import RunwayML

load_dotenv(Path(__file__).parent / ".env")

# Prompts use {a} / {b} / {c} for the input images; each API fills these in with
# its own way of referring to images (see _image_refs).
ONE_STEP_PROMPT = (
    "Here are two adult faces: {a} and {b}. Merge them into a single face, "
    "then generate the face of that merged person as a 1 year old baby."
)
MERGE_PROMPT = (
    "Here are two adult faces: {a} and {b}. "
    "Generate a single realistic adult face that is an even merge of both faces."
)
BABY_PROMPT = (
    "Here is an adult face: {a}. "
    "Generate the face of this person when they were a 1 year old baby."
)
# Child of the two faces at a given age. Optionally, an image of the same child
# at an earlier age can be passed as a third reference for consistency.
CHILD_AGES = (1, 5, 12, 15, 18, 21)
CHILD_PROMPT = (
    "Here are two adult faces: {a}{name_a} and {b}{name_b}. "
    "Merge them into one face to create their child: {age_desc}{child_name}."
)
# The baby portrait (age 1, no caption): a standalone prompt, without names.
BABY_PORTRAIT_PROMPT = (
    "Here are two adult faces: {a} and {b}. "
    "Generate an image of what it would be like if they had a baby by merging the faces "
    "then making it a 1 year old {noun}. "
    "Generate a close-up portrait of the baby's face. "
    "Do not include any text in the image. "
    "Be sure to take into account the details of the two adults, including hair, "
    "face shape, facial features, and skin tone."
)
# Without a caption: a plain portrait of the child.
PORTRAIT_PROMPT = " Generate a close-up portrait of the child's face."
# With a caption: the child in that moment. Ages in AGES_WITH_PARENTS also show
# both parents; the other ages show the child alone.
AGES_WITH_PARENTS = (1, 5)
SCENE_CHILD_ONLY_PROMPT = (
    " Show only the child, with no other people in the image, in this moment: {caption}"
)
SCENE_WITH_PARENTS_PROMPT = (
    " Show the child together with both parents, who must look like {a} and {b}, "
    "in this moment: {caption}"
)
NO_TEXT_PROMPT = " Do not include any text in the image."
CHILD_GENDERS = ("girl", "boy", "any")

# Text model used to suggest a name for "Surprise me".
TEXT_MODEL = "gemini-3.8-flash"
# Captions for each age and choice; see captions.json.
CAPTIONS_PATH = Path(__file__).parent / "captions.json"
CHILD_CHOICES = ("good", "bad")
# Name suggestion for "Surprise me". A random first letter keeps suggestions varied.
NAME_PROMPT = (
    "Suggest one first name, starting with the letter {letter}, for a {baby} "
    "whose parents are {name_a} and {name_b}. Reply with only the name."
)
NAME_LETTERS = "ABCDEFGHIJKLMNOPRSTVWZ"
JUDGE_PROMPT = (
    "You are judging a parenting game. The child, {child}, is {age} years old.\n"
    "Situation: {situation}\n"
    "The parents' response: {reply}\n"
    "Is this a good, caring and sensible way to handle the situation? "
    "Reply with only one word: good or bad."
)
CHILD_PREVIOUS_PROMPT = (
    " The same child at age {previous_age} is shown in {c}; keep them recognisably the same child."
)

GEMINI_MODEL = "gemini-nano-banana-2.1"
GEMINI_ASPECT_RATIO = "1:1"
GEMINI_IMAGE_SIZE = "1K"  # must be uppercase K
RUNWAY_MODEL = "gen4_image"
RUNWAY_RATIO = "1024:1024"

# Input images are shrunk so their longest side is at most this many pixels.
INPUT_MAX_SIDE = 512
RUNWAY_TAGS = ["person_a", "person_b", "child"]

PHOTOS_DIR = Path(__file__).parent / "photos"
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
MODELS_DIR = Path(__file__).parent / "models"
FACE_DETECTOR_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_detector/"
    "blaze_face_short_range/float16/latest/blaze_face_short_range.tflite"
)
SEGMENTER_URL = (
    "https://storage.googleapis.com/mediapipe-models/image_segmenter/"
    "selfie_multiclass_256x256/float32/latest/selfie_multiclass_256x256.tflite"
)

# selfie_multiclass categories: 0 background, 1 hair, 2 body skin, 3 face skin, 4 clothes, 5 other
HAIR, FACE_SKIN = 1, 3

# How much to grow the detector's box so the crop includes hair, chin and ears.
CROP_PAD_X, CROP_PAD_TOP, CROP_PAD_BOTTOM = 0.5, 0.8, 0.4


def _model_path(url: str) -> str:
    path = MODELS_DIR / url.rsplit("/", 1)[-1]
    if not path.exists():
        MODELS_DIR.mkdir(exist_ok=True)
        print(f"Downloading {path.name}...")
        urllib.request.urlretrieve(url, path)
    return str(path)


# ---------------------------------------------------------------------------
# Face segmentation
# ---------------------------------------------------------------------------

def _segment_crop(crop: np.ndarray, segmenter: vision.ImageSegmenter) -> Image.Image:
    """Keep only face skin and hair in `crop`, on a black background."""
    result = segmenter.segment(mp.Image(image_format=mp.ImageFormat.SRGB, data=crop))
    categories = result.category_mask.numpy_view().reshape(crop.shape[:2])
    mask = np.isin(categories, [HAIR, FACE_SKIN])
    if not mask.any():
        raise ValueError("Face found but segmentation produced an empty mask")
    out = np.zeros_like(crop)
    out[mask] = crop[mask]
    return Image.fromarray(out)


def segment_faces(image: Image.Image, count: int = 2) -> list[Image.Image]:
    """Return the `count` largest faces in `image`, ordered left to right,
    each cropped and cut out onto a black background."""
    rgb = np.asarray(image.convert("RGB"))
    h, w = rgb.shape[:2]
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)

    detector_opts = vision.FaceDetectorOptions(
        base_options=mp_tasks.BaseOptions(model_asset_path=_model_path(FACE_DETECTOR_URL)),
        min_detection_confidence=0.5,
    )
    with vision.FaceDetector.create_from_options(detector_opts) as detector:
        detections = detector.detect(mp_image).detections
    if len(detections) < count:
        raise ValueError(f"Expected {count} face(s), found {len(detections)}")

    boxes = sorted(
        (d.bounding_box for d in detections), key=lambda b: b.width * b.height, reverse=True
    )[:count]
    boxes.sort(key=lambda b: b.origin_x)

    segmenter_opts = vision.ImageSegmenterOptions(
        base_options=mp_tasks.BaseOptions(model_asset_path=_model_path(SEGMENTER_URL)),
        output_category_mask=True,
        output_confidence_masks=False,
    )
    faces = []
    with vision.ImageSegmenter.create_from_options(segmenter_opts) as segmenter:
        for box in boxes:
            # Expand the tight face box to include hair and chin.
            x0 = max(0, int(box.origin_x - box.width * CROP_PAD_X))
            x1 = min(w, int(box.origin_x + box.width * (1 + CROP_PAD_X)))
            y0 = max(0, int(box.origin_y - box.height * CROP_PAD_TOP))
            y1 = min(h, int(box.origin_y + box.height * (1 + CROP_PAD_BOTTOM)))
            faces.append(_segment_crop(np.ascontiguousarray(rgb[y0:y1, x0:x1]), segmenter))
    return faces


def segment_face(image: Image.Image) -> Image.Image:
    """Return the largest face in `image`, cropped and cut out onto a black background."""
    return segment_faces(image, count=1)[0]


# ---------------------------------------------------------------------------
# Image generation
# ---------------------------------------------------------------------------

def _b64_png(image: Image.Image) -> str:
    """PNG-encode `image` for an API request, shrunk to at most INPUT_MAX_SIDE px."""
    image = image.copy()
    image.thumbnail((INPUT_MAX_SIDE, INPUT_MAX_SIDE), Image.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _image_refs(model: str, count: int) -> dict[str, str]:
    """How each API's prompt refers to its input images: by order for Gemini,
    by @tag for Runway."""
    if model == "gemini":
        names = ["the image"] if count == 1 else ["the first image", "the second image", "the third image"]
    else:
        names = [f"@{tag}" for tag in RUNWAY_TAGS]
    return dict(zip("abc", names[:count]))


def generate_gemini(faces: list[Image.Image], prompt: str) -> Image.Image:
    client = genai.Client()  # reads GEMINI_API_KEY
    interaction = client.interactions.create(
        model=GEMINI_MODEL,
        response_format={
            "type": "image",
            "aspect_ratio": GEMINI_ASPECT_RATIO,
            "image_size": GEMINI_IMAGE_SIZE,
        },
        input=[
            {"type": "text", "text": prompt.format(**_image_refs("gemini", len(faces)))},
            *({"type": "image", "data": _b64_png(f), "mime_type": "image/png"} for f in faces),
        ],
    )
    if interaction.output_image is None:
        raise RuntimeError("Gemini returned no image (possibly blocked by safety filters)")
    return Image.open(io.BytesIO(base64.b64decode(interaction.output_image.data)))


def generate_runway(faces: list[Image.Image], prompt: str) -> Image.Image:
    client = RunwayML()  # reads RUNWAYML_API_SECRET
    task = client.text_to_image.create(
        model=RUNWAY_MODEL,
        prompt_text=prompt.format(**_image_refs("runway", len(faces))),
        ratio=RUNWAY_RATIO,
        reference_images=[
            {"uri": "data:image/png;base64," + _b64_png(f), "tag": tag}
            for f, tag in zip(faces, RUNWAY_TAGS)
        ],
    ).wait_for_task_output()
    with urllib.request.urlopen(task.output[0]) as resp:
        return Image.open(io.BytesIO(resp.read()))


GENERATORS = {"gemini": generate_gemini, "runway": generate_runway}


def one_step(model: str, faces: list[Image.Image]) -> dict[str, Image.Image]:
    return {"onestep_baby": GENERATORS[model](faces, ONE_STEP_PROMPT)}


def two_step(model: str, faces: list[Image.Image]) -> dict[str, Image.Image]:
    merged = GENERATORS[model](faces, MERGE_PROMPT)
    baby = GENERATORS[model]([merged], BABY_PROMPT)
    return {"twostep_merged": merged, "twostep_baby": baby}


def _age_noun(age: int, gender: str) -> str:
    if age <= 1:
        return {"girl": "baby girl", "boy": "baby boy"}.get(gender, "baby")
    if age < 13:
        return {"girl": "girl", "boy": "boy"}.get(gender, "child")
    if age >= 18:
        return {"girl": "young woman", "boy": "young man"}.get(gender, "young adult")
    return {"girl": "teenage girl", "boy": "teenage boy"}.get(gender, "teenager")


def _clean(text: str) -> str:
    # Text goes into a str.format template later, so drop braces.
    return text.replace("{", "").replace("}", "").strip()


def _age_desc(age: int, gender: str) -> str:
    article = "an" if str(age).startswith(("8", "11", "18")) else "a"
    return f"{article} {age} year old {_age_noun(age, gender)}"


def _ask_text_model(prompt: str) -> str:
    client = genai.Client()  # reads GEMINI_API_KEY
    interaction = client.interactions.create(
        model=TEXT_MODEL,
        input=prompt,
        generation_config={"thinking_level": "low"},
    )
    return (interaction.output_text or "").strip()


def judge_response(situation: str, reply: str, age: int, child_name: str = "") -> str:
    """Ask TEXT_MODEL whether `reply` is a good way to handle a parenting
    `situation` for a child of `age`. Returns "good" or "bad"; an empty reply
    is "bad" without asking."""
    reply = " ".join(reply.split())
    if not reply:
        return "bad"
    verdict = _ask_text_model(JUDGE_PROMPT.format(
        child=_clean(child_name) or "the child",
        age=age,
        situation=_clean(situation),
        reply=_clean(reply),
    )).lower()
    # Read the first word, in case the model adds punctuation or reasoning.
    word = verdict.split()[0].strip(".,!\"'*") if verdict else ""
    if word not in ("good", "bad"):
        raise RuntimeError(f"Gemini gave an unclear verdict: {verdict[:60]!r}")
    return word


def suggest_child_name(gender: str, parent_names: tuple[str, str] = ("", "")) -> str:
    """Ask TEXT_MODEL for a first name for a baby `gender` ("girl", "boy" or
    "any", which asks for a gender-neutral name)."""
    name_a, name_b = (_clean(n) for n in parent_names)
    name = _ask_text_model(NAME_PROMPT.format(
        letter=random.choice(NAME_LETTERS),
        baby={"girl": "baby girl", "boy": "baby boy"}.get(gender, "baby (a gender-neutral name)"),
        name_a=name_a or "Parent A",
        name_b=name_b or "Parent B",
    ))
    # Keep just the first word, in case the model adds punctuation or extra text.
    name = name.split()[0].strip(".,!\"'") if name else ""
    if not name:
        raise RuntimeError("Gemini returned no name")
    return name


def child_caption(
    age: int, choice: str,
    parent_names: tuple[str, str] = ("", ""),
    child_name: str = "",
    gender: str = "any",
    exclude: Iterable[str] = (),
) -> tuple[str, str]:
    """Randomly sample a caption for `age` and `choice` ("good" or "bad") from
    CAPTIONS_PATH and return (template, filled-in caption).

    captions.json maps age -> choice -> list of caption templates, with
    {parent_a}, {parent_b} and {child} filled in from the names. Templates in
    `exclude` (ones this couple has already seen) are skipped; once every
    template has been used, all of them are eligible again. `gender` is accepted
    for a consistent signature but the captions don't use it. The file is read
    on every call, so edits apply without restarting the app.
    """
    options = caption_templates(age, choice)
    if not options:
        raise RuntimeError(f"No caption in {CAPTIONS_PATH.name} for age {age}, {choice}")
    used = set(exclude)
    template = random.choice([t for t in options if t not in used] or options)
    return template, fill_caption(template, parent_names, child_name)


def caption_templates(age: int, choice: str) -> list[str]:
    """Caption templates in CAPTIONS_PATH for `age` and `choice`."""
    return json.loads(CAPTIONS_PATH.read_text()).get(str(age), {}).get(choice) or []


def fill_caption(template: str, parent_names: tuple[str, str] = ("", ""), child_name: str = "") -> str:
    """Fill {parent_a}, {parent_b} and {child} in a caption template."""
    name_a, name_b, child = (_clean(n) for n in (*parent_names, child_name))
    return template.format(
        parent_a=name_a or "Parent A",
        parent_b=name_b or "Parent B",
        child=child or "their child",
    )


def child_at_age(
    model: str, faces: list[Image.Image], age: int,
    previous: tuple[int, Image.Image] | None = None,
    parent_names: tuple[str, str] = ("", ""),
    child_name: str = "",
    gender: str = "any",
    caption: str = "",
) -> Image.Image:
    """Generate the child of the two `faces` at `age`.

    Without a `caption` this is a close-up portrait. With one, it shows the
    child in that moment: with both parents for ages in AGES_WITH_PARENTS, alone
    for the other ages. `previous` is an optional (age, image) of the same child
    at an earlier age, used for consistency. `parent_names`, `child_name` and
    `gender` ("girl", "boy" or "any") are optional details added to the prompt.
    """
    if age == 1 and not caption and not previous:
        prompt = BABY_PORTRAIT_PROMPT.replace("{noun}", _age_noun(age, gender))
        return GENERATORS[model](list(faces), prompt)
    name_a, name_b, child_name, caption = (_clean(n) for n in (*parent_names, child_name, caption))
    prompt = (
        CHILD_PROMPT
        .replace("{name_a}", f" ({name_a})" if name_a else "")
        .replace("{name_b}", f" ({name_b})" if name_b else "")
        .replace("{age_desc}", _age_desc(age, gender))
        .replace("{child_name}", f" named {child_name}" if child_name else "")
    )
    if not caption:
        prompt += PORTRAIT_PROMPT
    elif age in AGES_WITH_PARENTS:
        prompt += SCENE_WITH_PARENTS_PROMPT.replace("{caption}", caption)
    else:
        prompt += SCENE_CHILD_ONLY_PROMPT.replace("{caption}", caption)
    images = list(faces)
    if previous:
        previous_age, previous_image = previous
        prompt += CHILD_PREVIOUS_PROMPT.replace("{previous_age}", str(previous_age))
        images.append(previous_image)
    prompt += NO_TEXT_PROMPT
    return GENERATORS[model](images, prompt)


# two_step is disabled for now; add it back here to run it again.
PIPELINES = (one_step,)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def find_photos(names: list[str]) -> list[Path]:
    """Resolve two filenames inside PHOTOS_DIR, or pick the two newest photos there
    (older first) if no names are given."""
    PHOTOS_DIR.mkdir(exist_ok=True)
    if names:
        if len(names) != 2:
            raise SystemExit(f"Expected 2 photo filenames, got {len(names)}")
        paths = [PHOTOS_DIR / name for name in names]
        for path in paths:
            if not path.is_file():
                raise SystemExit(f"Photo not found: {path}")
        return paths
    photos = [p for p in PHOTOS_DIR.iterdir() if p.suffix.lower() in PHOTO_EXTS]
    if len(photos) < 2:
        raise SystemExit(f"Need at least 2 photos in {PHOTOS_DIR}, found {len(photos)}")
    return sorted(photos, key=lambda p: p.stat().st_mtime)[-2:]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "images", nargs="*", metavar="image",
        help="two photo filenames in photos/ (default: the two newest photos)",
    )
    parser.add_argument("--model", choices=list(GENERATORS), help="which API to use (default: both)")
    parser.add_argument("--out", type=Path, default=Path("outputs"), help="output directory")
    parser.add_argument(
        "--faces-dir", type=Path, default=Path("segmented_faces"),
        help="directory for the segmented faces",
    )
    args = parser.parse_args()

    image_paths = find_photos(args.images)
    print(f"Using photos {image_paths[0]} and {image_paths[1]}")
    args.out.mkdir(parents=True, exist_ok=True)
    args.faces_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{image_paths[0].stem}_and_{image_paths[1].stem}"

    faces = []
    for image_path in image_paths:
        face = segment_face(Image.open(image_path))
        faces.append(face)
        path = args.faces_dir / f"{image_path.stem}_face.png"
        face.save(path)
        print(f"Saved segmented face -> {path}")

    models = [args.model] if args.model else list(GENERATORS)
    with ThreadPoolExecutor() as pool:
        futures = {
            (model, pipeline.__name__): pool.submit(pipeline, model, faces)
            for model in models
            for pipeline in PIPELINES
        }
        for (model, pipeline), future in futures.items():
            try:
                for name, image in future.result().items():
                    path = args.out / f"{stem}_{model}_{name}.png"
                    image.save(path)
                    print(f"[{model} {pipeline}] saved -> {path}")
            except Exception as e:
                print(f"[{model} {pipeline}] failed: {e}")


if __name__ == "__main__":
    main()
