# NETVISTA in Docker.
#
#   docker build -t netvista .
#   docker run --rm -it --privileged -p 8000:8000 -v netvista-runs:/app/runs --env-file .env netvista
#
# --privileged is required: Mininet creates network namespaces, veth pairs and tc qdiscs, and
# Open vSwitch needs the host kernel's openvswitch / sch_netem / sch_htb modules (the WSL2 kernel
# behind Docker Desktop ships them). Without it the container starts but the emulation cannot.

# ---- 1. build the UI
FROM node:22-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- 2. runtime: Mininet + Open vSwitch + iperf3 + the Python backend
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive \
    NETVISTA_VENV=/opt/netvista/venv \
    PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
      mininet openvswitch-switch iperf3 iproute2 iputils-ping traceroute \
      ethtool net-tools tcpdump psmisc procps bridge-utils kmod curl ca-certificates \
      python3 python3-venv python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
# --system-site-packages so "import mininet" (installed by apt) works inside the venv
RUN python3 -m venv --system-site-packages "$NETVISTA_VENV" \
    && "$NETVISTA_VENV/bin/pip" install --no-cache-dir -r backend/requirements.txt

COPY backend/ backend/
COPY topologies/ topologies/
COPY scripts/ scripts/
COPY --from=ui /ui/dist frontend/dist
RUN sed -i 's/\r$//' scripts/*.sh && chmod +x scripts/*.sh

# runs/ holds the event log, calibration, residuals, drills and benchmarks: mount a volume on it
VOLUME /app/runs
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=40s \
  CMD curl -fs http://127.0.0.1:8000/api/health | grep -q running || exit 1

# docker/entrypoint.sh starts Open vSwitch, then hands over to scripts/run.sh
COPY docker/entrypoint.sh /usr/local/bin/netvista-entrypoint
RUN sed -i 's/\r$//' /usr/local/bin/netvista-entrypoint && chmod +x /usr/local/bin/netvista-entrypoint
ENTRYPOINT ["netvista-entrypoint"]
CMD []
