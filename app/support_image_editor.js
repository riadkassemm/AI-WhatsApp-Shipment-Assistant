// Editor coordinates are stored in the working image's pixels. CSS resizing never
// changes the object model, and history stores overlays rather than image copies.
// Rotate and crop are recorded as operations and replayed from the original
// bitmap, so undo never has to keep full-size copies of earlier images.
class ChatImageEditor {
  constructor(bitmap, mime) {
    this.bitmap = bitmap;
    this.mime = mime;
    const ratio = Math.min(1, 4096 / bitmap.width, 4096 / bitmap.height,
      Math.sqrt(16000000 / (bitmap.width * bitmap.height)));
    this.sourceWidth = Math.max(1, Math.round(bitmap.width * ratio));
    this.sourceHeight = Math.max(1, Math.round(bitmap.height * ratio));
    this.canvas = document.createElement('canvas');
    this.canvas.width = this.sourceWidth;
    this.canvas.height = this.sourceHeight;
    this.canvas.setAttribute('aria-label', 'Image editing canvas');
    this.canvas.tabIndex = 0;
    this.ctx = this.canvas.getContext('2d');
    if (!this.ctx) throw new Error('This browser could not create an image editing canvas.');
    this.objects = [];
    this.ops = [];
    this.base = bitmap;
    this.baseOps = this.ops;
    this.history = [{ops: this.ops, objects: []}]; this.historyIndex = 0;
    this.selected = -1; this.mode = 'draw'; this.pointer = null; this.cropRect = null;
    this.sendId = crypto.randomUUID(); this.sending = false;
    this.onChange = null; this.onResize = null;
    this.canvas.addEventListener('pointerdown', event => this.pointerDown(event));
    this.canvas.addEventListener('pointermove', event => this.pointerMove(event));
    this.canvas.addEventListener('pointerup', event => this.pointerUp(event));
    this.canvas.addEventListener('pointercancel', event => this.pointerUp(event));
    this.canvas.addEventListener('dblclick', event => this.doubleClick(event));
    this.canvas.addEventListener('keydown', event => {
      if (['Delete', 'Backspace'].includes(event.key) && this.selected >= 0) {
        event.preventDefault(); this.removeSelected();
      } else if (event.key === 'Enter' && this.mode === 'crop' && this.cropRect) {
        event.preventDefault(); this.applyCrop();
      }
    });
    this.resize(); this.render();
  }
  get dirty() { return this.historyIndex !== 0 || Boolean($('editorCaption').value.trim()); }
  get canUndo() { return this.historyIndex > 0; }
  get canRedo() { return this.historyIndex < this.history.length - 1; }
  get selectedType() { return this.objects[this.selected]?.type || null; }
  notify() { if (typeof this.onChange === 'function') this.onChange(this); }
  setMode(mode) {
    this.mode = mode;
    this.cropRect = null;
    if (mode !== 'select' && mode !== 'text') this.selected = -1;
    this.render(); this.notify();
  }
  resize() {
    const body = $('mediaViewerBody');
    const fit = Math.min((body.clientWidth - 20) / this.canvas.width, (body.clientHeight - 20) / this.canvas.height, 1);
    this.canvas.style.width = Math.round(this.canvas.width * fit) + 'px';
    this.canvas.style.height = Math.round(this.canvas.height * fit) + 'px';
  }
  point(event) {
    const rect = this.canvas.getBoundingClientRect();
    return {x: (event.clientX - rect.left) * this.canvas.width / rect.width,
      y: (event.clientY - rect.top) * this.canvas.height / rect.height};
  }
  snapshot() {
    this.history = this.history.slice(0, this.historyIndex + 1);
    this.history.push({ops: this.ops, objects: structuredClone(this.objects)});
    this.historyIndex++;
    this.render(); this.notify();
  }
  restore(entry) {
    this.objects = structuredClone(entry.objects);
    this.ops = entry.ops;
    this.selected = -1; this.cropRect = null;
    this.rebuildBase();
    this.render(); this.notify();
  }
  undo() { if (this.canUndo) this.restore(this.history[--this.historyIndex]); }
  redo() { if (this.canRedo) this.restore(this.history[++this.historyIndex]); }
  drawObjects(ctx, objects, selection) {
    objects.forEach((object, index) => {
      if (object.type === 'stroke') {
        ctx.beginPath(); ctx.strokeStyle = object.color; ctx.lineWidth = object.width;
        ctx.lineCap = 'round'; ctx.lineJoin = 'round';
        const points = object.points;
        if (!points.length) return;
        ctx.moveTo(points[0].x, points[0].y);
        if (points.length === 1) ctx.lineTo(points[0].x + 0.01, points[0].y + 0.01);
        else for (let i = 1; i < points.length; i++) ctx.lineTo(points[i].x, points[i].y);
        ctx.stroke();
        if (selection && index === this.selected) {
          ctx.setLineDash([8, 6]); ctx.strokeStyle = '#ffffff'; ctx.lineWidth = Math.max(2, object.width / 3);
          ctx.stroke(); ctx.setLineDash([]);
        }
      } else {
        ctx.fillStyle = object.color; ctx.font = object.size + 'px sans-serif';
        ctx.textBaseline = 'top'; ctx.textAlign = 'left';
        object.text.split('\n').forEach((line, lineIndex) => ctx.fillText(line, object.x, object.y + lineIndex * object.size * 1.2));
        if (selection && index === this.selected) {
          const bounds = this.textBounds(object);
          ctx.setLineDash([6, 5]); ctx.strokeStyle = '#ffffff'; ctx.lineWidth = 2;
          ctx.strokeRect(object.x - 4, object.y - 4, bounds.width + 8, bounds.height + 8);
          ctx.setLineDash([]);
        }
      }
    });
  }
  // Replays rotate/crop operations from the original bitmap. Overlays drawn
  // before an operation are baked into the image at that step.
  rebuildBase() {
    if (this.baseOps === this.ops) return;
    let source = this.bitmap;
    let width = this.sourceWidth;
    let height = this.sourceHeight;
    for (const op of this.ops) {
      const flat = document.createElement('canvas');
      flat.width = width; flat.height = height;
      const flatCtx = flat.getContext('2d');
      flatCtx.drawImage(source, 0, 0, width, height);
      this.drawObjects(flatCtx, op.baked, false);
      const next = document.createElement('canvas');
      if (op.type === 'rotate') {
        next.width = height; next.height = width;
        const ctx = next.getContext('2d');
        ctx.translate(height, 0);
        ctx.rotate(Math.PI / 2);
        ctx.drawImage(flat, 0, 0);
      } else {
        next.width = op.rect.w; next.height = op.rect.h;
        next.getContext('2d').drawImage(flat, op.rect.x, op.rect.y, op.rect.w, op.rect.h, 0, 0, op.rect.w, op.rect.h);
      }
      source = next; width = next.width; height = next.height;
    }
    this.base = source;
    this.baseOps = this.ops;
    if (this.canvas.width !== width || this.canvas.height !== height) {
      this.canvas.width = width; this.canvas.height = height;
      this.resize();
      if (typeof this.onResize === 'function') this.onResize(this);
    }
  }
  applyOperation(op) {
    this.ops = [...this.ops, {...op, baked: structuredClone(this.objects)}];
    this.objects = [];
    this.selected = -1; this.cropRect = null;
    this.rebuildBase();
    this.snapshot();
  }
  rotate() {
    if (this.sending) return;
    this.applyOperation({type: 'rotate'});
  }
  applyCrop() {
    const rect = this.normalizedCrop();
    if (!rect) return false;
    this.mode = 'draw';
    this.applyOperation({type: 'crop', rect});
    return true;
  }
  cancelCrop() { this.setMode('draw'); }
  normalizedCrop() {
    if (!this.cropRect) return null;
    const {x1, y1, x2, y2} = this.cropRect;
    const x = Math.max(0, Math.round(Math.min(x1, x2)));
    const y = Math.max(0, Math.round(Math.min(y1, y2)));
    const w = Math.min(this.canvas.width, Math.round(Math.max(x1, x2))) - x;
    const h = Math.min(this.canvas.height, Math.round(Math.max(y1, y2))) - y;
    if (w < 8 || h < 8) return null;
    return {x, y, w, h};
  }
  render(selection = true) {
    const ctx = this.ctx;
    ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    ctx.drawImage(this.base, 0, 0, this.canvas.width, this.canvas.height);
    this.drawObjects(ctx, this.objects, selection);
    const crop = selection && this.mode === 'crop' ? this.normalizedCrop() : null;
    if (crop) {
      ctx.fillStyle = 'rgba(0, 0, 0, 0.55)';
      ctx.fillRect(0, 0, this.canvas.width, crop.y);
      ctx.fillRect(0, crop.y + crop.h, this.canvas.width, this.canvas.height - crop.y - crop.h);
      ctx.fillRect(0, crop.y, crop.x, crop.h);
      ctx.fillRect(crop.x + crop.w, crop.y, this.canvas.width - crop.x - crop.w, crop.h);
      ctx.setLineDash([10, 6]); ctx.strokeStyle = '#ffffff';
      ctx.lineWidth = Math.max(2, Math.round(this.canvas.width / 500));
      ctx.strokeRect(crop.x, crop.y, crop.w, crop.h);
      ctx.setLineDash([]);
    }
  }
  textBounds(object) {
    this.ctx.font = object.size + 'px sans-serif';
    return {width: Math.max(1, ...object.text.split('\n').map(line => this.ctx.measureText(line).width)),
      height: object.text.split('\n').length * object.size * 1.2};
  }
  hitText(point) {
    for (let i = this.objects.length - 1; i >= 0; i--) {
      const object = this.objects[i];
      if (object.type !== 'text') continue;
      const bounds = this.textBounds(object);
      if (point.x >= object.x - 12 && point.x <= object.x + bounds.width + 12 &&
          point.y >= object.y - 12 && point.y <= object.y + bounds.height + 12) return i;
    }
    return -1;
  }
  hitStroke(point) {
    for (let i = this.objects.length - 1; i >= 0; i--) {
      const object = this.objects[i];
      if (object.type !== 'stroke') continue;
      for (let j = 0; j < object.points.length; j++) {
        const a = object.points[j];
        const b = object.points[Math.min(j + 1, object.points.length - 1)];
        const lengthSquared = (b.x - a.x) ** 2 + (b.y - a.y) ** 2;
        const t = lengthSquared ? Math.max(0, Math.min(1, ((point.x - a.x) * (b.x - a.x) + (point.y - a.y) * (b.y - a.y)) / lengthSquared)) : 0;
        const x = a.x + t * (b.x - a.x), y = a.y + t * (b.y - a.y);
        if (Math.hypot(x - point.x, y - point.y) <= Math.max(18, object.width * 2)) return i;
      }
    }
    return -1;
  }
  // Uses the dashboard's in-app dialog when available (window.promptDialog).
  askText(title, value = '') {
    if (typeof window.promptDialog === 'function') {
      return window.promptDialog({title, label: 'Text', value, confirmLabel: value ? 'Save text' : 'Add text'});
    }
    return Promise.resolve(prompt(title, value));
  }
  async addTextAt(point) {
    this.textPending = true;
    try {
      const value = await this.askText('Add text');
      if (value?.trim()) {
        this.objects.push({type: 'text', text: value.slice(0, 500), x: point.x, y: point.y,
          color: $('editorColor').value, size: Number($('editorSize').value) * 5});
        this.selected = this.objects.length - 1; this.snapshot();
      }
    } finally {
      this.textPending = false;
      this.canvas.focus?.();
    }
  }
  pointerDown(event) {
    if (this.sending || this.pointer || this.textPending || event.button === 1 || window.mediaEditorSpaceDown) return;
    event.preventDefault();
    const point = this.point(event);
    if (this.mode === 'crop') {
      this.canvas.setPointerCapture(event.pointerId);
      this.cropRect = {x1: point.x, y1: point.y, x2: point.x, y2: point.y};
      this.pointer = {id: event.pointerId, kind: 'crop'};
      this.render(); this.notify();
      return;
    }
    if (this.mode === 'text' || this.mode === 'select') {
      this.selected = this.hitText(point);
      if (this.selected < 0) this.selected = this.hitStroke(point);
      if (this.selected < 0 && this.mode === 'text') {
        this.render(); this.notify();
        return this.addTextAt(point);
      }
      if (this.selected >= 0 && this.objects[this.selected].type === 'text') {
        this.canvas.setPointerCapture(event.pointerId);
        this.pointer = {id: event.pointerId, kind: 'move', point, moved: false};
      }
      this.render(); this.notify();
    } else {
      this.canvas.setPointerCapture(event.pointerId);
      this.selected = -1;
      this.objects.push({type: 'stroke', color: $('editorColor').value,
        width: Number($('editorSize').value), points: [point]});
      this.pointer = {id: event.pointerId, kind: 'draw'}; this.render();
    }
  }
  pointerMove(event) {
    if (!this.pointer || this.pointer.id !== event.pointerId) return;
    const point = this.point(event);
    if (this.pointer.kind === 'crop') {
      this.cropRect.x2 = Math.max(0, Math.min(this.canvas.width, point.x));
      this.cropRect.y2 = Math.max(0, Math.min(this.canvas.height, point.y));
    } else if (this.pointer.kind === 'draw') {
      const samples = event.getCoalescedEvents?.() || [event];
      for (const sample of samples) this.objects[this.objects.length - 1].points.push(this.point(sample));
    }
    else if (this.selected >= 0) {
      const object = this.objects[this.selected];
      object.x += point.x - this.pointer.point.x;
      object.y += point.y - this.pointer.point.y;
      this.pointer.point = point; this.pointer.moved = true;
    }
    this.render(); event.preventDefault();
  }
  pointerUp(event) {
    if (!this.pointer || this.pointer.id !== event.pointerId) return;
    const kind = this.pointer.kind;
    if (kind === 'draw' || this.pointer.moved) this.snapshot();
    this.pointer = null;
    if (kind === 'crop') {
      if (!this.normalizedCrop()) this.cropRect = null;
      this.render(); this.notify();
    }
  }
  cancelActiveStroke() {
    if (!this.pointer) return;
    if (this.pointer.kind === 'crop') this.cropRect = null;
    this.objects = structuredClone(this.history[this.historyIndex].objects);
    this.pointer = null;
    this.selected = -1;
    this.render(); this.notify();
  }
  doubleClick(event) {
    if (this.mode !== 'text' && this.mode !== 'select') return;
    const selected = this.hitText(this.point(event));
    if (selected < 0) return;
    return this.editText(selected);
  }
  async editText(selected = this.selected) {
    if (selected < 0 || this.objects[selected]?.type !== 'text' || this.textPending) return;
    const object = this.objects[selected];
    this.textPending = true;
    let value;
    try { value = await this.askText('Edit text', object.text); }
    finally { this.textPending = false; this.canvas.focus?.(); }
    if (value === null || value === undefined || value === object.text) return;
    if (!value.trim()) { this.objects.splice(selected, 1); this.selected = -1; }
    else { object.text = value.slice(0, 500); this.selected = selected; }
    this.snapshot();
  }
  removeSelected() {
    if (this.selected < 0) return;
    this.objects.splice(this.selected, 1); this.selected = -1; this.snapshot();
  }
  setStyle() {
    if (this.selected < 0) return;
    const object = this.objects[this.selected];
    if (object.type !== 'text') return;
    object.color = $('editorColor').value; object.size = Number($('editorSize').value) * 5;
    this.snapshot();
  }
  // Lossless copy of the edited image for the clipboard (which only accepts PNG).
  async pngBlob() {
    this.render(false);
    const blob = await new Promise(resolve => this.canvas.toBlob(resolve, 'image/png'));
    this.render();
    if (!blob) throw new Error('Could not export the edited image.');
    return blob;
  }
  async blob() {
    this.render(false);
    const mime = this.mime === 'image/jpeg' ? 'image/jpeg' : 'image/png';
    const limit = __IMAGE_MAX_BYTES__;
    let output = this.canvas;
    let blob = null;
    for (let attempt = 0; attempt < 6; attempt++) {
      blob = await new Promise(resolve => output.toBlob(resolve, mime, Math.max(0.65, 0.92 - attempt * 0.06)));
      if (!blob || blob.size <= limit) break;
      if (mime === 'image/jpeg' && attempt < 2) continue;
      const reduced = document.createElement('canvas');
      reduced.width = Math.max(1, Math.round(output.width * 0.82));
      reduced.height = Math.max(1, Math.round(output.height * 0.82));
      reduced.getContext('2d').drawImage(output, 0, 0, reduced.width, reduced.height);
      output = reduced;
    }
    this.render();
    if (!blob) throw new Error('Could not export the edited image.');
    if (blob.size > limit) throw new Error('Edited image is too large to send. Try fewer overlays or a smaller source.');
    return blob;
  }
  dispose() { if (this.bitmap.close) this.bitmap.close(); }
}

window.ChatImageEditor = ChatImageEditor;
