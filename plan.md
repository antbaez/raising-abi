# Face Merge Demo — Plan

Interactive photo-booth demo: two people stand in front of a camera, choose when to take the photo, confirm their faces were captured, and get generated images of a merged face as a baby and a child.

## Flow

```
Live preview ──► User presses button ──► Capture frame ──► Detect + segment faces
                                                                   │
                         ┌──────── Retake ◄──── Confirm screen ◄───┘
                         ▼                          │ Use these
                   Live preview                     ▼
                                   Image API: merge faces → baby + child
                                                    │
                                                    ▼
                                             Result screen ──► Reset
```

## Stages

### 1. Live preview
- Show the camera feed (mirrored) so users can position themselves.
- Display only; no processing required.
- Optional: lightweight face detection as a visual hint ("2 faces detected"), never as a trigger.

### 2. User-triggered capture
- Users press a button (on-screen, key, or physical USB button).
- Optional 3-2-1 countdown, then grab one frame.
- Optional: keep a small buffer of recent frames and pick the sharpest.

### 3. Face detection + segmentation (on the captured frame only)
- Detect faces (MediaPipe / InsightFace) → boxes + landmarks.
- Choose the two foreground faces: keep the two largest boxes.
- Align each face using landmarks; crop.
- Segment each face (face/hair mask, e.g. MediaPipe face mesh or a face-parsing model) to cut it out from the background.
- Quality checks: exactly two faces found, sufficient size, not blurry, roughly frontal.

### 4. Confirmation screen
- Show the two segmented faces side by side.
- "Use these" → continue; "Retake" → back to live preview.
- On failure (fewer than two faces, bad quality), show a message and return to preview.

### 5. Generation (image model API)
- Send the two face images to an image model API.
- Step 1: merge the two faces into one face.
- Step 2: from the merged face, generate a baby version and a child version.
- Run in a background thread/process so the UI stays responsive; show the captured faces and a progress indicator meanwhile.
- Timeout + error handling → "Try again" and reset.

### 6. Result + reset
- Show the merged face, baby, and child images.
- Return to live preview after a timeout or button press.

## Architecture

- **Main loop / UI:** camera preview, button handling, screens driven by a simple state machine:
  `PREVIEW → COUNTDOWN → PROCESSING → CONFIRM → GENERATING → RESULT → PREVIEW`
- **Face module:** detect, select top two, align, segment, quality check.
- **Generation module:** API client wrapping the merge and aging calls; runs off the UI thread.
- **Platform options:** Python + OpenCV window (fastest prototype), or browser front end (`getUserMedia`) with a Python backend.

## Build Order

1. Camera preview + capture button.
2. Face detection and top-two selection on the captured frame.
3. Alignment + segmentation; confirmation screen with retake.
4. API integration: merge two faces, then generate baby and child images.
5. Background generation, progress UI, error handling, reset.
6. Polish: countdown, best-frame selection, model/API warm-up.

## Open Technical Questions

- Which image API, and does it accept two reference faces or require merging first?
- Merge in the API vs. locally (embedding average / landmark morph) before aging?
- Do we need true segmentation masks, or are aligned crops sufficient for the API?
- Desktop (OpenCV) vs. web front end?
