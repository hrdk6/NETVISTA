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
* **AI layer** over both: learned-baseline anomaly detection, root-cause analysis by probe-path
  tomography, and an LLM copilot that answers from live data through tools and can only
  *propose* changes (section 10).
* **Assure** on top: the operator states *intents* (SLOs, policies, "must survive any single
  failure"); they are checked every second, the twin predicts which would break after every
  possible single failure, a planner computes routes and pre-checked backups that keep them,
  and every deployment and drill measures whether the prediction was right (section 11).
* **Designer**: draw a topology in the browser, check it, and boot it as real Linux routers
  (section 12).

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
   |   ai/          anomaly detector + root cause (1 Hz), copilot (LLM),   |
   |                evaluation against real faults                         |
   |   assure/      intents (1 Hz), failure analysis, planner, autopilot,  |
   |                drills, live benchmark, prediction intervals           |
   |   topology/    library + design checks (designer page), redeploy      |
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

* down: no echo for `max(1.2 s, 3 × smoothed RTT)` (the dead interval), measured on the
  agents' clock up to their latest report, so a late report (backend busy) delays a verdict
  instead of faking one
* host stalls: when at least 80 % of *all* probe streams go mute (> 0.5 s) at the same moment,
  the cause is the measurement host, not the network. The worst single failure on the default
  topology silences 8 of 15 streams; a stall silences all of them. Time inside a stall is not
  counted towards any stream's silence, and probes in flight during it are left out of the RTT
  and loss statistics (counted, in `host_stalls` of the snapshot, and logged as
  `telemetry.stall`). Total silence longer than 4 s is judged as a real outage. Section 11.10
  says where the stalls came from.
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
settle 8 s, measure 12 s, twin simulated 14 s per scenario; re-run with the current code: herd
guard on, and every scenario now also scored for the fluid model of section 11.3, shown after
the slash):

| # | Scenario | Routing | RTT p50 err packet / fluid | RTT p95 err packet / fluid | Throughput err (per flow) | Bottleneck total err packet / fluid | Loss err (per flow) | Paths |
|---|---|---|---|---|---|---|---|---|
| 1 | Baseline (no change) | adaptive | 0.25 / 0.25 % | 0.37 / 0.49 % | 0.21 % | 0.19 / 0.00 % | 0.00 pp | 100 % |
| 2 | +40 ms latency on r2-r5 | static | 0.04 / 0.13 % | 0.08 / 0.28 % | 0.05 % | 0.05 / 0.00 % | 0.00 pp | 100 % |
| 3 | +40 ms latency on r2-r5 | adaptive | 0.10 / 0.23 % | 0.25 / 0.42 % | 0.35 % | 0.35 / 0.00 % | 0.00 pp | 100 % |
| 4 | 5% loss on r2-r5 | static | 0.16 / 0.62 % | 0.30 / 1.03 % | 0.18 % | 0.16 / 0.11 % | 0.05 pp | 100 % |
| 5 | 5% loss on r2-r5 | adaptive | 0.08 / 0.21 % | 0.16 / 0.33 % | 0.35 % | 0.35 / 0.00 % | 0.00 pp | 100 % |
| 6 | Bandwidth cap 14 Mbit/s on r2-r5 (89 % load) | static | 0.24 / 1.17 % | 0.58 / 2.59 % | 0.33 % | 0.04 / 0.00 % | 0.00 pp | 100 % |
| 7 | Bandwidth cap 8 Mbit/s on r2-r5 (overload) | static | 0.74 / 2.10 % | 0.36 / 1.35 % | 11.96 % | 0.00 / 0.13 % | 7.65 pp | 100 % |
| 8 | Burst 20 Mbit/s c2→srv1 (overload) | static | 0.29 / 0.69 % | 1.77 / 1.04 % | 7.72 % | 0.19 / 0.26 % | 7.37 pp | 100 % |
| 9 | Burst 20 Mbit/s c2→srv1 | adaptive | 0.03 / 0.43 % | 0.12 / 0.89 % | 0.09 % | 0.05 / 0.00 % | 0.00 pp | 100 % |
| 10 | Link r2-r5 down | adaptive | 0.40 / 0.27 % | 3.12 / 2.98 % | 0.35 % | 0.35 / 0.00 % | 0.00 pp | 100 % |
| 11 | Router r2 down | adaptive | 0.15 / 0.33 % | 0.27 / 0.51 % | 0.08 % | 0.08 / 0.00 % | 0.00 pp | 100 % |
| | **mean** | | **0.23 / 0.59 %** | **0.67 / 1.08 %** | **1.97 %** | **0.16 / 0.05 %** | **1.37 pp** | **100 %** |

Paths: 100 % for both engines. The fluid model's per-flow throughput and loss errors (1.75 %,
1.34 pp) match the packet twin's, because both are dominated by the overload split below. Time
per prediction: packet twin 0.24-1.46 s, fluid model 0.2-2.2 ms.

Example (row 7): a full 1000-packet buffer gives a live RTT p50 of 1244.2 ms, against 1235.4 ms
predicted by the packet twin and 1261.4 ms by the fluid model. Live goodput is 3.42 + 4.30 =
7.72 Mbit/s, against 3.88 + 3.84 = 7.72 predicted. Live aggregate loss is 35.66 %, against
35.71 % predicted. The run before this one (kept in `runs/validation_runs.jsonl`) split the
same overload 3.99 / 3.73: the per-flow split changes from run to run, the total does not.

How the twin improved over three iterations (the suite run before the jitter fix is kept in
`runs/archive/validation_runs_before_jitter.jsonl`; the FIFO-era figure comes from the first
single validation run, recorded in the development log):

| Model version | RTT p50 err | 8 Mbit/s overload, per-flow throughput err |
|---|---|---|
| FIFO server, idle-time calibration | ≈ 4–5 % | n/a (not yet in suite) |
| + token bucket, calibration under load | ≈ 0.2 % | 50 % (phase-locked sources) |
| + source timing jitter | ≈ 0.2 % | 3.5 % |

**What the twin gets right:** median latency to within about 0.2 %, including a full
1000-packet buffer (≈ 1.24 s of queueing delay, predicted within 0.7 %); the bottleneck's total
goodput and aggregate loss; and which path the adaptive controller picks, in every scenario.

**What it cannot do (and why):** under heavy tail-drop overload, *which* flow loses the packets
depends on packet micro-timing: NAPI batches, timer phases, iperf3's pacing timer. Live, small
forwarded probes lost 85 % while data lost 30 % and locally generated probes 35 % (measured with
ping and the agents in the same window). In row 8 the live split was 0.2 % / 13.6 % / 5.0 %
loss across three flows the twin treats fairly (≈ 9.4 % each); in the re-run above it was
1.6 % / 14.7 % / 0.9 %. No queueing model sees that, so
per-flow loss under overload carries errors of a few percentage points while the aggregate is
exact. The validation page reports both. Probe-loss rows show the same effect even more
strongly, so they are reported but excluded from the headline numbers. p95 errors (0.1–3.1 %)
are dominated by WSL timer noise in the tail.

## 10. AI layer (AIOps and copilot)

Three parts work on the same live measurements as the rest of the dashboard. Each can be
evaluated on its own. The first two need no language model and run all the time:

```
 probes + counters (1 Hz) --> signals.py --> anomaly.py ----------------+
                                              learned baselines         |
 probe paths + liveness  ----------------------------------------------> rca.py --> diagnosis
                                                                         tomography   |
 copilot/ (LLM)  <--- tools: live state, AI insights, twin, events <-----------------+
      |
      +--> propose_* --> a card in the UI --> the USER presses Apply --> chaos / routing / traffic
```

### 10.1 What the AI watches (`ai/signals.py`)

47 signals on the default topology, one scalar per second each:

| Signal | Source | Count |
|---|---|---|
| core link round trip, probe loss | `L:<link>` probe stream (router probes neighbour) | 2 × 7 |
| link load = tx / shaped rate (both directions, max) | interface counters | 13 |
| access segment round trip, probe loss | `G:<host>` stream (host probes gateway) | 2 × 4 |
| flow round trip, probe loss | `F:<client>><server>` stream | 2 × 4 |
| flow data loss | iperf3 receiver reports (only while traffic runs) | 4 |

Round trip is the mean over 2 s. Probe loss is over 10 s, with the window shifted back by the
2 s probe timeout: a lost probe is only known 2 s after it was sent, and is stamped with its
send time, so an unshifted window would always under-count. The AI layer **never reads the
chaos lab's fault list**. It sees what the network shows, so the evaluation can score it
against the injected faults honestly. Capacity comes from the interface's shaped rate, the
same thing SNMP's ifSpeed would report.

### 10.2 Learned-baseline anomaly detection (`ai/anomaly.py`)

The health colours use fixed rules: loss ≥ 1 %, load ≥ 85 %, RTT > 1.5 × design + 2 ms. A
fixed rule has to be loose enough for every link at once. So it misses a link that is 4 ms
slower than it has ever been, and it flaps on low, random loss: a single lost probe out of 50
already reads 2 %. Instead, each signal learns its own normal, online and unsupervised:

* **Model:** an exponentially weighted mean and variance (α = 0.05, about 20 s of memory). The
  first 20 samples are a cumulative average (warm-up).
* **Score:** z = (value − mean) / scale, with scale = max(std, floor, rel_floor × |mean|). The
  floors exist because emulated links are almost perfectly flat (std ≈ 0.02 ms). Without them,
  0.1 ms of WSL timer jitter would score z = 5. Floors used: RTT 0.3 ms or 5 % (flows 0.5 ms or
  6 %), probe loss 0.4 pp (flows 0.5 pp), iperf3 data loss 0.25 pp (about 625 datagrams/s per
  flow, so it is far less noisy than 10 probes/s), load 0.05 or 10 %.
* **Direction:** only increases count for RTT and loss. Load only counts when it is both unusual
  and above 70 % of capacity. An operator starting traffic is a change, not a fault.
* **Hysteresis:** a signal is raised when z ≥ 4 for 3 consecutive seconds, or z ≥ 8 for 2. It
  clears after 5 seconds below z = 2.
* **No learning from anomalies:** the baseline is frozen while a signal is anomalous, so a fault
  never becomes "normal". **Re-learn** (AI ops page) resets everything after a deliberate,
  permanent change.

### 10.3 Root-cause analysis by probe-path tomography (`ai/rca.py`)

Every probe stream crosses a known set of elements. `L:r2-r5` crosses {r2, r2-r5, r5}.
`G:c1` crosses {c1-sw1, sw1, sw1-r1, r1}. `F:c1>srv1` crosses everything on the flow's
installed path. A broken element makes every stream through it bad, and leaves every stream
that avoids it alone. This is Boolean network tomography:

1. **Observations.** Each stream is bad, good or unknown per symptom: dead (probe liveness),
   latency (detector), loss (detector), plus congestion for links at ≥ 85 % load. "Good" means
   clearly normal (z < 1). A stream that is merely below the alarm line neither accuses nor
   clears anything.
2. **Candidates** are the elements crossed by some bad observation. For each candidate,
   *explains* is the set of bad observations through it, and *contradicts* is the set of
   clearly good observations of the same symptom through it.
3. **Greedy cover.** Repeatedly pick the element with the most newly explained observations
   minus its contradictions. One crashed router (4 dead streams, no contradictions) beats four
   separate link failures. A single dead link beats its routers, because their other links
   still echo. Two simultaneous failures come out as two causes.
4. **Classify.** Dead → link or node down. A loaded link → congestion, or a traffic surge when a
   burst flow crosses it. Otherwise loss → packet loss, or latency → added delay.
5. **Confidence.** High when nothing contradicts it and it is backed by two observations or a
   direct probe. Medium when the probes cannot separate it from another element (for example
   sw1 vs its uplink sw1-r1); the alternatives are listed. Low when something contradicts it.

**Live finding (changed the design).** The first version blamed r1-r2 for a fault on r2-r5.
The adaptive controller moves the flows off r2-r5 in about 1.3 s, before the detector fires.
The flows' RTT is then up because the new path is longer, and every element on the new path
looked guilty. The fix tracks the path each flow's baseline was learned on. A new path becomes
the baseline only after the flow has stayed on it for 30 s with clearly normal RTT. A z-score
test alone raced: for one tick after the switch, the 2 s RTT window mixes old and new samples
and looks normal. A rerouted flow's higher RTT is now reported as a side effect, not a fault.
Its recent loss may be explained by elements of its old path, but its liveness may not, since
liveness is about now. Both cases are regression tests in `tests/test_ai.py`.

**Two more, found by the evaluation (10.4).**
* *Lucky loss-free streams.* With 1 % loss on r2-r5, some flows over it saw zero lost probes in
  their 10 s window and "cleared" the link, so no cause was found. But zero losses in 100
  probes is still 13 % likely at 2 % round-trip loss. Loss is random; latency is not. A clean
  stream now contradicts a loss hypothesis only when the loss seen on the bad streams is ≥ 5 %,
  where zero losses would be implausible (0.95¹⁰⁰ < 1 %). Below that, clean streams only break
  ties, so r2-r5 still beats r2 and r5, whose other links are clean.
* *Bufferbloat is not a cut.* A 28.5 Mbit/s surge into the 30 Mbit/s r2-r5 link (6 Mbit/s
  already on it) fills its 1000-packet queue. The RTT rises from 10 to 334 ms, and tail drop
  silences the probes for more than 1.2 s at a time, so the diagnosis flipped to "link down".
  But the counters show the link sending at 100 % of its rate, so it cannot be down. A
  silent link that is loaded at ≥ 85 % is now classified as congestion, or as a surge when a
  burst crosses it.

### 10.4 Evaluation against real faults (`ai/evaluation.py`)

**Protocol.** Everything runs on the live network, with the default traffic running and static
routing, so a fault stays on the flows' path.

1. Re-learn the baselines for 30 s.
2. A quiet minute with no faults: every anomaly raised there is a false alarm, and so is every
   ok → degraded flip of a health colour.
3. Each of 8 faults goes in through the chaos lab, and the system is polled every 0.5 s for up
   to 25 s. Three things are timed or scored:
   * **Learned detector:** when the first new anomaly is *raised*. This is the alarm time, not
     the onset; an earlier version of this scorer used the onset and flattered the detector by
     about 2 s.
   * **Threshold rules:** when a link on the fault's path first turns degraded or down.
   * **Diagnosis:** the top cause 4 s after the first alarm, which must name the injected
     element *and* the right fault class.
4. Revert, and wait until every anomaly has cleared.

The diagnosis never sees the fault list; only the scorer does. One run takes about 5 minutes.

**Results** (run 1 of 2, both after the fixes in 10.3; target link r2-r5, the 30 Mbit/s
bottleneck of c1 → srv1):

| Injected fault | Learned detector | Threshold rules | Diagnosis 4 s after first alarm |
|---|---|---|---|
| +2 ms latency (subtle) | 3.8 s | **missed** (RTT 14 ms < 17.4 ms rule) | Link r2-r5 has extra latency ✓ |
| +25 ms latency | 1.8 s | 0.5 s | Link r2-r5 has extra latency ✓ |
| 1 % loss (subtle) | 5.4 s (iperf3 data loss) | 4.5 s, then flipped twice | Link r2-r5 is dropping packets ✓ |
| 10 % loss | 1.9 s | 2.5 s | Link r2-r5 is dropping packets ✓ |
| capacity cap 4 Mbit/s (overload) | 1.0 s | 0.5 s | Link r2-r5 is congested ✓ |
| 28.5 Mbit/s surge c2 → srv1 | 1.7 s | 0.8 s | Traffic surge from c2 → srv1 is congesting r2-r5 ✓ |
| link r2-r5 down | 4.1 s | 1.5 s | Link r2-r5 is down ✓ |
| router r2 down | 3.7 s | 1.5 s | Router r2 is down ✓ |

| | Learned detector | Threshold rules |
|---|---|---|
| faults detected | **8 / 8** | 7 / 8 |
| subtle faults detected | **2 / 2** | 1 / 2 |
| mean time to detect | 2.9 s | **1.7 s** |
| false alarms in the quiet minute | 0 | 0 |
| health-colour flips while a fault was on | n/a (hysteresis: raise after 2–3 s, clear after 5 s) | 10 |

Root cause: **8 / 8 correct** (element and fault class), all at high confidence.

**Run 2** (same protocol, after a restart) reproduced it:
* learned detector 8 / 8 (mean 3.1 s), threshold rules 7 / 8 (mean 1.6 s, again missing the
  +2 ms);
* subtle faults 2 / 2 against 1 / 2;
* diagnosis 8 / 8 correct;
* 0 false alarms for either method.

Its surge diagnosis was right but at *low* confidence: the probes that got through the full
queue counted as evidence against the ones that did not. Liveness no longer contradicts a
congested link (regression test
`test_rca_congested_link_keeps_confidence_when_some_probes_survive`). Run 2's 1 % loss
diagnosis was *medium*: early on, only the flows' iperf3 data loss was bad, and that cannot
separate r2-r5 from the other links on the path.

**Reading it honestly:**
* The learned detector catches what the rules cannot (+2 ms), and its hysteresis keeps it from flapping. It is *slower*
  on hard faults, because it insists on 2–3 consecutive 1 Hz samples, and probe loss is only
  known after the 2 s probe timeout. That is the price of zero false alarms.
* Hard failures are not slowed down by this: the diagnosis uses probe liveness (1.2 s dead
  interval) directly, so "Link r2-r5 is down" was correct 2.5 s after the cut.
* The 1 % loss is found through the iperf3 receivers, not the probes. At 10 probes/s, about
  2 % round-trip probe loss is roughly two lost probes in a 10 s window, indistinguishable from
  luck for many seconds. Without traffic across the link, it would take much longer.
* One topology, one target link, one load level. The table shows the method works on this
  network; it is not a general benchmark.

### 10.5 Copilot (`ai/copilot/`)

A tool-using LLM agent: the model asks for tools, the backend runs them, the results go back
to the model, and the loop repeats (at most 8 rounds). The answer streams to the browser as
server-sent events.

* **Providers** (`providers.py`). Claude through the Anthropic SDK (default `claude-opus-5-5`,
  set with `NETVISTA_AI_MODEL`), with prompt caching on the static system prompt and tools. Or a
  local model through Ollama's native `/api/chat` with `think: false` and a 12k context. It is
  picked automatically: `ANTHROPIC_API_KEY` if set, otherwise the first tool-capable model of a
  running Ollama. Under WSL2 NAT networking, Windows' 127.0.0.1 is unreachable from the
  backend. So when plain HTTP fails, requests go through Windows' own `curl.exe` via WSL
  interop (65 ms overhead), which needs no firewall rule or Ollama setting.
* **Tools** (`tools.py`) come in three kinds, and the split is the safety model:
  * *read* (10): network status, link/node/flow details, AI insights, incidents, events,
    history, traceroute-style path, and twin/AI accuracy;
  * *simulate* (1): `run_what_if` on the SimPy twin, labelled SIMULATION in its result;
  * *propose* (4): fault, revert, routing, traffic.

  A proposal is validated with the same `validate_change` the chaos lab uses, stored and shown
  as a card. **The model has no tool that changes the network.** Only the user's Apply click
  does, and the event log records "approved by the user". The card can also ask the twin to
  predict the impact first. Local models get a smaller toolset of 9 to keep the prompt short.
* **Grounding check** (`grounding.py`). After each answer, every measurement-like number (a
  unit or a decimal point; bare counts, ids, IPs and clock times are skipped) is looked up in
  the numbers of the tool results, the topology facts and the user's own messages. Unit
  scaling (fraction ↔ %, ms ↔ s, bit/s → Mbit/s) is allowed, within the written precision or
  0.5 %. The UI shows "N of M values traced to data" and underlines the rest. In the live test
  below, the model wrote "over 90 seconds" for a fault the data said was 94.2 s old; that was
  the one value flagged.
* **Context.** Each question carries what the user is looking at (page, selection). Tool
  results older than the last turn (local) or last 3 turns (Claude) are replaced by a stub, so
  long conversations stay within the context. Tool outputs are compact JSON with nulls dropped.
* **Measured** (local `gemma4:12b`, CPU only, 7 GB RAM WSL): "explain the r2-down diagnosis"
  took 303 s. It made 2 tool calls, gave a correct explanation (cause, evidence, the
  controller's reroute to r1-r3-r4-r5, next steps), and 5 of 6 values traced to data. Claude
  was not measured: the development machine has no API key. The provider code path is covered
  by unit tests with a scripted provider.

## 11. Assure: intent-based assurance

### 11.1 The question, and why not "more AI"

Phases 1 to 6 measure the network, predict a change, validate the prediction and diagnose
faults. They still leave the operator's real question open: *will the network keep its
promises when something fails, and what should change so that it does?* That is what
intent-based networking (RFC 9315) and, in research, verification of traffic properties under
failures are about. Assure closes the loop:

```
 intents ──► checked every second (live)            ──► compliance history, violation events
    │
    ├──► failure analysis: every single failure,     ──► fragility map, failure × intent matrix,
    │    fluid twin + the controller's reaction           "avoidable" vs "no routing can avoid it"
    │
    ├──► planner: all flows' paths jointly,          ──► plan = primary paths + pre-checked backups
    │    + backups for every failure                      (cross-checked with the packet twin)
    │
    └──► autopilot: safety gate ─► apply ─► verify on live measurements ─► keep or roll back
              │                                     │
              └── drills and the live benchmark ────┴──► residuals ─► prediction intervals
```

An outside review suggested making machine learning the centre of the next version. Each
suggestion was checked against this system's own numbers before deciding:

| Suggestion | Decision | Why |
|---|---|---|
| Learn the twin's error with gradient-boosted trees (residual correction) | **Not done** | The twin is already within 0.2 % on median latency and 0.1 % on throughput for the traffic it models (section 9). A regressor trained on 50-100 runs would learn noise. Where the twin *is* wrong (TCP, per-flow overload split, transients) the cause is missing physics, which a regressor on CBR data cannot learn |
| Rank candidate paths by a weighted objective | Already existed | That is the adaptive controller's score (section 7.2). What was missing is joint, intent-aware, failure-aware planning, built in 11.5 |
| Conformal intervals, evaluated on unseen scenarios | **Done differently** | Split-conformal coverage assumes the new case is exchangeable with the calibration set; testing on unseen scenarios breaks that on purpose. The pool *measures* its coverage online instead of claiming it (11.7) |
| Reinforcement learning for routing | **Not done** | The decision space here is small enough to search exactly: 1296 primary routings on the default topology, 4096 on Abilene. Exhaustive search finds the optimum *and proves* when no routing can meet an intent, which no learned policy can |
| Graph neural network surrogate of the twin | **Not done** | For constant-bit-rate traffic a closed-form fluid model is as accurate as the packet twin and 1000 times faster (11.3). A learned surrogate would be justified for bursty or TCP traffic |
| Shadow mode, safety gate, rollback, benchmark against baselines | **Done** | Sections 11.6 and 11.9 |

### 11.2 Intents (`assure/intents.py`)

| Kind | Checks | Measured live as |
|---|---|---|
| `reach` | the flow is connected | flow probe liveness |
| `latency` | round trip p50/p95/p99 ≤ max_ms | flow probe RTT percentile over 10 s |
| `loss` | loss ≤ max_pct | iperf3 data loss over 3 s while the flow carries traffic, probe loss over 5 s otherwise |
| `bandwidth` | spare capacity at the path's bottleneck ≥ min_mbps | capacity − counters (without the flow itself) |
| `avoid` / `waypoint` | the path does not cross / does cross the listed elements | installed path |
| `max_util` | listed links (or every core link) ≤ max_pct load | interface counters / shaped rate |
| `disjoint` | two flows share no core link (and optionally no transit router) | installed paths |

Every intent has `protect` (none, link, node, any: must it also hold after any single
failure of that class?) and `priority` (critical 10, high 3, normal 1: weights in the
planner's objective). The *same* check runs on a live view and on a predicted view, so "holds
now" and "would hold if r2 failed" are one piece of code. Live results are recorded every
second; a violation is announced after 2 s and cleared after 3 s (debounce), and the page shows
compliance over 1, 5 and 15 minutes. Intents are stored per topology in `runs/intents.json`.
The **Gold / bronze** preset derives its numbers from the topology: each default-traffic flow
gets a round-trip SLO of 1.25 × its design RTT rounded to 5 ms (30 ms here), plus loss ≤ 1 %,
core links ≤ 85 % and reachability, all required to survive any single failure.

### 11.3 The fast twin: a fluid model (`simulator/fluid.py`)

The packet twin needs about a second per prediction; failure analysis and planning need
thousands. The fluid model computes the same steady state in closed form from the same
configuration, per directed hop (one tc egress):

* offered λ = Σ wire rates reaching the hop; after netem loss λ' = λ(1 − p)
* accepted a = min(λ', C, Q·s / (D + s/C)) — the third term is netem's buffer, which also
  counts packets still in the delay line (a long delay at a high rate drops below capacity)
* full buffer (λ' ≥ C): latency = Q·s / C by Little's law, deterministic
* otherwise latency = D + W with Kingman's G/D/1 waiting time W = ρ/(1−ρ) · ca²/2 · s/C, where
  ca² comes from Whitt's QNA superposition of the CBR sources (each with ±30 % gap jitter)
* flows crossing an overloaded hop lose the same fraction (fair tail drop), and the reduced
  rates are propagated downstream to a fixed point
* RTT percentiles = sum of hop latencies both ways + calibrated overheads (E + H·2n) +
  the calibrated noise quantile + z_q · √(Σ jitter² + Σ W²)

It returns the packet twin's result shape, so the controller-reaction loop, the validation
comparisons and the UI accept either engine (`whatif.predict_with`).

**Against the packet twin** (13 scenarios on the default topology, flow c1 → srv1 shown):

| Scenario | RTT p50 packet / fluid | Throughput packet / fluid | Path |
|---|---|---|---|
| Baseline, adaptive | 25.02 / 25.14 ms | 6.01 / 6.00 | same |
| +40 ms on r2-r5, static | 105.03 / 105.14 ms | 5.98 / 6.00 | same |
| +40 ms on r2-r5, adaptive (reroute) | 29.18 / 29.26 ms | 6.01 / 6.00 | same |
| 5 % loss, static | 25.07 / 25.14 ms | 5.72 / 5.70 | same |
| cap 14 Mbit/s (89 % load), static | 25.06 / 25.29 ms | 6.01 / 6.00 | same |
| cap 8 Mbit/s (overload), static | 1235.8 / 1262.1 ms | 3.92 / 3.86 | same |
| burst 20 Mbit/s, static (overload) | 348.3 / 351.4 ms | 5.44 / 5.43 | same |
| r2-r5 down, adaptive | 29.18 / 29.26 ms | 6.01 / 6.00 | same |
| r2 down, adaptive | 35.14 / 35.26 ms | 6.02 / 6.00 | same |
| 4 flows, 40 Mbit/s, static (overload) | 349.5 / 351.4 ms | 8.70 / 8.70 | same |

Paths agree in all 13; throughput within 1.5 %; RTT within 0.5 % below saturation. Under a full
buffer the fluid model is 2 % high, because it fills the queue with data-sized packets while
the real queue also holds a few small probes. It is slightly conservative where it matters.
Cost: 0.2-1.3 ms per prediction against 0.26-2.25 s. Its error against the live network is
measured by every validation run, drill and deployment (over the 11-scenario validation suite of section 9: RTT p50 error 0.59 % against 0.23 % for the packet twin, p95 1.08 % against 0.67 %, bottleneck total throughput 0.05 % against 0.16 %, paths 100 % for both).

### 11.4 Failure analysis (`assure/resilience.py`)

For every core link and every router (12 scenarios here; optionally every pair of core links),
with the live link parameters, demand and paths:

1. **Reaction.** Static: paths stay. Adaptive: the controller's own scoring rule, round by round,
   herd guard as configured (the first round is kept as the "transient"). Intent: the plan's
   pre-planned backups, the adaptive rule for anything the plan does not cover.
2. **Outcome.** The fluid model predicts latency, loss and load, and every intent whose
   `protect` covers that failure class is checked.
3. **Classification of every break.**
   * *cut off*: the failure disconnects the flow in the graph: a single point of failure of
     the topology, nothing to do with routing;
   * *unavoidable*: the best routing for that failure also breaks it. The bound is computed by
     **exhaustive search** over every combination of surviving candidate paths (local search
     above 6000 combinations). With K = 8 the candidate sets contain every simple path of the
     default topology, so "unavoidable" is a proof within the model, not a guess;
   * *avoidable*: some routing would have kept it.
4. **Scores.** Resilience = priority-weighted share of (failure, intent) cells that hold, next to
   the best any routing could reach. Each element's criticality = the weighted share of intents
   its failure breaks: the fragility map.

Cost: 0.2 s on the default topology, about 2 s on Abilene (25 scenarios, with bounds). It runs in
the twin's worker process at low CPU priority (11.10), every 60 s and 2 s after any change.

### 11.5 Planner and the intent routing mode (`assure/planner.py`, `routing/protection.py`)

**Objective**, lexicographic: (1) weighted intent violation now (severity 1 + relative excess,
times priority; "at risk" cells, inside the limit but not with the prediction margin, cost a
quarter), then (2) the same summed over the single failures, each with its best backups, then
(3) the controller's path score plus 3 ms-equivalents per traffic-carrying flow moved
(stability: never move traffic for nothing).

**Search.** Every combination of the flows' candidate paths when that is at most 6000 (1296 here),
local search with restarts otherwise. For the best primaries (those tied on the first key, at
most 48), each failure's backup is searched over every combination of the *affected* flows'
surviving candidates: local repair, as fast reroute does. Typical cost: ~2900 fluid evaluations,
0.55 s.

**Intent mode in the controller.** A plan is `{primary, protection: {scenario: {flow: path}}}`.
The controller pins flows to their primaries. When links die it names the scenario that explains
the dead set (one link: `link:x`; several links sharing one router: `node:r`) and installs that
scenario's backups at once, before it would have scored anything. A failure the plan does not
cover falls back to the adaptive rule. When links come back, flows return to their primaries
only after the links have stayed up for a wait-to-restore time of 5 s (revertive protection
with a WTR timer, as in ITU-T G.8031), so a flapping link cannot drag traffic back and forth.

**The herd guard.** The adaptive controller decides flows one after another within one tick,
against interface counters refreshed twice a second. When several flows leave a failed or
congested path they all see the same "empty" alternative and pile onto it, then all leave it
again: the oscillation that delay-based ARPANET routing showed in 1979. The twin predicted it on
the default topology under load (with the guard off, all four flows converge onto one path). With
the guard, each decision adds the moving flow's rate to the counters the next flow sees. It is on
by default and can be switched off on the Routing policy panel and in the benchmark. Live, the
effect depends on how many flows move at the same instant. In the first benchmark run phantom
"every link died" events moved all flows together every 33 s (84 route changes without the guard,
31 with it). Once those were fixed (11.10, finding 6), single failures moved one or two flows at a
time and the difference vanished (29 against 26), but the outcome became predictable (11.9).

**Example** (live, stress traffic of 12 + 8 + 8 + 12 Mbit/s, gold/bronze intents): the adaptive
controller balanced load and put gold flow c2 → srv2 on r1-r3-r5 at 36.9 ms, breaking its 30 ms
SLO. The plan kept both gold flows on r1-r2-r5 (24.7 ms) and the bronze ones on r1-r3-r5;
predicted resilience 59.3 % → 75.5 %, equal to the exhaustive bound; the packet twin agreed with
the fluid prediction within 2.7 % RTT and 0.14 pp load; once applied, all 5 intent outcomes were
as predicted.

### 11.6 Autopilot (`assure/autopilot.py`)

Modes: **off**; **shadow** (plans logged as "would apply", nothing changes); **approve** (plans
that pass the gate wait for the operator); **auto**. Triggers: intents changed, an intent
violated for 5 s, traffic or link state changed, or a periodic check every 60 s; 3 s settle
after a trigger. The **safety gate** is deterministic: no other job owns the network; every
primary path is alive; strictly better than staying put; no critical intent predicted to break
that holds now; predicted peak load ≤ 95 %; the packet twin agrees with the fluid model within
5 % RTT and 5 pp load; at least 20 s since the last deployment.

**Verification** applies to every deployment, by hand or automatic: wait 12 s, measure for 10 s,
compare every intent with its state before the change and with the prediction. A deployment is
**rolled back automatically** if an intent that held before is now violated, or a critical intent
predicted to hold is measured violated. Every comparison goes into the ledger and its residuals
into the uncertainty pool.

### 11.7 How much to trust a prediction (`assure/conformal.py`)

Every residual from validation runs, drills and deployments joins a pool per engine and metric
class (relative error for RTT and throughput, percentage points for loss). The 90 % interval
half-width is the ⌈(n+1)·0.9⌉-th smallest absolute residual, the split-conformal rule. Because
the scenarios are not exchangeable, the guarantee is not claimed: each new residual is first
tested against the interval computed without it, and the measured coverage is shown next to the
nominal 90 %. Measured on this machine: for round trips the fluid model's interval is ±3.8 % (160 residuals) and it covered 90.1 % of the 151 later results it was tested against; the packet twin's ±3.2 % (276 residuals) covered 91.4 % of 267. That is the nominal 90 %. For data loss and throughput the intervals are wide (about ±7.5 pp and ±8 %) and covered only 74-85 %: their residuals are dominated by the per-flow split under overload, which no queueing model predicts (section 9). Those scenarios are not exchangeable with the rest, and the measured coverage says so instead of hiding it. The planner uses the RTT half-width as its margin: a predicted latency
whose upper bound crosses the limit is "at risk".

### 11.8 Drills (`assure/drill.py`)

A drill fails one element on the live network and checks the failure analysis: it freezes both
engines' predictions, injects the failure, records every intent each second from the moment of
injection (the transient), measures 12 s of steady state after an 8 s settle, reverts, waits for
recovery and compares paths, every intent's outcome and the error of both engines.

Live example, link r2-r5 failing under plan P1: backup paths exactly as predicted by both
engines; 5 of 5 intent outcomes as predicted (the gold flows held at 28.8 ms against 30 ms, as
forecast); RTT p50 error 0.82 % (fluid) and 0.22 % (packet), p95 3.3 % and 2.1 %; the loss
intent was violated for 3 s during the 1.3 s detection window, then clean.

### 11.9 Live benchmark (`assure/benchmark.py`)

**Protocol.** Same traffic (every flow loaded: 12 + 8 + 8 + 12 Mbit/s, about 40 % of the source
site's uplinks), same intents (gold / bronze preset), same failures (every core link and every
transit router: 10 scenarios) under four strategies: static; adaptive as originally built (herd
guard off); adaptive with the herd guard; the intent plan. Per failure: an 8 s baseline window,
inject, watch 20 s, revert, then wait until the links answer and every intent is back to its
pre-failure state (at least 12 s). Every intent is checked every second. Score: priority-weighted
intent-seconds violated in the 20 s after the failure. Before each failure the failure analysis
predicts which intents will break for that strategy; the prediction is scored against the
measurement.

**Result** (latest run, 34 min, with the host-stall guard of section 5; the full per-failure
tables are in `docs/results/RESULTS.md`, exported by `scripts/export_results.py`):

| Strategy | Weighted intent-s violated | in normal operation (baseline windows) | Route changes | Data delivered | Prediction right |
|---|---|---|---|---|---|
| Static (Dijkstra) | 6730 | 2585 | 0 | 51.3 % | 4 of 10 |
| Adaptive, no herd guard | 2516 | 943 | 29 | 98.1 % | 3 of 10 |
| Adaptive + herd guard | 2597 | 920 | 26 | 97.8 % | 7 of 10 |
| Intent plan (Assure) | **1517** | **73** | **12** | **98.4 %** | **8 of 10** |

What it shows:

* **The plan is the only strategy that meets the intents in normal operation.** Static puts all
  four flows on the shortest path, which then carries 133 % of r2-r5's capacity. The adaptive
  controller balances load and moves a gold flow onto the 34 ms detour, breaking its 30 ms
  SLO most of the time. Across the ten 8 s baseline windows the plan violated 73
  weighted intent-seconds against 920-943 for adaptive routing, about 13 times fewer.
* **Over the failures it has 40 % fewer violation-seconds than either adaptive variant**, with
  half their route changes and the most data delivered. Its violations are mostly *new* ones
  (1117 of 1517): it pays during failures, while adaptive routing pays all the time.
* **Unavoidable failures cost every strategy alike.** When r1-r2 or r2 fails, the gold SLO
  cannot be met by any routing: the remaining paths are longer than 30 ms. The failure
  analysis predicted both gold intents to break under every strategy. They cost 376-425 under
  every dynamic strategy and are half of the plan's total.
* **Where the plan lost.** (a) r1-r3: r1 has only two links, so the bronze flows' backup has to
  leave over r1-r2, which the gold flows use. For 12 s the gold p95 (10 s window) stayed above
  30 ms. The steady state afterwards was clean, as predicted. (b) r2-r5 and r4-r5, the plan's
  two wrong predictions: the gold flows run r2-r5 at 83 % of its 30 Mbit/s, the only way both
  can meet 30 ms. In the half-minute before the r2-r5 failure, with no failure active, its
  queue built up twice: the gold round trips reached 166 ms and then 479 ms (2 s means). The gold intents were already violated when
  the failure came: only 9 of that row's 409 seconds are new. During r4-r5 a smaller burst
  pushed the gold p95 to 36 ms. The trace shows how: delivery dips for a second (to 61-76 % of
  the link), then the receivers get more than was offered (up to 14.4 against 12 Mbit/s) as
  iperf3 catches up to its average rate. Most likely the dips are short packet-path pauses,
  below the stall detector's 0.5 s threshold. At 83 % load only 17 % headroom is left to drain
  each burst, so the queue ratchets up. Both twins assume smooth CBR and predict a queue of
  well under a millisecond there. A 30 ms SLO with 5 ms of headroom is what makes these bursts
  visible.
* **The herd guard did not change the outcome in this run** (2597 against 2516, 26 against 29
  route changes, both within run-to-run noise), but the adaptive controller's outcome became
  predictable: 7 of 10 against 3 of 10.

**Two runs, and why the first one is not the headline.** The first full run, before the stall
guard, gave 6652 / 3859 / 2827 / 1268 weighted intent-seconds and 0 / 84 / 31 / 18 route
changes. Its ranking is the same and the plan's lead was larger. But every host stall (finding 6 in
11.10) then looked like every link failing at once: each adaptive variant failed over every 33 s for
nothing. The 84 route changes, and the large herd-guard effect they seemed to show, were mostly
those phantom fail-overs. Both runs stay in `RESULTS.md` ("All complete runs").

### 11.10 Findings that changed the design

1. **The reactive controller herds** (11.5). Predicted by the twin, measured live; fixed by the
   herd guard.
2. **Calibration across a reroute.** The first auto-calibration after a restart ran while flows
   were still being moved off the initial static paths. The model compared new paths with RTTs
   measured on the old ones: fit RMS 5.7 ms, and the "noise" distribution absorbed 300 ms
   queueing outliers, so every prediction was ~70 ms too high. Calibration now uses only the
   samples taken on each flow's current path, rejects any fit with an RMS above 1 ms (keeping the
   previous calibration) and retries every 20 s.
3. **iperf3 can report impossible loss.** After a failure one interval reported 4.9·10¹⁷ % loss
   (its sequence-gap counter wrapped on reordered datagrams). Intervals with lost > total are
   now dropped (regression test in `test_topology_and_parsers.py`).
4. **The verification window must outlast the measurement window.** A correct plan was rolled
   back: verification started 6 s after the change while the latency intents still used a 10 s
   RTT window full of the old path's samples. Settle is now 12 s, and the benchmark's recovery
   waits for every intent to return to its pre-failure state. The polluted residuals were removed
   from the pool (`runs/residuals.jsonl`), the benchmark run made before the fix is kept in
   `runs/archive/benchmarks_before_fixes.jsonl`.
5. **Background computation must not touch the data plane.** A failure analysis or a packet-twin
   run in the worker process could delay the emulated hosts' iperf3 senders and probe agents, and
   gold flows on unrelated paths showed RTT tails up to 36 ms. The worker processes now run at
   `nice 10`; the same experiment then showed at most +0.4 ms.
6. **The host's packet path freezes, and it looked like link failures.** In a quiet network,
   every core link was declared dead for about 100 ms, all together, roughly every 33 s (57
   false deaths in a 5-minute soak). Stamping echoes with the agents' clock (section 5) did not
   help, so the cause was not the backend. A kernel `ping` between two routers showed the same
   ~1.3 s gaps. Reproduced **with no NETVISTA code at all**: two bare namespaces, one veth, a
   20 Hz ping. Every 32-33 s the namespaced packet path stops for 0.75-1.5 s. It happens with
   HTB + netem, with netem only, and with no qdisc; with and without a 6 Mbit/s UDP load; with
   dynamic ARP, permanent ARP entries or a 200 ms ARP retransmit timer. Echoes that were in flight
   come back with RTTs up to 1.46 s. Busy loops pinned to each of the 14 vCPUs and a process
   sleeping in 10 ms steps saw no gap over 100 ms during the same 70 s, so it is not a VM or CPU
   freeze. It is a property of this WSL2 kernel (6.18.33.2-microsoft-standard), not of the
   design. It cannot be fixed from inside the guest, so it is **detected**: a failure silences the
   probes that cross it, while a host stall silences all of them at the same instant (section 5).
   The stall's time does not count as silence, and its samples are left out of the statistics.
   Each stall is logged and counted, so the evidence stays visible. Without this, every stall
   also left about 13 RTTs of up to 1.4 s in each stream's 10 s window. That is enough to break
   every p95 latency intent for 10 s, a third of the time. The benchmark in 11.9 was re-run with
   the guard. (The test scripts are in `scripts/diagnostics/`.)

## 12. Topology designer and library

Tools like Cisco Packet Tracer let you draw a network and *simulate* it. The designer lets you
draw one and then **boot it**: every router becomes a Linux network namespace with IP
forwarding, every switch an Open vSwitch bridge, every cable a veth pair shaped by tc, and the
rest of NETVISTA (probes, controller, twin, AI layer, Assure) attaches to it unchanged, because
every subsystem is derived from the topology file.

* **Editor** (`pages/DesignerPage.tsx`): an SVG canvas with the same equipment artwork as the
  live map. Add routers, switches, clients and servers; drag them; cable two devices in Connect
  mode; edit a cable's capacity, delay, jitter, loss and buffer; edit the default iperf3
  traffic; undo (Ctrl+Z), delete (Del), pan and zoom. It edits plain topology JSON, the same
  format as `topologies/*.json`.
* **Design checks** (`topology/library.py`, `POST /api/topologies/check`, on every edit):
  * the structural rules the emulation enforces (one link per host, one gateway per switch,
    connected graph, interface names of at most 15 characters, at most 40 nodes and 80 links);
  * per managed flow: candidate paths (Yen, K = 8), **link-disjoint paths between the two sites'
    gateway routers** (the host's access link is single by definition), design round trip and
    bottleneck capacity;
  * single points of failure (`assure/model.spof_elements`), with core SPOFs flagged as warnings
    and site gateways noted as inherent;
  * default traffic that cannot fit through its shortest path's bottleneck.
  Deploy stays disabled while any error remains.
* **Library**: built-in topologies in `topologies/`, designs saved from the UI in
  `topologies/user/`. File names are resolved inside those two folders only.
* **Redeploy in place** (`POST /api/topologies/deploy`, `api/app.py: redeploy`): stop every
  subsystem of the running runtime, clean up exactly like `scripts/run.sh` (agents, iperf3,
  `mn -c`), build a new `Runtime` for the new file and boot it. The event sequence numbers keep
  counting across runtimes (browsers only accept events newer than the last one they saw), and
  every snapshot carries a `boot_id`: when it changes, the UI reloads the topology and drops the
  old network's per-link history. A design that fails to load puts the previous topology back.
  Deploy is refused while a validation, evaluation, drill, demo or benchmark owns the network.

New built-in topologies:

| File | What | Why it is there |
|---|---|---|
| `abilene.json` | The Abilene (Internet2) research backbone: 11 US points of presence, 14 links; one-way delays from city distances at ~200 km per ms; capacities scaled from 10 Gbit/s to 50 Mbit/s, Atlanta-Indianapolis kept at a quarter (it was 2.5 Gbit/s) | The classic topology of traffic-engineering research; 25 single-failure scenarios and 4096 primary routings for the planner |
| `metro-ring.json` | Six routers in a ring with two thin chords, two sites | Three link-disjoint paths per flow: the shape of metro and campus networks |

Measured planning cost on Abilene with four loaded flows: the full failure analysis (25
scenarios, each with its exhaustive best-routing bound) 1.8-2.0 s, the planner (4096 primary
routings, backups for 25 failures) 1.6-3.9 s. It took 3.9 s under the stress profile, with
15 462 fluid evaluations.

**Live run on Abilene, deployed from the library** (the same steps as `live_check.py` step 8):
the emulation was rebuilt as 11 Linux routers in 4 s, and all 18 links answered probes. The
gold/bronze preset derived 65 ms and 60 ms SLOs from the design. Under stress traffic every
intent held. The analysis found 86.4 % of (failure, intent) cells held, equal to the best
achievable, with the four edge PoPs as single points of failure. So the plan could not improve
resilience, and said so (86.4 % → 86.4 %). It was verified on live measurements after 23 s. A
drill on dnvr-kscy matched both twins' backup paths and 100 % of the intent outcomes.

## 13. Frontend

* **No fake data.** Every value on the live pages comes from the WebSocket snapshot. The twin's
  view is labelled "Simulation" with a dashed border. Nothing in the UI is mocked, so there is
  no MOCK label anywhere.
* **The map** (`components/TopologyView.tsx`, `components/devices.ts`) is built from four
  layers:
  1. a floor canvas with a dot-grid plan, site zones captioned with their subnets, cables and
     flow fibres;
  2. Cytoscape, for the equipment artwork plus pan, zoom and hit-testing (edges are invisible
     hit areas);
  3. an overlay canvas for LEDs, labels, health rings, cable breaks and the selection halo;
  4. HTML for the hover card.

  Packets travel *under* the equipment, so they visibly enter and leave each device. The
  equipment follows network-diagram conventions: the router puck with its four-arrow emblem,
  the switch, the server and the laptop. The art is static and never carries state; every
  light on it is drawn from data.
* **What moves, and what drives it:**

  | Visual | Driven by |
  |---|---|
  | comet rate per cable direction | interface packets/s ("1 comet ≈ N packets" in the corner) |
  | comet colour | the flows whose installed path crosses that hop, by their offered rates; the rest (probes, control traffic) is grey |
  | comet travel time | measured link latency (slow links have slow comets) |
  | activity LED blink rate | the node's measured packets/s |
  | status LED, amber/red rings, cable break | probe-measured health |
  | cable brightness/glow, thickness | measured utilisation, configured capacity |
  | fibre draw-in / old route fade | a real path change from the controller (plus once at page load) |
  | ripple / green sweep | a link or node going down / a cable coming back |
  | dotted marker with a spark on a cable | the learned-baseline detector finds one of its signals unusual (one ring at onset) |
  | "Likely cause" tag pinned to a link or router | the top root cause of the probe-path diagnosis |

  Under `prefers-reduced-motion` the comets, pulses and draw-ins are off and the LEDs hold
  steady. Probe-only pairs are hidden unless their legend chip is hovered.
* **Colour system:** hue is reserved for meaning. Status uses green/amber/red, always with an
  icon and a label. Flow identity uses TIA-598 fibre colours (#1 blue, #12 aqua, #10 violet, #4
  brown), validated with a CVD checker on the dark surface. They sit in the 6–8 ΔE warn band, so
  each flow also has a dash pattern and a direct label. Link load uses a neutral brightness and
  width ramp, so it never competes with status hues.
* **Provenance has one border language:** solid = measured live, dashed = simulation, dotted =
  written or inferred by the AI layer (AI insights, the AI answer tag, map markers).
* **Copilot drawer** (`components/Copilot.tsx`, `lib/copilot.ts`, Ctrl+K). It streams the answer
  over SSE, with each tool call as a row whose raw JSON opens on click. Proposals are cards
  with *Apply*, *Predict impact first* and *Dismiss*. The answer is rendered by a small
  Markdown subset (React elements only, no HTML injection), and values the grounding check did
  not find are underlined.
* **Assure page** (`pages/AssurePage.tsx`, `components/assure/`): a KPI strip (intents met now,
  live; survive a single failure, predicted, with the bound; routing mode; autopilot mode) and
  three tabs. *Intents & resilience*: the intent list with a 5-minute status strip per intent
  (run-length encoded SVG, one tick per second), the fragility map and the failure × intent
  matrix. *Plan & autopilot*: the plan inspector (paths now vs planned, predicted RTT with its
  interval, resilience before → after against the bound, the objective, backups per failure, the
  packet-twin cross-check, the verification verdict) and the autopilot's decisions with every
  gate check. *Evidence*: the live benchmark (a single-series bar chart of weighted
  intent-seconds, the plan's bar emphasised by lightness only, since the strategies are named on
  the axis; a summary table; a per-failure heat table), drills, the uncertainty pool and the
  deployment ledger.
* **Risk colouring on the map**: the live map's Load / Latency switch has a third mode, *Risk*,
  which colours each link and router by the share of intent weight its failure would break
  (neutral → coral), draws single points of failure with a dashed halo and labels routers
  "if it fails: breaks I1, I2". Live "degraded" amber is suppressed in this mode so a prediction
  never borrows a status colour; real outages still show.
* **Designer** (`pages/DesignerPage.tsx`, section 12): SVG editor with the map's equipment
  artwork, a 10 px snap, pan/zoom, undo, and design checks that update on every edit.
* **Redeploy**: the store compares every snapshot's `boot_id` with the one the topology was
  loaded from; on a change it reloads the topology and drops per-link history and the selection.
* Routers are recognised by their type in the deployed topology (not by an `r` prefix), so
  topologies such as Abilene (`sttl`, `dnvr`, …) render paths correctly everywhere.
* Fonts (Barlow) are bundled locally, so the demo works offline.

## 14. Concurrency model

* asyncio (FastAPI) serves HTTP and pushes one snapshot every 0.5 s to all WebSocket clients.
* Daemon threads: one reader per probe agent, counters (2 Hz), tc stats (1 Hz), routing
  controller (10 Hz), history sampler (1 Hz), traffic watcher, and the validation/demo/replay
  jobs.
* The twin runs in a separate process.
* The AI layer adds one thread (1 Hz: sample 47 signals, update the detector, run the
  diagnosis; about 1 ms per tick). One copilot worker thread per question streams events
  through a queue to a synchronous SSE generator, which Starlette runs in its threadpool. Only
  one question runs at a time.
* Assure adds one thread (1 Hz: build the live view, check every intent, record compliance,
  feed the autopilot's triggers; ~2-5 ms per tick). Failure analysis, planning and packet-twin
  confirmations run in the twin's worker processes, started at `nice 10` so they never compete
  with the emulated hosts (section 11.10). Drill, benchmark, verification and autopilot decisions
  each run in their own daemon thread; jobs that own the network exclude each other.
* Producers never touch asyncio. The broadcaster pulls events by sequence number from a
  thread-safe ring buffer. A redeploy swaps the runtime under the broadcaster and continues the
  event sequence, so clients never see numbers go backwards.

## 15. Tests

* `tests/test_routing_algorithms.py` – Dijkstra and Yen vs networkx on random graphs,
  determinism, dead-edge avoidance, the six default-topology paths
* `tests/test_scoring.py` – score formula, infeasibility, hysteresis, hold-down vs fail-over,
  weights
* `tests/test_simulator.py` – closed-form checks (propagation + serialisation, Bernoulli loss,
  overload goodput = capacity, buffer-full queueing delay), token bucket, reproducibility,
  twin fail-over / outage / latency-reroute prediction, calibration fit recovery
* `tests/test_topology_and_parsers.py` – topology validation, addressing, tc/iperf/proc
  parsers, policy-route generation, change validation, validation error maths, liveness on the
  agents' clock, and host stalls (a total 1.5 s silence kills nothing and its delayed echoes stay
  out of the statistics; two failed links out of eight are still detected on time; a link
  that died before a stall stays dead; total silence beyond 4 s is an outage)
* `tests/test_ai.py` – detector (learn, raise, clear, frozen baseline, floors, load rule,
  re-learn), root cause on synthetic probe worlds (link vs router, edge router, two failures,
  ambiguous switch, latency/loss localisation, congestion and surge, reroute side effects,
  fast reroute, lucky loss-free streams, full-queue silence), grounding (extraction, unit
  scaling, JSON strings), provider message formats, context trimming, and the agent loop with
  a scripted model (tool round, grounding, proposals need the user, one approval = one action)
* `tests/test_fluid.py` – fluid model: closed forms (propagation, overload = capacity and a full
  buffer by Little's law, netem's delay-line buffer limit, Bernoulli loss both ways, link down),
  fair split with downstream propagation, routing predictions identical to the packet twin,
  queue/loss/load within tolerance of the packet twin, speed (≥ 50× faster), and the herd guard
  (without it two movers pile onto the same path; with it they split)
* `tests/test_assure.py` – intent validation and labels; checks on views (latency, at-risk margin,
  avoid, waypoint, max_util); scenario keys and plan lookup with fallback; single points of
  failure; static vs adaptive reactions; the planner on the default topology under load (meets
  every intent now, the gold flows avoid the 34 ms detour, and it reaches the exhaustive failure
  bound with no avoidable break left); hard-constraint pruning; the full failure matrix; the
  conformal pool (finite-sample quantile, online coverage, persistence); the copilot's Assure
  tools (read/simulate/propose only; a proposed intent is validated and added only on Apply)
* `tests/test_library.py` – every built-in topology boots and reports; default-topology facts
  (SPOFs, design RTT, disjoint paths); errors and warnings (single path, core SPOF, host with two
  cables, traffic that cannot fit); library paths confined to the topology folders
* `tests/test_smoke_emulation.py` (root only) – boots real Mininet: netem delay is really
  applied, a latency change moves the measured RTT, policy-route fail-over, traceroute goes
  through the expected router, the OVS-based default topology passes traffic
* `scripts/live_check.py` – end-to-end acceptance test against the running system, now ending
  with Assure: preset intents met, a 12-scenario failure analysis, a plan verified on live
  measurements, and a drill whose backup paths and intent outcomes match the prediction
* `scripts/twin_check.py --suite` – calibration plus the full validation suite

## 16. Known limitations (honest list)

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
7. **UDP CBR traffic only.** TCP congestion control is not modelled, by either twin. Assure's
   predictions therefore cover what the emulation carries. Adding TCP is the first item of
   future work, and it is more than starting iperf3 without `-u`: (a) the veths have TSO/GSO on,
   so offloads must be disabled or 64 KB super-packets pass through netem as one; (b) the
   standing queue lives inside netem under HTB, so an AQM (fq_codel) experiment needs a different
   tc pipeline (shaper and AQM before the delay line), which both twins would then have to model;
   (c) the fluid model needs elastic flows (max-min sharing of what CBR leaves, a standing queue
   of roughly 0.7-0.9 of the buffer for loss-based Cubic/Reno against about one BDP for BBR),
   validated live the way section 9 validates CBR. Doing (b) would change the very pipeline the
   current validation numbers are about, so it was deliberately left for after this version.
8. **WSL timing:** RTTs include WSL's virtualised timer behaviour. Calibration absorbs the
   mean, but jitter on a busy laptop can be higher than on bare metal.
9. **Optional extensions not built:** eBPF/XDP telemetry.
10. **AI detection is bounded by the probe rate.** 10 probes/s means about 1 % random loss
    needs many seconds to separate from chance. The iperf3 receivers (about 625 packets/s per
    flow) catch it faster, but only on links that carry traffic.
11. **The diagnosis assumes one element per symptom set.** It is greedy, not exhaustive. Two
    faults on the same path can be reported as one, and a switch cannot be told apart from its
    uplink (both are listed).
12. **The copilot is only as fast and as careful as its model.** A CPU-only local model takes
    minutes. The grounding check catches invented or rounded measurements, but not a wrong
    conclusion drawn from correct numbers. That is why the model can only propose, never act.
13. **Assure plans paths, not capacity.** When the best routing still breaks an intent (the
    "unavoidable" cells), the fix is a topology change, a capacity upgrade or a relaxed SLO; the
    planner says so but does not propose the upgrade. Failures beyond one element are analysed
    (N-2 option) but not pre-planned: they fall back to the adaptive rule.
14. **The bound is exact only within the candidate sets.** K = 8 shortest paths cover every
    simple path of the default topology; on larger topologies "unavoidable" means "no routing
    over the K candidates", and above 6000 combinations the search is local, not exhaustive.
15. **Prediction intervals are empirical.** Coverage is measured, not guaranteed, and the pool
    is small (tens to hundreds of residuals); a new kind of scenario can fall outside it.
16. **Host stalls are excused, not prevented.** On WSL2 the namespaced packet path stops for
    ~1 s about every 33 s (11.10, finding 6), and nothing inside the guest can stop it. The probe
    layer recognises these stalls and does not count them. The cost: a real failure that begins
    during a stall is detected up to one stall later. Total silence longer than 4 s is treated
    as a real outage. A topology with fewer than 6 probe streams cannot tell a stall from an
    outage, so there stalls are not excused. The iperf3 receivers are not filtered: an interval
    that spans a stall shows its real dip.

## 17. Likely viva questions

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
* *Is the "AI" just an LLM guessing?* No. Detection and diagnosis are deterministic,
  explainable algorithms (online statistics, Boolean tomography), scored against injected
  faults in section 10.4. The LLM only explains and proposes. Its numbers are checked against
  the data it read, and it has no tool that can change the network.
* *Why unsupervised baselines and not a trained classifier?* There is no labelled fault data
  for this network. Training on chaos-lab faults would teach the model the fault list we then
  evaluate on. Per-signal baselines need no labels and adapt to any topology file.
* *How do you stop hallucinated numbers?* Rule 1 of the system prompt, plus a post-hoc check:
  every number with a unit in the answer must match a number in the tool results, within the
  written precision. The UI shows the count and underlines the misses.
* *What surprised you?* Four things changed the design: HTB root changes failing, the token
  bucket, phase-locked CBR sources, and black-hole attraction during detection. Each is
  documented above, with the measurement that revealed it. Assure added six more (section
  11.10): herding, calibration across a reroute, iperf3's impossible loss, the verification
  window, background CPU leaking into the data plane, and WSL2's periodic packet-path stall,
  which looked exactly like every link failing at once until it was reproduced without NETVISTA.
* *Isn't this just Cisco Packet Tracer?* Packet Tracer simulates device configuration for
  teaching. NETVISTA boots real Linux routers (the Designer deploys any drawn topology), measures
  them, predicts what a change or a failure will do, plans routes against stated intents, and
  then measures whether its predictions were right (drills, deployments, a live benchmark).
* *Why no machine-learning model for the planner?* Because the problem can be solved exactly.
  1296 primary routings (4096 on Abilene) are searched in 0.6-4 s, which gives the optimum
  and a proof when no routing can meet an intent. A learned policy could do neither, and would
  need data from the very failures it is meant to prevent. ML is used where the problem is
  statistical: learned baselines for anomaly detection, and empirical prediction intervals.
* *Why trust the fluid model?* It agrees with the packet twin on every routing decision and is
  scored against the live network in every validation run (RTT p50 error 0.59 % against 0.23 %
  for the packet twin), every drill and every deployment. Under overload it errs on the high
  side.
* *What does "unavoidable" mean exactly?* For that failure, every combination of surviving
  candidate paths was evaluated and the intent still breaks in the best one. On the default
  topology the candidates are all simple paths, so no routing could keep it: it needs more
  capacity, a shorter link, or a different SLO.
* *What stops the autopilot from making things worse?* The deterministic gate (strictly better,
  no critical regression, both twins agree, headroom, rate limit), then verification on live
  measurements with automatic rollback. Shadow mode lets it run for days without touching
  anything, logging what it would have done.
