"""Segment the largest face in an image and generate a baby version of it
with both Gemini (Nano Banana) and Runway.

Photos are read from the photos/ directory next to this script.

Usage:
    python single_face_generation.py              # newest photo in photos/
    python single_face_generation.py me.jpg       # photos/me.jpg
    python single_face_generation.py me.jpg --model gemini   # only Gemini (or: runway)
    python single_face_generation.py me.jpg --out outputs

API keys are read from the environment or a .env file next to this script:
    GEMINI_API_KEY        Gemini API key
    RUNWAYML_API_SECRET   Runway API key
"""

import argparse
import base64
import io
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import mediapipe as mp
import numpy as np
from dotenv import load_dotenv
from google import genai
from mediapipe.tasks import python as mp_tasks
from mediapipe.tasks.python import vision
from PIL import Image
from runwayml import RunwayML

load_dotenv(Path(__file__).parent / ".env")

PROMPT = "this is an adult face. generate the face of this person when they were a 1 year old baby"

GEMINI_MODEL = "gemini-nano-banana-2.1"
GEMINI_ASPECT_RATIO = "1:1"
GEMINI_IMAGE_SIZE = "1K"  # must be uppercase K
RUNWAY_MODEL = "gen4_image"
RUNWAY_RATIO = "1024:1024"

# Input images are shrunk so their longest side is at most this many pixels.
INPUT_MAX_SIDE = 512

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

def segment_face(image: Image.Image) -> Image.Image:
    """Return the largest face in `image`, cropped and cut out onto a white background."""
    rgb = np.asarray(image.convert("RGB"))
    h, w = rgb.shape[:2]
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)

    detector_opts = vision.FaceDetectorOptions(
        base_options=mp_tasks.BaseOptions(model_asset_path=_model_path(FACE_DETECTOR_URL)),
        min_detection_confidence=0.5,
    )
    with vision.FaceDetector.create_from_options(detector_opts) as detector:
        detections = detector.detect(mp_image).detections
    if not detections:
        raise ValueError("No face found in image")

    box = max(detections, key=lambda d: d.bounding_box.width * d.bounding_box.height).bounding_box

    # Expand the tight face box to include hair and chin.
    x0 = max(0, int(box.origin_x - box.width * CROP_PAD_X))
    x1 = min(w, int(box.origin_x + box.width * (1 + CROP_PAD_X)))
    y0 = max(0, int(box.origin_y - box.height * CROP_PAD_TOP))
    y1 = min(h, int(box.origin_y + box.height * (1 + CROP_PAD_BOTTOM)))
    crop = np.ascontiguousarray(rgb[y0:y1, x0:x1])

    segmenter_opts = vision.ImageSegmenterOptions(
        base_options=mp_tasks.BaseOptions(model_asset_path=_model_path(SEGMENTER_URL)),
        output_category_mask=True,
        output_confidence_masks=False,
    )
    with vision.ImageSegmenter.create_from_options(segmenter_opts) as segmenter:
        result = segmenter.segment(mp.Image(image_format=mp.ImageFormat.SRGB, data=crop))
    categories = result.category_mask.numpy_view().reshape(crop.shape[:2])
    mask = np.isin(categories, [HAIR, FACE_SKIN])
    if not mask.any():
        raise ValueError("Face found but segmentation produced an empty mask")

    out = np.full_like(crop, 255)
    out[mask] = crop[mask]
    return Image.fromarray(out)


# ---------------------------------------------------------------------------
# Image generation
# ---------------------------------------------------------------------------

def _png_bytes(image: Image.Image) -> bytes:
    """PNG-encode `image` for an API request, shrunk to at most INPUT_MAX_SIDE px."""
    image = image.copy()
    image.thumbnail((INPUT_MAX_SIDE, INPUT_MAX_SIDE), Image.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def generate_gemini(face: Image.Image, prompt: str = PROMPT) -> Image.Image:
    client = genai.Client()  # reads GEMINI_API_KEY
    interaction = client.interactions.create(
        model=GEMINI_MODEL,
        response_format={
            "type": "image",
            "aspect_ratio": GEMINI_ASPECT_RATIO,
            "image_size": GEMINI_IMAGE_SIZE,
        },
        input=[
            {"type": "text", "text": prompt},
            {
                "type": "image",
                "data": base64.b64encode(_png_bytes(face)).decode("utf-8"),
                "mime_type": "image/png",
            },
        ],
    )
    if interaction.output_image is None:
        raise RuntimeError("Gemini returned no image (possibly blocked by safety filters)")
    return Image.open(io.BytesIO(base64.b64decode(interaction.output_image.data)))


def generate_runway(face: Image.Image, prompt: str = PROMPT) -> Image.Image:
    client = RunwayML()  # reads RUNWAYML_API_SECRET
    data_uri = "data:image/png;base64," + base64.b64encode(_png_bytes(face)).decode("utf-8")
    task = client.text_to_image.create(
        model=RUNWAY_MODEL,
        prompt_text=prompt,
        ratio=RUNWAY_RATIO,
        reference_images=[{"uri": data_uri, "tag": "face"}],
    ).wait_for_task_output()
    with urllib.request.urlopen(task.output[0]) as resp:
        return Image.open(io.BytesIO(resp.read()))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def find_photo(name: str | None) -> Path:
    """Resolve `name` inside PHOTOS_DIR, or pick the newest photo there if no name is given."""
    PHOTOS_DIR.mkdir(exist_ok=True)
    if name:
        path = PHOTOS_DIR / name
        if not path.is_file():
            raise SystemExit(f"Photo not found: {path}")
        return path
    photos = [p for p in PHOTOS_DIR.iterdir() if p.suffix.lower() in PHOTO_EXTS]
    if not photos:
        raise SystemExit(f"No photos in {PHOTOS_DIR}")
    return max(photos, key=lambda p: p.stat().st_mtime)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("image", nargs="?", help="photo filename in photos/ (default: newest photo)")
    parser.add_argument("--model", choices=["gemini", "runway"], help="which API to use (default: both)")
    parser.add_argument("--out", type=Path, default=Path("outputs"), help="output directory")
    args = parser.parse_args()

    image_path = find_photo(args.image)
    print(f"Using photo {image_path}")
    args.out.mkdir(parents=True, exist_ok=True)
    stem = image_path.stem

    face = segment_face(Image.open(image_path))
    face_path = args.out / f"{stem}_face.png"
    face.save(face_path)
    print(f"Saved segmented face -> {face_path}")

    generators = {"gemini": generate_gemini, "runway": generate_runway}
    if args.model:
        generators = {args.model: generators[args.model]}
    with ThreadPoolExecutor() as pool:
        futures = {name: pool.submit(fn, face) for name, fn in generators.items()}
        for name, future in futures.items():
            try:
                path = args.out / f"{stem}_baby_{name}.png"
                future.result().save(path)
                print(f"[{name}] saved -> {path}")
            except Exception as e:
                print(f"[{name}] failed: {e}")


if __name__ == "__main__":
    main()
