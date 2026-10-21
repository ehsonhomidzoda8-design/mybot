FROM python:3.12-slim

# ffmpeg нужен для MP3 и склейки видео+аудио
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Deno нужен yt-dlp для расшифровки ссылок YouTube
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
# при выходе новой версии yt-dlp этот слой пересобирается и тянет свежую версию
ADD https://pypi.org/pypi/yt-dlp/json /tmp/yt-dlp.json
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

CMD ["python", "bot.py"]
