# Google Cloud Deployment

Bu belge repository'deki mevcut Dockerfile ve FastAPI/Cloud SQL davranışına göre generic bir Cloud Run deployment akışı verir. Repository otomatik deployment scripti, Terraform veya aktif Kubernetes manifesti içermez; aşağıdaki komutlar ortamınıza göre uyarlanmalıdır.

## Bileşenler

- **Artifact Registry:** Docker image saklama
- **Cloud Run:** FastAPI web paneli ve HTTPS sync API
- **Cloud SQL for PostgreSQL:** Merkezi Vehicle ve AccessLog veritabanı
- **Secret Manager:** Parola, token, session secret ve credential içeren DB URL'si

Yerel güvenlik bilgisayarı Cloud SQL'a doğrudan bağlanmaz. Normal production veri yolu:

```text
Local PC -> HTTPS/443 -> Cloud Run -> Cloud SQL PostgreSQL
```

Cloud SQL Auth Proxy bu local sync yolu için gerekli değildir.

## Ön Koşullar

- Google Cloud CLI (`gcloud`) kurulmuş ve authenticate edilmiş olmalı
- Billing etkin olmalı
- `<PROJECT_ID>`, `<REGION>`, `<REPOSITORY>`, `<SERVICE_NAME>` ve Cloud SQL adları belirlenmeli
- Gerekli IAM rollerine sahip deployment hesabı kullanılmalı

```bash
gcloud config set project <PROJECT_ID>
gcloud services enable run.googleapis.com artifactregistry.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com cloudbuild.googleapis.com
```

## Artifact Registry

Docker repository oluşturun:

```bash
gcloud artifacts repositories create <REPOSITORY> \
  --repository-format=docker \
  --location=<REGION> \
  --description="License plate web service images"
```

Repository'deki `Dockerfile`, yalnız web/Cloud Run bağımlılıklarını `requirements-web.txt` üzerinden kurar ve Uvicorn'u port 8000'de başlatır.

Image build/push örneği:

```bash
gcloud builds submit \
  --tag <REGION>-docker.pkg.dev/<PROJECT_ID>/<REPOSITORY>/<SERVICE_NAME>:<IMAGE_TAG> .
```

## Cloud SQL PostgreSQL

Generic instance ve database oluşturma örneği:

```bash
gcloud sql instances create <INSTANCE_NAME> \
  --database-version=POSTGRES_16 \
  --region=<REGION>

gcloud sql databases create <DB_NAME> --instance=<INSTANCE_NAME>
```

Database kullanıcısını Cloud Console veya kuruluşunuzun güvenli provisioning akışıyla oluşturun. Parolayı terminal history'sine düz metin olarak yazmayın; credential içeren SQLAlchemy URL'sini Secret Manager'da saklayın.

Cloud Run Unix socket bağlantısı için `DATABASE_URL` genel biçimi:

```text
postgresql+psycopg2://<DB_USER>:<DB_PASSWORD>@/<DB_NAME>?host=/cloudsql/<PROJECT_ID>:<REGION>:<INSTANCE_NAME>
```

Bu değerde parola bulunduğu için tamamını secret kabul edin.

## Secret Manager

Gerekli production secret'ları:

- `WEB_ADMIN_PASSWORD`
- `WEB_SESSION_SECRET`
- `SYNC_API_TOKEN`
- `DATABASE_URL`

Secret resource'larını oluşturun:

```bash
gcloud secrets create WEB_ADMIN_PASSWORD --replication-policy=automatic
gcloud secrets create WEB_SESSION_SECRET --replication-policy=automatic
gcloud secrets create SYNC_API_TOKEN --replication-policy=automatic
gcloud secrets create DATABASE_URL --replication-policy=automatic
```

Secret değerlerini Cloud Console, güvenli stdin veya kuruluşunuzun secret pipeline'ı üzerinden yeni version olarak ekleyin. Gerçek değerleri shell history, README, deployment scripti veya Git içine koymayın.

Cloud Run runtime service account'a yalnız gereken secret'lar için `roles/secretmanager.secretAccessor` yetkisi ve Cloud SQL bağlantısı için uygun client rolü verin.

## Cloud Run Deploy

Generic deploy örneği:

```bash
gcloud run deploy <SERVICE_NAME> \
  --image <REGION>-docker.pkg.dev/<PROJECT_ID>/<REPOSITORY>/<SERVICE_NAME>:<IMAGE_TAG> \
  --region <REGION> \
  --platform managed \
  --port 8000 \
  --allow-unauthenticated \
  --add-cloudsql-instances <PROJECT_ID>:<REGION>:<INSTANCE_NAME> \
  --set-env-vars APP_ENV=production,WEB_ADMIN_USERNAME=<ADMIN_USERNAME> \
  --set-secrets WEB_ADMIN_PASSWORD=WEB_ADMIN_PASSWORD:latest,WEB_SESSION_SECRET=WEB_SESSION_SECRET:latest,SYNC_API_TOKEN=SYNC_API_TOKEN:latest,DATABASE_URL=DATABASE_URL:latest
```

`--allow-unauthenticated`, servisin public HTTPS URL almasını sağlar. İnsan paneli yine uygulama seviyesinde login/session/CSRF ile korunur; machine sync endpointleri Bearer token ister. Kuruluş politikası farklıysa Cloud Run IAM katmanını ayrıca sıkılaştırabilirsiniz, ancak yerel sync istemcisinin erişim modelini buna göre güncellemeniz gerekir.

Cloud Run'ın runtime service account'unu açıkça seçmek için kuruluşunuza ait dar yetkili bir hesapla `--service-account=<SERVICE_ACCOUNT>` kullanılması önerilir.

## Health ve İlk Doğrulama

Deployment sonrasında:

```bash
gcloud run services describe <SERVICE_NAME> --region <REGION>
```

Public URL üzerinde doğrulayın:

- `GET /health` → `200` ve `{"status":"ok"}`
- `GET /` → anonim kullanıcı için `/login` redirect
- `GET /login` → login formu
- Doğru admin bilgileri → dashboard
- Yanlış Bearer token → sync API `403`
- Geçerli Bearer token → boş/idempotent sync isteği kabul edilir

Secret değerlerini test çıktısına yazdırmayın.

## Local İstemci Yapılandırması

Cloud Run URL'sini aldıktan sonra yerel güvenlik bilgisayarında runtime environment üzerinden:

```bat
set CLOUD_SYNC_API_URL=https://<CLOUD_RUN_HOST>
set SYNC_API_TOKEN=<SECRET_VALUE>
set CAMERA_SOURCE=0
```

Ardından `start_system.bat` çalıştırılabilir. `SYNC_API_TOKEN` yerel güvenli secret yönetimiyle verilmelidir; `.env` veya batch dosyasına gerçek değer commit edilmemelidir.

## Güncelleme ve Rollback

Her release için değiştirilemez bir `<IMAGE_TAG>` kullanın. Yeni image deploy edildiğinde Cloud Run revision oluşturur. Sorun halinde Cloud Run revision traffic yönetimiyle önceki sağlıklı revision'a dönüş yapılabilir.

Schema yönetimi şu anda SQLAlchemy `create_all()` ve sınırlı, tahribatsız `sync_id` kolon yükseltmesi kullanır. Karmaşık production schema değişikliklerinde versioned migration aracı eklenmesi gerekir.

## Production Kontrolleri

- Cloud Run `APP_ENV=production`
- Session cookie üzerinde `Secure`, `HttpOnly`, `SameSite=Lax`
- Dört secret Secret Manager'dan bağlı
- Cloud SQL bağlantısı ve least-privilege service account doğrulanmış
- `/health` başarılı
- Login fail-closed davranıyor
- Local → Cloud push ve Cloud → Local approvals çalışıyor
- Loglarda password/token/session secret bulunmuyor
- Cloud Run revision ve Cloud SQL backup/retention politikaları ortam ihtiyacına göre ayarlı
