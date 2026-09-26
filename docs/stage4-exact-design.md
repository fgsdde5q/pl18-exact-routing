# Stage 4 exact solver design and feasibility

## Proof model

The search state is exactly `(q, S)`, where `q` is a frozen Stage 2B split-edge
state and `S` is the accumulated 18-bit city mask.  The start label is the
certified start edge with mask `1` and cost zero.  A successor adds the exact
integer turn cost and the exact integer cost of the outgoing split edge, then
unions that edge's model-specific city mask.  A label is a goal immediately
when its mask becomes `(1 << 18) - 1`; no exit from the final region and no
return to Warszawa is added.

Canonical decimal seconds are converted losslessly to integer microseconds.
Values with more precision are rejected.  Queue keys, label costs, dominance,
upper-bound pruning, checkpoint state, lower bounds, and the `UB == LB` proof
test contain integers only.  Seconds are presentation values.

## Admissible lower bounds

The solver takes the maximum of two independent relaxations.

1. **Minimum city-entry edge.** For every missing city `c`, let `e(c)` be the
   minimum nonnegative duration of any split edge carrying bit `c`.  Every
   completion must traverse at least one such outgoing edge, and every road
   transition costs at least that edge's duration.  Therefore
   `max(e(c) for missing c)` cannot exceed the remaining route cost.  Taking a
   maximum is essential because a single edge could carry multiple bits.
2. **Transition-count relaxation.** Let `k` be the largest number of city bits
   on any split edge and `m` the global minimum split-edge duration.  A label
   missing `r` bits requires at least `ceil(r/k)` transitions, each of which
   costs at least `m`; turn costs are nonnegative.  Thus
   `ceil(r/k) * m` is admissible.  It is valid, though weak, when `m = 0`.

The deterministic 4/6/8/10-city gate compares both bounds and their maximum
against exhaustive product Dijkstra and an independent Bellman-Ford product
relaxation.  It also checks the bound at intermediate start states.

## Safe reductions

For one road state, `(q, S1, g1)` dominates `(q, S2, g2)` only when `S1` is a
superset of `S2` and `g1 <= g2`.  Any continuation available at `q` is common
to both labels; unioning the same future masks preserves the superset relation,
and nonnegative identical suffix costs preserve the cost inequality.  Duplicate
states retain only their least cost.  A label is upper-bound-pruned only when
`g + h >= UB`, and only after `UB` is backed by a fully replayed witness.

The certified global lower bound is the minimum integer `f = g + h` in the
durable frontier.  If the frontier is empty it equals a finite incumbent.  A
timeout or chunk limit produces `INCOMPLETE`, never `OPTIMAL`.

## Checkpoint safety

The external solver separates an immutable exact road database from a mutable
SQLite product database.  The latter stores the frontier, labels, parent links,
incumbent history, counters, and a semantic identity containing the model and
all frozen input hashes.  SQLite WAL plus `synchronous=FULL` provides atomic
transactions.  Resume refuses a schema, graph, model, algorithm, heuristic, or
hash mismatch.  The workflow restores checkpoints only from an explicitly
named prior workflow run.

## Incumbent handoff

Committed Stage 3.1 scalar costs are never loaded by the exact solver.  The
recovery command reruns only the historical best actual first-hit order, saves
the returned complete split-edge list, scans the canonical TSV records again,
and independently checks every explicit or implicit transition, exact cost,
mask accumulation, first-hit order, early goal, path hash, and geometry hash.
Only `INCUMBENT_REPLAY_VALID` with `incumbent_enabled=true` can seed pruning.
Otherwise the initial upper bound is infinity.

## Runner feasibility

The frozen graph has 33,640,280 road states and 40,689,675 effective
transitions per model (15,752,983 explicit plus 24,936,692 implicit).  Even one
compact 24-byte record per possible road/mask pair would have a theoretical
upper envelope above 200 TiB; actual dominance should reduce this, but Stage 3
does not provide evidence that it reduces it enough for an ephemeral runner.
The exact graph database, indexes, product labels, checkpoint upload, and a
useful search chunk also compete within GitHub's 360-minute limit.

Consequently a full Poland-18 proof is **not responsibly feasible on the
current GitHub-hosted runner without measured evidence from bounded chunks**.
The workflow permits hosted validation and incumbent recovery, but fail-closes
hosted `exact-chunk`.  Long runs target a persistent self-hosted machine with at
least 32 CPUs, 128 GiB RAM, and 500 GiB NVMe.  This changes storage and runtime
only; it does not change routing semantics or the proof algorithm.

## Independent proof and top-three scope

The independent Bellman-Ford checker is exercised on all mandatory small
instances.  A Poland-18 result must not be certified until an independent
full-product or strict-below-`UB-1s` run agrees.  Terminal-specific optima and
second/third first-hit-order solutions require separate constrained searches;
global-incumbent pruning from the primary run does not certify them.  Until
those runs finish their status remains explicitly incomplete.
