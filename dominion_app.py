#!/usr/bin/env python3
"""
Dominion — pygame edition.

The full game (curses retired): the live territory-control sim with the wizard
layer, rendered in a real graphical window. The SIMULATION still lives in
dominion.py (imported here, unchanged — the single source of truth); this module
is only the front-end: rendering, input, and the realtime arena.

Three views, cycled with TAB/m:
  TILE   — the wizard's lived realtime arena (default): tactile 4-directional
           movement, terrain, towns, the shadow's minions, and battle fronts.
  MAP    — the macro world (50x50) as a minimap, corruption-shaded, with the
           Shadow/dominion/per-faction readout (dev view).
  CONTROLS — the dev sliders (territory/army/corruption/hearts/shadow).

Run:  python3 dominion_app.py
"""
import sys, types, math, random, glob, os

# Import the sim headlessly: it reads curses.* at import and init_terrain() needs
# curses.COLORS. We stub curses (256-color) and convert its palette to RGB; the
# sim's own curses draw/main code simply never runs.
_cur = types.ModuleType("curses")
_cur.COLORS = 256
_cur.COLOR_PAIRS = 256
_cur.A_NORMAL = 0
_cur.error = type("error", (Exception,), {})
_cur.init_pair = lambda *a: None
_cur.color_pair = lambda n: 0
_cur.__getattr__ = lambda name: 0
sys.modules["curses"] = _cur

import dominion as d
d.init_terrain()

import pygame

# ---------------- layout / tunables ----------------
CELL = 32
TILE = d.TILE_GS                       # 20
ARENA = TILE * CELL                    # 640
HUD = 168
W, H = ARENA, ARENA + HUD

SPEED = 6.0                            # wizard cells/second
RAD = 0.34
BOLT_SPEED = 14.0
BOLT_HIT = 0.55
CHARGE_SPEED_MULT = 1.5                # the charged bolt flies faster than a basic barb
OUTSKIRTS_RADIUS = 3                   # tiles from a town where its houses/farms/folk bleed out
# Equippable staves (the wizard's attack kit — first equipment slot). Each fires its
# `shots` (angle offsets, radians, off the facing dir) per attack, each projectile at
# `power_mult` of the shot's power. `equip_might` is the Might needed to wield it
# (magic is folded into Might for now — to fork into its own stat later).
STAVES = [
    {"name": "Plain Staff", "shots": [0.0], "power_mult": 1.0, "equip_might": 0},
    {"name": "Fanstaff", "shots": [-0.35, 0.0, 0.35], "power_mult": 1.0 / 3, "equip_might": 6},
]
SOLID = {"♣", "▲", "●", "≈"}          # impassable terrain features (interior only)

CARDINALS = [(1, 0), (-1, 0), (0, 1), (0, -1)]
MON_SPEED = 2.4
MON_RAD = 0.40
MON_LEG = (0.6, 1.6)
MON_PAUSE = 0.30
MON_FIRE_CD = (1.6, 2.8)
MON_PROJ_SPEED = 7.5
MON_HP = 2
TOUCH_RANGE = 0.6
INVULN = 1.0

# The shadow's lieutenant (Sauron-figure): a single powerful boss that holds the
# most-corrupted clan's seat. Defeating it routs that clan and wins the age.
BOSS_HP = 26                           # base hit points (scaled up by corruption)
BOSS_SPEED = 2.0                       # chases the wizard (cardinal, per-axis)
BOSS_FIRE_CD = (0.8, 1.3)              # fires faster than a common minion
BOSS_PROJ_SPEED = 9.5                  # and its bolts are faster
BOSS_TOUCH = 0.9                       # larger body — touch hurts at greater range
GUARDIAN_HP = 20                       # an artifact's mini-boss guardian (tough, < the lord)
AGENT_HP = 14                          # one of the lord's retinue (lesser than the lord/guardian)
ALLY_SPEED = 4.5                       # an escorted seeker keeps near the wizard
ALLY_FIRE_CD = (0.7, 1.2)              # and looses bolts at monsters this often

WAR_FAVOR = 3                          # favor <= this => that faction attacks the wizard
KILL_FAVOR_DOWN = 1.0
KILL_FAVOR_UP = 0.7
HOST_SIZE = 4
CYCLE_SECONDS = 3.0                    # a world-cycle this often while on a contested tile

FACTION_RGB = [(214, 64, 58), (74, 120, 232), (74, 184, 86), (228, 196, 72)]
# Wizard-view terrain paint (the geo traversal layer): rivers/ranges and their crossings.
GEO_PAINT_RGB = {"water": (52, 98, 170), "rock": (120, 116, 120),
                 "road": (193, 168, 120), "bridge": (150, 108, 66), "pass": (165, 150, 110)}


# ---------------- color ----------------
def xterm_rgb(n):
    if isinstance(n, tuple):
        return n
    if n < 16:
        base = [(0, 0, 0), (128, 0, 0), (0, 128, 0), (128, 128, 0), (0, 0, 128),
                (128, 0, 128), (0, 128, 128), (192, 192, 192), (128, 128, 128),
                (255, 0, 0), (0, 255, 0), (255, 255, 0), (0, 0, 255),
                (255, 0, 255), (0, 255, 255), (255, 255, 255)]
        return base[n]
    if n < 232:
        n -= 16
        lv = [0, 95, 135, 175, 215, 255]
        return (lv[n // 36], lv[(n // 6) % 6], lv[n % 6])
    v = 8 + (n - 232) * 10
    return (v, v, v)


def shade(rgb, f):
    return tuple(max(0, min(255, int(v * f))) for v in rgb)


# ---------------- sprites (pre-rendered vector) ----------------
def _wizard(sz):
    s = pygame.Surface((sz, sz), pygame.SRCALPHA)
    cx = sz // 2
    pygame.draw.polygon(s, (70, 60, 150), [(cx, sz * .40), (sz * .18, sz * .92), (sz * .82, sz * .92)])
    pygame.draw.polygon(s, (45, 38, 110), [(cx, sz * .40), (cx, sz * .92), (sz * .82, sz * .92)])
    pygame.draw.circle(s, (235, 205, 175), (cx, int(sz * .40)), int(sz * .13))
    pygame.draw.polygon(s, (40, 32, 90), [(cx, sz * .05), (sz * .30, sz * .46), (sz * .70, sz * .46)])
    pygame.draw.circle(s, (250, 235, 120), (cx, int(sz * .18)), max(2, sz // 16))
    pygame.draw.line(s, (150, 110, 70), (int(sz * .80), int(sz * .30)), (int(sz * .86), int(sz * .95)), max(2, sz // 18))
    pygame.draw.circle(s, (120, 220, 255), (int(sz * .80), int(sz * .28)), max(2, sz // 12))
    return s


def _monster(sz):
    s = pygame.Surface((sz, sz), pygame.SRCALPHA)
    cx = sz // 2
    pygame.draw.circle(s, (190, 50, 45), (cx, int(sz * .55)), int(sz * .34))
    pygame.draw.circle(s, (130, 30, 28), (cx, int(sz * .55)), int(sz * .34), 2)
    pygame.draw.polygon(s, (235, 220, 200), [(sz * .30, sz * .30), (sz * .20, sz * .10), (sz * .40, sz * .26)])
    pygame.draw.polygon(s, (235, 220, 200), [(sz * .70, sz * .30), (sz * .80, sz * .10), (sz * .60, sz * .26)])
    for ex in (.40, .60):
        pygame.draw.circle(s, (250, 240, 120), (int(sz * ex), int(sz * .52)), max(2, sz // 12))
        pygame.draw.circle(s, (20, 0, 0), (int(sz * ex), int(sz * .52)), max(1, sz // 28))
    return s


def _soldier(sz, rgb):
    s = pygame.Surface((sz, sz), pygame.SRCALPHA)
    cx = sz // 2
    dk = shade(rgb, .55)
    pygame.draw.rect(s, rgb, (int(cx - sz * .18), int(sz * .42), int(sz * .36), int(sz * .44)))
    pygame.draw.rect(s, dk, (int(cx - sz * .18), int(sz * .42), int(sz * .36), int(sz * .44)), 2)
    pygame.draw.circle(s, rgb, (cx, int(sz * .34)), int(sz * .16))
    pygame.draw.circle(s, dk, (cx, int(sz * .34)), int(sz * .16), 2)
    pygame.draw.line(s, (200, 200, 210), (int(sz * .76), int(sz * .14)), (int(sz * .76), int(sz * .92)), 2)
    pygame.draw.polygon(s, (220, 220, 230), [(int(sz * .76), int(sz * .08)), (int(sz * .70), int(sz * .20)), (int(sz * .82), int(sz * .20))])
    return s


def _townsperson(sz, rgb):
    s = pygame.Surface((sz, sz), pygame.SRCALPHA)
    cx = sz // 2
    pygame.draw.rect(s, shade(rgb, .8), (int(cx - sz * .14), int(sz * .46), int(sz * .28), int(sz * .40)))
    pygame.draw.circle(s, (235, 205, 175), (cx, int(sz * .38)), int(sz * .13))
    return s


def _keep(sz, rgb):
    s = pygame.Surface((sz, sz), pygame.SRCALPHA)
    pygame.draw.rect(s, shade(rgb, .9), (int(sz * .18), int(sz * .30), int(sz * .64), int(sz * .60)))
    for bx in (.16, .40, .64):                      # battlements
        pygame.draw.rect(s, shade(rgb, .9), (int(sz * bx), int(sz * .18), int(sz * .20), int(sz * .18)))
    pygame.draw.rect(s, (40, 30, 20), (int(sz * .42), int(sz * .58), int(sz * .16), int(sz * .32)))
    return s


def _house(sz, rgb):
    s = pygame.Surface((sz, sz), pygame.SRCALPHA)
    pygame.draw.rect(s, (150, 120, 90), (int(sz * .26), int(sz * .48), int(sz * .48), int(sz * .40)))
    pygame.draw.polygon(s, shade(rgb, .9), [(int(sz * .20), int(sz * .50)), (int(sz * .50), int(sz * .22)), (int(sz * .80), int(sz * .50))])
    return s


def _pedlar(sz):
    s = pygame.Surface((sz, sz), pygame.SRCALPHA)
    pygame.draw.rect(s, (180, 140, 70), (int(sz * .22), int(sz * .40), int(sz * .56), int(sz * .40)))
    pygame.draw.circle(s, (60, 50, 40), (int(sz * .34), int(sz * .84)), int(sz * .10))
    pygame.draw.circle(s, (60, 50, 40), (int(sz * .66), int(sz * .84)), int(sz * .10))
    pygame.draw.polygon(s, (230, 210, 120), [(int(sz * .22), int(sz * .40)), (int(sz * .5), int(sz * .22)), (int(sz * .78), int(sz * .40))])
    return s


# ---------------- sprites (pixel-art textures, hybrid swap) ----------------
ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "kenney_rpg")


def load_textures():
    """Load the small set of Kenney CC0 textures swapped in for ground/water/bushes.
    Missing/unloadable files fall back to None so callers keep the old procedural drawing."""
    names = ("grass_flat", "dirt_flat", "water_flat", "bush_small_green")
    tex = {}
    for name in names:
        try:
            img = pygame.image.load(os.path.join(ASSET_DIR, f"{name}.png")).convert_alpha()
            tex[name] = pygame.transform.smoothscale(img, (CELL, CELL))
        except Exception:
            tex[name] = None
    return tex


def draw_feature(surf, glyph, rect, tex=None):
    x, y, w, h = rect
    cx, cy = x + w // 2, y + h // 2
    if glyph == "♣":
        if tex and tex.get("bush_small_green"):
            surf.blit(tex["bush_small_green"], (x, y))
        else:
            pygame.draw.rect(surf, (90, 60, 35), (cx - 2, cy + 2, 4, h // 3))
            pygame.draw.circle(surf, (38, 120, 52), (cx, cy), w // 3)
            pygame.draw.circle(surf, (28, 95, 42), (cx, cy), w // 3, 2)
    elif glyph == "▲":
        pygame.draw.polygon(surf, (140, 140, 150), [(cx, y + 3), (x + 3, y + h - 3), (x + w - 3, y + h - 3)])
        pygame.draw.polygon(surf, (235, 235, 245), [(cx, y + 3), (cx - 4, y + 12), (cx + 4, y + 12)])
    elif glyph == "●":
        pygame.draw.circle(surf, (150, 140, 120), (cx, cy), w // 3)
        pygame.draw.circle(surf, (110, 100, 85), (cx, cy), w // 3, 2)
    elif glyph == "≈":
        for i in (-1, 0, 1):
            pygame.draw.arc(surf, (150, 215, 235), (x + 5, cy + i * 6 - 4, w - 10, 8), 3.5, 6.0, 2)
    elif glyph == "❀":
        pygame.draw.circle(surf, (235, 120, 170), (cx, cy), max(2, w // 7))
        pygame.draw.circle(surf, (250, 230, 120), (cx, cy), max(1, w // 16))


def short(fac):
    return d.POWERS[fac][0].split()[0]


def is_hostile(state, m):
    return m["fac"] < 0 or state["favor"][m["fac"]] <= WAR_FAVOR


def _unit(x, y, fac):
    d0 = random.choice(CARDINALS)
    return {"x": x, "y": y, "fac": fac, "dir": d0, "face": d0,
            "leg": random.uniform(*MON_LEG), "cool": random.uniform(*MON_FIRE_CD), "hp": MON_HP}


def scatter(cells, fac, lo, hi, n, avoid_center=False):
    out, tries = [], 0
    while len(out) < n and tries < 500:
        tries += 1
        r, c = random.uniform(lo, hi), random.uniform(2, TILE - 3)
        if cells[int(r)][int(c)][0] in SOLID:
            continue
        if avoid_center and math.hypot(r - TILE / 2, c - TILE / 2) < 5:
            continue
        out.append(_unit(c, r, fac))
    return out


# ---------------- the game ----------------
class Game:
    def __init__(self):
        pygame.init()
        self.screen = pygame.display.set_mode((W, H))
        pygame.display.set_caption("Dominion")
        self.clock = pygame.time.Clock()
        self.f = pygame.font.SysFont("monospace", 14)
        self.fb = pygame.font.SysFont("monospace", 18, bold=True)
        self.fbig = pygame.font.SysFont("monospace", 22, bold=True)
        self.fs = pygame.font.SysFont("monospace", 11)   # small: map town labels
        self.spr_wiz = _wizard(int(CELL * .95))
        self.spr_mon = _monster(int(CELL * .95))
        self.spr_sol = [_soldier(int(CELL * .95), c) for c in FACTION_RGB]
        self.spr_npc = [_townsperson(int(CELL * .8), c) for c in FACTION_RGB]
        self.spr_keep = [_keep(int(CELL * .95), c) for c in FACTION_RGB]
        self.spr_house = [_house(int(CELL * .95), c) for c in FACTION_RGB]
        self.spr_pedlar = _pedlar(int(CELL * .9))
        self.tex = load_textures()
        self.music_on = self.init_music()

        self.growth_model = d.DEFAULT_CORRUPTION_GROWTH   # corruption-growth model for new worlds
        self.state = d.fresh_state(corruption_growth=self.growth_model)
        # Dev-controls order (pygame layout): faction levers grouped per faction,
        # then the inter-faction relations pulled out into one BATTLES section —
        # each pair once (hearts are symmetric), not nested+duplicated per faction.
        self.ctrls = [("shadow",)]
        for i in range(d.N):
            self.ctrls += [("terr", i), ("army", i), ("corrupt", i)]
        for i in range(d.N):
            for j in range(i + 1, d.N):
                self.ctrls.append(("heart", i, j))
        self.view = "tile"
        self.sel = 0
        self.playing = False
        self.speed = 4
        self.auto_acc = 0.0
        self.commission = None
        self.talk = None         # active reassign-a-recruit modal ('t')
        self.char_open = False   # the wizard's character window ('c')
        self.journal = False     # the quest journal overlay ('j') — viewable any time
        self.map_mode = "world"  # map view: "world" (geography) or "powers" (factions), toggle 'g'
        self.char_sel = 0
        self.charging = None     # None, or seconds held to charge a heavy bolt
        self.chat = None         # active chat window with a townsperson/wanderer
        self.slumber = None      # active slumber menu (choose how long to sleep)
        self.beset_ids = set()   # recruit ids beset by monsters on the wizard's current tile
        self.creature = None     # a mythical creature dwelling on this tile (artifact quest-giver)
        self.sower_npc = None    # the sower, disguised as an ordinary folk on this tile (or None)
        self.led = None          # the seeker the wizard is escorting in person (or None)
        self.dmg_parity = 0      # counts blows landed, so the Veilcloak can turn aside every other
        self.tile_solid = [[False] * TILE for _ in range(TILE)]   # geo collision mask (per tile)
        self.geo_paint = [[None] * TILE for _ in range(TILE)]     # geo terrain paint (per tile)
        self.facing = (0, 1)
        self.last_axis = "v"
        self.bolts, self.eshots = [], []
        self.loot = ("", 0.0)
        self.invuln = 0.0
        self.victory = 0.0      # age-victory banner timer (seconds)
        self.enter_tile()

    # ----- music (optional; loops the first track in music/) -----
    # Muted by default: the track is loaded but not played until the player presses
    # 'p'. Returns False (the initial music_on state) so the game starts silent.
    def init_music(self):
        self.has_track = False
        self.music_started = False
        try:
            pygame.mixer.init()
            tracks = sorted(glob.glob(os.path.join("music", "*.wav")))
            if not tracks:
                return False
            pygame.mixer.music.load(tracks[0])
            self.has_track = True
        except Exception:
            pass
        return False  # muted by default

    def toggle_music(self):
        if not getattr(self, "has_track", False):
            return
        try:
            if self.music_on:
                pygame.mixer.music.pause(); self.music_on = False
            else:
                if self.music_started:
                    pygame.mixer.music.unpause()
                else:
                    pygame.mixer.music.play(-1); self.music_started = True
                self.music_on = True
        except Exception:
            pass

    # ----- tile setup -----
    def champion_seat(self):
        """(faction, (r,c)) of the dark lieutenant at his *current* tile — his throne
        when brooding, or wherever he has ridden to. (None, None) when no power is
        fallen (the age is at peace). The boss materialises wherever he stands."""
        lord = self.state.get("lord")
        if lord is None or d.dark_champion(self.state) is None:
            return None, None
        return lord["fac"], (lord["r"], lord["c"])

    def _can_apprehend(self):
        # Apprehending the lord in absentia only makes sense once his whereabouts are
        # actually known — the same Wits sense that reveals him on the map.
        st = self.state
        return (st.get("lord") is not None
                and st["wiz_stats"][2] >= d.WIZ_WITS_SENSE_AT)

    def make_boss(self, fac):
        cor = self.state["corruption"][fac]
        hp = int(BOSS_HP + cor * 18)                       # the further fallen, the tougher
        return {"x": TILE / 2.0, "y": 3.0, "fac": fac, "boss": True,
                "dir": (0, 0), "face": (0, 1), "leg": 0.0, "cool": 1.2,
                "hp": hp, "maxhp": hp}

    def make_guardian(self, art):
        # The mini-boss guarding an artifact's resting place. Behaves like the lord's
        # boss (chase + aimed bolts) but is its own creature (fac -1) and, when felled,
        # yields the artifact rather than winning the age.
        return {"x": TILE / 2.0, "y": 3.0, "fac": -1, "guardian": True, "art": art,
                "dir": (0, 0), "face": (0, 1), "leg": 0.0, "cool": 1.0,
                "hp": GUARDIAN_HP, "maxhp": GUARDIAN_HP}

    def make_agent_boss(self, ag):
        # One of the lord's retinue, caught in the field (sowing or marching to a
        # front). Same chase+aim AI as the lieutenant's boss, but weaker and yields
        # only a kill, not the age — slaying it just frees a slot in the retinue.
        return {"x": TILE / 2.0, "y": 3.0, "fac": -1, "lord_agent": True, "agent_id": ag["id"],
                "dir": (0, 0), "face": (0, 1), "leg": 0.0, "cool": 1.0,
                "hp": AGENT_HP, "maxhp": AGENT_HP}

    def make_sower_boss(self):
        # The sower himself, caught in the field sowing corruption. Same chase+aim AI;
        # slaying him doesn't end the dark economy, only pauses it (slay_sower starts
        # his shadow-lands recovery cooldown) before he resumes elsewhere.
        return {"x": TILE / 2.0, "y": 3.0, "fac": -1, "sower": True,
                "dir": (0, 0), "face": (0, 1), "leg": 0.0, "cool": 1.0,
                "hp": AGENT_HP, "maxhp": AGENT_HP}

    def expose_sower(self):
        # The wizard corners the disguised stranger and the mask falls away. Turns this
        # tile hostile on the spot — no separate re-entry needed. Walking away without
        # killing him costs nothing (slay_sower, the only penalty/respite trigger, fires
        # only on an actual kill); he simply resumes whatever he was doing.
        st = self.state
        wr, wc = st["wizard"]["r"], st["wizard"]["c"]
        self.mode = "sower"
        self.scenery = None
        self.sower_npc = None
        base = d.spawn_monsters(st, wr, wc)
        self.units = scatter(self.cells, -1, 2, TILE - 3, max(1, len(base) // 2), avoid_center=True)
        self.units.append(self.make_sower_boss())
        self.msg("You corner the stranger — and the mask falls away. The sower!")

    def make_ally(self, q, x, y):
        # The escorted seeker as a fighting ally: auto-fires at monsters, has Endurance
        # hearts (persisted on the recruit as q["hp"]).
        mhp = q.get("maxhp") or max(1, q["stats"][1])
        q["maxhp"] = mhp
        return {"x": x, "y": y, "ally": True, "q": q, "fac": q["fac"],
                "hp": q.get("hp", mhp), "maxhp": mhp,
                "cool": random.uniform(0.4, 0.9), "face": (0, 1)}

    def update_ally(self, m, dt, alive):
        # Follow the wizard loosely; fire at the nearest monster on a cooldown.
        q = m["q"]
        foes = [u for u in self.units if u.get("fac", 0) < 0]
        target = min(foes, key=lambda u: (u["x"] - m["x"]) ** 2 + (u["y"] - m["y"]) ** 2,
                     default=None)
        tx, ty = (target["x"], target["y"]) if target else (self.px, self.py)
        # keep within a few cells of the wizard
        dx, dy = self.px - m["x"], self.py - m["y"]
        if abs(dx) > 2.5 or abs(dy) > 2.5:
            step = (1 if dx > 0 else -1, 0) if abs(dx) > abs(dy) else (0, 1 if dy > 0 else -1)
            nx, ny = m["x"] + step[0] * ALLY_SPEED * dt, m["y"] + step[1] * ALLY_SPEED * dt
            if not self.blocked(nx, ny):
                m["x"], m["y"] = nx, ny
        m["cool"] -= dt
        if foes and m["cool"] <= 0:
            m["cool"] = random.uniform(*ALLY_FIRE_CD)
            ang = math.atan2(ty - m["y"], tx - m["x"])
            pw = max(1, q["stats"][0] // 3)                  # a third of their Might
            self.bolts.append([m["x"], m["y"], math.cos(ang), math.sin(ang), pw, False, set()])

    def ally_down(self, m):
        # The ally's hearts are spent. Endurance decides: a tough one is knocked DOWN
        # (revivable by camping); a frail one is slain. (Your "(a)/(b) dice on Endurance".)
        st, q = self.state, m["q"]
        q["hp"] = 0
        if random.randint(1, 10) <= q["stats"][1]:           # Endurance vs the brink
            q["downed"] = True
            self.msg(f"{d.recruit_name(q)} is struck down — camp [z] to revive them, or lose them.")
        else:
            st["recruits"] = [x for x in st["recruits"] if x["id"] != q["id"]]
            self.led = None
            self.msg(f"{d.recruit_name(q)} is slain at your side.")

    def claim_artifact(self, art):
        # The guardian is felled — the prize is the wizard's. Staves go into his wieldable
        # set (equipped if his Might allows); the charge artifact teaches the heavy bolt.
        # The guardian IS one of the lord's retinue, lent out to the artifact's watch —
        # felling it pulls that agent off the board for a time, same as catching one in
        # the open field (slay_lord_agent starts the recruit cooldown).
        st = self.state
        agents = st.get("lord_agents", [])
        if agents:
            d.slay_lord_agent(st, random.choice(agents)["id"])
            self.msg("One of the lord's retinue, set to guard it, is struck down — "
                     "gone from the board for a time.")
        art["found"] = True
        if art.get("grants") == "charge":
            st["can_charge"] = True
            self.msg(f"The guardian falls! You master {art['name']} — hold [space] to "
                     f"wind up a heavy, piercing bolt.")
        elif art.get("grants") == "cloak":
            st["has_cloak"] = True
            self.msg(f"The guardian falls! You don {art['name']} — it turns aside half "
                     f"of all harm.")
        else:
            st["staves_found"].add(art["staff"])
            need = STAVES[art["staff"]]["equip_might"]
            if st["wiz_stats"][0] >= need:
                st["wiz_staff"] = art["staff"]
                self.msg(f"The guardian falls! You claim the {art['name']} — and wield it. ([v] swap)")
            else:
                self.msg(f"The guardian falls! You claim the {art['name']} — but need Might {need} "
                         f"to wield it (raise it, then [v]).")
        self.enter_tile((self.px, self.py))   # guardian gone, the watch thins

    def hurt(self):
        # Apply one blow to the wizard. The Veilcloak (an artifact) turns aside every
        # other blow — halving harm taken. Either way he gets i-frames (INVULN).
        st = self.state
        if st.get("has_cloak"):
            self.dmg_parity ^= 1
            if self.dmg_parity == 1:                # this blow is turned aside
                self.invuln = INVULN
                self.loot = ("cloak turns it!", 0.8)
                return
        st["health"] -= 1
        self.invuln = INVULN

    def update_boss(self, m, dt, alive):
        """The lieutenant chases the wizard (cardinal, per-axis) and looses fast,
        aimed bolts on a short cooldown; its body hurts on contact."""
        st = self.state
        if not is_hostile(st, m):
            return                      # the lord, met in peace — he simply broods, no threat
        dx, dy = self.px - m["x"], self.py - m["y"]
        step = (1 if dx > 0 else -1, 0) if abs(dx) > abs(dy) else (0, 1 if dy > 0 else -1)
        m["face"] = step
        nx, ny = m["x"] + step[0] * BOSS_SPEED * dt, m["y"] + step[1] * BOSS_SPEED * dt
        if 1 <= nx <= TILE - 2:
            m["x"] = nx
        if 1 <= ny <= TILE - 2:
            m["y"] = ny
        m["cool"] -= dt
        if alive and m["cool"] <= 0:
            m["cool"] = random.uniform(*BOSS_FIRE_CD)
            dist = math.hypot(dx, dy) or 1.0
            f = BOSS_PROJ_SPEED / MON_PROJ_SPEED          # ride the shared eshot speed, scaled up
            self.eshots.append([m["x"], m["y"], dx / dist * f, dy / dist * f])
        if alive and self.invuln <= 0 and math.hypot(dx, dy) <= BOSS_TOUCH:
            self.hurt()

    def win_age(self, fac):
        """A lieutenant is struck down in person. Like any other character, whether he
        truly falls is an Endurance dice roll (lord_survives_roll, the same rule as a
        fighting companion's ally_down): a tough lord is only cast back to the shadow
        lands to recover; a frail one is destroyed outright and his clan can never raise
        a lord again. The age is only *won* once no power remains fallen — if the
        shadow still holds another clan, a fresh champion stands and the hunt goes on."""
        st = self.state
        lord = st.get("lord")
        survives = lord is not None and d.lord_survives_roll(lord)
        d.rout_champion(st, fac, survives=survives)
        self.eshots = []
        nxt = d.dark_champion(st)
        fate = "is cast back to the shadow lands to recover" if survives else "is destroyed outright"
        if nxt is None:
            self.victory = 7.0
            self.msg(f"{short(fac)}'s dark champion {fate} — the land is cleansed and the "
                     f"age's peace returns. Slumber [z] when you wish.")
        else:
            self.msg(f"{short(fac)}'s champion {fate} and its land is cleansed — but the shadow "
                     f"still holds {short(nxt)}. Hunt its lieutenant down.")
        self.enter_tile((self.px, self.py))   # the seat is liberated; townsfolk return

    def enter_tile(self, entry=None):
        st = self.state
        wr, wc = st["wizard"]["r"], st["wizard"]["c"]
        enemy = d.tile_enemy(st, wr, wc)
        town = st["towns"].get((wr, wc))
        champ, lpos = self.champion_seat()
        on_lord = champ is not None and (wr, wc) == lpos    # standing where the lord is
        self.npcs, self.buildings = [], []
        if on_lord and town is not None:
            # The lieutenant broods in his clan's stronghold (the keep), townsfolk gone.
            cells, _t, self.buildings, self.npcs = d.tile_town(st, wr, wc)
            self.npcs = []
        elif on_lord:
            cells = d.tile_terrain(st, wr, wc)[0]           # caught in the open field
        elif town is not None and enemy is None:
            cells, _t, self.buildings, self.npcs = d.tile_town(st, wr, wc)
        else:
            cells = d.tile_terrain(st, wr, wc)[0]
        self.cells = cells
        self.build_geo_terrain()    # the micro-terrain window (collision + paint) for this tile
        self.creature = None
        art_loc = d.artifact_loc_at(st, wr, wc)      # gear rests here, guarded (told, unfound)
        art_cre = d.artifact_creature_at(st, wr, wc)  # a mythical creature dwells here (untold)
        agent = d.lord_agent_at(st, wr, wc) if not on_lord else None  # a retinue mini-boss, caught
        sower_here = d.sower_at(st, wr, wc) if not on_lord else None  # the sower himself, caught
        wo = st["owner"][wr][wc]
        if on_lord:
            self.mode = "boss"
            self.lo_fac, self.hi_fac = wo, champ
            self.units = [self.make_boss(champ)]
        elif enemy is not None:
            self.mode = "contested"
            self.lo_fac, self.hi_fac = wo, enemy
            self.units = scatter(cells, wo, TILE / 2 + 1, TILE - 3, HOST_SIZE) \
                + scatter(cells, enemy, 2, TILE / 2 - 1, HOST_SIZE)
        elif art_loc is not None and town is None:
            # The artifact's resting place: a thick watch of minions + the mini-boss.
            self.mode = "guardian"
            base = d.spawn_monsters(st, wr, wc)
            self.units = scatter(cells, -1, 2, TILE - 3, len(base), avoid_center=True)
            self.units.append(self.make_guardian(art_loc))
        elif art_cre is not None and town is None:
            # The mythical quest-giver dwells here — peaceful; walk up and [e] to hear it.
            self.mode = "peaceful"
            self.units = []
            self.creature = art_cre
        elif agent is not None and town is None:
            # One of the lord's retinue, caught sowing or marching to a front — a
            # lesser mini-boss watched over by a thinner guard than the lord's own seat.
            self.mode = "agent"
            base = d.spawn_monsters(st, wr, wc)
            self.units = scatter(cells, -1, 2, TILE - 3, max(1, len(base) // 2), avoid_center=True)
            self.units.append(self.make_agent_boss(agent))
        else:
            # spawn_monsters returns sentries for a fallen tile, or the odd wild lair
            # out in the deep country; [] for a town tile or safe ground.
            base = d.spawn_monsters(st, wr, wc) if town is None else []
            if base:
                self.mode = "peaceful"
                self.units = scatter(cells, -1, 2, TILE - 3, len(base), avoid_center=True)
            else:
                self.mode = "town" if town is not None else "peaceful"
                self.units = []
        # Roll a fresh crowd's stats for this visit (aligned with self.npcs; [] off
        # a town or a contested one). Re-rolled every time he enters the tile.
        self.folk = d.roll_townsfolk(st, wr, wc)
        # Ambient scenery for a peaceful, town-less, un-fallen tile: houses/farms/folk
        # that thicken as you near a town, so the open world feels lived-in.
        self.scenery = (self.tile_scenery(wr, wc)
                        if town is None and self.mode == "peaceful" and not self.units else None)
        # The sower, caught sowing here, walks among ordinary folk unnoticed by default —
        # he's just another wanderer in the scenery, with no hostility, until the wizard
        # walks up and exposes him (needs WIZ_WITS_SOWER_AT; see nearby_person/expose_sower).
        # Striking him down only buys a respite (slay_sower); leaving him unexposed, or
        # exposed-but-unkilled, costs nothing — he simply goes back to what he was doing.
        self.sower_npc = None
        if sower_here is not None and self.scenery is not None:
            rng = random.Random((st["world_seed"] * 7919 ^ (wr * d.GRID + wc) ^ 0xC0DE) & 0xFFFFFFFF)
            fk = [float(rng.randint(3, TILE - 5)), float(rng.randint(3, TILE - 4)), 0.0, 0.0, "wander"]
            self.scenery["folk"].append(fk)
            self.sower_npc = fk
        # Catching an agent in open country: each one is beset (peril roll) by monsters
        # right now — a fight you can join. Clear the beasts to save them; leave and they
        # face it alone. (A town is refuge; a fallen tile's monsters serve as the beasts.)
        self.beset_ids = set()
        if self.mode == "peaceful" and town is None:
            for q in self.recruits_here():
                if (not q.get("led") and q["task"] in ("seek", "march")
                        and random.random() < d.recruit_peril(st, q)):
                    self.beset_ids.add(q["id"])
            if self.beset_ids:
                if not self.units:
                    self.units = scatter(self.cells, -1, 2, TILE - 3,
                                         len(self.beset_ids) + 1, avoid_center=True)
                self.msg(f"{len(self.beset_ids)} of your agents are beset — drive off the beasts!")
        # Escorting a seeker in person: they travel with you, and the journey's danger is
        # realised as combat — a watch (and, on hard roads, a warden) sized by the quest's
        # difficulty %, the same figure that sets its find-odds (macro % → wizard view).
        self.led = d.led_seeker(st)
        if self.led is not None:
            self.led["r"], self.led["c"] = wr, wc
            if self.mode == "peaceful" and town is None and not self.led.get("downed"):
                diff = d.quest_difficulty(st, self.led)
                if not any(m["fac"] < 0 for m in self.units):
                    n = int(round(diff * d.MONSTER_MAX))
                    if n:
                        self.units += scatter(cells, -1, 2, TILE - 3, n, avoid_center=True)
                if random.random() < diff * 0.35:          # a road warden bars the way
                    self.units.append(self.make_guardian(None))
        # Keep units (monsters/soldiers) off water/rock cells.
        self.units = [m for m in self.units
                      if not self.tile_solid[min(TILE - 1, max(0, int(m["y"])))][min(TILE - 1, max(0, int(m["x"])))]]
        if self.led is not None and not self.led.get("downed"):   # place the ally on open ground
            ax, ay = self.nearest_open(TILE / 2 - 1.5, TILE / 2)
            self.units.append(self.make_ally(self.led, ax, ay))
        self.tile_seconds = 0.0
        self.ctimer = 0.0
        if entry:
            self.px, self.py = entry
        else:
            self.px = self.py = TILE / 2.0
        if self.blocked(self.px, self.py):   # landed on water/rock -> step onto nearest land
            self.px, self.py = self.nearest_open(self.px, self.py)
        self.bolts, self.eshots = [], []
        self.charging = None        # drop any half-held charge when the scene changes

    # ----- collision / crossing -----
    GEO_PAINT_NAME = {d.GEO_WATER: "water", d.GEO_MOUNTAIN: "rock", d.GEO_ROAD: "road",
                      d.GEO_BRIDGE: "bridge", d.GEO_PASS: "pass"}

    def build_geo_terrain(self):
        # The wizard's traversal terrain for THIS tile is just the 20×20 WINDOW of the
        # global micro geo grid at (wr,wc): collision mask self.tile_solid (water/rock)
        # + paint map self.geo_paint drawn over the cosmetic tile_terrain. Because it is
        # one slice of a single continuous grid, rivers/roads line up across tile edges
        # for free. Decorative tile_terrain never collides — only this layer does.
        st = self.state
        self.tile_solid = [[False] * TILE for _ in range(TILE)]
        self.geo_paint = [[None] * TILE for _ in range(TILE)]
        geo = st.get("geo")
        if geo is None:
            return
        MW = st["geo_w"]
        br, bc = st["wizard"]["r"] * TILE, st["wizard"]["c"] * TILE
        name = self.GEO_PAINT_NAME
        for ly in range(TILE):
            row = (br + ly) * MW + bc
            solid_row, paint_row = self.tile_solid[ly], self.geo_paint[ly]
            for lx in range(TILE):
                code = geo[row + lx]
                if code == d.GEO_WATER or code == d.GEO_MOUNTAIN:
                    solid_row[lx] = True
                paint_row[lx] = name.get(code)        # None for plain land

    def nearest_open(self, x, y):
        # Nearest non-solid cell-centre to (x,y) within the tile (for spawn placement).
        if not self.blocked(x, y):
            return x, y
        best, bd = None, 1e9
        for r in range(TILE):
            for c in range(TILE):
                if not self.tile_solid[r][c]:
                    cx, cy = c + 0.5, r + 0.5
                    dd = (cx - x) ** 2 + (cy - y) ** 2
                    if dd < bd:
                        best, bd = (cx, cy), dd
        return best or (x, y)

    def solid_at(self, x, y):
        # Only the geo terrain layer (build_geo_terrain) blocks. Decorative tile_terrain
        # stays passable so the wizard can always reach a screen edge to cross.
        ix, iy = int(x), int(y)
        if ix < 0 or iy < 0 or ix >= TILE or iy >= TILE:
            return False        # out of bounds — let edge-crossing handle it
        return self.tile_solid[iy][ix]

    def blocked(self, cx, cy):
        return any(self.solid_at(cx + ox, cy + oy) for ox in (-RAD, RAD) for oy in (-RAD, RAD))

    def cross(self, dr, dc):
        st = self.state
        wiz = st["wizard"]
        nr = max(0, min(d.GRID - 1, wiz["r"] + dr))
        nc = max(0, min(d.GRID - 1, wiz["c"] + dc))
        # World edge, or a river/mountain wall (no crossing): clamp, no cross. (Open
        # edges only ever border walkable tiles, so this mainly guards corner cases.)
        if (nr, nc) == (wiz["r"], wiz["c"]) or not d.tile_walkable(st, nr, nc):
            self.px = min(max(self.px, RAD), TILE - RAD)
            self.py = min(max(self.py, RAD), TILE - RAD)
            return
        if self.beset_ids:                                 # you leave beset agents to it
            slain = []
            for q in list(st["recruits"]):
                if q["id"] in self.beset_ids and d._recruit_encounter(st, q) == "slain":
                    slain.append(q["id"])
            if slain:
                st["recruits"] = [q for q in st["recruits"] if q["id"] not in slain]
            self.beset_ids = set()
        led = d.led_seeker(st)                             # abandoning a downed companion is fatal
        if led is not None and led.get("downed"):
            st["recruits"] = [q for q in st["recruits"] if q["id"] != led["id"]]
            self.msg(f"You leave #{led['id']} where they fell — they do not survive.")
        if self.mode != "contested":                       # bank the wall-clock spent here
            days = math.ceil(self.tile_seconds / d.SECONDS_PER_DAY)
            if days > 0:
                d.advance_days(st, days)
        st["pedlar_here"] = random.random() < d.PEDLAR_CHANCE
        wiz["r"], wiz["c"] = nr, nc
        ex = TILE - RAD - 0.02 if dc < 0 else RAD + 0.02 if dc > 0 else self.px
        ey = TILE - RAD - 0.02 if dr < 0 else RAD + 0.02 if dr > 0 else self.py
        self.enter_tile((min(max(ex, RAD), TILE - RAD), min(max(ey, RAD), TILE - RAD)))

    # ----- world advancement -----
    def step_cycle(self):
        d.advance_days(self.state, d.DAYS_PER_CYCLE)

    def contested_tick(self):
        st = self.state
        bat = next((b for b in st["battles"]
                    if {b["winner"], b["loser"]} == {self.lo_fac, self.hi_fac}), None)
        if bat and st["favor"][self.lo_fac] != st["favor"][self.hi_fac]:
            lo, hi = self.lo_fac, self.hi_fac
            helped, opp = (lo, hi) if st["favor"][lo] > st["favor"][hi] else (hi, lo)
            st["aid"] = {"helped": helped, "opp": opp}
            st["aid_log"].setdefault(tuple(sorted((lo, hi))), {})
        self.step_cycle()
        # re-evaluate the front: front may have ended, else replenish hosts to cap
        wr, wc = st["wizard"]["r"], st["wizard"]["c"]
        if d.tile_enemy(st, wr, wc) is None:
            self.enter_tile((self.px, self.py))            # front quieted -> normal tile
            return
        for fac, lo, hi in ((self.lo_fac, TILE / 2 + 1, TILE - 3), (self.hi_fac, 2, TILE / 2 - 1)):
            have = sum(1 for m in self.units if m["fac"] == fac)
            if have < HOST_SIZE:
                self.units += scatter(self.cells, fac, lo, hi, HOST_SIZE - have)

    # ----- input -----
    def on_key(self, e):
        st = self.state
        if self.commission is not None:
            self.handle_commission(e)
            return
        if self.char_open:
            self.handle_char(e)
            return
        if self.talk is not None:
            self.handle_talk(e)
            return
        if self.chat is not None:
            self.handle_chat(e)
            return
        if self.slumber is not None:           # slumber menu is open
            self.handle_slumber(e)
            return
        if self.journal:                       # quest journal is open — any key closes it
            self.journal = e.key not in (pygame.K_j, pygame.K_ESCAPE)
            return
        k = e.key
        if k in (pygame.K_LEFT, pygame.K_a, pygame.K_RIGHT, pygame.K_d):
            self.last_axis = "h"
        elif k in (pygame.K_UP, pygame.K_w, pygame.K_DOWN, pygame.K_s):
            self.last_axis = "v"
        if k in (pygame.K_q, pygame.K_ESCAPE):
            self.running = False
        elif k in (pygame.K_m, pygame.K_TAB):
            self.view = {"tile": "map", "map": "controls", "controls": "tile"}[self.view]
            if self.view == "tile":
                self.enter_tile()
        elif k == pygame.K_r:
            self.state = st = d.fresh_state(corruption_growth=self.growth_model)
            self.playing = False
            self.enter_tile()
        elif k == pygame.K_o:                  # toggle corruption-growth model + fresh world
            self.growth_model = "sown" if self.growth_model == "ambient" else "ambient"
            self.state = st = d.fresh_state(corruption_growth=self.growth_model)
            self.playing = False
            self.enter_tile()
            self.msg(f"Corruption model: {self.growth_model.upper()} — new world.")
        elif k == pygame.K_RIGHTBRACKET:
            self.speed = min(10, self.speed + 1)
        elif k == pygame.K_LEFTBRACKET:
            self.speed = max(1, self.speed - 1)
        elif k == pygame.K_p:
            self.toggle_music()
        elif k == pygame.K_c and self.view == "tile":
            self.char_open = True
        elif k == pygame.K_v and self.view == "tile":
            self.cycle_staff()
        elif k == pygame.K_j:                  # quest journal — look at quests any time
            self.journal = True
        elif k == pygame.K_g:                  # map view: swap world-geography / powers
            self.map_mode = "powers" if self.map_mode == "world" else "world"
        elif k == pygame.K_l:
            st["map_live"] = not st["map_live"]
            self.msg(f"Live map (testing): {'ON' if st['map_live'] else 'OFF'}")
        elif self.view != "tile" and k == pygame.K_SPACE:
            self.playing = not self.playing
        elif self.view == "tile" and k == pygame.K_SPACE:
            if st["health"] > 0:
                if st.get("can_charge") and st["wiz_stats"][0] >= d.WIZ_MIGHT_CHARGE_AT:
                    self.charging = 0.0           # charge learned (the quest) + Might — KEYUP looses
                else:
                    self.fire_bolt()              # no charge yet — a basic bolt
        elif self.view == "controls":
            if k in (pygame.K_UP, pygame.K_w):
                self.sel = (self.sel - 1) % len(self.ctrls)
            elif k in (pygame.K_DOWN, pygame.K_s):
                self.sel = (self.sel + 1) % len(self.ctrls)
            elif k in (pygame.K_RIGHT, pygame.K_d, pygame.K_EQUALS, pygame.K_PLUS):
                d.adjust(st, self.ctrls[self.sel], +1)
            elif k in (pygame.K_LEFT, pygame.K_a, pygame.K_MINUS):
                d.adjust(st, self.ctrls[self.sel], -1)
        elif self.view == "map" and k in (pygame.K_UP, pygame.K_DOWN, pygame.K_LEFT, pygame.K_RIGHT):
            wiz = st["wizard"]
            dr = -1 if k == pygame.K_UP else 1 if k == pygame.K_DOWN else 0
            dc = -1 if k == pygame.K_LEFT else 1 if k == pygame.K_RIGHT else 0
            pr, pc = wiz["r"], wiz["c"]
            nr = max(0, min(d.GRID - 1, wiz["r"] + dr))
            nc = max(0, min(d.GRID - 1, wiz["c"] + dc))
            if d.tile_walkable(st, nr, nc):                # don't park on a river/range
                wiz["r"], wiz["c"] = nr, nc
            if (wiz["r"], wiz["c"]) != (pr, pc):
                st["pedlar_here"] = random.random() < d.PEDLAR_CHANCE
                self.step_cycle()
        elif self.view == "tile":
            self.on_tile_key(k)

    def on_tile_key(self, k):
        st = self.state
        wr, wc = st["wizard"]["r"], st["wizard"]["c"]
        if k in (pygame.K_1, pygame.K_2) and self.mode == "contested":
            wo, enemy = self.lo_fac, self.hi_fac
            if st["energy"] < d.AID_ENERGY:
                self.msg("Too exhausted to aid — eat [f] or slumber [z].")
            else:
                helped = wo if k == pygame.K_1 else enemy
                opp = enemy if k == pygame.K_1 else wo
                key = tuple(sorted((wo, enemy)))
                log = st["aid_log"].setdefault(key, {})
                log.setdefault("r", [0, 0])[0 if helped == key[0] else 1] += 1
                st["aid"] = {"helped": helped, "opp": opp}
                st["energy"] = max(0, st["energy"] - d.AID_ENERGY)
                self.step_cycle()
                if d.tile_enemy(st, wr, wc) is None:
                    self.enter_tile((self.px, self.py))
        elif k == pygame.K_e:
            near = self.nearby_person()            # talk to whoever's close — folk or townsperson
            if near is not None and near.get("sower_npc"):
                self.expose_sower()
            else:
                self.chat = near
                if self.chat is None:
                    self.msg("No one close enough to talk to.")
        elif k == pygame.K_f:
            if st["food"] <= 0:
                self.msg("No food — find a merchant, or slumber [z].")
            elif st["energy"] >= d.ENERGY_MAX:
                self.msg("Already at full strength.")
            else:
                st["food"] -= 1
                st["energy"] = min(d.ENERGY_MAX, st["energy"] + d.FOOD_ENERGY)
                self.msg(f"You eat. Energy {round(st['energy'])}/100, {st['food']} food left.")
        elif k == pygame.K_b:
            where = d.merchant_here(st)
            if where is None:
                self.msg("No merchant here.")
            elif st["gold"] < d.FOOD_PRICE:
                self.msg(f"Not enough gold (a ration is {d.FOOD_PRICE}g).")
            else:
                st["gold"] -= d.FOOD_PRICE
                st["food"] += 1
                self.msg(f"Bought a ration from the {'market' if where == 'town' else 'pedlar'}. "
                         f"Food {st['food']}, gold {st['gold']}g.")
        elif k == pygame.K_u:
            self.buy_map()
        elif k == pygame.K_k:
            if (wr, wc) not in st["towns"] or self.mode == "contested":
                self.msg("Find a town at peace to recruit a seeker.")
            elif st["energy"] < d.SEARCH_ENERGY:
                self.msg(f"Too weary to send a seeker (need {d.SEARCH_ENERGY}).")
            elif not self.folk:
                self.msg("No one here to send.")
            elif not st["relics_known"]:
                self.msg("You know of no relics to seek — hear an elder's legend first ([e] to greet).")
            elif len(st["recruits"]) >= d.MAX_RECRUITS:
                self.msg(f"Your following is full ({d.MAX_RECRUITS}) — reassign someone first.")
            else:
                # Read the crowd, then choose a person and the relic-journey to send
                # them on; their stat-fit sets how fast the errand pays off.
                self.commission = {"phase": "npc"}
        elif k == pygame.K_t:
            # Give new orders to a recruit standing on this tile — or the one you're
            # already leading (you travel together, so no need to walk up to them).
            led = d.led_seeker(st)
            near = [q for q in self.recruits_here()
                    if max(abs(self.recruit_micro(q)[0] - self.px),
                           abs(self.recruit_micro(q)[1] - self.py)) <= 1.6]
            target = led if (led is not None and (led["r"], led["c"]) == (wr, wc)) else (near[0] if near else None)
            if target is None:
                self.msg("Walk up to one of your recruits to give orders.")
            else:
                self.talk = {"recruit": target, "phase": "orders"}
        elif k == pygame.K_z:
            self.slumber = True          # open the slumber menu (rest, or sleep an age away)

    def _mend_companion(self):
        led = d.led_seeker(self.state)
        if led is not None:
            led["downed"] = False
            led["hp"] = led.get("maxhp", max(1, led["stats"][1]))

    def rest(self):
        # "Rest until hale" — the old slumber: recover energy/health (+ companion), or, if
        # the age is already won, sleep through the peace until the next age dawns.
        st = self.state
        led = d.led_seeker(st)
        led_hurt = led is not None and (led.get("downed") or led.get("hp", 0) < led.get("maxhp", 0))
        if d.dark_champion(st) is None:
            days = d.skip_to_next_age(st)
            st["energy"], st["health"] = d.ENERGY_MAX, d.wiz_max_hearts(st)
            self._mend_companion(); self.victory = 0.0
            self.enter_tile((self.px, self.py))
            champ = d.dark_champion(st)
            if champ is not None:
                self.msg(f"You sleep through {days / 360:.0f} years of peace. A new age dawns — "
                         f"the shadow rises anew in {short(champ)}.")
            else:
                self.msg(f"You sleep {days:.0f} days; the world remains at peace.")
        elif st["energy"] >= d.ENERGY_MAX and st["health"] >= d.wiz_max_hearts(st) and not led_hurt:
            self.msg("Already hale and rested.")
        else:
            days = max((d.ENERGY_MAX - st["energy"]) * d.SLUMBER_DAYS_PER_ENERGY,
                       (d.wiz_max_hearts(st) - st["health"]) * d.SLUMBER_DAYS_PER_HEART,
                       3 if led_hurt else 0)
            st["energy"], st["health"] = d.ENERGY_MAX, d.wiz_max_hearts(st)
            self._mend_companion()
            d.advance_days(st, days)
            self.enter_tile((self.px, self.py))
            tail = " Your companion recovers." if led_hurt else ""
            self.msg(f"You camp; {days:.0f} days pass. Life and energy restored.{tail}")

    def long_slumber(self, years):
        # Sleep a chosen span of years — the world (and its bloodlines) runs on. You wake
        # hale, to changed houses, an older shadow, perhaps a fallen world. The deeds that
        # passed while you slept are kept in the chronicle (read them in the journal [j]).
        st = self.state
        before = len(st.get("chronicle", []))
        d.slumber_years(st, years)
        st["energy"], st["health"] = d.ENERGY_MAX, d.wiz_max_hearts(st)
        self._mend_companion()
        self.victory = 0.0
        self.enter_tile((self.px, self.py))
        here = st["owner"][st["wizard"]["r"]][st["wizard"]["c"]]
        r = d.ruler_of(st, here)
        dom = sum(st["corruption"][i] * st["territory"][i] for i in range(d.N)) / 100.0
        deeds = len(st.get("chronicle", [])) - before
        who = f" House {r['house']} reigns here now ({r['given']})." if r else ""
        fate = "  THE WORLD HAS FALLEN TO THE SHADOW." if dom >= d.DOMINION_WIN else ""
        note = f"  {deeds} deeds passed — read them in the journal [j]." if deeds else ""
        self.msg(f"You sleep {years} years and wake to a changed world.{who}{fate}{note}")

    def handle_slumber(self, e):
        # The slumber menu: rest, or sleep a chosen span of years.
        if e.key == pygame.K_1:
            self.slumber = None; self.rest()
        elif e.key in (pygame.K_2, pygame.K_3, pygame.K_4):
            self.slumber = None
            self.long_slumber({pygame.K_2: 50, pygame.K_3: 100, pygame.K_4: 1000}[e.key])
        else:
            self.slumber = None

    def render_slumber(self):
        sc = self.screen
        lines = ["SLUMBER — how long? (any other key: stay awake)",
                 "  [1] Rest until hale",
                 "  [2] Sleep 50 years",
                 "  [3] Sleep 100 years",
                 "  [4] Sleep 1000 years",
                 "(the world and its houses run on while you sleep)"]
        bw, bh = 520, 18 + len(lines) * 24 + 12
        bx, by = (W - bw) // 2, 80
        pygame.draw.rect(sc, (16, 16, 26), (bx, by, bw, bh))
        pygame.draw.rect(sc, (150, 170, 235), (bx, by, bw, bh), 2)
        for k, line in enumerate(lines):
            col = (170, 190, 240) if k == 0 else (150, 150, 170) if line.startswith("(") else (220, 220, 230)
            sc.blit(self.f.render(line, True, col), (bx + 14, by + 10 + k * 24))

    def handle_commission(self, e):
        # Read the crowd -> pick a person -> pick a relic-journey. The town's own
        # faction receives the find; the seeker's stat-fit sets the ETA.
        st = self.state
        wr, wc = st["wizard"]["r"], st["wizard"]["c"]
        town = st["towns"].get((wr, wc))
        if pygame.K_1 <= e.key <= pygame.K_9 and town is not None and self.folk:
            sel = e.key - pygame.K_1
            if self.commission["phase"] == "npc" and sel < len(self.folk):
                self.commission = {"phase": "relic", "npc": sel}
                return
            known = sorted(st["relics_known"])
            if self.commission["phase"] == "relic" and sel < len(known):
                T, R = known[sel], town["faction"]
                person = self.folk[self.commission["npc"]]
                held = next((it["owner"] for it in st["items"]
                             if it["type"] == T and it["owner"] != R), None)
                if held is not None:
                    self.msg(f"The {d.ITEM_TYPES[T]['name']} is already in {d.POWERS[held][0]}'s hands.")
                    return                                  # stay in the modal to pick another
                if st["energy"] < d.SEARCH_ENERGY:
                    self.msg(f"Too weary (need {d.SEARCH_ENERGY}).")
                else:
                    st["energy"] -= d.SEARCH_ENERGY
                    born = d._now(st) - person["age"] * d.DAYS_PER_YEAR
                    rec = d.add_recruit(st, person["stats"], R, T, wr, wc,
                                        given=person["given"], house=person["house"], born=born)
                    eta = round(d.seek_eta(rec["fit"], d.relic_base_cycles(T)) * d.DAYS_PER_CYCLE / 365.0, 1)
                    self.msg(f"{d.recruit_name(rec)} sets out after the {d.ITEM_TYPES[T]['name']} "
                             f"for {d.POWERS[R][0]} (~{eta} yr at this fit).")
                self.commission = None
                return
        if self.commission["phase"] == "relic" and e.key == pygame.K_0:
            # recruit without sending them on a journey yet — they join your following
            # idle, and wait on this tile for orders ([t] later assigns a task).
            if st["energy"] < d.SEARCH_ENERGY:
                self.msg(f"Too weary (need {d.SEARCH_ENERGY}).")
            else:
                st["energy"] -= d.SEARCH_ENERGY
                person = self.folk[self.commission["npc"]]
                born = d._now(st) - person["age"] * d.DAYS_PER_YEAR
                rec = d.add_recruit(st, person["stats"], town["faction"], None, wr, wc,
                                    given=person["given"], house=person["house"], born=born)
                self.msg(f"{d.recruit_name(rec)} joins you, awaiting orders ([t] to assign a task).")
            self.commission = None
            return
        self.commission = None  # any other key cancels

    def handle_talk(self, e):
        # Reassign a recruit: send them to fight (forfeiting any seek) or off on a new
        # relic-journey. Two steps so a relic-journey can pick which relic.
        st = self.state
        q = self.talk["recruit"]
        if q not in st["recruits"]:
            self.talk = None
            return
        if self.talk["phase"] == "orders":
            if e.key == pygame.K_1:                       # march to the front
                lost = q["task"] == "seek"
                q["led"] = False
                d._recruit_to_march(q)
                self.msg(f"{d.recruit_name(q)} marches to the front" + (" (seek abandoned)." if lost else "."))
                self.talk = None
            elif e.key == pygame.K_2:                     # seek a relic — pick which
                self.talk = {"recruit": q, "phase": "relic"}
            elif e.key == pygame.K_4 and self._can_apprehend():   # hunt the lord or his retinue
                self.talk = {"recruit": q, "phase": "apprehend"}
            elif e.key == pygame.K_3 and q["task"] == "seek":   # lead / part ways (seekers)
                if q.get("led"):
                    q["led"] = False
                    self.msg(f"You part ways with {d.recruit_name(q)} — they take up their quest again.")
                else:
                    if d.led_seeker(st) is not None:
                        self.msg("You can only lead one seeker at a time.")
                    else:
                        q["led"] = True
                        q["downed"] = False
                        q["hp"] = q["maxhp"] = max(1, q["stats"][1])   # Endurance = hearts
                        self.msg(f"You take up the road with {d.recruit_name(q)} — guard them well.")
                self.talk = None
                self.enter_tile((self.px, self.py))       # bring them into the scene
            elif e.key == pygame.K_5 and q["task"] != "idle":   # stand by — go idle, await orders
                q["led"] = False
                q["task"], q["relic"], q["prog"], q["target"] = "idle", None, 0.0, None
                self.msg(f"{d.recruit_name(q)} stands by, awaiting orders.")
                self.talk = None
            else:
                self.talk = None
        elif self.talk["phase"] == "relic":
            if pygame.K_1 <= e.key <= pygame.K_9 and (e.key - pygame.K_1) < len(d.ITEM_TYPES):
                T = e.key - pygame.K_1
                q["task"], q["relic"], q["prog"], q["target"] = "seek", T, 0.0, None
                q["fit"] = d.journey_fit(q["stats"], st["relic_demand"][T])
                self.msg(f"{d.recruit_name(q)} sets out to seek the {d.ITEM_TYPES[T]['name']}.")
            self.talk = None
        elif self.talk["phase"] == "apprehend":
            choices = [None] + st.get("lord_agents", [])   # [0]=the lord himself, then his retinue
            if pygame.K_1 <= e.key <= pygame.K_9 and (e.key - pygame.K_1) < len(choices):
                pick = choices[e.key - pygame.K_1]
                q["led"] = False
                if pick is None:
                    d.start_apprehend_quest(st, q)
                    self.msg(f"{d.recruit_name(q)} sets out to hunt down the dark lord.")
                else:
                    d.start_apprehend_quest(st, q, agent_id=pick["id"])
                    self.msg(f"{d.recruit_name(q)} sets out to hunt down one of his retinue.")
            self.talk = None

    def render_talk_window(self):
        sc, S = self.screen, self.state
        if self.talk is None:
            return
        q = self.talk["recruit"]

        def statline(stx):
            return "  ".join(f"{d.STAT_ABBR[s]}{stx[s]:>2}" for s in range(len(d.STATS)))

        head = f"{d.recruit_name(q)}  {d.RACES[q['race']][0]}  {statline(q['stats'])}"
        if self.talk["phase"] == "orders":
            rows = ["give new orders:", "[1] march to the front (drops any seek)",
                    "[2] seek a relic"]
            if q["task"] == "seek":
                rows.append("[3] part ways (resume their quest)" if q.get("led")
                            else "[3] lead them in person (escort their quest)")
            if self._can_apprehend():
                rows.append("[4] hunt down the dark lord (apprehend)")
            if q["task"] != "idle":
                rows.append("[5] stand by (await orders, no task)")
        elif self.talk["phase"] == "relic":
            rows = ["seek which relic?"]
            rows += [f"[{i + 1}] {it['name']:<14} needs {statline(S['relic_demand'][i])}"
                     for i, it in enumerate(d.ITEM_TYPES)]
        else:
            agents = S.get("lord_agents", [])
            rows = ["hunt down whom?", "[1] the dark lord himself"]
            rows += [f"[{i + 2}] retinue agent #{ag['id']}  {statline(ag['stats'])}"
                     for i, ag in enumerate(agents)]
        bw, bh = 560, 56 + len(rows) * 24 + 12
        bx, by = (W - bw) // 2, 70
        pygame.draw.rect(sc, (18, 16, 28), (bx, by, bw, bh))
        pygame.draw.rect(sc, (140, 200, 235), (bx, by, bw, bh), 2)
        sc.blit(self.fb.render(head, True, (160, 215, 245)), (bx + 14, by + 10))
        for k, line in enumerate(rows):
            sc.blit(self.f.render(line, True, (215, 215, 225)), (bx + 16, by + 44 + k * 24))

    def cycle_staff(self):
        # Equip the next staff the wizard has FOUND and whose Might he can wield (magic
        # == Might for now). Staves beyond the Plain Staff are won from artifact quests.
        st = self.state
        found = st.get("staves_found", {0})
        n = len(STAVES)
        for step in range(1, n + 1):
            idx = (st["wiz_staff"] + step) % n
            staff = STAVES[idx]
            if idx in found and st["wiz_stats"][0] >= staff["equip_might"]:
                st["wiz_staff"] = idx
                self.msg(f"Equipped the {staff['name']}.")
                return
        self.msg("No other staff to wield — seek one in the wilds.")

    def fire_bolt(self, charged=False, power=1):
        # Bolt = [x, y, dx, dy, power, charged, hit-set]. A charged bolt pierces
        # (keeps flying through minions it has already struck). The equipped staff
        # decides the shot pattern — e.g. the Fanstaff looses a 3-way spread, each at
        # a third power. Power per projectile floors at 1 (a bolt always stings).
        staff = STAVES[self.state.get("wiz_staff", 0)]
        base = math.atan2(self.facing[1], self.facing[0])
        p = max(1, round(power * staff["power_mult"]))
        for off in staff["shots"]:
            a = base + off
            self.bolts.append([self.px, self.py, math.cos(a), math.sin(a), p, charged, set()])

    def on_keyup(self, e):
        # Release of the charge: a real hold (>= min) looses a heavy piercing bolt;
        # a quick tap looses a basic one. (pygame gives us the key-up curses can't.)
        if e.key == pygame.K_SPACE and self.charging is not None:
            st = self.state
            if self.view == "tile" and st["health"] > 0:
                frac = min(1.0, self.charging / d.WIZ_CHARGE_FULL)
                if self.charging >= d.WIZ_CHARGE_MIN:
                    self.fire_bolt(charged=True, power=1 + round(frac * st["wiz_stats"][0]))
                else:
                    self.fire_bolt()              # it was a tap — basic bolt
            self.charging = None

    def handle_char(self, e):
        # Character window: set the wizard's stats by hand (no limits — a testing
        # bench). Endurance is his heart cap; the others gate combat abilities.
        k = e.key
        st = self.state
        if k in (pygame.K_UP, pygame.K_w):
            self.char_sel = (self.char_sel - 1) % len(d.STATS)
        elif k in (pygame.K_DOWN, pygame.K_s):
            self.char_sel = (self.char_sel + 1) % len(d.STATS)
        elif k in (pygame.K_RIGHT, pygame.K_d, pygame.K_EQUALS, pygame.K_PLUS):
            st["wiz_stats"][self.char_sel] += 1
        elif k in (pygame.K_LEFT, pygame.K_a, pygame.K_MINUS):
            st["wiz_stats"][self.char_sel] = max(0, st["wiz_stats"][self.char_sel] - 1)
            st["health"] = min(st["health"], d.wiz_max_hearts(st))   # clamp if Endurance fell
            if st["wiz_stats"][0] < STAVES[st["wiz_staff"]]["equip_might"]:
                st["wiz_staff"] = 0     # too weak now to wield it — falls back to the plain staff
        else:
            self.char_open = False   # c / esc / any other key closes

    def msg(self, t):
        self.state["town_msg"] = t

    def tile_scenery(self, r, c):
        # Deterministic ambient decor for a peaceful wilderness tile: the nearer a town,
        # the more its life bleeds out — folk, then a lone house/farm, then a hamlet's
        # worth of houses + farms right on the outskirts. Fishers wherever there's water.
        # Folk are [x, y, vx, vy, kind] in tile-local cells; they wander while you watch.
        S = self.state
        rng = random.Random((S["world_seed"] * 2654435761 ^ (r * d.GRID + c) ^ 0xF00D) & 0xFFFFFFFF)
        towns = S["towns"]
        if towns:
            (tr, tc), tt = min(towns.items(), key=lambda kv: max(abs(kv[0][0] - r), abs(kv[0][1] - c)))
            dist, fac = max(abs(tr - r), abs(tc - c)), tt["faction"]
        else:
            dist, fac = 99, S["owner"][r][c]
        water = S["biome_map"][r][c] == 1
        houses, farms, folk, occ = [], [], [], set()

        def spot():
            for _ in range(8):
                p = (rng.randint(3, TILE - 4), rng.randint(3, TILE - 5))
                if p not in occ:
                    occ.add(p)
                    return p
            return (rng.randint(3, TILE - 4), rng.randint(3, TILE - 5))

        def folkspot(kind):
            sr, sc_ = spot()
            ang = rng.uniform(0, 2 * math.pi)
            spd = 0.0 if kind == "fish" else rng.uniform(0.6, 1.3)
            return [float(sc_), float(sr), math.cos(ang) * spd, math.sin(ang) * spd, kind]

        if dist <= 1:                              # immediate outskirts — a little hamlet
            for _ in range(rng.randint(1, 2)): houses.append(spot())
            for _ in range(rng.randint(2, 3)): farms.append(spot())
            for _ in range(rng.randint(1, 2)): folk.append(folkspot("wander"))
        elif dist <= OUTSKIRTS_RADIUS:             # nearing a town — a lone house/farm + a soul
            if rng.random() < 0.7:
                (houses if rng.random() < 0.45 else farms).append(spot())
                if rng.random() < 0.5: farms.append(spot())
            if rng.random() < 0.5: folk.append(folkspot("wander"))
        else:                                      # deep country — the odd wanderer
            if rng.random() < 0.35: folk.append(folkspot("wander"))
        if water and rng.random() < 0.6:           # fishers by the water, town or no town
            folk.append(folkspot("fish"))
        return {"houses": houses, "farms": farms, "folk": folk, "fac": fac}

    def draw_farm(self, c, r):
        sc = self.screen
        x, y, w, h = c * CELL, r * CELL, CELL * 2, CELL * 2
        pygame.draw.rect(sc, (126, 92, 58), (x, y, w, h))
        pygame.draw.rect(sc, (90, 64, 40), (x, y, w, h), 1)
        for i in range(1, 6):                      # furrows
            yy = y + i * h // 6
            pygame.draw.line(sc, (150, 122, 74), (x + 2, yy), (x + w - 2, yy), 1)

    def chart_world(self):
        # Snapshot the world as it is now (a paper map charted in a town); it freezes
        # and goes stale until the wizard returns to a town to chart it afresh. This
        # is the POWERS map's source of truth and is always the full world — per the
        # design, faction/territory knowledge updates fully on every chart.
        st = self.state
        st["map_snapshot"] = {
            "owner": [row[:] for row in st["owner"]],
            "corr": list(st["corruption"]),
            "terr": list(st["territory"]),
            "army": list(st["army"]),
            "shadow": st["shadow"],
            "day": st["day"],
        }
        # The GEOGRAPHY map, by contrast, is pieced together one chart at a time: each
        # chart only uncovers a circle of the world around the town it was bought in
        # (twice the radius at a capital), and circles from past visits accumulate —
        # so the full geography map is built up gradually by visiting many towns.
        wr, wc = st["wizard"]["r"], st["wizard"]["c"]
        radius = d.MAP_REVEAL_RADIUS
        t = st["towns"].get((wr, wc))
        if t is not None and t.get("capital"):
            radius = d.MAP_REVEAL_RADIUS_CAPITAL
        revealed = st.setdefault("map_revealed", bytearray(d.GRID * d.GRID))
        r2 = radius * radius
        r0, r1 = max(0, wr - radius), min(d.GRID - 1, wr + radius)
        c0, c1 = max(0, wc - radius), min(d.GRID - 1, wc + radius)
        for rr in range(r0, r1 + 1):
            dr2 = (rr - wr) ** 2
            row_off = rr * d.GRID
            for cc in range(c0, c1 + 1):
                if dr2 + (cc - wc) ** 2 <= r2:
                    revealed[row_off + cc] = 1

    def buy_map(self):
        # A map is no longer free: buy a fresh chart from a town's market for MAP_PRICE.
        st = self.state
        wr, wc = st["wizard"]["r"], st["wizard"]["c"]
        if (wr, wc) not in st["towns"] or self.mode == "contested":
            self.msg("You can only buy a map at a town's market.")
        elif st["gold"] < d.MAP_PRICE:
            self.msg(f"Not enough gold (a fresh map is {d.MAP_PRICE}g).")
        else:
            st["gold"] -= d.MAP_PRICE
            self.chart_world()
            cap = st["towns"].get((wr, wc), {}).get("capital")
            area = "a wide circle" if cap else "a circle"
            self.msg(f"You buy a fresh chart ({d.MAP_PRICE}g) — {area} of the wilds around "
                     f"is now charted, and the powers' standings updated. "
                     f"Gold {st['gold']}g. View it with TAB.")

    def nearby_person(self):
        # The closest person the wizard can talk to — a townsperson (elders may carry a
        # legend) or an ambient wanderer/fisher out in the country. None if no one's near.
        st = self.state
        wr, wc = st["wizard"]["r"], st["wizard"]["c"]
        if self.creature is not None:           # the mythical quest-giver, at the tile centre
            if max(abs(TILE / 2 - self.py), abs(TILE / 2 - self.px)) <= 2.2:
                return {"creature": self.creature, "answer": []}
        if self.mode == "boss" and self.units and not is_hostile(st, self.units[0]):
            lm = self.units[0]
            if max(abs(lm["y"] - self.py), abs(lm["x"] - self.px)) <= 1.6:
                return {"lord_npc": True, "answer": []}
        if self.sower_npc is not None and st["wiz_stats"][2] >= d.WIZ_WITS_SOWER_AT:
            # only a sharp enough Wits sees through the disguise — otherwise he's just
            # another face in the scenery folk loop below, with nothing to expose.
            fk = self.sower_npc
            if max(abs(fk[1] - self.py), abs(fk[0] - self.px)) <= 1.4:
                return {"sower_npc": True, "answer": []}
        if self.npcs and self.mode != "contested":
            elders = d.town_elders(st, wr, wc)
            for i, (nr, nc) in enumerate(self.npcs):
                if max(abs(nr - self.py), abs(nc - self.px)) <= 1.4:
                    return {"elder_relic": elders.get(i), "answer": []}
        if self.scenery:
            for fk in self.scenery["folk"]:
                if max(abs(fk[1] - self.py), abs(fk[0] - self.px)) <= 1.4:
                    return {"elder_relic": None, "answer": []}
        return None

    def agent_bearings(self):
        # Word of your agents — a bearing (N/S/E/W from where you stand) + a rough range.
        st = self.state
        wr, wc = st["wizard"]["r"], st["wizard"]["c"]
        if not st["recruits"]:
            return ["\"Your agents? None of yours have passed this way.\""]
        out = []
        for q in st["recruits"]:
            dr, dc = q["r"] - wr, q["c"] - wc
            if dr == 0 and dc == 0:
                where = "right here"
            else:
                if abs(dr) >= abs(dc):
                    d_ = "north" if dr < 0 else "south"
                else:
                    d_ = "west" if dc < 0 else "east"
                dist = max(abs(dr), abs(dc))
                band = "close" if dist <= 8 else "far" if dist > 20 else "a way off"
                where = f"to the {d_}, {band}"
            out.append(f"{d.recruit_name(q)} {d.RACES[q['race']][0]} ({q['task']}) — {where}")
        return out

    def nearest_town_dir(self):
        # Ask a local where the nearest town lies — a cardinal bearing + rough range.
        st = self.state
        wr, wc = st["wizard"]["r"], st["wizard"]["c"]
        towns = st["towns"]
        if not towns:
            return ["\"A town? None for many a league, I fear.\""]
        if (wr, wc) in towns:
            return [f"\"Why, you stand in {towns[(wr, wc)]['name']}!\""]
        (tr, tc), t = min(towns.items(),
                          key=lambda kv: (kv[0][0] - wr) ** 2 + (kv[0][1] - wc) ** 2)
        dr, dc = tr - wr, tc - wc
        ns = "north" if dr < 0 else "south" if dr > 0 else ""
        ew = "west" if dc < 0 else "east" if dc > 0 else ""
        d_ = (ns + ew) if (ns and ew) else (ns or ew)     # e.g. "northeast", "north"
        dist = max(abs(dr), abs(dc))
        band = "not far" if dist <= 6 else "a long way" if dist > 18 else "a fair walk"
        kind = "city" if t["capital"] else "village"
        return [f"\"The nearest {kind} is {t['name']},\"",
                f"\"away to the {d_} — {band} from here.\""]

    def world_news(self):
        st = self.state
        foc = max(range(d.N), key=lambda i: st["corruption"][i])
        lines = [f"\"They say the shadow's hand lies heaviest on the {d.POWERS[foc][0]}.\""]
        wars = [(b["winner"], b["loser"]) for b in st["battles"]]
        if wars:
            w, l = wars[0]
            lines.append(f"\"War rages — {d.POWERS[w][0]} press the {d.POWERS[l][0]}.\"")
        else:
            lines.append("\"The lands are quiet... for now.\"")
        # whose house rules the realm the wizard stands in
        here = st["owner"][st["wizard"]["r"]][st["wizard"]["c"]]
        r = d.ruler_of(st, here)
        if r is not None:
            lines.append(f"\"These lands answer to House {r['house']} — {r['given']} reigns, "
                         f"{d.person_age(st, r)} winters old.\"")
        motive = d.want_motive(st, here)        # what its corruption makes it covet
        if motive is not None:
            j, why = motive
            lines.append(f"\"The {d.POWERS[here][0]} {why} the {d.POWERS[j][0]}.\"")
        return lines

    def handle_chat(self, e):
        # The chat window: ask whoever you're speaking with about legends, your agents,
        # or news. Answers fill in below; any other key ends the conversation.
        st = self.state
        ch = self.chat
        art = ch.get("creature")
        if art is not None:                           # a mythical creature — the artifact quest
            if e.key == pygame.K_1 and not art["told"]:
                art["told"] = True
                art["known"] = True
                ch["answer"] = [f"\"Long have I kept the tale of {art['name']} — {art['hint']}.\"",
                                "\"It lies guarded in the wilds. Go yourself, wizard — no errand-",
                                " runner can claim it. Its resting place is marked on your map.\""]
            elif e.key == pygame.K_1:
                ch["answer"] = [f"\"You know where {art['name']} lies. Go and take it.\""]
            else:
                self.chat = None
            return
        if ch.get("lord_npc"):                        # the lord himself, met in peace
            if e.key == pygame.K_1:
                if st.get("corruption_growth") == "sown":
                    sow = st.get("sower")
                    t = sow.get("target") if sow else None
                    if t is not None:
                        st["wiz_sow_target"] = t
                        ch["answer"] = [f"\"Go then, and sow division among the {d.POWERS[t][0]}...\"",
                                        "\"...as my own servant does. We shall see what you become.\""]
                    else:
                        ch["answer"] = ["\"There is no errand to give, just now.\""]
                else:
                    ch["answer"] = ["\"...\" (he says nothing more)"]
            else:
                self.chat = None
            return
        if e.key == pygame.K_1:                       # old legends (only elders truly know)
            tale = ch.get("elder_relic")
            if tale is not None and tale[0] == "relic":
                T = tale[1]
                if T not in st["relics_known"]:
                    st["relics_known"].add(T)
                    ch["answer"] = [f"\"...the {d.ITEM_TYPES[T]['name']}, a relic of legend. Seek it, wizard.\"",
                                    "(you may now send a seeker after it)"]
                else:
                    ch["answer"] = ["\"That tale you have already heard.\""]
            elif tale is not None and tale[0] == "artifact":
                art2 = st["artifacts"][tale[1]]
                if not art2["known"]:
                    art2["known"] = True
                    ch["answer"] = [f"\"There is said to dwell {art2['creature_name']}, who knows of "
                                    f"{art2['name']}.\"",
                                    "\"Seek it out yourself, wizard — it will tell you more.\""]
                else:
                    ch["answer"] = ["\"That tale you have already heard.\""]
            else:
                ch["answer"] = ["\"Legends? I'm no loremaster — ask an elder in a town.\""]
        elif e.key == pygame.K_2:                     # whereabouts of your agents
            ch["answer"] = self.agent_bearings()
        elif e.key == pygame.K_3:                     # news of the world
            ch["answer"] = self.world_news()
        elif e.key == pygame.K_4:                     # where's the nearest town?
            ch["answer"] = self.nearest_town_dir()
        else:
            self.chat = None                          # any other key leaves

    def recruits_here(self):
        wr, wc = self.state["wizard"]["r"], self.state["wizard"]["c"]
        return [q for q in self.state["recruits"] if (q["r"], q["c"]) == (wr, wc)]

    def recruit_micro(self, q):
        # A stable micro spot for a recruit standing on the wizard's current tile.
        return (4 + (q["id"] * 7) % (TILE - 8), 4 + (q["id"] * 5) % (TILE - 8))

    # ----- per-frame update -----
    def update(self, dt):
        st = self.state
        if self.invuln > 0:
            self.invuln -= dt
        if self.loot[1] > 0:
            self.loot = (self.loot[0], self.loot[1] - dt)
        if self.victory > 0:
            self.victory -= dt

        if self.view in ("map", "controls"):
            if self.playing:
                self.auto_acc += dt
                interval = (1.1 - self.speed * 0.1)
                if self.auto_acc >= interval:
                    self.auto_acc = 0.0
                    self.step_cycle()
            return

        # ---- tile view (realtime) ----
        self.tile_seconds += dt
        if self.scenery:                             # ambient folk wander the clearing
            for fk in self.scenery["folk"]:
                if fk[4] == "wander":
                    fk[0] += fk[2] * dt; fk[1] += fk[3] * dt
                    if not 1 <= fk[0] <= TILE - 1:
                        fk[2] = -fk[2]; fk[0] = max(1.0, min(TILE - 1.0, fk[0]))
                    if not 1 <= fk[1] <= TILE - 1:
                        fk[3] = -fk[3]; fk[1] = max(1.0, min(TILE - 1.0, fk[1]))
        alive = st["health"] > 0
        if self.charging is not None:                # wind up the held charged bolt
            if alive:
                self.charging = min(d.WIZ_CHARGE_FULL, self.charging + dt)
                if self.charging >= d.WIZ_CHARGE_FULL:   # auto-loose at full
                    self.fire_bolt(charged=True, power=1 + st["wiz_stats"][0])
                    self.charging = None
            else:
                self.charging = None
        if alive:
            keys = pygame.key.get_pressed()
            dx = (keys[pygame.K_RIGHT] or keys[pygame.K_d]) - (keys[pygame.K_LEFT] or keys[pygame.K_a])
            dy = (keys[pygame.K_DOWN] or keys[pygame.K_s]) - (keys[pygame.K_UP] or keys[pygame.K_w])
            if dx and dy:
                if self.last_axis == "h":
                    dy = 0
                else:
                    dx = 0
            if dx or dy:
                self.facing = (dx, dy)
                if dx and not self.blocked(self.px + dx * SPEED * dt, self.py):
                    self.px += dx * SPEED * dt
                if dy and not self.blocked(self.px, self.py + dy * SPEED * dt):
                    self.py += dy * SPEED * dt
                if self.px < 0:
                    self.cross(0, -1)
                elif self.px >= TILE:
                    self.cross(0, 1)
                elif self.py < 0:
                    self.cross(-1, 0)
                elif self.py >= TILE:
                    self.cross(1, 0)

        # enemy AI: wander + spit; only the hostile fire at the wizard
        for m in self.units:
            if m.get("ally"):
                self.update_ally(m, dt, alive)
                continue
            if m.get("boss") or m.get("guardian") or m.get("lord_agent") or m.get("sower"):
                self.update_boss(m, dt, alive)
                continue
            m["leg"] -= dt
            nx, ny = m["x"] + m["dir"][0] * MON_SPEED * dt, m["y"] + m["dir"][1] * MON_SPEED * dt
            if m["leg"] <= 0 or not (1 <= nx <= TILE - 2 and 1 <= ny <= TILE - 2):  # turn at arena bounds
                m["leg"] = random.uniform(*MON_LEG)
                if random.random() < MON_PAUSE:
                    m["dir"] = (0, 0)
                else:
                    m["dir"] = random.choice(CARDINALS); m["face"] = m["dir"]
            elif m["dir"] != (0, 0):
                m["x"], m["y"] = nx, ny; m["face"] = m["dir"]
            hot = is_hostile(st, m)
            m["cool"] -= dt
            if alive and hot and m["cool"] <= 0:
                m["cool"] = random.uniform(*MON_FIRE_CD)
                self.eshots.append([m["x"], m["y"], m["face"][0], m["face"][1]])
            if alive and hot and self.invuln <= 0 and math.hypot(self.px - m["x"], self.py - m["y"]) <= TOUCH_RANGE:
                self.hurt()

        for b in self.bolts:
            spd = BOLT_SPEED * (CHARGE_SPEED_MULT if b[5] else 1.0)
            b[0] += b[2] * spd * dt; b[1] += b[3] * spd * dt
        for sh in self.eshots:
            sh[0] += sh[2] * MON_PROJ_SPEED * dt; sh[1] += sh[3] * MON_PROJ_SPEED * dt

        live = []
        for b in self.bolts:
            power, charged, seen = b[4], b[5], b[6]
            spent = False
            for m in self.units:
                if id(m) in seen or m.get("ally"):
                    continue                          # a pierced bolt hits each foe once; never an ally
                if math.hypot(b[0] - m["x"], b[1] - m["y"]) <= BOLT_HIT:
                    if m.get("boss"):
                        st["favor"][m["fac"]] = 0  # struck the lord — peace is instantly broken
                    m["hp"] -= power
                    seen.add(id(m))
                    if not charged:
                        spent = True; break           # a basic bolt is spent on one foe
                    # a charged bolt pierces — keep flying
            if not (spent or self.solid_at(b[0], b[1]) or not (0 <= b[0] < TILE and 0 <= b[1] < TILE)):
                live.append(b)
        self.bolts = live
        for m in [m for m in self.units if m["hp"] <= 0]:
            if m.get("ally"):
                self.ally_down(m)
            elif m.get("guardian"):
                if m.get("art"):
                    self.claim_artifact(m["art"])      # an artifact's guardian
                else:
                    self.loot = ("the warden falls!", 1.4)   # a road warden (no prize)
            elif m.get("lord_agent"):
                d.slay_lord_agent(st, m["agent_id"])
                g = random.randint(2, 8); st["gold"] += g
                self.loot = (f"the dark agent falls! +{g}g", 1.4)
                self.msg("One of the lord's retinue is slain — he must recruit another.")
            elif m.get("sower"):
                d.slay_sower(st)
                g = random.randint(2, 8); st["gold"] += g
                self.loot = (f"the sower falls back! +{g}g", 1.4)
                self.msg("The sower is struck down — he retreats to the shadow lands to recover.")
            elif m.get("boss"):
                self.win_age(m["fac"])
            elif m["fac"] < 0:
                g = random.randint(1, 5); st["gold"] += g; self.loot = (f"+{g}g", 1.2)
            else:
                other = self.hi_fac if m["fac"] == self.lo_fac else self.lo_fac
                st["favor"][m["fac"]] = max(0, st["favor"][m["fac"]] - KILL_FAVOR_DOWN)
                st["favor"][other] = min(10, st["favor"][other] + KILL_FAVOR_UP)
                self.loot = (f"{short(m['fac'])} angered", 1.2)
        self.units = [m for m in self.units if m["hp"] > 0]
        # Beset agents are saved once every beast is down.
        if self.beset_ids and not any(m["fac"] < 0 for m in self.units):
            for q in self.state["recruits"]:
                if q["id"] in self.beset_ids and q["task"] == "seek":
                    q["prog"] = min(100.0, q["prog"] + 12)   # a grateful surge onward
            self.loot = ("agents saved!", 1.4)
            self.beset_ids = set()

        live = []
        allies = [m for m in self.units if m.get("ally")]
        for sh in self.eshots:
            if alive and self.invuln <= 0 and math.hypot(sh[0] - self.px, sh[1] - self.py) <= 0.5:
                self.hurt(); continue
            hit_ally = next((a for a in allies if math.hypot(sh[0] - a["x"], sh[1] - a["y"]) <= 0.55), None)
            if hit_ally is not None:                 # the escorted seeker takes a blow
                hit_ally["hp"] -= 1
                hit_ally["q"]["hp"] = hit_ally["hp"]   # persist across tiles
                continue
            if self.solid_at(sh[0], sh[1]) or not (0 <= sh[0] < TILE and 0 <= sh[1] < TILE):
                continue
            live.append(sh)
        self.eshots = live

        if self.mode == "contested" and alive:
            self.ctimer += dt
            if self.ctimer >= CYCLE_SECONDS:
                self.ctimer -= CYCLE_SECONDS
                self.contested_tick()

    # ----- rendering -----
    def to_px(self, x, y):
        return int(x * CELL + CELL / 2), int(y * CELL + CELL / 2)

    def render(self):
        if self.view == "tile":
            self.render_tile()
        elif self.view == "map":
            self.render_map()
        else:
            self.render_controls()
        if self.journal:                       # quest journal overlays any view
            self.render_journal_window()
        pygame.display.flip()

    def render_tile(self):
        st = self.state
        sc = self.screen
        ground = xterm_rgb(d.tile_terrain(st, st["wizard"]["r"], st["wizard"]["c"])[2])
        ground_kind = d.BIOMES[st["biome_map"][st["wizard"]["r"]][st["wizard"]["c"]]]["ground"]
        ground_tex = {"grass": self.tex.get("grass_flat"), "sand": self.tex.get("dirt_flat")}.get(ground_kind)
        water_rgb = xterm_rgb(d.TERR["water"])
        if ground_tex is None:
            sc.fill(ground)
        else:
            for r in range(TILE):
                for c in range(TILE):
                    sc.blit(ground_tex, (c * CELL, r * CELL))
        for r in range(TILE):
            for c in range(TILE):
                g, _a, _fg, bg = self.cells[r][c]
                rect = (c * CELL, r * CELL, CELL, CELL)
                bgr = xterm_rgb(bg)
                if bgr != ground:
                    if bgr == water_rgb and self.tex.get("water_flat"):
                        sc.blit(self.tex["water_flat"], (rect[0], rect[1]))
                    else:
                        pygame.draw.rect(sc, bgr, rect)
                if g != " ":
                    draw_feature(sc, g, rect, self.tex)
        # the geo traversal layer painted over the cosmetic terrain: rivers/ranges
        # (with collision) and the road/bridge/pass corridors that thread them
        for r in range(TILE):
            row = self.geo_paint[r]
            for c in range(TILE):
                k = row[c]
                if k is None:
                    continue
                rect = (c * CELL, r * CELL, CELL, CELL)
                pygame.draw.rect(sc, GEO_PAINT_RGB[k], rect)
                if k == "water":
                    pygame.draw.rect(sc, (40, 78, 150), rect, 1)
                elif k == "rock":
                    cx = c * CELL + CELL // 2
                    pygame.draw.polygon(sc, (150, 148, 156), [(cx, r * CELL + 5),
                                        (c * CELL + 5, r * CELL + CELL - 5),
                                        (c * CELL + CELL - 5, r * CELL + CELL - 5)])
        for (r, c, kind) in self.buildings:
            spr = self.spr_keep if kind == "keep" else self.spr_house
            fac = st["towns"][(st["wizard"]["r"], st["wizard"]["c"])]["faction"]
            sc.blit(spr[fac], (c * CELL, r * CELL))
        elders = d.town_elders(st, st["wizard"]["r"], st["wizard"]["c"]) if self.npcs else {}
        for i, (r, c) in enumerate(self.npcs):
            fac = st["towns"][(st["wizard"]["r"], st["wizard"]["c"])]["faction"]
            sp = self.spr_npc[fac]
            ox, oy = c * CELL + (CELL - sp.get_width()) // 2, r * CELL + (CELL - sp.get_height()) // 2
            sc.blit(sp, (ox, oy))
            # An elder with a tale you've not yet heard wears a gold mark — seek them.
            tale = elders.get(i)
            unheard = tale is not None and (
                (tale[0] == "relic" and tale[1] not in st["relics_known"]) or
                (tale[0] == "artifact" and not st["artifacts"][tale[1]]["known"]))
            if unheard:
                pygame.draw.circle(sc, (245, 215, 90), (c * CELL + CELL // 2, r * CELL + 2), 3)
        if d.merchant_here(st) == "pedlar":
            sc.blit(self.spr_pedlar, (TILE // 2 * CELL, 1 * CELL))
        if self.scenery:                              # outskirts: farms, houses, ambient folk
            fac = self.scenery["fac"]
            for (fr, fc) in self.scenery["farms"]:
                self.draw_farm(fc, fr)
            for (hr, hc) in self.scenery["houses"]:
                sc.blit(self.spr_house[fac], (hc * CELL, hr * CELL))
            for fk in self.scenery["folk"]:
                sp = self.spr_npc[fac]
                px, py = self.to_px(fk[0], fk[1])
                sc.blit(sp, (px - sp.get_width() // 2, py - sp.get_height() // 2))
        for sh in self.eshots:
            pygame.draw.circle(sc, (255, 90, 70), self.to_px(*sh[:2]), 5)
        for b in self.bolts:
            if b[5]:                                  # charged: a heavier, brighter bolt
                pygame.draw.circle(sc, (255, 250, 205), self.to_px(*b[:2]), 9)
                pygame.draw.circle(sc, (255, 205, 110), self.to_px(*b[:2]), 9, 2)
            else:
                pygame.draw.circle(sc, (130, 225, 255), self.to_px(*b[:2]), 5)
        if self.creature is not None:          # the mythical quest-giver, looming at centre
            cx, cy = self.to_px(TILE / 2, TILE / 2)
            pygame.draw.circle(sc, (70, 30, 96), (cx, cy), int(CELL * 1.3))
            pygame.draw.circle(sc, (190, 110, 235), (cx, cy), int(CELL * 1.3), 3)
            for ex in (-0.4, 0.4):
                pygame.draw.circle(sc, (245, 230, 120), (cx + int(ex * CELL), cy - CELL // 3), max(2, CELL // 6))
        for m in self.units:
            mx, my = self.to_px(m["x"], m["y"])
            if m.get("boss") or m.get("guardian") or m.get("lord_agent") or m.get("sower"):
                guardian, agent_unit = m.get("guardian"), m.get("lord_agent") or m.get("sower")
                # A looming figure — far larger than a minion. The shadow-lord is dark
                # red; an artifact's guardian is a stone-grey warden; a retinue agent
                # (or the sower himself) is a smaller violet wraith (lesser than the lord).
                if guardian:
                    body, rim, eye = (40, 44, 52), (150, 150, 170), (180, 200, 230)
                elif agent_unit:
                    body, rim, eye = (50, 24, 60), (150, 90, 190), (220, 180, 240)
                else:
                    body, rim, eye = (24, 10, 34), (170, 40, 60), (210, 70, 90)
                size = CELL * (0.8 if agent_unit else 1.05)
                pygame.draw.circle(sc, body, (mx, my), int(size))
                pygame.draw.circle(sc, rim, (mx, my), int(size), 3)
                pygame.draw.circle(sc, eye, (mx, my), int(size * 0.48))
                if not (guardian or agent_unit):
                    sc.blit(self.spr_sol[m["fac"]], (mx - self.spr_sol[m["fac"]].get_width() // 2,
                                                     my - self.spr_sol[m["fac"]].get_height() // 2))
                bw = int(CELL * 2.2)
                bx, by = mx - bw // 2, my - int(CELL * 1.5)
                pygame.draw.rect(sc, (50, 20, 28), (bx, by, bw, 7))
                frac = max(0, m["hp"]) / max(1, m["maxhp"])
                pygame.draw.rect(sc, (210, 60, 70), (bx, by, int(bw * frac), 7))
                continue
            if m.get("ally"):                          # the escorted seeker, fighting with you
                spr = self.spr_sol[m["fac"]]
                sc.blit(spr, (mx - spr.get_width() // 2, my - spr.get_height() // 2))
                pygame.draw.circle(sc, (120, 230, 140), (mx, my), int(CELL * 0.55), 2)
                bw = int(CELL * 1.4); bx, by = mx - bw // 2, my - int(CELL * 0.8)
                pygame.draw.rect(sc, (40, 52, 40), (bx, by, bw, 5))
                pygame.draw.rect(sc, (120, 220, 140), (bx, by, int(bw * max(0, m["hp"]) / max(1, m["maxhp"])), 5))
                tag = self.f.render(f"#{m['q']['id']}", True, (200, 235, 200))
                sc.blit(tag, (mx - tag.get_width() // 2, my - CELL))
                continue
            spr = self.spr_mon if m["fac"] < 0 else self.spr_sol[m["fac"]]
            if is_hostile(st, m):
                pygame.draw.circle(sc, (255, 50, 50), (mx, my), int(CELL * .5), 2)
            sc.blit(spr, (mx - spr.get_width() // 2, my - spr.get_height() // 2))
        # The wizard's own recruits standing on this very tile (caught mid-journey or
        # holding here) — walk up and press [t] to give them new orders.
        for q in self.recruits_here():
            if q.get("led"):
                if q.get("downed"):                    # a fallen companion — camp [z] to revive
                    dx, dy = self.to_px(self.px + 1.0, self.py)
                    dspr = self.spr_sol[q["fac"]]
                    grey = dspr.copy(); grey.fill((90, 90, 100, 0), special_flags=pygame.BLEND_RGBA_MULT)
                    sc.blit(grey, (dx - grey.get_width() // 2, dy - grey.get_height() // 2))
                    t = self.f.render(f"{q.get('given', '#' + str(q['id']))} DOWN", True, (240, 120, 120))
                    sc.blit(t, (dx - t.get_width() // 2, dy - CELL))
                continue                                # active led ally is drawn in the units pass
            rpx, rpy = self.to_px(*self.recruit_micro(q))
            rspr = self.spr_sol[q["fac"]]
            sc.blit(rspr, (rpx - rspr.get_width() // 2, rpy - rspr.get_height() // 2))
            beset = q["id"] in self.beset_ids
            ring = (255, 60, 60) if beset else (245, 245, 250)
            pygame.draw.circle(sc, ring, (rpx, rpy), int(CELL * 0.55), 2 if not beset else 3)
            nm = q.get("given", f"#{q['id']}")
            tag = f"{nm} BESET!" if beset else f"{nm} {q['task']}"
            sc.blit(self.f.render(tag, True, (255, 90, 90) if beset else (235, 235, 245)),
                    (rpx - self.f.size(tag)[0] // 2, rpy - CELL))
        sx, sy = self.to_px(self.px, self.py)
        if st["health"] > 0 and (self.invuln <= 0 or int(self.invuln * 12) % 2 == 0):
            sc.blit(self.spr_wiz, (sx - self.spr_wiz.get_width() // 2, sy - self.spr_wiz.get_height() // 2))
        if self.charging is not None and self.charging >= d.WIZ_CHARGE_MIN * 0.5:
            frac = min(1.0, self.charging / d.WIZ_CHARGE_FULL)
            ready = self.charging >= d.WIZ_CHARGE_MIN
            col = (255, 235, 120) if ready else (165, 165, 180)
            pygame.draw.circle(sc, col, (sx, sy), int(CELL * 0.75), 3)
            bw = int(CELL * 1.5); bxx = sx - bw // 2; byy = sy - int(CELL * 1.0)
            pygame.draw.rect(sc, (40, 40, 52), (bxx, byy, bw, 5))
            pygame.draw.rect(sc, col, (bxx, byy, int(bw * frac), 5))
        if self.loot[1] > 0:
            sc.blit(self.fb.render(self.loot[0], True, (250, 215, 90)), (sx - 10, sy - CELL))
        if self.mode == "boss":
            peaceful = self.units and not is_hostile(st, self.units[0])
            if peaceful:
                banner = f"THE LIEUTENANT OF {short(self.hi_fac).upper()} BROODS HERE — [e] TO SPEAK"
                col = (210, 200, 150)
            else:
                banner = f"THE LIEUTENANT OF {short(self.hi_fac).upper()} BARS YOUR WAY"
                col = (235, 120, 140)
            tw = self.fbig.size(banner)[0]
            sc.blit(self.fbig.render(banner, True, col), ((W - tw) // 2, 8))
        elif self.mode == "guardian":
            banner = "A GUARDIAN WARDS THE ARTIFACT — FELL IT TO CLAIM"
            tw = self.fbig.size(banner)[0]
            sc.blit(self.fbig.render(banner, True, (210, 210, 235)), ((W - tw) // 2, 8))
        elif self.mode == "agent":
            banner = "ONE OF THE LORD'S RETINUE — FELL IT TO THIN HIS RANKS"
            tw = self.fbig.size(banner)[0]
            sc.blit(self.fbig.render(banner, True, (200, 150, 220)), ((W - tw) // 2, 8))
        elif self.mode == "sower":
            banner = "THE SOWER — FELL HIM TO BUY A RESPITE FROM HIS CORRUPTING"
            tw = self.fbig.size(banner)[0]
            sc.blit(self.fbig.render(banner, True, (200, 150, 220)), ((W - tw) // 2, 8))
        if self.victory > 0:
            banner = "THE AGE IS WON — PEACE RETURNS"
            tw = self.fbig.size(banner)[0]
            pygame.draw.rect(sc, (16, 14, 26), ((W - tw) // 2 - 16, ARENA // 2 - 24, tw + 32, 48))
            pygame.draw.rect(sc, (235, 215, 130), ((W - tw) // 2 - 16, ARENA // 2 - 24, tw + 32, 48), 2)
            sc.blit(self.fbig.render(banner, True, (245, 225, 140)), ((W - tw) // 2, ARENA // 2 - 16))
        if st["health"] <= 0:
            # Downed: movement is blocked and the world is frozen until he recovers.
            # Make the way out unmistakable (slumber restores life and runs time on).
            l1, l2 = "YOU ARE CAST DOWN", "press [z] to flee and recover"
            t1, t2 = self.fbig.size(l1)[0], self.fb.size(l2)[0]
            bw = max(t1, t2) + 40
            bx, by = (W - bw) // 2, ARENA // 2 - 34
            pygame.draw.rect(sc, (26, 6, 10), (bx, by, bw, 68))
            pygame.draw.rect(sc, (220, 70, 80), (bx, by, bw, 68), 2)
            sc.blit(self.fbig.render(l1, True, (235, 110, 120)), ((W - t1) // 2, by + 10))
            sc.blit(self.fb.render(l2, True, (220, 200, 150)), ((W - t2) // 2, by + 40))
        self.render_tile_hud()
        if self.char_open:
            self.render_char_window()
        if self.commission is not None:
            self.render_recruit_window()
        if self.talk is not None:
            self.render_talk_window()
        if self.chat is not None:
            self.render_chat()
        if self.slumber is not None:
            self.render_slumber()

    def render_chat(self):
        sc = self.screen
        art = self.chat.get("creature")
        if art is not None:
            lines = [f"A vast voice — {art['creature_name']}:",
                     "  [1] Hear its tale",
                     "  (any other key: depart)"]
        elif self.chat.get("lord_npc"):
            lines = ["THE LORD broods here — what do you ask? (any other key: leave)",
                      "  [1] Take up his offer — sow corruption in his name"]
        else:
            lines = ["SPEAK — what do you ask? (any other key: leave)",
                     "  [1] Of old legends",
                     "  [2] After your agents",
                     "  [3] News of the world",
                     "  [4] Way to the nearest town"]
        if self.chat.get("answer"):
            lines += [""] + self.chat["answer"]
        bw = 560
        bh = 18 + len(lines) * 22 + 10
        bx, by = (W - bw) // 2, 70
        pygame.draw.rect(sc, (16, 18, 26), (bx, by, bw, bh))
        pygame.draw.rect(sc, (160, 200, 150), (bx, by, bw, bh), 2)
        for k, line in enumerate(lines):
            col = (170, 220, 150) if k == 0 else (215, 215, 225)
            sc.blit(self.f.render(line, True, col), (bx + 14, by + 10 + k * 22))

    def render_recruit_window(self):
        sc, S = self.screen, self.state
        com = self.commission
        wr, wc = S["wizard"]["r"], S["wizard"]["c"]
        town = S["towns"].get((wr, wc))
        if com is None or town is None or not self.folk:
            return
        race_i = S["race"][town["faction"]]
        RED, NORMAL, HEAD = (235, 80, 95), (215, 215, 225), (245, 225, 140)

        def statline(stx):
            return "  ".join(f"{d.STAT_ABBR[s]}{stx[s]:>2}" for s in range(len(d.STATS)))

        # The relic each person is best *suited* to — the one whose demands most align
        # with their strengths (demand-weighted average of their stats), among relics
        # still worth seeking. (Raw journey_fit would just pick the easiest relic for
        # everyone; alignment instead lights up a brawny person for the brawn relic, a
        # cunning one for the cunning relic, etc.) Surfaced in red.
        claimed = {it["type"] for it in S["items"]}
        seeking = {q["relic"] for q in S["recruits"] if q["task"] == "seek" and q["relic"] is not None}
        known = sorted(S["relics_known"])    # only relics whose legend the wizard has heard

        def best_relic(stx):
            avail = [t for t in known if t not in claimed and t not in seeking] or known

            def align(t):
                dem = S["relic_demand"][t]
                tot = sum(dem) or 1
                return sum(stx[s] * dem[s] for s in range(len(d.STATS))) / tot
            return max(avail, key=align) if avail else None

        def who(p):
            return (f"{p['given']} {p['house']}" if p["house"] else p["given"])[:18]

        bw = 620
        if com["phase"] == "npc":
            title = f"RECRUIT A SEEKER — House {d.house_of(S, town['faction'])} & folk (◆=noble)"
            nrows = len(self.folk[:9])
        else:
            p = self.folk[com["npc"]]
            stx = p["stats"]
            title = f"CHOSEN: {who(p)} — {d.RACES[race_i][0]} {p['age']}y  {statline(stx)}"
            nrows = 2 + len(known)
        bh = 56 + nrows * 24 + 12
        bx, by = (W - bw) // 2, 60
        pygame.draw.rect(sc, (18, 16, 28), (bx, by, bw, bh))
        pygame.draw.rect(sc, (235, 215, 130), (bx, by, bw, bh), 2)
        sc.blit(self.fb.render(title, True, HEAD), (bx + 14, by + 10))
        if com["phase"] == "npc":
            for i, p in enumerate(self.folk[:9]):
                stx = p["stats"]
                ry = by + 44 + i * 24
                mark = "◆" if p.get("notable") else " "
                base = f"[{i + 1}]{mark}{who(p):<18} {p['age']:>3}y  {statline(stx)}  "
                sc.blit(self.f.render(base, True, (245, 225, 140) if p.get("notable") else NORMAL),
                        (bx + 16, ry))
                br = best_relic(stx)
                if br is not None:
                    tag = f"best: {d.ITEM_TYPES[br]['name']}"
                    sc.blit(self.f.render(tag, True, RED), (bx + 16 + self.f.size(base)[0], ry))
        else:
            best = best_relic(stx)
            sc.blit(self.f.render("send on which journey? (their best in red)", True, NORMAL),
                    (bx + 16, by + 44))
            for n, T in enumerate(known):
                ry = by + 44 + (1 + n) * 24
                is_best = T == best
                col = RED if is_best else NORMAL
                tail = "   ◄ best fit" if is_best else ""
                sc.blit(self.f.render(f"[{n + 1}] {d.ITEM_TYPES[T]['name']:<14} "
                                      f"needs {statline(S['relic_demand'][T])}{tail}",
                                      True, col), (bx + 16, ry))
            ry = by + 44 + (1 + len(known)) * 24
            sc.blit(self.f.render("[0] just recruit them — no task yet, they'll wait for orders",
                                  True, NORMAL), (bx + 16, ry))

    def render_char_window(self):
        sc, S = self.screen, self.state
        bw, bh = 470, 70 + len(d.STATS) * 28 + 64
        bx, by = (W - bw) // 2, 70
        pygame.draw.rect(sc, (18, 16, 28), (bx, by, bw, bh))
        pygame.draw.rect(sc, (235, 215, 130), (bx, by, bw, bh), 2)
        sc.blit(self.fb.render("WIZARD — character", True, (245, 225, 140)), (bx + 16, by + 10))
        sc.blit(self.f.render("up/down: pick stat   left/right: change value   v: swap staff   c: close",
                              True, (170, 170, 185)), (bx + 16, by + 36))
        for i in range(len(d.STATS)):
            label, thresh, wired = d.WIZ_ABILITIES[i]
            v = S["wiz_stats"][i]
            if thresh is None:
                eff = f"{label}: {v} hearts"
            elif label == "Charged bolt":
                # The charge is no longer a pure Might unlock — it's an artifact quest.
                if not S.get("can_charge"):
                    eff = f"{label}: locked — seek 'the Charged Bolt' (a creature's quest)"
                elif v >= thresh:
                    eff = f"{label}: UNLOCKED (learned; Might >= {thresh})"
                else:
                    eff = f"{label}: learned, but needs Might >= {thresh}"
            else:
                eff = f"{label}: {'UNLOCKED' if v >= thresh else 'locked'} (>= {thresh})"
                if not wired:
                    eff += " [pending]"
            seld = i == self.char_sel
            col = (250, 240, 150) if seld else (210, 210, 220)
            mark = ">" if seld else " "
            sc.blit(self.f.render(f"{mark} {d.STATS[i]:<10} {v:>3}    {eff}", True, col),
                    (bx + 16, by + 64 + i * 28))
        staff = STAVES[S.get("wiz_staff", 0)]
        sy0 = by + 64 + len(d.STATS) * 28 + 4
        sc.blit(self.f.render(f"  Staff: {staff['name']}  (needs Might {staff['equip_might']})",
                              True, (160, 215, 245)), (bx + 16, sy0))
        cloak = "the Veilcloak (halves harm)" if S.get("has_cloak") else "none — seek 'the Veilcloak'"
        sc.blit(self.f.render(f"  Cloak: {cloak}", True, (200, 175, 235)), (bx + 16, sy0 + 22))

    def render_journal_window(self):
        # The quest journal — viewable at any time from any view. Lists the wizard's
        # active seekers (with progress/ETA) and the relic-journeys he knows of and
        # could send people on (their stat demands + how long a quest they are).
        sc, S = self.screen, self.state
        HEAD, NORMAL, DIM, GOLD = (245, 225, 140), (215, 215, 225), (150, 150, 170), (245, 215, 90)

        def stats_demand(t):
            dem = S["relic_demand"][t]
            return "  ".join(f"{d.STAT_ABBR[s]}{dem[s]:>2}" for s in range(len(d.STATS)))

        held = {it["type"]: it["owner"] for it in S["items"]}
        sought = {q["relic"]: q for q in S["recruits"] if q["task"] == "seek"}
        known = sorted(S["relics_known"])
        active = S["recruits"]
        arts = S.get("artifacts", [])
        chron = S.get("chronicle", [])[-6:]
        rows = (1 + max(1, len(active)) + 1 + 1 + max(1, len(known)) + 1 + 2
                + max(1, len(arts)) + 2 + max(1, len(chron)))
        bw = 660
        bh = 52 + rows * 22 + 14
        bx, by = (W - bw) // 2, 48
        pygame.draw.rect(sc, (18, 16, 28), (bx, by, bw, bh))
        pygame.draw.rect(sc, (235, 215, 130), (bx, by, bw, bh), 2)
        sc.blit(self.fb.render("QUEST JOURNAL — [j]/esc to close", True, HEAD), (bx + 14, by + 10))
        x, yy = bx + 16, by + 44

        def row(txt, col=NORMAL):
            nonlocal yy
            sc.blit(self.f.render(txt, True, col), (x, yy))
            yy += 22

        track = S["wiz_stats"][2] >= d.WIZ_WITS_SEEK_AT
        row("YOUR SEEKERS", HEAD)
        if not active:
            row("  none afoot — recruit one in a town ([k])", DIM)
        for q in active:
            nm = d.ITEM_TYPES[q["relic"]]["name"] if q["relic"] is not None else "—"
            if q["task"] == "seek":
                if track:
                    yrs = round(d.seek_eta(q["fit"], d.relic_base_cycles(q["relic"]))
                                * d.DAYS_PER_CYCLE / 365.0, 1)
                    tail = f"seeks {nm}  {round(max(0,min(100,q['prog'])))}%  ~{yrs}yr  fit {q['fit']:.0%}"
                else:
                    tail = f"seeks {nm}  (raise Wits to {d.WIZ_WITS_SEEK_AT}+ to track)"
            elif q["task"] == "march":
                tail = "marches to the front"
            elif q["task"] == "idle":
                tail = "awaiting orders ([t] to assign)"
            else:
                tail = "fighting at the front"
            if q.get("led"):
                tail += "  — DOWNED, with you" if q.get("downed") else "  — led by you"
            row(f"  {q.get('given', '#' + str(q['id']))} {short(q['fac'])}  {tail}", FACTION_RGB[q["fac"]])
        yy += 6
        row("KNOWN QUESTS  (relic · demands · length · status)", HEAD)
        if not known:
            row("  you know of no relics — hear an elder's legend in a town ([e])", DIM)
        for t in known:
            yrs = round(d.relic_base_cycles(t) * d.DAYS_PER_CYCLE / 365.0, 1)
            if t in held:
                status, scol = f"held by {short(held[t])}", DIM
            elif t in sought:
                status, scol = f"sought by #{sought[t]['id']}", GOLD
            else:
                status, scol = "open", (140, 220, 150)
            base = f"  {d.ITEM_TYPES[t]['name']:<15} {stats_demand(t)}   ideal ~{yrs}yr   "
            sc.blit(self.f.render(base, True, NORMAL), (x, yy))
            sc.blit(self.f.render(status, True, scol), (x + self.f.size(base)[0], yy))
            yy += 22
        unheard = len(d.ITEM_TYPES) - len(known)
        if unheard > 0:
            yy += 4
            row(f"{unheard} legend{'s' if unheard != 1 else ''} yet unheard — seek out town elders", DIM)
        # The wizard's OWN quests: gear sought in person (artifacts).
        yy += 6
        row("YOUR OWN QUESTS  (gear you must seek yourself)", HEAD)
        if not arts:
            row("  none", DIM)
        unknown_n = 0
        for a in arts:
            if a["found"]:
                txt, col = f"  {a['name']} — claimed and yours", (140, 220, 150)
            elif a["told"]:
                txt, col = (f"  {a['name']} — lies at ({a['loc'][0]},{a['loc'][1]}); "
                            f"guarded. Go in person.", GOLD)
            elif a["known"]:
                txt, col = (f"  {a['name']} — rumoured; seek {a['creature_name']} "
                            f"to learn more", DIM)
            else:
                unknown_n += 1
                continue
            row(txt, col)
        if unknown_n > 0:
            row(f"  {unknown_n} tale{'s' if unknown_n != 1 else ''} yet unheard — seek out town elders", DIM)
        # The chronicle: recent deeds of the world (what unfolded while you slept).
        yy += 6
        row("CHRONICLE  (recent deeds)", HEAD)
        if not chron:
            row("  the world is quiet so far", DIM)
        for yr, msg in chron:
            row(f"  yr {yr}: {msg}"[:78], (200, 200, 215))

    def render_tile_hud(self):
        st = self.screen
        S = self.state
        pygame.draw.rect(st, (16, 16, 24), (0, ARENA, W, HUD))
        y = ARENA + 6
        hp = S["health"]
        hearts = "".join("♥" if i < hp else "♡" for i in range(d.wiz_max_hearts(S)))
        st.blit(self.fb.render(hearts, True, (235, 90, 90)), (10, y))
        en = S["energy"]
        st.blit(self.f.render(f"Energy {round(en):3d}/100   Food {S['food']}   Gold {S['gold']}g"
                              f"   Day {S['day']:.0f}", True, (200, 200, 210)), (10, y + 26))
        where = d.merchant_here(S)
        if where:
            hint = f"{'market' if where=='town' else 'pedlar'} here — [b] ration ({d.FOOD_PRICE}g)"
            if where == "town":
                hint += f" · [u] map ({d.MAP_PRICE}g)"
            st.blit(self.f.render(hint, True, (240, 220, 120)), (10, y + 44))
        # Recruits roster — each a persistent character, right-aligned. Seekers show
        # progress + ETA but only if your Wits are keen enough to track them.
        if S["recruits"]:
            def rblit(s, ry, col):
                st.blit(self.f.render(s, True, col), (W - 10 - self.f.size(s)[0], ry))
            track = S["wiz_stats"][2] >= d.WIZ_WITS_SEEK_AT
            rblit(f"RECRUITS  {len(S['recruits'])}/{d.MAX_RECRUITS}", y, (235, 235, 245))
            for k, q in enumerate(S["recruits"][:3]):
                col = FACTION_RGB[q["fac"]]
                nm_tag = q.get("given", f"#{q['id']}")
                if q["task"] == "seek":
                    nm = d.ITEM_TYPES[q["relic"]]["name"][:10]
                    if track:
                        prog = max(0.0, min(100.0, q["prog"]))
                        yrs = round(d.seek_eta(q["fit"], d.relic_base_cycles(q["relic"]))
                                    * d.DAYS_PER_CYCLE / 365.0, 1)
                        line = f"{nm_tag} {short(q['fac'])} seeks {nm} {round(prog)}% ~{yrs}yr"
                    else:
                        line = f"{nm_tag} {short(q['fac'])} seeks {nm} (Wits {d.WIZ_WITS_SEEK_AT}+)"
                elif q["task"] == "march":
                    line = f"{nm_tag} {short(q['fac'])} marching to the front"
                elif q["task"] == "idle":
                    line = f"{nm_tag} {short(q['fac'])} awaiting orders"
                else:
                    line = f"{nm_tag} {short(q['fac'])} fighting at the front"
                rblit(line, y + 20 + k * 18, col)
            extra = len(S["recruits"]) - 3
            if extra > 0:
                rblit(f"+{extra} more", y + 20 + 3 * 18, (150, 150, 170))
        if self.mode == "contested":
            self.render_battle_hud(y + 64)
        msg = self.state.get("town_msg")
        line2 = msg if msg else "arrows move · space smite · e talk · c char · j quests · k recruit · t orders · 1/2 aid · f eat · b ration · u map · z sleep · TAB view · q quit"
        st.blit(self.f.render(line2[:90], True, (150, 150, 170) if not msg else (250, 235, 140)), (10, ARENA + HUD - 22))

    def render_battle_hud(self, y):
        sc, S = self.screen, self.state
        x = 10
        for fac in (self.lo_fac, self.hi_fac):
            hot = S["favor"][fac] <= WAR_FAVOR
            txt = f"{short(fac)} favor {S['favor'][fac]:.1f}{' WAR' if hot else ''}"
            sc.blit(self.f.render(txt, True, (255, 80, 80) if hot else FACTION_RGB[fac]), (x, y))
            x += 230
        bat = next((b for b in S["battles"] if {b["winner"], b["loser"]} == {self.lo_fac, self.hi_fac}), None)
        if bat:
            prog = 0 if bat["tot"] <= 0 else max(0, min(1, 1 - bat["rem"] / bat["tot"]))
            left = max(1, math.ceil(bat["rem"] / bat["rate"])) if bat["rate"] else 1
            fill = int(prog * 18)
            line = f"{short(bat['winner'])} taking tile [{'#'*fill}{'-'*(18-fill)}] ~{left}c"
            helped = self.lo_fac if S["favor"][self.lo_fac] > S["favor"][self.hi_fac] else self.hi_fac
            if helped == bat["loser"]:
                line += f"  holding {bat.get('resist',0)}/{d.AID_FLIP} to FLIP"
            sc.blit(self.f.render(line, True, (225, 225, 240)), (10, y + 20))
        else:
            sc.blit(self.f.render("no battle rages here now", True, (180, 180, 195)), (10, y + 20))

    def render_map(self):
        sc, S = self.screen, self.state
        sc.fill((10, 10, 14))
        live, snap = S.get("map_live"), S.get("map_snapshot")
        # The player is blind until he charts a map in a town (unless testing-live).
        if not live and snap is None:
            for k, line in enumerate([f"You have no map of the world.",
                                      f"Buy a fresh chart at a town's market ([u], {d.MAP_PRICE}g) —",
                                      "it stays as it was until you buy another.",
                                      "( [l] toggles a live map, for testing )"]):
                t = self.fb.render(line, True, (210, 200, 170))
                sc.blit(t, ((W - t.get_width()) // 2, ARENA // 2 - 40 + k * 28))
            return
        if self.map_mode == "world":
            self.render_world_geo(live, snap)
            return
        # Map source: the frozen snapshot, or the live world if testing.
        owner = S["owner"] if live else snap["owner"]
        corr = S["corruption"] if live else snap["corr"]
        terr = S["territory"] if live else snap["terr"]
        army = S["army"] if live else snap["army"]
        shadow = S["shadow"] if live else snap["shadow"]
        cell = ARENA / d.GRID
        bands = [1.0, 0.78, 0.56, 0.36]                  # brightness per corruption band
        for r in range(d.GRID):
            for c in range(d.GRID):
                o = owner[r][c]
                col = shade(FACTION_RGB[o], bands[d._corrupt_band(corr[o])])
                pygame.draw.rect(sc, col, (int(c * cell), int(r * cell), math.ceil(cell), math.ceil(cell)))
        self.draw_map_markers(cell)
        # readout (from the same source as the map — frozen unless testing-live)
        pygame.draw.rect(sc, (16, 16, 24), (0, ARENA, W, HUD))
        champ, seat = self.champion_seat()
        sense = S["wiz_stats"][2] >= d.WIZ_WITS_SENSE_AT
        dom = sum(corr[i] * terr[i] for i in range(d.N)) / 100.0
        foc = max(range(d.N), key=lambda i: corr[i])
        now_day = S["day"]
        if live:
            chart = "LIVE (testing)"
        else:
            chart = f"charted day {snap['day']:.0f} ({now_day - snap['day']:.0f}d stale)"
        self.screen.blit(self.fb.render(f"POWERS MAP — {chart}   Shadow {shadow*100:.1f}%  "
                         f"dominion {dom*100:.1f}%   [g] world map", True, (230, 230, 240)), (10, ARENA + 6))
        lord = S.get("lord")
        if dom >= d.DOMINION_WIN:
            tail = "THE WORLD HAS FALLEN"
        elif champ is not None and lord is not None and sense:
            who = f"{short(champ)}'s dark lord"
            if lord.get("enthroned"):
                tail = f"{who} broods on his throne ({seat[0]},{seat[1]}) — storm the castle"
            else:
                task, tgt = lord.get("task"), lord.get("target")
                cor = round(S["corruption"][champ] * 100)
                if task == "seek" and tgt is not None:
                    doing = f"hunts the {d.ITEM_TYPES[tgt]['name']} ({round(lord.get('prog', 0))}%)"
                elif task == "march" and tgt is not None:
                    doing = f"rides to war on {short(tgt)}"
                elif task == "return":
                    doing = "rides home to his throne"
                else:
                    doing = f"rises ({cor}%→50) — roams the marches"
                tail = f"{who} {doing} — ({seat[0]},{seat[1]}); strike now"
        elif champ is not None and not sense:
            tail = "a shadow stirs — but your Wits are too dull to sense the lord (raise Wits)"
        else:
            tail = f"the age is at peace — no power has fallen (vessel forming: {short(foc)})"
        self.screen.blit(self.f.render(tail[:96], True, (210, 120, 230)), (10, ARENA + 30))
        for i in range(d.N):
            lbl = d.CORRUPT_BANDS[d._corrupt_band(corr[i])][3]
            txt = (f"{short(i):<8} {terr[i]:4.1f}%  army {round(army[i]):3d}  "
                   f"favor {S['favor'][i]:>4.1f}/10  {lbl:<8} {corr[i]*100:3.0f}%{'  <vessel' if i==foc else ''}")
            self.screen.blit(self.f.render(txt, True, FACTION_RGB[i]), (10, ARENA + 50 + i * 18))
        self.screen.blit(self.f.render("arrows walk (1 tile=10d) · space auto · [ ] speed · g world-map · l live · TAB view",
                         True, (130, 130, 150)), (10, ARENA + HUD - 20))

    GEO_MAP_RGB = [(74, 122, 64), (52, 98, 170), (120, 116, 120),
                   (193, 168, 120), (150, 108, 66), (165, 150, 110)]   # by GEO_* code

    def bake_world_surf(self):
        # Render the 1000×1000 micro geo grid to a Surface once (cached per world_seed),
        # then scale to the arena — far cheaper than drawing a million rects each frame.
        S = self.state
        geo, MW = S["geo"], S["geo_w"]
        lut = [bytes(c) for c in self.GEO_MAP_RGB]
        buf = bytearray(len(geo) * 3)
        for i in range(len(geo)):
            buf[i * 3:i * 3 + 3] = lut[geo[i]]
        surf = pygame.image.frombuffer(bytes(buf), (MW, MW), "RGB")
        self._world_surf = pygame.transform.smoothscale(surf, (ARENA, ARENA))
        self._world_surf_seed = S["world_seed"]

    def bake_fog_surf(self):
        # A black overlay covering every macro tile not yet charted — pieced together
        # circle by circle as the wizard charts maps in different towns. Cached and
        # only rebuilt when the revealed set actually grows.
        S = self.state
        revealed = S.get("map_revealed")
        cell = ARENA / d.GRID
        surf = pygame.Surface((ARENA, ARENA))
        surf.fill((0, 0, 0))
        surf.set_colorkey((0, 0, 0))
        fog = (14, 12, 18)
        for rr in range(d.GRID):
            row_off = rr * d.GRID
            for cc in range(d.GRID):
                if revealed is None or not revealed[row_off + cc]:
                    pygame.draw.rect(surf, fog, (int(cc * cell), int(rr * cell),
                                                  math.ceil(cell) + 1, math.ceil(cell) + 1))
        self._fog_surf = surf
        self._fog_revealed_n = sum(revealed) if revealed is not None else 0

    def render_world_geo(self, live, snap):
        # The world map of GEOGRAPHY: the connectivity-first micro terrain (rivers,
        # mountain ranges, and the road network with its bridge/pass crossings) — the
        # wizard's traversal world seen whole, but only pieced together gradually:
        # each town chart uncovers a circle of it (a capital's, twice as wide), and
        # past circles accumulate (state["map_revealed"]). Unrevealed land is fogged.
        sc, S = self.screen, self.state
        if getattr(self, "_world_surf_seed", None) != S["world_seed"]:
            self.bake_world_surf()
        sc.blit(self._world_surf, (0, 0))
        revealed = S.get("map_revealed")
        n = sum(revealed) if revealed is not None else 0
        if getattr(self, "_fog_revealed_n", None) != n:
            self.bake_fog_surf()
        sc.blit(self._fog_surf, (0, 0))
        cell = ARENA / d.GRID
        self.draw_map_markers(cell)
        pygame.draw.rect(sc, (16, 16, 24), (0, ARENA, W, HUD))
        geo = S["geo"]
        br = geo.count(d.GEO_BRIDGE)
        ps = geo.count(d.GEO_PASS)
        self.screen.blit(self.fb.render("WORLD MAP — geography (your traversal terrain)   [g] powers map",
                         True, (230, 230, 240)), (10, ARENA + 6))
        legend = [("land", self.GEO_MAP_RGB[d.GEO_LAND]), ("river", GEO_PAINT_RGB["water"]),
                  ("range", GEO_PAINT_RGB["rock"]), ("road", GEO_PAINT_RGB["road"]),
                  ("bridge", GEO_PAINT_RGB["bridge"]), ("pass", GEO_PAINT_RGB["pass"])]
        x = 10
        for name, col in legend:
            pygame.draw.rect(self.screen, col, (x, ARENA + 32, 14, 14))
            self.screen.blit(self.f.render(name, True, (210, 210, 220)), (x + 18, ARENA + 31))
            x += 30 + self.f.size(name)[0]
        self.screen.blit(self.f.render(f"{br} bridges · {ps} pass-tiles · cross rivers at bridges, "
                         f"ranges at passes — route around the rest",
                         True, (180, 180, 195)), (10, ARENA + 52))
        self.screen.blit(self.f.render("arrows walk (1 tile=10d) · g powers-map · l live · TAB view · r reset",
                         True, (130, 130, 150)), (10, ARENA + HUD - 20))

    def draw_map_markers(self, cell):
        # Towns/cities, the sensed dark lord, recruits (+ their routes), and the wizard —
        # the markers shared by both map modes.
        sc, S = self.screen, self.state
        # Towns & cities: villages are small white dots; a capital is a CITY — its 2x2
        # block of tiles each drawn as a white-ringed faction square (forming the city),
        # with the name labelled once on the anchor tile.
        cap_labels = []
        for (tr, tc), t in S["towns"].items():
            cx, cy = int(tc * cell + cell / 2), int(tr * cell + cell / 2)
            if t["capital"]:
                rect = (int(tc * cell), int(tr * cell), math.ceil(cell) + 1, math.ceil(cell) + 1)
                pygame.draw.rect(sc, FACTION_RGB[t["faction"]], rect)
                pygame.draw.rect(sc, (255, 255, 255), rect, 1)
                if t.get("main"):
                    cap_labels.append((cx, cy, t))
            else:
                pygame.draw.rect(sc, (245, 245, 250), (cx - 3, cy - 3, 6, 6))
                pygame.draw.rect(sc, (18, 18, 26), (cx - 3, cy - 3, 6, 6), 1)
                pygame.draw.rect(sc, FACTION_RGB[t["faction"]], (cx - 1, cy - 1, 2, 2))
        for cx, cy, t in cap_labels:
            house = d.house_of(self.state, t["faction"])
            name = (f"{t['name']} · {house}" if house else t["name"])[:22]
            tw = self.fs.size(name)[0]
            lx = cx - 10 - tw if cx + 10 + tw > W else cx + 10
            ly = cy - 6
            self.screen.blit(self.fs.render(name, True, (10, 10, 14)), (lx + 1, ly + 1))
            self.screen.blit(self.fs.render(name, True, (245, 245, 250)), (lx, ly))
        # The dark champion's seat — a black ring crowning its clan's capital, so the
        # player can see where the age must be decided.
        champ, seat = self.champion_seat()   # seat == the lord's CURRENT position
        # The wizard senses the shadow's lieutenant only if his Wits are keen enough
        # (the Wits unlock); otherwise the lord stays hidden. Drawn as an unmistakable
        # dark figure with a red eye at his current tile, so you can hunt him down.
        sense = S["wiz_stats"][2] >= d.WIZ_WITS_SENSE_AT
        if seat is not None and sense:
            scx, scy = int(seat[1] * cell + cell / 2), int(seat[0] * cell + cell / 2)
            rr = max(8, int(cell * 1.1))
            pygame.draw.circle(sc, (20, 6, 26), (scx, scy), rr)            # shadow body
            pygame.draw.circle(sc, (235, 55, 75), (scx, scy), rr, 3)       # red rim
            pygame.draw.circle(sc, (255, 95, 115), (scx, scy), max(2, rr // 3))  # red eye
        # The sower (sown corruption model): a dark agent roaming to a high-mismatch realm
        # to sow division — drawn as a small purple ✦ so you can watch where the shadow grows.
        sow = S.get("sower")
        if sow is not None and sow.get("cd", 0) <= 0:
            ox, oy = int(sow["c"] * cell + cell / 2), int(sow["r"] * cell + cell / 2)
            rr = max(4, int(cell * 0.7))
            pygame.draw.circle(sc, (28, 6, 40), (ox, oy), rr)
            pygame.draw.circle(sc, (180, 90, 220), (ox, oy), rr, 2)
            pygame.draw.line(sc, (180, 90, 220), (ox - rr, oy), (ox + rr, oy), 1)
            pygame.draw.line(sc, (180, 90, 220), (ox, oy - rr), (ox, oy + rr), 1)
        # The lord's retinue (sown model): smaller violet dots, sensed alongside him.
        if sense:
            for ag in S.get("lord_agents", []):
                ax, ay = int(ag["c"] * cell + cell / 2), int(ag["r"] * cell + cell / 2)
                rr = max(3, int(cell * 0.55))
                pygame.draw.circle(sc, (40, 14, 50), (ax, ay), rr)
                pygame.draw.circle(sc, (150, 90, 190), (ax, ay), rr, 2)
        # Recruits show on the map only while they're keeping to their route (on_path —
        # a high-fit agent usually is; a poor fit strays and vanishes, and you must ask
        # around for a bearing instead). Faction dot; gold ring = fighting at a front.
        for q in S["recruits"]:
            if not q.get("on_path"):
                continue
            rx, ry = int(q["c"] * cell + cell / 2), int(q["r"] * cell + cell / 2)
            rr = max(3, int(cell * 0.6))
            # The route they're walking: a faint line to where they're headed (a seeker's
            # roaming waypoint, or a marcher's war front).
            tgt = q.get("target")
            if tgt:
                tx, ty = int(tgt[1] * cell + cell / 2), int(tgt[0] * cell + cell / 2)
                pygame.draw.line(sc, shade(FACTION_RGB[q["fac"]], 0.55), (rx, ry), (tx, ty), 1)
                pygame.draw.circle(sc, shade(FACTION_RGB[q["fac"]], 0.7), (tx, ty), 2)
            pygame.draw.circle(sc, FACTION_RGB[q["fac"]], (rx, ry), rr)
            ring = (245, 215, 90) if q["task"] == "fight" else (240, 240, 248)
            pygame.draw.circle(sc, ring, (rx, ry), rr, 2)
        # Artifact quests: a mythical creature shows as a violet star until you've heard
        # its tale; thereafter the gear's resting place shows as a gold ✦ to seek.
        for a in S.get("artifacts", []):
            if a["found"]:
                continue
            if not a["known"]:
                continue                  # no elder has spoken of it, and it's not been found
            if not a["told"]:
                cr, cc = a["creature"]
                mx, my = int(cc * cell + cell / 2), int(cr * cell + cell / 2)
                r0 = max(4, int(cell * 0.8))
                pygame.draw.circle(sc, (190, 110, 235), (mx, my), r0)
                pygame.draw.circle(sc, (245, 235, 255), (mx, my), r0, 2)
                tag = f"{a['creature_name']} — {a['name']} ({cr},{cc})"
                self.screen.blit(self.fs.render(tag, True, (235, 215, 255)), (mx + r0 + 2, my - 6))
            else:
                sr, sc_ = a["loc"]
                mx, my = int(sc_ * cell + cell / 2), int(sr * cell + cell / 2)
                r0 = max(5, int(cell))
                pygame.draw.circle(sc, (245, 215, 90), (mx, my), r0)
                pygame.draw.circle(sc, (60, 40, 10), (mx, my), r0, 2)
                self.screen.blit(self.fs.render(a["name"][:12], True, (250, 235, 150)),
                                 (mx + r0 + 2, my - 6))
        wz = S["wizard"]
        wx, wy = int(wz["c"] * cell + cell / 2), int(wz["r"] * cell + cell / 2)
        pygame.draw.circle(sc, (255, 255, 255), (wx, wy), max(3, int(cell)))
        pygame.draw.circle(sc, (40, 30, 90), (wx, wy), max(3, int(cell)), 2)

    def render_controls(self):
        sc, S = self.screen, self.state
        sc.fill((12, 12, 18))
        sc.blit(self.fb.render(f"CONTROLS [dev] — up/down · left/right adjust · TAB view · "
                 f"[o] corruption: {S.get('corruption_growth', 'ambient').upper()}",
                 True, (230, 230, 240)), (10, 8))

        # Active battles, keyed by unordered pair, for the relations annotations.
        bat_by_pair = {frozenset((b["winner"], b["loser"])): b for b in S["battles"]}

        def slider_row(y, idx, label, val, frac, col, tail="", tail_col=None):
            sel = idx == self.sel
            if sel:
                pygame.draw.rect(sc, (40, 40, 60), (4, y - 1, W - 8, 17))
            barw, bx = 200, 230
            pygame.draw.rect(sc, (40, 40, 50), (bx, y + 2, barw, 9))
            pygame.draw.rect(sc, col, (bx, y + 2, int(barw * max(0, min(1, frac))), 9))
            txt = f"{'>' if sel else ' '} {label:<18} {val:>7}"
            sc.blit(self.f.render(txt, True, (235, 235, 245) if sel else col), (10, y))
            if tail:
                sc.blit(self.f.render(tail, True, tail_col or (150, 150, 170)), (bx + barw + 12, y))

        y = 38
        for idx, ctrl in enumerate(self.ctrls):
            kind = ctrl[0]
            # Section break: the heart pairs are pulled out of the per-faction
            # groups into one "BATTLES & RELATIONS" block between everyone.
            if kind == "heart" and (idx == 0 or self.ctrls[idx - 1][0] != "heart"):
                y += 10
                sc.blit(self.fb.render("BATTLES & RELATIONS — between everyone",
                         True, (235, 120, 120)), (10, y))
                y += 24
            if kind == "shadow":
                slider_row(y, idx, "Shadow", f"{S['shadow']*100:.1f}%", S["shadow"], (210, 120, 230))
            elif kind == "terr":
                i = ctrl[1]; slider_row(y, idx, f"{short(i)} land", f"{S['territory'][i]:.1f}%", S["territory"][i] / 100, FACTION_RGB[i])
            elif kind == "army":
                i = ctrl[1]; slider_row(y, idx, f"{short(i)} army", f"{round(S['army'][i])}", S["army"][i] / 100, FACTION_RGB[i])
            elif kind == "corrupt":
                i = ctrl[1]; slider_row(y, idx, f"{short(i)} corrupt", f"{S['corruption'][i]*100:.0f}%", S["corruption"][i], FACTION_RGB[i])
            else:
                i, j = ctrl[1], ctrl[2]
                hv = S["hearts"][i][j]
                status = d.status_of(hv)[0]
                bat = bat_by_pair.get(frozenset((i, j)))
                if bat is not None:
                    done = 0 if bat["tot"] <= 0 else max(0, min(100, round((1 - bat["rem"] / bat["tot"]) * 100)))
                    tail = f"⚔ {short(bat['winner'])} taking tile {done}%"
                    tail_col = (250, 200, 90)
                else:
                    tail, tail_col = status, (235, 120, 120) if status in ("AT WAR", "ENEMIES") else (150, 150, 170)
                slider_row(y, idx, f"{short(i)}<->{short(j)}", f"{hv}", hv / 10, FACTION_RGB[i], tail, tail_col)
            y += 18
            if kind == "corrupt":   # small gap between faction groups
                y += 6
        # Time-series graphs of the macro quantities, along the bottom (history window).
        self.render_graphs(top=max(y + 8, ARENA - 196))

    def _chart(self, rect, series, ymax, title, ymin=0.0, marks=()):
        # A small line chart: series = list of (values, color, width); marks = [(y,color)].
        sc = self.screen
        x, yy, w, h = rect
        pygame.draw.rect(sc, (18, 18, 26), (x, yy, w, h))
        pygame.draw.rect(sc, (44, 44, 58), (x, yy, w, h), 1)
        sc.blit(self.fs.render(title, True, (200, 200, 215)), (x + 4, yy + 2))
        px, py = x + 4, yy + 16
        pw, ph = w - 8, h - 20
        span = (ymax - ymin) or 1.0

        def ypix(v):
            return py + ph - int((max(ymin, min(ymax, v)) - ymin) / span * ph)
        for mv, mc in marks:
            yv = ypix(mv)
            pygame.draw.line(sc, mc, (px, yv), (px + pw, yv), 1)
        for values, col, wd in series:
            n = len(values)
            if n < 2:
                continue
            pts = [(px + int(i / (n - 1) * pw), ypix(v)) for i, v in enumerate(values)]
            pygame.draw.lines(sc, col, False, pts, wd)

    def render_graphs(self, top):
        sc, S = self.screen, self.state
        hist = S.get("history", [])
        dom = sum(S['corruption'][i] * S['territory'][i] for i in range(d.N)) / 100
        head = (f"GRAPHS — last {len(hist) * d.DAYS_PER_CYCLE // 360}yr · "
                f"Shadow {S['shadow'] * 100:.0f}% · dominion {dom:.0%}  "
                f"(top: per-realm · bottom: corruption×mismatch → war pressure → relations)")
        sc.blit(self.fb.render(head[:96], True, (235, 120, 230)), (10, top))
        gy = top + 20
        n = len(hist)
        if n < 2:
            sc.blit(self.f.render("(gathering data — step/auto-play or slumber to fill)",
                                  True, (150, 150, 170)), (12, gy + 8))
            return
        fac_cols = [FACTION_RGB[i] for i in range(d.N)]
        pairs = [(i, j) for i in range(d.N) for j in range(i + 1, d.N)]
        pair_cols = [tuple((FACTION_RGB[i][k] + FACTION_RGB[j][k]) // 2 for k in range(3))
                     for (i, j) in pairs]

        def series(key, idx):
            return [h.get(key, [0] * 9)[idx] for h in hist]
        # 3 columns × 2 rows. Top: per-realm drivers/outputs. Bottom: the want machinery.
        m, gap = 8, 6
        pw = (W - 2 * m - 2 * gap) // 3
        ph = (ARENA - gy - 6 - gap) // 2
        rc = lambda col, rowi: (m + col * (pw + gap), gy + rowi * (ph + gap), pw, ph)
        fac4 = lambda key: [(series(key, i), fac_cols[i], 2) for i in range(d.N)]
        pair6 = lambda key: [(series(key, p), pair_cols[p], 1) for p in range(len(pairs))]
        tmax = max(1.0, max(h["terr"][i] for h in hist for i in range(d.N)) * 1.1)
        amax = lambda key: max(0.02, max(max(h.get(key, [0] * 9)) for h in hist) * 1.2)
        # top row — per-realm
        self._chart(rc(0, 0), fac4("cor"), 1.0, "Corruption /realm", marks=[(d.FALLEN_AT, (120, 60, 60))])
        self._chart(rc(1, 0), fac4("terr"), tmax, "Territory % /realm")
        self._chart(rc(2, 0), fac4("army"), 100.0, "Army /realm")
        # bottom row — corruption×mismatch pressure (per pair) → relations
        self._chart(rc(0, 1), pair6("apull"), amax("apull"),
                    "ARMY mismatch×corrupt → war-skew /pair")
        self._chart(rc(1, 1), pair6("rpull"), amax("rpull"),
                    "RELIC mismatch×corrupt → war-skew /pair")
        self._chart(rc(2, 1), pair6("hearts"), 10.0, "Relations (hearts) /pair",
                    marks=[(3, (150, 60, 60))])

    # ----- loop -----
    def run(self):
        self.running = True
        while self.running:
            dt = self.clock.tick(60) / 1000.0
            for e in pygame.event.get():
                if e.type == pygame.QUIT:
                    self.running = False
                elif e.type == pygame.KEYDOWN:
                    self.on_key(e)
                elif e.type == pygame.KEYUP:
                    self.on_keyup(e)
            self.update(dt)
            self.render()
        pygame.quit()


if __name__ == "__main__":
    Game().run()
