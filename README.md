# PASCL

**Presence-Adaptive Solar Circadian Lighting.** A compositor for the lights in a home.

Every light is rendered continuously as a function of the sun, of who is where, of sleep, and of
what someone has asked for by hand. Manual input is a layer that eases back into the render, not
a mode that switches it off. Colour is a palette that moves with the day. The switches on the wall
stay switches. Nothing needs an app.

## Status

**Pre-alpha. There is no release and nothing here is usable yet.** The engine is being extracted
from a reference installation that has lit one home for about three years, and it is validated
against recorded traces of that installation before it is allowed to drive a single light.
Interfaces will change without notice. When there is something to run, it will ship as a Home
Assistant add-on and as a plain container, and it will be announced.

## What it is

- **An engine** that owns the whole render: a solar clock, per-fixture brightness and colour
  curves, presence phases fused from whatever sensors a home has, sleep as a mode, overrides as
  decaying layers, palettes as living colour, and a reconciler that keeps every light where the
  render put it. It is the single writer to the lights it manages.
- **A Home Model**: one versioned, hand-authorable file that describes rooms, zones, fixtures,
  sensors, control surfaces and transports. The runtime reads it; the tuning UI writes it; the
  engine never re-encodes topology anywhere else.
- **Adapters** for everything external. Home Assistant is the front door and one adapter among
  several; Zigbee2MQTT can be driven directly where fidelity needs the wire. The core never sees
  an entity, a topic or a device model.
- **Local-first.** No account, no cloud, no telemetry by default.

What it is not: a scene editor (a scene here is a rendered atmosphere, never a stored frame), a
notification surface, or anything that needs a phone.

## Architecture

Three layers with purity as a hard requirement, described in [docs/architecture.md](docs/architecture.md):
a pure render core, a deterministic estimator, and an impure shell. Time enters the first two only
through an injected clock, and `tests/test_purity.py` fails the build if that ever changes.

## Developing

Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).

```
make setup     # create .venv and install the package with its dev tools
make check     # ruff, mypy --strict, pytest
make docker    # build the container and run it once
```

## Contributing

Issues and pull requests are welcome, with the caveat that everything is still moving. Read
[CONTRIBUTING.md](CONTRIBUTING.md) first; outside contributions require a one-time signature of the
[Contributor License Agreement](CLA.md).

## Licence

[Apache License 2.0](LICENSE). Copyright 2026 zirezumi.
