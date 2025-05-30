import os

import pytest

from leapp.libraries.actor import removeobsoleterpmgpgkeys
from leapp.libraries.common.config.version import get_target_major_version
from leapp.libraries.common.rpms import has_package
from leapp.libraries.common.testutils import CurrentActorMocked, produce_mocked
from leapp.libraries.stdlib import api
from leapp.models import DNFWorkaround, InstalledRPM, RPM


def _get_test_installedrpm():
    return InstalledRPM(
        items=[
            RPM(
                name='gpg-pubkey',
                version='d4082792',
                release='5b32db75',
                epoch='0',
                packager='Red Hat, Inc. (auxiliary key 2) <security@redhat.com>',
                arch='noarch',
                pgpsig=''
            ),
            RPM(
                name='gpg-pubkey',
                version='2fa658e0',
                release='45700c69',
                epoch='0',
                packager='Red Hat, Inc. (auxiliary key) <security@redhat.com>',
                arch='noarch',
                pgpsig=''
            ),
            RPM(
                name='gpg-pubkey',
                version='12345678',
                release='abcdefgh',
                epoch='0',
                packager='made up',
                arch='noarch',
                pgpsig=''
            ),
        ]
    )


@pytest.mark.parametrize(
    "version, expected",
    [
        (9, ["gpg-pubkey-d4082792-5b32db75", "gpg-pubkey-2fa658e0-45700c69"]),
        (8, ["gpg-pubkey-2fa658e0-45700c69"])
    ]
)
def test_get_obsolete_keys(monkeypatch, version, expected):
    def get_target_major_version_mocked():
        return version

    monkeypatch.setattr(
        removeobsoleterpmgpgkeys,
        "get_target_major_version",
        get_target_major_version_mocked,
    )

    monkeypatch.setattr(
        api,
        "current_actor",
        CurrentActorMocked(
            msgs=[_get_test_installedrpm()]
        ),
    )

    cur_dir = os.path.dirname(os.path.abspath(__file__))
    monkeypatch.setattr(api, 'get_common_folder_path', lambda folder: os.path.join(cur_dir, '../../../files/', folder))

    keys = removeobsoleterpmgpgkeys._get_obsolete_keys()
    assert set(keys) == set(expected)


@pytest.mark.parametrize(
    "version, obsoleted_keys, expected",
    [
        (10, None, []),
        (10, {}, []),
        (10, {"8": ["gpg-pubkey-888-abc"], "10": ["gpg-pubkey-10-10"]}, ["gpg-pubkey-888-abc", "gpg-pubkey-10-10"]),
        (9, {"8": ["gpg-pubkey-888-abc"], "9": ["gpg-pubkey-999-def"]}, ["gpg-pubkey-999-def", "gpg-pubkey-888-abc"]),
        (8, {"8": ["gpg-pubkey-888-abc"], "9": ["gpg-pubkey-999-def"]}, ["gpg-pubkey-888-abc"])
    ]
)
def test_get_obsolete_keys_incomplete_data(monkeypatch, version, obsoleted_keys, expected):
    def get_target_major_version_mocked():
        return version

    def get_distribution_data_mocked(_distro):
        if obsoleted_keys is None:
            return {}
        return {'obsoleted-keys': obsoleted_keys}

    def has_package_mocked(*args, **kwargs):
        return True

    monkeypatch.setattr(
        removeobsoleterpmgpgkeys,
        "get_target_major_version",
        get_target_major_version_mocked,
    )

    monkeypatch.setattr(
        removeobsoleterpmgpgkeys,
        "get_distribution_data",
        get_distribution_data_mocked,
    )

    monkeypatch.setattr(
        removeobsoleterpmgpgkeys,
        "has_package",
        has_package_mocked,
    )

    monkeypatch.setattr(
        api,
        "current_actor",
        CurrentActorMocked(),
    )

    keys = removeobsoleterpmgpgkeys._get_obsolete_keys()
    assert set(keys) == set(expected)


@pytest.mark.parametrize(
    "keys, should_register",
    [
        (["gpg-pubkey-d4082792-5b32db75"], True),
        ([], False)
    ]
)
def test_workaround_should_register(monkeypatch, keys, should_register):
    def get_obsolete_keys_mocked():
        return keys

    monkeypatch.setattr(
        removeobsoleterpmgpgkeys,
        '_get_obsolete_keys',
        get_obsolete_keys_mocked
    )
    monkeypatch.setattr(api, 'produce', produce_mocked())
    monkeypatch.setattr(api, "current_actor", CurrentActorMocked())

    removeobsoleterpmgpgkeys.process()
    assert api.produce.called == should_register
