"""
Test orchestrator. Run from inside the Docker container:

  docker compose run --rm orchestrator python orchestrator.py generate
  docker compose run --rm orchestrator python orchestrator.py submit
  docker compose run --rm orchestrator python orchestrator.py wait
  docker compose run --rm orchestrator python orchestrator.py bench

Or simply:
  docker compose run --rm orchestrator python orchestrator.py full
to run the whole pipeline end-to-end.
"""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import boto3
import redis
from botocore.client import Config
from tabulate import tabulate

REDIS_URL = os.environ["REDIS_URL"]
S3_ENDPOINT = os.environ["S3_ENDPOINT"]
S3_PUBLIC_ENDPOINT = os.environ.get("S3_PUBLIC_ENDPOINT", S3_ENDPOINT)
S3_KEY = os.environ["S3_KEY"]
S3_SECRET = os.environ["S3_SECRET"]
S3_BUCKET = os.environ["S3_BUCKET"]

JOB_QUEUE = "gpu_jobs"
RESULT_PREFIX = "result:"

r = redis.from_url(REDIS_URL)
s3 = boto3.client(
    "s3",
    endpoint_url=S3_ENDPOINT,
    aws_access_key_id=S3_KEY,
    aws_secret_access_key=S3_SECRET,
    config=Config(signature_version="s3v4"),
    region_name="us-east-1",
)

VIDEOS_DIR = Path("/test-videos")
VIDEOS_DIR.mkdir(exist_ok=True)


def generate_test_videos():
    """Generate a few test videos of varying size with FFmpeg."""
    presets = [
        # (name, duration_s, resolution, bitrate)
        ("small_30s_1080p", 30, "1920x1080", "5M"),
        ("medium_120s_1080p", 120, "1920x1080", "8M"),
        ("large_300s_1080p", 300, "1920x1080", "8M"),
    ]
    for name, dur, res, br in presets:
        out = VIDEOS_DIR / f"{name}.mp4"
        if out.exists():
            print(f"  [skip] {out.name} already exists ({out.stat().st_size/1e6:.1f} MB)")
            continue
        print(f"  [gen]  {out.name} ...")
        # Use lavfi to generate synthetic video + audio
        # testsrc2 = moving pattern that compresses realistically (not flat colors)
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc2=size={res}:rate=30:duration={dur}",
            "-f", "lavfi", "-i", f"sine=frequency=1000:duration={dur}",
            "-c:v", "libx264", "-preset", "fast", "-b:v", br,
            "-c:a", "aac", "-b:a", "128k",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(out),
        ]
        subprocess.run(cmd, check=True)
        print(f"         {out.stat().st_size/1e6:.1f} MB")


def upload_test_videos():
    """Push test videos to MinIO under incoming/"""
    results = {}
    for f in sorted(VIDEOS_DIR.glob("*.mp4")):
        key = f"incoming/{f.name}"
        size = f.stat().st_size
        print(f"  [up]   {f.name} ({size/1e6:.1f} MB) -> s3://{S3_BUCKET}/{key}")
        t0 = time.monotonic()
        s3.upload_file(str(f), S3_BUCKET, key)
        dt = time.monotonic() - t0
        mbps = (size * 8) / dt / 1e6
        print(f"         {dt:.2f}s ({mbps:.1f} Mbps)")
        results[f.name] = {"key": key, "size": size, "upload_s": dt}
    return results


def submit_jobs(uploaded):
    """Push jobs onto Redis."""
    job_ids = []
    for name, meta in uploaded.items():
        job_id = str(uuid.uuid4())
        job = {
            "job_id": job_id,
            "src_key": meta["key"],
            "src_name": name,
            "src_size": meta["size"],
            "submitted_at": time.time(),
            # FFmpeg params
            "out_w": 1080,
            "out_h": 1920,
            "preset": "p4",
            "cq": 23,
        }
        r.lpush(JOB_QUEUE, json.dumps(job))
        job_ids.append(job_id)
        print(f"  [job]  queued {job_id[:8]}  src={name}")
    return job_ids


def wait_for_results(job_ids, timeout=600):
    """Poll Redis for result keys."""
    print(f"\n  Waiting for {len(job_ids)} job(s) to complete (timeout {timeout}s)...")
    pending = set(job_ids)
    start = time.monotonic()
    results = {}
    while pending and (time.monotonic() - start) < timeout:
        for jid in list(pending):
            data = r.get(f"{RESULT_PREFIX}{jid}")
            if data:
                results[jid] = json.loads(data)
                pending.discard(jid)
                status = results[jid].get("status", "?")
                src = results[jid].get("src_name", "?")
                ttot = results[jid].get("total_s", 0)
                print(f"  [done] {jid[:8]}  {src}  status={status}  {ttot:.2f}s")
        if pending:
            time.sleep(1)
    if pending:
        print(f"  [warn] {len(pending)} job(s) timed out")
    return results


def report(results):
    """Pretty-print timing breakdown."""
    rows = []
    for jid, res in results.items():
        if res.get("status") != "ok":
            rows.append([
                jid[:8], res.get("src_name", "?"), res.get("status"),
                "-", "-", "-", "-", "-", res.get("error", "")[:40],
            ])
            continue
        size_mb = res["src_size"] / 1e6
        dl = res["download_s"]
        enc = res["encode_s"]
        ul = res["upload_s"]
        tot = res["total_s"]
        dl_mbps = (res["src_size"] * 8) / dl / 1e6 if dl > 0 else 0
        # Encode speed: input duration / encode time
        enc_speed = res.get("input_duration_s", 0) / enc if enc > 0 else 0
        rows.append([
            jid[:8],
            res["src_name"],
            f"{size_mb:.1f} MB",
            f"{dl:.2f}s ({dl_mbps:.0f} Mbps)",
            f"{enc:.2f}s ({enc_speed:.1f}x rt)",
            f"{ul:.2f}s",
            f"{tot:.2f}s",
            res.get("encoder", "?"),
        ])
    print()
    print(tabulate(
        rows,
        headers=["job", "source", "size", "download", "encode (gpu)", "upload", "total", "encoder"],
        tablefmt="github",
    ))
    print()


def cmd_generate():
    print("[1/4] Generating test videos with FFmpeg...")
    generate_test_videos()


def cmd_submit():
    print("[2/4] Uploading test videos to MinIO...")
    uploaded = upload_test_videos()
    print("\n[3/4] Submitting jobs to Redis queue...")
    job_ids = submit_jobs(uploaded)
    Path("/tmp/last_jobs.json").write_text(json.dumps(job_ids))
    print(f"\n  {len(job_ids)} job(s) queued. Now start the Windows worker.")


def cmd_wait():
    job_ids = json.loads(Path("/tmp/last_jobs.json").read_text())
    print("[4/4] Waiting for worker to finish...")
    results = wait_for_results(job_ids)
    report(results)


def cmd_full():
    cmd_generate()
    print("\n[2/4] Uploading test videos to MinIO...")
    uploaded = upload_test_videos()
    print("\n[3/4] Submitting jobs to Redis queue...")
    job_ids = submit_jobs(uploaded)
    print(f"\n[!] {len(job_ids)} job(s) queued. Make sure the Windows worker is running.")
    print("[4/4] Waiting for worker to finish...")
    results = wait_for_results(job_ids)
    report(results)


def cmd_clean():
    """Remove all jobs and results, clear MinIO bucket."""
    print("Clearing Redis queue and results...")
    r.delete(JOB_QUEUE)
    for k in r.scan_iter(f"{RESULT_PREFIX}*"):
        r.delete(k)
    print("Clearing MinIO bucket...")
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET):
        for obj in page.get("Contents", []):
            s3.delete_object(Bucket=S3_BUCKET, Key=obj["Key"])
    print("Done.")


COMMANDS = {
    "generate": cmd_generate,
    "submit": cmd_submit,
    "wait": cmd_wait,
    "full": cmd_full,
    "clean": cmd_clean,
}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "full"
    if cmd not in COMMANDS:
        print(f"Unknown command: {cmd}")
        print(f"Available: {', '.join(COMMANDS)}")
        sys.exit(1)
    COMMANDS[cmd]()
