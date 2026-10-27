"""Tests for WifiManager helpers that shell out to system tools."""

from unittest.mock import patch

from utils import wifi_manager as wm_module
from utils.wifi_manager import WifiManager

WPA_STATUS = (
    "bssid=aa:bb:cc:dd:ee:ff\n"
    "freq=2437\n"
    "ssid=IOT_TAH\n"
    "id=0\n"
    "mode=station\n"
    "wpa_state=COMPLETED\n"
    "ip_address=192.168.99.33\n"
)


def _pi_manager():
    manager = WifiManager()
    manager._is_pi = True
    return manager


def test_ssid_falls_back_to_wpa_cli_without_iwgetid():
    # DietPi ships wpa_supplicant but not wireless-tools, so iwgetid is absent.
    manager = _pi_manager()
    with patch.object(wm_module.subprocess, "check_output", side_effect=FileNotFoundError), \
            patch.object(wm_module, "_run_wpa_cli", return_value=(True, WPA_STATUS)) as wpa:
        assert manager.get_wifi_ssid() == "IOT_TAH"
    wpa.assert_called_once_with(["-i", "wlan0", "status"])


def test_ssid_is_none_when_wpa_cli_reports_no_association():
    manager = _pi_manager()
    status = "wpa_state=DISCONNECTED\nip_address=169.254.1.2\n"
    with patch.object(wm_module.subprocess, "check_output", side_effect=FileNotFoundError), \
            patch.object(wm_module, "_run_wpa_cli", return_value=(True, status)):
        assert manager.get_wifi_ssid() is None


def test_ssid_is_none_when_no_tool_is_available():
    manager = _pi_manager()
    with patch.object(wm_module.subprocess, "check_output", side_effect=FileNotFoundError), \
            patch.object(wm_module, "_run_wpa_cli", return_value=(False, "not found")):
        assert manager.get_wifi_ssid() is None


def test_ssid_prefers_iwgetid_when_present():
    manager = _pi_manager()
    with patch.object(wm_module.subprocess, "check_output", return_value="HomeNet\n"), \
            patch.object(wm_module, "_run_wpa_cli") as wpa:
        assert manager.get_wifi_ssid() == "HomeNet"
    wpa.assert_not_called()
