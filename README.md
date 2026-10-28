# EDIFACT Matrix Analyzer & XML/EDI Master Pivot Builder

Outil autonome d'intégration EDI (Supply Chain) exécutable localement dans un navigateur, sans serveur, sans dépendance externe ni CDN.

## Objectif

Analyser des lots de fichiers EDIFACT réels provenant de clients multiples pour :

1. **Compléter un modèle XML Pivot canonique** (`<EDIXml>`) en identifiant et injectant toutes les balises manquantes.
2. **Générer un fichier .EDI de référence (Master EDIFACT)** contenant le sur-ensemble (superset) de tous les segments, éléments et sous-éléments détectés, enrichi de valeurs aléatoires réalistes conformes à la norme.
3. **Produire une matrice de mapping CSV** prête pour l'analyse d'intégration.

Flux pris en charge : **ORDERS** (D.96A / EAN008), **DESADV** (D.96A / EAN005), **INVOIC** (D.96A / EAN008).

## Utilisation

1. Ouvrir `index.html` directement dans un navigateur (double-clic, aucune installation requise, fonctionne hors-ligne).
2. Choisir le flux EDIFACT (ORDERS / DESADV / INVOIC) ou charger un fichier XML Pivot de référence (`.xml`).
3. Glisser-déposer les fichiers EDIFACT d'entrée (`.txt`, `.edi`) — analyse multi-fichiers.
4. Consulter le tableau de Gap Analysis, cocher les balises manquantes à ajouter (chemin XML cible éditable).
5. Exporter :
   - **XML de Référence complété** (`.xml`) — balises manquantes insérées au bon endroit (`OrderHeader`, sous-bloc partenaire, ou dans **tous** les blocs `<OrderItem>`), indentation 3 espaces, balises vides auto-fermantes `<Balise/>`.
   - **Fichier .EDI Master complété** (`.edi`) — superset fusionné, valeurs aléatoires typées (GLN/GTIN 13 chiffres, dates `CCYYMMDD[HHMM]`, prix 3 décimales), régénération à la volée, nombre de lignes `LIN` réglable (1 à 20), compteur `UNT` recalculé.
   - **Matrice de Mapping** (`.csv`, BOM UTF-8 pour Excel).

Des jeux de démonstration (`ORDERS`, `DESADV`, `INVOIC`) sont intégrés pour test immédiat.

## Moteur EDIFACT

- **Tokenisation** : segment optionnel `UNA` pris en compte (séparateurs personnalisés), gestion stricte du caractère d'échappement `?` (`?+`, `?:`, `?'` ne coupent pas les champs), ré-échappement systématique à la génération du Master `.edi`.
- **Auto-détection** du flux via `UNH` ou `BGM` (220/ORDERS, 351/DESADV, 380/INVOIC).
- **Machine à états de contexte** : portées `ENVELOPE` (UNB), `HEADER`, `PACKAGING` (CPS/PAC), `DETAIL` (LIN), `SUMMARY` (UNS+S / CNT). Contexte de groupe partenaire `NAD+<QUAL>` (BY, SU, DP, IV, UC) avec rattachement des `RFF`, `FTX`, `CTA`, `COM` ; fermé par `TDT`, `CUX`, `TOD`/`PAT` ou une ligne.
- **Filtrage QUAL vs DATA** : les qualifiants structurels (`DTM01.1 = 137`, `QTY01.1 = 21`, `PRI01.1 = AAA`, `NAD01.1 = BY`, …) sont maintenus stricts dans le Master EDI et ne créent pas de balises XML parasites.
- **Fusion superset** : union des segments par signature (`Section | Contexte | Tag | Qualifiant`) et combinaison des sous-éléments à toutes positions détectées (ex. `PAC+X+3+CS::9:SSCC01'` si les positions 1, 2, 3.1, 3.3, 3.4 existent dans le lot).
- **Contrôle d'intégrité** : recalcul dynamique de `UNT` (segments de `UNH` à `UNT` inclus, hors lignes vides), référence de message cohérente `UNH`/`UNT`, nettoyage des séparateurs résiduels en fin de segment.

## Sécurité

- Traitement 100 % client (API `FileReader`, `DOMParser`, DOM natif) — aucun envoi réseau.
- Échappement systématique des chaînes rendues dans l'interface (prévention XSS).

## Fichiers

| Fichier | Description |
|---|---|
| `index.html` | Application autonome (HTML5/CSS3/JS vanilla, fichier unique) |
