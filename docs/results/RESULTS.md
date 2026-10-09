# Results (netvista-default)

Exported by `scripts/export_results.py` from `runs/`. Every number was measured on the live emulation.

## Live benchmark

10 failures × 4 strategies, 34 min; traffic c1→srv1 12, c1→srv2 8, c2→srv1 8, c2→srv2 12 Mbit/s.

| Strategy | Weighted intent-s violated | of which new | Mean time to compliance | Never compliant | Route changes | Delivered | Prediction right |
|---|---|---|---|---|---|---|---|
| Static (Dijkstra) | 6730 | 0 | 1.0 s | 0 | 0 | 51.27 % | 40 % |
| Adaptive, no herd guard | 2516 | 711 | 2.0 s | 4 | 29 | 98.05 % | 30 % |
| Adaptive + herd guard | 2597 | 777 | 6.0 s | 3 | 26 | 97.81 % | 70 % |
| Intent plan (Assure) | 1517 | 1117 | 4.8 s | 4 | 12 | 98.39 % | 80 % |

Per failure (weighted intent-seconds violated in the 20 s after the failure):

| Failure | Static (Dijkstra) | Adaptive, no herd guard | Adaptive + herd guard | Intent plan (Assure) |
|---|---|---|---|---|
| Link r1-r2 fails | 663 | 425 | 409 | 389 |
| Link r1-r3 fails | 660 * | 3 * | 216 | 241 |
| Link r2-r4 fails | 700 * | 200 * | 353 | 0 |
| Link r3-r4 fails | 670 * | 200 * | 209 | 20 |
| Link r2-r5 fails | 660 | 200 * | 252 * | 409 * |
| Link r3-r5 fails | 707 * | 403 * | 230 * | 0 |
| Link r4-r5 fails | 700 * | 221 * | 192 * | 70 * |
| Router r2 fails | 660 | 412 | 412 | 376 |
| Router r3 fails | 650 * | 63 * | 122 | 12 |
| Router r4 fails | 660 | 389 | 202 | 0 |

\* the measured set of broken intents differed from the failure analysis' prediction.

All complete runs (weighted intent-s violated, route changes in brackets):

| Run | Static (Dijkstra) | Adaptive, no herd guard | Adaptive + herd guard | Intent plan (Assure) |
|---|---|---|---|---|
| 2026-10-09 20:45 | 6652 (0) | 3859 (84) | 2827 (31) | 1268 (18) |
| 2026-10-09 22:23 (latest) | 6730 (0) | 2516 (29) | 2597 (26) | 1517 (12) |

## Twin validation (both engines against the live network)

| Scenario | Routing | RTT p50 packet / fluid | RTT p95 packet / fluid | Bottleneck total packet / fluid | Loss (per flow) | Paths |
|---|---|---|---|---|---|---|
| Baseline (no change) | adaptive | 0.25 / 0.25 % | 0.37 / 0.49 % | 0.19 / 0.00 % | 0.00 pp | 100 % |
| +40 ms latency on r2-r5 | static | 0.04 / 0.13 % | 0.08 / 0.28 % | 0.05 / 0.00 % | 0.00 pp | 100 % |
| +40 ms latency on r2-r5 | adaptive | 0.10 / 0.23 % | 0.25 / 0.42 % | 0.35 / 0.00 % | 0.00 pp | 100 % |
| 5% loss on r2-r5 | static | 0.16 / 0.62 % | 0.30 / 1.03 % | 0.16 / 0.11 % | 0.05 pp | 100 % |
| 5% loss on r2-r5 | adaptive | 0.08 / 0.21 % | 0.16 / 0.33 % | 0.35 / 0.00 % | 0.00 pp | 100 % |
| Bandwidth cap 14 Mbit/s on r2-r5 (89% load) | static | 0.24 / 1.17 % | 0.58 / 2.59 % | 0.04 / 0.00 % | 0.00 pp | 100 % |
| Bandwidth cap 8 Mbit/s on r2-r5 (overload) | static | 0.74 / 2.10 % | 0.36 / 1.35 % | 0.00 / 0.13 % | 7.65 pp | 100 % |
| Burst 20 Mbit/s c2→srv1 (overload) | static | 0.29 / 0.69 % | 1.77 / 1.04 % | 0.19 / 0.26 % | 7.37 pp | 100 % |
| Burst 20 Mbit/s c2→srv1 | adaptive | 0.03 / 0.43 % | 0.12 / 0.89 % | 0.05 / 0.00 % | 0.00 pp | 100 % |
| Link r2-r5 down | adaptive | 0.40 / 0.27 % | 3.12 / 2.98 % | 0.35 / 0.00 % | 0.00 pp | 100 % |
| Router r2 down | adaptive | 0.15 / 0.33 % | 0.27 / 0.51 % | 0.08 / 0.00 % | 0.00 pp | 100 % |

## Drills

| Failure | Routing | Intent outcomes as predicted | Paths as predicted | RTT p50 err fluid / packet |
|---|---|---|---|---|
| Link r2-r5 fails | intent (P1) | 100 % | yes | 0.82 / 0.22 % |
| Link r2-r5 fails | intent (P1) | 100 % | yes | 1.75 / 1.26 % |
| Link r2-r5 fails | intent (P2) | 100 % | yes | 1.62 / 1.46 % |

