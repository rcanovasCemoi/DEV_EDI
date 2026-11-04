#!/usr/bin/env bash
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR=/opt/edi-sync
CONF_DIR=/etc/edi-sync
STATE_DIR=/var/lib/edi-sync
LOG_DIR=/var/log/edi-sync
SVC_USER=edisync

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Ce script doit etre execute en root (sudo)." >&2
  exit 1
fi

echo "==> Creation de l'utilisateur systeme ${SVC_USER}"
if ! id "${SVC_USER}" >/dev/null 2>&1; then
  useradd --system --home-dir "${STATE_DIR}" --shell /usr/sbin/nologin "${SVC_USER}"
fi

echo "==> Creation des repertoires"
install -d -o "${SVC_USER}" -g "${SVC_USER}" -m 0750 "${STATE_DIR}" "${STATE_DIR}/staging"
install -d -o root -g root -m 0755 "${APP_DIR}"
install -d -o root -g root -m 0700 "${CONF_DIR}" "${CONF_DIR}/keys"
install -d -o "${SVC_USER}" -g "${SVC_USER}" -m 0750 "${LOG_DIR}"

echo "==> Deploiement du code dans ${APP_DIR}"
install -m 0755 "${SRC_DIR}/src/edi_sync.py" "${APP_DIR}/edi_sync.py"
install -m 0644 "${SRC_DIR}/requirements.txt" "${APP_DIR}/requirements.txt"

echo "==> Creation du venv et installation des dependances"
python3 -m venv "${APP_DIR}/venv"
"${APP_DIR}/venv/bin/pip" install --quiet --upgrade pip
"${APP_DIR}/venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"

echo "==> Installation de la configuration"
if [[ ! -f "${CONF_DIR}/config.ini" ]]; then
  install -m 0600 "${SRC_DIR}/config/config.ini.example" "${CONF_DIR}/config.ini"
  echo "   config copiée depuis config.ini.example -> ${CONF_DIR}/config.ini (a editer)"
else
  echo "   ${CONF_DIR}/config.ini existe deja, conservee"
fi
chmod 0600 "${CONF_DIR}/config.ini"

echo "==> Installation de l'unite systemd"
install -m 0644 "${SRC_DIR}/systemd/edi-sync.service" /etc/systemd/system/edi-sync.service
systemctl daemon-reload

echo
echo "Installation terminee. Etapes restantes :"
echo "  1. Deposer les cles SSH dans ${CONF_DIR}/keys/ (chmod 600)"
echo "  2. Editer ${CONF_DIR}/config.ini (hosts, users, empreintes sha256 des serveurs)"
echo "  3. Verifier la config : ${APP_DIR}/venv/bin/python ${APP_DIR}/edi_sync.py --config ${CONF_DIR}/config.ini --check"
echo "  4. Demarrer le service : systemctl enable --now edi-sync"
echo "  5. Suivre les logs : journalctl -u edi-sync -f"
