"""The pure render core.

``f(model, phases, modes, layers, t) -> frame``, plus the glance frames feedback surfaces read.
No clock, no I/O, no randomness: time arrives as a value, the same inputs always give the same
frame, and ``tests/test_purity.py`` fails the build on any path by which that could stop being
true. This is what makes a recorded trace replayable and a preview identical to the live render.
"""
