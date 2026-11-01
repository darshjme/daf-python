"""Compare real Python and Rust command-adapter processes without provider calls."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument('--rust', required=True)
parser.add_argument('--report', required=True)
args = parser.parse_args()
python_command = [sys.executable, '-m', 'daf_adapter']
rust_command = [str(Path(args.rust).resolve())]
env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
checks = []
def invoke(command, value):
    raw = value if isinstance(value, str) else json.dumps(value)
    proc = subprocess.run(command + ['run', '-'], input=raw, text=True, capture_output=True, env=env, timeout=8)
    return proc.returncode, json.loads(proc.stdout) if proc.stdout else None

def check(name, value, invalid=False, platform_error=False):
    py, rust = [invoke(command, value) for command in (python_command, rust_command)]
    if invalid:
        assert py == rust == (2, None), (name, py, rust)
    elif platform_error:
        assert py[0] == rust[0] == 1, (name, py, rust)
        for result in (py[1], rust[1]):
            assert result['status'] == 'failed' and result['exit_code'] is None
    else:
        assert py == rust, (name, py, rust)
    checks.append({'name': name, 'status': 'passed'})

def req(code, **kw):
    return dict(schema_version=1, id='conformance', argv=[sys.executable, '-c', code], **kw)

caps = [json.loads(subprocess.check_output(command + ['capabilities'], env=env)) for command in (python_command, rust_command)]
assert caps[0] == caps[1], caps
checks.append({'name':'capabilities equality','status':'passed'})
check('success', req("print('hello')"))
check('literal argv', dict(schema_version=1,id='args',argv=[sys.executable,'-c','import sys;print(sys.argv[1])','$(echo unsafe); *']))
check('stdin', req('import sys;print(sys.stdin.read())', stdin='input\nλ'))
check('exit7', req('raise SystemExit(7)'))
check('dual output overflow', req("import os;os.write(1,b'x'*200000);os.write(2,b'y'*200000)",max_output_bytes=19))
check('lossy output', req('import os;os.write(1,bytes([255,226,130,172]))',max_output_bytes=3))
check('timeout', req('import time;time.sleep(3)',timeout_ms=40))
check('missing executable',dict(schema_version=1,id='missing',argv=['/nonexistent/daf-command']), platform_error=True)
with tempfile.TemporaryDirectory() as d:
    check('cwd',req('import os;print(os.getcwd())',cwd=d))
for name,key,value in [('wrong version','schema_version',2),('bool version','schema_version',True),('empty id','id',''),('blank id','id','  '),('UTF8 id','id','é'*65),('no argv','argv',[]),('NUL argv','argv',['x\0y']),('zero timeout','timeout_ms',0),('bool timeout','timeout_ms',True),('float timeout','timeout_ms',1.5),('oversize cap','max_output_bytes',1048577),('unknown field','extra',1),('bad cwd','cwd','/nonexistent/daf-cwd'),('empty cwd','cwd','')]:
    v=req('');v[key]=value;check(name,v,invalid=True)
check('duplicate fields','{"schema_version":1,"id":"a","id":"b","argv":["echo"]}',invalid=True)
check('oversize input',req('',stdin='x'*1048576),invalid=True)
Path(args.report).write_text(json.dumps({'checks':checks,'count':len(checks),'status':'passed','limits':'Local command protocol conformance; no live model provider calls.'},indent=2)+'\n')
print(json.dumps({'status':'passed','checks':len(checks)}))
