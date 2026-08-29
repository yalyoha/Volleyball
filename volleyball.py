"""Beach Slime Volleyball — pygame-ce.

Two-player local volleyball with universal gamepad and keyboard support.
Gamepads use SDL GameController API (Xbox, PlayStation, Switch Pro, F310, etc.).
Run:  python volleyball.py
"""

from __future__ import annotations

import array
import json
import math
import os
import random
import re
import sys
import tempfile
from dataclasses import dataclass, field

import pygame
from pygame import gfxdraw
from pygame._sdl2 import controller as sdl_controller

# ---------- Configuration ----------

# Native 16:9 resolution. All px-based constants below are derived from these
# so the game re-scales cleanly if you change WIDTH/HEIGHT (keep the ratio).
WIDTH, HEIGHT = 1920, 1080
FPS = 60
SCALE = HEIGHT / 640.0                 # baseline was 640-tall

def _s(v: float) -> int:
    return int(round(v * SCALE))

GROUND_Y = _s(520)         # top of the sand where slimes stand
NET_X = WIDTH // 2
NET_TOP_Y = _s(356)        # net top Y — reduced net height by 20% for more dynamic rallies
NET_WIDTH = _s(6)

SLIME_W = _s(160)          # visual body width — wide flat dome, 4:1 ratio
SLIME_H = _s(40)           # visual body height (dome apex above the foot)
BALL_R = _s(18)

GRAVITY = 1400.0 * SCALE       # px/s^2
MOVE_SPEED = 480.0 * SCALE     # slime horizontal speed
JUMP_VELOCITY = -720.0 * SCALE
BALL_MAX_SPEED = 1100.0 * SCALE
BALL_BOUNCE_DAMP = 0.95
BALL_HIT_BOOST = 1.08

# Jelly slime deformation — soft style: ±15% amplitude cap, ~0.3s return time,
# mild single overshoot. Squish > 0 means squashed (wider, shorter dome).
SQUISH_CAP = 0.15
SQUISH_K = 400.0                        # spring stiffness (ω ≈ 20 rad/s)
SQUISH_C = 16.0                         # damping (ζ ≈ 0.4 — light bounce)
SQUISH_JUMP = 0.10                      # anticipation stretch on jump
SQUISH_PER_LAND_VY = 1.2e-4             # squash per unit of landing vy
SQUISH_PER_HIT_DOT = 1.5e-4             # squash per unit of ball impact normal speed
# Tournament ladder:
# BALLS_PER_GAME balls per game → 1 big star,
# STARS_PER_MATCH big stars per match → 1 small star,
# STARS_PER_TOURNAMENT small stars → tournament won.
BALLS_PER_GAME = 5
STARS_PER_MATCH = 3
STARS_PER_TOURNAMENT = 5
SERVE_DELAY_MS = 800
STAR_TOAST_MS = 1400          # how long the "player earned a star" overlay stays on screen
NAME_MAX_LEN = 12
DEFAULT_NAMES = ("Игрок 1", "Игрок 2")

# ---- 5-color minimalist palette ----
INDIGO   = (0x22, 0x2E, 0x50)     # Space Indigo — net, ball dark, digits, text
CERULEAN = (0x00, 0x79, 0x91)     # Cerulean — Player 2
SEAGRASS = (0x43, 0x9A, 0x86)     # Seagrass — Player 1
CELADON  = (0xBC, 0xD8, 0xC1)     # Celadon — background (sky + sea unified)
GOLD     = (0xE9, 0xD9, 0x85)     # Light Gold — sand + ball light

# Semantic aliases used throughout the game
BG_COLOR   = CELADON
SAND       = GOLD
NET_COLOR  = INDIGO
SCORE_COLOR = INDIGO
P1_COLOR   = SEAGRASS
P2_COLOR   = CERULEAN
BALL_LIGHT = GOLD       # SVG panel color 1 (was yellow)
BALL_DARK  = INDIGO     # SVG panel color 2 (was purple) + ball body backing

# Universal gamepad mappings via SDL GameController API.
# Physical layouts differ per vendor, but SDL exposes logical buttons/axes.
BTN_A = pygame.CONTROLLER_BUTTON_A
BTN_B = pygame.CONTROLLER_BUTTON_B
BTN_START = pygame.CONTROLLER_BUTTON_START
BTN_BACK = pygame.CONTROLLER_BUTTON_BACK
BTN_DPAD_LEFT = pygame.CONTROLLER_BUTTON_DPAD_LEFT
BTN_DPAD_RIGHT = pygame.CONTROLLER_BUTTON_DPAD_RIGHT
AXIS_LX = pygame.CONTROLLER_AXIS_LEFTX
AXIS_MAX = 32767.0
STICK_DEADZONE = 0.25


# ---------- Anti-aliased draw helpers ----------


def aa_polygon(surface: pygame.Surface, color: tuple, pts) -> None:
    ipts = [(int(round(x)), int(round(y))) for x, y in pts]
    gfxdraw.filled_polygon(surface, ipts, color)
    gfxdraw.aapolygon(surface, ipts, color)


def aa_circle(surface: pygame.Surface, color: tuple, center, radius: int) -> None:
    cx = int(round(center[0]))
    cy = int(round(center[1]))
    r = int(radius)
    gfxdraw.filled_circle(surface, cx, cy, r, color)
    gfxdraw.aacircle(surface, cx, cy, r, color)


def aa_circle_outline(surface: pygame.Surface, color: tuple, center, radius: int) -> None:
    cx = int(round(center[0]))
    cy = int(round(center[1]))
    r = int(radius)
    gfxdraw.aacircle(surface, cx, cy, r, color)


def aa_ellipse(surface: pygame.Surface, color: tuple, cx: float, cy: float, rx: int, ry: int) -> None:
    icx, icy = int(round(cx)), int(round(cy))
    gfxdraw.filled_ellipse(surface, icx, icy, rx, ry, color)
    gfxdraw.aaellipse(surface, icx, icy, rx, ry, color)


SUPERSAMPLE = 4    # 4× render then downsample for creamy sprite edges


def supersampled(w: int, h: int, draw_fn) -> pygame.Surface:
    """Render draw_fn(big_surface, scale) into a supersampled RGBA surface and
    smoothscale it down to (w, h) for high-quality edge anti-aliasing."""
    s = SUPERSAMPLE
    big = pygame.Surface((w * s, h * s), pygame.SRCALPHA)
    draw_fn(big, s)
    return pygame.transform.smoothscale(big, (w, h))


# ---------- Data classes ----------


GROUND_FOOT_Y = (GROUND_Y + HEIGHT) // 2   # slime foot / ball death line — middle of the sand


@dataclass
class Slime:
    """One player: a wide flat dome (like classic slime volleyball)."""

    x: float
    color: tuple
    left_bound: float
    right_bound: float
    y: float = float(GROUND_FOOT_Y)   # foot Y — the flat bottom line of the dome
    vx: float = 0.0
    vy: float = 0.0
    on_ground: bool = True
    # Jelly deformation. squish > 0 = squashed (wider, shorter),
    # squish < 0 = stretched (narrower, taller). Springs back to 0 in update().
    squish: float = 0.0
    squish_v: float = 0.0

    def update(self, dt: float, move: float, jump_pressed: bool) -> None:
        self.vx = move * MOVE_SPEED
        if jump_pressed and self.on_ground:
            self.vy = JUMP_VELOCITY
            self.on_ground = False
            self.squish = -SQUISH_JUMP        # anticipation stretch
            self.squish_v = 0.0
            sfx_play("jump", volume=0.7)
        self.vy += GRAVITY * dt
        self.x += self.vx * dt
        self.y += self.vy * dt
        if self.y >= GROUND_FOOT_Y:
            landing_vy = self.vy
            self.y = GROUND_FOOT_Y
            self.vy = 0.0
            if not self.on_ground:
                self.on_ground = True
                self.squish += min(SQUISH_CAP, max(0.0, landing_vy * SQUISH_PER_LAND_VY))
                if landing_vy > 200.0:
                    sfx_play("land", volume=min(1.0, landing_vy / 1400.0))
                    emit_dust(self.x, GROUND_FOOT_Y, landing_vy)
        half_w = SLIME_W / 2
        self.x = max(self.left_bound + half_w, min(self.right_bound - half_w, self.x))

        # Damped spring pulling squish back to 0
        self.squish_v += (-SQUISH_K * self.squish - SQUISH_C * self.squish_v) * dt
        self.squish   += self.squish_v * dt
        if self.squish >  SQUISH_CAP: self.squish =  SQUISH_CAP; self.squish_v = 0.0
        if self.squish < -SQUISH_CAP: self.squish = -SQUISH_CAP; self.squish_v = 0.0


@dataclass
class Ball:
    x: float = WIDTH * 0.25
    y: float = 120.0
    vx: float = 0.0
    vy: float = 0.0
    spin: float = 0.0               # visual rotation, radians
    frozen: bool = True             # true during serve delay
    prev_x: float = WIDTH * 0.25    # x at start of the current physics step (for swept net collision)
    trail: list = field(default_factory=list)   # recent (x, y) for motion-blur trail

    def update(self, dt: float) -> None:
        self.prev_x = self.x
        if self.frozen:
            self.trail.clear()
            return
        self.vy += GRAVITY * dt
        speed = math.hypot(self.vx, self.vy)
        if speed > BALL_MAX_SPEED:
            self.vx *= BALL_MAX_SPEED / speed
            self.vy *= BALL_MAX_SPEED / speed
        self.x += self.vx * dt
        self.y += self.vy * dt
        self.spin += self.vx * dt * 0.01
        # Record trail only when moving noticeably — no trail on slow drops
        if speed > 200.0:
            self.trail.append((self.x, self.y))
            if len(self.trail) > 8:
                self.trail.pop(0)
        elif self.trail:
            self.trail.pop(0)


@dataclass
class Player:
    slime: Slime
    name: str = "Игрок"
    balls: int = 0            # rallies won in the current game (0..BALLS_PER_GAME-1)
    game_stars: int = 0       # games won in the current match (0..STARS_PER_MATCH-1)
    match_stars: int = 0      # matches won in the tournament (0..STARS_PER_TOURNAMENT)
    controller_index: int | None = None
    keys: dict = field(default_factory=dict)   # {'left':..., 'right':..., 'jump':...}

    def reset_tournament(self) -> None:
        self.balls = 0
        self.game_stars = 0
        self.match_stars = 0


# ---------- Input ----------


INPUT_AUTO = "auto"
INPUT_KEYBOARD = "keyboard"
INPUT_GAMEPAD = "gamepad"

MODE_DUO = "duo"
MODE_AI = "ai"

DIFF_EASY = "easy"
DIFF_MEDIUM = "medium"
DIFF_HARD = "hard"

# Per-difficulty AI tuning
_AI_PARAMS = {
    DIFF_EASY:   dict(reaction_ms=350, aim_noise_frac=0.35, jump_prob=0.15, smash_range=0.20),
    DIFF_MEDIUM: dict(reaction_ms=140, aim_noise_frac=0.15, jump_prob=0.55, smash_range=0.35),
    DIFF_HARD:   dict(reaction_ms=40,  aim_noise_frac=0.03, jump_prob=0.95, smash_range=0.50),
}


class AIState:
    """Per-slime AI state — remembers a delayed observation of the ball for reaction lag."""

    def __init__(self) -> None:
        self.buffer: list[tuple[int, float, float, float, float]] = []
        self.aim_noise: float = 0.0
        self.noise_seed: int = 0

    def sample_and_get_delayed(self, now_ms: int, ball, delay_ms: int):
        self.buffer.append((now_ms, ball.x, ball.y, ball.vx, ball.vy))
        cutoff = now_ms - delay_ms
        while len(self.buffer) > 1 and self.buffer[1][0] <= cutoff:
            self.buffer.pop(0)
        return self.buffer[0]


def _predict_ball_x_at(y_target: float, bx: float, by: float, bvx: float, bvy: float) -> float:
    """Return the ball's x when it next reaches y_target under gravity (ignores walls/net)."""
    if by >= y_target and bvy >= 0:
        return bx
    a = 0.5 * GRAVITY
    b = bvy
    c = by - y_target
    disc = b * b - 4 * a * c
    if disc < 0:
        return bx
    sq = math.sqrt(disc)
    t1 = (-b + sq) / (2 * a)
    t2 = (-b - sq) / (2 * a)
    t = max(t1, t2)
    if t <= 0:
        return bx
    return bx + bvx * t


def ai_input(state: AIState, slime: Slime, ball: Ball, difficulty: str, now_ms: int) -> tuple[float, bool]:
    p = _AI_PARAMS.get(difficulty, _AI_PARAMS[DIFF_MEDIUM])
    # Delayed observation of the ball to model reaction time
    _, bx, by, bvx, bvy = state.sample_and_get_delayed(now_ms, ball, p["reaction_ms"])

    is_right = slime.x > NET_X
    own_side = (bx >= NET_X) if is_right else (bx <= NET_X)

    if own_side:
        predicted_x = _predict_ball_x_at(GROUND_FOOT_Y, bx, by, bvx, bvy)
        # Smash offset — stand on our OWN-BACK side of the ball so contact
        # lands on the net-facing flank of the dome and the ball reflects
        # toward the opponent instead of straight up. Without this the AI
        # juggles the ball vertically on its own side forever.
        side_away_from_net = 1.0 if is_right else -1.0
        smash_offset = SLIME_W * 0.28 * side_away_from_net
        target = predicted_x + smash_offset
    else:
        # Ready position — a bit off-center on own half
        target = NET_X + (NET_X * 0.55 if is_right else -NET_X * 0.55)

    # Clamp to own half so AI doesn't try to cross the net
    if is_right:
        target = max(NET_X + SLIME_W * 0.6, min(WIDTH - SLIME_W * 0.5, target))
    else:
        target = max(SLIME_W * 0.5, min(NET_X - SLIME_W * 0.6, target))

    # Aim noise — refreshed occasionally so it doesn't jitter every frame
    if state.noise_seed != (now_ms // 300):
        state.noise_seed = now_ms // 300
        rng = (((state.noise_seed * 2654435761) & 0xFFFFFFFF) / 0xFFFFFFFF) * 2 - 1
        state.aim_noise = rng * SLIME_W * p["aim_noise_frac"]
    target += state.aim_noise

    dx = target - slime.x
    dead = SLIME_W * 0.10
    move = 0.0 if abs(dx) < dead else (1.0 if dx > 0 else -1.0)

    # Jump decision: only if ball is descending toward our side, near us, in smash range.
    jump = False
    if own_side and bvy > 0:
        horiz_ok = abs(bx - slime.x) < SLIME_W * (0.4 + p["smash_range"])
        # Predict height of ball when it reaches within reach of a jumping slime
        smash_zone_top = slime.y - SLIME_H - JUMP_VELOCITY * JUMP_VELOCITY / (2 * GRAVITY)
        smash_zone_bot = slime.y - SLIME_H
        vert_ok = smash_zone_top - _s(20) < by < smash_zone_bot + _s(40)
        if slime.on_ground and horiz_ok and vert_ok:
            # Deterministic pseudo-decision based on ball position, biased by jump_prob
            hash32 = ((int(bx) * 2654435761) ^ (now_ms // 100)) & 0xFFFF
            if (hash32 / 0xFFFF) < p["jump_prob"]:
                jump = True

    return move, jump


def read_player_input(player: Player, controllers: list, keys, input_mode: str) -> tuple[float, bool]:
    """Return (move_axis in [-1, 1], jump_pressed_this_frame_or_held).

    input_mode is one of INPUT_AUTO / INPUT_KEYBOARD / INPUT_GAMEPAD.
    """
    move = 0.0
    jump = False

    use_pad = input_mode != INPUT_KEYBOARD
    use_kb = input_mode != INPUT_GAMEPAD

    ctl = None
    if use_pad and player.controller_index is not None and player.controller_index < len(controllers):
        ctl = controllers[player.controller_index]

    if ctl is not None:
        try:
            lx = ctl.get_axis(AXIS_LX) / AXIS_MAX
        except pygame.error:
            lx = 0.0
        if abs(lx) > STICK_DEADZONE:
            move += lx
        try:
            if ctl.get_button(BTN_DPAD_LEFT):
                move -= 1.0
            if ctl.get_button(BTN_DPAD_RIGHT):
                move += 1.0
            if ctl.get_button(BTN_A):
                jump = True
        except pygame.error:
            pass

    if use_kb:
        if keys[player.keys.get("left", 0)]:
            move -= 1.0
        if keys[player.keys.get("right", 0)]:
            move += 1.0
        if keys[player.keys.get("jump", 0)]:
            jump = True

    return max(-1.0, min(1.0, move)), jump


# ---------- Rendering ----------


def render_background(surface: pygame.Surface) -> None:
    """Flat celadon background above the sand, gold sand below."""
    sand_top = GROUND_Y - _s(10)
    surface.fill(BG_COLOR, (0, 0, WIDTH, sand_top))
    surface.fill(SAND, (0, sand_top, WIDTH, HEIGHT - sand_top))


def draw_net(surface: pygame.Surface) -> None:
    # Pole extends from its cap all the way down to the play line
    pole_rect = pygame.Rect(NET_X - NET_WIDTH // 2, NET_TOP_Y, NET_WIDTH, GROUND_FOOT_Y - NET_TOP_Y)
    pygame.draw.rect(surface, NET_COLOR, pole_rect)
    aa_circle(surface, NET_COLOR, (NET_X, NET_TOP_Y), _s(6))


def _slime_polygon(cx: float, foot_y: float, w: float, h: float, samples: int = 24) -> list:
    """Points tracing the SVG dome: two cubic beziers on top, flat line at the bottom.

    Path matches Slime.svg exactly (scaled): right→top-center→left, then flat back.
    """
    left = cx - w / 2
    right = cx + w / 2
    top_y = foot_y - h

    def bezier(t, p0, p1, p2, p3):
        u = 1 - t
        return (
            u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0],
            u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1],
        )

    # Right curve: (right, foot_y) → (cx, top_y)
    p0 = (right, foot_y)
    p1 = (right,               foot_y - 0.5523 * h)
    p2 = (right - 0.2239 * w,  top_y)
    p3 = (cx, top_y)
    pts = [bezier(i / samples, p0, p1, p2, p3) for i in range(samples + 1)]

    # Left curve: (cx, top_y) → (left, foot_y)
    p0 = (cx, top_y)
    p1 = (cx - 0.2761 * w,     top_y)
    p2 = (left,                foot_y - 0.5523 * h)
    p3 = (left, foot_y)
    pts.extend(bezier(i / samples, p0, p1, p2, p3) for i in range(1, samples + 1))
    # Polygon auto-closes on the flat bottom line
    return pts


_slime_sprite_cache: dict = {}


def _get_slime_sprite(color: tuple) -> pygame.Surface:
    """Cached supersampled slime sprite. Dome centered horizontally, foot at bottom edge."""
    if color in _slime_sprite_cache:
        return _slime_sprite_cache[color]
    w, h = SLIME_W, SLIME_H

    def render(surf: pygame.Surface, s: int) -> None:
        pts = _slime_polygon(w * s / 2, h * s, w * s, h * s)
        pygame.draw.polygon(surf, color, pts)

    sprite = supersampled(w, h, render)
    _slime_sprite_cache[color] = sprite
    return sprite


def draw_slime(surface: pygame.Surface, slime: Slime) -> None:
    """Flat dome from Slime.svg — cached supersampled sprite for smooth edges.
    Non-uniform scale per frame applies the current jelly squish (foot stays anchored)."""
    sprite = _get_slime_sprite(slime.color)
    if abs(slime.squish) > 0.001:
        w, h = sprite.get_width(), sprite.get_height()
        new_w = max(1, int(round(w * (1.0 + slime.squish))))
        new_h = max(1, int(round(h * (1.0 - slime.squish))))
        sprite = pygame.transform.smoothscale(sprite, (new_w, new_h))
    rect = sprite.get_rect()
    rect.midbottom = (int(round(slime.x)), int(round(slime.y)))
    surface.blit(sprite, rect)


_ball_sprite_cache: dict = {}


def _inline_svg_fills(svg_text: str, palette_override: dict | None = None) -> str:
    """Replace ``class="filN"`` with inline ``fill="#RRGGBB"``.

    SDL_image's nanosvg backend does not resolve CSS <style> class selectors, so
    a CorelDRAW-exported SVG that only uses class-based fills renders as black.
    This walks the <style> block, extracts .filN → color mappings, and rewrites
    every ``class="filN"`` occurrence into an inline ``fill=`` attribute.

    ``palette_override`` maps a source color (``"#FFED00"``) to a replacement,
    letting us recolor a shipped SVG without editing it on disk.
    """
    fills = dict(re.findall(r"\.(\w+)\s*\{\s*fill\s*:\s*(#[0-9A-Fa-f]{3,8})", svg_text))
    if palette_override:
        override = {k.lower(): v for k, v in palette_override.items()}
        fills = {cls: override.get(color.lower(), color) for cls, color in fills.items()}
    if not fills:
        return svg_text

    def repl(m):
        cls = m.group(1)
        color = fills.get(cls)
        return f'fill="{color}"' if color else m.group(0)

    return re.sub(r'class\s*=\s*"([^"]+)"', repl, svg_text)


def _hex(rgb: tuple) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


BALL_SVG_PALETTE = {
    "#FFED00": _hex(BALL_LIGHT),   # yellow → gold
    "#393185": _hex(BALL_DARK),    # purple → indigo
}


def _get_ball_sprite(target: int | None = None) -> pygame.Surface | None:
    """Rasterize Ball.svg (with CSS fills inlined) to target×target. Cached per size."""
    if target is None:
        target = BALL_R * 2
    if target in _ball_sprite_cache:
        return _ball_sprite_cache[target]
    base_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    svg_path = os.path.join(base_dir, "Ball.svg")
    surf: pygame.Surface | None = None
    if os.path.exists(svg_path):
        try:
            with open(svg_path, "r", encoding="utf-8") as f:
                svg_text = _inline_svg_fills(f.read(), BALL_SVG_PALETTE)
            with tempfile.NamedTemporaryFile(mode="w", suffix=".svg", encoding="utf-8",
                                             delete=False) as tmp:
                tmp.write(svg_text)
                tmp_path = tmp.name
            try:
                surf = pygame.image.load_sized_svg(tmp_path, (target, target)).convert_alpha()
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except (pygame.error, AttributeError, OSError):
            surf = None
    if surf is None:
        png_path = os.path.join(base_dir, "ball.png")
        if os.path.exists(png_path):
            img = pygame.image.load(png_path).convert_alpha()
            surf = pygame.transform.smoothscale(img, (target, target))
    _ball_sprite_cache[target] = surf
    return surf


# --- Baked ball with rotation cache ---
# Bake the whole ball (dark ring + light interior + SVG pattern) once at high
# supersample, pre-rotate into N frames. Per-frame draw_ball becomes a plain
# blit — no per-frame rotozoom and no partially-AA gfxdraw circles.

SUPERSAMPLE_BALL = 6
BALL_ROT_FRAMES = 72

_ball_frame_cache: list = []


def _bake_ball_frames() -> list:
    """Bake N rotated ball frames. Idempotent."""
    if _ball_frame_cache:
        return _ball_frame_cache
    ss = SUPERSAMPLE_BALL
    big_size = BALL_R * 2 * ss
    big = pygame.Surface((big_size, big_size), pygame.SRCALPHA)
    center = big_size // 2
    ring_px = max(2, _s(2)) * ss              # ring thickness matches display scale
    pygame.draw.circle(big, BALL_DARK,  (center, center), big_size // 2)
    pygame.draw.circle(big, BALL_LIGHT, (center, center), big_size // 2 - ring_px)
    pattern = _get_ball_sprite(target=big_size)
    if pattern is not None:
        big.blit(pattern, (0, 0))
    for i in range(BALL_ROT_FRAMES):
        angle = i * (360.0 / BALL_ROT_FRAMES)
        _ball_frame_cache.append(pygame.transform.rotozoom(big, angle, 1.0 / ss))
    return _ball_frame_cache


def draw_ball(surface: pygame.Surface, ball: Ball) -> None:
    # Motion trail behind the ball — older = smaller & more transparent
    if ball.trail:
        trail_len = len(ball.trail)
        for i, (tx, ty) in enumerate(ball.trail):
            u = (i + 1) / trail_len            # 0..1, newer → higher
            alpha = int(70 * u)
            r = max(1, int(BALL_R * (0.35 + 0.4 * u)))
            surf = pygame.Surface((r * 2, r * 2), pygame.SRCALPHA)
            pygame.draw.circle(surf, (*BALL_LIGHT, alpha), (r, r), r)
            surface.blit(surf, (int(tx - r), int(ty - r)))
    frames = _bake_ball_frames()
    n = len(frames)
    angle_deg = (-math.degrees(ball.spin)) % 360.0
    idx = int(round(angle_deg * n / 360.0)) % n
    frame = frames[idx]
    rect = frame.get_rect(center=(int(round(ball.x)), int(round(ball.y))))
    surface.blit(frame, rect)


_score_cache: dict = {}


def _render_smooth_text(base_font_size: int, text: str, color: tuple, bold: bool = True) -> pygame.Surface:
    """Render font at 2× the base size, then smoothscale down for creamier AA."""
    key = ("arial", base_font_size, text, color, bold)
    cached = _score_cache.get(key)
    if cached is not None:
        return cached
    big_font = pygame.font.SysFont("arial", base_font_size * 2, bold=bold)
    big = big_font.render(text, True, color)
    small = pygame.transform.smoothscale(big, (big.get_width() // 2, big.get_height() // 2))
    _score_cache[key] = small
    return small


def draw_score(surface: pygame.Surface, font: pygame.font.Font, left: int, right: int) -> None:
    text = f"{left} - {right}"
    img = _render_smooth_text(_s(64), text, SCORE_COLOR)
    x = (WIDTH - img.get_width()) // 2
    surface.blit(img, (x, _s(20)))


_gear_sprite: pygame.Surface | None = None


def _get_gear_sprite() -> pygame.Surface:
    """Cached supersampled gear icon — solid gold, no border, no inner hub."""
    global _gear_sprite
    if _gear_sprite is not None:
        return _gear_sprite
    outer_r = _s(24)
    inner_r = _s(18)
    hub_r = _s(8)
    size = outer_r * 2 + 4
    fill = GOLD
    hole = CELADON
    teeth = 8

    def render(surf: pygame.Surface, s: int) -> None:
        cx = surf.get_width() // 2
        cy = surf.get_height() // 2
        or_ = outer_r * s
        ir_ = inner_r * s
        hr_ = hub_r * s
        pts = []
        step = math.pi / teeth
        for i in range(teeth * 2):
            r = or_ if i % 2 == 0 else ir_
            angle = -math.pi / 2 + i * step
            pts.append((cx + math.cos(angle) * r, cy + math.sin(angle) * r))
        pygame.draw.polygon(surf, fill, pts)
        # Punch out a background-colored circle in the middle (no rim around it)
        pygame.draw.circle(surf, hole, (cx, cy), hr_)

    _gear_sprite = supersampled(size, size, render)
    return _gear_sprite


def draw_gear_icon(surface: pygame.Surface) -> pygame.Rect:
    """Settings gear icon in the top-right corner. Returns its click rect."""
    sprite = _get_gear_sprite()
    cx, cy = WIDTH - _s(50), _s(50)
    rect = sprite.get_rect(center=(cx, cy))
    surface.blit(sprite, rect)
    return rect


def _star_points(cx: float, cy: float, r_outer: float, r_inner: float, tips: int = 5) -> list:
    pts = []
    for i in range(tips * 2):
        r = r_outer if i % 2 == 0 else r_inner
        angle = -math.pi / 2 + i * math.pi / tips
        pts.append((cx + math.cos(angle) * r, cy + math.sin(angle) * r))
    return pts


_star_sprite_cache: dict = {}


def _get_star_sprite(size_px: int, filled: bool) -> pygame.Surface:
    key = (size_px, filled)
    if key in _star_sprite_cache:
        return _star_sprite_cache[key]
    stroke = max(2, size_px // 12)

    def render(surf: pygame.Surface, s: int) -> None:
        cx = surf.get_width() / 2
        cy = surf.get_height() / 2
        r_out = (size_px * s) / 2 - stroke * s
        r_in = r_out * 0.42
        pts = _star_points(cx, cy, r_out, r_in)
        if filled:
            pygame.draw.polygon(surf, GOLD, pts)
            pygame.draw.polygon(surf, INDIGO, pts, stroke * s)
        else:
            pygame.draw.polygon(surf, INDIGO, pts, stroke * s)

    sprite = supersampled(size_px, size_px, render)
    _star_sprite_cache[key] = sprite
    return sprite


def _draw_star_row(surface: pygame.Surface, center_x: float, y: float,
                   size_px: int, total: int, filled_count: int) -> None:
    gap = int(size_px * 0.25)
    row_w = total * size_px + (total - 1) * gap
    x = center_x - row_w // 2
    for i in range(total):
        sprite = _get_star_sprite(size_px, filled=(i < filled_count))
        surface.blit(sprite, (x, y))
        x += size_px + gap


def draw_star_hud(surface: pygame.Surface, p1: Player, p2: Player) -> None:
    """Two rows of stars above each player's half — big (tournament sets) on top,
    small (current match games) below. Bigger achievement = bigger star."""
    match_size = _s(34)         # match wins = bigger achievement → bigger star
    game_size  = _s(22)         # game wins = smaller achievement → smaller star
    y_match = _s(20)
    y_game  = y_match + match_size + _s(10)

    for cx, player in ((WIDTH * 0.25, p1), (WIDTH * 0.75, p2)):
        _draw_star_row(surface, cx, y_match, match_size,
                       STARS_PER_TOURNAMENT, player.match_stars)
        _draw_star_row(surface, cx, y_game, game_size,
                       STARS_PER_MATCH, player.game_stars)


def point_status(player: Player) -> str | None:
    """Announcer text if `player` is one rally from winning set or match.
    Per-game point is intentionally silent — it triggers too often to feel special."""
    if player.balls != BALLS_PER_GAME - 1:
        return None
    if (player.match_stars == STARS_PER_TOURNAMENT - 1
            and player.game_stars == STARS_PER_MATCH - 1):
        return "МАТЧ-ПОИНТ"
    if player.game_stars == STARS_PER_MATCH - 1:
        return "СЕТ-ПОИНТ"
    return None


def draw_point_hints(surface: pygame.Surface, p1: Player, p2: Player,
                     font: pygame.font.Font) -> None:
    y = _s(150)
    for cx, player in ((WIDTH * 0.25, p1), (WIDTH * 0.75, p2)):
        hint = point_status(player)
        if hint is None:
            continue
        img = font.render(hint, True, INDIGO)
        surface.blit(img, (int(cx - img.get_width() / 2), y))


def draw_star_toast(surface: pygame.Surface, toast: dict, big_font: pygame.font.Font,
                    small_font: pygame.font.Font) -> None:
    """Big central star + '{name}: гейм/сет!' — brief on-earn overlay.
    toast: {'until_ms': int, 'name': str, 'kind': 'game'|'match'}."""
    now_ms = pygame.time.get_ticks()
    remaining = toast["until_ms"] - now_ms
    if remaining <= 0:
        return
    kind = toast["kind"]
    # Match stars are the bigger achievement — draw them larger.
    star_px = _s(220 if kind == "match" else 160)
    label = "сет" if kind == "match" else "победа"
    star = _get_star_sprite(star_px, filled=True)
    # Fade out over the last 300 ms
    alpha = 255 if remaining > 300 else int(255 * remaining / 300)
    star = star.copy()
    star.set_alpha(alpha)
    rect = star.get_rect(center=(WIDTH // 2, HEIGHT // 2 - _s(30)))
    surface.blit(star, rect)
    text = f"{toast['name']}: {label}!"
    img = big_font.render(text, True, INDIGO)
    img.set_alpha(alpha)
    surface.blit(img, ((WIDTH - img.get_width()) // 2, rect.bottom + _s(10)))


# ---------- Juice: particles, screen shake, hit-stop ----------

@dataclass
class Particle:
    x: float
    y: float
    vx: float
    vy: float
    life: float
    max_life: float
    size: float
    color: tuple


_particles: list = []
_shake: float = 0.0
_hitstop_frames: int = 0


def emit_dust(x: float, y: float, strength: float) -> None:
    """Puff of sand at (x, y). strength ≈ landing vy."""
    n = int(min(16, max(3, strength / 100)))
    scale = min(1.6, strength / 500)
    for _ in range(n):
        angle = random.uniform(-math.pi * 0.9, -math.pi * 0.1)  # upward hemisphere
        speed = random.uniform(60, 200) * scale
        _particles.append(Particle(
            x=x + random.uniform(-_s(10), _s(10)),
            y=y,
            vx=math.cos(angle) * speed,
            vy=math.sin(angle) * speed - random.uniform(20, 80),
            life=random.uniform(0.35, 0.65),
            max_life=0.65,
            size=random.uniform(_s(2), _s(5)),
            color=SAND,
        ))


def update_particles(dt: float) -> None:
    for p in _particles:
        p.vy += GRAVITY * dt * 0.4
        p.x  += p.vx * dt
        p.y  += p.vy * dt
        p.life -= dt
    _particles[:] = [p for p in _particles if p.life > 0]


def draw_particles(surface: pygame.Surface) -> None:
    for p in _particles:
        u = p.life / p.max_life
        alpha = max(0, min(255, int(240 * u)))
        r = int(p.size * (0.55 + 0.45 * u))
        if r < 1:
            continue
        surf = pygame.Surface((r * 2, r * 2), pygame.SRCALPHA)
        pygame.draw.circle(surf, (*p.color, alpha), (r, r), r)
        surface.blit(surf, (int(p.x - r), int(p.y - r)))


SHAKE_CAP = None    # computed in _init_shake_cap once _s is available at call time


def add_shake(amount: float) -> None:
    global _shake
    _shake = min(_s(11), max(_shake, amount))


def update_shake(dt: float) -> None:
    global _shake
    _shake *= max(0.0, 1.0 - dt * 8.0)
    if _shake < 0.3:
        _shake = 0.0


def get_shake_offset() -> tuple[int, int]:
    if _shake <= 0.0:
        return 0, 0
    return (int(random.uniform(-_shake, _shake)),
            int(random.uniform(-_shake, _shake)))


def request_hitstop(frames: int = 4) -> None:
    global _hitstop_frames
    _hitstop_frames = max(_hitstop_frames, frames)


# ---------- Procedural SFX (no assets, no numpy) ----------

SFX_SAMPLE_RATE = 44100
SFX: dict = {}
_audio_ok = False
_muted = False


def _synth_pcm(gen, dur_s: float, env=None, amp: float = 0.6) -> bytes:
    """Render a mono 16-bit PCM buffer. gen(t)->[-1,1]; env(u in [0,1])->[0,1]."""
    n = int(SFX_SAMPLE_RATE * dur_s)
    out = array.array("h", [0] * n)
    for i in range(n):
        t = i / SFX_SAMPLE_RATE
        v = gen(t)
        e = env(i / n) if env is not None else 1.0
        s = int(v * e * amp * 32767)
        if s > 32767: s = 32767
        elif s < -32768: s = -32768
        out[i] = s
    return out.tobytes()


def _env_exp(u):     return math.exp(-4.0 * u)
def _env_pluck(u):   return (u / 0.02) if u < 0.02 else math.exp(-8.0 * (u - 0.02))
def _sine(f):        return lambda t: math.sin(2 * math.pi * f * t)
def _square(f):      return lambda t: 1.0 if math.sin(2 * math.pi * f * t) >= 0 else -1.0
def _noise():        return lambda t: random.random() * 2 - 1


def _make_land_pcm() -> bytes:
    """Muffled sand-landing thud: low sine + heavily low-passed noise, slow decay."""
    n = int(SFX_SAMPLE_RATE * 0.18)
    out = array.array("h", [0] * n)
    y_lpf = 0.0
    for i in range(n):
        t = i / SFX_SAMPLE_RATE
        u = i / n
        env = math.exp(-3.5 * u)
        # LPF noise (dull) — 1-pole with alpha 0.12 rolls off high frequencies
        y_lpf = y_lpf * 0.88 + (random.random() * 2 - 1) * 0.12
        low = math.sin(2 * math.pi * 90 * t)                 # thump body ~90 Hz
        v = (y_lpf * 0.7 + low * 0.9) * env * 0.55
        s = int(max(-1.0, min(1.0, v)) * 32767)
        out[i] = s
    return out.tobytes()


def _init_sfx() -> None:
    """Set up pygame.mixer and bake all sound effects. Silent-fail on headless/no-audio."""
    global _audio_ok, SFX
    try:
        pygame.mixer.init(SFX_SAMPLE_RATE, -16, 1, 512)
    except pygame.error:
        _audio_ok = False
        return
    _audio_ok = True
    mk = lambda b: pygame.mixer.Sound(buffer=b)
    SFX["hit"]     = mk(_synth_pcm(_sine(180),       0.09, _env_pluck, 0.8))
    SFX["wallhit"] = mk(_synth_pcm(_sine(240),       0.05, _env_exp,   0.4))
    SFX["land"]    = mk(_make_land_pcm())
    # Jump: quick pitch sweep 200 → 480 Hz
    SFX["jump"]    = mk(_synth_pcm(
        lambda t: math.sin(2 * math.pi * (200 + 2800 * t) * t), 0.10, _env_exp, 0.45))
    SFX["serve"]   = mk(_synth_pcm(_square(800),     0.14, _env_exp,   0.35))
    SFX["score"]   = mk(_synth_pcm(_sine(660), 0.10, _env_exp, 0.55)
                      + _synth_pcm(_sine(990), 0.16, _env_exp, 0.55))
    # Match win: C - E - G - C arpeggio
    SFX["win"]     = mk(_synth_pcm(_sine(523), 0.14, _env_exp, 0.55)
                      + _synth_pcm(_sine(659), 0.14, _env_exp, 0.55)
                      + _synth_pcm(_sine(784), 0.14, _env_exp, 0.55)
                      + _synth_pcm(_sine(1046), 0.28, _env_exp, 0.55))


def sfx_play(name: str, volume: float = 1.0) -> None:
    if not _audio_ok or _muted:
        return
    s = SFX.get(name)
    if s is None:
        return
    ch = s.play()
    if ch is not None:
        ch.set_volume(max(0.0, min(1.0, volume)))


def sfx_toggle_mute() -> bool:
    global _muted
    _muted = not _muted
    if _muted:
        pygame.mixer.stop()
    return _muted


# ---------- Settings persistence ----------


def _settings_dir() -> str:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "BeachVolleyball")


def _settings_path() -> str:
    return os.path.join(_settings_dir(), "settings.json")


def load_settings() -> dict:
    try:
        with open(_settings_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(d: dict) -> None:
    try:
        os.makedirs(_settings_dir(), exist_ok=True)
        with open(_settings_path(), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def draw_message(surface: pygame.Surface, font: pygame.font.Font, lines: list[str]) -> None:
    total_h = sum(font.get_linesize() for _ in lines)
    y = (HEIGHT - total_h) // 2
    for line in lines:
        img = font.render(line, True, GOLD)
        x = (WIDTH - img.get_width()) // 2
        surface.blit(img, (x, y))
        y += font.get_linesize()


def _draw_button_row(
    surface: pygame.Surface,
    text_font: pygame.font.Font,
    entries: list,        # [(key, label), ...]
    current: str,
    y: float,
    btn_w: int,
    btn_h: int,
    panel_center_x: int,
    rects: dict,
    enabled: bool = True,
) -> None:
    gap = _s(20)
    total_w = btn_w * len(entries) + gap * (len(entries) - 1)
    x = panel_center_x - total_w // 2
    for key, label in entries:
        rect = pygame.Rect(x, y, btn_w, btn_h)
        selected = key == current
        if not enabled:
            bg = GOLD
            fg = tuple(int(c * 0.55 + 100 * 0.45) for c in INDIGO)
        elif selected:
            bg = SEAGRASS
            fg = INDIGO
        else:
            bg = GOLD
            fg = INDIGO
        pygame.draw.rect(surface, bg, rect, border_radius=_s(12))
        pygame.draw.rect(surface, INDIGO, rect, width=_s(2), border_radius=_s(12))
        img = text_font.render(label, True, fg)
        surface.blit(img, (rect.centerx - img.get_width() // 2,
                           rect.centery - img.get_height() // 2))
        if enabled:
            rects[key] = rect
        x += btn_w + gap


def draw_settings(
    surface: pygame.Surface,
    title_font: pygame.font.Font,
    text_font: pygame.font.Font,
    hint_font: pygame.font.Font,
    current_mode: str,
    current_game_mode: str,
    current_difficulty: str,
    controller_count: int,
    p1_name: str,
    p2_name: str,
    editing: str | None,          # 'p1' | 'p2' | None
) -> dict[str, pygame.Rect]:
    """Draw the settings overlay. Returns clickable rects keyed by action."""
    overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
    overlay.fill(INDIGO + (210,))
    surface.blit(overlay, (0, 0))

    panel_w, panel_h = _s(760), _s(660)
    panel = pygame.Rect((WIDTH - panel_w) // 2, (HEIGHT - panel_h) // 2, panel_w, panel_h)
    pygame.draw.rect(surface, CELADON, panel, border_radius=_s(18))
    pygame.draw.rect(surface, INDIGO, panel, width=_s(4), border_radius=_s(18))

    title = title_font.render("Настройки", True, INDIGO)
    surface.blit(title, (panel.centerx - title.get_width() // 2, panel.top + _s(20)))

    rects: dict[str, pygame.Rect] = {}

    def section_label(text: str, y: int) -> None:
        img = hint_font.render(text, True, INDIGO)
        surface.blit(img, (panel.left + _s(30), y))

    row_y = panel.top + _s(90)

    section_label("Ввод", row_y)
    _draw_button_row(
        surface, text_font,
        [(INPUT_AUTO, "Авто"), (INPUT_GAMEPAD, "Геймпад"), (INPUT_KEYBOARD, "Клавиатура")],
        current_mode, row_y + _s(24), _s(200), _s(56), panel.centerx, rects,
    )

    row_y += _s(24) + _s(56) + _s(30)
    section_label("Режим", row_y)
    _draw_button_row(
        surface, text_font,
        [(MODE_DUO, "Вдвоём"), (MODE_AI, "ИИ")],
        current_game_mode, row_y + _s(24), _s(200), _s(56), panel.centerx, rects,
    )

    row_y += _s(24) + _s(56) + _s(30)
    label_text = "Сложность ИИ" + (" (только в режиме ИИ)" if current_game_mode != MODE_AI else "")
    section_label(label_text, row_y)
    _draw_button_row(
        surface, text_font,
        [(DIFF_EASY, "Просто"), (DIFF_MEDIUM, "Средне"), (DIFF_HARD, "Сложно")],
        current_difficulty, row_y + _s(24), _s(180), _s(56), panel.centerx, rects,
        enabled=(current_game_mode == MODE_AI),
    )

    row_y += _s(24) + _s(56) + _s(30)
    section_label("Никнеймы (клик по полю для редактирования)", row_y)
    name_y = row_y + _s(24)
    name_w, name_h = _s(300), _s(52)
    gap = _s(40)
    p1_rect = pygame.Rect(panel.centerx - name_w - gap // 2, name_y, name_w, name_h)
    p2_rect = pygame.Rect(panel.centerx + gap // 2, name_y, name_w, name_h)
    for key, r, name in (("edit_p1", p1_rect, p1_name), ("edit_p2", p2_rect, p2_name)):
        is_editing = editing == key.split("_")[1]
        bg = GOLD if is_editing else CELADON
        pygame.draw.rect(surface, bg, r, border_radius=_s(10))
        pygame.draw.rect(surface, INDIGO, r, width=_s(2), border_radius=_s(10))
        shown = name + ("|" if is_editing else "")
        img = text_font.render(shown, True, INDIGO)
        surface.blit(img, (r.centerx - img.get_width() // 2,
                           r.centery - img.get_height() // 2))
        rects[key] = r

    back = pygame.Rect(panel.centerx - _s(100), panel.bottom - _s(70), _s(200), _s(48))
    pygame.draw.rect(surface, CERULEAN, back, border_radius=_s(10))
    pygame.draw.rect(surface, INDIGO, back, width=_s(2), border_radius=_s(10))
    back_img = text_font.render("Назад", True, GOLD)
    surface.blit(back_img, (back.centerx - back_img.get_width() // 2,
                            back.centery - back_img.get_height() // 2))
    rects["back"] = back

    return rects


# ---------- Physics ----------


def resolve_ball_slime(ball: Ball, slime: Slime) -> bool:
    """Bounce ball off the slime's dome (half-ellipse with flat bottom)."""
    # Semi-axes follow the current squish so the collision hitbox tracks the
    # visual deformation. squish > 0 → wider & shorter; squish < 0 → narrower & taller.
    a = (SLIME_W / 2) * (1.0 + slime.squish) + BALL_R
    b =  SLIME_H     * (1.0 - slime.squish) + BALL_R
    dx = ball.x - slime.x
    dy = ball.y - slime.y            # negative when ball is above the flat bottom
    if dy > 0:
        return False                  # ball is below the baseline — no bottom to hit
    v = (dx / a) ** 2 + (dy / b) ** 2
    if v >= 1.0:
        return False

    # Outward-pointing normal for the ellipse at this point: gradient of (x/a)^2+(y/b)^2
    nx = dx / (a * a)
    ny = dy / (b * b)
    nlen = math.hypot(nx, ny)
    if nlen < 1e-6:
        nx, ny = 0.0, -1.0
    else:
        nx /= nlen
        ny /= nlen

    # Push the ball to the ellipse surface (radial scale from center)
    scale = 1.0 / math.sqrt(v)
    ball.x = slime.x + dx * scale
    ball.y = slime.y + dy * scale

    # Reflect the ball's velocity relative to the slime's velocity
    rvx = ball.vx - slime.vx
    rvy = ball.vy - slime.vy
    dot = rvx * nx + rvy * ny
    impact = -dot if dot < 0 else 0.0
    if dot < 0:
        rvx -= 2 * dot * nx
        rvy -= 2 * dot * ny
    boost = 50.0 * SCALE
    ball.vx = (rvx + slime.vx) * BALL_HIT_BOOST + nx * boost
    ball.vy = (rvy + slime.vy) * BALL_HIT_BOOST + ny * boost

    # Jelly squash + hit SFX + optional screen shake at the moment of impact
    if impact > 0.0:
        slime.squish += min(SQUISH_CAP, impact * SQUISH_PER_HIT_DOT)
        sfx_play("hit", volume=min(1.0, 0.35 + impact / 1200.0))
        if impact > 700.0:
            add_shake(min(_s(11), (impact - 700.0) / 120.0))

    # Never let a slime hit place the ball on the far side of the net band —
    # otherwise the next frame's swept check has no chance to catch it and
    # the ball tunnels through the net.
    if slime.x < NET_X:
        limit = NET_X - NET_WIDTH // 2 - BALL_R
        if ball.x > limit:
            ball.x = limit
            if ball.vx > 0:
                ball.vx = -ball.vx * BALL_BOUNCE_DAMP
    elif slime.x > NET_X:
        limit = NET_X + NET_WIDTH // 2 + BALL_R
        if ball.x < limit:
            ball.x = limit
            if ball.vx < 0:
                ball.vx = -ball.vx * BALL_BOUNCE_DAMP
    return True


def resolve_ball_walls_and_net(ball: Ball) -> None:
    # Side walls
    hit_wall = False
    if ball.x < BALL_R:
        ball.x = BALL_R
        ball.vx = abs(ball.vx) * BALL_BOUNCE_DAMP
        hit_wall = True
    elif ball.x > WIDTH - BALL_R:
        ball.x = WIDTH - BALL_R
        ball.vx = -abs(ball.vx) * BALL_BOUNCE_DAMP
        hit_wall = True
    # Ceiling
    if ball.y < BALL_R:
        ball.y = BALL_R
        ball.vy = abs(ball.vy) * BALL_BOUNCE_DAMP
        hit_wall = True
    if hit_wall:
        sfx_play("wallhit", volume=0.4)

    # Net: pole rectangle (post) with a rounded top cap.
    # Swept side-collision using prev_x — determines which side the ball
    # came from, so it bounces back correctly even if velocity direction
    # disagrees with position (e.g., ball crossed NET_X within one step).
    post_left = NET_X - NET_WIDTH // 2 - BALL_R
    post_right = NET_X + NET_WIDTH // 2 + BALL_R
    if ball.y > NET_TOP_Y:
        inside_now = post_left < ball.x < post_right
        crossed_lr = ball.prev_x <= post_left and ball.x >= post_right
        crossed_rl = ball.prev_x >= post_right and ball.x <= post_left
        if inside_now or crossed_lr or crossed_rl:
            # Choose the side based on where the ball came from, not on vx
            came_from_left = ball.prev_x <= NET_X
            if came_from_left:
                ball.x = post_left
                ball.vx = -abs(ball.vx) * BALL_BOUNCE_DAMP
            else:
                ball.x = post_right
                ball.vx = abs(ball.vx) * BALL_BOUNCE_DAMP
            sfx_play("wallhit", volume=0.5)
    # Net cap
    dx = ball.x - NET_X
    dy = ball.y - NET_TOP_Y
    d = math.hypot(dx, dy)
    cap_r = _s(6) + BALL_R
    if d < cap_r and ball.y < NET_TOP_Y + cap_r:
        if d < 0.001:
            nx, ny = 0.0, -1.0
        else:
            nx, ny = dx / d, dy / d
        ball.x = NET_X + nx * cap_r
        ball.y = NET_TOP_Y + ny * cap_r
        dot = ball.vx * nx + ball.vy * ny
        if dot < 0:
            ball.vx -= 2 * dot * nx
            ball.vy -= 2 * dot * ny
            ball.vx *= BALL_BOUNCE_DAMP
            ball.vy *= BALL_BOUNCE_DAMP
            sfx_play("wallhit", volume=0.5)


# ---------- Game ----------


def make_players(controllers: list) -> tuple[Player, Player]:
    p1_slime = Slime(
        x=WIDTH * 0.25, color=P1_COLOR,
        left_bound=0, right_bound=NET_X - NET_WIDTH,
    )
    p2_slime = Slime(
        x=WIDTH * 0.75, color=P2_COLOR,
        left_bound=NET_X + NET_WIDTH, right_bound=WIDTH,
    )
    p1 = Player(
        slime=p1_slime,
        controller_index=0 if len(controllers) >= 1 else None,
        keys={"left": pygame.K_a, "right": pygame.K_d, "jump": pygame.K_w},
    )
    p2 = Player(
        slime=p2_slime,
        controller_index=1 if len(controllers) >= 2 else None,
        keys={"left": pygame.K_LEFT, "right": pygame.K_RIGHT, "jump": pygame.K_UP},
    )
    return p1, p2


def serve(ball: Ball, side: int) -> None:
    ball.frozen = True
    ball.x = WIDTH * (0.25 if side == 0 else 0.75)
    ball.y = float(_s(140))
    ball.vx = 0.0
    ball.vy = 0.0
    sfx_play("serve", volume=0.5)


def main() -> int:
    headless = os.environ.get("SDL_VIDEODRIVER") == "dummy"
    # Use anisotropic filtering when SDL upscales the 1080p framebuffer to a
    # higher-DPI monitor. Cleaner than the default linear/nearest filter.
    os.environ.setdefault("SDL_HINT_RENDER_SCALE_QUALITY", "2")
    # Small mixer buffer for low-latency SFX; must be called BEFORE pygame.init().
    pygame.mixer.pre_init(SFX_SAMPLE_RATE, -16, 1, 512)
    pygame.init()
    _init_sfx()
    pygame.display.set_caption("Beach Slime Volleyball")
    # Window icon — resolve icon.png next to the script or inside PyInstaller bundle
    base_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    icon_path = os.path.join(base_dir, "icon.png")
    if os.path.exists(icon_path):
        try:
            pygame.display.set_icon(pygame.image.load(icon_path))
        except pygame.error:
            pass

    fullscreen = True
    display_flags = pygame.SCALED
    screen = pygame.display.set_mode(
        (WIDTH, HEIGHT),
        display_flags | (pygame.FULLSCREEN if fullscreen else 0),
    )
    clock = pygame.time.Clock()

    # Fonts — default system font, bold, big (scaled to resolution)
    score_font = pygame.font.SysFont("arial", _s(64), bold=True)
    msg_font = pygame.font.SysFont("arial", _s(44), bold=True)
    title_font = pygame.font.SysFont("arial", _s(40), bold=True)
    text_font = pygame.font.SysFont("arial", _s(22), bold=True)
    hint_font = pygame.font.SysFont("arial", _s(18), bold=False)

    # Pre-render background
    background = pygame.Surface((WIDTH, HEIGHT))
    render_background(background)

    # Off-screen render target — everything draws here; then blitted to `screen`
    # with an optional shake offset so hard hits kick the whole scene.
    world = pygame.Surface((WIDTH, HEIGHT))

    # Gamepads via SDL GameController API (universal button/axis mapping)
    sdl_controller.init()
    controllers: list = []
    for i in range(sdl_controller.get_count()):
        if sdl_controller.is_controller(i):
            controllers.append(sdl_controller.Controller(i))

    p1, p2 = make_players(controllers)

    # Persisted settings — all user-visible options survive restarts.
    prefs = load_settings()
    p1.name = str(prefs.get("p1_name", DEFAULT_NAMES[0]))[:NAME_MAX_LEN] or DEFAULT_NAMES[0]
    p2.name = str(prefs.get("p2_name", DEFAULT_NAMES[1]))[:NAME_MAX_LEN] or DEFAULT_NAMES[1]

    ball = Ball()
    serve_side = 0
    serve_timer = pygame.time.get_ticks() + SERVE_DELAY_MS

    paused = False
    game_over = False
    winner_msg = ""
    settings_open = False
    editing_name: str | None = None      # 'p1' | 'p2' | None — text-input target
    star_toast: dict | None = None       # {'until_ms': int, 'name': str, 'kind': 'game'|'match'}
    input_mode    = prefs.get("input_mode", INPUT_AUTO)
    game_mode     = prefs.get("game_mode",  MODE_DUO)
    ai_difficulty = prefs.get("ai_difficulty", DIFF_MEDIUM)
    if input_mode    not in (INPUT_AUTO, INPUT_KEYBOARD, INPUT_GAMEPAD): input_mode    = INPUT_AUTO
    if game_mode     not in (MODE_DUO, MODE_AI):                         game_mode     = MODE_DUO
    if ai_difficulty not in (DIFF_EASY, DIFF_MEDIUM, DIFF_HARD):         ai_difficulty = DIFF_MEDIUM
    global _muted
    _muted = bool(prefs.get("muted", False))
    ai_state = AIState()
    gear_rect = pygame.Rect(0, 0, 0, 0)
    settings_rects: dict = {}

    serve(ball, serve_side)

    def _save_prefs() -> None:
        save_settings({
            "p1_name": p1.name, "p2_name": p2.name,
            "input_mode": input_mode, "game_mode": game_mode,
            "ai_difficulty": ai_difficulty, "muted": _muted,
        })

    # Hit-stop is a module-level counter we mutate from the main loop
    global _hitstop_frames

    running = True
    frames_run = 0
    max_frames = int(os.environ.get("VOLLEYBALL_MAX_FRAMES", "0"))

    while running:
        dt = clock.tick(FPS) / 1000.0
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                mx, my = event.pos
                if settings_open:
                    clicked_field = False
                    for key, rect in settings_rects.items():
                        if rect.collidepoint(mx, my):
                            if key == "back":
                                if editing_name:
                                    _save_prefs(); pygame.key.stop_text_input(); editing_name = None
                                settings_open = False
                            elif key in (INPUT_AUTO, INPUT_KEYBOARD, INPUT_GAMEPAD):
                                input_mode = key; _save_prefs()
                            elif key in (MODE_DUO, MODE_AI):
                                game_mode = key; _save_prefs()
                            elif key in (DIFF_EASY, DIFF_MEDIUM, DIFF_HARD):
                                ai_difficulty = key; _save_prefs()
                            elif key in ("edit_p1", "edit_p2"):
                                if editing_name:
                                    _save_prefs()
                                editing_name = "p1" if key == "edit_p1" else "p2"
                                pygame.key.start_text_input()
                                clicked_field = True
                            break
                    if not clicked_field and editing_name:
                        _save_prefs(); pygame.key.stop_text_input(); editing_name = None
                elif gear_rect.collidepoint(mx, my):
                    settings_open = True
            elif event.type == pygame.TEXTINPUT and editing_name:
                target = p1 if editing_name == "p1" else p2
                if len(target.name) < NAME_MAX_LEN:
                    target.name = target.name + event.text
            elif event.type == pygame.KEYDOWN:
                if editing_name:
                    target = p1 if editing_name == "p1" else p2
                    if event.key == pygame.K_BACKSPACE:
                        target.name = target.name[:-1]
                    elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_ESCAPE):
                        if not target.name:
                            target.name = DEFAULT_NAMES[0 if editing_name == "p1" else 1]
                        _save_prefs()
                        pygame.key.stop_text_input()
                        editing_name = None
                        if event.key == pygame.K_ESCAPE:
                            # Don't also treat Esc as exit-fullscreen while editing
                            continue
                    continue
                if event.key == pygame.K_ESCAPE:
                    if settings_open:
                        settings_open = False
                    elif fullscreen:
                        fullscreen = False
                        screen = pygame.display.set_mode((WIDTH, HEIGHT), display_flags)
                    else:
                        running = False
                elif event.key in (pygame.K_F11, pygame.K_f):
                    fullscreen = not fullscreen
                    flags = display_flags | pygame.FULLSCREEN if fullscreen else display_flags
                    screen = pygame.display.set_mode((WIDTH, HEIGHT), flags)
                elif event.key == pygame.K_p:
                    paused = not paused
                elif event.key == pygame.K_m:
                    sfx_toggle_mute(); _save_prefs()
                elif event.key == pygame.K_r or (
                    game_over and event.key in (pygame.K_RETURN, pygame.K_KP_ENTER)
                ):
                    p1.reset_tournament()
                    p2.reset_tournament()
                    game_over = False
                    serve_side = 0
                    serve(ball, serve_side)
                    serve_timer = pygame.time.get_ticks() + SERVE_DELAY_MS
            elif event.type == pygame.CONTROLLERBUTTONDOWN:
                if event.button == BTN_START and game_over:
                    p1.reset_tournament()
                    p2.reset_tournament()
                    game_over = False
                    serve_side = 0
                    serve(ball, serve_side)
                    serve_timer = pygame.time.get_ticks() + SERVE_DELAY_MS
                elif event.button == BTN_START:
                    paused = not paused
                elif event.button == BTN_BACK:
                    p1.reset_tournament()
                    p2.reset_tournament()
                    game_over = False
                    serve_side = 0
                    serve(ball, serve_side)
                    serve_timer = pygame.time.get_ticks() + SERVE_DELAY_MS
            elif event.type == pygame.CONTROLLERDEVICEADDED:
                if sdl_controller.is_controller(event.device_index):
                    controllers.append(sdl_controller.Controller(event.device_index))
                    p1.controller_index = 0 if len(controllers) >= 1 else None
                    p2.controller_index = 1 if len(controllers) >= 2 else None
            elif event.type == pygame.CONTROLLERDEVICEREMOVED:
                controllers = [c for c in controllers if c.id != event.instance_id]
                p1.controller_index = 0 if len(controllers) >= 1 else None
                p2.controller_index = 1 if len(controllers) >= 2 else None

        keys = pygame.key.get_pressed()

        if _hitstop_frames > 0 and not paused and not game_over and not settings_open:
            _hitstop_frames -= 1

        if not paused and not game_over and not settings_open and _hitstop_frames == 0:
            # Serve delay
            if ball.frozen and pygame.time.get_ticks() >= serve_timer:
                ball.frozen = False
                ball.vx = 0.0
                ball.vy = 0.0

            # Player input — P2 is AI-driven in single-player mode
            m1, j1 = read_player_input(p1, controllers, keys, input_mode)
            if game_mode == MODE_AI:
                m2, j2 = ai_input(ai_state, p2.slime, ball, ai_difficulty, pygame.time.get_ticks())
            else:
                m2, j2 = read_player_input(p2, controllers, keys, input_mode)
            p1.slime.update(dt, m1, j1)
            p2.slime.update(dt, m2, j2)

            # Ball
            ball.update(dt)
            resolve_ball_walls_and_net(ball)
            resolve_ball_slime(ball, p1.slime)
            resolve_ball_slime(ball, p2.slime)

            # Ground — award the rally and advance the tournament ladder
            if ball.y + BALL_R >= GROUND_FOOT_Y and not ball.frozen:
                scored_side = 1 if ball.x < NET_X else 0    # opposite side scores
                winner, loser = (p1, p2) if scored_side == 0 else (p2, p1)
                winner.balls += 1
                sfx_play("score", volume=0.6)
                request_hitstop(4)
                serve_side = 0 if scored_side == 1 else 1   # loser serves next
                if winner.balls >= BALLS_PER_GAME:
                    winner.balls = 0
                    loser.balls = 0
                    winner.game_stars += 1
                    star_toast = {"until_ms": pygame.time.get_ticks() + STAR_TOAST_MS,
                                  "name": winner.name, "kind": "game"}
                    if winner.game_stars >= STARS_PER_MATCH:
                        winner.game_stars = 0
                        loser.game_stars = 0
                        winner.match_stars += 1
                        star_toast = {"until_ms": pygame.time.get_ticks() + STAR_TOAST_MS,
                                      "name": winner.name, "kind": "match"}
                        if winner.match_stars >= STARS_PER_TOURNAMENT:
                            game_over = True
                            winner_msg = f"{winner.name} выиграл турнир!"
                            sfx_play("win", volume=0.7)
                if not game_over:
                    serve(ball, serve_side)
                    serve_timer = pygame.time.get_ticks() + SERVE_DELAY_MS

        # Juice updates run every frame regardless of hit-stop / pause
        update_particles(dt)
        update_shake(dt)

        # ---- Draw (into off-screen world; blitted to screen with shake) ----
        world.blit(background, (0, 0))
        draw_net(world)
        draw_slime(world, p1.slime)
        draw_slime(world, p2.slime)
        draw_particles(world)
        draw_ball(world, ball)
        draw_score(world, score_font, p1.balls, p2.balls)
        draw_star_hud(world, p1, p2)
        draw_point_hints(world, p1, p2, text_font)
        gear_rect = draw_gear_icon(world)

        # On-earn star overlay (fades over the last 300 ms of its lifetime)
        if star_toast is not None:
            if pygame.time.get_ticks() >= star_toast["until_ms"]:
                star_toast = None
            else:
                draw_star_toast(world, star_toast, msg_font, text_font)

        if paused:
            draw_message(world, msg_font, ["PAUSED", "Press P to resume"])
        elif game_over:
            draw_message(world, msg_font, [winner_msg, "Enter / Start — заново"])

        if settings_open:
            settings_rects = draw_settings(
                world, title_font, text_font, hint_font,
                input_mode, game_mode, ai_difficulty, len(controllers),
                p1.name, p2.name, editing_name,
            )
        else:
            settings_rects = {}

        ox, oy = get_shake_offset()
        if ox or oy:
            screen.fill(INDIGO)
        screen.blit(world, (ox, oy))
        pygame.display.flip()

        frames_run += 1
        if max_frames and frames_run >= max_frames:
            running = False
        if headless and frames_run >= 3:
            running = False

    pygame.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
