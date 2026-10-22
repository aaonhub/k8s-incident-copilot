# The copilot itself, for running inside the cluster (see deploy/copilot.yaml).
FROM python:3.12-slim
WORKDIR /srv
ENV HOME=/srv PYTHONPATH=/srv
RUN pip install --no-cache-dir "kubernetes>=30" "httpx>=0.27" "pydantic>=2.6" "numpy>=1.26" "fastembed>=0.4" \
    "fastapi>=0.110" "uvicorn>=0.29" && useradd -u 10001 -M -d /srv copilot
COPY corpus ./corpus
COPY copilot ./copilot
# Bake the embedding model and the corpus vectors into the image (no download at startup).
RUN python -c "from copilot.retrieval import KnowledgeBase; KnowledgeBase().emb" && chown -R copilot /srv
USER 10001
ENV COPILOT_LLM_MODE=live COPILOT_SPEND_CAP_USD=1.00
EXPOSE 8088
CMD ["python", "-m", "copilot.cli", "serve", "--port", "8088"]
