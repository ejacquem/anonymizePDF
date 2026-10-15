"""Faces of people, for the black boxes over photos (YuNet face detector, ONNX Runtime, offline).

The model file is anonymizer/models/face_detection_yunet_2023mar.onnx (OpenCV model zoo, MIT licence, ~230 KB):
nothing is downloaded at run time. No OpenCV: the anchors and the non-maximum suppression are done here.

A face found by the model is the tight area of the eyes / nose / mouth; find_faces() widens it to the hair and the
chin, so that the whole head is covered, not only the eyes.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np

MODEL = Path(__file__).resolve().parent / "models" / "face_detection_yunet_2023mar.onnx"
MODEL_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"   # the file that was tested
WINDOW = 640            # the model takes a fixed 640 x 640 input: big images are cut into windows
SCORE = 0.6             # face score (sqrt(class * objectness)) below which a detection is dropped
NMS = 0.3               # overlapping detections (IoU above this) are merged, the best one is kept
DIGITAL_DPI = 150       # digital pages are rendered at this resolution to look for faces
_session = None


def _load():
    """The model, loaded once. Refused if the file is not the one that was tested."""
    global _session
    if _session is None:
        if hashlib.sha256(MODEL.read_bytes()).hexdigest() != MODEL_SHA256:
            raise RuntimeError(f"face model changed or damaged: {MODEL}")
        import onnxruntime as ort
        _session = ort.InferenceSession(str(MODEL), providers=["CPUExecutionProvider"])
    return _session


def _decode(res: dict) -> np.ndarray:
    """Model outputs (one per stride) -> rows [x0, y0, x1, y1, score] in window pixels."""
    rows = []
    for stride in (8, 16, 32):
        cls = np.clip(res[f"cls_{stride}"][0, :, 0], 0, 1)
        obj = np.clip(res[f"obj_{stride}"][0, :, 0], 0, 1)
        score = np.sqrt(cls * obj)
        bbox = res[f"bbox_{stride}"][0]
        cols = WINDOW // stride
        idx = np.arange(len(score))
        cx = ((idx % cols) + bbox[:, 0]) * stride
        cy = ((idx // cols) + bbox[:, 1]) * stride
        w = np.exp(bbox[:, 2]) * stride
        h = np.exp(bbox[:, 3]) * stride
        rows.append(np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2, score], axis=1))
    out = np.concatenate(rows)
    return out[out[:, 4] >= SCORE]


def _iou(a: np.ndarray, rest: np.ndarray) -> np.ndarray:
    ix0, iy0 = np.maximum(a[0], rest[:, 0]), np.maximum(a[1], rest[:, 1])
    ix1, iy1 = np.minimum(a[2], rest[:, 2]), np.minimum(a[3], rest[:, 3])
    inter = np.clip(ix1 - ix0, 0, None) * np.clip(iy1 - iy0, 0, None)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_r = (rest[:, 2] - rest[:, 0]) * (rest[:, 3] - rest[:, 1])
    return inter / np.maximum(area_a + area_r - inter, 1e-6)


def _nms(dets: np.ndarray) -> np.ndarray:
    dets = dets[np.argsort(-dets[:, 4])]
    keep = []
    while len(dets):
        keep.append(dets[0])
        if len(dets) == 1:
            break
        dets = dets[1:][_iou(dets[0], dets[1:]) <= NMS]
    return np.array(keep).reshape(-1, 5)


def _starts(n: int) -> list[int]:
    """Window offsets along one axis: overlapping windows that together cover the whole length."""
    if n <= WINDOW:
        return [0]
    step = WINDOW * 3 // 4
    starts = list(range(0, n - WINDOW, step)) + [n - WINDOW]
    return starts


def _detect_at(rgb: np.ndarray, scale: float) -> np.ndarray:
    """Detections (original pixels) on the image resized by `scale`, cut into windows when it is big."""
    from PIL import Image

    h, w = rgb.shape[:2]
    h2, w2 = max(round(h * scale), 1), max(round(w * scale), 1)
    small = np.asarray(Image.fromarray(rgb).resize((w2, h2), Image.BILINEAR))
    bgr = small[:, :, ::-1].astype(np.float32)               # the model expects BGR, 0..255
    session = _load()
    names = [o.name for o in session.get_outputs()]
    found = []
    for y0 in _starts(h2):
        for x0 in _starts(w2):
            window = np.zeros((WINDOW, WINDOW, 3), dtype=np.float32)
            patch = bgr[y0:y0 + WINDOW, x0:x0 + WINDOW]
            window[:patch.shape[0], :patch.shape[1]] = patch
            outs = session.run(None, {"input": window.transpose(2, 0, 1)[None]})
            dets = _decode(dict(zip(names, outs)))
            if len(dets):
                dets[:, [0, 2]] += x0
                dets[:, [1, 3]] += y0
                found.append(dets)
    if not found:
        return np.zeros((0, 5))
    dets = np.concatenate(found)
    dets[:, :4] /= scale
    return dets


def _unrotate(dets: np.ndarray, k: int, h: int, w: int) -> np.ndarray:
    """Boxes found on np.rot90(image, k) -> boxes in the original image (h x w)."""
    x0, y0, x1, y1 = dets[:, 0].copy(), dets[:, 1].copy(), dets[:, 2].copy(), dets[:, 3].copy()
    if k == 1:
        x0, x1, y0, y1 = w - y1, w - y0, x0, x1
    elif k == 2:
        x0, x1, y0, y1 = w - x1, w - x0, h - y1, h - y0
    elif k == 3:
        x0, x1, y0, y1 = y0, y1, h - x1, h - x0
    dets[:, 0], dets[:, 1], dets[:, 2], dets[:, 3] = x0, y0, x1, y1
    return dets


def detect(rgb: np.ndarray) -> np.ndarray:
    """Faces of an RGB image (uint8, H x W x 3): rows [x0, y0, x1, y1, score] in its pixels.
    The model is not rotation invariant: sideways photos (a phone held the wrong way) are looked for turned by
    a quarter, a half and three quarters turns as well."""
    h, w = rgb.shape[:2]
    m = max(h, w)
    found = []
    for k in range(4):
        turned = np.ascontiguousarray(np.rot90(rgb, k))
        # the whole image at the model's size (big faces), at twice that, and at full size for k = 0 (small faces,
        # such as a portrait on an ID card): full size is cut into windows, the slowest part
        scales = [WINDOW / m, 2 * WINDOW / m] + ([1.0] if k == 0 else [])
        for sc in sorted(set(scales)):
            dets = _detect_at(turned, sc)
            if len(dets):
                found.append(_unrotate(dets, k, h, w))
    if not found:
        return np.zeros((0, 5))
    return _nms(np.concatenate(found))


def _widen(box, w_img: int, h_img: int) -> tuple:
    """A face box widened to the whole head (hair above, chin and neck below), clipped to the image."""
    x0, y0, x1, y1 = box
    fw, fh = x1 - x0, y1 - y0
    return (max(x0 - 0.2 * fw, 0), max(y0 - 0.35 * fh, 0),
            min(x1 + 0.2 * fw, w_img), min(y1 + 0.15 * fh, h_img))


def find_faces(img, dpi: int = 300) -> list[tuple]:
    """Boxes (image pixels) covering the faces of people on a page image (PIL image)."""
    rgb = np.asarray(img.convert("RGB"))
    h, w = rgb.shape[:2]
    return [_widen(d[:4], w, h) for d in detect(rgb)]


def find_faces_digital(page) -> list[tuple]:
    """Boxes (PDF points) covering the faces of people on a digital page (rendered to look for them)."""
    from PIL import Image

    pix = page.get_pixmap(dpi=DIGITAL_DPI, alpha=False)
    rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
    k = 72 / DIGITAL_DPI
    return [tuple(v * k for v in b) for b in find_faces(Image.fromarray(np.ascontiguousarray(rgb)))]
