"""
Creation of the target userspace.

This is the top level library of the actor - it orchestrates the creation of
the target userspace and produces the messages with the results. The actual
work is implemented in the libraries imported below.
"""

import os

from leapp.libraries.actor import bootstrap, inputdata, repoaccess, repofiles, targetrepos, targetrhui
from leapp.libraries.common import mounting, overlaygen, rhsm
from leapp.libraries.common.dnflibs import dnfplugin
from leapp.libraries.stdlib import api
from leapp.models import (
    RepositoriesFactsTarget,
    TargetOSInstallationImage,
    TargetUserSpaceInfo,
    UsedTargetRepositories,
    UsedTargetRepository
)

# NOTE: The repofiles inside the scratch container are parsed twice - once to
# get the repoids available before the target userspace is created and once
# after that to produce the RepositoriesFactsTarget msg. These are two
# different states of the container, so the parsing cannot be deduplicated.

SCRATCH_DIR = os.getenv('LEAPP_CONTAINER_ROOT', '/var/lib/leapp/scratch')
MOUNTS_DIR = os.path.join(SCRATCH_DIR, 'mounts')


def _create_target_userspace(context, indata, packages, files, target_repoids):
    """Create the target userspace."""
    target_path = bootstrap.get_target_userspace()
    bootstrap.prepare_target_userspace(context, target_path, target_repoids, list(packages))
    repoaccess.prep_repository_access(context, target_path)

    with mounting.NspawnActions(base_dir=target_path) as target_context:
        bootstrap.copy_files(target_context, files)
    dnfplugin.install(bootstrap.get_target_userspace())

    # If we used only repofiles from leapp-rhui-<provider> then remove these as they provide
    # duplicit definitions as the target clients already installed in the target container
    if indata.rhui_info:
        api.current_logger().debug(
            'Target container should have access to content. '
            'Removing repofiles from leapp-rhui-<provider> from the target..'
        )
        setup_info = indata.rhui_info.target_client_setup_info
        if not setup_info.bootstrap_target_client:
            targetrhui.remove_injected_repofiles_from_our_rhui_packages(context, setup_info)

    # and do not forget to set the rhsm into the container mode again
    with mounting.NspawnActions(bootstrap.get_target_userspace()) as target_context:
        rhsm.set_container_mode(target_context)


def perform():
    indata = inputdata.InputData()
    reserve_space = overlaygen.get_recommended_leapp_free_space(bootstrap.get_target_userspace())
    with overlaygen.create_source_overlay(
            mounts_dir=MOUNTS_DIR,
            scratch_dir=SCRATCH_DIR,
            storage_info=indata.storage_info,
            xfs_info=indata.xfs_info,
            scratch_reserve=reserve_space) as overlay:
        with overlay.nspawn() as context:
            # Mount the ISO into the scratch container
            target_iso = next(api.consume(TargetOSInstallationImage), None)
            with mounting.mount_upgrade_iso_to_root_dir(overlay.target, target_iso):

                # TODO: this is out of tests completely
                targetrhui.setup_target_rhui_access_if_needed(context, indata)

                target_repoids = targetrepos.setup_and_gather_target_repositories(context, indata)
                _create_target_userspace(context, indata, indata.packages, indata.files, target_repoids)
                # TODO: this is tmp solution as proper one needs significant refactoring
                target_repo_facts = repofiles.get_parsed_repofiles_or_stop(
                    context,
                    'Failed to parse target system repofiles',
                    hint=('Ensure the repository definition is correct or remove it '
                          'if the repository is not needed anymore. '
                          'This issue is typically caused by missing definition of the name field. '
                          'For more information, see: https://access.redhat.com/solutions/6969001.')
                )
                api.produce(RepositoriesFactsTarget(repositories=target_repo_facts))
                # ## TODO ends here
                api.produce(UsedTargetRepositories(
                    repos=[UsedTargetRepository(repoid=repo) for repo in target_repoids]))
                api.produce(TargetUserSpaceInfo(
                    path=bootstrap.get_target_userspace(),
                    scratch=SCRATCH_DIR,
                    mounts=MOUNTS_DIR))
