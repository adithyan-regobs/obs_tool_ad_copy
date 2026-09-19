FROM python:{{PYTHON_VERSION}}-slim

WORKDIR /app

{{CUSTOM_BUILD_ARGS}}
# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

ENV APP_PORT={{PORT}}
EXPOSE {{PORT}}

CMD ["python", "main.py"]
