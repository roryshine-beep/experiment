#!/usr/bin/env python3
"""
Dominion — terminal edition.

A faithful port of index.html's territory-control simulation to a curses TUI.
Pure stdlib, no browser, no dependencies. Uses a few MB of RAM by design — the
browser version was the thing that could eat a machine; this can't.

Controls:
  space      play / pause          up / down   move selection
  s          step one cycle        left/right  adjust selected value (or - / +)
  r          reset                 [ / ]       slower / faster
  q          quit
"""
import atexit
import curses
import locale
import math
import os
import random
import shutil
import signal
import subprocess
import time

import chiptune  # pure-stdlib MIDI -> chiptune WAV renderer (optional background music)

# ---------- Configuration (mirrors index.html) ----------
# Each power is a coloured **realm** (fixed: the colour and its biome never change)
# peopled by a **race** (Elves/Men/Dwarves/Cyclopes) — superficial for now, but
# each race will grow its own state later. Which race holds which realm is rolled
# fresh every run (assign_races, called from fresh_state): POWERS' display names
# and FACTION_EMOJI are rebuilt from REALMS + that rolled order, so the colour
# identity is stable while who-is-what is random.
REALMS = [
    ("Crimson", curses.COLOR_RED),
    ("Azure",   curses.COLOR_BLUE),
    ("Verdant", curses.COLOR_GREEN),
    ("Gilded",  curses.COLOR_YELLOW),
]
RACES = [("Elves", "🧝"), ("Men", "💂"), ("Dwarves", "🧔"), ("Cyclopes", "🧌")]
# POWERS / FACTION_EMOJI hold the *current run's* assignment; seeded here with a
# default order so module load (N, colour pairs) is valid before fresh_state rolls.
POWERS = [(f"{realm} {RACES[i][0]}", col) for i, (realm, col) in enumerate(REALMS)]
N = len(POWERS)

# Magical relics: higher boost = rarer (lower spawn weight). Max 3 in the world.
ITEM_TYPES = [
    {"name": "Iron Banner",    "icon": "#", "emoji": "🚩", "boost": 0.075, "weight": 50},
    {"name": "Sunsteel Blade", "icon": "/", "emoji": "🗡", "boost": 0.15,  "weight": 26},
    {"name": "Stormcrown",     "icon": "^", "emoji": "👑", "boost": 0.25,  "weight": 13},
    {"name": "Dragon Heart",   "icon": "*", "emoji": "💎", "boost": 0.425, "weight": 5},
]
MAX_ITEMS = 3
MIN_TERR = 1.5
# Battles closer than this in relative strength are stalemates: no land changes
# hands (both sides just bleed). Above it, land moves in proportion to the gap.
STALEMATE_GAP = 0.12

# Wizard intervention: the wizard can back one side of a battle. Each cycle of aid
# boosts that side's effective strength; sustained aid to the *losing* side can
# turn the tide. When the engagement ends, the side he backed warms to him and
# the other sours — tracked as `favor` (each faction's standing toward the wizard,
# 0..10, starting neutral; it never drifts, only the wizard's deeds move it).
AID_BOOST = 0.35       # +35% effective strength to the aided side, that cycle
AID_RATE_MULT = 2.0    # an aided winner presses its advantage (faster land transfer)
AID_FLIP = 3           # consecutive cycles aiding the loser before the battle flips
FAVOR_NEUTRAL = 5      # the wizard starts neutral with every faction
AID_FAVOR_MAX = 3      # most favor one engagement can swing

# Wizard energy (0..ENERGY_MAX): aiding a battle is tiring. He recovers by eating
# food (bought for gold from a town market or a roaming pedlar) or by slumbering —
# but slumber lets the world cycle on without him while he sleeps.
ENERGY_MAX = 100
AID_ENERGY = 8           # energy spent per cycle of battle-aid
FOOD_ENERGY = 30         # energy restored per ration eaten
FOOD_PRICE = 10          # gold per ration
MAP_PRICE = 10           # gold for a fresh chart of the world, bought in a town
MAP_REVEAL_RADIUS = 6    # tiles: a village's chart uncovers a circle of this radius
MAP_REVEAL_RADIUS_CAPITAL = MAP_REVEAL_RADIUS * 2   # a capital's chart, twice as far
START_FOOD = 3
START_GOLD = 60
SLUMBER_DAYS_PER_ENERGY = 1.0  # world-days per point regained (slumber recovers to FULL)
PEDLAR_CHANCE = 0.15     # chance a travelling merchant is on a tile the wizard steps onto

# Wizard life (player-facing, like energy — not hidden like corruption). The
# shadow's minions in fallen lands fire on him; each hit costs a heart. At zero
# he is struck down and must slumber to recover. (Distinct from state["hearts"],
# which is the faction *relationship* matrix.)
WIZ_HEARTS = 10              # the wizard's life total
SLUMBER_DAYS_PER_HEART = 1.0  # world-days slumbered per heart restored

# Seekers: in a town the wizard can send an NPC after a relic for a chosen clan.
# It costs energy and plays out off-screen as rising odds — but the world may hand
# that relic to someone else first, and then the errand fails.
SEARCH_ENERGY = 15       # energy to dispatch a seeker
SEARCH_GROW = (0.004, 0.020)  # per-cycle growth of the find-chance (low, with variance)
# Recruited NPCs become persistent characters (like the wizard and the lord): they
# roam the map on a task (seek a relic / march to a front / fight there), are visible
# and catchable, and reassignable when spoken to. See add_recruit / _update_recruits.
MAX_RECRUITS = 7         # most recruits (characters) the wizard can hold at once — this is
                         # the WIZARD's own retinue, distinct from the dark lord's retinue
                         # (state["lord_agents"], a separate cast — see _update_lord_agents).
RECRUIT_GROUP_CAP = max(1, round(MAX_RECRUITS * 0.3))   # at most this many of the wizard's
                         # recruits may be put on ONE shared quest at once (see join_quest /
                         # _update_recruits) — a party, not a race: they share one progress
                         # bar, scored by their group's elementwise-max stats vs the relic's
                         # demand, so a redundant stat-twin barely helps while a recruit who
                         # covers a stat the party lacks meaningfully speeds the quest.
RECRUIT_STRENGTH = 8.0   # effective-strength a fighting recruit lends its faction
RECRUIT_ENCOUNTER = 0.12 # peril × this = per-cycle chance a traveling recruit meets monsters
RECRUIT_BEASTS = ["a pack of wargs", "a hill-troll", "a cave-troll", "shadow-wolves",
                  "a band of orcs", "a fell beast", "a great spider", "barrow-wights"]

# The fallen god: the world was made good, but a vengeful presence (`shadow`,
# 0..1) creeps over it and seeps into the powers as `corruption` (0..1 each). The
# good god is hands-off (he sent the wizard). Corruption never shows in the
# wizard's view — only the dev views reveal it. It bends the relationship drift:
# the corrupt are belligerent, a corruption *gap* sours relations (the pure vs the
# fallen), and shared corruption *bonds* (the fallen flock together).
SHADOW_START = 0.02      # the presence begins tiny
# The shadow rises briskly through the unseen pre-history (so the wizard arrives
# into a world still ALIVE — a tipping point, not a corpse: territory consolidates
# fast, so a slow shadow only ever bites after the board has collapsed), then
# creeps slowly during play (an ominous, resistible decline). age_to_danger()
# switches the rate over at arrival.
AGE_SHADOW_GROWTH = 0.0020  # pre-history rate (off-screen): reach the tipping point fast
SHADOW_GROWTH = 0.0004      # playable rate: a slow creep the wizard can fight
CORRUPT_REVERT = 0.04    # how fast a power drifts toward its corruption equilibrium
CORRUPT_NOISE = 0.012   # jitter on that drift
# Each power's corruption gravitates toward shadow * its (hidden) susceptibility,
# so the shadow's growth sets the ceiling while susceptibility makes some powers
# fall far while others stay near-pure — a persistent divergence, not homogeneity.
SUSCEPT_RANGE = (0.25, 1.10)
CORRUPT_AGGR = 0.30    # corruption -> belligerence (hearts trend down: vie for power)
CORRUPT_SPLIT = 0.45   # a corruption *gap* -> hearts fall (pure distrust the fallen)
CORRUPT_BOND = 0.40    # shared high corruption pulls hearts up — but it only barely
                       # outweighs the belligerence above, so the fallen *may* ally,
                       # not always (their shared hunger for power pulls them apart)
# Wants (Layer 1): corruption *amplifies* how much a MISMATCH with a rival sours
# relations, so wars gain a motive — all inferred from existing state (no new content).
# Relic-wealth = Σ of a faction's owned relics' `boost`; the corrupt-and-relic-**poor**
# covet the **rich**. The corrupt-and-**strong** (bigger `army`) prey on the **weak**.
# Bigger mismatch → bigger downward pull on the pair's hearts (see _want_pull, drift).
HISTORY_LEN = 300           # macro samples kept for the dev graphs (a rolling window)
# Corruption-growth model is PLUGGABLE (state["corruption_growth"]): "ambient" (the
# original — it wells up on its own, focused on a vessel) or "sown" (experiment — it
# does NOT grow on its own; a dark agent, the SOWER, must travel to a high-mismatch
# region and sow division there, and only there does corruption take root). See
# grow_corruption / _grow_corruption_sown.
DEFAULT_CORRUPTION_GROWTH = "sown"
SOW_RATE = 0.012            # corruption sown per cycle the sower stands among a target realm —
                            # the FIRST sower (the one who becomes the lord) works fast; it's
                            # his lesser retinue (AGENT_SOW_RATE, below) that's the slow one
SOW_SHADOW_FEED = 0.5       # how much a region's rising corruption swells the GLOBAL shadow

# The lord's retinue (sown model only): once embodied, he gathers a fixed-size band
# of lesser dark agents — mini-bosses the wizard can hunt down in person. Each is
# either a secondary sower (corrupting a realm the main sower isn't working) or sent
# to reinforce a war front (lending strength like a recruit). Neither task can ever
# make an agent the lord himself — only dark_champion()'s pick of the most-corrupted
# power does that. A slain agent just frees a slot; the cooldown below refills it.
LORD_RETINUE = 7              # max agents in the lord's retinue at once
AGENT_RECRUIT_CYCLES = 180    # cycles to recruit one replacement agent (~5 years)
AGENT_SOW_RATE = 0.003        # an agent sows far slower than the lord's own (first) sower —
                               # tuned so a realm with no other susceptibility cap falls
                               # (0% to FALLEN_AT) in roughly 10 years (~360 cycles) of one
                               # agent's steady sowing (several piling on the same realm
                               # still multiplies this, since there's no claim-exclusivity)
AGENT_FRONT_STRENGTH = RECRUIT_STRENGTH  # effective-strength an agent lends at a front
CORRUPT_RELIC_WANT = 0.40   # × corruption × (normalized relic-wealth gap)
CORRUPT_ARMY_WANT = 0.35    # × corruption × (normalized army gap)
WANT_RELIC_REF = 0.425      # normalizer — the mightiest single relic's boost

# The fallen god is an *agent*, not weather: it pursues DOMINION — the share of the
# world under its sway — and each cycle directs its corruption at the single vessel
# that best extends its grip, letting the rest of the world dim only ambiently. The
# more it holds, the faster it grows (a snowball toward total night). Corruption is
# sticky: it never recedes on its own — only the wizard (a later slice) can cleanse.
SHADOW_FOCUS = 1.8     # corruption-ceiling multiplier on its chosen vessel
SHADOW_AMBIENT = 0.4   # ceiling multiplier on every other power
SHADOW_FEED = 0.0008   # shadow growth gained per unit of dominion (the snowball)
DOMINION_WIN = 0.90    # dominion at/above which the world has fallen (the entity wins)

# The lieutenant gates how fast corruption spreads (see _update_lord). It crawls
# before he is embodied, runs full while he broods on his throne, and nearly stalls
# while he is off it riding the world — so keeping him off the throne is the wizard's
# lever. Multiplies the per-faction corruption approach in cycle() step 0.
LORD_PREARRIVAL_MULT = 0.5   # no lord yet (no power fallen) — corruption at half pace
LORD_ABROAD_MULT = 0.2       # lord off his throne — corruption nearly stalls

# The good god is hands-off until the shadow claims its first vessel — that is when
# the wizard is sent. At world-gen the unseen history is fast-forwarded to that
# hour: the first power falls (corruption >= FALLEN_AT) while the world still
# stands. The entity then moves on to its next vessel during play, and the fallen
# *may* drift into alliance (shared corruption bonds — but shared hunger for power
# pulls the other way, so it is only a chance). The player thus always arrives into
# a living, contested world with one dark power newly risen.
FALLEN_AT = 0.50   # corruption at/above which a power is "fallen" — the lord claims its throne
LORD_APPEAR_AT = 0.40   # the lord first manifests (rides the world, not yet enthroned)
ARRIVE_AT = 0.42        # the good god sends the wizard — just after the lord appears
AGE_CAP = 30000    # safety cap on the silent pre-history (cycles)

# A lord cast down (rout_champion) is not destroyed, only set back to the shadow
# lands — his clan is purged pure and he cannot rise again there until this many
# cycles have passed (state["lord_cooldowns"], decremented in cycle()).
LORD_RECOVER_CYCLES = 60
# Apprehending him in absentia: with his whereabouts known (Wits sense), the wizard
# can send a recruit after him instead of fighting him in person. Difficulty is a
# stat contest (the recruit's Might+Wits vs the lord's), so a well-suited agent can
# still take many cycles — APPREHEND_BASE_CYCLES is the floor at a perfect match.
APPREHEND_BASE_CYCLES = 24

# World map: a GRID x GRID lattice of tiles, each owned by one power. The grid is
# a *projection* of territory[] (the source of truth), reconciled each cycle by
# flipping border tiles — it never feeds back into the simulation. A battle's prize
# is one tile (TILE_TERR), so a bigger GRID means each battle moves less land —
# expect to rebalance as the world grows (and battles may later take >1 tile).
GRID = 50
TILES = GRID * GRID
TILE_TERR = 100.0 / TILES  # one tile's share of the world (a battle's prize)

# Wizard layer: a player avatar that walks the tile map; each step advances one
# cycle. Armies are drawn as a few unit glyphs (inferred from army[], not stored)
# massed on each power's frontline. Glyphs come in an ASCII set and a "fancy"
# set; the wizard can be an emoji where the terminal supports it (units stay
# color-coded symbols because terminal emoji can't be tinted by curses colors).
ARMY_PER_UNIT = 20  # army points represented by one drawn unit glyph
GLYPHS = {
    # unit None -> use the power initial. monster/shot/bolt are the fallen-land
    # combat sprites: a shadow-fiend, its slow projectile, and the wizard's bolt.
    "ascii": {"wizard": "@", "unit": None, "monster": "M", "shot": "o", "bolt": "*", "cbolt": "X", "lord": "L"},
    "fancy": {"wizard": "🧙", "unit": "♟", "monster": "👹", "shot": "•", "bolt": "✦", "cbolt": "✸", "lord": "💀"},
}
# A distinct human emoji per faction, used for soldiers in the tile battle view
# (fancy mode). Emoji can't be tinted by curses, so factions are told apart here
# by their avatar rather than by color; ASCII mode falls back to colored initials.
FACTION_EMOJI = [RACES[i][1] for i in range(len(REALMS))]  # rebuilt per run by assign_races

# ---------- Stats (shared by the wizard, the lord, recruits, and the crowd) ----------
# One schema for every actor. The same four drive *tasks* (a relic journey weights
# them — see relic_demand/journey_fit) and will drive arena combat later (Might =
# damage, Endurance = HP, Wits = precision, Swiftness = speed). Scale 1..10.
STATS = ["Might", "Endurance", "Wits", "Swiftness"]
STAT_ABBR = ["Mgt", "End", "Wit", "Swf"]
STAT_MAX = 10
# Each race *leans* toward one stat (higher odds to roll well there) — a bias on the
# dice, NOT a guarantee; ranges overlap, so a scrawny Cyclops or a brawny Elf happen.
# Keyed by RACES index: Elves→Wits, Men→Swiftness, Dwarves→Endurance, Cyclopes→Might.
RACE_APTITUDE = {0: 2, 1: 3, 2: 1, 3: 0}

# ---- Lineage: per-race age curves (the world's people age, die, and beget heirs) ----
# Keyed by RACES index (0 Elves, 1 Men, 2 Dwarves, 3 Cyclopes): (lifespan_lo, lifespan_hi,
# maturity) in YEARS. Elves are near-immortal — an elf notable can outlast several of the
# wizard's ages, a throughline like himself — while Men turn over many generations in the
# same span. See _advance_lineage / found_houses.
DAYS_PER_YEAR = 360
RACE_AGE = {
    0: (2400, 6000, 110),     # Elves — millennia, slow to come of age
    1: (58, 95, 16),          # Men
    2: (190, 320, 35),        # Dwarves
    3: (70, 130, 14),         # Cyclopes — short, brutish
}
HOUSE_CAP = 7                 # most living kin a house tracks at once (lines persist, don't explode)
KIDS_CAP = 4                  # most children one notable begets
BIRTH_PER_CYCLE = 0.012       # per-cycle chance an adult begets (≈ a couple over a Man's prime)
# Names: houses (surnames / dynasties) and given names, drawn per run.
HOUSE_NAMES = ["Ironhold", "Greymane", "Ravenswood", "Stormcrest", "Blackbriar", "Ashford",
               "Hollowmere", "Thornfield", "Duskbane", "Goldhart", "Frostvale", "Emberlyn",
               "Oakenshield", "Wolfsbane", "Marchwood", "Stagholt", "Brightwater", "Direstone"]
GIVEN_NAMES = ["Aldric", "Borin", "Cira", "Dain", "Elina", "Faro", "Gisla", "Hark", "Ivo",
               "Jora", "Kael", "Lys", "Maren", "Nessa", "Orin", "Petra", "Rurik", "Sela",
               "Torin", "Una", "Vael", "Wren", "Yorin", "Zara", "Bren", "Caradoc", "Edda",
               "Halvar", "Mirela", "Soren", "Thessaly", "Ulric"]

# Combat abilities are *threshold unlocks*, not linear multipliers: a stat past its
# gate grants a new verb (Endurance is the exception — it scales hearts smoothly).
# Thresholds are provisional — tune freely (the wizard's stats are set by hand in the
# character window for now, with no upper limit, so any threshold is reachable).
WIZ_MIGHT_CHARGE_AT = 7    # Might ≥ → a charged heavy bolt (channel + loose)
WIZ_WITS_SEEK_AT = 4       # Wits ≥ → see your own seekers' journey progress
WIZ_WITS_SOWER_AT = 6      # Wits ≥ → can expose the sower disguised as an ordinary NPC
WIZ_WITS_SENSE_AT = 7      # Wits ≥ → sense the shadow (the lord shows on the map)
WIZ_SWIFT_FAST_AT = 7      # Swiftness ≥ → swifter movement                     [pending]
# Per-stat ability shown in the character window: (effect label, threshold or None
# for the continuous Endurance/hearts case, wired?). Indexed like STATS.
WIZ_ABILITIES = [
    ("Charged bolt", WIZ_MIGHT_CHARGE_AT, True),
    ("Hearts", None, True),
    ("Sense shadow", WIZ_WITS_SENSE_AT, True),
    ("Swift step", WIZ_SWIFT_FAST_AT, False),
]
# Note: "Expose the sower" (WIZ_WITS_SOWER_AT) is a second Wits-gated ability but isn't
# listed in WIZ_ABILITIES above (that list drives the character window's single-threshold
# display per stat; Wits already has two tiers there for Seek/Sense — a third would need
# that UI reworked. The ability is still fully wired in dominion_app.py regardless).


def wiz_max_hearts(state):
    """The wizard's heart cap = his Endurance stat (no upper limit while testing)."""
    return max(1, state["wiz_stats"][1])

# A relic journey's find-% climbs by a fit-derived rate each cycle. Each relic has
# a *base* length scaling with its power (the easiest at a perfect fit ≈ 6 months,
# the mightiest ≈ 10 years — a long quest, not a waste); a poorer fit stretches that
# toward a ~100-year useless cap. ETA = base / fit**POW. (DAYS_PER_CYCLE=10.)
SEEK_FAST_CYCLES = 18         # easiest relic, perfect fit ≈ 6 months
SEEK_HARD_CYCLES = 365        # mightiest relic, perfect fit ≈ 10 years (legitimate)
SEEK_USELESS_CYCLES = 3650    # a wrong fit ≈ 100 years — truly useless
SEEK_FIT_POW = 2.0            # how hard fit bites on top of the relic's base length


def assign_races(state):
    """Roll which race peoples which realm this run and rebuild the run's display:
    POWERS' names (\"<Realm> <Race>\") and FACTION_EMOJI. The realm colour for each
    index is untouched, so colour pairs / biomes stay put — only who-is-what moves.
    The chosen order is stored in state[\"race\"] (race index per faction) for the
    per-race state that will hang off it later."""
    order = list(range(len(RACES)))
    random.shuffle(order)
    state["race"] = order
    for i, (realm, col) in enumerate(REALMS):
        POWERS[i] = (f"{realm} {RACES[order[i]][0]}", col)
        FACTION_EMOJI[i] = RACES[order[i]][1]


def roll_stats(race_idx, rng=random, boost=0):
    """Roll a 1..10 stat block for someone of race `race_idx`. Every stat draws from
    a middling band; the race's aptitude stat draws higher (a bias, with overlap —
    nothing guaranteed). `boost` lifts every stat (champions/the lord). `rng` lets a
    caller seed it; the default global RNG re-rolls a fresh crowd every visit."""
    apt = RACE_APTITUDE.get(race_idx)
    stats = []
    for s in range(len(STATS)):
        v = rng.randint(2, 7)
        if s == apt:
            v += rng.randint(2, 4)        # the racial lean — higher, still overlapping
        stats.append(max(1, min(STAT_MAX, v + boost)))
    return stats


def journey_fit(stats, demand):
    """How well `stats` meet a journey's `demand` (per-stat target). A weighted
    geometric mean of the capped ratios — so *every* demanded stat is tested and a
    shortfall in a heavily-demanded one is crushing (the product collapses). Returns
    0..1, where 1 means every demand is met or beaten."""
    total = sum(demand)
    if total <= 0:
        return 1.0
    fit = 1.0
    for s in range(len(STATS)):
        if demand[s] <= 0:
            continue
        ratio = min(1.0, stats[s] / demand[s])
        fit *= ratio ** (demand[s] / total)
    return fit


def relic_base_cycles(typ):
    """The inherent length of a relic's journey at a *perfect* fit, scaling with its
    power: the easiest relic ≈ SEEK_FAST_CYCLES (6 mo), the mightiest ≈ SEEK_HARD_CYCLES
    (10 yr), geometric in between. So even a perfectly-crewed expedition for a legendary
    relic is a long one — fit only ever speeds a journey up to this floor."""
    r = len(ITEM_TYPES)
    if r <= 1:
        return SEEK_FAST_CYCLES
    return SEEK_FAST_CYCLES * (SEEK_HARD_CYCLES / SEEK_FAST_CYCLES) ** (typ / (r - 1))


def seek_eta(fit, base):
    """Cycles to complete a journey: base / fit**POW, clamped to [base, useless]. A
    perfect fit (1.0) runs at the relic's base length; a poor fit stretches toward the
    ~100-year useless cap. `base` comes from relic_base_cycles(type)."""
    if fit <= 0:
        return SEEK_USELESS_CYCLES
    return max(base, min(SEEK_USELESS_CYCLES, base / (fit ** SEEK_FIT_POW)))


def seek_rate(fit, base):
    """Per-cycle find-% growth (0..100 bar) for a journey of the given `fit`/`base`."""
    return 100.0 / seek_eta(fit, base)


def relic_demands(seed):
    """A per-relic *journey* demand profile (one per ITEM_TYPE), seeded so it is
    stable for the run but varies between runs. Every stat is demanded a little, but
    rarer relics (higher index) demand much more, concentrated on one dominant stat —
    and the greatest also need a second strength, so they take a real expedition to
    crew. Returns a list of 4-int demand vectors aligned with ITEM_TYPES."""
    rng = random.Random((seed ^ 0x5EE) & 0xFFFFFFFF)
    out = []
    for r in range(len(ITEM_TYPES)):
        base = 1 + r                       # ambient demand on every stat (1..4)
        d = [base] * len(STATS)
        prim = rng.randrange(len(STATS))
        d[prim] = 5 + r                    # the dominant strength the journey needs (5..8)
        if r >= len(ITEM_TYPES) - 1:       # the single greatest relic needs two
            sec = (prim + 1 + rng.randrange(len(STATS) - 1)) % len(STATS)
            d[sec] = max(d[sec], 5)
        out.append(d)
    return out


# Towns: some macro tiles hold a settlement (a capital per faction + a few
# villages), placed once at start and fixed to the land. A town tile's micro
# scene adds buildings and townsfolk to the wilderness; the wizard can greet the
# folk to warm that faction toward him. Glyphs (fancy emoji / ascii letter):
TOWNSFOLK = ["🧑", "🧓", "👩", "👨", "🧒"]  # civilian variety (fancy); ascii uses 'o'
TOWN_GLYPH = {"keep": ("🏰", "M"), "house": ("🏠", "n")}  # buildings
TOWN_MAP_GLYPH = {True: ("♜", "C"), False: ("⌂", "n")}    # capital / village on dev map
PEDLAR_GLYPH = ("🧺", "$")  # the roaming merchant (fancy / ascii)
VILLAGES_PER_FACTION = (9, 15)  # inclusive range (a fuller, more lived-in world)
# Whimsical name fragments for procedurally named towns.
TOWN_NAME_A = ["Bel", "Mor", "Dun", "Cair", "Ash", "Wyn", "Gel", "Thal", "Bre",
               "Stor", "Fen", "Oak", "Riven", "Gray", "Black"]
TOWN_NAME_B = ["moor", "ford", "hold", "fell", "wick", "haven", "reach", "mere",
               "gate", "watch", "crest", "bury", "dale", "march"]


def status_of(h):
    # >=8 allied, 5-7 peace, 4 enemies, <=3 war
    if h >= 8:
        return ("ALLIED", "allied")
    if h >= 5:
        return ("PEACE", "peace")
    if h >= 4:
        return ("ENEMIES", "enemy")
    return ("AT WAR", "war")


def rand(a, b):
    return a + random.random() * (b - a)


# ---------- State ----------
def fresh_state(corruption_growth=None):
    raw = [15 + random.random() * 20 for _ in range(N)]
    s = sum(raw)
    state = {
        "cycle": 0,
        "day": 0.0,  # elapsed world time; the wizard moves a half-day per step
        # Each faction's standing toward the wizard (0..10, neutral 5). Only the
        # wizard's deeds move it — it never drifts. See AID_BOOST / settle_aid().
        "favor": [FAVOR_NEUTRAL] * N,
        # Transient: the side the wizard is backing this cycle ({"helped","opp"})
        # or None. Consumed and cleared inside cycle().
        "aid": None,
        # Per-engagement aid ledger, keyed by sorted power-pair -> rounds backing
        # each side; settled into favor when the engagement (battle) ends.
        "aid_log": {},
        # Towns: {(r, c): {capital, faction, name}}; set by place_towns(). NPCs the
        # wizard has greeted (so each gives favor once): a set of (r, c, npc_idx).
        # `town_msg` is a transient line shown after an interaction.
        "towns": {},
        "met": set(),
        "town_msg": None,
        # The fallen god's presence and how far it has seeped into each power.
        # The world starts good: a faint shadow and only a trace of corruption,
        # unevenly spread so the powers diverge as it builds. See cycle() step 0.
        "shadow": SHADOW_START,
        "shadow_rate": AGE_SHADOW_GROWTH,  # brisk during pre-history, slowed at arrival
        # Pluggable corruption-growth model + the sower agent it uses (see grow_corruption).
        "corruption_growth": corruption_growth or DEFAULT_CORRUPTION_GROWTH,
        "sower": None,
        # If the wizard, at peace with the lord, takes up his offer to sow corruption
        # himself: the faction he's agreed to corrupt (or None). See wiz_sow_tick.
        "wiz_sow_target": None,
        "shadow_target": 0,  # the vessel the entity is currently working on (set each cycle)
        # The shadow's embodied lieutenant: None until a power passes LORD_APPEAR_AT
        # (40%), then {fac,r,c,home,enthroned,task,target,relic,chance}. While rising
        # (clan < 50%) he has no throne yet, so he roams the marches hunting relics /
        # pressing fronts (exposed — corruption crawls); once his clan falls and he
        # sits its seat he is enthroned and broods (corruption at full rate). Managed
        # by _update_lord at the end of each cycle.
        "lord": None,
        # The lord's retinue (sown model): list of {id,fac,r,c,task,target,stats},
        # capped at LORD_RETINUE, gradually recruited/refilled. See _update_lord_agents.
        "lord_agents": [],
        "lord_recruit_cd": 0,
        "next_agent_id": 1,
        "lord_cooldowns": {},
        "corruption": [round(random.random() * 0.03, 4) for _ in range(N)],
        # Hidden trait: how readily each power succumbs (sets its corruption ceiling
        # relative to the shadow). Drives which powers fall and which stay pure.
        "suscept": [round(rand(*SUSCEPT_RANGE), 3) for _ in range(N)],
        "territory": [(r / s) * 100 for r in raw],
        "army": [30 + random.random() * 40 for _ in range(N)],
        "hearts": [[10 if i == j else 4 + round(random.random() * 3)
                    for j in range(N)] for i in range(N)],
        "items": [],  # list of {"type": idx, "owner": power idx}
        # In-progress battles: each delivers `rate`% of land per cycle from
        # loser->winner until `rem` is exhausted (a multi-cycle land transfer).
        "battles": [],  # {"winner", "loser", "rem", "rate"}
        # The wizard avatar's tile position; he walks the map and each step
        # advances a cycle. The one piece of genuinely new player state.
        "wizard": {"r": GRID // 2, "c": GRID // 2},
        # The wizard's own stat block (the shared four: Might/Endurance/Wits/Swiftness).
        # Set by hand in the character window ('c'); Endurance is his heart cap, the
        # others gate combat abilities (see WIZ_ABILITIES). Endurance 10 = 10 hearts.
        "wiz_stats": [6, 10, 6, 6],
        # Equipped staff — index into the pygame STAVES table (his attack kit). The
        # first equipment slot; gear is wizard-only (distinct from faction relics).
        "wiz_staff": 0,
        # Which staff indices the wizard has ACQUIRED (the Plain Staff, 0, always; others
        # are won from artifact quests — see place_artifacts / state["artifacts"]). He can
        # only cycle/equip staves he has found (and his Might can wield).
        "staves_found": {0},
        # Whether the wizard has learned the **charged bolt** — itself an artifact quest
        # (granted by claiming "the Charged Bolt"); until then he can only loose basic
        # bolts, even at high Might. See place_artifacts / claim_artifact.
        "can_charge": False,
        # Worn gear won from an artifact quest: the Veilcloak halves harm taken (every
        # other blow is turned aside). False until "the Veilcloak" is claimed.
        "has_cloak": False,
        # Wizard-gear quests hidden in the world: each a staff or ability sought in
        # person — heard of from a mythical creature, guarded by a mini-boss. Set by
        # place_artifacts.
        "artifacts": [],
        # Lineage: the four royal dynasties (fac → house name) and the tracked notables
        # (rulers + kin) that age, die, and beget heirs over time. Set by found_houses,
        # advanced by _advance_lineage each cycle. The town crowd is generated from these.
        "houses": {},
        "notables": [],
        # A rolling log of notable deeds (quests won/lost, deaths, successions) — so the
        # wizard can read what unfolded while he slept (see _chronicle / the journal).
        "chronicle": [],
        # A rolling window of macro samples for the dev graphs (see _record_history /
        # the pygame "graphs" map mode): each {day, cor[], terr[], army[], hearts(pairs),
        # shadow, dom}. Records every cycle, capped at HISTORY_LEN.
        "history": [],
        # Knowledge of the world: the player starts BLIND. Visiting a town charts a
        # *static snapshot* of the world (borders/corruption/readout + the day it was
        # taken) into map_snapshot; it goes stale as the world moves until re-charted.
        # map_live is a testing override (always-current map). See pygame render_map.
        "map_snapshot": None,
        "map_live": False,
        # Relics are unknown until their legend is heard from a town/village elder —
        # only then can the wizard send a seeker after one. Starts empty. See
        # town_elders() and the greet handler.
        "relics_known": set(),
        # Counter for promoting an anonymous NPC into a persistent agent: the moment
        # the wizard sends one on a journey, they get the next id and leave the crowd.
        "next_npc_id": 1,
        # Wizard upkeep: energy (spent aiding battles), food rations, gold, and a
        # roaming pedlar he can buy food from out in the world.
        "energy": ENERGY_MAX,
        "health": WIZ_HEARTS,  # the wizard's life (hearts); spent to the shadow's minions
        "food": START_FOOD,
        "gold": START_GOLD,
        "pedlar_here": random.random() < PEDLAR_CHANCE,  # a travelling merchant on this tile?
        # Recruited NPCs, now persistent characters: each is an agent
        # {id, fac, race, stats, r, c, task, relic, fit, prog, target} that roams the
        # map. task "seek" ticks find-% (prog) by fit until it lands the relic (→fac),
        # then "march"es to a war front and "fight"s there (lending RECRUIT_STRENGTH).
        # Managed by _update_recruits each cycle; created by add_recruit; capped at 7.
        "recruits": [],
        # The world's terrain is fixed land, generated once and persisted: a
        # random per-run seed plus a snapshot of each tile's biome. Terrain is
        # regenerated deterministically from (world_seed, r, c) on demand, so it
        # is stable for the whole run and stays put when ownership flips (a
        # conquered forest is still a forest). See tile_terrain().
        "world_seed": random.getrandbits(30),
    }
    # Guarantee a fallible vessel: at least one realm susceptible enough to pass the
    # arrival threshold, so the pre-history always reaches danger and terminates fast
    # (in sown mode a world of only-resistant realms could never be sown to the brink).
    if max(state["suscept"]) < ARRIVE_AT + 0.08:
        state["suscept"][max(range(N), key=lambda i: state["suscept"][i])] = round(rand(0.55, 0.9), 3)
    # Snapshot of hearts at the end of the previous cycle, so alliance
    # obligations can fire on any war/peace shift since then (drift or slider).
    state["prev_hearts"] = [row[:] for row in state["hearts"]]
    # Each relic's journey demand profile, fixed for the run (seeded from the world).
    state["relic_demand"] = relic_demands(state["world_seed"])
    assign_races(state)  # roll which race holds which coloured realm this run
    seed_grid(state)  # owner[][] grid, sized to the starting territory shares
    # Freeze each tile's biome to its starting owner (land doesn't re-theme when
    # it changes hands).
    state["biome_map"] = [row[:] for row in state["owner"]]
    place_towns(state)  # capitals + villages, fixed to the land for the run
    found_houses(state)  # the four royal dynasties + their kin (lineage evolves in cycle())
    generate_geography(state)  # the micro terrain the wizard walks (connectivity-first)
    place_artifacts(state)     # wizard-gear quests hidden in the wilds (e.g. the Fanstaff)
    # Seat the wizard on an enter-able tile near the world centre (his in-tile spawn is
    # snapped onto land by the pygame view). Falls back to the nearest town if his
    # default tile somehow has no land at all.
    wr0, wc0 = state["wizard"]["r"], state["wizard"]["c"]
    if not tile_walkable(state, wr0, wc0):
        state["wizard"]["r"], state["wizard"]["c"] = min(
            state["towns"], key=lambda t: (t[0] - wr0) ** 2 + (t[1] - wc0) ** 2,
            default=(wr0, wc0))
    age_to_danger(state)  # advance the hidden history until the wizard is sent
    return state


def place_towns(state):
    """Choose town tiles once, deterministically from the world seed: each faction
    gets a capital (near its territorial centroid) plus a few villages, all within
    its starting land. A town's faction is fixed (the founding populace) even if
    the tile is later conquered. Stored in state["towns"] keyed by (r, c)."""
    rng = random.Random((state["world_seed"] ^ 0x70776E5) & 0xFFFFFFFF)
    owner = state["biome_map"]
    towns = {}

    def named():
        return rng.choice(TOWN_NAME_A) + rng.choice(TOWN_NAME_B)

    for f in range(N):
        tiles = [(r, c) for r in range(GRID) for c in range(GRID)
                 if owner[r][c] == f]
        if not tiles:
            continue
        cr = sum(r for r, _ in tiles) / len(tiles)
        cc = sum(c for _, c in tiles) / len(tiles)
        capital = min(tiles, key=lambda t: (t[0] - cr) ** 2 + (t[1] - cc) ** 2)
        cap_name = named()
        chosen = [capital]
        # A capital is a CITY spanning a 2x2 block of its own land (the anchor tile,
        # `main`, holds the keep; the others are its districts).
        for dr, dc in ((0, 0), (0, 1), (1, 0), (1, 1)):
            t = (capital[0] + dr, capital[1] + dc)
            if 0 <= t[0] < GRID and 0 <= t[1] < GRID and owner[t[0]][t[1]] == f and t not in towns:
                towns[t] = {"capital": True, "faction": f, "name": cap_name, "main": t == capital}
                if t != capital:
                    chosen.append(t)
        # Villages: spread out (>= 2 tiles apart) within the faction's land.
        candidates = [t for t in tiles if t not in towns]
        rng.shuffle(candidates)
        want = len(chosen) + rng.randint(*VILLAGES_PER_FACTION)
        for t in candidates:
            if len(chosen) >= want:
                break
            if all((t[0] - u[0]) ** 2 + (t[1] - u[1]) ** 2 >= 4 for u in chosen):
                chosen.append(t)
                towns[t] = {"capital": False, "faction": f, "name": named(), "main": False}
    state["towns"] = towns


# ---------- World geography (the wizard's traversal terrain) ----------
# A persistent, connectivity-first terrain layer at MICRO resolution — one cell per
# tile-view cell, so the whole world is a (GRID*TILE_GS) square lattice (1000×1000).
# Rivers are thin bands that wind across the world and line up tile-to-tile; mountain
# ranges are masses; a road network links every town through bridge/pass crossings.
# The pygame tile view is a 20×20 window into this grid (collision per cell), and the
# world map shows it whole. It is the wizard's *traversal* layer ONLY — it does NOT
# feed the macro sim (territory/owner/war untouched; the curses build ignores it).
# Connectivity is guaranteed by construction (every planned road is carved through
# whatever blocks it: river→bridge, mountain→pass), so no runtime flood-fill is run.
GEO_LAND, GEO_WATER, GEO_MOUNTAIN, GEO_ROAD, GEO_BRIDGE, GEO_PASS = range(6)
GEO_PASSABLE = frozenset((GEO_LAND, GEO_ROAD, GEO_BRIDGE, GEO_PASS))
GEO_SOLID = frozenset((GEO_WATER, GEO_MOUNTAIN))
GEO_RIVERS = (8, 13)          # winding rivers across the world
GEO_RANGES = (10, 16)         # mountain ranges
GEO_EXTRA_EDGES = 8           # loop roads beyond the spanning tree


def _geo_sgn(x):
    return (x > 0) - (x < 0)


def _geo_clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def _geo_meander(rng, src, dst, toward, limit, hi):
    """A jittered cardinal path src→dst within [0,hi] (organic; for rivers/ridges)."""
    r, c = src
    path = [(r, c)]
    for _ in range(limit):
        if abs(r - dst[0]) + abs(c - dst[1]) <= 1:
            break
        if rng.random() < toward:
            dr, dc = dst[0] - r, dst[1] - c
            if rng.random() < abs(dr) / max(1, abs(dr) + abs(dc)):
                r += _geo_sgn(dr)
            else:
                c += _geo_sgn(dc)
        else:
            dr, dc = rng.choice(((1, 0), (-1, 0), (0, 1), (0, -1)))
            r, c = r + dr, c + dc
        r, c = _geo_clamp(r, 0, hi), _geo_clamp(c, 0, hi)
        path.append((r, c))
    path.append((dst[0], dst[1]))
    return path


def _geo_route_path(rng, src, dst):
    """A clean road src→dst: march the dominant axis with only a gentle ~15% jog."""
    r, c = src
    path = [(r, c)]
    while (r, c) != (dst[0], dst[1]):
        dr, dc = dst[0] - r, dst[1] - c
        if dr == 0:
            c += _geo_sgn(dc)
        elif dc == 0:
            r += _geo_sgn(dr)
        else:
            along_r = abs(dr) >= abs(dc)
            if rng.random() < 0.15:
                along_r = not along_r
            r, c = (r + _geo_sgn(dr), c) if along_r else (r, c + _geo_sgn(dc))
        path.append((r, c))
    return path


def _geo_plan_edges(rng, nodes):
    """Connectivity intent: an MST (Prim) over town nodes + a few short loop edges."""
    n = len(nodes)

    def dist(a, b):
        return (nodes[a][0] - nodes[b][0]) ** 2 + (nodes[a][1] - nodes[b][1]) ** 2

    in_tree, edges = {0}, []
    while len(in_tree) < n:
        best = None
        for a in in_tree:
            for b in range(n):
                if b in in_tree:
                    continue
                dd = dist(a, b)
                if best is None or dd < best[0]:
                    best = (dd, a, b)
        edges.append((best[1], best[2]))
        in_tree.add(best[2])
    have = {frozenset(e) for e in edges}
    cand = sorted((dist(a, b), a, b) for a in range(n) for b in range(a + 1, n)
                  if frozenset((a, b)) not in have)
    for _d, a, b in cand[:GEO_EXTRA_EDGES]:
        edges.append((a, b))
    return edges


def _geo_flood(state, start):
    """Flood passable micro cells from `start` (y,x). For headless connectivity tests
    only — not run at game time (construction already guarantees town connectivity)."""
    geo, MW = state["geo"], state["geo_w"]
    seen = bytearray(MW * MW)
    si = start[0] * MW + start[1]
    seen[si] = 1
    stack = [si]
    while stack:
        i = stack.pop()
        r, c = divmod(i, MW)
        for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nr, nc = r + dr, c + dc
            if 0 <= nr < MW and 0 <= nc < MW:
                j = nr * MW + nc
                if not seen[j] and geo[j] in GEO_PASSABLE:
                    seen[j] = 1
                    stack.append(j)
    return seen


def generate_geography(state):
    """Build the wizard's micro traversal terrain (see section header), connectivity-
    first: plan a road graph over the towns → scatter rivers and mountain ranges →
    realize each road by carving through whatever blocks it (river→bridge,
    mountain→pass). Stores state["geo"] (a bytearray of GEO_* cell codes, row-major,
    GEO_W × GEO_W) and state["geo_w"]."""
    T = TILE_GS
    MW = GRID * T
    state["geo_w"] = MW
    geo = bytearray(MW * MW)              # all GEO_LAND (0)
    state["geo"] = geo
    towns = state["towns"]
    nodes = [(r * T + T // 2, c * T + T // 2)
             for (r, c), t in towns.items() if t.get("main") or not t.get("capital")]
    if len(nodes) < 2:
        return
    rng = random.Random(state["world_seed"] & 0xFFFFFFFF)

    # Protect each town's tile (+ a 2-cell ring) so a river/range never buries a town.
    protected = bytearray(MW * MW)
    for (tr, tc) in towns:
        for y in range(max(0, tr * T - 2), min(MW, tr * T + T + 2)):
            base = y * MW
            for x in range(max(0, tc * T - 2), min(MW, tc * T + T + 2)):
                protected[base + x] = 1

    def put(y, x, code):
        if 0 <= y < MW and 0 <= x < MW:
            i = y * MW + x
            if not protected[i]:
                geo[i] = code

    def edge_pt():
        side = rng.choice("NSEW")
        if side == "N":
            return (0, rng.randint(0, MW - 1))
        if side == "S":
            return (MW - 1, rng.randint(0, MW - 1))
        if side == "E":
            return (rng.randint(0, MW - 1), MW - 1)
        return (rng.randint(0, MW - 1), 0)

    edges = _geo_plan_edges(rng, nodes)

    # rivers — thin (~2-wide) bands winding edge to edge
    for _ in range(rng.randint(*GEO_RIVERS)):
        for (y, x) in _geo_meander(rng, edge_pt(), edge_pt(), 0.62, MW * 3, MW - 1):
            put(y, x, GEO_WATER)
            put(y, x + 1, GEO_WATER)

    # mountain ranges — ridge lines thickened into masses
    for _ in range(rng.randint(*GEO_RANGES)):
        y0, x0 = rng.randint(0, MW - 1), rng.randint(0, MW - 1)
        dst = (_geo_clamp(y0 + rng.randint(-MW // 3, MW // 3), 0, MW - 1),
               _geo_clamp(x0 + rng.randint(-MW // 3, MW // 3), 0, MW - 1))
        for (ry, rx) in _geo_meander(rng, (y0, x0), dst, 0.72, MW // 2, MW - 1):
            w = rng.choice((2, 3, 3, 4))
            for dy in range(-w, w + 1):
                for dx in range(-w, w + 1):
                    ny, nx = ry + dy, rx + dx
                    if 0 <= ny < MW and 0 <= nx < MW and geo[ny * MW + nx] == GEO_LAND:
                        put(ny, nx, GEO_MOUNTAIN)

    # realize roads — carve a guaranteed corridor through whatever blocks each route
    for a, b in edges:
        for (y, x) in _geo_route_path(rng, nodes[a], nodes[b]):
            i = y * MW + x
            cur = geo[i]
            if cur == GEO_WATER:
                geo[i] = GEO_BRIDGE
            elif cur == GEO_MOUNTAIN:
                geo[i] = GEO_PASS
            else:
                geo[i] = GEO_ROAD


def geo_cell(state, x, y):
    """Micro geo cell at (x=col, y=row); GEO_LAND outside the world / when ungenerated."""
    MW = state.get("geo_w")
    if MW is None or not (0 <= x < MW and 0 <= y < MW):
        return GEO_LAND
    return state["geo"][y * MW + x]


def tile_walkable(state, r, c):
    """Is macro tile (r,c) enter-able — does its 20×20 window hold any passable cell?
    True off the geo layer (sim / legacy curses unaffected)."""
    if not (0 <= r < GRID and 0 <= c < GRID):
        return False
    geo = state.get("geo")
    if geo is None:
        return True
    T, MW = TILE_GS, state["geo_w"]
    for dy in range(T):
        base = (r * T + dy) * MW + c * T
        for dx in range(T):
            if geo[base + dx] in GEO_PASSABLE:
                return True
    return False


# ---------- Wizard-gear artifact quests ----------
# Some of the wizard's gear (staves, later trinkets) is not bought or handed out but
# **sought in person**: a mythical creature dwelling in the wilds tells of it (giving
# the quest), the relic's resting place then shows on the world map, and a mini-boss
# guards it amid thickened monsters. The wizard must go himself — this is the avatar's
# own adventure, distinct from the faction relics recruits chase. Each entry:
#   {staff, name, creature, creature_name, loc, told, found}
# `staff` indexes the pygame STAVES table; `creature`/`loc` are macro tiles. Stored in
# state["artifacts"]; placement is deterministic per world_seed.
# Each spec grants either a staff (`grants:"staff"` + `staff` index) or an ability
# (`grants:"charge"` — the wound-up heavy bolt). Every artifact is told of by its OWN
# mythical creature, so each is a separate hunt.
ARTIFACT_SPECS = [
    {"grants": "staff", "staff": 1, "name": "Fanstaff",
     "creature_name": "the Sphinx of the Wastes",
     "hint": "a three-fanged staff that looses a spray of bolts"},
    {"grants": "charge", "name": "the Charged Bolt",
     "creature_name": "the Drake of the Embers",
     "hint": "the art of winding up a heavy, piercing bolt — hold to charge, release to loose"},
    {"grants": "cloak", "name": "the Veilcloak",
     "creature_name": "the Weaver of the Hollow",
     "hint": "a cloak woven of dusk — it turns aside half of all harm that finds you"},
]


def place_artifacts(state):
    """Hide each wizard-gear artifact in the wilds: pick a lair tile for its mythical
    creature and a resting tile for the gear itself — both walkable, well clear of
    towns, and a good way apart so the quest is a real journey. Deterministic per run."""
    rng = random.Random((state["world_seed"] ^ 0xA27FAC7) & 0xFFFFFFFF)
    towns = set(state["towns"])

    def far_wild_tile(avoid):
        # a walkable tile away from towns and from `avoid` tiles
        best = None
        for _ in range(400):
            r, c = rng.randint(2, GRID - 3), rng.randint(2, GRID - 3)
            if (r, c) in towns or not tile_walkable(state, r, c):
                continue
            dt = min((abs(r - tr) + abs(c - tc) for tr, tc in towns), default=GRID)
            da = min((abs(r - ar) + abs(c - ac) for ar, ac in avoid), default=GRID)
            score = min(dt, da)
            if best is None or score > best[0]:
                best = (score, (r, c))
        return best[1] if best else (GRID // 2, GRID // 2)

    arts, used = [], []
    for spec in ARTIFACT_SPECS:
        creature = far_wild_tile(used); used.append(creature)
        loc = far_wild_tile(used); used.append(loc)
        arts.append({"grants": spec["grants"], "staff": spec.get("staff"),
                     "name": spec["name"], "creature": creature,
                     "creature_name": spec["creature_name"], "hint": spec["hint"],
                     "loc": loc, "known": False, "told": False, "found": False})
    state["artifacts"] = arts


def artifact_creature_at(state, r, c):
    """An un-told artifact whose mythical creature dwells on tile (r,c), or None."""
    for a in state.get("artifacts", []):
        if not a["told"] and tuple(a["creature"]) == (r, c):
            return a
    return None


def artifact_loc_at(state, r, c):
    """A told-but-unfound artifact whose gear rests on tile (r,c), or None."""
    for a in state.get("artifacts", []):
        if a["told"] and not a["found"] and tuple(a["loc"]) == (r, c):
            return a
    return None


def _near_artifact_loc(state, r, c, rad=4):
    """Closest un-found artifact resting-tile within `rad` (Chebyshev), and the
    distance — for thickening monsters as the wizard nears it. (None, None) if none."""
    best = None
    for a in state.get("artifacts", []):
        if a["found"]:
            continue
        ar, ac = a["loc"]
        dd = max(abs(r - ar), abs(c - ac))
        if dd <= rad and (best is None or dd < best[1]):
            best = (a, dd)
    return best if best else (None, None)


# ---------- Lineage: houses, rulers, and the bloodlines that turn over with time ----------
# A small roster of *notable* people (the four royal houses + their kin) is tracked and
# evolves continuously as time passes (every cycle, in all paths — play, age-skips, slumber):
# they age on their race's curve, die, and beget heirs; when a ruler dies a prince inherits.
# The anonymous town crowd is NOT tracked — it is generated lazily from the living houses
# when the wizard visits (roll_townsfolk), so skipping a century and returning, the people
# you meet are the *descendants* of those you knew. Kings are a face/lineage-anchor only for
# now (they do not move territory/army). The clock keys off cycle×DAYS_PER_CYCLE, which
# advances every cycle even during the day-stale skip loops.

def _now(state):
    return state["cycle"] * DAYS_PER_CYCLE


def _roll_lifespan(race, rng):
    lo, hi, _mat = RACE_AGE[race]
    return rng.uniform(lo, hi) * DAYS_PER_YEAR


def inherit_stats(parent_stats, race, rng):
    """A child's stats: the parent's, pulled partway toward the race's natural roll, with
    noise — so a line carries its strengths but can throw up a lesser or a greater scion
    (Bilbo → Frodo). No deliberate decay yet (the fading arc comes later)."""
    fresh = roll_stats(race, rng)
    out = []
    for s in range(len(STATS)):
        v = round(0.6 * parent_stats[s] + 0.4 * fresh[s] + rng.uniform(-1.5, 1.5))
        out.append(max(1, min(STAT_MAX, v)))
    return out


def _new_notable(state, race, fac, house, rng, role="kin", parent=None,
                 born=None, stats=None, boost=0):
    nid = state["next_npc_id"]
    state["next_npc_id"] += 1
    lo, _hi, mat = RACE_AGE[race]
    if born is None:                       # spawn as an adult somewhere in their prime
        born = _now(state) - rng.uniform(mat, lo * 0.55) * DAYS_PER_YEAR
    if stats is None:
        stats = roll_stats(race, rng, boost)
    return {"id": nid, "race": race, "fac": fac, "house": house,
            "given": rng.choice(GIVEN_NAMES), "stats": stats, "born": born,
            "lifespan": _roll_lifespan(race, rng), "role": role, "parent": parent}


def _child_of(state, p, rng):
    nid = state["next_npc_id"]
    state["next_npc_id"] += 1
    return {"id": nid, "race": p["race"], "fac": p["fac"], "house": p["house"],
            "given": rng.choice(GIVEN_NAMES), "stats": inherit_stats(p["stats"], p["race"], rng),
            "born": _now(state), "lifespan": _roll_lifespan(p["race"], rng),
            "role": "kin", "parent": p["id"]}


def found_houses(state):
    """Found the four royal houses: each faction gets a named dynasty with a living ruler
    and an heir or two. Called from fresh_state (before the pre-history fast-forward, so the
    lines already age and turn over a little by the time the wizard arrives)."""
    rng = random.Random((state["world_seed"] ^ 0x40115E) & 0xFFFFFFFF)
    names = HOUSE_NAMES[:]
    rng.shuffle(names)
    houses, notables = {}, []
    for fac in range(N):
        race = state["race"][fac]
        house = names[fac % len(names)]
        houses[fac] = house
        ruler = _new_notable(state, race, fac, house, rng, role="ruler", boost=1)
        notables.append(ruler)
        for _ in range(rng.randint(1, 2)):
            notables.append(_child_of(state, ruler, rng))
    state["houses"] = houses
    state["notables"] = notables


def _succeed(state, dead_ruler, rng):
    """A ruler has died — the eldest living adult of the house takes the throne; if the
    house has no grown heir, a new scion steps forward (the line endures)."""
    fac, house = dead_ruler["fac"], dead_ruler["house"]
    _lo, _hi, mat = RACE_AGE[dead_ruler["race"]]
    now = _now(state)
    heirs = [p for p in state["notables"]
             if p["house"] == house and now - p["born"] >= mat * DAYS_PER_YEAR]
    if heirs:
        heir = max(heirs, key=lambda p: now - p["born"])   # the eldest
        heir["role"] = "ruler"
    else:
        heir = _new_notable(state, dead_ruler["race"], fac, house, rng, role="ruler", boost=1)
        state["notables"].append(heir)
    _chronicle(state, f"House {house}: {dead_ruler['given']} has died — {heir['given']} "
               f"takes the throne of {POWERS[fac][0]}.")


def _advance_lineage(state, rng=random):
    """Age the notables one cycle; resolve deaths (by lifespan), births (capped per line),
    and succession. Runs every cycle, so a century skipped equals a century of turnover."""
    if not state.get("notables"):
        return
    now = _now(state)
    living, dead = [], []
    for p in state["notables"]:
        if now - p["born"] >= p["lifespan"]:
            dead.append(p)
        else:
            living.append(p)
    state["notables"] = living
    for p in dead:
        if p["role"] == "ruler":
            _succeed(state, p, rng)
    # births: adults in their prime, under the per-line and per-parent caps
    born = []
    for p in state["notables"]:
        race = p["race"]
        _lo, _hi, mat = RACE_AGE[race]
        age = now - p["born"]
        if age < mat * DAYS_PER_YEAR or age > p["lifespan"] * 0.85:
            continue
        house_n = sum(1 for x in state["notables"] if x["house"] == p["house"])
        kids = sum(1 for x in state["notables"] if x.get("parent") == p["id"])
        if house_n < HOUSE_CAP and kids < KIDS_CAP and rng.random() < BIRTH_PER_CYCLE:
            born.append(_child_of(state, p, rng))
    state["notables"].extend(born)


def ruler_of(state, fac):
    """The living ruler of faction `fac`'s house, or None."""
    return next((p for p in state.get("notables", [])
                 if p["fac"] == fac and p["role"] == "ruler"), None)


def house_of(state, fac):
    return state.get("houses", {}).get(fac, "")


def person_age(state, p):
    return int((_now(state) - p["born"]) / DAYS_PER_YEAR)


def item_boost(state, i):
    return sum(ITEM_TYPES[it["type"]]["boost"]
               for it in state["items"] if it["owner"] == i)


# ---------- World map (projection of territory[]) ----------
def _neighbors(r, c):
    if r > 0:
        yield r - 1, c
    if r < GRID - 1:
        yield r + 1, c
    if c > 0:
        yield r, c - 1
    if c < GRID - 1:
        yield r, c + 1


def tile_targets(state):
    """How many tiles each power *should* own, summing to exactly TILES."""
    raw = [state["territory"][i] / 100 * TILES for i in range(N)]
    targets = [int(round(x)) for x in raw]
    # Fix rounding drift against the larger powers so the sum is exactly TILES.
    order = sorted(range(N), key=lambda i: state["territory"][i], reverse=True)
    d = TILES - sum(targets)
    k = 0
    while d != 0:
        i = order[k % N]
        if d > 0:
            targets[i] += 1
            d -= 1
        elif targets[i] > 1:
            targets[i] -= 1
            d += 1
        k += 1
    return targets


def _counts(owner):
    counts = [0] * N
    for row in owner:
        for o in row:
            counts[o] += 1
    return counts


def seed_grid(state):
    """Lay out N contiguous blobs sized to territory[] via multi-source BFS."""
    targets = tile_targets(state)
    owner = [[-1] * GRID for _ in range(GRID)]
    q = GRID // 4
    seeds = [(q, q), (q, GRID - 1 - q), (GRID - 1 - q, q), (GRID - 1 - q, GRID - 1 - q)]
    counts = [0] * N
    frontiers = [[] for _ in range(N)]
    for i in range(N):
        r, c = seeds[i]
        owner[r][c] = i
        counts[i] = 1
        for nr, nc in _neighbors(r, c):
            if owner[nr][nc] == -1:
                frontiers[i].append((nr, nc))

    # Grow all fronts one tile at a time, round-robin, until each hits its quota.
    changed = True
    while changed:
        changed = False
        for i in range(N):
            if counts[i] >= targets[i]:
                continue
            cell = None
            while frontiers[i]:
                r, c = frontiers[i].pop(random.randrange(len(frontiers[i])))
                if owner[r][c] == -1:
                    cell = (r, c)
                    break
            if cell is None:
                continue
            r, c = cell
            owner[r][c] = i
            counts[i] += 1
            changed = True
            for nr, nc in _neighbors(r, c):
                if owner[nr][nc] == -1:
                    frontiers[i].append((nr, nc))

    # Mop up any tile left unclaimed (a boxed-in front) to an adjacent owner.
    changed = True
    while changed:
        changed = False
        for r in range(GRID):
            for c in range(GRID):
                if owner[r][c] != -1:
                    continue
                for nr, nc in _neighbors(r, c):
                    if owner[nr][nc] != -1:
                        owner[r][c] = owner[nr][nc]
                        changed = True
                        break
    state["owner"] = owner


def _flip_frontier(owner, counts, loser, winner, n):
    """Flip up to n tiles owned by `loser` and bordering `winner` -> `winner`."""
    if n <= 0:
        return
    border = [(r, c) for r in range(GRID) for c in range(GRID)
              if owner[r][c] == loser
              and any(owner[nr][nc] == winner for nr, nc in _neighbors(r, c))]
    random.shuffle(border)
    for r, c in border[:n]:
        owner[r][c] = winner
        counts[loser] -= 1
        counts[winner] += 1


def reconcile_grid(state, wars):
    """Repaint the grid to match tile_targets(), advancing winners' borders."""
    owner = state["owner"]
    counts = _counts(owner)
    targets = tile_targets(state)

    # 1) Honest moves: each war's winner pushes its border into the loser, but
    #    never below the loser's own target (its territory floor = ~6 tiles), so
    #    no power can be drained to zero — a tile-less power has no border and
    #    could never recover. The cleanup pass then tops winners up to target.
    for winner, loser, grab in wars:
        want = int(round(grab / 100 * TILES))
        room = max(0, counts[loser] - targets[loser])
        _flip_frontier(owner, counts, loser, winner, min(want, room))

    # 2) Cleanup: settle residual drift (rounding/renormalization) so counts
    #    match targets exactly. Route one tile at a time from a surplus power to
    #    a deficit power along the shortest chain of bordering powers between
    #    them — passing a tile across each edge on the path. This drains surplus
    #    toward deficit regardless of how the blobs are arranged (the old
    #    "pull from largest neighbour" heuristic could strand surplus in a tiny
    #    power that no deficit power happened to border).
    for _ in range(TILES * 4):
        deficit = [i for i in range(N) if counts[i] < targets[i]]
        if not deficit:
            break
        # Power-adjacency graph from the current grid.
        adj = {i: set() for i in range(N)}
        for r in range(GRID):
            for c in range(GRID):
                p = owner[r][c]
                for nr, nc in _neighbors(r, c):
                    if owner[nr][nc] != p:
                        adj[p].add(owner[nr][nc])
        # BFS from a deficit power to the nearest surplus power.
        u = deficit[0]
        prev = {u: None}
        queue = [u]
        surplus = None
        while queue:
            x = queue.pop(0)
            if counts[x] > targets[x]:
                surplus = x
                break
            for y in adj[x]:
                if y not in prev:
                    prev[y] = x
                    queue.append(y)
        if surplus is None:
            break  # no surplus reachable (should not happen on a connected map)
        # Walk the path surplus -> ... -> u, passing one tile across each edge.
        path = []
        x = surplus
        while x is not None:
            path.append(x)
            x = prev[x]
        for k in range(len(path) - 1):
            _flip_frontier(owner, counts, path[k], path[k + 1], 1)


# ---------- Simulation ----------
def effective_strength(state, i, at_war_with):
    s = state["army"][i] * (0.5 + state["territory"][i] / 100) * rand(0.7, 1.3)
    for k in range(N):
        if k == i or k == at_war_with:
            continue
        if state["hearts"][i][k] >= 8:
            s += state["army"][k] * 0.25
    s *= (1 + item_boost(state, i))
    s += _recruit_strength(state, i)   # the wizard's recruits fighting at this faction's front
    s += _agent_strength(state, i)     # the lord's own retinue agents at this faction's front
    if state.get("aid") and state["aid"]["helped"] == i:  # the wizard's backing
        s *= (1 + AID_BOOST)
    return s


def dominion(state):
    """The share of the world (0..1) under the Shadow's sway: each power's land
    weighted by how corrupted it is. The fallen god's goal is to drive this to 1.0."""
    return sum(state["corruption"][i] * state["territory"][i] for i in range(N)) / 100.0


def dark_champion(state):
    """The shadow's embodied lieutenant — a Sauron-figure rising in the most-corrupted
    power. He manifests once that power passes LORD_APPEAR_AT (40%) and rides the
    world; he only *claims its throne* (enthroned) once it is truly fallen (≥
    FALLEN_AT, 50%). Returns his faction index, or None when no power is corrupt
    enough to embody him. A clan just cast down (rout_champion) is on cooldown
    (state["lord_cooldowns"]) and is skipped — he is recovering in the shadow lands,
    not gone for good. The diffuse Morgoth-shadow itself is never embodied; only
    this lieutenant can be struck down."""
    cds = state.get("lord_cooldowns", {})
    cand = [k for k in range(N) if not cds.get(k)]
    if not cand:
        return None
    i = max(cand, key=lambda k: state["corruption"][k])
    return i if state["corruption"][i] >= LORD_APPEAR_AT else None


def lord_survives_roll(lord):
    """Like any other character, whether the lord survives being struck down is an
    Endurance dice roll (the same (a)/(b)-on-Endurance rule as a fighting companion's
    ally_down): a tough lord is only cast back to the shadow lands to recover; a frail
    one is destroyed outright."""
    return random.randint(1, 10) <= lord["stats"][1]


def rout_champion(state, i, survives=True):
    """Strike the lieutenant down. His clan routs and its region is purged (corruption
    cleansed to pure, so its war-ambition collapses to a fair share and its fallen
    tiles clear of minions), the shadow's presence takes a blow, the corruption-driven
    wars subside (hearts pulled back to peace), and battles on that clan's fronts break
    off. If `survives` (an Endurance dice roll — see lord_survives_roll) he is only cast
    back to the shadow lands and cannot rise again in that clan for LORD_RECOVER_CYCLES;
    otherwise he is destroyed outright and that clan can never raise a lord again
    (its susceptibility to the shadow is spent)."""
    state["corruption"][i] = 0.0  # the region is purged — cleansed pure
    state["shadow"] = max(0.0, state["shadow"] * 0.5)
    for j in range(N):
        if j != i and state["hearts"][i][j] < 5:
            state["hearts"][i][j] = state["hearts"][j][i] = 5
    state["battles"] = [b for b in state["battles"] if i not in (b["winner"], b["loser"])]
    state["prev_hearts"] = [row[:] for row in state["hearts"]]  # don't re-trigger war propagation
    if survives:
        state.setdefault("lord_cooldowns", {})[i] = LORD_RECOVER_CYCLES
    else:
        state["suscept"][i] = 0.0   # destroyed outright — this clan is spent, can never fall again
    if state.get("lord") and state["lord"]["fac"] == i:
        state["lord"] = None  # the lieutenant is cast down
        state["lord_agents"] = []


def _seat_of(state, fac):
    """A faction's throne tile — its founding capital (fixed to the land)."""
    for (r, c), t in state["towns"].items():
        if t["capital"] and t["faction"] == fac:
            return (r, c)
    return (GRID // 2, GRID // 2)


def _front_tile(state, fac):
    """A tile of `fac` that borders an enemy it is at war with, and that enemy — a
    front the lord might ride to. (None, None) if the faction has no war on its
    borders."""
    owner = state["owner"]
    for r in range(GRID):
        for c in range(GRID):
            if owner[r][c] != fac:
                continue
            for nr, nc in _neighbors(r, c):
                e = owner[nr][nc]
                if e != fac and status_of(state["hearts"][fac][e])[1] == "war":
                    return (r, c), e
    return None, None


def _weighted_unclaimed_relic(state):
    """A relic no power yet holds, chosen at random weighted by rarity — so the
    mightier the relic the longer the odds the lord rides home with it (the same
    rarity weights the world uses to spawn relics). Skips relics he couldn't seize
    when the world is already full (one too weak to eclipse the weakest out there).
    None if nothing is worth/possible to hunt."""
    owned = {it["type"] for it in state["items"]}
    full = len(state["items"]) >= MAX_ITEMS
    weakest = min((ITEM_TYPES[it["type"]]["boost"] for it in state["items"]), default=0.0)
    avail = [t for t in range(len(ITEM_TYPES))
             if t not in owned and (not full or ITEM_TYPES[t]["boost"] > weakest)]
    if not avail:
        return None
    total = sum(ITEM_TYPES[t]["weight"] for t in avail)
    r = random.random() * total
    for t in avail:
        r -= ITEM_TYPES[t]["weight"]
        if r <= 0:
            return t
    return avail[-1]


def _marches_tile(state, fac):
    """The tile of `fac` farthest from its throne — the wild marches the lord rides
    to in search of a relic (relics are aspatial, so this just sends him out into
    the world, exposed, while he searches)."""
    owner = state["owner"]
    sr, sc = _seat_of(state, fac)
    best, bestd = (sr, sc), -1
    for r in range(GRID):
        for c in range(GRID):
            if owner[r][c] == fac:
                dd = (r - sr) ** 2 + (c - sc) ** 2
                if dd > bestd:
                    best, bestd = (r, c), dd
    return best


def _grant_relic(state, typ, owner):
    """Hand a found relic to `owner`; if the world is already full of relics, a
    greater find eclipses the weakest (same rule as the wizard's seeker)."""
    found = {"type": typ, "owner": owner}
    if len(state["items"]) < MAX_ITEMS:
        state["items"].append(found)
        return
    weakest = min(state["items"], key=lambda it: ITEM_TYPES[it["type"]]["boost"])
    if ITEM_TYPES[typ]["boost"] > ITEM_TYPES[weakest["type"]]["boost"]:
        state["items"].remove(weakest)
        state["items"].append(found)


def _step_toward(pos, target):
    """One cardinal step from pos toward target (prefer the longer axis)."""
    r, c = pos
    tr, tc = target
    if abs(tr - r) >= abs(tc - c) and tr != r:
        return (r + (1 if tr > r else -1), c)
    if tc != c:
        return (r, c + (1 if tc > c else -1))
    return pos


def _update_lord(state):
    """Manage the shadow's lieutenant (run at the end of cycle()).

    He manifests once a power passes LORD_APPEAR_AT (40%) — dark_champion — rising
    from the wild marches of that clan. While his clan is not yet *fallen* (< 50%)
    he is **rising**: he rides for its capital to claim the throne (and the wizard,
    arriving at 42%, has this window to cut him down before he digs in). Once the
    clan falls (≥ 50%) and he sits the seat he is **enthroned** — and behaves as
    before: brood, or (pressured) ride out to hunt a relic / defend a front.
    `enthroned` (clan fallen *and* on the throne) gates corruption to full rate."""
    champ = dark_champion(state)
    if champ is None:
        state["lord"] = None
        return
    seat = _seat_of(state, champ)
    lord = state.get("lord")
    if lord is None or lord["fac"] != champ:
        start = _marches_tile(state, champ)      # he rises out in the clan's marches
        # The lord is one of the stat actors too: a champion of his clan's race,
        # boosted (he is a boss). His own stats gate his relic hunt (and his boss
        # fight, later) — so the wizard-vs-lord relic race is a contest of fit.
        state["lord"] = {"fac": champ, "r": start[0], "c": start[1], "home": False,
                         "enthroned": False, "ever_enthroned": False, "task": "rising",
                         "target": None, "relic": None, "prog": 0.0, "fit": 0.0,
                         "stats": roll_stats(state["race"][champ], boost=2)}
        return
    fallen = state["corruption"][champ] >= FALLEN_AT
    pressured = state["territory"][champ] < 100.0 / N    # losing land
    enemy = None
    # A *secure ruler* (fallen and holding at least his fair share) sits the throne
    # — the corruption surge. In every other state he is out in the field with an
    # end to pursue: while **rising** (not yet fallen) he rides to amass power so he
    # can claim the seat, and once **fallen but pressured** he rides to claw land
    # back. Either way he hunts a relic (the force multiplier) or, failing that,
    # presses a war front — and stays exposed the whole time. Only a settled,
    # unthreatened realm draws him home to brood.
    # The FIRST time his clan falls he claims the throne outright — even if
    # pressured — so a beleaguered realm still gets its lord declared at 50%;
    # only after that initial claiming can being pressured pull him back out.
    if fallen and (not pressured or not lord.get("ever_enthroned")):
        lord["relic"], lord["prog"] = None, 0.0
        target = seat
    else:
        # Drop a relic goal that a rival has claimed out from under him.
        if lord.get("relic") is not None and any(it["type"] == lord["relic"] for it in state["items"]):
            lord["relic"], lord["prog"] = None, 0.0
        if lord.get("relic") is None:
            lord["relic"], lord["prog"] = _weighted_unclaimed_relic(state), 0.0
        if lord.get("relic") is not None:                # hunting a relic — fit gates the pace
            lord["fit"] = journey_fit(lord["stats"], state["relic_demand"][lord["relic"]])
            lord["prog"] += seek_rate(lord["fit"], relic_base_cycles(lord["relic"])) * rand(0.85, 1.15)
            if lord["prog"] >= 100.0:                     # the journey pays off — he seizes it
                _grant_relic(state, lord["relic"], champ)
                lord["relic"], lord["prog"] = None, 0.0
            target = _marches_tile(state, champ)         # roam the marches as he hunts
        else:                                            # no relic to hunt — press a front
            front, enemy = _front_tile(state, champ)
            target = front or _marches_tile(state, champ)
    if (lord["r"], lord["c"]) != target:
        lord["r"], lord["c"] = _step_toward((lord["r"], lord["c"]), target)
    at_seat = (lord["r"], lord["c"]) == seat
    lord["home"] = at_seat
    lord["enthroned"] = fallen and at_seat
    if lord["enthroned"] and not lord["ever_enthroned"]:
        house = house_of(state, champ)
        seat_nm = state["towns"].get(seat, {}).get("name")
        where = f"{seat_nm}, the seat of " if seat_nm else ""
        _chronicle(state, f"The throne of {where}House {house} now belongs to the lord.")
    if lord["enthroned"]:
        lord["ever_enthroned"] = True
    # Name what he is up to, for the map readout.
    if lord["enthroned"]:
        lord["task"], lord["target"] = "brood", None
    elif lord.get("relic") is not None:
        lord["task"], lord["target"] = "seek", lord["relic"]   # hunting a relic
    elif enemy is not None:
        lord["task"], lord["target"] = "march", enemy          # riding to a war front
    elif not fallen:
        lord["task"], lord["target"] = "rising", None          # roaming, amassing power
    else:
        lord["task"], lord["target"] = "return", None          # heading home to the throne


def _make_lord_agent(state, fac):
    """Spawn one fresh retinue agent, rising from the clan's marches like the lord
    himself but lesser (boost=1 vs his boost=2) — a mini-boss, never a lieutenant."""
    nid = state["next_agent_id"]
    state["next_agent_id"] += 1
    start = _marches_tile(state, fac)
    return {"id": nid, "fac": fac, "r": start[0], "c": start[1],
            "task": "sow", "target": None, "stats": roll_stats(state["race"][fac], boost=1)}


def lord_agent_at(state, r, c):
    """The lord's retinue agent standing at (r,c), if any — for the front-end to
    materialise as a catchable mini-boss."""
    for a in state.get("lord_agents", []):
        if (a["r"], a["c"]) == (r, c):
            return a
    return None


SOWER_RECOVER_CYCLES = 30   # a struck-down sower retreats to recover before sowing resumes


def sower_at(state, r, c):
    """The sower standing at (r,c), if any and not already recovering — for the
    front-end to materialise as a catchable mini-boss."""
    sow = state.get("sower")
    if sow is None or sow.get("cd", 0) > 0:
        return None
    return sow if (sow["r"], sow["c"]) == (r, c) else None


def slay_sower(state, agent_id=None):
    """Strike down the sower (called by a front-end mini-boss fight). He retreats to
    the shadow lands and stops sowing for SOWER_RECOVER_CYCLES, then resumes (picking a
    fresh target) — a temporary check on the dark economy, not a permanent win."""
    sow = state.get("sower")
    if sow is not None:
        sow["cd"] = SOWER_RECOVER_CYCLES
        sow["target"] = None


def slay_lord_agent(state, agent_id):
    """Remove a felled retinue agent (called by a front-end mini-boss fight). The
    cap-driven recruiting in _update_lord_agents naturally queues a replacement —
    no agent ever inherits the throne; only dark_champion()'s corruption pick does."""
    before = state.get("lord_agents", [])
    state["lord_agents"] = [a for a in before if a["id"] != agent_id]
    if len(state["lord_agents"]) < len(before):  # a kill always costs a fresh cooldown
        state["lord_recruit_cd"] = max(state.get("lord_recruit_cd", 0), AGENT_RECRUIT_CYCLES)


def _agent_strength(state, i):
    """Effective-strength lent to faction i by its retinue agents currently at a
    front (mirrors _recruit_strength's role for the wizard's recruits)."""
    lord = state.get("lord")
    if lord is None or lord["fac"] != i:
        return 0.0
    return AGENT_FRONT_STRENGTH * sum(1 for a in state.get("lord_agents", []) if a["task"] == "front")


def _update_lord_agents(state):
    """Manage the lord's retinue (sown model only — see grow_corruption). Once he is
    embodied, agents are recruited one at a time up to LORD_RETINUE (AGENT_RECRUIT_CYCLES
    apart — ~5 years; a replacement is slow, unlike the lord's own swift rise). Each cycle
    every agent either presses a war front (if the clan is pressured and one exists) or
    sows corruption in a realm the main sower isn't already working — so the dark economy
    can work several realms at once. Several agents CAN still pile onto the same realm (no
    claim-exclusivity yet), so AGENT_SOW_RATE is cut hard (1/10th of an earlier pass) to
    keep that from snowballing corruption too fast — revisit if it still needs taming. A
    slain agent (via slay_lord_agent) just frees a slot; this loop alone refills it, on its
    own cooldown.

    (The capped, stat-gap-aware shared-task mechanic — assign up to ~30% of a retinue to
    one task for a fit-scored, non-additive boost — belongs to the WIZARD's recruits, not
    this retinue; see RECRUIT_GROUP_CAP / party logic in _update_recruits.)"""
    if state.get("corruption_growth") != "sown":
        state["lord_agents"], state["lord_recruit_cd"] = [], 0
        return
    lord = state.get("lord")
    if lord is None:
        state["lord_agents"], state["lord_recruit_cd"] = [], 0
        return
    agents = state.setdefault("lord_agents", [])
    if agents and agents[0]["fac"] != lord["fac"]:   # the clan changed — retinue disbands
        agents.clear()
    cd = state.get("lord_recruit_cd", 0)
    if len(agents) < LORD_RETINUE and cd <= 0:
        agents.append(_make_lord_agent(state, lord["fac"]))
        state["lord_recruit_cd"] = AGENT_RECRUIT_CYCLES
    elif cd > 0:
        state["lord_recruit_cd"] = cd - 1

    main_target = state.get("sower", {}).get("target") if state.get("sower") else None
    front, enemy = _front_tile(state, lord["fac"])
    pressured = state["territory"][lord["fac"]] < 100.0 / N
    send_front = pressured and front is not None
    owner, cor, sus = state["owner"], state["corruption"], state["suscept"]
    for i, ag in enumerate(agents):
        if send_front and i % 2 == 0:
            ag["task"], ag["target"] = "front", enemy
            if (ag["r"], ag["c"]) != front:
                ag["r"], ag["c"] = _step_toward((ag["r"], ag["c"]), front)
            continue
        ag["task"] = "sow"
        t = ag.get("target")
        if t is None or t == main_target or cor[t] >= min(FALLEN_AT, sus[t]) - 1e-6:
            cand = [k for k in range(N) if k != main_target and cor[k] < min(FALLEN_AT, sus[k]) - 1e-6]
            t = ag["target"] = max(cand, key=lambda k: region_mismatch(state, k)) if cand else None
        if t is None:
            continue
        if owner[ag["r"]][ag["c"]] == t:
            ceiling = min(FALLEN_AT, sus[t])
            before = cor[t]
            cor[t] = min(ceiling, cor[t] + AGENT_SOW_RATE * (0.3 + region_mismatch(state, t)))
            state["shadow"] = min(1.0, state["shadow"] + (cor[t] - before) * SOW_SHADOW_FEED)
        else:
            dest, best = (ag["r"], ag["c"]), 1 << 30
            for r in range(GRID):
                row = owner[r]
                for c in range(GRID):
                    if row[c] == t:
                        dd = (r - ag["r"]) ** 2 + (c - ag["c"]) ** 2
                        if dd < best:
                            best, dest = dd, (r, c)
            ag["r"], ag["c"] = _step_toward((ag["r"], ag["c"]), dest)


def _lord_corruption_mult(state):
    """How fast corruption spreads this cycle, gated by the lieutenant's state: half
    before he is embodied, a crawl while he is loose-but-not-enthroned (rising, or
    ridden off his throne), and full only while he broods enthroned on his seat."""
    lord = state.get("lord")
    if lord is None:
        return LORD_PREARRIVAL_MULT
    return 1.0 if lord.get("enthroned") else LORD_ABROAD_MULT


def recruit_name(q):
    """A recruit's display name: 'Given of House X' (or just 'Given') + #id."""
    base = f"{q.get('given', 'A seeker')}"
    if q.get("house"):
        base += f" of House {q['house']}"
    return f"{base} (#{q['id']})"


_recruit_name = recruit_name   # back-compat alias for internal call sites


def _chronicle(state, msg):
    """Record a notable deed: show it now (town_msg) AND keep it in the rolling chronicle,
    so events that happen while the wizard sleeps aren't lost — he reads them on waking."""
    state["town_msg"] = msg
    log = state.setdefault("chronicle", [])
    log.append((round(state["cycle"] * DAYS_PER_CYCLE / DAYS_PER_YEAR), msg))   # (year, msg)
    if len(log) > 40:
        del log[:-40]


def add_recruit(state, stats, fac, relic_type, r, c, given=None, house=None, born=None):
    """Promote an NPC into a persistent recruit. If `relic_type` is given they set out at
    once to seek it for their home faction `fac`; if None they are recruited idle — they
    simply join the wizard's following at (r,c) and wait for orders (see handle_talk's
    [2] seek a relic / [1] march). Carries lineage (name/house/birth) so they age, can die
    mid-quest of old age, and leave heirs. Returns the recruit (also appended)."""
    nid = state["next_npc_id"]
    state["next_npc_id"] += 1
    race = state["race"][fac]
    _lo, _hi, mat = RACE_AGE[race]
    if born is None:
        born = _now(state) - random.uniform(mat, mat + 20) * DAYS_PER_YEAR
    task = "seek" if relic_type is not None else "idle"
    fit = journey_fit(stats, state["relic_demand"][relic_type]) if relic_type is not None else 0.0
    rec = {"id": nid, "fac": fac, "race": race, "stats": list(stats),
           "r": r, "c": c, "task": task, "relic": relic_type,
           "fit": fit,
           "prog": 0.0, "target": None, "on_path": True, "age": 0,
           "led": False, "downed": False,             # age = movement-parity counter (not years)
           "given": given or random.choice(GIVEN_NAMES), "house": house or "",
           "born": born, "lifespan": _roll_lifespan(race, random),
           "party": [], "party_of": None}   # see join_quest — this recruit's shared-quest party
    state["recruits"].append(rec)
    return rec


def led_seeker(state):
    """The seeker the wizard is currently escorting in person, or None."""
    return next((q for q in state.get("recruits", []) if q.get("led")), None)


def quest_party(state, q):
    """The full party working q's quest (q's leader plus its followers, in either
    direction) — q itself if it has no party. Used to compute group fit/stats and to
    keep followers shadowing their leader's position."""
    leader = (next((r for r in state["recruits"] if r["id"] == q["party_of"]), None)
              if q.get("party_of") is not None else q)
    if leader is None:
        return [q]
    followers = [r for r in state["recruits"] if r["id"] in leader.get("party", [])]
    return [leader] + followers


def quest_group_fit(state, q):
    """The shared quest's group fit: the elementwise MAX stat across q's whole party
    (leader + followers) scored against the relic's demand via journey_fit. A party
    member who duplicates a stat the party already covers barely raises this; one who
    fills a stat the party lacks raises it more — so piling on doesn't simply add up."""
    party = quest_party(state, q)
    group_stats = [0] * len(STATS)
    for r in party:
        for si, v in enumerate(r["stats"]):
            if v > group_stats[si]:
                group_stats[si] = v
    return journey_fit(group_stats, state["relic_demand"][q["relic"]])


def join_quest(state, follower_id, leader_id):
    """Put recruit `follower_id` onto recruit `leader_id`'s shared seek-quest, forming
    (or joining) a party — up to RECRUIT_GROUP_CAP members total. The follower stops
    independently seeking/progressing; only the leader's prog advances (at the party's
    group fit, see quest_group_fit), and the follower shadows the leader's position.
    Returns True on success (same faction, leader is seeking, party not full, not
    already partied), False otherwise."""
    recruits = state["recruits"]
    leader = next((r for r in recruits if r["id"] == leader_id), None)
    follower = next((r for r in recruits if r["id"] == follower_id), None)
    if leader is None or follower is None or leader is follower:
        return False
    if leader["task"] != "seek" or leader.get("party_of") is not None:
        return False
    if follower.get("party_of") is not None or follower.get("party"):
        return False
    if follower["fac"] != leader["fac"]:
        return False
    if 1 + len(leader.get("party", [])) >= RECRUIT_GROUP_CAP:
        return False
    leader.setdefault("party", []).append(follower["id"])
    follower["party_of"] = leader["id"]
    follower["task"], follower["prog"] = "seek", 0.0
    return True


def leave_quest(state, recruit_id):
    """Pull a recruit out of whatever shared quest party they're in — a follower
    resumes seeking independently (their own fit, fresh progress); if a leader leaves,
    their followers are freed to seek independently too (the party disbands)."""
    recruits = state["recruits"]
    r = next((x for x in recruits if x["id"] == recruit_id), None)
    if r is None:
        return
    if r.get("party_of") is not None:
        leader = next((x for x in recruits if x["id"] == r["party_of"]), None)
        if leader is not None:
            leader["party"] = [i for i in leader.get("party", []) if i != recruit_id]
        r["party_of"] = None
        r["prog"] = 0.0
    for fid in r.get("party", []):
        f = next((x for x in recruits if x["id"] == fid), None)
        if f is not None:
            f["party_of"] = None
            f["prog"] = 0.0
    r["party"] = []


def _recruit_strength(state, i):
    """Effective-strength a faction's fighting recruits lend it at the front."""
    return RECRUIT_STRENGTH * sum(1 for q in state.get("recruits", [])
                                  if q["task"] == "fight" and q["fac"] == i)


def start_apprehend_quest(state, q, agent_id=None):
    """Send a recruit to hunt down the dark lord himself, or — pass agent_id — one of
    his retinue agents, instead of marching to the front or chasing a relic. Only
    sensible once the target's whereabouts are known (the wizard's Wits sense). Reuses
    journey_fit, treating the target's own stats as the 'demand': a recruit strong
    where the target is strong closes in faster. On success the target is cast back to
    the shadow lands (rout_champion for the lord; slay_lord_agent for an agent) without
    a fight. q["target"] is kept as a live [r,c] (refreshed each cycle in
    _update_recruits) so the map/route-line code can treat it like any other quest."""
    if agent_id is not None:
        target = next((a for a in state.get("lord_agents", []) if a["id"] == agent_id), None)
        if target is None:
            return False
        q["apprehend_kind"], q["apprehend_id"] = "agent", agent_id
    else:
        target = state.get("lord")
        if target is None:
            return False
        q["apprehend_kind"], q["apprehend_id"] = "lord", None
    q["task"] = "apprehend"
    q["target"] = [target["r"], target["c"]]
    q["fit"] = journey_fit(q["stats"], target["stats"])
    q["prog"] = 0.0
    q["led"] = False
    return True


def _recruit_to_march(q):
    """Send a recruit off to find and hold a war front (drops any seek in progress)."""
    q["task"], q["relic"], q["prog"], q["target"] = "march", None, 0.0, None


def _roam_step(state, q):
    """Wander the world one step — a seeker has no fixed destination (relics are
    aspatial), so this just gives him a moving, catchable position, like the lord."""
    tgt = q.get("target")
    if not tgt or (q["r"], q["c"]) == tuple(tgt):
        q["target"] = [random.randrange(GRID), random.randrange(GRID)]
    q["r"], q["c"] = _step_toward((q["r"], q["c"]), q["target"])


def _recruit_found_relic(state, q):
    """A seeker's journey pays off: hand the relic to their faction (eclipsing the
    weakest if the world is full), announce it, and send them on to the front."""
    T, R = q["relic"], q["fac"]
    nm = _recruit_name(q)
    found = {"type": T, "owner": R}
    if len(state["items"]) < MAX_ITEMS:
        state["items"].append(found)
        _chronicle(state, f"{nm} found the {ITEM_TYPES[T]['name']} for {POWERS[R][0]}!")
    else:
        weakest = min(state["items"], key=lambda it: ITEM_TYPES[it["type"]]["boost"])
        if ITEM_TYPES[T]["boost"] > ITEM_TYPES[weakest["type"]]["boost"]:
            state["items"].remove(weakest); state["items"].append(found)
            _chronicle(state, f"{nm} found the {ITEM_TYPES[T]['name']} for "
                       f"{POWERS[R][0]}, eclipsing the {ITEM_TYPES[weakest['type']]['name']}.")
        else:
            _chronicle(state, f"{nm} returns empty-handed — only lesser relics remain.")
    _recruit_to_march(q)


def quest_difficulty(state, q):
    """How perilous a seeker's road is (0..1) — the SINGLE driver, tightly linked to the
    quest itself: the relic's inherent hardness (its base journey length) blended with
    the seeker's lack of suitability (poor fit = a longer, more exposed road). This sets
    both how slowly progress ticks AND the monster odds per tile (see recruit_peril and
    the pygame lead-a-seeker journey). Marchers/others have a modest fixed difficulty."""
    if q.get("task") != "seek" or q.get("relic") is None:
        return 0.3
    span = max(1, SEEK_HARD_CYCLES - SEEK_FAST_CYCLES)
    base = min(1.0, max(0.0, (relic_base_cycles(q["relic"]) - SEEK_FAST_CYCLES) / span))
    miss = 1.0 - q.get("fit", 1.0)                          # lack of suitability
    return max(0.0, min(1.0, 0.45 * base + 0.55 * miss + 0.25 * base * miss))


def recruit_peril(state, q):
    """A traveling recruit's danger (0..1) — dominated by the quest's difficulty, with the
    land's corruption and the shadow's strength as a lesser ambient add. Doubles as the
    chance the wizard finds them mid-battle on a tile, and (for a led seeker) the per-tile
    monster odds of the journey."""
    cor = state["corruption"][state["owner"][q["r"]][q["c"]]]
    return max(0.0, min(1.0, 0.05 + quest_difficulty(state, q) * 0.7
                        + cor * 0.18 + state["shadow"] * 0.10))


def _recruit_encounter(state, q):
    """Resolve a road-encounter for a traveling recruit. Survival is Might + Endurance
    against a monster threat that grows with the shadow. Returns 'slain'/'wounded'/'ok'."""
    D = q["stats"][0] + q["stats"][1] + rand(0, 6)          # Might + Endurance + luck
    M = rand(3, 9) + state["shadow"] * 10 + recruit_peril(state, q) * 6
    beast = random.choice(RECRUIT_BEASTS)
    if M > D + 7:
        _chronicle(state, f"{_recruit_name(q)} was slain by {beast}.")
        return "slain"
    if M > D:
        q["prog"] = max(0.0, q["prog"] - rand(8, 20))
        _chronicle(state, f"{_recruit_name(q)} was waylaid by {beast} — wounded, set back.")
        return "wounded"
    return "ok"


def _update_recruits(state):
    """Advance every recruit one cycle (from cycle()): seekers roam and tick their
    find-% (a rival grabbing the relic first ends the seek and sends them to fight);
    on success the relic goes to their faction and they march to a war front; marchers
    travel to the nearest front and dig in; fighters hold it (lending strength via
    _recruit_strength). Travelers risk monster encounters (Might+Endurance vs the road).
    Recruits persist — only the wizard reassigns them (or a beast slays them)."""
    slain = []
    now = _now(state)
    for q in state["recruits"]:
        # Old age claims a recruit mid-life (a Man may not outlast a decades-long quest) —
        # but their line lives on: an heir joins the world's notables (meet them later).
        if now - q.get("born", now) >= q.get("lifespan", 1e18):
            heir = _child_of(state, q, random)
            if not heir["house"]:
                heir["house"] = random.choice(HOUSE_NAMES)
            state["notables"].append(heir)
            _chronicle(state, f"{_recruit_name(q)} has died of old age; "
                       f"{heir['given']} of House {heir['house']} carries the line on.")
            slain.append(q["id"])
            continue
        # Recruits travel the world at half pace — a step only every other cycle — so
        # the wizard can run them down on the map and meet them in person.
        q["age"] = q.get("age", 0) + 1
        move = q["age"] % 2 == 0
        led = q.get("led")          # the wizard is escorting this seeker in person
        if q.get("party_of") is not None:
            # a follower on a shared quest: no independent task/peril — they just
            # shadow their leader (whose own prog already counts the whole party).
            leader = next((r for r in state["recruits"] if r["id"] == q["party_of"]), None)
            if leader is None or leader["task"] != "seek" or leader["id"] in slain:
                q["party_of"] = None   # the party's quest ended — resume independently
            else:
                if move:
                    q["r"], q["c"] = leader["r"], leader["c"]
                q["on_path"] = leader.get("on_path", True)
                continue
        if q["task"] == "seek":
            T = q["relic"]
            holder = next((it["owner"] for it in state["items"]
                           if it["type"] == T and it["owner"] != q["fac"]), None)
            if holder is not None:                       # a rival seized it first — go fight
                _chronicle(state, f"{_recruit_name(q)} lost the {ITEM_TYPES[T]['name']} to "
                           f"{POWERS[holder][0]} — marches to the front.")
                _recruit_to_march(q)
                q["led"] = False
                for fid in q.get("party", []):
                    f = next((r for r in state["recruits"] if r["id"] == fid), None)
                    if f is not None:
                        f["party_of"] = None
                q["party"] = []
            else:
                fit = quest_group_fit(state, q) if q.get("party") else q["fit"]
                q["prog"] += seek_rate(fit, relic_base_cycles(T)) * rand(0.85, 1.15)
                if q["prog"] >= 100.0:
                    _recruit_found_relic(state, q)
                    q["led"] = False                     # quest done — they part to the front
                    for fid in q.get("party", []):
                        f = next((r for r in state["recruits"] if r["id"] == fid), None)
                        if f is not None:
                            f["party_of"] = None
                    q["party"] = []
                elif move and not led:                   # while led, the wizard sets position
                    _roam_step(state, q)
        elif q["task"] == "march":
            front, _enemy = _front_tile(state, q["fac"])
            q["target"] = list(front) if front else list(_seat_of(state, q["fac"]))
            if move:
                q["r"], q["c"] = _step_toward((q["r"], q["c"]), q["target"])
            if front is not None and (q["r"], q["c"]) == front:
                q["task"] = "fight"
        elif q["task"] == "fight":
            if _front_tile(state, q["fac"])[0] is None:  # the front quieted — find another
                q["task"] = "march"
        elif q["task"] == "apprehend":
            is_agent = q.get("apprehend_kind") == "agent"
            target = (next((a for a in state.get("lord_agents", []) if a["id"] == q.get("apprehend_id")), None)
                      if is_agent else state.get("lord"))
            if target is None:
                # the quarry slipped away (felled by someone else, or his clan changed) —
                # the chase ends; they fall back to whatever they'd otherwise be doing.
                who = "one of the dark lord's retinue" if is_agent else "the dark lord"
                _chronicle(state, f"{_recruit_name(q)}'s hunt for {who} comes "
                           f"up empty — he is no longer to be found.")
                _recruit_to_march(q)
            else:
                q["target"] = [target["r"], target["c"]]
                q["prog"] += seek_rate(q["fit"], APPREHEND_BASE_CYCLES) * rand(0.85, 1.15)
                if move:
                    q["r"], q["c"] = _step_toward((q["r"], q["c"]), (target["r"], target["c"]))
                if q["prog"] >= 100.0:
                    if is_agent:
                        _chronicle(state, f"{_recruit_name(q)} has run down one of "
                                   f"{POWERS[target['fac']][0]}'s dark retinue!")
                        slay_lord_agent(state, target["id"])
                    else:
                        survives = lord_survives_roll(target)
                        tail = ("and cast him back to the shadow lands!" if survives
                                else "and struck him down for good!")
                        _chronicle(state, f"{_recruit_name(q)} has apprehended "
                                   f"{POWERS[target['fac']][0]}'s dark lord {tail}")
                        rout_champion(state, target["fac"], survives=survives)
                    _recruit_to_march(q)
        # Monsters on the road threaten travelers (not those dug in at a front). A LED
        # seeker faces this danger as real combat at the wizard's side instead (handled
        # in the pygame tile view), so the abstract roll is suppressed while led.
        if (not led and q["task"] in ("seek", "march")
                and random.random() < recruit_peril(state, q) * RECRUIT_ENCOUNTER):
            if _recruit_encounter(state, q) == "slain":
                slain.append(q["id"])
        # Are they keeping to their route this cycle? A well-suited (high-fit) seeker
        # holds the path and can be found on the map; a poor fit strays (pursued, lost),
        # vanishing from the map. A led seeker is always with you, so always findable.
        path_fit = quest_group_fit(state, q) if q.get("party") else q["fit"]
        q["on_path"] = True if (led or q["task"] != "seek") else (random.random() < path_fit)
    if slain:
        state["recruits"] = [q for q in state["recruits"] if q["id"] not in slain]
        for q in state["recruits"]:                 # drop slain party-mates everywhere
            if q.get("party"):
                q["party"] = [fid for fid in q["party"] if fid not in slain]
            if q.get("party_of") in slain:
                q["party_of"] = None


def desired_share(state, i):
    """How much of the world a power *wants*. The uncorrupted want only their fair
    share (an even split — they fight only to reclaim what they've lost, never to
    expand); past the fallen line ambition grows, reaching for the whole world at
    full corruption. This caps how much land a power takes in a war."""
    eq = 100.0 / N
    c = state["corruption"][i]
    if c <= FALLEN_AT:
        return eq
    return eq + (c - FALLEN_AT) / (1.0 - FALLEN_AT) * (100.0 - eq)


def _heart_floor(state, i, j):
    """The lowest hearts a pair may drift to. The pure are **blocked from the war
    zone**: they bottom out at 4 (ENEMIES) and never descend into war on their own.
    Corruption unlocks it — if a power in the pair is fallen, the floor drops to 0,
    so hearts *can* slide to ≤ 3, and war then starts automatically (the usual path)."""
    if state["corruption"][i] > FALLEN_AT or state["corruption"][j] > FALLEN_AT:
        return 0
    return 4


def _record_history(state):
    """Snapshot the macro quantities into the rolling history window (for the dev graphs)."""
    cor, terr, army = state["corruption"], state["territory"], state["army"]
    ij = [(i, j) for i in range(N) for j in range(i + 1, N)]
    pairs = [round(state["hearts"][i][j], 1) for (i, j) in ij]
    # The covet-pressure on each pair, split so the dev can see army-mismatch vs relics:
    apull, rpull = [], []
    for (i, j) in ij:
        apull.append(round((cor[i] * max(0, army[i] - army[j])
                            + cor[j] * max(0, army[j] - army[i])) / 100.0 * CORRUPT_ARMY_WANT, 3))
        rpull.append(round((cor[i] * max(0.0, item_boost(state, j) - item_boost(state, i))
                            + cor[j] * max(0.0, item_boost(state, i) - item_boost(state, j)))
                           / WANT_RELIC_REF * CORRUPT_RELIC_WANT, 3))
    dom = sum(cor[k] * terr[k] for k in range(N)) / 100.0
    h = state.setdefault("history", [])
    h.append({"day": state["cycle"] * DAYS_PER_CYCLE,
              "cor": [round(c, 3) for c in cor], "terr": [round(t, 2) for t in terr],
              "army": [round(a, 1) for a in army], "hearts": pairs,
              "apull": apull, "rpull": rpull,
              "shadow": round(state["shadow"], 3), "dom": round(dom, 3)})
    if len(h) > HISTORY_LEN:
        del h[:len(h) - HISTORY_LEN]


def _want_pull(state, i, j):
    """Layer-1 covetousness: the downward heart pull on pair (i,j) from corruption-
    amplified mismatches. The corrupt-and-relic-poor covet the relic-rich; the corrupt-
    and-strong prey on the weak. Each side's pull is scaled by ITS OWN corruption, so a
    pure faction never covets — only the fallen act on want. From existing state only."""
    cor, army = state["corruption"], state["army"]
    wi, wj = item_boost(state, i), item_boost(state, j)
    relic = (cor[i] * max(0.0, wj - wi) + cor[j] * max(0.0, wi - wj)) / WANT_RELIC_REF
    host = (cor[i] * max(0, army[i] - army[j]) + cor[j] * max(0, army[j] - army[i])) / 100.0
    return relic * CORRUPT_RELIC_WANT + host * CORRUPT_ARMY_WANT


def want_motive(state, i):
    """Flavour: the rival faction `i` most covets/preys on right now and why, or None.
    Only the corrupting act on want (the pull is scaled by their own corruption)."""
    cor, army = state["corruption"], state["army"]
    best = None
    for j in range(N):
        if j == i:
            continue
        relic = cor[i] * max(0.0, item_boost(state, j) - item_boost(state, i)) / WANT_RELIC_REF
        host = cor[i] * max(0, army[i] - army[j]) / 100.0
        rs, hs = relic * CORRUPT_RELIC_WANT, host * CORRUPT_ARMY_WANT
        score = rs + hs
        if score > 0.05 and (best is None or score > best[0]):
            best = (score, j, "covets the relics of" if rs >= hs else "would prey on the weakness of")
    return (best[1], best[2]) if best else None


def region_mismatch(state, i):
    """Raw tension (corruption-INDEPENDENT) between faction i and its most-mismatched
    rival — normalized army gap + relic gap. This is what a sower seeks out to exploit
    (it must be corruption-free, or there'd be nothing to seed growth from at zero)."""
    army = state["army"]
    best = 0.0
    for j in range(N):
        if j == i:
            continue
        a = abs(army[i] - army[j]) / 100.0
        r = abs(item_boost(state, i) - item_boost(state, j)) / WANT_RELIC_REF
        best = max(best, a + r)
    return best


def _grow_corruption_ambient(state):
    """The original model: corruption wells up on its own, focused on the vessel power."""
    cor = state["corruption"]
    state["shadow"] = min(1.0, state["shadow"]
                          + rand(0, state["shadow_rate"]) + dominion(state) * SHADOW_FEED)
    prio = [state["territory"][i] * state["suscept"][i] * (1 - cor[i]) for i in range(N)]
    focus = max(range(N), key=lambda i: prio[i])
    state["shadow_target"] = focus
    lmult = _lord_corruption_mult(state)   # gated by the lieutenant's whereabouts
    for i in range(N):
        mult = SHADOW_FOCUS if i == focus else SHADOW_AMBIENT
        ceiling = min(1.0, state["shadow"] * state["suscept"][i] * mult)
        if ceiling > cor[i]:
            cor[i] += (ceiling - cor[i]) * CORRUPT_REVERT * lmult
        cor[i] = max(0.0, min(1.0, cor[i] + rand(-0.3, 0.3) * CORRUPT_NOISE))


def _sowable(state, i):
    """Realm i still has corruption headroom AND hasn't fallen yet — once it passes
    FALLEN_AT the lord's own lifecycle takes over there (he rises and claims its
    throne), so a sower (the main one, an agent, or the wizard) moves on rather than
    lingering on a realm that's already fallen."""
    return state["corruption"][i] < min(FALLEN_AT, state["suscept"][i]) - 1e-6


def wiz_sow_tick(state):
    """If the wizard, at peace with the lord, has taken up his offer to sow corruption
    himself — exactly the sower's own errand, sent to the same target — he advances it
    while he stands among that realm's people, same rate and feed as the sower."""
    t = state.get("wiz_sow_target")
    if t is None:
        return
    if not _sowable(state, t):
        state["wiz_sow_target"] = None             # sown out — his errand is done
        return
    wr, wc = state["wizard"]["r"], state["wizard"]["c"]
    if state["owner"][wr][wc] == t and (wr, wc) in state["towns"]:
        cor = state["corruption"]
        ceiling = min(FALLEN_AT, state["suscept"][t])
        before = cor[t]
        cor[t] = min(ceiling, cor[t] + SOW_RATE * (0.3 + region_mismatch(state, t)))
        state["shadow"] = min(1.0, state["shadow"] + (cor[t] - before) * SOW_SHADOW_FEED)


def _grow_corruption_sown(state):
    """Experiment: corruption does NOT well up on its own. A SOWER (a dark agent — the
    lord's precursor) travels to the realm with the largest raw mismatch and sows
    division there; only where he sows does corruption take root (sticky, capped by the
    realm's susceptibility). The shadow's presence is just how much has been sown. Once a
    realm passes FALLEN_AT the usual lieutenant rises there (existing lord lifecycle)."""
    owner, cor = state["owner"], state["corruption"]
    sow = state.get("sower")
    if sow is None:
        sow = state["sower"] = {"r": GRID // 2, "c": GRID // 2, "target": None, "cd": 0}
    if sow.get("cd", 0) > 0:           # struck down in person — recovering in the shadow lands
        sow["cd"] -= 1
        return
    # COMMIT to a target until it's sown out — re-picking every cycle made the sower
    # chase a jittering mismatch and never arrive to actually sow (the fresh_state tail).
    t = sow.get("target")
    if t is None or not _sowable(state, t):
        cand = [i for i in range(N) if _sowable(state, i)] or list(range(N))
        t = max(cand, key=lambda i: region_mismatch(state, i))
        sow["target"] = t
    state["shadow_target"] = t
    sr, sc = sow["r"], sow["c"]
    if owner[sr][sc] == t:                          # sowing while among the target's people
        ceiling = min(FALLEN_AT, state["suscept"][t])
        before = cor[t]
        cor[t] = min(ceiling, cor[t] + SOW_RATE * (0.3 + region_mismatch(state, t)))
        # the region's rising corruption swells the shadow's GLOBAL presence (sticky).
        state["shadow"] = min(1.0, state["shadow"] + (cor[t] - before) * SOW_SHADOW_FEED)
    else:                                          # travel toward land the target ACTUALLY holds —
        # a town or city of theirs if one stands on it (sowing among a crowd, not the
        # empty wilds), preferring a capital; otherwise any tile they currently hold.
        dest, best, best_cap = (sr, sc), 1 << 30, 1 << 30
        cap_dest = None
        for (r, c), town in state["towns"].items():
            if owner[r][c] != t:                    # the town's seat may have been conquered
                continue
            dd = (r - sr) ** 2 + (c - sc) ** 2
            if town.get("capital"):
                if dd < best_cap:
                    best_cap, cap_dest = dd, (r, c)
            elif dd < best:
                best, dest = dd, (r, c)
        if cap_dest is not None:
            dest = cap_dest
        elif best == 1 << 30:                       # no town/city of theirs stands at all
            for r in range(GRID):
                row = owner[r]
                for c in range(GRID):
                    if row[c] == t:
                        dd = (r - sr) ** 2 + (c - sc) ** 2
                        if dd < best:
                            best, dest = dd, (r, c)
        sow["r"], sow["c"] = _step_toward((sr, sc), dest)


def grow_corruption(state):
    """Dispatch the (pluggable) corruption-growth model for this cycle."""
    if state.get("corruption_growth") == "sown":
        _grow_corruption_sown(state)
    else:
        _grow_corruption_ambient(state)


def cycle(state, reconcile=True):
    # `reconcile=False` skips the territory→map projection (step 6) — used only by the
    # blind multi-century fast-forward (slumber_years), which repaints once at the end.
    # The owner grid then lags territory for a stretch, fine while nobody is looking.
    state["cycle"] += 1

    prev = state["prev_hearts"]  # hearts as of the end of the last cycle

    # 0) The shadow's corruption grows — via the PLUGGABLE model (see grow_corruption):
    #    "ambient" wells up on its own focused on a vessel; "sown" only grows where the
    #    sower travels to exploit a mismatch. Corruption is sticky either way.
    cor = state["corruption"]
    grow_corruption(state)

    # 1) Relationships drift (slow random walk), kept symmetric — but the shadow
    #    tilts it: the corrupt vie for power (belligerent), a corruption *gap*
    #    sours relations, and shared corruption bonds.
    for i in range(N):
        for j in range(i + 1, N):
            avg = (cor[i] + cor[j]) / 2
            gap = abs(cor[i] - cor[j])
            bond = max(0.0, min(cor[i], cor[j]) - gap)  # high only if both high & close
            skew = avg * CORRUPT_AGGR + gap * CORRUPT_SPLIT - bond * CORRUPT_BOND
            skew += _want_pull(state, i, j)             # corrupt covet relics / prey on the weak
            p_down = max(0.05, min(0.95, 0.5 + skew))   # >0.5 leans toward war
            direction = -1 if random.random() < p_down else 1
            drift = direction * (1 if random.random() < 0.35 else 0)
            # The war zone (≤3) is walled off unless corruption has unlocked it.
            h = max(_heart_floor(state, i, j), min(10, state["hearts"][i][j] + drift))
            state["hearts"][i][j] = h
            state["hearts"][j][i] = h

    # 1b) Alliance obligations. When a power's stance toward a third power C
    #     crosses into war (or climbs back to peace) since last cycle, every
    #     ally is pulled with it — transitively across the alliance bloc, so
    #     allies fight the same wars and make peace together. War leads peace
    #     when both fire in one cycle (a fresh war overrides a fresh truce).
    def _is_ally(a, b):
        return state["hearts"][a][b] >= 8

    def _set_pair(a, c, v):
        state["hearts"][a][c] = v
        state["hearts"][c][a] = v

    war_pinned = set()
    for c in range(N):
        # Bloc-spread war: seed from anyone who just entered war with C.
        front = [i for i in range(N)
                 if i != c and prev[i][c] > 3 and state["hearts"][i][c] <= 3]
        seen = set(front)
        while front:
            i = front.pop()
            for a in range(N):
                if a in (i, c) or a in seen or not _is_ally(a, i):
                    continue
                target = max(3, _heart_floor(state, a, c))  # to war, or only to enmity if pure
                if state["hearts"][a][c] > target:
                    _set_pair(a, c, target)
                war_pinned.add((a, c))
                seen.add(a)
                front.append(a)
    for c in range(N):
        # Bloc-spread peace: seed from anyone who just climbed out of war with
        # C (so the release fires on a single +1 drift step, not a jump to 5).
        front = [i for i in range(N)
                 if i != c and prev[i][c] <= 3 and state["hearts"][i][c] > 3]
        seen = set(front)
        while front:
            i = front.pop()
            for a in range(N):
                if a in (i, c) or a in seen or not _is_ally(a, i):
                    continue
                if (a, c) in war_pinned:
                    continue                  # a fresh war this cycle wins
                if state["hearts"][a][c] <= 3:
                    _set_pair(a, c, 5)        # pulled up out of war, to PEACE
                seen.add(a)
                front.append(a)

    # 2) Wars (hearts <= 3): relics are looted instantly and stalemates bleed,
    #    but a decisive clash no longer flips land at once — it OPENS a battle
    #    over a SINGLE border tile. The winner is decided up front; that one
    #    tile then changes hands gradually over 1-20 cycles (step 2b). The army
    #    gap sets the *speed*, not the prize: a rout takes the tile in ~1 cycle,
    #    an even match grinds for ~20 — with +/-40% luck on top. A sustained war
    #    just opens a fresh one-tile battle once the previous tile has fallen.
    active = {frozenset((b["winner"], b["loser"])) for b in state["battles"]}
    for i in range(N):
        for j in range(i + 1, N):
            if status_of(state["hearts"][i][j])[1] != "war":  # hearts can only reach
                continue                                       # here once corruption unlocked the floor
            si = effective_strength(state, i, j)
            sj = effective_strength(state, j, i)
            total = si + sj
            if total <= 0:
                continue
            winner, loser = (i, j) if si >= sj else (j, i)
            gap = abs(si - sj) / total

            # Spoils: the stronger side loots the loser's relics every cycle of
            # war — even in a stalemate, relics change hands while the line holds.
            for it in state["items"]:
                if it["owner"] == loser:
                    it["owner"] = winner

            if gap < STALEMATE_GAP:
                # Stalemate — a war of attrition: both armies bleed, no land.
                state["army"][winner] = max(5, state["army"][winner] - rand(2, 6))
                state["army"][loser] = max(5, state["army"][loser] - rand(2, 6))
                continue

            if frozenset((i, j)) in active:
                continue  # this front is already being fought out

            # The prize is one tile, clamped both by the loser's floor and by how
            # much the *winner still wants*: the pure stop at their fair share
            # (reclaiming only what they lost), the fallen press on toward it all.
            grab = min(TILE_TERR,
                       state["territory"][loser] - MIN_TERR,
                       desired_share(state, winner) - state["territory"][winner])
            if grab <= 0:
                continue
            # Duration from relative army sizes: even armies (~0.5 share) -> ~20
            # cycles, total domination (~1.0) -> ~1 cycle.
            adv = state["army"][winner] / (state["army"][winner] + state["army"][loser])
            norm = max(0.0, min(1.0, (adv - 0.5) / 0.5))
            dur = max(1, min(20, round((1 + (1 - norm) * 19) * rand(0.6, 1.4))))
            state["battles"].append({"winner": winner, "loser": loser,
                                     "tot": grab, "rem": grab, "rate": grab / dur})
            active.add(frozenset((i, j)))

    # 2b) Advance every active battle: deliver its per-cycle slice of land
    #     (clamped to the loser's floor) and bleed both armies. A battle ends
    #     when its land is delivered or the loser can give no more. Battles run
    #     to completion even if the war is diplomatically over — the conquest in
    #     motion still plays out.
    wars = []  # (winner, loser, move%) this cycle — drives the map's flips
    ongoing = []
    aid = state.get("aid")
    aid_pair = frozenset((aid["helped"], aid["opp"])) if aid else None
    wiz_owner = state["owner"][state["wizard"]["r"]][state["wizard"]["c"]]

    def _ended(w, l, floored):
        # Announce the result, but only when the wizard's own realm was in the fight.
        if wiz_owner in (w, l):
            tail = " — driven to its last lands" if floored else ""
            state["town_msg"] = (f"Battle won: {POWERS[w][0]} defeated "
                                 f"{POWERS[l][0]}{tail}.")

    for b in state["battles"]:
        w, l = b["winner"], b["loser"]
        aided = aid_pair is not None and frozenset((w, l)) == aid_pair
        if aided:  # remember the wizard fought in this engagement
            state["aid_log"].setdefault(tuple(sorted((w, l))), {})["engaged"] = True

        if aided and aid["helped"] == l:
            # The wizard reinforces the losing side: the line holds (no land moves
            # this cycle) and the attacker is harried. Hold long enough and the
            # tide turns — the defender becomes the aggressor.
            state["army"][w] = max(5, state["army"][w] - rand(2, 5))
            b["resist"] = b.get("resist", 0) + 1
            if b["resist"] >= AID_FLIP:
                b["winner"], b["loser"] = l, w
                b["rem"] = b["tot"]
                b["resist"] = 0
            ongoing.append(b)
            continue

        cap = max(0.0, min(state["territory"][l] - MIN_TERR,           # loser's floor
                           desired_share(state, w) - state["territory"][w]))  # winner content
        rate = b["rate"] * (AID_RATE_MULT if aided and aid["helped"] == w else 1.0)
        move = min(rate, b["rem"], cap)
        if move <= 0:
            _ended(w, l, True)  # loser is floored — the advance stalls out, battle ends
            continue
        state["territory"][w] += move
        state["territory"][l] -= move
        wars.append((w, l, move))
        state["army"][w] = max(5, state["army"][w] - rand(0, 2))
        state["army"][l] = max(5, state["army"][l] - rand(1, 3))
        b["rem"] -= move
        if b["rem"] > 1e-9:
            ongoing.append(b)
        else:
            _ended(w, l, False)  # the contested tile has fallen — battle complete
    state["battles"] = ongoing

    # 3) Armies regrow toward what their land sustains (+ noise).
    for i in range(N):
        target = 20 + state["territory"][i] * 0.9
        state["army"][i] += (target - state["army"][i]) * 0.15 + rand(-3, 3)
        state["army"][i] = max(5, min(100, state["army"][i]))

    # 4) Renormalize territory to exactly 100% (respect the floor).
    for i in range(N):
        state["territory"][i] = max(MIN_TERR, state["territory"][i])
    s = sum(state["territory"])
    for i in range(N):
        state["territory"][i] = (state["territory"][i] / s) * 100

    # 5) Relics no longer well up from the world on their own — they enter play
    #    only by being *sought*: the wizard's seekers (5b) and the lord's hunt
    #    (_update_lord). Battle still redistributes the relics that exist.

    # 5b) Recruits advance: seekers roam + tick their find-%, then march to and hold
    #     war fronts. (Their fighting strength feeds effective_strength above.)
    _update_recruits(state)

    # 6) Project the new territory onto the world map (border flips only).
    if reconcile:
        reconcile_grid(state, wars)
    else:
        state["_pending_wars"] = wars        # remembered for the next reconcile

    # Remember this cycle's relationships so next cycle can detect war/peace
    # shifts (from drift or slider edits made before the next tick).
    state["prev_hearts"] = [row[:] for row in state["hearts"]]

    # The wizard's aid lasts exactly one cycle; settle any engagement that ended.
    state["aid"] = None
    settle_aid(state)

    # A clan cast down is recovering in the shadow lands — count down to his return.
    cds = state.get("lord_cooldowns")
    if cds:
        for k in list(cds):
            cds[k] -= 1
            if cds[k] <= 0:
                del cds[k]

    # The lieutenant takes stock: brood on his throne, or ride to a pressured front.
    _update_lord(state)
    # His retinue (sown model only): recruit toward LORD_RETINUE, sow secondary
    # realms or reinforce a front.
    _update_lord_agents(state)
    # The wizard's own sowing errand, if the lord offered it and he took it up.
    wiz_sow_tick(state)

    # The world's people age, die, and beget heirs; princes inherit thrones. Runs every
    # cycle — so the centuries skipped while the wizard slumbers turn the generations over.
    _advance_lineage(state)

    _record_history(state)        # sample macro quantities for the dev graphs


def settle_aid(state):
    """Pay out favor for engagements that have ended. An engagement is over once
    no battle is active for the pair and either it has stopped being a war or a
    battle the wizard fought in has resolved. The side he backed more warms to
    him; the other sours — by up to AID_FAVOR_MAX, scaled by the margin of aid."""
    active = {frozenset((b["winner"], b["loser"])) for b in state["battles"]}
    for key in list(state["aid_log"]):
        a, b = key
        if frozenset((a, b)) in active:
            continue  # the battle still rages
        log = state["aid_log"][key]
        at_war = status_of(state["hearts"][a][b])[1] == "war"
        if at_war and not log.get("engaged"):
            continue  # still at war and no battle has resolved yet — keep waiting
        ra, rb = log.get("r", (0, 0))
        net = ra - rb
        if net != 0:
            helped, opp = (a, b) if net > 0 else (b, a)
            delta = min(AID_FAVOR_MAX, abs(net))
            state["favor"][helped] = min(10, state["favor"][helped] + delta)
            state["favor"][opp] = max(0, state["favor"][opp] - delta)
        del state["aid_log"][key]


def advance_days(state, days):
    """Advance world time by `days` (may be fractional), running one cycle() for
    each DAYS_PER_CYCLE-day boundary crossed. This is the single time gate: the
    wizard's steps and the play/step controls all flow through it, so the world
    (battles, drift, regrowth, relics) advances exactly in step with elapsed time."""
    before = state["day"]
    state["day"] = before + days
    crossings = int(state["day"] // DAYS_PER_CYCLE) - int(before // DAYS_PER_CYCLE)
    for _ in range(crossings):
        cycle(state)


def _fallen_powers(state):
    return [i for i in range(N) if state["corruption"][i] >= FALLEN_AT]


def _is_dangerous(state):
    """The hour the wizard is sent: the shadow's lieutenant has risen and is loose in
    the world (a power has passed ARRIVE_AT, 42%) — just after he first appears at
    40% and before he has claimed any throne. The wizard thus arrives to find him
    already abroad, with a window to strike before he enthrones at 50%."""
    return max(state["corruption"]) >= ARRIVE_AT


def age_to_danger(state):
    """Fast-forward the world's unseen history to the tipping point (see
    _is_dangerous). The shadow rises briskly here, then slows to its playable
    creep at arrival; the clock is synced; capped so it always terminates. The
    player thus arrives into a living, contested world — a different crisis each run."""
    while state["cycle"] < AGE_CAP and not _is_dangerous(state):
        cycle(state)
    state["shadow_rate"] = SHADOW_GROWTH          # the brisk pre-history ends
    state["day"] = state["cycle"] * DAYS_PER_CYCLE
    state["town_msg"] = None                      # no stale battle toast at arrival


def slumber_years(state, years):
    """The wizard sleeps a chosen span of years — the world (and its bloodlines) runs on
    unattended. Returns the days that passed. Reconciles the world map only periodically
    (then once exactly at the end) so even a thousand-year sleep stays brisk; the wizard
    is blind while he sleeps, so the lagging border map is never seen. The shadow rises at
    its normal playable creep — sleep long enough with a lieutenant abroad and you may
    wake to a fallen world."""
    days = years * DAYS_PER_YEAR
    target_day = state["day"] + days
    n = int(days // DAYS_PER_CYCLE)
    every = 30                                    # repaint the map every ~30 cycles
    for i in range(n):
        cycle(state, reconcile=(i % every == every - 1))
    reconcile_grid(state, state.pop("_pending_wars", []))   # final exact projection
    state["day"] = target_day
    state["town_msg"] = None
    return days


def skip_to_next_age(state):
    """A won age's peace runs on, unseen, until the shadow regathers a vessel and the
    world is once more in danger — the next age. The mirror of age_to_danger, used
    when the wizard slumbers after restoring peace: the shadow rises briskly again
    off-screen, then slows to its playable creep when the new age dawns. Bounded
    relative to the current cycle (AGE_CAP is absolute) so it always returns. Reports
    the days that elapsed while he slept."""
    start = state["cycle"]
    state["shadow_rate"] = AGE_SHADOW_GROWTH      # off-screen years pass briskly again
    cap = start + AGE_CAP
    while state["cycle"] < cap and not _is_dangerous(state):
        cycle(state)
    state["shadow_rate"] = SHADOW_GROWTH
    state["day"] = state["cycle"] * DAYS_PER_CYCLE
    state["town_msg"] = None
    return (state["cycle"] - start) * DAYS_PER_CYCLE


# ---------- Editable controls (the "sliders") ----------
def build_controls():
    """Flat, stable list of editable fields, in display order."""
    ctrls = [("shadow",)]  # the fallen god's global presence (dev lever)
    for i in range(N):
        ctrls.append(("terr", i))
        ctrls.append(("army", i))
        ctrls.append(("corrupt", i))
        for j in range(N):
            if j != i:
                ctrls.append(("heart", i, j))
    return ctrls


def adjust(state, ctrl, step):
    kind = ctrl[0]
    if kind == "terr":
        i = ctrl[1]
        v = max(MIN_TERR, min(100 - MIN_TERR * (N - 1), state["territory"][i] + step))
        others = [k for k in range(N) if k != i]
        osum = sum(state["territory"][k] for k in others)
        remaining = 100 - v
        for k in others:
            state["territory"][k] = (state["territory"][k] / osum) * remaining if osum > 0 else remaining / len(others)
        state["territory"][i] = v
    elif kind == "army":
        i = ctrl[1]
        state["army"][i] = max(0, min(100, state["army"][i] + step))
    elif kind == "shadow":
        state["shadow"] = max(0.0, min(1.0, state["shadow"] + step * 0.05))
    elif kind == "corrupt":
        i = ctrl[1]
        state["corruption"][i] = max(0.0, min(1.0, state["corruption"][i] + step * 0.05))
    elif kind == "heart":
        i, j = ctrl[1], ctrl[2]
        h = max(0, min(10, state["hearts"][i][j] + (1 if step > 0 else -1)))
        state["hearts"][i][j] = h
        state["hearts"][j][i] = h


# ---------- Rendering ----------
def bar(value, width, ch="#"):
    n = int(round(value / 100 * width))
    return ch * n + "." * (width - n)


def draw(stdscr, state, sel_idx, ctrls, playing, speed, glyph_mode="fancy"):
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    def put(y, x, s, attr=curses.A_NORMAL):
        # Bounds-safe write: clip to the screen and never touch the bottom-right
        # cell (writing it always raises curses.error). Silently skip overflow.
        if y < 0 or y >= h or x < 0 or x >= w - 1:
            return
        try:
            stdscr.addstr(y, x, s[:w - 1 - x], attr)
        except curses.error:
            pass

    if h < 12 or w < 50:
        put(0, 0, "Terminal too small — resize to at least 50x12.")
        stdscr.refresh()
        return

    def pcol(i):
        return curses.color_pair(i + 1)

    row = 0
    relics = len(state["items"])
    head = f"  DOMINION   Day {state['day']:>6.1f}   Relics {relics}/{MAX_ITEMS}   " \
           f"Shadow {state['shadow'] * 100:3.0f}%/dominion {dominion(state) * 100:3.0f}%   " \
           f"Speed {speed}/10   [{'PLAYING' if playing else 'PAUSED'}]"
    put(row, 0, head, curses.A_BOLD)
    row += 1
    put(row, 0, " space play/pause  s step  r reset  arrows move/adjust  [ ] speed  m cycle view  q quit",
        curses.A_DIM)
    row += 2

    fancy = glyph_mode == "fancy"

    def slider_line(label, frac, ctrl, valtext, fill="#", barcol=None):
        # Each metric gets its own bar colour + fill glyph so territory / army /
        # corruption don't read as three identical sliders. When selected the
        # whole row reverses (standard highlight) and the per-metric colour is
        # dropped so the cursor stays legible.
        nonlocal row
        sel = ctrls[sel_idx] == ctrl
        marker = ">" if sel else " "
        head_s = f"{marker} {label:<11} ["
        b = bar(frac * 100, 28, ch=fill)
        tail_s = f"] {valtext}"
        if sel:
            put(row, 1, head_s + b + tail_s, curses.A_REVERSE)
        else:
            put(row, 1, head_s)
            put(row, 1 + len(head_s), b, barcol or curses.A_NORMAL)
            put(row, 1 + len(head_s) + len(b), tail_s)
        row += 1

    # World bar — proportional colored segments.
    barw = w - 2
    col = 1
    for i in range(N):
        seg = max(0, int(round(state["territory"][i] / 100 * barw)))
        if i == N - 1:
            seg = max(0, barw - (col - 1))
        label = f" {round(state['territory'][i])}% "
        text = label.center(seg)[:seg] if seg >= len(label) else " " * seg
        put(row, col, text, curses.color_pair(10 + i) | curses.A_BOLD)
        col += seg
    row += 1

    # Colour key — teach the per-metric colour code used by the faction sliders
    # and battles below, so the three bars never read as interchangeable.
    lx = 1
    put(row, lx, "key: ", curses.A_DIM); lx += 5
    for txt, col_attr in (("land", curses.color_pair(30)),
                          ("army", curses.color_pair(21)),
                          ("corruption", curses.color_pair(23)),
                          ("⚔battle" if fancy else "!battle", curses.color_pair(23) | curses.A_BOLD)):
        put(row, lx, txt, col_attr); lx += len(txt)
        put(row, lx, "  ", curses.A_DIM); lx += 2
    row += 2

    # Who the shadow is working on — always visible, above the (tall) faction list.
    _corruption_strip(put, row, state, glyph_mode == "fancy")
    row += 2

    # The fallen god's presence — a dev lever; drive it and watch the world react.
    slider_line("Shadow", state["shadow"], ("shadow",), f"{state['shadow'] * 100:5.1f}%",
                fill=("░" if fancy else "."), barcol=curses.color_pair(23))
    row += 1

    # Active battles — in-progress multi-cycle land transfers. Rendered with an
    # arrow gauge (▶ advancing on the loser) in the winner's colour, so it reads
    # as a directional conquest rather than another level slider.
    battles = state["battles"]
    if battles:
        swd, arr, emp = (("⚔", "▶", "·") if fancy else ("!", ">", "-"))
        put(row, 1, f"{swd} BATTLES", curses.color_pair(23) | curses.A_BOLD)
        row += 1
        for b in battles:
            if row >= h - 1:
                break
            w_, l_ = b["winner"], b["loser"]
            left = max(1, math.ceil(b["rem"] / b["rate"])) if b["rate"] > 0 else 1
            done = 0 if b["tot"] <= 0 else max(0.0, min(1.0, 1 - b["rem"] / b["tot"]))
            nfill = max(0, min(12, int(round(done * 12))))
            prog = arr * nfill + emp * (12 - nfill)
            wname, lname = POWERS[w_][0], POWERS[l_][0].split()[0]
            head_b = f"   {wname} {arr}{arr} {lname:<8} "
            put(row, 1, head_b, pcol(w_) | curses.A_BOLD)
            put(row, 1 + len(head_b), prog, pcol(w_))
            put(row, 1 + len(head_b) + len(prog), f" {round(done*100):3d}%  {left:>2} cyc")
            row += 1
        row += 1

    for i in range(N):
        if row >= h - 1:
            break
        name, _ = POWERS[i]
        boost = item_boost(state, i)
        # Power header
        put(row, 0, " " + name, pcol(i) | curses.A_BOLD)
        put(row, 22, f"{state['territory'][i]:5.1f}% land   army {round(state['army'][i]):3d}   "
                     f"relics +{round(boost*100)}% battle")
        row += 1

        # Territory, army, corruption sliders — colour + glyph keep them apart:
        #   land  = this faction's own colour, solid block   (their prosperity)
        #   army  = cyan, bars                                (their muscle)
        #   corrupt = red, the band's decay glyph            (the shadow's grip)
        slider_line("land", state["territory"][i] / 100, ("terr", i),
                    f"{state['territory'][i]:5.1f}%",
                    fill=("█" if fancy else "#"), barcol=pcol(i))
        slider_line("army", state["army"][i] / 100, ("army", i),
                    f"{state['army'][i]:5.1f}",
                    fill=("▮" if fancy else "="), barcol=curses.color_pair(21))
        cor = state["corruption"][i]
        band = _corrupt_band(cor)
        clabel = CORRUPT_BANDS[band][3]
        cglyph = CORRUPT_BANDS[band][1] if fancy else CORRUPT_BANDS[band][2]
        slider_line("corruption", cor, ("corrupt", i), f"{cor * 100:5.1f}%  {clabel}",
                    fill=cglyph, barcol=curses.color_pair(23))

        # Relics held
        held = [it for it in state["items"] if it["owner"] == i]
        if held:
            txt = "  ".join(f"{ITEM_TYPES[it['type']]['icon']} {ITEM_TYPES[it['type']]['name']} "
                            f"+{round(ITEM_TYPES[it['type']]['boost']*100)}%" for it in held)
        else:
            txt = "- none -"
        put(row, 3, "relics: " + txt, curses.A_DIM)
        row += 1

        # Relations
        for j in range(N):
            if j == i:
                continue
            hv = state["hearts"][i][j]
            label, key = status_of(hv)
            ctrl = ("heart", i, j)
            sel = ctrls[sel_idx] == ctrl
            marker = ">" if sel else " "
            attr = curses.A_REVERSE if sel else curses.color_pair(20 + ["allied", "peace", "enemy", "war"].index(key))
            hb = bar(hv / 10 * 100, 10, ch="=")
            other = POWERS[j][0].split()[0]
            put(row, 3, f"{marker} {other:<8} [{hb}] {hv:>2}  {label}", attr)
            row += 1
        row += 1

    stdscr.refresh()


# Corruption bands (dev view only): pure -> tempted -> falling -> fallen, by
# corruption level. Each has a map fill (territory frays as the shadow eats it)
# and a label.
CORRUPT_BANDS = [(0.05, "█", "#", "pure"),
                 (0.25, "▓", "=", "tempted"),
                 (0.50, "▒", ":", "falling"),    # fallen begins at FALLEN_AT (0.50)
                 (1.01, "░", ".", "fallen")]


def _corrupt_band(cor):
    for i, (hi, _, _, _) in enumerate(CORRUPT_BANDS):
        if cor < hi:
            return i
    return len(CORRUPT_BANDS) - 1


def _gauge(frac, width, fancy=True):
    filled = max(0, min(width, int(round(frac * width))))
    full, empty = ("▓", "░") if fancy else ("#", "-")
    return full * filled + empty * (width - filled)


def _corruption_strip(put, y, state, fancy):
    """One always-visible line naming each faction's corruption (who the shadow is
    working on), color-coded, with †/‡ marking the falling/fallen. Used by both
    dev views so the per-faction picture is never lost below a tall layout."""
    cx = 1
    put(y, cx, "Corrupting:", curses.A_DIM)
    cx += 12
    for i in range(N):
        cor = state["corruption"][i]
        band = _corrupt_band(cor)
        tag = (("", "", "†", "‡") if fancy else ("", "", "+", "*"))[band]
        seg = f"{POWERS[i][0].split()[0]} {cor * 100:2.0f}%{tag}  "
        put(y, cx, seg, curses.color_pair(i + 1) |
            (curses.A_BOLD if band >= 2 else curses.A_NORMAL))
        cx += len(seg)


def draw_world(stdscr, state, playing, speed, glyph_mode):
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    def put(y, x, s, attr=curses.A_NORMAL):
        if y < 0 or y >= h or x < 0 or x >= w - 1:
            return
        try:
            stdscr.addstr(y, x, s[:w - 1 - x], attr)
        except curses.error:
            pass

    g = GLYPHS[glyph_mode]
    fancy = glyph_mode == "fancy"
    owner = state["owner"]
    wiz = state["wizard"]
    wcol = curses.color_pair(30) | curses.A_BOLD  # wizard: bright, faction-neutral

    head = f"  DOMINION  WORLD [dev]   Day {state['day']:>6.1f}   Speed {speed}/10   " \
           f"Shadow {state['shadow'] * 100:4.1f}%   [{'PLAYING' if playing else 'PAUSED'}]"
    put(0, 0, head, curses.A_BOLD)
    put(1, 0, " arrows walk (1 tile = 10d)  m cycle view  g glyphs  space auto  s +10d"
              "  r reset  [ ] speed  q quit", curses.A_DIM)

    # Per-faction corruption strip — always visible above the map, so you can see
    # who the shadow is working on (the map itself frays in each one's color).
    _corruption_strip(put, 2, state, fancy)

    top = 3
    # The world can be larger than the screen, so render it as a fit-to-screen
    # minimap: each displayed cell summarizes a `block`×`block` patch of tiles by
    # its majority owner, shaded by that power's corruption; the wizard's patch and
    # any town take precedence. This keeps the whole-world overview at any GRID.
    avail_rows = max(8, h - top - (N + 12))   # reserve readout + a battles list
    avail_cols = max(8, (w - 2) // 2)
    block = max(1, -(-GRID // min(avail_rows, avail_cols)))   # ceil div
    disp = -(-GRID // block)
    towns = state["towns"]
    lord = state.get("lord")
    # The wizard senses the shadow's lieutenant only if his Wits are keen enough
    # (the Wits unlock). Below the threshold the lord is hidden — no marker, no readout.
    sense_lord = state["wiz_stats"][2] >= WIZ_WITS_SENSE_AT
    lr, lc = (lord["r"], lord["c"]) if (lord and sense_lord) else (-1, -1)
    lcol = curses.color_pair(23) | curses.A_BOLD          # lord: shadow-hued
    for dr in range(disp):
        for dc in range(disp):
            x = 1 + dc * 2
            counts = {}
            town_here = None
            wiz_here = False
            lord_here = False
            for r in range(dr * block, min(GRID, dr * block + block)):
                for c in range(dc * block, min(GRID, dc * block + block)):
                    o = owner[r][c]
                    counts[o] = counts.get(o, 0) + 1
                    if (r, c) == (wiz["r"], wiz["c"]):
                        wiz_here = True
                    if (r, c) == (lr, lc):
                        lord_here = True
                    t = towns.get((r, c))
                    if t is not None and (town_here is None or t["capital"]):
                        town_here = t
            maj = max(counts, key=counts.get)
            base = curses.color_pair(maj + 1) | curses.A_BOLD
            if wiz_here:
                put(top + dr, x, g["wizard"], wcol)           # emoji spans 2 cells
                if not fancy:
                    put(top + dr, x + 1, "█", base)
            elif lord_here:
                put(top + dr, x, g["lord"], lcol)             # the lieutenant, on the world
                if not fancy:
                    put(top + dr, x + 1, "█", base)
            elif town_here is not None:
                gf, ga = TOWN_MAP_GLYPH[town_here["capital"]]
                put(top + dr, x, (gf if fancy else ga),
                    curses.color_pair(town_here["faction"] + 1) | curses.A_BOLD)
                put(top + dr, x + 1, "█", base)
            else:
                bnd = _corrupt_band(state["corruption"][maj])  # frays as the owner corrupts
                fill = CORRUPT_BANDS[bnd][1] if fancy else CORRUPT_BANDS[bnd][2]
                put(top + dr, x, fill * 2, base)

    wo = owner[wiz["r"]][wiz["c"]]
    ly = top + disp + 1
    put(ly, 1, f"{g['wizard']} Wizard at ({wiz['r']:2d},{wiz['c']:2d}) — "
               f"in the land of {POWERS[wo][0]}", wcol)
    # The fallen god as an agent: its raw presence, and its DOMINION (its grip on
    # the world — its score), plus which vessel it is currently working on.
    sh, dom, focus = state["shadow"], dominion(state), state["shadow_target"]
    put(ly + 1, 1, f"Shadow of the Fallen God  {_gauge(sh, 24, fancy)}  {sh * 100:4.1f}% presence",
        curses.color_pair(23) | (curses.A_BOLD if sh >= 0.5 else curses.A_DIM))
    dom_tail = "  ☠ THE WORLD HAS FALLEN" if dom >= DOMINION_WIN else f"  · working on {POWERS[focus][0]}"
    put(ly + 2, 1, f"  its dominion          {_gauge(dom, 24, fancy)}  {dom * 100:4.1f}% of the world{dom_tail}",
        curses.color_pair(23) | curses.A_BOLD)
    ly += 3
    # The lieutenant: where he is and what he is up to — but only if the wizard's
    # Wits are keen enough to sense him (else he moves unseen). He has no throne to
    # sit until his clan falls (≥ 50%); until then he is loose and exposed.
    if lord is not None and sense_lord:
        if lord["task"] == "brood":
            doing = "enthroned at his seat — corruption surges"
        elif lord["task"] == "seek":
            rn = ITEM_TYPES[lord["target"]]["name"] if lord.get("target") is not None else "a relic"
            doing = f"abroad, hunting the {rn} ({lord.get('prog', 0):.0f}%)"
        elif lord["task"] == "march":
            doing = f"riding to war on {POWERS[lord['target']][0]}"
        elif lord["task"] == "return":
            doing = "returning to claim his throne"
        else:  # rising
            doing = "rising, roaming the marches"
        title = "Dark Lord" if lord["enthroned"] else "Dark Lord (rising)"
        put(ly, 1, f"{g['lord']} {title} of {POWERS[lord['fac']][0]} at "
                   f"({lord['r']:2d},{lord['c']:2d}) — {doing}",
            curses.color_pair(23) | curses.A_BOLD)
        ly += 1
    ly += 1
    uglyph = g["unit"] or "#"
    for i in range(N):
        n = max(1, int(round(state["army"][i] / ARMY_PER_UNIT)))
        cor = state["corruption"][i]
        label = CORRUPT_BANDS[_corrupt_band(cor)][3]
        vessel = " ◀ vessel" if i == focus else ""
        put(ly + i, 2,
            f"{uglyph}x{n:<2} {POWERS[i][0]:<16} {state['territory'][i]:4.1f}% land"
            f"   army {round(state['army'][i]):3d}   favor {state['favor'][i]:2d}/10"
            f"   {label:<8} {_gauge(cor, 10, fancy)} {cor * 100:3.0f}%{vessel}",
            curses.color_pair(i + 1) | curses.A_BOLD)

    # Battles — their own category: just who is fighting whom (and how far the
    # tile has fallen). The controls view carries the per-battle gauges; here it
    # is a glanceable roster of the world's live conflicts.
    by = ly + N + 1
    swd, arr = ("⚔", "▶") if fancy else ("x", ">")
    battles = state["battles"]
    if battles:
        put(by, 1, f"{swd} BATTLES", curses.color_pair(23) | curses.A_BOLD)
        by += 1
        for b in battles:
            if by >= h - 1:
                break
            w_, l_ = b["winner"], b["loser"]
            done = 0 if b["tot"] <= 0 else max(0.0, min(1.0, 1 - b["rem"] / b["tot"]))
            put(by, 2, f"{POWERS[w_][0]} {arr} {POWERS[l_][0]:<16} {round(done * 100):3d}% taken",
                curses.color_pair(w_ + 1) | curses.A_BOLD)
            by += 1
    else:
        put(by, 1, f"{swd} BATTLES   none — no land is changing hands", curses.A_DIM)

    stdscr.refresh()


TILE_GS = 20  # the micro arena is a TILE_GS x TILE_GS scene inside one macro tile

# Time model: the wizard walks the micro arena a half-day per step, so crossing a
# tile (TILE_GS steps) takes DAYS_PER_CYCLE days = exactly one cycle() of world
# simulation. A macro-map step crosses a whole tile at once (= one cycle).
DAYS_PER_STEP = 0.5
DAYS_PER_CYCLE = TILE_GS * DAYS_PER_STEP  # = 10 days per cycle

# Realtime arena (the tile view): the wizard roams in wall-clock time rather than
# per-step. SECONDS_PER_DAY seconds spent in a tile = one world-day; the elapsed
# time is banked (rounded up) into advance_days() when he steps off into the next
# macro tile. The macro sim is frozen while he is inside a tile.
SECONDS_PER_DAY = 20.0
# Monsters: a fallen region (owner corruption >= FALLEN_AT) is infested by the
# shadow's minions — stationary LOS sentries that fire a slow projectile down the
# wizard's row/column. Count scales with how far the owner has fallen.
MONSTER_MAX = 5                 # most minions on a single tile (at full corruption)
# Even an un-fallen world isn't wholly safe: the odd wild lair lurks out in the
# deep country (away from any town), so crossing the wilderness has a little bite.
WILD_LAIR_CHANCE = 0.12         # fraction of deep-country tiles harbouring beasts
WILD_LAIR_MIN_DIST = 3          # min tiles from any town for a wild lair to lurk
MONSTER_FIRE_COOLDOWN = 1.6     # seconds between a sentry's shots
MONSTER_PROJ_SPEED = 4.0        # projectile cells/second (slow — dodgeable)
WIZ_FIRE_COOLDOWN = 0.35        # seconds between the wizard's bolts
WIZ_PROJ_SPEED = 16.0           # the wizard's bolt is fast
# Charged bolt (the Might unlock): HOLD space to wind it up, release to loose a heavy
# projectile that PIERCES (kills every minion in its path, not just the first) and
# carries a `power` for HP foes later. A tap is a basic bolt; holding charges; moving
# aborts. ("Hold" is inferred from key auto-repeat — curses has no key-release — so
# SPACE_HOLD_GRACE is generous enough to ride out a slow repeat delay.)
WIZ_CHARGE_FULL = 1.5           # seconds of holding to reach full charge
WIZ_CHARGE_MIN = 0.7            # minimum hold before a charged bolt looses (else it was a tap)
WIZ_CHARGE_PROJ_SPEED = 22.0    # the charged bolt flies faster than a basic barb
WIZ_CHARGE_COOLDOWN = 0.6       # recovery after loosing a charged bolt
SPACE_HOLD_GRACE = 0.6          # max gap between space inputs still counted as "holding"


def tile_enemy(state, wr, wc):
    """The power contesting the wizard's tile: a neighbour at war with its owner
    (preferring one with an active battle here). None if the tile is peaceful."""
    owner = state["owner"]
    wo = owner[wr][wc]
    foes = {owner[nr][nc] for nr, nc in _neighbors(wr, wc) if owner[nr][nc] != wo}
    foes = [e for e in foes if status_of(state["hearts"][wo][e])[1] == "war"]
    if not foes:
        return None
    fighting = {b["loser"] if b["winner"] == wo else b["winner"]
                for b in state["battles"] if wo in (b["winner"], b["loser"])}
    foes.sort(key=lambda e: (e not in fighting, e))  # active battle first
    return foes[0]


def _town_distance(state, wr, wc):
    """Chebyshev tiles from (wr,wc) to the nearest town (inf if there are none)."""
    best = float("inf")
    for (tr, tc) in state["towns"]:
        best = min(best, max(abs(tr - wr), abs(tc - wc)))
    return best


def spawn_monsters(state, wr, wc):
    """The shadow's minions infesting a macro tile. A *fallen* tile (owner corruption
    >= FALLEN_AT) is heavily infested — count scales with how far it has fallen. An
    un-fallen tile is mostly safe, but the odd **wild lair** lurks in the deep country
    (>= WILD_LAIR_MIN_DIST tiles from any town), so the wilderness still bites. Either
    way the tile must be peaceful and townless (contested/town scenes draw their own
    hosts). Placement is deterministic per (world_seed, r, c) — a given tile always
    lays out the same. Returns a list of {r, c, cool} sentries (cool = seconds to fire)."""
    if tile_enemy(state, wr, wc) is not None or (wr, wc) in state["towns"]:
        return []
    cor = state["corruption"][state["owner"][wr][wc]]
    seed = (state["world_seed"] * 2246822519 + wr * GRID + wc) & 0xFFFFFFFF
    rng = random.Random(seed)
    art, adist = _near_artifact_loc(state, wr, wc)
    if cor < FALLEN_AT:
        # Not fallen: only a wild lair, and only out in the deep country.
        if (_town_distance(state, wr, wc) < WILD_LAIR_MIN_DIST
                or rng.random() >= WILD_LAIR_CHANCE):
            n = 0
        else:
            n = rng.randint(1, 2)
    else:
        frac = (cor - FALLEN_AT) / (1.0 - FALLEN_AT)    # 0 at the fallen line, 1 at full
        n = 1 + int(round(frac * (MONSTER_MAX - 1)))
    # The ground around a hidden artifact crawls with the shadow's guard — the nearer
    # the resting place, the thicker the watch (the resting tile itself gets a mini-boss
    # in the pygame view, on top of these).
    if art is not None:
        n = max(n, MONSTER_MAX - adist)                 # adist 0..rad → a full-to-light watch
    if n <= 0:
        return []
    mid = TILE_GS // 2
    mons, taken = [], set()
    for _ in range(n):
        for _try in range(20):                          # a few tries to find a free spot
            r = rng.randint(2, TILE_GS - 3)
            c = rng.randint(2, TILE_GS - 3)
            if (r, c) in taken or (abs(r - mid) <= 1 and abs(c - mid) <= 1):
                continue                                # not stacked, not on the entry point
            taken.add((r, c))
            mons.append({"r": r, "c": c, "cool": rng.uniform(0.3, MONSTER_FIRE_COOLDOWN)})
            break
    return mons


# ---------- Terrain (cosmetic NES-overworld decoration for the tile view) ----------
# Each macro tile renders as a screen of overworld terrain, generated
# deterministically from its (r, c) coords so the scene is identical whenever the
# wizard walks back to it. Terrain is a pure cosmetic projection of owner[][]
# (which sets the biome) + position; like the world map it never feeds the sim.
#
# Each power's land has its own biome. Like a Zelda overworld screen, every tile
# is an open clearing framed by a jagged "wall" of terrain (trees, mountains…),
# with features scattered across the interior.
BIOMES = {
    0: {"flavor": "the ashen reach of",  "ground": "rock",  "wall": "peak",
        "scatter": [("boulder", 0.08)]},                                   # Crimson
    1: {"flavor": "the azure coast of",  "ground": "grass", "wall": "tree",
        "scatter": [("flower", 0.05)], "pond": True},                      # Azure
    2: {"flavor": "the verdant wood of", "ground": "grass", "wall": "tree",
        "scatter": [("tree", 0.10), ("flower", 0.06)]},                    # Verdant
    3: {"flavor": "the gilded sands of", "ground": "sand",  "wall": "boulder",
        "scatter": [("boulder", 0.04)]},                                   # Gilded
}
GROUND_BG = {"grass": "grass", "sand": "sand", "rock": "rock"}  # ground kind -> TERR key

TERR = {}              # resolved terrain colors, filled by init_terrain()
_PAIR_CACHE = {}       # (fg, bg) -> allocated curses pair number
_PAIR_NEXT = [50]      # next free pair number (terrain pairs start past the fixed ones)


def init_terrain():
    """Pick a terrain palette once color support is known (rich 256-color, else 8)."""
    if curses.COLORS >= 256:
        TERR.update(grass=71, grass_d=65, tree=40, tree_bg=22, flower=(211, 219, 222),
                    water=25, water_f=45, sand=180, sand_d=143, rock=95, rock_d=240,
                    boulder=137, peak=223, peak_bg=137, wiz=231)
    else:
        K, G, Y, B = (curses.COLOR_BLACK, curses.COLOR_GREEN,
                      curses.COLOR_YELLOW, curses.COLOR_BLUE)
        W, C = curses.COLOR_WHITE, curses.COLOR_CYAN
        TERR.update(grass=G, grass_d=G, tree=K, tree_bg=G, flower=(W,),
                    water=B, water_f=C, sand=Y, sand_d=Y, rock=K, rock_d=W,
                    boulder=W, peak=W, peak_bg=K, wiz=W)


def _cpair(fg, bg):
    """A curses attr for fg-on-bg, allocating pairs lazily (and safely)."""
    key = (fg, bg)
    if key in _PAIR_CACHE:
        return curses.color_pair(_PAIR_CACHE[key])
    n = _PAIR_NEXT[0]
    if n >= min(curses.COLOR_PAIRS, 256):
        return curses.A_NORMAL  # out of pairs (tiny 8-color terminal) — degrade
    try:
        curses.init_pair(n, fg, bg)
    except curses.error:
        return curses.A_NORMAL
    _PAIR_CACHE[key] = n
    _PAIR_NEXT[0] = n + 1
    return curses.color_pair(n)


# A terrain cell is (fancy_glyph, ascii_glyph, fg, bg).
def _ground_cell(kind, rng):
    bg = {"grass": TERR["grass"], "sand": TERR["sand"], "rock": TERR["rock"]}[kind]
    tuft = {"grass": ("ʼ", ",", TERR["grass_d"], 0.16),
            "sand":  ("˙", ".", TERR["sand_d"], 0.14),
            "rock":  ("∴", ":", TERR["rock_d"], 0.18)}[kind]
    if rng.random() < tuft[3]:
        return (tuft[0], tuft[1], tuft[2], bg)
    return (" ", " ", bg, bg)


def _feature_cell(kind, rng, ground_bg):
    if kind == "tree":
        return ("♣", "T", TERR["tree"], TERR["tree_bg"])
    if kind == "flower":
        return ("❀", "*", rng.choice(TERR["flower"]), ground_bg)
    if kind == "boulder":
        return ("●", "o", TERR["boulder"], ground_bg)
    if kind == "peak":
        return ("▲", "^", TERR["peak"], TERR["peak_bg"])
    if kind == "water":
        return ("≈", "~", TERR["water_f"], TERR["water"])
    return (" ", " ", ground_bg, ground_bg)


def tile_terrain(state, wr, wc):
    """Deterministic TILE_GS×TILE_GS terrain for a peaceful macro tile: an open
    clearing framed by a jagged wall, Zelda-overworld style.
    Returns (cells, biome_flavor, ground_bg)."""
    b = BIOMES[state["biome_map"][wr][wc]]
    seed = (state["world_seed"] * 2654435761 + wr * GRID + wc) & 0xFFFFFFFF
    rng = random.Random(seed)                           # stable per (run, tile)
    ground_bg = TERR[GROUND_BG[b["ground"]]]
    cells = [[_ground_cell(b["ground"], rng) for _ in range(TILE_GS)]
             for _ in range(TILE_GS)]

    # Jagged wall framing the screen: density falls off away from the edge.
    edge_prob = {0: 0.92, 1: 0.5, 2: 0.14}
    for r in range(TILE_GS):
        for c in range(TILE_GS):
            d = min(r, c, TILE_GS - 1 - r, TILE_GS - 1 - c)
            if d in edge_prob and rng.random() < edge_prob[d]:
                cells[r][c] = _feature_cell(b["wall"], rng, ground_bg)

    if b.get("pond"):                                   # a pond in the open interior
        cy, cx = rng.randint(3, TILE_GS - 4), rng.randint(3, TILE_GS - 4)
        rad = rng.randint(2, 3)
        for r in range(TILE_GS):
            for c in range(TILE_GS):
                if (r - cy) ** 2 + (c - cx) ** 2 <= rad * rad:
                    cells[r][c] = _feature_cell("water", rng, ground_bg)

    for r in range(TILE_GS):                            # scatter features in the clearing
        for c in range(TILE_GS):
            if min(r, c, TILE_GS - 1 - r, TILE_GS - 1 - c) < 2:
                continue
            if cells[r][c][3] == TERR["water"]:
                continue
            for kind, prob in b["scatter"]:
                if rng.random() < prob:
                    cells[r][c] = _feature_cell(kind, rng, ground_bg)
                    break
    return cells, b["flavor"], ground_bg


def tile_town(state, wr, wc):
    """Lay a settlement over a town tile's wilderness: buildings and townsfolk
    placed deterministically (stable per run). Returns (cells, town, buildings,
    npcs) where buildings is [(r, c, kind)] and npcs is [(r, c)]."""
    cells = tile_terrain(state, wr, wc)[0]       # the setting is the tile's terrain
    town = state["towns"][(wr, wc)]
    rng = random.Random((state["world_seed"] * 40499 + wr * GRID + wc) & 0xFFFFFFFF)

    interior = [(r, c) for r in range(TILE_GS) for c in range(TILE_GS)
                if min(r, c, TILE_GS - 1 - r, TILE_GS - 1 - c) >= 3]
    rng.shuffle(interior)
    occupied = set()
    buildings = []
    if town.get("main"):                         # the keep dominates the city's anchor tile
        mid = TILE_GS // 2
        buildings.append((mid, mid, "keep"))
        occupied.add((mid, mid))
    want_b = 9 if town["capital"] else 4         # city districts are denser than villages
    for (r, c) in interior:
        if len(buildings) >= want_b:
            break
        if any(abs(r - br) <= 1 and abs(c - bc) <= 1 for br, bc, _ in buildings):
            continue                             # keep buildings from touching
        buildings.append((r, c, "house"))
        occupied.add((r, c))

    npcs = []
    want_n = rng.randint(6, 9) if town["capital"] else rng.randint(3, 5)
    for (r, c) in interior:
        if len(npcs) >= want_n:
            break
        if (r, c) in occupied:
            continue
        npcs.append((r, c))
        occupied.add((r, c))
    return cells, town, buildings, npcs


def town_elders(state, wr, wc):
    """Which townsfolk are elders, and the ONE tale each carries — either a faction
    relic's legend (npc index -> ("relic", relic type)) or a mythical creature's
    rumoured whereabouts (npc index -> ("artifact", artifact index)). Deterministic
    per town. An elder knows only the one tale; greeting them is how the wizard first
    LEARNS a relic exists (then it's seekable) or that an artifact's creature can be
    found (then it shows on the map, gold-dot marked, until heard)."""
    npcs = tile_town(state, wr, wc)[3]
    rng = random.Random((state["world_seed"] * 6364136 ^ (wr * GRID + wc) ^ 0xE1DE2) & 0xFFFFFFFF)
    pool = [("relic", t) for t in range(len(ITEM_TYPES))]
    pool += [("artifact", i) for i in range(len(state.get("artifacts", [])))]
    return {i: rng.choice(pool) for i in range(len(npcs)) if rng.random() < 0.35}


def roll_townsfolk(state, wr, wc):
    """The named crowd on a peaceful town tile, aligned by index with tile_town()'s npc
    list. Living **kin of the realm's house** mingle here (named descendants you can
    recruit — a prince, even), the rest are commoners generated fresh per visit. So after
    skipping a century the faces have changed to the *offspring* of those you knew. Each
    entry is {given, house, stats, race, age, notable, role}. [] off/contested a town."""
    town = state["towns"].get((wr, wc))
    if town is None or tile_enemy(state, wr, wc) is not None:
        return []
    npcs = tile_town(state, wr, wc)[3]
    fac = town["faction"]
    race = state["race"][fac]
    folk = []
    kin = [p for p in state.get("notables", []) if p["fac"] == fac]
    random.shuffle(kin)
    for p in kin[:min(len(npcs), 3)]:                 # a few of the house's living kin
        folk.append({"given": p["given"], "house": p["house"], "stats": list(p["stats"]),
                     "race": race, "age": person_age(state, p), "notable": p["id"],
                     "role": p["role"]})
    lo, hi, mat = RACE_AGE[race]
    while len(folk) < len(npcs):                       # commoners fill the rest
        folk.append({"given": random.choice(GIVEN_NAMES), "house": "", "stats": roll_stats(race),
                     "race": race, "age": random.randint(mat, int(hi * 0.5)),
                     "notable": None, "role": "folk"})
    return folk[:len(npcs)]


def merchant_here(state):
    """Where the wizard can buy food right now: 'town' (a market) if his tile holds
    a settlement, 'pedlar' if a travelling merchant happens to be on his tile (a
    random chance rolled per tile he enters), else None."""
    if (state["wizard"]["r"], state["wizard"]["c"]) in state["towns"]:
        return "town"
    return "pedlar" if state["pedlar_here"] else None


def draw_tile(stdscr, state, glyph_mode, lr, lc, arena):
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    def put(y, x, s, attr=curses.A_NORMAL):
        if y < 0 or y >= h or x < 0 or x >= w - 1:
            return
        try:
            stdscr.addstr(y, x, s[:w - 1 - x], attr)
        except curses.error:
            pass

    g = GLYPHS[glyph_mode]
    fancy = glyph_mode == "fancy"
    owner = state["owner"]
    wr, wc = state["wizard"]["r"], state["wizard"]["c"]
    wo = owner[wr][wc]
    enemy = tile_enemy(state, wr, wc)
    # The actual battle raging on this front, if any (soldiers show only while one
    # is being fought; when it's won/over they withdraw).
    battle = None
    if enemy is not None:
        battle = next((b for b in state["battles"]
                       if {b["winner"], b["loser"]} == {wo, enemy}), None)

    # Live day counter: seconds spent in this tile accrue toward world-days, banked
    # when the wizard steps off into the next tile (see SECONDS_PER_DAY).
    days_here = int((arena["now"] - arena["enter"]) / SECONDS_PER_DAY)
    put(0, 0, f"  DOMINION  TILE ({wr:2d},{wc:2d})   Day {state['day']:>6.1f}  (+{days_here}d here)",
        curses.A_BOLD)
    # Row 1: a transient message if any, else the key hints.
    if state.get("town_msg"):
        put(1, 0, " " + state["town_msg"], curses.color_pair(30) | curses.A_BOLD)
    else:
        put(1, 0, " arrows walk  space tap=bolt/hold=charge  c char  m view  g glyph  f eat  z sleep  b buy  k recruit  p music  r reset  q quit",
            curses.A_DIM)
    # Row 2: the wizard's own upkeep (player-facing, unlike corruption) — life first.
    hp = state["health"]
    mh = wiz_max_hearts(state)
    heart, heart_off = ("♥", "♡") if fancy else ("#", "-")
    life_seg = f" Life {heart * hp}{heart_off * max(0, mh - hp)} {hp}/{mh}   "
    hcol = curses.color_pair(23 if hp <= 3 else 30) | curses.A_BOLD
    put(2, 0, life_seg, hcol)
    e = state["energy"]
    ecol = curses.color_pair(23 if e <= AID_ENERGY else 30) | curses.A_BOLD
    status = (f"Energy {_gauge(e / ENERGY_MAX, 12, fancy)} {round(e):3d}/100"
              f"   Food {state['food']}   Gold {state['gold']}g")
    if state["searches"]:
        status += f"   · {len(state['searches'])} seeker(s) out"
    where = merchant_here(state)
    if where:
        status += f"   · {'market' if where == 'town' else 'pedlar'} here — [b] buy ({FOOD_PRICE}g)"
    put(2, len(life_seg), status, ecol)
    # Row 3: the charged-bolt channel gauge — shown only once the hold has passed the
    # minimum (so a quick basic tap never flashes it; only a committed charge shows).
    chg = arena.get("charging")
    if chg is not None and chg >= WIZ_CHARGE_MIN:
        frac = min(1.0, chg / WIZ_CHARGE_FULL)
        tail = "FULL — release to loose" if frac >= 1.0 else "release to loose · move cancels"
        put(3, 0, f" Charging {_gauge(frac, 18, fancy)} {round(frac * 100):3d}%   {tail}",
            curses.color_pair(30) | curses.A_BOLD)

    town = state["towns"].get((wr, wc))
    if town is not None:
        cells, town, buildings, npcs = tile_town(state, wr, wc)
        flavor = None
    else:
        cells, flavor, _ = tile_terrain(state, wr, wc)
        buildings, npcs = [], []

    top, left = 4, 2
    # Bordered playfield (the NES "screen" frame).
    tl, tr, bl, br, hz, vt = (("┌", "┐", "└", "┘", "─", "│") if fancy
                              else ("+", "+", "+", "+", "-", "|"))
    bw = TILE_GS * 2
    border = curses.color_pair(30)
    put(top - 1, left - 1, tl + hz * bw + tr, border)
    put(top + TILE_GS, left - 1, bl + hz * bw + br, border)
    for r in range(TILE_GS):
        put(top + r, left - 1, vt, border)
        put(top + r, left + bw, vt, border)

    for r in range(TILE_GS):                            # paint the terrain field
        for c in range(TILE_GS):
            gf, ga, fg, bg = cells[r][c]
            put(top + r, left + c * 2, (gf if fancy else ga) + " ", _cpair(fg, bg))

    def sprite(r, c, glyph_fancy, glyph_ascii, fg):
        # Fancy emoji span two cells; an ascii glyph needs a filler. Either way it
        # sits on the bg of the terrain cell beneath it.
        s = glyph_fancy if fancy else glyph_ascii + " "
        put(top + r, left + c * 2, s, _cpair(fg, cells[r][c][3]) | curses.A_BOLD)

    for (r, c, kind) in buildings:                      # town buildings
        gf, ga = TOWN_GLYPH[kind]
        sprite(r, c, gf, ga, POWERS[town["faction"]][1])

    if battle is not None:
        # Two hosts of soldiers drawn up across the clearing while a battle rages
        # (they withdraw once it's won). Enemy holds the upper ranks, owner lower.
        for c in range(2, TILE_GS - 2):
            for p, ranks in ((enemy, (2, 3)), (wo, (TILE_GS - 4, TILE_GS - 3))):
                glyph = FACTION_EMOJI[p] if fancy else POWERS[p][0][0] + " "
                for r in ranks:
                    put(top + r, left + c * 2, glyph,
                        _cpair(POWERS[p][1], cells[r][c][3]) | curses.A_BOLD)
        # Legendary items each host has brought to the field — the spoils the
        # victor will seize. They sit just behind each line, centred.
        for p, row in ((enemy, 5), (wo, TILE_GS - 6)):
            held = [it for it in state["items"] if it["owner"] == p]
            base_c = TILE_GS // 2 - (len(held) - 1)
            for k, it in enumerate(held):
                kind = ITEM_TYPES[it["type"]]
                sprite(row, base_c + k * 2, kind["emoji"], kind["icon"], curses.COLOR_YELLOW)
    elif enemy is None and town is not None:            # peaceful town: the populace
        for i, (r, c) in enumerate(npcs):
            sprite(r, c, TOWNSFOLK[i % len(TOWNSFOLK)], "o", POWERS[town["faction"]][1])

    if merchant_here(state) == "pedlar":                # a roaming pedlar on this tile
        sprite(2, TILE_GS // 2, PEDLAR_GLYPH[0], PEDLAR_GLYPH[1], curses.COLOR_YELLOW)

    for m in arena["monsters"]:                         # the shadow's minions (fallen land)
        sprite(m["r"], m["c"], GLYPHS["fancy"]["monster"], GLYPHS["ascii"]["monster"],
               curses.COLOR_RED)

    wbg = cells[lr][lc][3]                              # wizard stands on his terrain
    wcol = _cpair(TERR["wiz"], wbg) | curses.A_BOLD
    put(top + lr, left + lc * 2, g["wizard"] if fancy else g["wizard"] + " ", wcol)

    for sh in arena["shots"]:                           # projectiles on top of all
        rr, cc = int(round(sh["r"])), int(round(sh["c"]))
        if 0 <= rr < TILE_GS and 0 <= cc < TILE_GS:
            if sh["src"] == "mon":
                sym, col = g["shot"], curses.COLOR_RED
            elif sh.get("charged"):
                sym, col = g["cbolt"], curses.COLOR_WHITE   # heavy bolt: bright, piercing
            else:
                sym, col = g["bolt"], curses.COLOR_CYAN
            put(top + rr, left + cc * 2, sym + " ",
                _cpair(col, cells[rr][cc][3]) | curses.A_BOLD)

    def corr_tag(p):  # the wizard can sense the shadow's grip on a power
        c = state["corruption"][p]
        return f"{round(c * 100)}% corrupt ({CORRUPT_BANDS[_corrupt_band(c)][3]})"

    fy = top + TILE_GS + 1
    if enemy is not None:
        head = ("CONTESTED — a battle rages; back a side (each press passes a cycle):"
                if battle is not None else
                "WAR FRONT — back a side to spur the attack (each press passes a cycle):")
        put(fy, 2, head, curses.color_pair(23) | curses.A_BOLD)
        # Spell out which key backs which faction, in that faction's color, with
        # its current standing toward the wizard.
        x = 2
        for key_label, p in (("1", wo), ("2", enemy)):
            seg = f"[{key_label}] aid {POWERS[p][0]} "
            put(fy + 1, x, seg, curses.color_pair(p + 1) | curses.A_BOLD)
            x += len(seg)
            fav = f"(favor {state['favor'][p]}/10)    "
            put(fy + 1, x, fav, curses.A_DIM)
            x += len(fav)
        # The shadow's grip on each combatant.
        x = 2
        for p in (wo, enemy):
            seg = f"{POWERS[p][0]}: {corr_tag(p)}    "
            put(fy + 2, x, seg, curses.color_pair(p + 1) | curses.A_BOLD)
            x += len(seg)
        fy += 2
        # Battle status: progress toward taking the tile, and the turning-the-tide
        # counter so the wizard can see his aid working.
        fy += 1
        if battle is not None:
            w_ = battle["winner"]
            done = 0.0 if battle["tot"] <= 0 else max(0.0, min(1.0, 1 - battle["rem"] / battle["tot"]))
            left = max(1, math.ceil(battle["rem"] / battle["rate"])) if battle["rate"] > 0 else 1
            line = f"⚔ {POWERS[w_][0]} is taking the tile  {_gauge(done, 16, fancy)}  ~{left} cyc"
            resist = battle.get("resist", 0)
            if resist > 0:
                line += f"   · defenders holding {resist}/{AID_FLIP} to turn the tide"
            put(fy, 2, line, curses.color_pair(w_ + 1) | curses.A_BOLD)
        else:
            put(fy, 2, "no battle rages here now — aid [1]/[2] to press the attack",
                curses.A_DIM)
        # Name the legendary items at stake, color-coded by who currently holds them.
        spoils = [it for it in state["items"] if it["owner"] in (wo, enemy)]
        if spoils:
            fy += 1
            x = 2
            put(fy, x, "At stake: ", curses.A_DIM)
            x += 10
            for it in spoils:
                seg = f"{ITEM_TYPES[it['type']]['emoji' if fancy else 'icon']} {ITEM_TYPES[it['type']]['name']}   "
                put(fy, x, seg, curses.color_pair(it["owner"] + 1) | curses.A_BOLD)
                x += len(seg)
    elif town is not None:
        f = town["faction"]
        kind = "capital" if town["capital"] else "village"
        put(fy, 2, f"{town['name']} — {kind} of {POWERS[f][0]}   (favor {state['favor'][f]}/10)",
            curses.color_pair(f + 1) | curses.A_BOLD)
        put(fy + 1, 2, f"this region is {corr_tag(wo)}   ·   walk up to a townsperson and press [e] to greet",
            curses.A_DIM)
        fy += 1
    else:
        put(fy, 2, f"{flavor} {POWERS[wo][0]}  —  they regard you {state['favor'][wo]}/10",
            curses.color_pair(wo + 1) | curses.A_BOLD)
        put(fy + 1, 2, f"this region is {corr_tag(wo)}", curses.A_DIM)
        fy += 1
        if arena["monsters"]:                           # fallen wilderness: warn of minions
            warn = "⚠" if fancy else "!"
            n = len(arena["monsters"])
            put(fy + 1, 2,
                f"{warn} {n} shadow-fiend{'s' if n != 1 else ''} prowl this fallen land — "
                f"[space] to smite, or walk on",
                curses.color_pair(23) | curses.A_BOLD)
            fy += 1

    # Seekers the wizard has out — each a promoted NPC, with journey progress + ETA.
    if state["searches"]:
        fy += 2
        put(fy, 2, "SEEKERS", curses.color_pair(30) | curses.A_BOLD)
        for q in state["searches"]:
            fy += 1
            it = ITEM_TYPES[q["type"]]
            prog = max(0.0, min(100.0, q["prog"]))
            yrs = round(seek_eta(q["fit"], relic_base_cycles(q["type"])) * DAYS_PER_CYCLE / 365.0, 1)
            put(fy, 2, f"#{q['id']} {RACES[q['race']][0]:<8} {it['emoji' if fancy else 'icon']} "
                       f"{it['name']} → {POWERS[q['recipient']][0]}",
                curses.color_pair(q["recipient"] + 1) | curses.A_BOLD)
            put(fy, 46, f"{_gauge(prog / 100, 14, fancy)} {round(prog):3d}%  ~{yrs}yr",
                curses.A_DIM)

    # Recruit modal: read the crowd, then pick a relic-journey. A top overlay (drawn
    # over the arena so it's always on-screen). Shows raw stats and raw journey demands
    # — judging the fit yourself is the skill (no fit meter).
    com = arena.get("commission")
    folk = arena.get("townsfolk") or []
    if com is not None and town is not None and folk:

        def _statline(st):
            return "  ".join(f"{STAT_ABBR[s]}{st[s]:>2}" for s in range(len(STATS)))

        race_i = state["race"][town["faction"]]
        W = 48
        if com["phase"] == "npc":
            rows = ["RECRUIT A SEEKER — press a person's number (other key cancels):", ""]
            rows += [f" [{i + 1}] {RACES[race_i][0]:<9} {_statline(st)}"
                     for i, st in enumerate(folk[:9])]
        else:  # relic phase: chosen seeker shown, compare against each journey's demands
            st = folk[com["npc"]]
            rows = [f"CHOSEN  {RACES[race_i][0]} #{com['npc'] + 1}:  {_statline(st)}",
                    "send on which journey? (match stats to demands):", ""]
            rows += [f" [{i + 1}] {it['name']:<14} demands {_statline(state['relic_demand'][i])}"
                     for i, it in enumerate(ITEM_TYPES)]
        for k, line in enumerate(rows):
            attr = curses.color_pair(30) | curses.A_BOLD if k == 0 else curses.A_REVERSE
            put(5 + k, 4, " " + line.ljust(W) + " ", attr)

    # Character window: a top overlay (drawn over the arena so it's always on-screen,
    # unlike the footer which falls below the fold on short terminals). Set/inspect the
    # wizard's stats with no limits — Endurance scales hearts, the others gate abilities.
    if arena.get("char_open"):
        sel = arena.get("char_sel", 0)
        stats = state["wiz_stats"]
        W = 52
        rows = ["WIZARD — character   (arrows: select & adjust,  c: close)", ""]
        for i in range(len(STATS)):
            label, thresh, wired = WIZ_ABILITIES[i]
            if thresh is None:                       # Endurance → hearts (continuous)
                effect = f"{label}: {stats[i]} hearts"
            else:
                ok = stats[i] >= thresh
                effect = f"{label:<13} {'UNLOCKED' if ok else 'locked':<8} (>= {thresh})"
                if not wired:
                    effect += " [pending]"
            mark = ">" if i == sel else " "
            rows.append(f" {mark} {STATS[i]:<10} {stats[i]:>3}   {effect}")
        for k, line in enumerate(rows):
            body = k >= 2
            sel_row = body and (k - 2) == sel
            attr = (curses.color_pair(30) | curses.A_BOLD if k == 0
                    else curses.A_REVERSE | (curses.A_BOLD if sel_row else 0))
            put(5 + k, 4, " " + line.ljust(W) + " ", attr)

    stdscr.refresh()


def find_music():
    """First MIDI/WAV in ./music (next to this script), or None."""
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "music")
    if not os.path.isdir(d):
        return None
    for f in sorted(os.listdir(d)):
        if f.lower().endswith((".mid", ".midi", ".wav")):
            return os.path.join(d, f)
    return None


def prepare_music():
    """Resolve the music track to a playable WAV (baking a MIDI via chiptune on
    first run; cached thereafter). Prints a one-time notice. Returns a path/None."""
    src = find_music()
    if src is None:
        return None
    if src.lower().endswith(".wav"):
        return src
    cached = os.path.splitext(src)[0] + ".wav"
    if not (os.path.exists(cached) and os.path.getmtime(cached) >= os.path.getmtime(src)):
        print("Rendering chiptune (one-time)…", flush=True)
    return chiptune.render_cached(src)


class Music:
    """Loops a WAV in the background via the system `paplay`/`aplay` (the only audio
    we depend on, and only if present). No-op when no player or no track exists."""
    def __init__(self, wav):
        self.wav = wav
        self.proc = None

    @property
    def on(self):
        return self.proc is not None

    def _player(self):
        for cmd in (["paplay"], ["aplay", "-q"]):
            if shutil.which(cmd[0]):
                return cmd
        return None

    def start(self):
        if self.proc is not None or not self.wav:
            return
        cmd = self._player()
        if cmd is None:
            return
        # Loop forever in its own session so we can kill the whole group on exit;
        # if the player errors (e.g. no audio device) back off so we don't spin.
        loop = "while true; do %s \"$0\" >/dev/null 2>&1 || sleep 2; done" % " ".join(cmd)
        try:
            self.proc = subprocess.Popen(
                ["bash", "-c", loop, self.wav],
                stdin=subprocess.DEVNULL, start_new_session=True)
        except Exception:
            self.proc = None

    def stop(self):
        if self.proc is not None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except Exception:
                pass
            self.proc = None

    def toggle(self):
        self.stop() if self.on else self.start()


def main(stdscr, music_wav=None):
    locale.setlocale(locale.LC_ALL, "")  # enable wide/unicode glyph output
    curses.curs_set(0)
    stdscr.nodelay(True)
    curses.start_color()
    curses.use_default_colors()
    # power text colors (pairs 1..N)
    for i, (_, c) in enumerate(POWERS):
        curses.init_pair(i + 1, c, -1)
    # world-bar backgrounds (pairs 10..)
    for i, (_, c) in enumerate(POWERS):
        curses.init_pair(10 + i, curses.COLOR_BLACK, c)
    # relation status colors (pairs 20..23): allied/peace/enemy/war
    curses.init_pair(20, curses.COLOR_GREEN, -1)
    curses.init_pair(21, curses.COLOR_CYAN, -1)
    curses.init_pair(22, curses.COLOR_YELLOW, -1)
    curses.init_pair(23, curses.COLOR_RED, -1)
    curses.init_pair(30, curses.COLOR_WHITE, -1)  # wizard avatar
    init_terrain()  # pick the terrain palette now that color support is known

    enc = (locale.getpreferredencoding(False) or "").lower()
    glyph_mode = "fancy" if "utf" in enc else "ascii"

    state = fresh_state()
    ctrls = build_controls()
    sel_idx = 0
    playing = False
    speed = 4
    # "tile" is the wizard's lived view (the real game); "map" (macro world) and
    # "controls" (sliders) are dev views, reached by cycling with `m`.
    view = "tile"
    tile_lr, tile_lc = TILE_GS // 2, TILE_GS // 2  # wizard pos inside a tile
    commission = None  # active "send a seeker" modal: None | {"phase","npc"}
    char_open = False  # the wizard's character window (set/inspect his stats); 'c'
    char_sel = 0       # which stat row is selected in the character window
    last_tick = time.monotonic()
    # Realtime tile-arena state (all transient main-loop state, not stored in
    # `state` — see CLAUDE.md's note on the wizard's local arena position).
    facing = (1, 0)                     # last move dir; the wizard's bolts fly this way
    wiz_cool = 0.0                      # seconds until the wizard may fire again
    charging = None                     # None, or seconds the wizard has held space to charge
    space_last = 0.0                    # monotonic time of the last space input (hold detection)
    last_frame = time.monotonic()       # for per-frame physics dt
    tile_enter = time.monotonic()       # wall-clock the wizard stepped onto this tile
    monsters = spawn_monsters(state, state["wizard"]["r"], state["wizard"]["c"])
    # The town crowd's rolled stats (re-rolled whenever he changes macro tile —
    # i.e. each fresh visit; pinned while he stays). Aligned with tile_town npcs.
    last_wiz_tile = (state["wizard"]["r"], state["wizard"]["c"])
    townsfolk = roll_townsfolk(state, *last_wiz_tile)
    shots = []                          # active projectiles: {r, c, dr, dc, src}
    music = Music(music_wav)            # muted by default — toggle with 'p'
    atexit.register(music.stop)         # never leave the player running after exit

    while True:
        now = time.monotonic()
        interval = (1100 - speed * 100) / 1000.0
        # A fresh crowd whenever he sets foot on a different macro tile.
        cur_wiz_tile = (state["wizard"]["r"], state["wizard"]["c"])
        if cur_wiz_tile != last_wiz_tile:
            townsfolk = roll_townsfolk(state, *cur_wiz_tile)
            last_wiz_tile = cur_wiz_tile
        if view == "map":
            draw_world(stdscr, state, playing, speed, glyph_mode)
        elif view == "tile":
            draw_tile(stdscr, state, glyph_mode, tile_lr, tile_lc,
                      {"monsters": monsters, "shots": shots,
                       "enter": tile_enter, "now": now,
                       "commission": commission, "townsfolk": townsfolk,
                       "char_open": char_open, "char_sel": char_sel,
                       "charging": charging})
        else:
            draw(stdscr, state, sel_idx, ctrls, playing, speed, glyph_mode)

        ch = stdscr.getch()
        if ch != -1 and commission is not None:
            # Modal "recruit a seeker": read the crowd -> pick a person -> pick the
            # relic-journey to send them on. The town's own faction receives the find.
            wr, wc = state["wizard"]["r"], state["wizard"]["c"]
            town = state["towns"].get((wr, wc))
            if ord("1") <= ch <= ord("9") and town is not None and townsfolk:
                sel = ch - ord("1")
                if commission["phase"] == "npc" and sel < len(townsfolk):
                    commission = {"phase": "relic", "npc": sel}
                    state["town_msg"] = "On which journey? Press a relic's number (any other key cancels)."
                elif commission["phase"] == "relic" and sel < len(ITEM_TYPES):
                    T, R = sel, town["faction"]
                    held = next((it["owner"] for it in state["items"]
                                 if it["type"] == T and it["owner"] != R), None)
                    stats = townsfolk[commission["npc"]]
                    if held is not None:
                        state["town_msg"] = (f"The {ITEM_TYPES[T]['name']} is already in "
                                             f"{POWERS[held][0]}'s hands — choose another.")
                    elif state["energy"] < SEARCH_ENERGY:
                        state["town_msg"] = f"Too weary to send a seeker (need {SEARCH_ENERGY})."
                        commission = None
                    else:
                        # Dispatch promotes the NPC: snapshot stats, give them an id.
                        state["energy"] -= SEARCH_ENERGY
                        nid = state["next_npc_id"]
                        state["next_npc_id"] += 1
                        fit = journey_fit(stats, state["relic_demand"][T])
                        state["searches"].append(
                            {"id": nid, "type": T, "recipient": R, "race": state["race"][R],
                             "stats": list(stats), "fit": fit, "prog": 0.0})
                        eta = round(seek_eta(fit, relic_base_cycles(T)) * DAYS_PER_CYCLE / 365.0, 1)
                        state["town_msg"] = (f"Seeker #{nid} sets out after the {ITEM_TYPES[T]['name']} "
                                             f"for {POWERS[R][0]} (~{eta} yr at this fit).")
                        commission = None
            else:
                commission = None  # any other key cancels
                state["town_msg"] = None
        elif ch != -1 and char_open:
            # Character window: set the wizard's stats by hand (no limits, testing).
            if ch == curses.KEY_UP:
                char_sel = (char_sel - 1) % len(STATS)
            elif ch == curses.KEY_DOWN:
                char_sel = (char_sel + 1) % len(STATS)
            elif ch in (curses.KEY_RIGHT, ord("+"), ord("=")):
                state["wiz_stats"][char_sel] += 1
            elif ch in (curses.KEY_LEFT, ord("-"), ord("_")):
                state["wiz_stats"][char_sel] = max(0, state["wiz_stats"][char_sel] - 1)
                state["health"] = min(state["health"], wiz_max_hearts(state))  # clamp if Endurance fell
            else:
                char_open = False  # c / esc / any other key closes
        elif ch != -1:
            if ch in (ord("q"), 27):
                music.stop()
                break
            elif ch in (ord("p"), ord("P")):
                if not music.wav:
                    state["town_msg"] = "No music track found in ./music."
                else:
                    music.toggle()
                    state["town_msg"] = "Music on." if music.on else "Music muted."
            elif view == "tile" and ch in (ord("c"), ord("C")):
                char_open = True       # open the wizard's character window
            elif ch == ord("m"):
                # Cycle the three views: the wizard's tile -> dev world map ->
                # dev sliders -> back to the tile.
                view = {"tile": "map", "map": "controls", "controls": "tile"}[view]
                if view == "tile":     # re-entering the arena: fresh clock + minions
                    tile_enter = now
                    monsters = spawn_monsters(state, state["wizard"]["r"], state["wizard"]["c"])
                    shots = []
            elif ch == ord("g"):
                glyph_mode = "ascii" if glyph_mode == "fancy" else "fancy"
            elif ch == ord(" "):
                if view != "tile":
                    playing = not playing
                elif state["health"] <= 0:
                    state["town_msg"] = "Struck down — slumber [z] to recover."
                elif charging is not None:
                    space_last = now             # a held repeat — keep winding the charge up
                elif wiz_cool <= 0:              # press start: fire a basic bolt at once
                    dr, dc = facing
                    shots.append({"r": float(tile_lr + dr), "c": float(tile_lc + dc),
                                  "dr": dr, "dc": dc, "src": "wiz"})
                    wiz_cool = WIZ_FIRE_COOLDOWN
                    space_last = now
                    if state["wiz_stats"][0] >= WIZ_MIGHT_CHARGE_AT:
                        charging = 0.0           # ...and begin charging if the key is held
            elif ch == ord("s"):
                playing = False
                advance_days(state, DAYS_PER_CYCLE)  # step one cycle (10 days)
                tile_enter = now
                monsters = spawn_monsters(state, state["wizard"]["r"], state["wizard"]["c"])
                shots = []
            elif ch == ord("r"):
                playing = False
                state = fresh_state()
                tile_lr, tile_lc = TILE_GS // 2, TILE_GS // 2
                tile_enter = now
                monsters = spawn_monsters(state, state["wizard"]["r"], state["wizard"]["c"])
                shots = []
            elif ch == ord("]"):
                speed = min(10, speed + 1)
            elif ch == ord("["):
                speed = max(1, speed - 1)
            elif view == "map" and ch in (curses.KEY_UP, curses.KEY_DOWN,
                                          curses.KEY_LEFT, curses.KEY_RIGHT):
                # Dev map: a step crosses a whole tile, so 10 days pass (one cycle).
                wiz = state["wizard"]
                dr = -1 if ch == curses.KEY_UP else 1 if ch == curses.KEY_DOWN else 0
                dc = -1 if ch == curses.KEY_LEFT else 1 if ch == curses.KEY_RIGHT else 0
                moved = (wiz["r"], wiz["c"])
                wiz["r"] = max(0, min(GRID - 1, wiz["r"] + dr))
                wiz["c"] = max(0, min(GRID - 1, wiz["c"] + dc))
                if (wiz["r"], wiz["c"]) != moved:
                    state["pedlar_here"] = random.random() < PEDLAR_CHANCE
                    advance_days(state, DAYS_PER_CYCLE)
            elif view == "tile" and ch in (ord("1"), ord("2")):
                # Back a side of the battle on the wizard's tile: boost it for one
                # cycle, which then passes. Keep pressing to keep helping.
                wr, wc = state["wizard"]["r"], state["wizard"]["c"]
                wo = state["owner"][wr][wc]
                enemy = tile_enemy(state, wr, wc)
                if enemy is not None and state["energy"] < AID_ENERGY:
                    state["town_msg"] = "Too exhausted to aid — eat [f] or slumber [z]."
                elif enemy is not None:
                    helped = wo if ch == ord("1") else enemy
                    opp = enemy if ch == ord("1") else wo
                    key = tuple(sorted((wo, enemy)))
                    log = state["aid_log"].setdefault(key, {})
                    log.setdefault("r", [0, 0])[0 if helped == key[0] else 1] += 1
                    state["aid"] = {"helped": helped, "opp": opp}
                    state["energy"] = max(0, state["energy"] - AID_ENERGY)
                    advance_days(state, DAYS_PER_CYCLE)
                    tile_enter = now   # an explicit world-turn — restart the wall clock
            elif view == "tile" and ch in (ord("e"), ord("E")):
                # Greet a nearby townsperson: each gives favor once, and the side
                # warms toward the wizard. Only in a peaceful town.
                wr, wc = state["wizard"]["r"], state["wizard"]["c"]
                town = state["towns"].get((wr, wc))
                if town is not None and tile_enemy(state, wr, wc) is None:
                    f = town["faction"]
                    _, _, _, npcs = tile_town(state, wr, wc)
                    near = next((i for i, (nr, nc) in enumerate(npcs)
                                 if max(abs(nr - tile_lr), abs(nc - tile_lc)) <= 1), None)
                    if near is None:
                        state["town_msg"] = "No one close enough to greet."
                    elif (wr, wc, near) in state["met"]:
                        state["town_msg"] = f"The {POWERS[f][0]} folk nod — you've met."
                    else:
                        state["met"].add((wr, wc, near))
                        state["favor"][f] = min(10, state["favor"][f] + 1)
                        state["town_msg"] = (f"\"Well met, wizard.\" — the {POWERS[f][0]} "
                                             f"warm to you (favor {state['favor'][f]}/10)")
            elif view == "tile" and ch in (ord("f"), ord("F")):
                # Eat a ration to restore energy.
                if state["food"] <= 0:
                    state["town_msg"] = "No food to eat — find a merchant, or slumber [z]."
                elif state["energy"] >= ENERGY_MAX:
                    state["town_msg"] = "Already at full strength."
                else:
                    state["food"] -= 1
                    state["energy"] = min(ENERGY_MAX, state["energy"] + FOOD_ENERGY)
                    state["town_msg"] = (f"You eat. Energy {round(state['energy'])}/100, "
                                         f"{state['food']} food left.")
            elif view == "tile" and ch in (ord("b"), ord("B")):
                # Buy a ration from a town market or a roaming pedlar.
                where = merchant_here(state)
                if where is None:
                    state["town_msg"] = "No merchant here to trade with."
                elif state["gold"] < FOOD_PRICE:
                    state["town_msg"] = f"Not enough gold (a ration costs {FOOD_PRICE}g)."
                else:
                    state["gold"] -= FOOD_PRICE
                    state["food"] += 1
                    seller = "the market" if where == "town" else "the pedlar"
                    state["town_msg"] = (f"Bought a ration from {seller}. "
                                         f"Food {state['food']}, gold {state['gold']}g.")
            elif view == "tile" and ch in (ord("k"), ord("K")):
                # Recruit a seeker: inspect the town's people (their rolled stats),
                # then choose one and the relic-journey to send them on. Only in a
                # peaceful town with a crowd to read.
                wr, wc = state["wizard"]["r"], state["wizard"]["c"]
                if (wr, wc) not in state["towns"] or tile_enemy(state, wr, wc) is not None:
                    state["town_msg"] = "Find a town at peace to recruit a seeker."
                elif state["energy"] < SEARCH_ENERGY:
                    state["town_msg"] = f"Too weary to send a seeker (need {SEARCH_ENERGY})."
                elif not townsfolk:
                    state["town_msg"] = "No one here to send."
                else:
                    commission = {"phase": "npc"}
                    state["town_msg"] = "Read the people — press a number to choose a seeker, any other key to cancel."
            elif view == "tile" and ch in (ord("z"), ord("Z")):
                # Slumber: recover energy AND life to FULL — but the world cycles
                # on while he sleeps. Days = the larger of the two deficits' cost.
                mh = wiz_max_hearts(state)
                if state["energy"] >= ENERGY_MAX and state["health"] >= mh:
                    state["town_msg"] = "Already hale and rested."
                else:
                    days = max((ENERGY_MAX - state["energy"]) * SLUMBER_DAYS_PER_ENERGY,
                               (mh - state["health"]) * SLUMBER_DAYS_PER_HEART)
                    state["energy"] = ENERGY_MAX
                    state["health"] = mh
                    advance_days(state, days)
                    tile_enter = now
                    monsters = spawn_monsters(state, state["wizard"]["r"], state["wizard"]["c"])
                    shots = []
                    state["town_msg"] = (f"You slumber; {days:.0f} days pass and the world "
                                         f"turns without you. Life and energy restored.")
            elif view == "tile" and ch in (curses.KEY_UP, curses.KEY_DOWN,
                                           curses.KEY_LEFT, curses.KEY_RIGHT):
                # Realtime: roaming inside a tile costs no days directly — time is
                # the wall-clock spent here, banked on stepping off into the next
                # macro tile (ceil(seconds / SECONDS_PER_DAY), see the loop tail).
                dr = -1 if ch == curses.KEY_UP else 1 if ch == curses.KEY_DOWN else 0
                dc = -1 if ch == curses.KEY_LEFT else 1 if ch == curses.KEY_RIGHT else 0
                facing = (dr, dc)                      # the wizard now faces this way
                charging = None                        # moving aborts a charged-bolt channel
                if state["health"] <= 0:
                    state["town_msg"] = "Struck down — slumber [z] to recover."
                else:
                    state["town_msg"] = None  # clear any greeting toast on movement
                    nlr, nlc = tile_lr + dr, tile_lc + dc
                    blocked = any(m["r"] == nlr and m["c"] == nlc for m in monsters)
                    if blocked:
                        pass                           # bump a minion — can't walk through it
                    elif 0 <= nlr < TILE_GS and 0 <= nlc < TILE_GS:
                        tile_lr, tile_lc = nlr, nlc    # interior step (no day cost yet)
                    else:
                        wiz = state["wizard"]
                        nr = max(0, min(GRID - 1, wiz["r"] + dr))
                        nc = max(0, min(GRID - 1, wiz["c"] + dc))
                        if (nr, nc) != (wiz["r"], wiz["c"]):  # not blocked by the world edge
                            # Bank the time spent on this tile, then cross over.
                            days = math.ceil((now - tile_enter) / SECONDS_PER_DAY)
                            if days > 0:
                                advance_days(state, days)
                            tile_enter = now
                            wiz["r"], wiz["c"] = nr, nc
                            if nlr < 0:            tile_lr = TILE_GS - 1   # re-enter opposite edge
                            elif nlr >= TILE_GS:   tile_lr = 0
                            if nlc < 0:            tile_lc = TILE_GS - 1
                            elif nlc >= TILE_GS:   tile_lc = 0
                            state["pedlar_here"] = random.random() < PEDLAR_CHANCE
                            monsters = spawn_monsters(state, wiz["r"], wiz["c"])
                            shots = []
            elif view == "controls":
                if ch == curses.KEY_UP:
                    sel_idx = (sel_idx - 1) % len(ctrls)
                elif ch == curses.KEY_DOWN:
                    sel_idx = (sel_idx + 1) % len(ctrls)
                elif ch in (curses.KEY_RIGHT, ord("+"), ord("=")):
                    adjust(state, ctrls[sel_idx], +1)
                elif ch in (curses.KEY_LEFT, ord("-"), ord("_")):
                    adjust(state, ctrls[sel_idx], -1)

        # Realtime arena physics: minions fire down the wizard's row/column, the
        # wizard's bolts fly, and projectiles resolve. Runs every frame in the
        # tile view regardless of input; the macro sim stays frozen here.
        if view == "tile":
            dt = now - last_frame
            wiz_cool = max(0.0, wiz_cool - dt)
            if charging is not None:
                if now - space_last > SPACE_HOLD_GRACE:      # space released (no more held input)
                    if charging >= WIZ_CHARGE_MIN:           # held long enough → loose heavy bolt
                        dr, dc = facing
                        frac = min(1.0, charging / WIZ_CHARGE_FULL)
                        shots.append({"r": float(tile_lr + dr), "c": float(tile_lc + dc),
                                      "dr": dr, "dc": dc, "src": "wiz", "charged": True,
                                      "power": 1 + round(frac * state["wiz_stats"][0])})
                        wiz_cool = WIZ_CHARGE_COOLDOWN
                    charging = None                          # else it was just a tap — discard
                else:                                        # still held — wind it up
                    charging = min(WIZ_CHARGE_FULL, charging + dt)
                    if charging >= WIZ_CHARGE_FULL:          # auto-loose at full charge
                        dr, dc = facing
                        shots.append({"r": float(tile_lr + dr), "c": float(tile_lc + dc),
                                      "dr": dr, "dc": dc, "src": "wiz", "charged": True,
                                      "power": 1 + state["wiz_stats"][0]})
                        charging = None
                        wiz_cool = WIZ_CHARGE_COOLDOWN
            if state["health"] > 0:
                for m in monsters:                      # LOS sentries fire when aligned
                    m["cool"] -= dt
                    if m["cool"] > 0:
                        continue
                    if m["r"] == tile_lr and m["c"] != tile_lc:
                        mdr, mdc = 0, (1 if tile_lc > m["c"] else -1)
                    elif m["c"] == tile_lc and m["r"] != tile_lr:
                        mdr, mdc = (1 if tile_lr > m["r"] else -1), 0
                    else:
                        continue                        # not in line of sight
                    shots.append({"r": float(m["r"]), "c": float(m["c"]),
                                  "dr": mdr, "dc": mdc, "src": "mon"})
                    m["cool"] = MONSTER_FIRE_COOLDOWN
            alive = []
            for sh in shots:                            # advance + resolve projectiles
                if sh["src"] == "mon":
                    spd = MONSTER_PROJ_SPEED
                else:
                    spd = WIZ_CHARGE_PROJ_SPEED if sh.get("charged") else WIZ_PROJ_SPEED
                sh["r"] += sh["dr"] * spd * dt
                sh["c"] += sh["dc"] * spd * dt
                rr, cc = int(round(sh["r"])), int(round(sh["c"]))
                if not (0 <= rr < TILE_GS and 0 <= cc < TILE_GS):
                    continue                            # flew off the arena
                if sh["src"] == "mon":
                    if rr == tile_lr and cc == tile_lc and state["health"] > 0:
                        state["health"] = max(0, state["health"] - 1)
                        if state["health"] == 0:
                            state["town_msg"] = ("Struck down by the shadow's minions — "
                                                 "slumber [z] to recover.")
                        continue                        # spent on the wizard
                else:
                    hit = next((m for m in monsters if m["r"] == rr and m["c"] == cc), None)
                    if hit is not None:
                        monsters.remove(hit)
                        if not sh.get("charged"):
                            continue                    # a basic bolt is spent on one minion
                        # a charged bolt PIERCES — kills this one and flies on
                alive.append(sh)
            shots = alive
        last_frame = now

        if playing and view != "tile" and now - last_tick >= interval:
            advance_days(state, DAYS_PER_CYCLE)  # auto-advance one cycle per tick (dev views)
            last_tick = now

        time.sleep(0.02)


if __name__ == "__main__":
    music_wav = prepare_music()         # bake the chiptune (cached) before curses
    curses.wrapper(main, music_wav)
