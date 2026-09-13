// Undo/Redo。
//
// MAPは最大24行×3数値なので、逆操作を書くよりスナップショットを積む方が単純で
// 壊れにくい。ドラッグ中は積まず、pointerdown で1回・pointerup で確定させること
// （さもないと1ドラッグで数百エントリ積まれてUndoが使い物にならなくなる）。

import { clone } from './maptable.js';

export class History {
  constructor(limit = 100) {
    this.limit = limit;
    this.undoStack = [];
    this.redoStack = [];
  }

  get canUndo() { return this.undoStack.length > 0; }

  get canRedo() { return this.redoStack.length > 0; }

  /** 変更「前」の状態を積む。変更を確定させる直前に呼ぶ。 */
  commit(rowsBefore) {
    this.undoStack.push(clone(rowsBefore));
    if (this.undoStack.length > this.limit) this.undoStack.shift();
    this.redoStack.length = 0;
  }

  /** 現在の状態を渡すと、1つ前の状態を返す。戻せなければ null。 */
  undo(current) {
    if (!this.canUndo) return null;
    this.redoStack.push(clone(current));
    return this.undoStack.pop();
  }

  redo(current) {
    if (!this.canRedo) return null;
    this.undoStack.push(clone(current));
    return this.redoStack.pop();
  }

  clear() {
    this.undoStack.length = 0;
    this.redoStack.length = 0;
  }
}
