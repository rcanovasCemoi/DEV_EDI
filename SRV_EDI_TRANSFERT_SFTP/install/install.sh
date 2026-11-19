#!/usr/bin/env bash
set -euo pipefail

MODE=system
CRON=0
PREFIX=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) MODE=user; shift ;;
    --cron) CRON=1; shift ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --*) echo "option inconnue: $1" >&2; exit 2 ;;
    *) if [[ -z "$PREFIX" ]]; then PREFIX="$1"; fi; shift ;;   # positionnel = prefix
  esac
done
# usage : install.sh [--prefix /opt/XXX]                  -> mode system (root), prefix defaut /opt/edi-sync
#         install.sh --user [PREFIX] [--cron] [--prefix P] -> mode user
# --cron (mode user) : ajoute un @reboot au crontab -> demarrage au boot du serveur SANS root
# usage : install.sh            -> mode system (root), prefix /opt/edi-sync
#         install.sh --user [PREFIX] -> mode user, prefix par defaut ~/edi-sync (ex: /opt/EDI/edi-sync)

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "$MODE" == "system" ]]; then
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "Mode system : execute en root (sudo). Sans root, utiliser : install.sh --user [PREFIX]" >&2
    exit 1
  fi
  APP_DIR="${PREFIX:-/opt/edi-sync}"
  CONF_DIR=/etc/edi-sync
  STATE_DIR=/var/lib/edi-sync
  LOG_DIR=/var/log/edi-sync
  SVC_USER=edisync
  UNIT_DIR=/etc/systemd/system
  UNIT_SRC="${SRC_DIR}/systemd/edi-sync.service"
  SYSTEMCTL="systemctl"
  OWNER_ARGS=(-o "${SVC_USER}" -g "${SVC_USER}")
  CONF_OWNER="${SVC_USER}:${SVC_USER}"
  echo "==> Mode system (root) : utilisateur de service ${SVC_USER}"
  if ! id "${SVC_USER}" >/dev/null 2>&1; then
    useradd --system --home-dir "${STATE_DIR}" --shell /usr/sbin/nologin "${SVC_USER}"
  fi
  install -d -o "${SVC_USER}" -g "${SVC_USER}" -m 0750 "${STATE_DIR}" "${STATE_DIR}/staging"
  install -d -o root -g root -m 0755 "${APP_DIR}"
  install -d -o root -g root -m 0755 "${CONF_DIR}"
  install -d -o "${SVC_USER}" -g "${SVC_USER}" -m 0700 "${CONF_DIR}/keys"
  install -d -o "${SVC_USER}" -g "${SVC_USER}" -m 0750 "${LOG_DIR}"
else
  APP_DIR="${2:-${HOME}/edi-sync}"
  CONF_DIR="${APP_DIR}"
  STATE_DIR="${APP_DIR}/state"
  LOG_DIR="${APP_DIR}/log"
  UNIT_DIR="${HOME}/.config/systemd/user"
  UNIT_SRC="${SRC_DIR}/systemd/edi-sync-user.service"
  SYSTEMCTL="systemctl --user"
  OWNER_ARGS=()
  CONF_OWNER="$(id -un)"
  echo "==> Mode user : installation dans ${APP_DIR} (aucun droit root requis)"
  install -d -m 0750 "${APP_DIR}" "${STATE_DIR}" "${STATE_DIR}/staging" "${LOG_DIR}" "${UNIT_DIR}"
fi

echo "==> Deploiement du code dans ${APP_DIR}"
install -m 0755 "${SRC_DIR}/src/edi_sync.py" "${APP_DIR}/edi_sync.py"
install -m 0644 "${SRC_DIR}/requirements.txt" "${APP_DIR}/requirements.txt"

echo "==> venv + dependances"
python3 -m venv "${APP_DIR}/venv"
"${APP_DIR}/venv/bin/pip" install --quiet --upgrade pip
"${APP_DIR}/venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"

echo "==> Configuration"
if [[ ! -f "${CONF_DIR}/config.ini" ]]; then
  # en mode system, preferer le template system (chemins /etc,/opt,/var) s'il existe
  if [[ "$MODE" == "system" && -f "${SRC_DIR}/config/config.ini.system.example" ]]; then
    EXAMPLE="${SRC_DIR}/config/config.ini.system.example"
  else
    EXAMPLE="${SRC_DIR}/config/config.ini.example"
  fi
  install "${OWNER_ARGS[@]}" -m 0600 "${EXAMPLE}" "${CONF_DIR}/config.ini"
  echo "   config copiée -> ${CONF_DIR}/config.ini (a editer)"
else
  echo "   ${CONF_DIR}/config.ini existe deja, conservee"
fi
chown "${CONF_OWNER}" "${CONF_DIR}/config.ini" 2>/dev/null || true
chmod 0600 "${CONF_DIR}/config.ini"

echo "==> Unite systemd dans ${UNIT_DIR} (prefix ${APP_DIR})"
sed "s|__APP_DIR__|${APP_DIR}|g" "${UNIT_SRC}" > "${UNIT_DIR}/edi-sync.service.tmp"
install -m 0644 "${UNIT_DIR}/edi-sync.service.tmp" "${UNIT_DIR}/edi-sync.service"
rm -f "${UNIT_DIR}/edi-sync.service.tmp"
if ! ${SYSTEMCTL} daemon-reload 2>/dev/null; then
  echo "WARN: '${SYSTEMCTL} daemon-reload' a echoue (systemd ${MODE} non actif ici ?) - l'unite est ecrite, charge-la plus tard" >&2
fi
echo "${APP_DIR}" > "${APP_DIR}/.deploy-prefix"

echo
echo "Installation terminee (mode ${MODE}). Etapes restantes :"
if [[ "$MODE" == "system" ]]; then
  echo "  1. Cles SSH dans ${CONF_DIR}/keys/ :"
  echo "     sudo install -o ${SVC_USER} -g ${SVC_USER} -m 600 <cle_privee> ${CONF_DIR}/keys/id_rsa_xxx"
else
  echo "  1. Dans ${CONF_DIR}/config.ini, pointer key_file directement vers tes cles existantes,"
  echo "     ex : key_file = ${HOME}/.ssh/id_rsa_babelway_edi  (aucune copie necessaire, tu es $(id -un))"
  echo "     Pour que le service tourne aussi sans session ouverte, demander a l'admin root :"
  echo "     loginctl enable-linger $(id -un)"
fi
echo "  2. Editer ${CONF_DIR}/config.ini (hosts, users, empreintes sha256 des serveurs)"
echo "  3. Verifier : ${APP_DIR}/venv/bin/python ${APP_DIR}/edi_sync.py --config ${CONF_DIR}/config.ini --check"
echo "  4. Demarrer : ${SYSTEMCTL} enable --now edi-sync"
if [[ "$MODE" == "user" ]]; then
  echo "  5. Logs : journalctl --user -u edi-sync -f"
else
  echo "  5. Logs : journalctl -u edi-sync -f"
fi

if [[ "$MODE" == "user" && "$CRON" == "1" ]]; then
  echo "==> cron @reboot (demarrage au boot sans root, avec mini-superviseur run.sh)"
  cat > "${APP_DIR}/run.sh" <<'RUNSH'
#!/usr/bin/env bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
while true; do
  "$DIR/venv/bin/python" "$DIR/edi_sync.py" --config "$DIR/config.ini" >> "$DIR/log/daemon.log" 2>&1
  echo "$(date -Is) edi_sync sorti rc=$? -> relance dans 10s" >> "$DIR/log/daemon.log"
  sleep 10
done
RUNSH
  chmod 0755 "${APP_DIR}/run.sh"
  if command -v crontab >/dev/null 2>&1; then
    (crontab -l 2>/dev/null | grep -v "edi-sync/run.sh"; echo "@reboot ${APP_DIR}/run.sh") | crontab -
    echo "   @reboot ajoute au crontab de $(id -un) : $(crontab -l 2>/dev/null | grep run.sh)"
  else
    echo "   WARN: crontab absent -> ajouter manuellement : @reboot ${APP_DIR}/run.sh"
  fi
fi
