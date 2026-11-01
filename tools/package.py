import tarfile
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
target = root / 'dist' / 'queuepilot.tar.gz'
target.parent.mkdir(exist_ok=True)
with tarfile.open(target, 'w:gz') as archive:
    for name in ('install.sh', 'bin', 'panel', 'requirements.txt', 'README.md', '.env.example'):
        archive.add(root / name, arcname='queuepilot/' + name,
                    filter=lambda info: None if '__pycache__' in info.name or info.name.endswith('.pyc') else info)
print(target)

github = root / 'dist' / 'queuepilot-github.zip'
sources = ('install.sh', 'bin', 'panel', 'tools', 'tests', 'requirements.txt',
           'requirements-dev.txt', 'README.md', '.env.example', '.gitignore', '.gitattributes')
with zipfile.ZipFile(github, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
    for name in sources:
        path = root / name
        for file in sorted(path.rglob('*')) if path.is_dir() else [path]:
            relative = file.relative_to(root)
            if (not file.is_file() or '__pycache__' in relative.parts or file.suffix == '.pyc'
                    or file.name in ('.env', 'smtp.key', '.local-access.json')):
                continue
            archive.write(file, relative.as_posix())
print(github)
