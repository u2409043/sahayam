// Build Sahayam for offline use: compile JSX once, bundle React, Leaflet and fonts locally.
// Outputs:  ../frontend      (served by the FastAPI backend, config: same server)
//           ../mobile/www    (the Android app, config: mobile/config.js -> apiBase)
import { transform } from "esbuild";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const nm = (...p) => path.join(here, "node_modules", ...p);
const src = (...p) => path.join(here, "src", ...p);

const jsx = fs.readFileSync(src("app.jsx"), "utf8");
const { code } = await transform(jsx, {
  loader: "jsx", jsxFactory: "React.createElement", jsxFragment: "React.Fragment",
  target: "es2019", minify: true, legalComments: "none",
});

// Fonts: only the weights and scripts the app uses (Latin + Malayalam)
const FONT_FILES = [
  ["manrope", ["500", "600", "700", "800"]],
  ["noto-sans-malayalam", ["400", "500", "600", "700", "800"]],
  ["ibm-plex-mono", ["500"]],
];
const KEEP_SUBSETS = /(latin|malayalam)/;
let fontCss = "";
const fontAssets = new Set();
for (const [pkg, weights] of FONT_FILES) {
  for (const w of weights) {
    const css = fs.readFileSync(nm("@fontsource", pkg, `${w}.css`), "utf8");
    for (const block of css.split("@font-face").slice(1)) {
      const face = "@font-face" + block.slice(0, block.indexOf("}") + 1);
      const m = face.match(/url\(\.\/files\/([^)]+\.woff2)\)/);
      if (!m || !KEEP_SUBSETS.test(m[1]) || /latin-ext/.test(m[1]) && pkg === "ibm-plex-mono") continue;
      fontAssets.add(`${pkg}/${m[1]}`);
      fontCss += face
        .replace(/src:[^;]+;/, `src: url(${pkg}/${m[1]}) format('woff2');`)
        .replace(/'?(Manrope|Noto Sans Malayalam|IBM Plex Mono)'?/, (x, n) => `'${n}'`) + "\n";
    }
  }
}

function emit(outDir, configJs) {
  fs.rmSync(outDir, { recursive: true, force: true });
  fs.mkdirSync(path.join(outDir, "vendor"), { recursive: true });
  fs.mkdirSync(path.join(outDir, "fonts"), { recursive: true });
  fs.copyFileSync(src("index.html"), path.join(outDir, "index.html"));
  fs.copyFileSync(src("styles.css"), path.join(outDir, "styles.css"));
  if (fs.existsSync(src("icon.png"))) fs.copyFileSync(src("icon.png"), path.join(outDir, "icon.png"));
  fs.writeFileSync(path.join(outDir, "app.js"), code);
  fs.writeFileSync(path.join(outDir, "config.js"), configJs);
  fs.copyFileSync(nm("react", "umd", "react.production.min.js"), path.join(outDir, "vendor", "react.production.min.js"));
  fs.copyFileSync(nm("react-dom", "umd", "react-dom.production.min.js"), path.join(outDir, "vendor", "react-dom.production.min.js"));
  fs.copyFileSync(nm("leaflet", "dist", "leaflet.js"), path.join(outDir, "vendor", "leaflet.js"));
  fs.writeFileSync(path.join(outDir, "fonts", "fonts.css"), fontCss);
  for (const f of fontAssets) {
    const [pkg, file] = f.split("/");
    fs.mkdirSync(path.join(outDir, "fonts", pkg), { recursive: true });
    fs.copyFileSync(nm("@fontsource", pkg, "files", file), path.join(outDir, "fonts", pkg, file));
  }
}

// Web build served by the backend: talk to the same server it came from.
emit(path.join(here, "..", "frontend"), "// Served by the Sahayam backend: use the same server.\nwindow.SAHAYAM_CONFIG = { apiBase: \"\" };\n");

// Android build: the server address comes from mobile/config.js (edit that file, then rebuild).
const mobileCfg = path.join(here, "..", "mobile", "config.js");
const cfg = fs.existsSync(mobileCfg) ? fs.readFileSync(mobileCfg, "utf8")
  : "window.SAHAYAM_CONFIG = { apiBase: \"http://192.168.1.20:8000\" };\n";
emit(path.join(here, "..", "mobile", "www"), cfg);

const kb = (n) => (n / 1024).toFixed(0) + " KB";
console.log(`Built app.js ${kb(code.length)}, ${fontAssets.size} font files -> frontend/ and mobile/www/`);
