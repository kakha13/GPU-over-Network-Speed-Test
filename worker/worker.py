"""
Worker process. Runs inside Docker on the Windows machine with GPU passthrough.
Pulls jobs from Redis, downloads source from MinIO, encodes with NVENC, uploads result.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

import boto3
import redis
from botocore.client import Config

REDIS_PASSWORD = os.environ["REDIS_PASSWORD"]
MINIO_USER = os.environ.get("MINIO_USER", "admin")
MINIO_PASSWORD = os.environ["MINIO_PASSWORD"]

# Two ways to point the worker at services:
#   single-host (compose internal DNS):  REDIS_URL, S3_ENDPOINT explicit
#   split (LAN / cloud + home GPU):       SERVER_HOST builds both URLs
SERVER_HOST = os.environ.get("SERVER_HOST", "")
REDIS_URL = os.environ.get("REDIS_URL") or f"redis://:{REDIS_PASSWORD}@{SERVER_HOST}:6379/0"
S3_ENDPOINT = os.environ.get("S3_ENDPOINT") or f"http://{SERVER_HOST}:9000"
S3_BUCKET = os.environ.get("S3_BUCKET", "gputest")

JOB_QUEUE = "gpu_jobs"
RESULT_PREFIX = "result:"

print(f"[worker] Connecting to Redis at {REDIS_URL.split('@')[-1]} ...")
# socket_timeout must exceed BRPOP timeout (30s) or the read aborts before
# a blocking pop completes, raising TimeoutError and risking job loss when
# the server has already popped an item but the client closed the socket.
# socket_connect_timeout still bounds the initial connect for fast-fail.
r = redis.from_url(
    REDIS_URL,
    socket_connect_timeout=10,
    socket_timeout=35,
    socket_keepalive=True,
    health_check_interval=30,
)
r.ping()
print(f"[worker] Redis OK")

s3 = boto3.client(
    "s3",
    endpoint_url=S3_ENDPOINT,
    aws_access_key_id=MINIO_USER,
    aws_secret_access_key=MINIO_PASSWORD,
    config=Config(signature_version="s3v4"),
    region_name="us-east-1",
)
print(f"[worker] S3/MinIO endpoint set to {S3_ENDPOINT}  bucket={S3_BUCKET}")


def check_gpu():
    """Verify NVENC is actually available."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, check=True,
        ).stdout
        encoders = [
            line.strip() for line in out.splitlines()
            if "nvenc" in line.lower()
        ]
        if not encoders:
            print("[worker] !!! NO NVENC ENCODERS FOUND !!!")
            print("[worker] FFmpeg cannot see the GPU. Check NVIDIA Container Toolkit setup.")
            return False
        print("[worker] NVENC encoders available:")
        for e in encoders:
            print(f"           {e}")
        # Quick smoke test: try a 1-frame NVENC encode
        smoke = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=1:duration=1",
             "-c:v", "h264_nvenc", "-frames:v", "1",
             "-f", "null", "-"],
            capture_output=True, text=True,
        )
        if smoke.returncode != 0:
            print("[worker] !!! NVENC smoke test FAILED !!!")
            print(smoke.stderr)
            return False
        print("[worker] NVENC smoke test passed")
        return True
    except Exception as e:
        print(f"[worker] GPU check failed: {e}")
        return False


def get_input_duration(path: Path) -> float:
    """Probe input duration in seconds."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def process_job(job: dict) -> dict:
    """Run one job. Returns result dict."""
    jid = job["job_id"]
    src_key = job["src_key"]
    out_w = job.get("out_w", 1080)
    out_h = job.get("out_h", 1920)
    preset = job.get("preset", "p4")
    cq = job.get("cq", 23)

    workdir = Path(tempfile.mkdtemp(prefix=f"job-{jid[:8]}-"))
    src = workdir / "source.mp4"
    dst = workdir / "out.mp4"

    result = {
        "job_id": jid,
        "src_name": job.get("src_name"),
        "src_size": job.get("src_size"),
        "encoder": "h264_nvenc",
        "started_at": time.time(),
    }

    try:
        # 1. Download
        t0 = time.monotonic()
        s3.download_file(S3_BUCKET, src_key, str(src))
        result["download_s"] = time.monotonic() - t0
        result["input_duration_s"] = get_input_duration(src)

        # 2. GPU encode (1080 Ti / Pascal NVENC max-throughput config)
        t0 = time.monotonic()
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
            "-extra_hw_frames", "8",
            "-c:v", "h264_cuvid",
            "-i", str(src),
            "-vf",
            f"scale_cuda={out_w}:{out_h}:force_original_aspect_ratio=increase,"
            f"crop={out_w}:{out_h}",
            "-c:v", "h264_nvenc",
            "-preset", "p1",
            "-tune", "ull",
            "-rc", "cbr",
            "-b:v", "8M",
            "-maxrate", "8M",
            "-bufsize", "16M",
            "-bf", "0",
            "-rc-lookahead", "0",
            "-spatial_aq", "0",
            "-temporal_aq", "0",
            "-g", "120",
            "-async_depth", "4",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(dst),
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        result["encode_s"] = time.monotonic() - t0
        result["output_size"] = dst.stat().st_size

        # 3. Upload
        t0 = time.monotonic()
        out_key = f"outgoing/{jid}.mp4"
        s3.upload_file(str(dst), S3_BUCKET, out_key)
        result["upload_s"] = time.monotonic() - t0
        result["out_key"] = out_key

        result["status"] = "ok"
    except subprocess.CalledProcessError as e:
        result["status"] = "ffmpeg_error"
        result["error"] = e.stderr[-500:] if e.stderr else str(e)
    except Exception as e:
        result["status"] = "error"
        result["error"] = f"{type(e).__name__}: {e}"
        traceback.print_exc()
    finally:
        # Cleanup
        for f in (src, dst):
            try:
                f.unlink()
            except FileNotFoundError:
                pass
        try:
            workdir.rmdir()
        except OSError:
            pass

    result["finished_at"] = time.time()
    result["total_s"] = (
        result.get("download_s", 0)
        + result.get("encode_s", 0)
        + result.get("upload_s", 0)
    )
    return result


def main():
    if not check_gpu():
        print("[worker] GPU check FAILED. Exiting.")
        sys.exit(2)

    print(f"[worker] Listening on queue '{JOB_QUEUE}' ...")
    while True:
        try:
            # BRPOP blocks until a job appears, with 30s timeout for keep-alive
            popped = r.brpop(JOB_QUEUE, timeout=30)
            if not popped:
                print(f"[worker] heartbeat - waiting for jobs ...")
                continue
            _, raw = popped
            job = json.loads(raw)
            jid = job["job_id"]
            print(f"[worker] -> picked job {jid[:8]}  src={job.get('src_name')}")
            result = process_job(job)
            r.set(f"{RESULT_PREFIX}{jid}", json.dumps(result), ex=3600)
            status = result.get("status")
            if status == "ok":
                print(
                    f"[worker] <- done {jid[:8]}  "
                    f"dl={result['download_s']:.2f}s  "
                    f"enc={result['encode_s']:.2f}s  "
                    f"ul={result['upload_s']:.2f}s  "
                    f"({result['input_duration_s']:.0f}s input -> "
                    f"{result['input_duration_s']/max(result['encode_s'],0.01):.1f}x rt)"
                )
            else:
                print(f"[worker] <- FAILED {jid[:8]}  {status}: {result.get('error','')[:100]}")
        except redis.ConnectionError as e:
            print(f"[worker] Redis connection lost: {e}. Retrying in 5s ...")
            time.sleep(5)
        except KeyboardInterrupt:
            print("[worker] Interrupted, shutting down.")
            break
        except Exception as e:
            print(f"[worker] Unexpected error: {e}")
            traceback.print_exc()
            time.sleep(2)


if __name__ == "__main__":
    main()
