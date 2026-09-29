FROM python:3.13-slim
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy in the source code
COPY /app .

# Run app
CMD fastapi run  main.py --host ${HOST:-0.0.0.0} --port ${PORT:-8000}