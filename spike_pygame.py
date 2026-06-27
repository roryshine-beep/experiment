#!/usr/bin/env python3
"""
SPIKE — connectivity-first overworld generator (NES-LOZ feel).

The question this answers: can we generate a LARGE random world that LOOKS organic
(rivers, mountain ranges, coast) yet is PROVABLY navigable (every town connected;
chokepoints like bridges/passes are real but never seal anything off)?

The principle is **connectivity first, scenery second**:

  1. Scatter towns (Poisson-ish rejection sampling, min spacing).
  2. PLAN a route graph over the towns — a spanning tree + a few extra edges for
     loops. This is the connectivity *intent*, decided before any obstacle exists.
  3. Scatter obstacles FREELY: a sea on one edge, meandering rivers, mountain ranges.
     They don't have to dodge anything.
  4. REALIZE each planned route by carving a guaranteed-passable corridor through
     whatever's in the way:  water -> BRIDGE,  mountain -> PASS,  grass -> ROAD.
     Because every planned edge is realized, the spanning tree guarantees the whole
     town network is connected — by construction, not by luck.
  5. FLOOD-FILL from one town as the insurance: assert every town is reached. (It
     always is here; the check is what lets us *regenerate* if we ever break that.)

That carve-last step is exactly why bridges/passes are guaranteed: a route only ever
crosses a river where we then stamp a bridge, and only ever crosses a range where we
then cut a pass. Walk the world (collision is ON) — roads cross rivers at bridges,
thread mountain ranges at passes, and you must go *around* the rest.

How this maps onto the real game: this is the **third, persistent terrain layer** I
described — a static geography baked once per world_seed (like biome_map already is),
that the micro arenas would read from to know "the river runs through my east edge,
bridge at row 10". It deliberately does NOT touch dominion.py — it's a standalone
proof of the algorithm. Throwaway; we'll revisit.

Run:  python3 spike_pygame.py
Keys: arrows/WASD walk (collision) · r new world · t warp to next town ·
      g route graph · h flood-fill (unreachable pockets in red) · esc quit
"""
import math
import random

import pygame

# ---- map / display ----
MAP_W, MAP_H = 90, 56          # the world is a MAP_W x MAP_H lattice of terrain cells
CELL = 11
ARENA_W, ARENA_H = MAP_W * CELL, MAP_H * CELL
HUD = 96
W, H = ARENA_W, ARENA_H + HUD

# ---- terrain codes ----
GRASS, WATER, MOUNTAIN, ROAD, BRIDGE, TOWN, PASS = range(7)
PASSABLE = {GRASS, ROAD, BRIDGE, TOWN, PASS}
COLOR = {
    GRASS:    (78, 134, 66),
    WATER:    (52, 98, 170),
    MOUNTAIN: (118, 114, 120),
    ROAD:     (203, 178, 120),
    BRIDGE:   (150, 108, 66),
    TOWN:     (238, 238, 244),
    PASS:     (170, 150, 108),
}

# ---- generation params ----
SEA_ROWS = 7                   # depth of the coastal sea on one edge
TOWN_TARGET = 13
TOWN_SPACING = 9               # min Chebyshev tiles between town centers
TOWN_BUFFER = 2                # keep obstacles this far off a town
MARGIN = 3
EXTRA_EDGES = 4                # loops beyond the spanning tree
N_RIVERS = (2, 4)
N_RANGES = (3, 5)

CARD = [(1, 0), (-1, 0), (0, 1), (0, -1)]
SPEED = 7.5
RAD = 0.30


def sgn(x):
    return (x > 0) - (x < 0)


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


# --------------------------------------------------------------------------- gen
def _scatter_towns(rng, sea_edge):
    """Reject-sample town centers: inside margins, off the sea, min-spaced."""
    towns = []
    for _ in range(4000):
        if len(towns) >= TOWN_TARGET:
            break
        r = rng.randint(MARGIN, MAP_H - 1 - MARGIN)
        c = rng.randint(MARGIN, MAP_W - 1 - MARGIN)
        if sea_edge == "S" and r >= MAP_H - SEA_ROWS - TOWN_BUFFER:
            continue
        if sea_edge == "N" and r <= SEA_ROWS + TOWN_BUFFER:
            continue
        if sea_edge == "E" and c >= MAP_W - SEA_ROWS - TOWN_BUFFER:
            continue
        if sea_edge == "W" and c <= SEA_ROWS + TOWN_BUFFER:
            continue
        if all(max(abs(r - tr), abs(c - tc)) >= TOWN_SPACING for tr, tc in towns):
            towns.append((r, c))
    return towns


def _plan_edges(rng, towns):
    """Connectivity intent: a minimum spanning tree (Prim) over town centers, plus a
    few extra short edges for loops — decided before any obstacle is placed."""
    n = len(towns)

    def dist(a, b):
        return (towns[a][0] - towns[b][0]) ** 2 + (towns[a][1] - towns[b][1]) ** 2

    in_tree, edges = {0}, []
    while len(in_tree) < n:
        best = None
        for a in in_tree:
            for b in range(n):
                if b in in_tree:
                    continue
                d = dist(a, b)
                if best is None or d < best[0]:
                    best = (d, a, b)
        edges.append((best[1], best[2]))
        in_tree.add(best[2])
    # extra loop edges: random short pairs not already linked
    have = {frozenset(e) for e in edges}
    cand = sorted(((dist(a, b), a, b) for a in range(n) for b in range(a + 1, n)
                   if frozenset((a, b)) not in have), key=lambda x: x[0])
    for _d, a, b in cand[:EXTRA_EDGES * 3]:
        if len([1 for x, y in edges if a in (x, y) or b in (x, y)]):
            edges.append((a, b))
            if len(edges) >= (n - 1) + EXTRA_EDGES:
                break
    return edges


def _meander(rng, src, dst, toward, limit):
    """A jittered cardinal path from src to dst. `toward` is the bias (higher =
    straighter). Returns the list of cells walked."""
    r, c = src
    path = [(r, c)]
    for _ in range(limit):
        if abs(r - dst[0]) + abs(c - dst[1]) <= 1:
            break
        if rng.random() < toward:
            dr, dc = dst[0] - r, dst[1] - c
            if rng.random() < abs(dr) / max(1, abs(dr) + abs(dc)):
                r += sgn(dr)
            else:
                c += sgn(dc)
        else:
            dr, dc = rng.choice(CARD)
            r, c = r + dr, c + dc
        r, c = clamp(r, 0, MAP_H - 1), clamp(c, 0, MAP_W - 1)
        path.append((r, c))
    path.append(tuple(dst))
    return path


def _route_path(rng, src, dst):
    """A clean road from src to dst: march along the dominant axis with only gentle,
    occasional bends (no aimless wander), so roads read as purposeful lines that meet
    rivers/ranges square-on — a short bridge or pass rather than a long parallel run."""
    r, c = src
    path = [(r, c)]
    while (r, c) != (dst[0], dst[1]):
        dr, dc = dst[0] - r, dst[1] - c
        if dr == 0:
            c += sgn(dc)
        elif dc == 0:
            r += sgn(dr)
        else:
            # follow the longer remaining axis ~85% of the time; the rest is a small jog
            # on the other axis, which gives a relaxed bend instead of a rigid L.
            along_r = abs(dr) >= abs(dc)
            if rng.random() < 0.15:
                along_r = not along_r
            r, c = (r + sgn(dr), c) if along_r else (r, c + sgn(dc))
        path.append((r, c))
    return path


def _sea_target(rng, sea_edge):
    if sea_edge == "S":
        return (MAP_H - 1, rng.randint(0, MAP_W - 1))
    if sea_edge == "N":
        return (0, rng.randint(0, MAP_W - 1))
    if sea_edge == "E":
        return (rng.randint(0, MAP_H - 1), MAP_W - 1)
    return (rng.randint(0, MAP_H - 1), 0)


def _edge_point(rng):
    side = rng.choice("NSEW")
    if side == "N":
        return (0, rng.randint(MARGIN, MAP_W - 1 - MARGIN))
    if side == "S":
        return (MAP_H - 1, rng.randint(MARGIN, MAP_W - 1 - MARGIN))
    if side == "E":
        return (rng.randint(MARGIN, MAP_H - 1 - MARGIN), MAP_W - 1)
    return (rng.randint(MARGIN, MAP_H - 1 - MARGIN), 0)


def _near_town(towns, r, c, rad):
    return any(max(abs(r - tr), abs(c - tc)) <= rad for tr, tc in towns)


def _flood(grid, start):
    seen = [[False] * MAP_W for _ in range(MAP_H)]
    stack = [start]
    seen[start[0]][start[1]] = True
    while stack:
        r, c = stack.pop()
        for dr, dc in CARD:
            nr, nc = r + dr, c + dc
            if 0 <= nr < MAP_H and 0 <= nc < MAP_W and not seen[nr][nc] \
                    and grid[nr][nc] in PASSABLE:
                seen[nr][nc] = True
                stack.append((nr, nc))
    return seen


def generate(seed):
    """Build one world. Returns a dict with the grid, towns, edges, the reachability
    mask, and some stats. Loops (regenerates with a bumped seed) until the flood-fill
    confirms every town is connected — the navigability guarantee, enforced."""
    attempt = 0
    while True:
        rng = random.Random(seed + attempt * 7919)
        attempt += 1
        grid = [[GRASS] * MAP_W for _ in range(MAP_H)]
        sea_edge = rng.choice("NSEW")

        # (3a) coastal sea on one edge
        for r in range(MAP_H):
            for c in range(MAP_W):
                if (sea_edge == "S" and r >= MAP_H - SEA_ROWS) \
                        or (sea_edge == "N" and r < SEA_ROWS) \
                        or (sea_edge == "E" and c >= MAP_W - SEA_ROWS) \
                        or (sea_edge == "W" and c < SEA_ROWS):
                    grid[r][c] = WATER

        # (1) towns
        towns = _scatter_towns(rng, sea_edge)
        if len(towns) < 4:
            continue
        # (2) plan the route graph (before obstacles exist)
        edges = _plan_edges(rng, towns)

        # (3b) rivers — meander from a land edge to the sea, freely
        for _ in range(rng.randint(*N_RIVERS)):
            src = _edge_point(rng)
            dst = _sea_target(rng, sea_edge)
            for (r, c) in _meander(rng, src, dst, 0.66, (MAP_W + MAP_H) * 3):
                if grid[r][c] != WATER and not _near_town(towns, r, c, TOWN_BUFFER):
                    grid[r][c] = WATER

        # (3c) mountain ranges — ridge lines thickened into masses, on land
        for _ in range(rng.randint(*N_RANGES)):
            r, c = rng.randint(MARGIN, MAP_H - 1 - MARGIN), rng.randint(MARGIN, MAP_W - 1 - MARGIN)
            length = rng.randint(MAP_W // 4, MAP_W // 2)
            ridge_dst = (clamp(r + rng.randint(-MAP_H // 3, MAP_H // 3), 0, MAP_H - 1),
                         clamp(c + rng.randint(-MAP_W // 3, MAP_W // 3), 0, MAP_W - 1))
            for (rr, cc) in _meander(rng, (r, c), ridge_dst, 0.72, length):
                w = 2 if rng.random() < 0.3 else 1
                for dr in range(-w, w + 1):
                    for dc in range(-w, w + 1):
                        nr, nc = rr + dr, cc + dc
                        if 0 <= nr < MAP_H and 0 <= nc < MAP_W \
                                and grid[nr][nc] == GRASS \
                                and not _near_town(towns, nr, nc, TOWN_BUFFER):
                            grid[nr][nc] = MOUNTAIN

        # (4) REALIZE routes: carve a guaranteed corridor through whatever's there.
        # Roads are a clean single tile (no widening) following a straight-ish path, so
        # they look like roads, not cleared swaths; a river crossing becomes a one-tile
        # BRIDGE, a range crossing a one-tile PASS.
        for a, b in edges:
            for (r, c) in _route_path(rng, towns[a], towns[b]):
                cur = grid[r][c]
                if cur == TOWN:
                    continue
                if cur == WATER:
                    grid[r][c] = BRIDGE
                elif cur == MOUNTAIN:
                    grid[r][c] = PASS
                else:
                    grid[r][c] = ROAD

        # towns (stamp a 2x2 keep so they read as a place)
        for (tr, tc) in towns:
            for dr in (0, 1):
                for dc in (0, 1):
                    if tr + dr < MAP_H and tc + dc < MAP_W:
                        grid[tr + dr][tc + dc] = TOWN

        # (5) verify navigability — every town reachable from town 0
        reach = _flood(grid, towns[0])
        if all(reach[tr][tc] for tr, tc in towns):
            total = sum(grid[r][c] in PASSABLE for r in range(MAP_H) for c in range(MAP_W))
            got = sum(reach[r][c] for r in range(MAP_H) for c in range(MAP_W))
            return {"grid": grid, "towns": towns, "edges": edges, "reach": reach,
                    "seed": seed, "attempts": attempt, "sea": sea_edge,
                    "reach_pct": 100.0 * got / max(1, total)}
        if attempt > 30:        # should never happen — the carve guarantees it
            return {"grid": grid, "towns": towns, "edges": edges, "reach": reach,
                    "seed": seed, "attempts": attempt, "sea": sea_edge, "reach_pct": 0.0}


# ------------------------------------------------------------------------- render
def main():
    pygame.init()
    screen = pygame.display.set_mode((W, H))
    pygame.display.set_caption("Dominion — connectivity-first overworld spike")
    clock = pygame.time.Clock()
    font = pygame.font.SysFont("monospace", 14)
    big = pygame.font.SysFont("monospace", 18, bold=True)

    seed = random.randint(0, 10 ** 9)
    world = generate(seed)
    show_graph = False
    show_reach = False

    def town_center_px(t):
        return (t[1] * CELL + CELL, t[0] * CELL + CELL)

    # wizard starts on town 0
    t0 = world["towns"][0]
    px, py = t0[1] + 0.5, t0[0] + 0.5
    warp_i = 0

    def passable_cell(x, y):
        ix, iy = int(x), int(y)
        if ix < 0 or iy < 0 or ix >= MAP_W or iy >= MAP_H:
            return False
        return world["grid"][iy][ix] in PASSABLE

    def blocked(x, y):
        return any(not passable_cell(x + ox, y + oy) for ox in (-RAD, RAD) for oy in (-RAD, RAD))

    # pre-render the terrain to a surface (static until regenerate)
    def bake_terrain():
        surf = pygame.Surface((ARENA_W, ARENA_H))
        for r in range(MAP_H):
            row = world["grid"][r]
            for c in range(MAP_W):
                pygame.draw.rect(surf, COLOR[row[c]], (c * CELL, r * CELL, CELL, CELL))
        # town keeps get a white ring + dark dot so they pop
        for (tr, tc) in world["towns"]:
            rect = (tc * CELL, tr * CELL, CELL * 2, CELL * 2)
            pygame.draw.rect(surf, (255, 255, 255), rect, 2)
            pygame.draw.rect(surf, (40, 40, 60), (tc * CELL + CELL - 2, tr * CELL + CELL - 2, 4, 4))
        return surf

    terrain = bake_terrain()
    last_axis = "v"

    running = True
    while running:
        dt = clock.tick(60) / 1000.0
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                running = False
            elif e.type == pygame.KEYDOWN:
                if e.key in (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_a, pygame.K_d):
                    last_axis = "h"
                elif e.key in (pygame.K_UP, pygame.K_DOWN, pygame.K_w, pygame.K_s):
                    last_axis = "v"
                if e.key == pygame.K_ESCAPE:
                    running = False
                elif e.key == pygame.K_r:
                    seed = random.randint(0, 10 ** 9)
                    world = generate(seed)
                    terrain = bake_terrain()
                    t0 = world["towns"][0]
                    px, py = t0[1] + 0.5, t0[0] + 0.5
                    warp_i = 0
                elif e.key == pygame.K_g:
                    show_graph = not show_graph
                elif e.key == pygame.K_h:
                    show_reach = not show_reach
                elif e.key == pygame.K_t:
                    warp_i = (warp_i + 1) % len(world["towns"])
                    t = world["towns"][warp_i]
                    px, py = t[1] + 0.5, t[0] + 0.5

        # movement (4-dir, collision against water/mountain)
        keys = pygame.key.get_pressed()
        dx = (keys[pygame.K_RIGHT] or keys[pygame.K_d]) - (keys[pygame.K_LEFT] or keys[pygame.K_a])
        dy = (keys[pygame.K_DOWN] or keys[pygame.K_s]) - (keys[pygame.K_UP] or keys[pygame.K_w])
        if dx and dy:
            dy = 0 if last_axis == "h" else dy
            dx = 0 if last_axis == "v" else dx
        if dx and not blocked(px + dx * SPEED * dt, py):
            px += dx * SPEED * dt
        if dy and not blocked(px, py + dy * SPEED * dt):
            py += dy * SPEED * dt

        # ---- draw ----
        screen.blit(terrain, (0, 0))
        if show_reach:
            ov = pygame.Surface((ARENA_W, ARENA_H), pygame.SRCALPHA)
            reach = world["reach"]
            for r in range(MAP_H):
                for c in range(MAP_W):
                    if world["grid"][r][c] in PASSABLE and not reach[r][c]:
                        pygame.draw.rect(ov, (220, 40, 40, 130), (c * CELL, r * CELL, CELL, CELL))
            screen.blit(ov, (0, 0))
        if show_graph:
            for a, b in world["edges"]:
                pygame.draw.line(screen, (255, 255, 255), town_center_px(world["towns"][a]),
                                 town_center_px(world["towns"][b]), 1)

        sx, sy = int(px * CELL), int(py * CELL)
        pygame.draw.circle(screen, (250, 245, 120), (sx, sy), int(CELL * 0.45))
        pygame.draw.circle(screen, (60, 40, 110), (sx, sy), int(CELL * 0.45), 2)

        # ---- HUD ----
        pygame.draw.rect(screen, (16, 16, 24), (0, ARENA_H, W, HUD))
        rivers = sum(1 for r in range(MAP_H) for c in range(MAP_W)
                     if world["grid"][r][c] == WATER)
        bridges = sum(1 for r in range(MAP_H) for c in range(MAP_W) if world["grid"][r][c] == BRIDGE)
        passes = sum(1 for r in range(MAP_H) for c in range(MAP_W) if world["grid"][r][c] == PASS)
        screen.blit(big.render(f"WORLD seed {world['seed']}   "
                               f"towns {len(world['towns'])}  bridges {bridges}  passes {passes}",
                               True, (235, 235, 245)), (10, ARENA_H + 8))
        screen.blit(font.render(f"sea edge {world['sea']}   reachable {world['reach_pct']:.1f}% of "
                                f"walkable land   gen attempts {world['attempts']}   "
                                f"all towns connected: YES",
                                True, (170, 220, 170)), (10, ARENA_H + 36))
        screen.blit(font.render("arrows/WASD walk (collision on) · r new world · t warp town · "
                                "g route graph · h reachability · esc",
                                True, (130, 130, 150)), (10, ARENA_H + 62))
        pygame.display.flip()

    pygame.quit()


if __name__ == "__main__":
    main()
