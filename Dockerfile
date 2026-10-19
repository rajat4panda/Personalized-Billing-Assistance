# Vidyut Sahaayam — citizen electricity-bill assistant (AIKAM #6)
# Pure-stdlib Python server (serves the app + proxies to the Crest agent).
# No third-party packages, so the image stays tiny and builds offline.
FROM python:3.12-slim

# Don't buffer stdout — container logs show the startup banner immediately.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8014

WORKDIR /app

# App files. (.env is NOT copied — secrets are injected at runtime via env_file/
# environment, so the API key never gets baked into the image.)
COPY server.py index.html ./
# COPY vidyut_assist.crest.json billing_assistant.crest.json ./
# COPY test-bills/ ./test-bills/

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8014

# Liveness: the /api/config endpoint always responds (even in Demo mode).
HEALTHCHECK --interval=30s --timeout=4s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8014')+'/api/config',timeout=3).status==200 else 1)"

CMD ["python3", "server.py"]
