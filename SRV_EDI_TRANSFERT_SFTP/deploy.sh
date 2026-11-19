#!/usr/bin/env bash
# deploy.sh — applique les modifications du repo sur le serveur EDI apres un git pull.
#
# Usage (depuis le clone du repo, ex. ~/DEV_EDI) :
#   bash SRV_EDI_TRANSFERT_SFTP/deploy.sh                     # pull + deploiement + restart + checks
#   bash SRV_EDI_TRANSFERT_SFTP/deploy.sh --recette           # + lance la recette (tests/recette.py)
#   bash SRV_EDI_TRANSFERT_SFTP/deploy.sh --prefix /opt/EDI/edi-sync
#   bash SRV_EDI_TRANSFERT_SFTP/deploy.sh --no-restart        # deploie sans redemarrer (verifs minimales)
#
# Le mode (system/user) et le prefix sont detectes automatiquement depuis l'unite systemd
# ou /opt/edi-sync ; sinon --prefix est requis.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJ="$REPO_DIR/SRV_EDI_TRANSFERT_SFTP"
cd "$REPO_DIR"

PREFIX=""
RECETTE=0
RESTART=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --recette) RECETTE=1; shift ;;
    --no-restart) RESTART=0; shift ;;
    *) echo "option inconnue: $1" >&2; exit 2 ;;
  esac
done

echo "==> git pull dans $REPO_DIR"
git pull --ff-only

MODE=""
if [[ -z "$PREFIX" ]]; then
  if [[ -f "$HOME/.config/systemd/user/edi-sync.service" ]]; then
    MODE=user
    PREFIX=$(sed 's/^ExecStart=//' "$HOME/.config/systemd/user/edi-sync.service" | awk '{print $2}' | xargs dirname)
  elif [[ -f /opt/edi-sync/edi_sync.py ]]; then
    MODE=system
    PREFIX=/opt/edi-sync
  else
    echo "prefix inconnu : passer --prefix /opt/EDI/edi-sync" >&2
    exit 2
  fi
else
  MODE=user
  [[ -f /opt/edi-sync/edi_sync.py ]] && MODE=system
fi
echo "==> mode=$MODE prefix=$PREFIX"

LAST=""
[[ -f "$PREFIX/.deploy-sha" ]] && LAST="$(cat "$PREFIX/.deploy-sha")"
HEAD_SHA="$(git rev-parse HEAD)"

echo "==> copie du daemon -> $PREFIX/edi_sync.py"
install -d -m 0750 "$PREFIX"
install -m 0755 "$PROJ/src/edi_sync.py" "$PREFIX/edi_sync.py"

CHANGED=""
if [[ -n "$LAST" ]]; then
  CHANGED=$(git diff --name-only "$LAST" "$HEAD_SHA" -- "$PROJ/install" "$PROJ/requirements.txt" "$PROJ/systemd" 2>/dev/null || true)
fi
if [[ -z "$LAST" || -n "$CHANGED" ]]; then
  echo "==> install complet (premier deploy ou fichiers d'install modifies${CHANGED:+: $CHANGED})"
  if [[ "$MODE" == "system" ]]; then
    sudo bash "$PROJ/install/install.sh"
  else
    bash "$PROJ/install/install.sh" --user "$PREFIX"
  fi
fi
echo "$HEAD_SHA" > "$PREFIX/.deploy-sha"

if [[ "$RESTART" -eq 1 ]]; then
  echo "==> redemarrage du service edi-sync"
  if [[ "$MODE" == "system" ]]; then
    sudo systemctl restart edi-sync
    sleep 2
    sudo systemctl is-active edi-sync
  else
    systemctl --user restart edi-sync
    sleep 2
    systemctl --user is-active edi-sync
  fi
fi

echo "==> verification config"
"$PREFIX/venv/bin/python" "$PREFIX/edi_sync.py" --config "$PREFIX/config.ini" --check

if [[ "$RESTART" -eq 1 ]]; then
  echo "==> sonde monitoring (best-effort)"
  BIND=$(sed -n 's/^bind *= *//p' "$PREFIX/config.ini" | head -1); BIND="${BIND:-127.0.0.1}"
  PORT=$(sed -n 's/^port *= *//p' "$PREFIX/config.ini" | head -1); PORT="${PORT:-8443}"
  curl -sk -o /dev/null -w "GET /health -> HTTP %{http_code}\n" "https://$BIND:$PORT/health" || true
fi

if [[ "$RECETTE" -eq 1 ]]; then
  echo "==> recette (tests/recette.py)"
  "$PREFIX/venv/bin/python" "$PROJ/tests/recette.py" --config "$PREFIX/config.ini"
fi

echo "==> deploiement termine (sha $HEAD_SHA, mode $MODE, prefix $PREFIX)"
