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
    Gamut,
    HomeModel,
    ModelError,
    consumer_envelopes,
    dumps,
    fixture_clip,
    fixture_gamut,
    fixture_model_label,
    from_dict,
    load,
    loads,
    space_rooms,
    to_dict,
    validate,
    with_fixture_gamut,
)

__all__ = [
    "SCHEMA_VERSION",
    "Gamut",
    "HomeModel",
    "ModelError",
    "consumer_envelopes",
    "dumps",
    "fixture_clip",
    "fixture_gamut",
    "fixture_model_label",
    "from_dict",
    "load",
    "loads",
    "space_rooms",
    "to_dict",
    "validate",
    "with_fixture_gamut",
]
