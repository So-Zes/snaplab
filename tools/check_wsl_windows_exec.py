"""Windows-observed, model-free setup acceptance for copied PE executables."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time


@contextmanager
def windows_observer(report):
    """Keep the Windows observer alive until the WSL worker has been torn down."""
    with tempfile.TemporaryDirectory(prefix='snaplab-interop-') as tmp:
        marker = Path(tmp) / 'marker.txt'
        try:
            yield marker
        finally:
            time.sleep(1)
            report['late_windows_marker_created'] = marker.exists()
            marker.unlink(missing_ok=True)
            if report['late_windows_marker_created']:
                raise RuntimeError('Delayed Windows execution detected; worker disabled')


def check_windows_exec(run_linux, report, marker):
    """run_linux executes trusted Python as the worker account over the setup SSH connection."""
    results = []
    with tempfile.TemporaryDirectory(prefix='snaplab-cmd-control-') as tmp:
        executable = Path(os.environ['SYSTEMROOT']) / 'System32/cmd.exe'
        payload = base64.b64encode(executable.read_bytes()).decode('ascii')
        executable = Path(tmp) / 'cmd.exe'
        executable.write_bytes(base64.b64decode(payload))
        command = 'echo snaplab-interop-probe>"' + str(marker) + '"'
        # Verify the Windows observer and command quoting before testing denial.
        control = subprocess.run('"' + str(executable) + '" /d /q /c ' + command,
                                 capture_output=True, timeout=5)
        if control.returncode or not marker.exists() or marker.read_bytes().strip() != b'snaplab-interop-probe':
            raise RuntimeError('Windows interop positive control failed')
        marker.unlink()
        report['windows_positive_control'] = True
        report['windows_exec'] = results
        metadata = run_linux("from pathlib import Path\nimport json\np=Path('/proc/sys/fs/binfmt_misc/WSLInterop')\nprint(json.dumps({'registration':p.read_text() if p.exists() else 'absent'}))")
        report['binfmt'] = json.loads(metadata.stdout)['registration']
        for sandbox in (False, True):
            for location in ('workspace', 'tmp'):
                for kind in ('cmd', 'mz'):
                    # Temp trees are private to this invocation and removed on both sides.
                    body = '''import base64,json,pathlib,tempfile
from check_wsl_isolation import probe_executable
with tempfile.TemporaryDirectory(prefix='.snaplab-pe-',dir=DIRECTORY) as tmp:
 p=pathlib.Path(tmp)/'probe.exe'
 p.write_bytes(pathlib.Path('cmd-source').read_bytes() if KIND=='cmd' else b'MZ')
 p.chmod(0o700)
 print(json.dumps(probe_executable([str(p),'/d','/q','/c',COMMAND],timeout=TIMEOUT)))
'''
                    body = ('import sys\nsys.path.insert(0,"/opt/snaplab")\n'
                            + 'DIRECTORY=' + repr('.' if location == 'workspace' else '/tmp') + '\n'
                            + 'KIND=' + repr(kind)
                            + '\nCOMMAND=' + repr(command) + '\nTIMEOUT=' + repr(2 if sandbox else 12) + '\n' + body)
                    code = '''import os,sys,pathlib,tempfile,subprocess,base64
sys.path.insert(0,'/opt/snaplab')
from remote_worker import isolated_command
from wsl_runtime import outer
home=pathlib.Path.home()
with tempfile.TemporaryDirectory(prefix='interop-',dir=home/'jobs') as tmp:
 folder=pathlib.Path(tmp)
 config=folder/'config'; config.mkdir()
 os.chdir(folder)
 (folder/'cmd-source').write_bytes(base64.b64decode(PAYLOAD))
 argv=['/usr/bin/python3','-I','-c',BODY]
 if SANDBOX:
  raise SystemExit(outer(isolated_command(argv,folder,home,config)))
 raise SystemExit(subprocess.call(argv))
'''.replace('BODY', repr(body)).replace('SANDBOX', repr(sandbox)).replace('PAYLOAD', repr(payload))
                    result = run_linux(code)
                    time.sleep(.2)
                    observed = marker.exists()
                    row = {'sandbox': sandbox, 'location': location, 'file': kind,
                           'windows_marker_created': observed, 'runner_returncode': result.returncode}
                    results.append(row)
                    if observed:
                        marker.unlink()
                    if result.returncode:
                        raise RuntimeError('Windows execution probe did not complete')
                    row.update(json.loads(result.stdout))
                    if sandbox and (observed or not row.get('denied')):
                        raise RuntimeError('Windows execution isolation could not be verified; worker disabled')

