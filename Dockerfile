# Dockerfile: a recipe for a "container", a sealed box with Python + this app inside.
# Optional alternative to the venv + systemd setup. Build:  docker build -t usgs-mcp .

# Start from a small official Python image.
FROM python:3.12-slim

# Don't run as root inside the container (limits damage if something goes wrong).
RUN useradd --create-home app
WORKDIR /app

# Install dependencies first; Docker caches this step so rebuilds are fast.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the code. Secrets are NOT copied; they come in at runtime via --env-file.
COPY server.py guardrails.py agent.py ./
USER app

# Inside the container we listen on all interfaces (0.0.0.0) so Docker can forward the port.
# Publish it only to the host's localhost:  docker run -p 127.0.0.1:8000:8000 ...
EXPOSE 8000
CMD ["python", "server.py", "--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
