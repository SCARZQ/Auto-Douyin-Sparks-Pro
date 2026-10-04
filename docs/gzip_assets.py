"""为静态资源生成 .gz 预压缩文件（部署时跑一次即可）。

收益（实测比例）：
  element-plus.css        357 KB -> 约 42 KB
  element-plus.full.min.js 1042 KB -> 约 260 KB
  vue.global.prod.js      166 KB -> 约 60 KB
  axios.min.js             65 KB -> 约 22 KB
  index.html              464 KB -> 约 90 KB
"""
import gzip
import pathlib
import shutil

# 自动定位：本脚本在 docs/ 下，static 在项目根目录
STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"
EXTS = {".js", ".css", ".html", ".svg", ".json", ".txt", ".map"}
MIN_SIZE = 1024          # 小于 1KB 不值得压缩
LEVEL = 9

total_raw = 0
total_gz = 0
count = 0

for f in sorted(STATIC.rglob("*")):
    if not f.is_file():
        continue
    if f.suffix.lower() not in EXTS:
        continue
    if f.name.endswith(".gz"):
        continue
    size = f.stat().st_size
    if size < MIN_SIZE:
        continue

    raw = f.read_bytes()
    gz_path = f.with_suffix(f.suffix + ".gz")
    with gzip.GzipFile(filename="", mode="wb", fileobj=open(gz_path, "wb"), compresslevel=LEVEL) as gz:
        gz.write(raw)

    gz_size = gz_path.stat().st_size
    total_raw += size
    total_gz += gz_size
    count += 1
    print("%-42s %8s -> %8s  (%d%%)" % (
        str(f.relative_to(STATIC)), f"{size:,}", f"{gz_size:,}",
        round(gz_size * 100 / size)))

print()
print("压缩 %d 个文件" % count)
print("原始合计 : %s 字节" % f"{total_raw:,}")
print("压缩合计 : %s 字节" % f"{total_gz:,}")
if total_raw:
    print("整体节省 : %d%%" % round((1 - total_gz / total_raw) * 100))
