# Architecture — SRV_EDI_TRANSFERT_SFTP

## Vue d'ensemble

Daemon Python unique (`src/edi_sync.py`), déployé comme service systemd `edi-sync`, qui fait la navette **bidirectionnelle** de fichiers EDI entre deux serveurs SFTP :

```
┌─────────┐     flow_a : OUT → IN      ┌─────────┐
│ SFTP 1  │ ─────────────────────────► │ SFTP 2  │
│  /out   │   sftp1/out ──► sftp2/in   │  /in    │
│  /in    │ ◄───────────────────────── │  /out   │
│ /archive│     flow_b : OUT → IN      │ /archive│
└─────────┘   sftp2/out ──► sftp1/in   └─────────┘
            (daemon edi-sync au milieu)
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

- **Anti-boucle** : seuls les répertoires `OUT` sont scannés ; un fichier livré dans `IN` n'est jamais relu.
- **Stabilité** : un fichier est traité quand sa taille est inchangée sur `stability_scans` scans consécutifs ; `.tmp`, `.part` et fichiers cachés ignorés.
- **Anti-doublon** : contrainte `UNIQUE(flow, filename, size, src_mtime)` en SQLite.
- **Atomicité** : upload côté destination en `.<nom>.tmp` puis `rename` ; vérification des tailles à chaque étape.
- **Post-traitement** : `rename` du fichier source vers `archive_dir` (jamais de suppression immédiate), purge automatique après rétention.
- **Conflit de nom** côté `IN` : même taille = déjà livré (on archive quand même) ; taille différente = suffixe `.dup-<epoch>`.
- **Retry** : backoff 30 s → 1 min → 5 min → 15 min → 1 h, puis `failed` après `max_retries` tentatives. Reprise automatique des `pending` au redémarrage.

## Monitoring HTTPS

- Section `[monitoring]` : `enabled`, `bind` (défaut `127.0.0.1`), `port` (défaut `8443`).
- Certificat auto-signé généré au premier démarrage dans `<state_dir>/certs/` (ou `tls_cert`/`tls_key` fournis).
- Basic Auth optionnelle : `auth_user` + `auth_password_sha256`.
- Endpoints : `GET /` (page HTML auto-refresh 10 s : état des flux, heartbeat, compteurs, derniers événements), `GET /api/status` (JSON), `GET /health` (`200 ok`).
- Accès distant recommandé par tunnel SSH : `ssh -L 8443:127.0.0.1:8443 utilisateur@serveur-edi` puis `https://localhost:8443/`. Ne jamais binder sur `0.0.0.0`.
- États d'un flux : `ok` (heartbeat récent), `stale` (heartbeat vieux), `error` (au moins un transfert `failed`), `unknown` (pas encore de heartbeat).

## Sécurité

- Clés SSH dédiées, `chmod 600`, pas de mot de passe ; empreinte `sha256` du serveur vérifiée à chaque connexion (`RejectPolicy` + comparaison).
- Config et clés dans `/etc/edi-sync/` (root, 600/700).
- Service sandboxé : `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `ReadWritePaths=/var/lib/edi-sync /var/log/edi-sync`.
- Logs JSON sur stdout → journald (`journalctl -u edi-sync -f`).
