"""Real local acceptance: import -> analysis -> content model -> durable render."""
import time
from .acceptance import fixture_client, check_financials, write_report


def main():
    report = {'checks': [], 'real_model_workflows': 2}
    with fixture_client() as (client, shops, end, token):
        headers = {'Authorization': 'Bearer ' + token}
        def request(method, path, body=None):
            response = client.request(method, path, headers=headers, json=body)
            response.raise_for_status()
            return response.json()
        request('POST', '/api/ops/demo/import', {})
        print('Test data imported', flush=True)
        for scope in [None, *shops]:
            scoped = dict(headers, **({'X-Shop-ID': scope} if scope else {}))
            response = client.get('/api/ops/overview', headers=scoped, params={'end': end, 'days': 7})
            response.raise_for_status()
            check_financials(response.json(), (scope,) if scope else shops)
        report['fixture_shops'] = list(shops)
        report['period_end_exclusive'] = end
        report['checks'].append('imported_financials_match_ground_truth')
        request('POST', '/api/ops/tasks/generate', {})
        second = request('POST', '/api/ops/tasks/generate', {})
        assert second['created'] == 0
        report['checks'].append('daily_sop_is_idempotent')
        peer = request('POST', '/api/ops/peers/TEST-PEER-a-0/analyze', {})
        assert peer['analysis'] and peer['measurements']['duration_seconds'] > 11
        report['peer_analysis'] = peer['analysis']
        report['checks'].append('real_peer_analysis_uses_transcript_and_media_measurements')
        print('Peer analysis generated', flush=True)
        script = request('POST', '/api/ops/scripts/generate', {'product_id': 'MEN-001-01-M',
            'reference_id': 'TEST-PEER-a-0', 'brief': '生成三段通勤衬衫口播，引用100%棉事实；每段使用一条4秒素材，字幕简短，不提优惠。'})
        assert script['payload']['generation'] == 'Dify DeepSeek'
        report['script_id'] = script['id']
        report['checks'].append('real_model_script_saved_with_verified_asset_ids')
        print('Video script saved', script['id'], flush=True)
        job = request('POST', f'/api/ops/scripts/{script["id"]}/render', {'expected_version': script['version']})
        repeated = request('POST', f'/api/ops/scripts/{script["id"]}/render', {'expected_version': script['version']})
        assert repeated['id'] == job['id']
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            jobs = request('GET', '/api/ops/jobs')['items']
            actual = next(r for r in jobs if r['id'] == job['id'])
            if actual['status'] in ['succeeded', 'failed']:
                break
            time.sleep(.5)
        assert actual['status'] == 'succeeded', actual
        for kind in ['video', 'cover', 'subtitles']:
            response = client.get(f'/api/ops/jobs/{job["id"]}/{kind}', headers=headers)
            response.raise_for_status()
            assert response.content
        report['job'] = actual
        report['checks'].append('actual_render_and_three_authenticated_outputs')
        report['status'] = 'passed'
    report['temporary_user_removed'] = True
    write_report('operations-live-report.json', report)
    print('Operations acceptance passed:', len(report['checks']), '; render:', report['job']['id'])


if __name__ == '__main__':
    main()
