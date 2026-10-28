FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY remote_auth.py remote_mcp.py product_loop.py loop_onboarding.py loop_sources.py loop_measurement.py loop_github.py loop_recipe.py app.py audit.py retention.py catalog.py product_analytics.py journeys.py accounts.py billing.py payments.py AGENTS.md ./
COPY static ./static
COPY skills ./skills
ENV ANALYTICS_DB=/data/analytics.db PORT=8080
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT} --no-access-log"]
