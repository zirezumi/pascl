"""The Home Model.

The versioned, hand-authorable contract the runtime reads and the tuning UI writes; the two
never interact directly, and a hand-authored model and a UI-authored one are the same artifact.
Topology is declared once here and never re-encoded in code or in entity names. The engine
branches on fixture capabilities, never on device models, and nothing in this package names a
transport, a Home Assistant entity or an MQTT topic.

Schema v0 lands here next; its acceptance test is that a reference home encodes losslessly and
renders within tolerance with no home-specific code.
"""
