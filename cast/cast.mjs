// Teco.Pi cast: YouTube aplikacija i Chrome vide Teco.Pi kao „YouTube na televizoru“ (DIAL).
// Sve komande idu na Teco.Pi server (/api/cast), koji pušta video kroz isti mpv, listu i istoriju.
import YouTubeCastReceiver, { Player, Constants } from 'yt-cast-receiver';
import dial from '@patrickkfkan/peer-dial';
import { randomUUID } from 'crypto';
import fs from 'fs';

const API = process.env.TECO_API || 'http://127.0.0.1/api/cast';
const S = Constants.PLAYER_STATUSES;

// Trajno čuvanje (umesto ugrađenog node-persist, koji puca na ovom Node-u): uparivanje sa telefonom (app.pid,
// mdx kontekst) i ID uređaja ostaju isti posle restarta, pa telefon ne dobija „zastarelu“ vezu koja se sama prekine.
const STORE = new URL('../data/cast-store.json', import.meta.url);
class FileStore {
  constructor() { try { this.d = JSON.parse(fs.readFileSync(STORE, 'utf8')); } catch (e) { this.d = {}; } }
  setLogger(l) { this.logger = l; }
  async get(k) { return this.d[k] ?? null; }
  async set(k, v) { this.d[k] = v; try { fs.writeFileSync(STORE, JSON.stringify(this.d)); } catch (e) { console.error('čuvanje:', e.message); } }
}
const store = new FileStore();
if (!store.d.uuid) await store.set('uuid', randomUUID());
// DIAL ID uređaja je inače nasumičan pri svakom startu (biblioteka ga ne prosleđuje), pa ga ovde zadajemo stalan
const DialServer = dial.Server;
dial.Server = function (opts) { return new DialServer({ ...opts, uuid: store.d.uuid }); };

async function call(a, extra = {}) {
  try {
    const r = await fetch(API, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ a, ...extra }) });
    return await r.json();
  } catch (e) {
    return {};
  }
}

class TecoPlayer extends Player {
  constructor() {
    super();
    this.vid = null;      // video koji je poslat sa telefona i trenutno se pušta
    this.timer = null;
    this.lastPaused = null;
  }

  async doPlay(video, position) {
    this.vid = video.id;
    console.log(stamp(), 'pusti:', video.id);
    // Priprema videa traje ~3 s; telefon ne sme toliko da čeka odgovor (inače prekine cast), pa odgovaramo
    // najkasnije za 1,5 s, a Teco.Pi nastavlja da učitava (status tada javlja „loading“).
    const p = call('play', { id: video.id, pos: position || 0 });
    const r = await Promise.race([p, new Promise(res => setTimeout(() => res({ ok: true, early: true }), 1500))]);
    this.lastPaused = false;
    this.watch();
    return !!r.ok;
  }
  async doPause() { console.log(stamp(), 'pauza sa telefona'); return !!(await call('pause')).ok; }
  async doResume() { return !!(await call('resume')).ok; }
  async doStop() {
    this.vid = null; clearInterval(this.timer);
    // Kad se telefon odvoji (npr. YouTube aplikacija spuštena na iPhone-u), biblioteka resetuje plejer i zove stop.
    // Tada video na Teco.Pi nastavlja; pauzira se samo kad telefon koji je još povezan pošalje stop.
    if (!receiver.getConnectedSenders().length) { console.log(stamp(), 'stop posle odvajanja telefona: video nastavlja'); return true; }
    return !!(await call('stop')).ok;
  }
  async doSeek(position) { return !!(await call('seek', { pos: position })).ok; }
  async doSetVolume(volume) { return !!(await call('volume', { level: volume.level, muted: volume.muted })).ok; }
  async doGetVolume() { const s = await call('status'); return { level: s.vol ?? 70, muted: !!s.mute }; }
  async doGetPosition() { return (await call('status')).pos ?? 0; }
  async doGetDuration() { return (await call('status')).dur ?? 0; }

  // Prati šta se dešava na Teco.Pi: kraj videa (telefon šalje sledeći) i pauza sa web stranice ili daljinskog.
  watch() {
    clearInterval(this.timer);
    let busy = false;
    this.timer = setInterval(async () => {
      if (!this.vid || busy) return;
      const s = await call('status');
      if (!s || s.loading) return;
      busy = true;
      try {
        if (s.vid !== this.vid) {
          const ended = !s.vid;
          this.vid = null;
          clearInterval(this.timer);
          if (ended) { await this.pause(); await this.next(); }          // video je završen: sledeći sa telefona
          else await this.notifyExternalStateChange(S.STOPPED);          // na Teco.Pi je pušteno nešto drugo
        } else if (s.paused !== this.lastPaused) {
          this.lastPaused = s.paused;
          await this.notifyExternalStateChange(s.paused ? S.PAUSED : S.PLAYING);
        }
      } catch (e) {
        // prolazne greške u komunikaciji sa telefonom se ignorišu
      }
      busy = false;
    }, 1500);
  }
}

const player = new TecoPlayer();
const receiver = new YouTubeCastReceiver(player, {
  device: { name: 'Teco.Pi', screenName: 'Teco.Pi', brand: 'Teco', model: 'Pi' },
  dial: { port: 8099 },
  dataStore: store,
  logLevel: Constants.LOG_LEVELS.WARN,
  // plejer se resetuje samo kad se telefon izričito odvoji (ne kad se aplikacija spusti ili zaključa telefon)
  resetPlayerOnDisconnectPolicy: Constants.RESET_PLAYER_ON_DISCONNECT_POLICIES.ALL_EXPLICITLY_DISCONNECTED,
});

const stamp = () => new Date().toISOString().slice(11, 19);
const senderName = sender => (sender && (sender.user?.name || sender.name || sender.device?.name)) || 'Telefon';
receiver.on('senderConnect', sender => { console.log(stamp(), 'povezan:', senderName(sender)); call('sender', { name: senderName(sender), on: true }); });
receiver.on('senderDisconnect', (sender, implicit) => { console.log(stamp(), 'odvojen:', senderName(sender), implicit ? '(implicitno)' : ''); call('sender', { name: senderName(sender), on: false }); });
// posle restarta servera on ne zna ko je povezan: javljamo ponovo na 20 s
setInterval(() => { const s = receiver.getConnectedSenders(); if (s.length) call('sender', { name: senderName(s[0]), on: true }); }, 20000);
receiver.on('error', e => console.error('greška:', e?.message || e));
receiver.on('terminate', e => { console.error('prekid:', e?.message || e); process.exit(1); });

for (const sig of ['SIGTERM', 'SIGINT']) {
  process.on(sig, async () => { try { await receiver.stop(); } catch (e) {} process.exit(0); });
}

await receiver.start();
console.log('Teco.Pi cast spreman');
