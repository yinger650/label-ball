/* 浏览器端 AOSS（S3 兼容）客户端。
 *
 * 用 aws4fetch 做 SigV4，不用 @aws-sdk/client-s3：
 * AWS SDK JS v3 会给 GetObject 加上 amz-sdk-* / x-id 等头，
 * SenseCore QuarkOSS 对其返回 AccessDenied（同 AK 的 boto3 却正常）。
 *
 * 凭证只存在 localStorage，服务端永不接触。
 */
'use strict';

const LS_CRED = 'ball_gt_credentials';
const LS_WHO = 'ball_gt_annotator';
const CLAIM_TTL_SEC = 36 * 3600;

const DEFAULT_CRED = {
  endpoint: 'https://aoss.cn-sh-01b.sensecoreapi-oss.cn',
  access_key: '',
  secret_key: '',
  bucket: 'wangmin',
  prefix: 'ball_det/',
};

let _aws = null;
let _AwsClient = null;

function loadCred() {
  try {
    return { ...DEFAULT_CRED, ...JSON.parse(localStorage.getItem(LS_CRED) || '{}') };
  } catch {
    return { ...DEFAULT_CRED };
  }
}

function saveCred(c) {
  localStorage.setItem(LS_CRED, JSON.stringify(c));
  _aws = null;
}

function getWho() {
  return (localStorage.getItem(LS_WHO) || '').trim();
}

function setWho(name) {
  localStorage.setItem(LS_WHO, (name || '').trim());
}

function prefixOf(c) {
  return (c.prefix || 'ball_det/').replace(/^\/+|\/+$/g, '') + '/';
}

function keyOf(c, ...parts) {
  return prefixOf(c) + parts.map((p) => String(p).replace(/^\/+|\/+$/g, '')).join('/');
}

function endpointOf(c) {
  return c.endpoint.replace(/\/$/, '');
}

/** path-style: https://endpoint/bucket/key */
function objectUrl(c, key) {
  return `${endpointOf(c)}/${c.bucket}/${key.split('/').map(encodeURIComponent).join('/')}`;
}

async function ensureAws() {
  if (_aws) return _aws;
  const c = loadCred();
  if (!c.access_key || !c.secret_key) throw new Error('尚未配置对象存储凭证');
  if (!_AwsClient) {
    const mod = await import('https://cdn.jsdelivr.net/npm/aws4fetch@1.0.20/+esm');
    _AwsClient = mod.AwsClient;
  }
  _aws = new _AwsClient({
    accessKeyId: c.access_key,
    secretAccessKey: c.secret_key,
    region: 'us-east-1',
    service: 's3',
  });
  return _aws;
}

async function s3Fetch(url, init) {
  const aws = await ensureAws();
  const r = await aws.fetch(url, init);
  if (!r.ok) {
    let detail = '';
    try { detail = (await r.text()).slice(0, 300); } catch (_) {}
    const err = new Error(`AOSS ${r.status}: ${detail || r.statusText}`);
    err.status = r.status;
    throw err;
  }
  return r;
}

function parseListXml(xml) {
  const doc = new DOMParser().parseFromString(xml, 'application/xml');
  const contents = [...doc.querySelectorAll('Contents')].map((node) => {
    const get = (t) => (node.querySelector(t) || {}).textContent || '';
    return {
      Key: get('Key'),
      Size: Number(get('Size') || 0),
      LastModified: get('LastModified'),
    };
  });
  const truncated = (doc.querySelector('IsTruncated') || {}).textContent === 'true';
  const nextToken = (doc.querySelector('NextContinuationToken') || {}).textContent || '';
  return { contents, truncated, nextToken };
}

async function listAll(prefix) {
  const c = loadCred();
  const out = [];
  let token = '';
  do {
    const qs = new URLSearchParams({
      'list-type': '2',
      prefix,
      'encoding-type': 'url',
    });
    if (token) qs.set('continuation-token', token);
    const url = `${endpointOf(c)}/${c.bucket}?${qs.toString()}`;
    const r = await s3Fetch(url, { method: 'GET' });
    const xml = await r.text();
    const parsed = parseListXml(xml);
    // encoding-type=url 时 Key 是 URL 编码的
    for (const o of parsed.contents) {
      try { o.Key = decodeURIComponent(o.Key.replace(/\+/g, ' ')); } catch (_) {}
      out.push(o);
    }
    token = parsed.truncated ? parsed.nextToken : '';
  } while (token);
  return out;
}

async function putJson(subKey, obj) {
  const c = loadCred();
  const key = keyOf(c, subKey);
  const body = JSON.stringify(obj);
  await s3Fetch(objectUrl(c, key), {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body,
  });
}

async function getObjectBytes(subKey) {
  const c = loadCred();
  const key = keyOf(c, subKey);
  const r = await s3Fetch(objectUrl(c, key), { method: 'GET' });
  const ab = await r.arrayBuffer();
  return new Uint8Array(ab);
}

async function poolStatus(annotator) {
  const c = loadCred();
  const base = prefixOf(c);
  const [packs, claimsRaw, resultsRaw] = await Promise.all([
    listAll(base + 'packets/'),
    listAll(base + 'claims/'),
    listAll(base + 'results/'),
  ]);

  const packets = {};
  for (const o of packs) {
    const name = o.Key.split('/').pop();
    if (!name.endsWith('.zip')) continue;
    const pid = name.slice(0, -4);
    packets[pid] = { packet_id: pid, size: o.Size, last_modified: String(o.LastModified) };
  }

  const claims = {};
  const claimsBase = base + 'claims/';
  for (const o of claimsRaw) {
    if (!o.Key.startsWith(claimsBase)) continue;
    const rel = o.Key.slice(claimsBase.length);
    if (!rel.includes('/')) continue;
    const [pid, whoFile] = rel.split('/');
    const who = whoFile.endsWith('.json') ? whoFile.slice(0, -5) : whoFile;
    const mtime = o.LastModified ? new Date(o.LastModified).getTime() / 1000 : 0;
    (claims[pid] || (claims[pid] = [])).push({ who, mtime });
  }

  const results = {};
  for (const o of resultsRaw) {
    const name = o.Key.split('/').pop();
    if (!name.endsWith('.json') || !name.includes('__')) continue;
    const body = name.slice(0, -5);
    const i = body.lastIndexOf('__');
    const pid = body.slice(0, i);
    const who = body.slice(i + 2);
    (results[pid] || (results[pid] = [])).push(who);
  }

  const now = Date.now() / 1000;
  const available = [], mine = [], taken = [], done = [];
  for (const pid of Object.keys(packets)) {
    if (results[pid]) { done.push(pid); continue; }
    const cs = claims[pid] || [];
    if (cs.some((x) => x.who === annotator)) mine.push(pid);
    else if (cs.some((x) => now - x.mtime < CLAIM_TTL_SEC)) taken.push(pid);
    else available.push(pid);
  }
  // 新包优先：旧包 ROI 纵向偏窄（飞球/近端球裁掉），按 LastModified 降序领取
  const byNew = (a, b) => {
    const ta = Date.parse(packets[a].last_modified || 0) || 0;
    const tb = Date.parse(packets[b].last_modified || 0) || 0;
    return tb - ta;
  };
  available.sort(byNew);
  mine.sort(byNew);
  return { n_packets: Object.keys(packets).length, available, mine, taken, done, packets };
}

async function claim(packetId, annotator) {
  const st = await poolStatus(annotator);
  if (st.mine.includes(packetId)) return true;
  if (!st.available.includes(packetId)) return false;
  const now = Date.now() / 1000;
  await putJson(`claims/${packetId}/${annotator}.json`, {
    packet_id: packetId, annotator, claimed_at: now,
  });
  const again = await poolStatus(annotator);
  return again.mine.includes(packetId);
}

async function downloadPacketZip(packetId, onProgress) {
  const bytes = await getObjectBytes(`packets/${packetId}.zip`);
  if (onProgress) onProgress(bytes.length, bytes.length);
  return new Blob([bytes], { type: 'application/zip' });
}

async function submitResult(packetId, annotator, result) {
  const sub = `results/${packetId}__${annotator}.json`;
  await putJson(sub, result);
  return sub;
}

window.BallGtAoss = {
  loadCred, saveCred, getWho, setWho, DEFAULT_CRED,
  poolStatus, claim, downloadPacketZip, submitResult,
};
