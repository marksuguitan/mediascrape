# Lambda container image: Python handler + yt-dlp + Deno (YouTube JS
# challenges) + a static ffmpeg (merging video and audio streams).

FROM public.ecr.aws/docker/library/debian:bookworm-slim AS tools
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl unzip xz-utils \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /out
# Static ffmpeg build (GPL, linux64). Only the ffmpeg/ffprobe binaries are kept.
RUN curl -fsSL -o ffmpeg.tar.xz \
      https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz \
 && tar -xJf ffmpeg.tar.xz --strip-components=2 --wildcards '*/bin/ffmpeg' '*/bin/ffprobe' \
 && rm ffmpeg.tar.xz
RUN curl -fsSL -o deno.zip \
      https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip \
 && unzip deno.zip && rm deno.zip
RUN chmod 755 ffmpeg ffprobe deno && ./ffmpeg -version | head -1 && ./deno --version

FROM public.ecr.aws/lambda/python:3.12
COPY --from=tools /out/ffmpeg /out/ffprobe /out/deno /opt/bin/
ENV PATH="/opt/bin:${PATH}" \
    FFMPEG_LOCATION=/opt/bin \
    HOME=/tmp \
    DENO_DIR=/tmp/deno \
    XDG_CACHE_HOME=/tmp/.cache
COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt \
 && python -c "import yt_dlp, yt_dlp_ejs; print('yt-dlp', yt_dlp.version.__version__)"
COPY app.py ${LAMBDA_TASK_ROOT}/
CMD ["app.handler"]
