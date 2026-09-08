"""The deterministic estimator.

Typed evidence from presence sensors goes in; per-zone presence phases (entering, occupied,
fading, vacant) with time-in-phase come out. Probability never crosses this boundary: the render
consumes named phases only. Time reaches this package through the injected ``pascl.clock.Clock``
and nothing else, so a recorded evidence stream replays to the same phases every time.
"""
