# SRV_EDI_TRANSFERT_SFTP

Daemon Linux, lancé en tant que **service systemd**, qui transfère en **temps réel** des fichiers EDI entre deux serveurs SFTP, de manière **bidirectionnelle** (exemple avec babelway/keane) :

- `flow_babelway_to_keane` : `/TEST/EDI/OUT` (eu1.babelway.net) → `/opt/ASPEDI_FR/PRODUCTION/ENTREE/BW` (keane.cemoi.fr)
- `flow_keane_to_babelway` : `/opt/ASPEDI_FR/PRODUCTION/SORTIE/BW` (keane.cemoi.fr) → `/TEST/EDI/IN` (eu1.babelway.net)

## Démarrage rapide

```bash
# 1. cloner le depot
git clone https://github.com/rcanovasCemoi/DEV_EDI.git ~/DEV_EDI

# 2. installer (mode user sans root ; prefix ~/edi-sync par defaut, ex. /opt/EDI)
bash ~/DEV_EDI/SRV_EDI_TRANSFERT_SFTP/install/install.sh --user /opt/EDI
#    (mode system avec root : sudo bash ~/DEV_EDI/SRV_EDI_TRANSFERT_SFTP/install/install.sh)

# 3. configurer /opt/EDI/config.ini (voir config/config.ini.example) :
#    [sftp_*] host/port/user/key_file/host_fingerprint, [flow_*] source/dest dirs,
#    [general] local_archive_dir, [monitoring] bind/port/auth
#    empreintes : ssh-keyscan -t rsa,ecdsa,ed25519 <host> | ssh-keygen -lf -

# 4. verifier puis demarrer
/opt/EDI/venv/bin/python /opt/EDI/edi_sync.py --config /opt/EDI/config.ini --check
systemctl --user enable --now edi-sync        # ou: sudo systemctl enable --now edi-sync
# pour que le service demarre aussi au REBOOT du serveur sans session ouverte (une fois, root) :
#   sudo loginctl enable-linger $USER        # verif: loginctl show-user $USER | grep Linger -> yes

# 5. superviser
systemctl --user status edi-sync
curl -k -u admin:MOTDEPASSE https://127.0.0.1:8443/api/status   # depuis le serveur
# depuis ton poste : ssh -L 8443:127.0.0.1:8443 user@serveur-edi  puis https://localhost:8443/

# 6. tester
/opt/EDI/venv/bin/python ~/DEV_EDI/SRV_EDI_TRANSFERT_SFTP/tests/e2e_prod.py \
    --config /opt/EDI/config.ini --monitoring-password 'MOTDEPASSE'

# 7. mettre a jour plus tard
cd ~/DEV_EDI && bash SRV_EDI_TRANSFERT_SFTP/deploy.sh
```

## Architecture

```
┌─────────────────────────────────────┐                             ┌─────────────────────────────────────┐
│            sftp_babelway            │    flow_babelway_to_keane   │              sftp_keane             │
│           eu1.babelway.net          │  ────────────────────────►  │            keane.cemoi.fr           │
│          OUT: /TEST/EDI/OUT         │                             │              ENTREE/BW              │
│                                     │     flow_keane_to_babelway  │              SORTIE/BW              │
│          IN : /TEST/EDI/IN          │  ◄────────────────────────  │  (sous /opt/ASPEDI_FR/PRODUCTION/)  │
└─────────────────────────────────────┘                             └─────────────────────────────────────┘
```

Le daemon tourne sur un serveur Linux intermédiaire et gère **un thread par flux** (connexions SFTP dédiées par flux) :

| Composant | Rôle |
|---|---|
| Scanner (par flux) | Scanne la **racine** du répertoire source distant toutes les N secondes (pas de récursivité dans les sous-dossiers), détecte les fichiers nouveaux et *stables* |
| Queue persistante (SQLite) | Stocke chaque transfert avec son état (`pending`, `done`, `failed`), ses tentatives et ses timestamps ; au démarrage, les `failed` sont remis en `pending` |
| Worker (par flux) | Télécharge vers `staging/`, vérifie la taille source↔local, upload côté dest (direct au nom final si `dest_tmp_suffix` vide, sinon `.<nom>.tmp` + rename), archive localement, supprime la source |
| Purge | Supprime les archives locales de plus de `archive_retention_days` jours (toutes les heures) |
| Monitoring | Mini serveur HTTPS (voir ci-dessous) |
| Superviseur | Reconnexion SFTP automatique, keepalive 30 s, backoff exponentiel, arrêt propre sur SIGTERM/SIGINT |
| Service systemd | `edi-sync.service` (system) ou `edi-sync-user.service` (`--user`) : `Restart=always` |

## Règles de transfert

- **Anti-boucle** : seuls les répertoires sources (`source_dir`) sont scannés ; un fichier livré côté destination n'est jamais relu.
- **Scan racine seule** : les sous-dossiers de `source_dir` (ex. `KA/`, `REV/`, `SOL/`, `COPIES/`) **ne sont pas parcourus**.
- **Fichier stable** : taille inchangée entre 2 scans consécutifs ; les suffixes `.tmp`/`.part` et fichiers cachés sont ignorés.
- **Anti-doublon** : clé unique `(flow, nom, taille, mtime)` dans la queue SQLite.
- **Upload côté destination** : option `dest_tmp_suffix` par flow. **Vide (recommandé ici) = upload direct au nom final** — requis quand la plateforme destination prend les fichiers instantanément (babelway IN). Valeur `.tmp` = upload en `.<nom>.tmp` puis rename (atomique, pour un serveur SFTP classique). Si le `.tmp` a disparu juste après l'upload (pris par la plateforme), le transfert est considéré livré (warning).
- **Après succès** : le fichier est archivé **localement** dans `local_archive_dir/<flow>/` (ex. `/opt/EDI/ARCHIVE/flow_babelway_to_keane/`), puis **supprimé du SFTP source**, état `done`.
- **Purge** : les archives locales sont supprimées automatiquement après N jours (`archive_retention_days`, 7 par défaut).
- **Conflit de nom** côté `IN` : même nom + même taille = déjà livré (ignoré) ; taille différente = upload sous `<nom>.dup-<timestamp>`.
- **Échec** : 5 tentatives avec backoff (30 s → 1 min → 5 min → 15 min → 1 h), puis `failed` + alerte. Au redémarrage du daemon, les `failed` sont automatiquement remis en `pending` (nouvel essai).

## Structure

```
SRV_EDI_TRANSFERT_SFTP/
├── README.md                  # ce fichier
├── requirements.txt           # paramiko
├── src/
│   └── edi_sync.py            # daemon (flows + monitoring HTTPS en threads, queue SQLite, paramiko)
├── config/
│   ├── config.ini.example         # exemple generique de configuration
│   └── config.ini.system.example  # template MODE SYSTEM (srv025-gw-edi) : chemins /etc,/opt,/var, placeholders secrets
├── systemd/
│   ├── edi-sync.service       # unité systemd (mode system, root)
│   └── edi-sync-user.service  # unité systemd --user (mode sans root, template __APP_DIR__)
├── install/
│   ├── install.sh             # playbook d'installation (modes system / --user [PREFIX])
│   └── deploy.sh              # mise a jour apres git pull (pull + copie + restart + checks, +x)
├── tests/
│   ├── test_edi_sync.py       # tests unitaires (logique pure + serveur HTTP de monitoring, loopback)
│   ├── sftp_test_server.py    # 2 serveurs SFTP de test en loopback (pour le E2E local)
│   ├── e2e_local.py           # E2E local complet : lance serveurs de test + daemon + scenarios (✅)
│   ├── recette.py             # recette de fonctionnement (service, monitoring, transfert de test)
│   └── e2e_prod.py            # test E2E automatique en production, checks ✅ et bandeau final (+x)
└── docs/
    ├── architecture.md        # décisions d'architecture détaillées
    └── monitoring-ui.png      # capture de la page de monitoring (données de démo)
```

## Tests

```bash
cd SRV_EDI_TRANSFERT_SFTP
python -m py_compile src/edi_sync.py
python -m unittest discover -s tests -v          # unitaires (sans paramiko)
python src/edi_sync.py --config config/config.ini.example --check
pip install paramiko && python tests/e2e_local.py   # E2E local complet (2 serveurs SFTP de test)
```

Les tests unitaires couvrent la logique pure (config, queue SQLite, stabilité, backoff, nommage) sans dépendance à paramiko ni à un serveur SFTP. La CI GitHub Actions les rejoue à chaque push/PR sur ce sous-dossier.

## Recette de fonctionnement (en production, sur le serveur EDI)

`tests/recette.py` vérifie le service actif, le monitoring (`/health`, `/api/status`, heartbeats) et fait un **vrai transfert de test** (`TEST_EDI_SYNC_<ts>.edi`) par flow : dépôt source → livraison destination → suppression source → archive locale.

```bash
/opt/EDI/venv/bin/python ~/DEV_EDI/SRV_EDI_TRANSFERT_SFTP/tests/recette.py \
    --config /opt/EDI/config.ini --monitoring-password 'MOTDEPASSE'
# un seul flow :
... --flow flow_keane_to_babelway
```

> Le dépôt du fichier test se fait dans `source_dir`. Si le compte SFTP est en read-only sur ce répertoire (ex. `/TEST/EDI/OUT` sur babelway), lancer la recette sur l'autre flow (ex. `flow_keane_to_babelway`, `SORTIE/BW` est writable) ou demander à la plateforme de déposer un fichier test.

## Test E2E automatique en production (checks ✅)

```bash
/opt/EDI/venv/bin/python ~/DEV_EDI/SRV_EDI_TRANSFERT_SFTP/tests/e2e_prod.py \
    --config /opt/EDI/config.ini --monitoring-password 'MOTDEPASSE'
```

Vérifie config, service, monitoring (`/health`, `/api/status`, heartbeats), puis fait un **vrai transfert de test** (`TEST_E2E_<ts>.edi`) par flow : dépôt source → livraison destination → suppression source → archive locale. Affiche ✅ par check et un bandeau final `✅ E2E VALIDE — n/n checks OK` (exit 0) ou `❌ E2E EN ECHEC` (exit 1). Un flow en read-only côté source est SKIP (⏭️).

## Configuration

Fichier `/etc/edi-sync/config.ini` sur le serveur cible (voir `config/config.ini.example`) :

```ini
[general]
scan_interval_sec = 5
stability_scans = 2
max_retries = 5
archive_retention_days = 7
state_dir = /var/lib/edi-sync
local_archive_dir = /opt/EDI/ARCHIVE   # archive locale : <local_archive_dir>/<flow>/ ; vide = pas d'archive

[sftp_babelway]
host = sftp-babelway.exemple.com
port = 22
user = edi
key_file = /etc/edi-sync/keys/id_rsa_babelway
host_fingerprint = sha256:xxxxx

[sftp_keane]
host = sftp-keane.exemple.com
port = 22
user = edi
key_file = /etc/edi-sync/keys/id_rsa_keane
host_fingerprint = sha256:yyyyy

[flow_babelway_to_keane]
source = sftp_babelway
source_dir = /out
dest = sftp_keane
dest_dir = /in

[flow_keane_to_babelway]
source = sftp_keane
source_dir = /out
dest = sftp_babelway
dest_dir = /in
```

### Référence des options

| Section | Option | Défaut | Rôle |
|---|---|---|---|
| `[general]` | `scan_interval_sec` | 5 | période de scan du répertoire source |
| | `stability_scans` | 2 | nb de scans à taille identique avant de traiter un fichier |
| | `max_retries` | 5 | tentatives max avant `failed` (backoff 30 s → 1 h) |
| | `archive_retention_days` | 7 | rétention des archives locales (purge horaire) |
| | `state_dir` | `/var/lib/edi-sync` | queue.db, staging/, heartbeats, certs |
| | `local_archive_dir` | vide | archive locale `<dir>/<flow>/` ; vide = pas d'archive |
| | `log_file` | vide | copie des logs JSON dans un fichier |
| `[sftp_*]` | `host`, `port`, `user` | — | endpoint SFTP |
| | `key_file` | — | clé privée SSH (600) |
| | `host_fingerprint` | — | une ou plusieurs `sha256:...` (virgules), vérifiées à chaque connexion |
| `[flow_*]` | `source`, `dest` | — | références aux sections `[sftp_*]` |
| | `source_dir`, `dest_dir` | — | répertoires distant source/destination (racine seule, pas de récursivité) |
| | `dest_tmp_suffix` | `.tmp` | suffixe temporaire côté dest ; **vide = upload direct au nom final** |
| `[monitoring]` | `enabled`, `bind`, `port` | true, 127.0.0.1, 8443 | serveur HTTPS de supervision (loopback !) |
| | `tls_cert`, `tls_key` | auto-signé | certificat HTTPS |
| | `auth_user`, `auth_password_sha256` | vide | Basic Auth optionnelle |

## Installation (serveur Linux cible)

**Mode system (recommandé, nécessite root)** — préfixe par défaut `/opt/edi-sync`, personnalisable avec `--prefix` (l'unité systemd est générée avec ce chemin) :

```bash
sudo bash install/install.sh                          # -> /opt/edi-sync
sudo bash install/install.sh --prefix /opt/EDI   # -> /opt/EDI
sudo systemctl enable --now edi-sync
```

Crée l'utilisateur système `edisync`, déploie le code dans `/opt/edi-sync/`, la config dans `/etc/edi-sync/` (600, owner edisync) depuis **`config/config.ini.system.example`** (template mode system avec les bons chemins ; remplacer localement les placeholders `__FP_*__` et `__SHA256_MDP__` — ne pas commiter de vrais secrets dans le dépôt public) + `keys/` (700), l'état dans `/var/lib/edi-sync/` et l'unité systemd système.

**Mode user (sans aucun droit root)** — le daemon tourne sous ton compte. Préfixe par défaut `~/edi-sync`, personnalisable. Le template **`config/config.ini.user.example`** est utilisé (chemins relatifs au préfixe, `local_archive_dir=/opt/EDI/ARCHIVE`, clés `~/.ssh`) :

```bash
# deploiement dans /opt/EDI en tant que babelway (l'admin gere le service/boot) :
git clone https://github.com/rcanovasCemoi/DEV_EDI.git ~/DEV_EDI
bash ~/DEV_EDI/SRV_EDI_TRANSFERT_SFTP/install/install.sh --user /opt/EDI
# remplir dans /opt/EDI/config.ini uniquement les placeholders __FP_BABELWAY__, __FP_KEANE__, __SHA256_MDP__
/opt/EDI/venv/bin/python /opt/EDI/edi_sync.py --config /opt/EDI/config.ini --check
systemctl --user enable --now edi-sync    # (ou le service/boot gere par l'admin)
```

Autre préfixe (ex. `/opt/EDI`) :

```bash
git clone https://github.com/rcanovasCemoi/DEV_EDI.git ~/DEV_EDI
bash ~/DEV_EDI/SRV_EDI_TRANSFERT_SFTP/install/install.sh --user /opt/EDI
# éditer /opt/EDI/config.ini : key_file pointent directement vers ~/.ssh/id_rsa_* (pas de copie)
/opt/EDI/venv/bin/python /opt/EDI/edi_sync.py --config /opt/EDI/config.ini --check
systemctl --user enable --now edi-sync
systemctl --user status edi-sync
journalctl --user -u edi-sync -f    # ou: tail -f /opt/EDI/log/daemon.log si log_file renseigné
```

> Pour que le service user démarre aussi sans session ouverte, demander à l'admin root une seule fois : `loginctl enable-linger $USER` (sinon le service démarre à ta première connexion).

## Démarrage au boot du serveur (3 options)

| Option | Commande | Quand |
|---|---|---|
| **Mode system** (root, une fois) | `sudo install/install.sh` + `sudo systemctl enable edi-sync` | si tu as les droits root : démarrage natif au boot, supervision `Restart=always` |
| **Linger** (root, une fois) | `sudo loginctl enable-linger $USER` | en mode user : le service `systemctl --user` démarre au boot sans session ouverte (vérif : `loginctl show-user $USER \| grep Linger` → `yes`) |
| **Cron @reboot** (sans aucun root) | `install.sh --user <prefix> --cron` | ajoute `@reboot <prefix>/run.sh` au crontab de ton compte + mini-superviseur `run.sh` (relance toutes les 10 s si le daemon tombe) |

```bash
# option cron (sans root) :
bash ~/DEV_EDI/SRV_EDI_TRANSFERT_SFTP/install/install.sh --user /opt/EDI --cron
crontab -l | grep run.sh        # verif: @reboot /opt/EDI/run.sh
```

## Mise à jour après un `git pull`

```bash
cd ~/DEV_EDI
bash SRV_EDI_TRANSFERT_SFTP/deploy.sh              # pull + deploiement + restart + verifications
bash SRV_EDI_TRANSFERT_SFTP/deploy.sh --recette    # + recette de fonctionnement (tests/recette.py)
```

`deploy.sh` détecte automatiquement le mode (system/user) et le préfixe, copie le daemon, relance l'installation complète uniquement si `install.sh` / `requirements.txt` / les unités systemd ont changé (sha suivi dans `<prefix>/.deploy-sha`), redémarre le service et vérifie (`--check`, service actif, sonde `/health`).

## Supervision

```bash
systemctl status edi-sync
journalctl -u edi-sync -f
```

- Heartbeat par flow : `<state_dir>/heartbeat_flow_babelway_to_keane`, `heartbeat_flow_keane_to_babelway` (nommés d'après les sections `[flow_*]`)
- Métriques `pending/done/failed` par flow dans la base SQLite (`<state_dir>/queue.db`)
- Logs JSON sur stdout (journald) ; option `[general] log_file = /chemin/daemon.log` pour aussi écrire dans un fichier (utile en mode user sans accès au journal). Les erreurs incluent un champ `traceback`. Le bruit paramiko (bannière du serveur SFTP, « Connected… », « Auth OK ») est filtré (logger `paramiko` en WARNING) ; pour supprimer la bannière à la source, c'est côté serveur (directive `Banner` de sshd, à retirer par l'admin du serveur SFTP).
- Alerte si heartbeat absent ou si des transferts sont en `failed`

## Monitoring (mini serveur HTTPS embarqué)

Le daemon embarque un serveur HTTPS dans un thread dédié (même process, état partagé avec la queue) :

- **Accès** : bind `127.0.0.1:8443` par défaut — **ne jamais binder `0.0.0.0`**. Accès recommandé via tunnel SSH : `ssh -L 8443:127.0.0.1:8443 utilisateur@serveur-edi`, puis `https://localhost:8443/` (ou proxy SOCKS `ssh -D 1080 ...`). Alternative en réseau interne maîtrisé : `bind = <IP privée du serveur>` + règle pare-feu n'autorisant le port 8443 **que depuis le sous-réseau LAN** + Basic Auth (jamais ouvert sur Internet, jamais `0.0.0.0`). Pour supprimer l'alerte « certificat non sécurisé » du navigateur : fournir `tls_cert`/`tls_key` signés par votre CA interne.

Exemple (serveur 172.25.1.89, LAN 172.25.0.0/16) :

```ini
[monitoring]
bind = 172.25.1.89
port = 8443
```

```bash
# pare-feu : 8443 uniquement depuis le LAN (firewalld)
sudo firewall-cmd --permanent --add-rich-rule='rule family=ipv4 source address=172.25.0.0/16 port protocol=tcp port=8443 accept'
sudo firewall-cmd --permanent --add-rich-rule='rule family=ipv4 port protocol=tcp port=8443 drop'
sudo firewall-cmd --reload
# ou iptables :
#   sudo iptables -I INPUT -p tcp --dport 8443 -s 172.25.0.0/16 -j ACCEPT
#   sudo iptables -I INPUT -p tcp --dport 8443 -j DROP
```
- `GET /` : page HTML de suivi des flux (état `ok`/`stale`/`error`, heartbeat, compteurs `pending/done/failed`, 30 derniers événements avec leur champ `error`, auto-refresh 10 s)
- `GET /api/status` : mêmes données en JSON (événements avec `error`)
- `GET /health` : sonde `200 ok`
- Certificat **auto-signé** généré au premier démarrage dans `/var/lib/edi-sync/certs/` (ou cert/key fournis via `tls_cert` / `tls_key` dans `[monitoring]`)
- **Basic Auth optionnelle** : `auth_user` + `auth_password_sha256` (sha256 du mot de passe, ex. `echo -n 'motdepasse' | sha256sum`)

Exemple de section `[monitoring]` (voir `config/config.ini.example`) :

```ini
[monitoring]
enabled = true
bind = 127.0.0.1
port = 8443
auth_user = admin
auth_password_sha256 = <sha256 du mot de passe>
```

Aperçu de la page en production (`https://172.25.1.89:8443/`, les deux flows `ok`) :

![Page de monitoring](docs/monitoring-ui.png)

## Sécurité

- **Tous les mots de passe / secrets sont dans le KeePass de l'entreprise** (y compris le mot de passe du monitoring Basic Auth) — jamais dans le dépôt, jamais en clair dans les fichiers de config (on ne stocke que son hash `sha256` dans `auth_password_sha256`).
- Clés SSH dédiées par sens de transfert (`chmod 600`), **pas de mot de passe**
- Empreintes du serveur SFTP épinglées (`host_fingerprint` : une ou plusieurs `sha256:...` séparées par des virgules — récupérées via `ssh-keyscan -t rsa,ecdsa,ed25519 <host> | ssh-keygen -lf -`), vérification à chaque connexion
- Config et clés lisibles uniquement par l'utilisateur de service `edisync` (et `root`) : `/etc/edi-sync/config.ini` en 600 owner `edisync`, `/etc/edi-sync/keys/` en 700 owner `edisync`, clés en 600
- Service systemd sandboxé : `NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`, `ReadWritePaths` limités

## FAQ / problèmes connus

- **`Permission denied` au dépôt dans `source_dir`** : le compte SFTP est en read-only sur ce répertoire (ex. `/TEST/EDI/OUT` sur babelway) → le transfert fonctionne en lecture, mais le test de dépôt échouera (SKIP dans `e2e_prod.py`) ; tester via l'autre sens ou un dépôt manuel plateforme/channel.
- **Fichier reçu en `.nom.tmp` côté plateforme** : la plateforme destination prend le `.tmp` avant le rename → mettre `dest_tmp_suffix =` (vide) dans le flow concerné = upload direct au nom final.
- **`empreinte du serveur ... invalide`** : le serveur a plusieurs clés d'hôte (RSA/ECDSA/ED25519) et paramiko en choisit une autre → lister toutes les empreintes : `ssh-keyscan -t rsa,ecdsa,ed25519 <host> | ssh-keygen -lf -` et les mettre toutes dans `host_fingerprint` (séparées par des virgules).
- **`journalctl --user` : permission denied** → demander à l'admin root `usermod -aG systemd-journal $USER` (reconnexion), ou renseigner `log_file` dans `[general]` et lire ce fichier.
- **Service user inactif sans session ouverte / ne démarre pas au reboot du serveur** → demander à l'admin root, une fois pour toutes : `sudo loginctl enable-linger $USER` (vérif : `loginctl show-user $USER | grep Linger` → `Linger=yes`). Le service démarrera alors automatiquement à chaque boot, sans session ouverte.
- **`config invalide: File contains no section headers`** → le heredoc de la config n'a pas été rempli correctement (vérifier `cat config.ini`).
- **`Address already in use` sur le port monitoring** → un autre process edi-sync tourne encore (`pkill -f edi_sync.py`, attendre la fin du restart).
- **Port 8443 inaccessible depuis mon poste mais OK en local** (`Test-NetConnection` False, ping OK) → ACL inter-VLAN : le port 8443 n'est pas ouvert entre le VLAN des postes et celui du serveur. Solutions : (a) demander à l'admin réseau d'ouvrir TCP/8443 poste→serveur, puis accéder directement à `https://<IP-serveur>:8443/` ; (b) sinon, tunnel SSH par utilisateur (le port 22 passe déjà) : `ssh -L 8443:<IP-serveur>:8443 utilisateur@serveur-edi` puis `https://localhost:8443/`.
- **Après un crash, les `failed` ne repartent pas** → normal : ils sont remis en `pending` automatiquement à chaque redémarrage du daemon (`systemctl --user restart edi-sync`).

## Faire évoluer le projet

```bash
# tests en local (unitaires + E2E local avec 2 serveurs SFTP de test en loopback)
cd SRV_EDI_TRANSFERT_SFTP
python -m unittest discover -s tests -v          # unitaires (sans paramiko)
pip install paramiko && python tests/e2e_local.py  # E2E local complet (15 checks ✅)

# workflow de contribution
git checkout -b vibe/<mon-sujet>-0b70ca
# ... modifier ...
git commit -m "..." && git push -u origin vibe/<mon-sujet>-0b70ca
gh pr create --repo rcanovasCemoi/DEV_EDI --draft   # CI lance les tests automatiquement
# merge dans main une fois la CI verte
```

La CI (`.github/workflows/tests-srv-edi-transfert-sftp.yml`) lance à chaque push/PR sur ce dossier : syntaxe, tests unitaires, `--check` sur l'exemple de config, et le **E2E local** (`tests/e2e_local.py`).
