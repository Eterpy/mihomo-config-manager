import time

import pytest
from tests.helpers import link


def test_tabs_and_token_refresh(client, keys, settings):
    first = client.headers['X-CSRF-Token']
    client.cookies.clear()
    client.get('/')
    second = client.get('/api/csrf').json()['csrf_token']
    assert second != first
    for index, csrf in enumerate((second, first)):
        response = client.post('/api/configs', files={'file': (f'{index}.yaml', b'x: 1')}, headers={'X-CSRF-Token': csrf})
        assert response.status_code == 201
    assert client.post('/api/configs', headers={'X-CSRF-Token': 'expired'}).json()['code'] == 'csrf'
    assert client.post('/api/configs', headers={'Cf-Access-Jwt-Assertion': keys.token(settings, sub='other')}).status_code == 403
    assert 'access-control-allow-origin' not in client.get('/api/csrf', headers={'Origin': 'https://other.test'}).headers


@pytest.mark.parametrize('claim,offset,status', [('iat', 30, 200), ('nbf', 30, 200), ('exp', -30, 200),
                                              ('iat', 120, 401), ('nbf', 120, 401), ('exp', -120, 401)])
def test_access_clock_skew(client, keys, settings, claim, offset, status):
    token = keys.token(settings, **{claim: int(time.time()) + offset})
    assert client.get('/api/status', headers={'Cf-Access-Jwt-Assertion': token}).status_code == status


def test_navigation_and_api_expiry(client):
    headers = {'Cf-Access-Jwt-Assertion': '', 'Accept': 'text/html'}
    response = client.get('/', headers=headers)
    assert response.status_code == 401 and 'text/html' in response.headers['content-type']
    assert 'name="viewport" content="width=device-width, initial-scale=1"' in response.text
    assert '/cdn-cgi/access/logout' in response.text and '<script' not in response.text
    assert client.get('/api/configs', headers=headers).headers['content-type'] == 'application/json'


def test_link_only_on_demand(client, upload, application):
    item = upload().json()
    base = '/api/configs/' + item['id']
    token = link(client, item).split('/')[-2]
    for response in (item, client.get('/api/configs').json(), client.get(base).json()):
        assert token not in str(response) and 'subscription_url' not in str(response)
    for headers, code in [({'Origin': ''}, 403), ({'X-CSRF-Token': ''}, 403), ({'Cf-Access-Jwt-Assertion': ''}, 401)]:
        assert client.post(base + '/subscription-link', headers=headers).status_code == code
    application.state.store.snapshotting = True
    try:
        assert client.post(base + '/subscription-link').status_code == 200
    finally:
        application.state.store.snapshotting = False


@pytest.mark.parametrize('method,suffix', [('PATCH', ''), ('DELETE', ''), ('POST', '/enable'),
                                         ('POST', '/disable'), ('POST', '/rotate-token'), ('PUT', '/content'),
                                         ('POST', '/versions/1/rollback')])
def test_config_writes_require_current_revision(client, upload, method, suffix):
    item = upload().json()
    base = '/api/configs/' + item['id']
    changed = client.put(base + '/content', params={'expected_current_version': 1}, files={'file': ('x.yml', b'x: 2')}).json()
    def send(expected):
        if method == 'PUT':
            return client.put(base + suffix, params=expected, files={'file': ('x.yml', b'x: 3')})
        if method == 'PATCH' or suffix.endswith('/rollback'):
            return client.request(method, base + suffix, json=expected)
        return client.request(method, base + suffix, params=expected)
    versioned = method == 'PUT' or suffix.endswith('/rollback')
    expired = {'expected_current_version': 1} if versioned else {'expected_updated_at': item['updated_at']}
    assert send({}).status_code == 428
    if method == 'PATCH' or suffix.endswith('/rollback'):
        assert client.request(method, base + suffix).status_code == 428
    stale = send(expired)
    assert stale.status_code == 409 and stale.json()['current'] == changed
    assert client.get(base).json() == changed


@pytest.mark.parametrize('stem', ['x' * 101, 'x' * 250, '字' * 84, '字' * 200, ' '])
def test_long_default_names(upload, stem):
    name = stem + '.yaml'
    response = upload(filename=name)
    assert response.status_code == 201
    assert response.json()['filename'] == name
    assert response.json()['name'] == (stem.strip()[:100] or '.yaml')


def test_filename_errors(upload):
    assert upload(filename='x' * 251 + '.yaml').json()['code'] == 'filename_too_long'
    assert upload(filename='../x.yaml').json()['code'] == 'filename_invalid'
    assert upload(name='x' * 101).json()['code'] == 'name_invalid'


@pytest.mark.parametrize('version', [0, -1, 2**63, 2**63 - 1])
def test_history_version_range(client, upload, version):
    item = upload().json()
    path = f"/api/configs/{item['id']}/versions/{version}"
    expected = 404 if version == 2**63 - 1 else 422
    assert client.get(path + '/preview').status_code == expected
    assert client.post(path + '/rollback', json={'expected_current_version': 1}).status_code == expected


@pytest.mark.parametrize('method,suffix', [('DELETE', ''), ('POST', '/enable'), ('PATCH', '')])
def test_invalid_config_id_with_and_without_precondition(client, method, suffix):
    path = '/api/configs/not-a-uuid' + suffix
    fields = {'expected_updated_at': 'example'}
    assert client.request(method, path).status_code == 422
    arguments = {'json': fields} if method == 'PATCH' else {'params': fields}
    assert client.request(method, path, **arguments).status_code == 422


@pytest.mark.parametrize('value', ['a\0b', 'a\x07b', '\x7f', '\x85', '字' * 2001, '第一行\r\n\t第二行', '字' * 2000],
                         ids=['nul', 'bell', 'del', 'c1', 'too-long', 'multiline', 'max-length'])
def test_upload_and_rename_description(client, upload, value):
    base = upload().json()
    path = '/api/configs/' + base['id']
    created = client.post('/api/configs', files={'file': ('second.yaml', b'x: 2')}, data={'description': value})
    renamed = client.patch(path, json={'description': value, 'expected_updated_at': base['updated_at']})
    if value.startswith('第一行') or len(value) == 2000:
        assert (created.status_code, renamed.status_code) == (201, 200)
        for item in [created.json(), renamed.json()]:
            assert client.get('/api/configs/' + item['id']).json()['description'] == value
    else:
        assert created.status_code == renamed.status_code == 422
        assert created.json()['code'] == renamed.json()['code'] == 'description_invalid'
        assert client.get('/api/configs').json() == [base]
