import hashlib
from pathlib import Path
import re
import zipfile


root = Path(__file__).resolve().parents[1]
version = (root / "VERSION").read_text().strip()
if not re.fullmatch(r"\d+\.\d+\.\d+", version):
    raise ValueError("VERSION must contain a semantic version such as 0.1.0.")
output = root / "dist" / f"comfyui-papan-v{version}.zip"
output.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for name in ("__init__.py", "board.py", "nodes.py", "requirements.txt", "web/papan.js", "web/papan.css", "README.md", "LICENSE", "VERSION", "assets/screenshot.png", "examples/Papan_References.json", "examples/demo.papan.zip"):
        archive.write(root / name, f"comfyui-papan/{name}")
digest = hashlib.sha256(output.read_bytes()).hexdigest()
output.with_suffix(".zip.sha256").write_text(f"{digest}  {output.name}\n", encoding="utf-8")
print(output)
