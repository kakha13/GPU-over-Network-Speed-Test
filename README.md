# GPU-over-Network Speed Test

Two Docker setups to measure end-to-end throughput of:
**Hetzner server (or any Linux box) ⇄ home Windows PC with GTX 1080 Ti**

It runs realistic ffmpeg NVENC jobs over your real network and prints per-stage
timings (download / encode / upload) so you can decide whether the architecture
is worth committing to before integrating with Shortzly.

## Layout

```
gpu-test/
├── client/                 # runs on Hetzner (or any Linux box with public IP)
│   ├── docker-compose.yml  # redis + minio + orchestrator
│   ├── .env.example
│   └── orchestrator/       # generates test videos, submits jobs, prints report
│       ├── Dockerfile
│       ├── requirements.txt
│       └── orchestrator.py
└── worker/                 # runs on Windows with GTX 1080 Ti
    ├── docker-compose.yml  # GPU-passthrough worker
    ├── Dockerfile
    ├── .env.example
    ├── requirements.txt
    └── worker.py
```

## Prerequisites

### On the Linux server (client side)
- Docker + docker compose plugin
- Open ports `6379` and `9000` to your home IP, OR put both behind Tailscale and use the Tailscale IP as `SERVER_HOST`.

### On Windows (worker side)
1. **WSL2 enabled** (Windows 10 21H2+ / Windows 11). Run as admin:
   ```powershell
   wsl --install
   wsl --set-default-version 2
   ```
2. **Up-to-date NVIDIA driver** (Game Ready or Studio, **r470+**, recommend r550+ for clean WSL CUDA). Check with `nvidia-smi` in PowerShell.
3. **Docker Desktop for Windows** with WSL2 backend enabled.
4. **Verify GPU is visible to Docker** (one-time check):
   ```powershell
   docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
   ```
   You should see the GTX 1080 Ti listed. If you get an error about NVIDIA Container Toolkit, see Troubleshooting below.

## Setup

### Windows-only single-host mode (no LAN, fastest GPU benchmark)

Use this when you just want to know what your GPU can do. Everything runs in
one Docker compose stack on the same Windows machine; redis/minio/worker
communicate over the internal Docker network so there is no LAN leg at all.

```powershell
git clone <this repo>
cd gpu-test
copy .env.example .env
# (edit .env if you want non-default passwords)

docker compose build
docker compose up -d redis minio minio-init worker
docker compose run --rm orchestrator python orchestrator.py full
```

The MinIO console is at `http://localhost:9001` (login = MINIO_USER / MINIO_PASSWORD).
Test videos are written to `./test-videos/` on the host and reused across runs.

### Split mode (LAN / cloud server + home GPU)

Use this when you want to measure the production architecture: a Linux box
running redis+minio (e.g. Hetzner) with the GPU worker on a different Windows
machine reaching it over the LAN or internet.

#### 1. On the server (Linux)

```bash
cd client
cp .env.example .env
# edit .env: pick strong passwords, set S3_PUBLIC_ENDPOINT to your server's
# public IP or hostname (e.g. http://1.2.3.4:9000)
nano .env

docker compose up -d redis minio minio-init
docker compose ps   # all should be healthy
```

Open the MinIO console at `http://YOUR_IP:9001` (login = MINIO_USER / MINIO_PASSWORD) to confirm the `gputest` bucket exists.

#### 2. On Windows (worker)

```powershell
cd worker
copy .env.example .env
# edit .env: SERVER_HOST = your Linux server IP; passwords must match
notepad .env

docker compose build
docker compose up
```

You should see:
```
[worker] Connecting to Redis at 1.2.3.4:6379 ...
[worker] Redis OK
[worker] S3/MinIO endpoint set to http://1.2.3.4:9000
[worker] NVENC encoders available:
           V....D h264_nvenc           NVIDIA NVENC H.264 encoder
           V....D hevc_nvenc           NVIDIA NVENC hevc encoder
[worker] NVENC smoke test passed
[worker] Listening on queue 'gpu_jobs' ...
```

If NVENC is not visible, fix that **before** running the test (see Troubleshooting).

#### 3. Run the test (back on the server)

```bash
cd client
docker compose run --rm orchestrator python orchestrator.py full
```

This will:
1. Generate three test videos (30s, 120s, 300s at 1080p) using ffmpeg `testsrc2`
2. Upload them to MinIO
3. Push three jobs onto the Redis queue
4. Wait for the worker to process them all
5. Print a timing table

You can also run the steps individually:

```bash
docker compose run --rm orchestrator python orchestrator.py generate
docker compose run --rm orchestrator python orchestrator.py submit
docker compose run --rm orchestrator python orchestrator.py wait
```

To start fresh:
```bash
docker compose run --rm orchestrator python orchestrator.py clean
```

## What the report tells you

```
| job      | source                | size     | download         | encode (gpu)         | upload   | total   | encoder    |
|----------|-----------------------|----------|------------------|----------------------|----------|---------|------------|
| a4f1...  | small_30s_1080p.mp4   | 18.7 MB  | 2.14s (70 Mbps)  | 1.83s (16.4x rt)     | 1.91s    | 5.88s   | h264_nvenc |
| 7c2e...  | medium_120s_1080p.mp4 | 119.3 MB | 13.7s (70 Mbps)  | 7.21s (16.7x rt)     | 12.4s    | 33.3s   | h264_nvenc |
| ...
```

**How to read it:**
- **download**: server -> worker. Bottlenecked by your home *download* speed.
- **encode (gpu)**: pure GPU work. The "Nx rt" number is the realtime multiplier - 16x rt means 1 minute of video encoded in ~3.7 seconds. The 1080 Ti should hit 10-20x rt for 1080p H.264.
- **upload**: worker -> server. Bottlenecked by your home *upload* speed (usually the slowest leg on residential connections).
- **total**: sum of the three. If `total / input_duration < 1`, you're encoding faster than realtime end-to-end, which is the goal.

If `download + upload > encode * 3`, you're network-bound and adding more GPU power will not help. The fix in that case is moving to object storage colocated near the worker (or just renting a Hetzner GEX44).

## Troubleshooting

### "could not select device driver nvidia" on Windows
Docker Desktop's GPU support uses WSL2, not the older Linux nvidia-docker2. Make sure:
- Docker Desktop -> Settings -> Resources -> WSL Integration is enabled.
- `wsl --status` shows version 2 as default.
- `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi` works.

### NVENC encoders not listed
The Ubuntu-packaged ffmpeg in the worker container needs `libnvidia-encode.so.1`, which is supplied by the NVIDIA Container Toolkit. The toolkit is present on Docker Desktop's WSL2 backend automatically. If `nvidia-smi` works but ffmpeg can't see NVENC, drop into the container:
```powershell
docker compose run --rm --entrypoint bash worker
ffmpeg -encoders 2>/dev/null | grep nvenc
ldconfig -p | grep nvidia-encode
```
You should see `libnvidia-encode.so.1`. If not, your NVIDIA driver is too old. Upgrade to r550+.

### Worker can't reach Redis or MinIO
- Test from inside the worker container: `docker compose exec worker bash -c "curl -v http://$SERVER_HOST:9000"`.
- Check the server firewall: ports 6379 and 9000 must be open to your home IP.
- If you're using Tailscale, set `SERVER_HOST` to the Tailscale IP and only expose 6379/9000 on the Tailscale interface (recommended for security).

### Encode is much slower than expected
The 1080 Ti's NVENC engine handles 1080p H.264 at roughly 300-400 fps, which translates to 10-15x realtime for 30 fps source. If you're seeing 1-2x realtime:
- Confirm `-hwaccel cuda -hwaccel_output_format cuda` is in the command (the worker uses these by default).
- Check `nvidia-smi -l 1` while encoding - GPU utilization should hit 60-90%.
- WSL2 GPU passthrough can have ~5-10% overhead vs native; if you need every last frame, run the worker natively on Windows instead of in Docker.

## Going to production

This test setup is deliberately minimal. Once it shows acceptable numbers, swap in:
- **Tailscale** instead of public ports (already noted)
- **Cloudflare R2 or Hetzner Object Storage** instead of self-hosted MinIO
- **Celery** instead of raw Redis lists (retries, dead-letter queues, monitoring via Flower)
- **NSSM-wrapped native Windows worker** instead of Docker (saves the WSL2 overhead)
- Add **faster-whisper** for captions on the same GPU

See the architecture artifact for the full production design.
