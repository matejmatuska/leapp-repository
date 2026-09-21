"""
Setup of the access to the target content on RHUI (cloud) systems.

On RHUI systems the access to the target content is provided by the RHUI
client RPMs specific for the particular cloud provider. To be able to reach
the target content, the source clients have to be swapped for the target ones
inside the scratch container.
"""

import contextlib
import os

from leapp.exceptions import StopActorExecutionError
from leapp.libraries.actor import bootstrap, repoaccess, repofiles
from leapp.libraries.common import utils
from leapp.libraries.common.config.version import get_target_major_version
from leapp.libraries.stdlib import api, CalledProcessError

YUM_REPOS_DIR = '/etc/yum.repos.d'


def get_copy_location_from_copy_in_task(context_basepath, copy_task):
    """
    Get the path (inside the container) where the copied file will be located.

    When the destination of the copy task is an existing directory, the copied
    file keeps its name inside that directory.

    NOTE: The destination paths are expected to be absolute, in which case the
    given base path of the container is ignored (see os.path.join) and the
    existence of the destination directory is checked outside of the container.

    :param context_basepath: the base directory of the container
    :type context_basepath: string
    :param copy_task: the copy task to resolve
    :type copy_task: CopyFile
    :rtype: string
    """
    basename = os.path.basename(copy_task.src)
    dest_in_container = os.path.join(context_basepath, copy_task.dst)
    if os.path.isdir(dest_in_container):
        return os.path.join(copy_task.dst, basename)
    return copy_task.dst


def _is_repofile(path):
    return os.path.dirname(path) == YUM_REPOS_DIR and os.path.basename(path).endswith('.repo')


def _get_client_repofiles(context, rhui_info):
    """
    Get repofiles inside the container provided by the target RHUI clients.

    When the target clients are not bootstrapped, they provide nothing.

    :rtype: set[str]
    """
    if not rhui_info.target_client_setup_info.bootstrap_target_client:
        return set()

    target_content_access_files = repoaccess.query_rpm_for_pkg_files(context, rhui_info.target_client_pkg_names)
    return {context.full_path(path) for path in target_content_access_files if _is_repofile(path)}


def _get_setup_repofiles(context, rhui_info):
    """
    Get repofiles inside the container copied there to set up the target RHUI access.

    On some platforms the repositories provided by the client are not
    sufficient to install the client into the target userspace (GCP).

    :rtype: set[str]
    """
    setup_tasks = rhui_info.target_client_setup_info.preinstall_tasks.files_to_copy_into_overlay
    repofile_tasks = (task for task in setup_tasks if task.src.endswith('repo'))
    return {
        context.full_path(get_copy_location_from_copy_in_task(context.base_dir, task)) for task in repofile_tasks
    }


def _get_foreign_repofiles(context, rhui_info):
    """
    Get repofiles inside the container that are unknown to the target RHUI setup.

    :rtype: set[str]
    """
    client_repofiles = _get_client_repofiles(context, rhui_info)

    yum_repos_d = context.full_path(YUM_REPOS_DIR)
    all_repofiles = {os.path.join(yum_repos_d, path) for path in os.listdir(yum_repos_d) if path.endswith('.repo')}
    api.current_logger().debug('(RHUI Setup) All available repofiles: {0}'.format(' '.join(all_repofiles)))

    foreign_repofiles = all_repofiles - client_repofiles - _get_setup_repofiles(context, rhui_info)
    api.current_logger().debug(
        'The following repofiles are considered as unknown to'
        ' the target RHUI content setup and will be ignored: {0}'.format(' '.join(foreign_repofiles))
    )
    return foreign_repofiles


@contextlib.contextmanager
def _hidden_repofiles(repofile_paths):
    """
    Hide the given repofiles so that they are not recognized by DNF.

    The repofiles are renamed and they are guaranteed to be renamed back when
    leaving the context - even when an error occurs.

    :param repofile_paths: paths (on the host) of the repofiles to hide
    :type repofile_paths: iterable[str]
    """
    for repofile in repofile_paths:
        os.rename(repofile, '{0}.back'.format(repofile))
    try:
        yield
    finally:
        for repofile in repofile_paths:
            os.rename('{0}.back'.format(repofile), repofile)


def _query_repoids(context):
    """
    Get repoids of all the repositories available inside the container.

    The source and debug repositories are ignored.

    :rtype: set[str]
    """
    dnf_cmd = [
        'dnf', 'repolist',
        '--releasever', api.current_actor().configuration.version.target, '-v',
        '--enablerepo', '*',
        '--disablerepo', '*-source-*',
        '--disablerepo', '*-debug-*',
    ]
    try:
        repolist_result = context.call(dnf_cmd)['stdout']
    except CalledProcessError as err:
        details = {'err': err.stderr, 'details': str(err)}
        raise StopActorExecutionError(
            message='Failed to retrieve repoids provided by target RHUI clients.',
            details=details
        )

    repoid_lines = [line for line in repolist_result.split('\n') if line.startswith('Repo-id')]
    return {line.split(':', 1)[1].strip() for line in repoid_lines}


def get_rhui_available_repoids(context, rhui_info):
    """
    Get repoids provided by the RHUI target clients

    Only the repositories provided by the (already installed) target clients
    and by the repofiles copied into the container during the RHUI setup are
    taken into account. All the other repofiles are hidden for the time of
    the discovery.

    :rtype: set[str]
    """
    with _hidden_repofiles(_get_foreign_repofiles(context, rhui_info)):
        return _query_repoids(context)


def remove_injected_repofiles_from_our_rhui_packages(target_userspace_ctx, rhui_setup_info):
    """
    Remove the repofiles injected into the target userspace during the RHUI setup.

    Only the repofiles that are not owned by any RPM are removed as the others
    are provided by the RHUI clients installed in the target userspace and
    their removal would break the access to the target content.
    """
    target_userspace_path = bootstrap.get_target_userspace()
    for copy in rhui_setup_info.preinstall_tasks.files_to_copy_into_overlay:
        dst_in_container = get_copy_location_from_copy_in_task(target_userspace_path, copy)
        dst_in_container = dst_in_container.strip('/')
        dst_in_host = os.path.join(target_userspace_path, dst_in_container)

        if os.path.isfile(dst_in_host) and dst_in_host.endswith('.repo'):
            # The repofile might have been replaced by a new one provided by the RHUI client if names collide
            # Performance: Do the query here and not earlier, because we would be running rpm needlessly
            try:
                path_with_root = '/' + dst_in_container
                target_userspace_ctx.call(['rpm', '-q', '--whatprovides', path_with_root])
                api.current_logger().debug('Repofile {0} kept as it is owned by some RPM.'.format(dst_in_host))
            except CalledProcessError:
                # rpm exists with 1 if the file is not owned by any RPM. We might be catching all kinds of other
                # problems here, but still better than always removing repofiles.
                api.current_logger().debug('Removing repofile - not owned by any RPM: {0}'.format(dst_in_host))
                os.remove(dst_in_host)


def _apply_rhui_access_preinstall_tasks(context, rhui_setup_info):
    """
    Prepare the container for the installation of the target RHUI clients.
    """
    if rhui_setup_info.preinstall_tasks:
        api.current_logger().debug('Applying RHUI preinstall tasks.')
        preinstall_tasks = rhui_setup_info.preinstall_tasks

        for file_to_remove in preinstall_tasks.files_to_remove:
            api.current_logger().debug('Removing {0} from the scratch container.'.format(file_to_remove))
            context.remove(file_to_remove)

        for copy_info in preinstall_tasks.files_to_copy_into_overlay:
            api.current_logger().debug(
                'Copying {0} in {1} into the scratch container.'.format(copy_info.src, copy_info.dst)
            )
            context.makedirs(os.path.dirname(copy_info.dst), exists_ok=True)
            context.copy_to(copy_info.src, copy_info.dst)


def _apply_rhui_access_postinstall_tasks(context, rhui_setup_info):
    """
    Finish the setup of the access to the target content inside the container.
    """
    if rhui_setup_info.postinstall_tasks:
        api.current_logger().debug('Applying RHUI postinstall tasks.')
        for copy_info in rhui_setup_info.postinstall_tasks.files_to_copy:
            context.makedirs(os.path.dirname(copy_info.dst), exists_ok=True)
            debug_msg = 'Copying {0} to {1} (inside the scratch container).'
            api.current_logger().debug(debug_msg.format(copy_info.src, copy_info.dst))
            context.call(['cp', copy_info.src, copy_info.dst])


def _get_repoids_in_copied_repofiles(rhui_setup_info):
    """
    Get repoids defined in the repofiles copied into the container by the preinstall tasks.

    :rtype: set[str]
    """
    copy_tasks = rhui_setup_info.preinstall_tasks.files_to_copy_into_overlay
    copied_repofiles = [copy.src for copy in copy_tasks if copy.src.endswith('.repo')]

    copied_repoids = set()
    for repofile in copied_repofiles:
        repofile_contents = repofiles.parse_repofile_or_stop(repofile, 'Failed to parse repositories for RHUI')
        copied_repoids.update(entry.repoid for entry in repofile_contents.data)
    return copied_repoids


def _build_client_swap_cmd(rhui_info):
    """
    Build the dnf command performing the swap of the RHUI clients.
    """
    setup_info = rhui_info.target_client_setup_info

    cmd = ['dnf', '-y']

    if setup_info.enable_only_repoids_in_copied_files and setup_info.preinstall_tasks:
        cmd += ['--disablerepo', '*']
        for copied_repoid in _get_repoids_in_copied_repofiles(setup_info):
            cmd.extend(('--enablerepo', copied_repoid))

    cmd += [
        '--setopt=module_platform_id=platform:el{}'.format(get_target_major_version()),
        '--setopt=keepcache=1',
        '--releasever', api.current_actor().configuration.version.target,
        '--disableplugin', 'subscription-manager',
        'shell'
    ]
    return cmd


def _build_client_swap_transaction(rhui_info):
    """
    Build the `dnf shell` transaction swapping the source clients for the target ones.

    :rtype: string
    """
    src_client_remove_steps = ['remove {0}'.format(client) for client in rhui_info.src_client_pkg_names]
    target_client_install_steps = ['install {0}'.format(client) for client in rhui_info.target_client_pkg_names]

    dnf_transaction_steps = src_client_remove_steps + target_client_install_steps + ['transaction run']
    return '\n'.join(dnf_transaction_steps)


def _swap_rhui_clients(context, rhui_info):
    """
    Swap the source RHUI clients for the target ones inside the container.

    :raises StopActorExecutionError: when the clients cannot be swapped
    """
    cmd = _build_client_swap_cmd(rhui_info)
    try:
        dnf_shell_instructions = _build_client_swap_transaction(rhui_info)
        api.current_logger().debug(
            'Supplying the following instructions to the `dnf shell`: {}'.format(dnf_shell_instructions)
        )
        context.call(cmd, callback_raw=utils.logging_handler, stdin=dnf_shell_instructions)
    except CalledProcessError as error:
        api.current_logger().debug(
            'Failed to swap RHUI clients. This is likely because there are no repositories '
            ' containing RHUI clients enabled, or we cannot access them.'
        )
        api.current_logger().debug(error)

        swapping_clients_info_msg = 'Failed to swap `{0}` (source client{1}) with {2} (target client{3}).'
        swapping_clients_info_msg = swapping_clients_info_msg.format(
            ' '.join(rhui_info.src_client_pkg_names),
            '' if len(rhui_info.src_client_pkg_names) == 1 else 's',
            ' '.join(rhui_info.target_client_pkg_names),
            '' if len(rhui_info.target_client_pkg_names) == 1 else 's',
        )

        details = {
            'details': swapping_clients_info_msg,
            'error': str(error)
        }
        raise StopActorExecutionError(
            'Failed to swap RHUI clients to establish content access',
            details=details
        )


def _get_files_owned_by_target_clients(context, rhui_info):
    """
    Get files inside the container owned by the (installed) target RHUI clients.

    :raises StopActorExecutionError: when the target clients are not installed
    :rtype: set[str]
    """
    try:
        return repoaccess.query_rpm_for_pkg_files(context, rhui_info.target_client_pkg_names)
    except CalledProcessError as err:  # We failed to rpm -qf PKG, the PKG is most likely not installed
        api.current_logger().critical('Failed to query files owned by target RHUI clients (clients=%s). This is caused'
                                      ' by failing to install the target clients during the client-swap step.'
                                      ' Full error: %s', rhui_info.target_client_pkg_names, err)

        target_major = get_target_major_version()
        plural_suffix = 's' if len(rhui_info.target_client_pkg_names) > 1 else ''
        client_rpms = ', '.join(rhui_info.target_client_pkg_names)
        msg = ('Could not find the RHEL {target_major} RHUI client rpm{plural_suffix} ({client_rpms})'
               ' in the cloud provider\'s client repository.')
        raise StopActorExecutionError(msg.format(target_major=target_major, plural_suffix=plural_suffix,
                                                 client_rpms=client_rpms))


def _cleanup_rhui_setup_files(context, rhui_info):
    """
    Remove the files injected into the container that are not needed anymore.

    The files provided by the installed target clients and the files required
    for their operation are kept. The rest is removed so there are no duplicit
    repoids.
    """
    files_owned_by_clients = _get_files_owned_by_target_clients(context, rhui_info)

    setup_info = rhui_info.target_client_setup_info
    for copy_task in setup_info.preinstall_tasks.files_to_copy_into_overlay:
        dest = get_copy_location_from_copy_in_task(context.base_dir, copy_task)
        can_be_cleaned_up = copy_task.src not in setup_info.files_supporting_client_operation
        if dest not in files_owned_by_clients and can_be_cleaned_up:
            context.remove(dest)


def setup_target_rhui_access_if_needed(context, indata):
    """
    Set up the access to the target content provided by RHUI in the container.

    Nothing is done when the system is not a RHUI system.

    :param context: the scratch container
    :type context: mounting.IsolatedActions class
    :param indata: majority of input data for the actor
    :type indata: class InputData
    """
    if not indata.rhui_info:
        return

    bootstrap.create_target_userspace_directories(bootstrap.get_target_userspace())

    setup_info = indata.rhui_info.target_client_setup_info
    _apply_rhui_access_preinstall_tasks(context, setup_info)

    if not setup_info.bootstrap_target_client:
        # Installation of the target RHUI client is not possible and we bundle all necessary
        # files into the leapp-rhui-<provider> packages.
        api.current_logger().debug('Bootstrapping target RHUI client is disabled, leapp will rely '
                                   'only on files budled in leapp-rhui-<provider> package.')
        return

    _swap_rhui_clients(context, indata.rhui_info)
    _apply_rhui_access_postinstall_tasks(context, setup_info)
    _cleanup_rhui_setup_files(context, indata.rhui_info)
