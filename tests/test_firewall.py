from types import SimpleNamespace

import pytest

from panel import firewall


@pytest.mark.parametrize('host',['127.0.0.1','::1','localhost'])
def test_loopback_does_not_open_port(monkeypatch,host):
    monkeypatch.setattr(firewall,'dotenv_values',lambda _:dict(PANEL_HOST=host,PANEL_PORT='8090'))
    monkeypatch.setattr(firewall.shutil,'which',lambda _:pytest.fail('No firewall changes expected'))
    firewall.allow_panel_port()


def test_public_port_allowed_without_enabling_firewall(monkeypatch):
    monkeypatch.setattr(firewall,'dotenv_values',lambda _:dict(PANEL_HOST='0.0.0.0',PANEL_PORT='9100'))
    monkeypatch.setattr(firewall.shutil,'which',lambda _:'/usr/sbin/ufw')
    calls=[]
    monkeypatch.setattr(firewall.subprocess,'run',lambda args,**kwargs:calls.append(args) or SimpleNamespace(returncode=0))
    firewall.allow_panel_port()
    assert calls==[['/usr/sbin/ufw','allow','9100/tcp','comment','QueuePilot panel']]


def test_firewall_failure_is_visible(monkeypatch,capsys):
    monkeypatch.setattr(firewall,'dotenv_values',lambda _:dict(PANEL_HOST='0.0.0.0',PANEL_PORT='9100'))
    monkeypatch.setattr(firewall.shutil,'which',lambda _:'/usr/sbin/ufw')
    monkeypatch.setattr(firewall.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(returncode=1))
    firewall.allow_panel_port()
    assert 'WARNING' in capsys.readouterr().out
