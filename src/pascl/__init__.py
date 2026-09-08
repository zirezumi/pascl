"""PASCL: a presence-adaptive, solar-circadian lighting compositor.

Layers, in dependency order. A layer imports only from the layers listed above it, and
``tests/test_purity.py`` enforces the first three rules on every run:

* ``pascl.model``      the Home Model, the versioned hand-authorable contract the runtime reads
* ``pascl.core``       the pure render, ``f(model, phases, modes, layers, t) -> frame``; no clock,
                       no I/O, no randomness
* ``pascl.estimator``  the deterministic estimator, evidence -> presence phases; replayable
* ``pascl.harness``    golden traces, replay and preview: the same pure functions pointed at
                       recorded or synthetic inputs
* ``pascl.shell``      the impure shell: transports, scheduler, reconciler, event seam, persistence

``pascl.clock`` is the one seam through which time reaches the estimator, and the core only ever
receives a timestamp as a value.
"""

from pascl.version import __version__

__all__ = ["__version__"]
