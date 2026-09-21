import os
from collections import namedtuple

import pytest

from leapp import models
from leapp.exceptions import StopActorExecutionError
from leapp.libraries.actor import bootstrap, repoaccess, repofiles, targetrhui
from leapp.libraries.common import utils
from leapp.libraries.common.testutils import CurrentActorMocked, logger_mocked
from leapp.libraries.stdlib import api, CalledProcessError

TARGET_USERSPACE = '/var/lib/leapp/el10userspace'

REPOLIST_OUTPUT = '\n'.join([
    'Repo-id            : rhui-client-config-server-10',
    'Repo-name          : Client configuration',
    'Repo-status        : enabled',
    'Repo-id            : rhel-10-baseos-rhui-rpms',
    'Repo-name          : BaseOS',
])


class MockedContext:
    """
    Mocked mounting.IsolatedActions class recording all the performed operations.
    """

    def __init__(self, base_dir='/base/dir', call_result=None, call_hook=None):
        self.base_dir = str(base_dir)
        self.call_result = call_result if call_result is not None else {'stdout': ''}
        self.call_hook = call_hook
        # ordered log of all the operations performed on the container
        self.ops = []

    def full_path(self, path):
        return os.path.join(self.base_dir, path.lstrip('/'))

    def call(self, cmd, **kwargs):
        self.ops.append(('call', cmd, kwargs))
        if self.call_hook:
            self.call_hook(cmd)
        return self.call_result

    def remove(self, path):
        self.ops.append(('remove', path))

    def makedirs(self, path, exists_ok=True):
        self.ops.append(('makedirs', path))

    def copy_to(self, src, dst):
        self.ops.append(('copy_to', src, dst))

    def ops_of_kind(self, kind):
        return [op for op in self.ops if op[0] == kind]


def raise_call_error(cmd=None):
    raise CalledProcessError(
        message='Command {0} failed with exit code 1.'.format(cmd),
        command=cmd or ['cmd'],
        result={'signal': None, 'exit_code': 1, 'pid': 0, 'stdout': 'fake out', 'stderr': 'fake err'}
    )


def _gen_rhui_info(bootstrap_target_client=True,
                   enable_only_repoids_in_copied_files=True,
                   files_to_remove=None,
                   files_to_copy_into_overlay=None,
                   postinstall_files=None,
                   files_supporting_client_operation=None):
    setup_info = models.TargetRHUISetupInfo(
        bootstrap_target_client=bootstrap_target_client,
        enable_only_repoids_in_copied_files=enable_only_repoids_in_copied_files,
        preinstall_tasks=models.TargetRHUIPreInstallTasks(
            files_to_remove=files_to_remove or [],
            files_to_copy_into_overlay=files_to_copy_into_overlay or [],
        ),
        postinstall_tasks=models.TargetRHUIPostInstallTasks(files_to_copy=postinstall_files or []),
        files_supporting_client_operation=files_supporting_client_operation or [],
    )
    return models.RHUIInfo(
        provider='aws',
        src_client_pkg_names=['rh-amazon-rhui-client'],
        target_client_pkg_names=['rh-amazon-rhui-client-10'],
        target_client_setup_info=setup_info,
    )


def _gen_indata(rhui_info):
    return namedtuple('TestInData', ['rhui_info'])(rhui_info)


@pytest.fixture
def actor_mocked(monkeypatch):
    actor = CurrentActorMocked(dst_ver='10.0')
    monkeypatch.setattr(api, 'current_actor', actor)
    monkeypatch.setattr(api, 'current_logger', logger_mocked())
    return actor


@pytest.fixture
def userspace_mocked(monkeypatch):
    """
    Make the target userspace path known without touching the filesystem.
    """
    created_dirs = []
    monkeypatch.setattr(bootstrap, 'get_target_userspace', lambda: TARGET_USERSPACE)
    monkeypatch.setattr(bootstrap, 'create_target_userspace_directories', created_dirs.append)
    return created_dirs


#
# R1: get_copy_location_from_copy_in_task
#

@pytest.mark.parametrize(('dst', 'existing_dirs', 'expected'), [
    ('/etc/yum.repos.d', ['/etc/yum.repos.d'], '/etc/yum.repos.d/leapp-aws.repo'),
    ('/etc/yum.repos.d/client.repo', [], '/etc/yum.repos.d/client.repo'),
])
def test_get_copy_location_from_copy_in_task(monkeypatch, dst, existing_dirs, expected):
    monkeypatch.setattr(os.path, 'isdir', lambda path: path in existing_dirs)

    copy_task = models.CopyFile(src='/host/files/leapp-aws.repo', dst=dst)

    assert targetrhui.get_copy_location_from_copy_in_task('/base/dir', copy_task) == expected


def test_get_copy_location_from_copy_in_task_ignores_basepath_for_absolute_dst(monkeypatch):
    """
    Characterization: the base path is ignored when the destination is absolute.

    os.path.join() drops the base path when the second part is an absolute path,
    so the existence of the destination directory is in fact checked outside of
    the container.
    """
    checked_paths = []

    def isdir_mocked(path):
        checked_paths.append(path)
        return False

    monkeypatch.setattr(os.path, 'isdir', isdir_mocked)

    copy_task = models.CopyFile(src='/host/files/leapp-aws.repo', dst='/etc/yum.repos.d')
    targetrhui.get_copy_location_from_copy_in_task('/base/dir', copy_task)

    assert checked_paths == ['/etc/yum.repos.d']


#
# R2: get_rhui_available_repoids
#

@pytest.fixture
def container_repos_dir(tmpdir):
    """
    Container with client, setup, foreign and non-repofile files in /etc/yum.repos.d.
    """
    repos_dir = tmpdir.mkdir('etc').mkdir('yum.repos.d')
    for fname in ('client.repo', 'setup.repo', 'foreign.repo', 'ignored.txt'):
        repos_dir.join(fname).write('# {0}\n'.format(fname))
    return repos_dir


def _gen_repoids_rhui_info(bootstrap_target_client=True):
    return _gen_rhui_info(
        bootstrap_target_client=bootstrap_target_client,
        files_to_copy_into_overlay=[models.CopyFile(src='/host/setup.repo', dst='/etc/yum.repos.d/setup.repo')],
    )


@pytest.mark.parametrize('bootstrap_target_client', (True, False))
def test_get_rhui_available_repoids(monkeypatch, actor_mocked, tmpdir, container_repos_dir,
                                    bootstrap_target_client):
    """
    Only the client and the setup repofiles are visible to `dnf repolist`.
    """
    visible_during_repolist = []

    def call_hook(dummy_cmd):
        visible_during_repolist.extend(sorted(os.listdir(str(container_repos_dir))))

    monkeypatch.setattr(os.path, 'isdir', lambda dummy_path: False)
    monkeypatch.setattr(
        repoaccess, 'query_rpm_for_pkg_files',
        lambda dummy_ctx, dummy_pkgs: {'/etc/yum.repos.d/client.repo', '/usr/bin/client-tool'}
    )
    context = MockedContext(tmpdir, call_result={'stdout': REPOLIST_OUTPUT}, call_hook=call_hook)

    repoids = targetrhui.get_rhui_available_repoids(context, _gen_repoids_rhui_info(bootstrap_target_client))

    assert repoids == {'rhui-client-config-server-10', 'rhel-10-baseos-rhui-rpms'}

    # without the bootstrap of the target client, the client repofiles are unknown - hence foreign
    expected_visible = ['client.repo', 'foreign.repo.back', 'ignored.txt', 'setup.repo']
    if not bootstrap_target_client:
        expected_visible = ['client.repo.back', 'foreign.repo.back', 'ignored.txt', 'setup.repo']
    assert visible_during_repolist == expected_visible

    # the original state of the directory is restored
    assert sorted(os.listdir(str(container_repos_dir))) == [
        'client.repo', 'foreign.repo', 'ignored.txt', 'setup.repo'
    ]

    dnf_cmd = context.ops_of_kind('call')[0][1]
    assert dnf_cmd == [
        'dnf', 'repolist',
        '--releasever', '10.0', '-v',
        '--enablerepo', '*',
        '--disablerepo', '*-source-*',
        '--disablerepo', '*-debug-*',
    ]


def test_get_rhui_available_repoids_dnf_failure(monkeypatch, actor_mocked, tmpdir, container_repos_dir):
    """
    The hidden repofiles are restored even when `dnf repolist` fails.
    """
    monkeypatch.setattr(os.path, 'isdir', lambda dummy_path: False)
    monkeypatch.setattr(
        repoaccess, 'query_rpm_for_pkg_files',
        lambda dummy_ctx, dummy_pkgs: {'/etc/yum.repos.d/client.repo'}
    )
    context = MockedContext(tmpdir, call_hook=raise_call_error)

    with pytest.raises(StopActorExecutionError) as err:
        targetrhui.get_rhui_available_repoids(context, _gen_repoids_rhui_info())

    assert 'Failed to retrieve repoids provided by target RHUI clients.' in str(err.value)
    assert sorted(os.listdir(str(container_repos_dir))) == [
        'client.repo', 'foreign.repo', 'ignored.txt', 'setup.repo'
    ]


#
# R3, R4: pre/post-install tasks
#

def test_apply_rhui_access_preinstall_tasks(actor_mocked):
    """
    Files are removed from the container first, the copies are performed afterwards.
    """
    setup_info = _gen_rhui_info(
        files_to_remove=['/etc/yum.repos.d/redhat.repo'],
        files_to_copy_into_overlay=[
            models.CopyFile(src='/host/leapp-aws.repo', dst='/etc/yum.repos.d/leapp-aws.repo'),
            models.CopyFile(src='/host/client.pem', dst='/etc/pki/rhui/client.pem'),
        ],
    ).target_client_setup_info
    context = MockedContext()

    targetrhui._apply_rhui_access_preinstall_tasks(context, setup_info)

    assert context.ops == [
        ('remove', '/etc/yum.repos.d/redhat.repo'),
        ('makedirs', '/etc/yum.repos.d'),
        ('copy_to', '/host/leapp-aws.repo', '/etc/yum.repos.d/leapp-aws.repo'),
        ('makedirs', '/etc/pki/rhui'),
        ('copy_to', '/host/client.pem', '/etc/pki/rhui/client.pem'),
    ]


def test_apply_rhui_access_postinstall_tasks(actor_mocked):
    """
    The postinstall files are copied inside the container (by the `cp` command).
    """
    setup_info = _gen_rhui_info(
        postinstall_files=[models.CopyFile(src='/etc/leapp-rhui/client.pem', dst='/etc/pki/rhui/client.pem')],
    ).target_client_setup_info
    context = MockedContext()

    targetrhui._apply_rhui_access_postinstall_tasks(context, setup_info)

    assert context.ops == [
        ('makedirs', '/etc/pki/rhui'),
        ('call', ['cp', '/etc/leapp-rhui/client.pem', '/etc/pki/rhui/client.pem'], {}),
    ]


#
# R5: setup_target_rhui_access_if_needed
#

def _gen_setup_rhui_info(**kwargs):
    kwargs.setdefault('files_to_copy_into_overlay', [
        models.CopyFile(src='/host/leapp-aws.repo', dst='/etc/yum.repos.d/leapp-aws.repo'),
        models.CopyFile(src='/host/client.pem', dst='/etc/pki/rhui/client.pem'),
        models.CopyFile(src='/host/owned.repo', dst='/etc/yum.repos.d/owned.repo'),
    ])
    kwargs.setdefault('files_supporting_client_operation', ['/host/client.pem'])
    kwargs.setdefault('postinstall_files', [
        models.CopyFile(src='/etc/leapp-rhui/client.pem', dst='/etc/pki/rhui/client.pem')
    ])
    return _gen_rhui_info(**kwargs)


@pytest.fixture
def swap_mocked(monkeypatch):
    """
    Mock everything the client swap needs but the container itself.
    """
    monkeypatch.setattr(os.path, 'isdir', lambda dummy_path: False)
    monkeypatch.setattr(
        repoaccess, 'query_rpm_for_pkg_files',
        lambda dummy_ctx, dummy_pkgs: {'/etc/yum.repos.d/owned.repo'}
    )
    monkeypatch.setattr(
        repofiles, 'parse_repofile_or_stop',
        lambda repofile, dummy_msg: models.RepositoryFile(
            file=repofile,
            data=[
                models.RepositoryData(repoid='rhui-client-config', name='client config'),
                models.RepositoryData(repoid='rhui-baseos', name='baseos'),
            ]
        )
    )


def test_setup_target_rhui_access_if_needed_no_rhui(actor_mocked, userspace_mocked):
    context = MockedContext()

    targetrhui.setup_target_rhui_access_if_needed(context, _gen_indata(None))

    assert not context.ops
    assert not userspace_mocked


def test_setup_target_rhui_access_if_needed_no_bootstrap(actor_mocked, userspace_mocked, swap_mocked):
    """
    Without the client bootstrap only the preinstall tasks are applied.
    """
    context = MockedContext()
    rhui_info = _gen_setup_rhui_info(bootstrap_target_client=False)

    targetrhui.setup_target_rhui_access_if_needed(context, _gen_indata(rhui_info))

    assert userspace_mocked == [TARGET_USERSPACE]
    assert not context.ops_of_kind('call')
    assert not context.ops_of_kind('remove')
    assert [op[1] for op in context.ops_of_kind('copy_to')] == ['/host/leapp-aws.repo',
                                                                '/host/client.pem',
                                                                '/host/owned.repo']


def test_setup_target_rhui_access_if_needed(actor_mocked, userspace_mocked, swap_mocked):
    """
    The full setup: preinstall tasks, client swap, postinstall tasks and the cleanup.
    """
    context = MockedContext()

    targetrhui.setup_target_rhui_access_if_needed(context, _gen_indata(_gen_setup_rhui_info()))

    calls = context.ops_of_kind('call')
    assert len(calls) == 2  # the client swap and the postinstall copy

    cmd, kwargs = calls[0][1], calls[0][2]
    assert cmd[:4] == ['dnf', '-y', '--disablerepo', '*']
    enabled_repoids = {cmd[i + 1] for i, item in enumerate(cmd) if item == '--enablerepo'}
    assert enabled_repoids == {'rhui-client-config', 'rhui-baseos'}
    assert cmd[-7:] == [
        '--setopt=module_platform_id=platform:el10',
        '--setopt=keepcache=1',
        '--releasever', '10.0',
        '--disableplugin', 'subscription-manager',
        'shell'
    ]
    assert kwargs['stdin'] == ('remove rh-amazon-rhui-client\n'
                               'install rh-amazon-rhui-client-10\n'
                               'transaction run')
    assert kwargs['callback_raw'] == utils.logging_handler

    # the postinstall tasks are applied after the swap
    assert calls[1][1] == ['cp', '/etc/leapp-rhui/client.pem', '/etc/pki/rhui/client.pem']

    # only the injected files that are neither owned by the clients nor required
    # for their operation are cleaned up
    assert context.ops_of_kind('remove') == [('remove', '/etc/yum.repos.d/leapp-aws.repo')]


def test_setup_target_rhui_access_if_needed_all_repoids_enabled(actor_mocked, userspace_mocked, swap_mocked):
    """
    Repositories are not restricted when enable_only_repoids_in_copied_files is False.
    """
    context = MockedContext()
    rhui_info = _gen_setup_rhui_info(enable_only_repoids_in_copied_files=False)

    targetrhui.setup_target_rhui_access_if_needed(context, _gen_indata(rhui_info))

    cmd = context.ops_of_kind('call')[0][1]
    assert '--disablerepo' not in cmd
    assert '--enablerepo' not in cmd


def test_setup_target_rhui_access_if_needed_swap_failure(actor_mocked, userspace_mocked, swap_mocked):
    context = MockedContext(call_hook=raise_call_error)

    with pytest.raises(StopActorExecutionError) as err:
        targetrhui.setup_target_rhui_access_if_needed(context, _gen_indata(_gen_setup_rhui_info()))

    assert 'Failed to swap RHUI clients to establish content access' in str(err.value)
    # neither the postinstall tasks nor the cleanup have been performed
    assert len(context.ops_of_kind('call')) == 1
    assert not context.ops_of_kind('remove')


def test_setup_target_rhui_access_if_needed_missing_target_clients(monkeypatch, actor_mocked, userspace_mocked,
                                                                   swap_mocked):
    """
    The upgrade is stopped when the target clients cannot be found after the swap.
    """
    def query_rpm_for_pkg_files_mocked(dummy_context, dummy_pkgs):
        raise_call_error(['rpm', '-ql'])

    monkeypatch.setattr(repoaccess, 'query_rpm_for_pkg_files', query_rpm_for_pkg_files_mocked)
    context = MockedContext()

    with pytest.raises(StopActorExecutionError) as err:
        targetrhui.setup_target_rhui_access_if_needed(context, _gen_indata(_gen_setup_rhui_info()))

    assert 'Could not find the RHEL 10 RHUI client rpm (rh-amazon-rhui-client-10)' in str(err.value)
    assert not context.ops_of_kind('remove')


#
# R6: remove_injected_repofiles_from_our_rhui_packages
#

def test_remove_injected_repofiles_from_our_rhui_packages(monkeypatch, actor_mocked, tmpdir):
    """
    Only the injected repofiles that are not owned by any rpm are removed.
    """
    userspace = tmpdir.mkdir('el10userspace')
    repos_dir = userspace.mkdir('etc').mkdir('yum.repos.d')
    for fname in ('owned.repo', 'injected.repo'):
        repos_dir.join(fname).write('# {0}\n'.format(fname))

    def call_hook(cmd):
        assert cmd[:3] == ['rpm', '-q', '--whatprovides']
        if cmd[3] != '/etc/yum.repos.d/owned.repo':
            raise_call_error(cmd)

    monkeypatch.setattr(os.path, 'isdir', lambda dummy_path: False)
    monkeypatch.setattr(bootstrap, 'get_target_userspace', lambda: str(userspace))
    context = MockedContext(call_hook=call_hook)

    setup_info = _gen_rhui_info(
        files_to_copy_into_overlay=[
            models.CopyFile(src='/host/owned.repo', dst='/etc/yum.repos.d/owned.repo'),
            models.CopyFile(src='/host/injected.repo', dst='/etc/yum.repos.d/injected.repo'),
            # not a repofile - rpm is not queried at all
            models.CopyFile(src='/host/client.pem', dst='/etc/pki/rhui/client.pem'),
        ]
    ).target_client_setup_info

    targetrhui.remove_injected_repofiles_from_our_rhui_packages(context, setup_info)

    assert sorted(os.listdir(str(repos_dir))) == ['owned.repo']
    assert len(context.ops_of_kind('call')) == 2
