FROM python:3.12-slim
RUN pip install --no-cache-dir fastapi==0.115.* uvicorn==0.32.*
WORKDIR /srv
COPY main.py .
USER 10001
EXPOSE 8080
CMD ["python", "-u", "main.py"]
