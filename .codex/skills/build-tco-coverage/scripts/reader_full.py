"""完整阅读伴随的局部门禁与离线 SVG；不产生业务批准。"""
import html
import hashlib
import re
import textwrap
from collections import Counter
import xml.etree.ElementTree as ET
from urllib.parse import unquote

PROFILE = 'full_detail.v1'

def safe_id(value):
    return 'a-' + hashlib.sha256(str(value).encode()).hexdigest()[:20]

def text(value):
    if isinstance(value, dict):
        return '；'.join(str(k) + '：' + text(v) for k, v in value.items())
    if isinstance(value, list):
        return '；'.join(text(v) for v in value) or '未列条目'
    if value is None:
        return '未知'
    return str(value)

def known_bad(value, title=''):
    value = value.strip()
    return not value or value == title.strip() or value in {'同上', '详见', '待确认', '未知', 'N/A', '完成本步骤', '...', '…'} or bool(re.search(r'^(同上|详见)([。！!]|$)', value)) or value.endswith(('...', '…', '（略）'))

def audit(data, audited, require_full=False):
    detail = data['detail']
    full = detail.get('reading_profile') == PROFILE
    findings = audited[0]
    def fail(code, message, key=''):
        findings.append({'code': code, 'severity': 'error', 'subject_id': key, 'message': message})
    if require_full and not full:
        fail('READER_FULL_REQUIRED', '正式完整阅读必须声明 full_detail.v1，不能将旧稿重标完整。')
    if not full:
        return
    _, nodes, caps, _, _, tree, _ = audited
    def provenance(value, key, code):
        fields = ['source_refs', 'inference', 'unknowns']
        if any(not isinstance(value.get(f), list) for f in fields) or not any(value.get(f) for f in fields):
            fail(code, '来源、推断、具体未知必须完整，且不能三者全空。', key)
        for ref in value.get('source_refs', []):
            try:
                filename, sep, pointer = ref.partition('#')
                source = next(k for k, binding in detail['bindings'].items() if binding['path'] == filename)
                obj = data[source]
                if sep:
                    if not pointer.startswith('/'):
                        raise ValueError()
                    for token in pointer[1:].split('/'):
                        token = token.replace('~1', '/').replace('~0', '~')
                        obj = obj[int(token)] if isinstance(obj, list) else obj[token]
                else:
                    raise ValueError()
            except (ValueError, KeyError, StopIteration, IndexError, TypeError):
                # Existing statement/evidence identifiers are also valid sources.
                ids = {s['id'] for s in data['kym'].get('statements', [])}
                ids.update(r for c in detail['capabilities'].values() for a in c['assessment'] for r in a['evidence_refs'])
                if ref not in ids:
                    fail('READER_BINDING_DRIFT', '说明来源未绑定或 JSON Pointer 不存在：' + ref, key)
        for field in ('inference', 'unknowns'):
            for item in value.get(field, []):
                if known_bad(item):
                    fail(code, '推断或未知必须具体说明根据或确认动作。', key)
    reading = detail.get('node_reading', {})
    for namespace, source in [('kym', tree), ('model', nodes if 'model' in data else {})]:
        values = reading.get(namespace, {})
        for key in values.keys() - source.keys():
            fail('READER_TSP_INCOMPLETE', '说明指向不存在的来源节点：' + namespace, key)
        for key, node in source.items():
            required = [field for field in ('topic', 'scope', 'purpose') if node.get(field)]
            if node.get('node_kind') == 'm1' or ('model' not in data and key in caps):
                required = ['topic', 'scope', 'purpose']
            for field in required:
                value = values.get(key, {}).get(field)
                if not isinstance(value, dict) or known_bad(value.get('text', ''), node.get('title', '')):
                    fail('READER_TSP_INCOMPLETE', namespace + ' 缺少具体 ' + field + ' 解释。', key)
                    continue
                provenance(value, key, 'READER_TSP_INCOMPLETE')
                if not node.get(field) and not value.get('unknowns'):
                    fail('READER_TSP_INCOMPLETE', '源字段未提供，须列具体未知及确认动作：' + field, key)
    for key in caps:
        cap = detail['capabilities'].get(key)
        if not cap:
            fail('READER_FULL_REQUIRED', '缺少实际能力完整说明。', key)
            continue
        steps = cap['process']
        ids = {s['id'] for s in steps}
        entries = cap.get('entry_step_ids', [])
        if not entries or len(entries) != len(set(entries)) or not set(entries).issubset(ids):
            fail('READER_GRAPH_INVALID', '入口必须非空、唯一且全部指向真实步骤。', key)
        if known_bad(cap.get('entry_description', '')):
            fail('READER_PROCESS_INCOMPLETE', '缺少进入条件说明。', key)
        termination = cap.get('termination', {})
        if any(known_bad(termination.get(f, '')) for f in ('description', 'continuation')) or not isinstance(termination.get('unknowns'), list):
            fail('READER_PROCESS_INCOMPLETE', '缺少终止结果、持续/下一轮条件或具体未知。', key)
        reachable, pending = set(), list(entries)
        by_id = {s['id']: s for s in steps}
        while pending:
            sid = pending.pop()
            if sid in reachable or sid not in by_id:
                continue
            reachable.add(sid)
            pending.extend(e['to'] for e in by_id[sid]['transitions'])
        if reachable != ids:
            fail('READER_GRAPH_INVALID', '存在从显式入口不可达的步骤。', key)
        for step in steps:
            if step.get('kind') not in {'process', 'decision'} or not step.get('inputs') or any(known_bad(v) for v in step.get('inputs', [])) or known_bad(step.get('result', ''), step['label']) or known_bad(step['description'], step['label']):
                fail('READER_PROCESS_INCOMPLETE', '步骤缺少类型、输入、具体动作或结果：' + step['id'], key)
            provenance(step, key, 'READER_PROCESS_INCOMPLETE')
            edges = step['transitions']
            if step.get('kind') == 'decision' and (len(edges) < 2 or len({e['condition'] for e in edges}) != len(edges)):
                fail('READER_GRAPH_INVALID', '判断步骤须至少两条条件不同的出边。', key)
            if any(known_bad(e['condition']) or e['to'] not in ids for e in edges):
                fail('READER_GRAPH_INVALID', '转移条件空泛或目标不存在。', key)


def graph(cap, view, page=''):
    """有序主列及每边独立侧轨；节点/业务边保留可核对属性。"""
    esc = lambda v: html.escape(str(v), quote=True)
    steps = cap['process']
    edges = [(s['id'], e['condition'], e['to']) for s in steps for e in s['transitions']]
    entries = cap.get('entry_step_ids', [])
    terminals = [s for s in steps if not s['transitions']]
    lines = {s['id']: textwrap.wrap(s['label'], 18) or [''] for s in steps}
    heights = {s['id']: max(72, len(lines[s['id']]) * 24 + 32) * (2 if s.get('kind') == 'decision' else 1) for s in steps}
    positions = {}; cursor = 130
    for s in steps:
        positions[s['id']] = (cursor + heights[s['id']] / 2)
        cursor += heights[s['id']] + (110 if s in terminals else 75)
    width = max(760, 750 + len(edges) * 240)
    height = cursor + 90
    marker = safe_id(view + '/arrow')
    out = ['<svg xmlns="http://www.w3.org/2000/svg" role="img" aria-label="传统流程图" width="' + str(width) + '" height="' + str(height) + '" viewBox="0 0 ' + str(width) + ' ' + str(height) + '"><defs><marker id="' + marker + '" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto"><path d="M0 0 L10 4 L0 8 Z" fill="#183653"/></marker></defs><rect width="100%" height="100%" fill="#f5f7f9"/>']
    def label(x, y, value, small=False):
        chunks = textwrap.wrap(str(value), 18 if not small else 15) or ['']
        return '<text x="' + str(x) + '" y="' + str(y) + '" text-anchor="middle" fill="#183653" font-family="sans-serif" font-size="' + ('13' if small else '16') + '">' + ''.join('<tspan x="' + str(x) + '" dy="' + ('0' if i == 0 else '21') + '">' + esc(chunk) + '</tspan>' for i, chunk in enumerate(chunks)) + '</text>'
    for index, (src, condition, dst) in enumerate(edges):
        if src not in positions or dst not in positions:
            continue
        rail = 470 + index * 240
        sy, ty = positions[src], positions[dst]
        # Separate ports prevent a self-loop from collapsing to a zero-length line.
        sy -= 12; ty += 12
        out.append('<g data-edge-from="' + esc(src) + '" data-edge-to="' + esc(dst) + '" data-condition="' + esc(condition) + '"><path d="M410 ' + str(sy) + ' H' + str(rail) + ' V' + str(ty) + ' H410" fill="none" stroke="#183653" stroke-width="2" marker-end="url(#' + marker + ')"/>' + label(rail + 105, min(sy, ty) + 23, condition, True) + '</g>')
    if entries:
        out.append('<ellipse data-decoration="start" cx="230" cy="45" rx="110" ry="28" fill="white" stroke="#183653"/>' + label(230, 50, '开始：显式入口'))
        for index, sid in enumerate(entries):
            if sid in positions:
                entry_rail = 25 + index * 15
                out.append('<path data-decoration="entry" d="M120 45 H' + str(entry_rail) + ' V' + str(positions[sid]) + ' H50" fill="none" stroke="#183653" marker-end="url(#' + marker + ')"/>')
    for step_index, s in enumerate(steps, 1):
        sid = s['id']; y = positions[sid]; h = heights[sid]
        node_id = safe_id(view + '/graph/' + sid)
        out.append('<a id="' + node_id + '" data-step-id="' + esc(sid) + '" href="' + esc(page + '#' + safe_id(view + '/' + sid)) + '">')
        if s.get('kind') == 'decision':
            out.append('<polygon points="230,' + str(y-h/2) + ' 410,' + str(y) + ' 230,' + str(y+h/2) + ' 50,' + str(y) + '" fill="white" stroke="#183653" stroke-width="2"/>')
        else:
            out.append('<rect x="50" y="' + str(y-h/2) + '" width="360" height="' + str(h) + '" rx="4" fill="white" stroke="#183653" stroke-width="2"/>')
        out.append(label(230, y - (len(lines[sid])-1)*10, s['label']) + '</a>')
        if s in terminals:
            end_y = y+h/2+65
            out.append('<path data-decoration="terminal" d="M230 ' + str(y+h/2) + ' V' + str(end_y-27) + '" stroke="#183653" marker-end="url(#' + marker + ')"/><ellipse data-decoration="end" cx="230" cy="' + str(end_y) + '" rx="175" ry="28" fill="white" stroke="#183653"/>' + label(230, end_y+5, '结束 · 步骤 ' + str(step_index), True))
    if not entries:
        out.append(label(230, 45, '旧稿：入口和步骤类型未确认'))
    out.append('</svg>')
    return ''.join(out)



def verify_graph(svg, cap):
    """逐节点/业务边守恒；装饰边不参与计数。"""
    root = ET.fromstring(svg)
    expected_nodes = Counter(s['id'] for s in cap['process'])
    actual_nodes = Counter(e.attrib['data-step-id'] for e in root.iter() if 'data-step-id' in e.attrib)
    expected_edges = Counter((s['id'], e['condition'], e['to']) for s in cap['process'] for e in s['transitions'])
    actual_edges = Counter((e.attrib['data-edge-from'], e.attrib['data-condition'], e.attrib['data-edge-to']) for e in root.iter() if 'data-edge-from' in e.attrib)
    if actual_nodes != expected_nodes or actual_edges != expected_edges:
        raise ValueError('READER_GRAPH_INVALID：SVG 节点或业务边未与同源流程守恒。')
    return True


def flow(cap, view, markdown=False):
    esc = lambda v: html.escape(str(v), quote=True).replace('\n', '<br>')
    svg = graph(cap, view)
    verify_graph(svg, cap)
    filename = 'diagrams/' + safe_id(view) + '.svg'
    if markdown:
        out = ['![同源传统流程图](' + filename + ')\n']
    else:
        out = ['<div class="svg-scroll">' + svg + '</div><p><a href="' + filename + '">打开本地流程图</a></p>']
    out.append('<p>图例：椭圆表示开始或真实终止，矩形表示处理，菱形表示判断；箭头文字是转移条件，回路线保留原方向。</p>')
    out.append('<p>' + esc('进入条件：' + cap.get('entry_description', '旧稿未提供，不猜入口。')) + '</p>')
    out.append('<h3>处理 Process</h3>')
    for step in cap['process']:
        out.append('<article id="' + safe_id(view + '/' + step['id']) + '"><h4>' + esc(step['label']) + '</h4><table><tr><th>输入</th><td>' + esc(text(step.get('inputs', ['旧稿未提供']))) + '</td></tr><tr><th>具体动作</th><td>' + esc(step['description']) + '</td></tr><tr><th>结果</th><td>' + esc(step.get('result', '旧稿未提供')) + '</td></tr><tr><th>后续条件</th><td>' + esc(text([e['condition'] + ' → ' + e['to'] for e in step['transitions']]) if step['transitions'] else '真实终止步骤，无后继转移。') + '</td></tr><tr><th>来源 / 推断 / 未知</th><td>' + esc(text({f: step.get(f, []) for f in ('source_refs', 'inference', 'unknowns')})) + '</td></tr></table><a href="' + (filename if markdown else '') + '#' + safe_id(view + '/graph/' + step['id']) + '">返回本图步骤节点</a></article>')
    out.append('<p>' + esc('终止与继续：' + text(cap.get('termination', '旧稿未提供；不推定持续或结束条件。'))) + '</p>')
    return '\n'.join(out)


def readings(data, markdown=False, links=None, select=None):
    esc = lambda v: html.escape(str(v), quote=True)
    if data['detail'].get('reading_profile') != PROFILE:
        return '' if select else '<p class="notice">旧稿仅可读，未按新版完整阅读验收。</p>'
    sources = {'kym': {}, 'model': {n['id']: n for n in data.get('model', {}).get('nodes', [])}}
    def walk(n):
        sources['kym'][n['id']] = n
        for c in n.get('children', []): walk(c)
    walk(data['kym']['tree'])
    out = ['<section><h3>本能力的主题、范围与目的详解</h3>'] if select else ['<section><h2>其他实际节点的主题、范围与目的</h2><p>能力节点在各 M 树下详述；此处保留其他实际 TSP。阅读版本 full_detail.v1 的完整结构检查不等于语义批准。</p>']
    for namespace, values in data['detail'].get('node_reading', {}).items():
        for key, fields in values.items():
            if select and (namespace, key) != select:
                continue
            node = sources[namespace].get(key, {})
            if not select and ((namespace == 'model' and node.get('node_kind') == 'm1') or (namespace == 'kym' and 'model' not in data and key in data['detail']['capabilities'])):
                continue
            out.append('<details><summary>' + esc(namespace + ' · ' + key + ' · ' + node.get('title', key)) + '</summary><table><tr><th>字段</th><th>来源原值</th><th>具体解释</th><th>来源、推断与未知</th></tr>')
            for field, value in fields.items():
                refs = []
                for ref in value['source_refs']:
                    base, sep, pointer = ref.partition('#')
                    href = (links or {}).get(base)
                    refs.append('<a href="' + esc(href) + '">' + esc(ref) + '</a>' if href else esc(ref))
                out.append('<tr><th>' + {'topic':'主题', 'scope':'范围', 'purpose':'目的'}[field] + '</th><td>' + esc(text(node.get(field, '源字段未提供'))) + '</td><td>' + esc(value['text']) + '</td><td>' + '；'.join(refs) + '<br>' + esc(text({'推断':value['inference'], '未知及确认动作':value['unknowns']})) + '</td></tr>')
            out.append('</table></details>')
    return ''.join(out) + '</section>'
