FROM python:3.12-slim

# ffmpeg для сплита; unzip для TwitchDownloaderCLI; libicu + libssl нужны .NET runtime
# proxychains4 — оборачивает ffmpeg/TWD при SOCKS5-прокси (ffmpeg нативно SOCKS не умеет)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg curl unzip ca-certificates \
        libicu-dev libssl-dev \
        proxychains4 \
    && rm -rf /var/lib/apt/lists/*

# TwitchDownloaderCLI — запинено на конкретную версию для воспроизводимости билда.
# Обновлять вручную после проверки на staging.
ARG TWD_VERSION=1.56.4
RUN curl -L -o /tmp/twd.zip \
      "https://github.com/lay295/TwitchDownloader/releases/download/${TWD_VERSION}/TwitchDownloaderCLI-${TWD_VERSION}-Linux-x64.zip" \
 && unzip /tmp/twd.zip -d /usr/local/bin/ \
 && chmod +x /usr/local/bin/TwitchDownloaderCLI \
 && rm /tmp/twd.zip

# подстраховка на случай отсутствующей ICU data
ENV DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1

WORKDIR /app

# сначала зависимости (кэшируется Docker слоем)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# потом код
COPY bot/ bot/

CMD ["python", "-m", "bot.main"]
