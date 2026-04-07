import asyncio
import atexit
import os
import json
import re
import subprocess
import tempfile
import hashlib
import shutil
import base64
import urllib.request
import urllib.error
import traceback
import time
from collections import deque
from io import BytesIO
from typing import Optional, List, Set

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat, features

import arabic_reshaper
from bidi.algorithm import get_display

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Conflict, InvalidToken, NetworkError, TimedOut
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
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
DATA_DIR = os.path.join(BASE_DIR, "data")
ADMIN_STATE_PATH = os.path.join(DATA_DIR, "admin_state.json")
PILLOW_HAS_RAQM = bool(features.check("raqm"))
ADMIN_PASSWORD = "1234"
TEMPLATE_CACHE_KEY = "TEMPLATES"
TEMPLATE_CACHE_SIGNATURE_KEY = "TEMPLATES_SIGNATURE"
DEFAULT_TEMPLATE_ID = "classic"

load_dotenv(os.path.join(BASE_DIR, ".env"))
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_TEMPLATE_MODEL = os.getenv("OPENAI_TEMPLATE_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
TELEGRAM_PROXY_URL = os.getenv("TELEGRAM_PROXY_URL", "").strip()
TELEGRAM_BASE_URL = os.getenv("TELEGRAM_BASE_URL", "").strip()
TELEGRAM_BASE_FILE_URL = os.getenv("TELEGRAM_BASE_FILE_URL", "").strip()
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "").strip()
FFPROBE_PATH = os.getenv("FFPROBE_PATH", "").strip()
STARTUP_RETRY_SECONDS = max(5, int(os.getenv("STARTUP_RETRY_SECONDS", "20") or "20"))
BOT_ERROR_LOG_PATH = os.path.join(BASE_DIR, "bot.err.log")
BOT_LOCK_PATH = os.path.join(BASE_DIR, "bot.lock")
BOT_LOCK_FD: Optional[int] = None


class BotAlreadyRunningError(RuntimeError):
    pass


def safe_console_print(message: str):
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode("ascii", errors="replace").decode("ascii"))


def write_startup_error_log(message: str, exc: Exception):
    try:
        with open(BOT_ERROR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write("=" * 80 + "\n")
            f.write(message + "\n")
            f.write(traceback.format_exc())
            f.write("\n")
    except Exception:
        pass


def build_startup_error_message(exc: Exception) -> str:
    if isinstance(exc, InvalidToken):
        return "Startup failed: BOT_TOKEN is invalid. Check BOT_TOKEN in .env"
    if isinstance(exc, Conflict):
        return (
            "Startup failed: another Telegram polling client is already using this BOT_TOKEN. "
            "Stop the other bot instance or rotate BOT_TOKEN in .env."
        )
    if isinstance(exc, TimedOut):
        return (
            "Startup failed: connection to Telegram API timed out. "
            "Check server network access or set TELEGRAM_PROXY_URL / TELEGRAM_BASE_URL in .env"
        )
    if isinstance(exc, NetworkError):
        return (
            "Startup failed: unable to reach Telegram API. "
            "The server likely cannot reach api.telegram.org or requires a proxy. "
            "Set TELEGRAM_PROXY_URL or TELEGRAM_BASE_URL in .env if needed"
        )
    return f"Startup failed: {exc}"


def release_bot_lock():
    global BOT_LOCK_FD
    if BOT_LOCK_FD is None:
        return
    try:
        os.close(BOT_LOCK_FD)
    except OSError:
        pass
    BOT_LOCK_FD = None
    try:
        if os.path.exists(BOT_LOCK_PATH):
            os.remove(BOT_LOCK_PATH)
    except OSError:
        pass


def _read_bot_lock_pid() -> Optional[int]:
    try:
        with open(BOT_LOCK_PATH, "r", encoding="ascii", errors="ignore") as f:
            raw = (f.read() or "").strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION,
                False,
                pid,
            )
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def clear_stale_bot_lock() -> bool:
    if not os.path.exists(BOT_LOCK_PATH):
        return False
    lock_pid = _read_bot_lock_pid()
    if lock_pid and _pid_exists(lock_pid):
        return False
    try:
        os.remove(BOT_LOCK_PATH)
        return True
    except OSError:
        return False


def acquire_bot_lock():
    global BOT_LOCK_FD
    clear_stale_bot_lock()
    try:
        BOT_LOCK_FD = os.open(BOT_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        lock_pid = _read_bot_lock_pid()
        pid_note = f" (PID {lock_pid})" if lock_pid else ""
        raise BotAlreadyRunningError(
            "Another local bot process is already holding bot.lock"
            f"{pid_note}. "
            "Stop it or delete bot.lock if it is stale."
        ) from exc
    os.write(BOT_LOCK_FD, str(os.getpid()).encode("ascii", errors="ignore"))
    atexit.register(release_bot_lock)


# ===================== Admin / Employee State =====================
def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def default_admin_state() -> dict:
    return {
        "admin_password": ADMIN_PASSWORD,
        "max_employees": 0,
        "employee_ids": [],
        "enabled_templates": [],
    }


def load_admin_state() -> dict:
    ensure_data_dir()
    if not os.path.isfile(ADMIN_STATE_PATH):
        state = default_admin_state()
        save_admin_state(state)
        return state

    try:
        with open(ADMIN_STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = default_admin_state()

    base = default_admin_state()
    base.update(state if isinstance(state, dict) else {})
    if not isinstance(base.get("employee_ids"), list):
        base["employee_ids"] = []
    if not isinstance(base.get("enabled_templates"), list):
        base["enabled_templates"] = []
    base["max_employees"] = max(0, int(base.get("max_employees", 0) or 0))
    return base


def save_admin_state(state: dict):
    ensure_data_dir()
    with open(ADMIN_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def enable_template_for_employees(template_id: str) -> dict:
    state = load_admin_state()
    enabled_ids = set(state.get("enabled_templates", []))
    enabled_ids.add(template_id)
    state["enabled_templates"] = sorted(enabled_ids)
    save_admin_state(state)
    return state


def main_role_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("مدير", callback_data="role:admin")],
            [InlineKeyboardButton("موظف", callback_data="role:employee")],
        ]
    )


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("إضافة قالب جديد", callback_data="admin:add_template")],
            [InlineKeyboardButton("إدارة القوالب", callback_data="admin:templates")],
            [InlineKeyboardButton("تحديد عدد الموظفين", callback_data="admin:max_employees")],
            [InlineKeyboardButton("عرض الإعدادات", callback_data="admin:status")],
            [InlineKeyboardButton("رجوع للبداية", callback_data="nav:start")],
        ]
    )

def make_template_callback_id(template_id: str) -> str:
    return hashlib.sha1(template_id.encode("utf-8")).hexdigest()[:16]


def find_template_id_by_callback_token(templates: dict, token: str) -> Optional[str]:
    for template_id in templates.keys():
        if make_template_callback_id(template_id) == token:
            return template_id
    return None


def sort_templates_with_default_first(templates: dict) -> dict:
    if not templates:
        return templates
    ordered_ids = sorted(
        templates.keys(),
        key=lambda tid: (0 if tid == DEFAULT_TEMPLATE_ID else 1, str(templates[tid].get("name", tid))),
    )
    return {tid: templates[tid] for tid in ordered_ids}


def template_toggle_keyboard(templates: dict, enabled_ids: Set[str]) -> InlineKeyboardMarkup:
    rows = []
    for tid, cfg in templates.items():
        prefix = "✅" if tid in enabled_ids else "⬜"
        display_name = cfg.get("name", tid)
        rows.append(
            [
                InlineKeyboardButton(
                    f"{prefix} {display_name} [{tid}]",
                    callback_data=f"admin_tpl:{make_template_callback_id(tid)}",
                ),
                InlineKeyboardButton(
                    "حذف",
                    callback_data=f"admin_tpl_del:{make_template_callback_id(tid)}",
                ),
            ]
        )
    rows.append([InlineKeyboardButton("رجوع", callback_data="admin:menu")])
    return InlineKeyboardMarkup(rows)


def get_enabled_templates(templates: dict, state: dict, role: Optional[str]) -> dict:
    if role == "admin":
        return templates

    enabled_ids = set(state.get("enabled_templates", []))
    if not enabled_ids:
        return {}
    return {tid: cfg for tid, cfg in templates.items() if tid in enabled_ids}


def find_template_storage_target(template_cfg: dict) -> tuple[Optional[str], str]:
    template_path = str(template_cfg.get("template_path", "") or "")
    if not template_path:
        return None, "missing template_path"

    abs_template_path = resolve_path(template_path)
    try:
        abs_template_path = os.path.abspath(abs_template_path)
    except Exception:
        return None, "invalid template_path"

    templates_root = os.path.abspath(TEMPLATES_DIR)
    if not abs_template_path.startswith(templates_root):
        return None, "template outside templates directory"

    parent_dir = os.path.dirname(abs_template_path)
    if os.path.isfile(os.path.join(parent_dir, "config.json")):
        return parent_dir, "folder"

    cfg_file_candidate = os.path.splitext(abs_template_path)[0] + ".template.json"
    if os.path.isfile(cfg_file_candidate):
        return cfg_file_candidate, "file"

    basename = os.path.splitext(os.path.basename(abs_template_path))[0]
    loose_cfg_candidate = os.path.join(templates_root, f"{basename}.template.json")
    if os.path.isfile(loose_cfg_candidate):
        return loose_cfg_candidate, "file"

    return parent_dir, "folder"


def delete_template_from_disk(template_cfg: dict) -> Optional[str]:
    target_path, target_kind = find_template_storage_target(template_cfg)
    if not target_path:
        return "تعذر تحديد مكان القالب على القرص."

    try:
        if target_kind == "folder":
            if not os.path.isdir(target_path):
                return f"مجلد القالب غير موجود: {target_path}"
            shutil.rmtree(target_path)
        else:
            if not os.path.isfile(target_path):
                return f"ملف القالب غير موجود: {target_path}"
            os.remove(target_path)
    except Exception as e:
        return f"فشل حذف القالب: {e}"

    return None


def employee_count_text(state: dict) -> str:
    current = len(state.get("employee_ids", []))
    limit = int(state.get("max_employees", 0) or 0)
    if limit <= 0:
        return str(current)
    return f"{current} / {limit}"


def admin_status_text(state: dict, templates: dict) -> str:
    enabled_ids = set(state.get("enabled_templates", []))
    enabled_names = [
        str(cfg.get("name", tid))
        for tid, cfg in templates.items()
        if tid in enabled_ids
    ]
    enabled_line = "، ".join(enabled_names) if enabled_names else "لا يوجد"
    return (
        "لوحة المدير\n"
        f"عدد الموظفين: {employee_count_text(state)}\n"
        f"القوالب المفعلة للموظفين: {enabled_line}"
    )


async def show_start_menu(target_message, context: ContextTypes.DEFAULT_TYPE, text: Optional[str] = None):
    templates = get_templates(context)
    if not templates:
        prompt = text or "أهلاً بك.\nلا يوجد قوالب حالياً، لكن يمكنك الدخول كمدير لإضافة قالب جديد."
        await target_message.reply_text(prompt, reply_markup=main_role_keyboard())
        if fixed_stat_word:
            await update.message.reply_text("Ø£Ø±Ø³Ù„ Ø§Ù„Ø¢Ù† Ø§Ù„Ø¬Ù…Ù„Ø© Ø§Ù„ØªÙŠ Ø³ØªØ¸Ù‡Ø± Ø¨Ø§Ù„Ù„ÙˆÙ† Ø§Ù„Ø£Ø¨ÙŠØ¶ ØªØ­Øª.")
        else:
            await update.message.reply_text("Ø£Ø±Ø³Ù„ Ø§Ù„Ø¢Ù† Ø§Ù„ÙƒÙ„Ù…Ø© Ø§Ù„ØªÙŠ Ø³ØªØ¸Ù‡Ø± Ø¯Ø§Ø®Ù„ Ø§Ù„ØµÙ†Ø¯ÙˆÙ‚ Ø§Ù„Ø£Ø²Ø±Ù‚.")
        return

    prompt = text or "أهلاً بك.\nاختر طريقة الدخول:"
    await target_message.reply_text(prompt, reply_markup=main_role_keyboard())


async def send_templates_menu(target_message, context: ContextTypes.DEFAULT_TYPE):
    templates = get_templates(context, force_reload=True)
    state = load_admin_state()
    role = context.user_data.get("role")
    available_templates = get_enabled_templates(templates, state, role)

    if not available_templates:
        if role == "employee":
            await target_message.reply_text("لا يوجد قوالب مفعلة للموظفين حالياً.")
        else:
            await target_message.reply_text("ما في قوالب محمّلة حالياً.")
        return

    await target_message.reply_text(
        "اختر قالب:",
        reply_markup=templates_keyboard(available_templates),
    )


def preserve_session(context: ContextTypes.DEFAULT_TYPE) -> dict:
    keep_keys = {
        "role",
        "awaiting_admin_password",
        "awaiting_max_employees",
        "awaiting_new_template_name",
        "awaiting_new_template_image",
        "pending_template_name",
        "awaiting_stat_number",
        "awaiting_stat_word",
        "awaiting_stat_body",
        "pending_stat_number",
        "pending_stat_word",
        "awaiting_montage_video",
        "awaiting_montage_text",
        "montage_video_path",
        "montage_video_name",
    }
    return {k: v for k, v in context.user_data.items() if k in keep_keys}


def reset_design_state(context: ContextTypes.DEFAULT_TYPE):
    session = preserve_session(context)
    context.user_data.clear()
    context.user_data.update(session)


def clear_obsolete_auth_state(context: ContextTypes.DEFAULT_TYPE):
    stale_keys = []
    for key in list(context.user_data.keys()):
        lowered = str(key).lower()
        if lowered == "awaiting_admin_password":
            continue
        if any(token in lowered for token in ("password", "passcode", "login", "auth")):
            stale_keys.append(key)
    for key in stale_keys:
        context.user_data.pop(key, None)


def clear_stat_prompt_state(context: ContextTypes.DEFAULT_TYPE):
    for key in (
        "awaiting_stat_number",
        "awaiting_stat_word",
        "awaiting_stat_body",
        "pending_stat_number",
        "pending_stat_word",
    ):
        context.user_data.pop(key, None)


def clear_montage_state(context: ContextTypes.DEFAULT_TYPE):
    for key in (
        "awaiting_montage_video",
        "awaiting_montage_text",
        "montage_video_path",
        "montage_video_name",
    ):
        value = context.user_data.pop(key, None)
        if key == "montage_video_path" and value and os.path.isfile(value):
            try:
                os.remove(value)
            except Exception:
                pass


def is_image_document(document) -> bool:
    if not document:
        return False
    mime_type = str(getattr(document, "mime_type", "") or "").lower()
    file_name = str(getattr(document, "file_name", "") or "").lower()
    if mime_type.startswith("image/"):
        return True
    return file_name.endswith((".png", ".jpg", ".jpeg", ".webp"))


def is_video_document(document) -> bool:
    if not document:
        return False
    mime_type = str(getattr(document, "mime_type", "") or "").lower()
    file_name = str(getattr(document, "file_name", "") or "").lower()
    if mime_type.startswith("video/"):
        return True
    return file_name.endswith((".mp4", ".mov", ".m4v", ".webm", ".mkv"))


def make_template_folder_name(template_name: str) -> str:
    folder = re.sub(r'[<>:"/\\|?*\x00-\x1F]+', " ", str(template_name or ""))
    folder = re.sub(r"\s+", " ", folder).strip().strip(".")
    folder = folder.replace(" ", "_")
    if not folder:
        folder = f"template_{next(tempfile._get_candidate_names())}"
    base_folder = folder
    while os.path.exists(os.path.join(TEMPLATES_DIR, folder)):
        folder = f"{base_folder}_{next(tempfile._get_candidate_names())}"
    return folder


def get_preferred_project_font() -> str:
    candidates = [
        os.path.join(BASE_DIR, "HEADLINERBOLD.otf"),
        os.path.join(BASE_DIR, "HEADLINERMEDIUM.otf"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return candidates[0]


def attach_template_font(folder_path: str) -> str:
    source_font_path = get_preferred_project_font()
    font_name = os.path.basename(source_font_path)
    target_font_path = os.path.join(folder_path, font_name)

    if os.path.isfile(source_font_path) and not os.path.isfile(target_font_path):
        try:
            shutil.copy2(source_font_path, target_font_path)
        except Exception as e:
            print(f"[Templates] unable to attach font into '{folder_path}': {e}")

    return f"templates/{os.path.basename(folder_path)}/{font_name}".replace("\\", "/")


def build_default_template_config(template_name: str, folder_name: str, width: int, height: int) -> dict:
    text_left = max(40, int(width * 0.06))
    text_right = min(width - 40, int(width * 0.94))
    text_top = max(0, int(height * 0.68))
    text_bottom = min(height - 20, int(height * 0.94))
    min_font_size = max(34, int(height * 0.03))
    max_font_size = max(min_font_size + 10, int(height * 0.06))

    return {
        "name": template_name,
        "enabled": True,
        "requires_name": False,
        "requires_text": True,
        "render_text": True,
        "select_prompt": "أرسل الصورة التي تريد استخدامها في التصميم.",
        "template_path": f"templates/{folder_name}/template.png",
        "font_bold_path": f"templates/{folder_name}/{os.path.basename(get_preferred_project_font())}",
        "image_mode": "full",
        "image_area_bottom": int(height * 0.64),
        "text_box": [text_left, text_top, text_right, text_bottom],
        "template_cutouts": [],
        "max_font_size": max_font_size,
        "min_font_size": min_font_size,
        "text_color": [255, 255, 255],
        "shadow_color": [0, 0, 0, 160],
        "shadow_offset": [2, 3],
        "top_bias": 0.30,
        "text_align": "center",
        "short_text_align": "center",
        "text_padding_x": max(30, int(width * 0.05)),
        "text_padding_y": max(20, int(height * 0.02)),
        "line_spacing_factor": 0.22,
        "max_lines": 4,
        "short_centered_layout": True,
        "short_center_offset": 0,
        "short_fill_ratio": 0.65,
        "text_layout_engine": "smart_boxes",
        "text_box_separator": "|",
        "text_boxes": build_default_smart_text_boxes(width, height, max_font_size, min_font_size),
    }


def build_default_text_box(width: int, height: int) -> List[int]:
    text_left = max(40, int(width * 0.06))
    text_right = min(width - 40, int(width * 0.94))
    text_top = max(0, int(height * 0.68))
    text_bottom = min(height - 20, int(height * 0.94))
    return [text_left, text_top, text_right, text_bottom]


def build_default_smart_text_boxes(width: int, height: int, max_font_size: int, min_font_size: int) -> list[dict]:
    text_left = max(40, int(width * 0.06))
    text_right = min(width - 40, int(width * 0.94))
    text_top = max(0, int(height * 0.68))
    text_bottom = min(height - 20, int(height * 0.94))
    text_height = max(40, text_bottom - text_top)
    title_bottom = text_top + int(text_height * 0.66)
    subtitle_top = title_bottom + max(10, int(height * 0.01))
    return [
        {
            "id": "title",
            "enabled": True,
            "source": "headline_or_full",
            "box": [text_left, text_top, text_right, title_bottom],
            "padding_x": max(30, int(width * 0.04)),
            "padding_y": max(12, int(height * 0.01)),
            "text_align": "center",
            "vertical_align": "center",
            "max_font_size": max_font_size,
            "min_font_size": min_font_size,
            "max_lines": 4,
            "line_spacing_factor": 0.10,
            "line_height_factor": 0.92,
            "prefer_balanced_lines": True,
            "prefer_single_line": False,
            "text_color": [255, 255, 255],
            "shadow_color": [0, 0, 0, 160],
            "shadow_offset": [2, 3],
        },
        {
            "id": "subtitle",
            "enabled": True,
            "source": "subtitle_or_empty",
            "box": [text_left, subtitle_top, text_right, text_bottom],
            "padding_x": max(30, int(width * 0.04)),
            "padding_y": max(8, int(height * 0.008)),
            "text_align": "center",
            "vertical_align": "center",
            "max_font_size": max(min_font_size, int(max_font_size * 0.52)),
            "min_font_size": max(22, int(min_font_size * 0.60)),
            "max_lines": 2,
            "line_spacing_factor": 0.06,
            "line_height_factor": 0.92,
            "prefer_balanced_lines": True,
            "prefer_single_line": True,
            "text_color": [255, 255, 255],
            "shadow_color": [0, 0, 0, 140],
            "shadow_offset": [1, 2],
        },
    ]


def extract_openai_output_text(response_data: dict) -> str:
    output_text = response_data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    output_items = response_data.get("output", [])
    if not isinstance(output_items, list):
        return ""

    parts = []
    for item in output_items:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue
            text_value = content.get("text")
            if isinstance(text_value, str) and text_value.strip():
                parts.append(text_value.strip())
    return "\n".join(parts).strip()


def normalize_ai_text_boxes(candidate_boxes, width: int, height: int, max_font_size: int, min_font_size: int) -> list[dict]:
    fallback_boxes = build_default_smart_text_boxes(width, height, max_font_size, min_font_size)
    if not isinstance(candidate_boxes, list):
        return fallback_boxes

    normalized_boxes = []
    for idx, raw_box_cfg in enumerate(candidate_boxes):
        if not isinstance(raw_box_cfg, dict):
            continue

        raw_box = raw_box_cfg.get("box")
        if not isinstance(raw_box, (list, tuple)) or len(raw_box) != 4:
            continue

        try:
            l, t, r, b = [int(float(v)) for v in raw_box]
        except Exception:
            continue

        l = max(0, min(width - 2, l))
        t = max(0, min(height - 2, t))
        r = max(l + 2, min(width, r))
        b = max(t + 2, min(height, b))
        if (r - l) < 80 or (b - t) < 36:
            continue

        source = str(raw_box_cfg.get("source", "full_text") or "full_text").strip().lower()
        if source not in {"full_text", "headline_or_full", "subtitle_or_empty", "remaining_segments", "segment"}:
            source = "full_text"

        normalized_boxes.append(
            {
                "id": str(raw_box_cfg.get("id", f"box_{idx + 1}")).strip() or f"box_{idx + 1}",
                "enabled": bool(raw_box_cfg.get("enabled", True)),
                "source": source,
                "segment_index": int(raw_box_cfg.get("segment_index", 0) or 0),
                "box": [l, t, r, b],
                "padding_x": max(0, int(raw_box_cfg.get("padding_x", 24) or 0)),
                "padding_y": max(0, int(raw_box_cfg.get("padding_y", 8) or 0)),
                "text_align": str(raw_box_cfg.get("text_align", "center") or "center").lower(),
                "vertical_align": str(raw_box_cfg.get("vertical_align", "center") or "center").lower(),
                "max_font_size": max(12, min(int(raw_box_cfg.get("max_font_size", max_font_size) or max_font_size), max_font_size * 2)),
                "min_font_size": max(10, min(int(raw_box_cfg.get("min_font_size", min_font_size) or min_font_size), max_font_size * 2)),
                "max_lines": max(1, min(8, int(raw_box_cfg.get("max_lines", 4) or 4))),
                "line_spacing_factor": float(raw_box_cfg.get("line_spacing_factor", 0.10) or 0.10),
                "line_height_factor": float(raw_box_cfg.get("line_height_factor", 0.92) or 0.92),
                "prefer_balanced_lines": bool(raw_box_cfg.get("prefer_balanced_lines", True)),
                "prefer_single_line": bool(raw_box_cfg.get("prefer_single_line", False)),
                "text_color": raw_box_cfg.get("text_color", [255, 255, 255]),
                "shadow_color": raw_box_cfg.get("shadow_color", [0, 0, 0, 160]),
                "shadow_offset": raw_box_cfg.get("shadow_offset", [2, 3]),
            }
        )

    if not normalized_boxes:
        return fallback_boxes

    for box_cfg in normalized_boxes:
        if box_cfg["min_font_size"] > box_cfg["max_font_size"]:
            box_cfg["min_font_size"] = box_cfg["max_font_size"]

    return normalized_boxes


def normalize_ai_image_layout(candidate_layout: dict, width: int, height: int) -> dict:
    image_area_bottom = int(height * 0.64)
    fallback_layout = {
        "image_mode": "full",
        "image_area_bottom": image_area_bottom,
        "template_cutouts": [],
    }
    if not isinstance(candidate_layout, dict):
        return fallback_layout

    mode = str(candidate_layout.get("mode", "full") or "full").strip().lower()
    if mode == "box":
        raw_box = candidate_layout.get("box")
        if not isinstance(raw_box, (list, tuple)) or len(raw_box) != 4:
            return fallback_layout
        try:
            l, t, r, b = [int(float(v)) for v in raw_box]
        except Exception:
            return fallback_layout

        l = max(0, min(width - 2, l))
        t = max(0, min(height - 2, t))
        r = max(l + 2, min(width, r))
        b = max(t + 2, min(height, b))
        if (r - l) < 80 or (b - t) < 80:
            return fallback_layout

        raw_shape = str(candidate_layout.get("mask_shape", "rectangle") or "rectangle").lower()
        if raw_shape == "circle":
            mask_shape = "ellipse"
        else:
            mask_shape = "rectangle"

        return {
            "image_mode": "box",
            "image_box": [l, t, r, b],
            "image_mask_box": [l, t, r, b],
            "image_mask_shape": mask_shape,
            "template_cutouts": [{"shape": "rectangle", "box": [l, t, r, b]}],
        }

    bottom = int(candidate_layout.get("image_area_bottom", image_area_bottom) or image_area_bottom)
    bottom = max(int(height * 0.20), min(height, bottom))
    return {
        "image_mode": "full",
        "image_area_bottom": bottom,
        "template_cutouts": [{"shape": "rectangle", "box": [0, 0, width, bottom]}],
    }


def infer_image_layout_from_text_boxes(text_boxes: list[dict], width: int, height: int) -> dict:
    if not isinstance(text_boxes, list) or not text_boxes:
        return normalize_ai_image_layout({}, width, height)

    top_edges = []
    for box_cfg in text_boxes:
        if not isinstance(box_cfg, dict):
            continue
        raw_box = box_cfg.get("box")
        if not isinstance(raw_box, (list, tuple)) or len(raw_box) != 4:
            continue
        try:
            top_edges.append(int(raw_box[1]))
        except Exception:
            continue

    if not top_edges:
        return normalize_ai_image_layout({}, width, height)

    first_text_top = min(top_edges)
    inferred_bottom = max(int(height * 0.20), min(int(height * 0.82), first_text_top - max(12, int(height * 0.015))))
    return {
        "image_mode": "full",
        "image_area_bottom": inferred_bottom,
        "template_cutouts": [{"shape": "rectangle", "box": [0, 0, width, inferred_bottom]}],
    }


def ensure_image_window_cutouts(cfg: dict, width: int, height: int) -> dict:
    if not isinstance(cfg, dict):
        return cfg
    image_mode = str(cfg.get("image_mode", "full") or "full").strip().lower()
    cutouts = cfg.get("template_cutouts")
    has_valid_cutout = isinstance(cutouts, list) and any(
        isinstance(item, dict)
        and isinstance(item.get("box"), (list, tuple))
        and len(item.get("box")) == 4
        for item in cutouts
    )
    if has_valid_cutout:
        return cfg

    if image_mode == "box":
        raw_box = cfg.get("image_mask_box") or cfg.get("image_box")
        if not isinstance(raw_box, (list, tuple)) or len(raw_box) != 4:
            fallback_bottom = int(cfg.get("image_area_bottom", int(height * 0.58)) or int(height * 0.58))
            raw_box = [0, 0, width, max(2, min(height, fallback_bottom))]
            cfg["image_box"] = list(raw_box)
            cfg["image_mask_box"] = list(raw_box)
        l, t, r, b = [int(v) for v in raw_box]
        l = max(0, min(width - 2, l))
        t = max(0, min(height - 2, t))
        r = max(l + 2, min(width, r))
        b = max(t + 2, min(height, b))
        cfg["template_cutouts"] = [{"shape": "rectangle", "box": [l, t, r, b]}]
        return cfg

    bottom = int(cfg.get("image_area_bottom", int(height * 0.58)) or int(height * 0.58))
    bottom = max(2, min(height, bottom))
    cfg["template_cutouts"] = [{"shape": "rectangle", "box": [0, 0, width, bottom]}]
    return cfg


def _box_intersection_area(box_a: list[int], box_b: list[int]) -> int:
    l1, t1, r1, b1 = box_a
    l2, t2, r2, b2 = box_b
    iw = max(0, min(r1, r2) - max(l1, l2))
    ih = max(0, min(b1, b2) - max(t1, t2))
    return iw * ih


def _score_text_box_readability(template_image: Image.Image, box: list[int]) -> float:
    l, t, r, b = box
    if r <= l or b <= t:
        return -1e9
    crop = template_image.crop((l, t, r, b)).convert("RGB")
    gray = crop.convert("L")
    stat = ImageStat.Stat(gray)
    mean_l = float(stat.mean[0]) if stat.mean else 127.0
    std_l = float(stat.stddev[0]) if stat.stddev else 0.0
    edges = gray.filter(ImageFilter.FIND_EDGES)
    edge_mean = float(ImageStat.Stat(edges).mean[0])
    darkness_contrast = abs(mean_l - 28.0)
    lightness_contrast = abs(mean_l - 235.0)
    contrast_score = max(darkness_contrast, lightness_contrast)
    emptiness_score = max(0.0, 100.0 - (std_l + edge_mean))
    return (contrast_score * 1.35) + emptiness_score


def detect_image_layout_from_placeholder(
    template_image: Image.Image,
    width: int,
    height: int,
    text_boxes: list[dict],
) -> Optional[dict]:
    rgb = template_image.convert("RGB")
    px = rgb.load()
    if px is None:
        return None

    # Keep image region above text whenever possible.
    text_tops = []
    for box_cfg in text_boxes or []:
        raw_box = box_cfg.get("box") if isinstance(box_cfg, dict) else None
        if isinstance(raw_box, (list, tuple)) and len(raw_box) == 4:
            try:
                text_tops.append(int(raw_box[1]))
            except Exception:
                pass
    max_scan_bottom = min(height - 1, max(int(height * 0.22), (min(text_tops) - max(8, int(height * 0.01))) if text_tops else int(height * 0.72)))
    if max_scan_bottom < int(height * 0.18):
        max_scan_bottom = int(height * 0.72)

    row_step = 2 if height >= 900 else 1
    col_step = 3 if width >= 900 else 2
    white_row_ratio = [0.0] * height
    for y in range(0, max_scan_bottom, row_step):
        total = 0
        white = 0
        for x in range(0, width, col_step):
            r, g, b = px[x, y]
            total += 1
            # Bright + low chroma => likely empty placeholder region.
            if r >= 228 and g >= 228 and b >= 228 and (max(r, g, b) - min(r, g, b)) <= 22:
                white += 1
        ratio = (white / total) if total else 0.0
        for yy in range(y, min(y + row_step, height)):
            white_row_ratio[yy] = ratio

    # Smooth ratios to avoid noisy edges.
    smoothed = [0.0] * height
    for y in range(height):
        acc = 0.0
        cnt = 0
        for k in range(max(0, y - 3), min(height, y + 4)):
            acc += white_row_ratio[k]
            cnt += 1
        smoothed[y] = (acc / cnt) if cnt else 0.0

    threshold = 0.68
    runs = []
    run_start = None
    for y in range(0, max_scan_bottom):
        if smoothed[y] >= threshold:
            if run_start is None:
                run_start = y
        else:
            if run_start is not None:
                runs.append((run_start, y - 1))
                run_start = None
    if run_start is not None:
        runs.append((run_start, max_scan_bottom - 1))

    if not runs:
        return None

    min_run_h = max(40, int(height * 0.12))
    ranked = []
    for y1, y2 in runs:
        run_h = y2 - y1 + 1
        if run_h < min_run_h:
            continue
        avg_ratio = sum(smoothed[y1:y2 + 1]) / max(1, run_h)
        score = (run_h * avg_ratio) - (y1 * 0.08)  # prefer larger and a bit higher.
        ranked.append((score, y1, y2))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    _, y1, y2 = ranked[0]

    # Detect horizontal span of the placeholder within that band.
    band_h = max(1, y2 - y1 + 1)
    col_white_ratio = [0.0] * width
    for x in range(0, width, col_step):
        total = 0
        white = 0
        for y in range(y1, y2 + 1, row_step):
            r, g, b = px[x, y]
            total += 1
            if r >= 228 and g >= 228 and b >= 228 and (max(r, g, b) - min(r, g, b)) <= 22:
                white += 1
        ratio = (white / total) if total else 0.0
        for xx in range(x, min(x + col_step, width)):
            col_white_ratio[xx] = ratio

    col_threshold = 0.60
    spans = []
    span_start = None
    for x in range(width):
        if col_white_ratio[x] >= col_threshold:
            if span_start is None:
                span_start = x
        else:
            if span_start is not None:
                spans.append((span_start, x - 1))
                span_start = None
    if span_start is not None:
        spans.append((span_start, width - 1))

    if not spans:
        return {
            "image_mode": "full",
            "image_area_bottom": int(y2),
            "template_cutouts": [{"shape": "rectangle", "box": [0, 0, width, int(y2)]}],
        }

    spans = sorted(spans, key=lambda s: (s[1] - s[0] + 1), reverse=True)
    x1, x2 = spans[0]
    span_w = x2 - x1 + 1
    full_like = span_w >= int(width * 0.84)
    if full_like:
        return {
            "image_mode": "full",
            "image_area_bottom": int(y2),
            "template_cutouts": [{"shape": "rectangle", "box": [0, 0, width, int(y2)]}],
        }

    l = max(0, x1)
    r = min(width, x2 + 1)
    t = max(0, y1)
    b = min(height, y2 + 1)
    if (r - l) < 100 or (b - t) < 80:
        return None
    return {
        "image_mode": "box",
        "image_box": [l, t, r, b],
        "image_mask_box": [l, t, r, b],
        "image_mask_shape": "rectangle",
        "template_cutouts": [{"shape": "rectangle", "box": [l, t, r, b]}],
    }


def detect_dynamic_layout_heuristic(
    template_image: Image.Image,
    width: int,
    height: int,
    max_font_size: int,
    min_font_size: int,
) -> dict:
    safe_margin_x = max(28, int(width * 0.04))
    safe_margin_y = max(22, int(height * 0.03))
    base_w = max(140, int(width * 0.78))
    base_h = max(72, int(height * 0.24))
    cx = width // 2

    candidate_tops = [
        int(height * 0.50),
        int(height * 0.56),
        int(height * 0.62),
        int(height * 0.68),
    ]
    candidate_width_scales = [0.72, 0.78, 0.84]
    candidate_height_scales = [0.20, 0.24, 0.28]
    candidates: list[list[int]] = []
    for top in candidate_tops:
        for ws in candidate_width_scales:
            for hs in candidate_height_scales:
                bw = max(120, int(width * ws))
                bh = max(56, int(height * hs))
                l = max(safe_margin_x, cx - (bw // 2))
                r = min(width - safe_margin_x, l + bw)
                l = max(safe_margin_x, r - bw)
                t = max(safe_margin_y, top)
                b = min(height - safe_margin_y, t + bh)
                if (r - l) >= 120 and (b - t) >= 56:
                    candidates.append([l, t, r, b])

    if not candidates:
        fallback = build_default_text_box(width, height)
        candidates = [fallback]

    best_box = candidates[0]
    best_score = -1e9
    for box in candidates:
        score = _score_text_box_readability(template_image, box)
        # Prefer lower area slightly to protect logos usually near top.
        score += (box[1] / max(1, height)) * 14.0
        if score > best_score:
            best_score = score
            best_box = box

    text_top = best_box[1]
    suggested_image_bottom = max(int(height * 0.22), min(int(height * 0.84), text_top - max(8, int(height * 0.012))))
    image_layout = {
        "image_mode": "full",
        "image_area_bottom": suggested_image_bottom,
        "template_cutouts": [{"shape": "rectangle", "box": [0, 0, width, suggested_image_bottom]}],
    }

    l, t, r, b = best_box
    text_h = b - t
    title_bottom = t + int(text_h * 0.64)
    subtitle_top = title_bottom + max(6, int(height * 0.008))
    text_boxes = [
        {
            "id": "title",
            "enabled": True,
            "source": "headline_or_full",
            "box": [l, t, r, max(title_bottom, t + 34)],
            "padding_x": max(20, int(width * 0.028)),
            "padding_y": max(8, int(height * 0.008)),
            "text_align": "center",
            "vertical_align": "center",
            "max_font_size": max_font_size,
            "min_font_size": min_font_size,
            "max_lines": 3,
            "line_spacing_factor": 0.08,
            "line_height_factor": 0.92,
            "prefer_balanced_lines": True,
            "prefer_single_line": False,
            "text_color": [255, 255, 255],
            "shadow_color": [0, 0, 0, 170],
            "shadow_offset": [2, 3],
        },
        {
            "id": "subtitle",
            "enabled": True,
            "source": "subtitle_or_empty",
            "box": [l, subtitle_top, r, b],
            "padding_x": max(18, int(width * 0.024)),
            "padding_y": max(6, int(height * 0.006)),
            "text_align": "center",
            "vertical_align": "center",
            "max_font_size": max(min_font_size, int(max_font_size * 0.58)),
            "min_font_size": max(18, int(min_font_size * 0.64)),
            "max_lines": 2,
            "line_spacing_factor": 0.06,
            "line_height_factor": 0.92,
            "prefer_balanced_lines": True,
            "prefer_single_line": True,
            "text_color": [255, 255, 255],
            "shadow_color": [0, 0, 0, 150],
            "shadow_offset": [1, 2],
        },
    ]
    text_boxes = normalize_ai_text_boxes(text_boxes, width, height, max_font_size=max_font_size, min_font_size=min_font_size)
    detected_image_layout = detect_image_layout_from_placeholder(
        template_image=template_image,
        width=width,
        height=height,
        text_boxes=text_boxes,
    )
    if isinstance(detected_image_layout, dict):
        image_layout = detected_image_layout
    return {"text_boxes": text_boxes, "image_layout": image_layout}


def build_dynamic_elements_json(text_boxes: list[dict], image_layout: dict, width: int, height: int) -> dict:
    elements: list[dict] = []
    for box_cfg in text_boxes or []:
        raw_box = box_cfg.get("box")
        if not isinstance(raw_box, (list, tuple)) or len(raw_box) != 4:
            continue
        l, t, r, b = [int(v) for v in raw_box]
        if r <= l or b <= t:
            continue
        elements.append(
            {
                "type": "text",
                "id": str(box_cfg.get("id", "text")),
                "position": {"x": l, "y": t, "width": (r - l), "height": (b - t)},
                "alignment": str(box_cfg.get("text_align", "center")),
                "verticalAlignment": str(box_cfg.get("vertical_align", "center")),
                "autoResize": True,
                "padding": {
                    "x": int(box_cfg.get("padding_x", 0) or 0),
                    "y": int(box_cfg.get("padding_y", 0) or 0),
                },
                "responsive": {
                    "short": {"fontScale": 1.14, "preferSingleLine": True},
                    "medium": {"fontScale": 1.0, "preferBalancedLines": True},
                    "long": {"fontScale": 0.82, "maxLines": int(box_cfg.get("max_lines", 3) or 3)},
                    "overflow": "shrink_to_fit",
                },
            }
        )

    if isinstance(image_layout, dict):
        mode = str(image_layout.get("image_mode", "full") or "full").lower()
        if mode == "box":
            raw_box = image_layout.get("image_box") or image_layout.get("image_mask_box")
            if isinstance(raw_box, (list, tuple)) and len(raw_box) == 4:
                l, t, r, b = [int(v) for v in raw_box]
                if r > l and b > t:
                    elements.append(
                        {
                            "type": "image",
                            "id": "image_main",
                            "position": {"x": l, "y": t, "width": (r - l), "height": (b - t)},
                            "fit": "cover",
                            "crop": "smart",
                        }
                    )
        else:
            bottom = int(image_layout.get("image_area_bottom", int(height * 0.64)))
            bottom = max(2, min(height, bottom))
            elements.append(
                {
                    "type": "image",
                    "id": "image_main",
                    "position": {"x": 0, "y": 0, "width": int(width), "height": int(bottom)},
                    "fit": "cover",
                    "crop": "smart",
                }
            )

    return {"elements": elements}


def analyze_template_layout_with_openai(
    source_bytes: bytes,
    width: int,
    height: int,
    max_font_size: int,
    min_font_size: int,
) -> tuple[Optional[dict], Optional[str]]:
    if not OPENAI_API_KEY:
        return None, "OPENAI_API_KEY missing"

    image_b64 = base64.b64encode(source_bytes).decode("ascii")
    prompt = (
        "Analyze this social media news template image and propose a complete render layout for a Telegram news bot. "
        "Return only JSON with keys: summary, text_box_separator, image_layout, text_boxes. "
        "Assume the editor will send text as 'headline | subtitle'. "
        "Decide where the user photo should go and where the headline/subtitle should go. "
        "Use one title box and optionally one subtitle box only if the design clearly supports it. "
        f"Image size is {width}x{height}. "
        "For image_layout use either mode=full with image_area_bottom, or mode=box with box=[x1,y1,x2,y2]. "
        "Each text_boxes item must include: id, source, box, padding_x, padding_y, text_align, vertical_align, "
        "max_font_size, min_font_size, max_lines, line_spacing_factor, line_height_factor, prefer_balanced_lines, prefer_single_line. "
        "Keep boxes inside visible safe areas, avoid logos, avoid decorative overlays, and preserve brand layout."
    )

    payload = {
        "model": OPENAI_TEMPLATE_MODEL,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": f"data:image/png;base64,{image_b64}"},
                ],
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "template_layout",
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "summary": {"type": "string"},
                        "text_box_separator": {"type": "string"},
                        "image_layout": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "mode": {"type": "string", "enum": ["full", "box"]},
                                "image_area_bottom": {"type": "integer"},
                                "box": {
                                    "type": "array",
                                    "items": {"type": "integer"},
                                    "minItems": 4,
                                    "maxItems": 4,
                                },
                                "mask_shape": {"type": "string", "enum": ["rectangle", "circle"]},
                            },
                            "required": ["mode"],
                        },
                        "text_boxes": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "id": {"type": "string"},
                                    "source": {
                                        "type": "string",
                                        "enum": [
                                            "full_text",
                                            "headline_or_full",
                                            "subtitle_or_empty",
                                            "remaining_segments",
                                            "segment",
                                        ],
                                    },
                                    "segment_index": {"type": "integer"},
                                    "box": {
                                        "type": "array",
                                        "items": {"type": "integer"},
                                        "minItems": 4,
                                        "maxItems": 4,
                                    },
                                    "padding_x": {"type": "integer"},
                                    "padding_y": {"type": "integer"},
                                    "text_align": {"type": "string", "enum": ["left", "center", "right"]},
                                    "vertical_align": {"type": "string", "enum": ["top", "center", "bottom"]},
                                    "max_font_size": {"type": "integer"},
                                    "min_font_size": {"type": "integer"},
                                    "max_lines": {"type": "integer"},
                                    "line_spacing_factor": {"type": "number"},
                                    "line_height_factor": {"type": "number"},
                                    "prefer_balanced_lines": {"type": "boolean"},
                                    "prefer_single_line": {"type": "boolean"},
                                },
                                "required": [
                                    "id",
                                    "source",
                                    "box",
                                    "padding_x",
                                    "padding_y",
                                    "text_align",
                                    "vertical_align",
                                    "max_font_size",
                                    "min_font_size",
                                    "max_lines",
                                    "line_spacing_factor",
                                    "line_height_factor",
                                    "prefer_balanced_lines",
                                    "prefer_single_line",
                                ],
                            },
                        },
                    },
                    "required": ["summary", "text_box_separator", "image_layout", "text_boxes"],
                },
            }
        },
    }

    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            body = str(e)
        return None, f"OpenAI HTTP {e.code}: {body[:400]}"
    except Exception as e:
        return None, f"OpenAI request failed: {e}"

    output_text = extract_openai_output_text(response_data)
    if not output_text:
        return None, "OpenAI returned no output text"

    try:
        ai_json = json.loads(output_text)
    except Exception as e:
        return None, f"OpenAI returned invalid JSON: {e}"

    separator = str(ai_json.get("text_box_separator", "|") or "|")
    normalized_boxes = normalize_ai_text_boxes(
        ai_json.get("text_boxes"),
        width=width,
        height=height,
        max_font_size=max_font_size,
        min_font_size=min_font_size,
    )
    normalized_image_layout = normalize_ai_image_layout(ai_json.get("image_layout"), width=width, height=height)
    if normalized_image_layout.get("image_mode") == "full":
        inferred_layout = infer_image_layout_from_text_boxes(normalized_boxes, width=width, height=height)
        ai_bottom = int(normalized_image_layout.get("image_area_bottom", int(height * 0.64)))
        inferred_bottom = int(inferred_layout.get("image_area_bottom", ai_bottom))
        normalized_image_layout["image_area_bottom"] = max(ai_bottom, inferred_bottom)
        normalized_image_layout["template_cutouts"] = [{"shape": "rectangle", "box": [0, 0, width, normalized_image_layout["image_area_bottom"]]}]
    return {
        "summary": str(ai_json.get("summary", "") or "").strip(),
        "separator": separator,
        "model": OPENAI_TEMPLATE_MODEL,
        "text_boxes": normalized_boxes,
        "image_layout": normalized_image_layout,
    }, None


def create_template_from_image(template_name: str, source_bytes: bytes) -> str:
    ensure_data_dir()
    os.makedirs(TEMPLATES_DIR, exist_ok=True)

    with Image.open(BytesIO(source_bytes)) as img:
        template_image = img.convert("RGBA")
        width, height = template_image.size

    folder_name = make_template_folder_name(template_name)
    folder_path = os.path.join(TEMPLATES_DIR, folder_name)
    os.makedirs(folder_path, exist_ok=True)

    template_path = os.path.join(folder_path, "template.png")
    template_image.save(template_path, format="PNG")
    attach_template_font(folder_path)

    config = build_default_template_config(template_name, folder_name, width, height)
    heuristic_layout = detect_dynamic_layout_heuristic(
        template_image=template_image,
        width=width,
        height=height,
        max_font_size=int(config.get("max_font_size", 120)),
        min_font_size=int(config.get("min_font_size", 28)),
    )
    config["text_boxes"] = heuristic_layout.get("text_boxes", config.get("text_boxes", []))
    heuristic_image_layout = heuristic_layout.get("image_layout", {})
    config["image_mode"] = "full"
    config["image_area_bottom"] = int(heuristic_image_layout.get("image_area_bottom", config.get("image_area_bottom", int(height * 0.64))))
    config["template_cutouts"] = heuristic_image_layout.get("template_cutouts", [])
    ai_layout, ai_error = analyze_template_layout_with_openai(
        source_bytes=source_bytes,
        width=width,
        height=height,
        max_font_size=int(config.get("max_font_size", 120)),
        min_font_size=int(config.get("min_font_size", 28)),
    )
    if ai_layout:
        config["text_boxes"] = ai_layout.get("text_boxes", config.get("text_boxes", []))
        config["text_box_separator"] = str(ai_layout.get("separator", "|") or "|")
        image_layout = ai_layout.get("image_layout", {})
        if image_layout.get("image_mode") == "box":
            config["image_mode"] = "box"
            config["image_box"] = image_layout.get("image_box", config.get("image_box"))
            config["image_mask_box"] = image_layout.get("image_mask_box", config.get("image_mask_box", config.get("image_box")))
            config["image_mask_shape"] = image_layout.get("image_mask_shape", "rectangle")
            config["template_cutouts"] = image_layout.get("template_cutouts", [])
        else:
            config["image_mode"] = "full"
            config["image_area_bottom"] = int(image_layout.get("image_area_bottom", config.get("image_area_bottom", int(height * 0.64))))
            config["template_cutouts"] = image_layout.get("template_cutouts", [])
            config.pop("image_box", None)
            config.pop("image_mask_box", None)
            config.pop("image_mask_shape", None)
        config["layout_ai"] = {
            "enabled": True,
            "model": str(ai_layout.get("model", OPENAI_TEMPLATE_MODEL) or OPENAI_TEMPLATE_MODEL),
            "summary": str(ai_layout.get("summary", "") or ""),
        }
    else:
        config["layout_ai"] = {
            "enabled": False,
            "model": OPENAI_TEMPLATE_MODEL,
            "error": str(ai_error or "fallback_to_default"),
        }

    # Final pass: detect real image placeholder from the template itself.
    # This fixes cases where text is detected correctly but image region is not.
    placeholder_image_layout = detect_image_layout_from_placeholder(
        template_image=template_image,
        width=width,
        height=height,
        text_boxes=config.get("text_boxes", []),
    )
    if isinstance(placeholder_image_layout, dict):
        if placeholder_image_layout.get("image_mode") == "box":
            config["image_mode"] = "box"
            config["image_box"] = placeholder_image_layout.get("image_box", config.get("image_box"))
            config["image_mask_box"] = placeholder_image_layout.get("image_mask_box", config.get("image_mask_box", config.get("image_box")))
            config["image_mask_shape"] = placeholder_image_layout.get("image_mask_shape", "rectangle")
            config["template_cutouts"] = placeholder_image_layout.get("template_cutouts", config.get("template_cutouts", []))
            config.pop("image_area_bottom", None)
        else:
            config["image_mode"] = "full"
            config["image_area_bottom"] = int(
                placeholder_image_layout.get("image_area_bottom", config.get("image_area_bottom", int(height * 0.64)))
            )
            config["template_cutouts"] = placeholder_image_layout.get("template_cutouts", config.get("template_cutouts", []))
            config.pop("image_box", None)
            config.pop("image_mask_box", None)
            config.pop("image_mask_shape", None)
    config["dynamic_elements"] = build_dynamic_elements_json(
        text_boxes=config.get("text_boxes", []),
        image_layout={
            "image_mode": config.get("image_mode", "full"),
            "image_area_bottom": config.get("image_area_bottom"),
            "image_box": config.get("image_box"),
            "image_mask_box": config.get("image_mask_box"),
        },
        width=width,
        height=height,
    )
    config = ensure_image_window_cutouts(config, width=width, height=height)
    config_path = os.path.join(folder_path, "config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    return folder_name


def verify_template_saved(folder_name: str) -> Optional[str]:
    folder_path = os.path.join(TEMPLATES_DIR, folder_name)
    config_path = os.path.join(folder_path, "config.json")
    template_path = os.path.join(folder_path, "template.png")

    if not os.path.isdir(folder_path):
        return f"لم يتم إنشاء المجلد: templates/{folder_name}"
    if not os.path.isfile(config_path):
        return f"لم يتم إنشاء config.json داخل templates/{folder_name}"
    if not os.path.isfile(template_path):
        return f"لم يتم حفظ صورة القالب داخل templates/{folder_name}"
    return None


def create_default_template_config_file(folder_name: str, folder_path: str, image_path: str) -> Optional[dict]:
    try:
        with Image.open(image_path) as img:
            width, height = img.size
    except Exception as e:
        print(f"[Templates] unable to read image for '{folder_name}': {e}")
        return None

    attach_template_font(folder_path)
    config = build_default_template_config(folder_name, folder_name, width, height)
    config["name"] = folder_name.replace("_", " ").strip() or folder_name
    config["template_path"] = os.path.relpath(image_path, BASE_DIR).replace("\\", "/")
    config_path = os.path.join(folder_path, "config.json")
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Templates] unable to create default config for '{folder_name}': {e}")
        return None

    print(f"[Templates] created default config for '{folder_name}'")
    return config


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


def text_bbox_with_offsets(
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
    return draw.textbbox((0, 0), rendered_text, font=font, **draw_kwargs)


def resolve_text_x_in_box(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    box_left: int,
    box_right: int,
    align: str = "center",
    reshape_enabled: bool = True,
    prefer_raqm: bool = True,
) -> float:
    bbox = text_bbox_with_offsets(
        draw,
        text,
        font,
        reshape_enabled=reshape_enabled,
        prefer_raqm=prefer_raqm,
    )
    text_left, text_right = bbox[0], bbox[2]
    text_width = text_right - text_left
    box_width = box_right - box_left

    if align == "right":
        return box_right - text_right
    if align == "left":
        return box_left - text_left
    return box_left + ((box_width - text_width) / 2) - text_left


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


def build_linear_gradient(size, start_color, end_color, direction: str = "vertical") -> Image.Image:
    width, height = size
    width = max(1, int(width))
    height = max(1, int(height))
    direction = str(direction or "vertical").lower()

    start = tuple(int(v) for v in (list(start_color) + [0, 0, 0, 0])[:4])
    end = tuple(int(v) for v in (list(end_color) + [0, 0, 0, 0])[:4])

    gradient = Image.new("RGBA", (width, height), start)
    px = gradient.load()

    if direction == "horizontal":
        denom = max(1, width - 1)
        for x in range(width):
            ratio = x / denom
            color = tuple(int(start[i] + (end[i] - start[i]) * ratio) for i in range(4))
            for y in range(height):
                px[x, y] = color
        return gradient

    denom = max(1, height - 1)
    for y in range(height):
        ratio = y / denom
        color = tuple(int(start[i] + (end[i] - start[i]) * ratio) for i in range(4))
        for x in range(width):
            px[x, y] = color
    return gradient


def apply_gradient_overlay(canvas: Image.Image, overlay_cfg: dict, default_box):
    if not isinstance(overlay_cfg, dict) or not bool(overlay_cfg.get("enabled", False)):
        return

    overlay_box = overlay_cfg.get("box", default_box)
    if not isinstance(overlay_box, (list, tuple)) or len(overlay_box) != 4:
        overlay_box = default_box

    l, t, r, b = [int(v) for v in overlay_box]
    if r <= l or b <= t:
        return

    gradient = build_linear_gradient(
        (r - l, b - t),
        overlay_cfg.get("start_color", [0, 0, 0, 0]),
        overlay_cfg.get("end_color", [0, 0, 0, 120]),
        direction=str(overlay_cfg.get("direction", "vertical")),
    )
    canvas.alpha_composite(gradient, (l, t))


def apply_image_gradient_overlay(canvas: Image.Image, template_cfg: dict, image_box):
    apply_gradient_overlay(canvas, template_cfg.get("image_gradient_overlay"), image_box)


# ===================== Remove white background =====================
def remove_near_white_background(
    img: Image.Image,
    threshold: int = 235,
    softness: int = 25,
    mode: str = "all",
) -> Image.Image:
    """
    Removes near-white background and makes it transparent تدريجياً.
    """
    img = img.convert("RGBA")
    pixels = img.load()
    w, h = img.size

    if str(mode).lower() == "edge_connected":
        visited = set()
        queue = deque()

        def is_near_white(px) -> bool:
            r, g, b, _ = px
            mx = max(r, g, b)
            mn = min(r, g, b)
            return (
                (r >= threshold and g >= threshold and b >= threshold)
                or (mx >= threshold and (mx - mn) <= 18)
            )

        for x in range(w):
            queue.append((x, 0))
            queue.append((x, h - 1))
        for y in range(h):
            queue.append((0, y))
            queue.append((w - 1, y))

        while queue:
            x, y = queue.popleft()
            if (x, y) in visited or x < 0 or y < 0 or x >= w or y >= h:
                continue
            visited.add((x, y))
            if not is_near_white(pixels[x, y]):
                continue
            queue.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))

        for x, y in visited:
            if not is_near_white(pixels[x, y]):
                continue
            r, g, b, a = pixels[x, y]
            whiteness = (r + g + b) / 3
            alpha = int(max(0, min(255, (255 - whiteness) * (255 / max(1, softness)))))
            pixels[x, y] = (r, g, b, min(a, alpha))

        return img

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


def trim_bottom_white_band(
    img: Image.Image,
    threshold: int = 245,
    row_white_ratio: float = 0.90,
    start_scan_ratio: float = 0.35,
    min_run_rows: int = 36,
    window_rows: int = 48,
    window_match_ratio: float = 0.80,
    bottom_padding: int = 8,
) -> Image.Image:
    img = img.convert("RGBA")
    w, h = img.size
    if w <= 1 or h <= 1:
        return img

    pixels = img.load()
    start_y = max(0, min(h - 1, int(h * start_scan_ratio)))
    qualifying_rows = []

    for y in range(start_y, h):
        white_count = 0
        for x in range(w):
            r, g, b, a = pixels[x, y]
            if a == 0 or (r >= threshold and g >= threshold and b >= threshold):
                white_count += 1
        qualifying_rows.append(1 if (white_count / max(1, w)) >= row_white_ratio else 0)

    window_rows = max(1, int(window_rows))
    min_matches = max(1, int(window_rows * window_match_ratio))
    min_run_rows = max(min_run_rows, window_rows)

    for idx in range(0, max(0, len(qualifying_rows) - window_rows + 1)):
        if sum(qualifying_rows[idx:idx + window_rows]) < min_matches:
            continue

        run_end = idx + window_rows
        while run_end < len(qualifying_rows) and qualifying_rows[run_end]:
            run_end += 1

        if (run_end - idx) < min_run_rows:
            continue

        crop_bottom = max(1, min(h, start_y + idx + bottom_padding))
        if crop_bottom >= int(h * 0.35):
            return img.crop((0, 0, w, crop_bottom))

    return img


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


def choose_text_lines(
    draw,
    text: str,
    font,
    max_width: int,
    max_lines: int,
    reshape_enabled: bool = True,
) -> List[str]:
    if max_lines <= 1:
        return [text]

    lines = split_short_text_balanced(
        draw,
        text,
        font,
        max_width,
        reshape_enabled=reshape_enabled,
    )
    if len(lines) <= max_lines:
        return lines

    return break_lines_ar_balanced(
        draw,
        text,
        font,
        max_width,
        max_lines=max_lines,
        reshape_enabled=reshape_enabled,
    )


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


def find_template_image(path: str, cfg: dict) -> Optional[str]:
    configured_path = resolve_path(cfg.get("template_path", ""))
    if configured_path and os.path.isfile(configured_path):
        return configured_path

    for candidate in ("template.png", "template.jpg", "template.jpeg", "template.webp"):
        candidate_path = os.path.join(path, candidate)
        if os.path.isfile(candidate_path):
            return candidate_path

    for file_name in sorted(os.listdir(path)):
        candidate_path = os.path.join(path, file_name)
        if not os.path.isfile(candidate_path):
            continue
        ext = os.path.splitext(file_name)[1].lower()
        if ext in {".png", ".jpg", ".jpeg", ".webp"}:
            return candidate_path

    return configured_path or None


def ensure_existing_path(primary_path: Optional[str], fallback_path: Optional[str] = None) -> Optional[str]:
    if primary_path and os.path.isfile(primary_path):
        return primary_path
    if fallback_path and os.path.isfile(fallback_path):
        return fallback_path
    return primary_path or fallback_path


def find_linux_arabic_font() -> Optional[str]:
    candidates = [
        "/usr/share/fonts/truetype/noto/NotoKufiArabic-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
        "/usr/share/fonts/opentype/noto/NotoKufiArabic-Bold.ttf",
        "/usr/share/fonts/opentype/noto/NotoNaskhArabic-Bold.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansArabic-Bold.ttf",
        "/usr/share/fonts/truetype/fonts-arabeyes/ae_AlArabiya.ttf",
        "/usr/share/fonts/truetype/msttcorefonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def debug_log_arabic_render(stage: str, **data):
    try:
        safe = " ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in data.items())
        safe_log(f"[ArabicDebug] {stage} {safe}")
    except Exception:
        pass


def safe_log(*parts):
    message = " ".join(str(part) for part in parts)
    try:
        print(message)
    except UnicodeEncodeError:
        fallback = message.encode("ascii", "backslashreplace").decode("ascii")
        print(fallback)


async def safe_answer_callback(query, *args, **kwargs) -> bool:
    try:
        await query.answer(*args, **kwargs)
        return True
    except BadRequest as e:
        if "query is too old" in str(e).lower() or "query id is invalid" in str(e).lower():
            return False
        raise


def load_template_entry(folder: str) -> tuple[Optional[dict], Optional[str]]:
    path = os.path.join(TEMPLATES_DIR, folder)
    if not os.path.isdir(path):
        return None, "template folder missing"

    cfg_path = os.path.join(path, "config.json")
    if not os.path.isfile(cfg_path):
        fallback_image_path = find_template_image(path, {})
        if not fallback_image_path or not os.path.isfile(fallback_image_path):
            return None, "missing config.json and template image"

        cfg = create_default_template_config_file(folder, path, fallback_image_path)
        if not cfg:
            return None, "failed to create default config"
    else:
        try:
            with open(cfg_path, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
        except Exception as e:
            return None, f"json error: {e}"

    if not bool(cfg.get("enabled", True)):
        return None, "disabled via config.json"

    if not isinstance(cfg, dict):
        return None, "invalid config format"

    attached_font_rel = attach_template_font(path)
    cfg["id"] = str(cfg.get("id") or folder)
    cfg["template_path"] = find_template_image(path, cfg)
    cfg["font_bold_path"] = resolve_path(cfg.get("font_bold_path", "HEADLINERBOLD.otf"))
    fallback_template_font = resolve_path(attached_font_rel)
    project_font_fallback = get_preferred_project_font()
    cfg["font_bold_path"] = ensure_existing_path(
        cfg["font_bold_path"],
        ensure_existing_path(fallback_template_font, project_font_fallback),
    )

    if cfg.get("name_font_bold_path"):
        cfg["name_font_bold_path"] = resolve_path(cfg["name_font_bold_path"])
        cfg["name_font_bold_path"] = ensure_existing_path(cfg["name_font_bold_path"], cfg["font_bold_path"])
    if cfg.get("name_arabic_font_bold_path"):
        cfg["name_arabic_font_bold_path"] = resolve_path(cfg["name_arabic_font_bold_path"])
        cfg["name_arabic_font_bold_path"] = ensure_existing_path(
            cfg["name_arabic_font_bold_path"],
            cfg.get("name_font_bold_path", cfg["font_bold_path"]),
        )
    if cfg.get("name_no_raqm_font_bold_path"):
        cfg["name_no_raqm_font_bold_path"] = resolve_path(cfg["name_no_raqm_font_bold_path"])
        cfg["name_no_raqm_font_bold_path"] = ensure_existing_path(
            cfg["name_no_raqm_font_bold_path"],
            cfg.get("name_arabic_font_bold_path", cfg.get("name_font_bold_path", cfg["font_bold_path"])),
        )
    if cfg.get("caption_font_bold_path"):
        cfg["caption_font_bold_path"] = resolve_path(cfg["caption_font_bold_path"])
        cfg["caption_font_bold_path"] = ensure_existing_path(cfg["caption_font_bold_path"], cfg["font_bold_path"])

    if not os.path.isfile(cfg["template_path"]):
        return None, f"template not found: {cfg['template_path']}"

    if "text_box" not in cfg:
        if bool(cfg.get("render_text", True)):
            try:
                with Image.open(cfg["template_path"]) as template_img:
                    width, height = template_img.size
                cfg["text_box"] = build_default_text_box(width, height)
                print(f"[Templates] generated default text_box for '{folder}'")
            except Exception as e:
                return None, f"unable to infer text_box: {e}"
        else:
            cfg["text_box"] = [0, 0, 1, 1]

    if not os.path.isfile(cfg["font_bold_path"]):
        return None, f"font not found: {cfg['font_bold_path']}"

    if cfg.get("name_font_bold_path") and not os.path.isfile(cfg["name_font_bold_path"]):
        return None, f"name font not found: {cfg['name_font_bold_path']}"
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
        return None, f"caption font not found: {cfg['caption_font_bold_path']}"

    image_mode = str(cfg.get("image_mode", "full") or "full").strip().lower()
    if image_mode == "cover":
        image_mode = "full"
        cfg["image_mode"] = "full"
    if image_mode == "box" and "image_box" not in cfg:
        try:
            with Image.open(cfg["template_path"]) as template_img:
                width, height = template_img.size
            fallback_bottom = int(cfg.get("image_area_bottom", int(height * 0.58)))
            cfg["image_box"] = [0, 0, width, fallback_bottom]
            cfg["image_mask_box"] = cfg.get("image_mask_box", cfg["image_box"])
            print(f"[Templates] generated fallback image_box for '{folder}'")
        except Exception as e:
            return None, f"image_mode=box requires image_box and fallback failed: {e}"
    try:
        with Image.open(cfg["template_path"]) as template_img:
            width, height = template_img.size
        cfg = ensure_image_window_cutouts(cfg, width=width, height=height)
    except Exception as e:
        return None, f"unable to validate template_cutouts: {e}"

    return cfg, None


def load_loose_template_entry(cfg_path: str) -> tuple[Optional[dict], Optional[str]]:
    try:
        with open(cfg_path, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except Exception as e:
        return None, f"json error: {e}"

    if not isinstance(cfg, dict):
        return None, "invalid config format"
    if not bool(cfg.get("enabled", True)):
        return None, "disabled via config.json"

    cfg["id"] = str(cfg.get("id") or os.path.splitext(os.path.basename(cfg_path))[0])
    cfg["template_path"] = resolve_path(cfg.get("template_path", ""))
    cfg["font_bold_path"] = ensure_existing_path(
        resolve_path(cfg.get("font_bold_path", "")),
        get_preferred_project_font(),
    )

    if cfg.get("caption_font_bold_path"):
        cfg["caption_font_bold_path"] = ensure_existing_path(
            resolve_path(cfg["caption_font_bold_path"]),
            cfg["font_bold_path"],
        )

    if not os.path.isfile(cfg["template_path"]):
        return None, f"template not found: {cfg['template_path']}"
    if not os.path.isfile(cfg["font_bold_path"]):
        return None, f"font not found: {cfg['font_bold_path']}"

    if "text_box" not in cfg:
        if bool(cfg.get("render_text", True)):
            try:
                with Image.open(cfg["template_path"]) as template_img:
                    width, height = template_img.size
                cfg["text_box"] = build_default_text_box(width, height)
            except Exception as e:
                return None, f"unable to infer text_box: {e}"
        else:
            cfg["text_box"] = [0, 0, 1, 1]

    image_mode = str(cfg.get("image_mode", "full") or "full").strip().lower()
    if image_mode == "cover":
        image_mode = "full"
        cfg["image_mode"] = "full"
    if image_mode == "box" and "image_box" not in cfg:
        try:
            with Image.open(cfg["template_path"]) as template_img:
                width, height = template_img.size
            fallback_bottom = int(cfg.get("image_area_bottom", int(height * 0.58)))
            cfg["image_box"] = [0, 0, width, fallback_bottom]
            cfg["image_mask_box"] = cfg.get("image_mask_box", cfg["image_box"])
            print(f"[Templates] generated fallback image_box for '{cfg['id']}'")
        except Exception as e:
            return None, f"image_mode=box requires image_box and fallback failed: {e}"
    try:
        with Image.open(cfg["template_path"]) as template_img:
            width, height = template_img.size
        cfg = ensure_image_window_cutouts(cfg, width=width, height=height)
    except Exception as e:
        return None, f"unable to validate template_cutouts: {e}"

    return cfg, None


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
        cfg, error = load_template_entry(folder)
        if error:
            safe_log(f"[Templates] skipping '{folder}': {error}")
            continue
        template_id = str(cfg.get("id") or folder)
        templates[template_id] = cfg

    for file_name in sorted(os.listdir(TEMPLATES_DIR)):
        if not file_name.endswith(".template.json"):
            continue
        cfg_path = os.path.join(TEMPLATES_DIR, file_name)
        if not os.path.isfile(cfg_path):
            continue
        cfg, error = load_loose_template_entry(cfg_path)
        if error:
            safe_log(f"[Templates] skipping loose config '{file_name}': {error}")
            continue
        template_id = str(cfg.get("id") or os.path.splitext(file_name)[0])
        templates[template_id] = cfg

    safe_log("[Templates] Loaded:", list(templates.keys()))
    return templates


def get_templates_signature() -> tuple:
    if not os.path.isdir(TEMPLATES_DIR):
        return ()

    relevant_suffixes = (
        ".json",
        ".template.json",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".otf",
        ".ttf",
    )
    signature = []

    for root, dirs, files in os.walk(TEMPLATES_DIR):
        dirs.sort()
        rel_root = os.path.relpath(root, TEMPLATES_DIR)
        for name in sorted(files):
            lower_name = name.lower()
            if not lower_name.endswith(relevant_suffixes):
                continue
            full_path = os.path.join(root, name)
            try:
                stat = os.stat(full_path)
            except OSError:
                continue
            signature.append(
                (
                    os.path.join(rel_root, name).replace("\\", "/"),
                    stat.st_mtime_ns,
                    stat.st_size,
                )
            )

    return tuple(signature)


def get_templates(context: ContextTypes.DEFAULT_TYPE, force_reload: bool = False) -> dict:
    current_signature = get_templates_signature()
    cached_signature = context.bot_data.get(TEMPLATE_CACHE_SIGNATURE_KEY)
    cached_templates = context.bot_data.get(TEMPLATE_CACHE_KEY)

    if not force_reload and cached_templates is not None and cached_signature == current_signature:
        return cached_templates

    templates = load_templates()
    context.bot_data[TEMPLATE_CACHE_KEY] = templates
    context.bot_data[TEMPLATE_CACHE_SIGNATURE_KEY] = current_signature
    return templates


def register_new_template(context: ContextTypes.DEFAULT_TYPE, folder_name: str) -> tuple[Optional[dict], Optional[dict], Optional[str]]:
    save_error = verify_template_saved(folder_name)
    if save_error:
        return None, None, save_error

    cfg, load_error = load_template_entry(folder_name)
    if load_error or not cfg:
        return None, None, load_error or "تعذر تحميل القالب الجديد"

    templates = get_templates(context, force_reload=True)

    state = enable_template_for_employees(str(cfg.get("id") or folder_name))
    return templates, state, None


def templates_keyboard(templates: dict) -> InlineKeyboardMarkup:
    buttons = []
    for tid, cfg in sort_templates_with_default_first(templates).items():
        name = cfg.get("name", tid)
        buttons.append(
            [InlineKeyboardButton(f"📌 {name} [{tid}]", callback_data=f"tpl:{make_template_callback_id(tid)}")]
        )
    buttons.append([InlineKeyboardButton("🎬 مونتاج", callback_data="mode:montage")])
    return InlineKeyboardMarkup(buttons)


def get_template_cfg(templates: dict, template_id: Optional[str]) -> dict:
    if template_id and template_id in templates:
        return templates[template_id]
    if DEFAULT_TEMPLATE_ID in templates:
        return templates[DEFAULT_TEMPLATE_ID]
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
    def _render_to_png_bytes():
        out = render_post(img, text, template_cfg)
        out_bio = BytesIO()
        out.save(out_bio, format="PNG")
        out_bio.seek(0)
        return out_bio

    out_bio = await asyncio.to_thread(_render_to_png_bytes)

    await update.message.reply_document(
        document=out_bio,
        filename="post.png",
        read_timeout=60,
        write_timeout=60,
        connect_timeout=30,
        pool_timeout=30,
    )
    reset_design_state(context)


def get_ffmpeg_paths() -> tuple[Optional[str], Optional[str]]:
    ffmpeg_path = FFMPEG_PATH if FFMPEG_PATH and os.path.isfile(FFMPEG_PATH) else shutil.which("ffmpeg")
    ffprobe_path = FFPROBE_PATH if FFPROBE_PATH and os.path.isfile(FFPROBE_PATH) else shutil.which("ffprobe")
    return ffmpeg_path, ffprobe_path


def get_default_montage_logo_path() -> Optional[str]:
    candidates = [
        os.path.join(TEMPLATES_DIR, "الاقتحامات", "logo.png"),
        os.path.join(TEMPLATES_DIR, "breaking", "logo.png"),
        os.path.join(TEMPLATES_DIR, "mutabaa_ikhbariya", "logo.png"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def get_video_dimensions(ffprobe_path: str, video_path: str) -> tuple[int, int]:
    result = subprocess.run(
        [
            ffprobe_path,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            video_path,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    width_str, height_str = result.stdout.strip().split("x", 1)
    return int(width_str), int(height_str)


def wrap_text_to_width(draw, text: str, font, max_width: int) -> List[str]:
    words = [w for w in text.split() if w.strip()]
    if not words:
        return [""]

    lines = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}".strip()
        candidate_width, _ = text_bbox(draw, candidate, font)
        if candidate_width <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def split_text_to_two_lines(draw, text: str, font, max_width: int) -> List[str]:
    words = [w for w in text.split() if w.strip()]
    if not words:
        return [""]
    if len(words) == 1:
        return [words[0]]

    best_lines = [" ".join(words)]
    best_score = None
    for idx in range(1, len(words)):
        first = " ".join(words[:idx]).strip()
        second = " ".join(words[idx:]).strip()
        first_w, _ = text_bbox(draw, first, font)
        second_w, _ = text_bbox(draw, second, font)
        max_line_width = max(first_w, second_w)
        overflow = max(0, max_line_width - max_width)
        balance_penalty = abs(first_w - second_w)
        score = (overflow * 1000) + balance_penalty + max_line_width
        if best_score is None or score < best_score:
            best_score = score
            best_lines = [first, second]
    return best_lines


def create_montage_text_overlays(top_output_path: str, bottom_output_path: str, width: int, height: int, text: str):
    measure_canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(measure_canvas)
    montage_font = os.path.join(BASE_DIR, "HEADLINERBOLD.otf")
    if not os.path.isfile(montage_font):
        montage_font = os.path.join(BASE_DIR, "HEADLINERMEDIUM.otf")
    fallback_font = get_preferred_project_font()
    font_path = ensure_existing_path(montage_font, fallback_font)
    side_margin = max(112, int(width * 0.25))
    inner_pad_x = max(14, int(width * 0.022))
    inner_pad_y = max(6, int(height * 0.010))

    cleaned = clean_text_safe(text)
    words = [word for word in cleaned.split() if word.strip()]
    word_count = len(words)

    if word_count <= 2:
        font_size = max(68, int(width * 0.102))
    elif word_count <= 4:
        font_size = max(61, int(width * 0.093))
    elif word_count <= 6:
        font_size = max(52, int(width * 0.080))
    else:
        font_size = max(44, int(width * 0.068))

    min_font_size = max(26, int(font_size * 0.58))
    lines = [cleaned]
    font = ImageFont.truetype(font_path, font_size)
    fixed_band_height = max(74, int(height * 0.096))
    fixed_band_top = max(22, int(((height - fixed_band_height) / 2) + (height * 0.08)))
    fixed_band_bottom = min(height - 20, fixed_band_top + fixed_band_height)
    split_ratio = 0.50
    band_left = side_margin
    band_right = width - side_margin
    band_top = fixed_band_top
    band_bottom = fixed_band_bottom
    band_height = band_bottom - band_top
    split_y = band_top + int(band_height * split_ratio)

    max_text_width = max(1, (band_right - band_left) - (inner_pad_x * 2))
    while font_size >= min_font_size:
        font = ImageFont.truetype(font_path, font_size)
        lines = split_text_to_two_lines(draw, cleaned, font, max_text_width)
        widths = [text_bbox(draw, line, font)[0] for line in lines]
        heights = [text_bbox(draw, line, font)[1] for line in lines]
        top_box_height = split_y - band_top
        bottom_box_height = band_bottom - split_y
        top_h = heights[0] if heights else 0
        bottom_h = heights[1] if len(heights) > 1 else 0
        width_ok = (max(widths) if widths else 0) <= max_text_width
        height_ok = top_h <= max(1, top_box_height - (inner_pad_y * 2)) and bottom_h <= max(1, bottom_box_height - (inner_pad_y * 2))
        if width_ok and height_ok:
            break
        font_size -= 2

    heights = [text_bbox(draw, line, font)[1] for line in lines]

    top_canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    top_draw = ImageDraw.Draw(top_canvas)
    bottom_canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    bottom_draw = ImageDraw.Draw(bottom_canvas)

    # Keep a soft shadow so the flat banner remains readable over bright footage.
    shadow_blur = max(8, int(height * 0.01))
    top_shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    top_shadow_draw = ImageDraw.Draw(top_shadow)
    top_shadow_draw.rectangle((band_left, band_top + 6, band_right, split_y + 6), fill=(0, 0, 0, 90))
    top_canvas.alpha_composite(top_shadow.filter(ImageFilter.GaussianBlur(radius=shadow_blur)))

    bottom_shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    bottom_shadow_draw = ImageDraw.Draw(bottom_shadow)
    bottom_shadow_draw.rectangle((band_left, split_y + 6, band_right, band_bottom + 6), fill=(0, 0, 0, 90))
    bottom_canvas.alpha_composite(bottom_shadow.filter(ImageFilter.GaussianBlur(radius=shadow_blur)))

    top_draw.rectangle((band_left, band_top, band_right, split_y), fill=(255, 226, 0, 245))
    bottom_draw.rectangle((band_left, split_y, band_right, band_bottom), fill=(255, 255, 255, 245))

    top_inner_top = band_top + inner_pad_y
    top_inner_bottom = split_y - inner_pad_y
    bottom_inner_top = split_y + inner_pad_y
    bottom_inner_bottom = band_bottom - inner_pad_y

    for idx, line in enumerate(lines[:2]):
        rendered_text, draw_kwargs = get_text_render_parts(line, reshape_enabled=True, prefer_raqm=True)
        _, hpx = text_bbox(draw, line, font)
        x = int(resolve_text_x_in_box(draw, line, font, band_left + inner_pad_x, band_right - inner_pad_x, align="center"))
        shadow_offset = max(1, int(font_size * 0.035))
        if idx == 0:
            text_y = top_inner_top + int((max(1, top_inner_bottom - top_inner_top) - hpx) / 2)
        else:
            text_y = bottom_inner_top + int((max(1, bottom_inner_bottom - bottom_inner_top) - hpx) / 2)
        target_draw = top_draw if idx == 0 else bottom_draw
        target_draw.text((x + shadow_offset, text_y + shadow_offset), rendered_text, font=font, fill=(0, 0, 0, 70), **draw_kwargs)
        target_draw.text((x, text_y), rendered_text, font=font, fill=(0, 0, 0, 255), **draw_kwargs)

    top_canvas.save(top_output_path, format="PNG")
    bottom_canvas.save(bottom_output_path, format="PNG")


def create_resized_logo_overlay(output_path: str, width: int):
    logo_path = get_default_montage_logo_path()
    if not logo_path:
        raise RuntimeError("لم يتم العثور على شعار للمونتاج داخل templates.")

    with Image.open(logo_path) as logo_img:
        logo = logo_img.convert("RGBA")
        target_w = max(180, int(width * 0.24))
        scale = target_w / max(1, logo.width)
        target_h = max(1, int(logo.height * scale))
        logo = logo.resize((target_w, target_h), Image.LANCZOS)
        logo.save(output_path, format="PNG")


def render_montage_video(input_video_path: str, text: str) -> str:
    ffmpeg_path, ffprobe_path = get_ffmpeg_paths()
    if not ffmpeg_path or not ffprobe_path:
        raise RuntimeError("ميزة المونتاج تحتاج ffmpeg و ffprobe مثبتين على الجهاز.")

    width, height = get_video_dimensions(ffprobe_path, input_video_path)
    work_dir = tempfile.mkdtemp(prefix="montage_", dir=BASE_DIR)
    logo_overlay_path = os.path.join(work_dir, "logo_overlay.png")
    top_text_overlay_path = os.path.join(work_dir, "text_overlay_top.png")
    bottom_text_overlay_path = os.path.join(work_dir, "text_overlay_bottom.png")
    output_path = os.path.join(work_dir, "montage_output.mp4")

    create_resized_logo_overlay(logo_overlay_path, width)
    create_montage_text_overlays(top_text_overlay_path, bottom_text_overlay_path, width, height, text)

    logo_x = max(24, width - max(180, int(width * 0.24)) - int(width * 0.08))
    logo_y = max(24, int(height * 0.23))
    text_visible_seconds = 4.0
    text_stagger_delay = 0.18
    text_fade_duration = 0.38
    top_slide_duration = 0.24
    bottom_slide_duration = 0.22
    slide_offset = max(14, int(height * 0.018))
    text_fade_out_start = max(0.0, text_visible_seconds - text_fade_duration)
    bottom_visible_seconds = max(0.0, text_visible_seconds - text_stagger_delay)
    bottom_fade_out_start = max(text_stagger_delay, text_visible_seconds - text_fade_duration)
    ffmpeg_cmd = [
        ffmpeg_path,
        "-y",
        "-i",
        input_video_path,
        "-loop",
        "1",
        "-i",
        logo_overlay_path,
        "-loop",
        "1",
        "-i",
        top_text_overlay_path,
        "-loop",
        "1",
        "-i",
        bottom_text_overlay_path,
        "-filter_complex",
        (
            f"[2:v]format=rgba,fade=t=in:st=0:d=0.45:alpha=1,"
            f"fade=t=out:st={text_fade_out_start:.2f}:d={text_fade_duration:.2f}:alpha=1[toptext];"
            f"[3:v]format=rgba,fade=t=in:st={text_stagger_delay:.2f}:d=0.35:alpha=1,"
            f"fade=t=out:st={bottom_fade_out_start:.2f}:d={text_fade_duration:.2f}:alpha=1[bottomtext];"
            f"[0:v][1:v]overlay={logo_x}:{logo_y}[v1];"
            f"[v1][toptext]overlay=0:'if(lt(t,{top_slide_duration:.2f}),-{slide_offset}*(1-t/{top_slide_duration:.2f}),0)':format=auto:enable='between(t,0,{text_visible_seconds:.2f})'[v2];"
            f"[v2][bottomtext]overlay=0:'if(lt(t,{text_stagger_delay + bottom_slide_duration:.2f}),{slide_offset}*(1-(t-{text_stagger_delay:.2f})/{bottom_slide_duration:.2f}),0)':format=auto:enable='between(t,{text_stagger_delay:.2f},{text_stagger_delay + bottom_visible_seconds:.2f})'[v]"
        ),
        "-map",
        "[v]",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "veryfast",
        "-crf",
        "22",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        "-shortest",
        output_path,
    ]
    try:
        subprocess.run(
            ffmpeg_cmd,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        if stderr:
            error_lines = [line.strip() for line in stderr.splitlines() if line.strip()]
            error_text = "\n".join(error_lines[-8:])
            raise RuntimeError(f"فشل ffmpeg أثناء المونتاج:\n{error_text}") from e
        raise RuntimeError("فشل ffmpeg أثناء المونتاج بدون رسالة خطأ واضحة.") from e
    return output_path


async def send_rendered_montage(update: Update, context: ContextTypes.DEFAULT_TYPE, video_path: str, text: str):
    def _render():
        output_path = render_montage_video(video_path, text)
        work_dir = os.path.dirname(output_path)
        try:
            with open(output_path, "rb") as f:
                data = f.read()
            return BytesIO(data)
        finally:
            try:
                shutil.rmtree(work_dir, ignore_errors=True)
            except Exception:
                pass

    out_bio = await asyncio.to_thread(_render)
    out_bio.seek(0)
    await update.message.reply_document(
        document=out_bio,
        filename="montage.mp4",
        read_timeout=120,
        write_timeout=120,
        connect_timeout=30,
        pool_timeout=30,
    )
    clear_montage_state(context)


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
    vertical_offset: int = 0,
    prefer_single_line: bool = False,
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
        if prefer_single_line:
            single_w, single_h = text_bbox(
                draw,
                text,
                font,
                reshape_enabled=reshape_enabled,
                prefer_raqm=prefer_raqm,
            )
            if single_w <= box_w and single_h <= box_h:
                lines = [text]
            else:
                lines = choose_text_lines(
                    draw,
                    text,
                    font,
                    box_w,
                    max_lines=max_lines,
                    reshape_enabled=reshape_enabled,
                )
        else:
            lines = choose_text_lines(
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

    font_size = max(min_font_size, font_size)
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
    y = t + max(0, (box_h - total_h) // 2) + int(vertical_offset)

    for i, ln in enumerate(final_lines):
        rendered_text, draw_kwargs = get_text_render_parts(
            ln,
            reshape_enabled=reshape_enabled,
            prefer_raqm=prefer_raqm,
        )
        x = resolve_text_x_in_box(
            draw,
            ln,
            font,
            l,
            r,
            align="center",
            reshape_enabled=reshape_enabled,
            prefer_raqm=prefer_raqm,
        )
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
    max_lines: int = 2,
    reshape_enabled: bool = True,
    vertical_offset: int = 0,
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
        "max_lines": int(max_lines),
        "vertical_offset": int(vertical_offset),
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
$singleLineFlags = [System.Drawing.StringFormatFlags]::NoWrap
if ([int]$data.max_lines -le 1) {
    $fmt.FormatFlags = $fmt.FormatFlags -bor $singleLineFlags
}
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
$shadowRect = New-Object System.Drawing.RectangleF([single]$data.shadow_offset[0], [single]($data.shadow_offset[1] + $data.vertical_offset), $data.width, $data.height)
$rect = New-Object System.Drawing.RectangleF(0, [single]$data.vertical_offset, $data.width, $data.height)
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
            timeout=20,
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


def split_name_and_subtitle(text: str, template_cfg: dict) -> tuple[str, str]:
    cleaned = clean_text_safe(text)
    if not cleaned:
        return "", ""
    if not bool(template_cfg.get("name_subtitle_enabled", False)):
        return cleaned, ""
    separator = str(template_cfg.get("name_subtitle_separator", "|") or "|")
    if separator and separator in cleaned:
        name_text, subtitle_text = cleaned.split(separator, 1)
        return clean_text_safe(name_text), clean_text_safe(subtitle_text)
    split_markers = template_cfg.get("name_subtitle_split_markers", [])
    if isinstance(split_markers, (list, tuple)):
        marker_match = None
        for marker in split_markers:
            marker_text = clean_text_safe(str(marker))
            if not marker_text:
                continue
            idx = cleaned.find(marker_text)
            if idx <= 0:
                continue
            if marker_match is None or idx < marker_match[0]:
                marker_match = (idx, marker_text)
        if marker_match is not None:
            idx, marker_text = marker_match
            name_text = clean_text_safe(cleaned[:idx])
            subtitle_text = clean_text_safe(cleaned[idx:])
            if name_text and subtitle_text:
                return name_text, subtitle_text
    auto_split_words = int(template_cfg.get("name_subtitle_auto_split_words", 0))
    words = cleaned.split()
    if auto_split_words > 0 and len(words) > auto_split_words:
        name_text = " ".join(words[:auto_split_words])
        subtitle_text = " ".join(words[auto_split_words:])
        return clean_text_safe(name_text), clean_text_safe(subtitle_text)
    return cleaned, ""


def split_text_segments(text: str, separator: str = "|", max_parts: int = 3) -> list[str]:
    raw_text = str(text or "").strip()
    if not raw_text:
        return []

    separator = str(separator or "|")
    if separator:
        raw_parts = raw_text.split(separator, max_parts - 1)
    else:
        raw_parts = [raw_text]

    parts = [clean_text_safe(part) for part in raw_parts]
    return [part for part in parts if part]


def resolve_text_box_content(
    full_text: str,
    box_cfg: dict,
    separator: str = "|",
) -> str:
    cleaned_text = clean_text_safe(full_text)
    if not cleaned_text:
        return ""

    source = str(box_cfg.get("source", "full_text") or "full_text").strip().lower()
    segments = split_text_segments(
        cleaned_text,
        separator=str(box_cfg.get("separator", separator) or separator),
        max_parts=int(box_cfg.get("max_source_parts", 4)),
    )

    if source == "full_text":
        return cleaned_text
    if source == "headline_or_full":
        return segments[0] if segments else cleaned_text
    if source == "subtitle_or_empty":
        if len(segments) >= 2:
            return segments[1]
        return ""
    if source == "remaining_segments":
        return clean_text_safe(" ".join(segments[1:])) if len(segments) >= 2 else ""
    if source == "segment":
        segment_index = int(box_cfg.get("segment_index", 0))
        if 0 <= segment_index < len(segments):
            return segments[segment_index]
        return ""
    return cleaned_text


def draw_smart_text_box(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: str,
    box_cfg: dict,
    default_cfg: dict,
):
    content = clean_text_safe(text)
    if not content:
        return

    raw_box = box_cfg.get("box")
    if not isinstance(raw_box, (list, tuple)) or len(raw_box) != 4:
        return

    l, t, r, b = [int(v) for v in raw_box]
    if r <= l or b <= t:
        return

    bg_box = box_cfg.get("bg_box")
    if bg_box and isinstance(bg_box, (list, tuple)) and len(bg_box) == 4:
        draw_fill_box(
            draw,
            tuple(int(v) for v in bg_box),
            tuple(box_cfg.get("bg_fill", [0, 0, 0, 140])),
            int(box_cfg.get("bg_radius", 0)),
        )

    pad_x = int(box_cfg.get("padding_x", default_cfg.get("text_padding_x", 0)))
    pad_y = int(box_cfg.get("padding_y", default_cfg.get("text_padding_y", 0)))
    l += pad_x
    r -= pad_x
    t += pad_y
    b -= pad_y

    box_w = r - l
    box_h = b - t
    if box_w <= 0 or box_h <= 0:
        return

    max_font_size = int(box_cfg.get("max_font_size", default_cfg.get("max_font_size", 120)))
    min_font_size = int(box_cfg.get("min_font_size", default_cfg.get("min_font_size", 28)))
    max_lines = int(box_cfg.get("max_lines", default_cfg.get("max_lines", 4)))
    text_align = str(box_cfg.get("text_align", default_cfg.get("text_align", "center"))).lower()
    vertical_align = str(box_cfg.get("vertical_align", "center")).lower()
    vertical_offset = int(box_cfg.get("vertical_offset", 0))
    line_spacing_factor = float(box_cfg.get("line_spacing_factor", default_cfg.get("line_spacing_factor", 0.12)))
    line_height_factor = float(box_cfg.get("line_height_factor", default_cfg.get("line_height_factor", 1.0)))
    reshape_enabled = bool(box_cfg.get("reshape_text", True))
    prefer_raqm = bool(box_cfg.get("prefer_raqm", True))
    prefer_balanced_lines = bool(box_cfg.get("prefer_balanced_lines", True))
    prefer_single_line = bool(box_cfg.get("prefer_single_line", False))

    text_color = tuple(box_cfg.get("text_color", default_cfg.get("text_color", [255, 255, 255])))
    shadow_color = tuple(box_cfg.get("shadow_color", default_cfg.get("shadow_color", [0, 0, 0, 140])))
    shadow_offset = tuple(box_cfg.get("shadow_offset", default_cfg.get("shadow_offset", [2, 3])))

    final_lines = [content]
    final_heights = []
    final_spacing = 0
    font_size = max_font_size

    while font_size >= min_font_size:
        font = ImageFont.truetype(font_path, font_size)

        if prefer_single_line:
            single_w, single_h = text_bbox(
                draw,
                content,
                font,
                reshape_enabled=reshape_enabled,
                prefer_raqm=prefer_raqm,
            )
            if single_w <= box_w and single_h <= box_h:
                lines = [content]
            elif prefer_balanced_lines:
                lines = break_lines_ar_balanced(
                    draw,
                    content,
                    font,
                    box_w,
                    max_lines=max_lines,
                    reshape_enabled=reshape_enabled,
                )
            else:
                lines = choose_text_lines(
                    draw,
                    content,
                    font,
                    box_w,
                    max_lines=max_lines,
                    reshape_enabled=reshape_enabled,
                )
        elif prefer_balanced_lines:
            lines = break_lines_ar_balanced(
                draw,
                content,
                font,
                box_w,
                max_lines=max_lines,
                reshape_enabled=reshape_enabled,
            )
        else:
            lines = choose_text_lines(
                draw,
                content,
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
            heights.append(max(1, int(hpx * line_height_factor)))

        spacing = max(0, int(font_size * line_spacing_factor))
        total_h = sum(heights) + spacing * (len(lines) - 1)

        if (max(widths) if widths else 0) <= box_w and total_h <= box_h:
            final_lines = lines
            final_heights = heights
            final_spacing = spacing
            break

        font_size -= 2

    font_size = max(min_font_size, font_size)
    font = ImageFont.truetype(font_path, font_size)
    if not final_heights:
        final_heights = [
            max(
                1,
                int(
                    text_bbox(
                        draw,
                        ln,
                        font,
                        reshape_enabled=reshape_enabled,
                        prefer_raqm=prefer_raqm,
                    )[1]
                    * line_height_factor
                ),
            )
            for ln in final_lines
        ]
        final_spacing = max(0, int(font_size * line_spacing_factor))

    total_h = sum(final_heights) + final_spacing * (len(final_lines) - 1)
    if vertical_align == "top":
        y = t + vertical_offset
    elif vertical_align == "bottom":
        y = b - total_h + vertical_offset
    else:
        y = t + max(0, (box_h - total_h) // 2) + vertical_offset

    for i, ln in enumerate(final_lines):
        rendered_text, draw_kwargs = get_text_render_parts(
            ln,
            reshape_enabled=reshape_enabled,
            prefer_raqm=prefer_raqm,
        )
        x = resolve_text_x_in_box(
            draw,
            ln,
            font,
            l,
            r,
            align=text_align,
            reshape_enabled=reshape_enabled,
            prefer_raqm=prefer_raqm,
        )
        sx, sy = shadow_offset
        draw.text((x + sx, y + sy), rendered_text, font=font, fill=shadow_color, **draw_kwargs)
        draw.text((x, y), rendered_text, font=font, fill=text_color, **draw_kwargs)
        y += final_heights[i] + final_spacing


# ===================== Render =====================
def render_post(news_img: Image.Image, text: str, template_cfg: dict) -> Image.Image:
    template_path = template_cfg["template_path"]
    font_bold_path = ensure_existing_path(template_cfg["font_bold_path"])

    with Image.open(template_path) as template_image:
        original_base = template_image.convert("RGBA")

    base = original_base.copy()
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
            mode=str(template_cfg.get("white_bg_mode", "all")),
        )
        news_img = autocrop_transparent(
            news_img,
            padding=int(template_cfg.get("transparent_crop_padding", 8)),
        )

    if bool(template_cfg.get("trim_bottom_white_band", False)):
        news_img = trim_bottom_white_band(
            news_img,
            threshold=int(template_cfg.get("trim_bottom_white_threshold", 245)),
            row_white_ratio=float(template_cfg.get("trim_bottom_white_ratio", 0.90)),
            start_scan_ratio=float(template_cfg.get("trim_bottom_white_start_ratio", 0.35)),
            min_run_rows=int(template_cfg.get("trim_bottom_white_min_run_rows", 36)),
            window_rows=int(template_cfg.get("trim_bottom_white_window_rows", 48)),
            window_match_ratio=float(template_cfg.get("trim_bottom_white_window_ratio", 0.80)),
            bottom_padding=int(template_cfg.get("trim_bottom_white_padding", 8)),
        )

    image_mode = str(template_cfg.get("image_mode", "full") or "full").strip().lower()
    if image_mode == "cover":
        image_mode = "full"
    top_bias, left_bias, image_zoom = resolve_image_crop_settings(news_img, template_cfg)

    if image_mode == "full":
        bottom = int(template_cfg.get("image_area_bottom", int(H * 0.58)))
        full_box = (0, 0, W, bottom)
        fitted = fit_image_to_box(news_img, full_box, top_bias=top_bias, left_bias=left_bias, zoom=image_zoom)
        canvas.paste(fitted, (0, 0), fitted if fitted.mode == "RGBA" else None)
        apply_image_gradient_overlay(canvas, template_cfg, full_box)

    else:
        raw_image_box = template_cfg.get("image_box")
        raw_mask_box = template_cfg.get("image_mask_box")
        if not isinstance(raw_image_box, (list, tuple)) or len(raw_image_box) != 4:
            fallback_bottom = int(template_cfg.get("image_area_bottom", int(H * 0.58)))
            raw_image_box = [0, 0, W, fallback_bottom]
        image_box = tuple(int(v) for v in raw_image_box)
        mask_shape = str(template_cfg.get("image_mask_shape", "rectangle")).lower()
        if not isinstance(raw_mask_box, (list, tuple)) or len(raw_mask_box) != 4:
            raw_mask_box = image_box
        mask_box = tuple(int(v) for v in raw_mask_box)
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
        apply_image_gradient_overlay(canvas, template_cfg, mask_box)

    canvas.alpha_composite(base)
    for overlay_box in template_cfg.get("template_overlay_boxes", []):
        if not isinstance(overlay_box, (list, tuple)) or len(overlay_box) != 4:
            continue
        l, t, r, b = [int(v) for v in overlay_box]
        if r <= l or b <= t:
            continue
        overlay_crop = original_base.crop((l, t, r, b))
        canvas.alpha_composite(overlay_crop, (l, t))

    for overlay_cfg in template_cfg.get("image_overlays", []):
        if not isinstance(overlay_cfg, dict):
            continue
        overlay_path = ensure_existing_path(resolve_path(overlay_cfg.get("path", "")))
        overlay_box = overlay_cfg.get("box")
        if not overlay_path or not overlay_box or len(overlay_box) != 4 or not os.path.isfile(overlay_path):
            continue
        l, t, r, b = [int(v) for v in overlay_box]
        if r <= l or b <= t:
            continue
        try:
            with Image.open(overlay_path).convert("RGBA") as overlay_img:
                if bool(overlay_cfg.get("remove_white_bg", False)):
                    overlay_img = remove_near_white_background(
                        overlay_img,
                        threshold=int(overlay_cfg.get("white_bg_threshold", 235)),
                        softness=int(overlay_cfg.get("white_bg_softness", 25)),
                        mode=str(overlay_cfg.get("white_bg_mode", "all")),
                    )
                    overlay_img = autocrop_transparent(
                        overlay_img,
                        padding=int(overlay_cfg.get("transparent_crop_padding", 2)),
                    )
                overlay_img = overlay_img.resize((r - l, b - t), Image.LANCZOS)
                canvas.alpha_composite(overlay_img, (l, t))
        except Exception:
            pass

    apply_gradient_overlay(
        canvas,
        template_cfg.get("foreground_gradient_overlay"),
        (0, 0, W, H),
    )

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

    if template_cfg.get("brand_text") and template_cfg.get("brand_box"):
        draw_centered_text_block(
            draw=draw,
            text=str(template_cfg.get("brand_text", "")),
            font_path=template_cfg.get("brand_font_bold_path", font_bold_path),
            box=tuple(template_cfg["brand_box"]),
            text_color=tuple(template_cfg.get("brand_text_color", [255, 255, 255])),
            shadow_color=tuple(template_cfg.get("brand_shadow_color", [0, 0, 0, 90])),
            shadow_offset=tuple(template_cfg.get("brand_shadow_offset", [1, 2])),
            max_font_size=int(template_cfg.get("brand_max_font_size", 52)),
            min_font_size=int(template_cfg.get("brand_min_font_size", 24)),
            max_lines=int(template_cfg.get("brand_max_lines", 1)),
            reshape_enabled=bool(template_cfg.get("brand_reshape_text", True)),
        )

    if template_cfg.get("brand_subtext") and template_cfg.get("brand_subtext_box"):
        draw_centered_text_block(
            draw=draw,
            text=str(template_cfg.get("brand_subtext", "")),
            font_path=template_cfg.get("brand_subtext_font_bold_path", template_cfg.get("brand_font_bold_path", font_bold_path)),
            box=tuple(template_cfg["brand_subtext_box"]),
            text_color=tuple(template_cfg.get("brand_subtext_color", [255, 255, 255])),
            shadow_color=tuple(template_cfg.get("brand_subtext_shadow_color", [0, 0, 0, 90])),
            shadow_offset=tuple(template_cfg.get("brand_subtext_shadow_offset", [1, 2])),
            max_font_size=int(template_cfg.get("brand_subtext_max_font_size", 34)),
            min_font_size=int(template_cfg.get("brand_subtext_min_font_size", 18)),
            max_lines=int(template_cfg.get("brand_subtext_max_lines", 1)),
            reshape_enabled=bool(template_cfg.get("brand_subtext_reshape_text", True)),
            prefer_raqm=bool(template_cfg.get("brand_subtext_prefer_raqm", True)),
        )

    if bool(template_cfg.get("stat_layout_enabled", False)):
        stat_segments = split_text_segments(
            text,
            separator=str(template_cfg.get("stat_separator", "|") or "|"),
            max_parts=3,
        )
        stat_number = stat_segments[0] if len(stat_segments) >= 1 else ""
        stat_word = stat_segments[1] if len(stat_segments) >= 2 else ""
        stat_body = stat_segments[2] if len(stat_segments) >= 3 else ""
        fixed_stat_word = str(template_cfg.get("stat_word_fixed_text", "") or "").strip()
        if fixed_stat_word:
            stat_word = fixed_stat_word

        if template_cfg.get("stat_number_box") and stat_number:
            draw_centered_text_block(
                draw=draw,
                text=stat_number,
                font_path=template_cfg.get("stat_number_font_bold_path", font_bold_path),
                box=tuple(template_cfg["stat_number_box"]),
                text_color=tuple(template_cfg.get("stat_number_text_color", [255, 220, 60])),
                shadow_color=tuple(template_cfg.get("stat_number_shadow_color", [0, 0, 0, 120])),
                shadow_offset=tuple(template_cfg.get("stat_number_shadow_offset", [2, 3])),
                max_font_size=int(template_cfg.get("stat_number_max_font_size", 220)),
                min_font_size=int(template_cfg.get("stat_number_min_font_size", 80)),
                max_lines=1,
                reshape_enabled=bool(template_cfg.get("stat_number_reshape_text", True)),
                prefer_raqm=bool(template_cfg.get("stat_number_prefer_raqm", True)),
                vertical_offset=int(template_cfg.get("stat_number_vertical_offset", 0)),
            )

        if template_cfg.get("stat_word_bg_box"):
            draw_fill_box(
                draw,
                tuple(template_cfg["stat_word_bg_box"]),
                tuple(template_cfg.get("stat_word_bg_fill", [24, 52, 80, 255])),
                int(template_cfg.get("stat_word_bg_radius", 0)),
            )

        if template_cfg.get("stat_word_box") and stat_word:
            draw_centered_text_block(
                draw=draw,
                text=stat_word,
                font_path=template_cfg.get("stat_word_font_bold_path", font_bold_path),
                box=tuple(template_cfg["stat_word_box"]),
                text_color=tuple(template_cfg.get("stat_word_text_color", [255, 220, 60])),
                shadow_color=tuple(template_cfg.get("stat_word_shadow_color", [0, 0, 0, 90])),
                shadow_offset=tuple(template_cfg.get("stat_word_shadow_offset", [1, 2])),
                max_font_size=int(template_cfg.get("stat_word_max_font_size", 62)),
                min_font_size=int(template_cfg.get("stat_word_min_font_size", 24)),
                max_lines=1,
                reshape_enabled=bool(template_cfg.get("stat_word_reshape_text", True)),
                prefer_raqm=bool(template_cfg.get("stat_word_prefer_raqm", True)),
                vertical_offset=int(template_cfg.get("stat_word_vertical_offset", 0)),
            )

        if template_cfg.get("stat_body_box") and stat_body:
            draw_centered_text_block(
                draw=draw,
                text=stat_body,
                font_path=template_cfg.get("stat_body_font_bold_path", font_bold_path),
                box=tuple(template_cfg["stat_body_box"]),
                text_color=tuple(template_cfg.get("stat_body_text_color", [255, 255, 255])),
                shadow_color=tuple(template_cfg.get("stat_body_shadow_color", [0, 0, 0, 140])),
                shadow_offset=tuple(template_cfg.get("stat_body_shadow_offset", [2, 3])),
                max_font_size=int(template_cfg.get("stat_body_max_font_size", 74)),
                min_font_size=int(template_cfg.get("stat_body_min_font_size", 32)),
                max_lines=int(template_cfg.get("stat_body_max_lines", 2)),
                reshape_enabled=bool(template_cfg.get("stat_body_reshape_text", True)),
                prefer_raqm=bool(template_cfg.get("stat_body_prefer_raqm", True)),
                vertical_offset=int(template_cfg.get("stat_body_vertical_offset", 0)),
            )

        return canvas.convert("RGB")

    if template_cfg.get("name_box"):
        primary_name_text, subtitle_text = split_name_and_subtitle(text, template_cfg)
        name_box = tuple(template_cfg["name_box"])
        name_font_path = template_cfg.get("name_font_bold_path", font_bold_path)
        name_render_engine = str(template_cfg.get("name_render_engine", "")).lower()
        name_reshape_text = bool(template_cfg.get("name_reshape_text", True))
        name_prefer_raqm = bool(template_cfg.get("name_prefer_raqm", True))
        name_prefer_linux_system_font = bool(template_cfg.get("name_prefer_linux_system_font", False))
        using_linux_system_font = False
        if _count_arabic_chars(primary_name_text) > 0 and template_cfg.get("name_arabic_font_bold_path"):
            name_font_path = template_cfg.get("name_arabic_font_bold_path", name_font_path)
            name_render_engine = str(template_cfg.get("name_arabic_render_engine", name_render_engine)).lower()
            name_reshape_text = bool(template_cfg.get("name_arabic_reshape_text", name_reshape_text))
            name_prefer_raqm = bool(template_cfg.get("name_arabic_prefer_raqm", name_prefer_raqm))
            name_prefer_linux_system_font = bool(
                template_cfg.get("name_arabic_prefer_linux_system_font", name_prefer_linux_system_font)
            )
            if os.name != "nt" and name_prefer_linux_system_font:
                linux_arabic_font = find_linux_arabic_font()
                if linux_arabic_font:
                    print(f"[Arabic] Using preferred Linux system font for name: {linux_arabic_font}")
                    name_font_path = linux_arabic_font
                    using_linux_system_font = True
            if not PILLOW_HAS_RAQM and template_cfg.get("name_no_raqm_font_bold_path") and not using_linux_system_font:
                name_font_path = template_cfg.get("name_no_raqm_font_bold_path", name_font_path)
            configured_name_font_exists = bool(name_font_path and os.path.isfile(name_font_path))
            if os.name != "nt" and not PILLOW_HAS_RAQM and not configured_name_font_exists:
                linux_arabic_font = find_linux_arabic_font()
                if linux_arabic_font:
                    print(f"[Arabic] Using Linux fallback font for name: {linux_arabic_font}")
                    name_font_path = linux_arabic_font
                    using_linux_system_font = True
                else:
                    fallback_font_path = template_cfg.get("name_font_bold_path", font_bold_path)
                    if fallback_font_path and os.path.isfile(fallback_font_path):
                        print(f"[Arabic] Linux fallback font not found, using template font: {fallback_font_path}")
                        name_font_path = fallback_font_path
        name_font_path = ensure_existing_path(name_font_path, font_bold_path)
        if _count_arabic_chars(primary_name_text) > 0:
            debug_log_arabic_render(
                "name_render",
                template_id=template_cfg.get("id"),
                platform=os.name,
                raqm=PILLOW_HAS_RAQM,
                text=primary_name_text,
                font_path=name_font_path,
                font_exists=bool(name_font_path and os.path.isfile(name_font_path)),
                render_engine=name_render_engine,
                reshape=name_reshape_text,
                prefer_raqm=name_prefer_raqm,
                prefer_linux_system_font=name_prefer_linux_system_font,
                max_lines=int(template_cfg.get("name_max_lines", 2)),
            )
        name_text_color = tuple(template_cfg.get("name_text_color", template_cfg.get("text_color", [255, 255, 255])))
        name_shadow_color = tuple(template_cfg.get("name_shadow_color", template_cfg.get("shadow_color", [0, 0, 0, 140])))
        name_shadow_offset = tuple(template_cfg.get("name_shadow_offset", template_cfg.get("shadow_offset", [2, 3])))
        name_max_font_size = int(template_cfg.get("name_max_font_size", 84))
        name_min_font_size = int(template_cfg.get("name_min_font_size", 40))
        name_max_lines = int(template_cfg.get("name_max_lines", 2))
        name_vertical_offset = int(template_cfg.get("name_vertical_offset", 0))

        if name_render_engine == "gdi":
            rendered_name = render_text_block_gdi(
                text=primary_name_text,
                font_path=name_font_path,
                box=name_box,
                text_color=name_text_color,
                shadow_color=name_shadow_color,
                shadow_offset=name_shadow_offset,
                max_font_size=name_max_font_size,
                min_font_size=name_min_font_size,
                max_lines=name_max_lines,
                reshape_enabled=name_reshape_text,
                vertical_offset=name_vertical_offset,
            )
            if rendered_name is not None:
                canvas.alpha_composite(rendered_name, (name_box[0], name_box[1]))
            else:
                draw_centered_text_block(
                    draw=draw,
                    text=primary_name_text,
                    font_path=name_font_path,
                    box=name_box,
                    text_color=name_text_color,
                    shadow_color=name_shadow_color,
                    shadow_offset=name_shadow_offset,
                    max_font_size=name_max_font_size,
                    min_font_size=name_min_font_size,
                    max_lines=name_max_lines,
                    reshape_enabled=name_reshape_text,
                    prefer_raqm=name_prefer_raqm,
                    vertical_offset=name_vertical_offset,
                )
        else:
            draw_centered_text_block(
                draw=draw,
                text=primary_name_text,
                font_path=name_font_path,
                box=name_box,
                text_color=name_text_color,
                shadow_color=name_shadow_color,
                shadow_offset=name_shadow_offset,
                max_font_size=name_max_font_size,
                min_font_size=name_min_font_size,
                max_lines=name_max_lines,
                reshape_enabled=name_reshape_text,
                prefer_raqm=name_prefer_raqm,
                vertical_offset=name_vertical_offset,
            )

        if subtitle_text and template_cfg.get("name_subtitle_box"):
            subtitle_box = tuple(template_cfg["name_subtitle_box"])
            subtitle_font_path = ensure_existing_path(
                template_cfg.get("name_subtitle_font_bold_path", name_font_path),
                name_font_path,
            )
            subtitle_text_color = tuple(template_cfg.get("name_subtitle_text_color", name_text_color))
            subtitle_shadow_color = tuple(template_cfg.get("name_subtitle_shadow_color", name_shadow_color))
            subtitle_shadow_offset = tuple(template_cfg.get("name_subtitle_shadow_offset", name_shadow_offset))
            subtitle_max_font_size = int(template_cfg.get("name_subtitle_max_font_size", 26))
            subtitle_min_font_size = int(template_cfg.get("name_subtitle_min_font_size", 16))
            subtitle_max_lines = int(template_cfg.get("name_subtitle_max_lines", 1))
            subtitle_vertical_offset = int(template_cfg.get("name_subtitle_vertical_offset", 0))

            draw_centered_text_block(
                draw=draw,
                text=subtitle_text,
                font_path=subtitle_font_path,
                box=subtitle_box,
                text_color=subtitle_text_color,
                shadow_color=subtitle_shadow_color,
                shadow_offset=subtitle_shadow_offset,
                max_font_size=subtitle_max_font_size,
                min_font_size=subtitle_min_font_size,
                max_lines=subtitle_max_lines,
                reshape_enabled=bool(template_cfg.get("name_subtitle_reshape_text", True)),
                prefer_raqm=bool(template_cfg.get("name_subtitle_prefer_raqm", True)),
                vertical_offset=subtitle_vertical_offset,
                prefer_single_line=bool(template_cfg.get("name_subtitle_prefer_single_line", False)),
            )

    if not bool(template_cfg.get("render_text", True)):
        return canvas.convert("RGB")

    text_boxes = template_cfg.get("text_boxes")
    if isinstance(text_boxes, list) and text_boxes:
        separator = str(template_cfg.get("text_box_separator", "|") or "|")
        rendered_any_text = False
        for box_cfg in text_boxes:
            if not isinstance(box_cfg, dict):
                continue
            if not bool(box_cfg.get("enabled", True)):
                continue
            box_text = resolve_text_box_content(text, box_cfg, separator=separator)
            if not box_text:
                continue
            draw_smart_text_box(
                draw=draw,
                text=box_text,
                font_path=ensure_existing_path(box_cfg.get("font_bold_path", font_bold_path), font_bold_path),
                box_cfg=box_cfg,
                default_cfg=template_cfg,
            )
            rendered_any_text = True
        if rendered_any_text:
            return canvas.convert("RGB")

    text_box = tuple(template_cfg["text_box"])
    if template_cfg.get("text_bg_box"):
        draw_fill_box(
            draw,
            tuple(template_cfg["text_bg_box"]),
            tuple(template_cfg.get("text_bg_fill", [0, 0, 0, 140])),
            int(template_cfg.get("text_bg_radius", 0)),
        )
    elif bool(template_cfg.get("text_bg_use_text_box", False)):
        draw_fill_box(
            draw,
            text_box,
            tuple(template_cfg.get("text_bg_fill", [0, 0, 0, 140])),
            int(template_cfg.get("text_bg_radius", 0)),
        )

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
    few_lines_threshold = int(template_cfg.get("few_lines_threshold", 0))

    def is_few_lines_mode(line_count: int) -> bool:
        return few_lines_threshold > 0 and line_count <= few_lines_threshold
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
            lines = choose_text_lines(draw, text, font, box_w, max_lines=max_lines)
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
        if is_few_lines_mode(len(lines)):
            spacing_factor = float(
                template_cfg.get(
                    "few_line_spacing_factor",
                    template_cfg.get("short_line_spacing_factor", spacing_factor),
                )
            )

        line_height_factor = float(template_cfg.get("line_height_factor", 1.0))
        if short_style_mode:
            line_height_factor = float(template_cfg.get("short_line_height_factor", line_height_factor))
        if is_few_lines_mode(len(lines)):
            line_height_factor = float(
                template_cfg.get(
                    "few_line_height_factor",
                    template_cfg.get("short_line_height_factor", line_height_factor),
                )
            )
        effective_heights = [max(1, int(h * line_height_factor)) for h in heights]

        spacing = int(font_size * spacing_factor)
        total_h = sum(effective_heights) + spacing * (len(lines) - 1)

        if (max(widths) if widths else 0) <= box_w and total_h <= box_h:
            final_lines = lines
            final_heights = effective_heights
            final_spacing = spacing
            break

        font_size -= 2

    if final_lines is None:
        font_size = min_font_size
        font = ImageFont.truetype(font_bold_path, font_size)
        if short_style_mode:
            final_lines = choose_text_lines(draw, text, font, box_w, max_lines=max_lines)
        else:
            final_lines = break_lines_ar_balanced(draw, text, font, box_w, max_lines=max_lines)

        spacing_factor = float(template_cfg.get("line_spacing_factor", 0.24))
        if short_style_mode:
            spacing_factor = float(template_cfg.get("short_line_spacing_factor", 0.18))
        if is_few_lines_mode(len(final_lines)):
            spacing_factor = float(
                template_cfg.get(
                    "few_line_spacing_factor",
                    template_cfg.get("short_line_spacing_factor", spacing_factor),
                )
            )
        line_height_factor = float(template_cfg.get("line_height_factor", 1.0))
        if short_style_mode:
            line_height_factor = float(template_cfg.get("short_line_height_factor", line_height_factor))
        if is_few_lines_mode(len(final_lines)):
            line_height_factor = float(
                template_cfg.get(
                    "few_line_height_factor",
                    template_cfg.get("short_line_height_factor", line_height_factor),
                )
            )
        final_heights = [
            max(1, int(text_bbox(draw, ln, font)[1] * line_height_factor))
            for ln in final_lines
        ]
        final_spacing = int(font_size * spacing_factor)

    font = ImageFont.truetype(font_bold_path, font_size)

    top_start_offset = int(template_cfg.get("top_start_offset", -35))
    stretch_long_text = bool(template_cfg.get("stretch_long_text", True))
    stretch_short_text = bool(template_cfg.get("stretch_short_text", True))
    compact_short_text = bool(template_cfg.get("compact_short_text", False))
    few_lines_mode = is_few_lines_mode(len(final_lines))
    stretch_few_lines_text = bool(template_cfg.get("stretch_few_lines_text", False))
    compact_few_lines_text = bool(template_cfg.get("compact_few_lines_text", False))

    short_centered_layout = bool(template_cfg.get("short_centered_layout", True))
    short_center_offset = int(template_cfg.get("short_center_offset", -70))
    long_centered_layout = bool(template_cfg.get("long_centered_layout", False))
    long_center_offset = int(template_cfg.get("long_center_offset", 0))
    few_lines_centered_layout = bool(template_cfg.get("few_lines_centered_layout", True))
    few_lines_center_offset = int(template_cfg.get("few_lines_center_offset", 0))

    if few_lines_mode and few_lines_centered_layout:
        if compact_few_lines_text and len(final_lines) > 1:
            few_lines_max_fill_ratio = float(
                template_cfg.get(
                    "few_lines_max_fill_ratio",
                    template_cfg.get("short_max_fill_ratio", 0.34),
                )
            )
            max_total_h = int(box_h * few_lines_max_fill_ratio)
            if max_total_h > 0:
                available_spacing = max_total_h - sum(final_heights)
                max_spacing = max(0, available_spacing // (len(final_lines) - 1))
                final_spacing = min(final_spacing, max_spacing)
        elif len(final_lines) > 1:
            few_lines_fill_ratio = float(
                template_cfg.get(
                    "few_lines_fill_ratio",
                    template_cfg.get("short_fill_ratio", 0.86),
                )
            )
            target_total_h = int(box_h * few_lines_fill_ratio)
            desired_spacing = target_total_h - sum(final_heights)
            final_spacing = max(final_spacing, desired_spacing)
        total_h = sum(final_heights) + final_spacing * (len(final_lines) - 1)
        y = t + max(0, (box_h - total_h) // 2) + few_lines_center_offset
    elif short_style_mode and short_centered_layout:
        if compact_short_text and len(final_lines) > 1:
            short_max_fill_ratio = float(template_cfg.get("short_max_fill_ratio", 0.34))
            max_total_h = int(box_h * short_max_fill_ratio)
            if max_total_h > 0:
                available_spacing = max_total_h - sum(final_heights)
                max_spacing = max(0, available_spacing // (len(final_lines) - 1))
                final_spacing = min(final_spacing, max_spacing)
        elif len(final_lines) > 1:
            short_fill_ratio = float(template_cfg.get("short_fill_ratio", 0.86))
            target_total_h = int(box_h * short_fill_ratio)
            desired_spacing = target_total_h - sum(final_heights)
            final_spacing = max(final_spacing, desired_spacing)
        total_h = sum(final_heights) + final_spacing * (len(final_lines) - 1)
        y = t + max(0, (box_h - total_h) // 2) + short_center_offset
    elif not short_style_mode and long_centered_layout:
        if len(final_lines) > 1:
            long_fill_ratio = float(template_cfg.get("long_fill_ratio", 0.52))
            target_total_h = int(box_h * long_fill_ratio)
            desired_spacing = target_total_h - sum(final_heights)
            final_spacing = max(final_spacing, desired_spacing)
        total_h = sum(final_heights) + final_spacing * (len(final_lines) - 1)
        y = t + max(0, (box_h - total_h) // 2) + long_center_offset
    else:
        y = t + top_start_offset

    if stretch_few_lines_text and len(final_lines) > 1 and few_lines_mode:
        available_spacing = box_h - sum(final_heights)
        if available_spacing > 0:
            final_spacing = max(final_spacing, available_spacing // (len(final_lines) - 1))

    if stretch_short_text and not compact_short_text and len(final_lines) > 1 and short_style_mode and not few_lines_mode:
        available_spacing = box_h - sum(final_heights)
        if available_spacing > 0:
            final_spacing = max(final_spacing, available_spacing // (len(final_lines) - 1))

    if stretch_long_text and len(final_lines) > 1 and words_count >= long_words_threshold:
        available_spacing = box_h - sum(final_heights)
        if available_spacing > 0:
            final_spacing = max(final_spacing, available_spacing // (len(final_lines) - 1))

    for i, ln in enumerate(final_lines):
        rendered_text, draw_kwargs = get_text_render_parts(ln)
        x = resolve_text_x_in_box(draw, ln, font, l, r, align=text_align)

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
    if not await safe_answer_callback(q):
        return

    templates = get_templates(context)
    template_id = q.data.split(":", 1)[1]

    if template_id not in templates:
        await q.edit_message_text("القالب غير موجود. جرّب /start.")
        return

    context.user_data["template_id"] = template_id
    template_cfg = templates[template_id]
    name = template_cfg.get("name", template_id)
    select_prompt = str(template_cfg.get("select_prompt", "")).strip()

    if bool(template_cfg.get("requires_name", False)):
        prompt = select_prompt or "ابعت الصورة أولاً، وبعدها سأطلب منك الاسم."
        await q.edit_message_text(f"تم اختيار القالب: {name}\n{prompt}")
    elif bool(template_cfg.get("requires_text", True)):
        prompt = select_prompt or "الآن ابعث صورة الخبر."
        await q.edit_message_text(f"تم اختيار القالب: {name}\n{prompt}")
    else:
        prompt = select_prompt or "ابعت الصورة فقط."
        await q.edit_message_text(f"تم اختيار القالب: {name}\n{prompt}")


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
        after_photo_prompt = str(
            template_cfg.get("after_photo_prompt", "ابعت الاسم الذي تريد إضافته فوق العبارة.")
        ).strip()
        await update.message.reply_text(after_photo_prompt)
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


async def start_v2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await show_start_menu(update.message, context)


async def templates_cmd_v2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("role") not in {"admin", "employee"}:
        await show_start_menu(update.message, context, "لازم تختار مدير أو موظف أولاً.")
        return
    await send_templates_menu(update.message, context)


async def role_cb_v2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await safe_answer_callback(q):
        return
    role = q.data.split(":", 1)[1]
    context.user_data.clear()

    if role == "admin":
        clear_obsolete_auth_state(context)
        context.user_data["awaiting_admin_password"] = True
        await q.edit_message_text("أرسل كلمة سر المدير.")
        return

    state = load_admin_state()
    user_id = q.from_user.id
    employee_ids = set(state.get("employee_ids", []))
    max_employees = int(state.get("max_employees", 0) or 0)

    if user_id not in employee_ids:
        if max_employees > 0 and len(employee_ids) >= max_employees:
            await q.edit_message_text("وصلت لعدد الموظفين المسموح. راجع المدير لزيادة العدد.")
            return
        employee_ids.add(user_id)
        state["employee_ids"] = sorted(employee_ids)
        save_admin_state(state)

    context.user_data["role"] = "employee"
    await q.edit_message_text("تم تسجيلك كموظف.")
    await send_templates_menu(q.message, context)


async def admin_menu_cb_v2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await safe_answer_callback(q):
        return

    if context.user_data.get("role") != "admin":
        await safe_answer_callback(q, "هذه القائمة للمدير فقط.", show_alert=True)
        return

    templates = get_templates(context, force_reload=True)
    state = load_admin_state()
    action = q.data.split(":", 1)[1]

    if action == "menu":
        await q.edit_message_text(admin_status_text(state, templates), reply_markup=admin_menu_keyboard())
        return

    if action == "templates":
        enabled_ids = set(state.get("enabled_templates", []))
        await q.edit_message_text(
            "فعّل أو عطّل القوالب التي تظهر للموظفين:",
            reply_markup=template_toggle_keyboard(templates, enabled_ids),
        )
        return

    if action == "add_template":
        clear_obsolete_auth_state(context)
        context.user_data.pop("awaiting_max_employees", None)
        context.user_data["awaiting_new_template_name"] = True
        context.user_data.pop("awaiting_new_template_image", None)
        context.user_data.pop("pending_template_name", None)
        await q.edit_message_text("أرسل اسم القالب الجديد.")
        return

    if action == "max_employees":
        clear_obsolete_auth_state(context)
        context.user_data.pop("awaiting_new_template_name", None)
        context.user_data.pop("awaiting_new_template_image", None)
        context.user_data.pop("pending_template_name", None)
        context.user_data["awaiting_max_employees"] = True
        await q.edit_message_text("أرسل الآن عدد الموظفين المسموح به كرقم فقط.")
        return

    if action == "status":
        await q.edit_message_text(admin_status_text(state, templates), reply_markup=admin_menu_keyboard())
        return


async def nav_cb_v2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await safe_answer_callback(q):
        return
    context.user_data.clear()
    await q.edit_message_text("تمت العودة للبداية.")
    await show_start_menu(q.message, context)


async def admin_template_toggle_cb_v2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await safe_answer_callback(q):
        return

    if context.user_data.get("role") != "admin":
        await safe_answer_callback(q, "هذه القائمة للمدير فقط.", show_alert=True)
        return

    templates = get_templates(context, force_reload=True)
    callback_token = q.data.split(":", 1)[1]
    template_id = find_template_id_by_callback_token(templates, callback_token)
    if template_id not in templates:
        await safe_answer_callback(q, "القالب غير موجود", show_alert=True)
        return

    state = load_admin_state()
    enabled_ids = set(state.get("enabled_templates", []))
    if template_id in enabled_ids:
        enabled_ids.remove(template_id)
    else:
        enabled_ids.add(template_id)

    state["enabled_templates"] = sorted(enabled_ids)
    save_admin_state(state)
    await q.edit_message_text(
        "فعّل أو عطّل القوالب التي تظهر للموظفين:",
        reply_markup=template_toggle_keyboard(templates, enabled_ids),
    )


async def admin_template_delete_cb_v2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await safe_answer_callback(q):
        return

    if context.user_data.get("role") != "admin":
        await safe_answer_callback(q, "هذه القائمة للمدير فقط.", show_alert=True)
        return

    templates = get_templates(context, force_reload=True)
    callback_token = q.data.split(":", 1)[1]
    template_id = find_template_id_by_callback_token(templates, callback_token)
    if template_id not in templates:
        await safe_answer_callback(q, "القالب غير موجود", show_alert=True)
        return

    if template_id == DEFAULT_TEMPLATE_ID:
        await safe_answer_callback(q, "لا يمكن حذف القالب الافتراضي.", show_alert=True)
        return

    display_name = str(templates[template_id].get("name", template_id))
    await q.edit_message_text(
        f"هل تريد حذف القالب:\n{display_name} [{template_id}]؟\nسيتم حذف ملفاته نهائياً من مجلد templates.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("تأكيد الحذف", callback_data=f"admin_tpl_del_confirm:{make_template_callback_id(template_id)}"),
                    InlineKeyboardButton("إلغاء", callback_data="admin:templates"),
                ]
            ]
        ),
    )


async def admin_template_delete_confirm_cb_v2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await safe_answer_callback(q):
        return

    if context.user_data.get("role") != "admin":
        await safe_answer_callback(q, "هذه القائمة للمدير فقط.", show_alert=True)
        return

    templates = get_templates(context, force_reload=True)
    callback_token = q.data.split(":", 1)[1]
    template_id = find_template_id_by_callback_token(templates, callback_token)
    if template_id not in templates:
        await safe_answer_callback(q, "القالب غير موجود", show_alert=True)
        return

    if template_id == DEFAULT_TEMPLATE_ID:
        await safe_answer_callback(q, "لا يمكن حذف القالب الافتراضي.", show_alert=True)
        return

    delete_error = delete_template_from_disk(templates[template_id])
    if delete_error:
        await q.edit_message_text(
            f"تعذر حذف القالب [{template_id}].\nالسبب: {delete_error}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("رجوع", callback_data="admin:templates")]]),
        )
        return

    state = load_admin_state()
    enabled_ids = set(state.get("enabled_templates", []))
    enabled_ids.discard(template_id)
    state["enabled_templates"] = sorted(enabled_ids)
    save_admin_state(state)

    await q.edit_message_text(
        f"تم حذف القالب [{template_id}] بنجاح.",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("إدارة القوالب", callback_data="admin:templates")],
                [InlineKeyboardButton("رجوع", callback_data="admin:menu")],
            ]
        ),
    )


async def choose_template_cb_v2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await safe_answer_callback(q):
        return

    templates = get_templates(context, force_reload=True)
    state = load_admin_state()
    available_templates = get_enabled_templates(templates, state, context.user_data.get("role"))
    callback_token = q.data.split(":", 1)[1]
    template_id = find_template_id_by_callback_token(available_templates, callback_token)

    if template_id not in available_templates:
        template_id = find_template_id_by_callback_token(templates, callback_token)
        if template_id in templates and template_id not in available_templates:
            await q.edit_message_text("هذا القالب غير مفعّل لهذا الحساب حالياً.")
            return
        await q.edit_message_text("تم تحديث القائمة. اختر القالب من جديد.", reply_markup=templates_keyboard(available_templates))
        return

    context.user_data["template_id"] = template_id
    template_cfg = available_templates[template_id]
    name = template_cfg.get("name", template_id)
    select_prompt = str(template_cfg.get("select_prompt", "")).strip()

    if bool(template_cfg.get("requires_name", False)):
        prompt = select_prompt or "ابعت الصورة أولاً، وبعدها سأطلب منك الاسم أو النص المطلوب."
    elif bool(template_cfg.get("requires_text", True)):
        prompt = select_prompt or "الآن ابعث الصورة."
    else:
        prompt = select_prompt or "ابعت الصورة فقط."
    await q.edit_message_text(f"تم اختيار القالب: {name}\n{prompt}")


async def mode_cb_v2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await safe_answer_callback(q):
        return

    if context.user_data.get("role") not in {"admin", "employee"}:
        await q.edit_message_text("اختر أولاً الدخول كمدير أو موظف.")
        return

    mode = q.data.split(":", 1)[1]
    if mode != "montage":
        await safe_answer_callback(q, "وضع غير مدعوم.", show_alert=True)
        return

    reset_design_state(context)
    clear_stat_prompt_state(context)
    clear_montage_state(context)
    context.user_data["awaiting_montage_video"] = True
    await q.edit_message_text(
        "تم اختيار وضع المونتاج.\nأرسل الآن فيديو MP4 أو MOV، وبعده سأطلب منك النص المتحرك."
    )


async def store_montage_video_from_telegram_file(file_obj, suggested_name: str) -> str:
    ext = os.path.splitext(suggested_name or "")[1].lower()
    if ext not in {".mp4", ".mov", ".m4v", ".webm", ".mkv"}:
        ext = ".mp4"
    tmp_path = os.path.join(BASE_DIR, f"montage_{next(tempfile._get_candidate_names())}{ext}")
    await file_obj.download_to_drive(custom_path=tmp_path)
    return tmp_path


async def handle_photo_v2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_montage_video"):
        await update.message.reply_text("أرسل فيديو للمونتاج، وليس صورة.")
        return

    if context.user_data.get("awaiting_new_template_image"):
        if context.user_data.get("role") != "admin":
            reset_design_state(context)
            await show_start_menu(update.message, context)
            return

        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        bio = BytesIO()
        await file.download_to_memory(out=bio)
        folder_name = create_template_from_image(
            context.user_data.get("pending_template_name", "قالب جديد"),
            bio.getvalue(),
        )
        templates, state, register_error = register_new_template(context, folder_name)
        if register_error:
            reset_design_state(context)
            context.user_data["role"] = "admin"
            await update.message.reply_text(
                f"تم حفظ القالب في templates/{folder_name} لكنه لم يظهر في إدارة القوالب.\n"
                f"السبب: {register_error}",
                reply_markup=admin_menu_keyboard(),
            )
            return
        reset_design_state(context)
        context.user_data["role"] = "admin"
        await update.message.reply_text(
            f"تمت إضافة القالب بنجاح: {templates.get(folder_name, {}).get('name', folder_name)}\n"
            f"المجلد: templates/{folder_name}",
            reply_markup=admin_menu_keyboard(),
        )
        enabled_ids = set(state.get("enabled_templates", []))
        await update.message.reply_text(
            "فعّل أو عطّل القوالب التي تظهر للموظفين:",
            reply_markup=template_toggle_keyboard(templates, enabled_ids),
        )
        return

    if context.user_data.get("role") not in {"admin", "employee"}:
        await show_start_menu(update.message, context, "اختر أولاً الدخول كمدير أو موظف.")
        return

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
    state = load_admin_state()
    available_templates = get_enabled_templates(templates, state, context.user_data.get("role"))
    template_cfg = get_template_cfg(available_templates, context.user_data.get("template_id"))

    if bool(template_cfg.get("stat_layout_enabled", False)):
        clear_stat_prompt_state(context)
        context.user_data["news_img"] = img
        context.user_data["awaiting_stat_number"] = True
        await update.message.reply_text("أرسل الآن الرقم الذي سيظهر داخل الصندوق الأصفر.")
        return

    if bool(template_cfg.get("requires_name", False)):
        context.user_data["news_img"] = img
        after_photo_prompt = str(
            template_cfg.get("after_photo_prompt", "ابعت الآن النص أو الاسم المطلوب إضافته على التصميم.")
        ).strip()
        await update.message.reply_text(after_photo_prompt)
        return

    if not bool(template_cfg.get("requires_text", True)):
        try:
            await send_rendered_post(update, context, template_cfg, img)
        except Exception as e:
            await update.message.reply_text(f"صار خطأ أثناء التصميم: {e}")
            reset_design_state(context)
        return

    context.user_data["news_img"] = img
    await update.message.reply_text("تمام. الآن ابعث النص.")


async def handle_image_document_v2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    if not document:
        return

    if context.user_data.get("awaiting_montage_video"):
        if not is_video_document(document):
            await update.message.reply_text("أرسل فيديو بصيغة MP4 أو MOV أو ملف فيديو واضح.")
            return
        file = await context.bot.get_file(document.file_id)
        suggested_name = getattr(document, "file_name", "") or "montage.mp4"
        tmp_path = await store_montage_video_from_telegram_file(file, suggested_name)
        clear_montage_state(context)
        context.user_data["montage_video_path"] = tmp_path
        context.user_data["montage_video_name"] = suggested_name
        context.user_data["awaiting_montage_text"] = True
        await update.message.reply_text("أرسل الآن النص الذي تريد تحريكه على الفيديو.")
        return

    if context.user_data.get("awaiting_new_template_image"):
        if context.user_data.get("role") != "admin":
            reset_design_state(context)
            await show_start_menu(update.message, context)
            return
        if not is_image_document(document):
            await update.message.reply_text("أرسل صورة قالب بصيغة PNG أو JPG أو WEBP.")
            return

        file = await context.bot.get_file(document.file_id)
        bio = BytesIO()
        await file.download_to_memory(out=bio)
        folder_name = create_template_from_image(
            context.user_data.get("pending_template_name", "قالب جديد"),
            bio.getvalue(),
        )
        templates, state, register_error = register_new_template(context, folder_name)
        if register_error:
            reset_design_state(context)
            context.user_data["role"] = "admin"
            await update.message.reply_text(
                f"تم حفظ القالب في templates/{folder_name} لكنه لم يظهر في إدارة القوالب.\n"
                f"السبب: {register_error}",
                reply_markup=admin_menu_keyboard(),
            )
            return
        reset_design_state(context)
        context.user_data["role"] = "admin"
        await update.message.reply_text(
            f"تمت إضافة القالب بنجاح: {templates.get(folder_name, {}).get('name', folder_name)}\n"
            f"المجلد: templates/{folder_name}"
        )
        enabled_ids = set(state.get("enabled_templates", []))
        await update.message.reply_text(
            "فعّل أو عطّل القوالب التي تظهر للموظفين:",
            reply_markup=template_toggle_keyboard(templates, enabled_ids),
        )
        return

    if not is_image_document(document):
        await update.message.reply_text("هذا الملف ليس صورة مدعومة.")
        return

    if context.user_data.get("role") not in {"admin", "employee"}:
        await show_start_menu(update.message, context, "اختر أولاً الدخول كمدير أو موظف.")
        return

    if "template_id" not in context.user_data:
        await update.message.reply_text("اختر قالب أولاً من /start أو /templates.")
        return

    file = await context.bot.get_file(document.file_id)
    bio = BytesIO()
    await file.download_to_memory(out=bio)
    bio.seek(0)

    img = Image.open(bio)
    templates = get_templates(context)
    state = load_admin_state()
    available_templates = get_enabled_templates(templates, state, context.user_data.get("role"))
    template_cfg = get_template_cfg(available_templates, context.user_data.get("template_id"))

    if bool(template_cfg.get("stat_layout_enabled", False)):
        clear_stat_prompt_state(context)
        context.user_data["news_img"] = img
        context.user_data["awaiting_stat_number"] = True
        await update.message.reply_text("أرسل الآن الرقم الذي سيظهر داخل الصندوق الأصفر.")
        return

    if bool(template_cfg.get("requires_name", False)):
        context.user_data["news_img"] = img
        after_photo_prompt = str(
            template_cfg.get("after_photo_prompt", "ابعت الآن النص أو الاسم المطلوب إضافته على التصميم.")
        ).strip()
        await update.message.reply_text(after_photo_prompt)
        return

    if not bool(template_cfg.get("requires_text", True)):
        try:
            await send_rendered_post(update, context, template_cfg, img)
        except Exception as e:
            await update.message.reply_text(f"صار خطأ أثناء التصميم: {e}")
            reset_design_state(context)
        return

    context.user_data["news_img"] = img
    await update.message.reply_text("تمام. الآن ابعث النص.")


async def handle_video_v2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video = update.message.video
    if not video:
        return

    if not context.user_data.get("awaiting_montage_video"):
        await update.message.reply_text("إذا أردت المونتاج اختر زر مونتاج أولاً.")
        return

    file = await context.bot.get_file(video.file_id)
    suggested_name = getattr(video, "file_name", "") or "montage.mp4"
    tmp_path = await store_montage_video_from_telegram_file(file, suggested_name)
    clear_montage_state(context)
    context.user_data["montage_video_path"] = tmp_path
    context.user_data["montage_video_name"] = suggested_name
    context.user_data["awaiting_montage_text"] = True
    await update.message.reply_text("أرسل الآن النص الذي تريد تحريكه على الفيديو.")


async def handle_text_v2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_admin_password"):
        value = update.message.text.strip()
        state = load_admin_state()
        if value != str(state.get("admin_password", ADMIN_PASSWORD)):
            await update.message.reply_text("كلمة السر غير صحيحة.")
            return

        context.user_data.pop("awaiting_admin_password", None)
        context.user_data["role"] = "admin"
        templates = get_templates(context, force_reload=True)
        await update.message.reply_text("تم تسجيلك كمدير.")
        await update.message.reply_text(
            admin_status_text(state, templates),
            reply_markup=admin_menu_keyboard(),
        )
        return

    if context.user_data.get("awaiting_montage_text"):
        video_path = context.user_data.get("montage_video_path")
        if not video_path or not os.path.isfile(video_path):
            clear_montage_state(context)
            await update.message.reply_text("أرسل الفيديو من جديد أولاً.")
            return
        text = clean_text_safe(update.message.text) or update.message.text.strip()
        if not text:
            await update.message.reply_text("أرسل النص فقط.")
            return
        try:
            await send_rendered_montage(update, context, video_path, text)
        except Exception as e:
            await update.message.reply_text(f"صار خطأ أثناء المونتاج: {e}")
            clear_montage_state(context)
        return

    clear_obsolete_auth_state(context)

    if context.user_data.get("awaiting_new_template_name"):
        if context.user_data.get("role") != "admin":
            reset_design_state(context)
            await show_start_menu(update.message, context)
            return

        template_name = clean_text_safe(update.message.text) or update.message.text.strip()
        if not template_name:
            await update.message.reply_text("أرسل اسم واضح للقالب.")
            return

        context.user_data["pending_template_name"] = template_name
        context.user_data.pop("awaiting_new_template_name", None)
        context.user_data["awaiting_new_template_image"] = True
        await update.message.reply_text(
            "أرسل الآن صورة القالب الجديدة. الأفضل إرسالها كملف للحفاظ على الجودة والشفافية."
        )
        return

    if context.user_data.get("awaiting_max_employees"):
        if context.user_data.get("role") != "admin":
            reset_design_state(context)
            await show_start_menu(update.message, context)
            return

        value = update.message.text.strip()
        if not value.isdigit():
            await update.message.reply_text("أرسل رقم صحيح فقط.")
            return

        state = load_admin_state()
        state["max_employees"] = int(value)
        if state["max_employees"] > 0:
            state["employee_ids"] = state.get("employee_ids", [])[: state["max_employees"]]
        save_admin_state(state)
        context.user_data.pop("awaiting_max_employees", None)
        templates = get_templates(context)
        await update.message.reply_text(
            admin_status_text(state, templates),
            reply_markup=admin_menu_keyboard(),
        )
        return

    if context.user_data.get("awaiting_stat_number"):
        value = clean_text_safe(update.message.text) or update.message.text.strip()
        if not value:
            await update.message.reply_text("أرسل الرقم فقط.")
            return
        context.user_data["pending_stat_number"] = value
        context.user_data.pop("awaiting_stat_number", None)
        templates = get_templates(context)
        state = load_admin_state()
        available_templates = get_enabled_templates(templates, state, context.user_data.get("role"))
        template_cfg = get_template_cfg(available_templates, context.user_data.get("template_id"))
        fixed_stat_word = str(template_cfg.get("stat_word_fixed_text", "") or "").strip()
        if fixed_stat_word:
            context.user_data["pending_stat_word"] = fixed_stat_word
            context.user_data["awaiting_stat_body"] = True
        else:
            context.user_data["awaiting_stat_word"] = True
        if fixed_stat_word:
            await update.message.reply_text(
                "\u0623\u0631\u0633\u0644 \u0627\u0644\u0622\u0646 \u0627\u0644\u062c\u0645\u0644\u0629 \u0627\u0644\u062a\u064a "
                "\u0633\u062a\u0638\u0647\u0631 \u0628\u0627\u0644\u0644\u0648\u0646 \u0627\u0644\u0623\u0628\u064a\u0636 \u062a\u062d\u062a."
            )
            return
        if fixed_stat_word:
            await update.message.reply_text("Ø£Ø±Ø³Ù„ Ø§Ù„Ø¢Ù† Ø§Ù„Ø¬Ù…Ù„Ø© Ø§Ù„ØªÙŠ Ø³ØªØ¸Ù‡Ø± Ø¨Ø§Ù„Ù„ÙˆÙ† Ø§Ù„Ø£Ø¨ÙŠØ¶ ØªØ­Øª.")
            return
        await update.message.reply_text("أرسل الآن الكلمة التي ستظهر داخل الصندوق الأزرق.")
        return

    if context.user_data.get("awaiting_stat_word"):
        value = clean_text_safe(update.message.text) or update.message.text.strip()
        if not value:
            await update.message.reply_text("أرسل الكلمة فقط.")
            return
        context.user_data["pending_stat_word"] = value
        context.user_data.pop("awaiting_stat_word", None)
        context.user_data["awaiting_stat_body"] = True
        await update.message.reply_text("أرسل الآن الجملة التي ستظهر باللون الأبيض تحت.")
        return

    if context.user_data.get("awaiting_stat_body"):
        if "news_img" not in context.user_data:
            clear_stat_prompt_state(context)
            await update.message.reply_text("أرسل الصورة من جديد أولاً.")
            return
        if "template_id" not in context.user_data:
            clear_stat_prompt_state(context)
            await update.message.reply_text("اختر القالب أولاً من /start أو /templates.")
            return

        body = clean_text_safe(update.message.text) or update.message.text.strip()
        if not body:
            await update.message.reply_text("أرسل الجملة فقط.")
            return

        templates = get_templates(context)
        state = load_admin_state()
        available_templates = get_enabled_templates(templates, state, context.user_data.get("role"))
        template_cfg = get_template_cfg(available_templates, context.user_data.get("template_id"))
        img = context.user_data["news_img"]
        stat_text = " | ".join(
            [
                str(context.user_data.get("pending_stat_number", "")).strip(),
                str(context.user_data.get("pending_stat_word", "")).strip(),
                body.strip(),
            ]
        )
        try:
            await send_rendered_post(update, context, template_cfg, img, stat_text)
        except Exception as e:
            await update.message.reply_text(f"صار خطأ أثناء التصميم: {e}")
            reset_design_state(context)
        finally:
            clear_stat_prompt_state(context)
        return

    if "news_img" not in context.user_data:
        return

    if "template_id" not in context.user_data:
        await update.message.reply_text("اختر قالب أولاً من /start أو /templates.")
        return

    templates = get_templates(context)
    state = load_admin_state()
    available_templates = get_enabled_templates(templates, state, context.user_data.get("role"))
    template_cfg = get_template_cfg(available_templates, context.user_data.get("template_id"))

    if bool(template_cfg.get("requires_name", False)):
        text = clean_text_safe(update.message.text)
        img = context.user_data["news_img"]
        try:
            await send_rendered_post(update, context, template_cfg, img, text)
        except Exception as e:
            await update.message.reply_text(f"صار خطأ أثناء التصميم: {e}")
            reset_design_state(context)
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
        reset_design_state(context)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set in .env")
    try:
        acquire_bot_lock()
    except BotAlreadyRunningError as exc:
        safe_console_print(str(exc))
        return
    while True:
        builder = (
            Application.builder()
            .token(BOT_TOKEN)
            .read_timeout(60)
            .write_timeout(60)
            .connect_timeout(30)
            .pool_timeout(30)
        )

        if TELEGRAM_PROXY_URL:
            builder = builder.proxy(TELEGRAM_PROXY_URL).get_updates_proxy(TELEGRAM_PROXY_URL)
        if TELEGRAM_BASE_URL:
            builder = builder.base_url(TELEGRAM_BASE_URL)
        if TELEGRAM_BASE_FILE_URL:
            builder = builder.base_file_url(TELEGRAM_BASE_FILE_URL)

        app = builder.build()

        app.add_handler(CommandHandler("start", start_v2))
        app.add_handler(CommandHandler("templates", templates_cmd_v2))
        app.add_handler(CallbackQueryHandler(role_cb_v2, pattern=r"^role:"))
        app.add_handler(CallbackQueryHandler(mode_cb_v2, pattern=r"^mode:"))
        app.add_handler(CallbackQueryHandler(admin_menu_cb_v2, pattern=r"^admin:"))
        app.add_handler(CallbackQueryHandler(nav_cb_v2, pattern=r"^nav:"))
        app.add_handler(CallbackQueryHandler(admin_template_toggle_cb_v2, pattern=r"^admin_tpl:"))
        app.add_handler(CallbackQueryHandler(admin_template_delete_cb_v2, pattern=r"^admin_tpl_del:"))
        app.add_handler(CallbackQueryHandler(admin_template_delete_confirm_cb_v2, pattern=r"^admin_tpl_del_confirm:"))
        app.add_handler(CallbackQueryHandler(choose_template_cb_v2, pattern=r"^tpl:"))
        app.add_handler(MessageHandler(filters.VIDEO, handle_video_v2))
        app.add_handler(MessageHandler(filters.PHOTO, handle_photo_v2))
        app.add_handler(MessageHandler(filters.Document.ALL, handle_image_document_v2))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_v2))

        print("BASE_DIR:", BASE_DIR)
        print("TEMPLATES_DIR:", TEMPLATES_DIR)
        print("OS:", os.name)
        print("PILLOW_HAS_RAQM:", PILLOW_HAS_RAQM)
        if TELEGRAM_PROXY_URL:
            print("TELEGRAM_PROXY_URL: configured")
        if TELEGRAM_BASE_URL:
            print("TELEGRAM_BASE_URL:", TELEGRAM_BASE_URL)
        print("Bot is running...")
        try:
            app.run_polling(close_loop=False)
            return
        except Exception as exc:
            message = build_startup_error_message(exc)
            safe_console_print(message)
            write_startup_error_log(message, exc)
            if isinstance(exc, (Conflict, InvalidToken)):
                raise
            retry_message = (
                f"Retrying bot startup in {STARTUP_RETRY_SECONDS} seconds. "
                "Set TELEGRAM_PROXY_URL in .env if this server needs a proxy."
            )
            safe_console_print(retry_message)
            time.sleep(STARTUP_RETRY_SECONDS)


if __name__ == "__main__":
    main()
