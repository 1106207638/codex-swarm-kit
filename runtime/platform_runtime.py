"""Local paths and explicit shell contracts; never invokes models."""
import os
import base64
import shlex
import sys
from pathlib import Path


def codex_home():
    value = os.environ.get('CODEX_HOME')
    if value:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError('CODEX_HOME must be an absolute path')
        return path.resolve()
    return (Path.home() / '.codex').resolve()


def python_command(arguments, *, platform=None, executable=None):
    platform = platform or ('windows' if os.name == 'nt' else 'posix')
    executable = executable or sys.executable
    if not executable or any('\x00' in str(x) or '\n' in str(x) or '\r' in str(x) for x in [executable, *arguments]):
        raise ValueError('command arguments must be single-line strings')
    if platform == 'windows':
        # PowerShell treats smart quotes as delimiters too. Keep all argument
        # bytes outside the shell grammar, including executable paths.
        quote = lambda value: "([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + base64.b64encode(str(value).encode('utf-8')).decode('ascii') + "')))"
        return '& ' + ' '.join(quote(x) for x in [executable, *arguments]), 'PowerShell'
    if platform == 'posix':
        return ' '.join(shlex.quote(str(x)) for x in [executable, *arguments]), '/bin/sh'
    raise ValueError('unsupported shell platform')
