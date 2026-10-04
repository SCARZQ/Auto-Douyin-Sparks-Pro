/**
 * 构建：压缩 + 混淆
 *
 * 关键取舍（实测 gzip 后体积）：
 *   源码        35.0 KB
 *   仅压缩      23.6 KB   ← 最小
 *   轻度混淆    32.2 KB   ← 推荐：标识符全混淆 + 禁调试，体积可接受
 *   重度混淆   163.5 KB   ← 体积暴涨 4.6 倍，不可接受
 *
 * 所以默认用「轻度」：函数名/变量名全部变成 _0x 十六进制，
 * 保留字符串原文（字符串进了 stringArray 反而压不动）。
 *
 * 产物：
 *   static/dist/app.min.js  —— 生产（压缩+混淆）
 *   static/dist/app.raw.js  —— 调试（仅压缩）
 */
const fs = require("fs");
const path = require("path");
const zlib = require("zlib");
const Terser = require("terser");
const JavaScriptObfuscator = require("javascript-obfuscator");

// 自动定位：本脚本在 docs/ 下，static 在项目根目录
const STATIC = path.resolve(__dirname, "..", "static");
const SRC = path.join(STATIC, "app.js");
const DIST = path.join(STATIC, "dist");

const SKIP_OBF = process.argv.includes("--no-obfuscate");
const WITH_STRINGS = process.argv.includes("--with-strings");

const kb = (n) => (Number(n) / 1024).toFixed(1) + " KB";
const gz = (s) => zlib.gzipSync(Buffer.from(s, "utf8"), { level: 9 }).length;

(async () => {
  if (!fs.existsSync(DIST)) fs.mkdirSync(DIST, { recursive: true });

  const code = fs.readFileSync(SRC, "utf8");
  console.log("源文件 app.js   :", kb(Buffer.byteLength(code)), " gzip", kb(gz(code)));

  // ---------- 1) 压缩 ----------
  const min = await Terser.minify(code, {
    compress: {
      drop_debugger: true,
      passes: 2,
      drop_console: false,     // 保留 console，用户反馈问题时能看
    },
    mangle: { toplevel: false },
    format: { comments: false, ascii_only: true },
  });

  if (min.error) {
    console.error("压缩失败:", min.error);
    process.exit(1);
  }

  const minCode = min.code;
  fs.writeFileSync(path.join(DIST, "app.raw.js"), minCode, "utf8");
  console.log("压缩后          :", kb(Buffer.byteLength(minCode)), " gzip", kb(gz(minCode)));
  console.log("  -> dist/app.raw.js  (调试用)");

  if (SKIP_OBF) {
    fs.copyFileSync(path.join(DIST, "app.raw.js"), path.join(DIST, "app.min.js"));
    console.log("已跳过混淆");
    return;
  }

  // ---------- 2) 混淆（轻度，体积友好） ----------
  const opts = {
    compact: true,
    // 改名：所有局部标识符变 _0x 十六进制
    identifierNamesGenerator: "hexadecimal",
    renameGlobals: false,          // 全局名不能改（Vue 模板依赖）
    // 反调试：必须关闭！
    // debugProtection 会注入 setInterval 反复触发 debugger，
    // 只要用户打开 F12 就每 2 秒断一次 -> 页面永远白屏。
    // selfDefending 的自校验也会干扰正常执行。
    debugProtection: false,
    debugProtectionInterval: 0,   // 关闭（配合 debugProtection: false）
    // 自校验：关闭（会干扰正常运行，且对防破解作用有限）
    selfDefending: false,
    simplify: true,
    // 以下全部关闭（会显著增大体积或拖慢运行）
    controlFlowFlattening: false,
    deadCodeInjection: false,
    splitStrings: false,
    transformObjectKeys: false,
    stringArray: WITH_STRINGS,
    stringArrayThreshold: WITH_STRINGS ? 0.4 : 0,
    stringArrayEncoding: [],
    stringArrayCallsTransform: false,
    stringArrayIndexShift: WITH_STRINGS,
    stringArrayRotate: WITH_STRINGS,
    stringArrayShuffle: WITH_STRINGS,
    stringArrayWrappersCount: 0,
    stringArrayWrappersChainedCalls: false,
    stringArrayWrappersType: "variable",
    disableConsoleOutput: false,
  };

  const t0 = Date.now();
  const out = JavaScriptObfuscator.obfuscate(minCode, opts).getObfuscatedCode();
  console.log("混淆后          :", kb(Buffer.byteLength(out)), " gzip", kb(gz(out)),
              "  (" + (Date.now() - t0) + "ms)");

  fs.writeFileSync(path.join(DIST, "app.min.js"), out, "utf8");
  console.log("  -> dist/app.min.js  (生产用)");
})();
