"""Check shipped browser scripts, including their shared classic-script scope."""
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from app.support_ui import _ASSETS, _SCRIPT_NAMES, _configure

for name, source in _ASSETS.items():
    if name.endswith('.js'):
        subprocess.run(['node', '--check'], input=_configure(source), text=True, check=True)
subprocess.run(['node', '--check'], input='\n'.join(_configure(_ASSETS[name]) for name in _SCRIPT_NAMES),
               text=True, check=True)
subprocess.run(['node', '--check', str(root / 'app/support_image_editor.js')], check=True)
print('Support JavaScript syntax and combined scope: passed')
