// Запись сцены «Лофи-городов» прямо с canvas (480×270) через captureStream → webm
const { chromium } = require('playwright');
const fs = require('fs');
const [,, OUT, SHOTS] = process.argv;
const shots = JSON.parse(fs.readFileSync(SHOTS, 'utf8'));
(async () => {
  const b = await chromium.launch({ args: ['--autoplay-policy=no-user-gesture-required'] });
  for (const s of shots) {
    const ctx = await b.newContext({ viewport: { width: 960, height: 540 } });
    await ctx.addInitScript((ls) => { for (const [k, v] of Object.entries(ls)) localStorage.setItem('lg:' + k, JSON.stringify(v)); },
      Object.assign({ introSeen: true }, s.ls || {}));
    const p = await ctx.newPage();
    await p.goto('http://127.0.0.1:8765' + s.url);
    await p.waitForTimeout(s.warm || 4000);
    const rec = p.evaluate(async (ms) => {
      const c = document.getElementById('scene');
      const st = c.captureStream(30);
      const r = new MediaRecorder(st, { mimeType: 'video/webm;codecs=vp9', videoBitsPerSecond: 12e6 });
      const parts = []; r.ondataavailable = (e) => parts.push(e.data);
      const done = new Promise((res) => (r.onstop = res));
      r.start(); await new Promise((res) => setTimeout(res, ms)); r.stop(); await done;
      const buf = await new Blob(parts).arrayBuffer();
      let bin = ''; const u8 = new Uint8Array(buf);
      for (let i = 0; i < u8.length; i += 0x8000) bin += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000));
      return btoa(bin);
    }, s.sec * 1000);
    if (s.clickCurtainAt != null) {          // открыть/закрыть шторы кликом по краю шторы (хот-спот 0..30 px сцены)
      await p.waitForTimeout(s.clickCurtainAt * 1000);
      const r = await p.locator('#scene').boundingBox();
      await p.mouse.click(r.x + r.width * (12 / 480), r.y + r.height * (100 / 270));
    }
    fs.writeFileSync(OUT + '/' + s.name + '.webm', Buffer.from(await rec, 'base64'));
    console.log('ok', s.name);
    await ctx.close();
  }
  await b.close();
})();
