import os

from leapp.exceptions import StopActorExecutionError
from leapp.libraries.common.config.version import matches_source_version
from leapp.libraries.common.gpg import (
    GpgReadError,
    get_gpg_ascii_from_rpmdb,
    get_path_to_gpg_certs,
    GPG_PQC_CERTS_SUBFOLDER,
    iter_gpg_keyfiles,
    parse_gpg_key_from_file,
    parse_gpg_key_from_ascii
)
from leapp.libraries.common.rpms import has_package
from leapp.libraries.stdlib import api
from leapp.models import GpgKey, InstalledRPM, MisplacedTrustedGpgKeys, TrustedGpgKeys

_PQRPM_DB_PATH = '/usr/lib/pqrpm/lib/sysimage/rpm/'


def _key_info_to_model(info, rpmdb, filename=None):
    model = GpgKey(
        fingerprint=info.short_keyid,
        short_keyid=info.short_keyid,
        full_fingerprint=info.fingerprint,
        rpmdb=rpmdb,
    )
    if filename is not None:
        model.filename = filename
    return model


def _parse_trusted_keyfiles(include_pqc):
    """
    :rtype: Dict[String, List[GpgKeyInfo]]
    """
    file_to_fps = {}
    for key_file in iter_gpg_keyfiles(include_pqc=include_pqc):
        file_to_fps[key_file] = parse_gpg_key_from_file(key_file)
    return file_to_fps


def _get_parsed_keys_from_rpmdb(db_path=None):
    ascii_strs = get_gpg_ascii_from_rpmdb('gpg-pubkey', db_path)
    if ascii_strs is None:
        api.current_logger.debug('No gpg-pubkey rpms found in rpmdb at {}', db_path)
    # flatten, though each pubkey should only ever have one key,
    # parse_gpg_key_from_ascii returns a list
    ret = []
    for ascii_str in (ascii_strs or []):
        try:
            ret.extend(parse_gpg_key_from_ascii(ascii_str))
        except GpgReadError as err:
            # NOTE(mmatuska): the actor never handled gpg parsing errors, keeping that
            # behaviour, but it could be changed now
            api.current_logger().error(str(err))
    return ret


def _get_pubkeys(installed_rpms, keyfiles_to_infos):
    """
    Get pubkeys from installed rpms and the trusted directory
    """
    key_infos = _get_parsed_keys_from_rpmdb()
    lookup = {key.fingerprint for key in key_infos}

    # pqrpm, which owns the pqrpmdb is only available on >= 9.7 && < 10.0,
    # however 'sq', without which full PQC enablement during the upgrade is not
    # possible, is only available on >= 9.9.
    if has_package(InstalledRPM, 'pqrpm') and matches_source_version('>= 9.9'):
        # deduplicate, GpgKeyInfo is hashable, but lets use just the
        # fingerprint as the hash
        pqc_key_infos = [
            k
            for k in _get_parsed_keys_from_rpmdb(_PQRPM_DB_PATH)
            if k.fingerprint not in lookup
        ]
        key_infos += pqc_key_infos
        lookup |= {k.fingerprint for k in pqc_key_infos}

    pubkeys = [_key_info_to_model(i, rpmdb=True) for i in key_infos ]

    for keyfile, infos in keyfiles_to_infos.items():
        for info in infos:
            if info.fingerprint not in lookup:
                pubkeys.append(_key_info_to_model(info, rpmdb=False, filename=keyfile))
                lookup.add(info.fingerprint)
    return pubkeys


def _get_misplaced_keyfiles(keyfiles_to_infos):
    """
    Get keys incorrectly placed within the truste GPG keys directory
    """
    root_dir = get_path_to_gpg_certs()
    pqc_dir = os.path.join(root_dir, GPG_PQC_CERTS_SUBFOLDER)

    misplaced_pqc = []  # pqc (v6) keys found at the 'root_dir'
    misplaced_classic = []  # non-PQC keys found in the 'pqc_dir'

    for keyfile, key_infos in keyfiles_to_infos.items():
        if os.path.dirname(keyfile) == pqc_dir:
            misplaced_classic += [
                _key_info_to_model(key, rpmdb=False, filename=keyfile)
                for key in key_infos
                if not key.is_pqc
            ]
        else:
            # subdirs other than pqc are ignored by the iterator
            misplaced_pqc += [
                _key_info_to_model(key, rpmdb=False, filename=keyfile)
                for key in key_infos
                if key.is_pqc
            ]

    return misplaced_pqc, misplaced_classic


def process():
    """
    Process keys in RPM DB and the ones in trusted directory to produce a list of trusted keys

    On 9.9 and 9.10 also search for and produce a MisplacedTrustedGpgKeys message with GPG keys
    misplaced in the trusted directory.
    """

    try:
        installed_rpms = next(api.consume(InstalledRPM))
    except StopIteration:
        raise StopActorExecutionError(
            'Could not check for valid GPG keys', details={'details': 'No InstalledRPM facts'}
        )

    has_sq = matches_source_version('>= 9.9')
    # without sq there is no way to process PQC keys
    keyfiles_to_infos = _parse_trusted_keyfiles(include_pqc=has_sq)

    pubkeys = _get_pubkeys(installed_rpms, keyfiles_to_infos)
    api.produce(TrustedGpgKeys(items=pubkeys))

    if not has_sq:
        return

    misplaced_pqc, misplaced_classic = _get_misplaced_keyfiles(keyfiles_to_infos)
    if misplaced_pqc or misplaced_classic:
        api.produce(
            MisplacedTrustedGpgKeys(
                pqc_keys=misplaced_pqc, classic_keys=misplaced_classic
            )
        )
