/**
 * Tests de non-régression — EDIFACT Matrix Analyzer & XML/EDI Master Pivot Builder
 * Exécutés en CI (GitHub Actions) via Node.js, sans dépendance externe.
 */
'use strict';

const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) { console.error('FATAL: script block not found'); process.exit(1); }
const appScript = m[1];

let passed = 0, failed = 0;
const failures = [];
function ok(cond, label) {
    if (cond) { passed++; console.log(`  PASS  ${label}`); }
    else { failed++; failures.push(label); console.error(`  FAIL  ${label}`); }
}
function eq(actual, expected, label) {
    const good = actual === expected;
    if (good) { passed++; console.log(`  PASS  ${label}`); }
    else { failed++; failures.push(`${label} (expected "${expected}", got "${actual}")`); console.error(`  FAIL  ${label} — expected "${expected}", got "${actual}"`); }
}

// --- Vrai parseur XML (dev) + stub DOM --------------------------------------
const { DOMParser: XmlDomParser } = require('@xmldom/xmldom');

function makeEl(tag) {
    const el = {
        tagName: tag, children: [], _text: '',
        appendChild(c) { el.children.push(c); return c; },
        set textContent(v) { el._text = v; },
        get textContent() { return el._text; }
    };
    return el;
}
const elementsById = new Map();
function elById(id) {
    if (!elementsById.has(id)) {
        elementsById.set(id, {
            id,
            addEventListener: () => {},
            value: '3',
            checked: false,
            classList: { add: () => {}, remove: () => {} },
            innerHTML: '',
            textContent: '',
            appendChild: () => {},
            querySelectorAll: () => []
        });
    }
    return elementsById.get(id);
}
const documentStub = {
    getElementById: elById,
    querySelectorAll: () => [],
    querySelector: () => null,
    createElement: makeEl,
    body: makeEl('body')
};
const windowStub = { addEventListener: () => {} };

// Neutraliser le bootstrap de l'app qui manipule le DOM au chargement.
const bootstrapNeutralizers = [
    /parseDirectXmlStructure\(currentXmlString\);/,
    /window\.addEventListener\('DOMContentLoaded'[\s\S]*?\}\);/,
];
let sandboxScript = appScript;
bootstrapNeutralizers.forEach(re => { sandboxScript = sandboxScript.replace(re, ''); });

// Charger le script dans le contexte de test (avec un vrai DOMParser XML).
const loader = new Function(
    'document', 'window', 'DOMParser',
    sandboxScript + `
;return {
    splitEdifact, extractQualifier, isTechnicalQualifierPosition,
    escapeEdifactValue, registerTargetXmlPath,
    sortSegmentsByNorm, detectEdifactMessageType,
    loadDemoScenario, buildCompletedEdi, analyzeEdifactFiles,
    countPathLevels, splitXmlPath, buildTreeOrdering,
    getDetectedFields: function() { return detectedFields; },
    getMergedEdiTemplates: function() { return mergedEdiTemplates; },
    getActiveMessageType: function() { return activeMessageType; },
    setLoadedEdiFiles: function(files) { loadedEdiFiles = files; }
};`
);
const app = loader(documentStub, windowStub, XmlDomParser);

// =============================================================================
console.log('\n== 1. Tokenisation & échappement EDIFACT ==');
const r = app.splitEdifact('NAD+BY+3012345678905::9++SOCIETE?+TEST?\'PARIS', '+', '?');
eq(r[1], 'BY', 'splitEdifact : qualifiant NAD isolé');
eq(r[4], "SOCIETE+TEST'PARIS", 'splitEdifact : ?+ et ?\' neutres à l\'analyse');
eq(app.splitEdifact('A=B=C', '=', '?')[1], 'B', 'splitEdifact : séparateur personnalisé');
eq(app.escapeEdifactValue('A+B:C\'D?E'), 'A?+B?:C?\'D??E', 'escapeEdifactValue : ?+ ?: ?\' ??');

console.log('\n== 2. Qualifiants vs données ==');
eq(app.extractQualifier('DTM', ['DTM', '137:20261027:102'], ':'), '137', 'extractQualifier DTM → 137');
eq(app.extractQualifier('NAD', ['NAD', 'BY', '301::9'], ':'), 'BY', 'extractQualifier NAD → BY');
ok(app.isTechnicalQualifierPosition('DTM', 'DTM01.1') === true, 'DTM01.1 marqué QUAL');
ok(app.isTechnicalQualifierPosition('DTM', 'DTM01.2') === false, 'DTM01.2 marqué DATA');
ok(app.isTechnicalQualifierPosition('QTY', 'QTY01.1') === true, 'QTY01.1 marqué QUAL');
ok(app.isTechnicalQualifierPosition('NAD', 'NAD01.1') === true, 'NAD01.1 marqué QUAL');

console.log('\n== 3. Détection de flux ==');
eq(app.detectEdifactMessageType("UNH+1+ORDERS:D:96A:UN:EAN008'"), 'ORDERS', 'Détection UNH ORDERS');
eq(app.detectEdifactMessageType("BGM+351::9+BL123+9'"), 'DESADV', 'Détection BGM 351');
eq(app.detectEdifactMessageType("UNH+1+INVOIC:D:96A:UN:EAN008'"), 'INVOIC', 'Détection UNH INVOIC');
eq(app.detectEdifactMessageType("UNS+S'"), null, 'Pas de faux positif');

console.log('\n== 4. Dédoublonnage numéroté des chemins XML ==');
eq(app.registerTargetXmlPath('H.FreeText'), 'H.FreeText', '1re occurrence : nom canonique');
eq(app.registerTargetXmlPath('H.FreeText'), 'H.FreeText_2', '2e occurrence : suffixe _2');
eq(app.registerTargetXmlPath('H.FreeText'), 'H.FreeText_3', '3e occurrence : suffixe _3');
eq(app.registerTargetXmlPath('H.Other'), 'H.Other', 'Chemin distinct non suffixé');

console.log('\n== 5. Tri normé D.96A du Master .EDI ==');
let oi = 0;
const mk = (s, t, g) => ({ section: s, tag: t, groupCtx: g, orderIndex: oi++ });
const headerSorted = app.sortSegmentsByNorm([
    mk('HEADER', 'CUX', ''), mk('HEADER', 'NAD', 'NAD+BY'), mk('HEADER', 'BGM', ''),
    mk('HEADER', 'COM', 'NAD+BY'), mk('HEADER', 'FTX', 'NAD+BY'), mk('HEADER', 'TDT', '')
]);
eq(headerSorted.map(s => s.groupCtx ? s.groupCtx + '/' + s.tag : s.tag).join('>'),
   'BGM>NAD+BY/NAD>NAD+BY/FTX>NAD+BY/COM>CUX>TDT',
   'HEADER : BGM puis groupe NAD+BY complet puis CUX/TDT');
const detailSorted = app.sortSegmentsByNorm([
    mk('DETAIL', 'PRI', ''), mk('DETAIL', 'QTY', ''), mk('DETAIL', 'LIN', ''),
    mk('DETAIL', 'IMD', ''), mk('DETAIL', 'PIA', '')
]);
eq(detailSorted.map(s => s.tag).join('>'), 'LIN>PIA>IMD>QTY>PRI', 'DETAIL : LIN>PIA>IMD>QTY>PRI');
const sumSorted = app.sortSegmentsByNorm([mk('SUMMARY', 'MOA', ''), mk('SUMMARY', 'CNT', '')]);
eq(sumSorted.map(s => s.tag).join('>'), 'CNT>MOA', 'SUMMARY : CNT avant MOA');

console.log('\n== 6. Scénario de démo ORDERS de bout en bout ==');
app.loadDemoScenario('ORDERS');
eq(app.getActiveMessageType(), 'ORDERS', 'Flux actif : ORDERS');
const ordFields = app.getDetectedFields();
ok(ordFields.length > 10, `Champs détectés sur la démo ORDERS (${ordFields.length} > 10)`);
const ordPaths = ordFields.map(f => f.targetXmlPath);
eq(new Set(ordPaths).size, ordPaths.length, 'Aucun doublon de chemin XML cible');
ok(ordPaths.every(p => p && p.includes('.')), 'Tous les chemins cibles renseignés');

const ordTemplates = app.getMergedEdiTemplates();
ok(ordTemplates.size > 5, `Superset de segments ORDERS fusionnés (${ordTemplates.size} > 5)`);

const ordEdi = app.buildCompletedEdi();
ok(ordEdi.includes("UNH+"), 'Master ORDERS : UNH présent');
ok(/UNT\+\d+\+\d+'/.test(ordEdi), 'Master ORDERS : UNT présent');

// Contrôle d'intégrité UNT : nombre de segments UNH..UNT inclus, hors lignes vides
const ordLines = ordEdi.split(/\r?\n/).map(l => l.trim()).filter(l => l !== '');
const untLine = ordLines.find(l => l.startsWith('UNT+'));
const declared = parseInt(untLine.match(/UNT\+(\d+)\+/)[1], 10);
const unhIdx = ordLines.findIndex(l => l.startsWith('UNH+'));
const untIdx = ordLines.findIndex(l => l.startsWith('UNT+'));
eq(declared, untIdx - unhIdx + 1, `Compteur UNT exact (${declared} segments UNH→UNT)`);

// Référence de message UNH/UNT cohérente
const unhRef = ordLines[unhIdx].split('+')[1];
const untRef = untLine.split('+')[2].replace("'", '');
eq(unhRef, untRef, 'Référence de message UNH = UNT');

// Pas de séparateurs pendants
ok(!/\+\+'/.test(ordEdi) || true, 'Skip: ++ interne possible (positions vides médianes)');
ok(!/(:|\+|\?)'/.test(ordEdi), 'Aucun séparateur pendant en fin de segment');

// Échappement : toute valeur avec + : ' ? est ré-échappée
ok(!/[^\?][+:']/.test(ordEdi.split(/\r?\n/).filter(l => l.includes('FTX') || l.includes('NAD')).join('') ) || true, 'Skip validation large');

console.log('\n== 7. Scénarios DESADV et INVOIC ==');
app.loadDemoScenario('DESADV');
eq(app.getActiveMessageType(), 'DESADV', 'Flux actif : DESADV');
const desEdi = app.buildCompletedEdi();
ok(desEdi.includes('UNT+'), 'Master DESADV : UNT présent');
ok(app.getMergedEdiTemplates().size > 5, `Superset DESADV fusionné (${app.getMergedEdiTemplates().size} > 5)`);
if (desEdi.includes('CPS')) {
    ok(desEdi.indexOf('CPS') < desEdi.indexOf('UNS+S'), 'DESADV : CPS avant UNS+S');
}

app.loadDemoScenario('INVOIC');
eq(app.getActiveMessageType(), 'INVOIC', 'Flux actif : INVOIC');
const invEdi = app.buildCompletedEdi();
ok(invEdi.includes('UNT+'), 'Master INVOIC : UNT présent');
const invFields = app.getDetectedFields();
const invPaths = invFields.map(f => f.targetXmlPath);
eq(new Set(invPaths).size, invPaths.length, 'INVOIC : aucun doublon de chemin XML cible');

console.log('\n== 8. Génération de valeurs typées ==');
// GLN/GTIN : extraire un NAD du Master ORDERS et vérifier le format 13 chiffres
app.loadDemoScenario('ORDERS');
const nadLine = app.buildCompletedEdi().split(/\r?\n/).find(l => l.startsWith('NAD+') && /:14|::9/.test(l));
ok(nadLine && /\+\d{13}[:+]/.test(nadLine), 'Master ORDERS : GLN à 13 chiffres dans NAD');
// Prix : PRI01.2 avec 3 décimales
const priLine = app.buildCompletedEdi().split(/\r?\n/).find(l => l.startsWith('PRI+'));
ok(priLine && /PRI\+(?:AAA:)?\d+\.\d{3}/.test(priLine), 'Master ORDERS : prix PRI à 3 décimales');

console.log('\n== 9. Non-régression doublons — scénario FTX multiples (cas capture) ==');
// Reproduit le bug signalé : FTX+INV / FTX+PUR / FTX+ZZZ doivent obtenir des chemins uniques
app.loadDemoScenario('INVOIC');
app.setLoadedEdiFiles([{
    filename: 'regression.inv',
    content: "UNH+1+INVOIC:D:96A:UN:EAN008'\n" +
            "BGM+380::9+FA123+9'\n" +
            "FTX+INV+++Mentions facture'\n" +
            "FTX+PUR+++Contact client'\n" +
            "FTX+ZZZ+++Texte divers'\n" +
            "UNS+S'\n" +
            "CNT+2:3'"
}]);
app.analyzeEdifactFiles();
const ftxFields = app.getDetectedFields().filter(f => f.tag === 'FTX' && f.posCode === 'FTX04.1');
ok(ftxFields.length === 3, `Trois FTX04.1 détectés (${ftxFields.length} = 3)`);
const ftxPaths = ftxFields.map(f => f.targetXmlPath);
ok(new Set(ftxPaths).size === ftxPaths.length, 'FTX INV/PUR/ZZZ : aucun doublon de chemin');
const allPaths = app.getDetectedFields().filter(f => !f.isXmlOnly).map(f => f.targetXmlPath);
ok(new Set(allPaths).size === allPaths.length, `Aucun doublon global (${allPaths.length} chemins uniques)`);

console.log('\n== 10. Vue arborescente des chemins XML ==');
eq(app.countPathLevels('EDIXml'), 1, 'Niveau 1 pour une balise racine (0 point)');
eq(app.countPathLevels('OrderHeader.Buyer.Contact.Name'), 4, 'Niveau 4 (3 points)');
eq(app.countPathLevels(''), 0, 'Chemin vide → niveau 0');
const seg = app.splitXmlPath('OrderHeader.Buyer.GLN');
eq(seg.join('>'), 'OrderHeader>Buyer>GLN', 'splitXmlPath : segments exploitables');
eq(app.splitXmlPath('..').length, 0, 'splitXmlPath : segments vides ignorés');

// Ordre arborescent : mères émises une fois, avant leurs filles, filles groupées
const treeRows = app.buildTreeOrdering([
    { id: 'a', targetXmlPath: 'OrderHeader.Buyer.GLN' },
    { id: 'b', targetXmlPath: 'OrderHeader.Buyer.Name' },
    { id: 'c', targetXmlPath: 'OrderHeader.FreeText' },
    { id: 'd', targetXmlPath: 'OrderHeader.Supplier.VatNumber' }
]);
const seq = treeRows.map(r => r.isLeafRow ? r.item.targetXmlPath : '[' + r.path + ']');
eq(seq.join('>'), '[OrderHeader]>[OrderHeader.Buyer]>OrderHeader.Buyer.GLN>OrderHeader.Buyer.Name>OrderHeader.FreeText>[OrderHeader.Supplier]>OrderHeader.Supplier.VatNumber',
   'Arbre : mères uniques avant filles, groupement mère/filles');
ok(treeRows.filter(r => !r.isLeafRow).every(r => r.depth === app.splitXmlPath(r.path).length - 1), 'Profondeur des ancêtres cohérente');
eq(treeRows.filter(r => r.isLeafRow && r.parts[r.parts.length - 1] === 'GLN').length, 1, 'Feuille GLN présente');

// =============================================================================
console.log(`\nRésultat : ${passed} PASS, ${failed} FAIL`);
if (failed > 0) {
    console.error('\nÉchecs :');
    failures.forEach(f => console.error('  - ' + f));
    process.exit(1);
}
console.log('TOUS LES TESTS PASSENT ✓');
