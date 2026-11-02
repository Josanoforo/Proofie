"""Variant-local hosted footer, synthetic access/SDK, no OAuth or real environment."""
import re
import html

import pytest
from _hosted import authenticated_client
from copilot import config
from copilot.app import create_app
from copilot.web.routes_disputes import CredentialsMissing,get_client
from copilot.hosted_ephemeral import create_app as ephemeral_app,NOTICE


class Empty:
    def call(self,method,path,op,**kwargs):
        assert method=='GET'
        return {'items':[]}


def missing():
    raise CredentialsMissing('DEMO DATA')


@pytest.mark.parametrize('variant',['ephemeral','disk'])
@pytest.mark.parametrize('page',['inbox','credentials'])
def test_footer_has_one_restart_sentence_and_variant_is_not_global(monkeypatch,tmp_path,variant,page):
    monkeypatch.setenv('COPILOT_HOSTED_DEMO','true')
    monkeypatch.setenv('COPILOT_ACCESS_CODE','test-hosted-access-code')
    monkeypatch.setenv('COPILOT_FEATURE_OUTCOME_APPEAL','false')
    monkeypatch.setenv('COPILOT_EPHEMERAL_ROOT',str(tmp_path/'sessions'))
    app=ephemeral_app() if variant=='ephemeral' else create_app()
    app.dependency_overrides[get_client]=missing if page=='credentials' else lambda:Empty()
    web=authenticated_client(app)
    response=web.get('/disputes')
    assert response.status_code==(503 if page=='credentials' else 200)
    # one sentence per line (spans); the text without the tags is what is compared
    footer=' '.join(html.unescape(re.sub(r'<[^>]+>',' ',re.search(r'<p class="hosted-demo-note"[^>]*>(.*?)</p>',response.text,re.S).group(1))).split())
    if variant=='ephemeral':
        assert footer.count(NOTICE)==1
        assert "The demo keeps its database and a log of its PayPal calls on the host's disk" not in footer
        assert 'a host restart may erase them' not in footer
    else:
        assert "The demo keeps its database and a log of its PayPal calls on the host's disk" in footer
        assert 'a host restart may erase them' in footer and NOTICE not in footer
    # Factory construction must not corrupt the default disk template globals for another app.
    disk=create_app();disk.dependency_overrides[get_client]=lambda:Empty()
    disk_page=authenticated_client(disk).get('/disputes')
    assert NOTICE not in disk_page.text
