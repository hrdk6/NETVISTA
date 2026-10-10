#!/usr/bin/env bash
# Container start-up: check the privileges NETVISTA needs, start Open vSwitch, run the app.
set -u

if ! ip link add nvtest0 type veth peer name nvtest1 2>/dev/null; then
  cat >&2 <<'MSG'
!! This container cannot create network devices. Run it with --privileged:
     docker run --rm -it --privileged -p 8000:8000 netvista
MSG
  exit 1
fi
ip link del nvtest0 2>/dev/null || true

# the modules come from the host kernel; this is a no-op when they are built in or already loaded
for m in openvswitch sch_netem sch_htb; do modprobe "$m" 2>/dev/null || true; done

mkdir -p /var/run/openvswitch /var/log/openvswitch /etc/openvswitch
if ! /usr/share/openvswitch/scripts/ovs-ctl start --system-id=random >/tmp/ovs-start.log 2>&1; then
  echo "!! Open vSwitch did not start (needs the host kernel's openvswitch module):" >&2
  cat /tmp/ovs-start.log >&2
  echo "   Fall back to Linux bridges with:  -e NETVISTA_SWITCH=linuxbridge" >&2
  [ "${NETVISTA_SWITCH:-ovs}" = ovs ] && exit 1
fi

# Ctrl+C / docker stop: let the app clean up Mininet itself
exec bash /app/scripts/run.sh "$@"
