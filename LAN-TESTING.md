# LAN Testing Guide

You picked LAN-only testing for the first run. Smart - this isolates GPU performance from network variance and gives you a clean baseline number to compare WAN tests against later.

There are three ways to set this up depending on what hardware you have:

## Option A: Two machines on the same LAN (most realistic)

You have a separate Linux box (could be a NAS, Raspberry Pi, old laptop, anything) and the Windows + 1080 Ti machine, both plugged into the same router.

This best simulates the real Hetzner-Tbilisi setup, just without WAN latency.

### On the Linux box

1. Find its LAN IP:
   ```bash
   ip -4 addr show | grep inet
   # look for 192.168.x.x or 10.x.x.x
   ```
2. Set up the client:
   ```bash
   cd client
   cp .env.example .env
   nano .env
   # set S3_PUBLIC_ENDPOINT=http://YOUR_LAN_IP:9000
   docker compose up -d redis minio minio-init
   ```
3. Confirm services are reachable from the Windows machine. From PowerShell:
   ```powershell
   Test-NetConnection -ComputerName 192.168.1.50 -Port 9000
   Test-NetConnection -ComputerName 192.168.1.50 -Port 6379
   ```
   Both should say `TcpTestSucceeded : True`.

### On Windows

```powershell
cd worker
copy .env.example .env
notepad .env
# set SERVER_HOST=192.168.1.50 (the Linux box's LAN IP)
docker compose up
```

### Run the test (back on Linux)

```bash
cd client
docker compose run --rm orchestrator python orchestrator.py full
```

**What to expect on a typical home gigabit LAN:**
- download/upload around 800-950 Mbps (limited by gigabit ethernet)
- 119 MB upload should take about 1.0-1.3 seconds
- encode time is the only "real" measurement - everything else is essentially free at LAN speeds

## Option B: Everything on the Windows machine (pure GPU baseline)

If you don't have a separate Linux box, just run **both** Docker Compose stacks on the same Windows PC. Network goes through Docker's loopback bridge, which is essentially infinite bandwidth.

This gives you the **pure GPU encoding speed** with zero network noise. The download/upload columns in the report will be near-zero - that's the point. Whatever encode time you see here is the absolute best case.

### Setup

1. Open two PowerShell windows.
2. In the first:
   ```powershell
   cd client
   copy .env.example .env
   notepad .env
   # set S3_PUBLIC_ENDPOINT=http://host.docker.internal:9000
   docker compose up -d redis minio minio-init
   ```
   `host.docker.internal` is a magic Docker Desktop hostname that resolves to the host from inside any container.
3. In the second:
   ```powershell
   cd worker
   copy .env.example .env
   notepad .env
   # set SERVER_HOST=host.docker.internal
   docker compose up
   ```
4. Run the test from the first window:
   ```powershell
   cd client
   docker compose run --rm orchestrator python orchestrator.py full
   ```

## Option C: Linux server in a VM on Windows (middle ground)

If you want to simulate two machines but only have one box, run the Linux side in a Hyper-V or VirtualBox VM with bridged networking. Same setup as Option A, just the "Linux box" is virtual. This adds realistic network framing (broadcasts, ARP, MTU) without actual physical hardware.

Not really worth it unless you specifically want to test what real Linux networking does to the timings. Option A or B covers 99% of cases.

## Reading the LAN baseline

Once you run it, you'll see something like:

```
| job     | source             | size    | download        | encode (gpu)     | upload         | total  |
|---------|--------------------|---------|-----------------|------------------|----------------|--------|
| a4f1... | small_30s_1080p    | 18 MB   | 0.2s (720 Mbps) | 1.8s (16x rt)    | 0.2s (720 Mbps)| 2.2s   |
| 7c2e... | medium_120s_1080p  | 119 MB  | 1.3s (730 Mbps) | 7.2s (16x rt)    | 1.3s (730 Mbps)| 9.8s   |
| ...     | large_300s_1080p   | 298 MB  | 3.3s (720 Mbps) | 17.8s (16x rt)   | 3.3s (720 Mbps)| 24.4s  |
```

**The encode column is what matters for your decision.** That number won't change when you move to WAN. Your home upload speed will determine how much the upload column grows when you go cross-country to Hetzner.

Save this report. Once you do the WAN test later, you can compare directly:

- **LAN encode time = WAN encode time** (GPU doesn't care about network)
- **WAN download time = LAN download time × (LAN_speed / WAN_download_speed)**
- **WAN upload time = LAN upload time × (LAN_speed / WAN_upload_speed)**  ← this is usually the killer

For a Tbilisi residential connection at ~150 Mbps upload to Hetzner Falkenstein, expect upload times to be roughly 5-7× longer than the LAN result.

## Troubleshooting LAN connectivity

### Windows worker can't reach Linux box

- **Windows Firewall on the Linux box?** Most distros ship without one, but if you're using Ubuntu Server with UFW: `sudo ufw allow from 192.168.0.0/16 to any port 6379` and same for 9000.
- **Windows Defender Firewall blocking outbound?** Unlikely but possible if you have a corporate machine. Try `Test-NetConnection` first.
- **Docker bridge isolation?** If you're using Option B and the worker can't reach `host.docker.internal`, restart Docker Desktop and verify in the worker container with `getent hosts host.docker.internal`.
- **Wifi vs ethernet on Windows?** If both are connected, Windows might pick the wrong route. Disable wifi while testing.

### MinIO returns presigned URLs that point at the wrong host

This is the #1 source of confusion. MinIO uses whatever you set as `S3_PUBLIC_ENDPOINT` when generating presigned URLs. The orchestrator uses direct `boto3.upload_file` so it doesn't care, but **if you ever switch to presigned URLs**, that env var must point at an address the *worker* can reach.

For LAN: set it to the Linux box's LAN IP (Option A) or `host.docker.internal` (Option B), never `localhost`.

### Encode is suspiciously fast (>50× realtime)

Probably an ffmpeg config issue masking failure. Check the worker log for "GPU utilization" by running `nvidia-smi -l 1` on Windows during the test. If GPU stays at 0-5% util, NVENC isn't actually being used and the test is just doing a stream copy or CPU encode somehow.
