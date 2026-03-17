import os
import json
import re
import subprocess
import tempfile
from io import BytesIO
from typing import Optional, List

from PIL import Image, ImageDraw, ImageFilter, ImageFont, features

import arabic_reshaper
from bidi.algorithm import get_display

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ===================== Paths =====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
font_path = os.path.join(BASE_DIR, "Tajawal-Bold.ttf")

font = ImageFont.truetype(font_path, 70)
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
PILLOW_HAS_RAQM = bool(features.check("raqm"))

load_dotenv(os.path.join(BASE_DIR, ".env"))
BOT_TOKEN = os.getenv("BOT_TOKEN")


# ===================== Text Cleaning =====================
def _count_arabic_chars(text: str) -> int:
    return len(re.findall(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", text))


def fix_arabic_mojibake(text: str) -> str:
    suspect_chars = "ØÙÛÚÃÂ"
    suspect_count = sum(text.count(ch) for ch in suspect_chars)
    if suspect_count < 3:
        return text

    try:
        decoded = text.encode("latin-1", errors="ignore").decode("utf-8", errors="ignore")
    except Exception:
        return text

    if _count_arabic_chars(decoded) > _count_arabic_chars(text):
        return decoded
    return text


def clean_text(text: str) -> str:
    text = fix_arabic_mojibake(text)
    text = re.sub(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", text)
    text = re.sub(
        r'[^\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF0-9\s\.\,\!\?\:\;\-\(\)«»"\'ـ%]',
        "",
        text,
    )
    text = " ".join(text.replace("\n", " ").split())
    return text.strip()


def clean_text_v2(text: str) -> str:
    text = fix_arabic_mojibake(text)
    text = re.sub(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", text)
    text = re.sub(
        r"[^\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF0-9\s\.,!?:;\-\(\)«»\"'%،؛؟]",
        "",
        text,
    )
    text = " ".join(text.replace("\n", " ").split())
    return text.strip()


def fix_arabic_mojibake_v2(text: str) -> str:
    suspect_chars = "\u00D8\u00D9\u00DB\u00DA\u00C3\u00C2"
    suspect_count = sum(text.count(ch) for ch in suspect_chars)
    if suspect_count < 3:
        return text

    try:
        decoded = text.encode("latin-1", errors="ignore").decode("utf-8", errors="ignore")
    except Exception:
        return text

    if _count_arabic_chars(decoded) > _count_arabic_chars(text):
        return decoded
    return text


def clean_text_safe(text: str) -> str:
    text = fix_arabic_mojibake_v2(text)
    text = re.sub(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", text)
    text = re.sub(
        r"[^\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF0-9\s\.,!?:;\-\(\)\u00AB\u00BB\"'%\u060C\u061B\u061F]",
        "",
        text,
    )
    text = " ".join(text.replace("\n", " ").split())
    text = text.strip()
    text = re.sub(r"\.{2,}\s*$", "", text)
    return text.strip()


# ===================== Arabic helpers =====================
def reshape_ar(text: str) -> str:
    return get_display(arabic_reshaper.reshape(text))


def get_text_render_parts(text: str, reshape_enabled: bool = True, prefer_raqm: bool = True):
    has_arabic = _count_arabic_chars(text) > 0
    if reshape_enabled and prefer_raqm and has_arabic and PILLOW_HAS_RAQM:
        return text, {"direction": "rtl", "language": "ar"}
    return (reshape_ar(text) if reshape_enabled else text), {}


def prepare_text(text: str, reshape_enabled: bool = True, prefer_raqm: bool = True) -> str:
    rendered_text, _ = get_text_render_parts(
        text,
        reshape_enabled=reshape_enabled,
        prefer_raqm=prefer_raqm,
    )
    return rendered_text


def text_length(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    reshape_enabled: bool = True,
    prefer_raqm: bool = True,
):
    rendered_text, draw_kwargs = get_text_render_parts(
        text,
        reshape_enabled=reshape_enabled,
        prefer_raqm=prefer_raqm,
    )
    return draw.textlength(rendered_text, font=font, **draw_kwargs)


def text_bbox(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    reshape_enabled: bool = True,
    prefer_raqm: bool = True,
):
    rendered_text, draw_kwargs = get_text_render_parts(
        text,
        reshape_enabled=reshape_enabled,
        prefer_raqm=prefer_raqm,
    )
    bbox = draw.textbbox((0, 0), rendered_text, font=font, **draw_kwargs)
    return (bbox[2] - bbox[0], bbox[3] - bbox[1])


# ===================== Image fit =====================
def fit_image_to_box(
    img: Image.Image,
    box,
    top_bias: float = 0.20,
    left_bias: float = 0.50,
    zoom: float = 1.0,
) -> Image.Image:
    l, t, r, b = box
    bw, bh = r - l, b - t
    iw, ih = img.size

    target_ratio = bw / bh
    img_ratio = iw / ih
    zoom = max(1.0, float(zoom))

    if img_ratio > target_ratio:
        new_w = max(1, int((ih * target_ratio) / zoom))
        extra_w = max(0, iw - new_w)
        left_bias = min(max(left_bias, 0.0), 1.0)
        left = int(extra_w * left_bias)
        img = img.crop((left, 0, left + new_w, ih))
    else:
        new_h = max(1, int((iw / target_ratio) / zoom))
        extra_h = max(0, ih - new_h)
        top_bias = min(max(top_bias, 0.0), 1.0)
        top = int(extra_h * top_bias)
        img = img.crop((0, top, iw, top + new_h))

    return img.resize((bw, bh), Image.LANCZOS)


def resolve_image_crop_settings(img: Image.Image, template_cfg: dict):
    iw, ih = img.size
    img_ratio = iw / max(1, ih)

    top_bias = float(template_cfg.get("top_bias", 0.20))
    left_bias = float(template_cfg.get("left_bias", 0.50))
    image_zoom = float(template_cfg.get("image_zoom", 1.0))

    if img_ratio < float(template_cfg.get("portrait_ratio_threshold", 0.90)):
        top_bias = float(template_cfg.get("portrait_top_bias", top_bias))
        left_bias = float(template_cfg.get("portrait_left_bias", left_bias))
        image_zoom = float(template_cfg.get("portrait_image_zoom", image_zoom))

    return top_bias, left_bias, image_zoom


def build_shape_mask(size, shape: str, bleed: int = 0, feather: float = 0.0):
    shape = str(shape or "rectangle").lower()
    scale = 4
    bleed = max(0, int(bleed))

    large_size = (size[0] * scale, size[1] * scale)
    mask = Image.new("L", large_size, 0)
    d = ImageDraw.Draw(mask)

    if shape == "ellipse":
        box = (
            -bleed * scale,
            -bleed * scale,
            large_size[0] + bleed * scale,
            large_size[1] + bleed * scale,
        )
        d.ellipse(box, fill=255)
    else:
        d.rectangle((0, 0, large_size[0], large_size[1]), fill=255)

    mask = mask.resize(size, Image.LANCZOS)
    if feather > 0:
        mask = mask.filter(ImageFilter.GaussianBlur(radius=feather))
    return mask


# ===================== Remove white background =====================
def remove_near_white_background(img: Image.Image, threshold: int = 235, softness: int = 25) -> Image.Image:
    """
    Removes near-white background and makes it transparent تدريجياً.
    """
    img = img.convert("RGBA")
    pixels = img.load()
    w, h = img.size

    for y in range(h):
        for x in range(w):
            r, g, b, a = pixels[x, y]

            mx = max(r, g, b)
            mn = min(r, g, b)

            if r >= threshold and g >= threshold and b >= threshold:
                whiteness = (r + g + b) / 3
                alpha = int(max(0, min(255, (255 - whiteness) * (255 / max(1, softness)))))
                pixels[x, y] = (r, g, b, alpha)

            elif mx >= threshold and (mx - mn) <= 18:
                whiteness = (r + g + b) / 3
                fade = max(0, min(255, int((255 - whiteness) * 8)))
                new_alpha = max(0, min(a, fade))
                pixels[x, y] = (r, g, b, new_alpha)

    return img


def autocrop_transparent(img: Image.Image, padding: int = 8) -> Image.Image:
    """
    يقص الصورة بناء على الأجزاء غير الشفافة بعد إزالة الخلفية.
    """
    img = img.convert("RGBA")
    alpha = img.getchannel("A")
    bbox = alpha.getbbox()
    if not bbox:
        return img

    l, t, r, b = bbox
    l = max(0, l - padding)
    t = max(0, t - padding)
    r = min(img.width, r + padding)
    b = min(img.height, b + padding)

    return img.crop((l, t, r, b))


# ===================== Line breaking =====================
def break_lines_ar_balanced(draw, text, font, max_width, max_lines=4, reshape_enabled: bool = True):
    words = text.split()
    if not words:
        return [""]

    lines, cur = [], []
    for w in words:
        test = " ".join(cur + [w])
        wpx, _ = text_bbox(draw, test, font, reshape_enabled=reshape_enabled)
        if wpx <= max_width:
            cur.append(w)
        else:
            if cur:
                lines.append(" ".join(cur))
            cur = [w]
    if cur:
        lines.append(" ".join(cur))

    while len(lines) > max_lines:
        best_i = None
        best_len = None
        for i in range(len(lines) - 1):
            candidate = lines[i] + " " + lines[i + 1]
            wpx, _ = text_bbox(draw, candidate, font, reshape_enabled=reshape_enabled)
            if wpx <= max_width:
                if best_len is None or wpx < best_len:
                    best_len = wpx
                    best_i = i
        if best_i is None:
            break
        lines = lines[:best_i] + [lines[best_i] + " " + lines[best_i + 1]] + lines[best_i + 2:]

    changed = True
    guard = 0
    while changed and guard < 16 and len(lines) > 1:
        changed = False
        guard += 1
        for i in range(len(lines) - 1):
            cur_words = lines[i].split()
            next_words = lines[i + 1].split()
            if not cur_words or len(next_words) <= 1:
                continue

            cur_w, _ = text_bbox(draw, lines[i], font, reshape_enabled=reshape_enabled)
            next_w, _ = text_bbox(draw, lines[i + 1], font, reshape_enabled=reshape_enabled)
            before_gap = abs(cur_w - next_w)

            candidate_cur = " ".join(cur_words + [next_words[0]])
            cand_cur_w, _ = text_bbox(draw, candidate_cur, font, reshape_enabled=reshape_enabled)
            if cand_cur_w > max_width:
                continue
            candidate_next = " ".join(next_words[1:])
            cand_next_w, _ = text_bbox(draw, candidate_next, font, reshape_enabled=reshape_enabled)

            after_gap = abs(cand_cur_w - cand_next_w)
            if after_gap + 18 < before_gap:
                lines[i] = candidate_cur
                lines[i + 1] = candidate_next
                changed = True

    return [ln.strip() for ln in lines if ln.strip()]


def split_short_text_two_lines(text: str) -> List[str]:
    words = [w for w in text.split() if w.strip()]
    if not words:
        return [""]
    if len(words) == 1:
        return [words[0]]
    if len(words) == 2:
        return [words[0], words[1]]

    mid = (len(words) + 1) // 2
    return [" ".join(words[:mid]), " ".join(words[mid:])]


def split_short_text_balanced(draw, text: str, font, max_width: int, reshape_enabled: bool = True) -> List[str]:
    words = [w for w in text.split() if w.strip()]
    if not words:
        return [""]
    if len(words) == 1:
        return [words[0]]
    if len(words) == 2:
        return [words[0], words[1]]

    best_lines = split_short_text_two_lines(text)
    best_score = None

    for i in range(1, len(words)):
        l1 = " ".join(words[:i])
        l2 = " ".join(words[i:])
        w1, _ = text_bbox(draw, l1, font, reshape_enabled=reshape_enabled)
        w2, _ = text_bbox(draw, l2, font, reshape_enabled=reshape_enabled)
        if w1 > max_width or w2 > max_width:
            continue
        score = abs(w1 - w2)
        if best_score is None or score < best_score:
            best_score = score
            best_lines = [l1, l2]

    return best_lines


# ===================== Template cutouts =====================
def apply_template_cutouts(base: Image.Image, cutouts: List) -> Image.Image:
    if not cutouts:
        return base
    base = base.copy()
    d = ImageDraw.Draw(base)
    for item in cutouts:
        shape = "rectangle"
        fill = (0, 0, 0, 0)

        if isinstance(item, dict):
            box = item.get("box")
            shape = str(item.get("shape", "rectangle")).lower()
            fill = tuple(item.get("fill", [0, 0, 0, 0]))
        else:
            box = item

        if not box or len(box) != 4:
            continue

        l, t, r, b = box
        if shape == "ellipse":
            d.ellipse((l, t, r, b), fill=fill)
        else:
            d.rectangle((l, t, r, b), fill=fill)
    return base


# ===================== Templates system =====================
def resolve_path(p: str) -> str:
    if not p:
        return p
    if os.path.isabs(p):
        return p
    return os.path.join(BASE_DIR, p)


def ensure_existing_path(primary_path: Optional[str], fallback_path: Optional[str] = None) -> Optional[str]:
    if primary_path and os.path.isfile(primary_path):
        return primary_path
    if fallback_path and os.path.isfile(fallback_path):
        return fallback_path
    return primary_path or fallback_path


def find_linux_arabic_font() -> Optional[str]:
    candidates = [
        "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
        "/usr/share/fonts/opentype/noto/NotoNaskhArabic-Bold.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansArabic-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def load_templates() -> dict:
    templates = {}
    disabled_templates = {"mubasher"}

    if not os.path.isdir(TEMPLATES_DIR):
        print("[Templates] Folder not found:", TEMPLATES_DIR)
        return templates

    for folder in sorted(os.listdir(TEMPLATES_DIR)):
        path = os.path.join(TEMPLATES_DIR, folder)
        if not os.path.isdir(path):
            continue
        if folder in disabled_templates:
            continue

        cfg_path = os.path.join(path, "config.json")
        if not os.path.isfile(cfg_path):
            continue

        try:
            with open(cfg_path, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
        except Exception as e:
            print(f"[Templates] JSON error in {cfg_path}: {e}")
            continue

        if not bool(cfg.get("enabled", True)):
            print(f"[Templates] '{folder}' disabled via config.json")
            continue

        if "template_path" not in cfg or "text_box" not in cfg:
            print(f"[Templates] Missing keys in {cfg_path} (need template_path, text_box)")
            continue

        cfg["id"] = folder
        cfg["template_path"] = resolve_path(cfg["template_path"])
        cfg["font_bold_path"] = resolve_path(cfg.get("font_bold_path", "HEADLINERBOLD.otf"))

        if cfg.get("name_font_bold_path"):
            cfg["name_font_bold_path"] = resolve_path(cfg["name_font_bold_path"])
        if cfg.get("name_arabic_font_bold_path"):
            cfg["name_arabic_font_bold_path"] = resolve_path(cfg["name_arabic_font_bold_path"])
        if cfg.get("name_no_raqm_font_bold_path"):
            cfg["name_no_raqm_font_bold_path"] = resolve_path(cfg["name_no_raqm_font_bold_path"])
        if cfg.get("caption_font_bold_path"):
            cfg["caption_font_bold_path"] = resolve_path(cfg["caption_font_bold_path"])

        if not os.path.isfile(cfg["template_path"]):
            for candidate in ("template.png", "template.jpg", "template.jpeg", "template.webp"):
                candidate_path = os.path.join(path, candidate)
                if os.path.isfile(candidate_path):
                    cfg["template_path"] = candidate_path
                    break

        if not os.path.isfile(cfg["template_path"]):
            print(f"[Templates] template not found for '{folder}': {cfg['template_path']}")
            continue

        if not os.path.isfile(cfg["font_bold_path"]):
            print(f"[Templates] font not found for '{folder}': {cfg['font_bold_path']}")
            continue

        if cfg.get("name_font_bold_path") and not os.path.isfile(cfg["name_font_bold_path"]):
            print(f"[Templates] name font not found for '{folder}': {cfg['name_font_bold_path']}")
            continue
        if cfg.get("name_arabic_font_bold_path") and not os.path.isfile(cfg["name_arabic_font_bold_path"]):
            print(
                f"[Templates] arabic name font not found for '{folder}': "
                f"{cfg['name_arabic_font_bold_path']} - falling back to name_font_bold_path"
            )
            cfg["name_arabic_font_bold_path"] = cfg.get("name_font_bold_path", cfg["font_bold_path"])
        if cfg.get("name_no_raqm_font_bold_path") and not os.path.isfile(cfg["name_no_raqm_font_bold_path"]):
            print(
                f"[Templates] no-raqm name font not found for '{folder}': "
                f"{cfg['name_no_raqm_font_bold_path']} - falling back to arabic name font"
            )
            cfg["name_no_raqm_font_bold_path"] = cfg.get(
                "name_arabic_font_bold_path",
                cfg.get("name_font_bold_path", cfg["font_bold_path"]),
            )

        if cfg.get("caption_font_bold_path") and not os.path.isfile(cfg["caption_font_bold_path"]):
            print(f"[Templates] caption font not found for '{folder}': {cfg['caption_font_bold_path']}")
            continue

        image_mode = cfg.get("image_mode", "full")
        if image_mode == "box" and "image_box" not in cfg:
            print(f"[Templates] '{folder}' image_mode=box requires image_box in config.json")
            continue

        templates[folder] = cfg

    print("[Templates] Loaded:", list(templates.keys()))
    return templates


def get_templates(context: ContextTypes.DEFAULT_TYPE) -> dict:
    context.bot_data["TEMPLATES"] = load_templates()
    return context.bot_data["TEMPLATES"]


def templates_keyboard(templates: dict) -> InlineKeyboardMarkup:
    buttons = []
    for tid, cfg in templates.items():
        name = cfg.get("name", tid)
        buttons.append([InlineKeyboardButton(f"📌 {name}", callback_data=f"tpl:{tid}")])
    return InlineKeyboardMarkup(buttons)


def get_template_cfg(templates: dict, template_id: Optional[str]) -> dict:
    if template_id and template_id in templates:
        return templates[template_id]
    if templates:
        return next(iter(templates.values()))
    raise RuntimeError("No templates found. Add templates/<name>/config.json + template image")


async def send_rendered_post(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    template_cfg: dict,
    img: Image.Image,
    text: str = "",
):
    out = render_post(img, text, template_cfg)
    out_bio = BytesIO()
    out.save(out_bio, format="PNG")
    out_bio.seek(0)

    await update.message.reply_document(
        document=out_bio,
        filename="post.png",
        read_timeout=60,
        write_timeout=60,
        connect_timeout=30,
        pool_timeout=30,
    )
    context.user_data.clear()


def draw_centered_text_block(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: str,
    box,
    text_color,
    shadow_color,
    shadow_offset,
    max_font_size: int,
    min_font_size: int,
    max_lines: int = 2,
    reshape_enabled: bool = True,
    prefer_raqm: bool = True,
):
    l, t, r, b = box
    box_w, box_h = r - l, b - t
    text = clean_text_safe(text)
    if not text:
        return

    font_size = max_font_size
    final_lines = [text]
    final_heights = []
    final_spacing = 0

    while font_size >= min_font_size:
        font = ImageFont.truetype(font_path, font_size)
        lines = split_short_text_balanced(draw, text, font, box_w, reshape_enabled=reshape_enabled)
        if len(lines) > max_lines:
            lines = break_lines_ar_balanced(
                draw,
                text,
                font,
                box_w,
                max_lines=max_lines,
                reshape_enabled=reshape_enabled,
            )

        widths = []
        heights = []
        for ln in lines:
            wpx, hpx = text_bbox(
                draw,
                ln,
                font,
                reshape_enabled=reshape_enabled,
                prefer_raqm=prefer_raqm,
            )
            widths.append(wpx)
            heights.append(hpx)

        spacing = max(8, int(font_size * 0.10))
        total_h = sum(heights) + spacing * (len(lines) - 1)

        if (max(widths) if widths else 0) <= box_w and total_h <= box_h:
            final_lines = lines
            final_heights = heights
            final_spacing = spacing
            break

        font_size -= 2

    font = ImageFont.truetype(font_path, font_size)
    if not final_heights:
        final_heights = [
            text_bbox(
                draw,
                ln,
                font,
                reshape_enabled=reshape_enabled,
                prefer_raqm=prefer_raqm,
            )[1]
            for ln in final_lines
        ]
        final_spacing = max(8, int(font_size * 0.10))

    total_h = sum(final_heights) + final_spacing * (len(final_lines) - 1)
    y = t + max(0, (box_h - total_h) // 2)

    for i, ln in enumerate(final_lines):
        rendered_text, draw_kwargs = get_text_render_parts(
            ln,
            reshape_enabled=reshape_enabled,
            prefer_raqm=prefer_raqm,
        )
        wpx = draw.textlength(rendered_text, font=font, **draw_kwargs)
        x = l + (box_w - wpx) / 2
        sx, sy = shadow_offset
        draw.text((x + sx, y + sy), rendered_text, font=font, fill=shadow_color, **draw_kwargs)
        draw.text((x, y), rendered_text, font=font, fill=text_color, **draw_kwargs)
        y += final_heights[i] + final_spacing


def draw_fill_box(draw: ImageDraw.ImageDraw, box, fill, radius: int = 0):
    if not box:
        return
    if radius > 0:
        draw.rounded_rectangle(tuple(box), radius=radius, fill=tuple(fill))
    else:
        draw.rectangle(tuple(box), fill=tuple(fill))


def render_text_block_gdi(
    text: str,
    font_path: str,
    box,
    text_color,
    shadow_color,
    shadow_offset,
    max_font_size: int,
    min_font_size: int,
    reshape_enabled: bool = True,
) -> Optional[Image.Image]:
    if os.name != "nt":
        return None

    l, t, r, b = box
    box_w, box_h = r - l, b - t
    if box_w <= 0 or box_h <= 0:
        return None

    payload = {
        "text": prepare_text(clean_text_safe(text), reshape_enabled=reshape_enabled),
        "font_path": os.path.abspath(font_path),
        "out_path": os.path.abspath(os.path.join(tempfile.gettempdir(), f"gdi_text_{next(tempfile._get_candidate_names())}.png")),
        "width": int(box_w),
        "height": int(box_h),
        "max_font_size": int(max_font_size),
        "min_font_size": int(min_font_size),
        "text_color": list(text_color),
        "shadow_color": list(shadow_color),
        "shadow_offset": list(shadow_offset),
    }
    if not payload["text"]:
        return None

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
        payload_path = f.name

    ps_script = r"""
param([string]$PayloadPath)
Add-Type -AssemblyName System.Drawing
$data = Get-Content -Raw -Encoding UTF8 $PayloadPath | ConvertFrom-Json
$bmp = New-Object System.Drawing.Bitmap($data.width, $data.height)
$bmp.MakeTransparent()
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.Clear([System.Drawing.Color]::Transparent)
$g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
$pfc = New-Object System.Drawing.Text.PrivateFontCollection
$pfc.AddFontFile($data.font_path)
$family = $pfc.Families[0]
$fmt = New-Object System.Drawing.StringFormat
$fmt.Alignment = [System.Drawing.StringAlignment]::Center
$fmt.LineAlignment = [System.Drawing.StringAlignment]::Center
$fmt.FormatFlags = [System.Drawing.StringFormatFlags]::NoClip
$rect = New-Object System.Drawing.RectangleF(0, 0, $data.width, $data.height)
$font = $null
for ($size = [int]$data.max_font_size; $size -ge [int]$data.min_font_size; $size -= 2) {
    if ($font) { $font.Dispose() }
    $font = New-Object System.Drawing.Font($family, $size, [System.Drawing.FontStyle]::Regular, [System.Drawing.GraphicsUnit]::Pixel)
    $measured = $g.MeasureString([string]$data.text, $font, $data.width, $fmt)
    if ($measured.Width -le $data.width -and $measured.Height -le $data.height) { break }
}
$shadowColor = [System.Drawing.Color]::FromArgb([int]$data.shadow_color[3], [int]$data.shadow_color[0], [int]$data.shadow_color[1], [int]$data.shadow_color[2])
$textColor = [System.Drawing.Color]::FromArgb(255, [int]$data.text_color[0], [int]$data.text_color[1], [int]$data.text_color[2])
$shadowBrush = New-Object System.Drawing.SolidBrush($shadowColor)
$textBrush = New-Object System.Drawing.SolidBrush($textColor)
$shadowRect = New-Object System.Drawing.RectangleF([single]$data.shadow_offset[0], [single]$data.shadow_offset[1], $data.width, $data.height)
$g.DrawString([string]$data.text, $font, $shadowBrush, $shadowRect, $fmt)
$g.DrawString([string]$data.text, $font, $textBrush, $rect, $fmt)
$bmp.Save($data.out_path, [System.Drawing.Imaging.ImageFormat]::Png)
$textBrush.Dispose()
$shadowBrush.Dispose()
$font.Dispose()
$fmt.Dispose()
$g.Dispose()
$bmp.Dispose()
"""

    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script, "-PayloadPath", payload_path],
            check=True,
            capture_output=True,
            text=True,
        )
        if not os.path.isfile(payload["out_path"]):
            return None
        with Image.open(payload["out_path"]).convert("RGBA") as rendered:
            return rendered.copy()
    except Exception:
        return None
    finally:
        try:
            os.remove(payload_path)
        except OSError:
            pass
        try:
            if os.path.isfile(payload["out_path"]):
                os.remove(payload["out_path"])
        except OSError:
            pass


# ===================== Render =====================
def render_post(news_img: Image.Image, text: str, template_cfg: dict) -> Image.Image:
    template_path = template_cfg["template_path"]
    font_bold_path = ensure_existing_path(template_cfg["font_bold_path"])

    base = Image.open(template_path).convert("RGBA")
    base = apply_template_cutouts(base, template_cfg.get("template_cutouts", []))

    W, H = base.size
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    news_img = news_img.convert("RGBA")

    # إزالة الخلفية البيضاء تلقائياً إذا كان القالب يطلب ذلك
    if bool(template_cfg.get("remove_white_bg", False)):
        news_img = remove_near_white_background(
            news_img,
            threshold=int(template_cfg.get("white_bg_threshold", 235)),
            softness=int(template_cfg.get("white_bg_softness", 25)),
        )
        news_img = autocrop_transparent(
            news_img,
            padding=int(template_cfg.get("transparent_crop_padding", 8)),
        )

    image_mode = template_cfg.get("image_mode", "full")
    top_bias, left_bias, image_zoom = resolve_image_crop_settings(news_img, template_cfg)

    if image_mode == "full":
        bottom = int(template_cfg.get("image_area_bottom", int(H * 0.58)))
        full_box = (0, 0, W, bottom)
        fitted = fit_image_to_box(news_img, full_box, top_bias=top_bias, left_bias=left_bias, zoom=image_zoom)
        canvas.paste(fitted, (0, 0), fitted if fitted.mode == "RGBA" else None)

    else:
        image_box = tuple(template_cfg["image_box"])
        mask_shape = str(template_cfg.get("image_mask_shape", "rectangle")).lower()
        mask_box = tuple(template_cfg.get("image_mask_box", image_box))
        image_offset_x = int(template_cfg.get("image_offset_x", 0))
        image_offset_y = int(template_cfg.get("image_offset_y", 0))

        fitted = fit_image_to_box(
            news_img,
            mask_box,
            top_bias=top_bias,
            left_bias=left_bias,
            zoom=image_zoom,
        )

        mask_bleed = int(template_cfg.get("image_mask_bleed", 6))
        mask_feather = float(template_cfg.get("image_mask_feather", 0))

        mask = build_shape_mask(
            (mask_box[2] - mask_box[0], mask_box[3] - mask_box[1]),
            mask_shape,
            bleed=mask_bleed,
            feather=mask_feather,
        )

        layer_w = mask_box[2] - mask_box[0]
        layer_h = mask_box[3] - mask_box[1]

        image_layer = Image.new("RGBA", (layer_w, layer_h), (0, 0, 0, 0))
        paste_x = image_offset_x
        paste_y = image_offset_y

        image_layer.alpha_composite(fitted, (paste_x, paste_y))
        canvas.paste(image_layer, (mask_box[0], mask_box[1]), mask)

    canvas.alpha_composite(base)
    draw = ImageDraw.Draw(canvas)

    if template_cfg.get("caption_bg_box"):
        draw_fill_box(
            draw,
            template_cfg["caption_bg_box"],
            template_cfg.get("caption_bg_fill", [20, 24, 31, 220]),
            int(template_cfg.get("caption_bg_radius", 0)),
        )

    if template_cfg.get("caption_text") and template_cfg.get("caption_box"):
        draw_centered_text_block(
            draw=draw,
            text=str(template_cfg.get("caption_text", "")),
            font_path=template_cfg.get("caption_font_bold_path", font_bold_path),
            box=tuple(template_cfg["caption_box"]),
            text_color=tuple(template_cfg.get("caption_text_color", [255, 255, 255])),
            shadow_color=tuple(template_cfg.get("caption_shadow_color", [0, 0, 0, 140])),
            shadow_offset=tuple(template_cfg.get("caption_shadow_offset", [2, 3])),
            max_font_size=int(template_cfg.get("caption_max_font_size", 72)),
            min_font_size=int(template_cfg.get("caption_min_font_size", 36)),
            max_lines=int(template_cfg.get("caption_max_lines", 2)),
            reshape_enabled=bool(template_cfg.get("caption_reshape_text", True)),
        )

    if template_cfg.get("name_box"):
        name_box = tuple(template_cfg["name_box"])
        name_font_path = template_cfg.get("name_font_bold_path", font_bold_path)
        name_render_engine = str(template_cfg.get("name_render_engine", "")).lower()
        name_reshape_text = bool(template_cfg.get("name_reshape_text", True))
        if _count_arabic_chars(text) > 0 and template_cfg.get("name_arabic_font_bold_path"):
            name_font_path = template_cfg.get("name_arabic_font_bold_path", name_font_path)
            name_render_engine = str(template_cfg.get("name_arabic_render_engine", name_render_engine)).lower()
            name_reshape_text = bool(template_cfg.get("name_arabic_reshape_text", name_reshape_text))
            if not PILLOW_HAS_RAQM and template_cfg.get("name_no_raqm_font_bold_path"):
                name_font_path = template_cfg.get("name_no_raqm_font_bold_path", name_font_path)
            configured_name_font_exists = bool(name_font_path and os.path.isfile(name_font_path))
            if os.name != "nt" and not PILLOW_HAS_RAQM and not configured_name_font_exists:
                linux_arabic_font = find_linux_arabic_font()
                if linux_arabic_font:
                    print(f"[Arabic] Using Linux fallback font for name: {linux_arabic_font}")
                    name_font_path = linux_arabic_font
                else:
                    fallback_font_path = template_cfg.get("name_font_bold_path", font_bold_path)
                    if fallback_font_path and os.path.isfile(fallback_font_path):
                        print(f"[Arabic] Linux fallback font not found, using template font: {fallback_font_path}")
                        name_font_path = fallback_font_path
        name_font_path = ensure_existing_path(name_font_path, font_bold_path)
        name_text_color = tuple(template_cfg.get("name_text_color", template_cfg.get("text_color", [255, 255, 255])))
        name_shadow_color = tuple(template_cfg.get("name_shadow_color", template_cfg.get("shadow_color", [0, 0, 0, 140])))
        name_shadow_offset = tuple(template_cfg.get("name_shadow_offset", template_cfg.get("shadow_offset", [2, 3])))
        name_max_font_size = int(template_cfg.get("name_max_font_size", 84))
        name_min_font_size = int(template_cfg.get("name_min_font_size", 40))
        name_max_lines = int(template_cfg.get("name_max_lines", 2))

        if name_render_engine == "gdi":
            rendered_name = render_text_block_gdi(
                text=text,
                font_path=name_font_path,
                box=name_box,
                text_color=name_text_color,
                shadow_color=name_shadow_color,
                shadow_offset=name_shadow_offset,
                max_font_size=name_max_font_size,
                min_font_size=name_min_font_size,
                reshape_enabled=name_reshape_text,
            )
            if rendered_name is not None:
                canvas.alpha_composite(rendered_name, (name_box[0], name_box[1]))
            else:
                draw_centered_text_block(
                    draw=draw,
                    text=text,
                    font_path=name_font_path,
                    box=name_box,
                    text_color=name_text_color,
                    shadow_color=name_shadow_color,
                    shadow_offset=name_shadow_offset,
                    max_font_size=name_max_font_size,
                    min_font_size=name_min_font_size,
                    max_lines=name_max_lines,
                    reshape_enabled=name_reshape_text,
                    prefer_raqm=True,
                )
        else:
            draw_centered_text_block(
                draw=draw,
                text=text,
                font_path=name_font_path,
                box=name_box,
                text_color=name_text_color,
                shadow_color=name_shadow_color,
                shadow_offset=name_shadow_offset,
                max_font_size=name_max_font_size,
                min_font_size=name_min_font_size,
                max_lines=name_max_lines,
                reshape_enabled=name_reshape_text,
                prefer_raqm=True,
            )

    if not bool(template_cfg.get("render_text", True)):
        return canvas.convert("RGB")

    text_box = tuple(template_cfg["text_box"])
    l, t, r, b = text_box
    pad_x = int(template_cfg.get("text_padding_x", 40))
    pad_y = int(template_cfg.get("text_padding_y", 20))
    l, t, r, b = l + pad_x, t + pad_y, r - pad_x, b - pad_y
    box_w, box_h = r - l, b - t

    text = clean_text_safe(text)
    if not text:
        text = " "

    words_count = len(text.split())

    max_font_size = int(template_cfg.get("max_font_size", 120))
    min_font_size = int(template_cfg.get("min_font_size", 70))
    text_color = tuple(template_cfg.get("text_color", [255, 255, 255]))
    base_text_align = str(template_cfg.get("text_align", "right")).lower()
    shadow_color = tuple(template_cfg.get("shadow_color", [0, 0, 0, 140]))
    shadow_offset = tuple(template_cfg.get("shadow_offset", [2, 3]))

    max_lines = int(template_cfg.get("max_lines", 4 if box_h > 220 else 3))
    short_words_threshold = int(template_cfg.get("short_words_threshold", 6))
    long_words_threshold = int(template_cfg.get("long_words_threshold", 12))
    short_words_font_scale = float(template_cfg.get("short_words_font_scale", 1.95))
    long_words_font_scale = float(template_cfg.get("long_words_font_scale", 0.90))
    short_style_mode = words_count <= short_words_threshold
    short_text_align = str(template_cfg.get("short_text_align", "center")).lower()
    text_align = short_text_align if short_style_mode else base_text_align

    if words_count <= short_words_threshold:
        dynamic_scale = short_words_font_scale
    elif words_count >= long_words_threshold:
        dynamic_scale = long_words_font_scale
    else:
        ratio = (words_count - short_words_threshold) / max(1, long_words_threshold - short_words_threshold)
        dynamic_scale = short_words_font_scale + (long_words_font_scale - short_words_font_scale) * ratio

    max_font_size = max(24, int(max_font_size * dynamic_scale))
    min_font_size = max(18, int(min_font_size * dynamic_scale))
    if min_font_size > max_font_size:
        min_font_size = max_font_size

    if short_style_mode:
        max_lines = min(max_lines, 2)

    font_size = max_font_size
    final_lines = None
    final_heights = None
    final_spacing = None

    while font_size >= min_font_size:
        font = ImageFont.truetype(font_bold_path, font_size)

        if short_style_mode:
            lines = split_short_text_balanced(draw, text, font, box_w)
        else:
            lines = break_lines_ar_balanced(draw, text, font, box_w, max_lines=max_lines)

        widths, heights = [], []
        for ln in lines:
            wpx, hpx = text_bbox(draw, ln, font)
            widths.append(wpx)
            heights.append(hpx)

        spacing_factor = float(template_cfg.get("line_spacing_factor", 0.24 if len(lines) >= 4 else 0.30))
        if short_style_mode:
            spacing_factor = float(template_cfg.get("short_line_spacing_factor", 0.18))

        spacing = int(font_size * spacing_factor)
        total_h = sum(heights) + spacing * (len(lines) - 1)

        if (max(widths) if widths else 0) <= box_w and total_h <= box_h:
            final_lines = lines
            final_heights = heights
            final_spacing = spacing
            break

        font_size -= 2

    if final_lines is None:
        font_size = min_font_size
        font = ImageFont.truetype(font_bold_path, font_size)
        if short_style_mode:
            final_lines = split_short_text_balanced(draw, text, font, box_w)
        else:
            final_lines = break_lines_ar_balanced(draw, text, font, box_w, max_lines=max_lines)

        final_heights = [text_bbox(draw, ln, font)[1] for ln in final_lines]
        spacing_factor = float(template_cfg.get("line_spacing_factor", 0.24))
        if short_style_mode:
            spacing_factor = float(template_cfg.get("short_line_spacing_factor", 0.18))
        final_spacing = int(font_size * spacing_factor)

    font = ImageFont.truetype(font_bold_path, font_size)

    top_start_offset = int(template_cfg.get("top_start_offset", -35))
    stretch_long_text = bool(template_cfg.get("stretch_long_text", True))
    stretch_short_text = bool(template_cfg.get("stretch_short_text", True))

    short_centered_layout = bool(template_cfg.get("short_centered_layout", True))
    short_center_offset = int(template_cfg.get("short_center_offset", -70))

    if short_style_mode and short_centered_layout:
        if len(final_lines) > 1:
            short_fill_ratio = float(template_cfg.get("short_fill_ratio", 0.86))
            target_total_h = int(box_h * short_fill_ratio)
            desired_spacing = target_total_h - sum(final_heights)
            final_spacing = max(final_spacing, desired_spacing)
        total_h = sum(final_heights) + final_spacing * (len(final_lines) - 1)
        y = t + max(0, (box_h - total_h) // 2) + short_center_offset
    else:
        y = t + top_start_offset

    if stretch_short_text and len(final_lines) > 1 and short_style_mode:
        available_spacing = box_h - sum(final_heights)
        if available_spacing > 0:
            final_spacing = max(final_spacing, available_spacing // (len(final_lines) - 1))

    if stretch_long_text and len(final_lines) > 1 and words_count >= long_words_threshold:
        available_spacing = box_h - sum(final_heights)
        if available_spacing > 0:
            final_spacing = max(final_spacing, available_spacing // (len(final_lines) - 1))

    for i, ln in enumerate(final_lines):
        rendered_text, draw_kwargs = get_text_render_parts(ln)
        wpx = draw.textlength(rendered_text, font=font, **draw_kwargs)

        if text_align == "right":
            x = r - wpx
        elif text_align == "left":
            x = l
        else:
            x = l + (box_w - wpx) / 2

        sx, sy = shadow_offset
        draw.text((x + sx, y + sy), rendered_text, font=font, fill=shadow_color, **draw_kwargs)
        draw.text((x + 1, y + 1), rendered_text, font=font, fill=(0, 0, 0, 90), **draw_kwargs)
        draw.text((x, y), rendered_text, font=font, fill=text_color, **draw_kwargs)

        y += final_heights[i] + final_spacing

    return canvas.convert("RGB")


# ===================== Telegram handlers =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    templates = get_templates(context)

    if not templates:
        await update.message.reply_text(
            "ما في قوالب حالياً.\nتأكد من: templates/<name>/config.json + template image"
        )
        return

    await update.message.reply_text(
        "أهلاً 👋\nاختار القالب:",
        reply_markup=templates_keyboard(templates),
    )

async def templates_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    templates = get_templates(context)
    if not templates:
        await update.message.reply_text("ما في قوالب محمّلة حالياً.")
        return
    await update.message.reply_text("اختار قالب:", reply_markup=templates_keyboard(templates))


async def choose_template_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    templates = get_templates(context)
    template_id = q.data.split(":", 1)[1]

    if template_id not in templates:
        await q.edit_message_text("القالب غير موجود. جرّب /start.")
        return

    context.user_data["template_id"] = template_id
    template_cfg = templates[template_id]
    name = template_cfg.get("name", template_id)

    if bool(template_cfg.get("requires_name", False)):
        await q.edit_message_text(f"تم اختيار القالب: {name}\nابعت الصورة أولاً، وبعدها سأطلب منك الاسم.")
    elif bool(template_cfg.get("requires_text", True)):
        await q.edit_message_text(f"تم اختيار القالب: {name}\nالآن ابعث صورة الخبر.")
    else:
        await q.edit_message_text(f"تم اختيار القالب: {name}\nابعت الصورة فقط.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "template_id" not in context.user_data:
        await update.message.reply_text("اختر قالب أولاً من /start أو /templates.")
        return

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)

    bio = BytesIO()
    await file.download_to_memory(out=bio)
    bio.seek(0)

    img = Image.open(bio)
    templates = get_templates(context)
    template_cfg = get_template_cfg(templates, context.user_data.get("template_id"))

    if bool(template_cfg.get("requires_name", False)):
        context.user_data["news_img"] = img
        await update.message.reply_text("ابعت الاسم الذي تريد إضافته فوق العبارة.")
        return

    if not bool(template_cfg.get("requires_text", True)):
        try:
            await send_rendered_post(update, context, template_cfg, img)
        except Exception as e:
            await update.message.reply_text(f"صار خطأ أثناء التصميم: {e}")
            context.user_data.clear()
        return

    context.user_data["news_img"] = img
    await update.message.reply_text("تمام. الآن ابعث نص الخبر.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "news_img" not in context.user_data:
        await update.message.reply_text("ابعت الصورة أولاً.")
        return

    if "template_id" not in context.user_data:
        await update.message.reply_text("اختر قالب أولاً من /start أو /templates.")
        return

    templates = get_templates(context)
    template_cfg = get_template_cfg(templates, context.user_data.get("template_id"))

    if bool(template_cfg.get("requires_name", False)):
        text = clean_text_safe(update.message.text)
        img = context.user_data["news_img"]
        try:
            await send_rendered_post(update, context, template_cfg, img, text)
        except Exception as e:
            await update.message.reply_text(f"صار خطأ أثناء التصميم: {e}")
            context.user_data.clear()
        return

    if not bool(template_cfg.get("requires_text", True)):
        await update.message.reply_text("هذا القالب لا يحتاج نصاً. ابعت صورة فقط.")
        return

    text = clean_text_safe(update.message.text)
    img = context.user_data["news_img"]

    try:
        await send_rendered_post(update, context, template_cfg, img, text)
    except Exception as e:
        await update.message.reply_text(f"صار خطأ أثناء التصميم: {e}")
        context.user_data.clear()


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set in .env")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(30)
        .pool_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("templates", templates_cmd))
    app.add_handler(CallbackQueryHandler(choose_template_cb, pattern=r"^tpl:"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("BASE_DIR:", BASE_DIR)
    print("TEMPLATES_DIR:", TEMPLATES_DIR)
    print("Bot is running...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
