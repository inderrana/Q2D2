# R2D2 WSL2 Launcher Guide

This guide ensures your services launch in the correct order, prevents port conflicts, and maintains the tunnel connection.

## Prerequisites

Ensure your WSL distribution is configured for stability by adding this to your `%USERPROFILE%\.wslconfig` on Windows:

```ini
[wsl2]
networkingMode=mirrored
dnsTunneling=true
```

## Startup Procedure

Run these commands in your WSL terminal to initialize the environment.

### 1. Initialize System Services

First, ensure the Docker daemon and the tunnel service are running.

> If you are using Rootless Docker, use the user-level `systemctl` commands.

```bash
# Start Docker daemon
systemctl --user start docker

# Restart the Cloudflare Tunnel to establish a fresh connection
# (Ensure your cloudflared service is named correctly)
sudo systemctl restart cloudflared
```

### 2. Launch the Stack

Navigate to your project directory and use the custom `run.sh` script to bring up the containers.

```bash
cd ~/R2R2
./run.sh up -d
```

### 3. Verify Health

Verify that the services have successfully bound to their respective ports and the containers are healthy:

```bash
# Check container status
docker ps

# Confirm the backend is responding
curl -I http://127.0.0.1:8088/healthz
```

---

## Troubleshooting: "Address Already in Use"

If you receive the following error:

```text
failed to bind host port 0.0.0.0:11434
```

Follow these steps to clear the "ghost" process.

### Stop the Docker daemon

```bash
systemctl --user stop docker
```

### Confirm the port is clear

```bash
sudo ss -lptn 'sport = :11434'
```

This command should return no output.

### Restart the daemon and launch

```bash
systemctl --user start docker && ./run.sh up -d
```

---

## Quick Reference Commands

| Goal | Command |
|--------|---------|
| Launch Stack | `./run.sh up -d` |
| Stop Everything | `./run.sh down` |
| View Logs | `./run.sh logs -f` |
| Check Tunnel | `sudo systemctl status cloudflared` |
| Emergency Kill | `systemctl --user stop docker` |

---

## Understanding the WSL/Docker Bridge

The complexity here arises because WSL2 acts as a bridge between the Linux kernel and the Windows networking stack. Rootless Docker requires careful management of these bridges to avoid port contention.

---