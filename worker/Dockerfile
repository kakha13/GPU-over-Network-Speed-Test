# Use NVIDIA's official CUDA base. This image already has the right libs
# for NVENC/NVDEC when run with --runtime=nvidia.
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

# Install Python and FFmpeg with NVENC/NVDEC support.
# The 'ffmpeg' package in Ubuntu 22.04 universe is compiled with --enable-nvenc
# and --enable-cuda-llvm, so it can use the GPU when libnvidia-encode is available.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip \
        ffmpeg \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY worker.py .

# Quick startup self-check: confirm NVENC encoders are visible.
# Will print "h264_nvenc" if working.
RUN ffmpeg -hide_banner -encoders 2>/dev/null | grep nvenc || \
    echo "WARNING: nvenc encoders not visible at build time (this is OK; checked at runtime too)"

CMD ["python3", "-u", "worker.py"]
