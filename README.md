# NETVISTA

A visual **network digital twin** backed by a **real** emulated network. Mininet, Open vSwitch,
Linux routers and tc/netem carry real packets. The browser dashboard shows them live: you inject
latency, loss, bandwidth caps, link and router failures, and watch an adaptive routing
controller detect the fault and reroute. A SimPy twin of the same topology predicts the effect
of a change *before* you apply it, and a validation page measures how right it was.

Every number in the live views comes from the emulation (interface counters, probe agents,
iperf3 receivers). Simulated values are always labelled **Simulation**. Nothing is mocked.

See **[DESIGN.md](DESIGN.md)** for the architecture, every design decision, the validation
results and the limitations.

---

## 1. Requirements

| | Windows 11 (how this was developed) | Linux |
|---|---|---|
| Emulation | WSL2 + Ubuntu 24.04 (`wsl --install -d Ubuntu-24.04`) | Ubuntu 22.04/24.04 |
| Root | `wsl -u root` (used by the launcher) | `sudo` |
| UI build | Node.js 18+ on Windows | Node.js 18+ |
| Inside Linux | installed automatically: mininet, openvswitch-switch, iperf3, traceroute, Python venv (fastapi, simpy, networkx) | same |

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

## 3. How to demo each phase

Start the system, then on the **Live network** page:

### Phase 1: real topology and live visualisation
1. The map shows 5 routers, 2 OVS switches, 2 clients and 2 servers. Node rings show measured
   health. Grey dots are the probe traffic that is always flowing.
2. Click **Start traffic**. Two iperf3 UDP flows (6 Mbit/s each) start. Their fibre-coloured
   strands appear along their paths, the dots get denser and take the flow's colour, and the
   loaded links get brighter and wider.
3. Click a link (e.g. r2-r5) to see live utilisation and RTT sparklines, per-direction rate,
   packets/s, drops and queue, plus RTT percentiles vs the designed RTT. Click a node to see its
   interfaces and counters.
4. Toggle **Load / Latency** to colour links by utilisation or by how slow they are vs design.

### Phase 2: chaos lab
1. With a link selected: **Add latency** (+60 ms), **Drop packets** (5 %), **Cap bandwidth**,
   **Take link down**. With a router selected: **Crash**. With a client selected: **Start burst**.
2. Each fault appears under **Active faults** with its own **Revert**, and in the **Event log**.
3. The effect is real: open the link inspector and watch the measured RTT jump by 2 × the
   added latency.

### Phase 3: adaptive routing
1. **Path selection** shows each flow's candidate paths with latency, loss, load and score,
   re-scored every second, with the chosen path highlighted.
2. Take r2-r5 down. After ~1.3 s the link turns red and dashed, the strands jump to r1-r2-r4-r5,
   and **Failure handling** lists detection (≈1.3 s), reroute (≈15–25 ms) and recovery (≈1.45 s).
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

## 4. Tests

```bash
# unit tests (routing algorithms, scoring, simulator, parsers, validation maths)
wsl -d Ubuntu-24.04 -u root -- bash -c "cd /mnt/c/dev/NETVISTA/backend && /opt/netvista/venv/bin/python -m pytest -m 'not emulation'"
# smoke tests that boot real Mininet topologies (stop the app first: same interface names)
wsl -d Ubuntu-24.04 -u root -- bash -c "cd /mnt/c/dev/NETVISTA/backend && /opt/netvista/venv/bin/python -m pytest -m emulation"
# end-to-end acceptance against the running app
wsl -d Ubuntu-24.04 -u root -- /opt/netvista/venv/bin/python /mnt/c/dev/NETVISTA/scripts/live_check.py
# calibration + full twin validation suite against the running app
wsl -d Ubuntu-24.04 -u root -- /opt/netvista/venv/bin/python /mnt/c/dev/NETVISTA/scripts/twin_check.py --suite
```

Results at the time of writing: 106 unit tests and 2 emulation smoke tests pass,
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
  api/               FastAPI REST + WebSocket, serves the built UI
  runtime.py         wires everything together, composes the live snapshot
backend/tests/       pytest (unit + Mininet smoke tests)
frontend/src/        React + TypeScript + Tailwind + Cytoscape.js + Recharts
scripts/             run.ps1 (Windows), run.sh / stop.sh / setup_wsl.sh (Linux/WSL), checks
runs/                event log, calibration, validation runs, saved scenarios (git-ignored)
```

## 6. Known limitations

Short version (details in DESIGN.md §13):

* Failure detection is probe-based (10 Hz, 1.2 s dead interval), so it takes ~1.3 s, like BFD.
  It is not sub-second.
* L2 segments behind a switch are probed only as host → gateway.
* Under heavy tail-drop overload the twin predicts the bottleneck's total goodput and the
  queueing delay accurately, but not how the loss is split between flows (that depends on
  kernel packet micro-timing).
* Traffic is UDP CBR (iperf3). TCP dynamics are not modelled.
* Scale is tens of nodes (Python threads per probe stream, packet-level twin).
* The optional extensions (eBPF/XDP telemetry, LLM explanations, topology editor) were not
  built.

## 7. Troubleshooting

* **"The emulated network did not start"**: run the launcher (needs root in WSL). If Open
  vSwitch is the problem, start with `NETVISTA_SWITCH=linuxbridge` (needs `bridge-utils`).
* **Stale state after a crash**: `wsl -d Ubuntu-24.04 -u root -- bash /mnt/c/dev/NETVISTA/scripts/stop.sh`
  (kills agents and iperf3, runs `mn -c`).
* **Backend log**: the launcher prints it to the console. Events are also appended to
  `runs/events.jsonl`.
