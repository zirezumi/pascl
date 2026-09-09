"""The Home Model.

The versioned, hand-authorable contract the runtime reads and the tuning UI writes; the two
never interact directly, and a hand-authored model and a UI-authored one are the same artifact.
Topology is declared once here and never re-encoded in code or in entity names. The engine
branches on fixture capabilities, never on device models, and adapter addresses are opaque to
the core.

Schema v0 lives in :mod:`pascl.model.schema`. Its acceptance test is that a reference home
encodes losslessly (an unknown key is an error, and ``from_dict(to_dict(m)) == m``) and renders
within tolerance with no home-specific code.
"""

from pascl.model.schema import (
    SCHEMA_VERSION,
    HomeModel,
    ModelError,
    consumer_envelopes,
    dumps,
    from_dict,
    load,
    loads,
    to_dict,
    validate,
)

__all__ = [
    "SCHEMA_VERSION",
    "HomeModel",
    "ModelError",
    "consumer_envelopes",
    "dumps",
    "from_dict",
    "load",
    "loads",
    "to_dict",
    "validate",
]
