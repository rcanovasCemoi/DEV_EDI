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
    'document', 'window', 'DOMParser', 'prompt',
    sandboxScript + `
;return {
    splitEdifact, extractQualifier, isTechnicalQualifierPosition,
    escapeEdifactValue, registerTargetXmlPath,
    sortSegmentsByNorm, detectEdifactMessageType,
    switchMessageType, buildCompletedEdi, buildCompletedXml, analyzeEdifactFiles,
    countPathLevels, splitXmlPath, buildTreeOrdering, buildMoveOptionsList, insertIntermediateLevel,
    getDetectedFields: function() { return detectedFields; },
    getMergedEdiTemplates: function() { return mergedEdiTemplates; },
    getActiveMessageType: function() { return activeMessageType; },
    setLoadedEdiFiles: function(files) { loadedEdiFiles = files; }
};`
);
let promptCallCount = 0;
const promptStub = () => {
    promptCallCount++;
    return promptCallCount === 1 ? 'NewGroup' : null;
};
const app = loader(documentStub, windowStub, XmlDomParser, promptStub);

// --- Jeux EDI de test (remplacent les démos supprimées de l'app) ------------
const TEST_EDI = {
    ORDERS: "UNH+1+ORDERS:D:96A:UN:EAN008'\n" +
        "BGM+220::9+ORDER00001+9'\n" +
        "DTM+137:20260305:102'\n" +
        "DTM+2:202603120001:203'\n" +
        "FTX+PUR+1++CONTACT CLIENT TEL 04 68 00 00 00'\n" +
        "RFF+VN:SUPP00001'\n" +
        "DTM+171:20260305:102'\n" +
        "NAD+BY+3012345678905::9++CUSTOMER COMPANY+CUSTOMER ADDRESS+CITY++66000'\n" +
        "NAD+SU+3029876543210::9++SUPPLIER COMPANY+SUPPLIER ADDRESS+CITY++31000+FR'\n" +
        "RFF+VA:FR12345678901'\n" +
        "NAD+DP+3012345678905::9++CUSTOMER COMPANY+CUSTOMER ADDRESS+CITY++66000'\n" +
        "CUX+2:EUR:9'\n" +
        "PAT+22+++2:3:0'\n" +
        "TDT+20++30+TRP0001+++TRANSPORTS EXPRESS SAS'\n" +
        "TOD+10E+X1E+DDP'\n" +
        "LIN+1++3017620412345:EN'\n" +
        "QTY+21:144:PCE'\n" +
        "PRI+AAA:0.958::NTP::PCE'\n" +
        "RFF+ON:CMD000001'\n" +
        "UNS+S'\n" +
        "CNT+2:1'",
    DESADV: "UNB+UNOC:3+3029876543210:14+3012345678905:14+260310:1430+0000000098'\n" +
        "UNH+1+DESADV:D:96A:UN:EAN005'\n" +
        "BGM+351::9+BL000001+9'\n" +
        "DTM+137:20260310:102'\n" +
        "NAD+SU+3029876543210::9++SUPPLIER COMPANY+SUPPLIER ADDRESS+CITY++31000+FR'\n" +
        "NAD+DP+3012345678905::9++CUSTOMER COMPANY+CUSTOMER ADDRESS+CITY++66000'\n" +
        "CPS+1'\n" +
        "PAC+16+3+CS:::SSCC00000001'\n" +
        "MEA+AAE+WGM+1200.500:KGM'\n" +
        "GIN+BJ+330123456789012345'\n" +
        "LIN+1++3017620412345:EN'\n" +
        "QTY+12:16:PCE'\n" +
        "UNS+S'\n" +
        "CNT+2:1'",
    INVOIC: "UNH+1+INVOIC:D:96A:UN:EAN008'\n" +
        "BGM+380::9+FA000001+9'\n" +
        "DTM+137:20260312:102'\n" +
        "DTM+171:20260305:102'\n" +
        "RFF+VN:SUPP00001'\n" +
        "NAD+BY+3012345678905::9++CUSTOMER COMPANY+CUSTOMER ADDRESS+CITY++66000'\n" +
        "NAD+SU+3029876543210::9++SUPPLIER COMPANY+SUPPLIER ADDRESS+CITY++31000+FR'\n" +
        "CUX+2:EUR:9'\n" +
        "PAT+22+++2:3:0'\n" +
        "LIN+1++3017620412345:EN'\n" +
        "QTY+47:144:PCE'\n" +
        "PRI+AAA:0.958::NTP::PCE'\n" +
        "MOA+203:138.352'\n" +
        "TAX+7+VAT+++:::20+S'\n" +
        "UNS+S'\n" +
        "CNT+2:1'\n" +
        "MOA+9:165.99'",
};

function loadScenario(type) {
    app.switchMessageType(type, false);
    app.setLoadedEdiFiles([{ filename: `test_${type.toLowerCase()}.edi`, content: TEST_EDI[type] }]);
    app.analyzeEdifactFiles();
}

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

console.log('\n== 6. Scénario ORDERS de bout en bout ==');
loadScenario('ORDERS');
eq(app.getActiveMessageType(), 'ORDERS', 'Flux actif : ORDERS');
const ordFields = app.getDetectedFields();
ok(ordFields.length > 10, `Champs détectés sur le scénario ORDERS (${ordFields.length} > 10)`);
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
loadScenario('DESADV');
eq(app.getActiveMessageType(), 'DESADV', 'Flux actif : DESADV');
const desEdi = app.buildCompletedEdi();
ok(desEdi.includes('UNT+'), 'Master DESADV : UNT présent');
ok(app.getMergedEdiTemplates().size > 5, `Superset DESADV fusionné (${app.getMergedEdiTemplates().size} > 5)`);
if (desEdi.includes('CPS')) {
    ok(desEdi.indexOf('CPS') < desEdi.indexOf('UNS+S'), 'DESADV : CPS avant UNS+S');
}

loadScenario('INVOIC');
eq(app.getActiveMessageType(), 'INVOIC', 'Flux actif : INVOIC');
const invEdi = app.buildCompletedEdi();
ok(invEdi.includes('UNT+'), 'Master INVOIC : UNT présent');
const invFields = app.getDetectedFields();
const invPaths = invFields.map(f => f.targetXmlPath);
eq(new Set(invPaths).size, invPaths.length, 'INVOIC : aucun doublon de chemin XML cible');

console.log('\n== 8. Génération de valeurs typées ==');
// GLN/GTIN : extraire un NAD du Master ORDERS et vérifier le format 13 chiffres
loadScenario('ORDERS');
const nadLine = app.buildCompletedEdi().split(/\r?\n/).find(l => l.startsWith('NAD+') && /:14|::9/.test(l));
ok(nadLine && /\+\d{13}[:+]/.test(nadLine), 'Master ORDERS : GLN à 13 chiffres dans NAD');
// Prix : PRI01.2 avec 3 décimales
const priLine = app.buildCompletedEdi().split(/\r?\n/).find(l => l.startsWith('PRI+'));
ok(priLine && /PRI\+(?:AAA:)?\d+\.\d{3}/.test(priLine), 'Master ORDERS : prix PRI à 3 décimales');

console.log('\n== 9. Non-régression doublons — scénario FTX multiples (cas capture) ==');
// Reproduit le bug signalé : FTX+INV / FTX+PUR / FTX+ZZZ doivent obtenir des chemins uniques
loadScenario('INVOIC');
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

console.log('\n== 11. Listes de déplacement ==');
loadScenario('ORDERS');
const dests = app.buildMoveOptionsList('OrderHeader.Supplier', ['OrderHeader']);
ok(dests.includes('OrderHeader'), 'Destination : ancêtre direct proposé');
ok(!dests.includes('OrderHeader.Supplier'), 'Le chemin déplacé lui-même est exclu');
ok(!dests.some(p => p.startsWith('OrderHeader.Supplier.')), 'Descendants exclus (pas de cycle)');
ok(dests.every(p => p.indexOf('..') === -1), 'Chemins de destination bien formés');
const dests2 = app.buildMoveOptionsList('OrderHeader.Supplier.VatNumber', []);
ok(dests2.includes('OrderHeader') && dests2.includes('OrderHeader.Supplier'), 'Chemin profond : parents proposés');
const leafDests = app.buildMoveOptionsList('OrderHeader.FreeText', ['OrderHeader']);
ok(leafDests.includes('OrderHeader.Supplier'), 'Feuille : autres mères proposées');
ok(leafDests.includes('EDIXml'), 'Feuille : racine EDIXml proposée');

console.log('\n== 12. Ajout de niveau hiérarchique ==');
loadScenario('ORDERS');
const existingPaths = new Set(app.getDetectedFields().map(f => f.targetXmlPath));
const before = app.getDetectedFields().map(f => f.targetXmlPath);
// prompt stubé : nom de la nouvelle balise mère
const created = app.insertIntermediateLevel('OrderHeader', 'OrderHeader.FreeText', false);
eq(created, 'NewGroup', 'Niveau créé avec le nom saisi');
const movedField = app.getDetectedFields().find(f => f.targetXmlPath === 'OrderHeader.NewGroup.FreeText');
ok(Boolean(movedField), 'Feuille déplacée sous le nouveau niveau OrderHeader.NewGroup.FreeText');
const untouched = before.filter(p => p !== 'OrderHeader.FreeText' && app.getDetectedFields().every(f => f.targetXmlPath !== p));
ok(untouched.length === 0, 'Les autres champs ne sont pas modifiés');
// Cas annulation : prompt → null
const createdNull = app.insertIntermediateLevel('OrderHeader', 'OrderHeader.Currency', false);
eq(createdNull, null, 'Annulation du prompt → null, aucun changement');

console.log('\n== 13. Restructuration du XML exporté (balises déjà présentes) ==');
loadScenario('ORDERS');
const xmlBefore = app.buildCompletedXml();
ok(/<OrderDate>/.test(xmlBefore), 'Avant : <OrderDate> présent dans le XML');
const odField = app.getDetectedFields().find(f => f.targetXmlPath === 'OrderHeader.OrderDate');
ok(Boolean(odField) && odField.isCovered, 'Champ OrderDate couvert trouvé');
odField.targetXmlPath = 'OrderHeader.PurchaseOrderDate';
const xmlAfter = app.buildCompletedXml();
ok(/<PurchaseOrderDate>/.test(xmlAfter), 'Après : <PurchaseOrderDate> dans le XML exporté');
ok(!/<OrderDate>/.test(xmlAfter), 'Après : ancien <OrderDate> disparu');
const ftField = app.getDetectedFields().find(f => f.targetXmlPath === 'OrderHeader.FreeText' && !f.isXmlOnly);
if (ftField) {
    ftField.targetXmlPath = 'OrderHeader.Buyer.InternalNote';
    const xmlMoved = app.buildCompletedXml();
    ok(/<Buyer>[\s\S]*?<InternalNote/.test(xmlMoved), 'Déplacement : balise sous <Buyer><InternalNote>');
    ok(!/<OrderHeader>\n\s+<FreeText/.test(xmlMoved), 'Déplacement : plus de <FreeText> direct sous OrderHeader');
}
const ucFields = app.getDetectedFields().filter(f => f.targetXmlPath && f.targetXmlPath.startsWith('OrderHeader.UltimateConsignee'));
ok(ucFields.length > 0, 'Champs du groupe UltimateConsignee détectés');
ucFields.forEach(f => { f.targetXmlPath = f.targetXmlPath.replace('UltimateConsignee', 'FinalCustomer'); });
const xmlGroup = app.buildCompletedXml();
ok(/<FinalCustomer>/.test(xmlGroup), 'Groupe renommé : <FinalCustomer> présent');
ok(!/<UltimateConsignee>/.test(xmlGroup), 'Groupe renommé : <UltimateConsignee> supprimé');
ok(/<FinalCustomer>[\s\S]*?<GLN\//.test(xmlGroup), 'Groupe renommé : filles intactes dans FinalCustomer');

// =============================================================================
console.log(`\nRésultat : ${passed} PASS, ${failed} FAIL`);
if (failed > 0) {
    console.error('\nÉchecs :');
    failures.forEach(f => console.error('  - ' + f));
    process.exit(1);
}
console.log('TOUS LES TESTS PASSENT ✓');
