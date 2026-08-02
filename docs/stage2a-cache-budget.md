# Stage 2A cache budget decision v2

Decision ID: `stage2a-metadata-cache-budget-v2`.

The canonical metadata checkpoint produced by run `30384412281` was
`932689698` bytes. The previous `900000000` byte limit rejected that valid
checkpoint after both canonical exports had completed.

The pinned metadata checkpoint limit is therefore `950000000` bytes. This is a
targeted increase of `50000000` bytes and leaves more than `500000000` bytes
below GitHub's default 10 GB repository cache limit after accounting for the
frozen PBF, vcpkg, ccache, OSRM checkpoint, metadata checkpoint, and compact
certificate cache.

`scripts/test_stage2a_cache_contract.py` enforces both the pinned metadata limit
and the aggregate repository-cache margin. The workflow independently rejects
any produced archive larger than the pinned limit.

This changes only the versioned infrastructure storage ceiling. It does not
change frozen manifest v2, routing profile semantics, the PBF or PRG inputs,
the OSRM commit, graph eligibility, turn costs, or any solver input.
