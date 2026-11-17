from pathlib import Path
from unittest.mock import patch
import subprocess
import pytest
from wsa_agent import tools, permissions
from wsa_agent.session import SessionState

@pytest.fixture
def root(tmp_path):
    old = tools.get_project_root()
    tools.set_project_root(tmp_path)
    yield tmp_path
    tools.set_project_root(old)

@pytest.mark.parametrize('operation', ['copy', 'move'])
def test_transfer_preserves_binary_bytes(root, operation):
    raw = b'\x89PNG\r\n\x1a\n\xff\x00binary'
    (root/'source.png').write_bytes(raw)
    result = getattr(tools, '_'+operation+'_file')('source.png', 'dest.png')
    assert result.startswith('[OK]'), result
    assert (root/'dest.png').read_bytes() == raw
    assert (root/'source.png').exists() == (operation == 'copy')
    if operation == 'move':
        assert '[OK]' in tools._rollback()
        assert (root/'source.png').read_bytes() == raw
        assert not (root/'dest.png').exists()

@pytest.mark.parametrize('action,args', [('config',['core.editor','evil']), ('branch',['new-branch']), ('remote',['add','origin','example']), ('diff',['--output=x'])])
def test_git_mutating_operations_require_approval(root, action, args):
    assert permissions.approval_reason('git', {'action':action, 'args':args}, SessionState(project_root=root))

def test_failed_git_reports_failure(root):
    subprocess.run(['git','init',str(root)],capture_output=True,check=True)
    result=tools._git('show',['not-a-real-ref'])
    assert result.startswith('[Error]')
    assert 'Exit code: 128' in result

def test_partial_patch_restoration_retains_recovery_checkpoint(root):
    (root/'a.txt').write_bytes(b'old')
    (root/'b.txt').write_bytes(b'second')
    original=tools._write_bytes_atomic
    def fail_restore(path, data):
        if Path(path).name == 'a.txt' and data == b'old':
            raise PermissionError('simulated lock')
        return original(path, data)
    with patch.object(tools, '_write_bytes_atomic', side_effect=fail_restore):
        result=tools._apply_patch([{'path':'a.txt','old_string':'old','new_string':'changed'}, {'path':'b.txt','old_string':'missing','new_string':'x'}])
    assert result.startswith('[Error]') and 'Partial rollback' in result and 'checkpoint' in result
    assert (root/'a.txt').read_bytes() == b'changed'
    assert '[OK]' in tools._rollback()
    assert (root/'a.txt').read_bytes() == b'old'
