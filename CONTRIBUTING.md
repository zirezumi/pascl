# Contributing to PASCL

Thank you for looking. A few things to know before you spend time on a change.

## Where the project is

Pre-alpha. The engine is being extracted from a reference installation and validated against
recorded traces of it, subsystem by subsystem, before it drives a light anywhere. Large parts of
the tree are empty on purpose and the interfaces will change. If you want to work on something
substantial, open an issue first so the work is not lost to a refactor that was already underway.

## The Contributor License Agreement

Outside contributions need a one-time signature of the [Individual Contributor License
Agreement](CLA.md). When you open your first pull request a bot will ask; you sign by posting
this exact sentence as a comment on the pull request:

> I have read the CLA Document and I hereby sign the CLA

Your signature is recorded on the `cla-signatures` branch of this repository. You keep the
copyright in your work; the agreement gives the project the rights it needs to distribute it and
to relicense the whole if that is ever necessary.

## Setting up

```
make setup     # uv creates .venv and installs pascl with its dev tools
make check     # ruff, mypy --strict, pytest; the same three gates CI runs
```

Python 3.12 or newer. CI runs the same checks on 3.12 and 3.13 and builds the container.

Two test modules run only against private fixtures that never enter this repository, because
they carry the reference home's coordinates and topology: `tests/test_solar_golden.py` reads
`solar_*.json` snapshots from the directory named by `PASCL_PRIVATE_GOLDEN`, and
`tests/test_model_private.py` reads Home Models from `PASCL_PRIVATE_MODELS`, and
`tests/test_assemble_private.py` assembles recorded exports from `PASCL_PRIVATE_TRACES` under the
binding found beside the models. Without those variables they are skipped, which is what CI does;
the public `examples/` home and binding cover the same code paths.

## Rules the tests enforce

These are architectural laws, not style preferences, and `tests/test_purity.py` fails the build
when one is broken:

- **The render core (`pascl.core`) and the estimator (`pascl.estimator`) are pure.** No wall
  time, no randomness, no I/O, no logging. Time arrives as a value or through the injected
  `pascl.clock.Clock`; `SystemClock` belongs to the shell alone.
- **Layers import downward only.** `model` knows nothing; `core` may import `model`; `estimator`
  may import `core` and `model`; `harness` and `shell` may import anything above them. The core
  must never learn that a transport, an entity or a device model exists.
- **The engine is the single writer** to every load it manages. Nothing else in the process, and
  no adapter, writes to a light behind the reconciler's back.

## Writing code and comments

- Describe what the code does, not how it compares to other code, and put the non-obvious *why*
  in the comment rather than the *what*.
- No version markers, changelog prefixes or counts in docstrings, comments or names; git history
  is the changelog and counts drift.
- Types on every public signature; `mypy --strict` runs on `src`.
- A behaviour change comes with a test; a change to the render or the estimator comes with a
  trace or a property that would have caught the old behaviour.

## Commits and pull requests

Small, single-purpose commits with a subject line that says what changed and a body that says
why. Pull requests target `main`; CI must be green and the CLA signed before review.
