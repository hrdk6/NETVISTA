# NETVISTA – design notes

This document explains how NETVISTA is built and, above all, **why** each decision was
made. Every number quoted here was measured on this machine (Windows 11, WSL2 Ubuntu 24.04,
kernel 6.18) with the code in this repository.

---

## 1. What the system is

NETVISTA is a network *digital twin* with two halves that share one topology file:

* **Live**: a real emulated network (Mininet + Open vSwitch + Linux routers + tc/netem).
  Real packets, real queues, real routing tables. The dashboard shows only measured values.
* **Twin**: a SimPy discrete-event model of the *same* topology, calibrated from the live
  measurements. You change things in the twin first, see the prediction, and then apply the
  same change live to check whether the twin was right (validation page).

```
 Browser (React + Cytoscape + Recharts)
   |  REST: actions          ^  WebSocket: 2 Hz snapshot + events
   v                         |
 FastAPI (backend/netvista/api) ------------------------------------------+
   |                                                                       |
   | Runtime (runtime.py) owns every subsystem                             |
   |   emulation/   Mininet build, tc, iperf3 traffic         (root ns)    |
   |   telemetry/   probe agents (one per namespace) + /proc counters      |
   |   chaos/       fault injection = tc / ip link commands                |
   |   routing/     Dijkstra, Yen, path score, policy-route installer,     |
   |                controller loop (10 Hz)                                |
   |   simulator/   SimPy twin, calibration, what-if  -> separate process  |
   |   validation/  predict -> apply live -> measure -> compare            |
   |   scenarios/   record / replay / scripted demo                        |
   +-----------------------------------------------------------------------+
        |  nsenter -t <pid> -n ...   (one process per command, thread safe)
        v
 Linux network namespaces: c1 c2 | r1..r5 | srv1 srv2      OVS bridges: sw1 sw2 (root ns)
```

## 2. Environment: why WSL2 and not a VM or Docker

* Mininet needs a Linux kernel with network namespaces, veth, `sch_netem`, `sch_htb` and, for
  Open vSwitch, the `openvswitch` module. I checked the WSL2 kernel config (`/proc/config.gz`):
  all of them are present as loadable modules in kernel 6.18, and `modprobe` works.
* **WSL2** therefore runs real Mininet with no VM to manage, and Windows reaches the backend
  through WSL's localhost forwarding (`http://127.0.0.1:8000`).
* **Docker** was rejected for the emulator: Mininet in a container needs `--privileged`, the
  host's kernel modules and its own OVS daemon. On Docker Desktop that is fragile, and it adds
  nothing over WSL. One run script (`scripts/run.ps1` / `scripts/run.sh`) is simpler.
* The backend must run as root (namespaces, qdiscs). `run.ps1` uses `wsl -u root`.

## 3. Topology and addressing

`topologies/default.json`: two sites joined by a 5-router core.

```
                  ┌──── r2 ─────────────┐
 c1 ┐             │      │              │
    ├ sw1 ── r1 ──┤      r4 ─────────── r5 ── sw2 ┬ srv1
 c2 ┘             │      │              │         └ srv2
                  └──── r3 ─────────────┘
 core links (one-way delay / capacity):
   r1-r2 5 ms/50   r1-r3 8 ms/50   r2-r4 3 ms/50   r3-r4 3 ms/50
   r2-r5 5 ms/30 (bottleneck)      r3-r5 8 ms/50   r4-r5 4 ms/50
```

Six loop-free core paths exist between r1 and r5, so every failure has an alternative.
The shortest path (r1-r2-r5, 10 ms) has the smallest capacity, so it is the first to congest:
that makes static and adaptive routing behave visibly differently.

The **addressing plan is derived, not configured** (`topology/addressing.py`), so a new
topology file needs no IP bookkeeping:

* each switch is a LAN `10.0.k.0/24` (gateway router `.1`, hosts `.11, .12, …`)
* each router-router link is `10.10.m.0/24` (`.1` / `.2`)
* interfaces are `<node>-eth<i>` (≤ 15 characters, enforced by validation)

Validation rejects disconnected graphs, hosts with ≠ 1 link, switches with ≠ 1 gateway, etc.

## 4. Emulation (Phase 1)

* **Routers** are Mininet hosts with `ip_forward=1` (Linux routers). **Site switches** are Open
  vSwitch bridges in `standalone` mode (MAC-learning, no OpenFlow controller). **Clients and
  servers** are Mininet hosts.
* After start-up NETVISTA never calls Mininet's `node.cmd()` again, because it drives one shell
  per node and is not thread-safe. All later commands use `nsenter -t <pid> -n`, one process per
  command (`emulation/nsexec.py`).
* **Shaping** – both ends of every link get the same egress tree (`emulation/tc.py`):

  ```
  root htb 1: ── class 1:1 rate B ceil B ── qdisc netem: delay D [jitter J] loss L% limit 1000
  ```

  Each direction is shaped on its sender's egress, so a link is symmetric. Bandwidth is HTB,
  delay/loss are netem, and netem's `limit` is the link buffer.
  *Finding:* HTB's root qdisc does **not** support in-place change (`tc qdisc replace … root htb`
  fails the second time with "Change operation not supported"). The first live failure-revert
  hit this. Updates now rewrite only the HTB class and the netem child (both support change),
  with a rebuild fallback.
* **Traffic** – iperf3 **UDP constant bit rate** (1200-byte payload). UDP CBR was chosen on
  purpose: its offered load is known exactly, so the twin can be given the identical demand.
  TCP would adapt its rate and make predictions circular. Receiver reports (1 s intervals) give
  per-flow throughput, loss and jitter.

## 5. Telemetry

Two independent sources, both real:

1. **Probe agents** (`telemetry/probe_agent.py`, stdlib only) run *inside* every namespace.
   Each echoes UDP probes on port 47000 and sends one 64-byte probe per 100 ms to its targets:
   * `L:<link>`  router → neighbour router (one stream per core link)
   * `G:<host>`  host → its gateway (covers the access segment)
   * `F:c>s`     every client → every server (the end-to-end path of each managed flow)

   RTT is measured with the agent's own monotonic clock, so no clock sync is needed. A probe
   with no echo after 2 s is counted as lost. This is a BFD-like liveness mechanism.
2. **Counters** – `/proc/<pid>/net/dev` shows the counters of the namespace that `<pid>` lives
   in, so one file read per namespace gives bytes and packets for every veth, twice a second,
   with no subprocess. Queue drops and backlog come from `tc -s -j qdisc show`. The HTB root
   already includes its children's drops, so only the root is counted, to avoid double counting.

**Health is always measured.** A link is "down" because its probes stopped coming back, not
because the chaos engine says it injected a fault. That is what makes "failure detection time"
a meaningful number.

* down: no echo for `max(1.2 s, 3 × smoothed RTT)` (the dead interval)
* degraded: probe loss ≥ 1 %, utilisation ≥ 85 %, or RTT > 1.5 × designed + 2 ms
* the access-segment caveat: switch ports have no IP, so an L2 segment is covered by the
  host→gateway probe that crosses it. The UI says so ("Probe: c1 → gateway r1").

## 6. Chaos lab (Phase 2)

| UI action      | Real Linux action                                                       |
|----------------|-------------------------------------------------------------------------|
| add latency    | netem `delay base+X ms [jitter]` on both link ends                      |
| packet loss    | netem `loss X%` on both ends                                            |
| bandwidth cap  | HTB class `rate X mbit`                                                 |
| link down      | `ip link set … down` on **both** ends (cable pulled)                    |
| node down      | every interface **of that node** down; neighbours only lose carrier     |
| traffic burst  | an extra iperf3 UDP flow for N seconds                                  |

Link parameters are composed as base + at most one active override per field, so reverting
latency never disturbs a loss fault on the same link. Interface state is tracked per
interface, so overlapping link-down and node-down faults revert correctly. Every action and
revert goes into the event log (also `runs/events.jsonl`).

## 7. Routing (Phase 3)

### 7.1 Algorithms (own implementations)

* `routing/dijkstra.py` – binary-heap Dijkstra written from scratch, with deterministic
  tie-breaking (lexicographically smallest path) so static routes are reproducible.
  networkx is used only as the graph container.
* `routing/yen.py` – Yen's K-shortest loop-free paths on top of it.
* Both are tested against networkx's reference implementations on random graphs
  (`tests/test_routing_algorithms.py`).

### 7.2 Why "candidates + score" instead of Dijkstra on a composite weight

```
score = w_latency · latency_ms + w_loss · loss_% + w_util · util_%      (defaults 1, 5, 0.2)
  latency = Σ measured one-way latency (probe RTT/2, 2 s window)
  loss    = 1 − Π(1 − p_i)              (end-to-end loss of independent links)
  util    = max projected utilisation   (the bottleneck)
```

Loss compounds multiplicatively and utilisation is a *max*, so the score is not an additive edge
weight and Dijkstra cannot optimise it. Instead Yen enumerates the K = 8 shortest candidates by
configured delay (6 exist here), and each candidate is scored from live telemetry every second.
All terms are in millisecond-equivalents: 1 % loss costs as much as 5 ms, and 10 % more load
costs 2 ms.

### 7.3 Three anti-flapping mechanisms (all motivated by observed behaviour)

1. **Projected utilisation.** For each hop: `(measured rate − own rate if already there + own
   rate) / capacity`. Without it, a flow sees its own traffic on the current path, moves away,
   sees the old path empty, and moves back.
2. **Hysteresis (15 %) and hold-down (3 s)** for voluntary switches. A dead link bypasses both
   (immediate fail-over).
3. **Suspect links.** *Finding from the validation suite:* when router r2 crashed, during the
   1.2 s detection window its links looked **idle** on the counters (a veth whose peer is down
   drops without counting tx bytes), so the load-aware controller moved idle flows *into* the
   dead router. Now any link whose probes have been silent for > 0.5 s cannot be *chosen*. It
   never forces the current path off, though, because a +500 ms latency step also causes a
   short silence. After this fix the router-crash scenario matches the twin 100 %.

### 7.4 Installing paths: Linux policy routes, not OVS flows

The routers are Linux namespaces doing real IP forwarding, so routes are the native mechanism:
visible with `ip route`, shown hop by hop by `traceroute`, and needing no OpenFlow controller
(Ryu/OS-Ken are unmaintained on Python 3.12). OVS stays where it fits, as the site L2 switches.

Per-flow paths use **source+destination policy routing** (`routing/installer.py`):

```
ip rule  add from 10.0.1.11/32 to 10.0.2.11/32 lookup 100      (every router, once)
ip route replace 10.0.2.11/32 via <next hop> table 100          (routers on the path)
```

The reverse direction gets table 101 with the reversed path, so RTT probes measure one path.
Everything else (router probes, traceroute replies) uses the main table, which holds
shortest-path routes to every subnet that avoid dead links in adaptive mode.

**Make-before-break:** forward routes are written from the destination back towards the
source, so the first-hop router switches last. A **reconcile** pass every 2 s re-asserts every
route idempotently. That matters because the kernel deletes routes whose interface goes down,
and they must come back after a repair (this is how static mode recovers).

### 7.5 Timing definitions (shown per incident)

* **detection** = link declared dead − fault injected (≈ dead interval 1.2 s + ≤ 100 ms tick)
* **reroute**   = new routes installed − detection (path computation + `ip -batch` writes)
* **recovery**  = first end-to-end probe echo on the new path − fault injected

Measured on this machine (link r2-r5 down, adaptive): detection 1.31–1.37 s, reroute
14–25 ms, recovery 1.43–1.49 s. Router r2 crash: detection 1.21–1.36 s, reroute 13–56 ms
(flows are rerouted one after another), recovery 1.41–1.56 s.

## 8. Digital twin (Phase 4)

### 8.1 Model (`simulator/model.py`)

Packet-level SimPy model. Each link direction mirrors the tc pipeline exactly:

```
arrival → netem loss (Bernoulli) → buffer check (limit 1000 pkts) → delay D (+jitter) → HTB token bucket (rate R, 1600 B) → next hop
```

* Loss is drawn before the buffer check, the order `netem_enqueue()` uses.
* The buffer counts packets in delay *and* waiting for the shaper, like netem's `limit` under
  an HTB parent.
* **Token bucket, not a FIFO server.** *Finding:* the first version served packets at `size/R`
  each, Lindley-style, and over-predicted RTT by ~1 ms. HTB is a token bucket with a 1600-byte
  burst (`tc class show` reports `burst 1600b`), so a small probe right behind a data packet
  leaves immediately. Modelling the bucket cut latency error from ~1.5 % to ~0.2 %.
* **Sources:** CBR flows (1242 B on the wire = 1200 + UDP 8 + IP 20 + Ethernet 14; HTB
  accounts the Ethernet header) and the same 10 Hz probes as the live agents.
  *Finding:* perfectly periodic sources phase-lock at a full tail-drop queue. Two identical
  flows got 11 % and 60 % loss in the twin, while live they were roughly equal. Real senders are
  not perfectly periodic (iperf3 paces on a 1 ms timer, plus OS wake-up noise), so the twin adds
  a mean-preserving ±30 % jitter to each inter-departure gap. That restored a fair split.
* Everything uses one seeded RNG, so a prediction is reproducible.

### 8.2 Calibration (`simulator/calibration.py`)

The structural model knows propagation, shaping, queueing and loss. It cannot know the
emulator's own overhead (namespace forwarding, veth wake-ups, the agent's Python), so that is
learned from live data:

1. run the twin of the **current** state (same links, traffic, paths) with zero overhead,
   simulating one stream for every live probe stream
2. `residual_i = live median RTT_i − simulated median RTT_i` (queueing cancels out)
3. least squares `residual = E + H · 2n_i` over streams of different lengths (n = 1, 2, 6, 7
   links) to separate endpoint overhead E from per-hop overhead H
4. keep the live jitter around each end-to-end stream's median as an empirical noise
   distribution, added to every simulated probe RTT

Typical fit on this machine: E ≈ 0.04–0.09 ms, H ≈ 0.06–0.08 ms, RMS ≈ 0.06–0.08 ms.
*Finding:* overhead depends on load (the CPU idles less), so the validation job re-calibrates
after its traffic warm-up, in the same regime it then measures.

### 8.3 What-if and routing prediction (`simulator/whatif.py`)

The what-if config is built from the live state: effective tc parameters, the running iperf3
demand, current paths and candidates. Your changes are applied to that copy only. In adaptive
mode the twin also predicts what the controller will do: it runs a 6 s pilot simulation, feeds
predicted per-link latency, loss and rates into **the same `routing/scoring.py` code** the live
controller runs, re-routes, and repeats until the paths are stable (≤ 3 rounds).

The twin runs in a **separate process** (ProcessPoolExecutor, spawn). A 1–3 s CPU-bound
simulation inside the backend would hold the GIL, delay probe processing and could make healthy
links look dead.

## 9. Validation methodology and results

For each scenario (`validation/runner.py`):

1. **Predict.** Freeze the twin's prediction before anything is touched.
2. **Apply** the same change live through the chaos engine.
3. **Settle** 8 s (reroute and queue fill).
4. **Measure** 12 s of live probes and iperf3 reports.
5. **Revert**, then cool down for 10 s.
6. **Compare.**

Error definitions:

* latency (RTT p50/p95/p99) and throughput: relative error |pred − meas| / meas
* loss: absolute error in percentage points (relative error is meaningless when loss ≈ 0)
* path: did the twin predict the path the live controller chose?

The suite runs each impairment in **static** mode, where flows stay on the impaired link and
the queueing/loss model is tested, and in **adaptive** mode, where the controller reacts and
the twin's *routing prediction* is tested.

**Final suite on this machine** (11 scenarios, default topology, 2 × 6 Mbit/s UDP flows,
settle 8 s, measure 12 s, twin simulated 14 s per scenario):

| # | Scenario | Routing | RTT p50 err | RTT p95 err | Throughput err (per flow) | Bottleneck total err | Loss err (per flow) | Paths |
|---|---|---|---|---|---|---|---|---|
| 1 | Baseline (no change) | adaptive | 0.12 % | 2.24 % | 0.39 % | 0.30 % | 0.00 pp | 100 % |
| 2 | +40 ms latency on r2-r5 | static | 0.04 % | 0.55 % | 0.07 % | 0.00 % | 0.00 pp | 100 % |
| 3 | +40 ms latency on r2-r5 | adaptive | 0.21 % | 1.79 % | 0.10 % | 0.10 % | 0.00 pp | 100 % |
| 4 | 5% loss on r2-r5 | static | 0.05 % | 1.66 % | 0.58 % | 0.01 % | 0.36 pp | 100 % |
| 5 | 5% loss on r2-r5 | adaptive | 0.17 % | 1.77 % | 0.10 % | 0.10 % | 0.00 pp | 100 % |
| 6 | Bandwidth cap 14 Mbit/s on r2-r5 (89 % load) | static | 0.12 % | 2.70 % | 0.25 % | 0.25 % | 0.00 pp | 100 % |
| 7 | Bandwidth cap 8 Mbit/s on r2-r5 (overload) | static | 0.64 % | 0.24 % | 3.50 % | 0.03 % | 2.27 pp | 100 % |
| 8 | Burst 20 Mbit/s c2→srv1 (overload) | static | 0.30 % | 0.16 % | 6.32 % | 0.02 % | 6.10 pp | 100 % |
| 9 | Burst 20 Mbit/s c2→srv1 | adaptive | 0.33 % | 2.70 % | 0.06 % | 0.04 % | 0.00 pp | 100 % |
| 10 | Link r2-r5 down | adaptive | 0.15 % | 1.64 % | 0.10 % | 0.10 % | 0.00 pp | 100 % |
| 11 | Router r2 down | adaptive | 0.12 % | 1.30 % | 0.20 % | 0.03 % | 0.00 pp | 100 % |
| | **mean** | | **0.21 %** | **1.52 %** | **1.06 %** | **0.09 %** | **0.79 pp** | **100 %** |

Example (row 7): a full 1000-packet buffer gives a live RTT p50 of 1242.8 ms, against 1236.2 ms
predicted. Live goodput is 3.99 + 3.73 = 7.72 Mbit/s, against 3.86 + 3.86 = 7.72 predicted.
Live aggregate loss is 35.67 %, against 35.63 % predicted.

How the twin improved over three iterations (the suite run before the jitter fix is kept in
`runs/archive/validation_runs_before_jitter.jsonl`; the FIFO-era figure comes from the first
single validation run, recorded in the development log):

| Model version | RTT p50 err | 8 Mbit/s overload, per-flow throughput err |
|---|---|---|
| FIFO server, idle-time calibration | ≈ 4–5 % | n/a (not yet in suite) |
| + token bucket, calibration under load | ≈ 0.2 % | 50 % (phase-locked sources) |
| + source timing jitter | ≈ 0.2 % | 3.5 % |

**What the twin gets right:** median latency to within about 0.2 %, including a full
1000-packet buffer (≈ 1.24 s of queueing delay, predicted within 0.6 %); the bottleneck's total
goodput and aggregate loss; and which path the adaptive controller picks, in every scenario.

**What it cannot do (and why):** under heavy tail-drop overload, *which* flow loses the packets
depends on packet micro-timing: NAPI batches, timer phases, iperf3's pacing timer. Live, small
forwarded probes lost 85 % while data lost 30 % and locally generated probes 35 % (measured with
ping and the agents in the same window). In row 8 the live split was 0.2 % / 13.6 % / 5.0 %
loss across three flows the twin treats fairly (≈ 9.4 % each). No queueing model sees that, so
per-flow loss under overload carries errors of a few percentage points while the aggregate is
exact. The validation page reports both. Probe-loss rows show the same effect even more
strongly, so they are reported but excluded from the headline numbers. p95 errors (0.2–2.7 %)
are dominated by WSL timer noise in the tail.

## 10. Frontend

* **No fake data.** Every value on the live pages comes from the WebSocket snapshot. The twin's
  view is labelled "Simulation" with a dashed border. Nothing in the UI is mocked, so there is
  no MOCK label anywhere.
* **Packet dots:** the dot *rate* is the measured packets/s of each link direction (interface
  counters), scaled so the busiest direction shows about 14 dots/s. The legend shows "1 dot ≈ N
  packets". A dot's *colour* is attributed to the flows whose installed path crosses that hop,
  in proportion to their known offered rates. Anything beyond that (probes, control traffic) is
  grey.
* **Colour system:** hue is reserved for meaning. Status uses green/amber/red, always with an
  icon and a label. Flow identity uses TIA-598 fibre colours (#1 blue, #12 aqua, #10 violet, #4
  brown), validated with a CVD checker on the dark surface. They sit in the 6–8 ΔE warn band, so
  each flow also has a dash pattern and a direct label. Link load uses a neutral brightness and
  width ramp, so it never competes with status hues.
* Fonts (Barlow) are bundled locally, so the demo works offline.

## 11. Concurrency model

* asyncio (FastAPI) serves HTTP and pushes one snapshot every 0.5 s to all WebSocket clients.
* Daemon threads: one reader per probe agent, counters (2 Hz), tc stats (1 Hz), routing
  controller (10 Hz), history sampler (1 Hz), traffic watcher, and the validation/demo/replay
  jobs.
* The twin runs in a separate process.
* Producers never touch asyncio. The broadcaster pulls events by sequence number from a
  thread-safe ring buffer.

## 12. Tests

* `tests/test_routing_algorithms.py` – Dijkstra and Yen vs networkx on random graphs,
  determinism, dead-edge avoidance, the six default-topology paths
* `tests/test_scoring.py` – score formula, infeasibility, hysteresis, hold-down vs fail-over,
  weights
* `tests/test_simulator.py` – closed-form checks (propagation + serialisation, Bernoulli loss,
  overload goodput = capacity, buffer-full queueing delay), token bucket, reproducibility,
  twin fail-over / outage / latency-reroute prediction, calibration fit recovery
* `tests/test_topology_and_parsers.py` – topology validation, addressing, tc/iperf/proc
  parsers, policy-route generation, change validation, validation error maths
* `tests/test_smoke_emulation.py` (root only) – boots real Mininet: netem delay is really
  applied, a latency change moves the measured RTT, policy-route fail-over, traceroute goes
  through the expected router, the OVS-based default topology passes traffic
* `scripts/live_check.py` – end-to-end acceptance test against the running system
* `scripts/twin_check.py --suite` – calibration plus the full validation suite

## 13. Known limitations (honest list)

1. **Scale:** designed for tens of nodes. Every probe stream is a Python thread reading a
   pipe, and the twin is packet-level (about 1 s of compute per simulated 10 s at 12 Mbit/s).
2. **Probe resolution:** liveness is probe-based at 10 Hz with a 1.2 s dead interval, so
   detection is ~1.3 s. That is BFD-like, not sub-second. Lower values made 500 ms latency
   steps look like failures.
3. **Event timestamps** use the backend's receive time, with ~100 ms granularity (the agent
   batches results per tick).
4. **L2 segments** (host ↔ switch ↔ router) are probed only as a whole, by the host→gateway
   probe.
5. **Per-flow overload split** is not predictable (section 9). The twin models fair tail-drop,
   reality is timing-dependent.
6. **The twin predicts steady state.** It does not model the transient during detection, or
   path choices that depend on history among near-tied candidates (hysteresis).
7. **UDP CBR traffic only.** TCP congestion control is not modelled.
8. **WSL timing:** RTTs include WSL's virtualised timer behaviour. Calibration absorbs the
   mean, but jitter on a busy laptop can be higher than on bare metal.
9. **Optional extensions not built:** eBPF/XDP telemetry, the LLM "explain this router" panel
   and the drag-and-drop topology editor (left out by design, to keep the core solid).

## 14. Likely viva questions

* *Why not OpenFlow/SDN?* Linux routers plus policy routes are native, inspectable (`ip route`,
  `traceroute`) and need no unmaintained controller. OVS is still used, for L2.
* *Is the animation real?* Rates are real counters. Colour attribution uses known offered
  rates. Grey dots are traffic that isn't a flow.
* *How do you know detection time is real?* Health comes only from probe silence. The injection
  timestamp and the detection timestamp come from the same process clock.
* *Why does the twin need calibration?* It knows the physics (delay, shaping, queueing, loss)
  but not the emulator's processing overhead. That is fitted as E + H·2n from live data, using
  streams of different lengths to separate the two terms.
* *Why relative error for latency but absolute for loss?* Relative error explodes when the true
  value is ~0.
* *What surprised you?* Four things changed the design: HTB root changes failing, the token
  bucket, phase-locked CBR sources, and black-hole attraction during detection. Each is
  documented above, with the measurement that revealed it.
