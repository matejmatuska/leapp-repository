import errno
import textwrap

import pytest
import six

from leapp.libraries.actor import networkmanagerconnectionscanner as nmconnscanner
from leapp.libraries.common.testutils import make_OSError, produce_mocked
from leapp.libraries.stdlib import api
from leapp.models import NetworkManagerConnection

_builtins_open = "builtins.open" if six.PY3 else "__builtin__.open"


def _listdir_nm_conn(path):
    if path == nmconnscanner.NM_CONN_DIR:
        return ["conn1.nmconnection"]
    raise make_OSError(errno.ENOENT)


def _listdir_nm_conn2(path):
    if path == nmconnscanner.NM_CONN_DIR:
        return ["conn1.nmconnection", "conn2.nmconnection"]
    raise make_OSError(errno.ENOENT)


def _load_from_file(keyfile, filename, flags):
    if filename.endswith(".nmconnection"):
        return keyfile.load_from_data(textwrap.dedent("""
            [connection]
            type=wifi
            id=conn1
            uuid=a1bc695d-c548-40e8-9c7f-205a6587135d

            [wifi]
            mode=infrastructure
            ssid=wifi

            [wifi-security]
            auth-alg=open
            key-mgmt=none
            wep-key-type=1
            wep-key0=abcde
        """), nmconnscanner.GLib.MAXSIZE, flags)
    raise make_OSError(errno.ENOENT)


@pytest.mark.skipif(not nmconnscanner.libnm_available, reason="NetworkManager g-ir not installed")
def test_no_conf(monkeypatch):
    """
    No report if there are no keyfiles
    """

    monkeypatch.setattr(nmconnscanner.os, "listdir", lambda _: ())
    monkeypatch.setattr(api, "produce", produce_mocked())
    nmconnscanner.process()
    assert not api.produce.called


@pytest.mark.skipif(not nmconnscanner.libnm_available, reason="NetworkManager g-ir not installed")
def test_nm_conn(monkeypatch):
    """
    Check a basic keyfile and check that secrets are gone
    """

    monkeypatch.setattr(nmconnscanner.os, "listdir", _listdir_nm_conn)
    monkeypatch.setattr(api, "produce", produce_mocked())
    monkeypatch.setattr(nmconnscanner.GLib.KeyFile, "load_from_file", _load_from_file)
    nmconnscanner.process()

    assert api.produce.called == 1
    assert len(api.produce.model_instances) == 1
    nm_conn = api.produce.model_instances[0]
    assert isinstance(nm_conn, NetworkManagerConnection)
    assert nm_conn.filename == "/etc/NetworkManager/system-connections/conn1.nmconnection"
    assert len(nm_conn.settings) == 3

    conn_settings = nm_conn.settings[0]
    assert conn_settings.name == "connection"
    assert len(conn_settings.properties) == 3
    # have to iterate, seems like the order of keys within a group is not
    # preserved
    for prop in conn_settings.properties:
        if prop.name == "id":
            assert prop.value == "conn1"

    wifi_sec_settings = nm_conn.settings[2]
    assert wifi_sec_settings.name == "wifi-security"
    assert len(wifi_sec_settings.properties) == 3

    for prop in wifi_sec_settings.properties:
        # It's important that wek-key0 is gone
        assert prop.name != "wep-key0"
        if prop.name == "auth-alg":
            assert prop.value == "open"


@pytest.mark.skipif(not nmconnscanner.libnm_available, reason="NetworkManager g-ir not installed")
def test_nm_conn2(monkeypatch):
    """
    Check a pair of keyfiles
    """

    monkeypatch.setattr(nmconnscanner.os, "listdir", _listdir_nm_conn2)
    monkeypatch.setattr(api, "produce", produce_mocked())
    monkeypatch.setattr(nmconnscanner.GLib.KeyFile, "load_from_file", _load_from_file)
    nmconnscanner.process()

    assert api.produce.called == 2
    assert len(api.produce.model_instances) == 2
    assert api.produce.model_instances[0].filename.endswith("/conn1.nmconnection")
    assert api.produce.model_instances[1].filename.endswith("/conn2.nmconnection")
