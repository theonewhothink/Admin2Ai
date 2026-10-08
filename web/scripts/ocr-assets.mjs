#!/usr/bin/env node
/*
 * Put the in-browser reading engines under public/ocr/ (static demo only).
 *
 * The static site reads photos and scanned PDFs in the visitor's browser
 * (lib/ocr.ts). Everything it needs is self-hosted next to the site, copied
 * from the exact package versions in package-lock.json, so GitHub Pages
 * serves it and no third-party CDN is ever contacted. Nothing here is loaded
 * until a visitor uploads a photo or a PDF.
 *
 *   tesseract/           tesseract.js (ESM build + its worker), Apache-2.0
 *   tesseract/core/      the WebAssembly OCR core, LSTM builds only (relaxed SIMD, SIMD, plain:
 *                        the browser loads the one it supports), Apache-2.0
 *   tesseract/lang/      Portuguese and English LSTM models (tessdata_best, integer), Apache-2.0
 *   pdfjs/               pdf.js (its "legacy" build, for browsers of the last few years) and its
 *                        worker, plus the image decoders scans use (JBIG2, JPEG 2000, colour), Apache-2.0
 *
 * Usage: node scripts/ocr-assets.mjs [dest-dir]   (default: public/ocr; never committed)
 */
import { copyFileSync, mkdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const web = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const dest = resolve(process.argv[2] ?? join(web, "public", "ocr"));
const require = createRequire(join(web, "package.json"));
const pkg = (name) => dirname(require.resolve(`${name}/package.json`));
const version = (name) => require(`${name}/package.json`).version;

const tesseract = pkg("tesseract.js");
const core = pkg("tesseract.js-core");
const pdfjs = pkg("pdfjs-dist");
const files = [
  [join(tesseract, "dist/tesseract.esm.min.js"), "tesseract/tesseract.esm.min.js"],
  [join(tesseract, "dist/worker.min.js"), "tesseract/worker.min.js"],
  [join(tesseract, "LICENSE.md"), "tesseract/LICENSE.md", true],
  [join(core, "LICENSE"), "tesseract/core/LICENSE"],
  ...["relaxedsimd-lstm", "simd-lstm", "lstm"].map((v) => [join(core, `tesseract-core-${v}.wasm.js`), `tesseract/core/tesseract-core-${v}.wasm.js`]),
  ...["por", "eng"].map((lang) => [join(pkg(`@tesseract.js-data/${lang}`), "4.0.0_best_int", `${lang}.traineddata.gz`), `tesseract/lang/${lang}.traineddata.gz`]),
  // The legacy build: the modern one needs JavaScript features (Map.getOrInsertComputed) browsers ship only now.
  [join(pdfjs, "legacy/build/pdf.min.mjs"), "pdfjs/pdf.min.mjs"],
  [join(pdfjs, "legacy/build/pdf.worker.min.mjs"), "pdfjs/pdf.worker.min.mjs"],
  [join(pdfjs, "LICENSE"), "pdfjs/LICENSE"],
  ...["jbig2.wasm", "openjpeg.wasm", "qcms_bg.wasm", "LICENSE_JBIG2", "LICENSE_OPENJPEG", "LICENSE_QCMS"].map((f) => [join(pdfjs, "wasm", f), `pdfjs/wasm/${f}`]),
];

rmSync(dest, { recursive: true, force: true });
let bytes = 0;
for (const [from, to, optional] of files) {
  let size;
  try {
    size = statSync(from).size;
  } catch {
    if (optional) continue;
    throw new Error(`missing ${from}: run npm ci first`);
  }
  mkdirSync(dirname(join(dest, to)), { recursive: true });
  copyFileSync(from, join(dest, to));
  bytes += size;
}
const versions = { tesseract: version("tesseract.js"), core: version("tesseract.js-core"), pdfjs: version("pdfjs-dist") };
writeFileSync(join(dest, "versions.json"), JSON.stringify(versions, null, 2) + "\n");
console.log(`OCR assets in ${dest}: ${(bytes / 1024 / 1024).toFixed(1)} MB (tesseract.js ${versions.tesseract}, core ${versions.core}, pdf.js ${versions.pdfjs})`);
