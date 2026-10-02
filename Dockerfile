FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=America/New_York
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .

# 99:100 = Unraid nobody:users, so it can read the Conan appdata (770) and write snapshots
USER 99:100
# Settings come from env vars or a mounted /config/.env
ENV ENV_FILE=/config/.env
CMD ["python", "-u", "bot.py"]
