// Testes dos helpers de data/hora e do temporizador de tempo relativo de frontend/script.js.
// Executar a partir da raiz do projeto:  node frontend/tests/time.test.js
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const src = fs.readFileSync(path.join(__dirname, '..', 'script.js'), 'utf8');

// Extrai apenas o trecho de datas/tempo relativo (não depende do DOM)
const start = src.indexOf('const APP_TZ');
const end = src.indexOf('// Aba oculta');
assert(start > 0 && end > start, 'trechos não encontrados em script.js');
const esc = s => String(s);

const timers = { set: 0, cleared: 0, id: 0 };
const els = [];
const sandbox = {
    esc, Intl, Date, Math, Object, parseInt, RegExp,
    setInterval: () => { timers.set++; return ++timers.id; },
    clearInterval: () => { timers.cleared++; },
    document: { querySelectorAll: () => els },
};
vm.createContext(sandbox);
vm.runInContext(src.slice(start, end) + `
this.api = { APP_TZ, parseServerDate, formatDate, tzOffsetMs, isoToInputValue, inputValueToIso, timeAgo,
  relTimeHtml, refreshRelativeTimes, startRelativeTimeUpdates, stopRelativeTimeUpdates };`, sandbox);
const a = sandbox.api;

let n = 0;
const test = (name, fn) => { fn(); n++; console.log('ok -', name); };

const NOW = Date.parse('2026-10-17T15:00:00Z');
const ago = ms => new Date(NOW - ms).toISOString();

test('string sem fuso é tratada como UTC (causa da diferença de 3 h)', () => {
    assert.strictEqual(a.parseServerDate('2026-10-17T15:00:00').getTime(), NOW);
    assert.strictEqual(a.parseServerDate('2026-10-17T15:00:00Z').getTime(), NOW);
    assert.strictEqual(a.parseServerDate('2026-10-17T12:00:00-03:00').getTime(), NOW);
});

test('timeAgo: textos em português', () => {
    const t = ms => a.timeAgo(ago(ms), NOW);
    assert.strictEqual(t(5 * 1000), 'agora mesmo');
    assert.strictEqual(t(-30 * 1000), 'agora mesmo');          // relógio do servidor um pouco à frente
    assert.strictEqual(t(60 * 1000), 'há 1 minuto');
    assert.strictEqual(t(2 * 60 * 1000), 'há 2 minutos');
    assert.strictEqual(t(59 * 60 * 1000), 'há 59 minutos');
    assert.strictEqual(t(60 * 60 * 1000), 'há 1 hora');
    assert.strictEqual(t(5 * 3600 * 1000), 'há 5 horas');
    assert.strictEqual(t(86400 * 1000), 'há 1 dia');
    assert.strictEqual(t(3 * 86400 * 1000), 'há 3 dias');
    assert.strictEqual(t(30 * 86400 * 1000), '17/09/2026');
});

test('registro recém-criado (UTC sem Z do legado e com Z) não aparece com 3 h de diferença', () => {
    const legacy = new Date(NOW - 2 * 60000).toISOString().replace('Z', '');
    assert.strictEqual(a.timeAgo(legacy, NOW), 'há 2 minutos');
    assert.strictEqual(a.timeAgo(legacy + 'Z', NOW), 'há 2 minutos');
});

test('exibição em America/Sao_Paulo', () => {
    assert.strictEqual(a.formatDate('2026-10-17T15:00:00Z'), '17/10/2026, 12:00');
});

test('conversão do campo datetime-local <-> UTC', () => {
    assert.strictEqual(a.inputValueToIso('2026-10-17T12:00'), '2026-10-17T15:00:00.000Z');
    assert.strictEqual(a.isoToInputValue('2026-10-17T15:00:00Z'), '2026-10-17T12:00');
    // ida e volta
    assert.strictEqual(a.isoToInputValue(a.inputValueToIso('2030-03-05T23:45')), '2030-03-05T23:45');
    // virada de dia
    assert.strictEqual(a.inputValueToIso('2026-01-01T22:30'), '2026-01-02T01:30:00.000Z');
    assert.strictEqual(a.isoToInputValue('2026-01-02T01:30:00Z'), '2026-01-01T22:30');
    // horário de verão histórico (UTC-2 em jan/2018)
    assert.strictEqual(a.inputValueToIso('2018-01-10T12:00'), '2018-01-10T14:00:00.000Z');
});

test('refreshRelativeTimes atualiza os elementos existentes', () => {
    const el = { dataset: { ts: new Date(Date.now() - 3 * 60000).toISOString() }, textContent: 'agora mesmo' };
    els.push(el);
    a.refreshRelativeTimes();
    assert.strictEqual(el.textContent, 'há 3 minutos');
    els.length = 0;
});

test('relTimeHtml gera o span usado pelo temporizador', () => {
    const html = a.relTimeHtml(new Date().toISOString());
    assert(html.includes('class="time-ago"') && html.includes('data-ts="') && html.includes('agora mesmo'));
});

test('temporizador é único e pode ser parado', () => {
    a.startRelativeTimeUpdates(); a.startRelativeTimeUpdates(); a.startRelativeTimeUpdates();
    assert.strictEqual(timers.set, 1);
    a.stopRelativeTimeUpdates(); a.stopRelativeTimeUpdates();
    assert.strictEqual(timers.cleared, 1);
    a.startRelativeTimeUpdates();
    assert.strictEqual(timers.set, 2);
    a.stopRelativeTimeUpdates();
});

console.log(`\n${n} testes de frontend passaram`);
