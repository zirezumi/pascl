# Architecture

The commitments below are the ones that, if broken early, foreclose everything downstream. They
were settled before the first line of engine code and the test suite enforces the ones a test can
reach. Everything else is discovered by building against the reference installation.

## 1. A standalone engine behind adapter boundaries

The engine reproduces a home's lighting from a declarative model, for any home. Home Assistant is
the front door and exactly one adapter: a transport for commands and a source of signals. The core
never sees a Home Assistant entity, an MQTT topic or a device model, so a deployment with no Home
Assistant in it, a professional control plane driven directly, or an appliance all stay possible.

## 2. The Home Model is the hub

One versioned, documented, hand-authorable file describes the home: rooms, zones, fixtures with
capabilities and a calibration reference, sensors mapped to an abstract presence contract,
control surfaces, transports, and the derived facts the engine computes from them. The runtime
reads it and the tuning UI writes it; they never interact directly, and a hand-authored model and
a UI-authored one are the same artifact. Topology is declared once and never re-encoded in code
or in names. The version bumps on any breaking change.

## 3. Three layers, purity as a hard requirement

- **The pure render core** (`pascl.core`): `f(model, phases, modes, layers, t) -> frame`, plus the
  glance frames that feedback surfaces read. No clock, no I/O, no randomness. Time arrives as a
  value.
- **The deterministic estimator** (`pascl.estimator`): typed sensor evidence in, named presence
  phases per zone out (entering, occupied, fading, vacant, with time-in-phase). Probability never
  reaches the render. Time enters only through the injected clock.
- **The impure shell** (`pascl.shell`): transports, the scheduler, the reconciler, the event seam,
  persistence. The only layer that owns the real clock or touches a network.

`pascl.clock` is the seam. `tests/test_purity.py` reads the source of the first two layers and
fails the build on any import, call or name that would let wall time, randomness, I/O or a lower
layer in. The split is also the validation strategy: record a home's signal streams, replay them
through the estimator and the core, and diff the frames against what the home actually did.

## 4. The engine owns its state and is the single writer

State lives in the process and is exposed through a thin contract, never by spraying entities into
a host. The engine is the sole writer to every load it manages. A second writer is the cause of
flicker, fights and drift, so this is a law rather than a feature, and the reconciler treats any
change it did not command as either a human hand or a fault to heal.

## 5. Everything external is an adapter

Transports (Zigbee2MQTT first; others each isolated so the core never changes), presence sources
(an abstract zone-occupancy contract, never a host's binary sensor), and control surfaces. Vendor
knowledge, including the firmware choreography some switches need, lives only in adapters. Each
transport declares a descriptor: throughput ceiling, group semantics, fade support, confirmation
semantics, settle time, and how well it can attribute a change to a hand.

## 6. Local-first, with an empty cloud seam

The engine renders fully offline with no account and no telemetry by default. A clean, unused seam
is left for an optional future control plane so that it is never built in as a dependency and
never omitted as a hook.

## 7. Bring your own orchestration

An external system that sends lighting intent, and an engine that renders it, is a first-class
concept even before the API exists. Intent from outside spawns an override layer exactly as a
hand would: scoped, enveloped, never direct device state.

## 8. The hand always wins

Manual input becomes a layer with a condition-driven envelope, relative to the layer beneath it,
one per scope and channel, that eases back into the render rather than switching it off. Modes
such as sleep are presentation gates over a pipeline that never stops rendering. A scene is a
palette bound to a render group and rendered continuously, colour only; a stored frame is not a
concept here.

## 9. How correctness is established

Golden traces: everything the render reads, recorded at every tick and every input change with
the latent state a replay starts from, replayed in CI and compared within a per-attribute
tolerance. A property suite over synthetic homes unlike the reference one, with must-decline
cases for every learner. Shadow mode as a permanent capability: a room is in exactly one of
OLD (the previous automations drive it), SHADOW (the engine observes and never writes) or ENGINE
(the engine writes), with rollback written and tested before the first flip.
