"""Validate and package the operations fixtures without application/database access."""
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED

from .generate_ops_data import DEFAULT_OUTPUT, ROOT


def main():
    checked = subprocess.run([sys.executable, '-m', 'scripts.generate_ops_data', '--check'], cwd=ROOT,
                             check=True, capture_output=True, text=True)
    data_check = json.loads(checked.stdout)
    folder = DEFAULT_OUTPUT
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    assert (folder / '抖店运营测试数据.xlsx').is_file(), 'Export the workbook before packaging'
    report_folder = ROOT / 'tests' / 'artifacts' / 'ops-data'
    report_folder.mkdir(parents=True, exist_ok=True)
    junit = report_folder / 'tests.xml'
    subprocess.run([sys.executable, '-m', 'pytest', 'tests/unit/test_ops_data.py', '-q',
                    f'--junitxml={junit}'], cwd=ROOT, check=True)
    suites = ET.parse(junit).getroot().findall('testsuite')
    test_count = sum(int(s.attrib['tests']) for s in suites)
    ns = {'x': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    expected_totals = json.loads((folder / 'expected_results.json').read_text(encoding='utf-8'))
    with ZipFile(folder / '抖店运营测试数据.xlsx') as workbook:
        summary = ET.fromstring(workbook.read('xl/worksheets/sheet1.xml'))
        cells = {c.attrib['r']: c for c in summary.findall('.//x:c', ns)}
        for row, values in [(7, expected_totals['by_shop'][0]), (8, expected_totals['by_shop'][1]), (9, expected_totals['total'])]:
            for column, key in [('C', 'paid_fen'), ('D', 'refund_fen'), ('E', 'net_receipts_fen')]:
                assert abs(float(cells[f'{column}{row}'].find('x:v', ns).text) - values[key] / 100) < .00001
        for name in workbook.namelist():
            if name.startswith('xl/worksheets/sheet') and name.endswith('.xml'):
                sheet = ET.fromstring(workbook.read(name))
                assert not [c for c in sheet.findall('.//x:c', ns) if c.attrib.get('t') == 'e'], name
    media_checks = []
    for filename in manifest['media_files']:
        file = folder / 'media' / filename
        assert file.is_file(), filename
        if file.suffix != '.mp4':
            continue
        probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                                'format=duration:stream=codec_type,width,height', '-of', 'json', str(file)],
                               check=True, capture_output=True, text=True)
        info = json.loads(probe.stdout)
        streams = info['streams']
        video = next(s for s in streams if s['codec_type'] == 'video')
        assert (video['width'], video['height']) == (360, 640), filename
        duration = float(info['format']['duration'])
        expected = 4 if '_scene_' in filename else 12
        assert abs(duration - expected) < .1, filename
        assert any(s['codec_type'] == 'audio' for s in streams), filename
        if expected == 12:
            assert any(s['codec_type'] == 'subtitle' for s in streams), filename
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-xerror', '-i', str(file),
                        '-f', 'null', '-'], check=True, capture_output=True)
        media_checks.append(dict(file=filename, duration_seconds=duration, decode='passed'))
    report = dict(dataset_version=manifest['dataset_version'], data_checks=data_check['checks'],
                  offline_tests=dict(passed=test_count, failures=0), media=media_checks,
                  workbook=dict(cached_summary='reconciled_to_raw_data', cached_formula_errors=0),
                  agent_end_to_end='not_run', loaded_into_application=False)
    (report_folder / 'delivery-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    output = ROOT / 'tests' / 'artifacts' / 'packages' / 'douyin-ops-v1.zip'
    output.parent.mkdir(parents=True, exist_ok=True)
    included = []
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        for file in sorted(folder.rglob('*')):
            if file.is_file() and file.suffix in {'.json', '.jsonl', '.csv', '.md', '.mp4', '.srt', '.xlsx'}:
                archive.write(file, f'{folder.name}/{file.relative_to(folder).as_posix()}')
                included.append(file.relative_to(folder).as_posix())
        archive.write(report_folder / 'delivery-report.json', f'{folder.name}/delivery-report.json')
    with ZipFile(output) as archive:
        assert archive.testzip() is None
    print(json.dumps(dict(archive=str(output.relative_to(ROOT)), files=len(included) + 1,
                          bytes=output.stat().st_size, media_decodes=len(media_checks)), ensure_ascii=False))


if __name__ == '__main__':
    main()
