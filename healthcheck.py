#!/usr/bin/env python3
"""
healthcheck.py — periodic node/stack health checks with debounced email alerts
via Postal's HTTP API. Standard library only; intended to run as a one-shot
container scheduled by swarm-cronjob.

Differences from a host-level run:
  * Host filesystems are read through a read-only bind mount (HOST_PREFIX, /host).
  * Docker/Swarm checks talk to the Docker Engine API over a socket-proxy (DOCKER_HOST).
  * Config comes from environment variables; the Postal key comes from a secret.
  * There is no systemd check (no systemd inside a container) — keep that on a
    host-level systemd timer instead.
"""

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def env(name, default):
    return os.environ.get(name, default)


# ===== Configuration (from environment) ==============================
LABEL          = env("LABEL", socket.gethostname())
ALERT_TO       = [x.strip() for x in env("ALERT_TO", "").split(",") if x.strip()]
ALERT_FROM     = env("ALERT_FROM", "")
POSTAL_URL     = env("POSTAL_URL", "").rstrip("/")
STATE_FILE     = env("STATE_FILE", "/var/lib/vps-healthcheck/state.json")
COOLDOWN_HOURS = float(env("COOLDOWN_HOURS", "6"))

DISK_PCT       = int(env("DISK_PCT", "85"))
INODE_PCT      = int(env("INODE_PCT", "85"))
MEM_PCT        = int(env("MEM_PCT", "90"))
SWAP_PCT       = int(env("SWAP_PCT", "80"))
LOAD_PER_CORE  = float(env("LOAD_PER_CORE", "2.0"))

MOUNTS         = env("MOUNTS", "/").split()
HOST_PREFIX    = env("HOST_PREFIX", "/host")
DOCKER_HOST    = env("DOCKER_HOST", "tcp://socket-proxy:2375")
SECRET_PATH    = env("POSTAL_API_KEY_FILE", "/run/secrets/postal_api_key")
# =====================================================================


def api_key():
    if os.path.exists(SECRET_PATH):
        with open(SECRET_PATH) as fh:
            return fh.read().strip()
    return env("POSTAL_API_KEY", "")


def host_path(p):
    """Map a host path through the bind mount if it's available."""
    hp = os.path.join(HOST_PREFIX, p.lstrip("/"))
    return hp if os.path.exists(hp) else p


def meminfo():
    info = {}
    try:
        with open(host_path("/proc/meminfo")) as fh:
            for line in fh:
                parts = line.replace(":", "").split()
                if len(parts) >= 2 and parts[1].isdigit():
                    info[parts[0]] = int(parts[1])  # kB
    except OSError:
        pass
    return info


def docker_api(path):
    """GET JSON from the Docker Engine API via the socket-proxy."""
    base = DOCKER_HOST.replace("tcp://", "http://")
    try:
        with urllib.request.urlopen(f"{base}{path}", timeout=15) as r:
            return json.load(r)
    except (urllib.error.URLError, ValueError, OSError) as e:
        print(f"healthcheck: docker API {path} failed: {e}", file=sys.stderr)
        return None


def collect():
    """Return {alert_key: human_message} for everything currently wrong."""
    firing = {}

    # Disk space + inodes (host filesystems via the bind mount)
    for m in MOUNTS:
        target = host_path(m)
        if not os.path.isdir(target):
            continue
        try:
            s = os.statvfs(target)
        except OSError:
            continue
        used  = (s.f_blocks - s.f_bfree) * s.f_frsize
        avail = s.f_bavail * s.f_frsize
        if used + avail > 0:
            pct = round(100 * used / (used + avail))
            if pct >= DISK_PCT:
                firing[f"disk:{m}"] = f"Disk usage on {m} is {pct}% (threshold {DISK_PCT}%)."
        if s.f_files > 0:
            ipct = round(100 * (s.f_files - s.f_ffree) / s.f_files)
            if ipct >= INODE_PCT:
                firing[f"inode:{m}"] = f"Inode usage on {m} is {ipct}% (threshold {INODE_PCT}%)."

    # Memory + swap
    mi = meminfo()
    if mi.get("MemTotal", 0) > 0:
        avail = mi.get("MemAvailable", mi["MemTotal"])
        mem_pct = round(100 * (mi["MemTotal"] - avail) / mi["MemTotal"])
        if mem_pct >= MEM_PCT:
            firing["mem"] = f"RAM usage is {mem_pct}% (threshold {MEM_PCT}%)."
    if mi.get("SwapTotal", 0) > 0:
        sw_pct = round(100 * (mi["SwapTotal"] - mi.get("SwapFree", 0)) / mi["SwapTotal"])
        if sw_pct >= SWAP_PCT:
            firing["swap"] = f"Swap usage is {sw_pct}% (threshold {SWAP_PCT}%)."

    # Load average (5-minute) per core
    try:
        with open(host_path("/proc/loadavg")) as fh:
            load5 = float(fh.read().split()[1])
        cores = os.cpu_count() or 1
        if load5 / cores >= LOAD_PER_CORE:
            firing["load"] = (f"5-min load average is {load5} across {cores} core(s) "
                              f"(>= {LOAD_PER_CORE}/core).")
    except (OSError, ValueError):
        pass

    # Swarm services below their desired replica count
    services = docker_api("/services")
    tasks = docker_api("/tasks")
    if services is not None and tasks is not None:
        running = {}
        for t in tasks:
            if t.get("DesiredState") == "running" and \
               t.get("Status", {}).get("State") == "running":
                sid = t.get("ServiceID")
                running[sid] = running.get(sid, 0) + 1
        for svc in services:
            mode = svc.get("Spec", {}).get("Mode", {})
            want = mode.get("Replicated", {}).get("Replicas")
            if want is None:           # skip global / job modes
                continue
            name = svc.get("Spec", {}).get("Name", svc.get("ID", "?"))
            have = running.get(svc.get("ID"), 0)
            if have < want:
                firing[f"svc:{name}"] = f"Swarm service {name} has {have}/{want} replicas running."

    # Containers reporting unhealthy
    flt = urllib.parse.quote(json.dumps({"health": ["unhealthy"]}))
    conts = docker_api(f"/containers/json?filters={flt}")
    if conts:
        names = [c["Names"][0].lstrip("/") for c in conts if c.get("Names")]
        if names:
            firing["unhealthy"] = "Unhealthy containers: " + ", ".join(names) + "."

    return firing


def load_state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE_FILE)  # atomic


def send_email(subject, body):
    if not POSTAL_URL or not ALERT_TO or not ALERT_FROM:
        print("healthcheck: POSTAL_URL / ALERT_TO / ALERT_FROM not configured", file=sys.stderr)
        return False
    payload = json.dumps({
        "to": ALERT_TO,
        "from": ALERT_FROM,
        "subject": subject,
        "plain_body": body,
    }).encode()
    req = urllib.request.Request(
        f"{POSTAL_URL}/api/v1/send/message",
        data=payload,
        headers={"Content-Type": "application/json",
                 "X-Server-API-Key": api_key()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.load(resp)
    except urllib.error.URLError as e:
        print(f"healthcheck: could not reach Postal: {e}", file=sys.stderr)
        return False
    if result.get("status") != "success":
        print(f"healthcheck: Postal rejected the message: {result}", file=sys.stderr)
        return False
    return True


def main():
    firing = collect()
    state = load_state()                 # {alert_key: last_sent_epoch}
    now = int(time.time())
    cooldown = COOLDOWN_HOURS * 3600

    to_send = []
    new_state = {}
    for key, msg in firing.items():
        last = state.get(key)
        if last is None or now - last >= cooldown:
            to_send.append(msg)          # new, or cooldown expired
            new_state[key] = now
        else:
            new_state[key] = last        # still cooling down; keep timestamp

    recovered = [k for k in state if k not in firing]

    if to_send:
        lines = "\n".join(f"  - {m}" for m in to_send)
        body = (f"Health alerts on {LABEL} at "
                f"{time.strftime('%Y-%m-%d %H:%M:%S %Z')}:\n\n{lines}\n")
        if recovered:
            body += f"\nRecovered since last run: {', '.join(recovered)}\n"
        if send_email(f"[{LABEL}] {len(to_send)} health alert(s)", body):
            save_state(new_state)
    elif recovered:
        body = f"These issues on {LABEL} have cleared:\n  {', '.join(recovered)}\n"
        if send_email(f"[{LABEL}] recovered: {', '.join(recovered)}", body):
            save_state(new_state)
    else:
        save_state(new_state)


if __name__ == "__main__":
    main()
