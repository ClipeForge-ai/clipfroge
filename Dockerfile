FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -U yt-dlp
WORKDIR /app
COPY app.py index.html ./
ENV PORT=10000
EXPOSE 10000
CMD ["python", "-u", "app.py"]
