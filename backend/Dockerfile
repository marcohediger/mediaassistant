FROM python:3.12-slim

# Debian security updates first; ImageMagick is gone since 3.0.1 (GIF and the
# HEIC fallback go through Pillow, see pipeline/step_ia04_convert.py).
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends \
    exiftool \
    libheif-examples \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
