# healthcheck

A small [Co-op Cloud](https://coopcloud.tech) / `abra` recipe that periodically
checks a node and its Swarm stacks for common problems and emails you when
something is wrong — using your own [Postal](https://postalserver.io) server's
HTTP API to send the mail.

It is scheduled by [`swarm-cronjob`](https://recipes.coopcloud.tech/swarm-cronjob),
the same way `backup-bot-two` runs on a timer. The check itself is a single
standard-library Python script mounted as a Docker config, so there is no custom
image to build or publish.

## What it checks

- Disk usage per mount point (host filesystems, via a read-only `/host` mount)
- Inode usage per mount point (Docker layers and logs exhaust inodes long before bytes)
- RAM and swap usage
- 5-minute load average per CPU core
- Swarm services running below their desired replica count
- Containers reporting an `unhealthy` healthcheck

Alerts are **debounced**: a problem is emailed once, then stays quiet for
`COOLDOWN_HOURS`, and you get a short "recovered" note when it clears. All new
problems in a run are batched into a single email.

## Requirements

- A Docker Swarm managed by `abra`.
- `swarm-cronjob` deployed on the swarm:
  `abra app new swarm-cronjob` then `abra app deploy <swarm-cronjob domain>`.
- A Postal **API** credential (type must be `API`, not `SMTP`) and a `from`
  address on a domain your Postal server owns.

## Install

```sh
# Make the recipe available to abra
git clone https://github.com/<you>/healthcheck ~/.abra/recipes/healthcheck

# Create the app (use any name; it has no web domain)
abra app new healthcheck

# Configure it
abra app config <name>          # set LABEL, ALERT_TO, ALERT_FROM, POSTAL_URL, thresholds

# Store the Postal API key as a secret
abra app secret insert <name> postal_api_key v1 "your-postal-api-credential-key"

# Deploy
abra app deploy <name>
```

## Configuration

All settings live in the app's `.env` (see `.env.sample`):

| Variable | Purpose |
| --- | --- |
| `LABEL` | Node name shown in alert subjects/bodies |
| `CRON_SCHEDULE` | swarm-cronjob schedule, 5-field cron (default every 5 min) |
| `ALERT_TO` | Comma-separated recipients |
| `ALERT_FROM` | Sender; must be a Postal-owned domain |
| `POSTAL_URL` | Base URL of your Postal install |
| `DISK_PCT` / `INODE_PCT` / `MEM_PCT` / `SWAP_PCT` | Percentage thresholds |
| `LOAD_PER_CORE` | Load-average-per-core threshold |
| `COOLDOWN_HOURS` | Minimum gap before re-sending an unchanged alert |
| `MOUNTS` | Space-separated host mount points to watch |
| `DOCKER_HOST` | Docker API endpoint (the bundled socket-proxy) |

The Postal key is **not** an env var — it is an `abra` secret mounted at
`/run/secrets/postal_api_key`.

## Testing

The `app` service runs at 0 replicas (swarm-cronjob scales it up on schedule),
so run a one-off container to test:

```sh
abra app run <name> app -- python3 /healthcheck.py
```

No output and no email means everything is healthy. To force a real alert,
temporarily set a low threshold (e.g. `DISK_PCT=1`) with `abra app config`,
redeploy, and run it again. Send failures are printed to stderr — for example
`InvalidServerAPIKey` means the credential is not of type API, and a
From/Sender error means `ALERT_FROM` is not on a Postal-owned domain.

## Adding checks

Inside `collect()` in `healthcheck.py`, detect a condition and, if it's bad, set
`firing["some:key"] = "message"`. The debounce, batching, and recovery logic all
key off that dictionary, so new checks need no extra plumbing. Bump
`HEALTHCHECK_VERSION` in `.env` whenever you change the script so `abra` rolls
out the new config.

## Scope and caveats

This runs **inside a container**, which shapes what it can see:

- Memory, swap, and load average reflect host-wide values, so those work as-is.
- Disk and inode checks rely on the read-only `/:/host:ro` mount; `MOUNTS` are
  host paths and are read through `/host`.
- There is **no systemd `--failed` check** — there's no systemd in a container.
  Keep that (and any other purely host-level checks) on a host-level systemd
  timer running the script directly.
- On a multi-node swarm the cron task only inspects the node it lands on for the
  host-level (disk/inode/mem/load) checks. The placement constraint pins it to a
  manager; deploy one instance per node you want covered, or keep host checks on
  per-node timers. The Swarm and container checks are cluster-wide regardless.

A useful split: run the container/volume/service checks here (uniform with the
rest of your `abra` setup) and keep the host-level checks on a node-level
systemd timer — the latter still fires even if the Docker daemon or swarm is
unhealthy, which is exactly when you want the alert.

## License

MIT — see `LICENSE`. Set your name/year before publishing.
