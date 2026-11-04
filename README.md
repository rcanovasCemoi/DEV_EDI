# DEV_EDI — Projets EDI

Ce dépôt regroupe plusieurs projets EDI indépendants, chacun dans son propre sous-dossier.

| Projet | Description |
|---|---|
| [`EDIFACT_BABELWAY/`](EDIFACT_BABELWAY/) | Analyseur EDIFACT & générateur de fichiers de référence (.xml & .edi) — SPA autonome + tests de non-régression en CI |
| [`SRV_EDI_TRANSFERT_SFTP/`](SRV_EDI_TRANSFERT_SFTP/) | Daemon Linux (service systemd) de transfert SFTP en temps réel et bidirectionnel de fichiers EDI entre deux serveurs SFTP |

## CI

Chaque projet a sa propre suite de tests déclenchée par GitHub Actions uniquement lorsque ses fichiers changent (filtre `paths`).

- [`tests.yml`](.github/workflows/tests.yml) : tests de non-régression EDIFACT_BABELWAY (Node.js, 74 assertions)
