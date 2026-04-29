#!/usr/bin/env bash
# Probe what NVIDIA libs are visible inside a CUDA container.
echo '=== env (NVIDIA_*) ==='
env | grep -i nvidia
echo
echo '=== /usr/lib/wsl/lib/ ==='
ls /usr/lib/wsl/lib/ 2>&1
echo
echo '=== /usr/local/nvidia/lib* ==='
ls -la /usr/local/nvidia/lib /usr/local/nvidia/lib64 2>&1
echo
echo '=== libnvidia-encode anywhere? ==='
find /usr -name 'libnvidia-encode*' -o -name 'libnvcuvid*' 2>/dev/null
echo
echo '=== ldconfig encode/cuvid ==='
ldconfig -p 2>&1 | grep -E 'libnvidia-encode|libnvcuvid' || echo 'NONE in ldcache'
echo
echo '=== NVENC smoke test ==='
ffmpeg -hide_banner -loglevel error \
  -f lavfi -i 'testsrc2=size=320x240:rate=1:duration=1' \
  -c:v h264_nvenc -frames:v 1 -f null - 2>&1 | tail -10
echo "ffmpeg exit: $?"
