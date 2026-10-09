"""Build a standalone plugin release without the base application."""
import hashlib
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
root = Path(__file__).resolve().parent.parent
out = root / 'dist'
out.mkdir(exist_ok=True)
archive = out / 'plugin.zip'
with ZipFile(archive, 'w', ZIP_DEFLATED) as bundle:
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if path.is_file() and not any(part in ('.git', '.github', 'dist', '__pycache__', 'tests', 'tools', '.venv') for part in relative.parts):
            bundle.write(path, str(relative))
(out / 'SHA256SUMS').write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + '  plugin.zip\n')
print(archive)
