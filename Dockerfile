FROM python:3.11-slim-bookworm

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ── System dependencies ──────────────────────────────────────────────
# libgl1  → libGL.so.1 (OpenCV/cv2 အတွက် လိုအပ်)
# libglib2.0-0 → OpenCV ရဲ့ transitive dependency
# libsm6, libxext6, libxrender1 → image processing libs
# ffmpeg → video/audio processing (လိုအပ်ရင်)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libgomp1 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# ── Python dependencies ──────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ── App code ─────────────────────────────────────────────────────────
COPY . .

# ── Non-root user (Railway best practice) ────────────────────────────
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8099

CMD ["python", "bot_time_new.py"]
