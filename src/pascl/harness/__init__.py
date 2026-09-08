"""Golden traces, replay and preview.

A trace records everything the render reads at every tick and every input change, plus the
latent state a replay starts from, plus the outputs. Replay feeds it back through the estimator
and the core and diffs the frames against the recording within a per-attribute tolerance.
Preview is the same pure functions fed synthetic inputs, scrubbing time of day and season
instead of waiting for a sunset. Shadow mode is the same harness pointed at the present.
"""
