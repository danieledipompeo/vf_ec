FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive

# 1. Install Dependencies
# Added 'rsync', 'wget', 'curl' to handle downloads inside the image
RUN apt-get update && apt-get install -y \
    build-essential \
    pkg-config \
    yasm \
    git \
    python3 \
    linux-tools-common \
    libslang2 \
    libunwind8 \
    liblzma5 \
    libnuma1 \
    libelf1 \
    libdw1 \
    libzstd1 \
    libdebuginfod1 \
    libpfm4 \
    libpython3.12 \
    libperl5.38 \
    libtraceevent1 \
    libbabeltrace1 \
    rsync \
    wget \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 2. Setup paths
# Creating specific directories for inputs (repo/samples) and output (results)
WORKDIR /app
RUN mkdir -p /app/inputs \
    && mkdir -p /app/output

# 3. Git fix
RUN git config --global --add safe.directory '*'

# 4. Remove default perf wrapper to allow host perf mount
RUN rm -f /usr/bin/perf

# ------------------------------------------------------------------
# DOWNLOAD RESOURCES
# ------------------------------------------------------------------

# A. Download FFmpeg Repo to /app/inputs/FFmpeg
RUN git clone https://github.com/FFmpeg/FFmpeg.git /app/inputs/FFmpeg

# B. Download FATE Samples to /app/inputs/fate-samples
# (This step will take some time due to file size)
RUN rsync -aL rsync://fate-suite.ffmpeg.org/fate-suite/ /app/inputs/fate-samples/

# ------------------------------------------------------------------

COPY ffmpeg_pipeline.py /app/

CMD ["python3", "ffmpeg_pipeline.py"]