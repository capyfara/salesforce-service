job "Salesforce-Service-Stage-App" {
  datacenters = ["glynac-dc"]
  type        = "service"
  namespace   = "extraction-service"

  update {
    max_parallel     = 1
    health_check     = "task_states"
    min_healthy_time = "30s"
  }

  group "salesforce-stage-service" {
    count = 1

    network {
      port "http" {
        static       = 5711
        to           = 5711
        host_network = "private"
      }
    }

    service {
      name = "salesforce-service-staging"
      tags = ["apps", "logs.promtail"]
      port = "http"
      check {
        name     = "api-health"
        type     = "http"
        path     = "/api/health"
        interval = "30s"
        timeout  = "10s"
      }
    }

    constraint {
      attribute = "${attr.unique.hostname}"
      value     = "Worker-08"
    }

    task "salesforce-service" {
      driver = "docker"

      config {
        image       = "harbor-registry.service.consul:8085/glynac/salesforce-service:IMAGE_TAG_PLACEHOLDER"
        ports       = ["http"]
        dns_servers = ["172.17.0.1", "172.18.0.1", "8.8.8.8", "8.8.4.4", "1.1.1.1"]
      }

      vault {
        role = "glynac"
      }

      template {
        destination = "secrets/env"
        env         = true
        data        = <<EOF

APP_VERSION="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.APP_VERSION }}{{ end }}"
APP_TITLE="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.APP_TITLE }}{{ end }}"
FLASK_ENV="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.FLASK_ENV }}{{ end }}"
FLASK_DEBUG="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.FLASK_DEBUG }}{{ end }}"
SECRET_KEY="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.SECRET_KEY }}{{ end }}"
PORT="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.PORT }}{{ end }}"
HOST="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.HOST }}{{ end }}"
ENVIRONMENT="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.ENVIRONMENT }}{{ end }}"
LOG_LEVEL="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.LOG_LEVEL }}{{ end }}"
LOG_FORMAT="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.LOG_FORMAT }}{{ end }}"
LOKI_ENABLED="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.LOKI_ENABLED }}{{ end }}"

DB_HOST="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.DB_HOST }}{{ end }}"
DB_PORT="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.DB_PORT }}{{ end }}"
DB_NAME="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.DB_NAME }}{{ end }}"
DB_USER="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.DB_USER }}{{ end }}"
DB_PASSWORD="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.DB_PASSWORD }}{{ end }}"
DB_SCHEMA="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.DB_SCHEMA }}{{ end }}"

SF_CONSUMER_KEY="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.SF_CONSUMER_KEY }}{{ end }}"
SF_PRIVATE_KEY_PEM="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.SF_PRIVATE_KEY_PEM }}{{ end }}"
SF_USERNAME="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.SF_USERNAME }}{{ end }}"
SF_LOGIN_URL="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.SF_LOGIN_URL }}{{ end }}"
SF_API_VERSION="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.SF_API_VERSION }}{{ end }}"
SF_BULK_PAGE_SIZE="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.SF_BULK_PAGE_SIZE }}{{ end }}"
SF_MAX_JOB_TIMEOUT_HOURS="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.SF_MAX_JOB_TIMEOUT_HOURS }}{{ end }}"

MAX_CONCURRENT_SCANS="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.MAX_CONCURRENT_SCANS }}{{ end }}"
SCAN_TIMEOUT_HOURS="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.SCAN_TIMEOUT_HOURS }}{{ end }}"
CLEANUP_DAYS="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.CLEANUP_DAYS }}{{ end }}"

HMAC_ENABLED="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.HMAC_ENABLED }}{{ end }}"
HMAC_SECRET_KEY_CORE="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.HMAC_SECRET_KEY_CORE }}{{ end }}"
HMAC_SECRET_KEY_ENGINEER="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.HMAC_SECRET_KEY_ENGINEER }}{{ end }}"
HMAC_SIGNATURE_MAX_AGE="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.HMAC_SIGNATURE_MAX_AGE }}{{ end }}"

MINIO_ENABLED="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.MINIO_ENABLED }}{{ end }}"
MINIO_ENDPOINT="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.MINIO_ENDPOINT }}{{ end }}"
MINIO_ACCESS_KEY="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.MINIO_ACCESS_KEY }}{{ end }}"
MINIO_SECRET_KEY="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.MINIO_SECRET_KEY }}{{ end }}"
MINIO_SECURE="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.MINIO_SECURE }}{{ end }}"
MINIO_BUCKET="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.MINIO_BUCKET }}{{ end }}"

PII_MASKING_ENABLED="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.PII_MASKING_ENABLED }}{{ end }}"
PII_SERVICE_URL="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.PII_SERVICE_URL }}{{ end }}"
PII_HMAC_KEY="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.PII_HMAC_KEY }}{{ end }}"
PII_SERVICE_ID="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.PII_SERVICE_ID }}{{ end }}"

ALLOWED_ORIGINS="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.ALLOWED_ORIGINS }}{{ end }}"
CORE_SERVICE_URL="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.CORE_SERVICE_URL }}{{ end }}"

EOF
      }

      resources {
        cpu    = 512
        memory = 512
      }
    }
  }
}
