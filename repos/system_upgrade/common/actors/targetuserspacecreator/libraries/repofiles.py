"""
Parsing of repository files (repofiles) for the needs of this actor.

The repofiles are parsed on several places during the creation of the target
userspace and an invalid repofile always means the upgrade cannot continue.
The functions below are just thin wrappers around the repofileutils library
taking care of the error handling.
"""

from leapp.exceptions import StopActorExecutionError
from leapp.libraries.common import repofileutils

INVALID_REPOFILE_HINT = (
    'Ensure the repository definition is correct or remove it '
    'if the repository is not required for the upgrade.'
)


def get_parsed_repofiles_or_stop(context, error_msg, hint=INVALID_REPOFILE_HINT):
    """
    Get all repofiles inside the given container parsed or stop the upgrade.

    :param context: the container in which the repofiles should be parsed
    :type context: mounting.IsolatedActions class
    :param error_msg: the error message used when a repofile cannot be parsed
    :type error_msg: str
    :param hint: the hint provided to the user when a repofile cannot be parsed
    :type hint: str
    :rtype: list(RepositoryFile)
    :raises StopActorExecutionError: when any of the repofiles is invalid
    """
    try:
        return repofileutils.get_parsed_repofiles(context)
    except repofileutils.InvalidRepoDefinition as e:
        raise StopActorExecutionError(
            message='{}: {}'.format(error_msg, str(e)),
            details={'hint': hint})


def parse_repofile_or_stop(repofile, error_msg, hint=INVALID_REPOFILE_HINT):
    """
    Get the given repofile parsed or stop the upgrade.

    :param repofile: path to the repofile
    :type repofile: str
    :param error_msg: the error message used when the repofile cannot be parsed
    :type error_msg: str
    :param hint: the hint provided to the user when the repofile cannot be parsed
    :type hint: str
    :rtype: RepositoryFile
    :raises StopActorExecutionError: when the repofile is invalid
    """
    try:
        return repofileutils.parse_repofile(repofile)
    except repofileutils.InvalidRepoDefinition as e:
        raise StopActorExecutionError(
            message='{}: {}'.format(error_msg, str(e)),
            details={'hint': hint})
