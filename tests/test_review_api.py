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
