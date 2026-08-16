#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    printf "Run this script as root: sudo %s\n" "$0" >&2
    exit 1
fi

APP_DIR=${APP_DIR:-/opt/depth-motion}
APP_USER=${APP_USER:-ubuntu}
SERVER_NAME=${SERVER_NAME:-_}
MODEL_ID=${MODEL_ID:-depth-anything/Depth-Anything-V2-Small-hf}
MAX_UPLOAD_MB=${MAX_UPLOAD_MB:-500}

if [[ ! -f ${APP_DIR}/requirements.txt || ! -f ${APP_DIR}/app/main.py ]]; then
    printf "Project files were not found in %s\n" "${APP_DIR}" >&2
    exit 1
fi
if ! id "${APP_USER}" >/dev/null 2>&1; then
    printf "Application user does not exist: %s\n" "${APP_USER}" >&2
    exit 1
fi

APP_GROUP=$(id -gn "${APP_USER}")
APP_HOME=$(getent passwd "${APP_USER}" | cut -d: -f6)

apt-get update
apt-get install -y --no-install-recommends ca-certificates curl ffmpeg git libgl1 libglib2.0-0 nginx python3 python3-pip python3-venv

install -d -o "${APP_USER}" -g "${APP_GROUP}" /var/lib/depth-motion/jobs
install -d -o "${APP_USER}" -g "${APP_GROUP}" /var/cache/depth-motion/huggingface
install -d -o "${APP_USER}" -g "${APP_GROUP}" /var/cache/depth-motion/ms-playwright
chown -R "${APP_USER}:${APP_GROUP}" "${APP_DIR}"

as_app_user() {
    runuser -u "${APP_USER}" -- env HOME="${APP_HOME}" "$@"
}

if [[ ! -x ${APP_DIR}/.venv/bin/python ]]; then
    as_app_user python3 -m venv "${APP_DIR}/.venv"
fi
as_app_user "${APP_DIR}/.venv/bin/python" -m pip install --upgrade pip
as_app_user "${APP_DIR}/.venv/bin/python" -m pip install -r "${APP_DIR}/requirements.txt"
"${APP_DIR}/.venv/bin/python" -m playwright install-deps chromium
as_app_user env PLAYWRIGHT_BROWSERS_PATH=/var/cache/depth-motion/ms-playwright "${APP_DIR}/.venv/bin/python" -m playwright install chromium

if [[ ! -f ${APP_DIR}/model/model.safetensors ]]; then
    as_app_user env HF_HOME=/var/cache/depth-motion/huggingface "${APP_DIR}/.venv/bin/python" "${APP_DIR}/scripts/download-model.py" --model-id "${MODEL_ID}" --destination "${APP_DIR}/model"
fi

render() {
    sed -e "s|@APP_DIR@|${APP_DIR}|g" -e "s|@APP_USER@|${APP_USER}|g" -e "s|@APP_GROUP@|${APP_GROUP}|g" -e "s|@SERVER_NAME@|${SERVER_NAME}|g" "$1" > "$2"
}

render "${APP_DIR}/deploy/systemd/depth-motion.service" /etc/systemd/system/depth-motion.service
render "${APP_DIR}/deploy/systemd/depth-motion-cleanup.service" /etc/systemd/system/depth-motion-cleanup.service
install -m 0644 "${APP_DIR}/deploy/systemd/depth-motion-cleanup.timer" /etc/systemd/system/depth-motion-cleanup.timer

cat > /etc/depth-motion.env <<EOF
DATA_DIR=/var/lib/depth-motion
HF_HOME=/var/cache/depth-motion/huggingface
PLAYWRIGHT_BROWSERS_PATH=/var/cache/depth-motion/ms-playwright
MAX_UPLOAD_MB=${MAX_UPLOAD_MB}
DEPTH_MODEL_ID=${APP_DIR}/model
HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1
PYTHONDONTWRITEBYTECODE=1
PYTHONUNBUFFERED=1
EOF
chmod 0644 /etc/depth-motion.env

render "${APP_DIR}/deploy/nginx/depth-motion.conf" /etc/nginx/sites-available/depth-motion
ln -sfn /etc/nginx/sites-available/depth-motion /etc/nginx/sites-enabled/depth-motion
rm -f /etc/nginx/sites-enabled/default

systemctl daemon-reload
nginx -t
systemctl enable nginx depth-motion.service depth-motion-cleanup.timer
systemctl restart depth-motion.service
systemctl restart depth-motion-cleanup.timer
systemctl reload-or-restart nginx

for _ in {1..30}; do
    if curl --fail --silent --show-error http://127.0.0.1:8000/health; then
        printf "\nDepth Motion deployment completed.\n"
        exit 0
    fi
    sleep 1
done

printf "Depth Motion health check failed.\n" >&2
journalctl -u depth-motion.service -n 50 --no-pager >&2
exit 1
