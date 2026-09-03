"""Icons and menu bar image renderer for claude-usage plugin.

Provides:
- get_dropdown_icon_base64(name, theme=None): returns base64 PNG string (32x32 @ 144 DPI)
- render_menubar_image(segments, prefix="", suffix="", theme=None): returns base64 PNG
  rendering real icons, larger font numbers (25px), and hybrid-colored text with
  high-contrast red pill badges for burning quotas.
- is_dark_mode(): detects macOS dark mode
- HAS_PIL: boolean indicating Pillow availability
"""

import base64
import io
import os
import subprocess
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

ICONS_DIR = Path(__file__).resolve().parent / "icons"
_CACHE_B64 = {}

# Brand colors for weekly percentage numbers (tuned for high contrast on macOS menu bar)
BRAND_COLORS = {
    "dark": {
        "claude": (255, 185, 95),       # Warm glowing amber-orange / apricot (#FEB95F)
        "codex": (52, 211, 153),        # OpenAI vibrant mint emerald (#34D399)
        "kimi": (125, 211, 252),        # Moonshot bright ice cyan (#7DD3FC)
        "gemini": (220, 185, 255),      # Google Gemini bright lavender lilac (#DCB9FF)
        "antigravity": (220, 185, 255),
        "grok": (255, 255, 255),        # xAI crisp white
    },
    "light": {
        "claude": (196, 65, 30),        # Deep rich terracotta
        "codex": (5, 140, 95),          # Deep forest emerald
        "kimi": (2, 120, 190),          # Deep sky blue
        "gemini": (115, 45, 220),       # Deep rich purple
        "antigravity": (115, 45, 220),
        "grok": (25, 30, 40),           # Deep charcoal
    },
}

# Health status colors for projected end-of-week pace numbers
HEALTH_COLORS = {
    "dark": {
        "ok": (74, 222, 128),           # Vibrant lime green (#4ADE80)
        "warn": (253, 224, 71),         # Bright gold yellow (#FDE047)
        "bad": (225, 29, 72),           # Crimson red badge (#E11D48)
        "dim": (210, 225, 240, 200),    # Soft neutral separator dot
    },
    "light": {
        "ok": (22, 163, 74),            # Forest green (#16A34A)
        "warn": (202, 138, 4),          # Dark amber (#CA8A04)
        "bad": (220, 38, 38),           # Crimson red badge
        "dim": (90, 105, 120, 200),
    },
}


def is_dark_mode():
    try:
        proc = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"],
            capture_output=True, text=True, timeout=1
        )
        return proc.stdout.strip() == "Dark"
    except Exception:
        return True


def _get_font(size=25, bold=True):
    if bold:
        ttc_path = "/System/Library/Fonts/HelveticaNeue.ttc"
        if os.path.exists(ttc_path):
            try:
                return ImageFont.truetype(ttc_path, size, index=1)
            except Exception:
                pass
        arial_bold = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
        if os.path.exists(arial_bold):
            try:
                return ImageFont.truetype(arial_bold, size)
            except Exception:
                pass

    font_paths = [
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/SFCompact.ttf",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
    ]
    for fp in font_paths:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                pass
    return ImageFont.load_default()


def get_icon_image(name, theme=None, size=(32, 32)):
    if not HAS_PIL:
        return None
    if theme is None:
        theme = "dark" if is_dark_mode() else "light"

    # Try 32x32 pre-rendered icon first if size is (32, 32)
    p = None
    if size == (32, 32):
        p_pre = ICONS_DIR / "32" / theme / f"{name}.png"
        if p_pre.exists():
            p = p_pre

    if not p or not p.exists():
        for candidate in [
            ICONS_DIR / theme / f"{name}.png",
            ICONS_DIR / f"{name}.png",
            ICONS_DIR / "32" / theme / f"{name}.png",
        ]:
            if candidate.exists():
                p = candidate
                break

    if not p or not p.exists():
        return None

    try:
        im = Image.open(p).convert("RGBA")
        if size and im.size != size:
            im = im.resize(size, Image.Resampling.LANCZOS)
        return im
    except Exception:
        return None


def get_dropdown_icon_base64(name, theme=None):
    """Return base64 string of 32x32 Retina PNG icon for dropdown menu items."""
    if theme is None:
        theme = "dark" if is_dark_mode() else "light"

    cache_key = (name, theme)
    if cache_key in _CACHE_B64:
        return _CACHE_B64[cache_key]

    pre_path = ICONS_DIR / "32" / theme / f"{name}.png"
    if pre_path.exists():
        try:
            b64 = base64.b64encode(pre_path.read_bytes()).decode("ascii")
            _CACHE_B64[cache_key] = b64
            return b64
        except Exception:
            pass

    if not HAS_PIL:
        return ""

    im = get_icon_image(name, theme=theme, size=(32, 32))
    if not im:
        return ""

    try:
        buf = io.BytesIO()
        im.save(buf, format="PNG", dpi=(144, 144))
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        _CACHE_B64[cache_key] = b64
        return b64
    except Exception:
        return ""


def render_menubar_image(segments, prefix="", suffix="", theme=None):
    """
    Render a crisp Retina image strip containing real brand icons and hybrid-colored text:
    - Font size 25px for prominent legibility
    - Weekly % is colored in the provider's distinct brand color
    - Projected pace % is colored in health status color (Green / Yellow)
    - Burning fast pace (>100%) is enclosed in a high-contrast crimson pill badge with pure white text
    """
    if not HAS_PIL or not segments:
        return None

    if theme is None:
        theme = "dark" if is_dark_mode() else "light"

    # macOS menu bar height: 22pt = 44px @ 2x Retina
    height = 44
    icon_size = 28  # 14pt @ 2x

    font = _get_font(25, bold=True)
    label_font = _get_font(16, bold=True)

    fg_default = (255, 255, 255, 245) if theme == "dark" else (20, 20, 20, 245)
    dim_color = HEALTH_COLORS[theme]["dim"]
    shadow_col = (0, 0, 0, 180) if theme == "dark" else (255, 255, 255, 140)

    dummy = Image.new("RGBA", (1, 1))
    draw_dummy = ImageDraw.Draw(dummy)

    elements = []
    cur_x = 0

    # Prefix (e.g. ⚠️)
    if prefix:
        bbox = draw_dummy.textbbox((0, 0), prefix, font=font)
        pw = bbox[2] - bbox[0]
        elements.append(("text", prefix, cur_x, font, fg_default, True))
        cur_x += pw + 6

    for seg in segments:
        icon_name = seg.get("icon")
        brand_col = BRAND_COLORS[theme].get(icon_name, fg_default)

        # 1. Icon
        im = get_icon_image(icon_name, theme=theme, size=(icon_size, icon_size))
        if im:
            elements.append(("icon", im, cur_x, (height - icon_size) // 2))
            cur_x += icon_size + 3

        # 2. Sub-label (e.g. '3P')
        sub_label = seg.get("label")
        if sub_label:
            bbox = draw_dummy.textbbox((0, 0), sub_label, font=label_font)
            lw = bbox[2] - bbox[0]
            lh = bbox[3] - bbox[1]
            ly = (height - lh) // 2
            elements.append(("text", sub_label, cur_x, label_font, dim_color, True))
            cur_x += lw + 2

        # 3. Determine weekly_text and pace_text
        weekly_text = seg.get("weekly_text")
        pace_text = seg.get("pace_text")
        weekly_pct = seg.get("weekly_pct")
        pace_pct = seg.get("pace_pct")

        # Parse text field if separate fields are not passed
        if weekly_text is None:
            raw_text = seg.get("text", "")
            if "·" in raw_text:
                parts = raw_text.split("·", 1)
                weekly_text = parts[0]
                pace_text = parts[1]
            else:
                weekly_text = raw_text
                pace_text = None

        # Determine health status
        is_bad = False
        if weekly_pct is not None and weekly_pct >= 100:
            health_col = HEALTH_COLORS[theme]["bad"]
            is_bad = True
        elif pace_pct is not None:
            if pace_pct <= 100:
                health_col = HEALTH_COLORS[theme]["ok"]
            elif pace_pct <= 110:
                health_col = HEALTH_COLORS[theme]["warn"]
            else:
                health_col = HEALTH_COLORS[theme]["bad"]
                is_bad = True
        else:
            health_col = dim_color

        # 4. Weekly % text in brand color
        if weekly_text:
            cur_x += 2
            bbox = draw_dummy.textbbox((0, 0), weekly_text, font=font)
            tw = bbox[2] - bbox[0]
            elements.append(("text", weekly_text, cur_x, font, brand_col, True))
            cur_x += tw

        # 5. Separator dot and pace text in health color or red pill badge
        if pace_text:
            dot = "·"
            bbox_dot = draw_dummy.textbbox((0, 0), dot, font=font)
            dw = bbox_dot[2] - bbox_dot[0]
            elements.append(("text", dot, cur_x + 1, font, dim_color, False))
            cur_x += dw + 3

            bbox_p = draw_dummy.textbbox((0, 0), pace_text, font=font)
            pw = bbox_p[2] - bbox_p[0]

            if is_bad:
                # Red pill badge with white text for maximum legibility on any wallpaper
                badge_bg = (225, 29, 72, 240) if theme == "dark" else (220, 38, 38, 240)
                elements.append(("badge", pace_text, cur_x, font, badge_bg, (255, 255, 255)))
                cur_x += pw + 14
            else:
                elements.append(("text", pace_text, cur_x, font, health_col, True))
                cur_x += pw

        cur_x += 16  # Inter-provider spacing

    if suffix:
        bbox = draw_dummy.textbbox((0, 0), suffix, font=font)
        sw = bbox[2] - bbox[0]
        elements.append(("text", suffix, cur_x, font, dim_color, True))
        cur_x += sw + 4

    if cur_x <= 0:
        return None

    width = cur_x - 6
    strip = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(strip)

    for item in elements:
        kind = item[0]
        if kind == "icon":
            _, im, x, y = item
            strip.paste(im, (x, y), im)
        elif kind == "text":
            _, txt, x, f, col, has_shadow = item
            bbox = draw_dummy.textbbox((0, 0), txt, font=f)
            th = bbox[3] - bbox[1]
            y = (height - th) // 2 - 2
            if has_shadow:
                draw.text((x + 1, y + 1), txt, font=f, fill=shadow_col)
            draw.text((x, y), txt, font=f, fill=col)
        elif kind == "badge":
            _, txt, x, f, bg_badge, fg_badge = item
            bbox = draw_dummy.textbbox((0, 0), txt, font=f)
            pw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            y = (height - th) // 2 - 2
            draw.rounded_rectangle([x + 1, y - 2, x + pw + 9, y + th + 4], radius=6, fill=bg_badge)
            draw.text((x + 5, y), txt, font=f, fill=fg_badge)

    try:
        buf = io.BytesIO()
        strip.save(buf, format="PNG", dpi=(144, 144))
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None
