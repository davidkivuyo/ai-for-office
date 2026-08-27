FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir -e .
COPY app app
COPY public public
COPY scripts scripts
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
