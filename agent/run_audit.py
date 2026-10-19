"""Launch the audit bridge from a private receipt; no keys in command arguments."""
import json
import os
from pathlib import Path
import runpy
import stat
import sys
if len(sys.argv)!=2:raise SystemExit('Pass a private connect-audit receipt path.')
p=Path(sys.argv[1]);mode=stat.S_IMODE(p.stat().st_mode)
if mode & 0o077:raise SystemExit('Audit receipt must be private: chmod 600 the receipt file.')
value=json.loads(p.read_text())
if value.get('purpose')!='audit':raise SystemExit('An audit-purpose key receipt is required.')
os.environ.update(NATIVE_ANALYTICS_URL=value['origin'],AFTERSPAN_PROJECT=value['project'],AFTERSPAN_AUDIT_KEY=value['read_key'])
sys.path.insert(0,str(Path(__file__).resolve().parent))
runpy.run_path(str(Path(__file__).with_name('audit_server.py')),run_name='__main__')
