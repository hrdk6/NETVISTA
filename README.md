# NETVISTA

A visual **network digital twin** backed by a **real** emulated network. Mininet, Open vSwitch,
Linux routers and tc/netem carry real packets. The browser dashboard shows them live: you inject
latency, loss, bandwidth caps, link and router failures, and watch an adaptive routing
controller detect the fault and reroute. A SimPy twin of the same topology predicts the effect
of a change *before* you apply it, and a validation page measures how right it was.

An AI layer watches the same measurements. Each link and flow learns its own normal, so
faults too subtle for the fixed health rules are still caught. A root-cause engine names the
link or router that explains every bad and every healthy probe. A copilot (Claude, or a local
model through Ollama) answers questions from live data through tools, asks the twin "what if",
and proposes changes that only you can apply.

Every number in the live views comes from the emulation (interface counters, probe agents,
iperf3 receivers). Simulated values are always labelled **Simulation**, and AI-written text is
labelled **AI answer**, with every measured value checked against the data the model read.
Nothing is mocked.

See **[DESIGN.md](DESIGN.md)** for the architecture, every design decision, the validation
results and the limitations.

**Contents:** [Features](#features) · [Architecture](#architecture) · [How it works](#how-it-works)
· [Results](#results) · [1. Requirements](#1-requirements) · [2. Run it](#2-run-it-one-command)
· [3. Demo](#3-how-to-demo-each-phase) · [4. Tests](#4-tests) · [5. Layout](#5-project-layout)
· [6. Limitations](#6-known-limitations) · [7. Troubleshooting](#7-troubleshooting)

---

## Features

| Area | What you get |
|---|---|
| **Live network** | 11 devices (5 Linux routers, 2 Open vSwitch switches, 2 clients, 2 servers) in Mininet. The map shows real counters, probe RTT and loss, with packet comets driven by interface packets/s |
| **Chaos lab** | Reversible latency, loss, bandwidth caps, link-down, router crash and traffic bursts, applied with `tc`/`netem`/`ip link` |
| **Adaptive routing** | Own Dijkstra + Yen K-shortest paths, a latency/loss/load score, make-before-break policy routes, failure detection in ~1.3 s, reroute in ~15-55 ms |
| **Digital twin** | A SimPy model of the same topology, calibrated from live probes. Predict a change before applying it, then validate the prediction against the real network |
| **AI layer** | Learned-baseline anomaly detection, probe-path root-cause analysis, and a grounded LLM copilot that can only *propose* changes |
| **Packet journey, metrics, scenarios** | Per-hop kernel routing decisions, traceroute, 15 min metrics, record/replay and a one-button demo |

## Architecture

```mermaid
flowchart LR
    subgraph Browser["Browser (React, Cytoscape, Recharts)"]
        UI["Live map, What-if twin,<br/>Validation, AI ops, Copilot"]
    end

    subgraph API["FastAPI (REST + WebSocket 2 Hz + SSE)"]
        RT["Runtime<br/>owns every subsystem"]
    end

    subgraph WSL["WSL2 / Linux (root)"]
        EMU["emulation<br/>Mininet, OVS, tc/netem, iperf3"]
        TEL["telemetry<br/>probe agents + /proc counters"]
        CHAOS["chaos<br/>fault injection"]
        ROUTE["routing<br/>Dijkstra, Yen, score,<br/>policy-route installer"]
        SIM["simulator (separate process)<br/>SimPy twin + calibration"]
        VAL["validation<br/>predict, apply, measure, compare"]
        AI["ai<br/>anomaly detector, root cause,<br/>evaluation, copilot"]
    end

    LLM[("Claude API<br/>or local Ollama")]

    UI <-->|"REST, WebSocket, SSE"| RT
    RT --- EMU & TEL & CHAOS & ROUTE & SIM & VAL & AI
    CHAOS -->|"tc, ip link"| EMU
    TEL -->|"probes, counters"| EMU
    ROUTE -->|"ip route / ip rule"| EMU
    SIM -.->|"calibrated from"| TEL
    VAL --> SIM
    VAL --> CHAOS
    AI -.->|"reads measurements only"| TEL
    AI -->|"tool calls"| LLM
```

### The emulated topology

```mermaid
flowchart LR
    c1([Client 1]) --- sw1{{"Site A switch<br/>(OVS)"}}
    c2([Client 2]) --- sw1
    sw1 --- r1[R1 edge A]
    r1 ---|"50 Mbit/s, 5 ms"| r2[R2]
    r1 ---|"50 Mbit/s, 8 ms"| r3[R3]
    r2 ---|"50 Mbit/s, 3 ms"| r4[R4]
    r3 ---|"50 Mbit/s, 3 ms"| r4
    r2 ---|"30 Mbit/s, 5 ms (bottleneck)"| r5[R5 edge B]
    r3 ---|"50 Mbit/s, 8 ms"| r5
    r4 ---|"50 Mbit/s, 4 ms"| r5
    r5 --- sw2{{"Site B switch<br/>(OVS)"}}
    sw2 --- srv1([Server 1])
    sw2 --- srv2([Server 2])
```

Routers are Linux network namespaces, so every routing decision is a real kernel decision you
can inspect with `ip route get` and `traceroute`.

## How it works

### Failure detection and reroute

```mermaid
sequenceDiagram
    participant U as Operator
    participant C as Chaos lab
    participant N as Emulated network
    participant P as Probe agents (10 Hz)
    participant R as Routing controller
    participant A as AI layer
    U->>C: take link r2-r5 down
    C->>N: ip link set down (both ends)
    P--xN: echoes stop
    Note over P,R: no echo for 1.2 s (dead interval)
    P->>R: link declared dead (~1.3 s)
    R->>N: install new routes make-before-break (~15-55 ms)
    P->>R: first end-to-end echo on the new path
    Note over R: recovery measured (~1.45 s)
    P->>A: same probes, never the fault list
    A->>A: tomography names link r2-r5 as the cause
```

### AI pipeline

```mermaid
flowchart TD
    S["47 signals sampled at 1 Hz<br/>link RTT/loss/load, access, flow RTT/loss,<br/>iperf3 data loss"] --> D["Learned-baseline detector<br/>EWMA mean + spread per signal"]
    D -->|"z >= 4 for 3 s<br/>(or z >= 8 for 2 s)"| AN["Anomalies"]
    L["Probe liveness + paths"] --> T
    AN --> T["Probe-path tomography<br/>greedy cover: bad probes explained,<br/>good probes contradict"]
    T --> DX["Diagnosis<br/>element, fault type, confidence,<br/>alternatives, side effects"]
    DX --> MAP["Map marker +<br/>Likely cause tag"]
    DX --> CP["Copilot tools"]
    AN --> CP
```

```mermaid
stateDiagram-v2
    [*] --> Learning
    Learning --> Normal: 20 samples
    Normal --> Anomalous: z >= 4 for 3 s
    Anomalous --> Normal: z < 2 for 5 s
    Anomalous --> Anomalous: baseline frozen
    Normal --> Learning: Re-learn
```

### Copilot: it proposes, you decide

```mermaid
sequenceDiagram
    participant U as User
    participant UI as Copilot drawer
    participant AG as Agent loop
    participant M as LLM (Claude or Ollama)
    participant T as Tools
    participant N as Live network
    U->>UI: "Add 30 ms to r1-r3"
    UI->>AG: question + page context (SSE stream)
    AG->>M: question, tools
    M->>AG: call propose_change
    AG->>T: validate change
    T-->>AG: proposal card (NOT applied)
    AG-->>UI: tool row, card, answer, grounding check
    U->>UI: Predict impact first
    UI->>T: twin what-if (labelled Simulation)
    U->>UI: Apply
    UI->>N: chaos inject (logged as approved by the user)
```

The model has read tools (live state, AI insights, events), one simulate tool (the twin) and
propose tools. It has no tool that changes the network. After each answer, every measured
value is looked up in the data the model read; values it cannot trace are underlined.

### Validation loop

```mermaid
flowchart LR
    P["1. Predict<br/>(twin, frozen)"] --> A["2. Apply the same<br/>change live"]
    A --> S["3. Settle 8 s"]
    S --> M["4. Measure 12 s<br/>probes + iperf3"]
    M --> R["5. Revert"]
    R --> C["6. Compare<br/>latency, throughput,<br/>loss, path"]
```

## Results

All measured on the live emulation (Windows 11, WSL2 Ubuntu 24.04). Details in DESIGN.md.

| Measurement | Result |
|---|---|
| Failure detection / reroute / recovery | ~1.3 s / 15-55 ms / ~1.45 s |
| Twin error, RTT p50 | 0.21 % (p95: 1.52 %) |
| Twin error, bottleneck throughput | 0.09 % total, 1.06 % per flow |
| Twin loss error / paths predicted | 0.79 pp / 100 % |
| AI detector, 8 injected faults (2 runs) | 8 of 8 caught (threshold rules: 7 of 8, missed +2 ms) |
| AI root cause named correctly | 8 of 8 |
| AI false alarms in a quiet minute | 0 |
| AI mean time to detect | ~3.0 s (threshold rules ~1.7 s: slower on hard faults, better on subtle ones) |
| Tests | 133 unit + 2 Mininet smoke tests; `live_check.py` all checks passed |

---

## 1. Requirements

| | Windows 11 (how this was developed) | Linux |
|---|---|---|
| Emulation | WSL2 + Ubuntu 24.04 (`wsl --install -d Ubuntu-24.04`) | Ubuntu 22.04/24.04 |
| Root | `wsl -u root` (used by the launcher) | `sudo` |
| UI build | Node.js 18+ on Windows | Node.js 18+ |
| Inside Linux | installed automatically: mininet, openvswitch-switch, iperf3, traceroute, Python venv (fastapi, simpy, networkx, anthropic) | same |
| Copilot (optional) | an Anthropic API key in `.env`, **or** Ollama for Windows with a tool-capable model | an API key, or Ollama |

The WSL2 kernel (6.x) already ships the `openvswitch`, `sch_netem` and `sch_htb` modules. No VM
is needed.

## 2. Run it (one command)

**Windows** (from the repo folder, e.g. `C:\dev\NETVISTA`):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run.ps1
```

The first run installs the Linux packages inside WSL (a few minutes) and builds the UI. It then
starts the emulation and opens <http://127.0.0.1:8000>. Press Ctrl+C to stop; Mininet is cleaned
up automatically.

Options: `-Rebuild` (rebuild the UI), `-Topology topologies\small.json`, `-Port 8080`.

**Linux:**

```bash
cd frontend && npm install && npm run build && cd ..
sudo bash scripts/run.sh            # first run calls scripts/setup_wsl.sh
```

**UI development** (hot reload, backend still in WSL): `cd frontend && npm run dev`, then open
<http://localhost:5173> (it proxies `/api` and `/ws` to :8000).

**Copilot setup** (optional; anomaly detection and root-cause analysis work without it):

* **Claude (recommended):** copy `.env.example` to `.env` in the repo root, set
  `ANTHROPIC_API_KEY=...`, and restart. The default model is `claude-opus-5-5`. Set
  `NETVISTA_AI_MODEL=claude-sonnet-5-5` or `claude-haiku-5-5` for cheaper, faster answers.
* **Local, offline:** install [Ollama](https://ollama.com), pull a model with tool support
  (e.g. `ollama pull qwen3:8b`), and restart NETVISTA. It is found automatically, even though
  the backend runs in WSL (through Windows' `curl.exe` via WSL interop). On a CPU-only laptop
  expect one to four minutes per answer.
* The **AI ops** page shows which model is in use. Its **Check again** button re-detects
  without a restart.

## 3. How to demo each phase

Start the system, then on the **Live network** page:

### Phase 1: real topology and live visualisation
1. The map shows the real equipment: 5 routers, 2 Open vSwitch site switches, 2 laptops and 2
   servers, grouped into Site A, the routed core and Site B. Each device has a status LED
   (measured health) and an activity LED that blinks with its measured packets/s. Grey specks
   on the cables are the probe traffic that is always flowing.
2. Click **Start traffic**. Two iperf3 UDP flows (6 Mbit/s each) start and draw themselves in
   as coloured fibres along their paths. Comets in the flow's colour stream along them, and
   the loaded cables brighten and glow.
3. Hover any cable or device for a quick card (load, round trip, which flows it carries).
   Click it for the full inspector: utilisation and RTT sparklines, per-direction rate,
   packets/s, drops and queue, and RTT percentiles vs design. Hover a flow chip in the legend
   to follow that flow on its own.
4. Toggle **Load / Latency** to make the cables glow with utilisation or with how much slower
   than designed they measure. **Fit to view** resets pan and zoom.

### Phase 2: chaos lab
1. With a link selected: **Add latency** (+60 ms), **Drop packets** (5 %), **Cap bandwidth**,
   **Take link down**. With a router selected: **Crash**. With a client selected: **Start burst**.
2. Each fault appears under **Active faults** with its own **Revert**, and in the **Event log**.
3. The effect is real: open the link inspector and watch the measured RTT jump by 2 × the
   added latency.

### Phase 3: adaptive routing
1. **Path selection** shows each flow's candidate paths with latency, loss, load and score,
   re-scored every second, with the chosen path highlighted.
2. Take r2-r5 down. After ~1.3 s the cable turns red with a pulsing break, R2 and R5 get amber
   rings, the fibres redraw themselves along r1-r2-r4-r5 while the old route fades, and
   **Failure handling** lists detection (≈1.3 s), reroute (≈15–25 ms) and recovery (≈1.45 s).
   Crash a router to see it go dark with a red pulse.
3. Revert, switch **Routing policy** to *Static (Dijkstra)*, and repeat. Static routing does
   not react: the flow table shows "no echo" and data loss until you revert.
4. Change the score weights (e.g. loss weight 20) and see the path choice change.

### Phase 4: digital twin and validation
1. **What-if twin** page: add changes (e.g. +40 ms on r2-r5, or Router r2 down), press
   **Predict**. The LIVE map and the dashed SIMULATION map appear side by side, with a table of
   Live now / Twin unchanged / Twin what-if for RTT p50/p95/p99, loss, throughput and path. The
   twin also predicts whether the adaptive controller will reroute.
2. **Validate live** applies the same change to the real network, measures, reverts and
   compares.
3. **Validation** page → **Run validation suite** (~7 min): 11 scenarios in static and adaptive
   mode, with latency, throughput, loss and path errors per run, charts and drill-down tables.

### Phase 5: polish
* **Packet journey**: pick a source and destination. The path is highlighted, and every hop
  shows its live kernel decision (`ip route get`), its routing table and its outgoing link's
  metrics. **Run real traceroute** runs traceroute inside the source namespace and checks it
  against the installed path.
* **Metrics**: throughput, data loss, probe loss and RTT p50/p95/p99 over 1/5/15 minutes.
* **Scenarios & demo**: **Run demo** (also in the header) does a ~45 s live sequence: start
  traffic → +80 ms on the main flow's link → reroute → crash a router on the new path →
  recover → summary with measured timings. **Record a scenario**, do things, **Stop and save**,
  then **Replay live** re-executes the same actions at the same offsets.

### Phase 6: AI layer
1. **Subtle fault the rules miss.** With traffic running, click the r2-r5 cable, set Latency
   to 2 ms in the inspector and press **Add latency**. The cable stays green: the threshold rule
   only fires above about 17 ms. Within about 3 s,
   **AI insights** says "Link r2-r5 has extra latency", the map shows a dotted AI marker on the
   cable and a **Likely cause** tag, and the event log records the anomaly.
2. **Root cause, not symptoms.** Crash router r2. Three links go dark, but the diagnosis is one
   cause, "Router r2 is down" at high confidence. It lists the flows the controller moved off it,
   and reports their higher RTT as a side effect, not a second fault.
3. **Copilot.** Press **Copilot** (or Ctrl+K), or **Explain** on a diagnosis. Each answer shows:
   * the tools it used, with the raw data one click away;
   * a dotted "AI answer" label;
   * how many of its measured values were traced to that data (any that weren't are
     underlined).

   Try "What would happen if router r2 crashed?" (it runs the twin), "Add 50 ms to r1-r2"
   (it proposes a card: **Predict impact first**, then **Apply**), or **Post-mortem** on an
   incident.
4. **AI ops page.** Every watched signal with its learned normal and alarm line, a chart of
   measured vs learned normal, and the copilot's status. **Run evaluation** (~5 min) injects 8
   real faults and compares the learned detector with the threshold rules, plus the diagnosis
   accuracy.

## 4. Tests

```bash
# unit tests (routing, scoring, simulator, parsers, validation maths, AI detector / diagnosis / copilot)
wsl -d Ubuntu-24.04 -u root -- bash -c "cd /mnt/c/dev/NETVISTA/backend && /opt/netvista/venv/bin/python -m pytest -m 'not emulation'"
# smoke tests that boot real Mininet topologies (stop the app first: same interface names)
wsl -d Ubuntu-24.04 -u root -- bash -c "cd /mnt/c/dev/NETVISTA/backend && /opt/netvista/venv/bin/python -m pytest -m emulation"
# end-to-end acceptance against the running app
wsl -d Ubuntu-24.04 -u root -- /opt/netvista/venv/bin/python /mnt/c/dev/NETVISTA/scripts/live_check.py
# calibration + full twin validation suite against the running app
wsl -d Ubuntu-24.04 -u root -- /opt/netvista/venv/bin/python /mnt/c/dev/NETVISTA/scripts/twin_check.py --suite
```

Results at the time of writing: 132 unit tests and 2 emulation smoke tests pass,
`live_check.py` reports ALL CHECKS PASSED, and the suite figures are in DESIGN.md §9.

## 5. Project layout

```
topologies/          default.json (5 routers, 2 switches, 2 clients, 2 servers), small.json
backend/netvista/
  topology/          JSON model + validation, derived IP addressing plan
  emulation/         Mininet builder, tc/netem/htb, nsenter helpers, iperf3 traffic manager
  telemetry/         probe agent (runs in each namespace), /proc + tc counters, health views
  chaos/             reversible fault injection mapped to tc / ip link
  routing/           own Dijkstra + Yen, path score, policy-route installer, controller
  simulator/         SimPy packet model, model-based calibration, what-if + routing prediction
  validation/        predict → apply live → measure → compare
  scenarios/         record / replay, scripted demo
  ai/                signals, learned-baseline detector, probe-path root cause, evaluation
    copilot/         LLM agent: providers (Claude / Ollama), tools, grounding check, prompt
  api/               FastAPI REST + WebSocket, serves the built UI
  runtime.py         wires everything together, composes the live snapshot
backend/tests/       pytest (unit + Mininet smoke tests)
frontend/src/        React + TypeScript + Tailwind + Cytoscape.js + Recharts
scripts/             run.ps1 (Windows), run.sh / stop.sh / setup_wsl.sh (Linux/WSL), checks
runs/                event log, calibration, validation + AI evaluation runs, scenarios (git-ignored)
.env.example         copilot settings (copy to .env, git-ignored)
```

## 6. Known limitations

Short version (details in DESIGN.md §14):

* Failure detection is probe-based (10 Hz, 1.2 s dead interval), so it takes ~1.3 s, like BFD.
  It is not sub-second.
* L2 segments behind a switch are probed only as host → gateway.
* Under heavy tail-drop overload the twin predicts the bottleneck's total goodput and the
  queueing delay accurately, but not how the loss is split between flows (that depends on
  kernel packet micro-timing).
* Traffic is UDP CBR (iperf3). TCP dynamics are not modelled.
* Scale is tens of nodes (Python threads per probe stream, packet-level twin).
* The AI detector is bounded by the probe rate (10/s). About 1 % random loss takes many seconds
  to separate from chance, unless iperf3 traffic crosses the link.
* The diagnosis is greedy (one element per symptom set). A switch and its uplink cannot be
  told apart, so both are shown.
* A local LLM on a CPU-only laptop takes minutes per answer. Claude takes seconds, but needs an
  API key.
* eBPF/XDP telemetry and a topology editor were not built.

## 7. Troubleshooting

* **"The emulated network did not start"**: run the launcher (needs root in WSL). If Open
  vSwitch is the problem, start with `NETVISTA_SWITCH=linuxbridge` (needs `bridge-utils`).
* **Stale state after a crash**: `wsl -d Ubuntu-24.04 -u root -- bash /mnt/c/dev/NETVISTA/scripts/stop.sh`
  (kills agents and iperf3, runs `mn -c`).
* **Backend log**: the launcher prints it to the console. Events are also appended to
  `runs/events.jsonl`.
