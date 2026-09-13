// MAPのテーブルエディタ。
//
// 表示する情報:
//   - 実機MAPとの差分（変化量を色付きで）
//   - 検証違反セル（赤）
//   - 現在採用中の行（ファームが報告した row。GUIの推測ではない）
//   - 行ごとの滞在時間（背景バー）

import { MAP_MAX_ENTRIES, cellErrors, clampField } from '../model/maptable.js';

const FIELDS = ['rpm', 'inj', 'ign'];

export class MapTableView {
  /**
   * @param {HTMLElement} root
   * @param {{onEdit:Function, onSelect:Function, onCommitStart:Function}} handlers
   */
  constructor(root, handlers = {}) {
    this.root = root;
    this.h = handlers;
    this.state = {
      rows: [], deviceRows: [], diff: { cells: {}, breakpointsMatch: true },
      selection: new Set(), dwell: null, activeRow: -1, rpmLocked: false,
    };
    this._lastAnchor = 0;
    root.addEventListener('keydown', (e) => this._key(e));
  }

  set(state) {
    Object.assign(this.state, state);
    this.render();
  }

  _key(e) {
    const cell = e.target.closest?.('[data-field]');
    if (!cell) return;
    const i = Number(cell.dataset.index);
    const field = cell.dataset.field;
    if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
    e.preventDefault();
    const step = (e.shiftKey ? 10 : 1) * (e.key === 'ArrowUp' ? 1 : -1);
    if (this.h.onCommitStart) this.h.onCommitStart();
    const next = clampField(field, this.state.rows[i][field] + step);
    if (this.h.onEdit) this.h.onEdit(i, field, next, true);
  }

  _selectClick(i, e) {
    const sel = new Set(this.state.selection);
    if (e.shiftKey) {
      const [a, b] = [Math.min(this._lastAnchor, i), Math.max(this._lastAnchor, i)];
      for (let k = a; k <= b; k += 1) sel.add(k);
    } else if (e.metaKey || e.ctrlKey) {
      if (sel.has(i)) sel.delete(i); else sel.add(i);
      this._lastAnchor = i;
    } else {
      sel.clear();
      sel.add(i);
      this._lastAnchor = i;
    }
    if (this.h.onSelect) this.h.onSelect(sel);
  }

  render() {
    const {
      rows, diff, selection, dwell, activeRow, rpmLocked,
    } = this.state;
    const bad = cellErrors(rows);

    const head = `
      <div class="tr th">
        <span class="c-idx">#</span>
        <span class="c-num">RPM</span>
        <span class="c-num">噴射 ms</span>
        <span class="c-num">進角 CA</span>
        <span class="c-diff">差分</span>
      </div>`;

    const body = rows.map((r, i) => {
      const heat = dwell ? (dwell[i] || 0) : 0;
      const cls = [
        'tr',
        selection.has(i) ? 'sel' : '',
        i === activeRow ? 'active' : '',
      ].filter(Boolean).join(' ');

      const cell = (field, text) => {
        const key = `${i}:${field}`;
        const err = bad[key];
        const locked = field === 'rpm' && rpmLocked;
        return `<span class="c-num cell${err ? ' bad' : ''}${locked ? ' locked' : ''}"
                   data-index="${i}" data-field="${field}" tabindex="0"
                   contenteditable="${locked ? 'false' : 'true'}"
                   ${err ? `title="${err}"` : locked ? 'title="稼働中はRPMを変更できません"' : ''}
                   >${text}</span>`;
      };

      const dInj = diff.cells[`${i}:inj`];
      const dIgn = diff.cells[`${i}:ign`];
      const parts = [];
      if (dInj) parts.push(`<b class="${dInj > 0 ? 'up' : 'down'}">噴射 ${dInj > 0 ? '+' : ''}${(dInj / 10).toFixed(1)}</b>`);
      if (dIgn) parts.push(`<b class="${dIgn > 0 ? 'up' : 'down'}">進角 ${dIgn > 0 ? '+' : ''}${dIgn}</b>`);
      const diffText = diff.breakpointsMatch
        ? (parts.join(' ') || '<span class="dim">·</span>')
        : '<span class="dim">—</span>';

      return `
        <div class="${cls}" data-row="${i}" style="--heat:${heat.toFixed(3)}">
          <span class="c-idx">${i + 1}</span>
          ${cell('rpm', r.rpm)}
          ${cell('inj', (r.inj / 10).toFixed(1))}
          ${cell('ign', r.ign)}
          <span class="c-diff">${diffText}</span>
        </div>`;
    }).join('');

    this.root.innerHTML = head + body
      + `<div class="tr foot"><span class="c-idx"></span>
           <span class="rowcount">${rows.length} / ${MAP_MAX_ENTRIES} 行</span></div>`;

    this.root.querySelectorAll('.tr[data-row]').forEach((el) => {
      el.addEventListener('mousedown', (e) => {
        if (e.target.classList.contains('cell')) return;   // セル編集を邪魔しない
        this._selectClick(Number(el.dataset.row), e);
      });
    });

    this.root.querySelectorAll('.cell').forEach((el) => {
      el.addEventListener('focus', () => {
        if (this.h.onCommitStart) this.h.onCommitStart();
      });
      el.addEventListener('blur', () => this._commitCell(el));
      el.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); el.blur(); }
        if (e.key === 'Escape') { this.render(); }
      });
    });
  }

  _commitCell(el) {
    const i = Number(el.dataset.index);
    const field = el.dataset.field;
    const raw = el.textContent.trim();
    const n = Number(raw);
    if (raw === '' || Number.isNaN(n)) { this.render(); return; }
    // 噴射だけ表示がmsなので、内部のx0.1msへ戻す
    const value = clampField(field, field === 'inj' ? Math.round(n * 10) : Math.round(n));
    if (value === this.state.rows[i][field]) { this.render(); return; }
    if (this.h.onEdit) this.h.onEdit(i, field, value, true);
  }
}

/** FIELDS は UI 側の列順。model 側の行オブジェクトのキーと一致していること。 */
export { FIELDS };
