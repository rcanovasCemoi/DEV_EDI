# Architecture — SRV_EDI_TRANSFERT_SFTP

## Vue d'ensemble

Daemon Python unique (`src/edi_sync.py`), déployé comme service systemd `edi-sync`, qui fait la navette **bidirectionnelle** de fichiers EDI entre deux serveurs SFTP :

```
┌──────────────────┐  flow_babelway_to_keane   ┌──────────────────┐
│  sftp_babelway   │ ─────────────────────────► │    sftp_keane    │
│  /TEST/EDI/OUT   │  OUT ──► ENTREE/BW        │  .../ENTREE/BW   │
│  /TEST/EDI/IN    │ ◄───────────────────────── │  .../SORTIE/BW   │
└──────────────────┘  flow_keane_to_babelway   └──────────────────┘
        (daemon edi-sync sur le serveur EDI)
```

## Threads du process

| Thread | Rôle |
|---|---|
| `flow-<name>` (un par flux) | Scan périodique du `OUT` source, détection des fichiers stables, traitement de la queue du flux (download → upload atomique → archivage) |
| `purge` | Suppression des archives plus vieilles que `archive_retention_days` (toutes les heures) |
| `monitoring` | Mini serveur HTTPS de supervision (voir ci-dessous) |
| main | Signaux SIGTERM/SIGINT, arrêt propre (join des threads) |

## Pourquoi le monitoring est dans le même process

- La page de suivi lit directement la queue SQLite (thread-safe, verrou interne) et les fichiers `heartbeat_<flow>` : aucun mécanisme inter-process.
- Un seul service systemd à superviser ; `Restart=always` couvre transfert + monitoring.
- Le serveur de monitoring est en lecture seule, bindé sur `127.0.0.1` par défaut, donc il n'impacte pas les flux.
- Un second daemon ne se justifierait que pour exposer la page à plusieurs équipes ou la rendre indépendante d'un crash du transfert (couvert ici par le restart systemd en 10 s).

## Règles de transfert

- **Anti-boucle** : seuls les répertoires sources sont scannés ; un fichier livré côté destination n'est jamais relu.
- **Scan racine seule** : pas de récursivité dans les sous-dossiers de `source_dir`.
- **Stabilité** : un fichier est traité quand sa taille est inchangée sur `stability_scans` scans consécutifs ; `.tmp`, `.part` et fichiers cachés ignorés.
- **Anti-doublon** : contrainte `UNIQUE(flow, filename, size, src_mtime)` en SQLite.
- **Upload destination** : `dest_tmp_suffix` par flow, `.tmp` par défaut (upload en `.<nom>.tmp` puis `rename`, atomique). Si vide → upload direct au nom final (plateformes type babelway qui prennent les fichiers dans `IN` instantanément). Si le `.tmp` a disparu juste après l'upload (pris par la plateforme), transfert considéré livré (warning) ; erreur seulement si taille constatée différente.
- **Post-traitement** : après upload OK, le fichier est archivé **localement** dans `local_archive_dir/<flow>/` (ex. `/opt/EDI/ARCHIVE/flow_babelway_to_keane/`), puis **supprimé du SFTP source** ; purge automatique des archives locales après `archive_retention_days`.
- **Conflit de nom** côté `IN` : même taille = déjà livré (on archive quand même localement et on supprime la source) ; taille différente = suffixe `.dup-<epoch>`.
- **Retry** : backoff 30 s → 1 min → 5 min → 15 min → 1 h, puis `failed` après `max_retries` tentatives. Au redémarrage, `pending` repris et `failed` remis en `pending` (nouvel essai).

## Monitoring HTTPS

- Section `[monitoring]` : `enabled`, `bind` (défaut `127.0.0.1`), `port` (défaut `8443`).
- Certificat auto-signé généré au premier démarrage dans `<state_dir>/certs/` (ou `tls_cert`/`tls_key` fournis).
- Basic Auth optionnelle : `auth_user` + `auth_password_sha256`.
- Endpoints : `GET /` (page HTML auto-refresh 10 s : état des flux, heartbeat, compteurs, derniers événements), `GET /api/status` (JSON), `GET /health` (`200 ok`).
- Accès : par défaut `127.0.0.1` + tunnel SSH (`ssh -L 8443:127.0.0.1:8443 user@serveur-edi` puis `https://localhost:8443/`) ; ou bind sur l'IP LAN du serveur + pare-feu restreint au sous-réseau interne + Basic Auth (jamais `0.0.0.0`).
- États d'un flux : `ok` (heartbeat récent), `stale` (heartbeat vieux), `error` (au moins un transfert `failed`), `unknown` (pas encore de heartbeat).

## Sécurité

- Clés SSH dédiées, `chmod 600`, pas de mot de passe ; empreintes `sha256` du serveur épinglées (liste séparée par virgules, une par clé d'hôte RSA/ECDSA/ED25519) et vérifiées à chaque connexion.
- Config et clés dans `/etc/edi-sync/` : dossier `root:root 755`, mais `config.ini` (600) et `keys/` (700 + clés 600) appartiennent à `edisync`, l'utilisateur du service systemd, afin que le daemon puisse les lire sans les exposer aux autres comptes.
- Service sandboxé : `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `ReadWritePaths=/var/lib/edi-sync /var/log/edi-sync`.
- Logs JSON sur stdout → journald (`journalctl -u edi-sync -f`), avec champ `traceback` sur les erreurs ; option `[general] log_file` pour aussi écrire dans un fichier.

## Déploiement & CI

- **Install** : `install.sh` (mode system, root, préfixe `--prefix`, template `config.ini.system.example` → `/etc/edi-sync/config.ini`) ou `install.sh --user [PREFIX] [--cron]` (sans root, template `config.ini.user.example`, unité `systemd --user`, `--cron` = `@reboot` + mini-superviseur `run.sh`). Le mode/préfixe sont enregistrés dans `<prefix>/.deploy-prefix`.
- **Mise à jour** : `deploy.sh` (détecte mode/préfixe, `git pull`, copie le daemon, réinstalle seulement si `install.sh`/`requirements.txt`/unités changés, restart, checks `--check`/service/`/health`, option `--recette`).
- **Boot du serveur** : mode system natif, ou `loginctl enable-linger $USER` (root, une fois), ou cron `@reboot` (sans root).
- **CI** (`.github/workflows/tests-srv-edi-transfert-sftp.yml`, filtre `paths`) : syntaxe, 43 tests unitaires, `--check` sur les templates, **E2E local** `tests/e2e_local.py` (2 serveurs SFTP de test en loopback `tests/sftp_test_server.py` + vrai daemon, ~15 checks ✅).
- **Recette/E2E en prod** : `tests/recette.py`, `tests/e2e_prod.py` (checks ✅ + bandeau final, exit 0/1).
