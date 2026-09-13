// オフライン用の Service Worker。
//
// サーキットやピットではネットが無いので、GitHub Pages 版も初回アクセス後は
// オフラインで動くようにする。外部CDNを一切使っていないので、自分のファイルを
// キャッシュするだけで完結する。
//
// 更新時は CACHE の版数を上げること。

const CACHE = 'map-tuner-v1';

const ASSETS = [
  './',
  './index.html',
  './css/app.css',
  './js/app.js',
  './js/protocol/lines.js',
  './js/transport/webserial.js',
  './js/transport/httpbridge.js',
  './js/model/maptable.js',
  './js/model/csv.js',
  './js/model/history.js',
  './js/model/ring.js',
  './js/ui/chart.js',
  './js/ui/table.js',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  // ローカルサーバのAPIは絶対にキャッシュしない（テレメトリとコマンドの経路）
  if (url.pathname.startsWith('/api/')) return;
  if (e.request.method !== 'GET') return;

  // network-first: 更新があれば拾い、落ちていればキャッシュで動かす
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(e.request).then((r) => r || caches.match('./index.html'))),
  );
});
