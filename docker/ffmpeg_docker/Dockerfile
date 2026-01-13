FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive

# 1. Install Dependencies
# Added 'libtraceevent1' and 'libbabeltrace1'
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
    && rm -rf /var/lib/apt/lists/*

# 2. Setup paths
WORKDIR /app
RUN mkdir -p /app/ds_projects/FFmpeg \
    && mkdir -p /app/ds_projects/fate-samples \
    && mkdir -p /app/vfec_results

# 3. Git fix
RUN git config --global --add safe.directory '*'

# 4. Remove default perf wrapper
RUN rm -f /usr/bin/perf

COPY ffmpeg_pipeline.py /app/
COPY input_ffmpeg.csv /app/

CMD ["python3", "ffmpeg_pipeline.py"]