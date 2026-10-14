"""
Drawing helpers: write replacement text into digital pages (PyMuPDF) and scanned page images (Pillow).
"""
import numpy as np
from PIL import ImageFont

from .pii import fitz

# ---------------------------------------------------------------------------
# digital pages
# ---------------------------------------------------------------------------


def insert_replacement(page, box, text):
    """Write `text` into `box`, shrinking the font to fit the original width."""
    x0, y0, x1, y1 = box
    h = max(y1 - y0, 4)
    size = h * 0.8
    width = fitz.get_text_length(text, fontname="helv", fontsize=size)
    avail = max(x1 - x0, 1)
    if width > avail:
        size = max(size * avail / width, 5)  # never smaller than 5pt (may overflow slightly)
    page.insert_text((x0, y1 - h * 0.2), text, fontsize=size, fontname="helv", color=(0, 0, 0))


def apply_redactions(page):
    # images=0: leave images alone, graphics=0: leave lines/shapes alone (older PyMuPDF has no 'graphics')
    try:
        page.apply_redactions(images=0, graphics=0)
    except TypeError:
        page.apply_redactions(images=0)


# ---------------------------------------------------------------------------
# scanned pages (PIL images)
# ---------------------------------------------------------------------------

_FONT_CANDIDATES = ["DejaVuSans.ttf", "Arial.ttf", "arial.ttf", "Helvetica.ttc", "LiberationSans-Regular.ttf"]


def load_font(size: int):
    for name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def fit_font(text, max_w, h):
    size = max(int(h * 0.8), 8)
    while size > 8:
        f = load_font(size)
        if f.getlength(text) <= max_w:
            return f
        size -= 1
    return load_font(8)


def background_color(img, box):
    """Median colour of a thin ring around the box = local paper colour."""
    W, H = img.size
    x0, y0, x1, y1 = [int(v) for v in box]
    crop = np.asarray(img.crop((max(x0 - 4, 0), max(y0 - 4, 0), min(x1 + 4, W), min(y1 + 4, H))))
    if crop.shape[0] < 7 or crop.shape[1] < 7:
        return (255, 255, 255)
    ring = np.concatenate([crop[:3].reshape(-1, 3), crop[-3:].reshape(-1, 3),
                           crop[:, :3].reshape(-1, 3), crop[:, -3:].reshape(-1, 3)])
    return tuple(int(v) for v in np.median(ring, axis=0))
