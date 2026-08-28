"""Regression test for the net-tunneling bug.

Scenario: right slime is at its minimum x (right next to the net), the ball
approaches from above-left. Slime hit places the ball inside the net's
collision band with leftward velocity. Without the fix, the ball tunnels
through the net to the left side within a couple of frames.

Run:  python test_net_collision.py
"""

import os
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame
pygame.init()

from volleyball import (
    Ball, Slime, resolve_ball_slime, resolve_ball_walls_and_net,
    NET_X, NET_WIDTH, NET_TOP_Y, SLIME_W, BALL_R, WIDTH, FPS,
)


def simulate_right_slime_leftward_hit() -> tuple[float, list[float]]:
    """Realistic scenario: right slime sits close to the net; the ball falls
    onto the upper-left portion of the dome. Without the fix, resolve_ball_slime
    snaps the ball to the ellipse surface at an x that lies inside the net's
    collision band; the ball's leftward velocity then carries it past the net
    on the next frame with no chance for the wall check to react.

    Returns final ball x and a per-frame x trace."""
    right_slime = Slime(
        x=NET_X + NET_WIDTH + SLIME_W / 2,       # exactly at the minimum x (touching net)
        color=(0, 0, 0),
        left_bound=NET_X + NET_WIDTH,
        right_bound=WIDTH,
    )
    ball = Ball()
    ball.frozen = False
    # Ball at slime baseline, on the right side of the net. When resolve_ball_slime
    # snaps it to the ellipse surface with a large radial scale factor, the final
    # position lands on the LEFT of the net band — reproducing the original tunneling.
    ball.x = right_slime.x - 135                  # inside dome, at leftmost horizontal extent
    ball.y = right_slime.y - 30                   # a bit above baseline — still inside dome
    ball.vx = 0.0
    ball.vy = 800.0                               # falling

    dt = 1.0 / FPS
    trace = [ball.x]
    for _ in range(30):
        ball.update(dt)
        resolve_ball_walls_and_net(ball)
        resolve_ball_slime(ball, right_slime)
        trace.append(ball.x)
    return ball.x, trace


def main() -> int:
    final_x, trace = simulate_right_slime_leftward_hit()
    tunneled = final_x < NET_X

    print(f"final ball.x = {final_x:.1f}   NET_X = {NET_X}")
    print(f"min ball.x during trace = {min(trace):.1f}")
    print(f"tunneled through net: {tunneled}")

    assert not tunneled, (
        f"Ball tunneled through the net: reached x={min(trace):.1f}, "
        f"final x={final_x:.1f}, NET_X={NET_X}"
    )
    print("OK — ball stayed on the right side of the net.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
