/** Deterministic synthetic pages for quality-check tests. */
import type { GrayImage } from "../quality";

export function prng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Paper with lines of "words" made of thin dark strokes, like printed text. */
export function textPage(width = 480, height = 640, paper = 212, ink = 45, seed = 1, stroke = 1, size = 1): GrayImage {
  const rand = prng(seed);
  const data = new Uint8Array(width * height).fill(paper);
  const set = (x: number, y: number, v: number) => {
    for (let sy = 0; sy < stroke; sy++) {
      for (let sx = 0; sx < stroke; sx++) {
        const xx = x + sx;
        const yy = y + sy;
        if (xx >= 0 && yy >= 0 && xx < width && yy < height) data[yy * width + xx] = v;
      }
    }
  };
  for (let top = 40; top + 10 * size < height - 40; top += 18 * size) {
    let x = 36;
    while (x < width - 60 * size) {
      const letters = 2 + Math.floor(rand() * 7);
      for (let l = 0; l < letters; l++) {
        const glyphH = (7 + Math.floor(rand() * 3)) * size;
        // A vertical stem plus a random bar and a second stem, `stroke` px thick.
        for (let dy = 0; dy < glyphH; dy++) set(x, top + dy, ink);
        if (rand() < 0.7) for (let dx = 0; dx < 4 * size; dx++) set(x + dx, top + Math.floor(rand() * glyphH), ink);
        if (rand() < 0.5) for (let dy = 0; dy < glyphH; dy++) set(x + 3 * size, top + dy, ink);
        x += 6 * size;
      }
      x += 5 * size;
    }
  }
  return { width, height, data };
}

/** A bright specular highlight: clipped core with a soft halo. */
export function withGlare(img: GrayImage, cx: number, cy: number, rx: number, ry: number): GrayImage {
  const data = img.data.slice();
  for (let y = 0; y < img.height; y++) {
    for (let x = 0; x < img.width; x++) {
      const d = ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2;
      const i = y * img.width + x;
      if (d <= 1) data[i] = 255;
      else if (d <= 2.2) data[i] = Math.max(data[i]!, Math.round(255 - (d - 1) * 30));
    }
  }
  return { ...img, data };
}

export function scale(img: GrayImage, factor: number, offset = 0): GrayImage {
  return { ...img, data: Uint8Array.from(img.data, (v) => Math.max(0, Math.min(255, Math.round(v * factor + offset)))) };
}

/** Add mild sensor noise, as every real photo has. */
export function noisy(img: GrayImage, amplitude: number, seed = 7): GrayImage {
  const rand = prng(seed);
  return { ...img, data: Uint8Array.from(img.data, (v) => Math.max(0, Math.min(255, Math.round(v + (rand() - 0.5) * 2 * amplitude)))) };
}

/** Separable Gaussian blur with standard deviation `sigma` pixels (edges replicated). */
export function gaussian(img: GrayImage, sigma: number): GrayImage {
  if (sigma <= 0) return img;
  const radius = Math.ceil(sigma * 3);
  const kernel = new Float64Array(2 * radius + 1);
  let total = 0;
  for (let d = -radius; d <= radius; d++) {
    const k = Math.exp(-(d * d) / (2 * sigma * sigma));
    kernel[d + radius] = k;
    total += k;
  }
  for (let i = 0; i < kernel.length; i++) kernel[i] = kernel[i]! / total;
  const { width: w, height: h } = img;
  // Convolve each line of `len` samples read with `stride`, writing into `out`.
  const line = (src: Float64Array, out: Float64Array, start: number, stride: number, len: number) => {
    const padded = new Float64Array(len + 2 * radius);
    for (let i = 0; i < padded.length; i++) {
      const j = Math.min(len - 1, Math.max(0, i - radius));
      padded[i] = src[start + j * stride]!;
    }
    for (let i = 0; i < len; i++) {
      let s = 0;
      for (let k = 0; k < kernel.length; k++) s += padded[i + k]! * kernel[k]!;
      out[start + i * stride] = s;
    }
  };
  const src = Float64Array.from(img.data);
  const mid = new Float64Array(w * h);
  for (let y = 0; y < h; y++) line(src, mid, y * w, 1, w);
  const out = new Float64Array(w * h);
  for (let x = 0; x < w; x++) line(mid, out, x, w, h);
  return { width: w, height: h, data: Uint8Array.from(out, (v) => Math.round(v)) };
}
