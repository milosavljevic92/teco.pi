// Teco.Pi: stalno upaljen Node za YouTube JS zadatke (yt-dlp EJS rešavač).
// yt-dlp inače za svaki video pali novi Node i ponovo učitava ceo YouTube plejer (~4 MB), što na Pi 3 traje ~3,5 s.
// Ovde rešavač (lib + core) i plejer ostaju u memoriji, pa sledeći video čeka ~0,1-1 s.
// Protokol: jedan JSON po liniji. Ulaz {id, key, head?, pkey, player?, data} -> izlaz {id, out} ili {id, err}.
const vm = require('vm');
const rl = require('readline').createInterface({ input: process.stdin, crlfDelay: Infinity });
const heads = new Map(), players = new Map();   // key -> vm kontekst sa rešavačem, pkey -> plejer
const keep = (map, k, v) => { map.set(k, v); while (map.size > 2) map.delete(map.keys().next().value); };
const quiet = { log() {}, error() {}, warn() {}, info() {}, debug() {} };

rl.on('line', line => {
  let m;
  try { m = JSON.parse(line); } catch (e) { return; }
  let out;
  try {
    if (m.head != null) {
      const ctx = vm.createContext({ console: quiet });
      vm.runInContext(m.head, ctx);
      keep(heads, m.key, ctx);
    }
    if (m.player != null) keep(players, m.pkey, m.player);
    const ctx = heads.get(m.key);
    if (!ctx) throw new Error('need_head');
    const p = players.get(m.pkey);
    if (p == null) throw new Error('need_player');
    const d = m.data;
    d[d.type === 'preprocessed' ? 'preprocessed_player' : 'player'] = p;
    ctx.__d = d;
    out = { id: m.id, out: vm.runInContext('JSON.stringify(jsc(__d))', ctx) };
    ctx.__d = null;
  } catch (e) {
    out = { id: m.id, err: String((e && e.message) || e) };
  }
  process.stdout.write(JSON.stringify(out) + '\n');
});
rl.on('close', () => process.exit(0));
