"""Compare real Dify retrieval on a fixed, synthetic-document question set."""
import argparse
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from commerce import knowledge
from commerce.config import ROOT

CASES = [
    ('public', '尺码', ['成衣尺寸与人体尺寸', '尺码选择方法', '尺码与颜色库存']),
    ('public', '胸围和衣长的数值是量身体还是量衣服？', ['成衣尺寸与人体尺寸']),
    ('public', '混纺面料文案能不能写成百分百纯棉？', ['服装面料成分']),
    ('public', '只知道身高和体重，可以保证穿M码合身吗？', ['尺码选择方法']),
    ('internal', '退款与净收款口径', ['退款与净收款口径']),
    ('internal', '把退款扣掉后剩下的钱就是利润吗？', ['退款与净收款口径', '经营诊断的证据']),
    ('internal', '上一期为零时能说增长了百分之几吗？', ['时间窗口与环比']),
    ('internal', '没有流量和成本数据，可以推算转化率和净利润吗？', ['经营诊断的证据']),
    ('public', '如何证明哥德巴赫猜想？', []),
]

HELD_OUT = [
    ('public', '页面库存是昨天的，能直接告诉买家现在还有货吗？', ['商品库存']),
    ('public', '含四成羊毛的衣服，广告里能称为纯羊毛吗？', ['服装面料成分']),
    ('internal', '缺少成本和需求的候选商品，能跟数据完整的商品一起排名吗？', ['选品评分方法']),
    ('internal', '竞品报价都采集于两个月前，还能计算当前价格优势吗？', ['竞品可比性']),
    ('public', '明天深圳天气预报和降雨概率是多少？', []),
    ('internal', '求解一元二次方程的通用公式是什么？', []),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-hybrid', action='store_true')
    parser.add_argument('--held-out', action='store_true', help='Separate questions not used for initial retrieval configuration')
    args = parser.parse_args()
    rows = []
    for scope, query, expected in (HELD_OUT if args.held_out else CASES):
        started = time.perf_counter()
        result = knowledge.search(SimpleNamespace(role='operator'), query, scope)
        titles = [item['title'] for item in result['items']]
        passed = bool(set(titles) & set(expected)) if expected else not titles
        rows.append({'scope': scope, 'query': query, 'expected_any_title': expected,
                     'titles': titles, 'passed': passed, 'milliseconds': round((time.perf_counter() - started) * 1000, 1),
                     'retrieval': result['retrieval'],
                     'scores': [item['score'] for item in result['items']]})
        print(scope, passed, titles, flush=True)
    report = {'verified_at': datetime.now(timezone.utc).isoformat(), 'cases': rows,
              'passed_cases': sum(row['passed'] for row in rows), 'total_cases': len(rows),
              'scope': 'synthetic knowledge documents; real Dify retrieval API',
              'hybrid_required': args.require_hybrid, 'held_out': args.held_out}
    folder = ROOT / 'tests/artifacts/knowledge-hybrid'
    folder.mkdir(parents=True, exist_ok=True)
    name = 'hybrid.json' if args.require_hybrid else 'baseline.json'
    if args.held_out:
        name = 'held-out-' + name
    (folder / name).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if args.require_hybrid:
        assert all('hybrid' in row['retrieval'] and row['passed'] for row in rows), report


if __name__ == '__main__':
    main()
