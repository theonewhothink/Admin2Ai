/**
 * Page quality checks for the camera (§11: glare warning, blur detection).
 *
 * Pure functions over a grayscale image, so they run the same on the phone and
 * in tests. Results are advisory: they only offer "retake?", never block a
 * capture, and never replace server-side checks (§14 quality validation).
 *
 * Blur: for each tile that contains detail (text, lines), the energy of the
 * Laplacian is divided by the tile's intensity variance. The ratio does not
 * depend on exposure or ink contrast and falls sharply as edges soften. The page
 * score is the median over detailed tiles, so blank margins do not count.
 *
 * Glare: a specular highlight is a compact, clipped (near-white) blob on paper
 * that is otherwise not clipped. Pages whose background is already clipped
 * (aggressively whitened scans) cannot be judged and are left alone.
 *
 * Thresholds were chosen on synthetic pages (see __tests__/quality.test.ts) and
 * are deliberately conservative: a missed warning costs little because the
 * server re-checks every page; a false warning annoys the owner every time.
 */
import type { QualityIssue } from "../offline/types";

export interface GrayImage {
  width: number;
  height: number;
  /** Row-major luminance, 0..255. */
  data: Uint8Array;
}

export interface QualityMetrics {
  /** Median contrast-normalised Laplacian energy over detailed tiles; null if too little detail. */
  sharpness: number | null;
  detailTiles: number;
  meanLuma: number;
  medianLuma: number;
  /** Share of the page covered by the largest clipped highlight blob, 0..1. */
  glareShare: number;
}

export interface QualityThresholds {
  /** Below this sharpness the page reads as blurry. */
  minSharpness: number;
  /** Tile standard deviation that marks a tile as containing detail. */
  detailStdDev: number;
  /** Minimum detailed tiles needed before judging blur. */
  minDetailTiles: number;
  /** Luma at or above which a pixel counts as clipped. */
  clipLuma: number;
  /** Share of clipped pixels that makes a glare cell. */
  clippedCellShare: number;
  /** Highlight blob share of the page that triggers a glare warning. */
  minGlareShare: number;
  /** Larger blobs are the page itself (white-balanced scans), not glare. */
  maxGlareShare: number;
  /** Paper this bright is already clipped: glare cannot be told apart. */
  maxBackgroundLuma: number;
  /** Below this mean luma the page is too dark to read. */
  minMeanLuma: number;
}

export const DEFAULT_THRESHOLDS: QualityThresholds = {
  minSharpness: 0.12,
  detailStdDev: 5,
  minDetailTiles: 6,
  clipLuma: 250,
  clippedCellShare: 0.6,
  minGlareShare: 0.015,
  maxGlareShare: 0.5,
  maxBackgroundLuma: 244,
  minMeanLuma: 55,
};

const TILE = 32;
const CELL = 16;

/** Rec. 601 luma from RGBA bytes, integer arithmetic. */
export function rgbaToGray(rgba: Uint8Array, width: number, height: number): GrayImage {
  const n = width * height;
  if (rgba.length < n * 4) throw new Error("rgba buffer too small");
  const data = new Uint8Array(n);
  for (let i = 0, j = 0; i < n; i++, j += 4) {
    data[i] = ((299 * (rgba[j] as number) + 587 * (rgba[j + 1] as number) + 114 * (rgba[j + 2] as number)) / 1000) | 0;
  }
  return { width, height, data };
}

function median(values: number[]): number {
  if (values.length === 0) return 0;
  const sorted = values.slice().sort((a, b) => a - b);
  const mid = sorted.length >> 1;
  return sorted.length % 2 ? (sorted[mid] as number) : ((sorted[mid - 1] as number) + (sorted[mid] as number)) / 2;
}

/** Value at quantile q (0..1) of an unsorted list. */
function quantile(values: number[], q: number): number {
  if (values.length === 0) return 0;
  const sorted = values.slice().sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))] as number;
}

/**
 * Per-tile Laplacian energy over intensity variance, corrected for sensor noise.
 * The Laplacian of independent noise with variance s2 has energy 20 * s2, so the
 * noise estimated from the flattest tiles is removed from both terms.
 */
function sharpnessOf(img: GrayImage, t: QualityThresholds): { sharpness: number | null; detailTiles: number } {
  const { width: w, height: h, data } = img;
  const tx = Math.max(1, Math.floor(w / TILE));
  const ty = Math.max(1, Math.floor(h / TILE));
  const sum = new Float64Array(tx * ty);
  const sumSq = new Float64Array(tx * ty);
  const lapSq = new Float64Array(tx * ty);
  const count = new Float64Array(tx * ty);
  for (let y = 1; y < h - 1; y++) {
    const tyIdx = Math.min(ty - 1, Math.floor(y / TILE)) * tx;
    for (let x = 1; x < w - 1; x++) {
      const i = y * w + x;
      const v = data[i] as number;
      const lap = 4 * v - (data[i - 1] as number) - (data[i + 1] as number) - (data[i - w] as number) - (data[i + w] as number);
      const k = tyIdx + Math.min(tx - 1, Math.floor(x / TILE));
      sum[k] = (sum[k] as number) + v;
      sumSq[k] = (sumSq[k] as number) + v * v;
      lapSq[k] = (lapSq[k] as number) + lap * lap;
      count[k] = (count[k] as number) + 1;
    }
  }
  const tiles: Array<{ variance: number; energy: number }> = [];
  for (let k = 0; k < count.length; k++) {
    const n = count[k] as number;
    if (n === 0) continue;
    const mean = (sum[k] as number) / n;
    tiles.push({ variance: Math.max(0, (sumSq[k] as number) / n - mean * mean), energy: (lapSq[k] as number) / n });
  }
  const noiseVar = quantile(
    tiles.map((tile) => tile.variance),
    0.1,
  );
  const minVar = t.detailStdDev * t.detailStdDev;
  const scores: number[] = [];
  for (const tile of tiles) {
    const signalVar = tile.variance - noiseVar;
    if (signalVar < minVar) continue;
    scores.push(Math.max(0, tile.energy - 20 * noiseVar) / signalVar);
  }
  return {
    sharpness: scores.length >= t.minDetailTiles ? median(scores) : null,
    detailTiles: scores.length,
  };
}

/** Largest 4-connected blob of clipped cells, as a share of all cells. */
function largestHighlightShare(img: GrayImage, t: QualityThresholds): number {
  const { width: w, height: h, data } = img;
  const cx = Math.max(1, Math.floor(w / CELL));
  const cy = Math.max(1, Math.floor(h / CELL));
  const clipped = new Uint32Array(cx * cy);
  const total = new Uint32Array(cx * cy);
  for (let y = 0; y < cy * CELL && y < h; y++) {
    const row = Math.floor(y / CELL) * cx;
    for (let x = 0; x < cx * CELL && x < w; x++) {
      const k = row + Math.floor(x / CELL);
      total[k] = (total[k] as number) + 1;
      if ((data[y * w + x] as number) >= t.clipLuma) clipped[k] = (clipped[k] as number) + 1;
    }
  }
  const hot = new Uint8Array(cx * cy);
  for (let k = 0; k < hot.length; k++) {
    hot[k] = (total[k] as number) > 0 && (clipped[k] as number) / (total[k] as number) >= t.clippedCellShare ? 1 : 0;
  }
  let best = 0;
  const seen = new Uint8Array(cx * cy);
  const stack: number[] = [];
  for (let start = 0; start < hot.length; start++) {
    if (!hot[start] || seen[start]) continue;
    let size = 0;
    stack.push(start);
    seen[start] = 1;
    while (stack.length) {
      const k = stack.pop() as number;
      size++;
      const x = k % cx;
      const neighbours = [x > 0 ? k - 1 : -1, x < cx - 1 ? k + 1 : -1, k - cx, k + cx];
      for (const nb of neighbours) {
        if (nb >= 0 && nb < hot.length && hot[nb] && !seen[nb]) {
          seen[nb] = 1;
          stack.push(nb);
        }
      }
    }
    best = Math.max(best, size);
  }
  return best / (cx * cy);
}

function lumaStats(img: GrayImage): { mean: number; median: number } {
  const hist = new Uint32Array(256);
  let total = 0;
  for (const v of img.data) {
    hist[v] = (hist[v] as number) + 1;
    total += v;
  }
  const n = img.data.length || 1;
  let acc = 0;
  let med = 0;
  for (let v = 0; v < 256; v++) {
    acc += hist[v] as number;
    if (acc * 2 >= n) {
      med = v;
      break;
    }
  }
  return { mean: total / n, median: med };
}

export function measureQuality(img: GrayImage, t: QualityThresholds = DEFAULT_THRESHOLDS): QualityMetrics {
  if (img.width < 3 || img.height < 3 || img.data.length < img.width * img.height) {
    throw new Error("image too small to measure");
  }
  const { sharpness, detailTiles } = sharpnessOf(img, t);
  const { mean, median: med } = lumaStats(img);
  return { sharpness, detailTiles, meanLuma: mean, medianLuma: med, glareShare: largestHighlightShare(img, t) };
}

/** Plain issues for the UI. Blur is not judged on a page that is too dark. */
export function assessQuality(m: QualityMetrics, t: QualityThresholds = DEFAULT_THRESHOLDS): QualityIssue[] {
  const issues: QualityIssue[] = [];
  const tooDark = m.meanLuma < t.minMeanLuma;
  if (tooDark) issues.push("too_dark");
  if (!tooDark && m.sharpness !== null && m.sharpness < t.minSharpness) issues.push("blurry");
  if (m.medianLuma <= t.maxBackgroundLuma && m.glareShare >= t.minGlareShare && m.glareShare <= t.maxGlareShare) {
    issues.push("glare");
  }
  return issues;
}
