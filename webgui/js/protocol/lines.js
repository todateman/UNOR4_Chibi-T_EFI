// 受信行の分類とテレメトリのパース。
//
// ファーム（src/map_console.cpp）の契約:
//   - 非空行1つにつき "OK ..." / "ERR ..." が1行だけ返る
//   - USB出力は行単位で排他されるので、行が途中で混ざることはない
//   - 空行には応答が返らない（送ってはいけない）
//   - 応答の終端は CRLF
// テレメトリとコマンド応答が同じストリームに混在するため、まず行を分類する。
//
// 規則は tools/map_protocol.py の classify_line() と一致させること。

export const KIND = {
  TELEMETRY: 'telemetry',
  ACK_OK: 'ack_ok',
  ACK_ERR: 'ack_err',
  BODY: 'body',
};

/**
 * 受信した1行を分類する。
 * テレメトリ判定をタブの有無で行うのは、新書式("T\t...")と旧2Hz書式(9フィールドの
 * タブ区切り)の両方を1つの規則で拾うため。応答行とCSVダンプ行はタブを含まない。
 */
export function classifyLine(text) {
  if (!text) return KIND.BODY;
  if (text.includes('\t')) return KIND.TELEMETRY;
  if (text.startsWith('OK')) return KIND.ACK_OK;
  if (text.startsWith('ERR')) return KIND.ACK_ERR;
  return KIND.BODY;   // CSV行 / INFO行 / HELP行 / 起動バナー
}

export const FLAG = {
  ENG_ON: 0x01,
  LAUNCH: 0x02,
  CRANKING: 0x04,
  OUT_OF_RANGE: 0x08,
};

/**
 * テレメトリ行をパースする。値はファームの生の整数のまま保持し、
 * 表示単位への換算は描画側で行う（丸め誤差の混入源を1箇所に閉じ込める）。
 *
 * 新: T\t<seq>\t<ms>\t<rpm>\t<inj01>\t<ign>\t<spd01>\t<ne>\t<row>\t<flags>
 * 旧: <rpm>\t<inj_ms>\t<ign>\t<speed>\t<dist>\t<gas>\t<fuel>\t<work>\t<ne>
 */
export function parseTelemetry(text) {
  const p = text.split('\t');
  if (p[0] === 'T') {
    if (p.length < 10) return null;
    const v = p.slice(1, 10).map(Number);
    if (v.some(Number.isNaN)) return null;
    return {
      seq: v[0], ms: v[1], rpm: v[2], inj01: v[3], ign: v[4],
      spd01: v[5], ne: v[6], row: v[7], flags: v[8], legacy: false,
    };
  }
  if (p.length === 9) {
    const rpm = Number(p[0]);
    const inj = Number(p[1]);
    const ign = Number(p[2]);
    const spd = Number(p[3]);
    const ne = Number(p[8]);
    if ([rpm, inj, ign, spd, ne].some(Number.isNaN)) return null;
    // 旧書式は seq / row / flags を持たない
    return {
      seq: 0, ms: 0, rpm, inj01: Math.round(inj * 10), ign,
      spd01: Math.round(spd * 10), ne, row: 255, flags: 0, legacy: true,
    };
  }
  return null;
}

/** テレメトリ1サンプルから「稼働中か」を判定する。MAP INFO のスナップショットより信用する。 */
export function isRunning(t) {
  return !!t && ((t.flags & FLAG.ENG_ON) !== 0 || t.rpm > 0);
}

/** "INFO src=EEPROM rows=15 crc=0x52F4 eeprom=VALID rpm=0 eng=OFF" を dict にする。 */
export function parseInfo(bodyLines) {
  const out = {};
  for (const line of bodyLines) {
    if (!line.startsWith('INFO ')) continue;
    for (const token of line.slice(5).split(/\s+/)) {
      const eq = token.indexOf('=');
      if (eq < 0) continue;
      out[token.slice(0, eq)] = token.slice(eq + 1);
    }
  }
  if (out.rows !== undefined) out.rows = Number(out.rows);
  if (out.rpm !== undefined) out.rpm = Number(out.rpm);
  // crc はゼロ埋め無しの大文字HEX（0x52F4 / 0xA3）
  if (out.crc !== undefined) out.crc = parseInt(out.crc, 16);
  return out;
}

/** "OK VER 1.1.0 proto=2" を { fw, proto } にする。旧ファームは proto=1 とみなす。 */
export function parseVersion(ack) {
  const out = { fw: 'unknown', proto: 1 };
  const parts = ack.split(/\s+/);   // ["OK","VER","1.1.0","proto=2"]
  if (parts[2]) out.fw = parts[2];
  for (const p of parts) {
    if (p.startsWith('proto=')) out.proto = Number(p.slice(6)) || 1;
  }
  return out;
}

/** MAP? の本文から (rpm, inj, ign) の配列を取り出す。 */
export function parseDump(bodyLines) {
  const rows = [];
  for (const line of bodyLines) {
    if (!line || !/^\d/.test(line)) continue;   // ヘッダ行を捨てる
    const parts = line.split(',').map((s) => s.trim());
    if (parts.length < 3) continue;
    const v = parts.slice(0, 3).map(Number);
    if (v.some(Number.isNaN)) continue;
    rows.push({ rpm: v[0], inj: v[1], ign: v[2] });
  }
  return rows;
}

/** コマンド別のタイムアウト(ms)。データフラッシュ操作は数msブロックするので長い。 */
export function timeoutFor(line) {
  const up = line.toUpperCase();
  if (up.startsWith('MAP SAVE') || up.startsWith('MAP LOAD') || up.startsWith('MAP DEFAULT')) {
    return 10000;
  }
  if (up.startsWith('MAP?') || up === 'MAP' || up.startsWith('MAP INFO') || up.startsWith('HELP')) {
    return 3000;
  }
  return 2000;
}

/** 送信前に弾くべき行かを判定する。問題なければ null。 */
export function rejectReason(line) {
  if (!line || !line.trim()) return '空行は送信できません（ファームが応答を返しません）';
  if (line.length >= 95) return '1行は95バイト未満にしてください（ERR LINE_TOO_LONG になります）';
  // eslint-disable-next-line no-control-regex
  if (/[^\x00-\x7F]/.test(line)) return 'ASCII以外の文字は送信できません';
  return null;
}
