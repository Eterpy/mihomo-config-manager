"""Run with python -m tests.browser_review in a Playwright Chromium environment."""
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import expect, sync_playwright


def check_page(page):
    app = Path(__file__).resolve().parents[1] / 'app'
    html = Environment(loader=FileSystemLoader(app / 'templates')).get_template('index.html').render(
        csrf_token='fixture', max_mib=1)
    item = dict(id='sample', name='测试', filename='test.yaml', size=5, sha256='a' * 64, version=2,
                enabled=True, status='ready', updated_at='2026-11-28T00:00:00Z', description='')
    job = dict(id='job', status='ready', size=50, created_at=item['updated_at'])
    state = dict(ready=True, snapshotting=False, max_upload_bytes=1048576)
    configs = [item]
    jobs = [job]
    list_failures = 0
    backup_list_failures = 0
    status_failures = 0
    same_content = False
    held_backups = []
    hold_backup = False
    calls = Counter()
    errors = []

    def serve(route):
        nonlocal list_failures, status_failures, backup_list_failures
        path = urlsplit(route.request.url).path
        method = route.request.method
        calls[path, method] += 1
        if path == '/':
            return route.fulfill(content_type='text/html', body=html)
        if path.startswith('/static/'):
            return route.fulfill(path=app / path[1:])
        if path == '/api/status':
            if status_failures:
                status_failures -= 1
                return route.fulfill(status=503, json={'detail': '状态暂不可用'})
            return route.fulfill(json=state)
        if path == '/api/configs':
            if list_failures:
                list_failures -= 1
                return route.fulfill(status=503, json={'detail': '暂时无法读取配置'})
            return route.fulfill(json=configs)
        if path == '/api/backups' and method == 'GET':
            if backup_list_failures:
                backup_list_failures -= 1
                return route.fulfill(status=503, json={'detail': '备份列表暂不可用'})
            if hold_backup:
                held_backups.append(route)
                return
            return route.fulfill(json=dict(items=jobs, used_bytes=50, quota_bytes=100, max_bytes=100))
        if path.endswith('/versions'):
            return route.fulfill(json=[dict(item, version=v, is_current=v == 2, sha256=str(2 if same_content else v) * 64,
                                            created_at=item['updated_at']) for v in (2, 1)])
        if path.endswith('/rollback') and same_content:
            return route.fulfill(status=409, json={'code': 'unchanged', 'detail': '相同内容'})
        return route.fulfill(status=409, json={'detail': f'{method} 操作失败'})

    page.route('**/*', serve)
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('dialog', lambda dialog: dialog.accept())
    page.goto('https://fixture.test/')
    expect(page.locator('#service-status')).to_have_text('运行正常')
    page.locator('#backup-new').click()
    password = 'example-password-first'
    page.locator('#backup-password').fill(password)
    confirm = page.locator('#backup-confirm')
    confirm.fill(password + '-different')
    submit = page.locator('#backup-form [type=submit]')
    submit.click()
    expect(confirm).to_be_focused()
    expect(confirm).to_have_js_property('validationMessage', '两次密码不一致')
    assert calls['/api/backups', 'POST'] == 0
    confirm.fill(password)
    expect(confirm).to_have_js_property('validationMessage', '')
    submit.click()
    backup_error = page.locator('#backup-error')
    expect(backup_error).to_have_text('POST 操作失败')
    expect(backup_error).to_be_in_viewport()
    jobs.extend(dict(job, id=f'backup-{n}') for n in range(27))
    job['status'] = 'encrypting'
    page.locator('#backup-refresh').click()
    rows = page.locator('#backup-list article')
    expect(rows).to_have_count(28)
    # Count both live text updates and live alerts inserted with existing text.
    page.evaluate("""() => {
        window.deleteAlerts = 0;
        const list = document.querySelector('#backup-list');
        new MutationObserver(records => {
            for (const record of records) {
                for (const node of record.addedNodes) {
                    if (node.nodeType === Node.ELEMENT_NODE) {
                        const alerts = node.matches('[role=alert]') ? [node] : node.querySelectorAll('[role=alert]');
                        for (const alert of alerts) if (alert.textContent === 'DELETE 操作失败') window.deleteAlerts++;
                    } else if (node.textContent === 'DELETE 操作失败' && node.parentElement?.closest('[role=alert]')) {
                        window.deleteAlerts++;
                    }
                }
            }
        }).observe(list, {childList: true, subtree: true});
    }""")
    rows.last.get_by_role('button', name='删除').click()
    expect(rows.last.get_by_role('alert')).to_have_text('DELETE 操作失败')
    expect(rows.last.get_by_role('alert')).to_be_in_viewport()
    expect(rows.last.get_by_role('button')).to_be_in_viewport()
    assert page.evaluate('window.deleteAlerts') == 1
    backup_list_failures = 1
    list_error = page.locator('#backup-list-error')
    create_error = page.locator('#backup-create-error')
    expect(list_error).to_have_text('备份列表暂不可用')
    expect(create_error).to_have_text('POST 操作失败')
    expect(list_error).to_be_empty()
    for _ in range(2):
        page.wait_for_event('response', predicate=lambda r: urlsplit(r.url).path == '/api/backups', timeout=5000)
    expect(backup_error).to_have_text('POST 操作失败')
    expect(rows.last.get_by_text('DELETE 操作失败')).to_be_in_viewport()
    assert page.evaluate('window.deleteAlerts') == 1
    rows.last.get_by_role('button', name='删除').click()
    expect(rows.last.get_by_role('alert')).to_have_text('DELETE 操作失败')
    assert page.evaluate('window.deleteAlerts') == 2
    job['status'] = 'ready'
    jobs[:] = [job]
    page.locator('#backup-refresh').click()
    expect(rows).to_have_count(1)

    page.locator('[data-action=history]').click()
    page.locator('#history-dialog').get_by_role('button', name='预览').first.click()
    expect(page.locator('#history-dialog:modal [role=alert]')).to_have_text('GET 操作失败')
    page.get_by_role('button', name='回滚到此版本').click()
    expect(page.locator('#history-dialog:modal [role=alert]')).to_have_text('POST 操作失败')
    page.locator('#history-dialog [data-close]').click()
    same_content = True
    page.locator('[data-action=history]').click()
    page.get_by_role('button', name='校验并修复当前文件').click()
    outcome = page.locator('#history-dialog:modal [role=status]')
    expect(outcome).to_have_text('当前文件完整，无需修复')
    expect(outcome).to_be_in_viewport()
    page.locator('#history-dialog [data-close]').click()
    badge = page.locator('[data-field=badge]')
    write_button = page.locator('[data-action=rename]')
    for status in ['pending', 'deleting']:
        item['status'] = status
        page.locator('#refresh').click()
        expect(badge).to_have_text('处理中')
        expect(write_button).to_be_disabled()
        if status == 'pending':
            list_failures = 1
            expect(page.locator('#service-status')).to_have_text('配置列表读取失败')
        else:
            status_failures = 1
            expect(page.locator('#service-status')).to_have_text('连接异常')
        item['status'] = 'ready'
        expect(badge).to_have_text('已启用')
        expect(write_button).to_be_enabled()
    state['ready'] = False
    item['status'] = 'pending'
    page.locator('#refresh').click()
    expect(badge).to_have_text('待恢复')
    expect(write_button).to_be_disabled()

    state['ready'] = True
    item['status'] = 'ready'
    page.locator('#refresh').click()
    expect(badge).to_have_text('已启用')
    job['status'] = 'encrypting'
    page.locator('#backup-refresh').click()
    expect(page.locator('#backup-new')).to_be_disabled()
    page.locator('summary').click()
    copy = page.locator('[data-action=copy]')
    copy.focus()
    before = calls.copy()
    state['snapshotting'] = True
    expect(write_button).to_be_disabled()
    state['snapshotting'] = False
    expect(write_button).to_be_enabled()
    # Wait through at least three complete 2-second polling intervals.
    while calls['/api/backups', 'GET'] < before['/api/backups', 'GET'] + 3:
        page.wait_for_event('response', predicate=lambda r: urlsplit(r.url).path == '/api/backups', timeout=5000)
    assert calls['/api/configs', 'GET'] == before['/api/configs', 'GET']
    expect(copy).to_be_focused()
    expect(page.locator('details')).to_have_js_property('open', True)

    # A remote change requires new card contents. Preserve the user's place here too.
    item['description'] = '新说明'
    page.evaluate("new BroadcastChannel('mcm').postMessage('refresh')")
    expect(page.locator('[data-field=description]')).to_have_text('新说明')
    expect(copy).to_be_focused()
    expect(page.locator('details')).to_have_js_property('open', True)
    assert page.locator('#list').get_attribute('aria-live') is None
    # Hold the terminal backup response until the earlier status response has reached the DOM.
    hold_backup = True
    state['snapshotting'] = True
    expect(page.locator('#service-status')).to_have_text('备份快照中')
    if not held_backups:
        page.wait_for_event('request', predicate=lambda r: urlsplit(r.url).path == '/api/backups')
    hold_backup = False
    state['snapshotting'] = False
    job['status'] = 'ready'
    held_backups.pop().fulfill(json=dict(items=jobs, used_bytes=50, quota_bytes=100, max_bytes=100))
    expect(page.locator('#service-status')).to_have_text('运行正常')
    expect(page.locator('#upload-top')).to_be_enabled()

    # Start without cached configurations. Healthy status polls cannot mask a list outage.
    job['status'] = 'encrypting'
    list_failures = 3
    before = calls.copy()
    page.reload()
    for _ in range(3):
        expect(page.locator('#notice')).to_have_text('暂时无法读取配置')
        expect(page.locator('#service-status')).to_have_text('配置列表读取失败')
        expect(page.locator('#upload-top')).to_be_disabled()
        expect(page.locator('#empty')).to_be_hidden()
        expect(page.locator('#total')).to_have_text('—')
        if list_failures:
            page.wait_for_event('response', predicate=lambda r: urlsplit(r.url).path == '/api/configs', timeout=5000)
    expect(badge).to_have_text('已启用')
    expect(page.locator('#upload-top')).to_be_enabled()
    expect(page.locator('#notice')).to_be_hidden()
    assert calls['/api/configs', 'GET'] >= before['/api/configs', 'GET'] + 4
    configs.clear()
    page.locator('#refresh').click()
    expect(page.locator('#empty')).to_be_visible()
    expect(page.locator('#total')).to_have_text('0')
    assert not errors, errors


if __name__ == '__main__':
    with sync_playwright() as driver:
        browser = driver.chromium.launch()
        try:
            check_page(browser.new_page(viewport={'width': 390, 'height': 700}))
        finally:
            browser.close()
    print('Chromium 管理页回归通过')
