FROM python:3.12-slim

WORKDIR /app

# Yalnızca web paneli bağımlılıklarını kopyala ve kur
COPY requirements-web.txt .

RUN pip install --no-cache-dir -r requirements-web.txt

# Uygulama kaynak kodlarını ve şablonları kopyala
COPY src /app/src
COPY templates /app/templates
COPY static /app/static

# SQLite veritabanı bağlama klasörünü oluştur
RUN mkdir -p /app/data

EXPOSE 8000

# Exec form ile uvicorn web sunucusunu başlat
CMD ["uvicorn", "web_app:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8000"]
