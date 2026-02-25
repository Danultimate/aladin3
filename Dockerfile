FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# SQLite and Streamlit need writable dir
RUN mkdir -p /app && chmod 777 /app

CMD ["python", "bot.py"]
