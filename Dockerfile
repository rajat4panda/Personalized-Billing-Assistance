# Vidyut Sahaayam — citizen web app + Crest proxy (AIKAM #6).
# Pure-stdlib Python; no third-party packages, so the image is tiny and builds offline.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8014

WORKDIR /app

# App files only. .env is NOT copied — the API key is injected at runtime
# (env_file / environment), so it never gets baked into the image.
COPY server.py index.html ./

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8014

# Liveness: /healthz always responds (even in Demo mode).
HEALTHCHECK --interval=30s --timeout=4s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8014')+'/healthz',timeout=3).status==200 else 1)"

CMD ["python3", "server.py"]
