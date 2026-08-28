"""Generate icon.ico from Ball.svg (falls back to ball.png, then procedural).

Outputs icon.png (used as the window icon at runtime) and icon.ico (used by
PyInstaller for the .exe file icon in Explorer).
"""

import os
import re
import tempfile

from PIL import Image


# Palette override — recolor Ball.svg to match the 5-color scheme
PALETTE = {
    "#FFED00": "#E9D985",   # yellow → gold
    "#393185": "#222E50",   # purple → indigo
}
BG_LIGHT = (0xE9, 0xD9, 0x85)   # gold backing behind the ball


def _inline_svg_fills(svg_text: str) -> str:
    """SDL_image's SVG backend ignores CSS classes — inline .filN → fill=… ."""
    fills = dict(re.findall(r"\.(\w+)\s*\{\s*fill\s*:\s*(#[0-9A-Fa-f]{3,8})", svg_text))
    if not fills:
        return svg_text
    fills = {cls: PALETTE.get(color.upper(), color) for cls, color in fills.items()}
    def repl(m):
        cls = m.group(1)
        return f'fill="{fills[cls]}"' if cls in fills else m.group(0)
    return re.sub(r'class\s*=\s*"([^"]+)"', repl, svg_text)

BASE = os.path.dirname(os.path.abspath(__file__))
SVG_PATH = os.path.join(BASE, "Ball.svg")
PNG_PATH = os.path.join(BASE, "ball.png")
ICON_PNG = os.path.join(BASE, "icon.png")
ICON_ICO = os.path.join(BASE, "icon.ico")

SIZE = 256
PAD = 8


def _from_pygame_surface(surf) -> Image.Image:
    import pygame
    raw = pygame.image.tobytes(surf, "RGBA")
    return Image.frombytes("RGBA", surf.get_size(), raw)


img: Image.Image | None = None

# 1) Prefer Ball.svg — rasterized natively by pygame-ce at exact target size
if os.path.exists(SVG_PATH):
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    import pygame
    pygame.init()
    pygame.display.set_mode((16, 16))
    try:
        from pygame import gfxdraw
        inner = SIZE - 2 * PAD
        with open(SVG_PATH, "r", encoding="utf-8") as f:
            svg_text = _inline_svg_fills(f.read())
        with tempfile.NamedTemporaryFile(mode="w", suffix=".svg", encoding="utf-8",
                                         delete=False) as tmp:
            tmp.write(svg_text)
            tmp_path = tmp.name
        try:
            surf = pygame.image.load_sized_svg(tmp_path, (inner, inner))
        finally:
            try: os.unlink(tmp_path)
            except OSError: pass
        canvas = pygame.Surface((SIZE, SIZE), pygame.SRCALPHA)
        r = inner // 2
        cx = cy = SIZE // 2
        gfxdraw.filled_circle(canvas, cx, cy, r, BG_LIGHT + (255,))
        gfxdraw.aacircle(canvas, cx, cy, r, BG_LIGHT + (255,))
        canvas.blit(surf, ((SIZE - inner) // 2, (SIZE - inner) // 2))
        img = _from_pygame_surface(canvas)
    finally:
        pygame.quit()

# 2) Fallback — bundled PNG resized with Pillow
if img is None and os.path.exists(PNG_PATH):
    src = Image.open(PNG_PATH).convert("RGBA")
    inner = SIZE - 2 * PAD
    src.thumbnail((inner, inner), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    canvas.paste(src, ((SIZE - src.width) // 2, (SIZE - src.height) // 2), src)
    img = canvas

# 3) Last resort — procedural ball from the game code
if img is None:
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    import pygame
    import volleyball as v
    pygame.init()
    surf = pygame.Surface((SIZE, SIZE), pygame.SRCALPHA)
    orig_r = v.BALL_R
    v.BALL_R = (SIZE - 2 * PAD) // 2
    try:
        ball = v.Ball(x=SIZE / 2, y=SIZE / 2, spin=0.4)
        v.draw_ball(surf, ball)
    finally:
        v.BALL_R = orig_r
    img = _from_pygame_surface(surf)
    pygame.quit()

img.save(ICON_PNG)
img.save(ICON_ICO, sizes=[(s, s) for s in (16, 32, 48, 64, 128, 256)])
print(f"wrote {ICON_ICO} ({os.path.getsize(ICON_ICO)} bytes)")
