"""
Creation of the target userspace.

This is the top level library of the actor - it orchestrates the creation of
the target userspace and produces the messages with the results. The actual
work is implemented in the libraries imported below.
"""

import os

from leapp import reporting
from leapp.exceptions import StopActorExecution, StopActorExecutionError
from leapp.libraries.actor import bootstrap, inputdata, repoaccess, repofiles, targetrepos, targetrhui
from leapp.libraries.common import mounting, overlaygen, rhsm
from leapp.libraries.common.config import get_env, get_product_type, get_target_distro_id
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
PROD_CERTS_FOLDER = 'prod-certs'


def _check_deprecated_rhsm_skip():
    # we do not plan to cover this case by tests as it is purely
    # devel/testing stuff, that becomes deprecated now
    # just log the warning now (better than nothing?); deprecation process will
    # be specified in close future
    if get_env('LEAPP_DEVEL_SKIP_RHSM', '0') == '1':
        api.current_logger().warning(
            'The LEAPP_DEVEL_SKIP_RHSM has been deprecated. Use'
            ' LEAPP_NO_RHSM instead or use the --no-rhsm option for'
            ' leapp. as well custom repofile has not been defined.'
            ' Please read documentation about new "skip rhsm" solution.'
        )


def _get_product_certificate_path():
    """
    Retrieve the required / used product certificate for RHSM.

    Product certificates are only used for RHEL. Returns None if the target
    distro is not RHEL.

    :return: The path to the product certificate or None on non-RHEL systems
    :raises: StopActorExecution if a certificate cannot be found
    """
    if get_target_distro_id() != 'rhel':
        return None

    architecture = api.current_actor().configuration.architecture
    target_version = api.current_actor().configuration.version.target
    target_product_type = get_product_type('target')
    certs_dir = api.get_common_folder_path(PROD_CERTS_FOLDER)

    # We do not need any special certificates to reach repos from non-ga channels, only beta requires special cert.
    if target_product_type != 'beta':
        target_product_type = 'ga'

    prod_certs = {
        'x86_64': {
            'ga': '479.pem',
            'beta': '486.pem',
        },
        'aarch64': {
            'ga': '419.pem',
            'beta': '363.pem',
        },
        'ppc64le': {
            'ga': '279.pem',
            'beta': '362.pem',
        },
        's390x': {
            'ga': '72.pem',
            'beta': '433.pem',
        }
    }

    try:
        cert = prod_certs[architecture][target_product_type]
    except KeyError as e:
        raise StopActorExecutionError(message='Failed to determine what certificate to use for {}.'.format(e))

    cert_path = os.path.join(certs_dir, target_version, cert)
    if not os.path.isfile(cert_path):
        additional_summary = ''
        if target_product_type != 'ga':
            additional_summary = (
                ' This can happen when upgrading a beta system and the chosen target version does not have'
                ' beta certificates attached (for example, because the GA has been released already).'

            )

        reporting.create_report([
            reporting.Title('Cannot find the product certificate file for the chosen target system.'),
            reporting.Summary(
                'Expected certificate: {cert} with path {path} but it could not be found.{additional}'.format(
                    cert=cert, path=cert_path, additional=additional_summary)
            ),
            reporting.Groups([reporting.Groups.REPOSITORY]),
            reporting.Groups([reporting.Groups.INHIBITOR]),
            reporting.Severity(reporting.Severity.HIGH),
            reporting.Remediation(hint=(
                'Set the corresponding target os version in the LEAPP_DEVEL_TARGET_RELEASE environment variable for'
                'which the {cert} certificate is provided'.format(cert=cert)
            )),
        ])
        raise StopActorExecution()

    return cert_path


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
    # NOTE: this one action is out of unit-tests completely; we do not use
    # in unit tests the LEAPP_DEVEL_SKIP_RHSM envar anymore
    _check_deprecated_rhsm_skip()

    indata = inputdata.InputData()
    prod_cert_path = _get_product_certificate_path()
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

                target_repoids = targetrepos.setup_and_gather_target_repositories(context, indata, prod_cert_path)
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
