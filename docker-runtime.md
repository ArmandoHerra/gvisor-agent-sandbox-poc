# Docker Runtime Setup — gVisor (runsc)

This PoC runs its agent container under [gVisor](https://gvisor.dev/) (`runsc`), an
application kernel that filters syscalls between the container and the host. Docker
does not ship with runsc, so before `make run` or `make verify-gvisor` will work you
need to install it and register it as a Docker runtime.

This document is the full apply/revert runbook: how to capture your machine's current
Docker state, enable runsc, verify it, and return your machine to exactly the state it
was in before.

## Key principle: runsc is an *additional* runtime, never the default

Every target in this repo's Makefile passes `--runtime=runsc` per container
(`RUNTIME := runsc`). Registering runsc as an extra runtime is therefore enough —
**do not set `"default-runtime": "runsc"`** in `/etc/docker/daemon.json`. With only the
additional registration, all your other containers keep running under stock `runc`
exactly as before; only containers explicitly started with `--runtime=runsc` go through
gVisor. Changing the default runtime machine-wide is what causes "my normal containers
behave differently" surprises.

## Step 0 — Capture your baseline (do this first)

Record what your machine looks like now, so revert is mechanical rather than guesswork:

```bash
docker info | grep -iE 'server version|runtimes|default runtime'
ls -la /etc/docker/
# If you already have a daemon.json, back it up before anything modifies it:
sudo cp /etc/docker/daemon.json /etc/docker/daemon.json.pre-gvisor.bak 2>/dev/null \
  || echo "no daemon.json — baseline is stock defaults"
```

Two baseline shapes matter for revert:

- **No `daemon.json` existed** → revert = delete the file.
- **A `daemon.json` existed** → revert = restore your `.pre-gvisor.bak` copy.

## Apply — install and register runsc

Debian/Ubuntu (the official gVisor apt repo; for other distros see the
[gVisor install docs](https://gvisor.dev/docs/user_guide/install/)):

```bash
# 1. Install runsc
sudo apt-get update
sudo apt-get install -y apt-transport-https ca-certificates curl gnupg

ARCH=$(dpkg --print-architecture)
KEY=/usr/share/keyrings/gvisor-archive-keyring.gpg
URL=https://storage.googleapis.com/gvisor/releases
LIST=/etc/apt/sources.list.d/gvisor.list

curl -fsSL https://gvisor.dev/archive.key | sudo gpg --dearmor --yes -o "$KEY"
echo "deb [arch=$ARCH signed-by=$KEY] $URL release main" | sudo tee "$LIST"
# ^ tee must print exactly ONE line; a multi-line file means your terminal
#   wrapped the paste and apt-get update will report "Malformed entry"

sudo apt-get update
sudo apt-get install -y runsc

# 2. Register runsc as an additional Docker runtime
#    (writes/merges the runtimes.runsc entry into /etc/docker/daemon.json)
sudo runsc install

# 3. Reload docker config — reload is sufficient for runtime registration
#    and does not restart your running containers
sudo systemctl reload docker
```

Starting from stock defaults, `/etc/docker/daemon.json` should now contain exactly:

```json
{
    "runtimes": {
        "runsc": {
            "path": "/usr/bin/runsc"
        }
    }
}
```

If `runsc install` added anything else — in particular `default-runtime` — remove it
and reload docker again.

### Verify

```bash
docker info | grep -A2 -i runtimes   # expect runsc listed, "Default Runtime: runc"
make verify-gvisor                    # runs dmesg inside the sandbox; gVisor prints its
                                      # own kernel banner ("Starting gVisor...")
```

## Revert: return to your baseline

```bash
# 1. Stop everything the PoC started (proxy container, images, proxy-net network)
make clean

# 2. Undo the runtime registration
#    If you had NO daemon.json before:
sudo rm /etc/docker/daemon.json
#    If you backed one up in Step 0:
# sudo mv /etc/docker/daemon.json.pre-gvisor.bak /etc/docker/daemon.json

# 3. Restart docker to fully drop the runtime.
#    NOTE: restart stops running containers unless you have live-restore enabled —
#    time this accordingly.
sudo systemctl restart docker

# 4. Optional: uninstall runsc entirely
sudo apt-get remove -y runsc
sudo rm /etc/apt/sources.list.d/gvisor.list /usr/share/keyrings/gvisor-archive-keyring.gpg
sudo apt-get update

# 5. Verify baseline restored
docker info | grep -iE 'runtimes|default runtime'
```

Step 4 is optional because an installed-but-unregistered runsc binary is inert: without
the `daemon.json` entry Docker cannot select it. If you plan to keep working with this
PoC, leave runsc installed and only add/remove the registration.

## Quick state check

```bash
docker info | grep -iE 'runtimes|default runtime' && ls /etc/docker/
```

| Output | State |
|--------|-------|
| `runc` only, no `daemon.json` | Stock baseline |
| `runsc` listed, `Default Runtime: runc` | PoC-ready — normal containers unaffected |
| `Default Runtime: runsc` | Misconfigured — remove `default-runtime` from `daemon.json` |
