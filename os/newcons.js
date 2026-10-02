// Realističniji crteži PS1 i PS2 (blaga 3D perspektiva, bez logotipa); upisuje ih u consoles.json
const fs = require('fs');
const file = process.argv[2];
const C = JSON.parse(fs.readFileSync(file, 'utf8'));

C.ps1.svg = `<svg viewBox="0 0 260 150" role="img" aria-label="PlayStation 1">
<defs>
<linearGradient id="p1top" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#e3e4e8"/><stop offset="1" stop-color="#c9cacf"/></linearGradient>
<linearGradient id="p1front" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#b9bac0"/><stop offset="1" stop-color="#9c9da4"/></linearGradient>
<radialGradient id="p1lid" cx=".45" cy=".35" r=".7"><stop offset="0" stop-color="#d9dade"/><stop offset="1" stop-color="#bfc0c6"/></radialGradient>
<radialGradient id="p1btn" cx=".4" cy=".3" r=".8"><stop offset="0" stop-color="#e6e7ea"/><stop offset="1" stop-color="#b3b4ba"/></radialGradient>
</defs>
<path d="M44 38H216Q222 38 224 43L244 92Q245 96 240 96H20Q15 96 16 92L36 43Q38 38 44 38Z" fill="url(#p1top)" stroke="#a3a4ab" stroke-width=".8"/>
<path d="M16 96H244V114Q244 120 238 120H22Q16 120 16 114Z" fill="url(#p1front)" stroke="#8f9097" stroke-width=".8"/>
<path d="M18 96.6H242" stroke="#eceef1" stroke-width="1"/>
<path d="M150 44H214M154 50H216M158 56H218" stroke="#b8b9bf" stroke-width="1"/>
<ellipse cx="100" cy="68" rx="54" ry="24" fill="#a9aab0"/>
<ellipse cx="100" cy="66" rx="53" ry="23" fill="url(#p1lid)" stroke="#9d9ea5" stroke-width="1"/>
<ellipse cx="100" cy="66" rx="42" ry="18" fill="none" stroke="#b0b1b7" stroke-width="1.2"/>
<ellipse cx="100" cy="66" rx="9" ry="4" fill="#c4c5ca" stroke="#a6a7ad" stroke-width=".8"/>
<ellipse cx="190" cy="61" rx="15" ry="7.5" fill="#9fa0a7"/>
<ellipse cx="190" cy="59.5" rx="14" ry="7" fill="url(#p1btn)" stroke="#9a9ba2" stroke-width=".8"/>
<rect x="170" y="78" width="20" height="9" rx="4.5" fill="#a3a4ab"/><rect x="170" y="76.6" width="20" height="9" rx="4.5" fill="url(#p1btn)" stroke="#9a9ba2" stroke-width=".7"/>
<rect x="198" y="78" width="20" height="9" rx="4.5" fill="#a3a4ab"/><rect x="198" y="76.6" width="20" height="9" rx="4.5" fill="url(#p1btn)" stroke="#9a9ba2" stroke-width=".7"/>
<circle cx="226" cy="82" r="2.2" fill="#39d353"/><circle cx="226" cy="82" r="4" fill="#39d353" opacity=".25"/>
<rect x="52" y="100" width="40" height="2.4" rx="1.2" fill="#6e6f76"/>
<rect x="56" y="105" width="32" height="10" rx="3" fill="#5d5e65"/><rect x="59" y="107" width="26" height="6" rx="2" fill="#44454b"/>
<rect x="168" y="100" width="40" height="2.4" rx="1.2" fill="#6e6f76"/>
<rect x="172" y="105" width="32" height="10" rx="3" fill="#5d5e65"/><rect x="175" y="107" width="26" height="6" rx="2" fill="#44454b"/>
</svg>`;

C.ps2.svg = `<svg viewBox="0 0 260 150" role="img" aria-label="PlayStation 2">
<defs>
<linearGradient id="p2top" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#34363c"/><stop offset="1" stop-color="#202226"/></linearGradient>
<linearGradient id="p2front" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#1c1d21"/><stop offset="1" stop-color="#0f1013"/></linearGradient>
<linearGradient id="p2tray" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#26282d"/><stop offset="1" stop-color="#17181b"/></linearGradient>
</defs>
<path d="M34 40H232L248 58H18Z" fill="url(#p2top)" stroke="#3d4047" stroke-width=".8"/>
${Array.from({ length: 6 }, (_, i) => `<path d="M${40 - i * 2.4} ${43 + i * 2.6}H${226 + i * 2.4}" stroke="#191a1e" stroke-width="1.1"/>`).join('')}
<path d="M18 58H248V84H18Z" fill="url(#p2front)"/>
<path d="M18 58.5H248" stroke="#4a4d55" stroke-width="1"/>
<rect x="26" y="62" width="168" height="16" rx="2" fill="url(#p2tray)" stroke="#0a0b0d" stroke-width="1"/>
<rect x="28" y="63.5" width="164" height="1.2" fill="#3a3c43"/>
<rect x="204" y="62" width="36" height="7" rx="1.5" fill="#1f2125" stroke="#0a0b0d" stroke-width=".8"/>
<rect x="204" y="71" width="36" height="7" rx="1.5" fill="#1f2125" stroke="#0a0b0d" stroke-width=".8"/>
<circle cx="210" cy="65.5" r="1.8" fill="#22c55e"/><circle cx="210" cy="65.5" r="3.4" fill="#22c55e" opacity=".25"/>
<circle cx="210" cy="74.5" r="1.8" fill="#3b82f6"/><circle cx="210" cy="74.5" r="3.4" fill="#3b82f6" opacity=".25"/>
<path d="M22 84H244V88H22Z" fill="#08090a"/>
<path d="M18 88H248V112Q248 116 244 116H22Q18 116 18 112Z" fill="url(#p2front)"/>
<path d="M18 88.5H248" stroke="#2c2e33" stroke-width="1"/>
<rect x="30" y="92" width="24" height="3" rx="1" fill="#050506"/><rect x="60" y="92" width="24" height="3" rx="1" fill="#050506"/>
<rect x="31" y="99" width="22" height="10" rx="2.5" fill="#050506" stroke="#2a2c31" stroke-width=".8"/><rect x="61" y="99" width="22" height="10" rx="2.5" fill="#050506" stroke="#2a2c31" stroke-width=".8"/>
<rect x="130" y="101" width="9" height="5" rx="1" fill="#050506" stroke="#2a2c31" stroke-width=".6"/><rect x="143" y="101" width="9" height="5" rx="1" fill="#050506" stroke="#2a2c31" stroke-width=".6"/>
<rect x="158" y="101" width="7" height="5" rx="1" fill="#050506" stroke="#2a2c31" stroke-width=".6"/>
${Array.from({ length: 9 }, (_, i) => `<path d="M${196 + i * 5} 92V112" stroke="#08090a" stroke-width="1.6"/>`).join('')}
</svg>`;

for (const k of ['ps1', 'ps2']) C[k].svg = C[k].svg.replace(/\s*\n\s*/g, '');
fs.writeFileSync(file, JSON.stringify(C));
console.log('ok', Object.keys(C).join(','));
