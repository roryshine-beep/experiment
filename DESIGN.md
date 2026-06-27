# Dominion — Living World Design (future direction)

A forward-looking design sketch. **Nothing here is built.** The current code (`dominion.py` /
`index.html`) is only the **macro sim** described below. No code changes implied by this doc.

## Concept

The slider simulation is a **macro-level** model of a fantasy world: four factions contesting
territory, invisible to the player. The player is a **wizard** living inside that world. The world
is tiled (globe/region). The macro sim is the source of truth; micro detail is generated on demand
when the wizard enters a tile (is there a battle? army sizes, units, are any relics present?), then
cached briefly so revisiting feels consistent. The wizard acts — captures relics, fights on
contested tiles, recruits NPCs — and those impacts feed back up to the macro. Result: a living world
that runs everywhere without him, but that he can dent.

## Guiding principle

**A cheap regenerable world + a small list of the scars the player left on it.**

Two tiers of state, applied to *both* terrain and entities:

- **Regenerable truth** — macro sim + deterministic projection. Disposable; evict and rebuild freely.
- **Sparse override layer** — "the player touched this." Small, durable. The *actual* save data.

Everything the player doesn't affect is projection you can forget and recompute. Everything he does
affect must be promoted (folded into macro, or written to overrides) so it survives cache eviction.

## The two games (the factions play Civ; the gods play *for* the world)

There are **two stacked games**, and seeing them as distinct is the key to the whole thing:

- **The factions play Civilization.** The four powers contest territory, wage war, ally, and reconcile —
  the macro sim, running autonomously whether or not anyone watches. This is the *board*: a world busy
  with its own ambitions, morally neutral, never "about" the player.
- **The wizard and the fallen god play a game against each other, *through* that board.** Two forces —
  the good god's agent (the wizard / the player) and the corrupting entity (the fallen god) — walk the
  factions' world and contest its *fate*, not its borders. The entity is **not another faction**; it is a
  player at a higher table. Its goal is to engulf the world — spread its shadow toward 100%, turn powers
  into loyal vassals, grow its sphere until it owns everything. The wizard's game is to stop it: cleanse,
  rally, keep a light side alive. Neither commands the factions directly; both act *on and through* them —
  the entity by corrupting, the wizard by intervening.

So the factions' Civ-game is the **terrain** of the gods' game. A war between two powers is, to the
factions, the whole world; to the entity, an opening to corrupt a desperate loser; to the wizard, a front
to shore up. The same event reads differently at each table.

**The dark turn: the player may side with the entity.** The wizard is *sent* by the good god, but nothing
forces his allegiance. He can instead feed the shadow — become its hand in the world — and the upper game
inverts: the player and the entity are now *allies* driving toward 100%, and the "opponent" becomes the
dwindling coalition of free powers. Same board, same pieces, opposite goals.

**Why this matters for the build.** It tells us what each layer is *for*: the macro sim must feel like its
own self-interested world (so the gods' game has real stakes to play over), and the entity must be an
**agent with goals** (a fifth actor pursuing world-domination), not a weather system. The wizard's verbs
(corrupt / cleanse, rally / betray) are the *moves* in the upper game; the factions' rise and fall are the
*score*.

**Infer, don't add (parsimony of state).** The same disposability principle applies to the macro's own
variables: prefer deriving behavior from what already exists (territory/tiles, army, hearts, relics)
over adding a new faction variable. A new variable earns its place only if it is **orthogonal** — if it
lets power diverge from what the current variables already determine (a small-but-strong or
large-but-hollow power). One that tracks an existing quantity in lockstep (wealth ∝ army ∝ land) is
redundant; infer it on demand instead. This keeps the "source of truth" as small as the cached world.

## LOD stack

- **Macro** — faction sliders (territory, army, hearts, items). The world's truth.
- **Agent sim** — named/promoted entities (recruits, faction champions) with cheap persistent state,
  running off-screen, feeding deltas to macro like the wizard does.
- **Micro** — concrete tile contents, materialized only when observed.
- **Override layer** — player scars + crystallized entities. The real save.

Unifying idea: **everything is an agent at some LOD; the wizard is the one with a camera attached.**
One agent system drives recruits and faction actors alike — they collide without a second system.

## Key mechanisms

**Deterministic projection.** Tile content = pure `f(macro snapshot, tile_id, seed)`, with RNG seeded
by `hash(tile_id, epoch)`. Same tile + unchanged macro → identical content. Storage optional.

**Epoch-based caching (change-based, not time-based).** A macro *epoch* ticks only on *material*
change to a region (territory crosses a threshold, war starts/ends, relic changes owner). Cache tiles
by `(tile_id, epoch)`. Slider noise doesn't bump the epoch → stable on revisit; real shifts regenerate.
Optional soft time-expiry on top.

**Spatiality (the missing macro piece).** Territory is currently a *percentage*, aspatial. Bind it to
ground: a region/Voronoi map where each power owns a contiguous blob with area ∝ its share; shared
edges are **frontlines**. Then "is there a battle here?" = tile on a border between powers with
`hearts ≤ 3`. Give each relic a *location* so item presence is deterministic per tile.

**Detach / reattach (the two-clock problem).** While the wizard fights a tile in real time, the macro
keeps ticking abstractly. On entry, snapshot and pause that region's macro participation; run the
micro live; on exit compute a delta and merge back. "Detach → play → reattach with results."

**Deferred reconciliation (free movement).** The wizard carries a **pending-delta queue**; impacts
settle into macro when he crosses out of a region (or on a cadence). Start with order-preserving event
replay. A region he's "dirtied" is locked from contradictory macro decisions until its deltas land.
In-transit staleness is acceptable and thematic (news travels slowly).

**Feedback scale.** Tile-level impact on macro `∝ tile's share of the macro quantity` (one tile is a
tiny slice of an army). **Relics are the deliberate exception** — discrete and globally significant,
so seizing/destroying one is a big macro event. Makes "hunt the enemy's relic" the wizard's natural
high-leverage move.

**NPC promotion ladder (attention is the currency).**
1. **Anonymous projection** — generated from seed, never stored. The crowd.
2. **Session-remembered** — on interaction, pinned for the current visit/epoch so they don't morph
   mid-conversation. Discarded on epoch change.
3. **Promoted** — recruitment (or any act of consequence) lifts them out of the projection: snapshot
   attributes, assign a **stable ID**, persist independently of the tile.

Promotion is a **one-way crystallization** — copy seed-generated attributes into the durable record at
that instant, or a later epoch bump silently rewrites your companion. Cost is bounded by player
attention, which is something the player *does*, not a tunable.

**Off-screen missions.** Promoted NPCs on missions are lightweight agents with their own
"mission progress" state, resolving off-screen and feeding macro deltas. Same machinery as the wizard.

## Open hard problems (design deliberately, not as bugs)

- **Reference integrity under a moving world.** A promoted entity references macro things (its faction,
  a relic it carries) that the macro can invalidate while the player is away (e.g. his recruit's faction
  is wiped out). Needs a reconciliation pass with explicit reaction rules — defect, go rogue, mourn —
  so it feels alive, not broken. Same machinery covers the wizard's deferred deltas referencing a region
  or item the macro changed mid-transit.
- **Promotion triggers.** What exactly is an act "of consequence" that promotes an NPC?
- **Watching a dispatched mission.** When the wizard walks over to observe a mission he sent off, the
  abstract mission-progress must reconcile into a concrete materialized scene (two-clock problem again).
- **Anonymous-interaction persistence.** How much does a talked-to-but-not-recruited NPC persist before
  the player feels betrayed by their disappearance?
