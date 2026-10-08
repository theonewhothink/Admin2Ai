// The bridge between the browser's OCR and the Python engine (lib/ocr-bridge.ts).
// Run: npm run test:ocr   (node --test; Node 22 strips the TypeScript types itself)
//
// What tesseract.js, jsQR and pdf.js give is turned into the engine's device
// reading (backend/src/backoffice/reading/browser.py checks the same format;
// backend/tests/test_reading_browser.py sends it through the real engine).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import {
  EDGE,
  WIRE_METHOD,
  deviceReading,
  edgeSharpness,
  greyFromRgba,
  hasTextLayer,
  pageFromTesseract,
  qrCodes,
  textFromItems,
} from "../lib/ocr-bridge.ts";

const RECEIPT = new URL("../../backend/tests/fixtures/photos/fs-pb2026-0441.txt", import.meta.url);
const QR = "A:509882412*B:516123459*C:PT*D:FS*E:N*F:20260926*G:FS PB2026/441*H:KPB4M8XT-441*I1:PT*I5:7.43*I6:0.97*N:0.97*O:8.40*Q:Hq3z*R:1187";

/** tesseract.js output for the café receipt fixture, plus the noise it reads off the QR code. */
function tesseractBlocks() {
  const printed = readFileSync(RECEIPT, "utf8").split("\n").filter((l) => l.trim());
  const lines = printed.map((text, i) => ({ text: `${text}\n`, confidence: 91 + (i % 5), bbox: { x0: 200, y0: 160 + 66 * i, x1: 900, y1: 200 + 66 * i } }));
  lines.push({ text: "[5] amr? [5]", confidence: 23, bbox: { x0: 413, y0: 1449, x1: 787, y1: 1515 } });
  lines.push({ text: " ; ", confidence: 88, bbox: { x0: 413, y0: 1520, x1: 420, y1: 1530 } });
  lines.push({ text: "no box", confidence: 90, bbox: { x0: 10, y0: 10, x1: 10, y1: 20 } });
  return [{ paragraphs: [{ lines: lines.slice(0, 9) }, { lines: lines.slice(9) }] }];
}

test("a receipt read by tesseract.js becomes the engine's reading: the sure lines, scored 0-1, with boxes", () => {
  const page = pageFromTesseract(1, 1200, 2003, tesseractBlocks());
  const texts = page.lines.map((l) => l.text);
  assert.equal(texts[0], "PASTELARIA DO BOLHÃO");
  assert.ok(texts.includes("TOTAL           8,40 EUR".replace(/\s+/g, " ")));
  assert.ok(texts.includes("NIF: 509882412"));
  assert.ok(!texts.some((t) => t.includes("amr?") || t === ";" || t === "no box"), "QR noise, punctuation and empty boxes are dropped");
  for (const line of page.lines) {
    assert.ok(line.confidence > 0.9 && line.confidence <= 1);
    assert.equal(line.box.length, 4);
    assert.ok(line.box[2] > line.box[0] && line.box[3] > line.box[1]);
  }
  const reading = deviceReading({ engine: "tesseract.js", version: "7.0.0 por+eng", pages: [page], qr: [QR, QR, null, ""], sharpness: 0.97, ms: 2345.6 });
  assert.equal(reading.method, WIRE_METHOD);
  assert.deepEqual(reading.qr, [QR]);
  assert.equal(reading.ms, 2346);
  assert.equal(reading.pages[0].number, 1);
  assert.ok(!("textLayer" in reading) && !("metadata" in reading));
  // what the upload sends: JSON the engine parses (reading/browser.py DeviceReading.from_json)
  const sent = JSON.parse(JSON.stringify({ filename: "receipt.jpg", reading }));
  assert.equal(sent.reading.pages[0].lines.length, page.lines.length);
});

test("a PDF: its own text decides whether it was scanned; metadata keeps only what the engine reads", () => {
  const items = [{ str: "Fatura n.º FT EDP2026/558120", hasEOL: true }, { str: "Total: 64,10 €" }, { str: "", hasEOL: true }];
  assert.equal(textFromItems(items), "Fatura n.º FT EDP2026/558120\nTotal: 64,10 €\n");
  assert.ok(hasTextLayer([textFromItems(items)]));
  assert.ok(!hasTextLayer(["", "  \n 12 "]), "a scan: fewer than 20 characters of text");
  const reading = deviceReading({
    engine: "pdf.js", version: "6.3.289", pages: [], qr: [], textLayer: [textFromItems(items)],
    metadata: { Producer: "ReportLab PDF Library", Subject: QR, CreationDate: "D:2026", Trapped: { name: "False" } }, ms: 10,
  });
  assert.deepEqual(Object.keys(reading.metadata), ["Producer", "Subject"]);
  assert.equal(reading.sharpness, null);
});

test("QR codes: no blanks, no repeats, at most eight", () => {
  assert.deepEqual(qrCodes([" A ", "A", null, undefined, "", "B"]), ["A", "B"]);
  assert.equal(qrCodes(Array.from({ length: 20 }, (_, i) => `code ${i}`)).length, 8);
});

/** A synthetic page: dark strokes on paper, optionally blurred by a box filter of the given radius. */
function page(radius) {
  const width = 600;
  const height = 400;
  let grey = new Float32Array(width * height).fill(245);
  for (let y = 40; y < 360; y += 40) {
    for (let x = 30; x < 570; x += 14) {
      for (let dy = 0; dy < 16; dy += 1) for (let dx = 0; dx < 3; dx += 1) grey[(y + dy) * width + x + dx] = 25;
    }
  }
  for (let pass = 0; pass < radius; pass += 1) {
    const next = new Float32Array(grey.length);
    for (let y = 0; y < height; y += 1) {
      for (let x = 0; x < width; x += 1) {
        let sum = 0;
        let n = 0;
        for (let dy = -1; dy <= 1; dy += 1) for (let dx = -1; dx <= 1; dx += 1) {
          const yy = y + dy;
          const xx = x + dx;
          if (yy >= 0 && yy < height && xx >= 0 && xx < width) { sum += grey[yy * width + xx]; n += 1; }
        }
        next[y * width + x] = sum / n;
      }
    }
    grey = next;
  }
  return { grey, width, height };
}

test("edge sharpness: crisp print is near 1, a blurred page falls under the server's threshold, a blank page is not measured", () => {
  const crisp = page(0);
  const soft = page(2);
  const blurred = page(6);
  const score = (p) => edgeSharpness(p.grey, p.width, p.height);
  assert.equal(score(crisp), 1);
  assert.ok(score(soft) > 0.32 && score(soft) < 1, `soft ${score(soft)}`);
  assert.ok(score(blurred) < 0.32, `blurred ${score(blurred)}`); // QualityThresholds.min_edge_sharpness
  assert.equal(edgeSharpness(new Float32Array(600 * 400).fill(250), 600, 400), null);
  assert.equal(EDGE.side, 600);
});

test("greyscale is ITU-R 601 luma, as the server's Pillow 'L'", () => {
  const rgba = new Uint8ClampedArray([255, 0, 0, 255, 0, 255, 0, 255, 0, 0, 255, 255, 200, 200, 200, 255]);
  assert.deepEqual(Array.from(greyFromRgba(rgba, 4, 1)).map((v) => Math.round(v * 10) / 10), [76.2, 149.7, 29.1, 200]);
});
