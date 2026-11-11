"""Explicit registration only. Never invoked automatically by the installer.

Run via authorized docker exec as UID10003 with the one-time registration
token on stdin. No PAT is requested, minted, printed or placed in argv. The
runner consumes ACTIONS_RUNNER_INPUT_TOKEN and removes it from its environment.
"""
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys


def main():
    child = None
    try:
        if sys.argv[1:] or os.getresuid() != (10003,)*3:
            raise ValueError()
        os.umask(0o077)
        if any(Path('/runner',p).exists() for p in ('.runner','.credentials','.credentials_rsaparams','.registration-ready')):
            raise ValueError()  # Never replace a prior registration.
        raw=sys.stdin.buffer.read(8193)
        if not 1 <= len(raw) <= 8192 or not raw.strip() or any(c not in b'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-' for c in raw.strip()):
            raise ValueError()
        env=dict(PATH='/usr/local/bin:/usr/bin:/bin',LANG='C.UTF-8',HOME='/runner',
            DOTNET_PROCESSOR_COUNT='1',ACTIONS_RUNNER_INPUT_TOKEN=raw.decode('ascii').strip())
        child=subprocess.Popen(['/runner/bin/Runner.Listener','configure','--unattended',
            '--url','https://github.com/ken2129/c3_neural_compression',
            '--name','c3-issue-intake-'+Path('/etc/hostname').read_text().strip(),
            '--labels','c3-issue-intake-v1','--work','_work','--disableupdate'],
            cwd='/runner',env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,start_new_session=True)
        if child.wait(timeout=120) != 0: raise ValueError()
        for name in ('.runner','.credentials','.credentials_rsaparams'):
            p=Path('/runner',name);s=p.lstat()
            if not stat.S_ISREG(s.st_mode) or s.st_uid != 10003 or s.st_nlink!=1 or not 0<s.st_size<=65536:
                raise ValueError()
            p.chmod(0o600)
        with open('/runner/.registration-ready','xb') as out:
            out.write(b'REGISTERED\n');out.flush();os.fsync(out.fileno())
        print('RUNNER_REGISTRATION_SAVED')
        return 0
    except Exception:
        print('RUNNER_REGISTRATION_BLOCKED',file=sys.stderr)
        return 1
    finally:
        if child is not None:
            # Only this registration's owned session, never another process.
            try: os.killpg(child.pid,signal.SIGKILL)
            except ProcessLookupError: pass
            child.wait()


if __name__=='__main__':raise SystemExit(main())
